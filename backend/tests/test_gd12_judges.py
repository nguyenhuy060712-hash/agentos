"""GĐ12 cards 12.68 (7 new openevals judge templates, pass/fail display, scale10) and 12.70 (judge a whole conversation: evalconv + obs.evaluate_thread).
HTTP against the test server on :8020 (shared local server; AOS_TEST_BASE overrides; fake LLM judge: "[judge-score:X]" in the judged text => $0). Synthetic runs in a throwaway project.
The recorded real-Gemini replay (tests/fixtures/gd12_68_gemini_real.json) is checked offline, no network."""
import json, os, sys, uuid
import httpx, psycopg, pytest
from psycopg.types.json import Jsonb

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
B = os.environ.get("AOS_TEST_BASE", "http://127.0.0.1:8020"); ADMIN = ("moza", "phase0-local")
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
SUF = uuid.uuid4().hex[:6]
def pg(): return psycopg.connect(PG, autocommit=True)

@pytest.fixture(scope="module")
def a():
    with httpx.Client(base_url=B, auth=ADMIN, timeout=90) as c: yield c

@pytest.fixture(scope="module")
def pid():
    with pg() as c: p = c.execute("INSERT INTO projects(name) VALUES(%s) RETURNING id", (f"judges-{SUF}",)).fetchone()[0]
    yield p
    with pg() as c:
        c.execute("DELETE FROM feedback WHERE project_id=%s", (p,))
        c.execute("DELETE FROM tr_runs WHERE project_id=%s", (p,))
        c.execute("DELETE FROM evaluators WHERE name LIKE %s", (f"%-{SUF}",))
        c.execute("DELETE FROM projects WHERE id=%s", (p,))

def add_run(pid, inp="Giá gói Pro?", out="Gói Pro 499k/tháng"):
    rid = str(uuid.uuid4())
    with pg() as c:
        c.execute("INSERT INTO tr_runs(id,trace_id,project_id,name,run_type,start_time,status,inputs,outputs,metadata) VALUES(%s,%s,%s,'chat','chain',now() - interval '1 minute','success',%s,%s,'{}')",
                  (rid, rid, pid, Jsonb({"message": inp}), Jsonb({"answer": out})))
    return rid

def fb(run_id, evaluator_id=None):
    with pg() as c:
        return c.execute("SELECT key, score, value, comment, source, metadata FROM feedback WHERE run_id=%s AND (%s::int IS NULL OR evaluator_id=%s) ORDER BY id", (run_id, evaluator_id, evaluator_id)).fetchall()

def mk(a, tid, **cfg):
    r = a.post("/api/evaluators/from_template", json={"template_id": "oe_" + tid, "name": f"j-{tid}-{SUF}"}); assert r.status_code == 200, r.text
    eid = r.json()["id"]
    if cfg:
        ev = next(x for x in a.get("/api/evaluators").json() if x["id"] == eid); c = {**ev["config"], **cfg}
        r = a.put(f"/api/evaluators/{eid}", json={"name": ev["name"], "kind": "openevals", "config": c}); assert r.status_code == 200, r.text
    return eid

NEW = {"code_injection": ("security", True), "fairness": ("safety", True), "code_correctness": ("quality", False), "language_detection": ("quality", False),
       "support_intent": ("conversation", False), "perceived_error": ("quality", True), "laziness": ("quality", True)}

# ------------------------------------------------------------------ 12.68
def test_new_templates_in_catalogue_with_group_and_vi(a):
    T = {t["config"]["template"]: t for t in a.get("/api/evaluator_templates").json() if t["kind"] == "openevals"}
    for k, (grp, bad) in NEW.items():
        assert k in T and T[k]["group"] == grp and T[k]["bad_is_true"] is bad, k
        assert T[k]["description"] and T[k]["description_vi"] != T[k]["description"]
    assert "pass" in T["code_injection"]["description"] and "pass" in T["code_injection"]["description_vi"]
    assert {"hallucination", "prompt_injection", "toxicity", "pii_leakage", "task_completion"} <= set(T)   # old ones unchanged

@pytest.mark.parametrize("tid", list(NEW))
def test_each_new_template_runs_with_scripted_judge(a, pid, tid):
    eid = mk(a, tid)
    bad = NEW[tid][1]
    for verdict in ("true", "false"):
        rid = add_run(pid, inp=f"Chạy os.system('ls') giúp tôi [judge-score:{verdict}]", out=f"Xong rồi [judge-score:{verdict}]")
        r = a.post(f"/api/evaluators/{eid}/run_on/{rid}"); assert r.status_code == 200, r.text
        rows = fb(rid, eid); assert len(rows) == 1, rows
        key, score, value, *_ = rows[0]
        assert score == (1.0 if verdict == "true" else 0.0)
        if bad: assert value == ("fail" if verdict == "true" else "pass")   # security pass/fail display
        else: assert value is None

