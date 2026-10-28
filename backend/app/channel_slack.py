"""GĐ8.4 Slack adapter (23/10/2026) on slack-bolt 1.30.0 / slack-sdk 3.45.0 (MIT), Socket Mode: no public URL needed.

- One Slack app per binding. Binding secret (ws secret, allowed hosts slack.com + files.slack.com) = JSON {"bot_token": "xoxb-…",
  "app_token": "xapp-…"}. Both tokens only go to slack.com (Web API; the app token only to apps.connections.open); the bot token also to
  files.slack.com (url_private downloads). The Socket Mode WebSocket URL (wss://wss-primary.slack.com/…, carries a one-time ticket, no token)
  is checked: wss + *.slack.com (mock: loopback). Tokens are never logged (scrubbed from error text).
- One AsyncApp + AsyncSocketModeHandler (aiohttp) per active slack binding, in every process that runs the run queue; a Postgres session
  advisory lock (LOCK_NS, binding_id) keeps ONE Socket Mode connection per app across processes. A supervisor loop (SLACK_SUPERVISOR_S,
  default 15 s) starts connections for newly active bindings and closes disabled/deleted ones (and reconnects when the secret changes).
- Events: app_mention in channels (the bot mention is stripped) and direct messages (message.im). Bot messages, edits/deletes and other
  subtypes are ignored; Slack retries are deduped by (channel, ts). Replies go into the Slack thread (thread_ts = existing thread_ts or the
  message ts); one agent thread per Slack thread (Incoming.per_chat, chat_id = "<channel>:<thread_ts>").
- Files shared with the message: files.info -> url_private (bot token) -> attachments.store (6.8 docfiles path, 20 MB), like Telegram.
- Approvals: Block Kit section + Approve/Reject buttons (action_id aos_approve / aos_reject, value = approval_id) -> channels.handle_decision
  as the clicking Slack user; the button message is replaced with the outcome (chat.update).
- Question forms (GĐ12.74): an ask_user pause (hoi_thong_tin) -> a message with an "Trả lời" button (aos_ask_open) + a channel_prompts row; the click opens a modal built from the
  fields (text / number_input / static_select / multi_static_select / checkboxes / datepicker, max 8); the submit is validated (chat_gd5.check_answers; errors shown in the modal),
  the FIRST valid answer resumes the run (channels.handle_answer), a 2nd submit sees "đã được trả lời"; a typed reply declines it (channels._supersede); prompts expire after 24 h.
- Sign-in pause (GĐ12.78): a tool that needs an account the person has not connected pauses the run; the channel shows a "Kết nối" URL button (AOS_PUBLIC_URL + the
  relative sign-in path; unset = the path as text) and "Đã kết nối — chạy tiếp" (aos_connect_resume). The second one resumes only when the auth_sessions row is completed
  (channels.handle_connect); a pending one says "chưa thấy kết nối" and changes nothing. Only the binding OWNER's account is connected (no Slack-user -> user mapping): a session
  the owner may not complete (an agent / workspace account, owner not admin) gets "nhờ quản trị viên" and no button. Access = the binding's allowed_users.
- Files out (GĐ12.77): files the agent produced (reply links /api/files/<id> or the run's /workspace/out files) are uploaded into the thread with the Slack V2 flow
  (files.getUploadURLExternal -> POST the bytes to the returned URL (must be files.slack.com; mock: loopback; the ws secret must allow that host) -> files.completeUploadExternal
  with thread_ts); <= 5 per reply, each <= 20 MB (else a polite sentence), never twice for the same (binding, chat, file id) within an hour (retried delivery).
- Replies: agent Markdown -> Slack mrkdwn (*bold*, _italic_, `code`, ``` blocks, <url|text>), split at 3,500 chars per message.
- Mock: SLACK_MOCK=1 -> Web API base = SLACK_API_BASE (must be loopback; default http://127.0.0.1:8783/api/), Socket Mode URL and file URLs
  must be loopback too. overlay/run_local.py sets it, so the local stack never talks to slack.com. SLACK_SOCKET=0 disables the connections."""
import asyncio, contextlib, json, logging, os, re, time, weakref
from urllib.parse import urlparse
import httpx
from fastapi import HTTPException
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.app.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient
import channels as ch

KIND = "slack"
HOST = "slack.com"
FILES_HOST = "files.slack.com"
LOCK_NS = 8204                      # pg_try_advisory_lock(LOCK_NS, binding_id)
MOCK_BASE = "http://127.0.0.1:8783/api/"
LIMIT = 3500
SUPERVISOR_S = float(os.environ.get("SLACK_SUPERVISOR_S", "15"))
APPROVE, REJECT = "aos_approve", "aos_reject"
ASK_OPEN, ASK_VIEW = "aos_ask_open", "aos_ask"
ASK_BTN = "Trả lời"
CONNECT_OPEN, CONNECT_RESUME = "aos_connect_open", "aos_connect_resume"
CONNECT_BTN = "Kết nối"
CONNECT_RESUME_BTN = "Đã kết nối — chạy tiếp"
CONNECT_HEAD = "Trợ lý cần anh/chị kết nối tài khoản {} để làm tiếp."
CONNECT_PATH = "Mở liên kết này để kết nối: {}"
CONNECT_DONE = "Đã kết nối — đang chạy tiếp"
ASK_BY = "Đã trả lời bởi"
ASK_NO_PERM = "Bạn không có quyền trả lời biểu mẫu này."
ASK_NOT_NUM = "Hãy nhập một số."
ASK_NOT_OPT = "Hãy chọn một giá trị trong danh sách."
ASK_MISSING = "Trường này là bắt buộc."
TOKEN_RE = re.compile(r"xox[a-z]-[A-Za-z0-9-]+|xapp-[A-Za-z0-9-]+")
FILE_TOO_BIG = "Tệp {} quá lớn (tối đa 20 MB)."
FILE_FAIL = "không tải được tệp {}"
FILE_HOST_TEXT = "bí mật Slack chưa cho phép máy files.slack.com"
UPLOAD_FAIL = "Không gửi được tệp {} lên Slack."
UPLOAD_TTL_S = 3600
DONE = {True: "Đã duyệt", False: "Đã từ chối"}
LOG = logging.getLogger("agentos.slack"); LOG.setLevel(logging.WARNING)

