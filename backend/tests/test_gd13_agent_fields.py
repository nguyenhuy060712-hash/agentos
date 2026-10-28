"""GĐ13.7 Agent fields `response_format` (structured output) + `middleware` (allow-listed LangChain middleware: limits, retry, summarization, context editing, PII, ModelFallback).
Fake LLM / fake providers only ($0); ONE recorded real Gemini run is replayed (tests/data/gd13_13_7_structured.json; recording needs AOS_GD13_7_RECORD=<600 key file>).
Negative cases first: hostile schema (size, depth, $ref cycle, remote ref), middleware outside the allow-list (shell, file search), unknown / out-of-range params, demo workspace,
another workspace, a viewer. Parts: 1 pure validators (no DB), 2 pydantic -> HTTP 422, 3 the REAL build_agent (Postgres), 4 the recorded Gemini replay."""
import asyncio, json, os, sys, uuid
from pathlib import Path
import psycopg, pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

HERE = Path(__file__).resolve().parent
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE / ".." / "app")); sys.path.insert(0, str(HERE / ".." / "overlay"))
import deepagent_api as A  # noqa: E402,I001  (first app import: modules import each other circularly)
import admin, gateway, migrations, pii, agent_fields as AF  # noqa: E402
from fakes.fake_providers import FakeProviders  # noqa: E402

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos"); os.environ.setdefault("PG_URI", PG)
SUF = uuid.uuid4().hex[:6]
RF = {"title": "Order", "type": "object", "properties": {"customer": {"type": "string"}, "total": {"type": "number"}, "paid": {"type": "boolean"}}, "required": ["customer", "total"]}
BAD_NAMES = ["ShellToolMiddleware", "Shell", "FilesystemFileSearchMiddleware", "FileSearch", "LLMToolEmulator", "ToolEmulator", "HumanInTheLoop", "TodoList", "os.system", "", "ModelCall Limit",
             "../ModelRetry", "__import__('os')", "ModelRetryMiddleware2"]

# ------------------------------------------------------------------ 1. response_format validator (pure)
def test_response_format_valid_is_kept_and_title_made_tool_safe():
    out = AF.check_response_format({**RF, "title": "Đơn hàng / order #1"}); assert out["properties"] == RF["properties"] and out["title"] == "n_h_ng_order_1"
    assert AF.check_response_format(RF)["title"] == "Order" and AF.check_response_format({k: v for k, v in RF.items() if k != "title"})["title"] == "structured_output"
    assert AF.check_response_format(None) is None
    with_defs = {"type": "object", "properties": {"a": {"$ref": "#/$defs/Line"}}, "$defs": {"Line": {"type": "object", "properties": {"sku": {"type": "string"}}}}}
    assert AF.check_response_format(with_defs)["$defs"]["Line"]["type"] == "object"          # a local reference without a cycle is fine

@pytest.mark.parametrize("bad", [
    "x", [], 5, {}, {"type": "array", "items": {"type": "string"}}, {"type": "string"}, {"properties": {"a": {"type": "string"}}},          # not an object schema
    {"type": "object", "properties": {"a": {"type": "no-such-type"}}},                                                                      # not a valid JSON Schema
    {"type": "object", "properties": {"a": {"type": "string", "pattern": 5}}},
])
def test_response_format_shape_refused(bad):
    with pytest.raises(ValueError): AF.check_response_format(bad)

def test_response_format_too_large_and_too_deep_refused():
    big = {"type": "object", "properties": {f"p{i}": {"type": "string", "description": "x" * 200} for i in range(120)}}
    assert len(json.dumps(big)) > AF.MAX_SCHEMA_BYTES
    with pytest.raises(ValueError, match="too large"): AF.check_response_format(big)
    ok = {"type": "object", "properties": {"p": {"type": "string", "description": "x" * 19000}}}
    assert len(json.dumps(ok).encode()) < AF.MAX_SCHEMA_BYTES and AF.check_response_format(ok)      # just under the limit passes
    deep = {"type": "string"}
    for _ in range(60): deep = {"type": "object", "properties": {"n": deep}}
    with pytest.raises(ValueError, match="deep"): AF.check_response_format(deep)
    hostile = {"type": "string"}
    for _ in range(3000): hostile = {"type": "object", "properties": {"n": hostile}}              # a stack-bomb body must be a clean refusal, not a RecursionError
    with pytest.raises(ValueError): AF.check_response_format(hostile)

@pytest.mark.parametrize("schema", [
    {"type": "object", "properties": {"a": {"$ref": "#/$defs/A"}}, "$defs": {"A": {"type": "object", "properties": {"b": {"$ref": "#/$defs/B"}}}, "B": {"type": "object", "properties": {"a": {"$ref": "#/$defs/A"}}}}},   # A -> B -> A
    {"type": "object", "properties": {"a": {"$ref": "#/$defs/A"}}, "$defs": {"A": {"type": "object", "properties": {"self": {"$ref": "#/$defs/A"}}}}},                                                     # A -> A (tree node)
    {"type": "object", "properties": {"child": {"$ref": "#"}}},                                                                                                                                              # root -> root
    {"type": "object", "properties": {"a": {"$ref": "#/definitions/D"}}, "definitions": {"D": {"type": "array", "items": {"$ref": "#/definitions/D"}}}},
])
def test_response_format_reference_cycle_refused(schema):
    with pytest.raises(ValueError): AF.check_response_format(schema)

