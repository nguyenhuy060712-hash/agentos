"""GĐ11 card 11.24 (connections model, single owner, F1). In-process: migration 0021 is applied to the local DB, handlers are called with a faked current user
(admin.USER) on a private pool under the RLS role; the OAuth token endpoint is replaced by a counter. Migration up/down runs in a throwaway schema.
Negative tests: other workspace, demo, channel, non-admin, revoked, swapped ciphertext, forged agent id, no token in any response / log / audit row."""
import asyncio, importlib.util, json, logging, os, sys, uuid
import psycopg, pytest
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError
from starlette.requests import Request

os.environ.setdefault("GOOGLE_API_KEY", "x-not-used")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import deepagent_api as A  # noqa: E402,I001  (must come first: circular imports)
import admin, migrations, oauth_tools as O, connections as C, agent_identity  # noqa: E402

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
os.environ.setdefault("PG_URI", PG)   # crypto.seal reads its DEKs through PG_URI
TAG = uuid.uuid4().hex[:6]
WA, WB = 92001, 92002
SEEDS: dict = {}

def U(ws, uid, role="editor", **kw): return {"id": uid, "username": f"u{uid}", "role": role, "ws": ws, **kw}

def run(fn):
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as c:
            await migrations.run_pending(c)
        async with AsyncConnectionPool(PG, min_size=1, max_size=3, open=False, kwargs={"autocommit": True, "row_factory": dict_row}) as pool:
            await pool.open(); A.S["pool"] = pool; A.S["rls"] = admin.rls_ctx
            for g in ("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO aos_rls", "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO aos_rls"):
                for i in range(5):   # parallel test runs of other cards GRANT/ALTER at the same time ("tuple concurrently updated")
                    try: await A.q(g); break
                    except psycopg.errors.InternalError_:
                        if i == 4: raise
                        await asyncio.sleep(0.5)
            for t in ("connections", "oauth_connections"):   # what admin.rls_init does at boot
                await A.q(f"ALTER TABLE {t} ALTER COLUMN workspace_id SET DEFAULT coalesce(aos_ws(), 1)")
                await admin._policy(t, "aos_ws() IS NULL OR workspace_id = aos_ws()")
            await seed()
            try: return await fn()
            finally: await cleanup()
    return asyncio.run(go())

async def seed():
    for w in (WA, WB): await A.q("INSERT INTO workspaces(id,name) VALUES(%s,%s) ON CONFLICT DO NOTHING", (w, f"t1124-{TAG}-{w}"))
    for k in ("owner_a", "alice", "bob", "owner_b"):
        SEEDS[k] = (await A.q("INSERT INTO users(username,pw_hash) VALUES(%s,'x') RETURNING id", (f"t1124-{TAG}-{k}",), one=True))["id"]
    for k, w, o in (("ag_a1", WA, "owner_a"), ("ag_a2", WA, "owner_a"), ("ag_b", WB, "owner_b")):
        SEEDS[k] = (await A.q("INSERT INTO agents(name,workspace_id,owner_id) VALUES(%s,%s,%s) RETURNING id", (f"t1124-{TAG}-{k}", w, SEEDS[o]), one=True))["id"]
    for w in (WA, WB):   # one provider named crm per workspace (same name on purpose)
        p = await A.q("""INSERT INTO oauth_providers(name,display_name,authorize_url,token_url,api_base,client_id,workspace_id) VALUES('crm','CRM','http://127.0.0.1:9/a','http://127.0.0.1:9/t',
                         'http://127.0.0.1:9/api/','cid',%s) RETURNING *""", (w,), one=True)
        SEEDS[f"p_{w}"] = p

async def cleanup():
    ids = [SEEDS[k] for k in ("ag_a1", "ag_a2", "ag_b") if k in SEEDS]
    await A.q("DELETE FROM mcp_servers WHERE workspace_id IN (%s,%s)", (WA, WB)); await A.q("DELETE FROM tool_settings WHERE workspace_id IN (%s,%s)", (WA, WB))   # 11.27
    await A.q("DELETE FROM auth_sessions WHERE workspace_id IN (%s,%s)", (WA, WB)); await A.q("DELETE FROM agent_audit WHERE workspace_id IN (%s,%s)", (WA, WB))
    await A.q("DELETE FROM oauth_connections WHERE workspace_id IN (%s,%s)", (WA, WB)); await A.q("DELETE FROM connections WHERE workspace_id IN (%s,%s)", (WA, WB))
    await A.q("DELETE FROM oauth_providers WHERE workspace_id IN (%s,%s)", (WA, WB)); await A.q("DELETE FROM oauth_audit WHERE workspace_id IN (%s,%s)", (WA, WB))
    await A.q("DELETE FROM oauth_states WHERE provider_id IS NULL OR user_id = ANY(%s)", ([SEEDS[k] for k in ("alice", "bob", "owner_a", "owner_b") if k in SEEDS],))
    if ids: await A.q("DELETE FROM agent_identity WHERE agent_id = ANY(%s)", (ids,)); await A.q("DELETE FROM agents WHERE id = ANY(%s)", (ids,))
    await A.q("DELETE FROM users WHERE username LIKE %s", (f"t1124-{TAG}-%",))
    await A.q("DELETE FROM crypto_deks WHERE workspace_id IN (%s,%s)", (WA, WB)); await A.q("DELETE FROM workspaces WHERE id IN (%s,%s)", (WA, WB))
    agent_identity._MODE.clear(); agent_identity._OWN.clear(); SEEDS.clear()

async def as_(user, fn, *a, **kw):
    tok = admin.USER.set(user)
    try: return await fn(*a, **kw)
    finally: admin.USER.reset(tok)

