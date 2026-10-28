"""GĐ8.4 (23/10/2026): Slack adapter (app/channel_slack.py, slack-bolt 1.30 Socket Mode). Pure unit tests (Markdown -> mrkdwn, 3,500 split,
event parsing, Block Kit buttons, mock URLs, advisory lock) + end-to-end against the local server :8020, whose Socket Mode connection talks to
the fake Slack below (aiohttp on 127.0.0.1:8783: Web API /api/<method>, Socket Mode WebSocket /link/, file downloads /files/<id>;
SLACK_API_BASE set by overlay/run_local.py with SLACK_MOCK=1). Fake LLM => $0, no real network. Server tests run in file order (`sl` fixture)."""
import asyncio, json, os, random, sys, threading, time, uuid
import httpx, psycopg, pytest
from test_fixes import B, ADMIN, a  # noqa: F401  (fixtures)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
PORT = 8783; PW = "phase0-gd84-local"; SUF = uuid.uuid4().hex[:6]
BOT_TOKEN, APP_TOKEN = f"xoxb-1-{SUF}-faketoken", f"xapp-1-A1-{SUF}-faketoken"
BOT_USER = "UBOT42"

def pg(): return psycopg.connect(PG, autocommit=True)

# ------------------------------------------------------------------ fake Slack (Web API + Socket Mode + files): tests/fakes/fake_slack.py (GĐ12.74 moved it out)
from fakes.fake_slack import FakeSlack as _FakeSlack  # noqa: E402

def FakeSlack(): return _FakeSlack(PORT, BOT_TOKEN, APP_TOKEN, BOT_USER, SOCK_WAIT)

def wait_for(fn, timeout=90, every=0.2, what=""):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = fn()
        if r: return r
        time.sleep(every)
    w = what() if callable(what) else what
    raise AssertionError(f"timed out waiting ({timeout}s){': ' + w if w else ''}")

# Flaky under full-suite load (8.Z, root causes found 24/10): the Socket Mode connection is made by the SERVER's supervisor (a tick every SLACK_SUPERVISOR_S, then a
# PG connection + advisory lock + HTTPS-like handshake to the fake) and is re-dialled after any server restart or lock hand-over, so "the socket is open right now" is not
# an invariant. (1) first connect gets 120 s, (2) FakeSlack.push() waits up to SOCK_WAIT for the re-dial instead of failing at once, (3) the fixture waits for a stable server.
FIRST_CONNECT_S = 120
SOCK_WAIT = 90

def ts(): return f"{time.time():.6f}"

# ------------------------------------------------------------------ pure unit tests
def test_markdown_to_mrkdwn():
    import channel_slack as s
    m = s.md_to_mrkdwn("# Báo giá\n**đậm** *nghiêng* <script>x</script> & `a<b>` [web](https://x.vn/?a=1&b=2) [xấu](javascript:alert(1))\n- mục 1\n```py\nif a<b: pass\n```")
    assert "*Báo giá*" in m and "*đậm*" in m and "_nghiêng_" in m and "• mục 1" in m
    assert "&lt;script&gt;" in m and "<script" not in m and "`a&lt;b&gt;`" in m and " &amp; " in m
    assert "<https://x.vn/?a=1&amp;b=2|web>" in m and "<javascript" not in m
    assert "```\nif a&lt;b: pass\n```" in m

def test_long_reply_split_3500():
    import channel_slack as s
    t = s.md_to_mrkdwn("**" + "chữ đậm " * 900 + "**\n\n```\n" + "code dòng\n" * 600 + "```")
    parts = s.split_mrkdwn(t)
    assert len(parts) >= 3 and all(len(p) <= 3500 for p in parts)
    assert all(p.count("```") % 2 == 0 for p in parts)               # code fences closed in every part
    assert "".join(parts).replace("```", "").replace("\n", "") == t.replace("```", "").replace("\n", "")   # no text lost
    assert s.split_mrkdwn("ngắn") == ["ngắn"] and s.split_mrkdwn("") == []

