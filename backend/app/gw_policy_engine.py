"""GĐ11.14 policy engine (reduced): the ONE gate in front of every model call. `evaluate(ctx)` checks, in this order,
 (a) the workspace model allow-list (table gw_model_access, mode all | selected),
 (b) the feature's own list (gw_features.allowed: provider switched off, model outside the feature's row),
 (c) the spend caps (table spend_caps stays the storage; workspace / user / evaluator / feature / agent / provider, day / week / month / total, ICT periods).
`gateway.budget_check(ctx)` is a thin wrapper, so the old call sites keep working; the three internal model paths (agent middleware, `generate`, `lc_call`) use `gateway.gate(chain)`,
which is strict (spend read fresh for the matching caps, no CAPS_TTL lag) and RESERVES the call's estimated cost until the usage row is written, so concurrent calls cannot all slip under a
cap: a call passes only while spent + reserved < limit, hence the overspend is smaller than one call (when the estimate holds). The estimate = the workspace's largest single call in the last 24 h.
Fail closed: an error inside the engine (database down...) blocks the call (HTTP 402 "đã chặn để an toàn"), it never lets it through. Without a database pool (unit tests) only the in-memory rules run.
Demo workspaces: allow-list / feature rules are OFF (they are Gemini-only already); their spend caps still apply. Periods are counted in ICT (Vietnam time, UTC+7) and the API says so.
Selected allow-list = a model is allowed when its provider is in `providers` OR its ref is in `models` (both empty = nothing; the API refuses that). Full move of caps into gw_policies v2 = GĐ15."""
import asyncio, logging, os, time, weakref
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import gateway as gw
import gw_router

log = logging.getLogger("aos.policy")
router = APIRouter()
TZ_NAME = gw.CAPS_TZ
TZ_NOTE = {"vi": "Kỳ tính theo giờ Việt Nam (ICT, UTC+7)", "en": "Periods are counted in Vietnam time (ICT, UTC+7)"}
CACHE_S = float(os.getenv("AOS_POLICY_CACHE_S", "5"))          # allow-list / feature decisions
RES_TTL = 180.0                                                # a reservation never outlives a model call (120 s timeout) by much; a crashed call frees it by itself
EST_FLOOR = float(os.getenv("AOS_CALL_RESERVE_USD", "0"))      # minimum reserved per call (0 = only the learned estimate)
_KEY = {"workspace": "workspace_id", "user": "user_id", "evaluator": "evaluator_id", "feature": "feature", "agent": "agent_id"}

class ModelNotAllowed(HTTPException):
    """The model is outside the workspace allow-list / the feature's list: HTTP 403 (plain-text detail, like chat_settings.check_run_model, so every client shows it as is)."""
    def __init__(self, ref):
        self.ref = str(ref)[:80]
        super().__init__(403, f"Model '{self.ref}' không nằm trong danh sách cho phép của workspace này")

class PolicyUnavailable(gw.BudgetExceeded):
    """The engine could not decide (database error...): the call is blocked (fail closed), HTTP 402 like a cap."""

# ---------------------------------------------------------------- state (per process)
_ACC: dict = {}        # ws -> (loaded_at, gw_model_access row | None)
_DEC: dict = {}        # (ws, feature|None, ref) -> (decided_at, allowed bool)
_EST: dict = {}        # ws -> (loaded_at, usd)
_RES: list = []        # live reservations {"ws","ctx","ps","est","exp"}
_LOCKS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()   # event loop -> {ws: Lock}

def invalidate(ws=None):
    """Forget cached rules (the admin API calls it after a write; tests call it after writing rows directly)."""
    if ws is None: _ACC.clear(); _DEC.clear(); _EST.clear()
    else:
        ws = int(ws); _ACC.pop(ws, None); _EST.pop(ws, None)
        for k in [k for k in _DEC if k[0] == ws]: _DEC.pop(k, None)

def _lock(ws):
    d = _LOCKS.setdefault(asyncio.get_running_loop(), {})
    return d.setdefault(ws or 0, asyncio.Lock())

def _remove(r):
    try: _RES.remove(r)
    except ValueError: pass

class Ticket:
    """Result of evaluate(): `.warnings` (action=warn caps reached) and, when reserved, the claim on the caps that `await release()` frees (call it in a finally, after the usage row is written)."""
    __slots__ = ("warnings", "_res")
    def __init__(self, warnings=None, res=None): self.warnings, self._res = warnings or [], res
    async def release(self):
        r, self._res = self._res, None
        if r is None: return
        try:
            async with _lock(r["ws"]): _remove(r)
        except BaseException:   # noqa: BLE001  (cancelled while waiting for the lock: still free it, then re-raise)
            _remove(r); raise

