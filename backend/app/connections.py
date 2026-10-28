"""GĐ11.24 connections model (F1): the ONE place that decides whose credential a tool uses and hands it out.
- `connections` (migration 0021) = a workspace's named connection (slug, kind secret | oauth2 | mcp_oauth | client_credentials, optional provider, allowed_scopes, profile).
  `oauth_connections` = the credential of one OWNER for it: owner = ("user", id) | ("agent", id) | ("workspace", 0). kind 'oauth' = access/refresh tokens,
  kind 'secret' = one value (value_enc; this is the per-agent `.env` store: agents A and B can both hold CRM_KEY). Every secret cell is sealed with crypto.seal
  (per-workspace DEK, AAD = table.column | row key | ws; for tokens the row key is provider:aad_owner so a user's cell is never valid as an agent's).
- `get_token(owner, provider, scopes) -> Token | raise CredentialRequired` is the single entry point; refresh runs under the existing per-credential lock.
  `resolve(user, agent_id, provider)` picks the owner: demo never; a channel run only with the agent identity 'agent' (the admin's opt-in for unattended use);
  an agent's own connection wins over the user's (scheduled runs use the agent's account); else the user's (or the agent owner's in identity mode 'agent').
- Every SQL filters workspace_id explicitly (and the tables are in admin.RLS_DIRECT). Tokens are never returned by the API, logged or audited (Token.repr hides the value).
- API (one for all screens): /api/connections?owner=me|agent:<id>|workspace; agent / workspace owners are admin only. Header X-AOS-Profile selects a workspace profile
  (a connection with profile=true whose credential is owned by the workspace). The old /api/oauth/connections endpoints stay in oauth_tools.py."""
import asyncio, re, time
from dataclasses import dataclass, field
from typing import Literal
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
import deepagent_api as A

router = APIRouter(tags=["connections"])
OWNER_TYPES = ("user", "agent", "workspace")
STATUSES = ("active", "needs_reconnect", "revoked", "error")
SLUG_RE = r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$"
MSG_CHANNEL = "Tài khoản kết nối (Google, Microsoft…) không dùng được khi trả lời qua kênh nhắn tin ngoài / Connected accounts are not available on external chat channels"
MSG_DEMO = "Tài khoản kết nối không dùng được với tài khoản dùng thử / Connected accounts are not available for demo accounts"
MSG_NO_OWNER = "this agent uses its owner's credentials and they are not available (no owner, or a demo account is calling)"
SECRET_MAX = 8000

class CredentialRequired(HTTPException):
    """The owner has no (usable) credential for this connection. 401 so existing `except HTTPException` paths keep working; `reason`/`slug`/`missing_scopes` feed the
    connect card (11.27)."""
    def __init__(self, detail, *, slug=None, reason="not_connected", missing_scopes=()):
        super().__init__(401, detail); self.slug = slug; self.reason = reason; self.missing_scopes = list(missing_scopes)

@dataclass(frozen=True)
class Token:
    value: str = field(repr=False)          # never printed: repr / str / logging of a Token show no secret
    kind: str = "oauth"                     # 'oauth' | 'secret'
    slug: str | None = None
    provider: str | None = None
    provider_id: int | None = None
    display_name: str | None = None
    api_base: str | None = None
    token_type: str = "bearer"
    scope: str | None = None
    owner: tuple = ("user", 0)

def _owner(owner):
    try: t, i = owner
    except (TypeError, ValueError): raise ValueError("owner must be (type, id)") from None
    if t not in OWNER_TYPES or isinstance(i, bool) or not isinstance(i, int) or i < 0 or (t == "workspace") != (i == 0):
        raise ValueError(f"bad owner {owner!r}")
    return t, i

def aad_owner(owner) -> str:
    """Same string as the generated column oauth_connections.aad_owner: the user id for users (the key old rows were sealed with), 'agent-5' / 'workspace-0' otherwise."""
    t, i = _owner(owner); return str(i) if t == "user" else f"{t}-{i}"

def _ws(ws=None) -> int:
    """Workspace of this call: explicit, else the signed-in user's, else the background run's. NEVER a silent fallback to workspace 1."""
    if ws is not None and not isinstance(ws, bool): return int(ws)
    import admin
    u = admin.USER.get()
    if u and u.get("ws"): return int(u["ws"])
    w = admin.WS_CTX.get()
    if w: return int(w)
    raise HTTPException(400, "workspace unknown for this connection call")

