"""GĐ13.30 Durable background runs: heartbeat, sweeper (re-queue a run whose worker died), duplicate-send guard, counters, admin stats.
Flow: a queue worker claims a run (`server.queue_worker`), `server._run_task` runs it with a heartbeat (every HEARTBEAT_S) and a timeout (AOS_BG_JOB_TIMEOUT_SECS, default 86400).
If the worker dies the heartbeat stops; `sweep()` (API process, every cron tick) puts a `running` run silent for STALE_S back to `pending` (attempt+1, same row = same workspace / user /
thread) and the next claim resumes the graph from its last checkpoint with an empty input. After AOS_BG_JOB_MAX_RETRIES (default 3) retries the run becomes `error` /
`failure_after_retry`. Only a dead worker is retried: a run that ends in `error` (model or tool failure) is final, the sweeper only looks at rows still `running`.
Duplicate-send guard: `guard()` (called from mocks.AuditHooks around EVERY tool call) records (run_id, tool_call_id) in `tool_idem` before a write tool calls out and stores the result
after it; a replay of the same call (same AIMessage id + tool_call_id + same arguments) gets the stored result, a call whose outcome is unknown (killed mid-call) is NOT repeated.
Runs start with `durability="sync"` (see `stream_kw`) so the AIMessage that carries the write tool call is in the checkpoint BEFORE the tool runs.
Off for the demo workspace (no retries, no guard, plain async checkpoints)."""
import asyncio, contextvars, hashlib, json, logging, os
from fastapi import APIRouter, HTTPException

log = logging.getLogger("aos.durable")
router = APIRouter()
HEARTBEAT_S = 30                 # the executing worker touches lg_runs.heartbeat_at this often
STALE_S = 120                    # a running run without a heartbeat for this long = its worker died
RESULT_CAP = 2000                # stored tool result (plain text, deleted with the run)
CUR: contextvars.ContextVar = contextvars.ContextVar("aos_durable_run", default=None)   # {"run_id", "ws", "attempt"} of the run being executed (None outside the queue)
COUNTERS = {k: 0 for k in ("attempts", "success", "cancel", "retriable_failure", "failure_after_retry", "abandoned", "dedup")}   # in-process (this worker / API process only)

def bump(k, n=1): COUNTERS[k] = COUNTERS.get(k, 0) + n

def _env_int(name, default, lo=0):
    try: v = int(os.environ.get(name, ""))
    except ValueError: return default
    return v if v >= lo else default

def max_retries() -> int: return _env_int("AOS_BG_JOB_MAX_RETRIES", 3)
def job_timeout() -> int: return _env_int("AOS_BG_JOB_TIMEOUT_SECS", 86400, 1)
def shutdown_grace() -> int: return _env_int("AOS_BG_SHUTDOWN_GRACE_SECS", 120)

async def _q(*a, **k):   # lazy: keeps this module importable in unit tests
    import deepagent_api as A
    return await A.q(*a, **k)

def is_demo_ws(ws) -> bool:
    import gateway
    try: return bool(ws and int(ws) in gateway.DEMO_WS)
    except (TypeError, ValueError): return False

def context_for(run) -> dict | None:
    """What `CUR` holds while this run executes. None = durability off (demo workspace)."""
    if is_demo_ws(run.get("workspace_id")): return None
    return {"run_id": str(run["run_id"]), "ws": run.get("workspace_id"), "attempt": run.get("attempt") or 0}

def stream_kw() -> dict:
    """Extra astream() keywords for the run in progress: synchronous checkpoint writes when retries are on (see module doc)."""
    return {"durability": "sync"} if CUR.get() and max_retries() > 0 else {}

def resume_mode(run, ckpt_now) -> bool:
    """True = this is a re-execution of a run that already reached the graph AND wrote at least one checkpoint: resume with an empty input (the user's message, the approval
    decision and every finished step are in the checkpoint). False = start from the run's own input (first execution, or the earlier attempt died before the graph moved)."""
    sc = run.get("start_ckpt")
    if sc is None: return False
    return (ckpt_now or None) != (None if sc == "-" else sc)

