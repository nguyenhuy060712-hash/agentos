"""GĐ8.3b Zalo PERSONAL-account channel (24/10/2026), kind "zalo_personal". UNOFFICIAL and EXPERIMENTAL: Zalo has no API for personal accounts; the
sidecar sidecars/zalo-personal (Node, zca-js 2.2.0, emulates Zalo Web) logs in with a QR code. Zalo may LOCK the number -> use a spare number only.
Facts + risks: docs/GD6-9_RESEARCH.md §GĐ8.3b. Never part of the customer build, off for the demo workspace and demo accounts.

Switch: env AOS_ZALO_PERSONAL=1 (anything else = off: the kind is hidden, creating it is refused with 422, no supervisor work, NO sidecar process).
Creating / enabling / editing / QR login = workspace admin only, and creating needs config.accept_risk=true (the UI shows the red warning + checkbox first).

Architecture (same adapter shape as channels.py 8.1; like Telegram: supervisor + advisory lock):
- This module starts ONE Node sidecar process (lazily, only when a login or a stored session needs it): 127.0.0.1 only, random port, random token
  (env ZP_TOKEN, per backend process), minimal environment (no DB / API keys), exits when this process dies (stdin pipe). One zca-js session per binding.
  Node: env AOS_ZP_NODE (path of node; Windows: tools/node-v24.22.0-win-x64/node.exe) else `node` in PATH; dir: AOS_ZP_DIR or ../sidecars/zalo-personal
  (run `npm install` there once; mock mode needs no packages). Port: AOS_ZP_PORT; backend URL the sidecar calls: AOS_ZP_BACKEND or http://127.0.0.1:$PORT.
- Sidecar -> backend: POST /internal/zalo-personal/{binding_id}/incoming (message) and /session (login blob). Allowed only from loopback (no proxy
  headers) with header x-zp-token; 404 when the feature is off. The login blob (cookie, imei, userAgent) is stored ONLY encrypted in the binding's
  ws secret (6.5, row lock, host chat.zalo.me); the sidecar keeps sessions in memory and gets the blob back (/session/start) after a restart.
- Backend -> sidecar: POST /send {thread_id, text, type} (ONE message to ONE thread; there is no broadcast / friend-request / bulk anywhere).
- Anti-lock limits per binding (config can only make them stricter): >= 2 s between sends (queued sends wait, config min_interval_s), <= 200 sends per
  ICT day counted in table zp_sends (config daily_cap); at the cap nothing is sent and the drop is logged. No auto-friend. Groups: only when mentioned.
  Optional allow-list = binding.allowed_users: sender ids/names, or a GROUP id (matches any member of that group).
- Approvals: text "duyệt <số>" / "từ chối <số>" exactly like Zalo OA (channel_zalo helpers). Replies are plain text split at 2,000 chars.
- Identity: channel runs are auth='channel' (channels.as_owner), so the owner's accounts/memories/instructions stay blocked (8.Z).
- Mock: ZALO_PERSONAL_MOCK=1 -> the sidecar uses a fake zca client (no Zalo network); admin-only /api/channels/{id}/zalo-personal/mock/* endpoints
  scan the fake QR, inject incoming messages and list what was "sent". overlay/run_local.py sets it."""
import asyncio, contextlib, hmac, json, os, secrets, shutil, socket, time, uuid, weakref
from pathlib import Path
import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
import channels as ch
import channel_zalo as z

KIND = "zalo_personal"
HOST = "chat.zalo.me"                # allowed host of the session secret
LOCK_NS = 8203                       # pg_try_advisory_lock(LOCK_NS, binding_id): one session per binding across processes
MIN_INTERVAL = 2.0                   # seconds between two sends of one binding (config can only raise it)
DAILY_CAP = 200                      # sends per ICT day (config can only lower it)
SUPERVISOR_S = float(os.environ.get("ZALO_PERSONAL_SUPERVISOR_S", "15"))
MAX_BODY = 1 << 20
DAY_START = "(date_trunc('day', now() AT TIME ZONE 'Asia/Ho_Chi_Minh') AT TIME ZONE 'Asia/Ho_Chi_Minh')"

