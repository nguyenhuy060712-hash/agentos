"""GĐ13.12.35 Engine "Test issue" (reproduce an issue). Three columns on engine_project_settings: auto_validate (replay automatically after an analysis, default true),
auto_close_not_reproduced (close the issue when every replay is `not_reproduced`, default true) and replay_safe_tools (extra read-only tool names a replay may run, on top of the
built-in read-only list; write tools can never be listed). `issues.validation` (jsonb) already exists since 0200. engine.DDL creates the same end state. down(): drops the columns."""
VERSION = "0203"
NAME = "engine_validation"

async def up(q):
    await q("ALTER TABLE engine_project_settings ADD COLUMN IF NOT EXISTS auto_validate BOOLEAN NOT NULL DEFAULT true")
    await q("ALTER TABLE engine_project_settings ADD COLUMN IF NOT EXISTS auto_close_not_reproduced BOOLEAN NOT NULL DEFAULT true")
    await q("ALTER TABLE engine_project_settings ADD COLUMN IF NOT EXISTS replay_safe_tools TEXT[] NOT NULL DEFAULT '{}'")

async def down(q):
    await q("ALTER TABLE engine_project_settings DROP COLUMN IF EXISTS auto_validate, DROP COLUMN IF EXISTS auto_close_not_reproduced, DROP COLUMN IF EXISTS replay_safe_tools")

async def plan(q):
    return ["ALTER TABLE engine_project_settings ADD auto_validate bool default true, auto_close_not_reproduced bool default true, replay_safe_tools text[] default '{}'",
            "(issues.validation jsonb is already there since 0200)"]
