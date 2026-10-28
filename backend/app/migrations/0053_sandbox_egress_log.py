"""GĐ11.45 egress proxy: `sandbox_egress_log` = one row per allow / deny decision of the egress proxy (host, port, verdict, reason code, mode; NEVER paths, headers or
tokens). Not tied to `sandboxes` by a foreign key on purpose: the trail outlives a deleted box (pruned after 30 days by app/egress_ctl.py). The table gets its
row-level-security policy (workspace_id = aos_ws()) from admin.rls_init (RLS_DIRECT), which runs right after the migrations."""
VERSION = "0053"
NAME = "sandbox_egress_log"

_T = """CREATE TABLE IF NOT EXISTS sandbox_egress_log(id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL DEFAULT now(), workspace_id INT NOT NULL, sandbox_id TEXT,
  host TEXT NOT NULL, port INT NOT NULL DEFAULT 0, verdict TEXT NOT NULL CHECK (verdict IN ('allow','deny')), reason TEXT NOT NULL, mode TEXT NOT NULL DEFAULT '')"""

async def up(q):
    await q(_T)
    await q("CREATE INDEX IF NOT EXISTS sandbox_egress_log_ws_idx ON sandbox_egress_log(workspace_id, ts DESC)")
    await q("CREATE INDEX IF NOT EXISTS sandbox_egress_log_ts_idx ON sandbox_egress_log(ts)")

async def down(q):
    await q("DROP TABLE IF EXISTS sandbox_egress_log")

async def plan(q):
    return ["CREATE TABLE sandbox_egress_log (workspace_id, sandbox_id, host, port, verdict allow|deny, reason, mode) + 2 indexes (RLS_DIRECT by admin.rls_init)"]
