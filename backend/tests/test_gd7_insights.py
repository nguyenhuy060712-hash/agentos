"""GD7.3 (22/10/2026): Insights (app/insights.py): usage topics + weekly report. Local server :8020, fake LLM + fake bag-of-words embeddings (overlay/run_local.py)
=> $0, no network. Synthetic data: 60 chat threads in 3 clear topics (30 / 20 / 10) in a throwaway workspace; second batch with a different mix => the 2nd report shows the change;
weekly job => webhook channel of an alert rule (local receiver); caps => 402; RLS between workspaces; viewers/demo read-only."""
import json, os, random, threading, time, uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
import httpx, psycopg, pytest
from psycopg.types.json import Jsonb
from test_fixes import B, ADMIN, a  # noqa: F401  (fixtures)

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
PW = "phase0-gd73-local"; SUF = uuid.uuid4().hex[:6]
TOPICS = {"A": ["refund", "invoice", "payment", "billing"], "B": ["shipping", "delivery", "parcel", "tracking"], "C": ["password", "login", "account", "locked"]}

def pg(): return psycopg.connect(PG, autocommit=True)

def user(a, name, ws, role="editor", demo=False):
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": role, "workspace_id": ws, "demo": demo})
    assert r.status_code == 200, r.text
    return r.json()["id"], httpx.Client(base_url=B, auth=(name, PW), timeout=90)

@pytest.fixture(scope="module")
def env(a):
    w1 = a.post("/api/admin/workspaces", json={"name": f"ins-w1-{SUF}"}).json()["id"]
    w2 = a.post("/api/admin/workspaces", json={"name": f"ins-w2-{SUF}"}).json()["id"]
    e = {"w1": w1, "w2": w2}
    e["ed_id"], e["ed"] = user(a, f"ins_ed_{SUF}", w1)
    e["vw_id"], e["vw"] = user(a, f"ins_vw_{SUF}", w1, "viewer")
    e["ad_id"], e["ad"] = user(a, f"ins_ad_{SUF}", w1, "admin")
    e["dm_id"], e["dm"] = user(a, f"ins_dm_{SUF}", w1, "viewer", demo=True)
    e["ed2_id"], e["ed2"] = user(a, f"ins_ed2_{SUF}", w2)
    with pg() as c:
        e["pid"] = c.execute("INSERT INTO projects(name, workspace_id) VALUES(%s,%s) RETURNING id", (f"ins-{SUF}", w1)).fetchone()[0]
    e["made"] = []
    yield e
    for k in ("ed", "vw", "ad", "dm", "ed2"): e[k].close()
    with pg() as c:
        c.execute("DELETE FROM tr_runs WHERE project_id=%s", (e["pid"],))
        c.execute("DELETE FROM insight_reports WHERE workspace_id IN (%s,%s)", (w1, w2))
        c.execute("DELETE FROM alert_rules WHERE project_id=%s", (e["pid"],))
        c.execute("DELETE FROM projects WHERE id=%s", (e["pid"],))
        c.execute("DELETE FROM users WHERE username LIKE %s", (f"ins\\_%\\_{SUF}",))
        c.execute("DELETE FROM workspaces WHERE id IN (%s,%s)", (w1, w2))
        c.execute("DELETE FROM gateway_usage WHERE status='t73'")

def add_threads(e, plan, tag):
    """plan: {topic: (n_threads, n_errors, cost_each, feedback scores list)}; topic A threads belong to the editor (private), the rest are project-visible."""
    rnd = random.Random(tag); ids = []
    with pg() as c:
        for t, (n, errs, cost, fb) in plan.items():
            for i in range(n):
                rid = str(uuid.uuid4()); thread = f"ins-{SUF}-{tag}-{t}-{i}"
                text = " ".join(rnd.sample(TOPICS[t], 3)) + f" case{tag}{t}{i}"
                c.execute("""INSERT INTO tr_runs(id,trace_id,project_id,name,run_type,start_time,status,inputs,thread_id,cost_usd,user_id)
                    VALUES(%s,%s,%s,'chat','chain',now() - interval '3 days' + %s * interval '1 minute',%s,%s,%s,%s,%s)""",
                          (rid, rid, e["pid"], len(ids), "error" if i < errs else "success", Jsonb({"message": text}), thread, cost, e["ed_id"] if t == "A" else None))
                if i < len(fb): c.execute("INSERT INTO feedback(run_id,trace_id,key,score) VALUES(%s,%s,'user_score',%s)", (rid, rid, fb[i]))
                ids.append(rid)
    e["made"] += ids

