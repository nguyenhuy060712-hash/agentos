"""GĐ11 card 11.25 (OAuth engine upgrade + per-provider redirect, merges 11.25b). In-process against the fake authorization servers of tests/fakes/fake_mcp_oauth.py (11.30:
main = MCP-style AS with PKCE / resource / iss / rotating refresh, /google = Google-like (no rotation, offline), /ms = Microsoft-like (rotation)); handlers are called with a faked
current user (admin.USER) on a private pool under the RLS role. Every OAuth HTTP call goes through netguard (conftest sets AOS_EGRESS_ALLOW=127.0.0.0/8 for the fakes; the policy tests
clear it). A few HTTP checks run against this test's OWN server on a free port (started by the module fixture, killed at the end; never the shared :8020).
Negative tests: forged / expired / replayed / foreign-user / foreign-workspace state, another workspace's provider, private & metadata IP token endpoints, expired refresh,
forged X-Forwarded-Host, non-admin owners, demo, XSS reflection, secrets in audit / API."""
import asyncio, base64, hashlib, json, os, re, signal, subprocess, sys, time, uuid
from urllib.parse import parse_qs, urlparse

import httpx, psycopg, pytest
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError
from starlette.requests import Request

os.environ.setdefault("GOOGLE_API_KEY", "x-not-used")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import deepagent_api as A  # noqa: E402,I001  (must come first: circular imports)
import admin, migrations, netguard, oauth_tools as O, connections as C  # noqa: E402
from fakes.fake_mcp_oauth import MANUAL_CLIENT, fake_oauth_mcp  # noqa: E402,F401  (fixture)

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
os.environ.setdefault("PG_URI", PG)
TAG = uuid.uuid4().hex[:6]
WA, WB = 93025, 93026
SEEDS: dict = {}
BASE_URL = "https://aos.example.test"
LOOP = "http://127.0.0.1:8765/oauth/callback"
APP = os.path.join(os.path.dirname(__file__), "..")

def U(ws, uid, role="editor", **kw): return {"id": uid, "username": f"u{uid}", "role": role, "ws": ws, **kw}

def run(fn):
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as c:
            await migrations.run_pending(c)
        async with AsyncConnectionPool(PG, min_size=1, max_size=3, open=False, kwargs={"autocommit": True, "row_factory": dict_row}) as pool:
            await pool.open(); A.S["pool"] = pool; A.S["rls"] = admin.rls_ctx
            for g in ("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO aos_rls", "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO aos_rls"):
                for i in range(5):   # parallel runs of other cards GRANT/ALTER at the same time ("tuple concurrently updated")
                    try: await A.q(g); break
                    except psycopg.errors.InternalError_:
                        if i == 4: raise
                        await asyncio.sleep(0.5)
            for t in ("connections", "oauth_connections", "oauth_providers"):   # what admin.rls_init does at boot
                await A.q(f"ALTER TABLE {t} ALTER COLUMN workspace_id SET DEFAULT coalesce(aos_ws(), 1)")
                await admin._policy(t, "aos_ws() IS NULL OR workspace_id = aos_ws()")
            await seed()
            try: return await fn()
            finally: await cleanup()
    return asyncio.run(go())

async def seed():
    for w in (WA, WB): await A.q("INSERT INTO workspaces(id,name) VALUES(%s,%s) ON CONFLICT DO NOTHING", (w, f"t1125-{TAG}-{w}"))
    for k in ("admin_a", "alice", "bob", "admin_b"):
        SEEDS[k] = (await A.q("INSERT INTO users(username,pw_hash) VALUES(%s,'x') RETURNING id", (f"t1125-{TAG}-{k}",), one=True))["id"]
    for k, w, o in (("ag_a", WA, "admin_a"), ("ag_b", WB, "admin_b")):
        SEEDS[k] = (await A.q("INSERT INTO agents(name,workspace_id,owner_id) VALUES(%s,%s,%s) RETURNING id", (f"t1125-{TAG}-{k}", w, SEEDS[o]), one=True))["id"]

async def cleanup():
    uids = [SEEDS[k] for k in ("admin_a", "alice", "bob", "admin_b") if k in SEEDS]; ids = [SEEDS[k] for k in ("ag_a", "ag_b") if k in SEEDS]
    await A.q("DELETE FROM oauth_connections WHERE workspace_id IN (%s,%s)", (WA, WB)); await A.q("DELETE FROM connections WHERE workspace_id IN (%s,%s)", (WA, WB))
    await A.q("DELETE FROM oauth_providers WHERE workspace_id IN (%s,%s)", (WA, WB)); await A.q("DELETE FROM oauth_audit WHERE workspace_id IN (%s,%s)", (WA, WB))
    await A.q("DELETE FROM oauth_states WHERE workspace_id IN (%s,%s) OR user_id = ANY(%s)", (WA, WB, uids))
    if ids: await A.q("DELETE FROM agent_identity WHERE agent_id = ANY(%s)", (ids,)); await A.q("DELETE FROM agents WHERE id = ANY(%s)", (ids,))
    await A.q("DELETE FROM users WHERE username LIKE %s", (f"t1125-{TAG}-%",))
    await A.q("DELETE FROM crypto_deks WHERE workspace_id IN (%s,%s)", (WA, WB)); await A.q("DELETE FROM workspaces WHERE id IN (%s,%s)", (WA, WB))
    SEEDS.clear()

async def as_(user, fn, *a, **kw):
    tok = admin.USER.set(user)
    try: return await fn(*a, **kw)
    finally: admin.USER.reset(tok)

def req(headers=None, client=None, host="aos.local"):
    sc = {"type": "http", "headers": [(k.lower().encode(), v.encode()) for k, v in {"host": host, **(headers or {})}.items()], "query_string": b"", "scheme": "http", "path": "/",
          "server": (host, 80)}
    if client: sc["client"] = (client, 4242)
    return Request(sc)

@pytest.fixture
def fk(fake_oauth_mcp):
    """fake_oauth_mcp(mode) with 1-hour tokens (the fake's default 60 s would make every get_token refresh); a test that wants expiry calls expire()."""
    def make(mode="dcr"):
        s = fake_oauth_mcp(mode); s.token_ttl = 3600; return s
    return make

@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AOS_PUBLIC_BASE", BASE_URL)
    for k in ("AOS_TRUSTED_PROXIES", "AOS_LOOPBACK_PORT"): monkeypatch.delenv(k, raising=False)

def alice(ws=None): return U(ws or WA, SEEDS["alice"])
def bob(): return U(WA, SEEDS["bob"])
def adm(ws=WA): return U(ws, SEEDS["admin_a" if ws == WA else "admin_b"], "admin")

KINDS = {"main": ("", MANUAL_CLIENT, "read write", {}, "web"), "google": ("/google", ("google-client-id", "google-secret"), "openid email", {"access_type": "offline"}, "loopback_paste"),
         "ms": ("/ms", ("ms-client-id", "ms-secret"), "openid offline_access User.Read", {}, "loopback_paste")}

async def mk(f, kind="main", name=None, ws=WA, **over):
    """A provider made through the admin API handler against fake server `f`."""
    pre, (cid, sec), scopes, extra, mode = KINDS[kind]
    d = dict(name=name or f"{kind}p", authorize_url=f.base + pre + "/authorize", token_url=f.base + pre + "/token", revoke_url=f.base + pre + "/revoke",
             userinfo_url=f.base + pre + "/api/userinfo", api_base=f.base + pre + "/api/", client_id=cid, client_secret=sec, scopes=scopes, extra_params=extra, redirect_mode=mode)
    d.update(over)
    return await as_(adm(ws), O.prov_new, O.ProviderIn(**d))

def authorize(url):
    r = httpx.get(url, follow_redirects=False); assert r.status_code == 302, r.text
    return r.headers["location"]

async def begin(user, name, request=None, body=None):
    """connect + authorize at the fake -> (connect response, the address the browser ends up on, state)."""
    c = await as_(user, O.connect, name, request or req(), body)
    loc = authorize(c["authorize_url"]); return c, loc, parse_qs(urlparse(loc).query)["state"][0]

async def finish_cb(user, loc, request=None):
    q = {k: v[0] for k, v in parse_qs(urlparse(loc).query).items()}
    return await as_(user, O.callback, request or req(), q.get("state", ""), q.get("code", ""), q.get("error", ""), q.get("error_description", ""), q.get("iss", ""))

async def paste(user, loc): return await as_(user, O.paste_callback, O.PasteIn(url=loc))

