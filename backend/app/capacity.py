"""GĐ11 11.N3: capacity helper shared by the health check and the virtual-computer reaper (11.42)."""
import os

def mem_available_mb() -> int:
    for l in open("/proc/meminfo"):
        if l.startswith("MemAvailable"): return int(l.split()[1]) // 1024
    return 0

def headroom_ok(need_mb: float) -> bool:
    """True when MemAvailable minus the reserve (AOS_RESERVE_MB, default 512) still covers need_mb."""
    return mem_available_mb() - int(os.environ.get("AOS_RESERVE_MB") or 512) >= need_mb
