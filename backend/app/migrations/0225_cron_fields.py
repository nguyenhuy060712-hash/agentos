"""GĐ13.27 schedules: standard fields on lg_crons. end_time (not due at/after it), on_run_completed ('keep' = old behaviour | 'delete' = remove the cron's
temporary thread after the run), user_id, config / context (stored; used by 13.30 durability), durability (stored). Existing rows get 'keep' and NULLs.
down(): drops the columns."""
VERSION = "0225"
NAME = "cron_fields"
COLS = (("end_time", "TIMESTAMPTZ"), ("on_run_completed", "TEXT NOT NULL DEFAULT 'keep'"), ("user_id", "TEXT"),
        ("config", "JSONB"), ("context", "JSONB"), ("durability", "TEXT"))

async def up(q):
    for c, t in COLS: await q(f"ALTER TABLE lg_crons ADD COLUMN IF NOT EXISTS {c} {t}")

async def down(q):
    for c, _ in reversed(COLS): await q(f"ALTER TABLE lg_crons DROP COLUMN IF EXISTS {c}")

async def plan(q):
    return [f"ALTER TABLE lg_crons ADD {c} {t}" for c, t in COLS]
