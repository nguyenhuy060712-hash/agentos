"""GĐ12 card 12.85: GET /api/integrations = view-only state of every Google / Microsoft tool family for the CALLER (off | not_configured | reconnect | missing_scope | connected).
In-process with seeded connections (tokens made locally, no fake server needed), no network, no LLM, $0."""
import asyncio, json, os, sys, uuid
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

os.environ.setdefault("GOOGLE_API_KEY", "x-not-used")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import deepagent_api as A  # noqa: E402,I001  (must come first: circular imports)
import admin, migrations, oauth_tools as O, connections as C, integrations as I  # noqa: E402
import office_scopes as OS, workspace_tools as W  # noqa: E402

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
os.environ.setdefault("PG_URI", PG)
TAG = uuid.uuid4().hex[:6]
WA, WB = 93851, 93852
SEEDS: dict = {}
ACCESS, REFRESH = "ACCESS-SECRET-" + TAG, "REFRESH-SECRET-" + TAG

def U(ws, uid, role="editor", **kw): return {"id": uid, "username": f"u{uid}", "role": role, "ws": ws, **kw}

def run(fn):
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as c:
            await migrations.run_pending(c)
        async with AsyncConnectionPool(PG, min_size=1, max_size=4, open=False, kwargs={"autocommit": True, "row_factory": dict_row}) as pool:
            await pool.open(); A.S["pool"] = pool; A.S["rls"] = admin.rls_ctx
            for g in ("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO aos_rls", "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO aos_rls"):
                for i in range(5):
                    try: await A.q(g); break
                    except psycopg.errors.InternalError_:
                        if i == 4: raise
                        await asyncio.sleep(0.5)
            await W.init(); await seed()
            try: return await fn()
            finally: await cleanup()
    return asyncio.run(go())

