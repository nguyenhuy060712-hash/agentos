"""GĐ11.13 fallback chains v2: workspace chain rows, status-code based retry rules, no retry after the first streamed token, demo stays on Gemini. Fake provider servers only ($0)."""
import asyncio, json, os, sys, uuid
import psycopg, pytest
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app")); sys.path.insert(0, HERE)
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")
import deepagent_api   # noqa: F401,E402  (first app import: modules import each other circularly)
from fakes.fake_providers import FakeProviders  # noqa: E402

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos"); os.environ.setdefault("PG_URI", PG)
run = asyncio.run
KEY = "sk-fake-provider-key-1234"
CLAUDE, GPT = "anthropic/claude-haiku-5-5", "gpt-5.4-nano"

def with_ws(fn):
    import deepagent_api as A, admin, gw_providers as gp, gw_fallbacks as fb, migrations
    async def go():
        old = (A.S.get("pool"), A.S.get("rls"))
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as c: await migrations.run_pending(c)
        async with AsyncConnectionPool(PG, min_size=1, max_size=4, open=False, kwargs={"autocommit": True, "row_factory": dict_row}) as pool:
            await pool.open(); A.S["pool"] = pool; A.S["rls"] = admin.rls_ctx
            await admin.rls_grants()
            for t in ("gw_providers", "gw_fallback_chains"): await admin._policy(t, "aos_ws() IS NULL OR workspace_id = aos_ws()")
            tag = uuid.uuid4().hex[:8]; W = {"tag": tag}
            for s in "ab": W[s] = (await A.q("INSERT INTO workspaces(name) VALUES(%s) RETURNING id", (f"p1113-{s}-{tag}",), one=True))["id"]
            try: return await fn(A, admin, gp, fb, W)
            finally:
                for s in "ab":
                    for sql in ("DELETE FROM gw_fallback_chains WHERE workspace_id=%s", "DELETE FROM gateway_usage WHERE workspace_id=%s", "DELETE FROM gw_providers WHERE workspace_id=%s",
                                "DELETE FROM ws_secrets WHERE workspace_id=%s", "DELETE FROM workspaces WHERE id=%s"):
                        try: await A.q(sql, (W[s],))
                        except Exception as e: print("cleanup:", str(e)[:80])   # noqa: BLE001
                fb.invalidate(); A.S["pool"], A.S["rls"] = old
    return run(go())

async def as_user(admin, ws, fn, role="admin", demo=False):
    t = admin.USER.set({"id": 987656, "username": "t1113", "role": role, "ws": ws, "auth": "test", **({"demo": True} if demo else {})})
    try: return await fn()
    finally: admin.USER.reset(t)

async def _prov(admin, gp, ws, kind, fp): return await as_user(admin, ws, lambda: gp.create_provider(gp.ProviderIn(api_key=KEY, kind=kind, base_url=fp.url(kind))))
async def _chain(admin, fb, ws, primary, cands, **kw): return await as_user(admin, ws, lambda: fb.create_chain(fb.ChainIn(primary_ref=primary, candidates=cands, **kw)))
def msgs(fp): return [c for c in fp.calls if c["path"] in ("/v1/messages", "/v1/chat/completions")]

@pytest.fixture
def two(monkeypatch):
    monkeypatch.setenv("AOS_EGRESS_ALLOW", "127.0.0.0/8,::1")
    with FakeProviders(key=KEY, status=503) as cl, FakeProviders(key=KEY) as oa: yield cl, oa

def agent(gw, chain, fallbacks=None):
    from langchain.agents import create_agent
    return create_agent(model=gw._model("gemini-3.6-flash", 0), tools=[], middleware=[gw.GatewayMiddleware(None, chain, [] if fallbacks is None else fallbacks, 0.1)])

