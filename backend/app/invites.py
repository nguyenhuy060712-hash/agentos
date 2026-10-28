"""GĐ12.100 invite people to a workspace by LINK (no email column on users: 10.13 is deferred; an optional address lives ONLY on the invite row as `email_note`, never matched to a user).
Admin API (workspace admin of the CURRENT workspace, never demo): POST/GET /api/admin/workspaces/{wid}/invites, DELETE .../invites/{id}  (prefix /api/admin = ADMIN_ONLY in admin.py).
Public (PUBLIC_PREFIX '/invite/' + '/api/invites/'): GET /invite/{token} (HTML form) and POST /api/invites/{token}/accept.
Security rules (all enforced here, each has a negative test in tests/test_gd12_workspace.py):
  - the token is secrets.token_urlsafe(32); only its sha256 is stored; the link is returned ONCE; lists never show a token or a hash; the audit log and the outbox never get the token
    (audit paths are masked; the e-mail is sent with notify.notify, which does not write the outbox);
  - unknown / malformed / expired / revoked / used-up / demo-workspace / inviter-no-longer-allowed all give the SAME 404 (no oracle); bad tokens are rate limited per IP
    (20/min, counted in audit_log like the login back-off) plus a global ceiling, and a limited request writes nothing;
  - role ceiling: the invite role can never exceed the inviter's role in that workspace (checked when the invite is made AND again when it is used); only admins invite;
  - the accept is ONE transaction: `UPDATE ws_invites SET used_count = used_count + 1 WHERE ... used_count < max_uses` (two parallel accepts of a 1-use invite: exactly one wins)
    + the user / membership insert; a failure (name taken) rolls the use back;
  - a signed-in visitor only gets a membership (an existing role is never lowered); a demo account never joins; new accounts follow the admin user rules (name regex, password >= 10)."""
import asyncio, contextlib, hashlib, hmac, html, os, re, secrets
from types import SimpleNamespace
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

import notify

router = APIRouter()
ROLES = {"viewer": 1, "editor": 2, "admin": 3}
INVITER_MIN_ROLE = "admin"                       # who may invite (the /api/admin prefix already requires it; checked again here)
TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{43}$")   # token_urlsafe(32) is exactly 43 characters
EMAIL_RE = re.compile(r"^[^@\s<>,;\"'\\]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,}$")
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.\-]{2,64}$")   # = admin.UserIn
BAD_DETAIL = "mời: token sai"
MASKED_PATH = "/api/invites/*/accept"
BAD_PER_IP, BAD_ALL, MAX_PENDING = 20, 600, 200  # bad tokens per minute per IP / for everybody / open invites per workspace
GONE = "Lời mời không tồn tại hoặc đã hết hạn"
NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "Content-Security-Policy": "frame-ancestors 'none'"}

async def _q(*a, **k):   # lazy: unit tests do not need the whole app
    import deepagent_api as A
    return await A.q(*a, **k)

@contextlib.asynccontextmanager
async def _tx(ws):
    """One transaction inside the invite's workspace (row-level-security role + app.ws, like admin.as_workspace: the accept is anonymous, so the code must name the workspace it writes for)."""
    import deepagent_api as A
    async with A.S["pool"].connection() as c:
        await c.execute("SELECT set_config('app.ws',%s,false), set_config('app.uid','0',false), set_config('app.role','admin',false), set_config('role','aos_rls',false)", (str(int(ws)),))
        async with c.transaction():
            yield c

def _hash(token: str) -> str: return hashlib.sha256(token.encode()).hexdigest()
def _rank(role) -> int: return ROLES.get(role, 0)
def _ip(request): import admin; return admin._ip(request)
def _demo_ws(wid, row=None) -> bool:
    import gateway
    return bool((row or {}).get("demo")) or int(wid) in gateway.DEMO_WS

async def _log(uid, username, ws, request, status, detail):
    """Own audit insert: the public routes carry the token in their path and the audit middleware does not see them, so the path is always masked."""
    try:
        await _q("INSERT INTO audit_log(user_id,username,workspace_id,auth,method,path,status,ip,ms,detail) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,0,%s)",
                 (uid, username, ws, "invite", request.method, MASKED_PATH if request.method == "POST" else "/invite/*", status, _ip(request), detail))
    except Exception as e:  # noqa: BLE001
        print("audit error", type(e).__name__)

