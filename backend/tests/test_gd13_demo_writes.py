"""GĐ13.Z step 3: the demo account (and a caller of ANOTHER workspace) can not write through any NEW GĐ13 route, and nothing changes in the database.
The demo leak audit only probes GET routes with id "1"; this file calls every write route (engine issues / settings / projects, insights configs / reports / settings,
shared memory, retention, scheduler crons, agent PUT with `middleware` / `response_format`) with REAL ids: the ids of a non-demo workspace (w1) AND the demo's own ids.
Expected: 401/403/404 (never 2xx, never 5xx, never 422 = the request must be refused before it is looked at), and a fingerprint of the touched tables is identical afterwards.
Also: a demo GET of another workspace's issue / config / report / memory / cron by real id is 404/403 and the foreign marker never shows in a list.
13.28 schedule tools (agent_schedules.refusal) are checked at unit level. Local stack, fake LLM ($0).
Server: AOS_TEST_BASE (default the shared :8020, must run the current code). Demo login = the local demo account (local stack only): AUDIT_DEMO_PW (default demo-local-audit)."""
import asyncio, os, re, sys, uuid
from pathlib import Path
import httpx, psycopg, pytest
from psycopg.types.json import Jsonb
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")   # deepagent_api (unit test below) builds a client at import; never called

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE / ".." / "app")); sys.path.insert(0, str(HERE / ".." / "overlay"))

B = os.environ.get("AOS_TEST_BASE", "http://127.0.0.1:8020"); ADMIN = ("moza", "phase0-local")
DEMO = ("Moza", os.environ.get("AUDIT_DEMO_PW", "demo-local-audit"))
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
PW = "phase0-gd13z-local"; SUF = uuid.uuid4().hex[:6]; MARK = "zw1" + SUF   # MARK is in every text of the w1 objects: it must never show up for the demo / another workspace
REFUSED = (401, 403, 404)
def pg(): return psycopg.connect(PG, autocommit=True)

RF = {"type": "object", "title": "Z", "properties": {"a": {"type": "string"}}, "required": ["a"]}
MW = [{"name": "ModelCallLimit", "params": {"run_limit": 3}}]