def test_503_falls_back_to_gpt_and_chain_row_beats_agent_list(two):
    cl, oa = two
    async def go(A, admin, gp, fb, W):
        import gateway as gw
        a = W["a"]; await _prov(admin, gp, a, "anthropic", cl); await _prov(admin, gp, a, "openai", oa)
        async def ask(ag): gw.COOLDOWN.clear(); return await ag.ainvoke({"messages": [("user", "hi")]})
        # no chain row: the agent's own list is used (here: GPT)
        r = await as_user(admin, a, lambda: ask(agent(gw, CLAUDE, [GPT]))); assert r["messages"][-1].content == "pong" and msgs(cl) and msgs(oa)
        cl.calls.clear(); oa.calls.clear()
        # chain row for the primary: wins over the agent list (agent says a Gemini that must never be called)
        row = await _chain(admin, fb, a, CLAUDE, [GPT]); assert row["trigger_statuses"] == [429, 500, 502, 503, 504, 529] and row["header_timeout_s"] == 60 and row["primary_ref"] == CLAUDE
        r = await as_user(admin, a, lambda: ask(agent(gw, CLAUDE, ["gemini-3.6-flash"]))); assert r["messages"][-1].content == "pong" and msgs(oa)
        rows = await A.q("SELECT model, status FROM gateway_usage WHERE workspace_id=%s ORDER BY id", (a,))
        assert [(x["model"], x["status"]) for x in rows if x["model"] in (CLAUDE, GPT)][-2:] == [(CLAUDE, "error"), (GPT, "ok")]
        # lc_call and generate use the same row
        cl.calls.clear(); oa.calls.clear(); gw.COOLDOWN.clear()
        out = await as_user(admin, a, lambda: gw.lc_call([CLAUDE, "gemini-3.6-flash"], [("human", "x")], feature="engine")); assert out["model"] == GPT
        gw.COOLDOWN.clear(); txt, m, _, _ = await as_user(admin, a, lambda: gw.generate(None, "x", [CLAUDE, "gemini-3.6-flash"])); assert (txt, m) == ("pong", GPT)
    with_ws(go)

@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_client_errors_are_not_retried(two, status):
    cl, oa = two; cl.status = status
    async def go(A, admin, gp, fb, W):
        import gateway as gw
        a = W["a"]; await _prov(admin, gp, a, "anthropic", cl); await _prov(admin, gp, a, "openai", oa); await _chain(admin, fb, a, CLAUDE, [GPT])
        gw.COOLDOWN.clear()
        with pytest.raises(Exception) as e: await as_user(admin, a, lambda: agent(gw, CLAUDE).ainvoke({"messages": [("user", "hi")]}))
        assert fb.status_of(e.value) == status and len(msgs(cl)) == 1 and not msgs(oa)
        gw.COOLDOWN.clear()
        with pytest.raises(Exception): await as_user(admin, a, lambda: gw.lc_call([CLAUDE], [("human", "x")]))
        assert not msgs(oa)
    with_ws(go)

def test_custom_trigger_statuses_and_transport_flag(two):
    cl, oa = two; cl.status = 404
    async def go(A, admin, gp, fb, W):
        import gateway as gw
        a = W["a"]; await _prov(admin, gp, a, "anthropic", cl); await _prov(admin, gp, a, "openai", oa)
        await _chain(admin, fb, a, CLAUDE, [GPT], trigger_statuses=[404])      # an operator who wants 404 to fall back
        gw.COOLDOWN.clear(); r = await as_user(admin, a, lambda: agent(gw, CLAUDE).ainvoke({"messages": [("user", "hi")]})); assert r["messages"][-1].content == "pong" and msgs(oa)
    with_ws(go)

def test_header_timeout_moves_on_to_the_candidate(two):
    cl, oa = two; cl.status = 200; cl.latency = 3
    async def go(A, admin, gp, fb, W):
        import gateway as gw
        a = W["a"]; await _prov(admin, gp, a, "anthropic", cl); await _prov(admin, gp, a, "openai", oa); await _chain(admin, fb, a, CLAUDE, [GPT], header_timeout_s=1)
        gw.COOLDOWN.clear(); r = await as_user(admin, a, lambda: agent(gw, CLAUDE).ainvoke({"messages": [("user", "hi")]})); assert r["messages"][-1].content == "pong" and msgs(oa)
        fb.invalidate(); gw.COOLDOWN.clear(); oa.calls.clear()   # on_transport_error=false: a timeout is final
        row = (await as_user(admin, a, lambda: fb.list_chains()))["chains"][0]; await as_user(admin, a, lambda: fb.update_chain(row["id"], fb.ChainUpd(on_transport_error=False)))
        with pytest.raises(Exception): await as_user(admin, a, lambda: agent(gw, CLAUDE).ainvoke({"messages": [("user", "hi")]}))
        assert not msgs(oa)
    with_ws(go)

