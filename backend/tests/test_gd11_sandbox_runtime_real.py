"""GĐ11.40 REAL box tests: the aos-computer image under gVisor (runsc) through DockerGvisorDriver. Skipped unless AOS_REAL_SANDBOX=1 (needs Docker + runsc
+ the image `docker build -t aos-computer:20261026 -f sandbox/Dockerfile.computer sandbox/`). No Internet needed: packages are built offline inside the box.
Containers / volumes are named aos-box-<id>[-workspace|-opt] and every test deletes its own in `finally`; the module teardown checks nothing is left."""
import asyncio, os, shutil, subprocess, sys, uuid
import pytest

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)
import sbx_runtime as R  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("AOS_REAL_SANDBOX") != "1" or shutil.which("docker") is None, reason="needs AOS_REAL_SANDBOX=1 and a Docker CLI")
run = asyncio.run

def sh(*cmd, timeout=120):
    p = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout); return p.returncode, (p.stdout + p.stderr).strip()

def ctx(ws=1, uid=10, role="editor"): return {"ws": ws, "uid": uid, "role": role, "demo": False, "scope": "full"}

@pytest.fixture(scope="module", autouse=True)
def prereqs():
    rc, out = sh("docker", "info", "--format", "{{json .Runtimes}}")
    if rc or '"runsc"' not in out: pytest.skip("runsc is not registered in Docker")
    if sh("docker", "image", "inspect", R.IMAGE)[0]: pytest.skip(f"image {R.IMAGE} is not built")
    names = lambda *c: set(sh(*c)[1].split())                                                          # noqa: E731
    box_cmd, vol_cmd = ("docker", "ps", "-a", "--filter", "label=aos.box=1", "--format", "{{.Names}}"), ("docker", "volume", "ls", "-q", "--filter", "label=aos.box=1")
    boxes0, vols0 = names(*box_cmd), names(*vol_cmd)             # boxes that already exist (a developer's own agent box) are not ours: only NEW ones count as leftovers
    yield
    left, vols = names(*box_cmd) - boxes0, names(*vol_cmd) - vols0
    assert not left and not vols, f"leftover test boxes / volumes: {left} {vols}"

def comp(**kw):
    return R.Computers(R.MemStore(), R.DockerGvisorDriver(), disk_probe=lambda: 10, **kw)

def with_box(fn, size="small", who=None, c=None):
    """Create a real box, run fn(c, who, id), always delete it."""
    async def go():
        cc, w = c or comp(), who or ctx()
        r = await cc.create(w, name="t1140", size=size); i = r["id"]
        try: return await fn(cc, w, i)
        finally: await cc.delete(w, i)
    return run(go())

def inspect_json(i, fmt): return sh("docker", "inspect", "-f", fmt, R.cname(i))[1]