@pytest.mark.parametrize("ref", ["http://169.254.169.254/latest/meta-data", "https://evil.example/schema.json", "file:///etc/passwd", "other.json#/x", "#/properties/a", "#/$defs/Missing", "//x"])
def test_response_format_remote_or_unknown_reference_refused(ref):
    with pytest.raises(ValueError): AF.check_response_format({"type": "object", "properties": {"a": {"$ref": ref}}, "$defs": {"Line": {"type": "string"}}})

def test_response_format_dynamic_reference_refused():
    for k in ("$dynamicRef", "$recursiveRef"):
        with pytest.raises(ValueError): AF.check_response_format({"type": "object", "properties": {"a": {k: "#meta"}}})

def test_validate_structured_checks_the_value_against_the_schema():
    assert AF.validate_structured(RF, {"customer": "A", "total": 3.5}) is None
    assert "total" in AF.validate_structured(RF, {"customer": "A"})
    assert "total" in AF.validate_structured(RF, {"customer": "A", "total": "many"}) and AF.validate_structured(None, {"x": 1}) is None

# ------------------------------------------------------------------ 1b. middleware allow-list (pure)
@pytest.mark.parametrize("name", BAD_NAMES)
def test_middleware_outside_the_allow_list_refused(name):
    with pytest.raises(ValueError, match="not allowed"): AF.check_middleware([{"name": name, "params": {}}])

def test_shell_and_file_search_are_not_in_the_allow_list_at_all():
    assert not any(k.lower().startswith(("shell", "file", "tool_emulator", "llmtool", "humanin")) for k in AF.ALLOWED)
    assert set(AF.ALLOWED) == {"ModelCallLimit", "ToolCallLimit", "ModelRetry", "ToolRetry", "Summarization", "ContextEditing", "PII", "ModelFallback"}

@pytest.mark.parametrize("item", [
    {"name": "ModelCallLimit", "params": {"run_limit": 5, "shell": "rm -rf /"}},        # unknown key
    {"name": "ModelCallLimit", "params": {"run_limit": 0}}, {"name": "ModelCallLimit", "params": {"run_limit": 99999}}, {"name": "ModelCallLimit", "params": {"run_limit": True}},
    {"name": "ModelCallLimit", "params": {"run_limit": 2.5}}, {"name": "ModelCallLimit", "params": {"run_limit": "5"}}, {"name": "ModelCallLimit", "params": {}},
    {"name": "ModelCallLimit", "params": {"run_limit": 5, "exit_behavior": "continue"}},  # not valid for model limit
    {"name": "ToolCallLimit", "params": {"tool_name": "x" * 500, "run_limit": 3}}, {"name": "ToolCallLimit", "params": {"exit_behavior": "end"}},
    {"name": "ModelRetry", "params": {"max_retries": 99}}, {"name": "ModelRetry", "params": {"max_retries": -1}}, {"name": "ModelRetry", "params": {"backoff_factor": 1000}},
    {"name": "ModelRetry", "params": {"retry_on": "everything"}}, {"name": "ModelRetry", "params": {"jitter": "yes"}}, {"name": "ModelRetry", "params": [1, 2]},
    {"name": "ToolRetry", "params": {"tools": []}}, {"name": "ToolRetry", "params": {"tools": ["a"] * 30}}, {"name": "ToolRetry", "params": {"tools": [1]}},
    {"name": "Summarization", "params": {}}, {"name": "Summarization", "params": {"trigger_tokens": 5}}, {"name": "Summarization", "params": {"trigger_tokens": 5000, "model": "gpt-x"}},
    {"name": "ContextEditing", "params": {"trigger_tokens": 10}}, {"name": "ContextEditing", "params": {"edits": [{"type": "x"}]}},
    {"name": "PII", "params": {"strategy": "leak"}}, {"name": "PII", "params": {"entities": ["NOT_AN_ENTITY"]}}, {"name": "PII", "params": {"entities": []}},
    {"name": "PII", "params": {"detector": "r'.*'"}}, {"name": "ModelFallback", "params": {}}, {"name": "ModelFallback", "params": {"models": ["a b; rm"]}},
    {"name": "ModelFallback", "params": {"models": ["m"] * 9}}, {"name": "ModelCallLimit", "params": {"run_limit": 5}, "extra": 1},
])
def test_middleware_bad_params_refused(item):
    with pytest.raises(ValueError): AF.check_middleware([item])

def test_middleware_duplicates_and_count_refused():
    with pytest.raises(ValueError, match="twice"): AF.check_middleware([{"name": "ModelRetry"}, {"name": "ModelRetryMiddleware"}])
    with pytest.raises(ValueError, match="at most"): AF.check_middleware([{"name": "ModelRetry"}] * 13)
    with pytest.raises(ValueError): AF.check_middleware("ModelRetry")

