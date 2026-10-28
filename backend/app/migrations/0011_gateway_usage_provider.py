"""GĐ11.3 gateway_usage.provider: the provider NAME of the call (openai / anthropic / gemini, or the workspace's gw_providers.name). Provider spend caps sum it;
old rows keep NULL and are classified by model name (gateway.spend_query)."""
VERSION = "0011"
NAME = "gateway_usage_provider"

async def up(q):
    await q("ALTER TABLE gateway_usage ADD COLUMN IF NOT EXISTS provider TEXT")

async def down(q):
    await q("ALTER TABLE gateway_usage DROP COLUMN IF EXISTS provider")

async def plan(q):
    return ["ALTER TABLE gateway_usage ADD COLUMN provider TEXT (nullable; old rows classified by model name)"]
