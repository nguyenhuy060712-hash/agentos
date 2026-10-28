"""GD7.6 (23/10/2026): evaluator catalogue from openevals 0.2.0 - kind `openevals` (17 templates, judge = our gateway), trajectory match through openevals (4 modes,
parity with the legacy matcher), node-level evaluator target (config.target_node), regex items in annotation queues. Local server :8020, fake LLM judge (scripted score
via "[judge-score:X]" in the judged text, overlay/fake_llm.fake_judge) => $0, no network. Synthetic runs in a throwaway project."""
import itertools, json, os, random, sys, uuid
import psycopg, pytest
from psycopg.types.json import Jsonb
from test_fixes import B, ADMIN, VIEWER, a  # noqa: F401  (fixtures)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
SUF = uuid.uuid4().hex[:6]
def pg(): return psycopg.connect(PG, autocommit=True)

@pytest.fixture(scope="module")
def pid():
    with pg() as c: p = c.execute("INSERT INTO projects(name) VALUES(%s) RETURNING id", (f"evalcat-{SUF}",)).fetchone()[0]
    yield p
    with pg() as c:
        c.execute("DELETE FROM feedback WHERE trace_id IN (SELECT trace_id FROM tr_runs WHERE project_id=%s)", (p,))
        c.execute("DELETE FROM tr_runs WHERE project_id=%s", (p,))
        c.execute("DELETE FROM evaluators WHERE name LIKE %s", (f"%-{SUF}",))
        c.execute("DELETE FROM annotation_queues WHERE name LIKE %s", (f"%-{SUF}",))
        c.execute("DELETE FROM gateway_usage WHERE status='t76'")
        c.execute("DELETE FROM projects WHERE id=%s", (p,))

def add_run(pid, inp="Giá gói Pro?", out="Gói Pro 499k/tháng", spans=(), meta=None):
    """root chain run (+ child spans [(name, run_type, inputs, outputs, parent_index|None)]) -> root id; returns (root, [span ids])"""
    rid = str(uuid.uuid4()); ids = []
    with pg() as c:
        c.execute("INSERT INTO tr_runs(id,trace_id,project_id,name,run_type,start_time,status,inputs,outputs,metadata) VALUES(%s,%s,%s,'chat','chain',now() - interval '1 minute','success',%s,%s,%s)",
                  (rid, rid, pid, Jsonb({"message": inp}), Jsonb({"answer": out}), Jsonb(meta or {})))
        for i, (name, typ, i_, o_, par) in enumerate(spans):
            sid = str(uuid.uuid4()); ids.append(sid)
            c.execute("INSERT INTO tr_runs(id,trace_id,parent_id,project_id,name,run_type,start_time,status,inputs,outputs) VALUES(%s,%s,%s,%s,%s,%s,now() - interval '50 seconds' + %s * interval '1 second','success',%s,%s)",
                      (sid, rid, ids[par] if par is not None else rid, pid, name, typ, i, Jsonb(i_), Jsonb(o_)))
    return rid, ids

def fb(run_id, evaluator_id=None):
    with pg() as c:
        return c.execute("SELECT key, score, comment, source FROM feedback WHERE run_id=%s AND (%s::int IS NULL OR evaluator_id=%s) ORDER BY id", (run_id, evaluator_id, evaluator_id)).fetchall()

# ------------------------------------------------------------------ templates
def test_template_catalogue_has_vi_descriptions(a):
    T = [t for t in a.get("/api/evaluator_templates").json() if t["kind"] == "openevals"]
    want = {"correctness", "conciseness", "hallucination", "answer_relevance", "rag_groundedness", "rag_helpfulness", "rag_retrieval_relevance", "toxicity", "pii_leakage",
            "prompt_injection", "trajectory_accuracy", "tool_selection", "plan_adherence", "task_completion", "user_satisfaction", "knowledge_retention", "agent_tone"}
    assert want <= {t["config"]["template"] for t in T}   # GĐ12.68 added more templates (tests/test_gd12_judges.py)
    assert all(t["description"] and t["description_vi"] and t["description_vi"] != t["description"] for t in T)
    assert any(t["bad_is_true"] for t in T if t["config"]["template"] in ("toxicity", "pii_leakage", "prompt_injection"))

TEMPLATES = ["correctness", "conciseness", "hallucination", "answer_relevance", "rag_groundedness", "rag_helpfulness", "rag_retrieval_relevance", "toxicity", "pii_leakage",
             "prompt_injection", "trajectory_accuracy", "tool_selection", "plan_adherence", "task_completion", "user_satisfaction", "knowledge_retention", "agent_tone"]

