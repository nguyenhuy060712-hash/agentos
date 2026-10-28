"""GĐ13.25 Retention cleanup (anti disk-full). Default = OFF: with no settings row nothing is deleted except the run events (lg_run_events) after 14 days
(or AOS_RESUMABLE_STREAM_TTL_SECONDS seconds when that env is set), exactly the old behaviour.
Per workspace, admin only (`/api/admin/retention`, never the demo workspace):
  checkpoint_days        checkpoints of threads idle longer than N days are deleted (the thread row, its messages and files stay; the next message starts a fresh state)
  checkpoint_keep_latest checkpoints of threads idle for 1 hour+ are pruned to the latest one per namespace (the thread still resumes)
  run_events_days        run events older than N days (NULL = env / 14 days)
  thread_events_days     reserved for the thread-event table (GĐ16): stored only
Uses the saver's own API (adelete_thread / aprune), never SQL on the checkpoint tables. Threads that are busy, have a pending/running run or wait for an approval
(status 'interrupted') are never cleared. Every query is filtered by the workspace id; the log carries counts only, never content."""
import logging, os, time
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger("aos.retention")
router = APIRouter()
DEFAULT_EVENT_DAYS = 14
BATCH = 50                      # threads per saver call
SWEEP_LIMIT = 500               # threads per workspace per pass (the rest waits for the next pass)
KEEP_LATEST_IDLE_MIN = 60       # keep_latest only touches threads idle this long
SWEEP_EVERY_S = 300             # checkpoint sweep cadence inside the worker loop (the event cleanup runs on every call, as before)
MAX_DAYS = 3650
_last = [0.0]

async def _q(*a, **k):   # lazy: unit tests replace it
    import deepagent_api as A
    return await A.q(*a, **k)

def _saver():
    import deepagent_api as A
    return A.S.get("cp")

def is_demo_ws(ws) -> bool:
    import gateway
    return bool(ws and int(ws) in gateway.DEMO_WS)

def default_event_seconds() -> int:
    try: v = int(os.environ.get("AOS_RESUMABLE_STREAM_TTL_SECONDS", ""))
    except ValueError: v = 0
    return v if v > 0 else DEFAULT_EVENT_DAYS * 86400

async def get_settings(ws) -> dict:
    r = await _q("SELECT * FROM retention_settings WHERE workspace_id=%s", (int(ws),), one=True) or {}
    out = {"workspace_id": int(ws), "thread_events_days": r.get("thread_events_days"), "checkpoint_keep_latest": bool(r.get("checkpoint_keep_latest")),
           "checkpoint_days": r.get("checkpoint_days"), "run_events_days": r.get("run_events_days"), "updated_by": r.get("updated_by"),
           "updated_at": r.get("updated_at"), "last_run_at": r.get("last_run_at"), "last_counts": r.get("last_counts")}
    out["run_events_seconds"] = out["run_events_days"] * 86400 if out["run_events_days"] else default_event_seconds()
    out["auto_delete"] = bool(out["checkpoint_keep_latest"] or out["checkpoint_days"])
    return out

# ---------------------------------------------------------------- queries (always one workspace)
_IDLE = ("t.workspace_id=%(ws)s AND t.agent_id IS NOT NULL AND t.status NOT IN ('busy','interrupted') "
         "AND EXISTS (SELECT 1 FROM agents a WHERE a.id=t.agent_id AND a.workspace_id=t.workspace_id) "
         "AND NOT EXISTS (SELECT 1 FROM lg_runs x WHERE x.thread_id=t.thread_id AND x.status IN ('pending','running'))")
_CLEAR = f"SELECT t.thread_id, t.agent_id FROM lg_threads t WHERE {_IDLE} AND t.updated_at < now() - make_interval(days => %(d)s) AND (t.retention_cleared_at IS NULL OR t.retention_cleared_at < t.updated_at)"
_PRUNE = (f"SELECT t.thread_id, t.agent_id FROM lg_threads t WHERE {_IDLE} AND t.updated_at < now() - make_interval(mins => %(m)s) "
          "AND (t.retention_pruned_at IS NULL OR t.retention_pruned_at < t.updated_at) AND (t.retention_cleared_at IS NULL OR t.retention_cleared_at < t.updated_at)")
_EV_WHERE = "r.workspace_id=%(ws)s AND r.ended_at < now() - make_interval(secs => %(s)s)"

def _cp_id(t): return f"a{t['agent_id']}:{t['thread_id']}"

async def _count(sql, args) -> int:
    return int((await _q(f"SELECT count(*) n FROM ({sql}) z", args, one=True))["n"])

