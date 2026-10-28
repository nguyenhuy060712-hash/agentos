"""GĐ8.2 (23/10/2026): Telegram adapter (app/channel_telegram.py, aiogram 3.31). Pure unit tests (Markdown -> Telegram HTML, 4096 split,
group mention parsing, callback_data size, advisory lock) + end-to-end against the local server :8020, whose pollers talk to the fake Bot API
below (aiohttp on 127.0.0.1:8781 = TELEGRAM_API_BASE set by overlay/run_local.py with TELEGRAM_MOCK=1). Fake LLM => $0, no real network.
Server tests run in file order and share state through the `tg` fixture."""
import asyncio, base64, json, os, random, sys, threading, time, uuid
import httpx, psycopg, pytest
from aiohttp import web
from test_fixes import B, ADMIN, a  # noqa: F401  (fixtures)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
PORT = 8781; PW = "phase0-gd82-local"; SUF = uuid.uuid4().hex[:6]
TOKEN = f"{random.randint(10**8, 10**9)}:AAtest{SUF}_fake-token"
ME = {"id": 424242, "is_bot": True, "first_name": "Shop", "username": "shop_test_bot"}
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

def pg(): return psycopg.connect(PG, autocommit=True)

# ------------------------------------------------------------------ fake Bot API
class FakeBotAPI:
    """Minimal Telegram Bot API: records every call; serves getUpdates (per token), getFile and file downloads."""
    def __init__(self):
        self.calls, self.updates, self.files, self.seq, self.mid = [], {}, {}, 1, 5000

    def push(self, token, upd):
        upd = {"update_id": self.seq, **upd}; self.seq += 1
        self.updates.setdefault(token, []).append(upd); return upd

    def msg(self, chat, text, **extra):
        self.mid += 1
        return {"message_id": self.mid, "date": int(time.time()), "chat": chat, **({"text": text} if text is not None else {}), **extra}

    async def api(self, req):
        tok, m = req.match_info["token"], req.match_info["method"].lower()
        data = dict(await req.post()); rec = {"token": tok, "method": m, **data}; self.calls.append(rec)
        ok = lambda r: web.json_response({"ok": True, "result": r})
        if m == "getme": return ok(ME)
        if m == "getupdates":
            off, t0 = int(data.get("offset") or 0), time.time()
            pend = lambda: [u for u in self.updates.get(tok, []) if u["update_id"] >= off]
            while not pend() and time.time() - t0 < min(float(data.get("timeout") or 0), 1.0): await asyncio.sleep(0.05)
            return ok(pend())
        if m in ("sendmessage", "editmessagetext"):
            cid = int(data["chat_id"]); r = self.msg({"id": cid, "type": "private" if cid > 0 else "supergroup", **({} if cid > 0 else {"title": "Nhóm"})}, data.get("text"))
            if data.get("reply_markup"): r["reply_markup"] = json.loads(data["reply_markup"])
            rec["_result"] = r; return ok(r)
        if m in ("answercallbackquery", "sendchataction"): return ok(True)
        if m == "getfile":
            f = self.files.get(data.get("file_id"))
            if not f: return web.json_response({"ok": False, "error_code": 400, "description": "Bad Request: invalid file_id"}, status=400)
            return ok({"file_id": data["file_id"], "file_unique_id": "u" + data["file_id"], "file_size": len(f[1]), "file_path": f[0]})
        return web.json_response({"ok": False, "error_code": 404, "description": "Not Found"}, status=404)

    async def file(self, req):
        for p, data in self.files.values():
            if p == req.match_info["path"]: return web.Response(body=data)
        return web.Response(status=404)

    def start(self):
        self.loop = asyncio.new_event_loop(); ready = threading.Event()
        def run():
            asyncio.set_event_loop(self.loop)
            app = web.Application(client_max_size=50 << 20)
            app.router.add_post("/bot{token}/{method}", self.api); app.router.add_get("/file/bot{token}/{path:.+}", self.file)
            self.runner = web.AppRunner(app); self.loop.run_until_complete(self.runner.setup())
            self.loop.run_until_complete(web.TCPSite(self.runner, "127.0.0.1", PORT).start()); ready.set(); self.loop.run_forever()
        threading.Thread(target=run, daemon=True).start(); assert ready.wait(10)

    def stop(self):
        asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop).result(10); self.loop.call_soon_threadsafe(self.loop.stop)

    def sent(self, method, chat=None, token=TOKEN):
        return [c for c in list(self.calls) if c["token"] == token and c["method"] == method and (chat is None or str(c.get("chat_id")) == str(chat))]

