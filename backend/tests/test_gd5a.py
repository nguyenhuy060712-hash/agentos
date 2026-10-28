"""GĐ5a (21/10/2026): memory API (own/shared, permissions), thread full-text search (unaccent, RLS, 10k perf), @mention delegation,
ask-for-info form tool (interrupt -> resume with answers). Local server :8020, fake LLM => $0, no network."""
import json, os, time, uuid
import httpx, psycopg, pytest
from test_fixes import B, ADMIN, VIEWER, a, restore, _clean  # noqa: F401  (fixtures)

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
G = "agent-2"

@pytest.fixture(scope="module")
def v():
    with httpx.Client(base_url=B, auth=VIEWER, timeout=60) as c: yield c

@pytest.fixture(scope="module", autouse=True)
def gd5_agent(a):
    cfg = a.get("/api/agents/2").json()["config"]; before = json.loads(json.dumps(cfg))
    cfg["tools"] = sorted(set(cfg.get("tools", [])) | {"local:hoi_thong_tin"}); cfg["long_term_memory"] = True
    assert a.put("/api/agents/2", json=cfg).status_code == 200
    yield
    a.put("/api/agents/2", json=before)

def chat_thread(c, g=G, **md):
    return c.post("/api/v1/threads", json={"metadata": {"source": "chat", "graph_id": g, **md}}).json()["thread_id"]

def run(c, tid, text, g=G, **extra):
    r = c.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": g, "input": {"messages": [{"type": "human", "content": text}]}, **extra})
    assert r.status_code == 200, r.text
    return r

def state(c, tid): return c.get(f"/api/v1/threads/{tid}/state").json()
def tool_msgs(ms, name): return [m for m in ms if m.get("type") == "tool" and m.get("name") == name]

# ---------------------------------------------------------------- 1. memory
def test_memory_saved_listed_edited_forgotten(a, v):
    tid = chat_thread(a); run(a, tid, "Ghi nhớ: anh Huy thích báo cáo ngắn gọn, có số liệu")
    tm = tool_msgs(state(a, tid)["values"]["messages"], "luu_ghi_nho")
    assert tm and "[mem:" in tm[0]["content"], tm
    key = tm[0]["content"].split("[mem:")[1].rstrip("]")
    items = a.get("/api/chat/memories", params={"agent_id": G}).json()["items"]
    assert len([x for x in a.get("/api/chat/memories").json()["items"] if x["key"] == key]) == 1   # own namespace only, not again as "shared"
    it = next(x for x in items if x["key"] == key)
    assert it["agent_id"] == G and "báo cáo ngắn gọn" in it["text"] and it["shared"] is False
    # another user never sees it, cannot edit/delete it (own namespace only)
    assert all(x["key"] != key for x in v.get("/api/chat/memories").json()["items"])
    assert v.patch("/api/chat/memories", json={"agent_id": G, "key": key, "text": "x"}).status_code == 403      # viewer: no edit
    assert v.delete("/api/chat/memories", params={"agent_id": G, "key": key}).status_code == 404               # not in viewer's namespace
    r = a.patch("/api/chat/memories", json={"agent_id": G, "key": key, "text": "Anh Huy thích báo cáo 1 trang"}); assert r.status_code == 200, r.text
    assert next(x for x in a.get("/api/chat/memories").json()["items"] if x["key"] == key)["text"] == "Anh Huy thích báo cáo 1 trang"
    assert a.delete("/api/chat/memories", params={"agent_id": G, "key": key}).status_code == 200
    assert all(x["key"] != key for x in a.get("/api/chat/memories").json()["items"])
    assert a.delete("/api/chat/memories", params={"agent_id": G, "key": key}).status_code == 404

