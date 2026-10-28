"""Per-user OAuth 2.0 connections for agent tools (RFC 6749 authorization code + RFC 7636 PKCE S256, RFC 7009 revocation).
- Providers are configured by admins (client secret is write-only, encrypted at rest).
- Each user connects their own account; access/refresh tokens are encrypted at rest (GĐ9.7: envelope encryption, crypto.seal — per-workspace DEK,
  AAD bound to table.column + workspace + row key). Legacy Fernet values (AOS_OAUTH_KEY / _OLD) are still read until `python -m aos_crypto migrate`.
  Tokens are refreshed automatically shortly before expiry.
- Agent tool `oauth_request` calls the provider API with the CALLER's token, restricted to the provider's api_base.
- Every connect / refresh / revoke / tool use is written to oauth_audit.
- A built-in mock OAuth server (/mock-oauth/*, enabled with AOS_MOCK_OAUTH=1) lets the whole flow be tested without owner credentials.
GĐ11.25 engine upgrade (migration 0022; design GD11A_THIET_KE §5.10):
- EVERY server-side OAuth HTTP call (token, revocation, userinfo, provider API) goes through `netguard.client("oauth")`: https only (http only to AOS_EGRESS_ALLOW addresses),
  private / metadata addresses refused, redirects not followed. A provider whose token endpoint is a literal private IP is refused when saved; a hostname is checked on every call.
- grant_type authorization_code (PKCE S256, `resource`, `iss` check, scopes clipped to allowed_scopes, extra authorize params that can never override the security ones) or
  client_credentials (a token is fetched when the provider is saved, a failure = nothing is saved; "refresh" = fetch again; the credential is owned by the workspace).
- token_auth body | post | basic | none; token_params (plain) and token_headers (sealed, write-only) for odd token endpoints.
- `state` is random, ONE-TIME, 10 minutes, bound to user + workspace + provider (+ the owner the account is for) and carries snapshots of redirect_uri / issuer / resource / scope.
  It is deleted only by the user it was issued to; a forged, expired, replayed or foreign state changes nothing.
- redirect_uri: default_redirect_uri (https, or http on 127.0.0.1 / [::1]) wins, else by redirect_mode: web = <public base>/oauth/callback, tunnel = the same but the base must be
  https AND configured (AOS_PUBLIC_BASE or a forwarded host from a peer in AOS_TRUSTED_PROXIES), loopback_paste = http://127.0.0.1:<AOS_LOOPBACK_PORT|8765>/oauth/callback and the user
  pastes the address of the page the browser could not open into POST /api/oauth/paste-callback (Google / Microsoft while the operator has no domain, F1).
  `X-Forwarded-Host` / `X-Forwarded-Proto` / `CF-Visitor` are NEVER trusted from an ordinary client.
- AOS_PUBLIC_BASE changed -> the connections of `tunnel` providers become needs_reconnect (redirect_changed) at boot / next connect; an admin re-registers by hand
  (POST /api/oauth/providers/{id}/re-register); nothing is re-registered automatically.
- Refresh with rotation (a new refresh token replaces the old one; the lock is in connections.get_token); invalid_grant & co -> status needs_reconnect, a network error / 5xx leaves the status alone.
- Catalog of ~25 services: app/oauth_catalog.json -> GET /api/connections/catalog, POST /api/oauth/providers/from-catalog (client id + secret only)."""
import asyncio, base64, hashlib, html, json, logging, os, re, secrets, time, urllib.parse
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Literal
import httpx
import crypto, aos_crypto, netguard
from fastapi import APIRouter, HTTPException, Request, Form
from fastapi.responses import RedirectResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel, Field, model_validator
from psycopg.types.json import Jsonb
import deepagent_api as A

log = logging.getLogger("aos.oauth")

router = APIRouter(tags=["oauth"])
DDL = """
CREATE TABLE IF NOT EXISTS oauth_providers(id SERIAL PRIMARY KEY, name TEXT NOT NULL, display_name TEXT, authorize_url TEXT NOT NULL, token_url TEXT NOT NULL,
  revoke_url TEXT, userinfo_url TEXT, api_base TEXT NOT NULL, client_id TEXT NOT NULL, client_secret_enc TEXT, scopes TEXT NOT NULL DEFAULT '',
  extra_params JSONB NOT NULL DEFAULT '{}', token_auth TEXT NOT NULL DEFAULT 'body', pkce BOOLEAN NOT NULL DEFAULT true, enabled BOOLEAN NOT NULL DEFAULT true,
  workspace_id INT NOT NULL DEFAULT 1, created TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(workspace_id, name));
CREATE TABLE IF NOT EXISTS oauth_connections(id SERIAL PRIMARY KEY, provider_id INT REFERENCES oauth_providers(id) ON DELETE CASCADE, user_id INT NOT NULL,
  workspace_id INT NOT NULL DEFAULT 1, account TEXT, access_token_enc TEXT NOT NULL, refresh_token_enc TEXT, token_type TEXT, scope TEXT,
  expires_at TIMESTAMPTZ, status TEXT NOT NULL DEFAULT 'active', created TIMESTAMPTZ NOT NULL DEFAULT now(), updated TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_used TIMESTAMPTZ, last_refresh TIMESTAMPTZ, UNIQUE(provider_id, user_id));
CREATE TABLE IF NOT EXISTS oauth_states(state TEXT PRIMARY KEY, provider_id INT, user_id INT, verifier_enc TEXT, next TEXT,
  created TIMESTAMPTZ NOT NULL DEFAULT now(), expires TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_audit(id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL DEFAULT now(), user_id INT, workspace_id INT, provider TEXT,
  action TEXT NOT NULL, ok BOOLEAN NOT NULL DEFAULT true, agent_id INT, detail JSONB);
CREATE INDEX IF NOT EXISTS oauth_audit_ts ON oauth_audit(ts DESC);
"""
LOCKS: dict = {}

def enc(s, col, ws, *key):
    """col = 'oauth_providers.client_secret_enc' ...; key = natural row key that is part of the AAD (see aos_crypto.TEXT_TARGETS)."""
    return None if s is None else crypto.seal(s, int(ws), aos_crypto.ctx(col, *key))
def dec(s, col, ws, *key):
    if s is None: return None
    try: return crypto.unseal(s, int(ws), aos_crypto.ctx(col, *key))
    except crypto.CryptoError: raise HTTPException(500, "Stored token cannot be decrypted (key changed?) — reconnect the account")  # noqa: B904
def _sec(p):
    if "_secret_plain" in p: return p["_secret_plain"]   # a provider being created / updated: the secret is not sealed yet (the row id is part of the AAD)
    return dec(p["client_secret_enc"], "oauth_providers.client_secret_enc", p["workspace_id"], p["id"]) if p["client_secret_enc"] else None
