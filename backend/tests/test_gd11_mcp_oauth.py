"""GĐ11 card 11.30: self-tests of the fake OAuth / MCP servers (tests/fakes/fake_mcp_oauth.py). 11.26 adds the probe / registration tests to this file."""
import asyncio, base64, hashlib, os, sys
from urllib.parse import parse_qs, urlparse

import httpx, pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from fakes.fake_mcp_oauth import MANUAL_CLIENT, fake_oauth_mcp  # noqa: E402,F401  (fixture)
from mcp.client.auth.utils import (build_oauth_authorization_server_metadata_discovery_urls, build_protected_resource_metadata_discovery_urls,  # noqa: E402
                                   extract_resource_metadata_from_www_auth, handle_auth_metadata_response, handle_protected_resource_response)

RU = "http://127.0.0.1:8765/cb"
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
ACCEPT = {"accept": "application/json, text/event-stream"}
def pkce(): v = "v" * 50; return v, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
def probe(f): return httpx.post(f.mcp_url, json=INIT, headers=ACCEPT)

def get_token(f, cid, secret=None, scope="read", ru=RU):
    v, ch = pkce()
    a = httpx.get(f.urls["authorize"], params={"response_type": "code", "client_id": cid, "redirect_uri": ru, "state": "st1", "scope": scope, "code_challenge": ch,
                                               "code_challenge_method": "S256", "resource": f.mcp_url}, follow_redirects=False)
    assert a.status_code == 302, a.text
    q = parse_qs(urlparse(a.headers["location"]).query)
    data = {"grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": ru, "client_id": cid, "code_verifier": v}
    if secret: data["client_secret"] = secret
    return q, httpx.post(f.urls["token"], data=data)

@pytest.mark.parametrize("mode", ["dcr_cimd", "dcr"])
def test_fake_discovery_with_metadata(fake_oauth_mcp, mode):
    f = fake_oauth_mcp(mode); r = probe(f)
    assert r.status_code == 401
    url = extract_resource_metadata_from_www_auth(r); assert url == f.prm_url
    cands = build_protected_resource_metadata_discovery_urls(url, f.mcp_url)
    assert cands[0] == f.prm_url and cands[1].endswith("/.well-known/oauth-protected-resource/mcp")
    prm = None
    for u in cands:   # the header URL and the .well-known fallbacks all answer
        prm = asyncio.run(handle_protected_resource_response(httpx.get(u))); assert prm is not None and str(prm.resource) == f.mcp_url
    assert [str(x).rstrip("/") for x in prm.authorization_servers] == [f.issuer]
    md = None
    for u in build_oauth_authorization_server_metadata_discovery_urls(f.issuer, f.mcp_url):
        _, md = asyncio.run(handle_auth_metadata_response(httpx.get(u)))
        if md: break
    assert md and str(md.issuer).rstrip("/") == f.issuer and md.registration_endpoint and "S256" in md.code_challenge_methods_supported
    raw = httpx.get(f.base + "/.well-known/oauth-authorization-server").json()
    assert raw.get("client_id_metadata_document_supported", False) is (mode == "dcr_cimd") and raw["authorization_response_iss_parameter_supported"] is True

def test_fake_no_metadata_github_like(fake_oauth_mcp):
    f = fake_oauth_mcp("none"); r = probe(f)
    assert r.status_code == 401 and extract_resource_metadata_from_www_auth(r) is None and r.headers["www-authenticate"] == "Bearer"
    for u in build_protected_resource_metadata_discovery_urls(None, f.mcp_url): assert httpx.get(u).status_code == 404
    for u in build_oauth_authorization_server_metadata_discovery_urls(None, f.mcp_url):
        _, md = asyncio.run(handle_auth_metadata_response(httpx.get(u))); assert md is None
    assert httpx.post(f.urls["register"], json={"redirect_uris": [RU]}).status_code == 404
    q, t = get_token(f, *MANUAL_CLIENT); assert t.status_code == 200 and "iss" not in q   # manual client id works, no `iss`

def test_fake_dcr_authorize_token_refresh_rotation_and_iss(fake_oauth_mcp):
    f = fake_oauth_mcp("dcr_cimd")
    reg = httpx.post(f.urls["register"], json={"redirect_uris": [RU], "token_endpoint_auth_method": "none", "client_name": "t"}); assert reg.status_code == 201
    cid = reg.json()["client_id"]
    q, t = get_token(f, cid); assert t.status_code == 200 and q["iss"] == [f.issuer] and q["state"] == ["st1"]
    tok = t.json(); assert tok["expires_in"] == 60 and tok["refresh_token"]
    bad = httpx.get(f.urls["authorize"], params={"response_type": "code", "client_id": cid, "redirect_uri": "http://127.0.0.1:1/other", "code_challenge": "x", "code_challenge_method": "S256"})
    assert bad.status_code == 400   # redirect_uri not registered
    assert httpx.get(f.urls["authorize"], params={"response_type": "code", "client_id": cid, "redirect_uri": RU}).status_code == 400   # PKCE S256 is mandatory
    r1 = httpx.post(f.urls["token"], data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": cid}); assert r1.status_code == 200
    r2 = httpx.post(f.urls["token"], data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": cid}); assert r2.status_code == 400   # rotated: the old one is dead
    assert r1.json()["refresh_token"] != tok["refresh_token"]
    f.wrong_iss = True; q2, _ = get_token(f, cid); assert q2["iss"] == ["https://evil.example"]
    f.wrong_iss = False
    _, t3 = get_token(f, "http://127.0.0.1:1/client.json"); assert t3.status_code == 200   # client-id metadata document accepted in mode a
    v, ch = pkce()
    loc = httpx.get(f.urls["authorize"], params={"response_type": "code", "client_id": cid, "redirect_uri": RU, "code_challenge": ch, "code_challenge_method": "S256"}).headers["location"]
    assert httpx.post(f.urls["token"], data={"grant_type": "authorization_code", "code": parse_qs(urlparse(loc).query)["code"][0], "redirect_uri": RU, "client_id": cid,
                                             "code_verifier": "wrong"}).status_code == 400   # PKCE check
    assert httpx.post(f.base + "/revoke", data={"token": r1.json()["access_token"]}).status_code == 200
    assert httpx.post(f.urls["token"], data={"grant_type": "client_credentials", "client_id": MANUAL_CLIENT[0], "client_secret": MANUAL_CLIENT[1], "scope": "read"}).status_code == 200

def test_fake_protected_mcp_identity_and_insufficient_scope(fake_oauth_mcp):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport
    f = fake_oauth_mcp("dcr")
    cid = httpx.post(f.urls["register"], json={"redirect_uris": [RU], "token_endpoint_auth_method": "none"}).json()["client_id"]
    f.sub = "alice"; _, ta = get_token(f, cid, scope="read"); f.sub = "bob"; _, tb = get_token(f, cid, scope="read write")
    async def call(tok, name):
        async with Client(StreamableHttpTransport(f.mcp_url, headers={"Authorization": "Bearer " + tok})) as c:
            return (await c.call_tool(name, {})).data
    a, b = ta.json()["access_token"], tb.json()["access_token"]
    assert asyncio.run(call(a, "whoami")) == "alice" and asyncio.run(call(b, "whoami")) == "bob"
    assert asyncio.run(call(a, "read_data")) == "data-for-alice" and asyncio.run(call(b, "write_data")) == "wrote-as-bob"
    body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "write_data", "arguments": {}}}
    r = httpx.post(f.mcp_url, json=body, headers={"authorization": "Bearer " + a, **ACCEPT})
    wa = r.headers["www-authenticate"]
    assert r.status_code == 403 and 'error="insufficient_scope"' in wa and 'scope="write"' in wa and extract_resource_metadata_from_www_auth(r) == f.prm_url
    f.token_ttl = 0; _, tz = get_token(f, cid)   # expires at once
    assert httpx.post(f.mcp_url, json=body, headers={"authorization": "Bearer " + tz.json()["access_token"], **ACCEPT}).status_code == 401

