"""Demo account (21/10/2026): customer trial login = viewer + chat writes only, agent allow-list, no HITL approval, 1 run at a time,
per-user daily $ cap, write tools refused inside runs, owner 'moza' unaffected (login is exact-case). Local server :8020, fake LLM => $0."""
import asyncio, os, sys, uuid
import httpx, psycopg, pytest
B = "http://127.0.0.1:8020"; ADMIN = ("moza", "phase0-local")
SUF = uuid.uuid4().hex[:6]
DEMO = ("DemoT" + SUF, "demo-local-pass-123")   # temp demo user; the real "Moza" is only read (case-collision test)
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
ALLOWED = [2, 106, 107, 108, 109, 110, 111]

@pytest.fixture(scope="module")
def a():
    with httpx.Client(base_url=B, auth=ADMIN, timeout=90) as c: yield c

@pytest.fixture(scope="module")
def demo_id(a):
    """GĐ8.Z: a TEMPORARY demo user (never the real demo account 'Moza', which lives in its own demo workspace built by
    /api/admin/demo-workspace; patching it here used to drag it back to workspace 1 and broke the demo leak audit)."""
    r = a.post("/api/admin/users", json={"username": DEMO[0], "password": DEMO[1], "role": "viewer", "display_name": "Khách demo (test)",
                                         "demo": True, "daily_cap_usd": 2.0, "demo_agents": ALLOWED})
    assert r.status_code == 200, r.text
    uid = r.json()["id"]
    yield uid
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute("DELETE FROM lg_runs WHERE user_id=%s", (uid,)); c.execute("DELETE FROM lg_threads WHERE owner_id=%s", (uid,))
        c.execute("DELETE FROM memberships WHERE user_id=%s", (uid,)); c.execute("DELETE FROM users WHERE id=%s", (uid,))

@pytest.fixture(scope="module")
def d(demo_id):
    assert _login(*DEMO) == 200                     # the real login form path works
    with httpx.Client(base_url=B, auth=DEMO, timeout=90) as c: yield c   # Basic auth: the secure session cookie is not sent over plain http

def _login(u, p):
    with httpx.Client(base_url=B, timeout=30) as c: return c.post("/api/auth/login", json={"username": u, "password": p}).status_code

def test_case_variants_do_not_collide(a):
    """Owner 'moza' vs the real demo account 'Moza' (read only: wrong passwords must fail both ways, exact case)."""
    us = {u["username"]: u for u in a.get("/api/admin/users").json()}
    if "Moza" not in us: pytest.skip("no real demo account 'Moza' on this server")
    assert us["moza"]["is_superadmin"] and not us["Moza"]["is_superadmin"] and us["Moza"]["demo"]
    try:
        assert _login("moza", "demo-local-pass-123") == 401   # a demo-style password never opens the owner
        assert _login("Moza", ADMIN[1]) == 401         # owner password never opens the demo
        assert _login("MOZA", ADMIN[1]) == 401         # exact-case only
    finally:   # these deliberate failures feed the login backoff (admin._rate_ok); repeated runs within 1 h would make the owner's Basic calls 429
        with psycopg.connect(PG, autocommit=True) as c: c.execute("DELETE FROM audit_log WHERE detail='đăng nhập sai' AND username IN ('moza','Moza','MOZA')")
    assert "autocapitalize=none" in httpx.get(B + "/login").text

def test_me_is_demo_viewer(d):
    m = d.get("/api/auth/me").json()
    assert m["username"] == DEMO[0] and m["role"] == "viewer" and m["demo"] is True and m["daily_cap_usd"] == 2.0 and "spent_today_usd" in m

def test_demo_forced_viewer_even_if_membership_editor(a, demo_id, d):
    with psycopg.connect(PG) as c: c.execute("UPDATE memberships SET role='editor' WHERE user_id=%s", (demo_id,))
    try:
        assert d.get("/api/auth/me").json()["role"] == "viewer"
        assert d.put("/api/agents/2", json={}).status_code == 403
    finally:
        with psycopg.connect(PG) as c: c.execute("UPDATE memberships SET role='viewer' WHERE user_id=%s", (demo_id,))

def test_owner_cannot_be_demo(a):
    assert a.patch("/api/admin/users/1", json={"demo": True}).status_code == 400
    assert a.post("/api/admin/users", json={"username": "x" + uuid.uuid4().hex[:6], "password": "x" * 12, "role": "editor", "demo": True}).status_code == 422

def test_assistant_list_is_the_allow_list(d):
    ids = {x["agent_id"] for x in d.post("/api/v1/assistants/search", json={"limit": 100}).json()}
    assert ids and ids <= set(ALLOWED)

