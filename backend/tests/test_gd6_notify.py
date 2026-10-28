"""GĐ6.4 gửi cảnh báo thật (notify.py): máy chủ Telegram/SMTP/webhook giả chạy trong test, $0, không gọi dịch vụ thật.
notify chạy trong tiến trình test, nối thẳng Postgres local; chỉ vài test chạm API của server :8020."""
import asyncio, base64, hashlib, hmac, json, os, socketserver, sys, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import httpx, psycopg, pytest
from psycopg.rows import dict_row
from test_fixes import B, ADMIN, a  # noqa: F401

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
TOKEN = "123456:TEST-bot-token-xyz"; SMTP_PW = "smtp-pw-t64"; HOOK_KEY = "hook-key-t64"
WS = 1

# ------------------------------------------------------------ máy chủ giả
class Fake:
    def __init__(self, code=200): self.got = []; self.code = code
class _H(BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.fake.got.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "raw": raw})
        self.send_response(self.server.fake.code); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass

@pytest.fixture
def http():
    srv = HTTPServer(("127.0.0.1", 0), _H); srv.fake = Fake(); threading.Thread(target=srv.serve_forever, daemon=True).start()
    srv.fake.url = f"http://127.0.0.1:{srv.server_port}"; yield srv.fake; srv.shutdown(); srv.server_close()

class _S(socketserver.StreamRequestHandler):
    """SMTP tối thiểu: EHLO, AUTH PLAIN, MAIL, RCPT, DATA, QUIT."""
    def handle(self):
        f = self.server.fake; w = lambda s: self.wfile.write(s.encode() + b"\r\n")
        w("220 fake ESMTP")
        while True:
            line = self.rfile.readline().decode().strip()
            if not line: return
            c = line.upper()
            if c.startswith("EHLO"): self.wfile.write(b"250-fake\r\n250 AUTH PLAIN\r\n")
            elif c.startswith("AUTH PLAIN"):
                f.auth = base64.b64decode(line.split()[2]).split(b"\0"); w("235 ok")
            elif c.startswith("MAIL"): f.mail_from = line; w("250 ok")
            elif c.startswith("RCPT"): f.rcpt.append(line); w("250 ok")
            elif c == "DATA":
                w("354 go"); buf = []
                while True:
                    l = self.rfile.readline().decode()
                    if l in (".\r\n", ".\n"): break
                    buf.append(l)
                f.data = "".join(buf); f.messages += 1; w("250 queued")
            elif c.startswith("QUIT"): w("221 bye"); return
            else: w("250 ok")

@pytest.fixture
def smtp():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _S); srv.daemon_threads = True
    f = srv.fake = Fake(); f.rcpt = []; f.messages = 0; f.auth = None; f.port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start(); yield f; srv.shutdown(); srv.server_close()

# ------------------------------------------------------------ hạ tầng trong tiến trình
async def real_q(sql, args=(), one=False, many=True):
    async with await psycopg.AsyncConnection.connect(PG, autocommit=True, row_factory=dict_row) as c:
        cur = await c.execute(sql, args)
        if cur.description is None: return None
        rows = await cur.fetchall()
        return (rows[0] if rows else None) if one else rows

