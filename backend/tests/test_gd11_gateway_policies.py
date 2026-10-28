"""GĐ11.14 policy engine (reduced): workspace day / month caps (ICT), model allow-list, feature access enforced server-side, provider caps with fallback, concurrent spends,
fail-closed. Money-critical: exact boundary, burst of concurrent calls, demo workspace, other workspace, provider off, unauthorised role. Fake provider servers only ($0)."""
import asyncio, datetime as dt, json, os, re, sys, uuid
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
CLAUDE, GPT, GEM = "anthropic/claude-haiku-5-5", "gpt-5.4-nano", "gemini-3.6-flash"
MSG = re.compile(r"^Đã chạm trần chi phí .+\(đã dùng \$\d+\.\d\d / trần \$\d+\.\d\d .+\)\. Liên hệ quản trị viên để nâng trần\.$")
UTC = dt.timezone.utc

def with_ws(fn):
    import deepagent_api as A, admin, gateway as gw, gw_providers as gp, gw_policy_engine as pe, migrations
    async def go():
        old = (A.S.get("pool"), A.S.get("rls"))
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as c: await migrations.run_pending(c)
        async with AsyncConnectionPool(PG, min_size=1, max_size=6, open=False, kwargs={"autocommit": True, "row_factory": dict_row}) as pool:
            await pool.open(); A.S["pool"] = pool; A.S["rls"] = admin.rls_ctx
            await admin.rls_grants()
            for t in ("gw_providers", "gw_model_access", "model_feature_access"): await admin._policy(t, "aos_ws() IS NULL OR workspace_id = aos_ws()")
            tag = uuid.uuid4().hex[:8]; W = {"tag": tag}
            for s in "ab": W[s] = (await A.q("INSERT INTO workspaces(name) VALUES(%s) RETURNING id", (f"p1114-{s}-{tag}",), one=True))["id"]
            pe.invalidate(); pe._RES.clear(); gw.DB_BLOCK.clear(); gw.COOLDOWN.clear()
            try: return await fn(A, admin, gw, gp, pe, W)
            finally:
                for s in "ab":
                    for sql in ("DELETE FROM spend_caps WHERE workspace_id=%s", "DELETE FROM gw_model_access WHERE workspace_id=%s", "DELETE FROM model_feature_access WHERE workspace_id=%s",
                                "DELETE FROM gateway_usage WHERE workspace_id=%s", "DELETE FROM gw_providers WHERE workspace_id=%s", "DELETE FROM ws_secrets WHERE workspace_id=%s",
                                "DELETE FROM workspaces WHERE id=%s"):
                        try: await A.q(sql, (W[s],))
                        except Exception as e: print("cleanup:", str(e)[:80])   # noqa: BLE001
                pe.invalidate(); pe._RES.clear(); gw.DB_BLOCK.clear(); gw.DEMO_WS.discard(W["b"])
                gw._SNAP.update(t=0.0, caps=[]); A.S["pool"], A.S["rls"] = old
    return run(go())

async def as_user(admin, ws, fn, role="admin", demo=False):
    t = admin.USER.set({"id": 987657, "username": "t1114", "role": role, "ws": ws, "auth": "test", **({"demo": True} if demo else {})})
    try: return await fn()
    finally: admin.USER.reset(t)

async def raw(sql, args=()):
    async with await psycopg.AsyncConnection.connect(PG, autocommit=True, row_factory=dict_row) as c:
        cur = await c.execute(sql, args)
        return await cur.fetchall() if cur.description else None

async def usage(ws, cost, ts=None, provider="gemini", model="gemini-3.6-flash", feature=None, user_id=None):
    await raw("INSERT INTO gateway_usage(ts,model,input_tokens,output_tokens,cost_usd,status,workspace_id,provider,feature,user_id) VALUES(coalesce(%s::timestamptz, now()),%s,0,0,%s,'ok',%s,%s,%s,%s)",
              (ts, model, cost, ws, provider, feature, user_id))

async def cap(ws, scope, target, period, limit, action="block", workspace=True):
    return (await raw("INSERT INTO spend_caps(workspace_id,scope,target,period,limit_usd,action) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id", (ws if workspace else None, scope, target, period, limit, action)))[0]["id"]

async def prov(admin, gp, ws, kind, fp): return await as_user(admin, ws, lambda: gp.create_provider(gp.ProviderIn(api_key=KEY, kind=kind, base_url=fp.url(kind))))
def msgs(fp): return [c for c in fp.calls if c["path"] in ("/v1/messages", "/v1/chat/completions")]
def agent(gw, chain):
    from langchain.agents import create_agent
    return create_agent(model=gw._model(GEM, 0), tools=[], middleware=[gw.GatewayMiddleware(None, chain, [], 0.1)])
