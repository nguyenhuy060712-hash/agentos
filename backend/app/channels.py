"""GĐ8.1 Channel framework (23/10/2026): an agent answers on external chat channels (Telegram / Zalo OA / Slack come in 8.2-8.4).
Pattern of langchain-ai/langgraph-messaging-integrations (parse incoming -> run the agent on one thread per chat -> send reply),
our own code on top of the existing run queue (server._create_run): every channel message is a normal run => traced + costed like chat
(run metadata channel=<kind>, gateway feature 'channel'). Interrupts become Approve/Reject buttons (adapter.send_approval); the click
resumes the run with the same command shape as the Inbox (studio.appr_decide), as the user who created the binding.

Tables (workspace_id + RLS, admin.RLS_DIRECT): channel_bindings (agent <-> channel), channel_threads (binding + chat_key -> lg thread).
Adapters: one small shape (Incoming + Adapter.parse_incoming/send_text/send_approval), registry ADAPTERS[kind]. Built-in 'fake' adapter
keeps an in-memory outbox (tests + the "Thử" simulator on the agent page). Demo workspace / demo accounts: feature off."""
import asyncio, contextlib, json, re, time, uuid
from dataclasses import dataclass, field
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb

FEATURE = "channel"
DEFAULT_TIMEOUT = 300
REJECT_MSG = "Người dùng từ chối thao tác này."
ERR_TEXT = "Xin lỗi, trợ lý đang gặp sự cố và chưa trả lời được. Anh/chị vui lòng thử lại sau ít phút."
BUDGET_TEXT = "Xin lỗi, trợ lý đã dùng hết hạn mức chi phí hôm nay. Anh/chị vui lòng thử lại sau."
SLOW_TEXT = "Trợ lý đang xử lý lâu hơn dự kiến. Anh/chị vui lòng chờ hoặc nhắn lại sau ít phút."
EMPTY_TEXT = "(Trợ lý không có nội dung trả lời.)"
FILE_TEXT = "Không nhận được tệp: {}"
RECONNECT_TEXT = "Tài khoản đã kết nối (Google, Microsoft…) hết hạn hoặc bị thu hồi. Anh/chị nhờ quản trị viên kết nối lại rồi nhắn lại giúp em."   # GĐ12.78: one sentence per kind of failure
SCOPE_TEXT = "Tài khoản đã kết nối chưa được cấp đủ quyền cho việc này. Anh/chị nhờ quản trị viên kết nối lại với quyền rộng hơn."
CONNECT_ADMIN_TEXT = "Trợ lý cần kết nối tài khoản {} nhưng tài khoản này không phải của người dùng kênh. Anh/chị nhờ quản trị viên kết nối tài khoản giúp em."
PROMPT_DONE_TEXT = "Biểu mẫu này đã được trả lời hoặc đã đóng."
PROMPT_EXPIRED_TEXT = "Biểu mẫu này đã hết hạn (quá 24 giờ). Anh/chị nhắn lại để trợ lý hỏi lại."
CONNECT_WAIT_TEXT = "Chưa thấy kết nối, hãy bấm Kết nối trước rồi bấm lại."
CONNECT_FAIL_TEXT = "Phiên kết nối đã hết hạn hoặc không thành công. Anh/chị nhắn lại để trợ lý gửi liên kết mới."
PROMPT_TTL_H = 24

router = APIRouter()
DDL = """
CREATE TABLE IF NOT EXISTS channel_bindings(id SERIAL PRIMARY KEY, workspace_id INT NOT NULL DEFAULT 1, agent_id INT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', secret_ref TEXT, status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
  allowed_users TEXT[] NOT NULL DEFAULT '{}', config JSONB NOT NULL DEFAULT '{}', created_by INT REFERENCES users(id) ON DELETE SET NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS channel_bindings_agent ON channel_bindings(agent_id);
CREATE TABLE IF NOT EXISTS channel_threads(id SERIAL PRIMARY KEY, workspace_id INT NOT NULL DEFAULT 1,
  binding_id INT NOT NULL REFERENCES channel_bindings(id) ON DELETE CASCADE, chat_key TEXT NOT NULL, thread_id TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(binding_id, chat_key));
"""

async def _q(*a, **k):   # lazy: unit tests do not import the whole app
    import deepagent_api as A
    return await A.q(*a, **k)

async def init(): await _q(DDL)

# ------------------------------------------------------------------ adapter shape
@dataclass
class Incoming:
    chat_id: str
    user_id: str
    user_name: str = ""
    text: str = ""
    files: list = field(default_factory=list)   # adapter-specific file refs (8.2+: through the 6.8 docfiles path)
    is_group: bool = False
    mentioned: bool = False
    per_chat: bool = False      # GĐ8.4: one agent thread per chat_id even 1:1 (Slack: chat_id = "<channel>:<thread_ts>")