def test_parse_events():
    import channel_slack as s
    p = lambda ev: s.ADAPTER.parse_incoming({"event": ev, "bot_user_id": BOT_USER})
    m = p({"type": "app_mention", "user": "U1", "channel": "C1", "ts": "100.1", "text": f"<@{BOT_USER}> giá &lt;A&gt; bao nhiêu?"})
    assert m.text == "giá <A> bao nhiêu?" and m.chat_id == "C1:100.1" and m.is_group and m.mentioned and m.per_chat
    assert p({"type": "app_mention", "user": "U1", "channel": "C1", "ts": "100.2", "thread_ts": "100.1", "text": f"<@{BOT_USER}> tiếp"}).chat_id == "C1:100.1"
    d = p({"type": "message", "channel_type": "im", "user": "U2", "channel": "D2", "ts": "5.5", "text": "chào"})
    assert d.chat_id == "D2:5.5" and not d.is_group and d.text == "chào"
    import channels as ch
    assert ch.chat_key(d) == "g:D2:5.5"                              # one agent thread per Slack thread, DMs too
    assert p({"type": "message", "channel_type": "channel", "user": "U2", "channel": "C2", "ts": "1", "text": "hi"}) is None      # channel msg w/o mention
    assert p({"type": "message", "channel_type": "im", "channel": "D2", "ts": "1", "text": "x", "bot_id": "B9", "subtype": "bot_message"}) is None
    assert p({"type": "message", "channel_type": "im", "user": BOT_USER, "channel": "D2", "ts": "1", "text": "x"}) is None
    assert p({"type": "message", "channel_type": "im", "channel": "D2", "ts": "1", "subtype": "message_changed", "message": {"text": "x"}}) is None
    f = p({"type": "message", "channel_type": "im", "subtype": "file_share", "user": "U2", "channel": "D2", "ts": "2", "text": "",
           "files": [{"id": "F1", "name": "a.pdf", "size": 10}]})
    assert f.files == [{"id": "F1", "name": "a.pdf", "size": 10}] and f.text == ""

def test_approval_blocks_and_secret():
    import channel_slack as s
    bl = s.approval_blocks("Trợ lý cần duyệt <x>", 42)
    btn = bl[1]["elements"]
    assert [b["action_id"] for b in btn] == ["aos_approve", "aos_reject"] and {b["value"] for b in btn} == {"42"}
    assert bl[0]["text"]["text"] == "Trợ lý cần duyệt &lt;x&gt;"
    assert s.parse_secret(json.dumps({"bot_token": BOT_TOKEN, "app_token": APP_TOKEN}))["app_token"] == APP_TOKEN
    for bad in ("xoxb-only", json.dumps({"bot_token": APP_TOKEN, "app_token": BOT_TOKEN})):
        with pytest.raises(ValueError) as e: s.parse_secret(bad)
        assert "xapp-1" not in str(e.value)

def test_mock_urls_must_be_loopback(monkeypatch):
    import channel_slack as s
    monkeypatch.setenv("SLACK_MOCK", "1"); monkeypatch.setenv("SLACK_API_BASE", "https://slack.com/api/")
    with pytest.raises(RuntimeError): s.api_base()
    monkeypatch.delenv("SLACK_API_BASE"); assert s.api_base() == s.MOCK_BASE
    assert s.ws_url_ok("ws://127.0.0.1:1/link/") and not s.ws_url_ok("wss://wss-primary.slack.com/link/")
    monkeypatch.delenv("SLACK_MOCK"); monkeypatch.setenv("SLACK_API_BASE", "http://127.0.0.1:1/api/")
    assert s.api_base() == "https://slack.com/api/"                  # real mode ignores the override
    assert s.ws_url_ok("wss://wss-primary.slack.com/link/?ticket=x") and not s.ws_url_ok("ws://wss-primary.slack.com/") and not s.ws_url_ok("wss://evil.com/")
    assert s.file_url_ok("https://files.slack.com/files-pri/T1-F1/a.pdf") and not s.file_url_ok("https://evil.com/a")

def test_advisory_lock_is_exclusive():
    import channel_slack as s
    os.environ.setdefault("PG_URI", PG)
    bid = 900000 + random.randint(0, 99999)
    async def go():
        c1 = await psycopg.AsyncConnection.connect(PG, autocommit=True); c2 = await psycopg.AsyncConnection.connect(PG, autocommit=True)
        try:
            assert await s.try_lock(c1, bid) is True
            assert await s.try_lock(c2, bid) is False            # 2nd process cannot take the same app
            assert await s._connect(bid) == "busy"               # a 2nd connection loop exits at once
            await c1.close()
            for _ in range(100):   # Postgres frees the session lock when the holder's backend exits, a moment AFTER close() returned (slow under CPU load)
                if await s.try_lock(c2, bid): break
                await asyncio.sleep(0.1)
            else: pytest.fail("advisory lock was not released within 10 s after the holder's connection closed")
        finally:
            await c2.close()
    asyncio.run(go())

