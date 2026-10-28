"""GĐ11.1 provider registry + per-workspace keys (and 11.2 resolver tests appended below). In-process on a private pool (app RLS role), 2 workspaces,
fake provider servers on 127.0.0.1 (tests/fakes/fake_providers.py). No real network, no real LLM."""
import asyncio, json, logging, os, sys, uuid
import psycopg, pytest
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app")); sys.path.insert(0, HERE)
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")
import deepagent_api   # noqa: F401,E402  (first app import: modules import each other circularly)
from fakes.fake_providers import FakeProviders  # noqa: E402

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos"); os.environ.setdefault("PG_URI", PG)   # crypto.py reads the DEKs through PG_URI
run = asyncio.run
KEY = "sk-fake-provider-key-1234"

def with_ws(fn):
    """Run `await fn(A, admin, gp, W)` with the migration applied, RLS on gw_providers and two fresh workspaces; clean up afterwards."""
    import deepagent_api as A, admin, gw_providers as gp, migrations
    async def go():
        old = (A.S.get("pool"), A.S.get("rls"))
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as c: await migrations.run_pending(c)
        async with AsyncConnectionPool(PG, min_size=1, max_size=4, open=False, kwargs={"autocommit": True, "row_factory": dict_row}) as pool:
            await pool.open(); A.S["pool"] = pool; A.S["rls"] = admin.rls_ctx
            await admin.rls_grants()
            await admin._policy("gw_providers", "aos_ws() IS NULL OR workspace_id = aos_ws()")
            tag = uuid.uuid4().hex[:8]; W = {"tag": tag}
            for s in "ab": W[s] = (await A.q("INSERT INTO workspaces(name) VALUES(%s) RETURNING id", (f"p111-{s}-{tag}",), one=True))["id"]
            try: return await fn(A, admin, gp, W)
            finally:
                for s in "ab":
                    for sql in ("DELETE FROM gw_providers WHERE workspace_id=%s", "DELETE FROM ws_secrets WHERE workspace_id=%s", "DELETE FROM workspaces WHERE id=%s"):
                        try: await A.q(sql, (W[s],))
                        except Exception as e: print("cleanup:", str(e)[:80])   # noqa: BLE001
                A.S["pool"], A.S["rls"] = old
    return run(go())

async def as_user(admin, ws, fn, role="admin", demo=False):
    t = admin.USER.set({"id": 987655, "username": "t111", "role": role, "ws": ws, "auth": "test", **({"demo": True} if demo else {})})
    try: return await fn()
    finally: admin.USER.reset(t)

@pytest.fixture
def fp(monkeypatch):
    monkeypatch.setenv("AOS_EGRESS_ALLOW", "127.0.0.0/8,::1")
    with FakeProviders(key=KEY) as s: yield s

