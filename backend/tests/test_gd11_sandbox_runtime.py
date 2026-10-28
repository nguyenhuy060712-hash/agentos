"""GĐ11.40 virtual-computer foundation (app/sbx_runtime.py + migration 0050). Everything here runs with the FakeDriver / MemStore (no Docker) or a throw-away
Postgres schema; the real gVisor box is tested in test_gd11_sandbox_runtime_real.py (AOS_REAL_SANDBOX=1).
Negative tests first-class: other workspace, other user, viewer / demo / read-only key, no gVisor (runc / fake runtime), unknown core, bad ids / paths, quotas, full disk."""
import asyncio, os, sys, textwrap, uuid
import psycopg, pytest
from psycopg.rows import dict_row

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")   # deepagent_api (imported by admin) wants a key at import; nothing is called
import sbx_runtime as R  # noqa: E402
import migrations as M  # noqa: E402

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
run = asyncio.run

def ctx(ws=1, uid=10, role="editor", demo=False, scope="full"): return {"ws": ws, "uid": uid, "role": role, "demo": demo, "scope": scope}

def mk(disk=10, **kw):
    drv = R.FakeDriver(); store = R.MemStore()
    return R.Computers(store, drv, disk_probe=lambda: disk), drv, store

async def new(c, who=None, **kw): return await c.create(who or ctx(), **kw)

# ------------------------------------------------------------------ state machine
def test_lifecycle_create_exec_stop_start_delete():
    c, drv, st = mk()
    async def go():
        r = await new(c, name="demo"); assert r["status"] == "running" and r["core"] == "fake" and r["kind"] == "computer" and R.ID_RE.match(r["id"])
        assert (await c.exec(ctx(), r["id"], "echo hi"))["output"] == "hi\n"
        assert (await c.stop(ctx(), r["id"]))["status"] == "stopped"
        assert (await c.start(ctx(), r["id"]))["status"] == "running"
        assert await c.delete(ctx(), r["id"]) == {"ok": True} and r["id"] not in drv.boxes
        with pytest.raises(R.NotFound): await c.get(ctx(), r["id"])          # a deleted box is gone for everybody
        assert st.rows[r["id"]]["status"] == "deleted"
    run(go())

def test_transition_table_is_closed():
    assert R.TRANSITIONS["deleted"] == set()
    assert set(R.TRANSITIONS) == set(R.STATES)
    assert "stopped" not in R.TRANSITIONS["provisioning"] and "provisioning" not in R.TRANSITIONS["running"]   # no way back to provisioning

def test_invalid_transitions_are_refused():
    c, _, _ = mk()
    async def go():
        r = await new(c)
        with pytest.raises(R.BadState): await c.start(ctx(), r["id"])         # already running
        await c.stop(ctx(), r["id"])
        with pytest.raises(R.BadState): await c.stop(ctx(), r["id"])          # already stopped
        with pytest.raises(R.BadState): await c.exec(ctx(), r["id"], "echo x")  # exec needs a running box
        with pytest.raises(R.BadState): await c.put_file(ctx(), r["id"], "/workspace/a", b"x")
        with pytest.raises(R.BadState): await c.get_file(ctx(), r["id"], "/workspace/a")
    run(go())

def test_cas_blocks_a_stale_transition():
    s = R.MemStore()
    async def go():
        await s.insert({"id": "a" * 16, "name": "x", "workspace_id": 1, "owner_id": 1, "status": "running", "size": "small", "image": "i", "core": "fake"})
        assert await s.cas("a" * 16, "running", status="stopped") is True
        assert await s.cas("a" * 16, "running", status="failed") is False      # somebody else already moved it
        assert s.rows["a" * 16]["status"] == "stopped"
    run(go())

def test_driver_failure_marks_failed_and_cleans_up():
    c, drv, st = mk(); drv.fail["start"] = True
    async def go():
        with pytest.raises(R.BoxFailed): await new(c)
        [row] = list(st.rows.values()); assert row["status"] == "failed" and "fake start failure" in row["last_error"]
        assert drv.boxes == {}                                                  # half-created box removed
        drv.fail["start"] = False
        assert (await new(c))["status"] == "running"                            # the next box is fine
    run(go())

def test_failed_box_can_be_restarted_or_deleted():
    c, drv, st = mk()
    async def go():
        r = await new(c); i = r["id"]; st.rows[i]["status"] = "failed"
        assert (await c.start(ctx(), i))["status"] == "running"
        st.rows[i]["status"] = "failed"; assert await c.delete(ctx(), i) == {"ok": True}
    run(go())

