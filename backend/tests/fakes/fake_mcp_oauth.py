"""GĐ11.30 fake OAuth / MCP servers for tests ($0, 127.0.0.1, one background uvicorn thread per instance). Used by 11.25 / 11.26 / 11.27 / 11.29.
FakeOAuthMcp(mode) serves on ONE port:
  * an authorization server (issuer = base): RFC 8414 + OIDC metadata, /authorize (PKCE S256 only, `resource`, answers with `iss`), /token (authorization_code, rotating
    refresh_token, client_credentials; short-lived access tokens), /revoke, /register (DCR);
  * a protected MCP server at /mcp (FastMCP): 401 + `WWW-Authenticate: Bearer resource_metadata=...` without a token, RFC 9728 PRM at
    /.well-known/oauth-protected-resource[/mcp], tools `whoami` (identity of the token), `read_data` (scope read), `write_data` (scope write, else 403 insufficient_scope);
  * fake Google (/google/*) and Microsoft (/ms/*) authorize + token + API pairs for loopback-redirect (paste-the-address) flows.
Modes: "dcr_cimd" (a: DCR + client-id metadata documents), "dcr" (b: DCR only), "none" (c: GitHub-like, no metadata at all, manual client id/secret).
Knobs (attributes, change at any time): wrong_iss (authorize answers a different `iss`), token_ttl (s, default 60), registrations / calls (every request: method, path, headers lower-cased).
Manual client (mode "none" and always accepted): MANUAL_CLIENT. Tests set AOS_EGRESS_ALLOW=127.0.0.1 (conftest does)."""
import asyncio, base64, hashlib, json, secrets, socket, threading, time
from urllib.parse import parse_qsl, urlencode, urlparse

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

MANUAL_CLIENT = ("manual-client-id", "manual-client-secret")
MODES = ("dcr_cimd", "dcr", "none")
SCOPES = ["read", "write"]

def _s256(v: str) -> str: return base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()

