"""GĐ11 card 11.31 (find_tools + GET /api/tools). In-process: 300 fake MCP tools in two workspaces are put straight into mcp_registry._LIVE;
a scripted fake chat model picks the tool through find_tools inside a real langchain agent. No real LLM ($0: ranking is plain token match)."""
import asyncio, os, sys, uuid
import psycopg, pytest
from fastapi import HTTPException
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

os.environ.setdefault("GOOGLE_API_KEY", "x-not-used")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import deepagent_api as A  # noqa: E402,I001  (must come first: circular imports)
import admin, migrations, mcp_registry as R, tool_catalog as C  # noqa: E402

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
WA, WB = 92001, 92002
SID = -920000
def U(ws, **kw): return {"id": 900, "username": "t", "role": "admin", "ws": ws, **kw}

def mk(name, desc):
    async def _run(**kw): return f"called {name}"
    return StructuredTool.from_function(coroutine=_run, name=name, description=desc, args_schema={"type": "object", "properties": {}})

def seed_live(ws, server, tools, sid, conn=False):
    R._LIVE[sid] = {"ws": ws, "agent_id": None, "name": server, "prefix": False, "conn": conn, "tools": {t.name: t for t in tools}, "status": "ok"}

def run(fn):
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as c:
            await migrations.run_pending(c)
        async with AsyncConnectionPool(PG, min_size=1, max_size=3, open=False, kwargs={"autocommit": True, "row_factory": dict_row}) as pool:
            await pool.open(); A.S["pool"] = pool; A.S["rls"] = admin.rls_ctx
            try: return await fn()
            finally:
                await A.q("DELETE FROM tool_settings WHERE workspace_id IN (%s,%s)", (WA, WB))
                for sid in [i for i, e in R._LIVE.items() if e["ws"] in (WA, WB)]: R._LIVE.pop(sid, None)
    return asyncio.run(go())

async def enable(ws, server, names):
    await A.q("INSERT INTO tool_settings(workspace_id,server,tool,enabled) SELECT %s,%s,t,true FROM unnest(%s::text[]) t ON CONFLICT DO NOTHING", (ws, server, names))

def big(ws, server, sid, n, special):
    ts = [mk(f"tool_{i:03d}", f"Generic helper number {i} for report batch") for i in range(n)]
    ts.append(mk(special[0], special[1]))
    seed_live(ws, server, ts, sid, conn=True); return [t.name for t in ts]

def test_catalog_shape_prefix_and_isolation():
    async def go():
        names = big(WA, "crm", SID, 3, ("create_invoice", "Create a new customer invoice"))
        seed_live(WA, "erp", [mk("create_invoice", "ERP invoice")], SID - 1)
        seed_live(WB, "secret", [mk("b_only_tool", "workspace B tool")], SID - 2)
        await enable(WA, "crm", names); await enable(WA, "erp", ["create_invoice"]); await enable(WB, "secret", ["b_only_tool"])
        a = await C.catalog(WA); b = await C.catalog(WB)
        mc = [r for r in a if r["kind"] == "mcp"]
        assert len(mc) == 5 and all(set(r) == set(C.ROW_KEYS) for r in a)
        assert {r["handle"] for r in mc} >= {"mcp:crm__create_invoice", "mcp:erp__create_invoice"}   # same bare name coexists
        assert len({r["handle"] for r in a}) == len(a)
        assert [r["requires_connection"] for r in mc if r["server"] == "crm"][0] is True and [r["requires_connection"] for r in mc if r["server"] == "erp"][0] is False
        assert not any("b_only_tool" in r["name"] for r in a) and [r["name"] for r in b if r["kind"] == "mcp"] == ["b_only_tool"]
        assert any(r["kind"] == "builtin" and r["handle"].startswith("aos:") for r in a)
    run(go)

def test_api_tools_schema_filters_paging_demo():
    async def go():
        names = big(WA, "crm", SID, 60, ("create_invoice", "Create a new customer invoice")); await enable(WA, "crm", names)
        tok = admin.USER.set(U(WA))
        try:
            r = await C.tools_api(kind="mcp", server="crm", limit=25)
            assert set(r) == {"tools", "total", "next_cursor"} and r["total"] == 61 and len(r["tools"]) == 25 and r["next_cursor"] == "25"
            assert all(list(x) == list(C.ROW_KEYS) for x in r["tools"]) and all(isinstance(x["requires_connection"], bool) for x in r["tools"])
            r3 = await C.tools_api(kind="mcp", server="crm", limit=25, cursor="50"); assert len(r3["tools"]) == 11 and r3["next_cursor"] is None
            assert [x["name"] for x in (await C.tools_api(q="invoice", kind="mcp"))["tools"]] == ["create_invoice"]
            with pytest.raises(HTTPException) as e: await C.tools_api(kind="nope")
            assert e.value.status_code == 400
            admin.USER.set(U(WB))
            assert (await C.tools_api(kind="mcp"))["total"] == 0                      # workspace B sees nothing of A
            admin.USER.set(U(WA, demo=True))
            with pytest.raises(HTTPException) as e: await C.tools_api()
            assert e.value.status_code == 403
        finally: admin.USER.reset(tok)
    run(go)

def test_find_tools_agent_picks_right_tool_among_300(monkeypatch):
    ts = [mk(f"tool_{i:03d}", f"Generic helper number {i} for report batch") for i in range(299)] + [mk("linear__create_issue", "Create an issue in Linear project tracker")]
    keys = [f"crm:{t.name}" for t in ts]
    tools, mw = C.apply(ts, keys, limit=100)
    assert mw is not None and tools[-1].name == "find_tools" and len(tools) == 301
    seen = []
    class M(GenericFakeChatModel):
        def bind_tools(self, tl, **kw): seen.append([getattr(t, "name", None) or t.get("name") for t in tl]); return self
    msgs = iter([AIMessage(content="", tool_calls=[{"name": "find_tools", "args": {"query": "create issue in linear"}, "id": "c1"}]),
                 AIMessage(content="", tool_calls=[{"name": "linear__create_issue", "args": {}, "id": "c2"}]), AIMessage(content="done")])
    ag = create_agent(M(messages=msgs), tools=tools, middleware=[mw])
    out = asyncio.run(ag.ainvoke({"messages": [HumanMessage("tạo issue Linear")]}))
    assert seen[0] == ["find_tools"]                                   # only find_tools shown at first (no core tools in this pool)
    assert "linear__create_issue" in seen[1] and "tool_000" not in seen[1]
    assert any(m.name == "linear__create_issue" and m.content == "called linear__create_issue" for m in out["messages"] if m.type == "tool")

def test_below_limit_untouched_and_rank():
    ts = [mk("a", "x"), mk("b", "y")]
    t2, mw = C.apply(ts, ["crm:a", "crm:b"], limit=500)
    assert mw is None and t2 == ts
    t3, mw = C.apply([mk("a", "x"), mk("local_core", "core")], ["crm:a", "local:local_core"], limit=0)
    assert mw.deferred == {"a"} and [t.name for t in t3][-1] == "find_tools"    # core (local:) tool stays visible
    items = [("send_email", "Gửi thư điện tử"), ("create_invoice", "Tạo hoá đơn khách hàng"), ("x", "y")]
    assert C.rank(items, "hoa don") == [1] and C.rank(items, "email", 8) == [0] and C.rank(items, "zzz") == []
