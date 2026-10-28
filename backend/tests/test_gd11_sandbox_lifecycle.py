"""GĐ11.42 lifecycle, capacity gate, reconcile, usage metering, purge hooks (app/sbx_runtime.py + migration 0052).
FakeDriver / MemStore / fake clock / fake MemAvailable: no Docker. PgStore + migration run on a throw-away schema of the local Postgres."""
import asyncio, os, shutil, sys, time, uuid
import psycopg, pytest
from psycopg.rows import dict_row

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")
import sbx_runtime as R  # noqa: E402
import capacity  # noqa: E402
import migrations as M  # noqa: E402

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
run = asyncio.run
T0 = 1_800_000_000.0

def ctx(ws=1, uid=10, role="editor", demo=False, scope="full"): return {"ws": ws, "uid": uid, "role": role, "demo": demo, "scope": scope}

class Clock:
    def __init__(self): self.t = time.time()
    def __call__(self): return self.t

def mk(clock=None, **kw):
    drv, store, clock = R.FakeDriver(), R.MemStore(), clock or Clock()
    return R.Computers(store, drv, disk_probe=lambda: 10, clock=clock, **kw), drv, store, clock

@pytest.fixture(autouse=True)
def roomy(monkeypatch):
    """default: plenty of RAM and slots, so only the tests that care about capacity see a limit"""
    monkeypatch.setattr(capacity, "mem_available_mb", lambda: 64_000)
    monkeypatch.setenv("AOS_COMPUTER_MAX_RUNNING", "8"); monkeypatch.setenv("AOS_BOX_MAX_RUNNING", "10")
    monkeypatch.delenv("AOS_BOX_IDLE_TTL_S", raising=False)

def age(st, i, seconds, clock):
    """pretend the box was last active `seconds` ago"""
    st.rows[i]["last_active"] = clock() - seconds

# ------------------------------------------------------------------ idle stop + wake
def test_idle_box_stops_after_10_minutes_and_the_next_command_wakes_it_with_files_intact():
    c, drv, st, clk = mk()
    async def go():
        r = await c.create(ctx()); i = r["id"]
        await c.exec(ctx(), i, "write /workspace/keep.txt hello")
        age(st, i, 599, clk); assert await c.reap_tick() == [] and st.rows[i]["status"] == "running"      # not yet
        age(st, i, 601, clk); ev = await c.reap_tick(); assert ev == [{"id": i, "event": "idle_stop"}]
        assert st.rows[i]["status"] == "stopped" and drv.boxes[i]["state"] == "exited"
        out = await c.exec(ctx(), i, "cat /workspace/keep.txt"); assert out["output"] == "hello"            # woke up by itself, files intact
        assert st.rows[i]["status"] == "running" and "idle_stop" not in st.rows[i]["host_state"]
        attrs = R.SPAN_ATTRS.get(); assert attrs["sandbox.id"] == i and attrs["sandbox.status_before"] == "stopped" and attrs["sandbox.exit_code"] == 0 and attrs["sandbox.kind"] == "computer"
        await c.stop(ctx(), i)                                                                              # a USER stop stays stopped
        with pytest.raises(R.BadState): await c.exec(ctx(), i, "echo x")
    run(go())

def test_put_and_get_file_also_wake_an_idle_box_but_a_viewer_read_does_not():
    c, drv, st, clk = mk()
    async def go():
        i = (await c.create(ctx()))["id"]; await c.put_file(ctx(), i, "/workspace/a.txt", b"A")
        age(st, i, 700, clk); await c.reap_tick(); assert st.rows[i]["status"] == "stopped"
        with pytest.raises(R.BadState): await c.get_file(ctx(role="viewer", uid=10), i, "/workspace/a.txt")   # viewers cannot start a box
        assert st.rows[i]["status"] == "stopped"
        assert await c.get_file(ctx(), i, "/workspace/a.txt") == b"A" and st.rows[i]["status"] == "running"
        age(st, i, 700, clk); await c.reap_tick(); await c.put_file(ctx(), i, "/workspace/b.txt", b"B"); assert st.rows[i]["status"] == "running"
    run(go())