# ---------------------------------------------------------------- heartbeat
async def heartbeat(rid, attempt, worker, owner):
    """Touch heartbeat_at while the run is ours. When the row says it is no longer ours (re-queued by the sweeper after a long DB outage, cancelled, claimed by another
    worker) the owning task is cancelled so the same run never executes twice."""
    while True:
        await asyncio.sleep(HEARTBEAT_S)
        try:
            r = await _q("UPDATE lg_runs SET heartbeat_at=now() WHERE run_id=%s AND status='running' AND attempt=%s AND worker=%s RETURNING 1 AS ok", (rid, attempt, worker), one=True)
        except asyncio.CancelledError: raise
        except Exception as e:  # noqa: BLE001
            log.warning("heartbeat run=%s failed: %s", rid[:8], type(e).__name__); continue
        if not r:
            if owner and not owner.done(): owner.cancel()
            return

# ---------------------------------------------------------------- sweeper
_STALE_SQL = "status='running' AND coalesce(heartbeat_at, started_at, created_at) < now() - make_interval(secs => %s)"

async def sweep(ws=None) -> dict:
    """Called by server._ttl (API process, cron tick). A run stuck in `running` with no heartbeat for STALE_S: retry (back to `pending`, attempt+1) while attempt <
    AOS_BG_JOB_MAX_RETRIES, else final `error`. `ws` limits the pass to one workspace (admin / tests). The demo workspace is never retried. A re-queued row keeps its
    workspace_id, user_id and thread: nothing is copied between workspaces."""
    mx = max_retries(); import gateway
    demo = sorted(int(w) for w in gateway.DEMO_WS) or [0]
    wsf, wsa = (" AND workspace_id=%s", (int(ws),)) if ws is not None else ("", ())
    again = await _q(f"""UPDATE lg_runs SET status='pending', attempt=attempt+1, worker=NULL, heartbeat_at=NULL, run_at=now(), failure_kind='worker_died', error=NULL
                        WHERE {_STALE_SQL}{wsf} AND attempt < %s AND workspace_id <> ALL(%s) RETURNING run_id, thread_id, workspace_id, attempt""", (STALE_S, *wsa, mx, demo))
    gone = await _q(f"""UPDATE lg_runs SET status='error', ended_at=now(),
                          failure_kind=CASE WHEN attempt > 0 AND workspace_id <> ALL(%s) THEN 'failure_after_retry' ELSE 'worker_died' END,
                          error=CASE WHEN attempt > 0 AND workspace_id <> ALL(%s) THEN 'failure_after_retry: the worker stopped ' || attempt || ' times while running this run'
                                     ELSE 'worker stopped while the run was in progress' END
                        WHERE {_STALE_SQL}{wsf} RETURNING run_id, thread_id, workspace_id, failure_kind, error""", (demo, demo, STALE_S, *wsa))
    if again:
        bump("retriable_failure", len(again)); import server; server.KICK.set()
    for r in gone or []:
        if r["failure_kind"] == "failure_after_retry": bump("failure_after_retry")
        await _finish_dead(r)
    return {"requeued": [str(r["run_id"]) for r in again or []], "failed": [str(r["run_id"]) for r in gone or []]}

async def _finish_dead(r):
    """A run given up by the sweeper / boot recovery: thread out of `busy`, stream closed so waiting clients return."""
    try:
        import server
        await _q("UPDATE lg_threads SET status='error', updated_at=now() WHERE thread_id=%s AND status='busy' AND NOT EXISTS (SELECT 1 FROM lg_runs WHERE thread_id=%s AND status IN ('pending','running'))", (r["thread_id"], r["thread_id"]))
        rid = str(r["run_id"])
        await server._emit(rid, "error", {"error": r.get("failure_kind") or "worker_died", "message": r.get("error")}); await server._emit(rid, "end", None)
    except Exception as e:  # noqa: BLE001
        log.warning("finish dead run failed: %s", type(e).__name__)

