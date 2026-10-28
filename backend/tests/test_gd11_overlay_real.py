"""11.40a gate test (F12): what survives `docker stop` / `docker start` under gVisor (runsc)?  Real Docker, so skipped unless AOS_REAL_SANDBOX=1.
Throwaway containers/images are named `aos-probe-*` and always removed in `finally`. Never touches runsc, daemon.json or dockerd; never uses runc.
Answers measured 2026-10-26 are in RECORDED and in docs/thiet_ke_2026-10/nghien_cuu/N2_MAY_TINH_AO.md ("Lớp phủ gVisor").
Q3 is a hard assert (design depends on it); Q1/Q2/Q4/Q5 compare with RECORDED and xfail (documented) if the platform now behaves differently."""
import os, shutil, subprocess, tempfile, time, uuid
import pytest

IMAGE = os.environ.get("AOS_PROBE_IMAGE", "ubuntu:24.04")
RECORDED = {   # measured with runsc release-20260928.0 on the WSL2 dev box, 2026-10-26 (see N2 "Lớp phủ gVisor")
    "q1_usr_local_bin": False,   # file written in the container rootfs (/usr/local/bin) after stop/start
    "q1_usr_lib_python3": False,  # file written in /usr/lib/python3*/
    "q1_apt": False,              # apt-get install cowsay
    "q1_pip_usr": False,          # pip install into /usr
    "q2_commit_contains": False,  # docker commit contains the rootfs change
    "q4_annotation_overlay_none_effective": True,    # `--annotation dev.gvisor.flag.overlay2=none` on `docker run` (no daemon.json/runtime change): rootfs writes persist
    "q4_apt_persists_with_annotation": True,         # ...and an apt-get install survives stop/start in such a box
    "q4_commit_with_annotation": True,               # ...and docker commit contains the change
    "q4_other_flags_refused": True,                  # any other dev.gvisor.flag.* (e.g. platform) is refused: "flag override disabled" (allow-flag-override stays off)
    "q5_ro_bind_sees_host_change": True,   # host edit inside a :ro bind mount visible in the running box within 5 s
}

pytestmark = pytest.mark.skipif(os.environ.get("AOS_REAL_SANDBOX") != "1" or shutil.which("docker") is None,
                                reason="needs AOS_REAL_SANDBOX=1 and a Docker CLI")


def sh(*cmd, timeout=300):
    p = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).strip()


def dx(name, script, timeout=300):
    return sh("docker", "exec", "-e", "DEBIAN_FRONTEND=noninteractive", name, "bash", "-c", script, timeout=timeout)


def ok(name, script, timeout=60):
    return dx(name, script, timeout)[0] == 0


def run_box(name, host, extra=(), runtime="runsc"):
    cmd = ["docker", "run", "-d", "--name", name, "--runtime", runtime, *extra,
           "-v", f"{host}/ws:/workspace", "-v", f"{host}/opt:/opt/aos-user", "-v", f"{host}/ro:/ro:ro", IMAGE, "sleep", "infinity"]
    rc, out = sh(*cmd, timeout=120)
    assert rc == 0, out


def cycle(name):
    assert sh("docker", "stop", "-t", "1", name)[0] == 0
    assert sh("docker", "start", name)[0] == 0


def have_runsc():
    rc, out = sh("docker", "info", "--format", "{{json .Runtimes}}")
    return rc == 0 and '"runsc"' in out