async def conn_row(provider_id, owner=None):
    """The credential row of a provider (any owner, or the given (type, id))."""
    if owner: return await A.q("SELECT * FROM oauth_connections WHERE provider_id=%s AND owner_type=%s AND owner_id=%s", (provider_id, owner[0], owner[1]), one=True)
    return await A.q("SELECT * FROM oauth_connections WHERE provider_id=%s ORDER BY id LIMIT 1", (provider_id,), one=True)

async def expire(provider_id):
    await A.q("UPDATE oauth_connections SET expires_at = now() - interval '5 minutes' WHERE provider_id=%s", (provider_id,))

def posts(f, path): return [c for c in f.calls if c["method"] == "POST" and c["path"] == path]

async def token_of(user_dict, owner, name, ws=WA, **kw): return (await as_(user_dict, C.get_token, owner, name, ws=ws, **kw)).value

# ================================================================== authorization code: engine options
@pytest.mark.parametrize("auth", ["basic", "body", "post"])
def test_authorization_code_flow_pkce_resource_iss_userinfo_for_each_token_auth(fk, auth):
    f = fk("dcr")
    async def t():
        p = await mk(f, "main", token_auth=auth, issuer=f.issuer, iss_supported=True, resource=f.mcp_url)
        c, loc, state = await begin(alice(), "mainp")
        q = parse_qs(urlparse(c["authorize_url"]).query)
        assert q["response_type"] == ["code"] and q["client_id"] == [MANUAL_CLIENT[0]] and q["redirect_uri"] == [BASE_URL + "/oauth/callback"] and q["code_challenge_method"] == ["S256"]
        assert q["resource"] == [f.mcp_url] and q["scope"] == ["read write"] and len(q["code_challenge"][0]) == 43 and c["redirect_mode"] == "web" and not c["paste"]
        assert parse_qs(urlparse(loc).query)["iss"] == [f.issuer]
        r = await finish_cb(alice(), loc); assert r.status_code == 303 and r.headers["location"] == "/settings?s=connections"
        tok = await token_of(alice(), ("user", SEEDS["alice"]), "mainp")
        assert f.main.tokens[tok]["resource"] == f.mcp_url and f.main.tokens[tok]["scope"] == "read write"   # `resource` also reached the token endpoint (stored with the grant)
        row = await conn_row(p["id"]); assert row["account"] == "user-1@example.test" and row["status"] == "active"
        assert (await A.q("SELECT redirect_uri, issuer FROM oauth_states WHERE state=%s", (state,), one=True)) is None   # one-time: gone
    run(t)

def test_public_client_token_auth_none_sends_no_secret(fk):
    f = fk("dcr")
    reg = httpx.post(f.urls["register"], json={"redirect_uris": [BASE_URL + "/oauth/callback"], "token_endpoint_auth_method": "none"}); assert reg.status_code == 201
    cid = reg.json()["client_id"]
    async def t():
        await mk(f, "main", name="pub", token_auth="none", client_id=cid, client_secret=None)
        c, loc, _ = await begin(alice(), "pub"); assert (await finish_cb(alice(), loc)).status_code == 303
        assert await token_of(alice(), ("user", SEEDS["alice"]), "pub") in f.main.tokens
    run(t)
    d, auth = O._client_auth({"token_auth": "none", "client_id": "c1", "_secret_plain": "TOPSECRET"}, {"grant_type": "x"})   # unit: a stored secret is never sent for `none`
    assert d == {"grant_type": "x", "client_id": "c1"} and auth is None

def test_iss_mismatch_and_missing_iss_are_refused_and_never_reach_the_token_endpoint(fk):
    f = fk("dcr"); g = fk("none")
    async def t():
        pa = await mk(f, "main", name="strict", issuer=f.issuer, iss_supported=True)
        f.wrong_iss = True
        _, loc, _ = await begin(alice(), "strict"); assert "evil.example" in loc
        r = await finish_cb(alice(), loc); assert r.status_code == 400 and b"unexpected issuer" in r.body
        assert not posts(f, "/token") and not await conn_row(pa["id"])
        f.wrong_iss = False
        pb = await mk(g, "main", name="needsiss", issuer=g.issuer, iss_supported=True)   # the server (mode none) sends no `iss` at all, the provider says it must
        _, loc, _ = await begin(alice(), "needsiss"); assert "iss=" not in loc
        r = await finish_cb(alice(), loc); assert r.status_code == 400 and b"did not prove" in r.body and not posts(g, "/token") and not await conn_row(pb["id"])
        await mk(g, "main", name="plain")   # a GitHub-like manual provider: no issuer, no iss -> fine
        _, loc, _ = await begin(alice(), "plain"); assert (await finish_cb(alice(), loc)).status_code == 303
    run(t)

def test_scopes_are_clipped_to_allowed_scopes_and_a_member_can_only_narrow(fk):
    f = fk("dcr")
    async def t():
        with pytest.raises(HTTPException) as e: await mk(f, "main", name="bad", scopes="read write admin", allowed_scopes="read write")   # default scopes above the ceiling
        assert e.value.status_code == 422
        await mk(f, "main", name="clip", scopes="read write", allowed_scopes="read write")
        await A.q("UPDATE oauth_providers SET allowed_scopes='read' WHERE name='clip' AND workspace_id=%s", (WA,))   # ceiling narrowed after the fact (SQL / older row)
        c = await as_(alice(), O.connect, "clip", req(), None); assert parse_qs(urlparse(c["authorize_url"]).query)["scope"] == ["read"]   # provider default clipped
        await mk(f, "main", name="narrow", scopes="read write")   # no allowed_scopes: the provider's own scopes are the ceiling
        c = await as_(alice(), O.connect, "narrow", req(), O.ConnectIn(scopes="read")); assert parse_qs(urlparse(c["authorize_url"]).query)["scope"] == ["read"]
        c = await as_(alice(), O.connect, "narrow", req(), O.ConnectIn(scopes="read write admin delete")); assert parse_qs(urlparse(c["authorize_url"]).query)["scope"] == ["read write"]
        c = await as_(alice(), O.connect, "narrow", req(), O.ConnectIn(scopes="admin")); assert "scope" not in parse_qs(urlparse(c["authorize_url"]).query)
        st = await A.q("SELECT scope FROM oauth_states WHERE user_id=%s ORDER BY created DESC LIMIT 1", (SEEDS["alice"],), one=True); assert st["scope"] == ""
        p = await A.q("SELECT id FROM oauth_providers WHERE workspace_id=%s AND name='clip'", (WA,), one=True)   # get_token also enforces the provider ceiling
        with pytest.raises(HTTPException) as e: await as_(alice(), C.get_token, ("user", SEEDS["alice"]), "clip", ["read", "write"], ws=WA)
        assert e.value.status_code == 403
    run(t)

def test_extra_authorize_params_can_never_override_the_security_ones(fk):
    f = fk("dcr")
    for k in ("redirect_uri", "state", "client_id", "scope", "resource", "code_challenge", "response_type", "Redirect_URI"):
        with pytest.raises(ValidationError): O.ProviderIn(name="x1", authorize_url="https://a/", token_url="https://a/t", api_base="https://a/", client_id="c", extra_params={k: "evil"})
    async def t():
        await mk(f, "main", name="xp", extra_params={"prompt": "consent"})
        await A.q("UPDATE oauth_providers SET extra_params=%s WHERE name='xp' AND workspace_id=%s", (Jsonb({"redirect_uri": "https://evil.example/cb", "state": "fixed", "prompt": "consent"}), WA))   # a row written around the API
        c = await as_(alice(), O.connect, "xp", req(), None); q = parse_qs(urlparse(c["authorize_url"]).query)
        assert q["redirect_uri"] == [BASE_URL + "/oauth/callback"] and q["state"][0] != "fixed" and q["prompt"] == ["consent"]
    run(t)

