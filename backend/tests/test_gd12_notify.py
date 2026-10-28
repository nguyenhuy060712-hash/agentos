"""GĐ12.1 / 12.2 one notify layer (app/destinations.py, app/notify.py). In-process against local Postgres + fake receivers on loopback (fake Slack / SMTP / webhook), $0.
Migrations 0100/0101 are tested in a throwaway schema (never the real tables). The HTTP part (roles, demo, other workspace through the real middleware) runs on OUR OWN
server on port 8050 (fake LLM); it is killed by its PID at the end."""
import asyncio, base64, hashlib, hmac, json, os, subprocess, sys, textwrap, time, uuid
os.environ.setdefault("GOOGLE_API_KEY", "x-not-used")
import httpx, psycopg, pytest
from test_gd6_notify import http, smtp, real_q, PG  # noqa: F401  (fake receivers + DB helper of the 6.4 tests)

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app"))
run = asyncio.run
WA, WB = 99101, 99102                                      # workspaces of this test (no workspace row needed: nothing here has a foreign key to it)

def U(ws, role="admin", uid=1, demo=False): return {"id": uid, "username": f"t121_{uid}", "role": role, "ws": ws, **({"demo": True} if demo else {})}

def as_user(user, fn, *a, **k):
    import deepagent_api, admin  # noqa: F401  (deepagent_api first: admin alone has a circular import)
    async def go():
        tok = admin.USER.set(user)
        try: return await fn(*a, **k)
        finally: admin.USER.reset(tok)
    return run(go())

def verify(raw, header, key):                              # the receiver as in LangSmith's Python sample
    return hmac.compare_digest("sha256=" + hmac.new(key.encode(), raw, hashlib.sha256).hexdigest(), header or "")

@pytest.fixture(scope="module", autouse=True)
def migrated():
    import migrations
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as c:
            await c.execute("SELECT pg_advisory_lock(hashtext('agentos_boot_ddl'))")
            try: await migrations.run_pending(c)
            finally: await c.execute("SELECT pg_advisory_unlock(hashtext('agentos_boot_ddl'))")
    run(go())

@pytest.fixture
def env(monkeypatch, tmp_path):
    import crypto, destinations, notify, ws_secrets
    monkeypatch.setenv("PG_URI", PG)                          # crypto.py reads/creates the per-workspace data keys here (the real master key; the test workspaces' keys are removed in clean())
    monkeypatch.setenv("AOS_EGRESS_ALLOW", "127.0.0.0/8,::1")
    for m in (destinations, ws_secrets, notify): monkeypatch.setattr(m, "_q", real_q)
    monkeypatch.delenv("TELEGRAM_API_BASE", raising=False)
    def clean():
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute("DELETE FROM outbox WHERE ws_id IN (%s,%s)", (WA, WB)); c.execute("DELETE FROM notify_destinations WHERE workspace_id IN (%s,%s)", (WA, WB))
            c.execute("DELETE FROM ws_secrets WHERE workspace_id IN (%s,%s)", (WA, WB))
            for t in ("crypto_deks", "crypto_ws"): c.execute(f"DELETE FROM {t} WHERE workspace_id IN (%s,%s)", (WA, WB))
    clean(); yield destinations; clean()

def mk(D, ws, url, events=("alert.*",), user=None, **kw):
    b = D.DestIn(name=kw.pop("name", "t121 hook"), kind="webhook", config={"url": url}, events=list(events), **kw)
    return as_user(user or U(ws), D.create_destination, b)

def rows(sql, *a): return run(real_q(sql, a))