def ctx(gw, ws, providers, strict=False, **kw):
    c = gw.ctx_now(providers, workspace_id=ws, **kw)
    if strict: c["_strict"] = True    # what gateway.gate does: budget_check(ctx) has no keyword for it
    return c

@pytest.fixture
def two(monkeypatch):
    monkeypatch.setenv("AOS_EGRESS_ALLOW", "127.0.0.0/8,::1")
    with FakeProviders(key=KEY, usage=(4_000_000, 0)) as cl, FakeProviders(key=KEY, usage=(1, 1)) as oa: yield cl, oa   # one Claude call = $2.00 (4M tokens, long-context price 0.50/1M)

# ---------------------------------------------------------------- periods in ICT
def test_period_start_ict_boundaries():
    import gateway as gw
    d = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=UTC)   # noqa: E731
    assert gw.period_start("day", d("2026-10-25T16:59:00")) == d("2026-10-24T17:00:00")   # 23:59 ICT on the 25th -> still the 25th
    assert gw.period_start("day", d("2026-10-25T17:01:00")) == d("2026-10-25T17:00:00")   # 00:01 ICT on the 26th -> new day
    assert gw.period_start("day", d("2026-10-25T17:00:00")) == d("2026-10-25T17:00:00")   # exactly midnight ICT belongs to the new day
    assert gw.period_start("week", d("2026-10-25T16:59:00")) == d("2026-10-18T17:00:00")   # Sunday 23:59 ICT: week began Monday 19th 00:00 ICT
    assert gw.period_start("week", d("2026-10-25T17:01:00")) == d("2026-10-25T17:00:00")   # Monday 00:01 ICT: a new week
    assert gw.period_start("month", d("2026-10-31T16:59:00")) == d("2026-09-30T17:00:00")
    assert gw.period_start("month", d("2026-10-31T17:01:00")) == d("2026-10-31T17:00:00")   # 1 Nov 00:01 ICT
    assert gw.period_start("total") is None and gw.period_start("day", dt.datetime(2026, 10, 25, 17, 1)) == d("2026-10-25T17:00:00")   # naive = UTC
    with pytest.raises(ValueError): gw.period_start("year")

def test_reached_exact_boundary_and_zero_limit():
    import gateway as gw
    assert gw.reached(0.5, 0.5) and gw.reached(0.1 + 0.2, 0.3) and gw.reached(0, 0) and gw.reached(0.0000004 + 0.2999996, 0.3)
    assert not gw.reached(0.4999, 0.5) and not gw.reached(0.299999, 0.3)

def test_cap_spent_day_boundary_in_sql():
    async def go(A, admin, gw, gp, pe, W):
        a = W["a"]; d = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=UTC)   # noqa: E731
        await usage(a, 0.2, ts=d("2026-10-25T16:59:00")); await usage(a, 0.3, ts=d("2026-10-25T17:01:00"))
        c = {"scope": "workspace", "target": str(a), "period": "day", "workspace_id": a}
        assert round(await gw.cap_spent(c, d("2026-10-25T17:30:00")), 6) == 0.3     # 00:30 ICT on the 26th: only the 17:01Z row is "today"
        assert round(await gw.cap_spent(c, d("2026-10-25T16:59:30")), 6) == 0.5     # 23:59:30 ICT on the 25th: both rows (no upper bound) count in that ICT day
        assert round(await gw.cap_spent({**c, "period": "total"}, d("2026-10-25T17:30:00")), 6) == 0.5
        # another workspace's rows never count
        await usage(W["b"], 9.0); assert round(await gw.cap_spent(c), 6) <= 0.5
    with_ws(go)

# ---------------------------------------------------------------- exact boundary, $0 cap, every path
def test_exact_boundary_then_blocked_and_unblocked_by_raising_the_limit():
    async def go(A, admin, gw, gp, pe, W):
        a, b = W["a"], W["b"]
        cid = await cap(a, "workspace", str(a), "month", 0.5)
        await usage(a, 0.4999)
        for st in (False, True):
            assert await gw.budget_check(ctx(gw, a, "gemini", strict=st), fresh=True) == []       # $0.0001 below the limit: allowed
        await usage(a, 0.0001)                                                                    # exactly at the limit
        for st in (False, True):
            with pytest.raises(gw.BudgetExceeded) as e: await gw.budget_check(ctx(gw, a, "gemini", strict=st), fresh=True)
            assert MSG.match(str(e.value)) and e.value.info["scope"] == "workspace" and e.value.info["limit_usd"] == 0.5 and e.value.info["used_usd"] == 0.5
        await gw.budget_check(ctx(gw, b, "gemini", strict=True))                                  # another workspace is not affected
        await raw("UPDATE spend_caps SET limit_usd=0.5001 WHERE id=%s", (cid,))
        await gw.budget_check(ctx(gw, a, "gemini", strict=True))                                  # raised limit: allowed at once (no cache)
        await raw("UPDATE spend_caps SET action='warn' WHERE id=%s", (cid,)); await raw("UPDATE spend_caps SET limit_usd=0.5 WHERE id=%s", (cid,))
        assert len(await gw.budget_check(ctx(gw, a, "gemini", strict=True))) == 1                 # warn: reported, not blocked
        await raw("UPDATE spend_caps SET action='block', enabled=false WHERE id=%s", (cid,))
        await gw.budget_check(ctx(gw, a, "gemini", strict=True))                                  # disabled: ignored
    with_ws(go)