def test_fake_google_and_microsoft_loopback_pairs(fake_oauth_mcp):
    f = fake_oauth_mcp("none"); ru = "http://localhost:8099/"
    for pre, cid, sec in (("/google", "google-client-id", "google-secret"), ("/ms", "ms-client-id", "ms-secret")):
        v, ch = pkce()
        a = httpx.get(f.base + pre + "/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": ru, "state": "s", "scope": "openid email",
                                                            "access_type": "offline", "code_challenge": ch, "code_challenge_method": "S256"})
        assert a.status_code == 302 and a.headers["location"].startswith(ru); code = parse_qs(urlparse(a.headers["location"]).query)["code"][0]
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": ru, "client_id": cid, "client_secret": sec, "code_verifier": v}
        t = httpx.post(f.base + pre + "/token", data=data).json(); assert t["access_token"] and t["refresh_token"]
        me = httpx.get(f.base + pre + "/api/userinfo", headers={"authorization": "Bearer " + t["access_token"]}); assert me.status_code == 200 and me.json()["sub"] == "user-1"
        r = httpx.post(f.base + pre + "/token", data={"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": cid, "client_secret": sec}).json()
        assert r["access_token"] and (r["refresh_token"] == t["refresh_token"]) is (pre == "/google")   # Google keeps its refresh token, Microsoft rotates
        assert httpx.post(f.base + pre + "/token", data=data).status_code == 400   # the code is one-time


# ====================================================================================================================================================================
# GĐ11 card 11.26: MCP OAuth probe, PRM, registration (CIMD > DCR > manual). In-process against the 11.30 fakes (+ a tiny canned-document server for hostile / odd servers),
# handlers called with a faked current user (admin.USER) on a private pool under the RLS role; one test goes through the real HTTP stack on this file's OWN server, port 8023.
# Negative tests: wrong `iss`, wrong issuer in metadata, PRM about another server, no PKCE S256, private / metadata / http addresses at every hop, redirects, oversized answers,
# viewer / editor / demo / channel / anonymous callers, another workspace's provider / server, forged X-Forwarded-Host, half-created rows, secrets in API / audit / log.
# ====================================================================================================================================================================
import json, signal, socket, subprocess, threading, time, uuid  # noqa: E402

import psycopg, uvicorn  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
from psycopg_pool import AsyncConnectionPool  # noqa: E402
from starlette.requests import Request  # noqa: E402

os.environ.setdefault("GOOGLE_API_KEY", "x-not-used")
os.environ.setdefault("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
import deepagent_api as A  # noqa: E402,I001  (first app import: circular imports)
import admin, migrations, netguard, oauth_tools as O, connections as C, mcp_oauth as M, mcp_registry as R  # noqa: E402

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
TAG = uuid.uuid4().hex[:6]
WA, WB = 93031, 93032
SEEDS: dict = {}
BASE_URL = "https://aos.example.test"
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
            for t in ("connections", "oauth_connections", "oauth_providers", "mcp_servers", "tool_settings"):   # what admin.rls_init does at boot
                for i in range(5):
                    try:
                        await A.q(f"ALTER TABLE {t} ALTER COLUMN workspace_id SET DEFAULT coalesce(aos_ws(), 1)")
                        await admin._policy(t, "aos_ws() IS NULL OR workspace_id = aos_ws()"); break
                    except psycopg.errors.InternalError_:
                        if i == 4: raise
                        await asyncio.sleep(0.5)
            await seed()
            try: return await fn()
            finally: await cleanup()
    return asyncio.run(go())

async def seed():
    for w in (WA, WB): await A.q("INSERT INTO workspaces(id,name) VALUES(%s,%s) ON CONFLICT DO NOTHING", (w, f"t1126-{TAG}-{w}"))
    for k in ("admin_a", "alice", "admin_b"):
        SEEDS[k] = (await A.q("INSERT INTO users(username,pw_hash) VALUES(%s,'x') RETURNING id", (f"t1126-{TAG}-{k}",), one=True))["id"]

async def cleanup():
    uids = [SEEDS[k] for k in ("admin_a", "alice", "admin_b") if k in SEEDS]
    for t in ("oauth_connections", "connections", "oauth_providers", "oauth_audit", "mcp_servers", "tool_settings"):
        await A.q(f"DELETE FROM {t} WHERE workspace_id IN (%s,%s)", (WA, WB))
    await A.q("DELETE FROM oauth_states WHERE workspace_id IN (%s,%s) OR user_id = ANY(%s)", (WA, WB, uids))
    await A.q("DELETE FROM users WHERE username LIKE %s", (f"t1126-{TAG}-%",))
    await A.q("DELETE FROM crypto_deks WHERE workspace_id IN (%s,%s)", (WA, WB)); await A.q("DELETE FROM workspaces WHERE id IN (%s,%s)", (WA, WB))
    for sid in [i for i, e in R._LIVE.items() if e["ws"] in (WA, WB)]: R._LIVE.pop(sid, None)
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

def adm(ws=WA): return U(ws, SEEDS["admin_a" if ws == WA else "admin_b"], "admin")
def alice(): return U(WA, SEEDS["alice"])

@pytest.fixture
def fk(fake_oauth_mcp):
    def make(mode="dcr"):
        s = fake_oauth_mcp(mode); s.token_ttl = 3600; return s
    return make

@pytest.fixture(autouse=True)
def _env26(monkeypatch):
    monkeypatch.setenv("AOS_PUBLIC_BASE", BASE_URL)
    for k in ("AOS_TRUSTED_PROXIES", "AOS_LOOPBACK_PORT"): monkeypatch.delenv(k, raising=False)

class Mini:
    """A tiny HTTP server answering canned documents: routes = {"/path" | "POST /path": (status, headers, body) | callable(mini) -> that tuple}; body = dict / bytes. .hits = paths asked."""
    def __init__(self, routes):
        self.routes, self.hits = routes, []
        self._sock = socket.socket(); self._sock.bind(("127.0.0.1", 0)); self.port = self._sock.getsockname()[1]; self.base = f"http://127.0.0.1:{self.port}"
        async def app(scope, receive, send):
            if scope["type"] != "http": return
            self.hits.append(scope["path"])
            r = self.routes.get(f"{scope['method']} {scope['path']}") or self.routes.get(scope["path"]) or (404, {}, {"error": "not_found"})
            st, hd, body = r(self) if callable(r) else r
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            await send({"type": "http.response.start", "status": st, "headers": [(k.lower().encode(), v.encode()) for k, v in {"content-type": "application/json", **hd}.items()]})
            await send({"type": "http.response.body", "body": data})
        self._srv = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="off"))
        self._th = threading.Thread(target=lambda: asyncio.run(self._srv.serve([self._sock])), daemon=True)
    def __enter__(self):
        self._th.start()
        for _ in range(300):
            if self._srv.started: return self
            time.sleep(0.02)
        raise RuntimeError("mini server did not start")
    def __exit__(self, *a): self._srv.should_exit = True; self._th.join(5)

