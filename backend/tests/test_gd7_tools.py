"""GD7.0 (22/10/2026): one Agent OS tool set (app/aos_tools.py) served over MCP at /mcp/aos. Tools run AS the caller (RLS + GĐ6.10 per-user traces).
Official `mcp` client over HTTP (stateless, JSON) with Basic auth or aos_ keys. Local server :8020, fake LLM => $0. Throwaway users/data."""
import asyncio, base64, json, os, time, uuid
import httpx, psycopg, pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from test_fixes import B, ADMIN, a  # noqa: F401  (fixtures)

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
PW = "phase0-gd70-local"; G = "agent-2"; SUF = uuid.uuid4().hex[:6]
URL = B + "/mcp/aos"
READ = {"query_runs", "get_trace", "get_thread", "list_issues", "get_prompt", "get_pack", "list_pack_versions", "get_costs", "list_datasets", "get_experiment", "compare_experiments"}
WRITE = {"propose_prompt_edit", "create_dataset_from_runs", "draft_evaluator", "run_experiment"}

def basic(u, p): return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}
OWNER = basic(*ADMIN)

async def _mcp(headers, name=None, args=None):
    async with httpx.AsyncClient(headers=headers, timeout=60) as hc, streamable_http_client(URL, http_client=hc) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            if name is None: return await s.list_tools()
            return await s.call_tool(name, args or {})

def tools(headers): return asyncio.run(_mcp(headers)).tools

def call(headers, tool, /, **args):
    """-> (is_error, text). Parsed JSON is in data(...)"""
    r = asyncio.run(_mcp(headers, tool, args))
    return r.is_error, "".join(c.text for c in r.content if getattr(c, "type", "") == "text")

def ok(headers, tool, /, **args):
    err, txt = call(headers, tool, **args); assert not err, txt
    return json.loads(txt)

def _user(a, name, role="editor"):
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": role})
    uid = r.json().get("id") if r.status_code == 200 else next(x["id"] for x in a.get("/api/admin/users").json() if x["username"] == name)
    return uid, httpx.Client(base_url=B, auth=(name, PW), timeout=90)

def chat(c, text):
    tid = c.post("/api/v1/threads", json={"metadata": {"source": "chat", "graph_id": G}}).json()["thread_id"]
    r = c.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": G, "input": {"messages": [{"type": "human", "content": text}]}}); assert r.status_code == 200, r.text
    return tid

@pytest.fixture(scope="module")
def world(a):
    ua, ca = _user(a, "gd70_a"); ub, cb = _user(a, "gd70_b"); uv, cv = _user(a, "gd70_viewer", "viewer")
    pid = next(p["id"] for p in a.get("/api/obs/projects").json() if p["name"] == "agents")
    w = {"pid": pid, "ca": ca, "ua": ua, "ha": basic("gd70_a", PW), "hb": basic("gd70_b", PW), "hv": basic("gd70_viewer", PW)}
    w["ta"] = chat(ca, f"gd70 question A {SUF}"); w["tb"] = chat(cb, f"gd70 question B {SUF}")
    time.sleep(1)
    with psycopg.connect(PG) as pg:
        rows = pg.execute("SELECT thread_id, id, trace_id FROM tr_runs WHERE thread_id IN (%s,%s) AND parent_id IS NULL", (w["ta"], w["tb"])).fetchall()
    w["run_a"] = next(str(r[1]) for r in rows if r[0] == w["ta"]); w["trace_a"] = next(str(r[2]) for r in rows if r[0] == w["ta"])
    w["run_b"] = next(str(r[1]) for r in rows if r[0] == w["tb"])
    w["prompt"] = f"gd70-prompt-{SUF}"
    assert a.post("/api/hub/commit", json={"name": w["prompt"], "template": "You are v1. {{q}}", "tag": "production", "message": "v1"}).status_code == 200
    k = lambda scope: a.post("/api/auth/keys", json={"name": "gd70", "scope": scope}).json()
    w["kread"], w["kfull"] = k("read"), k("full")
    yield w
    for x in (w["kread"], w["kfull"]): a.delete(f"/api/auth/keys/{x['id']}")
    for c, i in ((ca, ua), (cb, ub), (cv, uv)): c.close(); a.delete(f"/api/admin/users/{i}")
    with psycopg.connect(PG, autocommit=True) as pg:
        pg.execute("DELETE FROM prompts WHERE name LIKE %s", (f"gd70-prompt-{SUF}%",))
        pg.execute("DELETE FROM datasets WHERE name LIKE %s", (f"gd70-ds-{SUF}%",))
        pg.execute("DELETE FROM evaluators WHERE name LIKE %s", (f"gd70-ev-{SUF}%",))