class _AS:
    """One authorization server namespace (main / google / ms): its own clients, codes and tokens."""
    def __init__(self, owner, prefix, clients, rotate=True, offline=False, pkce=True):
        self.o, self.p, self.clients, self.rotate, self.offline, self.pkce = owner, prefix, dict(clients), rotate, offline, pkce
        self.codes, self.tokens, self.refresh = {}, {}, {}
        self.paths = {"authorize": prefix + "/authorize", "token": prefix + "/token", "revoke": prefix + "/revoke", "register": prefix + "/register", "userinfo": prefix + "/api/userinfo"}

    def routes(self):
        return [Route(self.paths["authorize"], self.authorize, methods=["GET"]), Route(self.paths["token"], self.token, methods=["POST"]),
                Route(self.paths["revoke"], self.revoke, methods=["POST"]), Route(self.paths["userinfo"], self.userinfo, methods=["GET"])]

    def _client_ok(self, cid, redirect_uri):
        if cid in self.clients: return True
        return self.p == "" and self.o.mode == "dcr_cimd" and cid.startswith("http") and bool(redirect_uri)   # client-id metadata document (not fetched)

    async def authorize(self, r: Request):
        q = dict(r.query_params); cid, ru, st = q.get("client_id", ""), q.get("redirect_uri", ""), q.get("state", "")
        if q.get("response_type") != "code" or not self._client_ok(cid, ru): return JSONResponse({"error": "unauthorized_client"}, 400)
        reg = self.o.registrations.get(cid)
        if reg and ru not in reg["redirect_uris"]: return JSONResponse({"error": "invalid_request", "error_description": "redirect_uri not registered"}, 400)
        if self.pkce and (q.get("code_challenge_method") != "S256" or not q.get("code_challenge")): return JSONResponse({"error": "invalid_request", "error_description": "PKCE S256 required"}, 400)
        if q.get("deny"): return RedirectResponse(ru + ("&" if "?" in ru else "?") + urlencode({"error": "access_denied", "state": st}), 302)
        code = secrets.token_urlsafe(16)
        self.codes[code] = {"client": cid, "ru": ru, "challenge": q.get("code_challenge"), "scope": q.get("scope", ""), "resource": q.get("resource"), "exp": time.time() + 60,
                            "sub": self.o.sub, "offline": q.get("access_type") == "offline" or "offline_access" in q.get("scope", "")}
        out = {"code": code, "state": st}
        if self.p == "" and self.o.mode != "none": out["iss"] = "https://evil.example" if self.o.wrong_iss else self.o.base
        return RedirectResponse(ru + ("&" if "?" in ru else "?") + urlencode(out), 302)

    def _issue(self, sub, scope, client, resource, refresh=True):
        at = "at_" + secrets.token_urlsafe(18)
        self.tokens[at] = {"sub": sub, "scope": scope, "client": client, "resource": resource, "exp": time.time() + self.o.token_ttl}
        out = {"access_token": at, "token_type": "Bearer", "expires_in": self.o.token_ttl, "scope": scope}
        if refresh:
            rt = "rt_" + secrets.token_urlsafe(18); self.refresh[rt] = {"sub": sub, "scope": scope, "client": client, "resource": resource}; out["refresh_token"] = rt
        return out

    async def token(self, r: Request):
        o = await self._token(r); return o if isinstance(o, JSONResponse) else JSONResponse(o)

    async def _token(self, r: Request):
        f = dict(parse_qsl((await r.body()).decode())); gt, cid = f.get("grant_type"), f.get("client_id", "")
        basic = r.headers.get("authorization", "")
        if basic.lower().startswith("basic "):
            cid, _, f["client_secret"] = base64.b64decode(basic[6:]).decode().partition(":")
        if cid not in self.clients and not (self.o.mode == "dcr_cimd" and cid.startswith("http") and self.p == ""): return JSONResponse({"error": "invalid_client"}, 401)
        want = self.clients.get(cid)
        if want and f.get("client_secret") != want: return JSONResponse({"error": "invalid_client"}, 401)
        if gt == "authorization_code":
            c = self.codes.pop(f.get("code", ""), None)
            if not c or c["exp"] < time.time() or c["ru"] != f.get("redirect_uri") or c["client"] != cid: return JSONResponse({"error": "invalid_grant"}, 400)
            if c["challenge"] and _s256(f.get("code_verifier", "")) != c["challenge"]: return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, 400)
            return self._issue(c["sub"], c["scope"], cid, c["resource"], refresh=(not self.offline) or c["offline"])
        if gt == "refresh_token":
            rt = f.get("refresh_token", ""); d = self.refresh.pop(rt, None) if self.rotate else self.refresh.get(rt)
            if not d or d["client"] != cid: return JSONResponse({"error": "invalid_grant"}, 400)   # a used (rotated) refresh token is dead
            out = self._issue(d["sub"], d["scope"], cid, d["resource"], refresh=self.rotate)
            if not self.rotate: out["refresh_token"] = rt
            return out
        if gt == "client_credentials":
            if not want: return JSONResponse({"error": "unauthorized_client"}, 401)
            return self._issue("client:" + cid, f.get("scope", ""), cid, f.get("resource"), refresh=False)
        return JSONResponse({"error": "unsupported_grant_type"}, 400)

    async def revoke(self, r: Request):
        f = dict(parse_qsl((await r.body()).decode())); t = f.get("token", ""); self.tokens.pop(t, None); self.refresh.pop(t, None); return JSONResponse({})

    def bearer(self, header: str):
        t = self.tokens.get(header[7:]) if header.lower().startswith("bearer ") else None
        return t if t and t["exp"] > time.time() else None

    async def userinfo(self, r: Request):
        t = self.bearer(r.headers.get("authorization", ""))
        if not t: return JSONResponse({"error": "invalid_token"}, 401, headers={"WWW-Authenticate": 'Bearer error="invalid_token"'})
        return JSONResponse({"sub": t["sub"], "email": f"{t['sub']}@example.test", "scope": t["scope"]})

