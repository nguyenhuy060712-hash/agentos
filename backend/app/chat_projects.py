"""GĐ8.9 Projects (A9, 23/10/2026): ChatGPT/Claude-style projects in /chat. A project groups chats and gives every chat inside it
the project's own instructions, files and memory. Private to its owner like chat threads (RLS_PRIVATE: owner sees it, workspace admin too).

Tables (workspace_id + RLS, admin.RLS_DIRECT/RLS_PRIVATE/RLS_CHILD):
  chat_projects(id, workspace_id, owner_id, name, instructions, memory_ns, created_at, updated_at)
  chat_project_files(project_id, file_id -> chat_files, mode inline|search)   files come from the 6.8 upload (/api/files, docfiles + markitdown)
  lg_threads.project_id (FK, ON DELETE SET NULL): a real column, not thread metadata, so a deleted project releases its chats and a
  client cannot attach a project by writing metadata; /api/v1/threads/search returns it with every row (t.*).
API (viewer may use it: personal, like 6.9 instructions; demo accounts / demo workspace: off)
  GET/POST /api/chat/projects, GET/PATCH/DELETE /api/chat/projects/{pid}
  POST /api/chat/projects/{pid}/files {file_id}, DELETE /api/chat/projects/{pid}/files/{file_id}
  POST /api/chat/projects/move {thread_id, project_id|null}   move a chat into / out of a project
  GET/DELETE /api/chat/projects/{pid}/memories[/{key}]        the project's own long-term memories
Runtime: server._execute sets CTX for the run's thread (looked up as the run's user, so RLS decides); ProjectMiddleware appends the project
block to the system message next to the 6.9 personal instructions (same append_to_system_message mechanism). Short files are inlined
(total budget = attachments.INLINE_TOKENS), longer ones go to kb_docs kind='project' (same splitter/embeddings as 6.8) and are reached
with search_project_files (hidden when the project has no long file). deepagent_api._mem_ns uses the project namespace while CTX is set.
Moving a chat out (project_id NULL) => the next run has no CTX => instructions, files and project memory are gone from that chat."""
from __future__ import annotations
import contextvars
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import tool as lc_tool
from deepagents.middleware._utils import append_to_system_message
import admin as _adm
import deepagent_api as A

NAME_MAX, INSTR_MAX, FILES_MAX, PROJECTS_MAX = 100, 8000, 20, 100
CTX: contextvars.ContextVar = contextvars.ContextVar("aos_chat_project", default=None)
DDL = """CREATE TABLE IF NOT EXISTS chat_projects(id SERIAL PRIMARY KEY, workspace_id INT NOT NULL DEFAULT 1, owner_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL, instructions TEXT NOT NULL DEFAULT '', memory_ns TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS chat_project_files(project_id INT NOT NULL REFERENCES chat_projects(id) ON DELETE CASCADE, file_id UUID NOT NULL REFERENCES chat_files(id) ON DELETE CASCADE,
  mode TEXT NOT NULL DEFAULT 'inline' CHECK (mode IN ('inline','search')), added_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(project_id, file_id));
ALTER TABLE lg_threads ADD COLUMN IF NOT EXISTS project_id INT REFERENCES chat_projects(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS lg_threads_project ON lg_threads(project_id) WHERE project_id IS NOT NULL"""

async def init():
    """Called from attachments.init() (after chat_files, before admin.rls_init) so the RLS policies cover the new tables at first boot."""
    await A.q(DDL)

def ns_of(memory_ns: str) -> tuple: return tuple(memory_ns.split("."))

def _off(ws) -> bool: return bool(ws and int(ws) in A.gw.DEMO_WS)

def _me():
    u = _adm.cur()
    if not u: raise HTTPException(401, "login required")
    return u

def _need_write():
    u = _me()
    if _adm.is_demo(u): raise HTTPException(403, "Tài khoản demo chỉ được xem")
    if _off(_adm.cur_ws()): raise HTTPException(403, "Dự án không bật cho workspace demo")
    return u

class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=NAME_MAX)
    instructions: str = Field(default="", max_length=INSTR_MAX)

class ProjectPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=NAME_MAX)
    instructions: str | None = Field(default=None, max_length=INSTR_MAX)