def _scopes(s) -> set:
    return {x for x in re.split(r"[\s,]+", s or "") if x}

# ------------------------------------------------------------------ definitions
async def _def(ws, name):
    """The connection definition for `name` in workspace ws: by slug first, else the OAuth provider of that name (ensured). None if neither exists."""
    d = await A.q("SELECT * FROM connections WHERE workspace_id=%s AND slug=%s", (ws, name), one=True)
    if d: return d
    p = await A.q("SELECT * FROM oauth_providers WHERE workspace_id=%s AND name=%s", (ws, name), one=True)
    if not p: return None
    return await A.q("SELECT * FROM connections WHERE id=%s", (await ensure_connection(p),), one=True)

async def ensure_connection(p) -> int:
    """The `connections` row of an OAuth provider (created on first use; migration 0021 back-filled the existing ones)."""
    r = await A.q("SELECT id FROM connections WHERE workspace_id=%s AND provider_id=%s ORDER BY id LIMIT 1", (p["workspace_id"], p["id"]), one=True)
    if r: return r["id"]
    for slug in (p["name"], p["name"] + "-oauth"):
        r = await A.q("""INSERT INTO connections(workspace_id,slug,kind,provider_id,display_name) VALUES(%s,%s,'oauth2',%s,%s)
                         ON CONFLICT(workspace_id,slug) DO NOTHING RETURNING id""", (p["workspace_id"], slug, p["id"], p.get("display_name") or p["name"]), one=True)
        if r: return r["id"]
    r = await A.q("SELECT id FROM connections WHERE workspace_id=%s AND provider_id=%s ORDER BY id LIMIT 1", (p["workspace_id"], p["id"]), one=True)
    if r: return r["id"]
    raise HTTPException(409, "Connection name already used")

_CRED_JOIN = "(o.connection_id = c.id OR (o.provider_id IS NOT NULL AND o.provider_id = c.provider_id))"

async def _cred(owner, d, ws):
    ot, oid = _owner(owner)
    return await A.q(f"""SELECT o.* FROM oauth_connections o JOIN connections c ON c.id=%s AND c.workspace_id=o.workspace_id WHERE o.workspace_id=%s AND {_CRED_JOIN}
                         AND o.owner_type=%s AND o.owner_id=%s ORDER BY o.id LIMIT 1""", (d["id"], ws, ot, oid), one=True)

# ------------------------------------------------------------------ owner resolution (who may use whose credential)
async def resolve(user, agent_id, provider, profile=None):
    """-> (owner, credential_user). Raises 401 (no caller) / 403 (demo, channel, nothing to borrow). See the module docstring."""
    import admin, agent_identity
    if not user or not user.get("id"): raise CredentialRequired("no signed-in caller for this run, so no OAuth token can be used.", reason="no_caller")
    if admin.is_demo(user): raise HTTPException(403, MSG_DEMO)
    ws = int(user.get("ws") or 1)
    if agent_id is not None and not await A.q("SELECT 1 FROM agents WHERE id=%s AND workspace_id=%s", (agent_id, ws), one=True):
        raise HTTPException(403, "Agent is not part of this workspace")
    unattended_ok = bool(agent_id) and await agent_identity.mode_of(agent_id) == "agent"   # admin opted this agent into unattended (channel / schedule) use
    if user.get("auth") == "channel" and not unattended_ok: raise HTTPException(403, MSG_CHANNEL)
    if profile:
        d = await A.q("SELECT * FROM connections WHERE workspace_id=%s AND slug=%s AND profile", (ws, profile), one=True)
        if not d: raise HTTPException(404, "Connection profile not found")
        if not await _cred(("workspace", 0), d, ws): raise CredentialRequired(f"The workspace profile '{profile}' has no credential", slug=profile, reason="not_connected")
        return ("workspace", 0), user
    d = await _def(ws, provider)
    if d and d["provider_id"] and (pg := await A.q("SELECT grant_type FROM oauth_providers WHERE id=%s AND workspace_id=%s", (d["provider_id"], ws), one=True)) and pg["grant_type"] == "client_credentials":
        return ("workspace", 0), user   # GĐ11.25: a client_credentials provider has ONE credential, the workspace's (no person signs in)
    if agent_id:
        if d and (c := await _cred(("agent", agent_id), d, ws)) and c["status"] != "revoked": return ("agent", agent_id), user
    cu, _acted = await agent_identity.credential_user(agent_id, user)
    if cu is None: raise HTTPException(403, MSG_NO_OWNER)
    return ("user", cu["id"]), cu

