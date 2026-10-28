"""GĐ12.48 end-to-end scenario of part A (the story of docs/KICH_BAN_GD12.md), $0: fake LLM judge, fake Slack / SMTP / webhook receivers on loopback, no real network.
Test 1 (in-process on the local Postgres, two throw-away workspaces of gd12_world): an agent starts failing -> traces with TTFT / token / cost columns -> prebuilt dashboard numbers equal the
rows -> accent-free full-text search + `status:error` -> an alert fires to a signed webhook, a Slack Block Kit message (2 buttons) and an HTML email -> an automation rule samples the failing
traces into a dataset and runs a judge -> the agent is "fixed" and the alert resolves ("resolved" delivered) -> retention at +40 days keeps the rated traces only -> a receiver that answers 500
three times shows FAILING in the destination list -> part B: the dataset exports to CSV.
Test 2 (part B, lightly; over HTTP on OUR OWN server on :8066, killed by its PID): a skill template is read, linted and copied to an agent, an invite link is mailed and accepted.
TODO GĐ13: engine issue / propose / verify steps of the auto-fix loop are NOT in this scenario.  TODO GĐ14: the SDK-through-`/ls` step is NOT in this scenario."""
import asyncio, csv, datetime as dt, io, json, os, re, socketserver, subprocess, sys, threading, time, uuid
from http.server import HTTPServer
import httpx, psycopg, pytest
from gd12_world import NOW, PG, as_user, run, sysq, with_world
from test_gd6_notify import _H, _S, Fake, http, smtp  # noqa: F401  (fake receivers on 127.0.0.1)
from test_gd12_rules import fake_judge, migrated, mk_ds, mk_ev, n_ex, rin, verify  # noqa: F401  (migrations applied once; scripted judge; rule helpers)

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app"))
import deepagent_api  # noqa: E402,F401  (import order: deepagent_api first)
import alerts, charts, dashboards, destinations, evals_ds, obs, rules, ws_secrets  # noqa: E402

M = dt.timedelta(minutes=1); D = lambda n: dt.timedelta(days=n)   # noqa: E731
SLACK_PATH = "/services/T/B/E2E"
ERR = "Lỗi kết nối kho"

@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("PG_URI", PG); monkeypatch.setenv("AOS_EGRESS_ALLOW", "127.0.0.0/8,::1"); monkeypatch.setenv("AOS_PUBLIC_URL", "https://aos.example.test")
    monkeypatch.setenv("SLACK_HOOK_HOST", "127.0.0.1"); monkeypatch.delenv("TELEGRAM_API_BASE", raising=False)
    charts._CACHE.clear(); return monkeypatch

async def tidy(A, W):
    ws, pids = [W["a"]["ws"], W["b"]["ws"]], [W["a"]["pid"], W["b"]["pid"]]
    for sql, args in (("DELETE FROM automation_rules WHERE project_id = ANY(%s)", (pids,)), ("DELETE FROM alert_rules WHERE project_id = ANY(%s)", (pids,)),
                      ("DELETE FROM datasets WHERE workspace_id = ANY(%s)", (ws,)), ("DELETE FROM evaluators WHERE workspace_id = ANY(%s)", (ws,)),
                      ("DELETE FROM outbox WHERE ws_id = ANY(%s)", (ws,)), ("DELETE FROM notify_destinations WHERE workspace_id = ANY(%s)", (ws,)),
                      ("DELETE FROM ws_secrets WHERE workspace_id = ANY(%s)", (ws,)), ("DELETE FROM crypto_deks WHERE workspace_id = ANY(%s)", (ws,)),
                      ("DELETE FROM crypto_ws WHERE workspace_id = ANY(%s)", (ws,))):
        try: await sysq(A, sql, args)
        except Exception as e: print("tidy:", sql[:40], str(e)[:80])   # noqa: BLE001

def card(out, cid): return [c for c in out["cards"] if c["id"] == cid][0]
def got(srv, path=None, typ=None):
    out = [{**json.loads(g["raw"]), "_g": g} for g in srv.got if path is None or g["path"] == path]
    return [e for e in out if typ is None or e.get("type") == typ]