def test_memory_shared_is_admin_only(a, v):
    k = "pytest" + uuid.uuid4().hex[:6]
    assert a.post("/api/store/items", json={"ns": "agent/2/memories", "key": k, "text": "Ghi nhớ chung của agent"}).status_code == 200
    try:
        assert any(x["key"] == k and x["shared"] for x in a.get("/api/chat/memories").json()["items"])
        assert all(x["key"] != k for x in v.get("/api/chat/memories").json()["items"])
        assert v.delete("/api/chat/memories", params={"agent_id": G, "key": k, "shared": True}).status_code == 403
        assert a.patch("/api/chat/memories", json={"agent_id": G, "key": k, "text": "sửa", "shared": True}).status_code == 200
    finally:
        a.delete("/api/store/items", params={"ns": "agent/2/memories", "key": k})

def test_memory_toggle_admin_only(a, v):
    ags = {x["agent_id"]: x for x in a.get("/api/chat/memory/agents").json()}
    assert ags[G]["enabled"] is True
    assert v.put(f"/api/chat/memory/agents/{G}", json={"enabled": False}).status_code == 403
    r = a.put(f"/api/chat/memory/agents/{G}", json={"enabled": False}); assert r.status_code == 200 and r.json()["enabled"] is False
    assert {x["agent_id"]: x for x in a.get("/api/chat/memory/agents").json()}[G]["enabled"] is False
    assert a.put(f"/api/chat/memory/agents/{G}", json={"enabled": True}).status_code == 200

def test_memory_bad_agent(a):
    assert a.patch("/api/chat/memories", json={"agent_id": "nope", "key": "x", "text": "y"}).status_code == 400
    assert a.delete("/api/chat/memories", params={"agent_id": "agent-999999", "key": "x"}).status_code == 404

# ---------------------------------------------------------------- 2. full-text search
def test_search_unaccent_title_and_body(a, v):
    tag = uuid.uuid4().hex[:6]
    tid = chat_thread(a, title=f"Kế hoạch Tết {tag}")
    run(a, tid, f"Báo cáo doanh thu đặc biệt quý ba mã{tag}")
    for q in (f"bao cao dac biet ma{tag}", f"BÁO CÁO đặc biệt mã{tag}", f"doanh ma{tag[:4]}"):   # accents/case folded, prefix match
        hits = a.post("/api/chat/threads/search", json={"q": q}).json()["items"]
        h = next((x for x in hits if x["thread_id"] == tid), None)
        assert h, (q, hits[:3])
        assert "đặc biệt" in h["snippet"] and h["agent_id"] == G
    # title (renamed) is indexed too
    assert a.patch(f"/api/v1/threads/{tid}", json={"metadata": {"title": f"Đổi tên {tag} xuân"}}).status_code == 200
    hits = a.post("/api/chat/threads/search", json={"q": f"doi ten {tag} xuan"}).json()["items"]
    assert hits and hits[0]["thread_id"] == tid and hits[0]["title"].startswith("Đổi tên")
    # RLS: another user cannot find it
    assert all(x["thread_id"] != tid for x in v.post("/api/chat/threads/search", json={"q": f"ma{tag}"}).json()["items"])
    # non-chat threads are not indexed
    t2 = a.post("/api/v1/threads", json={"metadata": {"source": "pytest", "graph_id": G}}).json()["thread_id"]
    run(a, t2, f"Báo cáo ẩn ma{tag}")
    assert all(x["thread_id"] != t2 for x in a.post("/api/chat/threads/search", json={"q": f"an ma{tag}"}).json()["items"])

def test_search_self_heals_missing_or_stale_index(a):
    """A run-end index that failed (or ran in another process) is repaired by the next search (missing row / older than the thread)."""
    tag = uuid.uuid4().hex[:6]
    tid = chat_thread(a, title=f"Tự sửa {tag}")
    run(a, tid, f"Nội dung kiểm tra chỉ mục ma{tag}")
    with psycopg.connect(PG, autocommit=True) as c: c.execute("DELETE FROM chat_fts WHERE thread_id=%s", (tid,))
    hits = a.post("/api/chat/threads/search", json={"q": f"kiem tra chi muc ma{tag}"}).json()["items"]
    assert any(x["thread_id"] == tid for x in hits), hits[:3]
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute("UPDATE chat_fts SET body='', updated_at=now() - interval '1 hour' WHERE thread_id=%s", (tid,))
    hits = a.post("/api/chat/threads/search", json={"q": f"noi dung ma{tag}"}).json()["items"]
    assert any(x["thread_id"] == tid for x in hits), hits[:3]

