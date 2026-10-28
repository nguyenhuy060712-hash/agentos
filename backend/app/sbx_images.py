"""GĐ11.47a snapshots of virtual computers + the `setup.sh` bake. Table: `sandbox_snapshots` (kinds `capture` and `bake`, migration 0051; the old folder-sandbox snapshots are kind `files` and untouched).
 capture = a whole-machine snapshot: `tar.zst` of the two durable mounts (/workspace, /opt/aos-user) + `docker commit` of the box (the root FS is durable under gVisor overlay2=none, so apt/pip installs
           are inside the commit). New box from it = new box from the committed image, archive extracted INSIDE the box, then stop/start so aos-boot runs again.
 bake    = temp box (internet `basic`) runs `bash -e setup.sh` with the agent's secrets (ws_secrets named `agent:<identifier>/<KEY>`) only for that command; the files setup.sh changed (`docker diff`)
           are scanned for those values (plain, base64, url-safe base64); a hit refuses the image ("setup.sh đã ghi khoá vào máy"); success = `docker commit` -> `aos-ws<ws>-agent-<identifier>--setup:<hash>` (the Docker tag carries the workspace: identifiers are not unique across workspaces; the snapshot ROW name stays `agent-<identifier>--setup`, unique per workspace).
           Images baked before the fix (tag `agent-<identifier>--setup:<hash>`, shared by workspaces) are never reused and never retagged (their content may come from another workspace's secrets): the next bake rebuilds under the new tag, and an image still referenced by another row is never removed.
           Rebuilds only when the hash of (setup.sh + base image) changes; a failed build keeps the old image and stores the log; running boxes never change.
Disk is bounded per workspace: AOS_SNAP_MAX_COUNT (10) snapshots, AOS_SNAP_MAX_GB (5) of archives + committed layers, AOS_SNAP_MAX_MB (2048) per uncompressed archive.
Files never touch the host except as the compressed archive under admin.SBX_ROOT.parent/"snapshots" (mode 700); archives are read/written through `docker exec tar` inside the box."""
import asyncio, base64, hashlib, os, re, tarfile, tempfile, time
from pathlib import Path
import sbx_runtime as R

MAX_COUNT = lambda: int(os.environ.get("AOS_SNAP_MAX_COUNT", "10"))                       # noqa: E731
MAX_TOTAL = lambda: int(float(os.environ.get("AOS_SNAP_MAX_GB", "5")) * (1 << 30))      # noqa: E731
MAX_TAR = lambda: int(os.environ.get("AOS_SNAP_MAX_MB", "2048")) << 20                  # noqa: E731
BAKE_TIMEOUT = lambda: int(os.environ.get("AOS_BAKE_TIMEOUT_S", "900"))                 # noqa: E731
MIN_SECRET = 4          # shorter values would match random bytes everywhere
KINDS = ("capture", "bake")
DEFAULT_NAME = "default"        # the workspace's default snapshot: the one a new box starts from when the agent names none
MOUNTS = ("workspace", "opt/aos-user")
SCRIPT_DIR = "/tmp/aos-bake"
REFUSED = "setup.sh đã ghi khoá vào máy"

class BakeRefused(R.SbxError): status = 422

# ---------------------------------------------------------------- secret scan
def secret_needles(values) -> set:
    """Plain + base64 + url-safe base64 byte patterns of each value (base64 in all three alignments, partial edge characters dropped)."""
    out = set()
    for v in values:
        b = v.encode() if isinstance(v, str) else bytes(v)
        if len(b) < MIN_SECRET: continue
        out.add(b)
        for shift in (0, 1, 2):
            for enc in (base64.b64encode, base64.urlsafe_b64encode):
                e = enc(b"\0" * shift + b).rstrip(b"=")[(0, 2, 3)[shift]:]
                if (shift + len(b)) % 3: e = e[:-1]
                if len(e) >= MIN_SECRET: out.add(e)
    return out

