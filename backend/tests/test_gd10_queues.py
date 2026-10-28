"""GĐ10.5 annotation queue: real reviewer, reservations, reviews_per_run, assignees. In-process (no server restart): the 0003 migration is applied to
the local DB, handlers are called with a faked current user (admin.USER) on a private pool. Rows are removed at the end."""
import asyncio, os, sys, uuid
import psycopg, pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from fastapi import HTTPException

os.environ.setdefault("GOOGLE_API_KEY", "x-not-used")   # importing deepagent_api needs it; no LLM call is made here
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import deepagent_api as A  # noqa: E402,I001  (must come first: circular imports)
import admin, evals, migrations  # noqa: E402

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
SUF = uuid.uuid4().hex[:6]

def U(name): return {"id": 900, "username": name, "role": "admin", "ws": 1}

def run(coro_fn):
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as c:
            await migrations.run_pending(c)
        async with AsyncConnectionPool(PG, min_size=1, max_size=3, open=False, kwargs={"autocommit": True, "row_factory": dict_row}) as pool:
            await pool.open(); A.S["pool"] = pool; A.S["rls"] = admin.rls_ctx
            await A.q("GRANT SELECT, INSERT, UPDATE, DELETE ON queue_item_reviews TO aos_rls"); await A.q("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO aos_rls")
            await admin._policy("queue_item_reviews", "aos_ws() IS NULL OR workspace_id = aos_ws()")   # what rls_init does at boot (table is in RLS_DIRECT)
            await A.q("ALTER TABLE queue_item_reviews ALTER COLUMN workspace_id SET DEFAULT coalesce(aos_ws(), 1)")
            return await coro_fn()
    return asyncio.run(go())

async def as_(name, fn, *a, **kw):
    tok = admin.USER.set(U(name))
    try: return await fn(*a, **kw)
    finally: admin.USER.reset(tok)

async def setup(settings, assignees, n_items=1):
    qid = (await A.q("INSERT INTO annotation_queues(name,rubric,settings,assignees) VALUES(%s,%s,%s,%s) RETURNING id",
                     (f"q-{SUF}-{uuid.uuid4().hex[:4]}", evals.Jsonb([{"key": "ok", "type": "boolean"}]), evals.Jsonb(settings), assignees), one=True))["id"]
    pid = (await A.q("SELECT id FROM projects ORDER BY id LIMIT 1", one=True))["id"]
    runs = []
    for _ in range(n_items):
        r = (await A.q("INSERT INTO tr_runs(id,trace_id,name,project_id,start_time) VALUES(%s,%s,'t',%s,now()) RETURNING id", (uuid.uuid4(), uuid.uuid4().hex, pid), one=True))["id"]
        runs.append(r); await evals.queue_add(qid, str(r))
    return qid, runs

async def cleanup(qid, runs):
    await A.q("DELETE FROM feedback WHERE run_id::text = ANY(%s)", ([str(r) for r in runs],))
    await A.q("DELETE FROM annotation_queues WHERE id=%s", (qid,)); await A.q("DELETE FROM tr_runs WHERE id = ANY(%s)", (runs,))

def test_real_reviewer_in_feedback_and_item():
    async def go():
        qid, runs = await setup({}, [])
        try:
            it = (await as_("alice", evals.q_next, qid))["item"]
            await as_("alice", evals.q_submit, it["id"], evals.QSubmit(scores={"ok": True}))
            fb = await A.q("SELECT created_by FROM feedback WHERE run_id=%s", (runs[0],))
            assert [f["created_by"] for f in fb] == ["alice"]
            row = await A.q("SELECT status, reviewer FROM queue_items WHERE id=%s", (it["id"],), one=True)
            assert (row["status"], row["reviewer"]) == ("done", "alice")
            assert (await as_("alice", evals.q_next, qid))["item"] is None
        finally: await cleanup(qid, runs)
    run(go)

def test_reservation_blocks_second_until_expiry():
    async def go():
        qid, runs = await setup({"reservations": True, "reservation_minutes": 5}, [])
        try:
            assert (await as_("alice", evals.q_next, qid))["item"] is not None
            assert (await as_("bob", evals.q_next, qid))["item"] is None   # reserved by alice
            await A.q("UPDATE queue_item_reviews SET reserved_until = now() - interval '1 minute'")   # expired -> free
            assert (await as_("bob", evals.q_next, qid))["item"] is not None
        finally: await cleanup(qid, runs)
    run(go)

def test_reviews_per_run_two_needs_two_reviewers():
    async def go():
        qid, runs = await setup({"reviews_per_run": 2}, [])
        try:
            it = (await as_("alice", evals.q_next, qid))["item"]
            await as_("alice", evals.q_submit, it["id"], evals.QSubmit(scores={"ok": True}))
            assert (await A.q("SELECT status FROM queue_items WHERE id=%s", (it["id"],), one=True))["status"] == "pending"
            assert (await as_("alice", evals.q_next, qid))["item"] is None   # alice cannot review twice
            assert (await as_("bob", evals.q_next, qid))["item"]["id"] == it["id"]
            await as_("bob", evals.q_submit, it["id"], evals.QSubmit(scores={"ok": False}))
            assert (await A.q("SELECT status FROM queue_items WHERE id=%s", (it["id"],), one=True))["status"] == "done"
            assert sorted(f["created_by"] for f in await A.q("SELECT created_by FROM feedback WHERE run_id=%s", (runs[0],))) == ["alice", "bob"]
        finally: await cleanup(qid, runs)
    run(go)

def test_skip_hides_for_that_user_only():
    async def go():
        qid, runs = await setup({}, [])
        try:
            it = (await as_("alice", evals.q_next, qid))["item"]
            await as_("alice", evals.q_submit, it["id"], evals.QSubmit(scores={}, action="skip"))
            assert (await as_("alice", evals.q_next, qid))["item"] is None
            assert (await as_("bob", evals.q_next, qid))["item"]["id"] == it["id"]
        finally: await cleanup(qid, runs)
    run(go)

def test_non_assignee_gets_nothing():
    async def go():
        qid, runs = await setup({}, ["alice"])
        try:
            assert (await as_("bob", evals.q_next, qid))["item"] is None
            with pytest.raises(HTTPException) as e: await as_("bob", evals.q_submit, (await A.q("SELECT id FROM queue_items WHERE queue_id=%s", (qid,), one=True))["id"], evals.QSubmit(scores={"ok": True}))
            assert e.value.status_code == 403
            assert (await as_("alice", evals.q_next, qid))["item"] is not None
        finally: await cleanup(qid, runs)
    run(go)
