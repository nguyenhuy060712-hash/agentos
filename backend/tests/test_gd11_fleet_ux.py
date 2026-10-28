"""GĐ11.35 thread inbox (unread / awaiting + auto title). In-process like test_gd10_queues: migration 0036 applied to the local DB, handlers called with a faked
current user (admin.USER) on a private pool, fake gateway for the title (no model call). Rows are removed at the end."""
import asyncio, os, sys, uuid
import psycopg, pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from fastapi import HTTPException

os.environ.setdefault("GOOGLE_API_KEY", "x-not-used")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import deepagent_api as A  # noqa: E402,I001  (must come first: circular imports)
import admin, migrations, thread_inbox  # noqa: E402

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
UID = 9100 + uuid.uuid4().int % 800
os.environ.setdefault("PG_URI", PG)   # 11.34: crypto.seal (agent OAuth tokens) reads its DEKs through PG_URI

def U(uid=None, ws=1, **kw): return {"id": uid or UID, "username": f"u{uid or UID}", "role": "editor", "ws": ws, **kw}

def run(coro_fn):
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as c:
            await migrations.run_pending(c)
        async with AsyncConnectionPool(PG, min_size=1, max_size=3, open=False, kwargs={"autocommit": True, "row_factory": dict_row}) as pool:
            await pool.open(); A.S["pool"] = pool; A.S["rls"] = admin.rls_ctx
            await A.q("GRANT SELECT, INSERT, UPDATE, DELETE ON thread_reads TO aos_rls")
            await admin._policy("thread_reads", "aos_ws() IS NULL OR workspace_id = aos_ws()")   # what rls_init does at boot (table is in RLS_DIRECT)
            await A.q("ALTER TABLE thread_reads ALTER COLUMN workspace_id SET DEFAULT coalesce(aos_ws(), 1)")
            return await coro_fn()
    return asyncio.run(go())

async def as_(user, fn, *a, **kw):
    tok = admin.USER.set(user)
    try: return await fn(*a, **kw)
    finally: admin.USER.reset(tok)

async def mk_thread(status="idle", with_run=True, owner=None, ws=1, meta=None):
    tid = "t-" + uuid.uuid4().hex[:10]
    await A.q("INSERT INTO lg_threads(thread_id,metadata,status,workspace_id,owner_id) VALUES(%s,%s,%s,%s,%s)",
              (tid, admin_json(meta or {}), status, ws, owner or UID))
    if with_run:
        await A.q("INSERT INTO lg_runs(run_id,thread_id,status,input,workspace_id,owner_id) VALUES(%s,%s,'success',%s,%s,%s)",
                  (str(uuid.uuid4()), tid, admin_json({"messages": [{"role": "user", "content": "Đặt lịch họp với khách hàng ngày mai"}]}), ws, owner or UID))
    return tid

def admin_json(x):
    from psycopg.types.json import Jsonb
    return Jsonb(x)

async def cleanup(*tids):
    for t in tids:
        await A.q("DELETE FROM thread_reads WHERE thread_id=%s", (t,)); await A.q("DELETE FROM lg_runs WHERE thread_id=%s", (t,)); await A.q("DELETE FROM lg_threads WHERE thread_id=%s", (t,))

async def ids(filter="all"): return [i["thread_id"] for i in (await thread_inbox.inbox(filter=filter))["items"]]

def test_inbox_unread_read_awaiting():
    async def go():
        tid = await mk_thread()
        try:
            me = U()
            assert tid in await as_(me, ids, "unread")                      # never read + has a run -> unread
            await as_(me, thread_inbox.mark_read, tid)
            assert tid not in await as_(me, ids, "unread") and tid in await as_(me, ids, "all")
            await A.q("UPDATE lg_threads SET updated_at=now() WHERE thread_id=%s", (tid,))   # a new message / run end
            assert tid in await as_(me, ids, "unread")
            await A.q("UPDATE lg_threads SET status='busy' WHERE thread_id=%s", (tid,))
            assert tid not in await as_(me, ids, "unread")                  # running is not unread
            await A.q("UPDATE lg_threads SET status='interrupted' WHERE thread_id=%s", (tid,))
            assert tid in await as_(me, ids, "awaiting")
            r = await as_(me, thread_inbox.inbox, filter="all")
            assert r["counts"]["awaiting"] >= 1 and next(i for i in r["items"] if i["thread_id"] == tid)["awaiting"] is True
            await A.q("UPDATE lg_threads SET status='idle' WHERE thread_id=%s", (tid,))
            assert tid not in await as_(me, ids, "awaiting")
            assert tid not in await as_(U(UID + 1000), ids, "all")          # someone else's thread is not in my inbox
        finally: await cleanup(tid)
    run(go)

def test_inbox_pagination_and_agent_filter():
    async def go():
        ts = [await mk_thread() for _ in range(3)]
        try:
            r = await as_(U(), thread_inbox.inbox, filter="all", limit=2)
            assert len(r["items"]) == 2 and r["next_cursor"]
            r2 = await as_(U(), thread_inbox.inbox, filter="all", cursor=r["next_cursor"], limit=2)
            assert {i["thread_id"] for i in r2["items"]}.isdisjoint({i["thread_id"] for i in r["items"]}) and r2["items"]
            assert (await as_(U(), thread_inbox.inbox, agent_id=2147000000, filter="all"))["items"] == []
        finally: await cleanup(*ts)
    run(go)

def test_inbox_workspace_isolation():
    async def go():
        tid = await mk_thread()
        try:
            await as_(U(), thread_inbox.mark_read, tid)
            other = U(ws=987654)
            with pytest.raises(HTTPException) as e: await as_(other, thread_inbox.mark_read, tid)
            assert e.value.status_code == 404
            assert (await as_(other, A.q, "SELECT count(*) n FROM thread_reads WHERE thread_id=%s", (tid,), one=True))["n"] == 0
            assert (await as_(U(), A.q, "SELECT count(*) n FROM thread_reads WHERE thread_id=%s", (tid,), one=True))["n"] == 1
        finally: await cleanup(tid)
    run(go)

def test_inbox_demo_is_off():
    async def go():
        tid = await mk_thread()
        try:
            d = U(demo=True)
            assert (await as_(d, thread_inbox.inbox))["items"] == []
            with pytest.raises(HTTPException) as e: await as_(d, thread_inbox.mark_read, tid)
            assert e.value.status_code == 403
        finally: await cleanup(tid)
    run(go)

def test_inbox_title_set_once(monkeypatch):
    calls = []
    async def fake_generate(client, prompt, chain, **kw):
        calls.append(kw.get("feature")); return '"Họp với khách hàng."\n', "fake", 5, 5
    monkeypatch.setattr(A.gw, "generate", fake_generate)
    async def go():
        tid, tid2 = await mk_thread(), await mk_thread(meta={"title": "Đã có tên"})
        try:
            assert await as_(U(), thread_inbox.auto_title, tid) == "Họp với khách hàng"
            assert await as_(U(), thread_inbox.auto_title, tid) is None     # second pause: no second call, title unchanged
            assert await as_(U(), thread_inbox.auto_title, tid2) is None     # user / earlier title is never overwritten
            assert calls == ["chat_title"]
            assert (await A.q("SELECT metadata->>'title' t FROM lg_threads WHERE thread_id=%s", (tid,), one=True))["t"] == "Họp với khách hàng"
        finally: await cleanup(tid, tid2)
    run(go)

