"""GĐ6.7 TOTP 2FA + trang phiên đăng nhập. Chạy trên server local :8020 (fake LLM). Mã TOTP tính bằng pyotp."""
import json, os, uuid
import httpx, psycopg, pyotp, pytest
from test_fixes import B, ADMIN, a  # noqa: F401

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
PW = "t67-password-123"
UA1 = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0 Safari/537.36"
UA2 = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Safari/605.1"

def db(sql, args=()):
    with psycopg.connect(PG, autocommit=True) as c:
        cur = c.execute(sql, args or None); return cur.fetchall() if cur.description else None

def cookie(r, name="aos_s"):
    for h in r.headers.get_list("set-cookie"):
        if h.startswith(name + "="): return h.split(";")[0].split("=", 1)[1]

def login(user, ua=UA1, td=None):
    h = {"User-Agent": ua}
    if td: h["Cookie"] = f"aos_td={td}"
    return httpx.post(B + "/api/auth/login", json={"username": user["username"], "password": PW}, headers=h, timeout=30)

def sess(tok, ua=UA1): return httpx.Client(base_url=B, headers={"Cookie": f"aos_s={tok}", "User-Agent": ua}, timeout=30)

def code(secret, uid):
    db("UPDATE users SET totp_last_step=0 WHERE id=%s", (uid,))   # mã TOTP đã dùng không dùng lại -> reset để test lấy mã hiện tại
    return pyotp.TOTP(secret).now()

def mkuser(a, demo=False, role="editor"):
    n = "t67-" + uuid.uuid4().hex[:8]
    r = a.post("/api/admin/users", json={"username": n, "password": PW, "role": "viewer" if demo else role, "demo": demo}); assert r.status_code == 200, r.text
    return {"username": n, "id": r.json()["id"]}

@pytest.fixture
def user(a):
    u = mkuser(a); yield u
    db("DELETE FROM users WHERE username LIKE 't67-%'")

@pytest.fixture
def c2(user):
    """user with 2FA enabled: (user, secret, recovery_codes)"""
    tok = cookie(login(user)); c = sess(tok)
    secret = c.post("/api/auth/2fa/setup").json()["secret"]
    r = c.post("/api/auth/2fa/enable", json={"code": code(secret, user["id"])}); assert r.status_code == 200, r.text
    return user, secret, r.json()["recovery_codes"], c

def step2(user, secret, **kw):
    ch = login(user); assert ch.json().get("need_2fa"), ch.text
    body = {"challenge": ch.json()["challenge"], **kw}
    if "code" not in kw and "recovery_code" not in kw: body["code"] = code(secret, user["id"])
    return httpx.post(B + "/api/auth/2fa/verify", json=body, headers={"User-Agent": UA1}, timeout=30)

def test_enroll_enable_and_secret_encrypted(user):
    c = sess(cookie(login(user)))
    assert c.get("/api/auth/2fa").json()["enabled"] is False
    s = c.post("/api/auth/2fa/setup").json()
    assert s["otpauth_uri"].startswith("otpauth://totp/") and s["secret"] in s["otpauth_uri"] and s["qr_svg"].startswith("<svg")
    enc = db("SELECT totp_secret_enc FROM users WHERE id=%s", (user["id"],))[0][0]
    assert enc.startswith("v2:") and s["secret"] not in enc                      # lưu mã hoá, không lộ bản rõ
    assert c.post("/api/auth/2fa/enable", json={"code": "000000"}).status_code == 401
    r = c.post("/api/auth/2fa/enable", json={"code": code(s["secret"], user["id"])}); assert r.status_code == 200, r.text
    rc = r.json()["recovery_codes"]; assert len(rc) == 10 and len(set(rc)) == 10
    hashes = db("SELECT totp_recovery, totp_enabled FROM users WHERE id=%s", (user["id"],))[0]
    assert hashes[1] and len(hashes[0]) == 10 and not any(x.replace("-", "") in "".join(hashes[0]) for x in rc)   # chỉ lưu băm
    st = c.get("/api/auth/2fa").json(); assert st["enabled"] and st["recovery_left"] == 10
    assert c.post("/api/auth/2fa/setup").status_code == 409                       # đã bật thì không đăng ký lại