def test_real_lifecycle_isolation_flags_and_runtime():
    async def go(c, w, i):
        info = await c.store.get(i); assert info["status"] == "running" and info["core"] == "gvisor"
        assert inspect_json(i, "{{.HostConfig.Runtime}}") == "runsc"                                    # not runc
        assert inspect_json(i, "{{.HostConfig.NetworkMode}}") == "none" and inspect_json(i, "{{.HostConfig.Memory}}") == str(1 << 30)
        assert inspect_json(i, "{{.HostConfig.PidsLimit}}") == "256" and inspect_json(i, "{{.HostConfig.NanoCpus}}") == "500000000"
        assert inspect_json(i, "{{.HostConfig.Privileged}}") == "false" and inspect_json(i, "{{.HostConfig.SecurityOpt}}") == "[no-new-privileges]"
        assert '"dev.gvisor.flag.overlay2":"none"' in inspect_json(i, "{{json .HostConfig.Annotations}}")
        o = lambda cmd, **k: c.exec(w, i, cmd, **k)                                                      # noqa: E731
        assert "gvisor" in (await o("uname -r"))["output"]                                               # gVisor's own kernel, not the host's
        assert (await o("hostname"))["output"].strip() == R.cname(i)
        assert (await o("cat /proc/1/comm"))["output"].strip() == "tini"                                 # tini is PID 1
        assert (await o("id -u"))["output"].strip() == "0"                                               # root INSIDE the sandbox
        assert (await o("pwd"))["output"].strip() == "/workspace" and (await o("echo $AOS_X", env={"AOS_X": "v1"}))["output"].strip() == "v1"
        assert (await o("exit 7"))["exit_code"] == 7
        # no network: only loopback, and an outgoing connection fails
        assert (await o("ls /sys/class/net 2>/dev/null; cat /proc/net/dev | tail -n +3 | cut -d: -f1 | tr -d ' '"))["output"].split() == ["lo"]
        t = await o("python3 -c \"import socket;socket.create_connection(('1.1.1.1',53),3)\"")
        assert t["exit_code"] != 0
        # capability set is the reduced one (CapEff of PID 1 has CHOWN,DAC_OVERRIDE,FOWNER,SETUID,SETGID only = 0x00000000000000cb)
        capeff = int((await o("grep CapEff /proc/self/status | cut -f2"))["output"].strip(), 16)
        assert capeff & ((1 << 21) | (1 << 12) | (1 << 17)) == 0                                         # no SYS_ADMIN / NET_ADMIN / SYS_RAWIO
        # lifecycle
        await c.stop(w, i); assert inspect_json(i, "{{.State.Running}}") == "false"
        await c.start(w, i); assert (await o("echo again"))["output"] == "again\n"
        # image content
        out = (await o("python3 -c \"import docx,openpyxl,pptx,markitdown;print('libs ok')\" && node --version && soffice --version | head -c 20 && pandoc --version | head -1 && pdftotext -v 2>&1 | head -1 && rg --version | head -1 && jq --version && git --version", timeout=90))
        assert out["exit_code"] == 0 and "libs ok" in out["output"] and "v24." in out["output"], out["output"]
    with_box(go)

def test_real_output_cap_timeout_stdin_and_files():
    async def go(c, w, i):
        big = await c.exec(w, i, "head -c 250000 /dev/zero | tr '\\0' x")
        assert big["truncated"] is True and len(big["output"]) == R.OUT_CAP
        log = await c.get_file(w, i, f"/workspace/.aos/out/{big['command_id']}.log"); assert len(log) == 250000        # the full output is kept in the box
        small = await c.exec(w, i, "echo ok; echo err >&2"); assert small["output"] == "ok\nerr\n" and small["truncated"] is False
        t = await c.exec(w, i, "sleep 30", timeout=2); assert t["timed_out"] is True and t["exit_code"] == 137 and t["duration_ms"] < 15000
        assert (await c.exec(w, i, "echo still alive"))["output"] == "still alive\n"                                      # the box survives a killed command
        assert (await c.exec(w, i, "cat", stdin=b"from stdin"))["output"] == "from stdin"
        data = bytes(range(256)) * 10
        await c.put_file(w, i, "sub/dir/bin.dat", data); assert await c.get_file(w, i, "/workspace/sub/dir/bin.dat") == data
        assert (await c.exec(w, i, "wc -c < sub/dir/bin.dat"))["output"].strip() == "2560"
        with pytest.raises(R.NotFound): await c.get_file(w, i, "/workspace/nope")
        for bad in ("../x", "/proc/1/environ", "a\nb"):
            with pytest.raises(R.SbxError): await c.get_file(w, i, bad)
        with pytest.raises(R.SbxError): await c.exec(w, i, "echo x", env={"BAD NAME": "1"})
        with pytest.raises(R.SbxError): await c.exec(w, i, "echo x", cwd="/workspace/../etc")
    with_box(go)

