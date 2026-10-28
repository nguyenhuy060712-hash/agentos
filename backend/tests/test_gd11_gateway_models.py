"""GĐ11.11 model configurations v2 + 11.12 per-feature model access / shared catalog. In-process on a private pool (app RLS role), 2 workspaces,
fake provider servers on 127.0.0.1 (tests/fakes/fake_providers.py). No real network, no real LLM."""
import asyncio, json, os, sys, time, types, uuid
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
TABLES = ("gw_providers", "model_presets", "model_tiers", "model_feature_access")

def with_ws(fn):
    import deepagent_api as A, admin, ext, gw_providers as gp, gw_features as gf, gw_router, migrations
    async def go():
        old = (A.S.get("pool"), A.S.get("rls"))
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as c: await migrations.run_pending(c)
        async with AsyncConnectionPool(PG, min_size=1, max_size=4, open=False, kwargs={"autocommit": True, "row_factory": dict_row}) as pool:
            await pool.open(); A.S["pool"] = pool; A.S["rls"] = admin.rls_ctx
            await admin.rls_grants()
            for t in TABLES: await admin._policy(t, "aos_ws() IS NULL OR workspace_id = aos_ws()")
            tag = uuid.uuid4().hex[:8]; W = {"tag": tag}
            for s in "ab": W[s] = (await A.q("INSERT INTO workspaces(name) VALUES(%s) RETURNING id", (f"m111-{s}-{tag}",), one=True))["id"]
            try: return await fn(A, admin, ext, gp, gf, gw_router, W)
            finally:
                for s in "ab":
                    for sql in ("DELETE FROM model_feature_access WHERE workspace_id=%s", "DELETE FROM model_tiers WHERE workspace_id=%s", "DELETE FROM model_presets WHERE workspace_id=%s",
                                "DELETE FROM gw_providers WHERE workspace_id=%s", "DELETE FROM ws_secrets WHERE workspace_id=%s", "DELETE FROM workspaces WHERE id=%s"):
                        try: await A.q(sql, (W[s],))
                        except Exception as e: print("cleanup:", str(e)[:80])   # noqa: BLE001
                    gw_router.invalidate(W[s])
                    for d in (gw_router.CUSTOM, gw_router.TIER_OVERRIDE): d.pop(W[s], None)
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

@pytest.fixture(autouse=True)
def models(monkeypatch):
    import deepagent_api as A, gateway
    monkeypatch.setattr(gateway, "PRICING", {"gemini-3.6-flash": {"input_per_1m": 1, "output_per_1m": 2}, "gemini-3.1-flash-lite": {"input_per_1m": 1, "output_per_1m": 2},
                                              "gpt-5-nano": {"input_per_1m": 1, "output_per_1m": 2}})
    monkeypatch.setattr(A, "_models_cache", {"t": time.time(), "list": ["gemini-3.6-flash", "gemini-3.1-flash-lite"]})

def P(ext, **k): return ext.PresetIn(**{"name": "c1", "model": "x", **k})

def test_configs_create_clone_default_and_no_secret(fp):
    async def go(A, admin, ext, gp, gf, R, W):
        a = W["a"]; U = lambda fn, **k: as_user(admin, a, fn, **k)
        await U(lambda: gp.create_provider(gp.ProviderIn(kind="openai_compat", name="mine", base_url=fp.url("openai"), api_key=KEY)))
        c = await U(lambda: ext.config_create(P(ext, name="fast-mine", model="fake-model-a", provider="mine", temperature=0.2, max_tokens=99, extra_params={"seed": 1})))
        assert c["provider"] == "mine" and c["upstream_model"] == "fake-model-a" and c["model"] == "mine/fake-model-a" and c["extra_params"] == {"seed": 1}
        assert KEY not in json.dumps(c, default=str)
        cols = {r["column_name"] for r in await A.q("SELECT column_name FROM information_schema.columns WHERE table_name='model_presets'")}
        assert not {x for x in cols if any(w in x for w in ("secret", "key", "header", "token_val"))}
        with pytest.raises(HTTPException) as e: await U(lambda: ext.config_create(P(ext, name="fast-mine", model="fake-model-a", provider="mine")))
        assert e.value.status_code == 409
        with pytest.raises(HTTPException) as e: await U(lambda: ext.config_create(P(ext, name="bad", model="x", provider="nope")))
        assert e.value.status_code == 400
        cl = await U(lambda: ext.config_clone(c["id"])); cl2 = await U(lambda: ext.config_clone(c["id"]))
        assert cl["name"] == "fast-mine copy" and cl2["name"] == "fast-mine copy 2" and cl["max_tokens"] == 99 and cl["provider"] == "mine" and not cl["is_default"]
        await U(lambda: ext.config_default(cl["id"])); await U(lambda: ext.config_default(c["id"]))
        assert [r["name"] for r in await U(ext.preset_list) if r["is_default"]] == ["fast-mine"]
        up = await U(lambda: ext.config_patch(c["id"], ext.PresetUpd(max_tokens=5, model="fake-model-b")))
        assert up["max_tokens"] == 5 and up["model"] == "mine/fake-model-b" and up["provider"] == "mine"
        # workspace B sees nothing
        assert await as_user(admin, W["b"], ext.preset_list) == []
        with pytest.raises(HTTPException) as e: await as_user(admin, W["b"], lambda: ext.config_default(c["id"]))
        assert e.value.status_code == 404
        await U(lambda: ext.preset_del(cl["id"])); assert len(await U(ext.preset_list)) == 2
    with_ws(go)