# ---------------------------------------------------------------- 11.36 send while awaiting approval (API, fake LLM, own server: AOS_TEST_BASE=http://127.0.0.1:8024)
import httpx  # noqa: E402
B = os.environ.get("AOS_TEST_BASE", "http://127.0.0.1:8020"); ADMIN = ("moza", "phase0-local")

def _calls(ms, name): return [c for m in ms if m.get("type") == "ai" for c in (m.get("tool_calls") or []) if c["name"] == name]
def _tool_msgs(ms, name): return [m for m in ms if m.get("type") == "tool" and m.get("name") == name]

def test_resume_reject_then_new_message():
    """What the chat does when the user types while a run waits for approval: reject every pending action (decisions with the fixed message),
    then send the new message in the same thread. The old tool must never run, and the thread must be idle and usable afterwards."""
    with httpx.Client(base_url=B, auth=ADMIN, timeout=90) as a:
        cfg = a.get("/api/agents/1").json()["config"]; before = dict(cfg)
        cfg = {**cfg, "tools": sorted(set(cfg.get("tools", [])) | {"local:present", "local:ads_create_campaign"}), "interrupt_on": sorted(set(cfg.get("interrupt_on", [])) | {"local:ads_create_campaign"})}
        assert a.put("/api/agents/1", json=cfg).status_code == 200
        try:
            tid = a.post("/api/v1/threads", json={"metadata": {"source": "pytest", "graph_id": "agent-1"}}).json()["thread_id"]
            r = a.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": "agent-1", "input": {"messages": [{"type": "human", "content": "Tạo chiến dịch Drip bag Tết 2027 trên Facebook"}]}})
            assert r.status_code == 200, r.text
            assert a.get(f"/api/v1/threads/{tid}").json()["status"] == "interrupted"
            st = a.get(f"/api/v1/threads/{tid}/state").json()
            ints = [i for t in st["tasks"] for i in (t.get("interrupts") or [])]
            n = len(ints[0]["value"]["action_requests"]); assert n >= 1
            rej = [{"type": "reject", "message": "Người dùng gửi tin mới, bỏ yêu cầu này"}] * n
            r2 = a.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": "agent-1", "command": {"resume": {"decisions": rej}}})
            assert r2.status_code == 200, r2.text
            r3 = a.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": "agent-1", "input": {"messages": [{"type": "human", "content": "Thôi, cho tôi biết ngân sách tháng này còn bao nhiêu"}]}})
            assert r3.status_code == 200, r3.text
            ms = a.get(f"/api/v1/threads/{tid}/state").json()["values"]["messages"]
            assert not any("launched on facebook" in str(m.get("content")) for m in _tool_msgs(ms, "ads_create_campaign")), "the rejected tool must not run"
            assert a.get(f"/api/v1/threads/{tid}").json()["status"] in ("idle", "interrupted")
            assert sum(1 for m in ms if m.get("type") == "human") == 2
        finally:
            a.put("/api/agents/1", json=before)

# ---------------------------------------------------------------- 11.34 archive / permanent delete (in-process; own throw-away workspaces, everything removed at the end)
import hashlib, threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402
import agent_purge as P, attachments as AT, channels, connections, mcp_registry  # noqa: E402,I001

def run34(coro_fn):
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as c:
            await migrations.run_pending(c)
        async with AsyncConnectionPool(PG, min_size=1, max_size=4, open=False, kwargs={"autocommit": True, "row_factory": dict_row}) as pool:
            await pool.open(); A.S["pool"] = pool; A.S["rls"] = admin.rls_ctx
            await A.q("GRANT SELECT, INSERT, UPDATE, DELETE ON agent_delete_exports TO aos_rls")
            await A.q("GRANT USAGE, SELECT ON SEQUENCE agent_delete_exports_id_seq TO aos_rls")
            await admin._policy("agent_delete_exports", "aos_ws() IS NULL OR workspace_id = aos_ws()")   # what rls_init does at boot (table is in RLS_DIRECT)
            await A.q("ALTER TABLE agent_delete_exports ALTER COLUMN workspace_id SET DEFAULT coalesce(aos_ws(), 1)")
            P.QUEUE.clear()
            return await coro_fn()
    return asyncio.run(go())

def U34(role="editor", uid=None, ws=1, **kw): return {"id": uid or UID, "username": f"u{uid or UID}", "role": role, "ws": ws, **kw}
def ADM(ws): return U34("admin", UID + 1, ws)

async def mk_ws(demo=False):
    return (await A.q("INSERT INTO workspaces(name, demo) VALUES(%s,%s) RETURNING id", ("t34-" + uuid.uuid4().hex[:8], demo), one=True))["id"]

async def mk_agent(ws, owner=None, visibility="workspace", hidden=False, deleted=False, name=None, ident=None):
    name = name or "t34-" + uuid.uuid4().hex[:6]
    aid = (await A.q("INSERT INTO agents(name, workspace_id, owner_id, visibility, hidden, deleted, current_version, identifier) VALUES(%s,%s,%s,%s,%s,%s,1,%s) RETURNING id",
                     (name, ws, owner, visibility, hidden, deleted, ident), one=True))["id"]
    await A.q("INSERT INTO agent_versions(agent_id, version, config) VALUES(%s,1,%s)", (aid, Jsonb({"name": name, "model": "fake", "tools": [], "system_prompt": "x"})))
    return aid, name

async def n(sql, args=()): return (await A.q(f"SELECT count(*) c FROM ({sql}) t", args, one=True))["c"]