def test_token_call_composition_auth_modes_token_params_and_sealed_headers(monkeypatch):
    seen = []
    def handler(r):
        seen.append(r); return httpx.Response(200, json={"access_token": "AT", "token_type": "bearer"})
    monkeypatch.setattr(O.netguard, "client", lambda purpose, **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw))
    base = {"token_url": "https://idp.example/token", "client_id": "cid", "_secret_plain": "SEC", "token_params": {"audience": "api", "grant_type": "evil"}, "_headers_plain": {"X-Api-Key": "HDR-1"}}
    async def go(**over): return await O._token_call({**base, "token_auth": "body", **over}, {"grant_type": "authorization_code", "code": "c"})
    asyncio.run(go()); body = parse_qs(seen[-1].content.decode())
    assert body["client_id"] == ["cid"] and body["client_secret"] == ["SEC"] and body["audience"] == ["api"] and body["grant_type"] == ["authorization_code"]   # flow fields win over token_params
    assert seen[-1].headers["x-api-key"] == "HDR-1" and seen[-1].headers["accept"] == "application/json" and "authorization" not in seen[-1].headers
    asyncio.run(go(token_auth="basic")); body = parse_qs(seen[-1].content.decode())
    assert seen[-1].headers["authorization"] == "Basic " + base64.b64encode(b"cid:SEC").decode() and "client_secret" not in body and "client_id" not in body
    asyncio.run(go(token_auth="none")); body = parse_qs(seen[-1].content.decode())
    assert body["client_id"] == ["cid"] and "client_secret" not in body and "authorization" not in seen[-1].headers

def test_token_headers_are_sealed_write_only_and_validated(fk):
    f = fk("dcr")
    for bad in ({"Host": "evil"}, {"Content-Length": "1"}, {"X-A\r\nB": "v"}, {"X-A": "v\r\nInjected: 1"}, {"bad name": "v"}):
        with pytest.raises(ValidationError): O.ProviderIn(name="x1", authorize_url="https://a/", token_url="https://a/t", api_base="https://a/", client_id="c", token_headers=bad)
    for bad in ({"client_secret": "x"}, {"refresh_token": "x"}, {"Password": "x"}):
        with pytest.raises(ValidationError): O.ProviderIn(name="x1", authorize_url="https://a/", token_url="https://a/t", api_base="https://a/", client_id="c", token_params=bad)
    async def t():
        p = await mk(f, "main", name="hdr", token_headers={"X-Tenant-Key": "HDR-SECRET-9"}, token_params={"audience": "api"})
        assert p["has_token_headers"] and "HDR-SECRET-9" not in json.dumps(p, default=str) and "token_headers_enc" not in p and "client_secret_enc" not in p
        raw = await A.q("SELECT token_headers_enc, client_secret_enc, token_params FROM oauth_providers WHERE id=%s", (p["id"],), one=True)
        assert "HDR-SECRET-9" not in raw["token_headers_enc"] and MANUAL_CLIENT[1] not in raw["client_secret_enc"] and raw["token_params"] == {"audience": "api"}
        row = await A.q("SELECT * FROM oauth_providers WHERE id=%s", (p["id"],), one=True); assert O._token_headers(row) == {"X-Tenant-Key": "HDR-SECRET-9"}
        c, loc, _ = await begin(alice(), "hdr"); assert (await finish_cb(alice(), loc)).status_code == 303
        assert posts(f, "/token")[-1]["headers"]["x-tenant-key"] == "HDR-SECRET-9"   # reached the token endpoint
        body = O.ProviderIn(name="hdr", authorize_url=p["authorize_url"], token_url=p["token_url"], api_base=p["api_base"], client_id=MANUAL_CLIENT[0], scopes="read")
        await as_(adm(), O.prov_upd, p["id"], body)   # omitted = kept (write-only)
        assert O._token_headers(await A.q("SELECT * FROM oauth_providers WHERE id=%s", (p["id"],), one=True)) == {"X-Tenant-Key": "HDR-SECRET-9"}
        body.token_headers = {}; await as_(adm(), O.prov_upd, p["id"], body)   # {} clears
        assert (await A.q("SELECT token_headers_enc FROM oauth_providers WHERE id=%s", (p["id"],), one=True))["token_headers_enc"] is None
    run(t)

# ================================================================== client_credentials
def test_client_credentials_fetch_on_create_workspace_owned_refresh_is_fetch_again(fk):
    f = fk("dcr")
    async def t():
        p = await mk(f, "main", name="ccp", grant_type="client_credentials", authorize_url="", scopes="read", pkce=False)
        row = await A.q("SELECT owner_type, owner_id, kind, expires_at FROM oauth_connections WHERE provider_id=%s", (p["id"],), one=True)
        assert (row["owner_type"], row["owner_id"], row["kind"]) == ("workspace", 0, "oauth") and row["expires_at"] is not None
        owner, _ = await as_(alice(), C.resolve, alice(), None, "ccp"); assert owner == ("workspace", 0)   # no person signs in: everyone in the workspace uses the workspace credential
        t1 = await token_of(alice(), owner, "ccp"); assert f.main.tokens[t1]["sub"] == "client:" + MANUAL_CLIENT[0] and f.main.tokens[t1]["scope"] == "read"
        with pytest.raises(HTTPException) as e: await as_(alice(), O.connect, "ccp", req(), None)
        assert e.value.status_code == 409
        await expire(p["id"]); n = len(posts(f, "/token"))
        t2 = await token_of(alice(), owner, "ccp"); assert t2 != t1 and len(posts(f, "/token")) == n + 1 and t2 in f.main.tokens   # refresh = fetch again
        with pytest.raises(HTTPException) as e: await token_of(adm(WB), ("workspace", 0), "ccp", ws=WB)   # the other workspace has no such provider
        assert e.value.status_code == 404
    run(t)

def test_client_credentials_failure_means_nothing_is_created_or_changed(fk):
    f = fk("dcr")
    async def t():
        with pytest.raises(HTTPException) as e: await mk(f, "main", name="ccbad", grant_type="client_credentials", authorize_url="", client_secret="WRONG")   # invalid_client
        assert e.value.status_code == 400
        with pytest.raises(HTTPException) as e: await mk(f, "main", name="ccdown", grant_type="client_credentials", authorize_url="", token_url="http://127.0.0.1:9/token")   # nobody listens
        assert e.value.status_code == 502
        with pytest.raises(HTTPException) as e: await mk(f, "main", name="ccnosecret", grant_type="client_credentials", authorize_url="", client_secret=None)
        assert e.value.status_code == 422
        for n in ("ccbad", "ccdown", "ccnosecret"): assert not await A.q("SELECT 1 FROM oauth_providers WHERE name=%s AND workspace_id=%s", (n, WA), one=True)
        assert not await A.q("SELECT 1 FROM connections WHERE workspace_id=%s AND slug LIKE 'cc%%'", (WA,), one=True)
        p = await mk(f, "main", name="ccok", grant_type="client_credentials", authorize_url="")
        before = (await A.q("SELECT client_secret_enc FROM oauth_providers WHERE id=%s", (p["id"],), one=True))["client_secret_enc"]
        body = O.ProviderIn(name="ccok", token_url=f.base + "/token", api_base=f.base + "/api/", client_id=MANUAL_CLIENT[0], client_secret="WRONG", grant_type="client_credentials")
        with pytest.raises(HTTPException) as e: await as_(adm(), O.prov_upd, p["id"], body)   # an update that cannot get a token changes nothing
        assert e.value.status_code == 400 and (await A.q("SELECT client_secret_enc FROM oauth_providers WHERE id=%s", (p["id"],), one=True))["client_secret_enc"] == before
        body.client_secret = MANUAL_CLIENT[1]; body.grant_type = "authorization_code"; body.authorize_url = f.base + "/authorize"
        with pytest.raises(HTTPException) as e: await as_(adm(), O.prov_upd, p["id"], body)
        assert e.value.status_code == 422   # grant_type is not editable
    run(t)

# ================================================================== paste-the-address (Google / Microsoft fakes), refresh rotation, failure statuses
def test_google_and_microsoft_fakes_connect_by_paste_with_loopback_redirect(fk, monkeypatch):
    f = fk("dcr")
    async def t():
        for kind in ("google", "ms"):
            p = await mk(f, kind)
            c, loc, state = await begin(alice(), f"{kind}p")
            assert c["redirect_uri"] == LOOP and c["redirect_mode"] == "loopback_paste" and c["paste"] and loc.startswith(LOOP + "?")   # the browser would end on a page it cannot open
            r = await paste(alice(), loc); assert r["ok"] and r["provider"] == f"{kind}p" and r["account"] == "user-1@example.test"
            row = await conn_row(p["id"]); assert row["status"] == "active" and row["refresh_token_enc"]
            tok = await token_of(alice(), ("user", SEEDS["alice"]), f"{kind}p")
            assert tok in getattr(f, kind).tokens
            assert not await A.q("SELECT 1 FROM oauth_states WHERE state=%s", (state,), one=True)
        monkeypatch.setenv("AOS_LOOPBACK_PORT", "9911")
        c = await as_(alice(), O.connect, "googlep", req(), None); assert c["redirect_uri"] == "http://127.0.0.1:9911/oauth/callback"
    run(t)

