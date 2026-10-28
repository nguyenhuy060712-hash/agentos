"""GĐ8.8 Per-agent MCP endpoint /mcp/agents/{id} (next to A2A /a2a/{id}): any MCP client (Claude Code, Cursor, another agent) can
call ONE agent as a tool. Official `mcp` 2.x MCPServer via mcp_bridge.LCServer (stateless, JSON responses, same setup as /mcp/aos; GĐ9.6). Exposes:
  - tool `ask_<slug>(message)` -> the agent's reply. Runs through the normal run path (deepagent_api.run_agent, like /mcp ask_agent) AS the
    calling user (traced + costed + budget-checked); a run paused for approval returns a "needs approval" text (approve in the Inbox).
  - resource `agent://<id>/description` (name + description).
Auth = the normal middleware (user API key aos_... / session / Basic). Access = agent_sharing: invisible/private-unshared -> 404,
visible without 'use' right -> 403. Demo accounts / demo workspace: refused (feature off). Read-only keys cannot POST (middleware)."""
import contextlib, contextvars, json, re, unicodedata
from fastapi.responses import JSONResponse

AGENT: contextvars.ContextVar = contextvars.ContextVar("aos_mcp_agent", default=None)
_MCP = None
_SPECS: dict = {}
_CUR_TOOLS: contextvars.ContextVar = contextvars.ContextVar("aos_mcp_cur_tools", default=None)

def slug(name: str, aid: int) -> str:
    s = unicodedata.normalize("NFKD", (name or "").replace("đ", "d").replace("Đ", "D")).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")[:40].rstrip("_")
    return s or f"agent_{aid}"

def reply_text(r: dict) -> str:
    if r.get("paused"):
        return (f"Cần phê duyệt / needs approval: the agent wants to run an action that needs a human approval"
                f" (approval #{r.get('approval_id')}). Approve or reject it in Agent OS > Inbox, then ask again.")
    return r.get("reply") or ""

def _spec(a: dict):
    """A small LCServer holding this agent's tool + resource (cached by id/version/name/description; never run itself)."""
    key = (a["id"], a["current_version"], a["name"], a.get("description") or "")
    if key in _SPECS: return _SPECS[key]
    import mcp_bridge as B
    from mcp.server.mcpserver.resources import TextResource
    from langchain_core.tools import StructuredTool, ToolException
    from pydantic import BaseModel, Field
    aid, name, desc = a["id"], a["name"], a.get("description") or ""
    class Ask(BaseModel):
        message: str = Field(min_length=1, max_length=8000, description="Câu hỏi / yêu cầu gửi cho agent (message to the agent)")
    async def ask(message: str) -> str:
        import ext
        r = await ext._ask(aid, message)
        if r.get("error") and not r.get("paused"): raise ToolException(str(r["error"])[:500])
        return reply_text(r)
    t = StructuredTool.from_function(coroutine=ask, name=f"ask_{slug(name, aid)}", args_schema=Ask,
                                     description=(f"Hỏi agent '{name}' (Agent OS #{aid}) và nhận câu trả lời. " + desc)[:1000])
    fm = B.LCServer(f"agent-{aid}", [t])
    fm.add_resource(TextResource(uri=f"agent://{aid}/description", name="description", mime_type="text/markdown",
                                 description=f"Mô tả agent {name}", text=f"# {name}\n\n{desc or '(chưa có mô tả / no description)'}\n"))
    if len(_SPECS) >= 64: _SPECS.pop(next(iter(_SPECS)))
    _SPECS[key] = fm
    return fm

async def _agent(aid: int) -> dict:
    import deepagent_api as A
    return await A.q("SELECT a.id, a.name, a.current_version, v.config->>'description' description FROM agents a"
                     " JOIN agent_versions v ON v.agent_id=a.id AND v.version=a.current_version WHERE a.id=%s", (aid,), one=True)

def mcp_server():
    global _MCP
    if _MCP is None:
        import mcp_bridge as B
        class AgentsMCP(B.LCServer):   # one server for every /mcp/agents/{id}; the agent comes from the request (AGENT contextvar)
            async def _cur(self): return _spec(await _agent(AGENT.get()))
            def lc_tools(self): return _CUR_TOOLS.get() or {}
            async def list_tools(self):
                cur = await self._cur(); tok = _CUR_TOOLS.set(cur.lc_tools())
                try: return await super().list_tools()
                finally: _CUR_TOOLS.reset(tok)
            async def call_tool(self, name, arguments, context=None):
                cur = await self._cur(); tok = _CUR_TOOLS.set(cur.lc_tools())
                try: return await super().call_tool(name, arguments, context)
                finally: _CUR_TOOLS.reset(tok)
            async def list_resources(self): return await (await self._cur()).list_resources()
            async def read_resource(self, uri, context=None): return await (await self._cur()).read_resource(uri, context)
            async def list_resource_templates(self): return []
        _MCP = B.build(AgentsMCP("moza-agent", instructions="One Agent OS agent as a tool: call ask_<agent>(message) and get its reply."))   # auth = admin.middleware
    return _MCP

@contextlib.asynccontextmanager
async def mcp_lifespan():
    async with mcp_server().session_manager.run(): yield

async def _gate(aid: int):
    """-> None if the caller may use the agent, else (status, message)."""
    import admin, agent_sharing, gateway as gw
    from fastapi import HTTPException
    u = admin.cur() or {}
    if admin.is_demo(u) or int(u.get("ws") or 1) in gw.DEMO_WS: return 403, "MCP theo agent tắt cho tài khoản demo / per-agent MCP is off for demo accounts"
    try: await agent_sharing.require_use(aid)
    except HTTPException as e: return e.status_code, str(e.detail)
    return None

def mount(app):
    """/mcp/agents/{id} (and trailing slash) as raw ASGI routes, like aos_tools.mount."""
    from starlette.routing import Route
    srv = mcp_server()
    class Asgi:
        async def __call__(self, scope, receive, send):
            aid = int(scope["path_params"]["aid"])
            bad = await _gate(aid)
            if bad: return await JSONResponse({"detail": bad[1]}, bad[0])(scope, receive, send)
            tok = AGENT.set(aid)
            try: await srv.session_manager.handle_request(scope, receive, send)
            finally: AGENT.reset(tok)
    for p in ("/mcp/agents/{aid:int}", "/mcp/agents/{aid:int}/"): app.router.routes.insert(0, Route(p, endpoint=Asgi()))