def test_crud_and_key_never_leaks(fp, caplog):
    caplog.set_level(logging.DEBUG)
    async def go(A, admin, gp, W):
        a = W["a"]; bodies = []
        row = await as_user(admin, a, lambda: gp.create_provider(gp.ProviderIn(kind="openai_compat", name="mine", base_url=fp.url("openai"), api_key=KEY)))
        bodies.append(json.dumps(row, default=str)); assert row["key_last4"] == KEY[-4:] and row["has_key"] and row["name"] == "mine"
        lst = await as_user(admin, a, gp.list_providers); bodies.append(json.dumps(lst, default=str))
        assert [p["name"] for p in lst["providers"] if not p["system"]] == ["mine"]
        assert all("secret_name" not in p and "api_key" not in p for p in lst["providers"])
        sec = await A.q("SELECT value_enc, allowed_hosts FROM ws_secrets WHERE workspace_id=%s AND name=%s", (a, row and "OPENAI_COMPAT_API_KEY_MINE"), one=True)
        assert sec and KEY not in sec["value_enc"] and sec["allowed_hosts"] == ["127.0.0.1"]
        new = await as_user(admin, a, lambda: gp.update_provider(row["id"], gp.ProviderUpd(api_key=KEY + "zzzz", enabled=False)))
        bodies.append(json.dumps(new, default=str)); assert new["enabled"] is False and new["key_last4"] == "zzzz" and new["status"] == "untested"
        with pytest.raises(HTTPException) as e: await as_user(admin, a, lambda: gp.update_provider(row["id"], gp.ProviderUpd(base_url="http://127.0.0.2:9/v1")))
        assert e.value.status_code == 422                                   # new address needs the key typed again
        await as_user(admin, a, lambda: gp.update_provider(row["id"], gp.ProviderUpd(base_url="http://127.0.0.2:9/v1", api_key=KEY)))
        sec = await A.q("SELECT allowed_hosts FROM ws_secrets WHERE workspace_id=%s AND name=%s", (a, "OPENAI_COMPAT_API_KEY_MINE"), one=True); assert sec["allowed_hosts"] == ["127.0.0.2"]
        with pytest.raises(HTTPException) as e: await as_user(admin, a, lambda: gp.create_provider(gp.ProviderIn(kind="openai_compat", name="mine", base_url=fp.url("openai"))))
        assert e.value.status_code == 409
        await as_user(admin, a, lambda: gp.delete_provider(row["id"]))
        assert not await A.q("SELECT 1 FROM ws_secrets WHERE workspace_id=%s", (a,)) and not await A.q("SELECT 1 FROM gw_providers WHERE workspace_id=%s", (a,))
        assert not any(KEY in b for b in bodies) and KEY not in caplog.text
    with_ws(go)

def test_test_connection_ok_fail_and_no_leak_in_errors(fp):
    async def go(A, admin, gp, W):
        a = W["a"]; mk = lambda kind, key, **k: as_user(admin, a, lambda: gp.create_provider(gp.ProviderIn(kind=kind, api_key=key, base_url=fp.url(kind), **k)))
        ok = await mk("openai", KEY); r = await as_user(admin, a, lambda: gp.test_provider(ok["id"]))
        assert r["ok"] and r["models"] == 2 and not any(c["path"] == "/v1/chat/completions" for c in fp.calls)   # free check only
        r = await as_user(admin, a, lambda: gp.test_provider(ok["id"], gp.TestIn(confirm=True)))
        cc = [c for c in fp.calls if c["path"] == "/v1/chat/completions"]; assert r["ok"] and len(cc) == 1 and cc[0]["body"]["max_completion_tokens"] == 16
        assert (await as_user(admin, a, gp.list_providers))["providers"][0]["status"] == "ok"
        ant = await mk("anthropic", KEY); assert (await as_user(admin, a, lambda: gp.test_provider(ant["id"], gp.TestIn(confirm=True))))["ok"]
        assert fp.calls[-1]["path"] == "/v1/messages" and fp.calls[-1]["headers"]["x-api-key"] == KEY
        gem = await mk("gemini", KEY); assert (await as_user(admin, a, lambda: gp.test_provider(gem["id"], gp.TestIn(confirm=True))))["ok"]
        assert (await as_user(admin, a, lambda: gp.provider_models(ok["id"])))["models"] == [{"id": "fake-model-a"}, {"id": "fake-model-b"}]
        bad = await as_user(admin, a, lambda: gp.create_provider(gp.ProviderIn(kind="openai_compat", name="bad", base_url=fp.url("openai"), api_key="wrong-key-0000000000")))
        r = await as_user(admin, a, lambda: gp.test_provider(bad["id"])); assert not r["ok"] and "401" in r["detail"] and "wrong-key" not in json.dumps(r)
        fp.status = 500
        r = await as_user(admin, a, lambda: gp.test_provider(ok["id"], gp.TestIn(confirm=True))); assert not r["ok"] and KEY not in json.dumps(r)
        row = await A.q("SELECT status, last_test_result FROM gw_providers WHERE id=%s", (ok["id"],), one=True); assert row["status"] == "failed" and KEY not in row["last_test_result"]
        fp.status = 200
    with_ws(go)

