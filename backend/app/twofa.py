"""GĐ6.7 đăng nhập 2 lớp (TOTP, pyotp) + trang phiên đăng nhập.
- Khoá TOTP mã hoá bằng crypto.py (cột users.totp_secret_enc); mã dự phòng chỉ lưu SHA-256, dùng 1 lần.
- Mật khẩu đúng + đã bật 2 lớp -> /api/auth/login trả {need_2fa, challenge} (token ký AES-GCM, 5 phút) chưa cấp cookie phiên;
  /api/auth/2fa/verify đổi challenge + mã lấy phiên. "Nhớ thiết bị" = cookie aos_td ký bằng crypto, 30 ngày, gắn người dùng + UA + khoá TOTP hiện tại
  (tắt/đặt lại/đăng ký lại 2 lớp thì cookie cũ vô hiệu).
- Sai mã 5 lần -> khoá 15 phút (429). Mã TOTP đã dùng không dùng lại được (totp_last_step).
- Workspace require_2fa: người chưa bật bị giới hạn ở RESTRICTED_OK cho tới khi bật xong."""
import hashlib, hmac, io, json, re, secrets, time
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import crypto, aos_crypto, pyotp, segno

router = APIRouter()
ISSUER = "Moza Agent OS"
CHALLENGE_TTL = 300; REMEMBER_TTL = 30 * 86400; MAX_FAILS = 5; LOCK_MIN = 15
TD_COOKIE = "aos_td"
# phiên bị giới hạn (workspace bắt buộc 2 lớp mà người này chưa bật): chỉ các đường dẫn này
RESTRICTED_OK = ("/api/auth/2fa/setup", "/api/auth/2fa/enable", "/api/auth/2fa", "/api/auth/me", "/api/auth/logout", "/api/auth/lang")

def _a():
    import admin; return admin
async def _q(*a, **k):
    import deepagent_api as A; return await A.q(*a, **k)
def _sha(x: str) -> str: return hashlib.sha256(x.encode()).hexdigest()

def restricted_ok(path: str) -> bool: return path in RESTRICTED_OK

def device_name(ua: str) -> str:
    """Tên thiết bị ngắn từ User-Agent, ví dụ 'Chrome · Windows'."""
    ua = ua or ""
    br = next((n for rx, n in ((r"Edg/", "Edge"), (r"OPR/|Opera", "Opera"), (r"Firefox/", "Firefox"), (r"Chrome/|CriOS/", "Chrome"), (r"Safari/", "Safari"), (r"curl/|python-httpx|requests", "Script/API")) if re.search(rx, ua)), "")
    os_ = next((n for rx, n in ((r"Windows", "Windows"), (r"Android", "Android"), (r"iPhone|iPad|iOS", "iOS"), (r"Mac OS X|Macintosh", "macOS"), (r"Linux|X11", "Linux")) if re.search(rx, ua)), "")
    return " · ".join(x for x in (br, os_) if x) or (ua[:40] or "Không rõ")

# ---- token ký (AES-GCM của crypto.py cũng chống sửa đổi)
def _seal(d: dict) -> str: return crypto.encrypt(json.dumps(d, separators=(",", ":")))
def _open(tok: str, kind: str) -> dict | None:
    try:
        d = json.loads(crypto.decrypt(tok))
        return d if d.get("k") == kind and d.get("exp", 0) > time.time() else None
    except Exception:  # noqa: BLE001
        return None

def make_challenge(uid: int) -> str: return _seal({"k": "chal", "u": uid, "exp": time.time() + CHALLENGE_TTL, "n": secrets.token_hex(4)})
def _dev_tag(user: dict, ua: str) -> str:
    """Gắn thiết bị với khoá TOTP hiện tại (đổi khoá -> mất 'nhớ thiết bị'). GĐ9.7: dựa trên khoá giải mã (không phải bản mã, vì xoay khoá làm bản mã đổi)."""
    try: s = secret_of(user) if user.get("totp_secret_enc") else ""
    except crypto.CryptoError: s = ""
    return crypto.mac(f"dev|{ua}|{s}")[3:27]
def make_remember(user: dict, ua: str) -> str: return _seal({"k": "dev", "u": user["id"], "d": _dev_tag(user, ua), "exp": time.time() + REMEMBER_TTL})
def remember_ok(user: dict, request: Request) -> bool:
    d = _open(request.cookies.get(TD_COOKIE) or "", "dev")
    return bool(d and d["u"] == user["id"] and hmac.compare_digest(d["d"], _dev_tag(user, request.headers.get("user-agent") or "")))

