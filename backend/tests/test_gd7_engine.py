"""GD7.4 (22/10/2026): Engine-lite (app/engine.py): issues from error clusters / low evaluator scores / thumbs-down -> root cause + fix proposal (fake LLM, feature 'engine')
-> Apply (new Hub commit, never tagged) -> dataset from the sample runs -> re-test (two experiments old vs new). Local server :8020, fake LLM => $0, no network.
Synthetic data in a throwaway workspace; the tests run in file order and share state through the `env` fixture."""
import json, os, threading, time, uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
import httpx, psycopg, pytest
from psycopg.types.json import Jsonb
from test_fixes import B, ADMIN, a  # noqa: F401  (fixtures)
from test_gd7_tools import basic, call, ok  # noqa: F401  (MCP helpers for the aos_tools check)

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
PW = "phase0-gd74-local"; SUF = uuid.uuid4().hex[:6]
ERR = "Tool search_kb failed: upstream returned HTTP 503"
ERR2 = "Agent2 refused: policy lookup missing"

def pg(): return psycopg.connect(PG, autocommit=True)

def user(a, name, ws, role="editor", demo=False):
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": role, "workspace_id": ws, "demo": demo})
    assert r.status_code == 200, r.text
    return r.json()["id"], httpx.Client(base_url=B, auth=(name, PW), timeout=90)

class Hook(BaseHTTPRequestHandler):
    got = []
    def do_POST(self):
        Hook.got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"])))); self.send_response(200); self.end_headers()
    def log_message(self, *a): pass

@pytest.fixture(scope="module")
def env(a):
    w1 = a.post("/api/admin/workspaces", json={"name": f"eng-w1-{SUF}"}).json()["id"]
    w2 = a.post("/api/admin/workspaces", json={"name": f"eng-w2-{SUF}"}).json()["id"]
    e = {"w1": w1, "w2": w2}
    for k, role, ws, demo in (("ed", "editor", w1, False), ("ed3", "editor", w1, False), ("vw", "viewer", w1, False), ("ad", "admin", w1, False), ("dm", "viewer", w1, True), ("ed2", "editor", w2, False)):
        e[k + "_id"], e[k] = user(a, f"eng_{k}_{SUF}", ws, role, demo)
    with pg() as c:
        c.execute("UPDATE workspaces SET engine_enabled=true WHERE id IN (%s,%s)", (w1, w2))   # 12.32: new workspaces start with the Engine off
        e["pid"] = c.execute("INSERT INTO projects(name, workspace_id) VALUES(%s,%s) RETURNING id", (f"eng-{SUF}", w1)).fetchone()[0]
    r = e["ed"].post("/api/agents", json={"name": f"eng-agent-{SUF}", "system_prompt": "Bạn là trợ lý hỗ trợ khách hàng. Trả lời ngắn gọn.", "tools": []}); assert r.status_code == 200, r.text
    e["agent"] = r.json()["id"]
    e["prompt2"] = f"eng-p2-{SUF}"
    assert e["ed"].post("/api/hub/commit", json={"name": e["prompt2"], "template": "Bạn là agent 2.", "tag": "production", "message": "v1"}).status_code == 200
    r = e["ed"].post("/api/agents", json={"name": f"eng-agent2-{SUF}", "prompt_ref": e["prompt2"] + ":production", "tools": []}); assert r.status_code == 200, r.text
    e["agent2"] = r.json()["id"]
    srv = HTTPServer(("127.0.0.1", 0), Hook); threading.Thread(target=srv.serve_forever, daemon=True).start()
    with pg() as c:
        c.execute("INSERT INTO alert_rules(project_id,name,metric,op,threshold,channel,target) VALUES(%s,'eng-hook','count','gt',1000000,'webhook',%s)", (e["pid"], f"http://127.0.0.1:{srv.server_port}/h"))
    yield e
    srv.shutdown()
    for k in ("ed", "ed3", "vw", "ad", "dm", "ed2"): e[k].close()
    with pg() as c:
        c.execute("DELETE FROM experiments WHERE dataset_id IN (SELECT id FROM datasets WHERE workspace_id IN (%s,%s))", (w1, w2))
        c.execute("DELETE FROM datasets WHERE workspace_id IN (%s,%s)", (w1, w2))
        c.execute("DELETE FROM feedback WHERE trace_id IN (SELECT trace_id FROM tr_runs WHERE project_id IN (SELECT id FROM projects WHERE workspace_id IN (%s,%s)))", (w1, w2))
        c.execute("DELETE FROM tr_runs WHERE project_id IN (SELECT id FROM projects WHERE workspace_id IN (%s,%s))", (w1, w2))
        c.execute("DELETE FROM alert_rules WHERE project_id=%s", (e["pid"],))
        c.execute("DELETE FROM projects WHERE workspace_id IN (%s,%s)", (w1, w2))
        c.execute("DELETE FROM prompts WHERE workspace_id IN (%s,%s)", (w1, w2))
        c.execute("DELETE FROM agents WHERE workspace_id IN (%s,%s)", (w1, w2))
        c.execute("DELETE FROM outbox WHERE ws_id IN (%s,%s)", (w1, w2))
        c.execute("DELETE FROM users WHERE username LIKE %s", (f"eng\\_%\\_{SUF}",))
        c.execute("DELETE FROM workspaces WHERE id IN (%s,%s)", (w1, w2))
        c.execute("DELETE FROM gateway_usage WHERE status='t74'")

