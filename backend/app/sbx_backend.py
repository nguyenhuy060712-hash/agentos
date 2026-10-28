"""GĐ11.41 the virtual computer as the agent's file system + shell: `AgentOSSandbox(BaseSandbox)` (deepagents 0.7.32).

deepagents' BaseSandbox turns ONE primitive (`execute`) into ls / read / write / edit / glob / grep / delete; we add `execute`, `id`, `upload_files`,
`download_files` on top of `sbx_runtime.Computers` (11.40/11.42), so every permission, quota, capacity and lifecycle rule of the box layer applies unchanged.

* LAZY: agents are cached per (agent, version) (deepagent_api.build_agent), so the box is resolved on EVERY call from the run (`langgraph.config.get_config()`:
  thread id; `admin.USER` = who runs it). A run that never touches a file or `execute` never creates a box.
* WHOSE BOX (`box_key`): a box row is addressed by (agent, scope, thread, user) and the USER is always part of it, so two people never share a box:
  - mode `thread`: (agent, this thread, this user)        - mode `agent`: (agent, nothing, this user)  (F11: the owner and every shared user each get their own)
  - external senders (channel chatters, `configurable["aos_trust"] == "external"`, consumed by 11.72): ALWAYS their own per-thread box, never the owner's, no Internet.
  - unattended runs (cron / webhook, no signed-in user): the agent OWNER's identity and box.
  An admin of the workspace can see all boxes, but still only OPENS the one that matches the key, so A's files never reach B through a guessed thread id.
* Every failure (other workspace, demo account, viewer / read-only key, quota, no gVisor, failed box) is returned to the model as `Error: ...` text with exit code 1:
  nothing runs, the run does not crash.
* Output caps: the box layer cuts a command at 100 KB (rest in /workspace/.aos/out/<id>.log). The model sees the cut text + where the log is. deepagents' own helper
  scripts (read / ls / glob / grep / edit print JSON) are never cut: for those the full log is read back (<= 4 MiB).
* Files the agent writes to /workspace/out/ appear in the chat Files panel (`[aos-files]`, same store as the one-shot runner); attachments of the thread are copied to
  /workspace/uploads/ (OutFilesMiddleware / _push_uploads)."""
import asyncio, contextvars, functools, json, logging, os, posixpath, re, shlex, weakref
from dataclasses import dataclass

from deepagents.backends import CompositeBackend, StateBackend
from deepagents.backends.protocol import (FILE_NOT_FOUND, INVALID_PATH, PERMISSION_DENIED, DeleteResult, EditResult, ExecuteResponse, FileDownloadResponse,
                                          FileUploadResponse, GlobResult, GrepResult, LsResult, WriteResult)
from deepagents.backends.sandbox import BaseSandbox
from deepagents.middleware.filesystem import FilesystemPermission
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

import sbx_runtime as R
import sbx_images      # 11.47a: importing it registers the snapshot / setup-image purge in R.PURGE_HOOKS (also when sbx_api is not loaded)
from sbx_cfg import ComputerCfg
import sbx_skills as SK      # 11.64

_log = logging.getLogger("aos.sbx_backend")
EXEC_DEFAULT_S = int(os.environ.get("AOS_BOX_EXEC_S", "120"))        # `execute` without a timeout
EXEC_MAX_S = int(os.environ.get("AOS_BOX_EXEC_MAX_S", "600"))        # upper limit of a per-call timeout (a bigger request is cut down to this)
HELPER_LOG_MAX = 4 << 20                                            # deepagents' own helper scripts: how much of a >100 KB log is read back
PROTECTED = ("/", "/workspace", "/opt", "/opt/aos-user", "/workspace/.aos", "/workspace/uploads", "/workspace/out")   # `delete` refuses these (the folders themselves, not their content)
WATCH_TOOLS = ("execute", "write_file", "edit_file", "task")        # after these the /workspace/out folder is looked at
OUT_DIR, UPLOAD_DIR, PUBLISHED = "/workspace/out", "/workspace/uploads", "/workspace/.aos/published.json"
SCAN_OUT = ("# aos:scan-out\ncd " + OUT_DIR + " 2>/dev/null && find . -type f -printf '%P\\t%s\\t%T@\\n' 2>/dev/null | head -50")
SETUP_PATH = "/opt/aos-user/setup.sh"

