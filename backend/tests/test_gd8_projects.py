"""GĐ8.9 (23/10/2026): chat projects (A9). Local server :8020, fake LLM => $0, no network. The fake echoes the system prompt it received for
'show system prompt' and calls search_project_files for 'tìm trong dự án: <q>'. Throwaway users."""
import json, os, uuid
import httpx, psycopg, pytest
from test_fixes import B, ADMIN, a, restore, _clean  # noqa: F401  (fixtures)

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
PW = "phase0-gd89-local"; G = "agent-2"
INSTR = "Luôn xưng em, gọi anh, và kết thúc bằng câu 'Chúc anh bán đắt hàng' (marker-gd89-instr)"
PHRASE = "Bảng giá cà phê Moza: drip bag 12.000 đồng/gói (marker-gd89-file)"

def _user(a, name, role="editor"):
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": role})
    uid = r.json().get("id") if r.status_code == 200 else next(x["id"] for x in a.get("/api/admin/users").json() if x["username"] == name)
    return uid, httpx.Client(base_url=B, auth=(name, PW), timeout=90)

@pytest.fixture(scope="module")
def u1(a):
    uid, c = _user(a, "gd89_u1"); yield c; c.close(); a.delete(f"/api/admin/users/{uid}")

@pytest.fixture(scope="module")
def u2(a):
    uid, c = _user(a, "gd89_u2"); yield c; c.close(); a.delete(f"/api/admin/users/{uid}")

@pytest.fixture(scope="module")
def mem_agent(a):
    cfg = a.get("/api/agents/2").json()["config"]; before = json.loads(json.dumps(cfg))
    cfg["long_term_memory"] = True
    assert a.put("/api/agents/2", json=cfg).status_code == 200
    yield
    a.put("/api/agents/2", json=before)

def thread(c): return c.post("/api/v1/threads", json={"metadata": {"source": "chat", "graph_id": G}}).json()["thread_id"]
def ask(c, tid, text):
    r = c.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": G, "input": {"messages": [{"type": "human", "content": text}]}})
    assert r.status_code == 200, r.text
    return c.get(f"/api/v1/threads/{tid}/state").json()["values"]["messages"]
def sysprompt(c, tid):
    t = ask(c, tid, "show system prompt")[-1]["content"]; assert t.startswith("SYS<<"), t
    return t
def up(c, name, data): r = c.post("/api/files", files={"file": (name, data, "application/octet-stream")}); assert r.status_code == 200, r.text; return r.json()
def move(c, tid, pid): return c.post("/api/chat/projects/move", json={"thread_id": tid, "project_id": pid})

@pytest.fixture(scope="module")
def proj(u1):
    p = u1.post("/api/chat/projects", json={"name": "Quán cà phê Moza", "instructions": INSTR}); assert p.status_code == 200, p.text
    p = p.json(); f = up(u1, "bang-gia.txt", PHRASE.encode())
    r = u1.post(f"/api/chat/projects/{p['id']}/files", json={"file_id": f["id"]}); assert r.status_code == 200, r.text
    assert [x["mode"] for x in r.json()["files"]] == ["inline"]
    yield {**p, "file": f}
    u1.delete(f"/api/chat/projects/{p['id']}")

# ---------------------------------------------------------------- CRUD
def test_crud_and_validation(u1):
    p = u1.post("/api/chat/projects", json={"name": "Tạm", "instructions": ""}).json()
    assert p["name"] == "Tạm" and p["files"] == [] and p["n_threads"] == 0
    assert any(x["id"] == p["id"] for x in u1.get("/api/chat/projects").json()["items"])
    r = u1.patch(f"/api/chat/projects/{p['id']}", json={"name": "Đổi tên"}); assert r.status_code == 200 and r.json()["name"] == "Đổi tên"
    assert u1.patch(f"/api/chat/projects/{p['id']}", json={"instructions": "x" * 8001}).status_code == 422
    assert u1.post("/api/chat/projects", json={"name": ""}).status_code == 422
    assert u1.delete(f"/api/chat/projects/{p['id']}").status_code == 200
    assert u1.get(f"/api/chat/projects/{p['id']}").status_code == 404

def test_image_not_accepted_as_project_file(u1, proj):
    from test_fixes2 import png
    img = up(u1, "a.png", png())
    assert u1.post(f"/api/chat/projects/{proj['id']}/files", json={"file_id": img["id"]}).status_code == 415

# ---------------------------------------------------------------- runtime
def test_instructions_and_file_reach_the_prompt_and_leave_when_moved_out(u1, proj):
    tid = thread(u1)
    assert INSTR not in sysprompt(u1, tid)                                    # plain chat: nothing from the project
    assert move(u1, tid, proj["id"]).status_code == 200
    assert u1.get(f"/api/v1/threads/{tid}").json()["project_id"] == proj["id"]  # the column is returned with the thread row
    p = sysprompt(u1, tid)
    assert INSTR in p and PHRASE in p and 'Project "Quán cà phê Moza"' in p and '<project_file name="bang-gia.txt">' in p, p
    assert u1.get(f"/api/chat/projects/{proj['id']}").json()["n_threads"] == 1
    assert move(u1, tid, None).status_code == 200
    p = sysprompt(u1, tid); assert INSTR not in p and PHRASE not in p          # moved out: gone from the next run

