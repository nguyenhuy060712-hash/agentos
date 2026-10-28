"""GĐ11.27 runtime auth per caller: the CALLER's credential on every MCP call, and the "needs connection" answer a paused run hands to the chat card (11.29).
- ConnectionAuth (an `httpx2.Auth`: fastmcp 4.1.3 talks httpx2, not httpx) puts `Authorization: Bearer <token>` on every request of ONE MCP call. The token comes from
  `connections.get_token(owner, slug, scopes)` (the single entry point, refresh under its lock). 401 -> force one refresh and retry; still 401 / refresh refused -> the connection becomes
  `needs_reconnect` and CredentialRequired is raised. 403 `insufficient_scope` -> union of the scopes asked so far and the challenged ones (`mcp.client.auth.utils.union_scopes`), at most
  MAX_STEPUPS times; a connection that was not granted them raises CredentialRequired(reason=scope_missing, missing_scopes) so the person reconnects with the wider scope.
- A MCP tool that needs a person's account builds a fresh client PER CALL (`call_tool`): a shared client would share one session -- and one token -- between two callers. Owner =
  `connections.resolve(caller, agent, slug)`: the agent's own account when it has one, else the caller's (the agent owner's in identity mode `agent`); demo and channel callers are
  refused there (a channel may only use the agent's account when the agent's identity mode is `agent`).
- Missing / dead credential -> `needs_connection()`: the Fleet JSON `{"auth_required": true, "auth_url", "auth_id", "message"}` (agent_identity.parse_auth_required turns it into an
  interrupt for a person at the keyboard, a plain sentence for unattended callers). `auth_url` is /api/agent-auth/sessions/{auth_id}/start: an `auth_sessions` row (migration 0024, bound to
  the person and workspace, 10 min) whose start link creates the normal one-time OAuth state and redirects to the provider; the callback marks the session completed / failed; the card long-polls
  GET /api/agent-auth/sessions/{auth_id}. A `secret` connection has no sign-in: the link opens Settings > Connections.
- Header forwarding: `X-AOS-Forward-<Name>` request headers of the run (only when listed in http.configurable_headers: they travel in the signed run metadata) go to an MCP server ONLY when
  its row has forward_headers=true AND the run is a person at the keyboard (never a channel / mail / schedule / service key), as `<Name>`; a use is written to agent_audit (names only).
  No SSO token is kept after sign-in (by design), so there is nothing to forward for mcp_servers.trusted_idp_forward: that flag is read but changes nothing yet (listed as left undone).
Every SQL names workspace_id; the session API answers 404 for another person's or another workspace's auth_id."""
import asyncio, contextvars, json, re, secrets, time

import httpx2
from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from langchain_core.tools import ToolException
from mcp.client.auth.utils import extract_scope_from_www_auth, union_scopes

import caller

CUR_AGENT: contextvars.ContextVar = contextvars.ContextVar("aos_run_agent", default=None)   # set by IdentityMiddleware around every tool call
SESSION_MIN = 10
MAX_STEPUPS = 2
MAX_WAIT_S = 30
FWD_PREFIX = "x-aos-forward-"
_FWD_DENY = {"authorization", "cookie", "set-cookie", "host", "content-length", "transfer-encoding", "connection", "proxy-authorization", "x-api-key", "upgrade"}
_FWD_NAME = re.compile(r"^[a-z0-9][a-z0-9\-]{0,60}$")
_RETURN_OK = re.compile(r"^/(?![/\\])[^\s\x00-\x1f]{0,300}$")

def _C():
    import connections
    return connections

def _A():
    import deepagent_api as A
    return A

# ------------------------------------------------------------------ errors travel inside fastmcp / anyio exception wrappers
def find_exc(exc, cls):
    """The first `cls` hiding anywhere in an exception chain / group (raised inside the auth flow, re-wrapped by httpx2, fastmcp and anyio), else None."""
    seen = set(); stack = [exc]
    while stack:
        e = stack.pop()
        if e is None or id(e) in seen: continue
        seen.add(id(e))
        if isinstance(e, cls): return e
        stack += [e.__cause__, e.__context__, *getattr(e, "exceptions", ())]
    return None