# ------------------------------------------------------------------ exec / files
def test_exec_output_cap_exit_code_timeout_and_history():
    c, drv, st = mk()
    async def go():
        r = await new(c); i = r["id"]
        big = await c.exec(ctx(), i, "big 150000")
        assert big["truncated"] is True and len(big["output"]) == R.OUT_CAP
        assert len(drv.boxes[i]["files"][f"/workspace/.aos/out/{big['command_id']}.log"]) == 150000      # the full log stays in the box
        assert (await c.exec(ctx(), i, "exit 7"))["exit_code"] == 7
        t = await c.exec(ctx(), i, "sleep 99", timeout=2); assert t["timed_out"] is True and t["exit_code"] == 137
        assert (await c.exec(ctx(), i, "env FOO", env={"FOO": "bar"}))["output"] == "bar\n"
        assert (await c.exec(ctx(), i, "pwd", cwd="sub/dir"))["output"] == "/workspace/sub/dir\n"
        assert [x["status"] for x in st.cmds] == ["done", "done", "timeout", "done", "done"] and {x["workspace_id"] for x in st.cmds} == {1}
        assert st.cmds[0]["truncated"] is True and all(x["sandbox_id"] == i for x in st.cmds)
    run(go())

@pytest.mark.parametrize("bad", ["", "   ", "x" * (R.MAX_CMD + 1), None, 5])
def test_exec_rejects_bad_commands(bad):
    c, _, _ = mk()
    async def go():
        r = await new(c)
        with pytest.raises(R.SbxError): await c.exec(ctx(), r["id"], bad)
    run(go())

@pytest.mark.parametrize("env", [{"1BAD": "x"}, {"A-B": "x"}, {"A B": "x"}, {"OK": "x\x00"}, {"OK": 5}, {f"K{i}": "v" for i in range(51)}])
def test_exec_rejects_bad_environment(env):
    c, _, _ = mk()
    async def go():
        r = await new(c)
        with pytest.raises(R.SbxError): await c.exec(ctx(), r["id"], "echo x", env=env)
    run(go())

def test_files_roundtrip_survive_stop_start_and_path_rules():
    c, _, _ = mk()
    async def go():
        r = await new(c); i = r["id"]
        await c.put_file(ctx(), i, "notes/a.txt", b"hello"); assert await c.get_file(ctx(), i, "/workspace/notes/a.txt") == b"hello"   # relative = /workspace
        await c.stop(ctx(), i); await c.start(ctx(), i)
        assert await c.get_file(ctx(), i, "/workspace/notes/a.txt") == b"hello"              # durable
        with pytest.raises(R.NotFound): await c.get_file(ctx(), i, "/workspace/missing")
        for bad in ("../etc/passwd", "/workspace/../etc/passwd", "a/../../x", "/proc/1/environ", "/sys/kernel", "/dev/sda", "x\x00y", "a\nb", "/" + "a" * 2000):
            with pytest.raises(R.SbxError): await c.put_file(ctx(), i, bad, b"x")
            with pytest.raises(R.SbxError): await c.get_file(ctx(), i, bad)
    run(go())

def test_put_size_limit(monkeypatch):
    monkeypatch.setattr(R, "PUT_MAX", 10)
    c, _, _ = mk()
    async def go():
        r = await new(c)
        with pytest.raises(R.SbxError): await c.put_file(ctx(), r["id"], "/workspace/big", b"x" * 11)
    run(go())

def test_box_path_normalises():
    assert R.box_path(None) == "/workspace" and R.box_path("a//b/./c") == "/workspace/a/b/c" and R.box_path("//etc/x") == "/etc/x"
    assert R.box_path("/opt/aos-user/venv") == "/opt/aos-user/venv"

# ------------------------------------------------------------------ isolation flags (pure) - the box never gets network, host access or runc
def test_run_args_isolation_flags():
    a = R.run_args(R.BoxSpec("0123456789abcdef", "small", "", 7))
    s = " ".join(a)
    assert a[:2] == ["docker", "create"] and a[a.index("--runtime") + 1] == "runsc" and "runc" not in a
    assert a[a.index("--network") + 1] == "none" and a[a.index("--cap-drop") + 1] == "ALL" and a[a.index("--security-opt") + 1] == "no-new-privileges"
    assert [a[i + 1] for i, x in enumerate(a) if x == "--cap-add"] == ["CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID"]
    assert a[a.index("--pids-limit") + 1] == "256" and a[a.index("--memory") + 1] == "1g" == a[a.index("--memory-swap") + 1] and a[a.index("--cpus") + 1] == "0.5"
    assert a[a.index("--hostname") + 1] == "aos-box-0123456789abcdef" == a[a.index("--name") + 1]
    assert "dev.gvisor.flag.overlay2=none" in a and s.count("dev.gvisor.flag") == 1          # the one allowed override, nothing else
    assert "aos-box-0123456789abcdef-workspace:/workspace" in a and "aos-box-0123456789abcdef-opt:/opt/aos-user" in a and a[-1] == R.IMAGE
    for forbidden in ("--privileged", "--publish", "-p", "--add-host", "--network=host", "--pid", "--ipc", "--device", "--cap-add=ALL", "docker.sock"):
        assert forbidden not in a and forbidden not in s.split()
    assert not any(x.startswith("/") and ":" in x and not x.startswith("aos-box") for x in a)   # no host path is mounted
    m = R.run_args(R.BoxSpec("0123456789abcdef", "medium"))
    assert m[m.index("--memory") + 1] == "2g" and m[m.index("--cpus") + 1] == "1"

