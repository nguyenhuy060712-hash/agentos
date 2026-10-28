"""Ops status for Settings: Postgres backups (nightly pg_dump, 7 daily / 4 weekly, restore drill) and run-queue workers. Admin only (/api/admin/*)."""
import asyncio, json, os
from fastapi import APIRouter, HTTPException
import deepagent_api as A

router = APIRouter(tags=["ops"])
ROOT = "/opt/agentos_backups/pg"; OPS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ops")
LOCK = asyncio.Lock()

def _status():
    try: return json.load(open(f"{ROOT}/status.json"))
    except Exception: return {}  # noqa: BLE001

@router.get("/api/admin/ops")
async def ops_status():
    import admin; await admin.require_platform_owner()   # GĐ12.100d: machine-wide config / numbers -> platform owner only
    w = await A.q("SELECT name, pid, host, started, at, running, claimed_total, max_conc, extract(epoch from now()-at)::int age_s FROM worker_heartbeats ORDER BY name")
    for x in w: x["healthy"] = x["age_s"] is not None and x["age_s"] < 90
    q = await A.q("SELECT count(*) FILTER (WHERE status='pending') pending, count(*) FILTER (WHERE status='running') running FROM lg_runs", one=True)
    st = _status(); lb = st.get("last_backup") or {}
    import time
    stale = not lb.get("at") or time.time() - lb["at"] > 26 * 3600
    return {"backups": {**st, "stale": stale, "healthy": lb.get("status") == "ok" and not stale and (st.get("last_restore_test") or {}).get("status") == "ok"},
            "workers": w, "queue": q}

async def _sh(*cmd, timeout=300):
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try: out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError: p.kill(); raise HTTPException(504, "timed out")  # noqa: B904
    return p.returncode, out.decode(errors="replace")[-1500:]

@router.post("/api/admin/backups/run")
async def backup_now():
    if LOCK.locked(): raise HTTPException(409, "A backup job is already running")
    async with LOCK:
        rc, out = await _sh(f"{OPS}/pg_backup.sh")
        rc2, out2 = await _sh(f"{OPS}/pg_restore.sh", "--test") if rc == 0 else (None, "")
    return {"ok": rc == 0 and rc2 == 0, "backup": out.strip().splitlines()[-1:] , "restore_test": out2.strip().splitlines()[-3:], "status": _status()}