def wait_for(fn, timeout=90, every=0.2):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = fn()
        if r: return r
        time.sleep(every)
    raise AssertionError("timed out waiting")

def user_of(uid): return {"id": uid, "is_bot": False, "first_name": "Khách", "username": f"khach{uid}"}

# ------------------------------------------------------------------ pure unit tests
def test_markdown_to_safe_html():
    import channel_telegram as t
    h = t.md_to_html("# Báo giá\n**đậm** *nghiêng* <script>alert(1)</script> & `a<b>` [web](https://x.vn/?a=1&b=2) [xấu](javascript:alert(1))\n- mục 1\n```py\nif a<b: pass\n```")
    assert "<b>Báo giá</b>" in h and "<b>đậm</b>" in h and "<i>nghiêng</i>" in h and "• mục 1" in h
    assert "&lt;script&gt;" in h and "<script" not in h and "&amp;" in h and "<code>a&lt;b&gt;</code>" in h
    assert '<a href="https://x.vn/?a=1&amp;b=2">web</a>' in h and "javascript" in h and 'href="javascript' not in h
    assert "<pre>if a&lt;b: pass</pre>" in h
    import re
    assert set(re.findall(r"</?(\w+)", h)) <= {"b", "i", "code", "pre", "a"}
    assert t.md_to_html("*a **b* c**").count("<") == t.md_to_html("*a **b* c**").count(">")   # never crossing tags

def test_split_html_4096_and_balanced():
    import re, channel_telegram as t
    h = t.md_to_html("**" + "chữ đậm " * 900 + "**\n\n```\n" + "code dòng\n" * 600 + "```")
    parts = t.split_html(h)
    assert len(parts) >= 3 and all(t._u16(p) <= 4096 for p in parts)
    for p in parts:   # every part is a well-formed fragment: tags close in order
        st = []
        for close, name in re.findall(r"<(/?)(\w+)", p):
            if close: assert st and st.pop() == name
            else: st.append(name)
        assert not st
    assert re.sub(r"<[^>]+>", "", "".join(parts)) == re.sub(r"<[^>]+>", "", h)   # no text lost
    assert t.split_html("ngắn") == ["ngắn"] and t.split_html("") == []

def test_parse_incoming_group_mention_and_files():
    import channel_telegram as t
    from aiogram.types import Update, User
    me = User(**ME)
    def upd(**m): return Update.model_validate({"update_id": 1, "message": {"message_id": 1, "date": 1, "from": user_of(9), **m}})
    grp = {"id": -100, "type": "supergroup", "title": "G"}
    p = t.ADAPTER.parse_incoming
    assert p({"update": upd(chat=grp, text="mọi người ơi"), "me": me}).mentioned is False
    g = p({"update": upd(chat=grp, text="@Shop_Test_Bot giá bao nhiêu?"), "me": me})
    assert g.mentioned and g.is_group and g.text == "giá bao nhiêu?" and g.chat_id == "-100" and g.user_name == "khach9"
    assert p({"update": upd(chat=grp, text="@shop_test_botx hi"), "me": me}).mentioned is False                 # other bot name
    r = p({"update": upd(chat=grp, text="còn không", reply_to_message={"message_id": 0, "date": 1, "chat": grp, "from": ME, "text": "x"}), "me": me})
    assert r.mentioned
    d = p({"update": upd(chat={"id": 9, "type": "private"}, caption="xem tệp", document={"file_id": "F1", "file_unique_id": "U1", "file_name": "a.pdf", "file_size": 10}), "me": me})
    assert not d.is_group and d.text == "xem tệp" and d.files == [{"kind": "document", "file_id": "F1", "name": "a.pdf", "size": 10}]
    ph = p({"update": upd(chat={"id": 9, "type": "private"}, photo=[{"file_id": "s", "file_unique_id": "s1", "width": 1, "height": 1},
                                                                     {"file_id": "L", "file_unique_id": "L1", "width": 9, "height": 9}]), "me": me})
    assert ph.files[0]["file_id"] == "L" and ph.text == ""
    assert p({"update": upd(chat={"id": 9, "type": "private"}, sticker=None), "me": me}) is None
    assert len(f"ap:{2**62}:1".encode()) <= 64 and t.CB_RE.match("ap:12:0") and not t.CB_RE.match("ap:x:1")