def test_login_requires_code(c2):
    user, secret, _, _ = c2
    r = login(user); assert r.status_code == 200 and r.json()["need_2fa"] and r.json()["challenge"]
    assert cookie(r) is None                                                     # chưa có phiên
    assert httpx.post(B + "/api/auth/2fa/verify", json={"challenge": "x" * 40, "code": "123456"}).status_code == 401
    ok = step2(user, secret); assert ok.status_code == 200 and cookie(ok)
    assert sess(cookie(ok)).get("/api/auth/me").json()["username"] == user["username"]
    assert httpx.get(B + "/api/auth/me", auth=(user["username"], PW)).status_code == 401   # Basic không lách được 2 lớp

def test_replayed_code_rejected(c2):
    user, secret, _, _ = c2
    ch = login(user).json()["challenge"]; cd = code(secret, user["id"])
    assert httpx.post(B + "/api/auth/2fa/verify", json={"challenge": ch, "code": cd}).status_code == 200
    ch2 = login(user).json()["challenge"]
    assert httpx.post(B + "/api/auth/2fa/verify", json={"challenge": ch2, "code": cd}).status_code == 401

def test_wrong_code_x5_lockout(c2):
    user, secret, _, _ = c2
    ch = login(user).json()["challenge"]
    codes = []
    for i in range(5):
        r = httpx.post(B + "/api/auth/2fa/verify", json={"challenge": ch, "code": "11111%d" % i}); codes.append(r.status_code)
    assert codes == [401, 401, 401, 401, 429], codes
    assert "15 phút" in r.json()["detail"] and "Retry-After" in r.headers
    r = httpx.post(B + "/api/auth/2fa/verify", json={"challenge": ch, "code": code(secret, user["id"])})   # đúng mã vẫn bị khoá
    assert r.status_code == 429 and "phút" in r.json()["detail"]
    db("UPDATE users SET totp_locked_until=now()-interval '1 minute' WHERE id=%s", (user["id"],))
    assert httpx.post(B + "/api/auth/2fa/verify", json={"challenge": ch, "code": code(secret, user["id"])}).status_code == 200

def test_recovery_code_works_once(c2):
    user, secret, rc, _ = c2
    r = step2(user, secret, recovery_code=rc[0]); assert r.status_code == 200 and cookie(r)
    assert step2(user, secret, recovery_code=rc[0]).status_code == 401           # dùng lại không được
    assert step2(user, secret, recovery_code=rc[1].upper().replace("-", "")).status_code == 200   # không phân biệt hoa/thường, dấu gạch
    assert db("SELECT cardinality(totp_recovery) FROM users WHERE id=%s", (user["id"],))[0][0] == 8

def test_remember_device_skips_step2(c2):
    user, secret, _, _ = c2
    r = step2(user, secret, remember_device=True); td = cookie(r, "aos_td"); assert td
    assert "max-age=2592000" in [h for h in r.headers.get_list("set-cookie") if h.startswith("aos_td=")][0].lower()
    d = login(user, td=td); assert d.status_code == 200 and cookie(d) and not d.json().get("need_2fa")
    assert login(user, ua=UA2, td=td).json().get("need_2fa")                      # thiết bị khác -> hỏi mã
    # tắt 2 lớp rồi bật lại -> cookie cũ vô hiệu
    db("UPDATE users SET totp_secret_enc=NULL, totp_enabled=false WHERE id=%s", (user["id"],))
    c = sess(cookie(login(user))); s = c.post("/api/auth/2fa/setup").json()["secret"]
    assert c.post("/api/auth/2fa/enable", json={"code": code(s, user["id"])}).status_code == 200
    assert login(user, td=td).json().get("need_2fa")

def test_disable_and_regenerate(c2):
    user, secret, rc, c = c2
    assert c.post("/api/auth/2fa/disable", json={"password": "sai-mat-khau", "code": code(secret, user["id"])}).status_code == 400
    assert c.post("/api/auth/2fa/recovery/regenerate", json={"code": "000000"}).status_code == 401
    new = c.post("/api/auth/2fa/recovery/regenerate", json={"code": code(secret, user["id"])}); assert new.status_code == 200
    assert len(new.json()["recovery_codes"]) == 10 and step2(user, secret, recovery_code=rc[0]).status_code == 401   # bộ cũ vô hiệu
    r = c.post("/api/auth/2fa/disable", json={"password": PW, "code": code(secret, user["id"])}); assert r.status_code == 200, r.text
    row = db("SELECT totp_secret_enc, totp_enabled, cardinality(totp_recovery) FROM users WHERE id=%s", (user["id"],))[0]
    assert row == (None, False, 0)
    d = login(user); assert d.status_code == 200 and cookie(d) and not d.json().get("need_2fa")

