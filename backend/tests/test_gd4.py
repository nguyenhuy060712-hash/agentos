"""GĐ4 (20/10/2026) + GĐ3 leftovers. Local server :8020, fake LLM => $0, no network.
- case 16: present guidance prefers Steps/Timeline/mermaid for process questions; fake LLM renders Steps
- case 9: visualise-only requests -> present only; an explicit launch raises the HITL interrupt for ads_create_campaign, resume runs it once
- upload rate limit (429 + Retry-After) per user"""
import os, sys, httpx, pytest
from test_fixes import B, ADMIN, VIEWER, a, restore, _clean, ws, thread  # noqa: F401
from test_fixes2 import png
from test_gd3 import run, msgs, calls, tool_msgs, gen_agent  # noqa: F401

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

def test_guide_covers_process_and_visualise_only():
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "genui.py"), encoding="utf-8").read()
    for s in ("QUY TRÌNH", "Steps (steps=[{title, detail}]", "Timeline (items=[{time, title, detail}])", "```mermaid flowchart TD```",
              "CHỈ dùng `present`", "ads_create_campaign", "content_publish"):
        assert s in src, s

def test_process_question_renders_steps(a):
    tid, r = run(a, "Trình bày quy trình triển khai chiến dịch ra mắt"); assert r.status_code == 200, r.text
    ms = msgs(a, tid); c = calls(ms, "present")
    assert c and any(ch.get("_type") == "Steps" for ch in c[0]["args"]["children"]), c
    assert tool_msgs(ms, "present")[0]["content"].startswith("Shown to the user")

def test_budget_split_visualise_only_no_write_tool(a):
    tid, r = run(a, "Ngân sách 30 triệu phân bổ Facebook 50%, Google 30%, TikTok 20%, hiển thị trực quan"); assert r.status_code == 200
    ms = msgs(a, tid)
    assert calls(ms, "present") and not calls(ms, "ads_create_campaign")

@pytest.fixture(scope="module")
def ads_agent(a):
    cfg = a.get("/api/agents/1").json()["config"]; before = dict(cfg)
    cfg = {**cfg, "tools": sorted(set(cfg.get("tools", [])) | {"local:present", "local:ads_create_campaign"}), "interrupt_on": sorted(set(cfg.get("interrupt_on", [])) | {"local:ads_create_campaign"})}
    assert a.put("/api/agents/1", json=cfg).status_code == 200
    yield "agent-1"
    a.put("/api/agents/1", json={**before, "tools": sorted(set(before.get("tools", [])) | {"local:present", "local:web_search"})})

def test_case9_ads_launch_interrupt_then_resume(a, ads_agent):
    tid, r = run(a, "Tạo chiến dịch Drip bag Tết 2027 trên Facebook", g=ads_agent); assert r.status_code == 200
    st = a.get(f"/api/v1/threads/{tid}/state").json()
    ints = [i for t in st.get("tasks", []) for i in (t.get("interrupts") or [])]
    assert ints, st.get("tasks")
    req = ints[0]["value"]["action_requests"][0]
    assert req["name"] == "ads_create_campaign" and req["args"]["budget_vnd"] == 8000000 and req["args"]["channel"] == "facebook"
    assert not tool_msgs(st["values"]["messages"], "ads_create_campaign"), "nothing ran before approval"
    r2 = a.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": ads_agent, "command": {"resume": {"decisions": [{"type": "approve"}]}}})
    assert r2.status_code == 200, r2.text
    ms = msgs(a, tid); out = tool_msgs(ms, "ads_create_campaign")
    assert len(out) == 1 and "launched on facebook" in out[0]["content"], out
    assert a.get(f"/api/v1/threads/{tid}").json()["status"] == "idle"

def test_upload_rate_limit_429(a):
    """A throwaway editor user hits the limit (so the admin's own window used by other tests/E2E stays clean)."""
    name, pw = "ratelimit_editor", "phase0-ratelimit-local"
    u = a.post("/api/admin/users", json={"username": name, "password": pw, "role": "editor"})
    uid = u.json().get("id") if u.status_code == 200 else next(x["id"] for x in a.get("/api/admin/users").json() if x["username"] == name)
    try:
        v = httpx.Client(base_url=B, auth=(name, pw), timeout=30)
        codes = [v.post("/api/files", files={"file": (f"r{i}.png", png((i, 9, 9)), "image/png")}).status_code for i in range(13)]
        assert codes[:12] == [200] * 12 and codes[12] == 429, codes
        r = v.post("/api/files", files={"file": ("x.png", png(), "image/png")})
        assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1 and "max 12/min" in r.text
        assert a.post("/api/files", files={"file": ("ok.png", png((7, 7, 7)), "image/png")}).status_code == 200, "limit is per user"
    finally:
        a.delete(f"/api/admin/users/{uid}")

# ---------------------------------------------------------------- voice resumption: fresh single-use token per reconnect
def test_voice_token_reissue_for_active_session_only(a):
    from test_gd3 import voice_session
    sid = voice_session(a, thread(a, "agent-2"))
    try:
        r1 = a.post(f"/api/voice/session/{sid}/token", json={}); assert r1.status_code == 200, r1.text
        r2 = a.post(f"/api/voice/session/{sid}/token", json={}).json()
        assert r1.json()["value"] != r2["value"] and r2["reissues"] == 2 and r2["expires_at"] > 0
        for _ in range(6): assert a.post(f"/api/voice/session/{sid}/token", json={}).status_code == 200
        assert a.post(f"/api/voice/session/{sid}/token", json={}).status_code == 429, "max 8 reissues per session"
    finally:
        a.post(f"/api/voice/session/{sid}/end", json={"seconds": 1, "reason": "pytest"})
    assert a.post(f"/api/voice/session/{sid}/token", json={}).status_code == 409, "ended session gets no token"
    assert httpx.post(B + f"/api/voice/session/{sid}/token", json={}).status_code in (401, 403), "auth required"