# ------------------------------------------------------------------ signed delivery
def test_signature_verifies_and_roll_secret_invalidates_old_key(env, http):
    D = env; d = mk(D, WA, http.url + "/h"); key = d["signing_key"]
    assert key and d["signing_secret_ref"] == f"aos_dest_{d['id']}_sign" and d["health"] == "HEALTHY"
    assert run(D.emit("alert.fired", {"rule": "r1", "project_id": 3, "severity": "warn"}, WA)) == 1
    g = http.got[0]; env_ = json.loads(g["raw"])
    assert verify(g["raw"], g["headers"]["x-langsmith-signature"], key)
    assert env_["type"] == "alert.fired" and env_["data"]["rule"] == "r1" and {"id", "created", "request_id"} <= set(env_)
    assert g["headers"]["x-aos-event-id"] == env_["id"] and g["headers"]["x-aos-event-type"] == "alert.fired" and g["headers"]["x-aos-request-id"] == env_["request_id"]
    assert "webhook-signature" not in g["headers"]
    shown = json.dumps([as_user(U(WA), D.list_destinations), as_user(U(WA), D.get_destination, d["id"])], default=str)
    assert key not in shown                                              # the key is shown once, never again
    new = as_user(U(WA), D.roll_secret, d["id"])["signing_key"]; assert new != key
    run(D.emit("alert.fired", {"rule": "r2", "severity": "warn"}, WA)); g = http.got[1]
    assert verify(g["raw"], g["headers"]["x-langsmith-signature"], new) and not verify(g["raw"], g["headers"]["x-langsmith-signature"], key)
    assert new not in json.dumps(as_user(U(WA), D.deliveries, d["id"]), default=str)

def test_standard_webhooks_headers(env, http):
    D = env; d = mk(D, WA, http.url + "/h", sig_extra="standard_webhooks"); key = d["signing_key"]
    run(D.emit("alert.fired", {"rule": "r"}, WA)); g = http.got[0]; h = g["headers"]
    exp = "v1," + base64.b64encode(hmac.new(key.encode(), f"{h['webhook-id']}.{h['webhook-timestamp']}.".encode() + g["raw"], hashlib.sha256).digest()).decode()
    assert h["webhook-id"] == h["x-aos-event-id"] and h["webhook-signature"] == exp and verify(g["raw"], h["x-langsmith-signature"], key)

def test_retry_resends_same_bytes_and_health_flips(env, http):
    D = env; d = mk(D, WA, http.url + "/h"); http.code = 500
    for i in range(3): run(D.emit("alert.fired", {"rule": f"r{i}"}, WA))
    cur = as_user(U(WA), D.get_destination, d["id"])
    assert cur["health"] == "FAILING" and cur["fail_reason"] == "SERVER_ERROR" and cur["consecutive_failures"] == 3
    first = http.got[0]["raw"]; n = len(http.got)
    rows("UPDATE outbox SET next_at=now() - interval '1 second' WHERE ws_id=%s AND status='pending'", WA)
    run(D.retry_due()); assert len(http.got) == n + 3 and [g["raw"] for g in http.got[n:]].count(first) == 1   # every pending row re-sent with its own, unchanged bytes
    out = rows("SELECT status, attempts, next_at FROM outbox WHERE ws_id=%s ORDER BY id", WA); assert {r["status"] for r in out} == {"pending"} and all(r["attempts"] == 2 for r in out)
    http.code = 200
    r = as_user(U(WA), D.test_destination, d["id"]); assert r["ok"] and r["http_status"] == 200 and r["health"] == "HEALTHY" and r["latency_ms"] >= 0
    assert as_user(U(WA), D.get_destination, d["id"])["consecutive_failures"] == 0

def test_4xx_is_permanent_and_410_disables(env, http):
    D = env; d = mk(D, WA, http.url + "/h"); other = mk(D, WA, http.url + "/other", events=("destination.disabled",))
    http.code = 404; run(D.emit("alert.fired", {"rule": "r"}, WA))
    row = rows("SELECT status, attempts, http_status FROM outbox WHERE destination_id=%s", d["id"])[0]; assert (row["status"], row["attempts"], row["http_status"]) == ("failed", 1, 404)
    assert as_user(U(WA), D.get_destination, d["id"])["fail_reason"] == "URL_NOT_FOUND"
    http.code = 410; n = len(http.got); run(D.emit("alert.fired", {"rule": "r"}, WA))
    cur = as_user(U(WA), D.get_destination, d["id"]); assert cur["enabled"] is False
    ev = [json.loads(g["raw"]) for g in http.got[n:] if g["path"] == "/other"]; assert ev and ev[0]["type"] == "destination.disabled" and ev[0]["data"]["destination_id"] == d["id"]
    assert run(D.emit("alert.fired", {"rule": "r"}, WA)) == 0            # disabled: nothing addressed

def test_failing_over_24h_gets_one_try_per_event(env, http):
    D = env; d = mk(D, WA, http.url + "/h"); http.code = 500
    for i in range(3): run(D.emit("alert.fired", {"rule": "x"}, WA))
    rows("UPDATE notify_destinations SET first_failure_at = now() - interval '25 hours' WHERE id=%s", d["id"])
    run(D.emit("alert.fired", {"rule": "late"}, WA))
    assert rows("SELECT status, attempts FROM outbox WHERE destination_id=%s ORDER BY id DESC LIMIT 1", d["id"])[0] == {"status": "failed", "attempts": 1}

