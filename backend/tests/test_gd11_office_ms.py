"""GĐ11 cards 11.58 (Outlook mail complete) + 11.59 (Outlook calendar complete, ocal_*). In-process: the tools run against the FAKE Microsoft Graph (tests/fake_workspace_api.py on 127.0.0.1:8041,
in a thread); tokens come from the real connections layer; approvals go through the real langchain HumanInTheLoopMiddleware with a scripted fake model. Loopback only, no LLM, $0.
Covers: nested folders, search in a folder, read + attachment list + OneDrive links, attachment download (chat file + box), draft / reply draft / send draft / send / reply / reply all / forward with
a note / move / flag / delete, attachments from chat files and from the box (inline < 3 MB, upload session for 10 MB, macro refused before ANY API call), Vietnamese text both ways, approval tiers and
cards, calendars list, free/busy slots, find times, create (no guests = default-ask, guests = always-ask), update / cancel / respond, old `outlook_calendar_*` aliases."""
import asyncio, base64, json, os, sys, uuid
import psycopg, pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

os.environ.setdefault("GOOGLE_API_KEY", "x-not-used")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app")); sys.path.insert(0, os.path.dirname(__file__))
import deepagent_api as A  # noqa: E402,I001  (must come first: circular imports)
import admin, migrations, oauth_tools as O, connections as C, agent_identity  # noqa: E402
import office_scopes as OS, workspace_tools as W, workspace_tools_ms as M  # noqa: E402
import fake_workspace_api as F  # noqa: E402

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
os.environ.setdefault("PG_URI", PG)
TAG = uuid.uuid4().hex[:6]
WA = 93611
FPORT = 8041
FBASE = f"http://127.0.0.1:{FPORT}"
SEEDS: dict = {}
K = lambda n: "ms365:" + n  # noqa: E731

def U(uid, role="editor"): return {"id": uid, "username": f"u{uid}", "role": role, "ws": WA}

# ------------------------------------------------------------------ infrastructure (same approach as test_gd11_office_core.py)
@pytest.fixture(scope="module")
def fakeapi():
    stop = F.start_in_thread(FPORT); yield; stop()

@pytest.fixture(autouse=True)
def office_env(monkeypatch, fakeapi):
    F.reset()
    for k, v in (("GOOGLE_API_BASE", FBASE + "/"), ("GRAPH_API_BASE", FBASE + "/graph/v1.0/"), ("GOOGLE_OAUTH_BASE", FBASE + "/google/oauth"), ("MS_OAUTH_BASE", FBASE + "/microsoft/oauth"),
                 ("AOS_EGRESS_ALLOW", "127.0.0.0/8,::1")):
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AOS_EGRESS_DENY_HOSTS", raising=False)
    async def fake_sleep(s): pass
    monkeypatch.setattr(W, "SLEEP", fake_sleep); monkeypatch.setattr(W, "AUTO_TTL", 0.0)
    W.AUTO.clear(); W._ASKED.clear(); agent_identity._MODE.clear(); agent_identity._OWN.clear()

def run(fn):
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as c:
            await migrations.run_pending(c)
        async with AsyncConnectionPool(PG, min_size=1, max_size=4, open=False, kwargs={"autocommit": True, "row_factory": dict_row}) as pool:
            await pool.open(); A.S["pool"] = pool; A.S["rls"] = admin.rls_ctx
            for g in ("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO aos_rls", "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO aos_rls"):
                for i in range(5):   # other cards GRANT / ALTER at the same time ("tuple concurrently updated")
                    try: await A.q(g); break
                    except psycopg.errors.InternalError_:
                        if i == 4: raise
                        await asyncio.sleep(0.5)
            await W.init()
            await seed()
            try: return await fn()
            finally: await cleanup()
    return asyncio.run(go())

