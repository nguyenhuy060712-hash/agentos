"""GĐ8.5 (23/10/2026) + GĐ11.55: one local fake for Google (OAuth + Gmail/Calendar/Sheets/Drive REST) and Microsoft (OAuth + Graph) on 127.0.0.1:8784.
overlay/run_local.py (GW_MOCK=1) points GOOGLE_API_BASE / GRAPH_API_BASE / *_OAUTH_BASE here, so nothing ever reaches Google or Microsoft.
Every API call is authorised by a bearer token that maps to ONE fake person (alice / bob); the data returned is that person's only,
and every call is logged as (provider, person, method, path) so tests can prove whose token was used.
Used by tests/test_gd8_workspace_tools.py + tests/test_gd11_office_*.py (in a thread, `start_in_thread(port)`) and standalone: python tests/fake_workspace_api.py

GĐ11.55 additions (for the office cards 11.56-11.59 / 11.60):
- A real mailbox per person (`box(who)`): threads, labels, drafts, attachments, history (Gmail) and delta (Graph). `deliver(who, frm, subject, body, ...)` drops a new mail in.
- Gmail: threads.list/get, labels list/create, messages.modify/trash, drafts list/send, history.list (404 when the id is older than `expire_history`), upload (media/multipart)
  send, attachments.get, messages.send keeps threadId + headers. Calendar: freeBusy.query, events get/patch/delete (+ sendUpdates), events created by insert are stored.
- Graph: mailFolders (+childFolders, messages, delta with $deltatoken / 410 after `expire_delta`), createReply / createReplyAll / reply / replyAll / forward / move / send,
  message PATCH/DELETE, attachments (<= 3 MB inline, bigger via createUploadSession + chunked PUT to /upload-session/<id>), calendars, getSchedule (5-1440 min views),
  findMeetingTimes, events get/patch/delete, accept / decline / tentativelyAccept / cancel.
- Test levers (all default OFF, so old tests behave as before): `S["inject"]` = [{"match": "path substring", "status": 429, "headers": {"Retry-After": "1"}, "json": {...}, "times": 1,
  "prov": None}] answers matching requests with that response first; `S["enforce_scopes"] = True` makes routes that declare scopes answer 403 insufficient scopes when the token's
  scope string lacks them (a stronger scope counts, like the real services); `S["upload_auth_seen"]` lists upload-session calls that carried an Authorization header (must stay empty)."""
import asyncio, base64, email, email.policy, json, re, secrets, threading, time, urllib.parse
from email.mime.text import MIMEText
from aiohttp import web

PORT = 8784
GA = "https://www.googleapis.com/auth/"
PEOPLE = {
    "alice": {"mail": [("m-a1", "Kế toán <ketoan@congty.vn>", "Báo cáo doanh thu tháng 10", "Doanh thu tháng 10 đạt 1,2 tỷ đồng."),
                       ("m-a2", "Lan <lan@khach.vn>", "Hẹn gặp thứ Sáu", "Chị Lan muốn gặp lúc 9 giờ sáng thứ Sáu.")],
              "events": [("e-a1", "Họp giao ban tuần", "2026-10-26T09:00:00+07:00", "2026-10-26T10:00:00+07:00")],
              "sheet": [["Sản phẩm", "Số lượng"], ["Áo thun", "120"], ["Quần jean", "45"]],
              "files": [("f-a1", "Hợp đồng mẫu ALICE.txt", "text/plain", "Điều 1. Hợp đồng mẫu của Alice.")]},
    "bob": {"mail": [("m-b1", "Sếp <sep@congty.vn>", "BOB-SECRET kế hoạch quý 4", "Chỉ Bob được đọc.")],
            "events": [("e-b1", "BOB-SECRET phỏng vấn", "2026-10-27T14:00:00+07:00", "2026-10-27T15:00:00+07:00")],
            "sheet": [["BOB-SECRET", "1"]],
            "files": [("f-b1", "BOB-SECRET lương.txt", "text/plain", "Bảng lương của Bob.")]},
}
S = {"codes": {}, "tokens": {}, "refresh": {}, "log": [], "sent": [], "drafts": [], "created": [], "updated": [], "ttl": {"google": 3600, "microsoft": 3600},
     "refreshes": 0, "inject": [], "enforce_scopes": False, "upload_auth_seen": [], "box": {}, "labels": {}, "hist": {}, "hid": {}, "hist_floor": {}, "delta": {}, "delta_floor": {},
     "events": {}, "patched": [], "deleted": [], "moved": [], "modified": [], "uploads": [], "sessions": {}, "responded": [], "freebusy_calls": [], "schedule_calls": [], "seq": 0}

def reset():
    for k in ("codes", "tokens", "refresh"): S[k].clear()
    for k in ("log", "sent", "drafts", "created", "updated", "inject", "upload_auth_seen", "box", "labels", "hist", "hid", "hist_floor", "delta", "delta_floor", "events", "patched", "deleted",
              "moved", "modified", "uploads", "sessions", "responded", "freebusy_calls", "schedule_calls"): S[k].clear()
    S.pop("upload_drafts", None); S["ttl"].update(google=3600, microsoft=3600); S["refreshes"] = 0; S["enforce_scopes"] = False; S["seq"] = 0
    for m in _mods(): getattr(m, "reset", lambda _S: None)(S)

# ------------------------------------------------------------------ extra route modules (GĐ12): tests/fakes/<module>.py, each with routes(app, S, helpers) and optional reset(S)
EXTRA_ROUTES = ["fakes.fake_gdocs", "fakes.fake_gdrive", "fakes.fake_graph_files"]   # a later card appends its module here (fakes.fake_gdrive ...); a module that does not exist yet is skipped
_MODS = []

def _mods():
    import importlib
    if len(_MODS) != len(EXTRA_ROUTES):
        _MODS.clear()
        for n in EXTRA_ROUTES:
            try: _MODS.append(importlib.import_module(n))
            except ModuleNotFoundError as e:
                if e.name != n and not n.startswith((e.name or "-") + "."): raise
    return _MODS

def hook(name, *a):
    """The first extra module that has `hook_<name>` and answers (not None) wins; used by main-file routes whose data an extra module may own (e.g. spreadsheets it created)."""
    for m in _mods():
        f = getattr(m, "hook_" + name, None)
        r = f(*a) if f else None
        if r is not None: return r
    return None

def _reply(r): return r if isinstance(r, web.StreamResponse) else web.json_response(r)

def _id(p): S["seq"] += 1; return f"{p}-{S['seq']}-{secrets.token_hex(2)}"

def _issue(prov, who, scope):
    at, rt = f"{prov[0]}at_" + secrets.token_urlsafe(12), f"{prov[0]}rt_" + secrets.token_urlsafe(12)
    S["tokens"][at] = {"prov": prov, "who": who, "exp": time.time() + S["ttl"][prov], "scope": scope}; S["refresh"][rt] = {"prov": prov, "who": who, "scope": scope}
    return {"access_token": at, "refresh_token": rt, "token_type": "Bearer", "expires_in": S["ttl"][prov], "scope": scope}

# ------------------------------------------------------------------ OAuth (both providers: /google/oauth/*, /microsoft/oauth/*)
async def authorize(req):
    q = req.query; prov = req.match_info["prov"]; who = q.get("fake_user", "alice")
    if not q.get("client_id") or q.get("response_type") != "code": return web.json_response({"error": "invalid_request"}, status=400)
    code = secrets.token_urlsafe(10); S["codes"][code] = {"prov": prov, "who": who, "redirect_uri": q.get("redirect_uri"), "scope": q.get("scope", "")}
    raise web.HTTPFound(q["redirect_uri"] + "?" + urllib.parse.urlencode({"code": code, "state": q.get("state", "")}))

