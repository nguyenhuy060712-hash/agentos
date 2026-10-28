"""GĐ10.28 feedback.project_id: a feedback row without trace_id is scoped by its project (RLS in admin.RLS_CHILD['feedback']), no longer visible to every workspace."""
VERSION = "0002"
NAME = "feedback_project_id"

# the policy admin.rls_init installed before this migration (kept here so down() leaves a working table for the older code)
_OLD_POLICY = ("aos_ws() IS NULL OR ((experiment_id IS NOT NULL AND experiment_id IN (SELECT id FROM experiments)) OR (experiment_id IS NULL AND "
               "(trace_id IS NULL OR EXISTS (SELECT 1 FROM tr_runs r WHERE r.trace_id = feedback.trace_id))))")

async def up(q):
    await q("ALTER TABLE feedback ADD COLUMN IF NOT EXISTS project_id INT")
    # old rows: the project of the run they judge. No run (deleted / fake id) -> stays NULL = invisible to workspace sessions (system sessions still see it)
    await q("UPDATE feedback f SET project_id = (SELECT r.project_id FROM tr_runs r WHERE r.id = f.run_id LIMIT 1) WHERE f.project_id IS NULL")

async def down(q):
    await q("DROP POLICY IF EXISTS aos_ws ON feedback")   # the current policy reads project_id: drop it before the column
    await q("ALTER TABLE feedback DROP COLUMN IF EXISTS project_id")
    await q(f"CREATE POLICY aos_ws ON feedback USING ({_OLD_POLICY}) WITH CHECK ({_OLD_POLICY})")

async def plan(q):
    n = (await q("SELECT count(*) FROM feedback"))[0][0]
    return ["ALTER TABLE feedback ADD project_id INT", f"back-fill project_id from tr_runs for the {n} existing feedback rows"]