def test_legacy_presets_alias_still_works():
    async def go(A, admin, ext, gp, gf, R, W):
        a = W["a"]; U = lambda fn: as_user(admin, a, fn)
        r = await U(lambda: ext.preset_set(ext.PresetIn(name="old", model="gemini-3.6-flash", temperature=0.5, fallback_models=["gemini-3.1-flash-lite"])))
        assert r["model"] == "gemini-3.6-flash" and r["provider"] is None and r["provider_api"] == "chat"
        r2 = await U(lambda: ext.preset_set(ext.PresetIn(name="old", model="gemini-3.1-flash-lite", temperature=0.1)))   # upsert by name, as before
        assert r2["id"] == r["id"] and r2["model"] == "gemini-3.1-flash-lite"
        assert [x["name"] for x in await U(ext.preset_list)] == ["old"]
        await U(lambda: ext.preset_del(r["id"])); assert await U(ext.preset_list) == []
    with_ws(go)

def test_custom_ref_and_tier_override(fp):
    async def go(A, admin, ext, gp, gf, R, W):
        a = W["a"]; U = lambda fn, **k: as_user(admin, a, fn, **k)
        await U(lambda: gp.create_provider(gp.ProviderIn(kind="openai_compat", name="mine", base_url=fp.url("openai"), api_key=KEY)))
        await U(lambda: ext.config_create(P(ext, name="x", model="fake-model-a", provider="mine", temperature=0.3, max_tokens=64, reasoning_effort="low")))
        await U(lambda: ext.config_create(P(ext, name="g", model="gemini-3.6-flash", max_tokens=7)))
        r = await R.aresolve("custom/x", a)
        assert (r.provider, r.model, r.ref, r.kind) == ("mine", "fake-model-a", "mine/fake-model-a", "openai_compat")
        assert r.params == {"temperature": 0.3, "max_tokens": 64, "reasoning_effort": "low", "provider_api": "chat"}
        assert (await R.aresolve("custom/g", a)).ref == "gemini-3.6-flash"
        with pytest.raises(HTTPException) as e: await R.aresolve("custom/nope", a)
        assert e.value.status_code == 400
        with pytest.raises(HTTPException) as e: await R.aresolve("custom/x", W["b"])   # another workspace's name does not exist
        assert e.value.status_code == 400
        assert (await R.aresolve("aos/fast", a)).ref == "gemini-3.1-flash-lite"
        t = await U(lambda: ext.tiers_put(ext.TierIn(tier="fast", config_ref="custom/x")))
        assert (await R.aresolve("aos/fast", a)).ref == "mine/fake-model-a" and R.kind_of("aos/fast", a) == "openai_compat"
        assert [x for x in t["tiers"] if x["tier"] == "fast"][0]["effective"] == "mine/fake-model-a"
        assert (await R.aresolve("aos/fast", W["b"])).ref == "gemini-3.1-flash-lite"           # other workspace untouched
        for bad in ("aos/pro", "custom/missing", "zzz/m"):
            with pytest.raises(HTTPException) as e: await U(lambda: ext.tiers_put(ext.TierIn(tier="pro", config_ref=bad)))
            assert e.value.status_code == 400
        xid = (await A.q("SELECT id FROM model_presets WHERE workspace_id=%s AND name='x'", (a,), one=True))["id"]
        with pytest.raises(HTTPException) as e: await U(lambda: ext.preset_del(xid))
        assert e.value.status_code == 409                                                    # still used by a tier
        for role, demo in (("editor", False), ("admin", True)):
            with pytest.raises(HTTPException) as e: await U(lambda: ext.tiers_put(ext.TierIn(tier="max", config_ref="custom/g")), role=role, demo=demo)
            assert e.value.status_code == 403
        await U(lambda: ext.tiers_put(ext.TierIn(tier="fast", config_ref=None)))
        assert (await R.aresolve("aos/fast", a)).ref == "gemini-3.1-flash-lite"
    with_ws(go)

