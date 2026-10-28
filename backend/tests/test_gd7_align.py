"""GD7.5 (23/10/2026): judge alignment (app/align.py). 30 labelled runs; the fake judge (overlay/fake_llm.fake_judge) is LENIENT (always true) unless the openevals few-shot examples
in its prompt show the same topic with a score - so few-shot examples change its answers predictably. Human labels: topics T1,T2 pass, T3,T4,T5 fail -> agreement 0.40 before quick align,
1.00 after. Local server :8020, fake LLM => $0. Tests run in file order and share state through the `env` fixture."""
import json, os, sys, uuid
import httpx, psycopg, pytest
from psycopg.types.json import Jsonb
from test_fixes import B, ADMIN, VIEWER, a  # noqa: F401  (fixtures)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
SUF = uuid.uuid4().hex[:6]
KEY = f"corr{SUF}"
PASS_TOPICS = ("T1", "T2")
def pg(): return psycopg.connect(PG, autocommit=True)

def mkev(a, name, key=KEY):
    r = a.post("/api/evaluators/from_template", json={"template_id": "oe_correctness", "name": f"{name}-{SUF}", "overrides": {"key": key}}); assert r.status_code == 200, r.text
    return r.json()["id"]

@pytest.fixture(scope="module")
def env(a):
    e = {}
    with pg() as c:
        e["pid"] = c.execute("INSERT INTO projects(name) VALUES(%s) RETURNING id", (f"align-{SUF}",)).fetchone()[0]
        e["runs"] = []
        for t in range(1, 6):
            for i in range(1, 7):
                rid = str(uuid.uuid4()); tag = f"topic:T{t}"
                c.execute("INSERT INTO tr_runs(id,trace_id,project_id,name,run_type,start_time,status,inputs,outputs) VALUES(%s,%s,%s,'chat','chain',now() - interval '1 hour','success',%s,%s)",
                          (rid, rid, e["pid"], Jsonb({"message": f"Câu hỏi [judge-align] {tag} mẫu {i}"}), Jsonb({"answer": f"Trả lời [judge-align] {tag} mẫu {i}"})))
                e["runs"].append((rid, f"T{t}"))
    e["ev"] = mkev(a, "align")
    yield e
    with pg() as c:
        c.execute("DELETE FROM feedback WHERE trace_id IN (SELECT trace_id FROM tr_runs WHERE project_id=%s)", (e["pid"],))
        c.execute("DELETE FROM tr_runs WHERE project_id=%s", (e["pid"],))
        c.execute("DELETE FROM evaluators WHERE name LIKE %s", (f"%-{SUF}",))
        c.execute("DELETE FROM annotation_queues WHERE name LIKE %s", (f"%-{SUF}",))
        c.execute("DELETE FROM gateway_usage WHERE status='t75'")
        c.execute("DELETE FROM projects WHERE id=%s", (e["pid"],))

def label(rid, topic, key=KEY):
    with pg() as c:
        c.execute("INSERT INTO feedback(run_id,trace_id,key,score,source,created_by) VALUES(%s,%s,%s,%s,'annotation','reviewer')", (rid, rid, key, 1.0 if topic in PASS_TOPICS else 0.0))

# ------------------------------------------------------------------ pure metrics
def test_cohens_kappa_known_table():
    import alignkit as align
    pairs = [("y", "y")] * 20 + [("y", "n")] * 5 + [("n", "y")] * 10 + [("n", "n")] * 15       # po = 0.70, pe = (25*30 + 25*20) / 50^2 = 0.50 -> kappa = 0.40
    assert align.cohens_kappa(pairs) == pytest.approx(0.4)
    m = align.metrics([{"human": a, "judge": b} for a, b in pairs])
    assert m["mode"] == "categorical" and m["accuracy"] == pytest.approx(0.7) and m["kappa"] == pytest.approx(0.4)
    assert align.cohens_kappa([("a", "a"), ("b", "b"), ("c", "c"), ("a", "a")]) == 1.0
    assert align.cohens_kappa([("a", "b"), ("b", "a")]) == -1.0
    assert align.cohens_kappa([("a", "a"), ("a", "a")]) is None                              # single label on both sides: undefined
    assert align.cohens_kappa([(1.0, 1.0)] * 12 + [(0.0, 1.0)] * 18) == pytest.approx(0.0)   # judge says "pass" to everything -> chance level