def test_workspace_isolation_and_credentials(fp):
    async def go(A, admin, gp, W):
        a, b = W["a"], W["b"]
        row = await as_user(admin, a, lambda: gp.create_provider(gp.ProviderIn(kind="anthropic", base_url=fp.url("anthropic"), api_key=KEY)))
        assert [p for p in (await as_user(admin, b, gp.list_providers))["providers"] if not p["system"]] == []
        for fn in (lambda: gp.update_provider(row["id"], gp.ProviderUpd(enabled=False)), lambda: gp.delete_provider(row["id"]), lambda: gp.test_provider(row["id"]), lambda: gp.provider_models(row["id"])):
            with pytest.raises(HTTPException) as e: await as_user(admin, b, fn)
            assert e.value.status_code == 404
        assert not await as_user(admin, b, lambda: A.q("SELECT 1 FROM gw_providers"))                     # RLS: B sees no rows
        assert not await as_user(admin, b, lambda: A.q("SELECT 1 FROM ws_secrets WHERE name='ANTHROPIC_API_KEY'"))   # nor keys
        assert await gp.credentials(a, "anthropic") == (KEY, fp.url("anthropic"))
        with pytest.raises(LookupError): await gp.credentials(b, "anthropic")
        await as_user(admin, a, lambda: gp.update_provider(row["id"], gp.ProviderUpd(enabled=False)))
        with pytest.raises(LookupError): await gp.credentials(a, "anthropic")
        import gateway
        gateway.DEMO_WS.add(a)
        try:
            with pytest.raises(PermissionError): await gp.credentials(a, "anthropic")
        finally: gateway.DEMO_WS.discard(a)
    with_ws(go)

def test_internal_url_refused_unless_allow_listed(monkeypatch):
    async def go(A, admin, gp, W):
        a = W["a"]; monkeypatch.delenv("AOS_EGRESS_ALLOW", raising=False)
        for url in ("http://127.0.0.1:9/v1", "https://10.0.0.5/v1"):
            with pytest.raises(HTTPException) as e: await as_user(admin, a, lambda: gp.create_provider(gp.ProviderIn(kind="openai_compat", name="loc", base_url=url, api_key=KEY)))
            assert e.value.status_code == 400 and e.value.detail == gp.MSG_INTERNAL
        assert not await A.q("SELECT 1 FROM ws_secrets WHERE workspace_id=%s", (a,))                       # nothing stored on refusal
        monkeypatch.setenv("AOS_EGRESS_ALLOW", "127.0.0.0/8")
        r = await as_user(admin, a, lambda: gp.create_provider(gp.ProviderIn(preset="ollama", api_key=None))); assert r["kind"] == "openai_compat" and r["name"] == "ollama" and not r["has_key"]
        monkeypatch.delenv("AOS_EGRESS_ALLOW")                                                              # revoked later: every call re-checks
        import netguard
        with pytest.raises(netguard.NetBlocked): await gp.credentials(a, "ollama")
    with_ws(go)

def test_roles_demo_and_admin_only_prefix():
    import admin, gw_providers as gp
    for role in ("editor", "viewer"):
        for m in ("GET", "POST", "PATCH", "DELETE"): assert not admin.allowed(role, m, "/api/gw/providers/1/test")
    assert admin.allowed("admin", "POST", "/api/gw/providers") and not admin.allowed("viewer", "GET", "/api/gw/providers", demo=True)
    assert "gw_providers" in admin.RLS_DIRECT
    async def go():
        for kw in ({"role": "editor"}, {"role": "viewer"}, {"role": "viewer", "demo": True}):
            with pytest.raises(HTTPException) as e: await as_user(admin, 1, lambda: gp.create_provider(gp.ProviderIn(kind="gemini", api_key=KEY)), **kw)
            assert e.value.status_code == 403
    run(go())