# ------------------------------------------------------------------ the one entry point
async def get_token(owner, provider, scopes=(), *, ws=None, force_refresh=False, user=None) -> Token:
    """Valid credential of `owner` for `provider` (OAuth provider name or connection slug) in workspace `ws` (default: the caller's). Refreshes an OAuth access token that
    expires within 60 s (or always with force_refresh, e.g. the API answered 401 mid-call). Raises CredentialRequired (401) when the owner has none / revoked /
    lacks a requested scope, HTTPException 403 for a demo caller or a scope above the connection's allowed_scopes."""
    import admin, oauth_tools as O
    ot, oid = _owner(owner); ws = _ws(ws)
    if admin.is_demo(): raise HTTPException(403, MSG_DEMO)
    if ot == "agent" and not await A.q("SELECT 1 FROM agents WHERE id=%s AND workspace_id=%s", (oid, ws), one=True): raise HTTPException(403, "Agent is not part of this workspace")
    want = _scopes(" ".join(scopes) if not isinstance(scopes, str) else scopes)
    d = await _def(ws, provider)
    if d and d["allowed_scopes"] and want - _scopes(d["allowed_scopes"]):
        raise HTTPException(403, f"Scope not allowed for connection '{d['slug']}': {' '.join(sorted(want - _scopes(d['allowed_scopes'])))}")
    if d and d["kind"] == "secret":
        c = await _cred(owner, d, ws)
        if not c or c["status"] == "revoked" or not c["value_enc"]: raise CredentialRequired(f"No value stored for '{d['slug']}' — add it in Settings > Connections", slug=d["slug"])
        val = O.dec(c["value_enc"], "oauth_connections.value_enc", ws, c["id"])
        await A.q("UPDATE oauth_connections SET last_used=now() WHERE id=%s AND workspace_id=%s", (c["id"], ws))
        return Token(val, "secret", d["slug"], owner=owner)
    p = await A.q("SELECT * FROM oauth_providers WHERE workspace_id=%s AND enabled AND " + ("id=%s" if d and d["provider_id"] else "name=%s"),
                  (ws, d["provider_id"] if d and d["provider_id"] else provider), one=True)
    if not p: raise HTTPException(404, f"OAuth provider '{provider}' not configured")
    if p.get("allowed_scopes") and want - _scopes(p["allowed_scopes"]):   # GĐ11.25: the provider's own scope ceiling
        raise HTTPException(403, f"Scope not allowed for provider '{p['name']}': {' '.join(sorted(want - _scopes(p['allowed_scopes'])))}")
    async with O.LOCKS.setdefault((p["id"], ot, oid), asyncio.Lock()):
        c = await A.q("SELECT * FROM oauth_connections WHERE provider_id=%s AND owner_type=%s AND owner_id=%s AND workspace_id=%s AND kind='oauth'", (p["id"], ot, oid, ws), one=True)
        slug = d["slug"] if d else p["name"]
        if not c or c["status"] == "revoked": raise CredentialRequired(f"Not connected to {p['display_name'] or p['name']} — connect it in Settings > Connections", slug=slug)
        if want and c["scope"] and (miss := want - _scopes(c["scope"])):
            raise CredentialRequired(f"{p['display_name'] or p['name']}: missing permission {' '.join(sorted(miss))} — reconnect the account", slug=slug, reason="scope_missing", missing_scopes=sorted(miss))
        ref = user or {"id": oid if ot == "user" else None, "ws": ws}
        if force_refresh: tok = await O._refresh(c, p, ref, "http_401")
        elif c["expires_at"] and c["expires_at"].timestamp() < time.time() + 60: tok = await O._refresh(c, p, ref)
        else: tok = O._tok(c, "access_token_enc")
        await A.q("UPDATE oauth_connections SET last_used=now() WHERE id=%s AND workspace_id=%s", (c["id"], ws))
    return Token(tok, "oauth", slug, p["name"], p["id"], p["display_name"], p["api_base"], c["token_type"] or "bearer", c["scope"], owner)