def req(**headers): return Request({"type": "http", "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()], "query_string": b""})

async def put_oauth(owner, ws, access, refresh="rt", scope="read write", expires_in=3600, p=None):
    await C.save_tokens(owner, p or SEEDS[f"p_{ws}"], {"access_token": access, "refresh_token": refresh, "scope": scope, "expires_in": expires_in, "token_type": "bearer"})

async def set_mode(agent, mode):
    await A.q("INSERT INTO agent_identity(agent_id,workspace_id,mode) VALUES(%s,(SELECT workspace_id FROM agents WHERE id=%s),%s) ON CONFLICT(agent_id) DO UPDATE SET mode=EXCLUDED.mode", (agent, agent, mode))
    agent_identity._MODE.clear(); agent_identity._OWN.clear()

@pytest.fixture
def refreshes(monkeypatch):
    calls = []
    async def fake_call(p, data):
        calls.append(dict(data))
        if p.get("name") == "crm" and data.get("refresh_token") == "dead": raise HTTPException(400, "Token endpoint error: invalid_grant")
        return {"access_token": f"NEW-{len(calls)}", "refresh_token": f"rt{len(calls)}", "expires_in": 3600, "scope": "read write", "token_type": "bearer"}
    monkeypatch.setattr(O, "_token_call", fake_call)
    return calls

# ------------------------------------------------------------------ migration
def test_migration_up_down_in_throwaway_schema():
    mod = next(m for v, m, _ in migrations._load() if v == "0021"); sch = "t1124_" + uuid.uuid4().hex[:8]
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as root:
            await root.execute(f'CREATE SCHEMA "{sch}"')
        try:
            async with await psycopg.AsyncConnection.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
                q = migrations._q(c)
                for s in mod._BASE: await q(s)    # the OLD shape, with legacy data
                await q("INSERT INTO oauth_providers(id,name,display_name,authorize_url,token_url,api_base,client_id,workspace_id) VALUES(1,'crm','CRM','a','t','b','c',7)")
                await q("INSERT INTO oauth_connections(provider_id,user_id,workspace_id,access_token_enc,refresh_token_enc,status) VALUES(1,41,7,'v2:legacy-a','v2:legacy-r','active'),(1,42,7,'v2:legacy-b',NULL,'weird')")
                assert await mod.plan(q)   # dry-run text, changes nothing
                await mod.up(q); await mod.up(q)   # applying twice is harmless
                rows = {r[0]: r for r in await q("SELECT user_id, owner_type, owner_id, aad_owner, kind, connection_id, status, access_token_enc FROM oauth_connections ORDER BY user_id")}
                assert rows[41][1:5] == ("user", 41, "41", "oauth") and rows[41][5] is not None and rows[41][7] == "v2:legacy-a"   # token bytes untouched, AAD owner = user id
                assert rows[42][6] == "error"   # unknown legacy status mapped into the 4-value set
                assert (await q("SELECT slug, kind, workspace_id FROM connections"))[0] == ("crm", "oauth2", 7)
                with pytest.raises(psycopg.errors.UniqueViolation):   # old uniqueness still holds under the new key
                    await c.execute("INSERT INTO oauth_connections(provider_id,user_id,owner_type,owner_id,workspace_id,access_token_enc) VALUES(1,41,'user',41,7,'x')")
                await c.execute("INSERT INTO oauth_connections(provider_id,user_id,owner_type,owner_id,workspace_id,access_token_enc) VALUES(1,NULL,'agent',5,7,'x')")
                assert (await q("SELECT aad_owner FROM oauth_connections WHERE owner_type='agent'"))[0][0] == "agent-5"
                await c.execute("INSERT INTO oauth_connections(provider_id,user_id,owner_type,owner_id,workspace_id,access_token_enc) VALUES(1,NULL,'workspace',0,7,'x')")
                for bad in ("INSERT INTO oauth_connections(provider_id,user_id,owner_type,owner_id,workspace_id) VALUES(1,50,'user',50,7)",           # oauth row without a token
                            "INSERT INTO oauth_connections(provider_id,owner_type,owner_id,workspace_id,access_token_enc) VALUES(1,'user',51,7,'x')",   # user row without user_id
                            "INSERT INTO oauth_connections(provider_id,user_id,owner_type,owner_id,workspace_id,access_token_enc) VALUES(1,5,'robot',5,7,'x')"):
                    with pytest.raises(psycopg.errors.CheckViolation): await c.execute(bad)
                await c.execute("INSERT INTO connections(workspace_id,slug,kind) VALUES(7,'CRM_KEY','secret')")
                cid = (await q("SELECT id FROM connections WHERE slug='CRM_KEY'"))[0][0]
                for o in (1, 2): await c.execute("INSERT INTO oauth_connections(kind,owner_type,owner_id,connection_id,workspace_id,value_enc) VALUES('secret','agent',%s,%s,7,'s')", (o, cid))
                with pytest.raises(psycopg.errors.UniqueViolation): await c.execute("INSERT INTO oauth_connections(kind,owner_type,owner_id,connection_id,workspace_id,value_enc) VALUES('secret','agent',1,%s,7,'s2')", (cid,))
                await mod.down(q)   # agent / workspace / secret rows cannot exist in the old shape -> gone; user rows keep their tokens
                left = await q("SELECT user_id, access_token_enc FROM oauth_connections ORDER BY user_id")
                assert left == [(41, "v2:legacy-a"), (42, "v2:legacy-b")]
                assert not await q("SELECT 1 WHERE to_regclass('connections') IS NOT NULL") and not await q("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name='oauth_connections' AND column_name IN ('owner_type','aad_owner','value_enc')", (sch,))
                with pytest.raises(psycopg.errors.UniqueViolation): await c.execute("INSERT INTO oauth_connections(provider_id,user_id,workspace_id,access_token_enc) VALUES(1,41,7,'x')")
                with pytest.raises(psycopg.errors.NotNullViolation): await c.execute("INSERT INTO oauth_connections(provider_id,user_id,workspace_id) VALUES(1,60,7)")
                await mod.up(q)   # and up again after down
                assert (await q("SELECT count(*) FROM oauth_connections WHERE connection_id IS NOT NULL"))[0][0] == 2
        finally:
            async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as root: await root.execute(f'DROP SCHEMA "{sch}" CASCADE')
    asyncio.run(go())

def test_migration_on_fresh_database_creates_base_tables():
    mod = next(m for v, m, _ in migrations._load() if v == "0021"); sch = "t1124f_" + uuid.uuid4().hex[:8]
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as root: await root.execute(f'CREATE SCHEMA "{sch}"')
        try:
            async with await psycopg.AsyncConnection.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
                q = migrations._q(c); assert await mod.plan(q)
                await mod.up(q)   # nothing existed: oauth_tools.init has not run yet at that point of a first boot
                assert (await q("SELECT count(*) FROM oauth_connections"))[0][0] == 0 and (await q("SELECT count(*) FROM connections"))[0][0] == 0
        finally:
            async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as root: await root.execute(f'DROP SCHEMA "{sch}" CASCADE')
    asyncio.run(go())

# ------------------------------------------------------------------ owner resolution
def test_owner_resolution_agent_user_demo_channel():
    async def t():
        a1, a2, alice, oa = SEEDS["ag_a1"], SEEDS["ag_a2"], SEEDS["alice"], SEEDS["owner_a"]
        user = U(WA, alice)
        await put_oauth(("user", alice), WA, "USER-TOK")
        assert (await C.resolve(user, a1, "crm"))[0] == ("user", alice)          # no agent connection: the caller's own
        assert (await C.resolve(user, None, "crm"))[0] == ("user", alice)
        await put_oauth(("agent", a1), WA, "AGENT-TOK")
        assert (await C.resolve(user, a1, "crm"))[0] == ("agent", a1)            # the agent's own account wins ...
        assert (await C.resolve(user, a2, "crm"))[0] == ("user", alice)          # ... only for that agent
        assert (await C.get_token(("agent", a1), "crm", ws=WA)).value == "AGENT-TOK" and (await C.get_token(("user", alice), "crm", ws=WA)).value == "USER-TOK"
        # --- refused
        with pytest.raises(HTTPException) as x: await C.resolve({**user, "demo": True, "role": "viewer"}, a1, "crm")
        assert x.value.status_code == 403
        with pytest.raises(HTTPException) as x: await C.resolve({**user, "auth": "channel"}, a1, "crm")          # channel in identity mode 'user': never, not even the agent's own account
        assert x.value.status_code == 403
        with pytest.raises(HTTPException) as x: await C.resolve({**user, "auth": "channel"}, None, "crm")
        assert x.value.status_code == 403
        with pytest.raises(HTTPException) as x: await C.resolve(U(WB, alice), a1, "crm")                          # agent id of ANOTHER workspace (forged)
        assert x.value.status_code == 403
        with pytest.raises(HTTPException) as x: await C.resolve(None, a1, "crm")
        assert x.value.status_code == 401
        # --- identity mode 'agent' (admin's opt-in for unattended use): a channel run may use the agent's account; without one it gets the owner's, as before 11.24
        await set_mode(a1, "agent")
        assert (await C.resolve({**user, "auth": "channel"}, a1, "crm"))[0] == ("agent", a1)
        await set_mode(a2, "agent"); await put_oauth(("user", oa), WA, "OWNER-TOK")
        owner, cu = await C.resolve({**user, "auth": "channel"}, a2, "crm"); assert owner == ("user", oa) and cu["auth"] == "agent_identity"
        with pytest.raises(HTTPException) as x: await C.resolve({**user, "demo": True, "role": "viewer", "auth": "channel"}, a1, "crm")   # demo is refused in every mode
        assert x.value.status_code == 403
        # --- a revoked agent connection is skipped
        await A.q("UPDATE oauth_connections SET status='revoked' WHERE owner_type='agent' AND owner_id=%s", (a1,))
        assert (await C.resolve(user, a1, "crm"))[0] == ("user", oa)             # mode agent -> owner's accounts
    run(t)

def test_scheduled_run_uses_agents_account_user_sees_only_theirs(refreshes):
    async def t():
        a1, alice, bob = SEEDS["ag_a1"], SEEDS["alice"], SEEDS["bob"]
        await put_oauth(("agent", a1), WA, "AGENT-TOK"); await put_oauth(("user", alice), WA, "ALICE-TOK")
        sched = U(WA, SEEDS["owner_a"], "editor", auth="run")                    # a scheduled run acts as the cron creator
        assert (await as_(sched, O.get_token, "crm", sched, a1))[1] == "AGENT-TOK"   # old entry point, new behaviour
        tr = await as_(sched, O.tool_request, "crm", "GET", "http://127.0.0.1:9/nowhere", None, a1)
        assert tr.startswith("Error: ") and "AGENT-TOK" not in tr                # network failure text only, never the token
        for who, mine in ((alice, "ALICE-TOK"), (bob, None)):
            rows = await as_(U(WA, who), C.conn_list, req(), "me"); crm = next(r for r in rows if r["slug"] == "crm")
            assert crm["connected"] == (mine is not None) and crm["status"] == ("active" if mine else "not_connected")
            assert not any(k.endswith("_enc") for r in rows for k in r), "no ciphertext columns in the API"
        with pytest.raises(HTTPException) as x: await C.get_token(("user", bob), "crm", ws=WA)       # bob never inherits alice's or the agent's token
        assert x.value.status_code == 401 and isinstance(x.value, C.CredentialRequired)
    run(t)

# ------------------------------------------------------------------ secrets (the per-agent .env store) + workspace isolation + sealing
def test_two_agents_same_secret_name_and_cross_workspace_refused():
    async def t():
        a1, a2, ab, alice = SEEDS["ag_a1"], SEEDS["ag_a2"], SEEDS["ag_b"], SEEDS["alice"]
        adm, admb = U(WA, SEEDS["owner_a"], "admin"), U(WB, SEEDS["owner_b"], "admin")
        for who in (adm, admb): await as_(who, C.conn_create, C.ConnIn(slug="CRM_KEY", kind="secret", display_name="CRM key"))   # same slug in two workspaces is fine
        await as_(adm, C.conn_credentials, "CRM_KEY", C.CredIn(value="key-of-A1", owner=f"agent:{a1}"))
        await as_(adm, C.conn_credentials, "CRM_KEY", C.CredIn(value="key-of-A2", owner=f"agent:{a2}"))
        await as_(U(WA, alice), C.conn_credentials, "CRM_KEY", C.CredIn(value="alices-own"))
        await as_(admb, C.conn_credentials, "CRM_KEY", C.CredIn(value="key-of-B", owner=f"agent:{ab}"))
        g = lambda o, ws=WA, s="CRM_KEY": C.get_token(o, s, ws=ws)  # noqa: E731
        assert [(await g(("agent", a1))).value, (await g(("agent", a2))).value, (await g(("user", alice))).value] == ["key-of-A1", "key-of-A2", "alices-own"]
        assert (await g(("agent", ab), WB)).value == "key-of-B"
        # --- workspace B cannot read A's, with A's agent id nor with a user id
        with pytest.raises(HTTPException) as x: await g(("agent", a1), WB)
        assert x.value.status_code == 403
        with pytest.raises(HTTPException) as x: await g(("user", alice), WB)
        assert x.value.status_code == 401
        with pytest.raises(HTTPException) as x: await g(("agent", ab), WA)       # and the other way round
        assert x.value.status_code == 403
        # --- stored sealed, bound to its row and owner: a copied cell is useless
        rows = await A.q("SELECT id, owner_type, owner_id, value_enc FROM oauth_connections WHERE kind='secret' AND workspace_id=%s", (WA,))
        assert rows and all(r["value_enc"].startswith("v2:") and "key-of" not in r["value_enc"] and "alices-own" not in r["value_enc"] for r in rows)
        by = {(r["owner_type"], r["owner_id"]): r for r in rows}
        await A.q("UPDATE oauth_connections SET value_enc=%s WHERE id=%s", (by[("agent", a1)]["value_enc"], by[("agent", a2)]["id"]))
        with pytest.raises(HTTPException) as x: await g(("agent", a2))
        assert x.value.status_code == 500 and "key-of-A1" not in str(x.value.detail)
        # --- replacing a secret keeps one row per owner; deleting removes only that owner's
        await as_(adm, C.conn_credentials, "CRM_KEY", C.CredIn(value="rotated", owner=f"agent:{a1}"))
        assert (await g(("agent", a1))).value == "rotated" and (await A.q("SELECT count(*) n FROM oauth_connections WHERE kind='secret' AND owner_type='agent' AND owner_id=%s", (a1,), one=True))["n"] == 1
        await as_(adm, C.conn_delete, "CRM_KEY", f"agent:{a1}")
        with pytest.raises(HTTPException) as x: await g(("agent", a1))
        assert x.value.status_code == 401 and (await g(("user", alice))).value == "alices-own"
    run(t)

def test_token_repr_and_logs_never_contain_the_secret(caplog, refreshes, capsys):
    async def t():
        alice = SEEDS["alice"]
        caplog.set_level(logging.DEBUG)
        await put_oauth(("user", alice), WA, "SUPER-SECRET-ACCESS", refresh="SUPER-SECRET-REFRESH", expires_in=5)   # expires within 60 s -> refresh path runs
        tok = await C.get_token(("user", alice), "crm", ws=WA, user=U(WA, alice))
        assert tok.value.startswith("NEW-") and tok.value not in repr(tok) and tok.value not in str(tok)
        await put_oauth(("user", alice), WA, "SUPER-SECRET-ACCESS2", refresh="dead", expires_in=5)
        with pytest.raises(HTTPException) as x: await C.get_token(("user", alice), "crm", ws=WA, user=U(WA, alice))
        assert "dead" not in str(x.value.detail)
        row = await A.q("SELECT status, status_reason FROM oauth_connections WHERE owner_type='user' AND owner_id=%s", (alice,), one=True)
        assert (row["status"], row["status_reason"]) == ("needs_reconnect", "refresh_failed")
        audit = json.dumps(await A.q("SELECT * FROM oauth_audit WHERE workspace_id=%s", (WA,)), default=str)
        out = capsys.readouterr(); blob = caplog.text + out.out + out.err + audit
        assert "SUPER-SECRET" not in blob and "NEW-" not in blob
    run(t)

def test_agent_owner_oauth_refresh_scopes_and_aad_binding(refreshes):
    async def t():
        a1, alice = SEEDS["ag_a1"], SEEDS["alice"]
        await put_oauth(("agent", a1), WA, "AG-OLD", expires_in=5); await put_oauth(("user", alice), WA, "AL-FINE")
        t1 = await C.get_token(("agent", a1), "crm", ws=WA)                        # expiring -> refreshed in place, for the AGENT row only
        assert t1.value == "NEW-1" and len(refreshes) == 1 and refreshes[0]["refresh_token"] == "rt"
        assert (await C.get_token(("user", alice), "crm", ws=WA)).value == "AL-FINE" and len(refreshes) == 1
        assert (await C.get_token(("agent", a1), "crm", ws=WA)).value == "NEW-1" and len(refreshes) == 1   # fresh now
        assert (await C.get_token(("agent", a1), "crm", ws=WA, force_refresh=True)).value == "NEW-2"
        # --- scopes: missing -> CredentialRequired(scope_missing); above the connection's allowed_scopes -> 403
        with pytest.raises(C.CredentialRequired) as x: await C.get_token(("agent", a1), "crm", ["read", "admin"], ws=WA)
        assert x.value.reason == "scope_missing" and x.value.missing_scopes == ["admin"]
        assert (await C.get_token(("agent", a1), "crm", ["read"], ws=WA)).scope == "read write"
        await A.q("UPDATE connections SET allowed_scopes='read' WHERE workspace_id=%s AND slug='crm'", (WA,))
        with pytest.raises(HTTPException) as x: await C.get_token(("agent", a1), "crm", ["write"], ws=WA)
        assert x.value.status_code == 403
        # --- a user's cipher text pasted into the agent's row (or the other way round) does not decrypt
        r = {(x["owner_type"]): x for x in await A.q("SELECT id, owner_type, access_token_enc FROM oauth_connections WHERE workspace_id=%s", (WA,))}
        await A.q("UPDATE oauth_connections SET access_token_enc=%s WHERE id=%s", (r["user"]["access_token_enc"], r["agent"]["id"]))
        with pytest.raises(HTTPException) as x: await C.get_token(("agent", a1), "crm", ws=WA)
        assert x.value.status_code == 500
        # --- revoked -> must reconnect; unknown provider -> 404; no workspace -> refused (never silently workspace 1)
        await A.q("UPDATE oauth_connections SET status='revoked' WHERE id=%s", (r["user"]["id"],))
        with pytest.raises(C.CredentialRequired): await C.get_token(("user", alice), "crm", ws=WA)
        with pytest.raises(HTTPException) as x: await C.get_token(("user", alice), "nope", ws=WA)
        assert x.value.status_code == 404
        with pytest.raises(HTTPException) as x: await C.get_token(("user", alice), "crm")
        assert x.value.status_code == 400
        for bad in (("robot", 1), ("agent", 0), ("workspace", 3), ("user", True), "x"):
            with pytest.raises(ValueError): C._owner(bad)
        # --- demo caller context: refused even with an explicit owner
        with pytest.raises(HTTPException) as x: await as_(U(WA, alice, "viewer", demo=True), C.get_token, ("user", alice), "crm", ws=WA)
        assert x.value.status_code == 403
    run(t)

def test_old_user_tokens_sealed_before_the_migration_still_work(refreshes):
    """A row exactly as GĐ9-GĐ10 wrote it (AAD key provider_id:user_id, nothing else filled) is read by the new code."""
    async def t():
        alice, p = SEEDS["alice"], SEEDS[f"p_{WA}"]
        await A.q("""INSERT INTO oauth_connections(provider_id,user_id,owner_type,owner_id,workspace_id,access_token_enc,refresh_token_enc,status,expires_at) VALUES(%s,%s,'user',%s,%s,%s,%s,'active',now()+interval '1 hour')""",
                  (p["id"], alice, alice, WA, O.enc("LEGACY-TOKEN", "oauth_connections.access_token_enc", WA, p["id"], alice), O.enc("LEGACY-R", "oauth_connections.refresh_token_enc", WA, p["id"], alice)))
        assert (await C.get_token(("user", alice), "crm", ws=WA)).value == "LEGACY-TOKEN"
        assert (await C.get_token(("user", alice), "crm", ws=WA, force_refresh=True)).value.startswith("NEW-") and refreshes[0]["refresh_token"] == "LEGACY-R"
        assert (await C.get_token(("user", alice), "crm", ws=WA)).value.startswith("NEW-")    # re-sealed with the same AAD key: still readable
        al = await as_(U(WA, alice), O.conn_list); assert [r["provider"] for r in al] == ["crm"] and "access_token_enc" not in al[0]      # old read-only alias
    run(t)

# ------------------------------------------------------------------ API permissions (negative first)
def test_api_admin_only_owners_and_cross_workspace_ids():
    async def t():
        a1, ab = SEEDS["ag_a1"], SEEDS["ag_b"]
        ed, adm, admb = U(WA, SEEDS["alice"]), U(WA, SEEDS["owner_a"], "admin"), U(WB, SEEDS["owner_b"], "admin")
        await as_(adm, C.conn_create, C.ConnIn(slug="CRM_KEY", kind="secret"))
        for call in (lambda: C.conn_list(req(), f"agent:{a1}"), lambda: C.conn_list(req(), "workspace"), lambda: C.conn_get("CRM_KEY", "workspace"),
                     lambda: C.conn_create(C.ConnIn(slug="X1", kind="secret")), lambda: C.conn_credentials("CRM_KEY", C.CredIn(value="v", owner="workspace")),
                     lambda: C.conn_credentials("CRM_KEY", C.CredIn(value="v", owner=f"agent:{a1}")), lambda: C.agent_connections(a1),
                     lambda: C.conn_delete("CRM_KEY", "definition"), lambda: C.conn_delete("CRM_KEY", "workspace"), lambda: C.conn_label("CRM_KEY", C.LabelIn(label="x", owner=f"agent:{a1}"))):
            with pytest.raises(HTTPException) as x: await as_(ed, call)           # editor: only their own ("me") credentials
            assert x.value.status_code == 403, x.value.detail
        for call in (lambda: C.conn_list(req(), f"agent:{a1}"), lambda: C.agent_connections(a1), lambda: C.conn_credentials("CRM_KEY", C.CredIn(value="v", owner=f"agent:{a1}")),
                     lambda: C.conn_delete("CRM_KEY", f"agent:{a1}")):
            with pytest.raises(HTTPException) as x: await as_(admb, call)         # admin of ANOTHER workspace: A's agent does not exist for them
            assert x.value.status_code == 404
        with pytest.raises(HTTPException) as x: await as_(adm, C.conn_list, req(), f"agent:{ab}")   # and the reverse
        assert x.value.status_code == 404
        for bad in ("agent:abc", "agent:", "user:1", "me ; drop", "agent:1/../2"):
            with pytest.raises(HTTPException) as x: await as_(adm, C.conn_list, req(), bad)
            assert x.value.status_code == 422
        assert await as_(adm, C.conn_list, req(), f"agent:{a1}") and await as_(adm, C.agent_connections, a1)
        assert [r["slug"] for r in await as_(admb, C.conn_list, req(), "me")] == ["crm"], "workspace B sees none of A's connections"
        with pytest.raises(HTTPException) as x: await as_(admb, C.conn_get, "CRM_KEY", "me")
        assert x.value.status_code == 404
    run(t)

def test_api_demo_channel_anonymous_and_validation():
    async def t():
        adm, alice = U(WA, SEEDS["owner_a"], "admin"), SEEDS["alice"]
        await as_(adm, C.conn_create, C.ConnIn(slug="CRM_KEY", kind="secret"))
        demo, chan = U(WA, alice, "viewer", demo=True), U(WA, alice, "admin", auth="channel")
        for who, code in ((demo, 403), (chan, 403), (None, 401)):
            for call in (lambda: C.conn_list(req(), "me"), lambda: C.conn_get("crm", "me"), lambda: C.conn_credentials("CRM_KEY", C.CredIn(value="v")), lambda: C.tokens_exist("crm"),
                         lambda: C.conn_delete("crm", "me"), lambda: C.conn_label("crm", C.LabelIn(label="x")), lambda: C.conn_create(C.ConnIn(slug="Y1", kind="secret"))):
                if who is None:
                    with pytest.raises(HTTPException) as x: await call()
                else:
                    with pytest.raises(HTTPException) as x: await as_(who, call)
                assert x.value.status_code == code
        assert await A.q("SELECT count(*) n FROM oauth_connections WHERE kind='secret'", one=True) == {"n": 0}
        for kw in ({"slug": "../x", "kind": "secret"}, {"slug": "", "kind": "secret"}, {"slug": "a" * 80, "kind": "secret"}, {"slug": "ok", "kind": "mcp_oauth"}, {"slug": "ok", "kind": "client_credentials"}):
            with pytest.raises(ValidationError): C.ConnIn(**kw)
        with pytest.raises(ValidationError): C.CredIn(value="")
        with pytest.raises(ValidationError): C.CredIn(value="x" * (C.SECRET_MAX + 1))
        # --- create rules: provider of another workspace, name clashes
        await A.q("UPDATE oauth_providers SET name='only-b' WHERE id=%s", (SEEDS[f"p_{WB}"]["id"],))
        with pytest.raises(HTTPException) as x: await as_(adm, C.conn_create, C.ConnIn(slug="s1", kind="oauth2", provider="only-b"))
        assert x.value.status_code == 404
        with pytest.raises(HTTPException) as x: await as_(adm, C.conn_create, C.ConnIn(slug="CRM_KEY", kind="secret"))
        assert x.value.status_code == 409
        with pytest.raises(HTTPException) as x: await as_(adm, C.conn_create, C.ConnIn(slug="crm", kind="secret"))   # provider already named crm
        assert x.value.status_code == 409
        with pytest.raises(HTTPException) as x: await as_(adm, C.conn_create, C.ConnIn(slug="s2", kind="secret", provider="crm"))
        assert x.value.status_code == 422
        with pytest.raises(HTTPException) as x: await as_(U(WA, alice), C.conn_credentials, "crm", C.CredIn(value="v"))   # oauth2 connections take no pasted value
        assert x.value.status_code == 404
        assert await A.q("SELECT 1 FROM connections WHERE workspace_id=%s AND slug IN ('s1','s2')", (WA,)) == []
    run(t)

def test_profile_header_tokens_exist_label_and_definition_delete(refreshes):
    async def t():
        adm, admb, alice = U(WA, SEEDS["owner_a"], "admin"), U(WB, SEEDS["owner_b"], "admin"), SEEDS["alice"]
        await as_(adm, C.conn_create, C.ConnIn(slug="shared-crm", kind="secret", profile=True, display_name="Shared CRM"))
        await as_(adm, C.conn_create, C.ConnIn(slug="plain", kind="secret"))
        await as_(adm, C.conn_credentials, "shared-crm", C.CredIn(value="ws-wide-key", owner="workspace"))
        await as_(adm, C.conn_credentials, "plain", C.CredIn(value="ws-plain", owner="workspace"))
        me = U(WA, alice)
        rows = await as_(me, C.conn_list, req(**{"X-AOS-Profile": "shared-crm"}), "me"); assert [(r["slug"], r["connected"]) for r in rows] == [("shared-crm", True)]
        for bad in ("plain", "missing", "crm"):   # not a profile / unknown
            with pytest.raises(HTTPException) as x: await as_(me, C.conn_list, req(**{"X-AOS-Profile": bad}), "me")
            assert x.value.status_code == 404
        with pytest.raises(HTTPException) as x: await as_(U(WB, SEEDS["owner_b"]), C.conn_list, req(**{"X-AOS-Profile": "shared-crm"}), "me")   # A's profile does not exist in B
        assert x.value.status_code == 404
        assert (await C.resolve(me, None, "shared-crm", profile="shared-crm"))[0] == ("workspace", 0)
        with pytest.raises(HTTPException) as x: await C.resolve(me, None, "plain", profile="plain")
        assert x.value.status_code == 404
        with pytest.raises(HTTPException) as x: await C.resolve({**me, "auth": "channel"}, None, "shared-crm", profile="shared-crm")
        assert x.value.status_code == 403
        assert (await C.get_token(("workspace", 0), "shared-crm", ws=WA)).value == "ws-wide-key"
        with pytest.raises(HTTPException): await C.get_token(("workspace", 0), "shared-crm", ws=WB)
        # --- bulk presence check: own credentials only, booleans only
        await as_(me, C.conn_credentials, "plain", C.CredIn(value="mine")); await put_oauth(("user", alice), WA, "T")
        assert await as_(me, C.tokens_exist, "plain,crm,shared-crm,nope,../x") == {"plain": True, "crm": True, "shared-crm": False, "nope": False, "../x": False}
        # --- label PATCH
        await as_(me, C.conn_label, "plain", C.LabelIn(label="Work key")); assert (await as_(me, C.conn_get, "plain", "me"))["label"] == "Work key"
        with pytest.raises(HTTPException) as x: await as_(me, C.conn_label, "shared-crm", C.LabelIn(label="x"))   # nothing of mine to label
        assert x.value.status_code == 404
        # --- deleting the definition removes every credential under it, other workspace untouched
        await as_(admb, C.conn_create, C.ConnIn(slug="plain", kind="secret")); await as_(admb, C.conn_credentials, "plain", C.CredIn(value="b-plain", owner="workspace"))
        await as_(adm, C.conn_delete, "plain", "definition")
        assert await A.q("SELECT 1 FROM oauth_connections WHERE kind='secret' AND workspace_id=%s AND connection_id IN (SELECT id FROM connections WHERE slug='plain')", (WA,)) == []
        assert (await C.get_token(("workspace", 0), "plain", ws=WB)).value == "b-plain"
        with pytest.raises(HTTPException) as x: await as_(adm, C.conn_delete, "plain", "definition")
        assert x.value.status_code == 404
        blob = json.dumps([await as_(me, C.conn_list, req(), "me"), await as_(adm, C.conn_list, req(), "workspace")], default=str)
        assert "ws-wide-key" not in blob and "mine" not in blob and "ws-plain" not in blob and "v2:" not in blob
    run(t)

# ------------------------------------------------------------------ RLS + OAuth callback
def test_rls_hides_other_workspace_rows_even_without_a_where():
    async def t():
        alice = SEEDS["alice"]
        await put_oauth(("user", alice), WA, "A-TOK"); await put_oauth(("user", SEEDS["owner_b"]), WB, "B-TOK")
        await A.q("INSERT INTO connections(workspace_id,slug,kind) VALUES(%s,'b-only','secret')", (WB,))
        for who, ws, other in ((U(WA, alice), WA, WB), (U(WB, SEEDS["owner_b"]), WB, WA)):
            seen = await as_(who, A.q, "SELECT DISTINCT workspace_id FROM oauth_connections"); assert [r["workspace_id"] for r in seen] == [ws]
            seen = await as_(who, A.q, "SELECT DISTINCT workspace_id FROM connections"); assert {r["workspace_id"] for r in seen} == {ws}
            with pytest.raises(Exception): await as_(who, A.q, "INSERT INTO oauth_connections(provider_id,user_id,owner_type,owner_id,workspace_id,access_token_enc) VALUES(%s,1,'user',1,%s,'x')", (SEEDS[f"p_{other}"]["id"], other))
            with pytest.raises(Exception): await as_(who, A.q, "INSERT INTO connections(workspace_id,slug,kind) VALUES(%s,'evil','secret')", (other,))
            assert (await as_(who, A.q, "UPDATE oauth_connections SET status='revoked' WHERE workspace_id=%s", (other,), one=False)) in (None, [])
        assert (await A.q("SELECT count(*) n FROM oauth_connections WHERE status='revoked' AND workspace_id IN (%s,%s)", (WA, WB), one=True))["n"] == 0
    run(t)

def test_oauth_callback_refuses_provider_of_another_workspace(refreshes):
    async def t():
        alice = SEEDS["alice"]; pb = SEEDS[f"p_{WB}"]
        await A.q("INSERT INTO oauth_states(state,provider_id,user_id,verifier_enc,next,expires) VALUES('st-1124',%s,%s,NULL,'/x',now()+interval '5 minutes')", (pb["id"], alice))
        r = await as_(U(WA, alice), O.callback, req(), "st-1124", "code-1", "", "")   # alice (workspace A) completes a flow that was started for B's provider
        assert r.status_code == 400 and b"not available in this workspace" in r.body and not await A.q("SELECT 1 FROM oauth_connections WHERE workspace_id IN (%s,%s)", (WA, WB))
        assert not refreshes   # the token endpoint was never called
    run(t)

# ====================================================================================================== 11.27 runtime auth per caller + pre-run gate (fakes of 11.30, no real service)
import types  # noqa: E402
import httpx  # noqa: E402
import httpx2  # noqa: E402
import caller as CALLER, connection_runtime as R, mcp_registry as M  # noqa: E402,I001
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402
from fakes.fake_mcp_oauth import MANUAL_CLIENT, fake_oauth_mcp  # noqa: E402,F401  (fixture)

CACHE_WHOAMI = [{"name": "whoami", "description": "Identity of the token", "schema": {"type": "object", "properties": {}}, "read_only": True},
                {"name": "read_data", "description": "needs read", "schema": {"type": "object", "properties": {}}, "read_only": True},
                {"name": "write_data", "description": "needs write", "schema": {"type": "object", "properties": {}}, "read_only": False}]

async def mcp_world(fake, name="fk", ws=WA, forward=False, cache=CACHE_WHOAMI):
    """A provider + connection (kind mcp_oauth) + OAuth MCP server row pointing at a fake (mode 'none': manual client, no metadata). The saved tool list stands in for a listing."""
    p = await A.q("""INSERT INTO oauth_providers(name,display_name,authorize_url,token_url,api_base,client_id,scopes,allowed_scopes,default_redirect_uri,workspace_id,source,token_auth)
                     VALUES(%s,%s,%s,%s,%s,%s,'read write','read write',%s,%s,'mcp','post') RETURNING *""",
                  (f"mcp-{name}", f"MCP {name}", fake.base + "/authorize", fake.base + "/token", fake.mcp_url, MANUAL_CLIENT[0], "http://127.0.0.1:8023/oauth/callback", ws), one=True)
    await A.q("UPDATE oauth_providers SET client_secret_enc=%s WHERE id=%s", (O.enc(MANUAL_CLIENT[1], "oauth_providers.client_secret_enc", ws, p["id"]), p["id"]))
    d = await A.q("INSERT INTO connections(workspace_id,slug,kind,provider_id,mcp_url,display_name) VALUES(%s,%s,'mcp_oauth',%s,%s,%s) RETURNING *", (ws, f"mcp-{name}", p["id"], fake.mcp_url, name), one=True)
    s = await A.q("""INSERT INTO mcp_servers(name,url,transport,auth_type,connection_id,workspace_id,tools_cache,tools_cached_at,forward_headers)
                     VALUES(%s,%s,'streamable_http','oauth',%s,%s,%s,now(),%s) RETURNING *""", (name, fake.mcp_url, d["id"], ws, Jsonb(cache), forward), one=True)
    await M._load_one(s)   # nobody signed in -> NeedsAuth -> the saved list; tools call live with the caller's own credential
    return {"fake": fake, "p": p, "d": d, "s": s, "name": name, "ws": ws}

async def grant(w, owner, sub, scope="read write"):
    j = w["fake"].main._issue(sub, scope, MANUAL_CLIENT[0], None)
    await C.save_tokens(owner, w["p"], {"access_token": j["access_token"], "refresh_token": j["refresh_token"], "scope": scope, "expires_in": 3600, "token_type": "bearer"})

async def as_run(user, fn, *a, kind="session", agent=None, headers=None, **kw):
    """Run fn like a tool call of a run: signed-in identity + the run's caller snapshot + the calling agent."""
    t1 = admin.USER.set(user)
    t2 = CALLER.RUN.set({"caller": {"user_id": user["id"], "username": user.get("username"), "role": user.get("role"), "ws": user.get("ws"), "kind": kind, "auth": user.get("auth")}, "headers": headers or {}})
    t3 = R.CUR_AGENT.set(agent)
    try: return await fn(*a, **kw)
    finally: R.CUR_AGENT.reset(t3); CALLER.RUN.reset(t2); admin.USER.reset(t1)

def tool_of(w, tool="whoami"): return M.tools_for(w["ws"])[f"{w['name']}:{tool}"]
def auth_json(res):
    j = json.loads(res) if isinstance(res, str) else None
    assert isinstance(j, dict) and j.get("auth_required") is True, res
    return j
def bearer_calls(fake): return [c for c in fake.calls if c["path"].startswith("/mcp") and c["headers"].get("authorization")]
async def new_user(key):
    SEEDS[key] = (await A.q("INSERT INTO users(username,pw_hash) VALUES(%s,'x') RETURNING id", (f"t1124-{TAG}-{key}",), one=True))["id"]; return SEEDS[key]
async def cred(w, owner):
    return await A.q("SELECT status, status_reason, scope FROM oauth_connections WHERE owner_type=%s AND owner_id=%s AND provider_id=%s", (owner[0], owner[1], w["p"]["id"]), one=True)

@pytest.fixture(autouse=True)
def _clean_live():
    M._LIVE.clear(); yield; M._LIVE.clear()

def test_runtime_two_users_same_fake_server_see_their_own_data(fake_oauth_mcp):
    fake = fake_oauth_mcp("none")
    async def t():
        w = await mcp_world(fake); alice, bob, carol = SEEDS["alice"], SEEDS["bob"], await new_user("carol")
        await grant(w, ("user", alice), "sub-alice"); await grant(w, ("user", bob), "sub-bob")
        assert "cần kết nối" in M.status_of(w["s"]["id"])                                       # nobody signed in at load: saved list, not a failure
        tool = tool_of(w)
        ra, rb = await asyncio.gather(as_run(U(WA, alice), tool.ainvoke, {}), as_run(U(WA, bob), tool.ainvoke, {}))   # at the same time: no shared session or token
        assert "sub-alice" in str(ra) and "sub-bob" not in str(ra) and "sub-bob" in str(rb) and "sub-alice" not in str(rb)
        assert "data-for-sub-bob" in str(await as_run(U(WA, bob), tool_of(w, "read_data").ainvoke, {}))
        used = {c["headers"]["authorization"] for c in bearer_calls(fake)}
        assert len(used) == 2 and all(h.startswith("Bearer at_") for h in used)                 # exactly two different bearer tokens reached the server
        # --- a third person without a connection: no request leaves with anybody's token; the answer is the Fleet JSON with a sign-in link
        n = len(fake.calls)
        j = auth_json(await as_run(U(WA, carol), tool.ainvoke, {}))
        assert j["auth_url"] == f"/api/agent-auth/sessions/{j['auth_id']}/start" and len(fake.calls) == n
        # --- another workspace's user can not use this server at all (and is not told to connect)
        ob = await new_user("ob_user")
        res = await as_run(U(WB, ob), tool.ainvoke, {})
        assert "workspace" in str(res) and "auth_required" not in str(res) and len(fake.calls) == n
        # --- no token in the answers, the audit or the sign-in answer
        blob = json.dumps(await A.q("SELECT * FROM oauth_audit WHERE workspace_id=%s", (WA,)), default=str) + str(ra) + str(rb) + json.dumps(j)
        assert not any(h[7:] in blob for h in used)
    run(t)

def test_runtime_expired_token_refreshes_silently_then_dead_refresh_needs_reconnect(fake_oauth_mcp):
    fake = fake_oauth_mcp("none")
    async def t():
        w = await mcp_world(fake); alice, bob = SEEDS["alice"], SEEDS["bob"]; await grant(w, ("user", alice), "sub-alice")
        tool = tool_of(w)
        for tk in fake.main.tokens.values(): tk["exp"] = 0                                         # the access token died at the server (our clock says it is still fine)
        assert "sub-alice" in str(await as_run(U(WA, alice), tool.ainvoke, {}))                    # 401 -> one refresh -> retried, the person notices nothing
        assert (await cred(w, ("user", alice)))["status"] == "active" and len(fake.main.refresh) == 1   # rotated refresh token stored
        # --- the refresh token is dead too: needs_reconnect + a sign-in link, no loop
        for tk in fake.main.tokens.values(): tk["exp"] = 0
        fake.main.refresh.clear()
        j = auth_json(await as_run(U(WA, alice), tool.ainvoke, {}))
        assert j["auth_id"] and (await cred(w, ("user", alice)))["status"] == "needs_reconnect"
        # --- the server rejects even a freshly refreshed token (revoked on its side): needs_reconnect / rejected_by_server
        await grant(w, ("user", bob), "sub-bob")
        for tk in fake.main.tokens.values(): tk["exp"] = 0
        fake.token_ttl = 0
        j = auth_json(await as_run(U(WA, bob), tool.ainvoke, {}))
        c = await cred(w, ("user", bob))
        assert (c["status"], c["status_reason"]) == ("needs_reconnect", "rejected_by_server") and j["auth_id"]
    run(t)

def test_runtime_insufficient_scope_asks_for_more_and_stepups_are_capped(fake_oauth_mcp):
    fake = fake_oauth_mcp("none")
    async def t():
        w = await mcp_world(fake); alice, bob = SEEDS["alice"], SEEDS["bob"]
        await grant(w, ("user", alice), "sub-alice", scope="read"); await grant(w, ("user", bob), "sub-bob", scope="read write")
        assert "wrote-as-sub-bob" in str(await as_run(U(WA, bob), tool_of(w, "write_data").ainvoke, {}))
        j = auth_json(await as_run(U(WA, alice), tool_of(w, "write_data").ainvoke, {}))        # server: 403 insufficient_scope scope=write; alice never granted it
        s = await A.q("SELECT scope, status, user_id FROM auth_sessions WHERE auth_id=%s", (j["auth_id"],), one=True)
        assert s["scope"] == "read write" and s["status"] == "pending" and s["user_id"] == alice  # the sign-in asks for what she has PLUS what the server wants
        # --- the step-up loop itself is capped (a server that always answers 403 can not keep us busy)
        calls = []
        async def fake_get_token(owner, slug, scopes="", **kw):
            calls.append(scopes); return types.SimpleNamespace(value="T")
        orig = C.get_token; C.get_token = fake_get_token
        try:
            def handler(request): return httpx2.Response(403, headers={"www-authenticate": 'Bearer error="insufficient_scope", scope="s%d"' % len(calls)})
            async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler), auth=R.ConnectionAuth(("user", 1), "x", ws=WA)) as cl:
                r = await cl.post("http://x.test/mcp")
            assert r.status_code == 403 and len(calls) == 1 + R.MAX_STEPUPS and calls[-1].split() == ["s1", "s2"]
        finally: C.get_token = orig
    run(t)