async def seed(aid, ws, other_user, sha, tmp):
    """One row in every table the purge must empty (thread owned by ANOTHER user = private chat the owner can't see under RLS). Returns [(label, count-sql, args)]."""
    sfx = uuid.uuid4().hex[:8]; asst, tid, tid2, run1, run2 = uuid.uuid4(), "t34a-" + sfx, "t34b-" + sfx, uuid.uuid4(), uuid.uuid4(); ck = []
    await A.q("INSERT INTO lg_assistants(assistant_id, graph_id, agent_id, agent_version, workspace_id) VALUES(%s,'agent',%s,1,%s)", (asst, aid, ws))
    ck.append(("lg_assistants", "SELECT 1 FROM lg_assistants WHERE agent_id=%s", (aid,)))
    await A.q("INSERT INTO lg_threads(thread_id, assistant_id, agent_id, workspace_id, owner_id) VALUES(%s,%s,%s,%s,%s)", (tid, asst, aid, ws, other_user))
    await A.q("INSERT INTO lg_threads(thread_id, assistant_id, workspace_id, owner_id) VALUES(%s,%s,%s,%s)", (tid2, asst, ws, other_user))   # agent_id NULL: reached through the assistant
    ck.append(("lg_threads", "SELECT 1 FROM lg_threads WHERE thread_id IN (%s,%s)", (tid, tid2)))
    for rid, t in ((run1, tid), (run2, tid2)):
        await A.q("INSERT INTO lg_runs(run_id, thread_id, assistant_id, status, workspace_id, owner_id) VALUES(%s,%s,%s,'success',%s,%s)", (rid, t, asst, ws, other_user))
        await A.q("INSERT INTO lg_run_events(run_id, seq, event) VALUES(%s,1,'x')", (rid,))
    ck += [("lg_runs", "SELECT 1 FROM lg_runs WHERE run_id IN (%s,%s)", (run1, run2)), ("lg_run_events", "SELECT 1 FROM lg_run_events WHERE run_id IN (%s,%s)", (run1, run2))]
    await A.q("INSERT INTO checkpoints(thread_id, checkpoint_ns, checkpoint_id, checkpoint) VALUES(%s,'','c1','{}')", (tid,))
    await A.q("INSERT INTO checkpoint_writes(thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, blob) VALUES(%s,'','c1','t',0,'ch',%s)", (tid2, b"x"))
    await A.q("INSERT INTO checkpoint_blobs(thread_id, checkpoint_ns, channel, version, type) VALUES(%s,'','ch','1','t')", (tid,))
    ck += [(t, f"SELECT 1 FROM {t} WHERE thread_id IN (%s,%s)", (tid, tid2)) for t in ("checkpoints", "checkpoint_writes", "checkpoint_blobs")]
    await A.q("INSERT INTO thread_reads(workspace_id, user_id, thread_id) VALUES(%s,%s,%s)", (ws, other_user, tid)); ck.append(("thread_reads", "SELECT 1 FROM thread_reads WHERE thread_id=%s", (tid,)))
    await A.q("INSERT INTO chat_code_runs(user_id, workspace_id, thread_id) VALUES(%s,%s,%s)", (other_user, ws, tid)); ck.append(("chat_code_runs", "SELECT 1 FROM chat_code_runs WHERE thread_id=%s", (tid,)))
    await A.q("INSERT INTO kb_docs(kb, title, n_chunks, workspace_id, thread_id) VALUES('t34','d',0,%s,%s)", (ws, tid)); ck.append(("kb_docs", "SELECT 1 FROM kb_docs WHERE thread_id=%s", (tid,)))
    await A.q("INSERT INTO chat_canvas(doc_id, version, thread_id, title, kind, content, author) VALUES(%s,1,%s,'t','md','x','u')", ("d" + sfx, tid)); ck.append(("chat_canvas", "SELECT 1 FROM chat_canvas WHERE thread_id=%s", (tid,)))
    fid = uuid.uuid4()
    await A.q("INSERT INTO chat_files(id, sha256, mime_type, size, name, workspace_id, owner_id) VALUES(%s,%s,'text/plain',3,'f.txt',%s,%s)", (fid, sha, ws, other_user))
    await A.q("INSERT INTO chat_thread_files(thread_id, file_id) VALUES(%s,%s)", (tid, fid))
    ck += [("chat_files", "SELECT 1 FROM chat_files WHERE id=%s", (fid,)), ("chat_thread_files", "SELECT 1 FROM chat_thread_files WHERE thread_id=%s", (tid,))]
    p = tmp / sha[:2] / sha; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b"abc")
    hk = (await A.q("INSERT INTO agent_hooks(agent_id, name, token_hash) VALUES(%s,'h',%s) RETURNING id", (aid, "th" + sfx), one=True))["id"]
    await A.q("INSERT INTO hook_calls(hook_id, thread_id) VALUES(%s,%s)", (hk, tid)); await A.q("INSERT INTO hook_log(agent_id, kind, name) VALUES(%s,'k','n')", (aid,))
    ck += [("hook_calls", "SELECT 1 FROM hook_calls WHERE hook_id=%s", (hk,))]
    await A.q("INSERT INTO approvals(agent_id, thread_id) VALUES(%s,%s)", (aid, tid))
    await A.q("INSERT INTO lg_crons(cron_id, assistant_id, schedule, workspace_id) VALUES(%s,%s,'* * * * *',%s)", (uuid.uuid4(), asst, ws)); ck.append(("lg_crons", "SELECT 1 FROM lg_crons WHERE assistant_id=%s", (asst,)))
    await A.q("INSERT INTO agent_crons(agent_id, name, cron, message) VALUES(%s,'c','* * * * *','m')", (aid,))
    dp = (await A.q("INSERT INTO deployments(slug, agent_id, version, key_hash, key_prefix) VALUES(%s,%s,1,'h','p') RETURNING id", ("t34-" + sfx, aid), one=True))["id"]
    await A.q("INSERT INTO deployment_history(deployment_id, version, action) VALUES(%s,1,'deploy')", (dp,)); ck.append(("deployment_history", "SELECT 1 FROM deployment_history WHERE deployment_id=%s", (dp,)))
    await A.q("INSERT INTO agent_shares(workspace_id, agent_id, principal_kind, principal_id, role) VALUES(%s,%s,'user',%s,'use')", (ws, aid, other_user))
    b = (await A.q("INSERT INTO channel_bindings(workspace_id, agent_id, kind, name) VALUES(%s,%s,'fake','b') RETURNING id", (ws, aid), one=True))["id"]
    await A.q("INSERT INTO channel_threads(workspace_id, binding_id, chat_key, thread_id) VALUES(%s,%s,'k',%s)", (ws, b, tid)); ck.append(("channel_threads", "SELECT 1 FROM channel_threads WHERE binding_id=%s", (b,)))
    ms = (await A.q("INSERT INTO mcp_servers(name, url, agent_id, workspace_id) VALUES(%s,'https://example.test/mcp',%s,%s) RETURNING id", ("m" + sfx, aid, ws), one=True))["id"]
    await A.q("INSERT INTO tool_settings(workspace_id, server, tool, enabled) VALUES(%s,%s,'t',true)", (ws, "m" + sfx)); ck.append(("tool_settings", "SELECT 1 FROM tool_settings WHERE server=%s", ("m" + sfx,)))
    await A.q("INSERT INTO oauth_providers(name, authorize_url, token_url, api_base, client_id, workspace_id, revoke_url) VALUES(%s,'https://x.test/a','https://x.test/t','https://x.test','cid',%s,NULL)", ("p" + sfx, ws))
    prov = await A.q("SELECT * FROM oauth_providers WHERE workspace_id=%s AND name=%s", (ws, "p" + sfx), one=True)
    await connections.save_tokens(("agent", aid), prov, {"access_token": "AT-" + sfx, "refresh_token": "RT-" + sfx})
    await connections.save_tokens(("user", other_user), prov, {"access_token": "UAT-" + sfx})      # the same provider, a USER's connection: must survive
    ck.append(("oauth_connections(agent)", "SELECT 1 FROM oauth_connections WHERE owner_type='agent' AND owner_id=%s", (aid,)))
    pid = (await A.q("SELECT min(id) i FROM projects", one=True))["i"]
    await A.q("INSERT INTO eval_runs(agent_id, workspace_id) VALUES(%s,%s)", (aid, ws)); tset = (await A.q("INSERT INTO test_sets(name, workspace_id) VALUES(%s,%s) RETURNING id", ("t34-" + sfx, ws), one=True))["id"]; await A.q("INSERT INTO test_runs(agent_id, set_id) VALUES(%s,%s)", (aid, tset)); await A.q("INSERT INTO simulations(agent_id) VALUES(%s)", (aid,))
    await A.q("INSERT INTO issues(project_id, kind, signature, title, agent_id, workspace_id) VALUES(%s,'error_cluster',%s,'t',%s,%s)", (pid, "s" + sfx, aid, ws))
    await A.q("INSERT INTO a2a_tasks(id, agent_id, workspace_id) VALUES(%s,%s,%s)", ("t34-" + sfx, aid, ws)); await A.q("INSERT INTO agent_identity(agent_id, workspace_id) VALUES(%s,%s)", (aid, ws))
    sv = (await A.q("INSERT INTO users(username, pw_hash, service_agent_id) VALUES(%s,'x',%s) RETURNING id", ("svc.t34." + sfx, aid), one=True))["id"]
    await A.q("INSERT INTO memberships(user_id, workspace_id, role) VALUES(%s,%s,'viewer')", (sv, ws)); await A.q("INSERT INTO user_api_keys(user_id, prefix, key_hash) VALUES(%s,'aos_t34',%s)", (sv, "kh" + sfx))
    ck += [("users(service)", "SELECT 1 FROM users WHERE service_agent_id=%s", (aid,)), ("user_api_keys(service)", "SELECT 1 FROM user_api_keys WHERE user_id=%s", (sv,))]
    dm = (await A.q("INSERT INTO users(username, pw_hash, demo_agents) VALUES(%s,'x',%s) RETURNING id", ("dm.t34." + sfx, [aid, 999999999]), one=True))["id"]
    ck.append(("users.demo_agents", "SELECT 1 FROM users WHERE %s = ANY(demo_agents)", (aid,)))
    for pre in (f"agent.{aid}.u{other_user}.memories", f"agent.{aid}.memories"):
        await A.q("INSERT INTO store(prefix, key, value) VALUES(%s,'k','{}')", (pre,))
    ck.append(("store", "SELECT 1 FROM store WHERE prefix = %s OR prefix LIKE %s", (f"agent.{aid}", f"agent.{aid}.%")))
    await A.q("INSERT INTO sandboxes(id, name, workspace_id, owner_id, kind, agent_id) VALUES(%s,'s',%s,%s,'files',%s)", ("t34s" + sfx, ws, other_user, aid))
    await A.q("INSERT INTO gateway_usage(agent_id, model, workspace_id) VALUES(%s,'m',%s)", (aid, ws))      # KEPT
    await A.q("INSERT INTO oauth_audit(action, agent_id, workspace_id) VALUES('x',%s,%s)", (aid, ws))      # KEPT
    return {"ck": ck, "tid": tid, "tid2": tid2, "fid": fid, "sha": sha, "blob": p, "user_conn": ("SELECT 1 FROM oauth_connections WHERE owner_type='user' AND owner_id=%s AND workspace_id=%s", (other_user, ws)),
            "users": [sv, dm], "mcp": ms, "asst": asst}