def test_blocked_address_is_policy_failure(env, http, monkeypatch):
    D = env; d = mk(D, WA, http.url + "/h"); monkeypatch.setenv("AOS_EGRESS_ALLOW", "")
    run(D.emit("alert.fired", {"rule": "r"}, WA)); assert http.got == []
    cur = as_user(U(WA), D.get_destination, d["id"]); assert cur["fail_reason"] == "BLOCKED_BY_POLICY"
    assert rows("SELECT status, attempts FROM outbox WHERE destination_id=%s", d["id"])[0] == {"status": "failed", "attempts": 1}
    t = as_user(U(WA), D.test_destination, d["id"]); assert not t["ok"] and t["reason"] == "BLOCKED_BY_POLICY"

def test_filters_and_patterns(env, http):
    D = env; d = mk(D, WA, http.url + "/h", events=())
    as_user(U(WA), D.create_subscription, D.SubIn(destination_id=d["id"], event_type="alert.*", filter={"min_severity": "warn", "project_id": [3, 4]}))
    n = lambda *a, **k: run(D.emit(*a, **k))
    assert n("alert.fired", {"severity": "info"}, WA, project_id=3) == 0       # below min severity
    assert n("alert.fired", {"severity": "critical"}, WA, project_id=9) == 0   # other project
    assert n("alert.fired", {"severity": "critical"}, WA) == 0                 # the event cannot answer project_id
    assert n("alert.fired", {"severity": "warn"}, WA, project_id=4) == 1
    assert n("system.health.warn", {"severity": "warn"}, WA, project_id=3) == 0  # pattern alert.* only
    with pytest.raises(ValueError): n("nope.unknown", {}, WA)

def test_workspaces_users_and_demo(env, http):
    D = env; d = mk(D, WA, http.url + "/h")
    assert run(D.emit("alert.fired", {"rule": "r"}, WB)) == 0 and http.got == []              # B's events never reach A's destination
    assert as_user(U(WB), D.list_destinations) == []
    with pytest.raises(Exception) as e: as_user(U(WB), D.get_destination, d["id"]); assert e.value.status_code == 404
    for role, demo in (("editor", False), ("viewer", False), ("admin", True)):
        with pytest.raises(Exception) as e: as_user(U(WA, role, 7, demo), D.create_destination, D.DestIn(name="x", kind="webhook", config={"url": http.url}))
        assert e.value.status_code == 403
    with pytest.raises(Exception) as e: as_user(U(WA, "editor", 7), D.patch_destination, d["id"], D.DestUpd(name="z"))
    assert e.value.status_code == 404                                                           # an editor cannot even see a workspace destination
    assert as_user(U(WA, "admin", 9, True), D.emit, "alert.fired", {"rule": "r"}, WA) == 0 and http.got == []     # demo: emit is a no-op
    mine = as_user(U(WA, "editor", 7), D.create_my_destination, D.DestIn(name="mine", kind="webhook", config={"url": http.url + "/m"}))
    assert mine["owner_user_id"] == 7 and [x["id"] for x in as_user(U(WA, "editor", 7), D.my_destinations)] == [mine["id"]]
    assert as_user(U(WA, "editor", 8), D.my_destinations) == [] and as_user(U(WA, "editor", 8), D.list_destinations) == []
    assert {x["id"] for x in as_user(U(WA, "admin", 1), D.list_destinations)} == {d["id"], mine["id"]}   # the admin sees it too
    with pytest.raises(Exception) as e: as_user(U(WA, "editor", 8), D.get_destination, mine["id"])
    assert e.value.status_code == 404