def test_middleware_valid_is_normalised():
    out = AF.check_middleware([{"name": "modelcalllimitmiddleware", "params": {"run_limit": 5, "exit_behavior": "error"}}, {"name": "PII", "params": {"entities": ["VN_PHONE", "VN_PHONE"], "strategy": "mask"}},
                               {"name": "ToolRetry", "params": {"max_retries": 3, "backoff_factor": 1, "tools": ["fetch_url"]}}, {"name": "ModelRetry"}, {"name": "ContextEditing", "params": {"keep": 0}},
                               {"name": "Summarization", "params": {"trigger_tokens": 8000, "keep_messages": 6}}, {"name": "ToolCallLimit", "params": {"tool_name": "web_search", "thread_limit": 20}}])
    assert [m["name"] for m in out] == ["ModelCallLimit", "PII", "ToolRetry", "ModelRetry", "ContextEditing", "Summarization", "ToolCallLimit"]
    assert out[1]["params"] == {"entities": ["VN_PHONE"], "strategy": "mask"} and out[2]["params"]["backoff_factor"] == 1.0 and out[0]["params"] == {"run_limit": 5, "exit_behavior": "error"}

# ------------------------------------------------------------------ 2. AgentCfg: model_validator + HTTP 422
def test_agentcfg_refuses_and_maps():
    with pytest.raises(Exception, match="not allowed"): A.AgentCfg(name="x", middleware=[{"name": "ShellToolMiddleware", "params": {}}])
    with pytest.raises(Exception): A.AgentCfg(name="x", middleware=[{"name": "ModelRetry", "params": {}, "evil": 1}])
    with pytest.raises(Exception): A.AgentCfg(name="x", response_format={"type": "object", "properties": {"c": {"$ref": "#"}}})
    c = A.AgentCfg(name="x", fallback_models=["old"], middleware=[{"name": "ModelFallback", "params": {"models": ["gpt-5.4-nano", "gemini-3.6-flash"]}}, {"name": "ModelRetry", "params": {"max_retries": 1}}])
    assert c.fallback_models == ["gpt-5.4-nano", "gemini-3.6-flash"] and [m.name for m in c.middleware] == ["ModelRetry"]    # ModelFallback = the gateway list, not a second middleware
    again = A.AgentCfg(**c.model_dump(exclude={"note"})); assert again.model_dump() == c.model_dump()                       # export -> import round trip keeps both fields
    d = A.AgentCfg(name="y", response_format=RF, middleware=[{"name": "PII"}]); r = A.AgentCfg(**json.loads(json.dumps(d.model_dump())))
    assert r.response_format["properties"]["total"] == {"type": "number"} and r.middleware[0].name == "PII"
    assert "response_format" in A.OVERRIDABLE and "middleware" in A.OVERRIDABLE

def test_http_body_with_a_forbidden_middleware_is_422():
    app = FastAPI()
    @app.post("/api/agents")
    async def new(cfg: A.AgentCfg): return {"ok": True, "mw": [m.name for m in cfg.middleware]}
    cl = TestClient(app)
    assert cl.post("/api/agents", json={"name": "a", "middleware": [{"name": "ModelRetry", "params": {"max_retries": 2}}], "response_format": RF}).json() == {"ok": True, "mw": ["ModelRetry"]}
    for body in ({"name": "a", "middleware": [{"name": "ShellToolMiddleware", "params": {}}]}, {"name": "a", "middleware": [{"name": "FilesystemFileSearchMiddleware"}]},
                 {"name": "a", "middleware": [{"name": "ModelRetry", "params": {"max_retries": 500}}]}, {"name": "a", "middleware": [{"name": "ModelRetry", "params": {"zzz": 1}}]},
                 {"name": "a", "response_format": {"type": "object", "properties": {"a": {"$ref": "http://127.0.0.1/x"}}}}, {"name": "a", "response_format": "{}"}):
        assert cl.post("/api/agents", json=body).status_code == 422, body

def test_demo_caller_cannot_set_the_fields():
    cfg = A.AgentCfg(name="x", response_format=RF)
    for user in ({"id": 9, "role": "editor", "ws": 5, "demo": True}, {"id": 9, "role": "admin", "ws": 5, "demo": True}):
        t = admin.USER.set(user)
        try:
            with pytest.raises(HTTPException) as e: AF.refuse_demo(cfg)
            assert e.value.status_code == 403
        finally: admin.USER.reset(t)
    gateway.DEMO_WS.add(990013)                                                      # a demo WORKSPACE (user not flagged): refused as well
    t = admin.USER.set({"id": 9, "role": "admin", "ws": 990013})
    try:
        with pytest.raises(HTTPException): AF.refuse_demo(A.AgentCfg(name="x", middleware=[{"name": "ModelRetry"}]))
        AF.refuse_demo(A.AgentCfg(name="x"))                                         # an agent without the fields is saved as always
    finally: admin.USER.reset(t); gateway.DEMO_WS.discard(990013)
    t = admin.USER.set({"id": 9, "role": "admin", "ws": 5})
    try: AF.refuse_demo(cfg)                                                         # control: a normal workspace may
    finally: admin.USER.reset(t)

