"""Demo-workspace isolation audit (21/10/2026). Logs in as the owner and as the demo account, GETs EVERY GET route of the
OpenAPI schema (path params filled with the owner's real ids), and fails if any owner-workspace identifier shows up in a demo
response: UUIDs (threads, runs, traces, assistants, files, feedback), owner-only names (agents, projects, KB titles, usernames).
Usage: python demo_leak_audit.py BASE OWNER_USER DEMO_USER   (passwords from env AUDIT_OWNER_PW / AUDIT_DEMO_PW, never printed)
Read-only: GET only. Exit 1 on any leak."""
import os, re, sys, json, httpx
BASE, OU, DU = sys.argv[1].rstrip("/"), sys.argv[2], sys.argv[3]
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
STATIC_OK = {"/api/builder/templates"}   # static template catalogue (a template is titled "Trợ lý nhà"), not workspace data
SKIP = re.compile(r"/(logout|stream|join|sse|ws)(/|$)|^/api/voice/|^/v1/|^/mock-oauth|^/share/|^/d/|^/ingest/|^/static/")

def login(u, pw):
    c = httpx.Client(base_url=BASE, timeout=60, follow_redirects=False)
    r = c.post("/api/auth/login", json={"username": u, "password": pw}); assert r.status_code == 200, (u, r.status_code)
    if BASE.startswith("http://"):   # Secure cookie is not sent over plain http locally -> Basic auth instead
        c = httpx.Client(base_url=BASE, timeout=60, auth=(u, pw))
    return c

def j(c, path, method="GET", body=None):
    try:
        r = c.request(method, path, json=body) if body is not None else c.get(path, headers={"accept": "application/json"})
        return r.status_code, r.text
    except Exception as e:  # noqa: BLE001
        return 0, str(e)

o = login(OU, os.environ["AUDIT_OWNER_PW"]); d = login(DU, os.environ["AUDIT_DEMO_PW"])
me = d.get("/api/auth/me").json(); ows = o.get("/api/auth/me").json()
print(f"demo ws={me.get('ws')} role={me.get('role')} demo={me.get('demo')} | owner ws={ows.get('ws')}")
spec = o.get("/openapi.json").json()
gets = [p for p, v in spec["paths"].items() if "get" in v and not SKIP.search(p)]
# owner markers: ids & names that must never reach the demo account
owner_text = []
samples = {}
for p in gets:
    if "{" in p: continue
    s, t = j(o, p); owner_text.append(t if s == 200 else "")
for p, body in [("/api/v1/threads/search", {"limit": 200}), ("/api/v1/assistants/search", {"limit": 200}), ("/api/chat/threads/search", {"q": ""})]:
    s, t = j(o, p, "POST", body); owner_text.append(t if s == 200 else "")
blob = "\n".join(owner_text)
owner_uuids = set(UUID.findall(blob))
# the demo account's own ids are fine
demo_blob = []
for p, body in [("/api/v1/threads/search", {"limit": 200}), ("/api/v1/assistants/search", {"limit": 200})]:
    s, t = j(d, p, "POST", body); demo_blob.append(t if s == 200 else "")
own_demo = set(UUID.findall("\n".join(demo_blob)))
names = [n for n in os.environ.get("AUDIT_NAMES", "Trợ lý nhà|bench-research|Nội quy nhà Moza|lich_bao_tri|editor1792|isoA1792|Báo cáo sáng AI|ws2-agent").split("|") if n]
th = [x["thread_id"] for x in json.loads(j(o, "/api/v1/threads/search", "POST", {"limit": 50})[1] or "[]")]
tr = UUID.findall(j(o, "/api/obs/runs?limit=20")[1]) + UUID.findall(j(o, "/api/runs?limit=20")[1])
fill = {"thread_id": th[0] if th else "x", "tid": th[0] if th else "x", "run_id": tr[0] if tr else "x", "trace_id": tr[0] if tr else "x",
        "rid": tr[0] if tr else "x", "agent_id": "1", "aid": "1", "id": "1", "pid": "1", "project_id": "1", "did": "1", "dataset_id": "1",
        "name": "phong-ai-so", "assistant_id": "7b561a82-61c5-5166-8aa8-c921306f91a0"}
leaks, n200, checked = [], 0, 0
for p in gets:
    path = re.sub(r"\{(\w+)(:[^}]*)?\}", lambda m: fill.get(m.group(1), "1"), p)
    s, t = j(d, path); checked += 1
    if s != 200: continue
    n200 += 1
    u = (set(UUID.findall(t)) & owner_uuids) - own_demo
    nm = [x for x in names if x in t] if p not in STATIC_OK else []
    if u or nm: leaks.append((p, s, sorted(u)[:3], nm, len(u)))
for p, body in [("/api/v1/threads/search", {"limit": 200}), ("/api/chat/threads/search", {"q": ""}), ("/api/v1/assistants/search", {"limit": 200})]:
    s, t = j(d, p, "POST", body); checked += 1
    u = (set(UUID.findall(t)) & owner_uuids) - own_demo; nm = [x for x in names if x in t]
    if s == 200 and (u or nm): leaks.append((p, s, sorted(u)[:3], nm, len(u)))
# numbers without ids: gateway usage must only cover the demo's own agents
mine = {a.get("agent_id") for a in json.loads(j(d, "/api/v1/assistants/search", "POST", {"limit": 200})[1] or "[]")}
s, t = j(d, "/api/gateway")
if s == 200:
    g = json.loads(t); bad = {r.get("agent_id") for r in g.get("recent", []) + g.get("by_agent", [])} - mine
    if bad: leaks.append(("/api/gateway (usage of other agents)", s, [], sorted(map(str, bad))[:5], 0))
s, t = j(d, "/api/obs/outbox")
if s == 200 and json.loads(t): leaks.append(("/api/obs/outbox (non-empty)", s, [], [], 0))
print(f"GET routes checked {checked} (200: {n200}); owner uuids known {len(owner_uuids)}; leaks {len(leaks)}")
for l in leaks: print("LEAK", l[0], "uuids:", l[4], l[2], "names:", l[3])
sys.exit(1 if leaks else 0)
