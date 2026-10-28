"""Shared helpers of the GĐ12 lane-T tests (12.15 tracing, 12.18 retention): a private DB pool under the app's RLS role and a seeded two-workspace world
(workspaces a / b, one project + one ingest key each). Everything the tests create is removed afterwards; real projects are never touched.
Not a test file (no `test_` prefix)."""
import asyncio, datetime as dt, os, sys, uuid
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")   # deepagent_api (imported by obs) wants a key at import; nothing is called
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
NOW = lambda: dt.datetime.now(dt.timezone.utc)   # noqa: E731

def with_world(fn, **kw):
    """Run `await fn(A, admin, W)` on a private pool (the app's RLS role) with the seeded world; always clean up and restore the app's globals."""
    import deepagent_api as A, admin
    async def go():
        old = (A.S.get("pool"), A.S.get("rls"))
        async with AsyncConnectionPool(PG, min_size=1, max_size=4, open=False, kwargs={"autocommit": True, "row_factory": dict_row}) as pool:
            await pool.open(); A.S["pool"] = pool; A.S["rls"] = admin.rls_ctx
            if "trace_retention" in admin.RLS_CHILD: await admin._policy("trace_retention", "aos_ws() IS NULL OR (" + admin.RLS_CHILD["trace_retention"] + ")")   # what rls_init does at boot (12.18)
            await admin.rls_grants()
            W = await seed(A, **kw)
            try: return await fn(A, admin, W)
            finally:
                try: await cleanup(A, W)
                finally: A.S["pool"], A.S["rls"] = old
    return asyncio.run(go())

async def seed(A, tier=None):
    tag = uuid.uuid4().hex[:8]; W = {"tag": tag}
    for s in "ab":
        ws = (await A.q("INSERT INTO workspaces(name) VALUES(%s) RETURNING id", (f"t12-{s}-{tag}",), one=True))["id"]
        pid = (await A.q("INSERT INTO projects(name,workspace_id,retention_tier) VALUES(%s,%s,%s) RETURNING id", (f"p-{s}-{tag}", ws, tier), one=True))["id"]
        ing = "ak_" + uuid.uuid4().hex[:36]
        import admin
        await A.q("INSERT INTO ingest_keys(project_id,key_hash,prefix,label) VALUES(%s,%s,%s,'t12')", (pid, admin._h(ing), ing[:8]))
        W[s] = {"ws": ws, "pid": pid, "ing": ing}
    return W

async def cleanup(A, W):
    ws = [W[s]["ws"] for s in "ab"]; pids = [W[s]["pid"] for s in "ab"]
    for sql, args in (("DELETE FROM feedback WHERE run_id IN (SELECT id FROM tr_runs WHERE project_id = ANY(%s))", (pids,)),
                      ("DELETE FROM run_attachments WHERE run_id IN (SELECT id FROM tr_runs WHERE project_id = ANY(%s))", (pids,)),
                      ("DELETE FROM trace_retention WHERE project_id = ANY(%s)", (pids,)),
                      ("DELETE FROM tr_runs WHERE project_id = ANY(%s)", (pids,)),
                      ("DELETE FROM evaluators WHERE name LIKE %s", (f"t12-%-{W['tag']}",)),
                      ("DELETE FROM ingest_keys WHERE project_id = ANY(%s)", (pids,)),
                      ("DELETE FROM projects WHERE workspace_id = ANY(%s)", (ws,)),
                      ("DELETE FROM workspaces WHERE id = ANY(%s)", (ws,))):
        try: await A.q(sql, args)
        except Exception as e: print("cleanup:", sql[:50], str(e)[:100])   # noqa: BLE001  (best effort: never hide the test result)

async def as_user(admin, ws, fn, role="admin", demo=False):
    """Run `await fn()` as a signed-in user of workspace `ws` with the given role (admin | editor | viewer)."""
    t = admin.USER.set({"id": 987654, "username": "t12", "role": role, "ws": ws, "auth": "test", **({"demo": True} if demo else {})})
    try: return await fn()
    finally: admin.USER.reset(t)

async def sysq(A, sql, args=(), **kw): return await A.q(sql, args, **kw)   # no USER / WS_CTX = the system context (sees every workspace)

async def run(obs, pid, rid=None, trace=None, parent=None, start=None, ms=1000, **kw):
    """Store one finished run (obs.upsert_run) and return (run_id, trace_id)."""
    rid = rid or str(uuid.uuid4()); trace = trace or rid; start = start or NOW()
    await obs.upsert_run(pid, {"id": rid, "trace_id": trace, "parent_run_id": parent, "name": "r", "start_time": start,
                               "end_time": start + dt.timedelta(milliseconds=ms), "inputs": {"q": "hi"}, "outputs": {"a": "ok"}, **kw})
    return rid, trace
