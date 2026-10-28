"""GĐ11.43 Virtual-computer API v2 (boxes, execute, command history, files). Router `/api/v2/sandboxes/boxes`; the old `/api/sandboxes/*` (kind=files) is untouched.
Everything goes through `sbx_runtime.Computers` (owner / workspace-admin checks, RLS via the store, demo refused, workspace switch). Files are read and written INSIDE the box
(the driver's in-box shell), confined to /workspace after `realpath`: a symlink that points outside is refused, a host file is never reachable.
Tests swap `_c` (Computers), `_ctx` (caller) and `_thread` (thread visibility); they call `drain()` to wait for background deletes."""
import asyncio, fnmatch, functools, json, posixpath, time
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
import sbx_runtime as R
import sbx_images  # noqa: F401  (11.47a: importing it registers the snapshot / setup-image purge in R.PURGE_HOOKS)

router = APIRouter()
UPLOAD_MAX = 100 << 20          # whole multipart request
LIST_MAX = 2000
HISTORY_DAYS = 30
BATCH_MAX = 50
ROOT = "/workspace"

# ---------------------------------------------------------------- plumbing
def _c(): return R.shared()
def _ctx(): return R.ctx_from_admin()

async def _thread(tid):
    """The thread row if the caller can see it (lg_threads has row-level security), else None."""
    import deepagent_api as A
    return await A.q("SELECT thread_id, agent_id FROM lg_threads WHERE thread_id=%s", (tid,), one=True)

def _errs(fn):
    @functools.wraps(fn)
    async def w(*a, **k):
        try: return await fn(*a, **k)
        except R.SbxError as e: raise R.http_error(e) from None
    return w

_BG: set = set()
def _bg(coro):
    t = asyncio.create_task(coro); _BG.add(t); t.add_done_callback(_BG.discard)
async def drain(): await asyncio.gather(*list(_BG), return_exceptions=True)

async def _caller(c, enabled=True):
    """Signed-in caller. Demo workspace / account: refused on every route. Workspace switch off: 403 (except where `enabled=False`, i.e. delete)."""
    ctx = _ctx()
    if not ctx or ctx.get("uid") is None: raise R.Forbidden("not signed in")
    if ctx.get("demo"): raise R.Forbidden("virtual computers are off for demo accounts")
    if enabled:
        q = await c.store.quota(int(ctx["ws"]))
        if q is not None and not q["enabled"]: raise R.Forbidden(R.OFF_MSG)
    return ctx

async def _resolve(c, ctx, key):
    """{name} in the URL = the box id (16 hex) or a unique box name among the boxes the caller can see."""
    if R.ID_RE.match(key or ""): return key
    rows = [r for r in await c.list(ctx) if r.get("name") == key]
    if not rows: raise R.NotFound("no such computer")
    if len(rows) > 1: raise R.BadState("several computers have this name: use the id")
    return rows[0]["id"]

async def _read_row(c, ctx, key):
    """Row for a READ: owner / workspace admin, or (box of a thread) anyone who can see that thread. Anything else is a 404."""
    id = await _resolve(c, ctx, key)
    try: return await c.get(ctx, id)
    except R.NotFound:
        r = await c.sys.get(R.check_id(id))
        if r and r.get("thread_id") and int(r["workspace_id"]) == int(ctx["ws"]) and R._rank(ctx) >= R.ROLE_RANK["viewer"] and await _thread(r["thread_id"]): return r
        raise

def _view(r):
    hs = r.get("host_state") or {}      # never returned: holds the proxy credential
    return {"id": r["id"], "name": r.get("name"), "kind": "computer", "status": r["status"], "size": r.get("size"), "scope": r.get("scope"), "core": r.get("core"),
            "agent_id": r.get("agent_id"), "thread_id": r.get("thread_id"), "owner_id": r.get("owner_id"), "user_id": r.get("user_id"), "last_error": r.get("last_error"),
            "idle_ttl_s": r.get("idle_ttl_s"), "delete_after_stop_s": r.get("delete_after_stop_s"), "last_active": r.get("last_active"), "created": r.get("created"),
            "internet": (hs.get("egress") or {}).get("mode", "off"), "disk_warn": bool(hs.get("disk_warn"))}

