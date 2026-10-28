"""GĐ9.9 (24/10/2026): scheduled Parquet export (pyarrow 26). Local server :8020, fake LLM => $0. Throwaway workspaces + rows inserted straight into Postgres.
Export -> read back with pyarrow -> row counts match the DB per table; incremental run adds only new rows; another workspace never leaks; demo blocked."""
import datetime as dt, os, shutil, sys, uuid
import httpx, psycopg, pytest
import pyarrow as pa
import pyarrow.dataset as ds
from test_fixes import B, ADMIN, a  # noqa: F401  (fixtures)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
PW = "phase0-gd99-local"; SUF = uuid.uuid4().hex[:6]
TABLES = ("traces", "feedback", "eval_results", "gateway_usage")

def pg(): return psycopg.connect(PG, autocommit=True)

# ------------------------------------------------------------------ pure unit tests
def test_schema_is_stable():
    import parquet_export as P
    assert set(P.SCHEMAS) == set(TABLES) == set(P.TABLES)
    assert [(f.name, str(f.type)) for f in P.SCHEMAS["feedback"]] == [
        ("id", "int64"), ("run_id", "string"), ("trace_id", "string"), ("key", "string"), ("score", "double"), ("value", "string"), ("comment", "string"),
        ("correction", "string"), ("source", "string"), ("evaluator_id", "int32"), ("experiment_id", "int32"), ("created", "timestamp[us, tz=UTC]"), ("created_by", "string")]
    t = {f.name: str(f.type) for f in P.SCHEMAS["traces"]}
    assert t["start_time"] == "timestamp[us, tz=UTC]" and t["tags"] == "list<element: string>" and t["cost_usd"] == "decimal128(12, 6)" and t["inputs"] == "string"
    empty = P.to_table("gateway_usage", [])
    assert empty.schema == P.SCHEMAS["gateway_usage"] and empty.num_rows == 0

def test_s3_config_building():
    import parquet_export as P
    c = P.s3_cfg({"s3_endpoint": "https://acc.r2.cloudflarestorage.com", "s3_bucket": "bk", "s3_prefix": "/agentos/exports/", "s3_region": ""})
    assert (c["scheme"], c["host"], c["endpoint_override"], c["bucket"], c["prefix"], c["region"]) == ("https", "acc.r2.cloudflarestorage.com", "acc.r2.cloudflarestorage.com", "bk", "agentos/exports", None)
    assert P.s3_root(c) == "bk/agentos/exports"
    assert P.part_path(P.s3_root(c), 7, "traces", "2026-10-24") == "bk/agentos/exports/ws=7/table=traces/date=2026-10-24/part-0.parquet"
    assert P.s3_cfg({"s3_endpoint": "http://127.0.0.1:9000", "s3_bucket": "b"})["endpoint_override"] == "127.0.0.1:9000"
    for bad in ({"s3_endpoint": "http://evil.example.com", "s3_bucket": "b"}, {"s3_endpoint": "ftp://x.com", "s3_bucket": "b"}, {"s3_endpoint": "https://x.com", "s3_bucket": ""},
                {"s3_endpoint": "https://x.com", "s3_bucket": "a/b"}, {"s3_endpoint": "", "s3_bucket": "b"}):
        with pytest.raises(ValueError): P.s3_cfg(bad)

def test_s3_keys_only_for_the_endpoint_host(monkeypatch):
    import asyncio, parquet_export as P, ws_secrets
    seen = []
    async def fake(name, ws, host):
        seen.append((name, ws, host))
        if host != "acc.r2.cloudflarestorage.com": raise PermissionError("host")
        return "K-" + name
    monkeypatch.setattr(ws_secrets, "get_secret", fake)
    cfg = {"dest": "s3", "s3_endpoint": "https://acc.r2.cloudflarestorage.com", "s3_bucket": "bk", "access_key_secret": "AK", "secret_key_secret": "SK"}
    fs, root = asyncio.run(P.make_fs(cfg, 5))
    assert type(fs).__name__ == "S3FileSystem" and root == "bk" and seen == [("AK", 5, "acc.r2.cloudflarestorage.com"), ("SK", 5, "acc.r2.cloudflarestorage.com")]
    with pytest.raises(ValueError): asyncio.run(P.make_fs({**cfg, "s3_endpoint": "https://other.example.com"}, 5))