def find_credential_required(exc): return find_exc(exc, _C().CredentialRequired)

# ------------------------------------------------------------------ the httpx2 auth
class ConnectionAuth(httpx2.Auth):
    requires_response_body = False

    def __init__(self, owner, slug, scopes="", *, ws, user=None):
        self.owner, self.slug, self.scopes, self.ws, self.user = owner, slug, scopes or "", int(ws), user

    async def _token(self, *, force=False):
        C = _C()
        try: return await C.get_token(self.owner, self.slug, self.scopes, ws=self.ws, user=self.user, force_refresh=force)
        except C.CredentialRequired: raise
        except HTTPException as e:   # the refresh token endpoint said no (a definitive 400 / 401: the person must sign in again), or we could not ask / a config problem (passed on)
            code = getattr(e, "code", None)
            if code in ("unreachable", "egress_blocked") or e.status_code not in (400, 401): raise
            raise C.CredentialRequired(f"Kết nối '{self.slug}' hết hạn, cần kết nối lại / The connection '{self.slug}' expired: reconnect it", slug=self.slug, reason="needs_reconnect") from None

    async def async_auth_flow(self, request):
        C = _C(); tok = await self._token()
        request.headers["Authorization"] = f"Bearer {tok.value}"
        resp = yield request
        refreshed = False; steps = 0
        while True:
            if resp.status_code == 401:
                if refreshed:   # a freshly refreshed token was refused too: the server (or the person) revoked it
                    await mark_needs_reconnect(self.owner, self.slug, self.ws, "rejected_by_server")
                    raise C.CredentialRequired(f"Máy chủ từ chối tài khoản đã kết nối '{self.slug}', cần kết nối lại / The server rejected the connected account '{self.slug}': reconnect it",
                                               slug=self.slug, reason="needs_reconnect")
                refreshed = True
                tok = await self._token(force=True)
            elif resp.status_code == 403 and steps < MAX_STEPUPS and "insufficient_scope" in resp.headers.get("www-authenticate", ""):
                need = extract_scope_from_www_auth(resp)
                if not need: return
                steps += 1; self.scopes = union_scopes(self.scopes, need) or ""
                tok = await self._token()   # CredentialRequired(scope_missing, missing_scopes) when this connection was never granted them
            else: return
            request.headers["Authorization"] = f"Bearer {tok.value}"
            resp = yield request

async def mark_needs_reconnect(owner, slug, ws, reason):
    C, A = _C(), _A()
    try:
        d = await C._def(ws, slug); c = await C._cred(owner, d, ws) if d else None
        if c and c["kind"] == "oauth":
            await A.q("UPDATE oauth_connections SET status='needs_reconnect', status_reason=%s, updated=now() WHERE id=%s AND workspace_id=%s", (reason, c["id"], ws))
    except Exception: pass   # noqa: BLE001  (a bookkeeping failure must not hide the real answer)

# ------------------------------------------------------------------ sessions (the sign-in link of a paused run)
def _start_url(auth_id): return f"/api/agent-auth/sessions/{auth_id}/start"

async def create_session(user, ws, slug, owner, scope=None, return_to=None):
    """-> auth_id of a pending session for this person / connection / owner (an unexpired identical one is reused: a gate and a tool call must not pile up links)."""
    A = _A(); ot, oid = owner
    if return_to is not None and not _RETURN_OK.fullmatch(return_to): return_to = None
    r = await A.q("""SELECT auth_id FROM auth_sessions WHERE workspace_id=%s AND user_id=%s AND slug=%s AND owner_type=%s AND owner_id=%s AND status='pending' AND expires>now()
                     AND scope IS NOT DISTINCT FROM %s ORDER BY created DESC LIMIT 1""", (ws, user["id"], slug, ot, oid, scope or None), one=True)
    if r: return r["auth_id"]
    aid = secrets.token_urlsafe(24)
    await A.q("INSERT INTO auth_sessions(auth_id,workspace_id,user_id,owner_type,owner_id,slug,scope,return_to,expires) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,now()+make_interval(mins=>%s))",
              (aid, ws, user["id"], ot, oid, slug, scope or None, return_to, SESSION_MIN))
    return aid

