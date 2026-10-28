"""GĐ10.28b experiments + eval_runs get their own workspace_id. Before: RLS_CHILD let a row with dataset_id / agent_id NULL through to EVERY workspace.
Now: the column is NOT NULL, filled by a BEFORE INSERT trigger (dataset / agent / test set of the row, else the session's workspace, else 1) and the policy
in admin.RLS_CHILD requires workspace_id = aos_ws(). Old rows: back-filled from the dataset / agent / test set; a row that names none of them -> workspace 1
(the default workspace, same convention as admin.rls_init for RLS_DIRECT tables)."""
VERSION = "0006"
NAME = "experiments_evalruns_workspace"

# the policies admin.rls_init installed before this migration (kept so down() leaves working tables for the older code)
_OLD = {"experiments": "aos_ws() IS NULL OR (dataset_id IS NULL OR dataset_id IN (SELECT id FROM datasets))",
        "eval_runs": "aos_ws() IS NULL OR (agent_id IS NULL OR agent_id IN (SELECT id FROM agents))"}

_FN = {
 "experiments": """
CREATE OR REPLACE FUNCTION aos_fill_ws_experiments() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $f$
BEGIN
  IF NEW.workspace_id IS NULL THEN
    NEW.workspace_id := coalesce(
      (SELECT d.workspace_id FROM datasets d WHERE d.id = NEW.dataset_id),
      (SELECT a.workspace_id FROM agents a WHERE a.id = CASE WHEN NEW.target->>'agent_id' ~ '^[0-9]{1,9}$' THEN (NEW.target->>'agent_id')::int END),
      aos_ws(), 1);
  END IF;
  RETURN NEW;
END $f$""",
 "eval_runs": """
CREATE OR REPLACE FUNCTION aos_fill_ws_eval_runs() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $f$
BEGIN
  IF NEW.workspace_id IS NULL THEN
    NEW.workspace_id := coalesce(
      (SELECT a.workspace_id FROM agents a WHERE a.id = NEW.agent_id),
      (SELECT s.workspace_id FROM test_runs r JOIN test_sets s ON s.id = r.set_id WHERE r.id = NEW.test_run_id),
      aos_ws(), 1);
  END IF;
  RETURN NEW;
END $f$"""}

async def _has(q, t, c): return bool(await q("SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name=%s AND column_name=%s", (t, c)))

async def up(q):
    for t in ("experiments", "eval_runs"):
        await q(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS workspace_id INT")
        await q(_FN[t])
        await q(f"DROP TRIGGER IF EXISTS aos_fill_ws ON {t}")
        await q(f"CREATE TRIGGER aos_fill_ws BEFORE INSERT ON {t} FOR EACH ROW EXECUTE FUNCTION aos_fill_ws_{t}()")
    # old rows (a fresh database has none and may not have datasets/agents.workspace_id yet: those steps are skipped)
    if await _has(q, "datasets", "workspace_id"):
        await q("UPDATE experiments x SET workspace_id = d.workspace_id FROM datasets d WHERE x.workspace_id IS NULL AND d.id = x.dataset_id")
    if await _has(q, "agents", "workspace_id"):
        await q("UPDATE experiments x SET workspace_id = a.workspace_id FROM agents a WHERE x.workspace_id IS NULL AND x.target->>'agent_id' ~ '^[0-9]{1,9}$' AND a.id = (x.target->>'agent_id')::int")
        await q("UPDATE eval_runs r SET workspace_id = a.workspace_id FROM agents a WHERE r.workspace_id IS NULL AND a.id = r.agent_id")
        if await _has(q, "test_sets", "workspace_id"):
            await q("UPDATE eval_runs r SET workspace_id = s.workspace_id FROM test_runs tr JOIN test_sets s ON s.id = tr.set_id WHERE r.workspace_id IS NULL AND tr.id = r.test_run_id")
    for t in ("experiments", "eval_runs"):
        await q(f"UPDATE {t} SET workspace_id = 1 WHERE workspace_id IS NULL")
        await q(f"ALTER TABLE {t} ALTER COLUMN workspace_id SET NOT NULL")

async def down(q):
    for t in ("experiments", "eval_runs"):
        await q(f"DROP POLICY IF EXISTS aos_ws ON {t}")   # the current policy reads workspace_id: drop it before the column
        await q(f"DROP TRIGGER IF EXISTS aos_fill_ws ON {t}")
        await q(f"DROP FUNCTION IF EXISTS aos_fill_ws_{t}()")
        await q(f"ALTER TABLE {t} DROP COLUMN IF EXISTS workspace_id")
        await q(f"CREATE POLICY aos_ws ON {t} USING ({_OLD[t]}) WITH CHECK ({_OLD[t]})")

async def plan(q):
    n = {t: (await q(f"SELECT count(*) FROM {t}"))[0][0] for t in ("experiments", "eval_runs")}
    return ["ALTER TABLE experiments, eval_runs ADD workspace_id INT NOT NULL + BEFORE INSERT trigger that fills it",
            f"back-fill workspace_id from dataset / agent / test set for {n['experiments']} experiments and {n['eval_runs']} eval_runs (none found -> workspace 1)"]