def _log(bid, what): print(f"slack: binding {bid}: {what}", flush=True)

def _safe(e) -> str:
    """Exception text without any Slack token."""
    s = f"{type(e).__name__}: {e}"
    for _, sec, _c in list(_CLIENTS.values()):
        for t in sec.values(): s = s.replace(t, "***")
    return TOKEN_RE.sub("***", s)[:200]

def _loopback(url) -> bool: return urlparse(url or "").hostname in ("127.0.0.1", "localhost", "::1")
def _mock() -> bool: return os.environ.get("SLACK_MOCK") == "1"

def api_base() -> str:
    """Web API base. Real mode is always https://slack.com/api/ (the secret is only allowed for slack.com)."""
    if not _mock(): return "https://slack.com/api/"
    base = (os.environ.get("SLACK_API_BASE") or "").strip() or MOCK_BASE
    if not _loopback(base): raise RuntimeError("SLACK_MOCK=1 needs a loopback SLACK_API_BASE")
    return base if base.endswith("/") else base + "/"

def ws_url_ok(url) -> bool:
    u = urlparse(url or "")
    if _mock(): return u.scheme in ("ws", "wss") and _loopback(url)
    return u.scheme == "wss" and (u.hostname or "").endswith(".slack.com")

def file_url_ok(url) -> bool:
    u = urlparse(url or "")
    if _mock(): return u.scheme in ("http", "https") and _loopback(url)
    return u.scheme == "https" and u.hostname == FILES_HOST

def parse_secret(raw: str) -> dict:
    try: s = json.loads(raw)
    except ValueError: s = None
    if not isinstance(s, dict): raise ValueError("Slack secret must be JSON {bot_token, app_token}")
    bt, at = str(s.get("bot_token") or "").strip(), str(s.get("app_token") or "").strip()
    if not bt.startswith("xoxb-") or not at.startswith("xapp-"): raise ValueError("Slack secret needs bot_token (xoxb-…) and app_token (xapp-…)")
    return {"bot_token": bt, "app_token": at}

# ------------------------------------------------------------------ Markdown -> Slack mrkdwn
def _esc(s): return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
def _unesc(s): return s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")

TABLE_RE = re.compile(r"(?m)^[ \t]*\|.*\|[ \t]*\n[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(?:\|[ \t]*:?-{2,}:?[ \t]*)*\|?[ \t]*(?:\n[ \t]*\|.*\|[ \t]*)*")

def _table_text(block: str) -> str:
    """A Markdown pipe table -> plain aligned columns (header, a dashed rule, rows)."""
    rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in block.strip("\n").split("\n")]
    rows = [r for i, r in enumerate(rows) if i != 1]            # row 1 is the |---|---| separator
    n = max(len(r) for r in rows); rows = [r + [""] * (n - len(r)) for r in rows]
    w = [max(len(r[i]) for r in rows) for i in range(n)]
    fmt = lambda r: "  ".join(c.ljust(w[i]) for i, c in enumerate(r)).rstrip()
    return "\n".join([fmt(rows[0]), "  ".join("-" * x for x in w)] + [fmt(r) for r in rows[1:]])

def md_to_mrkdwn(md: str) -> str:
    """Agent Markdown -> Slack mrkdwn. &, <, > escaped everywhere; only http(s) links become <url|text>."""
    keep = []
    def stash(h): keep.append(h); return f"\x00{len(keep) - 1}\x00"
    s = (md or "").replace("\x00", "").replace("\x01", "")
    s = re.sub(r"```[^\n`]*\n?(.*?)(?:```|\Z)", lambda m: stash("```\n" + _esc(m[1].rstrip("\n")) + "\n```"), s, flags=re.S)
    s = re.sub(TABLE_RE, lambda m: stash("```\n" + _esc(_table_text(m[0])) + "\n```"), s)   # GĐ12.77: Slack has no tables: a pipe table becomes an aligned code block
    s = re.sub(r"`([^`\n]+)`", lambda m: stash(f"`{_esc(m[1])}`"), s)
    s = re.sub(r"\[([^\]\n]+)\]\((https?://[^\s()<>\"|]+)\)", lambda m: stash(f"<{_esc(m[2])}|{_esc(m[1]).replace('|', '¦')}>"), s)
    s = _esc(s)
    s = re.sub(r"\*\*(?=\S)([^\n]+?)(?<=\S)\*\*", "\x01\\1\x01", s)
    s = re.sub(r"(?<![\w])__(?=\S)([^\n]+?)(?<=\S)__(?![\w])", "\x01\\1\x01", s)
    s = re.sub(r"(?<![\w*])\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*(?![\w*])", r"_\1_", s)
    s = re.sub(r"~~(?=\S)([^\n]+?)(?<=\S)~~", r"~\1~", s)
    s = re.sub(r"(?m)^[ \t]*#{1,6}[ \t]+(.+?)[ \t#]*$", lambda m: "\x01" + m[1].replace("\x01", "") + "\x01", s)
    s = re.sub(r"(?m)^([ \t]*)[-*+][ \t]+", "\\1• ", s)
    s = s.replace("\x01", "*")
    return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m[1])], s)