def _may_connect(user, owner) -> bool:
    """A person connects their OWN account; the account of an agent / the workspace only an admin (same rule as POST /api/oauth/connect)."""
    if owner == ("user", user.get("id")): return True
    return owner[0] in ("agent", "workspace") and user.get("role") == "admin"

async def needs_connection(e, user, ws, owner, slug) -> str:
    """The Fleet `auth_required` JSON for a CredentialRequired. A sentence ("Error: ...") when this person can not connect that owner's account (e.g. the agent works with its
    owner's account) or no caller is known."""
    C = _C()
    if not user or not user.get("id") or e.reason == "no_caller": return f"Error: {e.detail}"
    d = await C._def(ws, slug)
    if d is None: return f"Error: {e.detail}"
    if d["kind"] == "secret":
        if not _may_connect(user, owner): return f"Error: {e.detail}"
        return json.dumps({"auth_required": True, "auth_url": f"/settings?s=connections&service={re.sub(r'[^A-Za-z0-9_.-]', '', d['slug'])[:64]}", "auth_id": "secret:" + d["slug"][:80],
                           "message": str(e.detail)[:400]}, ensure_ascii=False)
    if not d["provider_id"] or not _may_connect(user, owner):
        return f"Error: {e.detail} (tài khoản này không phải của bạn để kết nối / this account is not yours to connect)"
    scope = None
    if e.missing_scopes:   # step-up: what the connection has now + what the server asked for
        c = await C._cred(owner, d, ws)
        scope = " ".join(sorted(set((c or {}).get("scope", "").split()) | set(e.missing_scopes)))
    aid = await create_session(user, ws, d["slug"], owner, scope)
    return json.dumps({"auth_required": True, "auth_url": _start_url(aid), "auth_id": aid, "message": str(e.detail)[:400]}, ensure_ascii=False)

# ------------------------------------------------------------------ one MCP call with the caller's credential
def forward_headers(s, c, run_headers) -> dict:
    """X-AOS-Forward-<Name> headers of the run, only for a server with forward_headers=true and a person at the keyboard; sanitised; never a credential header."""
    from agent_identity import INTERACTIVE
    if not s.get("forward_headers") or c.get("kind") not in INTERACTIVE or c.get("agent_id") is not None: return {}
    out = {}
    for k, v in (run_headers or {}).items():
        k = str(k).lower()
        if not k.startswith(FWD_PREFIX): continue
        name = k[len(FWD_PREFIX):]
        if not _FWD_NAME.fullmatch(name) or name in _FWD_DENY or name.startswith(("x-aos-", "sec-")): continue
        v = str(v)
        if len(v) > 1000 or re.search(r"[\x00-\x1f\x7f]", v): continue
        out[name] = v
    return out

async def _context(s, user, ws):
    """-> (connection row, owner, credential user). Raises ToolException for a refused caller, CredentialRequired when the owner has no usable credential."""
    C, A = _C(), _A()
    if not user or not user.get("id"): raise ToolException("Không có người dùng đăng nhập cho lần chạy này / No signed-in caller for this run")
    if int(user.get("ws") or 1) != ws: raise ToolException("Máy chủ MCP không thuộc workspace của bạn / This MCP server is not in your workspace")
    d = await A.q("SELECT * FROM connections WHERE id=%s AND workspace_id=%s", (s.get("connection_id"), ws), one=True) if s.get("connection_id") else None
    if not d: raise ToolException(f"Máy chủ '{s['name']}' chưa có kết nối OAuth / The server has no OAuth connection set up")
    try: owner, cu = await C.resolve(user, CUR_AGENT.get(), d["slug"])
    except C.CredentialRequired: raise
    except HTTPException as e: raise ToolException(str(e.detail)) from None
    c = await C._cred(owner, d, ws)
    if not c or c["status"] == "revoked":
        ex = C.CredentialRequired(f"Chưa kết nối {d['display_name'] or d['slug']} / Not connected to {d['display_name'] or d['slug']}", slug=d["slug"]); ex.owner = owner
        raise ex
    return d, owner, cu

