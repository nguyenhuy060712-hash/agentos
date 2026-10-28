"""GĐ12.10 alerts (app/alerts.py, migration 0120): state machine with a fake clock, delivery through destinations to a fake webhook receiver on loopback, preview = what really
fires, edit / toggle / test, health events (fake readers). In-process on the local DB (gd12_world private pool + two throw-away workspaces); $0, fake LLM not even needed."""
import asyncio, collections, datetime as dt, json, os, sys, uuid
import pytest
from fastapi import HTTPException
from gd12_world import NOW, as_user, sysq, with_world
from test_gd6_notify import http  # noqa: F401  (fake webhook receiver on 127.0.0.1)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import deepagent_api  # noqa: E402,F401  (import order: deepagent_api first)
import alerts, charts, destinations  # noqa: E402

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
M = dt.timedelta(minutes=1); H = dt.timedelta(hours=1)
FIELDS12 = {"project_name", "workspace_name", "alert_rule_id", "alert_rule_name", "alert_rule_description", "alert_rule_type", "alert_rule_attribute", "alert_rule_url", "runs_url",
            "triggered_metric_value", "triggered_threshold", "timestamp"}

@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("PG_URI", PG); monkeypatch.setenv("AOS_EGRESS_ALLOW", "127.0.0.0/8,::1"); monkeypatch.setenv("AOS_PUBLIC_URL", "https://aos.example.test")
    charts._CACHE.clear()

async def mk_dest(A, admin, ws, url, events=("alert.*",), flt=None):
    b = destinations.DestIn(name="t1210 hook", kind="webhook", config={"url": url}, events=list(events))
    d = await as_user(admin, ws, lambda: destinations.create_destination(b))
    if flt: await sysq(A, "UPDATE notify_subscriptions SET filter=%s::jsonb WHERE destination_id=%s", (json.dumps(flt), d["id"]))
    return d

async def mk_rule(A, pid, **kw):
    v = {"name": "t1210 rule", "metric": "error_rate", "op": "gt", "threshold": 0.5, "window_min": 60, "renotify_min": 60, "severity": 2, **kw}
    cols = ",".join(v); ph = ",".join(["%s"] * len(v))
    return await sysq(A, f"INSERT INTO alert_rules(project_id,channel,{cols}) VALUES(%s,'log',{ph}) RETURNING *", (pid, *v.values()), one=True)

async def tidy(A, wss):
    for sql in ("DELETE FROM outbox WHERE ws_id = ANY(%s)", "DELETE FROM notify_destinations WHERE workspace_id = ANY(%s)", "DELETE FROM ws_secrets WHERE workspace_id = ANY(%s)",
                "DELETE FROM crypto_deks WHERE workspace_id = ANY(%s)", "DELETE FROM crypto_ws WHERE workspace_id = ANY(%s)"):
        try: await sysq(A, sql, (wss,))
        except Exception as e: print("tidy:", str(e)[:80])  # noqa: BLE001

async def put_run(A, pid, start, status="success", ms=100):
    rid = str(uuid.uuid4())
    await sysq(A, "INSERT INTO tr_runs(id,trace_id,project_id,name,run_type,start_time,end_time,latency_ms,status) VALUES(%s,%s,%s,'r','chain',%s,%s,%s,%s)",
               (rid, rid, pid, start, start + dt.timedelta(milliseconds=ms), ms, status))

def events(http_):
    return [json.loads(g["raw"]) for g in http_.got]