def split_mrkdwn(text: str, limit: int = LIMIT) -> list:
    """Split at line boundaries into parts <= limit chars; a ``` block cut in two is closed and reopened."""
    out, cur, fence, step = [], "", False, max(1, limit - 10)
    for line in (text or "").split("\n"):
        for p in [line[i:i + step] for i in range(0, len(line), step)] or [""]:
            add = ("\n" if cur else "") + p
            if len(cur) + len(add) + (4 if fence else 0) > limit and cur.strip():
                out.append(cur + ("\n```" if fence else "")); cur = "```" if fence else ""; add = ("\n" if cur else "") + p
            cur += add
            if p.count("```") % 2: fence = not fence
    if cur.strip() and cur.strip() != "```": out.append(cur)
    return out

# ------------------------------------------------------------------ Web API clients (one per binding, cached with its secret)
_CLIENTS: dict = {}                 # binding_id -> (raw secret, {bot_token, app_token}, AsyncWebClient)

async def _creds(b):
    import ws_secrets
    if not b.get("secret_ref"): raise LookupError("binding has no secret_ref (Slack tokens)")
    raw = await ws_secrets.get_secret(b["secret_ref"], b["workspace_id"], HOST)
    old = _CLIENTS.get(b["id"])
    if old and old[0] == raw: return old[1], old[2]
    s = parse_secret(raw)
    c = AsyncWebClient(token=s["bot_token"], base_url=api_base(), timeout=30, logger=LOG)
    _CLIENTS[b["id"]] = (raw, s, c)
    return s, c

def _split_chat(chat_id: str):
    channel, _, thread = str(chat_id).partition(":")
    return channel, (thread or None)

def approval_blocks(summary: str, approval_id: int) -> list:
    aid = str(int(approval_id))
    return [{"type": "section", "block_id": "aos_ap_text", "text": {"type": "mrkdwn", "text": _esc(summary)[:2900]}},
            {"type": "actions", "block_id": "aos_ap", "elements": [
                {"type": "button", "action_id": APPROVE, "style": "primary", "value": aid, "text": {"type": "plain_text", "text": "Duyệt"}},
                {"type": "button", "action_id": REJECT, "style": "danger", "value": aid, "text": {"type": "plain_text", "text": "Từ chối"}}]}]

def done_blocks(summary_mrkdwn: str, approve: bool, user_id: str) -> list:
    return [{"type": "section", "block_id": "aos_ap_text", "text": {"type": "mrkdwn", "text": summary_mrkdwn[:2900] or " "}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f"*{DONE[approve]}*" + (f" (<@{user_id}>)" if re.fullmatch(r"[A-Z0-9]{2,30}", user_id or "") else "")}]}]

# ------------------------------------------------------------------ GĐ12.74 question form (Block Kit modal)
def ask_blocks(ask: dict, pid: int) -> list:
    head = "*" + _esc(str(ask.get("title") or "Trợ lý cần thêm thông tin"))[:300] + "*" + (("\n" + _esc(str(ask["note"]))[:600]) if ask.get("note") else "")
    return [{"type": "section", "block_id": "aos_ask_text", "text": {"type": "mrkdwn", "text": head[:2900]}},
            {"type": "actions", "block_id": "aos_ask", "elements": [
                {"type": "button", "action_id": ASK_OPEN, "style": "primary", "value": str(int(pid)), "text": {"type": "plain_text", "text": ASK_BTN}}]}]

def connect_blocks(con: dict, pid: int, public_url: str = "") -> list:
    """Section + [URL button "Kết nối" (only with a full URL)] + "Đã kết nối — chạy tiếp". A relative sign-in path is joined to AOS_PUBLIC_URL; without it the path is shown as text."""
    url = str(con.get("connect_url") or "")
    if url.startswith("/") and not url.startswith("//"): url = (public_url.rstrip("/") + url) if public_url else ""
    elif not re.match(r"^https?://", url): url = ""
    head = CONNECT_HEAD.format("*" + _esc(str(con.get("service") or "")) + "*" if con.get("service") else "cần thiết")
    if con.get("message"): head += "\n" + _esc(str(con["message"]))[:400]
    if not url and con.get("connect_url"): head += "\n" + CONNECT_PATH.format("`" + _esc(str(con["connect_url"]))[:300] + "`")
    els = ([{"type": "button", "action_id": CONNECT_OPEN, "url": url[:2000], "text": _pt(CONNECT_BTN, 75)}] if url else [])
    els.append({"type": "button", "action_id": CONNECT_RESUME, "style": "primary", "value": str(int(pid)), "text": _pt(CONNECT_RESUME_BTN, 75)})
    return [{"type": "section", "block_id": "aos_cn_text", "text": {"type": "mrkdwn", "text": head[:2900]}}, {"type": "actions", "block_id": "aos_cn", "elements": els}]