async def recover_boot(worker) -> list:
    """Process start: this process's own `running` rows are certainly dead (it just started). Retry them (same rules as sweep) instead of failing them as before.
    Rows without a worker name (written before 13.20) and the demo workspace keep the old behaviour: `error`."""
    mx = max_retries(); import gateway
    demo = sorted(int(w) for w in gateway.DEMO_WS) or [0]
    again = await _q("""UPDATE lg_runs SET status='pending', attempt=attempt+1, worker=NULL, heartbeat_at=NULL, run_at=now(), failure_kind='worker_died', error=NULL
                        WHERE status='running' AND worker=%s AND attempt < %s AND workspace_id <> ALL(%s) RETURNING run_id, thread_id""", (worker, mx, demo))
    if again: bump("retriable_failure", len(again))
    gone = await _q("""UPDATE lg_runs SET status='error', ended_at=now(),
                        failure_kind=CASE WHEN attempt > 0 AND workspace_id <> ALL(%s) THEN 'failure_after_retry' ELSE 'worker_died' END,
                        error=CASE WHEN attempt > 0 AND workspace_id <> ALL(%s) THEN 'failure_after_retry: the worker stopped ' || attempt || ' times while running this run'
                                   ELSE 'worker restarted while the run was in progress' END
                      WHERE status='running' AND (worker=%s OR worker IS NULL) RETURNING thread_id""", (demo, demo, worker))
    for r in gone or []: await _q("UPDATE lg_threads SET status='idle' WHERE thread_id=%s AND status='busy'", (r["thread_id"],))
    return gone or []

async def release(worker) -> list:
    """Graceful shutdown: runs this process still holds go back to `pending` (attempt unchanged: a stop is not a failure); the next claim resumes them from the checkpoint."""
    rows = await _q("""UPDATE lg_runs SET status='pending', worker=NULL, heartbeat_at=NULL, run_at=now() WHERE status='running' AND worker=%s RETURNING run_id""", (worker,))
    if rows: bump("abandoned", len(rows))
    return rows or []

async def timed_out(run):
    """AOS_BG_JOB_TIMEOUT_SECS exceeded: final `timeout` status with cancel_reason='timeout'."""
    import server
    rid = str(run["run_id"]); msg = f"timeout: the run was cancelled after {job_timeout()} seconds (AOS_BG_JOB_TIMEOUT_SECS)"
    r = await _q("UPDATE lg_runs SET status='timeout', cancel_reason='timeout', error=%s, ended_at=now() WHERE run_id=%s AND status='running' RETURNING run_id, thread_id", (msg, rid), one=True)
    if not r: return
    bump("cancel")
    await _q("UPDATE lg_threads SET status='error', updated_at=now() WHERE thread_id=%s", (r["thread_id"],))
    await server._emit(rid, "error", {"error": "Timeout", "message": msg}); await server._emit(rid, "end", None)

# ---------------------------------------------------------------- duplicate-send guard
def is_write_tool(name, tool=None) -> bool:
    """Tools whose repeat is visible outside: Office / mail writes (workspace_tools tier != read), the local CRM / ads / publish writes, MCP tools that are not marked read-only,
    and any name listed in AOS_DURABLE_WRITE_TOOLS (comma list: Slack / Telegram / webhook tools of a custom server)."""
    if name in {x.strip() for x in os.environ.get("AOS_DURABLE_WRITE_TOOLS", "").split(",") if x.strip()}: return True
    import workspace_tools as W, admin
    m = W.BY_NAME.get(name)
    if m is not None: return m.tier != W.READ
    if name in admin.DEMO_TOOLS_DENY: return name != "chay_code_python"
    md = (getattr(tool, "metadata", None) or {})
    if "mcp" in md:
        import mcp_registry
        return not mcp_registry._read_only(tool)
    return bool(md.get("aos_write"))

def _msgs(state):
    return (state.get("messages") if isinstance(state, dict) else getattr(state, "messages", None)) or []

def call_key(request) -> str | None:
    """Stable id of one tool call across a replay: `<AIMessage id>:<tool_call_id>` (the AIMessage is in the checkpoint, so a resumed run sees the same ids even if the
    provider reuses short tool_call_ids such as `call_1` in a later step). None = no id at all (never guarded)."""
    tcid = (request.tool_call or {}).get("id")
    if not tcid: return None
    for m in reversed(_msgs(getattr(request, "state", None))):
        if any(c.get("id") == tcid for c in (getattr(m, "tool_calls", None) or [])):
            return f"{getattr(m, 'id', None) or ''}:{tcid}"
    return f":{tcid}"

def _args_hash(tc) -> str:
    return hashlib.sha256(json.dumps([tc.get("name"), tc.get("args")], sort_keys=True, default=str).encode()).hexdigest()[:32]

