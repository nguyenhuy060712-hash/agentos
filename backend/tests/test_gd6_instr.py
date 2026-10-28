"""GD6.9 (22/10/2026): personal instructions per user. Local server :8020, fake LLM => $0. The fake echoes the system prompt it received for
'show system prompt'. Throwaway users."""
import httpx, pytest
from test_fixes import B, ADMIN, a, restore, _clean  # noqa: F401  (fixtures)

PW = "phase0-gd69-local"; G = "agent-2"
ABOUT = "Tôi là chủ quán cà phê Moza ở Đà Lạt"; STYLE = "Trả lời ngắn gọn, xưng em gọi anh"

def _user(a, name, **kw):
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": "viewer" if kw.get("demo") else "editor", **kw})
    uid = r.json().get("id") if r.status_code == 200 else next(x["id"] for x in a.get("/api/admin/users").json() if x["username"] == name)
    return uid, httpx.Client(base_url=B, auth=(name, PW), timeout=90)

@pytest.fixture(scope="module")
def u1(a):
    uid, c = _user(a, "gd69_u1"); yield c; c.close(); a.delete(f"/api/admin/users/{uid}")

@pytest.fixture(scope="module")
def u2(a):
    uid, c = _user(a, "gd69_u2"); yield c; c.close(); a.delete(f"/api/admin/users/{uid}")

@pytest.fixture(scope="module")
def demo(a):
    uid, c = _user(a, "gd69_demo", demo=True, daily_cap_usd=1.0, demo_agents=[2]); yield c; c.close(); a.delete(f"/api/admin/users/{uid}")

def sysprompt(c):
    tid = c.post("/api/v1/threads", json={"metadata": {"source": "chat", "graph_id": G}}).json()["thread_id"]
    r = c.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": G, "input": {"messages": [{"type": "human", "content": "show system prompt"}]}})
    assert r.status_code == 200, r.text
    ms = c.get(f"/api/v1/threads/{tid}/state").json()["values"]["messages"]
    t = ms[-1]["content"]; assert t.startswith("SYS<<"), t
    return t

def test_default_empty(u1):
    r = u1.get("/api/chat/me/instructions").json()
    assert r["about"] == "" and r["style"] == "" and r["enabled"] is True and r["max_chars"] == 1500
    assert "Personal instructions" not in sysprompt(u1)

def test_present_when_enabled_absent_when_disabled(u1, u2):
    assert u1.put("/api/chat/me/instructions", json={"about": ABOUT, "style": STYLE, "enabled": True}).status_code == 200
    assert u1.get("/api/chat/me/instructions").json()["about"] == ABOUT
    p = sysprompt(u1); assert ABOUT in p and STYLE in p and "Personal instructions" in p
    assert ABOUT not in sysprompt(u2) and STYLE not in sysprompt(u2)      # another user never gets it
    assert u2.get("/api/chat/me/instructions").json()["about"] == ""
    assert u1.put("/api/chat/me/instructions", json={"about": ABOUT, "style": STYLE, "enabled": False}).status_code == 200
    p = sysprompt(u1); assert ABOUT not in p and STYLE not in p           # off-switch also applies to a brand-new thread of the same cached agent
    assert u1.get("/api/chat/me/instructions").json()["about"] == ABOUT   # text kept while disabled

def test_edit_takes_effect_on_existing_thread(u1):
    u1.put("/api/chat/me/instructions", json={"about": "v1-marker", "style": "", "enabled": True})
    tid = u1.post("/api/v1/threads", json={"metadata": {"source": "chat", "graph_id": G}}).json()["thread_id"]
    def ask():
        u1.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": G, "input": {"messages": [{"type": "human", "content": "show system prompt"}]}})
        return u1.get(f"/api/v1/threads/{tid}/state").json()["values"]["messages"][-1]["content"]
    assert "v1-marker" in ask()
    u1.put("/api/chat/me/instructions", json={"about": "v2-marker", "style": "", "enabled": True})
    t = ask(); assert "v2-marker" in t and "v1-marker" not in t

def test_length_limit(u1):
    assert u1.put("/api/chat/me/instructions", json={"about": "x" * 1500, "style": "y" * 1500, "enabled": True}).status_code == 200
    assert u1.put("/api/chat/me/instructions", json={"about": "x" * 1501, "style": "", "enabled": True}).status_code == 422
    assert u1.put("/api/chat/me/instructions", json={"about": "", "style": "y" * 1501, "enabled": True}).status_code == 422
    u1.put("/api/chat/me/instructions", json={"about": "", "style": "", "enabled": True})

def test_demo_read_only(demo):
    assert demo.get("/api/chat/me/instructions").json()["read_only"] is True
    assert demo.put("/api/chat/me/instructions", json={"about": "hack", "style": "", "enabled": True}).status_code == 403
    assert demo.get("/api/chat/me/instructions").json()["about"] == ""

def test_viewer_can_edit_own(a):
    uid, c = _user(a, "gd69_viewer")
    try:
        a.patch(f"/api/admin/users/{uid}", json={"role": "viewer"})
        assert c.put("/api/chat/me/instructions", json={"about": "viewer ok", "style": "", "enabled": True}).status_code == 200
        assert c.get("/api/chat/me/instructions").json()["about"] == "viewer ok"
    finally:
        c.close(); a.delete(f"/api/admin/users/{uid}")

def test_unauthenticated():
    assert httpx.get(B + "/api/chat/me/instructions").status_code == 401