# ---------------------------------------------------------------- (a)+(b) model rules
def _demo(ws):
    return int(ws) in gw.DEMO_WS or (gw._cur_ws() == int(ws) and gw.gemini_only())

def _feat(feature):
    """Feature name known to gw_features (aliases resolved), else None: cost-only names (gateway, deep_research...) have no model-access row."""
    if not feature: return None
    import gw_features
    f = gw_features.ALIASES.get(feature, feature)
    return f if f in gw_features.FEATURES else None

def _acc_pass(row, ws, ref):
    if not row or row.get("mode") != "selected": return True
    try: r = gw_router.resolve(ref, ws, (gw_router._ROWS.get(int(ws)) or (0, None))[1])
    except gw_router.ResolveError: return False
    ms = row.get("models") or []
    return r.provider in (row.get("providers") or []) or ref in ms or r.ref in ms

async def _acc_load(ws):
    h = _ACC.get(ws)
    if h and time.time() - h[0] < CACHE_S: return h[1]
    await gw_router.rows_for(ws)   # provider rows + custom presets, so refs resolve the way the router will
    try: row = await gw._sys("SELECT mode, providers, models FROM gw_model_access WHERE workspace_id=%s", (ws,), one=True)
    except Exception as e:  # noqa: BLE001
        if "gw_model_access" in str(e) and "does not exist" in str(e): row = None   # migration 0015 not applied yet: no allow-list can exist
        else: raise
    _ACC[ws] = (time.time(), row); return row

async def _decide(ws, f, ref):
    k = (ws, f, ref); h = _DEC.get(k)
    if h and time.time() - h[0] < CACHE_S: return h[1]
    ok = _acc_pass(await _acc_load(ws), ws, ref)
    if ok and f:
        import gw_features
        ok = await gw_features.allowed(f, ref, ws)   # provider switched off / outside the feature's row
    if len(_DEC) > 4000: _DEC.clear()
    _DEC[k] = (time.time(), ok); return ok

def model_ok(m, ws=None, feature=None):
    """Sync twin used by gateway._usable: may workspace `ws` (default: the current one) call model `m` (for `feature`)? Reads what evaluate()/prepare() loaded; a rule not loaded yet allows
    (the async gate always runs first on the call paths, and the allow-list row, once loaded, is applied to every model including the last-resort one)."""
    ws = ws or gw._cur_ws()
    if not ws: return True
    ws = int(ws)
    if ws in gw.DEMO_WS: return True
    h = _ACC.get(ws)
    if h and not _acc_pass(h[1], ws, m): return False
    d = _DEC.get((ws, _feat(feature), m))
    return d[1] if d else True

async def check_models(ws, models, feature=None):
    """Public /v1 paths (no user context): -> the models the workspace may use, in order; raises ModelNotAllowed when the FIRST (the one asked for) is not allowed."""
    ws = int(ws)
    if not gw._pool() or _demo(ws): return list(models)
    f = _feat(feature); out = []
    for i, m in enumerate(models):
        ok = await _decide(ws, f, m)
        if not ok and i == 0: raise ModelNotAllowed(m)
        if ok: out.append(m)
    return out

async def filter_models(ws, models, feature=None):
    """The models of `models` the workspace may use (order kept); no database / demo workspace = all."""
    if not gw._pool() or _demo(int(ws)): return list(models)
    f = _feat(feature)
    return [m for m in models if await _decide(int(ws), f, m)]

async def prepare(chain, feature=None, ws=None):
    """Called by gateway.gate BEFORE `_usable(chain)`: loads the model rules for every candidate (incl. the last resort) and refreshes the provider-cap blocks from the database."""
    ws = ws or gw._cur_ws()
    if not ws or not gw._pool(): return
    ws = int(ws)
    try:
        if not _demo(ws):
            f = _feat(feature)
            for m in dict.fromkeys([*[m for m in chain if m], gw.LAST_RESORT]): await _decide(ws, f, m)
        ps = sorted({gw.provider(m) for m in [*chain, gw.LAST_RESORT] if m})
        for c in await gw._sys("SELECT * FROM spend_caps WHERE enabled AND scope='provider' AND action='block' AND target = ANY(%s) AND (workspace_id IS NULL OR workspace_id=%s)", (ps, ws)):
            key = (c["workspace_id"], c["target"])
            if gw.reached(await gw.cap_spent(c), c["limit_usd"]): gw.DB_BLOCK.add(key)
            else: gw.DB_BLOCK.discard(key)
    except HTTPException: raise
    except Exception as e:  # noqa: BLE001
        log.error("policy prepare failed, blocking: %s", str(e)[:200])
        raise PolicyUnavailable(_FAIL_TEXT, {"scope": "policy", "target": "engine"}) from e

