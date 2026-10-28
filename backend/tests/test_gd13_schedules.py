"""GĐ13.27 schedules: end_time, next_run_date (timezone), full cron routes, on_run_completed, migration 0225, workspace isolation.
In-process (private pool, two seeded workspaces from test_gd10_security.with_db); no server, no network, fake LLM never called."""
import asyncio, datetime as dt, os, sys, uuid
import psycopg, pytest
sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls"); os.environ.setdefault("OPENAI_API_KEY", "sk-test-never-used")
import deepagent_api  # noqa: E402,F401
import evals, migrations as M  # noqa: E402

U = dt.timezone.utc
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
def at(h, m, d=26): return dt.datetime(2026, 10, d, h, m, 5, tzinfo=U)

def test_end_time_stops_is_due():
    end = at(10, 0)
    assert evals.is_due("* * * * *", "UTC", None, at(9, 58), None, end)
    assert not evals.is_due("* * * * *", "UTC", None, at(10, 0), None, end)      # at end_time
    assert not evals.is_due("* * * * *", "UTC", None, at(10, 30), None, end)
    assert evals.is_due("* * * * *", "UTC", None, at(10, 30))                    # default None = unchanged
    assert not evals.is_due("@once", "UTC", at(9, 0), at(10, 30), None, end)

def test_next_run_date_timezone():
    now = at(10, 0)   # 17:00 ICT on 26/10
    assert evals.next_run_date("30 9 * * *", "Asia/Ho_Chi_Minh", None, now) == dt.datetime(2026, 10, 27, 2, 30, tzinfo=U)
    assert evals.next_run_date("30 9 * * *", "UTC", None, now) == dt.datetime(2026, 10, 27, 9, 30, tzinfo=U)
    assert evals.next_run_date("30 9 * * *", "UTC", None, at(8, 0)) == dt.datetime(2026, 10, 26, 9, 30, tzinfo=U)
    assert evals.next_run_date("0 0 1 1 *", "UTC", None, now) == dt.datetime(2027, 1, 1, 0, 0, tzinfo=U)
    assert evals.next_run_date("30 9 * * *", "UTC", None, now, None, dt.datetime(2026, 10, 27, 9, 0, tzinfo=U)) is None   # past end_time
    ro = dt.datetime(2026, 11, 1, 3, 0, tzinfo=U)
    assert evals.next_run_date("@once", "UTC", ro, now) == ro
    assert evals.next_run_date("@once", "UTC", ro, now, dt.datetime(2026, 11, 1, 3, 1, tzinfo=U)) is None   # already ran

def test_norm_end_time():
    assert evals.norm_end_time(None) is None and evals.norm_end_time("") is None
    assert evals.norm_end_time("2026-10-26T19:00:00+07:00") == dt.datetime(2026, 10, 26, 12, 0, tzinfo=U)
    with pytest.raises(ValueError): evals.norm_end_time("later")