def test_email_and_telegram_destinations_use_text_senders(env, smtp, http, monkeypatch):
    D = env
    run(real_q("INSERT INTO ws_secrets(workspace_id,name,value_enc,allowed_hosts) VALUES(%s,'T121_PW',%s,%s)", (WA, __import__("ws_secrets").seal_value(WA, "T121_PW", "pw1"), ["127.0.0.1"])))
    run(real_q("INSERT INTO ws_secrets(workspace_id,name,value_enc,allowed_hosts) VALUES(%s,'T121_TG',%s,%s)", (WA, __import__("ws_secrets").seal_value(WA, "T121_TG", "123:tok"), ["127.0.0.1"])))
    as_user(U(WA), D.create_destination, D.DestIn(name="mail", kind="email", events=["alert.*"], config={"target": "a@x.test", "host": "127.0.0.1", "port": smtp.port, "user": "bot", "starttls": False, "password": "{{secret:T121_PW}}"}))
    monkeypatch.setenv("TELEGRAM_API_BASE", http.url)
    as_user(U(WA), D.create_destination, D.DestIn(name="tg", kind="telegram", events=["alert.*"], config={"target": "-100", "secret": "T121_TG"}))
    with pytest.raises(Exception) as e: as_user(U(WA), D.create_destination, D.DestIn(name="bad", kind="email", config={"target": "a@x.test", "host": "h", "password": "plain"}))
    assert e.value.status_code == 422
    assert run(D.emit("alert.fired", {"title": "t121 title", "text": "boom"}, WA)) == 2
    assert smtp.messages == 1 and "boom" in smtp.data and http.got[0]["path"] == "/bot123:tok/sendMessage"
    assert {r["status"] for r in rows("SELECT status FROM outbox WHERE ws_id=%s", WA)} == {"sent"}

def test_insights_channels_and_health_routing(env, http, monkeypatch):
    D = env; mk(D, WA, http.url + "/h", events=("insights.*",))
    ch = run(D.channels_for(WA, "insights.weekly")); assert len(ch) == 1 and ch[0]["kind"] == "webhook" and ch[0]["target"].endswith("/h") and run(D.channels_for(WB, "insights.weekly")) == []
    import health_watch, insights, notify
    health_watch.SENT.clear(); sent, emitted = [], []
    monkeypatch.setattr(health_watch, "_disk", lambda: type("D", (), {"total": 100, "used": 97, "free": 3})())
    monkeypatch.setattr(health_watch, "_meminfo", lambda: {"MemTotal": 100, "MemAvailable": 50})
    async def pg(): return 1
    monkeypatch.setattr(health_watch, "_pg_bytes", pg)
    async def fake_emit(t, data, ws, **k): emitted.append((t, data["kind"], ws)); return 1
    async def chans(ws): return [{"kind": "email", "to": "a@b.c"}]
    async def send(ws, c, title, text, link=None): sent.append(text); return "ok"
    monkeypatch.setattr(D, "emit", fake_emit); monkeypatch.setattr(insights, "weekly_channels", chans); monkeypatch.setattr(notify, "send", send)
    run(health_watch.run_check()); assert emitted == [("system.health.critical", "disk", 1)] and sent == []     # delivered by destinations: the old path stays quiet
    assert sent == []                                                                                              # 12.10: no old channel list any more

# ------------------------------------------------------------------ 12.2 Slack Block Kit + HTML email
ALERT = {"rule": "Chi phí cao", "workspace": "Moza", "project_id": 3, "severity": "critical", "metric": "Total Cost", "value": 12.5, "threshold": 5, "op": ">=", "unit": "usd", "agg": "avg", "window_min": 15}

def _secret(ws, name, value, hosts):
    run(real_q("INSERT INTO ws_secrets(workspace_id,name,value_enc,allowed_hosts) VALUES(%s,%s,%s,%s)", (ws, name, __import__("ws_secrets").seal_value(ws, name, value), hosts)))

def test_render_slack_blocks_buttons_and_resolved(monkeypatch):
    import notify
    monkeypatch.setenv("AOS_PUBLIC_URL", "https://aos.example.test/")
    p = notify.render_slack({"type": "alert.fired", "data": ALERT}); b = p["blocks"]
    assert b[0]["type"] == "header" and b[0]["text"]["text"] == "⚠ Chi phí cao – Moza"
    assert b[1]["text"]["text"] == "Total Cost: $12.50 ≥ $5.00 · avg · 15 min"
    els = b[2]["elements"]; assert [e["text"]["text"] for e in els] == ["View alert", "View runs"] and els[1]["url"] == "https://aos.example.test/projects?p=3" and els[0]["url"].startswith("https://aos.example.test/")
    assert notify.render_slack({"type": "alert.resolved", "data": ALERT})["blocks"][0]["text"]["text"].startswith("✅ Resolved")
    monkeypatch.delenv("AOS_PUBLIC_URL"); assert [x["type"] for x in notify.render_slack({"type": "alert.fired", "data": ALERT})["blocks"]] == ["header", "section"]   # no buttons without a public URL
    o = notify.render_slack({"type": "rule.error", "data": {"title": "t <b>", "text": "a & b"}}); assert o["blocks"][0]["text"]["text"] == "*t &lt;b&gt;*\na &amp; b" and o["text"]

