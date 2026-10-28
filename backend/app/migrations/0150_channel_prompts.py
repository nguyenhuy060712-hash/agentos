"""GĐ12.74 / 12.78 channel prompts: one row per question form ('ask') or sign-in button ('connect') that a paused run posted into an external chat (Slack).
  channel_prompts: binding_id -> channel_bindings ON DELETE CASCADE; thread_id / run_id = the paused run; chat_id = where it was posted ("<channel>:<thread_ts>");
  message_ts = the posted message (edited after an answer); payload = the form ({title, note, fields, tool_call_id}) or the sign-in ({connect_url, auth_id, service});
  status pending -> answered | expired | superseded (the first answer wins: an atomic UPDATE ... WHERE status='pending'); expires_at = created_at + 24 h.
  Workspace RLS comes from admin.RLS_DIRECT. down(): drops the table."""
VERSION = "0150"
NAME = "channel_prompts"

async def up(q):
    await q("""CREATE TABLE IF NOT EXISTS channel_prompts(
      id SERIAL PRIMARY KEY, workspace_id INT NOT NULL DEFAULT 1,
      binding_id INT NOT NULL REFERENCES channel_bindings(id) ON DELETE CASCADE,
      kind TEXT NOT NULL CHECK (kind IN ('ask','connect')), thread_id TEXT, run_id TEXT, chat_id TEXT NOT NULL, message_ts TEXT,
      payload JSONB NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','answered','expired','superseded')),
      answered_by TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), expires_at TIMESTAMPTZ NOT NULL)""")
    await q("CREATE INDEX IF NOT EXISTS channel_prompts_thread ON channel_prompts(binding_id, thread_id, status)")

async def down(q):
    await q("DROP TABLE IF EXISTS channel_prompts")

async def plan(q):
    return ["CREATE TABLE channel_prompts (id, workspace_id, binding_id -> channel_bindings ON DELETE CASCADE, kind ask|connect, thread_id, run_id, chat_id, message_ts, payload JSONB, "
            "status pending|answered|expired|superseded, answered_by, created_at, expires_at) + index (binding_id, thread_id, status); RLS_DIRECT by admin.rls_init"]
