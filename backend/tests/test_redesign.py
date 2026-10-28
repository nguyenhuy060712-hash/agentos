"""Regression tests for the Mic/Voice + layout redesign (queued 19:01/19:05, voice tech rule 19:07): dictation reuses the SAME selected
live model through an Agent OS ephemeral session (purpose=dictation, MOCK here => $0); voice threads get a title from the first utterance."""
from test_fixes import ADMIN, a, restore, _clean, ws, thread  # noqa: F401  (fixtures)

def _ws_ok(a):
    ws(a, allowed_models=["gpt-live-1", "gemini-3.8-live"], default_model="gemini-3.8-live", daily_budget_usd=100, daily_min_per_user=600, max_session_min=10)
    a.put("/api/settings/voice/agents/agent-1", json={"allowed_models": None, "default_model": None})

def test_dictation_session_same_live_model_no_thread_no_tools(a):
    _ws_ok(a)
    r = a.post("/api/voice/session", json={"model": "gemini-3.8-live", "agent_id": "agent-1", "purpose": "dictation"})
    assert r.status_code == 200, r.text
    g = r.json()
    assert g["purpose"] == "dictation" and g["model"] == "gemini-3.8-live" and g["thread_id"] is None and g["tools"] == []
    assert g["credential"]["type"] == "ephemeral_token" and g["mock"] is True
    # a dictation session never writes a transcript into a thread
    t = a.post("/api/voice/transcript", json={"voice_session_id": g["voice_session_id"], "items": [{"role": "user", "text": "x", "phase": "turn"}]})
    assert t.status_code in (409, 422), t.text
    e = a.post(f"/api/voice/session/{g['voice_session_id']}/end", json={"seconds": 2, "reason": "dictation-confirm"}).json()
    assert e["status"] == "ended" and e["cost_usd"] > 0, "dictation minutes are metered like a call (same budget)"

def test_dictation_respects_allow_list_and_budget(a):
    _ws_ok(a); a.put("/api/settings/voice/agents/agent-1", json={"allowed_models": ["gemini-3.8-live"], "default_model": "gemini-3.8-live"})
    assert a.post("/api/voice/session", json={"model": "gpt-live-1", "agent_id": "agent-1", "purpose": "dictation"}).status_code == 403
    a.put("/api/settings/voice/agents/agent-1", json={"allowed_models": None, "default_model": None})
    ws(a, daily_budget_usd=0)
    assert a.post("/api/voice/session", json={"model": "gemini-3.8-live", "agent_id": "agent-1", "purpose": "dictation"}).status_code == 402
    _ws_ok(a)

def test_voice_thread_title_from_first_utterance(a):
    _ws_ok(a)
    r = a.post("/api/voice/session", json={"model": "gemini-3.8-live", "agent_id": "agent-1"}).json()   # server-created voice thread
    tid = r["thread_id"]; md = a.get(f"/api/v1/threads/{tid}").json()["metadata"]
    assert md["voice"] is True and md["title_auto"] is True
    a.post("/api/voice/transcript", json={"voice_session_id": r["voice_session_id"], "items": [
        {"role": "assistant", "text": "Xin chào, em đang nghe anh.", "phase": "turn"},
        {"role": "user", "text": "Tóm tắt giúp anh tình hình chiến dịch tháng mười cho đội marketing nhé em", "phase": "turn"}]})
    md = a.get(f"/api/v1/threads/{tid}").json()["metadata"]
    assert md["title"].startswith("Tóm tắt giúp anh tình hình") and len(md["title"]) <= 60 and md["title_auto"] is False, md
    a.post("/api/voice/transcript", json={"voice_session_id": r["voice_session_id"], "items": [{"role": "user", "text": "câu sau", "phase": "turn"}]})
    assert a.get(f"/api/v1/threads/{tid}").json()["metadata"]["title"] == md["title"], "title fixed after the first utterance"
    # a typed thread that later gets a call keeps its typed title but is marked as voice
    t2 = thread(a, "agent-1", title="Kế hoạch Q4", source="chat")
    a.post(f"/api/voice/session/{r['voice_session_id']}/end", json={"seconds": 1, "reason": "t"})
    s2 = a.post("/api/voice/session", json={"model": "gemini-3.8-live", "agent_id": "agent-1", "thread_id": t2}).json()
    a.post("/api/voice/transcript", json={"voice_session_id": s2["voice_session_id"], "items": [{"role": "user", "text": "xin chào", "phase": "turn"}]})
    md2 = a.get(f"/api/v1/threads/{t2}").json()["metadata"]
    assert md2["title"] == "Kế hoạch Q4" and md2["voice"] is True

def test_readaloud_session_same_live_model_no_thread(a):
    _ws_ok(a)
    g = a.post("/api/voice/session", json={"model": "gemini-3.8-live", "agent_id": "agent-1", "purpose": "readaloud"}).json()
    assert g["purpose"] == "readaloud" and g["thread_id"] is None and g["tools"] == [] and g["model"] == "gemini-3.8-live"
    t = a.post("/api/voice/transcript", json={"voice_session_id": g["voice_session_id"], "items": [{"role": "assistant", "text": "x", "phase": "turn"}]})
    assert t.status_code in (409, 422)
    assert a.post(f"/api/voice/session/{g['voice_session_id']}/end", json={"seconds": 1, "reason": "readaloud-finished"}).json()["status"] == "ended"
    # any other purpose is rejected
    assert a.post("/api/voice/session", json={"model": "gemini-3.8-live", "agent_id": "agent-1", "purpose": "tts"}).status_code == 422
