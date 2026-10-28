"""GĐ11.38 "Trò chuyện chung" (general chat agent) + the caller's NAME in the prompt. In-process, fake LLM ($0): a scripted chat model drives the REAL build_agent
(deepagents + GatewayMiddleware + HITL) of a throwaway workspace that has a fake MCP server (Linear) in mcp_registry._LIVE."""
import asyncio, json, os, sys, uuid
import psycopg, pytest
from fastapi import HTTPException
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

os.environ.setdefault("GOOGLE_API_KEY", "x-not-used")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import deepagent_api as A  # noqa: E402,I001  (must come first: circular imports)
import admin, caller, chat_general as G, gateway, migrations, mcp_registry as R  # noqa: E402

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
WA, WB, WD = 93801, 93802, 93803
SID = -938000
SUF = uuid.uuid4().hex[:6]
def U(ws, **kw): return {"id": 900, "username": "t", "role": "admin", "ws": ws, **kw}

def mk(name, desc):
    async def _run(**kw): return f"called {name}"
    return StructuredTool.from_function(coroutine=_run, name=name, description=desc, args_schema={"type": "object", "properties": {}})

class Scripted(GenericFakeChatModel):
    """Records the tool names it was bound with and the system text of each call."""
    seen: list = []
    sys_texts: list = []
    def bind_tools(self, tl, **kw): self.seen.append([getattr(t, "name", None) or t.get("name") for t in tl]); return self
    def _generate(self, messages, *a, **kw):
        self.sys_texts.append("\n".join(str(m.content) for m in messages if m.type == "system")); return super()._generate(messages, *a, **kw)

def run(fn):
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as c:
            await migrations.run_pending(c)
        async with AsyncConnectionPool(PG, min_size=1, max_size=4, open=False, kwargs={"autocommit": True, "row_factory": dict_row}) as pool:
            await pool.open(); A.S["pool"] = pool; A.S["rls"] = admin.rls_ctx; A.S["cp"] = InMemorySaver(); A.S["agents"].clear()
            for w, d in ((WA, False), (WB, False), (WD, True)):
                await A.q("INSERT INTO workspaces(id,name,demo) VALUES(%s,%s,%s) ON CONFLICT (id) DO NOTHING", (w, f"gc{w}{SUF}", d))
            try: return await fn()
            finally:
                ws = (WA, WB, WD)
                await A.q("DELETE FROM agents WHERE workspace_id = ANY(%s)", (list(ws),))
                await A.q("DELETE FROM datasets WHERE workspace_id = ANY(%s)", (list(ws),))
                await A.q("DELETE FROM tool_settings WHERE workspace_id = ANY(%s)", (list(ws),))
                await A.q("DELETE FROM users WHERE username LIKE %s", (f"gc{SUF}%",))
                await A.q("DELETE FROM workspaces WHERE id = ANY(%s)", (list(ws),))
                for sid in [i for i, e in R._LIVE.items() if e["ws"] in ws]: R._LIVE.pop(sid, None)
                A.S["agents"].clear()
    return asyncio.run(go())

async def as_ws(ws, fn, **kw):
    tok = admin.USER.set(U(ws, **kw))
    try: return await fn()
    finally: admin.USER.reset(tok)

def fake_models(monkeypatch, msgs):
    m = Scripted(messages=iter(msgs)); m.seen = []; m.sys_texts = []
    async def noprep(ref): return None
    monkeypatch.setattr(gateway, "prepare", noprep); monkeypatch.setattr(gateway, "_model", lambda *a, **k: m)
    return m

async def seed_linear(ws):
    R._LIVE[SID] = {"ws": ws, "agent_id": None, "name": "linear", "prefix": False, "conn": False, "tools": {"create_issue": mk("create_issue", "Create an issue in the Linear project tracker"),
                    "list_issues": mk("list_issues", "List Linear issues")}, "status": "ok"}
    await A.q("INSERT INTO tool_settings(workspace_id,server,tool,enabled) VALUES(%s,'linear','create_issue',true),(%s,'linear','list_issues',true) ON CONFLICT DO NOTHING", (ws, ws))