def test_refresh_rotation_replaces_the_refresh_token_and_no_rotation_keeps_it(fk):
    f = fk("dcr")
    async def t():
        for kind, rotates in (("ms", True), ("google", False)):
            p = await mk(f, kind); _, loc, _ = await begin(alice(), f"{kind}p"); await paste(alice(), loc)
            row = await conn_row(p["id"]); rt1 = O._tok(row, "refresh_token_enc"); at1 = O._tok(row, "access_token_enc")
            await expire(p["id"])
            at2 = await token_of(alice(), ("user", SEEDS["alice"]), f"{kind}p"); assert at2 != at1
            row = await conn_row(p["id"]); rt2 = O._tok(row, "refresh_token_enc"); assert row["status"] == "active" and row["expires_at"].timestamp() > time.time() + 30
            assert (rt2 != rt1) is rotates
            if rotates:
                r = httpx.post(f.base + "/ms/token", data={"grant_type": "refresh_token", "refresh_token": rt1, "client_id": "ms-client-id", "client_secret": "ms-secret"})
                assert r.status_code == 400 and r.json()["error"] == "invalid_grant"   # the old one is dead at the server: we really stored the new one
    run(t)

def test_invalid_grant_needs_reconnect_but_network_trouble_does_not(fk, monkeypatch):
    f = fk("dcr")
    async def t():
        p = await mk(f, "ms"); _, loc, _ = await begin(alice(), "msp"); await paste(alice(), loc)
        await expire(p["id"]); f.ms.refresh.clear()   # the server forgot / revoked our refresh token
        with pytest.raises(HTTPException) as e: await token_of(alice(), ("user", SEEDS["alice"]), "msp")
        assert e.value.status_code == 400 and "invalid_grant" in e.value.detail
        row = await conn_row(p["id"]); assert (row["status"], row["status_reason"]) == ("needs_reconnect", "refresh_failed")
        _, loc, _ = await begin(alice(), "msp"); await paste(alice(), loc)   # reconnecting makes it active again
        row = await conn_row(p["id"]); assert (row["status"], row["status_reason"]) == ("active", None)
        await expire(p["id"])
        async def down(p_, data): raise O.OAuthError(502, "Token endpoint unreachable (ConnectError)", "unreachable")
        monkeypatch.setattr(O, "_token_call", down)
        with pytest.raises(HTTPException) as e: await token_of(alice(), ("user", SEEDS["alice"]), "msp")
        assert e.value.status_code == 502 and (await conn_row(p["id"]))["status"] == "active"   # we could not ask: the account is not condemned
        async def five(p_, data): raise O.OAuthError(502, "Token endpoint error: temporarily_unavailable", "temporarily_unavailable")
        monkeypatch.setattr(O, "_token_call", five)
        with pytest.raises(HTTPException): await token_of(alice(), ("user", SEEDS["alice"]), "msp")
        assert (await conn_row(p["id"]))["status"] == "active"
    run(t)

def test_refresh_to_a_blocked_endpoint_marks_error_not_reconnect(fk, monkeypatch):
    f = fk("dcr")
    async def t():
        p = await mk(f, "ms"); _, loc, _ = await begin(alice(), "msp"); await paste(alice(), loc); await expire(p["id"])
        monkeypatch.setenv("AOS_EGRESS_ALLOW", ""); n = len(f.calls)   # the operator no longer allows loopback: the policy refuses, the fake is never reached
        with pytest.raises(HTTPException) as e: await token_of(alice(), ("user", SEEDS["alice"]), "msp")
        assert e.value.status_code == 502 and "network policy" in e.value.detail and len(f.calls) == n
        row = await conn_row(p["id"]); assert (row["status"], row["status_reason"]) == ("error", "egress_blocked")
    run(t)

# ================================================================== state: forged / expired / replayed / foreign
def test_state_forged_replayed_expired_foreign_user_workspace_and_address(fk):
    f = fk("dcr")
    async def t():
        p = await mk(f, "google"); path = "/google/token"
        with pytest.raises(HTTPException) as e: await paste(alice(), f"{LOOP}?code=abc&state={'x' * 43}")   # forged
        assert e.value.status_code == 400
        for junk in ("", "a" * 500):
            with pytest.raises(HTTPException): await paste(alice(), f"{LOOP}?code=abc&state={junk}")
        _, loc, st = await begin(alice(), "googlep")   # replay
        await paste(alice(), loc)
        with pytest.raises(HTTPException) as e: await paste(alice(), loc)
        assert e.value.status_code == 400 and len(posts(f, path)) == 1
        r = await finish_cb(alice(), loc); assert r.status_code == 400 and b"Invalid or expired state" in r.body and len(posts(f, path)) == 1   # also through the callback route
        _, loc, st = await begin(alice(), "googlep")   # expired
        await A.q("UPDATE oauth_states SET expires = now() - interval '1 second' WHERE state=%s", (st,))
        with pytest.raises(HTTPException): await paste(alice(), loc)
        _, loc, st = await begin(alice(), "googlep")   # somebody else's: refused, and it does not burn alice's state
        with pytest.raises(HTTPException): await paste(bob(), loc)
        r = await finish_cb(bob(), loc); assert r.status_code == 400
        assert await A.q("SELECT 1 FROM oauth_states WHERE state=%s", (st,), one=True) and len(posts(f, path)) == 1 and not await conn_row(p["id"], ("user", SEEDS["bob"]))
        n = len(posts(f, path)); r = await paste(alice(), loc); assert r["ok"] and len(posts(f, path)) == n + 1
        _, loc, st = await begin(alice(), "googlep")   # the same person but signed in to ANOTHER workspace
        r = await finish_cb(alice(WB), loc); assert r.status_code == 400 and b"not available in this workspace" in r.body
        assert len(posts(f, path)) == n + 1 and not await A.q("SELECT 1 FROM oauth_connections WHERE workspace_id=%s", (WB,), one=True)
        _, loc, st = await begin(alice(), "googlep")   # a pasted page that is not our redirect address
        code = parse_qs(urlparse(loc).query)["code"][0]
        with pytest.raises(HTTPException) as e: await paste(alice(), f"https://evil.example/oauth/callback?code={code}&state={st}")
        assert e.value.status_code == 400 and len(posts(f, path)) == n + 1
        _, loc, st = await begin(alice(), "googlep")   # provider removed / disabled while the person was signing in
        await A.q("UPDATE oauth_providers SET enabled=false WHERE id=%s", (p["id"],))
        with pytest.raises(HTTPException): await paste(alice(), loc)
        await A.q("UPDATE oauth_providers SET enabled=true WHERE id=%s", (p["id"],))
        _, loc, st = await begin(alice(), "googlep")
        await as_(adm(), O.prov_del, p["id"])
        with pytest.raises(HTTPException): await paste(alice(), loc)
        assert len(posts(f, path)) == n + 1
    run(t)

def test_provider_error_is_not_reflected_as_html_and_denial_connects_nothing(fk):
    f = fk("dcr")
    async def t():
        p = await mk(f, "main")
        _, _, st = await begin(alice(), "mainp")
        r = await as_(alice(), O.callback, req(), st, "", "<script>alert(1)</script>", "<img src=x onerror=1>", "")
        assert r.status_code == 400 and b"<script>" not in r.body and b"&lt;script&gt;" in r.body and b"<img" not in r.body
        _, _, st = await begin(alice(), "mainp"); r = await as_(alice(), O.callback, req(), st, "", "access_denied", "", "")
        assert r.status_code == 400 and not await conn_row(p["id"]) and not posts(f, "/token")
        _, _, st = await begin(alice(), "mainp"); r = await as_(alice(), O.callback, req(), st, "", "", "", "")   # no code
        assert r.status_code == 400
        with pytest.raises(HTTPException): await paste(alice(), f"{BASE_URL}/oauth/callback?error=%3Cscript%3E&state=zzz")   # JSON API: plain text detail, never HTML
    run(t)

