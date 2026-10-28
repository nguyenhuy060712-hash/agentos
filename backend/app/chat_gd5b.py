"""GĐ5b (21/10/2026) /chat additions, own module (additive tables / columns only):

  Canvas (assistant-ui "Artifacts" pattern: a tool call renders inline, the document opens in a side panel)
    tools `canvas_ghi` (create / new version of a document or code file) and `canvas_doc` (read the latest version, incl. user edits)
    GET  /api/chat/canvas?thread_id=            documents of a thread (latest version of each)
    GET  /api/chat/canvas/{doc}                 versions (meta) + latest content;  GET .../v/{n}  one version
    POST /api/chat/canvas/{doc} {content,base_version,title?}  user edit = new version (editor+; 409 if someone saved meanwhile)
  Code run (existing gVisor sandbox: docker --runtime runsc, no network, read-only rootfs, non-root, CPU/RAM/PID/time limits)
    tool `chay_code_python` (replaces the text-only one): stdout + matplotlib charts (PNG -> /api/files) back into the chat
    POST /api/chat/code/run {code,thread_id?}  run from the canvas (role-gated, rate-limited, 2 concurrent per process)
    GET/PUT /api/chat/code/settings            admin: enabled + roles allowed to run code
  Schedules (agent_crons): GET/POST /api/chat/schedules, PATCH/DELETE /api/chat/schedules/{id}; locked rows (P0-1 test crons)
    can never be re-enabled, run or deleted (also enforced in studio.py + run_crons)
  Read-only share links: POST/GET /api/chat/shares, DELETE /api/chat/shares/{id} (revoke), GET /api/chat/share/{token}
    token = <id>.<secret>, only sha256(secret) is stored; expiry 1–30 days; readers must be signed in to the same workspace (no domain
    yet -> no public links); the view carries message text + render-only `present` UI only: no tool args/results, no files, redacted.
  Fixes: /favicon.ico (login page), @mention delegation forwards image attachments (chat_gd5.delegate)."""
from __future__ import annotations
import asyncio, base64, datetime as dt, hashlib, hmac, json, logging, os, re, secrets, time, uuid
from typing import Literal
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb
from langchain_core.tools import tool as lc_tool
from langchain_core.runnables import RunnableConfig
import admin as _adm
import deepagent_api as A
import chat_gd5 as G5

_log = logging.getLogger("uvicorn.error")
ROLES = G5.ROLES
_READY = {"ok": False}
DDL = r"""
CREATE TABLE IF NOT EXISTS chat_canvas(doc_id text NOT NULL, version int NOT NULL,
  thread_id text NOT NULL REFERENCES lg_threads(thread_id) ON DELETE CASCADE, workspace_id int NOT NULL DEFAULT 1,
  title text NOT NULL, kind text NOT NULL, language text NOT NULL DEFAULT '', content text NOT NULL,
  author text NOT NULL, author_id int, note text NOT NULL DEFAULT '', created timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(doc_id, version));
CREATE INDEX IF NOT EXISTS chat_canvas_thread ON chat_canvas(thread_id);
CREATE TABLE IF NOT EXISTS chat_shares(id text PRIMARY KEY, thread_id text NOT NULL REFERENCES lg_threads(thread_id) ON DELETE CASCADE,
  workspace_id int NOT NULL, token_hash text NOT NULL UNIQUE, created_by int, created timestamptz NOT NULL DEFAULT now(),
  expires timestamptz NOT NULL, revoked timestamptz, views int NOT NULL DEFAULT 0, last_view timestamptz);
CREATE TABLE IF NOT EXISTS chat_code_runs(id bigserial PRIMARY KEY, user_id int, workspace_id int, thread_id text, source text,
  ms int, exit_code int, status text, images int NOT NULL DEFAULT 0, created timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS chat_gd5b_settings(workspace_id int NOT NULL, key text NOT NULL, value jsonb NOT NULL,
  updated timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(workspace_id, key));
ALTER TABLE agent_crons ADD COLUMN IF NOT EXISTS locked_reason text;
ALTER TABLE agent_crons ADD COLUMN IF NOT EXISTS created_by int;
"""

async def ensure():
    if _READY["ok"]: return
    async with A.S["pool"].connection() as c:
        await c.execute("RESET ROLE")
        async with c.transaction():     # same lock as the boot DDL (api + worker)
            await c.execute("SELECT pg_advisory_xact_lock(hashtext('agentos_boot_ddl'))")
            await c.execute(DDL)
            for t in ("chat_canvas", "chat_shares", "chat_code_runs", "chat_gd5b_settings"):
                await c.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {t} TO aos_rls")
            await c.execute("GRANT USAGE, SELECT ON SEQUENCE chat_code_runs_id_seq TO aos_rls")
    _READY["ok"] = True

def _me(): return G5._me()
def _need(u, role): G5._need(u, role)
def _uid(): return (_adm.cur() or {}).get("id")
def _ws(): return _adm.cur_ws()

async def _system(coro):
    """Run a query batch in system scope (RLS off) in its own task context; callers check the workspace themselves."""
    async def run():
        _adm.USER.set(None); return await coro
    return await asyncio.get_running_loop().create_task(run())

def _tid_from(config) -> str | None:
    tid = ((config or {}).get("configurable") or {}).get("thread_id") or ""
    return tid.split(":", 1)[1] if ":" in tid else (tid or None)