def test_validation_and_system_providers(monkeypatch):
    import gw_providers as gp
    monkeypatch.setenv("GOOGLE_API_KEY", "g-secret-value-123456"); monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    sp = gp.system_providers(); assert [s["name"] for s in sp] == ["gemini"] and "g-secret" not in json.dumps(sp) and sp[0]["key_last4"] is None
    async def go(A, admin, gp, W):
        a = W["a"]
        for bad in (dict(kind="nope", api_key=KEY), dict(kind="openai", name="aos", api_key=KEY), dict(kind="openai", name="gemini", api_key=KEY), dict(kind="openai", name="Bad Name", api_key=KEY),
                    dict(kind="openai"), dict(kind="openai_compat", name="x"), dict(preset="nope"), dict(kind="openai", api_key=KEY, base_url="ftp://x"), dict(kind="openai", api_key=KEY, base_url="https://u:p@api.openai.com/v1")):
            with pytest.raises(HTTPException) as e: await as_user(admin, a, lambda: gp.create_provider(gp.ProviderIn(**bad)))
            assert e.value.status_code in (422, 400), bad
    with_ws(go)

# ---------------- 11.2 model-name resolver
LEGACY = [("gemini-3.6-flash", "gemini", "gemini-3.6-flash", "gemini-3.6-flash"), ("gemini-flash-lite-latest", "gemini", "gemini-flash-lite-latest", "gemini-flash-lite-latest"),
          ("gpt-5.4", "openai", "gpt-5.4", "gpt-5.4"), ("o3-mini", "openai", "o3-mini", "o3-mini"), ("o4-mini", "openai", "o4-mini", "o4-mini"),
          ("claude-haiku-5-5", "anthropic", "claude-haiku-5-5", "anthropic/claude-haiku-5-5"),
          ("totally-unknown", "gemini", "totally-unknown", "totally-unknown"),                       # before 11.2 every other bare name was Gemini
          ("anthropic/claude-haiku-5-5", "anthropic", "claude-haiku-5-5", "anthropic/claude-haiku-5-5"),
          ("gemini/gemini-3.6-flash", "gemini", "gemini-3.6-flash", "gemini-3.6-flash"), ("openai/gpt-5.4", "openai", "gpt-5.4", "gpt-5.4")]

@pytest.mark.parametrize("ref,kind,model,canon", LEGACY)
def test_resolver_table(ref, kind, model, canon):
    import gw_router as R, gateway as gw
    r = R.resolve(ref); assert (r.kind, r.model, r.ref) == (kind, model, canon)
    if "/" not in ref: assert gw.provider(ref) == ("openai" if ref.startswith(("gpt-", "o3", "o4")) else "anthropic" if ref.startswith("claude-") else "gemini")
    assert R.canonical(ref) == canon

def test_resolver_tiers_custom_and_errors():
    import gw_router as R
    for t, m in R.TIERS.items(): assert R.resolve("aos/" + t).model == m and R.resolve("aos/" + t).kind == "gemini"
    for bad in ("x/y", "aos/ultra", "custom/foo", "anthropic/", "/gpt-5", "openai_compat/m"):
        with pytest.raises(R.ResolveError): R.resolve(bad)
    assert "anthropic" in str(pytest.raises(R.ResolveError, R.resolve, "x/y").value)
    with pytest.raises(HTTPException) as e: R.http_resolve("x/y")
    assert e.value.status_code == 400
    assert R.canonical("x/y") == "x/y" and R.kind_of("x/y") == "gemini"                                  # sync helpers never raise
    with pytest.raises(HTTPException) as e: R.v1_gemini("anthropic/claude-haiku-5-5")
    assert e.value.detail == R.V1_ONLY_GEMINI and R.v1_gemini("gemini/gemini-3.6-flash") == "gemini-3.6-flash" and R.v1_gemini("aos/fast") == R.TIERS["fast"]