# ------------------------------------------------------------------ MCP surface
def test_mcp_lists_the_whole_tool_set(world):
    ts = tools(OWNER)
    assert {t.name for t in ts} == READ | WRITE
    assert all(t.description and t.input_schema.get("properties") is not None for t in ts)

def test_mcp_requires_auth():
    assert httpx.post(URL, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"accept": "application/json, text/event-stream"}).status_code == 401

def test_api_key_works_and_read_key_can_read(world):
    for k in (world["kfull"], world["kread"]):
        h = {"X-Api-Key": k["key"]}
        assert {t.name for t in tools(h)} == READ | WRITE
        assert ok(h, "list_datasets")["total"] >= 0

# ------------------------------------------------------------------ read tools (owner = workspace admin)
def test_query_runs_get_trace_get_thread(world):
    d = ok(OWNER, "query_runs", project="agents", limit=50)
    assert d["returned"] >= 2 and any(i["thread_id"] == world["ta"] for i in d["items"]) and len(json.dumps(d)) < 8000
    d2 = ok(OWNER, "query_runs", project=str(world["pid"]), filter='eq(status,"error")', limit=5)
    assert all(i["status"] == "error" for i in d2["items"])
    assert call(OWNER, "query_runs", project="nope-project")[0]
    ms = [i["ms"] for i in ok(OWNER, "query_runs", project="agents", limit=20, sort="slowest")["items"] if i["ms"] is not None]
    assert ms == sorted(ms, reverse=True)   # GD7.R sort
    assert call(OWNER, "query_runs", project="agents", sort="bogus")[0]
    tr = ok(OWNER, "get_trace", trace_id=world["trace_a"])
    assert tr["returned"] >= 1 and tr["items"][0]["id"] and "feedback" in tr
    full = ok(OWNER, "get_trace", trace_id=world["trace_a"], span_id=tr["items"][0]["id"]); assert full["id"] == tr["items"][0]["id"]
    assert call(OWNER, "get_trace", trace_id="not-a-uuid")[0]
    th = ok(OWNER, "get_thread", thread_id=world["ta"])
    assert th["returned"] == 1 and str(world["trace_a"]) == th["items"][0]["trace_id"] and "gd70 question A" in th["items"][0]["in"]

def test_list_issues_prompt_costs_datasets(world):
    iss = ok(OWNER, "list_issues", project="agents"); assert "returned" in iss and isinstance(iss["items"], list)   # 7.4: persistent issues (tests/test_gd7_engine.py covers content)
    assert call(OWNER, "list_issues", status="nope")[0] and call(OWNER, "list_issues", kind="nope")[0]
    p = ok(OWNER, "get_prompt", name=world["prompt"])
    assert p["commit"] == 1 and p["template"] == "You are v1. {{q}}" and p["tags"] == {"production": 1}
    assert ok(OWNER, "get_prompt", name=world["prompt"], tag="production")["commit"] == 1
    assert call(OWNER, "get_prompt", name=world["prompt"], tag="nope")[0]
    for g in ("model", "user", "agent", "feature"):
        c = ok(OWNER, "get_costs", period="30d", group_by=g); assert c["group_by"] == g and "total_cost_usd" in c
    assert call(OWNER, "get_costs", group_by="bogus")[0] and call(OWNER, "get_costs", period="forever")[0]
    assert ok(OWNER, "list_datasets")["total"] >= 0

