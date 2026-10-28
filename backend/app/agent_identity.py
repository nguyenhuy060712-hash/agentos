"""GĐ9.Q P4 — agent identity + auth-aware tool responses (LangSmith Fleet: "Agent identity", "Auth-aware tool responses").

Agent identity = whose credentials an agent uses, per agent:
  mode "user"  (default)  the agent acts with the connected accounts (Settings > Connections) of the user who is chatting — audit trail = that user;
  mode "agent"            the agent acts with the FIXED credentials of its owner (the person who delegated them): it can serve channels / schedules / API callers.
Only the agent owner or a workspace admin can switch to "agent" (they delegate their own accounts); every change is written to the agent audit log.
Service key: `POST /api/agents/{id}/service-key` creates a hidden service user (viewer, shared `use` on this agent only) with one `aos_…` key. The key can run
that agent and read its threads (+ MCP / A2A of the agent) and nothing else; a caller that holds it may send `X-On-Behalf-Of: <username>` so the audit says
"agent X acted on behalf of user Y" (a label, NOT a credential delegation).
Audit: every tool call of an agent is recorded in `agent_audit` (acted_as agent|user, principal, on_behalf_of, credential user, ok) -> GET /api/agents/{id}/audit.
Auth-aware tool responses (Fleet format): a tool that returns the JSON `{"auth_required": true, "auth_url": "...", "auth_id": "..."}` pauses the run with an
`auth_required` interrupt; when the client resumes (Command(resume=...)) the SAME tool call is retried once. Unattended callers (channels, schedules, service keys)
get a plain-text message instead of a pause. Tools can also read the caller (identity, role, scopes) — see `aos_whoami` and `aos_connect_check`."""
import json, re, secrets, time
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool as lc_tool
import caller

router = APIRouter(tags=["agent-identity"])
DDL = """
ALTER TABLE users ADD COLUMN IF NOT EXISTS service_agent_id INT;
CREATE TABLE IF NOT EXISTS agent_identity(agent_id INT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE, workspace_id INT NOT NULL DEFAULT 1, mode TEXT NOT NULL DEFAULT 'user'
  CHECK (mode IN ('user','agent')), service_user_id INT, updated TIMESTAMPTZ NOT NULL DEFAULT now(), updated_by INT);
CREATE TABLE IF NOT EXISTS agent_audit(id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL DEFAULT now(), workspace_id INT NOT NULL DEFAULT 1, agent_id INT, run_ref TEXT,
  action TEXT NOT NULL, tool TEXT, acted_as TEXT, principal_id INT, principal TEXT, on_behalf_of TEXT, credential_user_id INT, ok BOOLEAN NOT NULL DEFAULT true, detail JSONB NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS agent_audit_agent ON agent_audit(agent_id, id DESC);
"""
async def _q(*a, **k):
    import deepagent_api as A
    return await A.q(*a, **k)

async def init():
    await _q(DDL)
    import admin
    for t in ("agent_identity", "agent_audit", "auth_sessions"):   # auth_sessions: GĐ11.27 (migration 0024)
        if t not in admin.RLS_DIRECT: admin.RLS_DIRECT.append(t)

# ---------------------------------------------------------------- identity mode
_MODE: dict = {}
async def mode_of(agent_id) -> str:
    if not agent_id: return "user"
    e = _MODE.get(agent_id)
    if e and e[0] > time.monotonic(): return e[1]
    r = await _q("SELECT mode FROM agent_identity WHERE agent_id=%s", (agent_id,), one=True)
    m = (r or {}).get("mode") or "user"; _MODE[agent_id] = (time.monotonic() + 5, m); return m

_OWN: dict = {}
async def owner_id_of(agent_id):
    e = _OWN.get(agent_id)
    if e and e[0] > time.monotonic(): return e[1]
    r = await _q("SELECT owner_id FROM agents WHERE id=%s", (agent_id,), one=True)
    o = (r or {}).get("owner_id"); _OWN[agent_id] = (time.monotonic() + 30, o); return o