def good_as(m, **over):
    return {"issuer": m.base, "authorization_endpoint": m.base + "/authorize", "token_endpoint": m.base + "/token", "registration_endpoint": m.base + "/register",
            "response_types_supported": ["code"], "code_challenge_methods_supported": ["S256"], **over}

def hostile(prm=None, asm=None, mcp=None):
    """A server that answers 401 + a PRM header; prm / asm are callables(mini) -> the document."""
    return {"/mcp": mcp or (lambda m: (401, {"WWW-Authenticate": f'Bearer resource_metadata="{m.base}/prm"'}, {"error": "invalid_token"})),
            "/prm": lambda m: (200, {}, prm(m) if prm else {"resource": m.base + "/mcp", "authorization_servers": [m.base]}),
            "/.well-known/oauth-authorization-server": lambda m: (200, {}, asm(m) if asm else good_as(m))}

def probe_err(url):
    with pytest.raises(M.ProbeError) as e: asyncio.run(M.probe(url))
    return e.value

async def prov(name, ws=WA): return await A.q("SELECT * FROM oauth_providers WHERE workspace_id=%s AND name=%s", (ws, name), one=True)
async def counts(ws=WA): return {t: (await A.q(f"SELECT count(*) n FROM {t} WHERE workspace_id=%s", (ws,), one=True))["n"] for t in ("oauth_providers", "connections", "mcp_servers")}
async def register(f, name="srv", user=None, ws=WA, request=None, **kw): return await as_(user or adm(ws), M.api_register, M.RegisterIn(name=name, url=f.mcp_url, **kw), request or req())
async def refuse(coro, status):
    with pytest.raises(HTTPException) as e: await coro
    assert e.value.status_code == status, e.value.detail
    return e.value.detail

def authorize(url):
    r = httpx.get(url, follow_redirects=False); assert r.status_code == 302, r.text
    return r.headers["location"]

async def connect_cb(user, pname, request=None):
    """connect -> sign in at the fake -> callback; -> (the query the browser came back with, the callback response)."""
    c = await as_(user, O.connect, pname, request or req(), None)
    q = {k: v[0] for k, v in parse_qs(urlparse(authorize(c["authorize_url"])).query).items()}
    return q, await as_(user, O.callback, request or req(), q.get("state", ""), q.get("code", ""), q.get("error", ""), q.get("error_description", ""), q.get("iss", ""))

async def token_of(user, pname, ws=WA): return (await as_(user, C.get_token, ("user", user["id"]), pname, ws=ws)).value

# ================================================================== probe: the three kinds of server
def test_probe_cimd_when_the_public_address_is_a_stable_https(fk):
    f = fk("dcr_cimd"); r = asyncio.run(M.probe(f.mcp_url))
    assert r["reachable"] and r["needs_auth"] and r["status"] == 401 and not r["manual_required"]
    assert r["registration"] == "cimd" and r["cimd_supported"] and r["dcr_supported"] and r["public_ok"] and r["pkce_s256"] and r["iss_supported"]
    assert r["auth_server"] == f.issuer and r["resource"] == f.mcp_url and r["scopes"] == ["read", "write"]
    paths = [c["path"] for c in f.calls]
    assert paths[0] == "/mcp" and paths[1] == "/.well-known/oauth-protected-resource/mcp"   # the URL in WWW-Authenticate first
    assert len(paths) <= 1 + M.MAX_HOPS

def test_probe_falls_back_to_dcr_when_there_is_no_stable_https(fk, monkeypatch):
    f = fk("dcr_cimd")
    for base in (None, "http://127.0.0.1:8023"):   # unset, or plain http: a client-id metadata document could not be fetched reliably
        monkeypatch.delenv("AOS_PUBLIC_BASE", raising=False)
        if base: monkeypatch.setenv("AOS_PUBLIC_BASE", base)
        r = asyncio.run(M.probe(f.mcp_url)); assert r["registration"] == "dcr" and r["cimd_supported"] and not r["public_ok"]

def test_probe_dcr_only_server(fk):
    r = asyncio.run(M.probe(fk("dcr").mcp_url))
    assert r["registration"] == "dcr" and not r["cimd_supported"] and r["dcr_supported"] and r["needs_auth"]

def test_probe_server_without_any_metadata_needs_a_manual_client(fk):
    f = fk("none"); r = asyncio.run(M.probe(f.mcp_url))
    assert r["needs_auth"] and r["registration"] == "none" and r["manual_required"] and r["auth_server"] is None and r["scopes"] == [] and not r["dcr_supported"]
    assert len(f.calls) <= 1 + M.MAX_HOPS

def test_probe_open_server_and_unreachable_server_are_not_needs_auth():
    with Mini({"/mcp": (200, {}, {"jsonrpc": "2.0", "id": 1, "result": {}})}) as m:
        r = asyncio.run(M.probe(m.base + "/mcp")); assert r["reachable"] and not r["needs_auth"] and r["registration"] == "none" and not r["manual_required"]
    r = asyncio.run(M.probe("http://127.0.0.1:1/mcp")); assert not r["reachable"] and not r["needs_auth"]