@pytest.mark.parametrize("tid", TEMPLATES)
def test_each_template_runs_with_scripted_judge_and_stores_score(a, pid, tid):
    r = a.post("/api/evaluators/from_template", json={"template_id": "oe_" + tid, "name": f"oe-{tid}-{SUF}"}); assert r.status_code == 200, r.text
    eid = r.json()["id"]
    spans = lambda m: [("search_kb", "tool", {"args": {"q": "gói pro"}}, {"output": "Gói Pro giá 499k/tháng " + m}, None), ("write_todos", "tool", {"todos": [{"content": "tra giá"}]}, {"output": "ok"}, None)]   # the marker must reach the prompt whichever variables the template uses
    rid, _ = add_run(pid, inp="Giá Pro? [judge-score:true]", spans=spans("[judge-score:true]"), out="Gói Pro 499k/tháng [judge-score:true]")
    res = a.post(f"/api/evaluators/{eid}/run_on/{rid}"); assert res.status_code == 200, res.text
    x = res.json()[0]; assert x["key"] == tid and x["score"] == 1.0 and "scripted true" in x["comment"], x
    rows = fb(rid, eid); assert len(rows) == 1 and rows[0][0] == tid and rows[0][1] == 1.0 and "scripted true" in rows[0][2], rows
    rid2, _ = add_run(pid, inp="Giá Pro? [judge-score:false]", spans=spans("[judge-score:false]"), out="Không biết [judge-score:false]")
    assert a.post(f"/api/evaluators/{eid}/run_on/{rid2}").json()[0]["score"] == 0.0

def test_continuous_and_override_and_validation(a, pid):
    r = a.post("/api/evaluators/from_template", json={"template_id": "oe_correctness", "name": f"oe-cont-{SUF}", "overrides": {"continuous": True}}); assert r.status_code == 200, r.text
    rid, _ = add_run(pid, out="x [judge-score:0.7]")
    assert a.post(f"/api/evaluators/{r.json()['id']}/run_on/{rid}").json()[0]["score"] == pytest.approx(0.7)
    assert a.post("/api/evaluators", json={"name": f"oe-bad-{SUF}", "kind": "openevals", "config": {"key": "k", "template": "nope"}}).status_code == 400
    assert a.post("/api/evaluators", json={"name": f"oe-bad2-{SUF}", "kind": "openevals", "config": {"key": "k", "template": "correctness", "target_node": 5}}).status_code == 400

def test_judge_without_scripted_score_returns_a_default(a, pid):
    eid = a.post("/api/evaluators/from_template", json={"template_id": "oe_conciseness", "name": f"oe-def-{SUF}"}).json()["id"]
    rid, _ = add_run(pid)
    x = a.post(f"/api/evaluators/{eid}/run_on/{rid}").json()[0]; assert x["score"] in (0.0, 1.0)

def test_prompt_variables_come_from_the_trace():
    import evalcat
    run = {"input": "Giá?", "output": "499k", "transcript": "User: Giá?\nAgent: 499k",
           "spans": [{"name": "search_kb", "run_type": "tool", "inputs": {"q": "giá"}, "outputs": {"output": "DOC-A"}}, {"name": "calc", "run_type": "tool", "inputs": {}, "outputs": {"output": "42"}},
                     {"name": "write_todos", "run_type": "tool", "inputs": {"todos": ["b1", "b2"]}, "outputs": {}}]}
    ex = {"outputs": {"answer": "499k"}}
    a_ = evalcat.build_args(evalcat.BY_ID["rag_groundedness"], run, ex); assert "DOC-A" in a_["context"] and "42" not in a_["context"]
    a_ = evalcat.build_args(evalcat.BY_ID["plan_adherence"], run, ex); assert "b1" in a_["plan"] and "search_kb" in a_["outputs"] and "write_todos" not in a_["outputs"]
    a_ = evalcat.build_args(evalcat.BY_ID["task_completion"], run, ex); assert a_["outputs"].startswith("User: Giá?")
    a_ = evalcat.build_args(evalcat.BY_ID["correctness"], run, ex); assert "499k" in str(a_["reference_outputs"])
    a_ = evalcat.build_args(evalcat.BY_ID["hallucination"], {**run, "spans": []}, {"inputs": {"context": "CTX"}}); assert a_["context"] == "CTX"

# ------------------------------------------------------------------ trajectory (openevals create_trajectory_match_evaluator)
def tc(*names): return [{"name": n} for n in names]

