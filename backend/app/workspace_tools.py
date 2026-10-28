"""GĐ8.5 + GĐ11.55: Google Workspace + Microsoft 365 tools for agents — in-process, thin REST over httpx (no sidecar process, no extra package).
- Why not langchain-google-community 5.1.1 toolkits: compatible (no downgrade), but importing any of its tools runs the package __init__ (BigQuery, grpc,
  Model Armor ...) = +108 MB RSS per process (googleapiclient alone +27 MB; httpx +0), the tools are sync (threads) and its Gmail tools return RFC 2047-encoded
  subjects for Vietnamese mail. Why not MCP sidecars (google_workspace_mcp / ms-365-mcp-server): one more Python/Node process per workspace on a tight VPS.
- THIS FILE = the core (settings API, one HTTP path `_call`, registry, approval tiers, idempotency, middleware). The tools themselves live in
  `workspace_tools_google.py` (gmail_, gcal_, gsheets_, gdrive_ ...) and `workspace_tools_ms.py` (outlook_, ocal_, onedrive_, excel_ ...) and add themselves with
  `register(...)` (see its docstring: that is the whole API a new tool family needs). Scopes: `office_scopes.py`.
- Tools act AS THE CHATTING USER (or as the agent's fixed identity): the token ONLY comes from connections.get_token(owner, provider, scopes) (GĐ11.24/25: per-owner
  connection, auto refresh, encrypted at rest). No user -> no call; another user's token is never looked up; tokens are never logged or returned.
- Per workspace: workspaces.gw_tools = {"google": "off|read|write", "ms365": "off|read|write", "auto_tools": ["google:gmail_draft", ...]} (default off; demo workspace always off).
- Approval tiers (GĐ11.55, replace WRITE_NAMES): READ never asks. DEFAULT_ASK (☆) asks unless an admin put it in `auto_tools` (one store, PUT /api/workspace-tools) AND the
  agent's own interrupt_on does not list it AND the run is not external (channel / untrusted mail, `run_is_external()`). ALWAYS_ASK (★) always asks and can never be made automatic.
  A tool may decide by its arguments (`tier_fn`: an event WITH attendees is ★). deepagents 0.7.32 / langchain 1.4.7 `InterruptOnConfig` has a `when(ToolCallRequest) -> bool`
  predicate, so no custom approval middleware is needed: `interrupt_config()` builds the entries, `OfficeMiddleware` only refreshes the admin's list and sets the replay key.
- Replay safety: a write tool called twice for the same (agent, thread, tool_call_id) acts once (`agent_audit.idem_key`, migration 0060); the second call gets the stored answer.
- Keys in the tool catalogue: google:<tool> / ms365:<tool>; the tool name the model sees is the part after ':'. Old keys (google:sheets_read ...) stay as aliases.
- Mock: GOOGLE_API_BASE / GRAPH_API_BASE / GOOGLE_OAUTH_BASE / MS_OAUTH_BASE env overrides (overlay/run_local.py GW_MOCK=1 -> tests/fake_workspace_api.py)."""
import asyncio, contextvars, email.utils, inspect, json, os, re, time, urllib.parse
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable
import httpx
from fastapi import APIRouter, HTTPException
from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb
import office_scopes as OS

class _Lazy:   # deepagent_api imported on first use (keeps this module importable in unit tests)
    def __getattr__(self, n): import deepagent_api; return getattr(deepagent_api, n)
A = _Lazy()

router = APIRouter(tags=["workspace-tools"])
DDL = "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS gw_tools JSONB NOT NULL DEFAULT '{}'"
KINDS = {"google": "google", "ms365": "microsoft"}          # tool prefix -> oauth provider name
MODES = ("off", "read", "write")
READ, DEFAULT_ASK, ALWAYS_ASK = "read", "default_ask", "always_ask"   # tiers: no mark | ☆ | ★
TIERS = (READ, DEFAULT_ASK, ALWAYS_ASK)
q = lambda s: urllib.parse.quote(str(s), safe="")           # ids from the model never change the path

async def init(): await A.q(DDL)

# ------------------------------------------------------------------ OAuth provider presets (scopes: minimal, see office_scopes.py)
def scopes(provider, mode):
    """Space-separated scopes a provider is registered with. Mode 'off' (client configured before anything is switched on) = the read set."""
    return OS.scope_string(provider, "write" if mode == "write" else "read")

def preset(provider, mode="read"):
    if provider == "google":
        b = os.environ.get("GOOGLE_OAUTH_BASE", "").rstrip("/")
        urls = (f"{b}/authorize", f"{b}/token", f"{b}/revoke", f"{b}/userinfo") if b else (
            "https://accounts.google.com/o/oauth2/v2/auth", "https://oauth2.googleapis.com/token", "https://oauth2.googleapis.com/revoke",
            "https://openidconnect.googleapis.com/v1/userinfo")
        return {"display_name": "Google Workspace", "urls": urls, "api_base": os.environ.get("GOOGLE_API_BASE") or "https://www.googleapis.com/",
                "extra_params": {"access_type": "offline", "prompt": "consent", "include_granted_scopes": "true"}, "scopes": scopes("google", mode)}
    b = os.environ.get("MS_OAUTH_BASE", "").rstrip("/")
    urls = (f"{b}/authorize", f"{b}/token", None, f"{b}/userinfo") if b else (
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize", "https://login.microsoftonline.com/common/oauth2/v2.0/token", None,
        "https://graph.microsoft.com/oidc/userinfo")
    return {"display_name": "Microsoft 365", "urls": urls, "api_base": os.environ.get("GRAPH_API_BASE") or "https://graph.microsoft.com/v1.0/",
            "extra_params": {"prompt": "select_account"}, "scopes": scopes("microsoft", mode)}