def test_state_machine_fires_once_renotifies_after_interval_resolves_once(http):
    async def go(A, admin, W):
        a = W["a"]
        try:
            await mk_dest(A, admin, a["ws"], http.url + "/h")
            r0 = await mk_rule(A, a["pid"], description="spike"); t0 = NOW().replace(microsecond=0)
            async def ev(minutes, v):
                r = await sysq(A, "SELECT * FROM alert_rules WHERE id=%s", (r0["id"],), one=True)
                return await alerts.evaluate(r, t0 + minutes * M, v, use_value=True)
            assert await ev(0, 0.9) == "fired"
            assert await ev(1, 0.9) is None and await ev(30, 0.95) is None and len(http.got) == 1          # still above, before renotify_min: nothing
            assert await ev(61, 0.9) == "renotify" and await ev(62, 0.9) is None
            assert await ev(70, None) is None                                                              # no data: nothing changes
            assert (await sysq(A, "SELECT state FROM alert_rules WHERE id=%s", (r0["id"],), one=True))["state"] == "firing"
            assert await ev(75, 0.1) == "resolved" and await ev(76, 0.1) is None and await ev(77, 0.1) is None
            ev_ = events(http); assert [e["type"] for e in ev_] == ["alert.fired", "alert.renotify", "alert.resolved"]
            d = ev_[0]["data"]; assert FIELDS12 <= set(d) and d["alert_state"] == "firing" and d["severity"] == "critical" and d["alert_rule_name"] == "t1210 rule"
            assert d["alert_rule_description"] == "spike" and d["triggered_metric_value"] == 0.9 and d["triggered_threshold"] == 0.5 and d["alert_rule_type"] == "threshold"
            assert d["alert_rule_url"].startswith("https://aos.example.test/") and d["runs_url"].endswith(f"?p={a['pid']}") and d["project_name"] == f"p-a-{W['tag']}"
            assert ev_[1]["data"]["dedup_key"] == d["dedup_key"] and ev_[2]["data"]["alert_state"] == "ok"
            assert [e["kind"] for e in await sysq(A, "SELECT kind FROM alert_events WHERE rule_id=%s ORDER BY id", (r0["id"],))] == ["fired", "renotify", "resolved"]
            assert await ev(200, 0.9) == "fired" and len(http.got) == 4                                      # a new episode starts again
        finally: await tidy(A, [W["a"]["ws"], W["b"]["ws"]])
    with_world(go)

def test_subscription_filters_project_and_severity(http):
    async def go(A, admin, W):
        a = W["a"]
        try:
            await mk_dest(A, admin, a["ws"], http.url + "/h", flt={"project_id": a["pid"], "min_severity": "critical"})
            lo = await mk_rule(A, a["pid"], name="low", severity=0); hi = await mk_rule(A, a["pid"], name="high", severity=3)
            for r in (lo, hi): await alerts.evaluate(r, NOW(), 1.0, use_value=True)
            assert [e["data"]["rule"] for e in events(http)] == ["high"]
        finally: await tidy(A, [W["a"]["ws"], W["b"]["ws"]])
    with_world(go)

def test_toggle_off_fires_nothing_and_resets_state(http):
    async def go(A, admin, W):
        a = W["a"]
        try:
            await mk_dest(A, admin, a["ws"], http.url + "/h")
            r0 = await mk_rule(A, a["pid"]); await alerts.evaluate(r0, NOW(), 1.0, use_value=True); assert len(http.got) == 1
            out = await as_user(admin, a["ws"], lambda: alerts.alert_toggle(r0["id"], alerts.AlertEnabled(enabled=False)))
            assert out["enabled"] is False and out["state"] == "ok"
            await alerts.check_alerts()                                                                     # the worker skips disabled rules
            assert len(http.got) == 1
            out = await as_user(admin, a["ws"], lambda: alerts.alert_toggle(r0["id"], alerts.AlertEnabled(enabled=True))); assert out["enabled"] is True
        finally: await tidy(A, [W["a"]["ws"], W["b"]["ws"]])
    with_world(go)

def test_real_value_preview_matches_what_fires(http):
    async def go(A, admin, W):
        a = W["a"]
        try:
            await mk_dest(A, admin, a["ws"], http.url + "/h")
            now = NOW(); top = now.replace(minute=0, second=0, microsecond=0)
            for h_ago, errs, oks in ((5, 0, 3), (4, 3, 1), (3, 1, 3), (2, 0, 0), (1, 4, 0)):   # error rate per hour: 0, .75, .25, no data, 1
                s = top - h_ago * H
                for i in range(errs): await put_run(A, a["pid"], s + (5 + i) * M, "error")
                for i in range(oks): await put_run(A, a["pid"], s + (20 + i) * M)
            r0 = await mk_rule(A, a["pid"])
            pts = await as_user(admin, a["ws"], lambda: alerts.alert_preview(alerts.PreviewIn(project_id=a["pid"], hours=6, rule=alerts.AlertEdit(metric="error_rate", op="gt", threshold=0.5, window_min=60))))
            byt = {dt.datetime.fromisoformat(p["t"]).astimezone(dt.timezone.utc): p for p in pts}
            checked = 0
            for h_ago in (5, 4, 3, 2, 1):
                s = top - h_ago * H; charts._CACHE.clear()
                v = await alerts.metric_value(r0, s + H - M)                                                # the window [s, s+1h) = exactly that bucket
                fires = v is not None and v > 0.5; p = byt[s]
                assert p["fires"] == fires and (p["value"] is None) == (v is None) and (v is None or p["value"] == pytest.approx(v)), (h_ago, p, v)
                checked += 1
            assert checked == 5 and [byt[top - h * H]["fires"] for h in (5, 4, 3, 2, 1)] == [False, True, False, False, True]
        finally: await tidy(A, [W["a"]["ws"], W["b"]["ws"]])
    with_world(go)

