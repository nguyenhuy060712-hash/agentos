"""GĐ11 card 11.60, end to end on OUR OWN server (port 8043, fake LLM, $0): the real lifespan starts the mail pollers, the real run queue runs the mail, and the only mailbox is the
FAKE Google server (tests/fake_workspace_api.py in a thread of this test on 127.0.0.1:18043). Nothing talks to Google / Microsoft / the shared server on :8020.
Flow: workspace + owner + agent (fixed identity) -> Google preset -> the owner's connection -> POST /api/agents/{id}/channels kind=gmail -> baseline -> a mail arrives -> poll-now ->
one EXTERNAL run (trust=external, UNTRUSTED DATA block, owner's Inbox thread) -> pause / resume / roles / other workspace through HTTP. The server is killed by its PID at the end."""
import asyncio, os, subprocess, sys, time, uuid
os.environ.setdefault("GOOGLE_API_KEY", "x-not-used")   # only so deepagent_api can be imported here to write the owner's token; the SERVER has its own fakes
import httpx, psycopg, pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app")); sys.path.insert(0, HERE)
import fake_workspace_api as F  # noqa: E402

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
PORT, FPORT = 8043, 18043
B = f"http://127.0.0.1:{PORT}"; ADMIN = ("moza", "phase0-local"); PW = "phase0-gd1160-local"; SUF = uuid.uuid4().hex[:6]

def pg(): return psycopg.connect(PG, autocommit=True, row_factory=dict_row)

def wait_for(fn, what, secs=60, step=0.5):
    t0 = time.time(); last = None
    while time.time() - t0 < secs:
        last = fn()
        if last: return last
        time.sleep(step)
    raise AssertionError(f"timeout waiting for {what}: {last!r}")

@pytest.fixture(scope="module")
def srv():
    stop = F.start_in_thread(FPORT)
    env = {**os.environ, "PORT": str(PORT), "PG_URI": PG, "GOOGLE_API_BASE": f"http://127.0.0.1:{FPORT}/", "GRAPH_API_BASE": f"http://127.0.0.1:{FPORT}/graph/v1.0/",
           "GOOGLE_OAUTH_BASE": f"http://127.0.0.1:{FPORT}/google/oauth", "MS_OAUTH_BASE": f"http://127.0.0.1:{FPORT}/microsoft/oauth", "AOS_EGRESS_ALLOW": "127.0.0.0/8,::1",
           "AOS_MAIL_TICK_S": "1", "AOS_MAIL_SUPERVISOR_S": "1", "TELEGRAM_POLL": "0", "SLACK_SOCKET": "0", "AOS_ZALO_PERSONAL": "0"}
    os.makedirs(os.path.join(ROOT, "var"), exist_ok=True)
    log = open(os.path.join(ROOT, "var", f"server_{PORT}.log"), "wb")
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "overlay", "run_local.py")], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        def up():
            try: return httpx.get(B + "/health", timeout=3).status_code < 500
            except httpx.HTTPError: return False
        wait_for(up, "own server on :8043", secs=300, step=1.0)
        yield p
    finally:
        p.terminate()                                    # this PID only: never pkill run_local.py (it would kill every agent's server)
        try: p.wait(timeout=20)
        except subprocess.TimeoutExpired: p.kill()
        log.close(); stop()

def user(a, name, ws, role):
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": role, "workspace_id": ws}); assert r.status_code == 200, r.text
    return r.json()["id"], httpx.Client(base_url=B, auth=(name, PW), timeout=120)

async def _tokens(uid, ws, who):
    import deepagent_api as A, admin, connections as C, office_scopes as OS
    async with AsyncConnectionPool(PG, min_size=1, max_size=2, open=False, kwargs={"autocommit": True, "row_factory": dict_row}) as pool:
        await pool.open(); A.S["pool"] = pool; A.S["rls"] = admin.rls_ctx
        p = await A.q("SELECT * FROM oauth_providers WHERE workspace_id=%s AND name='google'", (ws,), one=True)
        await C.save_tokens(("user", uid), p, F._issue("google", who, OS.scope_string("google", "write")), account=f"{who}@fake.test")