def test_never_stopped_while_a_command_runs_and_custom_ttl_is_respected():
    c, drv, st, clk = mk()
    async def go():
        i = (await c.create(ctx(), idle_ttl_s=3600))["id"]; j = (await c.create(ctx()))["id"]
        age(st, i, 1000, clk); age(st, j, 5000, clk)
        c._busy[j] = 1                                                                                      # a command is running in j
        assert await c.reap_tick() == [] and st.rows[i]["status"] == st.rows[j]["status"] == "running"
        c._busy.clear(); assert [e["id"] for e in await c.reap_tick()] == [j]
        age(st, i, 3601, clk); assert [e["id"] for e in await c.reap_tick()] == [i]
    run(go())

def test_env_default_idle_ttl(monkeypatch):
    monkeypatch.setenv("AOS_BOX_IDLE_TTL_S", "120")
    c, drv, st, clk = mk()
    async def go():
        i = (await c.create(ctx()))["id"]; age(st, i, 121, clk); assert [e["id"] for e in await c.reap_tick()] == [i]
    run(go())

def test_slow_start_is_retried_once():
    c, drv, st, clk = mk(start_timeout=0.05)
    calls = []
    orig = drv.start
    async def slow(id, wait_boot=False):
        calls.append(1)
        if len(calls) == 1: await asyncio.sleep(1)       # the first wake-up attempt is too slow, the retry is fast
        await orig(id, wait_boot)
    async def go():
        i = (await c.create(ctx()))["id"]; drv.start = slow
        age(st, i, 700, clk); await c.reap_tick(); await c.exec(ctx(), i, "echo ok"); assert len(calls) == 2
        assert st.rows[i]["status"] == "running"
    run(go())

# ------------------------------------------------------------------ delete after stop
def test_delete_after_stop_but_never_for_shared_agent_boxes_or_failed_ones():
    c, drv, st, clk = mk()
    async def go():
        a = (await c.create(ctx(), delete_after_stop_s=3600))["id"]
        b = (await c.create(ctx(), scope="agent", agent_id=7, delete_after_stop_s=3600))["id"]
        d = (await c.create(ctx()))["id"]; f = (await c.create(ctx(), delete_after_stop_s=3600))["id"]
        for i in (a, b, d): await c.stop(ctx(), i)
        st.rows[f]["status"] = "failed"
        clk.t += 3599; assert await c.reap_tick() == []
        clk.t += 2; ev = await c.reap_tick(); assert ev == [{"id": a, "event": "auto_delete"}]
        assert st.rows[a]["status"] == "deleted" and a not in drv.boxes
        assert st.rows[b]["status"] == "stopped" and b in drv.boxes and st.rows[d]["status"] == "stopped" and st.rows[f]["status"] == "failed"
        clk.t += 90 * 86400; assert await c.reap_tick() == []                                              # shared / unset / failed boxes live on
    run(go())

def test_ttl_arguments_are_validated():
    c, drv, st, clk = mk()
    async def go():
        for kw in ({"delete_after_stop_s": 61}, {"delete_after_stop_s": 31 * 86400}, {"delete_after_stop_s": 0}, {"delete_after_stop_s": True}, {"idle_ttl_s": 10}, {"idle_ttl_s": 40 * 86400}, {"idle_ttl_s": "600"}):
            with pytest.raises(R.SbxError): await c.create(ctx(), **kw)
        assert st.rows == {}
        r = await c.create(ctx(), idle_ttl_s=60, delete_after_stop_s=30 * 86400); assert r["delete_after_stop_s"] == 30 * 86400
    run(go())