def _pt(text, n): return {"type": "plain_text", "text": str(text)[:n] or " "}

def _opt(o): return {"text": _pt(o, 75), "value": str(o)[:75]}

def field_block(f: dict) -> dict:
    """One ask field -> one Slack input block (block_id f_<name>, action_id v). text -> plain_text_input, number -> number_input, choice -> static_select, multi -> multi_static_select,
    toggle -> checkboxes (one option), date -> datepicker."""
    k = f.get("kind")
    el = {"action_id": "v"}
    if k == "number": el.update(type="number_input", is_decimal_allowed=True)
    elif k == "choice": el.update(type="static_select", options=[_opt(o) for o in f.get("options") or []][:12], placeholder=_pt("Chọn…", 150))
    elif k == "multi": el.update(type="multi_static_select", options=[_opt(o) for o in f.get("options") or []][:12], placeholder=_pt("Chọn…", 150))
    elif k == "toggle": el.update(type="checkboxes", options=[_opt(str(f.get("label") or f["name"]))])
    elif k == "date": el.update(type="datepicker")
    else: el.update(type="plain_text_input", multiline=False)
    if f.get("placeholder") and k in ("text", "number", "date"): el["placeholder"] = _pt(f["placeholder"], 150)
    return {"type": "input", "block_id": "f_" + f["name"], "optional": not f.get("required", True) or k == "toggle", "label": _pt(f.get("label") or f["name"], 2000), "element": el}

def ask_modal(pid: int, payload: dict) -> dict:
    fields = (payload.get("fields") or [])[:8]
    blocks = []
    if payload.get("note"): blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _esc(str(payload["note"]))[:2900]}})
    blocks += [field_block(f) for f in fields]
    return {"type": "modal", "callback_id": ASK_VIEW, "private_metadata": str(int(pid)), "title": _pt(payload.get("title") or ASK_BTN, 24),
            "submit": _pt("Gửi", 24), "close": _pt("Hủy", 24), "blocks": blocks}

def notice_view(text: str) -> dict:
    return {"type": "modal", "title": _pt("Biểu mẫu", 24), "close": _pt("Đóng", 24), "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": _esc(text)[:2900]}}]}

def view_answers(fields: list, values: dict) -> dict:
    """view.state.values -> raw answers {name: value} (not yet validated)."""
    out = {}
    for f in fields:
        el = ((values or {}).get("f_" + f["name"]) or {}).get("v") or {}
        k = f.get("kind")
        if k == "multi": out[f["name"]] = [o.get("value") for o in el.get("selected_options") or []]
        elif k == "choice": out[f["name"]] = (el.get("selected_option") or {}).get("value")
        elif k == "toggle": out[f["name"]] = bool(el.get("selected_options"))
        elif k == "date": out[f["name"]] = el.get("selected_date")
        else: out[f["name"]] = el.get("value")
    return out

def validate_answers(fields: list, raw: dict):
    """-> (clean answers, {block_id: error text}). Every field is checked on its own (chat_gd5.check_answers) so Slack can mark each wrong input."""
    import chat_gd5
    clean, errs = {}, {}
    for f in fields:
        v = raw.get(f["name"]); k = f["kind"]
        try: one = chat_gd5.check_answers([f], {f["name"]: v})
        except ValueError: one = None
        if one is None or f["name"] not in one:
            given = v not in (None, "", [])
            if given and k in ("number", "choice", "multi"): errs["f_" + f["name"]] = ASK_NOT_NUM if k == "number" else ASK_NOT_OPT
            elif one is None: errs["f_" + f["name"]] = ASK_MISSING
            continue
        clean.update(one)
    return clean, errs

FILE_MAX = 20 * 1024 * 1024
_UPLOADED: dict = {}                # (binding, chat_id, file id) -> time of the upload (in memory, UPLOAD_TTL_S)

def _first_upload(key) -> bool:
    now = time.monotonic()
    if len(_UPLOADED) > 2000:
        for k, t in list(_UPLOADED.items()):
            if now - t > UPLOAD_TTL_S: _UPLOADED.pop(k, None)
    if key in _UPLOADED and now - _UPLOADED[key] <= UPLOAD_TTL_S: return False
    _UPLOADED[key] = now; return True

async def _files_host_ok(binding) -> bool:
    """The ws secret must allow files.slack.com for uploads, like for downloads (ws_secrets host rule)."""
    import ws_secrets
    try: await ws_secrets.get_secret(binding["secret_ref"], binding["workspace_id"], FILES_HOST)
    except PermissionError: return False
    return True

async def _download(url, token, name, cap) -> bytes:
    """url_private with the bot token; redirects are not followed (the token must not leave files.slack.com)."""
    if not file_url_ok(url): raise HTTPException(422, FILE_FAIL.format(name))
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as h:
        async with h.stream("GET", url, headers={"Authorization": f"Bearer {token}"}) as r:
            if r.status_code != 200 or "text/html" in r.headers.get("content-type", ""): raise HTTPException(422, FILE_FAIL.format(name))   # html = login page (no files:read)
            buf = bytearray()
            async for chunk in r.aiter_bytes():
                buf += chunk
                if len(buf) > cap: raise HTTPException(413, FILE_TOO_BIG.format(name))
            return bytes(buf)

# ------------------------------------------------------------------ adapter
SUBTYPES_OK = (None, "file_share", "thread_broadcast")

