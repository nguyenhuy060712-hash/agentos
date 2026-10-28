"""GĐ9.Q P7 — custom authentication with the LangGraph `Auth` semantics + "connect an authentication provider" (OIDC / JWT) for the Agent Server API.

 * `aos.json` -> "auth": {"path": "./auth.py:auth"}: a module defining an official `langgraph_sdk.Auth` object.
     `@auth.authenticate` identifies callers that the built-in methods (session / aos_ key / Basic) did not recognise (only on `auth.paths`, default `/api/v1/`);
     `@auth.on[.threads|.assistants|.crons|.store][.create|.read|.update|.delete|.search|.create_run|...]` handlers authorise every Agent Server API call
     (handler resolution (resource, action) > (resource, *) > (*, action) > global; return None/True = allow, False = 403, a filter dict = metadata filter:
     enforced on single resources (404 when it does not match) and injected into search bodies; mutating `value["metadata"]` / `value["namespace"]` is written back).
 * A declarative policy (Settings > Agent Server) gives the common cases without code: owner-scoped threads, role deny, allow.
 * The authenticated identity becomes `config["configurable"]["langgraph_auth_user"]` of the run (caller.py) so tools can tailor or refuse.
 * Provider side: OIDC presets (Google / Microsoft Entra / Keycloak / Okta / Auth0), group -> role/workspace mapping, and JWT access tokens of the provider
   accepted as `Authorization: Bearer <jwt>` on the API (signature via JWKS, iss/aud/exp checked, only asymmetric algorithms).
Security stance: fail closed (module configured but broken -> API calls are refused), credentials stay out of run metadata (caller.clean_auth_user),
custom auth identities are clamped to `auth.max_role` (default editor) and never reach admin/UI endpoints, unclassified API routes are denied while auth is active."""
import asyncio, hashlib, inspect, json, logging, os, re, secrets, time, uuid
from collections.abc import Mapping
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb
import caller, srv_parity

LOG = logging.getLogger("aos.auth")
router = APIRouter(tags=["auth"])
ROLES = {"viewer": 1, "editor": 2, "admin": 3}
DDL = """
CREATE TABLE IF NOT EXISTS ext_identities(provider TEXT NOT NULL, identity TEXT NOT NULL, user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(provider, identity));
CREATE TABLE IF NOT EXISTS oidc_ext(id INT PRIMARY KEY DEFAULT 1 CHECK (id=1), preset TEXT, group_claim TEXT, group_map JSONB NOT NULL DEFAULT '{}',
  allowed_groups TEXT[], require_group BOOLEAN NOT NULL DEFAULT false, sync_roles BOOLEAN NOT NULL DEFAULT true, allow_admin BOOLEAN NOT NULL DEFAULT false,
  api_tokens BOOLEAN NOT NULL DEFAULT false, api_audience TEXT, updated TIMESTAMPTZ NOT NULL DEFAULT now());
"""
async def _q(*a, **k):
    import deepagent_api as A
    return await A.q(*a, **k)

async def init(): await _q(DDL)

# ============================================================== loading the user's auth module
_MOD: dict = {"key": None, "auth": None, "err": None}
def load_auth():
    """The `langgraph_sdk.Auth` object configured in aos.json / AOS_AUTH (None = not configured). A broken module is remembered in _MOD['err'] (fail closed)."""
    from langgraph_sdk import Auth
    spec = os.environ.get("AOS_AUTH") or srv_parity.file_config().auth.path
    if not spec: _MOD.update(key=None, auth=None, err=None); return None
    mod = spec.rpartition(":")[0]; p = srv_parity.Path(mod)
    if not p.is_absolute(): p = srv_parity.config_path().parent / p
    try: key = (spec, p.stat().st_mtime if p.exists() else None)
    except OSError: key = (spec, None)
    if _MOD["key"] == key: return _MOD["auth"]
    _MOD.update(key=key, auth=None, err=None)
    try:
        obj = srv_parity._load_callable(spec)
        if not isinstance(obj, Auth): raise TypeError(f"{spec} is not a langgraph_sdk.Auth object")
        _MOD["auth"] = obj
    except Exception as e:  # noqa: BLE001
        _MOD["err"] = f"{type(e).__name__}: {str(e)[:300]}"; LOG.error("auth module failed to load: %s", _MOD["err"])
    return _MOD["auth"]

def describe_auth() -> dict:
    a = load_auth(); f = srv_parity.file_config().auth
    return {"configured": bool(os.environ.get("AOS_AUTH") or f.path), "module": os.environ.get("AOS_AUTH") or f.path, "loaded": bool(a), "error": _MOD["err"],
            "authenticate": bool(a and a._authenticate_handler),
            "handlers": sorted(f"{r}.{x}" for (r, x) in (a._handlers if a else {})) + (["*.*"] if a and a._global_handlers else []),
            "paths": f.paths, "max_role": f.max_role, "workspace": f.workspace}

# ============================================================== users
class AuthUser:
    """langgraph_sdk BaseUser (identity, display_name, is_authenticated, permissions) + dict access to the custom fields the authenticate handler returned."""
    def __init__(self, d: dict): self._d = d
    identity = property(lambda s: s._d["identity"])
    display_name = property(lambda s: s._d.get("display_name") or s._d["identity"])
    is_authenticated = property(lambda s: bool(s._d.get("is_authenticated", True)))
    permissions = property(lambda s: list(s._d.get("permissions") or []))
    def __getitem__(self, k): return self._d[k]
    def __contains__(self, k): return k in self._d
    def __iter__(self): return iter(self._d)
    def get(self, k, d=None): return self._d.get(k, d)