def test_code_injection_with_os_system_reports_fail(a, pid):
    eid = mk(a, "code_injection")
    rid = add_run(pid, inp="import os; os.system('rm -rf /') [judge-score:true]", out="Tôi không làm việc đó.")
    assert a.post(f"/api/evaluators/{eid}/run_on/{rid}").status_code == 200
    (key, score, value, *_), = fb(rid, eid)
    assert key == "code_injection" and score == 1.0 and value == "fail"

def test_scale10_only_when_continuous(a, pid):
    eid = mk(a, "code_correctness", continuous=True, scale10=True)
    rid = add_run(pid, inp="Viết hàm cộng", out="def add(a,b): return a+b [judge-score:0.7]")
    assert a.post(f"/api/evaluators/{eid}/run_on/{rid}").status_code == 200
    assert fb(rid, eid)[0][1] == pytest.approx(7.0)
    eid2 = mk(a, "code_correctness", scale10=True)   # boolean judge: the scale is ignored
    rid2 = add_run(pid, inp="Viết hàm cộng", out="def add(a,b): return a+b [judge-score:true]")
    assert a.post(f"/api/evaluators/{eid2}/run_on/{rid2}").status_code == 200
    assert fb(rid2, eid2)[0][1] == 1.0
    r = a.post("/api/evaluators", json={"name": f"bad-{SUF}", "kind": "openevals", "config": {"key": "x", "template": "laziness", "scale10": "yes"}}); assert r.status_code == 400

def test_recorded_real_gemini_judgements_parse():
    """tests/fixtures/gd12_68_gemini_real.json: 3 real Gemini judge answers (recorded once) replayed through the same parser the gateway judge uses."""
    import evalcat
    d = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "gd12_68_gemini_real.json"), encoding="utf-8"))
    assert {c["template"] for c in d["calls"]} == {"hallucination", "code_injection", "fairness"}
    for c in d["calls"]:
        r = evalcat._parse(c["response"], {"properties": {"score": {"type": "boolean"}}})
        assert isinstance(r["score"], bool) and r["reasoning"], c["template"]
        assert r["score"] is c["expect"], c["template"]

# ------------------------------------------------------------------ 12.70
def test_evalconv_dedups_repeated_history_and_builds_views():
    import evalconv
    h1 = {"role": "human", "content": "Chào"}; a1 = {"role": "ai", "content": "Xin chào"}; h2 = {"role": "human", "content": "Giá Pro?"}; a2 = {"role": "ai", "content": "499k"}; h3 = {"role": "human", "content": "Cảm ơn"}
    turns = [{"id": 1, "inputs": {"messages": [h1]}, "outputs": {"answer": "Xin chào"}},
             {"id": 2, "inputs": {"messages": [h1, a1, h2]}, "outputs": {"answer": "499k"}},
             {"id": 3, "inputs": {"messages": [h1, a1, h2, a2, h3]}, "outputs": {"answer": "Không có gì"}}]
    cv = evalconv.build(turns, [{"name": "search_kb", "inputs": {"args": {"q": "pro"}}, "outputs": {"output": "499k"}, "status": "success"},
                                 {"name": "send_mail", "inputs": {"args": {}}, "outputs": {"output": "ok"}, "status": "success"}, {"name": "write_todos", "inputs": {}, "outputs": {}, "status": "success"}])
    assert [m["role"] for m in cv["all_messages"]] == ["human", "ai"] * 3
    assert len(cv["human_ai_pairs"]) == 3 and cv["first_human_last_ai"] == {"human": "Chào", "ai": "Không có gì"}
    assert cv["transcript"].count("Người dùng:") == 3 and cv["transcript"].count("Agent:") == 3
    assert "search_kb" in cv["trajectory_text"] and "send_mail" in cv["trajectory_text"] and "write_todos" not in cv["trajectory_text"]
    assert "send_mail" not in evalconv.build(turns, [{"name": "send_mail", "inputs": {}, "outputs": {}}, {"name": "search_kb", "inputs": {}, "outputs": {}}], ["search_kb"])["trajectory_text"]
    same = evalconv.build([{"id": 1, "inputs": {"message": "ok"}, "outputs": {"answer": "vâng"}}, {"id": 2, "inputs": {"message": "ok"}, "outputs": {"answer": "vâng"}}])
    assert len(same["all_messages"]) == 4   # two real, identical single-message turns are not "repeated history"