class Adapter:
    """One channel kind. parse_incoming: raw platform payload -> Incoming (None = not a message for the agent).
    send_text / send_approval: deliver to the chat; `binding` is the channel_bindings row (secret_ref -> ws_secrets.get_secret)."""
    kind = ""
    def available(self) -> bool:
        """False = the kind is switched off (e.g. 8.3b needs an env flag): hidden from the list, creating it is refused (422)."""
        return True
    # Optional hooks (8.3b), looked up with getattr:  async prepare_create(user, BindingIn) -> {"secret_ref":..., "config":...} (may raise HTTPException),
    # async guard_patch(user, row, BindingPatch), async after_delete(row)
    def parse_incoming(self, raw) -> "Incoming | None": raise NotImplementedError
    async def send_text(self, binding: dict, chat_id: str, text: str): raise NotImplementedError
    async def send_approval(self, binding: dict, chat_id: str, approval_id: int, summary: str): raise NotImplementedError
    # Optional (9.P2): async send_media(binding, chat_id, items) with items = [{"kind": "image"|"file", "name", "mime", "data": bytes}] -> files the agent
    # produced (reply text references /api/files/<id>) are delivered as real image/file messages; adapters without it keep getting the text as is.
    # Optional (GĐ12.74/12.78), looked up with getattr; adapters without them keep the old error text for such a pause:
    #   async send_ask(binding, chat_id, ask, ctx) -> a question form ({title, note, fields, tool_call_id}); async send_connect(binding, chat_id, connect, ctx) -> a sign-in
    #   button ({connect_url, auth_id, service, message}); ctx = {"thread_id", "run_id"}. async prompt_closed(binding, prompt_row, text) edits the posted message after a typed reply.
    async def fetch_files(self, binding: dict, msg: "Incoming") -> list:
        """Incoming.files -> standard content blocks {"type": "file"|"image", "url": "/api/files/<id>"} (runs as the binding owner; 8.2+ adapters
        download + attachments.store). Default: refs that already are such blocks. HTTP 413/415/422 -> the user gets FILE_TEXT."""
        return [f for f in msg.files if isinstance(f, dict) and f.get("type") in ("file", "image") and f.get("url")]

class FakeAdapter(Adapter):
    """In-memory channel: outgoing messages land in self.outbox (tests, UI simulator). No network."""
    kind = "fake"
    def __init__(self): self.outbox: list = []
    def parse_incoming(self, raw):
        if not isinstance(raw, dict) or not (raw.get("text") or raw.get("files")): return None
        uid = str(raw.get("user_id") or "")
        return Incoming(chat_id=str(raw.get("chat_id") or uid), user_id=uid, user_name=str(raw.get("user_name") or ""), text=str(raw.get("text") or ""),
                        files=list(raw.get("files") or []), is_group=bool(raw.get("is_group")), mentioned=bool(raw.get("mentioned")))
    async def send_text(self, binding, chat_id, text):
        self.outbox.append({"binding_id": binding.get("id"), "chat_id": chat_id, "type": "text", "text": text})
    async def send_media(self, binding, chat_id, items):
        for it in items: self.outbox.append({"binding_id": binding.get("id"), "chat_id": chat_id, "type": it["kind"], "name": it["name"], "mime": it["mime"], "size": len(it["data"])})
    async def send_ask(self, binding, chat_id, ask, ctx):   # GĐ12.74: the simulator lists the form (no prompt row: nobody can answer it here)
        self.outbox.append({"binding_id": binding.get("id"), "chat_id": chat_id, "type": "ask", "text": ask.get("title"), "fields": len(ask.get("fields") or [])})
    async def send_connect(self, binding, chat_id, con, ctx):   # GĐ12.78
        self.outbox.append({"binding_id": binding.get("id"), "chat_id": chat_id, "type": "connect", "text": con.get("service"), "url": con.get("connect_url")})
    async def send_approval(self, binding, chat_id, approval_id, summary):
        self.outbox.append({"binding_id": binding.get("id"), "chat_id": chat_id, "type": "approval", "approval_id": approval_id, "text": summary,
                            "buttons": [{"label": "Duyệt", "approve": True}, {"label": "Từ chối", "approve": False}]})

ADAPTERS: dict = {"fake": FakeAdapter()}
def register(adapter: Adapter): ADAPTERS[adapter.kind] = adapter

# ------------------------------------------------------------------ helpers
def chat_key(msg: Incoming) -> str: return f"g:{msg.chat_id}" if msg.is_group or msg.per_chat else f"u:{msg.user_id}"

def user_allowed(binding: dict, user_id, user_name="") -> bool:
    """allowed_users empty = everyone; else the external user id or name (case-insensitive, leading @ ignored) must be listed."""
    allow = [str(x).strip().lstrip("@").lower() for x in (binding.get("allowed_users") or []) if str(x).strip()]
    if not allow: return True
    return str(user_id).strip().lower() in allow or (bool(user_name) and str(user_name).strip().lstrip("@").lower() in allow)

def summarize(payload) -> str:
    """Short text of the pending action(s) of an approvals row: 'content_publish(channel=facebook, title=…)'."""
    out = []
    for p in payload or []:
        for ar in (p.get("action_requests") or []) if isinstance(p, dict) else []:
            args = ", ".join(f"{k}={str(v)[:60]}" for k, v in (ar.get("args") or {}).items())
            out.append(f"{ar.get('name')}({args})")
    s = "; ".join(out) or "một thao tác cần duyệt"
    return "Trợ lý cần anh/chị duyệt trước khi làm: " + (s[:400] + "…" if len(s) > 400 else s)

def _timeout(binding):
    try: return max(5, min(900, int((binding.get("config") or {}).get("timeout_s") or DEFAULT_TIMEOUT)))
    except (TypeError, ValueError): return DEFAULT_TIMEOUT

def _log(binding_id, what): print(f"channels: binding {binding_id}: {what}")