# ---- mã TOTP / mã dự phòng
def _sctx(uid) -> str: return aos_crypto.ctx("users.totp_secret_enc", uid)
def secret_of(user: dict) -> str: return crypto.unseal(user["totp_secret_enc"], crypto.PLATFORM_WS, _sctx(user["id"]))   # v2 (DEK nền tảng, AAD gắn user id) hoặc v1 cũ
def _rec_hash(c: str) -> str: return crypto.mac("rec|" + _norm_rec(c))   # GĐ9.7: HMAC có khoá (mã 40 bit không dò ngoại tuyến được khi lộ DB)
def _match_step(secret: str, code: str, after: int) -> int | None:
    code = re.sub(r"\s", "", code or "")
    if not re.fullmatch(r"\d{6}", code): return None
    t = pyotp.TOTP(secret); now = int(time.time()) // 30
    for off in (0, -1, 1):   # lệch giờ ±30 s
        step = now + off
        if step > after and hmac.compare_digest(t.at(step * 30), code): return step
    return None
def _norm_rec(c: str) -> str: return re.sub(r"[^0-9a-f]", "", (c or "").lower())
def new_recovery_codes(n=10) -> list[str]:
    out = []
    for _ in range(n): h = secrets.token_hex(5); out.append(h[:5] + "-" + h[5:])
    return out

async def check_code(u: dict, code: str | None = None, recovery: str | None = None):
    """Kiểm tra mã TOTP hoặc mã dự phòng của người dùng (hàng users). Khoá 15 phút sau 5 lần sai. Lỗi -> HTTPException."""
    lk = await _q("SELECT extract(epoch FROM totp_locked_until - now())::float8 s FROM users WHERE id=%s", (u["id"],), one=True)
    if lk and lk["s"] and lk["s"] > 0: raise HTTPException(429, f"Nhập sai mã quá 5 lần. Đăng nhập 2 lớp bị khoá, thử lại sau {int(lk['s'] // 60) + 1} phút", headers={"Retry-After": str(int(lk["s"]) + 1)})
    ok = False
    if recovery:
        for h in (_rec_hash(recovery), _sha(_norm_rec(recovery))):   # m1 (HMAC) trước, rồi mã cũ SHA-256 (tới khi tạo lại mã)
            r = await _q("UPDATE users SET totp_recovery=array_remove(totp_recovery,%s) WHERE id=%s AND %s = ANY(totp_recovery) RETURNING id", (h, u["id"], h), one=True)   # nguyên tử: dùng 1 lần
            ok = bool(r)
            if ok: break
    elif code:
        step = _match_step(secret_of(u), code, int(u.get("totp_last_step") or 0))
        if step is not None:
            r = await _q("UPDATE users SET totp_last_step=%s WHERE id=%s AND totp_last_step < %s RETURNING id", (step, u["id"], step), one=True)
            ok = bool(r)
    if ok:
        await _q("UPDATE users SET totp_failed=0, totp_locked_until=NULL WHERE id=%s", (u["id"],)); return
    n = (await _q("UPDATE users SET totp_failed=totp_failed+1 WHERE id=%s RETURNING totp_failed", (u["id"],), one=True))["totp_failed"]
    if n >= MAX_FAILS:
        await _q("UPDATE users SET totp_failed=0, totp_locked_until=now()+%s*interval '1 minute' WHERE id=%s", (LOCK_MIN, u["id"]))
        raise HTTPException(429, f"Nhập sai mã {MAX_FAILS} lần. Đăng nhập 2 lớp bị khoá {LOCK_MIN} phút", headers={"Retry-After": str(LOCK_MIN * 60)})
    raise HTTPException(401, f"Mã không đúng. Còn {MAX_FAILS - n} lần thử" if code or recovery else "Cần nhập mã")

async def ws_requires(ws: int) -> bool:
    r = await _q("SELECT require_2fa FROM workspaces WHERE id=%s", (ws,), one=True)
    return bool(r and r["require_2fa"])

def _qr_svg(uri: str) -> str:
    b = io.BytesIO(); segno.make(uri, error="m").save(b, kind="svg", xmldecl=False, svgns=True, scale=5, border=2, dark="#111111", light="#ffffff")
    return b.getvalue().decode()

