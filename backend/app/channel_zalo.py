"""GĐ8.3 Zalo OA adapter (23/10/2026), webhook based. Facts from the official docs (read 23/10, docs/GD6-9_RESEARCH.md §GĐ8 update).

- Webhook POST /channels/zalo/{binding_id} (public path, admin.PUBLIC_PREFIX): verify X-ZEvent-Signature = sha256(app_id + raw body + timestamp +
  OA secret key) hex ("mac=<hex>" or bare hex accepted: exact header text unverified), constant-time compare, 401 if bad. Answers 200 at once
  (Zalo wants < 2 s) and processes in a background task. Retries (header num_retry) are deduped by message.msg_id (table channel_events).
- Events: user_send_text / user_send_image / user_send_file. Attachments are downloaded from the event URLs and stored through the 6.8 path
  (attachments.store), like Telegram files. Other events are ignored.
- Binding secret (ws secret, allowed hosts openapi.zalo.me + oauth.zaloapp.com) = JSON {app_id, oa_secret_key, app_secret_key, access_token,
  refresh_token, access_expires_at (epoch s)}. Access token refreshed via OAuth v4 (grant_type=refresh_token, header secret_key) when expired or
  rejected; the refresh token is single-use, so the new pair is written back (encrypted, row lock FOR UPDATE) BEFORE it is used. Never logged.
- Replies: POST /v3.0/oa/message/cs {recipient:{user_id}, message:{text}}, plain text, split at 2,000 chars. Send window from the user's last
  message (channel_threads.last_user_at): <= 48 h send; 48 h-7 d only if binding config allow_after_48h (default off, uses OA quota); > 7 d never.
  Blocked sends are logged, not sent.
- Images / files (9.P2): the reply text may reference files the agent produced (/api/files/<id>); channels._deliver hands them to send_media(). Image:
  POST /v2.0/oa/upload/image (multipart "file", JPG/PNG <= 1 MB; bigger / other formats are re-encoded with Pillow) -> data.attachment_id, then cs
  {message:{attachment:{type:"template", payload:{template_type:"media", elements:[{media_type:"image", attachment_id}]}}}}. File: POST
  /v2.0/oa/upload/file (multipart, PDF/DOC/DOCX <= 5 MB) -> data.token, then cs {message:{attachment:{type:"file", payload:{token}}}}. Other types /
  sizes -> a short text note instead. Endpoint paths + payload shapes: see CHO_ANH "Zalo hinh/tep" (not re-verified against the live docs).
- Webhook freshness (9.P2): an event whose `timestamp` is older than FRESH_S (2 h = Zalo retry horizon 1 h + margin) or more than 5 min in the future is
  acknowledged (200) but not processed (binding config max_event_age_s overrides, 60 s .. 30 d); inside the window duplicates are still dropped by msg_id.
- Approvals: no buttons for consultation text in our scope -> text "Trả lời 'duyệt <id>' hoặc 'từ chối <id>'"; such replies -> handle_decision.
- ZNS (template messages) and GMF group chat: not built.
- Mock: ZALO_MOCK=1 -> ZALO_API_BASE / ZALO_OAUTH_BASE must be loopback (default http://127.0.0.1:8782), file URLs may be loopback http.
  overlay/run_local.py sets it, so the local stack never talks to Zalo."""
import asyncio, contextlib, hashlib, hmac, json, os, re, time, unicodedata, weakref
from urllib.parse import urlparse
import httpx
from fastapi import APIRouter, HTTPException, Request
import channels as ch
import netguard

