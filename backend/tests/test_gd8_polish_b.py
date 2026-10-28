"""GĐ8.P part B (24/10/2026): the 4 leftovers of the GĐ7.Z review.
a. Engine "Apply" double click -> exactly ONE Prompt Hub commit (atomic claim on issues.refs.applying).
b. Hidden assistant agent: one per workspace even when several requests / processes call ensure() at once (advisory lock + unique index).
c. HTTP 402 detail of a chat run is a machine-readable object {code: "spend_cap", message, scope, target, period, limit_usd, used_usd, feature}.
d. Deep-research stop message in the user's UI language (users.lang), not guessed from the question text.
Live tests hit the local server :8020 (fake LLM => $0); in-process gateway tests stub budget-related hooks like test_gd7r_gateway._light."""
import asyncio, os, sys, uuid
from concurrent.futures import ThreadPoolExecutor
import httpx, psycopg, pytest
from psycopg.types.json import Jsonb
from test_fixes import B, ADMIN, a  # noqa: F401  (fixtures)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
PW = "phase0-gd8p-local"; SUF = uuid.uuid4().hex[:6]

def pg(): return psycopg.connect(PG, autocommit=True)

def user(a, name, ws, role="editor"):
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": role, "workspace_id": ws}); assert r.status_code == 200, r.text
    return r.json()["id"], httpx.Client(base_url=B, auth=(name, PW), timeout=90)

@pytest.fixture(scope="module")
def env(a):
    w = a.post("/api/admin/workspaces", json={"name": f"pol-b-{SUF}"}).json()["id"]
    e = {"w": w}
    e["ed_id"], e["ed"] = user(a, f"polb_ed_{SUF}", w, "editor")
    e["ad_id"], e["ad"] = user(a, f"polb_ad_{SUF}", w, "admin")
    yield e
    e["ed"].close(); e["ad"].close()
    with pg() as c:
        c.execute("DELETE FROM issues WHERE workspace_id=%s", (w,))
        c.execute("DELETE FROM projects WHERE workspace_id=%s", (w,))
        c.execute("DELETE FROM prompts WHERE workspace_id=%s", (w,))
        c.execute("DELETE FROM lg_assistants WHERE workspace_id=%s", (w,))
        c.execute("DELETE FROM agents WHERE workspace_id=%s", (w,))
        c.execute("DELETE FROM users WHERE username LIKE %s", (f"polb\\_%\\_{SUF}",))
        c.execute("DELETE FROM workspaces WHERE id=%s", (w,))

# ---------------------------------------------------------------- a. Engine apply is idempotent
def _issue(e, sig):
    name = f"polb-p-{SUF}"
    if "prompt" not in e:
        r = e["ed"].post("/api/hub/commit", json={"name": name, "template": "Bạn là trợ lý.", "tag": "production", "message": "v1"}); assert r.status_code == 200, r.text
        r = e["ed"].post("/api/agents", json={"name": f"polb-agent-{SUF}", "prompt_ref": name + ":production", "tools": []}); assert r.status_code == 200, r.text
        e["prompt"], e["agent"] = name, r.json()["id"]
        with pg() as c:
            e["pid"] = c.execute("INSERT INTO projects(name, workspace_id) VALUES(%s,%s) RETURNING id", (f"polb-{SUF}", e["w"])).fetchone()[0]
    an = {"root_cause": "x", "evidence": [], "proposal": {"applicable": True, "new_text": "Bạn là trợ lý. Luôn hỏi lại khi thiếu dữ liệu.", "summary": "s"},
          "base": {"name": name, "version": 1, "inline": False}}
    with pg() as c:
        return c.execute("INSERT INTO issues(workspace_id, project_id, kind, signature, title, analysis, agent_id) VALUES(%s,%s,'error_cluster',%s,%s,%s,%s) RETURNING id",
                         (e["w"], e["pid"], sig, f"Issue {sig}", Jsonb(an), e["agent"])).fetchone()[0]