def test_slack_destination_receives_blocks(env, http, monkeypatch):
    D = env; monkeypatch.setenv("SLACK_HOOK_HOST", "127.0.0.1"); monkeypatch.setenv("AOS_PUBLIC_URL", "https://aos.example.test")
    _secret(WA, "T122_SL", http.url + "/services/T/B/X", ["127.0.0.1"])
    as_user(U(WA), D.create_destination, D.DestIn(name="slack", kind="slack_webhook", events=["alert.*", "rule.*"], config={"secret": "T122_SL"}))
    assert run(D.emit("alert.fired", {k: v for k, v in ALERT.items() if k != "workspace"}, WA)) == 1
    g = http.got[0]; body = json.loads(g["raw"]); assert g["path"] == "/services/T/B/X"
    assert body["blocks"][1]["text"]["text"] == "Total Cost: $12.50 ≥ $5.00 · avg · 15 min" and len(body["blocks"][2]["elements"]) == 2
    assert body["blocks"][0]["text"]["text"].startswith("⚠ Chi phí cao")
    run(D.emit("rule.error", {"title": "Rule x", "text": "lỗi"}, WA)); assert json.loads(http.got[1]["raw"])["blocks"][0]["text"]["text"] == "*Rule x*\nlỗi"

def test_bad_slack_host_is_permanent_and_leaks_nothing(env, http, monkeypatch):
    D = env; monkeypatch.setenv("SLACK_HOOK_HOST", "127.0.0.1"); url = "http://evil.test/hook/TOKEN-t122"
    _secret(WA, "T122_BAD", url, ["127.0.0.1"])
    d = as_user(U(WA), D.create_destination, D.DestIn(name="bad", kind="slack_webhook", events=["alert.*"], config={"secret": "T122_BAD"}))
    run(D.emit("alert.fired", ALERT, WA)); assert http.got == []
    row = rows("SELECT status, attempts, detail, last_error FROM outbox WHERE destination_id=%s", d["id"])[0]
    assert row["status"] == "failed" and row["attempts"] == 1 and "TOKEN-t122" not in json.dumps(row) and "evil.test" not in json.dumps(row)
    assert "TOKEN-t122" not in json.dumps(as_user(U(WA), D.deliveries, d["id"]), default=str)

def test_email_is_multipart_with_escaped_html(env, smtp):
    import email, email.policy
    D = env; _secret(WA, "T122_PW", "pw", ["127.0.0.1"])
    as_user(U(WA), D.create_destination, D.DestIn(name="mail", kind="email", events=["alert.*"], config={"target": "a@x.test", "host": "127.0.0.1", "port": smtp.port, "user": "bot", "starttls": False, "password": "{{secret:T122_PW}}"}))
    evil = "<script>alert(1)</script>"
    assert run(D.emit("alert.fired", {**ALERT, "rule": evil, "title": "t122 " + evil, "text": "boom & <b>", "link": "javascript:alert(1)"}, WA)) == 1 and smtp.messages == 1
    m = email.message_from_string(smtp.data, policy=email.policy.default)
    assert m.get_content_type() == "multipart/alternative" and [p.get_content_type() for p in m.iter_parts()] == ["text/plain", "text/html"]
    h = m.get_body(("html",)).get_content(); pl = m.get_body(("plain",)).get_content()
    assert "<script>" not in h and "&lt;script&gt;" in h and "boom &amp; &lt;b&gt;" in h and "<table" in h and "Total Cost" in h and "12.5" in h
    assert "href=" not in h                                                       # a javascript: link never becomes an anchor
    assert "boom & <b>" in pl and "<table" not in pl                                 # the plain part is plain text, not HTML

