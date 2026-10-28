"""GĐ11.34 permanent agent delete: before an agent is purged its configuration is saved here (so an admin can import it again within 30 days).
Row = one deleted agent: name, the current version's config, a small `meta` (owner, identifier, visibility, version count) and who deleted it. The same table is the
"already purged" marker that makes a second DELETE idempotent. RLS: admin.RLS_DIRECT (workspace_id). A worker tick deletes rows after `expires_at` (30 days)."""
VERSION = "0035"
NAME = "agent_delete_exports"

async def up(q):
    # migrations run BEFORE admin.rls_init(): aos_ws() may not exist yet on a fresh DB (rls_init then upgrades the default to coalesce(aos_ws(),1))
    ok = (await q("SELECT to_regproc('aos_ws') IS NOT NULL"))[0][0]
    d = "coalesce(aos_ws(), 1)" if ok else "1"
    await q(f"""CREATE TABLE IF NOT EXISTS agent_delete_exports(id SERIAL PRIMARY KEY, workspace_id INT NOT NULL DEFAULT {d}, agent_id INT NOT NULL, name TEXT NOT NULL,
      config JSONB NOT NULL DEFAULT '{{}}', deleted_by INT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '30 days')""")
    await q("CREATE INDEX IF NOT EXISTS agent_delete_exports_agent ON agent_delete_exports(workspace_id, agent_id)")
    await q("CREATE INDEX IF NOT EXISTS agent_delete_exports_expires ON agent_delete_exports(expires_at)")

async def down(q):
    await q("DROP TABLE IF EXISTS agent_delete_exports")

async def plan(q):
    return ["CREATE TABLE agent_delete_exports(id, workspace_id, agent_id, name, config JSONB, deleted_by, created_at, expires_at = created_at + 30 days)",
            "(RLS policy is registered by admin.RLS_DIRECT at boot)"]
