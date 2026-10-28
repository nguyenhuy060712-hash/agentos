"""GĐ13 12.30 Issue model v2: statuses open|fixing|watching|completed|ignored, severity, PATCH + history, issue_traces, migration 0200 up/down. Fake LLM, $0.
Server: AOS_TEST_BASE (default the shared :8020, must run the new code). Rows are synthetic, in throwaway workspaces."""
import asyncio, importlib.util, os, sys, uuid
from pathlib import Path
import httpx, psycopg, pytest
from psycopg.types.json import Jsonb

B = os.environ.get("AOS_TEST_BASE", "http://127.0.0.1:8020"); ADMIN = ("moza", "phase0-local")
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
PW = "phase0-gd1330-local"; SUF = uuid.uuid4().hex[:6]
def pg(): return psycopg.connect(PG, autocommit=True)

@pytest.fixture(scope="module")
def env():
    a = httpx.Client(base_url=B, auth=ADMIN, timeout=60)
    e = {"a": a}
    for k in ("w1", "w2"): e[k] = a.post("/api/admin/workspaces", json={"name": f"e30-{k}-{SUF}"}).json()["id"]
    for k, role, ws, demo in (("ed", "editor", e["w1"], False), ("vw", "viewer", e["w1"], False), ("dm", "viewer", e["w1"], True), ("ed2", "editor", e["w2"], False)):
        r = a.post("/api/admin/users", json={"username": f"e30_{k}_{SUF}", "password": PW, "role": role, "workspace_id": ws, "demo": demo}); assert r.status_code == 200, r.text
        e[k] = httpx.Client(base_url=B, auth=(f"e30_{k}_{SUF}", PW), timeout=60)
    with pg() as c:
        e["pid"] = c.execute("INSERT INTO projects(name, workspace_id) VALUES(%s,%s) RETURNING id", (f"e30-{SUF}", e["w1"])).fetchone()[0]
        e["ids"] = []
        for i, (sev, ago) in enumerate(((2, 3), (0, 2), (3, 1))):
            rid = str(uuid.uuid4())
            e["ids"].append(c.execute("""INSERT INTO issues(workspace_id,project_id,kind,signature,title,count,sample_run_ids,severity,last_seen)
                VALUES(%s,%s,'error_cluster',%s,%s,%s,%s,%s, now() - %s * interval '1 hour') RETURNING id""", (e["w1"], e["pid"], f"sig{i}-{SUF}", f"issue {i} {SUF}", i + 1, Jsonb([rid]), sev, ago)).fetchone()[0])
    yield e
    for k in ("ed", "vw", "dm", "ed2", "a"): e[k].close()
    with pg() as c:
        c.execute("DELETE FROM projects WHERE workspace_id IN (%s,%s)", (e["w1"], e["w2"]))
        c.execute("DELETE FROM users WHERE username LIKE %s", (f"e30\_%\_{SUF}",))

def test_list_sorts_by_severity_and_filters(env):
    r = env["ed"].get("/api/engine/issues", params={"sort": "severity"}).json()["items"]
    assert [i["severity"] for i in r] == [0, 2, 3] and all(i["status"] == "open" for i in r)
    assert [i["severity"] for i in env["ed"].get("/api/engine/issues", params={"severity": 3}).json()["items"]] == [3]
    assert env["ed"].get("/api/engine/issues", params={"sort": "x"}).status_code == 422
    assert len(env["ed"].get("/api/engine/issues", params={"status": "new"}).json()["items"]) == 3        # v1 name accepted

def test_patch_writes_history_and_close_fields(env):
    i = env["ids"][0]
    r = env["ed"].patch(f"/api/engine/issues/{i}", json={"severity": 1, "reason": "bigger"}); assert r.status_code == 200 and r.json()["severity"] == 1
    r = env["ed"].patch(f"/api/engine/issues/{i}", json={"status": "completed", "close_reason": "resolved", "close_note": "deployed"}).json()
    assert r["status"] == "completed" and r["close_reason"] == "resolved" and r["closed_at"]
    assert env["ed"].patch(f"/api/engine/issues/{i}", json={"status": "bogus"}).status_code == 422
    r = env["ed"].patch(f"/api/engine/issues/{i}", json={"status": "open"}).json()
    assert r["status"] == "open" and r["close_reason"] is None and r["closed_at"] is None            # reopen clears closed_*
    h = env["ed"].get(f"/api/engine/issues/{i}/history").json()["items"]
    assert len(h) == 3 and h[0]["to_val"] == "open" and h[0]["source"] == "user" and h[0]["actor"] == f"e30_ed_{SUF}" and h[-1]["action"] == "severity"
    assert env["ed"].put(f"/api/engine/issues/{i}", json={"status": "fixed"}).json()["status"] == "completed"   # old PUT body
    assert env["ed"].put(f"/api/engine/issues/{i}", json={"status": "new"}).json()["status"] == "open"
    assert len(env["ed"].get(f"/api/engine/issues/{i}/history").json()["items"]) == 5

def test_permissions_and_isolation(env):
    i = env["ids"][1]
    assert env["vw"].patch(f"/api/engine/issues/{i}", json={"severity": 0}).status_code == 403
    assert env["dm"].patch(f"/api/engine/issues/{i}", json={"severity": 0}).status_code == 403
    assert env["ed2"].patch(f"/api/engine/issues/{i}", json={"severity": 0}).status_code == 404
    assert env["ed2"].get(f"/api/engine/issues/{i}/history").status_code == 404
    assert env["ed2"].get("/api/engine/issues", params={"status": "all"}).json()["items"] == []

def test_upsert_links_traces(env):
    sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
    with pg() as c:
        c.execute("INSERT INTO issue_traces(issue_id, run_id) VALUES(%s,'r1'),(%s,'r1') ON CONFLICT DO NOTHING", (env["ids"][2], env["ids"][2]))
        assert c.execute("SELECT count(*) FROM issue_traces WHERE issue_id=%s", (env["ids"][2],)).fetchone()[0] == 1
    assert "has_analysis" in env["ed"].get(f"/api/engine/issues/{env['ids'][2]}").json()

