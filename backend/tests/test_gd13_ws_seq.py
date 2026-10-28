"""13.Z: the boot DDL of admin.py must never move workspaces_id_seq backwards (it used to setval to max(id), so the id of a deleted workspace was reused
by the next new workspace and saw that workspace's leftover rows, e.g. a context pack -> 409 in test_gd12_e2e_a)."""
import os, re, sys, uuid
from pathlib import Path
import psycopg

sys.path.insert(0, str(Path(__file__).parent.parent / "app")); sys.path.insert(0, str(Path(__file__).parent))
from test_gd13_engine import PG   # same local DB URI as the other GĐ13 tests
os.environ.setdefault("GOOGLE_API_KEY", "x-not-used")   # importing admin -> deepagent_api needs it; no LLM call is made here

def test_boot_setval_never_reuses_a_deleted_workspace_id():
    import deepagent_api  # noqa: F401  (first app import: modules import each other circularly)
    import admin
    stmt = next(s for s in admin.DDL.split(";") if "setval('workspaces_id_seq'" in s)
    stmt = re.sub(r"^\s*--[^\n]*\n", "", stmt.strip(), flags=re.M)
    with psycopg.connect(PG, autocommit=True) as c:
        wid = c.execute("INSERT INTO workspaces(name) VALUES(%s) RETURNING id", (f"seq-{uuid.uuid4().hex[:8]}",)).fetchone()[0]
        c.execute("DELETE FROM workspaces WHERE id=%s", (wid,))
        c.execute(stmt)                                  # what every boot runs
        nxt = c.execute("SELECT nextval('workspaces_id_seq')").fetchone()[0]
        assert nxt > wid