def test_daily_slot():
    import parquet_export as P
    now = dt.datetime.now().astimezone().replace(hour=P.HOUR, minute=5)
    today = now.date()
    assert P.due(now, None) and P.due(now, today - dt.timedelta(days=1)) and not P.due(now, today)
    assert not P.due(now.replace(hour=1, minute=59), None) and not P.due(now.replace(hour=P.HOUR + 12), None)

# ------------------------------------------------------------------ server tests
def _seed(c, ws, tag, n_traces, n_fb, n_ev, n_gw):
    """Rows of one workspace; returns ids."""
    now = dt.datetime.now(dt.timezone.utc)
    pid = c.execute("INSERT INTO projects(name, workspace_id) VALUES(%s,%s) RETURNING id", (f"pq-{tag}-{SUF}", ws)).fetchone()[0]
    traces = []
    for i in range(n_traces):
        tid = uuid.uuid4(); traces.append(tid)
        c.execute("INSERT INTO tr_runs(id,trace_id,project_id,name,run_type,start_time,end_time,latency_ms,status,inputs,outputs,metadata,tags,total_tokens,cost_usd,model) "
                  "VALUES(%s,%s,%s,%s,'chain',%s,%s,10,'success',%s,%s,'{}',%s,5,0.000123,'fake')",
                  (tid, tid, pid, f"{tag}-run{i}", now - dt.timedelta(days=i % 2, minutes=i), now, '{"q": "xin chào"}', '{"a": 1}', ["t1", "vn"]))
    for i in range(n_fb): c.execute("INSERT INTO feedback(run_id,trace_id,key,score,source) VALUES(%s,%s,'quality',%s,'human')", (traces[i % len(traces)], traces[i % len(traces)], 0.5))
    did = c.execute("INSERT INTO datasets(name, workspace_id) VALUES(%s,%s) RETURNING id", (f"pq-ds-{tag}-{SUF}", ws)).fetchone()[0]
    eid = c.execute("INSERT INTO experiments(name,dataset_id,target) VALUES(%s,%s,'{}') RETURNING id", (f"pq-exp-{tag}", did)).fetchone()[0]
    for i in range(n_ev): c.execute("INSERT INTO exp_results(experiment_id,example_id,output,latency_ms) VALUES(%s,%s,%s,7)", (eid, i, '{"reply": "ok"}'))
    for i in range(n_gw): c.execute("INSERT INTO gateway_usage(workspace_id,model,input_tokens,output_tokens,cost_usd,status,latency_ms) VALUES(%s,'fake',3,4,0.001,'ok',9)", (ws,))
    return {"pid": pid, "did": did, "eid": eid}

def _db_counts(c, ws):
    q = {"traces": "SELECT count(*) FROM tr_runs WHERE project_id IN (SELECT id FROM projects WHERE workspace_id=%s)",
         "feedback": "SELECT count(*) FROM feedback f WHERE f.trace_id IN (SELECT trace_id FROM tr_runs WHERE project_id IN (SELECT id FROM projects WHERE workspace_id=%s))",
         "eval_results": "SELECT count(*) FROM exp_results x JOIN experiments e ON e.id=x.experiment_id JOIN datasets d ON d.id=e.dataset_id WHERE d.workspace_id=%s",
         "gateway_usage": "SELECT count(*) FROM gateway_usage WHERE workspace_id=%s"}
    return {k: c.execute(s, (ws,)).fetchone()[0] for k, s in q.items()}

def _read(local_dir, name):
    d = f"{local_dir}/table={name}"
    return ds.dataset(d, format="parquet", partitioning="hive").to_table() if os.path.isdir(d) else None

