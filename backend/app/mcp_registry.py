"""GĐ11.23 MCP registry per workspace (replaces the global S["mcp_tools"]).
- Tables mcp_servers / tool_settings carry workspace_id (+ RLS, migration 0020). A server is workspace-wide (agent_id NULL) or private to one agent.
- Live state `_LIVE[server_id]` = {ws, agent_id, name, prefix, tools{name: tool}, status}. tools_for(ws, agent_id) -> {"server:tool": tool}; keys never change,
  the model-visible name is the bare tool name, or `{server}__{tool}` for prefix=true servers or when two servers expose the same tool name (model_names()).
- Connect: netguard.acheck(url, "mcp") at add and before every connect / call (the seeded `ha` row of workspace 1 is trusted infra: exempt).
  fastmcp uses httpx2, so the checked IP is not pinned (known residual, listed in the security package).
- token_env: superadmin only, must start with AOS_MCP_TOKEN_ (the `ha` row is exempt). Header values may only be {{secret:NAME}} references (ws_secrets).
- tools_cache keeps the listing (and calls fail with a clear message) while a server is down; TTL env AOS_MCP_CACHE_TTL_S (default 3600)."""
import asyncio, json, os, re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from langchain_core.tools import StructuredTool, ToolException
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

import netguard

router = APIRouter()
MAX_SERVERS = 50
TOKEN_PREFIX = "AOS_MCP_TOKEN_"
NAME_RE = r"^[a-z0-9_-]{2,30}$"
_SECRET_ONLY = re.compile(r"^\{\{\s*secret:[A-Za-z0-9_.\-]{1,64}\s*\}\}$")
_HDR_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_HDR_DENY = {"host", "content-length", "transfer-encoding", "connection"}
_LIVE: dict[int, dict] = {}

async def _q(*a, **k):
    import deepagent_api as A
    return await A.q(*a, **k)

def cur_ws() -> int:
    import admin
    u = admin.USER.get()
    if u: return int(u.get("ws") or 1)
    w = admin.WS_CTX.get()
    return int(w) if w else 1

def _ttl() -> int:
    try: return int(os.environ.get("AOS_MCP_CACHE_TTL_S", "3600"))
    except ValueError: return 3600

def _trusted(s) -> bool: return s["name"] == "ha" and int(s["workspace_id"]) == 1 and s.get("agent_id") is None

# ------------------------------------------------------------------ connect
def make_target(s, headers: dict, auth=None):
    """The fastmcp transport for one server row (tests replace this with an in-process FastMCP instance). `auth` = an httpx2.Auth (GĐ11.27 ConnectionAuth: the caller's token)."""
    from fastmcp.client.transports import SSETransport, StreamableHttpTransport
    return (SSETransport if s["transport"] == "sse" else StreamableHttpTransport)(s["url"], headers=headers, **({"auth": auth} if auth is not None else {}))

class NeedsAuth(RuntimeError):
    """An OAuth server could not be listed because nobody has a usable sign-in for it yet (not a failure: the saved tool list is used)."""

def _percall(s) -> bool:
    """GĐ11.27: tools of a server that needs the CALLER's credential (auth_type oauth) or forwards request headers get a client built per call (connection_runtime.call_tool)."""
    return s.get("auth_type") == "oauth" or bool(s.get("forward_headers"))

async def _egress(s):
    if _trusted(s): return
    try: await netguard.acheck(s["url"], "mcp")
    except netguard.NetBlocked as e: raise RuntimeError("địa chỉ bị chặn: " + e.reason) from None

async def _headers(s) -> dict:
    hdr = {}
    te = s.get("token_env")
    if te:
        if not (te.startswith(TOKEN_PREFIX) or _trusted(s)): raise RuntimeError("token_env không hợp lệ")
        if te not in os.environ: raise RuntimeError("chưa đặt biến môi trường " + te)
        hdr["Authorization"] = "Bearer " + os.environ[te]
    if s.get("headers"):
        import ws_secrets
        hdr.update(await ws_secrets.resolve_headers(dict(s["headers"]), s["url"], int(s["workspace_id"])))
    return hdr