KIND = "zalo"
HOST = "openapi.zalo.me"
OAUTH_HOST = "oauth.zaloapp.com"
MOCK_BASE = "http://127.0.0.1:8782"
LIMIT = 2000
WINDOW_FREE = 48 * 3600
WINDOW_MAX = 7 * 24 * 3600
REFRESH_EARLY = 300                 # refresh when the access token expires within 5 min
DEFAULT_TTL = 25 * 3600             # access token lifetime when Zalo does not say (docs: 25 h)
MAX_BODY = 1 << 20
EVENTS = ("user_send_text", "user_send_image", "user_send_file")
TOKEN_ERRORS = {-216, -124}         # access token invalid / expired (codes unverified -> HTTP 401 also counts)
DECIDE_RE = re.compile(r"^\s*['\"“]?\s*(duyệt|duyet|từ\s*chối|tu\s*choi)\s*#?\s*(\d{1,18})\s*['\"”]?\s*[.!]?\s*$", re.I)
APPROVE_HINT = "Trả lời 'duyệt {id}' hoặc 'từ chối {id}'."
FILE_TOO_BIG = "Tệp {} quá lớn (tối đa 20 MB)."
FILE_FAIL = "không tải được tệp {}"
FRESH_S = int(os.environ.get("ZALO_FRESH_S", "7200"))   # webhook events older than this are not processed (retries stop after ~1 h)
FUTURE_S = 300
IMG_MAX = 1_000_000                  # upload/image limit "1 MB" (decimal, to be safe)
FILE_MAX = 5_000_000                 # upload/file limit "5 MB"
FILE_EXTS = {"pdf", "doc", "docx"}   # types the OA file message accepts
UPLOAD_IMAGE = "/v2.0/oa/upload/image"
UPLOAD_FILE = "/v2.0/oa/upload/file"
CS_PATH = "/v3.0/oa/message/cs"
NO_MEDIA = "(Không gửi được {} qua Zalo: {}.)"

router = APIRouter()
DDL = """
ALTER TABLE channel_threads ADD COLUMN IF NOT EXISTS last_user_at TIMESTAMPTZ;
CREATE TABLE IF NOT EXISTS channel_events(workspace_id INT NOT NULL DEFAULT 1, binding_id INT NOT NULL REFERENCES channel_bindings(id) ON DELETE CASCADE,
  event_key TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(binding_id, event_key));
"""

async def init(): await ch._q(DDL)

def _log(bid, what): print(f"zalo: binding {bid}: {what}", flush=True)

class ZaloError(RuntimeError): pass
class _Skip(Exception): pass   # media that cannot be sent (type / size): message = reason shown to the user

def fit_image(data: bytes, limit: int = IMG_MAX):
    """Bytes of a JPG/PNG <= limit for upload/image: as is if it already fits and is JPG/PNG, else re-encoded (Pillow: RGB JPEG, quality then size stepped
    down). None when it is not an image or cannot be made small enough."""
    import io
    from PIL import Image
    try: im = Image.open(io.BytesIO(data)); im.load()
    except Exception: return None  # noqa: BLE001
    if im.format in ("JPEG", "PNG") and len(data) <= limit: return data
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA"); bg = Image.new("RGB", im.size, "white"); bg.paste(im, mask=im.split()[-1]); im = bg
    else: im = im.convert("RGB")
    for scale in (1.0, 0.75, 0.5, 0.35, 0.25):
        w = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale)))) if scale < 1 else im
        for q in (88, 75, 60, 45):
            buf = io.BytesIO(); w.save(buf, "JPEG", quality=q, optimize=True)
            if buf.tell() <= limit: return buf.getvalue()
    return None

# ------------------------------------------------------------------ pure helpers
def _loopback(url) -> bool: return urlparse(url).hostname in ("127.0.0.1", "localhost", "::1")
def _mock() -> bool: return os.environ.get("ZALO_MOCK") == "1"

def _base(env, real) -> str:
    base = (os.environ.get(env) or "").strip().rstrip("/")
    if _mock():
        base = base or MOCK_BASE
        if not _loopback(base): raise RuntimeError(f"ZALO_MOCK=1 needs a loopback {env}")
    return base or real

def api_base() -> str: return _base("ZALO_API_BASE", "https://openapi.zalo.me")
def oauth_base() -> str: return _base("ZALO_OAUTH_BASE", "https://oauth.zaloapp.com")

def sign(app_id, body: bytes, timestamp, oa_secret) -> str:
    return hashlib.sha256(str(app_id).encode() + body + str(timestamp).encode() + str(oa_secret).encode()).hexdigest()

