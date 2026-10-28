"""GĐ11.11 model configurations v2: a saved configuration points at a workspace provider (gw_providers) and an upstream model; NO secret / header column
(the key comes from the provider). model_tiers = per-workspace override of aos/fast|pro|max. model_presets is normally created by ext.init(); created here too
because migrations run first on a fresh database. RLS: model_presets by ext.WS_TABLES, model_tiers by admin.RLS_DIRECT."""
VERSION = "0012"
NAME = "model_presets_v2"

async def _default(q):
    ok = (await q("SELECT to_regproc('aos_ws') IS NOT NULL"))[0][0]   # migrations run BEFORE admin.rls_init(): aos_ws() may not exist yet
    return "coalesce(aos_ws(), 1)" if ok else "1"

async def up(q):
    d = await _default(q)
    await q(f"""CREATE TABLE IF NOT EXISTS model_presets(id SERIAL PRIMARY KEY, workspace_id INT NOT NULL DEFAULT {d}, name TEXT NOT NULL, model TEXT NOT NULL,
      temperature DOUBLE PRECISION, max_tokens INT, top_p DOUBLE PRECISION, fallback_models TEXT[], created TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(workspace_id, name))""")
    await q("ALTER TABLE model_presets ADD COLUMN IF NOT EXISTS reasoning_effort TEXT")
    await q("ALTER TABLE model_presets ADD COLUMN IF NOT EXISTS provider_id INT")
    await q("ALTER TABLE model_presets ADD COLUMN IF NOT EXISTS upstream_model TEXT")
    await q("ALTER TABLE model_presets ADD COLUMN IF NOT EXISTS provider_api TEXT NOT NULL DEFAULT 'chat'")
    await q("ALTER TABLE model_presets ADD COLUMN IF NOT EXISTS extra_params JSONB")
    await q("ALTER TABLE model_presets ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT false")
    await q("ALTER TABLE model_presets ADD COLUMN IF NOT EXISTS created_by TEXT")
    await q("DO $$ BEGIN ALTER TABLE model_presets ADD CONSTRAINT model_presets_api_chk CHECK (provider_api IN ('chat','responses')); EXCEPTION WHEN duplicate_object THEN NULL; END $$")
    await q("CREATE UNIQUE INDEX IF NOT EXISTS model_presets_one_default ON model_presets(workspace_id) WHERE is_default")
    await q(f"""CREATE TABLE IF NOT EXISTS model_tiers(workspace_id INT NOT NULL DEFAULT {d}, tier TEXT NOT NULL CHECK (tier IN ('fast','pro','max')),
      config_ref TEXT NOT NULL, updated_by TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(workspace_id, tier))""")

async def down(q):
    await q("DROP TABLE IF EXISTS model_tiers")
    await q("DROP INDEX IF EXISTS model_presets_one_default")
    for c in ("provider_id", "upstream_model", "provider_api", "extra_params", "is_default", "created_by"):
        await q(f"ALTER TABLE model_presets DROP COLUMN IF EXISTS {c}")

async def plan(q):
    return ["model_presets: add provider_id, upstream_model, provider_api ('chat'|'responses'), extra_params JSONB, is_default (one per workspace), created_by",
            "CREATE TABLE model_tiers(workspace_id, tier IN fast|pro|max, config_ref, updated_by, updated_at, PK(workspace_id,tier))",
            "(RLS policy of model_tiers is registered by admin.RLS_DIRECT at boot)"]
