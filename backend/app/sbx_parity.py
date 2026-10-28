"""GĐ9.Q P7 sandbox parity on one VPS with gVisor (LangSmith pages: service URLs, mounts, snapshots; CLI is app/sdk/aos_cli.py):
 * service URLs  : `POST /api/sandboxes/{sid}/service-url` -> a short-lived (<= 24 h) HMAC-signed URL for ONE service of ONE sandbox, usable without a login
                   (`X-Aos-Sandbox-Service-Token` header, or `browser_url` which sets a path-scoped cookie). Revocable per service (`revoke-links`), HTTPS only,
                   the token never reaches the service (stripped), and the service itself stays on the internal no-egress network.
 * mounts       : persistent volumes (ext.py: rw/ro at /mnt/<name>) + `POST /api/sandbox_volumes/import` = a public git repository (https, shallow, no hooks) or a
                   Context Hub pack copied into a LOCKED volume that can only be mounted read-only. S3/GCS bucket mounts are not offered (sandboxes have no network).
 * snapshots    : create / list (per sandbox too) / restore / clone exist in ext.py; added here: boot a new sandbox by snapshot NAME or id, and a snapshot that a
                   sandbox was created/restored from cannot be deleted (LangSmith: "fails if any sandbox still references it") — enforced in ext.snap_del.
All routes are workspace-scoped; negative cases (bad/expired/tampered token, other sandbox, stopped service) are tested."""
import asyncio, hmac, os, re, secrets, shutil, time, urllib.parse
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb

router = APIRouter(tags=["sandboxes"])
DDL = """
ALTER TABLE sandbox_services ADD COLUMN IF NOT EXISTS link_epoch INT NOT NULL DEFAULT 0;
ALTER TABLE sandbox_volumes ADD COLUMN IF NOT EXISTS source JSONB;
ALTER TABLE sandbox_volumes ADD COLUMN IF NOT EXISTS locked BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE sandboxes ADD COLUMN IF NOT EXISTS snapshot_id TEXT;
"""
TEST_ALLOW_FILE_GIT = False       # set by the local test launcher only (overlay/run_local.py); never in production
MAX_SNAPSHOTS = int(os.environ.get("AOS_MAX_SNAPSHOTS", "20"))
MAX_LINK_S = 86400
async def _q(*a, **k):
    import deepagent_api as A
    return await A.q(*a, **k)

async def init_late():
    await _q(DDL)
    import admin
    admin.PUBLIC_PREFIX = tuple(dict.fromkeys(admin.PUBLIC_PREFIX + ("/sbx/",)))   # token URLs authenticate themselves (below)

def mount_mode(vol: dict, readonly: bool) -> str:
    """'ro' | 'rw' for a volume mount: imported (locked) volumes are read-only whatever the caller asks."""
    return "ro" if (readonly or vol.get("locked")) else "rw"

# ---------------------------------------------------------------- service URLs
def _sign(sid, svc, exp, epoch):
    import ext
    return ext._sign(f"svc|{sid}|{svc}|{exp}|{epoch}")

def make_token(sid, svc, epoch, ttl, now=None):
    exp = int((now or time.time()) + ttl); return f"{exp}.{epoch}.{_sign(sid, svc, exp, epoch)}", exp

def check_token(tok, sid, svc, epoch, now=None) -> bool:
    try: exp_s, ep_s, sig = (tok or "").split(".", 2); exp, ep = int(exp_s), int(ep_s)
    except ValueError: return False
    return exp >= (now or time.time()) and ep == epoch and hmac.compare_digest(sig, _sign(sid, svc, exp, ep))

class UrlIn(BaseModel):
    service: str | None = Field(default=None, max_length=40, description="service id (from /services)")
    port: int | None = Field(default=None, ge=1024, le=65535)
    expires_in_seconds: int = Field(default=600, ge=60, le=MAX_LINK_S)

