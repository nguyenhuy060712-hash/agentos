"""GĐ9.6 (24/10/2026): open standards with the OFFICIAL clients — langgraph-sdk 0.4.9 (Agent Server API, every stream mode, interrupt/resume, resumable
streams), mcp 2.3 (spec 2026-07-28 stateless + older initialize-handshake clients), `langchain.mcp` (our MCP client), a2a-sdk 1.2.3 (A2A 1.0).
Local server :8020 + fake LLM => $0. Throwaway workspace/users/agent; tests share state through the `env` fixture."""
import asyncio, base64, contextlib, json, os, socket, threading, time, uuid
import httpx, psycopg, pytest
from test_fixes import B, ADMIN, a  # noqa: F401  (fixtures)

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
PW = "phase0-gd96-local"; SUF = uuid.uuid4().hex[:6]
API = B + "/api/v1"
MODES = ["values", "updates", "messages-tuple", "messages", "events", "custom", "debug", "tasks", "checkpoints"]

def basic(u, p=PW): return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}

def pg(): return psycopg.connect(PG, autocommit=True)

def user(a, name, ws, role="editor"):
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": role, "workspace_id": ws}); assert r.status_code == 200, r.text
    return r.json()["id"], httpx.Client(base_url=B, auth=(name, PW), timeout=150)

@pytest.fixture(scope="module")
def env(a):
    ws = a.post("/api/admin/workspaces", json={"name": f"std-w-{SUF}"}).json()["id"]
    e = {"ws": ws}
    for k, role in (("own", "editor"), ("oth", "editor"), ("vw", "viewer")):
        e[k + "_id"], e[k] = user(a, f"std_{k}_{SUF}", ws, role); e[k + "_name"] = f"std_{k}_{SUF}"
    r = e["own"].post("/api/agents", json={"name": f"Trợ lý chuẩn {SUF}", "description": "Agent thử chuẩn mở 9.6", "system_prompt": "Bạn là trợ lý.",
                                            "tools": ["local:content_publish"], "interrupt_on": ["local:content_publish"]}); assert r.status_code == 200, r.text
    e["agent"] = r.json()["id"]; e["graph"] = f"agent-{e['agent']}"
    k = e["own"].post("/api/auth/keys", json={"name": "gd96", "scope": "full"}).json(); e["key"], e["key_id"] = k["key"], k["id"]
    kr = e["own"].post("/api/auth/keys", json={"name": "gd96r", "scope": "read"}).json(); e["rkey"], e["rkey_id"] = kr["key"], kr["id"]
    yield e
    for k in ("own", "oth", "vw"): e[k].close()
    with pg() as c:
        c.execute("DELETE FROM approvals WHERE agent_id IN (SELECT id FROM agents WHERE workspace_id=%s)", (ws,))
        c.execute("DELETE FROM a2a_tasks WHERE workspace_id=%s", (ws,))
        c.execute("DELETE FROM eval_runs WHERE agent_id IN (SELECT id FROM agents WHERE workspace_id=%s)", (ws,))
        c.execute("DELETE FROM lg_runs WHERE workspace_id=%s", (ws,)); c.execute("DELETE FROM lg_threads WHERE workspace_id=%s", (ws,))
        c.execute("DELETE FROM agents WHERE workspace_id=%s", (ws,))
        c.execute("DELETE FROM user_api_keys WHERE workspace_id=%s", (ws,))
        c.execute("DELETE FROM users WHERE username LIKE %s", (f"std\\_%\\_{SUF}",))

def H(env, who="own"): return basic(env[who + "_name"])
def KEY(env): return {"X-Api-Key": env["key"]}

# ================================================================== (a) langgraph-sdk against our Agent Server API
def sdk(env, who="own", **kw):
    from langgraph_sdk import get_client
    return get_client(url=API, headers=kw.pop("headers", H(env, who)), timeout=120)

def human(t): return {"messages": [{"type": "human", "content": t}]}

async def _collect(it): return [p async for p in it]

def test_sdk_version_is_latest():
    import importlib.metadata as m
    assert m.version("langgraph-sdk") == "0.4.9"

