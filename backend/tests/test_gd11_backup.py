"""GĐ11.73 + 11.ZA2: backup of boxes / skills / part-A state. Unit tests use a fake `docker` (a shell shim on a temp dir): no Docker, no VPS.
The full restore drill (real Docker + local Postgres, ~1 min) runs only with AOS_REAL_SANDBOX=1."""
import os, shutil, stat, subprocess
import pytest

OPS = os.path.join(os.path.dirname(__file__), "..", "ops")
SCRIPT = os.path.join(OPS, "backup_computers.sh")
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash needed")

SHIM = r"""#!/bin/bash
# fake docker: `volume ls` lists FAKE_VOLS; `volume inspect X` succeeds when X is in FAKE_EXIST; everything else is logged and fails
echo "$@" >> "$FAKE_LOG"
case "$1 $2" in
  "volume ls") printf '%s\n' $FAKE_VOLS;;
  "volume inspect") for v in $FAKE_EXIST; do [ "$v" = "$3" ] && exit 0; done; exit 1;;
  "image ls") exit 0;;
  "network ls") exit 0;;
  *) exit 1;;
esac
"""


@pytest.fixture()
def fake(tmp_path):
    d = tmp_path / "bin"; d.mkdir(); p = d / "docker"; p.write_text(SHIM); p.chmod(p.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "docker.log"; log.write_text("")
    env = {**os.environ, "DOCKER": str(p), "FAKE_LOG": str(log), "AOS_VAR_DIR": str(tmp_path / "var"),
           "FAKE_VOLS": "odoo20_db-data homeassistant_config hermes-data aos-box-0123456789abcdef-workspace aos-box-0123456789abcdef-opt aos-box-NOTHEX-workspace aos-box-0123456789abcdef-evil",
           "FAKE_EXIST": ""}
    return env, log


def run(env, *args, script=SCRIPT):
    return subprocess.run(["bash", script, *args], env=env, capture_output=True, text=True, timeout=60)


def fn(env, code):
    return subprocess.run(["bash", "-c", f'AOS_BK_SOURCED=1 source "{SCRIPT}"; {code}'], env=env, capture_output=True, text=True, timeout=60)


def test_scripts_parse():
    for f in ("backup_computers.sh", "backup_drill_local.sh", "backup_vps83.sh"):
        r = subprocess.run(["bash", "-n", os.path.join(OPS, f)], capture_output=True, text=True)
        assert r.returncode == 0, f + ": " + r.stderr


def test_only_box_volumes_are_selected(fake):
    env, _ = fake
    r = fn(env, "_vols")
    assert r.stdout.split() == ["aos-box-0123456789abcdef-opt", "aos-box-0123456789abcdef-workspace"], r.stdout     # Odoo / HA / Hermes and malformed names never pass
    # the real docker call asks for the aos.box label too
    assert "--filter label=aos.box=1" in open(env["FAKE_LOG"]).read()


def test_cache_is_excluded_only_for_opt_volume(fake):
    env, _ = fake
    assert fn(env, "_excl aos-box-0123456789abcdef-opt").stdout.strip() == "./cache"
    assert fn(env, "_excl aos-box-0123456789abcdef-workspace").stdout.strip() != "./cache"


def test_restore_refuses_existing_volume_and_bad_checksum(fake, tmp_path):
    env, log = fake
    src = tmp_path / "bk"; (src / "volumes").mkdir(parents=True)
    (src / "volumes" / "aos-box-0123456789abcdef-opt.tgz").write_bytes(b"x")
    subprocess.run("sha256sum volumes/*.tgz > SHA256SUMS", shell=True, cwd=src, check=True)
    env = {**env, "FAKE_EXIST": "aosdrill-aos-box-0123456789abcdef-opt"}
    r = run(env, "restore", str(src), "--prefix", "aosdrill-")
    assert r.returncode == 1 and "refusing to overwrite" in r.stderr
    assert "volume create" not in log.read_text() and "volume rm" not in log.read_text()      # nothing was created or removed
    (src / "volumes" / "aos-box-0123456789abcdef-opt.tgz").write_bytes(b"tampered")
    r = run({**env, "FAKE_EXIST": ""}, "restore", str(src), "--prefix", "aosdrill-")
    assert r.returncode == 1 and "checksum mismatch" in r.stderr
    assert "volume create" not in log.read_text()


def test_vps_script_calls_computers_block_without_running_anything():
    s = open(os.path.join(OPS, "backup_vps83.sh"), encoding="utf-8").read()
    assert 'backup_computers.sh' in s and 'backup "$B/computers"' in s and "aos_secret.key" in s and "oauth_connections" in s
    assert 'exclude=\'agentos/snapshots\'' in s      # the old exclusions stay


@pytest.mark.skipif(os.environ.get("AOS_REAL_SANDBOX") != "1", reason="needs real Docker + local Postgres (AOS_REAL_SANDBOX=1)")
def test_full_drill_on_this_machine():
    r = subprocess.run(["bash", os.path.join(OPS, "backup_drill_local.sh")], capture_output=True, text=True, timeout=900)
    assert r.returncode == 0 and "ALL-PASS" in r.stdout, (r.stdout[-2500:] + r.stderr[-800:])
    assert "FAIL" not in r.stdout
