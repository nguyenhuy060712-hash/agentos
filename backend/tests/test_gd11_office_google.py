"""GĐ11 cards 11.56 (Gmail complete) + 11.57 (Google Calendar complete). In-process, like tests/test_gd11_office_core.py: the tools run against the FAKE Google server
(tests/fake_workspace_api.py on 127.0.0.1:8038, in a thread); tokens come from the real connections layer; no network except loopback, no LLM, $0.
Gmail: Vietnamese subjects / names / bodies round-trip (RFC 2047), reply lands in the right thread with In-Reply-To / References, forward keeps attachments, chat-file and
/workspace attachments, macro / 20 MB refused BEFORE any API call, > 3.5 MB through /upload/, labels, tiers (send / reply / forward / trash = always ask).
Calendar: calendars, get, free/busy, interval-intersection unit test + suggest on the fake API, attendees => invitations => always ask, update / delete / respond always ask."""
import asyncio, base64, datetime as dt, io, json, os, sys, uuid, zipfile
import psycopg, pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

os.environ.setdefault("GOOGLE_API_KEY", "x-not-used")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app")); sys.path.insert(0, os.path.dirname(__file__))
import deepagent_api as A  # noqa: E402,I001  (must come first: circular imports)
import admin, migrations, oauth_tools as O, connections as C, agent_identity  # noqa: E402
import office_scopes as OS, workspace_tools as W, workspace_tools_google as G  # noqa: E402
import attachments as AT  # noqa: E402
import fake_workspace_api as F  # noqa: E402

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
os.environ.setdefault("PG_URI", PG)
TAG = uuid.uuid4().hex[:6]
WA, WB = 93561, 93562
FPORT = 8038
FBASE = f"http://127.0.0.1:{FPORT}"
SEEDS: dict = {}
VN = dt.timezone(dt.timedelta(hours=7))

def U(ws, uid, role="editor", **kw): return {"id": uid, "username": f"u{uid}", "role": role, "ws": ws, **kw}