async def alive(s): return {lbl: await n(sql, a) for lbl, sql, a in s["ck"]}

async def drop_ws(*wss, users=()):
    """Purge every agent left in the throw-away workspaces through the real pipeline, then remove the workspaces and test users."""
    for ws in wss:
        for a in await A.q("SELECT id FROM agents WHERE workspace_id=%s", (ws,)):
            try: await P.purge_agent(a["id"], ws, ADM(ws))
            except Exception: pass  # noqa: BLE001
        await A.q("DELETE FROM agent_delete_exports WHERE workspace_id=%s", (ws,))
        for t in ("gateway_usage", "oauth_audit", "agent_audit", "sandboxes"): await A.q(f"DELETE FROM {t} WHERE workspace_id=%s", (ws,))      # the KEPT rows of the throw-away workspaces
        for t in ("oauth_connections", "connections", "oauth_providers", "lg_threads", "tool_settings", "test_sets"): await A.q(f"DELETE FROM {t} WHERE workspace_id=%s", (ws,))
        await A.q("DELETE FROM workspaces WHERE id=%s", (ws,))
    await A.q("DELETE FROM users WHERE username LIKE 'dm.t34.%%' OR username LIKE 'svc.t34.%%'")
    for u in users: await A.q("DELETE FROM users WHERE id=%s", (u,))

async def delete_as(user, aid, **kw):
    tok = admin.USER.set(user)
    try: return await P.delete_agent(aid, **kw)
    finally: admin.USER.reset(tok)

async def refused(user, aid, code, **kw):
    with pytest.raises(HTTPException) as e: await delete_as(user, aid, **kw)
    assert e.value.status_code == code, (e.value.status_code, e.value.detail, kw)

def test_agent_purge_removes_every_row_and_spares_the_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(AT, "DIR", tmp_path)
    async def go():
        w1, w2 = await mk_ws(), await mk_ws(); aid = 0
        try:
            sha = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
            aid, name = await mk_agent(w1, owner=UID, ident="t34-ident-" + uuid.uuid4().hex[:5]); dec, dname = await mk_agent(w1, owner=UID); oth, oname = await mk_agent(w2, owner=UID)
            s, sd, so = await seed(aid, w1, 9901, sha, tmp_path), await seed(dec, w1, 9901, sha, tmp_path), await seed(oth, w2, 9902, sha, tmp_path)
            await A.q("INSERT INTO store(prefix, key, value) VALUES(%s,'k','{}')", (f"agent.{aid}0.memories",))      # a different agent whose id merely starts with ours
            before = await alive(s); assert all(v >= 1 for v in before.values()), before
            uc = await n(*s["user_conn"])
            owner_u = U34("editor", UID, w1)                    # the OWNER (a plain editor) deletes; the chats belong to another user, private under RLS
            await refused(owner_u, aid, 400, mode="permanent", confirm=name + " ")       # exact name only
            assert await n("SELECT 1 FROM agents WHERE id=%s", (aid,)) == 1 and all(v >= 1 for v in (await alive(s)).values())
            res = await delete_as(owner_u, aid, mode="permanent", confirm=name)
            assert res["ok"] and res["purged"] and res["export_id"] and res["after"]["hook:computer"] is not None
            after = await alive(s); assert not any(after.values()), {k: v for k, v in after.items() if v}
            # the generic proof of the card: no table with an agent_id column mentions the agent any more (kept tables aside)
            for t in [r["table_name"] for r in await A.q("SELECT DISTINCT table_name FROM information_schema.columns WHERE table_schema='public' AND column_name='agent_id'")]:
                c = await n(f"SELECT 1 FROM {t} WHERE agent_id=%s", (aid,))
                assert (c >= 1) if t in P.KEPT else (c == 0), (t, c)
            assert await n("SELECT 1 FROM gateway_usage WHERE agent_id=%s", (aid,)) == 1      # costs stay in the reports
            assert await n("SELECT 1 FROM agent_audit WHERE agent_id=%s AND action='agent_purged'", (aid,)) == 1
            assert s["blob"].exists(), "other file rows still use this blob"
            # untouched: the other agent of the same workspace, the agent of the other workspace, the user's own connection to the same provider, the look-alike store prefix
            for other in (sd, so): assert all(v >= 1 for v in (await alive(other)).values()), await alive(other)
            assert await n(*s["user_conn"]) == uc and await n("SELECT 1 FROM store WHERE prefix=%s", (f"agent.{aid}0.memories",)) == 1
            assert (await A.q("SELECT count(*) c FROM agent_delete_exports WHERE agent_id=%s", (aid,), one=True))["c"] == 1
            await delete_as(ADM(w1), dec, mode="permanent", confirm=dname)
            assert not any((await alive(sd)).values()) and s["blob"].exists() and all(v >= 1 for v in (await alive(so)).values())     # w2 still uses the blob
            await delete_as(ADM(w2), oth, mode="permanent", confirm=oname)
            assert not any((await alive(so)).values()) and not s["blob"].exists()           # last user of the blob gone -> removed from the disk
        finally:
            await A.q("DELETE FROM store WHERE prefix=%s", (f"agent.{aid}0.memories",)); await drop_ws(w1, w2)
    run34(go)