# ------------------------------------------------------------------ the registry (the API of a tool family)
@dataclass(frozen=True)
class Meta:
    key: str                 # this entry's key ("google:gsheets_read" or its alias "google:sheets_read")
    canonical: str           # the real key (== key unless this is an alias)
    name: str                # the name the model sees (the part after ':')
    kind: str                # "google" | "ms365"
    family: str              # office_scopes family (gmail, calendar, sheets, drive | mail, calendar, files)
    tier: str                # READ | DEFAULT_ASK | ALWAYS_ASK (the default; tier_fn may raise it per call)
    scopes: tuple = ()       # extra scopes beyond the family's (e.g. calendar.freebusy)
    tier_fn: Callable | None = field(default=None, compare=False)
    describe: Callable | None = field(default=None, compare=False)
    label: str = ""
    alias_of: str | None = None
    lookup: Callable | None = field(default=None, compare=False)   # async fn(_c, **args) -> list[str]: the real recipients / guests the card must show (F-7)

SPECS: dict = {}       # key: (write?, description, fn) — kept 3-tuples (tests, studio); aliases included
LABELS: dict = {}      # key: short human name (Vietnamese in i18n_dict.json); aliases included
META: dict = {}        # key: Meta
BY_NAME: dict = {}     # model-visible tool name: Meta (aliases included)
PREFIXES = {"google": ("gmail_", "gcal_", "gsheets_", "gdrive_", "gdocs_"), "ms365": ("outlook_", "ocal_", "onedrive_", "word_", "excel_")}   # F16
_KEY = re.compile(r"^(google|ms365):([a-z][a-z0-9_]{2,59})$")

def register(key, desc, fn, *, label, family, tier=READ, scopes=(), tier_fn=None, describe=None, aliases=(), lookup=None):
    """Add one tool. `fn(_c, **args)` is an async function: `_c(method, url, params=, body=, headers=, text=, follow=, content=, raw=, public=)` is the ONLY way to reach the API
    (token, scopes, refresh, 429 back-off, audit, replay safety all happen there); it returns the JSON (or text / bytes) and raises `_Err("message for the model")`.
    key: "google:<name>" / "ms365:<name>", name prefixed per F16 (gmail_ gcal_ gsheets_ gdrive_ gdocs_ | outlook_ ocal_ onedrive_ word_ excel_), unique across both kinds.
    desc / label: plain English (Vietnamese in app/i18n_dict.json — add both strings). family: see office_scopes.FAMILIES. scopes: extra scopes this tool needs.
    tier: READ | DEFAULT_ASK (☆, asks unless admin allows automatic) | ALWAYS_ASK (★). tier_fn(args)->tier decides by arguments (e.g. event with attendees -> ALWAYS_ASK);
    it can only be used with a non-READ default tier and an exception in it counts as ALWAYS_ASK. describe(args)->str is the text of the approval card (default: recipients,
    subject, attendees, body preview). aliases: older keys that keep working (hidden from `all_tools`, shown in the catalogue with `alias_of`).
    lookup(_c, **args)->list[str] (★ tools that only know ids): runs with the caller's own token BEFORE the card is built (OfficeMiddleware), returns the lines to show
    (recipients / subject / guests). If it fails the card says so ("không xem được người nhận — từ chối nếu không chắc") instead of looking harmless."""
    m = _KEY.match(key or "")
    if not m: raise ValueError(f"tool key {key!r} must look like google:<name> or ms365:<name>")
    kind, name = m.groups()
    if tier not in TIERS: raise ValueError(f"unknown tier {tier!r}")
    if tier_fn and tier == READ: raise ValueError("tier_fn needs a write tool")
    if not name.startswith(PREFIXES[kind]): raise ValueError(f"{key}: tool names of {kind} start with {PREFIXES[kind]} (F16)")
    OS._fam(KINDS[kind], family)
    if not (desc and label and callable(fn)): raise ValueError(f"{key}: description, label and function are required")
    seen = set()
    for k in (key, *aliases):   # everything is checked BEFORE anything is stored: a refused registration leaves nothing behind
        km = _KEY.match(k or "")
        if not km or km.group(1) != kind: raise ValueError(f"alias {k!r} must be a {kind}: key")
        if k in META or km.group(2) in BY_NAME or km.group(2) in seen: raise ValueError(f"tool {k!r} is already registered")
        seen.add(km.group(2))
    SPECS[key] = (tier != READ, desc, fn); LABELS[key] = label
    META[key] = BY_NAME[name] = Meta(key, key, name, kind, family, tier, tuple(scopes), tier_fn, describe, label, lookup=lookup)
    for a in aliases:
        am = _KEY.match(a)
        SPECS[a] = SPECS[key]; LABELS[a] = label
        META[a] = BY_NAME[am.group(2)] = Meta(a, key, am.group(2), kind, family, tier, tuple(scopes), tier_fn, describe, label, alias_of=key, lookup=lookup)

# ------------------------------------------------------------------ workspace mode + the admin's automatic list (ONE store: workspaces.gw_tools)
async def _gw(ws) -> dict:
    """gw_tools of a workspace (system read) + whether it is a demo workspace."""
    import admin
    tok = admin.USER.set(None)
    try: r = await A.q("SELECT gw_tools, demo FROM workspaces WHERE id=%s", (int(ws or 1),), one=True)
    finally: admin.USER.reset(tok)
    if not r or r.get("demo") or int(ws or 1) in A.gw.DEMO_WS: return {"_demo": True}
    return dict(r.get("gw_tools") or {})

async def modes(ws) -> dict:
    """{"google": mode, "ms365": mode} of a workspace (system read). Demo workspace -> all off."""
    g = await _gw(ws)
    if g.get("_demo"): return {k: "off" for k in KINDS}
    return {k: (g.get(k) if g.get(k) in MODES else "off") for k in KINDS}

def _auto_keys(g) -> list:
    """The valid ☆ canonical keys in a gw_tools value (junk, ★ and read tools are ignored: they could never be automatic)."""
    out = []
    for k in (g.get("auto_tools") or []) if isinstance(g.get("auto_tools"), list) else []:
        m = META.get(k)
        if m and m.canonical == k and m.tier == DEFAULT_ASK and k not in out: out.append(k)
    return out