@pytest.fixture(scope="module")
def env(a):
    ws, ws2 = (a.post("/api/admin/workspaces", json={"name": f"pq-{k}-{SUF}"}).json()["id"] for k in ("a", "b"))
    e = {"ws": ws, "ws2": ws2}
    for k, w in (("adm", ws), ("adm2", ws2)): e[k] = httpx.Client(base_url=B, auth=ADMIN, headers={"X-Workspace-Id": str(w)}, timeout=150)
    for k, role, demo in (("ed", "editor", False), ("demo", "viewer", True)):
        r = a.post("/api/admin/users", json={"username": f"pq_{k}_{SUF}", "password": PW, "role": role, "workspace_id": ws, **({"demo": True} if demo else {})})
        assert r.status_code == 200, r.text
        e[k] = httpx.Client(base_url=B, auth=(f"pq_{k}_{SUF}", PW), timeout=150)
    with pg() as c:
        e["rows"] = _seed(c, ws, "a", 5, 3, 4, 6); e["rows2"] = _seed(c, ws2, "b", 3, 2, 2, 2)
    yield e
    with pg() as c:
        for w in (ws, ws2):
            c.execute("DELETE FROM feedback WHERE trace_id IN (SELECT trace_id FROM tr_runs WHERE project_id IN (SELECT id FROM projects WHERE workspace_id=%s))", (w,))
            c.execute("DELETE FROM tr_runs WHERE project_id IN (SELECT id FROM projects WHERE workspace_id=%s)", (w,))
            c.execute("DELETE FROM projects WHERE workspace_id=%s", (w,)); c.execute("DELETE FROM datasets WHERE workspace_id=%s", (w,))
            c.execute("DELETE FROM gateway_usage WHERE workspace_id=%s", (w,)); c.execute("DELETE FROM parquet_export_cfg WHERE workspace_id=%s", (w,))
    adm = httpx.Client(base_url=B, auth=ADMIN, timeout=60)
    for u in adm.get("/api/admin/users").json():
        if u["username"] in (f"pq_ed_{SUF}", f"pq_demo_{SUF}"): adm.delete(f"/api/admin/users/{u['id']}")

def test_blocked_for_non_admin_and_demo(env):
    for k in ("ed", "demo"):
        c = env[k]
        assert c.get("/api/exports/parquet").status_code == 403, k
        assert c.put("/api/exports/parquet", json={"enabled": True}).status_code == 403, k
        assert c.post("/api/exports/parquet/run", json={}).status_code == 403, k

def test_config_validation(env):
    r = env["adm"].get("/api/exports/parquet"); assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["dest"] == "local"
    for body in ({"dest": "s3", "s3_endpoint": "http://evil.example.com", "s3_bucket": "b", "access_key_secret": "x", "secret_key_secret": "y"},
                 {"dest": "s3", "s3_endpoint": "https://x.example.com", "s3_bucket": "b", "access_key_secret": "no-such", "secret_key_secret": "no-such"}):
        assert env["adm"].put("/api/exports/parquet", json=body).status_code == 422, body
    assert env["adm"].put("/api/exports/parquet", json={"enabled": True, "dest": "local"}).json()["enabled"] is True

def test_export_reads_back_with_matching_counts(env):
    r = env["adm"].post("/api/exports/parquet/run", json={}); assert r.status_code == 200, r.text
    j = r.json(); local = j["local_dir"]; assert local.endswith(f"ws={env['ws']}") and j["last_status"] == "ok"
    with pg() as c: db = _db_counts(c, env["ws"])
    assert db == {"traces": 5, "feedback": 3, "eval_results": 4, "gateway_usage": 6}
    for name in TABLES:
        t = _read(local, name); assert t is not None and t.num_rows == db[name], name
        assert j["counts"][name]["added"] == db[name] and j["totals"][name] == db[name]
        base = [f for f in t.schema if f.name != "date"]   # hive partition column "date" is added by the reader
        import parquet_export as P
        assert [(f.name, str(f.type)) for f in base] == [(f.name, str(f.type)) for f in P.SCHEMAS[name]], name
    days = sorted(os.listdir(f"{local}/table=traces")); assert len(days) == 2 and all(d.startswith("date=") for d in days)
    assert os.path.isfile(f"{local}/table=traces/{days[0]}/part-0.parquet")
    import pyarrow.parquet as pq
    assert pq.ParquetFile(f"{local}/table=gateway_usage/" + sorted(os.listdir(f"{local}/table=gateway_usage"))[0] + "/part-0.parquet").metadata.row_group(0).column(0).compression == "ZSTD"
    tr = _read(local, "traces").to_pylist()
    assert {x["project_id"] for x in tr} == {env["rows"]["pid"]} and all(x["inputs"] == '{"q": "xin chào"}' and x["tags"] == ["t1", "vn"] for x in tr)