@contextlib.contextmanager
def _system():
    import admin
    t = admin.USER.set(None)
    try: yield
    finally: admin.USER.reset(t)

async def _binding(bid):
    with _system():   # inbound webhooks/pollers have no session: read the binding as a system task, then act as its creator
        return await _q("SELECT * FROM channel_bindings WHERE id=%s", (bid,), one=True)

@contextlib.asynccontextmanager
async def as_owner(b):
    """Act as the binding's creator in the binding's workspace (RLS, per-user caps, traces), restored afterwards."""
    import admin
    with _system():
        u = await _q("""SELECT u.id, u.username, u.disabled, u.is_superadmin, u.demo, u.daily_cap_usd, u.demo_agents, m.role FROM users u
                        LEFT JOIN memberships m ON m.user_id=u.id AND m.workspace_id=%s WHERE u.id=%s""", (b["workspace_id"], b.get("created_by")), one=True)
    if not u or u["disabled"] or not (u["role"] or u["is_superadmin"]): raise PermissionError("binding owner is not a member of the workspace any more")
    row = dict(u)
    ident = {"id": row["id"], "username": row["username"], "role": row["role"] or "admin", "ws": b["workspace_id"], "auth": "channel"}
    tok = admin.USER.set(admin._with_demo(ident, row))
    try: yield admin.USER.get()
    finally: admin.USER.reset(tok)

def _off(ws) -> bool:
    import gateway as gw
    return bool(ws and int(ws) in gw.DEMO_WS)

async def _thread_for(b, key):
    """One lg thread per (binding, chat_key), created on the first message (owned by the binding creator)."""
    tid = f"ch{b['id']}-{uuid.uuid4().hex[:16]}"
    await _q("INSERT INTO channel_threads(workspace_id,binding_id,chat_key,thread_id) VALUES(%s,%s,%s,%s) ON CONFLICT (binding_id, chat_key) DO NOTHING",
             (b["workspace_id"], b["id"], key, tid))
    tid = (await _q("SELECT thread_id FROM channel_threads WHERE binding_id=%s AND chat_key=%s", (b["id"], key), one=True))["thread_id"]
    await _q("INSERT INTO lg_threads(thread_id,workspace_id,metadata) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
             (tid, b["workspace_id"], Jsonb({"source": "channel", "channel": b["kind"], "binding_id": b["id"], "chat_key": key})))
    return tid

async def _wait(rid, timeout):
    import server
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        r = await _q("SELECT run_id, thread_id, status, output, error FROM lg_runs WHERE run_id=%s", (rid,), one=True)
        if not r: return {"status": "error", "output": None, "error": "run deleted"}
        if r["status"] in server.TERMINAL: return r
        await asyncio.sleep(0.4)
    return {"status": "timeout", "output": None, "error": None, "run_id": rid}

LATE_TIMEOUT = 1800   # after SLOW_TEXT keep waiting this long, then deliver the real answer
_LATE: set = set()    # strong refs so late-delivery tasks are not garbage-collected

async def _late(ad, b, chat_id, rid):
    try:
        run = await _wait(rid, LATE_TIMEOUT)
        if run.get("status") != "timeout": await _deliver(ad, b, chat_id, run)
        else: _log(b["id"], f"run {rid} still not finished after late wait; no reply sent")
    except Exception as e:  # noqa: BLE001
        _log(b["id"], f"late delivery failed: {type(e).__name__}")
    finally: _LATE.discard(asyncio.current_task())

async def _run(b, tid, assistant_id, ext_user, input=None, command=None):
    import server
    r = await server._create_run(tid, server.RunIn(assistant_id=assistant_id, input=input, command=command, if_not_exists="create",
                                                   metadata={"channel": b["kind"], "binding_id": b["id"], "external_user": str(ext_user)[:120]}))
    return str(r["run_id"]), await _wait(str(r["run_id"]), _timeout(b))

FILE_REF = re.compile(r"(!?)\[([^\]\n]*)\]\(\s*(?:https?://[^\s)/]+)?/api/files/([0-9a-fA-F-]{36})\s*\)|(?<![\w/.-])(?:https?://[^\s)/]+)?/api/files/([0-9a-fA-F-]{36})")
MAX_MEDIA = 5

def out_files_of(text) -> list:
    """GĐ12.77: chat file ids in the `[aos-files] {"files": [...]}` line a tool result carries after the agent wrote into /workspace/out."""
    m = re.search(r"\[aos-files\]\s*(\{.*\})", str(text or ""))
    try: files = (json.loads(m.group(1)) or {}).get("files") if m else []
    except ValueError: files = []
    return [str(f["id"]).lower() for f in files or [] if isinstance(f, dict) and re.fullmatch(r"[0-9a-fA-F-]{36}", str(f.get("id") or ""))]

