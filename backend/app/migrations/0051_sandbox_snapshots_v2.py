"""GĐ11.47a snapshots of virtual computers: `sandbox_snapshots` (created by ext.py for the old folder sandboxes, kind 'files') gets the columns of the two new kinds:
`capture` (volume archive of /workspace + /opt/aos-user + a committed image of the box) and `bake` (image built from an agent's setup.sh).
The name is unique per workspace for the new kinds only (old 'files' rows may legally share a name; sbx_parity.resolve_snapshot copes with that)."""
VERSION = "0051"
NAME = "sandbox_snapshots_v2"

# same table ext.DDL creates at boot; repeated so the migration also works on a bare schema
_BASE = """CREATE TABLE IF NOT EXISTS sandbox_snapshots(id TEXT PRIMARY KEY, sandbox_id TEXT, name TEXT, workspace_id INT NOT NULL, owner_id INT, bytes BIGINT,
  created TIMESTAMPTZ NOT NULL DEFAULT now())"""

_COLS = [
    ("kind", "TEXT NOT NULL DEFAULT 'files' CHECK (kind IN ('files','capture','bake'))"),
    ("archive_path", "TEXT"),
    ("archive_bytes", "BIGINT NOT NULL DEFAULT 0"),
    ("image_tag", "TEXT"),
    ("image_bytes", "BIGINT NOT NULL DEFAULT 0"),
    ("base_image", "TEXT"),
    ("base_digest", "TEXT"),
    ("setup_hash", "TEXT"),
    ("agent_id", "INT"),
    ("status", "TEXT NOT NULL DEFAULT 'ready' CHECK (status IN ('building','ready','failed'))"),
    ("build_log", "TEXT"),
]

async def up(q):
    await q(_BASE)
    for c, d in _COLS: await q(f"ALTER TABLE sandbox_snapshots ADD COLUMN IF NOT EXISTS {c} {d}")
    await q("CREATE UNIQUE INDEX IF NOT EXISTS sandbox_snapshots_name_uq ON sandbox_snapshots(workspace_id, name) WHERE kind <> 'files'")
    await q("CREATE INDEX IF NOT EXISTS sandbox_snapshots_agent_idx ON sandbox_snapshots(agent_id) WHERE agent_id IS NOT NULL")

async def down(q):
    live = (await q("SELECT count(*) FROM sandbox_snapshots WHERE kind <> 'files'"))[0][0]
    if live: raise RuntimeError(f"{live} computer snapshot(s) still exist: delete them (DELETE /api/v2/sandboxes/snapshots/<id>) before rolling back 0051")
    await q("DROP INDEX IF EXISTS sandbox_snapshots_name_uq")
    await q("DROP INDEX IF EXISTS sandbox_snapshots_agent_idx")
    for c, _ in reversed(_COLS): await q(f"ALTER TABLE sandbox_snapshots DROP COLUMN IF EXISTS {c}")

async def plan(q):
    return ["ALTER TABLE sandbox_snapshots ADD kind/archive_path/archive_bytes/image_tag/image_bytes/base_image/base_digest/setup_hash/agent_id/status/build_log (old rows: kind 'files', status 'ready')",
            "CREATE UNIQUE INDEX (workspace_id, name) WHERE kind <> 'files' + index (agent_id)"]
