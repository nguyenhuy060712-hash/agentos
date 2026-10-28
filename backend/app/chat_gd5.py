"""GĐ5a (21/10/2026) /chat additions, own module (additive tables only, no change to existing schemas):

  Memory (long-term, LangGraph AsyncPostgresStore; namespace per user = agent/<id>/u<uid>/memories, written by `luu_ghi_nho`)
    GET    /api/chat/memories[?agent_id=agent-N]          the caller's own memories (+ the agent-wide shared ones for admins)
    PATCH  /api/chat/memories {agent_id,key,text,shared}  edit (editor+ for own, admin for shared); re-embedded by the Store
    DELETE /api/chat/memories?agent_id=&key=&shared=      forget (viewer may forget their own: privacy)
    GET    /api/chat/memory/agents                        agents + long_term_memory flag; PUT .../{agent-N} {enabled} (admin, new agent version)
  Thread search (Postgres FTS, 'simple' config over unaccent+lower, so "bao cao" finds "Báo cáo" and "đ" folds to "d")
    POST   /api/chat/threads/search {q,limit}             title + message text, prefix match per word, RLS through lg_threads
    index_thread(tid) after every run (server.py) and on title change; backfill of chat threads at first use
  @mention delegation
    POST   /api/chat/delegate {thread_id,agent_id,text,message_id}   runs the mentioned agent on a hidden child thread (with a short
           context of the conversation), then appends the user turn + that agent's answer to the current thread (as_node="model")
  Ask-for-info (tool `hoi_thong_tin`): ask_tool() builds it; validate_fields() bounds the form the model may ask for."""
from __future__ import annotations
import asyncio, json, re, unicodedata, uuid
from typing import Annotated, Literal
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb
from langchain_core.tools import tool as lc_tool, InjectedToolCallId
from langgraph.types import interrupt
import admin as _adm
import logging
_log = logging.getLogger("uvicorn.error")   # journal-visible (plain print is block-buffered under systemd)
import deepagent_api as A

ROLES = {"viewer": 1, "editor": 2, "admin": 3}
_READY = {"ok": False, "backfill": None}

_DDL = r"""
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE OR REPLACE FUNCTION aos_fold(text) RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
  AS $f$ SELECT lower(public.unaccent('public.unaccent'::regdictionary, $1)) $f$;
CREATE TABLE IF NOT EXISTS chat_fts(
  thread_id text PRIMARY KEY REFERENCES lg_threads(thread_id) ON DELETE CASCADE,
  title text NOT NULL DEFAULT '', body text NOT NULL DEFAULT '',
  tsv tsvector GENERATED ALWAYS AS (setweight(to_tsvector('simple', aos_fold(title)), 'A') || setweight(to_tsvector('simple', aos_fold(body)), 'B')) STORED,
  updated_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS chat_fts_tsv ON chat_fts USING gin(tsv);
"""

def _me():
    u = _adm.cur()
    if not u: raise HTTPException(401, "login required")
    return u

def _need(u, role):
    if ROLES.get(u.get("role") or "viewer", 0) < ROLES[role]: raise HTTPException(403, f"cần quyền {role}")

def _aid(agent) -> int:
    s = str(agent or ""); s = s[len("agent-"):] if s.startswith("agent-") else s
    if not s.isdigit(): raise HTTPException(400, "agent_id phải là agent-<số>")
    return int(s)

async def ensure():
    """DDL as the table owner (request connections may run as the RLS role aos_rls)."""
    if _READY["ok"]: return
    async with A.S["pool"].connection() as c:
        await c.execute("RESET ROLE")
        async with c.transaction():   # api + worker may both get here first: serialise (CREATE OR REPLACE FUNCTION races otherwise)
            await c.execute("SELECT pg_advisory_xact_lock(hashtext('agentos_boot_ddl'))")
            await c.execute(_DDL)
            await c.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON chat_fts TO aos_rls")
    _READY["ok"] = True