def test_trajectory_four_modes_via_openevals():
    import evalkit
    ref = tc("search", "calc")
    cases = {"strict": [(tc("search", "calc"), 1), (tc("calc", "search"), 0), (tc("search", "calc", "x"), 0)],
             "unordered": [(tc("calc", "search"), 1), (tc("search", "calc", "x"), 0), (tc("search", "search"), 0)],
             "subset": [(tc("search"), 1), (tc("search", "calc"), 1), (tc("search", "x"), 0)],
             "superset": [(tc("search", "calc", "x"), 1), (tc("calc", "search"), 1), (tc("search"), 0)]}
    for mode, rows in cases.items():
        for run, want in rows:
            assert evalkit.trajectory_match(run, ref, mode)["score"] == want, (mode, run)
            assert evalkit._openevals_match(evalkit._norm_traj(run), evalkit._norm_traj(ref), mode, "ignore") == bool(want), (mode, run)   # openevals itself agrees
    # args: exact / subset
    r1 = [{"name": "search", "args": {"q": "a", "n": 3}}]
    assert evalkit.trajectory_match(r1, [{"name": "search", "args": {"q": "a", "n": 3}}], "strict", "exact")["score"] == 1
    assert evalkit.trajectory_match(r1, [{"name": "search", "args": {"q": "a"}}], "strict", "exact")["score"] == 0
    assert evalkit.trajectory_match(r1, [{"name": "search", "args": {"q": "a"}}], "strict", "subset")["score"] == 1
    assert evalkit.trajectory_match(r1, [{"name": "search", "args": {"q": "b"}}], "superset", "subset")["score"] == 0
    assert evalkit.trajectory_match(r1, ["search"], "strict", "exact")["score"] == 1          # bare names = any args (legacy fallback)
    assert evalkit.trajectory_match(tc("write_todos", "search"), ["search"], "strict")["score"] == 1   # write_todos is ignored

def test_trajectory_openevals_matches_legacy_on_random_cases():
    import evalkit
    rnd = random.Random(7); names = ["a", "b", "c"]
    def mk(): return [{"name": rnd.choice(names), "args": {"k": rnd.choice([1, 2])}} for _ in range(rnd.randint(0, 3))]
    n = 0
    for _ in range(400):
        r, f = mk(), mk()
        for mode, args in itertools.product(evalkit.MODES, ("ignore", "exact", "subset")):
            oe = evalkit._openevals_match(evalkit._norm_traj(r), evalkit._norm_traj(f), mode, args)
            if oe is None: continue
            n += 1; assert oe == (evalkit._trajectory_match_legacy(evalkit._norm_traj(r), evalkit._norm_traj(f), mode, args)["score"] == 1.0), (r, f, mode, args)
    assert n > 1500

def test_trajectory_evaluator_kind_still_works(a, pid):
    eid = a.post("/api/evaluators", json={"name": f"traj-{SUF}", "kind": "trajectory", "config": {"key": "t", "mode": "superset", "reference": ["search"]}}).json()["id"]
    r = a.post(f"/api/evaluators/{eid}/test", json={"run": {"input": "q", "output": "o", "trajectory": tc("search", "calc")}}); assert r.json()[0]["score"] == 1.0, r.text

# ------------------------------------------------------------------ node-level target (B11)
def test_node_level_target(a, pid):
    spans = [("planner", "chain", {"q": 1}, {"output": "PLAN-OUT"}, None), ("lookup", "tool", {"args": {"q": "x"}}, {"output": "NODE-OUT 123"}, 0)]
    rid, ids = add_run(pid, out="root answer without the marker", spans=spans)
    cfg = {"key": "has_marker", "assertions": [{"type": "contains", "value": "NODE-OUT"}]}
    e_root = a.post("/api/evaluators", json={"name": f"node-root-{SUF}", "kind": "assertion", "config": cfg}).json()["id"]
    e_node = a.post("/api/evaluators", json={"name": f"node-node-{SUF}", "kind": "assertion", "config": {**cfg, "target_node": "lookup"}}).json()["id"]
    e_miss = a.post("/api/evaluators", json={"name": f"node-miss-{SUF}", "kind": "assertion", "config": {**cfg, "target_node": "nope"}}).json()["id"]
    assert a.post(f"/api/evaluators/{e_root}/run_on/{rid}").json()[0]["score"] == 0.0
    r = a.post(f"/api/evaluators/{e_node}/run_on/{rid}").json(); assert r[0]["score"] == 1.0, r
    assert [x[1] for x in fb(ids[1], e_node)] == [1.0] and fb(rid, e_node) == []          # feedback sits on the node span, not the root
    m = a.post(f"/api/evaluators/{e_miss}/run_on/{rid}").json(); assert m[0]["score"] is None and "nope" in m[0]["comment"] and fb(rid, e_miss) == []
    # a parent node sees its children (openevals RAG context = tool outputs under the node)
    e_par = a.post("/api/evaluators", json={"name": f"node-par-{SUF}", "kind": "assertion", "config": {"key": "p", "assertions": [{"type": "tool_called", "value": "lookup"}], "target_node": "planner"}}).json()["id"]
    assert a.post(f"/api/evaluators/{e_par}/run_on/{rid}").json()[0]["score"] == 1.0