def verify(app_id, body: bytes, timestamp, oa_secret, header) -> bool:
    """X-ZEvent-Signature: 'mac=<hex>' or bare hex (case-insensitive), compared in constant time."""
    if not header or timestamp in (None, "") or not app_id or not oa_secret: return False
    got = str(header).strip()
    if got[:4].lower() == "mac=": got = got[4:].strip()
    return hmac.compare_digest(got.lower().encode(), sign(app_id, body, timestamp, oa_secret).encode())

def window(cfg, last_at, now=None) -> str | None:
    """None = may send; else why not. last_at = epoch seconds of the user's last message."""
    if last_at is None: return "no message from this user recorded"
    age = (now or time.time()) - float(last_at)
    if age > WINDOW_MAX: return "last user message is older than 7 days (Zalo does not allow sending)"
    if age > WINDOW_FREE and not (cfg or {}).get("allow_after_48h"): return "outside the 48 h window (config allow_after_48h is off)"
    return None

def md_to_plain(md: str) -> str:
    """Zalo text is plain: drop Markdown markers, keep the words (links as 'text (url)')."""
    s = re.sub(r"(?m)^[ \t]*```[^\n]*\n?", "", md or "")
    s = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s()]+)\)", r"\1 (\2)", s)
    s = re.sub(r"(?m)^[ \t]*#{1,6}[ \t]+(.+?)[ \t#]*$", r"\1", s)
    s = re.sub(r"(?m)^([ \t]*)[-*+][ \t]+", "\\1• ", s)
    s = re.sub(r"\*\*(?=\S)([^\n]+?)(?<=\S)\*\*", r"\1", s)
    s = re.sub(r"(?<![\w])__(?=\S)([^\n]+?)(?<=\S)__(?![\w])", r"\1", s)
    s = re.sub(r"`([^`\n]+)`", r"\1", s)
    return s.strip()

def split_text(text: str, limit: int = LIMIT) -> list:
    """Parts of <= limit chars, cut at a blank line, line break or space when possible. No text lost except the cut whitespace."""
    out, s = [], (text or "").strip()
    while len(s) > limit:
        cut = max(s.rfind("\n\n", 0, limit + 1), -1)
        if cut < limit // 2: cut = s.rfind("\n", 0, limit + 1)
        if cut < limit // 2: cut = s.rfind(" ", 0, limit + 1)
        if cut < limit // 2: cut = limit
        out.append(s[:cut].rstrip()); s = s[cut:].lstrip()
    if s: out.append(s)
    return out

def parse_decision(text: str):
    """'duyệt 12' / 'từ chối #12' -> (12, True|False); else None."""
    m = DECIDE_RE.match(unicodedata.normalize("NFC", text or ""))
    if not m: return None
    return int(m[2]), m[1].lower().startswith("duy")

def _secret_json(raw: str) -> dict:
    try: s = json.loads(raw)
    except ValueError: raise ValueError("Zalo secret is not JSON") from None
    if not isinstance(s, dict) or not s.get("app_id") or not s.get("oa_secret_key"): raise ValueError("Zalo secret JSON needs app_id and oa_secret_key")
    return s

def fresh_limit(cfg) -> int:
    """Seconds an event may be old: binding config max_event_age_s (60 s .. 30 d) or FRESH_S."""
    try: return max(60, min(30 * 86400, int((cfg or {}).get("max_event_age_s") or FRESH_S)))
    except (TypeError, ValueError): return FRESH_S

def stale_reason(ts_ms, now=None, limit=None) -> str | None:
    """None = fresh enough to process. The timestamp is covered by the signature, so an old event is a replay (or a very late retry): not processed."""
    try: t = float(ts_ms) / 1000
    except (TypeError, ValueError): return "timestamp missing or not a number"
    age = (now or time.time()) - t
    limit = limit or FRESH_S
    if age > limit: return f"older than {limit} s ({int(age)} s)"
    if age < -FUTURE_S: return f"{int(-age)} s in the future"
    return None

def _exp(s) -> float:
    try: return float(s.get("access_expires_at") or 0)
    except (TypeError, ValueError): return 0.0