def test_admin_reset(a, c2):
    user, secret, _, _ = c2
    assert login(user).json().get("need_2fa")
    r = a.post(f"/api/admin/users/{user['id']}/2fa/reset"); assert r.status_code == 200, r.text
    d = login(user); assert cookie(d) and not d.json().get("need_2fa")
    assert a.post("/api/admin/users/999999/2fa/reset").status_code == 404
    tok = cookie(login(user))
    assert sess(tok).post(f"/api/admin/users/{user['id']}/2fa/reset").status_code == 403   # editor không phải admin

def test_require_2fa_restriction(a, user):
    wid = a.get("/api/auth/me").json()["ws"]
    assert a.patch(f"/api/admin/workspaces/{wid}/security", json={"require_2fa": True}).status_code == 200
    try:
        r = login(user); assert r.json()["must_enroll_2fa"] is True
        c = sess(cookie(r))
        me = c.get("/api/auth/me").json(); assert me["must_enroll_2fa"] is True
        blocked = c.get("/api/obs/projects"); assert blocked.status_code == 403 and blocked.json()["must_enroll_2fa"] is True
        assert c.post("/api/v1/threads/search", json={}).status_code == 403
        secret = c.post("/api/auth/2fa/setup").json()["secret"]
        assert c.post("/api/auth/2fa/enable", json={"code": code(secret, user["id"])}).status_code == 200
        assert c.get("/api/obs/projects").status_code == 200                      # bật xong thì mở lại
        assert not c.get("/api/auth/me").json().get("must_enroll_2fa")
        assert any(w["id"] == wid and w["require_2fa"] for w in a.get("/api/admin/workspaces").json())
    finally:
        a.patch(f"/api/admin/workspaces/{wid}/security", json={"require_2fa": False})
    assert a.patch(f"/api/admin/workspaces/{wid + 999}/security", json={"require_2fa": True}).status_code == 403

def test_demo_cannot_enable(a):
    d = mkuser(a, demo=True)
    try:
        c = sess(cookie(login(d)))
        assert c.post("/api/auth/2fa/setup").status_code == 403
        assert c.post("/api/auth/2fa/enable", json={"code": "123456"}).status_code == 403
        assert db("SELECT totp_enabled, totp_secret_enc FROM users WHERE id=%s", (d["id"],))[0] == (False, None)
    finally:
        db("DELETE FROM users WHERE username LIKE 't67-%'")

def test_sessions_list_revoke(user):
    t1 = cookie(login(user, UA1)); t2 = cookie(login(user, UA2)); t3 = cookie(login(user, UA1))
    c1 = sess(t1)
    ls = c1.get("/api/auth/sessions"); assert ls.status_code == 200
    rows = ls.json(); assert len(rows) == 3
    assert t1 not in ls.text and t2 not in ls.text and "token" not in ls.text            # không bao giờ trả token
    assert all(len(r["id"]) == 16 for r in rows) and sum(r["current"] for r in rows) == 1
    cur = next(r for r in rows if r["current"]); assert cur["device"] == "Chrome · Windows" and cur["ip"] and cur["last_seen"]
    other = next(r for r in rows if r["device"] == "Safari · iOS")
    assert c1.delete(f"/api/auth/sessions/{other['id']}").status_code == 200
    assert sess(t2, UA2).get("/api/auth/me").status_code == 401                         # phiên bị thu hồi
    assert c1.delete(f"/api/auth/sessions/{other['id']}").status_code == 404
    assert c1.delete("/api/auth/sessions/zzz").status_code == 400
    r = c1.post("/api/auth/sessions/revoke-others"); assert r.status_code == 200 and r.json()["revoked"] == 1
    assert sess(t3).get("/api/auth/me").status_code == 401 and c1.get("/api/auth/me").status_code == 200
    assert c1.delete(f"/api/auth/sessions/{cur['id']}").status_code == 200              # tự đăng xuất phiên hiện tại
    assert c1.get("/api/auth/me").status_code == 401

def test_session_last_seen_throttled(user):
    t = cookie(login(user)); c = sess(t)
    q = "SELECT last_seen FROM sessions WHERE user_id=%s"
    db("UPDATE sessions SET last_seen=now()-interval '5 minutes' WHERE user_id=%s", (user["id"],))
    old = db(q, (user["id"],))[0][0]; c.get("/api/auth/me")
    new = db(q, (user["id"],))[0][0]; assert new > old
    c.get("/api/auth/me"); assert db(q, (user["id"],))[0][0] == new                     # trong 1 phút không cập nhật lại
