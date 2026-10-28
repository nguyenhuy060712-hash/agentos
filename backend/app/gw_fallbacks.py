"""GĐ11.13 fallback chains v2. Which models the gateway tries after a model fails, and which failures count.
Order of precedence for a call whose first model is P: the workspace's chain row for P (this module, admin API /api/gw/fallbacks) > the agent's own
`fallback_models` (or the caller's list) > gateway.DEFAULT_FALLBACKS. Demo workspaces are still cut to Gemini last (gateway._usable -> demo_chain).
A failure is retryable when its HTTP status (openai / anthropic `status_code`, `response.status_code`, google-genai `code`) is in `trigger_statuses`
(400 / 401 / 403 / 404 never), or it is a transport / timeout error and `on_transport_error`; errors without a status keep the old string match (gateway.RETRYABLE)."""
import time, logging
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from psycopg.types.json import Jsonb

log = logging.getLogger("aos.gwfallback")
router = APIRouter()
DEFAULT_STATUSES = [429, 500, 502, 503, 504, 529]   # 529 = Anthropic overloaded
MAX_CANDIDATES = 5
_TTL = 30
_CACHE: dict = {}   # ws -> (loaded_at, {canonical primary: cfg})
TRANSPORT = {"TimeoutError", "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout", "TimeoutException", "ConnectError", "ConnectionError", "ConnectionResetError",
             "APIConnectionError", "APITimeoutError", "RemoteProtocolError", "ReadError", "NetworkError", "DeadlineExceeded", "ServerDisconnectedError", "ClientConnectionError"}
DEFAULT_CFG = {"trigger_statuses": DEFAULT_STATUSES, "on_transport_error": True, "header_timeout_s": None}   # no chain row: legacy behaviour, models keep their 120 s timeout

async def _q(*a, **k):
    import deepagent_api as A
    return await A.q(*a, **k)

def _admin():
    import admin; u = admin.cur()
    if not u or u.get("role") != "admin" or u.get("demo"): raise HTTPException(403, "Chỉ admin quản lý chuỗi dự phòng mô hình")
    return u

def invalidate(ws=None):
    if ws is None: _CACHE.clear()
    else: _CACHE.pop(int(ws), None)

def status_of(exc):
    """HTTP status carried by a provider SDK error (openai / anthropic: status_code, response.status_code; google-genai: code), else None."""
    for v in (getattr(exc, "status_code", None), getattr(getattr(exc, "response", None), "status_code", None), getattr(exc, "code", None)):
        if isinstance(v, int) and 100 <= v <= 599: return v
    return None

def mark_status(exc):
    """Status used for the model cool-down (gateway._mark): the HTTP status, or 504 for a transport / timeout error."""
    return status_of(exc) or (504 if is_transport(exc) else None)

def is_transport(exc):
    return any(c.__name__ in TRANSPORT for c in type(exc).__mro__)

def is_retryable(exc, cfg=None):
    """True when the failure is worth the next attempt / candidate. cfg: a chain row (or DEFAULT_CFG)."""
    import gateway as gw
    cfg = cfg or DEFAULT_CFG
    st = status_of(exc)
    if st is not None: return st in (cfg.get("trigger_statuses") or DEFAULT_STATUSES)
    if is_transport(exc): return bool(cfg.get("on_transport_error", True))
    s = (str(exc) or "") + " " + type(exc).__name__
    return any(k in s for k in gw.RETRYABLE)   # legacy string tokens

async def load(ws):
    """{canonical primary ref: row} of the workspace's enabled chains, cached 30 s."""
    ws = int(ws); c = _CACHE.get(ws)
    if c and time.time() - c[0] < _TTL: return c[1]
    import admin, gw_router
    async with admin.as_workspace(ws):
        rows = await _q("SELECT primary_ref, candidates, trigger_statuses, on_transport_error, header_timeout_s FROM gw_fallback_chains WHERE workspace_id=%s AND enabled", (ws,))
    got = {gw_router.canonical(r["primary_ref"], ws): dict(r) for r in rows}
    _CACHE[ws] = (time.time(), got); return got

async def chain_for(primary_ref, agent_fallbacks=None):
    """-> (chain, cfg): [primary, *candidates] and the retry settings. Workspace chain row > agent_fallbacks > DEFAULT_FALLBACKS. Never raises (a broken lookup = the old chain)."""
    import gateway as gw
    row = None; ws = gw._cur_ws()
    if ws:
        try: row = (await load(ws)).get(gw._cn(primary_ref))
        except Exception as e:  # noqa: BLE001
            log.warning("fallback chain lookup failed: %s", str(e)[:120])
    if row: return [primary_ref] + [c for c in row["candidates"] if c != primary_ref], row
    fb = gw.DEFAULT_FALLBACKS if agent_fallbacks is None else agent_fallbacks
    return [primary_ref] + [m for m in fb if m != primary_ref], DEFAULT_CFG