# ---------------------------------------------------------------- (c) caps
_FAIL_TEXT = "Không kiểm tra được hạn mức chi phí / quyền dùng mô hình lúc này, đã chặn lời gọi để an toàn. Thử lại sau."
LIMIT_TXT = ". Liên hệ quản trị viên để nâng trần."

def _matches(c, ctx, ws, ps):
    if c["workspace_id"] is not None and c["workspace_id"] != ws: return False
    if c["scope"] == "provider": return c["target"] in ps
    v = ctx.get(_KEY[c["scope"]])
    return v is not None and str(v) == str(c["target"])

def _reserved(c, now):
    return sum(r["est"] for r in _RES if r["exp"] > now and _matches(c, r["ctx"], r["ws"], r["ps"]))

async def _est(ws):
    if not ws: return EST_FLOOR
    h = _EST.get(ws)
    if h and time.time() - h[0] < 60: return h[1]
    r = await gw._sys("SELECT coalesce(max(cost_usd),0) c FROM gateway_usage WHERE workspace_id=%s AND ts > now() - interval '24 hours'", (ws,), one=True)
    v = max(EST_FLOOR, float(r["c"] or 0)); _EST[ws] = (time.time(), v); return v

async def _strict_caps(ws, ctx, ps):
    """Enabled caps of this workspace + the whole-machine ones, read from the database NOW; the spend is summed fresh only for the caps this call matches."""
    rows = [dict(r) for r in await gw._sys("SELECT * FROM spend_caps WHERE enabled AND (workspace_id IS NULL OR workspace_id=%s)", (ws,))]
    env = os.getenv("GEMINI_BUDGET_USD_MONTH")
    if env: rows.append({"id": None, "workspace_id": None, "scope": "provider", "target": "gemini", "period": "month", "limit_usd": float(env), "action": "block", "enabled": True})
    for r in rows:
        r["limit_usd"] = float(r["limit_usd"]); r["spent"] = await gw.cap_spent(r) if _matches(r, ctx, ws, ps) else 0.0
    return rows

def _verdict(ctx, ws, ps, caps, strict):
    """Pure and synchronous (no await): the decision and the reservation read the same reservation list, so two tasks cannot both see room for the last call."""
    now = time.time()
    for r in [r for r in _RES if r["exp"] <= now]: _remove(r)
    eff = lambda c: {**c, "spent": c["spent"] + _reserved(c, now)}   # noqa: E731
    warns = []
    for c in caps:   # scope caps
        if c["scope"] == "provider" or not _matches(c, ctx, ws, ps): continue
        e = eff(c)
        if not gw.reached(e["spent"], e["limit_usd"]): continue
        if c["action"] == "warn": warns.append(gw.cap_text(e))
        else: raise gw.BudgetExceeded("Đã chạm trần chi phí " + gw.cap_text(e) + LIMIT_TXT, gw._cap_info(e))
    for c in caps:   # provider caps: warn
        if c["scope"] != "provider" or c["action"] != "warn" or c["target"] not in ps or c["workspace_id"] not in (None, ws): continue
        e = eff(c)
        if gw.reached(e["spent"], e["limit_usd"]): warns.append(gw.cap_text(e))
    def pcap(p):   # the DB provider cap (block) that stops provider p for this workspace, with reservations counted
        for c in caps:
            if c["scope"] == "provider" and c["action"] == "block" and c["target"] == p and c["workspace_id"] in (None, ws):
                e = eff(c)
                if gw.reached(e["spent"], e["limit_usd"]): return e
    legacy = lambda p: gw.SPENT[p] >= gw.CAPS.get(p, float("inf"))   # noqa: E731  (env OPENAI_BUDGET_USD)
    if ps and all(legacy(p) or pcap(p) for p in ps):
        sp = sorted(set(ps)); txt, info = [], None
        for p in sp:
            e = None if legacy(p) else pcap(p)
            if e: txt.append(f"{gw._pname(p)} (đã dùng ${e['spent']:.2f} / trần ${e['limit_usd']:.2f} {gw.PERIOD_VI[e['period']]})"); info = info or gw._cap_info(e)
            else: txt.append(gw._prov_text(p, ws)); info = info or gw.prov_info(p, ws)
        raise gw.BudgetExceeded("Đã chạm trần chi phí " + ", ".join(txt) + LIMIT_TXT, info)
    for w in warns:
        if time.time() - gw._WARNED.get(w, 0) > 60: gw._WARNED[w] = time.time(); log.warning("spend cap warning: %s", w)
    return warns

