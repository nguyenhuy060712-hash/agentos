"""A8 gate checks (server must run with AOS_VOICE_REAL_PROVIDERS=google for the 'real' tests; they skip otherwise).
No Live session is opened here: minting an ephemeral token is free, and no audio is sent => $0."""
import json, os, httpx, pytest
from pathlib import Path
B = "http://127.0.0.1:8020"; ADMIN = ("moza", "phase0-local"); STORE = Path(__file__).parent.parent / "var" / "voice_store.json"
SRC = (Path(__file__).parent.parent / "overlay" / "chat_overlay.py").read_text()
ns: dict = {}
exec(SRC[SRC.index("LIVE_PRICES ="):SRC.index("\n", SRC.index("LIVE_PRICES ="))], ns); exec(SRC[SRC.index("def _usage_cost"):SRC.index("AGENT_OS_DELEGATE")], ns)

@pytest.fixture(scope="module")
def a():
    with httpx.Client(base_url=B, auth=ADMIN, timeout=60) as c: yield c
def real(a): return "google" in a.get("/api/voice/spend").json()["real_providers"]
def thread(a): return a.post("/api/v1/threads", json={"metadata": {"source": "pytest"}}).json()["thread_id"]
def end(a, sid): a.post(f"/api/voice/session/{sid}/end", json={"seconds": 0, "reason": "test"})

def test_pricing_matches_official_table_on_a_real_usage_message():
    u = {"promptTokenCount": 769, "responseTokenCount": 90, "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 479}, {"modality": "AUDIO", "tokenCount": 226}],
         "responseTokensDetails": [{"modality": "AUDIO", "tokenCount": 90}], "thoughtsTokenCount": 35}
    exp = (479 * 0.75 + 226 * 3.00 + 64 * 3.00 + 90 * 12.00 + 35 * 4.50) / 1e6
    assert abs(ns["_usage_cost"]("gemini-3.8-live", u) - exp) < 1e-12

def test_gpt_live_stays_mock_even_with_real_mode(a):
    s = a.get("/api/settings/voice").json()["workspace"]; w = {**s, "allowed_models": ["gpt-live-1", "gemini-3.8-live"]}; w.pop("voice_roles", None); w.pop("max_concurrent_per_user", None)
    a.put("/api/settings/voice", json={"workspace": w}); a.put("/api/settings/voice/agents/agent-1", json={"allowed_models": None, "default_model": None})
    g = a.post("/api/voice/session", json={"model": "gpt-live-1", "agent_id": "agent-1", "thread_id": thread(a)}).json()
    try: assert g["mock"] is True and g["adapter"] == "mock" and g["credential"]["value"].startswith("v=0 mock")
    finally: end(a, g["voice_session_id"])

def test_real_grant_is_ephemeral_token_never_the_key(a):
    if not real(a): pytest.skip("server not in A8 real mode")
    g = a.post("/api/voice/session", json={"model": "gemini-3.8-live", "agent_id": "agent-2", "thread_id": thread(a)}).json()
    try:
        assert g["mock"] is False and g["credential"]["type"] == "ephemeral_token" and g["credential"]["value"].startswith("auth_tokens/")
        assert "BidiGenerateContentConstrained" in g["ws_url"] and "v1beta" in g["ws_url"] and g["max_session_s"] <= 90
        for body in (json.dumps(g), a.get("/api/voice/config", params={"agent_id": "agent-2"}).text, a.get("/api/voice/spend").text):
            assert "AQ." not in body
    finally: end(a, g["voice_session_id"])

def test_a8_cap_refuses_new_real_sessions_at_080(a):
    if not real(a): pytest.skip("server not in A8 real mode")
    st = json.loads(STORE.read_text()); keep = st.get("real_spend_usd", 0.0)
    st["real_spend_usd"] = 0.81; STORE.write_text(json.dumps(st))
    try:
        r = a.post("/api/voice/session", json={"model": "gemini-3.8-live", "agent_id": "agent-2", "thread_id": thread(a)})
        assert r.status_code == 402 and r.json()["detail"]["code"] == "a8_cap"
    finally:
        st = json.loads(STORE.read_text()); st["real_spend_usd"] = keep; STORE.write_text(json.dumps(st, indent=1))
