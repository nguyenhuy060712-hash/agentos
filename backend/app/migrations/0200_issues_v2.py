"""GĐ13.12.30 Issue model v2 (reduced). Table `issues`: status values become open | fixing | watching | completed | ignored (old new -> open, in_progress -> fixing, fixed -> completed;
the old CHECK is looked up in pg_constraint, never guessed), new columns severity (0 urgent .. 3 low, default 2), priority_reason, category, description, close_reason, close_note,
closed_by, closed_at, validation. New child tables (RLS_CHILD, follow their issue): issue_traces (every sample trace ever linked) and issue_events (history of changes).
Idempotent (engine.DDL creates the same end state on a fresh database). down(): maps statuses back (watching -> in_progress), drops the columns and both tables."""
VERSION = "0200"
NAME = "issues_v2"

_COLS = ("severity SMALLINT NOT NULL DEFAULT 2 CHECK (severity BETWEEN 0 AND 3)", "priority_reason TEXT", "category TEXT", "description TEXT",
         "close_reason TEXT CHECK (close_reason IN ('resolved','incorrectly_flagged','not_important','not_reproduced','duplicate'))", "close_note TEXT",
         "closed_by INT", "closed_at TIMESTAMPTZ", "validation JSONB")
_DROP = ("severity", "priority_reason", "category", "description", "close_reason", "close_note", "closed_by", "closed_at", "validation")
_TRACES = """CREATE TABLE IF NOT EXISTS issue_traces(issue_id INT NOT NULL REFERENCES issues(id) ON DELETE CASCADE, run_id TEXT NOT NULL, trace_id TEXT, span_id TEXT,
  linked_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(issue_id, run_id))"""
_EVENTS = """CREATE TABLE IF NOT EXISTS issue_events(id SERIAL PRIMARY KEY, issue_id INT NOT NULL REFERENCES issues(id) ON DELETE CASCADE, ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  actor_user_id INT, source TEXT NOT NULL DEFAULT 'user' CHECK (source IN ('user','system','assistant','automation')), action TEXT NOT NULL, from_val TEXT, to_val TEXT, reason TEXT)"""

async def _drop_status_checks(q):
    for (n,) in await q("SELECT conname FROM pg_constraint WHERE conrelid='issues'::regclass AND contype='c' AND pg_get_constraintdef(oid) ILIKE '%%status%%'"):
        await q(f'ALTER TABLE issues DROP CONSTRAINT "{n}"')

async def up(q):
    await _drop_status_checks(q)
    await q("UPDATE issues SET status = CASE status WHEN 'new' THEN 'open' WHEN 'in_progress' THEN 'fixing' WHEN 'fixed' THEN 'completed' ELSE status END")
    await q("ALTER TABLE issues ALTER COLUMN status SET DEFAULT 'open'")
    await q("ALTER TABLE issues ADD CONSTRAINT issues_status_v2 CHECK (status IN ('open','fixing','watching','completed','ignored'))")
    for c in _COLS: await q("ALTER TABLE issues ADD COLUMN IF NOT EXISTS " + c)
    await q(_TRACES); await q(_EVENTS)
    await q("CREATE INDEX IF NOT EXISTS issue_events_issue ON issue_events(issue_id, ts)")
    await q("""INSERT INTO issue_traces(issue_id, run_id, trace_id) SELECT i.id, s, (SELECT t.trace_id::text FROM tr_runs t WHERE t.id::text = s LIMIT 1)
               FROM issues i, jsonb_array_elements_text(i.sample_run_ids) s ON CONFLICT DO NOTHING""")   # old rows: their samples become links

async def down(q):
    await q("DROP TABLE IF EXISTS issue_events"); await q("DROP TABLE IF EXISTS issue_traces")
    await _drop_status_checks(q)
    await q("UPDATE issues SET status = CASE status WHEN 'open' THEN 'new' WHEN 'fixing' THEN 'in_progress' WHEN 'watching' THEN 'in_progress' WHEN 'completed' THEN 'fixed' ELSE status END")
    await q("ALTER TABLE issues ALTER COLUMN status SET DEFAULT 'new'")
    await q("ALTER TABLE issues ADD CONSTRAINT issues_status_check CHECK (status IN ('new','in_progress','fixed','ignored'))")
    for c in _DROP: await q(f"ALTER TABLE issues DROP COLUMN IF EXISTS {c}")

async def plan(q):
    return ["issues.status CHECK replaced: new->open, in_progress->fixing, fixed->completed (+ watching); default 'open'",
            "issues + severity, priority_reason, category, description, close_reason, close_note, closed_by, closed_at, validation",
            "CREATE TABLE issue_traces (issue_id + run_id PK, cascade) filled from sample_run_ids; CREATE TABLE issue_events (history, cascade) (+ RLS_CHILD by admin.rls_init)"]
