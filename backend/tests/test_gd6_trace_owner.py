"""GD6.10 (22/10/2026): chat traces (tr_runs.user_id) and chat threads are private per user inside one workspace; admin sees all + "Của ai" filter;
SDK-ingested rows (user_id NULL) stay project-visible. Local server :8020, fake LLM => $0. Throwaway users."""
import os, uuid
import httpx, psycopg, pytest
from test_fixes import B, ADMIN, a, restore, _clean  # noqa: F401  (fixtures)

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
PW = "phase0-gd610-local"; G = "agent-2"

def _user(a, name):
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": "editor"})
    uid = r.json().get("id") if r.status_code == 200 else next(x["id"] for x in a.get("/api/admin/users").json() if x["username"] == name)
    return uid, httpx.Client(base_url=B, auth=(name, PW), timeout=90)

@pytest.fixture(scope="module")
def two(a):
    i1, c1 = _user(a, "gd610_u1"); i2, c2 = _user(a, "gd610_u2")
    yield (i1, c1), (i2, c2)
    c1.close(); c2.close(); a.delete(f"/api/admin/users/{i1}"); a.delete(f"/api/admin/users/{i2}")

@pytest.fixture(scope="module")
def data(a, two):
    (i1, c1), (i2, c2) = two
    out = {}
    for k, c in (("u1", c1), ("u2", c2)):
        tid = c.post("/api/v1/threads", json={"metadata": {"source": "chat", "graph_id": G}}).json()["thread_id"]
        r = c.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": G, "input": {"messages": [{"type": "human", "content": f"hello {k} {uuid.uuid4().hex[:6]}"}]}})
        assert r.status_code == 200, r.text; out[k] = tid
    pid = next(p["id"] for p in a.get("/api/obs/projects").json() if p["name"] == "agents")
    sdk = str(uuid.uuid4())
    with psycopg.connect(PG, autocommit=True) as pg:
        pg.execute("INSERT INTO tr_runs(id,trace_id,project_id,name,run_type,start_time,status) VALUES(%s,%s,%s,'gd610-sdk-row','chain',now(),'success')", (sdk, sdk, pid))
    out.update(pid=pid, sdk=sdk, i1=i1, i2=i2)
    yield out
    with psycopg.connect(PG, autocommit=True) as pg:
        pg.execute("DELETE FROM tr_runs WHERE id=%s", (sdk,))

def roots(c, pid, **kw):
    r = c.get("/api/obs/runs", params={"project_id": pid, "limit": 500, **kw}); assert r.status_code == 200, r.text
    return r.json()

def test_owner_column_set_on_chat_traces(data):
    with psycopg.connect(PG) as pg:
        rows = pg.execute("SELECT thread_id, user_id FROM tr_runs WHERE thread_id IN (%s,%s) AND parent_id IS NULL", (data["u1"], data["u2"])).fetchall()
        assert dict(rows) == {data["u1"]: data["i1"], data["u2"]: data["i2"]}
        n = pg.execute("SELECT count(*) FROM tr_runs t WHERE t.parent_id IS NOT NULL AND trace_id IN (SELECT trace_id FROM tr_runs WHERE thread_id=%s) AND user_id IS DISTINCT FROM %s", (data["u1"], data["i1"])).fetchone()[0]
        assert n == 0          # child spans carry the owner too

def test_users_see_only_their_own_traces(data, two):
    (_, c1), (_, c2) = two
    t1 = {r["thread_id"] for r in roots(c1, data["pid"])}; t2 = {r["thread_id"] for r in roots(c2, data["pid"])}
    assert data["u1"] in t1 and data["u2"] not in t1
    assert data["u2"] in t2 and data["u1"] not in t2

def test_trace_detail_and_threads_endpoint_hidden(data, two, a):
    (_, c1), (_, c2) = two
    tr = next(r["trace_id"] for r in roots(a, data["pid"]) if r["thread_id"] == data["u1"])
    assert len(c1.get(f"/api/obs/traces/{tr}").json()["runs"]) >= 1
    r2 = c2.get(f"/api/obs/traces/{tr}"); assert r2.status_code == 404 or r2.json().get("runs") == []
    th2 = {t["thread_id"] for t in c2.get(f"/api/obs/projects/{data['pid']}/threads").json()}
    assert data["u2"] in th2 and data["u1"] not in th2

def test_sdk_rows_stay_project_visible(data, two):
    (_, c1), (_, c2) = two
    for c in (c1, c2): assert any(r["name"] == "gd610-sdk-row" for r in roots(c, data["pid"]))

def test_admin_sees_all_and_owner_filter(data, a):
    rs = roots(a, data["pid"]); ths = {r["thread_id"] for r in rs}
    assert data["u1"] in ths and data["u2"] in ths and any(r["name"] == "gd610-sdk-row" for r in rs)
    f1 = roots(a, data["pid"], owner_id=data["i1"])
    assert {r["thread_id"] for r in f1 if r["thread_id"]} >= {data["u1"]} and data["u2"] not in {r["thread_id"] for r in f1}
    assert all(r["user_id"] == data["i1"] for r in f1) and f1[0]["owner"] == "gd610_u1"
    t = a.get(f"/api/obs/projects/{data['pid']}/threads", params={"owner_id": data["i2"]}).json()
    assert {x["thread_id"] for x in t} >= {data["u2"]} and data["u1"] not in {x["thread_id"] for x in t} and all(x["owner"] == "gd610_u2" for x in t)

def test_chat_threads_private_and_admin_filter(data, two, a):
    (_, c1), (_, c2) = two
    s1 = {t["thread_id"] for t in c1.post("/api/v1/threads/search", json={"limit": 1000}).json()}
    s2 = {t["thread_id"] for t in c2.post("/api/v1/threads/search", json={"limit": 1000}).json()}
    assert data["u1"] in s1 and data["u2"] not in s1 and data["u2"] in s2 and data["u1"] not in s2
    assert c2.get(f"/api/v1/threads/{data['u1']}").status_code in (403, 404)
    allt = a.post("/api/v1/threads/search", json={"limit": 1000}).json()
    assert {data["u1"], data["u2"]} <= {t["thread_id"] for t in allt}
    f = a.post("/api/v1/threads/search", json={"limit": 1000, "owner_id": data["i2"]}).json()
    assert f and {t["thread_id"] for t in f} >= {data["u2"]} and data["u1"] not in {t["thread_id"] for t in f} and all(t["owner"] == "gd610_u2" for t in f)