async def _evaluate(ctx, fresh, strict, reserve):
    ws = ctx.get("workspace_id"); ws = int(ws) if ws not in (None, "") else None
    ps = ctx.get("provider") or []; ps = [ps] if isinstance(ps, str) else list(ps)
    models = [m for m in (ctx.get("models") or []) if m]
    pool = gw._pool()
    if models and ws and pool and not _demo(ws):   # (a) allow-list + (b) feature access: the model asked for must pass; the others are only dropped from the fallbacks by gateway._usable
        f = _feat(ctx.get("feature"))
        if not await _decide(ws, f, models[0]): raise ModelNotAllowed(models[0])
        for m in models[1:]: await _decide(ws, f, m)
    c2 = {"workspace_id": ws, **{k: ctx.get(k) for k in ("user_id", "evaluator_id", "feature", "agent_id")}}
    if not (strict and pool):   # (c) cached snapshot (CAPS_TTL), the pre-check callers
        caps = await gw.caps_snapshot(fresh)
        if pool and gw._SNAP.get("err"): raise PolicyUnavailable(_FAIL_TEXT, {"scope": "policy", "target": "caps"})
        return Ticket(_verdict(ctx, ws, ps, caps, False))
    if not reserve:
        return Ticket(_verdict(ctx, ws, ps, await _strict_caps(ws, c2, ps), True))
    async with _lock(ws):   # decide + reserve atomically per workspace (the LLM call itself runs outside the lock)
        est = await _est(ws); caps = await _strict_caps(ws, c2, ps)
        warns = _verdict(ctx, ws, ps, caps, True)
        r = {"ws": ws, "ctx": c2, "ps": ps, "est": est, "exp": time.time() + RES_TTL}; _RES.append(r)
        return Ticket(warns, r)

NO_TICKET = Ticket()

async def evaluate(ctx, *, fresh=False, strict=False, reserve=False):
    """The single gate. ctx: provider (name or list), workspace_id, user_id, evaluator_id, feature, agent_id, models (refs the call may use, primary first), and the private flags _strict / _reserve (set by gateway.gate). -> Ticket.
    Raises ModelNotAllowed (403), BudgetExceeded (402, a cap is reached), PolicyUnavailable (402, the engine failed: blocked)."""
    reserve = reserve or bool(ctx.get("_reserve")); strict = strict or reserve or bool(ctx.get("_strict"))   # budget_check(ctx) has no keyword for these: the gate puts them in ctx
    try:
        t = await _evaluate(ctx, fresh, strict, reserve)
        if t._res is not None: ctx["_ticket"] = t   # the caller (gateway.gate) takes it from here and releases it
        return t
    except (HTTPException, gw.SpendCapReached): raise
    except Exception as e:  # noqa: BLE001
        log.error("policy engine failed, blocking: %s", str(e)[:200])
        raise PolicyUnavailable(_FAIL_TEXT, {"scope": "policy", "target": "engine"}) from e

# ---------------------------------------------------------------- admin API
def _admin():
    import gw_features
    return gw_features._admin()   # admin, not demo, not a demo workspace

async def _q(*a, **k):
    import deepagent_api as A
    return await A.q(*a, **k)

async def _check_providers(ws, names):
    rows = await gw_router.rows_for(ws, fresh=True)
    ok = set(gw_router.PREFIX_KINDS) | set(rows)
    for n in names:
        if n not in ok: raise HTTPException(400, f"Chưa có nhà cung cấp '{str(n)[:40]}'")

class AccessIn(BaseModel):
    mode: str = Field(pattern=r"^(all|selected)$")
    providers: list[str] | None = Field(default=None, max_length=30)
    models: list[str] | None = Field(default=None, max_length=300)