async def seed():
    for w, m in ((WA, {"google": "write", "ms365": "read"}), (WB, {"google": "read", "ms365": "read"})):
        await A.q("INSERT INTO workspaces(id,name,gw_tools) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (w, f"t1285-{TAG}-{w}", Jsonb(m)))
    for k in ("alice", "bob"):
        SEEDS[k] = (await A.q("INSERT INTO users(username,pw_hash) VALUES(%s,'x') RETURNING id", (f"t1285-{TAG}-{k}",), one=True))["id"]
    for prov in ("google", "microsoft"): SEEDS[f"p_{prov}"] = await mk_provider(prov, WA)

async def mk_provider(name, ws):
    p = await A.q("""INSERT INTO oauth_providers(name,display_name,authorize_url,token_url,revoke_url,api_base,client_id,workspace_id,scopes,token_auth,pkce,enabled,redirect_mode)
                     VALUES(%s,%s,'http://127.0.0.1:1/a','http://127.0.0.1:1/t',NULL,'http://127.0.0.1:1/','cid',%s,%s,'body',true,true,'loopback_paste') RETURNING *""",
                  (name, name, ws, OS.scope_string(name, "write")), one=True)
    await A.q("UPDATE oauth_providers SET client_secret_enc=%s WHERE id=%s", (O.enc("s", "oauth_providers.client_secret_enc", ws, p["id"]), p["id"]))
    return p

async def cleanup():
    ws = [WA, WB]
    for t in ("oauth_connections", "connections", "oauth_providers", "oauth_audit"):
        await A.q(f"DELETE FROM {t} WHERE workspace_id = ANY(%s)", (ws,))
    await A.q("DELETE FROM users WHERE username LIKE %s", (f"t1285-{TAG}-%",))
    await A.q("DELETE FROM crypto_deks WHERE workspace_id = ANY(%s)", (ws,)); await A.q("DELETE FROM workspaces WHERE id = ANY(%s)", (ws,))
    SEEDS.clear()

async def connect(prov, uid, scope=None):
    j = {"access_token": ACCESS, "refresh_token": REFRESH, "token_type": "bearer", "expires_in": 3600, "scope": scope if scope is not None else OS.scope_string(prov, "write")}
    await C.save_tokens(("user", uid), SEEDS[f"p_{prov}"], j, account=f"{prov}@fake.test")

async def get(user):
    tok = admin.USER.set(user)
    try: return await I.integrations()
    finally: admin.USER.reset(tok)

def by(rows, prov, fam): return next(r for r in rows if r["provider"] == prov and r["family"] == fam)

def test_states_and_no_token_in_answer():
    async def go():
        alice = U(WA, SEEDS["alice"])
        rows = await get(alice)
        assert {r["state"] for r in rows if r["provider"] == "google"} == {"not_configured"}      # mode write, nobody connected yet
        await connect("google", SEEDS["alice"]); await connect("microsoft", SEEDS["alice"], OS.scope_string("microsoft", "read"))
        rows = await get(alice)
        assert [r["family"] for r in rows if r["provider"] == "google"] == ["gmail", "calendar", "sheets", "docs", "drive"]
        assert {r["state"] for r in rows if r["provider"] == "google"} == {"connected"}
        assert by(rows, "google", "gmail")["account"] == "google@fake.test"
        assert {r["state"] for r in rows if r["provider"] == "microsoft"} == {"connected"}     # read mode needs only read scopes
        tools = by(rows, "google", "docs")["tools"]
        assert tools and all(set(t) == {"key", "label", "tier"} for t in tools) and {t["tier"] for r in rows for t in r["tools"]} <= set(W.TIERS)
        # missing scope: connected for read only while the workspace is in write mode
        await connect("google", SEEDS["alice"], "openid email " + OS.G + "gmail.readonly")
        rows = await get(alice)
        assert by(rows, "google", "gmail")["state"] == "missing_scope" and by(rows, "google", "docs")["state"] == "missing_scope"
        # the answer never carries a token
        raw = json.dumps(rows)
        assert ACCESS not in raw and REFRESH not in raw
        for w in ("token", "secret", "_enc", "refresh"): assert w not in raw.lower(), w
        # reconnect: the token no longer works
        await A.q("UPDATE oauth_connections SET status='needs_reconnect' WHERE workspace_id=%s AND owner_id=%s", (WA, SEEDS["alice"]))
        assert {r["state"] for r in await get(alice) if r["provider"] == "google"} == {"reconnect"}
        # off: the admin switched the families off
        await A.q("UPDATE workspaces SET gw_tools=%s WHERE id=%s", (Jsonb({"google": "off", "ms365": "off"}), WA))
        assert {r["state"] for r in await get(alice)} == {"off"}
    run(go)

def test_each_user_sees_only_their_own_connection():
    async def go():
        await connect("google", SEEDS["alice"])
        rows = await get(U(WA, SEEDS["bob"]))
        assert {r["state"] for r in rows if r["provider"] == "google"} == {"not_configured"}
        assert all("account" not in r for r in rows)
    run(go)

def test_viewer_sees_status_without_controls_and_demo_gets_nothing():
    async def go():
        await connect("google", SEEDS["alice"])
        v = await get(U(WA, SEEDS["alice"], role="viewer"))
        assert by(v, "google", "gmail")["state"] == "connected" and not any(r["can_manage"] for r in v)
        assert all(r["can_manage"] for r in await get(U(WA, SEEDS["alice"], role="admin")))
        assert await get(U(WA, SEEDS["alice"], demo=True)) == []
        assert await get(U(WA, SEEDS["alice"], auth="channel")) == []
        assert await get({}) == []
    run(go)

def test_microsoft_families_and_links():
    async def go():
        rows = await get(U(WB, SEEDS["bob"]))
        assert [r["family"] for r in rows if r["provider"] == "microsoft"] == ["mail", "calendar", "files"]
        ms = by(rows, "microsoft", "files")
        assert any(t["key"].startswith("ms365:onedrive_") for t in ms["tools"]) and any(t["key"].startswith("ms365:excel_") for t in ms["tools"])
        assert ms["connect_url"] == "/settings?s=connections"
    run(go)

def test_new_strings_have_vi_and_en():
    d = json.load(open(os.path.join(os.path.dirname(__file__), "..", "app", "i18n_dict.json"), encoding="utf-8"))
    for k in ("Reconnect needed", "Missing permission", "Not configured", "Manage in Connections", "Google & Microsoft 365"):
        assert d[k]["en"] and d[k]["vi"], k
