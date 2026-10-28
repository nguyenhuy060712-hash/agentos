"""GĐ12.48a: restore rehearsal of the part-A state (ops/backup_verify.py seed12/check12). Dumps the local dev DB, restores it into a throw-away database (always dropped) and checks with the
restored master key copy: sealed webhook signing key still signs, invite still validates, skills/templates, alert rule, retention tier, tr_stats_hourly, trace search, dataset, migrations.
A wrong key must fail. Skipped automatically when there is no local Postgres container / master key. Marker rows are removed again; nothing outside the local dev DB is touched."""
import json, os, shutil, subprocess, sys, uuid
import pytest

HERE = os.path.dirname(__file__); APP = os.path.abspath(os.path.join(HERE, "..")); VERIFY = os.path.join(APP, "ops", "backup_verify.py")
PGC = os.environ.get("PG_CONTAINER", "agentos-pg"); PGDB = os.environ.get("PG_DB", "agentos")
LIVE = os.environ.get("PG_URI", f"postgresql://aos:aoslocal@127.0.0.1:5432/{PGDB}"); KEY = os.path.join(os.environ.get("AOS_VAR_DIR", os.path.join(APP, "var")), "aos_secret.key")


def _have_pg():
    if not shutil.which("docker") or not os.path.exists(KEY): return False
    r = subprocess.run(["docker", "exec", PGC, "psql", "-U", "aos", "-d", PGDB, "-Atc", "SELECT to_regclass('ws_invites') IS NOT NULL"], capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip() == "t"


pytestmark = pytest.mark.skipif(not _have_pg(), reason="needs the local Postgres container agentos-pg, the dev master key and migrations up to 0190")


def V(*a, pg, key=KEY):
    env = {k: v for k, v in os.environ.items() if k not in ("AOS_SECRET_KEY", "AOS_SECRET_KEYS")}
    env.update(PG_URI=pg, AOS_SECRET_KEY_FILE=key, PYTHONPATH=os.path.join(APP, "app"))
    return subprocess.run([sys.executable, VERIFY, *a], env=env, capture_output=True, text=True, timeout=300)


def test_restore_rehearsal_gd12_state(tmp_path):
    scr = "aos_t1248a_" + uuid.uuid4().hex[:8]; seed = str(tmp_path / "seed.json"); counts = str(tmp_path / "counts.json"); dump = tmp_path / "x.dump"; base = LIVE.rsplit("/", 1)[0]
    psql = lambda sql, db="postgres": subprocess.run(["docker", "exec", "-i", PGC, "psql", "-U", "aos", "-d", db, "-v", "ON_ERROR_STOP=1", "-Atc", sql], capture_output=True, text=True)      # noqa: E731
    r = V("seed", "--out", seed, pg=LIVE); assert r.returncode == 0, r.stderr[-800:]
    try:
        assert V("counts", "--out", counts, pg=LIVE).returncode == 0
        with open(dump, "wb") as f: assert subprocess.run(["docker", "exec", PGC, "pg_dump", "-U", "aos", "-Fc", PGDB], stdout=f).returncode == 0
        V("unseed", "--in", seed, pg=LIVE)      # the live rows are gone; only the dump has them
        assert psql(f'CREATE DATABASE "{scr}"').returncode == 0
        with open(dump, "rb") as f: rr = subprocess.run(["docker", "exec", "-i", PGC, "pg_restore", "-U", "aos", "-d", scr, "--no-owner", "--exit-on-error"], stdin=f, capture_output=True, text=True)
        assert rr.returncode == 0, rr.stderr[-800:]
        c = V("check", "--in", seed, "--counts", counts, pg=f"{base}/{scr}"); assert c.returncode == 0, c.stdout[-1500:] + c.stderr[-500:]
        res = json.loads(c.stdout.strip().splitlines()[-1])
        assert res["g12"] and all(v == "ok" for v in res["g12"].values()), res["g12"]
        assert res["sweep_failed"] == 0
        wrong = tmp_path / "wrong.key"; wrong.write_text(os.urandom(32).hex()); wrong.chmod(0o600)
        w = V("check", "--in", seed, "--expect-fail", pg=f"{base}/{scr}", key=str(wrong)); assert w.returncode == 0, "a wrong master key must not sign/decrypt: " + w.stdout[-800:]
    finally:
        psql(f'DROP DATABASE IF EXISTS "{scr}" WITH (FORCE)')
        if os.path.exists(seed): V("unseed", "--in", seed, pg=LIVE)      # no-op when already removed
    assert psql("SELECT count(*) FROM pg_database WHERE datname=" + f"'{scr}'").stdout.strip() == "0"
    left = psql("SELECT (SELECT count(*) FROM notify_destinations WHERE name LIKE 'zzdrill-%') + (SELECT count(*) FROM tr_runs WHERE name LIKE 'zzdrill-%') + (SELECT count(*) FROM ws_invites WHERE max_uses=3 AND role='viewer' AND created_by IS NOT NULL AND expires_at > now() + interval '1 day')", PGDB)
    assert left.stdout.strip() == "0", "marker rows left in the live DB"
