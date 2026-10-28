"""GĐ13.30 durable runs. lg_runs: `attempt` (0 = first execution; +1 for every automatic retry after the worker died), `heartbeat_at` (the executing worker touches it every
30 s; a `running` run silent for 2+ minutes = its worker died), `failure_kind` (worker_died | failure_after_retry | run_error: only worker_died is ever retried automatically,
model / tool errors never), `cancel_reason` ('timeout' when AOS_BG_JOB_TIMEOUT_SECS is exceeded), `start_ckpt` (the thread's checkpoint id when the first attempt reached the graph,
'-' = the thread had none; NULL = the run never reached the graph, so a retry starts from the original input; any other value = resume from the last checkpoint with empty input).
tool_idem(run_id, tool_call_id) = duplicate-send guard: a write tool records the call BEFORE calling out (no `result` yet = outcome unknown, never repeated) and stores the result
afterwards; a repeat of the same call returns the stored result. Child of lg_runs (ON DELETE CASCADE; RLS through lg_runs = admin.RLS_CHILD).
Existing rows get attempt 0 and NULLs. down(): drops the table and the columns."""
VERSION = "0230"
NAME = "run_durability"
COLS = (("attempt", "INT NOT NULL DEFAULT 0"), ("heartbeat_at", "TIMESTAMPTZ"), ("failure_kind", "TEXT"), ("cancel_reason", "TEXT"), ("start_ckpt", "TEXT"))

async def up(q):
    for c, t in COLS: await q(f"ALTER TABLE lg_runs ADD COLUMN IF NOT EXISTS {c} {t}")
    await q("""CREATE TABLE IF NOT EXISTS tool_idem(
      run_id UUID NOT NULL REFERENCES lg_runs(run_id) ON DELETE CASCADE,
      tool_call_id TEXT NOT NULL,
      tool TEXT NOT NULL,
      args_hash TEXT,
      result JSONB,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      PRIMARY KEY(run_id, tool_call_id))""")

async def down(q):
    await q("DROP TABLE IF EXISTS tool_idem")
    for c, _ in reversed(COLS): await q(f"ALTER TABLE lg_runs DROP COLUMN IF EXISTS {c}")

async def plan(q):
    return [f"ALTER TABLE lg_runs ADD {c} {t}" for c, t in COLS] + ["CREATE TABLE tool_idem (run_id FK lg_runs cascade, tool_call_id, tool, args_hash, result jsonb, created_at; PK(run_id, tool_call_id); RLS_CHILD via lg_runs)"]