# ------------------------------------------------------------------ text folding (python twin of aos_fold, char-aligned)
def fold(s: str) -> str:
    """lower + strip Vietnamese diacritics, ONE output char per input char (so match offsets map back to the original text)."""
    out = []
    for ch in s or "":
        if ch in "đĐ": out.append("d"); continue
        b = unicodedata.normalize("NFD", ch)
        base = "".join(c for c in b if not unicodedata.combining(c)) or ch
        out.append(base[0].lower() if base else ch.lower())
    return "".join(out)

_WORD = re.compile(r"[0-9a-z]+")
def tsquery(q: str) -> str | None:
    """'báo cáo q3' -> 'bao:* & cao:* & q3:*' (prefix match per word; only [0-9a-z] reaches to_tsquery)."""
    toks = _WORD.findall(fold(q))[:8]
    return " & ".join(f"{t}:*" for t in toks) or None

def snippet(body: str, q: str, width: int = 70) -> str:
    fb = fold(body); toks = _WORD.findall(fold(q))
    pos = min([i for i in (fb.find(t) for t in toks) if i >= 0] or [0])
    a = max(0, pos - width // 3); b = min(len(body), a + width)
    s = re.sub(r"\s+", " ", body[a:b]).strip()
    return ("…" if a > 0 else "") + s + ("…" if b < len(body) else "")

# ------------------------------------------------------------------ indexer
def _content_text(c) -> str:
    if isinstance(c, str): return c
    if isinstance(c, list): return " ".join(str(p.get("text", "")) for p in c if isinstance(p, dict) and p.get("type", "text") == "text")
    return ""

def _msg_text(m) -> str:
    t = getattr(m, "type", None) or (m.get("type") if isinstance(m, dict) else None)
    if t not in ("human", "ai", "user", "assistant"): return ""
    c = getattr(m, "content", None) if not isinstance(m, dict) else m.get("content")
    txt = _content_text(c)
    txt = re.sub(r"<attachment[\s\S]*?(</attachment>|$)", " ", txt)                     # inlined text attachments: not searched
    txt = re.sub(r":(\w+)\[([^\]]*)\](\{[^}]*\})?", r"@\2", txt)                         # mention directives -> @Label
    return txt

async def index_thread(tid: str):
    """(Re)index one chat thread: title (metadata) + text of human/AI messages of the current root state."""
    try:
        await ensure()
        t = await A.q("SELECT thread_id, agent_id, metadata FROM lg_threads WHERE thread_id=%s", (tid,), one=True)
        if not t or (t["metadata"] or {}).get("source") != "chat": return
        body = ""
        if t["agent_id"]:
            import server as SV   # graph state (messages may be a delta channel: the raw checkpoint tuple does not carry them)
            msgs = ((await SV._state({"agent_id": t["agent_id"], "thread_id": tid})).get("values") or {}).get("messages") or []
            body = "\n".join(x for x in (_msg_text(m) for m in msgs) if x.strip())[-20000:]
        title = str((t["metadata"] or {}).get("title") or "")[:300]
        await A.q("""INSERT INTO chat_fts(thread_id,title,body,updated_at) VALUES(%s,%s,%s,now())
                     ON CONFLICT (thread_id) DO UPDATE SET title=EXCLUDED.title, body=EXCLUDED.body, updated_at=now()""", (tid, title, body))
    except Exception as e:  # noqa: BLE001  never break a run because of the search index
        _log.warning("chat_fts index %s failed: %s %s", tid, type(e).__name__, str(e)[:200])

async def _backfill():
    """Index chat threads that are missing from chat_fts or changed after their last indexing (self-healing: a run-end
    index that failed or ran in another process is picked up by the next search)."""
    _adm.USER.set(None)    # this task's own context: system scope (all chat threads of every workspace)
    rows = await A.q("""SELECT t.thread_id FROM lg_threads t LEFT JOIN chat_fts f USING(thread_id)
                        WHERE t.metadata->>'source'='chat' AND (f.thread_id IS NULL OR f.updated_at < t.updated_at - interval '1 second')
                        ORDER BY t.updated_at DESC LIMIT 5000""")
    for r in rows: await index_thread(r["thread_id"])
    if rows: _log.info("chat_fts backfill: %d thread(s)", len(rows))
    return len(rows)

def start_backfill():
    if _READY["backfill"] is None or _READY["backfill"].done():
        _READY["backfill"] = asyncio.get_running_loop().create_task(_backfill())
    return _READY["backfill"]

# ------------------------------------------------------------------ ask-for-info tool (hoi_thong_tin)
class AskField(BaseModel):
    name: str = Field(description="Khoá máy đọc, a-z0-9_ (vd 'ngan_sach').")
    label: str = Field(description="Nhãn tiếng Việt hiển thị cho người dùng.")
    kind: Literal["text", "number", "choice", "multi", "toggle", "date"] = Field(default="text", description="Kiểu ô nhập.")
    options: list[str] = Field(default=[], description="Các lựa chọn (bắt buộc cho choice/multi, tối đa 12).")
    required: bool = Field(default=True, description="Bắt buộc trả lời.")
    placeholder: str = Field(default="", description="Gợi ý trong ô nhập.")

MAX_FIELDS = 8
def validate_fields(fields) -> list[dict]:
    out, seen = [], set()
    for f in list(fields or [])[:MAX_FIELDS]:
        f = f.model_dump() if hasattr(f, "model_dump") else dict(f)
        name = re.sub(r"[^a-z0-9_]", "_", fold(str(f.get("name") or f.get("label") or "truong")))[:40].strip("_") or f"truong_{len(out)+1}"
        while name in seen: name += "_"
        seen.add(name)
        kind = f.get("kind") if f.get("kind") in ("text", "number", "choice", "multi", "toggle", "date") else "text"
        opts = [str(o)[:60] for o in (f.get("options") or [])][:12]
        if kind in ("choice", "multi") and not opts: kind = "text"
        out.append({"name": name, "label": str(f.get("label") or name)[:80], "kind": kind, "options": opts,
                    "required": bool(f.get("required", True)) and kind != "toggle", "placeholder": str(f.get("placeholder") or "")[:80]})
    if not out: raise ValueError("cần ít nhất 1 trường")
    return out

def check_answers(fields: list[dict], answers: dict) -> dict:
    """Keep only declared fields, coerce by kind, enforce options/required. Raises ValueError (the tool reports it, the run goes on)."""
    clean = {}
    for f in fields:
        v = (answers or {}).get(f["name"])
        if f["kind"] == "toggle": clean[f["name"]] = bool(v); continue
        if f["kind"] == "multi":
            v = [x for x in (v if isinstance(v, list) else []) if x in f["options"]]
        elif f["kind"] == "choice":
            v = v if v in f["options"] else None
        elif f["kind"] == "number":
            try: v = None if v in (None, "") else float(str(v).replace(",", "."))
            except ValueError: v = None
        else:
            v = str(v).strip()[:2000] if v not in (None, "") else None
        if f["required"] and (v is None or v == []): raise ValueError(f"thiếu '{f['label']}'")
        if v is not None and v != []: clean[f["name"]] = v
    return clean

def ask_tool():

    @lc_tool
    async def hoi_thong_tin(tieu_de: str, truong: list[AskField], ghi_chu: str = "",
                            tool_call_id: Annotated[str, InjectedToolCallId] = "") -> str:
        """Hỏi người dùng thông tin còn thiếu bằng một biểu mẫu (thẻ có ô nhập/lựa chọn) rồi dừng chờ trả lời.
        Dùng khi cần ≥1 thông tin cụ thể mới làm tiếp được (vd ngân sách, kênh, ngày). Không dùng để hỏi xác nhận chi tiền (đã có thẻ duyệt).
        tieu_de: câu hỏi ngắn; truong: tối đa 8 trường {name,label,kind,options,required,placeholder}; ghi_chu: giải thích ngắn (tuỳ chọn)."""
        try: fields = validate_fields(truong)
        except ValueError as e: return f"Biểu mẫu không hợp lệ: {e}. Hãy hỏi lại bằng chữ."
        ans = interrupt({"type": "ask_user", "tool_call_id": tool_call_id, "title": str(tieu_de)[:160], "note": str(ghi_chu)[:300], "fields": fields})
        if not isinstance(ans, dict) or ans.get("declined"): return "Người dùng không trả lời biểu mẫu. Đừng hỏi lại cùng câu; làm tiếp với giả định hợp lý hoặc hỏi bằng chữ."
        try: clean = check_answers(fields, ans.get("answers") or {})
        except ValueError as e: return f"Câu trả lời chưa đủ ({e})."
        labels = {f["name"]: f["label"] for f in fields}
        return "Người dùng đã trả lời: " + json.dumps({labels[k]: v for k, v in clean.items()}, ensure_ascii=False)
    return hoi_thong_tin

def is_ask(v) -> bool: return isinstance(v, dict) and v.get("type") == "ask_user"

# ------------------------------------------------------------------ memory helpers
def _ns_user(aid: int, uid) -> tuple: return ("agent", str(aid), f"u{uid}", "memories")
def _ns_shared(aid: int) -> tuple: return ("agent", str(aid), "memories")

class MemPatch(BaseModel):
    agent_id: str
    key: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=2000)
    shared: bool = False