def wipe_threads(e):
    with pg() as c: c.execute("DELETE FROM tr_runs WHERE project_id=%s", (e["pid"],))

def wait_done(c, rid, secs=60):
    t = time.time()
    while time.time() - t < secs:
        r = c.get(f"/api/insights/reports/{rid}").json()
        if r["status"] != "running": return r
        time.sleep(0.5)
    raise AssertionError("report still running")

def run(c, **kw):
    r = c.post("/api/insights/run", json={"days": 7, **kw}); assert r.status_code == 200, r.text
    return wait_done(c, r.json()["id"])

# ---------------------------------------------------------------- pure functions
def test_kmeans_and_text_helpers():
    import sys; sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
    import numpy as np
    import insights as I
    rng = np.random.default_rng(1)
    x = I.unit(np.vstack([rng.normal(m, 0.05, (15, 8)) for m in (np.eye(8)[0], np.eye(8)[3], np.eye(8)[6])]))
    lab, cen, k, sil = I.best_split(x, 3, 6)
    assert k == 3 and sil > 0.5 and sorted(np.bincount(lab)) == [15, 15, 15]
    assert (I.kmeans(x, 3)[0] == I.kmeans(x, 3)[0]).all()                    # deterministic
    assert [len(t["idx"]) for t in I.two_level(x)] == [15, 15, 15]
    assert I.first_text({"message": " hi "}) == "hi" and I.first_text({"messages": [{"type": "ai", "content": "x"}, {"type": "human", "content": [{"type": "text", "text": "q"}]}]}) == "q"
    assert I.first_text('{"question": "why"}') == "why" and I.first_text(None) == ""
    mon = I.dt.datetime(2026, 10, 19, 8, 5, tzinfo=I.ICT)                     # Monday 08:05 ICT
    assert I.due(mon, None) and not I.due(mon, mon) and I.due(mon, mon - I.dt.timedelta(days=7))
    assert not I.due(mon - I.dt.timedelta(hours=1), None) and not I.due(mon + I.dt.timedelta(days=1), None) and not I.due(mon + I.dt.timedelta(hours=13), None)

# ---------------------------------------------------------------- first report
def test_page_and_empty_period(a, env):
    assert a.get("/insights").status_code == 200
    r = env["ed"].post("/api/insights/run", json={"days": 7}); assert r.status_code == 422, r.text   # nothing to analyse yet

@pytest.fixture(scope="module")
def rep1(env):
    add_threads(env, {"A": (30, 6, 0.002, [1.0] * 10 + [0.0] * 5), "B": (20, 0, 0.01, []), "C": (10, 5, 0, [0.5] * 4)}, "p1")
    return run(env["ed"])

def test_three_topics_with_correct_counts_and_metrics(rep1, env):
    r = rep1; assert r["status"] == "done", r.get("summary")
    assert r["n_threads"] == 60 and r["scope"] == "workspace" and r["workspace_id"] == env["w1"]
    cl = r["clusters"]; assert [c["n"] for c in cl] == [30, 20, 10]
    assert abs(sum(c["share"] for c in cl) - 1) < 1e-9 and [round(c["share"], 3) for c in cl] == [0.5, 0.333, 0.167]
    assert [round(c["error_rate"], 3) for c in cl] == [0.2, 0.0, 0.5]
    a_, b_, c_ = cl
    assert abs(a_["avg_score"] - 10 / 15) < 1e-9 and b_["avg_score"] is None and abs(c_["avg_score"] - 0.5) < 1e-9
    assert [round(float(c["cost_usd"]), 4) for c in cl] == [0.06, 0.2, 0.0]
    for c in cl:
        assert c["label"].startswith("Chủ đề:") and "cuộc hội thoại" in c["summary"]       # labels/summaries came from the (fake) LLM
        assert c["examples"] and all(e["text"] and e["thread_id"].startswith(f"ins-{SUF}") for e in c["examples"])
        assert c["prev_share"] is None
        if c["children"]: assert sum(s["n"] for s in c["children"]) == c["n"]
    assert "60 hội thoại, 3 chủ đề" in r["summary"] and "Chưa có báo cáo trước" in r["summary"]
    assert float(r["cost_usd"]) >= 0 and r["meta"]["k_top"] == 3