async def _me_row(refresh=True) -> dict:
    return await _q("SELECT * FROM users WHERE id=%s", (_a().cur()["id"],), one=True)

def _no_demo():
    if _a().is_demo(): raise HTTPException(403, "Tài khoản demo không bật được xác thực 2 lớp")

# ------------------------------------------------------------ đăng ký / bật / tắt
@router.get("/api/auth/2fa")
async def status():
    u = await _me_row(); ws = _a().cur_ws()
    return {"enabled": bool(u["totp_enabled"]), "pending_setup": bool(u["totp_secret_enc"]) and not u["totp_enabled"], "recovery_left": len(u["totp_recovery"] or []),
            "workspace_requires": await ws_requires(ws), "must_enroll": not u["totp_enabled"] and await ws_requires(ws), "demo": _a().is_demo()}

@router.post("/api/auth/2fa/setup")
async def setup():
    _no_demo(); u = await _me_row()
    if u["totp_enabled"]: raise HTTPException(409, "Đã bật xác thực 2 lớp. Tắt trước nếu muốn đăng ký lại")
    secret = pyotp.random_base32()
    await _q("UPDATE users SET totp_secret_enc=%s, totp_last_step=0 WHERE id=%s", (crypto.seal(secret, crypto.PLATFORM_WS, _sctx(u["id"])), u["id"]))
    uri = pyotp.TOTP(secret).provisioning_uri(name=u["username"], issuer_name=ISSUER)
    return {"otpauth_uri": uri, "secret": secret, "qr_svg": _qr_svg(uri)}

class CodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=12)

@router.post("/api/auth/2fa/enable")
async def enable(b: CodeIn):
    _no_demo(); u = await _me_row()
    if u["totp_enabled"]: raise HTTPException(409, "Đã bật xác thực 2 lớp")
    if not u["totp_secret_enc"]: raise HTTPException(400, "Chưa tạo mã QR. Gọi /api/auth/2fa/setup trước")
    await check_code(u, code=b.code)
    codes = new_recovery_codes()
    await _q("UPDATE users SET totp_enabled=true, totp_recovery=%s WHERE id=%s", ([_rec_hash(c) for c in codes], u["id"]))
    return {"ok": True, "recovery_codes": codes, "note": "Lưu 10 mã dự phòng này ở nơi an toàn. Chỉ hiện một lần, mỗi mã dùng được một lần."}

class DisableIn(BaseModel):
    password: str = Field(min_length=1, max_length=200)
    code: str | None = Field(default=None, max_length=12)
    recovery_code: str | None = Field(default=None, max_length=24)

async def _clear(uid: int):
    await _q("UPDATE users SET totp_secret_enc=NULL, totp_enabled=false, totp_recovery='{}', totp_failed=0, totp_locked_until=NULL, totp_last_step=0 WHERE id=%s", (uid,))

@router.post("/api/auth/2fa/disable")
async def disable(b: DisableIn):
    u = await _me_row()
    if not u["totp_enabled"]: raise HTTPException(400, "Chưa bật xác thực 2 lớp")
    if not _a().check_pw(b.password, u["pw_hash"]): raise HTTPException(400, "Mật khẩu sai")
    await check_code(u, code=b.code, recovery=b.recovery_code)
    await _clear(u["id"]); return {"ok": True}

@router.post("/api/auth/2fa/recovery/regenerate")
async def regenerate(b: CodeIn):
    u = await _me_row()
    if not u["totp_enabled"]: raise HTTPException(400, "Chưa bật xác thực 2 lớp")
    await check_code(u, code=b.code)
    codes = new_recovery_codes()
    await _q("UPDATE users SET totp_recovery=%s WHERE id=%s", ([_rec_hash(c) for c in codes], u["id"]))
    return {"ok": True, "recovery_codes": codes}

# ------------------------------------------------------------ bước 2 của đăng nhập (công khai: tự xác thực bằng challenge)
class VerifyIn(BaseModel):
    challenge: str = Field(min_length=10, max_length=2000)
    code: str | None = Field(default=None, max_length=12)
    recovery_code: str | None = Field(default=None, max_length=24)
    remember_device: bool = False