def _page(items, limit, cursor):
    try: off = max(0, int(cursor or 0))
    except ValueError: raise R.SbxError("bad cursor") from None
    limit = max(1, min(int(limit), 200)); part = items[off:off + limit]
    return {"items": part, "next_cursor": str(off + limit) if off + limit < len(items) else None}

# ---------------------------------------------------------------- file layer (runs inside the box; FakeDriver has its own twin)
_FS = r'''op=$1; p=$2; d=$3; sh=$4
inside(){ case "$1" in /workspace|/workspace/*) return 0;; esac; exit 43; }
leaf(){ pp=$(realpath -m -- "$(dirname -- "$1")") || exit 43; inside "$pp"; printf '%s/%s' "${pp%/}" "$(basename -- "$1")"; }
case $op in
 ls) r=$(realpath -m -- "$p"); inside "$r"; [ -d "$r" ] || exit 44
     if [ "$sh" = 1 ]; then find "$r" -mindepth 1 -maxdepth 1 -printf '%y\t%s\t%T@\t%p\0'; else find "$r" -mindepth 1 -printf '%y\t%s\t%T@\t%p\0' | head -z -n 5000; fi;;
 read) r=$(realpath -m -- "$p"); inside "$r"; [ -f "$r" ] || exit 44; stat -c %s -- "$r"; head -c ''' + str(R.PUT_MAX + 1) + r''' -- "$r";;
 write) r=$(realpath -m -- "$p"); inside "$r"; [ -d "$r" ] && exit 46; mkdir -p -- "$(dirname -- "$r")" && cat > "$r";;
 mkdir) r=$(realpath -m -- "$p"); inside "$r"; [ -e "$r" ] && [ ! -d "$r" ] && exit 45; mkdir -p -- "$r";;
 rm) t=$(leaf "$p") || exit 43; [ -e "$t" ] || [ -L "$t" ] || exit 44; rm -rf -- "$t";;
 mv) s=$(leaf "$p") || exit 43; t=$(realpath -m -- "$d"); inside "$t"; [ -e "$s" ] || [ -L "$s" ] || exit 44; [ -e "$t" ] || [ -L "$t" ] && exit 45
     mkdir -p -- "$(dirname -- "$t")" && mv -T -- "$s" "$t";;
 *) exit 2;;
esac'''

def _inside(p): return p == ROOT or p.startswith(ROOT + "/")

def _fs_error(rc, err):
    if rc == 43: raise R.Forbidden("the path leaves /workspace (a link pointing outside is refused)")
    if rc == 44: raise R.NotFound("no such file or folder")
    if rc == 45: raise R.BadState("the target already exists")
    if rc == 46: raise R.SbxError("the path is a folder")
    raise R.SbxError("file operation failed: " + (err or "")[:200])

async def _fs_docker(d, id, op, path, dest, data, shallow):
    rc, out, err = await d._d("exec", "-i", R.cname(R.check_id(id)), "bash", "-c", _FS, "_", op, path, dest or "", "1" if shallow else "0", stdin=data, timeout=120)
    if rc: _fs_error(rc, err)
    if op == "ls":
        rows = []
        for e in out.split(b"\0"):
            if not e: continue
            t, size, mt, p = e.decode(errors="replace").split("\t", 3)
            rows.append({"path": p, "name": posixpath.basename(p), "type": {"f": "file", "d": "dir", "l": "link"}.get(t, "other"), "size": int(size), "mtime": int(float(mt))})
        return rows
    if op == "read":
        nl = out.index(b"\n"); size, body = int(out[:nl]), out[nl + 1:]
        if size > R.PUT_MAX: raise R.SbxError("file too large")
        return body
    return None

