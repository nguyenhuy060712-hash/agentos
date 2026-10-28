"""Chat modes Fast / Standard / Thinking (27/10/2026, docs/CHE_DO_CHAT_PLAN.md). Fake LLM only ($0).
Pure + in-process parts need no server. The HTTP part needs a FAKE-LLM server of its own (default :8051; env AOS_CHAT_MODES_BASE):
never point it at the shared :8020 (that one may run real Gemini)."""
import asyncio, json, os, sys, uuid
from pathlib import Path
import httpx, psycopg, pytest
sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")
import chat_modes as CM  # noqa: E402

B = os.environ.get("AOS_CHAT_MODES_BASE", "http://127.0.0.1:8051"); ADMIN = ("moza", "phase0-local")
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
SUF = uuid.uuid4().hex[:6]
ALL = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3-flash-preview", "gemini-3.1-flash-lite"]

# ---------------------------------------------------------------- pure: defaults, resolve, validation, level clamping
def test_defaults_resolve_in_order_with_timeouts():
    modes, d = CM.resolve(None, ALL)
    assert [(m["id"], m["model"], m["thinking"], m["timeout_s"]) for m in modes] == [
        ("fast", "gemini-3.8-flash", "low", 120), ("standard", "gemini-3.8-flash", None, 180), ("deep", "gemini-3.8-flash", "high", 300)]
    assert d == "standard"

def test_mode_without_usable_model_is_hidden_and_default_moves():
    modes, d = CM.resolve(None, ["gemini-3-flash-preview"])   # 3-flash-preview is in no "fast" list -> fast hidden; standard / deep use it as a fallback
    ids = [m["id"] for m in modes]
    assert "fast" not in ids and "deep" in ids and d in ids
    assert CM.resolve(None, ["gpt-5.4-nano"]) == ([], None)

def test_overrides_enabled_default_and_unallowed_override_ignored():
    cm = {"default": "deep", "enabled": ["standard", "deep"], "overrides": {"deep": {"model": "gemini-3.6-flash", "thinking": "medium"}, "standard": {"model": "gpt-9", "thinking": None}}}
    modes, d = CM.resolve(cm, ALL)
    assert d == "deep" and [m["id"] for m in modes] == ["standard", "deep"]
    deep = modes[1]; assert (deep["model"], deep["thinking"]) == ("gemini-3.6-flash", "medium")
    std = modes[0]; assert std["model"] == "gemini-3.8-flash" and std["thinking"] is None   # model outside the allow-list -> the default one; thinking null = provider default

@pytest.mark.parametrize("bad", [{"enabled": []}, {"enabled": ["turbo"]}, {"default": "x"}, {"enabled": ["fast"], "default": "deep"}, {"overrides": {"fast": {"thinking": "max"}}},
                                 {"overrides": {"nope": {}}}, {"overrides": {"fast": {"color": 1}}}, {"bogus": 1}, "text"])
def test_validate_rejects(bad):
    with pytest.raises(ValueError): CM.validate(bad)

def test_validate_normalises_and_empty_is_none():
    assert CM.validate({}) is None and CM.validate(None) is None
    assert CM.validate({"enabled": ["deep", "fast"]}) == {"enabled": ["fast", "deep"]}

def test_thinking_levels_nearest_and_never_for_unknown():
    assert CM.think_for("gemini-3.8-flash", "low") == "low" and CM.think_for("gemini-3.8-flash", "high") == "high"
    assert CM.think_for("gemini-3-pro-preview", "medium") == "high"       # pro 3 takes only low / high; tie goes up
    assert CM.think_for("gemini-3.6-flash", None) is None
    assert CM.think_for("gemini-2.0-flash", "high") is None               # no level control -> provider default, never a 400
    assert CM.think_for("gpt-4.1-mini", "low") is None and CM.think_for("gpt-5.4-nano", "low") == "low"
    assert CM.think_for("anthropic/claude-haiku-5-5", "medium") == "medium"
    assert CM.think_for("custom/x", "high") is None

def test_chat_modes_saved_with_agent_not_a_run_override():
    import deepagent_api as A
    assert "chat_modes" not in A.OVERRIDABLE and not any(k.startswith("run_") for k in A.OVERRIDABLE)   # the mode travels as runtime context, never as an agent override
    c = A.AgentCfg(name="x", chat_modes={"enabled": ["fast"]})
    assert c.model_dump()["chat_modes"] == {"enabled": ["fast"]}
    with pytest.raises(Exception): A.AgentCfg(name="x", chat_modes={"enabled": ["turbo"]})