_INTERNAL = contextvars.ContextVar("sbx_internal", default=False)    # True while a deepagents helper (read / ls / ...) runs, False for the model's own `execute`

# ---------------------------------------------------------------- who runs, which box
_C: dict = {"c": None}

def computers() -> R.Computers:
    """The box layer. Production: the one shared instance (locks / busy counters live on it). Test server only: AOS_SBX_FAKE_DRIVER=1 -> in-memory FakeDriver
    (never in production: ignored when AOS_ENV is prod)."""
    if _C["c"] is None:
        if os.environ.get("AOS_SBX_FAKE_DRIVER") == "1" and os.environ.get("AOS_ENV", "").lower() not in ("prod", "production"):
            _C["c"] = R.Computers(driver=R.FakeDriver())
        else: return R.shared()
    return _C["c"]

def set_computers(c): _C["c"] = c; _C["i"] = None                  # tests / the API start-up

def images() -> "sbx_images.Images":
    """Snapshots / setup images (11.47a) on the same box layer as `computers()`."""
    if _C.get("i") is None:
        c = computers(); _C["i"] = sbx_images.shared() if c is R.shared() else sbx_images.Images(computers=c)
    return _C["i"]

def set_images(i): _C["i"] = i

def _caller():
    """-> (ctx, auth) of the signed-in user of this run, or (None, None) for an unattended run."""
    import admin
    u = admin.cur()
    if not u or u.get("id") is None: return None, None
    return R.ctx_from_admin(), u.get("auth")

async def _owner_ctx(agent_id, ws):
    """Identity of the agent OWNER for unattended runs (cron / webhook): their role in the agent's workspace. None -> no box (an agent without an owner has no one to charge it to)."""
    import deepagent_api as A
    a = await A.q("SELECT owner_id, workspace_id FROM agents WHERE id=%s", (agent_id,), one=True)
    if not a or a["owner_id"] is None or int(a["workspace_id"]) != int(ws): return None
    m = await A.q("SELECT m.role, u.demo FROM users u LEFT JOIN memberships m ON m.user_id=u.id AND m.workspace_id=%s WHERE u.id=%s AND NOT u.disabled", (ws, a["owner_id"]), one=True)
    if not m or not m["role"]: return None
    import gateway as gw
    return {"ws": int(ws), "uid": a["owner_id"], "role": m["role"], "demo": bool(m["demo"]) or int(ws) in gw.DEMO_WS, "scope": "full"}

@dataclass
class Target:
    ctx: dict
    scope: str          # 'thread' | 'agent'
    thread: str | None  # value stored in sandboxes.thread_id (None for an agent box)
    user: int | None    # value stored in sandboxes.user_id (None for an external sender's box)
    external: bool
    plain_thread: str   # the chat thread id (attachments / names)

def _plain_thread(tid): return tid.split(":", 1)[1] if ":" in tid else tid

def box_key(cfg: ComputerCfg, ctx: dict, tid: str, external: bool):
    """-> (scope, thread_id, user_id): the three columns that, with agent_id, identify the box of this run. Pure (unit-tested)."""
    if external: return "thread", "ext:" + tid, None
    if cfg.mode == "thread": return "thread", tid, ctx["uid"]
    return "agent", None, ctx["uid"]

async def target_for(agent_id, ws, cfg: ComputerCfg, config) -> Target:
    configurable = (config or {}).get("configurable") or {}
    tid = configurable.get("thread_id")
    if not isinstance(tid, str) or not tid: raise R.SbxError("this run has no thread, so it has no computer")
    ctx, auth = _caller()
    if ctx is None: ctx = await _owner_ctx(agent_id, ws)
    if ctx is None: raise R.Forbidden("this agent has no owner who may run a computer")
    if int(ctx["ws"]) != int(ws): raise R.Forbidden("this agent belongs to another workspace")       # the box lives in the workspace of the person who runs the agent: never across
    if ctx.get("demo"): raise R.Forbidden("virtual computers are off for demo accounts")
    import run_trust
    external = configurable.get("aos_trust") == "external" or auth == "channel" or run_trust.is_external(config)      # 11.72: the one trust rule; an extra signal can only add
    scope, th, us = box_key(cfg, ctx, tid, external)
    return Target(ctx, scope, th, us, external, _plain_thread(tid))

