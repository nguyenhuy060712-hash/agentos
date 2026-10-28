"""GĐ3 (20/10/2026): generative UI `present`, web_search sources, live todos, per-run model allow-list, chat config/quota,
voice store in Postgres (+ JSON migration), nightly files backup. Local server :8020, fake LLM => $0, no network."""
import json, os, subprocess, uuid
import httpx, psycopg, pytest
from test_fixes import B, ADMIN, VIEWER, a, restore, _clean, ws, thread, voice_session  # noqa: F401  (fixtures)

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
GEN = "agent-1"   # local agent the GĐ3 fixture equips with present + web_search

@pytest.fixture(scope="module", autouse=True)
def gen_agent(a):
    cfg = a.get("/api/agents/1").json()["config"]; before = json.loads(json.dumps(cfg))
    cfg["tools"] = sorted(set(cfg.get("tools", [])) | {"local:present", "local:web_search"}); cfg["planning"] = True
    assert a.put("/api/agents/1", json=cfg).status_code == 200
    yield
    a.put("/api/agents/1", json=before)

def run(a, text, g=GEN, **extra):
    tid = thread(a, g)
    r = a.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": g, "input": {"messages": [{"type": "human", "content": text}]}, **extra})
    return tid, r

def msgs(a, tid): return a.get(f"/api/v1/threads/{tid}/state").json()["values"]["messages"]
def calls(ms, name): return [c for m in ms if m.get("type") == "ai" for c in (m.get("tool_calls") or []) if c["name"] == name]
def tool_msgs(ms, name): return [m for m in ms if m.get("type") == "tool" and m.get("name") == name]

# ---------------------------------------------------------------- 3.1 generative UI
@pytest.mark.parametrize("prompt,root", [("vẽ biểu đồ doanh thu", "Col"), ("lập bảng kênh", "Table"), ("so sánh 3 gói", "Comparison"), ("thông số chiến dịch", "SpecSheet")])
def test_present_renders_tree(a, prompt, root):
    tid, r = run(a, prompt); assert r.status_code == 200, r.text
    ms = msgs(a, tid); c = calls(ms, "present")
    assert c and c[0]["args"]["_type"] == root
    assert tool_msgs(ms, "present")[0]["content"].startswith("Shown to the user")
    assert ms[-1]["type"] == "ai" and ms[-1]["content"]   # text summary after the UI

def test_present_rejects_unknown_component(a):
    tid, _ = run(a, "ui lỗi")
    t = tool_msgs(msgs(a, tid), "present")[0]["content"]
    assert t.startswith("NOT shown") and "unknown _type" in t

def test_present_schema_shared_with_ui():
    here = os.path.dirname(os.path.abspath(__file__))
    srv = json.load(open(os.path.join(here, "..", "app", "genui_schema.json")))
    assert {"Chart", "Table", "Comparison", "SpecSheet", "Steps", "Citation"} <= set(srv["components"])
    assert not {"Image", "Map", "Button", "Form", "Input"} & set(srv["components"]), "no external-request / action components"
    ui = "/workspace/agentos-chat/src/genui/present_schema.json"
    if os.path.exists(ui): assert json.load(open(ui)) == srv, "server schema must be the UI export, verbatim"

def test_chat_config_flags_and_quota(a):
    c = a.get("/api/chat/config", params={"agent_id": GEN}).json()
    assert c["genui"] is True and c["default_model"] in [m["id"] for m in c["models"]]
    assert "openai" in c["quota"]["provider_caps"] and "agent_spent_today_usd" in c["quota"]
    assert a.get("/api/chat/config", params={"agent_id": "agent-2"}).json()["genui"] is False   # agent-2: no `present` (agent-3 is not in ws 1 on a fresh DB)
    assert httpx.get(B + "/api/chat/config", params={"agent_id": GEN}).status_code == 401

# ---------------------------------------------------------------- 3.3 sources
def test_web_search_results_are_numbered_sources(a):
    tid, _ = run(a, "tìm web về Agent OS")
    t = tool_msgs(msgs(a, tid), "web_search")[0]["content"]
    lines = t.splitlines()
    assert lines[0].startswith("1. ") and lines[1].strip().startswith("https://") and len([l for l in lines if l[:2] in ("1.", "2.", "3.")]) == 3