# ------------------------------------------------------------------ capacity gate
def test_low_memory_stops_the_least_recently_used_idle_box(monkeypatch):
    c, drv, st, clk = mk()
    async def go():
        a = (await c.create(ctx()))["id"]; b = (await c.create(ctx()))["id"]; d = (await c.create(ctx()))["id"]
        age(st, a, 300, clk); age(st, b, 900, clk); age(st, d, 100, clk)                                  # b is the least recently used
        # a "small" box needs 1024 MB + 512 reserve: free 2000 MB while 3 run, +1024 MB for every box stopped
        monkeypatch.setattr(capacity, "mem_available_mb", lambda: 1200 + 1024 * sum(1 for x in drv.boxes.values() if x["state"] != "running"))
        n = (await c.create(ctx(uid=11)))["id"]
        assert st.rows[b]["status"] == "stopped" and st.rows[b]["host_state"].get("idle_stop") and st.rows[a]["status"] == st.rows[d]["status"] == "running" and st.rows[n]["status"] == "running"
        age(st, n, 0, clk); assert (await c.exec(ctx(), b, "echo back"))["output"] == "back\n"           # the victim wakes on its next call (and stops somebody else)
        assert st.rows[b]["status"] == "running"
    run(go())

def test_no_idle_box_to_stop_gives_429_with_retry_after(monkeypatch):
    c, drv, st, clk = mk()
    async def go():
        a = (await c.create(ctx()))["id"]; c._busy[a] = 1                                                  # the only box runs a command
        monkeypatch.setattr(capacity, "mem_available_mb", lambda: 100)
        with pytest.raises(R.QuotaExceeded) as e: await c.create(ctx())
        assert e.value.status == 429 and e.value.retry_after == 30
        h = R.http_error(e.value); assert h.status_code == 429 and h.headers["Retry-After"] == "30"
        assert len(st.rows) == 1 and st.rows[a]["status"] == "running"                                    # nothing half-created, nothing stopped
    run(go())

def test_max_running_from_env_and_from_ram(monkeypatch):
    c, drv, st, clk = mk()
    async def go():
        monkeypatch.setenv("AOS_COMPUTER_MAX_RUNNING", "1")
        a = (await c.create(ctx()))["id"]
        b = (await c.create(ctx(uid=11)))["id"]                                                            # a is idle -> stopped to make room
        assert st.rows[a]["status"] == "stopped" and st.rows[b]["status"] == "running"
        c._busy[b] = 1
        with pytest.raises(R.QuotaExceeded): await c.start(ctx(), a)                                      # b is busy: 429
        c._busy.clear()
        await c.start(ctx(), a); assert st.rows[b]["status"] == "stopped" and st.rows[a]["status"] == "running"
    run(go())
    monkeypatch.delenv("AOS_COMPUTER_MAX_RUNNING")
    for mb, want in ((3800, 1), (7600, 3), (15500, 8), (31000, 8)):
        monkeypatch.setattr(R, "_mem_total_mb", lambda mb=mb: mb); assert R.max_running_default() == want
    monkeypatch.setenv("AOS_COMPUTER_MAX_RUNNING", "abc"); monkeypatch.setattr(R, "_mem_total_mb", lambda: 7600); assert R.max_running_default() == 3

def test_workspace_switch_and_running_quota_apply_to_every_start():
    c, drv, st, clk = mk()
    async def go():
        i = (await c.create(ctx()))["id"]; await c.stop(ctx(), i)
        st.quotas[1] = {"enabled": False, "max_total": 10, "max_running": 3}
        with pytest.raises(R.Forbidden, match="Workspace này chưa bật máy tính ảo"): await c.start(ctx(), i)
        with pytest.raises(R.Forbidden, match="chưa bật"): await c.create(ctx())
        st.quotas[1] = {"enabled": True, "max_total": 10, "max_running": 1}
        j = (await c.create(ctx()))["id"]
        with pytest.raises(R.QuotaExceeded): await c.start(ctx(), i)                                      # 1 already running in this workspace
        st.rows[j]["status"] = "stopped"; assert (await c.start(ctx(), i))["status"] == "running"
    run(go())

def test_demo_account_still_cannot_wake_or_create():
    c, drv, st, clk = mk()
    async def go():
        i = (await c.create(ctx()))["id"]; age(st, i, 700, clk); await c.reap_tick()
        with pytest.raises(R.Forbidden): await c.exec(ctx(demo=True), i, "echo x")
        assert st.rows[i]["status"] == "stopped"
    run(go())