def _fk_real(b, p, depth=0):
    if depth > 40: raise R.SbxError("too many symbolic links")
    cur = ""
    for part in p.strip("/").split("/"):
        cur += "/" + part
        if cur in b["links"]:
            t = b["links"][cur]; t = t if t.startswith("/") else posixpath.join(posixpath.dirname(cur), t)
            cur = _fk_real(b, posixpath.normpath(t), depth + 1)
    return cur

def _fk_in(p):
    if not _inside(p): _fs_error(43, "")
    return p

def _fk_all(b):
    kinds = {k: "file" for k in b["files"]}; kinds.update({k: "dir" for k in b["dirs"]}); kinds.update({k: "link" for k in b["links"]})
    for k in list(kinds):
        a = posixpath.dirname(k)
        while _inside(a) and a != ROOT and a not in kinds: kinds[a] = "dir"; a = posixpath.dirname(a)
    return kinds

def _fk_exists(b, p): return p == ROOT or p in _fk_all(b) or any(k.startswith(p + "/") for k in _fk_all(b))

def _fk_leaf(b, p): return posixpath.join(_fk_in(_fk_real(b, posixpath.dirname(p))), posixpath.basename(p))

def _fk_move(b, s, t):
    for store in (b["files"], b["links"]):
        for k in [k for k in store if k == s or k.startswith(s + "/")]: store[t + k[len(s):]] = store.pop(k)
    b["dirs"] |= {t + k[len(s):] for k in b["dirs"] if k == s or k.startswith(s + "/")}
    b["dirs"] -= {k for k in b["dirs"] if k == s or k.startswith(s + "/")}

async def _fs_fake(d, id, op, path, dest, data, shallow):
    b = d._b(id); b.setdefault("dirs", set()); b.setdefault("links", {})
    if op == "ls":
        r = _fk_in(_fk_real(b, path)); kinds = _fk_all(b)
        if not (r == ROOT or kinds.get(r) == "dir"): _fs_error(44, "")
        return [{"path": k, "name": posixpath.basename(k), "type": t, "size": len(b["files"].get(k, b"")), "mtime": 0} for k, t in sorted(kinds.items())
                if k.startswith(r + "/") and (not shallow or "/" not in k[len(r) + 1:])]
    if op == "read":
        r = _fk_in(_fk_real(b, path))
        if r not in b["files"]: _fs_error(44, "")
        return b["files"][r]
    if op == "write":
        r = _fk_in(_fk_real(b, path))
        if _fk_all(b).get(r) == "dir": _fs_error(46, "")
        b["files"][r] = bytes(data or b""); return None
    if op == "mkdir":
        r = _fk_in(_fk_real(b, path))
        if r in b["files"] or r in b["links"]: _fs_error(45, "")
        b["dirs"].add(r); return None
    if op == "rm":
        t = _fk_leaf(b, path)
        if not _fk_exists(b, t): _fs_error(44, "")
        for store in (b["files"], b["links"]):
            for k in [k for k in store if k == t or k.startswith(t + "/")]: del store[k]
        b["dirs"] -= {k for k in b["dirs"] if k == t or k.startswith(t + "/")}; return None
    if op == "mv":
        s, t = _fk_leaf(b, path), _fk_in(_fk_real(b, dest))
        if not _fk_exists(b, s): _fs_error(44, "")
        if _fk_exists(b, t) or t in b["links"]: _fs_error(45, "")
        if t.startswith(s + "/"): raise R.SbxError("cannot move a folder into itself")
        _fk_move(b, s, t); return None
    raise R.SbxError("bad operation")