async def save_tokens(owner, p, j, account=None):
    """Store an OAuth token response for `owner` (the OAuth callback / refresh / import). The workspace is the provider's."""
    import oauth_tools as O
    ot, oid = _owner(owner); ws = int(p["workspace_id"]); aad = aad_owner(owner)
    exp = j.get("expires_in"); exp_sql = None if exp is None else int(exp)
    cid = await ensure_connection(p)
    await A.q("""INSERT INTO oauth_connections(provider_id,user_id,owner_type,owner_id,connection_id,kind,workspace_id,account,access_token_enc,refresh_token_enc,token_type,scope,expires_at,status,updated,last_refresh)
        VALUES(%s,%s,%s,%s,%s,'oauth',%s,%s,%s,%s,%s,%s,CASE WHEN %s::int IS NULL THEN NULL ELSE now()+make_interval(secs=>%s::int) END,'active',now(),now())
        ON CONFLICT(provider_id,owner_type,owner_id) DO UPDATE SET account=coalesce(EXCLUDED.account,oauth_connections.account), access_token_enc=EXCLUDED.access_token_enc,
          refresh_token_enc=coalesce(EXCLUDED.refresh_token_enc,oauth_connections.refresh_token_enc), token_type=EXCLUDED.token_type,
          scope=coalesce(EXCLUDED.scope,oauth_connections.scope), expires_at=EXCLUDED.expires_at, status='active', status_reason=NULL, connection_id=coalesce(oauth_connections.connection_id,EXCLUDED.connection_id),
          updated=now(), last_refresh=now()""",
              (p["id"], oid if ot == "user" else None, ot, oid, cid, ws, account, O.enc(j["access_token"], "oauth_connections.access_token_enc", ws, p["id"], aad),
               O.enc(j.get("refresh_token"), "oauth_connections.refresh_token_enc", ws, p["id"], aad), j.get("token_type", "bearer"), j.get("scope"), exp_sql, exp_sql))

async def set_secret(owner, slug, value, ws):
    """Write (replace) the secret `slug` of `owner`. Sealed after the upsert because the row id is part of the AAD."""
    import oauth_tools as O
    ot, oid = _owner(owner); ws = _ws(ws)
    d = await A.q("SELECT * FROM connections WHERE workspace_id=%s AND slug=%s AND kind='secret'", (ws, slug), one=True)
    if not d: raise HTTPException(404, "Connection not found")
    if not isinstance(value, str) or not value or len(value) > SECRET_MAX: raise HTTPException(422, "Value must be 1-%d characters" % SECRET_MAX)
    if ot == "agent" and not await A.q("SELECT 1 FROM agents WHERE id=%s AND workspace_id=%s", (oid, ws), one=True): raise HTTPException(404, "Agent not found")
    r = await A.q("""INSERT INTO oauth_connections(kind,owner_type,owner_id,user_id,connection_id,workspace_id,status,updated) VALUES('secret',%s,%s,%s,%s,%s,'active',now())
                     ON CONFLICT(connection_id,owner_type,owner_id) WHERE kind='secret' DO UPDATE SET value_enc=NULL, status='active', status_reason=NULL, updated=now() RETURNING id""",
                  (ot, oid, oid if ot == "user" else None, d["id"], ws), one=True)
    await A.q("UPDATE oauth_connections SET value_enc=%s WHERE id=%s AND workspace_id=%s", (O.enc(value, "oauth_connections.value_enc", ws, r["id"]), r["id"], ws))
    return r["id"]

async def delete_credential(owner, name, ws):
    """Remove one owner's credential (OAuth: remote revoke first, best effort). Returns {"ok", "remote_revoked"} or raises 404."""
    import oauth_tools as O
    ot, oid = _owner(owner); ws = _ws(ws)
    d = await _def(ws, name)
    c = await _cred(owner, d, ws) if d else None
    if not c: raise HTTPException(404, "Connection not found")
    remote = None
    if c["kind"] == "oauth" and c["provider_id"]:
        p = await A.q("SELECT * FROM oauth_providers WHERE id=%s AND workspace_id=%s", (c["provider_id"], ws), one=True)
        if p: remote = await O.revoke_remote(c, p)
    await A.q("DELETE FROM oauth_connections WHERE id=%s AND workspace_id=%s", (c["id"], ws))
    await O.audit("connection.revoke", d["slug"], True, {"owner": f"{ot}:{oid}", "remote_revoked": remote})
    return {"ok": True, "remote_revoked": remote}

# ------------------------------------------------------------------ API
def _caller():
    """Signed-in, non-demo, non-channel caller. Demo and channel identities never touch connections, not even their own listing."""
    import admin
    u = admin.cur()
    if not u or not u.get("id"): raise HTTPException(401, "Sign in required")
    if admin.is_demo(u): raise HTTPException(403, MSG_DEMO)
    if u.get("auth") == "channel": raise HTTPException(403, MSG_CHANNEL)
    return u