def test_agent_purge_blob_removed_when_unshared(tmp_path, monkeypatch):
    monkeypatch.setattr(AT, "DIR", tmp_path)
    async def go():
        w = await mk_ws()
        try:
            sha = hashlib.sha256(uuid.uuid4().bytes).hexdigest(); aid, name = await mk_agent(w, owner=UID); s = await seed(aid, w, 9901, sha, tmp_path)
            assert s["blob"].exists()
            await delete_as(ADM(w), aid, mode="permanent", confirm=name)
            assert not s["blob"].exists()
        finally: await drop_ws(w)
    run34(go)

def test_agent_purge_covers_every_agent_id_table():
    """A new table with an agent_id column must be classified: purged by agent_purge or kept on purpose (costs / audit)."""
    async def go():
        tables = {r["table_name"] for r in await A.q("SELECT DISTINCT table_name FROM information_schema.columns WHERE table_schema='public' AND column_name='agent_id'")}
        unknown = tables - set(P.PURGED) - set(P.KEPT)
        assert not unknown, f"classify in agent_purge.PURGED / KEPT: {sorted(unknown)}"
        assert not (set(P.PURGED) & set(P.KEPT))
    run34(go)

def test_agent_purge_refused_cases():
    async def go():
        w1, w2, wd = await mk_ws(), await mk_ws(), await mk_ws(demo=True)
        A.gw.DEMO_WS.add(wd)
        try:
            owner, other = UID + 10, UID + 11
            priv, pname = await mk_agent(w1, owner=owner, visibility="private"); wsa, wname = await mk_agent(w1, owner=owner); hid, hname = await mk_agent(w1, owner=owner, hidden=True)
            foreign, fname = await mk_agent(w2, owner=owner); dag, dname = await mk_agent(wd, owner=owner)
            for uid, role in ((other, "use"), (other + 1, "edit")):
                await A.q("INSERT INTO agent_shares(workspace_id, agent_id, principal_kind, principal_id, role) VALUES(%s,%s,'user',%s,%s)", (w1, priv, uid, role))
            async def intact(*ids):
                for i in ids: assert await n("SELECT 1 FROM agents WHERE id=%s AND NOT deleted", (i,)) == 1, i
            # viewer: never, in either mode
            for mode, extra in (("archive", {}), ("permanent", {"confirm": pname})):
                await refused(U34("viewer", owner, w1), priv, 403, mode=mode, **extra)
            # shared with 'use' only: cannot archive or delete; shared with 'edit': may archive, but never delete permanently (not owner / admin)
            await refused(U34("editor", other, w1), priv, 403, mode="archive"); await refused(U34("editor", other, w1), priv, 403, mode="permanent", confirm=pname)
            await refused(U34("editor", other + 1, w1), priv, 403, mode="permanent", confirm=pname)
            # a workspace editor who is not the owner: permanent refused
            await refused(U34("editor", other + 2, w1), wsa, 403, mode="permanent", confirm=wname)
            # an unshared user does not even see a private agent
            await refused(U34("editor", other + 2, w1), priv, 404, mode="permanent", confirm=pname)
            # another workspace: the agent does not exist for them, in every mode, even for their admin
            for mode in ("archive", "permanent"): await refused(ADM(w2), wsa, 404, mode=mode, confirm=wname)
            await refused(ADM(w1), foreign, 404, mode="permanent", confirm=fname)
            # demo account, admin inside a demo workspace, external channel, API key with a lowered role is just the role: all refused
            await refused(U34("admin", owner, w1, demo=True), wsa, 403, mode="permanent", confirm=wname)
            await refused(ADM(wd), dag, 403, mode="permanent", confirm=dname); await refused(ADM(wd), dag, 403, mode="archive")
            await refused(U34("admin", owner, w1, auth="channel"), wsa, 403, mode="permanent", confirm=wname)
            await refused(None, wsa, 401, mode="permanent", confirm=wname)
            # system (hidden) agent; unknown mode; wrong confirm; missing confirm
            await refused(ADM(w1), hid, 403, mode="permanent", confirm=hname)
            await refused(ADM(w1), wsa, 422, mode="wipe")
            await refused(ADM(w1), wsa, 400, mode="permanent", confirm="wrong"); await refused(ADM(w1), wsa, 400, mode="permanent")
            await refused(ADM(w1), wsa, 400, purge=True)           # the old test flag is the same road: confirm still required
            await intact(priv, wsa, hid, foreign, dag)
            priv2, p2name = await mk_agent(w1, owner=owner, visibility="private")        # 'edit' share may archive (today's rule), 'use' share may not
            for uid, role in ((other, "use"), (other + 1, "edit")):
                await A.q("INSERT INTO agent_shares(workspace_id, agent_id, principal_kind, principal_id, role) VALUES(%s,%s,'user',%s,%s)", (w1, priv2, uid, role))
            await refused(U34("editor", other, w1), priv2, 403, mode="archive")
            assert (await delete_as(U34("editor", other + 1, w1), priv2, mode="archive"))["archived"]
            await refused(U34("editor", other + 1, w1), priv2, 403, mode="permanent", confirm=p2name)
            assert await n("SELECT 1 FROM agent_delete_exports WHERE workspace_id = ANY(%s)", ([w1, w2, wd],)) == 0
        finally:
            A.gw.DEMO_WS.discard(wd); await A.q("DELETE FROM agent_shares WHERE workspace_id=%s", (w1,)); await drop_ws(w1, w2, wd)
    run34(go)