def test_run_args_refuses_bad_specs():
    ok = "0123456789abcdef"
    for spec in (R.BoxSpec("../../etc", "small"), R.BoxSpec(ok + "0", "small"), R.BoxSpec("ABCDEF0123456789", "small"), R.BoxSpec(ok, "huge"), R.BoxSpec("", "small"),
                 R.BoxSpec(ok, "small", ro_mounts=(("/srv/skills:/etc", "/pack"),)), R.BoxSpec(ok, "small", ro_mounts=(("/srv/x,rw", "/pack"),)),
                 R.BoxSpec(ok, "small", ro_mounts=(("relative/dir", "/pack"),)), R.BoxSpec(ok, "small", ro_mounts=(("/srv/x", "pack"),))):
        with pytest.raises(R.SbxError): R.run_args(spec)
    assert "/srv/skills:/pack:ro" in R.run_args(R.BoxSpec(ok, "small", ro_mounts=(("/srv/skills", "/pack"),)))      # the server-chosen read-only mount is the only host path

def test_ids_and_names_cannot_inject():
    for bad in ("x", "", "../../etc", "A" * 16, "0123456789abcde", "0123456789abcdef0", "0123456789abcdef; rm -rf /", None, 5):
        with pytest.raises(R.NotFound): R.check_id(bad)
    assert not R.ID_RE.match("0123456789ab")      # legacy 12-hex folder-sandbox ids are never computer ids (and vice versa: admin._sbx_dir wants exactly 12)

# ------------------------------------------------------------------ core selection: never runc, unknown core = "not built"
def test_select_core_requires_gvisor_and_never_falls_back_to_runc(monkeypatch):
    monkeypatch.delenv("AOS_SBX_CORE", raising=False)
    assert R.select_core("computer", {"runtime": "gvisor (runsc)"}) == "gvisor"
    assert R.select_core("files", {"runtime": "docker (runc)"}) is None                    # the old folder sandbox is not affected
    for rt in ("docker (runc)", "fake (local test runner, no Docker)", "", "runc"):
        with pytest.raises(R.CoreUnavailable) as e: R.select_core("computer", {"runtime": rt})
        assert e.value.status == 503 and e.value.retry_after
    with pytest.raises(R.CoreUnavailable): R.select_core("computer", {})
    h = R.http_error(R.CoreUnavailable("x", retry_after=30)); assert h.status_code == 503 and h.headers["Retry-After"] == "30"
    h = R.http_error(R.CoreUnavailable("x")); assert h.headers["Retry-After"] == "30"          # always present on a 503

@pytest.mark.parametrize("val", ["firecracker", "runc", "fake", "kata", "gvisor,runc", "../x"])
def test_select_core_unknown_value_is_core_not_built(monkeypatch, val):
    monkeypatch.setenv("AOS_SBX_CORE", val)
    with pytest.raises(R.CoreNotBuilt) as e: R.select_core("computer", {"runtime": "gvisor (runsc)"})
    assert e.value.status == 501 and "not built" in str(e.value)

def test_select_core_accepts_auto_and_gvisor(monkeypatch):
    for v in ("auto", "gvisor", " GVISOR ", ""):
        monkeypatch.setenv("AOS_SBX_CORE", v); assert R.select_core("computer", {"runtime": "gvisor (runsc)"}) == "gvisor"

def test_get_driver_registry():
    assert set(R.DRIVERS) == {"gvisor", "fake"} and isinstance(R.get_driver("gvisor"), R.DockerGvisorDriver) and isinstance(R.get_driver("fake"), R.FakeDriver)
    with pytest.raises(R.CoreNotBuilt): R.get_driver("firecracker")
    assert R.get_driver("gvisor") is R.get_driver("gvisor")
    caps = R.get_driver("gvisor").capabilities(); assert caps["core"] == "gvisor" and caps["network"] == "none" and caps["durable_root"] and not caps["disk_enforced"]

def test_create_without_gvisor_is_refused_and_stores_nothing(monkeypatch):
    monkeypatch.delenv("AOS_SBX_CORE", raising=False)
    import types
    fake_a = types.SimpleNamespace(SBX_INFO={"runtime": "docker (runc)"}); monkeypatch.setitem(sys.modules, "deepagent_api", fake_a)
    st = R.MemStore(); c = R.Computers(st, None, disk_probe=lambda: 1)       # driver=None -> the core comes from select_core()
    with pytest.raises(R.CoreUnavailable): run(c.create(ctx()))
    assert st.rows == {}