def _admin(u):
    if u.get("role") != "admin": raise HTTPException(403, "Admin only")

async def _owner_arg(owner, u):
    o = (owner or "me").strip()
    if o == "me": return ("user", u["id"])
    _admin(u)
    if o == "workspace": return ("workspace", 0)
    m = re.fullmatch(r"agent:([0-9]{1,9})", o)
    if not m: raise HTTPException(422, "owner must be me, agent:<id> or workspace")
    if not await A.q("SELECT 1 FROM agents WHERE id=%s AND workspace_id=%s", (int(m.group(1)), u.get("ws") or 1), one=True): raise HTTPException(404, "Agent not found")
    return ("agent", int(m.group(1)))

_LIST = f"""SELECT c.slug, c.kind, c.display_name, c.allowed_scopes, c.profile, c.created_at, p.name provider,
       (o.id IS NOT NULL) AS connected, o.status, o.status_reason, o.label, o.account, o.scope, o.expires_at, o.last_used, o.last_refresh,
       coalesce(o.refresh_token_enc IS NOT NULL, false) has_refresh
    FROM connections c LEFT JOIN oauth_providers p ON p.id=c.provider_id AND p.workspace_id=c.workspace_id
    LEFT JOIN oauth_connections o ON o.workspace_id=c.workspace_id AND {_CRED_JOIN} AND o.owner_type=%s AND o.owner_id=%s
    WHERE c.workspace_id=%s"""

def _row(r):
    return {**r, "status": r["status"] if r["connected"] else "not_connected"}

async def sync_providers(ws):
    """Every OAuth provider of the workspace has a `connections` row (providers made after migration 0021 get theirs here, or on first token save)."""
    await A.q("""INSERT INTO connections(workspace_id,slug,kind,provider_id,display_name) SELECT p.workspace_id,p.name,'oauth2',p.id,coalesce(p.display_name,p.name) FROM oauth_providers p
                 WHERE p.workspace_id=%s AND NOT EXISTS (SELECT 1 FROM connections c WHERE c.provider_id=p.id) ON CONFLICT(workspace_id,slug) DO NOTHING""", (ws,))

async def _states(owner, ws, slug=None):
    ot, oid = owner
    await sync_providers(ws)
    rows = await A.q(_LIST + (" AND c.slug=%s" if slug else "") + " ORDER BY c.slug", (ot, oid, ws, *((slug,) if slug else ())))
    return [_row(r) for r in rows]

@router.get("/api/connections")
async def conn_list(request: Request, owner: str = "me"):
    u = _caller(); ws = int(u.get("ws") or 1)
    prof = request.headers.get("x-aos-profile")
    if prof:
        d = await A.q("SELECT 1 FROM connections WHERE workspace_id=%s AND slug=%s AND profile", (ws, prof), one=True)
        if not d: raise HTTPException(404, "Connection profile not found")
        return await _states(("workspace", 0), ws, prof)
    return await _states(await _owner_arg(owner, u), ws)

@router.get("/api/connections/catalog")
async def conn_catalog():
    """GĐ11.25: the ~25 ready-made OAuth services (app/oauth_catalog.json) + whether this workspace already has one under that name. Declared before /{slug}. Creating one: POST /api/oauth/providers/from-catalog."""
    u = _caller(); ws = int(u.get("ws") or 1)
    import oauth_tools as O
    have = {r["name"] for r in await A.q("SELECT name FROM oauth_providers WHERE workspace_id=%s", (ws,))}
    return [{**e, "created": e["id"] in have} for e in O.catalog()]

@router.get("/api/connections/{slug}")
async def conn_get(slug: str, owner: str = "me"):
    u = _caller(); ws = int(u.get("ws") or 1)
    r = await _states(await _owner_arg(owner, u), ws, slug)
    if not r: raise HTTPException(404, "Connection not found")
    return r[0]

class ConnIn(BaseModel):
    slug: str = Field(pattern=SLUG_RE)
    kind: Literal["secret", "oauth2"]
    display_name: str | None = Field(default=None, max_length=120)
    provider: str | None = Field(default=None, max_length=60, description="oauth2: name of an OAuth provider of this workspace")
    allowed_scopes: str | None = Field(default=None, max_length=1000)
    profile: bool = False

