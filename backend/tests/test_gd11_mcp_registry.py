"""GĐ11 card 11.23 (MCP registry per workspace). In-process: migration 0020 is applied to the local DB, handlers are called with a faked current user
(admin.USER) on a private pool; servers are in-process FastMCP instances (mcp_registry.make_target replaced). One test talks real HTTP to a FastMCP server on :8023."""
import asyncio, os, sys, threading, time, uuid
import psycopg, pytest
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

os.environ.setdefault("GOOGLE_API_KEY", "x-not-used")
os.environ.setdefault("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")   # app/crypto.py needs it when the file runs alone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import deepagent_api as A  # noqa: E402,I001  (must come first: circular imports)
import admin, migrations, mcp_registry as R  # noqa: E402
from fastmcp import FastMCP  # noqa: E402

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
TAG = uuid.uuid4().hex[:6]
WA, WB = 91001, 91002
def U(ws, role="admin", **kw): return {"id": 900, "username": "t", "role": role, "ws": ws, **kw}

def fake(name, *tools):
    m = FastMCP(name)
    for t in tools:
        m.tool(name=t)(lambda: "ok")
    return m

def run(fn):
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as c:
            await migrations.run_pending(c)
        async with AsyncConnectionPool(PG, min_size=1, max_size=3, open=False, kwargs={"autocommit": True, "row_factory": dict_row}) as pool:
            await pool.open(); A.S["pool"] = pool; A.S["rls"] = admin.rls_ctx
            await A.q("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO aos_rls"); await A.q("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO aos_rls")
            for t in ("mcp_servers", "tool_settings"):   # what admin.rls_init does at boot
                await A.q(f"ALTER TABLE {t} ALTER COLUMN workspace_id SET DEFAULT coalesce(aos_ws(), 1)")
                await admin._policy(t, "aos_ws() IS NULL OR workspace_id = aos_ws()")
            try: return await fn()
            finally:
                await A.q("DELETE FROM mcp_servers WHERE workspace_id IN (%s,%s)", (WA, WB)); await A.q("DELETE FROM tool_settings WHERE workspace_id IN (%s,%s)", (WA, WB))
                for sid in [i for i, e in R._LIVE.items() if e["ws"] in (WA, WB) or i < 0]: R._LIVE.pop(sid, None)
    return asyncio.run(go())

async def as_(user, fn, *a, **kw):
    tok = admin.USER.set(user)
    try: return await fn(*a, **kw)
    finally: admin.USER.reset(tok)

@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("AOS_EGRESS_ALLOW", "127.0.0.0/8")
    servers = {}
    def mt(s, headers):
        if s["url"] in servers.get("_down", set()): raise ConnectionError("connection refused")
        return servers[s["url"]]
    monkeypatch.setattr(R, "make_target", mt)
    async def no_super(): return False
    monkeypatch.setattr(R, "_is_super", no_super)
    return servers

def url(n): return f"http://127.0.0.1:9/{TAG}/{n}"

def test_workspace_isolation_same_tool_name_and_prefix(env):
    env[url("a1")] = fake("a1", "search", "only_a"); env[url("a2")] = fake("a2", "search"); env[url("b1")] = fake("b1", "search")
    async def t():
        r = await as_(U(WA), R.add_server, R.McpServer(name="srv-one", url=url("a1"))); assert r["status"] == "ok"
        await as_(U(WA), R.add_server, R.McpServer(name="srv-two", url=url("a2")))
        await as_(U(WB), R.add_server, R.McpServer(name="srv-one", url=url("b1"), prefix=True))   # same server name in another workspace is fine
        ta, tb = R.tools_for(WA), R.tools_for(WB)
        assert set(ta) == {"srv-one:search", "srv-one:only_a", "srv-two:search"} and set(tb) == {"srv-one:search"}
        na = R.model_names(WA); assert na["srv-one:only_a"] == "only_a" and na["srv-one:search"] == "srv-one__search" and na["srv-two:search"] == "srv-two__search"   # clash -> prefixed
        assert R.model_names(WB)["srv-one:search"] == "srv-one__search"   # prefix=true
        la = await as_(U(WA), R.mcp_list); lb = await as_(U(WB), R.mcp_list)
        assert {s["name"] for s in la["servers"]} == {"srv-one", "srv-two"} and {s["name"] for s in lb["servers"]} == {"srv-one"}
        assert {x["key"] for x in lb["tools"]} == {"srv-one:search"}
        await as_(U(WA), R.toggle, R.ToolToggle(server="srv-one", tool="search", enabled=True))
        assert (await A.enabled_tool_keys(WA))["srv-one:search"] and not (await A.enabled_tool_keys(WB))["srv-one:search"]
        with pytest.raises(HTTPException) as x: await as_(U(WB), R.upd_server, "srv-two", R.McpServerUpd(url=url("a2")))
        assert x.value.status_code == 404   # B cannot touch A's server
        with pytest.raises(HTTPException) as x: await as_(U(WA), R.add_server, R.McpServer(name="srv-one", url=url("a1")))
        assert x.value.status_code == 409
    run(t)