def test_part_a_story_failing_agent_to_resolved_alert(env, http, smtp):
    fake_judge(env)
    bad = HTTPServer(("127.0.0.1", 0), _H); bad.fake = Fake(code=500); threading.Thread(target=bad.serve_forever, daemon=True).start()
    bad_url = f"http://127.0.0.1:{bad.server_port}"
    async def go(A, admin, W):
        for t in ("automation_applied", "automation_log"): await admin._policy(t, "aos_ws() IS NULL OR (" + admin.RLS_CHILD[t] + ")")   # what rls_init does at boot (12.12)
        a = W["a"]; ws, pid = a["ws"], a["pid"]; me = lambda fn, w=ws, **k: as_user(admin, w, fn, **k)   # noqa: E731
        try:
            # ---- step 1: destinations subscribed to alert.* (signed webhook, Slack, email); the signing key is shown once
            d_hook = await me(lambda: destinations.create_destination(destinations.DestIn(name="e2e hook", kind="webhook", config={"url": http.url + "/h"}, events=["alert.*"])))
            key = d_hook["signing_key"]; assert key and d_hook["health"] == "HEALTHY"
            await sysq(A, "INSERT INTO ws_secrets(workspace_id,name,value_enc,allowed_hosts) VALUES(%s,'E2E_SL',%s,%s)", (ws, ws_secrets.seal_value(ws, "E2E_SL", http.url + SLACK_PATH), ["127.0.0.1"]))
            await sysq(A, "INSERT INTO ws_secrets(workspace_id,name,value_enc,allowed_hosts) VALUES(%s,'E2E_PW',%s,%s)", (ws, ws_secrets.seal_value(ws, "E2E_PW", "pw-e2e"), ["127.0.0.1"]))
            await me(lambda: destinations.create_destination(destinations.DestIn(name="e2e slack", kind="slack_webhook", events=["alert.*"], config={"secret": "E2E_SL"})))
            await me(lambda: destinations.create_destination(destinations.DestIn(name="e2e mail", kind="email", events=["alert.*"], config={
                "target": "ops@x.test", "host": "127.0.0.1", "port": smtp.port, "user": "bot", "starttls": False, "password": "{{secret:E2E_PW}}"})))
            # ---- step 2: the agent starts failing -> traces with TTFT / tokens / cost filled
            ok_ids, err_ids = [], []
            for i in range(2):   # two good answers first
                r, _ = await run(obs, pid, start=NOW() - (20 - i) * M, name="agent-demo", model="fake-model", usage={"input_tokens": 100, "output_tokens": 40}, cost_usd=0.002,
                                 first_token_time=NOW() - (20 - i) * M + dt.timedelta(milliseconds=300), outputs={"a": "xong [judge-score:0.9]"}); ok_ids.append(r)
            for i in range(6):
                st = NOW() - (12 - i) * M
                r, _ = await run(obs, pid, start=st, name="agent-demo", model="fake-model", usage={"input_tokens": 120, "output_tokens": 0}, cost_usd=0.003, error=ERR,
                                 first_token_time=st + dt.timedelta(milliseconds=450), outputs={"a": f"không có kết quả [judge-score:0.2] #{i}"}); err_ids.append(r)
            rows = await me(lambda: obs.runs(pid, since_hours=1))
            assert len(rows) == 8 and all(r["first_token_time"] and r["total_tokens"] and float(r["cost_usd"]) > 0 for r in rows)   # the TTFT / tokens / cost columns are filled, none blank
            assert {str(r["id"]) for r in rows if r["status"] == "error"} == set(err_ids)
            # ---- step 3: the prebuilt dashboard shows the same numbers as the rows
            async def dash(): charts._CACHE.clear(); return await me(lambda: dashboards.prebuilt(project_id=pid, environment=None, agent=None, group_by=None, tz="UTC", hours=6))
            out = await dash()
            assert all(c["error"] is None for c in out["cards"]) and card(out, "trace_count")["result"]["totals"]["s0"] == 8
            assert card(out, "trace_error_rate")["result"]["totals"]["s0"] == pytest.approx(6 / 8)
            assert card(out, "cost_total")["result"]["totals"]["s0"] == pytest.approx(sum(float(r["cost_usd"]) for r in rows))
            # ---- step 4: search by a word typed without accents, and a status filter
            f = lambda s: "search(" + json.dumps(s) + ")"   # noqa: E731
            found = {str(r["id"]) for r in await me(lambda: obs.runs(pid, filter=f("loi ket noi kho"), limit=100))}
            assert found == set(err_ids)                                     # 'loi' finds 'Lỗi'
            q = {str(r["id"]) for r in await me(lambda: obs.runs(pid, query="status:error", limit=100))}
            assert q == set(err_ids) and {str(r["id"]) for r in await me(lambda: obs.runs(pid, query="loi", limit=100))} == set(err_ids)
            assert await me(lambda: obs.runs(pid, filter=f("loi ket noi kho")), w=W["b"]["ws"]) == []   # workspace B sees nothing
            # ---- step 5: the alert (preview = what fires), then the real evaluation -> signed webhook + Slack Block Kit + HTML email
            body = alerts.AlertIn(project_id=pid, name="e2e error rate", metric="error_rate", op="gt", threshold=0.5, window_min=60, channel="log", renotify_min=5, severity=2)
            rid = (await me(lambda: alerts.alert_new(body)))["id"]
            pts = await me(lambda: alerts.alert_preview(alerts.PreviewIn(project_id=pid, hours=3, rule=alerts.AlertEdit(metric="error_rate", op="gt", threshold=0.5, window_min=60))))
            assert any(p["fires"] for p in pts)
            row = await sysq(A, "SELECT * FROM alert_rules WHERE id=%s", (rid,), one=True); charts._CACHE.clear()
            assert await alerts.evaluate(row) == "fired"
            fired = got(http, "/h", "alert.fired"); assert len(fired) == 1
            g = fired[0]["_g"]; assert verify(g["raw"], g["headers"]["x-langsmith-signature"], key)                       # signed payload
            assert fired[0]["data"]["triggered_metric_value"] == pytest.approx(card(out, "trace_error_rate")["result"]["totals"]["s0"])   # alert value == dashboard error rate
            sl = json.loads(http.got[[x["path"] for x in http.got].index(SLACK_PATH)]["raw"])
            assert sl["blocks"][0]["type"] == "header" and len(sl["blocks"][2]["elements"]) == 2           # Block Kit with 2 buttons
            assert smtp.messages == 1 and "<table" in smtp.data and "text/html" in smtp.data                                  # HTML email
            assert (await sysq(A, "SELECT state FROM alert_rules WHERE id=%s", (rid,), one=True))["state"] == "firing"
            # ---- step 6: an automation rule samples the failing traces into a dataset and runs a judge; a human rates one good trace
            ds, ev = await mk_ds(ws, f"t12-e2e-{W['tag']}"), await mk_ev(ws, f"t12-e2e-{W['tag']}")
            rule = await me(lambda: rules.rule_new(rin(pid, [{"type": "add_to_dataset", "dataset_id": ds}, {"type": "run_evaluator", "evaluator_id": ev}], filter='eq(status,"error")', backfill_hours=1)))
            await rules.apply_rules(project_ids=[pid])
            assert await n_ex(ds) == 6
            sc = await sysq(A, "SELECT run_id, score FROM feedback WHERE run_id = ANY(%s) AND key='quality'", (err_ids,)); assert len(sc) == 6 and {round(x["score"], 1) for x in sc} == {0.2}   # judge scores
            await rules.apply_rules(project_ids=[pid]); assert await n_ex(ds) == 6                                      # applied once per (rule, run)
            await me(lambda: obs.add_feedback(obs.FbIn(run_id=ok_ids[0], key="thumb", score=1.0), "human", by="e2e"))
            assert {x["reason"] for x in await sysq(A, "SELECT reason FROM trace_retention WHERE trace_id = ANY(%s)", (err_ids,))} == {"evaluator"}
            assert (await sysq(A, "SELECT reason FROM trace_retention WHERE trace_id=%s", (ok_ids[0],), one=True))["reason"] == "feedback"
            # ---- step 7: the agent is fixed -> the error rate drops -> the alert resolves and "resolved" is delivered
            fixed = []
            for i in range(30):
                st = NOW() - 3 * M + dt.timedelta(seconds=i); r, _ = await run(obs, pid, start=st, name="agent-demo", model="fake-model", usage={"input_tokens": 100, "output_tokens": 40}, cost_usd=0.002,
                                                                           first_token_time=st + dt.timedelta(milliseconds=250)); fixed.append(r)
            row = await sysq(A, "SELECT * FROM alert_rules WHERE id=%s", (rid,), one=True); charts._CACHE.clear()
            assert await alerts.evaluate(row) == "resolved"
            res = got(http, "/h", "alert.resolved"); assert len(res) == 1 and res[0]["data"]["alert_state"] == "ok"
            assert json.loads([x for x in http.got if x["path"] == SLACK_PATH][-1]["raw"])["blocks"][0]["text"]["text"].startswith("✅ Resolved") and smtp.messages == 2
            out = await dash(); rate = card(out, "trace_error_rate")["result"]["totals"]["s0"]
            assert rate == pytest.approx(6 / 38) and (await sysq(A, "SELECT last_value FROM alert_rules WHERE id=%s", (rid,), one=True))["last_value"] == pytest.approx(rate)   # same number everywhere
            assert [e["kind"] for e in await sysq(A, "SELECT kind FROM alert_events WHERE rule_id=%s ORDER BY id", (rid,))] == ["fired", "resolved"]
            # ---- step 8: retention at +40 days keeps the rated traces (judge + human) and removes the unrated ones
            await obs.retention(now=NOW() + D(40), project_ids=[pid], only_partitions=())
            left = {str(x["id"]) for x in await sysq(A, "SELECT id FROM tr_runs WHERE project_id=%s", (pid,))}
            assert left == set(err_ids) | {ok_ids[0]}, (len(left), len(err_ids))
            # ---- step 9: a receiver that answers 500 three times -> FAILING in the destination list
            d_bad = await me(lambda: destinations.create_destination(destinations.DestIn(name="e2e broken", kind="webhook", config={"url": bad_url + "/x"}, events=["alert.*"])))
            for i in range(3): await destinations.emit("alert.fired", {"rule": f"again{i}", "severity": "warn"}, ws, project_id=pid)
            lst = {x["id"]: x for x in await me(destinations.list_destinations)}
            assert lst[d_bad["id"]]["health"] == "FAILING" and lst[d_bad["id"]]["consecutive_failures"] == 3 and lst[d_hook["id"]]["health"] == "HEALTHY"
            assert len(bad.fake.got) >= 3
            # ---- part B (light): the dataset made by the rule exports to CSV
            resp = await me(lambda: evals_ds.ds_export(ds, format="csv"))
            table = list(csv.reader(io.StringIO(resp.body.decode("utf-8").lstrip("﻿")))); assert len(table) == 7           # header + 6 examples
            # workspace B cannot read A's alert, rule or dataset export
            from fastapi import HTTPException
            for fn in (lambda: alerts.alert_get(rid), lambda: rules.rule_logs(rule["id"]), lambda: evals_ds.ds_export(ds, format="csv")):
                with pytest.raises(HTTPException) as e: await me(fn, w=W["b"]["ws"])
                assert e.value.status_code == 404
        finally: await tidy(A, W)
    try: with_world(go)
    finally: bad.shutdown(); bad.server_close()

