"""Regression tests for the local fixes (steering 17:34): server.py/obs.py/admin.py/deepagent_api.py/mocks.py patches + voice overlay.
Run against the local server on :8020 (MOCK mode, fake LLM => $0)."""
import json, os, subprocess, threading, time, uuid, copy
import httpx, pytest
B = "http://127.0.0.1:8020"; ADMIN = ("moza", "phase0-local"); VIEWER = ("viewer1", "phase0-viewer-local")

@pytest.fixture(scope="module")
def a():
    with httpx.Client(base_url=B, auth=ADMIN, timeout=90) as c: yield c

@pytest.fixture(scope="module", autouse=True)
def restore(a):
    before = a.get("/api/settings/voice").json()
    yield
    w = before["workspace"]; w.pop("max_concurrent_per_user", None)
    a.put("/api/settings/voice", json={"workspace": w})

def end_all(a):
    for v in a.get("/api/voice/sessions", params={"status": "active"}).json():
        a.post(f"/api/voice/session/{v['id']}/end", json={"seconds": 1, "reason": "test-cleanup"})

@pytest.fixture(autouse=True)
def _clean(a):
    end_all(a); yield; end_all(a)

def ws(a, **kw):
    w = copy.deepcopy(a.get("/api/settings/voice").json()["workspace"]); w.update(kw); w.pop("max_concurrent_per_user", None)
    r = a.put("/api/settings/voice", json={"workspace": w}); assert r.status_code == 200, r.text

def thread(a, g="agent-2", **md):
    return a.post("/api/v1/threads", json={"metadata": {"source": "pytest", "graph_id": g, **md}}).json()["thread_id"]

def sse(a, tid, body, stop_after=None):
    """Return list of (event, data) from POST runs/stream; optionally close after N events (simulates the Stop button)."""
    out = []
    with a.stream("POST", f"/api/v1/threads/{tid}/runs/stream", json=body) as r:
        ev = None
        for line in r.iter_lines():
            if line.startswith("event: "): ev = line[7:]
            elif line.startswith("data: "):
                out.append((ev, json.loads(line[6:]))); ev = None
                if stop_after and len(out) >= stop_after: break
    return out

def voice_session(a, tid, g="agent-2"):
    ws(a, allowed_models=["gpt-live-1", "gemini-3.8-live"], default_model="gemini-3.8-live", daily_budget_usd=100, daily_min_per_user=600, max_session_min=10)
    a.put(f"/api/settings/voice/agents/{g}", json={"allowed_models": None, "default_model": None})
    r = a.post("/api/voice/session", json={"model": "gemini-3.8-live", "agent_id": g, "thread_id": tid}); assert r.status_code == 200, r.text
    return r.json()["voice_session_id"]

# ---------------------------------------------------------------- G2 / G3 / G12 / G16
def test_G2_stream_ids_match_state_no_duplicate_turn(a):
    tid = thread(a); mid = "client-" + uuid.uuid4().hex[:8]
    evs = sse(a, tid, {"assistant_id": "agent-2", "input": {"messages": [{"type": "human", "id": mid, "content": "hello ids"}]},
                       "stream_mode": ["messages-tuple", "values"]})
    chunk_ids = {d[0]["id"] for e, d in evs if e == "messages"}
    msgs = a.get(f"/api/v1/threads/{tid}/state").json()["values"]["messages"]
    assert msgs[0]["id"] == mid, "input id kept"
    assert len(chunk_ids) == 1 and chunk_ids == {msgs[-1]["id"]}, (chunk_ids, msgs[-1]["id"])
    last_values = [d for e, d in evs if e == "values"][-1]["messages"]
    assert [m["id"] for m in last_values] == [m["id"] for m in msgs] and len(msgs) == 2

def test_G12_values_emitted_per_step(a):
    tid = thread(a)
    evs = sse(a, tid, {"assistant_id": "agent-2", "input": {"messages": [{"type": "human", "content": "make a todo list"}]}, "stream_mode": ["values", "updates"]})
    names = [e for e, _ in evs]
    vals = [i for i, e in enumerate(names) if e == "values"]
    assert len(vals) >= 2 and vals[0] < names.index("end") - 1, names

