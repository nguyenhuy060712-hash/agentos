"""Sandboxes v2: gVisor isolation (runsc, Apache-2.0) when available, long-running sandbox services exposed only through an
authenticated, HTTPS-only reverse proxy (/api/sandboxes/{sid}/svc/{svc}/...), with TTL auto-stop and restart reconciliation.
Services run on an internal Docker bridge (no egress, inter-container traffic dropped); only the host can reach them."""
import asyncio, json, os, re, shlex, time, uuid
import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
import deepagent_api as A

router = APIRouter(tags=["sandboxes"])
FAKE_RUNTIME = "fake (local test runner, no Docker)"
NET = "aos-sbx-svc"; SUBNET = "172.31.250.0/24"
MAX_SERVICES = int(os.environ.get("AOS_SBX_MAX_SERVICES", "3"))
DDL = """CREATE TABLE IF NOT EXISTS sandbox_services(id TEXT PRIMARY KEY, sandbox_id TEXT NOT NULL, name TEXT, port INT NOT NULL, command JSONB NOT NULL,
  container TEXT NOT NULL, ip TEXT, status TEXT NOT NULL DEFAULT 'starting', runtime TEXT, owner_id INT, workspace_id INT,
  created TIMESTAMPTZ NOT NULL DEFAULT now(), expires TIMESTAMPTZ NOT NULL, stopped TIMESTAMPTZ, last_error TEXT);"""
HOP = {"x-aos-sandbox-service-token", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade", "host",
       "content-length", "cookie", "authorization", "x-api-key", "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "cf-connecting-ip",
       "cf-visitor", "cf-ray", "cf-ipcountry"}

async def _sh(*cmd, timeout=60):
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try: out, err = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError: p.kill(); return 124, "", "timeout"
    return p.returncode, out.decode(errors="replace").strip(), err.decode(errors="replace").strip()

async def detect_runtime():
    """Prefer gVisor (runsc) if Docker has it registered and it actually starts a container; else runc (docker default).
    GĐ9.P1: when the chat code runner is a fake (local dev box, no Docker) the label says so (it used to claim "runc (docker default)")."""
    import chat_gd5b
    if chat_gd5b.RUNNER["fn"] is not chat_gd5b._docker:
        A.SBX_RT[:] = []; A.SBX_INFO.update(runtime=FAKE_RUNTIME, kernel="none"); return
    want = os.environ.get("AOS_SBX_RUNTIME", "runsc")
    if want == "runsc":
        rc, out, _ = await _sh("docker", "info", "--format", "{{json .Runtimes}}")
        if rc == 0 and '"runsc"' in out:
            rc, out, _ = await _sh("docker", "run", "--rm", "--runtime", "runsc", "--network", "none", A.SANDBOX_IMAGE, "python", "-c", "import platform;print(platform.release())", timeout=60)
            if rc == 0 and "gvisor" in out:
                A.SBX_RT[:] = ["--runtime", "runsc"]; A.SBX_INFO.update(runtime="gvisor (runsc)", kernel=out); return
    A.SBX_RT[:] = []; A.SBX_INFO.update(runtime="docker (runc)", kernel="host")

async def init():
    await A.q(DDL)
    await detect_runtime()
    rc, _, _ = await _sh("docker", "network", "inspect", NET)
    if rc != 0:
        await _sh("docker", "network", "create", "--internal", "-o", "com.docker.network.bridge.enable_icc=false",
                  "-o", "com.docker.network.bridge.name=aos-sbx0", "--subnet", SUBNET, NET)
    if os.environ.get("AOS_ROLE") != "worker":
        await reconcile(); asyncio.create_task(_reaper())
        if str(A.SBX_INFO.get("runtime", "")).startswith("gvisor"): import egress_ctl; asyncio.create_task(egress_ctl.start_default())   # GĐ11.45: decision socket of the egress proxy (the proxy container itself starts with the first box that has Internet)
        import sbx_runtime; asyncio.create_task(sbx_runtime.reaper_loop())   # GĐ11.42: idle stop / delete-after-stop / reconcile / usage sampler of the virtual computers

async def reconcile():
    for s in await A.q("SELECT * FROM sandbox_services WHERE status IN ('starting','running')"):
        rc, out, _ = await _sh("docker", "inspect", "-f", "{{.State.Running}}", s["container"])
        if rc != 0 or out != "true":
            await A.q("UPDATE sandbox_services SET status='stopped', stopped=now(), last_error=coalesce(last_error,'container gone (restart)') WHERE id=%s", (s["id"],))

