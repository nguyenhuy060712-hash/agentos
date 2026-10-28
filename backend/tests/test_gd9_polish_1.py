"""GĐ9.P1 (24/10/2026) polish group 1: per-agent spend caps, user groups for agent sharing, builder asks "what may the agent do", today's date in the
system prompt (workspace time zone), a closed tab does not cancel a run, honest sandbox runtime label, command.update / command.goto, plural "1 message".
Local server :8020 + fake LLM => $0. Throwaway workspace / users / agent; tests share state through the `env` fixture."""
import asyncio, base64, os, re, sys, time, uuid
from datetime import datetime
import httpx, psycopg, pytest
from test_fixes import B, ADMIN, a, thread, sse  # noqa: F401  (fixtures + helpers)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
PW = "phase0-gd9p1-local"; SUF = uuid.uuid4().hex[:6]
TZ = "Asia/Ho_Chi_Minh"

def pg(): return psycopg.connect(PG, autocommit=True)
def basic(u): return {"Authorization": "Basic " + base64.b64encode(f"{u}:{PW}".encode()).decode()}

def user(a, name, ws, role="editor"):
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": role, "workspace_id": ws}); assert r.status_code == 200, r.text
    return r.json()["id"], httpx.Client(base_url=B, auth=(name, PW), timeout=150)

@pytest.fixture(scope="module")
def env(a):
    ws = a.post("/api/admin/workspaces", json={"name": f"p1-w-{SUF}"}).json()["id"]
    e = {"ws": ws, "adm": httpx.Client(base_url=B, auth=ADMIN, headers={"X-Workspace-Id": str(ws)}, timeout=150)}
    for k, role in (("own", "editor"), ("mem", "editor"), ("vw", "viewer"), ("oth", "editor")):
        e[k + "_id"], e[k] = user(a, f"p1_{k}_{SUF}", ws, role); e[k + "_name"] = f"p1_{k}_{SUF}"
    r = e["own"].post("/api/agents", json={"name": f"Trợ lý nhóm {SUF}", "description": "Agent thử 9.P1", "system_prompt": "Bạn là trợ lý.", "tools": []}); assert r.status_code == 200, r.text
    e["agent"] = r.json()["id"]
    yield e
    for k in ("adm", "own", "mem", "vw", "oth"): e[k].close()
    with pg() as c:
        c.execute("DELETE FROM gateway_usage WHERE status='t9p1'")
        c.execute("DELETE FROM spend_caps WHERE workspace_id=%s", (ws,))
        c.execute("DELETE FROM lg_runs WHERE workspace_id=%s", (ws,)); c.execute("DELETE FROM lg_threads WHERE workspace_id=%s", (ws,))
        c.execute("DELETE FROM agents WHERE workspace_id=%s", (ws,))
        c.execute("DELETE FROM user_groups WHERE workspace_id=%s", (ws,))
        c.execute("DELETE FROM users WHERE username LIKE %s", (f"p1\\_%\\_{SUF}",))

def chat(c, aid, text):
    g = f"agent-{aid}"
    r = c.post("/api/v1/threads", json={"metadata": {"source": "chat", "graph_id": g}})
    if r.status_code != 200: return r
    return c.post(f"/api/v1/threads/{r.json()['thread_id']}/runs/wait", json={"assistant_id": g, "input": {"messages": [{"type": "human", "content": text}]}})

def usage(aid, cost=0.5, ts="now()", ws=None):
    with pg() as c:
        c.execute(f"INSERT INTO gateway_usage(ts,agent_id,model,input_tokens,output_tokens,cost_usd,status,workspace_id) VALUES({ts},%s,'gemini-3.8-flash',0,0,%s,'t9p1',%s)", (aid, cost, ws))