def add_run(e, *, status="success", error=None, msg="hello", agent=None, user=None, minutes=1, name="chat", fb=None):
    rid = str(uuid.uuid4()); agent = agent or e["agent"]
    with pg() as c:
        c.execute("""INSERT INTO tr_runs(id,trace_id,project_id,name,run_type,start_time,status,error,inputs,outputs,metadata,user_id)
            VALUES(%s,%s,%s,%s,'chain',now() - %s * interval '1 minute',%s,%s,%s,%s,%s,%s)""",
                  (rid, rid, e["pid"], name, minutes, status, error, Jsonb({"message": msg}), Jsonb({"answer": "ans " + msg}), Jsonb({"agent_id": agent}), user))
        for key, score, ev in (fb or []):
            c.execute("INSERT INTO feedback(run_id,trace_id,key,score,evaluator_id) VALUES(%s,%s,%s,%s,%s)", (rid, rid, key, score, ev))
    return rid

def detect(e, who="ed", analyse=False):
    r = e[who].post("/api/engine/detect", params={"analyse": str(analyse).lower()}); assert r.status_code == 200, r.text
    return r.json()

def issues(c, **kw):
    r = c.get("/api/engine/issues", params={"status": "all", **kw}); assert r.status_code == 200, r.text
    return r.json()

def by_kind(c, kind):
    return next(i for i in issues(c)["items"] if i["kind"] == kind and i["project_id"])

def outbox(e):
    with pg() as c: return [r[0] for r in c.execute("SELECT subject FROM outbox WHERE ws_id=%s AND subject LIKE '[Moza Agent OS] Engine%%' ORDER BY id", (e["w1"],)).fetchall()]

# ---------------------------------------------------------------- detector: 3 problem types
def test_three_problem_types_make_three_issues_with_auto_analysis(env):
    env["private_err"] = add_run(env, status="error", error=ERR, msg=f"private err {SUF}", user=env["ed_id"], minutes=1)   # the editor's own (private) chat trace
    env["err_runs"] = [add_run(env, status="error", error=ERR, msg=f"err question {i} {SUF}", minutes=10 + i) for i in range(3)]
    for i in range(3): add_run(env, msg=f"low question {i} {SUF}", minutes=20 + i, fb=[("helpfulness", 0.2, 999)])
    add_run(env, msg="fine question", minutes=30, fb=[("helpfulness", 0.9, 999)])                                         # good score: not counted
    add_run(env, msg="exp question", minutes=31, fb=[("helpfulness", 0.1, None)])                                         # human/no evaluator: not counted
    for i in range(3): add_run(env, msg=f"thumbs question {i} {SUF}", minutes=40 + i, fb=[("user_thumbs", 0, None)])
    add_run(env, msg="thumbs up", minutes=45, fb=[("user_thumbs", 1, None)])
    add_run(env, status="error", error="one-off weirdness", minutes=50)                                                  # 1 error => still an issue (min 1 for errors)
    r = detect(env, analyse=True)
    assert len(r["new"]) == 4 and not r["reopened"] and len(r["analysed"]) == 3, r         # 3 auto analyses per cycle, the 4th waits for the button
    items = issues(env["ed"])["items"]
    kinds = sorted(i["kind"] for i in items); assert kinds == ["error_cluster", "error_cluster", "low_score", "negative_feedback"], kinds
    err = next(i for i in items if i["kind"] == "error_cluster" and i["count"] == 4); low = by_kind(env["ed"], "low_score"); neg = by_kind(env["ed"], "negative_feedback")
    assert low["count"] == 3 and neg["count"] == 3 and low["title"].startswith("helpfulness < 0.5") and f"eng-agent-{SUF}" in low["title"] and f"eng-agent-{SUF}" in neg["title"]
    assert detect(env)["new"] == [] and len(issues(env["ed"])["items"]) == 4           # idempotent
    env.update(err=err["id"], low=low["id"], neg=neg["id"], one=next(i["id"] for i in items if i["count"] == 1))
    d = env["ed"].get(f"/api/engine/issues/{env['err']}").json()
    assert d["status"] == "open" and d["count"] == 4 and d["has_analysis"] and d["analysis"]["root_cause"].startswith("Giả lập:")
    assert sum(i["has_analysis"] for i in issues(env["ed"])["items"]) == 3 and not env["ed"].get(f"/api/engine/issues/{env['one']}").json()["has_analysis"]