async def credential_user(agent_id, user: dict | None) -> tuple[dict | None, str]:
    """(user whose connected accounts the tool uses, acted_as). Mode 'agent' -> the agent owner as a synthetic identity (never marked 'channel')."""
    if await mode_of(agent_id) != "agent": return user, "user"
    r = await _q("SELECT a.owner_id, a.workspace_id, u.username FROM agents a LEFT JOIN users u ON u.id=a.owner_id WHERE a.id=%s", (agent_id,), one=True)
    if not r or not r["owner_id"]: return None, "agent"
    import admin
    if admin.is_demo(user): return None, "agent"       # a demo (trial) account never borrows the owner's connected accounts
    return {"id": r["owner_id"], "username": r["username"], "ws": r["workspace_id"], "role": "editor", "auth": "agent_identity"}, "agent"

async def audit(action, *, agent_id=None, tool=None, acted_as=None, ok=True, detail=None, cred_user=None, c=None):
    c = c or caller.current()
    try:
        obo = c.get("on_behalf_of")
        await _q("INSERT INTO agent_audit(workspace_id,agent_id,action,tool,acted_as,principal_id,principal,on_behalf_of,credential_user_id,ok,detail) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                 (c.get("ws") or 1, agent_id, action, tool, acted_as, c.get("user_id"), c.get("username"), (obo or {}).get("username") if isinstance(obo, dict) else obo, cred_user,
                  ok, Jsonb(detail or {})))
    except Exception as e: print("agent audit error", e)  # noqa: BLE001

# ---------------------------------------------------------------- the middleware (per agent): audit + auth-aware tool responses
QUIET = {"write_todos", "ls", "read_file", "glob", "grep", "write_file", "edit_file", "task"}   # internal / filesystem tools are not worth an audit row each
def parse_auth_required(content) -> dict | None:
    """The Fleet format: the whole tool output is a JSON object with auth_required=true (anything else is a normal result)."""
    if isinstance(content, list): content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    if not isinstance(content, str) or '"auth_required"' not in content[:4000]: return None
    try: j = json.loads(content)
    except ValueError: return None
    if not isinstance(j, dict) or j.get("auth_required") is not True: return None
    url = str(j.get("auth_url") or "")[:2000]
    if url and (not re.match(r"^(https://[^\s\\]+|http://(127\.0\.0\.1|localhost)(:\d+)?/\S*|/(?![/\\])\S*)$", url) or re.search(r"[\x00-\x1f]", url)): url = ""      # never hand the user a javascript:/data:/protocol-relative link
    return {"auth_url": url, "auth_id": str(j.get("auth_id") or "")[:200], "message": str(j.get("message") or "")[:500]}

def is_auth_interrupt(v) -> bool: return isinstance(v, dict) and v.get("type") in ("auth_required", "credential_authorization_required")   # both are answered in chat (ConnectCard), never an Inbox approval
INTERACTIVE = {"session", "user", "basic", "api_key", "custom_auth", "oidc_token"}

def _tool_name(t):
    return getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)

async def _channel_can_connect() -> bool:
    """GĐ12.78: the run belongs to a channel binding whose adapter can show a sign-in button (`send_connect`); other channels keep the plain sentence (nobody could answer a pause)."""
    try:
        import admin, channels
        m = re.match(r"^b(\d+):", str((admin.cur() or {}).get("chat") or ""))
        b = m and await _q("SELECT kind FROM channel_bindings WHERE id=%s", (int(m.group(1)),), one=True)
        return bool(b and getattr(channels.ADAPTERS.get(b["kind"]), "send_connect", None))
    except Exception: return False   # noqa: BLE001