def test_probe_sse_only_server_answers_the_stream_request():
    with Mini({"POST /mcp": (405, {}, {}), "GET /mcp": (401, {"WWW-Authenticate": "Bearer"}, {})}) as m:
        r = asyncio.run(M.probe(m.base + "/mcp")); assert r["reachable"] and r["needs_auth"] and r["status"] == 401 and r["manual_required"]

def test_probe_without_a_header_url_the_well_known_urls_are_tried():
    with Mini({"/mcp": (401, {"WWW-Authenticate": "Bearer"}, {}), "/.well-known/oauth-protected-resource/mcp": lambda m: (200, {}, {"resource": m.base + "/mcp", "authorization_servers": [m.base]}),
               "/.well-known/oauth-authorization-server": lambda m: (200, {}, good_as(m))}) as m:
        r = asyncio.run(M.probe(m.base + "/mcp")); assert r["registration"] == "dcr" and r["auth_server"] == m.base

def test_probe_stops_after_max_hops(fk, monkeypatch):
    f = fk("dcr"); monkeypatch.setattr(M, "MAX_HOPS", 1)
    r = asyncio.run(M.probe(f.mcp_url))
    assert len([c for c in f.calls if c["path"] != "/mcp"]) == 1 and r["registration"] == "none"   # PRM read, authorization-server metadata never fetched

# ================================================================== probe: hostile / odd servers are refused
def test_probe_rejects_a_metadata_issuer_that_differs_from_the_advertised_one():
    with Mini(hostile(asm=lambda m: good_as(m, issuer="https://evil.example"))) as m:
        assert probe_err(m.base + "/mcp").code == "issuer_mismatch"

def test_probe_rejects_a_prm_that_is_about_another_server():
    with Mini(hostile(prm=lambda m: {"resource": "http://127.0.0.1:9/other", "authorization_servers": [m.base]})) as m:
        assert probe_err(m.base + "/mcp").code == "resource_mismatch"

@pytest.mark.parametrize("methods", [["plain"], [], None])
def test_probe_rejects_an_authorization_server_without_pkce_s256(methods):
    def asm(m):
        d = good_as(m)
        if methods is None: d.pop("code_challenge_methods_supported")
        else: d["code_challenge_methods_supported"] = methods
        return d
    with Mini(hostile(asm=asm)) as m: assert probe_err(m.base + "/mcp").code == "no_s256"

@pytest.mark.parametrize("target", ["http://10.0.0.5", "http://169.254.169.254", "http://[fd00::1]"])
def test_probe_refuses_a_prm_that_points_at_an_internal_or_metadata_address(target):
    with Mini(hostile(prm=lambda m: {"resource": m.base + "/mcp", "authorization_servers": [target]})) as m:   # the MCP server itself IS allow-listed (loopback)
        e = probe_err(m.base + "/mcp"); assert e.code == "blocked"

@pytest.mark.parametrize("field", ["token_endpoint", "registration_endpoint", "revocation_endpoint"])
def test_probe_refuses_server_called_endpoints_on_internal_addresses(field):
    with Mini(hostile(asm=lambda m: good_as(m, **{field: "http://10.0.0.5/x"}))) as m: assert probe_err(m.base + "/mcp").code == "blocked"

def test_probe_refuses_internal_and_plain_http_mcp_addresses(fk, monkeypatch):
    f = fk("dcr"); monkeypatch.delenv("AOS_EGRESS_ALLOW", raising=False)   # no allow-list: loopback is internal
    assert probe_err(f.mcp_url).code == "blocked"
    assert probe_err("http://93.184.216.34/mcp").code == "blocked"        # public but plain http
    assert probe_err("http://169.254.169.254/latest/meta-data").code == "blocked"
    assert f.calls == []                                                   # nothing was sent

@pytest.mark.parametrize("bad", ["ftp://example.com/mcp", "http://user:pw@127.0.0.1:1/mcp", "https://example.com/mcp#frag", "not a url", "https:///mcp"])
def test_probe_rejects_malformed_urls(bad):
    assert probe_err(bad).code in ("bad_url", "blocked")

def test_probe_does_not_follow_redirects():
    with Mini({"/mcp": (302, {"Location": "http://169.254.169.254/"}, {})}) as m: assert probe_err(m.base + "/mcp").code == "redirect"
    # a metadata candidate that redirects is "not served here": the next candidate is tried, the redirect target is never contacted
    routes = hostile()
    routes["/prm"] = (302, {"Location": "http://169.254.169.254/"}, {})
    routes["/.well-known/oauth-protected-resource/mcp"] = lambda m: (200, {}, {"resource": m.base + "/mcp", "authorization_servers": [m.base]})
    with Mini(routes) as m: r = asyncio.run(M.probe(m.base + "/mcp")); assert r["registration"] == "dcr"

def test_probe_refuses_an_oversized_metadata_document():
    routes = hostile(); routes["/prm"] = (200, {}, b"{" + b" " * (M.MAX_BODY + 10) + b"}")
    with Mini(routes) as m: assert probe_err(m.base + "/mcp").code == "too_large"

# ================================================================== the client-id metadata document
def test_client_metadata_document_is_public_exact_and_off_without_a_stable_https(monkeypatch):
    from mcp.client.auth.utils import is_valid_client_metadata_url
    d = json.loads(asyncio.run(M.client_metadata()).body)
    assert d["client_id"] == BASE_URL + M.CIMD_PATH and is_valid_client_metadata_url(d["client_id"]) and d["redirect_uris"] == [BASE_URL + "/oauth/callback"]
    assert d["token_endpoint_auth_method"] == "none" and "client_secret" not in d and d["grant_types"] == ["authorization_code", "refresh_token"]
    for v in ("http://aos.example.test", None):
        monkeypatch.delenv("AOS_PUBLIC_BASE", raising=False)
        if v: monkeypatch.setenv("AOS_PUBLIC_BASE", v)
        with pytest.raises(HTTPException) as e: asyncio.run(M.client_metadata())
        assert e.value.status_code == 404
    assert M.CIMD_PATH in admin.PUBLIC_PREFIX   # the authorization server fetches it without a cookie

