"""GĐ11 11.N3: server health check (fake readers, fake notify.send)."""
import asyncio, collections, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")
import pytest
run = asyncio.run
DU = collections.namedtuple("DU", "total used free")

@pytest.fixture
def hw(monkeypatch):
    import health_watch
    health_watch.SENT.clear(); health_watch.LAST.clear(); sent = []
    st = {"disk": 0.5, "avail": 50}
    monkeypatch.setattr(health_watch, "_disk", lambda: DU(100, int(st["disk"] * 100), 0))
    monkeypatch.setattr(health_watch, "_meminfo", lambda: {"MemTotal": 100, "MemAvailable": st["avail"]})
    async def pg(): return 1000
    monkeypatch.setattr(health_watch, "_pg_bytes", pg)
    import destinations
    async def emit(event_type, data, ws, **k): sent.append(data["text"]); return 1   # GĐ12.10: health events go through destinations.emit
    monkeypatch.setattr(destinations, "emit", emit)
    return health_watch, st, sent

def test_disk_alert_once(hw):
    m, st, sent = hw; st["disk"] = 0.90
    run(m.run_check()); run(m.run_check())
    assert len(sent) == 1 and "90%" in sent[0] and m.LAST["disk_used_ratio"] == 0.9
    m.SENT["disk"] -= 7 * 3600; run(m.run_check()); assert len(sent) == 2

def test_low_ram_alert(hw):
    m, st, sent = hw; st["avail"] = 5
    run(m.run_check()); assert len(sent) == 1 and "RAM" in sent[0]

def test_healthy_no_alert(hw):
    m, st, sent = hw; run(m.run_check()); assert not sent and m.LAST

def test_non_admin_403():
    import deepagent_api, admin
    assert not admin.allowed("editor", "GET", "/api/admin/health") and admin.allowed("admin", "GET", "/api/admin/health")

def test_headroom(monkeypatch):
    import capacity
    monkeypatch.setattr(capacity, "mem_available_mb", lambda: 1000)
    assert capacity.headroom_ok(400) and not capacity.headroom_ok(600)
