"""GĐ13.25 retention: `retention_settings` = one row per workspace (RLS_DIRECT), written only through the admin API. Default = OFF: no row / NULL columns = nothing
is deleted automatically except the run events (lg_run_events) after 14 days (or AOS_RESUMABLE_STREAM_TTL_SECONDS), exactly as before.
  thread_events_days   reserved for the thread-event table of the protocol v2 card (GĐ16); stored, not swept yet
  checkpoint_keep_latest / checkpoint_days   sweep of LangGraph checkpoints of idle threads (prune to the latest | delete)
  run_events_days      NULL = default (env or 14 days)
lg_threads.retention_pruned_at / retention_cleared_at remember when a thread was last pruned / cleared so the sweeper does not repeat itself (a newer
updated_at makes the thread eligible again). down(): drops the table and the two columns."""
VERSION = "0227"
NAME = "retention_settings"

async def up(q):
    await q("""CREATE TABLE IF NOT EXISTS retention_settings(
      workspace_id INT PRIMARY KEY,
      thread_events_days INT CHECK (thread_events_days IS NULL OR thread_events_days BETWEEN 1 AND 3650),
      checkpoint_keep_latest BOOLEAN NOT NULL DEFAULT false,
      checkpoint_days INT CHECK (checkpoint_days IS NULL OR checkpoint_days BETWEEN 1 AND 3650),
      run_events_days INT CHECK (run_events_days IS NULL OR run_events_days BETWEEN 1 AND 3650),
      updated_by TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), last_run_at TIMESTAMPTZ, last_counts JSONB)""")
    await q("ALTER TABLE lg_threads ADD COLUMN IF NOT EXISTS retention_pruned_at TIMESTAMPTZ")
    await q("ALTER TABLE lg_threads ADD COLUMN IF NOT EXISTS retention_cleared_at TIMESTAMPTZ")

async def down(q):
    await q("ALTER TABLE lg_threads DROP COLUMN IF EXISTS retention_cleared_at")
    await q("ALTER TABLE lg_threads DROP COLUMN IF EXISTS retention_pruned_at")
    await q("DROP TABLE IF EXISTS retention_settings")

async def plan(q):
    return ["CREATE TABLE retention_settings (workspace_id PK, thread_events_days, checkpoint_keep_latest, checkpoint_days, run_events_days, updated_by/at, last_run_at/last_counts; RLS_DIRECT by admin.rls_init)",
            "ALTER TABLE lg_threads ADD retention_pruned_at, retention_cleared_at"]