# ================================================================== registration: CIMD, DCR, adopt, manual
def test_register_cimd_creates_provider_connection_and_server(fk):
    f = fk("dcr_cimd")
    async def t():
        out = await register(f, "cm")
        assert out["registration"] == "cimd" and out["provider"] == out["connection"] == "mcp-cm" and out["connect"] == "/api/oauth/connect/mcp-cm"
        p = await prov("mcp-cm")
        assert (p["source"], p["registration"], p["client_id"], p["token_auth"]) == ("mcp", "cimd", BASE_URL + M.CIMD_PATH, "none") and p["client_secret_enc"] is None
        assert (p["issuer"], p["iss_supported"], p["resource"], p["scopes"], p["pkce"]) == (f.issuer, True, f.mcp_url, "read write", True)
        assert (p["redirect_mode"], p["redirect_base"], p["redirect_uri_registered"], p["default_redirect_uri"]) == ("tunnel", BASE_URL, BASE_URL + "/oauth/callback", None)
        c = await A.q("SELECT * FROM connections WHERE workspace_id=%s AND slug='mcp-cm'", (WA,), one=True)
        assert (c["kind"], c["mcp_url"], c["provider_id"]) == ("mcp_oauth", f.mcp_url, p["id"])
        s = await A.q("SELECT * FROM mcp_servers WHERE workspace_id=%s AND name='cm'", (WA,), one=True)
        assert (s["auth_type"], s["connection_id"], s["url"]) == ("oauth", c["id"], f.mcp_url)
        assert not f.registrations   # CIMD: nothing was registered anywhere
        q, resp = await connect_cb(alice(), "mcp-cm"); assert resp.status_code == 303 and q["iss"] == f.issuer   # the metadata-document client id works end to end
    run(t)

def test_register_dcr_registers_the_https_redirect_and_seals_the_secrets(fk, caplog):
    f = fk("dcr")
    async def t():
        out = await register(f, "dc", scopes="read")
        assert out["registration"] == "dcr" and out["scopes"] == "read" and out["redirect_uri"] == BASE_URL + "/oauth/callback"
        (cid, reg), = f.registrations.items()
        assert reg["redirect_uris"] == [BASE_URL + "/oauth/callback"] and reg["body"]["application_type"] == "web" and reg["body"]["token_endpoint_auth_method"] == "client_secret_post"
        assert reg["body"]["grant_types"] == ["authorization_code", "refresh_token"] and reg["body"]["scope"] == "read"
        p = await prov("mcp-dc")
        assert (p["registration"], p["client_id"], p["token_auth"], p["redirect_mode"], p["redirect_base"]) == ("dcr", cid, "post", "tunnel", BASE_URL)
        assert p["allowed_scopes"] == "read write" and p["registration_client_uri"] == f"{f.base}/register/{cid}"
        secret = f.main.clients[cid]
        assert secret and secret not in p["client_secret_enc"] and O._sec(p) == secret                                     # sealed, and it decrypts
        assert "rat_" not in p["registration_token_enc"] and O.dec(p["registration_token_enc"], "oauth_providers.registration_token_enc", WA, p["id"]) == "rat_" + cid
        # nothing secret leaves through the API, the audit trail or the log
        listing = json.dumps(await as_(adm(), O.prov_list), default=str); pub = [x for x in json.loads(listing) if x["name"] == "mcp-dc"][0]
        assert "registration_token_enc" not in pub and "client_secret_enc" not in pub and pub["has_registration_token"] and pub["has_secret"]
        dump = listing + json.dumps(out) + json.dumps(await A.q("SELECT * FROM oauth_audit WHERE workspace_id=%s", (WA,)), default=str) + caplog.text
        assert secret not in dump and "rat_" + cid not in dump
        # the whole sign-in works: resource indicator, iss, then the token opens the protected MCP server as that user
        q, resp = await connect_cb(alice(), "mcp-dc"); assert resp.status_code == 303 and q["iss"] == f.issuer
        tok = await token_of(alice(), "mcp-dc"); assert f.main.bearer("Bearer " + tok)["resource"] == f.mcp_url
        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport
        async with Client(StreamableHttpTransport(f.mcp_url, headers={"Authorization": "Bearer " + tok})) as cl: assert (await cl.call_tool("whoami", {})).data == "user-1"
    run(t)

def test_a_wrong_iss_at_the_callback_is_rejected_and_nothing_is_connected(fk):
    f = fk("dcr")
    async def t():
        await register(f, "wi"); f.wrong_iss = True
        q, resp = await connect_cb(alice(), "mcp-wi"); assert q["iss"] == "https://evil.example" and resp.status_code == 400
        assert not await A.q("SELECT 1 FROM oauth_connections WHERE workspace_id=%s", (WA,), one=True)
    run(t)

def test_register_manual_client_for_a_server_without_metadata_then_sign_in(fk):
    f = fk("none")
    async def t():
        d = await refuse(register(f, "gh"), 409)   # nothing automatic is possible: say so, create nothing
        assert d["error"] == "manual_registration_required" and "đăng ký sẵn" in d["message"] and {"client_id", "client_secret", "authorize_url", "token_url"} <= set(d["needs"])
        assert d["redirect_uri"] == BASE_URL + "/oauth/callback" and d["probe"]["manual_required"]
        d = await refuse(register(f, "gh", client_id=MANUAL_CLIENT[0], client_secret=MANUAL_CLIENT[1]), 422); assert d["error"] == "endpoints_required"
        assert set((await counts()).values()) == {0}
        out = await register(f, "gh", client_id=MANUAL_CLIENT[0], client_secret=MANUAL_CLIENT[1], authorize_url=f.base + "/authorize", token_url=f.base + "/token", scopes="read")
        assert out["registration"] == "manual" and out["redirect_uri"] == BASE_URL + "/oauth/callback"
        p = await prov("mcp-gh"); assert (p["registration"], p["client_id"], p["issuer"], p["iss_supported"], p["resource"], p["redirect_uri_registered"]) == ("manual", MANUAL_CLIENT[0], None, False, f.mcp_url, None)
        assert O._sec(p) == MANUAL_CLIENT[1] and MANUAL_CLIENT[1] not in p["client_secret_enc"]
        q, resp = await connect_cb(alice(), "mcp-gh"); assert resp.status_code == 303 and "iss" not in q
        assert f.main.bearer("Bearer " + await token_of(alice(), "mcp-gh"))["resource"] == f.mcp_url
    run(t)

def test_a_client_the_admin_typed_wins_over_cimd_and_dcr(fk):
    f = fk("dcr_cimd")
    async def t():
        out = await register(f, "ty", client_id=MANUAL_CLIENT[0], client_secret=MANUAL_CLIENT[1])
        assert out["registration"] == "manual" and not f.registrations and (await prov("mcp-ty"))["client_id"] == MANUAL_CLIENT[0]
    run(t)

