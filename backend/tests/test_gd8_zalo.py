"""GĐ8.3 (23/10/2026): Zalo OA adapter (app/channel_zalo.py). Pure unit tests (signature, 2,000-char split, 48 h / 7 d window, decision text,
event parsing, mock base) + end-to-end against the local server :8020, whose adapter talks to the fake OA + OAuth server below (aiohttp on
127.0.0.1:8782 = ZALO_API_BASE / ZALO_OAUTH_BASE set by overlay/run_local.py with ZALO_MOCK=1). Fake LLM => $0, no real network.
Server tests run in file order and share state through the `zl` fixture."""
import asyncio, base64, hashlib, json, os, sys, threading, time, uuid
import httpx, psycopg, pytest
from aiohttp import web
from test_fixes import B, ADMIN, a  # noqa: F401  (fixtures)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
PG = os.environ.setdefault("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
LOG = os.path.join(os.path.dirname(__file__), "..", "var", "server.log")
PORT = 8782; PW = "phase0-gd83-local"; SUF = uuid.uuid4().hex[:6]
APP_ID, OA_SECRET, APP_SECRET = f"app{SUF}", f"oa-secret-{SUF}", f"app-secret-{SUF}"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

def pg(): return psycopg.connect(PG, autocommit=True)

# ------------------------------------------------------------------ fake Zalo OA API + OAuth v4
class FakeZalo:
    """Records every call. Refresh tokens are single-use (a reused one is rejected and recorded); access tokens can be revoked."""
    def __init__(self):
        self.sent, self.refresh_used, self.reused, self.files = [], [], [], {}
        self.refresh_valid, self.access_valid, self.n = {"RT0"}, set(), 0

    async def oauth(self, req):
        f = dict(await req.post())
        if req.headers.get("secret_key") != APP_SECRET or f.get("grant_type") != "refresh_token" or f.get("app_id") != APP_ID:
            return web.json_response({"error": -14002, "error_name": "Invalid app"})
        rt = f.get("refresh_token")
        if rt not in self.refresh_valid:
            self.reused.append(rt); return web.json_response({"error": -14014, "error_name": "Invalid refresh token"})
        self.refresh_valid.discard(rt); self.refresh_used.append(rt); self.n += 1
        await asyncio.sleep(0.3)   # widen the race window for concurrent refreshers
        at, nrt = f"AT{self.n}", f"RT{self.n}"
        self.access_valid.add(at); self.refresh_valid.add(nrt)
        return web.json_response({"access_token": at, "refresh_token": nrt, "expires_in": "90000"})

    async def cs(self, req):
        body = await req.json()
        if req.headers.get("access_token") not in self.access_valid: return web.json_response({"error": -216, "message": "Access token is invalid"})
        self.sent.append({"user_id": body["recipient"]["user_id"], "text": body["message"]["text"], "token": req.headers["access_token"]})
        return web.json_response({"error": 0, "message": "Success", "data": {"message_id": uuid.uuid4().hex, "user_id": body["recipient"]["user_id"]}})

    async def file(self, req):
        d = self.files.get(req.match_info["name"])
        return web.Response(body=d) if d is not None else web.Response(status=404)

    def start(self):
        self.loop = asyncio.new_event_loop(); ready = threading.Event()
        def run():
            asyncio.set_event_loop(self.loop)
            app = web.Application()
            app.router.add_post("/v4/oa/access_token", self.oauth); app.router.add_post("/v3.0/oa/message/cs", self.cs)
            app.router.add_get("/files/{name}", self.file)
            self.runner = web.AppRunner(app); self.loop.run_until_complete(self.runner.setup())
            self.loop.run_until_complete(web.TCPSite(self.runner, "127.0.0.1", PORT).start()); ready.set(); self.loop.run_forever()
        threading.Thread(target=run, daemon=True).start(); assert ready.wait(10)

    def stop(self):
        asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop).result(10); self.loop.call_soon_threadsafe(self.loop.stop)

    def to(self, uid): return [m for m in list(self.sent) if m["user_id"] == str(uid)]

def wait_for(fn, timeout=90, every=0.2):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = fn()
        if r: return r
        time.sleep(every)
    raise AssertionError("timed out waiting")

def event(uid, text=None, kind="text", attachments=None, ts_ms=None, msg_id=None):
    m = {"msg_id": msg_id or uuid.uuid4().hex}
    if text is not None: m["text"] = text
    if attachments: m["attachments"] = attachments
    return {"app_id": APP_ID, "sender": {"id": str(uid)}, "user_id_by_app": f"app-{uid}", "recipient": {"id": "oa-1"},
            "event_name": f"user_send_{kind}", "message": m, "timestamp": str(ts_ms or int(time.time() * 1000))}

def sig(body: bytes, ts, secret=OA_SECRET):
    return hashlib.sha256(APP_ID.encode() + body + str(ts).encode() + secret.encode()).hexdigest()

