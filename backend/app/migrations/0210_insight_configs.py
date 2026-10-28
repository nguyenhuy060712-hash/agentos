"""GĐ13 (12.42) Insights configurations. One row = one saved way to build an Insights report (which projects, how many days, filters, language, optional schedule).
  insight_configs: workspace_id (RLS_DIRECT); schedule_cron / schedule_tz / run_once_at are read by `evals.is_due` (cron NULL + run_once_at NULL = manual only);
                   created_by is TEXT ('migration:0210' = row made by this file, 'switch' = made by the old workspace weekly switch, else the user id).
  insight_reports: + config_id (the config that made it), name, notes, findings (used by 12.43).
Data step: every workspace with `workspaces.insights_weekly = true` gets one config "Báo cáo tuần" (topics, '0 8 * * 1', Asia/Ho_Chi_Minh, 7 days, enabled).
down(): deletes the rows made by this file (the old `insights_weekly` switch is untouched, so the old weekly behaviour keeps working), drops the table and the new report columns."""
VERSION = "0210"
NAME = "insight_configs"

async def up(q):
    await q("""CREATE TABLE IF NOT EXISTS insight_configs(
      id SERIAL PRIMARY KEY, workspace_id INT NOT NULL, name TEXT NOT NULL, project_ids INT[] NOT NULL DEFAULT '{}',
      mode TEXT NOT NULL DEFAULT 'topics' CHECK (mode IN ('topics','traces')), sample_size INT NOT NULL DEFAULT 1000 CHECK (sample_size BETWEEN 1 AND 2000),
      range_days INT NOT NULL DEFAULT 7 CHECK (range_days BETWEEN 1 AND 365), filter JSONB NOT NULL DEFAULT '{}', summary_prompt TEXT,
      attributes JSONB NOT NULL DEFAULT '[]', lang TEXT NOT NULL DEFAULT 'vi', model TEXT, schedule_cron TEXT, schedule_tz TEXT NOT NULL DEFAULT 'Asia/Ho_Chi_Minh',
      run_once_at TIMESTAMPTZ, enabled BOOLEAN NOT NULL DEFAULT true, last_run TIMESTAMPTZ, created_by TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    await q("CREATE INDEX IF NOT EXISTS insight_configs_ws ON insight_configs(workspace_id, id)")
    await q("ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS insights_weekly BOOLEAN NOT NULL DEFAULT false")
    await q("""DO $$ BEGIN IF to_regclass('public.insight_reports') IS NOT NULL THEN
      ALTER TABLE insight_reports ADD COLUMN IF NOT EXISTS config_id INT, ADD COLUMN IF NOT EXISTS name TEXT, ADD COLUMN IF NOT EXISTS notes TEXT, ADD COLUMN IF NOT EXISTS findings JSONB;
      END IF; END $$""")
    await q("""INSERT INTO insight_configs(workspace_id,name,mode,range_days,lang,schedule_cron,schedule_tz,enabled,created_by)
      SELECT id,'Báo cáo tuần','topics',7,'vi','0 8 * * 1','Asia/Ho_Chi_Minh',true,'migration:0210' FROM workspaces WHERE insights_weekly""")

async def down(q):
    await q("DELETE FROM insight_configs WHERE created_by='migration:0210'")
    await q("""DO $$ BEGIN IF to_regclass('public.insight_reports') IS NOT NULL THEN
      ALTER TABLE insight_reports DROP COLUMN IF EXISTS config_id, DROP COLUMN IF EXISTS name, DROP COLUMN IF EXISTS notes, DROP COLUMN IF EXISTS findings;
      END IF; END $$""")
    await q("DROP TABLE IF EXISTS insight_configs")

async def plan(q):
    return ["CREATE TABLE insight_configs (workspace_id, name, project_ids, mode topics|traces, sample_size <= 2000, range_days, filter, summary_prompt, attributes, lang, model, "
            "schedule_cron, schedule_tz, run_once_at, enabled, last_run, created_by) (RLS_DIRECT by admin.rls_init)",
            "ALTER TABLE insight_reports ADD config_id, name, notes, findings",
            "INSERT one config 'Báo cáo tuần' (0 8 * * 1, ICT) per workspace with insights_weekly = true"]