# ------------------------------------------------------------------ permissions (all must be refused)
def test_other_workspace_cannot_touch_a_box():
    c, drv, st = mk()
    async def go():
        r = await new(c, ctx(ws=1, uid=10)); i = r["id"]
        for who in (ctx(ws=2, uid=10), ctx(ws=2, uid=99, role="admin")):    # same user id / even an admin of ANOTHER workspace
            for call in (lambda: c.get(who, i), lambda: c.exec(who, i, "echo x"), lambda: c.stop(who, i), lambda: c.start(who, i), lambda: c.delete(who, i),
                         lambda: c.put_file(who, i, "/workspace/x", b"x"), lambda: c.get_file(who, i, "/workspace/x")):
                with pytest.raises(R.NotFound): await call()                 # 404, not 403: existence is not revealed
            assert await c.list(who) == []
        assert i in drv.boxes and st.rows[i]["status"] == "running" and st.cmds == []     # nothing happened
        a = await new(c, ctx(ws=2, uid=10)); assert a["id"] != i and [x["id"] for x in await c.list(ctx(ws=2, uid=10))] == [a["id"]]
    run(go())

def test_other_user_same_workspace_cannot_touch_a_box_but_admin_can():
    c, _, _ = mk()
    async def go():
        r = await new(c, ctx(uid=10)); i = r["id"]; other = ctx(uid=11)
        with pytest.raises(R.NotFound): await c.get(other, i)
        with pytest.raises(R.NotFound): await c.exec(other, i, "echo x")
        with pytest.raises(R.NotFound): await c.delete(other, i)
        assert await c.list(other) == []
        boss = ctx(uid=1, role="admin")
        assert (await c.get(boss, i))["id"] == i and (await c.exec(boss, i, "echo ok"))["output"] == "ok\n" and len(await c.list(boss)) == 1
        assert (await c.stop(boss, i))["status"] == "stopped"
    run(go())

def test_per_user_box_is_visible_to_that_user():
    c, _, _ = mk()
    async def go():
        r = await new(c, ctx(uid=1, role="admin"), user_id=11)           # box made by an admin for user 11 (e.g. unattended run, shared agent)
        assert (await c.get(ctx(uid=11), r["id"]))["id"] == r["id"] and (await c.exec(ctx(uid=11), r["id"], "echo a"))["exit_code"] == 0
        with pytest.raises(R.NotFound): await c.get(ctx(uid=12), r["id"])
    run(go())

def test_viewer_demo_readonly_key_and_anonymous_are_refused():
    c, drv, st = mk()
    async def go():
        r = await new(c, ctx(uid=10)); i = r["id"]
        for bad in (ctx(uid=10, role="viewer"), ctx(uid=10, demo=True), ctx(uid=10, scope="read")):
            with pytest.raises(R.Forbidden): await c.create(bad)
            with pytest.raises(R.Forbidden): await c.exec(bad, i, "echo x")
            with pytest.raises(R.Forbidden): await c.stop(bad, i)
            with pytest.raises(R.Forbidden): await c.delete(bad, i)
            with pytest.raises(R.Forbidden): await c.put_file(bad, i, "/workspace/x", b"x")
        for anon in (None, {}, {"ws": 1}, {"ws": 1, "uid": None, "role": "admin"}, {"uid": 10, "role": "admin"}):
            with pytest.raises(R.Forbidden): await c.create(anon)
            with pytest.raises(R.Forbidden): await c.get(anon, i)
        with pytest.raises(R.Forbidden): await c.list(ctx(role="nobody"))
        assert st.rows[i]["status"] == "running" and len(st.rows) == 1 and st.cmds == []
        assert (await c.get(ctx(uid=10, role="viewer"), i))["id"] == i            # a viewer who OWNS a box may read it (not change it)
    run(go())

def test_demo_workspace_cannot_create_even_as_admin():
    c, _, st = mk()
    with pytest.raises(R.Forbidden): run(c.create(ctx(role="admin", demo=True)))
    assert st.rows == {}

# ------------------------------------------------------------------ quotas and host disk guard
def test_quotas_and_workspace_switch():
    c, _, st = mk()
    async def go():
        st.quotas[1] = {"enabled": True, "max_running": 1, "max_total": 2}
        a = await new(c)
        with pytest.raises(R.QuotaExceeded): await new(c)                           # running limit
        b = await new(c, start=False); assert b["status"] == "stopped"
        with pytest.raises(R.QuotaExceeded): await new(c, start=False)              # total limit
        assert (await new(c, ctx(ws=2))) ["workspace_id"] == 2                       # another workspace has its own counters
        st.quotas[3] = {"enabled": False, "max_running": 9, "max_total": 9}
        with pytest.raises(R.Forbidden): await new(c, ctx(ws=3))
        await c.delete(ctx(), a["id"]); await new(c)                                # a deleted box frees its slot
    run(go())