def test_G3_additional_kwargs_survive_reload(a):
    tid = thread(a); a.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": "agent-2", "input": {"messages": [{"type": "human", "content": "hi"}]}})
    a.post(f"/api/v1/threads/{tid}/state", json={"values": {"messages": [{"type": "ai", "content": "spoken", "additional_kwargs": {"modality": "voice"}}]}})
    m = a.get(f"/api/v1/threads/{tid}/state").json()["values"]["messages"][-1]
    assert m["additional_kwargs"]["modality"] == "voice"

def test_G16_interrupt_shape_and_G7_inbox_row_resolved(a):
    tid = thread(a)
    a.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": "agent-2", "input": {"messages": [{"type": "human", "content": "please publish this post"}]}})
    st = a.get(f"/api/v1/threads/{tid}/state").json()
    it = st["tasks"][0]["interrupts"][0]
    assert set(it) >= {"value", "id"} and it["value"]["action_requests"][0]["name"] == "content_publish"
    row = [x for x in a.get("/api/approvals", params={"status": "all"}).json() if x["thread_id"] == tid][0]
    assert row["status"] == "pending"
    a.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": "agent-2", "command": {"resume": {"decisions": [{"type": "approve"}]}}})
    row = [x for x in a.get("/api/approvals", params={"status": "all"}).json() if x["id"] == row["id"]][0]
    assert row["status"] == "approved" and "Post #" in (row["result"] or ""), row
    again = a.post(f"/api/v1/threads/{tid}/runs", json={"assistant_id": "agent-2", "command": {"resume": {"decisions": [{"type": "approve"}]}}})
    assert again.status_code == 409, "already decided -> no double execution"

def test_G7_inbox_decided_first_blocks_chat_resume(a):
    tid = thread(a)
    a.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": "agent-2", "input": {"messages": [{"type": "human", "content": "publish now"}]}})
    row = [x for x in a.get("/api/approvals").json() if x["thread_id"] == tid][0]
    st = a.post(f"/api/approvals/{row['id']}", json={"decision": "reject", "message": "no"}).json()["status"]
    for _ in range(120):   # GĐ7.Z: a chat-thread decision resumes through the run queue; under load it may still be 'processing' after 25 s
        if st != "processing": break
        time.sleep(1); st = [x for x in a.get("/api/approvals", params={"status": "all"}).json() if x["id"] == row["id"]][0]["status"]
    assert st == "rejected"
    r = a.post(f"/api/v1/threads/{tid}/runs", json={"assistant_id": "agent-2", "command": {"resume": {"decisions": [{"type": "approve"}]}}})
    assert r.status_code == 409

# ---------------------------------------------------------------- G11
@pytest.mark.parametrize("mode,expect", [("cancel", "interrupted"), ("continue", "success")])
def test_G11_on_disconnect(a, mode, expect):
    tid = thread(a)
    evs = sse(a, tid, {"assistant_id": "agent-2", "input": {"messages": [{"type": "human", "content": "slow answer please"}]},
                       "stream_mode": ["messages-tuple"], "on_disconnect": mode}, stop_after=3)
    rid = next(d["run_id"] for e, d in evs if e == "metadata")
    deadline = time.time() + (8 if mode == "cancel" else 70); st = None
    while time.time() < deadline:
        st = a.get(f"/api/v1/threads/{tid}/runs/{rid}").json()["status"]
        if st in ("interrupted", "success", "error"): break
        time.sleep(0.3)
    assert st == expect

# ---------------------------------------------------------------- G14 / G8
def test_G14_feedback_accepts_lg_run_id_and_G8_records_user(a):
    tid = thread(a)
    a.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": "agent-2", "input": {"messages": [{"type": "human", "content": "rate me"}]}})
    rid = a.get(f"/api/v1/threads/{tid}/runs", params={"limit": 1}).json()[0]["run_id"]
    r = a.post("/api/obs/feedback", json={"run_id": rid, "key": "user_score", "score": 1})
    assert r.status_code == 200, r.text
    import psycopg
    with psycopg.connect("postgresql://aos:aoslocal@127.0.0.1:5432/agentos") as c:
        run_id, by = c.execute("SELECT run_id::text, created_by FROM feedback WHERE id=%s", (r.json()["id"],)).fetchone()
        trace = c.execute("SELECT trace_id::text FROM lg_runs WHERE run_id=%s", (rid,)).fetchone()[0]
        tr = c.execute("SELECT id::text FROM tr_runs WHERE id=%s", (trace,)).fetchone()
    assert tr and run_id == tr[0] and by == "moza"

