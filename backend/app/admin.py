"""P7 Admin (tự viết, theo mô tả công khai): đăng nhập nhiều người dùng, workspace, RBAC (admin/editor/viewer),
API key cá nhân, audit log, sandbox bền (thư mục riêng, chạy code trong container cô lập) + upload/download file.
Xác thực ở middleware: mặc định CHẶN mọi đường dẫn trừ danh sách công khai (đường public tự xác thực bằng key/chữ ký riêng)."""
import asyncio, base64, contextlib, contextvars, hashlib, hmac, json, os, re, secrets, shutil, time, uuid
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb
import deepagent_api as A

router = APIRouter()
DDL = """
CREATE TABLE IF NOT EXISTS users(id SERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL, display_name TEXT,
  is_superadmin BOOLEAN NOT NULL DEFAULT false, disabled BOOLEAN NOT NULL DEFAULT false, created TIMESTAMPTZ NOT NULL DEFAULT now(), last_login TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS workspaces(id SERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL, created TIMESTAMPTZ NOT NULL DEFAULT now());
INSERT INTO workspaces(id, name) VALUES(1, 'Mặc định') ON CONFLICT DO NOTHING;
-- 13.Z: never move the sequence backwards: a deleted workspace's id must not be handed to a new customer (rows left behind with that id would show up there)
SELECT setval('workspaces_id_seq', GREATEST((SELECT max(id) FROM workspaces), (SELECT last_value FROM workspaces_id_seq), 1));
CREATE TABLE IF NOT EXISTS memberships(user_id INT REFERENCES users(id) ON DELETE CASCADE, workspace_id INT REFERENCES workspaces(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (role IN ('admin','editor','viewer')), PRIMARY KEY(user_id, workspace_id));
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id INT REFERENCES users(id) ON DELETE CASCADE, workspace_id INT,
  expires TIMESTAMPTZ NOT NULL, ip TEXT, ua TEXT, created TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS user_api_keys(id SERIAL PRIMARY KEY, user_id INT REFERENCES users(id) ON DELETE CASCADE, workspace_id INT REFERENCES workspaces(id) ON DELETE CASCADE,
  name TEXT, prefix TEXT NOT NULL, key_hash TEXT UNIQUE NOT NULL, role TEXT CHECK (role IN ('admin','editor','viewer')), expires TIMESTAMPTZ,
  last_used TIMESTAMPTZ, revoked BOOLEAN NOT NULL DEFAULT false, created TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS audit_log(id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL DEFAULT now(), user_id INT, username TEXT, workspace_id INT, auth TEXT,
  method TEXT, path TEXT, status INT, ip TEXT, ms INT, detail TEXT);
CREATE INDEX IF NOT EXISTS audit_ts ON audit_log(ts DESC);
CREATE TABLE IF NOT EXISTS sandboxes(id TEXT PRIMARY KEY, name TEXT, workspace_id INT, owner_id INT, ttl_hours INT NOT NULL DEFAULT 168,
  created TIMESTAMPTZ NOT NULL DEFAULT now(), last_used TIMESTAMPTZ NOT NULL DEFAULT now(), runs INT NOT NULL DEFAULT 0);
ALTER TABLE agents ADD COLUMN IF NOT EXISTS workspace_id INT NOT NULL DEFAULT 1;
ALTER TABLE users ADD COLUMN IF NOT EXISTS lang TEXT NOT NULL DEFAULT 'en';
ALTER TABLE projects ADD COLUMN IF NOT EXISTS workspace_id INT NOT NULL DEFAULT 1;
ALTER TABLE datasets ADD COLUMN IF NOT EXISTS workspace_id INT NOT NULL DEFAULT 1;
ALTER TABLE users ADD COLUMN IF NOT EXISTS demo BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_cap_usd NUMERIC(10,4);
ALTER TABLE users ADD COLUMN IF NOT EXISTS demo_agents INT[];
ALTER TABLE user_api_keys ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'full';
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_secret_enc TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_enabled BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_recovery TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_failed INT NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_locked_until TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_last_step BIGINT NOT NULL DEFAULT 0;
ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS require_2fa BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE users ADD COLUMN IF NOT EXISTS instr_about TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS instr_style TEXT NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS instr_enabled BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS device TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ NOT NULL DEFAULT now();
"""
ROLES = {"viewer": 1, "editor": 2, "admin": 3}
USER: contextvars.ContextVar = contextvars.ContextVar("aos_user", default=None)   # {"id","username","role","ws","auth"}
PUBLIC_PREFIX = ("/static/", "/d/", "/ingest/", "/otel/v1/", "/share/", "/v1/", "/login", "/api/auth/login", "/api/auth/2fa/verify", "/favicon.ico",
                 "/channels/zalo/", "/internal/zalo-personal/", "/oauth/client-metadata.json", "/invite/", "/api/invites/")   # GĐ11.26: CIMD client-id metadata document (public by design, no workspace data); GĐ8.3: Zalo OA webhook (X-ZEvent-Signature); GĐ8.3b: sidecar callbacks (loopback + shared token, checked in channel_zalo_personal)
# Mock OAuth (AOS_MOCK_OAUTH=1): server-to-server endpoints only from the app itself (loopback, not proxied); /authorize and /_debug need a session.
MOCK_INTERNAL = ("/mock-oauth/token", "/mock-oauth/revoke", "/mock-oauth/userinfo", "/mock-oauth/api/")
def _loopback_direct(request):
    h = request.headers
    return (request.client and request.client.host in ("127.0.0.1", "::1")) and not h.get("x-forwarded-for") and not h.get("cf-connecting-ip") and not h.get("x-real-ip")
ADMIN_ONLY = ("/api/admin", "/api/gateway/key", "/api/gateway/caps", "/api/gateway/pricing", "/api/gw/keys", "/api/gw/providers", "/api/gw/fallbacks", "/api/gw/model-access", "/api/gw/limits", "/api/obs/redaction", "/api/mcp/servers", "/api/secrets", "/api/exports/parquet", "/api/exports/otlp")
VIEWER_OK_POST = ("/api/auth/lang", "/api/auth/logout", "/api/auth/password", "/api/auth/workspace", "/api/auth/keys", "/api/auth/2fa", "/api/auth/sessions", "/api/chat/me/instructions", "/mcp/aos", "/mcp/agents/", "/a2a/")   # GĐ8.8: /mcp/agents/{id} checks the share itself
# ---- Demo accounts (21/10/2026): users.demo = a customer trial login. Always treated as 'viewer' (whatever the membership says)
# plus exactly the chat writes below; everything else that changes state is 403. Spend: users.daily_cap_usd (ICT day) checked
# before every run (server._create_run); tools that write (CRM/ads/publish/code) are refused inside the run (mocks.AuditHooks).
DEMO_OK = [re.compile(x) for x in (
    r"POST /api/v1/threads$", r"(PATCH|DELETE) /api/v1/threads/[^/]+$",
    r"POST /api/v1/threads/[^/]+/runs(/stream|/wait)?$", r"POST /api/v1/threads/[^/]+/runs/[^/]+/cancel$",
    r"POST /api/v1/(assistants|threads)/search$", r"POST /api/chat/threads/search$", r"POST /api/chat/delegate$",
    r"POST /api/files$", r"POST /api/obs/feedback$",
    r"POST /api/auth/(lang|logout)$", r"POST /mcp/aos/?$")]   # GĐ7.0: /mcp/aos read tools (write tools refuse demo inside aos_tools)
# 8.P: a (non-demo) viewer may chat: threads + runs + search only. server._create_run then allows the run ONLY for agents shared to the viewer
# with role use|edit (agent_shares); everything else that writes stays editor+.
VIEWER_CHAT = [re.compile(x) for x in (
    r"POST /api/v1/threads$", r"(PATCH|DELETE) /api/v1/threads/[^/]+$",
    r"POST /api/v1/threads/[^/]+/runs(/stream|/wait)?$", r"POST /api/v1/threads/[^/]+/runs/[^/]+/cancel$",
    r"POST /api/v1/(assistants|threads)/search$", r"POST /api/chat/threads/search$")]
def viewer_chat(method, path): return any(rx.match(f"{method} {path}") for rx in VIEWER_CHAT)
DEMO_TOOLS_DENY = {"crm_log_deal", "crm_update_deal", "crm_schedule_followup", "ads_create_campaign", "content_publish", "chay_code_python"}
# GĐ6.3 fix: POST chỉ đọc (tìm kiếm/đếm) — key scope=read vẫn được gọi
READ_OK_POST = [re.compile(x) for x in (
    r"/api/v1/(assistants|threads)/(search|count)$", r"/api/v1/runs/(crons/)?search$", r"/api/v1/store/(items/search|namespaces)$", r"/api/chat/threads/search$", r"/mcp/aos/?$")]   # /mcp/aos: write tools check scope themselves
def read_post_ok(path): return any(rx.match(path) for rx in READ_OK_POST)
def is_demo(u=None):
    u = u if u is not None else USER.get()
    return bool(u and u.get("demo"))
def demo_allowed(method, path): return any(rx.match(f"{method} {path}") for rx in DEMO_OK)

def allow_viewer_post(*prefixes: str):
    """Extension point (G9): a module that enforces its own permission (e.g. voice.use) lets viewers reach its POST endpoints."""
    global VIEWER_OK_POST
    VIEWER_OK_POST = tuple(dict.fromkeys(VIEWER_OK_POST + tuple(p for p in prefixes if p.startswith("/api/"))))