async def _extract_media(ad, text: str, extra_ids=()):
    """GĐ9.P2: (items, text without the file references) for adapters that can send media. Only files the binding owner may read (RLS) are taken; the rest stay as text.
    GĐ12.77: `extra_ids` = files the agent wrote to /workspace/out in this run; they are sent too when the reply does not already link them (the model rarely writes the link)."""
    if not getattr(ad, "send_media", None) or ("/api/files/" not in (text or "") and not extra_ids): return [], text
    import attachments as AT
    items, seen = [], set()
    for fid in extra_ids:
        fid = str(fid).lower()
        if fid in seen or len(items) >= MAX_MEDIA or fid in (text or "").lower(): continue
        seen.add(fid)
        try:
            r = await AT._row(fid); data = AT._bytes(r)
        except Exception:  # noqa: BLE001
            continue
        items.append({"kind": "image" if str(r["mime_type"]).startswith("image/") else "file", "name": r.get("name") or fid, "mime": r["mime_type"], "data": data, "id": fid})
    n_extra = len(items)
    def sub(m):
        fid = (m.group(3) or m.group(4)).lower()
        return "\x00" if (m.group(1) == "!" or m.group(4)) else (m.group(2) or "")
    for m in FILE_REF.finditer(text):
        fid = (m.group(3) or m.group(4)).lower()
        if fid in seen or len(items) >= MAX_MEDIA: continue
        seen.add(fid)
        try:
            r = await AT._row(fid); data = AT._bytes(r)
        except Exception:  # noqa: BLE001  (not visible / gone: leave the text alone)
            continue
        items.append({"kind": "image" if str(r["mime_type"]).startswith("image/") else "file", "name": r.get("name") or fid, "mime": r["mime_type"], "data": data, "id": fid})
    if not items: return [], text
    ok = {i["id"] for i in items[n_extra:]}   # only linked files are cut out of the text
    out = FILE_REF.sub(lambda m: sub(m) if (m.group(3) or m.group(4)).lower() in ok else m.group(0), text)
    out = re.sub(r" ?\x00 ?", lambda m: " " if m.group(0).startswith(" ") and m.group(0).endswith(" ") else "", out)   # a removed reference leaves no double space
    return items, re.sub(r"\n{3,}", "\n\n", out).strip()

# ------------------------------------------------------------------ GĐ12.74 / 12.78: pauses that are not Inbox approvals (question form, sign-in) and their `channel_prompts` rows
def pause_info(reqs) -> dict:
    """Interrupt payloads of a paused run -> {"ask": {...}} | {"connect": {...}} for lg_runs.output (server._execute); {} for anything else. The first one of each kind wins."""
    out = {}
    for v in reqs or []:
        if not isinstance(v, dict): continue
        t = v.get("type")
        if t == "ask_user" and "ask" not in out and isinstance(v.get("fields"), list):
            out["ask"] = {"title": str(v.get("title") or "")[:160], "note": str(v.get("note") or "")[:300], "fields": v["fields"][:8], "tool_call_id": str(v.get("tool_call_id") or "")[:100]}
        elif t == "auth_required" and "connect" not in out:
            out["connect"] = {"connect_url": str(v.get("auth_url") or "")[:2000], "auth_id": str(v.get("auth_id") or "")[:200], "service": str(v.get("tool") or "")[:80], "message": str(v.get("message") or "")[:300]}
        elif t == "credential_authorization_required" and "connect" not in out and v.get("credentials"):
            c = (v["credentials"] or [{}])[0] or {}
            out["connect"] = {"connect_url": str(c.get("connect_url") or "")[:2000], "auth_id": str(c.get("auth_id") or "")[:200], "service": str(c.get("display_name") or c.get("slug") or "")[:80], "message": ""}
    return out

async def _may_connect_here(con) -> bool:
    """The run acts as the binding owner (as_owner): the sign-in session of this pause must be one that person may complete (own account; an agent / workspace account only an admin)."""
    import admin, connection_runtime as R
    s = await _q("SELECT owner_type, owner_id FROM auth_sessions WHERE auth_id=%s", (con.get("auth_id") or "",), one=True) if con.get("auth_id") else None
    return True if not s else bool(R._may_connect(admin.cur() or {}, (s["owner_type"], s["owner_id"])))

async def new_prompt(b, chat_id, ctx, kind, payload) -> int:
    return (await _q("""INSERT INTO channel_prompts(workspace_id,binding_id,kind,thread_id,run_id,chat_id,payload,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,now()+make_interval(hours=>%s)) RETURNING id""",
                     (b["workspace_id"], b["id"], kind, (ctx or {}).get("thread_id"), (ctx or {}).get("run_id"), chat_id, Jsonb(payload), PROMPT_TTL_H), one=True))["id"]

async def set_prompt_ts(pid, ts): await _q("UPDATE channel_prompts SET message_ts=%s WHERE id=%s", (str(ts), int(pid)))

async def load_prompt(bid, pid, kind):
    """The prompt row of THIS binding (system read: the Slack listeners have no session) -> (row | None, 'ok' | 'answered' | 'expired' | 'gone')."""
    with _system():
        p = await _q("SELECT * FROM channel_prompts WHERE id=%s AND binding_id=%s AND kind=%s", (int(pid), int(bid), kind), one=True)
    if not p: return None, "gone"
    if p["status"] != "pending": return p, "answered"
    return p, ("ok" if p["expires_at"].timestamp() > time.time() else "expired")

async def _claim(pid, status, by="") -> dict | None:
    return await _q("UPDATE channel_prompts SET status=%s, answered_by=%s WHERE id=%s AND status='pending' AND expires_at>now() RETURNING *", (status, str(by)[:120], int(pid)), one=True)

async def _close_prompt(pid, status): await _q("UPDATE channel_prompts SET status=%s WHERE id=%s AND status='pending'", (status, int(pid)))