def test_server_down_uses_cache_and_calls_fail_clearly(env):
    env[url("c1")] = fake("c1", "ping")
    async def t():
        r = await as_(U(WA), R.add_server, R.McpServer(name="cache-me", url=url("c1"))); sid = r["id"]
        env["_down"] = {url("c1")}
        out = await as_(U(WA), R.refresh_tools, sid)
        assert not out["ok"] and "đã lưu" in out["status"] and out["tools"] == 1 and "cache-me:ping" in R.tools_for(WA)
        res = await R.tools_for(WA)["cache-me:ping"].ainvoke({})
        assert "không truy cập được" in str(res)
        await as_(U(WA), R.clear_cache, sid)
        out = await as_(U(WA), R.refresh_tools, sid); assert out["tools"] == 0 and "lỗi" in out["status"]   # no cache left -> nothing to list
        env["_down"] = set(); out = await as_(U(WA), R.refresh_tools, sid); assert out["ok"] and out["tools"] == 1
        await as_(U(WA), R.upd_server, "cache-me", R.McpServerUpd(url=url("c1"), throw_on_load_error=True)); env["_down"] = {url("c1")}
        with pytest.raises(HTTPException) as x: await as_(U(WA), R.refresh_tools, sid)
        assert x.value.status_code == 502
    run(t)

def test_filters_and_validation(env):
    env[url("d1")] = fake("d1", "keep", "drop", "other")
    async def t():
        with pytest.raises(HTTPException) as x: await as_(U(WA), R.add_server, R.McpServer(name="filt", url=url("d1"), include_tools=["keep"], exclude_tools=["keep"]))
        assert x.value.status_code == 422
        await as_(U(WA), R.add_server, R.McpServer(name="filt", url=url("d1"), exclude_tools=["drop"]))
        assert set(R.tools_for(WA)) == {"filt:keep", "filt:other"}
        with pytest.raises(HTTPException) as x: await as_(U(WA), R.add_server, R.McpServer(name="hdr", url=url("d1"), auth_type="headers", headers={"X-Key": "real-value"}))
        assert x.value.status_code == 422
        with pytest.raises(HTTPException) as x: await as_(U(WA), R.add_server, R.McpServer(name="ha", url=url("d1")))
        assert x.value.status_code == 409   # reserved
        with pytest.raises(HTTPException) as x: await as_(U(WA), R.add_server, R.McpServer(name="pub", url="http://169.254.169.254/x"))
        assert x.value.status_code == 422   # netguard
        with pytest.raises(HTTPException) as x: await as_(U(WA), R.add_server, R.McpServer(name="pub", url="http://10.1.2.3/x"))
        assert x.value.status_code == 422   # internal address outside AOS_EGRESS_ALLOW
    run(t)

def test_token_env_superadmin_only_demo_viewer_refused(env, monkeypatch):
    env[url("e1")] = fake("e1", "t1")
    async def t():
        with pytest.raises(HTTPException) as x: await as_(U(WA), R.add_server, R.McpServer(name="tok", url=url("e1"), token_env="AOS_MCP_TOKEN_X"))
        assert x.value.status_code == 403
        for u in (U(WA, "viewer"), U(WA, "editor"), U(WA, demo=True)):
            with pytest.raises(HTTPException) as x: await as_(u, R.add_server, R.McpServer(name="nope", url=url("e1")))
            assert x.value.status_code == 403
        async def yes(): return True
        monkeypatch.setattr(R, "_is_super", yes)
        with pytest.raises(HTTPException) as x: await as_(U(WA), R.add_server, R.McpServer(name="tok", url=url("e1"), token_env="HA_TOKEN"))
        assert x.value.status_code == 422   # must start with AOS_MCP_TOKEN_
        await as_(U(WA), R.add_server, R.McpServer(name="tok", url=url("e1"), token_env="AOS_MCP_TOKEN_X"))
        monkeypatch.setattr(R, "_is_super", lambda: asyncio.sleep(0, False))
        with pytest.raises(HTTPException) as x: await as_(U(WA), R.upd_server, "tok", R.McpServerUpd(url=url("e1b"), token_env="AOS_MCP_TOKEN_X"))
        assert x.value.status_code == 403   # a workspace admin cannot repoint a token-carrying server (would leak the token)
    run(t)