# ---------------------------------------------------------------- 3.2 live todos
def test_todos_progress_three_updates(a):
    tid, _ = run(a, "lập kế hoạch báo cáo")
    st = a.get(f"/api/v1/threads/{tid}/state").json()
    assert len(calls(st["values"]["messages"], "write_todos")) == 3
    assert [t["status"] for t in st["values"]["todos"]] == ["completed"] * 3

# ---------------------------------------------------------------- 3.5 per-run model allow-list
def test_run_model_allow_list_403(a):
    allowed = [m["id"] for m in a.get("/api/chat/config", params={"agent_id": GEN}).json()["models"]]
    tid = thread(a, GEN)
    bad = a.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": GEN, "input": {"messages": [{"type": "human", "content": "hi"}]},
                                                         "config": {"configurable": {"model": "gpt-5.4-mini-not-allowed"}}})
    assert bad.status_code == 403
    ok = a.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": GEN, "input": {"messages": [{"type": "human", "content": "hi"}]},
                                                        "config": {"configurable": {"model": allowed[-1]}}})
    assert ok.status_code == 200, ok.text

def test_chat_settings_admin_only_and_agent_override(a):
    with httpx.Client(base_url=B, auth=VIEWER, timeout=30) as v:
        assert v.put("/api/settings/chat", json={"workspace": []}).status_code == 403
    assert a.put("/api/settings/chat", json={"workspace": ["not-a-model"]}).status_code == 422
    try:
        assert a.put("/api/settings/chat", json={"agents": {GEN: ["gpt-5.4-nano"]}}).status_code == 200
        c = a.get("/api/chat/config", params={"agent_id": GEN}).json()
        assert [m["id"] for m in c["models"]] == ["gpt-5.4-nano"] and c["picker"] is False
        tid = thread(a, GEN)
        assert a.post(f"/api/v1/threads/{tid}/runs", json={"assistant_id": GEN, "input": {"messages": [{"type": "human", "content": "hi"}]},
                                                          "config": {"configurable": {"model": "gemini-3.6-flash"}}}).status_code == 403
    finally:
        a.put("/api/settings/chat", json={"agents": {GEN: None}})

# ---------------------------------------------------------------- (c) voice store in Postgres
def test_voice_store_is_postgres_with_migration(a):
    st = a.get("/api/settings/voice").json()["store"]
    assert st["backend"] == "postgres" and st["migration"]["at"]
    with psycopg.connect(PG) as c:
        n = c.execute("SELECT count(*) FROM voice_sessions").fetchone()[0]
        kv = {r[0] for r in c.execute("SELECT k FROM voice_kv")}
    assert {"schema", "catalog", "workspace", "migration"} <= kv and n >= 1

def test_voice_session_row_written_and_updated(a):
    tid = thread(a, "agent-2")
    sid = voice_session(a, tid)
    with psycopg.connect(PG) as c:
        assert c.execute("SELECT status FROM voice_sessions WHERE id=%s", (sid,)).fetchone()[0] == "active"
    a.post(f"/api/voice/session/{sid}/end", json={"seconds": 3, "reason": "pytest"})
    with psycopg.connect(PG) as c:
        row = c.execute("SELECT status, data->>'seconds' FROM voice_sessions WHERE id=%s", (sid,)).fetchone()
    assert row[0] == "ended" and float(row[1]) == 3

def test_voice_settings_audit_append_only(a):
    with psycopg.connect(PG) as c: n0 = c.execute("SELECT count(*) FROM voice_audit").fetchone()[0]
    w = a.get("/api/settings/voice").json()["workspace"]
    ws(a, daily_min_per_user=30); ws(a, daily_min_per_user=31)
    with psycopg.connect(PG) as c: n1 = c.execute("SELECT count(*) FROM voice_audit").fetchone()[0]
    assert n1 == n0 + 2

# ---------------------------------------------------------------- (d) nightly backup includes var/files
def test_backup_script_archives_files():
    here = os.path.dirname(os.path.abspath(__file__)); p = os.path.join(here, "..", "app", "ops", "pg_backup.sh")
    assert subprocess.run(["bash", "-n", p]).returncode == 0
    s = open(p).read()
    assert "/opt/agentos/var/files" in s and "/opt/agentos_backups/files" in s and "tar -tzf" in s