def test_sdk_assistants_threads_runs(env):
    async def go():
        c = sdk(env); g = env["graph"]
        found = await c.assistants.search(graph_id=g, limit=5)
        assert found and found[0]["graph_id"] == g
        asst = await c.assistants.get(found[0]["assistant_id"]); assert asst["assistant_id"] == found[0]["assistant_id"]
        t = await c.threads.create(metadata={"graph_id": g, "src": SUF}); tid = t["thread_id"]
        assert any(x["thread_id"] == tid for x in await c.threads.search(metadata={"src": SUF}, limit=10))
        out = await c.runs.wait(tid, g, input=human(f"echo-user wait {SUF}"))
        assert f"USER<<echo-user wait {SUF}>>" in json.dumps(out)
        st = await c.threads.get_state(tid); assert st["values"]["messages"][-1]["type"] == "ai" and st["next"] == []
        hist = await c.threads.get_history(tid); assert len(hist) >= 2 and hist[0]["checkpoint"]["checkpoint_id"] == st["checkpoint"]["checkpoint_id"]
        run = await c.runs.create(tid, g, input=human(f"echo-user bg {SUF}"))
        joined = await c.runs.join(tid, run["run_id"]); assert f"echo-user bg {SUF}" in json.dumps(joined)
        runs = await c.runs.list(tid); assert {r["run_id"] for r in runs} >= {run["run_id"]} and all(r["status"] == "success" for r in runs)
        assert (await c.runs.get(tid, run["run_id"]))["status"] == "success"
        # store
        await c.store.put_item(["gd96", SUF], "k1", {"v": 1})
        assert (await c.store.get_item(["gd96", SUF], "k1"))["value"] == {"v": 1}
        assert (await c.store.search_items(["gd96", SUF]))["items"]
        await c.store.delete_item(["gd96", SUF], "k1")
    asyncio.run(go())

def test_sdk_every_stream_mode(env):
    """Each mode the SDK offers returns its own event type (v1 SSE format)."""
    async def go():
        c = sdk(env); g = env["graph"]; tid = (await c.threads.create(metadata={"graph_id": g}))["thread_id"]
        got = {}
        for m in MODES:
            parts = await _collect(c.runs.stream(tid, g, input=human(f"echo-user mode {m} {SUF}"), stream_mode=m))
            got[m] = parts
            ev = [p.event for p in parts]
            assert ev[0] == "metadata" and ev[-1] == "end" and "error" not in ev, (m, [(p.event, str(p.data)[:300]) for p in parts if p.event == "error"])
        want = {"values": "values", "messages-tuple": "messages", "messages": "messages/partial", "events": "events", "debug": "debug", "tasks": "tasks", "checkpoints": "checkpoints"}
        for m, e in want.items(): assert any(p.event == e for p in got[m]), (m, [p.event for p in got[m]])
        # messages-tuple = [chunk, metadata]; events = astream_events shape; debug / tasks / checkpoints carry LangGraph payloads
        mt = next(p for p in got["messages-tuple"] if p.event == "messages").data; assert mt[0]["type"] == "AIMessageChunk" and "langgraph_node" in mt[1]
        evs = [p.data for p in got["events"] if p.event == "events"]
        kinds = {e["event"] for e in evs}; assert {"on_chain_start", "on_chat_model_stream", "on_chain_end"} <= kinds and all({"name", "run_id", "data"} <= set(e) for e in evs)
        assert {p.data["type"] for p in got["debug"] if p.event == "debug"} >= {"checkpoint", "task"}
        tk = next(p for p in got["tasks"] if p.event == "tasks").data; assert tk["id"] and tk["name"]
        ck = next(p for p in got["checkpoints"] if p.event == "checkpoints").data; assert ck["config"]["configurable"]["checkpoint_id"] and "values" in ck
        # nothing leaks across modes: asking for `messages-tuple` alone gives no values-before-end noise besides the final `values` snapshot
        assert {p.event for p in got["custom"]} <= {"metadata", "values", "custom", "end"}
        # several modes at once
        parts = await _collect(c.runs.stream(tid, g, input=human(f"echo-user multi {SUF}"), stream_mode=["values", "messages-tuple", "tasks"]))
        assert {"values", "messages", "tasks"} <= {p.event for p in parts}
    asyncio.run(go())