def _token_headers(p) -> dict:
    """Extra token-endpoint headers (sealed JSON, write-only)."""
    if "_headers_plain" in p: return dict(p["_headers_plain"] or {})
    if not p.get("token_headers_enc"): return {}
    j = json.loads(dec(p["token_headers_enc"], "oauth_providers.token_headers_enc", p["workspace_id"], p["id"]))
    return {str(k): str(v) for k, v in j.items()} if isinstance(j, dict) else {}
def _tok(c, col):   # GĐ11.24: AAD owner part = aad_owner (user id for user rows = the key old cells were sealed with; 'agent-5' / 'workspace-0' for other owners)
    return dec(c[col], "oauth_connections." + col, c["workspace_id"], c["provider_id"], c.get("aad_owner") or c["user_id"])

def _u():
    import admin; return admin.cur() or {}
def _ws():
    import admin; return admin.cur_ws()

async def audit(action, provider=None, ok=True, detail=None, user=None, agent_id=None):
    u = user or _u()
    await A.q("INSERT INTO oauth_audit(user_id,workspace_id,provider,action,ok,agent_id,detail) VALUES(%s,%s,%s,%s,%s,%s,%s)",
              (u.get("id"), u.get("ws") or 1, provider, action, ok, agent_id, Jsonb(detail or {})))

async def init():
    await A.q(DDL)
    await A.q("ALTER TABLE lg_runs ADD COLUMN IF NOT EXISTS user_id INT")
    await A.q("DELETE FROM oauth_states WHERE expires < now()")
    await redirect_sweep()

# ------------------------------------------------------------------ public base / redirect URI
def _env_base() -> str | None:
    """AOS_PUBLIC_BASE (the operator's own statement of the public address) normalised to scheme://host[:port]; None when unset; a malformed value fails closed."""
    b = (os.environ.get("AOS_PUBLIC_BASE") or "").strip().rstrip("/")
    if not b: return None
    try: x = httpx.URL(b)
    except (httpx.InvalidURL, ValueError): x = None
    if x is None or x.scheme not in ("http", "https") or not x.host or x.userinfo or x.query or x.fragment or x.path not in ("", "/"):
        raise HTTPException(500, "AOS_PUBLIC_BASE must look like https://host[:port] (no path)")
    return f"{x.scheme}://{x.netloc.decode()}"

def _from_trusted_proxy(request: Request) -> bool:
    """True only when the TCP peer (as the server sees it) is listed in AOS_TRUSTED_PROXIES (IPs / CIDRs). Anyone else can send any forwarded header they like."""
    peer = request.client.host if request.client else ""
    try: a = ip_address(peer.split("%")[0])
    except ValueError: return False
    for x in re.split(r"[\s,;]+", os.environ.get("AOS_TRUSTED_PROXIES") or ""):
        if not x: continue
        try:
            if a in ip_network(x, strict=False): return True
        except ValueError: continue
    return False

_FWD_HOST = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9\-.]{0,251}[A-Za-z0-9])?|\[[0-9A-Fa-f:.]{2,45}\])(?::\d{1,5})?$")

def _public_base(request: Request, strict=False) -> str:
    """The address users reach this server on. ONLY AOS_PUBLIC_BASE, or X-Forwarded-Host/-Proto from a peer in AOS_TRUSTED_PROXIES (GĐ11.25: the old code trusted X-Forwarded-Host from
    anybody = a forged header could redirect the authorization code to another host). Otherwise the Host header of this very request (what the browser typed); strict=True (providers
    whose redirect is registered at the authorization server: tunnel) refuses that fallback."""
    b = _env_base()
    if b: return b
    h = request.headers
    if _from_trusted_proxy(request):
        host = (h.get("x-forwarded-host") or "").split(",")[0].strip()
        proto = (h.get("x-forwarded-proto") or "https").split(",")[0].strip().lower()
        if host and _FWD_HOST.match(host) and proto in ("http", "https"): return f"{proto}://{host}"
    if strict: raise HTTPException(409, "Cần địa chỉ HTTPS cố định: nhà vận hành đặt AOS_PUBLIC_BASE (đường hầm hoặc tên miền) / An HTTPS public address is required: the operator must set AOS_PUBLIC_BASE")
    return str(request.base_url).rstrip("/")

def _loopback_port() -> int:
    try: p = int(os.environ.get("AOS_LOOPBACK_PORT") or 8765)
    except ValueError: p = 8765
    return p if 1024 <= p <= 65535 else 8765

def _redirect_for(p, request: Request) -> tuple[str, str | None]:
    """-> (redirect_uri, public base it depends on or None). default_redirect_uri wins; else by redirect_mode."""
    if p.get("default_redirect_uri"): return p["default_redirect_uri"], None
    mode = p.get("redirect_mode") or "web"
    if mode == "loopback_paste": return f"http://127.0.0.1:{_loopback_port()}/oauth/callback", None
    base = _public_base(request, strict=(mode == "tunnel"))
    if mode == "tunnel" and not base.startswith("https://"):
        raise HTTPException(409, "Cần địa chỉ HTTPS (đường hầm hoặc tên miền) cho nhà cung cấp này / This provider needs an HTTPS public address (tunnel or domain)")
    return base + "/oauth/callback", (base if mode == "tunnel" else None)

async def _mark_redirect_changed(where_sql, args):
    return await A.q("UPDATE oauth_connections c SET status='needs_reconnect', status_reason='redirect_changed', updated=now() FROM oauth_providers p "
                     "WHERE c.provider_id=p.id AND p.redirect_mode='tunnel' AND p.redirect_base IS NOT NULL AND c.status='active' AND " + where_sql + " RETURNING c.id", args)

async def redirect_sweep():
    """At boot: AOS_PUBLIC_BASE differs from the address a tunnel provider was last registered for -> its connections need a reconnect (redirect_changed). Nothing is re-registered."""
    try: base = _env_base()
    except HTTPException: base = None
    if not base: return
    try: rows = await _mark_redirect_changed("p.redirect_base <> %s", (base,))
    except Exception as e:  # noqa: BLE001  (a sweep problem must never stop the server from booting)
        log.warning("oauth: redirect sweep failed (%s)", type(e).__name__); return
    if rows: log.warning("oauth: AOS_PUBLIC_BASE changed, %d connection(s) of tunnel providers need a reconnect (an admin must re-register the providers)", len(rows))

# ------------------------------------------------------------------ providers (admin)
GRANTS = ("authorization_code", "client_credentials")
RESERVED_AUTH = {"response_type", "client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method", "scope", "resource"}   # extra_params may never override these
SECRETISH = re.compile(r"secret|password|assertion|refresh_token|code_verifier|^code$|^grant_type$|^client_id$", re.I)                    # token_params is stored in the clear
BAD_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "upgrade", "te", "trailer", "content-type", "proxy-authorization"}

