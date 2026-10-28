"""GĐ10.6 schedules: per-schedule timezone (default Asia/Ho_Chi_Minh = the old server-local time, so old rows fire at the same wall-clock time) + one-shot time."""
VERSION = "0005"
NAME = "schedule_tz"
TABLES = ("lg_crons", "agent_crons", "eval_schedules")

async def up(q):
    for t in TABLES:
        await q(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS timezone TEXT NOT NULL DEFAULT 'Asia/Ho_Chi_Minh'")
        await q(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS run_once_at TIMESTAMPTZ")

async def down(q):
    for t in TABLES:
        await q(f"ALTER TABLE {t} DROP COLUMN IF EXISTS run_once_at")
        await q(f"ALTER TABLE {t} DROP COLUMN IF EXISTS timezone")

async def plan(q):
    return [f"ALTER TABLE {t} ADD timezone TEXT NOT NULL DEFAULT 'Asia/Ho_Chi_Minh', ADD run_once_at TIMESTAMPTZ" for t in TABLES]
