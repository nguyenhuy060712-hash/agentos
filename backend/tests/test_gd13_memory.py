"""GĐ13.6 memory part 2: proposals (external write -> pending, approve writes, secret refused), owner API guards, purge, privacy. Real SQL on a throwaway schema
(migration 0220), InMemoryStore for the files; no server, no LLM ($0)."""
import asyncio, importlib.util, os, re, sys, uuid
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")
import psycopg, pytest
from psycopg.rows import dict_row

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import deepagent_api as A  # noqa: E402,I001  (first: circular imports)
import admin, agent_sharing, agent_purge, memory_routes as M, memory_admin as MA, run_trust  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from deepagents.backends import CompositeBackend, StateBackend  # noqa: E402
from langgraph.store.memory import InMemoryStore  # noqa: E402

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
run = asyncio.run
AID = 91
OWNER = {"id": 10, "username": "owner", "role": "editor", "ws": 1, "auth": "session"}
OTHER = {"id": 11, "username": "other", "role": "viewer", "ws": 1, "auth": "session"}
DEMO = {**OWNER, "demo": True}
APIKEY = {**OWNER, "auth": "api_key:abc"}

def _migration():
    p = os.path.join(os.path.dirname(__file__), "..", "app", "migrations", "0220_memory_proposals.py")
    sp = importlib.util.spec_from_file_location("m0220", p); m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m); return m

@pytest.fixture
def env(monkeypatch):
    sch = "t136_" + uuid.uuid4().hex[:8]
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f'CREATE SCHEMA "{sch}"')
        c.execute(f'CREATE TABLE "{sch}".agents(id int primary key, workspace_id int not null)')
        c.execute(f'INSERT INTO "{sch}".agents VALUES ({AID}, 1)')
    async def q(sql, args=(), one=False, many=True):
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True, options=f"-csearch_path={sch}", row_factory=dict_row) as c:
            cur = await c.execute(sql, args)
            if cur.description is None: return None
            return await (cur.fetchone() if one else cur.fetchall())
    async def mq(sql):
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
            cur = await c.execute(sql)
            return await cur.fetchall() if cur.description else None
    run(_migration().up(mq))
    monkeypatch.setattr(MA, "_q", q)
    st = InMemoryStore(); monkeypatch.setitem(A.S, "store", st)
    async def access(aid, deleted_ok=False):
        u = admin.cur() or {}; ed = u.get("role") in ("editor", "admin") and not u.get("demo")
        return {"edit": ed, "use": True, "owner": u.get("id") == 10, "admin": u.get("role") == "admin"}
    monkeypatch.setattr(agent_sharing, "access", access)
    monkeypatch.setattr(M, "_mask", lambda t: t)
    be = CompositeBackend(default=StateBackend(), routes=M.routes(AID))
    yield be, q, st
    with psycopg.connect(PG, autocommit=True) as c: c.execute(f'DROP SCHEMA "{sch}" CASCADE')

def go(user, coro, external=False):
    async def w():
        tok = admin.USER.set(user)
        try:
            if external:
                with run_trust.scope(True): return await coro
            return await coro
        finally: admin.USER.reset(tok)
    return run(w())

def test_external_write_becomes_proposal_nothing_written_then_approve_writes(env):
    be, q, _ = env
    r = go(OWNER, be.awrite("/memories/agent/vip.md", "Khách VIP thích trà"), external=True)
    assert r.error and "proposal" in r.error
    assert go(OWNER, be.aread("/memories/agent/vip.md")).error            # nothing written
    props = go(OWNER, MA.proposals(AID))
    assert len(props) == 1 and props[0]["path"] == "/vip.md" and props[0]["source"] == "external"
    assert go(OWNER, MA.approve(AID, props[0]["id"]))["ok"]
    assert "trà" in go(OWNER, be.aread("/memories/agent/vip.md")).file_data["content"]
    assert go(OWNER, MA.proposals(AID)) == []
    with pytest.raises(HTTPException) as e: go(OWNER, MA.approve(AID, props[0]["id"]))   # already decided
    assert e.value.status_code == 404

def test_api_key_write_is_a_proposal_secret_text_is_not(env):
    be, q, _ = env
    assert "proposal" in go(APIKEY, be.awrite("/memories/agent/a.md", "ghi chú")).error
    assert go(APIKEY, be.awrite("/memories/agent/b.md", "password: hunter2hunter2")).error == M.MSG_READ_ONLY
    assert len(go(OWNER, MA.proposals(AID))) == 1