class SlackAdapter(ch.Adapter):
    kind = KIND
    needs_secret = True

    def parse_incoming(self, raw):
        """raw = {"event": Slack event dict, "bot_user_id": "U…"}. app_mention (channels) or message in a DM; else None."""
        ev, bot = raw.get("event") or {}, str(raw.get("bot_user_id") or "")
        if not isinstance(ev, dict) or ev.get("bot_id") or ev.get("subtype") not in SUBTYPES_OK: return None
        uid, channel, ts = str(ev.get("user") or ""), str(ev.get("channel") or ""), str(ev.get("ts") or "")
        if not uid or not channel or not ts or (bot and uid == bot): return None
        dm = ev.get("channel_type") == "im" or channel.startswith("D")
        if ev.get("type") == "app_mention": mentioned = True
        elif ev.get("type") == "message" and dm: mentioned = False
        else: return None
        text = str(ev.get("text") or "")
        text = re.sub(r"<@" + re.escape(bot) + r"(?:\|[^>]*)?>", "", text) if bot else re.sub(r"^\s*<@[A-Z0-9]+(?:\|[^>]*)?>", "", text)
        text = _unesc(re.sub(r"[ \t]{2,}", " ", text)).strip()
        files = [{"id": str(f["id"]), "name": str(f.get("name") or f.get("title") or "file")[:200], "size": f.get("size")}
                 for f in ev.get("files") or [] if isinstance(f, dict) and f.get("id")]
        if not text and not files: return None
        thread = str(ev.get("thread_ts") or ts)
        return ch.Incoming(chat_id=f"{channel}:{thread}", user_id=uid, text=text, files=files, is_group=not dm, mentioned=mentioned or dm, per_chat=True)

    async def fetch_files(self, binding, msg):
        """files.info -> url_private (bot token) -> attachments.store (6.8 path: sniff + convert, limits)."""
        import attachments, docfiles as D
        s, c = await _creds(binding)
        if not await _files_host_ok(binding): raise HTTPException(422, FILE_HOST_TEXT)
        out = []
        for f in msg.files[:attachments.MAX_PER_MESSAGE]:
            name = f["name"]
            try: declared = int(f.get("size") or 0)
            except (TypeError, ValueError): declared = 0
            if declared > D.DOC_MAX_BYTES: raise HTTPException(413, FILE_TOO_BIG.format(name))
            info = (await c.files_info(file=f["id"])).get("file") or {}
            name = str(info.get("name") or name)[:200]
            if int(info.get("size") or 0) > D.DOC_MAX_BYTES: raise HTTPException(413, FILE_TOO_BIG.format(name))
            data = await _download(info.get("url_private_download") or info.get("url_private"), s["bot_token"], name, D.DOC_MAX_BYTES)
            meta = await attachments.store(data, name)
            if meta.get("kind") and meta.get("status") != "ok": raise HTTPException(422, f"tệp {name} không đọc được: {meta.get('reason')}")
            out.append({"type": "file" if meta.get("kind") else "image", "url": meta["url"]})
        return out

    async def send_text(self, binding, chat_id, text):
        _, c = await _creds(binding)
        channel, thread = _split_chat(chat_id)
        for part in split_mrkdwn(md_to_mrkdwn(text)) or [_esc(ch.EMPTY_TEXT)]:
            await c.chat_postMessage(channel=channel, thread_ts=thread, text=part, unfurl_links=False, unfurl_media=False)

    async def send_media(self, binding, chat_id, items):
        """GĐ12.77: upload files into the thread (Slack V2 upload). Larger than 20 MB / not allowed host / failed upload -> a short sentence, the rest still goes."""
        s, c = await _creds(binding)
        if not await _files_host_ok(binding):
            await self.send_text(binding, chat_id, FILE_HOST_TEXT); return
        channel, thread = _split_chat(chat_id)
        for it in items[:ch.MAX_MEDIA]:
            name = str(it.get("name") or "file")[:200]; data = it["data"]
            if len(data) > FILE_MAX: await self.send_text(binding, chat_id, FILE_TOO_BIG.format(name)); continue
            key = (binding["id"], chat_id, str(it.get("id") or name))
            if not _first_upload(key): continue                       # a retried delivery must not post the same file twice
            try:
                up = await c.files_getUploadURLExternal(filename=name, length=len(data))
                url = up.get("upload_url")
                if not file_url_ok(url): raise PermissionError("upload URL host not allowed")
                async with httpx.AsyncClient(timeout=120, follow_redirects=False) as h:
                    r = await h.post(url, content=data, headers={"Content-Type": "application/octet-stream"})
                if r.status_code != 200: raise RuntimeError(f"upload HTTP {r.status_code}")
                await c.files_completeUploadExternal(files=[{"id": up["file_id"], "title": name}], channel_id=channel, thread_ts=thread)
            except Exception as e:  # noqa: BLE001
                _UPLOADED.pop(key, None); _log(binding["id"], f"file upload failed: {_safe(e)}")
                await self.send_text(binding, chat_id, UPLOAD_FAIL.format(name))

    async def send_approval(self, binding, chat_id, approval_id, summary):
        _, c = await _creds(binding)
        channel, thread = _split_chat(chat_id)
        await c.chat_postMessage(channel=channel, thread_ts=thread, text=summary[:3000], blocks=approval_blocks(summary, approval_id))

    async def send_ask(self, binding, chat_id, ask, ctx):
        """GĐ12.74: post the "Trả lời" button for an ask_user pause and remember it (channel_prompts); the form itself opens in a modal."""
        _, c = await _creds(binding)
        channel, thread = _split_chat(chat_id)
        pid = await ch.new_prompt(binding, chat_id, ctx, "ask", ask)
        r = await c.chat_postMessage(channel=channel, thread_ts=thread, text=str(ask.get("title") or "Trợ lý cần thêm thông tin")[:3000], blocks=ask_blocks(ask, pid))
        await ch.set_prompt_ts(pid, r.get("ts") or "")

    async def send_connect(self, binding, chat_id, con, ctx):
        """GĐ12.78: the "Kết nối" / "Đã kết nối — chạy tiếp" buttons for an auth pause (remembered in channel_prompts kind 'connect')."""
        _, c = await _creds(binding)
        channel, thread = _split_chat(chat_id)
        pid = await ch.new_prompt(binding, chat_id, ctx, "connect", con)
        blocks = connect_blocks(con, pid, os.environ.get("AOS_PUBLIC_URL", ""))
        r = await c.chat_postMessage(channel=channel, thread_ts=thread, text=blocks[0]["text"]["text"][:3000], blocks=blocks)
        await ch.set_prompt_ts(pid, r.get("ts") or "")

    async def prompt_closed(self, binding, prompt, text):
        """A typed reply declined a form: replace its button with a short note."""
        if not prompt.get("message_ts"): return
        _, c = await _creds(binding)
        channel, _t = _split_chat(prompt["chat_id"])
        head = (connect_blocks(prompt.get("payload") or {}, prompt["id"]) if prompt.get("kind") == "connect" else ask_blocks(prompt.get("payload") or {}, prompt["id"]))[0]
        await c.chat_update(channel=channel, ts=prompt["message_ts"], text=text, blocks=[head, {"type": "context", "elements": [{"type": "mrkdwn", "text": _esc(text)}]}])