def test_continuous_metrics_correlation_and_mae():
    import alignkit as align
    P = lambda h, j: [{"human": x, "judge": y} for x, y in zip(h, j)]
    m = align.metrics(P([1, 2, 3, 4, 5], [1, 2, 3, 4, 5]), (1.0, 5.0)); assert m["mode"] == "continuous" and m["correlation"] == 1.0 and m["mae"] == 0 and m["agreement"] == 1.0
    m = align.metrics(P([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]), (1.0, 5.0)); assert m["correlation"] == -1.0 and m["mae"] == pytest.approx(0.6)   # |diff| = 4,2,0,2,4 over a range of 4 -> mean 0.6
    m = align.metrics(P([1, 3, 5], [2, 3, 4]), (1.0, 5.0)); assert m["mae"] == pytest.approx((0.25 + 0 + 0.25) / 3, abs=1e-3) and m["correlation"] == 1.0
    m = align.metrics(P([0.2, 0.9], [0.2, 0.9]), (0.0, 1.0)); assert m["agreement"] == 1.0
    assert align.metrics([])["n"] == 0

def test_pick_diverse_covers_topics_and_prefers_recent():
    import alignkit as align
    mk = lambda fid, t: {"fid": fid, "input": f"q topic:{t} sample {fid}", "output": f"ans topic:{t} sample {fid}"}
    dis = [mk(i, "alpha") for i in range(1, 7)] + [mk(i, "beta") for i in range(7, 13)] + [mk(i, "gamma") for i in range(13, 19)]
    ch = align.pick_diverse(dis, 6)
    assert ch[0]["fid"] == 18 and len(ch) == 6 and len({c["fid"] for c in ch}) == 6 and {c["input"].split()[1] for c in ch[:3]} == {"topic:alpha", "topic:beta", "topic:gamma"}
    assert len(align.pick_diverse(dis[:2], 6)) == 2 and align.pick_diverse([], 6) == []

def test_example_shapes_for_both_judge_kinds():
    import alignkit as align
    p = {"human": 0.0, "judge": 1.0, "comment": "sai giá"}
    e = align.example({"kind": "openevals", "config": {}}, p, "in", "out"); assert e["score"] is False and e["inputs"] == "in" and "sai giá" in e["reasoning"] and e["_align"]
    e = align.example({"kind": "openevals", "config": {"continuous": True}}, {**p, "human": 0.25}, "in", "out"); assert e["score"] == 0.25
    e = align.example({"kind": "llm", "config": {}}, p, "in", "out"); assert e["điểm_đúng_do_người_sửa"] == 0.0 and e["ghi_chú"] == "sai giá"      # same shape as the use_corrections few-shot