# ---------------------------------------------------------------- G9 / G15 (voice)
def test_G9_voice_use_permission_viewer_default_denied_configurable(a):
    a.post("/api/admin/users", json={"username": VIEWER[0], "password": VIEWER[1], "role": "viewer"})
    ws(a, voice_roles=["admin", "editor"])
    tid = thread(a, "agent-1")
    with httpx.Client(base_url=B, auth=VIEWER, timeout=30) as v:
        for x in v.get("/api/voice/sessions", params={"status": "active"}).json(): v.post(f"/api/voice/session/{x['id']}/end", json={"seconds": 1})
        assert v.get("/api/voice/config", params={"agent_id": "agent-1"}).json()["can_use"] is False
        r = v.post("/api/voice/session", json={"model": "gemini-3.8-live", "agent_id": "agent-1", "thread_id": tid})
        assert r.status_code == 403 and "voice.use" in r.text, "denied by the voice.use check (not by the generic middleware)"
        ws(a, voice_roles=["admin", "editor", "viewer"], allowed_models=["gpt-live-1", "gemini-3.8-live"], default_model="gemini-3.8-live", daily_budget_usd=100, daily_min_per_user=600)
        a.put("/api/settings/voice/agents/agent-1", json={"allowed_models": None, "default_model": None})
        assert v.post("/api/v1/threads", json={"metadata": {}}).status_code == 403, "viewers cannot create threads themselves"
        r = v.post("/api/voice/session", json={"model": "gemini-3.8-live", "agent_id": "agent-1"})   # -> server creates the voice thread
        assert r.status_code == 200, r.text
        assert v.get(f"/api/v1/threads/{r.json()['thread_id']}").json()["assistant_id"], "viewer sees its bound voice thread"
        sid = r.json()["voice_session_id"]
        assert v.post("/api/voice/transcript", json={"voice_session_id": sid, "items": [{"role": "user", "text": "xin chào"}]}).status_code == 200
        assert v.post("/api/voice/delegate", json={"voice_session_id": sid, "call_id": "vw1", "request": "hi"}).status_code == 403, "agent runs stay editor+"
        assert v.post(f"/api/voice/session/{sid}/end", json={"seconds": 1}).status_code == 200
    ws(a, voice_roles=["admin", "editor"])
    assert a.put("/api/settings/voice", json={"workspace": {**a.get("/api/settings/voice").json()["workspace"], "voice_roles": ["editor"]}}).status_code == 422

def test_G15_voice_first_thread_is_bound_before_transcript(a):
    tid = thread(a)
    assert a.get(f"/api/v1/threads/{tid}").json().get("assistant_id") in (None, "")
    sid = voice_session(a, tid)
    t = a.get(f"/api/v1/threads/{tid}").json(); assert t["assistant_id"], "bound at session time"
    r = a.post("/api/voice/transcript", json={"voice_session_id": sid, "items": [{"role": "assistant", "text": "Hello."}]}).json()
    assert r["status"] == "written"

