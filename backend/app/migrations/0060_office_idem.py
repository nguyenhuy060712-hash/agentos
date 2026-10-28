"""GĐ11.55 office tools never act twice: `agent_audit` gets `idem_key` (the same tool call of the same thread, e.g. a run replayed after a crash) and `idem_result`
(what the first call answered). A UNIQUE partial index (workspace_id, idem_key) lets exactly one caller claim a key. Rows without a key are untouched.
agent_audit is created here when missing (a fresh DB runs migrations before agent_identity.init) in the shape agent_identity.DDL creates. down(): drops the index + columns."""
VERSION = "0060"
NAME = "office_idem"

_TABLE = """CREATE TABLE IF NOT EXISTS agent_audit(id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL DEFAULT now(), workspace_id INT NOT NULL DEFAULT 1, agent_id INT, run_ref TEXT,
  action TEXT NOT NULL, tool TEXT, acted_as TEXT, principal_id INT, principal TEXT, on_behalf_of TEXT, credential_user_id INT, ok BOOLEAN NOT NULL DEFAULT true, detail JSONB NOT NULL DEFAULT '{}')"""

async def up(q):
    await q(_TABLE)
    await q("ALTER TABLE agent_audit ADD COLUMN IF NOT EXISTS idem_key TEXT, ADD COLUMN IF NOT EXISTS idem_result TEXT")
    await q("CREATE UNIQUE INDEX IF NOT EXISTS agent_audit_idem ON agent_audit(workspace_id, idem_key) WHERE idem_key IS NOT NULL")

async def down(q):
    await q("DROP INDEX IF EXISTS agent_audit_idem")
    await q("ALTER TABLE agent_audit DROP COLUMN IF EXISTS idem_result, DROP COLUMN IF EXISTS idem_key")

async def plan(q):
    return ["agent_audit: ADD idem_key TEXT, idem_result TEXT; UNIQUE INDEX agent_audit_idem(workspace_id, idem_key) WHERE idem_key IS NOT NULL (existing rows unchanged)"]