class FakeOAuthMcp:
    def __init__(self, mode="dcr_cimd"):
        assert mode in MODES, mode
        self.mode, self.wrong_iss, self.token_ttl, self.sub = mode, False, 60, "user-1"
        self.registrations, self.calls, self.deregistered = {}, [], []   # deregistered: client ids removed through the RFC 7592 registration_client_uri (GĐ11.26)
        self._sock = socket.socket(); self._sock.bind(("127.0.0.1", 0)); self.port = self._sock.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.main = _AS(self, "", {MANUAL_CLIENT[0]: MANUAL_CLIENT[1]})
        self.google = _AS(self, "/google", {"google-client-id": "google-secret"}, rotate=False, offline=True)
        self.ms = _AS(self, "/ms", {"ms-client-id": "ms-secret"}, rotate=True)
        routes = [Route("/.well-known/oauth-authorization-server", self._asm), Route("/.well-known/openid-configuration", self._asm),
                  Route("/.well-known/oauth-protected-resource", self._prm), Route("/.well-known/oauth-protected-resource/mcp", self._prm),
                  Route("/register", self._register, methods=["POST"]), Route("/register/{cid}", self._deregister, methods=["DELETE"]), *self.main.routes(), *self.google.routes(), *self.ms.routes()]
        self._rest = Starlette(routes=routes)
        self._mcp = self._build_mcp()
        async def app(scope, receive, send): await self._asgi(scope, receive, send)   # plain function: uvicorn mistakes a bound method for an ASGI2 app
        self._srv = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="on"))
        self._th = threading.Thread(target=lambda: asyncio.run(self._srv.serve([self._sock])), daemon=True)

    # ---- lifecycle / urls
    def start(self):
        self._th.start()
        for _ in range(300):
            if self._srv.started: return self
            time.sleep(0.02)
        raise RuntimeError("fake oauth/mcp server did not start")
    def stop(self): self._srv.should_exit = True; self._th.join(5)
    def __enter__(self): return self.start()
    def __exit__(self, *a): self.stop()
    @property
    def mcp_url(self): return self.base + "/mcp"
    @property
    def issuer(self): return self.base
    @property
    def prm_url(self): return self.base + "/.well-known/oauth-protected-resource/mcp"
    @property
    def urls(self): return {"mcp": self.mcp_url, "issuer": self.issuer, "prm": self.prm_url, "authorize": self.base + "/authorize", "token": self.base + "/token",
                            "register": self.base + "/register", "google": self.base + "/google", "ms": self.base + "/ms"}
    def has_metadata(self): return self.mode != "none"

    # ---- documents
    async def _prm(self, r):
        if not self.has_metadata(): return JSONResponse({"error": "not_found"}, 404)
        return JSONResponse({"resource": self.mcp_url, "authorization_servers": [self.issuer], "scopes_supported": SCOPES, "bearer_methods_supported": ["header"]})

    async def _asm(self, r):
        if not self.has_metadata(): return JSONResponse({"error": "not_found"}, 404)
        d = {"issuer": self.issuer, "authorization_endpoint": self.base + "/authorize", "token_endpoint": self.base + "/token", "revocation_endpoint": self.base + "/revoke",
             "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token", "client_credentials"],
             "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
             "scopes_supported": SCOPES, "authorization_response_iss_parameter_supported": True, "registration_endpoint": self.base + "/register"}
        if self.mode == "dcr_cimd": d["client_id_metadata_document_supported"] = True
        return JSONResponse(d)

    async def _register(self, r: Request):
        if not self.has_metadata(): return JSONResponse({"error": "not_found"}, 404)
        b = await r.json(); uris = b.get("redirect_uris") or []
        if not uris: return JSONResponse({"error": "invalid_redirect_uri"}, 400)
        cid = f"dcr-{len(self.registrations) + 1}-{secrets.token_hex(3)}"; sec = secrets.token_urlsafe(12) if b.get("token_endpoint_auth_method") != "none" else None
        self.registrations[cid] = {"redirect_uris": uris, "body": b}; self.main.clients[cid] = sec
        out = {"client_id": cid, "client_id_issued_at": int(time.time()), "redirect_uris": uris, "grant_types": b.get("grant_types", ["authorization_code", "refresh_token"]),
               "token_endpoint_auth_method": b.get("token_endpoint_auth_method", "client_secret_post" if sec else "none")}
        if sec: out["client_secret"] = sec
        out["registration_client_uri"] = f"{self.base}/register/{cid}"; out["registration_access_token"] = "rat_" + cid   # RFC 7592
        return JSONResponse(out, 201)

    async def _deregister(self, r: Request):
        cid = r.path_params["cid"]
        if r.headers.get("authorization") != "Bearer rat_" + cid or cid not in self.registrations: return JSONResponse({"error": "invalid_token"}, 401)
        self.registrations.pop(cid); self.main.clients.pop(cid, None); self.deregistered.append(cid)
        return Response(status_code=204)

    # ---- protected MCP
    def _build_mcp(self):
        from fastmcp import FastMCP
        from fastmcp.server.dependencies import get_http_headers
        m = FastMCP("fake-protected")
        def who():
            t = self.main.bearer(get_http_headers(include={"authorization"}).get("authorization", ""))
            return t["sub"] if t else "anonymous"
        @m.tool
        def whoami() -> str:
            """Identity of the token this call carried."""
            return who()
        @m.tool
        def read_data() -> str:
            """Needs scope read."""
            return "data-for-" + who()
        @m.tool
        def write_data() -> str:
            """Needs scope write."""
            return "wrote-as-" + who()
        return m.http_app(path="/mcp")

    def _challenge(self, **kw):
        items = ([f'resource_metadata="{self.prm_url}"'] if self.has_metadata() else []) + [f'{k}="{v}"' for k, v in kw.items()]
        return {"WWW-Authenticate": "Bearer" + (" " + ", ".join(items) if items else "")}

    async def _gate(self, scope, receive, send):
        h = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        body, more = b"", True
        while more:
            m = await receive(); body += m.get("body", b""); more = m.get("more_body", False)
        tok = self.main.bearer(h.get("authorization", ""))
        resp = None
        if not tok or (tok.get("resource") and tok["resource"] != self.mcp_url and self.has_metadata()):
            resp = JSONResponse({"error": "invalid_token"}, 401, headers=self._challenge(error="invalid_token") if h.get("authorization") else self._challenge())
        else:
            need = None
            try: j = json.loads(body or b"{}")
            except ValueError: j = {}
            if isinstance(j, dict) and j.get("method") == "tools/call":
                name = (j.get("params") or {}).get("name"); need = {"read_data": "read", "write_data": "write"}.get(name)
            if need and need not in tok["scope"].split():
                resp = JSONResponse({"error": "insufficient_scope"}, 403, headers=self._challenge(error="insufficient_scope", scope=need))
        sent = False
        async def replay():
            nonlocal sent
            if not sent: sent = True; return {"type": "http.request", "body": body, "more_body": False}
            return await receive()
        if resp: await resp(scope, replay, send)
        else: await self._mcp(scope, replay, send)

    async def _asgi(self, scope, receive, send):
        if scope["type"] == "lifespan": return await self._mcp(scope, receive, send)
        if scope["type"] == "http":
            self.calls.append({"method": scope["method"], "path": scope["path"], "headers": {k.decode().lower(): v.decode() for k, v in scope["headers"]}})
            if scope["path"].startswith("/mcp"): return await self._gate(scope, receive, send)
        await self._rest(scope, receive, send)

@pytest.fixture
def fake_oauth_mcp():
    """Factory: `fake_oauth_mcp("dcr_cimd" | "dcr" | "none")` -> started FakeOAuthMcp (stopped at teardown). `.urls` has the endpoint URLs."""
    made = []
    def make(mode="dcr_cimd"):
        s = FakeOAuthMcp(mode).start(); made.append(s); return s
    yield make
    for s in made: s.stop()