def post(bid, ev, header=None, **h):
    body = json.dumps(ev, ensure_ascii=False).encode()
    hdr = {"Content-Type": "application/json", "X-ZEvent-Signature": header if header is not None else "mac=" + sig(body, ev["timestamp"]), **h}
    return httpx.post(f"{B}/channels/zalo/{bid}", content=body, headers=hdr, timeout=10)

def log_has(text):
    with open(LOG, encoding="utf-8", errors="replace") as f: return text in f.read()

# ------------------------------------------------------------------ pure unit tests
def untrusted(t, text):
    """11.72: a stranger's text reaches the agent inside the [UNTRUSTED DATA] wrapper (the fake agent echoes it back): check the wrapper + the text, not the plain echo."""
    return t.startswith("Echo: [UNTRUSTED DATA") and t.rstrip().endswith("[END UNTRUSTED DATA]") and ("\n" + text + "\n") in t

def test_signature_formats_and_tamper():
    import channel_zalo as z
    body, ts = b'{"a":1}', "1700000000000"
    h = z.sign(APP_ID, body, ts, OA_SECRET)
    assert h == sig(body, ts)
    assert z.verify(APP_ID, body, ts, OA_SECRET, h) and z.verify(APP_ID, body, ts, OA_SECRET, "mac=" + h) and z.verify(APP_ID, body, ts, OA_SECRET, "MAC=" + h.upper())
    assert not z.verify(APP_ID, body + b" ", ts, OA_SECRET, h) and not z.verify(APP_ID, body, "1", OA_SECRET, h)
    assert not z.verify(APP_ID, body, ts, "other", h) and not z.verify(APP_ID, body, ts, OA_SECRET, None) and not z.verify(APP_ID, body, ts, OA_SECRET, "mac=")

def test_split_2000_and_plain():
    import channel_zalo as z
    t = ("Đoạn văn tiếng Việt có dấu. " * 60 + "\n\n") * 4
    parts = z.split_text(t)
    assert len(parts) >= 3 and all(len(p) <= 2000 for p in parts)
    assert " ".join(" ".join(parts).split()) == " ".join(t.split())
    assert all(len(p) <= 2000 for p in z.split_text("x" * 4500)) and "".join(z.split_text("x" * 4500)) == "x" * 4500
    assert z.split_text("ngắn") == ["ngắn"] and z.split_text("") == []
    assert z.md_to_plain("# Giá\n**đậm** và [web](https://x.vn)\n- mục") == "Giá\nđậm và web (https://x.vn)\n• mục"

def test_window_rules():
    import channel_zalo as z
    now = time.time(); h = 3600
    assert z.window({}, now - h, now) is None
    assert "48 h" in z.window({}, now - 49 * h, now) and z.window({"allow_after_48h": True}, now - 49 * h, now) is None
    assert "7 days" in z.window({"allow_after_48h": True}, now - 8 * 24 * h, now)
    assert z.window({}, None, now)

def test_parse_decision_and_incoming():
    import channel_zalo as z
    assert z.parse_decision("duyệt 12") == (12, True) and z.parse_decision("Từ chối #7.") == (7, False)
    assert z.parse_decision("  'duyet 3' ") == (3, True) and z.parse_decision("tu choi 9") == (9, False)
    assert z.parse_decision("duyệt giúp tôi 12") is None and z.parse_decision("12") is None
    m = z.ADAPTER.parse_incoming(event(77, "chào"))
    assert m.chat_id == m.user_id == "77" and m.text == "chào" and not m.files and not m.is_group
    im = z.ADAPTER.parse_incoming(event(77, None, "image", [{"type": "image", "payload": {"url": "https://x/a.jpg", "thumbnail": "t"}}]))
    assert im.files == [{"kind": "image", "url": "https://x/a.jpg", "name": None, "size": None}]
    assert z.ADAPTER.parse_incoming({**event(77, "x"), "event_name": "user_send_sticker"}) is None
    assert z.ADAPTER.parse_incoming({**event(77, "x"), "event_name": "oa_send_text"}) is None

def test_mock_base_must_be_loopback(monkeypatch):
    import channel_zalo as z
    monkeypatch.setenv("ZALO_MOCK", "1"); monkeypatch.setenv("ZALO_API_BASE", "https://openapi.zalo.me")
    with pytest.raises(RuntimeError): z.api_base()
    monkeypatch.delenv("ZALO_API_BASE"); monkeypatch.delenv("ZALO_OAUTH_BASE", raising=False)
    assert z.api_base() == z.oauth_base() == z.MOCK_BASE
    monkeypatch.delenv("ZALO_MOCK"); assert z.api_base() == "https://openapi.zalo.me" and z.oauth_base() == "https://oauth.zaloapp.com"