def test_register_adopts_a_preregistered_provider_without_registering_anything(fk):
    f = fk("dcr")
    async def t():
        mk = lambda **kw: O.ProviderIn(**{**dict(name="mypre", authorize_url=f.base + "/authorize", token_url=f.base + "/token", api_base=f.mcp_url, client_id=MANUAL_CLIENT[0],
                                                client_secret=MANUAL_CLIENT[1], scopes="read"), **kw})
        pre = await as_(adm(), O.prov_new, mk())
        out = await register(f, "ad", provider="mypre")
        assert out["registration"] == "preregistered" and out["provider"] == "mypre" and not f.registrations
        p = await prov("mypre"); assert (p["registration"], p["issuer"], p["resource"], p["iss_supported"], p["source"]) == ("preregistered", f.issuer, f.mcp_url, True, "manual")
        c = await A.q("SELECT * FROM connections WHERE workspace_id=%s AND provider_id=%s", (WA, pre["id"]), one=True); assert (c["kind"], c["mcp_url"]) == ("mcp_oauth", f.mcp_url)
        assert (await A.q("SELECT auth_type, connection_id FROM mcp_servers WHERE workspace_id=%s AND name='ad'", (WA,), one=True)) == {"auth_type": "oauth", "connection_id": c["id"]}
        q, resp = await connect_cb(alice(), "mypre"); assert resp.status_code == 303 and q["iss"] == f.issuer   # iss is now checked for the adopted provider too
        # a provider whose endpoints are not this server's, another workspace's provider, one bound to another resource, a missing one
        await as_(adm(), O.prov_new, mk(name="wrong", token_url=f.base + "/elsewhere"))
        assert (await refuse(register(f, "ad2", provider="wrong"), 409))["error"] == "provider_mismatch"
        await as_(adm(WB), O.prov_new, mk(name="theirs")); await refuse(register(f, "ad3", provider="theirs"), 404)
        await as_(adm(), O.prov_new, mk(name="bound", resource="http://127.0.0.1:9/other")); await refuse(register(f, "ad4", provider="bound"), 409)
        await refuse(register(f, "ad5", provider="nope"), 404)
        assert (await prov("wrong"))["registration"] == "manual" and (await prov("theirs", WB))["registration"] == "manual"
        assert not await A.q("SELECT 1 FROM mcp_servers WHERE workspace_id=%s AND name IN ('ad2','ad3','ad4','ad5')", (WA,), one=True)
    run(t)

def test_register_turns_an_existing_server_row_into_an_oauth_one(fk):
    f = fk("dcr")
    async def t():
        sid = (await A.q("INSERT INTO mcp_servers(name,url,workspace_id) VALUES('ex',%s,%s) RETURNING id", (f.mcp_url, WA), one=True))["id"]
        await register(f, "ex")
        rows = await A.q("SELECT id, auth_type FROM mcp_servers WHERE workspace_id=%s AND name='ex'", (WA,)); assert rows == [{"id": sid, "auth_type": "oauth"}]
        # a server with another address / a fixed token, the reserved name, a server that needs no sign-in, a dead one
        await A.q("INSERT INTO mcp_servers(name,url,workspace_id) VALUES('other',%s,%s)", ("http://127.0.0.1:9/x", WA))
        assert (await refuse(register(f, "other"), 409))["error"] == "server_exists"
        await A.q("INSERT INTO mcp_servers(name,url,workspace_id,token_env) VALUES('tok',%s,%s,'AOS_MCP_TOKEN_X')", (f.mcp_url, WA))
        assert (await refuse(register(f, "tok"), 409))["error"] == "server_exists"
        await refuse(register(f, "ha"), 409)
        with Mini({"/mcp": (200, {}, {})}) as m:
            assert (await refuse(as_(adm(), M.api_register, M.RegisterIn(name="open", url=m.base + "/mcp"), req()), 409))["error"] == "no_auth_needed"
            assert (await refuse(as_(adm(), M.api_register, M.RegisterIn(name="dead", url="http://127.0.0.1:1/mcp"), req()), 409))["error"] == "no_auth_needed"
        assert not await A.q("SELECT 1 FROM mcp_servers WHERE workspace_id=%s AND name IN ('open','dead')", (WA,), one=True)
    run(t)

# ================================================================== registration: a failure leaves nothing half-created
def test_a_refused_registration_creates_nothing(fk, monkeypatch):
    f = fk("dcr")
    async def t():
        async def refused(*a, **k): raise M.ProbeError("registration_failed", "Đăng ký bị từ chối", 502)
        monkeypatch.setattr(M, "_dcr", refused)
        assert (await refuse(register(f, "rf"), 502))["error"] == "registration_failed"
        assert set((await counts()).values()) == {0} and not f.registrations
    run(t)

def test_registration_that_fails_after_the_remote_client_exists_is_undone_everywhere(fk, monkeypatch):
    f = fk("dcr")
    async def t():
        async def boom(*a, **k): raise RuntimeError("db down")
        for n, step in enumerate(("_link_server", "_link_connection"), 1):
            monkeypatch.setattr(M, step, boom)
            with pytest.raises(RuntimeError): await register(f, "un")
            monkeypatch.undo(); monkeypatch.setenv("AOS_PUBLIC_BASE", BASE_URL)
            assert set((await counts()).values()) == {0}, step                      # provider, connection and server are all gone
            assert not f.registrations and len(f.deregistered) == n                    # and the client at the authorization server was de-registered
        # an existing server row stays as it was
        await A.q("INSERT INTO mcp_servers(name,url,workspace_id) VALUES('keep',%s,%s)", (f.mcp_url, WA))
        monkeypatch.setattr(M, "_link_connection", boom)
        with pytest.raises(RuntimeError): await register(f, "keep")
        assert (await A.q("SELECT auth_type, connection_id FROM mcp_servers WHERE workspace_id=%s AND name='keep'", (WA,), one=True)) == {"auth_type": "none", "connection_id": None}
        assert (await counts())["oauth_providers"] == 0
    run(t)

def test_adopting_a_provider_that_fails_midway_puts_it_back(fk, monkeypatch):
    f = fk("dcr")
    async def t():
        await as_(adm(), O.prov_new, O.ProviderIn(name="mypre", authorize_url=f.base + "/authorize", token_url=f.base + "/token", api_base=f.mcp_url, client_id=MANUAL_CLIENT[0], client_secret="s"))
        before = await prov("mypre")
        async def boom(*a, **k): raise RuntimeError("db down")
        monkeypatch.setattr(M, "_link_server", boom)
        with pytest.raises(RuntimeError): await register(f, "ad", provider="mypre")
        after = await prov("mypre")
        assert {k: after[k] for k in ("registration", "issuer", "resource", "iss_supported")} == {k: before[k] for k in ("registration", "issuer", "resource", "iss_supported")}
        assert not await A.q("SELECT 1 FROM connections WHERE workspace_id=%s AND kind='mcp_oauth'", (WA,), one=True)
        assert not await A.q("SELECT 1 FROM mcp_servers WHERE workspace_id=%s", (WA,), one=True)
    run(t)