def test_zero_month_cap_blocks_agent_playground_judge_and_v1(two):
    cl, oa = two
    async def go(A, admin, gw, gp, pe, W):
        import gw_api as G
        a, b = W["a"], W["b"]; await prov(admin, gp, a, "anthropic", cl); await prov(admin, gp, b, "anthropic", cl)
        await cap(a, "workspace", str(a), "month", 0)
        ask = lambda: agent(gw, CLAUDE).ainvoke({"messages": [("user", "hi")]})   # noqa: E731
        with pytest.raises(gw.BudgetExceeded): await as_user(admin, a, ask)                                                                    # agent middleware
        with pytest.raises(gw.BudgetExceeded): await as_user(admin, a, lambda: gw.lc_call([CLAUDE], [("human", "x")], feature="playground"))    # Playground
        with pytest.raises(gw.BudgetExceeded): await as_user(admin, a, lambda: gw.generate(None, "x", [CLAUDE], evaluator_id=7, feature="evaluator"))   # judge
        with pytest.raises(HTTPException) as e: await G.budget({"workspace_id": a, "id": 1})                                                     # /v1 (chat, messages, embeddings share it)
        assert e.value.status_code == 402 and e.value.detail["error"]["type"] == "budget_exceeded"
        assert not msgs(cl)                                                                                                                      # nothing was sent upstream
        r = await as_user(admin, b, ask); assert r["messages"][-1].content == "pong"                                                             # other workspace: fine
        await G.budget({"workspace_id": b, "id": 2})
        assert not pe._RES                                                                                                                       # no reservation leaked by the blocked calls
    with_ws(go)

def test_old_spend_caps_rows_and_api_keep_working_and_say_ict():
    async def go(A, admin, gw, gp, pe, W):
        import gw_api as G
        a = W["a"]
        r = await as_user(admin, a, lambda: G.caps_create(G.CapIn(scope="workspace", target=str(a), period="day", limit_usd=1.0)))
        assert r["current_spend_usd"] == 0 and r["tz"] == "Asia/Ho_Chi_Minh" and r["period_start"].endswith("+00:00") and r["reached"] is False
        await usage(a, 1.0)
        rows = await as_user(admin, a, lambda: G.caps_list()); row = [x for x in rows if x["id"] == r["id"]][0]
        assert row["current_spend_usd"] == 1.0 and row["reached"] is True
        with pytest.raises(gw.BudgetExceeded): await gw.budget_check(ctx(gw, a, "gemini"), fresh=True)                # snapshot path, as before
        with pytest.raises(gw.BudgetExceeded): await gw.budget_check(ctx(gw, a, "gemini", strict=True))               # fresh path
        assert (await gw.budget_check(ctx(gw, W["b"], "gemini"), fresh=True)) == []
    with_ws(go)

# ---------------------------------------------------------------- provider caps + fallback
def test_provider_cap_blocks_only_that_provider_and_falls_back(two):
    cl, oa = two
    async def go(A, admin, gw, gp, pe, W):
        a, b = W["a"], W["b"]
        for w in (a, b): await prov(admin, gp, w, "anthropic", cl); await prov(admin, gp, w, "openai", oa)
        await cap(a, "provider", "anthropic", "day", 0)
        out = await as_user(admin, a, lambda: gw.lc_call([CLAUDE, GPT], [("human", "x")], feature="engine"))
        assert out["model"] == GPT and not msgs(cl) and msgs(oa)                                                  # Claude is capped: the call went to GPT
        with pytest.raises(gw.BudgetExceeded) as e: await pe.evaluate(ctx(gw, a, ["anthropic"], feature="engine"), strict=True)   # only Claude to use: refused (the 402 text names the provider)
        assert "Anthropic" in str(e.value) and MSG.match(str(e.value))
        await pe.evaluate(ctx(gw, a, ["anthropic", "openai"], feature="engine"), strict=True)                                      # with GPT as an alternative: allowed
        gw.COOLDOWN.clear()
        out = await as_user(admin, b, lambda: gw.lc_call([CLAUDE, GPT], [("human", "x")], feature="engine")); assert out["model"] == CLAUDE   # other workspace: Claude works
    with_ws(go)