async def list_auth(s):
    """Credential used to LIST the tools of an OAuth server (no caller is acting for a person then): the signed-in admin's own account when they have one, else a client_credentials
    provider's workspace credential. None -> the saved tool list is used and the server shows "cần kết nối OAuth"."""
    import admin
    C, A = _C(), _A(); ws = int(s["workspace_id"]); u = admin.cur()
    d = await A.q("SELECT * FROM connections WHERE id=%s AND workspace_id=%s", (s.get("connection_id"), ws), one=True) if s.get("connection_id") else None
    if not d: return None
    try:
        if u and u.get("id") and int(u.get("ws") or 1) == ws and not admin.is_demo(u):
            owner, cu = await C.resolve(u, None, d["slug"]); c = await C._cred(owner, d, ws)
            if c and c["status"] != "revoked": return ConnectionAuth(owner, d["slug"], ws=ws, user=cu)
        pg = await A.q("SELECT grant_type FROM oauth_providers WHERE id=%s AND workspace_id=%s", (d["provider_id"], ws), one=True) if d["provider_id"] else None
        if pg and pg["grant_type"] == "client_credentials": return ConnectionAuth(("workspace", 0), d["slug"], ws=ws)
    except HTTPException: return None
    return None

async def call_tool(s, name, description, schema, kw):
    """Call MCP tool `name` of server row `s` with a client built for THIS call: the caller's credential and the forwarded headers, nothing shared with another caller."""
    import admin, mcp_registry as M
    from langchain.mcp import MCPAdapter, as_langchain_tool
    from mcp.types import Tool
    C = _C(); ws = int(s["workspace_id"]); user = admin.cur(); c = caller.current()
    auth = None; ctx = None
    try:
        if s.get("auth_type") == "oauth":
            d, owner, cu = await _context(s, user, ws); ctx = (d, owner)
            auth = ConnectionAuth(owner, d["slug"], ws=ws, user=cu)
        hdr = await M._headers(s)
        fwd = forward_headers(s, c, ((caller.RUN.get() or {}).get("headers")))
        if fwd:
            hdr = {**hdr, **fwd}
            import agent_identity
            await agent_identity.audit("forward_headers", agent_id=CUR_AGENT.get(), tool=name, acted_as="user", detail={"server": s["name"], "headers": sorted(fwd)}, c=c)
        target = M.make_target(s, hdr, auth) if auth is not None else M.make_target(s, hdr)
        adapter = MCPAdapter(target)
        tool = await as_langchain_tool(Tool(name=name, description=description or "", inputSchema=schema or {"type": "object"}), adapter.client)
        return await tool.ainvoke(kw)
    except BaseException as e:   # noqa: BLE001  (re-raised unless it is the "connect first" answer)
        ce = find_credential_required(e)
        if ce is None:
            he = find_exc(e, HTTPException)   # 403 / 404 / unreachable from the credential store or the token endpoint: a sentence for the model, not a crash
            if he is not None: raise ToolException(str(he.detail)[:400]) from None
            raise
        owner = getattr(ce, "owner", None) or (ctx[1] if ctx else ("user", (user or {}).get("id")))
        slug = ctx[0]["slug"] if ctx else (ce.slug or s["name"])
        return await needs_connection(ce, user, ws, owner, slug)

# ------------------------------------------------------------------ the pre-run gate: which sign-ins are missing before the first model call
async def required_connections(ws, agent_id, tool_names) -> list:
    """Connection rows (kind mcp_oauth / oauth2) of the OAuth MCP servers whose tools the model is about to be offered."""
    import mcp_registry as M
    names = set(tool_names); servers = {k.split(":", 1)[0] for k, mn in M.model_names(ws, agent_id).items() if mn in names}
    seen, out = set(), []
    for e in M._entries(ws, agent_id):
        cid = e.get("conn_id")
        if e["name"] in servers and e.get("auth") == "oauth" and cid and cid not in seen:
            seen.add(cid)
            d = await _A().q("SELECT * FROM connections WHERE id=%s AND workspace_id=%s", (cid, ws), one=True)
            if d: out.append(d)
    return out

