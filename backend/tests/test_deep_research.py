"""GD7.7 (23/10/2026): Deep research mode of the Chat (app/deep_research.py). Scripted fake LLM (overlay/fake_llm.py _research_plan) + fake web search
(overlay/run_local.py patches deepagent_api._web_search) => $0, no network. Local server :8020. Throwaway users (suffix SUF).
HTTP tests: plan interrupt -> reject / approve -> report in Canvas -> PDF. In-process unit tests: ResearchGuard limits (searches, spend, time, plan guard)."""
import asyncio, os, sys, time, types, uuid
import httpx, psycopg, pytest
from test_fixes import B, ADMIN, a  # noqa: F401  (fixtures)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
PW = "phase0-gd77-local"; SUF = uuid.uuid4().hex[:6]
G = "agent-2"                                   # the chat's normal agent: the thread stays bound to it, only the run switches to the research agent
DR = {"configurable": {"mode": "deep_research"}}

def user(a, name, role="editor"):
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": role})
    uid = r.json().get("id") if r.status_code == 200 else next(x["id"] for x in a.get("/api/admin/users").json() if x["username"] == name)
    return uid, httpx.Client(base_url=B, auth=(name, PW), timeout=120)

def new_thread(c): return c.post("/api/v1/threads", json={"metadata": {"source": "chat", "graph_id": G}}).json()["thread_id"]
def ask(c, tid, text, config=DR): return c.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": G, "input": {"messages": [{"type": "human", "content": text}]}, "config": config})
def resume(c, tid, decision, config=DR): return c.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": G, "command": {"resume": {"decisions": [decision]}}, "config": config})
def status(c, tid): return c.get(f"/api/v1/threads/{tid}").json()["status"]
def msgs(r): assert r.status_code == 200, r.text; return r.json()["messages"]
def last_ai(r): return [m for m in msgs(r) if m.get("type") == "ai" and m.get("content")][-1]["content"]
def tool_calls(tid):   # every tool call that returned (main agent + subagents), per run, from eval_runs
    with psycopg.connect(PG) as pg:
        return [c for (tc,) in pg.execute("SELECT tool_calls FROM eval_runs WHERE thread_id=%s ORDER BY id", (tid,)).fetchall() for c in tc]
def research_agent(c, tid): return c.get(f"/api/v1/threads/{tid}").json()["metadata"].get("deep_research_agent")
def interrupts(c, tid): return [i for t in c.get(f"/api/v1/threads/{tid}/state").json()["tasks"] for i in t["interrupts"]]

@pytest.fixture(scope="module")
def world(a):
    ua, ca = user(a, f"gd77_a_{SUF}"); ub, cb = user(a, f"gd77_b_{SUF}")
    t0 = time.time()
    yield {"ca": ca, "cb": cb, "t0": t0}
    for c, i in ((ca, ua), (cb, ub)): c.close(); a.delete(f"/api/admin/users/{i}")
    with psycopg.connect(PG, autocommit=True) as pg:   # the cap tests add expensive scripted usage rows; remove them so no provider/feature cap is left tripped
        pg.execute("DELETE FROM gateway_usage WHERE feature='deep_research' AND cost_usd > 0.05 AND ts > to_timestamp(%s)", (t0,))
        pg.execute("DELETE FROM gateway_usage WHERE status='t77'")

def plan_run(c, text="So sánh LangGraph và Deep Agents", config=DR):
    tid = new_thread(c); r = ask(c, tid, text, config); assert r.status_code == 200, r.text
    return tid