# ============================================================== settings
DEFAULTS = {"code_run": {"enabled": True, "roles": ["admin", "editor"]}}
async def setting(key: str, ws: int | None = None) -> dict:
    await ensure()
    r = await A.q("SELECT value FROM chat_gd5b_settings WHERE workspace_id=%s AND key=%s", (ws or _ws(), key), one=True)
    return {**DEFAULTS[key], **((r or {}).get("value") or {})}

# ============================================================== canvas
MAX_DOC = 200_000
class CanvasEdit(BaseModel):
    content: str = Field(max_length=MAX_DOC)
    base_version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=120)

def _lang(kind: str, language: str) -> str:
    l = re.sub(r"[^a-z0-9+#.-]", "", (language or "").lower())[:20]
    return l or ("markdown" if kind == "document" else "text")

async def canvas_save(tid: str, *, doc_id: str | None, title: str, content: str, kind: str, language: str, author: str, author_id=None,
                      note: str = "", base_version: int | None = None, new_if_missing: bool = False) -> dict:
    await ensure()
    if len(content) > MAX_DOC: raise ValueError(f"tài liệu quá dài ({len(content)} > {MAX_DOC} ký tự)")
    t = await A.q("SELECT thread_id, workspace_id FROM lg_threads WHERE thread_id=%s", (tid,), one=True)
    if not t: raise ValueError("không có thread")
    async with A.S["pool"].connection() as c:
        await c.execute("RESET ROLE")
        async with c.transaction():
            cur = None
            if doc_id:
                await c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("canvas:" + doc_id,))
                cur = await (await c.execute("SELECT version, title, kind, language, thread_id FROM chat_canvas WHERE doc_id=%s ORDER BY version DESC LIMIT 1", (doc_id,))).fetchone()
                if not cur and new_if_missing: doc_id = None   # GĐ7.R: the model invented an id for a NEW document (real Gemini lost a whole research report this way)
            if doc_id:
                if not cur or cur["thread_id"] != tid: raise ValueError("không có tài liệu này trong cuộc trò chuyện")
                if base_version is not None and base_version != cur["version"]: raise LookupError(f"đã có bản v{cur['version']} mới hơn")
                if cur["version"] >= 200: raise ValueError("tài liệu đã có 200 phiên bản")
                v = cur["version"] + 1; title = (title or cur["title"]); kind = cur["kind"]; language = language or cur["language"]
            else:
                doc_id = uuid.uuid4().hex[:12]; v = 1
            await c.execute("""INSERT INTO chat_canvas(doc_id,version,thread_id,workspace_id,title,kind,language,content,author,author_id,note)
                               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (doc_id, v, tid, t["workspace_id"], (title or "Tài liệu")[:120], kind, _lang(kind, language), content, author, author_id, note[:200]))
    return {"doc_id": doc_id, "version": v, "title": (title or "Tài liệu")[:120], "kind": kind}

async def _canvas_latest(doc_id: str):
    return await A.q("SELECT * FROM chat_canvas WHERE doc_id=%s ORDER BY version DESC LIMIT 1", (doc_id,), one=True)

def canvas_tools() -> dict:
    @lc_tool
    async def canvas(tieu_de: str, noi_dung: str, loai: Literal["document", "code"] = "document", ngon_ngu: str = "",
                         doc_id: str = "", ghi_chu: str = "", config: RunnableConfig = None) -> str:
        """Mở/ghi một tài liệu hoặc file code trong CANVAS (khung bên cạnh chat, người dùng xem, sửa, so sánh phiên bản, tải về).
        Dùng khi kết quả là văn bản dài (bài viết, kế hoạch, email, báo cáo > ~15 dòng) hoặc code hoàn chỉnh. Nội dung đầy đủ, KHÔNG lặp lại toàn văn trong câu trả lời.
        loai: document (markdown) | code; ngon_ngu: vd python, sql, html (với code). Sửa tài liệu đã có: truyền doc_id để tạo phiên bản mới
        (gọi canvas_doc trước để lấy bản mới nhất, vì người dùng có thể đã sửa). ghi_chu: tóm tắt thay đổi (ngắn)."""
        tid = _tid_from(config)
        if not tid: return "Không lưu được canvas (thiếu cuộc trò chuyện)."
        try:
            r = await canvas_save(tid, doc_id=doc_id or None, title=tieu_de, content=noi_dung, kind=loai if loai in ("document", "code") else "document",
                                  language=ngon_ngu, author="agent", note=ghi_chu, new_if_missing=True)
        except (ValueError, LookupError) as e: return f"Không lưu được canvas: {e}."
        return f"Đã mở canvas «{r['title']}» bản v{r['version']} cho người dùng. Trả lời 1–2 câu tóm tắt, không chép lại nội dung. [canvas:{r['doc_id']}:{r['version']}]"

    @lc_tool
    async def canvas_doc(doc_id: str, config: RunnableConfig = None) -> str:
        """Đọc bản mới nhất của một tài liệu canvas (kể cả chỗ người dùng tự sửa) trước khi sửa tiếp."""
        tid = _tid_from(config); d = await _canvas_latest(doc_id.strip())
        if not d or d["thread_id"] != tid: return "Không có tài liệu này trong cuộc trò chuyện."
        body = d["content"]; cut = len(body) > 30_000
        return f"«{d['title']}» v{d['version']} ({'người dùng sửa' if d['author'] == 'user' else 'agent viết'}), {d['kind']} {d['language']}:\n{body[:30_000]}" + ("\n[…cắt bớt]" if cut else "")
    return {"local:canvas": canvas, "local:canvas_doc": canvas_doc}

# ============================================================== code run (gVisor sandbox)
SBX_DATA_IMAGE = os.environ.get("AOS_SBX_DATA_IMAGE", "")       # e.g. agentos-sbx-py:20261021 (numpy, pandas, matplotlib); else python alpine
CODE_TIMEOUT = int(os.environ.get("AOS_CODE_TIMEOUT", "30")); MAX_IMAGES = 4; MAX_IMAGE_BYTES = 2_000_000
RUN_LIMIT = (30, 600)                                              # per user: 30 runs / 10 min
# GĐ8.6 file skills (app/skills/{docx,xlsx,pptx,pdf}): helpers mounted read-only at /memories/skills (GĐ11.64: same path as in the virtual computer); a run that uses them gets 60 s
# (other runs keep CODE_TIMEOUT; RAM is 512 MB for every chat run); files the code writes to /workspace/out come back as chat files
# (download cards in the chat).
SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills"); SKILL_MOUNT = "/memories/skills"
SKILL_TIMEOUT = int(os.environ.get("AOS_SKILL_TIMEOUT", "60")); SKILL_MEMORY_MB = 512
MAX_OUT_FILES, MAX_OUT_FILE_BYTES, MAX_OUT_BYTES = 8, 10_000_000, 20_000_000
_SEM = asyncio.Semaphore(int(os.environ.get("AOS_CODE_CONCURRENCY", "2")))
MARK = "\x1eAOS_RESULT\x1e"
WRAPPER = r'''
import sys, io, json, base64, traceback, contextlib, os
os.makedirs("/tmp/mpl", exist_ok=True)
try: os.makedirs("/workspace/out", exist_ok=True)
except OSError: pass
code = sys.stdin.read()
out, err = io.StringIO(), io.StringIO()
real = sys.stdout
class Cap(io.StringIO):
    def write(self, s):
        if self.tell() < 40000: return super().write(s[:40000])
        return len(s)
o, e = Cap(), Cap()
import signal
def _late(*_):   # time limit inside the sandbox: report what was printed so far, then stop (the host kill stays as a backstop)
    real.write("__MARK__" + json.dumps({"ok": False, "timeout": True, "stdout": o.getvalue(), "stderr": e.getvalue(), "images": []})); real.flush(); os._exit(0)
signal.signal(signal.SIGALRM, _late); signal.alarm(int(os.environ.get("AOS_T", "30")))
g = {"__name__": "__main__"}
ok = True
with contextlib.redirect_stdout(o), contextlib.redirect_stderr(e):
    try: exec(compile(code, "<code>", "exec"), g)
    except SystemExit: pass
    except BaseException as x: ok = False; traceback.print_exception(type(x), x, x.__traceback__.tb_next)   # skip the wrapper's own frame
imgs = []
if "matplotlib" in sys.modules:
    try:
        import matplotlib.pyplot as plt
        for n in plt.get_fignums()[:4]:
            b = io.BytesIO(); plt.figure(n).savefig(b, format="png", dpi=110, bbox_inches="tight"); imgs.append(base64.b64encode(b.getvalue()).decode())
    except BaseException as x: e.write("\n[không lưu được biểu đồ: %s]" % x)
files, tot = [], 0
for root, _, fs in os.walk("/workspace/out"):
    for f in sorted(fs):
        p = os.path.join(root, f)
        try: sz = os.path.getsize(p)
        except OSError: continue
        if len(files) >= __NF__ or sz > __FB__ or tot + sz > __TB__: e.write("\n[bỏ qua tệp %s: quá giới hạn số lượng / dung lượng]" % f); continue
        with open(p, "rb") as fh: files.append({"path": os.path.relpath(p, "/workspace/out"), "b64": base64.b64encode(fh.read()).decode()})
        tot += sz
real.write("__MARK__" + json.dumps({"ok": ok, "stdout": o.getvalue(), "stderr": e.getvalue(), "images": imgs, "files": files}))
'''.replace("__MARK__", MARK).replace("__NF__", str(MAX_OUT_FILES)).replace("__FB__", str(MAX_OUT_FILE_BYTES)).replace("__TB__", str(MAX_OUT_BYTES))

async def _docker(code: str, timeout: int, files=()) -> tuple[int, str, str]:
    name = "moza-code-" + uuid.uuid4().hex[:10]
    image = SBX_DATA_IMAGE or A.SANDBOX_IMAGE
    cmd = ["docker", "run", "--rm", "-i", "--name", name, *A.SBX_RT, "--network", "none", "--memory", "512m", "--memory-swap", "512m",
           "--cpus", "1", "--pids-limit", "128", "--read-only", "--tmpfs", "/tmp:rw,size=64m,noexec", "--tmpfs", "/workspace:rw,size=64m,noexec,mode=1777", "--cap-drop", "ALL",
           "--security-opt", "no-new-privileges", "--user", "65534:65534", "--workdir", "/tmp", *[x for h, n in files if ":" not in h and "," not in h for x in ("-v", f"{h}:/tmp/{n}:ro")],
           *(["-v", f"{SKILLS_DIR}:{SKILL_MOUNT}:ro"] if os.path.isdir(SKILLS_DIR) and ":" not in SKILLS_DIR and "," not in SKILLS_DIR else []),
           "-e", f"AOS_T={timeout}", "-e", "MPLBACKEND=Agg", "-e", "MPLCONFIGDIR=/tmp/mpl", "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1",
           image, "python", "-I", "-c", WRAPPER]
    p = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(code.encode()[:100_000]), timeout + 15)   # + container start-up
        return p.returncode, out.decode(errors="replace"), err.decode(errors="replace")
    except asyncio.TimeoutError:
        await (await asyncio.create_subprocess_exec("docker", "kill", name, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)).wait()
        return 124, "", "timeout"

RUNNER = {"fn": _docker}     # tests swap in a fake runner (no Docker on the dev box)

def runtime_label() -> str:
    """The runner that REALLY executes code: fake (test / local runner) | docker (runc) | gvisor (runsc). GĐ9.P1: Settings showed "runc (docker default)" in fake mode."""
    if RUNNER["fn"] is not _docker: return "fake (local test runner, no Docker)"
    return A.SBX_INFO.get("runtime") or "docker"

async def run_code(code: str, *, source: str, thread_id: str | None = None, ws: int | None = None, user_id=None, files=()) -> dict:
    """-> {status, exit_code, ms, stdout, stderr, images:[{id,url}], runtime}"""
    await ensure()
    if not code.strip(): raise ValueError("chưa có code")
    if len(code) > 100_000: raise ValueError("code quá dài (> 100 000 ký tự)")
    tmo = SKILL_TIMEOUT if uses_skill(code) else CODE_TIMEOUT
    t0 = time.perf_counter()
    async with _SEM:
        rc, out, err = await (RUNNER["fn"](code, tmo, files) if files else RUNNER["fn"](code, tmo))
    ms = int((time.perf_counter() - t0) * 1000)
    res = {"status": "ok", "exit_code": rc, "ms": ms, "stdout": "", "stderr": "", "images": [], "files": [], "runtime": runtime_label()}
    if rc == 124 and err == "timeout":
        res.update(status="timeout", stderr=f"Hết giờ sau {tmo} s, container đã bị huỷ.")
    elif MARK in out:
        pre, _, js = out.partition(MARK)
        try: d = json.loads(js)
        except ValueError: d = {"ok": False, "stdout": pre, "stderr": "kết quả không đọc được", "images": []}
        res.update(stdout=(pre + d.get("stdout", ""))[:20_000], stderr=str(d.get("stderr", ""))[:8_000], status="ok" if d.get("ok") else "error")
        if d.get("timeout"): res.update(status="timeout", exit_code=124, stderr=(res["stderr"] + f"\nHết giờ sau {tmo} s, đã dừng.").strip())
        import attachments
        for b64 in (d.get("images") or [])[:MAX_IMAGES]:
            try:
                data = base64.b64decode(b64, validate=True)
                if len(data) > MAX_IMAGE_BYTES or not data.startswith(b"\x89PNG"): continue
                f = await attachments.store(data, "bieu-do.png", "image/png"); res["images"].append({"id": f["id"], "url": f["url"]})
            except Exception as e:  # noqa: BLE001
                _log.warning("code image store failed: %s", e)
        res["files"] = await _store_out_files(d.get("files") or [])
    else:   # the interpreter died before the wrapper could answer (OOM kill, pids limit, …)
        res.update(status="error", stdout=out[:20_000], stderr=(err or f"exit {rc}")[:8_000] + ("\n(có thể vượt giới hạn 512 MB RAM)" if rc == 137 else ""))
    try:
        await A.q("INSERT INTO chat_code_runs(user_id,workspace_id,thread_id,source,ms,exit_code,status,images) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                  (user_id, ws or 1, thread_id, source, ms, rc, res["status"], len(res["images"])))
    except Exception as e:  # noqa: BLE001
        _log.warning("chat_code_runs insert failed: %s", e)
    return res

def uses_skill(code: str) -> bool:
    """A run that imports a file-skill helper (SKILL.md: sys.path.insert(0, "/memories/skills/<skill>/scripts")) gets SKILL_TIMEOUT."""
    return SKILL_MOUNT + "/" in code

async def _store_out_files(items: list) -> list:
    """Files the code wrote under /workspace/out -> chat_files (same store / limits / type checks as uploads). -> [{id,url,name,size,mime_type}]"""
    import attachments
    out = []
    for f in items[:MAX_OUT_FILES]:
        name = str(f.get("path") or "")[:200].lstrip("/")
        try:
            data = base64.b64decode(f.get("b64") or "", validate=True)
            if not data or len(data) > MAX_OUT_FILE_BYTES: continue
            m = await attachments.store(data, name)
            out.append({"id": m["id"], "url": m["url"], "name": name, "size": m["size"], "mime_type": m["mime_type"]})
        except HTTPException as e:
            out.append({"name": name, "error": str(e.detail)[:200]})
        except Exception as e:  # noqa: BLE001
            _log.warning("code out file store failed: %s", e)
    return out

def files_line(files: list) -> str:
    """Tool-text marker the chat turns into file cards (agentos-chat genui/gd5b-helpers.ts parseFiles); the model reads the names."""
    ok = [{k: f[k] for k in ("id", "name", "size", "mime_type")} for f in files if f.get("id")]
    bad = [f"{f['name']}: {f['error']}" for f in files if f.get("error")]
    lines = ([f"[aos-files] {json.dumps({'files': ok}, ensure_ascii=False)}"] if ok else []) + (["[tệp không lưu được] " + "; ".join(bad)] if bad else [])
    return "\n".join(lines)

async def _code_allowed(u) -> None:
    s = await setting("code_run")
    if not s.get("enabled"): raise HTTPException(403, "Chạy code đang tắt (admin bật trong Cài đặt › Quyền)")
    if (u.get("role") or "viewer") not in (s.get("roles") or []): raise HTTPException(403, "Vai trò của anh/chị chưa được phép chạy code")
    n = await A.q("SELECT count(*) n FROM chat_code_runs WHERE user_id=%s AND created > now() - make_interval(secs => %s)", (u.get("id"), RUN_LIMIT[1]), one=True)
    if n and n["n"] >= RUN_LIMIT[0]: raise HTTPException(429, f"Tối đa {RUN_LIMIT[0]} lần chạy / {RUN_LIMIT[1] // 60} phút")

def code_tools() -> dict:
    @lc_tool
    async def chay_code_python(code: str, config: RunnableConfig = None) -> str:
        """Chạy code Python trong sandbox gVisor dùng-một-lần (không mạng, 512 MB RAM, 1 CPU, tối đa 30 s, rootfs chỉ đọc; có numpy, pandas,
        matplotlib nếu image dữ liệu đã cài). In kết quả bằng print(); biểu đồ matplotlib (plt.plot…) tự hiện cho người dùng, KHÔNG cần savefig/show.
        Tệp ghi vào /workspace/out/ được gửi cho người dùng (thẻ tải về, chỉ cần tóm tắt ngắn). Word/Excel/PowerPoint/PDF: làm theo skill docx/xlsx/pptx/pdf (60 s)."""
        tid = _tid_from(config)
        t = await A.q("SELECT workspace_id FROM lg_threads WHERE thread_id=%s", (tid,), one=True) if tid else None
        ws = (t or {}).get("workspace_id") or 1
        s = await setting("code_run", ws)
        if not s.get("enabled"): return "Chạy code đang bị admin tắt. Hãy trả lời không cần chạy code."
        u = _adm.cur()
        if u and (u.get("role") or "viewer") not in (s.get("roles") or []): return "Người dùng này chưa được phép chạy code. Hãy trả lời không cần chạy code."
        import attachments
        try: r = await run_code(code, source="agent", thread_id=tid, ws=ws, user_id=(u or {}).get("id"), files=await attachments.thread_data_files(tid or ""))
        except ValueError as e: return f"Không chạy được: {e}"
        lines = [f"status={r['status']} exit={r['exit_code']} ({r['ms']} ms, {r['runtime']})"]
        if r["stdout"]: lines.append("[stdout]\n" + r["stdout"][:6000])
        if r["stderr"]: lines.append("[stderr]\n" + r["stderr"][:3000])
        if r.get("files"): lines.append(files_line(r["files"]))
        if r["images"]: lines.append(f"[{len(r['images'])} biểu đồ đã hiện cho người dùng] [aos-images:{','.join(i['id'] for i in r['images'])}]")
        return "\n".join(lines)
    return {"local:chay_code_python": chay_code_python}

def tools(agent_id) -> dict:
    return {**canvas_tools(), **code_tools()}

class CodeIn(BaseModel):
    code: str = Field(min_length=1, max_length=100_000)
    thread_id: str | None = Field(default=None, max_length=100)

class CodeSettings(BaseModel):
    enabled: bool
    roles: list[Literal["admin", "editor", "viewer"]] = Field(default_factory=lambda: ["admin", "editor"])

# ============================================================== schedules (agent_crons)
TZ = os.environ.get("AOS_CRON_TZ", "Asia/Ho_Chi_Minh")
MAX_PER_DAY = 48
class SchedIn(BaseModel):
    agent_id: str
    name: str = Field(min_length=1, max_length=100)
    cron: str = Field(min_length=9, max_length=100)
    message: str = Field(min_length=1, max_length=4000)

class SchedPatch(BaseModel):
    enabled: bool

def _fires(expr: str, start: dt.datetime, minutes: int, stop_after: int | None = None) -> list[dt.datetime]:
    import evals
    out, t = [], start.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    for _ in range(minutes):
        if evals.cron_match(expr, t):
            out.append(t)
            if stop_after and len(out) >= stop_after: break
        t += dt.timedelta(minutes=1)
    return out

def cron_check(expr: str) -> None:
    import evals
    try: evals.cron_match(expr, dt.datetime.now())
    except Exception as e: raise HTTPException(400, f"Lịch cron không hợp lệ: {e}")  # noqa: BLE001,B904
    n = len(_fires(expr, dt.datetime(2026, 1, 5), 24 * 60))     # a Monday: worst-case runs per day
    if n > MAX_PER_DAY: raise HTTPException(400, f"Lịch chạy quá dày ({n} lần/ngày, tối đa {MAX_PER_DAY}), để tránh tốn chi phí")
    if not _fires(expr, dt.datetime.now(), 366 * 24 * 60, stop_after=1): raise HTTPException(400, "Lịch này không bao giờ chạy trong 1 năm tới")

def _row_out(c, u) -> dict:
    mine = c.get("created_by") is not None and c.get("created_by") == u.get("id")
    can = not c.get("locked_reason") and (u.get("role") == "admin" or (u.get("role") == "editor" and mine))
    nxt = None
    if c["enabled"] and not c.get("locked_reason"):
        f = _fires(c["cron"], dt.datetime.now(), 8 * 24 * 60, stop_after=1)
        nxt = f[0].isoformat() if f else None
    return {"id": c["id"], "agent_id": f"agent-{c['agent_id']}", "agent_name": c.get("agent") or f"#{c['agent_id']}", "name": c["name"], "cron": c["cron"],
            "message": c["message"], "enabled": c["enabled"], "locked": bool(c.get("locked_reason")), "locked_reason": c.get("locked_reason"),
            "last_run": c["last_run"], "last_status": c["last_status"], "next_run": nxt, "created_by": c.get("created_by"), "can_edit": can,
            "source": c.get("source") or "ui", "paused": bool(c.get("paused"))}   # GĐ13.28: source=agent -> "created by agent" tag

# ============================================================== share links
class ShareIn(BaseModel):
    thread_id: str = Field(min_length=8, max_length=100)
    days: int = Field(default=7, ge=1, le=30)

_ATT = re.compile(r'<attachment name="([^"]*)">[\s\S]*?(?:</attachment>|$)')
def _clean_text(s: str, rules) -> str:
    s = _ATT.sub(lambda m: f"[Tệp đính kèm: {m.group(1)[:80]}]", s or "")
    s = G5.plain_mentions(s)
    import obs
    return obs._redact_val(s, rules) if rules else s

async def share_view(msgs: list, rules) -> list:
    import genui
    out = []
    for m in msgs or []:
        if not isinstance(m, dict): continue
        t = m.get("type"); c = m.get("content")
        if t == "human":
            text = _clean_text(G5._content_text(c), rules)
            imgs = sum(1 for b in c if isinstance(b, dict) and b.get("type") in ("image", "image_url", "file")) if isinstance(c, list) else 0
            if imgs: text = (text + f"\n[{imgs} ảnh đính kèm]").strip()
            if text.strip(): out.append({"role": "user", "text": text})
        elif t == "ai":
            parts = []
            text = _clean_text(G5._content_text(c), rules)
            if text.strip(): parts.append({"type": "text", "text": text})
            for tc in m.get("tool_calls") or []:   # only the render-only generative UI; every other tool call (args, results) is dropped
                if tc.get("name") == "present" and isinstance(tc.get("args"), dict) and genui.check(tc["args"]) is None:
                    args = obs_safe(tc["args"], rules)
                    parts.append({"type": "present", "args": args})
                elif tc.get("name") == "canvas":
                    parts.append({"type": "note", "text": f"[Canvas: {str((tc.get('args') or {}).get('tieu_de', ''))[:80]}]"})
            ak = m.get("additional_kwargs") or {}
            who = ak.get("delegate_name") if ak.get("delegated_from") else None
            if parts: out.append({"role": "assistant", "parts": parts, **({"delegated_from": who} if who else {})})
    return out

def obs_safe(v, rules):
    import obs
    return obs._redact_val(v, rules) if rules else v

def _hash(secret: str) -> str: return hashlib.sha256(secret.encode()).hexdigest()

# ============================================================== router
def router():
    r = APIRouter()
    # login page: its own icon (the browser probed /favicon.ico -> 404 before)
    if "rel=icon" not in _adm.LOGIN_HTML:
        _adm.LOGIN_HTML = _adm.LOGIN_HTML.replace("<title>", '<link rel=icon href="/favicon.ico" type="image/svg+xml"><title>', 1)

    @r.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        import sys
        dist = next((getattr(sys.modules[m], "DIST") for m in ("voice_api", "chat_overlay") if m in sys.modules and hasattr(sys.modules[m], "DIST")), None)
        f = dist / "favicon.svg" if dist else None
        if not f or not f.is_file(): raise HTTPException(404)
        return Response(f.read_bytes(), media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"})

    # ---------------- canvas
    @r.get("/api/chat/canvas")
    async def canvas_list(thread_id: str):
        import server as SV
        _me(); await ensure(); t = await SV._get_thread(thread_id)
        rows = await A.q("""SELECT DISTINCT ON (doc_id) doc_id, version, title, kind, language, author, created FROM chat_canvas
                            WHERE thread_id=%s ORDER BY doc_id, version DESC""", (t["thread_id"],))
        return {"items": sorted(rows, key=lambda x: x["created"], reverse=True)}

    async def _doc_rows(doc_id: str):
        import server as SV
        rows = await A.q("SELECT doc_id, version, thread_id, title, kind, language, author, author_id, note, created, length(content) size FROM chat_canvas WHERE doc_id=%s ORDER BY version", (doc_id,))
        if not rows: raise HTTPException(404, "Không có tài liệu")
        await SV._get_thread(rows[0]["thread_id"])        # RLS: the caller's own thread
        return rows

    @r.get("/api/chat/canvas/{doc_id}")
    async def canvas_get(doc_id: str):
        u = _me(); await ensure(); rows = await _doc_rows(doc_id)
        last = await A.q("SELECT content FROM chat_canvas WHERE doc_id=%s AND version=%s", (doc_id, rows[-1]["version"]), one=True)
        return {"doc_id": doc_id, "thread_id": rows[0]["thread_id"], "title": rows[-1]["title"], "kind": rows[-1]["kind"], "language": rows[-1]["language"],
                "version": rows[-1]["version"], "content": last["content"], "versions": rows, "can_edit": ROLES.get(u.get("role"), 0) >= 2}

    @r.get("/api/chat/canvas/{doc_id}/pdf")
    async def canvas_pdf(doc_id: str, v: int | None = None):
        """GĐ7.7: export a canvas document (latest version, or ?v=N) as PDF. Same access check as canvas_get (the caller's own thread, RLS)."""
        import deep_research as DR
        from urllib.parse import quote
        _me(); await ensure(); rows = await _doc_rows(doc_id)
        d = await A.q("SELECT title, content, version FROM chat_canvas WHERE doc_id=%s AND version=%s", (doc_id, v or rows[-1]["version"]), one=True)
        if not d: raise HTTPException(404, "Không có phiên bản này")
        try: pdf = await asyncio.to_thread(DR.md_to_pdf, d["title"], d["content"])
        except Exception as e:  # noqa: BLE001
            _log.warning("canvas pdf failed: %s", e); raise HTTPException(500, "Không xuất được PDF") from None
        ascii_name, utf_name = DR.pdf_filename(d["title"])
        return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(utf_name)}",
                                                                   "X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-store"})

    @r.get("/api/chat/canvas/{doc_id}/v/{version}")
    async def canvas_version(doc_id: str, version: int):
        _me(); await ensure(); await _doc_rows(doc_id)
        v = await A.q("SELECT version, title, content, author, note, created FROM chat_canvas WHERE doc_id=%s AND version=%s", (doc_id, version), one=True)
        if not v: raise HTTPException(404, "Không có phiên bản này")
        return v

    @r.post("/api/chat/canvas/{doc_id}")
    async def canvas_edit(doc_id: str, b: CanvasEdit):
        u = _me(); _need(u, "editor"); await ensure(); rows = await _doc_rows(doc_id)
        try:
            return await canvas_save(rows[0]["thread_id"], doc_id=doc_id, title=b.title or "", content=b.content, kind=rows[-1]["kind"],
                                     language="", author="user", author_id=u.get("id"), note="người dùng sửa", base_version=b.base_version)
        except LookupError as e: raise HTTPException(409, f"Không lưu: {e}. Tải lại rồi sửa tiếp.")  # noqa: B904
        except ValueError as e: raise HTTPException(400, str(e))  # noqa: B904

    # ---------------- code run
    @r.post("/api/chat/code/run")
    async def code_run(b: CodeIn):
        import server as SV
        u = _me(); _need(u, "editor"); await ensure(); await _code_allowed(u)
        if b.thread_id: await SV._get_thread(b.thread_id)
        try: return await run_code(b.code, source="canvas", thread_id=b.thread_id, ws=_ws(), user_id=u.get("id"))
        except ValueError as e: raise HTTPException(400, str(e))  # noqa: B904

    @r.get("/api/chat/code/settings")
    async def code_settings():
        u = _me(); s = await setting("code_run")
        return {**s, "can_run": bool(s.get("enabled")) and (u.get("role") or "viewer") in (s.get("roles") or []), "is_admin": u.get("role") == "admin",
                "runtime": runtime_label(), "image": SBX_DATA_IMAGE or A.SANDBOX_IMAGE, "timeout_s": CODE_TIMEOUT, "skill_timeout_s": SKILL_TIMEOUT,
                "limits": {"memory_mb": 512, "cpus": 1, "pids": 128, "network": False, "runs_per_10min": RUN_LIMIT[0]}}

    @r.put("/api/chat/code/settings")
    async def code_settings_put(b: CodeSettings):
        u = _me(); _need(u, "admin"); await ensure()
        await A.q("""INSERT INTO chat_gd5b_settings(workspace_id,key,value) VALUES(%s,'code_run',%s)
                     ON CONFLICT (workspace_id,key) DO UPDATE SET value=EXCLUDED.value, updated=now()""", (_ws(), Jsonb(b.model_dump())))
        return await code_settings()

    # ---------------- schedules
    @r.get("/api/chat/schedules")
    async def sched_list():
        u = _me(); await ensure()
        rows = await A.q("SELECT c.*, a.name agent FROM agent_crons c JOIN agents a ON a.id=c.agent_id ORDER BY c.locked_reason IS NOT NULL, c.enabled DESC, c.id")   # RLS: own workspace
        return {"items": [_row_out(c, u) for c in rows], "can_create": ROLES.get(u.get("role"), 0) >= 2, "tz": TZ, "max_per_day": MAX_PER_DAY}

    @r.post("/api/chat/schedules")
    async def sched_new(b: SchedIn):
        u = _me(); _need(u, "editor"); await ensure()
        aid = G5._aid(b.agent_id)
        if not await A.q("SELECT 1 FROM agents WHERE id=%s AND NOT deleted", (aid,), one=True): raise HTTPException(404, "Không có agent")
        expr = " ".join(b.cron.split()); cron_check(expr)
        row = await A.q("INSERT INTO agent_crons(agent_id,name,cron,message,new_thread,created_by) VALUES(%s,%s,%s,%s,true,%s) RETURNING *",
                        (aid, b.name.strip(), expr, b.message.strip(), u.get("id")), one=True)
        row["agent"] = (await A.q("SELECT name FROM agents WHERE id=%s", (aid,), one=True) or {}).get("name")
        return _row_out(row, u)

    async def _sched(cid: int, u):
        c = await A.q("SELECT c.*, a.name agent FROM agent_crons c JOIN agents a ON a.id=c.agent_id WHERE c.id=%s", (cid,), one=True)
        if not c: raise HTTPException(404, "Không có lịch")
        if c.get("locked_reason"): raise HTTPException(409, f"Lịch đã khoá: {c['locked_reason']}")
        if not _row_out(c, u)["can_edit"]: raise HTTPException(403, "Chỉ admin hoặc người tạo lịch được sửa")
        return c

    @r.patch("/api/chat/schedules/{cid}")
    async def sched_patch(cid: int, b: SchedPatch):
        u = _me(); _need(u, "editor"); await ensure(); c = await _sched(cid, u)
        await A.q("UPDATE agent_crons SET enabled=%s WHERE id=%s AND locked_reason IS NULL", (b.enabled, cid))
        c["enabled"] = b.enabled; return _row_out(c, u)

    @r.delete("/api/chat/schedules/{cid}")
    async def sched_del(cid: int):
        u = _me(); _need(u, "editor"); await ensure(); await _sched(cid, u)
        await A.q("DELETE FROM agent_crons WHERE id=%s AND locked_reason IS NULL", (cid,)); return {"ok": True}

    # ---------------- share links
    @r.post("/api/chat/shares")
    async def share_new(b: ShareIn):
        import server as SV
        u = _me(); _need(u, "editor"); await ensure(); t = await SV._get_thread(b.thread_id)
        if (t.get("metadata") or {}).get("source") != "chat": raise HTTPException(400, "Chỉ chia sẻ được cuộc trò chuyện")
        n = await A.q("SELECT count(*) n FROM chat_shares WHERE thread_id=%s AND revoked IS NULL AND expires > now()", (t["thread_id"],), one=True)
        if n["n"] >= 10: raise HTTPException(429, "Tối đa 10 link đang hiệu lực cho một cuộc trò chuyện")
        sid, secret = secrets.token_hex(6), secrets.token_urlsafe(24)
        row = await A.q("""INSERT INTO chat_shares(id,thread_id,workspace_id,token_hash,created_by,expires) VALUES(%s,%s,%s,%s,%s,now() + make_interval(days => %s))
                           RETURNING id, thread_id, created, expires, revoked, views""", (sid, t["thread_id"], t["workspace_id"], _hash(secret), u.get("id"), b.days), one=True)
        return {**row, "token": f"{sid}.{secret}", "path": f"/chat/#/s/{sid}.{secret}"}

    @r.get("/api/chat/shares")
    async def share_list(thread_id: str):
        import server as SV
        _me(); await ensure(); t = await SV._get_thread(thread_id)
        return {"items": await A.q("SELECT id, thread_id, created, expires, revoked, views, last_view, created_by FROM chat_shares WHERE thread_id=%s ORDER BY created DESC", (t["thread_id"],))}

    @r.delete("/api/chat/shares/{sid}")
    async def share_revoke(sid: str):
        import server as SV
        u = _me(); _need(u, "editor"); await ensure()
        s = await A.q("SELECT * FROM chat_shares WHERE id=%s", (sid,), one=True)
        if not s: raise HTTPException(404, "Không có link")
        await SV._get_thread(s["thread_id"])
        if u.get("role") != "admin" and s["created_by"] != u.get("id"): raise HTTPException(403, "Chỉ người tạo link hoặc admin được thu hồi")
        await A.q("UPDATE chat_shares SET revoked=coalesce(revoked, now()) WHERE id=%s", (sid,)); return {"ok": True}

    @r.get("/api/chat/share/{token}")
    async def share_open(token: str):
        import server as SV, obs
        u = _me(); await ensure()
        sid, _, secret = token.partition(".")
        if not re.fullmatch(r"[0-9a-f]{12}", sid) or not secret: raise HTTPException(404, "Link không hợp lệ")
        async def load():
            s = await A.q("SELECT * FROM chat_shares WHERE id=%s", (sid,), one=True)
            if not s or not hmac.compare_digest(s["token_hash"], _hash(secret)) or s["workspace_id"] != (u.get("ws") or 1): return None, None, None
            t = await A.q("SELECT thread_id, agent_id, metadata, workspace_id, created_at FROM lg_threads WHERE thread_id=%s", (s["thread_id"],), one=True)
            ag = await A.q("SELECT name FROM agents WHERE id=%s", (t["agent_id"],), one=True) if t and t["agent_id"] else None
            return s, t, ag
        s, t, ag = await _system(load())
        if not s or not t: raise HTTPException(404, "Link không hợp lệ")
        if s["revoked"]: raise HTTPException(410, "Link đã bị thu hồi")
        if s["expires"] < dt.datetime.now(dt.timezone.utc): raise HTTPException(410, "Link đã hết hạn")
        st = await SV._state(t) if t["agent_id"] else {"values": {}}
        rules = await obs.redact_rules()
        msgs = await share_view(st.get("values", {}).get("messages") or [], rules)
        title = obs_safe((t["metadata"] or {}).get("title") or "Cuộc trò chuyện", rules)
        import pii
        pcfg = await pii.workspace_cfg(t["workspace_id"])   # GĐ9.8: a public link is an exposure to third parties -> mask personal data whenever the workspace enabled masking
        if pcfg:
            pz = pii.Pseudonymizer() if pcfg["mode"] == "pseudonym" else None
            msgs, title = await pii.amask_obj(msgs, pcfg, pz), await pii.amask_obj(title, pcfg, pz)
        await _system(A.q("UPDATE chat_shares SET views=views+1, last_view=now() WHERE id=%s", (sid,)))
        return {"title": title, "agent_name": (ag or {}).get("name"),
                "created": t["created_at"], "expires": s["expires"], "messages": msgs}
    return r
