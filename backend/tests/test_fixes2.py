"""Regression tests for the deferred items (approved 20/10/2026 18:51 ICT): G4 multimodal, interrupted-transcript truncation, G1 subgraphs.
Local server :8020, MOCK mode, fake LLM => $0."""
import base64, hashlib, json, struct, uuid, zlib
import httpx, psycopg, pytest
from test_fixes import B, ADMIN, VIEWER, a, restore, _clean, ws, thread, sse, voice_session  # noqa: F401  (fixtures)

def png(rgb=(255, 0, 0), w=2, h=2):
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
    ch = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + ch(b"IDAT", zlib.compress(raw)) + ch(b"IEND", b"")

# ---------------------------------------------------------------- G4
def test_G4_upload_limits_type_sniffing(a):
    assert a.post("/api/files", files={"file": ("x.png", b"not an image", "image/png")}).status_code == 415, "type sniffed from bytes"
    assert a.post("/api/files", files={"file": ("x.svg", b"<svg/>", "image/svg+xml")}).status_code == 415
    big = b"\x89PNG\r\n\x1a\n" + b"0" * (a.get("/api/files/config").json()["max_bytes"])
    assert a.post("/api/files", files={"file": ("big.png", big, "image/png")}).status_code == 413
    r = a.post("/api/files", files={"file": ("red.png", png(), "image/png")}); assert r.status_code == 200
    f = r.json(); g = a.get(f["url"])
    assert g.status_code == 200 and g.headers["content-type"] == "image/png" and g.content == png()

def test_G4_image_standard_blocks_end_to_end_and_reload(a):
    f = a.post("/api/files", files={"file": ("red.png", png(), "image/png")}).json()
    blue = png((0, 0, 255)); tid = thread(a, "agent-1")
    r = a.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": "agent-1", "input": {"messages": [{"type": "human", "content": [
        {"type": "text", "text": "what is in these?"}, {"type": "image", "url": f["url"]},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(blue).decode()}}]}]}})
    assert r.status_code == 200, r.text
    ans = r.json()["messages"][-1]["content"]
    # the (multimodal) fake model received the decoded bytes of BOTH images (LocalImageMiddleware materialised the URL blocks)
    assert "2 image(s)" in ans and hashlib.sha256(png()).hexdigest()[:12] in ans and hashlib.sha256(blue).hexdigest()[:12] in ans, ans
    human = a.get(f"/api/v1/threads/{tid}/state").json()["values"]["messages"][0]["content"]
    imgs = [b for b in human if b["type"] == "image"]
    assert len(imgs) == 2 and all(b["url"].startswith("/api/files/") and b["mime_type"] == "image/png" for b in imgs), human
    assert not any("base64" in b for b in human), "checkpoint keeps the URL block, never the bytes"
    with psycopg.connect("postgresql://aos:aoslocal@127.0.0.1:5432/agentos") as c:
        cp = c.execute("SELECT count(*) FROM checkpoint_blobs WHERE thread_id=%s AND position(%s in convert_from(blob,'LATIN1'))>0",
                       (f"a1:{tid}", base64.b64encode(blue).decode()[:40])).fetchone()[0]
    assert cp == 0, "no base64 in checkpoints"

def test_G4_run_input_limits(a):
    tid = thread(a, "agent-1")
    remote = {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}
    assert a.post(f"/api/v1/threads/{tid}/runs", json={"assistant_id": "agent-1", "input": {"messages": [{"type": "human", "content": [remote]}]}}).status_code == 422
    f = a.post("/api/files", files={"file": ("red.png", png(), "image/png")}).json()
    five = [{"type": "image", "url": f["url"]}] * 6   # GD6.8: limit is 5 files per message
    assert a.post(f"/api/v1/threads/{tid}/runs", json={"assistant_id": "agent-1", "input": {"messages": [{"type": "human", "content": five}]}}).status_code == 413
    pdf = {"type": "image", "base64": base64.b64encode(b"%PDF-1.4").decode(), "mime_type": "image/png"}
    assert a.post(f"/api/v1/threads/{tid}/runs", json={"assistant_id": "agent-1", "input": {"messages": [{"type": "human", "content": [pdf]}]}}).status_code == 415

def test_G4_files_are_private_to_the_uploader(a):
    a.post("/api/admin/users", json={"username": VIEWER[0], "password": VIEWER[1], "role": "viewer"})
    f = a.post("/api/files", files={"file": ("red.png", png((1, 2, 3)), "image/png")}).json()
    with httpx.Client(base_url=B, auth=VIEWER, timeout=30) as v:
        assert v.get(f["url"]).status_code == 404

# ---------------------------------------------------------------- interrupted voice transcript
def test_truncated_assistant_turn_is_persisted_with_flag(a):
    tid = thread(a); sid = voice_session(a, tid)
    a.post("/api/voice/transcript", json={"voice_session_id": sid, "items": [
        {"role": "user", "text": "tell me"},
        {"role": "assistant", "text": "The campaign is", "truncated": True, "played_ms": 900, "generated_ms": 3100, "full_chars": 58}]})
    m = a.get(f"/api/v1/threads/{tid}/state").json()["values"]["messages"][-1]
    kw = m["additional_kwargs"]
    assert m["content"] == "The campaign is" and "—" not in m["content"]
    assert kw["voice_truncated"] is True and kw["voice_played_ms"] == 900 and kw["voice_generated_ms"] == 3100 and kw["voice_full_chars"] == 58
    assert kw["metadata"]["voice_truncated"] is True, "exposed to assistant-ui as message.metadata.custom"

