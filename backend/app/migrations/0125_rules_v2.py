"""GĐ12.12 automation rules v2.
  projects:         thread_idle_min (default 10, CHECK >= 2) = the ONLY idle time of a conversation (F20; rules and the conversation judge share it);
                    thread_idle_cursor = the last moment up to which idle threads were reported (NULL = first look: starts at "now - idle", no catch-up).
  automation_rules: item_type 'runs'|'threads', environment (NULL = every environment), fb_cursor_ts (late-feedback cursor; existing rules start at the migration time),
                    last_error, updated_at.
  automation_applied(rule_id, item_key, ts): one row per (rule, run) or (rule, thread activity) already handled = "apply once". Rows older than 60 days are pruned by rules.py.
  automation_log:   batch_id, status 'ok'|'error'|'skipped', detail (+ index for GET /api/obs/rules/{id}/logs).
  feedback:         index (project_id, created) for the late-feedback query.
Row-level security of the new table and of automation_log follows automation_rules (admin.RLS_CHILD).
down(): drops the index, the table and the columns."""
VERSION = "0125"
NAME = "rules_v2"

async def up(q):
    await q("""ALTER TABLE projects
        ADD COLUMN IF NOT EXISTS thread_idle_min INT NOT NULL DEFAULT 10 CONSTRAINT projects_thread_idle_min_check CHECK (thread_idle_min >= 2),
        ADD COLUMN IF NOT EXISTS thread_idle_cursor TIMESTAMPTZ""")
    await q("""ALTER TABLE automation_rules
        ADD COLUMN IF NOT EXISTS item_type TEXT NOT NULL DEFAULT 'runs' CONSTRAINT automation_rules_item_type_check CHECK (item_type IN ('runs','threads')),
        ADD COLUMN IF NOT EXISTS environment TEXT,
        ADD COLUMN IF NOT EXISTS fb_cursor_ts TIMESTAMPTZ NOT NULL DEFAULT now(),
        ADD COLUMN IF NOT EXISTS last_error TEXT,
        ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()""")
    await q("""CREATE TABLE IF NOT EXISTS automation_applied(
        rule_id INT NOT NULL REFERENCES automation_rules(id) ON DELETE CASCADE, item_key TEXT NOT NULL, ts TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(rule_id, item_key))""")
    await q("CREATE INDEX IF NOT EXISTS automation_applied_ts ON automation_applied(ts)")
    fresh = not await q("SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='automation_log' AND column_name='status'")
    await q("""ALTER TABLE automation_log
        ADD COLUMN IF NOT EXISTS batch_id UUID,
        ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'ok' CONSTRAINT automation_log_status_check CHECK (status IN ('ok','error','skipped')),
        ADD COLUMN IF NOT EXISTS detail TEXT""")
    if fresh: await q("UPDATE automation_log SET status='error' WHERE result LIKE 'lỗi:%%'")   # rows written before this migration marked failures only in the text
    await q("CREATE INDEX IF NOT EXISTS automation_log_rule ON automation_log(rule_id, id DESC)")
    await q("CREATE INDEX IF NOT EXISTS feedback_proj_created ON feedback(project_id, created)")

async def down(q):
    await q("DROP INDEX IF EXISTS feedback_proj_created")
    await q("DROP INDEX IF EXISTS automation_log_rule")
    await q("ALTER TABLE automation_log DROP COLUMN IF EXISTS batch_id, DROP COLUMN IF EXISTS status, DROP COLUMN IF EXISTS detail")
    await q("DROP TABLE IF EXISTS automation_applied")
    await q("ALTER TABLE automation_rules DROP COLUMN IF EXISTS item_type, DROP COLUMN IF EXISTS environment, DROP COLUMN IF EXISTS fb_cursor_ts, "
            "DROP COLUMN IF EXISTS last_error, DROP COLUMN IF EXISTS updated_at")
    await q("ALTER TABLE projects DROP COLUMN IF EXISTS thread_idle_min, DROP COLUMN IF EXISTS thread_idle_cursor")

async def plan(q):
    n = (await q("SELECT count(*) FROM automation_rules"))[0][0]
    return ["ALTER TABLE projects ADD thread_idle_min (default 10, CHECK >= 2), thread_idle_cursor",
            f"ALTER TABLE automation_rules ADD item_type ('runs'|'threads', default runs), environment, fb_cursor_ts (default now), last_error, updated_at  (the {n} existing rules stay 'runs', all environments)",
            "CREATE TABLE automation_applied (rule_id -> automation_rules ON DELETE CASCADE, item_key, ts, PK(rule_id, item_key))",
            "ALTER TABLE automation_log ADD batch_id, status ('ok'|'error'|'skipped'), detail; old 'lỗi:' rows become status 'error'; index (rule_id, id DESC)",
            "CREATE INDEX feedback(project_id, created)"]