# ------------------------------------------------------------------ against the local server + fake Slack
@pytest.fixture(scope="module")
def sl(a):
    stable = 0   # another process may be restarting the server right now: wait for 5 healthy answers in a row before creating the binding
    for _ in range(180):
        try: stable = stable + 1 if httpx.get(B + "/health", timeout=3).status_code < 500 else 0
        except httpx.HTTPError: stable = 0
        if stable >= 5: break
        time.sleep(1)
    fake = FakeSlack(); fake.start()
    ws = a.post("/api/admin/workspaces", json={"name": f"sl-w-{SUF}"}).json()["id"]
    name = f"sl_adm_{SUF}"
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": "admin", "workspace_id": ws}); assert r.status_code == 200, r.text
    adm = httpx.Client(base_url=B, auth=(name, PW), timeout=150)
    sec = f"SLACK_{SUF}"
    assert adm.post(f"/api/secrets/{sec}", json={"value": json.dumps({"bot_token": BOT_TOKEN, "app_token": APP_TOKEN}),
                                                  "allowed_hosts": ["slack.com", "files.slack.com"]}).status_code == 200
    r = adm.post("/api/agents", json={"name": f"sl-agent-{SUF}", "system_prompt": "Bạn là trợ lý bán hàng.", "tools": ["local:content_publish"],
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

def test_create_binding_connects_socket_mode(sl):
    adm, f = sl["adm"], sl["fake"]
    assert adm.post(f"/api/agents/{sl['agent']}/channels", json={"kind": "slack"}).status_code == 422          # token secret required
    r = adm.post(f"/api/agents/{sl['agent']}/channels", json={"kind": "slack", "name": "Slack thử", "secret_ref": sl["sec"], "config": {"timeout_s": 120}})
    assert r.status_code == 200, r.text
    sl["bid"] = r.json()["id"]
    wait_for(lambda: f.open_socks(), timeout=FIRST_CONNECT_S, what=lambda: f"the supervisor never opened Socket Mode (apps.connections.open calls: {len(f.sent('apps.connections.open'))})")   # the server's supervisor opened the Socket Mode connection
    opens = f.sent("apps.connections.open")
    assert opens and all(c["token"] == APP_TOKEN for c in opens)       # app token only for apps.connections.open
    time.sleep(3)
    assert len(f.open_socks()) == 1                                    # exactly one connection for this app
    with pg() as c:   # held by the server's connection loop -> a 2nd one anywhere cannot start
        assert c.execute("SELECT pg_try_advisory_lock(8204, %s)", (sl["bid"],)).fetchone()[0] is False

def untrusted(t, text):
    """11.72: a stranger's text reaches the agent inside the [UNTRUSTED DATA] wrapper (the fake agent echoes it back): check the wrapper + the text, not the plain echo."""
    return t.startswith("Echo: [UNTRUSTED DATA") and t.rstrip().endswith("[END UNTRUSTED DATA]") and ("\n" + text + "\n") in t

def test_mention_gets_thread_reply(sl):
    f = sl["fake"]; t1 = ts()
    eid = f.event({"type": "app_mention", "user": "U5001", "channel": "C77", "ts": t1, "text": f"<@{BOT_USER}> giá bao nhiêu"})
    out = wait_for(lambda: f.sent("chat.postMessage", "C77"))
    assert untrusted(out[0]["text"], "giá bao nhiêu") and out[0]["thread_ts"] == t1 and out[0]["token"] == BOT_TOKEN
    assert any(x.get("envelope_id") == eid for x in f.acks)            # envelope acked over the socket
    f.event({"type": "app_mention", "user": "U5002", "channel": "C77", "ts": ts(), "thread_ts": t1, "text": f"<@{BOT_USER}> còn hàng không"})
    out = wait_for(lambda: len(f.sent("chat.postMessage", "C77")) >= 2 and f.sent("chat.postMessage", "C77"))
    assert untrusted(out[1]["text"], "còn hàng không") and out[1]["thread_ts"] == t1
    with pg() as c:
        th = c.execute("SELECT count(*) FROM channel_threads WHERE binding_id=%s AND chat_key=%s", (sl["bid"], f"g:C77:{t1}")).fetchone()[0]
        runs = c.execute("""SELECT count(DISTINCT r.thread_id), count(*) FROM lg_runs r JOIN channel_threads t ON t.thread_id=r.thread_id
                            WHERE t.binding_id=%s AND t.chat_key=%s""", (sl["bid"], f"g:C77:{t1}")).fetchone()
        md = c.execute("SELECT metadata FROM lg_runs WHERE workspace_id=%s ORDER BY created_at DESC LIMIT 1", (sl["ws"],)).fetchone()[0]
    assert th == 1 and runs == (1, 2)                                  # one agent thread for the Slack thread
    assert md["channel"] == "slack" and md["binding_id"] == sl["bid"] and md["external_user"] == "U5002"
    sl["conv"] = {"channel": "C77", "t1": t1}

def test_dm_gets_reply(sl):
    f = sl["fake"]; t = ts()
    f.event({"type": "message", "channel_type": "im", "user": "U5003", "channel": "D5003", "ts": t, "text": "xin chào shop"})
    out = wait_for(lambda: f.sent("chat.postMessage", "D5003"))
    assert untrusted(out[0]["text"], "xin chào shop") and out[0]["thread_ts"] == t

def test_bot_messages_and_edits_ignored(sl):
    f = sl["fake"]
    f.event({"type": "message", "channel_type": "im", "subtype": "bot_message", "bot_id": "B999", "channel": "D5004", "ts": ts(), "text": "tôi là bot"})
    f.event({"type": "message", "channel_type": "im", "user": BOT_USER, "bot_id": "BBOT42", "channel": "D5004", "ts": ts(), "text": "chính tôi"})
    f.event({"type": "message", "channel_type": "im", "subtype": "message_changed", "channel": "D5004", "ts": ts(), "message": {"user": "U5004", "text": "sửa"}})
    f.event({"type": "message", "channel_type": "channel", "user": "U5004", "channel": "C5004", "ts": ts(), "text": "không gọi bot"})
    time.sleep(4)
    assert not f.sent("chat.postMessage", "D5004") and not f.sent("chat.postMessage", "C5004")

def test_long_reply_split(sl):
    f = sl["fake"]
    f.event({"type": "message", "channel_type": "im", "user": "U5005", "channel": "D5005", "ts": ts(), "text": "<b>x</b> &amp; " + "dài " * 1500})
    wait_for(lambda: len(f.sent("chat.postMessage", "D5005")) >= 2, timeout=90)
    time.sleep(1); out = f.sent("chat.postMessage", "D5005")
    assert len(out) >= 2 and all(len(o["text"]) <= 3500 for o in out) and len({o["thread_ts"] for o in out}) == 1
    assert out[0]["text"].startswith("Echo: [UNTRUSTED DATA") and "\n&lt;b&gt;x&lt;/b&gt; &amp;" in "\n".join(o["text"] for o in out) and sum(len(o["text"]) for o in out) > 6000

def test_file_share_reaches_agent(sl):
    f = sl["fake"]
    f.files["F1"] = ("bao_gia.txt", "Bảng giá: sản phẩm A giá 120.000 đồng.\n".encode())
    f.event({"type": "message", "channel_type": "im", "subtype": "file_share", "user": "U5006", "channel": "D5006", "ts": ts(), "text": "tóm tắt tệp",
             "files": [{"id": "F1", "name": "bao_gia.txt", "size": 60}]})
    out = wait_for(lambda: f.sent("chat.postMessage", "D5006"))
    assert "tóm tắt tệp" in out[0]["text"] and "120.000" in out[0]["text"], out[0]["text"]
    assert f.sent("files.info") and f"Bearer {BOT_TOKEN}" in f.downloads
    with pg() as c:
        inp = c.execute("""SELECT r.input FROM lg_runs r JOIN channel_threads t ON t.thread_id=r.thread_id
                           WHERE t.binding_id=%s AND t.chat_key LIKE 'g:D5006:%%'""", (sl["bid"],)).fetchone()[0]
    assert [b["type"] for b in inp["messages"][0]["content"]] == ["text", "file"]
    f.files["F2"] = ("big.pdf", b"%PDF")
    f.event({"type": "message", "channel_type": "im", "subtype": "file_share", "user": "U5007", "channel": "D5007", "ts": ts(), "text": "",
             "files": [{"id": "F2", "name": "big.pdf", "size": 50 << 20}]})
    out = wait_for(lambda: f.sent("chat.postMessage", "D5007"))
    assert out[0]["text"].startswith("Không nhận được tệp") and "20 MB" in out[0]["text"]

def _approval(f, chan, text):
    t = ts()
    f.event({"type": "message", "channel_type": "im", "user": "U" + chan[1:], "channel": chan, "ts": t, "text": text})
    out = wait_for(lambda: f.sent("chat.postMessage", chan))
    bl = out[0]["blocks"]; btn = bl[1]["elements"]
    assert [b["action_id"] for b in btn] == ["aos_approve", "aos_reject"] and "content_publish(" in bl[0]["text"]["text"] and out[0]["thread_ts"] == t
    return int(btn[0]["value"]), out[0]["_result"]

def _click(f, chan, msg, action, aid):
    f.push("interactive", {"type": "block_actions", "user": {"id": "U" + chan[1:], "username": "khach"}, "team": {"id": "T1"}, "api_app_id": "A1",
                           "trigger_id": "tr", "container": {"type": "message", "message_ts": msg["ts"], "channel_id": chan, "is_ephemeral": False},
                           "channel": {"id": chan, "name": "directmessage"}, "message": msg,
                           "actions": [{"type": "button", "action_id": action, "block_id": "aos_ap", "value": str(aid), "action_ts": ts()}]})

def test_approval_buttons_approve_resumes(sl):
    f = sl["fake"]
    aid, msg = _approval(f, "D7001", "publish bài khuyến mãi")
    _click(f, "D7001", msg, "aos_approve", aid)
    up = wait_for(lambda: f.sent("chat.update", "D7001"))
    assert up[0]["ts"] == msg["ts"] and "Đã duyệt" in json.dumps(up[0]["blocks"], ensure_ascii=False) and "actions" not in [b["type"] for b in up[0]["blocks"]]
    out = wait_for(lambda: len(f.sent("chat.postMessage", "D7001")) >= 2 and f.sent("chat.postMessage", "D7001"))
    assert "content_publish" in out[1]["text"] and "blocks" not in out[1] and out[1]["thread_ts"] == msg["thread_ts"]
    with pg() as c: assert c.execute("SELECT status FROM approvals WHERE id=%s", (aid,)).fetchone()[0] == "approved"
    sl["approve"] = {"msg": msg, "update": up[0], "reply": out[1]}

def test_approval_buttons_reject_stops(sl):
    f = sl["fake"]
    aid, msg = _approval(f, "D7002", "publish thông báo nghỉ lễ")
    _click(f, "D7002", msg, "aos_reject", aid)
    up = wait_for(lambda: f.sent("chat.update", "D7002"))
    assert "Đã từ chối" in json.dumps(up[0]["blocks"], ensure_ascii=False)
    with pg() as c:
        assert c.execute("SELECT status FROM approvals WHERE id=%s", (aid,)).fetchone()[0] == "rejected"
        th = c.execute("SELECT thread_id FROM approvals WHERE id=%s", (aid,)).fetchone()[0]
        assert not c.execute("SELECT 1 FROM approvals WHERE thread_id=%s AND status='pending'", (th,)).fetchone()

def test_connection_closed_when_disabled(sl):
    f = sl["fake"]
    assert sl["adm"].patch(f"/api/channels/{sl['bid']}", json={"status": "disabled"}).json()["status"] == "disabled"
    def free():
        with pg() as c:
            if c.execute("SELECT pg_try_advisory_lock(8204, %s)", (sl["bid"],)).fetchone()[0]:
                c.execute("SELECT pg_advisory_unlock(8204, %s)", (sl["bid"],)); return True
    wait_for(free, timeout=40, every=0.5)                              # the server's loop released its lock
    wait_for(lambda: not f.open_socks(), timeout=20)                   # and closed the Socket Mode connection
    mine = lambda: [m for m in f.sent("apps.connections.open") if m["token"] == APP_TOKEN]   # only THIS run's binding (a stale binding of an older killed run may retry against the same fake port)
    n = len(mine()); time.sleep(4)
    assert len(mine()) == n