def test_resolver_workspace_provider_and_cooldown(fp):
    async def go(A, admin, gp, W):
        import gw_router as R, gateway as gw
        a, b = W["a"], W["b"]
        await as_user(admin, a, lambda: gp.create_provider(gp.ProviderIn(preset="deepseek", base_url=fp.url("openai"), api_key=KEY)))
        r = await R.aresolve("deepseek/deepseek-flash", a); assert (r.provider, r.kind, r.model, r.ref) == ("deepseek", "openai_compat", "deepseek-flash", "deepseek/deepseek-flash") and r.row["name"] == "deepseek"
        with pytest.raises(HTTPException): await R.aresolve("deepseek/deepseek-flash", b)                   # other workspace: no such provider
        t = admin.USER.set({"id": 1, "username": "u", "role": "admin", "ws": a})
        try: assert gw.provider("deepseek/deepseek-flash") == "openai_compat" and gw._cn("gemini/gemini-3.6-flash") == "gemini-3.6-flash"
        finally: admin.USER.reset(t)
        gw.COOLDOWN.clear(); gw._mark("gemini/gemini-3.6-flash", "429 RESOURCE_EXHAUSTED")
        assert "gemini-3.6-flash" in gw.COOLDOWN and "gemini-3.6-flash" not in gw._usable(["gemini-3.6-flash", "gemini-3.1-flash-lite"]); gw.COOLDOWN.clear()
    with_ws(go)


# ---------------------------------------------------------------- GĐ11.3: group-1 providers through init_chat_model (fake Claude / fake GPT / fake DeepSeek)
async def _mk(admin, gp, ws, **kw): return await as_user(admin, ws, lambda: gp.create_provider(gp.ProviderIn(api_key=KEY, **kw)))

def test_generate_on_fake_claude_gpt_and_compat_with_usage_rows(fp):
    async def go(A, admin, gp, W):
        import gateway as gw
        a = W["a"]; old = gw.PRICING.get("deepseek/deepseek-flash"); gw.PRICING["deepseek/deepseek-flash"] = {"input_per_1m": 0.3, "output_per_1m": 1.2}
        await _mk(admin, gp, a, kind="anthropic", base_url=fp.url("anthropic")); await _mk(admin, gp, a, kind="openai", base_url=fp.url("openai"))
        await _mk(admin, gp, a, preset="deepseek", base_url=fp.url("openai")); fp.usage = (1000, 500)
        async def run():
            out = {}
            for ref in ("anthropic/claude-haiku-5-5", "gpt-5.4-nano", "deepseek/deepseek-flash"):
                gw.COOLDOWN.clear(); fp.calls.clear()
                txt, m, i, o = await gw.generate(None, "hello", [ref], temperature=0.3, feature="playground"); out[ref] = (txt, m, i, o, [c for c in fp.calls if c["body"]])
            fp.calls.clear()
            r = await gw.lc_call(["anthropic/claude-sonnet-5-5"], [("human", "hi")], temperature=0.2, params={"max_tokens": 77}, feature="engine")   # LangChain-v1 path (judge / agents use it)
            return out, r
        out, r = await as_user(admin, a, run)
        for ref, (txt, m, i, o, calls) in out.items(): assert txt == "pong" and m == ref and (i, o) == (1000, 500), ref
        cl = out["anthropic/claude-haiku-5-5"][4][0]; assert cl["path"] == "/v1/messages" and cl["headers"]["x-api-key"] == KEY and cl["body"]["model"] == "claude-haiku-5-5" and cl["body"]["temperature"] == 0.3 and cl["body"]["max_tokens"] == 4096
        gp_ = out["gpt-5.4-nano"][4][0]; assert gp_["path"] == "/v1/chat/completions" and gp_["headers"]["authorization"] == "Bearer " + KEY and "temperature" not in gp_["body"] and gp_["body"]["max_completion_tokens"] == gw.MAX_TOKENS
        ds = out["deepseek/deepseek-flash"][4][0]; assert ds["body"]["model"] == "deepseek-flash" and ds["body"]["temperature"] == 0.3
        assert r["message"].content == "pong" and r["model"] == "anthropic/claude-sonnet-5-5"
        rows = await A.q("SELECT model, provider, input_tokens, output_tokens, cost_usd, status FROM gateway_usage WHERE workspace_id=%s ORDER BY id", (a,))
        by = {x["model"]: x for x in rows}
        assert by["anthropic/claude-haiku-5-5"]["provider"] == "anthropic" and abs(float(by["anthropic/claude-haiku-5-5"]["cost_usd"]) - (1000 * 0.10 + 500 * 0.50) / 1e6) < 1e-9   # 11.16 price
        assert by["gpt-5.4-nano"]["provider"] == "openai" and by["deepseek/deepseek-flash"]["provider"] == "deepseek" and abs(float(by["deepseek/deepseek-flash"]["cost_usd"]) - (300 + 600) / 1e6) < 1e-9
        await A.q("DELETE FROM gateway_usage WHERE workspace_id=%s", (a,)); gw.PRICING.pop("deepseek/deepseek-flash"); old and gw.PRICING.__setitem__("deepseek/deepseek-flash", old)
    with_ws(go)