def _commits(e):
    with pg() as c:
        return c.execute("SELECT count(*) FROM prompt_versions v JOIN prompts p ON p.id=v.prompt_id WHERE p.name=%s AND p.workspace_id=%s", (e["prompt"], e["w"])).fetchone()[0]

def test_engine_apply_double_click_makes_one_commit(env):
    iid = _issue(env, "dbl"); before = _commits(env)
    clients = [httpx.Client(base_url=B, auth=(f"polb_ed_{SUF}", PW), timeout=90) for _ in range(4)]
    try:
        with ThreadPoolExecutor(4) as ex: codes = sorted(r.status_code for r in ex.map(lambda c: c.post(f"/api/engine/issues/{iid}/apply"), clients))
    finally:
        for c in clients: c.close()
    assert codes == [200, 409, 409, 409], codes
    assert _commits(env) == before + 1
    d = env["ed"].get(f"/api/engine/issues/{iid}").json()
    assert d["refs"]["commit"]["version"] == before + 1 and "applying" not in d["refs"]   # the claim is released and never shown
    assert env["ed"].post(f"/api/engine/issues/{iid}/apply").status_code == 409

def test_engine_apply_claim_blocks_and_expires(env):
    iid = _issue(env, "claim")
    with pg() as c: c.execute("UPDATE issues SET refs = jsonb_build_object('applying', now()) WHERE id=%s", (iid,))
    r = env["ed"].post(f"/api/engine/issues/{iid}/apply"); assert r.status_code == 409, r.text                     # another request is applying right now
    with pg() as c: c.execute("UPDATE issues SET refs = jsonb_build_object('applying', now() - interval '10 minutes') WHERE id=%s", (iid,))
    r = env["ed"].post(f"/api/engine/issues/{iid}/apply"); assert r.status_code == 200, r.text                     # a crashed claim expires

def test_engine_ui_disables_apply_while_pending():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "ui_engine.py"), encoding="utf-8").read()   # no app import (keeps RAM low)
    assert '<button id="ap"${BUSY?' in src

# ---------------------------------------------------------------- b. one hidden assistant per workspace
def test_assistant_ensure_concurrent_creates_one_agent(env):
    clients = [httpx.Client(base_url=B, auth=(f"polb_ad_{SUF}", PW), timeout=90) for _ in range(6)]
    try:
        with ThreadPoolExecutor(6) as ex: rs = list(ex.map(lambda c: c.get("/api/assistant"), clients))
    finally:
        for c in clients: c.close()
    assert all(r.status_code == 200 for r in rs), [r.text for r in rs if r.status_code != 200]
    assert len({r.json()["agent_id"] for r in rs}) == 1
    with pg() as c:
        n = c.execute("SELECT count(*) FROM agents WHERE hidden AND NOT deleted AND name='agentos-assistant' AND workspace_id=%s", (env["w"],)).fetchone()[0]
        assert n == 1
        with pytest.raises(psycopg.errors.UniqueViolation):   # another process without the lock cannot add a second one either
            c.execute("INSERT INTO agents(name, workspace_id, hidden) VALUES('agentos-assistant', %s, true)", (env["w"],))

# ---------------------------------------------------------------- c. 402 detail object
def _light(monkeypatch, gw):   # budget_check itself is under test: its DB read (caps_snapshot) is stubbed instead
    monkeypatch.setattr(gw, "ctx_now", lambda *a, **k: {})
    monkeypatch.setattr(gw, "_cur_ws", lambda: None)
    monkeypatch.setattr(gw, "gemini_only", lambda: False)
    monkeypatch.setattr(gw, "USAGE_SINK", [])