class ConnectionGateMiddleware(AgentMiddleware):
    """GĐ11.27 pre-run gate: before the FIRST model call of a run started by a person (the last message is theirs), list the sign-ins that person still has to make for the OAuth MCP
    servers whose tools the model is about to be offered, and pause ONCE with `{"type": "credential_authorization_required", "credentials": [{slug, kind, connect_url, auth_id, ...}]}`.
    The client resumes with `{"type": "credential_authorization_completed", "connected_slugs": [...]}` (what it claims is NOT trusted: the store is read again; resuming early pauses again,
    at most MAX_REPAUSE times) or with `{"skip": true}` / `{"cancel": true}` (the run goes on; a tool that still lacks its sign-in answers by itself). Unattended runs (schedule, channel,
    service key, API without a person) never pause: the tool answers in plain text. Only connections the PERSON owns count: an agent's own account / the owner's account are not theirs to fix."""
    MAX_REPAUSE = 3
    def __init__(self, agent_id):
        super().__init__(); self.agent_id = agent_id
    async def awrap_model_call(self, request, handler):
        msgs = list(getattr(request, "messages", None) or [])
        if msgs and getattr(msgs[-1], "type", None) == "human": await self._gate(request)
        return await handler(request)
    async def _gate(self, request):
        c = caller.current()
        if c.get("kind") not in INTERACTIVE or c.get("agent_id") is not None: return
        import admin, connection_runtime as R
        user = admin.cur()
        names = [n for n in (_tool_name(t) for t in (getattr(request, "tools", None) or [])) if n]
        missing = await R.missing_for(user, self.agent_id, names)
        if not missing: return
        from langgraph.types import interrupt
        await audit("credential_gate", agent_id=self.agent_id, ok=True, acted_as="user", detail={"slugs": [d["slug"] for d, _r in missing]}, c=c)
        for _ in range(self.MAX_REPAUSE):
            ans = interrupt(await R.gate_payload(user, missing))
            if isinstance(ans, dict) and (ans.get("skip") or ans.get("cancel")): return
            missing = await R.missing_for(user, self.agent_id, names)
            if not missing: return

class IdentityMiddleware(ConnectionGateMiddleware):
    async def awrap_tool_call(self, request, handler):
        import connection_runtime as R
        name = request.tool_call.get("name"); c = caller.current(); ok = True; detail = {}
        tok = R.CUR_AGENT.set(self.agent_id)   # the MCP wrapper needs to know which agent is calling (agent's own account / identity mode)
        try:
            msg = await handler(request)
            ar = parse_auth_required(getattr(msg, "content", None)) if isinstance(msg, ToolMessage) else None
            if ar:
                detail = {"auth_required": True, "auth_url": ar["auth_url"]}
                msg = await self._auth_flow(request, handler, msg, ar, c)
                ok = parse_auth_required(getattr(msg, "content", None)) is None
            return msg
        except BaseException as e:
            ok = False
            if type(e).__name__ == "GraphInterrupt": ok = True; detail = {**detail, "paused": True}
            raise
        finally:
            R.CUR_AGENT.reset(tok)
            if name not in QUIET:
                m = await mode_of(self.agent_id)
                acted = "agent" if (m == "agent" or c.get("kind") == "agent_key") else "user"
                await audit("tool_call", agent_id=self.agent_id, tool=name, acted_as=acted, ok=ok, detail={**detail, "mode": m, "kind": c.get("kind")}, c=c, cred_user=(await owner_id_of(self.agent_id)) if m == "agent" else c.get("user_id"))
    async def _auth_flow(self, request, handler, msg, ar, c):
        tc = request.tool_call; text = f"Authorization required for tool {tc.get('name')}: " + (ar["auth_url"] or "open Settings > Connections") + (f" ({ar['message']})" if ar["message"] else "")
        if (c.get("kind") not in INTERACTIVE and not (c.get("kind") == "channel" and await _channel_can_connect())) or c.get("agent_id") is not None:     # nobody to ask: say what is missing (GĐ12.78: a channel whose adapter can post a Connect button CAN wait)
            return ToolMessage(content=text + ". This run is unattended, so it cannot wait for the user.", tool_call_id=tc["id"], name=tc.get("name"), status="error")
        from langgraph.types import interrupt
        ans = interrupt({"type": "auth_required", "tool": tc.get("name"), "tool_call_id": tc["id"], **ar})
        if isinstance(ans, dict) and (ans.get("cancel") or ans.get("authorized") is False):
            return ToolMessage(content="The user declined to authorize: " + (ar["auth_url"] or "connection"), tool_call_id=tc["id"], name=tc.get("name"), status="error")
        again = await handler(request)                                         # retried ONCE after the user came back
        if parse_auth_required(getattr(again, "content", None)):
            return ToolMessage(content=text + ". Authorization is still missing.", tool_call_id=tc["id"], name=tc.get("name"), status="error")
        return again