async def preview(ws) -> dict:
    """Counts only, deletes nothing. Same selection as sweep()."""
    s = await get_settings(ws); out = {"workspace_id": int(ws), "demo": is_demo_ws(ws), "auto_delete": s["auto_delete"], "threads_to_clear": 0, "threads_to_prune": 0}
    if not out["demo"]:
        if s["checkpoint_days"]: out["threads_to_clear"] = await _count(_CLEAR, {"ws": int(ws), "d": s["checkpoint_days"]})
        if s["checkpoint_keep_latest"]:   # a sweep clears the old ones first, so they are not pruned any more
            out["threads_to_prune"] = await _count(_PRUNE + (" AND t.updated_at >= now() - make_interval(days => %(d)s)" if s["checkpoint_days"] else ""),
                                                   {"ws": int(ws), "m": KEEP_LATEST_IDLE_MIN, "d": s["checkpoint_days"] or 0})
    out["run_events_to_delete"] = int((await _q(f"SELECT count(*) n FROM lg_run_events e JOIN lg_runs r ON e.run_id=r.run_id WHERE {_EV_WHERE}",
                                                 {"ws": int(ws), "s": s["run_events_seconds"]}, one=True))["n"])
    return out

# ---------------------------------------------------------------- sweepers
async def sweep_run_events() -> int:
    """lg_run_events of finished runs older than the workspace's days (default env / 14 days). One statement per workspace: a workspace's rows never use another's cutoff."""
    n = 0; explicit = await _q("SELECT workspace_id, run_events_days FROM retention_settings WHERE run_events_days IS NOT NULL")
    days = {int(r["workspace_id"]): int(r["run_events_days"]) for r in explicit if not is_demo_ws(r["workspace_id"])}; skip = list(days)
    sql = "WITH d AS (DELETE FROM lg_run_events e USING lg_runs r WHERE e.run_id=r.run_id AND r.ended_at < now() - make_interval(secs => %s) AND NOT (r.workspace_id = ANY(%s::int[])) RETURNING 1) SELECT count(*) n FROM d"
    n += int((await _q(sql, (default_event_seconds(), skip), one=True))["n"])
    for ws in skip:
        r = await _q(f"WITH d AS (DELETE FROM lg_run_events e USING lg_runs r WHERE e.run_id=r.run_id AND {_EV_WHERE} RETURNING 1) SELECT count(*) n FROM d",
                     {"ws": ws, "s": days[ws] * 86400}, one=True)
        n += int(r["n"])
    return n

async def _mark(col, ids, ws):
    if ids: await _q(f"UPDATE lg_threads SET {col}=now() WHERE workspace_id=%s AND thread_id = ANY(%s)", (int(ws), ids))

async def sweep_checkpoints(ws, s=None, limit=SWEEP_LIMIT) -> dict:
    """One pass for one workspace. Returns counts (cleared, pruned, skipped, more). Demo workspace / settings off = zeros."""
    out = {"cleared": 0, "pruned": 0, "skipped": 0, "more": False}
    s = s or await get_settings(ws); cp = _saver()
    if is_demo_ws(ws) or not s["auto_delete"] or cp is None: return out
    if s["checkpoint_days"]:
        rows = await _q(_CLEAR + " ORDER BY t.updated_at LIMIT %(l)s", {"ws": int(ws), "d": s["checkpoint_days"], "l": limit + 1})
        out["more"] = len(rows) > limit; done = []
        for t in rows[:limit]:
            try: await cp.adelete_thread(_cp_id(t)); done.append(t["thread_id"])
            except Exception as e: out["skipped"] += 1; log.warning("retention ws=%s checkpoint delete failed: %s", ws, type(e).__name__)  # noqa: BLE001
        await _mark("retention_cleared_at", done, ws); out["cleared"] = len(done)
    if s["checkpoint_keep_latest"]:
        rows = await _q(_PRUNE + " ORDER BY t.updated_at LIMIT %(l)s", {"ws": int(ws), "m": KEEP_LATEST_IDLE_MIN, "l": limit + 1})
        out["more"] = out["more"] or len(rows) > limit; rows = rows[:limit]
        for i in range(0, len(rows), BATCH):
            chunk = rows[i:i + BATCH]; final = []; ok = 0
            try: await cp.aprune([_cp_id(t) for t in chunk], strategy="keep_latest"); final = [t["thread_id"] for t in chunk]; ok = len(chunk)
            except NotImplementedError:   # saver without prune, or a thread with delta channels: try one by one, mark what is final
                for t in chunk:
                    try: await cp.aprune([_cp_id(t)], strategy="keep_latest"); final.append(t["thread_id"]); ok += 1
                    except NotImplementedError: out["skipped"] += 1; final.append(t["thread_id"])   # final: will not work later either
                    except Exception as e: out["skipped"] += 1; log.warning("retention ws=%s prune failed: %s", ws, type(e).__name__)  # noqa: BLE001
            except Exception as e: out["skipped"] += len(chunk); log.warning("retention ws=%s prune failed: %s", ws, type(e).__name__)  # noqa: BLE001
            await _mark("retention_pruned_at", final, ws); out["pruned"] += ok
    return out