# ---------------------------------------------------------------- transcript ordering / read-back, barge-in cancel
def test_transcript_preamble_and_readback_attach_to_the_run_answer(a):
    tid = thread(a); sid = voice_session(a, tid); cid = "c-" + uuid.uuid4().hex[:6]
    a.post("/api/voice/transcript", json={"voice_session_id": sid, "items": [{"role": "user", "text": "hello"}, {"role": "assistant", "text": "Hi there."}]})
    res = {}
    th = threading.Thread(target=lambda: res.update(a.post("/api/voice/delegate", json={"voice_session_id": sid, "call_id": cid, "request": "ask about the weekly report"}).json()))
    th.start(); time.sleep(0.05)
    a.post("/api/voice/transcript", json={"voice_session_id": sid, "items": [{"role": "assistant", "text": "Let me check that.", "phase": "preamble", "call_id": cid}]})
    th.join(30); assert res["status"] == "success"
    a.post("/api/voice/transcript", json={"voice_session_id": sid, "items": [{"role": "assistant", "text": "It says: weekly report.", "phase": "readback", "call_id": cid}]})
    msgs = a.get(f"/api/v1/threads/{tid}/state").json()["values"]["messages"]
    assert [m["content"] for m in msgs][:2] == ["hello", "Hi there."]
    assert msgs[2]["id"] == f"voice-{cid}" and msgs[2]["type"] == "human"
    ans = msgs[-1]; kw = ans["additional_kwargs"]
    assert len(msgs) == 4 and ans["content"].startswith("Echo:"), [m["content"] for m in msgs]
    assert kw["voice_preamble"] == "Let me check that." and kw["voice_readback"] == "It says: weekly report."

def test_barge_in_during_delegation_cancels_the_run_with_a_note(a):
    tid = thread(a); sid = voice_session(a, tid); cid = "c-" + uuid.uuid4().hex[:6]; res = {}
    th = threading.Thread(target=lambda: res.update(a.post("/api/voice/delegate", json={"voice_session_id": sid, "call_id": cid, "request": "slow report please"}).json()))
    th.start(); time.sleep(1.5)
    c = a.post(f"/api/voice/delegate/{cid}/cancel", json={"reason": "barge_in"}).json()
    assert c["cancelled"] is True and c["run_id"]
    th.join(20); assert res["status"] == "cancelled", res
    assert a.get(f"/api/v1/threads/{tid}/runs/{c['run_id']}").json()["status"] == "interrupted"
    msgs = a.get(f"/api/v1/threads/{tid}/state").json()["values"]["messages"]
    note = [m for m in msgs if (m.get("additional_kwargs") or {}).get("voice_event") == "delegate_cancelled"]
    assert note and note[0]["additional_kwargs"]["run_id"] == c["run_id"]
    assert a.post(f"/api/voice/delegate/{cid}/cancel", json={}).json()["cancelled"] is False, "idempotent"

# ---------------------------------------------------------------- fresh DB boot + mock tables under RLS
def test_fresh_database_boots_seeds_and_creates_mock_tables():
    db = "agentos_fresh_" + uuid.uuid4().hex[:6]; base = "postgresql://aos:aoslocal@127.0.0.1:5432/"
    import psycopg
    with psycopg.connect(base + "agentos", autocommit=True) as c: c.execute(f'CREATE DATABASE "{db}"')
    env = {**os.environ, "PG_URI": base + db, "PORT": "8021"}
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = subprocess.Popen([os.path.join(here, ".venv/bin/python"), os.path.join(here, "overlay/run_local.py")], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        log = ""; ok = False; t0 = time.time()
        while time.time() - t0 < 90:
            line = p.stdout.readline(); log += line
            if "Application startup complete" in line: ok = True; break
            if p.poll() is not None: break
        assert ok, log[-3000:]
        with psycopg.connect(base + db) as c:
            n = c.execute("SELECT count(*) FROM agents").fetchone()[0]
            tabs = {r[0] for r in c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}
        assert n >= 1, "SEED agents created on a fresh DB"
        assert {"published_posts", "crm_deals", "ads_campaigns"} <= tabs
    finally:
        p.terminate(); p.wait(15)
        with psycopg.connect(base + "agentos", autocommit=True) as c: c.execute(f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')

def test_G9_viewer_can_use_readonly_search_posts(a):
    """The front-end UI lists agents/threads via LangGraph POST /search; viewers must not be blocked (writes still are)."""
    a.post("/api/admin/users", json={"username": VIEWER[0], "password": VIEWER[1], "role": "viewer"})
    with httpx.Client(base_url=B, auth=VIEWER, timeout=30) as v:
        assert v.post("/api/v1/assistants/search", json={"limit": 10}).status_code == 200
        assert v.post("/api/v1/threads/search", json={"limit": 5}).status_code == 200
        assert v.post("/api/v1/threads", json={"metadata": {}}).status_code == 403