async def auto_tools(ws) -> list:
    g = await _gw(ws)
    return [] if g.get("_demo") else _auto_keys(g)

AUTO: dict = {}         # ws -> (loaded at monotonic time, frozenset of canonical keys): what the approval predicate reads (it cannot await)
AUTO_TTL = 2.0
async def refresh_auto(ws, force=False):
    """Re-read the admin's automatic list at most every AUTO_TTL s; called before every model turn of an agent that has office tools, so a switch-off by an admin
    (also in another server process) is seen within seconds and always before the next approval decision."""
    ws = int(ws or 1); e = AUTO.get(ws)
    if force or not e or time.monotonic() - e[0] > AUTO_TTL: AUTO[ws] = (time.monotonic(), frozenset(await auto_tools(ws)))
    return AUTO[ws][1]

class ModesIn(BaseModel):
    google: str = Field(default="off", pattern=r"^(off|read|write)$")
    ms365: str = Field(default="off", pattern=r"^(off|read|write)$")
    auto_tools: list[str] | None = Field(default=None, max_length=200, description="☆ tools (keys) that may run without asking; omitted = keep the current list; [] = none")

def _norm_auto(keys) -> list:
    out = []
    for k in keys:
        m = META.get(k)
        if not m: raise HTTPException(422, f"Unknown tool: {str(k)[:60]}")
        if m.tier == ALWAYS_ASK: raise HTTPException(422, f"{m.name} always asks for approval and cannot be made automatic")
        if m.tier == READ: raise HTTPException(422, f"{m.name} is read-only: there is nothing to approve")
        if m.canonical not in out: out.append(m.canonical)
    return out

def _redirect_uri():
    import oauth_tools as O
    return f"http://127.0.0.1:{O._loopback_port()}/oauth/callback"

@router.get("/api/workspace-tools")
async def get_modes():
    import admin
    u = admin.cur() or {}; ws = admin.cur_ws(); m = await modes(ws); auto = await auto_tools(ws)
    if admin.is_demo(u): m = {k: "off" for k in KINDS}; auto = []
    return {"modes": m, "providers": {k: KINDS[k] for k in KINDS}, "tools": await catalog(ws, u), "auto_tools": auto, "redirect_uri": _redirect_uri(),
            "all_tools": [{"key": k, "label": LABELS.get(k, k), "description": SPECS[k][1], "write": SPECS[k][0], "tier": META[k].tier, "auto": k in auto,
                           "by_arguments": bool(META[k].tier_fn)} for k in SPECS if not META[k].alias_of],
            "can_edit": u.get("role") == "admin" and not admin.is_demo(u) and not await _is_demo_ws(ws)}

@router.put("/api/workspace-tools")
async def put_modes(b: ModesIn):
    import admin
    u = admin.cur() or {}; ws = admin.cur_ws()
    if u.get("role") != "admin" or admin.is_demo(u): raise HTTPException(403, "Admin only")
    auto = _norm_auto(b.auto_tools) if b.auto_tools is not None else None
    if (b.google != "off" or b.ms365 != "off" or auto) and await _is_demo_ws(ws):
        raise HTTPException(403, "Google / Microsoft 365 tools are off for the demo workspace")
    new = {"google": b.google, "ms365": b.ms365}
    if auto is not None: new["auto_tools"] = auto
    await A.q("UPDATE workspaces SET gw_tools=coalesce(gw_tools,'{}'::jsonb) || %s::jsonb WHERE id=%s", (Jsonb(new), ws))
    for kind, mode in (("google", b.google), ("ms365", b.ms365)):   # write scopes only when the workspace enables write (users reconnect to grant them)
        await A.q("UPDATE oauth_providers SET scopes=%s WHERE workspace_id=%s AND name=%s", (scopes(KINDS[kind], mode), ws, KINDS[kind]))
    A.S["agents"].clear(); AUTO.pop(int(ws), None)
    import oauth_tools as O; await O.audit("workspace_tools.update", None, True, {"google": b.google, "ms365": b.ms365, **({"auto_tools": auto} if auto is not None else {})})
    return {"ok": True, "modes": await modes(ws), "auto_tools": await auto_tools(ws)}

async def _is_demo_ws(ws):
    import admin
    tok = admin.USER.set(None)
    try: r = await A.q("SELECT demo FROM workspaces WHERE id=%s", (int(ws),), one=True)
    finally: admin.USER.reset(tok)
    return bool((r or {}).get("demo")) or int(ws) in A.gw.DEMO_WS

class PresetIn(BaseModel):
    client_id: str = Field(min_length=1, max_length=400)
    client_secret: str | None = Field(default=None, max_length=2000, description="write-only; omit on update to keep")