def test_incremental_adds_only_new_rows(env):
    local = env["adm"].get("/api/exports/parquet").json()["local_dir"]
    with pg() as c:
        now = dt.datetime.now(dt.timezone.utc); t1, t2 = uuid.uuid4(), uuid.uuid4()
        for t in (t1, t2):   # t1 newest (merge into today part-0); t2 is OLDER than the cursor but inside the 24 h lookback (late span)
            c.execute("INSERT INTO tr_runs(id,trace_id,project_id,name,run_type,start_time,status) VALUES(%s,%s,%s,'new','chain',%s,'success')",
                      (t, t, env["rows"]["pid"], now if t == t1 else now - dt.timedelta(hours=20)))
        c.execute("INSERT INTO feedback(run_id,trace_id,key,score) VALUES(%s,%s,'quality',1)", (t1, t1))
        c.execute("INSERT INTO exp_results(experiment_id,example_id,output) VALUES(%s,99,'{}')", (env["rows"]["eid"],))
        c.execute("INSERT INTO gateway_usage(workspace_id,model,status) VALUES(%s,'fake','ok')", (env["ws"],))
        db = _db_counts(c, env["ws"])
    j = env["adm"].post("/api/exports/parquet/run", json={}).json()
    assert {k: v["added"] for k, v in j["counts"].items()} == {"traces": 2, "feedback": 1, "eval_results": 1, "gateway_usage": 1}
    for name in TABLES: assert _read(local, name).num_rows == db[name] == j["totals"][name], name
    ids = [x["id"] for x in _read(local, "traces").to_pylist()]; assert len(ids) == len(set(ids)) == 7   # nothing duplicated by the merge
    j = env["adm"].post("/api/exports/parquet/run", json={}).json()
    assert {v["added"] for v in j["counts"].values()} == {0}
    for name in TABLES: assert _read(local, name).num_rows == db[name], name

def test_other_workspace_never_included(env):
    local = env["adm"].get("/api/exports/parquet").json()["local_dir"]
    with pg() as c:
        other = {r[0] for r in c.execute("SELECT id::text FROM tr_runs WHERE project_id=%s", (env["rows2"]["pid"],)).fetchall()}
        other_eval = {r[0] for r in c.execute("SELECT x.id FROM exp_results x WHERE experiment_id=%s", (env["rows2"]["eid"],)).fetchall()}
    assert other and not other & {x["id"] for x in _read(local, "traces").to_pylist()}
    assert not other_eval & {x["id"] for x in _read(local, "eval_results").to_pylist()}
    assert {x["experiment_id"] for x in _read(local, "eval_results").to_pylist()} == {env["rows"]["eid"]}
    assert env["rows2"]["pid"] not in {x["project_id"] for x in _read(local, "traces").to_pylist()}
    j2 = env["adm2"].post("/api/exports/parquet/run", json={}).json()   # the other workspace exports only its own rows into its own folder
    with pg() as c: db2 = _db_counts(c, env["ws2"])
    assert db2 == {"traces": 3, "feedback": 2, "eval_results": 2, "gateway_usage": 2} and j2["local_dir"].endswith(f"ws={env['ws2']}") and j2["local_dir"] != local
    for name in TABLES: assert _read(j2["local_dir"], name).num_rows == db2[name], name

def test_full_reexport_rebuilds_same_data(env):
    local = env["adm"].get("/api/exports/parquet").json()["local_dir"]
    stale = f"{local}/table=traces/date=2000-01-01"; os.makedirs(stale, exist_ok=True); open(stale + "/part-0.parquet", "wb").close()   # left-over file: full run must delete it
    with pg() as c: db = _db_counts(c, env["ws"])
    j = env["adm"].post("/api/exports/parquet/run", json={"full": True}).json()
    assert j["last_mode"] == "full" and {k: v["added"] for k, v in j["counts"].items()} == db and not os.path.exists(stale)
    for name in TABLES: assert _read(local, name).num_rows == db[name], name
    shutil.rmtree(local, ignore_errors=True); shutil.rmtree(env["adm2"].get("/api/exports/parquet").json()["local_dir"], ignore_errors=True)