def _pack(res):
    from langchain_core.messages import ToolMessage
    if isinstance(res, ToolMessage):
        c = res.content
        if not isinstance(c, str): c = json.dumps(c, ensure_ascii=False, default=str)
        return {"content": c[:RESULT_CAP], "status": getattr(res, "status", "success")}
    return {"opaque": True}   # a langgraph Command: not stored, a replay is told the action already happened

def _msg(tc, text, status="success"):
    from langchain_core.messages import ToolMessage
    return ToolMessage(content=text, tool_call_id=tc.get("id"), name=tc.get("name"), status=status)

PENDING_TEXT = ("Error: this action was already started in an earlier attempt of this run and its result is unknown, so it was NOT repeated. "
                "Check the account (for example the Sent folder) before trying again.")
OPAQUE_TEXT = "This action was already carried out in an earlier attempt of this run; it was not repeated."
REUSED_TEXT = "Error: refused. This tool call id was already used in this run with different arguments."

async def guard(request, handler):
    """Wraps one tool call (see module doc). Outside a queue run, for read tools, for the demo workspace, or without a tool call id: plain `handler(request)`."""
    from psycopg.types.json import Jsonb
    cur = CUR.get(); tc = request.tool_call or {}; name = tc.get("name")
    if not cur or not is_write_tool(name, getattr(request, "tool", None)): return await handler(request)
    key = call_key(request)
    if not key: return await handler(request)
    h = _args_hash(tc); rid = cur["run_id"]
    mine = await _q("INSERT INTO tool_idem(run_id,tool_call_id,tool,args_hash) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING tool_call_id", (rid, key, name, h), one=True)
    if not mine:
        old = await _q("SELECT args_hash, result FROM tool_idem WHERE run_id=%s AND tool_call_id=%s", (rid, key), one=True) or {}
        if old.get("args_hash") != h: return _msg(tc, REUSED_TEXT, "error")
        bump("dedup"); res = old.get("result")
        if res is None: return _msg(tc, PENDING_TEXT, "error")          # started, never finished: unknown outcome -> never repeat
        if res.get("opaque"): return _msg(tc, OPAQUE_TEXT)
        return _msg(tc, res.get("content") or "", res.get("status") or "success")
    try:
        out = await handler(request)
    except Exception:   # a definite failure (or an approval pause): nothing was sent, let a later attempt try again
        try: await _q("DELETE FROM tool_idem WHERE run_id=%s AND tool_call_id=%s AND result IS NULL", (rid, key))
        except Exception: pass  # noqa: BLE001
        raise
    # a BaseException (the task was killed / cancelled mid-call) leaves the row without a result on purpose: the outcome is unknown
    await _q("UPDATE tool_idem SET result=%s WHERE run_id=%s AND tool_call_id=%s", (Jsonb(_pack(out)), rid, key))
    return out

# ---------------------------------------------------------------- admin stats
def _admin():
    import admin
    u = admin.cur()
    if not u or u.get("role") != "admin" or u.get("demo") or is_demo_ws(u.get("ws")): raise HTTPException(403, "Chỉ admin xem thống kê run / admin only")
    return u

@router.get("/api/admin/runs/stats")
async def runs_stats():
    """In-process counters of THIS process (a worker process has its own) + the caller's workspace queue snapshot (RLS + explicit workspace filter)."""
    u = _admin(); ws = int(u["ws"])
    rows = await _q("SELECT status, count(*) n FROM lg_runs WHERE workspace_id=%s GROUP BY status", (ws,))
    kinds = await _q("SELECT failure_kind, count(*) n FROM lg_runs WHERE workspace_id=%s AND failure_kind IS NOT NULL GROUP BY failure_kind", (ws,))
    retried = await _q("SELECT count(*) n FROM lg_runs WHERE workspace_id=%s AND attempt > 0", (ws,), one=True)
    return {"process": os.environ.get("AOS_WORKER", "api"), "counters": dict(COUNTERS), "workspace": {"by_status": {r["status"]: r["n"] for r in rows}, "failure_kinds": {r["failure_kind"]: r["n"] for r in kinds},
            "retried_runs": retried["n"]}, "limits": {"max_retries": max_retries(), "job_timeout_s": job_timeout(), "shutdown_grace_s": shutdown_grace(), "heartbeat_s": HEARTBEAT_S, "stale_s": STALE_S}}