def test_notify_new_issues_once(env):
    subj = outbox(env); assert len(subj) == 4 and all("new issue" in s for s in subj), subj
    assert len(Hook.got) >= 4 and any(ERR[:30] in m["subject"] for m in Hook.got)

# ---------------------------------------------------------------- recurrence / reopen
def test_recurrence_increments_count_and_last_seen(env):
    before = env["ed"].get(f"/api/engine/issues/{env['err']}").json()
    env["err_runs"] += [add_run(env, status="error", error=ERR, msg=f"err more {i} {SUF}", minutes=0.1) for i in range(2)]
    r = detect(env); assert r["new"] == [] and r["updated"] >= 1, r
    after = env["ed"].get(f"/api/engine/issues/{env['err']}").json()
    assert after["count"] == before["count"] + 2 == 6 and after["last_seen"] > before["last_seen"] and len(issues(env["ed"])["items"]) == 4

def test_fixed_then_recurs_is_reopened_and_notification_is_throttled(env):
    n_msgs = len(outbox(env))
    r = env["ed"].put(f"/api/engine/issues/{env['err']}", json={"status": "fixed", "note": "deployed fix"}); assert r.status_code == 200 and r.json()["status"] == "completed"
    detect(env); assert env["ed"].get(f"/api/engine/issues/{env['err']}").json()["status"] == "completed"          # nothing new since the fix => stays fixed
    time.sleep(1.2)
    add_run(env, status="error", error=ERR, msg=f"err after fix {SUF}", minutes=0)
    r = detect(env); assert r["reopened"] == [env["err"]] and not r["new"], r
    d = env["ed"].get(f"/api/engine/issues/{env['err']}").json()
    assert d["status"] == "open" and d["note"].startswith("Reopened") and d["count"] == 7 and d["reopened"] == 1
    assert len(outbox(env)) == n_msgs and r["notified"] == 0                       # already notified < 24 h ago => no second message
    with pg() as c: c.execute("UPDATE issues SET notified_at = now() - interval '2 day' WHERE id=%s", (env["err"],))
    ign = env["ed"].put(f"/api/engine/issues/{env['neg']}", json={"status": "ignored"}).json(); assert ign["status"] == "ignored"
    for _ in range(2): add_run(env, msg="more thumbs", minutes=0, fb=[("user_thumbs", 0, None)])
    detect(env); d2 = env["ed"].get(f"/api/engine/issues/{env['neg']}").json(); assert d2["status"] == "ignored" and d2["count"] == 5      # ignored stays ignored, still counted
    env["ed"].put(f"/api/engine/issues/{env['neg']}", json={"status": "new"})

