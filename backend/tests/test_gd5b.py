"""GĐ5b (21/10/2026): canvas (agent tool + versions + user edits + conflicts), code run (fake gVisor runner locally: stdout, charts ->
/api/files, timeout, role gate, admin switch), schedules (agent_crons: permissions, locked P0-1 rows never re-enabled/run/deleted, cron
limits), read-only share links (token hash, expiry, revoke, same-workspace login, sanitised view), @mention delegation forwards images,
/favicon.ico. Local server :8020, fake LLM => $0, no network."""
import base64, json, os, re, uuid
import httpx, psycopg, pytest
from test_fixes import B, ADMIN, VIEWER, a, restore, _clean  # noqa: F401  (fixtures)

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
G = "agent-2"; H = "agent-1"
EDITOR = ("editor5b", "phase0-editor-local")
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg==")

@pytest.fixture(scope="module")
def v():
    with httpx.Client(base_url=B, auth=VIEWER, timeout=60) as c: yield c

@pytest.fixture(scope="module")
def e(a):
    if not any(u["username"] == EDITOR[0] for u in a.get("/api/admin/users").json()):
        assert a.post("/api/admin/users", json={"username": EDITOR[0], "password": EDITOR[1], "role": "editor"}).status_code == 200
    with httpx.Client(base_url=B, auth=EDITOR, timeout=60) as c: yield c

@pytest.fixture(scope="module", autouse=True)
def gd5b_agent(a):
    cfg = a.get("/api/agents/2").json()["config"]; before = json.loads(json.dumps(cfg))
    cfg["tools"] = sorted(set(cfg.get("tools", [])) | {"local:canvas", "local:canvas_doc"}); cfg["sandbox"] = True; cfg["rpm_limit"] = 300
    assert a.put("/api/agents/2", json=cfg).status_code == 200
    yield
    a.put("/api/agents/2", json=before)
    a.put("/api/chat/code/settings", json={"enabled": True, "roles": ["admin", "editor"]})

def chat_thread(c, g=G, **md): return c.post("/api/v1/threads", json={"metadata": {"source": "chat", "graph_id": g, **md}}).json()["thread_id"]
def run(c, tid, text, g=G):
    r = c.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": g, "input": {"messages": [{"type": "human", "content": text}]}})
    assert r.status_code == 200, r.text
    return r
def msgs(c, tid): return c.get(f"/api/v1/threads/{tid}/state").json()["values"]["messages"]
def tool_msgs(ms, name): return [m for m in ms if m.get("type") == "tool" and m.get("name") == name]

# ---------------------------------------------------------------- 1. canvas
def test_canvas_agent_writes_user_edits_agent_reads_back(a, v):
    tid = chat_thread(a); run(a, tid, "Viết canvas kế hoạch nội dung")
    res = tool_msgs(msgs(a, tid), "canvas")[-1]["content"]
    doc, ver = re.search(r"\[canvas:([0-9a-f]{12}):(\d+)\]", res).groups(); assert ver == "1"
    lst = a.get("/api/chat/canvas", params={"thread_id": tid}).json()["items"]
    assert [x["doc_id"] for x in lst] == [doc] and lst[0]["kind"] == "document" and lst[0]["language"] == "markdown"
    d = a.get(f"/api/chat/canvas/{doc}").json(); assert d["version"] == 1 and "Lịch đăng" in d["content"] and d["can_edit"]
    r = a.post(f"/api/chat/canvas/{doc}", json={"content": d["content"] + "\n- Anh sửa: thêm livestream", "base_version": 1})
    assert r.status_code == 200 and r.json()["version"] == 2
    assert a.post(f"/api/chat/canvas/{doc}", json={"content": "cũ", "base_version": 1}).status_code == 409   # stale edit
    run(a, tid, f"Sửa canvas {doc} thêm KPI")
    d3 = a.get(f"/api/chat/canvas/{doc}").json()
    assert d3["version"] == 3 and "Anh sửa: thêm livestream" in d3["content"] and "KPI" in d3["content"]   # agent read the user's edit first
    assert [x["author"] for x in d3["versions"]] == ["agent", "user", "agent"]
    assert a.get(f"/api/chat/canvas/{doc}/v/1").json()["content"] == d["content"]
    # viewer: not their thread -> 404; cannot edit at all
    assert v.get(f"/api/chat/canvas/{doc}").status_code == 404
    assert v.post(f"/api/chat/canvas/{doc}", json={"content": "x", "base_version": 3}).status_code == 403

def test_canvas_agent_invented_doc_id_creates_a_new_doc(a, v):
    """GĐ7.R (real Gemini): the model passed its own doc_id for a NEW report -> must be saved as a new doc, not refused.
    An id that belongs to another thread is still refused."""
    tid = chat_thread(a); run(a, tid, "Sửa canvas oss_report_2026 thêm KPI")
    res = tool_msgs(msgs(a, tid), "canvas")[-1]["content"]
    doc = re.search(r"\[canvas:([0-9a-f]{12}):1\]", res).group(1)
    assert [x["doc_id"] for x in a.get("/api/chat/canvas", params={"thread_id": tid}).json()["items"]] == [doc]
    other = chat_thread(a); run(a, other, f"Sửa canvas {doc} thêm KPI")
    assert "không có tài liệu này" in tool_msgs(msgs(a, other), "canvas")[-1]["content"]
    assert a.get("/api/chat/canvas", params={"thread_id": other}).json()["items"] == []

