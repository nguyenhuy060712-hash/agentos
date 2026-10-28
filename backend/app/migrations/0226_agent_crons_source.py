"""GĐ13.28 agent-made schedules on agent_crons: source ('ui' = old behaviour | 'agent' | 'file'), owner_user_id (the person the agent was talking to), thread_id (continue this
chat thread when new_thread is false), end_time (13.27 field), paused. created_by is also set (the chat schedules page already reads it). Existing rows get source 'ui' and NULLs.
down(): drops the columns (created_by stays: chat_gd5b owns it)."""
VERSION = "0226"
NAME = "agent_crons_source"
COLS = (("source", "TEXT NOT NULL DEFAULT 'ui'"), ("owner_user_id", "INT"), ("thread_id", "TEXT"), ("end_time", "TIMESTAMPTZ"), ("paused", "BOOLEAN NOT NULL DEFAULT false"))

async def up(q):
    for c, t in COLS: await q(f"ALTER TABLE agent_crons ADD COLUMN IF NOT EXISTS {c} {t}")
    await q("ALTER TABLE agent_crons ADD COLUMN IF NOT EXISTS created_by INT")

async def down(q):
    for c, _ in reversed(COLS): await q(f"ALTER TABLE agent_crons DROP COLUMN IF EXISTS {c}")

async def plan(q):
    return [f"ALTER TABLE agent_crons ADD {c} {t}" for c, t in COLS]