@router.post("/api/workspace-tools/presets/{provider}")
async def put_preset(provider: str, b: PresetIn):
    """Admin: create/update the 'google' or 'microsoft' OAuth provider of this workspace from the preset (URLs + minimal scopes).
    GĐ11.55: redirect mode 'loopback_paste' (no domain yet: the person pastes the address the browser could not open, POST /api/oauth/paste-callback). A provider an admin
    already moved to another mode (tunnel / paste) keeps it; only the old default 'web' of a preset made before 11.55 is switched."""
    import admin, oauth_tools as O
    u = admin.cur() or {}; ws = admin.cur_ws()
    if u.get("role") != "admin" or admin.is_demo(u): raise HTTPException(403, "Admin only")
    if provider not in OS.PROVIDERS: raise HTTPException(404, "Unknown preset (google | microsoft)")
    if await _is_demo_ws(ws): raise HTTPException(403, "Google / Microsoft 365 tools are off for the demo workspace")
    kind = next(k for k, v in KINDS.items() if v == provider)
    p = preset(provider, (await modes(ws))[kind]); a, t, rv, ui = p["urls"]
    r = await A.q("""INSERT INTO oauth_providers(name,display_name,authorize_url,token_url,revoke_url,userinfo_url,api_base,client_id,client_secret_enc,scopes,extra_params,token_auth,pkce,enabled,workspace_id,redirect_mode)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'body',true,true,%s,'loopback_paste')
        ON CONFLICT(workspace_id,name) DO UPDATE SET display_name=EXCLUDED.display_name, authorize_url=EXCLUDED.authorize_url, token_url=EXCLUDED.token_url,
          revoke_url=EXCLUDED.revoke_url, userinfo_url=EXCLUDED.userinfo_url, api_base=EXCLUDED.api_base, client_id=EXCLUDED.client_id,
          client_secret_enc=coalesce(EXCLUDED.client_secret_enc, oauth_providers.client_secret_enc), scopes=EXCLUDED.scopes, extra_params=EXCLUDED.extra_params,
          token_auth='body', pkce=true, enabled=true,
          redirect_mode=CASE WHEN oauth_providers.redirect_mode='web' AND oauth_providers.default_redirect_uri IS NULL THEN 'loopback_paste' ELSE oauth_providers.redirect_mode END RETURNING id, redirect_mode""",
                  (provider, p["display_name"], a, t, rv, ui, p["api_base"], b.client_id, None, p["scopes"],
                   Jsonb(p["extra_params"]), ws), one=True)
    if b.client_secret:   # GĐ9.7: the row id is part of the AAD -> seal after the upsert
        await A.q("UPDATE oauth_providers SET client_secret_enc=%s WHERE id=%s", (O.enc(b.client_secret, "oauth_providers.client_secret_enc", ws, r["id"]), r["id"]))
    await O.audit("provider.preset", provider)
    return {"ok": True, "id": r["id"], "name": provider, "scopes": p["scopes"], "redirect_mode": r["redirect_mode"], "redirect_uri": _redirect_uri()}

# ------------------------------------------------------------------ HTTP as the current user
class _Err(Exception):
    """A tool failure the MODEL may read (plain English, no secrets). maybe_done=True: the request may have been processed (network error after sending)."""
    maybe_done = False

class _Auth(_Err):
    """The person must (re)connect an account / the agent needs a fixed identity: becomes the Fleet `auth_required` JSON (agent_identity.parse_auth_required)."""
    def __init__(self, message, *, reason="reconnect", url="/settings?s=connections", missing=()):
        super().__init__(message); self.reason, self.url, self.missing = reason, url, list(missing)
    def payload(self):
        return {"auth_required": True, "auth_url": self.url, "auth_id": self.reason, "message": str(self)[:500]}

NEEDS_FIXED_IDENTITY = ("This run has no person at the keyboard (schedule, webhook, channel or new mail) and the agent uses the identity of whoever chats, so it has no account to use. "
                        "The agent needs a fixed identity: its owner opens the agent's Identity setting and chooses 'agent', or connects an account for the agent.")
FIXED_URL = "/settings?s=connections"
MAX_TRIES, MAX_WAIT = 3, 20            # HTTP 429 / 503: at most 3 tries, never wait longer than MAX_WAIT s for one Retry-After
SLEEP = asyncio.sleep                   # tests replace it with a recorder
CALL_CTX: contextvars.ContextVar = contextvars.ContextVar("aos_office_call", default=None)   # set by OfficeMiddleware around a tool call: {"key": replay key | None}
EXTERNAL: contextvars.ContextVar = contextvars.ContextVar("aos_office_external", default=False)   # GĐ11.72 sets it for runs started by untrusted input (mail, strangers)

def run_is_external() -> bool:
    """True when this run was started by something that is not a trusted person: a channel chatter, or (GĐ11.72) an untrusted incoming mail. Then ☆ behaves like ★."""
    if EXTERNAL.get(): return True
    import admin
    return (admin.cur() or {}).get("auth") == "channel"

def _google(svc):
    b = os.environ.get("GOOGLE_API_BASE")
    if b: return b.rstrip("/") + "/"
    return {"gmail": "https://gmail.googleapis.com/", "sheets": "https://sheets.googleapis.com/", "docs": "https://docs.googleapis.com/"}.get(svc, "https://www.googleapis.com/")

def _graph(): return (os.environ.get("GRAPH_API_BASE") or "https://graph.microsoft.com/v1.0/").rstrip("/") + "/"

def _hosts(kind) -> set:
    """The only hosts a tool of this kind may call WITH the user's token (SSRF / token-leak guard on top of netguard)."""
    bases = [_graph()] if kind == "ms365" else [_google(s) for s in ("gmail", "sheets", "drive", "calendar", "docs")]
    return {urllib.parse.urlsplit(b).netloc.lower() for b in bases}

def _retry_after(r) -> float | None:
    v = (r.headers.get("retry-after") or "").strip()
    if not v: return None
    try: return max(0.0, float(v))
    except ValueError: pass
    try: return max(0.0, email.utils.parsedate_to_datetime(v).timestamp() - time.time())
    except (TypeError, ValueError): return None

def _err_parts(r):
    """-> (message, reason) of a Google / Graph error body (best effort, never raises)."""
    try: j = r.json()
    except Exception: return None, ""  # noqa: BLE001
    e = j.get("error") if isinstance(j, dict) else None
    if isinstance(e, dict):
        reasons = [str(x.get("reason") or "") for x in (e.get("errors") or []) if isinstance(x, dict)] + [str(x.get("reason") or "") for x in (e.get("details") or []) if isinstance(x, dict)]
        return e.get("message"), next((x for x in reasons if x), "") or str(e.get("code") or e.get("status") or "")
    return (str(e) if e else None), ""

_RATE_REASONS = {"ratelimitexceeded", "userratelimitexceeded", "throttled", "activityLimitReached".lower(), "applicationthrottled", "toomanyrequests"}
_SOFT_DENIED = {"insufficientpermissions", "erroraccessdenied", "authorization_requestdenied", "accessdenied", "permission_denied"}