def loc_params(resp):
    from urllib.parse import parse_qs, urlparse
    return parse_qs(urlparse(resp.headers["location"]).query)

def test_runtime_session_start_callback_longpoll_and_refusals(fake_oauth_mcp):
    fake = fake_oauth_mcp("none")
    async def t():
        import time
        w = await mcp_world(fake); carol, dave, bob, ownr = await new_user("carol"), await new_user("dave"), SEEDS["bob"], SEEDS["owner_a"]
        tool = tool_of(w)
        aid = auth_json(await as_run(U(WA, carol), tool.ainvoke, {}))["auth_id"]
        again = auth_json(await as_run(U(WA, carol), tool.ainvoke, {}))["auth_id"]
        assert again == aid and (await A.q("SELECT count(*) n FROM auth_sessions WHERE user_id=%s", (carol,), one=True))["n"] == 1   # the same open sign-in is reused, not piled up
        assert (await as_(U(WA, carol), R.session_get, aid))["status"] == "pending"
        # --- refused: another person, the same person id in another workspace, an unknown id, a made-up one
        for who in (U(WA, bob), U(WB, carol)):
            for fn, args in ((R.session_get, (aid,)), (R.session_start, (aid, req()))):
                with pytest.raises(HTTPException) as x: await as_(who, fn, *args)
                assert x.value.status_code == 404
        with pytest.raises(HTTPException) as x: await as_(U(WA, carol), R.session_get, "nope-" + "x" * 30)
        assert x.value.status_code == 404
        with pytest.raises(HTTPException) as x: await as_(U(WA, carol, auth="channel"), R.session_start, aid, req())   # a channel identity never signs in
        assert x.value.status_code == 403
        t0 = time.monotonic(); st = await as_(U(WA, carol), R.session_get, aid, 0.8)
        assert st["status"] == "pending" and time.monotonic() - t0 >= 0.7                                        # long poll waits, then says "still pending"
        # --- the real flow through the fake authorization server: start -> authorize -> callback -> completed -> the tool works
        fake.sub = "sub-carol"
        r = await as_(U(WA, carol), R.session_start, aid, req())
        assert r.status_code == 302 and r.headers["location"].startswith(fake.base + "/authorize?") and loc_params(r)["code_challenge_method"] == ["S256"]
        a = httpx.get(r.headers["location"], follow_redirects=False); assert a.status_code == 302
        q = loc_params(a); state, code = q["state"][0], q["code"][0]
        assert (await A.q("SELECT auth_id FROM oauth_states WHERE state=%s", (state,), one=True))["auth_id"] == aid
        # somebody else presenting carol's state neither completes nor burns it
        bad = await as_(U(WA, bob), O.callback, req(), state, code, "", "", "")
        assert bad.status_code == 400 and (await as_(U(WA, carol), R.session_get, aid))["status"] == "pending" and not await cred(w, ("user", bob))
        ok = await as_(U(WA, carol), O.callback, req(), state, code, "", "", "")
        assert ok.status_code == 303
        assert (await as_(U(WA, carol), R.session_get, aid))["status"] == "completed"
        assert "sub-carol" in str(await as_run(U(WA, carol), tool.ainvoke, {}))
        replay = await as_(U(WA, carol), O.callback, req(), state, code, "", "", "")
        assert replay.status_code == 400                                                                        # one-time state
        with pytest.raises(HTTPException) as x: await as_(U(WA, carol), R.session_start, aid, req())            # a finished sign-in can not be started again
        assert x.value.status_code == 409
        # --- long poll that returns as soon as the sign-in completes
        aid2 = auth_json(await as_run(U(WA, dave), tool.ainvoke, {}))["auth_id"]
        poll = asyncio.create_task(as_(U(WA, dave), R.session_get, aid2, 10))
        fake.sub = "sub-dave"; r2 = await as_(U(WA, dave), R.session_start, aid2, req()); q2 = loc_params(httpx.get(r2.headers["location"], follow_redirects=False))
        await asyncio.sleep(0.2); assert not poll.done()
        await as_(U(WA, dave), O.callback, req(), q2["state"][0], q2["code"][0], "", "", "")
        assert (await asyncio.wait_for(poll, 5))["status"] == "completed"
        # --- a refused sign-in (user says no at the provider) -> failed
        eve = await new_user("eve"); aid3 = auth_json(await as_run(U(WA, eve), tool.ainvoke, {}))["auth_id"]
        r3 = await as_(U(WA, eve), R.session_start, aid3, req())
        den = httpx.get(r3.headers["location"] + "&deny=1", follow_redirects=False); q3 = loc_params(den); assert q3["error"] == ["access_denied"]
        page = await as_(U(WA, eve), O.callback, req(), q3["state"][0], "", "access_denied", "", "")
        assert page.status_code == 400 and (await as_(U(WA, eve), R.session_get, aid3))["status"] == "failed" and not await cred(w, ("user", eve))
        # --- expiry
        frank = await new_user("frank"); aid4 = auth_json(await as_run(U(WA, frank), tool.ainvoke, {}))["auth_id"]
        await A.q("UPDATE auth_sessions SET expires=now()-interval '1 minute' WHERE auth_id=%s", (aid4,))
        with pytest.raises(HTTPException) as x: await as_(U(WA, frank), R.session_start, aid4, req())
        assert x.value.status_code == 410 and (await as_(U(WA, frank), R.session_get, aid4))["status"] == "expired"
        # --- a session for the AGENT's account: only an admin may start it; a plain member is told it is not theirs
        a1 = SEEDS["ag_a1"]
        await A.q("INSERT INTO auth_sessions(auth_id,workspace_id,user_id,owner_type,owner_id,slug,expires) VALUES('agent-sess',%s,%s,'agent',%s,%s,now()+interval '5 minutes')", (WA, bob, a1, w["d"]["slug"]))
        with pytest.raises(HTTPException) as x: await as_(U(WA, bob, "editor"), R.session_start, "agent-sess", req())
        assert x.value.status_code == 403
        e = C.CredentialRequired("not connected", slug=w["d"]["slug"])
        assert (await R.needs_connection(e, U(WA, bob, "editor"), WA, ("agent", a1), w["d"]["slug"])).startswith("Error:")
        assert auth_json(await R.needs_connection(e, U(WA, ownr, "admin"), WA, ("agent", a1), w["d"]["slug"]))["auth_url"].endswith("/start")
        assert (await R.needs_connection(e, U(WA, bob, "editor"), WA, ("user", ownr), w["d"]["slug"])).startswith("Error:")      # somebody else's account
        # --- the return address of a session must be a local path
        s1 = await R.create_session(U(WA, bob), WA, w["d"]["slug"], ("user", bob), None, "https://evil.example/x")
        assert (await A.q("SELECT return_to FROM auth_sessions WHERE auth_id=%s", (s1,), one=True))["return_to"] is None
    run(t)