COOKIE = "aos_s"
SBX_ROOT = Path(os.environ.get("AGENTOS_SBX_DIR", "/opt/agentos/sandboxes"))
SBX_QUOTA = 50 * 1024 * 1024; SBX_FILE_MAX = 5 * 1024 * 1024
_FAILS: dict = {}

def _h(x): return hashlib.sha256(x.encode()).hexdigest()

def hash_pw(pw: str) -> str:
    salt = secrets.token_bytes(16)
    d = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt:16384:8:1:" + base64.b64encode(salt).decode() + ":" + base64.b64encode(d).decode()   # ':' để không bị nội suy '$' trong .env

def check_pw(pw: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, d = stored.split(":")
        x = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(x, base64.b64decode(d))
    except Exception:  # noqa: BLE001
        return False

async def init():
    await A.q(DDL)
    SBX_ROOT.mkdir(parents=True, exist_ok=True); os.chmod(SBX_ROOT, 0o711)

async def bootstrap_owner():
    """Chủ sở hữu luôn giữ quyền: tạo 'moza' (superadmin) từ AGENTOS_OWNER_PW_HASH nếu chưa có; luôn đảm bảo admin ở workspace 1."""
    h = os.environ.get("AGENTOS_OWNER_PW_HASH")
    u = await A.q("SELECT id FROM users WHERE username='moza'", one=True)
    if not u and h:
        u = await A.q("INSERT INTO users(username,pw_hash,display_name,is_superadmin) VALUES('moza',%s,'Chủ sở hữu',true) RETURNING id", (h,), one=True)
    if u:
        await A.q("UPDATE users SET is_superadmin=true, disabled=false WHERE id=%s", (u["id"],))
        await A.q("INSERT INTO memberships(user_id,workspace_id,role) VALUES(%s,1,'admin') ON CONFLICT(user_id,workspace_id) DO UPDATE SET role='admin'", (u["id"],))

WS_CTX: contextvars.ContextVar = contextvars.ContextVar("aos_ws_ctx", default=None)   # workspace cho tác vụ nền (cron/schedule/queue)

def rls_ctx():
    """(ws, uid, role) đưa vào Postgres (set_config) cho Row-Level Security. '*' = tác vụ hệ thống (không lọc)."""
    u = USER.get()
    if u: return (str(u.get("ws") or 1), str(u.get("id") or 0), u.get("role") or "viewer")
    w = WS_CTX.get()
    if w: return (str(w), "0", "admin")
    return ("*", "0", "admin")

@contextlib.asynccontextmanager
async def as_workspace(ws_id):
    """GĐ10.1: run the DB work of a key/token-authenticated public route inside the workspace that owns the key (WS_CTX -> role aos_rls + that workspace),
    then restore the previous context. Public paths carry no USER, so without this every write is a system write that defaults to workspace 1.
    A signed-in USER already carries its workspace: no-op. ws_id missing = bug in the caller -> refuse (never fall back to the system context)."""
    if USER.get(): yield; return
    if ws_id is None or isinstance(ws_id, bool): raise ValueError("as_workspace: workspace id missing")
    prev = WS_CTX.get(); tok = WS_CTX.set(int(ws_id))
    try: yield
    finally:
        try: WS_CTX.reset(tok)
        except ValueError: WS_CTX.set(prev)   # exited from another asyncio context (streaming body): put the previous value back

RLS_FN = """
CREATE OR REPLACE FUNCTION aos_ws() RETURNS int LANGUAGE sql STABLE AS $f$ SELECT CASE WHEN current_setting('app.ws', true) ~ '^[0-9]+$' THEN current_setting('app.ws', true)::int END $f$;
CREATE OR REPLACE FUNCTION aos_uid() RETURNS int LANGUAGE sql STABLE AS $f$ SELECT CASE WHEN current_setting('app.uid', true) ~ '^[0-9]+$' THEN nullif(current_setting('app.uid', true)::int, 0) END $f$;
CREATE OR REPLACE FUNCTION aos_thread_exists(t text) RETURNS bool LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public AS $f$ BEGIN RETURN EXISTS (SELECT 1 FROM lg_threads WHERE thread_id = t); END $f$;   -- bypasses lg_threads RLS (plpgsql: lg_threads may not exist yet)
CREATE OR REPLACE FUNCTION aos_priv() RETURNS bool LANGUAGE sql STABLE AS $f$ SELECT aos_ws() IS NULL OR current_setting('app.role', true) = 'admin' $f$;
"""
# bảng gốc mang workspace_id; bảng con được lọc qua bảng cha (RLS lồng nhau tự áp dụng)
RLS_DIRECT = ["agents", "projects", "datasets", "prompts", "evaluators", "annotation_queues", "kb_docs", "test_sets", "hub_webhooks",
              "lg_assistants", "lg_threads", "lg_runs", "lg_crons", "sandboxes", "workspace_sandbox_quotas", "sandbox_egress_log", "sandbox_usage_hourly", "chat_files", "ws_secrets", "insight_reports", "issues",
              "channel_bindings", "channel_threads", "channel_events", "chat_projects", "agent_shares", "zp_sends", "parquet_export_cfg", "pii_settings", "privacy_requests",
              "context_packs", "context_blobs", "context_commits", "context_tags", "context_tag_events", "crypto_deks", "crypto_ws", "otel_export_cfg", "queue_item_reviews",   # GĐ8.1, GĐ8.3, GĐ8.9, GĐ8.8, GĐ8.3b, GĐ9.1
              "gw_vkeys", "gw_providers", "thread_reads", "mcp_servers", "tool_settings", "connections", "oauth_connections", "oauth_providers", "model_tiers", "model_feature_access", "gw_fallback_chains", "gw_model_access", "agent_delete_exports", "mail_cursors", "mail_seen", "notify_destinations", "notify_subscriptions", "channel_prompts", "ws_invites", "insight_configs", "memory_proposals", "retention_settings", "engine_project_settings", "engine_runs"]   # GĐ10.28 (+ GĐ11.60 mail triggers): workspace_id NOT NULL; /api/gw/keys list/usage/patch/delete had no workspace filter of their own
RLS_PRIVATE = {"lg_threads", "lg_runs", "sandboxes", "chat_files", "chat_projects"}   # riêng tư theo người dùng (admin workspace thấy tất cả)
RLS_CHILD = {  # bảng: điều kiện
    "agent_versions": "agent_id IN (SELECT id FROM agents)", "agent_crons": "agent_id IN (SELECT id FROM agents)",
    "agent_hooks": "agent_id IN (SELECT id FROM agents)", "hook_calls": "hook_id IN (SELECT id FROM agent_hooks)",   # GĐ10.1: webhook call log follows its hook's workspace
    "approvals": "agent_id IN (SELECT id FROM agents) AND (NOT aos_thread_exists(thread_id) OR thread_id IN (SELECT thread_id FROM lg_threads))",   # GĐ7.Z: an approval of a private chat thread is private too
    "simulations": "agent_id IN (SELECT id FROM agents)", "deployments": "agent_id IN (SELECT id FROM agents)",
    # GĐ10.28b: eval_runs / experiments carry workspace_id (migration 0006, filled by a trigger); a row with agent_id / dataset_id NULL used to be visible to every workspace
    "eval_runs": "workspace_id = aos_ws() AND (agent_id IS NULL OR agent_id IN (SELECT id FROM agents))",
    "deployment_history": "deployment_id IN (SELECT id FROM deployments)",
    "tr_runs": "project_id IN (SELECT id FROM projects) AND (user_id IS NULL OR aos_priv() OR user_id = aos_uid())",   # GĐ6.10: chat traces are private to their user (SDK rows: user_id NULL = project-visible)
    "alert_rules": "project_id IN (SELECT id FROM projects)", "insight_clusters": "report_id IN (SELECT id FROM insight_reports)",   # GD7.3
    "tool_idem": "run_id IN (SELECT run_id FROM lg_runs)",   # GĐ13.30: duplicate-send guard rows follow their run (lg_runs: private to the user, admin sees the workspace)
    "issue_traces": "issue_id IN (SELECT id FROM issues)", "issue_events": "issue_id IN (SELECT id FROM issues)",   # GĐ13.12.30
    "automation_rules": "project_id IN (SELECT id FROM projects)", "saved_filters": "project_id IN (SELECT id FROM projects)",
    "automation_applied": "rule_id IN (SELECT id FROM automation_rules)", "automation_log": "rule_id IN (SELECT id FROM automation_rules)",   # GĐ12.12 rule bookkeeping follows its rule's project
    "tr_stats_hourly": "project_id IN (SELECT id FROM projects) AND (NOT is_private OR aos_priv())",   # GĐ12.9 hourly rollup: private chat numbers only for a workspace admin
    "trace_retention": "project_id IN (SELECT id FROM projects)",   # GĐ12.18 kept (extended) traces follow their project's workspace
    "ingest_keys": "project_id IN (SELECT id FROM projects)",
    "chat_project_files": "project_id IN (SELECT id FROM chat_projects)",   # GĐ8.9
    "dataset_examples": "dataset_id IN (SELECT id FROM datasets)", "dataset_tags": "dataset_id IN (SELECT id FROM datasets)",
    "eval_schedules": "dataset_id IN (SELECT id FROM datasets)", "experiments": "workspace_id = aos_ws() AND (dataset_id IS NULL OR dataset_id IN (SELECT id FROM datasets))",
    "exp_results": "experiment_id IN (SELECT id FROM experiments)", "pairwise_runs": "exp_a IN (SELECT id FROM experiments)",
    "pairwise_results": "pairwise_id IN (SELECT id FROM pairwise_runs)",
    "example_revisions": "example_id IN (SELECT id FROM dataset_examples)", "example_attachments": "example_id IN (SELECT id FROM dataset_examples)",
    "prompt_versions": "prompt_id IN (SELECT id FROM prompts)", "prompt_tags": "prompt_id IN (SELECT id FROM prompts)",
    "queue_items": "queue_id IN (SELECT id FROM annotation_queues)", "evaluator_versions": "evaluator_id IN (SELECT id FROM evaluators)", "kb_chunks": "doc_id IN (SELECT id FROM kb_docs)",
    "test_cases": "set_id IN (SELECT id FROM test_sets)", "test_runs": "set_id IN (SELECT id FROM test_sets)",
    "eval_scores": "run_id IN (SELECT id FROM eval_runs)", "lg_run_events": "run_id IN (SELECT run_id FROM lg_runs)",
    "lg_assistant_versions": "assistant_id IN (SELECT assistant_id FROM lg_assistants)",
    "shares": "EXISTS (SELECT 1 FROM tr_runs r WHERE r.trace_id = shares.trace_id)",
    "run_attachments": "EXISTS (SELECT 1 FROM tr_runs r WHERE r.trace_id = run_attachments.trace_id)",
    # GĐ10.28: a row with a trace follows the trace; a row WITHOUT a trace follows feedback.project_id (migration 0002). Both NULL = nobody but the system session.
    "feedback": "(experiment_id IS NOT NULL AND experiment_id IN (SELECT id FROM experiments)) OR (experiment_id IS NULL AND "
                "((trace_id IS NOT NULL AND EXISTS (SELECT 1 FROM tr_runs r WHERE r.trace_id = feedback.trace_id)) OR (trace_id IS NULL AND project_id IN (SELECT id FROM projects))))",
    "sandbox_commands": "workspace_id = aos_ws() AND sandbox_id IN (SELECT id FROM sandboxes)",   # GĐ11.40: a command log follows its box (sandboxes is RLS_PRIVATE: owner / admin only)
    "gw_cache": "vkey_id IN (SELECT id FROM gw_vkeys)",   # GĐ10.28: cached answers belong to the key's workspace (a row without a key: system only)
}
# GĐ10.28 inventory (information_schema + pg_class.relrowsecurity, 26/10/2026). Tables with an INTENTIONAL workspace_id NULL row that are under RLS = this list.
# Not listed, on purpose: gateway_usage (NULL = rows written before 10.1 / with no workspace). NO RLS (10.28b decision): the provider-wide spend caps (gateway.py, deepagent_api.seed_spend)
# must sum ALL workspaces, and RLS would make a workspace session under-count them. Instead every user-facing reader filters by workspace_id itself (GET /api/gateway, aos_tools, gw_api, privacy, parquet).
# saved_views + contexts (no RLS; every query uses `workspace_id IS NOT DISTINCT FROM aos_ws()`, so a NULL row belongs to the system only, not to everybody).
GLOBAL_ROW_TABLES = ["spend_caps"]   # GĐ6.1 workspace_id NULL = global cap (set by the superadmin / .env)
RLS_UNIQUE = ["prompts", "projects", "datasets", "evaluators", "annotation_queues"]

async def rls_init():
    """Cô lập workspace bằng Postgres Row-Level Security. Truy vấn trong ngữ cảnh người dùng chạy dưới role aos_rls (NOSUPERUSER, vì superuser bỏ qua RLS);
    tác vụ hệ thống ('*') giữ role gốc để chạy DDL/worker."""
    await A.q(RLS_FN)
    if not await A.q("SELECT 1 FROM pg_roles WHERE rolname='aos_rls'", one=True):
        await A.q("CREATE ROLE aos_rls NOLOGIN NOSUPERUSER NOBYPASSRLS")
    me = (await A.q("SELECT current_user u", one=True))["u"]
    if not re.fullmatch(r"\w+", me): raise RuntimeError("tên role DB bất thường")
    await A.q(f'GRANT aos_rls TO "{me}"')
    await rls_grants()
    have = {r["table_name"] for r in await A.q("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}
    for t in RLS_DIRECT:
        if t not in have: continue
        await A.q(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS workspace_id INT")
        await A.q(f"UPDATE {t} SET workspace_id=1 WHERE workspace_id IS NULL")
        await A.q(f"ALTER TABLE {t} ALTER COLUMN workspace_id SET DEFAULT coalesce(aos_ws(), 1), ALTER COLUMN workspace_id SET NOT NULL")
        cond = "aos_ws() IS NULL OR workspace_id = aos_ws()"
        if t in RLS_PRIVATE:
            await A.q(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS owner_id INT")
            await A.q(f"ALTER TABLE {t} ALTER COLUMN owner_id SET DEFAULT aos_uid()")
            cond = "aos_ws() IS NULL OR (workspace_id = aos_ws() AND (aos_priv() OR owner_id IS NULL OR owner_id = aos_uid()))"
        await _policy(t, cond)
    # đồng bộ ws cho dữ liệu cũ của bảng con thuộc server (đã có cột)
    for t in RLS_UNIQUE:
        if t not in have: continue
        if await A.q("SELECT 1 FROM pg_constraint WHERE conname=%s", (f"{t}_name_key",), one=True):
            await A.q(f"ALTER TABLE {t} DROP CONSTRAINT {t}_name_key")
        await A.q(f"CREATE UNIQUE INDEX IF NOT EXISTS {t}_ws_name_key ON {t}(workspace_id, name)")
    if "tr_runs" in have:   # GĐ6.10: owner of a chat trace (nullable). First boot: back-fill from the chat thread's owner.
        fresh = not await A.q("SELECT 1 FROM information_schema.columns WHERE table_name='tr_runs' AND column_name='user_id'", one=True)
        await A.q("ALTER TABLE tr_runs ADD COLUMN IF NOT EXISTS user_id INT")
        if fresh and "lg_threads" in have:
            await A.q("UPDATE tr_runs r SET user_id=t.owner_id FROM lg_threads t WHERE r.thread_id=t.thread_id::text AND t.owner_id IS NOT NULL AND r.user_id IS NULL")
    for t, c in RLS_CHILD.items():
        if t in have: await _policy(t, f"aos_ws() IS NULL OR ({c})")
    for t in GLOBAL_ROW_TABLES:   # GĐ10.28 (F23): workspace_id NULL = "whole machine" row: a workspace session may read it, only the system session may write it
        if t in have: await _policy_global(t)

async def rls_grants():
    await A.q("GRANT USAGE ON SCHEMA public TO aos_rls")
    await A.q("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO aos_rls")
    await A.q("GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO aos_rls")
    await A.q("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO aos_rls")
    await A.q("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO aos_rls")

_GLOBAL_POLICIES = ("aos_ws_sel", "aos_ws_ins", "aos_ws_upd", "aos_ws_del")

async def _policy(t, cond):
    await A.q(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
    await A.q(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
    await A.q(f"DROP POLICY IF EXISTS aos_ws ON {t}")
    await A.q(f"CREATE POLICY aos_ws ON {t} USING ({cond}) WITH CHECK ({cond})")

async def _policy_global(t):
    """GĐ10.28: table with intentional workspace_id NULL rows. SELECT: system | NULL row | own row. INSERT/UPDATE/DELETE: system | own row only
    (a workspace session can neither create, change nor delete a NULL row, nor move a row into another workspace). Idempotent (runs at every boot)."""
    rd, wr = "aos_ws() IS NULL OR workspace_id IS NULL OR workspace_id = aos_ws()", "aos_ws() IS NULL OR workspace_id = aos_ws()"
    await A.q(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
    await A.q(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
    await A.q(f"DROP POLICY IF EXISTS aos_ws ON {t}")   # the old single policy (ALL) would still allow the write
    for n in _GLOBAL_POLICIES: await A.q(f"DROP POLICY IF EXISTS {n} ON {t}")
    await A.q(f"CREATE POLICY aos_ws_sel ON {t} FOR SELECT USING ({rd})")
    await A.q(f"CREATE POLICY aos_ws_ins ON {t} FOR INSERT WITH CHECK ({wr})")
    await A.q(f"CREATE POLICY aos_ws_upd ON {t} FOR UPDATE USING ({wr}) WITH CHECK ({wr})")
    await A.q(f"CREATE POLICY aos_ws_del ON {t} FOR DELETE USING ({wr})")

EXT_AUTHN = None    # GĐ9.Q hooks, set by gd9q.mount: async (request) -> user dict | Response | None
EXT_AUTHZ = None    # async (request, user) -> Response | None

def cur(): return USER.get()
def cur_ws(): return (USER.get() or {}).get("ws") or 1

async def _role(uid, ws):
    u = await A.q("SELECT is_superadmin FROM users WHERE id=%s", (uid,), one=True)
    m = await A.q("SELECT role FROM memberships WHERE user_id=%s AND workspace_id=%s", (uid, ws), one=True)
    if m: return m["role"]
    return "admin" if u and u["is_superadmin"] else None

async def _default_ws(uid):
    m = await A.q("SELECT workspace_id FROM memberships WHERE user_id=%s ORDER BY workspace_id LIMIT 1", (uid,), one=True)
    return m["workspace_id"] if m else 1

async def _rate_ok(key):
    """Chống dò mật khẩu bằng backoff luỹ thừa (không CAPTCHA, không khoá cứng):
    - theo (IP, tài khoản): sau 5 lần sai liên tiếp → chờ 2^(n-5) s, tối đa 300 s
    - theo tài khoản (mọi IP, chống xoay IP): sau 10 lần sai → chờ 2^(n-10) s, tối đa 60 s (chủ sở hữu không bị khoá dài)
    Đếm lần sai sau lần đăng nhập đúng gần nhất, trong 1 giờ. Trả về số giây phải chờ (0 = được thử)."""
    ip, _, user = key.partition("|")
    r = await A.q("""WITH ok AS (SELECT coalesce(max(ts), now()-interval '1 hour') t FROM audit_log WHERE username=%s AND detail='đăng nhập' AND ts > now()-interval '1 hour')
        SELECT count(*) FILTER (WHERE ip=%s) n_ip, count(*) n_user, max(ts) FILTER (WHERE ip=%s) last_ip, max(ts) last_user,
               extract(epoch FROM now())::float8 now_s FROM audit_log, ok
        WHERE detail='đăng nhập sai' AND username=%s AND ts > ok.t""", (user, ip, ip, user), one=True)
    wait = 0.0
    if r["n_ip"] >= 5: wait = max(wait, min(2 ** (r["n_ip"] - 5), 300) - (r["now_s"] - r["last_ip"].timestamp()))
    if r["n_user"] >= 10: wait = max(wait, min(2 ** (r["n_user"] - 10), 60) - (r["now_s"] - r["last_user"].timestamp()))
    return int(wait) + 1 if wait > 0 else 0

def _ip(req: Request): return req.headers.get("cf-connecting-ip") or req.headers.get("x-forwarded-for", "").split(",")[0].strip() or (req.client.host if req.client else "")

async def authenticate(req: Request):
    """Thứ tự: cookie phiên → X-Api-Key / Bearer aos_ → HTTP Basic (tương thích Caddy cũ & script)."""
    ws_hdr = req.headers.get("x-workspace-id")
    tok = req.cookies.get(COOKIE)
    if tok:
        s = await A.q("SELECT s.*, u.username, u.disabled, u.demo, u.daily_cap_usd, u.demo_agents, u.totp_enabled FROM sessions s JOIN users u ON u.id=s.user_id WHERE token_hash=%s AND expires > now()", (_h(tok),), one=True)
        if s and not s["disabled"]:
            ws = int(ws_hdr) if ws_hdr and ws_hdr.isdigit() else (s["workspace_id"] or await _default_ws(s["user_id"]))
            role = await _role(s["user_id"], ws)
            if role:
                if (datetime.now(timezone.utc) - s["last_seen"]).total_seconds() > 60:   # GĐ6.7: lần cuối hoạt động, cập nhật tối đa 1 lần/phút
                    await A.q("UPDATE sessions SET last_seen=now() WHERE token_hash=%s", (s["token_hash"],))
                ident = {"id": s["user_id"], "username": s["username"], "role": role, "ws": ws, "auth": "session", "totp": bool(s["totp_enabled"])}
                if not s["totp_enabled"] and not s["demo"] and await _ws_requires_2fa(ws): ident["must_enroll_2fa"] = True   # phiên giới hạn tới khi bật 2 lớp
                return _with_demo(ident, s)
    key = req.headers.get("x-api-key") or ""
    authz = req.headers.get("authorization") or ""
    if not key and authz.lower().startswith("bearer aos_"): key = authz[7:].strip()
    if key.startswith("aos_"):
        k = await A.q("SELECT k.*, u.username, u.disabled, u.demo, u.daily_cap_usd, u.demo_agents FROM user_api_keys k JOIN users u ON u.id=k.user_id WHERE key_hash=%s AND NOT revoked AND (expires IS NULL OR expires > now())",
                      (_h(key),), one=True)
        if k and not k["disabled"]:
            role = await _role(k["user_id"], k["workspace_id"])
            if role and k["role"] and ROLES[k["role"]] < ROLES[role]: role = k["role"]      # key có thể bị hạ quyền
            if role:
                await A.q("UPDATE user_api_keys SET last_used=now() WHERE id=%s", (k["id"],))
                return _with_demo({"id": k["user_id"], "username": k["username"], "role": role, "ws": k["workspace_id"], "auth": f"api_key:{k['prefix']}", "scope": k.get("scope") or "full"}, k)
    if authz.lower().startswith("basic "):
        try: user, _, pw = base64.b64decode(authz[6:]).decode().partition(":")
        except Exception: return None  # noqa: BLE001
        rk = f"{_ip(req)}|{user}"
        w = await _rate_ok(rk)
        if w: return {"error": 429, "wait": w}
        u = await A.q("SELECT * FROM users WHERE username=%s AND NOT disabled", (user,), one=True)
        if u and u["totp_enabled"] and check_pw(pw, u["pw_hash"]): return None   # GĐ6.7: đã bật 2 lớp thì không đăng nhập bằng mật khẩu qua Basic (dùng API key)
        if u and check_pw(pw, u["pw_hash"]):
            ws = int(ws_hdr) if ws_hdr and ws_hdr.isdigit() else await _default_ws(u["id"])
            role = await _role(u["id"], ws)
            if role: return _with_demo({"id": u["id"], "username": u["username"], "role": role, "ws": ws, "auth": "basic"}, u)
        await _audit({"username": user}, req, 401, 0, "đăng nhập sai")
    return None

async def _ws_requires_2fa(ws):
    r = await A.q("SELECT require_2fa FROM workspaces WHERE id=%s", (ws,), one=True)
    return bool(r and r["require_2fa"])

def _with_demo(d, row):
    """Demo users: role forced to viewer; cap/agent allow-list carried in the request identity."""
    if row.get("demo"):
        d.update(role="viewer", demo=True, daily_cap_usd=float(row["daily_cap_usd"]) if row.get("daily_cap_usd") is not None else None,
                 demo_agents=list(row.get("demo_agents") or []) or None)
    return d

def allowed(role, method, path, demo=False, has_share=False):
    if demo:
        if method in ("GET", "HEAD", "OPTIONS"): return not any(path.startswith(p) for p in ADMIN_ONLY)
        return demo_allowed(method, path)
    if any(path.startswith(p) for p in ADMIN_ONLY):
        return role == "admin" or (method in ("GET", "HEAD") and path.startswith(("/api/obs/redaction", "/api/mcp/servers", "/api/secrets")) and ROLES[role] >= 2)
    if method in ("GET", "HEAD", "OPTIONS"): return True
    if any(path.startswith(p) for p in VIEWER_OK_POST): return True
    if role == "viewer" and has_share and viewer_chat(method, path): return True   # 8.P: only viewers with an agent share (8.Z: plain viewers stay read-only); runs limited to shared agents in server._create_run
    return ROLES[role] >= ROLES["editor"]

async def middleware(request: Request, call_next):
    path = request.url.path
    if any(path.startswith(p) for p in PUBLIC_PREFIX):
        return await call_next(request)
    if path.startswith(MOCK_INTERNAL):
        if _loopback_direct(request): return await call_next(request)
        return JSONResponse({"detail": "Not found"}, 404)
    t0 = time.perf_counter()
    u = await authenticate(request)
    if u and u.get("error") == 429: return JSONResponse({"detail": f"Too many failed sign-in attempts, retry in {u['wait']} s"}, 429, headers={"Retry-After": str(u["wait"])})
    if not u and EXT_AUTHN:   # GĐ9.Q: OIDC bearer token / custom @auth.authenticate (lg_auth) for the Agent Server API paths
        u = await EXT_AUTHN(request)
        if isinstance(u, Response): return u
    if not u:
        if request.method == "GET" and "text/html" in (request.headers.get("accept") or "") and not path.startswith("/api/"):
            return RedirectResponse("/login?next=" + path, 303)
        return JSONResponse({"detail": "Not authenticated"}, 401)
    if u.get("must_enroll_2fa"):   # GĐ6.7: workspace bắt buộc 2 lớp mà người này chưa bật -> chỉ các đường dẫn đăng ký 2 lớp
        import twofa
        if not twofa.restricted_ok(path):
            if request.method == "GET" and "text/html" in (request.headers.get("accept") or "") and not path.startswith("/api/") and path != "/settings":
                return RedirectResponse("/settings?s=security", 303)
            if not (request.method == "GET" and not path.startswith("/api/")):
                return JSONResponse({"detail": "Workspace yêu cầu bật xác thực 2 lớp. Vào Cài đặt > Bảo mật để bật trước khi tiếp tục", "must_enroll_2fa": True}, 403)
    _share = False
    if u["role"] == "viewer" and not u.get("demo") and viewer_chat(request.method, path):   # 8.Z: only viewers that some agent is shared with may chat
        _share = bool(await A.q("SELECT 1 FROM agent_shares WHERE workspace_id=%s AND role IN ('use','edit') AND ((principal_kind='user' AND principal_id=%s)"
                                " OR (principal_kind='group' AND principal_id IN (SELECT group_id FROM user_group_members WHERE user_id=%s))) LIMIT 1",   # GĐ9.P1: group shares count
                                (u.get("ws") or 1, u["id"], u["id"]), one=True))
    if not allowed(u["role"], request.method, path, u.get("demo", False), _share):
        await _audit(u, request, 403, 0, "bị chặn theo vai trò")
        if u.get("demo"): return JSONResponse({"detail": "Tài khoản demo chỉ được chat và xem, không được thay đổi cấu hình (demo account: read-only)"}, 403)
        return JSONResponse({"detail": f"Role '{u['role']}' is not allowed to {request.method} {path}"}, 403)
    if u.get("scope") == "read" and request.method not in ("GET", "HEAD", "OPTIONS") and not (request.method == "POST" and read_post_ok(path)):   # GĐ6.3: key chỉ đọc không ghi được ở đâu cả
        await _audit(u, request, 403, 0, "key chỉ đọc")
        return JSONResponse({"detail": "Key chỉ đọc"}, 403)
    if EXT_AUTHZ:   # GĐ9.Q: agent service keys (identity) + @auth.on handlers / declarative policy; a response = refused
        r = await EXT_AUTHZ(request, u)
        if r is not None:
            await _audit(u, request, r.status_code, 0, "bị chặn bởi chính sách xác thực")
            return r
    tok = USER.set(u)
    try:
        resp = await call_next(request)
    finally:
        USER.reset(tok)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        await _audit(u, request, resp.status_code, int((time.perf_counter() - t0) * 1000))
    return resp

async def _audit(u, req, status, ms, detail=None):
    try:
        await A.q("INSERT INTO audit_log(user_id,username,workspace_id,auth,method,path,status,ip,ms,detail) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                  (u and u.get("id"), u and u.get("username"), u and u.get("ws"), u and u.get("auth"), req.method, req.url.path[:300], status, _ip(req), ms, detail))
    except Exception as e:  # noqa: BLE001
        print("audit error", e)

# ------------------------------------------------------------ đăng nhập
LOGIN_HTML = """<!doctype html><html lang=vi><head><meta charset=utf-8><meta name=viewport content="width=device-width"><title>Đăng nhập — Moza Agent OS</title>
<style>body{font-family:system-ui;background:#f3f4f6;display:grid;place-items:center;height:100vh;margin:0}form{background:#fff;padding:28px;border-radius:12px;
box-shadow:0 2px 12px #0001;width:320px}input{width:100%;padding:9px;margin:6px 0 12px;border:1px solid #d1d5db;border-radius:6px;box-sizing:border-box}
button{width:100%;padding:10px;background:#2563eb;color:#fff;border:0;border-radius:6px;font-weight:600}#e{color:#b91c1c;font-size:13px;min-height:18px}</style></head>
<body><form id=f><h2 style="margin-top:0">Moza Agent OS</h2><div id=s1><label>Tên đăng nhập<input id=u autocomplete=username autocapitalize=none autocorrect=off spellcheck=false required></label>
<label>Mật khẩu<input id=p type=password autocomplete=current-password required></label></div>
<div id=s2 hidden><p style="margin-top:0;font-size:14px">Nhập mã 6 số từ ứng dụng xác thực trên điện thoại.</p>
<label id=l1>Mã xác thực<input id=c inputmode=numeric autocomplete=one-time-code maxlength=7></label>
<label id=l2 hidden>Mã dự phòng<input id=rc autocomplete=off maxlength=12 placeholder="xxxxx-xxxxx"></label>
<label style="display:flex;gap:6px;align-items:center;font-size:13px"><input id=rm type=checkbox style="width:auto;margin:0">Nhớ thiết bị này 30 ngày</label>
<a href="#" id=sw style="font-size:13px;display:block;margin:10px 0">Dùng mã dự phòng</a></div>
<div id=e></div><button>Đăng nhập</button></form>
<script>let CH=null,REC=false;const nx=()=>{const n=new URLSearchParams(location.search).get('next')||'/';return n.startsWith('/')&&!n.startsWith('//')?n:'/'};
sw.onclick=ev=>{ev.preventDefault();REC=!REC;l1.hidden=REC;l2.hidden=!REC;sw.textContent=REC?'Dùng mã từ ứng dụng':'Dùng mã dự phòng';(REC?rc:c).focus()};
f.onsubmit=async ev=>{ev.preventDefault();e.textContent='';const post=(url,b)=>fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
let r=CH?await post('api/auth/2fa/verify',{challenge:CH,[REC?'recovery_code':'code']:(REC?rc:c).value.trim(),remember_device:rm.checked}):await post('api/auth/login',{username:u.value,password:p.value});
const d=await r.json().catch(()=>({}));
if(r.ok&&d.need_2fa){CH=d.challenge;s1.hidden=true;s2.hidden=false;c.focus();return}
if(r.ok){location.href=d.must_enroll_2fa?'/settings?s=security':nx()}
else{if(CH&&r.status===401&&/hết hạn|không hợp lệ/.test(d.detail||'')){CH=null;s1.hidden=false;s2.hidden=true}e.textContent=d.detail||'Lỗi'}}</script></body></html>"""

@router.get("/login", include_in_schema=False)
async def login_page(request: Request):
    import i18n; return HTMLResponse(i18n.page_for(i18n.inject(LOGIN_HTML), request))

class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)

@router.post("/api/auth/login")
async def login(b: LoginIn, request: Request):
    rk = f"{_ip(request)}|{b.username}"
    w = await _rate_ok(rk)
    if w: raise HTTPException(429, f"Too many failed sign-in attempts, retry in {w} s", headers={"Retry-After": str(w)})
    u = await A.q("SELECT * FROM users WHERE username=%s AND NOT disabled", (b.username,), one=True)
    if not u or not check_pw(b.password, u["pw_hash"]):
        await _audit({"username": b.username}, request, 401, 0, "đăng nhập sai")
        raise HTTPException(401, "Sai tên đăng nhập hoặc mật khẩu")
    if u["totp_enabled"]:   # GĐ6.7: mật khẩu đúng nhưng chưa cấp phiên; bước 2 ở /api/auth/2fa/verify (trừ khi thiết bị đã được nhớ)
        import twofa
        if not twofa.remember_ok(u, request):
            await _audit({"id": u["id"], "username": u["username"], "auth": "login"}, request, 200, 0, "đăng nhập (chờ mã 2 lớp)")
            return JSONResponse({"need_2fa": True, "challenge": twofa.make_challenge(u["id"]), "expires_in": twofa.CHALLENGE_TTL})
    return await issue_session(u, request)

async def issue_session(u, request: Request):
    """Cấp phiên 7 ngày sau khi xác thực xong (mật khẩu, và mã 2 lớp nếu đã bật)."""
    import twofa
    tok = secrets.token_urlsafe(32); ws = await _default_ws(u["id"]); ua = (request.headers.get("user-agent") or "")[:200]
    await A.q("INSERT INTO sessions(token_hash,user_id,workspace_id,expires,ip,ua,device) VALUES(%s,%s,%s,now()+interval '7 days',%s,%s,%s)",
              (_h(tok), u["id"], ws, _ip(request), ua, twofa.device_name(ua)))
    await A.q("UPDATE users SET last_login=now() WHERE id=%s", (u["id"],))
    await A.q("DELETE FROM sessions WHERE expires < now()")
    await _audit({"id": u["id"], "username": u["username"], "ws": ws, "auth": "login"}, request, 200, 0, "đăng nhập")
    r = JSONResponse({"ok": True, "username": u["username"], "must_enroll_2fa": (not u["totp_enabled"]) and not u.get("demo") and await _ws_requires_2fa(ws)})
    r.set_cookie(COOKIE, tok, max_age=7 * 86400, httponly=True, secure=True, samesite="lax", path="/")
    return r

@router.post("/api/auth/logout")
async def logout(request: Request):
    tok = request.cookies.get(COOKIE)
    if tok: await A.q("DELETE FROM sessions WHERE token_hash=%s", (_h(tok),))
    r = JSONResponse({"ok": True}); r.delete_cookie(COOKIE, path="/"); return r

@router.get("/api/auth/me")
async def me():
    u = cur()
    wss = await A.q("SELECT w.id, w.name, m.role FROM memberships m JOIN workspaces w ON w.id=m.workspace_id WHERE m.user_id=%s ORDER BY w.id", (u["id"],))
    lg = await A.q("SELECT lang FROM users WHERE id=%s", (u["id"],), one=True)
    out = {**u, "workspaces": wss, "lang": (lg or {}).get("lang") or "en"}
    if u.get("demo"): out["spent_today_usd"] = round(await demo_spent_today(u["id"]), 6)
    return out

async def demo_spent_today(uid):
    """Model spend of this user's runs since 00:00 Asia/Ho_Chi_Minh (lg_runs.output.cost_usd; delegated child runs included)."""
    r = await A.q("""SELECT coalesce(sum(nullif(output->>'cost_usd','')::numeric),0) c FROM lg_runs WHERE user_id=%s
                     AND created_at > date_trunc('day', now() AT TIME ZONE 'Asia/Ho_Chi_Minh') AT TIME ZONE 'Asia/Ho_Chi_Minh'""", (uid,), one=True)
    return float(r["c"] or 0)

async def demo_guard_run(u, assistant, b):
    """Called by server._create_run for demo users: agent allow-list, no HITL approval, no webhook/delay, one run at a time, daily cap."""
    if not is_demo(u): return
    allow = u.get("demo_agents")
    if allow and assistant.get("agent_id") not in allow and ((assistant.get("metadata") or {}).get("builtin") != "agentos-assistant"): raise HTTPException(403, "Tài khoản demo không dùng được agent này")
    resume = (b.command or {}).get("resume") if isinstance(b.command, dict) else None
    if isinstance(resume, dict) and "decisions" in resume: raise HTTPException(403, "Tài khoản demo không được duyệt thao tác (HITL)")
    if b.webhook or b.after_seconds: raise HTTPException(403, "Tài khoản demo không dùng webhook / hẹn giờ")
    if await A.q("SELECT 1 FROM lg_runs WHERE user_id=%s AND status IN ('pending','running') LIMIT 1", (u["id"],), one=True):
        raise HTTPException(429, "Tài khoản demo chỉ chạy 1 yêu cầu một lúc, đợi câu trả lời trước")
    cap = u.get("daily_cap_usd")
    if cap is not None and await demo_spent_today(u["id"]) >= cap:
        raise HTTPException(402, f"Tài khoản demo đã dùng hết hạn mức hôm nay (${cap:.2f}/ngày), mai dùng tiếp được")

class LangIn(BaseModel):
    lang: str = Field(pattern=r"^(en|vi)$")

@router.post("/api/auth/lang")
async def set_lang(b: LangIn):
    await A.q("UPDATE users SET lang=%s WHERE id=%s", (b.lang, cur()["id"]))
    r = JSONResponse({"ok": True, "lang": b.lang})
    r.set_cookie("aos_lang", b.lang, max_age=365 * 86400, secure=True, samesite="lax", path="/")
    return r

class WsSel(BaseModel):
    workspace_id: int

@router.post("/api/auth/workspace")
async def switch_ws(b: WsSel, request: Request):
    u = cur()
    if not await _role(u["id"], b.workspace_id): raise HTTPException(403, "Không thuộc workspace này")
    tok = request.cookies.get(COOKIE)
    if not tok: raise HTTPException(400, "Chỉ phiên đăng nhập mới đổi workspace (API key gắn cố định 1 workspace; Basic dùng header X-Workspace-Id)")
    await A.q("UPDATE sessions SET workspace_id=%s WHERE token_hash=%s", (b.workspace_id, _h(tok)))
    return {"ok": True}

class PwIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=10, max_length=200)

@router.post("/api/auth/password")
async def change_pw(b: PwIn):
    u = await A.q("SELECT * FROM users WHERE id=%s", (cur()["id"],), one=True)
    if not check_pw(b.old_password, u["pw_hash"]): raise HTTPException(400, "Mật khẩu cũ sai")
    await A.q("UPDATE users SET pw_hash=%s WHERE id=%s", (hash_pw(b.new_password), u["id"]))
    return {"ok": True}

# ---- API key cá nhân
class KeyIn(BaseModel):
    name: str = Field(default="key", max_length=80)
    role: str | None = Field(default=None, pattern=r"^(admin|editor|viewer)$")
    expires_days: int | None = Field(default=None, ge=1, le=3650)
    scope: str = Field(default="full", pattern=r"^(full|read)$")

@router.post("/api/auth/keys")
async def key_new(b: KeyIn):
    u = cur(); raw = "aos_" + secrets.token_hex(24)
    if b.role and ROLES[b.role] > ROLES[u["role"]]: raise HTTPException(403, "Không thể tạo key quyền cao hơn mình")
    r = await A.q("INSERT INTO user_api_keys(user_id,workspace_id,name,prefix,key_hash,role,scope,expires) VALUES(%s,%s,%s,%s,%s,%s,%s,"
                  "CASE WHEN %s::int IS NULL THEN NULL ELSE now() + %s::int * interval '1 day' END) RETURNING id",
                  (u["id"], u["ws"], b.name, raw[:12], _h(raw), b.role, b.scope, b.expires_days, b.expires_days), one=True)
    return {"id": r["id"], "scope": b.scope, "key": raw, "note": "Chỉ hiện một lần. Dùng header X-Api-Key."}

@router.get("/api/auth/keys")
async def key_list():
    return await A.q("SELECT id,name,prefix,role,scope,workspace_id,expires,last_used,revoked,created FROM user_api_keys WHERE user_id=%s ORDER BY id DESC", (cur()["id"],))

@router.delete("/api/auth/keys/{kid}")
async def key_revoke(kid: int):
    u = cur()
    await A.q("UPDATE user_api_keys SET revoked=true WHERE id=%s AND (user_id=%s OR %s)", (kid, u["id"], u["role"] == "admin"))
    return {"ok": True}

# ------------------------------------------------------------ quản trị (admin)
class UserIn(BaseModel):
    username: str = Field(pattern=r"^[a-zA-Z0-9_.\-]{2,64}$")
    password: str = Field(min_length=10, max_length=200)
    display_name: str | None = None
    role: str = Field(default="viewer", pattern=r"^(admin|editor|viewer)$")
    workspace_id: int | None = None
    demo: bool = False
    daily_cap_usd: float | None = Field(default=None, ge=0, le=100)
    demo_agents: list[int] | None = None

async def _admin_wids(u):
    """GĐ12.100c: the workspaces whose users `u` may manage. None = the platform owner (all). Otherwise the (non-demo) workspaces where `u` is admin;
    a personal API key only its own workspace; a demo account none."""
    if await _is_platform_owner(u["id"]): return None
    if u.get("demo") or is_demo(u): return set()
    rows = await A.q("SELECT m.workspace_id FROM memberships m JOIN workspaces w ON w.id=m.workspace_id WHERE m.user_id=%s AND m.role='admin' AND NOT coalesce(w.demo,false)", (u["id"],))
    ids = {r["workspace_id"] for r in rows} - set(A.gw.DEMO_WS)
    if str(u.get("auth", "")).startswith("api_key"): ids &= {int(u.get("ws") or 0)}
    return ids

async def _manageable_user(uid: int):
    """GĐ12.100c: may the caller reset the password / lock / edit / delete user `uid`? The platform owner: any user. A workspace admin: only a non-owner user whose
    memberships ALL lie in workspaces they administer (a user who also belongs elsewhere is only removed from the admin's own workspace), or themselves.
    Unknown id, another workspace and the owner give the same 403 (no probing). Returns the user row."""
    me = cur(); ids = await _admin_wids(me)
    t = await A.q("SELECT id, is_superadmin FROM users WHERE id=%s", (uid,), one=True)
    if ids is None:
        if not t: raise HTTPException(404)
        return t
    ms = {r["workspace_id"] for r in await A.q("SELECT workspace_id FROM memberships WHERE user_id=%s", (uid,))} if t else set()
    if not t or t["is_superadmin"] or not (uid == me["id"] or (ms and ms <= ids)):
        raise HTTPException(403, "Chỉ quản lý được người dùng chỉ thuộc workspace bạn quản trị (người thuộc workspace khác: chỉ gỡ khỏi workspace của bạn)")
    return t

@router.get("/api/admin/users")
async def users():
    """GĐ12.100c: the platform owner lists everyone; a workspace admin only the users who are members of a workspace they administer, and sees only those memberships.
    `manageable` = may reset the password / lock / delete (all memberships inside the admin's workspaces)."""
    ids = await _admin_wids(cur())
    sel = """SELECT u.id,u.username,u.display_name,u.is_superadmin,u.disabled,u.created,u.last_login,u.demo,u.totp_enabled,u.daily_cap_usd,u.demo_agents,"""
    if ids is None:
        return await A.q(sel + """(SELECT json_agg(json_build_object('workspace_id',m.workspace_id,'role',m.role)) FROM memberships m WHERE m.user_id=u.id) memberships, true manageable FROM users u ORDER BY u.id""")
    ids = list(ids)
    return await A.q(sel + """(SELECT json_agg(json_build_object('workspace_id',m.workspace_id,'role',m.role)) FROM memberships m WHERE m.user_id=u.id AND m.workspace_id = ANY(%s)) memberships,
        (NOT u.is_superadmin AND NOT EXISTS (SELECT 1 FROM memberships x WHERE x.user_id=u.id AND NOT (x.workspace_id = ANY(%s)))) manageable
        FROM users u WHERE EXISTS (SELECT 1 FROM memberships m WHERE m.user_id=u.id AND m.workspace_id = ANY(%s)) ORDER BY u.id""", (ids, ids, ids))

@router.post("/api/admin/users")
async def user_new(b: UserIn):
    if await A.q("SELECT 1 FROM users WHERE username=%s", (b.username,), one=True): raise HTTPException(409, "Tên đã tồn tại")
    if b.demo and b.role != "viewer": raise HTTPException(422, "Tài khoản demo luôn là viewer")
    target = b.workspace_id or cur_ws()
    await _ws_admin_of(target)   # GĐ12.100b: only an admin OF THE TARGET workspace (or the platform owner) may create a user there; unknown id = same 403
    if ROLES[b.role] > ROLES[cur()["role"]]: raise HTTPException(403, "Không thể cấp quyền cao hơn quyền của mình")
    r = await A.q("INSERT INTO users(username,pw_hash,display_name,demo,daily_cap_usd,demo_agents) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
                  (b.username, hash_pw(b.password), b.display_name, b.demo, b.daily_cap_usd, b.demo_agents), one=True)
    await A.q("INSERT INTO memberships(user_id,workspace_id,role) VALUES(%s,%s,%s)", (r["id"], target, b.role))
    return r

class UserUpd(BaseModel):
    disabled: bool | None = None
    password: str | None = Field(default=None, min_length=10, max_length=200)
    display_name: str | None = None
    demo: bool | None = None
    daily_cap_usd: float | None = Field(default=None, ge=0, le=100)
    demo_agents: list[int] | None = None

@router.patch("/api/admin/users/{uid}")
async def user_upd(uid: int, b: UserUpd):
    if uid == cur()["id"] and b.disabled: raise HTTPException(400, "Không tự khoá chính mình")
    await _manageable_user(uid)   # GĐ12.100c: before any change
    if b.disabled is not None:
        await A.q("UPDATE users SET disabled=%s WHERE id=%s", (b.disabled, uid))
        if b.disabled: await A.q("DELETE FROM sessions WHERE user_id=%s", (uid,))
    if b.password: await A.q("UPDATE users SET pw_hash=%s WHERE id=%s", (hash_pw(b.password), uid)); await A.q("DELETE FROM sessions WHERE user_id=%s", (uid,))
    if b.display_name is not None: await A.q("UPDATE users SET display_name=%s WHERE id=%s", (b.display_name, uid))
    if b.demo is not None:
        if b.demo and (await A.q("SELECT is_superadmin FROM users WHERE id=%s", (uid,), one=True) or {}).get("is_superadmin"): raise HTTPException(400, "Chủ sở hữu không thể là demo")
        await A.q("UPDATE users SET demo=%s WHERE id=%s", (b.demo, uid))
    if b.daily_cap_usd is not None: await A.q("UPDATE users SET daily_cap_usd=%s WHERE id=%s", (b.daily_cap_usd, uid))
    if b.demo_agents is not None: await A.q("UPDATE users SET demo_agents=%s WHERE id=%s", (b.demo_agents or None, uid))
    return {"ok": True}

@router.delete("/api/admin/users/{uid}")
async def user_del(uid: int):
    u = await _manageable_user(uid)   # GĐ12.100c: 404 unknown (owner only) / 403 not manageable
    if u["is_superadmin"] or uid == cur()["id"]: raise HTTPException(400, "Không xoá chủ sở hữu / chính mình")
    await A.q("DELETE FROM users WHERE id=%s", (uid,)); return {"ok": True}

class WsIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)

@router.get("/api/admin/workspaces")
async def ws_list():
    """GĐ12.100b: the platform owner (users.is_superadmin) sees every workspace; any other admin only the workspaces where THEY are admin (a personal API key: only its own)."""
    u = cur()
    where, args = "", ()
    if not await _is_platform_owner(u["id"]):
        where, args = "WHERE EXISTS (SELECT 1 FROM memberships mm WHERE mm.workspace_id=w.id AND mm.user_id=%s AND mm.role='admin')", (u["id"],)
        if str(u.get("auth", "")).startswith("api_key"): where, args = where + " AND w.id=%s", args + (int(u.get("ws") or 0),)
    return await A.q(f"""SELECT w.*, (SELECT count(*) FROM memberships m WHERE m.workspace_id=w.id) members,
        (SELECT count(*) FROM agents a WHERE a.workspace_id=w.id AND NOT a.deleted AND NOT a.hidden) agents, (SELECT count(*) FROM projects p WHERE p.workspace_id=w.id) projects FROM workspaces w {where} ORDER BY id""", args)

@router.post("/api/admin/workspaces")
async def ws_new(b: WsIn):
    r = await A.q("INSERT INTO workspaces(name) VALUES(%s) RETURNING id", (b.name,), one=True)
    await A.q("INSERT INTO memberships(user_id,workspace_id,role) VALUES(%s,%s,'admin') ON CONFLICT DO NOTHING", (cur()["id"], r["id"]))
    return r

class WsRename(BaseModel):
    name: str = Field(min_length=1, max_length=200)   # trimmed and re-checked to 1-80 in ws_rename (a name of only spaces is refused)

async def _is_platform_owner(uid) -> bool:
    """GĐ12.100b: the platform owner = the machine operator (users.is_superadmin, e.g. `moza`). Only the owner manages workspaces they are not a member-admin of."""
    r = await A.q("SELECT is_superadmin FROM users WHERE id=%s", (uid,), one=True)
    return bool(r and r["is_superadmin"])

async def require_platform_owner():
    """GĐ12.100d: machine-wide settings (SSO/OIDC, worker ops, server health) are the machine operator's, never a workspace admin's."""
    u = cur()
    if not u or not await _is_platform_owner(u.get("id")): raise HTTPException(403, "Chỉ chủ sở hữu máy / platform owner only")

async def _ws_admin_of(wid: int):
    """GĐ12.99: the caller must be an admin OF THAT workspace (membership role admin, or superadmin) - the /api/admin gate only proved admin in the CURRENT one.
    A personal API key is bound to one workspace: it can only act on its own. Returns the workspace row; 404 unknown, 403 not admin there / demo / workspace 1 is not touched here."""
    u = cur()
    if u.get("demo") or is_demo(u): raise HTTPException(403, "Tài khoản demo không đổi được workspace")
    if str(u.get("auth", "")).startswith("api_key") and int(u.get("ws") or 0) != wid: raise HTTPException(403, "Key chỉ dùng cho workspace của nó")
    w = await A.q("SELECT * FROM workspaces WHERE id=%s", (wid,), one=True)
    if not w or await _role(u["id"], wid) != "admin": raise HTTPException(403, "Chỉ admin của workspace này được thực hiện")   # same answer for unknown id and for another workspace: no probing of ids
    if (w.get("demo") or wid in A.gw.DEMO_WS) and not await _is_platform_owner(u["id"]): raise HTTPException(403, "Workspace demo chỉ do chủ sở hữu quản lý")   # GĐ12.100b
    return w

@router.get("/api/admin/workspaces/{wid}")
async def ws_get(wid: int):
    w = await _ws_admin_of(wid)
    n = await A.q("SELECT count(*) n FROM memberships WHERE workspace_id=%s", (wid,), one=True)
    return {"id": w["id"], "name": w["name"], "created": w["created"], "members": n["n"]}

@router.patch("/api/admin/workspaces/{wid}")
async def ws_rename(wid: int, b: WsRename):
    """GĐ12.99 rename only (no delete / handle: GĐ14). Workspace 1 and the demo workspace keep their name (403). Audit line = the audit middleware (PATCH + path + status)."""
    w = await _ws_admin_of(wid)
    if wid == 1 or w.get("demo") or wid in A.gw.DEMO_WS: raise HTTPException(403, "Workspace mặc định và workspace demo không đổi tên được")
    name = b.name.strip()
    if not 1 <= len(name) <= 80 or not name.isprintable(): raise HTTPException(422, "Tên workspace: 1-80 ký tự, không xuống dòng")
    if name == w["name"]: return {"id": wid, "name": name}
    try:
        r = await A.q("UPDATE workspaces SET name=%s WHERE id=%s AND NOT EXISTS (SELECT 1 FROM workspaces o WHERE o.name=%s AND o.id<>%s) RETURNING id, name", (name, wid, name, wid), one=True)
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ != "UniqueViolation": raise
        r = None   # lost a race with another rename to the same name
    if not r: raise HTTPException(409, "Tên workspace đã tồn tại")
    return r

class MemIn(BaseModel):
    user_id: int
    role: str = Field(pattern=r"^(admin|editor|viewer)$")

@router.post("/api/admin/workspaces/{wid}/members")
async def mem_set(wid: int, b: MemIn):
    """GĐ12.100b: caller must be admin OF `wid` (or the platform owner); the role is capped at the caller's own; only the owner may touch another owner's membership."""
    await _ws_admin_of(wid)
    t = await A.q("SELECT is_superadmin FROM users WHERE id=%s", (b.user_id,), one=True)
    ids = await _admin_wids(cur())
    if ids is not None:   # GĐ12.100c: a workspace admin may only add a user who is already here or who belongs to no workspace they do not administer (otherwise: the invite flow)
        here = await A.q("SELECT 1 FROM memberships WHERE user_id=%s AND workspace_id=%s", (b.user_id, wid), one=True)
        elsewhere = await A.q("SELECT 1 FROM memberships WHERE user_id=%s AND NOT (workspace_id = ANY(%s))", (b.user_id, list(ids)), one=True)
        if not t or (not here and elsewhere): raise HTTPException(403, "Không thêm trực tiếp được người dùng này - dùng lời mời (invite)")   # unknown id = same answer
    if not t: raise HTTPException(404, "Không có user")
    if ROLES[b.role] > ROLES[await _role(cur()["id"], wid)]: raise HTTPException(403, "Không thể cấp quyền cao hơn quyền của mình")
    if t["is_superadmin"] and not await _is_platform_owner(cur()["id"]): raise HTTPException(403, "Chỉ chủ sở hữu được đổi quyền của chủ sở hữu")
    await A.q("INSERT INTO memberships(user_id,workspace_id,role) VALUES(%s,%s,%s) ON CONFLICT(user_id,workspace_id) DO UPDATE SET role=EXCLUDED.role", (b.user_id, wid, b.role))
    return {"ok": True}

@router.delete("/api/admin/workspaces/{wid}/members/{uid}")
async def mem_del(wid: int, uid: int):
    await _ws_admin_of(wid)   # GĐ12.100b
    u = await A.q("SELECT is_superadmin FROM users WHERE id=%s", (uid,), one=True)
    if u and u["is_superadmin"] and not await _is_platform_owner(cur()["id"]): raise HTTPException(403, "Chỉ chủ sở hữu được gỡ chủ sở hữu")
    if u and u["is_superadmin"] and wid == 1: raise HTTPException(400, "Không gỡ chủ sở hữu khỏi workspace mặc định")
    await A.q("DELETE FROM memberships WHERE workspace_id=%s AND user_id=%s", (wid, uid)); return {"ok": True}

@router.get("/api/admin/audit")
async def audit(limit: int = 200, user: str | None = None, path: str | None = None):
    """GĐ12.100d: the platform owner sees every row (incl. system rows with no workspace); a workspace admin only rows whose workspace_id is a workspace they administer."""
    ids = await _admin_wids(cur())
    scope, sargs = ("", ()) if ids is None else (" AND workspace_id = ANY(%s)", (list(ids),))
    return await A.q("SELECT * FROM audit_log WHERE (%s::text IS NULL OR username=%s) AND (%s::text IS NULL OR path LIKE %s)" + scope + " ORDER BY id DESC LIMIT %s",
                     (user, user, path, (path or "") + "%") + sargs + (max(1, min(limit, 1000)),))

@router.get("/api/admin/sessions")
async def sess_list():
    """GĐ12.100d: the platform owner sees every live session; a workspace admin only those of (non-owner) users who are members of a workspace they administer.
    (There is no admin revoke-session route: sessions end by logout / user lock / password reset, which _manageable_user already scopes.)"""
    ids = await _admin_wids(cur())
    sel = "SELECT left(token_hash,10) id, s.user_id, u.username, {ws} workspace_id, s.expires, s.ip, s.created FROM sessions s JOIN users u ON u.id=s.user_id WHERE expires > now()"
    if ids is None: return await A.q(sel.format(ws="s.workspace_id") + " ORDER BY s.created DESC")
    return await A.q(sel.format(ws="CASE WHEN s.workspace_id = ANY(%s) THEN s.workspace_id END") + " AND NOT u.is_superadmin AND EXISTS (SELECT 1 FROM memberships m WHERE m.user_id=u.id AND m.workspace_id = ANY(%s)) ORDER BY s.created DESC",
                     (list(ids), list(ids)))

# ------------------------------------------------------------ sandbox bền + file
def _sbx_dir(sid):
    if not re.fullmatch(r"[0-9a-f]{12}", sid): raise HTTPException(400, "id sandbox sai")
    return SBX_ROOT / sid

def _safe(sid, rel):
    base = _sbx_dir(sid).resolve(); p = (base / rel.lstrip("/")).resolve()
    if p != base and base not in p.parents: raise HTTPException(400, "Đường dẫn ra ngoài sandbox")
    return p

def _du(p: Path): return sum(f.stat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink())

async def _own_sbx(sid):
    s = await A.q("SELECT * FROM sandboxes WHERE id=%s", (sid,), one=True)
    if not s or s["workspace_id"] != cur_ws() or s.get("kind", "files") != "files": raise HTTPException(404, "Không có sandbox")   # GĐ11.40: virtual computers are not folder sandboxes (own API: sbx_runtime)
    return s

class SbxIn(BaseModel):
    name: str = Field(default="sandbox", max_length=80)
    ttl_hours: int = Field(default=168, ge=1, le=24 * 90)

@router.post("/api/sandboxes")
async def sbx_new(b: SbxIn):
    sid = uuid.uuid4().hex[:12]; d = SBX_ROOT / sid; d.mkdir(mode=0o770); os.chown(d, 65534, 65534)
    await A.q("INSERT INTO sandboxes(id,name,workspace_id,owner_id,ttl_hours) VALUES(%s,%s,%s,%s,%s)", (sid, b.name, cur_ws(), cur()["id"], b.ttl_hours))
    return {"id": sid}

@router.get("/api/sandboxes")
async def sbx_list():
    rows = await A.q("SELECT s.*, u.username owner FROM sandboxes s LEFT JOIN users u ON u.id=s.owner_id WHERE workspace_id=%s ORDER BY created DESC", (cur_ws(),))
    for r in rows:
        d = SBX_ROOT / r["id"]; r["bytes"] = _du(d) if d.exists() else 0
    return rows

@router.delete("/api/sandboxes/{sid}")
async def sbx_del(sid: str):
    await _own_sbx(sid); shutil.rmtree(_sbx_dir(sid), ignore_errors=True)
    await A.q("DELETE FROM sandboxes WHERE id=%s", (sid,)); return {"ok": True}

class ExecIn(BaseModel):
    code: str = Field(min_length=1, max_length=100000)
    timeout: int = Field(default=30, ge=1, le=120)

LOCKS: dict = {}
@router.post("/api/sandboxes/{sid}/exec")
async def sbx_exec(sid: str, b: ExecIn):
    await _own_sbx(sid); d = _sbx_dir(sid)
    if _du(d) > SBX_QUOTA: raise HTTPException(413, f"Sandbox vượt hạn mức {SBX_QUOTA >> 20}MB — xoá bớt file trước")
    async with LOCKS.setdefault(sid, asyncio.Lock()):
        name = "moza-psbx-" + sid + "-" + uuid.uuid4().hex[:4]
        cmd = ["docker", "run", "--rm", "-i", "--name", name, *A.SBX_RT, "--network", "none", "--memory", "256m", "--memory-swap", "256m", "--cpus", "0.5",
               "--pids-limit", "64", "--read-only", "--tmpfs", "/tmp:rw,size=16m,noexec", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--user", "65534:65534", "-v", f"{d}:/work:rw", "--workdir", "/work", "-e", "HOME=/work", A.SANDBOX_IMAGE, "python", "-I", "-"]
        t0 = time.perf_counter()
        p = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(p.communicate(b.code.encode()), b.timeout)
            res = {"exit": p.returncode, "stdout": out.decode(errors="replace")[:20000], "stderr": err.decode(errors="replace")[:5000]}
        except asyncio.TimeoutError:
            await (await asyncio.create_subprocess_exec("docker", "kill", name, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)).wait()
            res = {"exit": None, "stdout": "", "stderr": f"HẾT GIỜ sau {b.timeout}s"}
        used = _du(d)
        if used > SBX_QUOTA: res["warning"] = f"Vượt hạn mức {SBX_QUOTA >> 20}MB ({used >> 20}MB) — xoá bớt file, lần chạy sau sẽ bị từ chối"
        await A.q("UPDATE sandboxes SET last_used=now(), runs=runs+1 WHERE id=%s", (sid,))
        res["ms"] = int((time.perf_counter() - t0) * 1000); res["bytes"] = used
        return res

@router.get("/api/sandboxes/{sid}/files")
async def sbx_files(sid: str):
    await _own_sbx(sid); base = _sbx_dir(sid)
    return [{"path": str(f.relative_to(base)), "bytes": f.stat().st_size, "mtime": int(f.stat().st_mtime)} for f in sorted(base.rglob("*")) if f.is_file() and not f.is_symlink()]

@router.post("/api/sandboxes/{sid}/files")
async def sbx_upload(sid: str, file: UploadFile = File(...), path: str = Form("")):
    await _own_sbx(sid)
    data = await file.read()
    if len(data) > SBX_FILE_MAX: raise HTTPException(413, "File > 5MB")
    if _du(_sbx_dir(sid)) + len(data) > SBX_QUOTA: raise HTTPException(413, "Vượt hạn mức sandbox 50MB")
    name = re.sub(r"[^\w.\-]", "_", (path or file.filename or "file"))[:120]
    p = _safe(sid, name); p.write_bytes(data); os.chown(p, 65534, 65534); os.chmod(p, 0o660)
    return {"path": name, "bytes": len(data)}

@router.get("/api/sandboxes/{sid}/files/{rel:path}")
async def sbx_download(sid: str, rel: str):
    await _own_sbx(sid); p = _safe(sid, rel)
    if not p.is_file() or p.is_symlink(): raise HTTPException(404)
    return Response(p.read_bytes(), media_type="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{p.name}"'})

@router.delete("/api/sandboxes/{sid}/files/{rel:path}")
async def sbx_rm(sid: str, rel: str):
    await _own_sbx(sid); p = _safe(sid, rel)
    if p.is_file(): p.unlink()
    return {"ok": True}

async def sandbox_ttl():
    for s in await A.q("SELECT id FROM sandboxes WHERE kind='files' AND last_used < now() - ttl_hours * interval '1 hour'"):
        shutil.rmtree(SBX_ROOT / s["id"], ignore_errors=True); await A.q("DELETE FROM sandboxes WHERE id=%s", (s["id"],))
    await A.q("DELETE FROM sessions WHERE expires < now()")
    await A.q("DELETE FROM audit_log WHERE ts < now() - interval '180 days'")