async def _assistant_of(b, thread_id) -> str:
    t = await _q("SELECT assistant_id, agent_id FROM lg_threads WHERE thread_id=%s", (thread_id,), one=True) or {}
    return str(t.get("assistant_id") or f"agent-{t.get('agent_id') or b['agent_id']}")

async def _supersede(ad, b, tid, user_id):
    """A new human message while a question / sign-in prompt of this thread is open = decline it (the paused run is resumed with declined, its reply is dropped), then the message runs normally."""
    rows = await _q("SELECT * FROM channel_prompts WHERE binding_id=%s AND thread_id=%s AND status='pending' ORDER BY id", (b["id"], tid))
    for p in rows or []:
        if not await _q("UPDATE channel_prompts SET status='superseded' WHERE id=%s AND status='pending' RETURNING id", (p["id"],), one=True): continue
        try: await _run(b, tid, await _assistant_of(b, tid), user_id, command={"resume": {"declined": True} if p["kind"] == "ask" else {"cancel": True, "authorized": False}})
        except Exception as e:  # noqa: BLE001
            _log(b["id"], f"declining prompt {p['id']} failed: {type(e).__name__}")
        if getattr(ad, "prompt_closed", None):
            try: await ad.prompt_closed(b, p, "Đã bỏ qua vì có tin nhắn mới")
            except Exception as e:  # noqa: BLE001
                _log(b["id"], f"prompt_closed failed: {type(e).__name__}")

async def _deliver(ad, b, chat_id, run):
    """Terminal run -> outgoing message. interrupted + approval -> buttons; error -> short polite text (never a stack trace)."""
    st, out = run.get("status"), run.get("output") or {}
    if st == "interrupted" and out.get("approval_id"):
        ap = await _q("SELECT payload FROM approvals WHERE id=%s", (out["approval_id"],), one=True)
        await ad.send_approval(b, chat_id, int(out["approval_id"]), summarize((ap or {}).get("payload")))
        return "approval"
    ctx = {"thread_id": run.get("thread_id"), "run_id": run.get("run_id")}
    if st == "interrupted" and isinstance(out.get("ask"), dict) and getattr(ad, "send_ask", None):   # GĐ12.74: ask_user pause -> a form the adapter can show
        await ad.send_ask(b, chat_id, out["ask"], ctx); return "ask"
    if st == "interrupted" and isinstance(out.get("connect"), dict) and getattr(ad, "send_connect", None):   # GĐ12.78: sign-in pause -> Connect button
        con = out["connect"]
        if not await _may_connect_here(con):
            await ad.send_text(b, chat_id, CONNECT_ADMIN_TEXT.format(con.get("service") or "?")); return "connect_refused"
        await ad.send_connect(b, chat_id, con, ctx); return "connect"
    if st == "success":
        media, reply = await _extract_media(ad, out.get("reply") or EMPTY_TEXT, out.get("files") or ())
        if reply or not media: await ad.send_text(b, chat_id, reply or EMPTY_TEXT)
        if media: await ad.send_media(b, chat_id, media)
        return "replied"
    if st == "timeout":
        await ad.send_text(b, chat_id, SLOW_TEXT)
        if run.get("run_id"): _LATE.add(asyncio.create_task(_late(ad, b, chat_id, run["run_id"])))   # context (owner identity) is copied into the task
        return "timeout"
    _log(b["id"], f"run ended {st}: {str(run.get('error') or '')[:200]}")
    await ad.send_text(b, chat_id, err_text(run.get("error"))); return "error"

def err_text(err) -> str:
    """GĐ12.78: one polite sentence per kind of failure that can reach a channel (a lost / revoked sign-in, a missing permission); everything else = ERR_TEXT."""
    low = str(err or "").lower()
    if "needs_reconnect" in low or "reconnect" in low: return RECONNECT_TEXT
    if "scope_missing" in low or "missing permission" in low or "insufficient_scope" in low: return SCOPE_TEXT
    return ERR_TEXT

async def _fail(ad, b, chat_id, e):
    from fastapi import HTTPException as _HE
    _log(b["id"], f"{type(e).__name__}: {str(getattr(e, 'detail', e))[:200]}")
    try: await ad.send_text(b, chat_id, BUDGET_TEXT if isinstance(e, _HE) and e.status_code == 402 else err_text(e.detail if isinstance(e, _HE) else None))
    except Exception as e2:  # noqa: BLE001
        _log(b["id"], f"send failed: {type(e2).__name__}")
    return {"status": "error"}

def _gate(b, user_id, user_name=""):
    """None = go on; else the reason the message is ignored (silently, logged)."""
    if not b: return "no binding"
    if b["status"] != "active": return "binding disabled"
    if _off(b["workspace_id"]): return "demo workspace"
    if not user_allowed(b, user_id, user_name): return f"user {str(user_id)[:40]} not in allowed_users"
    return None