# ====================================================================== part B, lightly: over HTTP on our own server
PORT = 8066; B = f"http://127.0.0.1:{PORT}"; ADMIN = ("moza", "phase0-local"); PW = "phase0-e2e12-local"; SUF = uuid.uuid4().hex[:6]
TPL = "bao-gia"

@pytest.fixture(scope="module")
def srv():
    ss = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _S); ss.daemon_threads = True
    f = ss.fake = Fake(); f.rcpt = []; f.messages = 0; f.auth = None; f.data = ""; f.port = ss.server_address[1]
    threading.Thread(target=ss.serve_forever, daemon=True).start()
    envv = {**os.environ, "PORT": str(PORT), "PG_URI": PG, "AOS_EGRESS_ALLOW": "127.0.0.0/8,::1", "TELEGRAM_POLL": "0", "SLACK_SOCKET": "0", "AOS_ZALO_PERSONAL": "0", "AOS_PUBLIC_URL": B,
            "SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(f.port), "SMTP_STARTTLS": "0", "SMTP_FROM": "agentos@test.local"}
    os.makedirs(os.path.join(ROOT, "var"), exist_ok=True); log = open(os.path.join(ROOT, "var", f"server_{PORT}.log"), "wb")
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "overlay", "run_local.py")], cwd=ROOT, env=envv, stdout=log, stderr=subprocess.STDOUT)
    try:
        t0 = time.time()
        while time.time() - t0 < 300:
            try:
                if httpx.get(B + "/health", timeout=3).status_code < 500: break
            except httpx.HTTPError: time.sleep(1)
        else: raise AssertionError(f"own server on :{PORT} did not start")
        yield f
    finally:
        p.terminate()                                                           # this PID only: never pkill run_local.py
        try: p.wait(timeout=20)
        except subprocess.TimeoutExpired: p.kill()
        log.close(); ss.shutdown(); ss.server_close()