# ------------------------------------------------------------------ infrastructure
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
    for w in (WA, WB):
        await A.q("INSERT INTO workspaces(id,name,gw_tools) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (w, f"t1156-{TAG}-{w}", Jsonb({"google": "write", "ms365": "write"})))
    for k in ("alice", "bob"):
        SEEDS[k] = (await A.q("INSERT INTO users(username,pw_hash) VALUES(%s,'x') RETURNING id", (f"t1156-{TAG}-{k}",), one=True))["id"]
    for w in (WA, WB): SEEDS[f"p_{w}"] = await mk_provider(w)
    SEEDS["ag"] = (await A.q("INSERT INTO agents(name,workspace_id,owner_id) VALUES(%s,%s,%s) RETURNING id", (f"t1156-{TAG}-ag", WA, SEEDS["alice"]), one=True))["id"]
    SEEDS["ag_b"] = (await A.q("INSERT INTO agents(name,workspace_id,owner_id) VALUES(%s,%s,%s) RETURNING id", (f"t1156-{TAG}-agb", WB, SEEDS["bob"]), one=True))["id"]

async def mk_provider(ws):
    p = await A.q("""INSERT INTO oauth_providers(name,display_name,authorize_url,token_url,revoke_url,api_base,client_id,workspace_id,scopes,token_auth,pkce,enabled,redirect_mode)
                     VALUES('google','Google',%s,%s,%s,%s,'fake-google-client',%s,%s,'body',true,true,'loopback_paste') RETURNING *""",
                  (f"{FBASE}/google/oauth/authorize", f"{FBASE}/google/oauth/token", f"{FBASE}/google/oauth/revoke", FBASE + "/", ws, OS.scope_string("google", "write")), one=True)
    await A.q("UPDATE oauth_providers SET client_secret_enc=%s WHERE id=%s", (O.enc("fake-secret", "oauth_providers.client_secret_enc", ws, p["id"]), p["id"]))
    return p

async def cleanup():
    ws = [WA, WB]; uids = [SEEDS[k] for k in ("alice", "bob") if k in SEEDS]
    for t in ("agent_audit", "oauth_connections", "connections", "oauth_providers", "oauth_audit"):
        await A.q(f"DELETE FROM {t} WHERE workspace_id = ANY(%s)", (ws,))
    await A.q("DELETE FROM oauth_states WHERE user_id = ANY(%s) OR workspace_id = ANY(%s)", (uids, ws))
    ids = [SEEDS[k] for k in ("ag", "ag_b") if k in SEEDS]
    if ids: await A.q("DELETE FROM agent_identity WHERE agent_id = ANY(%s)", (ids,)); await A.q("DELETE FROM agents WHERE id = ANY(%s)", (ids,))
    await A.q("DELETE FROM chat_files WHERE created_by = ANY(%s)", (uids,))
    await A.q("DELETE FROM users WHERE username LIKE %s", (f"t1156-{TAG}-%",))
    await A.q("DELETE FROM crypto_deks WHERE workspace_id = ANY(%s)", (ws,)); await A.q("DELETE FROM workspaces WHERE id = ANY(%s)", (ws,))
    agent_identity._MODE.clear(); agent_identity._OWN.clear(); SEEDS.clear()

async def as_(user, fn, *a, **kw):
    tok = admin.USER.set(user)
    try: return await fn(*a, **kw)
    finally: admin.USER.reset(tok)

async def connect(who, uid, ws, scope=None):
    j = F._issue("google", who, scope if scope is not None else OS.scope_string("google", "write"))
    await C.save_tokens(("user", uid), SEEDS[f"p_{ws}"], j, account=f"{who}@fake.test")

async def call(user, key, idem=None, **args):
    """Run one office tool the way an agent does (inside the replay / approval layer) as `user`; returns the text the model would read."""
    tok = admin.USER.set(user); t2 = W.CALL_CTX.set({"key": idem})
    try: return await W._tool("google:" + key, SEEDS["ag_b"] if user.get("ws") == WB else SEEDS["ag"]).ainvoke(args)
    finally: W.CALL_CTX.reset(t2); admin.USER.reset(tok)

async def alice():
    await connect("alice", SEEDS["alice"], WA); return U(WA, SEEDS["alice"])

def api_calls(): return [c for c in F.S["log"] if c[1] != "(injected)"]

class FBox:
    """Stands in for sbx_backend.Box: a dict of files."""
    def __init__(self, files=None): self.files = dict(files or {}); self.puts = []
    async def get(self, p):
        if p not in self.files: raise FileNotFoundError("no such file")
        return self.files[p]
    async def put(self, p, d): self.files[p] = d; self.puts.append(p); return {"ok": True}

def with_box(monkeypatch, box):
    import sbx_backend
    async def fc(config=None, *, create=True): return box
    monkeypatch.setattr(sbx_backend, "for_config", fc)

def docx(vba=False):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>"); z.writestr("word/document.xml", "<w/>")
        if vba: z.writestr("word/vbaProject.bin", b"\x00VBA")
    return b.getvalue()

def last_sent(): return F.S["sent"][-1]

# ================================================================== GMAIL (11.56)
def test_vietnamese_round_trip_and_header_safety():
    async def go():
        al = await alice(); subj = "Xác nhận lịch hẹn – chị Nguyễn Thị Hương ✓"; body = "Chào chị Hương,\nEm xác nhận 9 giờ sáng thứ Sáu tại 12 Lê Lợi, Quận 1.\nĐơn giá: 1.250.000đ"
        out = await call(al, "gmail_send", to=["Nguyễn Hương <huong@khach.vn>", "lan@khach.vn"], cc=["sep@congty.vn"], bcc=["kt@congty.vn"], subject=subj, body=body)
        assert out.startswith("Email sent"), out
        s = last_sent(); raw = s["raw_bytes"]
        raw.decode("ascii")                                                                                       # the whole message is 7-bit clean
        assert b"=?utf-8?" in raw.split(b"\r\n\r\n")[0].lower() and "Xác" not in raw.split(b"\r\n\r\n")[0].decode()   # subject + name are RFC 2047 encoded in the header
        assert s["subject"] == subj and "huong@khach.vn" in s["to"] and "Nguy" in s["to"] and "lan@khach.vn" in s["to"] and s["cc"] == "sep@congty.vn" and s["bcc"] == "kt@congty.vn"
        assert s["text"].replace(chr(13), "").strip() == body                                                                        # body survives (UTF-8)
        # reading an RFC 2047 header decodes it (Gmail returns the header as stored)
        enc = "=?UTF-8?B?" + base64.b64encode("Báo giá tháng 11".encode()).decode() + "?= =?UTF-8?Q?_=E2=9C=93?="
        F.deliver("alice", "=?UTF-8?B?" + base64.b64encode("Trần Văn Bình".encode()).decode() + "?= <binh@khach.vn>", enc, "nội dung")
        r = json.loads(await call(al, "gmail_search", query="nội dung"))
        assert r[0]["subject"] == "Báo giá tháng 11 ✓" and r[0]["from"].startswith("Trần Văn Bình"), r
        # injection: line breaks in a header value never create a header
        bad = await call(al, "gmail_send", to=["a@b.vn\r\nBcc: evil@x.vn"], subject="s", body="b")
        assert bad.startswith("Error") and len(F.S["sent"]) == 1
        await call(al, "gmail_send", to=["a@b.vn"], subject="Hi\r\nBcc: evil@x.vn", body="b")
        assert last_sent()["bcc"] == "" and "Bcc" not in last_sent()["headers"] and last_sent()["subject"] == "Hi Bcc: evil@x.vn"
        assert (await call(al, "gmail_send", to=["not-an-address"], subject="s", body="b")).startswith("Error") and len(F.S["sent"]) == 2
        assert (await call(al, "gmail_send", to=[], subject="s", body="b")).startswith("Error")
    run(go)

def test_html_body_has_plain_alternative():
    async def go():
        al = await alice()
        await call(al, "gmail_send", to=["a@b.vn"], subject="Thư HTML", body="<p>Xin chào <b>chị Lan</b></p>", body_format="html")
        raw = last_sent()["raw_bytes"]; assert b"multipart/alternative" in raw and b"text/html" in raw and b"text/plain" in raw
        assert (await call(al, "gmail_send", to=["a@b.vn"], subject="s", body="b", body_format="rtf")).startswith("Error")
    run(go)

def test_reply_lands_in_the_right_thread_with_reply_headers():
    async def go():
        al = await alice()
        m = F.deliver("alice", "Lan <lan@khach.vn>", "Hẹn gặp thứ Sáu", "Chị Lan muốn gặp.", thread="t-lan", headers={"Message-ID": "<orig1@khach.vn>", "References": "<r0@khach.vn>"},
                      to="alice@fake.test, sep@congty.vn")
        F.box("alice")[-1]["headers"]["Cc"] = "ketoan@congty.vn"
        out = await call(al, "gmail_reply", message_id=m["id"], body="Dạ em xác nhận ạ.")
        assert "thread t-lan" in out, out
        s = last_sent(); h = {k.lower(): v for k, v in s["headers"].items()}
        assert s["thread_id"] == "t-lan" and h["in-reply-to"] == "<orig1@khach.vn>" and h["references"] == "<r0@khach.vn> <orig1@khach.vn>"
        assert s["subject"] == "Re: Hẹn gặp thứ Sáu" and "lan@khach.vn" in s["to"] and s["cc"] == "" and "Dạ em xác nhận" in s["text"]
        th = json.loads(await call(al, "gmail_get_thread", thread_id="t-lan"))                                       # the reply is in the same conversation
        assert [x["id"] for x in th["messages"]][0] == m["id"] and len(th["messages"]) == 2 and "Dạ em xác nhận" in th["messages"][1]["body"]
        # reply all: everybody else but me; no double "Re: "
        m2 = F.box("alice")[-1]; m2.update(subject="Re: Hẹn gặp thứ Sáu", frm="Lan <lan@khach.vn>", labels=["INBOX"], to="alice@fake.test, sep@congty.vn"); m2["headers"]["Cc"] = "ketoan@congty.vn"
        await call(al, "gmail_reply", message_id=m2["id"], body="Cả nhà xem giúp.", reply_all=True)
        s = last_sent(); assert s["subject"] == "Re: Hẹn gặp thứ Sáu" and "lan@khach.vn" in s["to"]
        assert "sep@congty.vn" in s["cc"] and "ketoan@congty.vn" in s["cc"] and "alice@fake.test" not in s["cc"] + s["to"]
        # a message without Message-ID: still the same thread, no In-Reply-To
        await call(al, "gmail_reply", message_id="m-a2", body="ok")
        s = last_sent(); assert s["thread_id"] == "tm-a2" and "In-Reply-To" not in s["headers"] and s["subject"] == "Re: Hẹn gặp thứ Sáu"
        assert (await call(al, "gmail_reply", message_id="no-such", body="x")).startswith("Error")
    run(go)

def test_draft_reply_send_draft_and_list():
    async def go():
        al = await alice()
        out = await call(al, "gmail_draft", reply_to_message_id="m-a2", body="Em sẽ có mặt lúc 9 giờ.")
        assert out.startswith("Draft created"), out
        d = F.S["drafts"][-1]; msg = d["body"]["message"]; assert msg["threadId"] == "tm-a2"
        dm = F.box("alice")[-1]; assert dm["draft"] and dm["subject"] == "Re: Hẹn gặp thứ Sáu" and "lan@khach.vn" in dm["to"]
        lst = json.loads(await call(al, "gmail_list_drafts")); assert lst[0]["draft_id"] == d["draft_id"] and lst[0]["subject"] == "Re: Hẹn gặp thứ Sáu"
        assert (await call(al, "gmail_draft", subject="s", body="b")).startswith("Error")                              # no recipient
        assert not F.S["sent"]
        out = await call(al, "gmail_send_draft", draft_id=d["draft_id"]); assert out.startswith("Draft sent") and F.S["sent"][-1]["from_draft"]
        assert (await call(al, "gmail_send_draft", draft_id="nope")).startswith("Error")
    run(go)

def test_attachments_from_chat_files_and_the_computer(monkeypatch):
    async def go():
        al = await alice()
        f = await as_(al, AT.store, "Bảng giá tháng 11: 1.000.000đ\n".encode(), "Bảng giá.txt"); fid = f["id"]
        box = FBox({"/workspace/out/bao-cao.txt": "Báo cáo\n".encode(), "/workspace/out/macro.docm": docx(), "/workspace/out/trojan.docx": docx(vba=True),
                    "/workspace/out/big1.txt": b"a" * (11 * 1024 * 1024), "/workspace/out/big2.txt": b"b" * (11 * 1024 * 1024), "/workspace/out/big.txt": b"x" * 4_500_000})
        with_box(monkeypatch, box)
        out = await call(al, "gmail_send", to=["a@b.vn"], subject="Có đính kèm", body="Xem file", attachments=[fid, "/workspace/out/bao-cao.txt"])
        assert out.startswith("Email sent"), out
        names = [a["name"] for a in last_sent()["atts"]]; assert names == ["Bảng giá.txt", "bao-cao.txt"], names
        assert last_sent()["atts"][0]["data"].decode().startswith("Bảng giá tháng 11") and last_sent()["atts"][1]["data"] == "Báo cáo\n".encode() and not last_sent().get("via")
        # refused BEFORE any API call: macros (by extension and by content), > 20 MB in total, unknown places, no computer
        n = len(api_calls()); sent = len(F.S["sent"])
        for ref, word in (("/workspace/out/macro.docm", "macro"), ("/workspace/out/trojan.docx", "macro"), (["/workspace/out/big1.txt", "/workspace/out/big2.txt"], "20 MB"),
                          ("/etc/passwd", "/workspace/"), ("/workspace/out/missing.txt", "cannot read"), ("00000000-0000-4000-8000-000000000000", "not found")):
            r = await call(al, "gmail_send", to=["a@b.vn"], subject="s", body="b", attachments=ref if isinstance(ref, list) else [ref])
            assert r.startswith("Error") and word in r, (ref, r)
        assert len(api_calls()) == n and len(F.S["sent"]) == sent
        r = await call(al, "gmail_draft", to=["a@b.vn"], subject="s", attachments=["/workspace/out/macro.docm"]); assert "macro" in r and len(api_calls()) == n
        # another person's chat file is not theirs to attach
        await connect("bob", SEEDS["bob"], WB)
        r = await call(U(WB, SEEDS["bob"]), "gmail_send", to=["a@b.vn"], subject="s", body="b", attachments=[fid])
        assert r.startswith("Error") and len([s for s in F.S["sent"] if s["who"] == "bob"]) == 0, r
        # a big mail goes through /upload/, a small one does not
        await call(al, "gmail_send", to=["a@b.vn"], subject="Tệp lớn", body="b", attachments=["/workspace/out/big.txt"])
        s = last_sent(); assert s.get("via") == "upload" and s["atts"][0]["data"] == b"x" * 4_500_000 and s["subject"] == "Tệp lớn"
        await call(al, "gmail_draft", to=["a@b.vn"], subject="Nháp lớn", body="b", attachments=["/workspace/out/big.txt"]); assert F.S["upload_drafts"] == 1 and F.box("alice")[-1]["atts"][0]["name"] == "big.txt"
        await call(al, "gmail_send", to=["a@b.vn"], subject="nhỏ", body="b"); assert not last_sent().get("via")
        r = await call(al, "gmail_send", to=["a@b.vn"], subject="s", body="b", attachments=["/workspace/out/bao-cao.txt"] * 11); assert r.startswith("Error") and "too many" in r
    run(go)

def test_get_attachment_saves_to_chat_and_the_computer(monkeypatch):
    async def go():
        al = await alice()
        m = F.deliver("alice", "Lan <lan@khach.vn>", "Báo giá", "Xem file đính kèm", atts=[{"id": "att-0", "name": "Báo giá.txt", "mime": "text/plain", "data": "Giá: 1.000.000đ".encode()},
                                                                                         {"id": "att-1", "name": "run.exe", "mime": "application/octet-stream", "data": b"MZ\x90\x00"}])
        rd = json.loads(await call(al, "gmail_read", message_id=m["id"]))
        assert [(a["attachment_id"], a["filename"]) for a in rd["attachments"]] == [("att-0", "Báo giá.txt"), ("att-1", "run.exe")]
        r = json.loads(await call(al, "gmail_get_attachment", message_id=m["id"], attachment_id="att-0"))                # no computer: chat file only
        assert r["chat_file"]["id"] and "computer_path" not in r
        row = await as_(al, AT._row, r["chat_file"]["id"]); assert AT._bytes(row).decode() == "Giá: 1.000.000đ"
        box = FBox(); with_box(monkeypatch, box)
        r = json.loads(await call(al, "gmail_get_attachment", message_id=m["id"], attachment_id="att-0"))
        assert r["computer_path"].startswith("/workspace/uploads/mail_") and r["computer_path"].endswith("Báo giá.txt") and box.files[r["computer_path"]].decode() == "Giá: 1.000.000đ"
        r = json.loads(await call(al, "gmail_get_attachment", message_id=m["id"], attachment_id="att-1"))                # not a chat-file type, but the computer can keep it
        assert "chat_file" not in r and r["chat_file_error"] and r["computer_path"].endswith("run.exe")
        with_box(monkeypatch, None)
        assert (await call(al, "gmail_get_attachment", message_id=m["id"], attachment_id="att-1")).startswith("Error")   # nowhere to keep it
        assert (await call(al, "gmail_get_attachment", message_id=m["id"], attachment_id="att-9")).startswith("Error")
    run(go)

def test_forward_keeps_attachments():
    async def go():
        al = await alice()
        m = F.deliver("alice", "Kế toán <ketoan@congty.vn>", "Báo cáo doanh thu quý 3", "Doanh thu quý 3: 3,5 tỷ.", atts=[{"id": "att-0", "name": "doanh-thu.csv", "mime": "text/csv", "data": "thang,tien\n7,1\n".encode()}])
        out = await call(al, "gmail_forward", message_id=m["id"], to=["sep@congty.vn"], body="Anh xem giúp em.")
        assert out.startswith("Email forwarded"), out
        s = last_sent(); assert s["subject"] == "Fwd: Báo cáo doanh thu quý 3" and "sep@congty.vn" in s["to"] and s["thread_id"] is None
        assert "Anh xem giúp em." in s["text"] and "Forwarded message" in s["text"] and "ketoan@congty.vn" in s["text"] and "Doanh thu quý 3: 3,5 tỷ." in s["text"]
        assert [(a["name"], a["data"]) for a in s["atts"]] == [("doanh-thu.csv", "thang,tien\n7,1\n".encode())]
        await call(al, "gmail_forward", message_id=m["id"], to=["sep@congty.vn"], include_attachments=False); assert last_sent()["atts"] == []
        await call(al, "gmail_forward", message_id="m-a1", to=["sep@congty.vn"]); assert last_sent()["subject"] == "Fwd: Báo cáo doanh thu tháng 10"
        assert (await call(al, "gmail_forward", message_id=m["id"], to=[])).startswith("Error")
    run(go)

def test_labels_archive_mark_read_trash():
    async def go():
        al = await alice()
        labs = json.loads(await call(al, "gmail_list_labels")); assert {"INBOX", "UNREAD"} <= {x["id"] for x in labs}
        assert "Label created" in await call(al, "gmail_create_label", name="Khách hàng VIP"); assert (await call(al, "gmail_create_label", name="Khách hàng VIP")).startswith("Error")
        labs = json.loads(await call(al, "gmail_list_labels")); lid = next(x["id"] for x in labs if x["name"] == "Khách hàng VIP")
        out = await call(al, "gmail_label", message_id="m-a1", add=["khách hàng vip"], remove=["UNREAD"])                  # a name (any case) or an id
        assert lid in out and "UNREAD" not in out and F.S["modified"][-1]["add"] == [lid] and F.S["modified"][-1]["remove"] == ["UNREAD"]
        assert (await call(al, "gmail_label", message_id="m-a1", add=["Không có"])).startswith("Error")
        assert (await call(al, "gmail_label", message_id="m-a1")).startswith("Error")
        for bad in ("TRASH", "SPAM"):                                                                                      # the ☆ label tool must not do what the ★ trash tool does
            r = await call(al, "gmail_label", message_id="m-a1", add=[bad]); assert r.startswith("Error") and "gmail_trash" in r
        assert "TRASH" not in next(m for m in F.box("alice") if m["id"] == "m-a1")["labels"]
        await call(al, "gmail_mark_read", message_id="m-a2"); assert "UNREAD" not in next(m for m in F.box("alice") if m["id"] == "m-a2")["labels"]
        await call(al, "gmail_mark_read", message_id="m-a2", read=False); assert "UNREAD" in next(m for m in F.box("alice") if m["id"] == "m-a2")["labels"]
        await call(al, "gmail_archive", message_id="m-a2"); assert "INBOX" not in next(m for m in F.box("alice") if m["id"] == "m-a2")["labels"]
        assert "trash" in await call(al, "gmail_trash", message_id="m-a1") and "TRASH" in next(m for m in F.box("alice") if m["id"] == "m-a1")["labels"]
        assert not [m for m in json.loads(await call(al, "gmail_search", query="doanh thu"))]                              # trashed mail is not found any more
        # bob's mailbox is not alice's
        await connect("bob", SEEDS["bob"], WB)
        assert (await call(U(WB, SEEDS["bob"]), "gmail_archive", message_id="m-a2")).startswith("Error")
    run(go)

def test_read_tools():
    async def go():
        al = await alice()
        ts = json.loads(await call(al, "gmail_search_threads", query="in:inbox")); assert {t["thread_id"] for t in ts} == {"tm-a1", "tm-a2"} and ts[0]["unread"] and ts[0]["messages"] == 1
        assert ts[0]["subject"] == "Báo cáo doanh thu tháng 10" and ts[0]["from"].startswith("Kế toán")
        th = json.loads(await call(al, "gmail_get_thread", thread_id="tm-a2")); assert th["messages"][0]["body"].startswith("Chị Lan") and th["messages"][0]["subject"] == "Hẹn gặp thứ Sáu"
        assert (await call(al, "gmail_get_thread", thread_id="nope")).startswith("Error")
        rd = json.loads(await call(al, "gmail_read", message_id="m-a2")); assert rd["thread_id"] == "tm-a2" and "INBOX" in rd["labels"] and rd["attachments"] == []
        await connect("bob", SEEDS["bob"], WB)
        assert "BOB-SECRET" not in await call(al, "gmail_search_threads", query="BOB-SECRET")
    run(go)

def test_gmail_scopes_and_tiers():
    async def go():
        al = U(WA, SEEDS["alice"]); F.S["enforce_scopes"] = True
        await connect("alice", SEEDS["alice"], WA, scope=" ".join(OS.required("google", "read")))                        # a read-mode grant
        assert json.loads(await call(al, "gmail_get_thread", thread_id="tm-a1")) and json.loads(await call(al, "gmail_list_labels"))
        j = json.loads(await call(al, "gmail_send", to=["a@b.vn"], subject="s", body="b")); assert j["auth_required"] and j["auth_id"] == "scope_missing" and not F.S["sent"]
        await connect("alice", SEEDS["alice"], WA)                                                                        # write mode: gmail.modify only, no gmail.send / mail.google.com
        assert F.S["tokens"] and all("gmail.send" not in v["scope"] and "https://mail.google.com/" not in v["scope"] for v in F.S["tokens"].values())
        assert (await call(al, "gmail_send", to=["a@b.vn"], subject="s", body="b")).startswith("Email sent")
    run(go)

def test_gmail_tier_table_and_cards():
    A_, N, R = W.ALWAYS_ASK, W.DEFAULT_ASK, W.READ
    want = {"gmail_search": R, "gmail_read": R, "gmail_search_threads": R, "gmail_get_thread": R, "gmail_list_labels": R, "gmail_list_drafts": R, "gmail_get_attachment": R,
            "gmail_draft": N, "gmail_label": N, "gmail_create_label": N, "gmail_mark_read": N, "gmail_archive": N,
            "gmail_send": A_, "gmail_send_draft": A_, "gmail_reply": A_, "gmail_forward": A_, "gmail_trash": A_}
    for n, t in want.items(): assert W.tier_of(n, {}) == t, n
    auto = frozenset(W.META["google:" + n].canonical for n in want if want[n] == N) | {"google:gmail_send", "google:gmail_trash"}   # the stars in the set are ignored
    for n, t in want.items():
        assert W.needs_approval(n, {}, auto) is (t == A_), n
    assert not [k for k in W._auto_keys({"auto_tools": ["google:gmail_send", "google:gmail_reply", "google:gmail_forward", "google:gmail_trash", "google:gmail_send_draft"]})]
    cfg = W.interrupt_config(list(want), set(), WA)
    assert all("when" not in cfg[n] for n in want if want[n] == A_) and all("when" in cfg[n] for n in want if want[n] == N) and not [n for n in want if want[n] == R and n in cfg]
    card = W.describe_call("gmail_send", {"to": ["lan@khach.vn"], "cc": ["sep@congty.vn"], "bcc": ["kt@congty.vn"], "subject": "Xác nhận", "body": "Chào chị Lan", "attachments": ["/workspace/out/bao-gia.pdf"]})
    for x in ("To: lan@khach.vn", "Cc: sep@congty.vn", "Bcc: kt@congty.vn", "Subject: Xác nhận", "Chào chị Lan", "bao-gia.pdf"): assert x in card, card
    card = W.describe_call("gmail_reply", {"message_id": "m-1", "body": "ok", "reply_all": True}); assert "everyone on message m-1" in card
    assert "Attachments of the original" in W.describe_call("gmail_forward", {"message_id": "m-1", "to": ["a@b.vn"]})
    assert "Draft: d-1" in W.describe_call("gmail_send_draft", {"draft_id": "d-1"})
    for n in want: assert W.META["google:" + n].name == n and W.LABELS["google:" + n] and W.SPECS["google:" + n][1]

# ================================================================== GOOGLE CALENDAR (11.57)
def T(h, m=0, d=26): return dt.datetime(2026, 10, d, h, m, tzinfo=VN)

def test_suggest_slots_interval_intersection():
    busy = [(T(9), T(10)), (T(9, 30), T(11)), (T(14), T(15)), (T(16), T(17, 30))]                                          # overlapping busy intervals of several people
    got = G.suggest_slots(busy, T(0), T(23, d=27), 60, limit=6)
    assert [(a.hour, a.minute, b.hour, b.minute, a.day) for a, b in got] == [(8, 0, 9, 0, 26), (11, 0, 12, 0, 26), (12, 0, 13, 0, 26), (13, 0, 14, 0, 26), (15, 0, 16, 0, 26), (8, 0, 9, 0, 27)]
    for a, b in G.suggest_slots(busy, T(0), T(23, d=29), 45, limit=30):                                                    # every slot: in working hours, free of everybody, on weekdays
        assert T(8, d=a.day) <= a and b <= T(17, 30, d=a.day) and a.weekday() < 5 and not any(x < b and y > a for x, y in busy)
    assert G.suggest_slots([(T(8), T(17, 30))], T(0), T(23), 30) == []                                                      # busy all day: nothing that day
    assert [a.hour for a, _ in G.suggest_slots([], T(10, 10), T(23), 30, limit=1)] == [10] and G.suggest_slots([], T(10, 10), T(23), 30, limit=1)[0][0].minute == 30   # grid: next :30
    sat = dt.datetime(2026, 10, 31, 0, 0, tzinfo=VN); assert G.suggest_slots([], sat, sat + dt.timedelta(days=1), 30) == [] and G.suggest_slots([], sat, sat + dt.timedelta(days=1), 30, weekends=True)
    assert G.suggest_slots([], T(0), T(23), 600) == [] and G.suggest_slots([], T(12), T(13), 90) == []                      # does not fit
    assert G.suggest_slots([], dt.datetime(2026, 10, 26, 3, 0, tzinfo=dt.timezone.utc), T(23), 30)[0][0] == T(10, 0)       # an instant given in UTC is read in VN time (03:00Z = 10:00 VN)

def test_calendar_read_tools_and_free_busy():
    async def go():
        al = await alice()
        cals = json.loads(await call(al, "gcal_list_calendars")); assert cals[0]["primary"] and len(cals) == 2 and cals[1]["id"].startswith("team-")
        ev = json.loads(await call(al, "gcal_list", start="2026-10-26T00:00:00+07:00", days=1, calendar_id=cals[1]["id"]))
        assert ev[0]["summary"] == "Họp giao ban tuần" and any("team-alice@group.calendar.fake.test" in c[3] for c in F.S["log"]), F.S["log"]
        assert json.loads(await call(al, "gcal_list", start="2026-11-20T00:00:00+07:00", days=2)) == []                      # outside the range
        g = json.loads(await call(al, "gcal_get", event_id="e-a1")); assert g["summary"] == "Họp giao ban tuần" and g["start"].startswith("2026-10-26T09:00")
        assert (await call(al, "gcal_get", event_id="nope")).startswith("Error")
        fb = json.loads(await call(al, "gcal_free_busy", emails=["bob@fake.test", "ghost@fake.test"], start="2026-10-26T00:00:00+07:00", days=2))
        assert fb["busy"]["primary"][0]["start"].startswith("2026-10-26T09:00") and fb["busy"]["bob@fake.test"][0]["start"].startswith("2026-10-27T14:00") and fb["not_visible"] == ["ghost@fake.test"]
        assert {i["id"] for i in F.S["freebusy_calls"][-1]["body"]["items"]} == {"primary", "bob@fake.test", "ghost@fake.test"} and F.S["freebusy_calls"][-1]["body"]["timeZone"] == "Asia/Ho_Chi_Minh"
        # read-mode grant: calendarlist + freebusy scopes are in it
        F.S["enforce_scopes"] = True; await connect("alice", SEEDS["alice"], WA, scope=" ".join(OS.required("google", "read")))
        assert json.loads(await call(al, "gcal_free_busy", emails=["bob@fake.test"], start="2026-10-26T00:00:00+07:00")) and json.loads(await call(al, "gcal_list_calendars"))
        j = json.loads(await call(al, "gcal_delete", event_id="e-a1")); assert j["auth_required"] and not F.S["deleted"]
    run(go)

def test_suggest_time_on_the_fake_api():
    async def go():
        al = await alice()
        r = json.loads(await call(al, "gcal_suggest_time", attendees=["bob@fake.test"], duration_minutes=60, start="2026-10-26T08:30:00+07:00", days=2, max_suggestions=5))
        sl = [(dt.datetime.fromisoformat(s["start"]), dt.datetime.fromisoformat(s["end"])) for s in r["suggestions"]]
        assert r["timezone"] == "Asia/Ho_Chi_Minh" and len(sl) == 5 and not r["not_visible"], r
        busy = [(T(9), T(10)), (T(14, d=27), T(15, d=27))]                                                                # alice 26/10 09-10 and bob 27/10 14-15 (PEOPLE in the fake)
        assert (sl[0][0].hour, sl[0][0].minute) == (10, 0)                                                                # 08:30-09:30 clashes with alice's meeting
        for a, b in sl: assert T(8, d=a.day) <= a and b <= T(17, 30, d=a.day) and b - a == dt.timedelta(hours=1) and not any(x < b and y > a for x, y in busy), (a, b)
        # a calendar nobody shared is reported, never silently counted as free
        r = json.loads(await call(al, "gcal_suggest_time", attendees=["ghost@fake.test"], duration_minutes=30, start="2026-10-26T00:00:00+07:00", days=1))
        assert r["not_visible"] == ["ghost@fake.test"] and "NOT considered" in r["warning"] and r["suggestions"]
        assert json.loads(await call(al, "gcal_suggest_time", duration_minutes=30, start="2026-10-31T00:00:00+07:00", days=1))["suggestions"] == []   # Saturday
        for bad in ({"duration_minutes": 1}, {"work_start": "25:00"}, {"work_start": "18:00", "work_end": "09:00"}): assert (await call(al, "gcal_suggest_time", **bad)).startswith("Error")
    run(go)

def test_create_update_delete_respond_and_invitations():
    async def go():
        al = await alice()
        out = await call(al, "gcal_create", summary="Gặp chị Lan", start="2026-10-30T09:00:00+07:00", end="2026-10-30T10:00:00+07:00", location="12 Lê Lợi")
        assert out.startswith("Event created") and "invitations" not in out and F.S["created"][-1]["query"] == {} and "attendees" not in F.S["created"][-1]["body"]
        out = await call(al, "gcal_create", summary="Họp dự án", start="2026-10-30T14:00:00+07:00", end="2026-10-30T15:00:00+07:00", attendees=["bob@fake.test", "lan@khach.vn"], calendar_id="primary")
        c = F.S["created"][-1]; assert "invitations sent to bob@fake.test, lan@khach.vn" in out and c["query"] == {"sendUpdates": "all"} and [a["email"] for a in c["body"]["attendees"]] == ["bob@fake.test", "lan@khach.vn"]
        assert (await call(al, "gcal_create", summary="x", start="2026-10-30T14:00:00+07:00", end="2026-10-30T15:00:00+07:00", attendees=["bad"])).startswith("Error")
        assert any(e["summary"] == "Họp dự án" for e in json.loads(await call(al, "gcal_list", start="2026-10-30T00:00:00+07:00", days=1)))   # a created event is listed
        eid = next(k for k, v in F._evs("alice").items() if v.get("summary") == "Họp dự án")
        out = await call(al, "gcal_update", event_id=eid, start="2026-10-30T15:00:00+07:00", end="2026-10-30T16:00:00+07:00", location="Phòng 3")
        p = F.S["patched"][-1]; assert out.startswith("Event updated") and p["id"] == eid and p["query"] == {"sendUpdates": "all"} and p["body"]["location"] == "Phòng 3" and "summary" not in p["body"]
        await call(al, "gcal_update", event_id=eid, attendees=["bob@fake.test"], send_updates="none"); p = F.S["patched"][-1]; assert p["query"] == {"sendUpdates": "none"} and p["body"] == {"attendees": [{"email": "bob@fake.test"}]}
        for bad in ({}, {"send_updates": "maybe", "summary": "x"}): assert (await call(al, "gcal_update", event_id=eid, **bad)).startswith("Error")
        assert (await call(al, "gcal_update", event_id="nope", summary="x")).startswith("Error")
        assert "deleted" in await call(al, "gcal_delete", event_id=eid) and F.S["deleted"][-1]["id"] == eid and F.S["deleted"][-1]["query"] == {"sendUpdates": "all"}
        assert (await call(al, "gcal_delete", event_id=eid)).startswith("Error")                                          # already gone
        # answering an invitation changes only my own row of the guest list
        F._evs("alice")["e-inv"] = {"id": "e-inv", "summary": "Họp khách", "start": {"dateTime": "2026-10-28T09:00:00+07:00"}, "end": {"dateTime": "2026-10-28T10:00:00+07:00"},
                                    "attendees": [{"email": "bob@fake.test", "responseStatus": "accepted", "organizer": True}, {"email": "alice@fake.test", "self": True, "responseStatus": "needsAction"}]}
        out = await call(al, "gcal_respond", event_id="e-inv", response="accepted", comment="Em sẽ đến")
        gl = F._evs("alice")["e-inv"]["attendees"]; assert out.startswith("Answered") and gl[1]["responseStatus"] == "accepted" and gl[1]["comment"] == "Em sẽ đến" and gl[0]["responseStatus"] == "accepted" and len(gl) == 2
        assert F.S["responded"][-1]["id"] == "e-inv" and F.S["responded"][-1]["query"] == {"sendUpdates": "all"}
        await call(al, "gcal_respond", event_id="e-inv", response="declined", send_updates="none"); assert F._evs("alice")["e-inv"]["attendees"][1]["responseStatus"] == "declined" and F.S["responded"][-1]["query"] == {"sendUpdates": "none"}
        assert (await call(al, "gcal_respond", event_id="e-inv", response="maybe")).startswith("Error")
        assert "not on the guest list" in await call(al, "gcal_respond", event_id="e-a1", response="accepted")                 # an event with no guest list
    run(go)

def test_calendar_tiers_and_cards():
    A_, N, R = W.ALWAYS_ASK, W.DEFAULT_ASK, W.READ
    for n in ("gcal_list_calendars", "gcal_list", "gcal_get", "gcal_free_busy", "gcal_suggest_time"): assert W.tier_of(n, {}) == R, n
    for n in ("gcal_update", "gcal_delete", "gcal_respond"): assert W.tier_of(n, {}) == A_, n                              # editing an event ALWAYS goes to the approval card
    assert W.tier_of("gcal_create", {}) == N and W.tier_of("gcal_create", {"attendees": ["a@b.vn"]}) == A_
    auto = frozenset({"google:gcal_create", "google:gcal_update", "google:gcal_delete", "google:gcal_respond"})
    assert W.needs_approval("gcal_create", {}, auto) is False and W.needs_approval("gcal_create", {"attendees": ["a@b.vn"]}, auto) is True
    for n in ("gcal_update", "gcal_delete", "gcal_respond"): assert W.needs_approval(n, {"event_id": "e"}, auto) is True, n
    assert W._auto_keys({"auto_tools": list(auto)}) == ["google:gcal_create"]                                               # the stars cannot be put on the automatic list
    cfg = W.interrupt_config(["gcal_create", "gcal_update", "gcal_delete", "gcal_respond", "gcal_list"], set(), WA)
    assert "when" in cfg["gcal_create"] and all("when" not in cfg[n] for n in ("gcal_update", "gcal_delete", "gcal_respond")) and "gcal_list" not in cfg
    card = W.describe_call("gcal_create", {"summary": "Họp dự án", "start": "2026-10-30T14:00:00+07:00", "end": "2026-10-30T15:00:00+07:00", "attendees": ["bob@fake.test", "lan@khach.vn"]})
    assert "These people will receive an invitation email: bob@fake.test, lan@khach.vn" in card and "Họp dự án" in card, card
    assert "Only the user's own calendar" in W.describe_call("gcal_create", {"summary": "Nhắc việc"})
    card = W.describe_call("gcal_update", {"event_id": "e1", "attendees": ["x@y.vn"]}); assert "These people will receive an invitation email: x@y.vn" in card
    assert "guest list is emailed" in W.describe_call("gcal_update", {"event_id": "e1", "summary": "đổi tên"}) and "No email is sent" in W.describe_call("gcal_delete", {"event_id": "e1", "send_updates": "none"})
    assert "cancellation" in W.describe_call("gcal_delete", {"event_id": "e1"}) and "organizer is emailed" in W.describe_call("gcal_respond", {"event_id": "e1", "response": "declined"})
    for k in ("gcal_free_busy", "gcal_suggest_time"): assert OS.G + "calendar.freebusy" in W.BY_NAME[k].scopes
    assert OS.G + "calendar.calendarlist.readonly" in W.BY_NAME["gcal_list_calendars"].scopes