def test_advisory_lock_is_exclusive():
    import channel_telegram as t
    os.environ.setdefault("PG_URI", PG)
    bid = 900000 + random.randint(0, 99999)
    async def go():
        c1 = await psycopg.AsyncConnection.connect(PG, autocommit=True); c2 = await psycopg.AsyncConnection.connect(PG, autocommit=True)
        try:
            assert await t.try_lock(c1, bid) is True
            assert await t.try_lock(c2, bid) is False             # 2nd process cannot take the same binding
            assert await t._poll(bid) == "busy"                   # a 2nd poller exits at once
            await c1.close()
            # Postgres frees a session advisory lock when the holder's BACKEND exits, which happens after the client close() returned: under CPU load that
            # takes a moment (found 24/10: 2 of 6 runs failed with 12 busy loops running). Poll instead of assuming it is instant.
            for _ in range(100):
                if await t.try_lock(c2, bid): break              # lock freed when the holder's connection ends
                await asyncio.sleep(0.1)
            else: pytest.fail("advisory lock was not released within 10 s after the holder's connection closed")
        finally:
            await c2.close()
    asyncio.run(go())

def test_mock_base_must_be_loopback(monkeypatch):
    import channel_telegram as t
    monkeypatch.setenv("TELEGRAM_MOCK", "1"); monkeypatch.setenv("TELEGRAM_API_BASE", "https://api.telegram.org")
    with pytest.raises(RuntimeError): t._api_base()
    monkeypatch.delenv("TELEGRAM_API_BASE"); assert t._api_base() == t.MOCK_BASE
    monkeypatch.delenv("TELEGRAM_MOCK"); assert t._api_base() == "https://api.telegram.org"

# ------------------------------------------------------------------ against the local server + fake Bot API
@pytest.fixture(scope="module")
def tg(a):
    fake = FakeBotAPI(); fake.start()
    ws = a.post("/api/admin/workspaces", json={"name": f"tg-w-{SUF}"}).json()["id"]
    name = f"tg_adm_{SUF}"
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": "admin", "workspace_id": ws}); assert r.status_code == 200, r.text
    adm = httpx.Client(base_url=B, auth=(name, PW), timeout=150)
    sec = f"TG_{SUF}"
    assert adm.post(f"/api/secrets/{sec}", json={"value": TOKEN, "allowed_hosts": ["api.telegram.org"]}).status_code == 200
    r = adm.post("/api/agents", json={"name": f"tg-agent-{SUF}", "system_prompt": "Bạn là trợ lý bán hàng.", "tools": ["local:content_publish"],
                                       "interrupt_on": ["local:content_publish"]}); assert r.status_code == 200, r.text
    e = {"fake": fake, "ws": ws, "adm": adm, "sec": sec, "agent": r.json()["id"]}
    yield e
    adm.close()
    with pg() as c:
        c.execute("DELETE FROM channel_bindings WHERE workspace_id=%s", (ws,))
        c.execute("DELETE FROM approvals WHERE agent_id=%s", (e["agent"],))
        c.execute("DELETE FROM lg_runs WHERE workspace_id=%s", (ws,))
        c.execute("DELETE FROM lg_threads WHERE workspace_id=%s", (ws,))
        c.execute("DELETE FROM agents WHERE workspace_id=%s", (ws,))
        c.execute("DELETE FROM ws_secrets WHERE workspace_id=%s", (ws,))
        c.execute("DELETE FROM users WHERE username=%s", (name,))
    fake.stop()

def test_create_binding_starts_poller(tg):
    adm = tg["adm"]
    assert adm.post(f"/api/agents/{tg['agent']}/channels", json={"kind": "telegram"}).status_code == 422          # token secret required
    r = adm.post(f"/api/agents/{tg['agent']}/channels", json={"kind": "telegram", "name": "Bot thử", "secret_ref": tg["sec"], "config": {"timeout_s": 120}})
    assert r.status_code == 200, r.text
    tg["bid"] = r.json()["id"]
    wait_for(lambda: tg["fake"].sent("getupdates"), timeout=40)        # the server's supervisor picked it up
    with pg() as c:   # held by the server's poller -> a 2nd poller anywhere cannot start
        assert c.execute("SELECT pg_try_advisory_lock(8202, %s)", (tg["bid"],)).fetchone()[0] is False