def test_agent_middleware_on_fake_claude(fp):
    async def go(A, admin, gp, W):
        import gateway as gw
        from langchain.agents import create_agent
        a = W["a"]; await _mk(admin, gp, a, kind="anthropic", base_url=fp.url("anthropic")); fp.usage = (10, 3)
        ag = create_agent(model=gw._model("gemini-3.6-flash", 0), tools=[], middleware=[gw.GatewayMiddleware(None, "anthropic/claude-haiku-5-5", [], 0.1)])
        async def run(): return await ag.ainvoke({"messages": [("user", "hi")]})
        gw.COOLDOWN.clear(); res = await as_user(admin, a, run)
        assert res["messages"][-1].content == "pong" and fp.calls[-1]["path"] == "/v1/messages" and fp.calls[-1]["headers"]["x-api-key"] == KEY
        await A.q("DELETE FROM gateway_usage WHERE workspace_id=%s", (a,))
    with_ws(go)

def test_missing_or_disabled_key_gives_424_text(fp):
    async def go(A, admin, gp, W):
        import gateway as gw
        a, b = W["a"], W["b"]; row = await _mk(admin, gp, a, kind="anthropic", base_url=fp.url("anthropic"))
        async def gen(): return await gw.generate(None, "x", ["anthropic/claude-haiku-5-5"])
        with pytest.raises(gw.ProviderKeyMissing, match="Thiếu khoá Anthropic"): await as_user(admin, b, gen)      # workspace B has no Anthropic provider
        await as_user(admin, a, lambda: gp.update_provider(row["id"], gp.ProviderUpd(enabled=False))); gp.CRED.clear()
        with pytest.raises(gw.ProviderKeyMissing, match="đang tắt"): await as_user(admin, a, gen)
        with pytest.raises(gw.ProviderKeyMissing): await as_user(admin, b, lambda: gw.lc_call(["anthropic/claude-haiku-5-5"], [("human", "x")]))
        assert all(c["path"] != "/v1/messages" for c in fp.calls)                                                  # nothing was sent
        r = await A._provider_key_missing(None, gw.ProviderKeyMissing("Thiếu khoá Anthropic")); assert r.status_code == 424 and "Thiếu khoá Anthropic" in r.body.decode()
        with pytest.raises(gw.ProviderKeyMissing): gw._model("anthropic/claude-haiku-5-5", 0)                      # sync path fails closed too (no prepare, no workspace)
    with_ws(go)