async def token(req):
    f = await req.post(); prov = req.match_info["prov"]
    if f.get("client_id") != f"fake-{prov}-client" or f.get("client_secret") != "fake-secret": return web.json_response({"error": "invalid_client"}, status=401)
    if f.get("grant_type") == "authorization_code":
        c = S["codes"].pop(f.get("code"), None)
        if not c or c["prov"] != prov or c["redirect_uri"] != f.get("redirect_uri"): return web.json_response({"error": "invalid_grant"}, status=400)
        return web.json_response(_issue(prov, c["who"], c["scope"]))
    if f.get("grant_type") == "refresh_token":
        r = S["refresh"].pop(f.get("refresh_token"), None)
        if not r or r["prov"] != prov: return web.json_response({"error": "invalid_grant"}, status=400)
        S["refreshes"] += 1; return web.json_response(_issue(prov, r["who"], r["scope"]))
    return web.json_response({"error": "unsupported_grant_type"}, status=400)

async def revoke(req): return web.json_response({})

def _scopes_ok(have, alts):
    """alts: scope names, ANY of which (or a stronger scope listed in `have`) lets the call through."""
    have = {s.replace("https://graph.microsoft.com/", "") for s in (have or "").replace(",", " ").split()}
    return any(a in have for a in alts)

def _who(req, prov, *alts):
    a = req.headers.get("Authorization", ""); t = S["tokens"].get(a[7:]) if a.startswith("Bearer ") else None
    if not t or t["prov"] != prov or t["exp"] < time.time():
        S["log"].append((prov, None, req.method, req.path)); raise web.HTTPUnauthorized(text='{"error":"invalid_token"}', content_type="application/json")
    S["log"].append((prov, t["who"], req.method, req.path))
    if alts and S["enforce_scopes"] and not _scopes_ok(t["scope"], alts):
        if prov == "google":
            raise web.HTTPForbidden(text=json.dumps({"error": {"code": 403, "message": "Request had insufficient authentication scopes.", "status": "PERMISSION_DENIED",
                                                       "errors": [{"reason": "insufficientPermissions"}], "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}]}}), content_type="application/json")
        raise web.HTTPForbidden(text=json.dumps({"error": {"code": "ErrorAccessDenied", "message": "Access is denied. Check credentials and try again."}}), content_type="application/json")
    return t["who"]

def _err(status, message, code=None):
    return web.json_response({"error": {"code": code or status, "message": message}}, status=status)

@web.middleware
async def _inject(req, handler):
    """S["inject"] rules answer first (429 / 503 / 403 / 410 ... with headers); a rule is used `times` times (default once)."""
    for r in list(S["inject"]):
        if r.get("match", "") in req.path_qs and r.get("prov") in (None, "google" if "/graph/" not in req.path else "microsoft") and (not r.get("method") or r["method"] == req.method):
            r["times"] = r.get("times", 1) - 1
            if r["times"] <= 0: S["inject"].remove(r)
            S["log"].append(("google" if "/graph/" not in req.path else "microsoft", "(injected)", req.method, req.path))
            return web.json_response(r.get("json", {"error": {"code": r["status"], "message": "injected"}}), status=r["status"], headers=r.get("headers") or {})
    return await handler(req)

async def userinfo(req):
    prov = req.match_info["prov"]; who = _who(req, prov); return web.json_response({"sub": who, "email": f"{who}@fake.test", "name": who.title()})

# ------------------------------------------------------------------ the mailbox (shared by Gmail and Graph)
def _rfc(frm, to, subj, body, extra=None, atts=()):
    m = MIMEText(body, "plain", "utf-8"); m["From"] = frm; m["To"] = to; m["Subject"] = subj
    for k, v in (extra or {}).items(): m[k] = v
    if not atts: return m.as_bytes()
    from email.mime.multipart import MIMEMultipart
    from email.mime.application import MIMEApplication
    mm = MIMEMultipart(); [mm.__setitem__(k, m[k]) for k in ("From", "To", "Subject")]; mm.attach(m)
    for a in atts:
        p = MIMEApplication(a["data"], Name=a["name"]); p.add_header("Content-Disposition", "attachment", filename=a["name"]); mm.attach(p)
    return mm.as_bytes()

def _raw(frm, subj, body): return base64.urlsafe_b64encode(_rfc(frm, "me@fake.test", subj, body)).decode()

def box(who):
    """The person's mailbox: list of message dicts, built once from PEOPLE (so old tests see the same data)."""
    if who not in S["box"]:
        S["box"][who] = [{"id": m[0], "thread": "t" + m[0], "frm": m[1], "to": f"{who}@fake.test", "subject": m[2], "body": m[3], "labels": ["INBOX", "UNREAD"], "folder": "inbox",
                          "is_read": False, "headers": {}, "atts": [], "date": "Thu, 22 Oct 2026 08:00:00 +0700", "iso": "2026-10-22T08:00:00Z", "draft": False} for m in PEOPLE[who]["mail"]]
        S["hid"][who] = 100; S["hist"][who] = []; S["delta"][who] = []; S["hist_floor"][who] = 0; S["delta_floor"][who] = 0
    return S["box"][who]

def deliver(who, frm, subject, body, *, thread=None, headers=None, labels=("INBOX", "UNREAD"), folder="inbox", atts=(), to=None, mid=None, draft=False, fresh=False):
    """Put a message in `who`'s mailbox (a mail that ARRIVED: records a Gmail history entry and a Graph delta entry when it lands in the inbox). Returns the message dict."""
    b = box(who); mid = mid or _id("m")
    m = {"id": mid, "thread": thread or "t-" + mid, "frm": frm, "to": to or f"{who}@fake.test", "subject": subject, "body": body, "labels": list(labels), "folder": folder,
         "is_read": "UNREAD" not in labels, "headers": dict(headers or {}), "atts": [dict(a) for a in atts], "date": "Fri, 23 Oct 2026 09:00:00 +0700", "iso": "2026-10-23T09:00:00Z", "draft": draft}
    if fresh: m["ms"] = str(int(time.time() * 1000)); m["iso"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())   # 11.60: arrived "now" (real-clock server tests); default = the fixed old date
    b.append(m)
    if "INBOX" in m["labels"] or folder == "inbox":
        S["hid"][who] += 1; S["hist"][who].append({"id": str(S["hid"][who]), "messagesAdded": [{"message": {"id": mid, "threadId": m["thread"], "labelIds": m["labels"]}}]})
        S["delta"][who].append((len(S["delta"][who]) + 1, mid, False))
    return m

def expire_history(who): box(who); S["hist_floor"][who] = S["hid"][who] + 1            # Gmail: every startHistoryId handed out so far is now too old (404)
def expire_delta(who): box(who); S["delta_floor"][who] = len(S["delta"][who]) + 1        # Graph: every delta token handed out so far is gone (410 syncStateNotFound)
def _get(who, mid): return next((m for m in box(who) if m["id"] == mid), None)
def _addr(s): return s.split("<")[-1].rstrip(">").strip()
def _name(s): return s.split(" <")[0] if "<" in s else ""
def _bytes(a): return a["data"] if isinstance(a.get("data"), (bytes, bytearray)) else str(a.get("data", "")).encode()

# ------------------------------------------------------------------ Google: Gmail
def _gmsg(m, fmt="full"):
    hs = [{"name": "From", "value": m["frm"]}, {"name": "To", "value": m["to"]}, {"name": "Subject", "value": m["subject"]}, {"name": "Date", "value": m["date"]},
          *[{"name": k, "value": v} for k, v in m["headers"].items()]]
    body = {"data": base64.urlsafe_b64encode(m["body"].encode()).decode()}
    out = {"id": m["id"], "threadId": m["thread"], "labelIds": m["labels"], "snippet": m["body"][:60], "historyId": "100", "internalDate": m.get("ms", "1792000000000")}
    if fmt == "raw": return {**out, "raw": base64.urlsafe_b64encode(_rfc(m["frm"], m["to"], m["subject"], m["body"], m["headers"], m["atts"])).decode()}
    parts = [{"mimeType": "text/plain", "body": body}] + [{"mimeType": a.get("mime", "application/octet-stream"), "filename": a["name"],
                                                          "body": {"attachmentId": a.get("id") or f"att-{i}", "size": len(_bytes(a))}} for i, a in enumerate(m["atts"])]
    out["payload"] = {"mimeType": "multipart/mixed", "headers": hs, "body": {}, "parts": parts} if m["atts"] else {"mimeType": "text/plain", "headers": hs, "body": body}
    return out

def _gq(m, q):
    """Gmail search subset: from: subject: label: in:inbox is:unread (other operators such as newer_than: are accepted and ignored); the remaining words must appear as a phrase."""
    ops = re.findall(r"(\w+):(\S+)", q or ""); rest = re.sub(r"\w+:\S+", " ", q or "").strip().lower()
    for k, v in ops:
        v = v.lower()
        if k == "from" and v not in m["frm"].lower(): return False
        if k == "subject" and v not in m["subject"].lower(): return False
        if k == "label" and v.upper() not in m["labels"]: return False
        if k == "in" and v == "inbox" and "INBOX" not in m["labels"]: return False
        if k == "is" and v == "unread" and "UNREAD" not in m["labels"]: return False
    return not rest or rest in (m["subject"] + m["body"]).lower()

async def gmail_list(req):
    who = _who(req, "google", GA + "gmail.readonly", GA + "gmail.modify"); q = req.query.get("q") or ""
    ms = [m for m in box(who) if not m["draft"] and "TRASH" not in m["labels"] and _gq(m, q)]
    return web.json_response({"messages": [{"id": m["id"], "threadId": m["thread"]} for m in ms[:int(req.query.get("maxResults", 10))]], "resultSizeEstimate": len(ms)})

async def gmail_get(req):
    who = _who(req, "google", GA + "gmail.readonly", GA + "gmail.modify"); m = _get(who, req.match_info["id"])
    if not m: return _err(404, "Requested entity was not found.")
    return web.json_response(_gmsg(m, req.query.get("format", "full")))

async def gmail_attachment(req):
    who = _who(req, "google", GA + "gmail.readonly", GA + "gmail.modify"); m = _get(who, req.match_info["id"])
    a = next((a for i, a in enumerate(m["atts"] if m else []) if (a.get("id") or f"att-{i}") == req.match_info["aid"]), None)
    if not a: return _err(404, "Requested entity was not found.")
    return web.json_response({"size": len(_bytes(a)), "data": base64.urlsafe_b64encode(_bytes(a)).decode()})

def _parse_sent(who, raw_b64, thread_id):
    raw = base64.urlsafe_b64decode(raw_b64 + "=" * (-len(raw_b64) % 4)); msg = email.message_from_bytes(raw, policy=email.policy.default)
    atts = [{"name": p.get_filename(), "mime": p.get_content_type(), "data": p.get_payload(decode=True) or b""} for p in msg.iter_attachments()]
    body = msg.get_body(("plain",)); text = body.get_content() if body else ""
    return {"raw": raw_b64, "raw_bytes": raw, "thread_id": thread_id, "headers": {k: str(v) for k, v in msg.items()}, "subject": str(msg.get("Subject", "")), "to": str(msg.get("To", "")),
            "cc": str(msg.get("Cc", "")), "bcc": str(msg.get("Bcc", "")), "text": text, "atts": atts, "size": len(raw)}

def _gsent(who, parsed):
    sid = _id("sent"); S["sent"].append({"who": who, "prov": "google", "id": sid, **parsed})
    deliver(who, f"{who}@fake.test", parsed["subject"], parsed["text"], thread=parsed["thread_id"] or "t-new", labels=("SENT",), folder="sentitems", mid=sid, to=parsed["to"], headers={k: v for k, v in parsed["headers"].items() if k.lower() in ("in-reply-to", "references", "message-id", "x-aos-agent")})
    return web.json_response({"id": sid, "threadId": parsed["thread_id"] or "t-new", "labelIds": ["SENT"]})

async def gmail_send(req):
    who = _who(req, "google", GA + "gmail.modify", GA + "gmail.compose", GA + "gmail.send"); b = await req.json()
    return _gsent(who, _parse_sent(who, b.get("raw", ""), b.get("threadId")))

async def gmail_send_upload(req):
    """POST /upload/gmail/v1/users/me/messages/send?uploadType=media|multipart: the body is the raw RFC 822 message (media) or multipart/related with JSON metadata + message/rfc822."""
    who = _who(req, "google", GA + "gmail.modify", GA + "gmail.compose", GA + "gmail.send"); body = await req.read(); tid = None
    if req.query.get("uploadType") == "multipart":
        ct = req.headers.get("Content-Type", ""); m = re.search(r'boundary="?([^";]+)', ct)
        parts = body.split(("--" + m.group(1)).encode()) if m else []
        data = [p for p in parts if b"message/rfc822" in p[:200]]
        meta = [p for p in parts if b"application/json" in p[:200]]
        if meta: tid = (json.loads(meta[0].split(b"\r\n\r\n", 1)[1].strip() or b"{}").get("threadId"))
        body = data[0].split(b"\r\n\r\n", 1)[1] if data else b""
        if body.endswith(b"\r\n"): body = body[:-2]
    p = _parse_sent(who, base64.urlsafe_b64encode(body).decode(), tid or req.query.get("threadId")); p["via"] = "upload"
    return _gsent(who, p)

async def gmail_draft_upload(req):
    """POST /upload/gmail/v1/users/me/drafts?uploadType=multipart (GĐ11.56): JSON metadata {"message": {"threadId"}} + message/rfc822 -> a draft, like drafts.create."""
    who = _who(req, "google", GA + "gmail.modify", GA + "gmail.compose"); body = await req.read(); ct = req.headers.get("Content-Type", ""); m = re.search(r'boundary="?([^";]+)', ct)
    parts = body.split(("--" + m.group(1)).encode()) if m else []
    meta = [p for p in parts if b"application/json" in p[:200]]; data = [p for p in parts if b"message/rfc822" in p[:200]]
    tid = ((json.loads(meta[0].split(b"\r\n\r\n", 1)[1].strip() or b"{}").get("message") or {}).get("threadId")) if meta else None
    raw = data[0].split(b"\r\n\r\n", 1)[1] if data else b""
    if raw.endswith(b"\r\n"): raw = raw[:-2]
    S["upload_drafts"] = S.get("upload_drafts", 0) + 1
    return await _draft_create(who, {"message": {"raw": base64.urlsafe_b64encode(raw).decode(), "threadId": tid}})

async def gmail_profile(req):
    who = _who(req, "google", GA + "gmail.readonly", GA + "gmail.modify"); n = len(box(who))   # 11.60: historyId = where the mailbox is now (box() creates the counters)
    return web.json_response({"emailAddress": f"{who}@fake.test", "messagesTotal": n, "threadsTotal": n, "historyId": str(S["hid"][who])})

async def gmail_draft(req):
    who = _who(req, "google", GA + "gmail.modify", GA + "gmail.compose"); return await _draft_create(who, await req.json())

async def _draft_create(who, b):
    S["drafts"].append({"who": who, "body": b})
    msg = b.get("message") or {}; p = _parse_sent(who, msg.get("raw", ""), msg.get("threadId")); did = _id("draft")
    d = deliver(who, f"{who}@fake.test", p["subject"], p["text"], thread=msg.get("threadId"), labels=("DRAFT",), folder="drafts", to=p["to"], draft=True,
                headers={k: v for k, v in p["headers"].items() if k.lower() in ("in-reply-to", "references")}, atts=p["atts"])
    S["drafts"][-1]["draft_id"] = did; S["drafts"][-1]["message_id"] = d["id"]
    return web.json_response({"id": did, "message": {"id": d["id"], "threadId": d["thread"]}})

async def gmail_drafts_list(req):
    who = _who(req, "google", GA + "gmail.readonly", GA + "gmail.modify")
    return web.json_response({"drafts": [{"id": d["draft_id"], "message": {"id": d["message_id"]}} for d in S["drafts"] if d["who"] == who and "draft_id" in d]})

async def gmail_draft_get(req):
    """GET /gmail/v1/users/me/drafts/{id}?format=metadata|full (F-7): the draft with its message (headers = real To / Cc / Bcc / Subject). Bcc is shown when the draft has headers['Bcc']."""
    who = _who(req, "google", GA + "gmail.readonly", GA + "gmail.modify", GA + "gmail.compose")
    d = next((d for d in S["drafts"] if d["who"] == who and d.get("draft_id") == req.match_info["id"]), None)
    if not d: return _err(404, "Requested entity was not found.")
    return web.json_response({"id": d["draft_id"], "message": _gmsg(_get(who, d["message_id"]), req.query.get("format", "full"))})

async def gmail_draft_send(req):
    who = _who(req, "google", GA + "gmail.modify", GA + "gmail.compose", GA + "gmail.send"); b = await req.json()
    d = next((d for d in S["drafts"] if d["who"] == who and d.get("draft_id") == b.get("id")), None)
    if not d: return _err(404, "Requested entity was not found.")
    m = _get(who, d["message_id"]); m["labels"] = ["SENT"]; m["folder"] = "sentitems"; m["draft"] = False
    S["sent"].append({"who": who, "prov": "google", "id": m["id"], "raw": None, "thread_id": m["thread"], "headers": m["headers"], "subject": m["subject"], "to": m["to"], "text": m["body"], "atts": m["atts"], "from_draft": True})
    return web.json_response({"id": m["id"], "threadId": m["thread"], "labelIds": ["SENT"]})

async def gmail_threads(req):
    who = _who(req, "google", GA + "gmail.readonly", GA + "gmail.modify"); q = req.query.get("q", "")
    seen = {}
    for m in box(who):
        if not m["draft"] and "TRASH" not in m["labels"] and _gq(m, q): seen.setdefault(m["thread"], m)
    return web.json_response({"threads": [{"id": t, "snippet": m["body"][:60], "historyId": "100"} for t, m in list(seen.items())[:int(req.query.get("maxResults", 10))]]})

async def gmail_thread(req):
    who = _who(req, "google", GA + "gmail.readonly", GA + "gmail.modify"); ms = [m for m in box(who) if m["thread"] == req.match_info["id"] and not m["draft"]]
    if not ms: return _err(404, "Requested entity was not found.")
    return web.json_response({"id": req.match_info["id"], "historyId": "100", "messages": [_gmsg(m, req.query.get("format", "full")) for m in ms]})

SYS = ["INBOX", "SENT", "DRAFT", "TRASH", "SPAM", "UNREAD", "STARRED", "IMPORTANT"]
async def gmail_labels(req):
    who = _who(req, "google", GA + "gmail.readonly", GA + "gmail.modify")
    return web.json_response({"labels": [{"id": s, "name": s, "type": "system"} for s in SYS] + [{"id": i, "name": n, "type": "user"} for i, n in S["labels"].get(who, {}).items()]})

async def gmail_label_new(req):
    who = _who(req, "google", GA + "gmail.modify"); b = await req.json(); L = S["labels"].setdefault(who, {})
    if b.get("name") in L.values(): return _err(409, "Label name exists or conflicts")
    lid = f"Label_{len(L) + 1}"; L[lid] = b.get("name"); return web.json_response({"id": lid, "name": b.get("name"), "type": "user"})

async def gmail_modify(req):
    who = _who(req, "google", GA + "gmail.modify"); b = await req.json(); m = _get(who, req.match_info["id"])
    if not m: return _err(404, "Requested entity was not found.")
    valid = set(SYS) | set(S["labels"].get(who, {}))
    bad = [x for x in (b.get("addLabelIds") or []) + (b.get("removeLabelIds") or []) if x not in valid]
    if bad: return _err(400, f"Invalid label: {bad[0]}")
    m["labels"] = [x for x in m["labels"] if x not in (b.get("removeLabelIds") or [])] + [x for x in (b.get("addLabelIds") or []) if x not in m["labels"]]
    S["modified"].append({"who": who, "id": m["id"], "add": b.get("addLabelIds") or [], "remove": b.get("removeLabelIds") or []})
    return web.json_response(_gmsg(m, "minimal"))

async def gmail_trash(req):
    who = _who(req, "google", GA + "gmail.modify"); m = _get(who, req.match_info["id"])
    if not m: return _err(404, "Requested entity was not found.")
    m["labels"] = [x for x in m["labels"] if x != "INBOX"] + ["TRASH"]; S["deleted"].append({"who": who, "id": m["id"], "prov": "google"})
    return web.json_response(_gmsg(m, "minimal"))

async def gmail_history(req):
    who = _who(req, "google", GA + "gmail.readonly", GA + "gmail.modify"); box(who)
    try: start = int(req.query.get("startHistoryId", ""))
    except ValueError: return _err(400, "Invalid startHistoryId")
    if start < S["hist_floor"][who]: return _err(404, "Requested entity was not found.")
    rows = [h for h in S["hist"][who] if int(h["id"]) > start]
    if req.query.get("labelId"): rows = [{**h, "messagesAdded": [a for a in h["messagesAdded"] if req.query["labelId"] in a["message"]["labelIds"]]} for h in rows]
    return web.json_response({"history": [h for h in rows if h["messagesAdded"]] or None, "historyId": str(S["hid"][who])} if rows else {"historyId": str(S["hid"][who])})

# ------------------------------------------------------------------ Google: Calendar, Sheets, Drive
def _evs(who):
    S["events"].setdefault(who, {e[0]: {"id": e[0], "summary": e[1], "start": {"dateTime": e[2]}, "end": {"dateTime": e[3]}, "htmlLink": f"https://cal.fake/{e[0]}",
                                        "creator": {"email": f"{who}@fake.test"}, "organizer": {"email": f"{who}@fake.test"}} for e in PEOPLE[who]["events"]})
    return S["events"][who]

async def cal_list(req):
    who = _who(req, "google", GA + "calendar.calendarlist.readonly", GA + "calendar.readonly", GA + "calendar")
    return web.json_response({"items": [{"id": "primary", "summary": f"{who}@fake.test", "timeZone": "Asia/Ho_Chi_Minh", "primary": True, "accessRole": "owner"},
                                        {"id": f"team-{who}@group.calendar.fake.test", "summary": "Team calendar", "timeZone": "Asia/Ho_Chi_Minh", "accessRole": "reader"}]})

async def cal_events(req):
    """Events of the person (PEOPLE + the ones created / patched in this run); timeMin / timeMax / q filter them when given. The {cal} part is only logged (path)."""
    who = _who(req, "google", GA + "calendar.events.readonly", GA + "calendar.events", GA + "calendar.readonly", GA + "calendar")
    lo, hi, qq = req.query.get("timeMin"), req.query.get("timeMax"), (req.query.get("q") or "").lower()
    def keep(e):
        st = e.get("start", {}).get("dateTime") or e.get("start", {}).get("date") or ""; en = e.get("end", {}).get("dateTime") or st
        if lo and hi and st:
            try:
                if not (_dt(st) < _dt(hi) and _dt(en) > _dt(lo)): return False
            except (ValueError, TypeError): pass
        return not qq or qq in (e.get("summary") or "").lower()
    return web.json_response({"items": [e for e in _evs(who).values() if keep(e)]})

async def cal_insert(req):
    who = _who(req, "google", GA + "calendar.events", GA + "calendar"); b = await req.json(); S["created"].append({"who": who, "prov": "google", "body": b, "query": dict(req.query)})
    eid = "e-new" if len(S["created"]) == 1 else _id("e"); ev = {"id": eid, "htmlLink": f"https://cal.fake/{eid}", **b}; _evs(who)[eid] = ev
    return web.json_response(ev)

async def cal_event(req):
    who = _who(req, "google", *( (GA + "calendar.events.readonly", GA + "calendar.events", GA + "calendar.readonly", GA + "calendar") if req.method == "GET" else (GA + "calendar.events", GA + "calendar")))
    evs = _evs(who); ev = evs.get(req.match_info["id"])
    if not ev: return _err(404, "Not Found")
    if req.method == "GET": return web.json_response(ev)
    if req.method == "DELETE":
        evs.pop(ev["id"]); S["deleted"].append({"who": who, "id": ev["id"], "prov": "google", "query": dict(req.query)}); return web.Response(status=204)
    b = await req.json(); ev.update(b); S["patched"].append({"who": who, "id": ev["id"], "body": b, "query": dict(req.query)})
    if "attendees" in b and any("responseStatus" in a for a in b["attendees"]): S["responded"].append({"who": who, "id": ev["id"], "attendees": b["attendees"], "query": dict(req.query)})
    return web.json_response(ev)

def _dt(s): return __import__("datetime").datetime.fromisoformat(s.replace("Z", "+00:00"))

async def cal_freebusy(req):
    who = _who(req, "google", GA + "calendar.freebusy", GA + "calendar.readonly", GA + "calendar"); b = await req.json(); S["freebusy_calls"].append({"who": who, "body": b})
    lo, hi = _dt(b["timeMin"]), _dt(b["timeMax"]); out = {}
    for it in b.get("items", []):
        cid = it.get("id"); person = cid.split("@")[0] if cid.endswith("@fake.test") else (who if cid == "primary" else None)
        if person not in PEOPLE: out[cid] = {"errors": [{"domain": "global", "reason": "notFound"}], "busy": []}; continue
        out[cid] = {"busy": [{"start": e["start"]["dateTime"], "end": e["end"]["dateTime"]} for e in _evs(person).values() if "dateTime" in e.get("start", {}) and _dt(e["start"]["dateTime"]) < hi and _dt(e["end"]["dateTime"]) > lo]}
    return web.json_response({"kind": "calendar#freeBusy", "timeMin": b["timeMin"], "timeMax": b["timeMax"], "calendars": out})

async def sheet_get(req):
    who = _who(req, "google", GA + "spreadsheets.readonly", GA + "spreadsheets"); h = hook("sheet_get", who, req.match_info["sid"], req.match_info["range"])
    if h is not None: return _reply(h)
    return web.json_response({"range": urllib.parse.unquote(req.match_info["range"]), "majorDimension": "ROWS", "values": PEOPLE[who]["sheet"]})

async def sheet_put(req):
    who = _who(req, "google", GA + "spreadsheets"); b = await req.json()
    h = hook("sheet_put", who, req.match_info["sid"], req.match_info["range"], b.get("values"))
    if h is not None: return _reply(h)
    S["updated"].append({"who": who, "range": req.match_info["range"], "values": b.get("values")})
    v = b.get("values") or []
    return web.json_response({"updatedRange": urllib.parse.unquote(req.match_info["range"]), "updatedRows": len(v), "updatedColumns": max((len(r) for r in v), default=0),
                              "updatedCells": sum(len(r) for r in v)})

async def drive_files(req):
    who = _who(req, "google", GA + "drive.readonly", GA + "drive"); q = req.query.get("q", ""); h = hook("drive_files", who, req.query)
    if h is not None: return _reply(h)
    term = q.split("'")[1].lower() if "'" in q else ""
    fs = [f for f in PEOPLE[who]["files"] if term in f[1].lower() or term in f[3].lower()]
    return web.json_response({"files": [{"id": f[0], "name": f[1], "mimeType": f[2], "modifiedTime": "2026-10-20T08:00:00Z"} for f in fs]})

async def drive_file(req):
    who = _who(req, "google", GA + "drive.readonly", GA + "drive"); h = hook("drive_file", who, req.match_info["id"], req.query)
    if h is not None: return _reply(h)
    f = next((f for f in PEOPLE[who]["files"] if f[0] == req.match_info["id"]), None)
    if not f: return _err(404, "File not found")
    if req.query.get("alt") == "media": return web.Response(text=f[3], content_type=f[2])
    return web.json_response({"id": f[0], "name": f[1], "mimeType": f[2], "size": str(len(f[3].encode()))})

# ------------------------------------------------------------------ Microsoft Graph (/graph/v1.0/...)
MR = "Mail.Read", "Mail.ReadWrite"
MW = "Mail.ReadWrite",
MS_ = "Mail.Send", "Mail.ReadWrite"
CR = "Calendars.Read", "Calendars.ReadWrite"
CW = "Calendars.ReadWrite",
FOLDERS = {"inbox": "Inbox", "sentitems": "Sent Items", "drafts": "Drafts", "deleteditems": "Deleted Items", "archive": "Archive"}

def _mrec(a): return {"emailAddress": {"name": _name(a), "address": _addr(a)}}
def _ms(m, full=True):
    to = [x.strip() for x in m["to"].split(",") if x.strip()]
    d = {"id": m["id"], "subject": m["subject"], "from": _mrec(m["frm"]), "toRecipients": [_mrec(x) for x in to], "receivedDateTime": m["iso"], "bodyPreview": m["body"][:60],
         "conversationId": m["thread"], "parentFolderId": m["folder"], "isRead": m["is_read"], "hasAttachments": bool(m["atts"]), "isDraft": m["draft"], "categories": m.get("categories", []),
         "flag": {"flagStatus": m.get("flag", "notFlagged")}, "internetMessageHeaders": [{"name": k, "value": v} for k, v in m["headers"].items()]}
    if full: d["body"] = {"contentType": "text", "content": m["body"]}
    return d

async def ms_messages(req):
    who = _who(req, "microsoft", *MR); s = (req.query.get("$search") or "").strip('"').lower(); folder = req.match_info.get("folder")
    ms = [m for m in box(who) if (not folder or m["folder"] == folder.lower()) and (folder or not m["draft"]) and (not s or s in (m["subject"] + m["body"]).lower())]
    return web.json_response({"value": [{**_ms(m, False), "from": _mrec(m["frm"])} for m in ms[:int(req.query.get("$top", 50))]]})

async def ms_message(req):
    who = _who(req, "microsoft", *(MR if req.method == "GET" else MW)); m = _get(who, req.match_info["id"])
    if not m: return _err(404, "The specified object was not found in the store.", "ErrorItemNotFound")
    if req.method == "DELETE":
        m["folder"] = "deleteditems"; S["deleted"].append({"who": who, "id": m["id"], "prov": "microsoft"}); return web.Response(status=204)
    if req.method == "PATCH":
        b = await req.json()
        if "isRead" in b: m["is_read"] = b["isRead"]
        if "categories" in b: m["categories"] = b["categories"]
        if "flag" in b: m["flag"] = (b["flag"] or {}).get("flagStatus", "notFlagged")
        S["modified"].append({"who": who, "id": m["id"], "body": b})
    return web.json_response(_ms(m))

async def ms_send(req):
    who = _who(req, "microsoft", *MS_); b = await req.json(); S["sent"].append({"who": who, "prov": "microsoft", "message": b.get("message")}); return web.Response(status=202)

def _recips(b): return ", ".join(r["emailAddress"]["address"] for r in b or [])

async def ms_reply_op(req):
    """createReply / createReplyAll -> a DRAFT in the same conversation; reply / replyAll / forward -> sent (202). forward takes toRecipients + comment."""
    who = _who(req, "microsoft", *MS_); op = req.match_info["op"]; m = _get(who, req.match_info["id"])
    if not m: return _err(404, "The specified object was not found in the store.", "ErrorItemNotFound")
    b = await req.json() if req.can_read_body else {}
    if op.startswith("create"):
        subj = m["subject"] if m["subject"].lower().startswith("re:") else "Re: " + m["subject"]
        d = deliver(who, f"{who}@fake.test", subj, (b.get("comment") or "") + "\n\n" + m["body"], thread=m["thread"], labels=(), folder="drafts", to=m["frm"], draft=True, headers={"In-Reply-To": m["id"]})
        S["drafts"].append({"who": who, "message_id": d["id"], "op": op, "message": b.get("message")}); return web.json_response(_ms(d), status=201)
    S["sent"].append({"who": who, "prov": "microsoft", "op": op, "of": m["id"], "thread": m["thread"], "comment": b.get("comment"), "message": b.get("message"),
                      "to": _recips(b.get("toRecipients")) or m["frm"]})
    return web.Response(status=202)

async def ms_create_draft(req):
    who = _who(req, "microsoft", *MW); b = await req.json()
    d = deliver(who, f"{who}@fake.test", b.get("subject", ""), (b.get("body") or {}).get("content", ""), labels=(), folder="drafts", to=_recips(b.get("toRecipients")), draft=True)
    S["drafts"].append({"who": who, "message_id": d["id"], "op": "create", "body": b}); return web.json_response(_ms(d), status=201)

async def ms_draft_send(req):
    who = _who(req, "microsoft", *MS_); m = _get(who, req.match_info["id"])
    if not m or not m["draft"]: return _err(404, "The specified object was not found in the store.", "ErrorItemNotFound")
    m["draft"] = False; m["folder"] = "sentitems"; S["sent"].append({"who": who, "prov": "microsoft", "from_draft": True, "id": m["id"], "subject": m["subject"], "to": m["to"], "atts": m["atts"]})
    return web.Response(status=202)

async def ms_move(req):
    who = _who(req, "microsoft", *MW); b = await req.json(); m = _get(who, req.match_info["id"])
    if not m: return _err(404, "The specified object was not found in the store.", "ErrorItemNotFound")
    dest = str(b.get("destinationId", "")).lower()
    if dest not in FOLDERS: return _err(400, "The destination folder was not found.", "ErrorInvalidIdMalformed")
    m["folder"] = dest; S["moved"].append({"who": who, "id": m["id"], "to": dest}); return web.json_response(_ms(m), status=201)

async def ms_folders(req):
    who = _who(req, "microsoft", *MR); parent = req.match_info.get("pid")
    rows = [{"id": k, "displayName": v, "parentFolderId": "root", "childFolderCount": 1 if k == "inbox" else 0, "totalItemCount": sum(1 for m in box(who) if m["folder"] == k)} for k, v in FOLDERS.items()]
    if parent: rows = [{"id": "inbox-projects", "displayName": "Projects", "parentFolderId": "inbox", "childFolderCount": 0, "totalItemCount": 0}] if parent == "inbox" else []
    return web.json_response({"value": rows})

async def ms_delta(req):
    who = _who(req, "microsoft", "Mail.ReadBasic", *MR); box(who); tok = req.query.get("$deltatoken")
    if tok is not None:
        try: n = int(tok)
        except ValueError: return _err(400, "Bad delta token", "ErrorInvalidIdMalformed")
        if n < S["delta_floor"][who]: return _err(410, "The sync state generation is not found.", "syncStateNotFound")
    folder = req.match_info["folder"].lower()
    link = f"{req.url.origin()}/graph/v1.0/me/mailFolders('{req.match_info['folder']}')/messages/delta?$deltatoken={len(S['delta'][who])}"
    if tok is None:   # first round: every message of the folder, then a delta link that continues from here
        return web.json_response({"value": [_ms(m, False) for m in box(who) if m["folder"] == folder and not m["draft"]], "@odata.deltaLink": link})
    rows = [d for d in S["delta"][who] if d[0] > n]
    return web.json_response({"value": [{"@removed": {"reason": "deleted"}, "id": mid} if rem else _ms(_get(who, mid), False) for _, mid, rem in rows], "@odata.deltaLink": link})

async def ms_attachments(req):
    who = _who(req, "microsoft", *(MR if req.method == "GET" else MW)); m = _get(who, req.match_info["id"])
    if not m: return _err(404, "The specified object was not found in the store.", "ErrorItemNotFound")
    if req.method == "GET":
        return web.json_response({"value": [{"id": a.get("id") or f"att-{i}", "name": a["name"], "size": len(_bytes(a)), "contentType": a.get("mime"), "@odata.type": "#microsoft.graph.fileAttachment",
                                             "contentBytes": base64.b64encode(_bytes(a)).decode()} for i, a in enumerate(m["atts"])]})
    b = await req.json(); raw = base64.b64decode(b.get("contentBytes", ""))
    if len(raw) > 3 * 1024 * 1024: return _err(413, "The attachment is larger than 3 MB: use an upload session.", "ErrorAttachmentSizeLimitExceeded")
    a = {"id": _id("att"), "name": b.get("name"), "mime": b.get("contentType"), "data": raw}; m["atts"].append(a); S["uploads"].append({"who": who, "msg": m["id"], "name": a["name"], "size": len(raw), "via": "inline"})
    return web.json_response({"id": a["id"], "name": a["name"], "size": len(raw)}, status=201)

async def ms_upload_session(req):
    who = _who(req, "microsoft", *MW); b = await req.json(); item = b.get("AttachmentItem") or {}
    if not _get(who, req.match_info["id"]): return _err(404, "The specified object was not found in the store.", "ErrorItemNotFound")
    if int(item.get("size", 0)) > 150 * 1024 * 1024: return _err(413, "Too large", "ErrorAttachmentSizeLimitExceeded")
    sid = secrets.token_urlsafe(10); S["sessions"][sid] = {"who": who, "msg": req.match_info["id"], "name": item.get("name"), "size": int(item.get("size", 0)), "got": 0, "buf": bytearray()}
    return web.json_response({"uploadUrl": f"{req.url.origin()}/upload-session/{sid}", "expirationDateTime": "2026-10-30T00:00:00Z", "nextExpectedRanges": ["0-"]})

async def upload_put(req):
    """The Graph upload URL: NO bearer token is needed (and none must be sent: it is recorded if one arrives)."""
    if req.headers.get("Authorization"): S["upload_auth_seen"].append(req.path)
    s = S["sessions"].get(req.match_info["sid"])
    if not s: return _err(404, "Upload session not found", "itemNotFound")
    m = re.match(r"bytes (\d+)-(\d+)/(\d+)", req.headers.get("Content-Range", ""))
    if not m or int(m.group(1)) != s["got"]: return _err(416, "Range not satisfiable", "invalidRange")
    s["buf"] += await req.read(); s["got"] = len(s["buf"])
    if s["got"] >= s["size"]:
        msg = _get(s["who"], s["msg"]); a = {"id": _id("att"), "name": s["name"], "data": bytes(s["buf"])}; msg["atts"].append(a)
        S["uploads"].append({"who": s["who"], "msg": s["msg"], "name": s["name"], "size": s["got"], "via": "session"}); S["sessions"].pop(req.match_info["sid"])
        return web.json_response({"id": a["id"], "name": a["name"], "size": s["got"]}, status=201)
    return web.json_response({"expirationDateTime": "2026-10-30T00:00:00Z", "nextExpectedRanges": [f"{s['got']}-"]})

async def ms_calview(req):
    who = _who(req, "microsoft", *CR)
    return web.json_response({"value": [{"id": e[0], "subject": e[1], "start": {"dateTime": e[2], "timeZone": "UTC"}, "end": {"dateTime": e[3], "timeZone": "UTC"},
                                         "location": {"displayName": ""}, "organizer": {"emailAddress": {"address": f"{who}@fake.test"}}} for e in PEOPLE[who]["events"]]})

async def ms_calendars(req):
    who = _who(req, "microsoft", *CR); return web.json_response({"value": [{"id": "cal-main", "name": "Calendar", "owner": {"address": f"{who}@fake.test"}, "isDefaultCalendar": True}]})

async def ms_event_new(req):
    who = _who(req, "microsoft", *CW); b = await req.json(); S["created"].append({"who": who, "prov": "microsoft", "body": b})
    eid = "ms-e-new" if len(S["created"]) == 1 else _id("ms-e"); ev = {"id": eid, "webLink": f"https://outlook.fake/{eid}", **b}; _msevs(who)[eid] = ev
    return web.json_response(ev, status=201)

def _msevs(who):
    S["events"].setdefault("ms:" + who, {})
    return S["events"]["ms:" + who]

async def ms_event(req):
    who = _who(req, "microsoft", *(CR if req.method == "GET" else CW)); evs = _msevs(who); ev = evs.get(req.match_info["id"])
    if not ev and req.match_info["id"] in {e[0] for e in PEOPLE[who]["events"]}:
        e = next(e for e in PEOPLE[who]["events"] if e[0] == req.match_info["id"]); ev = evs[e[0]] = {"id": e[0], "subject": e[1], "start": {"dateTime": e[2]}, "end": {"dateTime": e[3]}}
    if not ev: return _err(404, "The specified object was not found in the store.", "ErrorItemNotFound")
    if req.method == "DELETE": evs.pop(ev["id"]); S["deleted"].append({"who": who, "id": ev["id"], "prov": "microsoft"}); return web.Response(status=204)
    if req.method == "PATCH": b = await req.json(); ev.update(b); S["patched"].append({"who": who, "id": ev["id"], "body": b})
    return web.json_response(ev)

async def ms_event_op(req):
    who = _who(req, "microsoft", *CW); op = req.match_info["op"]; b = await req.json() if req.can_read_body else {}
    S["responded"].append({"who": who, "id": req.match_info["id"], "op": op, "comment": b.get("comment"), "sendResponse": b.get("sendResponse")}); return web.Response(status=202)

async def ms_schedule(req):
    who = _who(req, "microsoft", "Calendars.ReadBasic", *CR); b = await req.json(); iv = int(b.get("availabilityViewInterval", 30)); S["schedule_calls"].append({"who": who, "body": b})
    if not 5 <= iv <= 1440: return _err(400, "availabilityViewInterval must be between 5 and 1440 minutes.", "ErrorInvalidRequest")
    off = "+07:00" if (b["startTime"].get("timeZone") == "SE Asia Standard Time") else "+00:00"   # GĐ11.59: a naive time in the Vietnam zone is UTC+7, otherwise UTC (as before)
    lo, hi = _dt(b["startTime"]["dateTime"] + (off if "+" not in b["startTime"]["dateTime"] and "Z" not in b["startTime"]["dateTime"] else "")), _dt(b["endTime"]["dateTime"] + (off if "+" not in b["endTime"]["dateTime"] and "Z" not in b["endTime"]["dateTime"] else ""))
    out = []
    for sid in b.get("schedules", []):
        person = sid.split("@")[0]; items = [e for e in PEOPLE.get(person, {}).get("events", [])]; view = ""; t = lo
        while t < hi:
            nxt = t + __import__("datetime").timedelta(minutes=iv)
            view += "2" if any(_dt(e[2]) < nxt and _dt(e[3]) > t for e in items) else "0"; t = nxt
        out.append({"scheduleId": sid, "availabilityView": view, "scheduleItems": [{"status": "busy", "start": {"dateTime": e[2]}, "end": {"dateTime": e[3]}, "subject": e[1]} for e in items]})
    return web.json_response({"value": out})

async def ms_find_times(req):
    who = _who(req, "microsoft", "Calendars.Read.Shared", *CR); b = await req.json(); S["schedule_calls"].append({"who": who, "body": b, "find": True})
    return web.json_response({"meetingTimeSuggestions": [{"confidence": 100, "organizerAvailability": "free", "meetingTimeSlot": {"start": {"dateTime": "2026-10-28T09:00:00", "timeZone": "SE Asia Standard Time"}, "end": {"dateTime": "2026-10-28T10:00:00", "timeZone": "SE Asia Standard Time"}}}],
                              "emptySuggestionsReason": ""})

async def ms_drive_search(req):
    who = _who(req, "microsoft", "Files.Read", "Files.ReadWrite"); term = req.match_info["q"].lower()
    fs = [f for f in PEOPLE[who]["files"] if term in f[1].lower() or term in f[3].lower()]
    return web.json_response({"value": [{"id": f[0], "name": f[1], "size": len(f[3].encode()), "webUrl": f"https://onedrive.fake/{f[0]}",
                                         "lastModifiedDateTime": "2026-10-20T08:00:00Z", "file": {"mimeType": f[2]}} for f in fs]})

async def ms_item(req):
    who = _who(req, "microsoft", "Files.Read", "Files.ReadWrite"); f = next((f for f in PEOPLE[who]["files"] if f[0] == req.match_info["id"]), None)
    if not f: return _err(404, "Item not found", "itemNotFound")
    return web.json_response({"id": f[0], "name": f[1], "size": len(f[3].encode()), "file": {"mimeType": f[2]}})

async def ms_content(req):
    who = _who(req, "microsoft", "Files.Read", "Files.ReadWrite"); f = next((f for f in PEOPLE[who]["files"] if f[0] == req.match_info["id"]), None)
    if not f: return _err(404, "Item not found", "itemNotFound")
    return web.Response(text=f[3], content_type=f[2])

async def ms_range(req):
    who = _who(req, "microsoft", "Files.Read", "Files.ReadWrite")
    return web.json_response({"address": f"{req.match_info['sheet']}!{req.match_info['addr']}", "values": PEOPLE[who]["sheet"]})

# ------------------------------------------------------------------ GĐ11.58 / 11.59 additions (Outlook mail + calendar tools): one attachment, its raw bytes, calendar-scoped routes
async def ms_attachment_one(req):
    """GET /me/messages/{id}/attachments/{aid} (metadata + contentBytes) and .../{aid}/$value (raw bytes). An attachment with `"ref": url` is a referenceAttachment (a OneDrive link)."""
    who = _who(req, "microsoft", *MR); m = _get(who, req.match_info["id"])
    a = next((a for i, a in enumerate(m["atts"] if m else []) if (a.get("id") or f"att-{i}") == req.match_info["aid"]), None)
    if not a: return _err(404, "The specified object was not found in the store.", "ErrorItemNotFound")
    if req.path.endswith("/$value"): return web.Response(body=_bytes(a), content_type="application/octet-stream")
    if a.get("ref"): return web.json_response({"id": req.match_info["aid"], "name": a["name"], "@odata.type": "#microsoft.graph.referenceAttachment", "sourceUrl": a["ref"], "size": 0})
    return web.json_response({"id": req.match_info["aid"], "name": a["name"], "size": len(_bytes(a)), "contentType": a.get("mime"), "@odata.type": "#microsoft.graph.fileAttachment",
                              "contentBytes": base64.b64encode(_bytes(a)).decode()})

def app():
    a = web.Application(middlewares=[_inject], client_max_size=64 * 1024 ** 2)
    g, mgr = "/graph/v1.0/me", "/graph/v1.0/me/messages/{id}"
    a.add_routes([web.get("/{prov}/oauth/authorize", authorize), web.post("/{prov}/oauth/token", token), web.post("/{prov}/oauth/revoke", revoke),
                  web.get("/{prov}/oauth/userinfo", userinfo),
                  web.get("/gmail/v1/users/me/messages", gmail_list), web.get("/gmail/v1/users/me/messages/{id}", gmail_get),
                  web.get("/gmail/v1/users/me/messages/{id}/attachments/{aid}", gmail_attachment),
                  web.post("/gmail/v1/users/me/messages/send", gmail_send), web.post("/upload/gmail/v1/users/me/messages/send", gmail_send_upload),
                  web.post("/gmail/v1/users/me/messages/{id}/modify", gmail_modify), web.post("/gmail/v1/users/me/messages/{id}/trash", gmail_trash),
                  web.post("/gmail/v1/users/me/drafts", gmail_draft), web.post("/upload/gmail/v1/users/me/drafts", gmail_draft_upload), web.get("/gmail/v1/users/me/profile", gmail_profile), web.get("/gmail/v1/users/me/drafts", gmail_drafts_list), web.post("/gmail/v1/users/me/drafts/send", gmail_draft_send), web.get("/gmail/v1/users/me/drafts/{id}", gmail_draft_get),
                  web.get("/gmail/v1/users/me/threads", gmail_threads), web.get("/gmail/v1/users/me/threads/{id}", gmail_thread),
                  web.get("/gmail/v1/users/me/labels", gmail_labels), web.post("/gmail/v1/users/me/labels", gmail_label_new), web.get("/gmail/v1/users/me/history", gmail_history),
                  web.get("/calendar/v3/users/me/calendarList", cal_list), web.get("/calendar/v3/calendars/{cal}/events", cal_events),
                  web.post("/calendar/v3/calendars/{cal}/events", cal_insert), web.post("/calendar/v3/freeBusy", cal_freebusy),
                  web.route("*", "/calendar/v3/calendars/{cal}/events/{id}", cal_event),
                  web.get("/v4/spreadsheets/{sid}/values/{range}", sheet_get), web.put("/v4/spreadsheets/{sid}/values/{range}", sheet_put),
                  web.get("/drive/v3/files", drive_files), web.get("/drive/v3/files/{id}", drive_file),
                  web.get(g + "/messages", ms_messages), web.get(mgr, ms_message), web.patch(mgr, ms_message), web.delete(mgr, ms_message),
                  web.post(g + "/sendMail", ms_send), web.post(g + "/messages", ms_create_draft), web.post(mgr + "/send", ms_draft_send),
                  web.post(mgr + "/{op:createReply|createReplyAll|reply|replyAll|forward}", ms_reply_op), web.post(mgr + "/move", ms_move),
                  web.get(mgr + "/attachments", ms_attachments), web.post(mgr + "/attachments", ms_attachments), web.post(mgr + "/attachments/createUploadSession", ms_upload_session),
                  web.put("/upload-session/{sid}", upload_put),
                  web.get(mgr + "/attachments/{aid}", ms_attachment_one), web.get(mgr + "/attachments/{aid}/$value", ms_attachment_one),
                  web.get(g + "/calendars/{cid}/calendarView", ms_calview), web.post(g + "/calendars/{cid}/events", ms_event_new),
                  web.get(g + "/mailFolders", ms_folders), web.get(g + "/mailFolders/{pid}/childFolders", ms_folders),
                  web.get(g + "/mailFolders/{folder}/messages", ms_messages), web.get(g + "/mailFolders('{folder}')/messages/delta", ms_delta),
                  web.get(g + "/calendarView", ms_calview), web.get(g + "/calendars", ms_calendars), web.post(g + "/events", ms_event_new),
                  web.route("*", g + "/events/{id}", ms_event), web.post(g + "/events/{id}/{op:accept|decline|tentativelyAccept|cancel}", ms_event_op),
                  web.post(g + "/calendar/getSchedule", ms_schedule), web.post(g + "/findMeetingTimes", ms_find_times)])   # OneDrive routes: tests/fakes/fake_graph_files.py (12.82a)
    helpers = {"who": _who, "err": _err, "GA": GA}
    for m in _mods(): m.routes(a, S, helpers)
    return a

def start_in_thread(port=PORT):
    """Starts the fake in a daemon thread (tests). Returns the stop function."""
    ready = threading.Event(); st = {}
    def run():
        loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop); st["loop"] = loop
        runner = web.AppRunner(app()); loop.run_until_complete(runner.setup()); st["runner"] = runner
        loop.run_until_complete(web.TCPSite(runner, "127.0.0.1", port).start()); ready.set(); loop.run_forever()
    threading.Thread(target=run, daemon=True).start(); assert ready.wait(10), "fake workspace API did not start"
    def stop():
        asyncio.run_coroutine_threadsafe(st["runner"].cleanup(), st["loop"]).result(10); st["loop"].call_soon_threadsafe(st["loop"].stop)
    return stop

if __name__ == "__main__":
    web.run_app(app(), host="127.0.0.1", port=PORT)