# ------------------------------------------------------------------ regex rubric item in an annotation queue (B10)
def test_regex_rubric_item(a, pid):
    rub = [{"key": "has_phone", "type": "regex", "pattern": r"\b0\d{9}\b", "field": "output"}, {"key": "no_secret", "type": "regex", "pattern": "sk-", "field": "output", "negate": True},
           {"key": "from_vip", "type": "regex", "pattern": "^vip$", "field": "metadata.tier"}, {"key": "ok", "type": "boolean"}]
    q = a.post("/api/queues", json={"name": f"rx-{SUF}", "rubric": rub}); assert q.status_code == 200, q.text; qid = q.json()["id"]
    rid, _ = add_run(pid, out="Gọi 0912345678 nhé", meta={"tier": "vip"})
    assert a.post(f"/api/queues/{qid}/items", json={"run_ids": [rid]}).status_code == 200
    nxt = a.get(f"/api/queues/{qid}/next").json(); assert nxt["auto"] == {"has_phone": 1.0, "no_secret": 1.0, "from_vip": 1.0}, nxt["auto"]
    r = a.post(f"/api/queue_items/{nxt['item']['id']}", json={"scores": {"has_phone": 0, "ok": True}}); assert r.status_code == 200, r.text   # the client cannot override a regex item
    got = {k: s for k, s, _, src in fb(rid) if src == "annotation"}; assert got == {"has_phone": 1.0, "no_secret": 1.0, "from_vip": 1.0, "ok": 1.0}, got
    rid2, _ = add_run(pid, out="key sk-123, no phone", meta={"tier": "free"})
    a.post(f"/api/queues/{qid}/items", json={"run_ids": [rid2]})
    nxt = a.get(f"/api/queues/{qid}/next").json(); assert nxt["auto"] == {"has_phone": 0.0, "no_secret": 0.0, "from_vip": 0.0}, nxt["auto"]
    assert a.post("/api/queues", json={"name": f"rxbad-{SUF}", "rubric": [{"key": "x", "type": "regex", "pattern": "("}]}).status_code == 400
    assert a.post("/api/queues", json={"name": f"rxbad2-{SUF}", "rubric": [{"key": "x", "type": "regex", "pattern": "a", "field": "secrets"}]}).status_code == 400

# ------------------------------------------------------------------ spend caps still apply (6.1)
def test_evaluator_weekly_cap_blocks_openevals_judge(a, pid):
    eid = a.post("/api/evaluators/from_template", json={"template_id": "oe_correctness", "name": f"oe-cap-{SUF}"}).json()["id"]
    rid, _ = add_run(pid, out="x [judge-score:true]")
    assert a.post(f"/api/evaluators/{eid}/run_on/{rid}").status_code == 200
    with pg() as c:
        c.execute("INSERT INTO gateway_usage(ts,model,input_tokens,output_tokens,cost_usd,status,evaluator_id) VALUES(now(),'gemini-3.8-flash',0,0,1.2,'t76',%s)", (eid,))
    cap = a.post("/api/gateway/caps", json={"scope": "evaluator", "target": str(eid), "period": "week", "limit_usd": 1.0}); assert cap.status_code == 200, cap.text
    try:
        r = a.post(f"/api/evaluators/{eid}/run_on/{rid}"); assert r.status_code == 402 and "mỗi tuần" in r.json()["detail"], r.text
        r = a.post(f"/api/evaluators/{eid}/test", json={"run": {"input": "q", "output": "o"}}); assert r.status_code == 402, r.text
    finally: a.delete(f"/api/gateway/caps/{cap.json()['id']}")
    assert a.post(f"/api/evaluators/{eid}/run_on/{rid}").status_code == 200       # cap removed -> runs again

def test_existing_llm_and_code_evaluators_unchanged(a):
    ev = {e["name"]: e["id"] for e in a.get("/api/evaluators").json()}
    r = a.post(f"/api/evaluators/{ev['co_chu_so']}/test", json={"run": {"input": "q", "output": "co 3 so"}}); assert r.status_code == 200 and r.json()[0]["score"] == 1, r.text