# ------------------------------------------------------------------ plan approval comes first
def test_plan_interrupt_happens_before_any_search(world):
    c = world["ca"]; tid = plan_run(c)
    assert status(c, tid) == "interrupted"
    ints = interrupts(c, tid)
    req = ints[0]["value"]["action_requests"][0]
    assert req["name"] == "propose_plan" and "Kế hoạch nghiên cứu" in req["args"]["plan"]
    assert set(ints[0]["value"]["review_configs"][0]["allowed_decisions"]) == {"approve", "edit", "reject"}
    assert [x for x in tool_calls(tid) if x["name"] in ("web_search", "task", "canvas")] == []   # nothing researched yet
    assert research_agent(c, tid)                                                                # thread state is read through the research graph
    assert c.get(f"/api/chat/canvas", params={"thread_id": tid}).json()["items"] == []

def test_reject_means_no_search_and_no_report(world):
    c = world["ca"]; tid = plan_run(c)
    r = resume(c, tid, {"type": "reject", "message": "đổi hướng"}); assert r.status_code == 200
    assert status(c, tid) == "idle" and "chưa được duyệt" in last_ai(r)
    assert [x for x in tool_calls(tid) if x["name"] in ("web_search", "task", "canvas")] == []
    assert c.get("/api/chat/canvas", params={"thread_id": tid}).json()["items"] == []

def test_task_before_plan_is_refused_by_the_guard(world):
    c = world["ca"]; tid = plan_run(c, "So sánh A và B [skip-plan]")
    tm = [m for m in c.get(f"/api/v1/threads/{tid}/state").json()["values"]["messages"] if m["type"] == "tool"]
    assert tm and tm[0]["name"] == "task" and "no approved plan" in tm[0]["content"]
    assert status(c, tid) == "interrupted" and interrupts(c, tid)[0]["value"]["action_requests"][0]["name"] == "propose_plan"   # the fake then plans properly
    assert [x for x in tool_calls(tid) if x["name"] == "web_search"] == []

# ------------------------------------------------------------------ approve -> researchers -> report in Canvas -> PDF
@pytest.fixture(scope="module")
def done(world):
    c = world["ca"]; tid = plan_run(c)
    r = resume(c, tid, {"type": "approve"}); assert r.status_code == 200, r.text
    items = c.get("/api/chat/canvas", params={"thread_id": tid}).json()["items"]
    return {"tid": tid, "r": r, "items": items}

def test_plan_approved_from_the_inbox_continues_the_research(world):
    """GĐ7.Z: approving from the Inbox (/api/approvals/{id}) must resume the same checkpoint as the chat's approve button."""
    c = world["ca"]; tid = plan_run(c)
    row = [x for x in c.get("/api/approvals").json() if x["thread_id"] == tid][0]
    r = c.post(f"/api/approvals/{row['id']}", json={"decision": "approve"}); assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved" and status(c, tid) == "idle"
    assert [x["name"] for x in tool_calls(tid)].count("web_search") == 2
    assert len(c.get("/api/chat/canvas", params={"thread_id": tid}).json()["items"]) == 1

def test_inbox_approval_of_a_private_thread_is_private(world):
    """GĐ7.Z review: another editor must not see (plan text) or decide the approval of someone else's private chat thread."""
    ca, cb = world["ca"], world["cb"]; tid = plan_run(ca)
    row = [x for x in ca.get("/api/approvals").json() if x["thread_id"] == tid][0]
    assert all(x["thread_id"] != tid for x in cb.get("/api/approvals", params={"status": "all"}).json())
    assert cb.post(f"/api/approvals/{row['id']}", json={"decision": "approve"}).status_code == 404
    assert status(ca, tid) == "interrupted"