# (method, path template, json body or None, owner) -- owner "id" = the route works on an object id (a caller of another workspace must get 404/403);
# owner "own" = acts on the caller's own workspace (no id): the demo must be refused, a caller of another workspace only ever touches ITS workspace.
ROUTES = [
    ("POST", "/api/engine/issues/from_trace", {"trace_id": "{trace}"}, "id"),
    ("POST", "/api/engine/issues/{iid}/analyse", {}, "id"),
    ("POST", "/api/engine/issues/{iid}/test", {}, "id"),
    ("POST", "/api/engine/issues/{iid}/verify", {}, "id"),
    ("PUT", "/api/engine/issues/{iid}", {"status": "ignored"}, "id"),
    ("PATCH", "/api/engine/issues/{iid}", {"status": "ignored", "severity": 3}, "id"),
    ("POST", "/api/engine/issues/{iid}/apply", None, "id"),
    ("POST", "/api/engine/issues/{iid}/dataset", None, "id"),
    ("POST", "/api/engine/issues/{iid}/examples", {"items": [{"run_id": "{trace}", "input": "x", "questions": ["q?"]}]}, "id"),
    ("POST", "/api/engine/issues/{iid}/retest", None, "id"),
    ("POST", "/api/engine/detect", None, "own"),
    ("PUT", "/api/engine/settings", {"enabled": True, "score_min": 50}, "own"),
    ("PUT", "/api/engine/projects/{pid}/settings", {"enabled": True, "preferences": "zz"}, "id"),
    ("POST", "/api/engine/projects/{pid}/pause", None, "id"),
    ("POST", "/api/engine/projects/{pid}/resume", None, "id"),
    ("POST", "/api/insights/configs/generate", {"question": "what do users ask?"}, "own"),
    ("POST", "/api/insights/run", {}, "own"),
    ("POST", "/api/insights/configs", {"name": "zz-new-{suf}"}, "own"),
    ("PUT", "/api/insights/configs/{cid}", {"name": "zz-hijack-{suf}"}, "id"),
    ("DELETE", "/api/insights/configs/{cid}", None, "id"),
    ("POST", "/api/insights/configs/{cid}/count", None, "id"),
    ("POST", "/api/insights/configs/{cid}/run", None, "id"),
    ("DELETE", "/api/insights/reports/{rid}", None, "id"),
    ("PUT", "/api/insights/settings", {"weekly": True}, "own"),
    ("POST", "/api/insights/weekly/run_now", None, "own"),
    ("PUT", "/api/agents/{aid}/memory/file", {"path": "/zz-{suf}.md", "content": "planted"}, "id"),
    ("DELETE", "/api/agents/{aid}/memory/file?path=/notes.md", None, "id"),
    ("POST", "/api/agents/{aid}/memory/proposals/{mpid}/approve", None, "id"),
    ("POST", "/api/agents/{aid}/memory/proposals/{mpid}/reject", None, "id"),
    ("PUT", "/api/admin/retention", {"run_events_days": 5}, "own"),
    ("POST", "/api/admin/retention/preview", None, "own"),
    ("POST", "/api/admin/retention/run", {"confirm": True}, "own"),
    ("PUT", "/api/agents/{aid}", {"name": "zz-{suf}", "middleware": MW}, "id"),
    ("PUT", "/api/agents/{aid}", {"name": "zz-{suf}", "response_format": RF}, "id"),
    ("POST", "/api/v1/runs/crons", {"assistant_id": "{asst}", "schedule": "0 8 * * 1", "input": {"messages": [{"role": "user", "content": "zz"}]}}, "id"),
    ("POST", "/api/v1/threads/{tid}/runs/crons", {"assistant_id": "{asst}", "schedule": "0 8 * * 1", "input": {"messages": [{"role": "user", "content": "zz"}]}}, "id"),
    ("POST", "/api/v1/runs/crons/search", {"limit": 50}, "own"),
    ("POST", "/api/v1/runs/crons/count", {}, "own"),
    ("PATCH", "/api/v1/runs/crons/{cron}", {"enabled": False, "schedule": "0 9 * * 2"}, "id"),
    ("DELETE", "/api/v1/runs/crons/{cron}", None, "id"),
]
# Existing, documented behaviour (test_gd7_insights, test_gd9_server_parity): DELETE of an invisible row is an idempotent 200 no-op. For a caller of ANOTHER workspace we accept
# that 200 on exactly these two routes, only because the row stays untouched (checked by the fingerprint + a read by the owner). The demo is refused on them like on all others.
NOOP_DELETE = {("DELETE", "/api/insights/reports/{rid}"), ("DELETE", "/api/v1/runs/crons/{cron}")}
GETS = ["/api/engine/issues/{iid}", "/api/engine/issues/{iid}/history", "/api/engine/projects/{pid}/settings", "/api/insights/configs/{cid}", "/api/insights/reports/{rid}",
        "/api/agents/{aid}/memory/files", "/api/agents/{aid}/memory/file?path=/notes.md", "/api/agents/{aid}/memory/proposals", "/api/v1/runs/crons/{cron}"]

def fill(s, o):
    for k, v in o.items(): s = s.replace("{" + k + "}", str(v))
    return s.replace("{suf}", SUF)
def fill_body(b, o):
    if b is None: return None
    if isinstance(b, dict): return {k: fill_body(v, o) for k, v in b.items()}
    if isinstance(b, list): return [fill_body(v, o) for v in b]
    return fill(b, o) if isinstance(b, str) else b

# ------------------------------------------------------------------ fixtures
def fingerprint(w1, w2, wd, ids):
    """Rows of every table a GĐ13 write route can touch: hash of all rows of the three workspaces (+ the rows hanging off the agents / issues)."""
    out, wss = {}, (w1, w2, wd)
    with pg() as c:
        for t in ("issues", "insight_configs", "insight_reports", "lg_crons", "memory_proposals", "retention_settings", "engine_project_settings", "engine_runs", "datasets", "agents"):
            if t == "agents": cols = "t.id, t.workspace_id, t.name, t.current_version"
            elif t == "lg_crons": cols = "t.cron_id, t.workspace_id, t.schedule, t.enabled, t.input, t.metadata, t.timezone"
            else: cols = "t"
            out[t] = c.execute(f"SELECT count(*), md5(coalesce(string_agg(({cols})::text, '|' ORDER BY ({cols})::text), '')) FROM {t} t WHERE t.workspace_id = ANY(%s)", (list(wss),)).fetchone()
        out["agent_versions"] = c.execute("SELECT count(*) FROM agent_versions WHERE agent_id = ANY(%s)", (ids["agents"],)).fetchone()
        out["issue_events"] = c.execute("SELECT count(*) FROM issue_events WHERE issue_id = ANY(%s)", (ids["issues"],)).fetchone()
        out["ws_weekly"] = c.execute("SELECT string_agg(id::text || ':' || coalesce(insights_weekly::text, ''), ',' ORDER BY id) FROM workspaces WHERE id = ANY(%s)", (list(wss),)).fetchone()
    return out