def test_default_limits_without_quota_row(monkeypatch):
    monkeypatch.setenv("AOS_BOX_MAX_RUNNING", "2"); monkeypatch.setenv("AOS_BOX_MAX_TOTAL", "3")
    c, _, _ = mk()
    async def go():
        await new(c); await new(c)
        with pytest.raises(R.QuotaExceeded): await new(c)
    run(go())

def test_full_host_disk_refuses_new_boxes_with_retry_after():
    c, drv, st = mk(disk=85.5)
    with pytest.raises(R.Capacity) as e: run(new(c))
    assert e.value.status == 503 and e.value.retry_after and st.rows == {} and drv.boxes == {}
    c2, _, _ = mk(disk=85.0); assert run(new(c2))["status"] == "running"           # the limit is "more than 85 %"

def test_bad_create_arguments():
    c, _, st = mk()
    for kw in ({"size": "huge"}, {"scope": "global"}, {"size": None}):
        with pytest.raises(R.SbxError): run(new(c, **kw))
    assert st.rows == {}

# ------------------------------------------------------------------ disk watchdog
def test_parse_size():
    assert R.parse_size("0B") == 0 and R.parse_size("20.5kB") == 20500 and R.parse_size("1.5GB") == 1_500_000_000 and R.parse_size("12MiB") == 12 * 1024 ** 2
    assert R.parse_size("2.1MB (virtual 74.7MB)") == 2_100_000 and R.parse_size("N/A") == 0 and R.parse_size(None) == 0 and R.parse_size("3") == 3

def test_disk_watchdog_warn_stop_and_clear(monkeypatch):
    monkeypatch.setenv("AOS_BOX_DISK_GB", "1"); G = 1 << 30
    c, drv, st = mk()
    async def go():
        a, b, z = [(await new(c, ctx(uid=u)))["id"] for u in (10, 11, 12)]
        drv.disk.update({a: int(0.5 * G), b: int(0.85 * G)})                         # z has no figure (df did not list it): untouched
        ev = await c.disk_tick(); assert [e["event"] for e in ev] == ["warn"] and ev[0]["id"] == b
        assert st.rows[b]["host_state"]["disk_warn"] is True and st.rows[b]["status"] == "running" and st.rows[a]["host_state"] == {}
        assert await c.disk_tick() == []                                              # warned once only
        drv.disk[b] = int(0.5 * G); await c.disk_tick(); assert "disk_warn" not in st.rows[b]["host_state"]   # back below 80 %: flag cleared
        drv.disk[a] = G                                                              # exactly 100 %
        ev = await c.disk_tick(); assert ev == [{"id": a, "event": "stopped", "used": G}]
        assert st.rows[a]["status"] == "stopped" and st.rows[a]["last_error"] == "disk quota" and drv.boxes[a]["state"] == "exited"
        assert st.rows[z]["status"] == "running" and st.rows[b]["status"] == "running"
        assert await c.disk_tick() == []                                              # a stopped box is not looked at again
        with pytest.raises(R.BadState): await c.exec(ctx(uid=10), a, "echo x")        # and it cannot be used until started again
        assert (await c.start(ctx(uid=10), a))["last_error"] is None
    run(go())

def test_disk_watchdog_survives_driver_errors(monkeypatch):
    monkeypatch.setenv("AOS_BOX_DISK_GB", "1")
    c, drv, st = mk()
    async def go():
        a = (await new(c))["id"]; drv.disk[a] = 2 << 30; drv.fail["stop"] = True
        ev = await c.disk_tick(); assert ev[0]["event"] == "stop_failed" and st.rows[a]["status"] == "running"   # next tick retries
        async def boom(): raise R.BoxFailed("df down")
        drv.disk_usage = boom; ev = await c.disk_tick(); assert ev[0]["event"] == "df_failed"
    run(go())

def test_disk_limit_default_is_5_gb(monkeypatch):
    monkeypatch.delenv("AOS_BOX_DISK_GB", raising=False); assert R.disk_limit_bytes() == 5 * (1 << 30)

def test_disk_tick_does_nothing_without_running_boxes():
    c, drv, _ = mk()
    async def boom(): raise AssertionError("docker must not be called")
    drv.disk_usage = boom; assert run(c.disk_tick()) == []

