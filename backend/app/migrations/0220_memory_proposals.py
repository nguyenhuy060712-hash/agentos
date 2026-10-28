"""GĐ13.6 two-layer memory part 2: `memory_proposals` = a write to the shared `/memories/agent/` layer that was not allowed to go through (external run, API key, cron)
is kept here until the agent owner approves or rejects it. `path` is relative to the agent layer (e.g. `/ghi_chu.md`). Carries `workspace_id` (RLS_DIRECT);
ON DELETE CASCADE removes the rows with the agent (also listed in agent_purge.PURGED). down(): drops the table."""
VERSION = "0220"
NAME = "memory_proposals"

async def up(q):
    await q("""CREATE TABLE IF NOT EXISTS memory_proposals(
      id SERIAL PRIMARY KEY, workspace_id INT NOT NULL, agent_id INT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
      path TEXT NOT NULL, content TEXT NOT NULL, reason TEXT, run_id TEXT, source TEXT,
      status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(), decided_by TEXT, decided_at TIMESTAMPTZ)""")
    await q("CREATE INDEX IF NOT EXISTS memory_proposals_agent ON memory_proposals(agent_id, status)")

async def down(q):
    await q("DROP TABLE IF EXISTS memory_proposals")

async def plan(q):
    return ["CREATE TABLE memory_proposals (agent_id -> agents ON DELETE CASCADE, path, content, status pending|approved|rejected, decided_by/at) + index (agent_id, status); RLS_DIRECT by admin.rls_init"]