def test_canvas_limits(a):
    tid = chat_thread(a); run(a, tid, "Code canvas tính tổng")
    doc = re.search(r"\[canvas:([0-9a-f]{12})", tool_msgs(msgs(a, tid), "canvas")[-1]["content"]).group(1)
    d = a.get(f"/api/chat/canvas/{doc}").json(); assert d["kind"] == "code" and d["language"] == "python"
    assert a.post(f"/api/chat/canvas/{doc}", json={"content": "x" * 200_001, "base_version": 1}).status_code == 422
    assert a.get("/api/chat/canvas/nonexistent0").status_code == 404

# ---------------------------------------------------------------- 2. code run
def test_agent_code_run_returns_stdout_and_chart(a):
    tid = chat_thread(a); run(a, tid, "Chạy code vẽ doanh thu")
    out = tool_msgs(msgs(a, tid), "chay_code_python")[-1]["content"]
    assert "status=ok" in out and "Tổng" in out
    fid = re.search(r"\[aos-images:([0-9a-f-]{36})", out).group(1)
    r = a.get(f"/api/files/{fid}"); assert r.status_code == 200 and r.content.startswith(b"\x89PNG")

def test_code_run_endpoint_gate_timeout_error(a, e, v):
    s = a.get("/api/chat/code/settings").json(); assert s["can_run"] and s["limits"]["network"] is False
    r = e.post("/api/chat/code/run", json={"code": "print(1+1)"}); assert r.status_code == 200 and r.json()["status"] == "ok", r.text
    assert a.post("/api/chat/code/run", json={"code": "while True: pass"}).json()["status"] == "timeout"
    r = a.post("/api/chat/code/run", json={"code": "raise ValueError('x')"}).json(); assert r["status"] == "error" and "ValueError" in r["stderr"]
    assert v.post("/api/chat/code/run", json={"code": "print(1)"}).status_code == 403                      # viewer
    assert e.put("/api/chat/code/settings", json={"enabled": False}).status_code == 403                   # only admin switches it
    assert a.put("/api/chat/code/settings", json={"enabled": True, "roles": ["admin"]}).status_code == 200
    assert e.post("/api/chat/code/run", json={"code": "print(1)"}).status_code == 403                      # editor no longer allowed
    assert a.put("/api/chat/code/settings", json={"enabled": False, "roles": ["admin"]}).status_code == 200
    assert a.post("/api/chat/code/run", json={"code": "print(1)"}).status_code == 403
    tid = chat_thread(a); run(a, tid, "Chạy code lần nữa")
    assert "tắt" in tool_msgs(msgs(a, tid), "chay_code_python")[-1]["content"]                            # the agent tool obeys it too
    assert a.put("/api/chat/code/settings", json={"enabled": True, "roles": ["admin", "editor"]}).status_code == 200

# ---------------------------------------------------------------- 3. schedules
@pytest.fixture()
def locked(a):
    with psycopg.connect(PG, autocommit=True) as c:
        cid = c.execute("INSERT INTO agent_crons(agent_id,name,cron,message,enabled,locked_reason) VALUES(2,'P0-1 test (pytest)','0 3 * * *','x',false,'P0-1 test cron (tắt 20/10)') RETURNING id").fetchone()[0]
    yield cid
    with psycopg.connect(PG, autocommit=True) as c: c.execute("DELETE FROM agent_crons WHERE id=%s", (cid,))

def test_locked_crons_stay_off(a, locked):
    it = next(x for x in a.get("/api/chat/schedules").json()["items"] if x["id"] == locked)
    assert it["locked"] and not it["enabled"] and not it["can_edit"] and it["next_run"] is None
    assert a.patch(f"/api/chat/schedules/{locked}", json={"enabled": True}).status_code == 409
    assert a.delete(f"/api/chat/schedules/{locked}").status_code == 409
    assert a.patch(f"/api/agent_crons/{locked}", params={"enabled": "true"}).status_code == 409             # old studio API too
    assert a.post(f"/api/agent_crons/{locked}/run_now").status_code == 409
    with psycopg.connect(PG) as c: assert c.execute("SELECT enabled FROM agent_crons WHERE id=%s", (locked,)).fetchone()[0] is False

