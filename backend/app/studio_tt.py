"""GĐ9.5 Studio time travel: re-run from any checkpoint, edit state then run, branch tree + compare, attachments, long-term memory editor.
Design (LangGraph-native, no thread copies): every Studio/Agent-Server thread already lives under one checkpoint key `a<agent>:<thread>`.
A checkpoint config {thread_id, checkpoint_id} + `update_state` / a run with that checkpoint_id makes LangGraph write NEW checkpoints whose
`parent` is the chosen one -- the old branch is never touched. The "branches" are just the leaves of that checkpoint tree (computed from
`aget_state_history`). Same semantics as LangGraph Studio / LangSmith, and the same calls the Agent Server API exposes (server.py), so
langgraph-sdk clients can fork too. Encrypted checkpoints (9.7) are read through the same checkpointer (transparent).
  GET  /api/studio/{aid}/threads/{tid}/timeline          checkpoints (node, step, time, diff vs parent) + branches (leaves) + head
  GET  /api/studio/{aid}/threads/{tid}/compare?a=&b=     two branches: final answers + message/state diff
  POST /api/studio/{aid}/threads/{tid}/validate          dry-run validation of an edited state
  POST /api/studio/{aid}/threads/{tid}/fork              [update_state at checkpoint] then run -> new branch
  POST /api/studio/{aid}/threads/{tid}/activate          make a branch the thread's current one (what /chat and the API continue)
  GET/PUT/DELETE /api/studio/{aid}/memory...             long-term memories of the agent (editor/admin only, audited)"""
import json, re
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
import deepagent_api as A
import admin as _adm

router = APIRouter(tags=["studio-time-travel"])
MAX_STATE_BYTES = 256 * 1024
MAX_HISTORY = 500
MSG_TYPES = {"human", "user", "ai", "assistant", "system", "tool", "remove"}

# ------------------------------------------------------------------ helpers
def _need_editor():
    u = _adm.cur() or {}
    if _adm.is_demo(u) or _adm.ROLES.get(u.get("role"), 0) < _adm.ROLES["editor"]: raise HTTPException(403, "Only admins and editors can use this")
    return u

async def _own_agent(aid: int):
    if not await A.q("SELECT 1 FROM agents WHERE id=%s AND NOT deleted", (aid,), one=True): raise HTTPException(404, "Agent not found")

async def _graph(aid: int):
    await _own_agent(aid)
    ag, cfg = await A.build_agent(aid, await A.cur_version(aid)); return ag

def _cfg(aid, tid, cp=None):
    c = {"configurable": {"thread_id": f"a{aid}:{tid}"}}
    if cp: c["configurable"]["checkpoint_id"] = cp
    return c