async def fs(c, ctx, id, op, path, *, dest=None, data=None, shallow=True, row=None):
    """One file operation in the box. `row` = a row the caller already authorised for READING (thread viewer); writes always re-check owner / admin."""
    write = op in ("write", "mkdir", "rm", "mv")
    if write: r = await c._wake(await c._row(ctx, id, True))
    elif row is not None: r = row
    else:
        r = await c._row(ctx, id, False)
        try: R.authorize(ctx, r, True); r = await c._wake(r)      # reading wakes an idle box only for someone who may start it
        except R.SbxError: pass
    if r["status"] != "running": raise R.BadState(f"the computer is {r['status']}, start it first")
    path, dest = R.box_path(path), (R.box_path(dest) if dest else None)
    if data is not None and len(data) > R.PUT_MAX: raise R.SbxError("file too large")
    d = c._drv(r["core"])
    res = await (_fs_fake if d.name == "fake" else _fs_docker)(d, r["id"], op, path, dest, data, shallow)
    if write: await c.store.touch(r["id"])
    return res

# ---------------------------------------------------------------- command history
async def history(c, r, limit, cursor, q, exit_code):
    """sandbox_commands of one box, newest first, kept 30 days (older rows are dropped here, no extra job)."""
    st = c.store
    if isinstance(st, R.MemStore):
        rows = [dict(x, started=x.get("started")) for x in reversed(st.cmds) if x["sandbox_id"] == r["id"]
                and (not q or q.lower() in x["command"].lower()) and (exit_code is None or x.get("exit_code") == exit_code)]
    else:
        try: await st.q("DELETE FROM sandbox_commands WHERE sandbox_id=%s AND started < now() - make_interval(days => %s)", (r["id"], HISTORY_DAYS))
        except Exception: pass  # noqa: BLE001  (a read-only viewer may not delete; the owner's next read prunes)
        rows = await st.q("SELECT id, command, cwd, status, exit_code, truncated, duration_ms, started, user_id FROM sandbox_commands WHERE sandbox_id=%s "
                          "AND (%s::text IS NULL OR command ILIKE '%%' || %s || '%%') AND (%s::int IS NULL OR exit_code=%s) ORDER BY started DESC, id DESC LIMIT 1000",
                          (r["id"], q or None, q or None, exit_code, exit_code))
    return _page([{k: x.get(k) for k in ("id", "command", "cwd", "status", "exit_code", "truncated", "duration_ms", "started", "user_id")} for x in rows], limit, cursor)

# ---------------------------------------------------------------- boxes
class BoxIn(BaseModel):
    name: str = Field(default="computer", max_length=80)
    size: str = "small"
    scope: str = "thread"
    agent_id: int | None = None
    thread_id: str | None = Field(default=None, max_length=200)
    idle_ttl_s: int | None = None
    delete_after_stop_s: int | None = None
    internet: str = "off"
    allow_list: list[str] = Field(default_factory=list, max_length=100)
    start: bool = True