# ---------------------------------------------------------------- analysis (fake LLM)
def test_analysis_is_stored_with_valid_evidence_and_diff(env):
    r = env["ed"].post(f"/api/engine/issues/{env['err']}/analyse", json={"lang": "vi"}); assert r.status_code == 200, r.text
    d = r.json(); an = d["analysis"]
    assert an["root_cause"].startswith("Giả lập:") and d["has_analysis"] and d["analysed_at"]
    assert all(e["run_id"] in [s["run_id"] for s in d["samples"]] for e in an["evidence"]) and len(an["evidence"]) == 1       # the invented run id was dropped
    p = an["proposal"]; assert p["type"] == "prompt" and p["applicable"] and "Quy tắc Engine" in p["new_text"] and p["diff"].count("\n+") >= 1 and "Bạn là trợ lý" in p["diff"] + p["new_text"]
    assert an["base"]["inline"] is True and an["agent"]["id"] == env["agent"]
    low = env["ed"].get(f"/api/engine/issues/{env['low']}").json(); assert "Giả lập" in low["analysis"]["root_cause"]           # automatic analysis from the detector
    cfg = env["ed"].post("/api/engine/issues/999999/analyse", json={}); assert cfg.status_code == 404

def test_private_traces_are_hidden_from_other_users(env):
    d = env["ed"].get(f"/api/engine/issues/{env['err']}").json(); assert d["hidden_samples"] == 0
    with pg() as c: c.execute("UPDATE issues SET sample_run_ids = %s::jsonb || sample_run_ids WHERE id=%s", (json.dumps([env["private_err"]]), env["err"]))
    mine = env["ed"].get(f"/api/engine/issues/{env['err']}").json(); other = env["ed3"].get(f"/api/engine/issues/{env['err']}").json(); admin = env["ad"].get(f"/api/engine/issues/{env['err']}").json()
    assert env["private_err"] in [s["run_id"] for s in mine["samples"]] and env["private_err"] in [s["run_id"] for s in admin["samples"]]
    assert env["private_err"] not in [s["run_id"] for s in other["samples"]] and other["hidden_samples"] >= 1
    with pg() as c: c.execute("UPDATE issues SET analysis = jsonb_set(analysis, '{evidence}', %s::jsonb) WHERE id=%s", (json.dumps([{"run_id": env["private_err"], "quote": "SECRET quote"}]), env["err"]))
    assert "SECRET" not in json.dumps(env["ed3"].get(f"/api/engine/issues/{env['err']}").json()) and "SECRET" in json.dumps(env["ed"].get(f"/api/engine/issues/{env['err']}").json())
    assert env["ed3"].post(f"/api/engine/issues/{env['err']}/analyse").status_code == 200       # analysing as another user works with the samples that user can read

# ---------------------------------------------------------------- apply / dataset / re-test
def hub(c, name):
    pid = next(p["id"] for p in c.get("/api/hub").json() if p["name"] == name); return c.get(f"/api/hub/{pid}").json()

def test_apply_creates_untagged_commit_and_production_is_untouched(env):
    r = env["ed"].post(f"/api/engine/issues/{env['err']}/apply"); assert r.status_code == 200, r.text
    j = r.json(); assert j["version"] == 2 and j["base_version"] == 1 and j["tagged"] is False
    h = hub(env["ed"], j["name"]); assert h["tags"] == [] and len(h["versions"]) == 2
    v2 = next(v for v in h["versions"] if v["version"] == 2); assert v2["note"].startswith("Engine: ") and "Quy tắc Engine" in v2["template"]
    assert env["ed"].post(f"/api/engine/issues/{env['err']}/apply").status_code == 409          # already applied
    assert env["ed"].get(f"/api/engine/issues/{env['err']}").json()["status"] == "fixing"
    # agent whose prompt lives in the Hub with a production tag: the tag must not move
    for i in range(2): add_run(env, status="error", error=ERR2, msg=f"agent2 q {i} {SUF}", agent=env["agent2"], minutes=5 + i)
    assert len(detect(env, analyse=True)["new"]) == 1
    i2 = next(i for i in issues(env["ed"])["items"] if "policy" in i["title"].lower())
    r = env["ed"].post(f"/api/engine/issues/{i2['id']}/apply"); assert r.status_code == 200, r.text
    assert r.json()["name"] == env["prompt2"] and r.json()["version"] == 2
    h2 = hub(env["ed"], env["prompt2"]); assert {t["tag"]: t["version"] for t in h2["tags"]} == {"production": 1}
    assert env["vw"].post(f"/api/engine/issues/{env['low']}/apply").status_code == 403
    r = env["ed"].post(f"/api/engine/issues/{env['low']}/apply"); assert r.status_code == 200, r.text
    assert r.json()["version"] == 3 and r.json()["base_version"] == 1      # same inline agent: the existing baseline commit (v1) is reused, not duplicated
    env["i2"] = i2["id"]

