"""GĐ11.2 model-name resolver: one place that turns any model reference into (provider, model, canonical ref).
Forms: `provider/model` (provider = openai | anthropic | gemini, or the name of a workspace gw_providers row), legacy bare names
(`gemini-*` -> gemini, `gpt-*|o3*|o4*` -> openai, `claude-*` -> anthropic, anything else -> gemini exactly as before 11.2), `aos/fast|pro|max` (tiers) and
`custom/<name>` (saved model configuration, 11.11). Canonical ref = the bare model for Gemini / OpenAI (history and PRICING keys unchanged), `provider/model` for everything else.
Only a ref that contains "/" can be rejected (ResolveError, HTTP 400 text); a bare name never fails."""
import time
from dataclasses import dataclass
from fastapi import HTTPException

TIERS = {"fast": "gemini-3.1-flash-lite", "pro": "gemini-3.8-flash", "max": "gemini-3.1-pro-preview"}   # defaults; 11.11 adds the per-workspace override (TIER_OVERRIDE)
TIER_OVERRIDE: dict = {}   # ws -> {tier: ref}; filled from model_tiers by rows_for (11.11)
CUSTOM: dict = {}          # ws -> {config name: {"ref": "<provider>/<model>", "params": {...}}}; filled from model_presets by rows_for (11.11)
BARE_KINDS = ("gemini", "openai")          # canonical ref is the bare model name
PREFIX_KINDS = ("openai", "anthropic", "gemini")   # usable as `kind/model` without a workspace row of that name
V1_ONLY_GEMINI = "Chỉ hỗ trợ Gemini ở /v1"
_ROWS: dict = {}   # ws -> (loaded_at, {name: row})
_TTL = 30

class ResolveError(ValueError): pass
class ProviderKeyMissing(RuntimeError):
    """GĐ11.3: the provider of a model has no usable key / is disabled. str(e) = user text ("Thiếu khoá <nhà cung cấp>"); the app answers HTTP 424."""

@dataclass(frozen=True)
class Resolved:
    provider: str          # provider name as written (a KINDS key or a workspace gw_providers.name)
    model: str
    ref: str               # canonical ref
    row: dict | None = None
    params: dict | None = None   # custom/<name>: the saved configuration's parameters (max_tokens, top_p, temperature, reasoning_effort, extra_params, provider_api)
    @property
    def kind(self): return self.row["kind"] if self.row else self.provider

def legacy_kind(m):
    m = m or ""
    if m.startswith(("gpt-", "o3", "o4")): return "openai"
    if m.startswith("claude-"): return "anthropic"
    return "gemini"

def _canon(provider, kind, model): return model if provider == kind and kind in BARE_KINDS else f"{provider}/{model}"

def valid_forms(rows=None):
    names = sorted(set(PREFIX_KINDS) | set(rows or {}))
    return "Dạng hợp lệ: <tên model> (vd gemini-3.6-flash), <nhà cung cấp>/<model> với nhà cung cấp là " + ", ".join(names) + ", aos/fast|pro|max, custom/<tên>"

def resolve(ref, ws=None, rows=None):
    """-> Resolved. `rows` = {name: gw_providers row} of the workspace (see rows_for); None = only the built-in providers are known."""
    ref = (ref or "").strip()
    if not ref: raise ResolveError("Thiếu tên model")
    if "/" not in ref:
        k = legacy_kind(ref)
        return Resolved(k, ref, ref if k in BARE_KINDS else f"{k}/{ref}")
    pfx, model = ref.split("/", 1)
    if not pfx or not model.strip() or model != model.strip(): raise ResolveError(f"Tên model không hợp lệ. {valid_forms(rows)}")
    if pfx == "aos":
        tgt = ((TIER_OVERRIDE.get(int(ws)) if ws else None) or {}).get(model)
        if not tgt or tgt.startswith("aos/"): tgt = TIERS.get(model)   # an override must not point at another tier (no loops)
        if not tgt: raise ResolveError(f"Không có mức aos/{model[:20]}. Có: " + ", ".join("aos/" + t for t in TIERS))
        return resolve(tgt, ws, rows)
    if pfx == "custom":
        c = ((CUSTOM.get(int(ws)) if ws else None) or {}).get(model)
        if not c or c["ref"].startswith(("aos/", "custom/")): raise ResolveError(f"Chưa có cấu hình custom/{model[:40]}")
        t = resolve(c["ref"], ws, rows)
        return Resolved(t.provider, t.model, t.ref, t.row, c["params"])
    row = (rows or {}).get(pfx)
    if row: return Resolved(pfx, model, _canon(pfx, row["kind"], model), row)
    if pfx in PREFIX_KINDS: return Resolved(pfx, model, _canon(pfx, pfx, model))
    raise ResolveError(f"Chưa có nhà cung cấp '{pfx[:40]}'. {valid_forms(rows)}")