async def seed():
    await A.q("INSERT INTO workspaces(id,name,gw_tools) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (WA, f"t1158-{TAG}", Jsonb({"google": "write", "ms365": "write"})))
    SEEDS["alice"] = (await A.q("INSERT INTO users(username,pw_hash) VALUES(%s,'x') RETURNING id", (f"t1158-{TAG}-alice",), one=True))["id"]
    SEEDS["ag"] = (await A.q("INSERT INTO agents(name,workspace_id,owner_id) VALUES(%s,%s,%s) RETURNING id", (f"t1158-{TAG}-ag", WA, SEEDS["alice"]), one=True))["id"]
    p = await A.q("""INSERT INTO oauth_providers(name,display_name,authorize_url,token_url,revoke_url,api_base,client_id,workspace_id,scopes,token_auth,pkce,enabled,redirect_mode)
                     VALUES('microsoft','Microsoft',%s,%s,%s,%s,'fake-microsoft-client',%s,%s,'body',true,true,'loopback_paste') RETURNING *""",
                  (f"{FBASE}/microsoft/oauth/authorize", f"{FBASE}/microsoft/oauth/token", f"{FBASE}/microsoft/oauth/revoke", FBASE + "/", WA, OS.scope_string("microsoft", "write")), one=True)
    await A.q("UPDATE oauth_providers SET client_secret_enc=%s WHERE id=%s", (O.enc("fake-secret", "oauth_providers.client_secret_enc", WA, p["id"]), p["id"]))
    SEEDS["prov"] = p
    j = F._issue("microsoft", "alice", OS.scope_string("microsoft", "write"))
    await C.save_tokens(("user", SEEDS["alice"]), p, j, account="alice@fake.test")

async def cleanup():
    for t in ("agent_audit", "oauth_connections", "connections", "oauth_providers", "oauth_audit"):
        await A.q(f"DELETE FROM {t} WHERE workspace_id = %s", (WA,))
    await A.q("DELETE FROM oauth_states WHERE user_id = %s OR workspace_id = %s", (SEEDS["alice"], WA))
    await A.q("DELETE FROM chat_files WHERE created_by = %s", (SEEDS["alice"],))
    await A.q("DELETE FROM agent_identity WHERE agent_id = %s", (SEEDS["ag"],)); await A.q("DELETE FROM agents WHERE id = %s", (SEEDS["ag"],))
    await A.q("DELETE FROM users WHERE username LIKE %s", (f"t1158-{TAG}-%",))
    await A.q("DELETE FROM crypto_deks WHERE workspace_id = %s", (WA,)); await A.q("DELETE FROM workspaces WHERE id = %s", (WA,))
    agent_identity._MODE.clear(); agent_identity._OWN.clear(); SEEDS.clear()

async def as_(user, fn, *a, **kw):
    tok = admin.USER.set(user)
    try: return await fn(*a, **kw)
    finally: admin.USER.reset(tok)

async def call(key, user=None, idem=None, **args):
    """Run one office tool the way an agent does (replay layer on) as alice; returns the text the model would read."""
    tok = admin.USER.set(user or U(SEEDS["alice"])); t2 = W.CALL_CTX.set({"key": idem})
    try: return await W._tool(K(key), SEEDS["ag"]).ainvoke(args)
    finally: W.CALL_CTX.reset(t2); admin.USER.reset(tok)

async def jcall(key, **args):
    r = await call(key, **args)
    try: return json.loads(r)
    except ValueError: raise AssertionError(r) from None

def api_calls(): return [c for c in F.S["log"] if c[1] != "(injected)"]

class FakeBox:
    """What sbx_backend.for_config returns when the agent has a computer."""
    def __init__(self, files=None): self.files = dict(files or {})
    async def get(self, p):
        if p not in self.files: raise FileNotFoundError(p)
        return self.files[p]
    async def put(self, p, d): self.files[p] = d

def with_box(monkeypatch, box):
    import sbx_backend
    async def fc(config=None, *, create=True): return box
    monkeypatch.setattr(sbx_backend, "for_config", fc)

def b64h(s): return "=?utf-8?B?" + base64.b64encode(s.encode()).decode() + "?="

# ------------------------------------------------------------------ registration + tiers
def test_tools_registered_with_right_tiers():
    star = ["outlook_send_draft", "outlook_send", "outlook_reply", "outlook_reply_all", "outlook_forward", "outlook_delete", "ocal_update", "ocal_cancel", "ocal_respond"]
    default = ["outlook_draft", "outlook_reply_draft", "outlook_move", "outlook_flag"]
    read = ["outlook_list_folders", "outlook_search", "outlook_read", "outlook_get_attachment", "ocal_list_calendars", "ocal_list", "ocal_free_busy", "ocal_find_times"]
    for n in star: assert W.tier_of(n, {}) == W.ALWAYS_ASK, n
    for n in default: assert W.tier_of(n, {}) == W.DEFAULT_ASK, n
    for n in read: assert W.tier_of(n, {}) == W.READ, n
    assert W.tier_of("ocal_create", {"subject": "x"}) == W.DEFAULT_ASK and W.tier_of("ocal_create", {"attendees": ["a@b.vn"]}) == W.ALWAYS_ASK
    assert W.tier_of("outlook_calendar_create", {"attendees": ["a@b.vn"]}) == W.ALWAYS_ASK and W.META["ms365:outlook_calendar_list"].alias_of == "ms365:ocal_list"
    assert set(star + default + ["ocal_create"]) <= W.WRITE_NAMES and not set(read) & W.WRITE_NAMES
    auto = frozenset(W.META[K(n)].canonical for n in default)                      # an admin may allow the default-ask ones, never the stars
    assert [n for n in default if W.needs_approval(n, {}, auto)] == [] and all(W.needs_approval(n, {}, auto) for n in star)

# ------------------------------------------------------------------ mail: read
def test_folders_search_read_and_attachments():
    async def go():
        d = await jcall("outlook_list_folders")
        inbox = next(f for f in d if f["id"] == "inbox"); assert inbox["name"] == "Inbox" and inbox["children"][0]["name"] == "Projects" and any(f["id"] == "archive" for f in d)
        assert all(not f.get("children") for f in d if f["id"] != "inbox")
        F.deliver("alice", "Lan <lan@khach.vn>", b64h("Báo giá tháng mười một"), "Xem tại https://congty-my.sharepoint.com/:w:/g/personal/x/abc và gửi lại.", folder="archive", labels=(),
                  atts=[{"id": "att-1", "name": "bao_gia.txt", "mime": "text/plain", "data": "Báo giá: 5 triệu".encode()}, {"id": "att-2", "name": "Sơ đồ.pdf", "mime": "application/pdf", "data": b"%PDF-1.4 x"}], mid="m-arch")
        r = await jcall("outlook_search", query="", folder_id="archive"); assert [m["id"] for m in r] == ["m-arch"] and r[0]["subject"] == "Báo giá tháng mười một" and r[0]["has_attachments"] is True
        assert [m["id"] for m in await jcall("outlook_search", query="thứ Sáu")] == ["m-a2"] and await jcall("outlook_search", query="thứ Sáu", folder_id="archive") == []
        m = await jcall("outlook_read", message_id="m-arch")
        assert m["subject"] == "Báo giá tháng mười một" and m["to"] == ["alice@fake.test"] and [a["name"] for a in m["attachments"]] == ["bao_gia.txt", "Sơ đồ.pdf"] and m["attachments"][0]["size"] == len("Báo giá: 5 triệu".encode())
        assert m["cloud_links"] == ["https://congty-my.sharepoint.com/:w:/g/personal/x/abc"] and "contentBytes" not in json.dumps(m)
        plain = await jcall("outlook_read", message_id="m-a1"); assert plain["attachments"] == [] and "1,2 tỷ" in plain["body"]
        assert (await call("outlook_read", message_id="nope")).startswith("Error: HTTP 404")
    run(go)

def test_get_attachment_saves_chat_file_and_box(monkeypatch):
    async def go():
        F.deliver("alice", "lan@khach.vn", "Tệp", "x", mid="m-f", labels=(), atts=[{"id": "att-1", "name": "ghi_chu.txt", "mime": "text/plain", "data": "Ghi chú tiếng Việt".encode()},
                                                                                 {"id": "att-2", "name": "macro.docm", "mime": "application/vnd.ms-word", "data": b"PK\x03\x04junk"}])
        box = FakeBox({"/workspace/uploads/ghi_chu.txt": b"mine"}); with_box(monkeypatch, box)
        r = await jcall("outlook_get_attachment", message_id="m-f", attachment_id="att-1")
        assert r["name"] == "ghi_chu.txt" and r["chat_file"]["url"].startswith("/api/files/") and r["box_path"].startswith("/workspace/uploads/") and r["box_path"] != "/workspace/uploads/ghi_chu.txt"
        assert box.files["/workspace/uploads/ghi_chu.txt"] == b"mine" and box.files[r["box_path"]] == "Ghi chú tiếng Việt".encode()          # the user's own file was not overwritten
        n = len(box.files); bad = await call("outlook_get_attachment", message_id="m-f", attachment_id="att-2")
        assert bad.startswith("Error:") and "macro" in bad and len(box.files) == n                                                                    # a macro file is saved nowhere
        with_box(monkeypatch, None)
        r = await jcall("outlook_get_attachment", message_id="m-f", attachment_id="att-1"); assert "chat_file" in r and "box_path" not in r              # no box: the chat file only
        F.box("alice")[-1]["atts"].append({"id": "att-3", "name": "tai-lieu", "ref": "https://1drv.ms/w/abc", "data": b""})
        lk = await jcall("outlook_get_attachment", message_id="m-f", attachment_id="att-3"); assert lk["link"] == "https://1drv.ms/w/abc" and "chat_file" not in lk
    run(go)

# ------------------------------------------------------------------ mail: write
def sent(): return F.S["sent"]

def test_send_draft_reply_forward_move_flag_delete():
    async def go():
        assert (await call("outlook_send", to=["lan@khach.vn"], subject="Họp thứ Sáu — xác nhận", body="Chào chị Lan,\nEm xác nhận 9 giờ sáng ạ. Cảm ơn!", cc=["sep@congty.vn"], bcc=["kt@congty.vn"])) == "Email sent."
        msg = sent()[-1]["message"]; assert msg["subject"] == "Họp thứ Sáu — xác nhận" and "Cảm ơn" in msg["body"]["content"] and msg["body"]["contentType"] == "Text"
        assert msg["bccRecipients"][0]["emailAddress"]["address"] == "kt@congty.vn" and msg["ccRecipients"][0]["emailAddress"]["address"] == "sep@congty.vn"
        await call("outlook_send", to=["lan@khach.vn"], subject="HTML", body="<b>Xin chào</b>", html=True); assert sent()[-1]["message"]["body"]["contentType"] == "HTML"
        n = len(sent()); bad = await call("outlook_send", to=["không phải email"], subject="s", body="b"); assert bad.startswith("Error:") and len(sent()) == n
        # drafts
        d = await jcall("outlook_draft", to=["lan@khach.vn"], subject="Nháp", body="chưa gửi"); assert d["draft_id"] and len(sent()) == n and F.S["drafts"][-1]["message_id"] == d["draft_id"]
        await call("outlook_send_draft", draft_id=d["draft_id"]); assert sent()[-1]["from_draft"] and sent()[-1]["subject"] == "Nháp"
        rd = await jcall("outlook_reply_draft", message_id="m-a2", comment="Em xác nhận ạ"); n = len(sent())
        assert rd["subject"] == "Re: Hẹn gặp thứ Sáu" and F.S["drafts"][-1]["op"] == "createReply" and rd["to"] == ["lan@khach.vn"]
        assert (await jcall("outlook_reply_draft", message_id="m-a2", reply_all=True))["draft_id"] and F.S["drafts"][-1]["op"] == "createReplyAll" and len(sent()) == n
        # reply / reply all / forward with a note
        await call("outlook_reply", message_id="m-a2", comment="Cảm ơn chị"); assert sent()[-1]["op"] == "reply" and sent()[-1]["comment"] == "Cảm ơn chị" and sent()[-1]["thread"] == "tm-a2"
        await call("outlook_reply_all", message_id="m-a2", comment="Cảm ơn cả nhà"); assert sent()[-1]["op"] == "replyAll"
        await call("outlook_forward", message_id="m-a1", to=["sep@congty.vn"], note="Anh xem giúp em số liệu này"); f = sent()[-1]
        assert f["op"] == "forward" and f["comment"] == "Anh xem giúp em số liệu này" and f["to"] == "sep@congty.vn" and f["of"] == "m-a1"
        assert (await call("outlook_forward", message_id="m-a1", to=[], note="x")).startswith("Error:")
        # move / flag / delete
        mv = await jcall("outlook_move", message_id="m-a1", folder_id="archive"); assert mv["moved"] and F.S["moved"][-1] == {"who": "alice", "id": "m-a1", "to": "archive"}
        assert (await call("outlook_move", message_id="m-a1", folder_id="khong-co")).startswith("Error: HTTP 400")
        await call("outlook_flag", message_id="m-a2", flag="flagged", categories=["Quan trọng"], is_read=True)
        assert F.S["modified"][-1]["body"] == {"flag": {"flagStatus": "flagged"}, "categories": ["Quan trọng"], "isRead": True}
        assert (await call("outlook_flag", message_id="m-a2")).startswith("Error:") and (await call("outlook_flag", message_id="m-a2", flag="bogus")).startswith("Error:")
        await call("outlook_delete", message_id="m-a2"); assert F.box("alice")[1]["folder"] == "deleteditems" and F.S["moved"][-1]["to"] == "deleteditems"
    run(go)

def test_vietnamese_round_trip():
    async def go():
        F.deliver("alice", "Kế toán <kt@congty.vn>", b64h("Đối chiếu công nợ — quý 3"), "Nội dung có dấu.", mid="m-vi")
        assert (await jcall("outlook_read", message_id="m-vi"))["subject"] == "Đối chiếu công nợ — quý 3" and (await jcall("outlook_search", query="Nội dung"))[0]["subject"] == "Đối chiếu công nợ — quý 3"
        await call("outlook_send", to=["a@b.vn"], subject="Tiếng Việt: ặ ế ợ ữ", body="Ổn định ✓"); m = sent()[-1]["message"]
        assert m["subject"] == "Tiếng Việt: ặ ế ợ ữ" and m["body"]["content"] == "Ổn định ✓"
    run(go)

# ------------------------------------------------------------------ mail: attachments
def test_attachments_inline_chat_file_and_box(monkeypatch):
    async def go():
        import attachments
        f = await as_(U(SEEDS["alice"]), attachments.store, "Báo cáo tháng 10: doanh thu 1,2 tỷ".encode(), "bao_cao.txt")
        box = FakeBox({"/workspace/out/so_lieu.pdf": b"%PDF-1.4 " + b"x" * 1000}); with_box(monkeypatch, box)
        r = await call("outlook_send", to=["lan@khach.vn"], subject="Đính kèm", body="Xem file", attachments=[f["id"], "/workspace/out/so_lieu.pdf"])
        assert r == "Email sent with 2 attachment(s)."
        m = sent()[-1]["message"]; assert [a["name"] for a in m["attachments"]] == ["bao_cao.txt", "so_lieu.pdf"] and base64.b64decode(m["attachments"][0]["contentBytes"]).decode().startswith("Báo cáo")
        assert F.S["uploads"] == []
        d = await jcall("outlook_draft", to=["lan@khach.vn"], subject="Nháp có file", body="b", attachments=["/workspace/out/so_lieu.pdf"])
        assert d["attachments"] == ["so_lieu.pdf"] and [u["via"] for u in F.S["uploads"]] == ["inline"]                       # draft: each small file is added to the draft
        assert (await call("outlook_draft", to=["a@b.vn"], subject="s", body="b", attachments=["/etc/passwd"])).startswith("Error:") and (await call("outlook_draft", to=["a@b.vn"], subject="s", body="b", attachments=["/workspace/../etc/passwd"])).startswith("Error:")
        assert (await call("outlook_draft", to=["a@b.vn"], subject="s", body="b", attachments=[str(uuid.uuid4())])).startswith("Error:")
        with_box(monkeypatch, None); assert "no computer" in await call("outlook_draft", to=["a@b.vn"], subject="s", body="b", attachments=["/workspace/x.pdf"])
    run(go)

def test_big_attachment_uses_upload_session_and_macro_refused_before_any_call(monkeypatch):
    async def go():
        big = b"%PDF-1.7 " + os.urandom(10 * 1024 * 1024)
        box = FakeBox({"/workspace/lon.pdf": big, "/workspace/mau.docm": b"PK\x03\x04", "/workspace/anh.pdf": b"%PDF- hop le", "/workspace/gia.docx": b"not a zip"}); with_box(monkeypatch, box)
        r = await call("outlook_send", to=["lan@khach.vn"], subject="File lớn", body="10 MB", attachments=["/workspace/lon.pdf", "/workspace/anh.pdf"])
        assert r == "Email sent with 2 attachment(s)."
        assert sorted(u["via"] for u in F.S["uploads"]) == ["inline", "session"] and next(u for u in F.S["uploads"] if u["via"] == "session")["size"] == len(big)
        assert F.S["upload_auth_seen"] == [] and sent()[-1]["from_draft"] and len(sent()[-1]["atts"]) == 2 and sent()[-1]["subject"] == "File lớn"      # no token on the upload URL; the draft was sent
        # refused files: no API call at all, nothing sent
        F.S["log"].clear(); n = len(sent())
        for bad in ("/workspace/mau.docm", "/workspace/gia.docx"):
            t = await call("outlook_send", to=["a@b.vn"], subject="s", body="b", attachments=["/workspace/anh.pdf", bad]); assert t.startswith("Error:") and "refused" in t, t
        assert api_calls() == [] and len(sent()) == n
        box.files["/workspace/qua.pdf"] = b"%PDF-" + b"0" * (21 * 1024 * 1024)
        t = await call("outlook_draft", to=["a@b.vn"], subject="s", body="b", attachments=["/workspace/qua.pdf"]); assert "20 MB" in t and api_calls() == []
        for i in range(11): box.files[f"/workspace/f{i}.pdf"] = b"%PDF-1"
        t = await call("outlook_draft", to=["a@b.vn"], subject="s", body="b", attachments=[f"/workspace/f{i}.pdf" for i in range(11)]); assert "at most 10" in t and api_calls() == []
    run(go)

# ------------------------------------------------------------------ approvals (real HumanInTheLoopMiddleware, scripted model)
def _agent_for(keys, calls):
    from langchain.agents import create_agent
    from langchain.agents.middleware import HumanInTheLoopMiddleware
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langgraph.checkpoint.memory import InMemorySaver
    class Scripted(BaseChatModel):
        script: list
        @property
        def _llm_type(self): return "scripted"
        def bind_tools(self, tools, **kw): return self
        def _generate(self, messages, stop=None, run_manager=None, **kw):
            n = sum(1 for m in messages if isinstance(m, AIMessage))
            return ChatResult(generations=[ChatGeneration(message=self.script[n] if n < len(self.script) else AIMessage(content="done"))])
    script = [AIMessage(content="", tool_calls=[{"name": n, "args": a, "id": i} for n, a, i in calls])]
    tools = [W._tool(k, SEEDS["ag"]) for k in keys]
    return create_agent(model=Scripted(script=script), tools=tools, middleware=[HumanInTheLoopMiddleware(interrupt_on=W.interrupt_config([t.name for t in tools], set(), WA)),
                                                                              W.OfficeMiddleware(SEEDS["ag"], WA)], checkpointer=InMemorySaver())

async def _drive(agent, thread, decision=None):
    from langgraph.types import Command
    from langchain_core.messages import HumanMessage
    cfg = {"configurable": {"thread_id": thread}}; tok = admin.USER.set(U(SEEDS["alice"]))
    try: return await agent.ainvoke(Command(resume={"decisions": [decision]}) if decision else {"messages": [HumanMessage("go")]}, cfg)
    finally: admin.USER.reset(tok)

def _req(r): return [x for i in r.get("__interrupt__", []) for x in i.value["action_requests"]]

def test_forward_with_note_and_move_go_through_the_approval_card(monkeypatch):
    async def go():
        box = FakeBox({"/workspace/a.pdf": b"%PDF-1"}); with_box(monkeypatch, box)
        ag = _agent_for([K("outlook_forward")], [("outlook_forward", {"message_id": "m-a1", "to": ["sep@congty.vn"], "note": "Anh xem giúp em"}, "c-f1")])
        r = await _drive(ag, "t-fwd"); q_ = _req(r)
        assert q_ and q_[0]["name"] == "outlook_forward" and "To: sep@congty.vn" in q_[0]["description"] and "Text: Anh xem giúp em" in q_[0]["description"] and "Message: m-a1" in q_[0]["description"] and not sent()
        await _drive(ag, "t-fwd", {"type": "approve"}); assert sent()[-1]["op"] == "forward" and sent()[-1]["comment"] == "Anh xem giúp em"
        await W.A.q("UPDATE workspaces SET gw_tools=gw_tools || %s::jsonb WHERE id=%s", (Jsonb({"auto_tools": ["ms365:outlook_move"]}), WA)); W.AUTO.clear()
        ag = _agent_for([K("outlook_move")], [("outlook_move", {"message_id": "m-a1", "folder_id": "archive"}, "c-m1")]); r = await _drive(ag, "t-mv")
        assert not _req(r) and F.S["moved"][-1]["to"] == "archive"                                             # default-ask AND allowed by the admin: runs alone
        await W.A.q("UPDATE workspaces SET gw_tools=gw_tools || %s::jsonb WHERE id=%s", (Jsonb({"auto_tools": []}), WA)); W.AUTO.clear()
        n = len(F.S["moved"]); ag = _agent_for([K("outlook_move")], [("outlook_move", {"message_id": "m-a2", "folder_id": "archive"}, "c-m2")]); r = await _drive(ag, "t-mv2")
        assert _req(r) and "Move to folder: archive" in _req(r)[0]["description"] and len(F.S["moved"]) == n                          # not allowed: the card
        await _drive(ag, "t-mv2", {"type": "reject", "message": "không"}); assert len(F.S["moved"]) == n
        ag = _agent_for([K("outlook_send")], [("outlook_send", {"to": ["a@b.vn"], "subject": "Chủ đề", "body": "Nội dung", "attachments": ["/workspace/a.pdf"]}, "c-s1")]); r = await _drive(ag, "t-snd")
        assert "Subject: Chủ đề" in _req(r)[0]["description"] and "Attachments: a.pdf" in _req(r)[0]["description"]
        # replaying the same tool_call_id sends once
        await _drive(ag, "t-snd", {"type": "approve"}); k = len(sent()); out = await call("outlook_send", to=["a@b.vn"], subject="Chủ đề", body="Nội dung", idem=f"{SEEDS['ag']}:t-snd:c-s1")
        assert len(sent()) == k and out.startswith("Email sent")
    run(go)

# ------------------------------------------------------------------ calendar
def test_calendars_list_and_old_aliases():
    async def go():
        cs = await jcall("ocal_list_calendars"); assert cs == [{"id": "cal-main", "name": "Calendar", "owner": "alice@fake.test", "default": True, "can_edit": None}]
        ev = await jcall("ocal_list", start="2026-10-25T00:00:00+07:00"); assert ev[0]["subject"] == "Họp giao ban tuần" and ev[0]["id"] == "e-a1"
        await jcall("ocal_list", start="2026-10-25T00:00:00+07:00", calendar_id="cal-main"); assert ("microsoft", "alice", "GET", "/graph/v1.0/me/calendars/cal-main/calendarView") in F.S["log"]
        old = await call("outlook_calendar_list", start="2026-10-25T00:00:00+07:00"); assert "Họp giao ban tuần" in old
        assert (await call("outlook_calendar_create", subject="Cũ", start="2026-10-28T09:00:00", end="2026-10-28T10:00:00")).startswith("Event created")
    run(go)

def test_free_busy_slots_and_find_times():
    async def go():
        r = await jcall("ocal_free_busy", emails=["alice@fake.test", "bob@fake.test"], start="2026-10-26T08:00:00+07:00", end="2026-10-26T12:00:00+07:00", interval_minutes=30)
        assert next(p for p in r["people"] if p["email"] == "alice@fake.test")["busy"] == [{"start": "2026-10-26T09:00:00+07:00", "end": "2026-10-26T10:00:00+07:00"}]
        assert next(p for p in r["people"] if p["email"] == "bob@fake.test")["busy"] == []
        assert r["common_free"] == [{"start": "2026-10-26T08:00:00+07:00", "end": "2026-10-26T09:00:00+07:00"}, {"start": "2026-10-26T10:00:00+07:00", "end": "2026-10-26T12:00:00+07:00"}]
        sc = F.S["schedule_calls"][-1]["body"]; assert sc["startTime"] == {"dateTime": "2026-10-26T08:00:00", "timeZone": "SE Asia Standard Time"} and sc["availabilityViewInterval"] == 30
        b = await jcall("ocal_free_busy", emails=["bob@fake.test"], start="2026-10-27T13:00:00+07:00", end="2026-10-27T16:00:00+07:00", interval_minutes=60)
        assert b["common_free"] == [{"start": "2026-10-27T13:00:00+07:00", "end": "2026-10-27T14:00:00+07:00"}, {"start": "2026-10-27T15:00:00+07:00", "end": "2026-10-27T16:00:00+07:00"}]
        assert (await jcall("ocal_free_busy", emails=["alice@fake.test"], start="2026-10-26T00:00:00+07:00", end="2026-10-27T00:00:00+07:00", interval_minutes=1440))["interval_minutes"] == 1440
        n = len(F.S["schedule_calls"])
        for bad in (3, 1441): assert (await call("ocal_free_busy", emails=["alice@fake.test"], interval_minutes=bad)).startswith("Error:")
        assert (await call("ocal_free_busy", emails=[])).startswith("Error:") and (await call("ocal_free_busy", emails=["x"])).startswith("Error:") and len(F.S["schedule_calls"]) == n
        t = await jcall("ocal_find_times", attendees=["bob@fake.test"], duration_minutes=90, start="2026-10-28T00:00:00+07:00", end="2026-10-30T00:00:00+07:00")
        assert t["suggestions"][0]["start"] == "2026-10-28T09:00:00" and t["suggestions"][0]["confidence"] == 100
        fb = next(c for c in F.S["schedule_calls"] if c.get("find"))["body"]; assert fb["meetingDuration"] == "PT1H30M" and fb["attendees"][0]["emailAddress"]["address"] == "bob@fake.test" and fb["timeConstraint"]["activityDomain"] == "work"
    run(go)

def test_event_create_update_cancel_respond():
    async def go():
        a = await call("ocal_create", subject="Họp nhóm", start="2026-10-28T09:00:00", end="2026-10-28T10:00:00", location="Phòng 3", body="Chuẩn bị báo cáo", attendees=["bob@fake.test"], calendar_id="cal-main")
        c = F.S["created"][-1]["body"]; assert a.startswith("Event created") and c["attendees"][0]["emailAddress"]["address"] == "bob@fake.test" and c["start"]["timeZone"] == "SE Asia Standard Time" and c["location"] == {"displayName": "Phòng 3"}
        eid = a.rsplit("id: ", 1)[1].rstrip(")"); assert ("microsoft", "alice", "POST", "/graph/v1.0/me/calendars/cal-main/events") in F.S["log"]
        await call("ocal_update", event_id=eid, subject="Họp nhóm (dời giờ)", start="2026-10-28T14:00:00", end="2026-10-28T15:00:00", attendees=["bob@fake.test", "sep@congty.vn"])
        p = F.S["patched"][-1]["body"]; assert p["subject"] == "Họp nhóm (dời giờ)" and len(p["attendees"]) == 2 and p["start"]["dateTime"] == "2026-10-28T14:00:00"
        assert (await call("ocal_update", event_id=eid)).startswith("Error:") and (await call("ocal_update", event_id=eid, start="2026-10-28T14:00:00")).startswith("Error:")
        assert "cancelled" in await call("ocal_cancel", event_id=eid, comment="Hủy vì trùng lịch"); assert F.S["responded"][-1]["op"] == "cancel" and F.S["responded"][-1]["comment"] == "Hủy vì trùng lịch" and not F.S["deleted"]
        solo = (await call("ocal_create", subject="Việc riêng", start="2026-10-29T09:00:00", end="2026-10-29T10:00:00")).rsplit("id: ", 1)[1].rstrip(")")
        assert await call("ocal_cancel", event_id=solo) == "Event deleted." and F.S["deleted"][-1]["id"] == solo                       # no guests: plain delete
        for resp, op in (("accept", "accept"), ("decline", "decline"), ("tentative", "tentativelyAccept")):
            await call("ocal_respond", event_id="e-a1", response=resp, comment="ok"); assert F.S["responded"][-1]["op"] == op
        await call("ocal_respond", event_id="e-a1", response="accept", send_response=False); assert F.S["responded"][-1]["sendResponse"] is False
        assert (await call("ocal_respond", event_id="e-a1", response="maybe")).startswith("Error:")
    run(go)

def test_calendar_approval_cards_list_guests():
    async def go():
        ag = _agent_for([K("ocal_create"), K("ocal_update"), K("ocal_respond")], [("ocal_create", {"subject": "Họp", "start": "2026-10-28T09:00:00", "end": "2026-10-28T10:00:00", "attendees": ["bob@fake.test", "lan@khach.vn"]}, "c-e1")])
        r = await _drive(ag, "t-ev"); d = _req(r)[0]["description"]
        assert "Title: Họp" in d and "bob@fake.test, lan@khach.vn" in d and "invitation" in d and not F.S["created"]                     # guests -> always the card, whatever the admin list says
        await W.A.q("UPDATE workspaces SET gw_tools=gw_tools || %s::jsonb WHERE id=%s", (Jsonb({"auto_tools": ["ms365:ocal_create"]}), WA)); W.AUTO.clear()
        ag = _agent_for([K("ocal_create")], [("ocal_create", {"subject": "Họp", "start": "2026-10-28T09:00:00", "end": "2026-10-28T10:00:00", "attendees": ["bob@fake.test"]}, "c-e2")]); assert _req(await _drive(ag, "t-ev2"))
        ag = _agent_for([K("ocal_create")], [("ocal_create", {"subject": "Việc riêng", "start": "2026-10-28T09:00:00", "end": "2026-10-28T10:00:00"}, "c-e3")]); r = await _drive(ag, "t-ev3")
        assert not _req(r) and len(F.S["created"]) == 1                                                                                   # no guests + allowed: runs alone
        ag = _agent_for([K("ocal_update")], [("ocal_update", {"event_id": "e-a1", "subject": "Đổi tên"}, "c-e4")]); assert _req(await _drive(ag, "t-ev4")) and not F.S["patched"]   # editing always asks
    run(go)