def test_candidate_without_key_is_skipped(two):
    cl, oa = two
    async def go(A, admin, gp, fb, W):
        import gateway as gw
        a = W["a"]; await _prov(admin, gp, a, "anthropic", cl); await _prov(admin, gp, a, "openai", oa)
        ds = await as_user(admin, a, lambda: gp.create_provider(gp.ProviderIn(preset="deepseek", api_key=KEY, base_url=oa.url("openai"))))
        await _chain(admin, fb, a, CLAUDE, ["deepseek/deepseek-flash", GPT])
        await as_user(admin, a, lambda: gp.update_provider(ds["id"], gp.ProviderUpd(enabled=False))); gp.CRED.clear()   # a candidate whose provider is off = no usable key
        gw.COOLDOWN.clear(); r = await as_user(admin, a, lambda: agent(gw, CLAUDE).ainvoke({"messages": [("user", "hi")]})); assert r["messages"][-1].content == "pong" and msgs(oa)
    with_ws(go)

def test_error_after_first_streamed_token_is_not_retried(two, monkeypatch):
    cl, oa = two
    class Boom(Exception):
        status_code = 503
    class Half:
        calls = 0
        async def astream(self, messages):
            from langchain_core.messages import AIMessageChunk
            Half.calls += 1; yield AIMessageChunk(content="par"); raise Boom("overloaded after the first token")
    async def go(A, admin, gp, fb, W):
        import gateway as gw
        a = W["a"]; await _prov(admin, gp, a, "openai", oa); await _chain(admin, fb, a, GPT, ["gemini-3.6-flash"])
        async def nop(ref): return None
        monkeypatch.setattr(gw, "prepare", nop); monkeypatch.setattr(gw, "_model", lambda m, t, p=None: Half())
        got = []
        async def tok(x): got.append(x)
        gw.COOLDOWN.clear()
        with pytest.raises(Boom): await as_user(admin, a, lambda: gw.lc_call([GPT], [("human", "x")], on_token=tok))
        assert got == ["par"] and Half.calls == 1                                  # one attempt: no second try, no candidate
        Half.calls = 0; got.clear(); gw.COOLDOWN.clear()
        class Early(Half):
            async def astream(self, messages):
                Early.calls += 1; raise Boom("overloaded before any token"); yield  # noqa: B901
        monkeypatch.setattr(gw, "_model", lambda m, t, p=None: Early())
        with pytest.raises(Boom): await as_user(admin, a, lambda: gw.lc_call([GPT], [("human", "x")], on_token=tok))
        assert Early.calls >= 2                                                    # nothing was sent yet: retried (same model, then the candidate)
    with_ws(go)

def test_api_validation_roles_isolation_and_demo(two):
    cl, oa = two
    async def go(A, admin, gp, fb, W):
        import gateway as gw
        a, b = W["a"], W["b"]; await _prov(admin, gp, a, "anthropic", cl); await _prov(admin, gp, a, "openai", oa)
        refs6 = [f"gemini-{i}" for i in range(6)]
        for cands, code in ((refs6, 422), ([], 422), ([GPT, GPT], 422), ([CLAUDE], 422), (["nope/unknown-model"], 400)):
            with pytest.raises(HTTPException) as e: await _chain(admin, fb, a, CLAUDE, cands)
            assert e.value.status_code == code, cands
        for kw in ({"trigger_statuses": [200]}, {"trigger_statuses": []}, {"header_timeout_s": 0}, {"header_timeout_s": 601}):
            with pytest.raises(HTTPException) as e: await _chain(admin, fb, a, CLAUDE, [GPT], **kw)
            assert e.value.status_code == 422, kw
        five = ["gemini-3.6-flash", "gemini-3-flash-preview", "gemini-3.1-flash-lite", "gpt-5.4-mini", GPT]
        row = await _chain(admin, fb, a, CLAUDE, five); assert len(row["candidates"]) == 5
        with pytest.raises(HTTPException) as e: await _chain(admin, fb, a, CLAUDE, [GPT])
        assert e.value.status_code == 409
        for role, demo in (("editor", False), ("viewer", False), ("viewer", True)):
            with pytest.raises(HTTPException) as e: await as_user(admin, a, lambda: fb.create_chain(fb.ChainIn(primary_ref=GPT, candidates=[CLAUDE])), role=role, demo=demo)
            assert e.value.status_code == 403
            assert not admin.allowed(role, "GET", "/api/gw/fallbacks", demo=demo) or role == "admin"
        assert admin.allowed("admin", "POST", "/api/gw/fallbacks") and "gw_fallback_chains" in admin.RLS_DIRECT
        assert (await as_user(admin, b, lambda: fb.list_chains()))["chains"] == []                              # workspace B sees nothing
        with pytest.raises(HTTPException) as e: await as_user(admin, b, lambda: fb.delete_chain(row["id"]))
        assert e.value.status_code == 404
        up = await as_user(admin, a, lambda: fb.update_chain(row["id"], fb.ChainUpd(candidates=[GPT], enabled=False))); assert up["candidates"] == [GPT] and up["enabled"] is False
        fb.invalidate(); assert (await as_user(admin, a, lambda: fb.chain_for(CLAUDE, ["gemini-3.6-flash"])))[0] == [CLAUDE, "gemini-3.6-flash"]   # disabled row = ignored
        assert (await as_user(admin, a, lambda: fb.delete_chain(row["id"])))["ok"]
        assert (await as_user(admin, a, lambda: fb.chain_for(CLAUDE)))[0] == [CLAUDE, *gw.DEFAULT_FALLBACKS]       # nothing set anywhere: DEFAULT_FALLBACKS
    with_ws(go)