def test_list_has_top_topics(rep1, env):
    rows = env["ed"].get("/api/insights/reports").json(); r = next(x for x in rows if x["id"] == rep1["id"])
    assert r["status"] == "done" and [t["label"] for t in r["top"]] == [c["label"] for c in rep1["clusters"]]

# ---------------------------------------------------------------- privacy / roles
def test_viewer_sees_aggregate_but_not_private_examples(rep1, env):
    v = env["vw"].get(f"/api/insights/reports/{rep1['id']}"); assert v.status_code == 200
    v = v.json(); assert [c["n"] for c in v["clusters"]] == [30, 20, 10] and [c["error_rate"] for c in v["clusters"]] == [c["error_rate"] for c in rep1["clusters"]]
    top_a = v["clusters"][0]      # topic A threads belong to the editor
    assert top_a["examples"] == [] and top_a["hidden_examples"] > 0
    assert v["clusters"][1]["examples"] and v["clusters"][1]["hidden_examples"] == 0
    assert env["ed"].get(f"/api/insights/reports/{rep1['id']}").json()["clusters"][0]["examples"]

def test_roles_and_demo_are_read_only(rep1, env):
    for k in ("vw", "dm"):
        assert env[k].post("/api/insights/run", json={"days": 7}).status_code == 403
        assert env[k].put("/api/insights/settings", json={"weekly": True}).status_code == 403
        assert env[k].delete(f"/api/insights/reports/{rep1['id']}").status_code == 403
        assert env[k].get("/api/insights/reports").status_code == 200 and env[k].get(f"/api/insights/reports/{rep1['id']}").status_code == 200
    assert env["ed"].put("/api/insights/settings", json={"weekly": True}).status_code == 403          # settings are admin-only
    assert env["ed"].post("/api/insights/weekly/run_now").status_code == 403

def test_rls_isolation_between_workspaces(rep1, env):
    assert env["ed2"].get("/api/insights/reports").json() == []
    assert env["ed2"].get(f"/api/insights/reports/{rep1['id']}").status_code == 404
    assert env["ed2"].delete(f"/api/insights/reports/{rep1['id']}").status_code == 200       # no-op: the row is invisible
    assert env["ed"].get(f"/api/insights/reports/{rep1['id']}").status_code == 200
    assert env["ed2"].post("/api/insights/run", json={"days": 7}).status_code == 422          # W2 has no threads; W1's are not visible
    with pg() as c: assert c.execute("SELECT count(*) FROM insight_clusters WHERE report_id=%s", (rep1["id"],)).fetchone()[0] >= 3

# ---------------------------------------------------------------- change vs previous report
def test_second_report_shows_change(rep1, env):
    wipe_threads(env)
    add_threads(env, {"A": (10, 0, 0.002, []), "B": (15, 0, 0.01, []), "C": (35, 5, 0, [])}, "p2")
    r2 = run(env["ed"]); assert r2["status"] == "done", r2.get("summary")
    cl = r2["clusters"]; assert [c["n"] for c in cl] == [35, 15, 10]
    top, mid, low = cl                                       # C (was 10 -> 35), B (20 -> 15), A (30 -> 10)
    assert round(top["prev_share"], 3) == 0.167 and round(mid["prev_share"], 3) == 0.333 and round(low["prev_share"], 3) == 0.5
    ch = r2["meta"]["change"]; assert ch["label"] == top["label"] and ch["new"] is False and abs(ch["delta"] - (35 / 60 - 10 / 60)) < 1e-9
    assert "tăng 42 điểm %" in r2["summary"] and top["label"] in r2["summary"]
    env["r2"] = r2

# ---------------------------------------------------------------- weekly report
class Hook(BaseHTTPRequestHandler):
    got = []
    def do_POST(self):
        Hook.got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"])))); self.send_response(200); self.end_headers()
    def log_message(self, *a): pass