async def _limited(request) -> bool:
    r = await _q("SELECT count(*) FILTER (WHERE ip=%s) n_ip, count(*) n_all FROM audit_log WHERE detail=%s AND ts > now() - interval '1 minute'", (_ip(request), BAD_DETAIL), one=True)
    return r["n_ip"] >= BAD_PER_IP or r["n_all"] >= BAD_ALL

# ------------------------------------------------------------------ admin API
def _admin_ctx(wid: int):
    """403 unless the caller is an admin of the workspace they are working in, that workspace is `wid`, and it is not a demo workspace."""
    import admin
    u = admin.cur()
    if not u or u.get("demo") or admin.is_demo(u): raise HTTPException(403, "Tài khoản demo không mời người được")
    if _demo_ws(wid): raise HTTPException(403, "Workspace demo không dùng lời mời")
    if _rank(u.get("role")) < _rank(INVITER_MIN_ROLE): raise HTTPException(403, "Chỉ admin được mời người")
    if int(wid) != int(u.get("ws") or 0): raise HTTPException(403, "Hãy chuyển sang workspace này trước khi mời người")   # invites follow the workspace you are in (row-level security)
    return u

class InviteIn(BaseModel):
    role: Literal["admin", "editor", "viewer"] = "viewer"
    email_note: str | None = Field(default=None, max_length=200)
    emails: list[str] | None = Field(default=None, max_length=20)
    expires_days: int = Field(default=7, ge=1, le=30)
    max_uses: int = Field(default=1, ge=1, le=20)

def _email(x) -> str:
    x = (x or "").strip()
    if not EMAIL_RE.match(x): raise HTTPException(422, "Địa chỉ email không hợp lệ")
    return x

def _link(token: str) -> str:
    return os.environ.get("AOS_PUBLIC_URL", "").rstrip("/") + "/invite/" + token

async def _mail(ws_id, ws_name, inviter, addr, link) -> bool:
    """Optional: only when SMTP is configured and a public URL exists. A failure never fails the invite. notify.notify = one shot, no outbox row (the outbox would keep the link)."""
    if not (addr and os.environ.get("SMTP_HOST") and os.environ.get("AOS_PUBLIC_URL")): return False
    name = re.sub(r"[\r\n]+", " ", ws_name)[:80]
    try:
        r = await notify.notify(ws_id, {"kind": "email", "target": addr}, f"Lời mời vào workspace {name} / Invitation to workspace {name}",
                                f"{inviter} mời bạn vào workspace {name}. Mở liên kết để tạo tài khoản.\n{inviter} invited you to the workspace {name}. Open the link to create your account.", link)
        return bool(r.get("ok"))
    except Exception:  # noqa: BLE001
        return False

@router.post("/api/admin/workspaces/{wid}/invites")
async def invite_create(wid: int, b: InviteIn, response: Response):
    u = _admin_ctx(wid)
    ws = await _q("SELECT * FROM workspaces WHERE id=%s", (wid,), one=True)
    if not ws: raise HTTPException(404, "Không có workspace")
    if _demo_ws(wid, ws): raise HTTPException(403, "Workspace demo không dùng lời mời")
    if _rank(b.role) > _rank(u["role"]): raise HTTPException(403, "Không thể mời quyền cao hơn mình")   # role ceiling = inviter's role
    if (await _q("SELECT count(*) n FROM ws_invites WHERE workspace_id=%s AND revoked_at IS NULL AND expires_at > now() AND used_count < max_uses", (wid,), one=True))["n"] >= MAX_PENDING:
        raise HTTPException(409, "Quá nhiều lời mời đang mở, thu hồi bớt")
    bulk = b.emails is not None
    targets = list(dict.fromkeys(_email(x) for x in b.emails)) if bulk else [_email(b.email_note) if b.email_note else None]
    if bulk and not targets: raise HTTPException(422, "Danh sách email trống")
    out = []
    for addr in targets:
        token = secrets.token_urlsafe(32)
        r = await _q("INSERT INTO ws_invites(workspace_id,token_hash,role,email_note,created_by,expires_at,max_uses) VALUES(%s,%s,%s,%s,%s,now() + %s::int * interval '1 day',%s) RETURNING id, expires_at",
                     (wid, _hash(token), b.role, addr, u["id"], b.expires_days, 1 if bulk else b.max_uses), one=True)
        link = _link(token)
        out.append({"id": r["id"], "email": addr, "link": link, "role": b.role, "expires_at": r["expires_at"], "max_uses": 1 if bulk else b.max_uses,
                    "emailed": await _mail(wid, ws["name"], u["username"], addr, link)})
    response.headers["Cache-Control"] = "no-store"
    return {"invites": out} if bulk else {k: v for k, v in out[0].items() if k != "email"}