# ---------------------------------------------------------------- concurrency
def test_concurrent_calls_cannot_overspend_by_more_than_one_call(two):
    cl, oa = two; cl.latency = 0.4
    async def go(A, admin, gw, gp, pe, W):
        a = W["a"]; await prov(admin, gp, a, "anthropic", cl)
        await cap(a, "workspace", str(a), "month", 5.0)
        await usage(a, 2.0)                                                                   # one earlier call of $2.00 = the estimate for the next ones
        res = await asyncio.gather(*[as_user(admin, a, lambda: gw.lc_call([CLAUDE], [("human", "x")], feature="engine")) for _ in range(12)], return_exceptions=True)
        ok = [r for r in res if isinstance(r, dict)]; blocked = [r for r in res if isinstance(r, gw.BudgetExceeded)]
        assert len(ok) + len(blocked) == 12, res
        spent = float((await raw("SELECT coalesce(sum(cost_usd),0) c FROM gateway_usage WHERE workspace_id=%s", (a,)))[0]["c"])
        assert len(ok) == 2 and spent == pytest.approx(6.0) and spent <= 5.0 + 2.0 + 1e-9, (len(ok), spent)   # at most one call over the limit
        assert len(msgs(cl)) == 2 and not pe._RES                                                              # blocked calls sent nothing; every reservation was freed
        with pytest.raises(gw.BudgetExceeded): await as_user(admin, a, lambda: gw.lc_call([CLAUDE], [("human", "x")], feature="engine"))   # and the next call is blocked at once
    with_ws(go)

def test_failed_call_frees_its_reservation_and_reservations_do_not_cross_workspaces(two):
    cl, oa = two; cl.status = 400   # a non-retryable error: raised at once (a retryable one would go on to the real Gemini last resort)
    async def go(A, admin, gw, gp, pe, W):
        a, b = W["a"], W["b"]; await prov(admin, gp, a, "anthropic", cl)
        await cap(a, "workspace", str(a), "month", 1.0); await cap(b, "workspace", str(b), "month", 1.0); await usage(a, 0.4); await usage(b, 0.4)
        with pytest.raises(Exception): await as_user(admin, a, lambda: gw.lc_call([CLAUDE], [("human", "x")], feature="engine"))
        assert not pe._RES                                                                    # error path: freed
        t = await pe.evaluate(ctx(gw, a, "anthropic", feature="engine"), strict=True, reserve=True)   # ws a holds $0.40
        assert len(pe._RES) == 1
        await pe.evaluate(ctx(gw, b, "anthropic", feature="engine"), strict=True, reserve=True)       # ws b has its own room: a's reservation is invisible to b
        await t.release(); await t.release()                                                  # idempotent
        assert len(pe._RES) == 1
        pe._RES[0]["exp"] = 0                                                                 # an expired reservation (crashed call) is dropped by the next decision
        await pe.evaluate(ctx(gw, b, "anthropic"), strict=True)
        assert not pe._RES
    with_ws(go)

# ---------------------------------------------------------------- allow-list
def test_allow_list_403_fallbacks_dropped_other_workspace_and_demo(two):
    cl, oa = two
    async def go(A, admin, gw, gp, pe, W):
        a, b = W["a"], W["b"]
        for w in (a, b): await prov(admin, gp, w, "anthropic", cl); await prov(admin, gp, w, "openai", oa)
        r = await as_user(admin, a, lambda: pe.access_put(pe.AccessIn(mode="selected", providers=["openai"])))
        assert r["mode"] == "selected" and r["providers"] == ["openai"]
        with pytest.raises(pe.ModelNotAllowed) as e: await as_user(admin, a, lambda: gw.lc_call([CLAUDE, GPT], [("human", "x")], feature="engine"))
        assert e.value.status_code == 403 and e.value.ref == CLAUDE and "danh sách cho phép" in e.value.detail and not msgs(cl)             # the asked-for model is outside the list: refused, not swapped
        with pytest.raises(pe.ModelNotAllowed): await as_user(admin, a, lambda: gw.budget_check(gw.ctx_now(["anthropic"], models=[CLAUDE]), fresh=True))   # pre-check sites (chat 403) too
        with pytest.raises(pe.ModelNotAllowed): await as_user(admin, a, lambda: agent(gw, CLAUDE).ainvoke({"messages": [("user", "hi")]}))
        oa.status = 503   # GPT fails: Claude (outside the list) must NOT be tried as a fallback
        with pytest.raises(Exception) as e: await as_user(admin, a, lambda: gw.lc_call([GPT, CLAUDE], [("human", "x")], feature="engine"))
        assert not isinstance(e.value, pe.ModelNotAllowed) and not msgs(cl) and msgs(oa)
        oa.status = 200; oa.calls.clear(); gw.COOLDOWN.clear()
        assert (await as_user(admin, a, lambda: gw.lc_call([GPT, CLAUDE], [("human", "x")], feature="engine")))["model"] == GPT
        # a models entry allows that one model even though its provider is not listed; the other Claude is refused
        await as_user(admin, a, lambda: pe.access_put(pe.AccessIn(mode="selected", models=[CLAUDE])))
        assert (await as_user(admin, a, lambda: gw.lc_call([CLAUDE], [("human", "x")], feature="engine")))["model"] == CLAUDE
        with pytest.raises(pe.ModelNotAllowed): await as_user(admin, a, lambda: gw.lc_call([GPT], [("human", "x")], feature="engine"))
        # workspace b has no row: both models work (the row of a does not leak)
        gw.COOLDOWN.clear(); assert (await as_user(admin, b, lambda: gw.lc_call([CLAUDE], [("human", "x")], feature="engine")))["model"] == CLAUDE
        assert (await as_user(admin, b, lambda: pe.access_get()))["mode"] == "all"
        # back to all
        await as_user(admin, a, lambda: pe.access_put(pe.AccessIn(mode="all")))
        assert (await as_user(admin, a, lambda: gw.lc_call([GPT], [("human", "x")], feature="engine")))["model"] == GPT
    with_ws(go)