def test_search_rejects_garbage(a):
    assert a.post("/api/chat/threads/search", json={"q": "!!! ''' &&"}).json()["items"] == []
    assert a.post("/api/chat/threads/search", json={"q": ""}).status_code == 422

def test_search_10k_threads_under_500ms(a):
    tag = "perf" + uuid.uuid4().hex[:6]
    words = ["báo", "cáo", "doanh", "thu", "khách", "hàng", "chiến", "dịch", "quảng", "cáo", "đơn", "hàng", "kế", "hoạch", "tết"]
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute("""INSERT INTO lg_threads(thread_id, metadata, workspace_id)
                     SELECT %s || '-' || g, jsonb_build_object('source','chat','title', 'Chủ đề ' || g), 1 FROM generate_series(1,10000) g""", (tag,))
        c.execute("""INSERT INTO chat_fts(thread_id, title, body)
                     SELECT %s || '-' || g, 'Chủ đề ' || g, (SELECT string_agg((%s::text[])[1 + ((g * 7 + i * 13) %% 15)], ' ') FROM generate_series(1,60) i)
                     FROM generate_series(1,10000) g""", (tag, words))
        c.execute("ANALYZE chat_fts")
    try:
        ts = []
        for q in ("bao cao doanh thu", "chien dich tet", "khach hang"):
            t0 = time.perf_counter(); r = a.post("/api/chat/threads/search", json={"q": q, "limit": 20}); ts.append((time.perf_counter() - t0) * 1000)
            assert r.status_code == 200 and len(r.json()["items"]) == 20
        print("search ms", [round(x) for x in ts])
        assert max(ts) < 500, ts
    finally:
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute("DELETE FROM lg_threads WHERE thread_id LIKE %s", (tag + "-%",))

# ---------------------------------------------------------------- 3. @mention delegation
def test_delegate_appends_answer_from_mentioned_agent(a):
    tid = chat_thread(a); run(a, tid, "Xin chào")
    mid = "client-" + uuid.uuid4().hex[:8]
    text = ":agent[Trợ lý nhà]{name=agent-1} kiểm tra đèn phòng khách"
    r = a.post("/api/chat/delegate", json={"thread_id": tid, "agent_id": "agent-1", "text": text, "message_id": mid}); assert r.status_code == 200, r.text
    d = r.json(); assert d["status"] == "success" and d["agent_id"] == "agent-1"
    ms = state(a, tid)["values"]["messages"]
    assert ms[-2]["id"] == mid and ms[-2]["content"] == text and ms[-2]["additional_kwargs"]["delegated_to"] == "agent-1"
    assert ms[-1]["type"] == "ai" and ms[-1]["additional_kwargs"]["delegated_from"] == "agent-1"
    assert "kiểm tra đèn phòng khách" in ms[-1]["content"] and ":agent[" not in ms[-1]["content"]   # child got plain "@Trợ lý nhà"
    st = state(a, tid); assert not st["next"] and not st.get("interrupts")
    child = a.get(f"/api/v1/threads/{d['child_thread']}").json()
    assert child["metadata"]["source"] == "delegation" and child["metadata"]["parent_thread"] == tid and child["agent_id"] == 1
    run(a, tid, "Cảm ơn")                                   # the thread keeps working with its own agent
    assert state(a, tid)["values"]["messages"][-1]["content"].startswith("Echo: Cảm ơn")

