"""GĐ11.14 workspace model allow-list (policy engine, reduced): mode 'all' (no restriction) or 'selected' (a model is allowed when its provider is in providers OR its ref is in models).
One row per workspace. RLS: admin.RLS_DIRECT (workspace_id). Spend caps stay in spend_caps."""
VERSION = "0015"
NAME = "gw_model_access"

async def up(q):
    ok = (await q("SELECT to_regproc('aos_ws') IS NOT NULL"))[0][0]
    d = "coalesce(aos_ws(), 1)" if ok else "1"
    await q(f"""CREATE TABLE IF NOT EXISTS gw_model_access(workspace_id INT PRIMARY KEY DEFAULT {d}, mode TEXT NOT NULL DEFAULT 'all' CHECK (mode IN ('all','selected')),
      providers TEXT[], models TEXT[], updated_by TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")

async def down(q):
    await q("DROP TABLE IF EXISTS gw_model_access")

async def plan(q):
    return ["CREATE TABLE gw_model_access(workspace_id PK, mode all|selected, providers TEXT[], models TEXT[], updated_by, updated_at)",
            "(RLS policy is registered by admin.RLS_DIRECT at boot)"]
