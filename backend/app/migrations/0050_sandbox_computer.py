"""GĐ11.40 virtual computer: `sandboxes` becomes the box table of BOTH kinds (`files` = the old one-shot folder sandbox, `computer` = a long-lived gVisor box).
New columns on `sandboxes` (RLS_PRIVATE stays: workspace + owner/admin); new tables `sandbox_commands` (command history, follows its box through RLS_CHILD)
and `workspace_sandbox_quotas` (RLS_DIRECT). Old rows keep working unchanged: kind 'files', status 'running' (the column is only meaningful for computers).
The tables get their row-level-security policies from admin.rls_init at boot, which runs right after the migrations."""
VERSION = "0050"
NAME = "sandbox_computer"

# same table admin.DDL creates at boot; repeated here so the migration also works on a bare schema (IF NOT EXISTS = no-op on a real database)
_BASE = """CREATE TABLE IF NOT EXISTS sandboxes(id TEXT PRIMARY KEY, name TEXT, workspace_id INT, owner_id INT, ttl_hours INT NOT NULL DEFAULT 168,
  created TIMESTAMPTZ NOT NULL DEFAULT now(), last_used TIMESTAMPTZ NOT NULL DEFAULT now(), runs INT NOT NULL DEFAULT 0)"""

_COLS = [
    ("kind", "TEXT NOT NULL DEFAULT 'files' CHECK (kind IN ('files','computer'))"),
    ("scope", "TEXT CHECK (scope IN ('thread','agent'))"),
    ("agent_id", "INT"),
    ("user_id", "INT"),
    ("thread_id", "TEXT"),
    ("status", "TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('provisioning','running','stopped','deleted','failed'))"),
    ("size", "TEXT CHECK (size IN ('small','medium'))"),
    ("image", "TEXT"),
    ("core", "TEXT NOT NULL DEFAULT 'gvisor'"),
    ("last_error", "TEXT"),
    ("host_state", "JSONB NOT NULL DEFAULT '{}'::jsonb"),
    ("idle_ttl_s", "INT"),
    ("delete_after_stop_s", "INT"),
    ("last_active", "TIMESTAMPTZ NOT NULL DEFAULT now()"),
]

_CMDS = """CREATE TABLE IF NOT EXISTS sandbox_commands(id TEXT PRIMARY KEY,
  sandbox_id TEXT NOT NULL REFERENCES sandboxes(id) ON DELETE CASCADE, workspace_id INT NOT NULL, user_id INT,
  command TEXT NOT NULL, cwd TEXT, status TEXT NOT NULL DEFAULT 'done' CHECK (status IN ('done','timeout','error')),
  exit_code INT, truncated BOOLEAN NOT NULL DEFAULT false, duration_ms INT, started TIMESTAMPTZ NOT NULL DEFAULT now())"""
_QUOTAS = """CREATE TABLE IF NOT EXISTS workspace_sandbox_quotas(workspace_id INT PRIMARY KEY, enabled BOOLEAN NOT NULL DEFAULT true,
  max_running INT NOT NULL DEFAULT 3 CHECK (max_running >= 0), max_total INT NOT NULL DEFAULT 10 CHECK (max_total >= 0),
  internet TEXT NOT NULL DEFAULT 'off' CHECK (internet IN ('off','basic','custom')))"""

async def up(q):
    await q(_BASE)
    for c, d in _COLS: await q(f"ALTER TABLE sandboxes ADD COLUMN IF NOT EXISTS {c} {d}")
    await q("CREATE INDEX IF NOT EXISTS sandboxes_computer_idx ON sandboxes(workspace_id, kind, status)")
    await q(_CMDS)
    await q("CREATE INDEX IF NOT EXISTS sandbox_commands_box_idx ON sandbox_commands(sandbox_id, started DESC)")
    await q(_QUOTAS)

async def down(q):
    # a computer that still exists would lose its row (and its container / volumes would be orphaned): refuse, the operator deletes them first
    live = (await q("SELECT count(*) FROM sandboxes WHERE kind='computer' AND status <> 'deleted'"))[0][0]
    if live: raise RuntimeError(f"{live} virtual computer(s) still exist: delete them (DELETE /api/computers/<id>) before rolling back 0050")
    await q("DROP TABLE IF EXISTS sandbox_commands")
    await q("DROP TABLE IF EXISTS workspace_sandbox_quotas")
    await q("DELETE FROM sandboxes WHERE kind='computer'")   # only 'deleted' tombstones are left here
    await q("DROP INDEX IF EXISTS sandboxes_computer_idx")
    for c, _ in reversed(_COLS): await q(f"ALTER TABLE sandboxes DROP COLUMN IF EXISTS {c}")

async def plan(q):
    return ["ALTER TABLE sandboxes ADD kind/scope/agent_id/user_id/thread_id/status/size/image/core/last_error/host_state/idle_ttl_s/delete_after_stop_s/last_active "
            "(old rows: kind 'files', status 'running', core 'gvisor')",
            "CREATE TABLE sandbox_commands (FK sandboxes ON DELETE CASCADE) + index", "CREATE TABLE workspace_sandbox_quotas"]