ADAPTER = SlackAdapter()
ch.register(ADAPTER)

# ------------------------------------------------------------------ event handling (called by the bolt listeners; testable directly)
_CHAT_LOCKS: "weakref.WeakValueDictionary" = weakref.WeakValueDictionary()
_DECIDING: set = set()
_SEEN: dict = {}                    # (binding, channel, ts) -> time, Slack retries / app_mention + message.im of the same message

def _chat_lock(key):
    lk = _CHAT_LOCKS.get(key)
    if lk is None: lk = _CHAT_LOCKS[key] = asyncio.Lock()
    return lk

def _first(key) -> bool:
    now = time.monotonic()
    if len(_SEEN) > 2000:
        for k, t in list(_SEEN.items()):
            if now - t > 600: _SEEN.pop(k, None)
    if key in _SEEN: return False
    _SEEN[key] = now; return True

async def on_event(bid: int, event: dict, bot_user_id: str = "") -> dict | None:
    msg = ADAPTER.parse_incoming({"event": event, "bot_user_id": bot_user_id})
    if not msg: return None
    if not _first((bid, event.get("channel"), event.get("ts"))): return {"status": "ignored", "reason": "duplicate"}
    async with _chat_lock((bid, ch.chat_key(msg))):   # one run at a time per Slack thread (same lg thread)
        return await ch.handle_incoming(bid, msg, ADAPTER)

def _where(body: dict):
    """(channel, thread_ts, message dict, container) of a block_actions payload."""
    m, cont = body.get("message") or {}, body.get("container") or {}
    channel = str((body.get("channel") or {}).get("id") or cont.get("channel_id") or "")
    return channel, str(m.get("thread_ts") or cont.get("thread_ts") or m.get("ts") or cont.get("message_ts") or ""), m, cont

async def on_ask_open(bid: int, body: dict) -> dict | None:
    """"Trả lời" clicked: open the modal (trigger_id lives ~3 s) unless the prompt is answered / expired / not for this chat / the user may not answer."""
    act = (body.get("actions") or [{}])[0] or {}
    try: pid = int(act.get("value"))
    except (TypeError, ValueError): return None
    channel, thread, _m, _c = _where(body); user = body.get("user") or {}; uid = str(user.get("id") or "")
    b = await ch._binding(bid)
    why = ch._gate(b, uid, str(user.get("username") or user.get("name") or ""))
    if why: ch._log(bid, "ask ignored: " + why); return {"status": "ignored", "reason": why}
    p, state = await ch.load_prompt(bid, pid, "ask")
    if not p or p["chat_id"] != f"{channel}:{thread}": return {"status": "ignored", "reason": "prompt not in this chat"}
    with ch._system():
        _, c = await _creds(b)
    if state != "ok":
        await ADAPTER.send_text(b, p["chat_id"], ch.PROMPT_EXPIRED_TEXT if state == "expired" else ch.PROMPT_DONE_TEXT); return {"status": state}
    await c.views_open(trigger_id=body.get("trigger_id"), view=ask_modal(pid, p["payload"]))
    return {"status": "opened"}