# ---------------------------------------------------------------- tools that READ the caller (template for tool authors)
def tools(agent_id=None) -> dict:
    @lc_tool
    async def aos_whoami(require_scope: str = "", config: RunnableConfig = None) -> str:
        """Who is calling: identity, role, scopes, acted-as and on-behalf-of. With require_scope (e.g. 'perm:billing' or 'role:admin') the tool REFUSES when the caller lacks it."""
        c = caller.current(); sc = caller.scopes_of(c); m = await mode_of(agent_id)
        cfg = ((config or {}).get("configurable") or {}); hdrs = {k: v for k, v in cfg.items() if isinstance(k, str) and k.startswith("x-")}
        import admin
        cu, _ = await credential_user(agent_id, admin.cur()); creds = (cu or {}).get("username")
        if require_scope and require_scope not in sc:
            return json.dumps({"refused": True, "reason": f"missing scope {require_scope}", "caller": c.get("username"), "scopes": sc}, ensure_ascii=False)
        return json.dumps({"identity": (c.get("auth_user") or {}).get("identity") or c.get("username"), "user": c.get("username"), "role": c.get("role"), "kind": c.get("kind"), "scopes": sc,
                           "acted_as": "agent" if (m == "agent" or c.get("kind") == "agent_key") else "user", "on_behalf_of": c.get("on_behalf_of"), "identity_mode": m,
                           "headers": hdrs, "auth_user": cfg.get("langgraph_auth_user"), "credentials_of": creds}, ensure_ascii=False)
    @lc_tool
    async def aos_connect_check(service: str = "demo") -> str:
        """Example of an auth-aware tool: answers only when the user has connected `service`; otherwise returns the Fleet auth_required JSON (the run pauses, then the call is retried)."""
        import srv_parity
        c = caller.current(); key = f"connected:{service}:{c.get('user_id')}"
        if await srv_parity.cache_get(key, ws=c.get("ws") or 1) is None:
            return json.dumps({"auth_required": True, "auth_url": f"/settings?s=connections&service={re.sub(r'[^a-z0-9_-]', '', service.lower())[:40]}", "auth_id": secrets.token_hex(6),
                               "message": f"connect {service}"})
        return f"{service} is connected for {c.get('username')}"
    return {"local:aos_whoami": aos_whoami, "local:aos_connect_check": aos_connect_check}

# ---------------------------------------------------------------- service key + on-behalf-of (authz hook)
_SVC: dict = {}
async def service_agent_of(uid) -> int | None:
    e = _SVC.get(uid)
    if e and e[0] > time.monotonic(): return e[1]
    r = await _q("SELECT service_agent_id FROM users WHERE id=%s", (uid,), one=True)
    a = (r or {}).get("service_agent_id"); _SVC[uid] = (time.monotonic() + 30, a); return a

SVC_OK = (re.compile(r"^/api/v1/(info|ok)$"), re.compile(r"^/api/v1/assistants/(search|count)$"), re.compile(r"^/api/v1/assistants/[0-9a-fA-F-]{36}(/schemas)?$"),
          re.compile(r"^/api/v1/threads(/[^/]+(/(state|history|runs(/[^/]+(/(wait|stream|join|cancel))?)?|stream))?|/search|/count)?$"),
          re.compile(r"^/(mcp/agents|a2a)/\d+"))
