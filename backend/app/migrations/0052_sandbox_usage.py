"""GĐ11.42 usage metering of the virtual computers: one row per (hour, box), filled by the 60 s sampler (cpu / memory / running seconds) and by the
disk watchdog (peak disk bytes). No foreign key to `sandboxes` on purpose: the numbers are accounting and outlive a deleted box. RLS_DIRECT (workspace_id)."""
VERSION = "0052"
NAME = "sandbox_usage"

_T = """CREATE TABLE IF NOT EXISTS sandbox_usage_hourly(hour TIMESTAMPTZ NOT NULL, workspace_id INT NOT NULL, sandbox_id TEXT NOT NULL,
  cpu_seconds DOUBLE PRECISION NOT NULL DEFAULT 0, mem_byte_seconds DOUBLE PRECISION NOT NULL DEFAULT 0, running_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
  disk_bytes_max BIGINT NOT NULL DEFAULT 0, PRIMARY KEY (hour, sandbox_id))"""

async def up(q):
    await q(_T)
    await q("CREATE INDEX IF NOT EXISTS sandbox_usage_hourly_ws_idx ON sandbox_usage_hourly(workspace_id, hour DESC)")

async def down(q):
    await q("DROP TABLE IF EXISTS sandbox_usage_hourly")

async def plan(q):
    return ["CREATE TABLE sandbox_usage_hourly (PK hour+sandbox_id) + index (workspace_id, hour)"]
