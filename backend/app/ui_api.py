"""API phụ trợ cho giao diện kiểu LangSmith: số đếm sidebar, Agent + Environment, Agent Overview, saved views của Tracing."""
import re
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb
import deepagent_api as A

router = APIRouter()
ENVS = ["production", "staging", "development", "local"]
# Môi trường của một run: metadata.ls_environment nếu có; nếu không thì suy ra (deployment → production, experiment/test → staging, còn lại → development)
ENV_SQL = ("COALESCE(NULLIF(r.metadata->>'ls_environment',''), CASE WHEN r.metadata ? 'deployment' THEN 'production' "
           "WHEN r.metadata ? 'experiment_id' OR r.metadata ? 'test_run_id' THEN 'staging' "
           "WHEN p.name NOT IN ('agents','playground','experiments','gateway') THEN 'local' ELSE 'development' END)")
IDENT_RX = re.compile(r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$|^[a-z]$")

DDL = """
ALTER TABLE hub_webhooks ADD COLUMN IF NOT EXISTS triggers TEXT[] NOT NULL DEFAULT '{commit}';
ALTER TABLE hub_webhooks ADD COLUMN IF NOT EXISTS headers JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE hub_webhooks ADD COLUMN IF NOT EXISTS prompt_id INT;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS identifier TEXT;
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS environment TEXT NOT NULL DEFAULT 'production';
CREATE TABLE IF NOT EXISTS saved_views(id SERIAL PRIMARY KEY, workspace_id INT, owner_id INT, scope TEXT NOT NULL DEFAULT 'tracing',
  name TEXT NOT NULL, description TEXT, state JSONB NOT NULL, created TIMESTAMPTZ NOT NULL DEFAULT now());
ALTER TABLE annotation_queues ADD COLUMN IF NOT EXISTS settings JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE annotation_queues ADD COLUMN IF NOT EXISTS assignees TEXT[] NOT NULL DEFAULT '{}';
CREATE TABLE IF NOT EXISTS contexts(id SERIAL PRIMARY KEY, workspace_id INT, kind TEXT NOT NULL DEFAULT 'agent', name TEXT NOT NULL, description TEXT,
  visibility TEXT NOT NULL DEFAULT 'private', status TEXT NOT NULL DEFAULT 'draft', files JSONB NOT NULL DEFAULT '{}'::jsonb, version INT NOT NULL DEFAULT 1,
  created TIMESTAMPTZ NOT NULL DEFAULT now(), updated TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS context_versions(context_id INT NOT NULL REFERENCES contexts(id) ON DELETE CASCADE, version INT NOT NULL, files JSONB NOT NULL,
  published TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(context_id, version));
"""

def slug(name: str, aid: int) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower().encode("ascii", "ignore").decode()).strip("-")[:63].rstrip("-")
    return s if IDENT_RX.match(s) else f"agent-{aid}"

async def ensure_ident(aid: int, name: str) -> str:
    ident, n = slug(name, aid), 1
    while await A.q("SELECT 1 FROM agents WHERE identifier=%s AND id<>%s", (ident, aid)):
        ident = f"{slug(name, aid)[:50]}-{aid}-{n}"; n += 1
    await A.q("UPDATE agents SET identifier=%s WHERE id=%s AND identifier IS NULL", (ident, aid))
    return ident

async def init():
    for st in [x for x in DDL.split(";\n") if x.strip()]:
        await A.q(st)
    for a in await A.q("SELECT id, name FROM agents WHERE identifier IS NULL"):
        await ensure_ident(a["id"], a["name"])

@router.get("/api/ui/counts")
async def counts():
    q = lambda s: A.q(s, one=True)
    out = {}
    for k, s in {"runs": "SELECT count(*) n FROM tr_runs r JOIN projects p ON p.id=r.project_id WHERE r.parent_id IS NULL AND r.start_time > now()-interval '30 days'",
                 "datasets": "SELECT count(*) n FROM datasets", "prompts": "SELECT count(*) n FROM prompts",
                 "deployments": "SELECT count(*) n FROM deployments d WHERE status='active'"}.items():
        try: out[k] = (await q(s))["n"]
        except Exception:  # noqa: BLE001
            out[k] = None
    return out

def _agent_filter(aid):
    return ("(r.metadata->>'agent_id'=%s OR %s = ANY(r.tags))", (str(aid), f"agent:{aid}"))