def _match(r, agent_id, t: Target):
    return r.get("agent_id") == agent_id and r.get("scope") == t.scope and r.get("thread_id") == t.thread and r.get("user_id") == t.user

_LOCKS: "weakref.WeakValueDictionary" = weakref.WeakValueDictionary()
def _lock(key):
    l = _LOCKS.get(key)
    if l is None: l = _LOCKS[key] = asyncio.Lock()
    return l

async def find_box(comp, agent_id, t: Target):
    """The box of this key among the boxes the caller may see (RLS + owner rule), or None. A failed box is NEVER replaced silently (11.42): the person decides."""
    for r in await comp.list(t.ctx):
        if _match(r, agent_id, t): return r
    return None

def _skill_mounts(agent_id):
    sbx = _BY_AGENT.get(int(agent_id))
    d = getattr(sbx, "skills_dir", None)
    return ((str(d), SK.MOUNT),) if d else ()

async def ensure_box(comp, agent_id, cfg: ComputerCfg, t: Target, create=True):
    async with _lock((t.ctx["ws"], agent_id, t.scope, t.thread, t.user)):          # two parallel tool calls of one run must not create two boxes
        r = await find_box(comp, agent_id, t)
        if r is not None:
            if r["status"] == "failed": raise R.BoxFailed(f"this computer failed ({r.get('last_error') or 'unknown'}): delete it and create a new one")
            return r
        if not create: return None
        name = (f"agent {agent_id} · chat {t.plain_thread[:12]}" if t.scope == "thread" else f"agent {agent_id}")[:80]
        net = ("off", []) if t.external else (cfg.internet, cfg.allow_list)             # an external sender's box never gets Internet
        kw = dict(name=name, size=cfg.size, scope=t.scope, agent_id=agent_id, thread_id=t.thread, user_id=t.user, idle_ttl_s=cfg.idle_ttl_seconds,
                  delete_after_stop_s=cfg.delete_after_stop_seconds if t.scope == "thread" else None, internet=net[0], allow_list=net[1],
                  ro_mounts=_skill_mounts(agent_id))      # 11.64: the agent's skills, read-only at /memories/skills (set at create; later changes arrive through the symlink swap)
        ref, baked = await start_image(comp, t.ctx, agent_id, t.external)
        r = await images().create_box(t.ctx, ref, **kw) if ref else await comp.create(t.ctx, **kw)      # 11.47a: the agent's baked setup image / the workspace's `default` snapshot
        if cfg.setup_sh.strip() and not baked: await _setup(comp, t.ctx, r["id"], cfg.setup_sh)      # a baked image already holds what setup.sh installs
        return r

async def start_image(comp, ctx, agent_id, external):
    """What a NEW box starts from -> (snapshot ref | None, is_bake). 1) the agent's baked `setup.sh` image (agent-<identifier>--setup), 2) the workspace's snapshot named `default`
    (a whole machine incl. /workspace files: never for an external sender, who only gets the tools of the agent's own image), 3) the plain computer image."""
    imgs = images()
    ident = sbx_images.safe_ident(await comp.sys.agent_identifier(agent_id), agent_id)
    for ref, baked in ((f"agent-{ident}--setup", True), (None if external else sbx_images.DEFAULT_NAME, False)):
        if not ref: continue
        try: snap = await imgs.get(ctx, ref)
        except R.NotFound: continue
        if snap.get("status") == "ready": return ref, baked
    return None, False

async def _setup(comp, ctx, box, script):
    """The owner's setup.sh: stored where aos-boot re-runs it at every start, and run once now (its output goes to boot.log). A failure never blocks the box."""
    try:
        await comp.put_file(ctx, box, SETUP_PATH, script.encode())
        await comp.exec(ctx, box, f"AOS_BOOT=1 timeout -s KILL 120 bash -l {SETUP_PATH} >>/workspace/.aos/boot.log 2>&1", timeout=130)
    except R.SbxError as e: _log.warning("setup.sh of box %s failed: %s", box, str(e)[:200])