def _ts(ev) -> float:
    """Event time (ms in the payload) -> epoch seconds, never in the future."""
    try: t = float(ev.get("timestamp")) / 1000
    except (TypeError, ValueError): t = time.time()
    return min(t, time.time())

# ------------------------------------------------------------------ tokens (secret JSON in ws_secrets, rotated atomically)
_TOK_LOCKS: dict = {}

async def _secret(b) -> dict:
    import ws_secrets
    if not b.get("secret_ref"): raise LookupError("binding has no secret_ref")
    with ch._system():
        return _secret_json(await ws_secrets.get_secret(b["secret_ref"], b["workspace_id"], HOST))

@contextlib.asynccontextmanager
async def _tx():
    """One transaction on a pool connection, as a system task (rows are filtered by workspace_id explicitly)."""
    import deepagent_api as A
    async with A.S["pool"].connection() as c:
        await c.execute("SELECT set_config('app.ws','*',false), set_config('app.uid','0',false), set_config('app.role','admin',false), set_config('role','none',false)")
        async with c.transaction(): yield c

async def _refresh(b, stale: str | None) -> str:
    """New access token via OAuth v4 refresh. Serialised per binding (process lock + row lock); if another caller already rotated the pair,
    its fresh token is used. The new pair is committed before this returns (single-use refresh token)."""
    import crypto, ws_secrets
    lk = _TOK_LOCKS.setdefault(b["id"], asyncio.Lock())
    async with lk, _tx() as c:
        row = await (await c.execute("SELECT value_enc, allowed_hosts FROM ws_secrets WHERE workspace_id=%s AND name=%s FOR UPDATE",
                                     (int(b["workspace_id"]), b["secret_ref"]))).fetchone()
        if not row: raise LookupError(f"Không có bí mật {b['secret_ref']}")
        if not ws_secrets.host_allowed(OAUTH_HOST, row["allowed_hosts"]): raise PermissionError(f"Bí mật {b['secret_ref']} không được phép gửi tới máy {OAUTH_HOST}")
        s = _secret_json(ws_secrets.open_value(row["value_enc"], b["workspace_id"], b["secret_ref"]))
        if s.get("access_token") and s["access_token"] != stale and _exp(s) > time.time() + REFRESH_EARLY: return s["access_token"]
        if not s.get("refresh_token") or not s.get("app_secret_key"): raise LookupError("Zalo secret needs refresh_token and app_secret_key to get an access token")
        async with netguard.client("webhook", timeout=15) as h:   # GĐ10.2: Zalo hosts are public https; the local mock needs AOS_EGRESS_ALLOW
            r = await h.post(oauth_base() + "/v4/oa/access_token", headers={"secret_key": str(s["app_secret_key"])},
                             data={"refresh_token": s["refresh_token"], "app_id": str(s["app_id"]), "grant_type": "refresh_token"})
        try: d = r.json()
        except ValueError: d = {}
        if not d.get("access_token") or not d.get("refresh_token"):
            raise ZaloError(f"token refresh failed: HTTP {r.status_code} error {d.get('error')} {str(d.get('error_name') or d.get('message') or '')[:120]}")
        try: ttl = int(d.get("expires_in") or DEFAULT_TTL)
        except (TypeError, ValueError): ttl = DEFAULT_TTL
        s.update(access_token=d["access_token"], refresh_token=d["refresh_token"], access_expires_at=int(time.time()) + ttl)
        await c.execute("UPDATE ws_secrets SET value_enc=%s, updated_at=now() WHERE workspace_id=%s AND name=%s",
                        (ws_secrets.seal_value(b["workspace_id"], b["secret_ref"], json.dumps(s)), int(b["workspace_id"]), b["secret_ref"]))
    _log(b["id"], "access token refreshed (refresh token rotated and stored)")
    return s["access_token"]

async def access_token(b) -> str:
    s = await _secret(b)
    if s.get("access_token") and _exp(s) > time.time() + REFRESH_EARLY: return s["access_token"]
    return await _refresh(b, s.get("access_token"))