def test_migration_0200_down_up_roundtrip():
    spec = importlib.util.spec_from_file_location("m0200", Path(__file__).parent.parent / "app" / "migrations" / "0200_issues_v2.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    async def run():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=False) as c:
            async def q(sql, args=()):
                cur = await c.execute(sql, args)
                return await cur.fetchall() if cur.description else None
            ws = (await q("SELECT id FROM workspaces LIMIT 1"))[0][0]
            pid = (await q("INSERT INTO projects(name, workspace_id) VALUES(%s,%s) RETURNING id", (f"e30m-{SUF}", ws)))[0][0]
            for n, st in enumerate(("open", "fixing", "completed", "ignored", "watching")):
                await q("INSERT INTO issues(workspace_id,project_id,kind,signature,title,status) VALUES(%s,%s,'low_score',%s,'t',%s)", (ws, pid, f"s{n}", st))
            await m.down(q)
            got = dict((await q("SELECT signature, status FROM issues WHERE project_id=%s", (pid,))))
            assert got == {"s0": "new", "s1": "in_progress", "s2": "fixed", "s3": "ignored", "s4": "in_progress"}, got
            assert (await q("SELECT to_regclass('issue_events')"))[0][0] is None
            await m.up(q)
            got = dict((await q("SELECT signature, status FROM issues WHERE project_id=%s", (pid,))))
            assert got["s0"] == "open" and got["s1"] == "fixing" and got["s2"] == "completed" and got["s3"] == "ignored"
            assert (await q("SELECT to_regclass('issue_events')"))[0][0] is not None
            await m.up(q)                                                                                   # idempotent
            await c.rollback()
    asyncio.run(run())

# ---- 12.31 Engine UI (reduced): server-rendered page carries the ids and both languages; evidence link data comes from issue_traces
def test_ui_engine_page_has_filters_toolbar_history_both_languages():
    h = httpx.get(B + "/engine", auth=ADMIN, timeout=30).text
    hv = httpx.get(B + "/engine", auth=ADMIN, timeout=30, headers={"Cookie": "aos_lang=vi"}).text      # vi cookie keeps the vi side of T_(en, vi) untouched
    for el in ('id="hd"', "'fp'", "'so'", "id=\"fc\"", 'id="prs"', 'id="cl-inc"', 'id="cl-res"', 'id="ro"', 'id="hl"', "aos_engine_view_", "api/engine/issues/'+id,'PATCH'", "/history", "?issue="):
        assert el in h, el
    for en, vi in (("Reopen", "Mở lại"), ("Incorrectly flagged", "Báo nhầm"), ("History", "Lịch sử"), ("All priorities", "Mọi mức"), ("Sort: Severity", "Xếp: Mức ưu tiên"), ("Urgent", "Khẩn cấp")):
        assert en in h and vi in hv, (en, vi)
    assert "Mark fixed" not in h                                                                       # the old PUT buttons are gone

def test_ui_issue_detail_exposes_trace_links_for_evidence(env):
    with pg() as c: c.execute("INSERT INTO issue_traces(issue_id, run_id, trace_id, span_id) VALUES(%s,'r9','t9','s9') ON CONFLICT DO NOTHING", (env["ids"][1],))
    d = env["ed"].get(f"/api/engine/issues/{env['ids'][1]}").json()
    assert {"run_id": "r9", "trace_id": "t9", "span_id": "s9"} in d["traces"]
    assert env["ed2"].get(f"/api/engine/issues/{env['ids'][1]}").status_code == 404                  # still isolated

def test_ui_trace_page_opens_the_linked_run():
    assert "QS.get('run')" in httpx.get(B + "/projects", auth=ADMIN, timeout=30).text

# ---- 12.32 Engine settings per project: three layers (env, workspace, project) + paused + due
@pytest.fixture(scope="module")
def eng32():
    a = httpx.Client(base_url=B, auth=ADMIN, timeout=60); s = uuid.uuid4().hex[:6]; e = {"a": a, "s": s}
    e["w"] = a.post("/api/admin/workspaces", json={"name": f"e32-w-{s}"}).json()["id"]
    e["w2"] = a.post("/api/admin/workspaces", json={"name": f"e32-x-{s}"}).json()["id"]
    for k, role, ws, demo in (("ad", "admin", e["w"], False), ("ed", "editor", e["w"], False), ("dm", "viewer", e["w"], True), ("ad2", "admin", e["w2"], False)):
        r = a.post("/api/admin/users", json={"username": f"e32_{k}_{s}", "password": PW, "role": role, "workspace_id": ws, "demo": demo}); assert r.status_code == 200, r.text
        e[k] = httpx.Client(base_url=B, auth=(f"e32_{k}_{s}", PW), timeout=60)
    with pg() as c:
        e["p1"], e["p2"] = [c.execute("INSERT INTO projects(name, workspace_id) VALUES(%s,%s) RETURNING id", (f"e32-{i}-{s}", e["w"])).fetchone()[0] for i in (1, 2)]
    yield e
    for k in ("ad", "ed", "dm", "ad2", "a"): e[k].close()
    with pg() as c:
        c.execute("DELETE FROM tr_runs WHERE project_id IN (%s,%s)", (e["p1"], e["p2"]))
        c.execute("DELETE FROM projects WHERE workspace_id IN (%s,%s)", (e["w"], e["w2"]))
        c.execute("DELETE FROM users WHERE username LIKE %s", (f"e32\\_%\\_{s}",))
        c.execute("DELETE FROM workspaces WHERE id IN (%s,%s)", (e["w"], e["w2"]))

def _err(e, pid, msg):
    rid = str(uuid.uuid4())
    with pg() as c: c.execute("INSERT INTO tr_runs(id,trace_id,project_id,name,run_type,start_time,status,error) VALUES(%s,%s,%s,'chat','chain',now() - interval '1 minute','error',%s)", (rid, rid, pid, msg))

def _n_issues(e): return len(e["ad"].get("/api/engine/issues", params={"status": "all"}).json()["items"])
def _usage():
    with pg() as c: return c.execute("SELECT count(*) FROM gateway_usage WHERE feature='engine'").fetchone()[0]

def test_new_workspace_starts_off_and_detector_does_nothing(eng32):
    e = eng32
    assert e["ad"].get("/api/engine/settings").json()["enabled"] is False
    for p in (e["p1"], e["p2"]): _err(e, p, f"boom-{p}-{e['s']}")
    u = _usage(); r = e["ad"].post("/api/engine/detect", params={"analyse": "true"}).json()
    assert r == {"new": [], "reopened": [], "updated": 0, "analysed": [], "notified": 0} and _n_issues(e) == 0 and _usage() == u    # no scan, no model call

def test_workspace_on_project_off_paused_and_resume(eng32):
    e = eng32
    assert e["ed"].put("/api/engine/settings", json={"enabled": True}).status_code == 403                    # admin only
    assert e["ad"].put("/api/engine/settings", json={"enabled": True}).json()["enabled"] is True
    g = e["ed"].get(f"/api/engine/projects/{e['p1']}/settings").json()                                       # editors may read; no row = follows the workspace
    assert g["enabled"] is True and g["configured"] is False and g["active"] is True and g["analysis_level"] == "reduced" and g["scan_every_min"] == 60
    assert g["estimate"]["standard"]["per_analysis_usd"] > g["estimate"]["reduced"]["per_analysis_usd"] and g["cap_tz"] == "Asia/Ho_Chi_Minh"
    assert e["ad"].put(f"/api/engine/projects/{e['p2']}/settings", json={"enabled": False}).json()["active"] is False
    r = e["ad"].post("/api/engine/detect", params={"analyse": "false"}).json(); assert len(r["new"]) == 1       # only project 1
    issue = e["ad"].get("/api/engine/issues", params={"status": "all"}).json()["items"][0]
    assert e["ad"].post(f"/api/engine/projects/{e['p1']}/pause").json()["paused"] is True
    _err(e, e["p1"], f"another-{e['s']}")
    assert e["ad"].post("/api/engine/detect", params={"analyse": "false"}).json()["new"] == []              # paused: nothing new ...
    assert e["ad"].get(f"/api/engine/issues/{issue['id']}").status_code == 200 and _n_issues(e) == 1            # ... but issues stay readable
    assert e["ad"].post(f"/api/engine/projects/{e['p1']}/resume").json()["paused"] is False
    assert len(e["ad"].post("/api/engine/detect", params={"analyse": "false"}).json()["new"]) == 1
    assert e["ad"].put("/api/engine/settings", json={"enabled": False}).json()["enabled"] is False            # workspace off -> nothing even for an enabled project
    _err(e, e["p1"], f"third-{e['s']}")
    assert e["ad"].post("/api/engine/detect").json()["new"] == [] and _n_issues(e) == 2
    assert e["ad"].put("/api/engine/settings", json={"score_min": 0.7}).json()["score_min"] == 0.7           # old body keeps working

def test_scan_interval_makes_a_project_not_due(eng32):
    import datetime as dt
    e = eng32; e["ad"].put("/api/engine/settings", json={"enabled": True})
    r = e["ad"].put(f"/api/engine/projects/{e['p1']}/settings", json={"scan_every_min": 120, "analysis_level": "standard", "preferences": "login errors matter most"}).json()
    assert r["scan_every_min"] == 120 and r["analysis_level"] == "standard" and r["preferences"].startswith("login")
    sys.path.insert(0, str(Path(__file__).parent.parent / "app")); import engine
    now = dt.datetime.now(dt.timezone.utc)
    assert engine._due({"last_scan": now, "scan_every_min": 120}, False) is False and engine._due({"last_scan": now, "scan_every_min": 120}, True) is True
    assert engine._due({"last_scan": now - dt.timedelta(minutes=121), "scan_every_min": 120}, False) is True and engine._due({"last_scan": None, "scan_every_min": 120}, False) is True
    assert e["ad"].post("/api/engine/detect", params={"analyse": "false"}).status_code == 200              # a manual scan ignores the interval and records last_scan
    assert e["ad"].get(f"/api/engine/projects/{e['p1']}/settings").json()["last_scan"]

def test_settings_validation_permissions_isolation_demo_and_ui(eng32):
    e = eng32; u = f"/api/engine/projects/{e['p1']}/settings"
    assert e["ad"].put(u, json={"scan_every_min": 5}).status_code == 422 and e["ad"].put(u, json={"analysis_level": "huge"}).status_code == 422
    assert e["ed"].put(u, json={"enabled": False}).status_code == 403 and e["ed"].post(f"/api/engine/projects/{e['p1']}/pause").status_code == 403
    assert e["dm"].put(u, json={"enabled": True}).status_code == 403 and e["dm"].post(f"/api/engine/projects/{e['p1']}/pause").status_code == 403
    assert e["ad2"].get(u).status_code == 404 and e["ad2"].put(u, json={"enabled": False}).status_code == 404       # workspace B cannot see or touch A's project
    assert e["ad"].get("/api/engine/projects/99999999/settings").status_code == 404
    with pg() as c: c.execute("UPDATE workspaces SET demo=true WHERE id=%s", (e["w2"],))
    try: assert e["ad2"].put("/api/engine/settings", json={"enabled": True}).status_code in (403, 404) or e["ad2"].get("/api/engine/settings").json()["enabled"] is False
    finally:
        with pg() as c: c.execute("UPDATE workspaces SET demo=false WHERE id=%s", (e["w2"],))
    h = httpx.get(B + "/engine", auth=ADMIN, timeout=30).text
    hv = httpx.get(B + "/engine", auth=ADMIN, timeout=30, headers={"Cookie": "aos_lang=vi"}).text
    assert 'id="psp"' in h and "ps-sv" in h and "Engine settings" in h and "Cài đặt Engine" in hv and "ICT" in h

def test_migration_0201_roundtrip_and_new_workspace_default():
    spec = importlib.util.spec_from_file_location("m0201", Path(__file__).parent.parent / "app" / "migrations" / "0201_engine_settings.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    async def run():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=False) as c:
            async def q(sql, args=()):
                cur = await c.execute(sql, args)
                return await cur.fetchall() if cur.description else None
            await m.down(q)
            assert (await q("SELECT to_regclass('engine_project_settings')"))[0][0] is None
            assert not await q("SELECT 1 FROM information_schema.columns WHERE table_name='workspaces' AND column_name='engine_enabled'")
            await m.up(q)
            assert all(r[0] for r in await q("SELECT engine_enabled FROM workspaces"))                       # existing workspaces keep the Engine on
            nid = (await q("INSERT INTO workspaces(name) VALUES(%s) RETURNING id", (f"e32m-{SUF}",)))[0][0]
            assert (await q("SELECT engine_enabled FROM workspaces WHERE id=%s", (nid,)))[0][0] is False    # a new one starts off
            await m.up(q)                                                                                   # idempotent
            await c.rollback()
    asyncio.run(run())

# ---- 12.39 Engine cost ledger and per-project cap
def _seed_issue(c, ws, pid, tag):
    rid = str(uuid.uuid4())
    c.execute("INSERT INTO tr_runs(id,trace_id,project_id,name,run_type,start_time,status,error,inputs) VALUES(%s,%s,%s,'chat','chain',now(),'error',%s,%s)", (rid, rid, pid, f"boom {tag}", Jsonb({"message": "hi"})))
    return c.execute("""INSERT INTO issues(workspace_id,project_id,kind,signature,title,count,sample_run_ids) VALUES(%s,%s,'error_cluster',%s,%s,3,%s) RETURNING id""",
                     (ws, pid, f"sig-{tag}", f"issue {tag}", Jsonb([rid]))).fetchone()[0]

@pytest.fixture(scope="module")
def eng39():
    a = httpx.Client(base_url=B, auth=ADMIN, timeout=90); s = uuid.uuid4().hex[:6]; e = {"a": a, "s": s}
    e["w"] = a.post("/api/admin/workspaces", json={"name": f"e39-w-{s}"}).json()["id"]
    e["w2"] = a.post("/api/admin/workspaces", json={"name": f"e39-x-{s}"}).json()["id"]
    for k, role, ws in (("ad", "admin", e["w"]), ("ed", "editor", e["w"]), ("ad2", "admin", e["w2"])):
        r = a.post("/api/admin/users", json={"username": f"e39_{k}_{s}", "password": PW, "role": role, "workspace_id": ws}); assert r.status_code == 200, r.text
        e[k] = httpx.Client(base_url=B, auth=(f"e39_{k}_{s}", PW), timeout=90)
    with pg() as c:
        c.execute("UPDATE workspaces SET engine_enabled=true WHERE id=%s", (e["w"],))
        e["pA"], e["pB"] = [c.execute("INSERT INTO projects(name, workspace_id) VALUES(%s,%s) RETURNING id", (f"e39-{i}-{s}", e["w"])).fetchone()[0] for i in "AB"]
        e["iA"], e["iB"] = [_seed_issue(c, e["w"], p, f"{i}-{s}") for i, p in (("A", e["pA"]), ("B", e["pB"]))]
    yield e
    for k in ("ad", "ed", "ad2", "a"): e[k].close()
    with pg() as c:
        c.execute("DELETE FROM tr_runs WHERE project_id IN (%s,%s)", (e["pA"], e["pB"]))
        c.execute("DELETE FROM projects WHERE workspace_id IN (%s,%s)", (e["w"], e["w2"]))
        c.execute("DELETE FROM users WHERE username LIKE %s", (f"e39\\_%\\_{s}",))
        c.execute("DELETE FROM workspaces WHERE id IN (%s,%s)", (e["w"], e["w2"]))

def _ledger(pid):
    with pg() as c: return float(c.execute("SELECT coalesce(sum(cost_usd),0) FROM engine_runs WHERE project_id=%s", (pid,)).fetchone()[0])

def test_analysis_writes_ledger_row_equal_to_gateway_usage(eng39):
    e = eng39
    with pg() as c: n0 = c.execute("SELECT count(*), coalesce(sum(cost_usd),0) FROM gateway_usage WHERE feature='engine' AND workspace_id=%s", (e["w"],)).fetchone()
    r = e["ad"].post(f"/api/engine/issues/{e['iA']}/analyse"); assert r.status_code == 200, r.text
    with pg() as c:
        rows = c.execute("SELECT kind, status, cost_usd, issue_id, finished_at, workspace_id FROM engine_runs WHERE project_id=%s", (e["pA"],)).fetchall()
        n1 = c.execute("SELECT count(*), coalesce(sum(cost_usd),0) FROM gateway_usage WHERE feature='engine' AND workspace_id=%s", (e["w"],)).fetchone()
    assert len(rows) == 1 and rows[0][:2] == ("analyse", "ok") and rows[0][3] == e["iA"] and rows[0][4] and rows[0][5] == e["w"]
    assert n1[0] > n0[0] and abs(float(rows[0][2]) - float(n1[1] - n0[1])) < 1e-6          # ledger == gateway_usage of feature engine
    u = e["ad"].get("/api/engine/usage", params={"by": "project"}).json()
    assert u["items"][0]["project_id"] == e["pA"] and u["items"][0]["runs"] == 1 and abs(u["total_usd"] - float(rows[0][2])) < 1e-6
    ru = e["ad"].get("/api/engine/usage", params={"by": "run", "from": "2020-01-01", "to": "2099-01-01"}).json()
    assert ru["items"][0]["kind"] == "analyse" and ru["items"][0]["project"].startswith("e39-A")
    assert e["ed"].get("/api/engine/usage").status_code == 403 and e["ad"].get("/api/engine/usage", params={"from": "x"}).status_code == 422
    assert e["ad2"].get("/api/engine/usage").json()["items"] == []                           # other workspace sees nothing

def test_cap_is_per_project_blocks_with_402_and_no_model_call(eng39):
    e = eng39; u = lambda p: f"/api/engine/projects/{p}/settings"
    assert e["ed"].put(u(e["pA"]), json={"cap_usd_month": 1}).status_code == 403
    assert e["ad"].put(u(e["pA"]), json={"cap_usd_month": -1}).status_code == 422
    r = e["ad"].put(u(e["pA"]), json={"cap_usd_month": 0.0001}).json()
    assert r["cap_usd_month"] == 0.0001 and r["month_usd"] == pytest.approx(_ledger(e["pA"]), abs=1e-6)
    assert e["ad"].put(u(e["pA"]), json={"enabled": True}).json()["cap_usd_month"] == 0.0001   # other fields keep the cap
    with pg() as c: n0 = c.execute("SELECT count(*) FROM gateway_usage WHERE feature='engine'").fetchone()[0]; r0 = c.execute("SELECT count(*) FROM engine_runs").fetchone()[0]
    r = e["ad"].post(f"/api/engine/issues/{e['iA']}/analyse")
    assert r.status_code == 402 and "hạn mức tháng" in r.text
    with pg() as c: assert c.execute("SELECT count(*) FROM gateway_usage WHERE feature='engine'").fetchone()[0] == n0 and c.execute("SELECT count(*) FROM engine_runs").fetchone()[0] == r0
    assert e["ad"].post(f"/api/engine/issues/{e['iB']}/analyse").status_code == 200           # project B has no cap: unaffected
    assert e["ad"].put(u(e["pA"]), json={"cap_usd_month": None}).json()["cap_usd_month"] is None   # explicit null clears the cap
    assert e["ad"].post(f"/api/engine/issues/{e['iA']}/analyse").status_code == 200

def test_month_rolls_over_at_ict_midnight_and_failed_step_is_stored(eng39):
    import datetime as dt
    sys.path.insert(0, str(Path(__file__).parent.parent / "app")); import engine
    t = dt.datetime(2026, 10, 31, 17, 0, tzinfo=dt.timezone.utc)                              # 00:00 on 1 Nov in Vietnam
    assert engine.month_start(t) == dt.datetime(2026, 11, 1, tzinfo=engine.ICT) and engine.month_start(t - dt.timedelta(seconds=1)) == dt.datetime(2026, 10, 1, tzinfo=engine.ICT)
    e = eng39; before = _ledger(e["pB"])
    with pg() as c: c.execute("INSERT INTO engine_runs(workspace_id,project_id,kind,status,cost_usd,started_at) VALUES(%s,%s,'scan','ok',5,%s)", (e["w"], e["pB"], engine.month_start() - dt.timedelta(minutes=1)))
    assert e["ad"].get(f"/api/engine/projects/{e['pB']}/settings").json()["month_usd"] == pytest.approx(before, abs=1e-6)   # last month's 5 USD does not count

def test_engine_run_helper_closes_ok_and_stores_error_then_reraises(eng39):
    sys.path.insert(0, str(Path(__file__).parent.parent / "app")); import engine
    e = eng39
    class FakeA:
        @staticmethod
        async def q(sql, args=(), one=False):
            with pg() as c:
                cur = c.execute(sql, args)
                if not cur.description: return None
                cols = [d.name for d in cur.description]; rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                return (rows[0] if rows else None) if one else rows
    real = engine.A; engine.A = FakeA
    async def go():
        async with engine.engine_run(e["w"], e["pB"], "validate", e["iB"]) as run: run.acc.extend([0.1, 0.15]); ok = run.id
        try:
            async with engine.engine_run(e["w"], e["pB"], "verify", None) as run: run.acc.append(0.25); bad = run.id; raise RuntimeError("model down")
        except RuntimeError: pass
        return ok, bad
    try: ok, bad = asyncio.run(go())
    finally: engine.A = real
    with pg() as c:
        r1 = c.execute("SELECT status, cost_usd, error, finished_at FROM engine_runs WHERE id=%s", (ok,)).fetchone()
        r2 = c.execute("SELECT status, cost_usd, error, finished_at FROM engine_runs WHERE id=%s", (bad,)).fetchone()
    assert r1[0] == "ok" and float(r1[1]) == 0.25 and r1[2] is None and r1[3]
    assert r2[0] == "error" and float(r2[1]) == 0.25 and r2[2] == "model down" and r2[3]

def test_migration_0202_roundtrip():
    spec = importlib.util.spec_from_file_location("m0202", Path(__file__).parent.parent / "app" / "migrations" / "0202_engine_runs.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    async def run():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=False) as c:
            async def q(sql, args=()):
                cur = await c.execute(sql, args)
                return await cur.fetchall() if cur.description else None
            await m.down(q)
            assert (await q("SELECT to_regclass('engine_runs')"))[0][0] is None
            assert not await q("SELECT 1 FROM information_schema.columns WHERE table_name='engine_project_settings' AND column_name='cap_usd_month'")
            await m.up(q); await m.up(q)                                                      # idempotent
            assert (await q("SELECT to_regclass('engine_runs')"))[0][0] is not None
            await c.rollback()
    asyncio.run(run())

def test_ui_shows_cap_and_month_to_date_both_languages():
    h = httpx.get(B + "/engine", auth=ADMIN, timeout=30).text; hv = httpx.get(B + "/engine", auth=ADMIN, timeout=30, headers={"Cookie": "aos_lang=vi"}).text
    assert "ps-cap" in h and "Monthly cap" in h and "Spent this month" in h and "Hạn mức tháng" in hv and "Đã dùng tháng này" in hv

# ---- 12.34 Analysis v2 (reduced): category / severity / claims / test questions / span ids; fake LLM $0 (+ replay of the one recorded Gemini analysis)
import json
REC34 = Path(__file__).parent / "data" / "gd13_12_34_analysis.json"

def _seed34(c, ws, pid, tag, n=3, sev=1):
    """An issue with n sample traces; every root run has one failing tool child (its id is the span the analysis may cite)."""
    ids, spans = [], []
    for i in range(n):
        rid, sid = str(uuid.uuid4()), str(uuid.uuid4())
        c.execute("INSERT INTO tr_runs(id,trace_id,project_id,name,run_type,start_time,status,error,inputs) VALUES(%s,%s,%s,'chat','chain',now() - %s * interval '1 minute','error',%s,%s)", (rid, rid, pid, i, f"boom {tag} {i}", Jsonb({"message": f"question {i}"})))
        c.execute("INSERT INTO tr_runs(id,trace_id,parent_id,project_id,name,run_type,start_time,status,error) VALUES(%s,%s,%s,%s,'search_kb','tool',now(),'error',%s)", (sid, rid, rid, pid, f"tool failed {tag} {i}"))
        ids.append(rid); spans.append(sid)
    iid = c.execute("INSERT INTO issues(workspace_id,project_id,kind,signature,title,count,sample_run_ids,severity,category) VALUES(%s,%s,'error_cluster',%s,%s,%s,%s,%s,'error') RETURNING id",
                    (ws, pid, f"sig34-{tag}", f"issue34 {tag}", n, Jsonb(ids), sev)).fetchone()[0]
    c.execute("INSERT INTO issue_traces(issue_id, run_id, trace_id) SELECT %s, s, s FROM unnest(%s::text[]) s", (iid, ids))
    return iid, ids, spans

@pytest.fixture(scope="module")
def eng34():
    a = httpx.Client(base_url=B, auth=ADMIN, timeout=90); s = uuid.uuid4().hex[:6]; e = {"a": a, "s": s}
    e["w"] = a.post("/api/admin/workspaces", json={"name": f"e34-w-{s}"}).json()["id"]
    r = a.post("/api/admin/users", json={"username": f"e34_ad_{s}", "password": PW, "role": "admin", "workspace_id": e["w"]}); assert r.status_code == 200, r.text
    e["ad"] = httpx.Client(base_url=B, auth=(f"e34_ad_{s}", PW), timeout=90)
    with pg() as c:
        c.execute("UPDATE workspaces SET engine_enabled=true WHERE id=%s", (e["w"],))
        e["p"] = c.execute("INSERT INTO projects(name, workspace_id) VALUES(%s,%s) RETURNING id", (f"e34-{s}", e["w"])).fetchone()[0]
        e["i1"], e["ids1"], e["spans1"] = _seed34(c, e["w"], e["p"], f"a-{s}")
        e["i2"], e["ids2"], e["spans2"] = _seed34(c, e["w"], e["p"], f"b-{s}", sev=1)
    yield e
    e["ad"].close(); a.close()
    with pg() as c:
        c.execute("DELETE FROM tr_runs WHERE project_id=%s", (e["p"],))
        c.execute("DELETE FROM projects WHERE workspace_id=%s", (e["w"],))
        c.execute("DELETE FROM users WHERE username LIKE %s", (f"e34\\_%\\_{e['s']}",))
        c.execute("DELETE FROM workspaces WHERE id=%s", (e["w"],))

def test_analysis_v2_fields_are_validated_and_synced(eng34):
    e = eng34
    r = e["ad"].post(f"/api/engine/issues/{e['i1']}/analyse"); assert r.status_code == 200, r.text
    d = r.json(); an = d["analysis"]
    assert an["samples_used"] == 1 and an["level"] == "reduced"                                       # level reduced reads ONE sample
    first = e["ids1"][0]; ev = an["evidence"]
    assert len(ev) == 1 and ev[0]["run_id"] == first and ev[0]["span_id"] == e["spans1"][0]          # the invented run id was dropped
    vd = {c["statement"]: c["verdict"] for c in an["claims"]}
    assert vd == {"Agent nói đã hoàn tất": "true", "Agent bịa mã đơn hàng": "false", "Agent nói đã gửi email": "no_evidence"}   # hallucinated claim = false; a `true` without a real quote is downgraded
    assert [n["run_id"] for n in an["sample_notes"]] == [first] and 2 <= len(an["sample_notes"][0]["test_questions"]) <= 5 and an["sample_notes"][0]["expected_output"]
    assert an["severity_rule"] == 1 and an["severity"] == 2 and d["severity"] == 2                    # the model asked for 3 (two steps): clamped to one step
    assert d["category"] == "low_quality" and d["description"] == "Giả lập: mô tả vấn đề."
    tr = {t["run_id"]: t["span_id"] for t in d["traces"]}
    assert tr[first] == e["spans1"][0] and tr[e["ids1"][1]] is None                                    # issue_traces.span_id filled from the evidence
    with pg() as c:
        assert c.execute("SELECT count(*) FROM engine_runs WHERE issue_id=%s AND kind='analyse' AND status='ok'", (e["i1"],)).fetchone()[0] == 1
        assert c.execute("SELECT count(*) FROM issue_events WHERE issue_id=%s AND action='severity' AND source='system'", (e["i1"],)).fetchone()[0] == 1
    r2 = e["ad"].post(f"/api/engine/issues/{e['i1']}/analyse").json()["analysis"]                      # analysing again does not drift the severity
    assert r2["severity_rule"] == 1 and r2["severity"] == 2

def test_level_standard_reads_up_to_five_samples_and_preferences_reach_the_prompt(eng34):
    e = eng34; sys.path.insert(0, str(Path(__file__).parent.parent / "app")); import engine
    assert e["ad"].put(f"/api/engine/projects/{e['p']}/settings", json={"analysis_level": "standard", "preferences": "refunds matter most"}).status_code == 200
    an = e["ad"].post(f"/api/engine/issues/{e['i2']}/analyse").json()["analysis"]
    assert an["samples_used"] == 3 and an["level"] == "standard" and len(an["sample_notes"]) == 3
    sp = engine.system_prompt("en", "tool_error", "refunds matter most")
    assert "refunds matter most" in sp and "tool_error" in sp and "at most one step" in sp and "refunds" not in engine.system_prompt("en")

def test_invented_span_id_becomes_null():
    sys.path.insert(0, str(Path(__file__).parent.parent / "app")); import engine
    smp = [{"run_id": "r1", "input": "hi", "output": "", "error": "boom", "steps": [{"span_id": "s1", "output": "x"}], "failing_spans": []}]
    res = engine.EngineAnalysis(root_cause="c", evidence=[engine.Evidence(run_id="r1", span_id="s1", quote="boom"), engine.Evidence(run_id="r1", span_id="nope", quote="boom")],
                                proposal=engine.Proposal(type="tool", target="t", rationale="r"))
    assert [x["span_id"] for x in engine.build_analysis(res, smp, {"severity": 2, "category": "error"})["evidence"]] == ["s1", None]

def test_clamp_severity_never_jumps_two_steps():
    sys.path.insert(0, str(Path(__file__).parent.parent / "app")); import engine
    assert [engine.clamp_sev(2, x) for x in (0, 1, 2, 3, None)] == [1, 1, 2, 3, 2] and engine.clamp_sev(0, 3) == 1 and engine.clamp_sev(3, 0) == 2 and engine.clamp_sev(1, 3) == 2

def test_old_analysis_without_new_fields_still_renders(eng34):
    e = eng34
    old = {"root_cause": "old", "evidence": [{"run_id": e["ids2"][0], "quote": "q"}], "proposal": {"type": "config", "target": "t", "rationale": "r", "applicable": False}, "samples_used": 1}
    with pg() as c: c.execute("UPDATE issues SET analysis=%s WHERE id=%s", (Jsonb(old), e["i2"]))
    d = e["ad"].get(f"/api/engine/issues/{e['i2']}").json()
    assert d["analysis"]["root_cause"] == "old" and d["analysis"]["claims"] == [] and d["analysis"]["sample_notes"] == [] and d["analysis"]["evidence"][0]["hidden"] is False
    page = httpx.get(B + "/engine", auth=ADMIN, timeout=30).text; pv = httpx.get(B + "/engine", auth=ADMIN, timeout=30, headers={"Cookie": "aos_lang=vi"}).text
    assert "Suggested test questions" in page and "Câu hỏi kiểm tra đề xuất" in pv and "an.claims||[]" in page and "an.sample_notes||[]" in page

def test_replay_recorded_gemini_analysis():
    """The recorded real run (Gemini, 3 fixture issues): the structured answers parse with our schema and survive `build_analysis`; cost within the card's cap."""
    sys.path.insert(0, str(Path(__file__).parent.parent / "app")); import engine
    with open(REC34, encoding="utf-8") as f: rec = json.load(f)
    assert rec["total_cost_usd"] <= 0.20 and len(rec["issues"]) == 3
    for it in rec["issues"]:
        res = engine.EngineAnalysis.model_validate(it["parsed"]); v = engine.build_analysis(res, it["samples"], {"severity": it["severity"], "category": it["category"]})
        assert res.root_cause and v["evidence"] and abs(v["severity"] - it["severity"]) <= 1 and res.proposal.type in ("prompt", "config", "tool")
        sid = {s["span_id"] for x in it["samples"] for s in x["steps"] + x["failing_spans"]}
        assert all(x["span_id"] is None or x["span_id"] in sid for x in v["evidence"])        # only real step ids survive
        assert all(c["verdict"] in ("true", "false", "no_evidence") for c in v["claims"]) and all(1 <= len(n["test_questions"]) <= 5 for n in v["sample_notes"])
    assert any(c["verdict"] == "false" for it in rec["issues"] for c in it["parsed"]["claims"])   # the fixtures contain invented statements the model caught

@pytest.mark.skipif(not os.environ.get("AOS_GD13_34_RECORD"), reason="real Gemini recording (hard stop $0.20); AOS_GD13_34_RECORD=<path of the 600 key file>")
def test_record_real_gemini_analysis():
    with open(os.path.expanduser(os.environ["AOS_GD13_34_RECORD"])) as f: os.environ["GOOGLE_API_KEY"] = f.read().strip()      # key stays in this process, never printed
    os.environ.pop("OPENAI_API_KEY", None)
    sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
    import deepagent_api, admin, engine, nlsearch               # no fake OPENAI key before the import: the default model would become an OpenAI one
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool
    CAP = 0.20; tot = [0.0]; calls = []; orig = nlsearch._ask; s = uuid.uuid4().hex[:6]
    async def rec(msgs, schema=None, feature=None, acc=None):
        if tot[0] >= CAP: raise RuntimeError(f"hard stop: ${tot[0]:.4f} >= ${CAP}")
        loc: list = []; r, er = await orig(msgs, schema=schema, feature=feature, acc=loc); tot[0] += sum(loc)
        if acc is not None: acc.extend(loc)
        calls.append({"schema": schema.__name__, "cost_usd": sum(loc), "payload_chars": sum(len(m) for m in msgs), "parsed": r.model_dump() if r else None, "error": er})
        return r, er
    nlsearch._ask = rec
    FX = [("error", 1, "Hỏi về đơn hàng DH-1042", "Đơn DH-1042 đã được giao hôm qua.", "kb_lookup timeout after 30s", "Bạn là trợ lý chăm sóc khách hàng. Trả lời ngắn gọn bằng tiếng Việt."),
          ("low_quality", 2, "Khách hỏi chính sách đổi trả 30 ngày", "Bạn có thể đổi trả trong vòng 90 ngày, miễn phí.", None, "Bạn là trợ lý bán hàng. Luôn lịch sự."),
          ("truncated_tool_result", 2, "Liệt kê 10 sản phẩm bán chạy", "Đây là các sản phẩm bán chạy: (danh sách bị cắt)", "result … truncated to 1000 chars", "Bạn là trợ lý catalog. Dùng công cụ list_products.")]
    async def go():
        async with AsyncConnectionPool(PG, min_size=1, max_size=4, open=False, kwargs={"autocommit": True, "row_factory": dict_row}) as pool:
            await pool.open(); A = deepagent_api; A.S["pool"] = pool; A.S["rls"] = admin.rls_ctx
            ws = (await A.q("INSERT INTO workspaces(name) VALUES(%s) RETURNING id", (f"e34-rec-{s}",), one=True))["id"]
            pid = (await A.q("INSERT INTO projects(name, workspace_id) VALUES(%s,%s) RETURNING id", (f"e34-rec-{s}", ws), one=True))["id"]
            out = []
            try:
                user = {"id": 987134, "username": "t1234", "role": "admin", "ws": ws, "auth": "session"}; tk = admin.USER.set(user)
                try:
                    for k, (cat, sev, q, a, err, prompt) in enumerate(FX):
                        ag = (await A.q("INSERT INTO agents(name) VALUES(%s) RETURNING id", (f"e34-rec-{k}-{s}",), one=True))["id"]
                        await A.q("INSERT INTO agent_versions(agent_id, version, config) VALUES(%s,1,%s)", (ag, Jsonb({"system_prompt": prompt, "tools": ["kb_lookup"]})))
                        rid = str(uuid.uuid4()); sid = str(uuid.uuid4())
                        await A.q("INSERT INTO tr_runs(id,trace_id,project_id,name,run_type,start_time,status,error,inputs,outputs,metadata) VALUES(%s,%s,%s,'chat','chain',now(),%s,%s,%s,%s,%s)",
                                  (rid, rid, pid, "error" if err and "timeout" in err else "success", None, Jsonb({"message": q}), Jsonb({"output": a}), Jsonb({"agent_id": str(ag)})))
                        await A.q("INSERT INTO tr_runs(id,trace_id,parent_id,project_id,name,run_type,start_time,status,error,outputs) VALUES(%s,%s,%s,%s,'kb_lookup','tool',now(),%s,%s,%s)",
                                  (sid, rid, rid, pid, "error" if err else "success", err, Jsonb({"result": "chính sách đổi trả: 30 ngày kể từ ngày nhận hàng"}) if not err else None))
                        iid = (await A.q("INSERT INTO issues(workspace_id,project_id,kind,signature,title,count,sample_run_ids,severity,category,agent_id) VALUES(%s,%s,'error_cluster',%s,%s,3,%s,%s,%s,%s) RETURNING id",
                                         (ws, pid, f"sig-{k}-{s}", f"{cat}: {q}"[:140], Jsonb([rid]), sev, cat, ag), one=True))["id"]
                        with engine.as_ws(ws):
                            n0 = len(calls); samples = await engine.digest({}, [rid], 1); an = await engine.analyse_issue(iid, ws, "vi")
                        out.append({"issue_id": iid, "category": cat, "severity": sev, "samples": samples, "parsed": calls[n0]["parsed"], "cost_usd": calls[n0]["cost_usd"], "stored": an})
                finally: admin.USER.reset(tk)
                return out
            finally:
                await A.q("DELETE FROM tr_runs WHERE project_id=%s", (pid,))
                await A.q("DELETE FROM engine_runs WHERE workspace_id=%s", (ws,)); await A.q("DELETE FROM projects WHERE workspace_id=%s", (ws,)); await A.q("DELETE FROM workspaces WHERE id=%s", (ws,))
                await A.q("DELETE FROM agents WHERE name LIKE %s", (f"e34-rec-%-{s}",))
    try: out = asyncio.run(go())
    finally: nlsearch._ask = orig
    with open(REC34, "w", encoding="utf-8") as f:
        json.dump({"recorded": "2026-10-26", "model": deepagent_api.DEFAULT_MODEL, "total_cost_usd": round(tot[0], 6), "issues": out}, f, ensure_ascii=False, indent=1, default=str)
    print(f"RECORDED cost=${tot[0]:.4f} calls={len(calls)}")

# ---- 12.37 Issue to test examples (reduced): edited input / expected output + test questions -> dataset examples + Assertions judge attached once; fake LLM $0
@pytest.fixture(scope="module")
def eng37():
    a = httpx.Client(base_url=B, auth=ADMIN, timeout=60); s = uuid.uuid4().hex[:6]; e = {"a": a, "s": s}
    e["w"] = a.post("/api/admin/workspaces", json={"name": f"e37-w-{s}"}).json()["id"]
    for k, role in (("ed", "editor"), ("vw", "viewer")):
        r = a.post("/api/admin/users", json={"username": f"e37_{k}_{s}", "password": PW, "role": role, "workspace_id": e["w"]}); assert r.status_code == 200, r.text
        e[k] = httpx.Client(base_url=B, auth=(f"e37_{k}_{s}", PW), timeout=60)
    with pg() as c:
        e["p"] = c.execute("INSERT INTO projects(name, workspace_id) VALUES(%s,%s) RETURNING id", (f"e37-{s}", e["w"])).fetchone()[0]
        e["i"], e["ids"], _ = _seed34(c, e["w"], e["p"], f"x-{s}")
        rid = str(uuid.uuid4())
        c.execute("INSERT INTO tr_runs(id,trace_id,project_id,name,run_type,start_time,status,inputs) VALUES(%s,%s,%s,'chat','chain',now(),'ok',%s)", (rid, rid, e["p"], Jsonb({"message": "unlinked"})))
        e["other"] = rid
    yield e
    for k in ("ed", "vw", "a"): e[k].close()
    with pg() as c:
        c.execute("DELETE FROM tr_runs WHERE project_id=%s", (e["p"],))
        c.execute("DELETE FROM projects WHERE workspace_id=%s", (e["w"],))
        c.execute("DELETE FROM datasets WHERE workspace_id=%s", (e["w"],))
        c.execute("DELETE FROM users WHERE username LIKE %s", (f"e37\_%\_{e['s']}",))
        c.execute("DELETE FROM workspaces WHERE id=%s", (e["w"],))

def test_examples_saved_with_edits_and_judge_once(eng37):
    e = eng37; url = f"/api/engine/issues/{e['i']}/examples"
    body = {"items": [{"run_id": e["ids"][0], "input": "edited question", "expected_output": "the right answer", "questions": ["Does it cite the order id?", "Is it polite?"]}]}
    r = e["ed"].post(url, json=body); assert r.status_code == 200, r.text
    j = r.json(); assert j["added"] == 1 and j["judge_id"] and j["name"] == f"engine-issue-{e['i']}"
    with pg() as c:
        row = c.execute("""SELECT r.inputs, r.outputs FROM dataset_examples x JOIN example_revisions r ON r.example_id=x.id WHERE x.dataset_id=%s AND x.source_run_id=%s""", (j["dataset_id"], e["ids"][0])).fetchone()
        assert json.dumps(row[0]).count("edited question") == 1 and row[1]["answer"] == "the right answer"
        assert [x["comment"] for x in row[1]["assertions"]] == ["Does it cite the order id?", "Is it polite?"]
        assert c.execute("SELECT evaluator_ids FROM datasets WHERE id=%s", (j["dataset_id"],)).fetchone()[0] == [j["judge_id"]]
    body["items"].append({"run_id": e["ids"][1], "questions": ["Is it short?"]})
    j2 = e["ed"].post(url, json=body).json(); assert j2["results"][0] == "đã có" and j2["added"] == 1 and j2["dataset_id"] == j["dataset_id"]
    with pg() as c:
        assert c.execute("SELECT evaluator_ids FROM datasets WHERE id=%s", (j["dataset_id"],)).fetchone()[0] == [j["judge_id"]]      # attached once
        assert c.execute("SELECT count(*) FROM dataset_examples WHERE dataset_id=%s", (j["dataset_id"],)).fetchone()[0] == 2
    assert e["ed"].get(f"/api/engine/issues/{e['i']}").json()["refs"]["dataset_id"] == j["dataset_id"]

def test_examples_permissions_and_validation(eng37):
    e = eng37; url = f"/api/engine/issues/{e['i']}/examples"; it = {"run_id": e["ids"][2], "questions": ["ok?"]}
    assert e["vw"].post(url, json={"items": [it]}).status_code == 403
    assert e["ed"].post(url, json={"items": [{"run_id": e["other"]}]}).status_code == 404            # not linked to the issue
    assert e["ed"].post(url, json={"items": [{**it, "questions": ["x" * 161]}]}).status_code == 422
    assert e["ed"].post(url, json={"items": [{**it, "questions": ["q"] * 6}]}).status_code == 422
    assert e["ed"].post(url, json={"items": []}).status_code == 422
    assert e["ed"].post(f"/api/engine/issues/999999999/examples", json={"items": [it]}).status_code == 404

def test_ui_has_offline_example_box_both_languages():
    h = httpx.get(B + "/engine", auth=ADMIN, timeout=30).text; hv = httpx.get(B + "/engine", auth=ADMIN, timeout=30, headers={"Cookie": "aos_lang=vi"}).text
    assert "api/engine/issues/${id}/examples" in h and "Add offline example" in h and "Thêm ví dụ kiểm thử" in hv


def test_ui_pages_have_no_raw_newline_in_js_string():
    """13.Z regression: a Python '\\n' inside a non-raw UI string became a real newline in the page JS ("Invalid or unexpected token" broke /engine).
    The UI modules build JS inside normal (non-raw) Python strings, so `.join('\\n')` must be written with a doubled backslash."""
    import re, glob
    rx = re.compile(r"(?:join|split)\('\r?\n'\)")
    bad = []
    for f in glob.glob(str(Path(__file__).parent.parent / "app" / "ui*.py")):
        bad += [Path(f).name for _ in rx.finditer(open(f, encoding="utf-8").read())]
    assert not bad, bad
    assert not rx.search(httpx.get(B + "/engine", auth=ADMIN, timeout=30).text)
