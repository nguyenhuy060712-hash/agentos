"""GĐ8.7 (23/10/2026): create an agent by chatting (app/builder_chat.py: POST /api/builder/chat, /api/builder/chat/create; page /builder-chat).
The fake LLM (overlay/fake_llm.py `_builder_turn`) finds the covered topics by keywords in the first brief, asks one question per missing topic,
then returns a BuilderTurn draft; "[nhieu-cau]" keeps asking (the endpoint must stop at 5 questions via its BUILDER_ERROR retry) and
"[tool:x]" puts tool x into the draft (unknown tools must be dropped). Local server :8020, fake LLM, $0, no network."""
import os, uuid
import httpx, psycopg, pytest
B = "http://127.0.0.1:8020"; ADMIN = ("moza", "phase0-local")
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
SUF = uuid.uuid4().hex[:6]; DU = ("demobc" + SUF, "demo-bc-pass-" + SUF)
ANSWERS = ["Nhân viên và khách hàng của công ty", "Dùng tài liệu chính sách và bảng giá", "Chỉ trả lời, không tự gửi gì", "Giọng thân thiện, lịch sự", "Thêm nữa thì không"]
BRIEFS = [   # (brief, expected name, channel, skills)
    ("Agent bán hàng tư vấn sản phẩm cho khách hàng qua Zalo, dùng bảng giá mới nhất, giọng thân thiện", "Trợ lý bán hàng", "zalo", []),
    ("Trợ lý CSKH trả lời khiếu nại và câu hỏi bảo hành", "Trợ lý chăm sóc khách hàng", "zalo", []),
    ("Agent kế toán lập báo cáo công nợ hằng tuần từ file Excel cho phòng tài chính", "Trợ lý kế toán", "none", ["xlsx", "pdf"]),
    ("Trợ lý nhân sự soạn hợp đồng lao động và trả lời câu hỏi về nghỉ phép", "Trợ lý nhân sự", "telegram", ["docx"]),
    ("Trợ lý nội bộ trả lời quy trình công ty cho nhân viên mới", "Trợ lý nội bộ", "slack", []),
]

@pytest.fixture(scope="module")
def a():
    with httpx.Client(base_url=B, auth=ADMIN, timeout=90) as c: yield c

@pytest.fixture(scope="module")
def created():
    ids = []
    yield ids
    with httpx.Client(base_url=B, auth=ADMIN, timeout=90) as c:
        for i in ids:
            for cp in c.get(f"/api/agents/{i}/caps").json().get("caps", []): c.delete(f"/api/gateway/caps/{cp['id']}")
            c.delete(f"/api/agents/{i}")

def converse(c, brief):
    """Answer every question with the next canned answer until a draft comes back. Returns (draft response, number of questions)."""
    msgs = [{"role": "user", "content": brief}]; n = 0
    for _ in range(7):
        r = c.post("/api/builder/chat", json={"messages": msgs}); assert r.status_code == 200, r.text
        j = r.json()
        if j["type"] == "draft": return j, n
        assert j["type"] == "question" and j["question"] and j["asked"] == n + 1
        msgs += [{"role": "assistant", "content": j["question"]}, {"role": "user", "content": ANSWERS[n]}]; n += 1
    raise AssertionError("no draft after 7 turns")

@pytest.mark.parametrize("brief,name,channel,skills", BRIEFS, ids=["ban-hang", "cskh", "ke-toan", "nhan-su", "noi-bo"])
def test_brief_gives_valid_draft_and_agent(a, created, brief, name, channel, skills):
    d, n = converse(a, brief)
    assert n <= 5
    dr = d["draft"]; cfg = d["config"]
    assert dr["name"] == name and dr["channel"] == channel and dr["skills"] == skills
    assert dr["instructions"].startswith("Bạn là") and cfg["system_prompt"] == dr["instructions"]   # Vietnamese brief -> Vietnamese instructions
    assert dr["tools"] == ["local:tra_cuu_kien_thuc"] and cfg["tools"] == dr["tools"]
    assert dr["model"] and cfg["model"] == dr["model"]
    assert dr["cost_cap_usd_month"] == 2.0 and cfg["budget_usd_day"] is None   # GĐ9.P1: a real monthly agent cap, not month/30 as a daily budget
    assert cfg["skills"] == [f"builtin:{s}" for s in skills] and cfg["sandbox"] == bool(skills)
    assert (d["channel_hint"] is None) == (channel == "none")
    dr["name"] = name + " " + SUF; dr["cost_cap_usd_month"] = 3   # edited on the summary card
    r = a.post("/api/builder/chat/create", json={"draft": dr}); assert r.status_code == 200, r.text
    j = r.json(); created.append(j["id"])
    assert j["test_url"] == f"builder?id={j['id']}&tab=test" and j["version"] >= 1
    ag = a.get(f"/api/agents/{j['id']}").json()
    conf = ag.get("config") or (ag.get("versions") or [{}])[0].get("config") or ag
    assert conf["name"] == name + " " + SUF and conf["budget_usd_day"] is None and conf["system_prompt"] == dr["instructions"]
    caps = a.get(f"/api/agents/{j['id']}/caps").json()["caps"]; assert [(c["scope"], c["period"], c["limit_usd"], c["action"]) for c in caps] == [("agent", "month", 3.0, "block")]

