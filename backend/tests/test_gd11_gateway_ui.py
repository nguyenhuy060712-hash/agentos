"""GĐ11.22 LLM Gateway page (reduced): the page source carries the six tab ids and no "allow private" switch, every new string has an English entry, and `GET /api/gw/usage`
(top 6 + "Khác", workspace isolation, admin vs own rows, demo 403). In-process, $0."""
import asyncio, json, os, re, sys, uuid
import psycopg, pytest
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "app")); sys.path.insert(0, HERE)
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")
import deepagent_api   # noqa: F401,E402  (first app import: modules import each other circularly)

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos"); os.environ.setdefault("PG_URI", PG)
run = asyncio.run
VI_RX = re.compile(r"[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]", re.I)

def _html():
    import ui_ls3
    return ui_ls3.GATEWAY

def test_page_has_tab_ids_and_no_allow_private():
    h = _html()
    for t in ("start", "providers", "models", "fallbacks", "limits", "keys"): assert f"['{t}'," in h, t
    for api in ("api/gw/providers", "api/gw/fallbacks", "api/gw/limits", "api/gw/model-access", "api/gw/usage", "api/gw/prices", "api/model_configs", "api/model_tiers", "api/model_feature_access"): assert api in h, api
    assert "allow_private" not in h and "allow-private" not in h and "allowPrivate" not in h
    assert 'data-ict' in h and 'tz_note' in h   # the ICT sentence is rendered next to the caps

def test_page_english_and_vietnamese_render():
    import i18n, ui_ls3
    h = ui_ls3.GATEWAY
    en = i18n.localize(h, "en", cache=False)
    assert "Add provider" in en or "Thêm nhà cung cấp" not in en   # server-side pass: source strings with diacritics are replaced
    assert i18n.localize(h, "vi", cache=False).count("Thêm nhà cung cấp") >= 1

def test_every_new_string_has_english_entry():
    """Each Vietnamese text/attribute chunk of the Gateway page (outside code) is a key of the dictionary, so EN mode shows no Vietnamese."""
    import i18n
    s = open(os.path.join(HERE, "..", "app", "ui_ls3.py"), encoding="utf-8").read()
    blk = s[s.index('GATEWAY = P("gateway"'):s.index('SANDBOX = P("sandboxes"')]
    chunks = set()
    for m in re.finditer(r">([^<>`${}]+)<|\b(?:placeholder)=\"([^\"]+)\"|(?:flash|confirm)\('([^']+)'|<(?:button|label|b|div|span|th|td|option)[^>]*>([^<>`${}]+)", blk):
        for g in m.groups():
            if g and VI_RX.search(g): chunks.add(g.strip())
    miss = sorted(c for c in chunks if c not in i18n.DICT and c.rstrip(":") not in i18n.DICT and not any(k.startswith(c + " {") for k in i18n.DICT) and not re.search(r"[(){};=]|\$|\bconst\b", c))
    assert not miss, miss

# ---------------------------------------------------------------- GET /api/gw/usage
def with_ws(fn):
    import deepagent_api as A, admin, migrations
    async def go():
        old = (A.S.get("pool"), A.S.get("rls"))
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as c: await migrations.run_pending(c)
        async with AsyncConnectionPool(PG, min_size=1, max_size=4, open=False, kwargs={"autocommit": True, "row_factory": dict_row}) as pool:
            await pool.open(); A.S["pool"] = pool; A.S["rls"] = admin.rls_ctx
            await admin.rls_grants()
            tag = uuid.uuid4().hex[:8]; W = {"tag": tag}
            for s in "ab": W[s] = (await A.q("INSERT INTO workspaces(name) VALUES(%s) RETURNING id", (f"ui22-{s}-{tag}",), one=True))["id"]
            try: return await fn(A, admin, W)
            finally:
                for s in "ab":
                    for sql in ("DELETE FROM gateway_usage WHERE workspace_id=%s", "DELETE FROM workspaces WHERE id=%s"):
                        try: await A.q(sql, (W[s],))
                        except Exception as e: print("cleanup:", str(e)[:80])   # noqa: BLE001
                A.S["pool"], A.S["rls"] = old
    return run(go())

