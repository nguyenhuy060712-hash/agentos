"""GD9.Q part A (24/10/2026): close the LangSmith P2 Evaluation gaps - assertions (expect-style + dataset assertions), composite evaluators (SDK / UI config / online),
decision-model evaluators (typed questions, preference, gate; offline + online), dataset prebuilt JSON-schema types, dataset transformations, evaluate a graph (SDK + server),
RAG evaluation template, retry failed rows of an experiment. Local server :8020 + Postgres, fake LLM => $0. Pure parts (SDK, evalkit, evalds) run without the server.
Docs checked 24/10/2026: docs.langchain.com/langsmith/{assertions, composite-evaluators-sdk, composite-evaluators-ui, online-evaluations-composite, decision-model-evaluator,
online-evaluations-decision-models, dataset-json-types, dataset-transformations, evaluate-graph, evaluate-rag-tutorial, evaluate-with-retry}.
Run with a RAM limit: ulimit -v 4000000."""
import datetime as dt, json, os, sys, time, uuid
import httpx, psycopg, pytest
from psycopg.types.json import Jsonb
HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "app")); sys.path.insert(0, os.path.join(HERE, "..", "app", "sdk"))
from test_fixes import B, ADMIN, a  # noqa: F401,E402
import evalds, evalkit  # noqa: E402
import agentos_evals as sdk_ev  # noqa: E402

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
SUF = uuid.uuid4().hex[:6]
def pg(): return psycopg.connect(PG, autocommit=True)

# ================================================================== 1. assertions: SDK expect
def test_expect_value_edit_and_embedding_distance_are_logged_as_feedback():
    got = []
    x = sdk_ev.Expect(lambda k, s, c: got.append((k, s)))
    x.value("Paris is the capital").to_contain("Paris"); x.value(5).to_satisfy(lambda v: v > 3, "gt3"); x.value("abc123").to_match(r"\d+")
    x.edit_distance("kitten", "sitting").to_be_less_than(0.6)
    x.embedding_distance("giá gói Pro là 499k", "gói Pro giá 499k").to_be_less_than(0.4)
    x.embedding_distance("a", "b", config={"embed": lambda ts: [[1.0, 0.0] if t == "a" else [0.0, 1.0] for t in ts]}).to_be_greater_than(0.9)   # custom embedding model
    keys = [k for k, _ in got]
    assert keys.count("expectation") == 6 and "edit_distance" in keys and keys.count("embedding_distance") == 2 and all(s == 1.0 for k, s in got if k == "expectation")
    assert sdk_ev.edit_distance("kitten", "sitting") == 3 and sdk_ev.normalized_edit_distance("abc", "abc") == 0.0
    with pytest.raises(AssertionError, match="not in"): x.value("abc").to_contain("zzz")
    assert got[-1] == ("expectation", 0.0)                                   # the failure is logged BEFORE it raises
    with pytest.raises(AssertionError): x.embedding_distance("giá gói Pro", "thời tiết hôm nay").to_be_less_than(0.3)

def test_pytest_recorder_logs_expect_results(monkeypatch):
    import agentos_pytest as ap
    r = ap._Rec("tests/x.py::t")
    r.expect.value("xin chào").to_contain("chào"); r.expect.value(2).to_be_less_than(5); r.expect.edit_distance("abc", "abd")
    assert r.d["feedback"]["expectation"] == 1.0 and r.d["feedback"]["edit_distance"] == pytest.approx(0.3333, abs=1e-3)
    with pytest.raises(AssertionError): r.expect.value("a").to_equal("b")
    assert r.d["feedback"]["expectation"] == pytest.approx(0.6667, abs=1e-3)   # several expectations under one key = their mean
    r.log_feedback("correct", 1.0); r.log_feedback("tone", 0.5)
    assert r.composite("quality", keys=["correct", "tone"], weights={"correct": 3}, aggregate="sum") == 3.5
    assert r.composite("quality2", keys=["correct", "tone"], aggregate="formula", formula="0.7*correct + 0.3*tone") == pytest.approx(0.85)

# ================================================================== 2. composite: SDK + evalkit
def test_composite_average_sum_min_max_formula_and_safety():
    s = {"summary": 1.0, "tone": 0.5, "formatting": 0.0}
    w = {"summary": 0.7, "tone": 0.2, "formatting": 0.1}
    for mod in (evalkit, sdk_ev):                                             # server and SDK compute the same numbers
        assert mod.composite(s, w, "average") == pytest.approx(0.8)           # sum(w*s)/sum(w)
        assert mod.composite(s, w, "sum") == pytest.approx(0.8) and mod.composite(s, {"summary": 2}, "sum") == pytest.approx(2.5)
        assert mod.composite(s, aggregate="min") == 0.0 and mod.composite(s, aggregate="max") == 1.0 and mod.composite(s, aggregate="all_pass", pass_threshold=0.5) == 0.0
        assert mod.composite(s, aggregate="formula", formula="0.7*summary + 0.3*min(tone, formatting)") == pytest.approx(0.7)
        assert mod.composite({"a-b": 1.0, "c": 0.5}, aggregate="formula", formula='s["a-b"] * 2 - c') == pytest.approx(1.5)
        for bad in ("__import__('os').system('x')", "summary.__class__", "open('f')", "nope + 1", "[1][0]", "x" * 500):
            with pytest.raises(ValueError): mod.safe_formula(bad, s)
    assert evalkit.composite({}, None) is None

def test_sdk_evaluate_with_function_evaluators_and_composites():
    import agentos_sdk as ao
    data = [{"inputs": {"question": "q1"}, "outputs": {"answer": "Paris"}}, {"inputs": {"question": "q2"}, "outputs": {"answer": "Rome"}}]
    def target(inputs): return {"answer": "Paris" if inputs["question"] == "q1" else "Madrid"}
    def correct(outputs, reference_outputs): return outputs["answer"] == reference_outputs["answer"]
    def tone(inputs, outputs): return {"key": "tone", "score": 0.5}
    def fmt(outputs): return {"results": [{"key": "fmt", "score": 1}]}
    r = ao.evaluate(target, data, [correct, tone, fmt], composites=[{"key": "quality", "feedback_keys": ["correct", "tone", "fmt"], "weights": {"correct": 2}, "aggregate": "average"},
                                                                   {"key": "late", "feedback_keys": ["correct", "ghost"]}], upload=False)
    fb = [x["feedback"] for x in r["results"]]
    assert fb[0]["correct"] == 1.0 and fb[1]["correct"] == 0.0 and fb[0]["quality"] == pytest.approx(0.875) and fb[1]["quality"] == pytest.approx(0.375)
    assert all("late" not in f for f in fb)                                   # a composite needs every key (LangSmith)