def test_demo_workspace_rules_off_but_caps_apply_and_api_refused():
    async def go(A, admin, gw, gp, pe, W):
        a, b = W["a"], W["b"]
        await raw("INSERT INTO gw_model_access(workspace_id,mode,providers) VALUES(%s,'selected','{openai}'),(%s,'selected','{openai}')", (a, b))   # a row written behind the API's back
        pe.invalidate()
        with pytest.raises(pe.ModelNotAllowed): await pe.evaluate(ctx(gw, a, "gemini", models=[GEM]))
        gw.DEMO_WS.add(b)
        await pe.evaluate(ctx(gw, b, "gemini", models=[GEM]))                                  # demo workspace: allow-list OFF (server-side)
        assert pe.model_ok(GEM, b) is True
        for w, kw in ((b, {}), (a, {"demo": True})):                                           # demo workspace admin / demo user cannot edit anything
            with pytest.raises(HTTPException) as e: await as_user(admin, w, lambda: pe.access_put(pe.AccessIn(mode="all")), **kw)
            assert e.value.status_code == 403
            with pytest.raises(HTTPException) as e: await as_user(admin, w, lambda: pe.limits_put(pe.LimitsIn(day_usd=5)), **kw)
            assert e.value.status_code == 403
        await cap(b, "workspace", str(b), "month", 0)                                          # caps still protect the demo workspace
        with pytest.raises(gw.BudgetExceeded): await pe.evaluate(ctx(gw, b, "gemini", models=[GEM]), strict=True)
        assert await raw("SELECT 1 FROM spend_caps WHERE workspace_id=%s AND action='block'", (b,))
    with_ws(go)

# ---------------------------------------------------------------- feature access (11.12 left the server-side enforcement to the engine)
def test_feature_access_and_provider_off_enforced_server_side(two):
    cl, oa = two
    async def go(A, admin, gw, gp, pe, W):
        import gw_features as F
        a, b = W["a"], W["b"]
        for w in (a, b): await prov(admin, gp, w, "anthropic", cl); await prov(admin, gp, w, "openai", oa)
        await as_user(admin, a, lambda: F.access_put("evaluator", F.AccessIn(providers=["openai"])))
        with pytest.raises(pe.ModelNotAllowed): await as_user(admin, a, lambda: gw.generate(None, "x", [CLAUDE], evaluator_id=3, feature="evaluator"))   # judge feature
        assert not msgs(cl)
        txt, m, _, _ = await as_user(admin, a, lambda: gw.generate(None, "x", [GPT, CLAUDE], evaluator_id=3, feature="evaluator")); assert m == GPT
        assert (await as_user(admin, a, lambda: gw.lc_call([CLAUDE], [("human", "x")], feature="playground")))["model"] == CLAUDE   # other feature: unrestricted
        assert (await as_user(admin, b, lambda: gw.generate(None, "x", [CLAUDE], evaluator_id=3, feature="evaluator")))[1] == CLAUDE   # other workspace
        # narrower wins: allow-list says anthropic only, the feature row says openai only -> nothing passes for 'evaluator'
        await as_user(admin, a, lambda: pe.access_put(pe.AccessIn(mode="selected", providers=["anthropic"])))
        with pytest.raises(pe.ModelNotAllowed): await as_user(admin, a, lambda: gw.generate(None, "x", [GPT], evaluator_id=3, feature="evaluator"))
        with pytest.raises(pe.ModelNotAllowed): await as_user(admin, a, lambda: gw.generate(None, "x", [CLAUDE], evaluator_id=3, feature="evaluator"))
        await as_user(admin, a, lambda: pe.access_put(pe.AccessIn(mode="all")))
        # provider switched off: refused for a feature (403), and its models are not a fallback
        rows = (await as_user(admin, a, lambda: gp.list_providers()))["providers"]; an = [r for r in rows if r["name"] == "anthropic" and r.get("id")][0]
        await as_user(admin, a, lambda: gp.update_provider(an["id"], gp.ProviderUpd(enabled=False))); pe.invalidate()
        with pytest.raises(pe.ModelNotAllowed): await as_user(admin, a, lambda: gw.lc_call([CLAUDE], [("human", "x")], feature="playground"))
        # unknown cost-only feature names (gateway, deep_research...) have no model-access row and do not raise 400
        await pe.evaluate(ctx(gw, a, "openai", feature="deep_research", models=[GPT]))
    with_ws(go)