def private(fake, uid, text=None, **extra):
    return fake.push(TOKEN, {"message": fake.msg({"id": uid, "type": "private"}, text, **{"from": user_of(uid), **extra})})

def untrusted(t, text):
    """11.72: a stranger's text reaches the agent inside the [UNTRUSTED DATA] wrapper (the fake agent echoes it back): check the wrapper + the text, not the plain echo."""
    return t.startswith("Echo: [UNTRUSTED DATA") and t.rstrip().endswith("[END UNTRUSTED DATA]") and ("\n" + text + "\n") in t

def test_text_reply_and_typing(tg):
    f = tg["fake"]
    private(f, 5001, "xin chào shop")
    out = wait_for(lambda: f.sent("sendmessage", 5001))
    assert untrusted(out[0]["text"], "xin chào shop") and out[0]["parse_mode"] == "HTML"
    assert f.sent("sendchataction", 5001)[0]["action"] == "typing"
    with pg() as c:
        md = c.execute("SELECT metadata FROM lg_runs WHERE workspace_id=%s ORDER BY created_at DESC LIMIT 1", (tg["ws"],)).fetchone()[0]
        assert md["channel"] == "telegram" and md["binding_id"] == tg["bid"] and md["external_user"] == "5001"

def test_long_reply_split_and_escaped(tg):
    f = tg["fake"]
    private(f, 5002, "<b>x</b> & " + "dài " * 1500)
    out = wait_for(lambda: len(f.sent("sendmessage", 5002)) >= 2 and f.sent("sendmessage", 5002), timeout=90)
    time.sleep(1); out = f.sent("sendmessage", 5002)
    assert len(out) >= 2 and all(len(o["text"]) <= 4096 for o in out)
    assert out[0]["text"].startswith("Echo: [UNTRUSTED DATA") and "\n&lt;b&gt;x&lt;/b&gt; &amp;" in out[0]["text"] and sum(len(o["text"]) for o in out) > 6000

def test_document_and_photo_reach_agent(tg):
    f = tg["fake"]
    f.files["doc-1"] = ("documents/bao_gia.txt", "Bảng giá: sản phẩm A giá 120.000 đồng.\n".encode())
    private(f, 5003, None, caption="tóm tắt tệp", document={"file_id": "doc-1", "file_unique_id": "udoc1", "file_name": "bao_gia.txt", "mime_type": "text/plain", "file_size": 60})
    out = wait_for(lambda: f.sent("sendmessage", 5003))
    assert "tóm tắt tệp" in out[0]["text"] and "120.000" in out[0]["text"], out[0]["text"]
    with pg() as c:
        inp, tid = c.execute("""SELECT r.input, r.thread_id FROM lg_runs r JOIN channel_threads t ON t.thread_id=r.thread_id
                                WHERE t.binding_id=%s AND t.chat_key='u:5003'""", (tg["bid"],)).fetchone()
        blocks = inp["messages"][0]["content"]
        assert [b["type"] for b in blocks] == ["text", "file"] and blocks[1]["extras"]["kind"] == "txt"
        assert c.execute("SELECT count(*) FROM chat_thread_files WHERE thread_id=%s", (tid,)).fetchone()[0] == 1
    f.files["ph-1"] = ("photos/p.jpg", PNG)
    private(f, 5004, None, photo=[{"file_id": "ph-1", "file_unique_id": "uph1", "width": 1, "height": 1, "file_size": len(PNG)}])
    out = wait_for(lambda: f.sent("sendmessage", 5004))
    assert f"I received 1 image(s): image/png {len(PNG)} B" in out[0]["text"]
    f.files["bad-1"] = ("documents/x.exe", b"MZ\x90\x00binary")
    private(f, 5005, None, document={"file_id": "bad-1", "file_unique_id": "ubad", "file_name": "x.exe", "file_size": 9})
    out = wait_for(lambda: f.sent("sendmessage", 5005))
    assert out[0]["text"].startswith("Không nhận được tệp")