class Paused(Exception): pass

def model_request(names, last="human"):
    msgs = [HumanMessage("hi")] if last == "human" else [HumanMessage("hi"), AIMessage("calling")]
    return types.SimpleNamespace(messages=msgs, tools=[types.SimpleNamespace(name=n) for n in names])

def test_runtime_gate_pauses_once_for_two_services_and_continues_after_both_connect(fake_oauth_mcp, monkeypatch):
    f1, f2 = fake_oauth_mcp("none"), fake_oauth_mcp("none")
    answers, seen = [], []
    def fake_interrupt(payload):
        seen.append(payload)
        if not answers: raise Paused()
        return answers.pop(0)
    monkeypatch.setattr("langgraph.types.interrupt", fake_interrupt)
    async def t():
        w1 = await mcp_world(f1, "fk"); w2 = await mcp_world(f2, "fk2", cache=[{"name": "ping", "description": "p", "schema": {"type": "object", "properties": {}}, "read_only": True}])
        a1, alice, bob = SEEDS["ag_a1"], SEEDS["alice"], SEEDS["bob"]; mw = agent_identity.IdentityMiddleware(a1); ran = []
        async def handler(req): ran.append(req); return "MODEL"
        async def gate(user, names=("whoami", "ping"), last="human", **kw): return await as_run(user, mw.awrap_model_call, model_request(names, last), handler, **kw)
        # --- alice has neither service: ONE pause, with both
        with pytest.raises(Paused): await gate(U(WA, alice))
        assert len(seen) == 1 and not ran
        p = seen[0]; assert p["type"] == "credential_authorization_required" and {c["slug"] for c in p["credentials"]} == {"mcp-fk", "mcp-fk2"}
        for c in p["credentials"]:
            assert c["kind"] == "mcp_oauth" and c["connect_url"] == f"/api/agent-auth/sessions/{c['auth_id']}/start" and c["reason"] == "not_connected"
        assert (await A.q("SELECT count(*) n FROM auth_sessions WHERE user_id=%s AND workspace_id=%s AND status='pending'", (alice, WA), one=True))["n"] == 2
        # --- the client says "done" but nothing was connected: the store is read again -> paused again (not trusted)
        answers.append({"type": "credential_authorization_completed", "connected_slugs": ["mcp-fk", "mcp-fk2"]})
        with pytest.raises(Paused): await gate(U(WA, alice))
        assert len(seen) == 3 and not ran and len(seen[-1]["credentials"]) == 2
        # --- one of the two connected -> the next pause lists only the other
        await grant(w1, ("user", alice), "sub-alice")
        answers.append({"type": "credential_authorization_completed", "connected_slugs": ["mcp-fk"]})
        with pytest.raises(Paused): await gate(U(WA, alice))
        assert [c["slug"] for c in seen[-1]["credentials"]] == ["mcp-fk2"] and not ran
        # --- both connected: the run goes on, no new pause
        await grant(w2, ("user", alice), "sub-alice2"); n = len(seen)
        assert await gate(U(WA, alice)) == "MODEL" and len(seen) == n and len(ran) == 1
        # --- skip / cancel lets the run go on without the connection
        with pytest.raises(Paused): await gate(U(WA, bob))
        answers.append({"skip": True}); assert await gate(U(WA, bob)) == "MODEL"
        answers.append({"cancel": True}); assert await gate(U(WA, bob)) == "MODEL"
        # --- it is not a gate for everybody: nothing is asked when ...
        n = len(seen)
        for kw in ({"kind": "run"}, {"kind": "channel"}, {"kind": "agent_key"}):                      # ... nobody is at the keyboard (schedule, channel, service key)
            assert await gate(U(WA, bob), **kw) == "MODEL"
        assert await gate(U(WA, bob), last="ai") == "MODEL"                                              # ... it is not the first model call of the run
        assert await gate(U(WA, bob), names=("unrelated",)) == "MODEL"                                   # ... the model is not offered that service's tools
        assert await gate(U(WA, bob, "viewer", demo=True)) == "MODEL"                                    # ... a demo account (the tool call refuses it by itself)
        ob = await new_user("ob2"); assert await gate(U(WB, ob)) == "MODEL"                              # ... another workspace (it does not even see the server)
        assert len(seen) == n
        # --- the agent has its own account: the person has nothing to connect for it
        await grant(w1, ("agent", a1), "sub-agent"); await grant(w2, ("agent", a1), "sub-agent2")
        assert await gate(U(WA, bob)) == "MODEL" and len(seen) == n
        # --- identity mode 'agent' without own accounts: the OWNER's accounts are used, which a chatting member can not fix -> no pause either
        await A.q("DELETE FROM oauth_connections WHERE owner_type='agent' AND workspace_id=%s", (WA,)); await set_mode(a1, "agent")
        assert await gate(U(WA, bob)) == "MODEL" and len(seen) == n
        assert agent_identity.is_auth_interrupt(p) and agent_identity.is_auth_interrupt({"type": "auth_required"}) and not agent_identity.is_auth_interrupt({"type": "approval"})
    run(t)