async def _list_live(s):
    import warnings
    from langchain_core._api import LangChainBetaWarning
    warnings.filterwarnings("ignore", category=LangChainBetaWarning)
    from langchain.mcp import MCPAdapter
    await _egress(s)
    hdr = await _headers(s)
    auth = None
    if s.get("auth_type") == "oauth":   # GĐ11.27: listing needs SOME credential (the signed-in admin's own, or the workspace's client_credentials); none -> the saved list is used
        import connection_runtime as R
        auth = await R.list_auth(s)
        if auth is None: raise NeedsAuth("cần kết nối OAuth")
    tries = 2 if s.get("reconnect") else 1
    last = None
    for _ in range(tries):
        for variant in ([s, {**s, "transport": "sse"}] if s.get("sse_fallback") and s["transport"] != "sse" else [s]):
            try: return await asyncio.wait_for(MCPAdapter(make_target(variant, hdr, auth) if auth is not None else make_target(variant, hdr)).list_tools(), s.get("timeout_s") or 60)
            except Exception as e:  # noqa: BLE001
                last = e
    raise last

def _read_only(t) -> bool:
    ann = (((getattr(t, "metadata", None) or {}).get("mcp") or {}).get("tool") or {}).get("annotations") or {}
    return bool(ann.get("read_only_hint") or ann.get("readOnlyHint"))

def _describe(t) -> dict:
    return {"name": t.name, "description": (t.description or "")[:2000], "schema": t.args_schema if isinstance(t.args_schema, dict) else {},
            "read_only": _read_only(t)}

def _wrap_percall(s, name, description, schema, metadata=None):
    async def _run(**kw):
        try: await _egress(s)
        except RuntimeError as e: raise ToolException(str(e)) from None
        import connection_runtime as R
        return await R.call_tool(s, name, description, schema, kw)
    return StructuredTool.from_function(coroutine=_run, name=name, description=description or "", args_schema=schema, metadata=metadata, handle_tool_error=True)

def _wrap(s, t):
    if _percall(s): return _wrap_percall(s, t.name, t.description or "", t.args_schema, getattr(t, "metadata", None))
    async def _run(**kw):
        try: await _egress(s)
        except RuntimeError as e: raise ToolException(str(e)) from None
        return await t.ainvoke(kw)
    return StructuredTool.from_function(coroutine=_run, name=t.name, description=t.description or "", args_schema=t.args_schema,
                                        metadata=getattr(t, "metadata", None), handle_tool_error=True)

def _cached_tool(s, c):
    name = s["name"]
    if _percall(s):   # the saved list of an OAuth server: calls go live with the caller's own credential
        md = {"mcp": {"tool": {"annotations": {"read_only_hint": True}}}} if c.get("read_only") else None
        return _wrap_percall(s, c["name"], c.get("description") or "", c.get("schema") or {"type": "object", "properties": {}}, md)
    async def _run(**kw):
        raise ToolException(f"Máy chủ MCP '{name}' đang không truy cập được (đang dùng danh sách đã lưu). Thử lại sau.")
    md = {"mcp": {"tool": {"annotations": {"read_only_hint": True}}}} if c.get("read_only") else None
    return StructuredTool.from_function(coroutine=_run, name=c["name"], description=c.get("description") or "", args_schema=c.get("schema") or {"type": "object", "properties": {}},
                                        metadata=md, handle_tool_error=True)

def _keep(s, names):
    inc, exc = s.get("include_tools"), s.get("exclude_tools") or []
    return [n for n in names if (not inc or n in inc) and n not in exc]

def _fresh(s) -> bool:
    at = s.get("tools_cached_at")
    return bool(s.get("tools_cache") and at and (datetime.now(timezone.utc) - at).total_seconds() < _ttl())

