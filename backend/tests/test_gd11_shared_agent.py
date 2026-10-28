"""GĐ11.41 two users on one shared agent with a virtual computer (real server on its OWN port 8025, fake LLM => $0, FakeDriver boxes: AOS_SBX_FAKE_DRIVER=1).
agent_shares (app/agent_sharing.py) lets user B use A's agent; B must get a separate box and never see A's files. Throwaway workspaces + users.
The fake LLM calls `[tool:execute] {...}`; the FakeDriver understands `write PATH TEXT` and `cat PATH`."""
import json, os, subprocess, sys, time, uuid
import httpx, psycopg, pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PORT = 8025; B = f"http://127.0.0.1:{PORT}"
ADMIN = ("moza", "phase0-local"); PW = "phase0-gd1141-local"; SUF = uuid.uuid4().hex[:6]
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
def pg(): return psycopg.connect(PG, autocommit=True)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="starts the local server (WSL / Linux)")

@pytest.fixture(scope="module")
def server():
    log = open(os.path.join(ROOT, "var", "server_8025.log"), "w")
    env = {**os.environ, "PORT": str(PORT), "AOS_SBX_FAKE_DRIVER": "1", "AOS_EGRESS_ALLOW": os.environ.get("AOS_EGRESS_ALLOW", "127.0.0.0/8,::1"), "GOOGLE_API_KEY": "local-fake-no-calls"}
    env.pop("AOS_ENV", None)
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "overlay", "run_local.py")], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        for _ in range(120):
            try:
                if httpx.get(B + "/health", timeout=2).status_code != 0: break
            except httpx.HTTPError: time.sleep(1)
            if p.poll() is not None: pytest.fail("server on :8025 exited, see var/server_8025.log")
        else: pytest.fail("server on :8025 did not start in 120 s")
        yield
    finally:
        p.terminate()
        try: p.wait(20)
        except subprocess.TimeoutExpired: p.kill()
        log.close()

def client(auth): return httpx.Client(base_url=B, auth=auth, timeout=90)

def mkuser(a, name, ws, role="editor"):
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": role, "workspace_id": ws}); assert r.status_code == 200, r.text
    return r.json()

def chat(c, aid, thread, text):
    """one turn on a thread -> text of the last assistant message"""
    gid = f"agent-{aid}"
    tid = thread[0] if isinstance(thread, tuple) else c.post("/api/v1/threads", json={"metadata": {"source": "pytest", "graph_id": gid}}).json()["thread_id"]
    r = c.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": gid, "input": {"messages": [{"type": "human", "content": text}]}})
    assert r.status_code == 200, r.text
    msgs = c.get(f"/api/v1/threads/{tid}/state").json()["values"]["messages"]
    return tid, (msgs[-1]["content"] if isinstance(msgs[-1]["content"], str) else json.dumps(msgs[-1]["content"]))

def tool(cmd): return "[tool:execute] " + json.dumps({"command": cmd})

def boxes(aid):
    with pg() as c: return c.execute("SELECT owner_id, user_id, scope, thread_id, workspace_id FROM sandboxes WHERE kind='computer' AND agent_id=%s AND status<>'deleted' ORDER BY created", (aid,)).fetchall()