router = APIRouter()
DDL = """
CREATE TABLE IF NOT EXISTS zp_sends(id BIGSERIAL PRIMARY KEY, workspace_id INT NOT NULL DEFAULT 1, binding_id INT NOT NULL REFERENCES channel_bindings(id) ON DELETE CASCADE,
  chat_id TEXT NOT NULL DEFAULT '', created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS zp_sends_binding_day ON zp_sends(binding_id, created_at);
"""

async def init(): await ch._q(DDL)

def enabled() -> bool: return os.environ.get("AOS_ZALO_PERSONAL") == "1"
def _mock() -> bool: return os.environ.get("ZALO_PERSONAL_MOCK") == "1"
def _log(bid, what): print(f"zalo_personal: binding {bid}: {what}", flush=True)

class ZpError(RuntimeError): pass

def limits(cfg) -> tuple:
    """(min seconds between sends, max sends per day): the config can only be stricter than the defaults."""
    cfg = cfg or {}
    try: iv = float(cfg.get("min_interval_s", MIN_INTERVAL))
    except (TypeError, ValueError): iv = MIN_INTERVAL
    try: cap = int(cfg.get("daily_cap", DAILY_CAP))
    except (TypeError, ValueError): cap = DAILY_CAP
    return max(MIN_INTERVAL, min(iv, 3600.0)), max(1, min(cap, DAILY_CAP))

def _clean_config(cfg: dict) -> dict:
    out = {}
    for k, lo, hi in (("min_interval_s", MIN_INTERVAL, 3600), ("daily_cap", 1, DAILY_CAP), ("timeout_s", 5, 900)):
        if k in cfg:
            try: out[k] = max(lo, min(hi, float(cfg[k]) if k == "min_interval_s" else int(cfg[k])))
            except (TypeError, ValueError): raise HTTPException(422, f"config.{k} phải là số") from None
    return out

# ------------------------------------------------------------------ sidecar process + client
SC: dict = {"proc": None, "port": 0, "token": ""}
_SC_LOCK = asyncio.Lock()

def _dir() -> Path: return Path(os.environ.get("AOS_ZP_DIR") or Path(__file__).resolve().parent.parent / "sidecars" / "zalo-personal")

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]

def _running() -> bool: return bool(SC["proc"] and SC["proc"].returncode is None)

async def _sc(method, path, payload=None, timeout=15.0):
    """Call the sidecar. Returns (http status, json). Never raises for HTTP errors; raises ZpError when it is not reachable."""
    if not SC["port"]: raise ZpError("sidecar not started")
    try:
        async with httpx.AsyncClient(timeout=timeout) as h:
            r = await h.request(method, f"http://127.0.0.1:{SC['port']}{path}", json=payload, headers={"x-zp-token": SC["token"]})
    except httpx.HTTPError as e: raise ZpError(f"sidecar unreachable: {type(e).__name__}") from None
    try: return r.status_code, r.json()
    except ValueError: return r.status_code, {}