def _is_rate_limited(r, reason) -> bool:
    return r.status_code in (429, 503) or (r.status_code == 403 and reason.lower() in _RATE_REASONS)

def _scope_denied(r, msg, reason, tok, need) -> bool:
    """403 because the token lacks a permission (not because the file / calendar is somebody else's)."""
    if r.status_code != 403: return False
    if reason == "ACCESS_TOKEN_SCOPE_INSUFFICIENT" or "insufficient authentication scopes" in (msg or "").lower(): return True
    return reason.lower() in _SOFT_DENIED and (not tok.scope or bool(OS.missing_from(tok.scope, need)))

async def _mark_reconnect(kind, owner, ws, why):
    """The account stopped working for this tool: the connection shows 'needs reconnect' (Settings > Connections) until the person signs in again."""
    try:
        await A.q("""UPDATE oauth_connections c SET status='needs_reconnect', status_reason=%s, updated=now() FROM oauth_providers p
                     WHERE c.provider_id=p.id AND p.workspace_id=%s AND p.name=%s AND c.workspace_id=%s AND c.owner_type=%s AND c.owner_id=%s AND c.kind='oauth' AND c.status='active'""",
                  (why, ws, KINDS[kind], ws, owner[0], owner[1]))
    except Exception as e: print("office: could not mark needs_reconnect", type(e).__name__)  # noqa: BLE001

async def _token(connections, owner, kind, need, ws, user, force=False):
    """-> Token. get_token(owner, provider, scopes) with the tool's minimal scopes; a stronger scope or a differently written grant (Graph prefix) is accepted."""
    prov = KINDS[kind]; name = "Google Workspace" if kind == "google" else "Microsoft 365"
    try:
        try: return await connections.get_token(owner, prov, need, ws=ws, force_refresh=force, user=user)
        except connections.CredentialRequired as e:
            if e.reason != "scope_missing": raise
            t = await connections.get_token(owner, prov, (), ws=ws, force_refresh=force, user=user)   # not literally granted: maybe a stronger scope (gmail.modify covers gmail.readonly)
            if OS.satisfied(t.scope, need): return t
            miss = OS.missing_from(t.scope, need)
            raise _Auth(f"{name}: permission missing ({', '.join(OS.label(s) for s in miss)}) — reconnect the account in Settings > Connections.", reason="scope_missing", missing=miss) from None
    except connections.CredentialRequired as e:
        if e.reason == "no_caller": raise _Auth(NEEDS_FIXED_IDENTITY, reason="fixed_identity", url=FIXED_URL) from None
        raise _Err(str(e.detail)) from None   # not connected / revoked: plain sentence (the connect card is GĐ11.27)
    except HTTPException as e:
        if e.status_code in (400, 401):   # the refresh was refused for good (invalid_grant: the oauth engine already marked the connection needs_reconnect) / no refresh token left
            raise _Auth(f"{name}: the saved sign-in no longer works — reconnect the account in Settings > Connections.", reason="needs_reconnect") from None
        raise _Err(str(e.detail)) from None   # 403 (above the scope ceiling / demo), 404 (provider not set up), 502 (token endpoint unreachable)

async def _send(kind, method, url, *, params, body, headers, content, tok, follow, public):
    """One HTTP request under the egress policy (netguard): purpose 'oauth' = https (http only to AOS_EGRESS_ALLOW), no redirect, internal only if allow-listed.
    follow (OneDrive download): purpose 'fetch', every hop re-checked, httpx drops Authorization on a cross-origin hop. public: no token at all (pre-signed / upload URLs)."""
    import netguard
    h = {"Accept": "application/json", **(headers or {})}
    if not public: h["Authorization"] = f"Bearer {tok}"
    async with netguard.client("fetch" if (follow or public) else "oauth", timeout=30 if (follow or public) else 20) as c:
        return await c.request(method, url, params=params, json=body, content=content, headers=h)

async def _own_connection(owner, agent_id, ws) -> bool:
    """F-9: is `owner` (from connections.resolve) the agent's OWN connection - the agent's identity account, or the agent OWNER's account while the agent is in fixed-identity mode?
    A workspace-wide credential, another person's account, a missing agent: False."""
    import agent_identity
    if not agent_id or not owner: return False
    if owner[0] == "agent": return owner[1] == agent_id
    if owner[0] != "user": return False
    r = await A.q("SELECT owner_id FROM agents WHERE id=%s AND workspace_id=%s", (agent_id, ws), one=True)
    return bool(r and r["owner_id"] is not None and r["owner_id"] == owner[1] and await agent_identity.mode_of(agent_id) == "agent")