# ---------------------------------------------------------------- in-process: the model build gets thinking + the per-mode timeout
def _light(monkeypatch, gw, built):
    async def nobudget(*a, **k): return []
    monkeypatch.setattr(gw, "budget_check", nobudget); monkeypatch.setattr(gw, "ctx_now", lambda *a, **k: {})
    monkeypatch.setattr(gw, "_cur_ws", lambda: None); monkeypatch.setattr(gw, "gemini_only", lambda: False)
    monkeypatch.setattr(gw, "USAGE_SINK", []); monkeypatch.setattr(gw, "COOLDOWN", {})
    def mk(name, temperature=None, params=None): built.append((name, params)); return object()
    monkeypatch.setattr(gw, "_model", mk)

def _call(gw, mw, ctx=None):
    from langchain_core.messages import AIMessage
    from types import SimpleNamespace
    class Req:
        runtime = SimpleNamespace(context=ctx)
        def override(self, **kw): return self
    class Resp:
        result = [AIMessage(content="ok", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})]
    async def handler(req): return Resp()
    asyncio.run(mw.awrap_model_call(Req(), handler))

def test_gateway_builds_model_with_thinking_and_mode_timeout(monkeypatch):
    import gateway as gw
    built = []; _light(monkeypatch, gw, built)
    def go(mode, primary="gemini-3.6-flash"):
        built.clear()
        _call(gw, gw.GatewayMiddleware(None, primary, ["gemini-3-flash-preview"], 0.2), CM.RunContext(chat_mode="x", **mode) if mode else None)
        return list(built)
    # the agent's own model is gemini-3.6-flash; the mode swaps the primary model for this run only (no rebuild), the fallback list stays behind it
    assert go({"model": "gemini-3.8-flash", "thinking": "high", "timeout_s": 240}) == [("gemini-3.8-flash", {"timeout": 240, "reasoning_effort": "high"})]
    assert go({"model": "gemini-3.1-flash-lite", "thinking": "low", "timeout_s": 30}) == [("gemini-3.1-flash-lite", {"timeout": 30, "reasoning_effort": "low"})]
    assert go({"model": "gemini-2.5-flash", "thinking": "high", "timeout_s": 60}) == [("gemini-2.5-flash", {"timeout": 60})]   # no level control: only the timeout
    assert go({"model": "gemini-3.6-flash", "thinking": None, "timeout_s": 60}) == [("gemini-3.6-flash", {"timeout": 60})]       # standard: provider-default thinking
    assert go(None) == [("gemini-3.6-flash", None)]                                                                                # no runtime context: as before

def test_runtime_context_reaches_gateway_through_a_real_agent_graph(monkeypatch):
    """create_agent(context_schema=RunContext) + astream/ainvoke(context=...): the middleware sees request.runtime.context (the official LangChain pattern)."""
    import gateway as gw
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage
    built = []; _light(monkeypatch, gw, built)
    def mk(name, temperature=None, params=None): built.append((name, params)); return GenericFakeChatModel(messages=iter([AIMessage(content="ok", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})]))
    monkeypatch.setattr(gw, "_model", mk)
    ag = create_agent(model=mk("gemini-3.6-flash"), tools=[], context_schema=CM.RunContext, middleware=[gw.GatewayMiddleware(None, "gemini-3.6-flash", [], 0.2)])
    built.clear()
    asyncio.run(ag.ainvoke({"messages": [{"role": "user", "content": "hi"}]}, context=CM.RunContext("deep", "gemini-3.8-flash", "high", 240)))
    assert built == [("gemini-3.8-flash", {"timeout": 240, "reasoning_effort": "high"})]

def test_subagents_receive_the_same_runtime_context():
    """deepagents sub-agents (task tool) see the run's context too, so the gateway middleware on them follows the mode."""
    from deepagents import create_deep_agent
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage
    seen = []
    class Probe(AgentMiddleware):
        def __init__(self, tag): super().__init__(); self.tag = tag
        async def awrap_model_call(self, request, handler): seen.append((self.tag, request.runtime.context)); return await handler(request)
    class Fk(GenericFakeChatModel):
        def bind_tools(self, tools, **kw): return self
    main = Fk(messages=iter([AIMessage(content="", tool_calls=[{"name": "task", "args": {"description": "do it", "subagent_type": "sub"}, "id": "c1"}]), AIMessage(content="done")]))
    sub = Fk(messages=iter([AIMessage(content="sub ok")]))
    ag = create_deep_agent(model=main, subagents=[{"name": "sub", "description": "d", "system_prompt": "s", "model": sub, "middleware": [Probe("sub")]}], middleware=[Probe("main")], context_schema=CM.RunContext)
    ctx = CM.RunContext("deep", "m", "high", 240)
    asyncio.run(ag.ainvoke({"messages": [{"role": "user", "content": "hi"}]}, context=ctx))
    assert ("sub", ctx) in seen and ("main", ctx) in seen