# ------------------------------------------------------------------ against the local server + fake Zalo
@pytest.fixture(scope="module")
def zl(a):
    fake = FakeZalo(); fake.start()
    ws = a.post("/api/admin/workspaces", json={"name": f"zl-w-{SUF}"}).json()["id"]
    name = f"zl_adm_{SUF}"
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": "admin", "workspace_id": ws}); assert r.status_code == 200, r.text
    adm = httpx.Client(base_url=B, auth=(name, PW), timeout=150)
    sec = f"ZALO_{SUF}"
    val = json.dumps({"app_id": APP_ID, "oa_secret_key": OA_SECRET, "app_secret_key": APP_SECRET, "access_token": "", "refresh_token": "RT0", "access_expires_at": 0})
    assert adm.post(f"/api/secrets/{sec}", json={"value": val, "allowed_hosts": ["openapi.zalo.me", "oauth.zaloapp.com"]}).status_code == 200
    r = adm.post("/api/agents", json={"name": f"zl-agent-{SUF}", "system_prompt": "Bạn là trợ lý bán hàng.", "tools": ["local:content_publish"],
                                       "interrupt_on": ["local:content_publish"]}); assert r.status_code == 200, r.text
    e = {"fake": fake, "ws": ws, "adm": adm, "sec": sec, "agent": r.json()["id"]}
    r = adm.post(f"/api/agents/{e['agent']}/channels", json={"kind": "zalo", "name": "OA thử", "secret_ref": sec, "config": {"timeout_s": 120}})
    assert r.status_code == 200, r.text
    e["bid"] = r.json()["id"]
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

def _secret_value(zl):
    import ws_secrets
    with pg() as c:
        enc = c.execute("SELECT value_enc FROM ws_secrets WHERE workspace_id=%s AND name=%s", (zl["ws"], zl["sec"])).fetchone()[0]
    return json.loads(ws_secrets.open_value(enc, zl["ws"], zl["sec"]))

def test_signature_checked_and_only_path_public(zl):
    f = zl["fake"]; ev = event(8000, "không được trả lời")
    assert post(zl["bid"], ev, header="mac=" + "0" * 64).status_code == 401
    assert post(zl["bid"], ev, header="").status_code == 401
    body = json.dumps(ev).encode()
    assert post(zl["bid"], ev, header=sig(body, ev["timestamp"], "wrong-secret")).status_code == 401
    assert post(999999999, ev).status_code == 404
    assert httpx.post(f"{B}/api/agents/{zl['agent']}/channels", json={"kind": "fake"}).status_code == 401   # other paths still need auth
    time.sleep(2); assert not f.to(8000)

def test_text_reply_and_first_refresh(zl):
    f = zl["fake"]
    r = post(zl["bid"], event(8001, "xin chào shop")); assert r.status_code == 200 and r.json() == {"ok": True}
    out = wait_for(lambda: f.to(8001))
    assert untrusted(out[0]["text"], "xin chào shop") and out[0]["token"] == "AT1"
    assert f.refresh_used == ["RT0"] and not f.reused
    s = _secret_value(zl)
    assert s["refresh_token"] == "RT1" and s["access_token"] == "AT1" and s["access_expires_at"] > time.time() + 80000
    with pg() as c:
        md = c.execute("SELECT metadata FROM lg_runs WHERE workspace_id=%s ORDER BY created_at DESC LIMIT 1", (zl["ws"],)).fetchone()[0]
        assert md["channel"] == "zalo" and md["binding_id"] == zl["bid"] and md["external_user"] == "8001"

def test_duplicate_retry_ignored(zl):
    f = zl["fake"]; ev = event(8002, "đơn hàng của tôi", msg_id=f"dup-{SUF}")
    assert post(zl["bid"], ev).status_code == 200
    r = post(zl["bid"], ev, num_retry="1"); assert r.status_code == 200 and r.json().get("duplicate") is True
    wait_for(lambda: f.to(8002)); time.sleep(3)
    assert len(f.to(8002)) == 1

def test_long_reply_split(zl):
    f = zl["fake"]
    post(zl["bid"], event(8003, "dài " * 1200))
    wait_for(lambda: len(f.to(8003)) >= 3, timeout=90); time.sleep(1)
    out = f.to(8003)
    assert len(out) >= 3 and all(len(o["text"]) <= 2000 for o in out) and out[0]["text"].startswith("Echo: [UNTRUSTED DATA") and "\ndài" in out[0]["text"]
    assert sum(len(o["text"]) for o in out) > 4500

