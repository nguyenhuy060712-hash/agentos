"""GĐ8.2 Telegram adapter (23/10/2026) on aiogram 3.31 (MIT): long polling (getUpdates), no public URL needed.

- One poller task per active telegram binding. Pollers run in every process that runs the run queue (API locally, API + worker on the VPS);
  a Postgres session advisory lock (LOCK_NS, binding_id) guarantees ONE poller per binding across processes (Telegram answers 409 to concurrent
  getUpdates). A supervisor loop (TELEGRAM_SUPERVISOR_S, default 15 s) starts pollers for newly active bindings and stops disabled/deleted ones.
- Token = ws secret `binding.secret_ref` (ws_secrets.get_secret, allowed host api.telegram.org). Never logged (scrubbed from error text).
- Messages: text, photo, document -> attachments.store (6.8 docfiles path, 20 MB) -> standard content blocks on the run input (like the chat).
  Groups: answered only when the bot is @mentioned or the message replies to the bot (mention stripped).
- Approvals: inline keyboard Duyệt / Từ chối, callback_data "ap:<approval_id>:1|0" -> channels.handle_decision; the button message is edited.
- Replies: agent Markdown -> Telegram-safe HTML (everything escaped; only b/i/code/pre/a), split at 4096 (UTF-16) without cutting inside tags.
- Mock mode: TELEGRAM_MOCK=1 -> Bot API base = TELEGRAM_API_BASE (must be loopback; default http://127.0.0.1:8781), e.g. the fake Bot API of
  tests/test_gd8_telegram.py. The local launcher (overlay/run_local.py) sets it, so the local stack never talks to api.telegram.org.
  TELEGRAM_POLL=0 disables the pollers in a process."""
import asyncio, contextlib, html, io, os, re, weakref
from urllib.parse import urlparse
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
import channels as ch

KIND = "telegram"
HOST = "api.telegram.org"
LOCK_NS = 8202                      # pg_try_advisory_lock(LOCK_NS, binding_id)
MOCK_BASE = "http://127.0.0.1:8781"
LIMIT = 4096
POLL_TIMEOUT = int(os.environ.get("TELEGRAM_POLL_TIMEOUT", "25"))
SUPERVISOR_S = float(os.environ.get("TELEGRAM_SUPERVISOR_S", "15"))
CB_RE = re.compile(r"^ap:(\d{1,18}):([01])$")
TOKEN_RE = re.compile(r"\d{3,}:[A-Za-z0-9_-]{10,}")
FILE_TOO_BIG = "Tệp {} quá lớn (tối đa 20 MB)."
DONE = {True: "Đã duyệt", False: "Đã từ chối"}

def _log(bid, what): print(f"telegram: binding {bid}: {what}", flush=True)

def _safe(e) -> str:
    """Exception text without any bot token (aiohttp/aiogram messages may contain URLs)."""
    s = f"{type(e).__name__}: {e}"
    for tok, _ in list(_BOTS.values()): s = s.replace(tok, "***")
    return TOKEN_RE.sub("***", s)[:200]

# ------------------------------------------------------------------ Markdown -> Telegram HTML
def _esc(s): return html.escape(s, quote=False)

def md_to_html(md: str) -> str:
    """Agent Markdown -> Telegram HTML. Everything is escaped first; only <b> <i> <code> <pre> <a href=http(s)> are produced."""
    keep = []
    def stash(h): keep.append(h); return f"\x00{len(keep) - 1}\x00"
    s = (md or "").replace("\x00", "")
    s = re.sub(r"```[^\n`]*\n?(.*?)(?:```|\Z)", lambda m: stash(f"<pre>{_esc(m[1].rstrip(chr(10)))}</pre>"), s, flags=re.S)
    s = re.sub(r"`([^`\n]+)`", lambda m: stash(f"<code>{_esc(m[1])}</code>"), s)
    s = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s()<>\"]+)\)", lambda m: stash(f'<a href="{html.escape(m[2], quote=True)}">{_esc(m[1])}</a>'), s)
    s = _esc(s)
    s = re.sub(r"(?m)^[ \t]*#{1,6}[ \t]+(.+?)[ \t#]*$", r"<b>\1</b>", s)
    s = re.sub(r"(?m)^([ \t]*)[-*+][ \t]+", "\\1• ", s)
    s = re.sub(r"\*\*(?=\S)([^\n]+?)(?<=\S)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\w])__(?=\S)([^\n]+?)(?<=\S)__(?![\w])", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\w*])\*(?=[^\s*])([^*\n<>]+?)(?<=[^\s*])\*(?![\w*])", r"<i>\1</i>", s)
    s = re.sub(r"(?<![\w])_(?=[^\s_])([^_\n<>]+?)(?<=[^\s_])_(?![\w])", r"<i>\1</i>", s)
    return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m[1])], s)