# ================================================================== 1. per-agent spend caps
def test_agent_scope_block_warn_and_other_agent_untouched(env):
    adm, aid = env["adm"], env["agent"]
    usage(aid, 0.5, ws=env["ws"])
    r = adm.post("/api/gateway/caps", json={"scope": "agent", "target": str(aid), "period": "month", "limit_usd": 0.4}); assert r.status_code == 200, r.text
    cap = r.json(); assert cap["scope"] == "agent" and cap["spent"] >= 0.5 and cap["reached"] is True
    r = adm.post("/api/gateway/caps/check", json={"agent_id": aid}); assert r.status_code == 402 and f"agent #{aid}" in r.json()["detail"], r.text
    assert adm.post("/api/gateway/caps/check", json={"agent_id": aid + 1}).status_code == 200      # another agent: not blocked
    assert adm.post("/api/gateway/caps/check", json={}).status_code == 200                           # no agent in ctx: not blocked
    assert adm.post("/api/gateway/caps", json={"scope": "agent", "target": "abc", "period": "day", "limit_usd": 1}).status_code == 422   # target must be an id
    adm.patch(f"/api/gateway/caps/{cap['id']}", json={"action": "warn"})
    r = adm.post("/api/gateway/caps/check", json={"agent_id": aid}); assert r.status_code == 200 and r.json()["warnings"], r.text
    adm.delete(f"/api/gateway/caps/{cap['id']}")
    assert adm.post("/api/gateway/caps/check", json={"agent_id": aid}).status_code == 200
    with pg() as c: c.execute("DELETE FROM gateway_usage WHERE status='t9p1'")

def test_agent_cap_periods_day_week_month(env):
    adm, aid = env["adm"], env["agent"]
    old = f"date_trunc('{{p}}', now() AT TIME ZONE '{TZ}') AT TIME ZONE '{TZ}' - interval '1 hour'"
    for p in ("day", "week", "month"):
        with pg() as c: c.execute("DELETE FROM gateway_usage WHERE status='t9p1'")
        usage(aid, 1.0, ts=old.format(p=p), ws=env["ws"])   # one hour before this period started: belongs to the previous period only
        cap = adm.post("/api/gateway/caps", json={"scope": "agent", "target": str(aid), "period": p, "limit_usd": 0.5}).json()
        try:
            assert cap["spent"] == 0 and cap["reached"] is False, (p, cap)
            usage(aid, 0.7, ws=env["ws"])
            now = [c for c in adm.get("/api/gateway/caps").json()["caps"] if c["id"] == cap["id"]][0]
            assert now["spent"] == 0.7 and now["reached"] is True, (p, now)
        finally: adm.delete(f"/api/gateway/caps/{cap['id']}")
    with pg() as c: c.execute("DELETE FROM gateway_usage WHERE status='t9p1'")

def test_agent_cap_card_api_and_rights(env):
    own, mem, aid = env["own"], env["mem"], env["agent"]
    r = own.put(f"/api/agents/{aid}/caps", json={"period": "day", "limit_usd": 0.01, "action": "block"}); assert r.status_code == 200, r.text
    own.put(f"/api/agents/{aid}/caps", json={"period": "week", "limit_usd": 2, "action": "warn"})
    d = own.get(f"/api/agents/{aid}/caps").json()
    assert d["can_edit"] is True and {(c["period"], c["limit_usd"], c["action"]) for c in d["caps"]} == {("day", 0.01, "block"), ("week", 2.0, "warn")}
    assert own.put(f"/api/agents/{aid}/caps", json={"period": "year", "limit_usd": 1}).status_code == 422
    own.put(f"/api/agents/{aid}/caps", json={"period": "day", "limit_usd": 0.02}); assert {c["limit_usd"] for c in own.get(f"/api/agents/{aid}/caps").json()["caps"] if c["period"] == "day"} == {0.02}   # replaced, not duplicated
    # the Gateway caps page (admin) lists the same rows with scope agent
    assert any(c["scope"] == "agent" and c["target"] == str(aid) for c in env["adm"].get("/api/gateway/caps").json()["caps"])
    own.put(f"/api/agents/{aid}/caps", json={"period": "day", "limit_usd": None}); own.put(f"/api/agents/{aid}/caps", json={"period": "week", "limit_usd": None})
    assert own.get(f"/api/agents/{aid}/caps").json()["caps"] == []                                    # null limit removes the cap