async def _load_one(s):
    sid, ws = s["id"], int(s["workspace_id"])
    tools, status = {}, "ok"
    try:
        live = await _list_live(s)
        desc = [_describe(t) for t in live]
        by = {t.name: t for t in live}
        tools = {n: _wrap(s, by[n]) for n in _keep(s, by)}
        await _q("UPDATE mcp_servers SET tools_cache=%s, tools_cached_at=now(), last_error=NULL WHERE id=%s", (Jsonb(desc), sid))
        names = list(tools)
    except Exception as e:  # noqa: BLE001
        err = (str(e) or e.__class__.__name__)[:150]
        needs = isinstance(e, NeedsAuth)
        status = "cần kết nối OAuth" if needs else "lỗi: " + err
        if not needs:
            try: await _q("UPDATE mcp_servers SET last_error=%s WHERE id=%s", (err, sid))
            except Exception: pass  # noqa: BLE001
        if s.get("tools_cache") and needs or (not s.get("throw_on_load_error") and _fresh(s)):
            cmap = {c["name"]: c for c in s["tools_cache"]}
            tools = {n: _cached_tool(s, cmap[n]) for n in _keep(s, cmap)}
            status += " (dùng danh sách đã lưu)"
        names = list(tools)
    if names:
        ha = _trusted(s)
        from deepagent_api import is_ha_read
        await _q("INSERT INTO tool_settings(workspace_id,server,tool,enabled) SELECT %s,%s,t,e FROM unnest(%s::text[], %s::bool[]) AS u(t,e) ON CONFLICT DO NOTHING",
                 (ws, s["name"], names, [ha and is_ha_read(s["name"], n) for n in names]))
    _LIVE[sid] = {"ws": ws, "agent_id": s.get("agent_id"), "name": s["name"], "prefix": bool(s.get("prefix")), "conn": bool(s.get("token_env") or s.get("headers")), "auth": s.get("auth_type"), "conn_id": s.get("connection_id"),
                  "tools": tools, "status": status}
    return status

def _scope():
    import deepagent_api as A
    ctx = A.S["rls"]() if A.S.get("rls") else ("*", "0", "admin")
    return int(ctx[0]) if str(ctx[0]).isdigit() else None

async def refresh(ids=None) -> dict:
    """Reload the enabled servers visible to the caller's context (all at boot, one workspace for a workspace admin). -> {server name: status}."""
    rows = await _q("SELECT * FROM mcp_servers WHERE enabled" + (" AND id = ANY(%s)" if ids is not None else "") + " ORDER BY id", (list(ids),) if ids is not None else ())
    await asyncio.gather(*[_load_one(r) for r in rows])
    seen = {r["id"] for r in rows}; sc = _scope()
    for sid in [i for i, e in _LIVE.items() if i not in seen and (ids is None or i in ids) and (sc is None or e["ws"] == sc)]:
        _LIVE.pop(sid, None)
    import deepagent_api as A
    A.S["agents"].clear()
    return {_LIVE[r["id"]]["name"]: _LIVE[r["id"]]["status"] for r in rows}

def drop(sid): _LIVE.pop(sid, None)

# ------------------------------------------------------------------ read side
def _entries(ws, agent_id=None):
    es = [e for e in _LIVE.values() if e["ws"] == ws and (e["agent_id"] is None or (agent_id is not None and e["agent_id"] == agent_id))]
    return sorted(es, key=lambda e: e["agent_id"] is not None)   # agent-private servers last: they win on a same key

def tools_for(ws, agent_id=None) -> dict:
    out = {}
    for e in _entries(ws, agent_id):
        for n, t in e["tools"].items(): out[f"{e['name']}:{n}"] = t
    return out

def model_names(ws, agent_id=None) -> dict:
    es = _entries(ws, agent_id); count = {}
    for e in es:
        for n in e["tools"]: count[n] = count.get(n, 0) + 1
    out = {}
    for e in es:
        for n in e["tools"]: out[f"{e['name']}:{n}"] = f"{e['name']}__{n}" if e["prefix"] or count[n] > 1 else n
    return out

def status_of(sid, default="tắt") -> str: return (_LIVE.get(sid) or {}).get("status", default)

async def enabled_keys(ws=None) -> dict:
    rows = await _q("SELECT server||':'||tool k, enabled FROM tool_settings WHERE workspace_id=%s", (ws if ws is not None else cur_ws(),))
    return {r["k"]: r["enabled"] for r in rows}

# ------------------------------------------------------------------ write side
def _writer():
    import admin
    if admin.is_demo(): raise HTTPException(403, "Tài khoản demo không dùng được tính năng này")
    u = admin.cur()
    if u and u.get("role") != "admin": raise HTTPException(403, "Chỉ quản trị workspace được sửa máy chủ MCP")
    return u

async def _is_super():
    import gw_api
    return await gw_api._is_super()

def _check_headers(h):
    for k, v in (h or {}).items():
        if not _HDR_NAME.match(k) or k.lower() in _HDR_DENY: raise HTTPException(422, f"Tên header không hợp lệ: {k}")
        if not isinstance(v, str) or not _SECRET_ONLY.match(v.strip()): raise HTTPException(422, f"Giá trị header '{k}' chỉ được là {{{{secret:TÊN}}}} (không ghi giá trị thật)")

