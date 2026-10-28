"""GĐ11.38 system agents that are visible in the chat (not hidden): `agents.system_kind` ('general_chat' = "Trò chuyện chung"). One live row per (workspace, kind), enforced by
a partial unique index so two processes racing to create it cannot make a second one. NULL = a normal agent. down(): drops the index + column."""
VERSION = "0037"
NAME = "agents_system_kind"

async def up(q):
    await q("ALTER TABLE agents ADD COLUMN IF NOT EXISTS system_kind TEXT")
    await q("CREATE UNIQUE INDEX IF NOT EXISTS agents_one_system_kind ON agents(workspace_id, system_kind) WHERE system_kind IS NOT NULL AND NOT deleted")

async def down(q):
    await q("DROP INDEX IF EXISTS agents_one_system_kind")
    await q("ALTER TABLE agents DROP COLUMN IF EXISTS system_kind")

async def plan(q):
    return ["agents: ADD system_kind TEXT NULL (existing rows stay NULL)", "UNIQUE INDEX agents_one_system_kind(workspace_id, system_kind) WHERE system_kind IS NOT NULL AND NOT deleted"]