def test_new_chat_in_project_and_edit_takes_effect(u1, proj):
    tid = thread(u1); move(u1, tid, proj["id"])
    u1.patch(f"/api/chat/projects/{proj['id']}", json={"instructions": "marker-gd89-v2"})
    try:
        p = sysprompt(u1, tid); assert "marker-gd89-v2" in p and INSTR not in p
    finally:
        u1.patch(f"/api/chat/projects/{proj['id']}", json={"instructions": INSTR})

def test_removed_file_leaves_the_prompt(u1, proj):
    f = up(u1, "tam.txt", "marker-gd89-removed".encode()); pid = proj["id"]
    u1.post(f"/api/chat/projects/{pid}/files", json={"file_id": f["id"]})
    tid = thread(u1); move(u1, tid, pid)
    assert "marker-gd89-removed" in sysprompt(u1, tid)
    assert u1.delete(f"/api/chat/projects/{pid}/files/{f['id']}").status_code == 200
    assert "marker-gd89-removed" not in sysprompt(u1, tid)

def test_long_file_goes_to_project_kb_and_search_tool_finds_chunk(u1, proj):
    text = "\n\n".join(f"Mục {i}: ghi chú riêng {uuid.uuid4().hex} về chi nhánh {i} của quán." * 6 for i in range(400))
    f = up(u1, "so-tay.txt", text.encode()); assert f["mode"] == "search"
    r = u1.post(f"/api/chat/projects/{proj['id']}/files", json={"file_id": f["id"]}).json()
    assert {x["name"]: x["mode"] for x in r["files"]}["so-tay.txt"] == "search"
    with psycopg.connect(PG) as c:
        rows = c.execute("SELECT d.kind, c.text FROM kb_chunks c JOIN kb_docs d ON d.id=c.doc_id WHERE d.kb=%s AND d.file_id=%s ORDER BY c.idx",
                         (f"project:{proj['id']}", f["id"])).fetchall()
    assert rows and all(x[0] == "project" for x in rows)
    tid = thread(u1); move(u1, tid, proj["id"])
    p = sysprompt(u1, tid); assert "so-tay.txt" in p and "search_project_files" in p and rows[3][1] not in p   # named, not inlined
    chunk = rows[len(rows) // 2][1]
    ms = ask(u1, tid, "tìm trong dự án: " + chunk)
    tool = [m for m in ms if m.get("type") == "tool" and m.get("name") == "search_project_files"]
    assert tool and tool[-1]["content"].startswith(f"[so-tay.txt #{len(rows) // 2} · 1.0] {chunk}"), tool[-1:]
    other = thread(u1)                                                          # a chat outside the project cannot search it
    ms = ask(u1, other, "tìm trong dự án: " + chunk)
    assert not [m for m in ms if m.get("type") == "tool" and m.get("name") == "search_project_files" and chunk in m["content"]]
    assert not [d for d in u1.get("/api/kb/docs").json() if str(d["kb"]).startswith("project:")]   # never in the shared KB list

def test_project_memory_namespace(u1, proj, mem_agent):
    tid = thread(u1); move(u1, tid, proj["id"])
    ms = ask(u1, tid, "Ghi nhớ: khách quen thích drip bag vị sô-cô-la (marker-gd89-mem)")
    assert [m for m in ms if m.get("type") == "tool" and m.get("name") == "luu_ghi_nho"]
    items = u1.get(f"/api/chat/projects/{proj['id']}/memories").json()["items"]
    assert any("marker-gd89-mem" in x["text"] for x in items)
    assert not [x for x in u1.get("/api/chat/memories").json()["items"] if "marker-gd89-mem" in x["text"]]   # not in the personal memory
    k = next(x["key"] for x in items if "marker-gd89-mem" in x["text"])
    assert u1.delete(f"/api/chat/projects/{proj['id']}/memories/{k}").status_code == 200

# ---------------------------------------------------------------- privacy (RLS)
def test_other_user_cannot_see_or_use_the_project(u1, u2, proj):
    pid = proj["id"]
    assert not [x for x in u2.get("/api/chat/projects").json()["items"] if x["id"] == pid]
    assert u2.get(f"/api/chat/projects/{pid}").status_code == 404
    assert u2.patch(f"/api/chat/projects/{pid}", json={"name": "hack"}).status_code == 404
    assert u2.post(f"/api/chat/projects/{pid}/files", json={"file_id": proj["file"]["id"]}).status_code == 404
    assert u2.get(f"/api/chat/projects/{pid}/memories").status_code == 404
    assert u2.delete(f"/api/chat/projects/{pid}").status_code == 404
    tid = thread(u2)
    assert move(u2, tid, pid).status_code == 404                               # cannot put own chat into someone else's project
    p = sysprompt(u2, tid); assert INSTR not in p and PHRASE not in p
    mine = u2.post("/api/chat/projects", json={"name": "Của u2"}).json()
    try:
        assert u2.post(f"/api/chat/projects/{mine['id']}/files", json={"file_id": proj["file"]["id"]}).status_code == 404   # u1's file (chat_files RLS)
        t1 = thread(u1)
        assert move(u2, t1, mine["id"]).status_code == 404                         # u1's chat is invisible to u2
    finally:
        u2.delete(f"/api/chat/projects/{mine['id']}")
    assert u1.get(f"/api/chat/projects/{pid}").json()["name"] == "Quán cà phê Moza"

def test_unauthenticated():
    assert httpx.get(B + "/api/chat/projects").status_code == 401
