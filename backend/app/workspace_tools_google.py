"""GĐ11.55: the Google tools (moved out of workspace_tools.py, same behaviour). Each section belongs to one card so parallel cards edit different regions:
  GMAIL (11.56) · GOOGLE CALENDAR (11.57) · DOCS + SHEETS + DRIVE (GĐ12). Every tool is `async def fn(_c, **args)` + one `register(...)` call (docs: workspace_tools.register).
Old keys stay as aliases (google:sheets_read, google:drive_search ...). Scopes per family: office_scopes.py.
GĐ11.56 Gmail: write scope is gmail.modify only (drafts, send, labels, trash); mail is built as UTF-8 MIME (Vietnamese subjects / names = RFC 2047 / 2231), attachments come from chat files
(`<uuid>` / `/api/files/<uuid>`) or the computer (`/workspace/...`), are checked with docfiles.sniff_doc (macros refused) BEFORE any API call, <= 20 MB in total, > 3.5 MB through /upload/."""
import asyncio, base64, datetime as dt, html as _html, json, re, secrets
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, parseaddr
import workspace_tools as W
import office_scopes as OS
import office_files as OF
from workspace_tools import register, READ, DEFAULT_ASK, ALWAYS_ASK, _Err, _iso, q, _google

_VN = dt.timezone(dt.timedelta(hours=7))   # Asia/Ho_Chi_Minh has no daylight saving

# ================================================================== GMAIL (11.56)
MAX_ATT_TOTAL = 20 * 1024 * 1024      # all attachments of one mail
MAX_ATT_COUNT = 10
UPLOAD_ABOVE = 3_500_000              # raw message bytes; base64 makes the JSON body 4/3 larger, so this stays under the 5 MB mark of a plain request
_NO_LABEL_ADD = {"TRASH": "gmail_trash", "SPAM": "gmail_trash"}   # a ☆ label tool must not do what a ★ tool does

def _dec(v):
    """Header value with RFC 2047 encoded words (=?UTF-8?B?...?=) decoded to text."""
    try: return str(make_header(decode_header(v or "")))
    except Exception: return v or ""  # noqa: BLE001

def _hraw(msg, name): return next((h["value"] for h in (msg.get("payload") or {}).get("headers", []) if h.get("name", "").lower() == name.lower()), "")
def _hdr(msg, name): return _dec(_hraw(msg, name))

def _b64d(d): return base64.urlsafe_b64decode(d + "=" * (-len(d) % 4))

def _gbody(part):
    """text/plain body of a Gmail 'full' payload (falls back to text/html without tags)."""
    found = {}
    def walk(p):
        mt = p.get("mimeType", ""); d = (p.get("body") or {}).get("data")
        if d and mt in ("text/plain", "text/html") and mt not in found and not p.get("filename"): found[mt] = _b64d(d).decode("utf-8", "replace")
        for c in p.get("parts") or []: walk(c)
    walk(part or {})
    return found.get("text/plain") or _plain(found.get("text/html", ""))

def _plain(h): return re.sub(r"[ \t]+", " ", _html.unescape(re.sub(r"(?s)<(script|style).*?</\1>|<[^>]+>", " ", h or ""))).strip()

def _parts(payload):
    """Attachment parts of a Gmail payload: [{attachment_id, filename, mime_type, size, data}] (data = small inline body, normally None)."""
    out = []
    def walk(p):
        b = p.get("body") or {}
        if p.get("filename") and (b.get("attachmentId") or b.get("data")):
            out.append({"attachment_id": b.get("attachmentId"), "filename": _dec(p["filename"]), "mime_type": p.get("mimeType"), "size": int(b.get("size") or 0), "data": b.get("data")})
        for c in p.get("parts") or []: walk(c)
    walk(payload or {})
    return out

def _clean(s, cap=300):
    """One header line: no CR / LF / NUL (header injection), trimmed."""
    return re.sub(r"[\r\n\x00]+", " ", str(s or "")).strip()[:cap]

def _addrs(x, what="recipient") -> list:
    """list or 'a@b, c@d' -> clean address strings; refuses anything that is not an address (also CR / LF / NUL)."""
    items = [] if x is None else ([x] if isinstance(x, str) else list(x))
    out = []
    for it in items:
        s = str(it or "").strip()
        if not s: continue
        if re.search(r"[\r\n\x00]", s): raise _Err(f"invalid {what}: line breaks are not allowed")
        for name, a in getaddresses([s]):
            if not re.fullmatch(r"[^\s<>@,;\"']+@[^\s<>@,;\"']+\.[^\s<>@,;\"']+", a or ""): raise _Err(f"invalid {what} address: {s[:80]}")
            out.append(formataddr((name, a)) if name else a)
    if len(out) > 100: raise _Err("too many recipients (max 100)")
    return out

def _same(a, b): return parseaddr(a)[1].lower() == parseaddr(b)[1].lower()

def _dedupe(xs, drop=()):
    out = []
    for a in xs:
        if not any(_same(a, o) for o in out) and not any(_same(a, d) for d in drop): out.append(a)
    return out

# ---- attachments
async def _load_atts(refs) -> list:
    """['<chat file id>' | '/api/files/<id>' | '/workspace/...'] -> [(name, mime, bytes)]. Everything is checked before any Gmail call (GĐ12.82b: the logic lives in office_files.load)."""
    return await OF.load(refs, max_total=MAX_ATT_TOTAL, max_count=MAX_ATT_COUNT)

def _sniff(data, name) -> str:
    """-> mime. Images by magic bytes, documents by docfiles.sniff_doc (macros, mismatching extension, unsupported types are refused)."""
    return OF.sniff_attachment(data, name)

# ---- MIME
def _build(to, cc, bcc, subject, body, fmt, atts, extra=None) -> bytes:
    """UTF-8 MIME (7-bit safe): non-ASCII Subject / names / file names are RFC 2047 / 2231 encoded by the email package."""
    m = EmailMessage(policy=policy.SMTP)
    if to: m["To"] = ", ".join(to)
    if cc: m["Cc"] = ", ".join(cc)
    if bcc: m["Bcc"] = ", ".join(bcc)
    m["Subject"] = _clean(subject, 500)
    for k, v in (extra or {}).items():
        if v: m[k] = _clean(v, 4000)
    body = body or ""
    if fmt == "html": m.set_content(_plain(body) or " ", cte="base64"); m.add_alternative(body, subtype="html", cte="base64")   # base64: the message stays 7-bit clean
    else: m.set_content(body, cte="base64")
    for name, mime, data in atts:
        mt, _, st = mime.partition("/"); m.add_attachment(data, maintype=mt or "application", subtype=st or "octet-stream", filename=name)
    return m.as_bytes()

def _tag(_c, extra=None) -> dict:
    """GĐ11 F-8: every mail an agent sends / drafts carries `X-AOS-Agent: <agent id>`, so mail_triggers (11.60) never starts a run on the agent's own mail."""
    a = getattr(_c, "agent_id", None)
    return {**(extra or {}), **({"X-AOS-Agent": str(a)} if a is not None else {})}

def _fmt(f):
    if f not in ("text", "html"): raise _Err("body_format must be 'text' or 'html'")
    return f

async def _submit(_c, raw: bytes, thread_id=None, *, draft=False):
    """Send / save as draft a finished RFC 822 message. Over UPLOAD_ABOVE bytes it goes through the /upload/ endpoint (multipart/related: JSON metadata + message/rfc822)."""
    base = _google("gmail")
    tid = {"threadId": thread_id} if thread_id else {}
    if len(raw) <= UPLOAD_ABOVE:
        b64 = base64.urlsafe_b64encode(raw).decode()
        if draft: return await _c("POST", base + "gmail/v1/users/me/drafts", body={"message": {"raw": b64, **tid}})
        return await _c("POST", base + "gmail/v1/users/me/messages/send", body={"raw": b64, **tid})
    bd = "aos" + secrets.token_hex(12)
    meta = json.dumps({"message": tid} if draft else tid)
    content = (f"--{bd}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n--{bd}\r\nContent-Type: message/rfc822\r\n\r\n").encode() + raw + f"\r\n--{bd}--".encode()
    return await _c("POST", base + "upload/gmail/v1/users/me/" + ("drafts" if draft else "messages/send"), params={"uploadType": "multipart"}, content=content,
                    headers={"Content-Type": f'multipart/related; boundary="{bd}"'})

def _re(s):
    s = _clean(s, 500)
    return s if re.match(r"(?i)^\s*re\s*:", s) else "Re: " + s

async def _original(_c, message_id, fmt="metadata"):
    params = [("format", fmt)] + ([("metadataHeaders", h) for h in ("From", "To", "Cc", "Reply-To", "Subject", "Date", "Message-ID", "References")] if fmt == "metadata" else [])
    return await _c("GET", _google("gmail") + f"gmail/v1/users/me/messages/{q(message_id)}", params=params)