# ================================================================== workspaces, roles, owners, demo
def test_other_workspaces_provider_is_invisible_and_untouchable(fk):
    f = fk("dcr")
    async def t():
        pb = await mk(f, "main", name="onlyb", ws=WB)
        assert [x["name"] for x in await as_(adm(), O.prov_list)] == [] and [x["name"] for x in await as_(adm(WB), O.prov_list)] == ["onlyb"]
        with pytest.raises(HTTPException) as e: await as_(alice(), O.connect, "onlyb", req(), None)
        assert e.value.status_code == 404
        body = O.ProviderIn(name="onlyb", authorize_url="https://a/", token_url=f.base + "/token", api_base=f.base + "/api/", client_id="evil")
        for fn, args in ((O.prov_upd, (pb["id"], body)), (O.prov_del, (pb["id"],))):
            with pytest.raises(HTTPException) as e: await as_(adm(), fn, *args)
            assert e.value.status_code == 404
        with pytest.raises(HTTPException) as e: await as_(adm(), O.prov_reregister, pb["id"], req())
        assert e.value.status_code == 404
        with pytest.raises(HTTPException) as e: await as_(alice(), C.get_token, ("user", SEEDS["alice"]), "onlyb", ws=WA)
        assert e.value.status_code == 404
        assert (await A.q("SELECT client_id FROM oauth_providers WHERE id=%s", (pb["id"],), one=True))["client_id"] == MANUAL_CLIENT[0]   # untouched
        await mk(f, "main", name="onlyb", ws=WA)   # the same name in each workspace is fine and separate
        assert len(await A.q("SELECT 1 FROM oauth_providers WHERE name='onlyb' AND workspace_id IN (%s,%s)", (WA, WB))) == 2
    run(t)

def test_rls_hides_other_workspaces_providers_even_without_a_where(fk):
    f = fk("dcr")
    async def t():
        await mk(f, "main", name="rls-a"); await mk(f, "main", name="rls-b", ws=WB)
        names = lambda rows: sorted(r["name"] for r in rows)   # noqa: E731
        assert names(await as_(alice(), A.q, "SELECT name FROM oauth_providers WHERE name LIKE 'rls-%%'")) == ["rls-a"]
        assert names(await as_(alice(WB), A.q, "SELECT name FROM oauth_providers WHERE name LIKE 'rls-%%'")) == ["rls-b"]
        with pytest.raises(Exception): await as_(alice(), A.q, "INSERT INTO oauth_providers(name,authorize_url,token_url,api_base,client_id,workspace_id) VALUES('evil','a','t','b','c',%s)", (WB,))
    run(t)

def test_roles_owners_non_admin_cannot_connect_agent_or_workspace_accounts(fk):
    f = fk("dcr")
    async def t():
        p = await mk(f, "google")
        for fn, args in ((O.prov_new, (O.ProviderIn(name="mine", authorize_url="https://a/", token_url="https://a/t", api_base="https://a/", client_id="c"),)),
                         (O.prov_upd, (p["id"], O.ProviderIn(name="googlep", authorize_url="https://a/", token_url="https://a/t", api_base="https://a/", client_id="c"))),
                         (O.prov_del, (p["id"],)), (O.prov_reregister, (p["id"], req())), (O.prov_from_catalog, (O.CatalogIn(catalog_id="github", client_id="c"),))):
            with pytest.raises(HTTPException) as e: await as_(alice(), fn, *args)
            assert e.value.status_code == 403
        for owner in ("workspace", f"agent:{SEEDS['ag_a']}"):
            with pytest.raises(HTTPException) as e: await as_(alice(), O.connect, "googlep", req(), O.ConnectIn(owner=owner))
            assert e.value.status_code == 403
        with pytest.raises(HTTPException) as e: await as_(adm(), O.connect, "googlep", req(), O.ConnectIn(owner=f"agent:{SEEDS['ag_b']}"))   # an agent of another workspace
        assert e.value.status_code == 404
        with pytest.raises(HTTPException) as e: await as_(adm(), O.connect, "googlep", req(), O.ConnectIn(owner="agent:abc"))
        assert e.value.status_code == 422
        # an admin connects the AGENT's account: stored under the agent, sealed as the agent (a user's token is not the agent's)
        _, loc, st = await begin(adm(), "googlep", body=O.ConnectIn(owner=f"agent:{SEEDS['ag_a']}"))
        assert (await A.q("SELECT owner_type, owner_id, workspace_id FROM oauth_states WHERE state=%s", (st,), one=True)) == {"owner_type": "agent", "owner_id": SEEDS["ag_a"], "workspace_id": WA}
        await paste(adm(), loc)
        assert await conn_row(p["id"], ("agent", SEEDS["ag_a"])) and not await conn_row(p["id"], ("user", SEEDS["admin_a"]))
        assert (await token_of(adm(), ("agent", SEEDS["ag_a"]), "googlep")) in f.google.tokens
        with pytest.raises(HTTPException) as e: await token_of(adm(), ("user", SEEDS["admin_a"]), "googlep")
        assert e.value.status_code == 401
        # a state for an agent account that a NON-admin completes (role lost / state handed over): refused, nothing stored
        _, loc, st = await begin(adm(), "googlep", body=O.ConnectIn(owner="workspace"))
        await A.q("UPDATE oauth_states SET user_id=%s WHERE state=%s", (SEEDS["bob"], st)); n = len(posts(f, "/google/token"))
        with pytest.raises(HTTPException) as e: await paste(bob(), loc)
        assert e.value.status_code == 403 and len(posts(f, "/google/token")) == n and not await conn_row(p["id"], ("workspace", 0))
        # the agent left the workspace while the admin was signing in
        _, loc, st = await begin(adm(), "googlep", body=O.ConnectIn(owner=f"agent:{SEEDS['ag_a']}"))
        await A.q("UPDATE oauth_states SET owner_id=%s WHERE state=%s", (SEEDS["ag_b"], st))
        with pytest.raises(HTTPException) as e: await paste(adm(), loc)
        assert e.value.status_code == 404 and len(posts(f, "/google/token")) == n
    run(t)

def test_demo_and_channel_callers_cannot_connect(fk):
    f = fk("dcr")
    async def t():
        await mk(f, "google"); _, loc, st = await begin(alice(), "googlep")
        demo = U(WA, SEEDS["alice"], demo=True); chan = U(WA, SEEDS["alice"], auth="channel")
        for who in (demo, chan):
            with pytest.raises(HTTPException) as e: await as_(who, O.connect, "googlep", req(), None)
            assert e.value.status_code == 403
        with pytest.raises(HTTPException) as e: await paste(demo, loc)   # paste: the demo check comes before anything is consumed or called
        assert e.value.status_code == 403 and not posts(f, "/google/token")
        with pytest.raises(HTTPException) as e: await as_({}, O.connect, "googlep", req(), None)
        assert e.value.status_code == 401
    run(t)

# ================================================================== network policy (netguard purpose oauth)
def test_netguard_has_the_oauth_purpose_and_behaves_like_webhook(monkeypatch):
    assert "oauth" in netguard.PURPOSES
    monkeypatch.setenv("AOS_EGRESS_ALLOW", "127.0.0.0/8")
    assert netguard.check("http://127.0.0.1:9/t", "oauth")[0] == "127.0.0.1"   # allow-listed by the operator
    for url in ("http://169.254.169.254/latest/meta-data", "http://0.0.0.0/t", "http://[fe80::1]/t"):   # never, even allow-listed
        with pytest.raises(netguard.NetBlocked): netguard.check(url, "oauth")
    with pytest.raises(netguard.NetBlocked): netguard.check("http://8.8.8.8/token", "oauth")   # plain http to a public address
    monkeypatch.setenv("AOS_EGRESS_ALLOW", "")
    for url in ("http://127.0.0.1:9/t", "https://10.1.2.3/t", "https://192.168.0.5/t", "https://[::1]/t"):
        with pytest.raises(netguard.NetBlocked): netguard.check(url, "oauth")

