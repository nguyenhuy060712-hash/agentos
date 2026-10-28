"""GĐ11 11.N3: server health check every 60 s (disk / RAM / Postgres size). GĐ12.10: events system.health.warn / .critical through destinations.emit (workspace 1), 6 h dedupe per kind."""
import logging, os, shutil, time
from fastapi import APIRouter

router = APIRouter()
log = logging.getLogger("health_watch")
LAST: dict = {}
SENT: dict = {}   # alert kind -> time.time() of last delivery
DEDUPE_S = 6 * 3600
DISK_MAX, MEM_MIN = 0.80, 0.15   # warn above / below these (GĐ12.10)
DISK_CRIT, MEM_CRIT = 0.90, 0.05   # beyond these the event is system.health.critical, else .warn

def _meminfo() -> dict:
    return {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo")}

def _disk():
    return shutil.disk_usage("/")

async def _pg_bytes() -> int:
    import obs
    r = await obs.A.q("SELECT pg_database_size(current_database()) b", one=True)
    return int(r["b"])

async def sample() -> dict:
    mi, du = _meminfo(), _disk()
    try: pg = await _pg_bytes()
    except Exception: pg = None  # noqa: BLE001
    return {"disk_used_ratio": du.used / du.total, "mem_avail_ratio": mi["MemAvailable"] / mi["MemTotal"],
            "load1": os.getloadavg()[0] if hasattr(os, "getloadavg") else None, "pg_db_bytes": pg, "at": time.time()}

def evaluate(s: dict) -> list[tuple[str, str]]:
    out = []
    if s["disk_used_ratio"] > DISK_MAX: out.append(("disk", f"Ổ đĩa đã dùng {s['disk_used_ratio'] * 100:.0f}% (ngưỡng {DISK_MAX * 100:.0f}%)"))
    if s["mem_avail_ratio"] < MEM_MIN: out.append(("ram", f"RAM còn trống {s['mem_avail_ratio'] * 100:.0f}% (ngưỡng {MEM_MIN * 100:.0f}%)"))
    lim = float(os.environ.get("AOS_PG_ALERT_GB") or 20) * 2**30
    if s.get("pg_db_bytes") and s["pg_db_bytes"] > lim: out.append(("postgres", f"Postgres {s['pg_db_bytes'] / 2**30:.1f} GB (ngưỡng {lim / 2**30:.0f} GB)"))
    return out

async def run_check():
    try:
        s = await sample(); LAST.clear(); LAST.update(s)
        alerts = evaluate(s)
        if not alerts: return
        import destinations
        for kind, text in alerts:
            log.warning("health alert %s: %s", kind, text)
            if time.time() - SENT.get(kind, 0) < DEDUPE_S: continue
            SENT[kind] = time.time()
            sev = "critical" if s["disk_used_ratio"] > DISK_CRIT or s["mem_avail_ratio"] < MEM_CRIT else "warn"
            try: await destinations.emit("system.health." + sev, {"kind": kind, "text": text, "severity": sev, "title": "[Moza Agent OS] Cảnh báo máy chủ"}, 1)
            except Exception: log.exception("health alert via destinations failed")  # noqa: BLE001
    except Exception: log.exception("health check failed")  # noqa: BLE001

@router.get("/api/admin/health")
async def health():
    import admin; await admin.require_platform_owner()   # GĐ12.100d: machine-wide config / numbers -> platform owner only
    return LAST