def test_real_packages_survive_stop_start_with_no_network():
    """11.40a decision: durable root FS (overlay2=none) + two volumes. Packages are built offline inside the box (no Internet in the box)."""
    deb = ("set -e; d=$(mktemp -d); mkdir -p $d/aoscow/DEBIAN $d/aoscow/usr/bin; printf 'Package: aoscow\\nVersion: 1.0\\nArchitecture: all\\nMaintainer: t <t@t>\\nDescription: t\\n' > $d/aoscow/DEBIAN/control; "
           "printf '#!/bin/sh\\necho moo\\n' > $d/aoscow/usr/bin/aoscow; chmod 755 $d/aoscow/usr/bin/aoscow; dpkg-deb --build $d/aoscow $d/aoscow.deb >/dev/null; dpkg -i $d/aoscow.deb >/dev/null")
    whl = ("python3 - <<'PY'\nimport zipfile\nz=zipfile.ZipFile('/workspace/aoswhl-1.0-py3-none-any.whl','w')\nz.writestr('aoswhl.py','VALUE=42\\n')\n"
           "z.writestr('aoswhl-1.0.dist-info/METADATA','Metadata-Version: 2.1\\nName: aoswhl\\nVersion: 1.0\\n')\nz.writestr('aoswhl-1.0.dist-info/WHEEL','Wheel-Version: 1.0\\nGenerator: t\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n')\n"
           "z.writestr('aoswhl-1.0.dist-info/RECORD','')\nz.close()\nPY")
    async def go(c, w, i):
        o = lambda cmd, **k: c.exec(w, i, cmd, timeout=120, **k)                                          # noqa: E731
        assert (await o(deb))["exit_code"] == 0 and (await o("aoscow"))["output"] == "moo\n"              # dpkg: the same root-FS write apt-get does
        assert (await o("which python pip; pip --version | head -c 3"))["exit_code"] == 0
        assert "/opt/aos-user/venv/bin/python" in (await o("which python"))["output"]                     # the user's venv comes first in PATH
        await o(whl); r = await o("pip install --no-index --quiet /workspace/aoswhl-1.0-py3-none-any.whl"); assert r["exit_code"] == 0, r["output"]
        assert (await o("python -c 'import aoswhl;print(aoswhl.VALUE)'"))["output"].strip() == "42"
        assert (await o("python -c 'import docx;print(1)'"))["output"].strip() == "1"                      # image-venv libs are still visible from the user venv
        await o("echo hello > /usr/local/bin/aos-rootfs-file; npm config get prefix > /workspace/npmprefix")
        assert (await c.get_file(w, i, "/workspace/npmprefix")).decode().strip() == "/opt/aos-user/npm"
        await c.stop(w, i); await c.start(w, i)
        assert (await o("aoscow"))["output"] == "moo\n"                                                    # dpkg-installed package: still there
        assert (await o("cat /usr/local/bin/aos-rootfs-file"))["output"] == "hello\n"                      # plain root-FS file: still there
        assert (await o("python -c 'import aoswhl;print(aoswhl.VALUE)'"))["output"].strip() == "42"        # pip into the user venv: still there
        t = await o("python3 -c \"import socket;socket.create_connection(('1.1.1.1',53),3)\""); assert t["exit_code"] != 0     # and still no network
        await c.exec(w, i, "ls /opt/aos-user/venv/bin/pip")                                                # volume content is on the host side too
        vol = R.vols(i)[1]; assert sh("docker", "volume", "inspect", vol)[0] == 0
    with_box(go)

def test_real_setup_sh_reruns_on_start_and_its_failure_does_not_stop_the_box():
    async def go(c, w, i):
        await c.put_file(w, i, "/opt/aos-user/setup.sh", b"echo \"boot=$AOS_BOOT\" > /workspace/setup_ran\nexit 3\n")
        await c.stop(w, i); await c.start(w, i, wait_boot=True)
        assert (await c.get_file(w, i, "/workspace/setup_ran")) == b"boot=1\n"                              # AOS_BOOT=1, ran on start
        await c.refresh(i); row = await c.store.get(i)
        assert row["status"] == "running" and row["last_error"].startswith("setup.sh failed (exit 3")       # failure recorded ...
        assert (await c.exec(w, i, "echo usable"))["output"] == "usable\n"                                   # ... box still usable
        await c.put_file(w, i, "/opt/aos-user/setup.sh", b"echo again > /workspace/setup_ran\n")
        await c.stop(w, i); await c.start(w, i, wait_boot=True); await c.refresh(i)
        assert (await c.get_file(w, i, "/workspace/setup_ran")) == b"again\n" and (await c.store.get(i))["last_error"] is None
        await c.put_file(w, i, "/opt/aos-user/setup.sh", b"sleep 300\n")                                      # the 120 s cap: checked by the script text, not by waiting 2 min
        assert "timeout -s KILL 120" in open(os.path.join(os.path.dirname(__file__), "..", "sandbox", "aos-boot")).read()
    with_box(go)