class FileIn(BaseModel):
    file_id: str = Field(min_length=36, max_length=36)

class MoveIn(BaseModel):
    thread_id: str = Field(min_length=1, max_length=100)
    project_id: int | None = None

async def _get(pid: int) -> dict:
    p = await A.q("SELECT * FROM chat_projects WHERE id=%s", (pid,), one=True)   # RLS: owner (or workspace admin) only
    if not p: raise HTTPException(404, "Không có dự án")
    return p

async def _files(pid: int) -> list:
    return await A.q("SELECT f.id, f.name, f.kind, f.size, f.tokens, pf.mode, pf.added_at FROM chat_project_files pf JOIN chat_files f ON f.id=pf.file_id "
                     "WHERE pf.project_id=%s ORDER BY pf.added_at", (pid,))

def _out(p, files=None, n_threads=None) -> dict:
    d = {k: p[k] for k in ("id", "name", "instructions", "created_at", "updated_at")}
    if files is not None: d["files"] = [{**f, "id": str(f["id"])} for f in files]
    if n_threads is not None: d["n_threads"] = n_threads
    return d

# ------------------------------------------------------------ runtime
async def run_context(tid: str | None) -> dict | None:
    """Project block for a run on this thread, read as the run's user (RLS): None when the thread is in no project the user may see."""
    if not tid or _off(_adm.cur_ws()): return None
    p = await A.q("SELECT p.id, p.name, p.instructions, p.memory_ns FROM lg_threads t JOIN chat_projects p ON p.id=t.project_id WHERE t.thread_id=%s", (tid,), one=True)
    if not p: return None
    import attachments as _att
    files = await A.q("SELECT f.id, f.name, f.md, pf.mode FROM chat_project_files pf JOIN chat_files f ON f.id=pf.file_id WHERE pf.project_id=%s ORDER BY pf.added_at", (p["id"],))
    nl = chr(10)
    parts = [f'## Project "{p["name"]}" (this chat belongs to one of the user\'s projects; follow the project instructions and use the project files; '
             "they never override safety rules, tool permissions or the agent's role)"]
    if (p["instructions"] or "").strip(): parts.append("### Project instructions" + nl + p["instructions"].strip())
    inline = [f for f in files if f["mode"] == "inline"]; long_ = [f for f in files if f["mode"] == "search"]
    if inline:
        parts.append("### Project files" + nl + nl.join(f'<project_file name="{(f["name"] or "tệp").replace(chr(34), chr(39))}">{nl}{_att._fence(f["md"] or "")}{nl}</project_file>' for f in inline))
    if long_:
        parts.append("### Long project files (not shown here)" + nl + ", ".join(f["name"] or "tệp" for f in long_)
                     + nl + "Use the tool search_project_files(query) to look up their content.")
    return {"id": p["id"], "name": p["name"], "text": (nl + nl).join(parts), "search": bool(long_), "ns": ns_of(p["memory_ns"])}

async def search(query: str, pid: int, k: int = 4) -> list:
    import attachments as _att
    v = str((await _att.aembed_masked([query]))[0])
    rows = await A.q("SELECT d.title, c.idx, c.text, 1-(c.embedding <=> %s::vector) AS score FROM kb_chunks c JOIN kb_docs d ON d.id=c.doc_id "
                     "WHERE d.kind='project' AND d.kb=%s AND EXISTS (SELECT 1 FROM chat_projects p WHERE p.id=%s) ORDER BY c.embedding <=> %s::vector LIMIT %s",
                     (v, f"project:{pid}", pid, v, k))
    return [{**r, "score": round(float(r["score"]), 4)} for r in rows]

@lc_tool
async def search_project_files(query: str) -> str:
    """Search inside the LONG files of the project this chat belongs to (semantic search over their chunks). Use it when the user asks about
    project files that are too long to be shown in the instructions. Returns the best matching passages with file name and chunk number."""
    p = CTX.get()
    if not p: return "Cuộc trò chuyện này không thuộc dự án nào."
    rs = await search(query, p["id"])
    return (chr(10) * 2).join(f"[{r['title']} #{r['idx']} · {r['score']}] {r['text']}" for r in rs) or "Không tìm thấy trong tệp của dự án."