@router.get("/api/ui/agents_summary")
async def agents_summary(days: int = 7):
    days = max(1, min(days, 90))
    rows = await A.q("SELECT a.id, a.name, a.identifier, a.current_version, a.created FROM agents a WHERE NOT a.deleted AND NOT a.hidden ORDER BY a.id")
    stats = {str(r["aid"]): r for r in await A.q(
        "SELECT r.metadata->>'agent_id' aid, count(*) n, count(*) FILTER (WHERE r.status='error') e, coalesce(sum(r.total_tokens),0) tk, "
        "coalesce(sum(r.cost_usd),0)::float8 cost, max(r.start_time) last FROM tr_runs r JOIN projects p ON p.id=r.project_id "
        "WHERE r.parent_id IS NULL AND r.start_time > now() - make_interval(days => %s) AND r.metadata ? 'agent_id' GROUP BY 1", (days,))}
    for a in rows:
        s = stats.get(str(a["id"])) or {}
        a.update(traces=s.get("n", 0), errors=s.get("e", 0), tokens=s.get("tk", 0), cost=s.get("cost", 0.0), last_run=s.get("last"))
    return rows

@router.get("/api/ui/agents/{aid}/overview")
async def agent_overview(aid: int, days: int = 7):
    days = max(1, min(days, 90))
    a = await A.q("SELECT id, name, identifier, current_version, created FROM agents WHERE id=%s AND NOT deleted", (aid,), one=True)
    if not a: raise HTTPException(404, "Agent not found")
    if not a["identifier"]: a["identifier"] = await ensure_ident(aid, a["name"])
    w, p = _agent_filter(aid)
    base = f"FROM tr_runs r JOIN projects p ON p.id=r.project_id WHERE r.parent_id IS NULL AND r.start_time > now() - make_interval(days => %s) AND {w}"
    m = await A.q("SELECT count(*) n, count(*) FILTER (WHERE r.status='error') e, coalesce(sum(r.total_tokens),0) tk, coalesce(sum(r.cost_usd),0)::float8 cost " + base, (days, *p), one=True)
    spark = await A.q("SELECT date_trunc('day', r.start_time AT TIME ZONE 'Asia/Bangkok') d, count(*) n " + base + " GROUP BY 1 ORDER BY 1", (days, *p))
    envs = {r["env"]: r for r in await A.q(
        f"SELECT {ENV_SQL} env, max(r.start_time) last, count(*) n, count(*) FILTER (WHERE r.status='error') e, "
        "percentile_cont(.5) WITHIN GROUP (ORDER BY r.latency_ms) p50, percentile_cont(.99) WITHIN GROUP (ORDER BY r.latency_ms) p99 " + base + " GROUP BY 1", (days, *p))}
    desc = {"production": "Live traffic from deployments", "staging": "Experiments and test runs", "development": "Chat, Playground and Studio runs", "local": "SDK / OTLP traces from local code"}
    env_rows = [{"environment": e, "project": f"{a['identifier']}-{e}", "description": desc[e], "last": (envs.get(e) or {}).get("last"),
                 "traces": (envs.get(e) or {}).get("n", 0), "errors": (envs.get(e) or {}).get("e", 0),
                 "p50_ms": (envs.get(e) or {}).get("p50"), "p99_ms": (envs.get(e) or {}).get("p99")} for e in ["local", "development", "staging", "production"]]
    deps = await A.q("SELECT id, slug, version, status, environment, updated FROM deployments WHERE agent_id=%s ORDER BY id DESC", (aid,))
    try:
        ev = await A.q("SELECT count(*) n FROM automation_rules WHERE enabled AND actions::text LIKE '%%evaluator%%'", one=True)
    except Exception:  # noqa: BLE001
        ev = {"n": None}
    issues = await A.q("SELECT count(*) n " + base + " AND r.status='error'", (days, *p), one=True)
    return {"agent": a, "days": days, "metrics": {"traces": m["n"], "errors": m["e"], "error_rate": (m["e"] / m["n"]) if m["n"] else 0, "tokens": m["tk"], "cost": m["cost"]},
            "sparkline": [{"day": r["d"].date().isoformat(), "n": r["n"]} for r in spark], "environments": env_rows, "deployments": deps,
            "online_evaluators": ev["n"], "error_traces": issues["n"]}

class AgentIdent(BaseModel):
    identifier: str = Field(min_length=1, max_length=63)