# ------------------------------------------------------------------ 3. the REAL build_agent (Postgres) + fake model / fake providers
def harness(fn, providers=None):
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as c: await migrations.run_pending(c)
        old = (A.S.get("pool"), A.S.get("rls"), A.S.get("cp"))
        async with AsyncConnectionPool(PG, min_size=1, max_size=4, open=False, kwargs={"autocommit": True, "row_factory": dict_row}) as pool:
            await pool.open(); A.S["pool"] = pool; A.S["rls"] = admin.rls_ctx; A.S["cp"] = InMemorySaver(); A.S["agents"].clear()
            await admin.rls_grants()
            for t in ("gw_providers", "gw_fallback_chains"): await admin._policy(t, "aos_ws() IS NULL OR workspace_id = aos_ws()")
            W = {"tag": SUF}
            for s in "ab": W[s] = (await A.q("INSERT INTO workspaces(name) VALUES(%s) RETURNING id", (f"p137-{s}-{SUF}",), one=True))["id"]
            try: return await fn(W)
            finally:
                for s in "ab":
                    await A.q("DELETE FROM agents WHERE workspace_id=%s", (W[s],))
                    for sql in ("DELETE FROM gateway_usage WHERE workspace_id=%s", "DELETE FROM gw_providers WHERE workspace_id=%s", "DELETE FROM ws_secrets WHERE workspace_id=%s", "DELETE FROM workspaces WHERE id=%s"):
                        try: await A.q(sql, (W[s],))
                        except Exception as e: print("cleanup:", str(e)[:80])   # noqa: BLE001
                A.S["agents"].clear(); A.S["pool"], A.S["rls"], A.S["cp"] = old
    return asyncio.run(go())

def user(ws, role="admin", demo=False): return {"id": 987137, "username": "t137", "role": role, "ws": ws, "auth": "session", **({"demo": True} if demo else {})}

class as_user:
    def __init__(self, u): self.u = u
    def __enter__(self): self.t = admin.USER.set(self.u)
    def __exit__(self, *a): admin.USER.reset(self.t)

@pytest.fixture
def fake_models(monkeypatch):
    """gateway._model -> the repo's scripted FakeAgentModel (structured output through bind_tools); no provider key / network."""
    from fake_llm import FakeAgentModel
    async def noprep(ref): return None
    monkeypatch.setattr(gateway, "prepare", noprep); monkeypatch.setattr(gateway, "_model", lambda name, temperature=None, params=None: FakeAgentModel(model_name=f"fake:{name}", delay=0))

def invoke(ag, text, tid=None):
    return ag.ainvoke({"messages": [{"role": "user", "content": text}]}, {"configurable": {"thread_id": "t137-" + (tid or uuid.uuid4().hex[:8])}})

def test_run_returns_schema_valid_structured_response(fake_models):
    async def go(W):
        with as_user(user(W["a"])):
            aid, v = await A.create_agent(A.AgentCfg(name="so " + SUF, tools=[], planning=False, response_format=RF))
            ag, cfg = await A.build_agent(aid, v)
            assert cfg.response_format["title"] == "Order"
            r = await invoke(ag, "make an order")
            sr = r["structured_response"]; assert AF.validate_structured(RF, sr) is None and set(sr) >= {"customer", "total"}, sr
            r2 = await invoke(ag, 'order [json] {"customer": "An", "total": 12.5, "paid": true}'); assert r2["structured_response"] == {"customer": "An", "total": 12.5, "paid": True}
            # the run wrapper (what the Agent Server stores as the run output) carries it, and flags a value that breaks the schema
            evs = [e async for e in A.run_agent(aid, v, "t137-run" + SUF, 'order [json] {"customer": "Bo", "total": 3}')]
            done = evs[-1]; assert done["type"] == "done" and done["structured_response"] == {"customer": "Bo", "total": 3} and "structured_error" not in done, done
            assert done["reply"].startswith("```json") and '"customer": "Bo"' in done["reply"], done["reply"]       # the chat shows the JSON block (the model answered by calling the structured tool: no text of its own)
            assert [m for m in (await ag.aget_state({"configurable": {"thread_id": "a%d:t137-run%s" % (aid, SUF)}})).values["messages"] if m.type == "ai"][-1].content == done["reply"]
            bad = [e async for e in A.run_agent(aid, v, "t137-run2" + SUF, 'order [json] {"customer": "Bo"}')][-1]
            assert bad["structured_response"] == {"customer": "Bo"} and "total" in bad["structured_error"], bad     # LangChain does not validate raw schemas: we flag it
            aid2, v2 = await A.create_agent(A.AgentCfg(name="plain " + SUF, tools=[], planning=False))
            ev2 = [e async for e in A.run_agent(aid2, v2, "t137-run3" + SUF, "hello")][-1]; assert "structured_response" not in ev2     # no field, no change
    harness(go)