def test_migration_0225_up_down():
    sch = "t1327_" + uuid.uuid4().hex[:8]
    cols = ("end_time", "on_run_completed", "user_id", "config", "context", "durability")
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
            await c.execute("CREATE TABLE lg_crons(cron_id int, schedule text)"); await c.execute("INSERT INTO lg_crons VALUES(1,'* * * * *')")
            mod = [m for v, m, _ in M._load() if v == "0225"][0]; q = M._q(c)
            await mod.up(q); await mod.up(q)   # idempotent
            row = await (await c.execute("SELECT on_run_completed, end_time, config FROM lg_crons")).fetchone()
            assert row == ("keep", None, None)
            await mod.down(q)
            n = await (await c.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema=%s AND column_name = ANY(%s)", (sch, list(cols)))).fetchone()
            assert n[0] == 0
    with psycopg.connect(PG, autocommit=True) as c: c.execute(f'CREATE SCHEMA "{sch}"')
    try: asyncio.run(go())
    finally:
        with psycopg.connect(PG, autocommit=True) as c: c.execute(f'DROP SCHEMA "{sch}" CASCADE')

def _routes():
    import server
    from test_gd10_security import with_db, as_user
    async def mig():   # only this card's migration (what the next boot does for lg_crons); other cards' pending migrations are not this test's business
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as c:
            await [m for v, m, _ in M._load() if v == "0225"][0].up(M._q(c))
    def with_db28(fn): asyncio.run(mig()); return with_db(fn)
    return server, with_db28, as_user

def test_cron_routes_full_cycle_and_isolation():
    server, with_db28, as_user = _routes()
    from fastapi import HTTPException
    async def go(A, admin, W):
        a, b = W["a"], W["b"]
        asst = (await A.q("INSERT INTO lg_assistants(assistant_id,graph_id,name,workspace_id) VALUES(%s,'agent','t1327',%s) RETURNING assistant_id",
                          (str(uuid.uuid4()), a["ws"]), one=True))["assistant_id"] if True else None
        mk = lambda **k: server.CronIn(assistant_id=str(asst), schedule="30 9 * * *", input={"messages": []}, timezone="Asia/Ho_Chi_Minh", **k)
        try:
            c = await as_user(admin, a["ws"], lambda: server.cron_new(mk(end_time="2099-01-01T00:00:00Z", on_run_completed="delete", config={"x": 1})))
            cid = str(c["cron_id"])
            assert c["on_run_completed"] == "delete" and c["config"] == {"x": 1} and c["end_time"].year == 2099 and c["next_run_date"]
            nxt = dt.datetime.fromisoformat(c["next_run_date"]); assert nxt.hour == 2 and nxt.minute == 30     # 09:30 ICT = 02:30 UTC
            g = await as_user(admin, a["ws"], lambda: server.cron_get(cid)); assert g["cron_id"] == c["cron_id"]
            # PATCH of every field persists
            body = {"schedule": "0 8 * * *", "timezone": "UTC", "end_time": "2098-01-01T00:00:00Z", "input": {"messages": [1]}, "enabled": False, "on_run_completed": "keep",
                    "config": {"y": 2}, "context": {"z": 3}, "durability": "sync", "user_id": "u1", "webhook": "http://127.0.0.1:1/x", "metadata": {"k": "v"}}
            p = await as_user(admin, a["ws"], lambda: server.cron_patch(cid, dict(body)))
            assert (p["schedule"], p["timezone"], p["enabled"], p["on_run_completed"], p["input"], p["config"], p["context"], p["durability"], p["user_id"], p["webhook"], p["metadata"]) == \
                   ("0 8 * * *", "UTC", False, "keep", {"messages": [1]}, {"y": 2}, {"z": 3}, "sync", "u1", "http://127.0.0.1:1/x", {"k": "v"})
            assert p["end_time"].year == 2098 and p["next_run_date"] is None      # disabled -> no next run
            p = await as_user(admin, a["ws"], lambda: server.cron_patch(cid, {"is_enabled": True}))
            assert p["enabled"] and p["next_run_date"] and dt.datetime.fromisoformat(p["next_run_date"]).hour == 8
            with pytest.raises(HTTPException) as e: await as_user(admin, a["ws"], lambda: server.cron_patch(cid, {"on_run_completed": "x"}))
            assert e.value.status_code == 422
            with pytest.raises(HTTPException) as e: await as_user(admin, a["ws"], lambda: server.cron_patch(cid, {"end_time": "nope"}))
            assert e.value.status_code == 422
            # search filters / paging / sorting / count
            c2 = await as_user(admin, a["ws"], lambda: server.cron_new(mk(enabled=False)))
            r = await as_user(admin, a["ws"], lambda: server.cron_search({"assistant_id": str(asst)})); assert len(r) == 2
            r = await as_user(admin, a["ws"], lambda: server.cron_search({"assistant_id": str(asst), "enabled": True})); assert [x["cron_id"] for x in r] == [c["cron_id"]]
            r = await as_user(admin, a["ws"], lambda: server.cron_search({"assistant_id": str(asst), "limit": 1, "offset": 1, "sort_by": "created_at", "sort_order": "asc"}))
            assert [x["cron_id"] for x in r] == [c2["cron_id"]]
            r = await as_user(admin, a["ws"], lambda: server.cron_search({"assistant_id": str(asst), "sort_by": "next_run_date", "sort_order": "asc"})); assert r[-1]["next_run_date"] is None
            assert await as_user(admin, a["ws"], lambda: server.cron_count({"assistant_id": str(asst)})) == 2
            assert await as_user(admin, a["ws"], lambda: server.cron_count({"assistant_id": str(asst), "enabled": False})) == 1
            # workspace B sees nothing of A's
            for fn in (lambda: server.cron_get(cid), lambda: server.cron_patch(cid, {"enabled": False})):
                with pytest.raises(HTTPException) as e: await as_user(admin, b["ws"], fn)
                assert e.value.status_code == 404
            assert await as_user(admin, b["ws"], lambda: server.cron_search({})) == []
            assert await as_user(admin, b["ws"], lambda: server.cron_count({})) == 0
        finally:
            await A.q("DELETE FROM lg_crons WHERE assistant_id=%s", (asst,)); await A.q("DELETE FROM lg_assistants WHERE assistant_id=%s", (asst,))
    with_db28(go)

def test_runner_respects_end_time_and_delete_flag(monkeypatch):
    import server
    calls = []
    base = {"cron_id": uuid.uuid4(), "assistant_id": uuid.uuid4(), "thread_id": None, "schedule": "* * * * *", "timezone": "UTC", "run_once_at": None, "last_run": None,
            "input": {}, "webhook": None, "workspace_id": 1, "end_time": None, "on_run_completed": "delete"}
    rows = [dict(base, end_time=dt.datetime(2000, 1, 1, tzinfo=U), cron_id=uuid.uuid4()), dict(base)]
    async def q(sql, args=(), one=False):
        calls.append((sql, args))
        if sql.startswith("SELECT * FROM lg_crons"): return rows
        if sql.startswith("SELECT * FROM lg_assistants"): return {"agent_id": 1, "assistant_id": base["assistant_id"]}
        return None
    monkeypatch.setattr(server.A, "q", q); monkeypatch.setattr(server, "_sr", lambda rid, k, v, ws=None: v)
    monkeypatch.delenv("AOS_SCHEDULES_DISABLED", raising=False)
    asyncio.run(server._crons())
    ins = [a for s, a in calls if s.startswith("INSERT INTO lg_runs")]
    assert len(ins) == 1 and ins[0][-1] is True        # expired cron skipped; 'delete' -> temporary run (thread removed at completion)
