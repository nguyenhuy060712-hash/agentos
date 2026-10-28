"""GĐ13.6 shared agent memory, owner side: proposals inbox + view / edit / delete of `/memories/agent/` files (UI: agent page, tab Memory).
  add_proposal()  called by memory_routes.MemoryLayer when a write may not go through (external run / API key / cron): stored as 'pending', max 20 per agent.
  GET  /api/agents/{id}/memory/files | file?path=     PUT / DELETE .../memory/file          GET .../memory/proposals
  POST .../memory/proposals/{pid}/approve | reject    (approve writes the file as the approver; same secret / PII scan as every write; demo accounts: 403)
Who: owner / workspace admin / 'edit' share (agent_sharing.access()["edit"]). The user layer is not managed here (chat memory panel + me/export, me/erase)."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import memory_routes as M

router = APIRouter()
MAX_PENDING = 20
MSG_DEMO = "Tắt trong tài khoản demo / off for demo accounts"

async def _q(*a, **k):   # lazy: unit tests replace it
    import deepagent_api as A
    return await A.q(*a, **k)

async def add_proposal(agent_id, path, content, reason=None) -> bool:
    """Store (or refresh, same path) a pending proposal. False = not stored (too many pending / agent unknown / demo workspace)."""
    import admin, run_trust
    aid = int(agent_id)
    if admin.is_demo(): return False
    old = await _q("SELECT id FROM memory_proposals WHERE agent_id=%s AND path=%s AND status='pending'", (aid, path), one=True)
    if not old:
        n = await _q("SELECT count(*) n FROM memory_proposals WHERE agent_id=%s AND status='pending'", (aid,), one=True)
        if n["n"] >= MAX_PENDING: return False
    u = admin.cur() or {}
    source = "external" if run_trust.is_external() else (str(u.get("auth") or "run").split(":")[0])
    if old:
        await _q("UPDATE memory_proposals SET content=%s, reason=%s, source=%s, created_at=now() WHERE id=%s", (content, reason, source, old["id"]))
        return True
    r = await _q("INSERT INTO memory_proposals(workspace_id, agent_id, path, content, reason, run_id, source) SELECT workspace_id, id, %s, %s, %s, %s, %s FROM agents WHERE id=%s RETURNING id",
                 (path, content, reason, run_trust.thread_key(), source, aid), one=True)
    return bool(r)

async def _guard(aid: int):
    import admin, agent_sharing
    u = admin.cur() or {}
    if u.get("demo") or admin.is_demo(u): raise HTTPException(403, MSG_DEMO)
    x = await agent_sharing.access(aid)
    if not x["edit"]: raise HTTPException(403, "Chỉ chủ agent / người được quyền sửa mới quản lý bộ nhớ chung / only the owner or an editor can manage shared memory")
    return u

def _layer(aid): return M.MemoryLayer(aid, "agent")

class FileBody(BaseModel):
    path: str = Field(max_length=300)
    content: str = Field(max_length=M.MAX_CHARS)

@router.get("/api/agents/{aid}/memory/files")
async def files(aid: int):
    await _guard(aid)
    out = []
    for p, (_, leg, v) in sorted((await _layer(aid)._all()).items()):
        fd = M.MemoryLayer._fd(leg, v); out.append({"path": p, "size": len(fd["content"].encode()), "modified_at": fd.get("modified_at") or fd.get("created_at") or ""})
    return out

@router.get("/api/agents/{aid}/memory/file")
async def read_file(aid: int, path: str):
    await _guard(aid)
    r = await _layer(aid)._get(M._norm(path) or "")
    if r is None: raise HTTPException(404, "File not found")
    return {"path": M._norm(path), "content": M.MemoryLayer._fd(r[1], r[2])["content"]}

@router.put("/api/agents/{aid}/memory/file")
async def write_file(aid: int, b: FileBody):
    await _guard(aid)
    err = await _layer(aid).owner_write(b.path, b.content)
    if err: raise HTTPException(422, err)
    return {"ok": True, "path": M._norm(b.path)}

@router.delete("/api/agents/{aid}/memory/file")
async def delete_file(aid: int, path: str):
    await _guard(aid)
    if not await _layer(aid).owner_delete(path): raise HTTPException(404, "File not found")
    return {"ok": True}

@router.get("/api/agents/{aid}/memory/proposals")
async def proposals(aid: int, status: str = "pending"):
    await _guard(aid)
    if status not in ("pending", "approved", "rejected", "all"): raise HTTPException(422, "status")
    return await _q("SELECT id, path, content, reason, run_id, source, status, created_at, decided_by, decided_at FROM memory_proposals WHERE agent_id=%s AND (%s='all' OR status=%s) ORDER BY id DESC LIMIT 200",
                    (aid, status, status))

async def _decide(aid, pid, status, u):
    r = await _q("UPDATE memory_proposals SET status=%s, decided_by=%s, decided_at=now() WHERE id=%s AND agent_id=%s AND status='pending' RETURNING id, path, content",
                 (status, u.get("username") or str(u.get("id")), pid, aid), one=True)
    if not r: raise HTTPException(404, "Proposal not found or already decided")
    return r

@router.post("/api/agents/{aid}/memory/proposals/{pid}/approve")
async def approve(aid: int, pid: int):
    u = await _guard(aid)
    r = await _decide(aid, pid, "approved", u)
    err = await _layer(aid).owner_write(r["path"], r["content"])
    if err:   # secret-looking / too big: back to pending, nothing written
        await _q("UPDATE memory_proposals SET status='pending', decided_by=NULL, decided_at=NULL WHERE id=%s", (pid,))
        raise HTTPException(422, err)
    return {"ok": True, "path": r["path"]}

@router.post("/api/agents/{aid}/memory/proposals/{pid}/reject")
async def reject(aid: int, pid: int):
    u = await _guard(aid)
    await _decide(aid, pid, "rejected", u)
    return {"ok": True}