def test_pii_middleware_redacts_the_phone_in_the_stored_state(fake_models, monkeypatch):
    async def go(W):
        async def nocfg(ws): return None
        monkeypatch.setattr(pii, "workspace_cfg", nocfg)
        with as_user(user(W["a"])):
            aid, v = await A.create_agent(A.AgentCfg(name="pii " + SUF, tools=[], planning=False, middleware=[{"name": "PII", "params": {"entities": ["VN_PHONE"]}}]))
            ag, _ = await A.build_agent(aid, v)
            r = await invoke(ag, "Gọi cho tôi theo số điện thoại 0912345678 nhé", "p1")
            human = r["messages"][0].content; assert "0912345678" not in human and "REDACTED" in human.upper(), human
            assert "0912345678" not in json.dumps([m.content for m in r["messages"]])
            aid2, v2 = await A.create_agent(A.AgentCfg(name="nopii " + SUF, tools=[], planning=False))      # control: no middleware = the state keeps the number
            ag2, _ = await A.build_agent(aid2, v2); r2 = await invoke(ag2, "Gọi cho tôi theo số điện thoại 0912345678 nhé", "p2"); assert "0912345678" in r2["messages"][0].content
    harness(go)

def test_pii_middleware_skips_entities_the_gateway_already_masks_and_rebuilds_when_that_changes(fake_models, monkeypatch):
    async def go(W):
        state = {"cfg": None}
        async def wscfg(ws): return state["cfg"]
        monkeypatch.setattr(pii, "workspace_cfg", wscfg)
        gw_on = pii.normalize_cfg({"enabled": True, "entities": ["VN_PHONE"], "apply_gateway": True, "mode": "pseudonym"})
        assert gw_on and gw_on["gateway"]
        with as_user(user(W["a"])):
            aid, v = await A.create_agent(A.AgentCfg(name="pii2 " + SUF, tools=[], planning=False, middleware=[{"name": "PII", "params": {"entities": ["VN_PHONE", "EMAIL_ADDRESS"]}}]))
            state["cfg"] = gw_on
            ag1, _ = await A.build_agent(aid, v)
            r = await invoke(ag1, "số 0912345678 và mail a@b.vn", "s1")
            assert "0912345678" in r["messages"][0].content and "a@b.vn" not in r["messages"][0].content     # the phone is the gateway's job (no double masking); the e-mail is ours
            state["cfg"] = None                                                                               # the workspace switched the gateway masking off
            ag2, _ = await A.build_agent(aid, v); assert ag2 is not ag1                                       # cached build was for the old setting -> rebuilt
            r = await invoke(ag2, "số 0912345678 và mail a@b.vn", "s2"); assert "0912345678" not in r["messages"][0].content
            ag3, _ = await A.build_agent(aid, v); assert ag3 is ag2                                           # stable again: cache is used
    harness(go)

def test_cache_is_not_shared_between_different_middleware(fake_models):
    async def go(W):
        with as_user(user(W["a"])):
            a1, v1 = await A.create_agent(A.AgentCfg(name="m1 " + SUF, tools=[], planning=False, middleware=[{"name": "ModelCallLimit", "params": {"run_limit": 3}}]))
            a2, v2 = await A.create_agent(A.AgentCfg(name="m2 " + SUF, tools=[], planning=False, middleware=[{"name": "ModelCallLimit", "params": {"run_limit": 7}}]))
            a3, v3 = await A.create_agent(A.AgentCfg(name="m3 " + SUF, tools=[], planning=False))
            g1, c1 = await A.build_agent(a1, v1); g2, c2 = await A.build_agent(a2, v2); g3, c3 = await A.build_agent(a3, v3)
            assert len({id(g1), id(g2), id(g3)}) == 3 and c1.middleware[0].params["run_limit"] == 3 and c2.middleware[0].params["run_limit"] == 7 and not c3.middleware
            assert (await A.build_agent(a1, v1))[0] is g1 and (await A.build_agent(a2, v2))[0] is g2                       # each reused by its own agent only
            # per-run override on the same agent version: its own key, the saved build untouched
            ov = [{"name": "ModelCallLimit", "params": {"run_limit": 9}}]
            go_, co = await A.build_agent(a3, v3, {"middleware": ov}); assert go_ is not g3 and co.middleware[0].params["run_limit"] == 9
            assert (await A.build_agent(a3, v3, {"middleware": ov}))[0] is go_ and (await A.build_agent(a3, v3))[0] is g3
            rf, cr = await A.build_agent(a3, v3, {"response_format": RF}); assert rf is not g3 and cr.response_format["title"] == "Order"
            for bad in ([{"name": "ShellToolMiddleware", "params": {}}], [{"name": "ModelRetry", "params": {"max_retries": 100}}]):   # an override cannot smuggle a forbidden one in
                with pytest.raises(HTTPException) as e: await A.build_agent(a3, v3, {"middleware": bad})
                assert e.value.status_code == 400
            with pytest.raises(HTTPException): await A.build_agent(a3, v3, {"response_format": {"type": "object", "properties": {"x": {"$ref": "#"}}}})
    harness(go)

def test_limit_middleware_really_applies(fake_models):
    async def go(W):
        with as_user(user(W["a"])):
            aid, v = await A.create_agent(A.AgentCfg(name="lim " + SUF, tools=[], planning=False, middleware=[{"name": "ModelCallLimit", "params": {"thread_limit": 1, "exit_behavior": "error"}}]))
            ag, _ = await A.build_agent(aid, v); tid = "lim" + SUF
            await invoke(ag, "one", tid)
            with pytest.raises(Exception, match="(?i)limit"): await invoke(ag, "two", tid)                            # the second model call of the thread is refused
    harness(go)