def test_agent_purge_owner_admin_allowed_and_idempotent():
    async def go():
        w1, w2 = await mk_ws(), await mk_ws()
        try:
            owner, stranger = UID + 20, UID + 21
            a1, n1 = await mk_agent(w1, owner=owner)                       # the owner (plain editor) may delete his own agent
            r = await delete_as(U34("editor", owner, w1), a1, mode="permanent", confirm=n1); assert r["purged"] and not r.get("already")
            a2, n2 = await mk_agent(w1, owner=owner, visibility="private")   # a workspace admin may delete a private agent nobody shared with him
            r2 = await delete_as(ADM(w1), a2, mode="permanent", confirm=n2); assert r2["purged"]
            # archived agents can be deleted for good
            a3, n3 = await mk_agent(w1, owner=owner, deleted=True)
            assert (await delete_as(ADM(w1), a3, mode="permanent", confirm=n3))["purged"]
            # repeat -> 200 'already' (owner or admin, right name); everybody else still refused; ids that never existed / other workspace -> 404
            r3 = await delete_as(U34("editor", owner, w1), a1, mode="permanent", confirm=n1); assert r3["already"] and r3["export_id"] == r["export_id"]
            assert (await delete_as(ADM(w1), a2, mode="permanent", confirm=n2))["already"]
            await refused(U34("editor", stranger, w1), a1, 403, mode="permanent", confirm=n1)
            await refused(ADM(w1), a1, 400, mode="permanent", confirm="nope")
            await refused(U34("viewer", owner, w1), a1, 403, mode="permanent", confirm=n1)
            await refused(ADM(w2), a1, 404, mode="permanent", confirm=n1)                # other workspace learns nothing
            await refused(ADM(w1), 2_000_000_000, 404, mode="permanent", confirm="x")
            assert await n("SELECT 1 FROM agent_audit WHERE agent_id=%s AND action='agent_purged'", (a1,)) == 1      # audited once
        finally: await drop_ws(w1, w2)
    run34(go)

def test_agent_purge_archive_and_restore(tmp_path):
    async def go():
        w1, w2 = await mk_ws(), await mk_ws()
        try:
            owner = UID + 30
            aid, name = await mk_agent(w1, owner=owner); other, _ = await mk_agent(w1, owner=owner)
            tid = (await seed(aid, w1, 9903, hashlib.sha256(b"x" + uuid.uuid4().bytes).hexdigest(), tmp_path))["tid"]
            ed = U34("editor", owner, w1)
            assert (await delete_as(ed, aid))["archived"]                                  # default mode = archive
            assert await n("SELECT 1 FROM agents WHERE id=%s AND deleted", (aid,)) == 1 and await n("SELECT 1 FROM lg_threads WHERE thread_id=%s", (tid,)) == 1   # nothing removed
            assert aid not in [a["id"] for a in await as_(ed, A.list_agents)]
            with pytest.raises(HTTPException) as e: await as_(ed, A.get_agent, aid)
            assert e.value.status_code == 404
            assert aid in [a["id"] for a in await as_(ed, P.archived_agents)]
            assert (await delete_as(ed, aid))["archived"]                                  # archive twice = fine
            # only people who may edit can restore; viewer / other workspace cannot
            for u, code in ((U34("viewer", owner, w1), 403), (ADM(w2), 404), (U34("admin", owner, w1, demo=True), 403)):
                with pytest.raises(HTTPException) as e: await as_(u, P.restore_agent, aid)
                assert e.value.status_code == code
            assert aid not in [a["id"] for a in await as_(ADM(w2), P.archived_agents)]
            assert (await as_(ed, P.restore_agent, aid))["restored"]
            assert aid in [a["id"] for a in await as_(ed, A.list_agents)] and (await as_(ed, A.get_agent, aid))["agent"]["id"] == aid
            assert other in [a["id"] for a in await as_(ed, A.list_agents)]
            assert await n("SELECT 1 FROM agent_audit WHERE agent_id=%s AND action IN ('agent_archived','agent_restored')", (aid,)) == 2
        finally: await drop_ws(w1, w2)
    run34(go)

def test_agent_purge_export_kept_for_admin_30_days():
    async def go():
        w1, w2 = await mk_ws(), await mk_ws()
        try:
            owner = UID + 40
            aid, name = await mk_agent(w1, owner=owner, ident="id-" + uuid.uuid4().hex[:4])
            r = await delete_as(ADM(w1), aid, mode="permanent", confirm=name); eid = r["export_id"]
            lst = await as_(ADM(w1), P.exports_list); assert eid in [x["id"] for x in lst]
            got = await as_(ADM(w1), P.export_get, eid)
            assert got["name"] == name and got["format"] == "moza-agent/1" and got["config"]["name"] == name and got["meta"]["owner_id"] == owner
            days = (await A.q("SELECT extract(epoch FROM expires_at - created_at)/86400 d FROM agent_delete_exports WHERE id=%s", (eid,), one=True))["d"]; assert round(float(days)) == 30
            for u in (U34("editor", owner, w1), U34("viewer", owner, w1), U34("admin", owner, w1, demo=True)):    # not an admin of this workspace
                with pytest.raises(HTTPException) as e: await as_(u, P.export_get, eid)
                assert e.value.status_code == 403
            with pytest.raises(HTTPException) as e: await as_(ADM(w2), P.export_get, eid)     # other workspace's admin: RLS -> 404
            assert e.value.status_code == 404
            assert eid not in [x["id"] for x in await as_(ADM(w2), P.exports_list)]
            await A.q("UPDATE agent_delete_exports SET expires_at = now() - interval '1 minute' WHERE id=%s", (eid,))
            with pytest.raises(HTTPException) as e: await as_(ADM(w1), P.export_get, eid)       # expired: gone even before the worker runs
            assert e.value.status_code == 404
            await P.tick(); assert await n("SELECT 1 FROM agent_delete_exports WHERE id=%s", (eid,)) == 0
        finally: await drop_ws(w1, w2)
    run34(go)