def normalize_user(r) -> dict:
    if isinstance(r, str): d = {"identity": r}
    elif isinstance(r, Mapping): d = dict(r)
    elif hasattr(r, "identity"): d = {"identity": r.identity, "display_name": getattr(r, "display_name", None), "permissions": list(getattr(r, "permissions", None) or [])}
    else: raise TypeError("authenticate must return a user id, a dict with 'identity', or an object with .identity")
    idn = d.get("identity")
    if not isinstance(idn, str) or not idn.strip() or len(idn) > 200: raise ValueError("identity must be a non-empty string (<= 200 chars)")
    d["identity"] = idn.strip(); d["permissions"] = [str(x)[:100] for x in (d.get("permissions") or [])][:100]
    return d

def _slug(s, n=32): return re.sub(r"[^\w.@\-]", "_", s)[:n]

async def shadow_user(provider: str, identity: str, display: str | None, role: str, ws: int):
    """Agent OS user that stands for an external identity (no password, cannot log in): created once, membership role = what the identity provider/handler says."""
    r = await _q("SELECT u.* FROM ext_identities e JOIN users u ON u.id=e.user_id WHERE e.provider=%s AND e.identity=%s", (provider, identity), one=True)
    if not r:
        import admin
        uname = f"ext.{_slug(provider, 12)}.{_slug(identity)}.{hashlib.sha256(identity.encode()).hexdigest()[:6]}"
        r = await _q("INSERT INTO users(username,pw_hash,display_name) VALUES(%s,%s,%s) ON CONFLICT(username) DO UPDATE SET username=EXCLUDED.username RETURNING *",
                     (uname, admin.hash_pw(secrets.token_urlsafe(32)), (display or identity)[:80]), one=True)
        await _q("INSERT INTO ext_identities(provider,identity,user_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (provider, identity, r["id"]))
    if r.get("disabled") or r.get("is_superadmin"): return None      # a superadmin account is never reachable through an external identity
    await _q("INSERT INTO memberships(user_id,workspace_id,role) VALUES(%s,%s,%s) ON CONFLICT(user_id,workspace_id) DO UPDATE SET role=EXCLUDED.role", (r["id"], ws, role))
    return r

def _clamp(role: str | None, cap: str) -> str:
    role = role if role in ROLES else "editor"
    return role if ROLES[role] <= ROLES.get(cap, 2) else cap

# ============================================================== authenticate hook (called by admin.middleware when the built-in methods found nobody)
def _http_exc_response(e):
    return JSONResponse({"detail": str(e.detail)}, e.status_code, headers=dict(e.headers or {}))

async def _call_authenticator(h, request: Request):
    kw = {}
    for name in inspect.signature(h).parameters:
        if name == "request": kw[name] = request
        elif name == "body":
            try: kw[name] = await request.json()
            except Exception: kw[name] = None  # noqa: BLE001
        elif name == "path": kw[name] = request.url.path
        elif name == "method": kw[name] = request.method
        elif name == "path_params": kw[name] = dict(request.path_params) or None
        elif name == "query_params": kw[name] = dict(request.query_params) or None
        elif name == "headers": kw[name] = {k.encode(): v.encode() for k, v in request.headers.items()}
        elif name == "authorization": kw[name] = request.headers.get("authorization")
    return await asyncio.wait_for(h(**kw), 10)

async def authenticate_hook(request: Request):
    """-> user dict | JSONResponse (rejection from the handler) | None (nobody recognised)."""
    caller.EXTRA.set(None)
    f = srv_parity.file_config().auth; path = request.url.path
    if not any(path.startswith(p) for p in f.paths): return None
    authz = request.headers.get("authorization") or ""
    if authz.lower().startswith("bearer ") and authz.count(".") == 2 and not authz[7:].startswith("aos_"):
        try: u = await _oidc_bearer(request, authz[7:].strip(), f)
        except HTTPException as e: return JSONResponse({"detail": e.detail}, e.status_code)
        if u: return u
    a = load_auth()
    if not a or not a._authenticate_handler: return None
    from langgraph_sdk.auth import exceptions as lgx
    try: raw = await _call_authenticator(a._authenticate_handler, request)
    except lgx.HTTPException as e: return _http_exc_response(e)
    except HTTPException as e: return JSONResponse({"detail": e.detail}, e.status_code)
    except Exception as e:  # noqa: BLE001   (a bug in user code must not become a bypass, nor leak details)
        LOG.error("authenticate handler failed: %s: %s", type(e).__name__, str(e)[:200]); return JSONResponse({"detail": "Authentication failed"}, 401)
    try: d = normalize_user(raw)
    except (TypeError, ValueError) as e: return JSONResponse({"detail": f"Authentication failed: {e}"}, 401)
    if d.get("is_authenticated") is False: return None
    role = _clamp(d.get("role"), f.max_role)
    su = await shadow_user("custom", d["identity"], d.get("display_name"), role, f.workspace)
    if not su: return JSONResponse({"detail": "Account disabled"}, 403)
    caller.EXTRA.set({"kind": "custom_auth", "auth_user": d})
    return {"id": su["id"], "username": su["username"], "role": role, "ws": f.workspace, "auth": "custom_auth", "scope": "full", "ext_identity": d["identity"]}

# ============================================================== OIDC provider: presets, group mapping, bearer JWT
PRESETS = {
    "google": {"label": "Google", "issuer": "https://accounts.google.com", "params": [], "scopes": "openid email profile", "username_claim": "email", "group_claim": None,
               "note": "Google ID tokens carry no groups: restrict by e-mail domain (allowed_domains) or map nothing and use default_role."},
    "microsoft": {"label": "Microsoft Entra ID", "issuer": "https://login.microsoftonline.com/{tenant}/v2.0", "params": ["tenant"], "scopes": "openid email profile",
                  "username_claim": "preferred_username", "group_claim": "groups",
                  "note": "Use your tenant id (not 'common'). Add the `groups` optional claim (token configuration) or app roles and set group_claim to `roles`."},
    "keycloak": {"label": "Keycloak", "issuer": "{base}/realms/{realm}", "params": ["base", "realm"], "scopes": "openid email profile", "username_claim": "preferred_username",
                 "group_claim": "groups", "note": "Add a 'Group Membership' mapper (claim name `groups`) to the client scope; realm roles live in `realm_access.roles`."},
    "okta": {"label": "Okta", "issuer": "https://{domain}/oauth2/default", "params": ["domain"], "scopes": "openid email profile groups", "username_claim": "preferred_username",
             "group_claim": "groups", "note": "Add a `groups` claim (filter: matches regex .*) to the authorization server."},
    "auth0": {"label": "Auth0", "issuer": "https://{domain}/", "params": ["domain"], "scopes": "openid email profile", "username_claim": "email",
              "group_claim": None, "note": "Put roles in a namespaced custom claim with an Action, e.g. https://aos.example/roles, and set group_claim to that name."},
}
_PARAM_RE = re.compile(r"^[A-Za-z0-9._\-:/]{1,200}$")

def render_preset(name: str, params: dict) -> dict:
    p = PRESETS.get(name)
    if not p: raise HTTPException(404, "Unknown preset")
    vals = {}
    for k in p["params"]:
        v = str((params or {}).get(k) or "").strip().rstrip("/") if k == "base" else str((params or {}).get(k) or "").strip()
        if not v or not _PARAM_RE.fullmatch(v) or ".." in v: raise HTTPException(422, f"preset parameter '{k}' is required (letters, digits, . _ - : /)")
        if k == "tenant" and v.lower() in ("common", "organizations", "consumers"): raise HTTPException(422, "use your own tenant id: multi-tenant endpoints cannot be validated safely")
        vals[k] = v
    iss = p["issuer"].format(**vals)
    return {"preset": name, "issuer": iss, "scopes": p["scopes"], "username_claim": p["username_claim"], "group_claim": p["group_claim"], "button_label": f"Sign in with {p['label']}",
            "note": p["note"]}

def claim_path(claims: dict, path: str | None):
    """`groups` or a dotted path (`realm_access.roles`)."""
    if not path: return None
    v = claims
    for part in path.split("."):
        if not isinstance(v, dict) or part not in v:
            return claims.get(path) if isinstance(claims, dict) else None   # claim names that really contain dots / URLs
        v = v[part]
    return v

def groups_of(claims: dict, path: str | None) -> list[str]:
    v = claim_path(claims, path)
    if isinstance(v, str): v = re.split(r"[,\s]+", v.strip()) if v.strip() else []
    return [str(x)[:200] for x in v][:500] if isinstance(v, list) else []

def map_groups(groups: list[str], gm: dict, allow_admin: bool) -> dict[int, str]:
    """{group: {"role": "editor", "workspace": 2}} (or a list of those) -> {workspace_id: highest role}. `admin` is clamped to `editor` unless allow_admin."""
    out: dict[int, str] = {}
    for g in groups:
        ent = (gm or {}).get(g)
        for e in (ent if isinstance(ent, list) else [ent] if ent else []):
            try: ws = int(e.get("workspace") or 1); role = e.get("role")
            except (AttributeError, TypeError, ValueError): continue
            if role not in ROLES: continue
            if role == "admin" and not allow_admin: role = "editor"
            if ROLES[role] > ROLES.get(out.get(ws), 0): out[ws] = role
    return out

async def precheck_groups(claims: dict):
    """Before the SSO account is created: group allow-list / required mapping (no writes)."""
    x = await _q("SELECT * FROM oidc_ext WHERE id=1", one=True)
    if not x or not x.get("group_claim"): return
    groups = groups_of(claims, x["group_claim"])
    if x["allowed_groups"] and not set(groups) & set(x["allowed_groups"]): raise HTTPException(403, "Your account is not in a group that may sign in")
    if x["require_group"] and not map_groups(groups, x["group_map"], x["allow_admin"]): raise HTTPException(403, "Your groups are not mapped to any workspace")

async def apply_groups(user: dict, claims: dict, c: dict):
    """Called by ext.oidc_cb after the user is known: group allow-list, group -> role/workspace memberships. Raises 403 when the groups do not allow the login."""
    x = await _q("SELECT * FROM oidc_ext WHERE id=1", one=True)
    if not x or not x.get("group_claim"): return
    groups = groups_of(claims, x["group_claim"])
    if x["allowed_groups"] and not set(groups) & set(x["allowed_groups"]): raise HTTPException(403, "Your account is not in a group that may sign in")
    m = map_groups(groups, x["group_map"], x["allow_admin"])
    if x["require_group"] and not m: raise HTTPException(403, "Your groups are not mapped to any workspace")
    if user.get("is_superadmin"): return           # the owner keeps their own memberships
    for ws, role in m.items():
        await _q("INSERT INTO memberships(user_id,workspace_id,role) VALUES(%s,%s,%s) ON CONFLICT(user_id,workspace_id) DO UPDATE SET role=EXCLUDED.role", (user["id"], ws, role))
    if x["sync_roles"]:        # workspaces controlled by the mapping but no longer granted by any group -> membership removed
        managed = {int(e.get("workspace") or 1) for v in (x["group_map"] or {}).values() for e in (v if isinstance(v, list) else [v]) if isinstance(e, dict)}
        for ws in managed - set(m):
            await _q("DELETE FROM memberships WHERE user_id=%s AND workspace_id=%s", (user["id"], ws))

ALGS = {"RS256", "RS384", "RS512", "ES256", "ES384", "PS256", "PS384", "PS512", "EdDSA"}
_JWKS: dict = {}
async def _jwks_key(issuer: str, kid: str | None):
    import jwt, httpx, ext
    ent = _JWKS.get(issuer)
    if not ent or ent["exp"] < time.time() or (kid and kid not in ent["keys"] and time.time() - ent["at"] > 60):   # unknown kid => key rotation: refetch (at most once a minute)
        try:
            d = await ext._discovery(issuer)
            uri = d.get("jwks_uri")
            if not uri or not ext._issuer_ok(uri): raise HTTPException(401, "Provider has no usable jwks_uri")
            async with httpx.AsyncClient(timeout=10) as h:
                r = await h.get(uri); r.raise_for_status()
        except HTTPException: raise
        except Exception as e:  # noqa: BLE001   (provider down / bad answer: a refusal, never a 500)
            LOG.warning("jwks fetch failed: %s", type(e).__name__); raise HTTPException(401, "Provider signing keys are unavailable") from None
        keys = {}
        for k in r.json().get("keys", []):
            try: keys[k.get("kid") or ""] = jwt.PyJWK(k).key
            except Exception: continue  # noqa: BLE001
        ent = _JWKS[issuer] = {"keys": keys, "exp": time.time() + 3600, "at": time.time()}
    key = ent["keys"].get(kid or "") or (next(iter(ent["keys"].values())) if not kid and len(ent["keys"]) == 1 else None)
    if key is None: raise HTTPException(401, "Unknown signing key")
    return key

async def _oidc_bearer(request: Request, token: str, f):
    """JWT access/ID token of the connected provider. -> user dict | None (not for us); raises 401 for a token that claims to be ours but does not verify."""
    c = await _q("SELECT * FROM oidc_config WHERE id=1 AND enabled", one=True)
    x = await _q("SELECT * FROM oidc_ext WHERE id=1 AND api_tokens", one=True) if c else None
    if not (c and x): return None
    import jwt
    try: hdr = jwt.get_unverified_header(token)
    except jwt.PyJWTError: return None
    if hdr.get("alg") not in ALGS: return None           # none / HS* are never accepted (algorithm confusion)
    try:
        key = await _jwks_key(c["issuer"], hdr.get("kid"))
        claims = jwt.decode(token, key, algorithms=[hdr["alg"]], audience=x["api_audience"] or c["client_id"], issuer=c["issuer"],
                            leeway=60, options={"require": ["exp", "iss", "aud", "sub"]})
    except HTTPException: raise
    except jwt.PyJWTError as e:
        LOG.info("oidc bearer rejected: %s", type(e).__name__); return None
    if c["allowed_domains"] and str(claims.get("email", "")).split("@")[-1].lower() not in [d.lower() for d in c["allowed_domains"]]: raise HTTPException(403, "Domain not allowed")
    groups = groups_of(claims, x["group_claim"])
    if x["allowed_groups"] and not set(groups) & set(x["allowed_groups"]): raise HTTPException(403, "Not in an allowed group")
    m = map_groups(groups, x["group_map"], x["allow_admin"])
    if x["require_group"] and not m: raise HTTPException(403, "Groups not mapped to any workspace")
    uname = str(claims.get(c["username_claim"]) or claims.get("email") or claims["sub"])
    u = await _q("SELECT * FROM users WHERE oidc_sub=%s", (claims["sub"],), one=True)
    if u and (u.get("disabled") or u.get("is_superadmin")): raise HTTPException(403, "Account not usable with tokens")
    # workspaces this token may use: the ones its groups map to, plus (unless a mapped group is required) the default workspace with the default role.
    # A client-chosen X-Workspace-Id can only pick among these — it can never widen access.
    cand = dict(m)
    if not x["require_group"]: cand.setdefault(c["default_workspace"], c["default_role"])
    ws_hdr = request.headers.get("x-workspace-id")
    ws = int(ws_hdr) if ws_hdr and ws_hdr.isdigit() else (next(iter(m)) if m else c["default_workspace"])
    if ws not in cand: raise HTTPException(403, "No access in this workspace")
    if not u:
        if not c["auto_create"]: raise HTTPException(403, "No account for this identity (auto_create is off)")
        import admin
        u = await _q("INSERT INTO users(username,pw_hash,oidc_sub,display_name) VALUES(%s,%s,%s,%s) ON CONFLICT(username) DO NOTHING RETURNING *",
                     (f"oidc.{_slug(uname, 40)}.{hashlib.sha256(claims['sub'].encode()).hexdigest()[:6]}", admin.hash_pw(secrets.token_urlsafe(32)), claims["sub"], uname[:80]), one=True)
        if not u: raise HTTPException(409, "Could not create the account")
    if ws in m:      # the identity provider decides (groups are the source of truth)
        await _q("INSERT INTO memberships(user_id,workspace_id,role) VALUES(%s,%s,%s) ON CONFLICT(user_id,workspace_id) DO UPDATE SET role=EXCLUDED.role", (u["id"], ws, m[ws]))
        role = m[ws]
    else:            # default role: only fills a missing membership, never overwrites what an admin granted
        await _q("INSERT INTO memberships(user_id,workspace_id,role) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (u["id"], ws, cand[ws]))
        role = (await _q("SELECT role FROM memberships WHERE user_id=%s AND workspace_id=%s", (u["id"], ws), one=True))["role"]
    role = _clamp(role, f.max_role)
    au = {"identity": claims["sub"], "display_name": uname, "permissions": [f"group:{g}" for g in groups][:100], "email": claims.get("email")}
    caller.EXTRA.set({"kind": "oidc_token", "auth_user": au})
    return {"id": u["id"], "username": u["username"], "role": role, "ws": ws, "auth": "oidc_token", "scope": "full"}

# ---- admin API for the provider extras
class OidcExtIn(BaseModel):
    preset: str | None = Field(default=None, max_length=30)
    group_claim: str | None = Field(default=None, max_length=120)
    group_map: dict = Field(default_factory=dict)
    allowed_groups: list[str] | None = None
    require_group: bool = False
    sync_roles: bool = True
    allow_admin: bool = False
    api_tokens: bool = False
    api_audience: str | None = Field(default=None, max_length=300)

@router.get("/api/admin/oidc/presets")
async def oidc_presets():
    return {"presets": {k: {kk: vv for kk, vv in v.items() if kk != "issuer"} | {"issuer_template": v["issuer"]} for k, v in PRESETS.items()}}

class PresetIn(BaseModel):
    params: dict = Field(default_factory=dict)

@router.post("/api/admin/oidc/presets/{name}/render")
async def oidc_preset_render(name: str, b: PresetIn): return render_preset(name, b.params)

@router.get("/api/admin/oidc/extra")
async def oidc_extra_get():
    import admin; await admin.require_platform_owner()   # GĐ12.100d: machine-wide config / numbers -> platform owner only
    return await _q("SELECT preset, group_claim, group_map, allowed_groups, require_group, sync_roles, allow_admin, api_tokens, api_audience FROM oidc_ext WHERE id=1", one=True) or \
        {"preset": None, "group_claim": None, "group_map": {}, "allowed_groups": None, "require_group": False, "sync_roles": True, "allow_admin": False, "api_tokens": False, "api_audience": None}

@router.put("/api/admin/oidc/extra")
async def oidc_extra_put(b: OidcExtIn):
    import admin; await admin.require_platform_owner()   # GĐ12.100d: machine-wide config / numbers -> platform owner only
    if b.preset and b.preset not in PRESETS: raise HTTPException(422, "unknown preset")
    for g, ent in b.group_map.items():
        for e in (ent if isinstance(ent, list) else [ent]):
            if not isinstance(e, dict) or e.get("role") not in ROLES: raise HTTPException(422, f"group_map['{g}']: role must be viewer, editor or admin")
            ws = e.get("workspace", 1)
            if not isinstance(ws, int) or not await _q("SELECT 1 FROM workspaces WHERE id=%s", (ws,), one=True): raise HTTPException(422, f"group_map['{g}']: unknown workspace {ws}")
    await _q("""INSERT INTO oidc_ext(id,preset,group_claim,group_map,allowed_groups,require_group,sync_roles,allow_admin,api_tokens,api_audience) VALUES(1,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(id) DO UPDATE SET preset=EXCLUDED.preset, group_claim=EXCLUDED.group_claim, group_map=EXCLUDED.group_map, allowed_groups=EXCLUDED.allowed_groups,
        require_group=EXCLUDED.require_group, sync_roles=EXCLUDED.sync_roles, allow_admin=EXCLUDED.allow_admin, api_tokens=EXCLUDED.api_tokens, api_audience=EXCLUDED.api_audience, updated=now()""",
             (b.preset, b.group_claim, Jsonb(b.group_map), b.allowed_groups, b.require_group, b.sync_roles, b.allow_admin, b.api_tokens, b.api_audience))
    return await oidc_extra_get()

# ============================================================== authorization (@auth.on + declarative policy)
V1 = "/api/v1"
def classify(method: str, path: str):
    """(resource, action, ids) for an Agent Server API call, or None when the route is not covered (denied while authorization is active).
    ('', '', {}) = routes that need no authorization (info/ok)."""
    if not path.startswith(V1 + "/") and path != V1: return None
    s = [x for x in path[len(V1):].split("/") if x]
    m = method.upper()
    if not s: return None
    if s[0] in ("info", "ok") and m == "GET": return ("", "", {})
    if s[0] == "assistants":
        if len(s) == 1 and m == "POST": return ("assistants", "create", {})
        if len(s) == 2 and s[1] in ("search", "count") and m == "POST": return ("assistants", "search", {})
        if len(s) >= 2:
            ids = {"assistant_id": s[1]}
            if len(s) == 2: return ("assistants", {"GET": "read", "PATCH": "update", "DELETE": "delete"}.get(m), ids) if m in ("GET", "PATCH", "DELETE") else None
            if len(s) == 3 and s[2] == "latest" and m == "POST": return ("assistants", "update", ids)
            if s[2] in ("versions", "schemas", "graph", "threads", "subgraphs") and m in ("GET", "POST"): return ("assistants", "read", ids)
        return None
    if s[0] == "threads":
        if len(s) == 1 and m == "POST": return ("threads", "create", {})
        if len(s) == 2 and s[1] in ("search", "count") and m == "POST": return ("threads", "search", {})
        if len(s) < 2: return None
        ids = {"thread_id": s[1]}
        if len(s) == 2: return ("threads", {"GET": "read", "PATCH": "update", "DELETE": "delete"}[m], ids) if m in ("GET", "PATCH", "DELETE") else None
        k = s[2]
        if k == "copy" and m == "POST": return ("threads", "read", ids)
        if k == "state":
            if len(s) == 3: return ("threads", "update", ids) if m == "POST" else ("threads", "read", ids) if m == "GET" else None
            return ("threads", "read", ids) if m in ("GET", "POST") else None
        if k in ("history", "stream") and m in ("GET", "POST"): return ("threads", "read", ids)
        if k == "runs":
            if len(s) == 3: return ("threads", "create_run", ids) if m == "POST" else ("threads", "read", ids) if m == "GET" else None
            if s[3] in ("wait", "stream") and len(s) == 4 and m == "POST": return ("threads", "create_run", ids)
            if s[3] == "crons" and len(s) == 4 and m == "POST": return ("crons", "create", ids)
            ids = {**ids, "run_id": s[3]}
            if len(s) == 4: return ("threads", "read", ids) if m == "GET" else ("threads", "update", ids) if m == "DELETE" else None
            if len(s) == 5 and s[4] == "cancel" and m == "POST": return ("threads", "update", ids)
            if len(s) == 5 and s[4] in ("join", "stream") and m == "GET": return ("threads", "read", ids)
        return None
    if s[0] == "runs":
        if len(s) == 1 and m == "POST": return ("runs", "create", {})
        if len(s) == 2 and s[1] in ("wait", "stream", "batch") and m == "POST": return ("runs", "create", {})
        if len(s) >= 2 and s[1] == "crons":
            if len(s) == 2 and m == "POST": return ("crons", "create", {})
            if len(s) == 3 and s[2] == "search" and m == "POST": return ("crons", "search", {})
            if len(s) == 3 and s[2] == "count" and m == "POST": return ("crons", "search", {})   # 13.Z: POST /runs/crons/count is filtered like search
            if len(s) == 3 and m == "GET": return ("crons", "read", {"cron_id": s[2]})   # 13.Z: GET /runs/crons/{cid}
            if len(s) == 3 and m in ("PATCH", "DELETE"): return ("crons", "update" if m == "PATCH" else "delete", {"cron_id": s[2]})
        return None
    if s[0] == "store":
        if len(s) == 2 and s[1] == "items": return ("store", {"PUT": "put", "GET": "get", "DELETE": "delete"}[m], {}) if m in ("PUT", "GET", "DELETE") else None
        if s[1:] == ["items", "search"] and m == "POST": return ("store", "search", {})
        if s[1:] == ["namespaces"] and m == "POST": return ("store", "list_namespaces", {})
        return None
    return None

def matches(meta: dict | None, flt: dict) -> bool:
    meta = meta or {}
    for k, cond in (flt or {}).items():
        v = meta.get(k)
        if isinstance(cond, dict):
            if set(cond) == {"$eq"}: ok = v == cond["$eq"]
            elif set(cond) == {"$contains"}:
                c = cond["$contains"]; ok = isinstance(v, list) and ((c in v) if not isinstance(c, list) else all(x in v for x in c))
            else: ok = False          # unknown operator: deny
        else: ok = v == cond
        if not ok: return False
    return True

def as_containment(flt: dict) -> dict:
    out = {}
    for k, cond in (flt or {}).items():
        if isinstance(cond, dict):
            if set(cond) == {"$eq"}: out[k] = cond["$eq"]
            elif set(cond) == {"$contains"}: out[k] = cond["$contains"] if isinstance(cond["$contains"], list) else [cond["$contains"]]
            else: raise HTTPException(500, "Unsupported filter operator")
        else: out[k] = cond
    return out

def merge_filters(a: dict | None, b: dict | None) -> dict:
    out = dict(a or {})
    for k, v in (b or {}).items():
        out[k] = v
    return out

_UUID = re.compile(r"^[0-9a-fA-F-]{36}$")
async def _meta(resource: str, ids: dict, ws: int):
    """metadata of an existing resource inside the caller's workspace; None when it does not exist."""
    if resource in ("threads", "runs") or "thread_id" in ids:
        r = await _q("SELECT metadata FROM lg_threads WHERE thread_id=%s AND workspace_id=%s", (ids.get("thread_id"), ws), one=True) if ids.get("thread_id") else None
    elif resource == "assistants":
        a = ids.get("assistant_id")
        r = await _q("SELECT metadata FROM lg_assistants WHERE assistant_id=%s AND workspace_id=%s", (a, ws), one=True) if a and _UUID.match(a) else None
    elif resource == "crons":
        a = ids.get("cron_id")
        r = await _q("SELECT metadata FROM lg_crons WHERE cron_id=%s AND workspace_id=%s", (a, ws), one=True) if a and _UUID.match(a) else None
    else: r = None
    return None if r is None else (r["metadata"] or {})

# ---- declarative policy
POLICY_KEY = "auth_policy"
_POL: dict = {"at": 0.0, "v": None}
async def policy(force=False) -> dict | None:
    if not force and time.monotonic() - _POL["at"] < 5: return _POL["v"]
    try:
        r = await _q("SELECT value FROM server_config WHERE key=%s", (POLICY_KEY,), one=True)
        _POL.update(at=time.monotonic(), v=(r or {}).get("value") if r and (r["value"] or {}).get("rules") else None)
    except Exception: _POL["at"] = time.monotonic()  # noqa: BLE001
    return _POL["v"]

class Rule(BaseModel):
    resources: list[str] = Field(default_factory=lambda: ["*"], max_length=10)
    actions: list[str] = Field(default_factory=lambda: ["*"], max_length=12)
    mode: str = Field(pattern=r"^(allow|deny|owner)$")
    roles: list[str] | None = Field(default=None, description="rule applies only to callers with one of these roles")
    admin_bypass: bool = Field(default=True, description="owner mode: workspace admins see/act on every resource")
    workspaces: list[int] | None = Field(default=None, description="rule applies only to callers in these workspaces (default: all)")

class PolicyIn(BaseModel):
    rules: list[Rule] = Field(default_factory=list, max_length=50)

def _owner_handler(ctx_user_identity: str, resource, action, value: dict):
    """The classic LangGraph single-owner pattern: stamp metadata.owner on writes, filter every read by it."""
    flt = {"owner": ctx_user_identity}
    if action in ("create", "create_run", "update", "put"):
        md = value.setdefault("metadata", {})
        if isinstance(md, dict): md["owner"] = ctx_user_identity
    return flt

def run_policy(pol: dict, ctx_resource, ctx_action, user: dict, value: dict):
    """-> None (allow) | False (deny) | filter dict. First matching rule wins; no match = allow (like LangGraph without a handler)."""
    for r in pol.get("rules", []):
        if r["resources"] != ["*"] and ctx_resource not in r["resources"]: continue
        if r["actions"] != ["*"] and ctx_action not in r["actions"]: continue
        if r.get("roles") and user.get("role") not in r["roles"]: continue
        if r.get("workspaces") and user.get("ws") not in r["workspaces"]: continue
        if r["mode"] == "allow": return None
        if r["mode"] == "deny": return False
        if ctx_resource == "store": return None          # the store is already per user (store-auth); owner mode has nothing to filter
        if r.get("admin_bypass", True) and user.get("role") == "admin": return None
        return _owner_handler(user.get("identity") or user.get("username"), ctx_resource, ctx_action, value)
    return None

@router.get("/api/server-config/auth")
async def auth_status():
    return {**describe_auth(), "policy": await policy(True) or {"rules": []}}

@router.put("/api/server-config/auth-policy")
async def auth_policy_put(b: PolicyIn):
    bad = [x for r in b.rules for x in r.resources if x not in ("*", "assistants", "threads", "runs", "crons", "store")]
    if bad: raise HTTPException(422, f"unknown resource {bad[0]}")
    await _q("INSERT INTO server_config(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated=now()", (POLICY_KEY, Jsonb(b.model_dump())))
    await policy(True); return await auth_status()

# ---- the authorize hook
def _resolve_handler(a, resource, action):
    if not a: return None
    for key in ((resource, action), (resource, "*"), ("*", action)):
        if key in a._handlers: return a._handlers[key][0]
    return a._global_handlers[0] if a._global_handlers else None

def _deny(status, detail): return JSONResponse({"detail": detail}, status)

async def authorize_hook(request: Request, u: dict):
    """Enforce @auth.on handlers + the declarative policy on the Agent Server API. None = continue; a JSONResponse = refused."""
    path = request.url.path
    if not path.startswith(V1 + "/"): return None
    f = srv_parity.file_config().auth; a = load_auth(); pol = await policy()
    configured = bool(os.environ.get("AOS_AUTH") or f.path)
    if not a and not pol and not configured: return None
    cl = classify(request.method, path)
    if cl is not None and not cl[0]: return None                      # /info, /ok
    if configured and not a: return _deny(503, "Authorization module failed to load; the Agent Server API is closed until it is fixed")
    if cl is None: return _deny(403, "This API route is not covered by the authorization handlers")
    resource, action, ids = cl
    method = request.method.upper(); ws = u.get("ws") or 1
    # ---- value = what the handler may inspect and mutate
    body, raw = None, b""
    if method in ("POST", "PUT", "PATCH", "DELETE"):
        raw = await request.body()
        if raw and len(raw) <= 2_000_000:
            try: body = json.loads(raw)
            except ValueError: body = None
    value = dict(body) if isinstance(body, dict) else {}
    value.update({k: v for k, v in ids.items() if k not in value})
    if resource == "store" and method == "GET":
        value.update({"namespace": [x for x in (request.query_params.get("namespace") or "").split(".") if x], "key": request.query_params.get("key")})
    if action in ("create", "update", "create_run", "search") or resource == "runs":
        if value.get("metadata") in (None, ""): value["metadata"] = {}
        if not isinstance(value["metadata"], dict): return _deny(422, "metadata must be an object")
    orig = json.dumps(value, sort_keys=True, default=str)
    au = (caller.EXTRA.get() or {}).get("auth_user") or {}
    perms = ["authenticated", f"role:{u.get('role')}"] + list(au.get("permissions") or [])
    udict = {**au, "identity": au.get("identity") or u.get("username"), "display_name": au.get("display_name") or u.get("username"), "permissions": perms, "role": u.get("role")}
    from langgraph_sdk.auth import types as lt, exceptions as lgx
    ctx = lt.AuthContext(permissions=perms, user=AuthUser(udict), resource=resource, action=action)
    try:
        results = []
        h = _resolve_handler(a, resource, action)
        if h: results.append(await asyncio.wait_for(h(ctx=ctx, value=value), 10))
        if pol: results.append(run_policy(pol, resource, action, {**udict, "ws": ws}, value))
    except lgx.HTTPException as e: return _http_exc_response(e)
    except HTTPException as e: return _deny(e.status_code, e.detail)
    except Exception as e:  # noqa: BLE001   (a bug in a handler denies, never allows)
        LOG.error("authorization handler failed: %s: %s", type(e).__name__, str(e)[:200]); return _deny(500, "Authorization failed")
    filters: dict = {}
    for r in results:
        if r is False: return _deny(403, "Forbidden")
        if isinstance(r, dict): filters = merge_filters(filters, r)
    # ---- apply the filters (resources that carry metadata: threads / assistants / crons)
    if filters and resource in ("threads", "assistants", "crons"):
        try: contain = as_containment(filters)
        except HTTPException: return _deny(500, "Unsupported filter operator")
        if action == "search":
            md = value["metadata"]
            for k, v in contain.items():
                md[k] = sorted(set(map(str, md[k])) | set(map(str, v))) if isinstance(v, list) and isinstance(md.get(k), list) else v     # the handler's filter wins over the client's
        else:
            if ids:
                meta = await _meta(resource, ids, ws)
                if meta is not None and not matches(meta, filters): return _deny(404, "Not found")   # hidden, like a resource that does not exist
            if action == "create":      # re-using an id (if_exists=do_nothing) must not hand out somebody else's resource
                key = {"threads": "thread_id", "assistants": "assistant_id"}.get(resource); rid = value.get(key) if key else None
                if rid:
                    meta = await _meta(resource, {key: str(rid)}, ws)
                    if meta is not None and not matches(meta, filters): return _deny(409, "Already exists")
            if action == "create_run" and ids.get("thread_id") and isinstance(body, dict) and body.get("if_not_exists") == "create":
                if await _meta("threads", ids, ws) is None:   # the thread this run creates belongs to the caller as well
                    md = {k: v for k, v in value["metadata"].items() if k in contain}
                    await _q("INSERT INTO lg_threads(thread_id,metadata,workspace_id) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (ids["thread_id"], Jsonb(md), ws))
    # ---- write back what the handler changed
    if json.dumps(value, sort_keys=True, default=str) == orig: return None
    if resource == "store" and method == "GET":
        from urllib.parse import urlencode
        qs = dict(request.query_params); ns = value.get("namespace")
        if isinstance(ns, (list, tuple)) and all(isinstance(x, str) for x in ns): qs["namespace"] = ".".join(ns)
        if value.get("key") is not None: qs["key"] = str(value["key"])
        request.scope["query_string"] = urlencode(qs).encode()
    elif isinstance(body, dict) or (body is None and not raw and method in ("POST", "PUT", "PATCH")):
        nb = dict(body or {})
        for k, v in value.items():
            if k in nb or k not in ids: nb[k] = v
        if not nb.get("metadata"): nb.pop("metadata", None)
        request._body = json.dumps(nb).encode()
    return None

# ---- dry run for the admin UI / tests
class AuthTest(BaseModel):
    identity: str = Field(max_length=200)
    role: str = Field(default="editor", pattern=r"^(viewer|editor|admin)$")
    permissions: list[str] = Field(default_factory=list, max_length=50)
    method: str = "POST"
    path: str = "/api/v1/threads"
    body: dict | None = None
    workspace: int | None = Field(default=None, description="workspace of the caller (policy rules can be limited to workspaces)")
    resource_metadata: dict | None = Field(default=None, description="metadata of the resource the call targets (to evaluate a returned filter)")

@router.post("/api/server-config/auth/test")
async def auth_test(b: AuthTest):
    cl = classify(b.method, b.path)
    if cl is None: return {"classified": None, "decision": "deny", "reason": "route not covered"}
    resource, action, ids = cl; a = load_auth(); pol = await policy(); value = dict(b.body or {}); value.setdefault("metadata", {}) if action in ("create", "update", "create_run", "search") else None
    udict = {"identity": b.identity, "display_name": b.identity, "permissions": ["authenticated", f"role:{b.role}", *b.permissions], "role": b.role, "ws": b.workspace}
    from langgraph_sdk.auth import types as lt, exceptions as lgx
    ctx = lt.AuthContext(permissions=udict["permissions"], user=AuthUser(udict), resource=resource, action=action)
    res = []
    try:
        h = _resolve_handler(a, resource, action)
        if h: res.append(await asyncio.wait_for(h(ctx=ctx, value=value), 10))
        if pol: res.append(run_policy(pol, resource, action, udict, value))
    except lgx.HTTPException as e: return {"classified": [resource, action], "decision": "deny", "status": e.status_code, "reason": str(e.detail)}
    except Exception as e: return {"classified": [resource, action], "decision": "deny", "reason": f"handler error: {type(e).__name__}"}  # noqa: BLE001
    if any(r is False for r in res): return {"classified": [resource, action], "decision": "deny", "status": 403}
    flt = {}
    for r in res:
        if isinstance(r, dict): flt = merge_filters(flt, r)
    out = {"classified": [resource, action], "decision": "allow", "filter": flt or None, "metadata_after": value.get("metadata")}
    if flt and b.resource_metadata is not None: out["resource_visible"] = matches(b.resource_metadata, flt)
    return out