async def on_connect_resume(bid: int, body: dict) -> dict | None:
    """"Đã kết nối — chạy tiếp" clicked: handled by channels.handle_connect (reads the auth_sessions row); on success the buttons are replaced."""
    act = (body.get("actions") or [{}])[0] or {}
    try: pid = int(act.get("value"))
    except (TypeError, ValueError): return None
    channel, thread, _m, _c = _where(body); user = body.get("user") or {}; uid = str(user.get("id") or ""); uname = str(user.get("username") or user.get("name") or "")
    p, _state = await ch.load_prompt(bid, pid, "connect")
    if not p or p["chat_id"] != f"{channel}:{thread}": return {"status": "ignored", "reason": "prompt not in this chat"}
    async with _chat_lock((bid, f"g:{p['chat_id']}")):
        res = await ch.handle_connect(bid, pid, uid, ADAPTER, user_name=uname)
    if res.get("status") not in ("stale", "ignored", "waiting", "expired") and p.get("message_ts"):
        try:
            with ch._system():
                _, c = await _creds(await ch._binding(bid))
            await c.chat_update(channel=channel, ts=p["message_ts"], text=CONNECT_DONE, blocks=[connect_blocks(p["payload"] or {}, pid)[0],
                                {"type": "context", "elements": [{"type": "mrkdwn", "text": f"*{CONNECT_DONE}*"}]}])
        except Exception as e:  # noqa: BLE001
            _log(bid, f"chat.update failed: {_safe(e)}")
    return res

async def check_view(bid: int, body: dict):
    """view_submission -> (ack kwargs, (pid, uid, uname, answers) | None). Fast: only reads (the 3 s ack window); the run is resumed by finish_ask afterwards."""
    view = body.get("view") or {}
    if view.get("callback_id") != ASK_VIEW: return {}, None
    user = body.get("user") or {}; uid = str(user.get("id") or ""); uname = str(user.get("username") or user.get("name") or "")
    try: pid = int(view.get("private_metadata"))
    except (TypeError, ValueError): return {}, None
    b = await ch._binding(bid)
    why = ch._gate(b, uid, uname)
    if why: ch._log(bid, "ask submit ignored: " + why); return {}, None
    p, state = await ch.load_prompt(bid, pid, "ask")
    if not p: return {}, None
    if state != "ok":
        return {"response_action": "update", "view": notice_view(ch.PROMPT_EXPIRED_TEXT if state == "expired" else ch.PROMPT_DONE_TEXT)}, None
    fields = (p["payload"] or {}).get("fields") or []
    clean, errs = validate_answers(fields, view_answers(fields, (view.get("state") or {}).get("values") or {}))
    if errs: return {"response_action": "errors", "errors": errs}, None
    return {}, (pid, uid, uname, clean)

async def finish_ask(bid: int, pid: int, uid: str, uname: str, answers: dict) -> dict:
    """After the ack: resume the run (first answer wins) and mark the posted message as answered."""
    p, _s = await ch.load_prompt(bid, pid, "ask")
    if not p: return {"status": "ignored"}
    async with _chat_lock((bid, f"g:{p['chat_id']}")):
        res = await ch.handle_answer(bid, pid, uid, answers, ADAPTER, user_name=uname)
    if res.get("status") not in ("stale", "ignored") and p.get("message_ts"):
        try:
            with ch._system():
                _, c = await _creds(await ch._binding(bid))
            channel, _t = _split_chat(p["chat_id"])
            who = f" (<@{uid}>)" if re.fullmatch(r"[A-Z0-9]{2,30}", uid or "") else ""
            await c.chat_update(channel=channel, ts=p["message_ts"], text=ASK_BY, blocks=[ask_blocks(p["payload"] or {}, pid)[0],
                                {"type": "context", "elements": [{"type": "mrkdwn", "text": f"*{ASK_BY}*{who}"}]}])
        except Exception as e:  # noqa: BLE001
            _log(bid, f"chat.update failed: {_safe(e)}")
    return res

async def on_action(bid: int, body: dict) -> dict | None:
    act = (body.get("actions") or [{}])[0] or {}
    if act.get("action_id") == ASK_OPEN: return await on_ask_open(bid, body)
    if act.get("action_id") == CONNECT_RESUME: return await on_connect_resume(bid, body)
    if act.get("action_id") not in (APPROVE, REJECT): return None
    try: aid = int(act.get("value"))
    except (TypeError, ValueError): return None
    if aid in _DECIDING: return {"status": "ignored", "reason": "in progress"}
    approve, m, user = act["action_id"] == APPROVE, body.get("message") or {}, body.get("user") or {}
    cont = body.get("container") or {}
    channel = str((body.get("channel") or {}).get("id") or cont.get("channel_id") or "")
    thread = str(m.get("thread_ts") or cont.get("thread_ts") or m.get("ts") or cont.get("message_ts") or "")
    uid = str(user.get("id") or "")
    _DECIDING.add(aid)
    try:
        res = await ch.handle_decision(bid, f"{channel}:{thread}", uid, aid, approve, ADAPTER, user_name=str(user.get("username") or user.get("name") or ""))
        ts = m.get("ts") or cont.get("message_ts")
        if res.get("status") in ("replied", "approval", "timeout", "error") and ts:
            summary = next((b.get("text", {}).get("text") for b in m.get("blocks") or [] if b.get("type") == "section"), None) or _esc(str(m.get("text") or ""))
            try:
                with ch._system():
                    _, c = await _creds(await ch._binding(bid))
                await c.chat_update(channel=channel, ts=ts, text=f"{_unesc(summary)[:2900]}\n{DONE[approve]}", blocks=done_blocks(summary, approve, uid))
            except Exception as e:  # noqa: BLE001
                _log(bid, f"chat.update failed: {_safe(e)}")
        return res
    finally: _DECIDING.discard(aid)

_TASKS: dict = {}                   # binding_id -> set of running event tasks