# ------------------------------------------------------------------ boot verdict -> last_error
def test_refresh_copies_boot_error_without_stopping_the_box():
    c, drv, st = mk()
    async def go():
        i = (await new(c))["id"]; drv.boxes[i]["boot_error"] = "setup.sh failed (exit 3; 137 = 120 s limit)"
        await c.refresh(i); assert st.rows[i]["last_error"].startswith("setup.sh failed") and st.rows[i]["status"] == "running"
        drv.boxes[i]["boot_error"] = ""; await c.refresh(i); assert st.rows[i]["last_error"] is None
        drv.boxes[i]["state"] = "exited"; await c.refresh(i); assert st.rows[i]["status"] == "stopped"      # container died behind our back
    run(go())

# ------------------------------------------------------------------ declarations: RLS + legacy endpoints must not see computers
def test_rls_registration_and_legacy_guards():
    import deepagent_api, admin   # noqa: F401  (deepagent_api first: admin <-> deepagent_api import cycle)
    assert "sandboxes" in admin.RLS_PRIVATE and "sandboxes" in admin.RLS_DIRECT and "workspace_sandbox_quotas" in admin.RLS_DIRECT
    cond = admin.RLS_CHILD["sandbox_commands"]; assert "workspace_id = aos_ws()" in cond and "sandbox_id IN (SELECT id FROM sandboxes)" in cond
    src = open(os.path.join(APP, "admin.py"), encoding="utf-8").read()
    assert "kind='files' AND last_used" in src and 's.get("kind", "files") != "files"' in src       # ttl reaper and folder endpoints leave computers alone

# ------------------------------------------------------------------ migration 0050 + PgStore (throw-away schema)
@pytest.fixture
def pg(tmp_path, monkeypatch):
    """(run, schema): migration 0050 alone on an empty schema; `run(fn, *a)` runs fn(conn, ...) on an autocommit connection of that schema."""
    import shutil
    shutil.copy(os.path.join(APP, "migrations", "0050_sandbox_computer.py"), tmp_path / "0050_sandbox_computer.py")
    monkeypatch.setattr(M, "DIR", tmp_path)
    sch = "t1140_" + uuid.uuid4().hex[:8]
    def runner(fn, *a, **kw):
        async def go():
            async with await psycopg.AsyncConnection.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c: return await fn(c, *a, **kw)
        return asyncio.run(go())
    with psycopg.connect(PG, autocommit=True) as c: c.execute(f'CREATE SCHEMA "{sch}"')
    yield runner
    with psycopg.connect(PG, autocommit=True) as c: c.execute(f'DROP SCHEMA "{sch}" CASCADE')