def test_sdk_updates_mode_with_tool_step_and_v2_format(env):
    async def go():
        c = sdk(env); g = env["graph"]; tid = (await c.threads.create(metadata={"graph_id": g}))["thread_id"]
        parts = await _collect(c.runs.stream(tid, g, input=human(f"please publish the post {SUF}"), stream_mode=["updates", "values"]))
        assert any(p.event == "updates" for p in parts) and any(p.event == "updates" and "__interrupt__" in json.dumps(p.data) for p in parts)
        # v2 format (client-side wrap of the same SSE): typed dicts with type / ns / data
        tid2 = (await c.threads.create(metadata={"graph_id": g}))["thread_id"]
        v2 = await _collect(c.runs.stream(tid2, g, input=human(f"echo-user v2 {SUF}"), stream_mode=["values", "messages-tuple"], version="v2"))
        assert v2 and all(isinstance(x, dict) and {"type", "ns", "data"} <= set(x) for x in v2), v2[:2]
        assert {"values", "messages"} <= {x["type"] for x in v2}
    asyncio.run(go())

def test_sdk_interrupt_and_resume(env):
    async def go():
        c = sdk(env); g = env["graph"]; tid = (await c.threads.create(metadata={"graph_id": g}))["thread_id"]
        await _collect(c.runs.stream(tid, g, input=human(f"please publish the post {SUF}"), stream_mode=["values", "updates"]))
        st = await c.threads.get_state(tid)
        assert st["next"] and st["interrupts"], st
        req = st["interrupts"][0]["value"]["action_requests"][0]; assert req["name"] == "content_publish"
        assert (await c.runs.list(tid))[0]["status"] == "interrupted"
        parts = await _collect(c.runs.stream(tid, g, command={"resume": {"decisions": [{"type": "approve"}]}}, stream_mode=["values"]))
        assert parts[-1].event == "end" and "error" not in [p.event for p in parts]
        st2 = await c.threads.get_state(tid); assert st2["next"] == [] and not st2["interrupts"]
        assert any(m["type"] == "tool" for m in st2["values"]["messages"])
        assert (await c.runs.list(tid))[0]["status"] == "success"
        # reject path
        tid3 = (await c.threads.create(metadata={"graph_id": g}))["thread_id"]
        await _collect(c.runs.stream(tid3, g, input=human(f"please publish the post again {SUF}")))
        await _collect(c.runs.stream(tid3, g, command={"resume": {"decisions": [{"type": "reject", "message": "no"}]}}))
        assert (await c.threads.get_state(tid3))["next"] == []
    asyncio.run(go())