def test_runtime_channel_and_demo_callers_are_refused_unless_the_agent_has_its_own_account(fake_oauth_mcp):
    fake = fake_oauth_mcp("none")
    async def t():
        w = await mcp_world(fake); a1, ab, alice, ownr = SEEDS["ag_a1"], SEEDS["ag_b"], SEEDS["alice"], SEEDS["owner_a"]; tool = tool_of(w)
        await grant(w, ("agent", a1), "sub-agent"); await grant(w, ("user", alice), "sub-alice")
        ch = U(WA, alice, auth="channel"); n = len(fake.calls)
        for agent in (a1, None):                                                                          # identity mode 'user': a channel gets nothing, not even the agent's account
            res = await as_run(ch, tool.ainvoke, {}, kind="channel", agent=agent)
            assert "channel" in str(res).lower() and "auth_required" not in str(res) and len(fake.calls) == n
        res = await as_run(U(WA, alice, "viewer", demo=True), tool.ainvoke, {}, agent=a1)                # demo
        assert "demo" in str(res).lower() and len(fake.calls) == n
        res = await as_run(U(WA, alice), tool.ainvoke, {}, agent=ab)                                      # an agent id of ANOTHER workspace (forged)
        assert "not part of this workspace" in str(res) and len(fake.calls) == n
        await set_mode(a1, "agent")                                                                       # the admin's opt-in for unattended use
        res = await as_run(ch, tool.ainvoke, {}, kind="channel", agent=a1)
        assert "sub-agent" in str(res) and "sub-alice" not in str(res)                                     # the agent's own account, never the chatting person's
        await A.q("DELETE FROM oauth_connections WHERE owner_type='agent' AND workspace_id=%s", (WA,))      # no own account any more: the owner's one is used ...
        res = await as_run(ch, tool.ainvoke, {}, kind="channel", agent=a1)
        assert str(res).startswith("Error:") and "auth_required" not in str(res)                           # ... which is not connected and not the channel person's to connect: a sentence, no link
        assert not [c for c in fake.calls if c["headers"].get("authorization", "").endswith("sub-alice")]
    run(t)