# ------------------------------------------------------------------ endpoints
def test_alignment_needs_labels_then_reports_pairs(a, env):
    r = a.get(f"/api/evaluators/{env['ev']}/alignment"); assert r.status_code == 200 and r.json()["n_human"] == 0 and r.json()["metrics"]["agreement"] is None
    r = a.post(f"/api/evaluators/{env['ev']}/align/quick"); assert r.status_code == 422 and "Need at least" in r.json()["detail"], r.text
    # 27 labels straight in the DB, 3 through the real annotation-queue endpoint (T1, T3, T5 runs)
    q = a.post("/api/queues", json={"name": f"alq-{SUF}", "rubric": [{"key": KEY, "type": "boolean"}]}).json()["id"]
    via_queue = {env["runs"][0][0]: True, env["runs"][12][0]: False, env["runs"][24][0]: False}
    for rid, topic in env["runs"]:
        if rid not in via_queue: label(rid, topic)
    a.post(f"/api/queues/{q}/items", json={"run_ids": list(via_queue)})
    for _ in via_queue:
        it = a.get(f"/api/queues/{q}/next").json()["item"]; v = via_queue[str(it["run_id"])]
        assert a.post(f"/api/queue_items/{it['id']}", json={"scores": {KEY: v}}).status_code == 200
    d = a.get(f"/api/evaluators/{env['ev']}/alignment").json(); assert d["n_human"] == 30 and d["n_pairs"] == 0 and d["n_unjudged"] == 30

def test_before_metrics_after_judging_the_labelled_runs(a, env):
    for rid, _ in env["runs"]: assert a.post(f"/api/evaluators/{env['ev']}/run_on/{rid}").status_code == 200
    d = a.get(f"/api/evaluators/{env['ev']}/alignment").json()
    assert d["n_pairs"] == 30 and d["mode"] == "categorical" and d["alignable"] and d["version"] == 1 and not d["can_revert"]
    m = d["metrics"]; assert m["accuracy"] == pytest.approx(0.4) and m["kappa"] == pytest.approx(0.0) and m["agreement"] == pytest.approx(0.4)
    assert len(d["disagreements"]) == 18 and all(x["human"] == 0.0 and x["judge"] == 1.0 for x in d["disagreements"])
    assert all(x["input"].startswith("Câu hỏi") for x in d["disagreements"])

def test_quick_align_raises_agreement_and_saves_a_new_version(a, env):
    e = env["ev"]; before_cfg = next(x for x in a.get("/api/evaluators").json() if x["id"] == e)["config"]
    r = a.post(f"/api/evaluators/{e}/align/quick"); assert r.status_code == 200, r.text; x = r.json()
    assert x["changed"] and 3 <= x["examples"] <= 6 and x["version"] == 2
    assert x["before"]["agreement"] == pytest.approx(0.4) and x["after"]["agreement"] == pytest.approx(1.0) and x["improved"]
    assert x["before"]["kappa"] == pytest.approx(0.0) and x["after"]["kappa"] == pytest.approx(1.0)
    assert x["held_out_after"]["agreement"] == pytest.approx(1.0) and x["held_out_after"]["n"] == 30 - x["examples"]            # also right on runs that were NOT used as examples
    topics = {t for rid, t in env["runs"] if rid in x["example_run_ids"]}; assert topics == {"T3", "T4", "T5"}               # diverse: every failing topic is covered
    cfg = next(y for y in a.get("/api/evaluators").json() if y["id"] == e)["config"]
    few = cfg["few_shot_examples"]; assert len(few) == x["examples"] and all(f["score"] is False and f["inputs"] and f["outputs"] and f["_align"] for f in few)
    assert {k: v for k, v in cfg.items() if k != "few_shot_examples"} == {k: v for k, v in before_cfg.items() if k != "few_shot_examples"}
    d = a.get(f"/api/evaluators/{e}/alignment").json(); assert d["version"] == 2 and d["can_revert"] and d["metrics"]["agreement"] == pytest.approx(1.0) and d["disagreements"] == []
    vs = a.get(f"/api/evaluators/{e}/versions").json(); assert [v["version"] for v in vs] == [2, 1] and vs[0]["metrics"]["after"]["agreement"] == pytest.approx(1.0)