# ================================================================== who may do this
def test_only_a_signed_in_admin_may_probe_or_register(fk):
    f = fk("dcr")
    async def t():
        body, rb = M.ProbeIn(url=f.mcp_url), M.RegisterIn(name="nope", url=f.mcp_url)
        for who, st in ((alice(), 403), (U(WA, SEEDS["alice"], "viewer"), 403), (U(WA, SEEDS["admin_a"], "admin", demo=True), 403), (U(WA, SEEDS["admin_a"], "admin", auth="channel"), 403)):
            await refuse(as_(who, M.api_probe, body), st); await refuse(as_(who, M.api_register, rb, req()), st)
        await refuse(M.api_probe(body), 401); await refuse(M.api_register(rb, req()), 401)   # nobody signed in
        assert f.calls == [] and set((await counts()).values()) == {0}                      # no request ever left for the server
        out = await as_(adm(), M.api_probe, body); assert out["registration"] == "dcr"
        d = await refuse(as_(adm(), M.api_probe, M.ProbeIn(url="http://169.254.169.254/latest")), 422); assert d["error"] == "blocked"
    run(t)

def test_nothing_crosses_workspaces(fk):
    f = fk("dcr")
    async def t():
        await register(f, "shared"); await register(f, "shared", ws=WB)             # the same server name in two workspaces: two independent sets of rows
        pa, pb = await prov("mcp-shared"), await prov("mcp-shared", WB); assert pa["id"] != pb["id"] and pa["client_id"] != pb["client_id"] and len(f.registrations) == 2
        await register(f, "only-a")
        assert [x["name"] for x in await as_(adm(WB), O.prov_list)] == ["mcp-shared"]
        assert {r["name"] for r in await as_(adm(WB), A.q, "SELECT name FROM mcp_servers", ())} == {"shared"}              # RLS: B sees none of A's rows
        assert {r["slug"] for r in await as_(adm(WB), A.q, "SELECT slug FROM connections", ())} == {"mcp-shared"}
        await refuse(as_(adm(WB), O.connect, "mcp-only-a", req(), None), 404)
        await refuse(register(f, "xx", provider="mcp-only-a", ws=WB), 404)
        pid = (await prov("mcp-only-a"))["id"]; await refuse(as_(adm(WB), O.prov_reregister, pid, req()), 404)
        await refuse(as_(adm(WB), C.get_token, ("user", SEEDS["admin_b"]), "mcp-only-a", ws=WB), 404)
        assert (await prov("mcp-only-a"))["workspace_id"] == WA and not await A.q("SELECT 1 FROM mcp_servers WHERE workspace_id=%s AND name='only-a'", (WB,), one=True)
    run(t)