async def _post_cs(b, payload) -> dict:
    """POST /v3.0/oa/message/cs; a rejected token -> one refresh + retry."""
    return await _post_api(b, CS_PATH, json=payload)

async def _post_api(b, path, *, json=None, files=None) -> dict:
    """POST to the OA API with the access token; a rejected token -> one refresh + retry. json= or files= (multipart)."""
    tok = await access_token(b)
    for attempt in (0, 1):
        async with netguard.client("webhook", timeout=30 if files else 15) as h:
            r = await h.post(api_base() + path, headers={"access_token": tok}, **({"json": json} if files is None else {"files": files}))
        try: d = r.json()
        except ValueError: d = {}
        if r.status_code == 401 or d.get("error") in TOKEN_ERRORS:
            if attempt: break
            tok = await _refresh(b, tok); continue
        if r.status_code >= 400 or d.get("error") not in (0, None):
            raise ZaloError(f"send failed: HTTP {r.status_code} error {d.get('error')} {str(d.get('message') or '')[:120]}")
        return d
    raise ZaloError("send failed: access token rejected after refresh")

# ------------------------------------------------------------------ file download (URLs from the signed event)
async def _download(url, name, cap) -> bytes:
    """Event file URLs are attacker-influenced: netguard (https only, <= 5 redirects, every hop re-checked, connects to the checked IP). The local mock needs AOS_EGRESS_ALLOW."""
    from fastapi import HTTPException as _HE
    try:
        async with netguard.client("fetch", https_only=True, timeout=30) as h:
            async with h.stream("GET", url) as r:
                if r.status_code != 200: raise _HE(422, FILE_FAIL.format(name))
                buf = bytearray()
                async for chunk in r.aiter_bytes():
                    buf += chunk
                    if len(buf) > cap: raise _HE(413, FILE_TOO_BIG.format(name))
                return bytes(buf)
    except httpx.HTTPError: raise _HE(422, FILE_FAIL.format(name)) from None   # includes netguard.NetBlocked (logged there)

def _name(f) -> str:
    if f.get("name"): return str(f["name"])[:200]
    base = os.path.basename(urlparse(f.get("url") or "").path)
    return base if "." in base else ("image.jpg" if f.get("kind") == "image" else "file")