def test_runtime_oauth_request_answers_with_a_signin_link_not_an_error_sentence():
    async def t():
        carol, alice, ownr, a2 = await new_user("carol"), SEEDS["alice"], SEEDS["owner_a"], SEEDS["ag_a2"]
        j = auth_json(await as_(U(WA, carol), O.tool_request, "crm", "GET", "http://127.0.0.1:9/x"))
        s = await A.q("SELECT slug, user_id, workspace_id, owner_type, status FROM auth_sessions WHERE auth_id=%s", (j["auth_id"],), one=True)
        assert (s["slug"], s["user_id"], s["workspace_id"], s["owner_type"], s["status"]) == ("crm", carol, WA, "user", "pending") and j["auth_url"].endswith("/start")
        # --- a missing key (secret connection) -> the settings link, nothing to start
        await as_(U(WA, SEEDS["owner_a"], "admin"), C.conn_create, C.ConnIn(slug="CRM_KEY", kind="secret"))
        j = auth_json(await as_(U(WA, carol), O.tool_request, "CRM_KEY", "GET", "http://127.0.0.1:9/x"))
        assert j["auth_url"] == "/settings?s=connections&service=CRM_KEY" and j["auth_id"] == "secret:CRM_KEY"
        # --- still refused (a sentence): channel in identity mode 'user', demo, workspace B asking for A's provider
        assert (await as_(U(WA, alice, auth="channel"), O.tool_request, "crm", "GET", "http://127.0.0.1:9/x")).startswith("Error: ")
        assert (await as_(U(WA, alice, "viewer", demo=True), O.tool_request, "crm", "GET", "http://127.0.0.1:9/x")).startswith("Error: ")
        ob = await new_user("ob3"); r = await as_(U(WB, ob), O.tool_request, "CRM_KEY", "GET", "http://127.0.0.1:9/x")
        assert r.startswith("Error: ") and "auth_required" not in r                                         # B has no connection of that name (A's secret slug does not exist there)
        # --- the agent works with its OWNER's account (identity mode 'agent'): a plain member is not given a link to the owner's account
        await set_mode(a2, "agent")
        r = await as_(U(WA, carol), O.tool_request, "crm", "GET", "http://127.0.0.1:9/x", None, a2)
        assert r.startswith("Error: ") and "auth_required" not in r
        assert (await A.q("SELECT count(*) n FROM auth_sessions WHERE user_id=%s", (carol,), one=True))["n"] == 1   # only the first link exists
    run(t)

