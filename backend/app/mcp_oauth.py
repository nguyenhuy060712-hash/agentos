"""GĐ11.26 MCP OAuth: probe, protected-resource metadata (PRM), client registration (CIMD first, DCR fallback, manual client last). Design: GD11A_THIET_KE §5.10.
The standard parts come from the official `mcp` 2.3 client helpers (mcp.client.auth.utils: WWW-Authenticate parsing, discovery URL lists, metadata / registration response handling,
issuer check, scope choice, CIMD decision); this file adds only what the SDK does not do for a headless server: every request goes through netguard, answers are size-capped,
the result lands in the database (oauth_providers source='mcp' + connections kind='mcp_oauth' + mcp_servers.auth_type='oauth'), and the admin API.
- probe(url) is ONE unauthenticated request to the MCP URL (+ at most MAX_HOPS metadata fetches): 401 -> `WWW-Authenticate` resource_metadata -> PRM (RFC 9728, header URL first, then
  the .well-known URLs; its `resource` must cover the MCP URL) -> authorization-server metadata (RFC 8414 then OIDC; its `issuer` must equal the advertised one, else REJECT;
  PKCE S256 must be advertised, else REJECT). The MCP URL uses netguard purpose "mcp", every other hop purpose "oauth": https only (http only to AOS_EGRESS_ALLOW addresses),
  private / metadata addresses refused, no redirect followed, so a hostile PRM can not point us at an internal address (a block anywhere = the whole probe is refused).
- Registration order: a provider the admin names (`provider`, adopted when its endpoints are the discovered ones) > client typed by the admin (`client_id`) > CIMD (only when
  AOS_PUBLIC_BASE is a stable https address; we serve GET /oauth/client-metadata.json, the client id IS that URL) > DCR (RFC 7591, application_type web, redirect from
  oauth_tools._public_base) > nothing: 409 `manual_registration_required` ("Máy chủ cần ứng dụng đăng ký sẵn") and no row is created.
- Result: oauth_providers row `mcp-<server>` (source='mcp', registration dcr|cimd|manual|preregistered, issuer / resource / iss_supported for the RFC 9207 + RFC 8707 checks of the
  11.25 engine), a `connections` row (kind mcp_oauth, mcp_url) and the mcp_servers row (auth_type 'oauth', connection_id). Any failure after the first write undoes the earlier writes
  (and de-registers the DCR client, best effort): no half-created rows. Client secrets / registration tokens are sealed (crypto.seal) and never returned.
- Known residual risk (CHO_ANH.md security package): fastmcp 4.1.3 talks to the MCP server with httpx2, outside netguard's pinned transport. netguard.check is done before every use,
  but a name that resolves to a public address at the check and to an internal one a moment later (DNS rebinding) is not stopped on that path. Probe / registration / token calls ARE pinned."""
import json, logging, re
from dataclasses import dataclass, field

import httpx, httpx2
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from mcp.client.auth import OAuthFlowError, OAuthRegistrationError
from mcp.client.auth.utils import (build_oauth_authorization_server_metadata_discovery_urls, build_protected_resource_metadata_discovery_urls,
                                   create_client_registration_request, extract_resource_metadata_from_www_auth, extract_scope_from_www_auth, get_client_metadata_scopes,
                                   handle_auth_metadata_response, handle_protected_resource_response, handle_registration_response, is_valid_client_metadata_url,
                                   issuers_match, should_use_client_metadata_url, validate_metadata_issuer)
from mcp.shared.auth import OAuthClientMetadata
from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url
from psycopg.errors import UniqueViolation

import netguard
import deepagent_api as A
import oauth_tools as O

log = logging.getLogger("aos.mcp_oauth")
router = APIRouter(tags=["mcp-oauth"])
MAX_HOPS = 5            # metadata documents fetched per probe (PRM + authorization-server metadata together)
MAX_BODY = 256 * 1024   # a metadata / registration answer larger than this is refused
TIMEOUT = 10
CIMD_PATH = "/oauth/client-metadata.json"
NAME_RE = r"^[a-z0-9_-]{2,30}$"
GRANTS = ["authorization_code", "refresh_token"]
MSG_MANUAL = "Máy chủ cần ứng dụng đăng ký sẵn / This server needs a pre-registered app"

class ProbeError(Exception):
    """A discovery / registration problem the admin must see. `code` is machine readable, `msg` is Vietnamese / English; `status` the HTTP status the API answers."""
    def __init__(self, code, msg, status=422): super().__init__(msg); self.code, self.msg, self.status = code, msg, status