def test_budget_check_raises_with_machine_readable_info(monkeypatch):
    import gateway as gw
    _light(monkeypatch, gw)
    caps = [{"id": 1, "workspace_id": None, "scope": "feature", "target": "deep_research", "period": "month", "limit_usd": 0.5, "spent": 0.75, "action": "block", "enabled": True}]
    async def snap(fresh=False): return caps
    monkeypatch.setattr(gw, "caps_snapshot", snap)
    with pytest.raises(gw.BudgetExceeded) as ei:
        asyncio.run(gw.budget_check({"provider": "gemini", "workspace_id": 1, "feature": "deep_research"}))
    d = gw.cap_detail(ei.value)
    assert d["code"] == "spend_cap" and d["scope"] == "feature" and d["feature"] == "deep_research" and d["period"] == "month"
    assert d["limit_usd"] == 0.5 and d["used_usd"] == 0.75 and d["message"].startswith("Đã chạm trần chi phí tính năng Nghiên cứu sâu")
    # provider cap: info of the blocked provider
    caps[:] = [{"id": 2, "workspace_id": None, "scope": "provider", "target": "gemini", "period": "day", "limit_usd": 1.0, "spent": 1.2, "action": "block", "enabled": True}]
    monkeypatch.setattr(gw, "DB_BLOCK", {(None, "gemini")}); monkeypatch.setattr(gw, "_SNAP", {"t": 0.0, "caps": caps})
    with pytest.raises(gw.BudgetExceeded) as ei:
        asyncio.run(gw.budget_check({"provider": "gemini", "workspace_id": 1}))
    d = gw.cap_detail(ei.value, {"feature": None})
    assert (d["scope"], d["target"], d["period"], d["limit_usd"], d["used_usd"]) == ("provider", "gemini", "day", 1.0, 1.2)
    assert gw.cap_detail("plain text") == {"code": "spend_cap", "message": "plain text", "feature": None}   # old-style reason still works

def test_chat_run_402_detail_is_an_object(a):
    aid = a.post("/api/agents", json={"name": f"polb-cap-{SUF}", "system_prompt": "x", "tools": [], "budget_usd_day": 0.01}).json()["id"]
    tid = a.post("/api/v1/threads", json={"metadata": {"source": "chat", "graph_id": f"agent-{aid}"}}).json()["thread_id"]
    with pg() as c: c.execute("INSERT INTO gateway_usage(agent_id,model,input_tokens,output_tokens,cost_usd,status) VALUES(%s,'gemini-3.8-flash',0,0,0.02,'polb')", (aid,))
    try:
        r = a.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": f"agent-{aid}", "input": {"messages": [{"type": "human", "content": "xin chào"}]}})
        assert r.status_code == 402, r.text
        d = r.json()["detail"]
        assert d["code"] == "spend_cap" and d["scope"] == "agent" and d["period"] == "day" and d["limit_usd"] == 0.01 and d["used_usd"] >= 0.02 and d["message"], d
    finally:
        with pg() as c:
            c.execute("DELETE FROM gateway_usage WHERE status='polb'")
            c.execute("DELETE FROM lg_runs WHERE thread_id=%s", (tid,)); c.execute("DELETE FROM lg_threads WHERE thread_id=%s", (tid,))
            c.execute("DELETE FROM agents WHERE id=%s AND name LIKE 'polb-cap-%%'", (aid,))   # hard delete: no leftover agent in the local chat sidebar

# ---------------------------------------------------------------- d. stop message in the UI language
def test_research_stop_message_uses_ui_language():
    import deep_research as D
    from langchain_core.messages import HumanMessage
    g = D.ResearchGuard()
    async def go(lang, q):
        D.begin(True, lang); D.RUN.get().t0 -= 10_000
        return (await g.abefore_model({"messages": [HumanMessage(q)]}, None))["messages"][0].content
    assert asyncio.run(go("en", "So sánh các công cụ quan sát LLM mã nguồn mở")).startswith("Research stopped")   # EN UI, Vietnamese question
    assert asyncio.run(go("vi", "Compare open-source LLM observability tools")).startswith("Đã dừng nghiên cứu")   # VI UI, English question
    assert asyncio.run(go(None, "Compare open-source LLM observability tools")).startswith("Research stopped")     # no user (cron/channel): guess from text
    D.begin(False)