async def _call(kind, tool, write, agent_id, method, url, *, params=None, body=None, headers=None, text=False, raw=False, follow=False, content=None, public=False,
                family=None, extra_scopes=()):
    import admin, caller, connections, oauth_tools as O, agent_identity
    user = admin.cur()
    if not user or not user.get("id"): raise _Auth(NEEDS_FIXED_IDENTITY, reason="fixed_identity", url=FIXED_URL)
    if admin.is_demo(user): raise _Err("Google / Microsoft 365 tools are off for demo accounts")
    ws = int(user.get("ws") or 1); prov = KINDS[kind]
    if not public and urllib.parse.urlsplit(url).netloc.lower() not in _hosts(kind):   # a model-built URL can never send the token to another host
        await O.audit("tool.call", prov, False, {"tool": tool, "error": "host_not_allowed"}, user=user, agent_id=agent_id)
        raise _Err("blocked: that address is not a " + ("Google" if kind == "google" else "Microsoft") + " API address")
    try: owner, user = await connections.resolve(user, agent_id, prov)   # GĐ11.24: ONE owner resolution; demo / channel refused there
    except connections.CredentialRequired as e:
        if e.reason == "no_caller": raise _Auth(NEEDS_FIXED_IDENTITY, reason="fixed_identity", url=FIXED_URL) from None
        raise _Err(str(e.detail)) from None
    except HTTPException as e: raise _Err(str(e.detail)) from None
    c = caller.current()
    unattended = c.get("kind") not in agent_identity.INTERACTIVE or c.get("agent_id") is not None
    if unattended and owner[0] == "user" and await agent_identity.mode_of(agent_id) != "agent":   # no person + no fixed identity = nobody's account to use (Fleet rule)
        raise _Auth(NEEDS_FIXED_IDENTITY, reason="fixed_identity", url=FIXED_URL)
    import run_trust
    if not write and run_trust.is_mail_run() and not await _own_connection(owner, agent_id, ws):   # F-9: a read that skipped the approval card must use the agent owner's own connection, nothing else
        await O.audit("tool.call", prov, False, {"tool": tool, "error": "mail_run_not_own_connection"}, user=user, agent_id=agent_id)
        raise _Err("blocked: a run started by an incoming mail may only read the agent owner's own connected account")
    m = (await modes(user.get("ws") or 1))[kind]
    if m == "off" or (write and m != "write"):
        raise _Err(f"{kind} tools are {'off' if m == 'off' else 'read-only'} in this workspace — an admin can change it in Settings")
    need = OS.need(prov, family, write, extra_scopes) if family else []
    tok = None if public else await _token(connections, owner, kind, need, ws, user)
    tries = refreshed = 0; last = "ms"
    while True:
        tries += 1
        try: r = await _send(kind, method, url, params=params, body=body, headers=headers, content=content, tok=tok.value if tok else None, follow=follow, public=public)
        except httpx.HTTPError as e:
            await O.audit("tool.call", prov, False, {"tool": tool, "error": type(e).__name__, "tries": tries}, user=user, agent_id=agent_id)
            err = _Err(f"network error ({type(e).__name__})")   # a refused / failed connect sent nothing; a read timeout after the request went out may have been processed
            err.maybe_done = write and method.upper() != "GET" and not isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)) and type(e).__name__ != "NetBlocked"; raise err from None
        msg, reason = _err_parts(r) if r.status_code >= 400 else (None, "")
        if r.status_code == 401 and not public and not refreshed:   # 8.P: token revoked / rotated early -> refresh ONCE (11.25 engine) and retry once
            refreshed = 1; await O.audit("tool.call", prov, False, {"tool": tool, "status": 401, "retry": "refresh"}, user=user, agent_id=agent_id)
            tok = await _token(connections, owner, kind, need, ws, user, force=True); continue
        if r.status_code >= 400 and _is_rate_limited(r, reason):
            wait = _retry_after(r); wait = (2 ** (tries - 1)) if wait is None else wait
            if tries < MAX_TRIES and wait <= MAX_WAIT: await SLEEP(wait); continue
            last = f"HTTP {r.status_code}: the service is busy or rate-limited — try again in about {int(wait) + 1} s"
        break
    await O.audit("tool.call", prov, r.status_code < 400, {"tool": tool, "status": r.status_code, **({"tries": tries} if tries > 1 else {})}, user=user, agent_id=agent_id)
    if r.status_code >= 400:
        if r.status_code == 401 and not public:
            await _mark_reconnect(kind, owner, ws, "http_401")
            raise _Auth(f"{'Google Workspace' if kind == 'google' else 'Microsoft 365'} rejected the saved sign-in — reconnect the account in Settings > Connections.", reason="needs_reconnect")
        if tok and _scope_denied(r, msg, reason, tok, need):
            miss = OS.missing_from(tok.scope, need) or need
            await _mark_reconnect(kind, owner, ws, "scope_insufficient")
            raise _Auth(f"{'Google Workspace' if kind == 'google' else 'Microsoft 365'}: permission missing ({', '.join(OS.label(s) for s in miss)}) — reconnect the account in Settings > Connections.",
                        reason="scope_missing", missing=miss)
        if _is_rate_limited(r, reason):
            err = _Err(last); err.maybe_done = False; raise err
        hint = " (reconnect the account in Settings > Connections to grant the needed permission)" if r.status_code in (401, 403) else ""
        err = _Err(f"HTTP {r.status_code}: {str(msg or r.reason_phrase)[:300]}{hint}"); err.maybe_done = write and r.status_code >= 500 and method.upper() != "GET"; raise err
    if raw: return r.content
    if text: return r.text
    return r.json() if r.content and "json" in r.headers.get("content-type", "") else {}

def _out(x, cap=8000):
    s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False, default=str)
    return s if len(s) <= cap else s[:cap] + " …(truncated)"

def _iso(s, default):
    import datetime as dt
    if not s: return default.isoformat()
    try: x = dt.datetime.fromisoformat(s)
    except ValueError: raise _Err("datetime must be ISO 8601, e.g. 2026-10-26T09:00:00+07:00") from None
    return (x if x.tzinfo else x.replace(tzinfo=dt.timezone(dt.timedelta(hours=7)))).isoformat()

# ------------------------------------------------------------------ replay safety (agent_audit.idem_key)
async def _idem_claim(key, tool, agent_id, user):
    """-> ('new', id, None) the caller owns the key | ('done', id, stored answer) | ('pending', id, None) an earlier attempt started and never finished."""
    ws = int(user.get("ws") or 1)
    r = await A.q("""INSERT INTO agent_audit(workspace_id,agent_id,action,tool,principal_id,principal,ok,detail,idem_key) VALUES(%s,%s,'office_write',%s,%s,%s,true,%s,%s)
                     ON CONFLICT(workspace_id, idem_key) WHERE idem_key IS NOT NULL DO NOTHING RETURNING id""",
                  (ws, agent_id, tool, user.get("id"), user.get("username"), Jsonb({"idem": True}), key), one=True)
    if r: return "new", r["id"], None
    old = await A.q("SELECT id, idem_result FROM agent_audit WHERE workspace_id=%s AND idem_key=%s", (ws, key), one=True) or {}
    return ("done", old.get("id"), old["idem_result"]) if old.get("idem_result") is not None else ("pending", old.get("id"), None)