def test_run_blocked_by_agent_cap_with_402_detail(env):
    own, aid = env["own"], env["agent"]
    assert chat(own, aid, f"echo-user before cap {SUF}").status_code == 200
    usage(aid, 0.9, ws=env["ws"])
    own.put(f"/api/agents/{aid}/caps", json={"period": "month", "limit_usd": 0.5, "action": "block"})
    env["adm"].post("/api/gateway/caps/check", json={"agent_id": aid})   # refresh the snapshot
    time.sleep(0.2)
    r = chat(own, aid, "echo-user after cap")
    assert r.status_code == 402, r.text
    dt = r.json()["detail"]; assert dt["code"] == "spend_cap" and dt["scope"] == "agent" and dt["period"] == "month" and dt["target"] == str(aid), dt
    own.put(f"/api/agents/{aid}/caps", json={"period": "month", "action": "warn", "limit_usd": 0.5})   # warn only: the run goes through
    time.sleep(0.2); assert chat(own, aid, f"echo-user warn only {SUF}").status_code == 200
    own.put(f"/api/agents/{aid}/caps", json={"period": "month", "limit_usd": None})
    with pg() as c: c.execute("DELETE FROM gateway_usage WHERE status='t9p1'")

def test_budget_check_is_per_model_call_and_covers_sub_agents():
    """In-process: the gateway gate sees agent_id on EVERY model call (GatewayMiddleware builds ctx with its agent id; sub-agents get the same id)."""
    import gateway as gw
    gw._SNAP.update(t=time.time() + 1000, caps=[{"id": 1, "workspace_id": None, "scope": "agent", "target": "77", "period": "month", "limit_usd": 1.0, "spent": 1.5, "action": "block", "enabled": True}])
    try:
        with pytest.raises(gw.BudgetExceeded) as e: asyncio.run(gw.budget_check(gw.ctx_now("gemini", agent_id=77)))
        assert e.value.info["scope"] == "agent" and e.value.info["target"] == "77"
        assert asyncio.run(gw.budget_check(gw.ctx_now("gemini", agent_id=78))) == []
        assert asyncio.run(gw.budget_check(gw.ctx_now("gemini"))) == []
        import inspect
        assert "agent_id=self.agent_id" in inspect.getsource(gw.GatewayMiddleware.awrap_model_call)
        assert "agent_id=agent_id" in inspect.getsource(gw._generate) and "agent_id=agent_id" in inspect.getsource(gw.lc_call)
    finally:
        gw._SNAP.update(t=0.0, caps=[])

def test_legacy_daily_budget_still_enforced(env):
    own = env["own"]
    r = own.post("/api/agents", json={"name": f"Trợ lý ngân sách cũ {SUF}", "description": "x", "system_prompt": "Bạn là trợ lý.", "tools": [], "budget_usd_day": 0.01}); assert r.status_code == 200, r.text
    aid = r.json()["id"]
    assert own.get(f"/api/agents/{aid}/caps").json()["legacy_day_budget_usd"] == 0.01
    usage(aid, 0.5, ws=env["ws"])
    r = chat(own, aid, "echo-user legacy"); assert r.status_code == 402 and r.json()["detail"]["scope"] == "agent" and r.json()["detail"]["period"] == "day", r.text