def scan_file(path, needles, chunk=1 << 20) -> bool:
    """True when any needle occurs in the file (streamed, overlap = longest needle)."""
    if not needles: return False
    keep, tail = max(len(n) for n in needles) - 1, b""
    with open(path, "rb") as f:
        while True:
            d = f.read(chunk)
            if not d: return False
            buf = tail + d
            if any(n in buf for n in needles): return True
            tail = buf[-keep:] if keep else b""

def redact(text, values) -> str:
    for v in sorted((x for x in values if len(x) >= MIN_SECRET), key=len, reverse=True):
        text = text.replace(v, "***").replace(base64.b64encode(v.encode()).decode(), "***")
    return text

# ---------------------------------------------------------------- operations on a box / on images (Docker; FakeOps for tests)
async def _pipe(cmd, dest=None, max_bytes=0, stdin_bytes=None, stdin_path=None, timeout=900):
    """Run a command; stdout streams into the file `dest` (killed when it grows past max_bytes). -> (rc, stderr, written)."""
    fin = open(stdin_path, "rb") if stdin_path else None
    try:
        p = await asyncio.create_subprocess_exec(*cmd, stdin=fin if fin else (asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL),
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        written, over = 0, False
        async def feed():
            if stdin_bytes is not None: p.stdin.write(stdin_bytes); await p.stdin.drain(); p.stdin.close()
        async def drain():
            nonlocal written, over
            with open(dest, "wb") if dest else open(os.devnull, "wb") as out:
                while True:
                    d = await p.stdout.read(1 << 20)
                    if not d: return
                    written += len(d); out.write(d)
                    if max_bytes and written > max_bytes: over = True; p.kill(); return
        try:
            _, _, err = await asyncio.wait_for(asyncio.gather(feed(), drain(), p.stderr.read()), timeout)
        except asyncio.TimeoutError:
            p.kill(); return 124, "timeout", written
        await p.wait()
        return (413 if over else p.returncode), err.decode(errors="replace").strip()[:300], written
    finally:
        if fin: fin.close()

class DockerOps:
    async def _d(self, *a, timeout=120): return await R._sh("docker", *a, timeout=timeout)
    async def tar_out(self, id, dest, max_bytes, paths=None):
        """Tar of the durable mounts (default; /workspace/.aos/out excluded) or of an explicit list of absolute paths, into the host file `dest`. 413 = bigger than max_bytes."""
        c = R.cname(R.check_id(id))
        if paths is None:
            rc, err, n = await _pipe(["docker", "exec", c, "tar", "-C", "/", "--exclude=workspace/.aos/out", "-cf", "-", *MOUNTS], dest, max_bytes)
        else:
            lst = "\n".join(p.lstrip("/") for p in paths if "\n" not in p and p.startswith("/")).encode() + b"\n"
            rc, err, n = await _pipe(["docker", "exec", "-i", c, "tar", "-C", "/", "--no-recursion", "--ignore-failed-read", "-cf", "-", "-T", "-"], dest, max_bytes, stdin_bytes=lst)
        if rc == 413: raise R.SbxError(f"the data is larger than {max_bytes >> 20} MB: clean up first")
        if rc not in (0, 1): raise R.BoxFailed("archiving failed: " + err[:200])     # tar exit 1 = "file changed while reading": fine
        return n
    async def tar_in(self, id, src):
        c = R.cname(R.check_id(id))
        rc, err, _ = await _pipe(["docker", "exec", "-i", c, "tar", "-x", "-C", "/", "-f", "-", *MOUNTS], stdin_path=src, timeout=900)     # only the two mounts, extracted inside the box
        if rc: raise R.BoxFailed("restoring failed: " + err[:200])
    async def after_create(self, id, image): pass
    async def image_info(self, tag):
        rc, out, _ = await self._d("image", "inspect", "-f", "{{.Id}} {{.Size}}", tag, timeout=30)
        if rc: return None
        i, s = out.decode().split(); return {"id": i, "size": int(s)}
    async def commit(self, id, tag):
        rc, _, err = await self._d("commit", R.cname(R.check_id(id)), tag, timeout=600)
        if rc: raise R.BoxFailed("docker commit failed: " + err[:200])
        return await self.image_info(tag)
    async def changed(self, id):
        rc, out, err = await self._d("diff", R.cname(R.check_id(id)), timeout=300)
        if rc: raise R.BoxFailed("docker diff failed: " + err[:200])
        return [l[2:] for l in out.decode(errors="replace").splitlines() if l[:2] in ("A ", "C ") and l[2:].startswith("/")]
    async def image_rm(self, tag):
        rc, _, err = await self._d("rmi", tag, timeout=120)
        if rc and "No such image" not in err: raise R.BadState("image still in use: " + err[:120])
    async def images(self, repo):
        rc, out, _ = await self._d("image", "ls", "--filter", f"reference={repo}", "--format", "{{.Repository}}:{{.Tag}}", timeout=30)
        return out.decode().split() if rc == 0 else []

class FakeOps:
    """Same operations on a FakeDriver: files in `driver.boxes[id]['files']`; 'root' files = every path outside the two mounts (what docker commit would hold)."""
    def __init__(self, driver): self.d = driver; self.d.__dict__.setdefault("images", {})
    def _f(self, id): return self.d._b(id)["files"]
    @staticmethod
    def _mount(p): return p.startswith(("/workspace/", "/opt/aos-user/")) or p in ("/workspace", "/opt/aos-user")
    async def tar_out(self, id, dest, max_bytes, paths=None):
        f = self._f(id)
        names = [p for p in f if self._mount(p) and not p.startswith("/workspace/.aos/out/")] if paths is None else [p for p in paths if p in f]
        with tarfile.open(dest, "w") as t:
            for p in names:
                ti = tarfile.TarInfo(p.lstrip("/")); ti.size = len(f[p]); t.addfile(ti, __import__("io").BytesIO(f[p]))
        n = os.path.getsize(dest)
        if n > max_bytes: raise R.SbxError(f"the data is larger than {max_bytes >> 20} MB: clean up first")
        return n
    async def tar_in(self, id, src):
        f = self._f(id)
        with tarfile.open(src, "r") as t:
            for m in t.getmembers():
                if m.isfile() and self._mount("/" + m.name): f["/" + m.name] = t.extractfile(m).read()
    async def after_create(self, id, image):
        if image in self.d.images: self._f(id).update(self.d.images[image]["files"])
    async def image_info(self, tag):
        im = self.d.images.get(tag)
        return {"id": "sha256:" + hashlib.sha256(tag.encode()).hexdigest(), "size": 1000 + sum(len(v) for v in im["files"].values())} if im else None
    async def commit(self, id, tag):
        self.d.images[tag] = {"files": {p: v for p, v in self._f(id).items() if not self._mount(p)}}
        return await self.image_info(tag)
    async def changed(self, id): return [p for p in self._f(id) if not self._mount(p)]
    async def image_rm(self, tag):
        if any(b.get("image") == tag for b in self.d.boxes.values()): raise R.BadState("image still in use")     # like `docker rmi` of an image with containers
        self.d.images.pop(tag, None)
    async def images(self, repo): return [t for t in self.d.images if t.split(":")[0] == repo]

def ops_for(driver): return FakeOps(driver) if getattr(driver, "name", "") == "fake" else DockerOps()

# ---------------------------------------------------------------- stores
_VIEW = ("id", "name", "kind", "status", "agent_id", "sandbox_id", "archive_bytes", "image_bytes", "base_image", "setup_hash", "created")

class MemSnapStore:
    def __init__(self): self.rows = {}
    def system(self): return self
    async def insert(self, r):
        if any(x["workspace_id"] == r["workspace_id"] and x["name"] == r["name"] for x in self.rows.values()): raise R.BadState("a snapshot with this name already exists")
        self.rows[r["id"]] = dict(r, created=time.time())
    async def get(self, ws, ref):
        rows = [r for r in self.rows.values() if r["workspace_id"] == ws and (r["id"] == ref or r["name"] == ref)]
        return dict(rows[0]) if rows else None
    async def list(self, ws): return sorted((dict(r) for r in self.rows.values() if r["workspace_id"] == ws), key=lambda r: -r["created"])
    async def update(self, id, **f): self.rows[id].update(f)
    async def delete(self, id): self.rows.pop(id, None)
    async def for_agent(self, agent_id, ws): return [dict(r) for r in self.rows.values() if r.get("agent_id") == agent_id and (ws is None or r["workspace_id"] == ws)]
    async def with_image(self, tag): return [dict(r) for r in self.rows.values() if r.get("image_tag") == tag]

_COLS = ("name", "status", "archive_path", "archive_bytes", "image_tag", "image_bytes", "base_image", "base_digest", "setup_hash", "build_log", "bytes")

class PgSnapStore:
    def __init__(self, q=None):
        if q is None:
            import deepagent_api as A; q = A.q
        self.q = q
    def system(self): return PgSnapStore(q=R._sys_q)
    async def insert(self, r):
        try:
            await self.q("INSERT INTO sandbox_snapshots(id,name,kind,workspace_id,owner_id,sandbox_id,agent_id,status,archive_path,archive_bytes,image_tag,image_bytes,base_image,base_digest,setup_hash,build_log,bytes) "
                         "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                         (r["id"], r["name"], r["kind"], r["workspace_id"], r.get("owner_id"), r.get("sandbox_id"), r.get("agent_id"), r["status"], r.get("archive_path"), r.get("archive_bytes", 0),
                          r.get("image_tag"), r.get("image_bytes", 0), r.get("base_image"), r.get("base_digest"), r.get("setup_hash"), r.get("build_log"), r.get("archive_bytes", 0)))
        except Exception as e:  # noqa: BLE001
            if "unique" in str(e).lower() or "duplicate" in str(e).lower(): raise R.BadState("a snapshot with this name already exists") from None
            raise
    async def get(self, ws, ref):
        return await self.q("SELECT * FROM sandbox_snapshots WHERE kind IN ('capture','bake') AND workspace_id=%s AND (id=%s OR name=%s) ORDER BY created DESC LIMIT 1", (ws, ref, ref), one=True)
    async def list(self, ws): return await self.q("SELECT * FROM sandbox_snapshots WHERE kind IN ('capture','bake') AND workspace_id=%s ORDER BY created DESC", (ws,))
    async def update(self, id, **f):
        for k in f:
            if k not in _COLS: raise ValueError(k)
        if "archive_bytes" in f: f["bytes"] = f["archive_bytes"]
        await self.q(f"UPDATE sandbox_snapshots SET {','.join(k + '=%s' for k in f)} WHERE id=%s", (*f.values(), id))
    async def delete(self, id): await self.q("DELETE FROM sandbox_snapshots WHERE id=%s", (id,))
    async def for_agent(self, agent_id, ws):
        return await self.q("SELECT * FROM sandbox_snapshots WHERE kind IN ('capture','bake') AND agent_id=%s AND (%s::int IS NULL OR workspace_id=%s)", (agent_id, ws, ws))
    async def with_image(self, tag): return await self.q("SELECT id, workspace_id FROM sandbox_snapshots WHERE image_tag=%s", (tag,))      # use on system(): every workspace

