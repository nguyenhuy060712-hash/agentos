"""P5 Deployment / Agent Server API (tự viết theo tài liệu công khai về khái niệm assistants/threads/runs):
assistants (phiên bản), threads (metadata, state, history, copy, TTL), runs nền qua hàng đợi Postgres (FOR UPDATE SKIP LOCKED),
stream SSE (metadata/updates/values/end) + join stream, /wait, stateless runs, huỷ (interrupt/rollback), double-texting
(reject/enqueue/interrupt/rollback), webhook khi xong (nội bộ gửi, bên ngoài = bản nháp), cron theo assistant/thread, HITL resume.
Prefix /api/v1 — xác thực qua middleware P7 (phiên / X-Api-Key / Basic)."""
import asyncio, datetime as dt, json, time, uuid
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb
import deepagent_api as A
import obs

router = APIRouter(prefix="/api/v1", tags=["agent-server"])
# LangGraph's search endpoints are POST but read-only: viewers (who may GET threads/assistants)
# must be able to use them, otherwise the front-end UI cannot even list agents for a viewer.
import admin as _adm_boot
_adm_boot.allow_viewer_post("/api/v1/assistants/search", "/api/v1/threads/search")
DDL = """
CREATE TABLE IF NOT EXISTS lg_assistants(assistant_id UUID PRIMARY KEY, graph_id TEXT NOT NULL, agent_id INT REFERENCES agents(id) ON DELETE CASCADE,
  agent_version INT, name TEXT, description TEXT, config JSONB NOT NULL DEFAULT '{}', metadata JSONB NOT NULL DEFAULT '{}', version INT NOT NULL DEFAULT 1,
  workspace_id INT NOT NULL DEFAULT 1, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS lg_assistant_versions(assistant_id UUID REFERENCES lg_assistants(assistant_id) ON DELETE CASCADE, version INT NOT NULL,
  agent_version INT, config JSONB, metadata JSONB, name TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(assistant_id, version));
CREATE TABLE IF NOT EXISTS lg_threads(thread_id TEXT PRIMARY KEY, assistant_id UUID, agent_id INT, metadata JSONB NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'idle', workspace_id INT NOT NULL DEFAULT 1, ttl_expires TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS lg_runs(run_id UUID PRIMARY KEY, thread_id TEXT, assistant_id UUID, status TEXT NOT NULL DEFAULT 'pending',
  multitask_strategy TEXT NOT NULL DEFAULT 'enqueue', input JSONB, command JSONB, checkpoint_id TEXT, pre_checkpoint TEXT, stream_mode TEXT[],
  webhook TEXT, metadata JSONB NOT NULL DEFAULT '{}', output JSONB, error TEXT, trace_id UUID, temporary BOOLEAN NOT NULL DEFAULT false,
  run_at TIMESTAMPTZ NOT NULL DEFAULT now(), created_at TIMESTAMPTZ NOT NULL DEFAULT now(), started_at TIMESTAMPTZ, ended_at TIMESTAMPTZ, workspace_id INT NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS lg_runs_q ON lg_runs(status, run_at) WHERE status='pending';
CREATE INDEX IF NOT EXISTS lg_runs_thread ON lg_runs(thread_id, created_at);
CREATE TABLE IF NOT EXISTS lg_run_events(run_id UUID, seq INT, event TEXT, data JSONB, ts TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(run_id, seq));
CREATE TABLE IF NOT EXISTS lg_crons(cron_id UUID PRIMARY KEY, assistant_id UUID REFERENCES lg_assistants(assistant_id) ON DELETE CASCADE, thread_id TEXT,
  schedule TEXT NOT NULL, input JSONB, webhook TEXT, metadata JSONB NOT NULL DEFAULT '{}', enabled BOOLEAN NOT NULL DEFAULT true, last_run TIMESTAMPTZ,
  workspace_id INT NOT NULL DEFAULT 1, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
"""
MAX_CONC = int(__import__("os").environ.get("AOS_QUEUE_CONC", "2"))
WID = __import__("os").environ.get("AOS_WORKER", "api")      # "api" (in-process) or "worker1" (deepagent-worker.service)
TASKS: dict = {}          # run_id -> asyncio.Task
TERMINAL = ("success", "error", "interrupted", "timeout", "cancelled")

def _ws():
    import admin; return admin.cur_ws()

async def init():
    global DRAINING
    DRAINING = False
    await A.q(DDL)
    await A.q("ALTER TABLE lg_runs ADD COLUMN IF NOT EXISTS config JSONB")
    await A.q("ALTER TABLE lg_runs ADD COLUMN IF NOT EXISTS stream_subgraphs BOOLEAN NOT NULL DEFAULT false")   # G1
    await A.q("ALTER TABLE lg_assistants ADD COLUMN IF NOT EXISTS context JSONB NOT NULL DEFAULT '{}'")
    await A.q("ALTER TABLE lg_assistant_versions ADD COLUMN IF NOT EXISTS context JSONB, ADD COLUMN IF NOT EXISTS description TEXT")
    # run đang 'running' từ lần khởi động trước → coi như lỗi (worker đã chết)
    await A.q("ALTER TABLE lg_runs ADD COLUMN IF NOT EXISTS worker TEXT")
    await A.q("ALTER TABLE lg_runs ADD COLUMN IF NOT EXISTS attempt INT NOT NULL DEFAULT 0, ADD COLUMN IF NOT EXISTS heartbeat_at TIMESTAMPTZ, ADD COLUMN IF NOT EXISTS failure_kind TEXT, ADD COLUMN IF NOT EXISTS cancel_reason TEXT, ADD COLUMN IF NOT EXISTS start_ckpt TEXT")   # 13.Z: same columns as migration 0230; recover_boot below runs BEFORE the numbered migrations on a fresh database
    await A.q("""CREATE TABLE IF NOT EXISTS worker_heartbeats(name TEXT PRIMARY KEY, pid INT, host TEXT, started TIMESTAMPTZ, at TIMESTAMPTZ,
        running INT NOT NULL DEFAULT 0, claimed_total BIGINT NOT NULL DEFAULT 0, max_conc INT)""")
    # only runs claimed by THIS process (or legacy rows without a worker) are orphaned by a restart
    # GĐ13.30: durable runs - a run of THIS process is put back to `pending` (retry from its checkpoint, durable.max_retries) instead of failing; legacy rows (no worker) and the demo workspace still fail
    import durable; await durable.recover_boot(WID)
    await A.q("INSERT INTO worker_heartbeats(name,pid,host,started,at,max_conc) VALUES(%s,%s,%s,now(),now(),%s) ON CONFLICT(name) DO UPDATE SET pid=EXCLUDED.pid, host=EXCLUDED.host, started=now(), at=now(), max_conc=EXCLUDED.max_conc",
              (WID, __import__("os").getpid(), __import__("socket").gethostname(), MAX_CONC))

def _j(x): return json.loads(json.dumps(x, default=str))

class DefaultJsonContentType:
    """GĐ9.6: the LangGraph Agent Server reads request bodies as JSON whatever the Content-Type; some clients drop or mangle it (agent-chat-ui's Next.js API
    passthrough only forwards x-* headers and re-sends the body as a string => `text/plain`). FastAPI would hand such a body over as a plain string (422), so
    POST/PUT/PATCH on /api/v1 with no / text/plain content type are treated as `application/json`."""
    def __init__(self, app): self.app = app
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] in ("POST", "PUT", "PATCH") and scope["path"].startswith("/api/v1/"):
            hs = scope["headers"]; ct = next((v for k, v in hs if k == b"content-type"), b"")
            if not ct or ct.lower().startswith(b"text/plain"):
                scope = {**scope, "headers": [(k, v) for k, v in hs if k != b"content-type"] + [(b"content-type", b"application/json")]}
        await self.app(scope, receive, send)

def _nj(x):
    """GĐ9.6: JSON for LangGraph-native stream payloads (tasks / checkpoints / debug / custom): messages -> the same dict shape as `values`, interrupts -> {value,id}."""
    from langchain_core.messages import BaseMessage
    def d(o):
        if isinstance(o, BaseMessage): return _mv(o)
        if hasattr(o, "model_dump"):
            try: return o.model_dump()
            except Exception: pass  # noqa: BLE001
        if hasattr(o, "value") and hasattr(o, "id"): return {"value": getattr(o, "value", None), "id": getattr(o, "id", None)}
        if isinstance(o, (set, frozenset, tuple)): return list(o)
        return str(o)
    return json.loads(json.dumps(x, default=d))

def _ev(kind, name, rid, data=None, **kw):
    """GĐ9.6 `events` stream mode: astream_events(v2)-shaped record (event, name, run_id, tags, metadata, data, parent_ids) synthesised from the run's steps."""
    return {"event": kind, "name": name, "run_id": rid, "tags": [], "metadata": kw.get("metadata") or {}, "data": data or {}, "parent_ids": kw.get("parent_ids") or []}