def _err(e: Exception) -> str:
    ra = getattr(e, "retry_after", None)
    return "Error: " + str(e)[:400] + (f" (try again in {ra} s)" if ra else "")

def _file_error(e: Exception) -> str:
    m = str(e)
    if isinstance(e, R.NotFound) and "no such file" in m: return FILE_NOT_FOUND
    if isinstance(e, R.SbxError) and m.startswith("bad path"): return INVALID_PATH
    if isinstance(e, R.Forbidden): return PERMISSION_DENIED
    return m[:300]

# ---------------------------------------------------------------- handle for other code (11.56 / 11.58 attachments)
@dataclass
class Box:
    id: str
    ctx: dict
    comp: object
    row: dict
    async def put(self, path, data: bytes): return await self.comp.put_file(self.ctx, self.id, path, data)
    async def get(self, path) -> bytes: return await self.comp.get_file(self.ctx, self.id, path)
    async def exec(self, cmd, **kw): return await self.comp.exec(self.ctx, self.id, cmd, **kw)

_BY_AGENT: "weakref.WeakValueDictionary" = weakref.WeakValueDictionary()   # agent id -> its AgentOSSandbox while the built agent is cached

async def for_config(config=None, *, create=True):
    """The caller's box for this run config (used by 11.56 Gmail / 11.58 attachments): `Box` or None when the agent of this thread has no computer."""
    from langgraph.config import get_config
    if config is None:
        try: config = get_config()
        except RuntimeError: return None
    m = re.match(r"^a(\d+):", str(((config or {}).get("configurable") or {}).get("thread_id") or ""))
    sbx = _BY_AGENT.get(int(m.group(1))) if m else None
    return await sbx.box(config, create=create) if sbx else None

# ---------------------------------------------------------------- helper scripts of deepagents must not run as "the model's command"
def _internal_async(fn):
    @functools.wraps(fn)
    async def w(self, *a, **k):
        tok = _INTERNAL.set(True)
        try: return await fn(self, *a, **k)
        finally: _INTERNAL.reset(tok)
    return w

def _internal_sync(fn):
    @functools.wraps(fn)
    def w(self, *a, **k):
        tok = _INTERNAL.set(True)
        try: return fn(self, *a, **k)
        finally: _INTERNAL.reset(tok)
    return w

