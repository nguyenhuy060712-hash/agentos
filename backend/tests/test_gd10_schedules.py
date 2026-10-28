"""GĐ10.6 schedules: UTC matching with per-schedule timezone, one-shot, is_enabled alias, global off switch, migration 0005.
In-process (no server): is_due with fixed datetimes; migration on a throwaway schema."""
import asyncio, datetime as dt, inspect, os, sys, uuid
import psycopg, pytest
APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls"); os.environ.setdefault("OPENAI_API_KEY", "sk-test-never-used")   # read at import; no call is made
import deepagent_api  # noqa: E402,F401  (must load first: evals <-> deepagent_api circular import)
import evals, migrations as M  # noqa: E402

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
U = dt.timezone.utc
def at(h, m, d=26): return dt.datetime(2026, 10, d, h, m, 5, tzinfo=U)

def test_tz_utc_vs_ict():
    assert evals.is_due("6 18 * * *", "UTC", None, at(18, 6))
    assert not evals.is_due("6 18 * * *", "UTC", None, at(11, 6))
    assert evals.is_due("6 18 * * *", "Asia/Ho_Chi_Minh", None, at(11, 6))     # 18:06 ICT = 11:06 UTC
    assert not evals.is_due("6 18 * * *", "Asia/Ho_Chi_Minh", None, at(18, 6))

def test_default_tz_is_ict_for_old_rows():
    assert evals.is_due("0 9 * * *", None, None, at(2, 0))           # NULL/absent tz -> ICT
    assert evals.is_due("0 9 * * *", "Asia/Ho_Chi_Minh", None, at(2, 1))   # previous-minute tolerance

def test_last_run_guard_and_naive_last():
    assert not evals.is_due("* * * * *", "UTC", None, at(10, 1), at(10, 0))
    assert evals.is_due("* * * * *", "UTC", None, at(10, 5), at(10, 0))
    assert not evals.is_due("* * * * *", "UTC", None, at(10, 1), dt.datetime(2026, 10, 26, 10, 0))   # naive = UTC

def test_one_shot_fires_once():
    ro = at(12, 0)
    assert not evals.is_due("@once", "UTC", ro, at(11, 59))
    assert evals.is_due("@once", "UTC", ro, at(12, 0))
    assert evals.is_due("@once", "UTC", ro, at(12, 30))                 # late tick still fires
    assert not evals.is_due("@once", "UTC", ro, at(12, 31), at(12, 1))  # already ran
    assert not evals.is_due("@once", "UTC", None, at(12, 0))

def test_norm_schedule():
    assert evals.norm_schedule("*/5 * * * *") == ("*/5 * * * *", "Asia/Ho_Chi_Minh", None)
    c, tz, ro = evals.norm_schedule("x", "UTC", "2026-10-26T19:00:00+07:00")
    assert (c, tz, ro) == ("@once", "UTC", dt.datetime(2026, 10, 26, 12, 0, tzinfo=U))
    assert evals.norm_schedule("x", None, "2026-10-26T12:00:00")[2].tzinfo is not None
    for bad in (("* * * *", None, None), ("* * * * *", "Mars/Base", None), ("@once", None, None), ("x", None, "not-a-date")):
        with pytest.raises(ValueError): evals.norm_schedule(*bad)

def test_patch_is_enabled_alias_signature():
    import studio
    for f in (evals.sched_toggle, studio.cron_toggle):
        assert "is_enabled" in inspect.signature(f).parameters and "enabled" in inspect.signature(f).parameters
    src = inspect.getsource(__import__("server").cron_patch)
    assert "is_enabled" in src

def test_global_off_switch(monkeypatch):
    import server, studio
    async def boom(*a, **k): raise AssertionError("DB touched while schedules are disabled")
    for mod in (evals.A, ):
        monkeypatch.setattr(mod, "q", boom)
    monkeypatch.setenv("AOS_SCHEDULES_DISABLED", "1")
    assert evals.schedules_disabled()
    asyncio.run(evals.run_schedules()); asyncio.run(server._crons()); asyncio.run(studio.run_crons())
    monkeypatch.delenv("AOS_SCHEDULES_DISABLED"); assert not evals.schedules_disabled()

def test_runner_one_shot_disables(monkeypatch):
    """run_schedules: due one-shot -> _fire then enabled=false; recurring not due -> nothing."""
    calls = []
    row = {"id": 1, "cron": "@once", "timezone": "UTC", "run_once_at": dt.datetime(2000, 1, 1, tzinfo=U), "last_run": None}
    async def q(sql, args=(), one=False):
        calls.append(sql); return [row] if sql.startswith("SELECT") else None
    async def fire(s): calls.append("FIRE")
    monkeypatch.setattr(evals.A, "q", q); monkeypatch.setattr(evals, "_fire", fire)
    asyncio.run(evals.run_schedules())
    assert "FIRE" in calls and any("enabled=false" in c for c in calls)

def test_migration_0005_up_down():
    sch = "t1006_" + uuid.uuid4().hex[:8]
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
            for t in M_TABLES: await c.execute(f"CREATE TABLE {t}(id int, cron text)")
            await c.execute("INSERT INTO lg_crons VALUES(1,'0 9 * * *')")
            mod = [m for v, m, _ in M._load() if v == "0005"][0]
            q = M._q(c)
            await mod.up(q); await mod.up(q)    # idempotent
            row = await (await c.execute("SELECT timezone, run_once_at FROM lg_crons")).fetchone()
            assert row == ("Asia/Ho_Chi_Minh", None)
            for t in M_TABLES: await c.execute(f"SELECT timezone, run_once_at FROM {t}")
            await mod.down(q)
            cols = await (await c.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema=%s AND column_name IN ('timezone','run_once_at')", (sch,))).fetchone()
            assert cols[0] == 0
    M_TABLES = ("lg_crons", "agent_crons", "eval_schedules")
    with psycopg.connect(PG, autocommit=True) as c: c.execute(f'CREATE SCHEMA "{sch}"')
    try: asyncio.run(go())
    finally:
        with psycopg.connect(PG, autocommit=True) as c: c.execute(f'DROP SCHEMA "{sch}" CASCADE')