# ------------------------------------------------------------------ adapter
class ZaloAdapter(ch.Adapter):
    kind = KIND
    needs_secret = True

    def parse_incoming(self, raw):
        """raw = webhook event dict."""
        if not isinstance(raw, dict) or raw.get("event_name") not in EVENTS: return None
        uid = str((raw.get("sender") or {}).get("id") or "")
        m = raw.get("message") or {}
        if not uid or not isinstance(m, dict): return None
        files = []
        for a in m.get("attachments") or []:
            p = (a or {}).get("payload") or {}
            if (a or {}).get("type") in ("image", "file") and p.get("url"):
                files.append({"kind": a["type"], "url": str(p["url"]), "name": p.get("name"), "size": p.get("size")})
        text = str(m.get("text") or "")
        if not text.strip() and not files: return None
        return ch.Incoming(chat_id=uid, user_id=uid, text=text, files=files)

    async def fetch_files(self, binding, msg):
        """Download from the event URLs and store through the 6.8 path (attachments.store: sniff + convert, limits)."""
        import attachments, docfiles as D
        out = []
        for f in msg.files[:attachments.MAX_PER_MESSAGE]:
            name = _name(f)
            try: declared = int(f.get("size") or 0)
            except (TypeError, ValueError): declared = 0
            if declared > D.DOC_MAX_BYTES: raise HTTPException(413, FILE_TOO_BIG.format(name))
            data = await _download(f["url"], name, D.DOC_MAX_BYTES)
            meta = await attachments.store(data, name)
            if meta.get("kind") and meta.get("status") != "ok": raise HTTPException(422, f"tệp {name} không đọc được: {meta.get('reason')}")
            out.append({"type": "file" if meta.get("kind") else "image", "url": meta["url"]})
        return out

    async def _blocked(self, binding, chat_id) -> str | None:
        r = await ch._q("SELECT extract(epoch FROM last_user_at)::float8 t FROM channel_threads WHERE binding_id=%s AND chat_key=%s",
                        (binding["id"], f"u:{chat_id}"), one=True)
        return window(binding.get("config"), (r or {}).get("t"))

    async def send_text(self, binding, chat_id, text):
        why = await self._blocked(binding, chat_id)
        if why:
            _log(binding["id"], f"no send to user {str(chat_id)[:40]}: {why}"); return
        for part in split_text(md_to_plain(text)) or [ch.EMPTY_TEXT]:
            await _post_cs(binding, {"recipient": {"user_id": str(chat_id)}, "message": {"text": part}})

    async def send_media(self, binding, chat_id, items):
        """items: [{"kind": "image"|"file", "name", "mime", "data": bytes}] (from channels._deliver). One consultation message per item; same send window as text.
        Not sendable (type / size) -> one short text note, never an exception."""
        why = await self._blocked(binding, chat_id)
        if why:
            _log(binding["id"], f"no media to user {str(chat_id)[:40]}: {why}"); return
        for it in items:
            name = it.get("name") or "tệp"
            try:
                if it.get("kind") == "image": await self._send_image(binding, chat_id, name, it["data"])
                else: await self._send_file(binding, chat_id, name, it["data"])
            except _Skip as e:
                await _post_cs(binding, {"recipient": {"user_id": str(chat_id)}, "message": {"text": NO_MEDIA.format(name, e)}})
            except ZaloError as e:
                _log(binding["id"], f"media send failed: {str(e)[:160]}")
                await _post_cs(binding, {"recipient": {"user_id": str(chat_id)}, "message": {"text": NO_MEDIA.format(name, "Zalo từ chối tệp")}})

    async def _send_image(self, b, chat_id, name, data):
        img = await asyncio.to_thread(fit_image, data)
        if img is None: raise _Skip("không đọc được ảnh hoặc ảnh quá lớn")
        png = img[:4] == b"\x89PNG"
        up = await _post_api(b, UPLOAD_IMAGE, files={"file": ((os.path.splitext(name)[0] or "image") + (".png" if png else ".jpg"), img, "image/png" if png else "image/jpeg")})
        aid = (up.get("data") or {}).get("attachment_id")
        if not aid: raise ZaloError("upload image: no attachment_id")
        await _post_cs(b, {"recipient": {"user_id": str(chat_id)}, "message": {"attachment": {"type": "template", "payload": {
            "template_type": "media", "elements": [{"media_type": "image", "attachment_id": aid}]}}}})

    async def _send_file(self, b, chat_id, name, data):
        ext = os.path.splitext(name)[1].lstrip(".").lower()
        if ext not in FILE_EXTS: raise _Skip("Zalo chỉ nhận tệp PDF/DOC/DOCX")
        if len(data) > FILE_MAX: raise _Skip("tệp lớn hơn 5 MB")
        up = await _post_api(b, UPLOAD_FILE, files={"file": (name, data, "application/octet-stream")})
        tok = (up.get("data") or {}).get("token")
        if not tok: raise ZaloError("upload file: no token")
        await _post_cs(b, {"recipient": {"user_id": str(chat_id)}, "message": {"attachment": {"type": "file", "payload": {"token": tok}}}})

    async def send_approval(self, binding, chat_id, approval_id, summary):
        await self.send_text(binding, chat_id, f"{summary}\n\n{APPROVE_HINT.format(id=int(approval_id))}")

ADAPTER = ZaloAdapter()
ch.register(ADAPTER)

# ------------------------------------------------------------------ webhook
_TASKS: set = set()
_CHAT_LOCKS: "weakref.WeakValueDictionary" = weakref.WeakValueDictionary()
_PRUNE = {"n": 0}

def _chat_lock(key):
    lk = _CHAT_LOCKS.get(key)
    if lk is None: lk = _CHAT_LOCKS[key] = asyncio.Lock()
    return lk

def _event_key(ev, body: bytes) -> str:
    mid = (ev.get("message") or {}).get("msg_id") if isinstance(ev.get("message"), dict) else None
    return f"m:{mid}"[:200] if mid else "h:" + hashlib.sha256(body).hexdigest()