class AgentOSSandbox(BaseSandbox):
    grep_exclude_dirs = (".git", "node_modules", ".aos")

    def __init__(self, agent_id: int, workspace_id: int, cfg: ComputerCfg, computers_=None):
        self.agent_id, self.workspace_id, self.cfg, self._comp = int(agent_id), int(workspace_id), cfg, computers_
        self._loop, self._sent, self._pub = None, {}, {}
        self.skills_dir = None          # 11.64: set by wire() (host folder mounted read-only at /memories/skills)
        _BY_AGENT[self.agent_id] = self

    @property
    def comp(self): return self._comp or computers()

    @property
    def id(self) -> str: return f"agentos-agent-{self.agent_id}"

    # ---- resolution
    async def _resolve(self, create=True, config=None):
        from langgraph.config import get_config
        self._loop = asyncio.get_running_loop()
        if config is None:
            try: config = get_config()
            except RuntimeError: raise R.SbxError("the virtual computer can only be used inside an agent run") from None
        t = await target_for(self.agent_id, self.workspace_id, self.cfg, config)
        row = await ensure_box(self.comp, self.agent_id, self.cfg, t, create=create)
        if row is not None: await self._push_uploads(t, row)
        return t, row

    async def box(self, config=None, create=True):
        """Handle of this run's box (created on demand). Raises R.SbxError when the caller may not use it."""
        t, row = await self._resolve(create, config)
        return Box(row["id"], t.ctx, self.comp, row) if row else None

    async def _push_uploads(self, t: Target, row):
        """Attachments of the chat thread -> /workspace/uploads/<name> (once per file per box and process). Best effort: never blocks the call."""
        try:
            items = await _thread_uploads(t.plain_thread)
            done = self._sent.setdefault(row["id"], set())
            if len(self._sent) > 500: self._sent.clear()
            for fid, name, load in items:
                if fid in done: continue
                await self.comp.put_file(t.ctx, row["id"], f"{UPLOAD_DIR}/{name}", await load())
                done.add(fid)
        except Exception as e:  # noqa: BLE001
            _log.warning("uploads of thread %s not copied: %s", t.plain_thread[:20], str(e)[:200])

    # ---- execute
    async def _exec(self, command, timeout, internal) -> ExecuteResponse:
        if timeout is None or timeout <= 0: timeout = EXEC_DEFAULT_S
        timeout = max(1, min(int(timeout), EXEC_MAX_S))
        try:
            t, row = await self._resolve()
            res = await self.comp.exec(t.ctx, row["id"], command, timeout=timeout, record=not internal)
            out, cid = res["output"], res["command_id"]
            if res["truncated"] and internal:
                try: out = (await self.comp.get_file(t.ctx, row["id"], f"/workspace/.aos/out/{cid}.log"))[:HELPER_LOG_MAX].decode(errors="replace")
                except R.SbxError: pass
                else: return ExecuteResponse(output=out, exit_code=res["exit_code"], truncated=False)
            if res["truncated"]: out += f"\n[output cut at {R.OUT_CAP // 1000} KB; the full log is /workspace/.aos/out/{cid}.log - read it with read_file or grep]"
            if res["timed_out"]: out += f"\n[timed out after {timeout} s and was stopped]"
            return ExecuteResponse(output=out, exit_code=res["exit_code"], truncated=bool(res["truncated"]))
        except R.SbxError as e:
            return ExecuteResponse(output=_err(e), exit_code=1)

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:  # noqa: ASYNC109
        return await self._exec(command, timeout, _INTERNAL.get())

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        return self._sync(self._exec(command, timeout, _INTERNAL.get()))

    # ---- files
    async def aupload_files(self, files):
        try: t, row = await self._resolve()
        except R.SbxError as e: return [FileUploadResponse(path=p, error=_err(e)) for p, _ in files]
        out = []
        for p, data in files:
            try: await self.comp.put_file(t.ctx, row["id"], p, data); out.append(FileUploadResponse(path=p))
            except R.SbxError as e: out.append(FileUploadResponse(path=p, error=_file_error(e)))
        return out

    async def adownload_files(self, paths):
        try: t, row = await self._resolve()
        except R.SbxError as e: return [FileDownloadResponse(path=p, error=_err(e)) for p in paths]
        out = []
        for p in paths:
            try: out.append(FileDownloadResponse(path=p, content=await self.comp.get_file(t.ctx, row["id"], p)))
            except R.SbxError as e: out.append(FileDownloadResponse(path=p, error=_file_error(e)))
        return out

    def upload_files(self, files): return self._sync(self.aupload_files(files))
    def download_files(self, paths): return self._sync(self.adownload_files(paths))

    async def adelete(self, file_path: str) -> DeleteResult:
        p = posixpath.normpath(file_path) if isinstance(file_path, str) and file_path.startswith("/") else None
        if p is None or p in PROTECTED: return DeleteResult(error=f"Error: '{file_path}' cannot be deleted")
        q = shlex.quote(p)
        tok = _INTERNAL.set(True)
        try:
            ex = await self.aexecute(f"test -e {q} || test -L {q}")
            if ex.exit_code is not None and ex.exit_code != 0: return DeleteResult(error=f"Error: '{file_path}' not found" if not ex.output.startswith("Error:") else ex.output)
            r = await self.aexecute(f"rm -rf -- {q}")
        finally: _INTERNAL.reset(tok)
        return DeleteResult(path=file_path) if r.exit_code == 0 else DeleteResult(error=f"Error deleting file '{file_path}': {r.output.strip() or 'unknown error'}")

    def delete(self, file_path: str) -> DeleteResult: return self._sync(self.adelete(file_path))

    # ---- helper scripts: internal (never cut at 100 KB)
    als, aread, awrite, aedit, agrep, aglob = (_internal_async(getattr(BaseSandbox, n)) for n in ("als", "aread", "awrite", "aedit", "agrep", "aglob"))
    ls, read, write, edit, grep, glob = (_internal_sync(getattr(BaseSandbox, n)) for n in ("ls", "read", "write", "edit", "grep", "glob"))

    # ---- sync entry points (deepagents' sync tools): only from a thread other than the loop that serves the agent
    def _sync(self, coro):
        try: asyncio.get_running_loop()
        except RuntimeError: running = False
        else: running = True
        if running:
            coro.close(); raise RuntimeError("AgentOSSandbox: use the async methods inside the event loop")
        loop = self._loop
        if loop is not None and loop.is_running(): return asyncio.run_coroutine_threadsafe(coro, loop).result(EXEC_MAX_S + 60)
        return asyncio.run(coro)

    # ---- /workspace/out -> chat Files panel
    async def publish_out(self) -> list:
        """New or changed files under /workspace/out since the last look -> stored like uploads (chat_files) -> [{id,name,size,mime_type | error}]. Never creates a box."""
        try: t, row = await self._resolve(create=False)
        except R.SbxError: return []
        if row is None or row["status"] == "stopped": return []
        comp, bid = self.comp, row["id"]
        try:
            res = await comp.exec(t.ctx, bid, SCAN_OUT, timeout=20)
            seen = {}
            for line in res["output"].splitlines():
                p = line.split("\t")
                if len(p) == 3 and p[0] and ".." not in p[0].split("/"):
                    try: seen[p[0]] = [int(p[1]), round(float(p[2]), 2)]
                    except ValueError: pass
            if not seen: return []
            if bid not in self._pub:
                try: self._pub[bid] = json.loads((await comp.get_file(t.ctx, bid, PUBLISHED)).decode())
                except (R.SbxError, ValueError): self._pub[bid] = {}
                if len(self._pub) > 200: self._pub = {bid: self._pub[bid]}
            pub = self._pub[bid]
            import base64, chat_gd5b as G
            new, items = [k for k, v in seen.items() if pub.get(k) != v], []
            for rel in new[:G.MAX_OUT_FILES]:
                pub[rel] = seen[rel]
                if seen[rel][0] > G.MAX_OUT_FILE_BYTES or seen[rel][0] == 0: continue
                items.append({"path": rel, "b64": base64.b64encode(await comp.get_file(t.ctx, bid, f"{OUT_DIR}/{rel}")).decode()})
            if new: await comp.put_file(t.ctx, bid, PUBLISHED, json.dumps(pub).encode())
            return await G._store_out_files(items) if items else []
        except R.SbxError as e:
            _log.warning("out files of box %s not published: %s", bid, str(e)[:200]); return []