async def ensure_sidecar():
    """Start the Node sidecar if it is not running (only ever called when AOS_ZALO_PERSONAL=1)."""
    if not enabled(): raise ZpError("AOS_ZALO_PERSONAL is not 1")
    async with _SC_LOCK:
        if _running(): return
        node, d = os.environ.get("AOS_ZP_NODE") or shutil.which("node"), _dir()
        if not node: raise ZpError("node not found (set AOS_ZP_NODE)")
        if not (d / "index.mjs").exists(): raise ZpError(f"sidecar not found in {d}")
        if not _mock() and not (d / "node_modules" / "zca-js").exists(): raise ZpError(f"run 'npm install' in {d}")
        port, token = int(os.environ.get("AOS_ZP_PORT") or 0) or _free_port(), secrets.token_urlsafe(32)
        env = {k: os.environ[k] for k in ("PATH", "HOME", "SYSTEMROOT", "TEMP", "TMP", "LANG") if k in os.environ}   # no DB url / API keys for the sidecar
        env.update(ZP_PORT=str(port), ZP_TOKEN=token, ZP_WATCH_STDIN="1", ZALO_PERSONAL_MOCK="1" if _mock() else "0",
                   ZP_BACKEND=os.environ.get("AOS_ZP_BACKEND") or f"http://127.0.0.1:{os.environ.get('PORT') or 8020}")
        proc = await asyncio.create_subprocess_exec(node, "index.mjs", cwd=str(d), env=env, stdin=asyncio.subprocess.PIPE)
        SC.update(proc=proc, port=port, token=token)
        for _ in range(60):
            if proc.returncode is not None: raise ZpError(f"sidecar exited ({proc.returncode})")
            with contextlib.suppress(ZpError):
                if (await _sc("GET", "/status", timeout=2))[0] == 200:
                    _log(0, f"sidecar started on 127.0.0.1:{port} ({'MOCK' if _mock() else 'REAL zca-js'})"); return
            await asyncio.sleep(0.25)
        proc.kill(); raise ZpError("sidecar did not answer")

async def stop_sidecar():
    p = SC["proc"]
    if p and p.returncode is None:
        with contextlib.suppress(ProcessLookupError): p.terminate()
        with contextlib.suppress(Exception): await asyncio.wait_for(p.wait(), 5)
        if p.returncode is None:
            with contextlib.suppress(ProcessLookupError): p.kill()
    SC.update(proc=None, port=0, token="")

# ------------------------------------------------------------------ session blob in ws secrets (encrypted, row lock)
async def _load_session(b) -> dict | None:
    import ws_secrets
    if not b.get("secret_ref"): return None
    with ch._system():
        raw = await ws_secrets.get_secret(b["secret_ref"], b["workspace_id"], HOST)
    try: s = json.loads(raw)
    except ValueError: return None
    return s if isinstance(s, dict) and s.get("cookie") and s.get("imei") and s.get("userAgent") else None

async def _store_session(b, blob: dict):
    import ws_secrets
    async with z._tx() as c:
        row = await (await c.execute("SELECT 1 FROM ws_secrets WHERE workspace_id=%s AND name=%s FOR UPDATE", (int(b["workspace_id"]), b["secret_ref"]))).fetchone()
        if not row: raise LookupError(f"Không có bí mật {b['secret_ref']}")
        await c.execute("UPDATE ws_secrets SET value_enc=%s, updated_at=now() WHERE workspace_id=%s AND name=%s",
                        (ws_secrets.seal_value(b["workspace_id"], b["secret_ref"], json.dumps({"cookie": blob["cookie"], "imei": blob["imei"], "userAgent": blob["userAgent"]})), int(b["workspace_id"]), b["secret_ref"]))

async def _clear_session(b):
    import ws_secrets
    async with z._tx() as c:
        await c.execute("UPDATE ws_secrets SET value_enc=%s, updated_at=now() WHERE workspace_id=%s AND name=%s", (ws_secrets.seal_value(b["workspace_id"], b["secret_ref"], "{}"), int(b["workspace_id"]), b["secret_ref"]))

# ------------------------------------------------------------------ sending with the anti-lock limits
_SEND_LOCKS: dict = {}               # binding_id -> asyncio.Lock (FIFO: queued sends wait their turn)
_LAST: dict = {}                     # binding_id -> time.monotonic() of the last send attempt

async def _sent_today(b) -> int:
    with ch._system():
        r = await ch._q(f"SELECT count(*) n FROM zp_sends WHERE binding_id=%s AND created_at >= {DAY_START}", (b["id"],), one=True)
    return int(r["n"])