class ProviderIn(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]{1,39}$")
    display_name: str | None = None
    authorize_url: str = Field(default="", description="required for authorization_code; browser address, not called by the server")
    token_url: str = Field(pattern=r"^https?://")
    revoke_url: str | None = None
    userinfo_url: str | None = None
    api_base: str = Field(pattern=r"^https?://", description="Tool calls are only allowed to URLs under this prefix")
    client_id: str = Field(min_length=1, max_length=400)
    client_secret: str | None = Field(default=None, max_length=2000, description="write-only; omit on update to keep")
    scopes: str = ""
    extra_params: dict = {}
    token_auth: str = Field(default="body", pattern=r"^(body|post|basic|none)$")
    pkce: bool = True
    enabled: bool = True
    grant_type: Literal["authorization_code", "client_credentials"] = "authorization_code"
    issuer: str | None = Field(default=None, max_length=500, description="RFC 9207: the `iss` the authorization server must send back")
    iss_supported: bool = Field(default=False, description="the server always sends `iss`: a callback without it is refused")
    resource: str | None = Field(default=None, max_length=500, description="RFC 8707 resource indicator")
    token_params: dict = Field(default={}, description="extra fields of the token request (stored in the clear: no secrets)")
    token_headers: dict | None = Field(default=None, description="extra token-endpoint headers; write-only (sealed); omit on update to keep, {} clears")
    allowed_scopes: str | None = Field(default=None, max_length=2000, description="ceiling for requested scopes")
    default_redirect_uri: str | None = Field(default=None, max_length=1000, description="own redirect URI: https, or http on 127.0.0.1 / [::1]")
    redirect_mode: Literal["web", "loopback_paste", "tunnel"] = "web"

    @model_validator(mode="after")
    def _shape(self):
        if self.grant_type == "authorization_code" and not re.match(r"^https?://", self.authorize_url or ""): raise ValueError("authorize_url is required (http/https) for authorization_code")
        for k, v in (self.extra_params or {}).items():
            if not isinstance(k, str) or k.lower() in RESERVED_AUTH or not isinstance(v, (str, int, float, bool)): raise ValueError(f"extra_params: '{k}' is not allowed")
        for k, v in (self.token_params or {}).items():
            if not isinstance(k, str) or SECRETISH.search(k) or not isinstance(v, (str, int, float, bool)): raise ValueError(f"token_params: '{k}' is not allowed (secrets go in client_secret / token_headers)")
        for k, v in (self.token_headers or {}).items():
            if (not isinstance(k, str) or not re.fullmatch(r"[A-Za-z0-9\-]{1,64}", k) or k.lower() in BAD_HEADERS or not isinstance(v, str) or len(v) > 2000
                    or "\r" in v or "\n" in v): raise ValueError(f"token_headers: '{k}' is not allowed")
        return self

def _admin():
    if _u().get("role") != "admin": raise HTTPException(403, "Admin only")

def _pub(p):
    return {k: v for k, v in p.items() if k not in ("client_secret_enc", "token_headers_enc", "registration_token_enc")} | {
        "has_secret": bool(p.get("client_secret_enc")), "has_token_headers": bool(p.get("token_headers_enc")), "has_registration_token": bool(p.get("registration_token_enc"))}

def _scopes(s) -> list:
    return [x for x in re.split(r"[\s,]+", s or "") if x]

def _clip(want: str, ceiling: str | None) -> str:
    """Scopes of `want` that are inside `ceiling` (None = no ceiling). The original string is returned untouched when nothing was removed (some providers want commas)."""
    if ceiling is None: return want
    w, c = _scopes(want), set(_scopes(ceiling))
    keep = [x for x in w if x in c]
    return want if keep == w else " ".join(keep)

def _server_url(url, what):
    """URL of something THIS server will call: no credentials in it, and a literal private / metadata IP is refused already when the provider is saved
    (a host name is checked by netguard on every call, so DNS can never swap the target later)."""
    try: x = httpx.URL(url)
    except (httpx.InvalidURL, ValueError): raise HTTPException(422, f"{what}: not a valid URL") from None
    if x.scheme not in ("http", "https") or not x.host or x.userinfo or x.fragment: raise HTTPException(422, f"{what}: need an http(s) URL with a host and no credentials / fragment")
    try: ip_address(x.host)
    except ValueError: return url
    try: netguard.check(url, "oauth")
    except netguard.NetBlocked as e: raise HTTPException(422, f"{what}: address refused ({e.reason})") from None
    return url

def _redirect_ok(url):
    try: x = httpx.URL(url)
    except (httpx.InvalidURL, ValueError): raise HTTPException(422, "default_redirect_uri: not a valid URL") from None
    if x.userinfo or x.fragment or not x.host: raise HTTPException(422, "default_redirect_uri: no credentials / fragment, host required")
    if x.scheme == "https" or (x.scheme == "http" and x.host in ("127.0.0.1", "::1")): return url
    raise HTTPException(422, "default_redirect_uri: https, or http only on 127.0.0.1 / [::1]")