@router.post("/api/auth/2fa/verify")
async def verify(b: VerifyIn, request: Request):
    ch = _open(b.challenge, "chal")
    if not ch: raise HTTPException(401, "Phiên xác thực đã hết hạn, đăng nhập lại từ đầu")
    u = await _q("SELECT * FROM users WHERE id=%s AND NOT disabled", (ch["u"],), one=True)
    if not u or not u["totp_enabled"]: raise HTTPException(401, "Phiên xác thực không hợp lệ")
    if not (b.code or b.recovery_code): raise HTTPException(422, "Cần nhập mã 6 số hoặc mã dự phòng")
    try: await check_code(u, code=b.code, recovery=b.recovery_code)
    except HTTPException as e:
        await _a()._audit({"id": u["id"], "username": u["username"]}, request, e.status_code, 0, "mã 2 lớp sai"); raise
    r = await _a().issue_session(u, request)
    if b.remember_device:
        r.set_cookie(TD_COOKIE, make_remember(u, request.headers.get("user-agent") or ""), max_age=REMEMBER_TTL, httponly=True, secure=True, samesite="lax", path="/")
    return r

# ------------------------------------------------------------ admin
@router.post("/api/admin/users/{uid}/2fa/reset")
async def admin_reset(uid: int):
    a = _a(); me = a.cur()
    t = await _q("SELECT id, is_superadmin FROM users WHERE id=%s", (uid,), one=True)
    in_ws = await _q("SELECT 1 FROM memberships WHERE user_id=%s AND workspace_id=%s", (uid, a.cur_ws()), one=True)
    if not t or not in_ws: raise HTTPException(404, "Người dùng không thuộc workspace này")
    if t["is_superadmin"] and uid != me["id"] and not (await _q("SELECT is_superadmin FROM users WHERE id=%s", (me["id"],), one=True))["is_superadmin"]:
        raise HTTPException(403, "Chỉ chủ sở hữu mới đặt lại 2 lớp của chủ sở hữu")
    await a._manageable_user(uid)   # GĐ12.100c: a user who also belongs to a workspace this admin does not administer keeps their own 2FA
    await _clear(uid)
    await _q("DELETE FROM sessions WHERE user_id=%s", (uid,))   # đăng xuất mọi thiết bị của người đó
    return {"ok": True}

class Req2fa(BaseModel):
    require_2fa: bool

@router.patch("/api/admin/workspaces/{wid}/security")
async def ws_security(wid: int, b: Req2fa):
    if wid != _a().cur_ws(): raise HTTPException(403, "Chỉ đổi được workspace đang chọn")
    await _q("UPDATE workspaces SET require_2fa=%s WHERE id=%s", (b.require_2fa, wid)); return {"ok": True, "require_2fa": b.require_2fa}

# ------------------------------------------------------------ phiên đăng nhập của chính mình (không bao giờ trả token)
def _cur_hash(request: Request): t = request.cookies.get(_a().COOKIE); return _sha(t) if t else None

@router.get("/api/auth/sessions")
async def sessions(request: Request):
    cur = _cur_hash(request)
    rows = await _q("SELECT left(token_hash,16) id, token_hash, device, ip, created, last_seen, expires FROM sessions WHERE user_id=%s AND expires > now() ORDER BY last_seen DESC", (_a().cur()["id"],))
    for r in rows: r["current"] = r.pop("token_hash") == cur
    return rows

@router.delete("/api/auth/sessions/{sid}")
async def session_revoke(sid: str, request: Request):
    if not re.fullmatch(r"[0-9a-f]{16}", sid): raise HTTPException(400, "id phiên sai")
    r = await _q("DELETE FROM sessions WHERE user_id=%s AND left(token_hash,16)=%s RETURNING token_hash", (_a().cur()["id"], sid), one=True)
    if not r: raise HTTPException(404, "Không có phiên này")
    out = JSONResponse({"ok": True, "current": r["token_hash"] == _cur_hash(request)})
    if r["token_hash"] == _cur_hash(request): out.delete_cookie(_a().COOKIE, path="/")
    return out

@router.post("/api/auth/sessions/revoke-others")
async def sessions_revoke_others(request: Request):
    cur = _cur_hash(request)
    if not cur: raise HTTPException(400, "Chỉ phiên đăng nhập (cookie) mới dùng được thao tác này")
    rows = await _q("DELETE FROM sessions WHERE user_id=%s AND token_hash<>%s RETURNING 1", (_a().cur()["id"], cur))
    return {"ok": True, "revoked": len(rows)}