def _check_lists(inc, exc):
    both = set(inc or []) & set(exc or [])
    if both: raise HTTPException(422, "Tool vừa nằm trong include_tools vừa trong exclude_tools: " + ", ".join(sorted(both)))

async def _check_url(url):
    try: await netguard.acheck(url, "mcp")
    except netguard.NetBlocked as e: raise HTTPException(422, "Địa chỉ máy chủ MCP bị chặn: " + e.reason) from None

async def _check_agent(agent_id):
    if agent_id is None: return
    import agent_sharing
    await agent_sharing.guard_edit(agent_id)

class McpServer(BaseModel):
    name: str = Field(pattern=NAME_RE)
    url: str = Field(pattern=r"^https?://")
    transport: str = Field(default="streamable_http", pattern=r"^(streamable_http|sse)$")
    token_env: str | None = None
    agent_id: int | None = None
    auth_type: str = Field(default="none", pattern=r"^(none|headers)$")
    headers: dict[str, str] | None = None
    include_tools: list[str] | None = None
    exclude_tools: list[str] | None = None
    prefix: bool = False
    timeout_s: int = Field(default=60, ge=1, le=300)
    sse_fallback: bool = False
    reconnect: bool = False
    throw_on_load_error: bool = False

class McpServerUpd(BaseModel):
    url: str = Field(pattern=r"^https?://")
    transport: str = Field(default="streamable_http", pattern=r"^(streamable_http|sse)$")
    token_env: str | None = None
    auth_type: str | None = Field(default=None, pattern=r"^(none|headers)$")
    headers: dict[str, str] | None = None
    include_tools: list[str] | None = None
    exclude_tools: list[str] | None = None
    prefix: bool | None = None
    timeout_s: int | None = Field(default=None, ge=1, le=300)
    sse_fallback: bool | None = None
    reconnect: bool | None = None
    throw_on_load_error: bool | None = None
    forward_headers: bool | None = None   # GĐ11.27: forward the run's X-AOS-Forward-* headers to this server (people at the keyboard only)
    enabled: bool | None = None

async def _check_token_env(te, super_, old=None):
    if not te or te == old: return
    if not super_: raise HTTPException(403, "Chỉ quản trị toàn hệ thống được đặt token_env")
    if not te.startswith(TOKEN_PREFIX): raise HTTPException(422, f"token_env phải bắt đầu bằng {TOKEN_PREFIX}")

def _dup(name): return {"detail": f"Server '{name}' đã tồn tại", "hint": f"PUT /api/mcp/servers/{name} to update"}

@router.post("/api/mcp/servers")
async def add_server(s: McpServer):
    _writer(); ws = cur_ws()
    if s.name == "ha": raise HTTPException(409, _dup(s.name))   # reserved: the seeded Home Assistant row (guard() treats server 'ha' as read-exempt)
    await _check_token_env(s.token_env, await _is_super() if s.token_env else False)
    _check_headers(s.headers); _check_lists(s.include_tools, s.exclude_tools)
    await _check_agent(s.agent_id); await _check_url(s.url)
    if (await _q("SELECT count(*) n FROM mcp_servers WHERE workspace_id=%s", (ws,), one=True))["n"] >= MAX_SERVERS:
        raise HTTPException(422, f"Tối đa {MAX_SERVERS} máy chủ MCP cho mỗi workspace")
    if await _q("SELECT 1 FROM mcp_servers WHERE workspace_id=%s AND coalesce(agent_id,0)=coalesce(%s,0) AND name=%s", (ws, s.agent_id, s.name), one=True):
        raise HTTPException(409, _dup(s.name))
    dup = await _q("SELECT name FROM mcp_servers WHERE workspace_id=%s AND url=%s AND name<>%s LIMIT 1", (ws, s.url, s.name), one=True)
    try:
        r = await _q("INSERT INTO mcp_servers(name,url,transport,token_env,agent_id,auth_type,headers,include_tools,exclude_tools,prefix,timeout_s,sse_fallback,reconnect,throw_on_load_error,workspace_id) "
                     "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                     (s.name, s.url, s.transport, s.token_env or None, s.agent_id, s.auth_type, Jsonb(s.headers) if s.headers else None, s.include_tools, s.exclude_tools,
                      s.prefix, s.timeout_s, s.sse_fallback, s.reconnect, s.throw_on_load_error, ws), one=True)
    except UniqueViolation: raise HTTPException(409, _dup(s.name)) from None
    st = (await refresh([r["id"]])).get(s.name)
    out = {"ok": True, "id": r["id"], "status": st}
    if dup: out["warning"] = f"same URL as {dup['name']}"
    if st and st.startswith("lỗi") and not (s.token_env or s.headers):   # GĐ11.26: a server that just wants OAuth sign-in is not a plain failure
        import mcp_oauth
        hint = await mcp_oauth.hint_if_needs_auth(s.url)
        if hint: out.update(needs_auth=True, probe=hint)
    return out

