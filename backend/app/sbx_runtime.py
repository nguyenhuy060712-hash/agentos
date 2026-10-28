"""GĐ11.40 virtual-computer foundation: a long-lived box per thread / agent, isolated by gVisor (runsc), driven through a swappable CORE.
Pieces: errors (each carries its HTTP status) · state machine · Driver interface + `DRIVERS` registry (`gvisor` = DockerGvisorDriver, `fake` = FakeDriver for tests,
no Docker) · `select_core()` (env AOS_SBX_CORE=auto|gvisor; anything else = "core not built") · `Computers` (permission checks + lifecycle on top of a Store) ·
disk watchdog (`docker system df`; warn at 80 %, stop at 100 % of AOS_BOX_DISK_GB; refuse new boxes when the host disk is > 85 % full).
Security rules: the computer NEVER runs on runc (the driver always passes `--runtime runsc` itself and re-checks it after `docker create`), no network
(`--network none`), files move only through `docker exec` inside the box, every query/row is workspace-checked here AND by Postgres RLS (`sandboxes` is RLS_PRIVATE).
The root FS is durable on purpose: `--annotation dev.gvisor.flag.overlay2=none` (measured in N2 §8, card 11.40a Q4); /workspace and /opt/aos-user are named volumes."""
import asyncio, contextvars, dataclasses, json, os, posixpath, re, secrets, shutil, time, uuid

IMAGE = os.environ.get("AOS_COMPUTER_IMAGE", "aos-computer:20261026")
SIZES = {"small": {"cpus": "0.5", "memory": "1g"}, "medium": {"cpus": "1", "memory": "2g"}}
CAPS = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "SETUID", "SETGID")
OUT_CAP = 100_000                      # bytes of command output returned; the rest stays in /workspace/.aos/out/<command_id>.log
MAX_CMD = 100_000
PUT_MAX = int(os.environ.get("AOS_BOX_PUT_MAX", str(50 << 20)))
STATES = ("provisioning", "running", "stopped", "deleted", "failed")
TRANSITIONS = {"provisioning": {"running", "failed", "deleted"}, "running": {"stopped", "failed", "deleted"},
               "stopped": {"running", "failed", "deleted"}, "failed": {"running", "deleted"}, "deleted": set()}
ID_RE = re.compile(r"^[0-9a-f]{16}$")    # 16 hex: never matches the legacy 12-hex folder-sandbox ids (admin._sbx_dir rejects them)
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
ROLE_RANK = {"viewer": 1, "editor": 2, "admin": 3}

# ---------------------------------------------------------------- errors (status = HTTP code the API layer will answer)
class SbxError(Exception):
    status = 400
    def __init__(self, msg, retry_after=None): super().__init__(msg); self.retry_after = retry_after
class NotFound(SbxError): status = 404          # also for "exists in another workspace / belongs to another user": existence is not revealed
class Forbidden(SbxError): status = 403
class BadState(SbxError): status = 409
class CoreUnavailable(SbxError): status = 503   # gVisor missing -> computer refused (never falls back to runc)
class CoreNotBuilt(SbxError): status = 501
class Capacity(SbxError): status = 503
class QuotaExceeded(SbxError): status = 429
class BoxFailed(SbxError): status = 502

def http_error(e: SbxError):
    """SbxError -> fastapi HTTPException (503 and 429 carry Retry-After)."""
    from fastapi import HTTPException
    h = {"Retry-After": str(int(e.retry_after or 30))} if e.status in (503, 429) else None
    return HTTPException(e.status, str(e), headers=h)

# ---------------------------------------------------------------- helpers
def cname(i): return f"aos-box-{i}"
def vols(i): return f"aos-box-{i}-workspace", f"aos-box-{i}-opt"
def new_id(): return uuid.uuid4().hex[:16]

def check_id(i):
    if not isinstance(i, str) or not ID_RE.match(i): raise NotFound("no such computer")
    return i

def box_path(p, default="/workspace"):
    """Absolute, normalised path INSIDE the box. Relative = under /workspace. No NUL / newline / '..' segment; /proc /sys /dev refused."""
    p = default if p in (None, "") else p
    if not isinstance(p, str) or len(p) > 1024 or "\x00" in p or "\n" in p or "\r" in p: raise SbxError("bad path")
    if ".." in p.split("/"): raise SbxError("bad path: '..' is not allowed")
    if not p.startswith("/"): p = posixpath.join("/workspace", p)
    p = posixpath.normpath(p)
    if p.startswith("//"): p = "/" + p.lstrip("/")
    if p.split("/")[1:2] in (["proc"], ["sys"], ["dev"]): raise SbxError("bad path")
    return p

def check_env(env):
    env = dict(env or {})
    if len(env) > 50: raise SbxError("too many environment variables")
    for k, v in env.items():
        if not isinstance(k, str) or not ENV_RE.match(k) or not isinstance(v, str) or len(v) > 4096 or "\x00" in v: raise SbxError(f"bad environment variable {str(k)[:20]!r}")
    return env

_UNITS = {"b": 1, "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4, "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4}
def parse_size(s) -> int:
    """'20.5kB' / '1.5GB' / '0B' / '12MiB' (docker CLI sizes) -> bytes; anything else -> 0."""
    m = re.match(r"^\s*([0-9]*\.?[0-9]+)\s*([A-Za-z]*)", str(s or ""))
    return int(float(m.group(1)) * _UNITS.get(m.group(2).lower() or "b", 1)) if m else 0

def disk_limit_bytes(): return int(float(os.environ.get("AOS_BOX_DISK_GB", "5")) * (1 << 30))

# ---- 11.42 lifecycle / capacity helpers
OFF_MSG = "Workspace này chưa bật máy tính ảo"
SAMPLE_S = 60
MAX_TTL_S = 30 * 86400
SPAN_ATTRS = contextvars.ContextVar("sbx_span_attrs", default=None)   # set by Computers.exec: sandbox.id / kind / status_before / exit_code / duration_ms (the tool wrapper copies it onto its span)
PURGE_HOOKS: list = []     # async fn(agent_id, workspace_id) -> anything; 11.47a appends the snapshot / setup-image purge here

def _ts(v):
    """last_active as epoch seconds (MemStore keeps floats, Postgres gives datetimes)."""
    if v is None: return 0.0
    return float(v) if isinstance(v, (int, float)) else v.timestamp()

def box_ram_mb(size): return int(SIZES[size]["memory"].rstrip("g")) * 1024

def _mem_total_mb():
    for l in open("/proc/meminfo"):
        if l.startswith("MemTotal"): return int(l.split()[1]) // 1024
    return 0

def max_running_default():
    """AOS_COMPUTER_MAX_RUNNING, else from the RAM of the host: 4 GB -> 1, 8 GB -> 3, 16 GB -> 8 (a "4 GB" machine reports ~3.8 GB, so the cut-offs sit at 6 and 12 GB)."""
    v = os.environ.get("AOS_COMPUTER_MAX_RUNNING", "")
    if v.isdigit() and int(v) >= 1: return int(v)
    try: mb = _mem_total_mb()
    except OSError: return 3
    return 1 if mb < 6144 else 3 if mb < 12288 else 8

def idle_ttl_default():
    v = os.environ.get("AOS_BOX_IDLE_TTL_S", "")
    return int(v) if v.isdigit() and int(v) >= 60 else 600

def check_ttls(idle_ttl_s, delete_after_stop_s):
    ok = lambda v: isinstance(v, int) and not isinstance(v, bool)   # noqa: E731
    if idle_ttl_s is not None and not (ok(idle_ttl_s) and 60 <= idle_ttl_s <= MAX_TTL_S): raise SbxError("idle_ttl_s must be a whole number of seconds from 60 to 30 days")
    if delete_after_stop_s is not None and not (ok(delete_after_stop_s) and 60 <= delete_after_stop_s <= MAX_TTL_S and delete_after_stop_s % 60 == 0):
        raise SbxError("delete_after_stop_s must be a multiple of 60 seconds, at most 30 days")

