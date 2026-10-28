"""GĐ13.12.33 Detection v2. `issues.kind` CHECK is widened with the new detector kinds: inefficiency, truncated_tool_result, guard_violation and manual (issue made by hand from one trace).
The old CHECK is looked up in pg_constraint (never guessed). Idempotent (engine.DDL creates the same end state on a fresh database).
down(): DELETES the issues of the four new kinds (their children go with them: ON DELETE CASCADE) and restores the old CHECK."""
VERSION = "0204"
NAME = "issue_kinds_v2"

OLD = ("error_cluster", "low_score", "negative_feedback")
NEW = OLD + ("inefficiency", "truncated_tool_result", "guard_violation", "manual")

def _in(kinds): return ",".join(f"'{k}'" for k in kinds)

async def _drop_kind_checks(q):
    for (n,) in await q("SELECT conname FROM pg_constraint WHERE conrelid='issues'::regclass AND contype='c' AND pg_get_constraintdef(oid) ILIKE '%%kind%%'"):
        await q(f'ALTER TABLE issues DROP CONSTRAINT "{n}"')

async def up(q):
    await _drop_kind_checks(q)
    await q(f"ALTER TABLE issues ADD CONSTRAINT issues_kind_v2 CHECK (kind IN ({_in(NEW)}))")

async def down(q):
    await _drop_kind_checks(q)
    await q(f"DELETE FROM issues WHERE kind NOT IN ({_in(OLD)})")
    await q(f"ALTER TABLE issues ADD CONSTRAINT issues_kind_check CHECK (kind IN ({_in(OLD)}))")

async def plan(q):
    return ["issues.kind CHECK widened: + inefficiency, truncated_tool_result, guard_violation, manual (down() deletes rows of those kinds)"]