def test_demo_never_leaves_gemini(two):
    cl, oa = two; cl.status = 503
    async def go(A, admin, gp, fb, W):
        import gateway as gw
        a = W["a"]; await _prov(admin, gp, a, "anthropic", cl); await _prov(admin, gp, a, "openai", oa)
        await _chain(admin, fb, a, CLAUDE, [GPT]); gw.DEMO_WS.add(a)
        try:
            async def chk():
                chain, cfg = await fb.chain_for(CLAUDE)
                assert chain == [CLAUDE, GPT] and all(gw.provider(m) == "gemini" for m in gw._usable(chain))
                with pytest.raises(gw.DemoOpenAIBlocked): await gw.prepare(GPT)   # a non-Gemini model is refused before any request
            await as_user(admin, a, chk, role="viewer", demo=True)
            assert not msgs(cl) and not msgs(oa)
        finally: gw.DEMO_WS.discard(a)
    with_ws(go)

def test_is_retryable_rules():
    import gw_fallbacks as fb
    class E(Exception):
        def __init__(self, msg="", **kw): super().__init__(msg); self.__dict__.update(kw)
    class R:
        status_code = 502
    class ReadTimeout(Exception): pass
    class APIConnectionError(Exception): pass
    cfg = {"trigger_statuses": [429, 503], "on_transport_error": True}
    assert fb.is_retryable(E(status_code=503), cfg) and fb.is_retryable(E(code=429), cfg) and not fb.is_retryable(E(status_code=500), cfg)
    assert not fb.is_retryable(E("Timeout in the message", status_code=400), cfg) and not fb.is_retryable(E(response=R()), cfg) and fb.is_retryable(E(response=R()))
    assert fb.is_retryable(ReadTimeout(), cfg) and fb.is_retryable(APIConnectionError("x"), cfg) and not fb.is_retryable(ReadTimeout(), {**cfg, "on_transport_error": False})
    assert fb.is_retryable(RuntimeError("503 UNAVAILABLE: overloaded")) and fb.is_retryable(RuntimeError("RESOURCE_EXHAUSTED")) and not fb.is_retryable(ValueError("bad schema"))
    assert fb.mark_status(ReadTimeout()) == 504 and fb.mark_status(E(status_code=429)) == 429 and fb.mark_status(ValueError()) is None

def test_529_anthropic_overloaded_is_retryable_by_default():
    import gw_fallbacks as fb
    class E(Exception):
        def __init__(self, **kw): super().__init__("overloaded"); self.__dict__.update(kw)
    assert 529 in fb.DEFAULT_STATUSES and fb.is_retryable(E(status_code=529)) and fb.is_retryable(E(status_code=529), {"trigger_statuses": None})
    assert not fb.is_retryable(E(status_code=529), {"trigger_statuses": [429, 503]})   # a custom list still wins