@pytest.fixture(scope="module")
def env(srv):
    os.environ.setdefault("PG_URI", PG)
    with httpx.Client(base_url=B, auth=ADMIN, timeout=120) as a:
        w1 = a.post("/api/admin/workspaces", json={"name": f"mt-w1-{SUF}"}).json()["id"]; w2 = a.post("/api/admin/workspaces", json={"name": f"mt-w2-{SUF}"}).json()["id"]
        e = {"w1": w1, "w2": w2}
        for k, role, ws in (("owner", "admin", w1), ("viewer", "viewer", w1), ("other", "admin", w2)):
            e[k + "_id"], e[k] = user(a, f"mt_{k}_{SUF}", ws, role)
    r = e["owner"].post("/api/agents", json={"name": f"mt-agent-{SUF}", "system_prompt": "Bạn là trợ lý hòm thư."}); assert r.status_code == 200, r.text
    e["agent"] = r.json()["id"]
    assert e["owner"].put("/api/workspace-tools", json={"google": "write", "ms365": "off"}).status_code == 200
    assert e["owner"].post("/api/workspace-tools/presets/google", json={"client_id": "fake-google-client", "client_secret": "fake-secret"}).status_code == 200
    asyncio.run(_tokens(e["owner_id"], w1, "alice"))
    yield e
    for k in ("owner", "viewer", "other"): e[k].close()
    with pg() as c:
        c.execute("DELETE FROM approvals WHERE agent_id=%s", (e["agent"],))
        for t in ("channel_bindings", "lg_runs", "lg_threads", "agent_audit", "oauth_connections", "connections", "oauth_providers", "oauth_audit"):
            c.execute(f"DELETE FROM {t} WHERE workspace_id IN (%s,%s)", (e["w1"], e["w2"]))
        c.execute("DELETE FROM agent_identity WHERE agent_id=%s", (e["agent"],)); c.execute("DELETE FROM agents WHERE workspace_id IN (%s,%s)", (e["w1"], e["w2"]))
        c.execute("DELETE FROM users WHERE username LIKE %s", (f"mt\\_%\\_{SUF}",))

def test_new_mail_runs_the_agent_externally_on_the_owners_inbox(env):
    ow, vw, ot, aid = env["owner"], env["viewer"], env["other"], env["agent"]
    # rules at create time: identity must be fixed first; viewers and other workspaces cannot create
    r = ow.post(f"/api/agents/{aid}/channels", json={"kind": "gmail"}); assert r.status_code == 409, r.text
    assert ow.put(f"/api/agents/{aid}/identity", json={"mode": "agent"}).status_code == 200
    assert vw.post(f"/api/agents/{aid}/channels", json={"kind": "gmail"}).status_code == 403
    assert ot.post(f"/api/agents/{aid}/channels", json={"kind": "gmail"}).status_code == 404
    r = ow.post(f"/api/agents/{aid}/channels", json={"kind": "gmail", "name": "Hòm thư", "config": {"poll_s": 30, "max_runs_per_hour": 5}}); assert r.status_code == 200, r.text
    bid = r.json()["id"]; env["bid"] = bid
    assert r.json()["config"]["max_runs_per_hour"] == 5 and r.json()["secret_ref"] is None
    st = wait_for(lambda: (s := ow.get(f"/api/channels/{bid}/status").json()) and s["armed"] and s, "baseline")        # the supervisor started the poller, the first round armed it
    assert st["state"] == "active" and st["queued_24h"] == 0
    F.deliver("alice", "Lan <lan@khach.vn>", "Báo giá đơn hàng", "Chị Lan cần báo giá 50 áo thun.", thread="th-e2e", mid="e2e-1", fresh=True)
    F.deliver("alice", "Tin <news@khach.vn>", "Bản tin tuần", "x", mid="e2e-list", fresh=True, headers={"List-Id": "<news>"})
    st = ow.post(f"/api/channels/{bid}/poll-now").json()
    st = wait_for(lambda: (s := ow.get(f"/api/channels/{bid}/status").json()) and s["queued_24h"] == 1 and s, "one queued run")
    assert st["state"] == "active" and st["errors"] == 0
    with pg() as c:
        run = wait_for(lambda: (x := c.execute("SELECT * FROM lg_runs WHERE metadata @> %s::jsonb", ('{"mail_id": "e2e-1"}',)).fetchone()) and x["status"] in ("success", "error", "interrupted") and x, "run end", secs=120)
        assert run["status"] == "success", run
        md = run["metadata"]; assert md["trust"] == "external" and md["channel"] == "gmail" and md["binding_id"] == bid and md["external_user"] == "lan@khach.vn"
        th = c.execute("SELECT owner_id, workspace_id, metadata FROM lg_threads WHERE thread_id=%s", (run["thread_id"],)).fetchone()
        assert th["owner_id"] == env["owner_id"] and th["workspace_id"] == env["w1"] and th["metadata"]["source"] == "channel" and th["metadata"]["title"] == "Báo giá đơn hàng"
        seen = {r["msg_id"]: r["outcome"] for r in c.execute("SELECT msg_id, outcome FROM mail_seen WHERE binding_id=%s", (bid,)).fetchall()}
        assert seen == {"e2e-1": "queued", "e2e-list": "skipped_bulk"}
        assert c.execute("SELECT count(*) n FROM lg_runs WHERE metadata @> %s::jsonb", ('{"binding_id": %d}' % bid,)).fetchone()["n"] == 1
        reply = (run["output"] or {}).get("reply", "")
    assert "UNTRUSTED DATA" in reply and "Báo giá đơn hàng" in reply                                                           # the fake model echoes what it was given: the mail was inside the untrusted block
    inbox = ow.get("/api/inbox", params={"agent_id": aid}).json(); assert any(i["thread_id"] == run["thread_id"] for i in inbox["items"])   # the owner finds it in the Inbox
    assert not vw.get("/api/inbox", params={"agent_id": aid}).json()["items"]
    # nothing but alice's mailbox was read
    assert {c[1] for c in F.S["log"] if c[0] == "google" and c[1] != "(injected)"} == {"alice"}