def test_dataset_from_issue_has_the_sample_examples(env):
    d = env["ed"].get(f"/api/engine/issues/{env['err']}").json(); want = {s["run_id"] for s in d["samples"]}; assert len(want) == 5
    r = env["ed"].post(f"/api/engine/issues/{env['err']}/dataset"); assert r.status_code == 200, r.text
    j = r.json(); assert j["examples"] == 5 and j["name"] == f"engine-issue-{env['err']}"
    with pg() as c:
        src = {str(x[0]) for x in c.execute("SELECT source_run_id FROM dataset_examples WHERE dataset_id=%s", (j["dataset_id"],)).fetchall()}
        qs = [x[0] for x in c.execute("SELECT r.inputs->>'question' FROM dataset_examples e JOIN example_revisions r ON r.example_id=e.id WHERE e.dataset_id=%s", (j["dataset_id"],)).fetchall()]
    assert src == want and all(q and (SUF in q) for q in qs)
    assert env["ed"].post(f"/api/engine/issues/{env['err']}/dataset").json()["examples"] == 5            # idempotent
    other = env["ed3"].post(f"/api/engine/issues/{env['err']}/dataset"); assert other.status_code == 200 and other.json()["skipped_private"] == 1     # ed3 cannot read the private sample
    env["ds"] = j["dataset_id"]

def test_retest_creates_two_experiments_old_vs_new(env):
    assert env["ed"].post(f"/api/engine/issues/{env['low']}/retest").status_code in (409, 200)
    r = env["ed"].post(f"/api/engine/issues/{env['err']}/retest"); assert r.status_code == 200, r.text
    j = r.json(); assert j["old"] != j["new"] and j["dataset_id"] == env["ds"]
    with pg() as c:
        tg = {x[0]: x[1] for x in c.execute("SELECT id, target FROM experiments WHERE id IN (%s,%s)", (j["old"], j["new"])).fetchall()}
    assert tg[j["old"]]["version"] == 1 and tg[j["new"]]["version"] == 2 and tg[j["new"]]["type"] == "prompt" and tg[j["new"]]["prompt_id"] == tg[j["old"]]["prompt_id"]
    t = time.time(); d = None
    while time.time() - t < 60:
        d = env["ed"].get(f"/api/engine/issues/{env['err']}").json()
        res = d["retest_results"][-1]
        if res["old"]["status"] == "done" and res["new"]["status"] == "done": break
        time.sleep(0.5)
    assert res["old"]["status"] == "done" and res["new"]["status"] == "done" and res["old"]["summary"]["n"] == 5 == res["new"]["summary"]["n"], res
    assert env["ed"].post(f"/api/engine/issues/{env['i2']}/retest").status_code == 200
    fresh = env["ed"].post(f"/api/engine/issues/{env['one']}/retest"); assert fresh.status_code == 409        # nothing applied yet

# ---------------------------------------------------------------- aos_tools.list_issues (7.0 tool now returns real issues)
def test_aos_tool_list_issues_returns_real_issues(env):
    h = basic(f"eng_ed_{SUF}", PW)
    out = ok(h, "list_issues", project=f"eng-{SUF}"); assert out["total"] >= 3
    it = next(i for i in out["items"] if i["id"] == env["err"]); assert it["kind"] == "error_cluster" and it["has_analysis"] and it["root_cause"].startswith("Giả lập") and it["proposal_type"] == "prompt"
    assert env["err_runs"][0] in it["sample_run_ids"] or len(it["sample_run_ids"]) == 5
    assert all(i["status"] in ("open", "fixing") for i in out["items"])
    assert {i["kind"] for i in ok(h, "list_issues", project=f"eng-{SUF}", kind="low_score")["items"]} == {"low_score"}
    o3 = ok(basic(f"eng_ed3_{SUF}", PW), "list_issues", status="all"); e3 = next(i for i in o3["items"] if i["id"] == env["err"])
    assert env["private_err"] not in e3["sample_run_ids"]                                              # RLS of the caller
    assert ok(basic(f"eng_ed2_{SUF}", PW), "list_issues", status="all")["total"] == 0                  # other workspace