def _fields(b: ProviderIn, source="manual") -> dict:
    """Validated column values of a provider (no secret, no workspace)."""
    if b.resource and (not re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*:", b.resource) or "#" in b.resource): raise HTTPException(422, "resource: must be an absolute URI without a fragment")
    if b.issuer is not None and re.search(r"[\x00-\x20]", b.issuer): raise HTTPException(422, "issuer: no spaces / control characters")
    ceiling = b.allowed_scopes
    if ceiling is not None and set(_scopes(b.scopes)) - set(_scopes(ceiling)): raise HTTPException(422, "scopes exceed allowed_scopes")
    return dict(name=b.name, display_name=b.display_name or b.name, authorize_url=b.authorize_url or "", token_url=_server_url(b.token_url, "token_url"),
                revoke_url=_server_url(b.revoke_url, "revoke_url") if b.revoke_url else None, userinfo_url=_server_url(b.userinfo_url, "userinfo_url") if b.userinfo_url else None,
                api_base=_server_url(b.api_base, "api_base").rstrip("/") + "/", client_id=b.client_id, scopes=b.scopes, extra_params=Jsonb(b.extra_params or {}),
                token_auth=b.token_auth, pkce=b.pkce, enabled=b.enabled, grant_type=b.grant_type, issuer=b.issuer or None, iss_supported=b.iss_supported, resource=b.resource or None,
                token_params=Jsonb(b.token_params or {}), allowed_scopes=ceiling or None, default_redirect_uri=_redirect_ok(b.default_redirect_uri) if b.default_redirect_uri else None,
                redirect_mode=b.redirect_mode, source=source)

def _cc_data(p) -> dict:
    d = {"grant_type": "client_credentials"}
    if p.get("scopes"): d["scope"] = _clip(p["scopes"], p.get("allowed_scopes"))
    if p.get("resource"): d["resource"] = p["resource"]
    return d

@router.get("/api/oauth/providers")
async def prov_list():
    rows = await A.q("SELECT * FROM oauth_providers WHERE workspace_id=%s ORDER BY id", (_ws(),))
    return [_pub(r) for r in rows]

async def _create_provider(b: ProviderIn, source="manual"):
    ws = _ws()
    if await A.q("SELECT 1 FROM oauth_providers WHERE workspace_id=%s AND name=%s", (ws, b.name), one=True): raise HTTPException(409, "Provider name already exists")
    if await A.q("SELECT 1 FROM connections WHERE workspace_id=%s AND slug=%s AND kind='secret'", (ws, b.name), one=True): raise HTTPException(409, "A secret connection already uses this name")   # GĐ11.24
    f = _fields(b, source); first = None
    if b.grant_type == "client_credentials":   # fail = do not create: the token is fetched BEFORE any row exists
        if not b.client_secret and b.token_auth != "none": raise HTTPException(422, "client_secret is required for client_credentials")
        tmp = {**f, "id": 0, "workspace_id": ws, "client_secret_enc": None, "_secret_plain": b.client_secret, "_headers_plain": b.token_headers or {},
               "extra_params": b.extra_params or {}, "token_params": b.token_params or {}}
        first = await _token_call(tmp, _cc_data(tmp))
    cols = list(f)
    r = await A.q(f"INSERT INTO oauth_providers({','.join(cols)},workspace_id) VALUES({','.join(['%s'] * len(cols))},%s) RETURNING *", (*[f[c] for c in cols], ws), one=True)
    try:
        sets, args = [], []
        if b.client_secret: sets.append("client_secret_enc=%s"); args.append(enc(b.client_secret, "oauth_providers.client_secret_enc", ws, r["id"]))   # the row id is part of the AAD -> seal after the insert
        if b.token_headers: sets.append("token_headers_enc=%s"); args.append(enc(json.dumps(b.token_headers), "oauth_providers.token_headers_enc", ws, r["id"]))
        if sets: r = await A.q(f"UPDATE oauth_providers SET {','.join(sets)} WHERE id=%s RETURNING *", (*args, r["id"]), one=True)
        if first is not None:
            import connections
            await connections.save_tokens(("workspace", 0), r, first)
    except Exception:
        await A.q("DELETE FROM oauth_providers WHERE id=%s AND workspace_id=%s", (r["id"], ws)); raise   # nothing half-created
    await audit("provider.create", b.name, True, {"grant_type": b.grant_type, "source": source, "redirect_mode": b.redirect_mode}); return _pub(r)

@router.post("/api/oauth/providers")
async def prov_new(b: ProviderIn):
    _admin(); return await _create_provider(b)

@router.put("/api/oauth/providers/{pid}")
async def prov_upd(pid: int, b: ProviderIn):
    _admin(); ws = _ws()
    old = await A.q("SELECT * FROM oauth_providers WHERE id=%s AND workspace_id=%s", (pid, ws), one=True)
    if not old: raise HTTPException(404, "Provider not found")
    if b.grant_type != old["grant_type"]: raise HTTPException(422, "grant_type cannot be changed - create a new provider")
    f = _fields(b, old["source"]); first = None
    if old["grant_type"] == "client_credentials":   # the new settings must still get a token, else nothing changes
        tmp = {**old, **f, "extra_params": b.extra_params or {}, "token_params": b.token_params or {}}
        if b.client_secret: tmp["_secret_plain"] = b.client_secret
        if b.token_headers is not None: tmp["_headers_plain"] = b.token_headers
        first = await _token_call(tmp, _cc_data(tmp))
    sets, args = [f"{c}=%s" for c in f], list(f.values())
    if b.client_secret: sets.append("client_secret_enc=%s"); args.append(enc(b.client_secret, "oauth_providers.client_secret_enc", ws, pid))
    if b.token_headers is not None: sets.append("token_headers_enc=%s"); args.append(enc(json.dumps(b.token_headers), "oauth_providers.token_headers_enc", ws, pid) if b.token_headers else None)
    r = await A.q(f"UPDATE oauth_providers SET {','.join(sets)} WHERE id=%s AND workspace_id=%s RETURNING *", (*args, pid, ws), one=True)
    if first is not None:
        import connections
        await connections.save_tokens(("workspace", 0), r, first)
    await audit("provider.update", b.name); return _pub(r)

@router.delete("/api/oauth/providers/{pid}")
async def prov_del(pid: int):
    _admin()
    r = await A.q("DELETE FROM oauth_providers WHERE id=%s AND workspace_id=%s RETURNING name", (pid, _ws()), one=True)
    if not r: raise HTTPException(404, "Provider not found")
    await audit("provider.delete", r["name"]); return {"ok": True}

@router.post("/api/oauth/providers/{pid}/re-register")
async def prov_reregister(pid: int, request: Request):
    """Admin button after AOS_PUBLIC_BASE changed: the provider is told to use the NEW address. The admin updates the redirect URL at the provider's console / MCP registration by hand
    (no automatic re-registration, GĐ11 S1#22); users then reconnect once."""
    _admin()
    p = await A.q("SELECT * FROM oauth_providers WHERE id=%s AND workspace_id=%s", (pid, _ws()), one=True)
    if not p: raise HTTPException(404, "Provider not found")
    if p["redirect_mode"] != "tunnel": raise HTTPException(409, "Only providers with redirect_mode 'tunnel' have an address to re-register")
    ru, base = _redirect_for(p, request)
    if base is None: raise HTTPException(409, "This provider has its own default_redirect_uri")
    mode = "manual"
    if p.get("source") == "mcp":   # GĐ11.26: a provider made from an MCP server: dcr registers again, cimd takes the new document URL (both before anything is changed)
        import mcp_oauth
        mode = await mcp_oauth.reregister(p, ru, base)
    await A.q("UPDATE oauth_providers SET redirect_base=%s WHERE id=%s AND workspace_id=%s", (base, pid, _ws()))
    await audit("provider.re_register", p["name"], True, {"redirect_uri": ru, "registration": mode})
    return {"ok": True, "redirect_uri": ru, "registration": mode}

# ------------------------------------------------------------------ catalog (~25 services; creating from it needs only a client id + secret)
CATALOG_FILE = Path(__file__).with_name("oauth_catalog.json")
_CAT: list | None = None

def catalog() -> list:
    global _CAT
    if _CAT is None: _CAT = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))["services"]
    return _CAT

class CatalogIn(BaseModel):
    catalog_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]{1,39}$")
    client_id: str = Field(min_length=1, max_length=400)
    client_secret: str | None = Field(default=None, max_length=2000)
    name: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_\-]{1,39}$")
    scopes: str | None = None

@router.post("/api/oauth/providers/from-catalog")
async def prov_from_catalog(b: CatalogIn):
    _admin()
    e = next((x for x in catalog() if x["id"] == b.catalog_id), None)
    if not e: raise HTTPException(404, "Not in the catalog")
    keys = ("display_name", "authorize_url", "token_url", "revoke_url", "userinfo_url", "api_base", "scopes", "extra_params", "token_auth", "pkce", "redirect_mode", "issuer", "iss_supported", "resource")
    d = {k: e[k] for k in keys if k in e and e[k] is not None} | {"name": b.name or e["id"], "client_id": b.client_id, "client_secret": b.client_secret}
    if b.scopes is not None: d["scopes"] = b.scopes
    try: pi = ProviderIn.model_validate(d)
    except ValueError as ex: raise HTTPException(422, str(ex)[:300]) from None
    return await _create_provider(pi, "catalog")

