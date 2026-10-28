"""GĐ10 card 10.12 (MCP servers): handlers called in-process against the local Postgres; refresh_mcp is stubbed (no network)."""
import asyncio, os, sys, uuid
import pytest
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos"); os.environ.setdefault("PG_URI", PG)

def with_pool(fn):
    async def go():
        import deepagent_api as A, mcp_registry as R, migrations, psycopg
        os.environ["AOS_EGRESS_ALLOW"] = "127.0.0.0/8"   # GĐ11.23: the URL is netguard-checked (purpose mcp)
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as c: await migrations.run_pending(c)
        pool = AsyncConnectionPool(PG, min_size=1, max_size=3, open=False, kwargs={"autocommit": True, "row_factory": dict_row}); await pool.open()
        old = (A.S.get("pool"), A.S.get("rls"), R.refresh); A.S["pool"], A.S["rls"] = pool, None; A.S.setdefault("mcp_info", {})
        async def noop(ids=None): return {}
        R.refresh = noop
        try: return await fn(R)
        finally:
            A.S["pool"], A.S["rls"], R.refresh = old; await pool.close()
    return asyncio.run(go())

TAG = uuid.uuid4().hex[:8]

def test_mcp_duplicate_409_put_updates_and_url_warning():
    async def t(R):
        n1, n2 = f"t1012a-{TAG}", f"t1012b-{TAG}"; u1, u2 = f"http://127.0.0.1:9/{TAG}/a", f"http://127.0.0.1:9/{TAG}/b"
        try:
            r = await R.add_server(R.McpServer(name=n1, url=u1)); assert r["ok"] and "warning" not in r
            with pytest.raises(HTTPException) as x: await R.add_server(R.McpServer(name=n1, url=u2))
            assert x.value.status_code == 409 and n1 in x.value.detail["hint"] and "PUT" in x.value.detail["hint"]
            r = await R.add_server(R.McpServer(name=n2, url=u1)); assert r["warning"] == f"same URL as {n1}"   # still saved
            assert await R._q("SELECT 1 FROM mcp_servers WHERE name=%s", (n2,), one=True)
            r = await R.upd_server(n2, R.McpServerUpd(url=u2)); assert r["ok"] and "warning" not in r
            row = await R._q("SELECT url,token_env FROM mcp_servers WHERE name=%s", (n2,), one=True); assert row == {"url": u2, "token_env": None}
            r = await R.upd_server(n2, R.McpServerUpd(url=u1)); assert r["warning"] == f"same URL as {n1}"
            with pytest.raises(HTTPException) as x: await R.upd_server(f"nope-{TAG}", R.McpServerUpd(url=u1))
            assert x.value.status_code == 404
        finally:
            await R._q("DELETE FROM mcp_servers WHERE name IN (%s,%s)", (n1, n2))
    with_pool(t)