def test_questions_skip_answered_topics(a):
    r = a.post("/api/builder/chat", json={"messages": [{"role": "user", "content": BRIEFS[0][0]}]}); j = r.json()
    assert j["type"] == "question" and {"job", "audience", "data", "tone"} <= set(j["answered"])   # only "actions" is still open
    assert "làm gì thay" in j["question"]
    d, n = converse(a, BRIEFS[0][0]); assert n == 1 and d["type"] == "draft"

def test_never_more_than_five_questions(a):
    d, n = converse(a, "Trợ lý bán hàng [nhieu-cau]")   # the fake would keep asking forever
    assert n == 5 and d["type"] == "draft"

def test_unknown_tools_dropped_with_note(a, created):
    d, _ = converse(a, "Trợ lý nội bộ cho nhân viên, dùng tài liệu quy trình, chỉ trả lời, giọng lịch sự [tool:web:bogus_search]")
    assert d["config"]["tools"] == ["local:tra_cuu_kien_thuc"] and any("web:bogus_search" in w for w in d["warnings"])
    dr = dict(d["draft"], tools=["local:tra_cuu_kien_thuc", "x:not_a_tool"], model="no-such-model", name="Bogus " + SUF)
    r = a.post("/api/builder/chat/create", json={"draft": dr}); assert r.status_code == 200, r.text
    created.append(r.json()["id"])
    assert r.json()["draft"]["tools"] == ["local:tra_cuu_kien_thuc"] and r.json()["draft"]["model"] != "no-such-model"
    assert any("x:not_a_tool" in w for w in r.json()["warnings"])

def test_input_validation(a):
    assert a.post("/api/builder/chat", json={"messages": []}).status_code == 422
    assert a.post("/api/builder/chat", json={"messages": [{"role": "assistant", "content": "hi"}]}).status_code == 422
    assert a.post("/api/builder/chat", json={"messages": [{"role": "system", "content": "x"}]}).status_code == 422
    assert a.post("/api/builder/chat/create", json={"draft": {"name": "x"}}).status_code == 422                      # no instructions
    assert a.post("/api/builder/chat/create", json={"draft": {"name": "x", "instructions": "y", "channel": "fax"}}).status_code == 422

def test_feature_builder_is_tagged_and_capped(a):
    with psycopg.connect(PG, autocommit=True) as c:
        before = c.execute("SELECT count(*) FROM gateway_usage WHERE feature='builder'").fetchone()[0]
    assert a.post("/api/builder/chat", json={"messages": [{"role": "user", "content": BRIEFS[1][0]}]}).status_code == 200
    with psycopg.connect(PG, autocommit=True) as c:
        assert c.execute("SELECT count(*) FROM gateway_usage WHERE feature='builder'").fetchone()[0] > before   # usage row tagged feature=builder
        c.execute("INSERT INTO gateway_usage(ts,model,input_tokens,output_tokens,cost_usd,status,feature) VALUES(now(),'gemini-3.8-flash',0,0,0.5,'t87','builder')")
    cap = a.post("/api/gateway/caps", json={"scope": "feature", "target": "builder", "period": "total", "limit_usd": 0.01}); assert cap.status_code == 200, cap.text
    try:
        r = a.post("/api/builder/chat", json={"messages": [{"role": "user", "content": BRIEFS[1][0]}]})
        assert r.status_code == 402 and ("Tạo agent" in r.json()["detail"] or "Create agent" in r.json()["detail"]), r.text   # FEATURE_VI label (EN UI translates it)
    finally:
        a.delete(f"/api/gateway/caps/{cap.json()['id']}")
        with psycopg.connect(PG, autocommit=True) as c: c.execute("DELETE FROM gateway_usage WHERE status='t87'")
    assert a.post("/api/builder/chat", json={"messages": [{"role": "user", "content": BRIEFS[1][0]}]}).status_code == 200

def test_demo_blocked(a):
    r = a.post("/api/admin/users", json={"username": DU[0], "password": DU[1], "role": "viewer", "demo": True, "daily_cap_usd": 0.05, "demo_agents": [2]}); assert r.status_code == 200, r.text
    uid = r.json()["id"]
    p = a.post("/api/admin/demo-workspace", json={"user_id": uid, "name": "Demo bc " + SUF, "source_agents": [2], "daily_cap_usd": 2.0}); assert p.status_code == 200, p.text
    try:
        with httpx.Client(base_url=B, auth=DU, timeout=90) as d:
            assert d.post("/api/builder/chat", json={"messages": [{"role": "user", "content": BRIEFS[0][0]}]}).status_code == 403
            assert d.post("/api/builder/chat/create", json={"draft": {"name": "x", "instructions": "y"}}).status_code == 403
    finally:
        with psycopg.connect(PG, autocommit=True) as c: c.execute("UPDATE users SET disabled=true WHERE id=%s", (uid,))

def test_page_served(a):
    r = a.get("/builder-chat"); assert r.status_code == 200 and "api/builder/chat" in r.text