async def _me(_c) -> str:
    return (await _c("GET", _google("gmail") + "gmail/v1/users/me/profile")).get("emailAddress") or ""

async def _reply_ctx(_c, message_id, reply_all=False) -> dict:
    """Recipients + threading headers for a reply to `message_id`: To = Reply-To or From (the original To when I wrote it), reply_all adds everyone else but me."""
    d = await _original(_c, message_id); me = await _me(_c)
    frm, rto, to, cc = _hdr(d, "From"), _hdr(d, "Reply-To"), _hdr(d, "To"), _hdr(d, "Cc")
    people = lambda s: [formataddr((n, a)) if n else a for n, a in getaddresses([s]) if a]  # noqa: E731
    base = people(to) if (me and _same(frm, me)) else people(rto or frm)
    others = people(to) + people(cc) if reply_all else []
    mid, refs = _hdr(d, "Message-ID"), _hdr(d, "References")
    return {"to": _dedupe(base), "cc": _dedupe([a for a in others if not any(_same(a, b) for b in base)], drop=[me] if me else []), "subject": _re(_hdr(d, "Subject")),
            "in_reply_to": mid, "references": " ".join(x for x in (refs, mid) if x), "thread_id": d.get("threadId")}

def _mail_card(label):
    def d(a):
        L = [label]
        for k, t in (("to", "To"), ("cc", "Cc"), ("bcc", "Bcc")):
            if a.get(k): L.append(f"{t}: " + ", ".join(map(str, a[k])) if isinstance(a[k], (list, tuple)) else f"{t}: {a[k]}")
        if not a.get("to"):
            if a.get("message_id") or a.get("reply_to_message_id"): L.append("To: " + ("everyone on" if a.get("reply_all") else "the sender of") + " message " + str(a.get("message_id") or a.get("reply_to_message_id")))
            elif a.get("draft_id"): L.append(f"Draft: {a['draft_id']} (its recipients are the ones saved in the draft)")
        if a.get("subject"): L.append("Subject: " + _clean(a["subject"], 200))
        if a.get("body"): L.append("Text: " + _clean(a["body"], 400))
        if a.get("attachments"): at = a["attachments"] if isinstance(a["attachments"], list) else [a["attachments"]]; L.append(f"Attachments: {len(at)} (" + ", ".join(str(x)[:80] for x in at) + ")")
        if a.get("include_attachments", True) and label.startswith("Forward"): L.append("Attachments of the original message are included")
        return "\n".join(L)
    return d

# ---- F-7: what the card must show for a ★ call that only knows ids (run BEFORE the card is built, with the caller's own token, read only)
def _shown(name, addr):
    """F-9: one person on a card = `Decoded Name <address>`. The RAW header is parsed FIRST and only then are the names RFC 2047 decoded, and `< > @ " ,` are stripped from the decoded name, so an
    encoded word can neither show as `=?utf-8?b?...?=` nor smuggle a fake `<boss@company.vn>` into the line the owner approves. The address always comes from the parser, never from the name."""
    n = re.sub(r"[<>@\",;\r\n\x00]+", " ", _dec(name)); n = " ".join(n.split())[:80]
    return f"{n} <{addr}>" if n else addr

def _plist(s): return ", ".join(_shown(n, a) for n, a in getaddresses([s or ""]) if a)

def _mail_lines(to, cc, bcc, subject):
    to, cc, bcc = ([_plist(x) for x in xs] for xs in (to, cc, bcc))      # entries are raw headers / formatted addresses: decode their names for the card
    L = ["Will be sent to: " + (", ".join(to) if to else "(nobody)")]
    if cc: L.append("Cc: " + ", ".join(cc))
    if bcc: L.append("Bcc: " + ", ".join(bcc))
    L.append("Subject: " + _clean(subject, 200))
    return L

async def _look_draft(_c, draft_id, **_):
    d = await _c("GET", _google("gmail") + f"gmail/v1/users/me/drafts/{q(draft_id)}", params=[("format", "metadata")] + [("metadataHeaders", h) for h in ("To", "Cc", "Bcc", "Subject")])
    m = d.get("message") or {}
    return _mail_lines([x for x in [_hraw(m, "To")] if x], [x for x in [_hraw(m, "Cc")] if x], [x for x in [_hraw(m, "Bcc")] if x], _hdr(m, "Subject"))

async def _look_reply(_c, message_id, reply_all=False, to=None, cc=None, bcc=None, **_):
    c = await _reply_ctx(_c, message_id, bool(reply_all))
    return _mail_lines(_addrs(to) or c["to"], _addrs(cc, "cc") or c["cc"], _addrs(bcc, "bcc"), c["subject"])

async def _look_forward(_c, message_id, **_):
    d = await _original(_c, message_id)
    return ["Original message: " + _clean(_hdr(d, "Subject"), 200) + " (from " + _clean(_plist(_hraw(d, "From")) or _hdr(d, "From"), 200) + ")"]

# ---- read tools
async def gmail_search(_c, query: str = "in:inbox", max_results: int = 10):
    ids = (await _c("GET", _google("gmail") + "gmail/v1/users/me/messages", params={"q": query, "maxResults": max(1, min(int(max_results), 25))})).get("messages") or []
    out = []
    for m in ids:
        d = await _c("GET", _google("gmail") + f"gmail/v1/users/me/messages/{q(m['id'])}",
                     params=[("format", "metadata"), ("metadataHeaders", "From"), ("metadataHeaders", "Subject"), ("metadataHeaders", "Date")])
        out.append({"id": m["id"], "thread_id": d.get("threadId"), "from": _hdr(d, "From"), "subject": _hdr(d, "Subject"), "date": _hdr(d, "Date"), "snippet": d.get("snippet", "")})
    return out

async def gmail_read(_c, message_id: str):
    d = await _c("GET", _google("gmail") + f"gmail/v1/users/me/messages/{q(message_id)}", params={"format": "full"})
    return {"id": d.get("id"), "thread_id": d.get("threadId"), "from": _hdr(d, "From"), "to": _hdr(d, "To"), "cc": _hdr(d, "Cc"), "subject": _hdr(d, "Subject"), "date": _hdr(d, "Date"),
            "labels": d.get("labelIds", []), "body": _gbody(d.get("payload"))[:6000],
            "attachments": [{k: p[k] for k in ("attachment_id", "filename", "mime_type", "size")} for p in _parts(d.get("payload"))]}

async def gmail_search_threads(_c, query: str = "in:inbox", max_results: int = 10):
    ts = (await _c("GET", _google("gmail") + "gmail/v1/users/me/threads", params={"q": query, "maxResults": max(1, min(int(max_results), 20))})).get("threads") or []
    out = []
    for t in ts:
        d = await _c("GET", _google("gmail") + f"gmail/v1/users/me/threads/{q(t['id'])}",
                     params=[("format", "metadata"), ("metadataHeaders", "From"), ("metadataHeaders", "Subject"), ("metadataHeaders", "Date")])
        ms = d.get("messages") or [{}]
        out.append({"thread_id": d.get("id") or t["id"], "subject": _hdr(ms[0], "Subject"), "from": _hdr(ms[0], "From"), "last_date": _hdr(ms[-1], "Date"), "messages": len(ms),
                    "unread": any("UNREAD" in (m.get("labelIds") or []) for m in ms), "snippet": t.get("snippet", "")})
    return out

async def gmail_get_thread(_c, thread_id: str):
    d = await _c("GET", _google("gmail") + f"gmail/v1/users/me/threads/{q(thread_id)}", params={"format": "full"})
    return {"thread_id": d.get("id"), "messages": [{"id": m.get("id"), "from": _hdr(m, "From"), "to": _hdr(m, "To"), "cc": _hdr(m, "Cc"), "subject": _hdr(m, "Subject"), "date": _hdr(m, "Date"),
                                                      "labels": m.get("labelIds", []), "body": _gbody(m.get("payload"))[:2500],
                                                      "attachments": [{k: p[k] for k in ("attachment_id", "filename", "mime_type", "size")} for p in _parts(m.get("payload"))]}
                                                     for m in (d.get("messages") or [])[-12:]]}

async def gmail_list_labels(_c):
    return [{"id": x.get("id"), "name": x.get("name"), "type": x.get("type")} for x in (await _c("GET", _google("gmail") + "gmail/v1/users/me/labels")).get("labels", [])]

