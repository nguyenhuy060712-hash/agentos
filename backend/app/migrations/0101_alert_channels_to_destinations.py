"""GĐ12.1 data migration: every distinct (channel, target, channel_cfg) of `alert_rules` (per workspace) becomes ONE destination plus subscriptions
`alert.*`, `insights.*`, `engine.*` (and `system.*` for workspace 1, because health_watch sends there). A webhook's old signing secret is referenced (not copied) as
signing_secret_ref. The old alert_rules columns are untouched (the alert code still reads them until 12.10). Rows are tagged created_by='migration:0101'.
down(): deletes those destinations (their subscriptions go with them). Re-running up() adds nothing twice."""
import json

VERSION = "0101"
NAME = "alert_channels_to_destinations"
TAG = "migration:0101"
PATTERNS = ("alert.*", "insights.*", "engine.*")
KIND = {"email": "email", "telegram": "telegram", "slack": "slack_webhook", "webhook": "webhook"}

def _dest(channel, target, cfg):
    """-> (kind, name, config, signing_secret_ref) of the new destination."""
    kind, cfg = KIND[channel], dict(cfg or {})
    if kind == "webhook":
        sig = cfg.pop("signing_secret", None)
        host = (target or "").split("//")[-1].split("/")[0]
        return kind, f"Webhook {host}"[:80], {"url": target}, sig
    if target: cfg["target"] = target
    label = cfg.get("target") or cfg.get("secret") or ""
    return kind, f"{channel} {label}"[:80].strip(), cfg, None

async def up(q):
    if not (await q("SELECT to_regclass('alert_rules')"))[0][0]: return
    rows = await q("""SELECT DISTINCT p.workspace_id, a.channel, a.target, a.channel_cfg FROM alert_rules a JOIN projects p ON p.id=a.project_id
                      WHERE a.channel = ANY(%s) ORDER BY p.workspace_id, a.channel, a.target""", (list(KIND),))
    for ws, channel, target, cfg in rows:
        kind, name, config, sig = _dest(channel, target, cfg)
        if await q("SELECT 1 FROM notify_destinations WHERE workspace_id=%s AND created_by=%s AND kind=%s AND config=%s::jsonb", (ws, TAG, kind, json.dumps(config))): continue
        did = (await q("""INSERT INTO notify_destinations(workspace_id,name,kind,config,secret_refs,signing_secret_ref,created_by) VALUES(%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s) RETURNING id""",
                       (ws, name, kind, json.dumps(config), json.dumps({"signing": sig} if sig else {}), sig, TAG)))[0][0]
        for pat in PATTERNS + (("system.*",) if ws == 1 else ()):
            await q("INSERT INTO notify_subscriptions(workspace_id,destination_id,event_type,created_by) VALUES(%s,%s,%s,%s)", (ws, did, pat, TAG))

async def down(q):
    await q("DELETE FROM notify_destinations WHERE created_by=%s", (TAG,))

async def plan(q):
    if not (await q("SELECT to_regclass('alert_rules')"))[0][0]: return ["no alert_rules table: nothing to migrate"]
    n = (await q("SELECT count(*) FROM (SELECT DISTINCT p.workspace_id, a.channel, a.target, a.channel_cfg FROM alert_rules a JOIN projects p ON p.id=a.project_id WHERE a.channel = ANY(%s)) x", (list(KIND),)))[0][0]
    return [f"INSERT {n} notify_destinations (one per distinct alert channel) + subscriptions {', '.join(PATTERNS)} (+ system.* for workspace 1), created_by='{TAG}'"]