async def send_part(b, chat_id: str, text: str) -> bool:
    """One message to one chat. Waits for the 2 s spacing; at the daily cap nothing is sent (logged). True = sent."""
    bid = b["id"]
    async with _SEND_LOCKS.setdefault(bid, asyncio.Lock()):
        interval, cap = limits(b.get("config"))
        n = await _sent_today(b)
        if n >= cap:
            _log(bid, f"daily cap reached ({n}/{cap}), message to {chat_id[:40]} NOT sent"); return False
        wait = _LAST.get(bid, 0.0) + interval - time.monotonic()
        if wait > 0: await asyncio.sleep(wait)
        grp = chat_id.startswith("grp:")
        _LAST[bid] = time.monotonic()
        code, d = await _sc("POST", "/send", {"binding_id": bid, "thread_id": chat_id[4:] if grp else chat_id, "text": text, "type": "group" if grp else "user"})
        _LAST[bid] = time.monotonic()
        if code != 200: raise ZpError(f"send failed: HTTP {code} {str(d.get('error') or '')[:120]}")
        with ch._system():
            await ch._q("INSERT INTO zp_sends(workspace_id,binding_id,chat_id) VALUES(%s,%s,%s)", (b["workspace_id"], bid, chat_id[:120]))
        return True

# ------------------------------------------------------------------ adapter
class ZaloPersonalAdapter(ch.Adapter):
    kind = KIND
    needs_secret = False

    def available(self) -> bool: return enabled()

    async def prepare_create(self, u, b):
        if not enabled(): raise HTTPException(422, "Kênh Zalo cá nhân đang tắt trên máy chủ này (AOS_ZALO_PERSONAL)")
        import admin
        if not u or u.get("role") != "admin" or admin.is_demo(u): raise HTTPException(403, "Chỉ admin được tạo kênh Zalo cá nhân")
        cfg = dict(b.config)
        if cfg.pop("accept_risk", None) is not True: raise HTTPException(422, "Cần xác nhận rủi ro khoá số (config.accept_risk=true)")
        cfg = _clean_config(cfg); cfg.update(risk_ack_by=u["username"], risk_ack_at=int(time.time()))
        name = "ZALOP_" + uuid.uuid4().hex[:10]
        import ws_secrets
        await ch._q("INSERT INTO ws_secrets(workspace_id,name,value_enc,allowed_hosts,created_by) VALUES(%s,%s,%s,%s,%s)",
                    (admin.cur_ws(), name, ws_secrets.seal_value(admin.cur_ws(), name, "{}"), [HOST], u["username"]))
        return {"secret_ref": name, "config": cfg}

    async def guard_patch(self, u, row, p):
        if not u or u.get("role") != "admin": raise HTTPException(403, "Chỉ admin được sửa kênh Zalo cá nhân")
        if p.status == "active" and not enabled(): raise HTTPException(422, "Kênh Zalo cá nhân đang tắt trên máy chủ này (AOS_ZALO_PERSONAL)")
        if "secret_ref" in p.model_fields_set: raise HTTPException(422, "Bí mật của kênh Zalo cá nhân do hệ thống tự quản lý")
        if p.config is not None:   # numbers checked; the risk acknowledgement stays
            keep = {k: v for k, v in (row.get("config") or {}).items() if k in ("risk_ack_by", "risk_ack_at")}
            p.config = {**_clean_config(dict(p.config)), **keep}

    async def after_delete(self, row):
        if row.get("secret_ref"):
            await ch._q("DELETE FROM ws_secrets WHERE workspace_id=%s AND name=%s", (row["workspace_id"], row["secret_ref"]))
        if _running():
            with contextlib.suppress(ZpError): await _sc("POST", "/session/stop", {"binding_id": row["id"]})
        await _unlock(row["id"])

    def parse_incoming(self, raw):
        """raw = the sidecar's event {msg_id, thread_id, sender_id, sender_name, text, files, is_group, mentioned}. Group chat ids are "grp:<id>"; in a
        group user_name carries the GROUP id so an allow-list entry for the group admits its members (channels.user_allowed matches id or name)."""
        if not isinstance(raw, dict): return None
        tid, uid = str(raw.get("thread_id") or ""), str(raw.get("sender_id") or "")
        if not tid or not uid: return None
        files = [{"kind": f["kind"], "url": str(f["url"]), "name": f.get("name"), "size": f.get("size")} for f in (raw.get("files") or [])
                 if isinstance(f, dict) and f.get("url") and f.get("kind") in ("image", "file")][:10]
        text = str(raw.get("text") or "")[:8000]
        if not text.strip() and not files: return None
        grp = bool(raw.get("is_group"))
        return ch.Incoming(chat_id=("grp:" + tid) if grp else tid, user_id=uid, user_name=tid if grp else str(raw.get("sender_name") or "")[:120], text=text,
                           files=files, is_group=grp, mentioned=bool(raw.get("mentioned")))

    async def fetch_files(self, binding, msg):
        return await z.ADAPTER.fetch_files(binding, msg)   # same download + 6.8 store path as Zalo OA (https only, no private IPs; loopback in mock)

    async def send_text(self, binding, chat_id, text):
        for part in z.split_text(z.md_to_plain(text)) or [ch.EMPTY_TEXT]:
            if not await send_part(binding, chat_id, part): return   # cap reached: the rest is dropped too

    async def send_approval(self, binding, chat_id, approval_id, summary):
        await self.send_text(binding, chat_id, f"{summary}\n\n{z.APPROVE_HINT.format(id=int(approval_id))}")