@router.get("/api/admin/workspaces/{wid}/invites")
async def invite_list(wid: int):
    _admin_ctx(wid)
    return await _q("""SELECT i.id, i.role, i.email_note, i.created_at, i.expires_at, i.used_count, i.max_uses, u.username created_by_name
        FROM ws_invites i LEFT JOIN users u ON u.id=i.created_by
        WHERE i.workspace_id=%s AND i.revoked_at IS NULL AND i.expires_at > now() AND i.used_count < i.max_uses ORDER BY i.id DESC""", (wid,))

@router.delete("/api/admin/workspaces/{wid}/invites/{iid}")
async def invite_revoke(wid: int, iid: int):
    _admin_ctx(wid)
    r = await _q("UPDATE ws_invites SET revoked_at=now() WHERE id=%s AND workspace_id=%s AND revoked_at IS NULL RETURNING id", (iid, wid), one=True)
    if not r: raise HTTPException(404, "Không có lời mời này")
    return {"ok": True}

# ------------------------------------------------------------------ public side
async def _inviter_still_ok(inv) -> bool:
    """The authority is re-checked at use time: the inviter must still exist, be enabled, not be a demo account and still be an admin of that workspace with a role >= the invite's."""
    u = await _q("SELECT id, disabled, demo, is_superadmin FROM users WHERE id=%s", (inv["created_by"],), one=True)
    if not u or u["disabled"] or u["demo"]: return False
    m = await _q("SELECT role FROM memberships WHERE user_id=%s AND workspace_id=%s", (u["id"], inv["workspace_id"]), one=True)
    role = m["role"] if m else ("admin" if u["is_superadmin"] else None)
    return bool(role) and _rank(role) >= _rank(INVITER_MIN_ROLE) and _rank(role) >= _rank(inv["role"])

async def _find(token) -> dict | None:
    """The valid invite (with `ws` = its workspace row) or None - one answer for every kind of 'no'."""
    if not isinstance(token, str) or not TOKEN_RE.match(token): return None
    h = _hash(token)
    inv = await _q("SELECT * FROM ws_invites WHERE token_hash=%s AND revoked_at IS NULL AND expires_at > now() AND used_count < max_uses", (h,), one=True)
    if not inv or not hmac.compare_digest(inv["token_hash"], h): return None
    ws = await _q("SELECT * FROM workspaces WHERE id=%s", (inv["workspace_id"],), one=True)
    if not ws or _demo_ws(inv["workspace_id"], ws) or not await _inviter_still_ok(inv): return None
    return {**inv, "ws": ws}

async def _session_user(request):
    import admin
    tok = request.cookies.get(admin.COOKIE)
    if not tok: return None
    return await _q("SELECT s.user_id id, u.username, u.demo FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=%s AND s.expires > now() AND NOT u.disabled", (admin._h(tok),), one=True)