def test_approve_runs_researchers_and_writes_report_to_canvas(world, done):
    c = world["ca"]; tid = done["tid"]; assert status(c, tid) == "idle"
    calls = tool_calls(tid)
    assert [x["name"] for x in calls].count("web_search") == 2 and [x["name"] for x in calls].count("think_tool") == 2   # 2 parallel researchers: search + reflect each
    assert [x["name"] for x in calls if x["depth"] == 0].count("task") == 2 and all(x["depth"] >= 1 for x in calls if x["name"] == "web_search")
    assert len(done["items"]) == 1 and done["items"][0]["author"] == "agent"
    doc = c.get(f"/api/chat/canvas/{done['items'][0]['doc_id']}").json()
    md = doc["content"]
    assert md.startswith("# ") and "## Mục lục" in md and md.index("## Mục lục") < md.index("## Tổng quan")       # title, then the table of contents at the top
    assert all(f"[{n}]" in md for n in (1, 2, 3)) and "### Nguồn" in md and "https://docs.langchain.com/oss/python/langgraph/overview" in md
    nums = [int(x) for x in __import__("re").findall(r"^\[(\d+)\] ", md.split("### Nguồn")[1], __import__("re").M)]
    assert nums == list(range(1, len(nums) + 1))                                                                      # sources numbered 1..n without gaps
    assert "Canvas" in last_ai(done["r"]) and "# Báo cáo" not in last_ai(done["r"])                                   # short summary, not the whole report again

