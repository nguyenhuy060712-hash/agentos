"""Phase 0 server-side checks for the voice control plane (overlay on the real server.py, fake LLM, no provider calls).
Run: .venv/bin/python -m pytest -q tests/ (server must be running on :8020)."""
import copy, httpx, pytest, uuid
B = "http://127.0.0.1:8020"; ADMIN = ("moza", "phase0-local"); VIEWER = ("viewer1", "phase0-viewer-local")

@pytest.fixture(scope="module")
def a():
    with httpx.Client(base_url=B, auth=ADMIN, timeout=60) as c: yield c

@pytest.fixture(scope="module", autouse=True)
def restore(a):
    before = a.get("/api/settings/voice").json()
    yield
    a.put("/api/settings/voice", json={"workspace": before["workspace"]})
    for g in ("agent-1", "agent-2"):
        ov = before["agents"].get(g) or {}
        a.put(f"/api/settings/voice/agents/{g}", json={"allowed_models": ov.get("allowed_models"), "default_model": ov.get("default_model")})

def ws(a, **kw):
    w = copy.deepcopy(a.get("/api/settings/voice").json()["workspace"]); w.update(kw); w.pop("voice_roles", None); w.pop("max_concurrent_per_user", None)
    r = a.put("/api/settings/voice", json={"workspace": w}); assert r.status_code == 200, r.text; return r.json()

def thread(a, g="agent-2"):
    return a.post("/api/v1/threads", json={"metadata": {"source": "pytest", "graph_id": g}}).json()["thread_id"]

def end_all(a):
    for v in a.get("/api/voice/sessions", params={"status": "active"}).json():
        a.post(f"/api/voice/session/{v['id']}/end", json={"seconds": 1, "reason": "test-cleanup"})

@pytest.fixture(autouse=True)
def _clean(a):
    end_all(a); yield; end_all(a)

def test_A1_allowlist_default_and_agent_override(a):
    ws(a, allowed_models=["gpt-live-1", "gemini-3.8-live"], default_model="gpt-live-1")
    a.put("/api/settings/voice/agents/agent-2", json={"allowed_models": None, "default_model": None})
    c = a.get("/api/voice/config", params={"agent_id": "agent-2"}).json()
    assert c["allowed"] == ["gpt-live-1", "gemini-3.8-live"] and c["default_model"] == "gpt-live-1" and c["source"] == "workspace"
    a.put("/api/settings/voice/agents/agent-2", json={"allowed_models": ["gemini-3.8-live"], "default_model": "gemini-3.8-live"})
    c2 = a.get("/api/voice/config", params={"agent_id": "agent-2"}).json()
    assert c2["allowed"] == ["gemini-3.8-live"] and c2["default_model"] == "gemini-3.8-live" and c2["source"] == "agent"
    assert a.get("/api/voice/config", params={"agent_id": "agent-1"}).json()["allowed"] == ["gpt-live-1", "gemini-3.8-live"]
    r = a.post("/api/voice/session", json={"model": "gpt-live-1", "agent_id": "agent-2", "thread_id": thread(a)})
    assert r.status_code == 403 and "not allowed" in r.text
    r = a.post("/api/voice/session", json={"model": "made-up-live", "agent_id": "agent-1", "thread_id": thread(a, "agent-1")})
    assert r.status_code == 403

def test_settings_validation(a):
    w = a.get("/api/settings/voice").json()["workspace"]
    bad = {**w, "default_model": "gemini-3.8-live", "allowed_models": ["gpt-live-1"]}; bad.pop("voice_roles", None); bad.pop("max_concurrent_per_user", None)
    assert a.put("/api/settings/voice", json={"workspace": bad}).status_code == 422
    assert a.put("/api/settings/voice/agents/agent-1", json={"allowed_models": ["nope"]}).status_code == 422
    assert a.get("/api/settings/voice").json()["audit"], "every change is audited"

def test_A2_third_model_is_catalog_data(a):
    s = a.get("/api/settings/voice").json(); cat = [m for m in s["catalog"] if m["id"] != "gemini-3.8-live-eu"]
    extra = {"id": "gemini-3.8-live-eu", "provider": "google", "display_name": "Gemini 3.8 Live (EU, test row)", "transport": "websocket",
             "price_unit": "per_mtok", "price_in": 3.0, "price_out": 12.0, "est_usd_per_min": 0.045, "enabled": True, "voice": "Kore"}
    w = {k: v for k, v in s["workspace"].items() if k not in ("voice_roles", "max_concurrent_per_user")}
    w["allowed_models"] = list(dict.fromkeys(w["allowed_models"] + [extra["id"]]))
    assert a.put("/api/settings/voice", json={"workspace": w, "catalog": cat + [extra]}).status_code == 200
    a.put("/api/settings/voice/agents/agent-1", json={"allowed_models": None, "default_model": None})
    assert extra["id"] in a.get("/api/voice/config", params={"agent_id": "agent-1"}).json()["allowed"]
    r = a.post("/api/voice/session", json={"model": extra["id"], "agent_id": "agent-1", "thread_id": thread(a, "agent-1")})
    try:
        assert r.status_code == 200 and r.json()["provider"] == "google"
        a.post(f"/api/voice/session/{r.json()['voice_session_id']}/end", json={"seconds": 1})
    finally:
        w["allowed_models"] = [m for m in w["allowed_models"] if m != extra["id"]]; a.put("/api/settings/voice", json={"workspace": w, "catalog": cat})