# ---------------------------------------------------------------- /v1
def test_v1_model_gate_and_models_list():
    async def go(A, admin, gw, gp, pe, W):
        import gw_api as G
        a, b = W["a"], W["b"]; ka, kb = {"workspace_id": a, "id": 1}, {"workspace_id": b, "id": 2}
        await raw("INSERT INTO gw_model_access(workspace_id,mode,models) VALUES(%s,'selected','{gemini-3.1-flash-lite}')", (a,)); pe.invalidate()
        with pytest.raises(HTTPException) as e: await G.model_gate(ka, GEM, [GEM, "gemini-3.1-flash-lite"])
        assert e.value.status_code == 403 and e.value.detail["error"]["type"] == "permission_error"
        assert await G.model_gate(ka, "gemini-3.1-flash-lite", ["gemini-3.1-flash-lite", GEM]) == ["gemini-3.1-flash-lite"]   # the fallback outside the list is dropped
        assert await G.model_gate(kb, GEM, [GEM, "gemini-3.1-flash-lite"]) == [GEM, "gemini-3.1-flash-lite"]                    # other workspace: untouched
        assert await pe.filter_models(a, [GEM, "gemini-3.1-flash-lite"]) == ["gemini-3.1-flash-lite"]
    with_ws(go)

# ---------------------------------------------------------------- fail closed
def test_engine_failure_blocks_and_sends_nothing(two, monkeypatch):
    cl, oa = two
    async def go(A, admin, gw, gp, pe, W):
        a = W["a"]; await prov(admin, gp, a, "anthropic", cl)
        orig = gw._sys
        async def boom(*x, **k): raise RuntimeError("db down")
        monkeypatch.setattr(gw, "_sys", boom)
        with pytest.raises(pe.PolicyUnavailable) as e: await as_user(admin, a, lambda: gw.lc_call([CLAUDE], [("human", "x")], feature="engine"))
        assert "chặn" in str(e.value) and isinstance(e.value, gw.BudgetExceeded) and not msgs(cl)
        with pytest.raises(pe.PolicyUnavailable): await as_user(admin, a, lambda: agent(gw, CLAUDE).ainvoke({"messages": [("user", "hi")]}))
        with pytest.raises(pe.PolicyUnavailable): await pe.evaluate(ctx(gw, a, "anthropic"), strict=True)
        monkeypatch.setattr(gw, "_sys", orig)
        gw._SNAP.update(t=__import__("time").time() + 100, caps=[], err=True)                   # the cached snapshot path: a failed refresh blocks too
        with pytest.raises(pe.PolicyUnavailable): await gw.budget_check(ctx(gw, a, "gemini"))
        gw._SNAP.update(err=False); await gw.budget_check(ctx(gw, a, "gemini"))
        assert not pe._RES
    with_ws(go)