async def enrich_and_restrict(request: Request, u: dict):
    """Called for every authenticated request: tags agent-key callers and enforces what a service key may touch. -> JSONResponse | None."""
    from fastapi.responses import JSONResponse
    if not str(u.get("auth") or "").startswith("api_key") or not u.get("id"): return None
    aid = await service_agent_of(u["id"])
    if aid is None: return None
    path = request.url.path
    if not any(rx.match(path) for rx in SVC_OK):
        return JSONResponse({"detail": "This is an agent service key: it can only run its agent and read its threads"}, 403)
    ex = {"kind": "agent_key", "agent_id": aid}
    obo = request.headers.get("x-on-behalf-of")
    if obo:
        t = await _q("SELECT u.id, u.username FROM users u JOIN memberships m ON m.user_id=u.id AND m.workspace_id=%s WHERE u.username=%s AND NOT u.disabled AND u.service_agent_id IS NULL",
                     (u.get("ws") or 1, obo[:80]), one=True)
        if not t: return JSONResponse({"detail": "X-On-Behalf-Of: unknown user in this workspace"}, 403)
        ex["on_behalf_of"] = {"user_id": t["id"], "username": t["username"]}
    caller.EXTRA.set(ex); return None

# ---------------------------------------------------------------- API
@router.get("/api/agent-auth/sessions/{auth_id}")
async def auth_session_get(auth_id: str, wait_seconds: float = 0):
    """Long poll for the connect card: {auth_id, slug, status pending|completed|failed|expired}; only the person the session was made for (404 for anyone else / another workspace)."""
    import connection_runtime as R
    return await R.session_get(auth_id, wait_seconds)

@router.get("/api/agent-auth/sessions/{auth_id}/start")
async def auth_session_start(auth_id: str, request: Request):
    """The link a paused run hands out (`auth_url` / `connect_url`): opens the provider's sign-in for this session (302). Same person only."""
    import connection_runtime as R
    return await R.session_start(auth_id, request)

class IdentityIn(BaseModel):
    mode: str = Field(pattern=r"^(user|agent)$")

async def _agent(aid):
    import agent_sharing
    x = await agent_sharing.access(aid)
    return x

@router.get("/api/agents/{aid}/identity")
async def identity_get(aid: int):
    import agent_sharing
    x = await agent_sharing.require_use(aid)
    r = await _q("SELECT i.mode, i.updated, i.service_user_id FROM agent_identity i WHERE i.agent_id=%s", (aid,), one=True) or {"mode": "user", "service_user_id": None}
    key = await _q("SELECT k.prefix, k.created, k.last_used FROM user_api_keys k WHERE k.user_id=%s AND NOT k.revoked ORDER BY k.id DESC LIMIT 1", (r["service_user_id"],), one=True) if r.get("service_user_id") else None
    return {"agent_id": aid, "mode": r["mode"], "service_key": key, "can_change": bool(x["owner"] or x["admin"]), "can_manage_key": x["edit"],
            "owner": (await _q("SELECT username FROM users WHERE id=%s", (x["agent"]["owner_id"],), one=True) or {}).get("username") if x["agent"]["owner_id"] else None}

@router.put("/api/agents/{aid}/identity")
async def identity_put(aid: int, b: IdentityIn):
    x = await _agent(aid)
    if not (x["owner"] or x["admin"]): raise HTTPException(403, "Only the agent owner or a workspace admin can change whose credentials the agent uses")
    if b.mode == "agent" and not x["agent"]["owner_id"]: raise HTTPException(409, "This agent has no owner whose fixed credentials it could use")
    import admin
    await _q("INSERT INTO agent_identity(agent_id,workspace_id,mode,updated_by) VALUES(%s,%s,%s,%s) ON CONFLICT(agent_id) DO UPDATE SET mode=EXCLUDED.mode, updated=now(), updated_by=EXCLUDED.updated_by",
             (aid, x["agent"]["workspace_id"], b.mode, (admin.cur() or {}).get("id")))
    _MODE.pop(aid, None)
    await audit("identity_changed", agent_id=aid, ok=True, detail={"mode": b.mode}, acted_as="user")
    return await identity_get(aid)