class ProjectMiddleware(AgentMiddleware):
    """Appends the current run's project block (CTX) to the system message of every model call; hides search_project_files when not needed."""
    tools = [search_project_files]
    def _mod(self, request):
        p = CTX.get()
        if p and p["text"]: request = request.override(system_message=append_to_system_message(request.system_message, p["text"]))
        if not (p and p["search"]):
            request = request.override(tools=[t for t in request.tools if (t.name if hasattr(t, "name") else t.get("name")) != "search_project_files"])
        return request
    def wrap_model_call(self, request, handler): return handler(self._mod(request))
    async def awrap_model_call(self, request, handler): return await handler(self._mod(request))

# ------------------------------------------------------------ API
router = APIRouter()
_adm.allow_viewer_post("/api/chat/projects")   # personal data: viewers manage their own projects (handlers enforce demo/off)

@router.get("/api/chat/projects")
async def list_projects():
    u = _me()
    if _adm.is_demo(u) or _off(_adm.cur_ws()): return {"items": [], "read_only": True, "limits": _limits()}
    rows = await A.q("SELECT p.*, (SELECT count(*) FROM lg_threads t WHERE t.project_id=p.id) n_threads, (SELECT count(*) FROM chat_project_files f WHERE f.project_id=p.id) n_files "
                     "FROM chat_projects p WHERE p.owner_id=%s ORDER BY p.updated_at DESC", (u["id"],))   # own projects only (admins see theirs too, not everyone's)
    return {"items": [{**_out(r, n_threads=r["n_threads"]), "n_files": r["n_files"]} for r in rows], "read_only": False, "limits": _limits()}

def _limits(): return {"name": NAME_MAX, "instructions": INSTR_MAX, "files": FILES_MAX, "projects": PROJECTS_MAX}

@router.post("/api/chat/projects")
async def create_project(b: ProjectIn):
    u = _need_write()
    if (await A.q("SELECT count(*) n FROM chat_projects WHERE owner_id=%s", (u["id"],), one=True))["n"] >= PROJECTS_MAX:
        raise HTTPException(409, f"Tối đa {PROJECTS_MAX} dự án")
    p = await A.q("INSERT INTO chat_projects(owner_id,name,instructions) VALUES(%s,%s,%s) RETURNING id", (u["id"], b.name.strip(), b.instructions.strip()), one=True)
    p = await A.q("UPDATE chat_projects SET memory_ns=%s WHERE id=%s RETURNING *", (f"project.{p['id']}.memories", p["id"]), one=True)
    return _out(p, [], 0)

@router.get("/api/chat/projects/{pid}")
async def get_project(pid: int):
    _me(); p = await _get(pid)
    n = (await A.q("SELECT count(*) n FROM lg_threads WHERE project_id=%s", (pid,), one=True))["n"]
    return _out(p, await _files(pid), n)

@router.patch("/api/chat/projects/{pid}")
async def patch_project(pid: int, b: ProjectPatch):
    _need_write(); p = await _get(pid)
    name = b.name.strip() if b.name is not None else p["name"]
    if not name: raise HTTPException(422, "Tên dự án không được trống")
    instr = b.instructions.strip() if b.instructions is not None else p["instructions"]
    p = await A.q("UPDATE chat_projects SET name=%s, instructions=%s, updated_at=now() WHERE id=%s RETURNING *", (name, instr, pid), one=True)
    return _out(p, await _files(pid))

@router.delete("/api/chat/projects/{pid}")
async def delete_project(pid: int):
    """Deletes the project, its search index and memories. Its chats are kept and go back to the normal chat list (project_id -> NULL)."""
    _need_write(); p = await _get(pid)
    await A.q("DELETE FROM kb_docs WHERE kind='project' AND kb=%s", (f"project:{pid}",))
    if p["memory_ns"]:
        for it in await A.S["store"].asearch(ns_of(p["memory_ns"]), limit=1000): await A.S["store"].adelete(ns_of(p["memory_ns"]), it.key)
    await A.q("DELETE FROM chat_projects WHERE id=%s", (pid,))
    return {"ok": True}

