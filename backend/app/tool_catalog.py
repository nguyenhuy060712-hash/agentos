"""GĐ11.31 tool catalog (`GET /api/tools`) + `find_tools` for agents with many tools.
- catalog(ws, agent_id) -> rows {id, handle, kind, name, server, description, requires_connection}. handle = prefixed name (F16): `mcp:{server}__{tool}`,
  builtin `aos:{name}` / `local:{name}`, office `google:…`/`ms365:…`; handles are unique, bare names are not. Shape is FROZEN for 12.85 (tests/test_gd11_tools.py).
- find_tools(query, k) ranks the agent's registered tools by token match on name/description (no model call). With more than AOS_MCP_MAX_TOOLS (500) MCP tools
  the agent only shows find_tools + core tools to the model; FindToolsMiddleware reveals the tools that find_tools returned (kept in the ToolMessage artifact)."""
import os, re, unicodedata

from fastapi import APIRouter, HTTPException
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool

router = APIRouter()
KINDS = ("builtin", "mcp", "office", "custom", "schema")
ROW_KEYS = ("id", "handle", "kind", "name", "server", "description", "requires_connection")
FIND = "find_tools"

def max_tools() -> int:
    try: return int(os.environ.get("AOS_MCP_MAX_TOOLS", "500"))
    except ValueError: return 500

def _row(handle, kind, name, server, desc, conn):
    return {"id": handle, "handle": handle, "kind": kind, "name": name, "server": server, "description": (desc or "")[:400], "requires_connection": bool(conn)}

async def catalog(ws, agent_id=None) -> list[dict]:
    import deepagent_api as A, mcp_registry as R, workspace_tools as W, assistant
    allowed = await R.enabled_keys(ws); names = R.model_names(ws, agent_id); live = {e["name"]: e for e in R._entries(ws, agent_id)}
    rows = []
    for k, t in R.tools_for(ws, agent_id).items():
        if not allowed.get(k): continue
        srv = k.split(":", 1)[0]
        rows.append(_row(f"mcp:{srv}__{k.split(':', 1)[1]}", "mcp", names.get(k, t.name), srv, t.description, (live.get(srv) or {}).get("conn")))
    for k, t in A.local_tools(agent_id or 0).items(): rows.append(_row(k, "builtin", t.name, "", t.description, False))
    for k, t in assistant.tool_pool().items(): rows.append(_row(k, "builtin", t.name, "", t.description, False))
    for r in await W.catalog(ws):
        if r.get("alias_of"): continue   # GĐ11.55: old tool keys still work but the catalogue lists each tool once
        rows.append(_row(r["key"], "office", r["key"].split(":", 1)[-1], r["key"].split(":", 1)[0], r["description"], True))
    return rows

def _norm(s) -> str:
    return unicodedata.normalize("NFKD", (s or "").lower().replace("đ", "d")).encode("ascii", "ignore").decode()

def _toks(s) -> set:
    return {w for w in re.split(r"[^a-z0-9]+", _norm(s)) if len(w) > 1}

def rank(items, query, k=8):
    """items: [(name, description)] -> best k indexes (score > 0), name hits count 3x."""
    q = _toks(query); out = []
    if not q: return []
    for i, (n, d) in enumerate(items):
        nt, dt = _toks(n), _toks(d)
        s = 3 * len(q & nt) + len(q & dt)
        if s: out.append((-s, i))
    return [i for _, i in sorted(out)[:max(1, min(int(k), 25))]]

def make_find_tools(searchable: list) -> StructuredTool:
    """searchable = the agent's registered tools (the deferred ones included)."""
    def _find(query: str, k: int = 8):
        hits = [searchable[i] for i in rank([(t.name, t.description or "") for t in searchable], query, k)]
        if not hits: return "Không tìm thấy công cụ phù hợp. Thử từ khoá khác.", []
        return "Các công cụ tìm được (đã mở để dùng ở lượt sau):\n" + "\n".join(f"- {t.name}: {(t.description or '')[:160]}" for t in hits), [t.name for t in hits]
    return StructuredTool.from_function(func=_find, name=FIND, response_format="content_and_artifact",
        description="Tìm công cụ theo từ khoá khi bạn có quá nhiều công cụ. Trả về tên + mô tả; công cụ tìm được sẽ dùng được ngay sau đó. query: nhu cầu bằng vài từ; k: số kết quả (mặc định 8).")

class FindToolsMiddleware(AgentMiddleware):
    """Hides the deferred tools from the model until find_tools returned them."""
    def __init__(self, deferred: set[str]):
        super().__init__(); self.deferred = deferred
    def _chosen(self, messages) -> set:
        out = set()
        for m in messages:
            if isinstance(m, ToolMessage) and m.name == FIND and isinstance(m.artifact, list): out |= {str(x) for x in m.artifact}
        return out
    def _flt(self, request):
        keep = self._chosen(request.messages)
        return request.override(tools=[t for t in request.tools if (t.name if hasattr(t, "name") else t.get("name")) not in self.deferred or (t.name if hasattr(t, "name") else t.get("name")) in keep])
    async def awrap_model_call(self, request, handler): return await handler(self._flt(request))
    def wrap_model_call(self, request, handler): return handler(self._flt(request))

def apply(tools: list, keys: list, limit: int | None = None):
    """tools = pick(cfg.tools), keys = cfg.tools. -> (tools, middleware or None). Above the limit: find_tools is added and MCP tools are deferred."""
    mcp = [t for k, t in zip(keys, tools) if not k.startswith(("local:", "aos:", "google:", "ms365:"))] if len(keys) == len(tools) else []
    explicit = any(t.name == FIND for t in tools)
    if len(mcp) <= (limit if limit is not None else max_tools()) and not explicit: return tools, None
    ft = make_find_tools(list(tools)) if not explicit else next(t for t in tools if t.name == FIND)
    return (tools if explicit else tools + [ft]), FindToolsMiddleware({t.name for t in mcp})

@router.get("/api/tools")
async def tools_api(q: str = "", kind: str = "", server: str = "", cursor: str = "", limit: int = 50, agent_id: int | None = None):
    import admin
    u = admin.cur() or {}
    if admin.is_demo(u): raise HTTPException(403, "Tính năng này tắt trong tài khoản demo")
    if kind and kind not in KINDS: raise HTTPException(400, f"kind phải là một trong {', '.join(KINDS)}")
    try: off = int(cursor or 0)
    except ValueError: raise HTTPException(400, "cursor không hợp lệ") from None
    ws = int(u.get("ws") or 1); rows = await catalog(ws, agent_id)
    if kind: rows = [r for r in rows if r["kind"] == kind]
    if server: rows = [r for r in rows if r["server"] == server]
    if q:
        qt = _toks(q); rows = [r for r in rows if qt & (_toks(r["name"]) | _toks(r["description"]) | _toks(r["server"]))]
    rows.sort(key=lambda r: r["handle"]); limit = max(1, min(int(limit), 200)); page = rows[off:off + limit]
    return {"tools": page, "total": len(rows), "next_cursor": str(off + limit) if off + limit < len(rows) else None}