# ------------------------------------------------------------------ reconcile
def test_reconcile_marks_missing_boxes_failed_and_never_replaces_them():
    c, drv, st, clk = mk()
    async def go():
        a = (await c.create(ctx()))["id"]; b = (await c.create(ctx()))["id"]; d = (await c.create(ctx()))["id"]
        drv.boxes.pop(a)                                                                                   # container gone (docker prune, reinstall ...)
        drv.boxes[b]["state"] = "exited"                                                                   # host rebooted
        st.rows["p" * 16] = {"id": "p" * 16, "workspace_id": 1, "status": "provisioning", "last_active": clk() - 500, "core": "fake", "host_state": {}}
        ev = await c.reconcile()
        assert {(e["id"], e["event"]) for e in ev} == {(a, "failed"), (b, "stopped"), ("p" * 16, "failed")}
        assert st.rows[a]["status"] == "failed" and "gone" in st.rows[a]["last_error"] and "new computer" in st.rows[a]["last_error"]
        assert st.rows[b]["status"] == "stopped" and st.rows[d]["status"] == "running" and len(drv.boxes) == 2    # nothing was created in a's place
        with pytest.raises(R.BadState): await c.exec(ctx(), a, "echo x")                                   # a failed box does not wake up by itself
    run(go())

def test_reconcile_unreachable_driver_marks_failed_with_reason():
    c, drv, st, clk = mk()
    async def go():
        a = (await c.create(ctx()))["id"]
        async def boom(id): raise R.BoxFailed("docker daemon not running")
        drv.inspect = boom; await c.reconcile()
        assert st.rows[a]["status"] == "failed" and "cannot be reached" in st.rows[a]["last_error"]
    run(go())