# ---------------------------------------------------------------- caps
def test_feature_cap_gives_402_for_the_button_and_a_reason_for_the_detector(env, a):
    with pg() as c: c.execute("INSERT INTO gateway_usage(ts,model,input_tokens,output_tokens,cost_usd,status,feature) VALUES(now(),'gemini-3.6-flash',0,0,0.5,'t74','engine')")
    cap = a.post("/api/gateway/caps", json={"scope": "feature", "target": "engine", "period": "total", "limit_usd": 0.01}); assert cap.status_code == 200, cap.text
    try:
        r = env["ed"].post(f"/api/engine/issues/{env['one']}/analyse", json={}); assert r.status_code == 402 and "engine" in r.json()["detail"].lower(), r.text
        add_run(env, status="error", error="cap test error " + SUF, minutes=0)
        res = detect(env, analyse=True); assert len(res["new"]) == 1 and res["analysed"] == [], res
        d = env["ed"].get(f"/api/engine/issues/{res['new'][0]}").json(); assert not d["has_analysis"] and "engine" in d["refs"]["analysis_error"].lower()
    finally:
        a.delete(f"/api/gateway/caps/{cap.json()['id']}")
        with pg() as c: c.execute("DELETE FROM gateway_usage WHERE status='t74'")
    assert env["ed"].post(f"/api/engine/issues/{env['one']}/analyse", json={}).status_code == 200            # cap removed => works

# ---------------------------------------------------------------- RLS / read-only / settings
def test_workspace_isolation_and_read_only_roles(env):
    assert issues(env["ed2"])["items"] == [] and env["ed2"].get(f"/api/engine/issues/{env['err']}").status_code == 404
    assert env["ed2"].put(f"/api/engine/issues/{env['err']}", json={"status": "fixed"}).status_code == 404
    for c in (env["vw"], env["dm"]):
        assert len(issues(c)["items"]) >= 4 and c.get(f"/api/engine/issues/{env['err']}").status_code == 200       # read
        for m, p, b in (("POST", f"/api/engine/issues/{env['err']}/analyse", {}), ("PUT", f"/api/engine/issues/{env['err']}", {"status": "fixed"}), ("POST", f"/api/engine/issues/{env['err']}/apply", None),
                        ("POST", f"/api/engine/issues/{env['err']}/dataset", None), ("POST", f"/api/engine/issues/{env['err']}/retest", None), ("POST", "/api/engine/detect", None), ("PUT", "/api/engine/settings", {"score_min": 1})):
            assert c.request(m, p, json=b).status_code == 403, (m, p)
    assert env["ed"].get("/api/engine/issues", params={"status": "bogus"}).status_code == 422 and env["ed"].get("/api/engine/issues", params={"kind": "bogus"}).status_code == 422

def test_score_threshold_is_configurable(env):
    assert env["ed"].put("/api/engine/settings", json={"score_min": 0.95}).status_code == 403          # admin only
    s = env["ad"].get("/api/engine/settings").json(); assert s["score_min"] == 0.5 and [c["kind"] for c in s["channels"]] == ["webhook"]
    assert env["ad"].put("/api/engine/settings", json={"score_min": 0.95}).json() == {"score_min": 0.95}
    try:
        r = env["ad"].post("/api/engine/detect", params={"analyse": "false"}).json()          # 0.9 and 0.2 scores are now both < 0.95 -> a second low_score signature? same key+agent => same issue, higher count
        low = env["ed"].get(f"/api/engine/issues/{env['low']}").json(); assert low["count"] == 4 and low["title"].startswith("helpfulness < 0.95"), (low, r)       # same issue (the signature has no threshold), the title follows the setting
    finally:
        env["ad"].put("/api/engine/settings", json={"score_min": 0.5})
    env["ad"].post("/api/engine/detect", params={"analyse": "false"}); assert env["ed"].get(f"/api/engine/issues/{env['low']}").json()["title"].startswith("helpfulness < 0.5")
    assert env["ad"].get("/api/engine/settings").json()["score_min"] == 0.5