async def resolve_chain(chain):
    """For the three gateway paths: `chain` = the list the caller built (its first item is the primary). -> (chain, cfg)."""
    if not chain: return chain, DEFAULT_CFG
    return await chain_for(chain[0], list(chain[1:]))

# ---------------------------------------------------------------- admin API
class ChainIn(BaseModel):
    primary_ref: str
    candidates: list[str]
    trigger_statuses: list[int] | None = None
    on_transport_error: bool | None = None
    header_timeout_s: int | None = None
    enabled: bool | None = None

class ChainUpd(BaseModel):
    candidates: list[str] | None = None
    trigger_statuses: list[int] | None = None
    on_transport_error: bool | None = None
    header_timeout_s: int | None = None
    enabled: bool | None = None

async def _check(ws, primary, cands, statuses, timeout):
    import gw_router
    rows = await gw_router.rows_for(ws)
    if not cands or len(cands) > MAX_CANDIDATES: raise HTTPException(422, f"Cần 1 đến {MAX_CANDIDATES} model dự phòng")
    for r in [primary, *cands]: gw_router.http_resolve(r, ws, rows)   # unknown provider / model -> 400
    if len(set(cands)) != len(cands) or primary in cands: raise HTTPException(422, "Model dự phòng không được trùng nhau hoặc trùng model chính")
    if statuses is not None and (not statuses or any(not 400 <= s <= 599 for s in statuses)): raise HTTPException(422, "Mã lỗi phải nằm trong 400-599")
    if timeout is not None and not 1 <= timeout <= 600: raise HTTPException(422, "Thời gian chờ phản hồi từ 1 đến 600 giây")

COLS = "id, primary_ref, candidates, trigger_statuses, on_transport_error, header_timeout_s, enabled, updated_by, updated_at"

@router.get("/api/gw/fallbacks")
async def list_chains():
    import admin, gateway as gw
    ws = admin.cur_ws()
    return {"chains": await _q(f"SELECT {COLS} FROM gw_fallback_chains WHERE workspace_id=%s ORDER BY primary_ref", (ws,)),
            "defaults": {"fallbacks": gw.DEFAULT_FALLBACKS, "trigger_statuses": DEFAULT_STATUSES, "on_transport_error": True, "header_timeout_s": 60, "max_candidates": MAX_CANDIDATES}}

@router.post("/api/gw/fallbacks")
async def create_chain(b: ChainIn):
    import admin, gw_router
    u = _admin(); ws = admin.cur_ws(); primary = b.primary_ref.strip()
    await _check(ws, primary, b.candidates, b.trigger_statuses, b.header_timeout_s)
    canon = gw_router.canonical(primary, ws)
    try:
        row = await _q(f"""INSERT INTO gw_fallback_chains(primary_ref, candidates, trigger_statuses, on_transport_error, header_timeout_s, enabled, updated_by)
            VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING {COLS}""", (canon, Jsonb(b.candidates), b.trigger_statuses or DEFAULT_STATUSES,
            True if b.on_transport_error is None else b.on_transport_error, b.header_timeout_s or 60, True if b.enabled is None else b.enabled, u.get("username")), one=True)
    except Exception as e:  # noqa: BLE001
        if "unique" in str(e).lower(): raise HTTPException(409, "Model chính này đã có chuỗi dự phòng") from None
        raise
    invalidate(ws); return row

async def _row(cid, ws):
    r = await _q(f"SELECT {COLS} FROM gw_fallback_chains WHERE id=%s AND workspace_id=%s", (cid, ws), one=True)
    if not r: raise HTTPException(404, "Không có chuỗi dự phòng này")
    return r

@router.patch("/api/gw/fallbacks/{cid}")
async def update_chain(cid: int, b: ChainUpd):
    import admin
    u = _admin(); ws = admin.cur_ws(); r = await _row(cid, ws)
    f = b.model_dump(exclude_none=True)
    if not f: raise HTTPException(422, "Không có gì để đổi")
    await _check(ws, r["primary_ref"], f.get("candidates", r["candidates"]), f.get("trigger_statuses"), f.get("header_timeout_s"))
    if "candidates" in f: f["candidates"] = Jsonb(f["candidates"])
    await _q("UPDATE gw_fallback_chains SET " + ", ".join(f"{k}=%s" for k in f) + ", updated_by=%s, updated_at=now() WHERE id=%s AND workspace_id=%s", (*f.values(), u.get("username"), cid, ws))
    invalidate(ws); return await _row(cid, ws)

@router.delete("/api/gw/fallbacks/{cid}")
async def delete_chain(cid: int):
    import admin
    _admin(); ws = admin.cur_ws(); await _row(cid, ws)
    await _q("DELETE FROM gw_fallback_chains WHERE id=%s AND workspace_id=%s", (cid, ws)); invalidate(ws)
    return {"ok": True}