@pytest.fixture(scope="module")
def env():
    a = httpx.Client(base_url=B, auth=ADMIN, timeout=60); e = {"a": a}
    for k in ("w1", "w2"):
        r = a.post("/api/admin/workspaces", json={"name": f"g13z-{k}-{SUF}"}); assert r.status_code == 200, r.text; e[k] = r.json()["id"]
    for k, role, ws in (("ad1", "admin", e["w1"]), ("ed2", "editor", e["w2"]), ("ad2", "admin", e["w2"])):
        r = a.post("/api/admin/users", json={"username": f"g13z_{k}_{SUF}", "password": PW, "role": role, "workspace_id": ws}); assert r.status_code == 200, r.text
        e[k] = httpx.Client(base_url=B, auth=(f"g13z_{k}_{SUF}", PW), timeout=60)
    d = httpx.Client(base_url=B, auth=DEMO, timeout=60); e["d"] = d
    me = d.get("/api/auth/me"); assert me.status_code == 200 and me.json()["demo"] is True, "the local demo account is missing (skill kiem-tra-toan-bo, demo leak audit)"
    e["wd"] = me.json()["ws"]; assert e["wd"] not in (e["w1"], e["w2"], 1)
    # ---- objects of the NON-demo workspace w1 (real ids)
    o = {}; ad1 = e["ad1"]
    r = ad1.post("/api/agents", json={"name": f"g13z-agent-{MARK}"}); assert r.status_code == 200, r.text; o["aid"] = r.json()["id"]
    r = ad1.put(f"/api/agents/{o['aid']}/memory/file", json={"path": "/notes.md", "content": MARK + " private note"}); assert r.status_code == 200, r.text
    r = ad1.post("/api/insights/configs", json={"name": f"g13z-cfg-{MARK}"}); assert r.status_code == 200, r.text; o["cid"] = r.json()["id"]
    asst = [x["assistant_id"] for x in ad1.post("/api/v1/assistants/search", json={"limit": 50}).json() if str(x.get("agent_id")) == str(o["aid"])]
    assert asst, "no assistant for the w1 agent"; o["asst"] = asst[0]
    r = ad1.post("/api/v1/threads", json={"metadata": {"source": "pytest"}}); assert r.status_code == 200, r.text; o["tid"] = r.json()["thread_id"]
    r = ad1.post("/api/v1/runs/crons", json={"assistant_id": o["asst"], "schedule": "0 8 * * 1", "input": {"messages": [{"role": "user", "content": MARK + " cron"}]}}); assert r.status_code == 200, r.text
    o["cron"] = r.json()["cron_id"]; o["trace"] = str(uuid.uuid4())
    with pg() as c:
        o["pid"] = c.execute("INSERT INTO projects(name, workspace_id) VALUES(%s,%s) RETURNING id", (f"g13z-p1-{SUF}", e["w1"])).fetchone()[0]
        o["iid"] = c.execute("INSERT INTO issues(workspace_id,project_id,kind,signature,title,count,sample_run_ids) VALUES(%s,%s,'error_cluster',%s,%s,2,%s) RETURNING id",
                             (e["w1"], o["pid"], f"sig-{MARK}", f"{MARK} issue", Jsonb([o["trace"]]))).fetchone()[0]
        o["rid"] = c.execute("INSERT INTO insight_reports(workspace_id, period, status, summary) VALUES(%s,'7d','done',%s) RETURNING id", (e["w1"], MARK + " report")).fetchone()[0]
        o["mpid"] = c.execute("INSERT INTO memory_proposals(workspace_id, agent_id, path, content, reason, source) VALUES(%s,%s,'/p.md',%s,%s,'external') RETURNING id", (e["w1"], o["aid"], MARK + " proposal", MARK)).fetchone()[0]
        c.execute("INSERT INTO retention_settings(workspace_id, run_events_days) VALUES(%s, 90)", (e["w1"],))
        c.execute("INSERT INTO engine_project_settings(project_id, workspace_id, preferences) VALUES(%s,%s,%s)", (o["pid"], e["w1"], MARK + " prefs"))
    e["F"] = o
    # ---- the demo's OWN objects (ws of the demo account), removed at the end
    m = {}; m["aid"] = me.json()["demo_agents"][0]
    dasst = [x["assistant_id"] for x in d.post("/api/v1/assistants/search", json={"limit": 50}).json() if str(x.get("agent_id")) == str(m["aid"])]
    assert dasst, "the demo account has no assistant"; m["asst"] = dasst[0]; m["trace"] = str(uuid.uuid4())
    with pg() as c:
        m["pid"] = c.execute("INSERT INTO projects(name, workspace_id) VALUES(%s,%s) RETURNING id", (f"g13z-pd-{SUF}", e["wd"])).fetchone()[0]
        m["iid"] = c.execute("INSERT INTO issues(workspace_id,project_id,kind,signature,title,count,sample_run_ids) VALUES(%s,%s,'error_cluster',%s,%s,1,%s) RETURNING id",
                             (e["wd"], m["pid"], f"sigd-{SUF}", f"demo issue {SUF}", Jsonb([m["trace"]]))).fetchone()[0]
        m["cid"] = c.execute("INSERT INTO insight_configs(workspace_id, name) VALUES(%s,%s) RETURNING id", (e["wd"], f"g13z-dcfg-{SUF}")).fetchone()[0]
        m["rid"] = c.execute("INSERT INTO insight_reports(workspace_id, period, status, summary) VALUES(%s,'7d','done',%s) RETURNING id", (e["wd"], "demo report " + SUF)).fetchone()[0]
        m["mpid"] = c.execute("INSERT INTO memory_proposals(workspace_id, agent_id, path, content, reason, source) VALUES(%s,%s,'/pd.md','demo proposal',%s,'external') RETURNING id", (e["wd"], m["aid"], SUF)).fetchone()[0]
        m["cron"] = str(uuid.uuid4())
        c.execute("INSERT INTO lg_crons(cron_id, assistant_id, schedule, input, workspace_id) VALUES(%s,%s,'0 8 * * 1',%s,%s)", (m["cron"], m["asst"], Jsonb({"messages": [{"role": "user", "content": "demo cron " + SUF}]}), e["wd"]))
    m["tid"] = d.post("/api/v1/threads", json={"metadata": {"source": "pytest"}}).json()["thread_id"]
    e["D"] = m
    e["ids"] = {"agents": [o["aid"], m["aid"]], "issues": [o["iid"], m["iid"]]}
    yield e
    for k in ("ad1", "ed2", "ad2", "d", "a"): e[k].close()
    with pg() as c:
        c.execute("DELETE FROM lg_crons WHERE cron_id=%s OR workspace_id = ANY(%s)", (m["cron"], [e["w1"], e["w2"]]))
        c.execute("DELETE FROM memory_proposals WHERE id=%s", (m["mpid"],)); c.execute("DELETE FROM insight_reports WHERE id=%s", (m["rid"],))
        c.execute("DELETE FROM insight_configs WHERE id=%s", (m["cid"],)); c.execute("DELETE FROM projects WHERE id=%s OR workspace_id = ANY(%s)", (m["pid"], [e["w1"], e["w2"]]))
        c.execute("DELETE FROM lg_threads WHERE thread_id=%s", (m["tid"],)); c.execute("DELETE FROM users WHERE username LIKE %s", (f"g13z\\_%\\_{SUF}",))
        for t in ("insight_configs", "insight_reports", "memory_proposals", "retention_settings", "agents"):
            try: c.execute(f"DELETE FROM {t} WHERE workspace_id = ANY(%s)", ([e["w1"], e["w2"]],))
            except Exception: pass  # noqa: BLE001