# ------------------------------------------------------------------ usage metering
def test_sampler_writes_hourly_usage_rows():
    c, drv, st, clk = mk()
    async def go():
        a = (await c.create(ctx()))["id"]; clk.t = T0 + 10
        async def stats(id): return {"cpu_percent": "50.0%", "memory": "100MiB / 1GiB", "pids": "3"}
        drv.stats = stats
        assert await c.sample_tick() == [a]; clk.t += 60; await c.sample_tick(); clk.t += 60; await c.sample_tick()
        [u] = st.usage.values(); assert u["sandbox_id"] == a and u["workspace_id"] == 1 and u["hour"] == int(T0 // 3600 * 3600)
        assert u["running_seconds"] == 60 + 60 + 60 and abs(u["cpu_seconds"] - 0.5 * 180) < 1e-6 and u["mem_byte_seconds"] == 100 * 1024 ** 2 * 180
        clk.t += 3600; await c.sample_tick(); assert len(st.usage) == 2                                  # next hour = next row
        await c.stop(ctx(), a); n = len(st.usage); await c.sample_tick(); assert len(st.usage) == n and a not in c._sample_t
    run(go())

def test_disk_watchdog_records_peak_disk_per_hour():
    c, drv, st, clk = mk()
    async def go():
        a = (await c.create(ctx()))["id"]; clk.t = T0
        drv.disk[a] = 100 * 1024 ** 2; await c.disk_tick(); drv.disk[a] = 40 * 1024 ** 2; await c.disk_tick()
        [u] = st.usage.values(); assert u["disk_bytes_max"] == 100 * 1024 ** 2 and u["cpu_seconds"] == 0
    run(go())

def test_parse_stats():
    assert R.parse_stats({"cpu_percent": "12.5%", "memory": "10MiB / 1GiB"}) == (12.5, 10 * 1024 ** 2)
    assert R.parse_stats({}) == (0.0, 0) and R.parse_stats({"cpu_percent": "--", "memory": "--"}) == (0.0, 0)

# ------------------------------------------------------------------ purge hooks
def test_purge_for_agent_removes_boxes_rows_dirs_commands_and_agent_secrets(tmp_path):
    c, drv, st, clk = mk(sbx_root=tmp_path)
    async def go():
        a = (await c.create(ctx(), agent_id=5))["id"]; b = (await c.create(ctx(), agent_id=5, scope="agent"))["id"]; o = (await c.create(ctx(), agent_id=6))["id"]
        await c.exec(ctx(), a, "echo hi"); assert st.cmds
        st.rows["f" * 12] = {"id": "f" * 12, "kind": "files", "workspace_id": 1, "agent_id": 5, "owner_id": 10, "status": "running", "core": "gvisor", "host_state": {}}
        (tmp_path / ("f" * 12)).mkdir(); (tmp_path / ("f" * 12) / "x.txt").write_text("x")
        st.identifiers[5] = "sales-bot"
        st.secrets[:] = [(1, "agent:sales-bot/API_KEY"), (1, "agent:5/OTHER"), (1, "agent:other-bot/API_KEY"), (1, "OPENAI_API_KEY"), (2, "agent:sales-bot/API_KEY")]
        seen = []
        async def snap_hook(aid, ws): seen.append((aid, ws)); return {"snapshots": 2}
        saved = list(R.PURGE_HOOKS); R.PURGE_HOOKS[:] = [snap_hook]       # isolate from the real hooks (11.47a sbx_images registers one)
        try: res = await c.purge_for_agent(5, workspace_id=1)
        finally: R.PURGE_HOOKS[:] = saved
        assert res["boxes"] == 3 and res["errors"] == [] and res["secrets"] == 2 and res["extra"] == {"snap_hook": {"snapshots": 2}} and seen == [(5, 1)]
        assert set(st.rows) == {o} and a not in drv.boxes and b not in drv.boxes and o in drv.boxes and st.cmds == []
        assert not (tmp_path / ("f" * 12)).exists()
        assert st.secrets == [(1, "agent:other-bot/API_KEY"), (1, "OPENAI_API_KEY"), (2, "agent:sales-bot/API_KEY")]
        assert (await c.purge_for_agent(5, workspace_id=1))["boxes"] == 0                                 # repeatable
    run(go())

def test_purge_keeps_the_row_when_the_container_cannot_be_removed():
    c, drv, st, clk = mk()
    async def go():
        a = (await c.create(ctx(), agent_id=5))["id"]
        async def boom(id): raise R.BoxFailed("docker down")
        good = drv.delete; drv.delete = boom
        res = await c.purge_for_agent(5); assert res["boxes"] == 0 and res["errors"][0]["id"] == a and a in st.rows
        drv.delete = good; assert (await c.purge_for_agent(5))["boxes"] == 1 and st.rows == {}
    run(go())

def test_purge_needs_an_agent_or_an_owner_and_never_wipes_everything():
    st = R.MemStore()
    with pytest.raises(ValueError): run(st.purge_rows())
    c, drv, st, clk = mk()
    async def go():
        a = (await c.create(ctx(uid=10)))["id"]; b = (await c.create(ctx(uid=11)))["id"]
        res = await c.purge_for_user(10, 1); assert res["boxes"] == 1 and set(st.rows) == {b} and a not in drv.boxes
        assert (await c.purge_for_user(10, 2))["boxes"] == 0
    run(go())

def test_privacy_erasure_goes_through_the_same_purge_and_loop_is_registered():
    src = open(os.path.join(APP, "privacy.py"), encoding="utf-8").read()
    assert "purge_for_user(uid, scope_ws)" in src and "DELETE FROM sandboxes" not in src
    s2 = open(os.path.join(APP, "sandbox2.py"), encoding="utf-8").read()
    assert "sbx_runtime.reaper_loop()" in s2 and s2.index("AOS_ROLE") < s2.index("reaper_loop()")        # only in the API process, not the worker
    import deepagent_api, admin   # noqa: F401
    assert "sandbox_usage_hourly" in admin.RLS_DIRECT
    assert "0052" in [v for v, _, _ in M._load()]

# ------------------------------------------------------------------ migration 0052 + PgStore (throw-away schema)
@pytest.fixture
def pg(tmp_path, monkeypatch):
    for f in ("0050_sandbox_computer.py", "0052_sandbox_usage.py"): shutil.copy(os.path.join(APP, "migrations", f), tmp_path / f)
    monkeypatch.setattr(M, "DIR", tmp_path)
    sch = "t1142_" + uuid.uuid4().hex[:8]
    def runner(fn, *a, **kw):
        async def go():
            async with await psycopg.AsyncConnection.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c: return await fn(c, *a, **kw)
        return asyncio.run(go())
    with psycopg.connect(PG, autocommit=True) as c: c.execute(f'CREATE SCHEMA "{sch}"')
    yield runner
    with psycopg.connect(PG, autocommit=True) as c: c.execute(f'DROP SCHEMA "{sch}" CASCADE')

def test_migration_0052_roundtrip_and_pgstore_usage_purge(pg):
    def mkq(c):
        async def q(sql, args=(), one=False):
            cur = c.cursor(row_factory=dict_row); await cur.execute(sql, args)
            if cur.description is None: return None
            return await (cur.fetchone() if one else cur.fetchall())
        return q
    async def go(c):
        assert await M.run_pending(c) == ["0050 sandbox_computer: applied", "0052 sandbox_usage: applied"]
        s = R.PgStore(mkq(c)); assert s.system().q is not s.q
        i, j = R.new_id(), R.new_id()
        await s.insert({"id": i, "name": "n", "workspace_id": 5, "owner_id": 9, "status": "running", "size": "small", "image": "img", "core": "gvisor", "scope": "thread", "agent_id": 3})
        await s.insert({"id": j, "name": "n", "workspace_id": 5, "owner_id": 8, "status": "stopped", "size": "small", "image": "img", "core": "gvisor", "scope": "agent", "agent_id": 4})
        assert [r["id"] for r in await s.list_active()] == [i] and [r["id"] for r in await s.list_stopped()] == [j]
        await s.add_usage(3600, 5, i, 1.5, 100.0, 60, 10); await s.add_usage(3600, 5, i, 0.5, 50.0, 60, 4); await s.add_usage(7200, 5, i, 0, 0, 0, 7)
        rows = await (await c.execute("SELECT hour, cpu_seconds, mem_byte_seconds, running_seconds, disk_bytes_max FROM sandbox_usage_hourly ORDER BY hour")).fetchall()
        assert [(r[1], r[2], r[3], r[4]) for r in rows] == [(2.0, 150.0, 120.0, 10), (0.0, 0.0, 0.0, 7)]       # sums add up, disk keeps the peak
        await c.execute("INSERT INTO sandbox_commands(id,sandbox_id,workspace_id,command) VALUES('c1',%s,5,'x')", (i,))
        assert [r["id"] for r in await s.purge_rows(agent_id=3)] == [i] and await s.purge_rows(agent_id=3, workspace_id=6) == [] and [r["id"] for r in await s.purge_rows(owner_id=8)] == [j]
        with pytest.raises(ValueError): await s.purge_rows()
        await c.execute("CREATE TABLE IF NOT EXISTS ws_secrets(workspace_id INT, name TEXT)")
        await c.execute("INSERT INTO ws_secrets VALUES (5,'agent:bot/K'),(5,'agent:bot/L'),(5,'agent:bots/K'),(6,'agent:bot/K'),(5,'agent_bot/K'),(5,'agent:100%/K')")
        assert await s.purge_secrets(5, ["agent:bot/"]) == 2 and await s.purge_secrets(None, ["agent:100%/"]) == 1
        assert {r[0] for r in await (await c.execute("SELECT name FROM ws_secrets")).fetchall()} == {"agent:bot/K", "agent:bots/K", "agent_bot/K"}
        assert await s.agent_identifier(3) is None                                                         # no agents table in this schema -> None, no crash
        await s.purge_row(i); assert (await (await c.execute("SELECT count(*) FROM sandbox_commands")).fetchone())[0] == 0
        assert (await (await c.execute("SELECT count(*) FROM sandbox_usage_hourly")).fetchone())[0] == 2    # accounting stays
        assert await M.rollback(c, 1) == ["0052 sandbox_usage: rolled back"]
        assert "sandbox_usage_hourly" not in [r[0] for r in await (await c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema()")).fetchall()]
    pg(go)