class OutFilesMiddleware(AgentMiddleware):
    """After `execute` / write_file / edit_file / task: files that appeared in /workspace/out are added to the tool result as `[aos-files]` (the chat shows file cards)."""
    def __init__(self, sandbox: AgentOSSandbox):
        super().__init__(); self.sbx = sandbox
    async def awrap_tool_call(self, request, handler):
        res = await handler(request)
        if request.tool_call.get("name") in WATCH_TOOLS and isinstance(res, ToolMessage) and isinstance(res.content, str) and res.status != "error":
            try: files = await self.sbx.publish_out()
            except Exception as e:  # noqa: BLE001
                _log.warning("publish_out failed: %s", str(e)[:200]); return res
            if files:
                import chat_gd5b
                return res.model_copy(update={"content": res.content + "\n" + chat_gd5b.files_line(files)})
        return res

async def _thread_uploads(thread_id):
    """-> [(file_id, safe_name, async loader)] of the files attached to this chat thread (RLS: only what the caller may see)."""
    import attachments, deepagent_api as A, docfiles as D
    rows = await A.q("SELECT f.id, f.enc, f.sha256, f.name, f.kind FROM chat_thread_files t JOIN chat_files f ON f.id=t.file_id WHERE t.thread_id=%s ORDER BY f.created_at", (thread_id,))
    out, used = [], set()
    for r in rows:
        n = base = D.safe_name(r["name"], "file." + str(r.get("kind") or "bin")); i = 1
        while n in used: i += 1; n = f"{i}_{base}"
        used.add(n)
        out.append((str(r["id"]), n, functools.partial(asyncio.to_thread, attachments._bytes, r)))
    return out

