"""GĐ13.12.39 Engine cost ledger. Table `engine_runs` (one row per Engine model step: scan / analyse / validate / verify; RLS_DIRECT) and
engine_project_settings.cap_usd_month (NULL = no per-project cap). engine.DDL creates the same end state. down(): drops the table and the column."""
VERSION = "0202"
NAME = "engine_runs"

TABLE = """CREATE TABLE IF NOT EXISTS engine_runs(id SERIAL PRIMARY KEY, workspace_id INT NOT NULL DEFAULT 1, project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  issue_id INT, kind TEXT NOT NULL CHECK (kind IN ('scan','analyse','validate','verify')), status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','ok','error')),
  cost_usd NUMERIC(12,6) NOT NULL DEFAULT 0, started_at TIMESTAMPTZ NOT NULL DEFAULT now(), finished_at TIMESTAMPTZ, error TEXT, request_id TEXT)"""

async def up(q):
    await q(TABLE)
    await q("CREATE INDEX IF NOT EXISTS engine_runs_proj ON engine_runs(project_id, started_at)")
    await q("ALTER TABLE engine_project_settings ADD COLUMN IF NOT EXISTS cap_usd_month NUMERIC(12,4) CHECK (cap_usd_month IS NULL OR cap_usd_month >= 0)")

async def down(q):
    await q("DROP TABLE IF EXISTS engine_runs")
    await q("ALTER TABLE engine_project_settings DROP COLUMN IF EXISTS cap_usd_month")

async def plan(q):
    return ["CREATE TABLE engine_runs (id, workspace_id, project_id, issue_id, kind, status, cost_usd, started_at, finished_at, error, request_id; RLS_DIRECT)",
            "ALTER TABLE engine_project_settings ADD cap_usd_month (NULL = no cap)"]