# ------------------------------------------------------------ assistants
async def _sync_system_assistants():
    for a in await A.q("SELECT a.id, a.name, a.workspace_id FROM agents a WHERE NOT a.deleted AND NOT a.hidden AND NOT EXISTS (SELECT 1 FROM lg_assistants s WHERE s.agent_id=a.id AND s.metadata->>'created_by'='system')"):
        aid = uuid.uuid5(uuid.NAMESPACE_URL, f"agentos-agent-{a['id']}")
        await A.q("INSERT INTO lg_assistants(assistant_id,graph_id,agent_id,name,metadata,workspace_id) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                  (aid, f"agent-{a['id']}", a["id"], a["name"], Jsonb({"created_by": "system"}), a["workspace_id"]))
    await A.q("""INSERT INTO lg_assistant_versions(assistant_id,version,agent_version,config,context,metadata,name,description)
        SELECT assistant_id,version,agent_version,config,context,metadata,name,description FROM lg_assistants s
        WHERE NOT EXISTS (SELECT 1 FROM lg_assistant_versions v WHERE v.assistant_id=s.assistant_id) ON CONFLICT DO NOTHING""")

class AssistantIn(BaseModel):
    graph_id: str = Field(pattern=r"^agent-\d+$")
    name: str | None = None
    description: str | None = None
    config: dict = {}
    context: dict = {}
    metadata: dict = {}
    agent_version: int | None = None
    assistant_id: str | None = None
    if_exists: str = Field(default="raise", pattern=r"^(raise|do_nothing)$")

async def _get_assistant(aid):
    try: uuid.UUID(str(aid))
    except ValueError:
        if str(aid) == __import__("assistant").NAME: return await __import__("assistant").ensure()   # GĐ7.1: built-in assistant of this workspace
        if str(aid).startswith("agent-"):
            await _sync_system_assistants()
            r = await A.q("SELECT * FROM lg_assistants WHERE graph_id=%s AND metadata->>'created_by'='system' AND workspace_id=%s", (aid, _ws()), one=True)
            if r: return r
        raise HTTPException(404, "Không có assistant")
    r = await A.q("SELECT * FROM lg_assistants WHERE assistant_id=%s AND workspace_id=%s", (aid, _ws()), one=True)
    if not r: raise HTTPException(404, "Không có assistant")
    return r

@router.post("/assistants")
async def as_create(b: AssistantIn):
    agent_id = int(b.graph_id.split("-")[1])
    if not await A.q("SELECT 1 FROM agents WHERE id=%s AND NOT deleted AND workspace_id=%s", (agent_id, _ws()), one=True): raise HTTPException(404, "graph_id không tồn tại")
    aid = b.assistant_id or str(uuid.uuid4())
    if await A.q("SELECT 1 FROM lg_assistants WHERE assistant_id=%s", (aid,), one=True):
        if b.if_exists == "do_nothing": return await _get_assistant(aid)
        raise HTTPException(409, "assistant đã tồn tại")
    r = await A.q("INSERT INTO lg_assistants(assistant_id,graph_id,agent_id,agent_version,name,description,config,context,metadata,workspace_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                  (aid, b.graph_id, agent_id, b.agent_version, b.name or b.graph_id, b.description, Jsonb(b.config), Jsonb(b.context), Jsonb(b.metadata), _ws()), one=True)
    await A.q("INSERT INTO lg_assistant_versions(assistant_id,version,agent_version,config,context,metadata,name,description) VALUES(%s,1,%s,%s,%s,%s,%s,%s)",
              (aid, b.agent_version, Jsonb(b.config), Jsonb(b.context), Jsonb(b.metadata), r["name"], b.description))
    return r

def _as_where(b):
    q = (b.get("name") or b.get("query") or "").strip()
    return ("workspace_id=%s AND coalesce(metadata->>'hidden','')<>'true' AND (%s::text IS NULL OR graph_id=%s) AND metadata @> %s AND (%s='' OR name ILIKE %s OR description ILIKE %s OR assistant_id::text ILIKE %s)",
            [_ws(), b.get("graph_id"), b.get("graph_id"), Jsonb(b.get("metadata") or {}), q, f"%{q}%", f"%{q}%", f"{q}%"])

@router.post("/assistants/search")
async def as_search(b: dict = None):
    b = b or {}; await __import__("chat_general").ensure_for_request(); await _sync_system_assistants()   # GĐ11.38: "Trò chuyện chung" of this workspace
    w, p = _as_where(b)
    sb = b.get("sort_by") if b.get("sort_by") in ("assistant_id", "graph_id", "name", "created_at", "updated_at") else "created_at"
    so = "DESC" if str(b.get("sort_order", "asc")).lower() == "desc" else "ASC"
    rows = await A.q(f"SELECT * FROM lg_assistants WHERE {w} ORDER BY {sb} {so} LIMIT %s OFFSET %s", (*p, min(int(b.get("limit", 10)), 1000), int(b.get("offset", 0))))
    import admin as _adm
    allow = (_adm.cur() or {}).get("demo_agents") if _adm.is_demo() else None
    return [r for r in rows if r.get("agent_id") in allow] if allow else rows   # demo: only the agents opened for the trial

@router.post("/assistants/count")
async def as_count(b: dict = None):
    b = b or {}; await _sync_system_assistants(); w, p = _as_where(b)
    return (await A.q(f"SELECT count(*) n FROM lg_assistants WHERE {w}", tuple(p), one=True))["n"]

@router.get("/assistants/{aid}/threads")
async def as_threads(aid: str, limit: int = 50):
    a = await _get_assistant(aid)
    return await A.q("""SELECT t.*, (SELECT count(*) FROM lg_runs r WHERE r.thread_id=t.thread_id) runs,
        (SELECT max(r.created_at) FROM lg_runs r WHERE r.thread_id=t.thread_id) last_run FROM lg_threads t
        WHERE t.assistant_id=%s OR EXISTS (SELECT 1 FROM lg_runs r WHERE r.thread_id=t.thread_id AND r.assistant_id=%s)
        ORDER BY t.updated_at DESC LIMIT %s""", (a["assistant_id"], a["assistant_id"], min(limit, 500)))

@router.get("/assistants/{aid}/schemas")
async def as_schemas(aid: str):
    a = await _get_assistant(aid)
    keys = sorted(A.OVERRIDABLE)
    return {"graph_id": a["graph_id"],
            "input_schema": {"type": "object", "properties": {"messages": {"type": "array"}}},
            "output_schema": {"type": "object", "properties": {"messages": {"type": "array"}}},
            "state_schema": {"type": "object", "properties": {"messages": {"type": "array"}, "todos": {"type": "array"}, "files": {"type": "object"}}},
            "config_schema": {"type": "object", "properties": {"configurable": {"type": "object", "properties": {k: {} for k in keys}}}},
            "context_schema": {"type": "object", "additionalProperties": True,
                               "description": "Free-form runtime context passed to the agent with each run (e.g. user_id, locale)."}}

@router.get("/assistants/{aid}")
async def as_get(aid: str): return await _get_assistant(aid)

class AssistantUpd(BaseModel):
    name: str | None = None
    description: str | None = None
    config: dict | None = None
    context: dict | None = None
    metadata: dict | None = None
    agent_version: int | None = None

@router.patch("/assistants/{aid}")
async def as_patch(aid: str, b: AssistantUpd):
    a = await _get_assistant(aid); v = a["version"] + 1
    v = ((await A.q("SELECT max(version) m FROM lg_assistant_versions WHERE assistant_id=%s", (a["assistant_id"],), one=True))["m"] or a["version"]) + 1
    r = await A.q("""UPDATE lg_assistants SET name=coalesce(%s,name), description=coalesce(%s,description), config=coalesce(%s,config), context=coalesce(%s,context),
        metadata=metadata || %s, agent_version=coalesce(%s,agent_version), version=%s, updated_at=now() WHERE assistant_id=%s RETURNING *""",
                  (b.name, b.description, Jsonb(b.config) if b.config is not None else None, Jsonb(b.context) if b.context is not None else None,
                   Jsonb(b.metadata or {}), b.agent_version, v, a["assistant_id"]), one=True)
    await A.q("INSERT INTO lg_assistant_versions(assistant_id,version,agent_version,config,context,metadata,name,description) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
              (a["assistant_id"], v, r["agent_version"], Jsonb(r["config"]), Jsonb(r["context"]), Jsonb(r["metadata"]), r["name"], r["description"]))
    return r

@router.post("/assistants/{aid}/versions")
async def as_versions(aid: str):
    a = await _get_assistant(aid)
    return await A.q("SELECT * FROM lg_assistant_versions WHERE assistant_id=%s ORDER BY version DESC", (a["assistant_id"],))

@router.post("/assistants/{aid}/latest")
async def as_set_latest(aid: str, b: dict):
    a = await _get_assistant(aid)
    v = await A.q("SELECT * FROM lg_assistant_versions WHERE assistant_id=%s AND version=%s", (a["assistant_id"], int(b["version"])), one=True)
    if not v: raise HTTPException(404, "Không có phiên bản")
    return await A.q("UPDATE lg_assistants SET agent_version=%s, config=%s, context=%s, metadata=%s, name=%s, description=coalesce(%s,description), version=%s, updated_at=now() WHERE assistant_id=%s RETURNING *",
                     (v["agent_version"], Jsonb(v["config"] or {}), Jsonb(v["context"] or {}), Jsonb(v["metadata"] or {}), v["name"], v["description"], v["version"], a["assistant_id"]), one=True)

@router.delete("/assistants/{aid}")
async def as_del(aid: str):
    a = await _get_assistant(aid)
    if (a["metadata"] or {}).get("created_by") == "system": raise HTTPException(400, "assistant hệ thống — xoá agent thay vì assistant")
    await A.q("DELETE FROM lg_assistants WHERE assistant_id=%s", (a["assistant_id"],)); return {"ok": True}

@router.get("/assistants/{aid}/graph")
async def as_graph(aid: str):
    import studio
    a = await _get_assistant(aid); return await studio.st_graph(a["agent_id"], a["agent_version"])

# ------------------------------------------------------------ threads
class ThreadIn(BaseModel):
    thread_id: str | None = Field(default=None, max_length=100)
    metadata: dict = {}
    if_exists: str = Field(default="raise", pattern=r"^(raise|do_nothing)$")
    ttl: dict | None = None            # {"ttl": phút}

async def _get_thread(tid):
    t = await A.q("SELECT * FROM lg_threads WHERE thread_id=%s AND workspace_id=%s", (tid, _ws()), one=True)
    if not t: raise HTTPException(404, "Không có thread")
    return t

@router.post("/threads")
async def th_create(b: ThreadIn):
    tid = b.thread_id or str(uuid.uuid4())
    if await A.q("SELECT 1 FROM lg_threads WHERE thread_id=%s", (tid,), one=True):
        if b.if_exists == "do_nothing": return await _get_thread(tid)
        raise HTTPException(409, "thread đã tồn tại")
    ttl = (b.ttl or {}).get("ttl")
    return await A.q("INSERT INTO lg_threads(thread_id,metadata,workspace_id,ttl_expires) VALUES(%s,%s,%s,CASE WHEN %s::int IS NULL THEN NULL ELSE now()+%s::int*interval '1 minute' END) RETURNING *",
                     (tid, Jsonb(b.metadata), _ws(), ttl, ttl), one=True)

@router.get("/threads/{tid}")
async def th_get(tid: str):
    t = await _get_thread(tid)
    if t["agent_id"]: t["values"] = (await _state(t))["values"]
    return t

@router.patch("/threads/{tid}")
async def th_patch(tid: str, b: dict):
    await _get_thread(tid)
    row = await A.q("UPDATE lg_threads SET metadata=metadata || %s, updated_at=now() WHERE thread_id=%s RETURNING *", (Jsonb(b.get("metadata") or {}), tid), one=True)
    if "title" in (b.get("metadata") or {}):
        import chat_gd5; await chat_gd5.index_thread(tid)   # GĐ5a: renamed thread is searchable by its new title
    return row

@router.delete("/threads/{tid}")
async def th_del(tid: str):
    t = await _get_thread(tid); await _delete_thread(t); return {"ok": True}

async def _delete_thread(t):
    for r in await A.q("SELECT run_id FROM lg_runs WHERE thread_id=%s AND status IN ('pending','running')", (t["thread_id"],)):
        await _cancel(str(r["run_id"]), "interrupt")
    if t["agent_id"]:
        try: await A.S["cp"].adelete_thread(f"a{t['agent_id']}:{t['thread_id']}")
        except Exception as e: print("delete checkpoint", e)  # noqa: BLE001
    await A.q("DELETE FROM lg_threads WHERE thread_id=%s", (t["thread_id"],))

@router.post("/threads/search")
async def th_search(b: dict = None):
    b = b or {}
    # GĐ6.10: owner (username) on every row + optional owner_id filter (RLS already hides other users' threads from non-admins)
    own = b.get("owner_id"); own = int(own) if str(own or "").isdigit() else None
    return await A.q("SELECT t.*, u.username owner FROM lg_threads t LEFT JOIN users u ON u.id=t.owner_id WHERE t.workspace_id=%s AND t.metadata @> %s AND (%s::text IS NULL OR t.status=%s) AND (%s::int IS NULL OR t.owner_id=%s) ORDER BY t.updated_at DESC LIMIT %s OFFSET %s",
                     (_ws(), Jsonb(b.get("metadata") or {}), b.get("status"), b.get("status"), own, own, min(int(b.get("limit", 10)), 1000), int(b.get("offset", 0))))

@router.post("/threads/{tid}/copy")
async def th_copy(tid: str):
    t = await _get_thread(tid); new = str(uuid.uuid4())
    await A.q("INSERT INTO lg_threads(thread_id,assistant_id,agent_id,metadata,workspace_id) VALUES(%s,%s,%s,%s,%s)", (new, t["assistant_id"], t["agent_id"], Jsonb(t["metadata"]), t["workspace_id"]))
    if t["agent_id"]:
        ag = await _graph(t)
        s = await ag.aget_state({"configurable": {"thread_id": f"a{t['agent_id']}:{tid}"}})
        if s.values: await ag.aupdate_state({"configurable": {"thread_id": f"a{t['agent_id']}:{new}"}}, s.values)
    return await _get_thread(new)

async def _graph(t):
    import deep_research as _dr
    g = await _dr.graph_for(t)   # GĐ7.7: a thread whose latest run was a deep-research run is read through the research agent's graph (its HITL node), same checkpoint key
    if g is not None: return g
    ag, _ = await A.build_agent(t["agent_id"], await A.cur_version(t["agent_id"])); return ag

def _content(c):
    """G4: text stays a string; multimodal content keeps its LangChain standard blocks (text + image URL blocks, never base64)."""
    if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") in ("image", "file") for b in c):
        return [({"type": "text", "text": b.get("text", "")} if b.get("type") in ("text", "text_delta") else
                 {k: b[k] for k in ("type", "url", "mime_type", "id", "extras") if k in b})
                for b in c if isinstance(b, dict) and b.get("type") in ("text", "text_delta", "image", "file") and (b.get("type") not in ("image", "file") or b.get("url"))]
    return A._text(c)

def _mv(m):
    d = {"type": m.type, "id": getattr(m, "id", None), "content": _content(m.content)}
    if getattr(m, "tool_calls", None): d["tool_calls"] = [{"id": c.get("id"), "name": c["name"], "args": c.get("args")} for c in m.tool_calls]
    if m.type == "tool": d["name"] = m.name; d["tool_call_id"] = m.tool_call_id; d["status"] = getattr(m, "status", None) or "success"   # GĐ9.Q: LangGraph's ToolMessage.status (auth-required / refused calls are "error")
    if getattr(m, "additional_kwargs", None): d["additional_kwargs"] = m.additional_kwargs      # G3: modality/voice metadata survives reload
    return d

def _vals(v):
    vals = dict(v or {}); vals["messages"] = [_mv(m) for m in vals.get("messages") or []]
    return {k: x for k, x in vals.items() if k in ("messages", "todos", "files")}

def _snap(s, subs=None):
    """subs: {checkpoint_ns: nested snapshot dict} for tasks that run a subgraph (G1, getState(subgraphs=True))."""
    subs = subs or {}
    def task(tk):
        ns = f"{tk.name}:{tk.id}"
        st = _snap(tk.state) if hasattr(getattr(tk, "state", None), "values") else subs.get(ns)
        return {"id": tk.id, "name": tk.name, "path": list(tk.path) if getattr(tk, "path", None) else None, "checkpoint_ns": ns,
                "interrupts": [_iv(i, _ns_of(i, subs)) for i in tk.interrupts], "state": st}
    return _j({"values": _vals(s.values), "next": list(s.next),
               "checkpoint": {"checkpoint_id": s.config["configurable"].get("checkpoint_id"), "thread_id": None, "checkpoint_ns": s.config["configurable"].get("checkpoint_ns", "")},
               "metadata": s.metadata, "created_at": s.created_at, "parent_checkpoint": (s.parent_config or {}).get("configurable", {}).get("checkpoint_id"),
               "tasks": [task(tk) for tk in (s.tasks or [])],
               "interrupts": [_iv(i, _ns_of(i, subs)) for i in (s.interrupts or [])]})

def _ns_of(i, subs):
    """Namespace of the (deepest) subgraph that raised interrupt `i` (same interrupt id bubbles up to the parent), else None."""
    iid = getattr(i, "id", None); hit = None
    for ns, st in (subs or {}).items():
        if iid and any(x.get("id") == iid for x in st.get("interrupts") or []):
            if hit is None or len(ns) > len(hit): hit = ns
    return hit.split("|") if hit else None

def _iv(i, ns=None):
    """G16: LangGraph API shape for an interrupt: {"value": ..., "id": ...} (was the raw value). G1: + "ns" when raised in a subgraph."""
    d = {"value": i.value, "id": getattr(i, "id", None) or getattr(i, "interrupt_id", None)}
    if ns: d["ns"] = ns
    return d

async def _state(t, checkpoint_id=None, subgraphs=False, checkpoint_ns=""):
    if not t["agent_id"]: return {"values": {}, "next": [], "checkpoint": None, "metadata": {}, "tasks": [], "interrupts": []}
    ag = await _graph(t); key = f"a{t['agent_id']}:{t['thread_id']}"; c = {"configurable": {"thread_id": key}}
    if checkpoint_id: c["configurable"]["checkpoint_id"] = checkpoint_id
    if checkpoint_ns:      # G1: state of one subgraph namespace (POST /state/checkpoint {checkpoint_ns})
        sub = await _sub_state(key, checkpoint_ns, checkpoint_id, recurse=subgraphs)
        if sub is None: raise HTTPException(404, f"no checkpoint for namespace {checkpoint_ns}")
        sub["checkpoint"]["thread_id"] = t["thread_id"]; return sub
    s = await ag.aget_state(c, subgraphs=subgraphs)      # official: fills task.state for statically registered subgraphs
    subs = {}
    if subgraphs or s.interrupts:   # interrupts are always tagged with their subgraph namespace (cheap: only pending tasks)
        for tk in s.tasks or []:
            if hasattr(getattr(tk, "state", None), "values"): continue
            ns = f"{tk.name}:{tk.id}"
            sub = await _sub_state(key, ns, recurse=True)
            if sub: subs.update(_flatten(ns, sub))
    out = _snap(s, subs)
    if not subgraphs:
        for x in out["tasks"]: x.pop("state", None)
    out["checkpoint"]["thread_id"] = t["thread_id"]; return out

def _flatten(ns, sub):
    d = {ns: sub}
    for x in sub.get("tasks") or []:
        if x.get("state"): d.update(_flatten(x["checkpoint_ns"], x["state"]))
    return d

async def _sub_state(key, ns, checkpoint_id=None, recurse=True, depth=0):
    """G1: snapshot of a subgraph that LangGraph cannot resolve statically: a Deep Agents sub-agent runs inside the `task` TOOL
    (create_agent graph invoked from the tools node), so Pregel.get_subgraphs() does not list it and aget_state(subgraphs=True)
    leaves task.state=None. Its checkpoints are still written by LangGraph under checkpoint_ns "<node>:<task_id>" of the same
    thread; read them through the official checkpointer API (BaseCheckpointSaver.aget_tuple) and shape them like a StateSnapshot:
    values = channel values, next = nodes with a pending `branch:to:<node>` trigger, interrupts = pending `__interrupt__` writes."""
    cfg = {"configurable": {"thread_id": key, "checkpoint_ns": ns}}
    if checkpoint_id: cfg["configurable"]["checkpoint_id"] = checkpoint_id
    tup = await A.S["cp"].aget_tuple(cfg)
    if not tup: return None
    cv = tup.checkpoint.get("channel_values") or {}
    nxt = [k[len("branch:to:"):] for k in cv if k.startswith("branch:to:")]
    ints, by_task = [], {}
    for task_id, ch, val in tup.pending_writes or []:
        if ch == "__interrupt__":
            for i in (val if isinstance(val, (list, tuple)) else [val]):
                iv = _iv(i, ns.split("|")); ints.append(iv); by_task.setdefault(task_id, []).append(iv)
    tasks = []
    for task_id, its in by_task.items():
        child = None
        if recurse and depth < 4:      # nested sub-agent (a sub-agent that itself delegated): "<ns>|<node>:<task_id>"
            for node in nxt or ["tools"]:
                child = await _sub_state(key, f"{ns}|{node}:{task_id}", None, True, depth + 1)
                if child: cns = f"{ns}|{node}:{task_id}"; break
        tasks.append({"id": task_id, "name": (nxt or [None])[0], "checkpoint_ns": cns if child else None, "interrupts": its, "state": child})
    md = tup.metadata or {}
    return _j({"values": _vals(cv), "next": nxt,
               "checkpoint": {"checkpoint_id": tup.config["configurable"].get("checkpoint_id"), "thread_id": None, "checkpoint_ns": ns},
               "metadata": md, "created_at": tup.checkpoint.get("ts"),
               "parent_checkpoint": (tup.parent_config or {}).get("configurable", {}).get("checkpoint_id"),
               "tasks": tasks, "interrupts": ints})

def _bool(x): return str(x).lower() in ("1", "true", "yes")

@router.get("/threads/{tid}/state")
async def th_state(tid: str, checkpoint_id: str | None = None, subgraphs: str | None = None):
    return await _state(await _get_thread(tid), checkpoint_id, _bool(subgraphs))

@router.post("/threads/{tid}/state/checkpoint")
async def th_state_checkpoint(tid: str, b: dict):
    """LangGraph API: {"checkpoint": {"checkpoint_id"?, "checkpoint_ns"?}, "subgraphs"?: bool} (read-only)."""
    ck = b.get("checkpoint") or {}
    return await _state(await _get_thread(tid), ck.get("checkpoint_id"), bool(b.get("subgraphs")), ck.get("checkpoint_ns") or "")

@router.get("/threads/{tid}/state/{checkpoint_id}")
async def th_state_at(tid: str, checkpoint_id: str, subgraphs: str | None = None):
    return await _state(await _get_thread(tid), checkpoint_id, _bool(subgraphs))

async def bind_thread(t, assistant_or_graph: str | None = None):
    """G15: bind a thread that has never run to an assistant (explicit id/graph_id, else thread.metadata.graph_id / assistant_id).
    Used by run creation, POST /threads/{tid}/state and the voice session endpoint, so a voice-first thread can be written to."""
    if t["agent_id"]: return t
    key = assistant_or_graph or (t.get("metadata") or {}).get("assistant_id") or (t.get("metadata") or {}).get("graph_id")
    if not key: return t
    a = await _get_assistant(key)
    await A.q("UPDATE lg_threads SET agent_id=%s, assistant_id=%s WHERE thread_id=%s AND agent_id IS NULL", (a["agent_id"], a["assistant_id"], t["thread_id"]))
    return await A.q("SELECT * FROM lg_threads WHERE thread_id=%s", (t["thread_id"],), one=True)

@router.post("/threads/{tid}/state")
async def th_update_state(tid: str, b: dict):
    """Cập nhật state: {"values": {"messages":[{"role":"user","content":"..."}]}, "as_node": tuỳ chọn, "checkpoint_id": tuỳ chọn,
    "assistant_id": tuỳ chọn (G15: gắn thread chưa chạy lần nào; mặc định metadata.graph_id)}."""
    t = await bind_thread(await _get_thread(tid), b.get("assistant_id"))
    if not t["agent_id"]: raise HTTPException(409, "Thread chưa gắn assistant (chưa chạy lần nào)")
    ag = await _graph(t); c = {"configurable": {"thread_id": f"a{t['agent_id']}:{tid}"}}
    cid = b.get("checkpoint_id") or (b.get("checkpoint") or {}).get("checkpoint_id")   # GĐ9.5: langgraph-sdk sends {"checkpoint": {...}}
    if cid: c["configurable"]["checkpoint_id"] = cid
    nc = await ag.aupdate_state(c, b.get("values") or {}, as_node=b.get("as_node"))
    return {"checkpoint": {"thread_id": tid, "checkpoint_id": nc["configurable"].get("checkpoint_id")}}

@router.api_route("/threads/{tid}/history", methods=["GET", "POST"])
async def th_history(tid: str, limit: int = 10, request: Request = None):
    t = await _get_thread(tid); before = flt = None
    if request is not None and request.method == "POST":
        try:
            body = await request.json(); limit = int(body.get("limit", limit))
            bb = body.get("before") or {}; bid = bb.get("checkpoint_id") or (bb.get("configurable") or {}).get("checkpoint_id")   # GĐ9.5: langgraph-sdk get_history(before=...)
            before = {"configurable": {"thread_id": f"a{t['agent_id']}:{tid}", "checkpoint_id": bid}} if bid and t["agent_id"] else None
            flt = body.get("metadata") or None
        except Exception: pass  # noqa: BLE001
    if not t["agent_id"]: return []
    ag = await _graph(t); out = []
    async for s in ag.aget_state_history({"configurable": {"thread_id": f"a{t['agent_id']}:{tid}"}}, filter=flt, before=before, limit=min(limit, 200)):
        x = _snap(s); x["checkpoint"]["thread_id"] = tid; out.append(x)
    return out

# ------------------------------------------------------------ runs
class RunIn(BaseModel):
    assistant_id: str
    input: dict | None = None
    command: dict | None = None             # {"resume": ...} cho HITL
    checkpoint_id: str | None = None
    checkpoint: dict | None = None          # GĐ9.5: LangGraph API {"checkpoint_id": ...} (langgraph-sdk `checkpoint=`); fork = run from a past checkpoint
    metadata: dict = {}
    config: dict = {}
    webhook: str | None = Field(default=None, max_length=500)
    multitask_strategy: str = Field(default="enqueue", pattern=r"^(reject|enqueue|interrupt|rollback)$")
    stream_mode: list[str] | str = ["values"]
    if_not_exists: str = Field(default="reject", pattern=r"^(reject|create)$")
    after_seconds: int | None = Field(default=None, ge=0, le=86400)
    on_completion: str = Field(default="delete", pattern=r"^(delete|keep)$")   # stateless
    on_disconnect: str = Field(default="continue", pattern=r"^(cancel|continue)$")   # G11: LangGraph API; SDK/assistant-ui send "cancel"
    stream_subgraphs: bool = False          # G1: LangGraph API — subgraph events as "<mode>|<checkpoint_ns>"

def _modes(sm): return [sm] if isinstance(sm, str) else list(sm or ["values"])

async def _spend_guard(agent_id, model=None, feature=None):
    """Spend caps (agent daily budget, provider cap) checked before the run is queued -> HTTP 402 with the reason, which the chat
    shows instead of a run that fails silently. Nothing is queued or sent to a model."""
    import chat_settings
    cfg = await chat_settings._agent_cfg(agent_id)
    # GĐ8.P: every 402 here carries a machine-readable detail (gateway.cap_detail: code spend_cap + scope/period/limit/used + the human sentence in `message`)
    try: await A.budget_check(agent_id, cfg.get("budget_usd_day"))
    except RuntimeError as e: raise HTTPException(402, A.gw.cap_detail(e, {"feature": feature})) from None
    await A.seed_spend()
    chain = [model or cfg.get("model") or A.DEFAULT_MODEL] + list(cfg.get("fallback_models") or A.gw.DEFAULT_FALLBACKS)
    msg = A.gw.cap_message(chain)
    if msg: raise HTTPException(402, A.gw.cap_detail(msg, {**A.gw.cap_info(chain), "feature": feature}))
    tok = A.gw.FEATURE.set(feature) if feature else None   # GĐ7.1: built-in assistant => feature caps (scope=feature, target=assistant) apply to the pre-check too
    try: await A.gw.budget_check(A.gw.ctx_now([A.gw.provider(m) for m in A.gw._usable(chain)], agent_id=agent_id, models=chain))   # unified caps: workspace / user / evaluator / feature / agent / provider
    except A.gw.BudgetExceeded as e: raise HTTPException(402, A.gw.cap_detail(e)) from None
    finally:
        if tok is not None: A.gw.FEATURE.reset(tok)

async def _viewer_guard_run(u, a):
    """8.P: a non-demo viewer may run ONLY agents shared to them (agent_shares role use|edit); not the built-in assistant, not other agents."""
    import admin as _adm
    if not u or _adm.is_demo(u) or _adm.ROLES.get(u.get("role") or "viewer", 1) >= _adm.ROLES["editor"]: return
    import agent_sharing
    try: x = await agent_sharing.access(a.get("agent_id")) if a.get("agent_id") else {}
    except HTTPException: x = {}
    if x.get("share") not in ("use", "edit") and not x.get("owner"):
        raise HTTPException(403, "Bạn chỉ chat được với agent được chia sẻ cho bạn / viewers can only chat with agents shared with them")

async def _create_run(tid, b: RunIn, temporary=False):
    a = await _get_assistant(b.assistant_id)
    import admin as _adm
    await _adm.demo_guard_run(_adm.cur(), a, b)   # demo account: agent allow-list, no HITL approval/webhook, 1 run at a time, daily $ cap
    await _viewer_guard_run(_adm.cur(), a)
    _m = run_overrides({}, b.config).get("model")   # GĐ3.5: per-run model from the chat composer -> must be in the agent's allow-list
    _cm = run_chat_mode({}, b.config)
    if _cm is not None:   # 27/10 chat mode (fast | standard | deep): must be enabled for the agent (403); it wins over `model` (resolved from the agent's own allow-list, so no second check)
        import chat_settings
        _m = (await chat_settings.resolve_run_mode(a["agent_id"], _cm))["model"]
    elif _m is not None:
        import chat_settings
        await chat_settings.check_run_model(a["agent_id"], _m)
    import assistant as _as
    builtin = _as.is_builtin(a); pctx = None
    if builtin:   # GĐ7.1: page context travels in the run input as `context`; keep it out of the graph input, store a sanitised copy in the run metadata
        pctx = _as.clean_context((b.input or {}).get("context"))
    if b.input and "context" in b.input: b.input = {k: v for k, v in b.input.items() if k != "context"} or None
    import deep_research as _dr
    _dm = None if builtin else _dr.run_mode(b.config)   # GĐ7.7: configurable.mode == "deep_research" (allow-listed value)
    if _dm and _adm.is_demo(_adm.cur()): raise HTTPException(403, "Tài khoản demo không dùng được chế độ Nghiên cứu sâu (cần duyệt kế hoạch) / Deep research needs plan approval, not available for demo accounts")
    import channels as _ch   # GĐ8.1: a channel message (metadata.channel) is costed under feature 'channel'
    await _spend_guard(a["agent_id"], _m, _as.FEATURE if builtin else (_dr.FEATURE if _dm else (_ch.FEATURE if (b.metadata or {}).get("channel") else None)))
    t = await A.q("SELECT * FROM lg_threads WHERE thread_id=%s AND workspace_id=%s", (tid, _ws()), one=True)
    if not t:
        if b.if_not_exists != "create" and not temporary: raise HTTPException(404, "Không có thread (đặt if_not_exists=create)")
        t = await A.q("INSERT INTO lg_threads(thread_id,workspace_id,metadata) VALUES(%s,%s,%s) RETURNING *", (tid, _ws(), Jsonb({"temporary": temporary} if temporary else {})), one=True)
    if t["agent_id"] and t["agent_id"] != a["agent_id"]: raise HTTPException(409, "Thread đã gắn với graph khác")
    if not t["agent_id"]:
        await A.q("UPDATE lg_threads SET agent_id=%s, assistant_id=%s WHERE thread_id=%s", (a["agent_id"], a["assistant_id"], tid))
    if builtin and (t["metadata"] or {}).get("source") != "assistant":   # assistant threads never appear in the chat sidebar / thread search (source='chat')
        await A.q("UPDATE lg_threads SET metadata = metadata || %s WHERE thread_id=%s", (Jsonb({"source": "assistant"}), tid))
    if not b.checkpoint_id and (b.checkpoint or {}).get("checkpoint_id"): b.checkpoint_id = str(b.checkpoint["checkpoint_id"])
    if not b.input and not b.command and not b.checkpoint_id: raise HTTPException(422, "Cần input, command hoặc checkpoint_id")
    if b.command:   # GĐ9.P1: LangGraph Command = resume | update | goto (goto: node name, list of names, or {node, input} = Send)
        bad = set(b.command) - {"resume", "update", "goto"}
        if bad: raise HTTPException(422, f"command chỉ nhận resume / update / goto (thừa: {sorted(bad)}) / command accepts resume, update, goto only")
        gt_ = b.command.get("goto")
        if gt_ is not None and not (isinstance(gt_, (str, dict)) or (isinstance(gt_, list) and all(isinstance(g, (str, dict)) and (not isinstance(g, dict) or g.get("node")) for g in gt_))) or (isinstance(gt_, dict) and not gt_.get("node")):
            raise HTTPException(422, "command.goto = tên node, danh sách tên node hoặc {node, input} / command.goto must be a node name, a list of names or {node, input}")
        if b.command.get("update") is not None and not isinstance(b.command["update"], (dict, list)): raise HTTPException(422, "command.update phải là object (hoặc danh sách [khoá, giá trị]) / command.update must be an object")
    if b.input: b.input = await _normalize_input(b.input, tid)   # G4: images -> locally stored standard content blocks (413/415 here, not mid-run)
    appr = await _claim_approval(tid, b.command)
    active = await A.q("SELECT run_id, status, pre_checkpoint FROM lg_runs WHERE thread_id=%s AND status IN ('pending','running') ORDER BY created_at", (tid,))
    ckpt = b.checkpoint_id
    if active:
        if b.multitask_strategy == "reject": raise HTTPException(409, "Thread đang có run chạy (multitask_strategy=reject)")
        if b.multitask_strategy in ("interrupt", "rollback"):
            for r in active:
                await _cancel(str(r["run_id"]), b.multitask_strategy)
                if b.multitask_strategy == "rollback" and r["pre_checkpoint"] and not ckpt: ckpt = r["pre_checkpoint"]
    rid = str(uuid.uuid4())
    await A.q("""INSERT INTO lg_runs(run_id,thread_id,assistant_id,multitask_strategy,input,command,checkpoint_id,stream_mode,webhook,metadata,temporary,run_at,workspace_id,config)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now()+%s*interval '1 second',%s,%s)""",
              (rid, tid, a["assistant_id"], b.multitask_strategy, _sr(rid, "input", Jsonb(b.input) if b.input else None), _sr(rid, "command", Jsonb(b.command) if b.command else None), ckpt,
               _modes(b.stream_mode), b.webhook, Jsonb({**b.metadata, **({"on_completion": b.on_completion} if temporary else {}), **({"approval_id": appr} if appr else {}), **({"page_context": pctx} if pctx else {}), **__import__("otel_ctx").run_meta(), **__import__("caller").run_meta()}), temporary, b.after_seconds or 0, _ws(), _sr(rid, "config", Jsonb(b.config or {}))))   # GĐ9.10 W3C trace context travels with the queued run
    import admin as _adm
    await A.q("UPDATE lg_runs SET user_id=%s, stream_subgraphs=%s WHERE run_id=%s", ((_adm.cur() or {}).get("id"), b.stream_subgraphs, rid))   # caller identity → per-user OAuth tokens in the worker
    await A.q("UPDATE lg_threads SET updated_at=now() WHERE thread_id=%s", (tid,))
    KICK.set()
    return await _run_row(rid)

async def _claim_approval(tid, command):
    """G7: resuming a HITL interrupt from the chat UI must resolve the same approvals row the Inbox shows.
    Atomically moves the latest 'pending' row of this thread to 'processing' (records the decision); 409 if the Inbox already decided."""
    resume = (command or {}).get("resume") if isinstance(command, dict) else None
    if not isinstance(resume, dict) or not isinstance(resume.get("decisions"), list): return None
    row = await A.q("""UPDATE approvals SET status='processing', decision=%s, decided=now() WHERE id=(
                         SELECT id FROM approvals WHERE thread_id=%s AND status='pending' ORDER BY id DESC LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id""",
                    (Jsonb({"decisions": resume["decisions"]}), tid), one=True)
    if row: return row["id"]
    last = await A.q("SELECT id, status FROM approvals WHERE thread_id=%s ORDER BY id DESC LIMIT 1", (tid,), one=True)
    if last and last["status"] != "pending": raise HTTPException(409, f"Approval #{last['id']} đã được xử lý ({last['status']})")
    return None

def _approval_status(decisions):
    kinds = {d.get("type") for d in decisions or [] if isinstance(d, dict)}
    return "rejected" if kinds == {"reject"} else "edited" if "edit" in kinds else "approved"

async def _run_row(rid):
    r = await A.q("SELECT * FROM lg_runs WHERE run_id=%s", (rid,), one=True)
    if not r: raise HTTPException(404, "Không có run")
    return r

async def _cancel(rid, action="interrupt"):
    r = await A.q("SELECT status, thread_id FROM lg_runs WHERE run_id=%s", (rid,), one=True)
    if not r or r["status"] in TERMINAL: return False
    task = TASKS.get(rid)
    if task: task.cancel()
    if action == "rollback":
        await A.q("DELETE FROM lg_run_events WHERE run_id=%s", (rid,)); await A.q("DELETE FROM lg_runs WHERE run_id=%s", (rid,))
    else:
        await A.q("UPDATE lg_runs SET status='interrupted', error=coalesce(error,'bị huỷ (interrupt)'), ended_at=now() WHERE run_id=%s AND status IN ('pending','running')", (rid,))
        await _emit(rid, "error", {"error": "cancelled", "message": "run bị huỷ"}); await _emit(rid, "end", None)
    await A.q("UPDATE lg_threads SET status='idle' WHERE thread_id=%s AND NOT EXISTS (SELECT 1 FROM lg_runs WHERE thread_id=%s AND status='running')", (r["thread_id"], r["thread_id"]))
    return True

def _sr(rid, col, j, ws=None):
    """GĐ9.P2: payload của run -> phong bì AES-GCM khi workspace bật mã hoá 'runs' (đọc lại trong A.q, trong suốt)."""
    return __import__("aos_crypto").seal_jsonb("runs", "lg_runs", col, rid, j, ws if ws is not None else _ws())

SEQ: dict = {}
async def _emit(rid, event, data):
    """Append a stream event; seq is allocated in the DB so two processes (API cancel + worker) never collide."""
    for _ in range(4):
        try:
            ews = await __import__("aos_crypto").run_ws(rid)
            if ews is not None:   # mã hoá 'runs': seq phải biết trước để gắn vào AAD của dòng (PK trùng -> vòng thử lại)
                seq = (await A.q("SELECT coalesce(max(seq),0)+1 s FROM lg_run_events WHERE run_id=%s", (rid,), one=True))["s"]
                await A.q("INSERT INTO lg_run_events(run_id,seq,event,data) VALUES(%s,%s,%s,%s)",
                          (rid, seq, event, __import__("aos_crypto").seal_jsonb("runs", "lg_run_events", "data", f"{rid}:{seq}", Jsonb(_j(data)), ews))); return
            await A.q("INSERT INTO lg_run_events(run_id,seq,event,data) SELECT %s, coalesce(max(seq),0)+1, %s, %s FROM lg_run_events WHERE run_id=%s",
                      (rid, event, Jsonb(_j(data)), rid)); return
        except Exception as e:  # noqa: BLE001
            err = e; await asyncio.sleep(0.05)
    print("emit", err)

def _user_msg(inp):
    """G2: (id, additional_kwargs) of the last human input message, so the stored HumanMessage keeps the client's id."""
    msgs = (inp or {}).get("messages") or []
    if isinstance(msgs, dict): msgs = [msgs]
    for m in reversed(msgs):
        if isinstance(m, dict) and (m.get("role") or m.get("type")) in ("user", "human"):
            return {k: v for k, v in (("id", m.get("id")), ("additional_kwargs", m.get("additional_kwargs"))) if v}
    return {}

async def _normalize_input(inp, thread_id=None):
    import attachments
    msgs = inp.get("messages")
    if isinstance(msgs, dict): msgs = [msgs]
    if not isinstance(msgs, list): return inp
    out = []
    for m in msgs:
        if isinstance(m, dict) and isinstance(m.get("content"), list): m = {**m, "content": await attachments.normalize_content(m["content"], thread_id)}
        out.append(m)
    return {**inp, "messages": out}

def _user_content(inp):
    """G4: the last human message's content as sent: str, or standard content blocks when it carries images."""
    msgs = (inp or {}).get("messages") or []
    if isinstance(msgs, dict): msgs = [msgs]
    for m in reversed(msgs):
        if isinstance(m, dict) and (m.get("role") or m.get("type")) in ("user", "human") and isinstance(m.get("content"), list) \
                and any(isinstance(b, dict) and b.get("type") in ("image", "file") for b in m["content"]):
            return m["content"]
    return _user_text(inp)

def _user_text(inp):
    msgs = (inp or {}).get("messages") or []
    if isinstance(msgs, dict): msgs = [msgs]
    for m in reversed(msgs):
        if isinstance(m, str): return m
        if (m.get("role") or m.get("type")) in ("user", "human"):
            c = m.get("content"); return c if isinstance(c, str) else " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    return None

def run_overrides(acfg, rcfg):
    """Gộp cấu hình: assistant.config < run.config. Nhận cả dạng {"configurable": {...}} và khoá phẳng."""
    out = {}
    for c in (acfg or {}, rcfg or {}):
        out.update({k: v for k, v in c.items() if k != "configurable"})
        out.update(c.get("configurable") or {})
    return {k: v for k, v in out.items() if k in A.OVERRIDABLE}

def run_chat_mode(acfg, rcfg):
    """configurable.chat_mode (or a flat `chat_mode`) of the assistant config < run config; None when absent."""
    out = None
    for c in (acfg or {}, rcfg or {}):
        for src in (c, c.get("configurable") or {}):
            if src.get("chat_mode") is not None: out = src["chat_mode"]
    return out

async def apply_chat_mode(ov, agent_id, mode):
    """27/10 chat mode: model + thinking level + per-mode timeout for THIS run (the agent's fallback chain still applies); wins over configurable.model.
    No agent rebuild: the resolved mode becomes the run's LangChain runtime context (chat_modes.RunContext, passed as astream(context=...)); `ov` only loses a conflicting `model`."""
    import chat_settings, chat_modes as CM
    rm = await chat_settings.resolve_run_mode(agent_id, mode)
    ov.pop("model", None)
    CM.RUN_CTX.set(CM.RunContext(chat_mode=rm["id"], model=rm["model"], thinking=rm["thinking"], timeout_s=rm["timeout_s"]))
    return rm

async def _execute(run):
    """Runs in the queue worker task; restores the caller identity so tools (e.g. oauth_request) act as the user who created the run."""
    import admin as _adm
    import gateway as _gw, demo_ws as _dws
    _gw.RUN_WS.set(run.get("workspace_id"))          # demo workspace => Gemini only even for runs without a user (cron/background)
    try: await _dws.load_demo_ws()
    except Exception: pass  # noqa: BLE001  (keeps the last known set)
    import chat_settings as _cs
    _md = run.get("metadata") or {}
    __import__("otel_ctx").restore(_md)   # GĐ9.10: always set/cleared (a reused task must not inherit the previous run's trace context)
    __import__("caller").restore(_md)     # GĐ9.Q: who the run acts as + allow-listed request headers (signed at creation); always set/cleared
    _chan = f"b{_md.get('binding_id')}:{str(_md.get('external_user') or '')[:120]}" if _md.get("channel") else None   # GĐ8.Z: external chatter key
    __import__("run_trust").begin(bool(_chan) or _md.get("trust") == "external", mail=_md.get("trust") == "external" and bool(_md.get("mail_id")) and bool(_md.get("binding_id")))   # F-9 mail=: only run metadata the mail trigger writes; GĐ11.72: channel runs + metadata trust=external (mail trigger 11.60) = external source; set/cleared on every run
    # GĐ6.9: always set (a reused task must not carry the previous run's text); never the binding owner's personal instructions on a channel run
    _cs.INSTR.set("" if _chan else await _cs.instructions_text(run.get("user_id")))
    __import__("chat_modes").RUN_CTX.set(None)   # chat mode: always reset (a reused task must not carry the previous run's mode); apply_chat_mode sets it
    if run.get("user_id"):
        u = await A.q("SELECT u.id, u.username, m.role, m.workspace_id ws, u.demo, u.daily_cap_usd, u.demo_agents FROM users u LEFT JOIN memberships m ON m.user_id=u.id AND m.workspace_id=%s WHERE u.id=%s",
                      (run.get("workspace_id") or 1, run["user_id"]), one=True)
        if u:
            row = dict(u); ident = {k: row[k] for k in ("id", "username", "role", "ws")}
            _adm.USER.set(_adm._with_demo({**ident, "role": ident.get("role") or "viewer", "ws": ident.get("ws") or run.get("workspace_id") or 1,
                                       "auth": "channel" if _chan else "run", **({"chat": _chan} if _chan else {})}, row))   # GĐ8.Z: channel runs = no owner accounts/memories
    _st = None if _chan or _md.get("trust") == "external" or not run.get("thread_id") else await A.q("SELECT (metadata ->> 'mail') = 'true' AS mail FROM lg_threads WHERE thread_id=%s AND metadata ->> 'source' = 'channel'", (run["thread_id"],), one=True)
    if _st:
        __import__("run_trust").begin(True, mail=bool(_st.get("mail")))   # F-9: a mail thread resumed from the Inbox stays a mail run (reads of the owner's own mailbox keep running; the call-time check still applies)   # GĐ11.72: a stranger's thread stays external when someone else resumes it (Inbox approval): the flag cannot be lowered by who clicks
    import chat_projects as _cp
    _cp.CTX.set(await _cp.run_context(run.get("thread_id")))   # GĐ8.9: always set, as the run's user (RLS): project instructions/files/memory of this chat
    return await _execute_inner(run)

async def _execute_inner(run):
    rid = str(run["run_id"]); tid = run["thread_id"]
    a = await A.q("SELECT * FROM lg_assistants WHERE assistant_id=%s", (run["assistant_id"],), one=True)
    final = None; status = "success"; err = None; pause_reqs = []; out_files = []
    try:
        if not a: raise RuntimeError("assistant đã bị xoá")
        import assistant as _as, gateway as _gwm
        _bi = _as.is_builtin(a)   # GĐ7.1: every model call of the built-in assistant is tagged feature='assistant' (spend caps) and sees the page context
        import deep_research as _dr
        _dm = (not _bi) and (_dr.run_mode(run.get("config")) or await _dr.resuming(tid, run))   # GĐ7.7: research mode = the workspace's hidden research agent runs on this thread (feature 'deep_research')
        import channels as _ch   # GĐ8.1: channel runs (metadata.channel) -> feature 'channel'
        _gwm.FEATURE.set(_as.FEATURE if _bi else (_dr.FEATURE if _dm else (_ch.FEATURE if (run.get("metadata") or {}).get("channel") else None))); _as.PAGE.set((run.get("metadata") or {}).get("page_context") if _bi else None)
        _dr.begin(bool(_dm), await _dr.user_lang(run.get("user_id")) if _dm else None)   # GĐ8.P: stop message in the user's UI language
        ver = a["agent_version"] or await A.cur_version(a["agent_id"])
        ov = run_overrides(a["config"], run.get("config"))
        _cm = None if (_bi or _dm) else run_chat_mode(a["config"], run.get("config"))
        if _cm is not None: await apply_chat_mode(ov, a["agent_id"], _cm)
        run_aid, run_ver, run_ov = a["agent_id"], ver, ov
        ctx = {**(a.get("context") or {}), **((run.get("metadata") or {}).get("context") or {})}
        if ctx:
            base = ov.get("system_prompt")
            if base is None:
                base = ((await A.q("SELECT config FROM agent_versions WHERE agent_id=%s AND version=%s", (a["agent_id"], ver), one=True)) or {}).get("config", {}).get("system_prompt") or ""
            ov["system_prompt"] = (base + "\n\n" if base else "") + "Runtime context (JSON, trusted, set by the assistant owner): " + json.dumps(ctx, ensure_ascii=False, default=str)[:2000]
        tmeta = {}
        if _dm:
            run_aid = (await _dr.ensure())["id"]; run_ver = await A.cur_version(run_aid); run_ov = {k: v for k, v in ov.items() if k == "model"}
            tmeta = {_dr.META: run_aid}
            await A.q("UPDATE lg_threads SET metadata = metadata || %s WHERE thread_id=%s", (Jsonb(tmeta), tid))
        else:
            await A.q("UPDATE lg_threads SET metadata = metadata - %s::text WHERE thread_id=%s AND metadata ->> %s::text IS NOT NULL", (_dr.META, tid, _dr.META))
        ag, _ = await A.build_agent(run_aid, run_ver, run_ov)
        pre = await ag.aget_state({"configurable": {"thread_id": f"a{a['agent_id']}:{tid}"}})
        _ck = pre.config["configurable"].get("checkpoint_id")
        import durable as _dur
        retry = _dur.resume_mode(run, _ck)   # GĐ13.30: a re-execution after the worker died: resume from the last checkpoint with an empty input (the first attempt's input + finished steps are in it)
        if run.get("start_ckpt") is None: await A.q("UPDATE lg_runs SET pre_checkpoint=%s, start_ckpt=%s WHERE run_id=%s", (_ck, _ck or "-", rid))   # first time the run reaches the graph (later attempts keep the original)
        await _emit(rid, "metadata", {"run_id": rid, "thread_id": tid, "assistant_id": str(a["assistant_id"])})
        if "events" in set(run["stream_mode"] or []): await _emit(rid, "events", _ev("on_chain_start", f"agent_{a['agent_id']}", rid, {"input": run["input"]}))
        modes = run["stream_mode"] or ["values"]
        resume = (run["command"] or {}).get("resume") if run["command"] and not retry else None
        msg = _user_content(run["input"]) if not retry else None
        acc = ""; last_ckpt = None
        async def _values():   # G12: `values` after every step (not only at the end of the run), skipped when the checkpoint did not move
            nonlocal last_ckpt
            st = await _state({"agent_id": a["agent_id"], "thread_id": tid, "metadata": tmeta})
            ck = (st.get("checkpoint") or {}).get("checkpoint_id")
            if ck != last_ckpt: last_ckpt = ck; await _emit(rid, "values", st["values"])
        subg = bool(run.get("stream_subgraphs")); key = f"a{a['agent_id']}:{tid}"; sub_ck: dict = {}
        async def _sub_values(nsl):   # G1: "values|<ns>" = the sub-agent's own state after its step (checkpointer read by namespace)
            ns = "|".join(nsl); st = await _sub_state(key, ns, recurse=False)
            if st and st["checkpoint"]["checkpoint_id"] != sub_ck.get(ns):
                sub_ck[ns] = st["checkpoint"]["checkpoint_id"]; await _emit(rid, f"values|{ns}", st["values"])
        async for ev in A.run_agent(run_aid, run_ver, tid, msg, resume=resume, checkpoint_id=None if retry else run["checkpoint_id"], tags=["agent-server", f"run:{rid[:8]}"] + (["deep_research"] if _dm else []),
                                    project="agents", deployment=f"assistant:{str(a['assistant_id'])[:8]}", overrides=run_ov, thread_agent=a["agent_id"] if _dm else None,
                                    tokens=bool({"messages", "messages-tuple", "events"} & set(modes)), msg_meta=None if retry else _user_msg(run["input"]), sub_tokens=subg,
                                    native=tuple(m for m in ("custom", "tasks", "checkpoints", "debug") if m in modes), cmd=None if retry else (run["command"] or None)):
            nsl = ev.get("ns") or []
            if ev["type"] == "step" and ev.get("kind") == "result" and "[aos-files]" in (ev.get("text") or ""):   # GĐ12.77: files from /workspace/out (channels send them as media)
                out_files += [x for x in __import__("channels").out_files_of(ev["text"]) if x not in out_files]
            if ev["type"] == "native":   # GĐ9.6: LangGraph-native modes, event name = mode (+ "|<ns>" for subgraphs with stream_subgraphs)
                if not nsl or subg: await _emit(rid, ev["mode"] + (("|" + "|".join(nsl)) if nsl else ""), _nj(ev["data"]))
                continue
            if "events" in modes and not nsl:   # GĐ9.6: events mode (astream_events v2 shape, synthesised): model stream chunks + tool start/end
                if ev["type"] == "token":
                    await _emit(rid, "events", _ev("on_chat_model_stream", "model", rid, {"chunk": {"type": "AIMessageChunk", "content": ev["text"], "id": ev.get("id") or f"run-{rid}"}}, metadata={"langgraph_node": ev.get("node")}))
                elif ev["type"] == "step" and ev["kind"] != "result":
                    await _emit(rid, "events", _ev("on_tool_start", ev.get("name"), ev.get("tool_call_id") or rid, {"input": ev.get("args")}))
                elif ev["type"] == "step" and ev["kind"] == "result":
                    await _emit(rid, "events", _ev("on_tool_end", ev.get("name"), ev.get("tool_call_id") or rid, {"output": ev.get("text")}))
            if subg and nsl:       # G1 stream_subgraphs: subgraph events carry their namespace in the event name (LangGraph API)
                tag = "|".join(nsl)
                if ev["type"] == "token":
                    if "messages-tuple" in modes:
                        await _emit(rid, f"messages|{tag}", [{"type": "AIMessageChunk", "content": ev["text"], "id": ev.get("id") or f"run-{rid}-{tag}"},
                                                             {"langgraph_node": ev.get("node"), "langgraph_checkpoint_ns": tag, "run_id": rid}])
                elif ev["type"] == "step":
                    if "updates" in modes: await _emit(rid, f"updates|{tag}", {ev.get("node") or ev.get("name") or ev["kind"]: ev})
                    if "values" in modes: await _sub_values(nsl)
                elif ev["type"] == "sub_interrupt":
                    if "updates" in modes: await _emit(rid, f"updates|{tag}", {"__interrupt__": [{**i, "ns": nsl} for i in ev["interrupts"]]})
                continue
            if ev["type"] == "sub_interrupt": continue
            if ev["type"] == "token":
                acc += ev["text"]; mid = ev.get("id") or f"run-{rid}"      # G2: same id as the AIMessage that lands in state
                chunk = {"type": "AIMessageChunk", "content": ev["text"], "id": mid}
                if "messages-tuple" in modes: await _emit(rid, "messages", [chunk, {"langgraph_node": ev.get("node"), "run_id": rid}])
                if "messages" in modes: await _emit(rid, "messages/partial", [{"type": "ai", "content": acc, "id": mid}])
                continue
            if ev["type"] == "step":
                if "updates" in modes: await _emit(rid, "updates", {ev.get("name") or ev["kind"]: ev})
                if "values" in modes: await _values()
            elif ev["type"] == "interrupt":   # LangGraph shape {value,id} (+ns when raised inside a sub-agent, G1)
                ints = (await _state({"agent_id": a["agent_id"], "thread_id": tid, "metadata": tmeta})).get("interrupts") or [{"value": r} for r in ev["requests"]]
                await _emit(rid, "updates", {"__interrupt__": ints, "approval_id": ev["approval_id"]})
                pause_reqs = ev.get("requests") or []
            elif ev["type"] == "done":
                final = ev
        if final and final.get("error"): status, err = "error", final["error"]
        elif final and final.get("paused"): status = "interrupted"
        last_ckpt = None; await _values()      # final state is always emitted (join/wait read it)
        if "events" in modes: await _emit(rid, "events", _ev("on_chain_end", f"agent_{a['agent_id']}", rid, {"output": {"reply": (final or {}).get("reply")}}))
        if status == "error": await _emit(rid, "error", {"error": "RunError", "message": err})
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        status, err = "error", f"{type(e).__name__}: {str(e)[:400]}"
        await _emit(rid, "error", {"error": type(e).__name__, "message": err})
    out = {"reply": (final or {}).get("reply"), "trace_id": (final or {}).get("trace_id"), "approval_id": (final or {}).get("approval_id"),
           "tokens": (final or {}).get("tokens"), "cost_usd": (final or {}).get("cost_usd")}
    if out_files: out["files"] = out_files[:8]
    if (final or {}).get("structured_response") is not None: out.update({k: final[k] for k in ("structured_response", "structured_error") if k in final})   # GĐ13.7
    if status == "interrupted" and not out["approval_id"]:   # GĐ12.74/12.78: a pause that is not an Inbox approval (ask form / sign-in) -> channels can show it
        import channels as _ch; out.update(_ch.pause_info(pause_reqs))
    await A.q("UPDATE lg_runs SET status=%s, error=%s, output=%s, trace_id=%s, ended_at=now(), failure_kind=%s WHERE run_id=%s AND status='running'",
              (status, err, _sr(rid, "output", Jsonb(_j(out)), run.get("workspace_id")), (final or {}).get("trace_id"), "run_error" if status == "error" else None, rid))   # GĐ13.30: run_error = model / tool failure, never retried
    await A.q("UPDATE lg_threads SET status=%s, updated_at=now() WHERE thread_id=%s", ({"success": "idle", "interrupted": "interrupted"}.get(status, "error"), tid))
    if status == "interrupted":   # GĐ11.35: first pause for approval -> auto title (once, cheap call, never raises)
        import thread_inbox; await thread_inbox.auto_title(tid)
    import chat_gd5; await chat_gd5.index_thread(tid)   # GĐ5a: full-text search index (never raises)
    appr = (run.get("metadata") or {}).get("approval_id")
    if appr:   # G7: close the Inbox row this resume claimed
        dec = ((run.get("command") or {}).get("resume") or {}).get("decisions")
        await A.q("UPDATE approvals SET status=%s, result=%s WHERE id=%s AND status='processing'",
                  (_approval_status(dec) if status != "error" else "error", (out.get("reply") or err or "")[:4000], appr))
    await _emit(rid, "end", None)
    r = await A.q("SELECT * FROM lg_runs WHERE run_id=%s", (rid,), one=True)
    if r and r["webhook"]:
        await obs.deliver("webhook", r["webhook"], f"run {rid} {status}", json.dumps(_j({"run_id": rid, "thread_id": tid, "status": status, "output": out, "error": err}), ensure_ascii=False),
                          ws_id=r.get("workspace_id"))   # GĐ10.28: the outbox row belongs to the run's workspace
    if r and r["temporary"] and (r["metadata"] or {}).get("on_completion", "delete") == "delete":
        t = await A.q("SELECT * FROM lg_threads WHERE thread_id=%s", (tid,), one=True)
        if t: await _delete_thread(t)

KICK = asyncio.Event()
DRAINING = False          # GĐ13.30: set by shutdown(): no new claims, in-flight runs finish (AOS_BG_SHUTDOWN_GRACE_SECS), the rest goes back to `pending`
ATT: dict = {}            # run_id -> attempt this process claimed (a task whose row moved on - re-queued, claimed by someone else - is cancelled)

async def _run_task(run):
    """GĐ13.30: one claimed run = heartbeat + AOS_BG_JOB_TIMEOUT_SECS + a final state for anything that escapes _execute (a run is never left `running` by a crash of this code).
    A task cancelled by shutdown() leaves its row `running` on purpose: shutdown() releases it to `pending`."""
    import admin as _adm, durable as D
    rid = str(run["run_id"]); D.bump("attempts")
    owner = asyncio.current_task()
    hb = asyncio.create_task(D.heartbeat(rid, run.get("attempt") or 0, WID, owner))   # created first: system context, not the run user's
    D.CUR.set(D.context_for(run))
    try:
        try:
            async with asyncio.timeout(D.job_timeout()) as cm:
                await _execute(run)
        except TimeoutError:
            if not cm.expired(): raise
            _adm.USER.set(None); await D.timed_out(run); return
        except asyncio.CancelledError:
            if not DRAINING: D.bump("cancel")
            raise
        except Exception as e:  # noqa: BLE001   (before / around _execute_inner: it handles its own errors)
            _adm.USER.set(None); print("run task", rid[:8], type(e).__name__, str(e)[:200])
            r = await A.q("UPDATE lg_runs SET status='error', error=%s, failure_kind='run_error', ended_at=now() WHERE run_id=%s AND status='running' RETURNING thread_id", (f"{type(e).__name__}: {str(e)[:400]}", rid), one=True)
            if r: await A.q("UPDATE lg_threads SET status='error' WHERE thread_id=%s", (r["thread_id"],)); await _emit(rid, "error", {"error": type(e).__name__, "message": str(e)[:400]}); await _emit(rid, "end", None)
            return
        _adm.USER.set(None)
        st = await A.q("SELECT status FROM lg_runs WHERE run_id=%s", (rid,), one=True)
        if st and st["status"] == "success": D.bump("success")
    finally:
        hb.cancel()

async def shutdown(grace=None):
    """GĐ13.30 graceful stop (SIGTERM of the worker / API process): stop claiming, let in-flight runs finish for `grace` seconds (default AOS_BG_SHUTDOWN_GRACE_SECS = 120),
    cancel what is left and release every run this process still holds back to `pending` (not lost, not failed): the next claim resumes it from its checkpoint."""
    global DRAINING
    import durable as D
    DRAINING = True
    if TASKS: await asyncio.wait(list(TASKS.values()), timeout=D.shutdown_grace() if grace is None else grace)
    left = [t for t in TASKS.values() if not t.done()]
    for t in left: t.cancel()
    if left: await asyncio.wait(left, timeout=10)
    rows = await D.release(WID)
    if rows: print(f"shutdown: {len(rows)} run(s) released to pending", flush=True)
    return rows

async def queue_worker():
    """Hàng đợi Postgres: lấy run 'pending' (mỗi thread chỉ 1 run chạy, FIFO), tối đa MAX_CONC song song."""
    n = 0
    while True:
        try:
            try: await asyncio.wait_for(KICK.wait(), 2 if WID == "api" else 1)
            except asyncio.TimeoutError: pass
            KICK.clear(); n += 1
            for rid in [k for k, t in TASKS.items() if t.done()]: TASKS.pop(rid, None); SEQ.pop(rid, None); ATT.pop(rid, None)
            if TASKS:   # cancelled from another process → stop our task too (GĐ13.30: also when the row was re-queued / claimed by someone else: one run never executes twice)
                for r in await A.q("SELECT run_id, status, attempt, worker FROM lg_runs WHERE run_id::text = ANY(%s)", (list(TASKS),)):
                    t = TASKS.get(str(r["run_id"]))
                    if t and not t.done() and (r["status"] != "running" or r["worker"] != WID or r["attempt"] != ATT.get(str(r["run_id"]), r["attempt"])): t.cancel()
            free = MAX_CONC - len(TASKS)
            if free > 0 and not DRAINING:
                rows = await A.q("""UPDATE lg_runs SET status='running', started_at=now(), heartbeat_at=now(), worker=%s WHERE run_id IN (
                    SELECT r.run_id FROM lg_runs r WHERE r.status='pending' AND r.run_at <= now()
                      AND (%s <> 'api' OR r.created_at < now() - interval '3 seconds'
                           OR NOT EXISTS (SELECT 1 FROM worker_heartbeats h WHERE h.name <> 'api' AND h.at > now() - interval '20 seconds'))
                      AND NOT EXISTS (SELECT 1 FROM lg_runs x WHERE x.thread_id=r.thread_id AND x.status='running')
                      AND r.created_at = (SELECT min(y.created_at) FROM lg_runs y WHERE y.thread_id=r.thread_id AND y.status='pending')
                    ORDER BY r.created_at FOR UPDATE SKIP LOCKED LIMIT %s) RETURNING *""", (WID, WID, free))
                for r in rows:
                    await A.q("UPDATE lg_threads SET status='busy' WHERE thread_id=%s", (r["thread_id"],))
                    ATT[str(r["run_id"])] = r["attempt"]; TASKS[str(r["run_id"])] = asyncio.create_task(_run_task(r))
                if rows: await A.q("UPDATE worker_heartbeats SET claimed_total=claimed_total+%s WHERE name=%s", (len(rows), WID))
            await A.q("UPDATE worker_heartbeats SET at=now(), running=%s, pid=%s WHERE name=%s", (len(TASKS), __import__("os").getpid(), WID))
            if n % 15 == 0 and WID == "api": await _crons(); await _ttl()   # schedulers run once, in the API process
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            print("queue worker", e); await asyncio.sleep(2)

async def _ttl():
    try: import durable; await durable.sweep()   # GĐ13.30: runs whose worker died (no heartbeat) -> retry from the checkpoint / failure_after_retry
    except Exception as e: print("durable sweep", type(e).__name__, str(e)[:200])  # noqa: BLE001
    for t in await A.q("SELECT * FROM lg_threads WHERE ttl_expires IS NOT NULL AND ttl_expires < now()"): await _delete_thread(t)
    import retention; await retention.sweep_all()   # GĐ13.25: run events (14 days / AOS_RESUMABLE_STREAM_TTL_SECONDS / per workspace) + opt-in checkpoint sweep

async def _crons():
    import evals
    if evals.schedules_disabled(): return
    now = dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
    for c in await A.q("SELECT * FROM lg_crons WHERE enabled"):
        try:
            if not evals.is_due(c["schedule"], c.get("timezone"), c.get("run_once_at"), now, c["last_run"], c.get("end_time")): continue
        except ValueError: continue
        await A.q("UPDATE lg_crons SET last_run=%s" + (", enabled=false" if c.get("run_once_at") else "") + " WHERE cron_id=%s", (now, c["cron_id"]))
        tid = c["thread_id"] or f"cron-{str(c['cron_id'])[:8]}-{uuid.uuid4().hex[:6]}"
        rid = str(uuid.uuid4())
        if not await A.q("SELECT 1 FROM lg_threads WHERE thread_id=%s", (tid,), one=True):
            await A.q("INSERT INTO lg_threads(thread_id,workspace_id,metadata) VALUES(%s,%s,%s)", (tid, c["workspace_id"], Jsonb({"cron_id": str(c["cron_id"])})))
        a = await A.q("SELECT * FROM lg_assistants WHERE assistant_id=%s", (c["assistant_id"],), one=True)
        await A.q("UPDATE lg_threads SET agent_id=%s, assistant_id=%s WHERE thread_id=%s AND agent_id IS NULL", (a["agent_id"], a["assistant_id"], tid))
        tmp = c.get("on_run_completed") == "delete" and not c["thread_id"]   # GĐ13.27: the cron's own thread is removed after the run (trace stays)
        await A.q("INSERT INTO lg_runs(run_id,thread_id,assistant_id,input,webhook,metadata,workspace_id,temporary) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                  (rid, tid, c["assistant_id"], _sr(rid, "input", Jsonb(c["input"]), c["workspace_id"]), c["webhook"], Jsonb({"cron_id": str(c["cron_id"])}), c["workspace_id"], tmp))
        KICK.set()

async def _cron_run_now(cid):
    await A.q("UPDATE lg_crons SET last_run=NULL WHERE cron_id=%s", (cid,))

# ---- endpoints runs (thread)
@router.post("/threads/{tid}/runs")
async def run_bg(tid: str, b: RunIn): return await _create_run(tid, b)

@router.post("/threads/{tid}/runs/wait")
async def run_wait(tid: str, b: RunIn):
    r = await _create_run(tid, b); return await _join(str(r["run_id"]))

@router.post("/threads/{tid}/runs/stream")
async def run_stream(tid: str, b: RunIn):
    r = await _create_run(tid, b)
    return StreamingResponse(_sse_guard(str(r["run_id"]), b.on_disconnect == "cancel"), media_type="text/event-stream",
                             headers={"Content-Location": f"/api/v1/threads/{tid}/runs/{r['run_id']}"})

async def _sse_guard(rid, cancel_on_disconnect):
    """G11: when the client goes away before `end` (Stop button / tab closed) and asked on_disconnect="cancel", cancel the run."""
    finished = False
    try:
        async for chunk in _sse(rid):
            yield chunk
        finished = True
    finally:
        if cancel_on_disconnect and not finished:
            asyncio.get_running_loop().create_task(_cancel(rid, "interrupt"))   # shielded from the request's own cancellation

@router.get("/threads/{tid}/runs")
async def run_list(tid: str, limit: int = 10, offset: int = 0, status: str | None = None):
    await _get_thread(tid)
    return await A.q("SELECT * FROM lg_runs WHERE thread_id=%s AND (%s::text IS NULL OR status=%s) ORDER BY created_at DESC LIMIT %s OFFSET %s", (tid, status, status, min(limit, 1000), offset))

@router.get("/threads/{tid}/runs/{rid}")
async def run_get(tid: str, rid: str):
    await _get_thread(tid); return await _run_row(rid)

@router.post("/threads/{tid}/runs/{rid}/cancel")
async def run_cancel(tid: str, rid: str, action: str = "interrupt", wait: bool = False):
    await _get_thread(tid)
    if action not in ("interrupt", "rollback"): raise HTTPException(422, "action = interrupt | rollback")
    ok = await _cancel(rid, action); return {"ok": ok}

@router.delete("/threads/{tid}/runs/{rid}")
async def run_delete(tid: str, rid: str):
    await _get_thread(tid); r = await _run_row(rid)
    if r["status"] in ("pending", "running"): raise HTTPException(409, "Run đang chạy — huỷ trước")
    await A.q("DELETE FROM lg_run_events WHERE run_id=%s", (rid,)); await A.q("DELETE FROM lg_runs WHERE run_id=%s", (rid,)); return {"ok": True}

@router.get("/threads/{tid}/runs/{rid}/join")
async def run_join(tid: str, rid: str):
    await _get_thread(tid); return await _join(rid)

def _mode_filter(modes):
    """GĐ9.6 join_stream(stream_mode=...): the SDK names -> the stored event names (`messages-tuple` is stored as `messages`); metadata / error / end always pass."""
    modes = [m for m in modes or [] if m and m != "None"]   # the SDK sends an empty `stream_mode=` when none was given
    if not modes: return None
    want = {"messages" if m == "messages-tuple" else m for m in modes}
    if "messages" in want: want.add("messages/partial")
    return want

@router.get("/threads/{tid}/runs/{rid}/stream")
async def run_join_stream(tid: str, rid: str, request: Request, stream_mode: list[str] = Query(default=[]), cancel_on_disconnect: bool = False):
    await _get_thread(tid); await _run_row(rid)
    last = request.headers.get("last-event-id")   # resumable: replay every stored event after this id (SDK runs.join_stream(last_event_id=...))
    gen = _sse(rid, int(last) if last and last.isdigit() else 0, _mode_filter(stream_mode))
    return StreamingResponse(_guard_gen(gen, rid, cancel_on_disconnect), media_type="text/event-stream")

async def _guard_gen(gen, rid, cancel_on_disconnect):
    finished = False
    try:
        async for chunk in gen: yield chunk
        finished = True
    finally:
        if cancel_on_disconnect and not finished: asyncio.get_running_loop().create_task(_cancel(rid, "interrupt"))

@router.get("/threads/{tid}/stream")
async def thread_stream(tid: str, request: Request, stream_mode: list[str] = Query(default=["run_modes"])):
    """GĐ9.6 SDK threads.join_stream: events of the thread's active (else latest) run. run_modes = every stored event; lifecycle = run start/end only;
    state_update = the `values` snapshots. Last-Event-ID resumes after that event id."""
    await _get_thread(tid)
    r = await A.q("SELECT run_id FROM lg_runs WHERE thread_id=%s ORDER BY (status IN ('pending','running')) DESC, created_at DESC LIMIT 1", (tid,), one=True)
    if not r: raise HTTPException(404, "Thread chưa có run nào / no run on this thread yet")
    last = request.headers.get("last-event-id")
    stream_mode = [m for m in stream_mode if m] or ["run_modes"]
    flt = None if "run_modes" in stream_mode else ({"values"} if "state_update" in stream_mode and "lifecycle" not in stream_mode else set())
    return StreamingResponse(_sse(str(r["run_id"]), int(last) if last and last.isdigit() else 0, flt), media_type="text/event-stream")

async def _join(rid, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = await A.q("SELECT status, thread_id FROM lg_runs WHERE run_id=%s", (rid,), one=True)
        if not r: raise HTTPException(404, "run đã bị xoá (rollback?)")
        if r["status"] in TERMINAL:
            v = await A.q("SELECT data FROM lg_run_events WHERE run_id=%s AND event='values' ORDER BY seq DESC LIMIT 1", (rid,), one=True)
            if v: return v["data"]
            t = await A.q("SELECT * FROM lg_threads WHERE thread_id=%s", (r["thread_id"],), one=True)
            return (await _state(t))["values"] if t else {"status": r["status"]}
        await asyncio.sleep(0.5)
    raise HTTPException(504, "Hết thời gian chờ")

async def _sse(rid, after=0, only=None):
    seq = after; idle = 0
    while True:
        evs = await A.q("SELECT seq, event, data FROM lg_run_events WHERE run_id=%s AND seq > %s ORDER BY seq", (rid, seq))
        for e in evs:
            seq = e["seq"]
            if only is not None and e["event"].split("|")[0] not in only and e["event"] not in ("metadata", "error", "end"): continue
            yield f"id: {seq}\nevent: {e['event']}\ndata: {json.dumps(e['data'], ensure_ascii=False, default=str)}\n\n"
            if e["event"] == "end": return
        if not evs:
            idle += 1
            r = await A.q("SELECT status FROM lg_runs WHERE run_id=%s", (rid,), one=True)
            if not r or (r["status"] in TERMINAL and idle > 6):
                yield "event: end\ndata: null\n\n"; return
            if idle % 30 == 0: yield ": keep-alive\n\n"
        await asyncio.sleep(0.3)

# ---- stateless runs
@router.post("/runs")
async def sl_bg(b: RunIn): return await _create_run("tmp-" + uuid.uuid4().hex, b, temporary=True)

@router.post("/runs/wait")
async def sl_wait(b: RunIn):
    r = await _create_run("tmp-" + uuid.uuid4().hex, b, temporary=True); return await _join(str(r["run_id"]))

@router.post("/runs/stream")
async def sl_stream(b: RunIn):
    r = await _create_run("tmp-" + uuid.uuid4().hex, b, temporary=True)
    return StreamingResponse(_sse_guard(str(r["run_id"]), b.on_disconnect == "cancel"), media_type="text/event-stream")

@router.post("/runs/batch")
async def sl_batch(items: list[RunIn]):
    if len(items) > 20: raise HTTPException(422, "tối đa 20")
    return [await _create_run("tmp-" + uuid.uuid4().hex, b, temporary=True) for b in items]

# ---- crons
class CronIn(BaseModel):
    assistant_id: str
    schedule: str
    input: dict
    webhook: str | None = None
    metadata: dict = {}
    timezone: str | None = None
    run_at: str | None = None
    end_time: str | None = None                  # GĐ13.27: ISO datetime; never due at/after it
    enabled: bool = True
    on_run_completed: str = "keep"               # delete | keep
    config: dict | None = None                   # stored; used by 13.30 (durability)
    context: dict | None = None
    durability: str | None = None
    user_id: str | None = None

def _cron_end(v):
    import evals
    try: return evals.norm_end_time(v)
    except ValueError as e: raise HTTPException(422, str(e))  # noqa: B904

def _cron_out(c):
    """GĐ13.27: a cron row + `next_run_date` (ISO, UTC; None when finished or disabled)."""
    import evals
    if not c: return c
    try: n = evals.next_run_date(c["schedule"], c.get("timezone"), c.get("run_once_at"), dt.datetime.now(dt.timezone.utc), c.get("last_run"), c.get("end_time")) if c.get("enabled") else None
    except ValueError: n = None
    return {**c, "next_run_date": n.isoformat() if n else None}

async def _cron_create(b: CronIn, tid=None):
    import evals
    try: sched, tz, ro = evals.norm_schedule(b.schedule, b.timezone, b.run_at)
    except Exception as e: raise HTTPException(422, f"schedule không hợp lệ (cron 5 trường / timezone / run_at): {e}")  # noqa: B904,BLE001
    if b.on_run_completed not in ("delete", "keep"): raise HTTPException(422, "on_run_completed phải là delete hoặc keep")
    end = _cron_end(b.end_time)
    a = await _get_assistant(b.assistant_id); cid = str(uuid.uuid4())
    return _cron_out(await A.q("""INSERT INTO lg_crons(cron_id,assistant_id,thread_id,schedule,input,webhook,metadata,workspace_id,timezone,run_once_at,end_time,enabled,on_run_completed,config,context,durability,user_id)
                     VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                     (cid, a["assistant_id"], tid, sched, Jsonb(b.input), b.webhook, Jsonb(b.metadata), _ws(), tz, ro, end, b.enabled, b.on_run_completed,
                      Jsonb(b.config) if b.config is not None else None, Jsonb(b.context) if b.context is not None else None, b.durability, b.user_id), one=True))

@router.post("/runs/crons")
async def cron_new(b: CronIn): return await _cron_create(b)

@router.post("/threads/{tid}/runs/crons")
async def cron_new_thread(tid: str, b: CronIn):
    await _get_thread(tid); return await _cron_create(b, tid)

_CRON_SORT = {"created_at", "next_run_date", "schedule", "last_run", "end_time", "enabled"}

def _cron_where(b: dict):
    w = ["workspace_id=%s", "(%s::uuid IS NULL OR assistant_id=%s::uuid)", "(%s::text IS NULL OR thread_id=%s)", "metadata @> %s"]
    a = [_ws(), b.get("assistant_id"), b.get("assistant_id"), b.get("thread_id"), b.get("thread_id"), Jsonb(b.get("metadata") or {})]   # GĐ9.Q: metadata filter (auth filters)
    if b.get("enabled") is not None: w.append("enabled=%s"); a.append(bool(b["enabled"]))
    return " AND ".join(w), a

@router.post("/runs/crons/search")
async def cron_search(b: dict = None):
    b = b or {}
    by = b.get("sort_by") or "created_at"
    if by not in _CRON_SORT: raise HTTPException(422, f"sort_by phải là một trong {sorted(_CRON_SORT)}")
    desc = str(b.get("sort_order") or "desc").lower() != "asc"
    where, a = _cron_where(b)
    col = "created_at" if by == "next_run_date" else by
    lim, off = min(int(b.get("limit", 10)), 1000), max(int(b.get("offset", 0)), 0)
    if by == "next_run_date":   # computed field: sort in Python over the (workspace-scoped) matches, then page
        rows = [_cron_out(r) for r in await A.q(f"SELECT * FROM lg_crons WHERE {where}", a)]
        rows.sort(key=lambda r: r["next_run_date"] or "", reverse=desc)
        none = [r for r in rows if not r["next_run_date"]]; rows = [r for r in rows if r["next_run_date"]] + none   # finished ones last
        return rows[off:off + lim]
    rows = await A.q(f"SELECT * FROM lg_crons WHERE {where} ORDER BY {col} {'DESC' if desc else 'ASC'}, cron_id LIMIT %s OFFSET %s", (*a, lim, off))
    return [_cron_out(r) for r in rows]

@router.post("/runs/crons/count")
async def cron_count(b: dict = None):
    where, a = _cron_where(b or {})
    return (await A.q(f"SELECT count(*) n FROM lg_crons WHERE {where}", a, one=True))["n"]

def _is_uuid(v):
    try: uuid.UUID(str(v)); return True
    except ValueError: return False

@router.get("/runs/crons/{cid}")
async def cron_get(cid: str):
    c = await A.q("SELECT * FROM lg_crons WHERE cron_id=%s AND workspace_id=%s", (cid, _ws()), one=True) if _is_uuid(cid) else None
    if not c: raise HTTPException(404, "Không có cron")
    return _cron_out(c)

@router.patch("/runs/crons/{cid}")
async def cron_patch(cid: str, b: dict):
    import evals
    c = await A.q("SELECT * FROM lg_crons WHERE cron_id=%s AND workspace_id=%s", (cid, _ws()), one=True) if _is_uuid(cid) else None
    if not c: raise HTTPException(404, "Không có cron")
    sets, args = [], []
    k = "is_enabled" if "is_enabled" in b else "enabled"
    if k in b: sets.append("enabled=%s"); args.append(bool(b[k]))
    if any(x in b for x in ("schedule", "timezone", "run_at")):
        once = c.get("run_once_at") and "schedule" not in b
        try: sched, tz, ro = evals.norm_schedule(b.get("schedule") or ("@once" if once else c["schedule"]), b.get("timezone") or c.get("timezone"),
                                                  b.get("run_at") or (c.get("run_once_at") if once else None))
        except Exception as e: raise HTTPException(422, f"schedule không hợp lệ (cron 5 trường / timezone / run_at): {e}")  # noqa: B904,BLE001
        sets += ["schedule=%s", "timezone=%s", "run_once_at=%s"]; args += [sched, tz, ro]
        if b.get("run_at"): sets.append("last_run=NULL")   # a new one-shot time can fire again
    if "end_time" in b: sets.append("end_time=%s"); args.append(_cron_end(b["end_time"]))
    if "on_run_completed" in b:
        if b["on_run_completed"] not in ("delete", "keep"): raise HTTPException(422, "on_run_completed phải là delete hoặc keep")
        sets.append("on_run_completed=%s"); args.append(b["on_run_completed"])
    if "input" in b: sets.append("input=%s"); args.append(Jsonb(b["input"]))
    if "metadata" in b: sets.append("metadata=%s"); args.append(Jsonb(b["metadata"] or {}))
    for f in ("config", "context"):
        if f in b: sets.append(f"{f}=%s"); args.append(Jsonb(b[f]) if b[f] is not None else None)
    for f in ("webhook", "durability", "user_id"):
        if f in b: sets.append(f"{f}=%s"); args.append(b[f])
    if sets: await A.q(f"UPDATE lg_crons SET {', '.join(sets)} WHERE cron_id=%s AND workspace_id=%s", (*args, cid, _ws()))
    if b.get("run_now"): await _cron_run_now(cid)
    return _cron_out(await A.q("SELECT * FROM lg_crons WHERE cron_id=%s AND workspace_id=%s", (cid, _ws()), one=True))

@router.delete("/runs/crons/{cid}")
async def cron_del(cid: str):
    await A.q("DELETE FROM lg_crons WHERE cron_id=%s AND workspace_id=%s", (cid, _ws())); return {"ok": True}

@router.get("/info")
async def info():
    q = await A.q("SELECT status, count(*) n FROM lg_runs GROUP BY status")
    return {"version": "agentos-1", "flags": {"assistants": True, "crons": True, "store": True}, "queue": {r["status"]: r["n"] for r in q},
            "workers": {"max": MAX_CONC, "active": len([t for t in TASKS.values() if not t.done()])}}

@router.get("/ok")
async def ok(): return {"ok": True}


# ================================================================== GĐ6.6 /store chuẩn LangGraph (langgraph-sdk 0.4.8 StoreClient)
# Dùng chung AsyncPostgresStore (có index embedding) với /api/store/* cũ. Namespace tự gắn tiền tố rồi bỏ khi trả về:
#   editor/viewer -> ("ws", ws, "user", uid)   admin -> ("ws", ws) (thấy cả ("user", uid, ...) của mọi người trong workspace)
# Nhãn do client gửi chỉ nối SAU tiền tố nên không thể thoát ra ngoài; nhãn rỗng/không phải chuỗi bị từ chối.
_adm_boot.allow_viewer_post("/api/v1/store/items/search", "/api/v1/store/namespaces")   # POST chỉ đọc

def _store_prefix() -> tuple:
    import admin; u = admin.cur() or {}
    ws = str(u.get("ws") or 1)
    return ("ws", ws) if u.get("role") == "admin" and not u.get("demo") else ("ws", ws, "user", str(u.get("id") or 0))

def _labels(ns, allow_empty=False) -> tuple:
    if not isinstance(ns, (list, tuple)) or not all(isinstance(x, str) and x and "." not in x for x in ns) or not (ns or allow_empty):
        raise HTTPException(422, "namespace phải là danh sách nhãn chuỗi, không rỗng, không chứa dấu chấm")
    return tuple(ns)

def _item(i, pre: int) -> dict:
    d = {"namespace": list(i.namespace[pre:]), "key": i.key, "value": i.value, "created_at": i.created_at.isoformat(), "updated_at": i.updated_at.isoformat()}
    if getattr(i, "score", None) is not None: d["score"] = i.score
    return d

class StorePutIn(BaseModel):
    namespace: list
    key: str = Field(min_length=1, max_length=512)
    value: dict
    index: bool | list[str] | None = None   # False = không lập chỉ mục; list = các trường cần nhúng
    ttl: float | None = Field(default=None, gt=0)

@router.put("/store/items", status_code=204)
async def store_put(b: StorePutIn):
    pre = _store_prefix(); kw = {}
    if b.index is True: raise HTTPException(422, "index: false hoặc danh sách trường")
    if b.index is not None: kw["index"] = b.index
    if b.ttl is not None: kw["ttl"] = b.ttl
    await A.S["store"].aput(pre + _labels(b.namespace), b.key, b.value, **kw)

@router.get("/store/items")
async def store_get(key: str, namespace: str = ""):
    pre = _store_prefix()
    i = await A.S["store"].aget(pre + _labels(namespace.split(".") if namespace else []), key)
    if not i: raise HTTPException(404, "Không có mục này")
    return _item(i, len(pre))

class StoreDelIn(BaseModel):
    namespace: list
    key: str

@router.delete("/store/items", status_code=204)
async def store_delete(b: StoreDelIn):
    await A.S["store"].adelete(_store_prefix() + _labels(b.namespace), b.key)

class StoreSearchIn(BaseModel):
    namespace_prefix: list | None = None
    filter: dict | None = None
    limit: int = Field(default=10, ge=1, le=200)
    offset: int = Field(default=0, ge=0)
    query: str | None = None
    refresh_ttl: bool | None = None

@router.post("/store/items/search")
async def store_search(b: StoreSearchIn):
    pre = _store_prefix(); st = A.S["store"]
    kw = dict(filter=b.filter, limit=b.limit, offset=b.offset, **({"refresh_ttl": b.refresh_ttl} if b.refresh_ttl is not None else {}))
    p = pre + _labels(b.namespace_prefix or [], allow_empty=True)
    try:
        res = await st.asearch(p, query=b.query or None, **kw)
    except HTTPException: raise
    except Exception:  # noqa: BLE001   # nhúng lỗi (hết hạn mức embedding...) -> lùi về tìm theo bộ lọc, không làm hỏng yêu cầu
        if not b.query: raise
        res = await st.asearch(p, query=None, **kw)
    return {"items": [_item(i, len(pre)) for i in res]}

class StoreNsIn(BaseModel):
    prefix: list | None = None
    suffix: list | None = None
    max_depth: int | None = Field(default=None, ge=1)
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)

@router.post("/store/namespaces")
async def store_namespaces(b: StoreNsIn):
    pre = _store_prefix(); n = len(pre)
    res = await A.S["store"].alist_namespaces(prefix=pre + _labels(b.prefix or [], allow_empty=True), suffix=_labels(b.suffix) if b.suffix else None,
                                              max_depth=b.max_depth + n if b.max_depth else None, limit=b.limit, offset=b.offset)
    return {"namespaces": [list(x[n:]) for x in res if len(x) > n]}