async def _idem_store(rid, user, text):
    await A.q("UPDATE agent_audit SET idem_result=%s WHERE id=%s AND workspace_id=%s", (text, rid, int(user.get("ws") or 1)))

async def _idem_release(rid, user):
    await A.q("DELETE FROM agent_audit WHERE id=%s AND workspace_id=%s AND idem_result IS NULL", (rid, int(user.get("ws") or 1)))

PENDING_TEXT = ("Error: this action was already started in an earlier attempt and its result is unknown, so it was NOT repeated. "
                "Check the account (for example the Sent folder or the calendar) before trying again.")

def _tool(key, agent_id):
    m = META[key]; write, desc, fn = SPECS[key]; kind, name = m.kind, m.name
    async def _c(method, url, **kw): return await _call(kind, name, write, agent_id, method, url, family=m.family, extra_scopes=m.scopes, **kw)
    _c.agent_id = agent_id   # GĐ11 F-8: mail tools stamp `X-AOS-Agent: <agent_id>` on what they send (mail_triggers skips it: no self-trigger loop)
    async def run(**kw):
        import admin
        ctx = CALL_CTX.get()
        if write and ctx is None:   # fail closed: a write tool only runs inside an agent that has the approval + replay layer (OfficeMiddleware)
            return "Error: this tool can only run inside an agent, where the user approves it."
        user = admin.cur() or {}; claim = None
        try:
            idem = ctx.get("key") if (write and ctx) else None
            if idem and user.get("id"):
                st, rid, old = await _idem_claim(idem, name, agent_id, user)
                if st == "done": return old
                if st == "pending": return PENDING_TEXT
                claim = rid
            out = _out(await fn(_c, **kw))
            if claim: await _idem_store(claim, user, out); claim = None
            return out
        except _Auth as e:
            return json.dumps(e.payload(), ensure_ascii=False)
        except _Err as e:
            if e.maybe_done: claim = None   # keep the claim row (no answer stored): the outcome is unknown, so a replay must not repeat it blindly
            return f"Error: {e}"
        except asyncio.CancelledError:
            claim = None; raise            # killed mid-call: same, unknown outcome
        finally:
            if claim:   # a definite failure (or a bug) before anything was done: let a later attempt try again
                try: await _idem_release(claim, user)
                except Exception: pass  # noqa: BLE001
    sig = inspect.signature(fn); params = list(sig.parameters.values())[1:]
    run.__signature__ = sig.replace(parameters=params); run.__annotations__ = {p.name: p.annotation for p in params}
    run.__name__ = name; run.__doc__ = desc
    note = "" if not write else (" [WRITE — the user must approve]" if m.tier == ALWAYS_ASK else " [WRITE — the user must approve unless an admin allowed it to run automatically]")
    return StructuredTool.from_function(coroutine=run, name=name, description=desc + (note or " [read-only]"))

# ------------------------------------------------------------------ approval tiers
def tier_of(name, args=None) -> str:
    """Tier of one call (by the model-visible tool name and its arguments). Unknown tool -> READ (not ours)."""
    m = BY_NAME.get(name)
    if not m: return READ
    if m.tier_fn:
        try: t = m.tier_fn(dict(args or {}))
        except Exception: return ALWAYS_ASK  # noqa: BLE001  (fail closed)
        return t if t in TIERS else ALWAYS_ASK
    return m.tier

def needs_approval(name, args, auto=frozenset(), *, forced=False, external=False) -> bool:
    """The whole approval rule in one pure function. auto = canonical keys the admin allowed; forced = the agent's own interrupt_on lists it."""
    t = tier_of(name, args); m = BY_NAME.get(name)
    if t == READ or not m: return False
    if t == ALWAYS_ASK or forced or external: return True
    return m.canonical not in auto

def ask_if_attendees(args) -> str:
    """tier_fn for calendar creates: an event WITHOUT guests only touches the person's own calendar (☆); with guests it sends invitations to other people (★)."""
    return ALWAYS_ASK if (args or {}).get("attendees") else DEFAULT_ASK

_ASKED: OrderedDict = OrderedDict()   # tool_call_id -> True: a call that was once held for approval stays held (the predicate runs again on resume and must not flip)
def _asked(tid, ask):
    if not tid: return ask
    if tid in _ASKED: return True
    if ask:
        _ASKED[tid] = True
        while len(_ASKED) > 5000: _ASKED.popitem(last=False)
    return ask

_CARD: OrderedDict = OrderedDict()   # tool_call_id -> list[str] (looked-up lines) | None (the look-up failed); filled by resolve_card() before the card is built

def _lookup_fail(name):
    who = "người tham dự" if name.startswith(("gcal_", "ocal_")) else "người nhận"
    return f"CẢNH BÁO: không xem được {who} — từ chối nếu không chắc. (Could not look up who this will reach: reject if unsure.)"

async def resolve_card(agent_id, tc) -> None:
    """F-7: for a ★ tool that only knows ids, fetch the real recipients / guests with the caller's own token (read only) so the card can show them. Never raises."""
    m = BY_NAME.get((tc or {}).get("name")); tid = (tc or {}).get("id")
    if not (m and m.lookup and tid): return
    async def _c(method, url, **kw): return await _call(m.kind, m.name, False, agent_id, method, url, family=m.family, extra_scopes=m.scopes, **kw)
    try:
        lines = await asyncio.wait_for(m.lookup(_c, **dict(tc.get("args") or {})), 20)
        lines = [str(x)[:400] for x in lines if str(x).strip()][:12] or None
    except Exception: lines = None  # noqa: BLE001  (_Auth, _Err, timeout, bad arguments: all mean "not seen")
    _CARD[tid] = lines
    while len(_CARD) > 5000: _CARD.popitem(last=False)