async def gmail_list_drafts(_c, max_results: int = 10):
    ds = (await _c("GET", _google("gmail") + "gmail/v1/users/me/drafts", params={"maxResults": max(1, min(int(max_results), 20))})).get("drafts") or []
    out = []
    for x in ds:
        mid = (x.get("message") or {}).get("id")
        d = await _c("GET", _google("gmail") + f"gmail/v1/users/me/messages/{q(mid)}", params=[("format", "metadata"), ("metadataHeaders", "To"), ("metadataHeaders", "Subject")]) if mid else {}
        out.append({"draft_id": x.get("id"), "message_id": mid, "to": _hdr(d, "To"), "subject": _hdr(d, "Subject"), "snippet": d.get("snippet", "")})
    return out

async def _fetch_part(_c, message_id, p) -> bytes:
    if p.get("attachment_id"): d = await _c("GET", _google("gmail") + f"gmail/v1/users/me/messages/{q(message_id)}/attachments/{q(p['attachment_id'])}")
    else: d = {"data": p.get("data")}
    return _b64d(d.get("data") or "")

async def gmail_get_attachment(_c, message_id: str, attachment_id: str, save_to_computer: bool = True):
    """Save a mail attachment as a chat file (the person sees / can reuse it) and, when the agent has a computer, in /workspace/uploads/."""
    d = await _c("GET", _google("gmail") + f"gmail/v1/users/me/messages/{q(message_id)}", params={"format": "full"})
    p = next((x for x in _parts(d.get("payload")) if x["attachment_id"] == attachment_id), None)
    if not p: raise _Err("attachment not found in that message (use the attachment_id listed by gmail_read / gmail_get_thread)")
    if p["size"] > MAX_ATT_TOTAL: raise _Err(f"attachment {p['filename']} is larger than 20 MB")
    data = await _fetch_part(_c, message_id, p)
    import attachments as AT, docfiles as D
    out = {"filename": p["filename"], "size": len(data), "mime_type": p["mime_type"]}
    try: f = await AT.store(data, p["filename"]); out["chat_file"] = {"id": f["id"], "url": f["url"], "name": f.get("name")}
    except Exception as e: out["chat_file_error"] = str(getattr(e, "detail", e))[:200]  # noqa: BLE001  (type not accepted as chat file: the computer copy may still work)
    if save_to_computer:
        import sbx_backend
        try: box = await sbx_backend.for_config(create=False)
        except Exception: box = None  # noqa: BLE001
        if box is not None:
            path = f"/workspace/uploads/mail_{re.sub(r'[^A-Za-z0-9]', '', message_id)[:10]}_{D.safe_name(p['filename'], 'attachment')}"
            try: await box.put(path, data); out["computer_path"] = path
            except Exception as e: out["computer_error"] = sbx_backend._file_error(e)  # noqa: BLE001
    if "chat_file" not in out and "computer_path" not in out: raise _Err(f"could not keep the attachment: {out.get('chat_file_error') or out.get('computer_error') or 'no place to save it'}")
    return out

# ---- write tools
async def gmail_draft(_c, to: list[str] | None = None, subject: str | None = None, body: str = "", cc: list[str] | None = None, bcc: list[str] | None = None,
                      body_format: str = "text", attachments: list[str] | None = None, reply_to_message_id: str | None = None):
    to_, cc_, bcc_, fmt = _addrs(to), _addrs(cc, "cc"), _addrs(bcc, "bcc"), _fmt(body_format)
    atts = await _load_atts(attachments); extra, tid = {}, None
    if reply_to_message_id:
        c = await _reply_ctx(_c, reply_to_message_id); to_ = to_ or c["to"]; cc_ = cc_ or c["cc"]; subject = subject or c["subject"]; tid = c["thread_id"]
        extra = {"In-Reply-To": c["in_reply_to"], "References": c["references"]}
    extra = _tag(_c, extra)
    if not to_: raise _Err("a draft needs at least one recipient (to)")
    d = await _submit(_c, _build(to_, cc_, bcc_, subject or "", body, fmt, atts, extra), tid, draft=True)
    return f"Draft created (id {d.get('id')}, message {(d.get('message') or {}).get('id')})."

async def gmail_send(_c, to: list[str], subject: str, body: str, cc: list[str] | None = None, bcc: list[str] | None = None, body_format: str = "text", attachments: list[str] | None = None):
    to_, cc_, bcc_, fmt = _addrs(to), _addrs(cc, "cc"), _addrs(bcc, "bcc"), _fmt(body_format)
    if not to_: raise _Err("an email needs at least one recipient (to)")
    atts = await _load_atts(attachments)
    d = await _submit(_c, _build(to_, cc_, bcc_, subject, body, fmt, atts, _tag(_c)))
    return f"Email sent (id {d.get('id')}, thread {d.get('threadId')})."

async def gmail_send_draft(_c, draft_id: str):
    d = await _c("POST", _google("gmail") + "gmail/v1/users/me/drafts/send", body={"id": draft_id})
    return f"Draft sent (message id {d.get('id')}, thread {d.get('threadId')})."

async def gmail_reply(_c, message_id: str, body: str, reply_all: bool = False, to: list[str] | None = None, cc: list[str] | None = None, bcc: list[str] | None = None,
                      body_format: str = "text", attachments: list[str] | None = None):
    to_, cc_, bcc_, fmt = _addrs(to), _addrs(cc, "cc"), _addrs(bcc, "bcc"), _fmt(body_format)
    atts = await _load_atts(attachments)
    c = await _reply_ctx(_c, message_id, bool(reply_all))
    to_ = to_ or c["to"]; cc_ = cc_ or c["cc"]
    if not to_: raise _Err("cannot work out who to reply to; pass `to`")
    d = await _submit(_c, _build(to_, cc_, bcc_, c["subject"], body, fmt, atts, _tag(_c, {"In-Reply-To": c["in_reply_to"], "References": c["references"]})), c["thread_id"])
    return f"Reply sent in thread {d.get('threadId')} (id {d.get('id')}) to {', '.join(to_ + cc_)}."

async def gmail_forward(_c, message_id: str, to: list[str], body: str = "", cc: list[str] | None = None, bcc: list[str] | None = None, include_attachments: bool = True,
                        attachments: list[str] | None = None):
    to_, cc_, bcc_ = _addrs(to), _addrs(cc, "cc"), _addrs(bcc, "bcc")
    if not to_: raise _Err("forwarding needs at least one recipient (to)")
    extra_atts = await _load_atts(attachments)
    d = await _original(_c, message_id, "full"); atts = list(extra_atts); total = sum(len(a[2]) for a in atts)
    if include_attachments:
        for p in _parts(d.get("payload")):
            if total + p["size"] > MAX_ATT_TOTAL: raise _Err("attachments are larger than 20 MB in total (try include_attachments=false)")
            data = await _fetch_part(_c, message_id, p); total += len(data)
            import docfiles as D
            atts.append((D.safe_name(p["filename"], "attachment"), _sniff(data, p["filename"]), data))
    subj = _hdr(d, "Subject"); subj = _clean(subj, 500) if re.match(r"(?i)^\s*(fwd?|fw)\s*:", subj) else "Fwd: " + _clean(subj, 500)
    head = "\n".join(["---------- Forwarded message ---------", f"From: {_hdr(d, 'From')}", f"Date: {_hdr(d, 'Date')}", f"Subject: {_hdr(d, 'Subject')}", f"To: {_hdr(d, 'To')}"]
                     + ([f"Cc: {_hdr(d, 'Cc')}"] if _hdr(d, "Cc") else []))
    text = (body + "\n\n" if body else "") + head + "\n\n" + _gbody(d.get("payload"))
    r = await _submit(_c, _build(to_, cc_, bcc_, subj, text, "text", atts, _tag(_c)))
    return f"Email forwarded (id {r.get('id')}, thread {r.get('threadId')}) to {', '.join(to_ + cc_)}."

async def _label_ids(_c, names) -> list:
    names = [str(n).strip() for n in (names or []) if str(n).strip()]
    if not names: return []
    ls = (await _c("GET", _google("gmail") + "gmail/v1/users/me/labels")).get("labels", []); out = []
    for n in names:
        hit = next((x for x in ls if x.get("id") == n or (x.get("name") or "").lower() == n.lower()), None)
        if not hit: raise _Err(f"unknown label {n[:60]!r} (see gmail_list_labels; new ones: gmail_create_label)")
        out.append(hit["id"])
    return out

async def _modify(_c, message_id, add=(), remove=()):
    return await _c("POST", _google("gmail") + f"gmail/v1/users/me/messages/{q(message_id)}/modify", body={"addLabelIds": list(add), "removeLabelIds": list(remove)})