# ------------------------------------------------------------------ migrations (throwaway schema)
def test_migrations_0100_0101_up_dry_run_down():
    import migrations as M, importlib.util
    mods = {v: m for v, m, _ in M._load()}; m0, m1 = mods["0100"], mods["0101"]
    sch = "t121_" + uuid.uuid4().hex[:8]
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
            q = M._q(c)
            for ddl in ("CREATE TABLE projects(id serial primary key, workspace_id int)", "CREATE TABLE outbox(id serial primary key, kind text)",
                        "CREATE TABLE alert_rules(id serial primary key, project_id int, channel text, target text, channel_cfg jsonb, enabled bool default true)"):
                await q(ddl)
            await q("INSERT INTO projects(workspace_id) VALUES (1),(2)")
            for pid, ch, tg, cfg in ((1, "webhook", "https://hooks.example.test/a", '{"signing_secret":"KEY1"}'), (1, "webhook", "https://hooks.example.test/a", '{"signing_secret":"KEY1"}'),
                                     (1, "slack", None, '{"secret":"SL"}'), (2, "email", "o@x.test", '{"host":"smtp.x.test"}'), (2, "sms", "1", None)):
                await q("INSERT INTO alert_rules(project_id,channel,target,channel_cfg) VALUES(%s,%s,%s,%s::jsonb)", (pid, ch, tg, cfg))
            assert "ALTER TABLE outbox" in " ".join(await m0.plan(q)) and "3 notify_destinations" in (await _plan_after(m0, m1, q))[0]
            await m0.up(q); await m0.up(q)                                           # re-runnable
            assert await q("SELECT count(*) FROM notify_destinations") == [(0,)] and await q("SELECT count(*) FROM alert_rules") == [(5,)]
            await m1.up(q); await m1.up(q)                                           # idempotent
            d = await q("SELECT workspace_id, kind, signing_secret_ref, config FROM notify_destinations ORDER BY id")
            assert sorted((r[0], r[1], r[2]) for r in d) == sorted([(1, "slack_webhook", None), (1, "webhook", "KEY1"), (2, "email", None)])
            subs = {(r[0], r[1]) for r in await q("SELECT workspace_id, event_type FROM notify_subscriptions")}
            assert {(1, "system.*"), (1, "alert.*"), (2, "alert.*"), (2, "insights.*"), (2, "engine.*")} <= subs and (2, "system.*") not in subs
            assert await q("SELECT count(*) FROM alert_rules") == [(5,)]            # old columns untouched
            await q("INSERT INTO notify_destinations(workspace_id,name,kind,created_by) VALUES(1,'own','webhook','someone')")
            await m1.down(q); assert await q("SELECT name FROM notify_destinations") == [("own",)]
            await m0.down(q); assert await q("SELECT to_regclass('notify_destinations')") == [(None,)]
            assert "destination_id" not in " ".join(r[0] for r in await q("SELECT column_name FROM information_schema.columns WHERE table_name='outbox' AND table_schema=%s", (sch,)))
    with psycopg.connect(PG, autocommit=True) as c: c.execute(f'CREATE SCHEMA "{sch}"')
    try: run(go())
    finally:
        with psycopg.connect(PG, autocommit=True) as c: c.execute(f'DROP SCHEMA "{sch}" CASCADE')

async def _plan_after(m0, m1, q):
    await m0.up(q); out = await m1.plan(q); await m0.down(q); return out

# ------------------------------------------------------------------ over HTTP: our own server on :8050
PORT = 8050; B = f"http://127.0.0.1:{PORT}"; ADMIN = ("moza", "phase0-local"); PW = "phase0-gd121-local"; SUF = uuid.uuid4().hex[:6]

@pytest.fixture(scope="module")
def srv():
    envv = {**os.environ, "PORT": str(PORT), "PG_URI": PG, "AOS_EGRESS_ALLOW": "127.0.0.0/8,::1", "TELEGRAM_POLL": "0", "SLACK_SOCKET": "0", "AOS_ZALO_PERSONAL": "0"}
    os.makedirs(os.path.join(ROOT, "var"), exist_ok=True); log = open(os.path.join(ROOT, "var", f"server_{PORT}.log"), "wb")
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "overlay", "run_local.py")], cwd=ROOT, env=envv, stdout=log, stderr=subprocess.STDOUT)
    try:
        t0 = time.time()
        while time.time() - t0 < 300:
            try:
                if httpx.get(B + "/health", timeout=3).status_code < 500: break
            except httpx.HTTPError: time.sleep(1)
        else: raise AssertionError("own server on :8050 did not start")
        yield p
    finally:
        p.terminate()                                                                     # this PID only: never pkill run_local.py
        try: p.wait(timeout=20)
        except subprocess.TimeoutExpired: p.kill()
        log.close()

