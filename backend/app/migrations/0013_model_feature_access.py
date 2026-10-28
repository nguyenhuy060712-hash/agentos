"""GĐ11.12 which providers / models each feature (chat, playground, evaluator, ...) may offer. No row = no restriction. Empty providers / models = all.
RLS: admin.RLS_DIRECT (workspace_id)."""
VERSION = "0013"
NAME = "model_feature_access"

async def up(q):
    ok = (await q("SELECT to_regproc('aos_ws') IS NOT NULL"))[0][0]
    d = "coalesce(aos_ws(), 1)" if ok else "1"
    await q(f"""CREATE TABLE IF NOT EXISTS model_feature_access(workspace_id INT NOT NULL DEFAULT {d}, feature TEXT NOT NULL, providers TEXT[], models TEXT[],
      default_ref TEXT, updated_by TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(workspace_id, feature))""")

async def down(q):
    await q("DROP TABLE IF EXISTS model_feature_access")

async def plan(q):
    return ["CREATE TABLE model_feature_access(workspace_id, feature, providers TEXT[], models TEXT[], default_ref, updated_by, updated_at, PK(workspace_id,feature))",
            "(RLS policy is registered by admin.RLS_DIRECT at boot)"]