def test_real_two_boxes_do_not_share_anything():
    async def go():
        c = comp(); a = (await c.create(ctx(uid=10), name="t1140"))["id"]; b = None
        try:
            b = (await c.create(ctx(uid=11), name="t1140"))["id"]
            await c.put_file(ctx(uid=10), a, "/workspace/secret.txt", b"A only")
            r = await c.exec(ctx(uid=11), b, "ls /workspace /opt/aos-user | grep -c secret || true"); assert r["output"].strip() == "0"
            with pytest.raises(R.SbxError): await c.get_file(ctx(uid=11), b, "/workspace/secret.txt")
            with pytest.raises(R.NotFound): await c.get_file(ctx(uid=11), a, "/workspace/secret.txt")           # user 11 may not read A's box at all
            with pytest.raises(R.NotFound): await c.exec(ctx(ws=2, uid=10, role="admin"), a, "cat /workspace/secret.txt")   # nor another workspace's admin
            assert len(R.vols(a)) == 2 and set(R.vols(a)).isdisjoint(R.vols(b))
        finally:
            await c.delete(ctx(uid=10), a)
            if b: await c.delete(ctx(uid=11), b)
    run(go())

def test_real_medium_size_limits():
    async def go(c, w, i):
        assert inspect_json(i, "{{.HostConfig.Memory}}") == str(2 << 30) and inspect_json(i, "{{.HostConfig.NanoCpus}}") == "1000000000"
        assert inspect_json(i, "{{.HostConfig.MemorySwap}}") == str(2 << 30)
    with_box(go, size="medium")

def test_real_fork_bomb_is_contained_by_the_pids_limit():
    """--pids-limit 256 counts gVisor's own host threads too: a fork bomb kills THIS box (finding, see N2 §8), never the host or other boxes."""
    async def go():
        c = comp(); a = (await c.create(ctx(uid=10), name="t1140"))["id"]; b = (await c.create(ctx(uid=11), name="t1140"))["id"]
        try:
            try: await c.exec(ctx(uid=10), a, "i=0; while [ $i -lt 400 ]; do sleep 20 & i=$((i+1)); done 2>/dev/null; echo done", timeout=60)
            except R.BoxFailed: pass
            row = await c.store.get(a); assert row["status"] in ("running", "stopped")                                  # the DB follows the container (refresh after a failed exec)
            if inspect_json(a, "{{.State.Running}}") == "false": assert row["status"] == "stopped"
            assert inspect_json(a, "{{.HostConfig.PidsLimit}}") == "256"
            assert (await c.exec(ctx(uid=11), b, "echo neighbour fine"))["output"] == "neighbour fine\n"                 # the other box is untouched
        finally:
            await c.delete(ctx(uid=10), a); await c.delete(ctx(uid=11), b)
    run(go())

def test_real_disk_usage_and_watchdog_stops_a_box_over_its_limit(monkeypatch):
    async def go(c, w, i):
        use = await R.DockerGvisorDriver().disk_usage()
        assert i in use and use[i] > 0
        await c.exec(w, i, "head -c 30000000 /dev/zero > /workspace/fill.dat; sync", timeout=60)                # 30 MB written to the volume
        monkeypatch.setenv("AOS_BOX_DISK_GB", "0.1")                                                          # ~107 MB: 30 MB is below the warn line
        assert [e for e in await c.disk_tick() if e.get("id") == i] == []
        monkeypatch.setenv("AOS_BOX_DISK_GB", "0.03")                                                         # ~32 MB: 30 MB + the box's own layer -> above 80 %, below 100 %? image-dependent
        ev = [e for e in await c.disk_tick() if e.get("id") == i]; assert ev and ev[0]["event"] in ("warn", "stopped")
        monkeypatch.setenv("AOS_BOX_DISK_GB", "0.01")                                                         # ~10 MB: certainly over
        ev = [e for e in await c.disk_tick() if e.get("id") == i]
        row = await c.store.get(i)
        assert row["status"] == "stopped" and row["last_error"] == "disk quota", (ev, row)
        assert inspect_json(i, "{{.State.Running}}") == "false"
        await c.start(w, i); assert (await c.exec(w, i, "echo back"))["output"] == "back\n"                   # data intact after the stop
        assert (await c.exec(w, i, "wc -c < /workspace/fill.dat"))["output"].strip() == "30000000"
    with_box(go)
