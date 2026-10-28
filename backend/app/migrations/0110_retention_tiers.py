"""GĐ12.18 retention tiers (base 30 days / extended 180 days) and the per-trace upgrade.
  workspaces: retention_default 'base'|'extended' (default base), retention_base_days (30), retention_extended_days (180).
  projects:   retention_tier NULL|'base'|'extended'|'custom'  (NULL = follow the workspace default);
              legacy_ttl_days = the retention_days the project had when this migration ran: the days of every run whose tr_runs.ttl_days is NULL (written before 12.18).
              Every EXISTING project becomes 'custom' with its current retention_days, so nothing changes for old data or old settings.
  trace_retention(trace_id PK, project_id, tier 'extended', keep_until, reason, set_by, set_at): a trace listed here (and not past keep_until) is never deleted by
              the retention job, whatever its runs' ttl_days say. Row-level security follows projects (admin.RLS_CHILD).
down(): drops the table and the columns (retention then falls back to projects.retention_days, which this migration never changes)."""
VERSION = "0110"
NAME = "retention_tiers"

async def up(q):
    await q("""ALTER TABLE workspaces
        ADD COLUMN IF NOT EXISTS retention_default TEXT NOT NULL DEFAULT 'base' CHECK (retention_default IN ('base','extended')),
        ADD COLUMN IF NOT EXISTS retention_base_days INT NOT NULL DEFAULT 30 CHECK (retention_base_days BETWEEN 1 AND 3650),
        ADD COLUMN IF NOT EXISTS retention_extended_days INT NOT NULL DEFAULT 180 CHECK (retention_extended_days BETWEEN 1 AND 3650)""")
    fresh = not await q("SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='projects' AND column_name='retention_tier'")
    await q("""ALTER TABLE projects
        ADD COLUMN IF NOT EXISTS retention_tier TEXT CHECK (retention_tier IN ('base','extended','custom')),
        ADD COLUMN IF NOT EXISTS legacy_ttl_days INT""")
    if fresh: await q("UPDATE projects SET retention_tier='custom', legacy_ttl_days=retention_days")   # only the projects that existed before this migration
    await q("""CREATE TABLE IF NOT EXISTS trace_retention(
        trace_id UUID PRIMARY KEY, project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        tier TEXT NOT NULL DEFAULT 'extended' CHECK (tier = 'extended'), keep_until TIMESTAMPTZ NOT NULL,
        reason TEXT NOT NULL CHECK (reason IN ('feedback','evaluator','rule','manual')), set_by TEXT, set_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    await q("CREATE INDEX IF NOT EXISTS trace_retention_proj ON trace_retention(project_id)")
    await q("CREATE INDEX IF NOT EXISTS trace_retention_until ON trace_retention(keep_until)")

async def down(q):
    await q("DROP TABLE IF EXISTS trace_retention")
    await q("ALTER TABLE projects DROP COLUMN IF EXISTS retention_tier, DROP COLUMN IF EXISTS legacy_ttl_days")
    await q("ALTER TABLE workspaces DROP COLUMN IF EXISTS retention_default, DROP COLUMN IF EXISTS retention_base_days, DROP COLUMN IF EXISTS retention_extended_days")

async def plan(q):
    n = (await q("SELECT count(*) FROM projects"))[0][0]
    return ["ALTER TABLE workspaces ADD retention_default ('base'|'extended', default base), retention_base_days (30), retention_extended_days (180)",
            "ALTER TABLE projects ADD retention_tier (NULL|base|extended|custom), legacy_ttl_days",
            f"UPDATE projects: the {n} existing projects become 'custom' with their current retention_days (nothing changes for them)",
            "CREATE TABLE trace_retention (trace_id PK, project_id -> projects ON DELETE CASCADE, tier, keep_until, reason, set_by, set_at) + 2 indexes (RLS via admin.RLS_CHILD)"]