def snap_root() -> Path:
    import admin
    p = admin.SBX_ROOT.parent / "snapshots"; p.mkdir(parents=True, exist_ok=True)
    try: os.chmod(p, 0o700)
    except OSError: pass
    return p

async def load_agent_secrets(ws, identifier) -> dict:
    """{KEY: value} of the ws_secrets named `agent:<identifier>/<KEY>` (only keys that are valid variable names). Values never leave this process except into the bake's exec environment."""
    import ws_secrets
    prefix = f"agent:{identifier}/"
    rows = await R._sys_q("SELECT name, value_enc FROM ws_secrets WHERE workspace_id=%s AND left(name, %s) = %s", (int(ws), len(prefix), prefix))
    return {r["name"][len(prefix):]: ws_secrets.open_value(r["value_enc"], ws, r["name"]) for r in rows if R.ENV_RE.match(r["name"][len(prefix):])}

def safe_ident(ident, agent_id) -> str: return re.sub(r"[^a-z0-9]+", "-", str(ident or "").lower()).strip("-")[:40] or f"a{int(agent_id)}"
def setup_repo(ws, ident) -> str: return f"aos-ws{int(ws)}-agent-{ident}--setup"      # Docker repository of a baked setup image: scoped to the workspace

# ---------------------------------------------------------------- the service
class Images:
    def __init__(self, computers=None, store=None, ops=None, root=None, secrets_loader=None):
        self.c, self._store, self._ops, self._root, self.load_secrets = computers or R.shared(), store, ops, root, secrets_loader or load_agent_secrets
        self._locks = {}
    @property
    def store(self):
        if self._store is None: self._store = PgSnapStore()
        return self._store
    def ops(self, core=None): return self._ops or ops_for(self.c._driver)
    def root(self): return self._root or snap_root()
    def _lock(self, k): return self._locks.setdefault(k, asyncio.Lock())

    # ---- helpers
    async def _usage(self, ws):
        rows = await self.store.list(ws)
        return len(rows), sum(int(r.get("archive_bytes") or 0) + int(r.get("image_bytes") or 0) for r in rows)
    async def _room(self, ws, add_count=1):
        n, used = await self._usage(ws)
        if n + add_count > MAX_COUNT(): raise R.QuotaExceeded(f"at most {MAX_COUNT()} snapshots per workspace: delete one first")
        if used >= MAX_TOTAL(): raise R.QuotaExceeded("the snapshots of this workspace use all their disk space: delete one first")
    def _check(self, ctx, write=True):
        return R.authorize(ctx, {"workspace_id": ctx.get("ws"), "owner_id": ctx.get("uid")} if ctx else None, write)
    @staticmethod
    def _name(name):
        name = (name or "").strip()
        if not name or len(name) > 80 or any(ord(c) < 32 for c in name): raise R.SbxError("a snapshot name has 1 to 80 characters")
        return name
    async def _delta(self, ops, tag, base):
        a, b = await ops.image_info(tag), await ops.image_info(base)
        return max(0, (a or {}).get("size", 0) - (b or {}).get("size", 0)), (a or {}).get("id")
    async def _drop_files(self, r):
        if r.get("archive_path"): Path(r["archive_path"]).unlink(missing_ok=True)
    async def _drop_image(self, ops, tag, own_id=None):
        """Remove an image unless another snapshot row (any workspace) still points at the same tag (old shared setup tags)."""
        if tag:
            try:
                if any(x["id"] != own_id for x in await self.store.system().with_image(tag)): return
                await ops.image_rm(tag)
            except R.SbxError as e: print(f"[sbx-images] could not remove image {tag}: {str(e)[:120]}", flush=True)

    # ---- read
    async def list(self, ctx):
        self._check(ctx, False); return await self.store.list(int(ctx["ws"]))
    async def get(self, ctx, ref):
        self._check(ctx, False); r = await self.store.get(int(ctx["ws"]), ref)
        if not r: raise R.NotFound("no such snapshot")
        return r

    # ---- capture
    async def capture(self, ctx, box_id, name, *, secrets=()):
        """Snapshot a RUNNING box (its owner or a workspace admin). `secrets`: values that must not be inside (checked on the uncompressed stream)."""
        name = self._name(name); b = await self.c._row(ctx, box_id, True); ws = int(ctx["ws"])
        b = await self.c._wake(b)
        if b["status"] != "running": raise R.BadState(f"the computer is {b['status']}, start it first")
        await self._room(ws)
        sid, ops = R.new_id(), self.ops(b["core"]); tag = f"aos-snap-{sid}:1"; arc = self.root() / f"{sid}.tar.zst"
        row = {"id": sid, "name": name, "kind": "capture", "workspace_id": ws, "owner_id": ctx["uid"], "sandbox_id": b["id"], "agent_id": b.get("agent_id"), "status": "building"}
        await self.store.insert(row)
        try:
            n, size = await self._archive(ops, b["id"], arc, secrets)
            await ops.commit(b["id"], tag)
            delta, digest = await self._delta(ops, tag, b["image"])
            if (await self._usage(ws))[1] + size + delta > MAX_TOTAL(): raise R.QuotaExceeded("this snapshot does not fit in the disk space of the workspace: delete one first")
            await self.store.update(sid, status="ready", archive_path=str(arc), archive_bytes=size, image_tag=tag, image_bytes=delta, base_image=b["image"], base_digest=digest)
        except BaseException:
            arc.unlink(missing_ok=True); await self._drop_image(ops, tag); await self.store.delete(sid); raise
        return await self.store.get(ws, sid)

    async def _archive(self, ops, box_id, arc, secrets):
        """tar of the mounts -> secret scan of the UNCOMPRESSED stream -> tar.zst (mode 600). -> (uncompressed bytes, compressed bytes)."""
        import zstandard
        fd, tmp = tempfile.mkstemp(suffix=".tar", dir=str(self.root())); os.close(fd)
        try:
            n = await ops.tar_out(box_id, tmp, MAX_TAR())
            if secrets and await asyncio.to_thread(scan_file, tmp, secret_needles(secrets)): raise BakeRefused(REFUSED)
            def comp():
                with open(tmp, "rb") as i, open(arc, "wb") as o: zstandard.ZstdCompressor(level=6).copy_stream(i, o)
            await asyncio.to_thread(comp); os.chmod(arc, 0o600)
            return n, arc.stat().st_size
        finally: Path(tmp).unlink(missing_ok=True)

    # ---- new box from a snapshot
    async def create_box(self, ctx, ref, *, name="computer", size="small", scope="thread", agent_id=None, thread_id=None, **kw):
        snap = await self.get(ctx, ref)
        if snap["status"] != "ready": raise R.BadState("this snapshot is not ready")
        box = await self.c.create(ctx, name=name, size=size, scope=scope, agent_id=agent_id, thread_id=thread_id, image=snap["image_tag"] or snap["base_image"], **kw)
        ops = self.ops(box["core"])
        try:
            await ops.after_create(box["id"], box["image"])
            if snap.get("archive_path") and kw.get("start", True):
                await self._restore(ops, box["id"], snap)
                await self.c.stop(ctx, box["id"]); await self.c.start(ctx, box["id"], wait_boot=True)      # aos-boot runs again on the restored mounts
        except BaseException:
            try: await self.c.delete(ctx, box["id"])
            except Exception: pass  # noqa: BLE001
            raise
        return await self.c.store.get(box["id"])

    async def _restore(self, ops, box_id, snap):
        import zstandard
        fd, tmp = tempfile.mkstemp(suffix=".tar", dir=str(self.root())); os.close(fd)
        try:
            def dec():
                n = 0
                with open(snap["archive_path"], "rb") as i, open(tmp, "wb") as o, zstandard.ZstdDecompressor().stream_reader(i) as r:
                    while True:
                        d = r.read(1 << 20)
                        if not d: return
                        n += len(d)
                        if n > MAX_TAR() * 2: raise R.SbxError("the snapshot archive is larger than allowed")
                        o.write(d)
            await asyncio.to_thread(dec); await ops.tar_in(box_id, tmp)
        finally: Path(tmp).unlink(missing_ok=True)

    # ---- delete
    async def delete(self, ctx, ref):
        """Repeatable (a missing snapshot = done). 409 while a box still uses the image."""
        self._check(ctx, True); ws = int(ctx["ws"]); r = await self.store.get(ws, ref)
        if not r: return True
        if R._rank(ctx) < R.ROLE_RANK["admin"] and ctx["uid"] != r.get("owner_id"): raise R.Forbidden("only the owner of a snapshot or a workspace admin can delete it")
        tag = r.get("image_tag")
        used = [b for b in await self.c.sys.list(ws) if tag and b.get("image") == tag]      # system view: boxes of other users count too
        if used: raise R.BadState(f"{len(used)} computer(s) still use this snapshot: delete them first")
        await self._drop_files(r); await self._drop_image(self.ops(), tag, r["id"]); await self.store.delete(r["id"]); return True

    # ---- bake
    async def bake(self, ctx, agent_id, setup_sh, *, identifier=None, internet="basic"):
        """Build (or reuse) the setup image of an agent. -> snapshot row. Refused/failed builds raise BakeRefused (422) after the log is stored; the old image stays."""
        self._check(ctx, True)
        if not isinstance(setup_sh, str) or not setup_sh.strip() or len(setup_sh) > 65536 or "\x00" in setup_sh: raise R.SbxError("setup.sh must be text of 1 to 65536 characters")
        aid, ws = int(agent_id), int(ctx["ws"])
        raw = identifier if identifier is not None else await self.c.sys.agent_identifier(aid)
        ident = safe_ident(raw, aid); name = f"agent-{ident}--setup"
        ops = self.ops(); h = hashlib.sha256((setup_sh + "\n" + R.IMAGE).encode()).hexdigest()[:16]; tag = f"{setup_repo(ws, ident)}:{h}"
        async with self._lock((ws, name)):
            old = await self.store.get(ws, name)
            if old and old["status"] == "ready" and old.get("setup_hash") == h and old.get("image_tag") == tag and await ops.image_info(tag): return old      # an old-style shared tag is rebuilt under the workspace tag
            if not old: await self._room(ws)
            env = await self.load_secrets(ws, raw if raw else aid); vals = list(env.values())
            fresh = not (old and old.get("image_tag"))        # no earlier image to fall back on
            if old is None:
                old = {"id": R.new_id(), "name": name, "kind": "bake", "workspace_id": ws, "owner_id": ctx["uid"], "agent_id": aid, "status": "building"}
                await self.store.insert(old)
            log, err = "", None
            try:
                if scan_in_text(setup_sh, vals): raise BakeRefused(REFUSED)
                log = await self._bake_run(ctx, ops, aid, ident, setup_sh, env, vals, tag, old, internet)
            except BakeRefused as e: err = str(e)
            except R.SbxError as e: err = "build failed: " + str(e)[:300]
            except Exception as e: err = "build failed: " + str(e)[:300]  # noqa: BLE001
            if err:
                msg = redact(err + ("\n" + log if log else ""), vals)[-20000:]
                if fresh: await self.store.update(old["id"], status="failed", build_log=msg)
                else: await self.store.update(old["id"], build_log="FAILED (old image kept): " + msg)
                raise BakeRefused(redact(err, vals))
            return await self.store.get(ws, name)

    async def _bake_run(self, ctx, ops, aid, ident, setup_sh, env, vals, tag, row, internet="basic"):
        box = await self.c.create(ctx, name=f"bake-{ident}"[:80], size="small", scope="thread", agent_id=aid, internet=internet)
        try:
            await self.c.put_file(ctx, box["id"], f"{SCRIPT_DIR}/setup.sh", setup_sh.encode())
            res = await self.c.exec(ctx, box["id"], f"bash -e {SCRIPT_DIR}/setup.sh", env=env, timeout=BAKE_TIMEOUT())
            log = redact(res["output"], vals)[-20000:]
            if res["exit_code"] != 0: raise R.SbxError(f"setup.sh exited with code {res['exit_code']}" + (" (time limit)" if res["timed_out"] else "") + "\n" + log)
            await self.c.exec(ctx, box["id"], f"rm -rf {SCRIPT_DIR}", timeout=30)
            if vals:
                fd, tmp = tempfile.mkstemp(suffix=".tar", dir=str(self.root())); os.close(fd)
                try:
                    await ops.tar_out(box["id"], tmp, MAX_TAR(), paths=await ops.changed(box["id"]))
                    if await asyncio.to_thread(scan_file, tmp, secret_needles(vals)): raise BakeRefused(REFUSED)
                finally: Path(tmp).unlink(missing_ok=True)
            await ops.commit(box["id"], tag)
            delta, digest = await self._delta(ops, tag, box["image"]); ws = int(ctx["ws"])
            if (await self._usage(ws))[1] - int(row.get("image_bytes") or 0) + delta > MAX_TOTAL(): await self._drop_image(ops, tag); raise R.QuotaExceeded("this image does not fit in the disk space of the workspace")
            prev = row.get("image_tag")
            await self.store.update(row["id"], status="ready", image_tag=tag, image_bytes=delta, base_image=box["image"], base_digest=digest, setup_hash=tag.split(":")[1], build_log=log)
            if prev and prev != tag: await self._drop_image(ops, prev)
            return log
        finally:
            try: await self.c.delete(ctx, box["id"])
            except Exception as e: print(f"[sbx-images] bake box {box['id']} not removed: {str(e)[:120]}", flush=True)  # noqa: BLE001

    # ---- purge (hook of sbx_runtime.purge_for_agent)
    async def purge_agent(self, agent_id, workspace_id=None):
        """Snapshots of the agent (rows, archives, images) and the `aos-ws<ws>-agent-<identifier>--setup` images of ITS OWN workspace only (workspace_id=None: the workspaces of the agent's rows).
        Repeatable; a row stays when its image is still in use. An image another row still points at is kept."""
        sys_store, ops, aid, errors, n = self.store.system(), self.ops(), int(agent_id), [], 0
        rows = await sys_store.for_agent(aid, workspace_id)
        wss = {int(workspace_id)} if workspace_id is not None else {int(r["workspace_id"]) for r in rows}
        for r in rows:
            try:
                tag = r.get("image_tag")
                if tag and not any(x["id"] != r["id"] for x in await sys_store.with_image(tag)): await ops.image_rm(tag)
                await self._drop_files(r); await sys_store.delete(r["id"]); n += 1
            except Exception as e: errors.append({"snapshot": r["id"], "error": str(e)[:160]})  # noqa: BLE001
        raw = await self.c.sys.agent_identifier(aid)
        for repo in {setup_repo(w, i) for w in wss for i in (safe_ident(raw, aid), f"a{aid}")}:
            for t in await ops.images(repo):
                try:
                    if not await sys_store.with_image(t): await ops.image_rm(t)
                except Exception as e: errors.append({"image": t, "error": str(e)[:160]})  # noqa: BLE001
        return {"snapshots": n, "errors": errors}

def scan_in_text(text, values): return any(n in text.encode() for n in secret_needles(values))

def view(r):
    v = {k: r.get(k) for k in _VIEW}
    v["build_log"] = (r.get("build_log") or "")[-4000:]; return v

_SH: dict = {}
def shared() -> Images: return _SH.setdefault("i", Images())

async def purge_hook(agent_id, workspace_id):
    return await shared().purge_agent(agent_id, workspace_id)

if purge_hook not in R.PURGE_HOOKS: R.PURGE_HOOKS.append(purge_hook)