def _http(e: ProbeError): return HTTPException(e.status, {"error": e.code, "message": e.msg})

# ------------------------------------------------------------------ http (netguard, size-capped, httpx2 responses for the SDK helpers)
def _hdrs(r): return [(k, v) for k, v in r.headers.multi_items() if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")]

async def _stream(h, method, url, *, cap, strict=True, headers=None, content=None):
    async with h.stream(method, url, headers=headers, content=content) as r:
        buf = b""
        try:
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) > cap:
                    if strict: raise ProbeError("too_large", "Câu trả lời quá lớn / The answer is too large")
                    buf = buf[:cap]; break
        except httpx.TimeoutException:
            if strict: raise
        return httpx2.Response(r.status_code, headers=_hdrs(r), content=buf)

async def _call(h, method, url, *, skip_redirect=False, **kw):
    """One request: -> httpx2.Response, None when the host can not be reached. A netguard refusal is a hard error (never 'not found'); a redirect is never followed:
    for a metadata candidate (skip_redirect) it means 'not served here, try the next one', anywhere else it is reported."""
    try: return await _stream(h, method, url, **kw)
    except netguard.NetBlocked as e:
        if e.reason == "redirect not followed":
            if skip_redirect: return None
            raise ProbeError("redirect", "Máy chủ chuyển hướng sang địa chỉ khác (không tự theo): hãy nhập địa chỉ cuối cùng / The server redirects (not followed): enter the final address") from None
        raise ProbeError("blocked", f"Địa chỉ bị chặn bởi chính sách mạng / Address refused by the network policy: {e.reason}") from None
    except httpx.HTTPError: return None

async def _guard(url):
    try: await netguard.acheck(url, "oauth")
    except netguard.NetBlocked as e: raise ProbeError("blocked", f"Địa chỉ bị chặn bởi chính sách mạng / Address refused by the network policy: {e.reason}") from None

# ------------------------------------------------------------------ discovery
def _cimd_base():
    """The stable https address (AOS_PUBLIC_BASE, never a header) the client-id metadata document may live at; None otherwise."""
    try: b = O._env_base()
    except HTTPException: return None
    return b if b and b.startswith("https://") else None

@dataclass
class Disc:
    url: str
    canonical: str
    reachable: bool = False
    status: int | None = None
    needs_auth: bool = False
    www: str | None = None
    www_scope: str | None = None
    prm: object = None
    md: object = None
    scopes: list = field(default_factory=list)
    hops: int = 0
    cimd_url: str | None = None
    resource: str = ""

    @property
    def registration(self) -> str:
        if self.md is None: return "none"
        if should_use_client_metadata_url(self.md, self.cimd_url): return "cimd"
        return "dcr" if self.md.registration_endpoint else "none"

    @property
    def auth_server(self):
        if self.md is not None: return str(self.md.issuer)
        return str(self.prm.authorization_servers[0]) if self.prm is not None else None

    def public(self) -> dict:
        md = self.md
        return {"url": self.url, "reachable": self.reachable, "status": self.status, "needs_auth": self.needs_auth, "auth_server": self.auth_server,
                "registration": self.registration if self.needs_auth else "none", "cimd_supported": bool(md and md.client_id_metadata_document_supported),
                "dcr_supported": bool(md and md.registration_endpoint), "scopes": self.scopes, "public_ok": self.cimd_url is not None, "resource": self.resource or None,
                "iss_supported": bool(md and md.authorization_response_iss_parameter_supported), "pkce_s256": bool(md and "S256" in (md.code_challenge_methods_supported or [])),
                "manual_required": self.needs_auth and self.registration == "none"}

def _spend(d: Disc) -> bool:
    if d.hops >= MAX_HOPS: return False
    d.hops += 1; return True

def _check_url(url):
    try: x = httpx.URL(url)
    except (httpx.InvalidURL, ValueError): raise ProbeError("bad_url", "Địa chỉ không hợp lệ / Not a valid URL") from None
    if x.scheme not in ("http", "https") or not x.host or x.userinfo or x.fragment:
        raise ProbeError("bad_url", "Cần địa chỉ http(s) có tên máy, không kèm tài khoản hay # / Need an http(s) URL with a host, no credentials or fragment")