def test_pdf_export_vietnamese_and_access_control(world, done):
    c, cb = world["ca"], world["cb"]; doc = done["items"][0]["doc_id"]
    r = c.get(f"/api/chat/canvas/{doc}/pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf" and r.content[:5] == b"%PDF-" and len(r.content) > 2000
    cd = r.headers["content-disposition"]; assert cd.startswith("attachment;") and ".pdf" in cd and "\n" not in cd and "filename*=UTF-8''" in cd
    assert c.get(f"/api/chat/canvas/{doc}/pdf", params={"v": 1}).status_code == 200
    assert c.get(f"/api/chat/canvas/{doc}/pdf", params={"v": 99}).status_code == 404
    assert c.get("/api/chat/canvas/doesnotexist/pdf").status_code == 404
    assert cb.get(f"/api/chat/canvas/{doc}/pdf").status_code in (403, 404)                                              # another user: not their thread
    assert httpx.get(B + f"/api/chat/canvas/{doc}/pdf").status_code == 401

def test_pdf_does_not_fetch_external_resources_or_render_html():
    import deep_research as DRM
    pdf = DRM.md_to_pdf("Test", "# Xin chào\n\n![x](http://127.0.0.1:9/evil.png)\n\n<script>alert(1)</script>\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    assert pdf[:5] == b"%PDF-"
    raw = DRM.md_to_pdf("Test", "# Xin chào Việt Nam", uncompressed_pdf=True)
    assert b"DejaVu-Sans" in raw and b"http://127.0.0.1:9" not in raw and b"alert(1)" not in pdf                    # Vietnamese set in an embedded DejaVu font; nothing external
    assert DRM.pdf_filename("Báo cáo: Đường đi/của\"AI\"")[0] == "Bao_cao_Duong_di_cua_AI.pdf"

# ------------------------------------------------------------------ tagging, caps, mode allow-list, demo
def test_runs_are_tagged_feature_deep_research(world, done):
    aid = research_agent(world["ca"], done["tid"])
    with psycopg.connect(PG) as pg:
        feats = {r[0] for r in pg.execute("SELECT feature FROM gateway_usage WHERE agent_id=%s AND ts > to_timestamp(%s)", (aid, world["t0"])).fetchall()}
        tags = pg.execute("SELECT count(*) FROM gateway_usage WHERE agent_id=%s AND feature IS DISTINCT FROM 'deep_research' AND ts > to_timestamp(%s)", (aid, world["t0"])).fetchone()[0]
    assert feats == {"deep_research"} and tags == 0

def test_mode_is_allow_listed_and_other_runs_unaffected(world):
    c = world["ca"]
    for cfg in ({"configurable": {"mode": "bogus"}}, {"configurable": {"mode": "deep_research "}}, {"configurable": {"mode": ["deep_research"]}}, {}):
        tid = new_thread(c); r = ask(c, tid, "xin chào mode test", cfg)
        assert r.status_code == 200 and last_ai(r).startswith("Echo:") and status(c, tid) == "idle" and research_agent(c, tid) is None, cfg
    tid = new_thread(c); ask(c, tid, "hello flat", {"mode": "deep_research"}); assert research_agent(c, tid)           # flat key accepted like the other overrides
    ask(c, tid, "now a normal question", {}); assert research_agent(c, tid) is None                                 # a normal run on the same thread switches back
    t2 = plan_run(c); c.patch(f"/api/v1/threads/{t2}", json={"metadata": {"deep_research_agent": 1}})               # a client cannot point the thread at another agent
    assert c.get(f"/api/v1/threads/{t2}/state").status_code == 200

def test_resume_without_mode_still_continues_the_research(world):
    c = world["ca"]; tid = plan_run(c)
    r = resume(c, tid, {"type": "approve"}, config={}); assert r.status_code == 200
    assert len(c.get("/api/chat/canvas", params={"thread_id": tid}).json()["items"]) == 1

def test_feature_cap_deep_research_blocks_402(world, a):
    c = world["ca"]
    with psycopg.connect(PG, autocommit=True) as pg:
        pg.execute("INSERT INTO gateway_usage(ts,model,input_tokens,output_tokens,cost_usd,status,feature) VALUES(now(),'gemini-3.8-flash',0,0,0.5,'t77','deep_research')")
    cap = a.post("/api/gateway/caps", json={"scope": "feature", "target": "deep_research", "period": "total", "limit_usd": 0.4, "action": "block"}); assert cap.status_code == 200, cap.text
    try:
        r = ask(c, new_thread(c), "chặn?"); assert r.status_code == 402 and r.json()["detail"]["code"] == "spend_cap" and "Nghiên cứu sâu" in r.json()["detail"]["message"], r.text   # GĐ8.P: detail object
        assert ask(c, new_thread(c), "chat thường vẫn chạy", {}).status_code == 200
    finally:
        a.delete(f"/api/gateway/caps/{cap.json()['id']}")
        with psycopg.connect(PG, autocommit=True) as pg: pg.execute("DELETE FROM gateway_usage WHERE status='t77'")

def test_demo_user_cannot_use_deep_research(world, a):
    dname = f"gd77_demo_{SUF}"
    uid = a.post("/api/admin/users", json={"username": dname, "password": PW, "role": "viewer", "demo": True, "daily_cap_usd": 0.05, "demo_agents": [2]}).json()["id"]
    p = a.post("/api/admin/demo-workspace", json={"user_id": uid, "name": "gd77 demo " + SUF, "source_agents": [2], "daily_cap_usd": 2.0}); assert p.status_code == 200, p.text
    try:
        with httpx.Client(base_url=B, auth=(dname, PW), timeout=60) as d:
            a2 = d.post("/api/v1/assistants/search", json={"limit": 5}).json()[0]["graph_id"]
            tid = d.post("/api/v1/threads", json={"metadata": {"source": "chat", "graph_id": a2}}).json()["thread_id"]
            r = d.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": a2, "input": {"messages": [{"type": "human", "content": "x"}]}, "config": DR})
            assert r.status_code == 403, r.text                                                                   # plan approval is impossible for demo accounts
    finally:
        with psycopg.connect(PG, autocommit=True) as pg:
            pg.execute("UPDATE users SET disabled=true WHERE id=%s", (uid,)); pg.execute("DELETE FROM lg_threads WHERE workspace_id=%s", (p.json()["workspace_id"],))

# ------------------------------------------------------------------ limits (end to end)
def test_per_researcher_search_limit_enforced_by_toolcalllimit(world):
    c = world["ca"]; tid = plan_run(c, "So sánh X và Y [flood]")
    r = resume(c, tid, {"type": "approve"}); assert r.status_code == 200 and status(c, tid) == "idle"
    res = [x for x in tool_calls(tid) if x["name"] == "web_search"]
    blocked = [x for x in res if "limit" in x["result"].lower() and "exceed" in x["result"].lower()]
    assert len(res) == 14 and len(blocked) == 4 and len(res) - len(blocked) == 10                                    # 2 researchers x (7 asked, 5 allowed)

def test_spend_cap_stops_the_run_gracefully(world):
    c = world["ca"]; assert c.post("/api/auth/lang", json={"lang": "vi"}).status_code == 200   # GĐ8.P: the stop message follows the user's UI language
    tid = plan_run(c, "So sánh X và Y [cost-tokens:450000]")   # each orchestrator model call bills 450k input tokens = ~$0.34 > the $0.30 cap
    r = resume(c, tid, {"type": "approve"}); assert r.status_code == 200 and status(c, tid) == "idle"
    assert "Đã dừng nghiên cứu" in last_ai(r) and "trần chi phí" in last_ai(r) and " / " not in last_ai(r)
    assert [x for x in tool_calls(tid) if x["name"] in ("web_search", "canvas")] == []
    assert c.get("/api/chat/canvas", params={"thread_id": tid}).json()["items"] == []

# ------------------------------------------------------------------ limits (in-process unit tests of ResearchGuard)
def _req(name, msgs=(), id_="c1"): return types.SimpleNamespace(tool_call={"name": name, "id": id_, "args": {}}, state={"messages": list(msgs)})
async def _ok(request): return "ran"

def _guard(sub=False):
    import deep_research as D
    return D, D.ResearchGuard(sub)

def test_global_search_budget_per_run(monkeypatch):
    monkeypatch.setenv("AOS_DEEP_RESEARCH_MAX_SEARCHES", "3")
    D, g = _guard(True)
    async def go():
        D.begin(True); out = [await g.awrap_tool_call(_req("web_search"), _ok) for _ in range(5)]
        return out, D.RUN.get().searches
    out, n = asyncio.run(go())
    assert out[:3] == ["ran"] * 3 and all(getattr(o, "status", "") == "error" and "budget exhausted" in o.content for o in out[3:]) and n == 3
    async def fresh(): D.begin(True); return await g.awrap_tool_call(_req("web_search"), _ok)
    assert asyncio.run(fresh()) == "ran"                                                                             # a new run starts with a fresh budget
    async def none_mode(): D.begin(False); return await g.awrap_tool_call(_req("web_search"), _ok)
    assert asyncio.run(none_mode()) == "ran"                                                                         # outside research mode the guard does nothing

def test_spend_cap_and_timeout_stop_before_the_next_model_call(monkeypatch):
    monkeypatch.setenv("AOS_DEEP_RESEARCH_MAX_USD", "0.30")
    D, g = _guard()
    async def go(spent=0.0, age=0.0):
        D.begin(True); r = D.RUN.get(); r.spent = spent; r.t0 -= age
        return await g.abefore_model({"messages": []}, None)
    assert asyncio.run(go(0.29)) is None
    o = asyncio.run(go(0.31)); assert o["jump_to"] == "end" and "trần chi phí" in o["messages"][0].content
    o = asyncio.run(go(0.0, 601)); assert o["jump_to"] == "end" and "giới hạn thời gian" in o["messages"][0].content
    async def go_en(age):
        from langchain_core.messages import HumanMessage
        D.begin(True); D.RUN.get().t0 -= age
        return await g.abefore_model({"messages": [HumanMessage("Compare open-source LLM observability tools")]}, None)
    o = asyncio.run(go_en(601)); assert "time limit" in o["messages"][0].content and "giới hạn" not in o["messages"][0].content
    async def sink():   # spend is summed from the gateway usage records of THIS run only
        D.begin(True); await D._sink(1, "gemini-3.8-flash", 10, 10, 0.2, "ok", 1, None); await D._sink(1, "gemini-3.8-flash", 10, 10, 0.2, "ok", 1, None); s = D.RUN.get().spent
        D.begin(False); await D._sink(1, "m", 1, 1, 5.0, "ok", 1, None); return s
    assert abs(asyncio.run(sink()) - 0.4) < 1e-9

def test_soft_deadline_stops_research_tools_so_a_report_is_still_written():
    """GD7.R: after 70% of the time limit, web_search/fetch_url/task are refused with "write the final report now"; other tools still run."""
    D, g = _guard()
    D.begin(True); D.RUN.get().t0 -= 0.75 * D.limits()["timeout"]
    run = lambda req: asyncio.run(g.awrap_tool_call(req, _ok))
    for n in ("web_search", "fetch_url", "task"): assert "write the final report now" in run(_req(n)).content
    assert run(_req("think_tool")) == "ran"
    D.begin(True); assert run(_req("web_search")) == "ran"   # a fresh run is not affected

def test_plan_guard_parallel_and_round_limits(monkeypatch):
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    monkeypatch.setenv("AOS_DEEP_RESEARCH_MAX_PARALLEL", "2"); monkeypatch.setenv("AOS_DEEP_RESEARCH_MAX_ROUNDS", "2")
    D, g = _guard()
    t = lambda i: {"name": "task", "id": i, "args": {}}
    plan = [HumanMessage("q"), AIMessage("", tool_calls=[{"name": "propose_plan", "id": "p", "args": {"plan": "x"}}])]
    approved = plan + [ToolMessage("ok", tool_call_id="p", name="propose_plan")]
    rejected = plan + [ToolMessage("no", tool_call_id="p", name="propose_plan", status="error")]
    assert not D.plan_approved(plan) and not D.plan_approved(rejected) and D.plan_approved(approved) and not D.plan_approved(approved + [HumanMessage("new turn")])
    run = lambda req: asyncio.run(g.awrap_tool_call(req, _ok))
    r1 = AIMessage("", tool_calls=[t("a"), t("b"), t("c")])
    assert "no approved plan" in run(_req("task", plan + [r1], "a")).content and "no approved plan" in run(_req("task", rejected + [r1], "a")).content
    st = approved + [r1]
    assert run(_req("task", st, "a")) == "ran" and run(_req("task", st, "b")) == "ran" and "at most 2 researchers" in run(_req("task", st, "c")).content
    r2, r3 = AIMessage("", tool_calls=[t("d")]), AIMessage("", tool_calls=[t("e")])
    st = approved + [r1, r2, r3]
    assert run(_req("task", st, "d")) == "ran" and "round limit" in run(_req("task", st, "e")).content
    assert run(_req("think_tool", plan)) == "ran"                                                                    # other tools are never blocked by the guard

def test_run_mode_allow_list_and_limit_defaults(monkeypatch):
    import deep_research as D
    assert D.run_mode({"configurable": {"mode": "deep_research"}}) == "deep_research" and D.run_mode({"mode": "deep_research"}) == "deep_research"
    for bad in (None, {}, {"configurable": {}}, {"configurable": {"mode": "x"}}, {"configurable": {"mode": ["deep_research"]}}, "deep_research"): assert D.run_mode(bad) is None, bad
    for k in [k for k in os.environ if k.startswith("AOS_DEEP_RESEARCH_")]: monkeypatch.delenv(k)
    assert D.limits() == {"par": 3, "rounds": 3, "searches": 15, "timeout": 600.0, "usd": 0.30, "soft": 0.7}
    monkeypatch.setenv("AOS_DEEP_RESEARCH_MAX_USD", "0.5"); monkeypatch.setenv("AOS_DEEP_RESEARCH_MAX_ROUNDS", "zz")
    assert D.limits()["usd"] == 0.5 and D.limits()["rounds"] == 3                                                    # bad value -> default
    assert "{max_parallel}" not in D.orchestrator_prompt() and "at most 3 parallel" in D.orchestrator_prompt().lower()