ADAPTER = ZaloPersonalAdapter()
ch.register(ADAPTER)

# ------------------------------------------------------------------ incoming (called by the sidecar through /internal)
_TASKS: set = set()
_CHAT_LOCKS: "weakref.WeakValueDictionary" = weakref.WeakValueDictionary()

def _chat_lock(key):
    lk = _CHAT_LOCKS.get(key)
    if lk is None: lk = _CHAT_LOCKS[key] = asyncio.Lock()
    return lk

async def process(b, ev):
    try:
        msg = ADAPTER.parse_incoming(ev)
        if not msg: return {"status": "ignored", "reason": "event"}
        why = ch._gate(b, msg.user_id, msg.user_name)
        if not why and msg.is_group and not msg.mentioned: why = "group message without mention"
        if why:
            _log(b["id"], "ignored: " + why); return {"status": "ignored", "reason": why}
        if ev.get("msg_id") and not await z._first_time(b, f"zp:{ev['msg_id']}"[:200]):
            _log(b["id"], "duplicate message ignored"); return {"status": "ignored", "reason": "duplicate"}
        async with _chat_lock((b["id"], ch.chat_key(msg))):
            d = None if msg.files else z.parse_decision(msg.text)
            if d: return await ch.handle_decision(b["id"], msg.chat_id, msg.user_id, d[0], d[1], ADAPTER, user_name=msg.user_name)
            return await ch.handle_incoming(b["id"], msg, ADAPTER)
    except Exception as e:  # noqa: BLE001
        _log(b["id"], f"message failed: {type(e).__name__}: {str(e)[:200]}"); return {"status": "error"}

def _internal(request: Request):
    """Sidecar callbacks: feature on, direct loopback (no proxy headers), shared token. Anything else looks like a missing route or is refused."""
    import admin
    if not enabled(): raise HTTPException(404, "Not found")
    if not admin._loopback_direct(request): raise HTTPException(403, "loopback only")
    tok = (request.headers.get("x-zp-token") or "").encode()
    if not SC["token"] or not hmac.compare_digest(tok, SC["token"].encode()): raise HTTPException(401, "bad token")

async def _json(request):
    body = await request.body()
    if len(body) > MAX_BODY: raise HTTPException(413, "too large")
    try: d = json.loads(body)
    except ValueError: raise HTTPException(400, "bad json") from None
    if not isinstance(d, dict): raise HTTPException(400, "bad json")
    return d

async def _bound(bid):
    b = await ch._binding(bid)
    if not b or b["kind"] != KIND: raise HTTPException(404, "Not found")
    return b