# ------------------------------------------------------------------ connect flow (per user)
STATE_MINUTES = 10

def _pkce():
    v = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    c = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    return v, c

class ConnectIn(BaseModel):
    scopes: str | None = Field(default=None, max_length=2000, description="narrower scopes than the provider default; never wider than its allowed_scopes / scopes")
    owner: str = Field(default="me", description="me | agent:<id> | workspace (the last two: admin only)")

@router.post("/api/oauth/connect/{name}")
async def connect(name: str, request: Request, b: ConnectIn | None = None):
    import connections
    u = connections._caller(); ws = int(u.get("ws") or 1); b = b or ConnectIn()
    p = await A.q("SELECT * FROM oauth_providers WHERE workspace_id=%s AND name=%s AND enabled", (ws, name), one=True)
    if not p: raise HTTPException(404, "Provider not found or disabled")
    owner = await connections._owner_arg(b.owner, u)   # agent / workspace owners: admin only
    return await begin_flow(u, ws, p, owner, b.scopes, request)

async def begin_flow(u, ws, p, owner, scopes_req, request: Request, *, auth_id=None, next_url="/settings?s=connections"):
    """Start one authorization-code flow: a one-time state (PKCE verifier sealed) and the provider's authorize URL. Shared by POST /api/oauth/connect/{name} and the runtime
    sign-in link of a paused run (GĐ11.27: auth_id links the state to its auth_sessions row). The caller has already checked who may use `owner`."""
    name = p["name"]
    if p["grant_type"] != "authorization_code": raise HTTPException(409, "This provider uses client credentials: there is nothing for a person to sign in to")
    ru, base = _redirect_for(p, request)
    if base:   # tunnel: the address the app was registered for must still be this one
        if p["redirect_base"] and p["redirect_base"] != base:
            await _mark_redirect_changed("p.id=%s AND c.workspace_id=%s", (p["id"], ws))
            await audit("connect.start", name, False, {"reason": "redirect_changed"})
            raise HTTPException(409, "redirect_changed: địa chỉ công khai đã đổi, nhờ quản trị bấm 'Đăng ký lại' / the public address changed: ask an admin to re-register this provider")
        if not p["redirect_base"]: await A.q("UPDATE oauth_providers SET redirect_base=%s WHERE id=%s AND workspace_id=%s", (base, p["id"], ws))
    ceiling = p["allowed_scopes"] if p["allowed_scopes"] is not None else p["scopes"]   # a member may narrow the scopes, never widen them
    scope = _clip(scopes_req if scopes_req is not None else p["scopes"], ceiling)
    state = secrets.token_urlsafe(32); v, ch = _pkce()
    await A.q("""INSERT INTO oauth_states(state,provider_id,user_id,verifier_enc,next,expires,owner_type,owner_id,workspace_id,issuer,resource,scope,redirect_uri,auth_id)
                 VALUES(%s,%s,%s,%s,%s,now()+make_interval(mins=>%s),%s,%s,%s,%s,%s,%s,%s,%s)""",
              (state, p["id"], u["id"], enc(v, "oauth_states.verifier_enc", ws, state), next_url, STATE_MINUTES, owner[0], owner[1], ws, p["issuer"], p["resource"], scope, ru, auth_id))
    q = {**{k: v for k, v in (p["extra_params"] or {}).items() if k.lower() not in RESERVED_AUTH}, "response_type": "code", "client_id": p["client_id"], "redirect_uri": ru, "state": state}
    if scope: q["scope"] = scope
    if p["resource"]: q["resource"] = p["resource"]
    if p["pkce"]: q.update(code_challenge=ch, code_challenge_method="S256")
    await audit("connect.start", name, True, {"owner": f"{owner[0]}:{owner[1]}", "redirect_mode": p["redirect_mode"]})
    return {"authorize_url": p["authorize_url"] + ("&" if "?" in p["authorize_url"] else "?") + urllib.parse.urlencode(q), "redirect_uri": ru,
            "redirect_mode": p["redirect_mode"], "paste": p["redirect_mode"] == "loopback_paste" or bool(p["default_redirect_uri"]), "state_minutes": STATE_MINUTES}

class OAuthError(HTTPException):
    """A token-endpoint failure. `code` = the OAuth error (invalid_grant ...) or unreachable / egress_blocked. 400 = the server said no, 502 = we could not ask / it is broken."""
    def __init__(self, status, detail, code=None): super().__init__(status, detail); self.code = code

def _client_auth(p, data):
    """RFC 6749 §2.3 client authentication per token_auth: body|post (secret in the form), basic (HTTP Basic), none (public client: client_id only, a secret is never sent)."""
    data = dict(data); secret = None if p["token_auth"] == "none" else _sec(p); auth = None
    if p["token_auth"] == "basic" and secret: auth = (p["client_id"], secret)
    else:
        data["client_id"] = p["client_id"]
        if secret: data["client_secret"] = secret
    return data, auth

async def _token_call(p, data):
    data, auth = _client_auth(p, {**(p.get("token_params") or {}), **data})   # the flow's own fields win over token_params
    headers = {**_token_headers(p), "Accept": "application/json"}
    try:
        async with netguard.client("oauth", timeout=20) as h:
            r = await h.post(p["token_url"], data=data, auth=auth, headers=headers)
    except netguard.NetBlocked as e: raise OAuthError(502, f"Token endpoint refused by the network policy: {e.reason}", "egress_blocked") from None
    except httpx.HTTPError as e: raise OAuthError(502, f"Token endpoint unreachable ({type(e).__name__})", "unreachable") from None
    try: j = r.json()
    except Exception: j = dict(urllib.parse.parse_qsl(r.text))  # noqa: BLE001  (GitHub legacy form encoding)
    if not isinstance(j, dict): j = {}
    if r.status_code >= 400 or "access_token" not in j:
        err = str(j.get("error", r.status_code))[:60]
        raise OAuthError(502 if r.status_code >= 500 else 400, f"Token endpoint error: {err} {str(j.get('error_description', ''))[:200]}", err)
    return j

async def _save_tokens(p, user, j, account=None, owner=None):
    """GĐ11.24: stored by connections.save_tokens (conflict target UNIQUE(provider_id, owner_type, owner_id)); owner defaults to the user who connected."""
    import connections
    await connections.save_tokens(owner or ("user", user["id"]), p, j, account)

class FlowError(Exception):
    def __init__(self, msg, status=400, reason=None, provider=None): super().__init__(msg); self.msg, self.status, self.reason, self.provider = msg, status, reason or msg, provider

async def _take_state(u, state):
    """The one-time state: removed from the table by the user it was issued to (someone else's attempt neither uses nor burns it); None if absent / foreign / expired."""
    if not state or len(state) > 200 or not u.get("id"): return None
    st = await A.q("DELETE FROM oauth_states WHERE state=%s AND user_id=%s RETURNING *", (state, u["id"]), one=True)
    return st if st and st["expires"].timestamp() >= time.time() else None

