"""GD6.11 - off-site backup drill: dump -> restic (encrypted) -> drop DB -> restore into a new DB -> rows match."""
import json, os, shutil, subprocess, uuid
import psycopg, psycopg.conninfo, pytest

from test_gd6_2fa import PG  # same local DB URI as the other GD6 tests
OPS = os.path.join(os.path.dirname(__file__), "..", "app", "ops")
RESTIC = shutil.which("restic") or os.path.expanduser("~/.local/bin/restic")
pytestmark = pytest.mark.skipif(not os.access(RESTIC, os.X_OK), reason="restic binary not installed (~/.local/bin/restic)")
CT, PGU = os.environ.get("PG_CONTAINER", "agentos-pg"), os.environ.get("PG_USER", "aos")
MARK = "MARKER-" + uuid.uuid4().hex
SRC, DST = "aos_drill_src", "aos_drill_dst"


def admin(sql):
    with psycopg.connect(PG, autocommit=True) as c:
        cur = c.execute(sql); return cur.fetchall() if cur.description else None


def rows(db):
    base = psycopg.conninfo.conninfo_to_dict(PG); base["dbname"] = db
    with psycopg.connect(**base) as c:
        return c.execute("SELECT id, val FROM drill ORDER BY id").fetchall()


def sh(script, env, *args):
    e = {**os.environ, "PATH": os.path.dirname(RESTIC) + os.pathsep + os.environ["PATH"], "RESTIC": RESTIC, **env}
    return subprocess.run(["bash", os.path.join(OPS, script), *args], env=e, capture_output=True, text=True, timeout=300)


@pytest.fixture(scope="module")
def drill(tmp_path_factory):
    t = tmp_path_factory.mktemp("offsite")
    pw = t / "pw"; pw.write_text(uuid.uuid4().hex); pw.chmod(0o600)
    code = t / "code"; code.mkdir(); (code / "app.py").write_text("print('hi')"); (code / "var").mkdir(); (code / "var" / "skip.txt").write_text("x")
    (code / ".env").write_text("SECRET_TOKEN=" + MARK + "_ENV")
    env = {"RESTIC_REPOSITORY": str(t / "repo"), "RESTIC_PASSWORD_FILE": str(pw), "PG_CONTAINER": CT, "PG_USER": PGU, "PG_DB": SRC,
           "PG_DUMP_DIR": str(t / "nodumps"), "OFFSITE_WORK_DIR": str(t / "work"), "OFFSITE_STATUS_FILE": str(t / "status.json"),
           "OFFSITE_PATHS": str(code), "FILES_BACKUP_DIR": str(t / "nofiles"), "OFFSITE_CHECK": "1"}
    for d in (SRC, DST): admin(f'DROP DATABASE IF EXISTS "{d}" WITH (FORCE)')
    admin(f'CREATE DATABASE "{SRC}"')
    base = psycopg.conninfo.conninfo_to_dict(PG); base["dbname"] = SRC
    with psycopg.connect(**base, autocommit=True) as c:
        c.execute("CREATE TABLE drill(id int primary key, val text)")
        for i in range(50): c.execute("INSERT INTO drill VALUES (%s, %s)", (i, f"{MARK}-{i}"))
    yield t, env
    for d in (SRC, DST): admin(f'DROP DATABASE IF EXISTS "{d}" WITH (FORCE)')


def test_backup_drop_restore_roundtrip(drill):
    t, env = drill
    expected = rows(SRC)
    r = sh("offsite_backup.sh", env)
    assert r.returncode == 0, r.stderr
    st = json.load(open(env["OFFSITE_STATUS_FILE"]))
    assert st["status"] == "ok" and st["snapshot_id"] and st["size_bytes"] > 0 and st["last_ok"] and st["dump"].endswith(".dump")
    assert "check ok" in st["message"]  # weekly integrity check ran (forced)
    # encrypted at rest: neither the DB marker nor the .env secret appear in any raw repo file
    blob = b"".join(open(os.path.join(d, f), "rb").read() for d, _, fs in os.walk(env["RESTIC_REPOSITORY"]) for f in fs)
    assert MARK.encode() not in blob and b"SECRET_TOKEN" not in blob
    admin(f'DROP DATABASE "{SRC}" WITH (FORCE)')
    ls = sh("offsite_restore.sh", env, "--list")
    assert ls.returncode == 0 and st["snapshot_id"] in ls.stdout
    # restoring into the live DB name requires explicit flags
    bad = sh("offsite_restore.sh", env, "--target", str(t / "x"), "--db", SRC)
    assert bad.returncode == 2 and "live" in bad.stderr
    r = sh("offsite_restore.sh", {**env, "PG_DB": "langgraph"}, "--snapshot", st["snapshot_id"], "--target", str(t / "restored"), "--db", DST)
    assert r.returncode == 0, r.stderr
    assert rows(DST) == expected and len(expected) == 50
    restored = [os.path.join(d, f) for d, _, fs in os.walk(t / "restored") for f in fs]
    assert any(p.endswith("app.py") for p in restored) and any(p.endswith(".env") for p in restored)
    assert not any(p.endswith("skip.txt") for p in restored)  # var/ excluded


def test_retention_runs_and_failure_writes_status(drill):
    t, env = drill
    r = subprocess.run([RESTIC, "forget", "--keep-daily", "7", "--keep-weekly", "4", "--keep-monthly", "6", "--prune"],
                       env={**os.environ, **env}, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    bad = sh("offsite_backup.sh", {**env, "RESTIC_PASSWORD_FILE": str(t / "nope")})
    assert bad.returncode == 1
    st = json.load(open(env["OFFSITE_STATUS_FILE"]))
    assert st["status"] == "error" and st["last_error"] and st["last_ok"]  # last_ok preserved from the good run