def test_playground_runs_custom_with_preset_params(monkeypatch):
    import deepagent_api as A0
    seen = {}
    class Boom(Exception): pass
    async def gen(model, contents, config): seen.setdefault("model", model); seen.setdefault("cfg", config); raise Boom("stop here")
    monkeypatch.setattr(A0, "_g", types.SimpleNamespace(aio=types.SimpleNamespace(models=types.SimpleNamespace(generate_content=gen))))
    async def go(A, admin, ext, gp, gf, R, W):
        a = W["a"]; U = lambda fn: as_user(admin, a, fn)
        await U(lambda: ext.config_create(P(ext, name="g", model="gemini-3.1-flash-lite", max_tokens=77, temperature=0.4)))
        with pytest.raises(HTTPException) as e: await U(lambda: ext.play_chat(ext.PlayChat(messages=[{"role": "user", "content": "hi"}], model="custom/g")))
        assert e.value.status_code == 400 and "stop here" in str(e.value.detail)           # reached the (fake) model call
        assert seen["model"] == "gemini-3.1-flash-lite" and seen["cfg"].max_output_tokens == 77
        with pytest.raises(HTTPException) as e: await U(lambda: ext.play_chat(ext.PlayChat(messages=[{"role": "user", "content": "hi"}], model="claude-haiku-5-5")))
        assert e.value.status_code == 400                                                    # non-Gemini: 11.3
    with_ws(go)

def test_feature_list_and_unknown_feature(fp):
    async def go(A, admin, ext, gp, gf, R, W):
        a = W["a"]; U = lambda fn, **k: as_user(admin, a, fn, **k)
        fl = (await gf.features_list())["features"]; ids = [f["id"] for f in fl]
        assert len(ids) == 19 and "chat" in ids and all(f["vi"] and f["en"] for f in fl)
        import gateway; assert gateway.FEATURE_VI["chat_title"] == "Đặt tên cuộc chat" and gateway.FEATURE_VI["playground"] == "Playground"
        for fn in (lambda: gf.access_put("nope", gf.AccessIn(providers=["gemini"])), lambda: gf.catalog_get("nope"), lambda: gf.allowed("nope", "gemini-3.6-flash")):
            with pytest.raises(HTTPException) as e: await U(fn)
            assert e.value.status_code == 400
        assert gf.norm("search") == "ai_search"                                             # cost-feature alias
        with pytest.raises(HTTPException) as e: await U(lambda: gf.access_put("chat", gf.AccessIn(models=["zzz/m"])))
        assert e.value.status_code == 400
        for role, demo in (("editor", False), ("admin", True)):
            with pytest.raises(HTTPException) as e: await U(lambda: gf.access_put("chat", gf.AccessIn(providers=["gemini"])), role=role, demo=demo)
            assert e.value.status_code == 403
        with pytest.raises(HTTPException) as e: await as_user(admin, a, gf.access_list, role="viewer")
        assert e.value.status_code == 403
    with_ws(go)