# ------------------------------------------------------------------ entry points (adapters call these)
async def handle_incoming(binding_id: int, msg: Incoming, adapter: Adapter | None = None) -> dict:
    """External message -> agent run on the chat's thread -> reply / approval buttons. Returns {"status": ignored|replied|approval|timeout|error, ...}."""
    b = await _binding(binding_id)
    why = _gate(b, msg.user_id, msg.user_name)
    if not why and msg.is_group and not msg.mentioned: why = "group message without mention"
    if not why and not (msg.text or "").strip() and not msg.files: why = "empty message"
    if why:
        _log(binding_id, "ignored: " + why); return {"status": "ignored", "reason": why}
    ad = adapter or ADAPTERS.get(b["kind"])
    if not ad:
        _log(binding_id, f"no adapter for kind {b['kind']}"); return {"status": "ignored", "reason": "no adapter"}
    try:
        async with as_owner(b):
            tid = await _thread_for(b, chat_key(msg))
            await _supersede(ad, b, tid, msg.user_id)
            content = msg.text[:8000]
            if msg.files:   # GĐ8.2: files -> standard blocks, same run input shape as the chat (server normalises + attaches them to the thread)
                try: blocks = await ad.fetch_files(b, msg)
                except HTTPException as e:
                    if e.status_code not in (413, 415, 422): raise
                    _log(binding_id, f"file refused: {str(e.detail)[:200]}")
                    await ad.send_text(b, msg.chat_id, FILE_TEXT.format(str(e.detail)[:300])); return {"status": "error", "reason": "file"}
                if blocks: content = ([{"type": "text", "text": content}] if content.strip() else []) + blocks
            rid, run = await _run(b, tid, f"agent-{b['agent_id']}", msg.user_id, input={"messages": [{"type": "human", "content": content}]})
            st = await _deliver(ad, b, msg.chat_id, run)
            return {"status": st, "thread_id": tid, "run_id": rid, "approval_id": (run.get("output") or {}).get("approval_id") if st == "approval" else None}
    except Exception as e:  # noqa: BLE001
        return await _fail(ad, b, msg.chat_id, e)

async def handle_decision(binding_id: int, chat_id: str, user_id: str, approval_id: int, approve: bool, adapter: Adapter | None = None, user_name: str = "") -> dict:
    """Approve/Reject button: the approval must belong to this binding's thread for this chat; resumes the run like the Inbox does."""
    b = await _binding(binding_id)
    why = _gate(b, user_id, user_name)
    if why:
        _log(binding_id, "decision ignored: " + why); return {"status": "ignored", "reason": why}
    ad = adapter or ADAPTERS.get(b["kind"])
    if not ad: return {"status": "ignored", "reason": "no adapter"}
    try:
        async with as_owner(b):
            ap = await _q("SELECT id, thread_id, status, payload FROM approvals WHERE id=%s", (int(approval_id),), one=True)
            th = ap and await _q("SELECT thread_id FROM channel_threads WHERE binding_id=%s AND thread_id=%s AND chat_key = ANY(%s)",
                                 (b["id"], ap["thread_id"], [f"g:{chat_id}", f"u:{user_id}"]), one=True)
            if not th:
                _log(binding_id, f"decision ignored: approval {approval_id} is not from this chat"); return {"status": "ignored", "reason": "approval not in this chat"}
            latest = await _q("SELECT id FROM approvals WHERE thread_id=%s AND status='pending' ORDER BY id DESC LIMIT 1", (ap["thread_id"],), one=True)
            if ap["status"] != "pending" or not latest or latest["id"] != ap["id"]:
                await ad.send_text(b, chat_id, "Yêu cầu này đã được xử lý hoặc đã cũ."); return {"status": "stale"}
            reqs = [ar for p in (ap["payload"] or []) for ar in (p.get("action_requests") or [])] or [None]
            decisions = [{"type": "approve"} if approve else {"type": "reject", "message": REJECT_MSG} for _ in reqs]
            t = await _q("SELECT assistant_id, agent_id FROM lg_threads WHERE thread_id=%s", (ap["thread_id"],), one=True) or {}
            aid = str(t.get("assistant_id") or f"agent-{t.get('agent_id') or b['agent_id']}")
            rid, run = await _run(b, ap["thread_id"], aid, user_id, command={"resume": {"decisions": decisions}})
            st = await _deliver(ad, b, chat_id, run)
            return {"status": st, "thread_id": ap["thread_id"], "run_id": rid, "approval_id": (run.get("output") or {}).get("approval_id") if st == "approval" else None}
    except Exception as e:  # noqa: BLE001
        return await _fail(ad, b, chat_id, e)

async def _prompt_gate(binding_id, user_id, user_name, adapter):
    b = await _binding(binding_id)
    why = _gate(b, user_id, user_name)
    if why:
        _log(binding_id, "prompt answer ignored: " + why); return b, None, {"status": "ignored", "reason": why}
    ad = adapter or ADAPTERS.get(b["kind"])
    return (b, ad, None) if ad else (b, None, {"status": "ignored", "reason": "no adapter"})

async def handle_answer(binding_id: int, prompt_id: int, user_id: str, answers: dict, adapter: Adapter | None = None, user_name: str = "") -> dict:
    """GĐ12.74: the question form was submitted. The FIRST valid answer wins (atomic claim); the paused run resumes with {"answers": …} as the binding owner, like a button."""
    b, ad, bad = await _prompt_gate(binding_id, user_id, user_name, adapter)
    if bad: return bad
    p = None
    try:
        async with as_owner(b):
            p = await _q("SELECT * FROM channel_prompts WHERE id=%s AND binding_id=%s AND kind='ask'", (int(prompt_id), b["id"]), one=True)
            if not p: return {"status": "ignored", "reason": "unknown prompt"}
            if not await _claim(p["id"], "answered", user_id): return {"status": "stale"}
            rid, run = await _run(b, p["thread_id"], await _assistant_of(b, p["thread_id"]), user_id, command={"resume": {"answers": dict(answers or {})}})
            st = await _deliver(ad, b, p["chat_id"], run)
            return {"status": st, "thread_id": p["thread_id"], "run_id": rid}
    except Exception as e:  # noqa: BLE001
        return await _fail(ad, b, (p or {}).get("chat_id") or "", e)