def call(cl, m, path, body):
    return cl.request(m, path, json=body) if body is not None else cl.request(m, path)

# ------------------------------------------------------------------ 0. the table is complete: a new write route must be added here
def test_every_gd13_write_route_is_in_the_table():
    app = HERE / ".." / "app"; found = set()
    rx = re.compile(r'@(?:router|app)\.(post|put|patch|delete)\("([^"]+)"')
    for f in ("engine.py", "engine_validate.py", "insights.py", "memory_admin.py", "retention.py", "durable.py", "agent_schedules.py", "deepagent_api.py", "server.py", "agent_fields.py"):
        src = (app / f).read_text(encoding="utf-8")
        for mth, p in rx.findall(src):
            if f == "server.py":
                if "/runs/crons" not in p: continue
                p = "/api/v1" + p
            if f == "deepagent_api.py" and (mth, p) != ("put", "/api/agents/{aid}"): continue   # only the agent PUT carries the 13.7 fields
            found.add((mth.upper(), p))
    have = {(m, p.split("?")[0]) for m, p, _, _ in ROUTES}
    norm = lambda p: re.sub(r"\{\w+\}", "{}", p)
    missing = {x for x in found if (x[0], norm(x[1])) not in {(m, norm(p)) for m, p in have}}
    assert not missing, f"new GĐ13 write routes without a demo-refusal test: {sorted(missing)}"
    assert len(have) >= 36