@router.post("/api/agents/{aid}/service-key")
async def service_key_new(aid: int):
    """Create (or rotate) the service key of this agent. The key is shown once."""
    x = await _agent(aid)
    if not (x["edit"] or x["admin"]): raise HTTPException(403, "You cannot manage this agent")
    import admin
    ws = x["agent"]["workspace_id"]
    r = await _q("SELECT service_user_id FROM agent_identity WHERE agent_id=%s", (aid,), one=True)
    uid = (r or {}).get("service_user_id")
    if not uid:
        u = await _q("INSERT INTO users(username,pw_hash,display_name,service_agent_id) VALUES(%s,%s,%s,%s) RETURNING id",
                     (f"svc.agent.{aid}.{secrets.token_hex(3)}", admin.hash_pw(secrets.token_urlsafe(32)), f"Agent {x['agent']['name'][:40]} (service)", aid), one=True)
        uid = u["id"]
        await _q("INSERT INTO memberships(user_id,workspace_id,role) VALUES(%s,%s,'viewer')", (uid, ws))
        await _q("INSERT INTO agent_shares(workspace_id,agent_id,principal_kind,principal_id,role,created_by) VALUES(%s,%s,'user',%s,'use',%s) ON CONFLICT DO NOTHING", (ws, aid, uid, (admin.cur() or {}).get("id")))
        await _q("INSERT INTO agent_identity(agent_id,workspace_id,service_user_id) VALUES(%s,%s,%s) ON CONFLICT(agent_id) DO UPDATE SET service_user_id=EXCLUDED.service_user_id", (aid, ws, uid))
    await _q("UPDATE user_api_keys SET revoked=true WHERE user_id=%s", (uid,))
    raw = "aos_" + secrets.token_urlsafe(32)
    await _q("INSERT INTO user_api_keys(user_id,workspace_id,name,prefix,key_hash,role,scope) VALUES(%s,%s,%s,%s,%s,'viewer','full')", (uid, ws, f"agent:{aid}", raw[:12], admin._h(raw)))
    _SVC.pop(uid, None)
    await audit("service_key_created", agent_id=aid, ok=True, detail={"prefix": raw[:12]}, acted_as="user")
    return {"agent_id": aid, "key": raw, "note": "Shown once. Use as X-Api-Key. It can only run this agent; add X-On-Behalf-Of: <username> to label the user you act for."}

@router.delete("/api/agents/{aid}/service-key")
async def service_key_del(aid: int):
    x = await _agent(aid)
    if not (x["edit"] or x["admin"]): raise HTTPException(403, "You cannot manage this agent")
    r = await _q("SELECT service_user_id FROM agent_identity WHERE agent_id=%s", (aid,), one=True)
    if r and r["service_user_id"]:
        await _q("UPDATE user_api_keys SET revoked=true WHERE user_id=%s", (r["service_user_id"],)); _SVC.pop(r["service_user_id"], None)
    await audit("service_key_revoked", agent_id=aid, ok=True, acted_as="user")
    return {"ok": True}

@router.get("/api/agents/{aid}/audit")
async def audit_list(aid: int, limit: int = 100, action: str | None = None):
    import agent_sharing
    await agent_sharing.require_edit(aid)
    rows = await _q("SELECT id, ts, action, tool, acted_as, principal, on_behalf_of, credential_user_id, ok, detail FROM agent_audit WHERE agent_id=%s AND (%s::text IS NULL OR action=%s) ORDER BY id DESC LIMIT %s",
                    (aid, action, action, max(1, min(limit, 500))))
    for r in rows:
        who = r["on_behalf_of"]
        r["text"] = (f"agent acted as {r['acted_as']}" if r["acted_as"] else r["action"]) + (f" · {r['tool']}" if r["tool"] else "") + f" · by {r['principal'] or 'system'}" + (f" on behalf of {who}" if who else "")
    return {"agent_id": aid, "audit": rows}