def test_private_and_metadata_token_endpoints_are_refused_when_saved_and_on_every_call(fk, monkeypatch):
    f = fk("dcr")
    async def t():
        for bad in ("http://169.254.169.254/token", "http://0.0.0.0/token", "http://[fe80::1]/token", "http://user:pw@127.0.0.1/token"):   # never reachable / credentials in the URL
            with pytest.raises(HTTPException) as e: await mk(f, "main", name="priv", token_url=bad)
            assert e.value.status_code == 422
        monkeypatch.setenv("AOS_EGRESS_ALLOW", "")
        for bad in ("http://127.0.0.1:9/token", "https://10.1.2.3/token", "https://192.168.1.1/token", "https://[::1]/token"):   # internal and not allow-listed
            with pytest.raises(HTTPException) as e: await mk(f, "main", name="priv", token_url=bad)
            assert e.value.status_code == 422 and "token_url" in e.value.detail
        monkeypatch.setenv("AOS_EGRESS_ALLOW", "127.0.0.0/8")
        for field in ("userinfo_url", "revoke_url", "api_base"):   # every URL the server will call gets the same check, not only the token endpoint
            with pytest.raises(HTTPException) as e: await mk(f, "main", name="priv", **{field: "http://169.254.169.254/x"})
            assert e.value.status_code == 422 and field in e.value.detail
        assert not await A.q("SELECT 1 FROM oauth_providers WHERE name='priv'", one=True)
        p = await mk(f, "main", name="lateblock", userinfo_url=f.base + "/api/userinfo")   # a host name (or an operator change) is only caught at call time
        monkeypatch.setenv("AOS_EGRESS_ALLOW", "")
        n = len(f.calls)
        with pytest.raises(HTTPException) as e: await O._token_call(await A.q("SELECT * FROM oauth_providers WHERE id=%s", (p["id"],), one=True), {"grant_type": "authorization_code", "code": "c"})
        assert e.value.status_code == 502 and e.value.code == "egress_blocked" and len(f.calls) == n
        _, loc, _ = await begin(alice(), "lateblock"); n = len(f.calls)
        r = await finish_cb(alice(), loc); assert r.status_code == 400 and b"network policy" in r.body and len(f.calls) == n and not await conn_row(p["id"])   # code never leaves
        monkeypatch.setenv("AOS_EGRESS_ALLOW", "127.0.0.0/8")
        _, loc, _ = await begin(alice(), "lateblock"); await finish_cb(alice(), loc)   # connected while allowed
        monkeypatch.setenv("AOS_EGRESS_ALLOW", ""); n = len(f.calls)
        row = await conn_row(p["id"]); prow = await A.q("SELECT * FROM oauth_providers WHERE id=%s", (p["id"],), one=True)
        assert await O.revoke_remote(row, prow) is False and len(f.calls) == n   # revocation is best effort and also policed
        monkeypatch.setenv("AOS_EGRESS_ALLOW", "127.0.0.0/8")
        monkeypatch.setenv("AOS_EGRESS_ALLOW", ""); n = len(f.calls)
        out = await as_(alice(), O.tool_request, "lateblock", "GET", f.base + "/api/userinfo"); assert out.startswith("Error") and len(f.calls) == n   # tool calls carry the token: policed too
        monkeypatch.setenv("AOS_EGRESS_ALLOW", "127.0.0.0/8")
        out = await as_(alice(), O.tool_request, "lateblock", "GET", f.base + "/api/userinfo"); assert out.startswith("HTTP ")
    run(t)

# ================================================================== public base: X-Forwarded-Host is not trusted
def test_public_base_ignores_forged_forwarded_headers(monkeypatch):
    forged = {"x-forwarded-host": "evil.example", "x-forwarded-proto": "https", "cf-visitor": '{"scheme":"https"}'}
    monkeypatch.delenv("AOS_PUBLIC_BASE")
    assert O._public_base(req(forged, client="203.0.113.9")) == "http://aos.local"   # no env, not a trusted proxy: what the browser typed (Host), never the forwarded one
    assert O._public_base(req(forged)) == "http://aos.local"
    monkeypatch.setenv("AOS_PUBLIC_BASE", "https://pinned.example/")
    assert O._public_base(req(forged, client="203.0.113.9")) == "https://pinned.example"   # the operator's statement always wins
    monkeypatch.setenv("AOS_TRUSTED_PROXIES", "203.0.113.0/24"); assert O._public_base(req(forged, client="203.0.113.9")) == "https://pinned.example"
    monkeypatch.delenv("AOS_PUBLIC_BASE")
    assert O._public_base(req(forged, client="203.0.113.9")) == "https://evil.example"   # ONLY a proxy the operator listed is believed
    assert O._public_base(req(forged, client="198.51.100.7")) == "http://aos.local" and O._public_base(req(forged)) == "http://aos.local"
    for host in ("evil.example/x", "a b", "evil.example@x", "", "a.b:99999999", "[<script>]"):
        assert O._public_base(req({"x-forwarded-host": host}, client="203.0.113.9")) == "http://aos.local", host
    assert O._public_base(req({"x-forwarded-host": "h.example:8443", "x-forwarded-proto": "javascript"}, client="203.0.113.9")) == "http://aos.local"
    assert O._public_base(req({"x-forwarded-host": "h.example:8443"}, client="203.0.113.9")) == "https://h.example:8443"
    for strict_req in (req(forged, client="198.51.100.7"), req()):
        with pytest.raises(HTTPException) as e: O._public_base(strict_req, strict=True)
        assert e.value.status_code == 409
    for bad in ("aos.example.test", "https://a.test/some/path", "ftp://a.test", "https://u:p@a.test", "https://a.test?x=1"):
        monkeypatch.setenv("AOS_PUBLIC_BASE", bad)
        with pytest.raises(HTTPException) as e: O._public_base(req())
        assert e.value.status_code == 500

def test_forged_forwarded_host_does_not_change_the_redirect_uri_that_is_sent(fk, monkeypatch):
    f = fk("dcr")
    async def t():
        await mk(f, "main"); forged = {"x-forwarded-host": "evil.example", "x-forwarded-proto": "https"}
        good = await as_(alice(), O.connect, "mainp", req(), None)
        evil = await as_(alice(), O.connect, "mainp", req(forged, client="203.0.113.9"), None)
        assert good["redirect_uri"] == evil["redirect_uri"] == BASE_URL + "/oauth/callback" and "evil" not in evil["authorize_url"]
        monkeypatch.delenv("AOS_PUBLIC_BASE")   # even with no env at all
        evil = await as_(alice(), O.connect, "mainp", req(forged, client="203.0.113.9"), None); assert "evil.example" not in evil["authorize_url"] and evil["redirect_uri"] == "http://aos.local/oauth/callback"
        st = await A.q("SELECT redirect_uri FROM oauth_states WHERE state=%s", (parse_qs(urlparse(evil["authorize_url"]).query)["state"][0],), one=True)
        assert st["redirect_uri"] == "http://aos.local/oauth/callback"   # and the token request will use exactly this
    run(t)

# ================================================================== own redirect address, tunnel mode and the re-register button
def test_default_redirect_uri_validation_and_use(fk):
    f = fk("dcr")
    async def t():
        for bad in ("http://evil.example/cb", "http://localhost:8000/cb", "http://127.0.0.2/cb", "javascript:alert(1)", "https://u:p@a.example/cb", "https://a.example/cb#frag", "ftp://a.example/cb", "/relative"):
            with pytest.raises(HTTPException) as e: await mk(f, "main", name="dru", default_redirect_uri=bad)
            assert e.value.status_code == 422, bad
        for ok in ("https://client.example/cb", "http://127.0.0.1:8123/cb", "http://[::1]:8123/cb"):
            await as_(adm(), O.prov_del, (await mk(f, "main", name="dru", default_redirect_uri=ok))["id"])
        await mk(f, "main", name="dru", default_redirect_uri="http://127.0.0.1:8123/cb")
        c, loc, st = await begin(alice(), "dru")
        assert c["redirect_uri"] == "http://127.0.0.1:8123/cb" and c["paste"] and loc.startswith("http://127.0.0.1:8123/cb?")   # wins over the mode and over AOS_PUBLIC_BASE
        r = await paste(alice(), loc); assert r["ok"]
        _, loc, st = await begin(alice(), "dru"); code = parse_qs(urlparse(loc).query)["code"][0]
        with pytest.raises(HTTPException): await paste(alice(), f"{LOOP}?code={code}&state={st}")   # right state, wrong address
    run(t)