def _u16(s): return len(s.encode("utf-16-le")) // 2
_TOK = re.compile(r"<[^>]+>|&[#\w]+;|\n| +|[^<&\n ]+")

def split_html(h: str, limit: int = LIMIT) -> list:
    """Split Telegram HTML into parts of <= limit UTF-16 units; open tags are closed at the end of a part and reopened in the next."""
    out, cur, stack, step = [], "", [], max(1, limit // 4)
    close = lambda: "".join(f"</{n}>" for n, _ in reversed(stack))
    for tok in _TOK.findall(h):
        pieces = [tok[i:i + step] for i in range(0, len(tok), step)] if tok[0] not in "<&" and len(tok) > step else [tok]
        for p in pieces:
            if _u16(cur) + _u16(p) + _u16(close()) > limit and re.sub(r"<[^>]+>", "", cur).strip():
                out.append(cur + close()); cur = "".join(o for _, o in stack)
            cur += p
            if p.startswith("</"):
                if stack: stack.pop()
            elif p.startswith("<"): stack.append((re.match(r"<(\w+)", p)[1], p))
    if re.sub(r"<[^>]+>", "", cur).strip(): out.append(cur + close())
    return out

def _plain(h: str) -> str: return html.unescape(re.sub(r"<[^>]+>", "", h))

# ------------------------------------------------------------------ bots (one per binding, cached with its token)
_BOTS: dict = {}                    # binding_id -> (token, Bot)

def _api_base() -> str:
    base = (os.environ.get("TELEGRAM_API_BASE") or "").strip()
    if os.environ.get("TELEGRAM_MOCK") == "1":
        base = base or MOCK_BASE
        if urlparse(base).hostname not in ("127.0.0.1", "localhost", "::1"): raise RuntimeError("TELEGRAM_MOCK=1 needs a loopback TELEGRAM_API_BASE")
    return base or "https://api.telegram.org"

async def _bot(b) -> Bot:
    import ws_secrets
    if not b.get("secret_ref"): raise LookupError("binding has no secret_ref (bot token)")
    tok = await ws_secrets.get_secret(b["secret_ref"], b["workspace_id"], HOST)
    old = _BOTS.get(b["id"])
    if old and old[0] == tok: return old[1]
    bot = Bot(tok, session=AiohttpSession(api=TelegramAPIServer.from_base(_api_base())),
              default=DefaultBotProperties(parse_mode="HTML", link_preview_is_disabled=True))
    _BOTS[b["id"]] = (tok, bot)
    if old: asyncio.get_running_loop().call_later(60, lambda: asyncio.ensure_future(old[1].session.close()))   # token rotated: let in-flight sends finish
    return bot

# ------------------------------------------------------------------ adapter
def _user_name(u): return (u.username or u.full_name or "") if u else ""

class TelegramAdapter(ch.Adapter):
    kind = KIND
    needs_secret = True

    def parse_incoming(self, raw):
        """raw = {"update": aiogram Update, "me": bot User}."""
        upd, me = raw["update"], raw["me"]
        m = upd.message
        if not m or not m.from_user or m.from_user.is_bot: return None
        text = m.text if m.text is not None else (m.caption or "")
        ents = (m.entities if m.text is not None else m.caption_entities) or []
        files = []
        if m.photo:
            p = m.photo[-1]; files.append({"kind": "photo", "file_id": p.file_id, "name": f"photo_{p.file_unique_id}.jpg", "size": p.file_size})
        if m.document:
            d = m.document; files.append({"kind": "document", "file_id": d.file_id, "name": d.file_name or "file", "size": d.file_size})
        if not text.strip() and not files: return None
        is_group = m.chat.type in ("group", "supergroup")
        uname = (me.username or "").strip()
        mrx = re.compile(r"(?<![\w@])@" + re.escape(uname) + r"(?!\w)", re.I) if uname else None
        mentioned = bool(mrx and mrx.search(text)) or any(e.type == "text_mention" and e.user and e.user.id == me.id for e in ents) \
            or bool(m.reply_to_message and m.reply_to_message.from_user and m.reply_to_message.from_user.id == me.id)
        if mrx: text = re.sub(r"[ \t]{2,}", " ", mrx.sub("", text)).strip()
        return ch.Incoming(chat_id=str(m.chat.id), user_id=str(m.from_user.id), user_name=_user_name(m.from_user), text=text, files=files,
                           is_group=is_group, mentioned=mentioned)

    async def fetch_files(self, binding, msg):
        """Download through the Bot API (getFile) and store through the 6.8 path (attachments.store: sniff + convert, limits)."""
        import attachments, docfiles as D
        from fastapi import HTTPException
        bot, out = await _bot(binding), []
        for f in msg.files[:attachments.MAX_PER_MESSAGE]:
            if (f.get("size") or 0) > D.DOC_MAX_BYTES: raise HTTPException(413, FILE_TOO_BIG.format(f["name"]))
            tf = await bot.get_file(f["file_id"])
            if (tf.file_size or 0) > D.DOC_MAX_BYTES or not tf.file_path: raise HTTPException(413, FILE_TOO_BIG.format(f["name"]))
            data = (await bot.download_file(tf.file_path, destination=io.BytesIO())).getvalue()
            meta = await attachments.store(data, f["name"])
            if meta.get("kind") and meta.get("status") != "ok": raise HTTPException(422, f"tệp {f['name']} không đọc được: {meta.get('reason')}")
            out.append({"type": "file" if meta.get("kind") else "image", "url": meta["url"]})
        return out

    async def _send(self, bot, chat_id, h, **kw):
        try: return await bot.send_message(chat_id, h, **kw)
        except TelegramBadRequest as e:   # entity parse error -> same text without formatting
            if "entit" not in str(e).lower() and "tag" not in str(e).lower(): raise
            return await bot.send_message(chat_id, _plain(h), parse_mode=None, **kw)

    async def send_text(self, binding, chat_id, text):
        bot = await _bot(binding)
        for part in split_html(md_to_html(text)) or [_esc(ch.EMPTY_TEXT)]:
            await self._send(bot, chat_id, part)

    async def send_approval(self, binding, chat_id, approval_id, summary):
        bot = await _bot(binding)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Duyệt",callback_data=f"ap:{int(approval_id)}:1"),
                                                    InlineKeyboardButton(text="Từ chối", callback_data=f"ap:{int(approval_id)}:0")]])
        await self._send(bot, chat_id, _esc(summary[:3500]), reply_markup=kb)