async def _find(name, agent_id=None):
    r = await _q("SELECT * FROM mcp_servers WHERE workspace_id=%s AND name=%s AND coalesce(agent_id,0)=coalesce(%s,0)", (cur_ws(), name, agent_id), one=True)
    if not r: raise HTTPException(404, f"Không có server '{name}'")
    return r

@router.put("/api/mcp/servers/{name}")
async def upd_server(name: str, s: McpServerUpd, agent_id: int | None = None):
    _writer(); old = await _find(name, agent_id)
    super_ = await _is_super()
    await _check_token_env(s.token_env, super_, old["token_env"])
    if old["token_env"] and s.url != old["url"] and not super_: raise HTTPException(403, "Server dùng token_env: chỉ quản trị toàn hệ thống được đổi địa chỉ")
    _check_headers(s.headers)
    inc = s.include_tools if "include_tools" in s.model_fields_set else old["include_tools"]
    exc = s.exclude_tools if "exclude_tools" in s.model_fields_set else old["exclude_tools"]
    _check_lists(inc, exc)
    if s.url != old["url"]: await _check_url(s.url)
    dup = await _q("SELECT name FROM mcp_servers WHERE workspace_id=%s AND url=%s AND name<>%s LIMIT 1", (old["workspace_id"], s.url, name), one=True)
    sets, args = ["url=%s", "transport=%s", "token_env=%s"], [s.url, s.transport, s.token_env or None]
    for f in ("auth_type", "prefix", "timeout_s", "sse_fallback", "reconnect", "throw_on_load_error", "enabled", "include_tools", "exclude_tools", "forward_headers"):
        if f in s.model_fields_set: sets.append(f + "=%s"); args.append(getattr(s, f))
    if "headers" in s.model_fields_set: sets.append("headers=%s"); args.append(Jsonb(s.headers) if s.headers else None)
    await _q(f"UPDATE mcp_servers SET {', '.join(sets)} WHERE id=%s", (*args, old["id"]))
    if s.url != old["url"]: await _q("UPDATE mcp_servers SET tools_cache=NULL, tools_cached_at=NULL WHERE id=%s", (old["id"],))
    st = (await refresh([old["id"]])).get(name)
    if st is None: drop(old["id"])
    out = {"ok": True, "status": st}
    if dup: out["warning"] = f"same URL as {dup['name']}"
    return out

@router.delete("/api/mcp/servers/{name}")
async def del_server(name: str, agent_id: int | None = None):
    _writer()
    if name == "ha": raise HTTPException(400, "Không xoá server HA mặc định (có thể tắt từng tool)")
    old = await _find(name, agent_id)
    await _q("DELETE FROM mcp_servers WHERE id=%s", (old["id"],)); drop(old["id"])
    await _q("DELETE FROM tool_settings WHERE workspace_id=%s AND server=%s AND NOT EXISTS (SELECT 1 FROM mcp_servers m WHERE m.workspace_id=%s AND m.name=%s)",
             (old["workspace_id"], name, old["workspace_id"], name))
    await refresh([]); return {"ok": True}

async def _by_id(sid):
    r = await _q("SELECT * FROM mcp_servers WHERE id=%s", (sid,), one=True)
    if not r: raise HTTPException(404, "Không có server")
    return r

@router.post("/api/mcp/servers/{sid}/refresh-tools")
async def refresh_tools(sid: int):
    _writer(); r = await _by_id(sid)
    st = (await refresh([sid])).get(r["name"], "tắt")
    if r["throw_on_load_error"] and st.startswith("lỗi"): raise HTTPException(502, st)
    return {"ok": not st.startswith("lỗi"), "status": st, "tools": len((_LIVE.get(sid) or {}).get("tools", {}))}