def test_weekly_job_notifies_alert_channel(env):
    srv = HTTPServer(("127.0.0.1", 0), Hook); threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pg() as c:
            c.execute("INSERT INTO alert_rules(project_id,name,metric,op,threshold,channel,target) VALUES(%s,'ins-hook','count','gt',1000000,'webhook',%s)", (env["pid"], f"http://127.0.0.1:{srv.server_port}/h"))
        ad = env["ad"]
        assert ad.put("/api/insights/settings", json={"weekly": False}).json() == {"weekly": False}
        assert ad.post("/api/insights/weekly/run_now", params={"check": "true"}).json() == {"skipped": "weekly report is off for this workspace"}   # switch off => scheduler would skip it
        assert not Hook.got
        assert ad.put("/api/insights/settings", json={"weekly": True}).json() == {"weekly": True}
        s = ad.get("/api/insights/settings").json(); assert s["weekly"] and [c["kind"] for c in s["channels"]] == ["webhook"]
        r = ad.post("/api/insights/weekly/run_now", params={"check": "true"}); assert r.status_code == 200, r.text
        j = r.json(); assert j["status"] == "done" and j["delivery"][0]["channel"] == "webhook" and j["delivery"][0]["result"].startswith("sent"), j
        assert len(Hook.got) == 1
        m = Hook.got[0]; assert "Insights tuần" in m["subject"] and "Top chủ đề:" in m["body"]
        lines = [x for x in m["body"].splitlines() if x[:2] in ("1.", "2.", "3.", "4.")]
        assert len(lines) == 3 and "%" in lines[0] and "60 hội thoại, 3 chủ đề" in m["body"]       # top topics with their share; same data as report 2 => no new "change" sentence
        assert "Thay đổi lớn nhất" not in m["body"]
        rep = ad.get(f"/api/insights/reports/{j['report_id']}").json(); assert rep["source"] == "weekly" and rep["status"] == "done"
        ad.put("/api/insights/settings", json={"weekly": False})
    finally:
        srv.shutdown()

# ---------------------------------------------------------------- caps
def test_feature_cap_gives_402_before_any_work(env, a):
    n_before = len(env["ed"].get("/api/insights/reports").json())
    with pg() as c: c.execute("INSERT INTO gateway_usage(ts,model,input_tokens,output_tokens,cost_usd,status,feature) VALUES(now(),'gemini-3.6-flash',0,0,0.5,'t73','insights')")
    cap = a.post("/api/gateway/caps", json={"scope": "feature", "target": "insights", "period": "total", "limit_usd": 0.01}); assert cap.status_code == 200, cap.text
    try:
        r = env["ed"].post("/api/insights/run", json={"days": 7})
        assert r.status_code == 402 and r.json()["detail"].startswith("Đã chạm trần chi phí tính năng Insights"), r.text
        assert len(env["ed"].get("/api/insights/reports").json()) == n_before        # nothing was created
    finally:
        a.delete(f"/api/gateway/caps/{cap.json()['id']}")
        with pg() as c: c.execute("DELETE FROM gateway_usage WHERE status='t73'")

def test_estimate_over_remaining_cap_aborts_with_clear_message(env, a):
    cap = a.post("/api/gateway/caps", json={"scope": "feature", "target": "insights", "period": "total", "limit_usd": 0.0001}); assert cap.status_code == 200, cap.text
    try:
        with pg() as c: spent = c.execute("SELECT coalesce(sum(cost_usd),0) FROM gateway_usage WHERE feature='insights'").fetchone()[0]
        if float(spent) >= 0.0001: pytest.skip("insights already spent more than the test cap")
        r = env["ed"].post("/api/insights/run", json={"days": 7})
        assert r.status_code == 402 and "Ước tính chi phí" in r.json()["detail"] and "vượt hạn mức còn lại" in r.json()["detail"], r.text
    finally:
        a.delete(f"/api/gateway/caps/{cap.json()['id']}")
    assert env["ed"].post("/api/insights/run", json={"days": 7}).status_code == 200      # cap removed -> runs (and cost estimate is well under $0.05)

def test_input_validation(env):
    assert env["ed"].post("/api/insights/run", json={"days": 0}).status_code == 422
    assert env["ed"].post("/api/insights/run", json={"days": 7, "lang": "fr"}).status_code == 422
    assert env["ed"].post("/api/insights/run", json={"days": 7, "project_id": 99999999}).status_code == 404
    assert env["ed"].get("/api/insights/reports/99999999").status_code == 404