def _thread(c, g="agent-2"): 
    r = c.post("/api/v1/threads", json={"metadata": {"source": "chat", "graph_id": g}}); assert r.status_code == 200, r.text; return r.json()["thread_id"]
def _msg(t="Xin chào"): return {"messages": [{"type": "human", "content": t}]}

def test_chat_works_and_rename_pin_archive(d):
    tid = _thread(d)
    r = d.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": "agent-2", "input": _msg()}); assert r.status_code == 200, r.text
    assert d.patch(f"/api/v1/threads/{tid}", json={"metadata": {"title": "demo", "pinned": True}}).status_code == 200
    assert d.post("/api/chat/threads/search", json={"q": "demo"}).status_code == 200
    assert d.delete(f"/api/v1/threads/{tid}").status_code == 200

def test_agent_outside_allow_list_refused(d):
    tid = _thread(d, "agent-1")
    assert d.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": "agent-1", "input": _msg()}).status_code == 403

def test_no_hitl_approval_webhook_or_delay(d):
    tid = _thread(d)
    assert d.post(f"/api/v1/threads/{tid}/runs", json={"assistant_id": "agent-2", "command": {"resume": {"decisions": [{"type": "approve"}]}}}).status_code == 403
    assert d.post(f"/api/v1/threads/{tid}/runs", json={"assistant_id": "agent-2", "input": _msg(), "webhook": "https://example.com/x"}).status_code == 403
    assert d.post(f"/api/v1/threads/{tid}/runs", json={"assistant_id": "agent-2", "input": _msg(), "after_seconds": 60}).status_code == 403

def test_one_run_at_a_time(d, demo_id):
    tid = _thread(d); rid = str(uuid.uuid4())
    with psycopg.connect(PG) as c:
        c.execute("INSERT INTO lg_runs(run_id,thread_id,assistant_id,status,user_id,workspace_id) SELECT %s,%s,assistant_id,'running',%s,1 FROM lg_assistants WHERE graph_id='agent-2' LIMIT 1", (rid, tid, demo_id))
    try:
        assert d.post(f"/api/v1/threads/{_thread(d)}/runs/wait", json={"assistant_id": "agent-2", "input": _msg()}).status_code == 429
    finally:
        with psycopg.connect(PG) as c: c.execute("DELETE FROM lg_runs WHERE run_id=%s", (rid,))

def test_daily_cap(a, d, demo_id):
    assert a.patch(f"/api/admin/users/{demo_id}", json={"daily_cap_usd": 0}).status_code == 200
    try:
        r = d.post(f"/api/v1/threads/{_thread(d)}/runs/wait", json={"assistant_id": "agent-2", "input": _msg()})
        assert r.status_code == 402 and ("hạn mức" in r.json()["detail"] or "limit" in r.json()["detail"])
    finally:
        a.patch(f"/api/admin/users/{demo_id}", json={"daily_cap_usd": 2.0})
    # spend is summed from the user's own runs of the ICT day
    tid = _thread(d); rid = str(uuid.uuid4())
    with psycopg.connect(PG) as c:
        c.execute("""INSERT INTO lg_runs(run_id,thread_id,assistant_id,status,user_id,workspace_id,output) SELECT %s,%s,assistant_id,'success',%s,1,'{"cost_usd":2.01}'::jsonb
                     FROM lg_assistants WHERE graph_id='agent-2' LIMIT 1""", (rid, tid, demo_id))
    try:
        assert d.get("/api/auth/me").json()["spent_today_usd"] >= 2.01
        assert d.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": "agent-2", "input": _msg()}).status_code == 402
    finally:
        with psycopg.connect(PG) as c: c.execute("DELETE FROM lg_runs WHERE run_id=%s", (rid,))

SENSITIVE = [("PUT", "/api/agents/2"), ("POST", "/api/agents"), ("DELETE", "/api/agents/2"), ("PUT", "/api/settings/chat"), ("PUT", "/api/settings/voice"),
             ("PUT", "/api/settings/voice/agents/agent-2"), ("POST", "/api/admin/users"), ("PATCH", "/api/admin/users/1"), ("POST", "/api/admin/workspaces/1/members"),
             ("POST", "/api/auth/password"), ("POST", "/api/auth/keys"), ("POST", "/api/auth/workspace"), ("POST", "/api/chat/schedules"), ("PATCH", "/api/chat/schedules/1"),
             ("PUT", "/api/chat/code/settings"), ("POST", "/api/chat/code/run"), ("PUT", "/api/chat/memory/agents/agent-2"), ("PATCH", "/api/chat/memories"),
             ("DELETE", "/api/chat/memories"), ("POST", "/api/chat/shares"), ("POST", "/api/chat/canvas/abc"), ("POST", "/api/voice/session"), ("POST", "/api/voice/transcript"),
             ("POST", "/api/gateway/key"), ("POST", "/api/mcp/servers"), ("POST", "/api/v1/assistants"), ("PATCH", "/api/v1/assistants/agent-2"),
             ("POST", "/api/v1/runs/crons"), ("POST", "/api/v1/runs"), ("POST", "/api/v1/runs/wait"), ("POST", "/api/sandboxes")]