async def session_mark(st, status):
    """GĐ11.27: the runtime sign-in (auth_sessions) a one-time state belongs to is finished -> the chat card's long poll sees it. Only a still-pending session of the same person / workspace changes."""
    if not st.get("auth_id"): return
    try:
        await A.q("UPDATE auth_sessions SET status=%s, completed_at=now() WHERE auth_id=%s AND workspace_id=%s AND user_id=%s AND status='pending'",
                  (status, st["auth_id"], st["workspace_id"], st["user_id"]))
    except Exception as e: log.warning("auth session not marked (%s)", type(e).__name__)   # noqa: BLE001

async def _complete(u, st, *, code="", error="", error_description="", iss="", fallback_ru=None):
    """Finish a flow whose state was taken: workspace / provider / owner re-checks, `iss`, code -> tokens -> saved. Shared by GET /oauth/callback and POST /api/oauth/paste-callback."""
    import admin
    if admin.is_demo(u): raise FlowError("Connected accounts are not available for demo accounts", 403, "demo")
    ws = int(u.get("ws") or 1)
    if st.get("workspace_id") is not None and st["workspace_id"] != ws: raise FlowError("Provider not available in this workspace. Start the connection again.", 400, "state of another workspace")
    p = await A.q("SELECT * FROM oauth_providers WHERE id=%s", (st["provider_id"],), one=True)
    if not p or p["workspace_id"] != ws:   # GĐ11.24: tokens are stored under the provider's workspace -> the signed-in workspace must be that one
        raise FlowError("Provider not available in this workspace. Start the connection again.", 400, "provider missing or in another workspace")
    if not p["enabled"] or p["grant_type"] != "authorization_code": raise FlowError("Provider not available. Start the connection again.", 400, "provider disabled or not authorization_code", p["name"])
    new = st.get("workspace_id") is not None   # states made before 0022 carry no snapshot: use the provider's current values
    snap = lambda k: st[k] if new else p.get(k)   # noqa: E731
    owner = (st.get("owner_type") or "user", st["owner_id"] if st.get("owner_id") is not None else st["user_id"])
    if owner != ("user", u["id"]):   # an account for an agent / the workspace: only an admin may complete it, and the agent must still be in this workspace
        if u.get("role") != "admin": raise FlowError("Admin only", 403, "owner needs admin", p["name"])
        if owner[0] == "agent" and not await A.q("SELECT 1 FROM agents WHERE id=%s AND workspace_id=%s", (owner[1], ws), one=True): raise FlowError("Agent not found", 404, "agent not in workspace", p["name"])
    expected = snap("issuer")   # RFC 9207: checked before anything else the response says, errors included
    if iss and expected and iss != expected: raise FlowError("The authorization server identified itself with an unexpected issuer. Nothing was connected.", 400, "iss mismatch", p["name"])
    if p["iss_supported"] and (not iss or not expected): raise FlowError("The authorization server did not prove its identity (iss). Nothing was connected.", 400, "iss missing", p["name"])
    if error or not code: raise FlowError(f"OAuth connection was not completed: {error or 'no code'}", 400, f"provider error: {error or 'no code'} {error_description[:200]}", p["name"])
    ru = st.get("redirect_uri") or fallback_ru
    if not ru: raise FlowError("Redirect address unknown. Start the connection again.", 400, "no redirect_uri", p["name"])
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": ru}
    if p["pkce"]: data["code_verifier"] = dec(st["verifier_enc"], "oauth_states.verifier_enc", p["workspace_id"], st["state"])
    if snap("resource"): data["resource"] = snap("resource")
    try: j = await _token_call(p, data)
    except HTTPException as e: raise FlowError(str(e.detail), 400, f"token: {e.detail}", p["name"]) from None
    account = None
    if p["userinfo_url"]:
        try:
            async with netguard.client("oauth", timeout=10) as h:
                ui = (await h.get(p["userinfo_url"], headers={"Authorization": f"Bearer {j['access_token']}", "Accept": "application/json"})).json()
            account = str(ui.get("email") or ui.get("login") or ui.get("preferred_username") or ui.get("name") or ui.get("sub") or "")[:200] or None
        except Exception: pass  # noqa: BLE001  (an unreachable / blocked userinfo endpoint only costs the account label)
    await _save_tokens(p, u, j, account, owner=owner)
    await session_mark(st, "completed")
    await audit("connect.success", p["name"], True, {"account": account, "scope": j.get("scope"), "owner": f"{owner[0]}:{owner[1]}"})
    return p, account, st["next"]

def _page(title, msg, status):
    return HTMLResponse(f"<h3>{html.escape(title)}</h3><p>{html.escape(str(msg))}</p>", status)   # the message may echo provider / query text: never raw into HTML

@router.get("/oauth/callback")
async def callback(request: Request, state: str = "", code: str = "", error: str = "", error_description: str = "", iss: str = ""):
    u = _u()
    st = await _take_state(u, state)   # one-time use
    if not st:
        await audit("connect.callback", None, False, {"reason": "invalid, expired or foreign state"})
        return _page("OAuth connection failed", "Invalid or expired state. Start the connection again.", 400)
    fb = None if st.get("redirect_uri") else _public_base(request) + "/oauth/callback"   # a state made before migration 0022 has no stored redirect address
    try: _p, _acc, nxt = await _complete(u, st, code=code, error=error, error_description=error_description, iss=iss, fallback_ru=fb)
    except FlowError as e:
        await audit("connect.callback", e.provider, False, {"reason": e.reason[:300]})
        await session_mark(st, "failed")
        return _page("OAuth connection failed" if not error else "OAuth connection was not completed", e.msg, e.status)
    return RedirectResponse(nxt or "/settings?s=connections", 303)

class PasteIn(BaseModel):
    url: str = Field(min_length=1, max_length=4000, description="the address of the page the browser could not open after the sign-in")

@router.post("/api/oauth/paste-callback")
async def paste_callback(b: PasteIn):
    """redirect_mode loopback_paste: the user pastes the address the browser ended on (http://127.0.0.1:<port>/oauth/callback?code=...&state=...). Same checks and exchange as the callback."""
    import connections
    u = connections._caller()
    try: x = urllib.parse.urlsplit(b.url.strip()); q = dict(urllib.parse.parse_qsl(x.query))
    except ValueError: raise HTTPException(400, "That is not an address") from None
    st = await _take_state(u, q.get("state", ""))
    if not st:
        await audit("connect.paste", None, False, {"reason": "invalid, expired or foreign state"})
        raise HTTPException(400, "Invalid or expired state. Start the connection again.")
    if st.get("redirect_uri"):   # the pasted page must be the redirect we sent, not any link that happens to carry a state
        want = urllib.parse.urlsplit(st["redirect_uri"])
        if (x.scheme, x.netloc.lower(), x.path.rstrip("/")) != (want.scheme, want.netloc.lower(), want.path.rstrip("/")):
            await audit("connect.paste", None, False, {"reason": "address is not the redirect address"})
            raise HTTPException(400, "That address is not the one this sign-in returns to. Start the connection again.")
    try: p, account, _nxt = await _complete(u, st, code=q.get("code", ""), error=q.get("error", ""), error_description=q.get("error_description", ""), iss=q.get("iss", ""))
    except FlowError as e:
        await audit("connect.paste", e.provider, False, {"reason": e.reason[:300]})
        await session_mark(st, "failed")
        raise HTTPException(e.status, e.msg) from None
    return {"ok": True, "provider": p["name"], "account": account}