def test_ensure_per_workspace_first_in_list_read_only():
    async def go():
        a = await as_ws(WA, G.ensure_general_chat); a2 = await as_ws(WA, G.ensure_general_chat); b = await as_ws(WB, G.ensure_general_chat)
        assert a["id"] == a2["id"] and a["assistant_id"] == a2["assistant_id"] and b["id"] != a["id"] and b["assistant_id"] != a["assistant_id"]   # own agent per workspace, idempotent
        n = await A.q("SELECT count(*) n, min(current_version) v FROM agents WHERE workspace_id=%s AND system_kind='general_chat'", (WA,), one=True)
        assert n["n"] == 1 and n["v"] == 1
        await as_ws(WA, lambda: A.create_agent(A.AgentCfg(name="Agent thường")))   # a normal agent created AFTER: still second
        rows = await as_ws(WA, A.list_agents)
        assert [r["name"] for r in rows][:2] == [G.NAME, "Agent thường"]
        asr = await A.q("SELECT metadata, created_at FROM lg_assistants WHERE agent_id=%s", (a["id"],), one=True)
        assert asr["metadata"]["chat_visible"] is True and asr["created_at"].year == 2000                # sorts first in the chat list (created_at asc)
        cfg = (await A.q("SELECT config FROM agent_versions WHERE agent_id=%s", (a["id"],), one=True))["config"]
        assert cfg["tools"] == ["aos:list_examples"] and "aos:run_experiment" in cfg["interrupt_on"]
        with pytest.raises(HTTPException) as e: await as_ws(WA, lambda: A._visible(a["id"]))
        assert e.value.status_code == 403
    run(go)

def test_find_tools_then_write_waits_for_approval(monkeypatch):
    async def go():
        await seed_linear(WA)
        m = fake_models(monkeypatch, [AIMessage(content="", tool_calls=[{"name": "find_tools", "args": {"query": "create issue linear"}, "id": "c1"}]),
                                      AIMessage(content="", tool_calls=[{"name": "create_issue", "args": {}, "id": "c2"}]), AIMessage(content="xong")])
        async def go2():
            g = await G.ensure_general_chat(); v = await A.cur_version(g["id"])
            ag, cfg = await A.build_agent(g["id"], v)
            return ag, cfg
        ag, cfg = await as_ws(WA, go2)
        assert "linear:create_issue" in cfg.tools and cfg.tools[0] == "aos:list_examples"
        out = await ag.ainvoke({"messages": [HumanMessage("tạo issue Linear: lỗi đăng nhập")]}, {"configurable": {"thread_id": "gc-" + SUF}})
        assert "create_issue" not in m.seen[0] and "list_examples" in m.seen[0] and "find_tools" in m.seen[0]   # nothing but core + find_tools at first
        assert "create_issue" in m.seen[1] and "get_trace" not in m.seen[1] and "run_experiment" not in m.seen[1]                                 # revealed by find_tools only
        ints = out.get("__interrupt__")
        assert ints and ints[0].value["action_requests"][0]["name"] == "create_issue"                           # write tool: paused for approval, not executed
        assert not any(x.type == "tool" and x.name == "create_issue" for x in out["messages"])
    run(go)

def test_demo_workspace_read_only_tools(monkeypatch):
    async def go():
        await seed_linear(WD)
        async def go2():
            g = await G.ensure_general_chat(); ag, cfg = await A.build_agent(g["id"], await A.cur_version(g["id"])); return cfg
        cfg = await as_ws(WD, go2, demo=True)
        assert cfg.tools[0] == "aos:list_examples" and not any(k.startswith("linear:") for k in cfg.tools)
        assert "aos:get_trace" in cfg.tools and not any(k.split(":", 1)[1] in ("run_experiment", "propose_prompt_edit", "create_dataset_from_runs", "draft_evaluator") for k in cfg.tools)
    run(go)