# ------------------------------------------------------------------ write tools + experiments
def test_write_tools_end_to_end(world, a):
    # propose_prompt_edit: a NEW commit, the production tag is untouched
    r = ok(world["ha"], "propose_prompt_edit", name=world["prompt"], new_template="You are v2, be brief. {{q}}", message="shorter")
    assert r["new_commit"] == 2 and r["parent_commit"] == 1 and r["tags_unchanged"] == {"production": 1}
    assert ok(OWNER, "get_prompt", name=world["prompt"], tag="production")["template"] == "You are v1. {{q}}"
    new = ok(OWNER, "get_prompt", name=world["prompt"]); assert new["commit"] == 2 and new["note"].startswith("[proposed via Agent OS tools]") and new["tags"] == {"production": 1}
    assert call(world["ha"], "propose_prompt_edit", name="gd70-no-such-prompt", new_template="x", message="m")[0]
    # create_dataset_from_runs: right examples, nothing created when a run is invisible
    ds = f"gd70-ds-{SUF}"
    d = ok(world["ha"], "create_dataset_from_runs", name=ds, run_ids=[world["run_a"]])
    assert d["version"] >= 1 and len(d["results"]) == 1
    got = a.get(f"/api/datasets/{d['dataset_id']}").json()["examples"]
    assert len(got) == 1 and "gd70 question A" in json.dumps(got[0]["inputs"]) and got[0]["source_run_id"] == world["run_a"]
    assert ok(world["ha"], "create_dataset_from_runs", name=ds, run_ids=[world["run_a"]])["results"] == ["đã có"]   # idempotent
    err, txt = call(world["hb"], "create_dataset_from_runs", name=f"{ds}-b", run_ids=[world["run_a"]])
    assert err and "not found" in txt.lower()
    assert not any(x["name"] == f"{ds}-b" for x in a.get("/api/datasets").json())
    # draft_evaluator
    e = ok(world["ha"], "draft_evaluator", name=f"gd70-ev-{SUF}", kind="llm", prompt="Q: {{input}} A: {{output}} Is it polite?")
    row = next(x for x in a.get("/api/evaluators").json() if x["id"] == e["evaluator_id"]); assert row["config"]["draft"] is True and row["kind"] == "llm"
    assert call(world["ha"], "draft_evaluator", name=f"gd70-ev-{SUF}x", kind="code", code="print(1)")[0]   # code must define evaluate()
    # run_experiment x2, then get_experiment / compare_experiments
    ids = []
    for i in range(2):
        r = ok(world["ha"], "run_experiment", dataset=ds, target_agent="2", name=f"gd70-exp-{SUF}-{i}"); assert r["status"] == "started"; ids.append(r["experiment_id"])
    for eid in ids:
        for _ in range(60):
            x = ok(OWNER, "get_experiment", id=eid)
            if x["status"] != "queued" and x["status"] != "running": break
            time.sleep(1)
        assert x["status"] == "done" and x["n"] == 1 and x["dataset"] == ds, x
    cmp = ok(OWNER, "compare_experiments", a=ids[0], b=ids[1])
    assert cmp["a"]["id"] == ids[0] and cmp["b"]["n"] == 1 and "score_delta_b_minus_a" in cmp
    assert call(OWNER, "get_experiment", id=999999)[0]
    for eid in ids: a.delete(f"/api/experiments/{eid}")

# ------------------------------------------------------------------ who can do what
def test_viewer_reads_but_cannot_write(world):
    assert ok(world["hv"], "get_prompt", name=world["prompt"])["commit"] >= 1
    n = ok(OWNER, "get_prompt", name=world["prompt"])["latest_commit"]
    err, txt = call(world["hv"], "propose_prompt_edit", name=world["prompt"], new_template="viewer edit", message="no")
    assert err and "write tools" in txt.lower()
    assert ok(OWNER, "get_prompt", name=world["prompt"])["latest_commit"] == n

