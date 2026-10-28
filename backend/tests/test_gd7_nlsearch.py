"""GD7.2 (22/10/2026): natural-language trace/thread search (app/nlsearch.py, POST /api/obs/nl-filter).
The fake LLM (overlay/fake_llm.py) answers a question carrying "[spec] {json}" with that FilterSpec as a structured-output tool call
("[spec2] {json}" after the endpoint's FILTER_ERROR retry message), so the gold set (tests/data/nl_search_gold.jsonl, 20 VI + 20 EN) checks
FilterSpec -> DSL rendering + the existing parser end to end: 40/40. Local server :8020, $0, no network."""
import json, os, re, uuid
from pathlib import Path
import httpx, psycopg, pytest
B = "http://127.0.0.1:8020"; ADMIN = ("moza", "phase0-local"); VIEWER = ("viewer1", "phase0-viewer-local")
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
SUF = uuid.uuid4().hex[:6]; DU = ("demonl" + SUF, "demo-nl-pass-" + SUF)
GOLD = [json.loads(x) for x in (Path(__file__).parent / "data" / "nl_search_gold.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
ISO = re.compile(r'"\d{4}-\d\d-\d\dT[^"]*"')
KEYS = {"filter", "explanation", "fields_used", "since_hours", "mode", "sort"}

@pytest.fixture(scope="module")
def a():
    with httpx.Client(base_url=B, auth=ADMIN, timeout=90) as c: yield c

@pytest.fixture(scope="module")
def pid(a):
    r = a.post("/api/obs/projects", json={"name": f"gd72-{SUF}"}); assert r.status_code == 200, r.text
    yield r.json()["id"]
    with psycopg.connect(PG, autocommit=True) as c: c.execute("DELETE FROM projects WHERE id=%s", (r.json()["id"],))

def ask(c, pid, spec, question="test", spec2=None, **kw):
    q = f"{question} [spec] {json.dumps(spec, ensure_ascii=False)}" + (f" [spec2] {json.dumps(spec2)}" if spec2 else "")
    return c.post("/api/obs/nl-filter", json={"project_id": pid, "question": q, **kw})

def test_gold_set_shape():
    assert len(GOLD) == 40 and sum(g["lang"] == "vi" for g in GOLD) == 20 and sum(g["lang"] == "en" for g in GOLD) == 20

@pytest.mark.parametrize("g", GOLD, ids=lambda g: f"{g['id']:02d}-{g['lang']}")
def test_gold_question_renders_to_expected_dsl(a, pid, g):
    r = ask(a, pid, g["spec"], g["question"], lang=g["lang"]); assert r.status_code == 200, r.text
    j = r.json(); assert set(j) == KEYS
    assert ISO.sub('"{T}"', j["filter"]) == g["dsl"]
    assert j["since_hours"] == g["spec"]["since_hours"] and j["mode"] == g["spec"]["mode"] and j["sort"] == g["spec"]["sort"]
    assert j["explanation"] == g["spec"]["explanation"] and all(isinstance(x, str) for x in j["fields_used"])
    # the generated filter is accepted by the real run/thread queries (the endpoint itself ran none)
    ok = a.get("/api/obs/runs", params={"project_id": pid, "filter": j["filter"], "sort": j["sort"] or "newest"}); assert ok.status_code == 200, ok.text
    ok = a.get(f"/api/obs/projects/{pid}/threads", params={"filter": j["filter"]}); assert ok.status_code == 200, ok.text

def test_fields_used_and_time(a, pid):
    spec = {"conditions": [{"field": "metadata", "op": "eq", "value": "x", "key": "env"}, {"field": "latency", "op": "gt", "value": "2"}], "since_hours": 48, "until_hours": 24}
    assert ask(a, pid, spec).json()["fields_used"] == ["metadata.env", "latency", "start_time"]

def test_invalid_spec_gets_one_retry_with_the_error(a, pid):
    bad = {"conditions": [{"field": "metadata", "op": "eq", "value": "x"}]}   # metadata without key -> cannot be rendered
    good = {"conditions": [{"field": "metadata", "op": "eq", "value": "x", "key": "env"}]}
    r = ask(a, pid, bad, spec2=good); assert r.status_code == 200, r.text
    assert r.json()["filter"] == 'eq(metadata.env,"x")'
    r = ask(a, pid, {"conditions": [{"field": "latency", "op": "gt", "value": "fast"}]}, spec2=good); assert r.status_code == 200   # bad number -> retried too
    r = ask(a, pid, bad); assert r.status_code == 422 and "valid filter" in r.json()["detail"]                                  # retry fails as well
    r = ask(a, pid, {"conditions": [{"field": "bogus", "op": "eq", "value": "x"}]}, spec2=good); assert r.status_code == 200      # schema violation -> retried

def test_parser_error_also_retries(a, pid):
    bad = {"conditions": [{"field": "metadata", "op": "eq", "value": "x", "key": "a b"}]}   # renders fine but the parser rejects the key
    r = ask(a, pid, bad, spec2={"conditions": [{"field": "name", "op": "eq", "value": "n"}]}); assert r.status_code == 200, r.text
    assert r.json()["filter"] == 'eq(name,"n")'

def test_endpoint_validates_input_and_project(a, pid):
    assert a.post("/api/obs/nl-filter", json={"project_id": pid, "question": "x"}).status_code == 422
    assert a.post("/api/obs/nl-filter", json={"project_id": 99999999, "question": "errors today"}).status_code == 404
    assert a.post("/api/obs/nl-filter", json={"project_id": pid, "question": "errors today", "lang": "fr"}).status_code == 422

def test_works_on_a_populated_project(a):
    p = next((x for x in a.get("/api/obs/projects").json() if x["traces"] > 0), None)
    if not p: pytest.skip("no populated project")
    r = ask(a, p["id"], {"conditions": [{"field": "status", "op": "eq", "value": "error"}], "since_hours": 24}); assert r.status_code == 200, r.text   # catalogue SQL on real data

def test_feature_cap_gives_friendly_402(a, pid):
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute("INSERT INTO gateway_usage(ts,model,input_tokens,output_tokens,cost_usd,status,feature) VALUES(now(),'gemini-3.8-flash',0,0,0.5,'t72','search')")
    cap = a.post("/api/gateway/caps", json={"scope": "feature", "target": "search", "period": "total", "limit_usd": 0.01}); assert cap.status_code == 200, cap.text
    try:
        r = ask(a, pid, {"conditions": []}); assert r.status_code == 402 and r.json()["detail"].startswith("Đã chạm trần chi phí tính năng Tìm trace"), r.text
    finally:
        a.delete(f"/api/gateway/caps/{cap.json()['id']}")
        with psycopg.connect(PG, autocommit=True) as c: c.execute("DELETE FROM gateway_usage WHERE status='t72'")
    assert ask(a, pid, {"conditions": []}).status_code == 200   # cap removed

def test_viewer_may_use_it_read_only(a, pid):
    with httpx.Client(base_url=B, auth=VIEWER, timeout=90) as v:
        if v.get("/api/auth/me").status_code != 200: pytest.skip("viewer1 not available")
        assert v.post("/api/obs/projects", json={"name": f"gd72v-{SUF}"}).status_code == 403   # viewers still cannot write
        r = ask(v, pid, {"conditions": [{"field": "status", "op": "eq", "value": "error"}]}); assert r.status_code == 200, r.text
        assert r.json()["filter"] == 'eq(status,"error")'

def test_demo_user_only_in_own_workspace(a, pid):
    r = a.post("/api/admin/users", json={"username": DU[0], "password": DU[1], "role": "viewer", "demo": True, "daily_cap_usd": 0.05, "demo_agents": [2]}); assert r.status_code == 200, r.text
    uid = r.json()["id"]
    p = a.post("/api/admin/demo-workspace", json={"user_id": uid, "name": "Demo nl " + SUF, "source_agents": [2], "daily_cap_usd": 2.0}); assert p.status_code == 200, p.text
    ws = p.json()["workspace_id"]
    with psycopg.connect(PG, autocommit=True) as c:
        mine = c.execute("INSERT INTO projects(name, workspace_id) VALUES(%s,%s) RETURNING id", (f"demo-nl-{SUF}", ws)).fetchone()[0]
    try:
        with httpx.Client(base_url=B, auth=DU, timeout=90) as d:
            r = ask(d, mine, {"conditions": [{"field": "status", "op": "eq", "value": "error"}]}); assert r.status_code == 200, r.text
            assert ask(d, pid, {"conditions": []}).status_code == 404                              # the owner's project: not visible
            assert d.post("/api/obs/projects", json={"name": "nope" + SUF}).status_code == 403     # demo stays read-only otherwise
    finally:
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute("DELETE FROM projects WHERE id=%s", (mine,)); c.execute("UPDATE users SET disabled=true WHERE id=%s", (uid,))

def test_runs_sort_and_threads_filter_validate(a, pid):
    assert a.get("/api/obs/runs", params={"project_id": pid, "sort": "bogus"}).status_code == 400
    assert a.get(f"/api/obs/projects/{pid}/threads", params={"filter": "nope("}).status_code == 400

def test_fallback_tries_a_different_model_after_a_503(monkeypatch):
    """GD7.R (real Gemini): DEFAULT_MODEL equals DEFAULT_FALLBACKS[0]; the second try must be another model, not the same overloaded one."""
    import sys, asyncio
    sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
    os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")   # deepagent_api reads it at import; no call is made here
    import deepagent_api  # noqa: F401  (first: nlsearch and deepagent_api import each other)
    import nlsearch, gateway as gw
    from langchain_core.messages import AIMessage
    tried = []
    class M:
        def __init__(s, n): s.n = n
        def with_structured_output(s, schema, include_raw=True): return s
        async def ainvoke(s, msgs):
            tried.append(s.n)
            if len(tried) == 1: raise RuntimeError("503 UNAVAILABLE. This model is currently experiencing high demand.")
            return {"raw": AIMessage(content="", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}), "parsed": nlsearch.FilterSpec(), "parsing_error": None}
    monkeypatch.setattr(gw, "_model", lambda name, temperature=None: M(name))
    monkeypatch.setattr(gw, "USAGE_SINK", [])
    spec, err = asyncio.run(nlsearch._ask(["sys", "q"]))
    assert spec is not None and err is None and len(tried) == 2 and tried[0] != tried[1], tried

def test_all_default_spec_is_retried(a, pid):
    """GD7.R: an all-default FilterSpec ({} from Gemini) is not accepted as "no filter": the model is asked again."""
    r = ask(a, pid, {}, spec2={"conditions": [{"field": "status", "op": "eq", "value": "error"}], "explanation": "lỗi"})
    assert r.status_code == 200 and r.json()["filter"] == 'eq(status,"error")', r.text
    assert ask(a, pid, {"conditions": [], "explanation": "tất cả"}).status_code == 200   # a real "show everything" stays valid
