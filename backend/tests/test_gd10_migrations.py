"""GĐ10.29 ordered migrations. Pure DB test (no server): temp migrations dir (monkeypatched) + a throwaway schema, so the real
schema_migrations rows / tables are never touched."""
import asyncio, os, subprocess, sys, textwrap, uuid
import psycopg, pytest

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)
import migrations as M  # noqa: E402

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")

def mig(ver, name, up="", down="", plan=None):
    pl = f"\nasync def plan(q): return {plan!r}\n" if plan else ""
    return textwrap.dedent(f'''
        VERSION = "{ver}"
        NAME = "{name}"
        async def up(q): {up or "pass"}
        async def down(q): {down or "pass"}
        ''') + pl

@pytest.fixture
def env(tmp_path, monkeypatch):
    """(folder, run) - run(coro_fn, *a) executes against a fresh schema on an autocommit connection."""
    monkeypatch.setattr(M, "DIR", tmp_path)
    sch = "t1029_" + uuid.uuid4().hex[:8]
    def run(fn, *a, **kw):
        async def go():
            async with await psycopg.AsyncConnection.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
                return await fn(c, *a, **kw)
        return asyncio.run(go())
    with psycopg.connect(PG, autocommit=True) as c: c.execute(f'CREATE SCHEMA "{sch}"')
    yield tmp_path, run
    with psycopg.connect(PG, autocommit=True) as c: c.execute(f'DROP SCHEMA "{sch}" CASCADE')

def put(d, ver, name, **kw): (d / f"{ver}_{name}.py").write_text(mig(ver, name, **kw), encoding="utf-8")
async def rows(c, sql): return await (await c.execute(sql)).fetchall()
async def versions(c): return [r[0] for r in await rows(c, "SELECT version FROM schema_migrations ORDER BY version")]
async def exists(c, t): return (await (await c.execute("SELECT to_regclass(%s)", (t,))).fetchone())[0] is not None

T1 = 'await q("CREATE TABLE t_one(id int)")'
D1 = 'await q("DROP TABLE t_one")'
T2 = 'await q("CREATE TABLE t_two(id int)")'

def test_real_baseline_file_is_valid():
    assert [v for v, _, _ in M._load()][0] == "0000"

def test_apply_in_order_and_idempotent(env):
    d, run = env
    put(d, "0000", "baseline"); put(d, "0002", "two", up=T2); put(d, "0001", "one", up=T1)
    out = run(M.run_pending)
    assert [l.split()[0] for l in out] == ["0000", "0001", "0002"]
    assert run(versions) == ["0000", "0001", "0002"] and run(exists, "t_one") and run(exists, "t_two")
    assert run(M.run_pending) == []                      # re-boot applies nothing
    assert [r["applied"] for r in run(M.status)] == [True, True, True]

def test_failed_migration_rolls_back_and_stops(env):
    d, run = env
    put(d, "0001", "one", up=T1); put(d, "0002", "bad", up='await q("CREATE TABLE t_half(id int)"); await q("SELECT 1/0")'); put(d, "0003", "three", up=T2)
    with pytest.raises(Exception): run(M.run_pending)
    assert run(versions) == ["0001"] and not run(exists, "t_half") and not run(exists, "t_two")   # 0002 fully undone, 0003 never ran

def test_dry_run_changes_nothing_and_lists_steps(env):
    d, run = env
    put(d, "0001", "one", up=T1, plan=["create table t_one"]); put(d, "0002", "noplan", up=T2)
    out = run(M.run_pending, dry_run=True)
    assert out == ["0001 one: create table t_one", "0002 noplan: (no plan given)"]
    async def nothing(c): return (await exists(c, "schema_migrations"), await exists(c, "t_one"), await exists(c, "t_two"))
    assert run(nothing) == (False, False, False)         # not even the bookkeeping table
    run(M.run_pending)
    assert run(M.run_pending, dry_run=True) == []

def test_rollback_and_roundtrip(env):
    d, run = env
    put(d, "0001", "one", up=T1, down=D1); put(d, "0002", "two", up=T2, down='await q("DROP TABLE t_two")')
    run(M.run_pending)
    assert run(M.rollback) == ["0002 two: rolled back"]
    assert run(versions) == ["0001"] and not run(exists, "t_two") and run(exists, "t_one")
    assert run(M.rollback, 5) == ["0001 one: rolled back"] and run(versions) == [] and not run(exists, "t_one")
    assert run(M.rollback) == []
    assert len(run(M.run_pending)) == 2 and run(exists, "t_one") and run(exists, "t_two")   # down -> up round-trips

def test_rollback_failure_keeps_row(env):
    d, run = env
    put(d, "0001", "one", up=T1, down='await q("DROP TABLE nope")')
    run(M.run_pending)
    with pytest.raises(Exception): run(M.rollback)
    assert run(versions) == ["0001"] and run(exists, "t_one")

def test_tampered_checksum_fails_loudly(env):
    d, run = env
    put(d, "0001", "one", up=T1); put(d, "0002", "two", up=T2)
    run(M.run_pending)
    p = d / "0001_one.py"; p.write_text(p.read_text(encoding="utf-8") + "\n# sneaky edit\n", encoding="utf-8")
    for fn in (M.run_pending, M.rollback, M.status):
        with pytest.raises(M.MigrationError, match="checksum mismatch"): run(fn)
    assert run(versions) == ["0001", "0002"]             # nothing was undone or applied

def test_crlf_does_not_change_checksum(env):
    d, run = env
    put(d, "0001", "one", up=T1); run(M.run_pending)
    p = d / "0001_one.py"; p.write_bytes(p.read_bytes().replace(b"\n", b"\r\n"))
    assert run(M.run_pending) == []

def test_missing_applied_file_fails(env):
    d, run = env
    put(d, "0001", "one", up=T1); run(M.run_pending); (d / "0001_one.py").unlink()
    with pytest.raises(M.MigrationError, match="file is missing"): run(M.run_pending)

def test_bad_files_refused(env):
    d, run = env
    (d / "0001_one.py").write_text(mig("0009", "one"), encoding="utf-8")        # VERSION != prefix
    with pytest.raises(M.MigrationError, match="does not match"): run(M.run_pending)
    (d / "0001_one.py").unlink(); put(d, "0001", "a"); put(d, "0001", "b")      # duplicate version
    with pytest.raises(M.MigrationError, match="duplicate"): run(M.run_pending)

def test_cli_status_and_dry_run_use_pg_uri_and_report_errors():
    env = {**os.environ, "PG_URI": PG}
    cli = os.path.join(os.path.dirname(__file__), "..", "ops", "migrate.py")
    r = subprocess.run([sys.executable, cli, "status"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "0000  baseline" in r.stdout, r.stderr
    r = subprocess.run([sys.executable, cli, "bogus"], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2