def test_gateway_without_chat_mode_builds_exactly_as_before(monkeypatch):
    import gateway as gw
    built = []; _light(monkeypatch, gw, built)
    _call(gw, gw.GatewayMiddleware(None, "gemini-3.6-flash", [], 0.2))   # request without runtime at all
    assert built == [("gemini-3.6-flash", None)]   # no params at all: same call as before chat modes

def test_apply_chat_mode_sets_model_thinking_timeout_and_403(monkeypatch):
    import deepagent_api  # noqa: F401  (first app import: modules import each other circularly)
    import server, chat_settings as cs
    from fastapi import HTTPException
    async def modes(aid): return CM.resolve({"enabled": ["fast", "deep"]}, ALL)
    monkeypatch.setattr(cs, "mode_set", modes)
    ov = {"model": "gpt-5.4-nano"}
    async def go(m):
        CM.RUN_CTX.set(None); r = await server.apply_chat_mode(ov, 1, m); return r, CM.RUN_CTX.get()
    r, ctx = asyncio.run(go("deep"))
    assert "model" not in ov and ctx == CM.RunContext("deep", "gemini-3.8-flash", "high", 300)   # chat_mode wins over the old `model`; the run gets it as runtime context
    with pytest.raises(HTTPException) as e: asyncio.run(server.apply_chat_mode({}, 1, "standard"))
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e: asyncio.run(server.apply_chat_mode({}, 1, "nonsense"))
    assert e.value.status_code == 403

def test_run_chat_mode_reads_configurable_and_flat():
    import deepagent_api  # noqa: F401
    import server
    assert server.run_chat_mode({}, {"configurable": {"chat_mode": "fast"}}) == "fast"
    assert server.run_chat_mode({"configurable": {"chat_mode": "deep"}}, {"configurable": {"chat_mode": "fast"}}) == "fast"
    assert server.run_chat_mode({"configurable": {"chat_mode": "deep"}}, {}) == "deep"
    assert server.run_chat_mode({}, {"configurable": {"model": "m"}}) is None and server.run_chat_mode(None, None) is None

def test_demo_caller_cannot_save_chat_modes():
    import deepagent_api as A, admin, agent_fields as AF
    from fastapi import HTTPException
    cfg = A.AgentCfg(name="d", chat_modes={"enabled": ["fast"]})
    t = admin.USER.set({"id": 987001, "role": "viewer", "demo": True, "ws": 1})
    try:
        with pytest.raises(HTTPException) as e: AF.refuse_demo(cfg)
        assert e.value.status_code == 403
        AF.refuse_demo(A.AgentCfg(name="d"))   # no chat_modes: fine
    finally: admin.USER.reset(t)

# ---------------------------------------------------------------- HTTP on our own fake-LLM server
@pytest.fixture(scope="module")
def a():
    try: httpx.get(B + "/health", timeout=5)
    except Exception: pytest.skip(f"no fake-LLM server on {B} (start your own, see docstring)")
    with httpx.Client(base_url=B, auth=ADMIN, timeout=90) as c: yield c

def _mk(a, name, **kw):
    cfg = {"name": f"pytest-modes-{name}-{SUF}", "model": "gemini-3.8-flash", "fallback_models": ["gemini-3.6-flash", "gemini-3-flash-preview", "gemini-3.1-flash-lite"],
           "tools": [], "planning": False, "filesystem": False, **kw}
    r = a.post("/api/agents", json=cfg); assert r.status_code == 200, r.text
    return r.json()["id"], cfg["name"]

@pytest.fixture(scope="module")
def ag(a):
    ids = []
    def make(name, **kw):
        i, n = _mk(a, name, **kw); ids.append((i, n)); return i
    yield make
    for i, n in ids: a.delete(f"/api/agents/{i}")   # archive (the throwaway DB of this test server is dropped afterwards)

def _run(a, aid, cfg=None, text="xin chào"):
    t = a.post("/api/v1/threads", json={"metadata": {"source": "pytest"}}).json()["thread_id"]
    with psycopg.connect(PG) as c: mx = c.execute("SELECT coalesce(max(id),0) FROM gateway_usage").fetchone()[0]
    r = a.post(f"/api/v1/threads/{t}/runs/wait", json={"assistant_id": f"agent-{aid}", "input": {"messages": [{"type": "human", "content": text}]}, **({"config": cfg} if cfg else {})})
    with psycopg.connect(PG) as c: used = [x[0] for x in c.execute("SELECT model FROM gateway_usage WHERE id>%s AND agent_id=%s AND status='ok' ORDER BY id", (mx, aid))]
    return r, used