def measure():
    """Run every probe once; return (answers dict, notes dict). All probe objects are removed in `finally`."""
    tag = uuid.uuid4().hex[:8]
    names = [f"aos-probe-{tag}-main", f"aos-probe-{tag}-ann"]
    img = f"aos-probe-img-{tag}:t"
    host = tempfile.mkdtemp(prefix="aos-probe-")
    for d in ("ws", "opt", "ro"): os.makedirs(f"{host}/{d}")
    A, N = {}, {}
    try:
        if sh("docker", "image", "inspect", IMAGE)[0] != 0:
            assert sh("docker", "pull", IMAGE, timeout=600)[0] == 0, "cannot get probe image"
        run_box(names[0], host)
        n = names[0]
        N["runtime"] = sh("docker", "inspect", "--format", "{{.HostConfig.Runtime}}", n)[1]
        N["kernel"] = dx(n, "uname -r; (dmesg 2>/dev/null | head -2)")[1].splitlines()[0]
        N["network"] = dx(n, "timeout 90 apt-get update >/dev/null 2>&1 && echo yes || echo no")[1] == "yes"
        # ---- Q1 rootfs writes, then stop/start
        dx(n, "echo hi > /usr/local/bin/aos-probe-file && PYD=$(ls -d /usr/lib/python3*/ | head -1) && echo hi > ${PYD}aos_probe_file.py")
        if N["network"]:
            dx(n, "timeout 280 apt-get install -y --no-install-recommends cowsay python3-pip >/dev/null 2>&1", 300)
            dx(n, "timeout 120 pip install --break-system-packages --quiet six 2>&1 | tail -1", 150)
            N["q1_installed_before_stop"] = ok(n, "test -x /usr/games/cowsay && python3 -c 'import six'")
        # ---- Q3 durable mounts (written before the stop; pip --target + apt .deb cache + plain files)
        dx(n, "echo hi > /workspace/aos_probe_ws.txt; mkdir -p /opt/aos-user/cache/apt /opt/aos-user/lib; echo hi > /opt/aos-user/aos_probe_opt.txt")
        if N["network"]:
            dx(n, "cd /opt/aos-user/cache/apt && timeout 120 apt-get download cowsay >/dev/null 2>&1; "
                  "timeout 120 pip install --break-system-packages --quiet --target /opt/aos-user/lib six 2>&1 | tail -1", 300)
        # ---- Q2 commit of the running box (taken before the stop)
        rc, out = sh("docker", "commit", n, img)
        N["commit_rc"] = rc
        if rc == 0:
            rc2, o2 = sh("docker", "run", "--rm", "--runtime", "runsc", img, "bash", "-c",
                         "test -f /usr/local/bin/aos-probe-file && echo bin; test -x /usr/games/cowsay && echo cow")
            N["commit_runsc"] = o2
            A["q2_commit_contains"] = "bin" in o2.split()
        else:
            N["commit_error"] = out[:200]; A["q2_commit_contains"] = False
        # ---- stop / start
        cycle(n)
        A["q1_usr_local_bin"] = ok(n, "test -f /usr/local/bin/aos-probe-file")
        A["q1_usr_lib_python3"] = ok(n, "ls /usr/lib/python3*/aos_probe_file.py")
        if N["network"]:
            A["q1_apt"] = ok(n, "test -x /usr/games/cowsay && dpkg -s cowsay >/dev/null 2>&1")
            A["q1_pip_usr"] = ok(n, "python3 -c 'import six'")
        # ---- Q3 after stop/start
        A["q3_workspace_file"] = ok(n, "test \"$(cat /workspace/aos_probe_ws.txt)\" = hi")
        A["q3_opt_file"] = ok(n, "test \"$(cat /opt/aos-user/aos_probe_opt.txt)\" = hi")
        A["q3_host_sees_files"] = os.path.exists(f"{host}/ws/aos_probe_ws.txt") and os.path.exists(f"{host}/opt/aos_probe_opt.txt")
        if N["network"]:
            A["q3_pip_target"] = ok(n, "test -f /opt/aos-user/lib/six.py") and os.path.exists(f"{host}/opt/lib/six.py")   # (python3 itself is gone: it came from apt in the rootfs)
            A["q3_deb_cache"] = ok(n, "ls /opt/aos-user/cache/apt/cowsay*.deb")
        # ---- Q4 per-container overlay setting through annotations (no daemon.json / runtime change)
        rc, out = sh("docker", "run", "-d", "--name", names[1], "--runtime", "runsc", "--annotation", "dev.gvisor.flag.overlay2=none",
                     IMAGE, "sleep", "infinity")
        N["q4_start_rc"], N["q4_out"] = rc, out[-200:]
        if rc == 0:
            dx(names[1], "echo hi > /usr/local/bin/aos-probe-file")
            if N["network"]:
                dx(names[1], "timeout 90 apt-get update >/dev/null 2>&1; timeout 120 apt-get install -y --no-install-recommends tree >/dev/null 2>&1", 240)
            N["q4_diff"] = sh("docker", "diff", names[1])[1][:200]   # overlay none = writes land in Docker's own writable layer
            rcc, _ = sh("docker", "commit", names[1], img + "2")
            if rcc == 0:
                A["q4_commit_with_annotation"] = "bin" in sh("docker", "run", "--rm", "--runtime", "runsc", img + "2", "bash", "-c",
                                                              "test -f /usr/local/bin/aos-probe-file && echo bin")[1].split()
                sh("docker", "rmi", "-f", img + "2")
            else:
                A["q4_commit_with_annotation"] = False
            cycle(names[1])
            A["q4_annotation_overlay_none_effective"] = ok(names[1], "test -f /usr/local/bin/aos-probe-file")
            if N["network"]: A["q4_apt_persists_with_annotation"] = ok(names[1], "test -x /usr/bin/tree")
        else:
            A["q4_annotation_overlay_none_effective"] = False   # runtime refused the override
        rc, out = sh("docker", "run", "--rm", "--runtime", "runsc", "--annotation", "dev.gvisor.flag.platform=ptrace", IMAGE, "true")
        A["q4_other_flags_refused"] = rc != 0 and "flag override disabled" in out
        N["q4_storage_opt_size"] = sh("docker", "run", "--rm", "--runtime", "runsc", "--storage-opt", "size=1G", IMAGE, "true")[1][-160:] or "accepted (measured 2026-10-26: NOT enforced - a 200 MB write passed a 50M limit)"
        # ---- Q5 host edit in a :ro bind mount
        with open(f"{host}/ro/f.txt", "w") as f: f.write("v1")
        assert dx(n, "cat /ro/f.txt")[1] == "v1"
        with open(f"{host}/ro/f.txt", "w") as f: f.write("v2")
        t0, seen = time.time(), False
        while time.time() - t0 < 5:
            if dx(n, "cat /ro/f.txt")[1] == "v2": seen = True; break
            time.sleep(0.5)
        A["q5_ro_bind_sees_host_change"] = seen
        N["q5_seconds"] = round(time.time() - t0, 1)
        with open(f"{host}/ro/new.txt", "w") as f: f.write("n")
        os.replace(f"{host}/ro/new.txt", f"{host}/ro/f.txt")   # atomic replace (new inode), the way editors/tools usually write
        time.sleep(1)
        N["q5_atomic_replace_seen"] = dx(n, "cat /ro/f.txt")[1] == "n"
        N["q5_ro_write_refused"] = not ok(n, "echo x > /ro/f.txt")
        return A, N
    finally:
        for c in names: sh("docker", "rm", "-f", c)
        sh("docker", "rmi", "-f", img); sh("docker", "rmi", "-f", img + "2")
        sh("docker", "run", "--rm", "--runtime", "runsc", "-v", f"{host}:/h", IMAGE, "chmod", "-R", "a+rwX", "/h")   # root-owned files from the box
        shutil.rmtree(host, ignore_errors=True)