# ---------------------------------------------------------------- admin API: limits + roles
def test_limits_api_sets_day_month_provider_caps_only_in_own_workspace():
    async def go(A, admin, gw, gp, pe, W):
        a, b = W["a"], W["b"]
        gcap = await cap(None, "workspace", str(a), "month", 7.0, workspace=False)           # whole-machine cap on ws a: never touched by the convenience API
        bcap = await cap(b, "workspace", str(b), "day", 3.0)
        out = await as_user(admin, a, lambda: pe.limits_put(pe.LimitsIn(day_usd=2.5, month_usd=40, provider_caps={"anthropic": pe.Slot(month_usd=10)})))
        assert out["tz"] == "Asia/Ho_Chi_Minh" and "ICT" in out["tz_note"]["vi"] and "ICT" in out["tz_note"]["en"]
        assert out["workspace"]["day"]["limit_usd"] == 2.5 and out["workspace"]["month"]["limit_usd"] == 40 and out["provider_caps"]["anthropic"]["month"]["limit_usd"] == 10
        assert out["workspace"]["day"]["period_start"].endswith("+00:00") and out["workspace"]["day"]["current_spend_usd"] == 0
        out = await as_user(admin, a, lambda: pe.limits_put(pe.LimitsIn(day_usd=None)))          # null removes the day cap, the rest is untouched
        assert out["workspace"]["day"] is None and out["workspace"]["month"]["limit_usd"] == 40
        out = await as_user(admin, a, lambda: pe.limits_put(pe.LimitsIn(month_usd=45)))
        assert len(await raw("SELECT 1 FROM spend_caps WHERE workspace_id=%s AND scope='workspace' AND period='month'", (a,))) == 1   # replaced, not duplicated
        assert (await raw("SELECT limit_usd FROM spend_caps WHERE id=%s", (gcap,)))[0]["limit_usd"] == 7 and (await raw("SELECT limit_usd FROM spend_caps WHERE id=%s", (bcap,)))[0]["limit_usd"] == 3
        assert (await as_user(admin, b, lambda: pe.limits_get()))["workspace"]["day"]["limit_usd"] == 3.0 and (await as_user(admin, b, lambda: pe.limits_get()))["workspace"]["month"] is None
        await usage(a, 45.0)
        with pytest.raises(gw.BudgetExceeded): await gw.budget_check(ctx(gw, a, "gemini", strict=True))
        # validation: negative, NaN, infinity, unknown provider
        for bad in ({"day_usd": -1}, {"month_usd": float("nan")}, {"day_usd": float("inf")}, {"month_usd": 10**9}):
            with pytest.raises(Exception): pe.LimitsIn(**bad)
        with pytest.raises(HTTPException) as e: await as_user(admin, a, lambda: pe.limits_put(pe.LimitsIn(provider_caps={"nope": pe.Slot(day_usd=1)})))
        assert e.value.status_code == 400
    with_ws(go)

def test_model_access_api_validation_and_roles():
    import admin
    async def go(A, admin_, gw, gp, pe, W):
        a = W["a"]
        for role in ("editor", "viewer"):
            for call in (lambda: pe.access_put(pe.AccessIn(mode="all")), lambda: pe.access_get(), lambda: pe.limits_put(pe.LimitsIn(day_usd=1)), lambda: pe.limits_get()):
                with pytest.raises(HTTPException) as e: await as_user(admin_, a, call, role=role)
                assert e.value.status_code == 403
        with pytest.raises(HTTPException) as e: await as_user(admin_, a, lambda: pe.access_put(pe.AccessIn(mode="selected")))
        assert e.value.status_code == 422                                                       # an empty "selected" list would block every model
        with pytest.raises(HTTPException) as e: await as_user(admin_, a, lambda: pe.access_put(pe.AccessIn(mode="selected", providers=["nope"])))
        assert e.value.status_code == 400
        with pytest.raises(HTTPException) as e: await as_user(admin_, a, lambda: pe.access_put(pe.AccessIn(mode="selected", models=["nope/model-x"])))
        assert e.value.status_code == 400
        with pytest.raises(Exception): pe.AccessIn(mode="everything")
        assert not await raw("SELECT 1 FROM gw_model_access WHERE workspace_id=%s", (a,))
    with_ws(go)
    # the middleware gate (admin.allowed) refuses non-admins before the routes run, for GET and PUT alike
    for path in ("/api/gw/model-access", "/api/gw/limits"):
        for m in ("GET", "PUT"):
            assert admin.allowed("admin", m, path) and not admin.allowed("editor", m, path) and not admin.allowed("viewer", m, path) and not admin.allowed("admin", m, path, demo=True)