def test_image_reaches_agent(zl):
    f = zl["fake"]; f.files["p.png"] = PNG
    post(zl["bid"], event(8004, None, "image", [{"type": "image", "payload": {"url": f"http://127.0.0.1:{PORT}/files/p.png", "thumbnail": ""}}]))
    out = wait_for(lambda: f.to(8004))
    assert f"I received 1 image(s): image/png {len(PNG)} B" in out[0]["text"], out[0]["text"]
    f.files["bao_gia.txt"] = "Bảng giá: sản phẩm A giá 120.000 đồng.\n".encode()
    post(zl["bid"], event(8005, "tóm tắt tệp", "file", [{"type": "file", "payload": {"url": f"http://127.0.0.1:{PORT}/files/bao_gia.txt", "name": "bao_gia.txt", "size": 60}}]))
    out = wait_for(lambda: f.to(8005))
    assert "120.000" in out[0]["text"], out[0]["text"]

OLD_OK = {"timeout_s": 120, "max_event_age_s": 10 * 86400}   # 9.P2: old event timestamps are normally dropped (2 h freshness); these window tests need them processed

def test_outside_48h_default_no_send_logged(zl):
    f = zl["fake"]
    assert zl["adm"].patch(f"/api/channels/{zl['bid']}", json={"config": OLD_OK}).status_code == 200
    try:
        post(zl["bid"], event(8006, "tin cũ", ts_ms=int((time.time() - 3 * 86400) * 1000)))
        wait_for(lambda: log_has(f"zalo: binding {zl['bid']}: no reply to user 8006: outside the 48 h window"), timeout=30)
        time.sleep(1); assert not f.to(8006)
    finally:
        zl["adm"].patch(f"/api/channels/{zl['bid']}", json={"config": {"timeout_s": 120}})

def test_7_day_rule_with_flag(zl):
    f = zl["fake"]
    assert zl["adm"].patch(f"/api/channels/{zl['bid']}", json={"config": {**OLD_OK, "allow_after_48h": True}}).status_code == 200
    try:
        post(zl["bid"], event(8007, "tin 3 ngày", ts_ms=int((time.time() - 3 * 86400) * 1000)))
        assert untrusted(wait_for(lambda: f.to(8007))[0]["text"], "tin 3 ngày")                    # 48 h-7 d allowed by the flag
        post(zl["bid"], event(8008, "tin 8 ngày", ts_ms=int((time.time() - 8 * 86400) * 1000)))
        wait_for(lambda: log_has(f"zalo: binding {zl['bid']}: no reply to user 8008: last user message is older than 7 days"), timeout=30)
        time.sleep(1); assert not f.to(8008)
    finally:
        zl["adm"].patch(f"/api/channels/{zl['bid']}", json={"config": {"timeout_s": 120}})

def _approval(zl, uid, text):
    import re
    f = zl["fake"]; post(zl["bid"], event(uid, text))
    out = wait_for(lambda: f.to(uid))
    assert "content_publish(" in out[0]["text"]
    m = re.search(r"Trả lời 'duyệt (\d+)' hoặc 'từ chối \1'", out[0]["text"]); assert m, out[0]["text"]
    return int(m[1])

def test_approval_via_text(zl):
    f = zl["fake"]
    aid = _approval(zl, 8010, "publish bài khuyến mãi")
    post(zl["bid"], event(8010, f"duyệt {aid}"))
    out = wait_for(lambda: len(f.to(8010)) >= 2 and f.to(8010))
    assert "content_publish" in out[1]["text"] and "duyệt" not in out[1]["text"]
    with pg() as c: assert c.execute("SELECT status FROM approvals WHERE id=%s", (aid,)).fetchone()[0] == "approved"
    aid2 = _approval(zl, 8011, "publish thông báo nghỉ lễ")
    post(zl["bid"], event(8011, f"Từ chối {aid2}"))
    wait_for(lambda: len(f.to(8011)) >= 2)
    with pg() as c: assert c.execute("SELECT status FROM approvals WHERE id=%s", (aid2,)).fetchone()[0] == "rejected"
    post(zl["bid"], event(8012, f"duyệt {aid}"))                                                   # another chat cannot decide it
    time.sleep(3); assert not f.to(8012)

def test_refresh_rotation_on_rejected_token(zl):
    f = zl["fake"]
    used0 = list(f.refresh_used); cur = _secret_value(zl)
    f.access_valid.discard(cur["access_token"])                       # Zalo revokes the access token -> -216 on the next send
    for uid in (8020, 8021, 8022): post(zl["bid"], event(uid, f"chào {uid}"))   # concurrent senders: exactly one refresh
    for uid in (8020, 8021, 8022): wait_for(lambda: f.to(uid))
    new = f.refresh_used[len(used0):]
    assert new == [cur["refresh_token"]] and not f.reused             # the old refresh token used once, never reused
    s = _secret_value(zl)
    assert s["refresh_token"] == f"RT{f.n}" and s["refresh_token"] != cur["refresh_token"] and s["access_token"] == f"AT{f.n}"
    assert {m["token"] for uid in (8020, 8021, 8022) for m in f.to(uid)} == {s["access_token"]}
