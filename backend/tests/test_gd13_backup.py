"""GĐ13.60: the GĐ13 tables are in the required-table check of the backup script + the drill helper; the drill itself runs only with AOS_REAL_SANDBOX=1."""
import importlib.util, os, re, subprocess, sys
import pytest

OPS = os.path.join(os.path.dirname(__file__), "..", "ops")
NEEDED = ("issue_traces", "issue_events", "engine_project_settings", "engine_runs", "insight_configs", "memory_proposals", "retention_settings", "tool_idem")


def _verify():
    spec = importlib.util.spec_from_file_location("backup_verify", os.path.join(OPS, "backup_verify.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def test_vps_script_checks_every_gd13_table():
    s = open(os.path.join(OPS, "backup_vps83.sh"), encoding="utf-8").read()
    m = re.search(r'miss13=""; for t in ([a-z_ ]+); do', s)
    assert m and tuple(m.group(1).split()) == NEEDED
    assert "GD13 tables missing from dump" in s and "$miss13" in s


def test_drill_helper_lists_same_tables_and_checks_issue_and_memory():
    v = _verify()
    assert tuple(v.G13_TABLES) == NEEDED
    assert set(NEEDED) <= set(v.TABLES)                  # row counts are compared before / after the dump
    assert v.G13_MIGRATIONS[0] == "0200" and v.G13_MIGRATIONS[-1] == "0230"
    assert callable(v.seed13) and callable(v.check13) and callable(v.unseed13)
    s = open(os.path.join(OPS, "backup_drill_local.sh"), encoding="utf-8").read()
    assert "G13_TABLES" in s and 'get("g13"' in s


def test_every_gd13_table_exists_in_a_migration():
    mig = os.path.join(os.path.dirname(__file__), "..", "app", "migrations")
    text = "".join(open(os.path.join(mig, f), encoding="utf-8").read() for f in os.listdir(mig) if f.endswith(".py"))
    for t in NEEDED: assert f"CREATE TABLE IF NOT EXISTS {t}(" in text, t


def test_scripts_parse():
    for f in ("backup_drill_local.sh", "backup_vps83.sh"):
        r = subprocess.run(["bash", "-n", os.path.join(OPS, f)], capture_output=True, text=True)
        assert r.returncode == 0, f + ": " + r.stderr


@pytest.mark.skipif(os.environ.get("AOS_REAL_SANDBOX") != "1", reason="needs real Docker + local Postgres (AOS_REAL_SANDBOX=1)")
def test_drill_checks_gd13_state():
    r = subprocess.run(["bash", os.path.join(OPS, "backup_drill_local.sh")], capture_output=True, text=True, timeout=900)
    assert r.returncode == 0 and "dump has every GĐ13 table (0 missing)" in r.stdout and "GĐ13 state after restore" in r.stdout, r.stdout[-2000:]