# ================================================================== 2. user groups for agent sharing
def test_group_admin_crud(env):
    adm = env["adm"]
    r = adm.post("/api/admin/groups", json={"name": f"Kinh doanh {SUF}", "description": "Nhóm bán hàng", "members": [env["mem_id"], env["vw_id"]]}); assert r.status_code == 200, r.text
    g = r.json(); env["gid"] = g["id"]
    assert {m["id"] for m in g["members"]} == {env["mem_id"], env["vw_id"]} and g["shared_agents"] == 0
    assert adm.post("/api/admin/groups", json={"name": f"kinh doanh {SUF}"}).status_code == 409              # unique name per workspace (case-insensitive)
    assert adm.post("/api/admin/groups", json={"name": "x", "members": [987654]}).status_code == 404         # not a member of this workspace
    assert env["own"].post("/api/admin/groups", json={"name": "nope"}).status_code == 403                       # admins only
    assert [x["id"] for x in adm.get("/api/admin/groups").json()] == [g["id"]]
    r = adm.patch(f"/api/admin/groups/{g['id']}", json={"description": "đổi mô tả"}); assert r.json()["description"] == "đổi mô tả" and len(r.json()["members"]) == 2
    assert adm.patch(f"/api/admin/groups/{g['id']}", json={"members": [env["mem_id"]]}).json()["members"][0]["id"] == env["mem_id"]   # full replacement
    adm.patch(f"/api/admin/groups/{g['id']}", json={"members": [env["mem_id"], env["vw_id"]]})
    assert adm.get("/api/admin/groups/999999").status_code in (404, 405)

def test_group_share_use_private_agent_chat_and_rights(env):
    own, mem, vw, oth, aid, gid = env["own"], env["mem"], env["vw"], env["oth"], env["agent"], env["gid"]
    own.post(f"/api/agents/{aid}/shares", json={"visibility": "private"})
    for c in (mem, vw, oth): assert c.get(f"/api/agents/{aid}").status_code == 404                              # private, not shared yet
    d = own.post(f"/api/agents/{aid}/shares", json={"group_id": gid, "role": "use"}).json()
    sh = [s for s in d["shares"] if s["principal_kind"] == "group"]
    assert len(sh) == 1 and sh[0]["group_name"] == f"Kinh doanh {SUF}" and sh[0]["group_members"] == 2 and d["groups_supported"] is True
    assert [g["id"] for g in d["groups"]] == [gid]                                                              # the share pane can offer the group
    assert own.post(f"/api/agents/{aid}/shares", json={"group_id": 987654}).status_code == 404
    assert mem.get(f"/api/agents/{aid}").status_code == 200                                                    # RLS policy honours the group share
    assert aid in [x["id"] for x in mem.get("/api/agents").json()]
    r = chat(mem, aid, f"echo-user nhóm {SUF}"); assert r.status_code == 200 and f"nhóm {SUF}" in str(r.json()), r.text
    cfg = mem.get(f"/api/agents/{aid}").json()["config"]
    assert mem.put(f"/api/agents/{aid}", json=cfg).status_code == 403                                           # 'use' cannot edit
    assert oth.get(f"/api/agents/{aid}").status_code == 404                                                    # not in the group: still hidden
    # viewer chat rule (admin middleware has_share): a plain viewer can chat only because of the GROUP share
    assert chat(vw, aid, f"echo-user viewer {SUF}").status_code == 200
    assert chat(vw, 2, "echo-user other agent").status_code in (403, 404)                                       # but only that agent

def test_group_share_edit_and_priority(env):
    own, mem, aid, gid = env["own"], env["mem"], env["agent"], env["gid"]
    own.post(f"/api/agents/{aid}/shares", json={"group_id": gid, "role": "edit"})
    cfg = mem.get(f"/api/agents/{aid}").json()["config"]; cfg["description"] = "sửa bởi nhóm"
    assert mem.put(f"/api/agents/{aid}", json=cfg).status_code == 200                                           # group 'edit'
    own.post(f"/api/agents/{aid}/shares", json={"username": env["mem_name"], "role": "use"})                  # personal 'use' + group 'edit' -> edit wins
    assert mem.put(f"/api/agents/{aid}", json=cfg).status_code == 200
    own.post(f"/api/agents/{aid}/shares", json={"group_id": gid, "role": "use"})                               # back to 'use'
    assert mem.put(f"/api/agents/{aid}", json=cfg).status_code == 403