def parse_stats(st) -> tuple:
    """driver.stats() -> (cpu percent of ONE core, memory bytes in use): {'cpu_percent': '12.5%', 'memory': '10.5MiB / 1GiB'}."""
    try: cpu = float(str(st.get("cpu_percent") or "0").strip().rstrip("%") or 0)
    except ValueError: cpu = 0.0
    return cpu, parse_size(str(st.get("memory") or "").split("/")[0])

@dataclasses.dataclass
class BoxSpec:
    id: str
    size: str = "small"
    image: str = ""
    workspace_id: int = 0
    ro_mounts: tuple = ()          # ((host_dir, box_path), ...) read-only bind mounts decided by the server (skills, 11.64); never from user input
    net: dict = None               # 11.45: host_state["egress"] of a box with internet basic|custom (own internal network + fixed IP); None = `--network none`

@dataclasses.dataclass
class ExecResult:
    command_id: str
    exit_code: int
    output: str
    truncated: bool = False
    timed_out: bool = False
    duration_ms: int = 0
    def as_dict(self): return dataclasses.asdict(self)

def net_args(net):
    """`--network none` (the default) or, for a box with Internet (11.45), its own internal egress network + fixed IP + a DNS server that answers nothing."""
    if not net: return ["--network", "none"]
    import egress_ctl
    return egress_ctl.box_net_args(net)

def run_args(spec: BoxSpec) -> list:
    """`docker create` arguments of one box. Pure function (unit-tested): every isolation flag is visible here."""
    if not ID_RE.match(spec.id) or spec.size not in SIZES: raise SbxError("bad box spec")
    sz, (vw, vo) = SIZES[spec.size], vols(spec.id)
    a = ["docker", "create", "--name", cname(spec.id), "--hostname", cname(spec.id), "--runtime", "runsc", "--annotation", "dev.gvisor.flag.overlay2=none",
         *net_args(spec.net), "--cap-drop", "ALL", *[x for c in CAPS for x in ("--cap-add", c)], "--security-opt", "no-new-privileges",
         "--pids-limit", "256", "--memory", sz["memory"], "--memory-swap", sz["memory"], "--cpus", sz["cpus"],
         "--label", "aos.box=1", "--label", f"aos.box.id={spec.id}", "--label", f"aos.box.ws={int(spec.workspace_id)}",
         "-v", f"{vw}:/workspace", "-v", f"{vo}:/opt/aos-user"]
    for host, ctr in spec.ro_mounts:
        if not host.startswith("/") or any(c in host + ctr for c in ":,\n\x00") or not ctr.startswith("/"): raise SbxError("bad mount")
        a += ["-v", f"{host}:{ctr}:ro"]
    return a + [spec.image or IMAGE]

# ---------------------------------------------------------------- drivers
class Driver:
    """create/start/stop/delete/exec/put/get/inspect/stats/capabilities (+ disk_usage for the watchdog). All async. Box id = the 16-hex id of the DB row."""
    name = "?"
    async def create(self, spec: BoxSpec): raise NotImplementedError
    async def start(self, id, wait_boot=False): raise NotImplementedError
    async def stop(self, id): raise NotImplementedError
    async def delete(self, id): raise NotImplementedError
    async def exec(self, id, cmd, *, cwd="/workspace", env=None, timeout=60, command_id=None, stdin=None) -> ExecResult: raise NotImplementedError
    async def put(self, id, path, data: bytes): raise NotImplementedError
    async def get(self, id, path) -> bytes: raise NotImplementedError
    async def inspect(self, id) -> dict: raise NotImplementedError
    async def stats(self, id) -> dict: raise NotImplementedError
    async def disk_usage(self) -> dict: raise NotImplementedError
    def capabilities(self) -> dict: raise NotImplementedError