def test_all_allowed_middleware_build_together(fake_models, monkeypatch):
    async def go(W):
        async def nocfg(ws): return None
        monkeypatch.setattr(pii, "workspace_cfg", nocfg)
        mw = [{"name": n, "params": p} for n, p in [("ModelCallLimit", {"run_limit": 20}), ("ToolCallLimit", {"run_limit": 20}), ("ModelRetry", {"max_retries": 1, "initial_delay": 0}),
                                                    ("ToolRetry", {"max_retries": 1}), ("Summarization", {"trigger_tokens": 50000, "keep_messages": 10}), ("ContextEditing", {"trigger_tokens": 50000, "keep": 2}),
                                                    ("PII", {"strategy": "mask"})]]
        with as_user(user(W["a"])):
            aid, v = await A.create_agent(A.AgentCfg(name="all " + SUF, tools=[], planning=False, middleware=mw, response_format=RF))
            ag, cfg = await A.build_agent(aid, v); assert len(cfg.middleware) == 7
            r = await invoke(ag, "order for 0912345678"); assert AF.validate_structured(RF, r["structured_response"]) is None
    harness(go)

def test_demo_workspace_never_gets_the_fields(fake_models):
    async def go(W):
        with as_user(user(W["a"])):
            aid, v = await A.create_agent(A.AgentCfg(name="dm " + SUF, tools=[], planning=False, response_format=RF, middleware=[{"name": "ModelRetry"}]))
        gateway.DEMO_WS.add(W["a"])
        try:
            A.S["agents"].clear()
            _, cfg = await A.build_agent(aid, v); assert cfg.response_format is None and cfg.middleware == []              # an agent copied into / switched to demo: stripped when built
            with as_user(user(W["a"], "editor", demo=True)):
                n0 = (await A.q("SELECT count(*) c FROM agents WHERE workspace_id=%s", (W["a"],), one=True))["c"]
                with pytest.raises(HTTPException) as e: await A.create_agent(A.AgentCfg(name="dm2 " + SUF, tools=[], middleware=[{"name": "ModelRetry"}]))
                assert e.value.status_code == 403
                with pytest.raises(HTTPException): await A.save_version(aid, A.AgentCfg(name="dm " + SUF, tools=[], response_format=RF))
                assert (await A.q("SELECT count(*) c FROM agents WHERE workspace_id=%s", (W["a"],), one=True))["c"] == n0    # no orphan agent, no new version
                assert (await A.q("SELECT current_version v FROM agents WHERE id=%s", (aid,), one=True))["v"] == v
                await A.create_agent(A.AgentCfg(name="dm3 " + SUF, tools=[]))                                              # control: a plain agent is still fine
        finally: gateway.DEMO_WS.discard(W["a"])
    harness(go)

def test_another_workspace_cannot_run_or_read_the_structured_agent(fake_models):
    async def go(W):
        with as_user(user(W["a"])):
            aid, v = await A.create_agent(A.AgentCfg(name="own " + SUF, tools=[], planning=False, response_format=RF, middleware=[{"name": "ModelRetry"}]))
        with as_user(user(W["b"])):
            with pytest.raises(HTTPException) as e: [x async for x in A.run_agent(aid, v, "t137-x" + SUF, "order")]
            assert e.value.status_code == 404                                                                               # no structured_response of A's agent for B
            assert not await A.q("SELECT 1 FROM agents WHERE id=%s", (aid,), one=True) and not await A.q("SELECT 1 FROM agent_versions WHERE agent_id=%s", (aid,), one=True)   # nor its config
    harness(go)

KEY = "sk-fake-provider-key-1234"; CLAUDE, GPT = "anthropic/claude-haiku-5-5", "gpt-5.4-nano"

def test_modelfallback_changes_the_chain_actually_used(monkeypatch):
    monkeypatch.setenv("AOS_EGRESS_ALLOW", "127.0.0.0/8,::1")
    import gw_providers as gp
    with FakeProviders(key=KEY, status=503) as cl, FakeProviders(key=KEY) as oa:
        async def go(W):
            a = W["a"]
            async def prov(kind, fp):
                with as_user(user(a)): await gp.create_provider(gp.ProviderIn(api_key=KEY, kind=kind, base_url=fp.url(kind)))
            await prov("anthropic", cl); await prov("openai", oa)
            calls = lambda fp: [c for c in fp.calls if c["path"] in ("/v1/messages", "/v1/chat/completions")]
            with as_user(user(a)):
                gateway.COOLDOWN.clear()
                aid, v = await A.create_agent(A.AgentCfg(name="fb " + SUF, model=CLAUDE, tools=[], planning=False, filesystem=False, fallback_models=[],
                                                         middleware=[{"name": "ModelFallback", "params": {"models": [GPT]}}]))
                ag, cfg = await A.build_agent(aid, v); assert cfg.fallback_models == [GPT] and cfg.middleware == []
                r = await invoke(ag, "hi"); assert r["messages"][-1].content == "pong" and calls(cl) and calls(oa)           # the 503 provider -> the next model of ModelFallback answered
                rows = await A.q("SELECT model, status FROM gateway_usage WHERE workspace_id=%s ORDER BY id", (a,))
                assert [(x["model"], x["status"]) for x in rows if x["model"] in (CLAUDE, GPT)][-2:] == [(CLAUDE, "error"), (GPT, "ok")]
                cl.calls.clear(); oa.calls.clear(); gateway.COOLDOWN.clear()
                aid2, v2 = await A.create_agent(A.AgentCfg(name="nofb " + SUF, model=CLAUDE, tools=[], planning=False, filesystem=False, fallback_models=[]))      # control: no ModelFallback
                ag2, _ = await A.build_agent(aid2, v2)
                with pytest.raises(Exception): await invoke(ag2, "hi")
                assert calls(cl) and not calls(oa)
                cl.calls.clear(); oa.calls.clear(); gateway.COOLDOWN.clear()
                ag3, c3 = await A.build_agent(aid2, v2, {"middleware": [{"name": "ModelFallback", "params": {"models": [GPT]}}]}); assert c3.fallback_models == [GPT] and ag3 is not ag2   # as a per-run override
                assert (await invoke(ag3, "hi"))["messages"][-1].content == "pong" and calls(oa)
        harness(go)