async def cols(c): return {r[0]: r[1] for r in await (await c.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='sandboxes'")).fetchall()}
async def tables(c): return {r[0] for r in await (await c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema()")).fetchall()}

NEW_COLS = {"kind", "scope", "agent_id", "user_id", "thread_id", "status", "size", "image", "core", "last_error", "host_state", "idle_ttl_s", "delete_after_stop_s", "last_active"}

def test_migration_plan_up_down_roundtrip(pg):
    async def dry(c): return await M.run_pending(c, dry_run=True)
    out = pg(dry); assert len(out) == 3 and all(l.startswith("0050 sandbox_computer:") for l in out)
    async def nothing(c): return "sandboxes" in await tables(c)
    assert pg(nothing) is False                                                      # dry-run changed nothing
    async def up(c): return await M.run_pending(c)
    assert pg(up) == ["0050 sandbox_computer: applied"]
    async def check(c):
        assert NEW_COLS <= set(await cols(c)) and {"sandbox_commands", "workspace_sandbox_quotas"} <= await tables(c)
        await c.execute("INSERT INTO sandboxes(id,name,workspace_id,owner_id) VALUES('legacy000001','old',1,1)")          # a pre-existing folder sandbox keeps working
        r = await (await c.execute("SELECT kind,status,core,host_state,size FROM sandboxes WHERE id='legacy000001'")).fetchone()
        assert r == ("files", "running", "gvisor", {}, None)
    pg(check)
    async def again(c): return await M.run_pending(c)
    assert pg(again) == []                                                           # idempotent
    async def down(c): return await M.rollback(c, 1)
    assert pg(down) == ["0050 sandbox_computer: rolled back"]
    async def gone(c):
        assert not (NEW_COLS & set(await cols(c))) and not ({"sandbox_commands", "workspace_sandbox_quotas"} & await tables(c))
        assert (await (await c.execute("SELECT count(*) FROM sandboxes WHERE id='legacy000001'")).fetchone())[0] == 1     # the old row survived the round trip
    pg(gone); assert pg(up) == ["0050 sandbox_computer: applied"]                    # and up again works

def test_migration_down_refuses_while_a_computer_exists(pg):
    async def go(c):
        await M.run_pending(c)
        await c.execute("INSERT INTO sandboxes(id,name,workspace_id,owner_id,kind,status) VALUES('c0000000000000aa','x',1,1,'computer','running')")
        with pytest.raises(RuntimeError, match="still exist"): await M.rollback(c, 1)
        assert "kind" in await cols(c)                                               # nothing was dropped
        await c.execute("UPDATE sandboxes SET status='deleted' WHERE id='c0000000000000aa'")
        assert await M.rollback(c, 1) == ["0050 sandbox_computer: rolled back"]
    pg(go)

def test_migration_constraints(pg):
    async def go(c):
        await M.run_pending(c)
        for bad in ("kind='vm'", "status='zombie'", "size='huge'", "scope='global'"):
            col, val = bad.split("=")
            with pytest.raises(psycopg.errors.CheckViolation):
                await c.execute(f"INSERT INTO sandboxes(id,workspace_id,{col}) VALUES(%s,1,{val})", (uuid.uuid4().hex,))
        with pytest.raises(psycopg.errors.CheckViolation): await c.execute("INSERT INTO workspace_sandbox_quotas(workspace_id,internet) VALUES(1,'all')")
        await c.execute("INSERT INTO workspace_sandbox_quotas(workspace_id) VALUES(1)")
        assert await (await c.execute("SELECT enabled,max_running,max_total,internet FROM workspace_sandbox_quotas")).fetchone() == (True, 3, 10, "off")
    pg(go)

def test_pgstore_roundtrip_cas_and_cascade(pg):
    def mkq(c):
        async def q(sql, args=(), one=False):
            cur = c.cursor(row_factory=dict_row); await cur.execute(sql, args)
            if cur.description is None: return None
            return await (cur.fetchone() if one else cur.fetchall())
        return q
    async def go(c):
        await M.run_pending(c); s = R.PgStore(mkq(c)); i = R.new_id()
        await s.insert({"id": i, "name": "n", "workspace_id": 5, "owner_id": 9, "status": "provisioning", "size": "small", "image": "img", "core": "gvisor", "scope": "thread", "thread_id": "t1"})
        r = await s.get(i); assert r["kind"] == "computer" and r["workspace_id"] == 5 and r["host_state"] == {} and r["thread_id"] == "t1"
        assert await s.cas(i, "provisioning", status="running") is True and await s.cas(i, "provisioning", status="failed") is False
        await s.touch(i, host_state={"disk_warn": True}); assert (await s.get(i))["host_state"] == {"disk_warn": True}
        with pytest.raises(ValueError): await s.cas(i, "running", workspace_id=6)       # a box can never be moved to another workspace through the store
        with pytest.raises(ValueError): await s.touch(i, kind="files")                  # only whitelisted columns can be written
        assert [x["id"] for x in await s.list(5)] == [i] and await s.list(6) == [] and [x["id"] for x in await s.list_running()] == [i]
        assert await s.count(5, ("running",)) == 1 and await s.count(5, ("stopped",)) == 0 and await s.count(6, ("running",)) == 0
        assert await s.quota(5) is None; await c.execute("INSERT INTO workspace_sandbox_quotas(workspace_id,max_total) VALUES(5,4)"); assert (await s.quota(5))["max_total"] == 4 and await s.quota(6) is None
        await s.add_command({"id": "c" * 16, "sandbox_id": i, "workspace_id": 5, "user_id": 9, "command": "echo " + "x" * 5000, "status": "done", "exit_code": 0, "truncated": False, "duration_ms": 3})
        assert len((await (await c.execute("SELECT command FROM sandbox_commands")).fetchone())[0]) == 4000
        await s.cas(i, "running", status="deleted"); assert await s.get(i) is None and await s.list(5) == []      # tombstone is invisible
        await c.execute("DELETE FROM sandboxes WHERE id=%s", (i,)); assert (await (await c.execute("SELECT count(*) FROM sandbox_commands")).fetchone())[0] == 0   # commands go with the box
    pg(go)

def test_migration_is_listed_in_the_real_folder():
    assert "0050" in [v for v, _, _ in M._load()]

# ------------------------------------------------------------------ row-level security on the real local DB (policies exactly as admin.rls_init builds them)
def _rls_env():
    import deepagent_api as A, admin
    from psycopg_pool import AsyncConnectionPool
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as c: await M.run_pending(c)
        async with AsyncConnectionPool(PG, min_size=1, max_size=2, open=False, kwargs={"autocommit": True, "row_factory": dict_row}) as pool:
            await pool.open(); old = (A.S.get("pool"), A.S.get("rls")); A.S["pool"] = pool; A.S["rls"] = None
            try:
                await admin._policy("sandbox_commands", "aos_ws() IS NULL OR (" + admin.RLS_CHILD["sandbox_commands"] + ")")
                await admin._policy("workspace_sandbox_quotas", "aos_ws() IS NULL OR workspace_id = aos_ws()")
                await admin._policy("sandboxes", "aos_ws() IS NULL OR (workspace_id = aos_ws() AND (aos_priv() OR owner_id IS NULL OR owner_id = aos_uid()))")
            finally: A.S["pool"], A.S["rls"] = old
    asyncio.run(go())

def _as(c, ws, uid=0, role="editor"):
    c.execute("SELECT set_config('app.ws',%s,false), set_config('app.uid',%s,false), set_config('app.role',%s,false), set_config('role',%s,false)",
              (str(ws), str(uid), role, "aos_rls" if ws != "*" else "none"))

def test_rls_isolates_boxes_commands_and_quotas_between_workspaces():
    _rls_env(); tag = uuid.uuid4().hex[:6]; WA, WB = 9_140_001, 9_140_002; ida, idb = "a1" + tag + "0" * 8, "b1" + tag + "0" * 8
    with psycopg.connect(PG, autocommit=True, row_factory=dict_row) as c:
        try:
            _as(c, "*")
            for i, ws, owner in ((ida, WA, 21), (idb, WB, 31)):
                c.execute("INSERT INTO sandboxes(id,name,workspace_id,owner_id,kind,status) VALUES(%s,'t1140',%s,%s,'computer','running')", (i, ws, owner))
                c.execute("INSERT INTO sandbox_commands(id,sandbox_id,workspace_id,command) VALUES(%s,%s,%s,'echo')", ("c" + i[:15], i, ws))
                c.execute("INSERT INTO workspace_sandbox_quotas(workspace_id,max_total) VALUES(%s,2) ON CONFLICT DO NOTHING", (ws,))
            def seen(ws, uid, role):
                _as(c, ws, uid, role)
                return ({r["id"] for r in c.execute("SELECT id FROM sandboxes WHERE name='t1140'").fetchall()}, {r["sandbox_id"] for r in c.execute("SELECT sandbox_id FROM sandbox_commands WHERE command='echo' AND sandbox_id IN (%s,%s)", (ida, idb)).fetchall()},
                        {r["workspace_id"] for r in c.execute("SELECT workspace_id FROM workspace_sandbox_quotas WHERE workspace_id IN (%s,%s)", (WA, WB)).fetchall()})
            assert seen(WA, 21, "editor") == ({ida}, {ida}, {WA})                    # owner: only own workspace
            assert seen(WB, 31, "editor") == ({idb}, {idb}, {WB})
            assert seen(WA, 22, "editor") == (set(), set(), {WA})                    # same workspace, other user: no box, no command log
            assert seen(WA, 22, "admin") == ({ida}, {ida}, {WA})                     # workspace admin sees the workspace's boxes, never the other workspace's
            assert seen(9_999_999, 21, "admin") == (set(), set(), set())            # a third workspace sees nothing, whatever the role / user id
            _as(c, WB, 31, "admin")
            assert c.execute("UPDATE sandboxes SET status='stopped' WHERE id=%s", (ida,)).rowcount == 0     # cannot change it
            assert c.execute("DELETE FROM sandboxes WHERE id=%s", (ida,)).rowcount == 0                     # cannot delete it
            with pytest.raises(psycopg.errors.InsufficientPrivilege):                                        # cannot plant a row in another workspace
                c.execute("INSERT INTO sandboxes(id,name,workspace_id,owner_id,kind) VALUES(%s,'t1140',%s,31,'computer')", ("d1" + tag + "0" * 8, WA))
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute("INSERT INTO sandbox_commands(id,sandbox_id,workspace_id,command) VALUES(%s,%s,%s,'echo')", ("e" * 16, ida, WB))     # a log line on A's box
            _as(c, "*"); assert c.execute("SELECT status FROM sandboxes WHERE id=%s", (ida,)).fetchone()["status"] == "running"
        finally:
            _as(c, "*")
            c.execute("DELETE FROM sandboxes WHERE name='t1140' AND workspace_id IN (%s,%s)", (WA, WB)); c.execute("DELETE FROM workspace_sandbox_quotas WHERE workspace_id IN (%s,%s)", (WA, WB))

# ------------------------------------------------------------------ obs worker hook
def test_disk_tick_global_is_a_noop_without_gvisor_and_the_worker_runs_it(monkeypatch):
    import types
    monkeypatch.setitem(sys.modules, "deepagent_api", types.SimpleNamespace(SBX_INFO={"runtime": "fake (local test runner, no Docker)"}))
    assert run(R.disk_tick_global()) == []                       # dev machine without gVisor: no docker call, no error
    src = open(os.path.join(APP, "obs.py"), encoding="utf-8").read()
    assert '("computer_disk", _computer_disk, 15)' in src and "sbx_runtime.disk_tick_global()" in src      # 15 x 20 s = every 5 minutes