def test_group_only_when_mentioned_or_replied(tg):
    f = tg["fake"]; grp = {"id": -100777, "type": "supergroup", "title": "Nhóm"}
    f.push(TOKEN, {"message": f.msg(grp, "mọi người ơi", **{"from": user_of(6001)})})
    f.push(TOKEN, {"message": f.msg(grp, "@shop_test_bot giá bao nhiêu", **{"from": user_of(6001)})})
    out = wait_for(lambda: f.sent("sendmessage", -100777))
    assert untrusted(out[0]["text"], "giá bao nhiêu")
    bot_msg = out[0]["_result"]
    f.push(TOKEN, {"message": f.msg(grp, "còn hàng không", **{"from": user_of(6002), "reply_to_message": {**bot_msg, "from": ME}})})
    out = wait_for(lambda: len(f.sent("sendmessage", -100777)) >= 2 and f.sent("sendmessage", -100777))
    time.sleep(1.5)
    assert len(f.sent("sendmessage", -100777)) == 2 and untrusted(f.sent("sendmessage", -100777)[0]["text"], "giá bao nhiêu") and untrusted(f.sent("sendmessage", -100777)[1]["text"], "còn hàng không")   # the unmentioned one was ignored

def _approval(f, chat, text):
    private(f, chat, text)
    out = wait_for(lambda: f.sent("sendmessage", chat))
    kb = out[0]["_result"]["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in kb] == ["Duyệt", "Từ chối"] and "content_publish(" in out[0]["text"]
    aid = int(kb[0]["callback_data"].split(":")[1])
    assert [b["callback_data"] for b in kb] == [f"ap:{aid}:1", f"ap:{aid}:0"]
    return aid, out[0]["_result"]

def _click(f, chat, msg, data, cq):
    f.push(TOKEN, {"callback_query": {"id": cq, "from": user_of(chat), "chat_instance": "ci", "data": data, "message": msg}})

def test_approval_buttons_approve_resumes(tg):
    f = tg["fake"]
    aid, msg = _approval(f, 7001, "publish bài khuyến mãi")
    _click(f, 7001, msg, f"ap:{aid}:1", "cq-1")
    wait_for(lambda: [c for c in f.sent("answercallbackquery") if c["callback_query_id"] == "cq-1"])
    out = wait_for(lambda: len(f.sent("sendmessage", 7001)) >= 2 and f.sent("sendmessage", 7001))
    assert "content_publish" in out[1]["text"] and "reply_markup" not in out[1]
    ed = wait_for(lambda: f.sent("editmessagetext", 7001))
    assert ed[0]["text"].endswith("<b>Đã duyệt</b>") and str(ed[0]["message_id"]) == str(msg["message_id"]) and "reply_markup" not in ed[0]
    with pg() as c: assert c.execute("SELECT status FROM approvals WHERE id=%s", (aid,)).fetchone()[0] == "approved"

def test_approval_buttons_reject_stops(tg):
    f = tg["fake"]
    aid, msg = _approval(f, 7002, "publish thông báo nghỉ lễ")
    _click(f, 7002, msg, f"ap:{aid}:0", "cq-2")
    ed = wait_for(lambda: f.sent("editmessagetext", 7002))
    assert ed[0]["text"].endswith("<b>Đã từ chối</b>")
    with pg() as c:
        assert c.execute("SELECT status FROM approvals WHERE id=%s", (aid,)).fetchone()[0] == "rejected"
        th = c.execute("SELECT thread_id FROM approvals WHERE id=%s", (aid,)).fetchone()[0]
        assert not c.execute("SELECT 1 FROM approvals WHERE thread_id=%s AND status='pending'", (th,)).fetchone()

def test_poller_stops_when_disabled(tg):
    f = tg["fake"]
    assert tg["adm"].patch(f"/api/channels/{tg['bid']}", json={"status": "disabled"}).json()["status"] == "disabled"
    def free():
        with pg() as c:
            if c.execute("SELECT pg_try_advisory_lock(8202, %s)", (tg["bid"],)).fetchone()[0]:
                c.execute("SELECT pg_advisory_unlock(8202, %s)", (tg["bid"],)); return True
    wait_for(free, timeout=40, every=0.5)                      # the server's poller released its lock
    time.sleep(1.5); n = len(f.sent("getupdates"))
    private(f, 5009, "còn ai không")
    time.sleep(4)
    assert len(f.sent("getupdates")) == n and not f.sent("sendmessage", 5009)