def test_forged_forwarded_host_never_reaches_a_registered_redirect(fk, monkeypatch):
    f = fk("dcr"); forged = {"X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "https", "CF-Visitor": '{"scheme":"https"}'}
    async def t():
        await register(f, "fh", request=req(forged, client="203.0.113.7"))
        (reg,) = f.registrations.values(); assert reg["redirect_uris"] == [BASE_URL + "/oauth/callback"]   # AOS_PUBLIC_BASE decides
        f.registrations.clear()
        monkeypatch.delenv("AOS_PUBLIC_BASE")   # no operator address: an ordinary client's headers decide nothing, and a plain-http host can not be registered
        assert (await refuse(register(f, "fh2", request=req(forged, client="203.0.113.7")), 409))["error"] == "redirect_not_secure"
        assert not f.registrations and not await A.q("SELECT 1 FROM oauth_providers WHERE workspace_id=%s AND name='mcp-fh2'", (WA,), one=True)
        await register(f, "fh3", request=req(host="127.0.0.1:8023"))   # development on loopback: http is fine and is pinned as the redirect
        p = await prov("mcp-fh3"); assert (p["redirect_mode"], p["default_redirect_uri"]) == ("web", "http://127.0.0.1:8023/oauth/callback")
        (reg,) = f.registrations.values(); assert reg["redirect_uris"] == ["http://127.0.0.1:8023/oauth/callback"] and reg["body"]["application_type"] == "native"
    run(t)

# ================================================================== re-register (AOS_PUBLIC_BASE changed)
def test_reregister_dcr_registers_the_new_address_and_asks_everyone_to_reconnect(fk, monkeypatch):
    f = fk("dcr")
    async def t():
        await register(f, "rr"); await connect_cb(alice(), "mcp-rr"); old = await prov("mcp-rr")
        await refuse(as_(alice(), O.prov_reregister, old["id"], req()), 403)
        monkeypatch.setenv("AOS_PUBLIC_BASE", "https://new.example.test")
        r = await as_(adm(), O.prov_reregister, old["id"], req())
        assert r["registration"] == "dcr" and r["redirect_uri"] == "https://new.example.test/oauth/callback"
        p = await prov("mcp-rr"); new_cid = [c for c in f.registrations if c != old["client_id"]][0]
        assert p["client_id"] == new_cid and p["redirect_base"] == "https://new.example.test" and p["redirect_uri_registered"] == "https://new.example.test/oauth/callback"
        assert f.registrations[new_cid]["redirect_uris"] == ["https://new.example.test/oauth/callback"] and f.deregistered == [old["client_id"]]
        assert O._sec(p) == f.main.clients[new_cid] and O.dec(p["registration_token_enc"], "oauth_providers.registration_token_enc", WA, p["id"]) == "rat_" + new_cid
        c = await A.q("SELECT status, status_reason FROM oauth_connections WHERE provider_id=%s", (p["id"],), one=True); assert c == {"status": "needs_reconnect", "status_reason": "redirect_changed"}
        # a failing new registration changes nothing
        async def refused(*a, **k): raise M.ProbeError("registration_failed", "refused", 502)
        monkeypatch.setattr(M, "_dcr", refused); monkeypatch.setenv("AOS_PUBLIC_BASE", "https://third.example.test")
        await refuse(as_(adm(), O.prov_reregister, p["id"], req()), 502)
        q = await prov("mcp-rr"); assert (q["client_id"], q["redirect_base"]) == (p["client_id"], p["redirect_base"])
    run(t)

def test_reregister_cimd_takes_the_new_document_url_and_cross_workspace_is_refused(fk, monkeypatch):
    f = fk("dcr_cimd")
    async def t():
        await register(f, "rc"); p = await prov("mcp-rc")
        await refuse(as_(adm(WB), O.prov_reregister, p["id"], req()), 404)
        monkeypatch.setenv("AOS_PUBLIC_BASE", "https://new.example.test")
        r = await as_(adm(), O.prov_reregister, p["id"], req()); assert r["registration"] == "cimd"
        q = await prov("mcp-rc"); assert q["client_id"] == "https://new.example.test" + M.CIMD_PATH and q["redirect_uri_registered"] == "https://new.example.test/oauth/callback"
        assert not f.registrations
    run(t)

# ================================================================== adding a server that answers 401
def test_adding_a_server_that_answers_401_says_it_needs_sign_in(fk):
    f = fk("dcr")
    async def t():
        out = await as_(adm(), R.add_server, R.McpServer(name="fk401", url=f.mcp_url))
        assert out["status"].startswith("lỗi") and out["needs_auth"] is True and out["probe"]["registration"] == "dcr" and out["probe"]["needs_auth"]
        dead = await as_(adm(), R.add_server, R.McpServer(name="dead1", url="http://127.0.0.1:9/mcp"))
        assert dead["status"].startswith("lỗi") and "needs_auth" not in dead                       # a plain failure stays a plain failure
        with Mini({"/mcp": (500, {}, {})}) as m:
            assert "needs_auth" not in await as_(adm(), R.add_server, R.McpServer(name="err500", url=m.base + "/mcp"))
    run(t)

# ================================================================== plumbing: migration, crypto targets
def test_migration_0023_up_down_twice_check_and_legacy_rows():
    m21 = next(m for v, m, _ in migrations._load() if v == "0021"); mod = next(m for v, m, _ in migrations._load() if v == "0023"); sch = "t1126_" + uuid.uuid4().hex[:8]
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as root: await root.execute(f'CREATE SCHEMA "{sch}"')
        try:
            async with await psycopg.AsyncConnection.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
                q = migrations._q(c)
                for s in m21._BASE: await q(s)
                await q("INSERT INTO oauth_providers(id,name,authorize_url,token_url,api_base,client_id,workspace_id) VALUES(1,'crm','a','t','b','c',7)")
                assert await mod.plan(q)
                await mod.up(q); await mod.up(q)   # idempotent
                assert (await q("SELECT registration,registration_client_uri,registration_token_enc,redirect_uri_registered FROM oauth_providers"))[0] == ("manual", None, None, None)
                for ok in ("dcr", "cimd", "preregistered", "manual"): await c.execute("UPDATE oauth_providers SET registration=%s", (ok,))
                with pytest.raises(psycopg.errors.CheckViolation): await c.execute("UPDATE oauth_providers SET registration='oauth'")
                await mod.down(q)
                assert (await q("SELECT count(*) FROM oauth_providers"))[0][0] == 1   # the provider stays
                assert not await q("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name='oauth_providers' AND column_name IN ('registration','registration_token_enc')", (sch,))
                await mod.up(q)
        finally:
            async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as root: await root.execute(f'DROP SCHEMA "{sch}" CASCADE')
    asyncio.run(go())

def test_registration_token_column_is_in_the_crypto_rotation_targets():
    import aos_crypto
    assert any(t["name"] == "oauth_providers.registration_token_enc" and t["key"] == ("id",) and t["ws"] == "workspace_id" for t in aos_crypto.TEXT_TARGETS)

# ================================================================== through the real HTTP stack (own server on :8023)
PORT = 8023
HB = f"http://127.0.0.1:{PORT}"
ADMIN_AUTH, VIEWER_AUTH = ("moza", "phase0-local"), ("viewer1", "phase0-viewer-local")

@pytest.fixture(scope="module")
def server():
    os.makedirs(os.path.join(APP, "var"), exist_ok=True)
    log = open(os.path.join(APP, "var", "server_8023.log"), "ab")
    env = {**os.environ, "PORT": str(PORT), "AOS_EGRESS_ALLOW": os.environ.get("AOS_EGRESS_ALLOW") or "127.0.0.0/8,::1", "AOS_PUBLIC_BASE": BASE_URL}
    env.pop("AOS_TRUSTED_PROXIES", None)
    pr = subprocess.Popen([sys.executable, os.path.join(APP, "overlay", "run_local.py")], cwd=APP, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        for _ in range(180):
            try:
                if httpx.get(HB + "/health", timeout=2).status_code < 500: break
            except httpx.HTTPError: pass
            time.sleep(1)
        else: raise RuntimeError("test server on :8023 did not start (see var/server_8023.log)")
        yield HB
    finally:
        try: os.killpg(pr.pid, signal.SIGTERM)
        except ProcessLookupError: pass
        pr.wait(20); log.close()

def test_http_probe_register_roles_and_the_public_client_document(server, fk):
    f = fk("dcr"); a = httpx.Client(base_url=server, auth=ADMIN_AUTH, timeout=90); v = httpx.Client(base_url=server, auth=VIEWER_AUTH, timeout=90); anon = httpx.Client(base_url=server, timeout=90)
    name = "t1126h" + TAG; made = []
    try:
        d = anon.get(M.CIMD_PATH); assert d.status_code == 200 and d.json()["client_id"] == BASE_URL + M.CIMD_PATH   # public, no cookie
        for c, st in ((anon, 401), (v, 403)):
            assert c.post("/api/mcp/servers/probe", json={"url": f.mcp_url}).status_code == st
            assert c.post("/api/mcp/servers/oauth", json={"name": name, "url": f.mcp_url}).status_code == st
        assert f.calls == []
        r = a.post("/api/mcp/servers/probe", json={"url": f.mcp_url}); assert r.status_code == 200 and r.json()["needs_auth"] and r.json()["registration"] == "dcr"
        for bad in ("http://169.254.169.254/latest", "http://10.255.255.1/mcp", "http://93.184.216.34/mcp"):
            r = a.post("/api/mcp/servers/probe", json={"url": bad}); assert r.status_code == 422 and r.json()["detail"]["error"] == "blocked", (bad, r.text)
        r = a.post("/api/mcp/servers/oauth", json={"name": name, "url": f.mcp_url}, headers={"X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "https"})
        assert r.status_code == 200, r.text; made.append(name)
        assert r.json()["registration"] == "dcr" and r.json()["redirect_uri"] == BASE_URL + "/oauth/callback" and "evil.example" not in r.text
        (reg,) = f.registrations.values(); assert reg["redirect_uris"] == [BASE_URL + "/oauth/callback"]
        lst = a.get("/api/oauth/providers").json(); mine = [x for x in lst if x["name"] == "mcp-" + name]
        assert mine and mine[0]["source"] == "mcp" and "registration_token_enc" not in mine[0] and "client_secret_enc" not in mine[0]
        assert a.post("/api/mcp/servers/oauth", json={"name": name, "url": f.mcp_url}).status_code == 409   # already registered under that provider name
    finally:
        for n in made:
            p = [x for x in a.get("/api/oauth/providers").json() if x["name"] == "mcp-" + n]
            if p: a.delete(f"/api/oauth/providers/{p[0]['id']}")
            a.delete(f"/api/mcp/servers/{n}")