async def _stop(s, why="stopped"):
    await _sh("docker", "rm", "-f", s["container"], timeout=30)
    await A.q("UPDATE sandbox_services SET status=%s, stopped=now() WHERE id=%s", (why, s["id"]))

async def _reaper():
    while True:
        try:
            for s in await A.q("SELECT * FROM sandbox_services WHERE status IN ('starting','running') AND expires < now()"): await _stop(s, "expired")
            for s in await A.q("SELECT s.* FROM sandbox_services s LEFT JOIN sandboxes b ON b.id=s.sandbox_id WHERE s.status IN ('starting','running') AND b.id IS NULL"): await _stop(s)
        except Exception as e: print("sbx reaper", e)  # noqa: BLE001
        await asyncio.sleep(30)

def _require_tls(request: Request):
    """Service traffic must arrive over HTTPS (Cloudflare tunnel / TLS proxy); plain HTTP from outside is refused."""
    xf = request.headers.get("x-forwarded-proto", ""); cv = request.headers.get("cf-visitor", "")
    if "https" in xf or '"https"' in cv or request.url.scheme == "https": return
    raise HTTPException(403, "Sandbox services are only served over HTTPS")

class SvcIn(BaseModel):
    command: list[str] | str = Field(description="e.g. ['python','-m','http.server','8000']")
    port: int = Field(default=8000, ge=1024, le=65535)
    name: str = Field(default="service", max_length=60)
    ttl_minutes: int = Field(default=60, ge=5, le=480)
    files_readonly: bool = False

