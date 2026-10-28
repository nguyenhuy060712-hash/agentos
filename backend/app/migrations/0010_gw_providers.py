"""GĐ11.1 LLM providers per workspace (OpenAI / Anthropic / Gemini / any OpenAI-compatible server). The API key is NOT here: it is a ws_secrets row
named in secret_name. No allow_private column: an internal base_url is allowed only by the operator's env AOS_EGRESS_ALLOW (netguard purpose model_provider)."""
VERSION = "0010"
NAME = "gw_providers"

async def _default(q):
    # migrations run BEFORE admin.rls_init(): aos_ws() may not exist yet on a fresh DB; rls_init then upgrades the default to coalesce(aos_ws(),1)
    ok = (await q("SELECT to_regproc('aos_ws') IS NOT NULL"))[0][0]
    return "coalesce(aos_ws(), 1)" if ok else "1"

async def up(q):
    d = await _default(q)
    await q(f"""CREATE TABLE IF NOT EXISTS gw_providers(id SERIAL PRIMARY KEY, workspace_id INT NOT NULL DEFAULT {d},
      kind TEXT NOT NULL CHECK (kind IN ('openai','anthropic','gemini','openai_compat')), name TEXT NOT NULL, base_url TEXT NOT NULL, secret_name TEXT,
      enabled BOOLEAN NOT NULL DEFAULT true, status TEXT NOT NULL DEFAULT 'untested', last_test_at TIMESTAMPTZ, last_test_result TEXT,
      created_by TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(workspace_id, name))""")

async def down(q):
    await q("DROP TABLE IF EXISTS gw_providers")

async def plan(q):
    return ["CREATE TABLE gw_providers(id, workspace_id, kind, name, base_url, secret_name, enabled, status, last_test_at, last_test_result, created_by, created_at, UNIQUE(workspace_id,name))",
            "(RLS policy is registered by admin.RLS_DIRECT at boot)"]