@router.get("/api/gw/model-access")
async def access_get():
    u = _admin(); ws = int(u["ws"])
    r = await _q("SELECT mode, providers, models, updated_by, updated_at FROM gw_model_access WHERE workspace_id=%s", (ws,), one=True)
    rows = await gw_router.rows_for(ws)
    return {**(r or {"mode": "all", "providers": None, "models": None, "updated_by": None, "updated_at": None}), "providers_available": sorted(set(gw_router.PREFIX_KINDS) | set(rows))}

@router.put("/api/gw/model-access")
async def access_put(b: AccessIn):
    u = _admin(); ws = int(u["ws"])
    if b.mode == "selected" and not (b.providers or b.models): raise HTTPException(422, "Chọn ít nhất một nhà cung cấp hoặc một model (danh sách rỗng sẽ chặn mọi model)")
    await _check_providers(ws, b.providers or [])
    rows = await gw_router.rows_for(ws)
    for ref in b.models or []:
        try: gw_router.resolve(ref, ws, rows)
        except gw_router.ResolveError as e: raise HTTPException(400, str(e)) from None
    r = await _q("""INSERT INTO gw_model_access(workspace_id,mode,providers,models,updated_by) VALUES(%s,%s,%s,%s,%s)
      ON CONFLICT(workspace_id) DO UPDATE SET mode=EXCLUDED.mode, providers=EXCLUDED.providers, models=EXCLUDED.models, updated_by=EXCLUDED.updated_by, updated_at=now()
      RETURNING mode, providers, models, updated_by, updated_at""", (ws, b.mode, (b.providers or None) if b.mode == "selected" else None, (b.models or None) if b.mode == "selected" else None, u.get("username")), one=True)
    invalidate(ws)
    return r

class Slot(BaseModel):
    day_usd: float | None = Field(default=None, ge=0, le=1_000_000, allow_inf_nan=False)
    month_usd: float | None = Field(default=None, ge=0, le=1_000_000, allow_inf_nan=False)

class LimitsIn(Slot):
    provider_caps: dict[str, Slot] | None = None   # {provider: {day_usd, month_usd}}; a field set to null removes that cap, a field left out is untouched

async def _slot(c):
    spent = round(await gw.cap_spent(c), 6); ps = gw.period_start(c["period"])
    return {"id": c["id"], "limit_usd": float(c["limit_usd"]), "current_spend_usd": spent, "reached": gw.reached(spent, c["limit_usd"]), "action": c["action"], "enabled": c["enabled"],
            "period_start": ps.isoformat() if ps else None}

@router.get("/api/gw/limits")
async def limits_get():
    u = _admin(); ws = int(u["ws"])
    out = {"tz": TZ_NAME, "tz_note": TZ_NOTE, "workspace": {"day": None, "month": None}, "provider_caps": {}}
    for c in await _q("SELECT * FROM spend_caps WHERE workspace_id=%s AND action='block' AND period IN ('day','month') AND ((scope='workspace' AND target=%s) OR scope='provider') ORDER BY id", (ws, str(ws))):
        s = await _slot(c)
        if c["scope"] == "workspace": out["workspace"][c["period"]] = s
        else: out["provider_caps"].setdefault(c["target"], {"day": None, "month": None})[c["period"]] = s
    return out

async def _set(ws, scope, target, period, usd):
    await _q("DELETE FROM spend_caps WHERE workspace_id=%s AND scope=%s AND target=%s AND period=%s AND action='block'", (ws, scope, target, period))
    if usd is not None: await _q("INSERT INTO spend_caps(workspace_id,scope,target,period,limit_usd,action,enabled) VALUES(%s,%s,%s,%s,%s,'block',true)", (ws, scope, target, period, usd))

@router.put("/api/gw/limits")
async def limits_put(b: LimitsIn):
    u = _admin(); ws = int(u["ws"])
    await _check_providers(ws, list(b.provider_caps or {}))
    for fld, per in (("day_usd", "day"), ("month_usd", "month")):
        if fld in b.model_fields_set: await _set(ws, "workspace", str(ws), per, getattr(b, fld))
    for p, s in (b.provider_caps or {}).items():
        for fld, per in (("day_usd", "day"), ("month_usd", "month")):
            if fld in s.model_fields_set: await _set(ws, "provider", p, per, getattr(s, fld))
    await gw.caps_snapshot(fresh=True)
    return await limits_get()
