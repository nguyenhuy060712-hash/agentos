"""GĐ11.23 MCP registry per workspace. mcp_servers gets workspace_id (+ optional agent_id), auth / filter / cache columns; tool_settings gets workspace_id.
Existing rows -> workspace 1, prefix=false (model-visible tool names stay exactly as before). UNIQUE(name) becomes a unique index on
(workspace_id, coalesce(agent_id,0), name), tool_settings' primary key becomes (workspace_id, server, tool). Both tables are in admin.RLS_DIRECT (policy installed by rls_init at boot).
down(): keeps the oldest row per name / (server, tool) so the old unique constraints can come back (rows of other workspaces with a clashing name are dropped)."""
VERSION = "0020"
NAME = "mcp_registry_v2"

_COLS = ["workspace_id INT NOT NULL DEFAULT 1", "agent_id INT", "auth_type TEXT NOT NULL DEFAULT 'none'", "headers JSONB", "connection_id INT",
         "include_tools TEXT[]", "exclude_tools TEXT[]", "prefix BOOLEAN NOT NULL DEFAULT false", "timeout_s INT NOT NULL DEFAULT 60",
         "sse_fallback BOOLEAN NOT NULL DEFAULT false", "reconnect BOOLEAN NOT NULL DEFAULT false", "throw_on_load_error BOOLEAN NOT NULL DEFAULT false",
         "tools_cache JSONB", "tools_cached_at TIMESTAMPTZ", "last_error TEXT", "vendor TEXT",
         "forward_headers BOOLEAN NOT NULL DEFAULT false", "trusted_idp_forward BOOLEAN NOT NULL DEFAULT false"]

async def _tables(q):
    return {r[0] for r in await q("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name IN ('mcp_servers','tool_settings')")}

async def up(q):
    have = await _tables(q)
    if "mcp_servers" in have:
        await q("ALTER TABLE mcp_servers " + ", ".join("ADD COLUMN IF NOT EXISTS " + c for c in _COLS))
        await q("ALTER TABLE mcp_servers DROP CONSTRAINT IF EXISTS mcp_servers_name_key")
        await q("CREATE UNIQUE INDEX IF NOT EXISTS mcp_servers_ws_name ON mcp_servers(workspace_id, coalesce(agent_id, 0), name)")
    if "tool_settings" in have:
        await q("ALTER TABLE tool_settings ADD COLUMN IF NOT EXISTS workspace_id INT NOT NULL DEFAULT 1")
        await q("ALTER TABLE tool_settings DROP CONSTRAINT IF EXISTS tool_settings_pkey")
        await q("ALTER TABLE tool_settings ADD PRIMARY KEY (workspace_id, server, tool)")

async def down(q):
    have = await _tables(q)
    if "mcp_servers" in have:
        await q("DROP INDEX IF EXISTS mcp_servers_ws_name")
        await q("DELETE FROM mcp_servers a USING mcp_servers b WHERE a.name = b.name AND a.id > b.id")
        await q("ALTER TABLE mcp_servers DROP CONSTRAINT IF EXISTS mcp_servers_name_key")
        await q("ALTER TABLE mcp_servers ADD CONSTRAINT mcp_servers_name_key UNIQUE (name)")
        await q("DROP POLICY IF EXISTS aos_ws ON mcp_servers")   # the policy reads workspace_id: drop it before the column
        await q("ALTER TABLE mcp_servers " + ", ".join("DROP COLUMN IF EXISTS " + c.split()[0] for c in _COLS))
    if "tool_settings" in have:
        await q("ALTER TABLE tool_settings DROP CONSTRAINT IF EXISTS tool_settings_pkey")
        await q("DELETE FROM tool_settings a USING tool_settings b WHERE a.server = b.server AND a.tool = b.tool AND a.workspace_id > b.workspace_id")
        await q("DROP POLICY IF EXISTS aos_ws ON tool_settings")
        await q("ALTER TABLE tool_settings DROP COLUMN IF EXISTS workspace_id")
        await q("ALTER TABLE tool_settings ADD PRIMARY KEY (server, tool)")

async def plan(q):
    have = await _tables(q)
    return [f"mcp_servers: ADD {len(_COLS)} columns (workspace_id, agent_id, auth_type, headers, include/exclude_tools, prefix, tools_cache ...), "
            "UNIQUE(name) -> unique index (workspace_id, coalesce(agent_id,0), name); existing rows -> workspace 1, prefix=false",
            "tool_settings: ADD workspace_id (existing rows -> 1), primary key (workspace_id, server, tool)"] if have else ["mcp_servers / tool_settings not created yet: nothing to change"]