# ------------------------------------------------------------------ 1. the demo account
@pytest.mark.parametrize("target", ["F", "D"])
def test_demo_cannot_write_any_gd13_route(env, target):
    o = env[target]; ids = env["ids"]; before = fingerprint(env["w1"], env["w2"], env["wd"], ids); bad = []
    for m, p, b, _ in ROUTES:
        r = call(env["d"], m, fill(p, o), fill_body(b, o))
        if r.status_code not in REFUSED: bad.append((m, p, target, r.status_code, r.text[:80]))
    assert not bad, bad
    assert fingerprint(env["w1"], env["w2"], env["wd"], ids) == before
    # the owner's own data is still what it was (read back through the API of the real owner)
    assert env["ad1"].get(f"/api/insights/configs/{env['F']['cid']}").json()["name"] == f"g13z-cfg-{MARK}"
    assert env["ad1"].get(f"/api/agents/{env['F']['aid']}/memory/file", params={"path": "/notes.md"}).json()["content"] == MARK + " private note"

def test_demo_post_with_garbage_or_no_body_is_refused_before_validation(env):
    """The refusal must not depend on the body being valid (a 422 would mean the route looked at the request first)."""
    o = env["F"]; bad = []
    for m, p, _, _ in ROUTES:
        for body in ({}, {"junk": 1}):
            if m in ("DELETE",) and body: continue
            r = call(env["d"], m, fill(p, o), body)
            if r.status_code not in REFUSED: bad.append((m, p, r.status_code))
    assert not bad, bad

def test_demo_cannot_read_another_workspaces_objects_by_real_id(env):
    o = env["F"]; bad = []
    for p in GETS:
        r = env["d"].get(fill(p, o))
        if r.status_code not in (403, 404) or MARK in r.text: bad.append((p, r.status_code))
    assert not bad, bad
    r = env["d"].post("/api/v1/runs/crons/search", json={"limit": 100}); assert r.status_code in REFUSED, r.status_code    # not allowed to the demo at all
    for lst in ("/api/engine/issues", "/api/insights/configs", "/api/insights/reports", "/api/engine/projects", "/api/v1/runs/crons/" + env["F"]["cron"]):
        r = env["d"].get(lst)
        assert r.status_code in (200, 403, 404), (lst, r.status_code)
        assert str(env["F"]["iid"]) not in {str(x.get("id")) for x in (r.json().get("items") if r.status_code == 200 and isinstance(r.json(), dict) else [])}, lst
        assert MARK not in r.text, lst

def test_demo_reads_its_own_objects_control(env):
    """Control (the ids are real): the demo can read its OWN issue / config; the owner of w1 can read w1's objects."""
    D, F = env["D"], env["F"]
    assert env["d"].get(f"/api/engine/issues/{D['iid']}").status_code == 200 and env["d"].get(f"/api/insights/configs/{D['cid']}").status_code == 200
    assert env["d"].get(f"/api/v1/runs/crons/{D['cron']}").status_code == 200
    for p in ("/api/engine/issues/{iid}", "/api/insights/configs/{cid}", "/api/insights/reports/{rid}", "/api/agents/{aid}/memory/files", "/api/v1/runs/crons/{cron}", "/api/engine/projects/{pid}/settings"):
        assert env["ad1"].get(fill(p, F)).status_code == 200, p