class BoxPatch(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    idle_ttl_s: int | None = None
    delete_after_stop_s: int | None = None

P = "/api/v2/sandboxes/boxes"

@router.get(P)
@_errs
async def boxes_list(status: str | None = None, kind: str | None = None, agent_id: int | None = None, thread_id: str | None = None, limit: int = 50, cursor: str | None = None):
    c = _c(); ctx = await _caller(c)
    rows = [] if kind not in (None, "computer") else [r for r in await c.list(ctx) if (not status or r["status"] == status)
                                                      and (agent_id is None or r.get("agent_id") == agent_id) and (not thread_id or r.get("thread_id") == thread_id)]
    out = _page(rows, limit, cursor); out["items"] = [_view(r) for r in out["items"]]; return out

@router.post(P, status_code=201)
@_errs
async def boxes_create(b: BoxIn):
    c = _c(); ctx = await _caller(c)
    return _view(await c.create(ctx, name=b.name, size=b.size, scope=b.scope, agent_id=b.agent_id, thread_id=b.thread_id, idle_ttl_s=b.idle_ttl_s,
                                delete_after_stop_s=b.delete_after_stop_s, internet=b.internet, allow_list=b.allow_list, start=b.start))

@router.post("/api/v2/sandboxes/boxes/batch-delete")
@_errs
async def boxes_batch_delete(b: dict):
    c = _c(); ctx = await _caller(c, enabled=False); names = b.get("names")
    if not isinstance(names, list) or not names or len(names) > BATCH_MAX or not all(isinstance(n, str) for n in names): raise R.SbxError(f"names: 1 to {BATCH_MAX} strings")
    return {"results": [{"name": n, "ok": await _delete_one(c, ctx, n)} for n in names]}

@router.get(P + "/{name}")
@_errs
async def box_get(name: str):
    c = _c(); ctx = await _caller(c); return _view(await _read_row(c, ctx, name))

@router.get(P + "/{name}/status")
@_errs
async def box_status(name: str):
    c = _c(); ctx = await _caller(c); r = await _read_row(c, ctx, name)
    return {"id": r["id"], "status": r["status"], "last_error": r.get("last_error"), "last_active": r.get("last_active"), "disk_warn": bool((r.get("host_state") or {}).get("disk_warn"))}

@router.patch(P + "/{name}")
@_errs
async def box_patch(name: str, b: BoxPatch):
    c = _c(); ctx = await _caller(c); r = await c._row(ctx, await _resolve(c, ctx, name), True)
    f = {k: getattr(b, k) for k in b.model_fields_set}
    if "name" in f: f["name"] = (f["name"] or "").strip()[:80] or r["name"]
    R.check_ttls(f.get("idle_ttl_s", r.get("idle_ttl_s")), f.get("delete_after_stop_s", r.get("delete_after_stop_s")))
    if f and not await c.store.cas(r["id"], r["status"], **f): raise R.BadState("state changed, try again")
    return _view(await c.store.get(r["id"]))

async def _delete_one(c, ctx, key):
    """Repeatable: a box that is already gone (or not visible) answers like a deleted one. The container + volumes are removed in the background."""
    try: r = await c._row(ctx, await _resolve(c, ctx, key), True)
    except R.NotFound: return True
    _bg(_bg_delete(c, ctx, r["id"])); return True

async def _bg_delete(c, ctx, id):
    try: await c.delete(ctx, id)
    except R.NotFound: pass
    except Exception as e: print(f"[sbx-api] delete of {id} failed: {str(e)[:200]}", flush=True)  # noqa: BLE001

@router.delete(P + "/{name}", status_code=204)
@_errs
async def box_delete(name: str):
    c = _c(); ctx = await _caller(c, enabled=False); await _delete_one(c, ctx, name); return Response(status_code=204)

@router.post(P + "/{name}/start")
@_errs
async def box_start(name: str):
    c = _c(); ctx = await _caller(c); return _view(await c.start(ctx, await _resolve(c, ctx, name)))

@router.post(P + "/{name}/stop")
@_errs
async def box_stop(name: str):
    c = _c(); ctx = await _caller(c); return _view(await c.stop(ctx, await _resolve(c, ctx, name)))

class ThreadIn(BaseModel):
    size: str = "small"

@router.post("/api/threads/{tid}/computer/activate")
@_errs
async def thread_activate(tid: str, b: ThreadIn | None = None):
    """The caller's own computer of this thread: reuse it (starting it if stopped) or create it."""
    c = _c(); ctx = await _caller(c); t = await _thread(tid)
    if not t: raise R.NotFound("no such thread")
    mine = [r for r in await c.list(ctx) if r.get("thread_id") == tid and r.get("scope") == "thread" and r["status"] != "failed" and ctx["uid"] in (r.get("owner_id"), r.get("user_id"))]
    if mine:
        r = mine[0]
        return _view(await c.start(ctx, r["id"]) if r["status"] == "stopped" else r)
    return _view(await c.create(ctx, name="thread " + tid[:8], size=(b.size if b else "small"), scope="thread", agent_id=t.get("agent_id"), thread_id=tid))

# ---------------------------------------------------------------- execute + history
class ExecIn(BaseModel):
    command: str = Field(min_length=1, max_length=R.MAX_CMD)
    timeout: int = Field(default=60, ge=1, le=3600)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str = ROOT
    stdin: str | None = Field(default=None, max_length=1 << 20)

@router.post(P + "/{name}/execute")
@_errs
async def box_execute(name: str, b: ExecIn):
    c = _c(); ctx = await _caller(c)       # Computers.exec sets R.SPAN_ATTRS (sandbox.id / kind / status_before / exit_code / duration_ms) for whoever wraps a span around the call
    res = await c.exec(ctx, await _resolve(c, ctx, name), b.command, cwd=b.cwd, env=b.env, timeout=b.timeout, stdin=b.stdin.encode() if b.stdin is not None else None)
    return {"stdout": res["output"], "stderr": "", "exit_code": res["exit_code"], "success": res["exit_code"] == 0 and not res["timed_out"], "truncated": res["truncated"],
            "timed_out": res["timed_out"], "duration_ms": res["duration_ms"], "command_id": res["command_id"]}

@router.get(P + "/{name}/commands")
@_errs
async def box_commands(name: str, limit: int = 50, cursor: str | None = None, q: str | None = None, exit_code: int | None = None):
    c = _c(); ctx = await _caller(c); return await history(c, await _read_row(c, ctx, name), limit, cursor, q, exit_code)

# ---------------------------------------------------------------- files
async def _read_op(name, op, path, **kw):
    c = _c(); ctx = await _caller(c); r = await _read_row(c, ctx, name)
    return await fs(c, ctx, r["id"], op, path, row=r, **kw)

@router.get(P + "/{name}/files")
@_errs
async def files_list(name: str, path: str = ROOT, glob: str | None = None):
    rows = await _read_op(name, "ls", path, shallow=not glob)
    base = R.box_path(path)
    if glob: rows = [x for x in rows if fnmatch.fnmatchcase(x["path"][len(base):].lstrip("/") if "/" in glob else x["name"], glob)]
    return {"path": base, "items": rows[:LIST_MAX], "truncated": len(rows) > LIST_MAX}

@router.get(P + "/{name}/files/content")
@_errs
async def file_get(name: str, path: str):
    body = await _read_op(name, "read", path)
    # user files are untrusted: never rendered by the browser
    return Response(body, media_type="application/octet-stream", headers={"Content-Disposition": 'attachment; filename="' + posixpath.basename(R.box_path(path)).replace('"', "") + '"',
                                                                         "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"})

async def _write(name, op, path, **kw):
    c = _c(); ctx = await _caller(c); return await fs(c, ctx, await _resolve(c, ctx, name), op, path, **kw)

@router.put(P + "/{name}/files/content")
@_errs
async def file_put(name: str, path: str, request: Request):
    if int(request.headers.get("content-length") or 0) > R.PUT_MAX: return Response(json.dumps({"detail": "file too large"}), 413, media_type="application/json")
    data = await request.body()
    await _write(name, "write", path, data=data); return {"ok": True, "path": R.box_path(path), "bytes": len(data)}

@router.post(P + "/{name}/files/upload")
@_errs
async def files_upload(name: str, request: Request):
    """multipart: field `files` (one or many; the filename may carry subfolders, e.g. `src/a/b.txt`) + optional field `path` (target folder, default /workspace)."""
    if int(request.headers.get("content-length") or 0) > UPLOAD_MAX + (1 << 20): return Response(json.dumps({"detail": "upload larger than 100 MB"}), 413, media_type="application/json")
    form = await request.form(); base = R.box_path(form.get("path") if isinstance(form.get("path"), str) else None); done, total = [], 0
    for f in form.getlist("files"):
        fn = (getattr(f, "filename", "") or "").replace("\\", "/").lstrip("/")
        if not fn: raise R.SbxError("a file has no name")
        data = await f.read(); total += len(data)
        if len(data) > R.PUT_MAX or total > UPLOAD_MAX: return Response(json.dumps({"detail": "file or upload too large"}), 413, media_type="application/json")
        dest = R.box_path(posixpath.join(base, fn)); await _write(name, "write", dest, data=data); done.append({"path": dest, "bytes": len(data)})
    if not done: raise R.SbxError("no files in the upload (field name: files)")
    return {"uploaded": done}

class PathIn(BaseModel):
    path: str = Field(max_length=1024)

class MoveIn(BaseModel):
    src: str = Field(max_length=1024)
    dest: str = Field(max_length=1024)

@router.post(P + "/{name}/files/mkdir")
@_errs
async def files_mkdir(name: str, b: PathIn): await _write(name, "mkdir", b.path); return {"ok": True, "path": R.box_path(b.path)}

@router.post(P + "/{name}/files/move")
@_errs
async def files_move(name: str, b: MoveIn): await _write(name, "mv", b.src, dest=b.dest); return {"ok": True, "path": R.box_path(b.dest)}

@router.delete(P + "/{name}/files")
@_errs
async def files_delete(name: str, path: str): await _write(name, "rm", path); return {"ok": True}

# ---------------------------------------------------------------- 11.47a snapshots (capture of a box, setup.sh bake) - logic in sbx_images.py
SP = "/api/v2/sandboxes/snapshots"
def _i():
    import sbx_images; return sbx_images.shared()

class SnapIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)