PAGE = """<!doctype html><html lang=vi><head><meta charset=utf-8><meta name=viewport content="width=device-width"><meta name=referrer content=no-referrer><title>Lời mời vào workspace — Moza Agent OS</title>
<style>body{font-family:system-ui;background:#f3f4f6;display:grid;place-items:center;min-height:100vh;margin:0}form,.box{background:#fff;padding:28px;border-radius:12px;
box-shadow:0 2px 12px #0001;width:340px}input{width:100%;padding:9px;margin:6px 0 12px;border:1px solid #d1d5db;border-radius:6px;box-sizing:border-box}
button{width:100%;padding:10px;background:#2563eb;color:#fff;border:0;border-radius:6px;font-weight:600}#e{color:#b91c1c;font-size:13px;min-height:18px;margin-bottom:8px}.muted{color:#6b7280;font-size:13px}</style></head>
<body><form id=f><h2 style="margin-top:0">Moza Agent OS</h2><p>Bạn được mời tham gia workspace <b translate=no>@@WS@@</b> với vai trò <b translate=no>@@ROLE@@</b>.</p>
<div id=nu><label>Tên đăng nhập<input id=u autocomplete=username autocapitalize=none autocorrect=off spellcheck=false pattern="[a-zA-Z0-9_.\\-]{2,64}" required></label>
<label>Tên hiển thị (không bắt buộc)<input id=d maxlength=80 autocomplete=name></label>
<label>Mật khẩu (ít nhất 10 ký tự)<input id=p type=password minlength=10 maxlength=200 autocomplete=new-password required></label>
<label>Nhập lại mật khẩu<input id=p2 type=password minlength=10 maxlength=200 autocomplete=new-password required></label></div>
<div id=su hidden><p class=muted>Bạn đang đăng nhập là <b translate=no>@@WHO@@</b>. Bấm tiếp tục để tham gia bằng tài khoản này.</p></div>
<div id=e></div><button id=go>Tham gia</button></form>
<script>const SIGNED=@@SIGNED@@;if(SIGNED){nu.hidden=true;su.hidden=false;for(const x of [u,p,p2])x.required=false}
const tok=location.pathname.split('/').filter(Boolean).pop();
f.onsubmit=async ev=>{ev.preventDefault();e.textContent='';
if(!SIGNED&&p.value!==p2.value){e.textContent='Hai mật khẩu không khớp';return}
go.disabled=true;const body=SIGNED?{}:{username:u.value.trim(),display_name:d.value.trim()||null,password:p.value};
const r=await fetch('/api/invites/'+encodeURIComponent(tok)+'/accept',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
const j=await r.json().catch(()=>({}));
if(r.ok){location.href=j.must_enroll_2fa?'/settings?s=security':'/'}else{go.disabled=false;e.textContent=typeof j.detail==='string'?j.detail:'Lỗi'}}</script></body></html>"""
GONE_PAGE = """<!doctype html><html lang=vi><head><meta charset=utf-8><meta name=viewport content="width=device-width"><meta name=referrer content=no-referrer><title>Lời mời vào workspace — Moza Agent OS</title>
<style>body{font-family:system-ui;background:#f3f4f6;display:grid;place-items:center;min-height:100vh;margin:0}.box{background:#fff;padding:28px;border-radius:12px;box-shadow:0 2px 12px #0001;width:340px}</style></head>
<body><div class=box><h2 style="margin-top:0">Moza Agent OS</h2><p>@@MSG@@</p></div></body></html>"""

def _page(src: str, request, **vals) -> str:
    import i18n
    out = i18n.localize(i18n.inject(src), i18n.lang_of(request), cache=False)   # translate the page source first, THEN insert the (escaped) values: a workspace name is never translated or evaluated
    for k, v in vals.items(): out = out.replace(f"@@{k}@@", v)
    return out

@router.get("/invite/{token}", include_in_schema=False)
async def invite_page(token: str, request: Request):
    if await _limited(request): return HTMLResponse(_page(GONE_PAGE, request, MSG="Quá nhiều lần thử, đợi một phút rồi thử lại"), 429, {**NO_STORE, "Retry-After": "60"})
    inv = await _find(token)
    if not inv:
        await _log(None, None, None, request, 404, BAD_DETAIL)
        return HTMLResponse(_page(GONE_PAGE, request, MSG=GONE), 404, NO_STORE)
    me = await _session_user(request)
    signed = bool(me and not me["demo"])
    return HTMLResponse(_page(PAGE, request, WS=html.escape(inv["ws"]["name"]), ROLE=html.escape(inv["role"]), SIGNED="true" if signed else "false", WHO=html.escape(me["username"]) if signed else ""), 200, NO_STORE)

class AcceptIn(BaseModel):
    username: str | None = Field(default=None, max_length=64)
    display_name: str | None = Field(default=None, max_length=80)
    password: str | None = Field(default=None, max_length=200)

_RANK_SQL = "CASE {c} WHEN 'admin' THEN 3 WHEN 'editor' THEN 2 ELSE 1 END"

async def _consume(c, inv) -> bool:
    cur = await c.execute("UPDATE ws_invites SET used_count = used_count + 1 WHERE id=%s AND workspace_id=%s AND revoked_at IS NULL AND expires_at > now() AND used_count < max_uses RETURNING id",
                          (inv["id"], inv["workspace_id"]))
    return bool(await cur.fetchone())