def test_part_b_light_skill_template_copied_and_linted_invite_accepted(srv):
    import email
    wid = None; names = [f"e2e_adm_{SUF}", f"e2e_new_{SUF}"]
    with httpx.Client(base_url=B, auth=ADMIN, timeout=60) as sup:
        try:
            wid = sup.post("/api/admin/workspaces", json={"name": f"e2e-{SUF}"}).json()["id"]
            r = sup.post("/api/admin/users", json={"username": names[0], "password": PW, "role": "admin", "workspace_id": wid}); assert r.status_code == 200, r.text
            with httpx.Client(base_url=B, auth=(names[0], PW), timeout=150) as adm:
                # a skill template is read, linted with zero errors and copied (private) to an agent
                tpl = adm.get(f"/api/skills/template/{TPL}"); assert tpl.status_code == 200 and tpl.json()["files"]["SKILL.md"].startswith("---")
                lint = adm.post("/api/skills/lint", json={"name": TPL, "files": tpl.json()["files"]}); assert lint.status_code == 200 and lint.json()["errors"] == [], lint.text
                ag = adm.post("/api/agents", json={"name": f"e2e {SUF}", "system_prompt": "Bạn là trợ lý.", "skills": [], "filesystem": False}); assert ag.status_code == 200, ag.text
                cp = adm.post("/api/skills/from-template", json={"template": TPL, "agent_id": ag.json()["id"]}); assert cp.status_code == 200 and cp.json()["visibility"] == "private", cp.text
                # an invite link is mailed (to the fake SMTP) and accepted by a new person
                before = srv.messages
                inv = adm.post(f"/api/admin/workspaces/{wid}/invites", json={"role": "viewer", "email_note": f"{names[1]}@example.com"}); assert inv.status_code == 200, inv.text
                link = inv.json()["link"]; assert srv.messages == before + 1
                mail = "\n".join(p.get_payload(decode=True).decode("utf-8", "replace") for p in email.message_from_string(srv.data).walk() if not p.is_multipart())
                assert link in mail and link.startswith(B + "/invite/")
                token = link.rsplit("/invite/", 1)[1]
                ac = httpx.post(f"{B}/api/invites/{token}/accept", headers={"X-Forwarded-For": "10.12.66.7"}, json={"username": names[1], "password": PW}, timeout=60)
                assert ac.status_code == 200 and ac.json()["workspace_id"] == wid and ac.json()["role"] == "viewer", ac.text
                assert httpx.get(f"{B}/invite/{token}", headers={"X-Forwarded-For": "10.12.66.8"}).status_code == 404   # single-use link is now dead
            cookie = re.search(r"aos_s=([^;]+)", " ".join(ac.headers.get_list("set-cookie")))
            me = httpx.get(f"{B}/api/auth/me", headers={"Cookie": f"aos_s={cookie.group(1)}"}).json(); assert me["ws"] == wid and me["role"] == "viewer"
        finally:
            with psycopg.connect(PG, autocommit=True) as c:
                if wid:
                    for sql in ("DELETE FROM agents WHERE workspace_id=%s", "DELETE FROM ws_invites WHERE workspace_id=%s"):
                        try: c.execute(sql, (wid,))
                        except Exception as e: print("cleanup:", str(e)[:80])   # noqa: BLE001
                c.execute("DELETE FROM users WHERE username = ANY(%s)", (names,))
                if wid: c.execute("DELETE FROM workspaces WHERE id=%s", (wid,))