def test_read_scope_key_cannot_call_write_tools(world):
    h = {"X-Api-Key": world["kread"]["key"]}
    n = ok(OWNER, "get_prompt", name=world["prompt"])["latest_commit"]
    for name, args in (("propose_prompt_edit", dict(name=world["prompt"], new_template="x", message="m")),
                       ("create_dataset_from_runs", dict(name=f"gd70-ds-{SUF}-ro", run_ids=[world["run_a"]])),
                       ("draft_evaluator", dict(name=f"gd70-ev-{SUF}-ro", kind="llm", prompt="p")),
                       ("run_experiment", dict(dataset="x", target_agent="2"))):
        err, txt = call(h, name, **args); assert err and "write tools" in txt.lower(), (name, txt)
    assert ok(OWNER, "get_prompt", name=world["prompt"])["latest_commit"] == n
    assert ok({"X-Api-Key": world["kfull"]["key"]}, "propose_prompt_edit", name=world["prompt"], new_template="full key edit", message="ok")["new_commit"] == n + 1   # full key can

# ------------------------------------------------------------------ isolation
def test_user_b_cannot_see_user_a_chat_traces_via_tools(world):
    a_items = ok(world["ha"], "query_runs", project="agents", limit=100)["items"]; b_items = ok(world["hb"], "query_runs", project="agents", limit=100)["items"]
    assert any(i["thread_id"] == world["ta"] for i in a_items) and not any(i["thread_id"] == world["ta"] for i in b_items)
    assert any(i["thread_id"] == world["tb"] for i in b_items) and not any(i["thread_id"] == world["tb"] for i in a_items)
    for name, args in (("get_trace", {"trace_id": world["trace_a"]}), ("get_thread", {"thread_id": world["ta"]})):
        assert call(world["hb"], name, **args)[0], name
        assert not call(world["ha"], name, **args)[0]
    assert not any(i["thread_id"] == world["ta"] for i in ok(world["hb"], "query_runs", project="agents", filter='search("gd70 question A")')["items"])

def test_demo_user_confined_to_its_workspace(world, a):
    dname = f"gd70_demo_{SUF}"
    uid = a.post("/api/admin/users", json={"username": dname, "password": PW, "role": "viewer", "demo": True, "daily_cap_usd": 0.05, "demo_agents": [2]}).json()["id"]
    p = a.post("/api/admin/demo-workspace", json={"user_id": uid, "name": "gd70 demo " + SUF, "source_agents": [2], "daily_cap_usd": 2.0}); assert p.status_code == 200, p.text
    try:
        h = basic(dname, PW)
        assert {t.name for t in tools(h)} == READ | WRITE
        # owner data (ws 1) is invisible: trace, thread, run, prompt, datasets, costs
        assert call(h, "get_trace", trace_id=world["trace_a"])[0] and call(h, "get_thread", thread_id=world["ta"])[0]
        assert call(h, "get_prompt", name=world["prompt"])[0]
        assert call(h, "query_runs", project="agents")[0] or not ok(h, "query_runs", project="agents")["items"]
        assert all(d["name"] != f"gd70-ds-{SUF}" for d in ok(h, "list_datasets")["items"])
        err, txt = call(h, "create_dataset_from_runs", name=f"gd70-ds-{SUF}-demo", run_ids=[world["run_a"]]); assert err
        err, txt = call(h, "propose_prompt_edit", name=world["prompt"], new_template="demo edit", message="no"); assert err
    finally:
        with psycopg.connect(PG, autocommit=True) as pg:
            pg.execute("UPDATE users SET disabled=true WHERE id=%s", (uid,))
            pg.execute("DELETE FROM lg_threads WHERE workspace_id=%s", (p.json()["workspace_id"],))

def test_skill_file_is_valid_frontmatter():
    import pathlib, re
    p = pathlib.Path(__file__).resolve().parents[1] / "app" / "skills" / "agentos" / "SKILL.md"
    s = p.read_text(encoding="utf-8"); m = re.match(r"^---\nname: agentos\ndescription: (.+)\n---\n", s)
    assert m and len(m[1]) < 1024
    for t in READ | WRITE: assert t in s, t

def test_identity_never_leaks_between_concurrent_requests(world):
    from concurrent.futures import ThreadPoolExecutor
    jobs = [("ha" if i % 2 == 0 else "hb") for i in range(16)]
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(lambda k: (k, call(world[k], "get_thread", thread_id=world["ta"])[0]), jobs))
    assert all(err == (k == "hb") for k, err in res)   # A always reads its thread, B never does
