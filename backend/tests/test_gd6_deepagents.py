"""GĐ6.0b (22/10/2026): deepagents 0.7.32 pin (GĐ11.63; was 0.7.30) + fs-permission build check. 0.7.25 made permission rules / interrupt_on check both the
requested and the symlink-resolved path; our rules use virtual StateBackend paths (/skills/**, /memory/**) with no symlinks, so they are unaffected.
Local server :8020, fake LLM => $0."""
import json, uuid
from importlib.metadata import version
import pytest
from test_fixes import B, ADMIN, VIEWER, a, restore, _clean  # noqa: F401  (fixtures)

def test_deepagents_pinned():
    assert version("deepagents") == "0.7.32"

def test_agent_builds_with_permissions(a):
    """Agent with a published skill (=> FilesystemPermission deny-write on /skills/**, /memory/**) must build and answer under 0.7.29+."""
    cfg = a.get("/api/agents/1").json()["config"]; before = json.loads(json.dumps(cfg))
    nm = "t-gd6-skill-" + uuid.uuid4().hex[:6]
    r = a.post("/api/contexts", json={"kind": "skill", "name": nm, "status": "active",
                                      "files": {"SKILL.md": "\n".join(["---", "name: " + nm, "description: test skill", "---", "Say hi."])}})
    assert r.status_code == 200, r.text; cid = r.json()["id"]
    try:
        cfg["skills"] = [nm]
        assert a.put("/api/agents/1", json=cfg).status_code == 200
        t = a.post("/api/v1/threads", json={"metadata": {"source": "chat", "graph_id": "agent-1"}}).json()["thread_id"]
        r = a.post(f"/api/v1/threads/{t}/runs/wait", json={"assistant_id": "agent-1", "input": {"messages": [{"type": "human", "content": "xin chào"}]}})
        assert r.status_code == 200, r.text
    finally:
        a.put("/api/agents/1", json=before); a.delete(f"/api/contexts/{cid}")