def test_config_lists_default_modes_and_keeps_old_fields(a, ag):
    i = ag("def")
    c = a.get("/api/chat/config", params={"agent_id": f"agent-{i}"}).json()
    assert [m["id"] for m in c["modes"]] == ["fast", "standard", "deep"] and c["default_mode"] == "standard"
    fast = c["modes"][0]; assert fast["model"] == "gemini-3.8-flash" and fast["thinking"] == "low" and fast["timeout_s"] == 120
    assert fast["input_per_1m"] == 0.25 and fast["output_per_1m"] == 1.5
    assert c["modes"][2]["thinking"] == "high" and c["modes"][2]["timeout_s"] == 240
    assert c["models"] and c["default_model"] == "gemini-3.8-flash" and c["picker"] is True

def test_disabled_mode_is_hidden_and_403(a, ag):
    i = ag("dis", chat_modes={"default": "fast", "enabled": ["fast", "standard"]})
    c = a.get("/api/chat/config", params={"agent_id": f"agent-{i}"}).json()
    assert [m["id"] for m in c["modes"]] == ["fast", "standard"] and c["default_mode"] == "fast"
    r, _ = _run(a, i, {"configurable": {"chat_mode": "deep"}}); assert r.status_code == 403, r.text
    r, _ = _run(a, i, {"configurable": {"chat_mode": "turbo"}}); assert r.status_code == 403

def test_chat_mode_overrides_model_and_old_model_still_works(a, ag):
    i = ag("run", chat_modes={"overrides": {"standard": {"model": "gemini-3-flash-preview", "thinking": "medium"}}})
    r, used = _run(a, i, {"configurable": {"chat_mode": "fast"}}); assert r.status_code == 200, r.text
    assert used and used[0] == "gemini-3.8-flash"                           # anh 27/10: every mode on gemini-3.8-flash
    r, used = _run(a, i, {"configurable": {"chat_mode": "standard"}}); assert r.status_code == 200 and used[0] == "gemini-3-flash-preview"
    r, used = _run(a, i, {"configurable": {"model": "gemini-3.6-flash"}}); assert r.status_code == 200 and used[0] == "gemini-3.6-flash"   # old clients
    r, used = _run(a, i, {"configurable": {"model": "gemini-3.6-flash", "chat_mode": "fast"}}); assert r.status_code == 200 and used[0] == "gemini-3.8-flash"   # chat_mode wins
    r, _ = _run(a, i, {"configurable": {"model": "gpt-5.4-nano"}}); assert r.status_code == 403                                  # allow-list unchanged
    r, used = _run(a, i); assert r.status_code == 200 and used[0] == "gemini-3.8-flash"                                         # no mode, no model: agent default

def test_invalid_chat_modes_is_422_and_export_import_keeps_it(a, ag):
    assert a.post("/api/agents", json={"name": "pytest-bad-" + SUF, "chat_modes": {"enabled": ["turbo"]}}).status_code == 422
    cm = {"default": "deep", "enabled": ["standard", "deep"], "overrides": {"deep": {"model": "gemini-3.6-flash", "thinking": "medium"}}}
    i = ag("exp", chat_modes=cm)
    ex = a.get(f"/api/agents/{i}/export").json(); assert ex["config"]["chat_modes"] == cm
    ex["config"]["name"] = f"pytest-modes-imp-{SUF}"
    r = a.post("/api/agents/import", json=ex); assert r.status_code == 200, r.text
    j = r.json()["id"]
    try:
        assert a.get(f"/api/agents/{j}").json()["config"]["chat_modes"] == cm
        c = a.get("/api/chat/config", params={"agent_id": f"agent-{j}"}).json()
        assert c["default_mode"] == "deep" and c["modes"][1]["thinking"] == "medium"
    finally: a.delete(f"/api/agents/{j}")

def test_demo_user_gets_gemini_defaults_and_cannot_save_modes(a):
    u = "demomodes" + SUF; pw = "demo-modes-pass-" + SUF
    r = a.post("/api/admin/users", json={"username": u, "password": pw, "role": "viewer", "demo": True, "daily_cap_usd": 0.05, "demo_agents": [2]}); assert r.status_code == 200, r.text
    uid = r.json()["id"]
    try:
        p = a.post("/api/admin/demo-workspace", json={"user_id": uid, "name": "Demo modes " + SUF, "source_agents": [2], "daily_cap_usd": 2.0}); assert p.status_code == 200, p.text
        clone = p.json()["agents"]["2"]
        with httpx.Client(base_url=B, auth=(u, pw), timeout=60) as d:
            c = d.get("/api/chat/config", params={"agent_id": f"agent-{clone}"}).json()
            assert c["modes"] and all(m["model"].startswith("gemini") for m in c["modes"])
            cfg = d.get(f"/api/agents/{clone}").json()["config"]
            r = d.put(f"/api/agents/{clone}", json={**cfg, "chat_modes": {"enabled": ["fast"]}}); assert r.status_code == 403, r.text
    finally:
        with psycopg.connect(PG) as c: c.execute("UPDATE users SET disabled=true WHERE id=%s", (uid,))