ADAPTER = TelegramAdapter()
ch.register(ADAPTER)

# ------------------------------------------------------------------ update handling
_CHAT_LOCKS: "weakref.WeakValueDictionary" = weakref.WeakValueDictionary()
_DECIDING: set = set()

def _chat_lock(key):
    lk = _CHAT_LOCKS.get(key)
    if lk is None: lk = _CHAT_LOCKS[key] = asyncio.Lock()
    return lk

async def _typing(bot, chat_id):
    with contextlib.suppress(Exception):
        while True:
            await bot.send_chat_action(chat_id, "typing"); await asyncio.sleep(4.5)

async def _on_message(b, bot, me, upd):
    msg = ADAPTER.parse_incoming({"update": upd, "me": me})
    if not msg: return None
    async with _chat_lock((b["id"], ch.chat_key(msg))):   # one run at a time per chat (same lg thread)
        show = (msg.mentioned or not msg.is_group) and ch.user_allowed(b, msg.user_id, msg.user_name)
        t = asyncio.create_task(_typing(bot, msg.chat_id)) if show else None
        try: return await ch.handle_incoming(b["id"], msg, ADAPTER)
        finally:
            if t: t.cancel()

async def _on_callback(b, bot, cq):
    m = CB_RE.match(cq.data or "")
    if not m or not cq.message or (aid := int(m[1])) in _DECIDING:
        with contextlib.suppress(Exception): await bot.answer_callback_query(cq.id, text="Đang xử lý…" if m else None)
        return None
    approve, chat = m[2] == "1", cq.message.chat
    _DECIDING.add(aid)
    try:
        with contextlib.suppress(Exception): await bot.answer_callback_query(cq.id, text="Đã nhận")
        t = asyncio.create_task(_typing(bot, chat.id))
        try: res = await ch.handle_decision(b["id"], str(chat.id), str(cq.from_user.id), aid, approve, ADAPTER, user_name=_user_name(cq.from_user))
        finally: t.cancel()
        if res.get("status") in ("replied", "approval", "timeout", "error"):
            body = getattr(cq.message, "html_text", None) or _esc(getattr(cq.message, "text", "") or "")
            with contextlib.suppress(Exception):
                await bot.edit_message_text(text=f"{body}\n\n<b>{DONE[approve]}</b>", chat_id=chat.id, message_id=cq.message.message_id, reply_markup=None)
        return res
    finally: _DECIDING.discard(aid)