async def _sh(*cmd, timeout=60, stdin=None, cap=None):
    """-> (rc, stdout bytes, stderr str). rc 124 on timeout (process killed)."""
    p = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                                             stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try: out, err = await asyncio.wait_for(p.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        try: p.kill()
        except ProcessLookupError: pass
        return 124, b"", "timeout"
    return p.returncode, (out if cap is None else out[:cap]), err.decode(errors="replace").strip()

_WRAP = ('f=/workspace/.aos/out/$1.log; mkdir -p /workspace/.aos/out; t=$2; m=$3; shift 3; '
         '"$@" >"$f" 2>&1; rc=$?; sz=$(stat -c %s "$f" 2>/dev/null || echo 0); head -c ' + str(OUT_CAP) + ' "$f"; printf "\\n%s %s %s\\n" "$m" "$rc" "$sz"; exit 0')

class DockerGvisorDriver(Driver):
    name = "gvisor"
    egress = None          # 11.45: EgressCtl used for Internet boxes; None = egress_ctl.default() (tests pass their own)
    async def _d(self, *a, timeout=60, stdin=None, cap=None): return await _sh("docker", *a, timeout=timeout, stdin=stdin, cap=cap)

    async def create(self, spec):
        vw, vo = vols(spec.id); args = run_args(spec)
        if "runsc" not in args or args[args.index("--runtime") + 1] != "runsc": raise CoreUnavailable("computer must run under runsc")   # belt and braces
        try:
            for v in (vw, vo):
                rc, _, err = await self._d("volume", "create", "--label", "aos.box=1", "--label", f"aos.box.id={spec.id}", v)
                if rc: raise BoxFailed("volume create failed: " + err[:300])
            rc, _, err = await _sh(*args, timeout=120)
            if rc: raise BoxFailed("docker create failed: " + err[:300])
            rc, out, _ = await self._d("inspect", "-f", "{{.HostConfig.Runtime}}", cname(spec.id))
            if rc or out.decode().strip() != "runsc": raise CoreUnavailable("container was not created with the runsc runtime - refused (no runc fallback)")
        except BaseException:
            await self.delete(spec.id); raise

    async def start(self, id, wait_boot=False):
        c = cname(check_id(id))
        await self._egress_up(id)
        rc, _, err = await self._d("start", c, timeout=60)
        if rc: raise BoxFailed("docker start failed: " + err[:300])
        end = time.monotonic() + 30
        while time.monotonic() < end:           # aos-boot touches /run/aos-ready; the root FS is durable, aos-boot removes the old marker first
            if (await self._d("exec", c, "test", "-f", "/run/aos-ready", timeout=15))[0] == 0: break
            await asyncio.sleep(0.4)
        else: raise BoxFailed("box did not become ready in 30 s")
        if wait_boot:
            end = time.monotonic() + 125
            while time.monotonic() < end and (await self._d("exec", c, "test", "-f", "/run/aos-boot-done", timeout=15))[0] != 0: await asyncio.sleep(1)

    async def stop(self, id):
        rc, _, err = await self._d("stop", "-t", "3", cname(check_id(id)), timeout=60)
        if rc and "No such container" in err: raise NotFound("no such computer")
        if rc: raise BoxFailed("docker stop failed: " + err[:300])

    async def _egress_up(self, id):
        """11.45: a box with Internet starts only after the proxy is up and attached to ITS network; no proxy = the box still starts, offline (no route out)."""
        import egress_ctl; n = egress_ctl.net_name(id)
        if (await self._d("network", "inspect", n))[0] == 0: await (self.egress or egress_ctl.default()).attach_net(n)

    async def delete(self, id):
        check_id(id); vw, vo = vols(id)
        await self._d("rm", "-f", cname(id), timeout=60)
        import egress_ctl; await egress_ctl.DockerNet().remove(egress_ctl.net_name(id))      # 11.45: its private egress network (no-op for a box without Internet)
        for v in (vw, vo): await self._d("volume", "rm", "-f", v, timeout=60)

    async def exec(self, id, cmd, *, cwd="/workspace", env=None, timeout=60, command_id=None, stdin=None):
        c = cname(check_id(id)); command_id = command_id or uuid.uuid4().hex[:16]
        if not isinstance(cmd, str) or not cmd.strip() or len(cmd) > MAX_CMD: raise SbxError("bad command")
        if not re.fullmatch(r"[0-9a-f]{16}", command_id): raise SbxError("bad command id")
        cwd, env, timeout = box_path(cwd), check_env(env), max(1, min(int(timeout), 3600)); mark = "__AOS_" + secrets.token_hex(8) + "__"
        a = ["exec", "-i", "-w", cwd, *[x for k, v in env.items() for x in ("-e", f"{k}={v}")], c, "bash", "-c", _WRAP, "_", command_id, str(timeout), mark,
             "timeout", "-s", "KILL", str(timeout), "bash", "-lc", cmd]
        t0 = time.monotonic()
        rc, out, err = await self._d(*a, timeout=timeout + 20, stdin=stdin or b"")
        ms = int((time.monotonic() - t0) * 1000)
        if rc == 124: return ExecResult(command_id, 137, "", False, True, ms)   # the docker client hung past the limit
        text = out.decode(errors="replace"); i = text.rfind("\n" + mark)
        if rc or i < 0: raise BoxFailed("exec failed: " + (err or text)[:300])
        parts = text[i + 1:].split(); code, size = int(parts[1]), int(parts[2])
        return ExecResult(command_id, code, text[:i], size > OUT_CAP, code == 137, ms)

    async def put(self, id, path, data):
        c, path = cname(check_id(id)), box_path(path)
        if len(data) > PUT_MAX: raise SbxError("file too large")
        rc, _, err = await self._d("exec", "-i", c, "bash", "-c", 'mkdir -p -- "$(dirname -- "$1")" && cat > "$1"', "_", path, stdin=data, timeout=120)
        if rc: raise SbxError("write failed: " + err[:200])

    async def get(self, id, path):
        c, path = cname(check_id(id)), box_path(path)
        rc, out, err = await self._d("exec", c, "bash", "-c", '[ -f "$1" ] || exit 44; stat -c %s -- "$1"; head -c ' + str(PUT_MAX + 1) + ' -- "$1"', "_", path, timeout=120)
        if rc == 44: raise NotFound("no such file")
        if rc: raise SbxError("read failed: " + err[:200])
        nl = out.index(b"\n"); size, data = int(out[:nl]), out[nl + 1:]
        if size > PUT_MAX: raise SbxError("file too large")
        return data

    async def inspect(self, id):
        c = cname(check_id(id))
        rc, out, _ = await self._d("inspect", c)
        if rc: return {"state": "missing"}
        j = json.loads(out)[0]; st = j["State"]
        info = {"state": "running" if st.get("Running") else "exited", "runtime": j["HostConfig"].get("Runtime"), "exit_code": st.get("ExitCode"),
                "oom": bool(st.get("OOMKilled")), "started_at": st.get("StartedAt"), "image": j["Config"].get("Image")}
        if st.get("Running"):
            info["boot_done"] = (await self._d("exec", c, "test", "-f", "/run/aos-boot-done", timeout=15))[0] == 0
            rc, e, _ = await self._d("exec", c, "cat", "/workspace/.aos/boot_error", timeout=15)
            info["boot_error"] = e.decode(errors="replace").strip()[:500] if rc == 0 else ""
        return info

    async def stats(self, id):
        rc, out, _ = await self._d("stats", "--no-stream", "--format", "{{json .}}", cname(check_id(id)), timeout=30)
        try: j = json.loads(out)
        except ValueError: return {}
        return {"cpu_percent": j.get("CPUPerc"), "memory": j.get("MemUsage"), "pids": j.get("PIDs")}

    async def disk_usage(self):
        """{box_id: bytes} = writable layer of the container (the durable root FS lives there) + its two volumes. One `docker system df -v` call."""
        rc, out, _ = await self._d("system", "df", "-v", "--format", "json", timeout=120)
        if rc: raise BoxFailed("docker system df failed")
        j, use = json.loads(out), {}
        for c in j.get("Containers") or []:
            m = re.fullmatch(r"aos-box-([0-9a-f]{16})", c.get("Names", ""))
            if m: use[m.group(1)] = use.get(m.group(1), 0) + parse_size(c.get("Size"))
        for v in j.get("Volumes") or []:
            m = re.fullmatch(r"aos-box-([0-9a-f]{16})-(workspace|opt)", v.get("Name", ""))
            if m: use[m.group(1)] = use.get(m.group(1), 0) + parse_size(v.get("Size"))
        return use

    def capabilities(self):
        return {"core": "gvisor", "isolation": "gvisor (runsc)", "network": "none", "durable_root": True, "durable_volumes": ["/workspace", "/opt/aos-user"],
                "disk_enforced": False, "disk_watchdog": True, "snapshots": False, "sizes": sorted(SIZES)}

class FakeDriver(Driver):
    """In-memory core for unit tests (no Docker). Files live in a dict and survive stop/start like the real volumes + durable root.
    exec understands: echo X | pwd | cat PATH | write PATH TEXT | big N (N bytes of 'x') | sleep S | exit N | true | false | env NAME; anything else exits 127."""
    name = "fake"
    def __init__(self): self.boxes, self.fail, self.disk = {}, {}, {}
    def _b(self, id):
        b = self.boxes.get(check_id(id))
        if not b: raise NotFound("no such computer")
        return b
    def _boom(self, op):
        if self.fail.get(op): raise BoxFailed(f"fake {op} failure")
    async def create(self, spec):
        self._boom("create")
        if spec.id in self.boxes: raise BoxFailed("exists")
        self.boxes[spec.id] = {"state": "created", "files": {}, "size": spec.size, "image": spec.image or IMAGE, "args": run_args(spec), "boot_error": ""}
    async def start(self, id, wait_boot=False): self._boom("start"); self._b(id)["state"] = "running"
    async def stop(self, id): self._boom("stop"); self._b(id)["state"] = "exited"
    async def delete(self, id): self.boxes.pop(check_id(id), None)
    async def exec(self, id, cmd, *, cwd="/workspace", env=None, timeout=60, command_id=None, stdin=None):
        import shlex
        b = self._b(id); command_id = command_id or uuid.uuid4().hex[:16]; cwd, env = box_path(cwd), check_env(env)
        if b["state"] != "running": raise BoxFailed("box is not running")
        if not isinstance(cmd, str) or not cmd.strip() or len(cmd) > MAX_CMD: raise SbxError("bad command")
        out, code, timed_out = b"", 0, False
        try: w = shlex.split(cmd)
        except ValueError: w = [""]
        try: out, code, timed_out = await self._fake_run(b, w, cwd, env, timeout)
        except (IndexError, ValueError): out, code = b"bash: syntax error\n", 2
        b["files"][f"/workspace/.aos/out/{command_id}.log"] = out
        return ExecResult(command_id, code, out[:OUT_CAP].decode(errors="replace"), len(out) > OUT_CAP, timed_out, 1)
    async def _fake_run(self, b, w, cwd, env, timeout):
        out, code, timed_out = b"", 0, False
        if w[0] == "echo": out = (" ".join(w[1:]) + "\n").encode()
        elif w[0] == "pwd": out = (cwd + "\n").encode()
        elif w[0] == "env": out = (env.get(w[1], "") + "\n").encode()
        elif w[0] == "cat": p = box_path(w[1], cwd); out, code = (b["files"][p], 0) if p in b["files"] else (b"cat: no such file\n", 1)
        elif w[0] == "write": b["files"][box_path(w[1], cwd)] = " ".join(w[2:]).encode()
        elif w[0] == "big": out = b"x" * int(w[1])
        elif w[0] == "sleep": timed_out = float(w[1]) > timeout; code = 137 if timed_out else 0
        elif w[0] == "exit": code = int(w[1])
        elif w[0] == "true": pass
        elif w[0] == "false": code = 1
        else: out, code = b"bash: command not found\n", 127
        return out, code, timed_out
    async def put(self, id, path, data):
        b = self._b(id)
        if len(data) > PUT_MAX: raise SbxError("file too large")
        b["files"][box_path(path)] = bytes(data)
    async def get(self, id, path):
        b, p = self._b(id), box_path(path)
        if p not in b["files"]: raise NotFound("no such file")
        return b["files"][p]
    async def inspect(self, id):
        b = self._b(id); return {"state": "running" if b["state"] == "running" else "exited", "runtime": "fake", "boot_done": True, "boot_error": b["boot_error"], "image": b["image"]}
    async def stats(self, id): self._b(id); return {"cpu_percent": "0%", "memory": "0B / 1GiB", "pids": "1"}
    async def disk_usage(self): return dict(self.disk)
    def capabilities(self): return {"core": "fake", "isolation": "none (test double)", "network": "none", "durable_root": True, "snapshots": False, "sizes": sorted(SIZES)}

DRIVERS = {"gvisor": DockerGvisorDriver, "fake": FakeDriver}

def select_core(kind="computer", info=None):
    """The core a new box of `kind` runs on. kind != computer: None (old folder sandboxes are untouched).
    AOS_SBX_CORE = auto | gvisor; any other value -> 501 "core not built". The computer needs REAL gVisor: sandbox2 reports `A.SBX_INFO["runtime"]` starting with 'gvisor'
    (runc / fake runtime -> 503 + Retry-After). Never returns a runc core."""
    if kind != "computer": return None
    want = (os.environ.get("AOS_SBX_CORE") or "auto").strip().lower()
    if want not in ("auto", "gvisor"): raise CoreNotBuilt(f"core {want!r} is not built in this version (available: auto, gvisor)")
    if info is None:
        import deepagent_api as A; info = A.SBX_INFO
    if not str(info.get("runtime", "")).startswith("gvisor"):
        raise CoreUnavailable("the virtual computer needs gVisor (runsc), which is not available on this server (it never runs on runc)", retry_after=30)
    return "gvisor"

_DRV: dict = {}
def get_driver(core):
    if core not in DRIVERS: raise CoreNotBuilt(f"core {core!r} is not built in this version")
    if core == "fake": return DRIVERS[core]()
    return _DRV.setdefault(core, DRIVERS[core]())

# ---------------------------------------------------------------- stores (rows of `sandboxes` with kind='computer')
_FIELDS = ("name", "owner_id", "user_id", "agent_id", "thread_id", "scope", "size", "image", "core", "status", "last_error", "host_state", "idle_ttl_s", "delete_after_stop_s", "last_active")

class MemStore:
    """Dict store for unit tests; mirrors PgStore (including the compare-and-set on status)."""
    def __init__(self): self.rows, self.cmds, self.quotas, self.usage, self.secrets, self.identifiers = {}, [], {}, {}, [], {}   # secrets: [(workspace_id, name)]; identifiers: {agent_id: str}
    async def insert(self, row): self.rows[row["id"]] = dict(row, kind="computer", last_active=time.time(), host_state=row.get("host_state") or {})
    async def get(self, id): r = self.rows.get(id); return dict(r) if r and r["status"] != "deleted" else None
    async def cas(self, id, old, **f):
        r = self.rows.get(id)
        if not r or r["status"] != old: return False
        r.update(f); return True
    async def touch(self, id, **f): self.rows[id].update(f, last_active=time.time())
    async def list(self, ws): return [dict(r) for r in self.rows.values() if r["workspace_id"] == ws and r["status"] != "deleted"]
    async def list_running(self): return [dict(r) for r in self.rows.values() if r["status"] == "running"]
    async def count(self, ws, statuses): return sum(1 for r in self.rows.values() if r["workspace_id"] == ws and r["status"] in statuses)
    async def quota(self, ws): return self.quotas.get(ws)
    async def add_command(self, c): self.cmds.append(c)
    # ---- 11.42
    def system(self): return self                      # no row-level security here
    async def list_active(self): return [dict(r) for r in self.rows.values() if r["status"] in ("provisioning", "running")]
    async def list_stopped(self): return [dict(r) for r in self.rows.values() if r["status"] == "stopped"]
    async def add_usage(self, hour, ws, sid, cpu, mem, run, disk):
        u = self.usage.setdefault((hour, sid), {"hour": hour, "workspace_id": ws, "sandbox_id": sid, "cpu_seconds": 0.0, "mem_byte_seconds": 0.0, "running_seconds": 0.0, "disk_bytes_max": 0})
        u["cpu_seconds"] += cpu; u["mem_byte_seconds"] += mem; u["running_seconds"] += run; u["disk_bytes_max"] = max(u["disk_bytes_max"], disk)
    async def purge_rows(self, agent_id=None, owner_id=None, workspace_id=None):
        if agent_id is None and owner_id is None: raise ValueError("purge needs an agent or an owner")
        return [dict(r) for r in self.rows.values() if (agent_id is None or r.get("agent_id") == agent_id) and (owner_id is None or owner_id in (r.get("owner_id"), r.get("user_id")))
                and (workspace_id is None or r["workspace_id"] == workspace_id)]
    async def purge_row(self, id):
        self.rows.pop(id, None); self.cmds[:] = [c for c in self.cmds if c["sandbox_id"] != id]
    async def agent_identifier(self, agent_id): return self.identifiers.get(agent_id)
    async def purge_secrets(self, workspace_id, prefixes):
        gone = [s for s in self.secrets if (workspace_id is None or s[0] == workspace_id) and any(s[1].startswith(p) for p in prefixes)]
        self.secrets[:] = [s for s in self.secrets if s not in gone]; return len(gone)

class PgStore:
    """`q` = deepagent_api.q (runs under the caller's RLS context; the obs worker runs as system and sees all workspaces)."""
    def __init__(self, q=None):
        if q is None:
            import deepagent_api as A; q = A.q
        self.q = q
    async def insert(self, r):
        await self.q("INSERT INTO sandboxes(id,name,workspace_id,owner_id,kind,scope,agent_id,user_id,thread_id,status,size,image,core,host_state,idle_ttl_s,delete_after_stop_s) "
                     "VALUES(%s,%s,%s,%s,'computer',%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)",
                     (r["id"], r["name"], r["workspace_id"], r["owner_id"], r.get("scope"), r.get("agent_id"), r.get("user_id"), r.get("thread_id"), r["status"],
                      r["size"], r["image"], r["core"], json.dumps(r.get("host_state") or {}), r.get("idle_ttl_s"), r.get("delete_after_stop_s")))
    async def get(self, id): return await self.q("SELECT * FROM sandboxes WHERE id=%s AND kind='computer' AND status<>'deleted'", (id,), one=True)
    async def cas(self, id, old, **f):
        sets, args = [], []
        for k, v in f.items():
            if k not in _FIELDS: raise ValueError(k)
            sets.append(f"{k}=%s::jsonb" if k == "host_state" else f"{k}=%s"); args.append(json.dumps(v) if k == "host_state" else v)
        r = await self.q(f"UPDATE sandboxes SET {','.join(sets)} WHERE id=%s AND kind='computer' AND status=%s RETURNING id", (*args, id, old), one=True)
        return bool(r)
    async def touch(self, id, **f):
        sets, args = ["last_active=now()", "last_used=now()"], []
        for k, v in f.items():
            if k not in _FIELDS: raise ValueError(k)
            sets.append(f"{k}=%s::jsonb" if k == "host_state" else f"{k}=%s"); args.append(json.dumps(v) if k == "host_state" else v)
        await self.q(f"UPDATE sandboxes SET {','.join(sets)} WHERE id=%s AND kind='computer'", (*args, id))
    async def list(self, ws): return await self.q("SELECT * FROM sandboxes WHERE workspace_id=%s AND kind='computer' AND status<>'deleted' ORDER BY created DESC", (ws,))
    async def list_running(self): return await self.q("SELECT * FROM sandboxes WHERE kind='computer' AND status='running'")
    async def count(self, ws, statuses): return (await self.q("SELECT count(*) n FROM sandboxes WHERE workspace_id=%s AND kind='computer' AND status = ANY(%s)", (ws, list(statuses)), one=True))["n"]
    async def quota(self, ws): return await self.q("SELECT * FROM workspace_sandbox_quotas WHERE workspace_id=%s", (ws,), one=True)
    async def add_command(self, c):
        await self.q("INSERT INTO sandbox_commands(id,sandbox_id,workspace_id,user_id,command,cwd,status,exit_code,truncated,duration_ms) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                     (c["id"], c["sandbox_id"], c["workspace_id"], c.get("user_id"), c["command"][:4000], c.get("cwd"), c["status"], c.get("exit_code"), c.get("truncated", False), c.get("duration_ms")))
    # ---- 11.42
    def system(self): return PgStore(q=_sys_q)         # the same queries as the obs worker / reaper: all workspaces (the caller's RLS context is not applied)
    async def list_active(self): return await self.q("SELECT * FROM sandboxes WHERE kind='computer' AND status IN ('provisioning','running')")
    async def list_stopped(self): return await self.q("SELECT * FROM sandboxes WHERE kind='computer' AND status='stopped'")
    async def add_usage(self, hour, ws, sid, cpu, mem, run, disk):
        await self.q("INSERT INTO sandbox_usage_hourly(hour,workspace_id,sandbox_id,cpu_seconds,mem_byte_seconds,running_seconds,disk_bytes_max) VALUES(to_timestamp(%s),%s,%s,%s,%s,%s,%s) "
                     "ON CONFLICT (hour, sandbox_id) DO UPDATE SET cpu_seconds=sandbox_usage_hourly.cpu_seconds+EXCLUDED.cpu_seconds, mem_byte_seconds=sandbox_usage_hourly.mem_byte_seconds+EXCLUDED.mem_byte_seconds, "
                     "running_seconds=sandbox_usage_hourly.running_seconds+EXCLUDED.running_seconds, disk_bytes_max=GREATEST(sandbox_usage_hourly.disk_bytes_max, EXCLUDED.disk_bytes_max)",
                     (hour, ws, sid, cpu, mem, run, disk))
    async def purge_rows(self, agent_id=None, owner_id=None, workspace_id=None):
        if agent_id is None and owner_id is None: raise ValueError("purge needs an agent or an owner")
        return await self.q("SELECT id, kind, core, workspace_id FROM sandboxes WHERE (%s::int IS NULL OR agent_id=%s) AND (%s::int IS NULL OR owner_id=%s OR user_id=%s) "
                            "AND (%s::int IS NULL OR workspace_id=%s)", (agent_id, agent_id, owner_id, owner_id, owner_id, workspace_id, workspace_id))
    async def purge_row(self, id): await self.q("DELETE FROM sandboxes WHERE id=%s", (id,))     # sandbox_commands go with it (ON DELETE CASCADE); usage rows stay (accounting)
    async def agent_identifier(self, agent_id):
        try: r = await self.q("SELECT identifier FROM agents WHERE id=%s", (agent_id,), one=True)     # column arrives with part A (F19); absent before that
        except Exception: return None  # noqa: BLE001
        return (r or {}).get("identifier")
    async def purge_secrets(self, workspace_id, prefixes):
        n = 0
        for p in prefixes:
            n += len(await self.q("DELETE FROM ws_secrets WHERE left(name, length(%s)) = %s AND (%s::int IS NULL OR workspace_id=%s) RETURNING 1", (p, p, workspace_id, workspace_id)))
        return n

async def _sys_q(sql, args=(), one=False):
    """deepagent_api.q with the SYSTEM context (all workspaces, as the obs worker): for the reaper, the capacity gate and the purge, which must see every workspace's boxes."""
    import deepagent_api as A
    async with A.S["pool"].connection() as c:
        await c.execute("SELECT set_config('app.ws',%s,false), set_config('app.uid',%s,false), set_config('app.role',%s,false), set_config('role', CASE WHEN %s ~ '^[0-9]+$' THEN 'aos_rls' ELSE 'none' END, false)", ("*", "0", "admin", "*"))
        cur = await c.execute(sql, args)
        if cur.description is None: return None
        return await (cur.fetchone() if one else cur.fetchall())

# ---------------------------------------------------------------- permissions
def ctx_from_admin():
    """The caller (admin.USER) as the plain dict `Computers` checks: {ws, uid, role, demo, scope}."""
    import admin, gateway as gw
    u = admin.cur() or {}; ws = int(admin.cur_ws() or 1)
    return {"ws": ws, "uid": u.get("id"), "role": u.get("role") or "viewer", "demo": bool(admin.is_demo(u) or ws in gw.DEMO_WS), "scope": u.get("scope") or "full"}

def _rank(ctx): return ROLE_RANK.get(ctx.get("role"), 0)

def authorize(ctx, row, write):
    """Raise unless `ctx` may use `row`. Another workspace and another user's box (non-admin) = NotFound (same as RLS: existence is not revealed);
    write needs role >= editor, no demo account, no read-only API key."""
    if not ctx or ctx.get("ws") is None or ctx.get("uid") is None: raise Forbidden("not signed in")
    if not row or int(row["workspace_id"]) != int(ctx["ws"]): raise NotFound("no such computer")
    if _rank(ctx) < ROLE_RANK["viewer"]: raise Forbidden("no role in this workspace")
    if _rank(ctx) < ROLE_RANK["admin"] and ctx["uid"] not in (row.get("owner_id"), row.get("user_id")): raise NotFound("no such computer")
    if write:
        if ctx.get("demo"): raise Forbidden("virtual computers are off for demo accounts")
        if _rank(ctx) < ROLE_RANK["editor"]: raise Forbidden("viewers cannot change a computer")
        if ctx.get("scope") == "read": raise Forbidden("read-only API key")
    return row

def host_disk_pct(path=None):
    path = path or os.environ.get("AOS_DOCKER_ROOT") or ("/var/lib/docker" if os.path.isdir("/var/lib/docker") else "/")
    try: u = shutil.disk_usage(path)
    except OSError: u = shutil.disk_usage("/")
    return 100.0 * u.used / u.total

class Computers:
    """Lifecycle + permission layer. `driver=None` -> the core chosen per call by select_core() (production); tests pass a FakeDriver.
    11.42: idle stop / auto-wake / delete-after-stop (`reap_tick`), capacity gate before every start (`_gate`), reconcile, usage sampler, purge hooks.
    Keep ONE instance per process in the API (the per-box locks and the busy counters live on the instance)."""
    def __init__(self, store=None, driver=None, disk_probe=host_disk_pct, clock=time.time, start_timeout=15.0, sbx_root=None, egress=None):
        self.store, self._driver, self.disk_probe, self._locks, self.egress = store or PgStore(), driver, disk_probe, {}, egress
        self.sys = self.store.system()                  # same data, but not filtered by the caller's workspace (reaper, capacity gate, purge)
        self.clock, self.start_timeout, self.sbx_root = clock, start_timeout, sbx_root
        self._busy, self._sample_t, self._cap_lock = {}, {}, asyncio.Lock()
    def _drv(self, core=None):
        return self._driver if self._driver is not None else get_driver(core or select_core("computer"))
    def _lock(self, id): return self._locks.setdefault(id, asyncio.Lock())
    def _egress(self):
        if self.egress is None:
            import egress_ctl; self.egress = egress_ctl.default()
        return self.egress

    async def _egress_prepare(self, i, internet, allow_list, quota):
        """11.45: validate the Internet request, cap it by the workspace ceiling, create the box's private network + credential -> host_state['egress'] (None for `off`)."""
        import egress_ctl, egress_policy as EP
        if internet not in EP.MODES: raise SbxError(f"internet must be one of {list(EP.MODES)}")
        if internet == "off": return None
        try: lst = EP.validate_list(allow_list) if internet == "custom" else []
        except EP.BadEntry as e: raise SbxError("bad allow-list: " + str(e)) from None
        ceiling = (quota.get("internet") or "off") if quota else egress_ctl.default_ceiling()
        if EP.mode_rank(internet) > EP.mode_rank(ceiling): raise Forbidden(f"this workspace allows Internet access up to '{ceiling}' for virtual computers")
        try: return await self._egress().provision(i, internet, lst)
        except egress_ctl.EgressError as e: raise Capacity("cannot set up the Internet access of this computer: " + str(e)[:160], retry_after=60) from None

    async def _row(self, ctx, id, write):
        return authorize(ctx, await self.store.get(check_id(id)), write)

    # ---- capacity ------------------------------------------------------------------------------------------------------------
    def _headroom(self, need_mb):
        import capacity
        try: return capacity.headroom_ok(need_mb)       # MemAvailable - 512 MiB reserve >= the box's RAM
        except OSError: return True                      # no /proc/meminfo (not Linux): do not block

    async def _stop_box(self, r, now=None, idle=True):
        """Stop a RUNNING box for the system (idle reaper / capacity). Never while a command runs. idle=True marks it as wake-able by the next call."""
        async with self._lock(r["id"]):
            cur = await self.sys.get(r["id"])
            if not cur or cur["status"] != "running" or self._busy.get(r["id"]): return False
            await self._drv(cur["core"]).stop(cur["id"])
            hs = dict(cur.get("host_state") or {}); hs["stopped_at"] = now or self.clock()
            if idle: hs["idle_stop"] = True
            return await self.sys.cas(cur["id"], "running", status="stopped", host_state=hs)

    async def _gate(self, ws, size, exclude=None):
        """Before EVERY start (call it under self._cap_lock). Workspace switch + per-workspace running quota, then the host: running < AOS_COMPUTER_MAX_RUNNING and enough
        free RAM; if not, the least-recently-used idle box is stopped (it wakes by itself on its next call); none left -> 429 + Retry-After."""
        quota = await self.store.quota(ws)
        if quota is not None and not quota["enabled"]: raise Forbidden(OFF_MSG)
        mx_run = quota["max_running"] if quota else int(os.environ.get("AOS_BOX_MAX_RUNNING", "3"))
        if await self.store.count(ws, ("provisioning", "running")) >= mx_run: raise QuotaExceeded("running-computer limit of this workspace reached")
        need, cap, tried = box_ram_mb(size), max_running_default(), set()
        while True:
            active = [r for r in await self.sys.list_active() if r["id"] != exclude]
            if len(active) < cap and self._headroom(need): return
            idle = sorted((r for r in active if r["status"] == "running" and not self._busy.get(r["id"]) and r["id"] not in tried), key=lambda r: _ts(r["last_active"]))
            if not idle: raise QuotaExceeded("the server is full: every computer is busy, try again in a moment", retry_after=30)
            tried.add(idle[0]["id"])
            try: await self._stop_box(idle[0], idle=True)
            except Exception as e: print(f"[sbx] capacity stop of {idle[0]['id']} failed: {str(e)[:200]}", flush=True)  # noqa: BLE001

    async def create(self, ctx, *, name="computer", size="small", scope="thread", agent_id=None, thread_id=None, user_id=None, image=None,
                     idle_ttl_s=None, delete_after_stop_s=None, ro_mounts=(), start=True, internet="off", allow_list=()):
        authorize(ctx, {"workspace_id": ctx.get("ws"), "owner_id": ctx.get("uid")} if ctx else None, True)
        if size not in SIZES: raise SbxError(f"size must be one of {sorted(SIZES)}")
        if scope not in ("thread", "agent"): raise SbxError("scope must be thread or agent")
        check_ttls(idle_ttl_s, delete_after_stop_s)
        core = select_core("computer") if self._driver is None else self._driver.name       # refuses (503) without gVisor, before anything is stored
        if self.disk_probe() > 85: raise Capacity("the server disk is more than 85 % full: no new virtual computers for now", retry_after=300)
        ws, quota = int(ctx["ws"]), await self.store.quota(int(ctx["ws"]))
        if quota is not None and not quota["enabled"]: raise Forbidden(OFF_MSG)
        mx_total = quota["max_total"] if quota else int(os.environ.get("AOS_BOX_MAX_TOTAL", "10"))
        if await self.store.count(ws, ("provisioning", "running", "stopped", "failed")) >= mx_total: raise QuotaExceeded("computer limit of this workspace reached")
        i = new_id(); image = image or IMAGE
        eg = await self._egress_prepare(i, internet, allow_list, quota)      # 11.45: None = `--network none`
        row = {"id": i, "name": (name or "computer").strip()[:80] or "computer", "workspace_id": ws, "owner_id": ctx["uid"], "user_id": user_id, "agent_id": agent_id,
               "thread_id": thread_id, "scope": scope, "size": size, "image": image, "core": core, "status": "provisioning", "idle_ttl_s": idle_ttl_s, "delete_after_stop_s": delete_after_stop_s,
               "host_state": {"egress": eg} if eg else {}}
        try:
            async with self._cap_lock:          # the gate and the 'provisioning' row are one step: two parallel creates cannot both take the last slot
                if start: await self._gate(ws, size)
                await self.store.insert(row)
        except BaseException:
            if eg: await self._egress().release(eg, i)
            raise
        d = self._drv(core)
        try:
            await d.create(BoxSpec(i, size, image, ws, tuple(ro_mounts), net=eg))
            if start: await d.start(i); await self.store.cas(i, "provisioning", status="running")
            else: await self.store.cas(i, "provisioning", status="stopped", host_state={"stopped_at": self.clock()})
        except Exception as e:  # noqa: BLE001
            await self.store.cas(i, "provisioning", status="failed", last_error=str(e)[:300])
            try: await d.delete(i)
            except Exception: pass  # noqa: BLE001
            if eg: await self._egress().release(eg, i)
            raise
        return await self.store.get(i)

    async def get(self, ctx, id): return await self._row(ctx, id, False)

    async def list(self, ctx):
        if _rank(ctx) < ROLE_RANK["viewer"] or ctx.get("uid") is None: raise Forbidden("no role in this workspace")
        rows = await self.store.list(int(ctx["ws"]))
        return [r for r in rows if _rank(ctx) >= ROLE_RANK["admin"] or ctx["uid"] in (r.get("owner_id"), r.get("user_id"))]

    async def _start_locked(self, r, wait_boot=False):
        """Gate + start + CAS. The caller holds _lock(box). A start slower than start_timeout (15 s) is retried once."""
        async with self._cap_lock:
            await self._gate(int(r["workspace_id"]), r["size"], exclude=r["id"])
            d = self._drv(r["core"])
            try: await asyncio.wait_for(d.start(r["id"], wait_boot=wait_boot), self.start_timeout)
            except asyncio.TimeoutError: await d.start(r["id"], wait_boot=wait_boot)
            hs = {k: v for k, v in (r.get("host_state") or {}).items() if k not in ("idle_stop", "stopped_at")}
            if not await self.store.cas(r["id"], r["status"], status="running", last_error=None, host_state=hs): raise BadState("state changed, try again")
            await self.store.touch(r["id"])      # the idle clock starts now, not at the last command before the stop

    async def start(self, ctx, id, wait_boot=False):
        r = await self._row(ctx, id, True)
        async with self._lock(r["id"]):
            if r["status"] not in ("stopped", "failed"): raise BadState(f"cannot start a computer that is {r['status']}")
            await self._start_locked(r, wait_boot)
        return await self.store.get(r["id"])

    async def _wake(self, r):
        """A box the system stopped (idle / capacity) starts again on the next call, files intact. A box the USER stopped (or that failed) stays stopped."""
        if r["status"] != "stopped" or not (r.get("host_state") or {}).get("idle_stop"): return r
        async with self._lock(r["id"]):
            cur = await self.store.get(r["id"])
            if cur and cur["status"] == "stopped" and (cur.get("host_state") or {}).get("idle_stop"): await self._start_locked(cur)
            return await self.store.get(r["id"]) or r

    async def stop(self, ctx, id):
        r = await self._row(ctx, id, True)
        async with self._lock(r["id"]):
            if r["status"] != "running": raise BadState(f"cannot stop a computer that is {r['status']}")
            await self._drv(r["core"]).stop(r["id"])
            hs = {k: v for k, v in (r.get("host_state") or {}).items() if k != "idle_stop"}; hs["stopped_at"] = self.clock()
            if not await self.store.cas(r["id"], "running", status="stopped", host_state=hs): raise BadState("state changed, try again")
        return await self.store.get(r["id"])

    async def delete(self, ctx, id):
        r = await self._row(ctx, id, True)
        async with self._lock(r["id"]):
            await self._drv(r["core"]).delete(r["id"])     # the container and both volumes are gone before the row says so
            eg = (r.get("host_state") or {}).get("egress")
            if eg: await self._egress().release(eg, r["id"])      # 11.45: private network + cached credential
            await self.store.cas(r["id"], r["status"], status="deleted", host_state={})
        return {"ok": True}

    async def exec(self, ctx, id, cmd, *, cwd="/workspace", env=None, timeout=60, stdin=None, record=True):
        """record=False (deepagents helper scripts with base64 payloads): the command is neither stored in sandbox_commands nor reported in SPAN_ATTRS."""
        r = await self._row(ctx, id, True); before = r["status"]
        r = await self._wake(r)
        if r["status"] != "running": raise BadState(f"the computer is {r['status']}, start it first")
        self._busy[r["id"]] = self._busy.get(r["id"], 0) + 1       # the idle reaper and the capacity gate never stop a box that runs a command
        try:
            await self.store.touch(r["id"])
            eg = (r.get("host_state") or {}).get("egress")
            if eg: env = {**self._egress().proxy_env(eg, r["id"]), **(env or {})}      # 11.45: per-command proxy variables (the box's own credential); never stored in the command history
            try: res = await self._drv(r["core"]).exec(r["id"], cmd, cwd=cwd, env=env, timeout=timeout, stdin=stdin)
            except BoxFailed:
                await self.refresh(r["id"])      # the sandbox may have died (e.g. a fork bomb hit the pids limit): the row must say 'stopped', not 'running'
                raise
        finally:
            self._busy[r["id"]] -= 1
            if self._busy[r["id"]] <= 0: self._busy.pop(r["id"], None)
        await self.store.touch(r["id"])
        if not record: return res.as_dict()
        await self.store.add_command({"id": res.command_id, "sandbox_id": r["id"], "workspace_id": r["workspace_id"], "user_id": ctx["uid"], "command": cmd, "cwd": cwd,
                                      "status": "timeout" if res.timed_out else "done", "exit_code": res.exit_code, "truncated": res.truncated, "duration_ms": res.duration_ms})
        attrs = {"sandbox.id": r["id"], "sandbox.kind": "computer", "sandbox.status_before": before, "sandbox.exit_code": res.exit_code, "sandbox.duration_ms": res.duration_ms}
        cur = SPAN_ATTRS.get()
        if isinstance(cur, dict): cur.update(attrs)      # a tool wrapper put a holder here: the update is visible to it even when the tool ran in a copied context
        else: SPAN_ATTRS.set(attrs)
        return res.as_dict()

    async def put_file(self, ctx, id, path, data):
        r = await self._wake(await self._row(ctx, id, True))
        if r["status"] != "running": raise BadState(f"the computer is {r['status']}, start it first")
        await self._drv(r["core"]).put(r["id"], path, data); await self.store.touch(r["id"]); return {"ok": True, "bytes": len(data)}

    async def get_file(self, ctx, id, path):
        r = await self._row(ctx, id, False)
        try: authorize(ctx, r, True); r = await self._wake(r)       # reading wakes an idle box only for someone who may start it
        except SbxError: pass
        if r["status"] != "running": raise BadState(f"the computer is {r['status']}, start it first")
        return await self._drv(r["core"]).get(r["id"], path)

    async def refresh(self, id):
        """System call (no user): copy aos-boot's verdict (setup.sh failed / 120 s limit) into last_error. A failed setup never stops the box."""
        r = await self.store.get(check_id(id))
        if not r or r["status"] != "running": return None
        info = await self._drv(r["core"]).inspect(r["id"])
        if info.get("state") != "running":
            await self.store.cas(r["id"], "running", status="stopped", last_error=r.get("last_error") or "container stopped", host_state={**(r.get("host_state") or {}), "stopped_at": self.clock()}); return None
        err = info.get("boot_error") or None
        if info.get("boot_done") and err != r.get("last_error"): await self.store.touch(r["id"], last_error=err)
        return info

    async def disk_tick(self, now=None):
        """Watchdog (every 5 min from the obs worker). Per running box: >= 80 % of AOS_BOX_DISK_GB -> host_state.disk_warn; >= 100 % -> stop with last_error='disk quota'.
        Also records the peak disk bytes of the hour in sandbox_usage_hourly. System call: sees every workspace. -> list of events (also useful in tests)."""
        rows = await self.store.list_running()
        if not rows: return []
        limit, events, usage = disk_limit_bytes(), [], {}
        for core in {r["core"] for r in rows}:
            try: usage.update(await self._drv(core).disk_usage())
            except Exception as e: events.append({"core": core, "event": "df_failed", "error": str(e)[:200]})  # noqa: BLE001
        hour = int((now or self.clock()) // 3600 * 3600)
        for r in rows:
            used = usage.get(r["id"])
            if used is None: continue
            try: await self.sys.add_usage(hour, int(r["workspace_id"]), r["id"], 0, 0, 0, used)
            except Exception as e: events.append({"id": r["id"], "event": "usage_failed", "error": str(e)[:200]})  # noqa: BLE001
            hs = dict(r.get("host_state") or {})
            if used >= limit:
                try: await self._drv(r["core"]).stop(r["id"])
                except Exception as e: events.append({"id": r["id"], "event": "stop_failed", "error": str(e)[:200]}); continue  # noqa: BLE001
                hs.update(disk_bytes=used, disk_limit=limit, stopped_at=self.clock()); hs.pop("disk_warn", None); hs.pop("idle_stop", None)
                if await self.store.cas(r["id"], "running", status="stopped", last_error="disk quota", host_state=hs): events.append({"id": r["id"], "event": "stopped", "used": used})
            elif used >= 0.8 * limit:
                if not hs.get("disk_warn"):
                    hs.update(disk_warn=True, disk_bytes=used, disk_limit=limit); await self.store.touch(r["id"], host_state=hs)
                    print(f"[sbx] computer {r['id']} uses {used >> 20} MB of {limit >> 20} MB (>= 80 %)", flush=True); events.append({"id": r["id"], "event": "warn", "used": used})
            elif hs.get("disk_warn"):
                hs.pop("disk_warn", None); await self.store.touch(r["id"], host_state=hs)
        return events

    # ---- 11.42 background work (API process, see reaper_loop) -----------------------------------------------------------------
    async def reap_tick(self, now=None):
        """Idle boxes stop after idle_ttl_s (default AOS_BOX_IDLE_TTL_S = 600; never while a command runs). Stopped boxes with delete_after_stop_s are deleted that long
        after they stopped; a per-agent shared box (scope 'agent') is never deleted automatically. A 'failed' box is never touched (the owner decides)."""
        now, events = now or self.clock(), []
        for r in await self.sys.list_running():
            if now - _ts(r["last_active"]) < (r.get("idle_ttl_s") or idle_ttl_default()) or self._busy.get(r["id"]): continue
            try:
                if await self._stop_box(r, now=now, idle=True): events.append({"id": r["id"], "event": "idle_stop"})
            except Exception as e: events.append({"id": r["id"], "event": "stop_failed", "error": str(e)[:200]})  # noqa: BLE001
        for r in await self.sys.list_stopped():
            ds = r.get("delete_after_stop_s")
            if not ds or r.get("scope") == "agent": continue
            if now - (float((r.get("host_state") or {}).get("stopped_at") or 0) or _ts(r["last_active"])) < ds: continue
            try:
                async with self._lock(r["id"]):
                    await self._drv(r["core"]).delete(r["id"])
                    if await self.sys.cas(r["id"], "stopped", status="deleted", host_state={}): events.append({"id": r["id"], "event": "auto_delete"})
            except Exception as e: events.append({"id": r["id"], "event": "delete_failed", "error": str(e)[:200]})  # noqa: BLE001
        return events

    async def reconcile(self, now=None):
        """On API start: compare every 'running' / 'provisioning' row with `docker inspect`. A box that is gone or unreachable becomes `failed` + last_error and is NEVER
        replaced silently (the owner sees 'Tạo máy mới'); a container that exited becomes `stopped`; a 'provisioning' row older than 2 minutes was cut off by a restart."""
        now, events = now or self.clock(), []
        for r in await self.sys.list_active():
            if r["status"] == "provisioning":
                if now - _ts(r["last_active"]) > 120 and await self.sys.cas(r["id"], "provisioning", status="failed", last_error="creation was interrupted by a server restart"):
                    events.append({"id": r["id"], "event": "failed", "why": "interrupted"})
                continue
            try: state = (await self._drv(r["core"]).inspect(r["id"])).get("state")
            except NotFound: state = "missing"
            except Exception as e: state = "unreachable: " + str(e)[:120]  # noqa: BLE001
            if state == "running": continue
            if state == "exited":
                hs = {**(r.get("host_state") or {}), "stopped_at": now}; hs.pop("idle_stop", None)
                if await self.sys.cas(r["id"], "running", status="stopped", last_error="container stopped", host_state=hs): events.append({"id": r["id"], "event": "stopped"})
            elif await self.sys.cas(r["id"], "running", status="failed", last_error=("the box is gone from the server" if state == "missing" else "the box cannot be reached (" + state + ")") + ": create a new computer"):
                events.append({"id": r["id"], "event": "failed", "why": state})
        return events

    async def sample_tick(self, now=None):
        """Every 60 s: `docker stats` of each running box -> sandbox_usage_hourly (cpu seconds, memory byte-seconds, running seconds of the current hour)."""
        now, out, live = now or self.clock(), [], set()
        hour = int(now // 3600 * 3600)
        for r in await self.sys.list_running():
            live.add(r["id"])
            try: st = await self._drv(r["core"]).stats(r["id"])
            except Exception: continue  # noqa: BLE001
            if not st: continue
            last = self._sample_t.get(r["id"]); self._sample_t[r["id"]] = now
            dt = min(max(now - last, 0.0), 120.0) if last else SAMPLE_S
            cpu, mem = parse_stats(st)
            await self.sys.add_usage(hour, int(r["workspace_id"]), r["id"], cpu / 100.0 * dt, mem * dt, dt, 0); out.append(r["id"])
        for k in [k for k in self._sample_t if k not in live]: self._sample_t.pop(k, None)
        return out

    # ---- purge (part A 11.34 hard delete of an agent, privacy erasure of a user) -----------------------------------------------
    async def _purge_rows(self, rows):
        done, errors = 0, []
        for r in rows:
            try:
                if (r.get("kind") or "computer") == "computer":
                    async with self._lock(r["id"]): await self._drv(r["core"]).delete(r["id"])      # container + both volumes
                elif re.fullmatch(r"[0-9A-Za-z_-]{1,40}", r["id"]):
                    if self.sbx_root is None:
                        import admin; self.sbx_root = admin.SBX_ROOT
                    shutil.rmtree(os.path.join(str(self.sbx_root), r["id"]), ignore_errors=True)    # old one-shot folder sandbox
            except Exception as e: errors.append({"id": r["id"], "error": str(e)[:200]}); continue  # noqa: BLE001  (row stays: the purge can be repeated)
            await self.sys.purge_row(r["id"]); self._busy.pop(r["id"], None); self._sample_t.pop(r["id"], None); done += 1
        return {"boxes": done, "errors": errors}

    async def purge_for_agent(self, agent_id, *, workspace_id=None, identifier=None):
        """Hook for part A 11.34: every box of the agent (both kinds: container + volumes, host folder, commands by cascade), then the agent's own secrets
        (ws_secrets named `agent:<identifier>/…`), then the extension hooks (11.47a adds snapshots / setup images). -> {"boxes", "errors", "secrets"}.
        Boxes that could not be removed keep their row and are listed in `errors` (run it again)."""
        aid = int(agent_id)
        res = await self._purge_rows(await self.sys.purge_rows(agent_id=aid, workspace_id=workspace_id))
        identifier = identifier if identifier is not None else await self.sys.agent_identifier(aid)
        prefixes = [f"agent:{aid}/"] + ([f"agent:{identifier}/"] if identifier else [])
        res["secrets"] = await self.sys.purge_secrets(workspace_id, prefixes)
        for h in PURGE_HOOKS:
            try: res.setdefault("extra", {})[getattr(h, "__name__", "hook")] = await h(aid, workspace_id)
            except Exception as e: res["errors"].append({"hook": getattr(h, "__name__", "hook"), "error": str(e)[:200]})  # noqa: BLE001
        return res

    async def purge_for_user(self, user_id, workspace_id=None):
        """Privacy erasure of a person: the boxes they own (or that belong to them as user) in the workspace (None = all workspaces)."""
        return await self._purge_rows(await self.sys.purge_rows(owner_id=int(user_id), workspace_id=workspace_id))

async def purge_for_agent(agent_id, *, workspace_id=None, identifier=None):
    """`sbx_runtime.purge_for_agent(agent_id)` - the entry point 11.34 calls."""
    return await Computers().purge_for_agent(agent_id, workspace_id=workspace_id, identifier=identifier)

async def purge_for_user(user_id, workspace_id=None): return await Computers().purge_for_user(user_id, workspace_id)

async def disk_tick_global():
    """obs worker hook. Nothing to do (and no docker call) unless a computer is running; skipped where gVisor is not available (fake / runc dev machines)."""
    try: select_core("computer")
    except SbxError: return []
    return await Computers().disk_tick()

_API: dict = {}
def shared():
    """The one Computers instance of the API process (locks and busy counters live on it); the reaper and the 11.43 routes use it."""
    return _API.setdefault("c", Computers())

async def reaper_loop():
    """API process (started by sandbox2.init, not in the worker): reconcile once, then every 30 s the idle / delete-after-stop reaper and every 60 s the usage sampler.
    Without gVisor there are no computers: the loop only waits."""
    c = None
    while True:
        try:
            select_core("computer")
            if c is None: c = shared(); await c.reconcile()
            await c.reap_tick()
            if time.monotonic() - _API.get("sampled", 0) >= SAMPLE_S: _API["sampled"] = time.monotonic(); await c.sample_tick()
        except SbxError: c = None
        except Exception as e: print("[sbx] reaper", str(e)[:200], flush=True)  # noqa: BLE001
        await asyncio.sleep(30)
