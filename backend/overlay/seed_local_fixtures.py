"""LOCAL-ONLY: bring a fresh local DB to the agent state the test suite expects (the old box DB had it from the Phase 0 spike):
agent-2 can call content_publish, gated by HITL (interrupt_on) -> test_fixes G16/G7, test_voice_overlay A4/A5.
Idempotent. Needs the local server on :8020 (run_local.py). Never run against #83."""
import os, httpx

B = os.environ.get("AOS_BASE", "http://127.0.0.1:8020"); ADMIN = ("moza", "phase0-local")
assert B.startswith("http://127.0.0.1"), "local server only"

with httpx.Client(base_url=B, auth=ADMIN, timeout=60) as a:
    cfg = a.get("/api/agents/2").json()["config"]
    want_tools = set(cfg.get("tools", [])) | {"local:content_publish"}; want_gate = set(cfg.get("interrupt_on", [])) | {"local:content_publish"}
    if want_tools != set(cfg.get("tools", [])) or want_gate != set(cfg.get("interrupt_on", [])):
        r = a.put("/api/agents/2", json={**cfg, "tools": sorted(want_tools), "interrupt_on": sorted(want_gate)}); r.raise_for_status()
        print("agent-2: content_publish + HITL added")
print("fixtures ok")