def test_real_evaluation_then_test_endpoint_per_destination(http):
    async def go(A, admin, W):
        a = W["a"]
        try:
            await mk_dest(A, admin, a["ws"], http.url + "/h")
            for i in range(3): await put_run(A, a["pid"], NOW() - (2 + i) * M, "error")
            r0 = await mk_rule(A, a["pid"], metric="count", op="gte", threshold=3, window_min=30)
            assert await alerts.evaluate(r0, NOW()) == "fired" and events(http)[0]["data"]["triggered_metric_value"] == 3
            out = await as_user(admin, a["ws"], lambda: alerts.alert_test(r0["id"], True))
            assert out["value"] == 3 and out["fires"] is True
            dlv = out["deliveries"]; assert len(dlv) == 1 and dlv[0]["http_status"] == 200 and dlv[0]["health"] == "HEALTHY" and dlv[0]["latency_ms"] is not None and dlv[0]["ok"]
            assert events(http)[-1]["type"] == "alert.test"
            out = await as_user(admin, a["ws"], lambda: alerts.alert_test(r0["id"], False)); assert "deliveries" not in out and len(http.got) == 2
        finally: await tidy(A, [W["a"]["ws"], W["b"]["ws"]])
    with_world(go)

def test_edit_validation_and_other_workspace_cannot_see(http):
    async def go(A, admin, W):
        a, b = W["a"], W["b"]
        try:
            r0 = await mk_rule(A, a["pid"])
            out = await as_user(admin, a["ws"], lambda: alerts.alert_put(r0["id"], alerts.AlertEdit(threshold=0.2, severity=3, description="d")))
            assert out["threshold"] == 0.2 and out["severity"] == 3 and out["description"] == "d" and out["name"] == "t1210 rule"
            with pytest.raises(HTTPException) as e: await as_user(admin, a["ws"], lambda: alerts.alert_put(r0["id"], alerts.AlertEdit(metric="feedback_avg")))
            assert e.value.status_code == 422
            with pytest.raises(HTTPException) as e: await as_user(admin, b["ws"], lambda: alerts.alert_get(r0["id"]))
            assert e.value.status_code == 404
            bad = alerts.AlertIn(project_id=a["pid"], name="x", metric="feedback_avg", op="lt", threshold=1, channel="log")
            with pytest.raises(HTTPException) as e: await as_user(admin, a["ws"], lambda: alerts.alert_new(bad))
            assert e.value.status_code == 422
            ok = alerts.AlertIn(project_id=a["pid"], name="t1210 new", metric="latency_p95", op="gt", threshold=500, channel="log", aggregation="p99", severity=2, renotify_min=5)
            nid = (await as_user(admin, a["ws"], lambda: alerts.alert_new(ok)))["id"]
            row = await sysq(A, "SELECT * FROM alert_rules WHERE id=%s", (nid,), one=True); assert row["state"] == "ok" and row["aggregation"] == "p99" and row["renotify_min"] == 5
        finally: await tidy(A, [W["a"]["ws"], W["b"]["ws"]])
    with_world(go)

def test_health_watch_warn_reaches_admin_destination(http, monkeypatch):
    import health_watch
    DU = collections.namedtuple("DU", "total used free")
    monkeypatch.setattr(health_watch, "_disk", lambda: DU(100, 85, 15))
    monkeypatch.setattr(health_watch, "_meminfo", lambda: {"MemTotal": 100, "MemAvailable": 50})
    async def pg(): return 1
    monkeypatch.setattr(health_watch, "_pg_bytes", pg); health_watch.SENT.clear()
    async def go(A, admin, W):
        d = None
        try:
            d = await mk_dest(A, admin, 1, http.url + "/h", events=("system.*",))
            await health_watch.run_check()
            ev_ = events(http); assert [e["type"] for e in ev_] == ["system.health.warn"] and ev_[0]["data"]["kind"] == "disk"
            monkeypatch.setattr(health_watch, "_disk", lambda: DU(100, 95, 5)); health_watch.SENT.clear()
            await health_watch.run_check(); assert events(http)[-1]["type"] == "system.health.critical"
        finally:
            if d: await sysq(A, "DELETE FROM outbox WHERE destination_id=%s", (d["id"],)); await sysq(A, "DELETE FROM notify_destinations WHERE id=%s", (d["id"],)); await sysq(A, "DELETE FROM ws_secrets WHERE name=%s", (f"aos_dest_{d['id']}_sign",))
            health_watch.SENT.clear()
    with_world(go)