def describe_call(name, args, tid=None) -> str:
    """Text of the approval card: who gets it, what it says, what is attached. Custom per tool via register(describe=). A tool with a look-up shows what resolve_card() found
    (real recipients / subject / guests), or a clear warning when it could not."""
    m = BY_NAME.get(name); args = args or {}
    if m and m.lookup:
        facts = _CARD.get(tid) if tid else None
        base = _describe_base(m, name, args)[:1400]
        return (base + chr(10) + (chr(10).join(facts) if facts else _lookup_fail(name)))[:2000]
    return _describe_base(m, name, args)

def _describe_base(m, name, args) -> str:
    if m and m.describe:
        try: return str(m.describe(dict(args)))[:2000]
        except Exception: pass  # noqa: BLE001
    lines = [f"{(m.label if m else name)} ({name})"]
    for k, t in (("to", "To"), ("cc", "Cc"), ("bcc", "Bcc"), ("subject", "Subject"), ("summary", "Event"), ("start", "Start"), ("end", "End"),
                 ("attendees", "Attendees (they will receive an invitation)"), ("spreadsheet_id", "Spreadsheet"), ("range", "Range")):
        v = args.get(k)
        if v: lines.append(f"{t}: " + (", ".join(map(str, v)) if isinstance(v, (list, tuple)) else str(v)[:300]))
    for k in ("body", "text", "description", "note"):
        if args.get(k): lines.append("Text: " + str(args[k])[:400].replace("\n", " ")); break
    att = args.get("attachments")
    if att: lines.append(f"Attachments: {len(att)}")
    return "\n".join(x for x in lines if x.strip())[:2000]

def interrupt_config(names, forced, ws_id) -> dict:
    """{tool name: InterruptOnConfig} for the office tools among `names` (the agent's tool names). forced = names in the agent's own interrupt_on. Entries that always ask
    carry no predicate; ☆ tools get `when` (ask unless the admin allowed it AND the run is trusted)."""
    out = {}
    for n in names:
        m = BY_NAME.get(n)
        if not m or (m.tier == READ): continue
        cfg = {"allowed_decisions": ["approve", "edit", "reject"], "description": (lambda tc, _s, _r: describe_call(tc.get("name"), tc.get("args"), tc.get("id")))}
        if m.tier != ALWAYS_ASK and n not in forced:
            def when(req, _n=n):
                tc = req.tool_call
                return _asked(tc.get("id"), needs_approval(_n, tc.get("args") or {}, AUTO.get(int(ws_id or 1), (0, frozenset()))[1], external=run_is_external()))
            cfg["when"] = when
        out[n] = cfg
    return out

class OfficeMiddleware(AgentMiddleware):
    """Per agent that has office tools: (1) before every model turn re-read the admin's automatic list (so the approval predicate never uses a stale list), (2) around every
    office tool call set the replay key (agent + thread + tool_call_id) and mark the call as running inside an agent."""
    def __init__(self, agent_id, ws_id):
        super().__init__(); self.agent_id, self.ws_id = agent_id, int(ws_id or 1)
    async def awrap_model_call(self, request, handler):
        await refresh_auto(self.ws_id)
        resp = await handler(request)
        try:   # F-7: look up recipients / guests of the ★ calls the model just made, BEFORE the approval card is built (the card callable is sync)
            calls = [tc for msg in (getattr(resp, "result", None) or []) for tc in (getattr(msg, "tool_calls", None) or []) if (BY_NAME.get(tc.get("name")) or Meta).lookup]
            if calls: await asyncio.gather(*(resolve_card(self.agent_id, tc) for tc in calls))
        except Exception: pass  # noqa: BLE001
        return resp
    async def awrap_tool_call(self, request, handler):
        tc = request.tool_call
        if tc.get("name") not in BY_NAME: return await handler(request)
        cfg = getattr(request.runtime, "config", None) or {}
        tid = (cfg.get("configurable") or {}).get("thread_id") or cfg.get("run_id")
        key = f"{self.agent_id}:{tid}:{tc.get('id')}" if tid and tc.get("id") else None
        t = CALL_CTX.set({"key": key})
        try: return await handler(request)
        finally: CALL_CTX.reset(t)

# ------------------------------------------------------------------ the pool / catalogue
async def _agent_ws(agent_id):
    r = await A.q("SELECT workspace_id FROM agents WHERE id=%s", (agent_id,), one=True)
    return (r or {}).get("workspace_id") or 1

def _allowed(key, m): return m.get(key.split(":")[0]) in ("read", "write") and (not SPECS[key][0] or m.get(key.split(":")[0]) == "write")

async def pool(agent_id) -> dict:
    """Tools of the agent's workspace (only the enabled kinds; write tools only in 'write' mode). Used by deepagent_api.build_agent."""
    m = await modes(await _agent_ws(agent_id))
    return {k: _tool(k, agent_id) for k in SPECS if _allowed(k, m)}

async def catalog(ws=None, user=None) -> list:
    """Catalogue rows (studio._catalog / /api/toolcatalog) for the current workspace. Aliases (old keys) are listed with alias_of."""
    import admin
    user = user if user is not None else (admin.cur() or {})
    if admin.is_demo(user): return []
    m = await modes(ws or admin.cur_ws())
    return [{"key": k, "description": SPECS[k][1][:200], "read_only": not SPECS[k][0], "enabled": True, "tier": META[k].tier, **({"alias_of": META[k].alias_of} if META[k].alias_of else {})}
            for k in SPECS if _allowed(k, m)]

# ------------------------------------------------------------------ tool families register themselves here (import AFTER everything above exists)
import workspace_tools_google, workspace_tools_ms  # noqa: E402,F401

def __getattr__(n):   # computed on access: the family modules may still be registering when this module is first imported from one of them
    if n == "WRITE_NAMES": return {m.name for m in BY_NAME.values() if m.tier != READ}   # every tool that can write (☆ and ★, old names included); the approval rule is needs_approval()
    raise AttributeError(n)