def test_demo_stays_gemini_only(fp):
    async def go(A, admin, gp, W):
        import gateway as gw, gw_providers, gw_router, time
        a = W["a"]; await _mk(admin, gp, a, kind="anthropic", base_url=fp.url("anthropic")); gw.DEMO_WS.add(a)
        try:
            async def chk():
                assert gw.gemini_only() and all(gw.provider(m) == "gemini" for m in gw._usable(["anthropic/claude-haiku-5-5", "gpt-5.4-nano"]))
                with pytest.raises(gw.DemoOpenAIBlocked): await gw.prepare("anthropic/claude-haiku-5-5")
            await as_user(admin, a, chk, role="viewer", demo=True)
            res = gw_router.resolve("anthropic/claude-haiku-5-5"); llm = gw_providers.build_model(res, {}, (time.time(), KEY, fp.base))
            async def blocked(): await llm.ainvoke([("human", "hi")])
            with pytest.raises(gw.DemoOpenAIBlocked): await as_user(admin, a, blocked, role="viewer", demo=True)
            assert not [c for c in fp.calls if c["path"] == "/v1/messages"]                                        # refused before any request
        finally: gw.DEMO_WS.discard(a)
    with_ws(go)

def test_params_are_filtered_with_one_warning(caplog, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-never-used")
    import gw_providers as gp, gw_router as R
    caplog.set_level(logging.WARNING, logger="aos.gwprov"); gp._WARNED.clear()
    cred = (0, KEY, "http://127.0.0.1:1")
    anth = gp.build_model(R.resolve("anthropic/claude-haiku-5-5"), {"temperature": 0.5, "top_k": 7, "thinking": {"type": "enabled", "budget_tokens": 2048}, "service_tier": "flex", "extra_params": '{"top_k": 9}'}, cred)
    assert anth.top_k == 9 and anth.thinking["budget_tokens"] == 2048 and anth.temperature is None and anth.max_tokens == 4096      # thinking drops temperature; extra_params wins
    gp.build_model(R.resolve("anthropic/claude-haiku-5-5"), {"service_tier": "flex", "temperature": 0.4}, cred)
    assert len([r for r in caplog.records if "service_tier" in r.message]) == 1                                                    # unsupported key: dropped, ONE warning
    son = gp.build_model(R.resolve("anthropic/claude-sonnet-5-5"), {"temperature": 0.3, "top_k": 4, "top_p": 0.9, "thinking": {"type": "enabled", "budget_tokens": 1024}, "reasoning_effort": "low"}, cred)
    assert son.temperature is None and son.top_k is None and son.top_p is None and son.thinking is None and son.reasoning_effort == "low"   # Claude Sonnet 5.5 refuses these: dropped, not sent
    oa = gp.build_model(R.resolve("gpt-5.6-terra"), {"reasoning_effort": "low", "service_tier": "flex", "use_responses_api": True, "top_k": 3, "parallel_tool_calls": False, "temperature": 0.9}, None)
    assert oa.reasoning_effort == "low" and oa.service_tier == "flex" and oa.use_responses_api is True and oa.temperature is None and oa.model_kwargs == {"parallel_tool_calls": False}
    ge = gp.build_model(R.resolve("gemini-3.6-flash"), {"top_k": 5, "safety": None, "reasoning_effort": None}, None); assert ge.top_k == 5 and type(ge).__name__ == "ChatGoogleGenerativeAI"

def test_spend_query_provider_column_and_old_rows():
    import gateway as gw
    sql, a = gw.spend_query({"scope": "provider", "target": "anthropic", "period": "total", "workspace_id": None})
    assert "coalesce(provider," in sql and a == ("anthropic",) and "claude-%%" in sql

def test_usage_cache_write_1h_details():
    import gateway as gw
    d = gw.details_from_usage({"input_token_details": {"cache_read": 100, "cache_creation": 0, "ephemeral_5m_input_tokens": 200, "ephemeral_1h_input_tokens": 300}})
    assert d == {"cached": 100, "cache_write": 200, "cache_write_1h": 300}

def test_real_gemini_call_recorded_once_replays_cost():
    """tests/fixtures/gd11_3_gemini_real.json = the ONE real 16-token Gemini call through init_chat_model (26/10/2026, ~$0.000005; answered by the last-resort alias, see its note): replayed here, no network."""
    import gateway as gw, gw_providers as gp, gw_router as R
    rec = json.load(open(os.path.join(HERE, "fixtures", "gd11_3_gemini_real.json"), encoding="utf-8")); u = rec["usage_metadata"]
    assert rec["content"] == "pong" and type(gp.build_model(R.resolve(rec["model"]), {"max_tokens": 16}, None)).__name__ == "ChatGoogleGenerativeAI"
    assert gw.cost(rec["model"], u["input_tokens"], u["output_tokens"], gw.details_from_usage(u)) == rec["cost_usd"]

# ---------------------------------------------------------------- 11.10 prompt cache for internal agents
def test_cache_control_only_on_anthropic_attempts_and_priced_once(fp):
    async def go(A, admin, gp, W):
        import gateway as gw
        from langchain.agents import create_agent
        a = W["a"]; await _mk(admin, gp, a, kind="anthropic", base_url=fp.url("anthropic"))
        with FakeProviders(key=KEY, usage=(7, 3)) as oa:
            await _mk(admin, gp, a, kind="openai", base_url=oa.url("openai"))
            fp.usage, fp.cache = (10, 50), (1000, 200)
            def agent(chain): return create_agent(model=gw._model("gemini-3.6-flash", 0), tools=[], system_prompt="You are a careful assistant.", middleware=[gw.GatewayMiddleware(None, chain[0], chain[1:], 0.1)])
            async def run(chain): gw.COOLDOWN.clear(); return await agent(chain).ainvoke({"messages": [("user", "hi")]})
            await as_user(admin, a, lambda: run(["anthropic/claude-haiku-5-5"]))
            body = [c for c in fp.calls if c["path"] == "/v1/messages"][-1]["body"]
            assert "cache_control" in json.dumps(body) and '"ttl": "5m"' in json.dumps(body)
            fp.status = 503; fp.calls.clear()
            res = await as_user(admin, a, lambda: run(["anthropic/claude-haiku-5-5", "gpt-5.4-nano"]))      # Claude fails -> GPT fallback answers
            assert res["messages"][-1].content == "pong" and [c for c in fp.calls if c["path"] == "/v1/messages"]
            gpt = [c for c in oa.calls if c["path"] == "/v1/chat/completions"][-1]["body"]; assert "cache_control" not in json.dumps(gpt)   # no leak into the fallback
        rows = await A.q("SELECT cost_usd, status FROM gateway_usage WHERE workspace_id=%s AND model='anthropic/claude-haiku-5-5' AND status='ok'", (a,))
        assert len(rows) == 1 and abs(float(rows[0]["cost_usd"]) - (1000 * 0.01 + 200 * 0.125 + 10 * 0.1 + 50 * 0.5) / 1e6) < 1e-9   # read + write priced once, not as plain input
        await A.q("DELETE FROM gateway_usage WHERE workspace_id=%s", (a,))
    with_ws(go)

def test_cache_ttl_1h_only_from_preset():
    import gateway as gw, gw_router
    ws = 987001
    try:
        gw_router.CUSTOM[ws] = {"c1": {"ref": "anthropic/claude-haiku-5-5", "params": {"extra_params": {"cache_ttl": "30m"}}}, "c2": {"ref": "anthropic/claude-haiku-5-5", "params": {"extra_params": '{"cache_ttl": "1h"}'}}, "c3": {"ref": "anthropic/claude-haiku-5-5", "params": {}}}
        import admin
        t = admin.USER.set({"id": 1, "username": "t", "role": "admin", "ws": ws, "auth": "test"})
        try: assert (gw.cache_ttl("custom/c1"), gw.cache_ttl("custom/c2"), gw.cache_ttl("custom/c3"), gw.cache_ttl("anthropic/claude-haiku-5-5"), gw.cache_ttl("gemini-3.6-flash")) == ("1h", "1h", "5m", "5m", "5m")
        finally: admin.USER.reset(t)
    finally: gw_router.CUSTOM.pop(ws, None)