def test_secret_proposal_refused_when_approved_and_reject(env):
    be, q, _ = env
    run(q("INSERT INTO memory_proposals(workspace_id, agent_id, path, content) VALUES (1,%s,'/k.md','api_key: sk-abcdefghijklmnopqrstuvwx')", (AID,)))
    run(q("INSERT INTO memory_proposals(workspace_id, agent_id, path, content) VALUES (1,%s,'/n.md','bình thường')", (AID,)))
    ps = {p["path"]: p["id"] for p in go(OWNER, MA.proposals(AID))}
    with pytest.raises(HTTPException) as e: go(OWNER, MA.approve(AID, ps["/k.md"]))
    assert e.value.status_code == 422
    assert go(OWNER, be.aread("/memories/agent/k.md")).error
    assert go(OWNER, MA.reject(AID, ps["/n.md"]))["ok"]
    assert go(OWNER, be.aread("/memories/agent/n.md")).error
    assert [p["path"] for p in go(OWNER, MA.proposals(AID))] == ["/k.md"]        # still pending, not written

def test_max_20_pending_and_same_path_refreshes(env):
    be, q, _ = env
    for i in range(25): go(APIKEY, be.awrite(f"/memories/agent/f{i}.md", "x"))
    assert len(go(OWNER, MA.proposals(AID))) == 20
    go(APIKEY, be.awrite("/memories/agent/f3.md", "mới"))
    ps = go(OWNER, MA.proposals(AID)); assert len(ps) == 20 and [p for p in ps if p["path"] == "/f3.md"][0]["content"] == "mới"

def test_owner_api_view_edit_delete_and_guards(env):
    be, q, _ = env
    assert go(OWNER, MA.write_file(AID, MA.FileBody(path="ghi_chu.md", content="hello")))["path"] == "/ghi_chu.md"
    assert go(OWNER, MA.files(AID))[0]["path"] == "/ghi_chu.md"
    assert go(OWNER, MA.read_file(AID, "/ghi_chu.md"))["content"] == "hello"
    with pytest.raises(HTTPException) as e: go(OWNER, MA.write_file(AID, MA.FileBody(path="/s.md", content="token: abcdefghijkl")))
    assert e.value.status_code == 422
    for who in (OTHER, DEMO):
        with pytest.raises(HTTPException) as e: go(who, MA.files(AID))
        assert e.value.status_code == 403
    assert go(OWNER, MA.delete_file(AID, "/ghi_chu.md"))["ok"]
    with pytest.raises(HTTPException) as e: go(OWNER, MA.read_file(AID, "/ghi_chu.md"))
    assert e.value.status_code == 404

def test_user_layer_isolated_between_users(env):
    be, q, st = env
    async def seed():
        await st.aput(("agent", str(AID), "u10", "memories"), "abc", {"text": "của A"})
        await st.aput(("agent", str(AID), "u11", "memories"), "zzz", {"text": "của B"})
    run(seed())
    assert [e["path"] for e in go(OWNER, be.als("/memories/user/")).entries] == ["/memories/user/abc.md"]
    assert [e["path"] for e in go({**OTHER, "role": "editor"}, be.als("/memories/user/")).entries] == ["/memories/user/zzz.md"]

def test_demo_adds_no_proposal_and_agent_delete_removes_them(env):
    be, q, _ = env
    assert go(DEMO, MA.add_proposal(AID, "/x.md", "y")) is False
    go(APIKEY, be.awrite("/memories/agent/a.md", "x"))
    assert "memory_proposals" in agent_purge.PURGED and "memory_proposals" in admin.RLS_DIRECT
    run(q("DELETE FROM agents WHERE id=%s", (AID,)))                       # FK cascade = the DB side of the purge
    assert run(q("SELECT count(*) n FROM memory_proposals", one=True))["n"] == 0

def test_privacy_export_and_erase_cover_user_layer_prefix():
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "privacy.py"), encoding="utf8").read()
    assert src.count(r"u{uid}\.memories") >= 2            # me/export and me/erase both select the per-person namespace
    rx = r"^agent\.[0-9]+\.u10\.memories$"
    assert re.match(rx, "agent.91.u10.memories") and not re.match(rx, "agent.91.memories")   # the agent layer is workspace data, not per person