async def missing_for(user, agent_id, tool_names) -> list:
    """-> [(connection row, reason)] for the connections THIS person must make: the owner resolves to the person's own account and it is absent / revoked / needs_reconnect."""
    C = _C()
    if not user or not user.get("id"): return []
    ws = int(user.get("ws") or 1); out = []
    for d in await required_connections(ws, agent_id, tool_names):
        try: owner, _cu = await C.resolve(user, agent_id, d["slug"])
        except HTTPException: continue            # refused (demo / channel / no owner): the tool call itself will say so
        if owner != ("user", user["id"]): continue   # the agent's own / the owner's account is not something this person can fix
        c = await C._cred(owner, d, ws)
        if not c or c["status"] == "revoked": out.append((d, "not_connected"))
        elif c["status"] == "needs_reconnect": out.append((d, "needs_reconnect"))
    return out

async def gate_payload(user, missing) -> dict:
    ws = int(user.get("ws") or 1); creds = []
    for d, reason in missing:
        aid = await create_session(user, ws, d["slug"], ("user", user["id"]))
        creds.append({"slug": d["slug"], "kind": d["kind"], "display_name": d["display_name"] or d["slug"], "reason": reason, "connect_url": _start_url(aid), "auth_id": aid})
    return {"type": "credential_authorization_required", "credentials": creds}

# ------------------------------------------------------------------ session API (registered on agent_identity.router)
async def session_start(auth_id, request):
    C, A = _C(), _A(); u = C._caller(); ws = int(u.get("ws") or 1)
    s = await A.q("SELECT * FROM auth_sessions WHERE auth_id=%s AND workspace_id=%s AND user_id=%s", (auth_id[:200], ws, u["id"]), one=True)
    if not s: raise HTTPException(404, "Unknown sign-in session")
    if s["status"] == "pending" and s["expires"].timestamp() < time.time():
        await A.q("UPDATE auth_sessions SET status='expired' WHERE auth_id=%s AND workspace_id=%s AND status='pending'", (auth_id, ws)); s["status"] = "expired"
    if s["status"] != "pending": raise HTTPException(410 if s["status"] == "expired" else 409, f"This sign-in is {s['status']}: ask the agent again")
    owner = (s["owner_type"], s["owner_id"])
    if not _may_connect(u, owner): raise HTTPException(403, "Admin only")
    d = await C._def(ws, s["slug"])
    p = await A.q("SELECT * FROM oauth_providers WHERE id=%s AND workspace_id=%s AND enabled", (d["provider_id"], ws), one=True) if d and d["provider_id"] else None
    if not p: raise HTTPException(404, "Provider not found or disabled")
    if owner[0] == "agent" and not await A.q("SELECT 1 FROM agents WHERE id=%s AND workspace_id=%s", (owner[1], ws), one=True): raise HTTPException(404, "Agent not found")
    import oauth_tools as O
    res = await O.begin_flow(u, ws, p, owner, s["scope"], request, auth_id=auth_id, next_url=s["return_to"] or "/settings?s=connections")
    return RedirectResponse(res["authorize_url"], 302, headers={"Cache-Control": "no-store"})

async def session_get(auth_id, wait_seconds=0.0):
    C, A = _C(), _A(); u = C._caller(); ws = int(u.get("ws") or 1)
    deadline = time.monotonic() + max(0.0, min(float(wait_seconds or 0), MAX_WAIT_S))
    while True:
        s = await A.q("SELECT auth_id, slug, status, expires, owner_type FROM auth_sessions WHERE auth_id=%s AND workspace_id=%s AND user_id=%s", (auth_id[:200], ws, u["id"]), one=True)
        if not s: raise HTTPException(404, "Unknown sign-in session")
        if s["status"] == "pending" and s["expires"].timestamp() < time.time():
            await A.q("UPDATE auth_sessions SET status='expired' WHERE auth_id=%s AND workspace_id=%s AND status='pending'", (auth_id, ws)); s["status"] = "expired"
        if s["status"] != "pending" or time.monotonic() >= deadline: return {"auth_id": s["auth_id"], "slug": s["slug"], "status": s["status"]}
        await asyncio.sleep(0.4)