@router.post("/api/connections")
async def conn_create(b: ConnIn):
    u = _caller(); _admin(u); ws = int(u.get("ws") or 1); pid = None
    if b.kind == "oauth2":
        p = await A.q("SELECT id FROM oauth_providers WHERE workspace_id=%s AND name=%s", (ws, b.provider or ""), one=True)
        if not p: raise HTTPException(404, "OAuth provider not found")
        pid = p["id"]
    elif b.provider: raise HTTPException(422, "provider is only for kind oauth2")
    elif await A.q("SELECT 1 FROM oauth_providers WHERE workspace_id=%s AND name=%s", (ws, b.slug), one=True): raise HTTPException(409, "An OAuth provider already uses this name")
    r = await A.q("""INSERT INTO connections(workspace_id,slug,kind,provider_id,display_name,allowed_scopes,profile,created_by) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                     ON CONFLICT(workspace_id,slug) DO NOTHING RETURNING slug""", (ws, b.slug, b.kind, pid, b.display_name or b.slug, b.allowed_scopes or None, b.profile, u["id"]), one=True)
    if not r: raise HTTPException(409, "Connection name already exists")
    import oauth_tools as O; await O.audit("connection.create", b.slug, True, {"kind": b.kind, "profile": b.profile})
    return (await _states(("user", u["id"]), ws, b.slug))[0]

class CredIn(BaseModel):
    value: str = Field(min_length=1, max_length=SECRET_MAX)
    owner: str = "me"

@router.post("/api/connections/{slug}/credentials")
async def conn_credentials(slug: str, b: CredIn):
    """Write-only: the value is sealed and never returned. owner=me (your own key) or, as admin, agent:<id> / workspace."""
    u = _caller(); ws = int(u.get("ws") or 1); owner = await _owner_arg(b.owner, u)
    await set_secret(owner, slug, b.value, ws)
    import oauth_tools as O; await O.audit("connection.credential", slug, True, {"owner": f"{owner[0]}:{owner[1]}"})
    return {"ok": True, "slug": slug, "owner": f"{owner[0]}:{owner[1]}"}

class LabelIn(BaseModel):
    label: str | None = Field(default=None, max_length=80)
    owner: str = "me"

@router.patch("/api/connections/{slug}")
async def conn_label(slug: str, b: LabelIn):
    u = _caller(); ws = int(u.get("ws") or 1); owner = await _owner_arg(b.owner, u)
    d = await _def(ws, slug); c = await _cred(owner, d, ws) if d else None
    if not c: raise HTTPException(404, "Connection not found")
    await A.q("UPDATE oauth_connections SET label=%s, updated=now() WHERE id=%s AND workspace_id=%s", (b.label, c["id"], ws))
    return {"ok": True}

@router.delete("/api/connections/{slug}")
async def conn_delete(slug: str, owner: str = "me"):
    """owner=me|agent:<id>|workspace removes that owner's credential; owner=definition (admin) removes the connection and every credential under it."""
    u = _caller(); ws = int(u.get("ws") or 1)
    if owner == "definition":
        _admin(u)
        r = await A.q("DELETE FROM connections WHERE workspace_id=%s AND slug=%s RETURNING id, provider_id", (ws, slug), one=True)
        if not r: raise HTTPException(404, "Connection not found")
        import oauth_tools as O; await O.audit("connection.delete", slug, True, {}); return {"ok": True}   # the FK cascade removed its credentials
    return await delete_credential(await _owner_arg(owner, u), slug, ws)

@router.get("/api/agents/{agent_id}/connections")
async def agent_connections(agent_id: int):
    u = _caller(); _admin(u); ws = int(u.get("ws") or 1)
    if not await A.q("SELECT 1 FROM agents WHERE id=%s AND workspace_id=%s", (agent_id, ws), one=True): raise HTTPException(404, "Agent not found")
    return await _states(("agent", agent_id), ws)

@router.get("/api/oauth/tokens")
async def tokens_exist(exists: str = ""):
    """Bulk presence check for the caller's OWN credentials: {name: true|false}. Never returns a token."""
    u = _caller(); ws = int(u.get("ws") or 1); out = {}
    for n in [x.strip() for x in exists.split(",") if x.strip()][:50]:
        d = await _def(ws, n) if re.fullmatch(SLUG_RE, n) else None
        c = await _cred(("user", u["id"]), d, ws) if d else None
        out[n] = bool(c and c["status"] != "revoked" and (c["access_token_enc"] or c["value_enc"]))
    return out