async def _audit(u, detail, path):
    try:
        await A.q("INSERT INTO audit_log(user_id,username,workspace_id,auth,method,path,status,ms,detail) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                  (u.get("id"), u.get("username"), u.get("ws"), u.get("auth"), "STUDIO", path[:300], 200, 0, detail[:500]))
    except Exception as e:  # noqa: BLE001
        print("studio audit error", e)

async def _snap(ag, aid, tid, cp):
    """State snapshot at a checkpoint of THIS thread; 404 when it does not exist (aget_state would otherwise return an empty snapshot for any id)."""
    try: s = await ag.aget_state(_cfg(aid, tid, cp))
    except Exception: s = None  # noqa: BLE001
    if not s or s.created_at is None or s.config["configurable"].get("checkpoint_id") != cp: raise HTTPException(404, "Checkpoint not found")
    return s

def _mtext(m) -> str:
    c = getattr(m, "content", m.get("content") if isinstance(m, dict) else "")
    return A._text(c) if not isinstance(c, str) else c

def _mid(m): return getattr(m, "id", None) or (m.get("id") if isinstance(m, dict) else None)
def _mtype(m): return getattr(m, "type", None) or (m.get("type") if isinstance(m, dict) else "")

def _short(m, n=160):
    d = {"id": _mid(m), "type": _mtype(m), "text": _mtext(m)[:n]}
    if getattr(m, "tool_calls", None): d["tool_calls"] = [t.get("name") for t in m.tool_calls]
    return d

def _answer(values) -> str:
    for m in reversed((values or {}).get("messages") or []):
        if _mtype(m) == "ai" and not getattr(m, "tool_calls", None): return _mtext(m)
    return ""

def _diff(prev: dict | None, cur: dict) -> dict:
    """State diff of one checkpoint against its parent: messages added / changed / removed (by id) + other state keys that changed."""
    pm = {(_mid(m) or i): m for i, m in enumerate((prev or {}).get("messages") or [])}
    cm = {(_mid(m) or i): m for i, m in enumerate((cur or {}).get("messages") or [])}
    added = [_short(m) for k, m in cm.items() if k not in pm]
    changed = [{**_short(m), "before": _mtext(pm[k])[:160]} for k, m in cm.items() if k in pm and (_mtext(pm[k]) != _mtext(m) or bool(getattr(pm[k], "tool_calls", None)) != bool(getattr(m, "tool_calls", None)))]
    removed = [_short(m) for k, m in pm.items() if k not in cm]
    keys = sorted(k for k in set(prev or {}) | set(cur or {}) if k != "messages" and json.dumps((prev or {}).get(k), default=str, sort_keys=True) != json.dumps((cur or {}).get(k), default=str, sort_keys=True))
    return {"added": added, "changed": changed, "removed": removed, "keys": keys}

def _jsonable(v):
    from langchain_core.messages import BaseMessage
    if isinstance(v, BaseMessage): return {"id": v.id, "type": v.type, "content": v.content, **({"tool_calls": v.tool_calls} if getattr(v, "tool_calls", None) else {}),
                                           **({"name": v.name} if getattr(v, "name", None) else {}), **({"tool_call_id": v.tool_call_id} if getattr(v, "tool_call_id", None) else {})}
    if isinstance(v, dict): return {k: _jsonable(x) for k, x in v.items() if k not in ("files", "skills_metadata")}
    if isinstance(v, (list, tuple)): return [_jsonable(x) for x in v]
    return v if isinstance(v, (str, int, float, bool, type(None))) else str(v)

# ------------------------------------------------------------------ timeline + branches
@router.get("/api/studio/{aid}/threads/{tid}/timeline")
async def timeline(aid: int, tid: str, limit: int = 300):
    ag = await _graph(aid)
    snaps = [s async for s in ag.aget_state_history(_cfg(aid, tid), limit=max(1, min(limit, MAX_HISTORY)))]
    by = {s.config["configurable"]["checkpoint_id"]: s for s in snaps}
    kids: dict = {}
    for cid, s in by.items():
        p = (s.parent_config or {}).get("configurable", {}).get("checkpoint_id"); kids.setdefault(p if p in by else None, []).append(cid)
    cps = []
    for cid, s in sorted(by.items(), key=lambda x: (x[1].created_at or "", x[0])):
        p = (s.parent_config or {}).get("configurable", {}).get("checkpoint_id"); ps = by.get(p)
        md = s.metadata or {}; src = md.get("source")
        node = ", ".join((md.get("writes") or {}).keys()) if isinstance(md.get("writes"), dict) and md.get("writes") else (", ".join(ps.next) if ps and src == "loop" else "")
        if not node: node = "__input__" if src == "input" else ("update" if src == "update" else (src or ""))
        vals = s.values or {}
        cps.append({"checkpoint_id": cid, "parent": p if p in by else None, "step": md.get("step"), "source": src, "node": node, "next": list(s.next), "created": s.created_at,
                    "n_messages": len(vals.get("messages") or []), "diff": _diff(ps.values if ps else None, vals), "children": len(kids.get(cid, [])),
                    "interrupts": [str(getattr(i, "value", i))[:200] for i in (s.interrupts or [])]})
    leaves = [c for c in cps if c["children"] == 0]
    try: head = (await ag.aget_state(_cfg(aid, tid))).config["configurable"].get("checkpoint_id")
    except Exception: head = None  # noqa: BLE001
    branches = []
    for lf in leaves:
        path, cur = [], lf["checkpoint_id"]
        while cur:
            path.append(cur); cur = next((c["parent"] for c in cps if c["checkpoint_id"] == cur), None)
        path.reverse()
        branches.append({"id": lf["checkpoint_id"], "path": path, "steps": len(path), "created": lf["created"], "active": lf["checkpoint_id"] == head,
                         "answer": _answer(by[lf["checkpoint_id"]].values)[:300], "next": lf["next"], "n_messages": lf["n_messages"]})
    fork_pts = {c["checkpoint_id"] for c in cps if c["children"] > 1}
    for b in branches:
        b["fork_from"] = next((x for x in reversed(b["path"][:-1]) if x in fork_pts), None)
        i0 = b["path"].index(b["fork_from"]) + 1 if b["fork_from"] else 0
        b["start"] = by[b["path"][i0]].created_at or ""          # first checkpoint that belongs to this branch: stable ordering/label even after the branch grows
    for i, b in enumerate(sorted(branches, key=lambda x: (x["start"], x["id"])), 1): b["label"] = i
    branches.sort(key=lambda x: x["label"]); seen: set = set()
    for b in branches:      # fork_from = where this branch left the branches created before it (None for the first one)
        b["fork_from"] = next((x for x in reversed(b["path"]) if x in seen), None); seen |= set(b["path"])
    return {"head": head, "checkpoints": cps, "branches": branches, "nodes": sorted(n for n in ag.nodes if not n.startswith("__")), "truncated": len(snaps) >= limit}

@router.get("/api/studio/{aid}/threads/{tid}/compare")
async def compare(aid: int, tid: str, a: str, b: str):
    ag = await _graph(aid)
    sa, sb = await _snap(ag, aid, tid, a), await _snap(ag, aid, tid, b)
    ma, mb = (sa.values or {}).get("messages") or [], (sb.values or {}).get("messages") or []
    n = 0
    while n < min(len(ma), len(mb)) and _mid(ma[n]) == _mid(mb[n]) and _mtext(ma[n]) == _mtext(mb[n]): n += 1
    d = _diff({**(sa.values or {}), "messages": ma}, {**(sb.values or {}), "messages": mb})
    return {"a": {"checkpoint_id": a, "answer": _answer(sa.values), "n_messages": len(ma), "next": list(sa.next)},
            "b": {"checkpoint_id": b, "answer": _answer(sb.values), "n_messages": len(mb), "next": list(sb.next)},
            "common_messages": n, "only_a": [_short(m) for m in ma[n:]], "only_b": [_short(m) for m in mb[n:]], "diff": d,
            "same_answer": _answer(sa.values) == _answer(sb.values)}

# ------------------------------------------------------------------ edited state: validation + update_state
class StateEdit(BaseModel):
    checkpoint_id: str
    values: dict
    as_node: str | None = Field(default=None, max_length=80)

async def _validate(ag, aid, tid, e: StateEdit) -> tuple[list, list]:
    errs, warns = [], []
    if not e.values: errs.append("values must be a non-empty JSON object")
    if len(json.dumps(e.values, default=str)) > MAX_STATE_BYTES: errs.append(f"state edit is larger than {MAX_STATE_BYTES // 1024} KB")
    chans = set(getattr(ag, "stream_channels_list", None) or []) | {"messages"}
    for k in e.values:
        if k not in chans: errs.append(f"unknown state key '{k}' (allowed: {', '.join(sorted(chans))})")
    if e.as_node and e.as_node not in ag.nodes: errs.append(f"as_node '{e.as_node}' is not a node of this agent ({', '.join(sorted(n for n in ag.nodes if not n.startswith('__')))})")
    try: snap = await _snap(ag, aid, tid, e.checkpoint_id)
    except HTTPException: errs.append("checkpoint not found in this thread"); return errs, warns
    msgs = e.values.get("messages")
    if "messages" in e.values:
        if not isinstance(msgs, list) or not msgs: errs.append("messages must be a non-empty list"); return errs, warns
        have = {_mid(m) for m in (snap.values or {}).get("messages") or []}
        for i, m in enumerate(msgs):
            if isinstance(m, str): continue
            if not isinstance(m, dict): errs.append(f"messages[{i}] must be an object or a string"); continue
            t = m.get("type") or m.get("role")
            if t not in MSG_TYPES: errs.append(f"messages[{i}].type must be one of human, ai, system, tool, remove"); continue
            if t == "remove":
                if m.get("id") not in have: errs.append(f"messages[{i}]: remove needs the id of an existing message")
                continue
            c = m.get("content", "")
            if not isinstance(c, (str, list)): errs.append(f"messages[{i}].content must be a string or a list of content blocks")
            if t == "tool" and not m.get("tool_call_id"): errs.append(f"messages[{i}]: a tool message needs tool_call_id")
            if t in ("ai", "assistant") and m.get("tool_calls") is not None:
                tc = m["tool_calls"]
                if not isinstance(tc, list) or any(not isinstance(x, dict) or not x.get("name") or not isinstance(x.get("args", {}), dict) for x in tc): errs.append(f"messages[{i}].tool_calls must be a list of {{name, args, id}}")
            if m.get("id") is not None and not isinstance(m["id"], str): errs.append(f"messages[{i}].id must be a string")
            if m.get("id") and m["id"] not in have: warns.append(f"messages[{i}]: id '{m['id']}' does not exist at this checkpoint, it will be appended")
        if not errs:
            try:
                from langchain_core.messages import convert_to_messages
                from langgraph.graph.message import add_messages
                merged = add_messages(list((snap.values or {}).get("messages") or []), convert_to_messages(msgs))
                answered = {x.tool_call_id for x in merged if x.type == "tool"}
                if any(x.type == "ai" and any(t.get("id") not in answered for t in (x.tool_calls or [])) for x in merged[:-1]):
                    warns.append("an AI message has tool_calls without a matching tool result: some models reject such a history")
            except Exception as ex:  # noqa: BLE001
                errs.append(f"messages are not valid LangChain messages: {str(ex)[:200]}")
    return errs, warns

@router.post("/api/studio/{aid}/threads/{tid}/validate")
async def validate(aid: int, tid: str, e: StateEdit):
    _need_editor(); ag = await _graph(aid); errs, warns = await _validate(ag, aid, tid, e)
    return {"ok": not errs, "errors": errs, "warnings": warns}

async def _writer_node(ag, aid, tid, cp):
    """The node that wrote this checkpoint (so the edit keeps the same `next`): parent's next step; __input__ checkpoint -> __start__."""
    s = await _snap(ag, aid, tid, cp); md = s.metadata or {}
    if md.get("source") == "input": return "__start__"
    if s.parent_config:
        ps = await ag.aget_state(s.parent_config)
        n = [x for x in ps.next if x in ag.nodes]
        if n and md.get("source") == "loop": return n[0]
    return None

class ForkIn(BaseModel):
    checkpoint_id: str | None = None          # None = continue the current branch (plain run)
    values: dict | None = None                # update_state at that checkpoint before running
    as_node: str | None = Field(default=None, max_length=80)
    message: str | None = Field(default=None, max_length=8000)
    files: list[str] = Field(default_factory=list, max_length=5)
    step: bool = False

async def _content(message, files, tid):
    """Text + uploaded TXT/CSV/MD/JSON(+pdf/docx/...) -> standard content blocks through the same guard as chat uploads (6.8: limits, sniffing, RLS)."""
    if not files: return message
    import attachments
    return await attachments.normalize_content(([{"type": "text", "text": message}] if message else [{"type": "text", "text": "Use the attached file(s)."}]) + [{"type": "file", "url": f"/api/files/{f}"} for f in files], thread_id=tid)

@router.post("/api/studio/{aid}/threads/{tid}/fork")
async def fork(aid: int, tid: str, r: ForkIn):
    """Re-run from a checkpoint (new branch). values -> update_state first (as_node defaults to the checkpoint's writer) and the run starts from the NEW checkpoint."""
    u = _need_editor(); ag = await _graph(aid); cp = r.checkpoint_id; warns = []
    if cp: await _snap(ag, aid, tid, cp)
    if r.values:
        if not cp: raise HTTPException(422, "checkpoint_id is required to edit state")
        e = StateEdit(checkpoint_id=cp, values=r.values, as_node=r.as_node); errs, warns = await _validate(ag, aid, tid, e)
        if errs: raise HTTPException(422, {"message": "Invalid state edit", "errors": errs})
        nc = await ag.aupdate_state(_cfg(aid, tid, cp), r.values, as_node=r.as_node or await _writer_node(ag, aid, tid, cp))
        cp = nc["configurable"]["checkpoint_id"]
        await _audit(u, f"studio.update_state agent={aid} thread={tid} from={r.checkpoint_id} new={cp} keys={','.join(r.values)}", f"/api/studio/{aid}/threads/{tid}/fork")
    content = await _content(r.message, r.files, tid)
    if cp and not content:
        if not (await _snap(ag, aid, tid, cp)).next: raise HTTPException(422, "This is a final checkpoint (no next step): type a message or edit the state to run it")
    elif not cp and not content: raise HTTPException(422, "A message or a checkpoint is required")
    events = []
    async for ev in A.run_agent(aid, await A.cur_version(aid), tid, content, checkpoint_id=cp, step=r.step, tags=["studio", "fork"] if r.checkpoint_id else ["studio"]): events.append(ev)
    head = (await ag.aget_state(_cfg(aid, tid))).config["configurable"].get("checkpoint_id")
    if r.checkpoint_id: await _audit(u, f"studio.fork agent={aid} thread={tid} from={r.checkpoint_id} head={head}", f"/api/studio/{aid}/threads/{tid}/fork")
    return {"events": events, "done": events[-1] if events else None, "from_checkpoint": cp, "head": head, "warnings": warns}

class ActivateIn(BaseModel):
    checkpoint_id: str

@router.post("/api/studio/{aid}/threads/{tid}/activate")
async def activate(aid: int, tid: str, b: ActivateIn):
    """/chat and the Agent Server API continue the LATEST checkpoint of a thread. Switching branch = write an empty update on the chosen leaf
    (LangGraph-native: a new checkpoint child of that leaf, same state), which becomes the latest one. Nothing else is copied or deleted."""
    u = _need_editor(); ag = await _graph(aid)
    s = await _snap(ag, aid, tid, b.checkpoint_id)
    head = (await ag.aget_state(_cfg(aid, tid))).config["configurable"].get("checkpoint_id")
    if head == b.checkpoint_id: return {"checkpoint_id": head, "changed": False}
    nc = await ag.aupdate_state(s.config, {}, as_node=await _writer_node(ag, aid, tid, b.checkpoint_id) or (list(s.next)[0] if s.next else None))
    await _audit(u, f"studio.activate agent={aid} thread={tid} branch={b.checkpoint_id}", f"/api/studio/{aid}/threads/{tid}/activate")
    return {"checkpoint_id": nc["configurable"]["checkpoint_id"], "changed": True}

@router.get("/api/studio/{aid}/threads/{tid}/raw")
async def raw_state(aid: int, tid: str, checkpoint_id: str):
    """State values at a checkpoint as editable JSON (messages as {id,type,content,...}); the starting text of the edit dialog."""
    ag = await _graph(aid); s = await _snap(ag, aid, tid, checkpoint_id)
    return {"checkpoint_id": checkpoint_id, "next": list(s.next), "values": _jsonable(s.values or {})}

# ------------------------------------------------------------------ long-term memory editor
NS_RX = re.compile(r"^(shared|u\d+|ch:[\w@.\-]{1,80}|p:\d+)$")

def _ns(aid: int, token: str) -> tuple:
    if not NS_RX.fullmatch(token or ""): raise HTTPException(422, "Invalid namespace")
    if token == "shared": return ("agent", str(aid), "memories")
    if token.startswith("u"): return ("agent", str(aid), token, "memories")
    if token.startswith("ch:"): return ("agent", str(aid), "ch", token[3:], "memories")
    return ("project", token[2:], "memories")

async def _check_ns(aid: int, token: str):
    """Namespaces are only reachable through an agent of the caller's workspace (RLS on `agents`); user/project ids must belong to that workspace too."""
    await _own_agent(aid); ws = (_adm.cur() or {}).get("ws")
    if token.startswith("u") and token != "shared" and not await A.q("SELECT 1 FROM memberships WHERE user_id=%s AND workspace_id=%s", (int(token[1:]), ws), one=True): raise HTTPException(404, "User not found in this workspace")
    if token.startswith("p:") and not await A.q("SELECT 1 FROM chat_projects WHERE id=%s", (int(token[2:]),), one=True): raise HTTPException(404, "Project not found")

@router.get("/api/studio/{aid}/memory/namespaces")
async def mem_namespaces(aid: int, thread: str | None = None):
    _need_editor(); await _own_agent(aid)
    rows = await A.q("SELECT prefix, count(*) n FROM store WHERE prefix ~ %s GROUP BY prefix", (rf"^agent\.{int(aid)}\.(u[0-9]+\.|ch\..+\.)?memories$",))
    users = {f"u{x['id']}": x["username"] for x in await A.q("SELECT u.id, u.username FROM users u JOIN memberships m ON m.user_id=u.id AND m.workspace_id=%s", ((_adm.cur() or {}).get("ws"),))}
    out = {}
    for x in rows:
        p = x["prefix"].split(".")
        tok = "shared" if len(p) == 3 else (p[2] if len(p) == 4 else "ch:" + ".".join(p[3:-1]))
        if tok.startswith("u") and tok not in users: continue
        out[tok] = {"ns": tok, "kind": "shared" if tok == "shared" else ("user" if tok.startswith("u") else "channel"), "label": "" if tok == "shared" else users.get(tok, tok[3:] if tok.startswith("ch:") else tok), "count": x["n"]}
    me = f"u{(_adm.cur() or {}).get('id')}"
    for tok in ("shared", me):
        out.setdefault(tok, {"ns": tok, "kind": "shared" if tok == "shared" else "user", "label": "" if tok == "shared" else users.get(tok, tok), "count": 0})
    t = thread and await A.q("SELECT project_id FROM lg_threads WHERE thread_id=%s", (thread,), one=True)
    if t and t.get("project_id"):
        pn = await A.q("SELECT count(*) n FROM store WHERE prefix=%s", (f"project.{int(t['project_id'])}.memories",), one=True)
        out[f"p:{t['project_id']}"] = {"ns": f"p:{t['project_id']}", "kind": "project", "label": str(t["project_id"]), "count": (pn or {}).get("n", 0)}
    return {"items": list(out.values()), "me": me}

@router.get("/api/studio/{aid}/memory")
async def mem_list(aid: int, ns: str):
    _need_editor(); await _check_ns(aid, ns)
    items = await A.S["store"].asearch(_ns(aid, ns), limit=200)
    return {"ns": ns, "items": [{"key": i.key, "text": str((i.value or {}).get("text") or json.dumps(i.value, ensure_ascii=False))[:2000], "created_at": i.created_at, "updated_at": i.updated_at} for i in items]}

class MemIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)

@router.put("/api/studio/{aid}/memory/{key}")
async def mem_put(aid: int, key: str, ns: str, b: MemIn):
    u = _need_editor(); await _check_ns(aid, ns)
    if not re.fullmatch(r"[\w\-]{1,64}", key): raise HTTPException(422, "Invalid memory key")
    if not await A.S["store"].aget(_ns(aid, ns), key): raise HTTPException(404, "Memory not found")
    await A.S["store"].aput(_ns(aid, ns), key, {"text": b.text.strip()})
    await _audit(u, f"studio.memory.edit agent={aid} ns={ns} key={key}", f"/api/studio/{aid}/memory/{key}")
    return {"ok": True}

@router.delete("/api/studio/{aid}/memory/{key}")
async def mem_del(aid: int, key: str, ns: str):
    u = _need_editor(); await _check_ns(aid, ns)
    if not await A.S["store"].aget(_ns(aid, ns), key): raise HTTPException(404, "Memory not found")
    await A.S["store"].adelete(_ns(aid, ns), key)
    await _audit(u, f"studio.memory.delete agent={aid} ns={ns} key={key}", f"/api/studio/{aid}/memory/{key}")
    return {"ok": True}