def test_runtime_forwarded_headers_only_to_flagged_servers_and_people_at_the_keyboard(fake_oauth_mcp):
    f, g = fake_oauth_mcp("none"), fake_oauth_mcp("none")
    async def t():
        wf = await mcp_world(f, "fwd", forward=True); wn = await mcp_world(g, "nofwd"); alice = SEEDS["alice"]
        await grant(wf, ("user", alice), "sub-alice"); await grant(wn, ("user", alice), "sub-alice")
        hdrs = {"x-aos-forward-tenant": "acme", "x-aos-forward-authorization": "Bearer evil", "x-aos-forward-cookie": "s=1", "x-aos-forward-x-aos-secret": "no",
                "x-aos-forward-sec-fetch": "no", "x-aos-forward-bad\nname": "x", "x-aos-forward-long": "v" * 1001, "x-aos-forward-ctl": "a\r\nb: c", "x-other": "o", "authorization": "Bearer evil2"}
        def mcp(fake): return [c for c in fake.calls if c["path"].startswith("/mcp")]
        await as_run(U(WA, alice), tool_of(wf).ainvoke, {}, headers=hdrs)
        sent = mcp(f); assert sent and all(c["headers"].get("tenant") == "acme" for c in sent)
        blob = json.dumps(sent)
        assert "evil" not in blob and "s=1" not in blob and "x-other" not in blob and "x-aos-secret" not in blob and "sec-fetch" not in blob and '"ctl"' not in blob and '"long"' not in blob
        assert all(c["headers"]["authorization"].startswith("Bearer at_") for c in sent if c["headers"].get("authorization"))   # the bearer is the connection's token, never a forwarded one
        await as_run(U(WA, alice), tool_of(wn).ainvoke, {}, headers=hdrs)                                        # server without forward_headers
        assert mcp(g) and not any("tenant" in c["headers"] for c in mcp(g))
        f.calls.clear()
        for kind in ("run", "channel", "agent_key", "mail"):                                                     # not a person at the keyboard: nothing is forwarded
            await as_run(U(WA, alice), tool_of(wf).ainvoke, {}, kind=kind, headers=hdrs)
        assert mcp(f) and not any("tenant" in c["headers"] for c in mcp(f))
        rows = await A.q("SELECT detail FROM agent_audit WHERE action='forward_headers' AND workspace_id=%s", (WA,))
        assert rows and all(r["detail"]["headers"] == ["tenant"] and r["detail"]["server"] == "fwd" for r in rows) and "acme" not in json.dumps(rows, default=str)
    run(t)