@pytest.fixture(scope="module")
def env(server):
    with client(ADMIN) as a:
        wa = a.post("/api/admin/workspaces", json={"name": f"d41-a-{SUF}"}).json()["id"]; wo = a.post("/api/admin/workspaces", json={"name": f"d41-o-{SUF}"}).json()["id"]
        ua, ub = mkuser(a, f"d41_a_{SUF}", wa), mkuser(a, f"d41_b_{SUF}", wa); uo = mkuser(a, f"d41_o_{SUF}", wo)
    A, Bc, O = client((f"d41_a_{SUF}", PW)), client((f"d41_b_{SUF}", PW)), client((f"d41_o_{SUF}", PW))
    mk = lambda name, comp: A.post("/api/agents", json={"name": f"{name} {SUF}", "model": "gpt-5.4-nano", "tools": [], **({"computer": comp} if comp else {})})
    ag, th, off = mk("May dung chung", {"mode": "agent"}), mk("May theo chat", {"mode": "thread"}), mk("Khong may", None)
    for r in (ag, th, off): assert r.status_code == 200, r.text
    ids = {"agent": ag.json()["id"], "thread": th.json()["id"], "off": off.json()["id"]}
    for aid in ids.values():
        r = A.post(f"/api/agents/{aid}/shares", json={"username": f"d41_b_{SUF}", "role": "use"}); assert r.status_code == 200, r.text    # shared to user B
    yield {"A": A, "B": Bc, "O": O, "ids": ids, "wa": wa, "wo": wo}
    for c in (A, Bc, O): c.close()
    with pg() as c:
        for aid in ids.values(): c.execute("DELETE FROM sandboxes WHERE agent_id=%s", (aid,))
        c.execute("DELETE FROM agents WHERE id = ANY(%s)", (list(ids.values()),))
        c.execute("DELETE FROM lg_runs WHERE workspace_id = ANY(%s)", ([wa, wo],)); c.execute("DELETE FROM lg_threads WHERE workspace_id = ANY(%s)", ([wa, wo],))
        c.execute("DELETE FROM users WHERE username LIKE %s", (f"d41\\_%\\_{SUF}",))

def test_shared_agent_user_b_gets_a_separate_box_and_never_sees_as_files(env):
    A, Bc, aid = env["A"], env["B"], env["ids"]["agent"]
    _, out = chat(A, aid, None, tool("write /workspace/a-secret.txt only-for-A")); assert "execute" in out
    _, out = chat(A, aid, None, tool("cat /workspace/a-secret.txt")); assert "only-for-A" in out            # A, another thread: the same box (mode agent)
    assert len(boxes(aid)) == 1
    _, out = chat(Bc, aid, None, tool("cat /workspace/a-secret.txt")); assert "only-for-A" not in out and "no such file" in out
    _, out = chat(Bc, aid, None, tool("write /workspace/b.txt mine")); assert "execute" in out
    rows = boxes(aid); assert len(rows) == 2 and len({r[1] for r in rows}) == 2 and {r[2] for r in rows} == {"agent"}      # one box per (agent, user)
    _, out = chat(A, aid, None, tool("cat /workspace/b.txt")); assert "mine" not in out                    # and not the other way round either
    assert len(boxes(aid)) == 2

def test_thread_mode_two_threads_two_boxes_and_the_same_thread_keeps_its_box(env):
    A, aid = env["A"], env["ids"]["thread"]
    t1, _ = chat(A, aid, None, tool("write /workspace/t.txt in-thread-1"))
    _, out = chat(A, aid, (t1,), tool("cat /workspace/t.txt")); assert "in-thread-1" in out                  # turn 2 of the same thread
    _, out = chat(A, aid, None, tool("cat /workspace/t.txt")); assert "in-thread-1" not in out               # a new thread = a new box
    rows = boxes(aid); assert len(rows) == 2 and len({r[3] for r in rows}) == 2 and {r[2] for r in rows} == {"thread"}

def test_mode_off_agent_creates_no_box(env):
    _, out = chat(env["A"], env["ids"]["off"], None, tool("echo hi"))
    assert boxes(env["ids"]["off"]) == []

def test_other_workspace_cannot_use_the_agent_and_creates_no_box(env):
    O = env["O"]
    for aid in env["ids"].values():
        r = O.post("/api/v1/threads", json={"metadata": {"source": "pytest", "graph_id": f"agent-{aid}"}})
        if r.status_code == 200:
            r = O.post(f"/api/v1/threads/{r.json()['thread_id']}/runs/wait", json={"assistant_id": f"agent-{aid}", "input": {"messages": [{"type": "human", "content": tool("write /workspace/x y")}]}})
        assert r.status_code in (403, 404, 422) or "execute" not in r.text.lower() or "Error" in r.text, r.text[:200]
    assert all(r[4] == env["wa"] for aid in env["ids"].values() for r in boxes(aid))                          # no box was created in the other workspace

def test_unauthenticated_cannot_run_the_agent(env):
    with httpx.Client(base_url=B, timeout=30) as anon:
        assert anon.post("/api/v1/threads", json={}).status_code in (401, 403)
    assert not any(r[4] == env["wo"] for aid in env["ids"].values() for r in boxes(aid))