def test_sdk_resumable_streams_and_cancel(env):
    async def go():
        c = sdk(env); g = env["graph"]; tid = (await c.threads.create(metadata={"graph_id": g}))["thread_id"]
        created = []
        parts = await _collect(c.runs.stream(tid, g, input=human(f"echo-user resume {SUF}"), stream_mode=["values", "messages-tuple"], stream_resumable=True,
                                             on_run_created=lambda m: created.append(m["run_id"])))
        rid = created[0]
        full = await _collect(c.runs.join_stream(tid, rid)); ids = [p.id for p in full if p.id]
        assert len(ids) >= 4 and full[-1].event == "end"
        mid = ids[len(ids) // 2]
        rest = await _collect(c.runs.join_stream(tid, rid, last_event_id=mid))   # Last-Event-ID -> only what came after
        assert rest and all(int(p.id) > int(mid) for p in rest if p.id) and rest[-1].event == "end" and len(rest) < len(full)
        only = await _collect(c.runs.join_stream(tid, rid, stream_mode="messages-tuple"))   # server-side stream_mode filter
        assert {p.event for p in only} <= {"metadata", "messages", "messages/partial", "end", "error"} and any(p.event == "messages" for p in only)
        # thread-level join_stream (run_modes / state_update)
        th = await _collect(await c.threads.join_stream(tid)); assert th and th[-1].event == "end"
        su = await _collect(await c.threads.join_stream(tid, stream_mode="state_update")); assert su and {p.event for p in su} <= {"metadata", "values", "end", "error"}
        # cancel a scheduled (pending) run
        run = await c.runs.create(tid, g, input=human(f"echo-user later {SUF}"), after_seconds=60)
        await c.runs.cancel(tid, run["run_id"]); assert (await c.runs.get(tid, run["run_id"]))["status"] == "interrupted"
    asyncio.run(go())

def test_body_without_json_content_type_is_accepted(env):
    """agent-chat-ui's Next.js passthrough re-sends bodies as text/plain (or with no content type); the LangGraph server still reads JSON."""
    for ct in ({"content-type": "text/plain;charset=UTF-8"}, {}):
        with httpx.Client(base_url=API, headers=KEY(env), timeout=60) as c:
            r = c.request("POST", "/threads/search", content=json.dumps({"metadata": {"graph_id": env["graph"]}, "limit": 5}).encode(), headers=ct)
            assert r.status_code == 200 and isinstance(r.json(), list), (ct, r.status_code, r.text[:200])
            r = c.request("POST", "/threads", content=json.dumps({"metadata": {"src": SUF}}).encode(), headers=ct); assert r.status_code == 200 and r.json()["thread_id"], r.text[:200]

def test_sdk_foreign_and_unauthorised(env):
    async def go():
        from langgraph_sdk import get_client
        from langgraph_sdk.errors import APIStatusError
        c = sdk(env); tid = (await c.threads.create(metadata={"graph_id": env["graph"]}))["thread_id"]
        with pytest.raises(Exception): await get_client(url=API, headers={}).threads.get(tid)           # no credentials
        k = get_client(url=API, headers=KEY(env)); assert (await k.threads.get(tid))["thread_id"] == tid   # X-Api-Key (what agent-chat-ui sends)
        with pytest.raises(Exception): await get_client(url=API, headers={"X-Api-Key": "aos_nope"}).threads.get(tid)
        assert issubclass(APIStatusError, Exception)
    asyncio.run(go())

# ================================================================== (b) MCP servers (mcp 2.x) and our MCP client (langchain.mcp)
async def _mcp2(url, headers, mode="auto", op="list_tools", *a):
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    async with httpx.AsyncClient(headers=headers, timeout=120) as hc:
        async with Client(streamable_http_client(url, http_client=hc), mode=mode) as c:
            proto = c.session.protocol_version if hasattr(c.session, "protocol_version") else None
            return proto, await getattr(c, op)(*a)

def test_mcp_versions_pinned():
    import importlib.metadata as m
    assert m.version("mcp") == "2.3.0" and m.version("fastmcp") == "4.1.3"
    with pytest.raises(Exception): import langchain_mcp_adapters  # noqa: F401  (archived, removed)

@pytest.mark.parametrize("mode,proto", [("auto", "2026-07-28"), ("legacy", "2025-11-25")])
def test_mcp2_client_on_aos_server(env, mode, proto):
    v, tools = asyncio.run(_mcp2(B + "/mcp/aos", H(env), mode)); assert v == proto
    names = {t.name for t in tools.tools}; assert {"query_runs", "get_trace", "run_experiment"} <= names
    assert all(t.description and t.input_schema.get("properties") is not None for t in tools.tools)
    _, r = asyncio.run(_mcp2(B + "/mcp/aos", H(env), mode, "call_tool", "list_datasets", {})); assert not r.is_error and r.content[0].text.startswith("{")
    _, r = asyncio.run(_mcp2(B + "/mcp/aos", H(env), mode, "call_tool", "get_trace", {"trace_id": "not-a-uuid"})); assert r.is_error   # tool error, not a protocol crash
    _, r = asyncio.run(_mcp2(B + "/mcp/aos", H(env), mode, "call_tool", "no_such_tool", {})); assert r.is_error

def test_mcp_older_protocol_clients_still_work(env):
    """Raw JSON-RPC like a 2024-11-05 / 2025-06-18 / 2025-11-25 client: initialize -> tools/list -> tools/call; the server answers with a version it supports."""
    hd = {**H(env), "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    with httpx.Client(headers=hd, timeout=60) as hc:
        for pv in ("2025-11-25", "2025-06-18", "2024-11-05"):
            r = hc.post(B + "/mcp/aos", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": pv, "capabilities": {}, "clientInfo": {"name": "old", "version": "1"}}})
            res = r.json()["result"]; assert r.status_code == 200 and res["protocolVersion"] <= "2025-11-25" and res["serverInfo"]["name"] == "moza-agent-os", (pv, r.text[:200])
            h2 = {"MCP-Protocol-Version": res["protocolVersion"]}
            tl = hc.post(B + "/mcp/aos", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=h2).json()["result"]["tools"]; assert any(t["name"] == "query_runs" for t in tl)
            cr = hc.post(B + "/mcp/aos", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_datasets", "arguments": {}}}, headers=h2).json()["result"]
            assert cr["isError"] is False and cr["content"][0]["type"] == "text"
        assert hc.post(B + "/mcp/aos", json={"jsonrpc": "2.0", "id": 4, "method": "ping"}).json()["result"] == {}
    assert httpx.post(B + "/mcp/aos", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"Accept": "application/json, text/event-stream"}).status_code in (401, 403)

def test_mcp_agent_endpoint_both_generations_and_sharing(env):
    url = f"{B}/mcp/agents/{env['agent']}"
    for mode in ("auto", "legacy"):
        _, tl = asyncio.run(_mcp2(url, H(env), mode)); assert [t.name for t in tl.tools] == [f"ask_tro_ly_chuan_{SUF}"]
        _, r = asyncio.run(_mcp2(url, H(env), mode, "call_tool", tl.tools[0].name, {"message": f"echo-user via mcp {mode} {SUF}"}))
        assert not r.is_error and r.content[0].text == f"USER<<echo-user via mcp {mode} {SUF}>>"
        _, res = asyncio.run(_mcp2(url, H(env), mode, "list_resources")); assert res.resources[0].uri.startswith("agent://")
    _, r = asyncio.run(_mcp2(url, KEY(env), "auto", "call_tool", tl.tools[0].name, {"message": "please publish the post"})); assert "needs approval" in r.content[0].text   # HITL -> text, approve in Inbox
    assert env["own"].post(f"/api/agents/{env['agent']}/shares", json={"visibility": "private"}).status_code == 200
    try:
        with pytest.raises(Exception): asyncio.run(_mcp2(url, H(env, "oth")))        # private + not shared -> hidden
        with pytest.raises(Exception): asyncio.run(_mcp2(url, {}))                    # no credentials
    finally: env["own"].post(f"/api/agents/{env['agent']}/shares", json={"visibility": "workspace"})

def test_mcp_root_endpoint_list_and_ask_agent(env):
    _, tl = asyncio.run(_mcp2(B + "/mcp", H(env))); assert {t.name for t in tl.tools} == {"list_agents", "ask_agent"}
    _, r = asyncio.run(_mcp2(B + "/mcp", H(env), "auto", "call_tool", "list_agents", {})); assert str(env["agent"]) in r.content[0].text
    _, r = asyncio.run(_mcp2(B + "/mcp", H(env), "legacy", "call_tool", "ask_agent", {"agent_id": env["agent"], "message": f"echo-user root {SUF}"})); assert r.content[0].text == f"USER<<echo-user root {SUF}>>"

def test_langchain_mcp_client_loads_and_calls_tools(env):
    """Our MCP client path = langchain.mcp.MCPAdapter (FastMCP client) -> LangChain tools; against an in-process server AND our own /mcp/agents/{id}."""
    import warnings
    from langchain_core._api import LangChainBetaWarning
    warnings.filterwarnings("ignore", category=LangChainBetaWarning)
    from fastmcp import FastMCP
    from fastmcp.client.transports import StreamableHttpTransport
    from langchain.mcp import MCPAdapter
    srv = FastMCP("gd96-test")
    @srv.tool
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b
    async def go():
        tools = await MCPAdapter(srv).list_tools()
        assert [t.name for t in tools] == ["add"] and tools[0].description.startswith("Add")
        out = await tools[0].ainvoke({"a": 2, "b": 3}); assert "5" in str(out)
        tr = StreamableHttpTransport(f"{B}/mcp/agents/{env['agent']}", headers=KEY(env))
        remote = await MCPAdapter(tr).list_tools(); assert len(remote) == 1 and remote[0].name.startswith("ask_tro_ly_chuan")
        res = await remote[0].ainvoke({"message": f"echo-user langchain.mcp {SUF}"}); assert f"USER<<echo-user langchain.mcp {SUF}>>" in str(res)
        # the whole Agent OS tool set through the same client
        aos = await MCPAdapter(StreamableHttpTransport(B + "/mcp/aos", headers=KEY(env))).list_tools(); assert "query_runs" in {t.name for t in aos}
    asyncio.run(go())

def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

def test_server_refresh_mcp_uses_langchain_mcp(a):
    """POST /api/mcp/servers -> deepagent_api.refresh_mcp connects with langchain.mcp and registers `<server>:<tool>`."""
    from fastmcp import FastMCP
    srv = FastMCP("gd96-ext");
    @srv.tool
    def ping96(x: str) -> str:
        """Echo x."""
        return x
    port = _free_port()
    threading.Thread(target=lambda: srv.run(transport="http", host="127.0.0.1", port=port, show_banner=False), daemon=True).start()
    for _ in range(50):
        try: socket.create_connection(("127.0.0.1", port), timeout=0.2).close(); break
        except OSError: time.sleep(0.2)
    name = f"gd96-{SUF}"[:30].lower().replace("_", "-")
    try:
        r = a.post("/api/mcp/servers", json={"name": name, "url": f"http://127.0.0.1:{port}/mcp", "transport": "streamable_http"}); assert r.status_code == 200, r.text
        assert r.json()["status"] == "ok", r.text
        info = a.get("/api/mcp").json(); assert any(t.get("key", f"{t.get('server')}:{t.get('name')}") .endswith("ping96") for t in info["tools"]), info["tools"][:3]
    finally:
        a.delete(f"/api/mcp/servers/{name}")

# ================================================================== (c) A2A 1.0 with the official a2a-sdk client
@contextlib.asynccontextmanager
async def _a2a(url, headers, streaming=True):
    from a2a.client import ClientConfig, create_client
    hc = httpx.AsyncClient(headers=headers, timeout=120)
    try: yield await create_client(url, ClientConfig(httpx_client=hc, streaming=streaming))
    finally: await hc.aclose()

def _msg(text, **kw):
    from a2a.helpers.proto_helpers import new_text_message
    from a2a.types.a2a_pb2 import Role, SendMessageRequest
    return SendMessageRequest(message=new_text_message(text, role=Role.ROLE_USER, **kw))

async def _task(cl, text):
    """Non-streaming send -> the Task the server returns."""
    t = None
    async for e in cl.send_message(_msg(text)):
        if e.HasField("task"): t = e.task
    return t

def test_a2a_versions_pinned():
    import importlib.metadata as m
    assert m.version("a2a-sdk") == "1.2.3"

def test_a2a_agent_card_per_agent(env):
    url = f"{B}/a2a/{env['agent']}/.well-known/agent-card.json"
    r = httpx.get(url, headers=H(env)); assert r.status_code == 200, r.text
    card = r.json()
    assert card["name"].startswith("Trợ lý chuẩn") and card["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC" and card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
    assert card["supportedInterfaces"][0]["url"].endswith(f"/a2a/{env['agent']}") and card["capabilities"]["streaming"] is True
    assert "bearer" in card["securitySchemes"] and card["skills"][0]["id"] == f"agent-{env['agent']}"
    assert card["protocolVersion"].startswith("0.3") and card["url"].endswith(f"/a2a/{env['agent']}")   # v0.3 top-level fields for older clients
    assert httpx.get(url).status_code == 401                                                    # card needs our credentials too
    assert httpx.get(f"{B}/a2a/{env['agent']}/.well-known/agent-card.json", headers=KEY(env)).status_code == 200   # X-Api-Key accepted
    lst = httpx.get(f"{B}/a2a", headers=H(env)).json(); assert any(c["name"].startswith("Trợ lý chuẩn") for c in lst)
    assert httpx.get(f"{B}/a2a/99999999/.well-known/agent-card.json", headers=H(env)).status_code == 404

def test_a2a_send_stream_get_list(env):
    from a2a.types.a2a_pb2 import GetTaskRequest, ListTasksRequest, TaskState
    url = f"{B}/a2a/{env['agent']}"
    async def go():
        async with _a2a(url, H(env), False) as cl:
            evs = [e async for e in cl.send_message(_msg(f"echo-user a2a send {SUF}"))]
            task = next(e.task for e in evs if e.HasField("task"))
            assert task.status.state == TaskState.TASK_STATE_COMPLETED and f"USER<<echo-user a2a send {SUF}>>" in "".join(p.text for ar in task.artifacts for p in ar.parts)
            got = await cl.get_task(GetTaskRequest(id=task.id)); assert got.id == task.id and got.status.state == TaskState.TASK_STATE_COMPLETED
            lst = await cl.list_tasks(ListTasksRequest(context_id=task.context_id)); assert [t.id for t in lst.tasks] == [task.id]
        async with _a2a(url, KEY(env), True) as cl2:
            kinds, text, final = [], "", None
            async for e in cl2.send_message(_msg(f"echo-user a2a stream {SUF}")):
                k = e.WhichOneof("payload"); kinds.append(k)
                if k == "artifact_update": text += "".join(p.text for p in e.artifact_update.artifact.parts)
                if k == "status_update": final = e.status_update.status.state
            assert kinds[0] == "task" and "status_update" in kinds and kinds.count("artifact_update") >= 2     # token-level chunks
            assert text == f"USER<<echo-user a2a stream {SUF}>>" and final == TaskState.TASK_STATE_COMPLETED
    asyncio.run(go())

def test_a2a_input_required_then_cancel(env):
    """A run paused for human approval -> task INPUT_REQUIRED; tasks/cancel then moves it to CANCELED; a finished task cannot be canceled."""
    from a2a.types.a2a_pb2 import CancelTaskRequest, TaskState
    from a2a.utils.errors import TaskNotCancelableError
    url = f"{B}/a2a/{env['agent']}"
    async def go():
        async with _a2a(url, H(env), False) as cl:
            task = await _task(cl, f"please publish the post {SUF}")
            assert task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED and "needs approval" in task.status.message.parts[0].text
            c = await cl.cancel_task(CancelTaskRequest(id=task.id)); assert c.status.state == TaskState.TASK_STATE_CANCELED
            done = await _task(cl, f"echo-user done {SUF}")
            with pytest.raises(TaskNotCancelableError): await cl.cancel_task(CancelTaskRequest(id=done.id))
    asyncio.run(go())

def test_a2a_old_method_names_still_work(env):
    body = {"jsonrpc": "2.0", "id": 7, "method": "message/send", "params": {"message": {"kind": "message", "role": "user", "messageId": uuid.uuid4().hex, "parts": [{"kind": "text", "text": f"echo-user old a2a {SUF}"}]}}}
    r = httpx.post(f"{B}/a2a/{env['agent']}", json=body, headers=H(env), timeout=60).json()
    assert r["result"]["status"]["state"] == "completed" and f"USER<<echo-user old a2a {SUF}>>" in json.dumps(r["result"])

def test_a2a_refuses_unauthorised_unshared_and_foreign_tasks(env):
    from a2a.types.a2a_pb2 import GetTaskRequest
    from a2a.utils.errors import TaskNotFoundError
    aid, url = env["agent"], f"{B}/a2a/{env['agent']}"
    body = {"jsonrpc": "2.0", "id": 1, "method": "message/send", "params": {"message": {"kind": "message", "role": "user", "messageId": "m", "parts": [{"kind": "text", "text": "x"}]}}}
    assert httpx.post(url, json=body).status_code == 401                                         # no credentials
    assert httpx.post(url, json=body, headers={"X-Api-Key": "aos_nope"}).status_code == 401
    assert httpx.post(url, json=body, headers={"X-Api-Key": env["rkey"]}).status_code == 403     # read-only key cannot POST
    async def mk():
        async with _a2a(url, H(env), False) as cl: return (await _task(cl, f"echo-user mine {SUF}")).id
    tid = asyncio.run(mk())
    # a viewer with no share on a workspace agent / another editor: allowed to see the agent, but never the owner's task
    assert httpx.post(url, json=body, headers=H(env, "vw"), timeout=60).status_code == 403
    async def foreign():
        async with _a2a(url, H(env, "oth"), False) as cl:
            with pytest.raises(TaskNotFoundError): await cl.get_task(GetTaskRequest(id=tid))
    asyncio.run(foreign())
    # private + unshared -> the card and the endpoint are hidden (404) for other users
    assert env["own"].post(f"/api/agents/{aid}/shares", json={"visibility": "private"}).status_code == 200
    try:
        assert httpx.get(f"{url}/.well-known/agent-card.json", headers=H(env, "oth")).status_code == 404
        assert httpx.post(url, json=body, headers=H(env, "oth")).status_code == 404
        assert not any(c["name"].startswith("Trợ lý chuẩn") for c in httpx.get(f"{B}/a2a", headers=H(env, "oth")).json())
        assert httpx.get(f"{url}/.well-known/agent-card.json", headers=H(env)).status_code == 200   # the owner still can
    finally: env["own"].post(f"/api/agents/{aid}/shares", json={"visibility": "workspace"})