@router.get("/api/oauth/connections")
async def conn_list():
    u = _u()
    return await A.q("""SELECT c.id, p.name provider, p.display_name, c.account, c.scope, c.status, c.expires_at, c.created, c.updated, c.last_used, c.last_refresh,
        (c.refresh_token_enc IS NOT NULL) has_refresh FROM oauth_connections c JOIN oauth_providers p ON p.id=c.provider_id
        WHERE c.owner_type='user' AND c.kind='oauth' AND c.user_id=%s AND c.workspace_id=%s ORDER BY c.id""", (u.get("id"), _ws()))   # GĐ11.24: read-only alias of GET /api/connections?owner=me

async def _refresh(c, p, user, reason="auto"):
    """refresh_token grant (a rotated refresh token replaces the old one in save_tokens); client_credentials providers simply fetch a new token. A definitive refusal
    (invalid_grant & co) -> needs_reconnect; we could not ask (network, 5xx) -> the status is left alone; blocked by the egress policy -> status error (a config problem)."""
    cc = p.get("grant_type") == "client_credentials"
    if not cc and not c["refresh_token_enc"]: raise HTTPException(401, f"{p['name']}: token expired and no refresh token — reconnect the account")
    data = _cc_data(p) if cc else {"grant_type": "refresh_token", "refresh_token": _tok(c, "refresh_token_enc"), **({"resource": p["resource"]} if p.get("resource") else {})}
    try: j = await _token_call(p, data)
    except HTTPException as e:
        code = getattr(e, "code", None)
        if code == "egress_blocked": st, why = "error", "egress_blocked"
        elif code == "unreachable" or e.status_code >= 500: st, why = None, None
        else: st, why = "needs_reconnect", "refresh_failed"
        if st: await A.q("UPDATE oauth_connections SET status=%s, status_reason=%s, updated=now() WHERE id=%s AND workspace_id=%s", (st, why, c["id"], c["workspace_id"]))
        await audit("token.refresh", p["name"], False, {"reason": reason, "error": e.detail, "code": code}, user=user); raise
    await _save_tokens(p, user, j, owner=(c["owner_type"], c["owner_id"] if c["owner_id"] is not None else c["user_id"]))   # GĐ11.24: refresh in place, whoever the owner is
    await audit("token.refresh", p["name"], True, {"reason": reason}, user=user)
    return j["access_token"]

async def get_token(provider_name, user, agent_id=None, force_refresh=False):
    """Compat wrapper (GĐ11.24): -> (provider dict, access token). The logic lives in connections.get_token (owner resolution, refresh under the lock, scopes).
    Raises 401 if not connected, 403 for a channel run (GĐ8.Z) / demo."""
    import connections
    owner, cu = await connections.resolve(user, agent_id, provider_name)
    t = await connections.get_token(owner, provider_name, ws=(user.get("ws") or 1), force_refresh=force_refresh, user=cu)
    return {"id": t.provider_id, "name": t.provider, "display_name": t.display_name, "api_base": t.api_base}, t.value

@router.post("/api/oauth/connections/{cid}/refresh")
async def conn_refresh(cid: int):
    u = _u(); c = await A.q("SELECT * FROM oauth_connections WHERE id=%s AND owner_type='user' AND kind='oauth' AND user_id=%s AND workspace_id=%s", (cid, u.get("id"), _ws()), one=True)
    if not c: raise HTTPException(404, "Connection not found")
    p = await A.q("SELECT * FROM oauth_providers WHERE id=%s AND workspace_id=%s", (c["provider_id"], _ws()), one=True)
    async with LOCKS.setdefault((p["id"], "user", u["id"]), asyncio.Lock()): await _refresh(c, p, u, "manual")
    return {"ok": True}

async def revoke_remote(c, p):
    """RFC 7009 revocation of a stored credential at the provider (best effort). None = provider has no revoke_url; True/False = all calls ok / some failed."""
    if not p["revoke_url"]: return None
    revoked_remote = True
    for tok, hint in ((c["refresh_token_enc"], "refresh_token"), (c["access_token_enc"], "access_token")):
        if not tok: continue
        try:
            data, auth = _client_auth(p, {"token": _tok(c, hint + "_enc"), "token_type_hint": hint})
            async with netguard.client("oauth", timeout=15) as h:
                r = await h.post(p["revoke_url"], data=data, auth=auth)
            if r.status_code >= 400: revoked_remote = False
        except Exception: revoked_remote = False  # noqa: BLE001
    return revoked_remote

@router.delete("/api/oauth/connections/{cid}")
async def conn_revoke(cid: int):
    u = _u(); c = await A.q("SELECT * FROM oauth_connections WHERE id=%s AND owner_type='user' AND kind='oauth' AND user_id=%s AND workspace_id=%s", (cid, u.get("id"), _ws()), one=True)
    if not c: raise HTTPException(404, "Connection not found")
    p = await A.q("SELECT * FROM oauth_providers WHERE id=%s AND workspace_id=%s", (c["provider_id"], _ws()), one=True)
    revoked_remote = await revoke_remote(c, p)
    await A.q("DELETE FROM oauth_connections WHERE id=%s AND workspace_id=%s", (cid, _ws()))
    await audit("connection.revoke", p["name"], True, {"remote_revoked": revoked_remote})
    return {"ok": True, "remote_revoked": revoked_remote}

@router.get("/api/oauth/audit")
async def audit_list(limit: int = 100):
    u = _u()
    if u.get("role") == "admin":
        return await A.q("SELECT a.*, u.username FROM oauth_audit a LEFT JOIN users u ON u.id=a.user_id WHERE a.workspace_id=%s ORDER BY a.id DESC LIMIT %s", (_ws(), min(limit, 1000)))
    return await A.q("SELECT a.*, NULL username FROM oauth_audit a WHERE a.user_id=%s ORDER BY a.id DESC LIMIT %s", (u.get("id"), min(limit, 1000)))

