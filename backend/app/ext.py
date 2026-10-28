"""Final push: OIDC/SSO, MCP server, A2A, Anthropic /v1/messages, sandbox snapshot/mount/link ký số, data export,
presigned feedback, feedback criteria + audit, retry experiment, backtest, prompt formats/presets/context, playground tools/multimodal,
re-embed Store. Tự viết theo tài liệu công khai (MCP spec 2025-06-18, A2A spec, Anthropic Messages API, OpenID Connect Core)."""
import contextlib, asyncio, base64, csv, gzip, hashlib, hmac, json, os, re, secrets, shutil, tarfile, time, uuid
from pathlib import Path
import httpx
from fastapi import APIRouter, HTTPException, Request, Header
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse, Response
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb
import deepagent_api as A
import admin, obs

router = APIRouter()
DDL = """
CREATE TABLE IF NOT EXISTS oidc_config(id INT PRIMARY KEY DEFAULT 1 CHECK (id=1), enabled BOOLEAN NOT NULL DEFAULT false, issuer TEXT, client_id TEXT,
  client_secret_env TEXT, scopes TEXT NOT NULL DEFAULT 'openid email profile', username_claim TEXT NOT NULL DEFAULT 'preferred_username',
  auto_create BOOLEAN NOT NULL DEFAULT false, default_role TEXT NOT NULL DEFAULT 'viewer', default_workspace INT NOT NULL DEFAULT 1,
  allowed_domains TEXT[], button_label TEXT NOT NULL DEFAULT 'Đăng nhập SSO', updated TIMESTAMPTZ NOT NULL DEFAULT now());
ALTER TABLE users ADD COLUMN IF NOT EXISTS oidc_sub TEXT;
CREATE TABLE IF NOT EXISTS a2a_tasks(id TEXT PRIMARY KEY, agent_id INT, context_id TEXT, state TEXT, history JSONB, artifacts JSONB,
  workspace_id INT, created TIMESTAMPTZ NOT NULL DEFAULT now(), updated TIMESTAMPTZ NOT NULL DEFAULT now());
ALTER TABLE a2a_tasks ADD COLUMN IF NOT EXISTS task JSONB;   -- GĐ9.6: full A2A 1.0 Task (proto JSON) + owner
ALTER TABLE a2a_tasks ADD COLUMN IF NOT EXISTS owner_id INT;
CREATE TABLE IF NOT EXISTS sandbox_volumes(id TEXT PRIMARY KEY, name TEXT NOT NULL, workspace_id INT NOT NULL, owner_id INT, created TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE(workspace_id, name));
CREATE TABLE IF NOT EXISTS sandbox_snapshots(id TEXT PRIMARY KEY, sandbox_id TEXT, name TEXT, workspace_id INT NOT NULL, owner_id INT, bytes BIGINT,
  created TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS export_jobs(id SERIAL PRIMARY KEY, project_id INT, workspace_id INT NOT NULL, fmt TEXT, start_ts TIMESTAMPTZ, end_ts TIMESTAMPTZ,
  status TEXT NOT NULL DEFAULT 'queued', rows BIGINT, bytes BIGINT, file TEXT, error TEXT, created TIMESTAMPTZ NOT NULL DEFAULT now(), finished TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS feedback_tokens(token_hash TEXT PRIMARY KEY, run_id UUID NOT NULL, key TEXT NOT NULL, expires TIMESTAMPTZ NOT NULL,
  uses INT NOT NULL DEFAULT 0, max_uses INT NOT NULL DEFAULT 1, created TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS feedback_configs(id SERIAL PRIMARY KEY, workspace_id INT NOT NULL, key TEXT NOT NULL, type TEXT NOT NULL CHECK (type IN ('continuous','categorical','freeform')),
  min DOUBLE PRECISION, max DOUBLE PRECISION, categories JSONB, description TEXT, created TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(workspace_id, key));
CREATE TABLE IF NOT EXISTS model_presets(id SERIAL PRIMARY KEY, workspace_id INT NOT NULL, name TEXT NOT NULL, model TEXT NOT NULL, temperature DOUBLE PRECISION,
  max_tokens INT, top_p DOUBLE PRECISION, fallback_models TEXT[], created TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(workspace_id, name));
CREATE TABLE IF NOT EXISTS context_files(id SERIAL PRIMARY KEY, workspace_id INT NOT NULL, name TEXT NOT NULL, content TEXT NOT NULL, version INT NOT NULL DEFAULT 1,
  updated TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(workspace_id, name));
ALTER TABLE prompt_versions ADD COLUMN IF NOT EXISTS template_format TEXT NOT NULL DEFAULT 'mustache';
ALTER TABLE model_presets ADD COLUMN IF NOT EXISTS reasoning_effort TEXT;   -- GĐ9.3
"""
WS_TABLES = ["a2a_tasks", "sandbox_volumes", "sandbox_snapshots", "export_jobs", "feedback_configs", "model_presets", "context_files"]
EXPORT_DIR = Path(os.environ.get("AGENTOS_EXPORT_DIR", "/opt/agentos/exports"))
VOL_ROOT = admin.SBX_ROOT.parent / "volumes"; SNAP_ROOT = admin.SBX_ROOT.parent / "snapshots"
KEY_FILE = Path("/opt/agentos/.link_key")

async def init():
    await A.q(DDL)
    for t in WS_TABLES:
        await A.q(f"ALTER TABLE {t} ALTER COLUMN workspace_id SET DEFAULT coalesce(aos_ws(), 1)")
        await admin._policy(t, "aos_ws() IS NULL OR workspace_id = aos_ws()")
    await admin._policy("feedback_tokens", "aos_ws() IS NULL OR EXISTS (SELECT 1 FROM tr_runs r WHERE r.id = feedback_tokens.run_id)")
    await admin.rls_grants()
    for d in (EXPORT_DIR, VOL_ROOT, SNAP_ROOT): d.mkdir(parents=True, exist_ok=True)
    os.chmod(EXPORT_DIR, 0o700); os.chmod(SNAP_ROOT, 0o700)
    if not KEY_FILE.exists():
        KEY_FILE.write_bytes(secrets.token_bytes(32)); os.chmod(KEY_FILE, 0o600)

def _sign(msg: str) -> str: return hmac.new(KEY_FILE.read_bytes(), msg.encode(), hashlib.sha256).hexdigest()[:32]
def _ws(): return admin.cur_ws()
def _u(): return admin.cur() or {}
def _admin_only():
    if (_u().get("role")) != "admin": raise HTTPException(403, "Chỉ admin")
def _base(req: Request):
    host = req.headers.get("x-forwarded-host") or req.headers.get("host") or ""
    proto = "http" if re.match(r"^(127\.0\.0\.1|localhost)(:|$)", host) else "https"     # công khai luôn qua TLS (Caddy/Cloudflare)
    return f"{proto}://{host}"
def _rpc(id_, result=None, error=None):
    return {"jsonrpc": "2.0", "id": id_, **({"error": error} if error else {"result": result})}

# ================================================================ OIDC / SSO (Authorization Code + PKCE)
class OidcCfg(BaseModel):
    enabled: bool = False
    issuer: str | None = Field(default=None, max_length=300)
    client_id: str | None = Field(default=None, max_length=300)
    client_secret_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{2,60}$")   # TÊN biến môi trường chứa secret, không lưu secret trong DB
    scopes: str = "openid email profile"
    username_claim: str = Field(default="preferred_username", pattern=r"^[\w.]{1,60}$")
    auto_create: bool = False
    default_role: str = Field(default="viewer", pattern=r"^(viewer|editor)$")
    default_workspace: int = 1
    allowed_domains: list[str] | None = None
    button_label: str = Field(default="Đăng nhập SSO", max_length=60)