def test_runtime_listing_uses_the_signed_in_admins_own_account(fake_oauth_mcp):
    fake = fake_oauth_mcp("none")
    async def t():
        w = await mcp_world(fake); alice, bob = SEEDS["alice"], SEEDS["bob"]; await grant(w, ("user", alice), "sub-alice")
        s = await A.q("SELECT * FROM mcp_servers WHERE id=%s", (w["s"]["id"],), one=True)
        assert (await as_(U(WA, bob, "admin"), M._load_one, s)).startswith("cần kết nối")                 # no account of his own: still the saved list, no request sent
        assert not fake.calls
        assert await as_(U(WA, alice, "admin"), M._load_one, s) == "ok"                                    # her account lists the live tools
        assert "Needs scope read" in (await A.q("SELECT tools_cache FROM mcp_servers WHERE id=%s", (s["id"],), one=True))["tools_cache"][1]["description"]
        assert {c["headers"]["authorization"] for c in bearer_calls(fake)}                                  # and the listing carried her token
    run(t)

def test_runtime_unit_forward_headers_and_unwrapping():
    s = {"forward_headers": True}; c = {"kind": "session"}
    assert R.forward_headers(s, c, {"x-aos-forward-a-b": "1", "x-aos-forward-": "x", "x-aos-forward-Host": "h"}) == {"a-b": "1"}
    assert R.forward_headers({}, c, {"x-aos-forward-a": "1"}) == {} and R.forward_headers({"forward_headers": False}, c, {"x-aos-forward-a": "1"}) == {}
    assert R.forward_headers(s, {"kind": "session", "agent_id": 3}, {"x-aos-forward-a": "1"}) == {}
    assert R.forward_headers(s, {"kind": "api_key"}, {"x-aos-forward-a": "1"}) == {"a": "1"}
    inner = C.CredentialRequired("x", slug="s")
    try:
        try: raise inner
        except C.CredentialRequired as e: raise RuntimeError("Client failed to connect") from e
    except RuntimeError as e: wrapped = e
    assert R.find_credential_required(wrapped) is inner and R.find_credential_required(RuntimeError("plain")) is None
    grp = BaseExceptionGroup("tg", [OSError("x"), ExceptionGroup("inner", [inner])])
    assert R.find_credential_required(grp) is inner

def test_runtime_migration_0024_up_down_in_throwaway_schema():
    mod = next(m for v, m, _ in migrations._load() if v == "0024"); sch = "t1127_" + uuid.uuid4().hex[:8]
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as root: await root.execute(f'CREATE SCHEMA "{sch}"')
        try:
            async with await psycopg.AsyncConnection.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
                q = migrations._q(c); assert await mod.plan(q)
                await mod.up(q); await mod.up(q)                                                              # twice is harmless; oauth_states is created when missing
                await c.execute("INSERT INTO auth_sessions(auth_id,workspace_id,user_id,slug,expires) VALUES('a',7,1,'s',now()+interval '1 minute')")
                for bad in ("INSERT INTO auth_sessions(auth_id,user_id,slug,expires,status) VALUES('b',1,'s',now(),'weird')",
                            "INSERT INTO auth_sessions(auth_id,user_id,slug,expires,owner_type) VALUES('c',1,'s',now(),'robot')"):
                    with pytest.raises(psycopg.errors.CheckViolation): await c.execute(bad)
                with pytest.raises(psycopg.errors.UniqueViolation): await c.execute("INSERT INTO auth_sessions(auth_id,user_id,slug,expires) VALUES('a',1,'s',now())")
                assert (await q("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name='oauth_states' AND column_name='auth_id'", (sch,)))
                await mod.down(q)
                assert not await q("SELECT 1 WHERE to_regclass('auth_sessions') IS NOT NULL")
                assert not await q("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name='oauth_states' AND column_name='auth_id'", (sch,))
                await mod.up(q)
        finally:
            async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as root: await root.execute(f'DROP SCHEMA "{sch}" CASCADE')
    asyncio.run(go())

def test_runtime_gate_and_tool_pause_inside_a_real_agent_graph(fake_oauth_mcp):
    """The same pauses in a real LangChain agent graph (checkpointer + resume): pre-run gate, then the tool-level auth_required, retried once after the person connects."""
    from langchain.agents import create_agent
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command
    class Scripted(BaseChatModel):
        script: list = []
        calls: int = 0
        @property
        def _llm_type(self): return "scripted"
        def bind_tools(self, tools, **kw): return self
        def _generate(self, messages, stop=None, run_manager=None, **kw):
            m = self.script[min(self.calls, len(self.script) - 1)]; self.calls += 1
            return ChatResult(generations=[ChatGeneration(message=m)])
    fake = fake_oauth_mcp("none")
    async def t():
        w = await mcp_world(fake); a1, alice, carol = SEEDS["ag_a1"], SEEDS["alice"], await new_user("carol")
        # --- 1) gate: the person has not connected -> the run pauses BEFORE the model; connecting + resume -> the model answers
        model = Scripted(script=[AIMessage("all good")])
        agent = create_agent(model, [tool_of(w)], middleware=[agent_identity.IdentityMiddleware(a1)], checkpointer=InMemorySaver())
        cfg = {"configurable": {"thread_id": "g1"}}
        r = await as_run(U(WA, alice), agent.ainvoke, {"messages": [HumanMessage("hi")]}, cfg)
        ints = r.get("__interrupt__") or []; assert len(ints) == 1 and model.calls == 0
        assert ints[0].value["type"] == "credential_authorization_required" and [c["slug"] for c in ints[0].value["credentials"]] == ["mcp-fk"]
        await grant(w, ("user", alice), "sub-alice")
        r = await as_run(U(WA, alice), agent.ainvoke, Command(resume={"type": "credential_authorization_completed", "connected_slugs": ["mcp-fk"]}), cfg)
        assert not r.get("__interrupt__") and r["messages"][-1].content == "all good" and model.calls == 1
        # --- 2) skip the gate; the model calls the tool; the tool answers auth_required -> pause -> connect -> the SAME call is retried once and sees her own data
        model = Scripted(script=[AIMessage("", tool_calls=[{"name": "whoami", "args": {}, "id": "c1", "type": "tool_call"}]), AIMessage("fin")])
        agent = create_agent(model, [tool_of(w)], middleware=[agent_identity.IdentityMiddleware(a1)], checkpointer=InMemorySaver())
        cfg = {"configurable": {"thread_id": "g2"}}
        r = await as_run(U(WA, carol), agent.ainvoke, {"messages": [HumanMessage("hi")]}, cfg); assert r["__interrupt__"][0].value["type"] == "credential_authorization_required"
        r = await as_run(U(WA, carol), agent.ainvoke, Command(resume={"skip": True}), cfg)
        iv = r["__interrupt__"][0].value; assert iv["type"] == "auth_required" and iv["tool"] == "whoami" and iv["auth_url"].endswith("/start") and iv["auth_id"]
        assert (await A.q("SELECT owner_type, user_id FROM auth_sessions WHERE auth_id=%s", (iv["auth_id"],), one=True)) == {"owner_type": "user", "user_id": carol}
        await grant(w, ("user", carol), "sub-carol")
        r = await as_run(U(WA, carol), agent.ainvoke, Command(resume={"authorized": True}), cfg)
        tm = [m for m in r["messages"] if m.type == "tool"]; assert not r.get("__interrupt__") and "sub-carol" in str(tm[-1].content) and tm[-1].status != "error" and r["messages"][-1].content == "fin"
        # --- 3) nobody at the keyboard (a scheduled run): no pause, the model gets a plain sentence it can relay
        model = Scripted(script=[AIMessage("", tool_calls=[{"name": "whoami", "args": {}, "id": "c2", "type": "tool_call"}]), AIMessage("fin")])
        agent = create_agent(model, [tool_of(w)], middleware=[agent_identity.IdentityMiddleware(a1)], checkpointer=InMemorySaver())
        dave = await new_user("dave2")
        r = await as_run(U(WA, dave), agent.ainvoke, {"messages": [HumanMessage("hi")]}, {"configurable": {"thread_id": "g3"}}, kind="run")
        tm = [m for m in r["messages"] if m.type == "tool"]; assert not r.get("__interrupt__") and "unattended" in str(tm[-1].content) and tm[-1].status == "error"
    run(t)