# ------------------------------------------------------------------ 2. a caller of ANOTHER workspace (editor + admin) may not touch w1's objects either
@pytest.mark.parametrize("who", ["ed2", "ad2"])
def test_other_workspace_cannot_use_w1_ids(env, who):
    o = env["F"]; ids = env["ids"]; before = fingerprint(env["w1"], env["w2"], env["wd"], ids); bad = []
    for m, p, b, kind in ROUTES:
        if kind != "id": continue
        r = call(env[who], m, fill(p, o), fill_body(b, o))
        if r.status_code not in REFUSED and not (r.status_code == 200 and (m, p) in NOOP_DELETE): bad.append((m, p, who, r.status_code, r.text[:80]))
    assert not bad, bad
    assert fingerprint(env["w1"], env["w2"], env["wd"], ids) == before          # incl. the two idempotent DELETEs below: nothing was deleted
    for p in ("/api/insights/reports/{rid}", "/api/v1/runs/crons/{cron}"): assert env["ad1"].get(fill(p, o)).status_code == 200, p
    for p in GETS:
        r = env[who].get(fill(p, o)); assert r.status_code in (403, 404) and MARK not in r.text, (p, r.status_code)

def test_other_workspace_own_scope_writes_never_reach_w1(env):
    """Routes without an id act on the caller's own workspace: an admin of w2 changes w2 only (w1's rows keep their hash)."""
    ids = env["ids"]; w1 = env["w1"]
    with pg() as c: b = c.execute("SELECT md5(string_agg(t::text,'|' ORDER BY t::text)) FROM retention_settings t WHERE workspace_id=%s", (w1,)).fetchone()
    b_all = {t: fingerprint_one(t, w1) for t in ("issues", "insight_configs", "lg_crons", "engine_project_settings")}
    assert env["ad2"].put("/api/admin/retention", json={"run_events_days": 7}).status_code == 200
    assert env["ad2"].put("/api/engine/settings", json={"score_min": 33}).status_code == 200
    assert env["ad2"].put("/api/insights/settings", json={"weekly": True}).status_code == 200
    assert env["ed2"].post("/api/insights/configs", json={"name": f"zz-own-{SUF}"}).status_code == 200
    with pg() as c: a_ = c.execute("SELECT md5(string_agg(t::text,'|' ORDER BY t::text)) FROM retention_settings t WHERE workspace_id=%s", (w1,)).fetchone()
    assert a_ == b and {t: fingerprint_one(t, w1) for t in b_all} == b_all
    assert env["ad1"].get("/api/admin/retention").json()["run_events_days"] == 90
    assert [c["name"] for c in env["ad1"].get("/api/insights/configs").json()].count(f"zz-own-{SUF}") == 0

def fingerprint_one(t, ws):
    with pg() as c: return c.execute(f"SELECT count(*), md5(coalesce(string_agg(t::text,'|' ORDER BY t::text),'')) FROM {t} t WHERE workspace_id=%s", (ws,)).fetchone()

# ------------------------------------------------------------------ 3. unit level: 13.28 schedule tools + agent fields refuse the demo
def test_schedule_tools_and_agent_fields_refuse_demo_unit():
    import deepagent_api  # noqa: F401  (first: imports before the other app modules, like the other GĐ13 tests)
    import admin, agent_fields as AF, agent_schedules as S
    from fastapi import HTTPException
    def with_user(u, fn):
        t = admin.USER.set(u)
        try: return fn()
        finally: admin.USER.reset(t)
    mk = lambda role, **kw: {"id": 987138, "username": "t138", "role": role, "ws": 5, "auth": "session", **kw}
    for u in (mk("editor", demo=True), mk("admin", demo=True)):
        c, why = with_user(u, S.refusal); assert c is None and why and "demo" in why.lower()
    for note, u in (("viewer", mk("viewer")), ("api key", mk("admin", auth="api_key:abcd1234")), ("channel", mk("editor", auth="channel"))):
        c, why = with_user(u, S.refusal); assert c is None and why, note
    assert with_user(None, S.refusal)[0] is None                                       # no person at all (a schedule-triggered run)
    c, why = with_user(mk("editor"), S.refusal); assert why is None and c                  # control: a signed-in editor may
    from deepagent_api import AgentCfg
    for u in (mk("editor", demo=True), mk("admin", demo=True)):
        for cfg in (AgentCfg(name="x", middleware=MW), AgentCfg(name="x", response_format=RF)):
            with pytest.raises(HTTPException) as e: with_user(u, lambda: AF.refuse_demo(cfg))
            assert e.value.status_code == 403
    with_user(mk("editor", demo=True), lambda: AF.refuse_demo(AgentCfg(name="x")))      # an agent without the fields saves as always