async def _probe_mcp(h, d: Disc):
    from mcp_types import LATEST_PROTOCOL_VERSION
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": LATEST_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "agentos-probe", "version": "1"}}}
    r = await _call(h, "POST", d.url, cap=2048, strict=False, content=json.dumps(init).encode(), headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"})
    if r is None: return
    if r.status_code in (404, 405, 406):   # an SSE-only server: the stream endpoint answers GET
        g = await _call(h, "GET", d.url, cap=2048, strict=False, headers={"Accept": "text/event-stream"})
        if g is not None: r = g
    d.reachable, d.status = True, r.status_code
    if r.status_code == 401:
        d.needs_auth = True
        d.www = extract_resource_metadata_from_www_auth(r); d.www_scope = extract_scope_from_www_auth(r)

async def _prm(h, d: Disc):
    for u in build_protected_resource_metadata_discovery_urls(d.www, d.url):
        if not _spend(d): return
        r = await _call(h, "GET", u, cap=MAX_BODY, skip_redirect=True)
        prm = await handle_protected_resource_response(r) if r is not None else None
        if prm is None: continue
        if not check_resource_allowed(d.canonical, str(prm.resource)):   # RFC 9728 §3.3: the document must be about THIS server
            raise ProbeError("resource_mismatch", "Tài liệu PRM nói về máy chủ khác / The protected-resource document is about another server")
        d.prm = prm; d.resource = str(prm.resource); return

async def _as_metadata(h, d: Disc):
    as_url = str(d.prm.authorization_servers[0]) if d.prm is not None else None
    p = httpx.URL(d.url); expected = as_url or f"{p.scheme}://{p.netloc.decode()}"
    for u in build_oauth_authorization_server_metadata_discovery_urls(as_url, d.url):
        if not _spend(d): return
        r = await _call(h, "GET", u, cap=MAX_BODY, skip_redirect=True)
        if r is None: continue
        cont, md = await handle_auth_metadata_response(r)
        if md is not None:
            try: validate_metadata_issuer(md, expected)
            except OAuthFlowError:
                if not issuers_match(str(md.issuer), expected):
                    raise ProbeError("issuer_mismatch", "Máy chủ ủy quyền tự nhận một issuer khác với issuer được công bố / The authorization server names a different issuer than the one advertised") from None
            d.md = md; return
        if not cont: return

async def discover(url: str) -> Disc:
    url = (url or "").strip(); _check_url(url)
    d = Disc(url=url, canonical=resource_url_from_server_url(url), resource=resource_url_from_server_url(url))
    async with netguard.client("mcp", timeout=TIMEOUT) as hm, netguard.client("oauth", timeout=TIMEOUT) as ho:
        await _probe_mcp(hm, d)
        if not d.needs_auth: return d
        await _prm(ho, d)
        await _as_metadata(ho, d)
    md = d.md
    if md is not None:
        if "S256" not in (md.code_challenge_methods_supported or []):
            raise ProbeError("no_s256", "Máy chủ ủy quyền không công bố PKCE S256 (chỉ nhận S256) / The authorization server does not advertise PKCE S256 (only S256 is accepted)")
        for ep in (md.token_endpoint, md.registration_endpoint, md.revocation_endpoint):
            if ep is not None: await _guard(str(ep))
    d.scopes = (get_client_metadata_scopes(d.www_scope, d.prm, md, GRANTS) or "").split()
    base = _cimd_base(); cu = base + CIMD_PATH if base else None
    d.cimd_url = cu if is_valid_client_metadata_url(cu) else None
    return d

async def probe(url: str) -> dict:
    """-> {reachable, status, needs_auth, auth_server, registration: cimd|dcr|none, scopes, public_ok, ...}; raises ProbeError for a blocked / hostile / unusable server."""
    return (await discover(url)).public()

# ------------------------------------------------------------------ client-id metadata document (CIMD)
@router.get(CIMD_PATH)
async def client_metadata():
    """Public on purpose (the authorization server fetches it, no cookie): says only what any client says about itself. 404 unless AOS_PUBLIC_BASE is a stable https address."""
    base = _cimd_base()
    if not base: raise HTTPException(404, "Not available")
    return JSONResponse({"client_id": base + CIMD_PATH, "client_name": "Agent OS", "client_uri": base, "redirect_uris": [base + "/oauth/callback"], "grant_types": GRANTS,
                         "response_types": ["code"], "token_endpoint_auth_method": "none", "application_type": "web"}, headers={"Cache-Control": "public, max-age=300"})

# ------------------------------------------------------------------ registration
def _redirect_plan(request: Request) -> dict:
    """Where the authorization server will send the user back, and how the 11.25 engine keeps it: https -> tunnel mode (a changed AOS_PUBLIC_BASE = needs_reconnect + re-register);
    http is accepted for 127.0.0.1 / [::1] only (development) and pinned with default_redirect_uri."""
    try: base = O._public_base(request, strict=True)
    except HTTPException: base = O._public_base(request)
    ru = base + "/oauth/callback"
    if base.startswith("https://"): return {"redirect_uri": ru, "redirect_mode": "tunnel", "redirect_base": base, "default_redirect_uri": None, "application_type": "web"}
    host = httpx.URL(base).host
    if host not in ("127.0.0.1", "::1"):
        raise ProbeError("redirect_not_secure", "Cần địa chỉ HTTPS cố định (AOS_PUBLIC_BASE) hoặc 127.0.0.1 để đăng ký / A stable HTTPS address (AOS_PUBLIC_BASE) or 127.0.0.1 is needed to register", 409)
    return {"redirect_uri": ru, "redirect_mode": "web", "redirect_base": None, "default_redirect_uri": ru, "application_type": "native"}

def _auth_method_request(md) -> str:
    sup = md.token_endpoint_auth_methods_supported
    if sup is None or "client_secret_post" in sup: return "client_secret_post"
    for m in ("client_secret_basic", "none"):
        if m in sup: return m
    return "client_secret_post"

_METHOD = {"client_secret_post": "post", "client_secret_basic": "basic", "none": "none"}

async def _dcr(d: Disc, redirect_uri: str, scope: str | None, app_type: str) -> dict:
    """RFC 7591 registration at the advertised endpoint (nothing is stored here). -> {client_id, client_secret, token_auth, reg_uri, reg_token}."""
    md = d.md
    cm = OAuthClientMetadata(redirect_uris=[redirect_uri], token_endpoint_auth_method=_auth_method_request(md), grant_types=GRANTS, response_types=["code"], scope=scope or None,
                             client_name="Agent OS", application_type=app_type)
    req = create_client_registration_request(md, cm, str(md.issuer))
    async with netguard.client("oauth", timeout=TIMEOUT) as h:
        r = await _call(h, "POST", str(req.url), cap=MAX_BODY, content=bytes(req.content), headers={"Content-Type": "application/json", "Accept": "application/json"})
    if r is None: raise ProbeError("registration_failed", "Không gọi được điểm đăng ký / The registration endpoint is unreachable", 502)
    try: info = await handle_registration_response(r)
    except OAuthRegistrationError as e: raise ProbeError("registration_failed", "Đăng ký bị từ chối / Registration refused: " + str(e)[:200], 502) from None
    got = {str(u) for u in (info.redirect_uris or [])}
    if got and str(cm.redirect_uris[0]) not in got:
        raise ProbeError("registration_failed", "Máy chủ đăng ký địa chỉ chuyển về khác với yêu cầu / The server registered a different redirect address", 502)
    method = info.token_endpoint_auth_method or ("client_secret_post" if info.client_secret else "none")
    if method not in _METHOD: raise ProbeError("registration_failed", "Cách xác thực client không được hỗ trợ / Unsupported client authentication method: " + method[:40], 502)
    if method != "none" and not info.client_secret: raise ProbeError("registration_failed", "Máy chủ không cấp client_secret / No client secret was issued", 502)
    try: raw = json.loads(bytes(r.content))
    except ValueError: raw = {}
    uri, tok = raw.get("registration_client_uri"), raw.get("registration_access_token")
    if not (isinstance(uri, str) and re.match(r"^https?://", uri) and len(uri) <= 1000 and isinstance(tok, str) and 0 < len(tok) <= 2000): uri = tok = None
    return {"client_id": info.client_id, "client_secret": info.client_secret if method != "none" else None, "token_auth": _METHOD[method], "reg_uri": uri, "reg_token": tok}

async def _deregister(uri, tok):
    """Best-effort RFC 7592 delete of a client we registered and then could not store."""
    if not uri or not tok: return
    try:
        async with netguard.client("oauth", timeout=TIMEOUT) as h: await h.delete(uri, headers={"Authorization": "Bearer " + tok})
    except Exception as e: log.warning("mcp_oauth: could not de-register a client (%s)", type(e).__name__)   # noqa: BLE001

class ProbeIn(BaseModel):
    url: str = Field(min_length=8, max_length=2000)

class RegisterIn(BaseModel):
    name: str = Field(pattern=NAME_RE, description="the MCP server name in this workspace (created when missing)")
    url: str = Field(min_length=8, max_length=2000)
    scopes: str | None = Field(default=None, max_length=2000, description="narrower than the server's advertised scopes; default = what the server asks for")
    provider: str | None = Field(default=None, max_length=60, description="adopt an existing OAuth provider of this workspace (pre-registered app)")
    client_id: str | None = Field(default=None, min_length=1, max_length=400, description="manual client")
    client_secret: str | None = Field(default=None, max_length=2000)
    authorize_url: str | None = Field(default=None, max_length=2000, description="only when the server publishes no metadata")
    token_url: str | None = Field(default=None, max_length=2000, description="only when the server publishes no metadata")
    grant_type: str = Field(default="authorization_code", pattern=r"^(authorization_code|client_credentials)$")
    token_auth: str = Field(default="body", pattern=r"^(body|post|basic|none)$")
    pkce: bool = True

def _admin_ws():
    import connections as C
    u = C._caller(); C._admin(u); return u, int(u.get("ws") or 1)

@router.post("/api/mcp/servers/probe")
async def api_probe(b: ProbeIn):
    u, _ws = _admin_ws()
    try: out = await probe(b.url)
    except ProbeError as e:
        await O.audit("mcp.probe", None, False, {"host": _host(b.url), "error": e.code}); raise _http(e) from None
    await O.audit("mcp.probe", None, True, {"host": _host(b.url), "needs_auth": out["needs_auth"], "registration": out["registration"]})
    return out

def _host(url):
    try: return (httpx.URL(url).host or "?")[:100]
    except Exception: return "?"   # noqa: BLE001

async def _existing_server(ws, name, url):
    r = await A.q("SELECT * FROM mcp_servers WHERE workspace_id=%s AND name=%s AND agent_id IS NULL", (ws, name), one=True)
    if r and r["url"] != url: raise ProbeError("server_exists", f"Server '{name}' đã có với địa chỉ khác / A server '{name}' already exists with another URL", 409)
    if r and (r["token_env"] or r["headers"]): raise ProbeError("server_exists", f"Server '{name}' đang dùng token/header cố định / The server uses a fixed token or headers", 409)
    return r

async def _need_manual(b: RegisterIn, d: Disc, request: Request):
    needs = ["client_id", "client_secret"] + ([] if d.md is not None else ["authorize_url", "token_url"])
    try: ru = _redirect_plan(request)["redirect_uri"]
    except ProbeError: ru = None
    raise HTTPException(409, {"error": "manual_registration_required", "message": MSG_MANUAL, "needs": needs, "redirect_uri": ru, "probe": d.public()})

async def _undo(ws, *, pid=None, server_id=None, old_provider=None, old_conn=None, dereg=None):
    """Put back what register() changed before it failed. Every step is independent: one failing undo must not stop the others."""
    steps = []
    if server_id: steps.append(("DELETE FROM mcp_servers WHERE id=%s AND workspace_id=%s", (server_id, ws)))
    if pid and old_provider is None: steps.append(("DELETE FROM oauth_providers WHERE id=%s AND workspace_id=%s", (pid, ws)))   # the FK cascade removes its connections row
    for sql, args in steps:
        try: await A.q(sql, args)
        except Exception as e: log.error("mcp_oauth: undo step failed (%s)", type(e).__name__)   # noqa: BLE001
    if old_provider is not None:
        try:
            await A.q("UPDATE oauth_providers SET registration=%s, issuer=%s, resource=%s, iss_supported=%s WHERE id=%s AND workspace_id=%s",
                      (old_provider["registration"], old_provider["issuer"], old_provider["resource"], old_provider["iss_supported"], old_provider["id"], ws))
            if old_conn is None: await A.q("DELETE FROM connections WHERE workspace_id=%s AND provider_id=%s AND kind='mcp_oauth'", (ws, old_provider["id"]))
            else: await A.q("UPDATE connections SET kind=%s, mcp_url=%s WHERE id=%s AND workspace_id=%s", (old_conn["kind"], old_conn["mcp_url"], old_conn["id"], ws))
        except Exception as e: log.error("mcp_oauth: undo of an adopted provider failed (%s)", type(e).__name__)   # noqa: BLE001
    if dereg: await _deregister(*dereg)

def _scopes_for(b: RegisterIn, d: Disc):
    """-> (scopes the connection asks for, ceiling). Default = what the server asks for (WWW-Authenticate, then PRM, then AS metadata); the ceiling also holds everything the server advertises."""
    adv = set(d.scopes)
    for src in (d.prm, d.md):
        adv |= set(getattr(src, "scopes_supported", None) or [])
    want = b.scopes if b.scopes is not None else " ".join(d.scopes)
    return want, (" ".join(sorted(adv | set(want.split()))) if adv else None)

def _provider_in(b: RegisterIn, d: Disc, *, name, cid, secret, token_auth, rp, scopes_ceiling, grant="authorization_code") -> O.ProviderIn:
    md = d.md
    az = str(md.authorization_endpoint) if md else (b.authorize_url or "")
    tk = str(md.token_endpoint) if md else (b.token_url or "")
    if (grant == "authorization_code" and not az) or not tk:
        raise HTTPException(422, {"error": "endpoints_required", "message": "Máy chủ không công bố metadata: cần authorize_url và token_url / The server publishes no metadata: authorize_url and token_url are required"})
    want, ceiling = scopes_ceiling
    try:
        return O.ProviderIn(name=name, display_name=f"MCP {b.name}", authorize_url=az, token_url=tk, revoke_url=str(md.revocation_endpoint) if md and md.revocation_endpoint else None,
                            api_base=d.url, client_id=cid, client_secret=secret, scopes=want, token_auth=token_auth, pkce=b.pkce if not md else True, grant_type=grant,
                            issuer=str(md.issuer) if md else None, iss_supported=bool(md and md.authorization_response_iss_parameter_supported), resource=d.resource or None,
                            allowed_scopes=ceiling, default_redirect_uri=rp["default_redirect_uri"], redirect_mode=rp["redirect_mode"])
    except ValueError as e: raise HTTPException(422, str(e)[:300]) from None

async def _link_connection(ws, pid, slug, url, display, created_by):
    r = await A.q("UPDATE connections SET kind='mcp_oauth', mcp_url=%s WHERE workspace_id=%s AND provider_id=%s RETURNING id, slug", (url, ws, pid), one=True)
    if r: return r
    r = await A.q("INSERT INTO connections(workspace_id,slug,kind,provider_id,mcp_url,display_name,created_by) VALUES(%s,%s,'mcp_oauth',%s,%s,%s,%s) "
                  "ON CONFLICT(workspace_id,slug) DO NOTHING RETURNING id, slug", (ws, slug, pid, url, display, created_by), one=True)
    if not r: raise HTTPException(409, "Connection name already used")
    return r

async def _link_server(ws, b: RegisterIn, existing, cid) -> int | None:
    """-> id of the server row we CREATED (None when an existing row was only switched to oauth)."""
    if existing:
        await A.q("UPDATE mcp_servers SET auth_type='oauth', connection_id=%s WHERE id=%s AND workspace_id=%s", (cid, existing["id"], ws)); return None
    import mcp_registry as R
    if (await A.q("SELECT count(*) n FROM mcp_servers WHERE workspace_id=%s", (ws,), one=True))["n"] >= R.MAX_SERVERS:
        raise HTTPException(422, f"Tối đa {R.MAX_SERVERS} máy chủ MCP cho mỗi workspace")
    try:
        r = await A.q("INSERT INTO mcp_servers(name,url,transport,auth_type,connection_id,workspace_id) VALUES(%s,%s,'streamable_http','oauth',%s,%s) RETURNING id", (b.name, b.url, cid, ws), one=True)
    except UniqueViolation: raise HTTPException(409, f"Server '{b.name}' đã tồn tại") from None
    return r["id"]

@router.post("/api/mcp/servers/oauth")
async def api_register(b: RegisterIn, request: Request):
    u, ws = _admin_ws()
    if b.name == "ha": raise HTTPException(409, "Tên 'ha' dành riêng cho Home Assistant")
    try:
        import mcp_registry as R
        await R._check_url(b.url)
        existing = await _existing_server(ws, b.name, b.url)
        d = await discover(b.url)
        if not d.needs_auth:
            raise ProbeError("no_auth_needed", "Máy chủ này không yêu cầu đăng nhập OAuth / This server does not ask for OAuth sign-in" if d.reachable else
                             "Không kết nối được máy chủ / The server could not be reached", 409)
        out = await _register(u, ws, b, d, existing, request)
    except ProbeError as e:
        await O.audit("mcp.register", b.name, False, {"host": _host(b.url), "error": e.code}); raise _http(e) from None
    await O.audit("mcp.register", out["provider"], True, {"registration": out["registration"], "server": b.name, "host": _host(b.url), "issuer": out.get("issuer")})
    return out

async def _register(u, ws, b: RegisterIn, d: Disc, existing, request: Request) -> dict:
    pname = f"mcp-{b.name}"
    if b.grant_type == "client_credentials" and not b.client_id: raise HTTPException(422, "client_credentials needs client_id / client_secret")
    adopt = None; dcr = None; pi = None; mode = None
    if b.provider:   # 1) an app the admin registered by hand and already stored as a provider
        adopt = await A.q("SELECT * FROM oauth_providers WHERE workspace_id=%s AND name=%s AND enabled", (ws, b.provider), one=True)
        if not adopt: raise HTTPException(404, "Provider not found")
        if adopt["grant_type"] != "authorization_code": raise HTTPException(409, "Only authorization_code providers can be used for an MCP server")
        if d.md is not None and (adopt["authorize_url"] != str(d.md.authorization_endpoint) or adopt["token_url"] != str(d.md.token_endpoint)):
            raise HTTPException(409, {"error": "provider_mismatch", "message": "Endpoint của nhà cung cấp không khớp máy chủ ủy quyền của MCP / The provider's endpoints are not those of this server's authorization server"})
        if adopt["resource"] and adopt["resource"] != d.resource: raise HTTPException(409, "That provider is already bound to another MCP resource")
        mode = "preregistered"
    elif await A.q("SELECT 1 FROM oauth_providers WHERE workspace_id=%s AND name=%s", (ws, pname), one=True): raise HTTPException(409, "Provider name already exists")
    if adopt is None:
        scopes = _scopes_for(b, d)
        if b.client_id:   # 2) a client the admin typed in
            rp = {"redirect_mode": "web", "default_redirect_uri": None, "redirect_base": None, "redirect_uri": None}
            pi = _provider_in(b, d, name=pname, cid=b.client_id, secret=b.client_secret, token_auth=b.token_auth, rp=rp, scopes_ceiling=scopes, grant=b.grant_type)
            mode = "manual"
            if b.grant_type == "authorization_code":   # shown to the admin: the address to register at the provider's console
                try: rp["redirect_uri"] = _redirect_plan(request)["redirect_uri"]
                except ProbeError: pass
        elif d.registration == "cimd":   # 3) the client id is the address of our metadata document
            rp = _redirect_plan(request)
            if rp["redirect_mode"] != "tunnel" or rp["redirect_base"] != _cimd_base(): raise ProbeError("redirect_not_secure", "CIMD cần AOS_PUBLIC_BASE https cố định / CIMD needs a stable https AOS_PUBLIC_BASE", 409)
            pi = _provider_in(b, d, name=pname, cid=d.cimd_url, secret=None, token_auth="none", rp=rp, scopes_ceiling=scopes); mode = "cimd"
        elif d.registration == "dcr":   # 4) dynamic client registration
            rp = _redirect_plan(request)
            dcr = await _dcr(d, rp["redirect_uri"], scopes[0], rp["application_type"])
            pi = _provider_in(b, d, name=pname, cid=dcr["client_id"], secret=dcr["client_secret"], token_auth=dcr["token_auth"], rp=rp, scopes_ceiling=scopes); mode = "dcr"
        else: await _need_manual(b, d, request)   # 5) nothing automatic is possible
    dereg = (dcr["reg_uri"], dcr["reg_token"]) if dcr else None
    pid = server_id = None; old_provider = old_conn = None
    try:
        if adopt is not None:
            old_provider = adopt; old_conn = await A.q("SELECT id, kind, mcp_url FROM connections WHERE workspace_id=%s AND provider_id=%s ORDER BY id LIMIT 1", (ws, adopt["id"]), one=True)
            pid = adopt["id"]
            await A.q("UPDATE oauth_providers SET registration='preregistered', issuer=coalesce(issuer,%s), resource=coalesce(resource,%s), iss_supported=iss_supported OR %s "
                      "WHERE id=%s AND workspace_id=%s", (str(d.md.issuer) if d.md else None, d.resource or None, bool(d.md and d.md.authorization_response_iss_parameter_supported), pid, ws))
            conn = await _link_connection(ws, pid, adopt["name"], d.url, f"MCP {b.name}", u["id"])
            ru = adopt["default_redirect_uri"] or None
        else:
            p = await O._create_provider(pi, "mcp"); pid = p["id"]
            sets, args = ["registration=%s", "redirect_uri_registered=%s", "redirect_base=%s"], [mode, None if mode == "manual" else rp["redirect_uri"], rp["redirect_base"]]
            if dcr and dcr["reg_uri"]:
                sets += ["registration_client_uri=%s", "registration_token_enc=%s"]; args += [dcr["reg_uri"], O.enc(dcr["reg_token"], "oauth_providers.registration_token_enc", ws, pid)]
            await A.q(f"UPDATE oauth_providers SET {','.join(sets)} WHERE id=%s AND workspace_id=%s", (*args, pid, ws))
            conn = await _link_connection(ws, pid, pname, d.url, f"MCP {b.name}", u["id"])
            ru = rp["redirect_uri"]
        server_id = await _link_server(ws, b, existing, conn["id"])
    except Exception:
        await _undo(ws, pid=pid, server_id=server_id, old_provider=old_provider, old_conn=old_conn, dereg=dereg); raise
    prov = await A.q("SELECT name, scopes, issuer, resource FROM oauth_providers WHERE id=%s AND workspace_id=%s", (pid, ws), one=True)
    return {"ok": True, "registration": mode, "provider": prov["name"], "connection": conn["slug"], "server": b.name, "scopes": prov["scopes"], "issuer": prov["issuer"], "resource": prov["resource"],
            "redirect_uri": ru, "connect": f"/api/oauth/connect/{prov['name']}"}

# ------------------------------------------------------------------ re-register (admin button after AOS_PUBLIC_BASE changed; called by oauth_tools.prov_reregister)
async def reregister(p, ru: str, base: str | None) -> str:
    """Make a provider with source='mcp' use the NEW redirect address. dcr: register again (the old client is de-registered best effort); cimd: the client id is the new document URL;
    manual / preregistered: nothing to do remotely (the admin edits the redirect at the provider by hand). Nothing changes when the new registration fails. Existing connections
    of a re-registered provider need a reconnect (their tokens belong to the old client). -> the registration mode."""
    mode = p.get("registration") or "manual"; ws = int(p["workspace_id"])
    if mode not in ("dcr", "cimd"): return mode
    c = await A.q("SELECT mcp_url FROM connections WHERE workspace_id=%s AND provider_id=%s AND kind='mcp_oauth' AND mcp_url IS NOT NULL ORDER BY id LIMIT 1", (ws, p["id"]), one=True)
    if not c: raise HTTPException(409, "This provider has no MCP server address")
    try:
        if mode == "cimd":
            cb = _cimd_base()
            if not cb or cb != base: raise ProbeError("redirect_not_secure", "CIMD cần AOS_PUBLIC_BASE https cố định / CIMD needs a stable https AOS_PUBLIC_BASE", 409)
            new = {"client_id": cb + CIMD_PATH, "client_secret": None, "reg_uri": None, "reg_token": None}
        else:
            d = await discover(c["mcp_url"])
            if d.md is None or not d.md.registration_endpoint or not issuers_match(str(d.md.issuer), p["issuer"] or ""):
                raise ProbeError("registration_failed", "Máy chủ ủy quyền đã đổi hoặc không còn đăng ký động / The authorization server changed or no longer offers dynamic registration", 409)
            new = await _dcr(d, ru, p["scopes"], "web" if ru.startswith("https://") else "native")
    except ProbeError as e: raise _http(e) from None
    try:
        await A.q("UPDATE oauth_providers SET client_id=%s, redirect_uri_registered=%s, redirect_base=%s, registration_client_uri=%s, registration_token_enc=%s, client_secret_enc=%s, "
                  "token_auth=%s WHERE id=%s AND workspace_id=%s",
                  (new["client_id"], ru, base, new["reg_uri"], O.enc(new["reg_token"], "oauth_providers.registration_token_enc", ws, p["id"]),
                   O.enc(new["client_secret"], "oauth_providers.client_secret_enc", ws, p["id"]), new.get("token_auth") or p["token_auth"], p["id"], ws))
    except Exception:
        await _deregister(new.get("reg_uri"), new.get("reg_token")); raise
    await A.q("UPDATE oauth_connections SET status='needs_reconnect', status_reason='redirect_changed', updated=now() WHERE provider_id=%s AND workspace_id=%s AND status<>'revoked'", (p["id"], ws))
    if mode == "dcr" and p.get("registration_client_uri") and p.get("registration_token_enc"):
        await _deregister(p["registration_client_uri"], O.dec(p["registration_token_enc"], "oauth_providers.registration_token_enc", ws, p["id"]))
    return mode

# ------------------------------------------------------------------ used by mcp_registry.add_server: "needs sign-in" instead of a plain failure
async def hint_if_needs_auth(url: str) -> dict | None:
    """After a server could not be loaded: does it just want OAuth? -> the probe result (needs_auth True) or None. Never raises."""
    try: out = await probe(url)
    except Exception: return None   # noqa: BLE001  (a refused / broken probe is reported by the connect error itself)
    return out if out["needs_auth"] else None