class MemToggle(BaseModel):
    enabled: bool

class SearchIn(BaseModel):
    q: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=20, ge=1, le=50)

class DelegateIn(BaseModel):
    thread_id: str = Field(min_length=8, max_length=100)
    agent_id: str
    text: str = Field(min_length=1, max_length=8000)
    message_id: str | None = Field(default=None, max_length=100)
    content: list | None = Field(default=None, max_length=20)   # GĐ5b: the user's content blocks (images forwarded to the mentioned agent)

_MENTION = re.compile(r":agent(?:\[([^\]]*)\]\{name=([^}]*)\}|\{label=\"([^\"]*)\" name=\"([^\"]*)\"\})")
def plain_mentions(text: str) -> str:
    return _MENTION.sub(lambda m: "@" + (m.group(1) or m.group(3) or ""), text)

def router():
    r = APIRouter()
    _adm.allow_viewer_post("/api/chat/memories", "/api/chat/threads/search")   # handlers enforce their own rules below

    # ---------------- memory
    @r.get("/api/chat/memories")
    async def mem_list(agent_id: str | None = None):
        u = _me(); aid = _aid(agent_id) if agent_id else None
        names = {x["id"]: x["name"] for x in await A.q("SELECT id, name FROM agents WHERE NOT deleted")}   # RLS: own workspace only
        a_re = str(aid) if aid else "[0-9]+"     # exact namespaces (a LIKE 'agent.%.memories' would also match the per-user ones)
        rows = await A.q("SELECT prefix, key, value, created_at, updated_at FROM store WHERE prefix ~ %s ORDER BY updated_at DESC LIMIT 500",
                         (rf"^agent\.{a_re}\.u{int(u['id'])}\.memories$",))
        if u.get("role") == "admin":
            rows += await A.q("SELECT prefix, key, value, created_at, updated_at FROM store WHERE prefix ~ %s ORDER BY updated_at DESC LIMIT 200",
                              (rf"^agent\.{a_re}\.memories$",))
        out = []
        for x in rows:
            p = x["prefix"].split(".")
            if len(p) < 3 or not p[1].isdigit() or int(p[1]) not in names: continue
            out.append({"agent_id": f"agent-{p[1]}", "agent_name": names[int(p[1])], "key": x["key"], "text": str((x["value"] or {}).get("text") or "")[:2000],
                        "shared": len(p) == 3, "created_at": x["created_at"], "updated_at": x["updated_at"]})
        return {"items": out, "can_edit": ROLES.get(u.get("role"), 0) >= 2, "is_admin": u.get("role") == "admin"}

    async def _ns_for(u, agent_id, shared):
        aid = _aid(agent_id)
        if not await A.q("SELECT 1 FROM agents WHERE id=%s AND NOT deleted", (aid,), one=True): raise HTTPException(404, "Không có agent")
        if shared:
            _need(u, "admin"); return _ns_shared(aid)
        return _ns_user(aid, u["id"])

    @r.patch("/api/chat/memories")
    async def mem_edit(b: MemPatch):
        u = _me(); _need(u, "editor"); ns = await _ns_for(u, b.agent_id, b.shared)
        if not await A.S["store"].aget(ns, b.key): raise HTTPException(404, "Không có ghi nhớ này")
        await A.S["store"].aput(ns, b.key, {"text": b.text.strip()})
        return {"ok": True}

    @r.delete("/api/chat/memories")
    async def mem_del(agent_id: str, key: str, shared: bool = False):
        u = _me(); ns = await _ns_for(u, agent_id, shared)   # own memories: any role may forget them (privacy); shared: admin
        if not await A.S["store"].aget(ns, key): raise HTTPException(404, "Không có ghi nhớ này")
        await A.S["store"].adelete(ns, key)
        return {"ok": True}

    @r.get("/api/chat/memory/agents")
    async def mem_agents():
        _me()
        rows = await A.q("SELECT a.id, a.name, v.config->>'long_term_memory' ltm FROM agents a JOIN agent_versions v ON v.agent_id=a.id AND v.version=a.current_version WHERE NOT a.deleted AND NOT a.hidden ORDER BY a.id")
        return [{"agent_id": f"agent-{x['id']}", "name": x["name"], "enabled": x["ltm"] == "true"} for x in rows]

    @r.put("/api/chat/memory/agents/{agent_id}")
    async def mem_toggle(agent_id: str, b: MemToggle):
        u = _me(); _need(u, "admin"); aid = _aid(agent_id)
        ver = await A.cur_version(aid)
        row = await A.q("SELECT config FROM agent_versions WHERE agent_id=%s AND version=%s", (aid, ver), one=True)
        cfg = A.AgentCfg(**{**(row["config"] or {}), "long_term_memory": b.enabled})
        v = await A.save_version(aid, cfg, f"/chat: bộ nhớ dài hạn {'bật' if b.enabled else 'tắt'}")
        A.S.get("agents", {}).clear()     # rebuild with/without the memory tools
        return {"agent_id": f"agent-{aid}", "enabled": b.enabled, "version": v}

    # ---------------- thread search
    @r.post("/api/chat/threads/search")
    async def th_search(b: SearchIn):
        _me(); await ensure()
        try: await asyncio.wait_for(asyncio.shield(start_backfill()), 2.0)   # fresh results for just-finished chats; never blocks > 2 s
        except Exception: pass   # noqa: BLE001  (timeout: the task keeps running in the background)
        tq = tsquery(b.q)
        if not tq: return {"items": []}
        rows = await A.q("""SELECT t.thread_id, t.metadata, t.updated_at, t.agent_id, f.title, f.body, ts_rank(f.tsv, q) AS rank
            FROM chat_fts f JOIN lg_threads t USING(thread_id), to_tsquery('simple', %s) q
            WHERE f.tsv @@ q AND t.metadata @> '{"source":"chat"}' ORDER BY rank DESC, t.updated_at DESC LIMIT %s""", (tq, b.limit))
        return {"items": [{"thread_id": x["thread_id"], "title": (x["metadata"] or {}).get("title") or x["title"] or "",
                           "agent_id": f"agent-{x['agent_id']}" if x["agent_id"] else (x["metadata"] or {}).get("graph_id"),
                           "archived": bool((x["metadata"] or {}).get("archived")), "pinned": bool((x["metadata"] or {}).get("pinned")),
                           "updated_at": x["updated_at"], "snippet": snippet(x["body"] or x["title"] or "", b.q)} for x in rows]}

    # ---------------- @mention delegation
    @r.post("/api/chat/delegate")
    async def delegate(b: DelegateIn):
        import server as SV
        u = _me()
        if not _adm.is_demo(u): _need(u, "editor")                  # demo account: may @mention (the child run is capped by _create_run)
        t = await SV._get_thread(b.thread_id)                         # RLS: the caller's own thread
        target = await SV._get_assistant(f"agent-{_aid(b.agent_id)}")
        if not t["agent_id"]: raise HTTPException(409, "Thread chưa có agent: gửi bình thường để chuyển cho agent được nhắc")
        if t["agent_id"] == target["agent_id"]: raise HTTPException(409, "Đây đã là agent của cuộc trò chuyện")
        if await A.q("SELECT 1 FROM lg_runs WHERE thread_id=%s AND status IN ('pending','running')", (t["thread_id"],), one=True):
            raise HTTPException(409, "Cuộc trò chuyện đang chạy")
        st = await SV._state(t)
        if st.get("interrupts"): raise HTTPException(409, "Cuộc trò chuyện đang chờ duyệt/trả lời")
        prev = [x for x in (_msg_text(m) for m in (st["values"].get("messages") or [])[-8:]) if x.strip()]
        ctx = "\n".join(f"- {p[:400]}" for p in prev)[-2500:]
        text = plain_mentions(b.text).strip()
        lead = (await A.q("SELECT name FROM agents WHERE id=%s", (t["agent_id"],), one=True) or {}).get("name", "")
        child_in = (f"[Được giao từ cuộc trò chuyện với {lead}]\nBối cảnh gần đây:\n{ctx}\n\nYêu cầu: {text}" if ctx else text)
        blocks = None
        if b.content:   # GĐ5b: images attached to the @mention message go to the mentioned agent too (stored/validated like run input)
            import attachments
            blocks = await attachments.normalize_content(b.content)
            imgs = [x for x in blocks if isinstance(x, dict) and x.get("type") == "image"]
            child_in = [{"type": "text", "text": child_in}, *imgs] if imgs else child_in
            if not imgs: blocks = None
        child = str(uuid.uuid4())
        await A.q("INSERT INTO lg_threads(thread_id,workspace_id,metadata) VALUES(%s,%s,%s)",
                  (child, SV._ws(), Jsonb({"source": "delegation", "parent_thread": t["thread_id"], "graph_id": target["graph_id"], "title": text[:60]})))
        run = await SV._create_run(child, SV.RunIn(assistant_id=str(target["assistant_id"]), input={"messages": [{"role": "user", "content": child_in}]}))
        await SV._join(str(run["run_id"]))
        rr = await SV._run_row(str(run["run_id"]))
        cst = await SV._state(await SV._get_thread(child))
        ans = next((_content_text(m.get("content")) for m in reversed(cst["values"].get("messages") or []) if isinstance(m, dict) and m.get("type") == "ai" and _content_text(m.get("content")).strip()), "")
        status = rr["status"]
        if status == "interrupted" or cst.get("interrupts"): status = "interrupted"; ans = ans or "Agent cần anh/chị duyệt hoặc trả lời thêm, mở cuộc trò chuyện được giao để tiếp tục."
        elif status != "success": ans = ans or f"Không giao được việc ({(rr.get('error') or status)[:200]})."
        from langchain_core.messages import AIMessage, HumanMessage
        name = target["name"] or target["graph_id"]
        hm = HumanMessage(content=blocks or b.text, id=b.message_id or str(uuid.uuid4()), additional_kwargs={"delegated_to": target["graph_id"], "metadata": {"delegated_to": target["graph_id"], "delegate_name": name}})
        am = AIMessage(content=ans, id=str(uuid.uuid4()), name=re.sub(r"[^A-Za-z0-9_-]", "_", fold(name))[:60] or None,
                       additional_kwargs={"delegated_from": target["graph_id"], "delegate_name": name, "delegate_thread": child, "delegate_status": status,
                                          # additional_kwargs.metadata = assistant-ui message.metadata.custom (react-langchain converter)
                                          "metadata": {"delegated_from": target["graph_id"], "delegate_name": name, "delegate_status": status}})
        ag = await SV._graph(t)
        _c = {"configurable": {"thread_id": f"a{t['agent_id']}:{t['thread_id']}"}}
        await ag.aupdate_state(_c, {"messages": [hm, am]}, as_node="model")
        await ag.aupdate_state(_c, None, as_node="__end__")   # clear the after_model hooks the "model" write scheduled: thread is idle
        await A.q("UPDATE lg_threads SET updated_at=now() WHERE thread_id=%s", (t["thread_id"],))
        await index_thread(t["thread_id"])
        return {"status": status, "child_thread": child, "agent_id": target["graph_id"], "agent_name": name,
                "values": (await SV._state(t))["values"]}
    return r
