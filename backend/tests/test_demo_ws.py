"""Demo workspace + Gemini-only (21/10/2026). A demo user is moved into its own workspace (POST /api/admin/demo-workspace): it sees none
of the owner's threads/traces/runs/usage, chats with Gemini-only clones of the chosen agents, OpenAI is refused (403 on a per-run
model, DemoOpenAIBlocked inside the gateway), cap $2/day. Local server :8020 (fake LLM, $0)."""
import asyncio, os, re, sys, uuid
import httpx, psycopg, pytest
B = "http://127.0.0.1:8020"; ADMIN = ("moza", "phase0-local")
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SUF = uuid.uuid4().hex[:6]; DU = ("demows" + SUF, "demo-ws-pass-" + SUF)

@pytest.fixture(scope="module")
def a():
    with httpx.Client(base_url=B, auth=ADMIN, timeout=90) as c: yield c

@pytest.fixture(scope="module")
def prov(a):
    # owner data that must stay invisible: a thread + run in workspace 1
    t = a.post("/api/v1/threads", json={"metadata": {"source": "chat"}}).json()["thread_id"]
    assert a.post(f"/api/v1/threads/{t}/runs/wait", json={"assistant_id": "agent-2", "input": {"messages": [{"type": "human", "content": "owner secret " + SUF}]}}).status_code == 200
    r = a.post("/api/admin/users", json={"username": DU[0], "password": DU[1], "role": "viewer", "demo": True, "daily_cap_usd": 0.05, "demo_agents": [2]})
    assert r.status_code == 200, r.text; uid = r.json()["id"]
    p = a.post("/api/admin/demo-workspace", json={"user_id": uid, "name": "Demo test " + SUF, "source_agents": [2], "daily_cap_usd": 2.0})
    assert p.status_code == 200, p.text
    yield {"uid": uid, "owner_thread": t, **p.json()}
    with psycopg.connect(PG) as c:
        c.execute("UPDATE users SET disabled=true WHERE id=%s", (uid,))
        c.execute("DELETE FROM lg_threads WHERE workspace_id=%s", (p.json()["workspace_id"],))

@pytest.fixture(scope="module")
def d(prov):
    with httpx.Client(base_url=B, auth=DU, timeout=90) as c: yield c

def test_provision_is_idempotent_and_owner_only(a, d, prov):
    again = a.post("/api/admin/demo-workspace", json={"user_id": prov["uid"], "name": "Demo test " + SUF, "source_agents": [2], "daily_cap_usd": 2.0}).json()
    assert again["workspace_id"] == prov["workspace_id"] and again["agents"] == prov["agents"] and again["cloned_now"] == []
    assert a.post("/api/admin/demo-workspace", json={"user_id": 1, "source_agents": [2]}).status_code == 400   # owner can never be moved
    assert d.post("/api/admin/demo-workspace", json={"user_id": prov["uid"], "source_agents": [2]}).status_code == 403

def test_demo_lives_in_its_own_workspace(d, prov):
    m = d.get("/api/auth/me").json()
    assert m["ws"] == prov["workspace_id"] and m["role"] == "viewer" and m["demo"] and m["daily_cap_usd"] == 2.0
    assert [w["id"] for w in m["workspaces"]] == [prov["workspace_id"]]
    assert d.get("/api/auth/me", headers={"x-workspace-id": "1"}).status_code == 401   # no way back into the owner's workspace

def test_sees_only_the_clones_and_no_owner_data(a, d, prov):
    clone = prov["agents"]["2"]
    assert {x["agent_id"] for x in d.post("/api/v1/assistants/search", json={"limit": 50}).json()} == {clone}
    assert prov["owner_thread"] not in d.post("/api/v1/threads/search", json={"limit": 200}).text
    assert d.get(f"/api/v1/threads/{prov['owner_thread']}").status_code == 404
    owner_ids = set(UUID.findall(a.get("/api/runs?limit=50").text)) | {prov["owner_thread"]}
    for p in ("/api/runs?limit=50", "/api/obs/projects", "/api/agents", "/api/kb/docs", "/api/approvals", "/api/ui/feedback_recent", "/api/gateway", "/api/obs/outbox"):
        r = d.get(p); assert r.status_code in (200, 404), p
        assert not (set(UUID.findall(r.text)) & owner_ids), p
    g = d.get("/api/gateway").json()
    assert {x["agent_id"] for x in g["recent"] + g["by_agent"]} <= {clone}
    assert d.get("/api/obs/outbox").json() == []

