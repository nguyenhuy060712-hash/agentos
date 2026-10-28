"""GĐ12.10 alerts v2: a state machine per rule (ok -> firing -> ok) instead of the old "last_fired + window" dedupe.
  alert_rules (+ columns):  description, aggregation (NULL = the metric's usual one), severity 0..3 (0 info, 1 warn, 2-3 critical), state 'ok'|'firing', state_since,
                            renotify_min (default 60), last_notified_at, dedup_key (one id per firing episode), last_error.
  alert_events (+ column):  kind 'fired'|'resolved'|'renotify'|'test' (old rows are 'fired').
Rules that are firing right now at upgrade time stay 'ok' (the first evaluation moves them to firing). down() drops the added columns."""
VERSION = "0120"
NAME = "alert_rules_v2"

_RULE = ("description TEXT", "aggregation TEXT", "severity SMALLINT NOT NULL DEFAULT 1 CHECK (severity BETWEEN 0 AND 3)",
         "state TEXT NOT NULL DEFAULT 'ok' CHECK (state IN ('ok','firing'))", "state_since TIMESTAMPTZ", "renotify_min INT NOT NULL DEFAULT 60 CHECK (renotify_min BETWEEN 1 AND 10080)",
         "last_notified_at TIMESTAMPTZ", "dedup_key TEXT", "last_error TEXT")

async def up(q):
    await q("ALTER TABLE alert_rules " + ", ".join("ADD COLUMN IF NOT EXISTS " + c for c in _RULE))
    await q("ALTER TABLE alert_events ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'fired' CHECK (kind IN ('fired','resolved','renotify','test'))")

async def down(q):
    await q("ALTER TABLE alert_events DROP COLUMN IF EXISTS kind")
    await q("ALTER TABLE alert_rules " + ", ".join("DROP COLUMN IF EXISTS " + c.split()[0] for c in _RULE))

async def plan(q):
    return ["ALTER TABLE alert_rules ADD description, aggregation, severity, state ('ok'|'firing'), state_since, renotify_min, last_notified_at, dedup_key, last_error",
            "ALTER TABLE alert_events ADD kind ('fired'|'resolved'|'renotify'|'test')"]