async def sweep_workspace(ws, user=None) -> dict:
    """Event cleanup of this workspace + checkpoint sweep (used by the manual run)."""
    s = await get_settings(ws); out = await sweep_checkpoints(ws, s)
    r = await _q(f"WITH d AS (DELETE FROM lg_run_events e USING lg_runs r WHERE e.run_id=r.run_id AND {_EV_WHERE} RETURNING 1) SELECT count(*) n FROM d",
                 {"ws": int(ws), "s": s["run_events_seconds"]}, one=True)
    out["run_events"] = int(r["n"]); await _record(ws, out); return out

async def _record(ws, out):
    if any(out.get(k) for k in ("cleared", "pruned", "run_events")):
        log.info("retention ws=%s cleared=%s pruned=%s skipped=%s run_events=%s", ws, out.get("cleared"), out.get("pruned"), out.get("skipped"), out.get("run_events"))
    from psycopg.types.json import Jsonb
    await _q("UPDATE retention_settings SET last_run_at=now(), last_counts=%s WHERE workspace_id=%s", (Jsonb({k: v for k, v in out.items() if k != "more"}), int(ws)))

async def sweep_all():
    """Called by server._ttl every pass: today's event cleanup (every call) + the opt-in checkpoint sweep (every SWEEP_EVERY_S, only workspaces that turned it on)."""
    n = await sweep_run_events()
    if n: log.info("retention run_events deleted=%s", n)
    if time.monotonic() - _last[0] < SWEEP_EVERY_S: return
    _last[0] = time.monotonic()
    for r in await _q("SELECT workspace_id FROM retention_settings WHERE checkpoint_keep_latest OR checkpoint_days IS NOT NULL"):
        ws = int(r["workspace_id"])
        if is_demo_ws(ws): continue
        try:
            out = await sweep_checkpoints(ws)
            if out["cleared"] or out["pruned"] or out["skipped"]: await _record(ws, out)
        except Exception as e: log.warning("retention sweep ws=%s failed: %s", ws, type(e).__name__)  # noqa: BLE001

# ---------------------------------------------------------------- admin API
def _admin():
    import admin
    u = admin.cur()
    if not u or u.get("role") != "admin" or u.get("demo") or is_demo_ws(u.get("ws")): raise HTTPException(403, "Chỉ admin quản lý việc dọn dữ liệu cũ / admin only")
    return u

class RetentionIn(BaseModel):
    thread_events_days: int | None = Field(default=None, ge=1, le=MAX_DAYS)
    checkpoint_keep_latest: bool | None = None
    checkpoint_days: int | None = Field(default=None, ge=1, le=MAX_DAYS)
    run_events_days: int | None = Field(default=None, ge=1, le=MAX_DAYS)

class RunIn(BaseModel):
    confirm: bool = False

@router.get("/api/admin/retention")
async def retention_get():
    u = _admin(); s = await get_settings(u["ws"])
    return {**s, "defaults": {"run_events_days": default_event_seconds() / 86400, "limits": {"batch": BATCH, "sweep_limit": SWEEP_LIMIT, "keep_latest_idle_min": KEEP_LATEST_IDLE_MIN}}}

@router.put("/api/admin/retention")
async def retention_put(b: RetentionIn):
    u = _admin(); given = {k: getattr(b, k) for k in b.model_fields_set}
    if given.get("checkpoint_keep_latest") is None: given.pop("checkpoint_keep_latest", None)   # a bool column: null is not a value
    if not given: raise HTTPException(422, "Không có trường nào để lưu / nothing to save")
    cols = list(given)
    await _q(f"INSERT INTO retention_settings(workspace_id,{','.join(cols)},updated_by,updated_at) VALUES(%s,{','.join(['%s'] * len(cols))},%s,now()) "
             f"ON CONFLICT (workspace_id) DO UPDATE SET {','.join(c + '=EXCLUDED.' + c for c in cols)}, updated_by=EXCLUDED.updated_by, updated_at=now()",
             (int(u["ws"]), *given.values(), u.get("username")))
    return await retention_get()

@router.post("/api/admin/retention/preview")
async def retention_preview():
    return await preview(_admin()["ws"])

@router.post("/api/admin/retention/run")
async def retention_run(b: RunIn):
    u = _admin()
    if not b.confirm: raise HTTPException(422, "Cần xác nhận (confirm=true): dọn dữ liệu không hoàn tác được / confirmation required")
    return await sweep_workspace(u["ws"], u)