@pytest.fixture(scope="module")
def probe():
    if not have_runsc(): pytest.skip("Docker has no runsc runtime; the computer must never run on runc")
    return measure()


def test_probe_ran_on_gvisor_not_runc(probe):
    A, N = probe
    assert N["runtime"] == "runsc"            # explicit runtime, no fallback
    assert "4.4.0" in N["kernel"] or "gvisor" in N["kernel"].lower()   # gVisor's emulated kernel, not the host's


def test_q3_host_mounted_volumes_survive_stop_start(probe):
    A, _ = probe
    assert A["q3_workspace_file"] and A["q3_opt_file"], "files in /workspace or /opt/aos-user were lost after stop/start"
    assert A["q3_host_sees_files"], "the files must exist on the host side of the mounts (that is what makes them durable)"
    if "q3_pip_target" in A:   # only when the probe box had Internet
        assert A["q3_pip_target"], "pip --target into /opt/aos-user must survive"
        assert A["q3_deb_cache"], "the .deb cache in /opt/aos-user must survive"


@pytest.mark.parametrize("key", ["q1_usr_local_bin", "q1_usr_lib_python3", "q1_apt", "q1_pip_usr"])
def test_q1_rootfs_changes_after_stop_start_recorded(probe, key):
    A, _ = probe
    if key not in A: pytest.skip("probe box had no Internet")
    if A[key] != RECORDED[key]:
        pytest.xfail(f"{key}: now {A[key]}, recorded {RECORDED[key]} - re-read N2 'Lớp phủ gVisor' and the 11.40 F12 design")


def test_q2_docker_commit_recorded(probe):
    A, N = probe
    if A["q2_commit_contains"] != RECORDED["q2_commit_contains"]:
        pytest.xfail(f"docker commit now contains the change ({N}) - recorded: it does not")


def test_q4_overlay_annotation_recorded(probe):
    A, N = probe
    for k in ("q4_annotation_overlay_none_effective", "q4_apt_persists_with_annotation", "q4_commit_with_annotation", "q4_other_flags_refused"):
        if k in A and A[k] != RECORDED[k]:
            pytest.xfail(f"{k}: now {A[k]}, recorded {RECORDED[k]} ({N.get('q4_diff')}) - re-read N2 'Lớp phủ gVisor' before relying on 11.40 F12")


def test_q4_negative_other_runsc_flags_still_refused(probe):
    """The daemon keeps allow-flag-override off: overriding anything but the overlay (e.g. the platform) must fail, and we must not have needed it."""
    A, _ = probe
    assert A["q4_other_flags_refused"], "a non-overlay dev.gvisor.flag.* annotation was accepted: allow-flag-override seems to be on - STOP and tell anh"


def test_q5_ro_bind_mount_sees_host_change_within_5s_recorded(probe):
    A, N = probe
    if A["q5_ro_bind_sees_host_change"] != RECORDED["q5_ro_bind_sees_host_change"]:
        pytest.xfail(f"ro bind mount no longer shows host edits within 5 s ({N.get('q5_seconds')} s) - 11.64 must use upload_files")
    assert N["q5_ro_write_refused"], "a :ro mount must refuse writes from inside the box"


def test_no_probe_objects_left_behind(probe):
    names = sh("docker", "ps", "-a", "--filter", "name=aos-probe-", "--format", "{{.Names}}")[1]
    imgs = sh("docker", "images", "--filter", "reference=aos-probe-img-*", "--format", "{{.Repository}}")[1]
    assert names == "" and imgs == ""