def test_catalog_filtering_provider_off_and_allowed(fp):
    async def go(A, admin, ext, gp, gf, R, W):
        a = W["a"]; U = lambda fn, **k: as_user(admin, a, fn, **k)
        ids = lambda c: [m["id"] for m in c["models"]]
        base = ids(await U(lambda: gf.catalog_get("playground")))
        assert "gemini-3.6-flash" in base and "gemini-3.1-flash-lite" in base and "aos/fast" in base and "gpt-5-nano" not in base   # no openai key/row: not offered
        prov = await U(lambda: gp.create_provider(gp.ProviderIn(kind="openai_compat", name="mine", base_url=fp.url("openai"), api_key=KEY)))
        await U(lambda: ext.config_create(P(ext, name="mc", model="fake-model-a", provider="mine")))
        full = await U(lambda: gf.catalog_get("playground")); assert "custom/mc" in ids(full)
        # restrict playground to the gemini provider and one model
        await U(lambda: gf.access_put("playground", gf.AccessIn(providers=["gemini"], models=["gemini-3.6-flash"], default_ref="gemini-3.6-flash")))
        c = await U(lambda: gf.catalog_get("playground")); assert ids(c) == ["gemini-3.6-flash"] and c["default"] == "gemini-3.6-flash"
        assert "custom/mc" in ids(await U(lambda: gf.catalog_get("evaluator")))               # other feature untouched
        assert await U(lambda: gf.allowed("playground", "gemini-3.6-flash")) and not await U(lambda: gf.allowed("playground", "gemini-3.1-flash-lite"))
        assert await U(lambda: gf.allowed("evaluator", "gemini-3.1-flash-lite"))
        assert not await U(lambda: gf.allowed("playground", "bad/model"))
        assert (await as_user(admin, W["b"], lambda: gf.allowed("playground", "gemini-3.1-flash-lite")))   # workspace B has no row
        # empty body clears the row
        await U(lambda: gf.access_put("playground", gf.AccessIn())); assert set(base) <= set(ids(await U(lambda: gf.catalog_get("playground"))))
        # provider switched off -> its models and configurations are gone everywhere; not allowed for any feature
        await U(lambda: gp.update_provider(prov["id"], gp.ProviderUpd(enabled=False)))
        for ft in ("playground", "evaluator", None):
            assert "custom/mc" not in ids(await U(lambda: gf.catalog_get(ft)))
        assert not await U(lambda: gf.allowed("evaluator", "mine/fake-model-a"))
        # no_key badge: admin only
        await U(lambda: gp.update_provider(prov["id"], gp.ProviderUpd(enabled=True)))
        nk = await U(lambda: gp.create_provider(gp.ProviderIn(kind="openai_compat", name="nokey", base_url=fp.url("openai"))))
        await U(lambda: ext.config_create(P(ext, name="nk", model="m", provider="nokey")))
        cat_admin = await U(lambda: gf.catalog_get(None)); cat_ed = await U(lambda: gf.catalog_get(None), role="editor")
        assert "custom/nk" in ids(cat_ed) and not any("no_key" in m for m in cat_ed["models"])
        assert not any("no_key" in m for m in cat_admin["models"])                              # openai_compat may be keyless: no badge
        # demo: Gemini only
        gdemo = await U(lambda: gf.catalog_get(None), demo=True); assert all(m["kind"] == "gemini" for m in gdemo["models"])
    with_ws(go)

def test_chat_list_follows_feature_access_and_403(monkeypatch):
    import chat_settings as cs
    async def go(A, admin, ext, gp, gf, R, W):
        a = W["a"]; U = lambda fn: as_user(admin, a, fn)
        async def cfg(aid): return {"model": "gemini-3.6-flash", "fallback_models": ["gemini-3.1-flash-lite"]}
        async def get(k, d): return d
        monkeypatch.setattr(cs, "_agent_cfg", cfg); monkeypatch.setattr(cs, "_get", get)
        assert (await U(lambda: cs.allowed_models(1)))[0] == ["gemini-3.6-flash", "gemini-3.1-flash-lite"]
        await U(lambda: gf.access_put("chat", gf.AccessIn(models=["gemini-3.1-flash-lite"])))
        out, dflt = await U(lambda: cs.allowed_models(1)); assert out == ["gemini-3.1-flash-lite"] and dflt == "gemini-3.1-flash-lite"
        with pytest.raises(HTTPException) as e: await U(lambda: cs.check_run_model(1, "gemini-3.6-flash"))
        assert e.value.status_code == 403
        await U(lambda: cs.check_run_model(1, "gemini-3.1-flash-lite"))
    with_ws(go)