@router.post("/api/ui/agents/{aid}/identifier")
async def set_identifier(aid: int, b: AgentIdent):
    """Identifier chỉ đặt được khi agent chưa có (giống LangSmith: không đổi sau khi tạo)."""
    if not IDENT_RX.match(b.identifier): raise HTTPException(400, "Identifier must be 1-63 lowercase letters, digits or hyphens, start with a letter and end with a letter or digit")
    a = await A.q("SELECT identifier FROM agents WHERE id=%s", (aid,), one=True)
    if not a: raise HTTPException(404, "Agent not found")
    if a["identifier"]: raise HTTPException(409, "Agent identifier cannot be changed after creation")
    await A.q("UPDATE agents SET identifier=%s WHERE id=%s", (b.identifier, aid)); return {"ok": True}

# ---- saved views (Tracing) ----
class View(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = None
    state: dict
    scope: str = "tracing"

@router.get("/api/ui/views")
async def views(scope: str = "tracing"):
    return await A.q("SELECT id, name, description, state, created FROM saved_views WHERE scope=%s AND workspace_id IS NOT DISTINCT FROM aos_ws() ORDER BY name", (scope,))

@router.post("/api/ui/views")
async def save_view(b: View):
    return await A.q("INSERT INTO saved_views(workspace_id, owner_id, scope, name, description, state) VALUES(aos_ws(), aos_uid(), %s,%s,%s,%s) RETURNING id",
                     (b.scope, b.name, b.description, Jsonb(b.state)), one=True)

@router.put("/api/ui/views/{vid}")
async def update_view(vid: int, b: View):
    r = await A.q("UPDATE saved_views SET name=%s, description=%s, state=%s WHERE id=%s AND workspace_id IS NOT DISTINCT FROM aos_ws() RETURNING id",
                  (b.name, b.description, Jsonb(b.state), vid), one=True)
    if not r: raise HTTPException(404, "View not found")
    return r

@router.delete("/api/ui/views/{vid}")
async def delete_view(vid: int):
    await A.q("DELETE FROM saved_views WHERE id=%s AND workspace_id IS NOT DISTINCT FROM aos_ws()", (vid,)); return {"ok": True}

# ---- Context Hub (Agent / Skill contexts: markdown files, draft -> active)
class Ctx(BaseModel):
    kind: str = Field(default="agent", pattern=r"^(agent|skill)$")
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    visibility: str = Field(default="private", pattern=r"^(private|public)$")
    status: str = Field(default="draft", pattern=r"^(draft|active)$")
    files: dict = {}

import re as _re, yaml as _yaml
_SKILL_RE = r"^[a-z0-9]+(-[a-z0-9]+)*$"
def skill_check(name: str, files: dict):
    """Agent Skills spec (agentskills.io) như deepagents 0.7.28 áp dụng: SKILL.md + YAML frontmatter; name 1-64, chữ thường/số/gạch nối, trùng tên thư mục; description 1-1024."""
    md = files.get("SKILL.md")
    if not isinstance(md, str): raise HTTPException(400, "Skill must contain SKILL.md / Skill phải có file SKILL.md")
    m = _re.match(r"^---\s*\n(.*?)\n---\s*(\n|$)", md, _re.S)
    if not m: raise HTTPException(400, "SKILL.md must start with YAML frontmatter between --- lines")
    try: fm = _yaml.safe_load(m.group(1)) or {}
    except Exception as e: raise HTTPException(400, f"Invalid YAML frontmatter: {str(e)[:200]}")  # noqa: B904
    if not isinstance(fm, dict): raise HTTPException(400, "Frontmatter must be a mapping")
    n, d = fm.get("name"), fm.get("description")
    if not isinstance(n, str) or not (1 <= len(n) <= 64) or not _re.match(_SKILL_RE, n): raise HTTPException(400, "frontmatter name: 1-64 chars, lowercase letters/digits/hyphens")
    if n != name: raise HTTPException(400, f"frontmatter name '{n}' must equal the skill name '{name}'")
    if not isinstance(d, str) or not (1 <= len(d.strip()) <= 1024): raise HTTPException(400, "frontmatter description: required, max 1024 chars")
    if len(str(fm.get("compatibility") or "")) > 500: raise HTTPException(400, "frontmatter compatibility: max 500 chars")
    inc = (fm.get("metadata") or {}).get("include_tools") if isinstance(fm.get("metadata"), dict) else None
    if inc is not None and not isinstance(inc, str): raise HTTPException(400, "metadata.include_tools must be a space-separated string")
    return fm

def publish_check(b):
    if b.status != "active": return
    if b.kind == "skill": skill_check(b.name, b.files)
    elif not isinstance(b.files.get("AGENTS.md"), str): raise HTTPException(400, "Agent context must contain AGENTS.md to be published")

async def _snapshot(cid):
    await A.q("INSERT INTO context_versions(context_id,version,files) SELECT id,version,files FROM contexts WHERE id=%s AND status='active' ON CONFLICT DO NOTHING", (cid,))
    await __import__("context_hub").mirror_legacy(cid)   # GĐ9.1: keep the context pack of this legacy context in step (never raises)

def _files_ok(f: dict):
    if len(f) > 50 or any(not isinstance(k, str) or len(k) > 120 or ".." in k or not isinstance(v, str) or len(v) > 200_000 for k, v in f.items()):
        raise HTTPException(400, "Invalid files: up to 50 files, 200 KB each, no '..' in paths")

@router.get("/api/contexts")
async def ctx_list():
    return await A.q("SELECT id, kind, name, description, visibility, status, version, created, updated, (SELECT count(*) FROM jsonb_object_keys(files)) n_files FROM contexts WHERE workspace_id IS NOT DISTINCT FROM aos_ws() ORDER BY updated DESC")

@router.get("/api/contexts/{cid}")
async def ctx_get(cid: int):
    r = await A.q("SELECT * FROM contexts WHERE id=%s AND workspace_id IS NOT DISTINCT FROM aos_ws()", (cid,), one=True)
    if not r: raise HTTPException(404, "Context not found")
    return r

@router.post("/api/contexts")
async def ctx_new(b: Ctx):
    f = b.files or ({"SKILL.md": f"---\nname: {b.name}\ndescription: {b.description or b.name}\n---\n\n# {b.name}\n"} if b.kind == "skill" else {"AGENTS.md": f"# {b.name}\n\n{b.description or ''}\n"})
    _files_ok(f); b.files = f; publish_check(b)
    r = await A.q("INSERT INTO contexts(workspace_id,kind,name,description,visibility,status,files) VALUES(aos_ws(),%s,%s,%s,%s,%s,%s) RETURNING id",
                     (b.kind, b.name, b.description, b.visibility, b.status, Jsonb(f)), one=True)
    await _snapshot(r["id"]); return r

@router.put("/api/contexts/{cid}")
async def ctx_put(cid: int, b: Ctx):
    _files_ok(b.files); publish_check(b)
    r = await A.q("UPDATE contexts SET kind=%s,name=%s,description=%s,visibility=%s,status=%s,files=%s,version=version+1,updated=now() WHERE id=%s AND workspace_id IS NOT DISTINCT FROM aos_ws() RETURNING id, version",
                  (b.kind, b.name, b.description, b.visibility, b.status, Jsonb(b.files), cid), one=True)
    if not r: raise HTTPException(404, "Context not found")
    await _snapshot(cid); return r

@router.get("/api/contexts/{cid}/versions")
async def ctx_versions(cid: int):
    return await A.q("SELECT cv.version, cv.published, (SELECT count(*) FROM jsonb_object_keys(cv.files)) n_files FROM context_versions cv JOIN contexts c ON c.id=cv.context_id WHERE cv.context_id=%s AND c.workspace_id IS NOT DISTINCT FROM aos_ws() ORDER BY cv.version DESC", (cid,))

@router.get("/api/skills/published")
async def skills_published():
    return await A.q("SELECT c.kind, c.name, c.description, max(cv.version) version FROM contexts c JOIN context_versions cv ON cv.context_id=c.id WHERE c.workspace_id IS NOT DISTINCT FROM aos_ws() GROUP BY 1,2,3 ORDER BY 1,2")

@router.delete("/api/contexts/{cid}")
async def ctx_del(cid: int):
    if await A.q("SELECT 1 FROM contexts WHERE id=%s AND workspace_id IS NOT DISTINCT FROM aos_ws()", (cid,), one=True):
        await A.q("DELETE FROM context_versions WHERE context_id=%s", (cid,))  # publish snapshots go with the context
    await A.q("DELETE FROM contexts WHERE id=%s AND workspace_id IS NOT DISTINCT FROM aos_ws()", (cid,))
    await __import__("context_hub").mirror_delete(cid)   # GĐ9.1: the linked pack goes too unless an agent still pins it
    return {"ok": True}

# ---- usage (self-hosted replacement for Billing)
@router.get("/api/ui/usage")
async def usage(days: int = 30):
    days = max(1, min(days, 365))
    daily = await A.q("""SELECT (r.start_time AT TIME ZONE 'Asia/Bangkok')::date d, w.name workspace, count(*) traces, coalesce(sum(r.total_tokens),0) tokens, coalesce(sum(r.cost_usd),0)::float8 cost
        FROM tr_runs r JOIN projects p ON p.id=r.project_id LEFT JOIN workspaces w ON w.id=p.workspace_id
        WHERE r.parent_id IS NULL AND r.start_time > now() - make_interval(days => %s) GROUP BY 1,2 ORDER BY 1""", (days,))
    months = await A.q("""SELECT to_char(date_trunc('month', r.start_time AT TIME ZONE 'Asia/Bangkok'),'YYYY-MM') m, count(*) traces,
        count(*) FILTER (WHERE r.status='error') errors FROM tr_runs r WHERE r.parent_id IS NULL AND r.start_time > now() - interval '12 months' GROUP BY 1 ORDER BY 1""")
    by_project = await A.q("""SELECT p.name project, w.name workspace, count(*) traces, count(*) FILTER (WHERE r.status='error') errors FROM tr_runs r JOIN projects p ON p.id=r.project_id
        LEFT JOIN workspaces w ON w.id=p.workspace_id WHERE r.parent_id IS NULL AND r.start_time > date_trunc('month', now()) GROUP BY 1,2 ORDER BY 3 DESC""")
    return {"daily": daily, "months": months, "by_project": by_project}

@router.get("/api/ui/tags")
async def tag_summary():
    fb = await A.q("SELECT key, count(*) n, avg(score)::float8 avg, max(created) last FROM feedback GROUP BY key ORDER BY n DESC LIMIT 200")
    rt = await A.q("SELECT t tag, count(*) n FROM tr_runs, unnest(tags) t WHERE start_time > now() - interval '30 days' GROUP BY t ORDER BY n DESC LIMIT 200")
    return {"feedback_keys": fb, "resource_tags": rt}

class QSettings(BaseModel):
    settings: dict = {}
    assignees: list[str] = []

@router.patch("/api/ui/queues/{qid}/settings")
async def queue_settings(qid: int, b: QSettings):
    r = await A.q("UPDATE annotation_queues SET settings=%s, assignees=%s WHERE id=%s RETURNING id", (Jsonb(b.settings), b.assignees, qid), one=True)
    if not r: raise HTTPException(404, "Queue not found")
    return r

@router.get("/api/ui/evaluator_stats")
async def evaluator_stats():
    ev = await A.q("SELECT id, name, kind, config FROM evaluators ORDER BY id")
    n = {r["key"]: r["n"] for r in await A.q("SELECT key, count(*) n FROM feedback GROUP BY key")}
    rules = await A.q("SELECT r.actions, p.name project FROM automation_rules r LEFT JOIN projects p ON p.id=r.project_id WHERE r.enabled")
    dss = await A.q("SELECT name, evaluator_ids FROM datasets WHERE evaluator_ids IS NOT NULL")
    for e in ev:
        e["evaluations"] = n.get((e["config"] or {}).get("key"), 0)
        e["bindings"] = sorted({r["project"] for r in rules if any(a.get("evaluator_id") == e["id"] for a in (r["actions"] or []))} - {None})
        e["datasets"] = sorted(d["name"] for d in dss if e["id"] in (d["evaluator_ids"] or []))
        e["bindings"] = e["bindings"] + ["dataset: " + x for x in e["datasets"]]
        e["status"] = "active" if e["bindings"] or e["evaluations"] else "idle"
    return ev


# ---- Prompt webhooks with triggers (commit / tag) and custom headers. Header values are write-only: listed as names only.
class PHook(BaseModel):
    url: str = Field(pattern=r"^https?://", max_length=500)
    triggers: list[str] = Field(default=["commit"])
    headers: dict = {}
    prompt_id: int | None = None

@router.post("/api/ui/hub_webhooks")
async def phook_new(b: PHook):
    tr = [t for t in b.triggers if t in ("commit", "tag")] or ["commit"]
    hd = {str(k)[:80]: str(v)[:500] for k, v in list(b.headers.items())[:20]}
    return await A.q("INSERT INTO hub_webhooks(url,triggers,headers,prompt_id) VALUES(%s,%s,%s,%s) RETURNING id", (b.url, tr, Jsonb(hd), b.prompt_id), one=True)

@router.get("/api/ui/hub_webhooks")
async def phook_list():
    rows = await A.q("SELECT id,url,triggers,headers,prompt_id,created FROM hub_webhooks ORDER BY id")
    for r in rows: r["headers"] = sorted((r.get("headers") or {}).keys())
    return rows

@router.get("/api/ui/feedback_recent")
async def feedback_recent(key: str = "", limit: int = 20):
    return await A.q("SELECT run_id, key, score, value, comment, source, created FROM feedback WHERE (%s = '' OR key = %s) ORDER BY created DESC LIMIT %s", (key, key, min(limit, 200)))