def test_A6_budget_402_and_minutes_cap(a):
    ws(a, allowed_models=["gpt-live-1", "gemini-3.8-live"], default_model="gemini-3.8-live", daily_budget_usd=0.0)
    a.put("/api/settings/voice/agents/agent-1", json={"allowed_models": None, "default_model": None})
    r = a.post("/api/voice/session", json={"model": "gemini-3.8-live", "agent_id": "agent-1", "thread_id": thread(a, "agent-1")})
    assert r.status_code == 402 and r.json()["detail"]["code"] == "budget"
    ws(a, daily_budget_usd=1.0, max_session_min=0.1)
    r = a.post("/api/voice/session", json={"model": "gemini-3.8-live", "agent_id": "agent-1", "thread_id": thread(a, "agent-1")})
    assert r.status_code == 200 and r.json()["max_session_s"] == 6
    assert "sk-" not in r.text and "AIza" not in r.text and r.json()["mock"] is True
    sid = r.json()["voice_session_id"]
    assert a.post("/api/voice/session", json={"model": "gemini-3.8-live", "agent_id": "agent-1", "thread_id": thread(a, "agent-1")}).status_code == 429
    e = a.post(f"/api/voice/session/{sid}/end", json={"seconds": 600}).json()
    assert e["seconds"] == 6 and e["cost_usd"] > 0, "server clamps to max session length"

def test_A4_A5_delegation_hitl_and_transcript(a):
    ws(a, allowed_models=["gpt-live-1", "gemini-3.8-live"], default_model="gemini-3.8-live", daily_budget_usd=1.0, max_session_min=10)
    tid = thread(a, "agent-2"); a.put("/api/settings/voice/agents/agent-2", json={"allowed_models": None, "default_model": None})
    g = a.post("/api/voice/session", json={"model": "gemini-3.8-live", "agent_id": "agent-2", "thread_id": tid}).json(); sid = g["voice_session_id"]
    assert a.post("/api/voice/transcript", json={"voice_session_id": sid, "items": [{"role": "assistant", "text": "Hi, I'm listening."}]}).json()["status"] == "written"
    d = a.post("/api/voice/delegate", json={"voice_session_id": sid, "call_id": "t-" + uuid.uuid4().hex[:6], "request": "ask AI Lead about the campaign status"}).json()
    assert d["status"] == "success" and "campaign" in d["reply"]
    n_posts = len(a.get("/api/v1/threads/" + tid + "/state").json()["values"]["messages"])
    cid = "t-" + uuid.uuid4().hex[:6]
    h = a.post("/api/voice/delegate", json={"voice_session_id": sid, "call_id": cid, "request": "publish the campaign post"}).json()
    assert h["status"] == "interrupted" and h["approval"]["interrupts"][0]["action_requests"][0]["name"] == "content_publish"
    st = a.get(f"/api/v1/threads/{tid}/state").json()
    assert not any(m["type"] == "tool" for m in st["values"]["messages"][n_posts:]), "nothing ran before approval"
    a.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": "agent-2", "command": {"resume": {"decisions": [{"type": "approve"}]}}})
    s2 = a.get(f"/api/voice/delegate/{cid}").json()
    assert s2["status"] == "success" and "published" in (s2["reply"] or "")
    a.post(f"/api/voice/session/{sid}/end", json={"seconds": 20})
    msgs = a.get(f"/api/v1/threads/{tid}/state").json()["values"]["messages"]
    texts = [m["content"] for m in msgs]
    assert "Hi, I'm listening." in texts and any("Post #" in t for t in texts) and any(m.get("tool_calls") for m in msgs)

def test_foreign_session_is_404(a):
    assert a.post("/api/voice/delegate", json={"voice_session_id": "vs_nope", "call_id": "x", "request": "hi"}).status_code == 404

def test_viewer_permissions(a):
    a.post("/api/admin/users", json={"username": VIEWER[0], "password": VIEWER[1], "role": "viewer"})
    with httpx.Client(base_url=B, auth=VIEWER, timeout=30) as v:
        assert v.get("/api/voice/config", params={"agent_id": "agent-1"}).status_code == 200
        assert v.put("/api/settings/voice", json={"workspace": a.get("/api/settings/voice").json()["workspace"]}).status_code == 403
        r = v.post("/api/voice/session", json={"model": "gemini-3.8-live", "agent_id": "agent-1", "thread_id": "00000000-0000-0000-0000-000000000000"})
        # FINDING: admin.py middleware blocks every viewer POST outside VIEWER_OK_POST -> Phase 1 must list /api/voice/* there if Q3 grants viewers.
        assert r.status_code == 403