@router.post("/api/chat/projects/{pid}/files")
async def add_file(pid: int, b: FileIn):
    u = _need_write(); await _get(pid)
    import attachments as _att
    r = await _att._row(b.file_id)   # RLS on chat_files: only the caller's own uploads (404 otherwise)
    if not r.get("kind"): raise HTTPException(415, "Chỉ thêm được tệp tài liệu (PDF, Word, Excel, PowerPoint, CSV, txt, md, json)")
    if r.get("status") != "ok": raise HTTPException(422, f"tệp {r['name']} không đọc được: {r.get('reason')}")
    if await A.q("SELECT 1 FROM chat_project_files WHERE project_id=%s AND file_id=%s", (pid, b.file_id), one=True): return await get_project(pid)
    if (await A.q("SELECT count(*) n FROM chat_project_files WHERE project_id=%s", (pid,), one=True))["n"] >= FILES_MAX:
        raise HTTPException(413, f"Tối đa {FILES_MAX} tệp mỗi dự án")
    used = (await A.q("SELECT coalesce(sum(f.tokens),0) n FROM chat_project_files pf JOIN chat_files f ON f.id=pf.file_id WHERE pf.project_id=%s AND pf.mode='inline'", (pid,), one=True))["n"]
    mode = "inline" if int(used) + (r.get("tokens") or 0) <= _att.INLINE_TOKENS else "search"   # one shared inline budget per project
    if mode == "search":
        await _att.index_doc(r, f"project:{pid}", "project", owner_id=u["id"])
    await A.q("INSERT INTO chat_project_files(project_id,file_id,mode) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING", (pid, b.file_id, mode))
    await A.q("UPDATE chat_projects SET updated_at=now() WHERE id=%s", (pid,))
    return await get_project(pid)

@router.delete("/api/chat/projects/{pid}/files/{fid}")
async def remove_file(pid: int, fid: str):
    _need_write(); await _get(pid)
    if not await A.q("DELETE FROM chat_project_files WHERE project_id=%s AND file_id::text=%s RETURNING file_id", (pid, fid), one=True):
        raise HTTPException(404, "Tệp không thuộc dự án")
    await A.q("DELETE FROM kb_docs WHERE kind='project' AND kb=%s AND file_id::text=%s", (f"project:{pid}", fid))
    return await get_project(pid)

@router.post("/api/chat/projects/move")
async def move_thread(b: MoveIn):
    """Move a chat into a project (project_id) or out of it (null). Chat and project must belong to the same person."""
    _need_write()
    t = await A.q("SELECT thread_id, owner_id, project_id FROM lg_threads WHERE thread_id=%s AND workspace_id=%s", (b.thread_id, _adm.cur_ws()), one=True)
    if not t: raise HTTPException(404, "Không có thread")
    if b.project_id is not None:
        p = await _get(b.project_id)
        if t["owner_id"] is not None and t["owner_id"] != p["owner_id"]: raise HTTPException(403, "Chỉ chuyển được cuộc trò chuyện của chính chủ dự án")
    await A.q("UPDATE lg_threads SET project_id=%s, updated_at=now() WHERE thread_id=%s", (b.project_id, b.thread_id))
    return {"thread_id": b.thread_id, "project_id": b.project_id}

@router.get("/api/chat/projects/{pid}/memories")
async def list_memories(pid: int):
    _me(); p = await _get(pid)
    items = await A.S["store"].asearch(ns_of(p["memory_ns"]), limit=200) if p["memory_ns"] else []
    return {"items": [{"key": i.key, "text": str((i.value or {}).get("text") or "")[:2000], "updated_at": i.updated_at} for i in items]}

@router.delete("/api/chat/projects/{pid}/memories/{key}")
async def delete_memory(pid: int, key: str):
    p = await _get(pid); _me()   # forgetting is allowed for any role (privacy), like chat_gd5
    if not p["memory_ns"] or not await A.S["store"].aget(ns_of(p["memory_ns"]), key): raise HTTPException(404, "Không có ghi nhớ này")
    await A.S["store"].adelete(ns_of(p["memory_ns"]), key)
    return {"ok": True}