# ---------------------------------------------------------------- the virtual folders (skills / memory / pack) stay where they are, next to the box
class StateView(StateBackend):
    """Read-only window on the graph-state virtual files below `prefix` (today's /skills/main/..., /memory/..., and an empty /pack/ when the agent has no pack).
    CompositeBackend strips the route prefix before calling a route, StateBackend stores the files WITH it: this puts it back (and takes it off the answers)."""
    def __init__(self, prefix: str):
        super().__init__(); self._p = prefix.rstrip("/")
    def _in(self, p): return self._p + (p if p and p.startswith("/") else "/" + (p or ""))
    def _out(self, p): return p[len(self._p):] or "/" if isinstance(p, str) and p.startswith(self._p) else p
    def _infos(self, xs): return [{**x, "path": self._out(x["path"])} for x in xs or []]
    def ls(self, path): r = super().ls(self._in(path)); return LsResult(entries=self._infos(r.entries), error=r.error) if r.entries is not None else r
    def read(self, file_path, offset=0, limit=2000): return super().read(self._in(file_path), offset, limit)
    def glob(self, pattern, path=None): r = super().glob(pattern, self._in(path or "/")); return GlobResult(matches=self._infos(r.matches), truncated=r.truncated, truncation_reason=r.truncation_reason, error=r.error)
    def grep(self, pattern, path=None, glob=None, *, max_count=None):
        r = super().grep(pattern, self._in(path or "/"), glob, max_count=max_count)
        return GrepResult(matches=[{**m, "path": self._out(m["path"])} for m in r.matches] if r.matches is not None else None, error=r.error, truncated=r.truncated)
    def download_files(self, paths): return [FileDownloadResponse(path=p, content=r.content, error=r.error) for p, r in zip(paths, super().download_files([self._in(p) for p in paths]), strict=True)]
    RO = "Error: read-only folder"
    def write(self, file_path, content): return WriteResult(error=self.RO)
    def edit(self, file_path, old_string, new_string, replace_all=False): return EditResult(error=self.RO)   # noqa: FBT002
    def delete(self, file_path): return DeleteResult(error=self.RO)
    def upload_files(self, files): return [FileUploadResponse(path=p, error=self.RO) for p, _ in files]

def wire(agent_id: int, workspace_id: int, ccfg: ComputerCfg, pack=None, computers_=None, extra_routes=None, skills=None) -> dict:
    """Everything build_agent needs when `computer.mode` is not off:
    backend      CompositeBackend(default=AgentOSSandbox, routes /pack/ /skills/ /memory/) - the routes keep today's virtual folders readable
    permissions  writes to /pack /skills /memory always denied, then the agent's fs_rules (Deep Agents only accepts rules for routed paths on a backend with a shell)
    interrupt    {"execute": ...} when approve_shell
    middleware   OutFilesMiddleware
    sandbox      the AgentOSSandbox (tests, for_config)
    skills       (agent folder, hash folder, names) from sbx_skills.prepare (11.64): route /memories/skills/ (read-only) + the same folder bind-mounted read-only in the box"""
    sbx = AgentOSSandbox(agent_id, workspace_id, ccfg, computers_)
    routes = {"/pack/": pack.backend if pack is not None else StateView("/pack"), "/skills/": StateView("/skills"), "/memory/": StateView("/memory"), **(extra_routes or {}), **({SK.MOUNT + "/": SK.RoHostBackend(skills[1])} if skills else {})}   # extra_routes: /memories/user/ /memories/agent/ (11.70)
    perms = [FilesystemPermission(operations=["write"], paths=["/pack/**", "/skills/**", "/memory/**"] + ([SK.MOUNT + "/**"] if skills else []), mode="deny")]   # only routed paths may carry rules (deepagents)
    if skills: sbx.skills_dir = skills[0]
    perms += [FilesystemPermission(operations=list(r.operations), paths=list(r.paths), mode=r.mode) for r in ccfg.fs_rules]
    return {"backend": CompositeBackend(default=sbx, routes=routes), "permissions": perms, "sandbox": sbx,
            "interrupt": {"execute": {"allowed_decisions": ["approve", "edit", "reject"]}} if ccfg.approve_shell else None,
            "middleware": [OutFilesMiddleware(sbx)]}