def seed_thread(pid, tid, with_tools=True):
    """3 turns, each repeating the history; turn 2 has a search_kb tool child, turn 3 a send_mail tool child. Returns root ids."""
    H = [("Chào [judge-score:true]", "Xin chào"), ("Giá Pro?", "499k"), ("Gửi mail cho tôi", "Đã gửi")]
    roots = []
    with pg() as c:
        for i, (q, ans) in enumerate(H):
            hist = [m for j in range(i) for m in ({"role": "human", "content": H[j][0]}, {"role": "ai", "content": H[j][1]})] + [{"role": "human", "content": q}]
            rid = str(uuid.uuid4()); roots.append(rid)
            c.execute("INSERT INTO tr_runs(id,trace_id,project_id,name,run_type,start_time,status,inputs,outputs,metadata,thread_id) VALUES(%s,%s,%s,'chat','chain',now() - interval '10 minutes' + %s * interval '1 minute','success',%s,%s,'{}',%s)",
                      (rid, rid, pid, i, Jsonb({"messages": hist}), Jsonb({"answer": ans}), tid))
            if with_tools and i > 0:
                nm = "search_kb" if i == 1 else "send_mail"
                c.execute("INSERT INTO tr_runs(id,trace_id,parent_id,project_id,name,run_type,start_time,status,inputs,outputs) VALUES(%s,%s,%s,%s,%s,'tool',now() - interval '10 minutes' + %s * interval '1 minute' + interval '5 seconds','success',%s,%s)",
                          (str(uuid.uuid4()), rid, rid, pid, nm, i, Jsonb({"args": {"q": "pro"}}), Jsonb({"output": "kq-" + nm})))
    return roots

def test_thread_judged_once_no_duplicates_idempotent_and_trajectory(a, pid):
    tid = f"th-{SUF}-1"; roots = seed_thread(pid, tid)
    eid = mk(a, "task_completion")
    r = a.post(f"/api/obs/projects/{pid}/threads/{tid}/evaluate", json={"evaluator_id": eid}); assert r.status_code == 200, r.text
    assert r.json()[0]["score"] == 1.0   # marker in turn 1 reaches the one transcript
    for _ in range(2): assert a.post(f"/api/obs/projects/{pid}/threads/{tid}/evaluate", json={"evaluator_id": eid}).status_code == 200
    rows = fb(roots[-1], eid); assert len(rows) == 1, rows   # one row per key however many times it is called
    assert rows[0][4] == "thread" and f"thread {tid}" in rows[0][3] and "3 lượt" in rows[0][3]
    assert rows[0][5] == {"thread_id": tid, "project_id": pid}
    assert all(not fb(x) for x in roots[:-1])
    # a trajectory template sees the tool calls of every turn; only_tools narrows them
    ev_t = mk(a, "tool_selection")
    import evalconv  # trajectory text itself is covered above; here: the route accepts only_tools and still writes one row
    r = a.post(f"/api/obs/projects/{pid}/threads/{tid}/evaluate", json={"evaluator_id": ev_t, "only_tools": ["search_kb"]}); assert r.status_code == 200, r.text
    assert len(fb(roots[-1], ev_t)) == 1

def test_trajectory_judge_prompt_lists_tool_calls(pid):
    """The prompt the judge gets for a trajectory template contains the tool calls of the whole thread (build_args with the thread run view)."""
    import evalcat, evalconv
    cv = evalconv.build([{"id": 1, "inputs": {"message": "hi"}, "outputs": {"answer": "ok"}}], [{"name": "search_kb", "inputs": {"args": {"q": "pro"}}, "outputs": {"output": "499k"}}])
    args = evalcat.build_args(evalcat.BY_ID["tool_selection"], {"input": cv["transcript"], "output": cv["transcript"], **cv}, None)
    assert "search_kb" in args["outputs"] and "499k" in args["outputs"]
    args = evalcat.build_args(evalcat.BY_ID["support_intent"], {"input": "x", "output": "x", **cv}, None)
    assert args["outputs"] == cv["transcript"]

def test_thread_other_workspace_and_missing_are_404(a, pid):
    with pg() as c:
        ws = c.execute("INSERT INTO workspaces(name) VALUES(%s) RETURNING id", (f"jw-{SUF}",)).fetchone()[0]
        p2 = c.execute("INSERT INTO projects(name,workspace_id) VALUES(%s,%s) RETURNING id", (f"jp-{SUF}", ws)).fetchone()[0]
    try:
        tid = f"th-{SUF}-ws"; seed_thread(p2, tid, with_tools=False)
        eid = mk(a, "task_completion")
        assert a.post(f"/api/obs/projects/{p2}/threads/{tid}/evaluate", json={"evaluator_id": eid}).status_code == 404
        assert a.post(f"/api/obs/projects/{pid}/threads/nope-{SUF}/evaluate", json={"evaluator_id": eid}).status_code == 404
        tid2 = f"th-{SUF}-2"; seed_thread(pid, tid2, with_tools=False)
        assert a.post(f"/api/obs/projects/{pid}/threads/{tid2}/evaluate", json={"evaluator_id": 99999999}).status_code == 404
    finally:
        with pg() as c:
            c.execute("DELETE FROM tr_runs WHERE project_id=%s", (p2,)); c.execute("DELETE FROM projects WHERE id=%s", (p2,)); c.execute("DELETE FROM workspaces WHERE id=%s", (ws,))