@router.delete("/api/mcp/servers/{sid}/tools-cache")
async def clear_cache(sid: int):
    _writer(); await _by_id(sid)
    await _q("UPDATE mcp_servers SET tools_cache=NULL, tools_cached_at=NULL WHERE id=%s", (sid,)); return {"ok": True}

class CallIn(BaseModel):
    tool: str
    args: dict = {}
    confirm: bool = False

@router.post("/api/mcp/servers/{sid}/call")
async def call_tool(sid: int, b: CallIn):
    _writer(); r = await _by_id(sid)
    t = (_LIVE.get(sid) or {}).get("tools", {}).get(b.tool)
    if not t: raise HTTPException(404, f"Server '{r['name']}' không có tool '{b.tool}'")
    import time
    import deepagent_api as A
    if not (_trusted(r) and A.is_ha_read("ha", b.tool)) and not _read_only(t) and not b.confirm:
        raise HTTPException(409, {"detail": "Tool có thể ghi dữ liệu: cần confirm:true", "needs_confirm": True})
    t0 = time.perf_counter(); res = await A.guard(t, r["name"]).ainvoke(b.args)
    return {"ms": int((time.perf_counter() - t0) * 1000), "result": str(res)[:6000]}

# ------------------------------------------------------------------ legacy paths (kept)
@router.get("/api/mcp")
async def mcp_list(agent_id: int | None = None):
    import deepagent_api as A
    ws = cur_ws()
    servers = await _q("SELECT id,name,url,transport,token_env,enabled,agent_id,auth_type,prefix,timeout_s,include_tools,exclude_tools,sse_fallback,reconnect,"
                       "throw_on_load_error,tools_cached_at,last_error,forward_headers,connection_id,(SELECT c.provider_id FROM connections c WHERE c.id=mcp_servers.connection_id AND c.workspace_id=mcp_servers.workspace_id) AS provider_id "
                       "FROM mcp_servers WHERE workspace_id=%s ORDER BY id", (ws,))
    allowed = await enabled_keys(ws); tools = []
    for k, t in tools_for(ws, agent_id).items():
        srv, name = k.split(":", 1)
        ha = srv == "ha" and ws == 1
        blocked = bool(A.block_re().search(name)) and not (ha and A.is_ha_read(srv, name))
        tools.append({"server": srv, "tool": name, "key": k, "description": (t.description or "")[:400], "enabled": bool(allowed.get(k)),
                      "read_only": ha and A.is_ha_read(srv, name), "blocked": blocked, "schema": t.args_schema if isinstance(t.args_schema, dict) else {}})
    return {"servers": [{**s, "status": status_of(s["id"])} for s in servers], "tools": tools,
            "block_pattern": A.BLOCK_RE_BASE, "blocked_devices": sorted(A.S["block_names"])}

class ToolToggle(BaseModel):
    server: str
    tool: str
    enabled: bool

@router.post("/api/mcp/toggle")
async def toggle(t: ToolToggle):
    import deepagent_api as A
    await _q("INSERT INTO tool_settings(workspace_id,server,tool,enabled) VALUES(%s,%s,%s,%s) ON CONFLICT(workspace_id,server,tool) DO UPDATE SET enabled=EXCLUDED.enabled",
             (cur_ws(), t.server, t.tool, t.enabled))
    A.S["agents"].clear(); return {"ok": True}

@router.post("/api/mcp/refresh")
async def mcp_refresh():
    return await refresh()

class ToolTest(BaseModel):
    key: str
    args: dict = {}

@router.post("/api/mcp/test")
async def tool_test(t: ToolTest):
    import time
    import deepagent_api as A
    srv, name = t.key.split(":", 1); ws = cur_ws()
    if not (srv == "ha" and ws == 1 and A.is_ha_read(srv, name)): raise HTTPException(403, "Chỉ cho gọi thử tool CHỈ ĐỌC")
    tool = tools_for(ws).get(t.key)
    if not tool: raise HTTPException(404, "Không có tool")
    t0 = time.perf_counter(); r = await A.guard(tool, srv).ainvoke(t.args)
    return {"ms": int((time.perf_counter() - t0) * 1000), "result": str(r)[:6000]}