def test_delegate_guards(a, v):
    fresh = chat_thread(a)
    assert a.post("/api/chat/delegate", json={"thread_id": fresh, "agent_id": "agent-1", "text": "x"}).status_code == 409   # unbound: route instead
    tid = chat_thread(a); run(a, tid, "Xin chào")
    assert a.post("/api/chat/delegate", json={"thread_id": tid, "agent_id": G, "text": "x"}).status_code == 409             # same agent
    assert v.post("/api/chat/delegate", json={"thread_id": tid, "agent_id": "agent-1", "text": "x"}).status_code == 403    # viewer

def test_route_unbound_thread_to_mentioned_agent(a):
    """Route: an empty thread mentioning another agent simply runs with that agent (the UI sends it as assistant_id)."""
    tid = chat_thread(a, g=G)
    run(a, tid, ":agent[Trợ lý nhà]{name=agent-1} chào", g="agent-1")
    assert a.get(f"/api/v1/threads/{tid}").json()["agent_id"] == 1

# ---------------------------------------------------------------- 4. ask-for-info
def _ask(a):
    tid = chat_thread(a); run(a, tid, "Hãy hỏi thông tin để lên kế hoạch chiến dịch")
    st = state(a, tid); ints = st.get("interrupts") or [i for t in st.get("tasks", []) for i in (t.get("interrupts") or [])]
    return tid, st, ints

def test_ask_interrupt_shape_no_inbox_row(a):
    tid, st, ints = _ask(a)
    assert ints and ints[0]["value"]["type"] == "ask_user", ints
    val = ints[0]["value"]; call = [c for m in st["values"]["messages"] if m.get("type") == "ai" for c in (m.get("tool_calls") or []) if c["name"] == "hoi_thong_tin"][0]
    assert val["tool_call_id"] == call["id"]
    kinds = {f["name"]: f["kind"] for f in val["fields"]}
    assert kinds == {"ngan_sach": "number", "kenh": "choice", "doi_tuong": "multi", "ngay_bat_dau": "date", "ghi_chu": "text", "can_bao_cao": "toggle"}
    assert a.get(f"/api/v1/threads/{tid}").json()["status"] == "interrupted"
    with psycopg.connect(PG) as c:
        assert c.execute("SELECT count(*) FROM approvals WHERE thread_id=%s", (tid,)).fetchone()[0] == 0   # not an Inbox approval

def test_ask_resume_with_answers(a):
    tid, _, _ = _ask(a)
    ans = {"ngan_sach": "20", "kenh": "TikTok", "doi_tuong": ["Sinh viên", "Hacker"], "ngay_bat_dau": "2026-11-01", "can_bao_cao": True, "la": "x"}
    r = a.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": G, "command": {"resume": {"answers": ans}}}); assert r.status_code == 200, r.text
    ms = state(a, tid)["values"]["messages"]; t = tool_msgs(ms, "hoi_thong_tin")[0]["content"]
    assert t.startswith("Người dùng đã trả lời")
    got = json.loads(t.split(":", 1)[1])
    assert got == {"Ngân sách (triệu đồng)": 20.0, "Kênh chính": "TikTok", "Đối tượng": ["Sinh viên"], "Ngày bắt đầu": "2026-11-01", "Gửi báo cáo hằng tuần": True}
    assert ms[-1]["type"] == "ai" and ms[-1]["content"].startswith("Cảm ơn anh")
    assert a.get(f"/api/v1/threads/{tid}").json()["status"] == "idle"

def test_ask_missing_required_and_decline(a):
    tid, _, _ = _ask(a)
    a.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": G, "command": {"resume": {"answers": {"kenh": "Facebook"}}}})
    assert "chưa đủ" in tool_msgs(state(a, tid)["values"]["messages"], "hoi_thong_tin")[0]["content"]
    tid2, _, _ = _ask(a)
    a.post(f"/api/v1/threads/{tid2}/runs/wait", json={"assistant_id": G, "command": {"resume": {"declined": True}}})
    assert "không trả lời" in tool_msgs(state(a, tid2)["values"]["messages"], "hoi_thong_tin")[0]["content"]