def _issuer_ok(u):
    return bool(u) and (u.startswith("https://") or bool(re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", u)))

@router.get("/api/admin/oidc")
async def oidc_get():
    import admin; await admin.require_platform_owner()   # GĐ12.100d: machine-wide config / numbers -> platform owner only
    r = await A.q("SELECT * FROM oidc_config WHERE id=1", one=True) or {"enabled": False}
    r["secret_present"] = bool(r.get("client_secret_env") and os.environ.get(r["client_secret_env"]))
    return r

@router.put("/api/admin/oidc")
async def oidc_put(b: OidcCfg):
    import admin; await admin.require_platform_owner()   # GĐ12.100d: machine-wide config / numbers -> platform owner only
    if b.enabled:
        if not (_issuer_ok(b.issuer) and b.client_id): raise HTTPException(400, "Bật OIDC cần issuer (https) và client_id")
        try: await _discovery(b.issuer, force=True)
        except Exception as e: raise HTTPException(400, f"Không đọc được discovery của issuer: {str(e)[:200]}")  # noqa: B904
    await A.q("""INSERT INTO oidc_config(id,enabled,issuer,client_id,client_secret_env,scopes,username_claim,auto_create,default_role,default_workspace,allowed_domains,button_label)
        VALUES(1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET enabled=EXCLUDED.enabled, issuer=EXCLUDED.issuer, client_id=EXCLUDED.client_id,
        client_secret_env=EXCLUDED.client_secret_env, scopes=EXCLUDED.scopes, username_claim=EXCLUDED.username_claim, auto_create=EXCLUDED.auto_create,
        default_role=EXCLUDED.default_role, default_workspace=EXCLUDED.default_workspace, allowed_domains=EXCLUDED.allowed_domains, button_label=EXCLUDED.button_label, updated=now()""",
              (b.enabled, b.issuer, b.client_id, b.client_secret_env, b.scopes, b.username_claim, b.auto_create, b.default_role, b.default_workspace, b.allowed_domains, b.button_label))
    return await oidc_get()

DISC: dict = {}
async def _discovery(issuer, force=False):
    if not force and issuer in DISC and DISC[issuer][0] > time.time(): return DISC[issuer][1]
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(issuer.rstrip("/") + "/.well-known/openid-configuration"); r.raise_for_status(); d = r.json()
    if d.get("issuer", "").rstrip("/") != issuer.rstrip("/"): raise ValueError("issuer trong discovery không khớp")
    for k in ("authorization_endpoint", "token_endpoint"):
        if not _issuer_ok(d.get(k)): raise ValueError(f"{k} không hợp lệ")
    DISC[issuer] = (time.time() + 3600, d); return d

PENDING: dict = {}   # state -> (exp, verifier, nonce, next)

async def _cfg_enabled():
    c = await A.q("SELECT * FROM oidc_config WHERE id=1 AND enabled", one=True)
    if not c: raise HTTPException(404, "SSO chưa được cấu hình/bật")
    return c

@router.get("/login/oidc/status")
async def oidc_status():
    c = await A.q("SELECT enabled, button_label FROM oidc_config WHERE id=1", one=True)
    return {"enabled": bool(c and c["enabled"]), "label": (c or {}).get("button_label") or "Đăng nhập SSO"}

@router.get("/login/oidc")
async def oidc_start(request: Request, next: str = "/"):
    c = await _cfg_enabled(); d = await _discovery(c["issuer"])
    state = secrets.token_urlsafe(24); verifier = secrets.token_urlsafe(48); nonce = secrets.token_urlsafe(16)
    chal = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    now = time.time()
    for k in [k for k, v in PENDING.items() if v[0] < now]: PENDING.pop(k, None)
    PENDING[state] = (now + 600, verifier, nonce, next if next.startswith("/") and not next.startswith("//") else "/")
    qp = httpx.QueryParams({"response_type": "code", "client_id": c["client_id"], "redirect_uri": _base(request) + "/login/oidc/callback", "scope": c["scopes"],
                            "state": state, "nonce": nonce, "code_challenge": chal, "code_challenge_method": "S256"})
    ae = d["authorization_endpoint"]
    return RedirectResponse(ae + ("&" if "?" in ae else "?") + str(qp), 302)

@router.get("/login/oidc/callback")
async def oidc_cb(request: Request, code: str = "", state: str = "", error: str = ""):
    if error: raise HTTPException(401, f"IdP từ chối: {error[:100]}")
    p = PENDING.pop(state, None)
    if not p or p[0] < time.time(): raise HTTPException(400, "state không hợp lệ/hết hạn")
    c = await _cfg_enabled(); d = await _discovery(c["issuer"])
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": _base(request) + "/login/oidc/callback", "client_id": c["client_id"], "code_verifier": p[1]}
    sec = os.environ.get(c["client_secret_env"]) if c["client_secret_env"] else None
    if sec: data["client_secret"] = sec
    async with httpx.AsyncClient(timeout=15) as h:
        t = await h.post(d["token_endpoint"], data=data, headers={"Accept": "application/json"})
        if t.status_code != 200: raise HTTPException(401, "Đổi code thất bại")
        tok = t.json(); claims = {}
        if d.get("userinfo_endpoint"):
            ui = await h.get(d["userinfo_endpoint"], headers={"Authorization": f"Bearer {tok.get('access_token')}"})
            if ui.status_code == 200: claims = ui.json()
    if tok.get("id_token"):   # kiểm tra nonce/aud/iss/exp (id_token nhận trực tiếp từ token endpoint qua TLS, OIDC Core 3.1.3.7)
        try:
            pl = json.loads(base64.urlsafe_b64decode(tok["id_token"].split(".")[1] + "==="))
        except Exception: raise HTTPException(401, "id_token hỏng")  # noqa: B904,BLE001
        aud = pl.get("aud"); aud = aud if isinstance(aud, list) else [aud]
        if pl.get("nonce") != p[2] or c["client_id"] not in aud or str(pl.get("iss", "")).rstrip("/") != c["issuer"].rstrip("/") or pl.get("exp", 0) < time.time():
            raise HTTPException(401, "id_token không hợp lệ (nonce/aud/iss/exp)")
        if claims.get("sub") and claims["sub"] != pl.get("sub"): raise HTTPException(401, "sub userinfo khác id_token")
        claims = {**pl, **claims}
    if not claims.get("sub"): raise HTTPException(401, "IdP không trả sub")
    uname = str(claims.get(c["username_claim"]) or claims.get("email") or "")[:60]
    if not re.fullmatch(r"[\w.@\-]{2,60}", uname): raise HTTPException(401, "Claim tên đăng nhập không hợp lệ")
    if c["allowed_domains"] and str(claims.get("email", "")).split("@")[-1].lower() not in [x.lower() for x in c["allowed_domains"]]:
        raise HTTPException(403, "Domain email không được phép")
    import lg_auth; await lg_auth.precheck_groups(claims)   # GĐ9.Q: not in an allowed group -> refused before any account is created
    u = await A.q("SELECT * FROM users WHERE oidc_sub=%s", (claims["sub"],), one=True) or await A.q("SELECT * FROM users WHERE username=%s", (uname,), one=True)
    if u and u.get("oidc_sub") and u["oidc_sub"] != claims["sub"]: raise HTTPException(403, "Tài khoản đã gắn với danh tính SSO khác")
    if u and (u.get("is_superadmin") or u.get("superadmin")) and not u.get("oidc_sub"): raise HTTPException(403, "Tài khoản chủ sở hữu chỉ đăng nhập bằng mật khẩu")
    if not u:
        if not c["auto_create"]: raise HTTPException(403, "Chưa có tài khoản, nhờ admin tạo hoặc bật auto_create")
        u = await A.q("INSERT INTO users(username,pw_hash,oidc_sub) VALUES(%s,%s,%s) RETURNING *", (uname, admin.hash_pw(secrets.token_urlsafe(32)), claims["sub"]), one=True)
        await A.q("INSERT INTO memberships(user_id,workspace_id,role) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (u["id"], c["default_workspace"], c["default_role"]))
    elif not u.get("oidc_sub"):
        await A.q("UPDATE users SET oidc_sub=%s WHERE id=%s", (claims["sub"], u["id"]))
    if u.get("disabled"): raise HTTPException(403, "Tài khoản bị khoá")
    import lg_auth; await lg_auth.apply_groups(u, claims, c)   # GĐ9.Q: group claim -> allowed groups + role/workspace memberships
    sess = secrets.token_urlsafe(32); ws = await admin._default_ws(u["id"])
    await A.q("INSERT INTO sessions(token_hash,user_id,workspace_id,expires,ip,ua) VALUES(%s,%s,%s,now()+interval '7 days',%s,%s)",
              (admin._h(sess), u["id"], ws, admin._ip(request), (request.headers.get("user-agent") or "")[:200]))
    await admin._audit({"id": u["id"], "username": u["username"], "ws": ws, "auth": "oidc"}, request, 200, 0, "đăng nhập")
    r = RedirectResponse(p[3], 303); r.set_cookie(admin.COOKIE, sess, max_age=7 * 86400, httponly=True, secure=True, samesite="lax", path="/")
    return r

# ================================================================ MCP server (Streamable HTTP, stateless, JSON response)
async def _agents():
    return await A.q("SELECT id, name, NULL::text description, current_version FROM agents WHERE NOT deleted AND NOT hidden ORDER BY id")

async def _ask(agent_id, message, thread_id=None, overrides=None):
    ver = await A.cur_version(agent_id); last = None
    async for ev in A.run_agent(agent_id, ver, thread_id or ("mcp-" + uuid.uuid4().hex[:10]), message, tags=["mcp"], overrides=overrides): last = ev
    return last or {}

# GĐ9.6: /mcp = official `mcp` 2.x MCPServer (spec 2026-07-28 + older handshake), tools list_agents / ask_agent. Mounted by mount_mcp(app).
_MCP = None
def mcp_server():
    global _MCP
    if _MCP is None:
        import mcp_bridge as B
        from langchain_core.tools import StructuredTool, ToolException
        async def list_agents() -> str:
            """List agents in the current workspace."""
            return json.dumps(json.loads(json.dumps(await _agents(), default=str)), ensure_ascii=False)
        async def ask_agent(agent_id: int, message: str, thread_id: str = "") -> str:
            """Send a message to an Agent OS agent and return its answer."""
            if not message.strip(): raise ToolException("message is required")
            try: r = await _ask(agent_id, message[:8000], thread_id or None)
            except HTTPException as e: raise ToolException(str(e.detail)) from None
            if r.get("error") and not r.get("paused"): raise ToolException(str(r["error"])[:500])
            return r.get("reply") or ""
        _MCP = B.build(B.LCServer("moza-agent-os", [StructuredTool.from_function(coroutine=list_agents, name="list_agents"),
                                                    StructuredTool.from_function(coroutine=ask_agent, name="ask_agent")]))
    return _MCP

def mount_mcp(app):
    from starlette.routing import Route
    import mcp_bridge as B
    a = B.asgi(mcp_server())
    for p in ("/mcp", "/mcp/"): app.router.routes.insert(0, Route(p, endpoint=a))

@contextlib.asynccontextmanager
async def mcp_lifespan():
    async with mcp_server().session_manager.run(): yield

# GĐ9.6: A2A moved to a2a_official.py (official a2a-sdk 1.2.3, A2A 1.0; old message/send etc. kept via SDK v0.3 compat)

# ================================================================ Anthropic Messages API -> Gemini (/v1/messages)
def _anth_to_oai(b):
    msgs = []; sysp = b.get("system")
    if sysp: msgs.append({"role": "system", "content": sysp if isinstance(sysp, str) else "".join(x.get("text", "") for x in sysp)})
    for m in b.get("messages") or []:
        c = m.get("content")
        if isinstance(c, str): msgs.append({"role": m["role"], "content": c}); continue
        texts, imgs, calls, results = [], [], [], []
        for blk in c or []:
            t = blk.get("type")
            if t == "text": texts.append({"type": "text", "text": blk["text"]})
            elif t == "image":
                s = blk.get("source") or {}
                imgs.append({"type": "image_url", "image_url": {"url": f"data:{s.get('media_type')};base64,{s.get('data')}" if s.get("type") == "base64" else s.get("url")}})
            elif t == "tool_use": calls.append({"id": blk["id"], "type": "function", "function": {"name": blk["name"], "arguments": json.dumps(blk.get("input") or {})}})
            elif t == "tool_result":
                rc = blk.get("content"); rc = rc if isinstance(rc, str) else "".join(x.get("text", "") for x in rc or [] if isinstance(x, dict))
                results.append({"role": "tool", "tool_call_id": blk["tool_use_id"], "content": rc})
        msgs.extend(results)
        if m["role"] == "assistant":
            if texts or calls: msgs.append({"role": "assistant", "content": "".join(x["text"] for x in texts) or None, **({"tool_calls": calls} if calls else {})})
        elif texts or imgs: msgs.append({"role": "user", "content": texts + imgs})
    o = {"model": b.get("model"), "messages": msgs, "max_tokens": b.get("max_tokens"), "temperature": b.get("temperature"), "top_p": b.get("top_p"), "stop": b.get("stop_sequences")}
    if b.get("tools"):
        o["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("input_schema") or {"type": "object"}}} for t in b["tools"]]
        tc = b.get("tool_choice") or {}
        o["tool_choice"] = {"any": "required", "none": "none", "tool": {"type": "function", "function": {"name": tc.get("name")}}}.get(tc.get("type"))
    return {k: v for k, v in o.items() if v is not None}

def _anth_content(msg):
    out = [{"type": "text", "text": msg["content"]}] if msg.get("content") else []
    for tc in msg.get("tool_calls") or []:
        try: inp = json.loads(tc["function"]["arguments"] or "{}")
        except Exception: inp = {}  # noqa: BLE001
        out.append({"type": "tool_use", "id": tc["id"], "name": tc["function"]["name"], "input": inp})
    return out

def _anth_err(status, typ, msg): return JSONResponse({"type": "error", "error": {"type": typ, "message": msg}}, status)

_ANTH_TYPES = {400: "invalid_request_error", 401: "authentication_error", 402: "api_error", 403: "permission_error", 404: "not_found_error", 429: "rate_limit_error"}
def _anth_http(e):
    """HTTPException raised by the shared Gateway checks (key / budget / policy) -> Anthropic error body with the same status."""
    d = e.detail
    msg = d["error"]["message"] if isinstance(d, dict) and isinstance(d.get("error"), dict) and d["error"].get("message") else str(d)
    return _anth_err(e.status_code, _ANTH_TYPES.get(e.status_code, "api_error"), msg)

@router.post("/v1/messages")
async def anth_messages(request: Request, x_api_key: str = Header(default=""), authorization: str = Header(default="")):
    import gw_api as G
    return await G.run_ticketed(_anth_messages(request, x_api_key, authorization))   # GĐ11.14: the cost reservation is freed when the call (or its stream) ends

async def _anth_messages(request, x_api_key, authorization):
    import gw_api as G, gateway as gw, gw_policies
    try: k = await G._vkey(authorization or (f"Bearer {x_api_key}" if x_api_key else ""))
    except HTTPException as e: return _anth_http(e)
    try: await G.budget(k, reserve=True)   # unified spend caps (GĐ6.1), strict + reservation (GĐ11.14)
    except HTTPException as e: return _anth_err(402, "api_error", e.detail["error"]["message"])
    try: await gw_policies.enforce(k, request.headers)   # GĐ10.3: same order as /v1/chat/completions (key -> budget -> per-customer X-Gateway-* policies); sets GW_VKEY / GW_HDRS
    except HTTPException as e: return _anth_http(e)
    try: b = await request.json()
    except Exception: return _anth_err(400, "invalid_request_error", "Invalid JSON body")  # noqa: BLE001
    if not b.get("max_tokens"): return _anth_err(400, "invalid_request_error", "max_tokens: Field required")
    model = b.get("model") or A.DEFAULT_MODEL
    if "/" in model:   # GĐ11.2: gemini/<m>, aos/fast accepted; another provider -> 400
        try: model = __import__("gw_router").v1_gemini(model, k["workspace_id"])
        except HTTPException as e: return _anth_err(400, "invalid_request_error", str(e.detail))
    if not re.fullmatch(r"gemini-[\w.\-]+", model): return _anth_err(400, "invalid_request_error", "Only Gemini models are supported (e.g. gemini-flash-lite-latest)")
    body = _anth_to_oai(b)
    try: body = G._guard_in(k, body); chain = G._chain(k, model)
    except HTTPException as e: return _anth_err(e.status_code, "invalid_request_error", str(e.detail))
    try: chain = await G.model_gate(k, model, chain)   # GĐ11.14: workspace model allow-list (403 permission_error)
    except HTTPException as e: return _anth_http(e)
    ids = [tc.get("id") for m in body["messages"] for tc in (m.get("tool_calls") or []) if tc.get("id")]
    sigs = {r["call_id"]: bytes(r["sig"]) for r in await A.q("SELECT call_id, sig FROM gw_tool_sigs WHERE call_id = ANY(%s)", (ids,))} if ids else {}
    contents, cfg = G.to_gemini(body, sigs); mid = "msg_" + uuid.uuid4().hex[:24]
    if b.get("stream"): return StreamingResponse(G.with_ticket(_anth_stream(k, body, chain, contents, cfg, mid)), media_type="text/event-stream")
    last = None
    for m in chain:
        t0 = time.perf_counter()
        try: r = await A._g.aio.models.generate_content(model=m, contents=contents, config=cfg)
        except Exception as e:  # noqa: BLE001
            last = e; await G._record(k, m, 0, 0, int((time.perf_counter() - t0) * 1000), "error", str(e)[:300], body, None)
            if any(x in str(e) for x in gw.RETRYABLE): gw._mark(m, str(e)); continue
            return _anth_err(400, "api_error", str(e)[:400])
        i, o = G._usage(r); so = {}; msg, finish = G._msg_from(r, so); await G._save_sigs(so)
        if k["pii_mode"] == "mask" and msg.get("content"): msg["content"] = G.pii_mask(msg["content"])
        elif k["pii_mode"] == "block" and msg.get("content") and G.pii_scan(msg["content"]): msg["content"] = "[Response blocked by PII guardrail]"
        await G._record(k, m, i, o, int((time.perf_counter() - t0) * 1000), "ok", None, body, msg)
        return JSONResponse({"id": mid, "type": "message", "role": "assistant", "model": m, "content": _anth_content(msg),
                             "stop_reason": "tool_use" if msg.get("tool_calls") else ("max_tokens" if finish == "length" else "end_turn"),
                             "stop_sequence": None, "usage": {"input_tokens": i, "output_tokens": o}}, headers={"x-agentos-model": m})
    return _anth_err(529, "overloaded_error", f"All fallback models failed: {str(last)[:200]}")

async def _anth_stream(k, body, chain, contents, cfg, mid):
    import gw_api as G, gateway as gw
    def ev(name, d): return f"event: {name}\ndata: {json.dumps({'type': name, **d}, ensure_ascii=False)}\n\n"
    last = None
    for m in chain:
        t0 = time.perf_counter(); i = o = 0; started = False; idx = -1; text_open = False; full = []; calls = []
        try:
            async for r in await A._g.aio.models.generate_content_stream(model=m, contents=contents, config=cfg):
                if not started:
                    yield ev("message_start", {"message": {"id": mid, "type": "message", "role": "assistant", "model": m, "content": [], "stop_reason": None,
                                                           "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}}); started = True
                if r.usage_metadata: i, o = G._usage(r)
                so = {}; msg, _ = G._msg_from(r, so); await G._save_sigs(so)
                if msg.get("content"):
                    if not text_open: idx += 1; text_open = True; yield ev("content_block_start", {"index": idx, "content_block": {"type": "text", "text": ""}})
                    t = G.pii_mask(msg["content"]) if k["pii_mode"] == "mask" else msg["content"]; full.append(t)
                    yield ev("content_block_delta", {"index": idx, "delta": {"type": "text_delta", "text": t}})
                for tc in msg.get("tool_calls") or []:
                    if text_open: yield ev("content_block_stop", {"index": idx}); text_open = False
                    idx += 1; calls.append(tc)
                    yield ev("content_block_start", {"index": idx, "content_block": {"type": "tool_use", "id": tc["id"], "name": tc["function"]["name"], "input": {}}})
                    yield ev("content_block_delta", {"index": idx, "delta": {"type": "input_json_delta", "partial_json": tc["function"]["arguments"] or "{}"}})
                    yield ev("content_block_stop", {"index": idx})
            if text_open: yield ev("content_block_stop", {"index": idx})
            yield ev("message_delta", {"delta": {"stop_reason": "tool_use" if calls else "end_turn", "stop_sequence": None}, "usage": {"output_tokens": o}})
            yield ev("message_stop", {})
            await G._record(k, m, i, o, int((time.perf_counter() - t0) * 1000), "ok", None, body, {"role": "assistant", "content": "".join(full), "tool_calls": calls or None}, stream=True)
            return
        except Exception as e:  # noqa: BLE001
            last = e; await G._record(k, m, 0, 0, int((time.perf_counter() - t0) * 1000), "error", str(e)[:300], body, None, stream=True)
            if started or not any(x in str(e) for x in gw.RETRYABLE): break
            gw._mark(m, str(e))
    yield ev("error", {"error": {"type": "api_error", "message": str(last)[:300]}})

# ================================================================ Sandbox: volumes (mounts), snapshots, signed download links
class VolIn(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]{0,39}$")

@router.post("/api/sandbox_volumes")
async def vol_new(b: VolIn):
    if await A.q("SELECT 1 FROM sandbox_volumes WHERE name=%s", (b.name,), one=True): raise HTTPException(409, "Volume name already exists")
    vid = uuid.uuid4().hex[:12]; d = VOL_ROOT / vid; d.mkdir(mode=0o770); os.chown(d, 65534, 65534)
    await A.q("INSERT INTO sandbox_volumes(id,name,owner_id) VALUES(%s,%s,%s)", (vid, b.name, _u().get("id")))
    return {"id": vid, "name": b.name}

@router.get("/api/sandbox_volumes")
async def vol_list():
    rows = await A.q("SELECT * FROM sandbox_volumes ORDER BY created DESC")
    for r in rows: r["bytes"] = admin._du(VOL_ROOT / r["id"]) if (VOL_ROOT / r["id"]).exists() else 0
    return rows

@router.delete("/api/sandbox_volumes/{vid}")
async def vol_del(vid: str):
    if not await A.q("DELETE FROM sandbox_volumes WHERE id=%s RETURNING id", (vid,), one=True): raise HTTPException(404, "Volume not found")
    shutil.rmtree(VOL_ROOT / vid, ignore_errors=True); return {"ok": True}

class Mount(BaseModel):
    volume: str
    readonly: bool = False

class ExecMIn(BaseModel):
    code: str = Field(min_length=1, max_length=100000)
    timeout: int = Field(default=30, ge=1, le=120)
    mounts: list[Mount] = []

@router.post("/api/sandboxes/{sid}/exec_mounted")
async def sbx_exec_m(sid: str, b: ExecMIn):
    """Like /exec, plus persistent volumes mounted at /mnt/<name> (rw or ro), shareable across sandboxes in the workspace."""
    await admin._own_sbx(sid); d = admin._sbx_dir(sid); extra = []
    if len(b.mounts) > 4: raise HTTPException(400, "At most 4 volumes")
    for m in b.mounts:
        v = await A.q("SELECT * FROM sandbox_volumes WHERE id=%s OR name=%s", (m.volume, m.volume), one=True)
        if not v: raise HTTPException(404, f"Volume not found: {m.volume}")
        if not (m.readonly or v.get("locked")) and admin._du(VOL_ROOT / v["id"]) > admin.SBX_QUOTA: raise HTTPException(413, f"Volume {v['name']} exceeds 50MB")
        extra += ["-v", f"{VOL_ROOT / v['id']}:/mnt/{v['name']}:{__import__('sbx_parity').mount_mode(v, m.readonly)}"]   # GĐ9.Q: imported (locked) volumes are read-only
    async with admin.LOCKS.setdefault(sid, asyncio.Lock()):
        name = "moza-psbx-" + sid + "-" + uuid.uuid4().hex[:4]
        cmd = ["docker", "run", "--rm", "-i", "--name", name, *A.SBX_RT, "--network", "none", "--memory", "256m", "--memory-swap", "256m", "--cpus", "0.5",
               "--pids-limit", "64", "--read-only", "--tmpfs", "/tmp:rw,size=16m,noexec", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
               "--user", "65534:65534", "-v", f"{d}:/work:rw", *extra, "--workdir", "/work", "-e", "HOME=/work", A.SANDBOX_IMAGE, "python", "-I", "-"]
        t0 = time.perf_counter()
        p = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(p.communicate(b.code.encode()), b.timeout)
            res = {"exit": p.returncode, "stdout": out.decode(errors="replace")[:20000], "stderr": err.decode(errors="replace")[:5000]}
        except asyncio.TimeoutError:
            await (await asyncio.create_subprocess_exec("docker", "kill", name, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)).wait()
            res = {"exit": None, "stdout": "", "stderr": f"TIMEOUT after {b.timeout}s"}
        await A.q("UPDATE sandboxes SET last_used=now(), runs=runs+1 WHERE id=%s", (sid,))
        res["ms"] = int((time.perf_counter() - t0) * 1000); return res

class SnapIn(BaseModel):
    name: str = Field(default="snapshot", max_length=80)

@router.post("/api/sandboxes/{sid}/snapshots")
async def snap_new(sid: str, b: SnapIn):
    await admin._own_sbx(sid); src = admin._sbx_dir(sid); snid = uuid.uuid4().hex[:12]; f = SNAP_ROOT / f"{snid}.tar.gz"
    import sbx_parity
    if (await A.q("SELECT count(*) n FROM sandbox_snapshots WHERE kind='files'", one=True))["n"] >= sbx_parity.MAX_SNAPSHOTS: raise HTTPException(429, f"At most {sbx_parity.MAX_SNAPSHOTS} snapshots per workspace: delete one first")   # GĐ9.Q
    def mk():
        with tarfile.open(f, "w:gz") as t:
            for p in src.rglob("*"):
                if p.is_file() and not p.is_symlink(): t.add(p, arcname=str(p.relative_to(src)))
    await asyncio.to_thread(mk); os.chmod(f, 0o600)
    await A.q("INSERT INTO sandbox_snapshots(id,sandbox_id,name,owner_id,bytes) VALUES(%s,%s,%s,%s,%s)", (snid, sid, b.name, _u().get("id"), f.stat().st_size))
    return {"id": snid, "bytes": f.stat().st_size}

@router.get("/api/sandbox_snapshots")
async def snap_list(): return await A.q("SELECT * FROM sandbox_snapshots WHERE kind='files' ORDER BY created DESC")   # GĐ11.47a: computer snapshots have their own API (sbx_images)

@router.delete("/api/sandbox_snapshots/{snid}")
async def snap_del(snid: str):
    if not await A.q("SELECT 1 FROM sandbox_snapshots WHERE id=%s AND kind='files'", (snid,), one=True): raise HTTPException(404, "Snapshot not found")
    users = await A.q("SELECT id FROM sandboxes WHERE snapshot_id=%s", (snid,))   # GĐ9.Q: like LangSmith, a snapshot that sandboxes were created/restored from cannot be deleted
    if users: raise HTTPException(409, f"Snapshot is still used by {len(users)} sandbox(es): delete or re-create them first")
    await A.q("DELETE FROM sandbox_snapshots WHERE id=%s", (snid,))
    (SNAP_ROOT / f"{snid}.tar.gz").unlink(missing_ok=True); return {"ok": True}

def _restore(snid, dst: Path):
    base = dst.resolve()
    with tarfile.open(SNAP_ROOT / f"{snid}.tar.gz", "r:gz") as t:
        for m in t.getmembers():
            if not m.isfile(): continue
            p = (base / m.name).resolve()
            if base not in p.parents: continue
            p.parent.mkdir(parents=True, exist_ok=True)
            with t.extractfile(m) as fs: p.write_bytes(fs.read())
            os.chown(p, 65534, 65534); os.chmod(p, 0o660)
            for par in p.parents:
                if par == base: break
                os.chown(par, 65534, 65534)

async def _snap_ok(snid):
    if not re.fullmatch(r"[0-9a-f]{12}", snid) or not await A.q("SELECT 1 FROM sandbox_snapshots WHERE id=%s AND kind='files'", (snid,), one=True): raise HTTPException(404, "Snapshot not found")

@router.post("/api/sandbox_snapshots/{snid}/restore/{sid}")
async def snap_restore(snid: str, sid: str):
    await _snap_ok(snid); await admin._own_sbx(sid); d = admin._sbx_dir(sid)
    for p in d.iterdir(): shutil.rmtree(p) if p.is_dir() else p.unlink()
    await asyncio.to_thread(_restore, snid, d); await A.q("UPDATE sandboxes SET snapshot_id=%s WHERE id=%s", (snid, sid)); return {"ok": True, "files": len([p for p in d.rglob("*") if p.is_file()])}

@router.post("/api/sandbox_snapshots/{snid}/clone")
async def snap_clone(snid: str, b: SnapIn):
    await _snap_ok(snid); r = await admin.sbx_new(admin.SbxIn(name=b.name)); await asyncio.to_thread(_restore, snid, admin._sbx_dir(r["id"]))
    await A.q("UPDATE sandboxes SET snapshot_id=%s WHERE id=%s", (snid, r["id"])); return r

class LinkIn(BaseModel):
    path: str = Field(min_length=1, max_length=300)
    expires_s: int = Field(default=3600, ge=60, le=7 * 86400)

@router.post("/api/sandboxes/{sid}/link")
async def sbx_link(sid: str, b: LinkIn, request: Request):
    await admin._own_sbx(sid); p = admin._safe(sid, b.path)
    if not p.is_file(): raise HTTPException(404, "File not found")
    rel = str(p.relative_to(admin._sbx_dir(sid).resolve())); exp = int(time.time()) + b.expires_s
    return {"url": f"{_base(request)}/share/sbx/{sid}/{rel}?exp={exp}&sig={_sign(f'sbx|{sid}|{rel}|{exp}')}", "expires": exp}

@router.get("/share/sbx/{sid}/{rel:path}")
async def sbx_public(sid: str, rel: str, exp: int = 0, sig: str = ""):
    if exp < time.time() or not hmac.compare_digest(sig, _sign(f"sbx|{sid}|{rel}|{exp}")): raise HTTPException(403, "Link expired or invalid signature")
    p = admin._safe(sid, rel)
    if not p.is_file() or p.is_symlink(): raise HTTPException(404, "File not found")
    return Response(p.read_bytes(), media_type="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{p.name}"'})

async def gc():
    for s in await A.q("SELECT id FROM sandbox_snapshots WHERE created < now() - interval '90 days'"):
        (SNAP_ROOT / f"{s['id']}.tar.gz").unlink(missing_ok=True); await A.q("DELETE FROM sandbox_snapshots WHERE id=%s", (s["id"],))
    for f in EXPORT_DIR.glob("*.gz"):
        if f.stat().st_mtime < time.time() - 7 * 86400: f.unlink()
    await A.q("DELETE FROM feedback_tokens WHERE expires < now() - interval '1 day'")

# ================================================================ Bulk data export + monitor
class ExportIn(BaseModel):
    project_id: int
    fmt: str = Field(default="jsonl", pattern=r"^(jsonl|csv)$")
    start: str | None = None
    end: str | None = None

EXPORT_COLS = ["id", "trace_id", "parent_id", "name", "run_type", "start_time", "end_time", "latency_ms", "status", "error", "inputs", "outputs", "metadata", "tags",
               "input_tokens", "output_tokens", "total_tokens", "cost_usd", "model", "thread_id"]

@router.post("/api/exports")
async def export_new(b: ExportIn):
    if not await A.q("SELECT 1 FROM projects WHERE id=%s", (b.project_id,), one=True): raise HTTPException(404, "Project not found")
    r = await A.q("INSERT INTO export_jobs(project_id,fmt,start_ts,end_ts) VALUES(%s,%s,%s,%s) RETURNING *", (b.project_id, b.fmt, b.start, b.end), one=True)
    ws = _ws()
    async def job():
        admin.USER.set(None); admin.WS_CTX.set(ws)      # own task context
        try:
            await A.q("UPDATE export_jobs SET status='running' WHERE id=%s", (r["id"],))
            f = EXPORT_DIR / f"export-{r['id']}-{secrets.token_hex(4)}.{b.fmt}.gz"; n = 0; last = None
            with gzip.open(f, "wt", encoding="utf-8") as out:
                w = csv.writer(out) if b.fmt == "csv" else None
                if w: w.writerow(EXPORT_COLS)
                while True:   # keyset pagination, bounded RAM
                    rows = await A.q(f"""SELECT {','.join(EXPORT_COLS)} FROM tr_runs WHERE project_id=%s AND (%s::timestamptz IS NULL OR start_time >= %s::timestamptz)
                        AND (%s::timestamptz IS NULL OR start_time < %s::timestamptz) AND (%s::timestamptz IS NULL OR (start_time, id::text) > (%s::timestamptz, %s::text))
                        ORDER BY start_time, id::text LIMIT 500""", (b.project_id, b.start, b.start, b.end, b.end, last and last[0], last and last[0], last and last[1]))
                    if not rows: break
                    for x in rows:
                        if w: w.writerow([json.dumps(x[c], ensure_ascii=False, default=str) if isinstance(x[c], (dict, list)) else x[c] for c in EXPORT_COLS])
                        else: out.write(json.dumps(x, ensure_ascii=False, default=str) + "\n")
                    n += len(rows); last = (rows[-1]["start_time"], str(rows[-1]["id"]))
                    await A.q("UPDATE export_jobs SET rows=%s WHERE id=%s", (n, r["id"]))
            os.chmod(f, 0o600)
            await A.q("UPDATE export_jobs SET status='done', rows=%s, bytes=%s, file=%s, finished=now() WHERE id=%s", (n, f.stat().st_size, f.name, r["id"]))
        except Exception as e:  # noqa: BLE001
            await A.q("UPDATE export_jobs SET status='error', error=%s, finished=now() WHERE id=%s", (str(e)[:300], r["id"]))
    asyncio.create_task(job()); return r

@router.get("/api/exports")
async def export_list(): return await A.q("SELECT e.*, p.name project FROM export_jobs e LEFT JOIN projects p ON p.id=e.project_id ORDER BY e.id DESC LIMIT 100")

@router.get("/api/exports/{xid}/download")
async def export_dl(xid: int):
    e = await A.q("SELECT * FROM export_jobs WHERE id=%s AND status='done'", (xid,), one=True)
    if not e or not e["file"]: raise HTTPException(404, "Export not found")
    f = EXPORT_DIR / e["file"]
    if not f.exists(): raise HTTPException(410, "Export file was cleaned up (kept 7 days)")
    return Response(f.read_bytes(), media_type="application/gzip", headers={"Content-Disposition": f'attachment; filename="{e["file"]}"'})

# ================================================================ Presigned feedback tokens, feedback criteria, score audit/correction
class FbTokIn(BaseModel):
    key: str = Field(pattern=r"^[\w\-.]{1,60}$")
    expires_hours: int = Field(default=72, ge=1, le=24 * 30)
    max_uses: int = Field(default=1, ge=1, le=1000)

@router.post("/api/obs/runs/{run_id}/feedback_token")
async def fb_token(run_id: str, b: FbTokIn, request: Request):
    if not await A.q("SELECT 1 FROM tr_runs WHERE id=%s", (run_id,), one=True): raise HTTPException(404, "Run not found")
    t = "fbt_" + secrets.token_urlsafe(24)
    await A.q("INSERT INTO feedback_tokens(token_hash,run_id,key,expires,max_uses) VALUES(%s,%s,%s,now()+%s*interval '1 hour',%s)", (admin._h(t), run_id, b.key, b.expires_hours, b.max_uses))
    return {"token": t, "url": f"{_base(request)}/share/feedback/{t}", "expires_hours": b.expires_hours}

def _check_fb(cfg, score, value):
    if not cfg: return
    if cfg["type"] == "continuous":
        if score is None: raise HTTPException(400, f"'{cfg['key']}' requires a numeric score")
        if (cfg["min"] is not None and float(score) < cfg["min"]) or (cfg["max"] is not None and float(score) > cfg["max"]):
            raise HTTPException(400, f"score must be within [{cfg['min']}, {cfg['max']}]")
    if cfg["type"] == "categorical":
        cats = [str(c.get("label")) for c in cfg["categories"] or []]
        if value is None or str(value) not in cats: raise HTTPException(400, f"value must be one of {cats}")

FB_BODY_MAX = 32768   # GĐ10.14: whole POST body of the public feedback link (comment 2 KB + correction 8 KB + metadata 4 KB fit with room)

@router.api_route("/share/feedback/{tok}", methods=["GET", "POST"])
async def fb_public(tok: str, request: Request, score: float | None = None, value: str | None = None, comment: str | None = None):
    correction = metadata = None
    if request.method == "POST":
        cl = request.headers.get("content-length") or ""
        if cl.isdigit() and int(cl) > FB_BODY_MAX: raise HTTPException(413, f"Body is larger than {FB_BODY_MAX} bytes")
        raw = await request.body()
        if len(raw) > FB_BODY_MAX: raise HTTPException(413, f"Body is larger than {FB_BODY_MAX} bytes")   # public route: size is checked before the token is spent
        try: d = json.loads(raw or b"{}")
        except Exception: d = {}  # noqa: BLE001
        if not isinstance(d, dict): d = {}
        score = d.get("score", score); value = d.get("value", value); comment = d.get("comment", comment)
        # GĐ10.14: correction (dict, <= 8 KB) and metadata (dict, <= 4 KB) are stored with the feedback; GET keeps score/value/comment only
        for nm, mx in (("correction", obs.FB_CORRECTION_MAX), ("metadata", obs.FB_METADATA_MAX)):
            v = d.get(nm)
            if v is None: continue
            if not isinstance(v, dict): raise HTTPException(400, f"{nm} must be a JSON object")
            if len(json.dumps(v, ensure_ascii=False, default=str)) > mx: raise HTTPException(413, f"{nm} is larger than {mx} bytes")
        correction, metadata = d.get("correction") or None, d.get("metadata") or None
    if score is None and value is None: raise HTTPException(400, "score or value is required")
    t = await A.q("SELECT * FROM feedback_tokens WHERE token_hash=%s AND expires > now() AND uses < max_uses", (admin._h(tok),), one=True)
    if not t: raise HTTPException(403, "Feedback token is invalid, expired or used up")
    w = await A.q("SELECT p.workspace_id FROM tr_runs r JOIN projects p ON p.id=r.project_id WHERE r.id=%s", (t["run_id"],), one=True)
    if not w: raise HTTPException(404, "Run not found")   # GĐ10.1: the token's run is gone -> nothing to attach feedback to (was: silently workspace 1)
    _check_fb(await A.q("SELECT * FROM feedback_configs WHERE key=%s AND workspace_id=%s", (t["key"], w["workspace_id"]), one=True), score, value)
    async with admin.as_workspace(w["workspace_id"]):   # GĐ10.1: use counter + feedback row in the workspace of the run the token was issued for
        if not await A.q("UPDATE feedback_tokens SET uses=uses+1 WHERE token_hash=%s AND uses < max_uses RETURNING 1", (admin._h(tok),), one=True):
            raise HTTPException(403, "Feedback token used up")
        r = await obs.add_feedback(obs.FbIn(run_id=str(t["run_id"]), key=t["key"], score=None if score is None else float(score), value=value,
                                            comment=(comment or "")[:2000] or None, correction=correction, metadata=metadata), source="presigned")
    return {"ok": True, "feedback_id": r["id"]}

class FbCfgIn(BaseModel):
    key: str = Field(pattern=r"^[\w\-.]{1,60}$")
    type: str = Field(pattern=r"^(continuous|categorical|freeform)$")
    min: float | None = None
    max: float | None = None
    categories: list[dict] | None = None
    description: str | None = None

@router.post("/api/feedback_configs")
async def fbc_set(b: FbCfgIn):
    if b.type == "categorical" and not b.categories: raise HTTPException(400, "categorical requires categories")
    if b.type == "continuous" and b.min is not None and b.max is not None and b.min >= b.max: raise HTTPException(400, "min must be < max")
    return await A.q("""INSERT INTO feedback_configs(key,type,min,max,categories,description) VALUES(%s,%s,%s,%s,%s,%s)
        ON CONFLICT(workspace_id,key) DO UPDATE SET type=EXCLUDED.type,min=EXCLUDED.min,max=EXCLUDED.max,categories=EXCLUDED.categories,description=EXCLUDED.description RETURNING *""",
                     (b.key, b.type, b.min, b.max, Jsonb(b.categories) if b.categories else None, b.description), one=True)

@router.get("/api/feedback_configs")
async def fbc_list(): return await A.q("SELECT * FROM feedback_configs ORDER BY key")

@router.delete("/api/feedback_configs/{cid}")
async def fbc_del(cid: int): await A.q("DELETE FROM feedback_configs WHERE id=%s", (cid,)); return {"ok": True}

@router.post("/api/feedback_configs/validate")
async def fbc_validate(b: dict):
    _check_fb(await A.q("SELECT * FROM feedback_configs WHERE key=%s", (b.get("key"),), one=True), b.get("score"), b.get("value")); return {"ok": True}

class CorrIn(BaseModel):
    score: float | None = None
    value: str | None = None
    comment: str | None = Field(default=None, max_length=2000)

@router.post("/api/feedback/{fid}/correct")
async def fb_correct(fid: int, b: CorrIn):
    """Audit evaluator scores: a reviewer corrects a score (original kept). LLM evaluators with use_corrections=true learn from these."""
    if b.score is None and b.value is None: raise HTTPException(400, "score or value is required")
    r = await A.q("UPDATE feedback SET correction=%s, corrected_by=%s, corrected_at=now() WHERE id=%s RETURNING id, key, score, value, correction, evaluator_id",
                  (Jsonb(b.model_dump(exclude_none=True)), _u().get("username"), fid), one=True)
    if not r: raise HTTPException(404, "Feedback not found")
    return r

@router.get("/api/evaluators/{eid}/audit")
async def ev_audit(eid: int, limit: int = 100):
    rows = await A.q("SELECT id, run_id, key, score, value, comment, correction, corrected_by, corrected_at, created FROM feedback WHERE evaluator_id=%s ORDER BY id DESC LIMIT %s",
                     (eid, min(limit, 500)))
    corr = [r for r in rows if r["correction"]]
    def same(r):
        c = r["correction"]
        if c.get("score") is not None and r["score"] is not None: return abs(float(c["score"]) - float(r["score"])) < 1e-9
        return c.get("value") is not None and c["value"] == r["value"]
    return {"total": len(rows), "audited": len(corr), "agreement": round(sum(map(same, corr)) / len(corr), 3) if corr else None, "rows": rows}

# ================================================================ Experiments: retry failed rows, results reader, backtests
@router.post("/api/experiments/{eid}/retry_failed")
async def exp_retry(eid: int):
    """Old name of POST /api/experiments/{id}/retry (9.Q: one implementation - failed AND missing rows re-run into the same experiment, stale scores replaced)."""
    import evals
    try: return await evals.exp_retry(eid)
    except HTTPException as ex:
        if ex.status_code == 400: return {"retrying": 0}   # nothing to retry: the old answer
        raise

@router.get("/api/experiments/{eid}/results.jsonl")
async def exp_results_jsonl(eid: int):
    rows = await A.q("""SELECT r.example_id, r.repetition, r.run_id, r.output, r.error, r.latency_ms, r.total_tokens, r.cost_usd,
        (SELECT json_object_agg(f.key, f.score) FROM feedback f WHERE f.run_id=r.run_id AND f.experiment_id=r.experiment_id) scores
        FROM exp_results r WHERE experiment_id=%s ORDER BY id""", (eid,))
    return Response("".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in rows), media_type="application/x-ndjson")

class BacktestIn(BaseModel):
    project_id: int
    agent_id: int
    version: int | None = None
    limit: int = Field(default=10, ge=1, le=100)
    evaluator_ids: list[int] = []

@router.post("/api/backtests")
async def backtest(b: BacktestIn):
    """Backtest a new agent: last N successful production traces -> dataset (old output as reference) -> experiment on the new agent/version."""
    import evals
    runs = await A.q("SELECT id FROM tr_runs WHERE project_id=%s AND parent_id IS NULL AND error IS NULL ORDER BY start_time DESC LIMIT %s", (b.project_id, b.limit))
    if not runs: raise HTTPException(400, "Project has no successful traces to backtest")
    ds = await A.q("INSERT INTO datasets(name,description) VALUES(%s,%s) RETURNING id",
                   (f"backtest-p{b.project_id}-{time.strftime('%Y%m%d-%H%M%S')}", f"Backtest from {len(runs)} production traces"), one=True)
    for r in runs: await evals.example_from_run(ds["id"], r["id"])
    eid = await evals.start_experiment(evals.ExpIn(name=f"backtest agent {b.agent_id}", dataset_id=ds["id"],
                                                   target={"type": "agent", "agent_id": b.agent_id, "version": b.version}, evaluator_ids=b.evaluator_ids))
    return {"dataset_id": ds["id"], "experiment_id": eid, "examples": len(runs)}

# ================================================================ Prompts: template formats, context files, model presets, playground chat (tools + multimodal)
def render_fmt(t: str, v: dict, fmt: str | None = None):
    fmt = fmt or "mustache"
    if fmt == "f-string":
        import string
        out = []
        for lit, field, spec, _conv in string.Formatter().parse(t):
            out.append(lit)
            if field is None: continue
            if not re.fullmatch(r"\w+", field): raise HTTPException(400, f"f-string only allows plain variable names: {{{field}}}")
            out.append(format(v[field], spec or "") if field in v else "{" + field + "}")
        return "".join(out)
    if fmt == "jinja2":
        from jinja2.sandbox import SandboxedEnvironment
        try: return SandboxedEnvironment(autoescape=False).from_string(t).render(**v)
        except Exception as e: raise HTTPException(400, f"jinja2 template error: {str(e)[:200]}")  # noqa: B904,BLE001
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: str(v.get(m.group(1), m.group(0))), t)

CTX_RE = r"\{\{\s*ctx:([\w\-.]+)\s*\}\}"
async def expand_context(t: str):
    names = set(re.findall(CTX_RE, t or ""))
    if not names: return t
    rows = {r["name"]: r["content"] for r in await A.q("SELECT name, content FROM context_files WHERE name = ANY(%s)", (list(names),))}
    return re.sub(CTX_RE, lambda m: rows.get(m.group(1), f"[missing context {m.group(1)}]"), t)

async def render_async(t, v, fmt=None): return render_fmt(await expand_context(t), v or {}, fmt)

class CtxIn(BaseModel):
    name: str = Field(pattern=r"^[\w\-.]{1,60}$")
    content: str = Field(max_length=200000)

@router.put("/api/context_files")
async def ctx_put(b: CtxIn):
    r = await A.q("""INSERT INTO context_files(name,content) VALUES(%s,%s) ON CONFLICT(workspace_id,name) DO UPDATE SET content=EXCLUDED.content,
        version=context_files.version+1, updated=now() RETURNING id,name,version,updated""", (b.name, b.content), one=True)
    A.S["agents"].clear(); return r

@router.get("/api/context_files")
async def ctx_list(): return await A.q("SELECT id,name,version,length(content) chars,updated FROM context_files ORDER BY name")

@router.get("/api/context_files/{name}")
async def ctx_get(name: str):
    r = await A.q("SELECT * FROM context_files WHERE name=%s", (name,), one=True)
    if not r: raise HTTPException(404, "Context file not found")
    return r

@router.delete("/api/context_files/{name}")
async def ctx_del(name: str): await A.q("DELETE FROM context_files WHERE name=%s", (name,)); A.S["agents"].clear(); return {"ok": True}

class PresetIn(BaseModel):
    """GĐ11.11 model configuration (saved preset). `model` = a model name / ref (gemini-x, claude-x, provider/model); `provider` = optional workspace provider name (then model is the upstream name).
    No secret field exists: the key belongs to the provider."""
    name: str = Field(pattern=r"^[\w\-. ]{1,60}$")
    model: str = Field(min_length=1, max_length=120)
    provider: str | None = Field(default=None, max_length=32)
    provider_api: str = Field(default="chat", pattern=r"^(chat|responses)$")
    extra_params: dict | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=65536)
    top_p: float | None = Field(default=None, ge=0, le=1)
    fallback_models: list[str] | None = None
    reasoning_effort: str | None = Field(default=None, pattern=r"^(minimal|low|medium|high)$")   # GĐ9.3
    is_default: bool = False

class PresetUpd(BaseModel):
    name: str | None = Field(default=None, pattern=r"^[\w\-. ]{1,60}$")
    model: str | None = Field(default=None, min_length=1, max_length=120)
    provider: str | None = Field(default=None, max_length=32)
    provider_api: str | None = Field(default=None, pattern=r"^(chat|responses)$")
    extra_params: dict | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=65536)
    top_p: float | None = Field(default=None, ge=0, le=1)
    fallback_models: list[str] | None = None
    reasoning_effort: str | None = Field(default=None, pattern=r"^(minimal|low|medium|high)$")

class CloneIn(BaseModel):
    name: str | None = Field(default=None, pattern=r"^[\w\-. ]{1,60}$")

class TierIn(BaseModel):
    tier: str = Field(pattern=r"^(fast|pro|max)$")
    config_ref: str | None = Field(default=None, max_length=120)   # None = back to the built-in model

PRESET_SQL = "SELECT m.*, p.name AS provider FROM model_presets m LEFT JOIN gw_providers p ON p.id=m.provider_id"

def _cfg_admin():
    u = admin.cur()
    if not u or u.get("role") != "admin" or u.get("demo"): raise HTTPException(403, "Chỉ admin đổi mức mô hình")
    return u

async def _cfg_target(model, provider, ws):
    """-> (provider_id, upstream model, canonical ref) of a configuration's target; 400 for an unknown provider / aos/ / custom/."""
    import gw_router, gateway
    ref = f"{provider}/{model}" if provider else model
    if ref.startswith(("aos/", "custom/")): raise HTTPException(400, "Cấu hình không thể trỏ tới aos/ hoặc custom/")
    r = await gw_router.aresolve(ref, ws)
    if gateway.gemini_only() and r.kind != "gemini": raise HTTPException(403, "Workspace demo chỉ dùng Gemini")
    return (r.row or {}).get("id"), r.model, r.ref

async def _cfg_refresh(ws):
    import gw_router
    gw_router.invalidate(ws); await gw_router.rows_for(ws, fresh=True)

async def _cfg_one(pid):
    r = await A.q(PRESET_SQL + " WHERE m.id=%s", (pid,), one=True)
    if not r: raise HTTPException(404, "Model preset not found")
    return r

async def _cfg_default(pid):
    await A.q("UPDATE model_presets SET is_default=false WHERE is_default AND id<>%s", (pid,))
    await A.q("UPDATE model_presets SET is_default=true WHERE id=%s", (pid,))

async def _cfg_save(b: PresetIn, upsert: bool):
    ws = _ws(); pid, up, ref = await _cfg_target(b.model, b.provider, ws)
    if not upsert and await A.q("SELECT 1 FROM model_presets WHERE name=%s", (b.name,), one=True): raise HTTPException(409, "Tên cấu hình đã tồn tại")
    ex = Jsonb(b.extra_params) if b.extra_params is not None else None
    row = await A.q("""INSERT INTO model_presets(name,model,temperature,max_tokens,top_p,fallback_models,reasoning_effort,provider_id,upstream_model,provider_api,extra_params,created_by)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(workspace_id,name) DO UPDATE SET model=EXCLUDED.model,temperature=EXCLUDED.temperature,max_tokens=EXCLUDED.max_tokens,top_p=EXCLUDED.top_p,
        fallback_models=EXCLUDED.fallback_models,reasoning_effort=EXCLUDED.reasoning_effort,provider_id=EXCLUDED.provider_id,upstream_model=EXCLUDED.upstream_model,
        provider_api=EXCLUDED.provider_api,extra_params=EXCLUDED.extra_params RETURNING id""",
        (b.name, ref, b.temperature, b.max_tokens, b.top_p, b.fallback_models, b.reasoning_effort, pid, up, b.provider_api, ex, _u().get("username")), one=True)
    if b.is_default: await _cfg_default(row["id"])
    await _cfg_refresh(ws); return await _cfg_one(row["id"])

@router.post("/api/model_presets")
async def preset_set(b: PresetIn): return await _cfg_save(b, True)   # legacy alias: upsert by name, as before

@router.post("/api/model_configs")
async def config_create(b: PresetIn): return await _cfg_save(b, False)

@router.get("/api/model_presets")
@router.get("/api/model_configs")
async def preset_list(): return await A.q(PRESET_SQL + " ORDER BY m.name")

@router.patch("/api/model_configs/{pid}")
async def config_patch(pid: int, b: PresetUpd):
    cur = await _cfg_one(pid); d = b.model_dump(exclude_unset=True); ws = _ws()
    if "name" in d and d["name"] != cur["name"] and await A.q("SELECT 1 FROM model_presets WHERE name=%s", (d["name"],), one=True): raise HTTPException(409, "Tên cấu hình đã tồn tại")
    if "model" in d or "provider" in d:
        prov = d["provider"] if "provider" in d else cur.get("provider")
        model = d.get("model") or cur.get("upstream_model") or cur["model"]
        d["provider_id"], d["upstream_model"], d["model"] = await _cfg_target(model, prov, ws)
    d.pop("provider", None)
    if d.get("extra_params") is not None: d["extra_params"] = Jsonb(d["extra_params"])
    if d:
        cols = list(d); await A.q(f"UPDATE model_presets SET {', '.join(c + '=%s' for c in cols)} WHERE id=%s", [d[c] for c in cols] + [pid])
    await _cfg_refresh(ws); return await _cfg_one(pid)

@router.post("/api/model_configs/{pid}/clone")
async def config_clone(pid: int, b: CloneIn | None = None):
    cur = await _cfg_one(pid); base = (b.name if b and b.name else f"{cur['name']} copy")[:55]; name = base; i = 2
    while await A.q("SELECT 1 FROM model_presets WHERE name=%s", (name,), one=True): name = f"{base} {i}"; i += 1
    row = await A.q("""INSERT INTO model_presets(name,model,temperature,max_tokens,top_p,fallback_models,reasoning_effort,provider_id,upstream_model,provider_api,extra_params,created_by)
        SELECT %s,model,temperature,max_tokens,top_p,fallback_models,reasoning_effort,provider_id,upstream_model,provider_api,extra_params,%s FROM model_presets WHERE id=%s RETURNING id""",
        (name, _u().get("username"), pid), one=True)
    await _cfg_refresh(_ws()); return await _cfg_one(row["id"])

@router.put("/api/model_configs/{pid}/default")
async def config_default(pid: int):
    await _cfg_one(pid); await _cfg_default(pid); return await _cfg_one(pid)

@router.delete("/api/model_presets/{pid}")
@router.delete("/api/model_configs/{pid}")
async def preset_del(pid: int):
    ws = _ws()
    t = await A.q("SELECT m.tier FROM model_tiers m JOIN model_presets p ON m.config_ref='custom/'||p.name WHERE p.id=%s", (pid,), one=True)
    if t: raise HTTPException(409, f"Cấu hình đang được dùng cho mức aos/{t['tier']}")
    await A.q("DELETE FROM model_presets WHERE id=%s", (pid,)); await _cfg_refresh(ws); return {"ok": True}

@router.get("/api/model_tiers")
async def tiers_get():
    import gw_router
    got = {r["tier"]: r["config_ref"] for r in await A.q("SELECT tier, config_ref FROM model_tiers")}
    return {"tiers": [{"tier": t, "default": d, "config_ref": got.get(t), "effective": (await gw_router.aresolve("aos/" + t, _ws())).ref} for t, d in gw_router.TIERS.items()]}

@router.put("/api/model_tiers")
async def tiers_put(b: TierIn):
    import gw_router
    u = _cfg_admin(); ws = _ws()
    if b.config_ref is None: await A.q("DELETE FROM model_tiers WHERE tier=%s", (b.tier,))
    else:
        if b.config_ref.startswith("aos/"): raise HTTPException(400, "Mức không thể trỏ tới mức khác")
        await gw_router.aresolve(b.config_ref, ws)   # 400 if it does not resolve
        await A.q("""INSERT INTO model_tiers(tier,config_ref,updated_by) VALUES(%s,%s,%s) ON CONFLICT(workspace_id,tier) DO UPDATE SET config_ref=EXCLUDED.config_ref,
            updated_by=EXCLUDED.updated_by, updated_at=now()""", (b.tier, b.config_ref, u.get("username")))
    await _cfg_refresh(ws); return await tiers_get()

class PlayChat(BaseModel):
    messages: list[dict] = Field(min_length=1)
    variables: dict = {}
    template_format: str = Field(default="mustache", pattern=r"^(mustache|f-string|jinja2)$")
    model: str | None = None
    preset: str | None = None
    temperature: float | None = None
    tools: list[dict] | None = None
    tool_choice: str | dict | None = None
    response_format: dict | None = None

@router.post("/api/playground/chat")
async def play_chat(b: PlayChat):
    import gw_api as G
    import gw_router, gw_features
    pr = await A.q("SELECT * FROM model_presets WHERE name=%s", (b.preset,), one=True) if b.preset else None
    if b.preset and not pr: raise HTTPException(404, "Model preset not found")
    dflt = None if (b.model or pr) else await A.q("SELECT * FROM model_presets WHERE is_default", one=True)   # GĐ11.11 the workspace's default configuration
    pr = pr or dflt
    model = b.model or (pr or {}).get("model") or A.DEFAULT_MODEL
    r = await gw_router.aresolve(model, _ws())   # GĐ11.11: custom/<name>, aos/<tier>, provider/model all resolve (Playground still runs Gemini only; other providers = 11.3)
    if r.params and model.startswith("custom/"): pr = {**(pr or {}), **r.params}
    model = r.model
    if r.kind != "gemini" or not re.fullmatch(r"gemini-[\w.\-]+", model): raise HTTPException(400, "Only Gemini models are supported")
    if not await gw_features.allowed("playground", b.model or (pr or {}).get("model") or model): raise HTTPException(403, "Mô hình này không được phép dùng ở Playground")
    msgs = []
    for m in b.messages:
        c = m.get("content")
        if isinstance(c, str): c = await render_async(c, b.variables, b.template_format)
        elif isinstance(c, list): c = [({**x, "text": await render_async(x.get("text", ""), b.variables, b.template_format)} if x.get("type") == "text" else x) for x in c]
        msgs.append({**m, "content": c})
    body = {"messages": msgs, "temperature": b.temperature if b.temperature is not None else (pr or {}).get("temperature"), "max_tokens": (pr or {}).get("max_tokens"),
            "top_p": (pr or {}).get("top_p"), "tools": b.tools, "tool_choice": b.tool_choice, "response_format": b.response_format}
    body = {k: v for k, v in body.items() if v is not None}
    ids = [tc.get("id") for m in msgs for tc in (m.get("tool_calls") or []) if tc.get("id")]
    sigs = {r["call_id"]: bytes(r["sig"]) for r in await A.q("SELECT call_id, sig FROM gw_tool_sigs WHERE call_id = ANY(%s)", (ids,))} if ids else {}
    pchain = list(dict.fromkeys([model] + list((pr or {}).get("fallback_models") or []) + list(A.gw.DEFAULT_FALLBACKS)))
    try: await A.gw.budget_check(A.gw.ctx_now("gemini", feature="playground", models=pchain))   # unified spend caps (GĐ6.1) + GĐ11.14 model access (403 outside the list)
    except A.gw.BudgetExceeded as e: raise HTTPException(402, str(e)) from None
    pe_ = __import__("gw_policy_engine"); pchain = [m for m in pchain if pe_.model_ok(m, None, "playground")]   # fallbacks outside the list are never tried
    contents, cfg = G.to_gemini(body, sigs); t0 = time.perf_counter(); r = None; last = None
    for m in pchain:
        try: r = await A._g.aio.models.generate_content(model=m, contents=contents, config=cfg); break
        except Exception as e: last = e  # noqa: BLE001
    if r is None: raise HTTPException(400, f"Model error: {str(last)[:300]}")
    i, o = G._usage(r); so = {}; msg, finish = G._msg_from(r, so); await G._save_sigs(so)
    ms = int((time.perf_counter() - t0) * 1000); rid = str(uuid.uuid4())
    await obs.upsert_run(await obs.project_id("playground"), {"id": rid, "name": f"playground chat · {m}", "run_type": "llm", "start_time": time.time() - ms / 1000, "end_time": time.time(),
                         "inputs": {"messages": [{**x, "content": x["content"] if isinstance(x.get("content"), str) else "[multimodal]"} for x in msgs]}, "outputs": {"message": msg},
                         "model": m, "usage": {"input_tokens": i, "output_tokens": o}, "tags": ["playground"] + (["tools"] if b.tools else []) + ([f"preset:{b.preset}"] if b.preset else [])})
    return {"model": m, "message": msg, "finish_reason": finish, "input_tokens": i, "output_tokens": o, "cost_usd": A.gw.cost(m, i, o), "ms": ms, "run_id": rid}

# ================================================================ Re-embed Store vectors (after the batch-embedding fix)
@router.post("/api/admin/store/reembed")
async def store_reembed(dry_run: bool = False):
    _admin_only()
    import numpy as np
    def cos(a, b): a, b = np.array(a, dtype=float), np.array(b, dtype=float); return round(float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b))), 4)
    items = await A.q("SELECT prefix, key, value FROM store ORDER BY prefix, key"); out = []
    for it in items:
        val = it["value"]; text = val.get("text") if isinstance(val, dict) else None
        if not text: out.append({"ns": it["prefix"], "key": it["key"], "skipped": "no text field"}); continue
        old = await A.q("SELECT embedding::text e FROM store_vectors WHERE prefix=%s AND key=%s AND field_name='text'", (it["prefix"], it["key"]), one=True)
        fresh = (await A.aembed([text]))[0]
        before = cos(json.loads(old["e"]), fresh) if old and old["e"] else None
        if not dry_run: await A.S["store"].aput(tuple(it["prefix"].split(".")), it["key"], val)
        new = await A.q("SELECT embedding::text e FROM store_vectors WHERE prefix=%s AND key=%s AND field_name='text'", (it["prefix"], it["key"]), one=True)
        out.append({"ns": it["prefix"], "key": it["key"], "cos_old_vs_fresh": before, "cos_new_vs_fresh": cos(json.loads(new["e"]), fresh)})
    return {"items": len(items), "dry_run": dry_run, "results": out}

import gw_features as _gwf; router.include_router(_gwf.router)   # GĐ11.12 /api/gw/features, /api/gw/catalog, /api/model_feature_access
