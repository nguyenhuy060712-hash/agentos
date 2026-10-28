"""GĐ11.60 new-mail trigger (Gmail + Outlook polling). Two tables, both tied to `channel_bindings` (kind 'gmail' | 'outlook') with ON DELETE CASCADE, so deleting a binding or
permanently deleting its agent removes them. Both carry `workspace_id` and get their row-level-security policy (workspace_id = aos_ws()) from admin.rls_init (RLS_DIRECT).
  mail_cursors: one row per binding = where the poller is (`pos` = Gmail historyId | Graph delta/next link), the runtime state (active | paused | error | needs_reconnect), the error
                counter + back-off time, the hourly-notice marker. Never holds a token.
  mail_seen:    one row per (binding, message id) = the dedupe memory ('claiming' -> 'queued' | 'skipped_<reason>'); also what the hourly / daily run caps count.
down(): drops both tables."""
VERSION = "0061"
NAME = "mail_triggers"

_CURSORS = """CREATE TABLE IF NOT EXISTS mail_cursors(
  binding_id INT PRIMARY KEY REFERENCES channel_bindings(id) ON DELETE CASCADE, workspace_id INT NOT NULL,
  pos TEXT, account TEXT,
  state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','paused','error','needs_reconnect')), state_reason TEXT,
  errors INT NOT NULL DEFAULT 0, last_error TEXT,
  armed_at TIMESTAMPTZ, last_poll_at TIMESTAMPTZ, last_ok_at TIMESTAMPTZ, next_poll_at TIMESTAMPTZ,
  poll_requested BOOLEAN NOT NULL DEFAULT false,
  notice_key TEXT, notice_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())"""
_SEEN = """CREATE TABLE IF NOT EXISTS mail_seen(
  binding_id INT NOT NULL REFERENCES channel_bindings(id) ON DELETE CASCADE, workspace_id INT NOT NULL, msg_id TEXT NOT NULL,
  outcome TEXT NOT NULL, run_id TEXT, thread_id TEXT, seen_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (binding_id, msg_id))"""

async def up(q):
    await q(_CURSORS)
    await q(_SEEN)
    await q("CREATE INDEX IF NOT EXISTS mail_seen_at ON mail_seen(binding_id, seen_at)")

async def down(q):
    await q("DROP TABLE IF EXISTS mail_seen")
    await q("DROP TABLE IF EXISTS mail_cursors")

async def plan(q):
    return ["CREATE TABLE mail_cursors (binding_id PK -> channel_bindings ON DELETE CASCADE, pos, state, errors, next_poll_at ...; pos = the position)",
            "CREATE TABLE mail_seen (binding_id + msg_id PK -> channel_bindings ON DELETE CASCADE, outcome, run_id, seen_at) + index (RLS_DIRECT by admin.rls_init)"]