@router.post("/api/sandboxes/{sid}/service-url")
async def service_url(sid: str, b: UrlIn, request: Request):
    import admin, ext
    await admin._own_sbx(sid)
    if not (b.service or b.port): raise HTTPException(422, "give a service id or a port")
    s = await _q("SELECT * FROM sandbox_services WHERE sandbox_id=%s AND status IN ('starting','running') AND (id=%s OR port=%s) ORDER BY created DESC LIMIT 1", (sid, b.service, b.port), one=True)
    if not s: raise HTTPException(404, "No running service on that id/port in this sandbox (start it first)")
    tok, exp = make_token(sid, s["id"], s["link_epoch"], b.expires_in_seconds)
    base = f"{ext._base(request)}/sbx/{sid}/{s['id']}"
    return {"service_id": s["id"], "service_url": base + "/", "browser_url": f"{base}/_svc/auth?token={urllib.parse.quote(tok)}", "token": tok,
            "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(exp)), "header": "X-Aos-Sandbox-Service-Token"}

@router.post("/api/sandboxes/{sid}/services/{svc}/revoke-links")
async def revoke_links(sid: str, svc: str):
    import admin
    await admin._own_sbx(sid)
    r = await _q("UPDATE sandbox_services SET link_epoch=link_epoch+1 WHERE id=%s AND sandbox_id=%s RETURNING link_epoch", (svc, sid), one=True)
    if not r: raise HTTPException(404, "Service not found")
    return {"ok": True, "epoch": r["link_epoch"]}

_FAILS: dict = {}
def _throttle(ip):
    now = time.time(); w = [t for t in _FAILS.get(ip, []) if now - t < 60]; _FAILS[ip] = w
    if len(w) >= 30: raise HTTPException(429, "Too many invalid links, wait a minute")
def _failed(ip): _FAILS.setdefault(ip, []).append(time.time())

@router.api_route("/sbx/{sid}/{svc}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"], include_in_schema=False)
async def token_proxy(sid: str, svc: str, path: str, request: Request):
    import sandbox2, admin
    sandbox2._require_tls(request); ip = admin._ip(request); _throttle(ip)
    s = await _q("SELECT * FROM sandbox_services WHERE id=%s AND sandbox_id=%s AND status IN ('starting','running')", (svc, sid), one=True) if re.fullmatch(r"[0-9a-f]{12}", sid) and re.fullmatch(r"[0-9a-f]{10}", svc) else None
    cookie = f"aos_svc_{svc}"
    tok = request.headers.get("x-aos-sandbox-service-token") or request.cookies.get(cookie) or (request.query_params.get("token") if path == "_svc/auth" else None)
    if not s or not check_token(tok, sid, svc, s["link_epoch"]):
        _failed(ip); raise HTTPException(403, "This service link is invalid, expired or revoked")      # same answer for every failure: nothing to probe
    if path == "_svc/auth":      # browser entry: trade the token for a cookie that is scoped to this one service
        r = RedirectResponse(f"/sbx/{sid}/{svc}/", 303)
        exp = int(tok.split(".")[0]); r.set_cookie(cookie, tok, max_age=max(1, exp - int(time.time())), httponly=True, secure=True, samesite="strict", path=f"/sbx/{sid}/{svc}/")
        return r
    return await sandbox2.forward(s, request, path)

# ---------------------------------------------------------------- snapshots by name
class FromSnap(BaseModel):
    snapshot: str = Field(min_length=1, max_length=100, description="snapshot id or name")
    name: str = Field(default="sandbox", max_length=80)

async def resolve_snapshot(ref: str) -> dict:
    rows = await _q("SELECT * FROM sandbox_snapshots WHERE kind='files' AND (id=%s OR name=%s) ORDER BY created DESC", (ref, ref))
    if not rows: raise HTTPException(404, "Snapshot not found")
    byid = [r for r in rows if r["id"] == ref]
    if byid: return byid[0]
    if len(rows) > 1: raise HTTPException(409, f"{len(rows)} snapshots are named '{ref}': use the id")
    return rows[0]

@router.post("/api/sandboxes/from-snapshot")
async def from_snapshot(b: FromSnap):
    import ext
    s = await resolve_snapshot(b.snapshot)
    r = await ext.snap_clone(s["id"], ext.SnapIn(name=b.name))
    return {**r, "snapshot_id": s["id"], "snapshot_name": s["name"]}

@router.get("/api/sandboxes/{sid}/snapshots")
async def snapshots_of(sid: str):
    import admin
    await admin._own_sbx(sid)
    return await _q("SELECT id, name, bytes, created FROM sandbox_snapshots WHERE kind='files' AND sandbox_id=%s ORDER BY created DESC", (sid,))

# ---------------------------------------------------------------- volume import (git / context pack) -> locked, read-only mount
def _public_host(url: str):
    """netguard purpose 'public' (GĐ10.2): https + public addresses only, AOS_EGRESS_ALLOW ignored. -> (ip, host) so the clone can be pinned to the checked IP."""
    import netguard
    try: return netguard.check(url, "public")
    except netguard.NetBlocked as e: raise HTTPException(422, "The git host is not allowed: " + e.reason) from None

def validate_git_url(url: str) -> str:
    if len(url) > 300 or re.search(r"[\s\x00-\x1f\\]", url): raise HTTPException(422, "Invalid repository URL")
    u = urllib.parse.urlsplit(url)
    if TEST_ALLOW_FILE_GIT and u.scheme == "file": return url
    if u.scheme != "https": raise HTTPException(422, "Only https:// repositories can be mounted")
    if u.username or u.password or "@" in u.netloc: raise HTTPException(422, "Credentials in the URL are not accepted: mounts are for public repositories")
    if not u.hostname or u.port not in (None, 443): raise HTTPException(422, "Invalid repository host/port")
    if not re.fullmatch(r"[\w.~%/\-]+", u.path or "") or u.query or u.fragment: raise HTTPException(422, "Invalid repository path")
    _public_host(url)
    return url

