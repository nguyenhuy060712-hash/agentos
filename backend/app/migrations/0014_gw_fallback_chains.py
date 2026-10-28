"""GĐ11.13 fallback chains v2: per workspace, per primary model: 1-5 candidate refs tried in order when the primary fails with a retryable error. No secret here.
RLS: admin.RLS_DIRECT (workspace_id). Old columns gw_vkeys.fallback_models / model_presets.fallback_models stay as read-only fallbacks (no data copy)."""
VERSION = "0014"
NAME = "gw_fallback_chains"

async def up(q):
    ok = (await q("SELECT to_regproc('aos_ws') IS NOT NULL"))[0][0]   # migrations run BEFORE admin.rls_init(): aos_ws() may not exist yet
    d = "coalesce(aos_ws(), 1)" if ok else "1"
    await q(f"""CREATE TABLE IF NOT EXISTS gw_fallback_chains(id SERIAL PRIMARY KEY, workspace_id INT NOT NULL DEFAULT {d}, primary_ref TEXT NOT NULL,
      candidates JSONB NOT NULL, trigger_statuses INT[] NOT NULL DEFAULT '{{429,500,502,503,504}}', on_transport_error BOOLEAN NOT NULL DEFAULT true,
      header_timeout_s INT NOT NULL DEFAULT 60, enabled BOOLEAN NOT NULL DEFAULT true, updated_by TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE(workspace_id, primary_ref))""")

async def down(q):
    await q("DROP TABLE IF EXISTS gw_fallback_chains")

async def plan(q):
    return ["CREATE TABLE gw_fallback_chains(id, workspace_id, primary_ref, candidates JSONB, trigger_statuses INT[], on_transport_error, header_timeout_s, enabled, updated_by, updated_at, UNIQUE(workspace_id,primary_ref))",
            "(RLS policy is registered by admin.RLS_DIRECT at boot)"]