@router.post("/internal/zalo-personal/{binding_id}/incoming")
async def incoming(binding_id: int, request: Request):
    _internal(request); ev = await _json(request); b = await _bound(binding_id)
    if b["status"] != "active" or ch._off(b["workspace_id"]): _log(binding_id, "ignored: binding disabled or demo workspace"); return {"ok": True}
    t = asyncio.create_task(process(b, ev)); _TASKS.add(t); t.add_done_callback(_TASKS.discard)
    return {"ok": True}

@router.post("/internal/zalo-personal/{binding_id}/session")
async def session(binding_id: int, request: Request):
    _internal(request); d = await _json(request); b = await _bound(binding_id)
    if not (d.get("cookie") and d.get("imei") and d.get("userAgent")): raise HTTPException(422, "session needs cookie, imei, userAgent")
    await _store_session(b, d); _log(binding_id, "login session stored (encrypted)")
    return {"ok": True}

# ------------------------------------------------------------------ admin API (QR login, status, mock helpers)
class LoginIn(BaseModel):
    confirm: bool = False

def _user_ok():
    import admin
    u = admin.cur()
    return bool(u and u.get("role") == "admin" and not admin.is_demo(u) and not ch._off(admin.cur_ws()))

@router.get("/api/channels/zalo-personal/capabilities")
async def capabilities():
    """The UI shows the 'Zalo cá nhân' kind only when this says enabled (feature on, admin, not demo)."""
    return {"enabled": enabled() and _user_ok(), "mock": _mock() and enabled()}

async def _admin_binding(bid):
    if not enabled(): raise HTTPException(404, "Not found")
    if not _user_ok(): raise HTTPException(403, "Chỉ admin (không phải demo) được dùng kênh Zalo cá nhân")
    b = await ch._get(bid)
    if b["kind"] != KIND: raise HTTPException(404, "Không có kênh Zalo cá nhân này")
    return b

@router.post("/api/channels/{bid}/zalo-personal/login")
async def login(bid: int, body: LoginIn):
    """Start the QR login: only after the explicit confirmation (the warning checkbox). Returns {status, qr (data URL)}."""
    b = await _admin_binding(bid)
    if body.confirm is not True: raise HTTPException(422, "Cần xác nhận rủi ro trước khi hiện mã QR")
    if b["status"] != "active": raise HTTPException(409, "Hãy bật kênh trước khi đăng nhập")
    if not await _lock(bid): raise HTTPException(409, "Kênh đang được tiến trình khác giữ phiên")
    try:
        await ensure_sidecar()
        code, d = await _sc("POST", "/login/qr", {"binding_id": bid}, timeout=40)
    except ZpError as e: raise HTTPException(503, f"Không chạy được sidecar Zalo: {str(e)[:160]}") from None
    if code != 200: raise HTTPException(502, "Sidecar Zalo báo lỗi")
    _log(bid, "QR login started")
    return d

@router.get("/api/channels/{bid}/zalo-personal/status")
async def status(bid: int):
    b = await _admin_binding(bid)
    out = {"status": "stopped", "qr": None, "name": "", "error": "", "has_session": False}
    with contextlib.suppress(Exception): out["has_session"] = bool(await _load_session(b))
    if _running():
        with contextlib.suppress(ZpError):
            code, d = await _sc("GET", f"/status?binding_id={int(bid)}", timeout=5)
            if code == 200: out.update({k: d.get(k) for k in ("status", "qr", "name", "error")})
    return out

@router.post("/api/channels/{bid}/zalo-personal/logout")
async def logout(bid: int):
    """Drop the live session and forget the stored login blob."""
    b = await _admin_binding(bid)
    if _running():
        with contextlib.suppress(ZpError): await _sc("POST", "/session/stop", {"binding_id": bid})
    await _clear_session(b); await _unlock(bid)
    return {"ok": True}

async def _sc_or_503(*a, **k):
    try: return await _sc(*a, **k)
    except ZpError as e: raise HTTPException(503, f"Sidecar Zalo chưa chạy: {str(e)[:120]}") from None