async def handle_connect(binding_id: int, prompt_id: int, user_id: str, adapter: Adapter | None = None, user_name: str = "") -> dict:
    """GĐ12.78: "Đã kết nối — chạy tiếp". Resumes only when the sign-in session is completed (a pending one changes nothing); no session row (a service connected on the
    Settings page) = go on, the tool retries once and says by itself if it is still missing."""
    b, ad, bad = await _prompt_gate(binding_id, user_id, user_name, adapter)
    if bad: return bad
    p = None
    try:
        async with as_owner(b):
            p = await _q("SELECT * FROM channel_prompts WHERE id=%s AND binding_id=%s AND kind='connect'", (int(prompt_id), b["id"]), one=True)
            if not p: return {"status": "ignored", "reason": "unknown prompt"}
            if p["status"] != "pending": await ad.send_text(b, p["chat_id"], PROMPT_DONE_TEXT); return {"status": "stale"}
            if p["expires_at"].timestamp() <= time.time(): await _close_prompt(p["id"], "expired"); await ad.send_text(b, p["chat_id"], PROMPT_EXPIRED_TEXT); return {"status": "expired"}
            aid = (p["payload"] or {}).get("auth_id")
            s = await _q("SELECT status, expires FROM auth_sessions WHERE auth_id=%s AND user_id=%s", (aid, b["created_by"]), one=True) if aid else None
            if s and s["status"] == "pending" and s["expires"].timestamp() < time.time(): s["status"] = "expired"
            if s and s["status"] == "pending": await ad.send_text(b, p["chat_id"], CONNECT_WAIT_TEXT); return {"status": "waiting"}
            if s and s["status"] != "completed":
                await _close_prompt(p["id"], "expired"); await ad.send_text(b, p["chat_id"], CONNECT_FAIL_TEXT); return {"status": "expired"}
            if not await _claim(p["id"], "answered", user_id): return {"status": "stale"}
            rid, run = await _run(b, p["thread_id"], await _assistant_of(b, p["thread_id"]), user_id, command={"resume": {"authorized": True}})
            st = await _deliver(ad, b, p["chat_id"], run)
            return {"status": st, "thread_id": p["thread_id"], "run_id": rid}
    except Exception as e:  # noqa: BLE001
        return await _fail(ad, b, (p or {}).get("chat_id") or "", e)

# ------------------------------------------------------------------ API
NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
COLS = "id, workspace_id, agent_id, kind, name, secret_ref, status, allowed_users, config, created_by, created_at"

class BindingIn(BaseModel):
    kind: str = Field(min_length=1, max_length=20)
    name: str = Field(default="", max_length=80)
    secret_ref: str | None = Field(default=None, max_length=64)
    allowed_users: list[str] = Field(default_factory=list, max_length=200)
    config: dict = Field(default_factory=dict)

class BindingPatch(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    secret_ref: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, pattern=r"^(active|disabled)$")
    allowed_users: list[str] | None = Field(default=None, max_length=200)
    config: dict | None = None

class Decision(BaseModel):
    approval_id: int
    approve: bool

class SimIn(BaseModel):
    text: str = Field(default="", max_length=8000)
    user_id: str = Field(default="sim-user", min_length=1, max_length=120)
    user_name: str = Field(default="", max_length=120)
    chat_id: str | None = Field(default=None, max_length=120)
    is_group: bool = False
    mentioned: bool = False
    decision: Decision | None = None

def _need_write():
    import admin
    u = admin.cur()
    if not u or admin.ROLES.get(u.get("role"), 0) < admin.ROLES["editor"]: raise HTTPException(403, "Cần quyền editor")
    if admin.is_demo(u): raise HTTPException(403, "Tài khoản demo chỉ được xem")
    if _off(admin.cur_ws()): raise HTTPException(403, "Kênh nhắn tin không bật cho workspace demo")
    return u

def _users(xs): return list(dict.fromkeys(x.strip()[:120] for x in xs or [] if x and x.strip()))

def _config(c):
    if len(str(c)) > 4000: raise HTTPException(422, "config quá lớn (tối đa 4000 ký tự)")
    if "timeout_s" in c:
        try: c["timeout_s"] = max(5, min(900, int(c["timeout_s"])))
        except (TypeError, ValueError): raise HTTPException(422, "config.timeout_s phải là số giây") from None
    return c

async def _secret_ok(name):
    if name is None or name == "": return None
    import admin
    if not NAME_RE.match(name): raise HTTPException(422, "secret_ref: tên bí mật không hợp lệ")
    if not await _q("SELECT 1 FROM ws_secrets WHERE workspace_id=%s AND name=%s", (admin.cur_ws(), name), one=True): raise HTTPException(422, f"Không có bí mật {name} trong workspace")
    return name

async def _agent(aid):
    import admin
    a = await _q("SELECT id FROM agents WHERE id=%s AND NOT deleted AND NOT hidden AND workspace_id=%s", (aid, admin.cur_ws()), one=True)
    if not a: raise HTTPException(404, "Không có agent")
    return a