# ------------------------------------------------------------------ agent tool helper
async def tool_request(provider, method, url, body=None, agent_id=None):
    import admin, connections
    user = admin.cur()
    if not user or not user.get("id"): return "Error: no signed-in caller for this run, so no OAuth token can be used."
    caller_user, owner = user, None
    try:   # GĐ11.24: ONE entry point -- owner = the agent's own connection when present, else the caller's (or the agent owner's, identity mode 'agent'); demo/channel refused
        owner, user = await connections.resolve(user, agent_id, provider)
        t = await connections.get_token(owner, provider, ws=admin.cur_ws(), user=user)
    except connections.CredentialRequired as e:   # GĐ11.27: not connected -> the Fleet auth_required JSON (a sign-in link), not an "Error:" sentence
        await audit("tool.request", provider, False, {"error": e.detail, "url": url[:200]}, user=user, agent_id=agent_id)
        if owner is None: return f"Error: {e.detail}"
        import connection_runtime
        return await connection_runtime.needs_connection(e, caller_user, admin.cur_ws(), owner, provider)
    except HTTPException as e:
        await audit("tool.request", provider, False, {"error": e.detail, "url": url[:200]}, user=user, agent_id=agent_id); return f"Error: {e.detail}"
    p, tok = {"name": t.provider, "api_base": t.api_base}, t.value
    if t.kind != "oauth": return f"Error: '{provider}' is a stored secret, not an OAuth provider."
    if not url.startswith("http"): url = p["api_base"] + url.lstrip("/")
    if not url.startswith(p["api_base"]):
        await audit("tool.request", provider, False, {"error": "url outside api_base", "url": url[:200]}, user=user, agent_id=agent_id)
        return f"Error: URL must start with {p['api_base']} for provider {provider}."
    m = (method or "GET").upper()
    if m not in ("GET", "POST", "PUT", "PATCH", "DELETE"): return "Error: unsupported method"
    if m != "GET" and provider in ("google", "microsoft"):   # GĐ8.5: writes to Google / M365 only through the google:*/ms365:* tools (always approval-gated)
        return "Error: oauth_request is read-only (GET) for google/microsoft — use the google:* / ms365:* tools, which ask the user for approval."
    try:
        async with netguard.client("oauth", timeout=20) as h:   # GĐ11.25: the token leaves only to a public / allow-listed address, https, no redirect
            r = await h.request(m, url, headers={"Authorization": f"Bearer {tok}", "Accept": "application/json", "User-Agent": "AgentOS"},
                                json=body if body not in (None, "", {}) else None)
    except httpx.HTTPError as e:
        await audit("tool.request", provider, False, {"error": type(e).__name__, "url": url[:200], "method": m}, user=user, agent_id=agent_id); return f"Error: {type(e).__name__}"
    await audit("tool.request", provider, r.status_code < 400, {"url": url[:200], "method": m, "status": r.status_code}, user=user, agent_id=agent_id)
    return f"HTTP {r.status_code}\n" + r.text[:6000]

# ------------------------------------------------------------------ mock OAuth 2.0 server (tests; AOS_MOCK_OAUTH=1)
MOCK = {"codes": {}, "tokens": {}, "refresh": {}}
MOCK_CLIENT = ("agentos-mock-client", "mock-secret")

def _mock_on():
    if os.environ.get("AOS_MOCK_OAUTH") != "1": raise HTTPException(404)

@router.get("/mock-oauth/authorize")
async def mock_authorize(response_type: str = "", client_id: str = "", redirect_uri: str = "", state: str = "", scope: str = "",
                         code_challenge: str = "", code_challenge_method: str = "", deny: int = 0):
    _mock_on()
    if response_type != "code" or client_id != MOCK_CLIENT[0]: return JSONResponse({"error": "unauthorized_client"}, 400)
    if deny: return RedirectResponse(redirect_uri + "?" + urllib.parse.urlencode({"error": "access_denied", "state": state}), 302)
    code = secrets.token_urlsafe(16)
    MOCK["codes"][code] = {"redirect_uri": redirect_uri, "challenge": code_challenge, "method": code_challenge_method, "scope": scope, "exp": time.time() + 60, "sub": "mock-user-42"}
    return RedirectResponse(redirect_uri + "?" + urllib.parse.urlencode({"code": code, "state": state}), 302)

def _mock_issue(sub, scope, ttl):
    at, rt = "mat_" + secrets.token_urlsafe(18), "mrt_" + secrets.token_urlsafe(18)
    MOCK["tokens"][at] = {"sub": sub, "scope": scope, "exp": time.time() + ttl}; MOCK["refresh"][rt] = {"sub": sub, "scope": scope}
    return {"access_token": at, "refresh_token": rt, "token_type": "Bearer", "expires_in": ttl, "scope": scope}

@router.post("/mock-oauth/token")
async def mock_token(grant_type: str = Form(""), code: str = Form(""), redirect_uri: str = Form(""), code_verifier: str = Form(""),
                     refresh_token: str = Form(""), client_id: str = Form(""), client_secret: str = Form(""), ttl: int = Form(3600)):
    _mock_on()
    if (client_id, client_secret) != MOCK_CLIENT: return JSONResponse({"error": "invalid_client"}, 401)
    if grant_type == "authorization_code":
        c = MOCK["codes"].pop(code, None)
        if not c or c["exp"] < time.time() or c["redirect_uri"] != redirect_uri: return JSONResponse({"error": "invalid_grant"}, 400)
        if c["challenge"]:
            calc = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(b"=").decode()
            if c["method"] != "S256" or calc != c["challenge"]: return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, 400)
        return _mock_issue(c["sub"], c["scope"], int(os.environ.get("AOS_MOCK_OAUTH_TTL", "3600")))
    if grant_type == "refresh_token":
        r = MOCK["refresh"].pop(refresh_token, None)   # rotating refresh tokens
        if not r: return JSONResponse({"error": "invalid_grant"}, 400)
        return _mock_issue(r["sub"], r["scope"], int(os.environ.get("AOS_MOCK_OAUTH_TTL", "3600")))
    return JSONResponse({"error": "unsupported_grant_type"}, 400)

@router.post("/mock-oauth/revoke")
async def mock_revoke(token: str = Form(""), client_id: str = Form(""), client_secret: str = Form("")):
    _mock_on()
    MOCK["tokens"].pop(token, None); MOCK["refresh"].pop(token, None); return {}

def _mock_bearer(request: Request):
    a = request.headers.get("authorization", "")
    t = MOCK["tokens"].get(a[7:]) if a.lower().startswith("bearer ") else None
    if not t or t["exp"] < time.time(): raise HTTPException(401, "invalid_token")
    return t

@router.get("/mock-oauth/userinfo")
async def mock_userinfo(request: Request):
    _mock_on(); t = _mock_bearer(request); return {"sub": t["sub"], "email": "mock.user@example.test", "name": "Mock User"}

@router.api_route("/mock-oauth/api/{path:path}", methods=["GET", "POST"])
async def mock_api(path: str, request: Request):
    _mock_on(); t = _mock_bearer(request)
    if path == "me": return {"sub": t["sub"], "scope": t["scope"], "projects": ["alpha", "beta"]}
    if path == "echo": return {"sub": t["sub"], "body": (await request.json()) if request.method == "POST" else None}
    raise HTTPException(404)

@router.get("/mock-oauth/_debug/active")
async def mock_debug():
    _mock_on(); return {"access_tokens": len(MOCK["tokens"]), "refresh_tokens": len(MOCK["refresh"])}