# ------------------------------------------------------------------ 4. one recorded real Gemini structured-output run, replayed
REC = HERE / "data" / "gd13_13_7_structured.json"

def test_recorded_gemini_structured_run_replay():
    """One real Gemini run (gemini-3.8-flash through build_agent -> create_deep_agent(response_format=...), ProviderStrategy) recorded with AOS_GD13_7_RECORD; replayed here at $0:
    the recorded schema passes our validator and the recorded structured_response satisfies it."""
    rec = json.loads(REC.read_text(encoding="utf-8"))
    assert rec["model"].startswith("gemini") and rec["cost_usd"] < 0.05
    rf = AF.check_response_format(rec["response_format"]); assert rf == rec["response_format"]
    assert AF.validate_structured(rf, rec["structured_response"]) is None
    assert rec["structured_response"]["needs_callback"] is True and rec["structured_response"]["priority"] == "high" and "Lan Anh" in rec["structured_response"]["customer"]   # what the real model understood
    assert rec["answer_text"] == ""            # recorded BEFORE StructuredReply: the real run's assistant text was empty -> the middleware now fills it with the JSON block (checked above with the fake model)
    assert "structured_error" not in rec

class Replay(GenericFakeChatModel):
    def bind_tools(self, tools, **kw): return self

def test_recorded_response_replays_through_build_agent(fake_models, monkeypatch):
    rec = json.loads(REC.read_text(encoding="utf-8"))
    async def go(W):
        monkeypatch.setattr(gateway, "_model", lambda *a, **k: Replay(messages=iter([AIMessage(content="", tool_calls=[{"name": rec["response_format"]["title"], "args": rec["structured_response"], "id": "c1"}])])))
        with as_user(user(W["a"])):
            aid, v = await A.create_agent(A.AgentCfg(name="rep " + SUF, tools=[], planning=False, response_format=rec["response_format"]))
            done = [e async for e in A.run_agent(aid, v, "t137-rep" + SUF, rec["prompt"])][-1]
            assert not done.get("error"), done["error"]
            assert done["structured_response"] == rec["structured_response"] and "structured_error" not in done and "```json" in done["reply"]
    harness(go)

@pytest.mark.skipif(not os.environ.get("AOS_GD13_7_RECORD"), reason="real Gemini recording (~$0.002); AOS_GD13_7_RECORD=<path of the 600 key file>")
def test_record_real_gemini_run():
    import time
    with open(os.path.expanduser(os.environ["AOS_GD13_7_RECORD"])) as f: os.environ["GOOGLE_API_KEY"] = f.read().strip()    # key stays in this process, never printed
    schema = {"title": "SupportTicket", "type": "object", "properties": {"customer": {"type": "string"}, "priority": {"type": "string", "enum": ["low", "normal", "high"]},
                                                                       "summary": {"type": "string"}, "needs_callback": {"type": "boolean"}}, "required": ["customer", "priority", "summary", "needs_callback"]}
    prompt = "Khách Lan Anh báo rằng máy in văn phòng hỏng từ sáng, cả phòng không in được hợp đồng, cần xử lý gấp trong hôm nay và nhờ gọi lại. Lập phiếu hỗ trợ."
    model = "gemini-3.8-flash"
    async def go(W):
        base = float((await A.q("SELECT coalesce(sum(cost_usd),0) c FROM gateway_usage WHERE NOT (coalesce(model,'') LIKE 'gpt-%%' OR coalesce(model,'') LIKE 'o3%%' OR coalesce(model,'') LIKE 'o4%%')", one=True))["c"])
        gateway.CAPS["gemini"] = base + 0.05; gateway.CAPS["openai"] = 0.0      # hard stop: this recording may spend at most $0.05 (the cap counts all earlier real runs too)
        with as_user(user(W["a"])):
            aid, v = await A.create_agent(A.AgentCfg(name="rec " + SUF, model=model, tools=[], planning=False, filesystem=False, fallback_models=[], temperature=0, response_format=schema,
                                                     system_prompt="Extract a support ticket from the message. Answer in the language of the message."))
            t0 = time.time(); evs = [e async for e in A.run_agent(aid, v, "t137-real" + SUF, prompt)]; done = evs[-1]
            assert done["type"] == "done" and not done.get("error") and done.get("structured_response") and "structured_error" not in done, {k: done.get(k) for k in ("error", "structured_error")}
            REC.write_text(json.dumps({"recorded": "2026-10-26", "model": model, "prompt": prompt, "response_format": AF.check_response_format(schema), "structured_response": done["structured_response"],
                                       "answer_text": done["reply"], "cost_usd": done["cost_usd"], "tokens": done["tokens"], "latency_s": round(time.time() - t0, 2)}, ensure_ascii=False, indent=1), encoding="utf-8")
    harness(go)