# ---------------------------------------------------------------- the two /v1 routes themselves (OpenAI + Anthropic shape)
def test_v1_routes_402_403_other_workspace_and_reservation_freed():
    import gw_api as G, ext, gw_policy_engine as pe, gateway as gw
    from test_gd10_security import Req, with_db, CHAT, ANTH   # helpers of the 10.x tests: 2 workspaces with a virtual key each + a fake google-genai
    async def go(A, admin, W):
        a, b = W["a"], W["b"]; ka, kb = "Bearer " + a["vk"], "Bearer " + b["vk"]
        try:
            cid = await cap(a["ws"], "workspace", str(a["ws"]), "month", 0)
            n0 = A._g.calls
            with pytest.raises(HTTPException) as e: await G.chat_completions(Req(CHAT), authorization=ka)
            assert e.value.status_code == 402 and MSG.match(e.value.detail["error"]["message"])
            r = await ext.anth_messages(Req(ANTH), x_api_key=a["vk"], authorization=""); assert r.status_code == 402 and b"api_error" in r.body
            assert A._g.calls == n0 and not pe._RES                                                        # nothing reached the model, nothing reserved
            assert (await G.chat_completions(Req(CHAT), authorization=kb)).status_code == 200              # the other workspace is not affected
            assert (await ext.anth_messages(Req(ANTH), x_api_key=b["vk"], authorization="")).status_code == 200
            assert not pe._RES                                                                              # a finished call frees its reservation
            await raw("DELETE FROM spend_caps WHERE id=%s", (cid,))
            await raw("INSERT INTO gw_model_access(workspace_id,mode,providers) VALUES(%s,'selected','{openai}')", (a["ws"],)); pe.invalidate()
            n0 = A._g.calls
            with pytest.raises(HTTPException) as e: await G.chat_completions(Req(CHAT), authorization=ka)
            assert e.value.status_code == 403 and e.value.detail["error"]["type"] == "permission_error"
            r = await ext.anth_messages(Req(ANTH), x_api_key=a["vk"], authorization=""); assert r.status_code == 403 and b"permission_error" in r.body
            assert A._g.calls == n0 and not pe._RES
            assert (await G.chat_completions(Req(CHAT), authorization=kb)).status_code == 200
            await raw("DELETE FROM gw_model_access WHERE workspace_id=%s", (a["ws"],)); pe.invalidate()
            # streaming keeps the reservation until the stream is consumed
            async def one(model, contents, config):
                async def it(): yield (await A._g._gen(model, contents, config))
                return it()
            A._g.aio.models.generate_content_stream = one
            r = await G.chat_completions(Req({**CHAT, "stream": True}), authorization=ka); assert len(pe._RES) == 1
            async for _ in r.body_iterator: pass
            assert not pe._RES
            r = await ext.anth_messages(Req({**ANTH, "stream": True}), x_api_key=a["vk"], authorization=""); assert len(pe._RES) == 1
            async for _ in r.body_iterator: pass
            assert not pe._RES
        finally:
            pe._RES.clear(); pe.invalidate()
            for w in (a["ws"], b["ws"]): await raw("DELETE FROM gw_model_access WHERE workspace_id=%s", (w,))
    with_db(go)

# ---------------------------------------------------------------- the HTTP routes on the local server (admin allowed, viewer refused, validation)
def test_http_routes_admin_ok_viewer_refused():
    import httpx
    from test_fixes import B, ADMIN, VIEWER
    with httpx.Client(base_url=B, auth=ADMIN, timeout=60) as a, httpx.Client(base_url=B, auth=VIEWER, timeout=60) as v:
        lim = a.get("/api/gw/limits"); assert lim.status_code == 200 and lim.json()["tz"] == "Asia/Ho_Chi_Minh" and "ICT" in lim.json()["tz_note"]["vi"], lim.text
        caps = a.get("/api/gateway/caps").json(); assert caps["tz"] == "Asia/Ho_Chi_Minh" and all("current_spend_usd" in c and "period_start" in c for c in caps["caps"])
        assert a.get("/api/gw/model-access").json()["mode"] in ("all", "selected")
        assert a.put("/api/gw/model-access", json={"mode": "selected"}).status_code == 422            # empty "selected" would block everything
        assert a.put("/api/gw/model-access", json={"mode": "selected", "providers": ["nope"]}).status_code == 400
        assert a.put("/api/gw/model-access", json={"mode": "bogus"}).status_code == 422
        assert a.put("/api/gw/limits", json={"day_usd": -1}).status_code == 422
        assert a.put("/api/gw/limits", json={"day_usd": 1e12}).status_code == 422
        if not lim.json()["workspace"]["month"]:                                                      # do not touch limits an operator already set
            r = a.put("/api/gw/limits", json={"month_usd": 123.45}); assert r.status_code == 200 and r.json()["workspace"]["month"]["limit_usd"] == 123.45, r.text
            try: assert any(c["scope"] == "workspace" and c["limit_usd"] == 123.45 for c in a.get("/api/gateway/caps").json()["caps"])   # the old caps API sees the same row
            finally: assert a.put("/api/gw/limits", json={"month_usd": None}).json()["workspace"]["month"] is None
        for method, path, body in (("GET", "/api/gw/limits", None), ("PUT", "/api/gw/limits", {"day_usd": 1}), ("GET", "/api/gw/model-access", None), ("PUT", "/api/gw/model-access", {"mode": "all"})):
            r = v.request(method, path, json=body); assert r.status_code == 403, (method, path, r.status_code)
        assert httpx.get(B + "/api/gw/limits").status_code in (401, 403)                                # no credentials at all