async def _dispatch(b, bot, me, upd):
    try:
        if upd.callback_query: return await _on_callback(b, bot, upd.callback_query)
        return await _on_message(b, bot, me, upd)
    except Exception as e:  # noqa: BLE001
        _log(b["id"], f"update {upd.update_id} failed: {_safe(e)}")

# ------------------------------------------------------------------ pollers + supervisor
POLLERS: dict = {}                  # binding_id -> asyncio.Task (this process)
_INFLIGHT: dict = {}                # binding_id -> set of update tasks

async def try_lock(conn, bid) -> bool:
    return bool((await (await conn.execute("SELECT pg_try_advisory_lock(%s::int,%s::int)", (LOCK_NS, int(bid)))).fetchone())[0])

async def _poll(bid):
    """Long-poll one binding while it is active. Returns 'busy' when another process holds its lock, 'stopped' when the binding is off."""
    import psycopg
    conn = await psycopg.AsyncConnection.connect(os.environ["PG_URI"], autocommit=True)
    try:
        if not await try_lock(conn, bid): return "busy"
        _log(bid, "poller started"); offset, delay = None, 1
        while True:
            await conn.execute("SELECT 1")   # the lock lives on this connection: if it died, stop (another process may take over)
            b = await ch._binding(bid)
            if not b or b["status"] != "active" or b["kind"] != KIND or ch._off(b["workspace_id"]):
                _log(bid, "poller stopped (binding off)"); return "stopped"
            try:
                with ch._system():
                    bot = await _bot(b); me = await bot.me()
                ups = await bot.get_updates(offset=offset, timeout=POLL_TIMEOUT, allowed_updates=["message", "callback_query"], request_timeout=POLL_TIMEOUT + 15)
                delay = 1
            except asyncio.CancelledError: raise
            except Exception as e:  # noqa: BLE001  (bad token, 409 conflict, network...) -> back off, never log the token
                wait = max(delay, int(getattr(e, "retry_after", 0) or 0))
                _log(bid, f"poll error {_safe(e)}; retry in {wait}s"); await asyncio.sleep(wait); delay = min(delay * 2, 60)
                continue
            for u in ups:
                offset = u.update_id + 1
                t = asyncio.create_task(_dispatch(b, bot, me, u))
                _INFLIGHT.setdefault(bid, set()).add(t); t.add_done_callback(_INFLIGHT[bid].discard)
    finally:
        with contextlib.suppress(Exception): await conn.close()

async def _tick():
    with ch._system():
        rows = await ch._q("SELECT id, workspace_id FROM channel_bindings WHERE kind=%s AND status='active'", (KIND,))
    want = {r["id"] for r in rows or [] if not ch._off(r["workspace_id"])}
    for bid, t in list(POLLERS.items()):
        if bid not in want:
            t.cancel(); POLLERS.pop(bid, None); _log(bid, "poller cancelled (binding disabled or deleted)")
        elif t.done(): POLLERS.pop(bid, None)
    for bid in want - POLLERS.keys(): POLLERS[bid] = asyncio.create_task(_poll(bid))
    for bid in [x for x in _BOTS if x not in want and not _INFLIGHT.get(x)]:
        with contextlib.suppress(Exception): await _BOTS.pop(bid)[1].session.close()

async def supervisor():
    try:
        while True:
            try: await _tick()
            except asyncio.CancelledError: raise
            except Exception as e:  # noqa: BLE001
                print(f"telegram: supervisor tick failed: {_safe(e)}", flush=True)
            await asyncio.sleep(SUPERVISOR_S)
    finally:
        for t in POLLERS.values(): t.cancel()
        POLLERS.clear()

def start() -> asyncio.Task:
    """Called from the app lifespan of every process that runs the run queue."""
    if os.environ.get("TELEGRAM_POLL", "1") == "0": return asyncio.create_task(asyncio.sleep(0))
    return asyncio.create_task(supervisor())