def test_schedule_crud_and_permissions(a, e, v):
    assert v.post("/api/chat/schedules", json={"agent_id": G, "name": "x", "cron": "0 9 * * 1", "message": "hi"}).status_code == 403
    assert e.post("/api/chat/schedules", json={"agent_id": G, "name": "x", "cron": "0 25 * * *", "message": "hi"}).status_code in (400, 422)
    assert e.post("/api/chat/schedules", json={"agent_id": G, "name": "x", "cron": "* * * * *", "message": "hi"}).status_code == 400   # too often
    assert e.post("/api/chat/schedules", json={"agent_id": G, "name": "x", "cron": "0 9 31 2 *", "message": "hi"}).status_code == 400   # never
    r = e.post("/api/chat/schedules", json={"agent_id": G, "name": "Báo cáo sáng thứ 2", "cron": "0  9 * * 1", "message": "Tóm tắt tuần"})
    assert r.status_code == 200, r.text
    s = r.json(); assert s["cron"] == "0 9 * * 1" and s["enabled"] and s["can_edit"] and s["next_run"]
    try:
        other = next(x for x in v.get("/api/chat/schedules").json()["items"] if x["id"] == s["id"]); assert not other["can_edit"]
        assert e.patch(f"/api/chat/schedules/{s['id']}", json={"enabled": False}).json()["enabled"] is False
        assert v.patch(f"/api/chat/schedules/{s['id']}", json={"enabled": True}).status_code == 403
        own_admin = a.post("/api/chat/schedules", json={"agent_id": G, "name": "admin's", "cron": "30 8 * * *", "message": "x"}).json()
        assert e.patch(f"/api/chat/schedules/{own_admin['id']}", json={"enabled": False}).status_code == 403    # editor: own rows only
        assert a.delete(f"/api/chat/schedules/{own_admin['id']}").status_code == 200
    finally:
        assert e.delete(f"/api/chat/schedules/{s['id']}").status_code == 200

# ---------------------------------------------------------------- 4. share links
def test_share_link_sanitised_expiry_revoke(a, v, e):
    tid = chat_thread(a)
    a.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": G, "input": {"messages": [{"type": "human", "content":
        '<attachment name="bí mật.txt">\nmật khẩu: hunter2 SECRETBODY\n</attachment>\nPhân bổ ngân sách giúp anh'}]}})
    run(a, tid, "Chạy code tính tổng")
    r = a.post("/api/chat/shares", json={"thread_id": tid, "days": 7}); assert r.status_code == 200, r.text
    tok = r.json()["token"]; sid, secret = tok.split(".", 1)
    with psycopg.connect(PG) as c:
        h = c.execute("SELECT token_hash FROM chat_shares WHERE id=%s", (sid,)).fetchone()[0]
    assert secret not in h and len(h) == 64
    view = v.get(f"/api/chat/share/{tok}")             # another user of the same workspace (cannot open the thread itself)
    assert view.status_code == 200 and v.get(f"/api/v1/threads/{tid}").status_code == 404
    body = json.dumps(view.json(), ensure_ascii=False)
    assert "Phân bổ" in body and "[Tệp đính kèm: bí mật.txt]" in body and "SECRETBODY" not in body and "hunter2" not in body
    assert '"present"' in body                           # render-only UI kept
    assert "chay_code_python" not in body and "import matplotlib" not in body and "status=ok" not in body   # tool args/results dropped
    assert httpx.get(f"{B}/api/chat/share/{tok}").status_code == 401                                     # login required (no domain yet)
    assert v.get(f"/api/chat/share/{sid}.wrong").status_code == 404
    assert v.post("/api/chat/shares", json={"thread_id": tid}).status_code in (403, 404)
    assert e.delete(f"/api/chat/shares/{sid}").status_code in (403, 404)                                 # not the creator
    lst = a.get("/api/chat/shares", params={"thread_id": tid}).json()["items"]; assert lst[0]["views"] == 1 and "token_hash" not in lst[0]
    assert a.delete(f"/api/chat/shares/{sid}").status_code == 200
    assert v.get(f"/api/chat/share/{tok}").status_code == 410
    tok2 = a.post("/api/chat/shares", json={"thread_id": tid, "days": 1}).json()["token"]
    with psycopg.connect(PG, autocommit=True) as c: c.execute("UPDATE chat_shares SET expires=now() - interval '1 minute' WHERE id=%s", (tok2.split(".")[0],))
    assert v.get(f"/api/chat/share/{tok2}").status_code == 410
    assert a.post("/api/chat/shares", json={"thread_id": tid, "days": 31}).status_code == 422

# ---------------------------------------------------------------- 5. small fixes
def test_delegate_forwards_images(a):
    up = a.post("/api/files", files={"file": ("a.png", PNG, "image/png")}).json()
    tid = chat_thread(a); run(a, tid, "Xin chào")
    content = [{"type": "text", "text": "@Trợ lý xem ảnh này"}, {"type": "image", "url": up["url"], "mime_type": "image/png"}]
    r = a.post("/api/chat/delegate", json={"thread_id": tid, "agent_id": H, "text": "@Trợ lý xem ảnh này", "content": content})
    assert r.status_code == 200, r.text
    child = r.json()["child_thread"]
    first = msgs(a, child)[0]["content"]
    assert isinstance(first, list) and any(b.get("type") == "image" and b.get("url") == up["url"] for b in first)
    hm = [m for m in r.json()["values"]["messages"] if m["type"] == "human"][-1]
    assert isinstance(hm["content"], list) and any(b.get("type") == "image" for b in hm["content"])

def test_favicon_and_login_icon():
    r = httpx.get(f"{B}/favicon.ico"); assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert 'rel=icon href="/favicon.ico"' in httpx.get(f"{B}/login").text