# ------------------------------------------------------------------ 5. over HTTP (a running server with this code: own port, AOS_GD13_BASE=http://127.0.0.1:8034; default :8020 once the shared server is restarted)
BASE = os.environ.get("AOS_GD13_BASE", "http://127.0.0.1:8020"); ADMIN = ("moza", "phase0-local"); PW = "phase0-gd137-local"

@pytest.fixture(scope="module")
def http():
    import httpx
    a = httpx.Client(base_url=BASE, auth=ADMIN, timeout=90)
    try: has = "MiddlewareCfg" in a.get("/openapi.json").text   # read-only probe (an old server would silently accept and STORE the unknown fields)
    except Exception: pytest.skip("no server")   # noqa: BLE001
    if not has: pytest.skip("the running server does not have GĐ13.7 yet (restart it / set AOS_GD13_BASE)")
    wb = a.post("/api/admin/workspaces", json={"name": f"p137-{SUF}"}).json()["id"]
    mk = lambda n, ws, role: (a.post("/api/admin/users", json={"username": n, "password": PW, "role": role, "workspace_id": ws}).raise_for_status(), httpx.Client(base_url=BASE, auth=(n, PW), timeout=90))[1]
    yield {"a": a, "ws": wb, "other": mk(f"p137o_{SUF}", wb, "editor"), "viewer": mk(f"p137v_{SUF}", 1, "viewer")}
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute("DELETE FROM agents WHERE name LIKE %s", (f"e2e137 {SUF}%",))
        for n in (f"p137o_{SUF}", f"p137v_{SUF}"):
            try: c.execute("DELETE FROM users WHERE username=%s", (n,))
            except Exception as e: print("cleanup:", str(e)[:80])   # noqa: BLE001
        try: c.execute("DELETE FROM workspaces WHERE id=%s", (wb,))
        except Exception as e: print("cleanup:", str(e)[:80])   # noqa: BLE001

def test_http_create_run_export_and_refusals(http):
    a, other, viewer = http["a"], http["other"], http["viewer"]
    body = {"name": "e2e137 " + SUF, "tools": [], "planning": False, "response_format": RF, "middleware": [{"name": "ModelCallLimit", "params": {"run_limit": 8}}]}
    assert viewer.post("/api/agents", json=body).status_code in (401, 403)                                          # a viewer cannot create / change agents at all
    with __import__("httpx").Client(base_url=BASE, timeout=30) as anon: assert anon.post("/api/agents", json=body).status_code in (401, 403)
    r = a.post("/api/agents", json=body); assert r.status_code == 200, r.text; aid = r.json()["id"]; g = f"agent-{aid}"
    cfg = a.get(f"/api/agents/{aid}/export").json()["config"]; assert cfg["response_format"]["title"] == "Order" and cfg["middleware"] == [{"name": "ModelCallLimit", "params": {"run_limit": 8}}]
    assert a.post("/api/agents/import", json={"config": cfg}).status_code == 200                                    # export -> import carries both fields
    for bad in ({"name": "b", "middleware": [{"name": "ShellToolMiddleware"}]}, {"name": "b", "middleware": [{"name": "ModelRetry", "params": {"max_retries": 99}}]},
                {"name": "b", "response_format": {"type": "object", "properties": {"x": {"$ref": "#"}}}}):
        assert a.post("/api/agents", json=bad).status_code == 422 and a.put(f"/api/agents/{aid}", json=bad).status_code == 422
        assert a.post("/api/agents/import", json={"config": bad}).status_code == 422
    assert len(a.get(f"/api/agents/{aid}").json()["versions"]) == 1                                               # the refused PUTs did not make a version
    tid = a.post("/api/v1/threads", json={"metadata": {"source": "pytest", "graph_id": g}}).json()["thread_id"]
    r = a.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": g, "input": {"messages": [{"type": "human", "content": 'order [json] {"customer": "An", "total": 5}'}]}}); assert r.status_code == 200, r.text
    out = a.get(f"/api/v1/threads/{tid}/runs").json()[0]["output"]
    assert out["structured_response"] == {"customer": "An", "total": 5} and "structured_error" not in out and out["reply"].startswith("```json"), out
    # another workspace: no run, no export, no config of this agent
    assert other.get(f"/api/agents/{aid}/export").status_code in (403, 404) and other.put(f"/api/agents/{aid}", json=body).status_code in (403, 404)
    assert other.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": g, "input": {"messages": [{"type": "human", "content": "order"}]}}).status_code in (403, 404)
    assert aid not in [x["id"] for x in other.get("/api/agents").json()]