@router.post("/api/sandboxes/{sid}/services")
async def svc_start(sid: str, b: SvcIn, request: Request):
    import admin
    await admin._own_sbx(sid); d = admin._sbx_dir(sid)
    n = (await A.q("SELECT count(*) n FROM sandbox_services WHERE status IN ('starting','running')", one=True))["n"]
    if n >= MAX_SERVICES: raise HTTPException(429, f"At most {MAX_SERVICES} sandbox services can run at once (RAM limit); stop one first")
    cmd = shlex.split(b.command) if isinstance(b.command, str) else list(b.command)
    if not cmd or len(cmd) > 40 or any(len(x) > 500 for x in cmd): raise HTTPException(400, "Invalid command")
    svc = uuid.uuid4().hex[:10]; name = f"aos-svc-{sid}-{svc}"
    args = ["docker", "run", "-d", "--name", name, *A.SBX_RT, "--network", NET, "--memory", "256m", "--memory-swap", "256m", "--cpus", "0.5",
            "--pids-limit", "64", "--read-only", "--tmpfs", "/tmp:rw,size=16m,noexec", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", "65534:65534", "-v", f"{d}:/work:{'ro' if b.files_readonly else 'rw'}", "--workdir", "/work", "-e", "HOME=/work",
            "-e", f"PORT={b.port}", "--label", f"aos.sandbox={sid}", A.SANDBOX_IMAGE, *cmd]
    rc, out, err = await _sh(*args)
    if rc != 0: raise HTTPException(400, "Could not start service: " + err[-300:])
    _, ip, _ = await _sh("docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name)
    u = admin.cur() or {}
    await A.q("INSERT INTO sandbox_services(id,sandbox_id,name,port,command,container,ip,status,runtime,owner_id,workspace_id,expires) VALUES(%s,%s,%s,%s,%s,%s,%s,'starting',%s,%s,%s,now()+make_interval(mins=>%s))",
              (svc, sid, b.name, b.port, json.dumps(cmd), name, ip, A.SBX_INFO.get("runtime"), u.get("id"), admin.cur_ws(), b.ttl_minutes))
    ok = False
    for _ in range(30):   # wait for the port (max ~15 s)
        try:
            async with httpx.AsyncClient(timeout=1) as h: await h.get(f"http://{ip}:{b.port}/"); ok = True; break
        except Exception: await asyncio.sleep(0.5)  # noqa: BLE001
    _, logs, lerr = await _sh("docker", "logs", "--tail", "20", name)
    await A.q("UPDATE sandbox_services SET status=%s, last_error=%s WHERE id=%s", ("running" if ok else "starting", None if ok else (logs + lerr)[-400:], svc))
    base = str(request.base_url).rstrip("/")
    if request.headers.get("x-forwarded-host"): base = "https://" + request.headers["x-forwarded-host"]
    elif "https" in request.headers.get("x-forwarded-proto", "") or '"https"' in request.headers.get("cf-visitor", ""): base = base.replace("http://", "https://")
    return {"id": svc, "status": "running" if ok else "starting", "runtime": A.SBX_INFO.get("runtime"),
            "url": f"{base}/api/sandboxes/{sid}/svc/{svc}/", "expires_in_minutes": b.ttl_minutes}

@router.get("/api/sandboxes/{sid}/services")
async def svc_list(sid: str):
    import admin
    await admin._own_sbx(sid)
    return await A.q("SELECT id, name, port, command, status, runtime, created, expires, stopped, last_error FROM sandbox_services WHERE sandbox_id=%s ORDER BY created DESC LIMIT 50", (sid,))

@router.delete("/api/sandboxes/{sid}/services/{svc}")
async def svc_stop(sid: str, svc: str):
    import admin
    await admin._own_sbx(sid)
    s = await A.q("SELECT * FROM sandbox_services WHERE id=%s AND sandbox_id=%s", (svc, sid), one=True)
    if not s: raise HTTPException(404, "Service not found")
    await _stop(s); return {"ok": True}

@router.get("/api/sandboxes/{sid}/services/{svc}/logs")
async def svc_logs(sid: str, svc: str, tail: int = 200):
    import admin
    await admin._own_sbx(sid)
    s = await A.q("SELECT * FROM sandbox_services WHERE id=%s AND sandbox_id=%s", (svc, sid), one=True)
    if not s: raise HTTPException(404, "Service not found")
    rc, out, err = await _sh("docker", "logs", "--tail", str(min(max(tail, 1), 2000)), s["container"])
    return {"logs": (out + ("\n" + err if err else ""))[-20000:], "running": rc == 0}

SVC_IP_RE = re.compile(r"172\.31\.250\.\d{1,3}")   # only the internal service network is ever a proxy target (local test harness widens it to loopback)

async def forward(s, request: Request, path: str):
    """Reverse-proxy one request to the sandbox service row `s` (shared by the signed-in proxy below and the token service URLs in sbx_parity)."""
    if not SVC_IP_RE.fullmatch(s["ip"] or ""): raise HTTPException(502, "Service has no internal address")
    url = f"http://{s['ip']}:{s['port']}/{path}" + (f"?{request.url.query}" if request.url.query else "")
    hdr = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as h:
            r = await h.request(request.method, url, headers=hdr, content=body)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Service unreachable: {type(e).__name__}")  # noqa: B904
    out = {k: v for k, v in r.headers.items() if k.lower() not in HOP | {"content-encoding", "set-cookie"}}
    out["X-Sandbox-Runtime"] = str(A.SBX_INFO.get("runtime", "")).encode("ascii", "ignore").decode().strip()[:80]   # 9.Z: HTTP headers are latin-1 only
    out["Content-Security-Policy"] = "sandbox allow-scripts allow-forms"
    if s["status"] == "starting": await A.q("UPDATE sandbox_services SET status='running', last_error=NULL WHERE id=%s", (s["id"],))
    return Response(r.content, status_code=r.status_code, headers=out)

@router.api_route("/api/sandboxes/{sid}/svc/{svc}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def svc_proxy(sid: str, svc: str, path: str, request: Request):
    """Authenticated (session / API key / basic, via the global middleware) + HTTPS-only reverse proxy to a sandbox service."""
    import admin
    _require_tls(request); await admin._own_sbx(sid)
    s = await A.q("SELECT * FROM sandbox_services WHERE id=%s AND sandbox_id=%s AND status IN ('starting','running')", (svc, sid), one=True)
    if not s: raise HTTPException(404, "Service not running")
    return await forward(s, request, path)

@router.get("/api/sandbox_runtime")
async def sbx_runtime():
    import sbx_runtime as R   # GĐ11.42: computers of the caller's workspace by status (RLS) + the host-wide running limit
    comp = {r["status"]: r["n"] for r in await A.q("SELECT status, count(*) n FROM sandboxes WHERE kind='computer' AND status<>'deleted' GROUP BY status")}
    return {**A.SBX_INFO, "services_running": (await A.q("SELECT count(*) n FROM sandbox_services WHERE status IN ('starting','running')", one=True))["n"], "max_services": MAX_SERVICES,
            "computers": comp, "max_running_computers": R.max_running_default()}