def test_http_roles_demo_and_workspace_isolation(srv, http):
    cl = {}
    with httpx.Client(base_url=B, auth=ADMIN, timeout=60) as a:
        w1 = a.post("/api/admin/workspaces", json={"name": f"nt-w1-{SUF}"}).json()["id"]; w2 = a.post("/api/admin/workspaces", json={"name": f"nt-w2-{SUF}"}).json()["id"]
        for k, role, ws in (("admin", "admin", w1), ("editor", "editor", w1), ("viewer", "viewer", w1), ("other", "admin", w2)):
            r = a.post("/api/admin/users", json={"username": f"nt_{k}_{SUF}", "password": PW, "role": role, "workspace_id": ws}); assert r.status_code == 200, r.text
            cl[k] = httpx.Client(base_url=B, auth=(f"nt_{k}_{SUF}", PW), timeout=60)
        r = a.post("/api/admin/users", json={"username": f"nt_demo_{SUF}", "password": PW, "role": "viewer", "workspace_id": w1, "demo": True})
        cl["demo"] = httpx.Client(base_url=B, auth=(f"nt_demo_{SUF}", PW), timeout=60) if r.status_code == 200 else None
    try:
        body = {"name": "http t121", "kind": "webhook", "config": {"url": http.url + "/h"}, "events": ["alert.*"]}
        assert cl["editor"].post("/api/notify/destinations", json=body).status_code == 403
        assert cl["viewer"].post("/api/notify/destinations", json=body).status_code == 403
        if cl["demo"]: assert cl["demo"].post("/api/notify/destinations", json=body).status_code == 403 and cl["demo"].post("/api/me/destinations", json=body).status_code == 403
        r = cl["admin"].post("/api/notify/destinations", json=body); assert r.status_code == 200, r.text
        d = r.json(); key = d["signing_key"]
        t = cl["admin"].post(f"/api/notify/destinations/{d['id']}/test"); assert t.status_code == 200 and t.json()["ok"] and t.json()["http_status"] == 200 and t.json()["health"] == "HEALTHY"
        g = http.got[-1]; assert verify(g["raw"], g["headers"]["x-langsmith-signature"], key) and json.loads(g["raw"])["type"] == "alert.test"
        assert cl["other"].get(f"/api/notify/destinations/{d['id']}").status_code == 404 and cl["other"].get("/api/notify/destinations").json() == []
        assert cl["editor"].get(f"/api/notify/destinations/{d['id']}").status_code == 404
        assert cl["other"].post(f"/api/notify/destinations/{d['id']}/roll_secret").status_code == 404
        r = cl["admin"].post(f"/api/notify/destinations/{d['id']}/roll_secret"); assert r.status_code == 200 and r.json()["signing_key"] != key
        assert key not in cl["admin"].get("/api/notify/destinations").text and key not in cl["admin"].get(f"/api/notify/destinations/{d['id']}/deliveries").text
        assert any(x["name"] == "alert.fired" for x in cl["editor"].get("/api/notify/event_types").json())
        mine = cl["editor"].post("/api/me/destinations", json={**body, "name": "mine"}); assert mine.status_code == 200 and mine.json()["owner_user_id"]
        assert len(cl["editor"].get("/api/me/destinations").json()) == 1 and cl["admin"].get("/api/me/destinations").json() == []
        assert {x["id"] for x in cl["admin"].get("/api/notify/destinations").json()} == {d["id"], mine.json()["id"]}
        assert cl["admin"].delete(f"/api/notify/destinations/{d['id']}").status_code == 200
        assert cl["admin"].get("/api/obs/outbox").status_code == 200                      # the outbox page still serializes (raw_body is not selected)
    finally:
        for c in cl.values():
            if c: c.close()
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute("DELETE FROM notify_destinations WHERE workspace_id IN (SELECT id FROM workspaces WHERE name LIKE %s)", (f"nt-w_-{SUF}",))
            c.execute("DELETE FROM ws_secrets WHERE name LIKE 'aos_dest_%%_sign' AND workspace_id IN (SELECT id FROM workspaces WHERE name LIKE %s)", (f"nt-w_-{SUF}",))
            c.execute("DELETE FROM users WHERE username LIKE %s", (f"nt\\_%\\_{SUF}",))