def test_chat_is_gemini_only(d, prov):
    g = f"agent-{prov['agents']['2']}"
    with psycopg.connect(PG) as c: mx = c.execute("SELECT coalesce(max(id),0) FROM gateway_usage").fetchone()[0]
    t = d.post("/api/v1/threads", json={"metadata": {"source": "chat"}}).json()["thread_id"]
    assert d.post(f"/api/v1/threads/{t}/runs/wait", json={"assistant_id": g, "input": {"messages": [{"type": "human", "content": "Xin chào"}]}}).status_code == 200
    with psycopg.connect(PG) as c:
        ms = {r[0] for r in c.execute("SELECT model FROM gateway_usage WHERE id>%s AND agent_id=%s", (mx, prov["agents"]["2"]))}
    assert ms and all(m.startswith("gemini") for m in ms), ms
    assert d.post(f"/api/v1/threads/{t}/runs/wait", json={"assistant_id": g, "input": {"messages": [{"type": "human", "content": "hi"}]},
                                                         "config": {"configurable": {"model": "gpt-5.4-nano"}}}).status_code == 403
    cfg = d.get("/api/chat/config", params={"agent_id": g}).json()
    assert cfg["models"] and all(m["provider"] == "gemini" for m in cfg["models"]) and cfg["default_model"] == "gemini-3.8-flash"
    with psycopg.connect(PG) as c:
        conf = c.execute("SELECT v.config FROM agents a JOIN agent_versions v ON v.agent_id=a.id AND v.version=a.current_version WHERE a.id=%s", (prov["agents"]["2"],)).fetchone()[0]
    assert conf["model"] == "gemini-3.8-flash" and all(m.startswith("gemini") for m in conf["fallback_models"])

def test_gateway_fails_closed_for_demo():
    sys.path[:0] = [os.path.join(os.path.dirname(__file__), "..", "app"), os.path.join(os.path.dirname(__file__), "..", "overlay")]
    os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls"); os.environ.setdefault("PG_URI", PG); os.environ.setdefault("OPENAI_API_KEY", "sk-test-never-used")
    import deepagent_api, admin, gateway as gw  # noqa: F401
    from langchain_core.messages import HumanMessage
    llm = gw._model("gpt-5.4-nano", None)
    tok = admin.USER.set({"id": 9, "role": "viewer", "demo": True, "ws": 1})
    try:
        assert gw.gemini_only()
        with pytest.raises(gw.DemoOpenAIBlocked): asyncio.run(llm.ainvoke([HumanMessage("hi")]))   # raised before any network call
        with pytest.raises(gw.DemoOpenAIBlocked): llm.invoke([HumanMessage("hi")])
        assert all(gw.provider(m) == "gemini" for m in gw._usable(["gpt-5.4-nano", "gpt-5-mini"]))
        with pytest.raises(gw.DemoOpenAIBlocked): asyncio.run(gw.generate(None, "hi", ["gpt-5.4-nano"]) if False else _openai_generate(gw))
    finally:
        admin.USER.reset(tok)
    tok = admin.USER.set({"id": 1, "role": "admin", "ws": 1})
    try: assert not gw.gemini_only() and gw._usable(["gpt-5.4-nano"])[0] in ("gpt-5.4-nano", gw.LAST_RESORT)   # owner unchanged
    finally: admin.USER.reset(tok)
    gw.DEMO_WS.add(424242); t2 = gw.RUN_WS.set(424242)
    try: assert gw.gemini_only()                                            # background run inside a demo workspace, no user
    finally: gw.RUN_WS.reset(t2); gw.DEMO_WS.discard(424242)

async def _openai_generate(gw):
    # an OpenAI-only chain for a demo caller is rewritten to Gemini; the explicit guard on the OpenAI branch is the backstop
    gw.COOLDOWN.update({m: 9e18 for m in [gw.DEMO_MODEL, *gw.DEMO_FALLBACKS, gw.LAST_RESORT]})
    try:
        orig = gw._usable; gw._usable = lambda chain: ["gpt-5.4-nano"]
        try: await gw.generate(None, "hi", ["gpt-5.4-nano"])
        finally: gw._usable = orig
    finally:
        gw.COOLDOWN.clear()
