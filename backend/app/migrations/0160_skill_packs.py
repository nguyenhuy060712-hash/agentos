"""GĐ12.86 Skills on one model: a Skill is a Context Hub package (`context_packs`) with `kind='skill'` (NEN_TANG_CHUNG F14). Adds to context_packs:
  kind (agent | skill | template), owner_agent_id (the one agent that may use a PRIVATE skill), visibility (private | workspace | public; public is stored only),
  source (manual | import | ai | ...), lint (last lint result), created_by_id (users.id of the creator: only the creator or an admin edits a shared skill),
  archived (soft delete: hidden from lists, still resolves for agents that already use it) + index (workspace_id, kind).
Existing packs mirrored from a legacy `contexts` row of kind 'skill' become kind='skill'. context_packs already has RLS (admin.RLS_DIRECT); no new table.
down(): drops the index and the columns."""
VERSION = "0160"
NAME = "skill_packs"

COLS = ["kind", "owner_agent_id", "visibility", "source", "lint", "created_by_id", "archived"]

async def up(q):
    await q("""ALTER TABLE context_packs
      ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'agent' CHECK (kind IN ('agent','skill','template')),
      ADD COLUMN IF NOT EXISTS owner_agent_id INT,
      ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'workspace' CHECK (visibility IN ('private','workspace','public')),
      ADD COLUMN IF NOT EXISTS source TEXT,
      ADD COLUMN IF NOT EXISTS lint JSONB,
      ADD COLUMN IF NOT EXISTS created_by_id INT,
      ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT false""")
    await q("CREATE INDEX IF NOT EXISTS context_packs_ws_kind ON context_packs(workspace_id, kind)")
    if (await q("SELECT to_regclass('contexts')"))[0][0]:
        await q("UPDATE context_packs p SET kind='skill' FROM contexts c WHERE c.id=p.legacy_context_id AND c.kind='skill' AND p.kind='agent'")

async def down(q):
    await q("DROP INDEX IF EXISTS context_packs_ws_kind")
    await q("ALTER TABLE context_packs " + ", ".join(f"DROP COLUMN IF EXISTS {c}" for c in COLS))

async def plan(q):
    return ["ALTER TABLE context_packs ADD kind (agent|skill|template, default agent), owner_agent_id, visibility (private|workspace|public, default workspace), source, lint JSONB, created_by_id, archived",
            "CREATE INDEX context_packs_ws_kind (workspace_id, kind)",
            "UPDATE context_packs SET kind='skill' for packs mirrored from legacy contexts of kind 'skill'"]