def test_caller_name_in_prompt_without_email(monkeypatch):
    async def go():
        u = await A.q("INSERT INTO users(username,pw_hash,display_name) VALUES(%s,'x',%s) RETURNING id", (f"gc{SUF}a", "Nguyễn Văn An"), one=True)
        u2 = await A.q("INSERT INTO users(username,pw_hash,display_name) VALUES(%s,'x',%s) RETURNING id", (f"gc{SUF}b", "an.nguyen@corp.example"), one=True)
        m = fake_models(monkeypatch, [AIMessage(content="một"), AIMessage(content="hai"), AIMessage(content="ba"), AIMessage(content="bốn")])
        async def agent_for(name):
            aid, v = await A.create_agent(A.AgentCfg(name=name, filesystem=False, planning=False)); return aid, await A.build_agent(aid, v)
        async def go2():
            g = await G.ensure_general_chat(); gag, _ = await A.build_agent(g["id"], await A.cur_version(g["id"]))
            aid, (pag, _) = await agent_for("Agent chung " + SUF)
            return g, gag, aid, pag
        g, gag, aid, pag = await as_ws(WA, go2)
        async def ask(ag, uid, thread):
            tok = caller.RUN.set({"caller": {"user_id": uid, "username": "x", "kind": "api_key", "ws": WA}, "headers": {}})   # headless / API run: restored caller, no HTTP request
            try: return await ag.ainvoke({"messages": [HumanMessage("chào")]}, {"configurable": {"thread_id": thread + SUF}})
            finally: caller.RUN.reset(tok)
        await ask(gag, u["id"], "n1"); assert "Người dùng: Nguyễn Văn An" in m.sys_texts[-1]
        await ask(gag, u2["id"], "n2"); t = m.sys_texts[-1]
        assert "Người dùng: an.nguyen" in t and "@" not in t and "corp.example" not in t                    # e-mail-looking value is cut: never an e-mail
        await ask(pag, u["id"], "n3"); assert "Người dùng:" not in m.sys_texts[-1]                          # private, unshared agent: untouched
        await A.q("INSERT INTO agent_shares(workspace_id,agent_id,principal_kind,principal_id,role) VALUES(%s,%s,'user',%s,'use')", (WA, aid, u2["id"]))
        G._SHARED.clear()
        await ask(pag, u["id"], "n4"); assert "Người dùng: Nguyễn Văn An" in m.sys_texts[-1]                # shared agent
    run(go)

def test_channel_caller_and_binding_refused(monkeypatch):
    async def go():
        import channels
        m = fake_models(monkeypatch, [AIMessage(content="x")])
        async def go2():
            g = await G.ensure_general_chat(); ag, _ = await A.build_agent(g["id"], await A.cur_version(g["id"])); return g, ag
        g, ag = await as_ws(WA, go2)
        tok = caller.RUN.set({"caller": {"user_id": None, "kind": "channel", "ws": WA}, "headers": {}})
        try:
            with pytest.raises(PermissionError): await ag.ainvoke({"messages": [HumanMessage("chào")]}, {"configurable": {"thread_id": "ch" + SUF}})
        finally: caller.RUN.reset(tok)
        assert not m.sys_texts
        with pytest.raises(HTTPException) as e: await as_ws(WA, lambda: channels.create_binding(g["id"], channels.BindingIn(kind="telegram", name="x")))
        assert e.value.status_code in (403, 422) and (e.value.status_code == 403 or "kênh" in str(e.value.detail).lower())
    run(go)

def test_list_examples_reads_own_workspace_only():
    async def go():
        for ws, nm in ((WA, "ds-a"), (WB, "ds-b")):
            d = await A.q("INSERT INTO datasets(name, workspace_id) VALUES(%s,%s) RETURNING id", (nm + SUF, ws), one=True)
            for i, txt in enumerate(["hoá đơn tháng 9", "vé máy bay"]):
                e = await A.q("INSERT INTO dataset_examples(dataset_id) VALUES(%s) RETURNING id", (d["id"],), one=True)
                await A.q("INSERT INTO example_revisions(example_id, version, inputs, outputs) VALUES(%s,1,%s,%s)", (e["id"], json.dumps({"q": txt}), json.dumps({"a": "ok"})))
        await A.q("UPDATE datasets SET version=1 WHERE workspace_id = ANY(%s)", ([WA, WB],))
        out = json.loads(await as_ws(WA, lambda: G.list_examples.ainvoke({"dataset": "ds-a" + SUF})))
        assert out["total"] == 2 and out["next_offset"] is None
        f = json.loads(await as_ws(WA, lambda: G.list_examples.ainvoke({"dataset": "ds-a" + SUF, "filter": "HOÁ ĐƠN", "limit": 1})))
        assert f["total"] == 1 and "hoá đơn" in f["examples"][0]["inputs"]
        r = await as_ws(WA, lambda: G.list_examples.ainvoke({"dataset": "ds-b" + SUF}))
        assert "not found" in str(r).lower()                                                              # another workspace's dataset is invisible
    run(go)