def test_pause_resume_roles_and_other_workspace_over_http(env):
    ow, vw, ot, bid = env["owner"], env["viewer"], env["other"], env["bid"]
    assert ot.get(f"/api/channels/{bid}/status").status_code == 404 and ot.post(f"/api/channels/{bid}/pause").status_code == 404
    assert vw.get(f"/api/channels/{bid}/status").status_code == 200
    for p in ("pause", "resume", "poll-now"): assert vw.post(f"/api/channels/{bid}/{p}").status_code == 403, p
    assert ow.post(f"/api/channels/{bid}/pause").json()["state"] == "paused"
    F.deliver("alice", "Lan <lan@khach.vn>", "Lúc tạm dừng", "x", mid="e2e-2", fresh=True)
    assert ow.post(f"/api/channels/{bid}/poll-now").status_code == 409
    time.sleep(3)
    with pg() as c: assert not c.execute("SELECT 1 FROM lg_runs WHERE metadata @> %s::jsonb", ('{"mail_id": "e2e-2"}',)).fetchone()
    assert ow.post(f"/api/channels/{bid}/resume").json()["state"] == "active"
    ow.post(f"/api/channels/{bid}/poll-now")
    wait_for(lambda: ow.get(f"/api/channels/{bid}/status").json()["queued_24h"] == 2, "second run after resume")
    # the simulator is not for mail triggers; the agent leaving fixed identity stops the trigger
    assert ow.post(f"/api/channels/{bid}/simulate", json={"text": "hi"}).status_code == 422
    assert ow.put(f"/api/agents/{env['agent']}/identity", json={"mode": "user"}).status_code == 200
    time.sleep(6)                                                                                                             # agent_identity caches the mode for 5 s
    F.deliver("alice", "Lan <lan@khach.vn>", "Sau khi đổi", "x", mid="e2e-3", fresh=True)
    ow.post(f"/api/channels/{bid}/poll-now")
    st = wait_for(lambda: (s := ow.get(f"/api/channels/{bid}/status").json()) and s["state"] == "error" and s, "stopped after identity change")
    assert st["state_reason"] == "identity_not_fixed" and st["queued_24h"] == 2
    assert ow.post(f"/api/channels/{bid}/resume").status_code == 409