def test_sensitive_writes_403(d):
    bad = [(m, p, r.status_code) for m, p in SENSITIVE if (r := d.request(m, p, json={})).status_code != 403]
    assert not bad, bad

def test_every_non_get_route_is_403_unless_chat_allow_listed(a, d):
    import re
    spec = a.get("/openapi.json").json()
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
    ok_rx = [re.compile(x) for x in (r"POST /api/v1/threads$", r"(PATCH|DELETE) /api/v1/threads/[^/]+$", r"POST /api/v1/threads/[^/]+/runs(/stream|/wait)?$",
             r"POST /api/v1/threads/[^/]+/runs/[^/]+/cancel$", r"POST /api/v1/(assistants|threads)/search$", r"POST /api/chat/threads/search$", r"POST /api/chat/delegate$",
             r"POST /api/files$", r"POST /api/obs/feedback$", r"POST /api/obs/nl-filter$", r"POST /api/auth/(lang|logout|login)$")]
    public = ("/static/", "/d/", "/ingest/", "/share/", "/v1/", "/login", "/api/auth/login", "/api/auth/2fa/verify", "/favicon.ico", "/mock-oauth/",
              "/channels/zalo/",
              "/api/invites/",           # GĐ12.100: POST /api/invites/{token}/accept is public by design - the one-time invite token IS the credential (anonymous caller, no session); a bad/expired/used token -> 4xx, never a 403 gate
              "/otel/v1/",               # GĐ9.10: OTLP ingest, authenticated by its ingest key (x-api-key / Bearer), not by a user session
              "/internal/zalo-personal/")   # GĐ8.3b: sidecar callbacks, loopback + shared token only   # GĐ8.3: public Zalo OA webhook, POST-only, authenticated by X-ZEvent-Signature (no user session); bad signature -> 401, empty body -> 422
    leaks = []
    for path, ops in spec["paths"].items():
        for m in ops:
            M = m.upper()
            if M in ("GET", "HEAD", "OPTIONS"): continue
            p = re.sub(r"\{[^}]+\}", "x1", path)
            if p.startswith(public) or any(rx.match(f"{M} {p}") for rx in ok_rx): continue
            r = d.request(M, p, json={})
            if r.status_code != 403: leaks.append((M, path, r.status_code))
    assert not leaks, leaks

def test_admin_only_gets_403(d):
    for p in ("/api/admin/users", "/api/admin/audit", "/api/gw/keys", "/api/mcp/servers"):
        assert d.get(p).status_code == 403, p

def test_write_tools_refused_inside_demo_runs():
    sys.path[:0] = [os.path.join(os.path.dirname(__file__), "..", "app"), os.path.join(os.path.dirname(__file__), "..", "overlay")]
    os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls"); os.environ.setdefault("PG_URI", PG)
    import deepagent_api, admin, mocks  # noqa: F401  (app import order: deepagent_api first)
    from types import SimpleNamespace
    called = []
    async def handler(req): called.append(1); return "ran"
    async def go(user, name):
        tok = admin.USER.set(user)
        try: return await mocks.AuditHooks(109).awrap_tool_call(SimpleNamespace(tool_call={"name": name, "args": {}, "id": "t1"}), handler)
        finally: admin.USER.reset(tok)
    mocks._log = lambda *a, **k: asyncio.sleep(0)
    demo = {"id": 9, "role": "viewer", "demo": True}
    for n in admin.DEMO_TOOLS_DENY:
        r = asyncio.run(go(demo, n)); assert "demo" in r.content.lower() and not called
    assert asyncio.run(go(demo, "crm_list_deals")) == "ran"            # read-only tools still work
    assert asyncio.run(go({"id": 1, "role": "admin"}, "crm_log_deal")) == "ran"

def test_delegate_allowed_for_demo(d):
    r = d.post("/api/chat/delegate", json={"thread_id": "nope", "agent_id": "agent-106", "text": "@AI SEO hi"})
    assert r.status_code != 403   # reaches the handler (404 for a foreign/unknown thread)