def test_agent_purge_registry_hooks_and_retry(monkeypatch):
    """register_purge: later cards add their cleanup; one failing hook neither undoes the delete nor stops the others; a failed step is retried."""
    async def go():
        w = await mk_ws()
        try:
            orig = P.HOOKS; monkeypatch.setitem(P.HOOKS, "computer", lambda aid, ws, identifier=None: _async({"boxes": 0, "errors": []}))
            seen, flaky = [], {"n": 0}
            async def good(agent_id, workspace_id, *, identifier=None): seen.append((agent_id, workspace_id, identifier)); return {"rows": 2}
            async def old_style(agent_id, workspace_id): seen.append(("old", agent_id))
            async def bad(agent_id, workspace_id):
                flaky["n"] += 1
                if flaky["n"] < 3: raise RuntimeError("provider down")
                return "ok"
            async def soft_fail(agent_id, workspace_id): return {"errors": [{"id": "box1", "error": "busy"}]}
            for nm, fn in (("t34-good", good), ("t34-old", old_style), ("t34-bad", bad), ("t34-soft", soft_fail)): P.register_purge(nm, fn)
            P.register_purge("t34-good", good)        # same name = replace, never a second run
            with pytest.raises(ValueError): P.register_purge("", good)
            aid, name = await mk_agent(w, owner=UID, ident="ident-34"); await A.q("INSERT INTO agent_crons(agent_id, name, cron, message) VALUES(%s,'c','* * * * *','m')", (aid,))
            r = await delete_as(ADM(w), aid, mode="permanent", confirm=name)
            assert await n("SELECT 1 FROM agents WHERE id=%s", (aid,)) == 0 and await n("SELECT 1 FROM agent_crons WHERE agent_id=%s", (aid,)) == 0     # the delete stands
            assert seen.count((aid, w, "ident-34")) == 1 and ("old", aid) in seen                       # identifier passed only to hooks that accept it
            assert r["after"]["hook:t34-good"] == {"rows": 2} and r["after"]["hook:t34-bad"].startswith("queued") and r["after"]["hook:t34-soft"].startswith("queued")
            assert sorted(q["name"] for q in P.QUEUE) == ["hook:t34-bad", "hook:t34-soft"]
            assert await P.retry_queue(aid) == 0 and len(P.QUEUE) == 2 and flaky["n"] == 2              # retry #1: bad fails again, soft still fails
            P.QUEUE[:] = [q for q in P.QUEUE if q["name"] == "hook:t34-bad"]
            assert await P.retry_queue(aid) == 1 and P.QUEUE == []                                      # third attempt succeeds, leaves the queue
            # repeating the delete re-runs pending steps of THAT agent only
            P.QUEUE.append({"name": "x", "agent_id": aid + 1, "ws": w, "tries": 1, "next_at": 0, "error": "", "run": lambda: _async("ok")})
            assert (await delete_as(ADM(w), aid, mode="permanent", confirm=name))["already"] and len(P.QUEUE) == 1
            # a step that never works is dropped after MAX_TRIES (logged), not kept forever
            P.QUEUE[:] = [{"name": "never", "agent_id": 1, "ws": w, "tries": 1, "next_at": 0, "error": "", "run": lambda: _raise()}]
            for _ in range(P.MAX_TRIES + 1):
                for q in P.QUEUE: q["next_at"] = 0
                await P.retry_queue()
            assert P.QUEUE == []
            # nothing registered -> nothing breaks
            monkeypatch.setattr(P, "HOOKS", {})
            a2, n2 = await mk_agent(w, owner=UID); assert (await delete_as(ADM(w), a2, mode="permanent", confirm=n2))["after"] == {}
        finally:
            for nm in ("t34-good", "t34-old", "t34-bad", "t34-soft"): orig.pop(nm, None)
            P.QUEUE.clear(); await drop_ws(w)
    run34(go)

async def _async(v): return v
async def _raise(): raise RuntimeError("still down")

class _Rev(BaseHTTPRequestHandler):
    calls: list = []
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0)).decode(); type(self).calls.append(body); self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass

def test_agent_purge_token_revoke_goes_through_netguard(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _Rev); _Rev.calls = []; threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/revoke"
    async def go():
        w = await mk_ws()
        try:
            monkeypatch.setitem(P.HOOKS, "computer", lambda aid, ws, identifier=None: _async({}))
            async def mk(rurl):
                aid, name = await mk_agent(w, owner=UID); sfx = uuid.uuid4().hex[:6]
                await A.q("INSERT INTO oauth_providers(name, authorize_url, token_url, api_base, client_id, workspace_id, revoke_url) VALUES(%s,'https://x.test/a','https://x.test/t','https://x.test','cid',%s,%s)", ("p" + sfx, w, rurl))
                prov = await A.q("SELECT * FROM oauth_providers WHERE workspace_id=%s AND name=%s", (w, "p" + sfx), one=True)
                await connections.save_tokens(("agent", aid), prov, {"access_token": "AT-" + sfx, "refresh_token": "RT-" + sfx})
                return aid, name, sfx
            # 1) internal address NOT allow-listed: the call never leaves; the delete still completes; the token rows are gone
            monkeypatch.delenv("AOS_EGRESS_ALLOW", raising=False)
            aid, name, sfx = await mk(url); r = await delete_as(ADM(w), aid, mode="permanent", confirm=name)
            assert _Rev.calls == [] and r["purged"] and any(v.startswith("failed") for k, v in r["after"].items() if k.startswith("revoke:"))
            assert await n("SELECT 1 FROM oauth_connections WHERE owner_type='agent' AND owner_id=%s", (aid,)) == 0
            # 2) allow-listed (operator choice): both tokens are revoked at the provider; nothing but the revoke goes out
            monkeypatch.setenv("AOS_EGRESS_ALLOW", "127.0.0.0/8")
            aid, name, sfx = await mk(url); r = await delete_as(ADM(w), aid, mode="permanent", confirm=name)
            assert len(_Rev.calls) == 2 and any("RT-" + sfx in c for c in _Rev.calls) and any("AT-" + sfx in c for c in _Rev.calls)
            assert all(v == "ok" for k, v in r["after"].items() if k.startswith("revoke:"))
            assert "AT-" not in str(r) and "RT-" not in str(r)             # no token in the response
            # 3) link-local metadata address is unreachable even when everything internal is allowed
            _Rev.calls.clear(); aid, name, sfx = await mk("http://169.254.169.254/revoke"); r = await delete_as(ADM(w), aid, mode="permanent", confirm=name)
            assert r["purged"] and any(v.startswith("failed") for k, v in r["after"].items() if k.startswith("revoke:"))
        finally: srv.shutdown(); await drop_ws(w)
    run34(go)

def test_agent_purge_is_one_transaction(tmp_path):
    """A failure in the middle of the DB part rolls everything back: agent, chats and the export row all still there; the next try works."""
    async def go():
        w = await mk_ws()
        try:
            aid, name = await mk_agent(w, owner=UID); s = await seed(aid, w, 9904, hashlib.sha256(uuid.uuid4().bytes).hexdigest(), tmp_path)
            await A.q("CREATE OR REPLACE FUNCTION t34_block() RETURNS trigger LANGUAGE plpgsql AS $f$ BEGIN RAISE EXCEPTION 't34 boom'; END $f$")
            await A.q(f"CREATE TRIGGER t34_trg BEFORE DELETE ON agent_identity FOR EACH ROW WHEN (OLD.agent_id = {int(aid)}) EXECUTE FUNCTION t34_block()")
            try:
                with pytest.raises(Exception) as e: await delete_as(ADM(w), aid, mode="permanent", confirm=name)
                assert "boom" in str(e.value)
                assert all(v >= 1 for v in (await alive(s)).values()), await alive(s)
                assert await n("SELECT 1 FROM agents WHERE id=%s", (aid,)) == 1 and await n("SELECT 1 FROM agent_delete_exports WHERE agent_id=%s", (aid,)) == 0
                assert await n("SELECT 1 FROM agent_audit WHERE agent_id=%s AND action='agent_purged'", (aid,)) == 0
            finally:
                await A.q("DROP TRIGGER IF EXISTS t34_trg ON agent_identity"); await A.q("DROP FUNCTION IF EXISTS t34_block()")
            assert (await delete_as(ADM(w), aid, mode="permanent", confirm=name))["purged"] and not any((await alive(s)).values())
        finally: await drop_ws(w)
    run34(go)