# ================================================================== 6. evaluate a graph: SDK with a real LangGraph app
def _weather_graph():
    from typing import Annotated, TypedDict
    from langchain_core.messages import AIMessage, ToolMessage
    from langgraph.graph import END, START, StateGraph
    from langgraph.graph.message import add_messages
    class State(TypedDict):
        messages: Annotated[list, add_messages]
    def agent(state):
        last = state["messages"][-1]
        if isinstance(last, ToolMessage): return {"messages": [AIMessage(content=f"It is {last.content}")]}
        q = last.content if hasattr(last, "content") else last["content"]
        return {"messages": [AIMessage(content="", tool_calls=[{"name": "search", "args": {"query": q}, "id": "c1", "type": "tool_call"}])]}
    def tools(state):
        q = state["messages"][-1].tool_calls[0]["args"]["query"]
        return {"messages": [ToolMessage(content="60 degrees and foggy" if "sf" in q.lower() else "90 degrees and sunny", tool_call_id="c1", name="search")]}
    def route(state): return "tools" if getattr(state["messages"][-1], "tool_calls", None) else END
    g = StateGraph(State); g.add_node("agent", agent); g.add_node("tools", tools); g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route); g.add_edge("tools", "agent")
    return g.compile()

def test_sdk_evaluate_graph_end_to_end_trajectory_and_single_node():
    import agentos_sdk as ao
    app = _weather_graph()
    data = [{"inputs": {"question": "weather in sf"}, "outputs": {"answer": "60 degrees and foggy", "trajectory": ["search"]}},
            {"inputs": {"question": "weather in tangier"}, "outputs": {"answer": "90 degrees and sunny", "trajectory": ["search"]}}]
    def correct(outputs, reference_outputs): return reference_outputs["answer"] in outputs["answer"]                       # end-to-end: final answer
    def right_tool(outputs, trajectory): return [t["name"] for t in trajectory] == ["search"]                                # intermediate steps from the state
    def traj_strict(trajectory, reference_outputs): return sdk_ev.trajectory_match(trajectory, reference_outputs["trajectory"], "strict")
    r = ao.evaluate_graph(app, data, [correct, right_tool, traj_strict], upload=False)
    assert [x["feedback"]["correct"] for x in r["results"]] == [1.0, 1.0] and all(x["feedback"]["right_tool"] == 1.0 and x["feedback"]["traj_strict"] == 1.0 for x in r["results"])
    assert r["results"][0]["trajectory"] == [{"name": "search", "args": {"query": "weather in sf"}}] and not r["results"][0]["error"]
    # a single node alone ("agent" decides to call the tool): same examples, only that node runs
    def called_tool(trajectory): return len(trajectory) == 1 and trajectory[0]["name"] == "search"
    n = ao.evaluate_graph(app, data, [called_tool], node="agent", upload=False)
    assert all(x["feedback"]["called_tool"] == 1.0 for x in n["results"]), n["results"][0]
    bad = ao.evaluate_graph(app, [{"inputs": {"question": "x"}}], [correct], to_state=lambda i: (_ for _ in ()).throw(RuntimeError("boom")), upload=False)
    assert "boom" in bad["results"][0]["error"]                               # a failing example is recorded, the run goes on

def test_sdk_evaluate_graph_uploads_experiment_with_trajectory(a):
    import agentos_sdk as ao
    pid = a.post("/api/obs/projects", json={"name": f"g9q-sdk-{SUF}"}).json()["id"]
    key = a.post(f"/api/obs/projects/{pid}/keys").json()["api_key"]
    ao.configure(endpoint=B, api_key=key)
    ds = f"g9q-graph-{SUF}"
    try:
        r = ao.evaluate_graph(_weather_graph(), [{"inputs": {"question": "weather in sf"}, "outputs": {"answer": "60 degrees and foggy", "trajectory": ["search"]}}],
                              [lambda outputs, reference_outputs: reference_outputs["answer"] in outputs["answer"]], dataset=ds, experiment=f"graph-{SUF}")
        assert r["experiment_id"] and r["results"] == r["results"]
        # server side: the same experiment accepts a trajectory evaluator afterwards (child tool runs were created from `trajectory`)
        tid = a.post("/api/evaluators", json={"name": f"g9q-traj-{SUF}", "kind": "trajectory", "config": {"key": "traj", "mode": "strict"}}).json()["id"]
        out = a.post(f"/api/experiments/{r['experiment_id']}/evaluate", json={"evaluator_ids": [tid]}); assert out.status_code == 200, out.text
        sc = a.get("/api/experiments", params={"dataset_id": r["dataset_id"]}).json()[0]["summary"]["scores"]
        assert {s["key"]: float(s["avg"]) for s in sc}["traj"] == 1.0
    finally:
        with pg() as c:
            c.execute("DELETE FROM datasets WHERE name=%s", (ds,)); c.execute("DELETE FROM evaluators WHERE name=%s", (f"g9q-traj-{SUF}",))
        with pg() as c: c.execute("DELETE FROM projects WHERE id=%s", (pid,))

# ================================================================== 4/5. dataset types + transformations (pure)
def test_prebuilt_types_validate_with_the_bundled_langsmith_schemas():
    for k, t in evalds.DATASET_TYPES.items():
        for sch in (t["inputs_schema"], t["outputs_schema"]):
            if sch: evalds.check_schema(sch)                                    # $ref https://api.smith.langchain.com/public/schemas/v1/*.json resolve OFFLINE
        i, o = evalds.apply(t["example"]["inputs"], t["example"]["outputs"], t["transformations"], t["inputs_schema"], t["outputs_schema"])
        assert not (t["inputs_schema"] and evalds.problem(i, t["inputs_schema"])) and not (t["outputs_schema"] and evalds.problem(o, t["outputs_schema"])), k
    ct = evalds.DATASET_TYPES["chat_model"]
    assert evalds.problem({"messages": [{"role": "robot", "content": "x"}]}, ct["inputs_schema"]).startswith("/messages/0/role")
    assert "required" in evalds.problem({"tools": []}, ct["inputs_schema"])
    with pytest.raises(ValueError, match="unresolved"): evalds.check_schema({"properties": {"a": {"$ref": "https://example.com/x.json"}}})