async def _first_time(b, key) -> bool:
    with ch._system():
        r = await ch._q("INSERT INTO channel_events(workspace_id,binding_id,event_key) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING RETURNING 1",
                        (b["workspace_id"], b["id"], key), one=True)
        _PRUNE["n"] += 1
        if _PRUNE["n"] % 200 == 0: await ch._q("DELETE FROM channel_events WHERE created_at < now() - interval '8 days'")
    return bool(r)

async def process(b, ev):
    """Background: one event -> interaction time recorded -> decision or agent run -> reply (window rules in send_text)."""
    try:
        msg = ADAPTER.parse_incoming(ev)
        if not msg:
            _log(b["id"], f"ignored event {str(ev.get('event_name'))[:40]}"); return {"status": "ignored", "reason": "event"}
        why = ch._gate(b, msg.user_id)
        if why:
            _log(b["id"], "ignored: " + why); return {"status": "ignored", "reason": why}
        async with _chat_lock((b["id"], msg.user_id)):   # one run at a time per chat (same lg thread)
            async with ch.as_owner(b):
                await ch._thread_for(b, ch.chat_key(msg))
                r = await ch._q("""UPDATE channel_threads SET last_user_at=GREATEST(COALESCE(last_user_at, to_timestamp(0)), to_timestamp(%s))
                                   WHERE binding_id=%s AND chat_key=%s RETURNING extract(epoch FROM last_user_at)::float8 t""",
                                (_ts(ev), b["id"], ch.chat_key(msg)), one=True)
            why = window(b.get("config"), (r or {}).get("t"))
            if why:
                _log(b["id"], f"no reply to user {msg.user_id[:40]}: {why}"); return {"status": "ignored", "reason": why}
            d = None if msg.files else parse_decision(msg.text)
            if d: return await ch.handle_decision(b["id"], msg.chat_id, msg.user_id, d[0], d[1], ADAPTER)
            return await ch.handle_incoming(b["id"], msg, ADAPTER)
    except Exception as e:  # noqa: BLE001
        _log(b["id"], f"event failed: {type(e).__name__}: {str(e)[:200]}"); return {"status": "error"}

@router.post("/channels/zalo/{binding_id}")
async def webhook(binding_id: int, request: Request):
    """Public (no user auth): the signature is the authentication. 200 at once, work in the background."""
    body = await request.body()
    if len(body) > MAX_BODY: raise HTTPException(413, "too large")
    b = await ch._binding(binding_id)
    if not b or b["kind"] != KIND: raise HTTPException(404, "Not found")
    try: ev = json.loads(body)
    except ValueError: raise HTTPException(400, "bad json") from None
    if not isinstance(ev, dict): raise HTTPException(400, "bad json")
    try: s = await _secret(b)
    except Exception as e:  # noqa: BLE001
        _log(binding_id, f"cannot check signature: {type(e).__name__}: {str(e)[:160]}"); raise HTTPException(401, "bad signature") from None
    if not verify(s["app_id"], body, ev.get("timestamp"), s["oa_secret_key"], request.headers.get("x-zevent-signature")):
        _log(binding_id, "rejected webhook: bad signature"); raise HTTPException(401, "bad signature")
    if b["status"] != "active" or ch._off(b["workspace_id"]):
        _log(binding_id, "ignored webhook: binding disabled or demo workspace"); return {"ok": True}
    why_stale = stale_reason(ev.get("timestamp"), limit=fresh_limit(b.get("config")))
    if why_stale:
        _log(binding_id, f"stale/odd event not processed: {why_stale}"); return {"ok": True, "stale": True}
    if not await _first_time(b, _event_key(ev, body)):
        _log(binding_id, f"duplicate event ignored (num_retry={str(request.headers.get('num_retry') or '')[:8]})"); return {"ok": True, "duplicate": True}
    t = asyncio.create_task(process(b, ev)); _TASKS.add(t); t.add_done_callback(_TASKS.discard)
    return {"ok": True}