async def as_user(admin, ws, fn, role="admin", demo=False, uid=987661):
    t = admin.USER.set({"id": uid, "username": "t1122", "role": role, "ws": ws, "auth": "test", **({"demo": True} if demo else {})})
    try: return await fn()
    finally: admin.USER.reset(t)

async def put(A, ws, model, cost, provider=None, feature="agent", user_id=None, status="ok", n=1):
    for _ in range(n):
        await A.q("INSERT INTO gateway_usage(model,input_tokens,output_tokens,cost_usd,status,latency_ms,workspace_id,provider,feature,user_id) VALUES(%s,100,10,%s,%s,5,%s,%s,%s,%s)",
                  (model, cost, status, ws, provider, feature, user_id))

def test_usage_breakdowns_top6_and_isolation():
    import gw_api
    async def go(A, admin, W):
        a, b = W["a"], W["b"]
        for i in range(8): await put(A, a, f"gemini-x{i}", 0.5 - i * 0.05, provider="gemini", feature="engine" if i % 2 else "playground")
        await put(A, a, "gpt-5.4-nano", 0.2, provider="openai", n=2); await put(A, a, "claude-haiku-5-5", 0.1, provider=None, status="error")   # provider NULL = old row, classified by model name
        await put(A, b, "gemini-other", 99.0, provider="gemini")
        call = lambda **k: as_user(admin, a, lambda: gw_api.usage(**k))
        r = await call(days=30, breakdown="model")
        assert len(r["rows"]) == 7 and r["rows"][-1]["key"] == "Khác" and r["rows"][-1]["other"] and r["scope"] == "workspace"
        assert r["rows"][0]["key"] == "gemini-x0" and r["total"]["calls"] == 11 and r["total"]["errors"] == 1
        assert abs(sum(x["cost"] for x in r["rows"]) - r["total"]["cost"]) < 1e-5 and abs(sum(x["share"] for x in r["rows"]) - 1) < 0.01
        assert 99.0 not in [x["cost"] for x in r["rows"]] and r["total"]["cost"] < 10   # other workspace never leaks
        p = {x["key"]: x for x in (await call(days=7, breakdown="provider"))["rows"]}
        assert set(p) == {"gemini", "openai", "anthropic"} and p["openai"]["calls"] == 2 and p["anthropic"]["errors"] == 1
        f = {x["key"]: x["calls"] for x in (await call(days=7, breakdown="feature"))["rows"]}; assert f["engine"] == 4 and f["playground"] == 4 and f["agent"] == 3
        u = (await call(days=7, breakdown="user"))["rows"]; assert u[0]["key"] == "(API keys / hệ thống)"
        for bad in ({"days": 0}, {"days": 366}, {"breakdown": "nope"}):
            with pytest.raises(HTTPException) as e: await call(**{"days": 7, "breakdown": "model", **bad}); assert e.value.status_code == 422
        e = await call(days=1, breakdown="model"); assert e["total"]["calls"] == 11
    with_ws(go)

def test_usage_non_admin_sees_only_own_and_demo_blocked():
    import gw_api
    async def go(A, admin, W):
        a = W["a"]
        u = await A.q("INSERT INTO users(username,pw_hash) VALUES(%s,'x') RETURNING id", (f"ui22v-{W['tag']}",), one=True)
        try:
            await put(A, a, "gemini-3.6-flash", 0.3, provider="gemini", user_id=u["id"], n=2); await put(A, a, "gemini-3.6-flash", 5.0, provider="gemini", user_id=None)
            mine = await as_user(admin, a, lambda: gw_api.usage(days=30, breakdown="model"), role="viewer", uid=u["id"])
            assert mine["scope"] == "self" and mine["total"]["calls"] == 2 and abs(mine["total"]["cost"] - 0.6) < 1e-6
            allr = await as_user(admin, a, lambda: gw_api.usage(days=30, breakdown="model")); assert allr["total"]["calls"] == 3
            with pytest.raises(HTTPException) as e: await as_user(admin, a, lambda: gw_api.usage(days=30, breakdown="model"), role="viewer", demo=True, uid=u["id"])
            assert e.value.status_code == 403
        finally: await A.q("DELETE FROM users WHERE id=%s", (u["id"],))
    with_ws(go)