def test_group_share_mcp_and_a2a(env):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    aid = env["agent"]
    async def go(headers):
        async with httpx.AsyncClient(headers=headers, timeout=150) as hc, streamable_http_client(f"{B}/mcp/agents/{aid}", http_client=hc) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize(); return await s.list_tools()
    assert len(asyncio.run(go(basic(env["mem_name"]))).tools) == 1                                              # group member reaches the per-agent MCP
    assert httpx.get(f"{B}/a2a/{aid}/.well-known/agent-card.json", headers=basic(env["mem_name"])).status_code == 200   # and A2A
    assert httpx.get(f"{B}/a2a/{aid}/.well-known/agent-card.json", headers=basic(env["oth_name"])).status_code == 404
    r = httpx.post(f"{B}/mcp/agents/{aid}", headers={**basic(env["oth_name"]), "content-type": "application/json", "accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code in (403, 404)

def test_leaving_group_or_deleting_it_removes_access(env):
    adm, own, mem, vw, aid, gid = env["adm"], env["own"], env["mem"], env["vw"], env["agent"], env["gid"]
    for s in own.get(f"/api/agents/{aid}/shares").json()["shares"]:                                              # drop mem's personal share (added in the priority test)
        if s["principal_kind"] == "user": own.delete(f"/api/agents/{aid}/shares/{s['id']}")
    adm.patch(f"/api/admin/groups/{gid}", json={"members": [env["mem_id"]]})                                   # viewer leaves the group
    assert vw.get(f"/api/agents/{aid}").status_code == 404 and chat(vw, aid, "echo-user x").status_code in (403, 404)
    assert mem.get(f"/api/agents/{aid}").status_code == 200
    assert adm.get("/api/admin/groups").json()[0]["shared_agents"] == 1
    assert adm.delete(f"/api/admin/groups/{gid}").status_code == 200                                           # deleting the group removes its shares
    assert mem.get(f"/api/agents/{aid}").status_code == 404
    with pg() as c: assert c.execute("SELECT count(*) FROM agent_shares WHERE principal_kind='group' AND principal_id=%s", (gid,)).fetchone()[0] == 0
    assert adm.get("/api/admin/groups").json() == []

# ================================================================== 3. builder asks what the agent may do on the user's behalf
def converse(a, brief, lang=None, answers=("Khách hàng", "Dùng bảng giá", "Chỉ trả lời, không tự gửi gì", "Thân thiện")):
    msgs = [{"role": "user", "content": brief}]; asked = 0
    for i in range(8):
        r = a.post("/api/builder/chat", json={"messages": msgs, **({"lang": lang} if lang else {})}); assert r.status_code == 200, r.text
        j = r.json()
        if j["type"] == "draft": return j, asked, msgs
        asked += 1; msgs += [{"role": "assistant", "content": j["question"]}, {"role": "user", "content": answers[min(i, len(answers) - 1)]}]
    raise AssertionError("no draft")

def test_builder_always_asks_what_the_agent_may_do(a):
    j = a.post("/api/builder/chat", json={"messages": [{"role": "user", "content": "Trợ lý bán hàng cho khách hàng, dùng bảng giá, giọng thân thiện [no-actions]"}]}).json()
    # the model (fake) would draft right away without the topic; the server asks anyway
    assert j["type"] == "question" and j["question"] == "Agent được làm gì thay anh/chị (gửi, tạo, cập nhật…)?", j
    d, n, msgs = converse(a, "Trợ lý bán hàng cho khách hàng, dùng bảng giá, giọng thân thiện [no-actions]")
    assert d["type"] == "draft" and n == 1 and n <= 5
    e = a.post("/api/builder/chat", json={"lang": "en", "messages": [{"role": "user", "content": "Sales helper for customers using the price list, friendly [no-actions]"}]}).json()
    assert e["type"] == "question" and e["question"] == "What may the agent do on your behalf (send, create, update…)?", e
    d, n, _ = converse(a, "Trợ lý bán hàng [nhieu-cau]"); assert n <= 5 and d["type"] == "draft"                  # the 5-question cap still holds

# ================================================================== 4. today's date in every system prompt
def test_date_line_unit():
    import agent_clock as C
    from zoneinfo import ZoneInfo
    from langchain_core.messages import SystemMessage
    now = datetime(2026, 10, 24, 14, 5, tzinfo=ZoneInfo(TZ))
    assert C.line(now).startswith("Current date and time: Saturday 2026-10-24 14:05 (Asia/Ho_Chi_Minh, UTC+07:00).")
    assert C.stamp(SystemMessage(content="Bạn là trợ lý."), now).content.startswith("Bạn là trợ lý.\n\nCurrent date and time: Saturday 2026-10-24 14:05")
    assert C.stamp(None, now).content.startswith("Current date and time:")
    blocks = C.stamp(SystemMessage(content=[{"type": "text", "text": "a"}]), now).content; assert blocks[0] == {"type": "text", "text": "a"} and "Current date" in blocks[1]["text"]
    tokyo = C.line(datetime(2026, 1, 2, 3, 4, tzinfo=ZoneInfo("Asia/Tokyo"))); assert "(Asia/Tokyo, UTC+09:00)" in tokyo

def test_date_in_system_prompt_workspace_timezone_and_not_saved(env):
    own, adm, aid = env["own"], env["adm"], env["agent"]
    assert adm.get("/api/admin/workspace/timezone").json()["timezone"] == TZ                                    # default
    assert adm.put("/api/admin/workspace/timezone", json={"timezone": "Mars/Base"}).status_code == 422
    r = chat(own, aid, "show system prompt"); assert r.status_code == 200, r.text
    txt = [m for m in r.json()["messages"] if m["type"] == "ai"][-1]["content"]
    today = datetime.now(__import__("zoneinfo").ZoneInfo(TZ))
    assert txt.startswith("SYS<<Bạn là trợ lý.") and f"Current date and time: {today.strftime('%A %Y-%m-%d')}" in txt and "(Asia/Ho_Chi_Minh, UTC+07:00)" in txt, txt
    assert "Current date" not in own.get(f"/api/agents/{aid}").json()["config"]["system_prompt"]               # not baked into the saved prompt
    assert adm.put("/api/admin/workspace/timezone", json={"timezone": "Asia/Tokyo"}).status_code == 200
    txt = [m for m in chat(own, aid, "show system prompt").json()["messages"] if m["type"] == "ai"][-1]["content"]
    assert "(Asia/Tokyo, UTC+09:00)" in txt, txt
    adm.put("/api/admin/workspace/timezone", json={"timezone": TZ})

# ================================================================== 5. a closed tab does not cancel a run; explicit Stop does
def test_closed_stream_keeps_running_then_reconnect_shows_result(a):
    tid = thread(a)
    # what the chat now sends: on_disconnect "continue" (default). The tab "closes" after 3 events.
    evs = sse(a, tid, {"assistant_id": "agent-2", "input": {"messages": [{"type": "human", "content": f"slow answer please {SUF}"}]}, "stream_mode": ["messages-tuple"], "on_disconnect": "continue"}, stop_after=3)
    rid = next(d["run_id"] for e, d in evs if e == "metadata")
    time.sleep(1)
    assert a.get(f"/api/v1/threads/{tid}/runs/{rid}").json()["status"] in ("running", "pending", "success")     # still alive after the disconnect
    # reconnect: join the run's stream (9.6 resumable streams) and read it to the end
    parts = []
    with a.stream("GET", f"/api/v1/threads/{tid}/runs/{rid}/stream", params={"stream_mode": "messages-tuple"}, timeout=120) as r:
        for line in r.iter_lines():
            if line.startswith("event: "): parts.append(line[7:])
    assert parts[-1] == "end" and "messages" in parts
    assert a.get(f"/api/v1/threads/{tid}/runs/{rid}").json()["status"] == "success"
    msgs = a.get(f"/api/v1/threads/{tid}/state").json()["values"]["messages"]; assert msgs[-1]["type"] == "ai" and msgs[-1]["content"]

def test_explicit_stop_cancels(a):
    tid = thread(a)
    evs = sse(a, tid, {"assistant_id": "agent-2", "input": {"messages": [{"type": "human", "content": "slow answer please"}]}, "stream_mode": ["messages-tuple"], "on_disconnect": "continue"}, stop_after=3)
    rid = next(d["run_id"] for e, d in evs if e == "metadata")
    assert a.post(f"/api/v1/threads/{tid}/runs/{rid}/cancel").json()["ok"] is True
    deadline = time.time() + 10; st = None
    while time.time() < deadline:
        st = a.get(f"/api/v1/threads/{tid}/runs/{rid}").json()["status"]
        if st in ("interrupted", "success", "error"): break
        time.sleep(0.3)
    assert st == "interrupted"

def test_chat_runtime_uses_continue_and_explicit_stop():
    src = open(os.path.join(os.path.dirname(__file__), "..", "..", "agentos-chat", "src", "runtime", "useAgentOSRuntime.ts"), encoding="utf-8").read()
    assert 'onDisconnect: "continue"' in src and 'onDisconnect: "cancel"' not in src and "stopRun(" in src and "awaitActiveRun(" in src

# ================================================================== 6. sandbox runtime label
def test_sandbox_runtime_label_is_the_real_runner(a, monkeypatch):
    os.environ.setdefault("GOOGLE_API_KEY", "x-in-process-only")   # deepagent_api builds a genai client at import
    import deepagent_api as A, chat_gd5b as G
    s = a.get("/api/chat/code/settings").json()
    assert s["runtime"].startswith("fake") and "runc" not in s["runtime"], s                                 # local server = fake runner
    async def fake(code, tmo): return 0, "", ""
    monkeypatch.setitem(G.RUNNER, "fn", fake); assert G.runtime_label().startswith("fake")
    monkeypatch.setitem(G.RUNNER, "fn", G._docker)
    monkeypatch.setitem(A.SBX_INFO, "runtime", "docker (runc)"); assert G.runtime_label() == "docker (runc)"
    monkeypatch.setitem(A.SBX_INFO, "runtime", "gvisor (runsc)"); assert G.runtime_label() == "gvisor (runsc)"

# ================================================================== 7. command.update / command.goto, plural
def test_command_update_and_goto(env):
    own, aid = env["own"], env["agent"]
    g = f"agent-{aid}"; tid = own.post("/api/v1/threads", json={"metadata": {"source": "chat", "graph_id": g}}).json()["thread_id"]
    run = lambda body: own.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": g, **body})
    state = lambda: own.get(f"/api/v1/threads/{tid}/state").json()["values"]["messages"]
    assert run({"input": {"messages": [{"type": "human", "content": "echo-user one"}]}}).status_code == 200
    n_before = len(state())
    r = run({"command": {"update": {"messages": [{"type": "human", "content": "ghi chú thêm"}]}}}); assert r.status_code == 200, r.text   # update only = state edit, no model call
    ms = state(); assert len(ms) == n_before + 1 and ms[-1]["type"] == "human" and ms[-1]["content"] == "ghi chú thêm"
    r = run({"command": {"update": {"messages": [{"type": "human", "content": "echo-user two"}]}, "goto": "model"}}); assert r.status_code == 200, r.text   # update + goto = run the node
    ms = state(); assert ms[-1]["type"] == "ai" and ms[-1]["content"] == "USER<<echo-user two>>", ms[-2:]
    assert run({"command": {"foo": 1}}).status_code == 422
    assert run({"command": {"goto": 5}}).status_code == 422
    assert run({"command": {"goto": [{"input": 1}]}}).status_code == 422
    assert run({"command": {"update": "x"}}).status_code == 422

def test_studio_timeline_pluralises():
    import json as _j
    html = httpx.get(f"{B}/studio", auth=ADMIN, follow_redirects=True).text
    assert "c.n_messages===1?'message':'messages'" in html and "b.n_messages===1?'message':'messages'" in html and "b.steps===1?'step':'steps'" in html
    d = _j.load(open(os.path.join(os.path.dirname(__file__), "..", "app", "i18n_dict.json"), encoding="utf-8"))
    assert d["message"]["vi"] == "tin nhắn" and d["messages"]["vi"] == "tin nhắn" and d["step"]["vi"] == "bước"