def test_agent_purge_channels_stop_and_unlink(monkeypatch):
    async def go():
        w = await mk_ws()
        try:
            monkeypatch.setitem(P.HOOKS, "computer", lambda aid, ws, identifier=None: _async({}))
            gone, mode = [], {"fail": True}
            class Zz(channels.Adapter):
                kind = "zz34"
                async def after_delete(self, row):
                    if mode["fail"]: raise RuntimeError("webhook host down")
                    gone.append(row["id"])
            monkeypatch.setitem(channels.ADAPTERS, "zz34", Zz())
            aid, name = await mk_agent(w, owner=UID)
            b = (await A.q("INSERT INTO channel_bindings(workspace_id, agent_id, kind, name) VALUES(%s,%s,'zz34','b') RETURNING id", (w, aid), one=True))["id"]
            r = await delete_as(ADM(w), aid, mode="permanent", confirm=name)
            assert await n("SELECT 1 FROM channel_bindings WHERE id=%s", (b,)) == 0      # an incoming message finds no binding -> nobody answers
            assert r["after"][f"channel:zz34:{b}"].startswith("queued")
            mode["fail"] = False; assert await P.retry_queue(aid) == 1 and gone == [b]
        finally: await drop_ws(w)
    run34(go)

def test_agent_purge_service_user_and_mcp_state(monkeypatch, tmp_path):
    async def go():
        w = await mk_ws()
        try:
            monkeypatch.setitem(P.HOOKS, "computer", lambda aid, ws, identifier=None: _async({}))
            aid, name = await mk_agent(w, owner=UID); s = await seed(aid, w, 9905, hashlib.sha256(uuid.uuid4().bytes).hexdigest(), tmp_path)
            mcp_registry._LIVE[s["mcp"]] = {"ws": w, "agent_id": aid, "name": "x", "prefix": False, "conn": False, "tools": {}, "status": "ok"}
            await delete_as(ADM(w), aid, mode="permanent", confirm=name)
            assert s["mcp"] not in mcp_registry._LIVE                                              # live tools of the agent's private MCP server are dropped
            assert await n("SELECT 1 FROM users WHERE id = ANY(%s)", (s["users"][:1],)) == 0 and await n("SELECT 1 FROM user_api_keys WHERE user_id = ANY(%s)", (s["users"][:1],)) == 0
            assert await n("SELECT 1 FROM users WHERE id = %s", (s["users"][1],)) == 1             # an unrelated user only loses the agent id from demo_agents
            assert (await A.q("SELECT demo_agents d FROM users WHERE id=%s", (s["users"][1],), one=True))["d"] == [999999999]
        finally: await drop_ws(w)
    run34(go)

# ---------------------------------------------------------------- 11.34 over HTTP (shared fake server; optional own server via AOS_TEST_BASE_PURGE)
BP = os.environ.get("AOS_TEST_BASE_PURGE", B)   # default = the shared fake server (:8029 collides with the *_real probe servers)

def test_agent_purge_http_roles_and_flow():
    with httpx.Client(base_url=BP, auth=ADMIN, timeout=60) as a:
        base = a.get("/api/agents/1").json()["config"]; sfx = uuid.uuid4().hex[:6]; name = "t34-http-" + sfx
        aid = a.post("/api/agents", json={**base, "name": name}).json()["id"]; made = []
        def user(role, demo=False):
            u = f"t34{role[0]}{'d' if demo else ''}-{sfx}"; r = a.post("/api/admin/users", json={"username": u, "password": "t34-pass-" + sfx, "role": role, "demo": demo}); assert r.status_code == 200, r.text
            made.append(r.json()["id"]); return httpx.Client(base_url=BP, auth=(u, "t34-pass-" + sfx), timeout=60)
        try:
            with user("viewer") as v:                                          # viewer: neither archive nor delete nor restore nor the list
                assert v.delete(f"/api/agents/{aid}").status_code == 403
                assert v.delete(f"/api/agents/{aid}", params={"mode": "permanent", "confirm": name}).status_code == 403
                assert v.post(f"/api/agents/{aid}/restore").status_code == 403 and v.get("/api/archived-agents").status_code == 403
            with user("viewer", demo=True) as d:                               # demo account
                assert d.delete(f"/api/agents/{aid}", params={"mode": "permanent", "confirm": name}).status_code == 403
            with user("editor") as e:                                          # editor who is not the owner: never permanent, never the exports
                assert e.delete(f"/api/agents/{aid}", params={"mode": "permanent", "confirm": name}).status_code == 403
                assert e.get("/api/admin/agent-exports").status_code == 403
            assert a.delete(f"/api/agents/{aid}", params={"mode": "permanent", "confirm": name + "x"}).status_code == 400       # wrong name
            assert a.delete(f"/api/agents/{aid}", params={"purge": "true"}).status_code == 400                                    # legacy flag needs the name too
            assert a.delete(f"/api/agents/{aid}", params={"mode": "bogus"}).status_code == 422
            assert a.get(f"/api/agents/{aid}").status_code == 200
            assert a.delete(f"/api/agents/{aid}").json()["archived"] is True                                                        # default = archive
            assert a.get(f"/api/agents/{aid}").status_code == 404 and aid in [x["id"] for x in a.get("/api/archived-agents").json()]
            assert a.post(f"/api/agents/{aid}/restore").json()["restored"] is True and a.get(f"/api/agents/{aid}").status_code == 200
            r = a.delete(f"/api/agents/{aid}", params={"mode": "permanent", "confirm": name}); assert r.status_code == 200 and r.json()["purged"] is True, r.text
            assert a.get(f"/api/agents/{aid}").status_code == 404 and a.get(f"/api/agents/{aid}/export").status_code == 404
            r2 = a.delete(f"/api/agents/{aid}", params={"mode": "permanent", "confirm": name}); assert r2.status_code == 200 and r2.json()["already"] is True      # idempotent
            ex = a.get(f"/api/admin/agent-exports/{r.json()['export_id']}"); assert ex.status_code == 200 and ex.json()["config"]["name"] == name
            assert aid in [x["agent_id"] for x in a.get("/api/admin/agent-exports").json()]
        finally:
            for uid in made: a.delete(f"/api/admin/users/{uid}")


def test_chat_queue_max_setting():
    """F-1: chat.queue_max = settings row (default 5, clamped 1..20) that the chat reads from /api/chat/config."""
    import chat_settings as cs
    async def go():
        await cs._ensure()
        old = await A.q("SELECT v FROM aos_chat_settings WHERE k='queue_max'", one=True)
        try:
            await A.q("DELETE FROM aos_chat_settings WHERE k='queue_max'")
            assert await cs.queue_max() == 5
            await cs._put("queue_max", 3); assert await cs.queue_max() == 3
            await cs._put("queue_max", 99); assert await cs.queue_max() == 20
            await cs._put("queue_max", "x"); assert await cs.queue_max() == 5
        finally:
            await A.q("DELETE FROM aos_chat_settings WHERE k='queue_max'")
            if old: await cs._put("queue_max", old["v"])
    run(go)