class BakeIn(BaseModel):
    agent_id: int
    setup_sh: str = Field(min_length=1, max_length=65536)
    identifier: str | None = Field(default=None, max_length=80)

class FromSnapIn(BaseModel):
    name: str = Field(default="computer", max_length=80)
    size: str = "small"
    scope: str = "thread"
    agent_id: int | None = None
    thread_id: str | None = None

@router.post(P + "/{name}/snapshot", status_code=201)
@_errs
async def box_snapshot(name: str, b: SnapIn):
    c = _c(); ctx = await _caller(c); import sbx_images
    return sbx_images.view(await _i().capture(ctx, await _resolve(c, ctx, name), b.name))

@router.get(SP)
@_errs
async def snaps_list(limit: int = 50, cursor: str | None = None):
    import sbx_images
    c = _c(); ctx = await _caller(c); out = _page(await _i().list(ctx), limit, cursor); out["items"] = [sbx_images.view(r) for r in out["items"]]; return out

@router.post(SP + "/bake", status_code=201)
@_errs
async def snaps_bake(b: BakeIn):
    """Builds (or reuses) the setup image of an agent; can take minutes. 422 + the log in `snapshots/<name>` when setup.sh fails or leaks a secret."""
    import sbx_images
    c = _c(); ctx = await _caller(c); return sbx_images.view(await _i().bake(ctx, b.agent_id, b.setup_sh, identifier=b.identifier))

@router.get(SP + "/{ref}")
@_errs
async def snap_get(ref: str):
    import sbx_images
    c = _c(); ctx = await _caller(c); return sbx_images.view(await _i().get(ctx, ref))

@router.delete(SP + "/{ref}", status_code=204)
@_errs
async def snap_delete(ref: str):
    c = _c(); ctx = await _caller(c, enabled=False); await _i().delete(ctx, ref); return Response(status_code=204)

@router.post(SP + "/{ref}/boxes", status_code=201)
@_errs
async def snap_new_box(ref: str, b: FromSnapIn):
    c = _c(); ctx = await _caller(c)
    return _view(await _i().create_box(ctx, ref, name=b.name, size=b.size, scope=b.scope, agent_id=b.agent_id, thread_id=b.thread_id))