@pytest.fixture
def env(monkeypatch, tmp_path):
    import crypto, notify, ws_secrets
    monkeypatch.setattr(crypto, "_RING", None); monkeypatch.setattr(crypto, "KEY_FILE", tmp_path / "k")
    monkeypatch.setenv("AOS_SECRET_KEY", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setattr(ws_secrets, "_q", real_q); monkeypatch.setattr(notify, "_q", real_q)
    monkeypatch.delenv("TELEGRAM_API_BASE", raising=False)
    names = []
    def sec(name, value, hosts):
        asyncio.run(real_q("INSERT INTO ws_secrets(workspace_id,name,value_enc,allowed_hosts) VALUES(%s,%s,%s,%s) ON CONFLICT(workspace_id,name) DO UPDATE SET value_enc=EXCLUDED.value_enc, allowed_hosts=EXCLUDED.allowed_hosts",
                           (WS, name, crypto.encrypt(value), hosts))); names.append(name)
    yield notify, sec
    with psycopg.connect(PG, autocommit=True) as c:
        for n in names: c.execute("DELETE FROM ws_secrets WHERE workspace_id=%s AND name=%s", (WS, n))
        c.execute("DELETE FROM outbox WHERE subject LIKE 't64-%'")

run = asyncio.run; CRLF = chr(13) + chr(10)

# ------------------------------------------------------------ Telegram
def test_telegram_delivers_and_escapes(env, http, monkeypatch):
    notify, sec = env
    monkeypatch.setenv("TELEGRAM_API_BASE", http.url); sec("T64_TG", TOKEN, ["127.0.0.1"])
    r = run(notify.notify(WS, {"kind": "telegram", "target": "-100555", "secret": "T64_TG"}, "t64 <b>x</b>", "a & b <script>", "https://x.test/?a=1&b=2"))
    assert r["ok"], r
    g = http.got[0]; body = json.loads(g["raw"])
    assert g["path"] == f"/bot{TOKEN}/sendMessage" and body["chat_id"] == "-100555" and body["parse_mode"] == "HTML"
    assert "&lt;b&gt;x&lt;/b&gt;" in body["text"] and "a &amp; b &lt;script&gt;" in body["text"] and "&amp;b=2" in body["text"]
    assert TOKEN not in json.dumps(r)

def test_host_not_allowed_fails_without_sending(env, http, monkeypatch):
    notify, sec = env
    monkeypatch.setenv("TELEGRAM_API_BASE", http.url); sec("T64_TG2", TOKEN, ["api.telegram.org"])      # 127.0.0.1 không được phép
    r = run(notify.notify(WS, {"kind": "telegram", "target": "1", "secret": "T64_TG2"}, "t64", "x"))
    assert not r["ok"] and r["permanent"] and http.got == [] and TOKEN not in r["detail"]
    r = run(notify.notify(WS, {"kind": "telegram", "target": "1", "secret": "T64_NOPE"}, "t64", "x"))      # bí mật không tồn tại
    assert not r["ok"] and r["permanent"] and http.got == []

def test_slack_webhook(env, http, monkeypatch):
    notify, sec = env
    monkeypatch.setenv("SLACK_HOOK_HOST", "127.0.0.1"); sec("T64_SL", http.url + "/services/T/B/X", ["127.0.0.1"])
    assert run(notify.notify(WS, {"kind": "slack", "secret": "T64_SL"}, "t64 tiêu đề", "nội dung"))["ok"]
    assert http.got[0]["path"] == "/services/T/B/X" and "t64 tiêu đề" in json.loads(http.got[0]["raw"])["text"]
    sec("T64_SL2", "http://evil.test/hook", ["127.0.0.1"])                                                 # URL trong bí mật trỏ sang máy khác
    r = run(notify.notify(WS, {"kind": "slack", "secret": "T64_SL2"}, "t64", "x")); assert not r["ok"] and r["permanent"] and len(http.got) == 1

# ------------------------------------------------------------ webhook + HMAC
def test_webhook_hmac_signature(env, http):
    notify, sec = env
    sec("T64_HK", HOOK_KEY, ["127.0.0.1"])
    r = run(notify.notify(WS, {"kind": "webhook", "target": http.url + "/h", "signing_secret": "T64_HK"}, "t64 hook", "body", "https://l.test"))
    assert r["ok"], r
    g = http.got[0]; sig = g["headers"]["x-aos-signature"]; ts = g["headers"]["x-aos-timestamp"]
    assert sig == "sha256=" + hmac.new(HOOK_KEY.encode(), ts.encode() + b"." + g["raw"], hashlib.sha256).hexdigest()
    assert abs(int(ts) - __import__("time").time()) < 30 and json.loads(g["raw"])["subject"] == "t64 hook"
    # không cấu hình bí mật: vẫn gửi tới URL nội bộ, không có chữ ký
    assert run(notify.notify(WS, {"kind": "webhook", "target": http.url + "/h"}, "t64", "x"))["ok"] and "x-aos-signature" not in http.got[1]["headers"]
    # đích ngoài mà không có khoá ký -> từ chối
    r = run(notify.notify(WS, {"kind": "webhook", "target": "https://example.invalid/h"}, "t64", "x")); assert not r["ok"] and r["permanent"]

# ------------------------------------------------------------ Email SMTP
def test_email_smtp(env, smtp):
    notify, sec = env
    sec("T64_SMTP", SMTP_PW, ["127.0.0.1"])
    ch = {"kind": "email", "target": "anh@example.test, b@example.test", "host": "127.0.0.1", "port": smtp.port, "user": "bot", "starttls": False, "password": "{{secret:T64_SMTP}}", "from": "aos@example.test"}
    r = run(notify.notify(WS, ch, "t64 cảnh báo\r\nBcc: x@y", "nội dung", "https://l.test")); assert r["ok"], r
    assert smtp.messages == 1 and smtp.auth[1:] == [b"bot", SMTP_PW.encode()] and len(smtp.rcpt) == 2
    hdrs = smtp.data.split(CRLF * 2)[0].split(CRLF)
    assert not any(h.lower().startswith("bcc:") for h in hdrs) and "https://l.test" in smtp.data            # CRLF trong tiêu đề không chèn được header
    sec("T64_SMTP2", SMTP_PW, ["smtp.other.test"])
    r = run(notify.notify(WS, {**ch, "password": "{{secret:T64_SMTP2}}"}, "t64", "x")); assert not r["ok"] and r["permanent"] and smtp.messages == 1

def test_email_unreachable_is_transient(env):
    notify, _ = env
    r = run(notify.notify(WS, {"kind": "email", "target": "a@b.test", "host": "127.0.0.1", "port": 1, "starttls": False}, "t64", "x"))
    assert not r["ok"] and not r["permanent"]

# ------------------------------------------------------------ outbox + retry
def _row(i): return run(real_q("SELECT * FROM outbox WHERE id=%s", (i,), one=True))

def test_transition_table():
    from notify import transition
    ok, bad, perm = {"ok": True}, {"ok": False}, {"ok": False, "permanent": True}
    assert transition(1, ok) == ("sent", None) and transition(1, perm) == ("failed", None)
    assert [transition(n, bad) for n in (1, 2, 3, 4)] == [("pending", 1), ("pending", 5), ("pending", 15), ("failed", None)]

def test_outbox_retry_then_success(env, http, monkeypatch):
    notify, sec = env
    monkeypatch.setenv("TELEGRAM_API_BASE", http.url); sec("T64_TG3", TOKEN, ["127.0.0.1"])
    http.code = 500                                                                                        # lần 1 lỗi tạm thời
    st = run(notify.send(WS, {"kind": "telegram", "target": "9", "secret": "T64_TG3"}, "t64-retry", "nội dung"))
    assert st.startswith("pending")
    row = run(real_q("SELECT * FROM outbox WHERE subject='t64-retry'", one=True))
    assert (row["status"], row["attempts"], row["last_error"]) == ("pending", 1, "HTTP 500") and row["next_at"] > row["created"]
    assert TOKEN not in json.dumps(row, default=str)
    run(notify.retry_due()); assert _row(row["id"])["attempts"] == 1 and len(http.got) == 1                 # chưa đến giờ thử lại
    for expected_wait in (5, 15):                                                                         # lần 2, 3 vẫn lỗi: giãn 5 rồi 15 phút
        run(real_q("UPDATE outbox SET next_at=now() - interval '1 second' WHERE id=%s", (row["id"],))); run(notify.retry_due())
        r = _row(row["id"]); wait = round((r["next_at"] - __import__("datetime").datetime.now(r["next_at"].tzinfo)).total_seconds() / 60)
        assert r["status"] == "pending" and wait == expected_wait
    http.code = 200                                                                                        # lần 4 thành công
    run(real_q("UPDATE outbox SET next_at=now() - interval '1 second' WHERE id=%s", (row["id"],))); run(notify.retry_due())
    r = _row(row["id"]); assert (r["status"], r["attempts"], r["next_at"], r["last_error"]) == ("sent", 4, None, None) and len(http.got) == 4

def test_outbox_gives_up_after_three_retries(env, http, monkeypatch):
    notify, sec = env
    monkeypatch.setenv("TELEGRAM_API_BASE", http.url); sec("T64_TG4", TOKEN, ["127.0.0.1"]); http.code = 503
    run(notify.send(WS, {"kind": "telegram", "target": "9", "secret": "T64_TG4"}, "t64-giveup", "x"))
    i = run(real_q("SELECT id FROM outbox WHERE subject='t64-giveup'", one=True))["id"]
    for _ in range(3):
        run(real_q("UPDATE outbox SET next_at=now() - interval '1 second' WHERE id=%s", (i,))); run(notify.retry_due())
    r = _row(i); assert (r["status"], r["attempts"], r["next_at"]) == ("failed", 4, None) and len(http.got) == 4
    run(notify.retry_due()); assert len(http.got) == 4                                                    # failed thì không thử nữa

def test_permanent_failure_is_failed_immediately(env, http, monkeypatch):
    notify, sec = env
    monkeypatch.setenv("TELEGRAM_API_BASE", http.url); sec("T64_TG5", TOKEN, ["api.telegram.org"])
    assert run(notify.send(WS, {"kind": "telegram", "target": "9", "secret": "T64_TG5"}, "t64-perm", "x")).startswith("failed")
    r = run(real_q("SELECT * FROM outbox WHERE subject='t64-perm'", one=True)); assert (r["status"], r["attempts"]) == ("failed", 1) and http.got == []

# ------------------------------------------------------------ demo
def test_demo_sends_nothing(env, http, monkeypatch):
    import contextvars, types
    notify, sec = env
    USER = contextvars.ContextVar("u", default=None)   # admin.py thật kéo cả deepagent_api (cần khoá Google) -> dùng bản giả cùng giao diện
    monkeypatch.setitem(sys.modules, "admin", types.SimpleNamespace(USER=USER, is_demo=lambda: bool((USER.get() or {}).get("demo"))))
    monkeypatch.setenv("TELEGRAM_API_BASE", http.url); sec("T64_TG6", TOKEN, ["127.0.0.1"])
    tok = USER.set({"id": 9, "username": "demo", "role": "viewer", "ws": WS, "demo": True})
    try:
        ch = {"kind": "telegram", "target": "9", "secret": "T64_TG6"}
        assert not run(notify.notify(WS, ch, "t64-demo", "x"))["ok"] and run(notify.send(WS, ch, "t64-demo", "x")).startswith("demo")
    finally: USER.reset(tok)
    assert http.got == [] and run(real_q("SELECT 1 FROM outbox WHERE subject='t64-demo'", one=True)) is None

# ------------------------------------------------------------ API (server :8020)
def test_alert_rule_api_validation(a):
    pid = a.get("/api/obs/projects").json()[0]["id"] if a.get("/api/obs/projects").status_code == 200 else 1
    base = {"project_id": pid, "name": "t64-rule", "metric": "count", "op": "gt", "threshold": 1e9}
    assert a.post("/api/obs/alerts", json={**base, "channel": "telegram", "target": "1"}).status_code == 422             # thiếu bí mật
    assert a.post("/api/obs/alerts", json={**base, "channel": "email", "target": "a@b.test", "config": {"host": "h", "password": "plain"}}).status_code == 422
    r = a.post("/api/obs/alerts", json={**base, "channel": "telegram", "target": "1", "config": {"secret": "T64_X", "junk": 1}}); assert r.status_code == 200, r.text
    try:
        rule = [x for x in a.get("/api/obs/alerts").json() if x["id"] == r.json()["id"]][0]
        assert rule["channel_cfg"] == {"secret": "T64_X"}
        t = a.post(f"/api/obs/alerts/{rule['id']}/test", params={"send": "true"}); assert t.status_code == 200 and t.json()["delivery"].startswith("failed")   # bí mật không tồn tại
        ob = [o for o in a.get("/api/obs/outbox").json() if o["subject"].startswith("[Moza Agent OS] Gửi thử: 't64-rule'")][0]
        assert ob["status"] == "failed" and ob["attempts"] == 1
    finally:
        a.delete(f"/api/obs/alerts/{r.json()['id']}")
        with psycopg.connect(PG, autocommit=True) as c: c.execute("DELETE FROM outbox WHERE subject LIKE '%Gửi thử: ''t64-rule''%'")