def test_revert_restores_config_and_judge_scores(a, env):
    e = env["ev"]
    r = a.post(f"/api/evaluators/{e}/align/revert"); assert r.status_code == 200, r.text; x = r.json()
    assert x["version"] == 3 and x["restored_scores"] == 30 and x["metrics"]["agreement"] == pytest.approx(0.4)
    cfg = next(y for y in a.get("/api/evaluators").json() if y["id"] == e)["config"]; assert not cfg.get("few_shot_examples")
    d = a.get(f"/api/evaluators/{e}/alignment").json(); assert d["version"] == 3 and len(d["disagreements"]) == 18
    # keep / re-align after a revert works again (the earlier auto examples are replaced, not stacked)
    x = a.post(f"/api/evaluators/{e}/align/quick").json(); assert x["changed"] and x["version"] == 4 and x["after"]["agreement"] == pytest.approx(1.0)
    cfg = next(y for y in a.get("/api/evaluators").json() if y["id"] == e)["config"]; assert len(cfg["few_shot_examples"]) == x["examples"]
    assert a.post(f"/api/evaluators/{e}/align/quick").json()["changed"] is False                                           # already agrees everywhere -> nothing to do

def test_revert_without_history_is_409(a, env):
    e2 = mkev(a, "fresh", key=KEY)
    assert a.post(f"/api/evaluators/{e2}/align/revert").status_code == 409

def test_viewer_can_read_but_not_align(a, env):
    with httpx.Client(base_url=B, auth=VIEWER, timeout=60) as v:
        assert v.get(f"/api/evaluators/{env['ev']}/alignment").status_code == 200
        assert v.get(f"/api/evaluators/{env['ev']}/versions").status_code == 200
        assert v.post(f"/api/evaluators/{env['ev']}/align/quick").status_code == 403
        assert v.post(f"/api/evaluators/{env['ev']}/align/revert").status_code == 403

def test_only_llm_judges_and_enough_labels(a, env):
    code = next(x["id"] for x in a.get("/api/evaluators").json() if x["name"] == "co_chu_so")
    assert a.post(f"/api/evaluators/{code}/align/quick").status_code == 400
    e3 = mkev(a, "nolabels", key=f"nolabel{SUF}")
    assert a.post(f"/api/evaluators/{e3}/align/quick").status_code == 422

def test_evaluator_weekly_cap_blocks_quick_align_and_changes_nothing(a, env):
    e2 = mkev(a, "capped", key=KEY)                          # same key -> same 30 human labels, but this judge has scored nothing yet
    with pg() as c: c.execute("INSERT INTO gateway_usage(ts,model,input_tokens,output_tokens,cost_usd,status,evaluator_id) VALUES(now(),'gemini-3.8-flash',0,0,1.2,'t75',%s)", (e2,))
    cap = a.post("/api/gateway/caps", json={"scope": "evaluator", "target": str(e2), "period": "week", "limit_usd": 1.0}); assert cap.status_code == 200, cap.text
    try:
        r = a.post(f"/api/evaluators/{e2}/align/quick"); assert r.status_code == 402 and "mỗi tuần" in r.json()["detail"], r.text
        assert a.get(f"/api/evaluators/{e2}/versions").json() == []
        assert not next(y for y in a.get("/api/evaluators").json() if y["id"] == e2)["config"].get("few_shot_examples")
    finally: a.delete(f"/api/gateway/caps/{cap.json()['id']}")
    # a feature cap named 'align' also stops it (the judge calls of an alignment count under feature 'align')
    with pg() as c: c.execute("INSERT INTO gateway_usage(ts,model,input_tokens,output_tokens,cost_usd,status,feature) VALUES(now(),'gemini-3.8-flash',0,0,1.2,'t75','align')")
    cap = a.post("/api/gateway/caps", json={"scope": "feature", "target": "align", "period": "week", "limit_usd": 1.0}); assert cap.status_code == 200, cap.text
    try:
        r = a.post(f"/api/evaluators/{e2}/align/quick"); assert r.status_code == 402, r.text
    finally: a.delete(f"/api/gateway/caps/{cap.json()['id']}")
    assert a.post(f"/api/evaluators/{e2}/align/quick").status_code == 200                      # caps removed -> it runs