def _spawn(bid, coro):
    async def run():
        try: await coro
        except Exception as e:  # noqa: BLE001
            _log(bid, f"event failed: {_safe(e)}")
    t = asyncio.create_task(run()); _TASKS.setdefault(bid, set()).add(t); t.add_done_callback(_TASKS[bid].discard)
    return t

def build_app(bid: int, client: AsyncWebClient) -> AsyncApp:
    """Bolt app of one binding: listeners only hand the payload to on_event / on_action (bolt acks the envelope at once)."""
    app = AsyncApp(client=client, request_verification_enabled=False, logger=LOG)   # Socket Mode: no signing secret

    @app.event("app_mention")
    async def _mention(event, context): _spawn(bid, on_event(bid, event, context.bot_user_id or ""))

    @app.event("message")
    async def _message(event, context): _spawn(bid, on_event(bid, event, context.bot_user_id or ""))

    @app.action(re.compile(f"^({APPROVE}|{REJECT}|{ASK_OPEN}|{CONNECT_OPEN}|{CONNECT_RESUME})$"))
    async def _button(ack, body):
        await ack(); _spawn(bid, on_action(bid, body))

    @app.view(ASK_VIEW)
    async def _ask_submit(ack, body):
        kw, go = await check_view(bid, body)
        await ack(**kw)
        if go: _spawn(bid, finish_ask(bid, *go))

    return app

# ------------------------------------------------------------------ Socket Mode connections + supervisor
CONNS: dict = {}                    # binding_id -> asyncio.Task (this process)

async def try_lock(conn, bid) -> bool:
    return bool((await (await conn.execute("SELECT pg_try_advisory_lock(%s::int,%s::int)", (LOCK_NS, int(bid)))).fetchone())[0])

def _handler(bid, s, c) -> AsyncSocketModeHandler:
    h = AsyncSocketModeHandler(build_app(bid, c), app_token=s["app_token"], logger=LOG)
    orig = h.client.issue_new_wss_url
    async def checked(*a, **k):   # every (re)connect: the WebSocket URL must be Slack's (mock: loopback)
        url = await orig(*a, **k)
        if not ws_url_ok(url): raise PermissionError("Socket Mode URL host not allowed")
        return url
    h.client.issue_new_wss_url = checked
    return h

async def _close(h):
    if h:
        with contextlib.suppress(Exception): await h.close_async()

async def _connect(bid):
    """Hold one Socket Mode connection while the binding is active. 'busy' when another process holds its lock, 'stopped' when it is off."""
    import psycopg
    conn = await psycopg.AsyncConnection.connect(os.environ["PG_URI"], autocommit=True)
    h, key, delay = None, None, 1
    try:
        if not await try_lock(conn, bid): return "busy"
        _log(bid, "socket mode supervisor started")
        while True:
            await conn.execute("SELECT 1")   # the lock lives on this connection: if it died, stop (another process may take over)
            b = await ch._binding(bid)
            if not b or b["status"] != "active" or b["kind"] != KIND or ch._off(b["workspace_id"]):
                _log(bid, "socket mode stopped (binding off)"); return "stopped"
            try:
                with ch._system(): s, c = await _creds(b)
                if h is None or key != (s["bot_token"], s["app_token"]):
                    await _close(h); h = None
                    h = _handler(bid, s, c)
                    h.client.wss_uri = await h.client.issue_new_wss_url()   # fails fast on a bad app token (connect() would retry forever)
                    await asyncio.wait_for(h.connect_async(), 30)
                    key, delay = (s["bot_token"], s["app_token"]), 1
                    _log(bid, "socket mode connected")
            except asyncio.CancelledError: raise
            except Exception as e:  # noqa: BLE001  (bad token, network...) -> back off, never log the token
                await _close(h); h, key = None, None
                _log(bid, f"connect error {_safe(e)}; retry in {delay}s"); await asyncio.sleep(delay); delay = min(delay * 2, 60)
                continue
            await asyncio.sleep(SUPERVISOR_S)
    finally:
        await _close(h)
        with contextlib.suppress(Exception): await conn.close()

async def _tick():
    with ch._system():
        rows = await ch._q("SELECT id, workspace_id FROM channel_bindings WHERE kind=%s AND status='active'", (KIND,))
    want = {r["id"] for r in rows or [] if not ch._off(r["workspace_id"])}
    for bid, t in list(CONNS.items()):
        if bid not in want:
            t.cancel(); CONNS.pop(bid, None); _log(bid, "socket mode cancelled (binding disabled or deleted)")
        elif t.done(): CONNS.pop(bid, None)
    for bid in want - CONNS.keys(): CONNS[bid] = asyncio.create_task(_connect(bid))
    for bid in [x for x in _CLIENTS if x not in want and not _TASKS.get(x)]: _CLIENTS.pop(bid, None)

async def supervisor():
    try:
        while True:
            try: await _tick()
            except asyncio.CancelledError: raise
            except Exception as e:  # noqa: BLE001
                print(f"slack: supervisor tick failed: {_safe(e)}", flush=True)
            await asyncio.sleep(SUPERVISOR_S)
    finally:
        for t in CONNS.values(): t.cancel()
        CONNS.clear()

def start() -> asyncio.Task:
    """Called from the app lifespan of every process that runs the run queue."""
    if os.environ.get("SLACK_SOCKET", "1") == "0": return asyncio.create_task(asyncio.sleep(0))
    return asyncio.create_task(supervisor())
