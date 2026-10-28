"""GĐ13.12.32 Engine settings per project. Table `engine_project_settings` (one row per project, RLS_DIRECT; no row = the project follows the workspace switch) and
workspaces.engine_enabled: existing workspaces become TRUE (behaviour unchanged), NEW workspaces default FALSE (the Engine is opt-in). engine.DDL creates the same end state.
down(): drops the table and the column."""
VERSION = "0201"
NAME = "engine_settings"

TABLE = """CREATE TABLE IF NOT EXISTS engine_project_settings(project_id INT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE, workspace_id INT NOT NULL DEFAULT 1,
  enabled BOOLEAN NOT NULL DEFAULT false, paused BOOLEAN NOT NULL DEFAULT false, analysis_level TEXT NOT NULL DEFAULT 'reduced' CHECK (analysis_level IN ('reduced','standard')),
  scan_every_min INT NOT NULL DEFAULT 60 CHECK (scan_every_min BETWEEN 10 AND 10080), preferences TEXT, last_scan TIMESTAMPTZ, updated_by TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"""

async def up(q):
    await q(TABLE)
    await q("ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS engine_enabled BOOLEAN")
    await q("UPDATE workspaces SET engine_enabled=true WHERE engine_enabled IS NULL")
    await q("ALTER TABLE workspaces ALTER COLUMN engine_enabled SET DEFAULT false, ALTER COLUMN engine_enabled SET NOT NULL")

async def down(q):
    await q("DROP TABLE IF EXISTS engine_project_settings")
    await q("ALTER TABLE workspaces DROP COLUMN IF EXISTS engine_enabled")

async def plan(q):
    return ["CREATE TABLE engine_project_settings (project_id PK, enabled, paused, analysis_level, scan_every_min, preferences, last_scan; RLS_DIRECT)",
            "ALTER TABLE workspaces ADD engine_enabled (existing rows true, new rows default false)"]