def test_transformations_langsmith_path_form_and_ours():
    ct = evalds.DATASET_TYPES["chat_model"]
    i, o = evalds.apply({"messages": [{"type": "system", "content": "s"}, {"type": "human", "content": "hi"}], "junk": 1,
                         "tools": [{"name": "f", "description": "d", "parameters": {"type": "object", "properties": {}}}]}, {"message": {"type": "ai", "content": "yo"}, "x": 1},
                        ct["transformations"] + [{"path": ["inputs", "messages"], "transformation_type": "remove_system_messages"}], ct["inputs_schema"], ct["outputs_schema"])
    assert i == {"messages": [{"role": "user", "content": "hi"}], "tools": [{"type": "function", "function": {"name": "f", "description": "d", "parameters": {"type": "object", "properties": {}}}}]}
    assert o == {"message": {"role": "assistant", "content": "yo"}}                  # LangChain format -> OpenAI format, extra fields removed, system message dropped
    i2, _ = evalds.apply({"messages": [{"role": "assistant", "tool_calls": [{"id": "1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}], "content": ""},
                                       {"role": "user", "content": "x" * 50}]}, None,
                         [{"path": ["inputs"], "transformation_type": "extract_tools"}, {"path": ["inputs", "messages"], "transformation_type": "trim", "max_chars": 10, "max_messages": 1}])
    assert [t["function"]["name"] for t in i2["tools"]] == ["get_weather"] and i2["messages"] == [{"role": "user", "content": "xxxxxxxxxx…"}]
    assert evalds.apply({"A": " x "}, {"B": " y "}, ["lowercase_keys", "strip_strings"]) == ({"a": "x"}, {"b": "y"})   # legacy names still work
    with pytest.raises(ValueError): evalds.normalize([{"path": ["metadata"], "transformation_type": "trim"}])
    with pytest.raises(ValueError): evalds.normalize(["no_such"])

# ================================================================== server-side fixtures
@pytest.fixture(scope="module")
def cleanup():
    made = {"datasets": [], "evaluators": [], "projects": [], "rules": []}
    yield made
    with pg() as c:
        for r in made["rules"]: c.execute("DELETE FROM automation_rules WHERE id=%s", (r,))
        for p in made["projects"]:
            c.execute("DELETE FROM feedback WHERE trace_id IN (SELECT trace_id FROM tr_runs WHERE project_id=%s)", (p,)); c.execute("DELETE FROM tr_runs WHERE project_id=%s", (p,)); c.execute("DELETE FROM projects WHERE id=%s", (p,))
        for d in made["datasets"]: c.execute("DELETE FROM datasets WHERE id=%s", (d,))
        c.execute("DELETE FROM evaluators WHERE name LIKE %s", (f"%-g9q{SUF}%",))
        c.execute("DELETE FROM datasets WHERE name LIKE %s", (f"%-g9q{SUF}%",))

@pytest.fixture(scope="module")
def pid(cleanup):
    with pg() as c: p = c.execute("INSERT INTO projects(name) VALUES(%s) RETURNING id", (f"g9q-{SUF}",)).fetchone()[0]
    cleanup["projects"].append(p); return p

def add_run(pid, inp="Giá gói Pro?", out="Gói Pro 499k/tháng", spans=(), ago=60):
    """finished root chain run (+ child spans) -> (root id, span ids)"""
    rid = str(uuid.uuid4()); ids = []; t0 = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=ago)
    with pg() as c:
        c.execute("INSERT INTO tr_runs(id,trace_id,project_id,name,run_type,start_time,end_time,status,inputs,outputs,metadata) VALUES(%s,%s,%s,'chat','chain',%s,%s,'success',%s,%s,%s)",
                  (rid, rid, pid, t0, t0 + dt.timedelta(seconds=2), Jsonb({"message": inp}), Jsonb({"answer": out}), Jsonb({})))
        for i, (name, typ, i_, o_, par) in enumerate(spans):
            sid = str(uuid.uuid4()); ids.append(sid)
            c.execute("INSERT INTO tr_runs(id,trace_id,parent_id,project_id,name,run_type,start_time,status,inputs,outputs) VALUES(%s,%s,%s,%s,%s,%s,%s,'success',%s,%s)",
                      (sid, rid, ids[par] if par is not None else rid, pid, name, typ, t0 + dt.timedelta(milliseconds=100 * (i + 1)), Jsonb(i_), Jsonb(o_)))
    return rid, ids

def fb(run_id, key=None):
    with pg() as c:
        rows = c.execute("SELECT key, score, value, comment, source FROM feedback WHERE run_id=%s AND (%s::text IS NULL OR key=%s) ORDER BY id", (run_id, key, key)).fetchall()
    return rows

def mk_ev(a, cleanup, name, kind, cfg):
    r = a.post("/api/evaluators", json={"name": f"{name}-g9q{SUF}", "kind": kind, "config": cfg}); assert r.status_code == 200, r.text
    cleanup["evaluators"].append(r.json()["id"]); return r.json()["id"]

def mk_ds(a, cleanup, name, **kw):
    r = a.post("/api/datasets", json={"name": f"{name}-g9q{SUF}", **kw}); assert r.status_code == 200, r.text
    cleanup["datasets"].append(r.json()["id"]); return r.json()["id"]

def wait_exp(a, did, xid, secs=150):
    for _ in range(secs):
        x = next((e for e in a.get("/api/experiments", params={"dataset_id": did}).json() if e["id"] == xid), None)
        if x and x["status"] in ("done",) or (x and x["status"].startswith("lỗi")): return x
        time.sleep(1)
    raise AssertionError(f"experiment {xid} did not finish")

# ================================================================== 1. assertions on the server
def test_assertion_evaluator_distances_and_custom_code(a, cleanup, pid):
    rid, _ = add_run(pid, out="Gói Pro giá 499k mỗi tháng")
    ex = {"outputs": {"answer": "Gói Pro 499k/tháng"}}
    eid = mk_ev(a, cleanup, "asr", "assertion", {"key": "close", "all_or_nothing": False, "assertions": [
        {"type": "edit_distance_max", "value": 0.5}, {"type": "embedding_distance_max", "value": 0.5},
        {"type": "custom", "code": "def check(output, reference, run, example):\n    return '499k' in str(output) and reference['answer'].startswith('Gói')\n"},
        {"type": "contains", "value": "499k"}]})
    r = a.post(f"/api/evaluators/{eid}/test", json={"run": {"input": "q", "output": "Gói Pro giá 499k mỗi tháng"}, "example": ex}); assert r.status_code == 200, r.text
    by = {x["key"]: x for x in r.json()}
    assert by["close"]["score"] == 1.0 and 0 <= by["edit_distance"]["score"] < 0.5 and 0 <= by["embedding_distance"]["score"] <= 0.5, r.json()   # the distances themselves are feedback too
    far = a.post(f"/api/evaluators/{eid}/test", json={"run": {"input": "q", "output": "thời tiết hôm nay rất đẹp ở Hà Nội"}, "example": ex}).json()
    f = {x["key"]: x for x in far}; assert f["close"]["score"] < 1.0 and "edit_distance_max" in f["close"]["comment"] and "embedding_distance_max" in f["close"]["comment"] and "custom" in f["close"]["comment"]
    assert f["edit_distance"]["score"] > 0.5 and f["embedding_distance"]["score"] > 0.5
    r = a.post(f"/api/evaluators/{eid}/run_on/{rid}"); assert r.status_code == 200
    keys = {k for k, *_ in fb(rid)}; assert {"close", "edit_distance", "embedding_distance"} <= keys, keys   # logged as feedback on the run
    for bad in ({"type": "edit_distance_max", "value": 5}, {"type": "custom", "code": "x=1"}):
        assert a.post("/api/evaluators", json={"name": f"bad-g9q{SUF}", "kind": "assertion", "config": {"key": "k", "assertions": [bad]}}).status_code == 400

def test_dataset_assertions_from_run_and_evaluator(a, cleanup, pid):
    """LangSmith "Use assertions": claims saved as the example's reference outputs, one feedback score per claim."""
    rid, _ = add_run(pid, inp="Giá gói Pro?", out="Gói Pro 499k/tháng, xem https://example.com/gia")
    did = mk_ds(a, cleanup, "asr-ds")
    assert a.post(f"/api/datasets/{did}/from_runs", json={"run_ids": [rid], "assertions": []}).status_code == 400
    r = a.post(f"/api/datasets/{did}/from_runs", json={"run_ids": [rid], "assertions": [
        {"key": "must_cite_source", "comment": "The response cites the URL it draws from."}, {"key": "must_not_invent_url", "comment": "No URL that is not in the inputs."},
        {"key": "has_price", "comment": "mentions the price", "check": {"type": "contains", "value": "499k"}}]}); assert r.status_code == 200, r.text
    ex = a.get(f"/api/datasets/{did}").json()["examples"][0]
    assert ex["outputs"] == {"assertions": [{"key": "must_cite_source", "comment": "The response cites the URL it draws from."}, {"key": "must_not_invent_url", "comment": "No URL that is not in the inputs."},
                                            {"key": "has_price", "comment": "mentions the price", "check": {"type": "contains", "value": "499k"}}]}   # the actual output is NOT stored
    eid = mk_ev(a, cleanup, "asr-ref", "assertion", {"key": "assertions_met", "from_reference": True})
    good = a.post(f"/api/evaluators/{eid}/test", json={"run": {"input": "q", "output": "Gói Pro 499k, nguồn https://example.com/gia"}, "example": {"outputs": ex["outputs"]}}).json()
    s = {x["key"]: x["score"] for x in good}; assert s == {"must_cite_source": 1.0, "must_not_invent_url": 1.0, "has_price": 1.0, "assertions_met": 1.0}, good
    bad = a.post(f"/api/evaluators/{eid}/test", json={"run": {"input": "q", "output": "Gói Pro rẻ lắm [assert:must_not_invent_url=false]"}, "example": {"outputs": ex["outputs"]}}).json()
    s = {x["key"]: x["score"] for x in bad}; assert s["must_not_invent_url"] == 0.0 and s["has_price"] == 0.0 and s["must_cite_source"] == 1.0 and s["assertions_met"] == pytest.approx(0.3333, abs=1e-3), bad   # partial credit
    none = a.post(f"/api/evaluators/{eid}/test", json={"run": {"input": "q", "output": "x"}, "example": {"outputs": {"answer": "x"}}}).json()
    assert none[0]["score"] is None and "no outputs.assertions" in none[0]["comment"]

# ================================================================== 2. composite evaluators on the server
def test_composite_over_feedback_keys_offline_and_validation(a, cleanup, pid):
    rid, _ = add_run(pid)
    e1 = mk_ev(a, cleanup, "c-sum", "assertion", {"key": "summary", "assertions": [{"type": "contains", "value": "499k"}]})
    e2 = mk_ev(a, cleanup, "c-tone", "assertion", {"key": "tone", "assertions": [{"type": "contains", "value": "xin lỗi"}]})      # fails -> 0
    comp = mk_ev(a, cleanup, "c-avg", "composite", {"key": "weighted_summary", "feedback_keys": ["summary", "tone"], "weights": {"summary": 0.7, "tone": 0.3}, "aggregate": "average"})
    summ = mk_ev(a, cleanup, "c-sumagg", "composite", {"key": "weighted_sum", "feedback_keys": ["summary", "tone"], "weights": {"summary": 3, "tone": 1}, "aggregate": "sum"})
    form = mk_ev(a, cleanup, "c-form", "composite", {"key": "custom_formula", "feedback_keys": ["summary", "tone"], "aggregate": "formula", "formula": "0.8*summary + 0.2*tone"})
    r = a.post(f"/api/evaluators/{comp}/run_on/{rid}"); assert r.status_code == 200 and r.json()[0]["score"] is None and "summary, tone" in r.json()[0]["comment"], r.text   # keys not there yet
    assert fb(rid, "weighted_summary") == []                                   # nothing is written while a key is missing (LangSmith: not calculated)
    for e in (e1, e2): assert a.post(f"/api/evaluators/{e}/run_on/{rid}").status_code == 200
    out = {k: a.post(f"/api/evaluators/{e}/run_on/{rid}").json()[0] for k, e in (("avg", comp), ("sum", summ), ("form", form))}
    assert out["avg"]["score"] == pytest.approx(0.7) and out["sum"]["score"] == pytest.approx(3.0) and out["form"]["score"] == pytest.approx(0.8), out
    assert "summary=1" in out["avg"]["comment"] and [r_[1] for r_ in fb(rid, "weighted_summary")] == [0.7]
    for bad in ({"feedback_keys": [], "aggregate": "average"}, {"feedback_keys": ["a"], "aggregate": "median"}, {"feedback_keys": ["a"], "aggregate": "formula", "formula": "__import__('os')"},
                {"feedback_keys": ["x"], "aggregate": "average"}):
        r = a.post("/api/evaluators", json={"name": f"badc-g9q{SUF}", "kind": "composite", "config": {"key": "x", **bad}}); assert r.status_code == 400, (bad, r.text)
    old = mk_ev(a, cleanup, "c-old", "composite", {"key": "legacy", "evaluator_ids": [e1, e2], "aggregate": "min"})      # the old evaluator_ids form still works
    assert a.post(f"/api/evaluators/{old}/run_on/{rid}").json()[0]["score"] == 0.0

def test_composite_on_an_experiment_after_the_fact(a, cleanup, pid):
    """SDK-style flow of the docs: run the experiment, then add composite feedback computed from the stored scores."""
    did = mk_ds(a, cleanup, "comp-exp"); a.post(f"/api/datasets/{did}/examples", json=[{"inputs": {"question": f"hello {i}"}, "outputs": {"answer": "x"}} for i in range(2)])
    e1 = mk_ev(a, cleanup, "ce-a", "assertion", {"key": "has_echo", "assertions": [{"type": "contains", "value": "Echo"}]})
    e2 = mk_ev(a, cleanup, "ce-b", "assertion", {"key": "short", "assertions": [{"type": "max_length", "value": 5}]})
    xid = a.post("/api/experiments", json={"name": f"g9q-comp-{SUF}", "dataset_id": did, "target": {"type": "agent", "agent_id": 2, "version": 1}, "evaluator_ids": [e1, e2]}).json()["id"]
    wait_exp(a, did, xid)
    r = a.post(f"/api/experiments/{xid}/composite", json={"key": "blend", "feedback_keys": ["has_echo", "short"], "weights": {"has_echo": 0.7, "short": 0.3}, "aggregate": "sum"}); assert r.status_code == 200, r.text
    assert r.json() == {"key": "blend", "scored": 2, "skipped": 0}
    sc = {s["key"]: float(s["avg"]) for s in a.get("/api/experiments", params={"dataset_id": did}).json()[0]["summary"]["scores"]}; assert sc["blend"] == pytest.approx(0.7)
    assert a.post(f"/api/experiments/{xid}/composite", json={"key": "blend", "feedback_keys": ["has_echo", "short"], "aggregate": "min"}).json()["scored"] == 2   # re-run replaces
    sc = {s["key"]: (float(s["avg"]), s["n"]) for s in a.get("/api/experiments", params={"dataset_id": did}).json()[0]["summary"]["scores"]}; assert sc["blend"] == (0.0, 2)
    assert a.post(f"/api/experiments/{xid}/composite", json={"key": "blend2", "feedback_keys": ["has_echo", "ghost"]}).json() == {"key": "blend2", "scored": 0, "skipped": 2}
    # bound to the dataset: a composite listed AFTER its children in the experiment runs last, even when listed first
    comp = mk_ev(a, cleanup, "ce-comp", "composite", {"key": "auto_blend", "feedback_keys": ["has_echo", "short"], "aggregate": "average"})
    assert a.patch(f"/api/datasets/{did}/config", json={"evaluator_ids": [comp, e1, e2]}).status_code == 200
    x2 = a.post("/api/experiments", json={"name": f"g9q-comp2-{SUF}", "dataset_id": did, "target": {"type": "agent", "agent_id": 2, "version": 1}}).json()["id"]; wait_exp(a, did, x2)
    sc = {s["key"]: float(s["avg"]) for e in a.get("/api/experiments", params={"dataset_id": did}).json() if e["id"] == x2 for s in e["summary"]["scores"]}
    assert sc["auto_blend"] == pytest.approx(0.5) and sc["has_echo"] == 1.0 and sc["short"] == 0.0

def test_composite_online_rule_runs_after_its_children(a, cleanup, pid):
    """Online: one rule on a tracing project with [child evaluators..., composite]; the loop runs the composite last whatever the listed order (LangSmith online composite)."""
    e1 = mk_ev(a, cleanup, "on-a", "assertion", {"key": "on_price", "assertions": [{"type": "contains", "value": "499k"}]})
    e2 = mk_ev(a, cleanup, "on-b", "assertion", {"key": "on_len", "assertions": [{"type": "max_length", "value": 500}]})
    comp = mk_ev(a, cleanup, "on-comp", "composite", {"key": "on_quality", "feedback_keys": ["on_price", "on_len"], "weights": {"on_price": 2}, "aggregate": "average"})
    dec = mk_ev(a, cleanup, "on-dec", "decision", {"key": "on_decision", "questions": [{"name": "on_helpful", "type": "predicate", "instructions": "Is the answer helpful?", "pass_min": 0.5}]})
    rid, _ = add_run(pid, out="Gói Pro 499k/tháng [decide:on_helpful=0.8]", ago=30)
    r = a.post("/api/obs/rules", json={"project_id": pid, "name": f"g9q-online-{SUF}", "filter": None, "sampling_rate": 1.0, "backfill_hours": 1,
                                       "actions": [{"type": "run_evaluator", "evaluator_id": comp}, {"type": "run_evaluator", "evaluator_id": e1}, {"type": "run_evaluator", "evaluator_id": e2},
                                                   {"type": "run_evaluator", "evaluator_id": dec}]}); assert r.status_code == 200, r.text
    cleanup["rules"].append(r.json()["id"])
    for _ in range(150):
        if fb(rid, "on_quality") and fb(rid, "on_helpful"): break
        time.sleep(1)
    assert [x[1] for x in fb(rid, "on_quality")] == [1.0], fb(rid)           # composite found both children although it was listed first
    assert [x[1] for x in fb(rid, "on_helpful")] == [0.8] and all(x[4] == "online" for x in fb(rid))

def test_composite_only_rule_is_processed_after_the_rule_of_its_children(a, cleanup, pid):
    """Two separate rules (composite rule created FIRST): the loop runs the children's rule first within a cycle, so the composite finds every key."""
    e1 = mk_ev(a, cleanup, "o2-a", "assertion", {"key": "o2_price", "assertions": [{"type": "contains", "value": "499k"}]})
    e2 = mk_ev(a, cleanup, "o2-b", "assertion", {"key": "o2_len", "assertions": [{"type": "max_length", "value": 500}]})
    comp = mk_ev(a, cleanup, "o2-comp", "composite", {"key": "o2_quality", "feedback_keys": ["o2_price", "o2_len"], "aggregate": "sum"})
    rid, _ = add_run(pid, out="Gói Pro 499k/tháng", ago=20)
    for nm, acts in (("comp", [comp]), ("kids", [e1, e2])):
        r = a.post("/api/obs/rules", json={"project_id": pid, "name": f"g9q-o2-{nm}-{SUF}", "sampling_rate": 1.0, "backfill_hours": 1, "filter": f"eq(id, \"{rid}\")", "actions": [{"type": "run_evaluator", "evaluator_id": i} for i in acts]})
        assert r.status_code == 200, r.text; cleanup["rules"].append(r.json()["id"])
    for _ in range(150):
        if fb(rid, "o2_quality"): break
        time.sleep(1)
    assert [x[1] for x in fb(rid, "o2_quality")] == [2.0], fb(rid)

# ================================================================== 3. decision-model evaluators
def test_decision_model_typed_questions_each_become_a_feedback_key(a, cleanup, pid):
    cfg = {"key": "triage", "state": "Input: {{input}}\nOutput: {{output}}", "questions": [
        {"name": "pii", "type": "predicate", "instructions": "Does the output contain personal data?", "true_means": "PII present", "false_means": "no PII", "pass_min": 0.5},
        {"name": "intent", "type": "choice", "instructions": "What does the user want?", "options": [{"name": "question", "description": "asks something"}, {"name": "request"}, {"name": "other"}]},
        {"name": "frustration", "type": "score", "instructions": "How frustrated is the user?", "levels": ["calm", "annoyed", "angry", "furious"]}]}
    eid = mk_ev(a, cleanup, "dec", "decision", cfg)
    r = a.post(f"/api/evaluators/{eid}/test", json={"run": {"input": "tôi muốn đổi trả", "output": "ok [decide:pii=0.2] [decide:intent=request] [decide:frustration=2.5]"}}); assert r.status_code == 200, r.text
    by = {x["key"]: x for x in r.json()}
    assert by["pii"]["score"] == pytest.approx(0.2) and "[FAIL]" in by["pii"]["comment"]                     # predicate: probability 0..1; gate pass_min 0.5
    assert by["intent"]["value"] == "request" and by["frustration"]["score"] == pytest.approx(2.5)           # choice: value; score: 0..levels-1
    d = a.post(f"/api/evaluators/{eid}/test", json={"run": {"input": "x", "output": "plain"}}).json()
    assert {x["key"]: x for x in d}["intent"]["value"] == "question" and {x["key"]: x for x in d}["frustration"]["score"] == 0   # fake model defaults
    rid, _ = add_run(pid, out="ok [decide:pii=0.9] [decide:intent=other] [decide:frustration=9]")
    assert a.post(f"/api/evaluators/{eid}/run_on/{rid}").status_code == 200                                   # online path = same call
    rows = {k: (s, v) for k, s, v, *_ in fb(rid)}; assert rows["pii"][0] == pytest.approx(0.9) and rows["intent"][1] == "other" and rows["frustration"][0] == 3.0   # clamped to the top level
    for bad in ([], [{"name": "a", "type": "choice", "instructions": "x", "options": [{"name": "only"}]}], [{"name": "a", "type": "score", "instructions": "x", "levels": ["one"]}],
                [{"name": "a", "type": "predicate", "instructions": "x"}, {"name": "a", "type": "predicate", "instructions": "y"}], [{"name": "a b", "type": "predicate", "instructions": "x"}],
                [{"name": "a", "type": "magic", "instructions": "x"}]):
        assert a.post("/api/evaluators", json={"name": f"badd-g9q{SUF}", "kind": "decision", "config": {"key": "k", "questions": bad}}).status_code == 400, bad
    leg = mk_ev(a, cleanup, "dec-legacy", "decision", {"key": "esc", "prompt": "Escalate? {{output}}", "choices": ["escalate", "resolved"], "scores": {"escalate": 0, "resolved": 1}})   # old form kept
    assert a.post(f"/api/evaluators/{leg}/test", json={"run": {"input": "q", "output": "o"}}).status_code == 200

def test_decision_model_preference_pairwise_and_gate(a, cleanup, pid):
    """`preference` = pairwise judge (is the run better than the reference / the previous answer?), position swapped per run; `pass_min` makes a pass/fail gate on the experiment."""
    eid = mk_ev(a, cleanup, "pref", "decision", {"key": "better_than_ref", "against": "reference", "questions": [
        {"name": "better", "type": "preference", "instructions": "Which answer is more helpful and accurate?", "pass_min": 0.5}]})
    ref = {"outputs": {"answer": "an old weak answer"}}
    win = a.post(f"/api/evaluators/{eid}/test", json={"run": {"input": "q", "output": "great detailed answer [better]"}, "example": ref}).json()[0]
    lose = a.post(f"/api/evaluators/{eid}/test", json={"run": {"input": "q", "output": "meh"}, "example": {"outputs": {"answer": "the reference is [better]"}}}).json()[0]
    tie = a.post(f"/api/evaluators/{eid}/test", json={"run": {"input": "q", "output": "same"}, "example": ref}).json()[0]
    assert (win["value"], win["score"]) == ("output", 1.0) and (lose["value"], lose["score"]) == ("other", 0.0) and (tie["value"], tie["score"]) == ("tie", 0.5)
    none = a.post(f"/api/evaluators/{eid}/test", json={"run": {"input": "q", "output": "x"}}).json()
    assert none[0]["score"] is None and "no answer to compare" in none[0]["comment"]
    # online: compare with what the agent answered last time to the same input
    prev = mk_ev(a, cleanup, "pref-prev", "decision", {"key": "vs_previous", "against": "previous_run", "questions": [{"name": "improved", "type": "preference", "instructions": "Is answer A or B better?"}]})
    q = f"câu hỏi trùng {SUF}"; add_run(pid, inp=q, out="old answer", ago=3600); new, _ = add_run(pid, inp=q, out="new answer [better]", ago=10)
    r = a.post(f"/api/evaluators/{prev}/run_on/{new}"); assert r.status_code == 200 and r.json()[0]["value"] == "output", r.text
    assert fb(new, "improved")[0][2] == "output"
    # gate on an experiment: avg of `better` must reach pass_min
    did = mk_ds(a, cleanup, "gate"); a.post(f"/api/datasets/{did}/examples", json=[{"inputs": {"question": "hello"}, "outputs": {"answer": "[better] reference"}}])
    xid = a.post("/api/experiments", json={"name": f"g9q-gate-{SUF}", "dataset_id": did, "target": {"type": "agent", "agent_id": 2, "version": 1}, "evaluator_ids": [eid]}).json()["id"]
    x = wait_exp(a, did, xid); g = x["summary"]["gate"]
    assert g["passed"] is False and g["checks"] == [{"key": "better", "min": 0.5, "avg": 0.0, "passed": False}], g      # the reference was better than the agent echo
    assert a.get("/api/experiments", params={"dataset_id": did}).json()[0]["summary"]["gate"]["passed"] is False

# ================================================================== 4/5. dataset types + transformations on the server
def test_dataset_types_api_validation_and_transformations(a, cleanup, pid):
    types = {t["id"]: t for t in a.get("/api/dataset_types").json()}
    assert {"kv", "chat_model", "chat", "qa", "tool_calling", "rag"} <= set(types) and types["chat_model"]["inputs_schema"]["properties"]["messages"]["items"]["$ref"].endswith("/message.json")
    assert a.post("/api/datasets", json={"name": f"x-g9q{SUF}", "dataset_type": "nope"}).status_code == 400
    did = mk_ds(a, cleanup, "chat", dataset_type="chat")
    d = a.get(f"/api/datasets/{did}").json()["dataset"]; assert d["dataset_type"] == "chat" and d["inputs_schema"] and d["transformations"]
    r = a.post(f"/api/datasets/{did}/examples", json={"inputs": {"messages": [{"type": "system", "content": "you are x"}, {"type": "human", "content": "Giá gói Pro?"}]},
                                                       "outputs": {"message": {"type": "ai", "content": "499k"}}}); assert r.status_code == 200, r.text
    ex = a.get(f"/api/datasets/{did}").json()["examples"][0]
    assert ex["inputs"] == {"messages": [{"role": "user", "content": "Giá gói Pro?"}]} and ex["outputs"] == {"message": {"role": "assistant", "content": "499k"}}   # system removed, OpenAI format
    bad = a.post(f"/api/datasets/{did}/examples", json={"inputs": {"messages": [{"role": "robot", "content": "x"}]}, "outputs": {"message": {"role": "assistant", "content": "y"}}})
    assert bad.status_code == 400 and "/messages/0/role" in bad.json()["detail"], bad.text                                                                   # validation names the field
    assert a.post(f"/api/datasets/{did}/examples", json={"inputs": {"foo": 1}, "outputs": {"message": {"role": "assistant", "content": "y"}}}).status_code == 400   # no messages and nothing to build them from
    assert a.post(f"/api/datasets/{did}/examples", json={"inputs": {"question": "from a plain question"}, "outputs": {"answer": "ok"}}).status_code == 200            # LangSmith: the message is taken from a well-known field
    plain = [e for e in a.get(f"/api/datasets/{did}").json()["examples"] if "plain" in json.dumps(e["inputs"])][0]
    assert plain["inputs"] == {"messages": [{"role": "user", "content": "from a plain question"}]} and plain["outputs"] == {"message": {"role": "assistant", "content": "ok"}}
    assert a.put(f"/api/examples/{ex['id']}", json={"inputs": {"messages": []}, "outputs": {"message": {"role": "assistant", "content": "y"}}}).status_code == 400   # edit validates too
    # qa type: CSV import is validated like any add (bad row -> 400, nothing stored)
    qa = mk_ds(a, cleanup, "qa", dataset_type="qa")
    assert a.post(f"/api/datasets/{qa}/import_csv", json={"csv": "q,a\n  Giá?  ,499k\n", "input_cols": ["q"], "output_cols": ["a"]}).status_code == 400   # keys q/a are not question/answer
    assert a.post(f"/api/datasets/{qa}/import_csv", json={"csv": "question,answer\n  Giá?  ,  499k \n", "input_cols": ["question"], "output_cols": ["answer"]}).status_code == 200
    assert a.get(f"/api/datasets/{qa}").json()["examples"][0]["inputs"] == {"question": "Giá?"}                   # strip_strings from the type
    # runs from tracing follow the dataset schema (chat shape built from the plain question/answer); a run that cannot fit is skipped with the reason
    rid, _ = add_run(pid, inp="Xin chào", out="Chào anh"); r = a.post(f"/api/datasets/{did}/from_runs", json={"run_ids": [rid]}).json()["results"][0]; assert r.startswith("example #"), r
    got = [e for e in a.get(f"/api/datasets/{did}").json()["examples"] if e["source_run_id"]][0]
    assert got["inputs"] == {"messages": [{"role": "user", "content": "Xin chào"}]} and got["outputs"] == {"message": {"role": "assistant", "content": "Chào anh"}}
    tool = mk_ds(a, cleanup, "tools", dataset_type="tool_calling")
    assert a.post(f"/api/datasets/{tool}/from_runs", json={"run_ids": [add_run(pid, inp="thời tiết", out="nắng")[0]]}).json()["results"][0].startswith("bỏ qua"), "outputs.tool_calls is required"
    # change the schema of a dataset that already has data: examples stay (as LangSmith), the count of non-matching ones is reported
    p = a.patch(f"/api/datasets/{qa}/config", json={"inputs_schema": {"type": "object", "properties": {"question": {"type": "string", "minLength": 50}}, "required": ["question"]}}); assert p.status_code == 200
    assert p.json()["invalid_examples"] == 1
    assert a.patch(f"/api/datasets/{qa}/config", json={"inputs_schema": {"properties": {"a": {"$ref": "https://example.com/x.json"}}}}).status_code == 400
    assert a.patch(f"/api/datasets/{qa}/config", json={"transformations": [{"path": ["nope"], "transformation_type": "trim"}]}).status_code == 400
    # configurable per dataset: LangSmith path form + our trim / extract_tools
    cfgd = mk_ds(a, cleanup, "cfg")
    assert a.patch(f"/api/datasets/{cfgd}/config", json={"transformations": ["lowercase_keys", {"path": ["inputs", "text"], "transformation_type": "trim", "max_chars": 5}]}).status_code == 200
    a.post(f"/api/datasets/{cfgd}/examples", json={"inputs": {"TEXT": "abcdefghij"}})
    assert a.get(f"/api/datasets/{cfgd}").json()["examples"][0]["inputs"] == {"text": "abcde…"}
    assert a.patch(f"/api/datasets/{cfgd}/config", json={"dataset_type": "kv"}).json()["transformations"] == []     # back to free form

# ================================================================== 6. evaluate a graph on the server (agent = LangGraph app) with trajectory + node-level evaluators
def test_evaluate_a_graph_experiment_with_trajectory_and_node_evaluators(a, cleanup, pid):
    info = a.get("/api/graph_eval/4").json()
    assert "researcher" in info["subagents"] and "sub-agent: researcher" in info["nodes"] and "content_publish" in info["tools"]
    did = mk_ds(a, cleanup, "graph", dataset_type="tool_calling")
    r = a.post(f"/api/datasets/{did}/examples", json=[{"inputs": {"messages": [{"role": "user", "content": "delegate write the weekly report"}]}, "outputs": {"tool_calls": [{"name": "task"}], "trajectory": ["task"]}},
                                                      {"inputs": {"messages": [{"role": "user", "content": "just say hi"}]}, "outputs": {"tool_calls": [], "trajectory": []}}]); assert r.status_code == 200, r.text
    traj = mk_ev(a, cleanup, "g-traj", "trajectory", {"key": "trajectory_ok", "mode": "strict"})                       # reference = example outputs.trajectory
    node = mk_ev(a, cleanup, "g-node", "assertion", {"key": "task_ran", "assertions": [{"type": "no_error"}], "target_node": "sub-agent: researcher"})   # scores only that sub-agent's span
    e2e = mk_ev(a, cleanup, "g-e2e", "assertion", {"key": "answered", "assertions": [{"type": "min_length", "value": 1}]})
    xid = a.post("/api/experiments", json={"name": f"g9q-graph-{SUF}", "dataset_id": did, "target": {"type": "agent", "agent_id": 4, "version": 1}, "evaluator_ids": [traj, node, e2e]}).json()["id"]
    x = wait_exp(a, did, xid, 180); sc = {s["key"]: s for s in x["summary"]["scores"]}
    assert x["status"] == "done" and x["summary"]["errors"] == 0, x
    assert float(sc["trajectory_ok"]["avg"]) == 1.0 and sc["trajectory_ok"]["n"] == 2 and float(sc["answered"]["avg"]) == 1.0, sc            # delegate -> [task]; hi -> []
    assert sc["task_ran"]["n"] == 1                                            # node-level: only the example that reached the sub-agent got a score
    cmp = a.get("/api/experiments/compare", params={"ids": str(xid)}).json()
    spans = {f["key"] for r_ in cmp["results"] for f in (r_["fb"] or [])}; assert {"trajectory_ok", "task_ran", "answered"} <= spans

# ================================================================== 7. RAG evaluation template
def test_rag_template_one_click(a, cleanup):
    had = {e["name"] for e in a.get("/api/evaluators").json()}
    try: _rag_template_checks(a, cleanup)
    finally:   # the template creates four shared evaluators; remove the ones this test created
        with pg() as c: c.execute("DELETE FROM evaluators WHERE name = ANY(%s)", ([n for n in ("rag_correctness", "rag_answer_relevance", "rag_groundedness", "rag_retrieval_relevance") if n not in had],))

def _rag_template_checks(a, cleanup):
    r = a.post("/api/rag_template", json={"agent_id": 2, "version": 1, "name": f"rag-g9q{SUF}"}); assert r.status_code == 200, r.text
    t = r.json(); cleanup["datasets"].append(t["dataset_id"])
    d = a.get(f"/api/datasets/{t['dataset_id']}").json()
    assert d["dataset"]["dataset_type"] == "rag" and len(d["examples"]) == 3 and "contexts" in d["examples"][0]["outputs"] and sorted(d["dataset"]["evaluator_ids"]) == sorted(t["evaluator_ids"])
    evs = {e["id"]: e for e in a.get("/api/evaluators").json()}
    assert sorted(evs[i]["config"]["template"] for i in t["evaluator_ids"]) == ["answer_relevance", "correctness", "rag_groundedness", "rag_retrieval_relevance"]
    x = wait_exp(a, t["dataset_id"], t["experiment_id"], 180); keys = {s["key"] for s in x["summary"]["scores"]}
    assert {"correctness", "answer_relevance", "rag_groundedness", "rag_retrieval_relevance"} <= keys and x["summary"]["n"] == 3, x["summary"]
    r2 = a.post("/api/rag_template", json={"agent_id": 2, "name": f"rag-g9q{SUF}", "run": False, "examples": [{"question": "Q?", "answer": "A."}]}).json(); cleanup["datasets"].append(r2["dataset_id"])
    assert r2["dataset_name"] == f"rag-g9q{SUF}-2" and r2["experiment_id"] is None and r2["evaluator_ids"] == t["evaluator_ids"]       # names stay unique, evaluators are reused
    assert a.post("/api/rag_template", json={"agent_id": 2, "run": False, "examples": [{"question": "only a question"}]}).status_code == 400
    assert a.post("/api/rag_template", json={"agent_id": 999999}).status_code == 404

# ================================================================== 8. retry failed rows
def test_retry_only_the_failed_rows_of_an_experiment(a, cleanup):
    did = mk_ds(a, cleanup, "retry"); a.post(f"/api/datasets/{did}/examples", json=[{"inputs": {"question": f"retry {i}"}, "outputs": {"answer": "x"}} for i in range(3)])
    ev = mk_ev(a, cleanup, "retry-ev", "assertion", {"key": "retry_ok", "assertions": [{"type": "no_error"}, {"type": "contains", "value": "Echo"}]})
    xid = a.post("/api/experiments", json={"name": f"g9q-retry-{SUF}", "dataset_id": did, "target": {"type": "agent", "agent_id": 2, "version": 1}, "evaluator_ids": [ev]}).json()["id"]
    wait_exp(a, did, xid)
    assert a.post(f"/api/experiments/{xid}/retry").status_code == 400                                              # nothing failed yet
    with pg() as c:
        rows = c.execute("SELECT id, example_id, run_id FROM exp_results WHERE experiment_id=%s ORDER BY id", (xid,)).fetchall(); assert len(rows) == 3
        c.execute("UPDATE exp_results SET error='rate limit (simulated)', output=NULL WHERE id=%s", (rows[1][0],))
        c.execute("DELETE FROM exp_results WHERE id=%s", (rows[2][0],)); c.execute("DELETE FROM feedback WHERE run_id=%s", (str(rows[2][2]),))   # an interrupted row: no result, no score
        c.execute("UPDATE feedback SET score=0, comment='stale' WHERE experiment_id=%s AND run_id=%s", (xid, str(rows[1][2])))
    before_ok = rows[0][2]
    r = a.post(f"/api/experiments/{xid}/retry"); assert r.status_code == 200 and r.json()["retrying"] == 2, r.text
    for _ in range(150):
        x = next(e for e in a.get("/api/experiments", params={"dataset_id": did}).json() if e["id"] == xid)
        if x["status"] == "done" and x["summary"]["n"] == 3: break
        time.sleep(1)
    with pg() as c:
        after = c.execute("SELECT example_id, run_id, error FROM exp_results WHERE experiment_id=%s ORDER BY example_id", (xid,)).fetchall()
        retries = c.execute("SELECT retries FROM experiments WHERE id=%s", (xid,)).fetchone()[0]
        n_fb = c.execute("SELECT count(*) FROM feedback WHERE experiment_id=%s AND key='retry_ok'", (xid,)).fetchone()[0]
    assert len(after) == 3 and all(e is None for _, _, e in after) and retries == 1
    run_of = {ex_: str(run_) for ex_, run_, _ in after}
    assert run_of[rows[0][1]] == str(before_ok), "the row that already succeeded is untouched"
    assert run_of[rows[1][1]] != str(rows[1][2]), "the failed row has a fresh run"
    assert n_fb == 3 and float(x["summary"]["scores"][0]["avg"]) == 1.0 and x["summary"]["errors"] == 0           # the stale score of the failed run is gone, every row re-scored
    assert a.post(f"/api/experiments/{xid}/retry").status_code == 400
    assert a.post("/api/experiments/999999/retry").status_code == 404
    assert a.post(f"/api/experiments/{xid}/retry_failed").json() == {"retrying": 0}                                 # the older endpoint name is the same implementation