@router.post("/api/invites/{token}/accept")
async def invite_accept(token: str, request: Request, b: AcceptIn | None = None):
    import admin
    b = b or AcceptIn()
    if await _limited(request): return JSONResponse({"detail": "Quá nhiều lần thử, đợi một phút rồi thử lại"}, 429, headers={"Retry-After": "60", **NO_STORE})
    inv = await _find(token)
    if not inv:
        await _log(None, None, None, request, 404, BAD_DETAIL)
        raise HTTPException(404, GONE)
    async with admin.as_workspace(inv["workspace_id"]):   # everything below writes for ONE workspace (the invite's)
        return await _accept(request, b, inv)

async def _accept(request, b, inv):
    import admin
    wid, role = inv["workspace_id"], inv["role"]
    me = await _session_user(request)
    if me:                                                    # already signed in: membership only (an existing role is never lowered)
        if me["demo"]: raise HTTPException(403, "Tài khoản demo không tham gia workspace khác")
        async with _tx(wid) as c:
            if not await _consume(c, inv): raise HTTPException(404, GONE)
            cur = await c.execute("INSERT INTO memberships(user_id,workspace_id,role) VALUES(%s,%s,%s) ON CONFLICT(user_id,workspace_id) DO UPDATE SET role = CASE WHEN "
                                  + _RANK_SQL.format(c="memberships.role") + " >= " + _RANK_SQL.format(c="EXCLUDED.role") + " THEN memberships.role ELSE EXCLUDED.role END RETURNING role", (me["id"], wid, role))
            final = (await cur.fetchone())["role"]
        tok = request.cookies.get(admin.COOKIE)
        await _q("UPDATE sessions SET workspace_id=%s WHERE token_hash=%s AND user_id=%s", (wid, admin._h(tok), me["id"]))   # land in the new workspace
        await _log(me["id"], me["username"], wid, request, 200, f"mời: tham gia với vai trò {final}")
        return JSONResponse({"ok": True, "workspace_id": wid, "workspace": inv["ws"]["name"], "role": final, "created": False}, headers=NO_STORE)
    name, pw = (b.username or "").strip(), b.password or ""   # a new account follows the admin user rules
    if not name or not pw: raise HTTPException(422, "Cần tên đăng nhập và mật khẩu")
    if not USERNAME_RE.match(name): raise HTTPException(422, "Tên đăng nhập: 2-64 ký tự chữ, số, _ . -")
    if not 10 <= len(pw) <= 200: raise HTTPException(422, "Mật khẩu cần ít nhất 10 ký tự")
    taken = HTTPException(409, "Tên đăng nhập đã có người dùng, hãy chọn tên khác")
    if await _q("SELECT 1 FROM users WHERE lower(username)=lower(%s)", (name,), one=True): raise taken
    pw_hash = await asyncio.to_thread(admin.hash_pw, pw)     # scrypt is slow: after the token check, before the transaction
    try:
        async with _tx(wid) as c:
            if not await _consume(c, inv): raise HTTPException(404, GONE)
            cur = await c.execute("INSERT INTO users(username,pw_hash,display_name) VALUES(%s,%s,%s) RETURNING id", (name, pw_hash, (b.display_name or "").strip() or None))
            uid = (await cur.fetchone())["id"]
            await c.execute("INSERT INTO memberships(user_id,workspace_id,role) VALUES(%s,%s,%s)", (uid, wid, role))
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ == "UniqueViolation": raise taken   # lost the race for the name: the transaction rolled back, the invite is not used up
        raise
    await _log(uid, name, wid, request, 200, f"mời: tạo tài khoản, vai trò {role}")
    masked = SimpleNamespace(method=request.method, url=SimpleNamespace(path=MASKED_PATH), headers=request.headers, client=request.client)   # issue_session audits the path: never the token
    r = await admin.issue_session({"id": uid, "username": name, "totp_enabled": False, "demo": False}, masked)
    import json
    out = JSONResponse({**json.loads(r.body), "workspace_id": wid, "workspace": inv["ws"]["name"], "role": role, "created": True}, headers=NO_STORE)
    out.raw_headers.extend(h for h in r.raw_headers if h[0] == b"set-cookie")
    return out