def _need_mock():
    if not (enabled() and _mock()): raise HTTPException(404, "Not found")

@router.post("/api/channels/{bid}/zalo-personal/mock/scan")
async def mock_scan(bid: int):
    _need_mock(); await _admin_binding(bid)
    return (await _sc_or_503("POST", "/mock/scan", {"binding_id": bid}))[1]

class MockIn(BaseModel):
    message: dict

@router.post("/api/channels/{bid}/zalo-personal/mock/incoming")
async def mock_incoming(bid: int, m: MockIn):
    """Deliver a fake incoming message through the real sidecar path (sidecar -> /internal/.../incoming -> agent)."""
    _need_mock(); await _admin_binding(bid)
    code, d = await _sc_or_503("POST", "/mock/incoming", {"binding_id": bid, "message": m.message})
    if code != 200: raise HTTPException(409, str(d.get("error") or "sidecar refused"))
    return d

@router.get("/api/channels/{bid}/zalo-personal/mock/sent")
async def mock_sent(bid: int):
    _need_mock(); await _admin_binding(bid)
    return (await _sc_or_503("GET", "/mock/sent"))[1]

# ------------------------------------------------------------------ advisory locks + supervisor
_LOCKS: dict = {}                    # binding_id -> psycopg connection holding the advisory lock

async def _lock(bid) -> bool:
    if bid in _LOCKS: return True
    import psycopg
    conn = await psycopg.AsyncConnection.connect(os.environ["PG_URI"], autocommit=True)
    ok = bool((await (await conn.execute("SELECT pg_try_advisory_lock(%s::int,%s::int)", (LOCK_NS, int(bid)))).fetchone())[0])
    if ok: _LOCKS[bid] = conn
    else: await conn.close()
    return ok

async def _unlock(bid):
    conn = _LOCKS.pop(bid, None)
    if conn:
        with contextlib.suppress(Exception): await conn.close()

async def _tick():
    if not enabled():
        if _running(): await stop_sidecar()
        return
    with ch._system():
        rows = await ch._q("SELECT * FROM channel_bindings WHERE kind=%s AND status='active'", (KIND,))
    want = {r["id"]: r for r in rows or [] if not ch._off(r["workspace_id"])}
    stored = {}
    for bid, b in want.items():
        with contextlib.suppress(Exception): stored[bid] = await _load_session(b)
    if any(stored.values()) and not _running():
        try: await ensure_sidecar()
        except ZpError as e: _log(0, f"cannot start sidecar: {e}"); return
    if not _running():
        for bid in list(_LOCKS): await _unlock(bid)
        return
    try: code, d = await _sc("GET", "/status", timeout=5)
    except ZpError as e: _log(0, f"sidecar not answering: {e}"); return
    live = {int(k) for k in (d.get("sessions") or {})}
    for bid, sess in stored.items():
        if sess and bid not in live and await _lock(bid):
            c, r = await _sc("POST", "/session/start", {"binding_id": bid, "session": sess}, timeout=40)
            _log(bid, f"session start from stored login: {r.get('status') or c}")
    for bid in live - want.keys():
        await _sc("POST", "/session/stop", {"binding_id": bid}); await _unlock(bid); _log(bid, "session stopped (binding disabled or deleted)")

async def supervisor():
    try:
        while True:
            try: await _tick()
            except asyncio.CancelledError: raise
            except Exception as e:  # noqa: BLE001
                _log(0, f"supervisor tick failed: {type(e).__name__}: {str(e)[:160]}")
            await asyncio.sleep(SUPERVISOR_S)
    finally:
        await asyncio.shield(stop_sidecar())
        for bid in list(_LOCKS): await _unlock(bid)

def start() -> asyncio.Task:
    """From the app lifespan, API process only (it serves the sidecar callbacks). Off -> nothing runs."""
    if not enabled() or os.environ.get("AOS_ROLE") == "worker": return asyncio.create_task(asyncio.sleep(0))
    return asyncio.create_task(supervisor())
