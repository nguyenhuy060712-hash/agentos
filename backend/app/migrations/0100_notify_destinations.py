"""GĐ12.1 one notify layer. Two tables (both carry `workspace_id`, so admin.rls_init gives them the workspace policy via RLS_DIRECT) + delivery columns on `outbox`.
  notify_destinations:   where a message goes (email | telegram | slack_webhook | webhook). No key is stored here: every key is a `ws_secrets` row named in config / secret_refs /
                         signing_secret_ref. owner_user_id NULL = workspace destination (admin), else "mine" (owner + admin only). health HEALTHY | FAILING (+ fail_reason).
  notify_subscriptions:  which event types (pattern like `alert.*`) + filter go to a destination. ON DELETE CASCADE from the destination.
  outbox (+9 columns):   destination_id, subscription_id, event_id, event_type, request_id, raw_body (the exact signed bytes, re-sent on retry), http_status, latency_ms.
down(): drops the two tables and the added outbox columns."""
VERSION = "0100"
NAME = "notify_destinations"

_DEST = """CREATE TABLE IF NOT EXISTS notify_destinations(
  id SERIAL PRIMARY KEY, workspace_id INT NOT NULL, owner_user_id INT, name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('email','telegram','slack_webhook','webhook')),
  config JSONB NOT NULL DEFAULT '{}', secret_refs JSONB NOT NULL DEFAULT '{}', headers JSONB NOT NULL DEFAULT '{}', signing_secret_ref TEXT,
  sig_extra TEXT CHECK (sig_extra IS NULL OR sig_extra = 'standard_webhooks'),
  health TEXT NOT NULL DEFAULT 'HEALTHY' CHECK (health IN ('HEALTHY','FAILING')), fail_reason TEXT, consecutive_failures INT NOT NULL DEFAULT 0,
  first_failure_at TIMESTAMPTZ, last_success_at TIMESTAMPTZ, last_attempt_at TIMESTAMPTZ,
  enabled BOOLEAN NOT NULL DEFAULT true, created_by TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now())"""
_SUB = """CREATE TABLE IF NOT EXISTS notify_subscriptions(
  id SERIAL PRIMARY KEY, workspace_id INT NOT NULL, destination_id INT NOT NULL REFERENCES notify_destinations(id) ON DELETE CASCADE,
  event_type TEXT NOT NULL, filter JSONB NOT NULL DEFAULT '{}', created_by TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now())"""
_COLS = ("destination_id INT", "subscription_id INT", "event_id UUID", "event_type TEXT", "request_id UUID", "raw_body BYTEA", "http_status INT", "latency_ms INT")

async def up(q):
    await q(_DEST)
    await q(_SUB)
    await q("CREATE INDEX IF NOT EXISTS notify_dest_ws ON notify_destinations(workspace_id)")
    await q("CREATE INDEX IF NOT EXISTS notify_sub_ws ON notify_subscriptions(workspace_id, destination_id)")
    await q("ALTER TABLE outbox " + ", ".join("ADD COLUMN IF NOT EXISTS " + c for c in _COLS))
    await q("CREATE INDEX IF NOT EXISTS outbox_dest ON outbox(destination_id, id) WHERE destination_id IS NOT NULL")

async def down(q):
    await q("DROP INDEX IF EXISTS outbox_dest")
    await q("ALTER TABLE outbox " + ", ".join("DROP COLUMN IF EXISTS " + c.split()[0] for c in _COLS))
    await q("DROP TABLE IF EXISTS notify_subscriptions")
    await q("DROP TABLE IF EXISTS notify_destinations")

async def plan(q):
    return ["CREATE TABLE notify_destinations (workspace_id, owner_user_id, kind, config, secret_refs, headers, signing_secret_ref, sig_extra, health, enabled ...)",
            "CREATE TABLE notify_subscriptions (destination_id -> notify_destinations ON DELETE CASCADE, event_type, filter)",
            "ALTER TABLE outbox ADD destination_id, subscription_id, event_id, event_type, request_id, raw_body, http_status, latency_ms (+ index)  (RLS_DIRECT by admin.rls_init for the two tables)"]