class ImportIn(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]{0,39}$")
    git: dict | None = Field(default=None, description="{url, ref?}")
    context_pack: str | None = Field(default=None, max_length=140, description="'name@tag'")

def _pin(url: str) -> str:
    ip, host = _public_host(url)
    return f"{host}:443:[{ip}]" if ":" in ip else f"{host}:443:{ip}"

async def _run(*cmd, env=None, cwd=None, timeout=90):
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env, cwd=cwd)
    try: out, err = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError: p.kill(); return 124, "", "timeout"
    return p.returncode, out.decode(errors="replace"), err.decode(errors="replace")

def _tidy(d: Path, cap: int):
    """Delete .git and every symlink; fail when the tree is bigger than the quota / has too many files."""
    shutil.rmtree(d / ".git", ignore_errors=True)
    n = tot = 0
    for p in list(d.rglob("*")):
        if p.is_symlink(): p.unlink(); continue
        if p.is_file(): n += 1; tot += p.stat().st_size
    if n > 5000 or tot > cap: raise HTTPException(413, f"Repository too large for a volume ({n} files, {tot >> 20} MB; limit 5000 files / {cap >> 20} MB)")

def _own(d: Path):
    for p in [d, *d.rglob("*")]:
        try: os.chown(p, 65534, 65534); os.chmod(p, 0o750 if p.is_dir() else 0o640)
        except OSError: pass

@router.post("/api/sandbox_volumes/import")
async def volume_import(b: ImportIn):
    import admin, ext
    if bool(b.git) == bool(b.context_pack): raise HTTPException(422, "give exactly one of git / context_pack")
    if await _q("SELECT 1 FROM sandbox_volumes WHERE name=%s", (b.name,), one=True): raise HTTPException(409, "Volume name already exists")
    vid = secrets.token_hex(6); d = ext.VOL_ROOT / vid; d.mkdir(mode=0o770, parents=True)
    try:
        if b.git:
            url = validate_git_url(str(b.git.get("url") or "")); ref = str(b.git.get("ref") or "")
            if ref and not re.fullmatch(r"[A-Za-z0-9][\w./\-]{0,99}", ref): raise HTTPException(422, "Invalid ref")
            env = {"PATH": os.environ.get("PATH", "/usr/bin"), "HOME": "/nonexistent", "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "file" if TEST_ALLOW_FILE_GIT and url.startswith("file:") else "https",
                   "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
            pin = [] if url.startswith("file:") else ["-c", "http.curloptResolve=" + await asyncio.to_thread(_pin, url)]   # git resolves DNS itself: pin it to the IP netguard checked (no rebinding)
            rc, _, err = await _run("git", "-c", "core.hooksPath=/dev/null", "-c", "http.followRedirects=false", *pin, "clone", "--depth", "1", "--single-branch", "--no-tags",
                                    *(["--branch", ref] if ref else []), "--", url, str(d / "repo"), env=env)
            if rc != 0: raise HTTPException(422, "git clone failed: " + err.strip()[-200:])
            src = {"kind": "git", "url": url, "ref": ref or "default"}
            for p in (d / "repo").iterdir(): shutil.move(str(p), str(d / p.name)) if p.name != ".git" else None
            shutil.rmtree(d / "repo", ignore_errors=True)
        else:
            import context_hub
            ws = admin.cur_ws(); pack, c = await context_hub._resolve_ws(ws, b.context_pack)
            if not pack or not c: raise HTTPException(404, "Context pack or tag not found")
            files = await context_hub.files_of(pack["id"], c)
            for rel, txt in files.items():
                p = (d / rel).resolve()
                if d.resolve() not in p.parents: continue
                p.parent.mkdir(parents=True, exist_ok=True); p.write_text(txt, encoding="utf-8")
            src = {"kind": "context_pack", "ref": b.context_pack, "commit": c["hash"][:12]}
        await asyncio.to_thread(_tidy, d, admin.SBX_QUOTA); await asyncio.to_thread(_own, d)
    except BaseException:
        shutil.rmtree(d, ignore_errors=True); raise
    await _q("INSERT INTO sandbox_volumes(id,name,owner_id,source,locked) VALUES(%s,%s,%s,%s,true)", (vid, b.name, (admin.cur() or {}).get("id"), Jsonb(src)))
    return {"id": vid, "name": b.name, "source": src, "locked": True, "files": len([p for p in d.rglob("*") if p.is_file()])}