def test_truncated_readback_attached_to_run_answer(a):
    import threading, time
    tid = thread(a); sid = voice_session(a, tid); cid = "c-" + uuid.uuid4().hex[:6]; res = {}
    th = threading.Thread(target=lambda: res.update(a.post("/api/voice/delegate", json={"voice_session_id": sid, "call_id": cid, "request": "ask about the weekly report"}).json()))
    th.start(); th.join(30); assert res["status"] == "success"
    a.post("/api/voice/transcript", json={"voice_session_id": sid, "items": [{"role": "assistant", "text": "It says", "phase": "readback", "call_id": cid,
                                                                            "truncated": True, "played_ms": 400, "generated_ms": 2000, "full_chars": 40}]})
    kw = a.get(f"/api/v1/threads/{tid}/state").json()["values"]["messages"][-1]["additional_kwargs"]
    assert kw["voice_readback"] == "It says" and kw["voice_readback_truncated"] is True and kw["voice_readback_played_ms"] == 400

# ---------------------------------------------------------------- G1 subgraphs
@pytest.fixture(scope="module")
def coord(a):
    ags = a.get("/api/agents").json()
    c = next((x for x in ags if x["name"] == "Coordinator (fake)"), None)
    if not c:
        c = a.post("/api/agents", json={"name": "Coordinator (fake)", "model": "gpt-5.4-nano", "tools": ["local:content_publish"], "interrupt_on": ["local:content_publish"],
                                        "subagents": [{"name": "researcher", "description": "Researches and publishes on request", "tools": ["local:content_publish"]}]}).json()
    return f"agent-{c['id']}"

def test_G1_stream_subgraphs_event_names_and_values(a, coord):
    tid = thread(a, coord)
    evs = sse(a, tid, {"assistant_id": coord, "input": {"messages": [{"type": "human", "content": "delegate a short market report"}]},
                       "stream_mode": ["messages-tuple", "updates", "values"], "stream_subgraphs": True})
    names = [e for e, _ in evs]
    sub = [e for e in names if "|" in e]
    assert any(e.startswith("updates|tools:") for e in sub) and any(e.startswith("values|tools:") for e in sub) and any(e.startswith("messages|tools:") for e in sub), names
    sv = next(d for e, d in evs if e.startswith("values|tools:"))
    assert sv["messages"][0]["content"] == "a short market report", "sub-agent state, not the parent's"
    assert evs[-1][0] == "end" and "Echo: a short market report" in json.dumps(next(d for e, d in reversed(evs) if e == "values"))
    # without stream_subgraphs: no namespaced events (unchanged behaviour)
    tid2 = thread(a, coord)
    evs2 = sse(a, tid2, {"assistant_id": coord, "input": {"messages": [{"type": "human", "content": "delegate another report"}]}, "stream_mode": ["updates", "values"]})
    assert not [e for e, _ in evs2 if "|" in e]

def test_G1_hitl_inside_subgraph_state_and_resume(a, coord):
    tid = thread(a, coord)
    evs = sse(a, tid, {"assistant_id": coord, "input": {"messages": [{"type": "human", "content": "delegate publish the weekly summary"}]},
                       "stream_mode": ["updates", "values"], "stream_subgraphs": True})
    root_int = next(d for e, d in evs if e == "updates" and "__interrupt__" in d)["__interrupt__"][0]
    assert root_int["ns"][0].startswith("tools:") and root_int["value"]["action_requests"][0]["name"] == "content_publish"
    assert any(e.startswith("updates|tools:") and "__interrupt__" in d for e, d in evs), "subgraph-level interrupt event"
    st = a.get(f"/api/v1/threads/{tid}/state", params={"subgraphs": "true"}).json()
    t0 = st["tasks"][0]; sub = t0["state"]
    assert t0["checkpoint_ns"] == root_int["ns"][0] and sub["checkpoint"]["checkpoint_ns"] == t0["checkpoint_ns"]
    assert sub["next"] == ["HumanInTheLoopMiddleware.after_model"] and sub["interrupts"][0]["id"] == root_int["id"]
    assert [m["type"] for m in sub["values"]["messages"]] == ["human", "ai"] and sub["values"]["messages"][1]["tool_calls"][0]["name"] == "content_publish"
    assert "state" not in a.get(f"/api/v1/threads/{tid}/state").json()["tasks"][0], "subgraphs=false -> no nested state"
    one = a.post(f"/api/v1/threads/{tid}/state/checkpoint", json={"checkpoint": {"checkpoint_ns": t0["checkpoint_ns"]}}).json()
    assert one["checkpoint"]["checkpoint_ns"] == t0["checkpoint_ns"] and one["next"] == sub["next"]
    # resume from the parent (official Command(resume)), decision recorded in the Inbox row (G7)
    evs = sse(a, tid, {"assistant_id": coord, "command": {"resume": {"decisions": [{"type": "approve"}]}}, "stream_mode": ["updates", "values"], "stream_subgraphs": True})
    assert any(e.startswith("updates|tools:") and "content_publish" in json.dumps(d) for e, d in evs)
    fin = a.get(f"/api/v1/threads/{tid}/state", params={"subgraphs": "true"}).json()
    assert fin["next"] == [] and fin["tasks"] == [] and "published" in fin["values"]["messages"][-1]["content"]
    with psycopg.connect("postgresql://aos:aoslocal@127.0.0.1:5432/agentos") as c:
        assert c.execute("SELECT status FROM approvals WHERE thread_id=%s ORDER BY id DESC LIMIT 1", (tid,)).fetchone()[0] == "approved"