async def gmail_label(_c, message_id: str, add: list[str] | None = None, remove: list[str] | None = None):
    a, r = await _label_ids(_c, add), await _label_ids(_c, remove)
    for x in a:
        if x in _NO_LABEL_ADD: raise _Err(f"use {_NO_LABEL_ADD[x]} (it needs the user's approval) instead of adding the label {x}")
    if not (a or r): raise _Err("nothing to do: give labels to add and / or remove")
    d = await _modify(_c, message_id, a, r)
    return f"Labels updated on {message_id}: now {', '.join(d.get('labelIds') or [])}."

async def gmail_create_label(_c, name: str):
    name = _clean(name, 200)
    if not name: raise _Err("label name is empty")
    d = await _c("POST", _google("gmail") + "gmail/v1/users/me/labels", body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"})
    return f"Label created (id {d.get('id')}, name {d.get('name')})."

async def gmail_mark_read(_c, message_id: str, read: bool = True):
    await _modify(_c, message_id, [] if read else ["UNREAD"], ["UNREAD"] if read else [])
    return f"Message {message_id} marked as {'read' if read else 'unread'}."

async def gmail_archive(_c, message_id: str):
    await _modify(_c, message_id, [], ["INBOX"])
    return f"Message {message_id} archived (out of the inbox, still searchable)."

async def gmail_trash(_c, message_id: str):
    await _c("POST", _google("gmail") + f"gmail/v1/users/me/messages/{q(message_id)}/trash")
    return f"Message {message_id} moved to the trash."

_GM = "gmail"
register("google:gmail_search", "Search the current user's Gmail (Gmail query syntax, e.g. 'from:lan newer_than:7d'; default the inbox). Returns id, thread_id, from, subject, date, snippet.",
         gmail_search, label="Search Gmail", family=_GM)
register("google:gmail_read", "Read one Gmail message of the current user by id (from gmail_search): from, to, cc, subject, date, labels, body text, attachments (attachment_id, filename, size).", gmail_read,
         label="Read a Gmail message", family=_GM)
register("google:gmail_search_threads", "Search Gmail conversations (threads) with Gmail query syntax. Returns thread_id, subject, from, last_date, number of messages, unread, snippet.", gmail_search_threads,
         label="Search Gmail conversations", family=_GM)
register("google:gmail_get_thread", "Read a whole Gmail conversation by thread_id (the last 12 messages: from, to, cc, subject, date, body, attachments).", gmail_get_thread,
         label="Read a Gmail conversation", family=_GM)
register("google:gmail_list_labels", "List the Gmail labels (system and the user's own) with their ids.", gmail_list_labels, label="List Gmail labels", family=_GM)
register("google:gmail_list_drafts", "List the user's Gmail drafts: draft_id, message_id, to, subject.", gmail_list_drafts, label="List Gmail drafts", family=_GM)
register("google:gmail_get_attachment", "Save an attachment of a Gmail message (message_id + attachment_id from gmail_read) as a chat file and, if the agent has a computer, in /workspace/uploads/. Returns the file id / path.",
         gmail_get_attachment, label="Save a Gmail attachment", family=_GM)
register("google:gmail_draft", "Create a Gmail DRAFT in the current user's mailbox (not sent). cc / bcc, body_format 'text' or 'html', attachments (chat file ids or /workspace/ paths), "
         "reply_to_message_id to draft a reply in the same conversation (to / subject then default). Needs the user's approval.", gmail_draft,
         label="Create a Gmail draft", family=_GM, tier=DEFAULT_ASK, describe=_mail_card("Create a Gmail draft"))
register("google:gmail_send", "SEND a new email from the current user's Gmail (to, cc, bcc, subject, body, body_format 'text'|'html', attachments = chat file ids or /workspace/ paths, max 20 MB). "
         "Needs the user's approval before it runs.", gmail_send, label="Send Gmail", family=_GM, tier=ALWAYS_ASK, describe=_mail_card("Send an email"))
register("google:gmail_send_draft", "SEND an existing Gmail draft by draft_id (from gmail_draft / gmail_list_drafts). Needs the user's approval before it runs.", gmail_send_draft,
         label="Send a Gmail draft", family=_GM, tier=ALWAYS_ASK, describe=_mail_card("Send a saved draft"), lookup=_look_draft)
register("google:gmail_reply", "REPLY to a Gmail message in its conversation (keeps the thread; reply_all=true also answers everyone else). Optional to/cc/bcc override, attachments. Needs the user's approval.", gmail_reply,
         label="Reply to a Gmail message", family=_GM, tier=ALWAYS_ASK, describe=_mail_card("Reply to an email"), lookup=_look_reply)
register("google:gmail_forward", "FORWARD a Gmail message to new recipients with an optional note; the original attachments are included unless include_attachments=false. Needs the user's approval.", gmail_forward,
         label="Forward a Gmail message", family=_GM, tier=ALWAYS_ASK, describe=_mail_card("Forward an email"), lookup=_look_forward)
register("google:gmail_label", "Add and / or remove labels (names or ids) on one Gmail message. Cannot trash (use gmail_trash). Needs the user's approval unless an admin allowed it.", gmail_label,
         label="Label a Gmail message", family=_GM, tier=DEFAULT_ASK)
register("google:gmail_create_label", "Create a new Gmail label. Needs the user's approval unless an admin allowed it.", gmail_create_label, label="Create a Gmail label", family=_GM, tier=DEFAULT_ASK)
register("google:gmail_mark_read", "Mark a Gmail message as read (or unread with read=false). Needs the user's approval unless an admin allowed it.", gmail_mark_read,
         label="Mark a Gmail message read", family=_GM, tier=DEFAULT_ASK)
register("google:gmail_archive", "Archive a Gmail message (remove it from the inbox; it stays searchable). Needs the user's approval unless an admin allowed it.", gmail_archive,
         label="Archive a Gmail message", family=_GM, tier=DEFAULT_ASK)
register("google:gmail_trash", "Move a Gmail message to the trash. Needs the user's approval before it runs.", gmail_trash, label="Trash a Gmail message", family=_GM, tier=ALWAYS_ASK)

# ================================================================== GOOGLE CALENDAR (11.57)
_CAL = "calendar"
_RESP = {"accepted": "accepted", "declined": "declined", "tentative": "tentative"}

def _ev(e) -> dict:
    s, n = e.get("start") or {}, e.get("end") or {}
    out = {"id": e.get("id"), "summary": e.get("summary"), "start": s.get("dateTime") or s.get("date"), "end": n.get("dateTime") or n.get("date"), "location": e.get("location"), "link": e.get("htmlLink")}
    if e.get("attendees"): out["attendees"] = [{"email": a.get("email"), "status": a.get("responseStatus"), **({"me": True} if a.get("self") else {})} for a in e["attendees"]]
    if e.get("organizer"): out["organizer"] = e["organizer"].get("email")
    return out

def _updates(v):
    if v not in ("all", "externalOnly", "none"): raise _Err("send_updates must be 'all', 'externalOnly' or 'none'")
    return v

def _cal_path(calendar_id): return _google("calendar") + f"calendar/v3/calendars/{q(calendar_id or 'primary')}"

async def gcal_list_calendars(_c):
    d = await _c("GET", _google("calendar") + "calendar/v3/users/me/calendarList")
    return [{"id": x.get("id"), "name": x.get("summary"), "timezone": x.get("timeZone"), "primary": bool(x.get("primary") or x.get("id") == "primary"), "access": x.get("accessRole")} for x in d.get("items", [])]

async def gcal_list(_c, start: str | None = None, days: int = 7, query: str | None = None, calendar_id: str = "primary"):
    t0 = dt.datetime.fromisoformat(_iso(start, dt.datetime.now(_VN)))
    p = {"timeMin": t0.isoformat(), "timeMax": (t0 + dt.timedelta(days=max(1, min(int(days), 62)))).isoformat(), "singleEvents": "true", "orderBy": "startTime", "maxResults": 50}
    if query: p["q"] = query
    d = await _c("GET", _cal_path(calendar_id) + "/events", params=p)
    return [_ev(e) for e in d.get("items", [])]

async def gcal_get(_c, event_id: str, calendar_id: str = "primary"):
    return _ev(await _c("GET", _cal_path(calendar_id) + f"/events/{q(event_id)}"))

def _parse_busy(s):
    x = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return x if x.tzinfo else x.replace(tzinfo=_VN)

async def _freebusy(_c, ids, t0, t1):
    """-> ({calendar id: [(start, end)]}, [ids whose calendar could not be read])."""
    d = await _c("POST", _google("calendar") + "calendar/v3/freeBusy", body={"timeMin": t0.isoformat(), "timeMax": t1.isoformat(), "timeZone": "Asia/Ho_Chi_Minh", "items": [{"id": i} for i in ids]})
    busy, bad = {}, []
    for cid, v in (d.get("calendars") or {}).items():
        if v.get("errors"): bad.append(cid)
        busy[cid] = [(_parse_busy(b["start"]), _parse_busy(b["end"])) for b in v.get("busy", [])]
    return busy, bad

def _ids(emails) -> list:
    out = []
    for e in ["primary"] + _addrs(emails, "attendee"):
        a = parseaddr(e)[1] or e
        if a not in out: out.append(a)
    if len(out) > 20: raise _Err("too many calendars (max 20 people)")
    return out

async def gcal_free_busy(_c, emails: list[str] | None = None, start: str | None = None, days: int = 1):
    """Busy intervals of the user (primary) and of other people / calendars (their email) between `start` and start + days."""
    t0 = dt.datetime.fromisoformat(_iso(start, dt.datetime.now(_VN))); t1 = t0 + dt.timedelta(days=max(1, min(int(days), 31)))
    busy, bad = await _freebusy(_c, _ids(emails), t0, t1)
    return {"from": t0.isoformat(), "to": t1.isoformat(), "busy": {c: [{"start": a.isoformat(), "end": b.isoformat()} for a, b in v] for c, v in busy.items() if c not in bad},
            "not_visible": bad}

def _up(x, step):
    x = x.astimezone(_VN); m = x.hour * 60 + x.minute + (1 if (x.second or x.microsecond) else 0); m = -(-m // step) * step
    return dt.datetime.combine(x.date(), dt.time(0), tzinfo=_VN) + dt.timedelta(minutes=m)

def suggest_slots(busy, t0, t1, minutes, work_start=(8, 0), work_end=(17, 30), step=30, weekends=False, limit=3) -> list:
    """Free slots of `minutes` where EVERY given busy interval [(start, end)] is avoided, only inside the working hours (VN time) between t0 and t1, on a `step`-minute grid, earliest first."""
    iv = sorted(busy); out = []; day = t0.astimezone(_VN).date(); last = t1.astimezone(_VN).date(); dur = dt.timedelta(minutes=minutes)
    while day <= last and len(out) < limit:
        if weekends or day.weekday() < 5:
            lo = max(dt.datetime.combine(day, dt.time(*work_start), tzinfo=_VN), t0); hi = min(dt.datetime.combine(day, dt.time(*work_end), tzinfo=_VN), t1)
            cur = _up(lo, step)
            while cur + dur <= hi and len(out) < limit:
                end = cur + dur; clash = next((b for b in iv if b[0] < end and b[1] > cur), None)
                if clash: cur = _up(max(clash[1], cur + dt.timedelta(minutes=1)), step); continue
                out.append((cur, end)); cur = _up(end, step)
        day += dt.timedelta(days=1)
    return out

def _hm(s, what):
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", str(s).strip())
    if not m: raise _Err(f"{what} must look like 08:30")
    return int(m.group(1)), int(m.group(2))

async def gcal_suggest_time(_c, attendees: list[str] | None = None, duration_minutes: int = 30, start: str | None = None, days: int = 7, work_start: str = "08:00", work_end: str = "17:30",
                            include_weekends: bool = False, max_suggestions: int = 3):
    """Meeting times when the user AND all attendees are free, inside working hours in Vietnam time (Asia/Ho_Chi_Minh, UTC+7)."""
    dur = int(duration_minutes)
    if not 5 <= dur <= 480: raise _Err("duration_minutes must be between 5 and 480")
    ws, we = _hm(work_start, "work_start"), _hm(work_end, "work_end")
    if ws >= we: raise _Err("work_end must be after work_start")
    t0 = dt.datetime.fromisoformat(_iso(start, dt.datetime.now(_VN))); t1 = t0 + dt.timedelta(days=max(1, min(int(days), 31)))
    busy, bad = await _freebusy(_c, _ids(attendees), t0, t1)
    slots = suggest_slots([b for c, v in busy.items() if c not in bad for b in v], t0, t1, dur, ws, we, 30, bool(include_weekends), max(1, min(int(max_suggestions), 10)))
    return {"timezone": "Asia/Ho_Chi_Minh", "suggestions": [{"start": a.isoformat(), "end": b.isoformat()} for a, b in slots], "not_visible": bad,
            **({} if slots else {"note": "no common free time in that range and working hours; widen the range"}),
            **({"warning": "these calendars could not be read, so their owners' busy times are NOT considered: " + ", ".join(bad)} if bad else {})}

async def gcal_create(_c, summary: str, start: str, end: str, timezone: str = "Asia/Ho_Chi_Minh", attendees: list[str] | None = None,
                      location: str | None = None, description: str | None = None, calendar_id: str = "primary", send_updates: str = "all"):
    b = {"summary": summary, "start": {"dateTime": _iso(start, None), "timeZone": timezone}, "end": {"dateTime": _iso(end, None), "timeZone": timezone}}
    guests = _addrs(attendees, "attendee")
    if guests: b["attendees"] = [{"email": a} for a in guests]
    if location: b["location"] = location
    if description: b["description"] = description
    d = await _c("POST", _cal_path(calendar_id) + "/events", body=b, params={"sendUpdates": _updates(send_updates)} if guests else None)
    return f"Event created: {d.get('htmlLink') or d.get('id')}" + (f" (invitations sent to {', '.join(guests)})" if guests and send_updates != "none" else "")

async def gcal_update(_c, event_id: str, summary: str | None = None, start: str | None = None, end: str | None = None, timezone: str = "Asia/Ho_Chi_Minh",
                      attendees: list[str] | None = None, location: str | None = None, description: str | None = None, calendar_id: str = "primary", send_updates: str = "all"):
    """Change fields of an event (only the given ones). `attendees`, if given, REPLACES the guest list."""
    b = {}
    if summary is not None: b["summary"] = summary
    if start: b["start"] = {"dateTime": _iso(start, None), "timeZone": timezone}
    if end: b["end"] = {"dateTime": _iso(end, None), "timeZone": timezone}
    if attendees is not None: b["attendees"] = [{"email": a} for a in _addrs(attendees, "attendee")]
    if location is not None: b["location"] = location
    if description is not None: b["description"] = description
    if not b: raise _Err("nothing to change: give at least one field")
    d = await _c("PATCH", _cal_path(calendar_id) + f"/events/{q(event_id)}", body=b, params={"sendUpdates": _updates(send_updates)})
    return f"Event updated: {d.get('htmlLink') or d.get('id')}"

async def gcal_delete(_c, event_id: str, calendar_id: str = "primary", send_updates: str = "all"):
    await _c("DELETE", _cal_path(calendar_id) + f"/events/{q(event_id)}", params={"sendUpdates": _updates(send_updates)})
    return f"Event {event_id} deleted."

async def gcal_respond(_c, event_id: str, response: str, comment: str | None = None, calendar_id: str = "primary", send_updates: str = "all"):
    """Answer an invitation: response = accepted | declined | tentative (changes the user's own entry in the guest list)."""
    if response not in _RESP: raise _Err("response must be 'accepted', 'declined' or 'tentative'")
    ev = await _c("GET", _cal_path(calendar_id) + f"/events/{q(event_id)}"); guests = ev.get("attendees") or []
    mine = next((a for a in guests if a.get("self")), None)
    if not mine: raise _Err("you are not on the guest list of this event, so there is nothing to answer")
    mine["responseStatus"] = _RESP[response]
    if comment: mine["comment"] = str(comment)[:500]
    await _c("PATCH", _cal_path(calendar_id) + f"/events/{q(event_id)}", body={"attendees": guests}, params={"sendUpdates": _updates(send_updates)})
    return f"Answered '{response}' to {ev.get('summary') or event_id}."

async def _look_event(_c, event_id, calendar_id="primary", **_):
    e = await _c("GET", _cal_path(calendar_id) + f"/events/{q(event_id)}"); v = _ev(e); g = e.get("attendees") or []
    L = ["Event: " + _clean(v.get("summary"), 200) + f" ({v.get('start')} - {v.get('end')})"]
    if v.get("organizer"): L.append("Organizer: " + _clean(v["organizer"], 200))
    L.append(f"Guests on the event now ({len(g)}): " + ", ".join(_clean(a.get("email"), 100) for a in g[:30]) + (f" … (+{len(g) - 30} more)" if len(g) > 30 else "") if g else "No guests: only the user's own calendar")
    return L

def _cal_card(label, note=""):
    def d(a):
        L = [label]
        for k, t in (("summary", "Event"), ("event_id", "Event id"), ("start", "Start"), ("end", "End"), ("response", "Answer"), ("location", "Where")):
            if a.get(k): L.append(f"{t}: {str(a[k])[:200]}")
        if a.get("attendees"): L.append("These people will receive an invitation email: " + ", ".join(map(str, a["attendees"])))
        elif note: L.append(note)
        if a.get("send_updates") == "none": L.append("No email is sent to guests (send_updates=none)")
        return "\n".join(L)
    return d

register("google:gcal_list_calendars", "List the calendars the current user can see (id, name, timezone, access).", gcal_list_calendars, label="List Google calendars", family=_CAL,
         scopes=(OS.G + "calendar.calendarlist.readonly",))
register("google:gcal_list", "List events of a Google Calendar (default the primary one; calendar_id from gcal_list_calendars) from `start` (ISO 8601, default now) for `days` days, optional text query.", gcal_list,
         label="List Google Calendar events", family=_CAL)
register("google:gcal_get", "Read one Google Calendar event by id: time, location, organizer, guests and their answers.", gcal_get, label="Read a Google Calendar event", family=_CAL)
register("google:gcal_free_busy", "Busy times of the user and of other people (their email addresses) between `start` and start + `days`; people whose calendar is not shared are listed in not_visible.",
         gcal_free_busy, label="Check free/busy times", family=_CAL, scopes=(OS.G + "calendar.freebusy",))
register("google:gcal_suggest_time", "Suggest meeting times when the user and all `attendees` (emails) are free for `duration_minutes`, inside working hours (default 08:00-17:30 Mon-Fri, Vietnam time).",
         gcal_suggest_time, label="Suggest a meeting time", family=_CAL, scopes=(OS.G + "calendar.freebusy",))
register("google:gcal_create", "Create an event in a Google Calendar (start/end ISO 8601). Guests (attendees) get an invitation email, so an event with guests always needs the user's approval; without guests it needs approval unless an admin allowed it.",
         gcal_create, label="Create a Google Calendar event", family=_CAL, tier=DEFAULT_ASK, tier_fn=W.ask_if_attendees,   # with guests = invitations go out = ★
         describe=_cal_card("Create a calendar event", "Only the user's own calendar (no guests)"))
register("google:gcal_update", "Change an event (only the fields you give; `attendees` replaces the guest list). Guests get an update email unless send_updates='none'. Needs the user's approval.", gcal_update,
         label="Update a Google Calendar event", family=_CAL, tier=ALWAYS_ASK, describe=_cal_card("Update a calendar event", "Everyone on the event's guest list is emailed about the change"), lookup=_look_event)
register("google:gcal_delete", "Delete an event; guests get a cancellation email unless send_updates='none'. Needs the user's approval.", gcal_delete,
         label="Delete a Google Calendar event", family=_CAL, tier=ALWAYS_ASK, describe=_cal_card("Delete a calendar event", "Everyone on the event's guest list is emailed a cancellation"), lookup=_look_event)
register("google:gcal_respond", "Answer an invitation: response = accepted | declined | tentative (optional comment). The organizer is notified. Needs the user's approval.", gcal_respond,
         label="Answer a calendar invitation", family=_CAL, tier=ALWAYS_ASK, describe=_cal_card("Answer a calendar invitation", "The organizer is emailed your answer"), lookup=_look_event)

# ================================================================== DOCS + SHEETS + DRIVE (GĐ12)
# ---- Google Docs (12.81)
_DOC_URL = "https://docs.google.com/document/d/{}/edit"
_SHEET_URL = "https://docs.google.com/spreadsheets/d/{}/edit"
range_ = range   # tools with a parameter called `range` (A1 notation) cannot use the builtin inside

async def _gx(coro, what):
    """Run one API call; a 404 becomes a plain sentence (Google answers 404 both for a wrong id and for somebody else's file)."""
    try: return await coro
    except _Err as e:
        if str(e).startswith("HTTP 404"): raise _Err(f"{what} not found, or the connected account has no access to it — check the id") from None
        raise

def _doc_paras(doc) -> list:
    """[(style, text)] of a Docs document resource (one entry per paragraph, text without the final newline)."""
    out = []
    for el in (doc.get("body") or {}).get("content") or []:
        p = el.get("paragraph")
        if not p: continue
        out.append(((p.get("paragraphStyle") or {}).get("namedStyleType", "NORMAL_TEXT"), "".join((e.get("textRun") or {}).get("content", "") for e in p.get("elements") or []).rstrip("\n")))
    return out

async def _get_doc(_c, document_id):
    return await _gx(_c("GET", _google("docs") + f"v1/documents/{q(document_id)}"), "Google Doc")

async def gdocs_read(_c, document_id: str):
    d = await _get_doc(_c, document_id); ps = _doc_paras(d)
    text = "\n".join(t for _s, t in ps).strip("\n")
    return {"id": d.get("documentId") or document_id, "title": d.get("title"), "headings": [t for s, t in ps if (s.startswith("HEADING") or s == "TITLE") and t][:50],
            "text": text[:6000], **({"truncated": True} if len(text) > 6000 else {})}

def _need_text(text, what="text"):
    if not isinstance(text, str) or not text.strip(): raise _Err(f"{what} must not be empty")
    return text

async def gdocs_create(_c, title: str, text: str = ""):
    title = _clean(title, 300)
    if not title: raise _Err("a document needs a title")
    d = await _c("POST", _google("docs") + "v1/documents", body={"title": title}); did = d.get("documentId")
    if text.strip():
        try: await _c("POST", _google("docs") + f"v1/documents/{q(did)}:batchUpdate", body={"requests": [{"insertText": {"location": {"index": 1}, "text": text}}]})
        except _Err as e: raise _Err(f"the document was created (id {did}, {_DOC_URL.format(did)}) but its text could not be written: {e}") from None
    return f"Created Google Doc '{title}' (id {did}, link {_DOC_URL.format(did)})."

async def gdocs_append(_c, document_id: str, text: str):
    _need_text(text); d = await _get_doc(_c, document_id)
    empty = not any(t.strip() for _s, t in _doc_paras(d))
    await _gx(_c("POST", _google("docs") + f"v1/documents/{q(document_id)}:batchUpdate",
                 body={"requests": [{"insertText": {"endOfSegmentLocation": {}, "text": text if empty else "\n" + text}}]}), "Google Doc")
    return f"Appended {len(text)} characters to '{d.get('title')}'."

async def gdocs_edit(_c, document_id: str, find: str, replace: str = "", match_case: bool = True):
    _need_text(find, "find")
    d = await _gx(_c("POST", _google("docs") + f"v1/documents/{q(document_id)}:batchUpdate",
                     body={"requests": [{"replaceAllText": {"containsText": {"text": find, "matchCase": bool(match_case)}, "replaceText": replace}}]}), "Google Doc")
    n = int((((d.get("replies") or [{}])[0]).get("replaceAllText") or {}).get("occurrencesChanged") or 0)
    if not n: return "No match found: nothing was changed."
    return f"Replaced {n} occurrence(s)." if replace else f"Deleted {n} occurrence(s)."

async def _look_doc_edit(_c, document_id=None, find="", replace="", **_):
    """Approval-card facts for gdocs_edit: the real title and the paragraphs that would change (the model only knows an id)."""
    d = await _get_doc(_c, str(document_id)); ps = [t for _s, t in _doc_paras(d)]
    hit = [t for t in ps if find and str(find).lower() in t.lower()]
    return [f"Document: {d.get('title')}", f"Paragraphs that match: {len(hit)}" + ("" if hit else " — nothing would change")] + [f"  • {t[:200]}" for t in hit[:4]]

def _edit_card(a):
    f, r = str(a.get("find", ""))[:200], str(a.get("replace", ""))[:200]
    return f"Edit a Google Doc (gdocs_edit)\nDocument id: {str(a.get('document_id', ''))[:80]}\n" + (f"Replace: “{f}” → “{r}”" if r else f"Delete the text: “{f}”")

# ---- Google Sheets (12.81)
def _rows(values, cap=1000):
    if not isinstance(values, list) or not values or not all(isinstance(r, list) for r in values): raise _Err("values must be a non-empty list of rows, each row a list of cells")
    if len(values) > cap or any(len(r) > 100 for r in values): raise _Err(f"too many cells (max {cap} rows of 100 columns)")
    return values

async def gsheets_get(_c, spreadsheet_id: str):
    d = await _gx(_c("GET", _google("sheets") + f"v4/spreadsheets/{q(spreadsheet_id)}", params={"fields": "spreadsheetId,properties.title,spreadsheetUrl,sheets.properties"}), "Google Sheet")
    return {"id": d.get("spreadsheetId") or spreadsheet_id, "title": (d.get("properties") or {}).get("title"), "url": d.get("spreadsheetUrl"),
            "sheets": [{"title": p.get("title"), "rows": (p.get("gridProperties") or {}).get("rowCount"), "columns": (p.get("gridProperties") or {}).get("columnCount")}
                       for p in ((s.get("properties") or {}) for s in d.get("sheets") or [])]}

async def gsheets_get_values(_c, spreadsheet_id: str, ranges: list[str]):
    if isinstance(ranges, str): ranges = [ranges]
    if not ranges or len(ranges) > 20: raise _Err("give 1 to 20 ranges in A1 notation, e.g. ['Sheet1!A1:D20']")
    d = await _gx(_c("GET", _google("sheets") + f"v4/spreadsheets/{q(spreadsheet_id)}/values:batchGet", params=[("ranges", r) for r in ranges]), "Google Sheet")
    return [{"range": v.get("range"), "values": v.get("values", [])} for v in d.get("valueRanges") or []]

async def gsheets_append_rows(_c, spreadsheet_id: str, range: str, values: list[list]):
    _rows(values)
    d = await _gx(_c("POST", _google("sheets") + f"v4/spreadsheets/{q(spreadsheet_id)}/values/{q(range)}:append", params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
                     body={"values": values}), "Google Sheet")
    u = d.get("updates") or {}
    return f"Appended {u.get('updatedRows', len(values))} row(s) at {u.get('updatedRange')}."

async def gsheets_create(_c, title: str, sheet_names: list[str] | None = None):
    title = _clean(title, 300)
    if not title: raise _Err("a spreadsheet needs a title")
    names = [_clean(n, 100) for n in (sheet_names or []) if _clean(n, 100)][:20]
    d = await _c("POST", _google("sheets") + "v4/spreadsheets", body={"properties": {"title": title}, **({"sheets": [{"properties": {"title": n}} for n in names]} if names else {})})
    sid = d.get("spreadsheetId")
    return f"Created Google Sheet '{title}' (id {sid}, link {_SHEET_URL.format(sid)}; tabs: {', '.join(s['properties']['title'] for s in d.get('sheets') or [])})."

async def gsheets_add_sheet(_c, spreadsheet_id: str, title: str):
    title = _clean(title, 100)
    if not title: raise _Err("the new tab needs a name")
    await _gx(_c("POST", _google("sheets") + f"v4/spreadsheets/{q(spreadsheet_id)}:batchUpdate", body={"requests": [{"addSheet": {"properties": {"title": title}}}]}), "Google Sheet")
    return f"Added the tab '{title}'."

_NUM = re.compile(r"^[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?$")
def _num(x):
    """cell -> Decimal | None (blank) | False (text). Plain numbers and 1,234.5 style thousands; nothing else is guessed."""
    from decimal import Decimal
    if isinstance(x, bool): return False
    if isinstance(x, (int, float)): return Decimal(str(x))
    s = str(x if x is not None else "").strip()
    if not s: return None
    return Decimal(s.replace(",", "")) if _NUM.match(s) else False

def _plain_num(d):
    from decimal import Decimal
    return int(d) if d == d.to_integral_value() else float(d.quantize(Decimal("1e-10")).normalize())

async def gsheets_summarize(_c, spreadsheet_id: str, range: str, has_header: bool = True):
    from decimal import Decimal
    d = await _gx(_c("GET", _google("sheets") + f"v4/spreadsheets/{q(spreadsheet_id)}/values/{q(range)}"), "Google Sheet")
    rows = d.get("values") or []
    if not rows: return {"range": d.get("range"), "rows": 0, "columns": []}
    head, body = (rows[0], rows[1:]) if has_header else ([f"column {i + 1}" for i in range_(max(len(r) for r in rows))], rows)
    out, skipped = [], []
    for i in range_(max([len(head)] + [len(r) for r in body])):
        vals = [_num(r[i] if i < len(r) else None) for r in body]; nums = [v for v in vals if v is not None and v is not False]
        name = str(head[i]) if i < len(head) else f"column {i + 1}"
        if not nums: continue
        if any(v is False for v in vals): skipped.append(name); continue
        s = sum(nums, Decimal(0))
        out.append({"column": name, "count": len(nums), "sum": _plain_num(s), "min": _plain_num(min(nums)), "max": _plain_num(max(nums)), "average": _plain_num(s / len(nums))})
    return {"range": d.get("range"), "rows": len(body), "columns": out, **({"skipped_mixed_columns": skipped} if skipped else {})}

# ---- Google Drive: get, download, upload, folder (12.82)
MAX_DRIVE_FILE = OF.MAX_UPLOAD          # 20 MB both ways
_FOLDER = "application/vnd.google-apps.folder"
_GDOC, _GSHEET = "application/vnd.google-apps.document", "application/vnd.google-apps.spreadsheet"
_EXPORTS = {"docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "pdf": "application/pdf", "csv": "text/csv", "txt": "text/plain"}
_EXPORT_KINDS = {_GDOC: ("docx", "pdf", "txt"), _GSHEET: ("xlsx", "pdf", "csv")}   # first = default; other Google files (slides, drawings) only as pdf

async def gdrive_get(_c, file_id: str):
    m = await _gx(_c("GET", _google("drive") + f"drive/v3/files/{q(file_id)}", params={"fields": "id,name,mimeType,size,modifiedTime,webViewLink,parents"}), "Google Drive file")
    return {k: m.get(k) for k in ("id", "name", "mimeType", "size", "modifiedTime", "webViewLink", "parents") if m.get(k) is not None}

async def _keep(data: bytes, fname: str, prefix: str, save_to_computer: bool) -> dict:
    """A downloaded file -> a chat file (the person can open it) and, if asked, /workspace/uploads/ on the agent's computer (same pattern as gmail_get_attachment)."""
    import attachments as AT, docfiles as D
    out = {"filename": fname, "size": len(data)}
    try: f = await AT.store(data, fname); out["chat_file"] = {"id": f["id"], "url": f["url"], "name": f.get("name")}
    except Exception as e: out["chat_file_error"] = str(getattr(e, "detail", e))[:200]  # noqa: BLE001  (type not accepted as a chat file: the computer copy may still work)
    if save_to_computer:
        import sbx_backend
        try: box = await sbx_backend.for_config(create=False)
        except Exception: box = None  # noqa: BLE001
        if box is not None:
            path = f"/workspace/uploads/{prefix}_{D.safe_name(fname, 'file')}"
            try: await box.put(path, data); out["computer_path"] = path
            except Exception as e: out["computer_error"] = sbx_backend._file_error(e)  # noqa: BLE001
    if "chat_file" not in out and "computer_path" not in out: raise _Err(f"could not keep the file: {out.get('chat_file_error') or out.get('computer_error') or 'no place to save it'}")
    return out

async def gdrive_download(_c, file_id: str, export_as: str | None = None, save_to_computer: bool = True):
    base = _google("drive") + f"drive/v3/files/{q(file_id)}"
    m = await _gx(_c("GET", base, params={"fields": "id,name,mimeType,size"}), "Google Drive file"); mt = m.get("mimeType", ""); name = m.get("name") or "file"
    if mt == _FOLDER: raise _Err("that is a folder: search inside it with gdrive_search")
    if mt.startswith("application/vnd.google-apps."):   # Google Doc / Sheet / ...: exported, not downloaded
        kinds = _EXPORT_KINDS.get(mt, ("pdf",)); ext = (export_as or kinds[0]).lower().lstrip(".")
        if ext not in kinds: raise _Err(f"this Google file can be exported as: {', '.join(kinds)}")
        data = await _c("GET", base + "/export", params={"mimeType": _EXPORTS[ext]}, raw=True)
        fname = name if name.lower().endswith("." + ext) else f"{name}.{ext}"
    else:
        if int(m.get("size") or 0) > MAX_DRIVE_FILE: raise _Err(f"{name} is larger than 20 MB: it was not downloaded")
        data = await _c("GET", base, params={"alt": "media"}, raw=True); fname = name
    if len(data) > MAX_DRIVE_FILE: raise _Err(f"{name} is larger than 20 MB: it was not kept")
    out = await _keep(data, fname, "drive_" + re.sub(r"[^A-Za-z0-9]", "", file_id)[:10], bool(save_to_computer))
    return {**out, "mime_type": mt, **({"exported_as": ext} if mt.startswith("application/vnd.google-apps.") else {})}

async def gdrive_upload(_c, file: str, name: str | None = None, folder_id: str | None = None):
    # everything is checked BEFORE any Drive call: macros, hostile content, wrong extension, > 20 MB (office_files.prepare_upload)
    fname, mime, data = await OF.prepare_upload(file, name)
    meta = {"name": fname, **({"parents": [str(folder_id)]} if folder_id else {})}
    bd = "aos" + secrets.token_hex(16)
    body = (f"--{bd}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta, ensure_ascii=False)}\r\n--{bd}\r\nContent-Type: {mime}\r\n\r\n").encode("utf-8") + data + f"\r\n--{bd}--".encode()
    d = await _gx(_c("POST", _google("drive") + "upload/drive/v3/files", params={"uploadType": "multipart", "fields": "id,name,mimeType,size,webViewLink"}, content=body,
                     headers={"Content-Type": f'multipart/related; boundary="{bd}"'}), "Target folder")
    return f"Uploaded '{d.get('name')}' ({len(data)} bytes) as a NEW Drive file (id {d.get('id')}, link {d.get('webViewLink')}). Nothing existing was replaced."

def _upload_card(a):
    f = str(a.get("file") or "").strip(); name = a.get("name")
    if not name and f.startswith("/workspace/"): name = f.rsplit("/", 1)[-1]
    if f.startswith("/workspace/") or re.fullmatch(r"(?:file:|/api/files/)?[0-9a-fA-F-]{36}", f): size = "checked just before the upload"
    else: size = f"about {len(re.sub(r'[^A-Za-z0-9+/_-]', '', f)) * 3 // 4:,} bytes"
    return (f"Upload a file to Google Drive (gdrive_upload)\nFile name: {str(name or '(from the file)')[:200]}\nSource: {f[:80] if len(f) < 100 else 'file content (base64)'}\nSize: {size}\n"
            f"Folder: {str(a.get('folder_id'))[:80] if a.get('folder_id') else 'My Drive (top level)'}\nA NEW file is created; a file with the same name is not touched.")

async def gdrive_create_folder(_c, name: str, parent_id: str | None = None):
    name = _clean(name, 200)
    if not name: raise _Err("a folder needs a name")
    d = await _gx(_c("POST", _google("drive") + "drive/v3/files", params={"fields": "id,name,webViewLink"}, body={"name": name, "mimeType": _FOLDER, **({"parents": [str(parent_id)]} if parent_id else {})}), "Parent folder")
    return f"Created folder '{d.get('name')}' (id {d.get('id')})."

async def gsheets_read(_c, spreadsheet_id: str, range: str):
    d = await _c("GET", _google("sheets") + f"v4/spreadsheets/{q(spreadsheet_id)}/values/{q(range)}")
    return {"range": d.get("range"), "values": d.get("values", [])}

async def gsheets_update(_c, spreadsheet_id: str, range: str, values: list[list]):
    d = await _c("PUT", _google("sheets") + f"v4/spreadsheets/{q(spreadsheet_id)}/values/{q(range)}", params={"valueInputOption": "USER_ENTERED"}, body={"values": values})
    return f"Updated {d.get('updatedCells', 0)} cells in {d.get('updatedRange')}."

async def gdrive_search(_c, query: str, max_results: int = 10):
    t = query.replace("\\", "\\\\").replace("'", "\\'")
    d = await _c("GET", _google("drive") + "drive/v3/files", params={"q": f"(name contains '{t}' or fullText contains '{t}') and trashed = false",
                                                                      "pageSize": max(1, min(int(max_results), 50)), "fields": "files(id,name,mimeType,modifiedTime,webViewLink)"})
    return d.get("files", [])

_EXPORT = {"application/vnd.google-apps.document": "text/plain", "application/vnd.google-apps.spreadsheet": "text/csv", "application/vnd.google-apps.presentation": "text/plain"}
async def gdrive_read(_c, file_id: str):
    base = _google("drive") + f"drive/v3/files/{q(file_id)}"
    meta = await _c("GET", base, params={"fields": "id,name,mimeType,size"}); mt = meta.get("mimeType", "")
    if mt in _EXPORT: txt = await _c("GET", base + "/export", params={"mimeType": _EXPORT[mt]}, text=True)
    elif mt.startswith("text/") or mt in ("application/json", "application/csv"):
        if int(meta.get("size") or 0) > 2_000_000: return f"{meta.get('name')}: file too large to read as text"
        txt = await _c("GET", base, params={"alt": "media"}, text=True)
    else: return f"{meta.get('name')} ({mt}): not a text file — open it in Drive"
    return {"name": meta.get("name"), "mimeType": mt, "text": txt[:6000]}

register("google:gsheets_read", "Read a range of a Google Sheet the current user can access (range in A1 notation, e.g. 'Sheet1!A1:D20').", gsheets_read,
         label="Read a Google Sheet", family="sheets", aliases=("google:sheets_read",))
register("google:gsheets_update", "Write values (2-D list) into a Google Sheet range. Needs the user's approval.", gsheets_update,
         label="Update a Google Sheet", family="sheets", tier=ALWAYS_ASK, aliases=("google:sheets_update",))   # overwrites cells that already exist = ★
register("google:gdrive_search", "Search the current user's Google Drive by file name / content. Returns id, name, mimeType, link.", gdrive_search,
         label="Search Google Drive", family="drive", aliases=("google:drive_search",))
register("google:gdrive_read", "Read the text of a Google Drive file by id (Google Docs/Sheets are exported as text/CSV; binary files are not read).", gdrive_read,
         label="Read a Google Drive file", family="drive", aliases=("google:drive_read",))
register("google:gdocs_read", "Read a Google Doc by id: title, headings and the text (first 6,000 characters).", gdocs_read, label="Read a Google Doc", family="docs")
register("google:gdocs_create", "Create a new Google Doc in the user's Drive with a title and optional starting text. Needs the user's approval unless an admin allowed it to run automatically.", gdocs_create,
         label="Create a Google Doc", family="docs", tier=DEFAULT_ASK)
register("google:gdocs_append", "Add text as a new paragraph at the end of a Google Doc (existing text is not changed). Needs the user's approval unless an admin allowed it to run automatically.", gdocs_append,
         label="Add text to a Google Doc", family="docs", tier=DEFAULT_ASK)
register("google:gdocs_edit", "Replace (or, with an empty `replace`, delete) every occurrence of a text in a Google Doc. Needs the user's approval.", gdocs_edit,
         label="Edit a Google Doc", family="docs", tier=ALWAYS_ASK, describe=_edit_card, lookup=_look_doc_edit)   # changes or removes text that already exists = ★
register("google:gsheets_get", "Get a Google Sheet's title and its tabs (names and sizes).", gsheets_get, label="Get Google Sheet details", family="sheets")
register("google:gsheets_get_values", "Read several ranges of a Google Sheet at once (A1 notation, e.g. ['Sheet1!A1:D20', 'Sheet2!A:B']).", gsheets_get_values, label="Read ranges of a Google Sheet", family="sheets")
register("google:gsheets_append_rows", "Add rows after the last filled row of a table in a Google Sheet (range = the tab or table, e.g. 'Sheet1'; values = list of rows). Existing cells are not changed. Needs the user's approval unless an admin allowed it to run automatically.",
         gsheets_append_rows, label="Add rows to a Google Sheet", family="sheets", tier=DEFAULT_ASK)
register("google:gsheets_create", "Create a new Google Sheet in the user's Drive (title, optional tab names). Needs the user's approval unless an admin allowed it to run automatically.", gsheets_create,
         label="Create a Google Sheet", family="sheets", tier=DEFAULT_ASK)
register("google:gsheets_add_sheet", "Add a new tab to a Google Sheet. Needs the user's approval unless an admin allowed it to run automatically.", gsheets_add_sheet,
         label="Add a tab to a Google Sheet", family="sheets", tier=DEFAULT_ASK)
register("google:gsheets_summarize", "For each numeric column of a Google Sheet range: count, sum, min, max, average (exact, computed here). Columns that mix numbers and text are reported as skipped.", gsheets_summarize,
         label="Summarize numbers in a Google Sheet", family="sheets")
register("google:gdrive_get", "Get a Google Drive file's details by id: name, type, size, modified time, link, folder.", gdrive_get, label="Get Google Drive file details", family="drive")
register("google:gdrive_download", "Download a Google Drive file (max 20 MB) to a chat file and to /workspace/uploads/ on the agent's computer. A Google Doc is exported as docx (or pdf, txt), a Google Sheet as xlsx (or pdf, csv) with `export_as`.",
         gdrive_download, label="Download a Google Drive file", family="drive")
register("google:gdrive_upload", "Upload a file as a NEW file to Google Drive (never replaces a file with the same name). `file` = chat file id, /workspace/... path, or the content as base64; optional `name` and `folder_id`. docx, xlsx, pptx, pdf, csv, txt, md, json and images only; files with macros are refused. Needs the user's approval unless an admin allowed it to run automatically.",
         gdrive_upload, label="Upload a file to Google Drive", family="drive", tier=DEFAULT_ASK, describe=_upload_card)
register("google:gdrive_create_folder", "Create a folder in Google Drive (optional `parent_id`). Needs the user's approval unless an admin allowed it to run automatically.", gdrive_create_folder,
         label="Create a Google Drive folder", family="drive", tier=DEFAULT_ASK)