async def _get(bid):
    import admin
    r = await _q(f"SELECT {COLS} FROM channel_bindings WHERE id=%s AND workspace_id=%s", (bid, admin.cur_ws()), one=True)
    if not r: raise HTTPException(404, "Không có kênh")
    return r

@router.get("/api/agents/{aid}/channels")
async def list_bindings(aid: int):
    import admin
    if admin.is_demo() or _off(admin.cur_ws()): return []
    rows = await _q(f"""SELECT {COLS}, (SELECT count(*) FROM channel_threads t WHERE t.binding_id=b.id) threads FROM channel_bindings b
                        WHERE agent_id=%s AND workspace_id=%s ORDER BY id""", (aid, admin.cur_ws()))
    return [r for r in rows if getattr(ADAPTERS.get(r["kind"]), "available", lambda: True)()]

@router.post("/api/agents/{aid}/channels")
async def create_binding(aid: int, b: BindingIn):
    import admin
    u = _need_write(); await _agent(aid); await __import__("agent_sharing").guard_edit(aid)   # 8.P: share 'use' -> 403
    if await _q("SELECT 1 FROM agents WHERE id=%s AND system_kind IS NOT NULL", (aid,), one=True): raise HTTPException(403, "Agent hệ thống không gắn được vào kênh công khai / system agents cannot be bound to a public channel")   # GĐ11.38 (F10)
    if b.kind not in ADAPTERS: raise HTTPException(422, f"Loại kênh chưa hỗ trợ: {b.kind} (có: {', '.join(sorted(ADAPTERS))})")
    ad = ADAPTERS[b.kind]
    if not ad.available(): raise HTTPException(422, "Loại kênh này đang tắt trên máy chủ này (cần bật bằng biến môi trường)")
    if getattr(ad, "needs_secret", False) and not b.secret_ref: raise HTTPException(422, "Kênh này cần tên bí mật chứa token (secret_ref)")
    if hasattr(ad, "guard_create"): await ad.guard_create(u, aid, b)   # GĐ11.60: mail triggers (agent owner only, fixed identity ...); may raise HTTPException
    ref, cfg = b.secret_ref, dict(b.config)
    if hasattr(ad, "prepare_create"):
        o = await ad.prepare_create(u, b); ref, cfg = o.get("secret_ref", ref), o.get("config", cfg)
    return await _q(f"""INSERT INTO channel_bindings(workspace_id,agent_id,kind,name,secret_ref,allowed_users,config,created_by)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {COLS}""",
                    (admin.cur_ws(), aid, b.kind, b.name.strip() or b.kind, ref if hasattr(ad, "prepare_create") else await _secret_ok(ref), _users(b.allowed_users),
                     Jsonb(_config(cfg)), u["id"]), one=True)

@router.patch("/api/channels/{bid}")
async def patch_binding(bid: int, p: BindingPatch):
    u = _need_write(); row = await _get(bid); await __import__("agent_sharing").guard_edit(row["agent_id"])
    if hasattr(ADAPTERS.get(row["kind"]), "guard_patch"): await ADAPTERS[row["kind"]].guard_patch(u, row, p)
    sets, args = [], []
    if p.name is not None: sets.append("name=%s"); args.append(p.name.strip())
    if p.status is not None: sets.append("status=%s"); args.append(p.status)
    if p.allowed_users is not None: sets.append("allowed_users=%s"); args.append(_users(p.allowed_users))
    if p.config is not None: sets.append("config=%s"); args.append(Jsonb(_config(dict(p.config))))
    if "secret_ref" in p.model_fields_set: sets.append("secret_ref=%s"); args.append(await _secret_ok(p.secret_ref))
    if not sets: return await _get(bid)
    return await _q(f"UPDATE channel_bindings SET {', '.join(sets)} WHERE id=%s RETURNING {COLS}", (*args, bid), one=True)

@router.delete("/api/channels/{bid}")
async def delete_binding(bid: int):
    _need_write(); row = await _get(bid); await __import__("agent_sharing").guard_edit(row["agent_id"])
    await _q("DELETE FROM channel_bindings WHERE id=%s", (bid,))
    if hasattr(ADAPTERS.get(row["kind"]), "after_delete"): await ADAPTERS[row["kind"]].after_delete(row)
    return {"ok": True}

@router.post("/api/channels/{bid}/simulate")
async def simulate(bid: int, s: SimIn):
    """Send a message (or click Approve/Reject) as a fake external user; outgoing messages are captured, never sent to the real channel."""
    _need_write(); row = await _get(bid); await __import__("agent_sharing").guard_edit(row["agent_id"])
    if getattr(ADAPTERS.get(row["kind"]), "poll_only", False): raise HTTPException(422, "Kênh này nhận thư bằng cách hỏi định kỳ, không mô phỏng được / this channel is polled and cannot be simulated")   # GĐ11.60
    ad = FakeAdapter(); chat = s.chat_id or s.user_id
    if s.decision: res = await handle_decision(bid, chat, s.user_id, s.decision.approval_id, s.decision.approve, adapter=ad, user_name=s.user_name)
    else: res = await handle_incoming(bid, Incoming(chat_id=chat, user_id=s.user_id, user_name=s.user_name, text=s.text, is_group=s.is_group, mentioned=s.mentioned), adapter=ad)
    return {"result": res, "messages": ad.outbox}