def test_tunnel_provider_needs_a_configured_https_base_and_marks_reconnect_when_it_changes(fk, monkeypatch):
    f = fk("dcr")
    async def t():
        p = await mk(f, "main", name="tun", redirect_mode="tunnel")
        monkeypatch.delenv("AOS_PUBLIC_BASE")
        with pytest.raises(HTTPException) as e: await as_(alice(), O.connect, "tun", req({"x-forwarded-host": "evil.example"}, client="203.0.113.9"), None)
        assert e.value.status_code == 409   # a forged header cannot stand in for the operator's address
        monkeypatch.setenv("AOS_PUBLIC_BASE", "http://plain.example")
        with pytest.raises(HTTPException) as e: await as_(alice(), O.connect, "tun", req(), None)
        assert e.value.status_code == 409   # http is not good enough for a registered redirect
        monkeypatch.setenv("AOS_PUBLIC_BASE", "https://a.example")
        c, loc, _ = await begin(alice(), "tun"); assert c["redirect_uri"] == "https://a.example/oauth/callback" and not c["paste"]
        assert (await A.q("SELECT redirect_base FROM oauth_providers WHERE id=%s", (p["id"],), one=True))["redirect_base"] == "https://a.example"
        await finish_cb(alice(), loc); assert (await conn_row(p["id"]))["status"] == "active"
        monkeypatch.setenv("AOS_PUBLIC_BASE", "https://b.example")   # the tunnel got a new address
        with pytest.raises(HTTPException) as e: await as_(alice(), O.connect, "tun", req(), None)
        assert e.value.status_code == 409 and "redirect_changed" in e.value.detail
        row = await conn_row(p["id"]); assert (row["status"], row["status_reason"]) == ("needs_reconnect", "redirect_changed")
        with pytest.raises(HTTPException) as e: await as_(alice(), O.prov_reregister, p["id"], req())
        assert e.value.status_code == 403
        r = await as_(adm(), O.prov_reregister, p["id"], req()); assert r["redirect_uri"] == "https://b.example/oauth/callback" and r["registration"] == "manual"
        c, loc, _ = await begin(alice(), "tun"); assert c["redirect_uri"] == "https://b.example/oauth/callback"
        await finish_cb(alice(), loc); assert (await conn_row(p["id"]))["status"] == "active"   # one reconnect and it is active again
        # at boot: AOS_PUBLIC_BASE differs from what the provider was registered for -> every connection of tunnel providers flips, nothing is re-registered
        monkeypatch.setenv("AOS_PUBLIC_BASE", "https://c.example"); await O.redirect_sweep()
        row = await conn_row(p["id"]); assert (row["status"], row["status_reason"]) == ("needs_reconnect", "redirect_changed")
        assert (await A.q("SELECT redirect_base FROM oauth_providers WHERE id=%s", (p["id"],), one=True))["redirect_base"] == "https://b.example"
        w = await mk(f, "main", name="webp")   # providers in other modes are not touched
        _, loc, _ = await begin(alice(), "webp"); await finish_cb(alice(), loc); await O.redirect_sweep()
        assert (await conn_row(w["id"]))["status"] == "active"
        with pytest.raises(HTTPException) as e: await as_(adm(), O.prov_reregister, w["id"], req())
        assert e.value.status_code == 409
    run(t)

# ================================================================== catalog
def test_catalog_file_is_well_formed_and_has_the_named_services():
    cat = O.catalog(); ids = [e["id"] for e in cat]
    assert len(cat) >= 25 and len(set(ids)) == len(ids)
    for need in ("atlassian", "github", "gitlab", "linear", "notion", "slack", "zoom", "hubspot", "asana", "dropbox", "figma", "airtable", "google", "microsoft"): assert need in ids
    for e in cat:
        assert re.fullmatch(r"[a-z0-9][a-z0-9_\-]{1,39}", e["id"]) and e["display_name"]
        for k in ("authorize_url", "token_url", "api_base"): assert e[k].startswith("https://"), (e["id"], k)
        for k in ("revoke_url", "userinfo_url"): assert not e.get(k) or e[k].startswith("https://"), (e["id"], k)
        assert e["token_auth"] in ("body", "post", "basic", "none") and e["redirect_mode"] in ("web", "loopback_paste", "tunnel")
        O.ProviderIn(name=e["id"], client_id="c", **{k: e[k] for k in ("display_name", "authorize_url", "token_url", "revoke_url", "userinfo_url", "api_base", "scopes", "token_auth", "extra_params", "redirect_mode")})
        for k in ("token_url", "revoke_url", "userinfo_url", "api_base"):
            if e.get(k): O._server_url(e[k], k)   # the same save-time URL checks a hand-made provider gets
    assert next(e for e in cat if e["id"] == "google")["redirect_mode"] == "loopback_paste" and next(e for e in cat if e["id"] == "microsoft")["redirect_mode"] == "loopback_paste"
    paths = [r.path for r in C.router.routes]; assert paths.index("/api/connections/catalog") < paths.index("/api/connections/{slug}")   # not shadowed by the slug route

def test_create_from_catalog_needs_only_client_id_and_secret():
    async def t():
        listing = await as_(alice(), C.conn_catalog); assert len(listing) >= 25 and not any(e["created"] for e in listing)
        p = await as_(adm(), O.prov_from_catalog, O.CatalogIn(catalog_id="github", client_id="gh-id", client_secret="gh-secret"))
        assert p["name"] == "github" and p["source"] == "catalog" and p["has_secret"] and p["token_url"] == "https://github.com/login/oauth/access_token" and p["redirect_mode"] == "web"
        g = await as_(adm(), O.prov_from_catalog, O.CatalogIn(catalog_id="google", client_id="g-id", client_secret="g-secret", name="google-work", scopes="openid email"))
        assert g["name"] == "google-work" and g["redirect_mode"] == "loopback_paste" and g["scopes"] == "openid email" and g["extra_params"] == {"access_type": "offline", "prompt": "consent"}
        assert "gh-secret" not in json.dumps(p, default=str)
        raw = await A.q("SELECT client_secret_enc FROM oauth_providers WHERE id=%s", (p["id"],), one=True); assert "gh-secret" not in raw["client_secret_enc"]
        assert next(e for e in await as_(alice(), C.conn_catalog) if e["id"] == "github")["created"] is True
        assert not next(e for e in await as_(alice(WB), C.conn_catalog) if e["id"] == "github")["created"]   # per workspace
        for args, code in ((O.CatalogIn(catalog_id="github", client_id="x"), 409), (O.CatalogIn(catalog_id="nope", client_id="x"), 404)):
            with pytest.raises(HTTPException) as e: await as_(adm(), O.prov_from_catalog, args)
            assert e.value.status_code == code
        with pytest.raises(HTTPException) as e: await as_(U(WA, SEEDS["alice"], demo=True), C.conn_catalog)
        assert e.value.status_code == 403
        await as_(adm(WB), O.prov_from_catalog, O.CatalogIn(catalog_id="github", client_id="b-id", client_secret="b-secret"))   # the same service in B is a separate row
        assert len(await A.q("SELECT 1 FROM oauth_providers WHERE name='github' AND workspace_id IN (%s,%s)", (WA, WB))) == 2
    run(t)

# ================================================================== secrets stay out of audit / API / logs
def test_no_secret_reaches_the_audit_log_the_api_or_the_logs(fk, caplog):
    f = fk("dcr")
    async def t():
        p = await mk(f, "ms", token_headers={"X-Tenant-Key": "HDR-SECRET-7"})
        _, loc, st = await begin(alice(), "msp"); code = parse_qs(urlparse(loc).query)["code"][0]; await paste(alice(), loc)
        row = await conn_row(p["id"]); at, rt = O._tok(row, "access_token_enc"), O._tok(row, "refresh_token_enc")
        await expire(p["id"]); await token_of(alice(), ("user", SEEDS["alice"]), "msp"); f.ms.refresh.clear(); await expire(p["id"])
        with pytest.raises(HTTPException): await token_of(alice(), ("user", SEEDS["alice"]), "msp")
        dump = json.dumps(await A.q("SELECT * FROM oauth_audit WHERE workspace_id=%s", (WA,)), default=str) + json.dumps(await as_(adm(), O.prov_list), default=str)
        dump += json.dumps(await as_(alice(), C.conn_list, req(), "me"), default=str) + caplog.text
        for s in (at, rt, "ms-secret", "HDR-SECRET-7", code, st): assert s not in dump, s
        assert (await A.q("SELECT count(*) n FROM oauth_audit WHERE workspace_id=%s AND action IN ('connect.start','connect.success','token.refresh')", (WA,), one=True))["n"] >= 4
    run(t)

