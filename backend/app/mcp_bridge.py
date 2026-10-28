"""GĐ9.6 MCP servers on the official `mcp` 2.x SDK (spec 2026-07-28 stateless single-exchange AND the older 2025-11-25 / 2025-06-18 `initialize`
handshake: StreamableHTTPSessionManager classifies each request, so one endpoint serves both generations of clients).
`LCServer` = `MCPServer` that serves LangChain tools directly (replaces langchain-mcp-adapters' `to_fastmcp`, archived, mcp 1.x only).
Stateless + JSON responses: every request is authenticated by the normal middleware (admin.middleware) and runs under that request's identity."""
import contextlib
from langchain_core.tools import ToolException
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, Tool as MCPTool

# auth is done by admin.middleware (public host behind Cloudflare), so DNS-rebinding host checks are off like before
SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)

def _schema(t) -> dict:
    s = dict(t.get_input_jsonschema()); s.pop("title", None); s.setdefault("type", "object"); s.setdefault("properties", {})
    return s

class LCServer(MCPServer):
    def __init__(self, name, lc_tools=(), **kw):
        super().__init__(name, **kw)
        self._lc = {t.name: t for t in lc_tools}

    def lc_tools(self) -> dict: return self._lc

    async def list_tools(self):
        return [MCPTool(name=n, description=(t.description or "")[:2000], input_schema=_schema(t)) for n, t in self.lc_tools().items()]

    async def call_tool(self, name, arguments, context=None):
        t = self.lc_tools().get(name)
        if t is None: return CallToolResult(content=[TextContent(type="text", text=f"Unknown tool: {name}")], is_error=True)
        try:
            out = await t.ainvoke(arguments or {})
        except ToolException as e: return CallToolResult(content=[TextContent(type="text", text=str(e))], is_error=True)
        except Exception as e:  # noqa: BLE001  (bad arguments etc.: the tool call fails, the server does not)
            return CallToolResult(content=[TextContent(type="text", text=f"{type(e).__name__}: {str(e)[:400]}")], is_error=True)
        return CallToolResult(content=[TextContent(type="text", text=out if isinstance(out, str) else str(out))])

def build(srv: MCPServer) -> MCPServer:
    """Create the session manager (stateless, JSON responses)."""
    srv.streamable_http_app(stateless_http=True, json_response=True, transport_security=SECURITY)
    return srv

@contextlib.asynccontextmanager
async def run(srv: MCPServer):
    async with srv.session_manager.run(): yield

def asgi(srv: MCPServer):
    """Raw ASGI app (a class instance, so Starlette routes every method to it)."""
    class Asgi:
        async def __call__(self, scope, receive, send): await srv.session_manager.handle_request(scope, receive, send)
    return Asgi()