def http_resolve(ref, ws=None, rows=None):
    """resolve() with HTTPException(400) instead of ResolveError."""
    try: return resolve(ref, ws, rows)
    except ResolveError as e: raise HTTPException(400, str(e)) from None

def v1_gemini(ref, ws=None):
    """/v1/* stay Gemini-only: -> the bare Gemini model (accepts `gemini/<m>`, `aos/fast`...); any other provider -> 400."""
    r = http_resolve(ref, ws)
    if r.kind != "gemini": raise HTTPException(400, V1_ONLY_GEMINI)
    return r.model

def kind_of(ref, ws=None):
    """Sync, never raises: the provider kind of a ref (gateway.provider). Unknown refs fall back to the pre-11.2 rule."""
    try: return resolve(ref, ws, (_ROWS.get(int(ws)) or (0, None))[1] if ws else None).kind
    except ResolveError: return legacy_kind(ref)

def resolve_sync(ref, ws=None):
    """Sync resolve with the cached workspace rows (never raises: unresolvable refs get the pre-11.2 legacy kind). Used by gateway._model."""
    try: return resolve(ref, ws, (_ROWS.get(int(ws)) or (0, None))[1] if ws else None)
    except ResolveError:
        k = legacy_kind(ref); return Resolved(k, ref, ref)

def canonical(ref, ws=None):
    """Sync, never raises: canonical ref (COOLDOWN / usage key). Unresolvable refs are returned unchanged."""
    try: return resolve(ref, ws, (_ROWS.get(int(ws)) or (0, None))[1] if ws else None).ref
    except ResolveError: return ref

async def rows_for(ws, fresh=False):
    """{name: row} of the workspace's gw_providers (cached 30 s; gw_providers CRUD calls invalidate)."""
    ws = int(ws); c = _ROWS.get(ws)
    if c and not fresh and time.time() - c[0] < _TTL: return c[1]
    import admin, deepagent_api as A
    async with admin.as_workspace(ws):
        got = await A.q("SELECT id, kind, name, base_url, secret_name, enabled FROM gw_providers WHERE workspace_id=%s", (ws,))
        cfgs = await A.q("SELECT c.name, c.model, c.upstream_model, c.temperature, c.max_tokens, c.top_p, c.reasoning_effort, c.extra_params, c.provider_api, p.name AS pname "
                         "FROM model_presets c LEFT JOIN gw_providers p ON p.id=c.provider_id WHERE c.workspace_id=%s", (ws,))
        tiers = await A.q("SELECT tier, config_ref FROM model_tiers WHERE workspace_id=%s", (ws,))
    rows = {r["name"]: r for r in got}
    CUSTOM[ws] = {c["name"]: {"ref": f"{c['pname']}/{c['upstream_model']}" if c["pname"] and c["upstream_model"] else c["model"],
                              "params": {k: c[k] for k in ("temperature", "max_tokens", "top_p", "reasoning_effort", "extra_params", "provider_api") if c.get(k) is not None}} for c in cfgs}
    TIER_OVERRIDE[ws] = {t["tier"]: t["config_ref"] for t in tiers}
    _ROWS[ws] = (time.time(), rows); return rows

def invalidate(ws=None):
    if ws is None: _ROWS.clear()
    else: _ROWS.pop(int(ws), None)   # CUSTOM / TIER_OVERRIDE are refilled by the next rows_for (stale until then, at most one request)

async def aresolve(ref, ws):
    """Async resolve with the workspace's providers loaded. Raises HTTPException(400) for a bad ref."""
    return http_resolve(ref, ws, await rows_for(ws))