# ================================================================== migration 0022
def test_migration_0022_up_down_twice_checks_and_legacy_rows(fk):
    m21 = next(m for v, m, _ in migrations._load() if v == "0021"); mod = next(m for v, m, _ in migrations._load() if v == "0022"); sch = "t1125_" + uuid.uuid4().hex[:8]
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as root: await root.execute(f'CREATE SCHEMA "{sch}"')
        try:
            async with await psycopg.AsyncConnection.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
                q = migrations._q(c)
                for s in m21._BASE: await q(s)    # the shape before 0022, with a legacy provider and an in-flight state
                await q("INSERT INTO oauth_providers(id,name,authorize_url,token_url,api_base,client_id,workspace_id,token_auth) VALUES(1,'crm','a','t','b','c',7,'basic')")
                await q("CREATE TABLE oauth_states(state TEXT PRIMARY KEY, provider_id INT, user_id INT, verifier_enc TEXT, next TEXT, created TIMESTAMPTZ NOT NULL DEFAULT now(), expires TIMESTAMPTZ NOT NULL)")
                await q("INSERT INTO oauth_states(state,provider_id,user_id,expires) VALUES('s1',1,41,now()+interval '5 minutes')")
                assert await mod.plan(q)
                await mod.up(q); await mod.up(q)   # idempotent
                r = (await q("SELECT grant_type,redirect_mode,source,iss_supported,token_auth,token_params,issuer,allowed_scopes,default_redirect_uri FROM oauth_providers"))[0]
                assert r == ("authorization_code", "web", "manual", False, "basic", {}, None, None, None)   # old behaviour, untouched
                assert (await q("SELECT owner_type,owner_id,workspace_id FROM oauth_states"))[0] == ("user", 41, 7)
                for bad in ("UPDATE oauth_providers SET grant_type='password'", "UPDATE oauth_providers SET redirect_mode='cloud'", "UPDATE oauth_providers SET source='x'",
                            "UPDATE oauth_providers SET token_auth='private_key_jwt'", "UPDATE oauth_states SET owner_type='robot'"):
                    with pytest.raises(psycopg.errors.CheckViolation): await c.execute(bad)
                for ok in ("none", "post", "basic", "body"): await c.execute("UPDATE oauth_providers SET token_auth=%s", (ok,))
                await c.execute("UPDATE oauth_providers SET grant_type='client_credentials', token_auth='none'")
                await mod.down(q)
                assert not await q("SELECT 1 FROM oauth_providers")   # a client_credentials provider cannot exist in the old shape
                assert not await q("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name IN ('oauth_providers','oauth_states') AND column_name IN ('grant_type','redirect_mode','owner_type','token_headers_enc')", (sch,))
                await mod.up(q)
        finally:
            async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as root: await root.execute(f'DROP SCHEMA "{sch}" CASCADE')
    asyncio.run(go())

def test_migration_0022_on_a_fresh_database_creates_the_states_table():
    m21 = next(m for v, m, _ in migrations._load() if v == "0021"); mod = next(m for v, m, _ in migrations._load() if v == "0022"); sch = "t1125f_" + uuid.uuid4().hex[:8]
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as root: await root.execute(f'CREATE SCHEMA "{sch}"')
        try:
            async with await psycopg.AsyncConnection.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
                q = migrations._q(c)
                for s in m21._BASE: await q(s)
                await mod.up(q)   # oauth_tools.init has not run yet on a first boot
                assert (await q("SELECT count(*) FROM oauth_states"))[0][0] == 0
        finally:
            async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as root: await root.execute(f'DROP SCHEMA "{sch}" CASCADE')
    asyncio.run(go())

def test_token_headers_column_is_in_the_crypto_rotation_targets():
    import aos_crypto
    assert any(t["name"] == "oauth_providers.token_headers_enc" and t["key"] == ("id",) and t["ws"] == "workspace_id" for t in aos_crypto.TEXT_TARGETS)

# ================================================================== through the real HTTP stack (own server, free port)
def _free_port():   # parallel agents run their own servers: never a hard-coded port
    import socket
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0)); return sk.getsockname()[1]
PORT = _free_port()
HB = f"http://127.0.0.1:{PORT}"
ADMIN_AUTH, VIEWER_AUTH = ("moza", "phase0-local"), ("viewer1", "phase0-viewer-local")

@pytest.fixture(scope="module")
def server():
    log = open(os.path.join(APP, "var", f"server_{PORT}.log"), "ab")
    env = {**os.environ, "PORT": str(PORT), "AOS_EGRESS_ALLOW": os.environ.get("AOS_EGRESS_ALLOW") or "127.0.0.0/8,::1", "AOS_PUBLIC_BASE": BASE_URL}
    env.pop("AOS_TRUSTED_PROXIES", None)
    pr = subprocess.Popen([sys.executable, os.path.join(APP, "overlay", "run_local.py")], cwd=APP, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        for _ in range(180):
            try:
                if httpx.get(HB + "/health", timeout=2).status_code < 500: break
            except httpx.HTTPError: pass
            time.sleep(1)
        else: raise RuntimeError(f"test server on :{PORT} did not start (see var/server_{PORT}.log)")
        yield HB
    finally:
        try: os.killpg(pr.pid, signal.SIGTERM)
        except ProcessLookupError: pass
        pr.wait(20); log.close()

def test_http_forged_forwarded_host_roles_catalog_and_xss(server, fk):
    f = fk("dcr")
    a = httpx.Client(base_url=server, auth=ADMIN_AUTH, timeout=60); v = httpx.Client(base_url=server, auth=VIEWER_AUTH, timeout=60); anon = httpx.Client(base_url=server, timeout=60)
    name = "t1125http" + TAG
    body = dict(name=name, authorize_url=f.base + "/authorize", token_url=f.base + "/token", api_base=f.base + "/api/", client_id=MANUAL_CLIENT[0], client_secret=MANUAL_CLIENT[1], scopes="read")
    pid = None
    try:
        r = a.post("/api/oauth/providers", json=body); assert r.status_code == 200, r.text; pid = r.json()["id"]
        assert "client_secret" not in r.text and r.json()["grant_type"] == "authorization_code" and r.json()["has_secret"]
        forged = {"X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "https", "CF-Visitor": '{"scheme":"https"}'}
        c1 = a.post(f"/api/oauth/connect/{name}"); c2 = a.post(f"/api/oauth/connect/{name}", headers=forged)
        assert c1.status_code == c2.status_code == 200 and c1.json()["redirect_uri"] == c2.json()["redirect_uri"] == BASE_URL + "/oauth/callback" and "evil.example" not in c2.text
        r = a.post("/api/oauth/connect/nope" + TAG); assert r.status_code == 404
        # unsigned / viewer / private-IP provider over HTTP
        assert anon.post(f"/api/oauth/connect/{name}").status_code == 401 and anon.post("/api/oauth/paste-callback", json={"url": "http://127.0.0.1:8765/oauth/callback?state=x"}).status_code == 401
        assert v.post("/api/oauth/paste-callback", json={"url": "http://127.0.0.1:8765/oauth/callback?state=x"}).status_code == 403
        assert v.post(f"/api/oauth/connect/{name}").status_code == 403 and v.post("/api/oauth/providers", json=body).status_code == 403
        assert v.post("/api/oauth/providers/from-catalog", json={"catalog_id": "github", "client_id": "x"}).status_code == 403
        assert v.post(f"/api/oauth/providers/{pid}/re-register").status_code == 403
        r = a.post("/api/oauth/providers", json={**body, "name": name + "x", "token_url": "http://169.254.169.254/latest/api/token"}); assert r.status_code == 422 and "refused" in r.text
        r = a.post("/api/oauth/providers", json={**body, "name": name + "y", "default_redirect_uri": "http://evil.example/cb"}); assert r.status_code == 422
        r = a.post("/api/oauth/providers", json={**body, "name": name + "z", "extra_params": {"redirect_uri": "https://evil.example"}}); assert r.status_code == 422
        # forged state through the real callback route, and the error text is not reflected as HTML
        r = a.get("/oauth/callback", params={"state": "x" * 43, "code": "c"}); assert r.status_code == 400
        r = a.get("/oauth/callback", params={"state": "x" * 43, "error": "<script>alert(1)</script>"}); assert "<script>" not in r.text
        r = a.post("/api/oauth/paste-callback", json={"url": "http://127.0.0.1:8765/oauth/callback?code=c&state=" + "y" * 43}); assert r.status_code == 400
        # a real round trip: state from connect, code from the fake, completed through the callback route; replay refused
        loc = authorize(c1.json()["authorize_url"]); r = a.get(loc.replace(BASE_URL, ""), follow_redirects=False); assert r.status_code == 303, r.text
        r2 = a.get(loc.replace(BASE_URL, ""), follow_redirects=False); assert r2.status_code == 400 and len(posts(f, "/token")) == 1
        cat = a.get("/api/connections/catalog"); assert cat.status_code == 200 and len(cat.json()) >= 25 and v.get("/api/connections/catalog").status_code == 200 and anon.get("/api/connections/catalog").status_code == 401
        assert a.get("/api/connections/github").status_code in (200, 404)   # the slug route still works next to /catalog
    finally:
        if pid: a.delete(f"/api/oauth/providers/{pid}")
        for k in (a, v, anon): k.close()