def test_max_servers_and_call_confirm(env):
    env[url("f1")] = fake("f1", "do_it")
    async def t():
        r = await as_(U(WA), R.add_server, R.McpServer(name="callme", url=url("f1"))); sid = r["id"]
        with pytest.raises(HTTPException) as x: await as_(U(WA), R.call_tool, sid, R.CallIn(tool="do_it"))
        assert x.value.status_code == 409
        out = await as_(U(WA), R.call_tool, sid, R.CallIn(tool="do_it", confirm=True)); assert "ok" in out["result"]
        with pytest.raises(HTTPException) as x: await as_(U(WB), R.call_tool, sid, R.CallIn(tool="do_it", confirm=True))
        assert x.value.status_code == 404   # other workspace cannot see it
        await A.q("INSERT INTO mcp_servers(name,url,workspace_id) SELECT 'bulk'||g, 'http://x/'||g, %s FROM generate_series(1,49) g", (WA,))
        with pytest.raises(HTTPException) as x: await as_(U(WA), R.add_server, R.McpServer(name="onemore", url=url("f1")))
        assert x.value.status_code == 422
    run(t)

def test_ha_row_workspace1_trusted_and_default_enabled(env):
    env["http://ha.invalid:8123/api/mcp"] = fake("hafake", "homeassistant__GetLiveContext", "homeassistant__HassTurnOn")
    async def t():
        row = {"id": -1, "name": "ha", "workspace_id": 1, "agent_id": None, "url": "http://ha.invalid:8123/api/mcp", "transport": "streamable_http", "token_env": None}
        assert (await R._load_one(row)) == "ok"   # netguard would reject this host (no DNS): the seeded ha row is exempt
        assert "ha:homeassistant__GetLiveContext" in R.tools_for(1)
        en = await A.enabled_tool_keys(1)
        assert en["ha:homeassistant__GetLiveContext"] is True and en["ha:homeassistant__HassTurnOn"] is False
        await A.q("DELETE FROM tool_settings WHERE workspace_id=1 AND server='ha' AND tool IN ('homeassistant__GetLiveContext','homeassistant__HassTurnOn')")
    run(t)

def test_real_http_secret_header_reaches_server(env, monkeypatch):
    import socket, uvicorn
    from fastmcp.server.dependencies import get_http_headers
    m = FastMCP("echo")
    @m.tool
    def whoami() -> str:
        return get_http_headers(include={"x-api-key"}).get("x-api-key", "")
    app = m.http_app(path="/mcp")
    with socket.socket() as sk:   # free port: parallel agents run their own servers
        sk.bind(("127.0.0.1", 0)); port = sk.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    th = threading.Thread(target=srv.run, daemon=True); th.start()
    for _ in range(100):
        if srv.started: break
        time.sleep(0.1)
    async def t():
        import ws_secrets
        from fastmcp.client.transports import StreamableHttpTransport
        monkeypatch.setattr(R, "make_target", lambda s, h: StreamableHttpTransport(s["url"], headers=h))
        await A.q("INSERT INTO ws_secrets(workspace_id,name,value_enc,allowed_hosts,created_by) VALUES(%s,'MCP_KEY',%s,%s,'t') ON CONFLICT DO NOTHING",
                  (WA, ws_secrets.seal_value(WA, "MCP_KEY", "s3cret-value"), ["127.0.0.1"]))
        try:
            r = await as_(U(WA), R.add_server, R.McpServer(name="http-echo", url=f"http://127.0.0.1:{port}/mcp", auth_type="headers", headers={"X-Api-Key": "{{secret:MCP_KEY}}"}))
            assert r["status"] == "ok", r
            tool = R.tools_for(WA)["http-echo:whoami"]
            assert "s3cret-value" in str(await tool.ainvoke({}))
            row = await A.q("SELECT headers FROM mcp_servers WHERE id=%s", (r["id"],), one=True); assert "s3cret-value" not in str(row)   # only the reference is stored
        finally:
            await A.q("DELETE FROM ws_secrets WHERE workspace_id=%s AND name='MCP_KEY'", (WA,)); srv.should_exit = True
    run(t)
