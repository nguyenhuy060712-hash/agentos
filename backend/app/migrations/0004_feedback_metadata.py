"""GĐ10.14 feedback.metadata: free-form JSON a feedback sender attaches (presigned-token feedback and the ingest API)."""
VERSION = "0004"
NAME = "feedback_metadata"

async def up(q): await q("ALTER TABLE feedback ADD COLUMN IF NOT EXISTS metadata JSONB")
async def down(q): await q("ALTER TABLE feedback DROP COLUMN IF EXISTS metadata")
async def plan(q): return ["ALTER TABLE feedback ADD metadata JSONB"]
