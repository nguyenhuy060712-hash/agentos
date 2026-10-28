"""GĐ13.30 durable runs (card 13.30). In-process, real Postgres test DB (private pool under the RLS role), fake SMTP = a list the fake send tool appends to, no LLM ($0).
Covers: the duplicate-send guard (tool_idem) with a real LangGraph agent + scripted model + a crash injected after the mail went out (exactly ONE mail after the retry), unknown
outcome never repeated, same tool_call_id with other arguments refused, replay is per run (another run / another workspace sends), the sweeper (re-queue once, 4th failure =
failure_after_retry, a live heartbeat is left alone, another workspace is never touched, model / tool errors are never retried, demo workspace off), heartbeat, timeout,
SIGTERM-style shutdown (nothing stays `running`), the queue worker end to end, resume-from-checkpoint decision, admin stats endpoint (admin only) and RLS on tool_idem."""
import asyncio, os, sys, uuid
import psycopg, pytest
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

os.environ.setdefault("GOOGLE_API_KEY", "x-not-used")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import deepagent_api as A  # noqa: E402,I001  (must come first: circular imports)
import admin, durable, gateway, migrations, mocks, server  # noqa: E402

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
TAG = uuid.uuid4().hex[:6]
WA, WB, WDEMO = 93701, 93702, 93703
SEEDS: dict = {}
OUTBOX: list = []      # the fake SMTP server's mailbox
CRASHED: list = []

def U(ws, uid, role="editor"): return {"id": uid, "username": f"u{uid}", "role": role, "ws": ws}

def run(fn):
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as c:
            await migrations.run_pending(c)
        async with AsyncConnectionPool(PG, min_size=1, max_size=4, open=False, kwargs={"autocommit": True, "row_factory": dict_row}) as pool:
            await pool.open(); A.S["pool"] = pool; A.S["rls"] = admin.rls_ctx
            for g in ("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO aos_rls", "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO aos_rls"):
                for i in range(5):   # other cards GRANT / ALTER at the same time ("tuple concurrently updated")
                    try: await A.q(g); break
                    except psycopg.errors.InternalError_:
                        if i == 4: raise
                        await asyncio.sleep(0.5)
            await admin._policy("tool_idem", "aos_ws() IS NULL OR (run_id IN (SELECT run_id FROM lg_runs))")   # what rls_init does at boot (RLS_CHILD)
            await seed()
            try: return await fn()
            finally: await cleanup()
    return asyncio.run(go())

async def seed():
    for w in (WA, WB, WDEMO):
        await A.q("INSERT INTO workspaces(id,name) VALUES(%s,%s) ON CONFLICT DO NOTHING", (w, f"t1330-{TAG}-{w}"))
    for k in ("ua", "ub"):
        SEEDS[k] = (await A.q("INSERT INTO users(username,pw_hash) VALUES(%s,'x') RETURNING id", (f"t1330-{TAG}-{k}",), one=True))["id"]
    gateway.DEMO_WS.add(WDEMO)

async def cleanup():
    gateway.DEMO_WS.discard(WDEMO)
    await A.q("DELETE FROM lg_run_events WHERE run_id IN (SELECT run_id FROM lg_runs WHERE thread_id LIKE %s)", (f"t1330-{TAG}-%",))
    await A.q("DELETE FROM lg_runs WHERE thread_id LIKE %s", (f"t1330-{TAG}-%",))
    await A.q("DELETE FROM lg_threads WHERE thread_id LIKE %s", (f"t1330-{TAG}-%",))
    await A.q("DELETE FROM users WHERE id = ANY(%s)", ([SEEDS["ua"], SEEDS["ub"]],))
    await A.q("DELETE FROM workspaces WHERE id = ANY(%s)", ([WA, WB, WDEMO],))

async def mk_run(ws, *, status="running", attempt=0, age_s=600, hb_age_s=None, user=None, worker="wtest", start_ckpt=None, thread=None, blocker=False, failure_kind=None):
    """A lg_runs row whose heartbeat (or start) is `age_s` old. blocker=True: a second, live `running` run on the same thread, so no other queue process (the shared dev server)
    can claim the row once it is `pending` (one running run per thread)."""
    rid = str(uuid.uuid4()); tid = thread or f"t1330-{TAG}-{uuid.uuid4().hex[:6]}"
    await A.q("INSERT INTO lg_threads(thread_id,workspace_id,status) VALUES(%s,%s,'busy') ON CONFLICT DO NOTHING", (tid, ws))
    if blocker:
        await A.q("INSERT INTO lg_runs(run_id,thread_id,status,workspace_id,worker,started_at,heartbeat_at) VALUES(%s,%s,'running',%s,'wblocker',now(),now())", (str(uuid.uuid4()), tid, ws))
    await A.q("""INSERT INTO lg_runs(run_id,thread_id,status,workspace_id,user_id,attempt,worker,started_at,heartbeat_at,start_ckpt,failure_kind,input)
                 VALUES(%s,%s,%s,%s,%s,%s,%s,now() - make_interval(secs => %s),CASE WHEN %s::float IS NULL THEN NULL ELSE now() - make_interval(secs => %s::float) END,%s,%s,%s)""",
              (rid, tid, status, ws, user, attempt, worker if status == "running" else None, age_s, hb_age_s, hb_age_s, start_ckpt, failure_kind, Jsonb({"messages": [{"role": "user", "content": "gửi mail"}]})))
    return rid

async def row(rid): return await A.q("SELECT * FROM lg_runs WHERE run_id=%s", (rid,), one=True)

# ------------------------------------------------------------------ agent with a fake "send mail" write tool
def agent_for(*, send_args=None, script_calls=None, saver=None, crash_in_tool=False):
    from langchain.agents import create_agent
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.tools import StructuredTool
    from langgraph.checkpoint.memory import InMemorySaver
    class Scripted(BaseChatModel):
        script: list
        @property
        def _llm_type(self): return "scripted"
        def bind_tools(self, tools, **kw): return self
        def _generate(self, messages, stop=None, run_manager=None, **kw):
            n = sum(1 for m in messages if isinstance(m, AIMessage))
            return ChatResult(generations=[ChatGeneration(message=self.script[n] if n < len(self.script) else AIMessage(content="mail sent"))])
    async def send_mail(to: str, subject: str = "") -> str:
        """Send a mail through the (fake) SMTP server."""
        OUTBOX.append((to, subject))
        if crash_in_tool and not CRASHED: CRASHED.append(1); raise Crash()   # killed right after the SMTP server accepted the mail, before the tool returned
        return f"sent #{len(OUTBOX)} to {to}"
    async def read_inbox() -> str:
        """Read-only tool."""
        return "empty"
    tools = [StructuredTool.from_function(coroutine=send_mail, name="send_mail"), StructuredTool.from_function(coroutine=read_inbox, name="read_inbox")]
    calls = script_calls or [{"name": "send_mail", "args": send_args or {"to": "a@x.vn", "subject": "hi"}, "id": "call_1"}]
    sv = saver or InMemorySaver()
    return create_agent(model=Scripted(script=[AIMessage(content="", tool_calls=calls)]), tools=tools, middleware=[mocks.AuditHooks(1)], checkpointer=sv), sv

class Crash(BaseException):
    """The worker process is killed: nothing after this point runs, no `except Exception` handler sees it."""

def _crash_after_tool_log(monkeypatch, n=1):
    """Kill the run exactly after the write tool's call returned (the mail went out, the guard stored its result) and before LangGraph recorded the step: AuditHooks logs the
    call in its `finally`, which runs after the guard returned."""
    real = mocks._log; left = [n]
    async def _log(aid, kind, name, ms, ok, detail):
        await real(aid, kind, name, ms, ok, detail)
        if kind == "tool" and name == "send_mail" and left[0] > 0: left[0] -= 1; raise Crash()
    monkeypatch.setattr(mocks, "_log", _log)

async def drive(ag, ws, uid, thread, rid, *, first=True):
    """One execution attempt of a queue run: durability context + the same astream keywords as deepagent_api._run_agent."""
    from langchain_core.messages import HumanMessage
    tok_u = admin.USER.set(U(ws, uid)); tok_c = durable.CUR.set({"run_id": rid, "ws": ws, "attempt": 0 if first else 1})
    try:
        out = None
        async for upd in ag.astream({"messages": [HumanMessage("gửi mail cho a@x.vn")]} if first else None, config={"configurable": {"thread_id": thread}}, stream_mode="values", **durable.stream_kw()): out = upd
        return out
    finally: durable.CUR.reset(tok_c); admin.USER.reset(tok_u)

def test_crash_after_send_then_retry_sends_exactly_one_mail(monkeypatch):
    monkeypatch.setenv("AOS_DURABLE_WRITE_TOOLS", "send_mail"); OUTBOX.clear()
    async def go():
        rid = await mk_run(WA, user=SEEDS["ua"]); ag, sv = agent_for(); _crash_after_tool_log(monkeypatch); th = f"t1330-{TAG}-ck1"
        with pytest.raises(Crash): await drive(ag, WA, SEEDS["ua"], th, rid)
        assert len(OUTBOX) == 1
        st = await A.q("SELECT tool, result FROM tool_idem WHERE run_id=%s", (rid,))
        assert [r["tool"] for r in st] == ["send_mail"] and st[0]["result"]["content"].startswith("sent #1")
        out = await drive(ag, WA, SEEDS["ua"], th, rid, first=False)       # the retry: resume from the checkpoint, empty input
        assert len(OUTBOX) == 1, OUTBOX                                     # ONE mail, not two
        assert out["messages"][-1].content == "mail sent"
        assert durable.COUNTERS["dedup"] >= 1
    run(go)

def test_control_without_the_guard_the_mail_goes_out_twice(monkeypatch):
    """Proves the crash injection is real: with send_mail NOT treated as a write tool the retry repeats the tool."""
    monkeypatch.delenv("AOS_DURABLE_WRITE_TOOLS", raising=False); OUTBOX.clear()
    async def go():
        rid = await mk_run(WA, user=SEEDS["ua"]); ag, sv = agent_for(); _crash_after_tool_log(monkeypatch); th = f"t1330-{TAG}-ctl"
        with pytest.raises(Crash): await drive(ag, WA, SEEDS["ua"], th, rid)
        await drive(ag, WA, SEEDS["ua"], th, rid, first=False)
        assert len(OUTBOX) == 2 and not await A.q("SELECT 1 FROM tool_idem WHERE run_id=%s", (rid,))
    run(go)

def test_killed_inside_the_tool_outcome_unknown_is_never_repeated(monkeypatch):
    monkeypatch.setenv("AOS_DURABLE_WRITE_TOOLS", "send_mail"); OUTBOX.clear(); CRASHED.clear()
    async def go():
        rid = await mk_run(WA, user=SEEDS["ua"]); ag, sv = agent_for(crash_in_tool=True); th = f"t1330-{TAG}-unk"
        with pytest.raises(Crash): await drive(ag, WA, SEEDS["ua"], th, rid)
        st = await A.q("SELECT result FROM tool_idem WHERE run_id=%s", (rid,))
        assert len(st) == 1 and st[0]["result"] is None                      # claimed before calling out, no result: unknown
        out = await drive(ag, WA, SEEDS["ua"], th, rid, first=False)
        assert len(OUTBOX) == 1, OUTBOX                                      # the retry did NOT send again ...
        tool_msgs = [m for m in out["messages"] if m.type == "tool"]
        assert len(tool_msgs) == 1 and "NOT repeated" in tool_msgs[0].content   # ... and the model was told so
    run(go)

def _req(name, args, tcid="call_1", mid="m1"):
    from types import SimpleNamespace
    from langchain_core.messages import AIMessage
    tc = {"name": name, "args": args, "id": tcid}
    return SimpleNamespace(tool_call=tc, state={"messages": [AIMessage(content="", id=mid, tool_calls=[tc])]}, tool=None)

async def _sender(request):
    from langchain_core.messages import ToolMessage
    OUTBOX.append(request.tool_call["args"]); return ToolMessage(content=f"sent #{len(OUTBOX)}", tool_call_id=request.tool_call["id"], name=request.tool_call["name"])

def _in_run(ws, uid, rid):
    return admin.USER.set(U(ws, uid)), durable.CUR.set({"run_id": rid, "ws": ws, "attempt": 0})

def test_guard_replay_returns_stored_result_other_args_refused(monkeypatch):
    monkeypatch.setenv("AOS_DURABLE_WRITE_TOOLS", "send_mail"); OUTBOX.clear()
    async def go():
        rid = await mk_run(WA, user=SEEDS["ua"]); tu, tc = _in_run(WA, SEEDS["ua"], rid)
        try:
            r1 = await durable.guard(_req("send_mail", {"to": "a@x.vn"}), _sender)
            r2 = await durable.guard(_req("send_mail", {"to": "a@x.vn"}), _sender)           # replay of the same call
            assert (r1.content, r2.content, len(OUTBOX)) == ("sent #1", "sent #1", 1)
            r3 = await durable.guard(_req("send_mail", {"to": "evil@x.vn"}), _sender)        # same AIMessage + tool_call_id, other arguments: refused
            assert "refused" in r3.content and r3.status == "error" and len(OUTBOX) == 1
            r4 = await durable.guard(_req("send_mail", {"to": "evil@x.vn"}, mid="m2"), _sender)   # a later step that reuses `call_1` (provider quirk) is a NEW call
            assert r4.content == "sent #2" and len(OUTBOX) == 2
        finally: durable.CUR.reset(tc); admin.USER.reset(tu)
    run(go)

def test_guard_is_per_run_and_per_workspace_and_ignores_reads_and_non_queue_calls(monkeypatch):
    monkeypatch.setenv("AOS_DURABLE_WRITE_TOOLS", "send_mail"); OUTBOX.clear()
    async def go():
        ra = await mk_run(WA, user=SEEDS["ua"]); rb = await mk_run(WB, user=SEEDS["ub"]); ra2 = await mk_run(WA, user=SEEDS["ua"])
        for ws, uid, rid in ((WA, SEEDS["ua"], ra), (WB, SEEDS["ub"], rb), (WA, SEEDS["ua"], ra2)):   # same ids + same arguments in three runs: three mails
            tu, tc = _in_run(ws, uid, rid)
            try: await durable.guard(_req("send_mail", {"to": "a@x.vn"}), _sender)
            finally: durable.CUR.reset(tc); admin.USER.reset(tu)
        assert len(OUTBOX) == 3 and len(await A.q("SELECT 1 FROM tool_idem WHERE run_id = ANY(%s)", ([ra, rb, ra2],))) == 3
        tu, tc = _in_run(WA, SEEDS["ua"], ra)
        try:
            await durable.guard(_req("read_inbox", {}, tcid="r1"), _sender); await durable.guard(_req("read_inbox", {}, tcid="r1"), _sender)   # read tools: always run, no row
        finally: durable.CUR.reset(tc); admin.USER.reset(tu)
        assert len(OUTBOX) == 5 and not await A.q("SELECT 1 FROM tool_idem WHERE tool='read_inbox'")
        await durable.guard(_req("send_mail", {"to": "z@x.vn"}, tcid="n1"), _sender)         # outside a queue run (chat, tests): plain handler call, nothing recorded
        assert len(OUTBOX) == 6 and not await A.q("SELECT 1 FROM tool_idem WHERE tool_call_id LIKE %s", ("%:n1",))
    run(go)

def test_guard_handler_exception_releases_the_claim(monkeypatch):
    monkeypatch.setenv("AOS_DURABLE_WRITE_TOOLS", "send_mail"); OUTBOX.clear()
    async def go():
        rid = await mk_run(WA, user=SEEDS["ua"]); tu, tc = _in_run(WA, SEEDS["ua"], rid)
        async def boom(request): raise RuntimeError("smtp refused")
        try:
            with pytest.raises(RuntimeError): await durable.guard(_req("send_mail", {"to": "a@x.vn"}), boom)
            assert not await A.q("SELECT 1 FROM tool_idem WHERE run_id=%s", (rid,))
            assert (await durable.guard(_req("send_mail", {"to": "a@x.vn"}), _sender)).content == "sent #1"   # the later attempt may try again
        finally: durable.CUR.reset(tc); admin.USER.reset(tu)
    run(go)

def test_write_tool_classification(monkeypatch):
    monkeypatch.delenv("AOS_DURABLE_WRITE_TOOLS", raising=False)
    import workspace_tools as W
    import workspace_tools_google, workspace_tools_ms  # noqa: F401  (they register their tools)
    w = next(n for n, m in W.BY_NAME.items() if m.tier != W.READ); r = next(n for n, m in W.BY_NAME.items() if m.tier == W.READ)
    from types import SimpleNamespace as NS
    assert durable.is_write_tool(w) and not durable.is_write_tool(r)
    assert durable.is_write_tool("crm_log_deal") and durable.is_write_tool("content_publish") and not durable.is_write_tool("chay_code_python")
    assert not durable.is_write_tool("tra_cuu_kien_thuc") and not durable.is_write_tool("some_unknown_tool")
    assert durable.is_write_tool("x", NS(metadata={"mcp": {"tool": {"annotations": {}}}})) and not durable.is_write_tool("x", NS(metadata={"mcp": {"tool": {"annotations": {"readOnlyHint": True}}}}))
    monkeypatch.setenv("AOS_DURABLE_WRITE_TOOLS", "slack_send, telegram_send")
    assert durable.is_write_tool("slack_send") and durable.is_write_tool("telegram_send")

def test_demo_workspace_has_no_durability_context():
    run_row = {"run_id": uuid.uuid4(), "workspace_id": WDEMO, "attempt": 0}
    async def go():
        assert durable.context_for(run_row) is None and durable.context_for({**run_row, "workspace_id": WA})["ws"] == WA
        assert durable.stream_kw() == {}                                # no run context: plain checkpoints
        tok = durable.CUR.set(durable.context_for({**run_row, "workspace_id": WA}))
        try: assert durable.stream_kw() == {"durability": "sync"}
        finally: durable.CUR.reset(tok)
    run(go)

def test_tool_idem_rls_other_workspace_cannot_read_or_write(monkeypatch):
    monkeypatch.setenv("AOS_DURABLE_WRITE_TOOLS", "send_mail"); OUTBOX.clear()
    async def go():
        ra = await mk_run(WA, user=SEEDS["ua"]); tu, tc = _in_run(WA, SEEDS["ua"], ra)
        try: await durable.guard(_req("send_mail", {"to": "a@x.vn"}), _sender)
        finally: durable.CUR.reset(tc); admin.USER.reset(tu)
        tb = admin.USER.set(U(WB, SEEDS["ub"], "admin"))                    # an admin of ANOTHER workspace
        try:
            assert await A.q("SELECT 1 FROM tool_idem WHERE run_id=%s", (ra,)) == []
            with pytest.raises(psycopg.errors.InsufficientPrivilege):       # cannot plant a "done" row for A's run either
                await A.q("INSERT INTO tool_idem(run_id,tool_call_id,tool,result) VALUES(%s,'x:y','send_mail',%s)", (ra, Jsonb({"content": "forged"})))
        finally: admin.USER.reset(tb)
        ta = admin.USER.set(U(WA, SEEDS["ua"]))
        try: assert len(await A.q("SELECT 1 FROM tool_idem WHERE run_id=%s", (ra,))) == 1
        finally: admin.USER.reset(ta)
    run(go)

# ------------------------------------------------------------------ resume decision
def test_resume_mode_decision():
    f = durable.resume_mode
    assert f({"start_ckpt": None}, "c9") is False             # the first attempt never reached the graph: start from the original input
    assert f({}, "c9") is False
    assert f({"start_ckpt": "c1"}, "c1") is False             # the earlier attempt wrote no checkpoint: nothing to resume
    assert f({"start_ckpt": "c1"}, "c2") is True              # progress: resume with an empty input
    assert f({"start_ckpt": "-"}, None) is False              # new thread, still empty
    assert f({"start_ckpt": "-"}, "c1") is True               # new thread, the crashed attempt wrote its input checkpoint

# ------------------------------------------------------------------ sweeper
@pytest.fixture(autouse=True)
def mine_only(monkeypatch):
    """The dev DB is shared: every sweep / recovery in these tests may only see this test's own threads (a real stale run of someone else must not be re-queued by a test)."""
    monkeypatch.setattr(durable, "_STALE_SQL", durable._STALE_SQL + f" AND thread_id LIKE 't1330-{TAG}-%%'")
    real = durable._q
    async def mine_q(sql, args=(), one=False, many=True):
        if "recover" in sql or "WHERE status='running' AND (worker=%s" in sql or "WHERE status='running' AND worker=%s" in sql:
            sql = sql.replace("WHERE status='running' AND", f"WHERE thread_id LIKE 't1330-{TAG}-%%' AND status='running' AND")
        return await real(sql, args, one, many)
    monkeypatch.setattr(durable, "_q", mine_q)

def test_sweeper_requeues_then_fourth_failure_is_failure_after_retry(monkeypatch):
    monkeypatch.delenv("AOS_BG_JOB_MAX_RETRIES", raising=False)
    async def go():
        before = dict(durable.COUNTERS)
        rid = await mk_run(WA, user=SEEDS["ua"], blocker=True, start_ckpt="c1"); old = await row(rid)
        for n in (1, 2, 3):                                                   # three automatic retries
            out = await durable.sweep(); assert rid in out["requeued"] and rid not in out["failed"]
            r = await row(rid)
            assert (r["status"], r["attempt"], r["worker"], r["failure_kind"]) == ("pending", n, None, "worker_died")
            assert (r["workspace_id"], r["user_id"], r["thread_id"], r["start_ckpt"]) == (old["workspace_id"], old["user_id"], old["thread_id"], "c1")   # same row, same owner
            await A.q("UPDATE lg_runs SET status='running', worker='wtest', started_at=now() - interval '10 minutes', heartbeat_at=now() - interval '10 minutes' WHERE run_id=%s", (rid,))   # claimed again, worker dies again
        await A.q("DELETE FROM lg_runs WHERE thread_id=%s AND worker='wblocker'", (old["thread_id"],))   # (the thread is only freed once no other run of it is pending / running)
        out = await durable.sweep()                                           # the 4th failure
        assert rid in out["failed"] and rid not in out["requeued"]
        r = await row(rid)
        assert (r["status"], r["failure_kind"], r["attempt"]) == ("error", "failure_after_retry", 3) and r["error"].startswith("failure_after_retry") and r["ended_at"]
        ev = [e["event"] for e in await A.q("SELECT event FROM lg_run_events WHERE run_id=%s ORDER BY seq", (rid,))]
        assert ev[-2:] == ["error", "end"]                                    # waiting clients return
        assert (await A.q("SELECT status FROM lg_threads WHERE thread_id=%s", (r["thread_id"],), one=True))["status"] == "error"
        assert durable.COUNTERS["retriable_failure"] - before["retriable_failure"] == 3 and durable.COUNTERS["failure_after_retry"] - before["failure_after_retry"] == 1
        assert (await durable.sweep())["failed"].count(rid) == 0              # nothing left to do: final
    run(go)

def test_sweeper_leaves_live_finished_and_errored_runs_alone():
    async def go():
        live = await mk_run(WA, hb_age_s=5, age_s=7200)                       # started long ago but the heartbeat is fresh: alive
        young = await mk_run(WA, age_s=30)                                    # just claimed, no heartbeat yet
        pend = await mk_run(WA, status="pending", age_s=7200)
        err = await mk_run(WA, status="error", age_s=7200, failure_kind="run_error")      # model / tool error: final, never retried
        ok = await mk_run(WA, status="success", age_s=7200); intr = await mk_run(WA, status="interrupted", age_s=7200)
        out = await durable.sweep(ws=WA)
        for r in (live, young, pend, err, ok, intr): assert r not in out["requeued"] + out["failed"]
        assert [(await row(r))["status"] for r in (live, young, pend, err, ok, intr)] == ["running", "running", "pending", "error", "success", "interrupted"]
        assert (await row(err))["attempt"] == 0
    run(go)

def test_sweeper_never_touches_another_workspace(monkeypatch):
    async def go():
        a = await mk_run(WA, user=SEEDS["ua"], blocker=True); b = await mk_run(WB, user=SEEDS["ub"], blocker=True)
        out = await durable.sweep(ws=WA)                                      # an admin pass for workspace A
        assert a in out["requeued"] and b not in out["requeued"] + out["failed"] and (await row(b))["status"] == "running"
        await A.q("UPDATE lg_runs SET status='running', worker='wtest', heartbeat_at=now() - interval '10 minutes' WHERE run_id=%s", (a,))
        tok = admin.USER.set(U(WB, SEEDS["ub"], "admin"))                     # even a sweep called inside workspace B's session (RLS) cannot reach A's run
        try: out = await durable.sweep()
        finally: admin.USER.reset(tok)
        assert b in out["requeued"] and a not in out["requeued"] + out["failed"] and (await row(a))["status"] == "running"
        rb = await row(b); assert (rb["workspace_id"], rb["user_id"], rb["status"]) == (WB, SEEDS["ub"], "pending")        # re-queued in ITS workspace, same owner
        out = await durable.sweep()                                           # the system pass handles what is left, each run keeps its own workspace
        ra = await row(a); assert a in out["requeued"] and (ra["workspace_id"], ra["user_id"], ra["attempt"]) == (WA, SEEDS["ua"], 2)
    run(go)

def test_sweeper_demo_workspace_and_zero_retries_fail_instead_of_retry(monkeypatch):
    async def go():
        d = await mk_run(WDEMO, attempt=0); a = await mk_run(WA, blocker=True)
        out = await durable.sweep(ws=WDEMO)
        assert d in out["failed"] and (await row(d))["status"] == "error" and (await row(d))["failure_kind"] == "worker_died"      # demo: off
        monkeypatch.setenv("AOS_BG_JOB_MAX_RETRIES", "0")
        out = await durable.sweep(ws=WA)
        assert a in out["failed"] and (await row(a))["failure_kind"] == "worker_died" and (await row(a))["attempt"] == 0            # retries switched off: old behaviour
        monkeypatch.setenv("AOS_BG_JOB_MAX_RETRIES", "x"); assert durable.max_retries() == 3
        assert durable.stream_kw() == {}
    run(go)

def test_boot_recovery_requeues_only_this_process_and_keeps_legacy_failure(monkeypatch):
    async def go():
        mine = await mk_run(WA, worker="wmine", hb_age_s=1, age_s=5, blocker=True); other = await mk_run(WA, worker="wother", hb_age_s=1, age_s=5)
        legacy = await mk_run(WA, worker="wmine", hb_age_s=1, age_s=5); await A.q("UPDATE lg_runs SET worker=NULL WHERE run_id=%s", (legacy,))
        spent = await mk_run(WA, worker="wmine", attempt=3, hb_age_s=1, age_s=5); demo = await mk_run(WDEMO, worker="wmine", hb_age_s=1, age_s=5)
        await durable.recover_boot("wmine")
        assert [(await row(r))["status"] for r in (mine, other, spent, demo)] == ["pending", "running", "error", "error"]
        assert (await row(mine))["attempt"] == 1 and (await row(spent))["failure_kind"] == "failure_after_retry" and (await row(demo))["failure_kind"] == "worker_died"
        # the legacy NULL-worker row and every other live run in the DB are also handled by the old rule; only the `other` row of a different named worker is untouched
        assert (await row(legacy))["status"] == "error" and (await row(other))["worker"] == "wother"
    run(go)

# ------------------------------------------------------------------ _run_task: heartbeat, timeout, errors, shutdown
@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(server, "WID", f"w{TAG}"); monkeypatch.setattr(durable, "HEARTBEAT_S", 0.05); monkeypatch.setattr(server, "DRAINING", False)
    server.TASKS.clear()

async def claimed(ws=WA, **kw):
    rid = await mk_run(ws, worker=server.WID, age_s=1, hb_age_s=1, user=SEEDS["ua"], **kw); return await row(rid)

def test_heartbeat_touches_the_row_and_a_lost_row_cancels_the_task(fast):
    async def go():
        r = await claimed(blocker=True); rid = str(r["run_id"]); h0 = r["heartbeat_at"]
        async def long(run_): await asyncio.sleep(30)
        server._execute = long
        t = asyncio.create_task(server._run_task(r)); await asyncio.sleep(0.4)
        assert (await row(rid))["heartbeat_at"] > h0                          # heartbeat running
        await A.q("UPDATE lg_runs SET status='pending', attempt=attempt+1, worker=NULL WHERE run_id=%s", (rid,))   # the sweeper re-queued it (we looked dead): we must stop
        with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(t, 3)
        assert (await row(rid))["status"] == "pending"                        # the cancelled zombie wrote nothing
    _with_execute(go)

def _with_execute(go):
    real = server._execute
    try: run(go)
    finally: server._execute = real

def test_timeout_cancels_with_reason_timeout(fast, monkeypatch):
    monkeypatch.setenv("AOS_BG_JOB_TIMEOUT_SECS", "1")
    async def go():
        r = await claimed(); rid = str(r["run_id"]); before = durable.COUNTERS["cancel"]
        async def long(run_): await asyncio.sleep(30)
        server._execute = long
        await server._run_task(r)
        x = await row(rid)
        assert (x["status"], x["cancel_reason"]) == ("timeout", "timeout") and "AOS_BG_JOB_TIMEOUT_SECS" in x["error"] and x["ended_at"]
        assert [e["event"] for e in await A.q("SELECT event FROM lg_run_events WHERE run_id=%s ORDER BY seq", (rid,))][-2:] == ["error", "end"]
        assert durable.COUNTERS["cancel"] == before + 1 and (await durable.sweep(ws=WA))["requeued"].count(rid) == 0     # a timeout is final
    _with_execute(go)

def test_a_crash_outside_execute_inner_is_a_final_error_never_retried(fast):
    async def go():
        r = await claimed(); rid = str(r["run_id"])
        async def bad(run_): raise ValueError("model exploded")
        server._execute = bad
        await server._run_task(r)
        x = await row(rid)
        assert (x["status"], x["failure_kind"]) == ("error", "run_error") and "ValueError" in x["error"]
        out = await durable.sweep(ws=WA); assert rid not in out["requeued"] + out["failed"]
        assert (await row(rid))["attempt"] == 0
    _with_execute(go)

def test_normal_run_counts_attempt_and_success(fast):
    async def go():
        r = await claimed(); rid = str(r["run_id"]); a0, s0 = durable.COUNTERS["attempts"], durable.COUNTERS["success"]; seen = {}
        async def ok(run_):
            seen["cur"] = durable.CUR.get(); await A.q("UPDATE lg_runs SET status='success', ended_at=now() WHERE run_id=%s", (str(run_["run_id"]),))
        server._execute = ok
        await server._run_task(r)
        assert seen["cur"] == {"run_id": rid, "ws": WA, "attempt": 0}
        assert durable.COUNTERS["attempts"] == a0 + 1 and durable.COUNTERS["success"] == s0 + 1
    _with_execute(go)

def test_sigterm_style_shutdown_releases_runs_and_nothing_stays_running(fast):
    async def go():
        r1 = await claimed(blocker=True); r2 = await claimed(blocker=True); fin = await claimed(); ab0 = durable.COUNTERS["abandoned"]
        stuck = asyncio.Event()
        async def fake(run_):
            if str(run_["run_id"]) == str(fin["run_id"]): await A.q("UPDATE lg_runs SET status='success', ended_at=now() WHERE run_id=%s", (str(run_["run_id"]),)); return
            stuck.set(); await asyncio.sleep(60)                               # a long run
        server._execute = fake
        for r in (r1, r2, fin): server.TASKS[str(r["run_id"])] = asyncio.create_task(server._run_task(r))
        await asyncio.sleep(0.3)
        rows = await server.shutdown(grace=0.3)                               # the SIGTERM path: in-flight runs get 0.3 s, the rest is released
        assert server.DRAINING is True and {str(x["run_id"]) for x in rows} == {str(r1["run_id"]), str(r2["run_id"])}
        for r in (r1, r2):
            x = await row(r["run_id"]); assert (x["status"], x["worker"], x["attempt"], x["failure_kind"]) == ("pending", None, 0, None)   # not lost, not failed, no retry consumed
        assert (await row(fin["run_id"]))["status"] == "success"
        assert not await A.q("SELECT 1 FROM lg_runs WHERE worker=%s AND status='running'", (server.WID,))
        assert durable.COUNTERS["abandoned"] == ab0 + 2 and all(t.done() for t in server.TASKS.values())
        for r in (r1, r2): await A.q("UPDATE lg_runs SET status='error' WHERE run_id=%s", (r["run_id"],))   # nobody else may pick them up
    _with_execute(go)

def test_queue_worker_claims_heartbeat_and_stops_claiming_while_draining(fast, monkeypatch):
    """The real queue loop (claim SQL included), restricted to this test's threads; a stale heartbeat of a dead worker is swept and the run is executed again."""
    async def go():
        real_q = A.q
        async def q_mine(sql, args=(), one=False, many=True):   # the claim may only take this test's runs (the dev DB is shared with other queue processes)
            if "FOR UPDATE SKIP LOCKED" in sql: sql = sql.replace("WHERE r.status='pending'", f"WHERE r.status='pending' AND r.thread_id LIKE 't1330-{TAG}-%%'")
            return await real_q(sql, args, one, many)
        monkeypatch.setattr(A, "q", q_mine)
        await A.q("INSERT INTO worker_heartbeats(name,pid,host,started,at,max_conc) VALUES(%s,1,'t',now(),now(),2) ON CONFLICT(name) DO UPDATE SET at=now()", (server.WID,))
        tid = f"t1330-{TAG}-q1"
        rid = await mk_run(WA, status="pending", age_s=5, user=SEEDS["ua"], thread=tid); await A.q("UPDATE lg_runs SET run_at=now() WHERE run_id=%s", (rid,))
        calls = []
        async def fake(run_):
            calls.append((str(run_["run_id"]), run_["attempt"], run_["start_ckpt"])); n = len(calls)
            await A.q("UPDATE lg_runs SET start_ckpt=coalesce(start_ckpt,'c1') WHERE run_id=%s", (str(run_["run_id"]),))
            if n == 1: await asyncio.sleep(60)                                 # first execution: the worker "dies" while running
            await A.q("UPDATE lg_runs SET status='success', ended_at=now() WHERE run_id=%s", (str(run_["run_id"]),))
        server._execute = fake
        loop = asyncio.create_task(server.queue_worker())
        for _ in range(60):
            await asyncio.sleep(0.1)
            if calls: break
        assert calls == [(rid, 0, None)]
        r = await row(rid); assert r["status"] == "running" and r["worker"] == server.WID and r["heartbeat_at"] is not None       # claim stamped the heartbeat
        # worker dies hard: tasks and heartbeat vanish, the row stays `running`
        loop.cancel(); [t.cancel() for t in server.TASKS.values()]; await asyncio.sleep(0.2); server.TASKS.clear()
        assert (await row(rid))["status"] == "running"
        await A.q("UPDATE lg_runs SET heartbeat_at=now() - interval '5 minutes' WHERE run_id=%s", (rid,))
        assert rid in (await durable.sweep(ws=WA))["requeued"]
        loop = asyncio.create_task(server.queue_worker())
        for _ in range(60):
            await asyncio.sleep(0.1)
            if len(calls) > 1 and (await row(rid))["status"] == "success": break
        assert calls[1] == (rid, 1, "c1")                                      # second execution: attempt 1, knows the run already reached the graph (-> resume mode)
        assert (await row(rid))["status"] == "success"
        # draining: a new run is NOT claimed
        server.DRAINING = True
        rid2 = await mk_run(WA, status="pending", age_s=5, thread=f"t1330-{TAG}-q2"); await A.q("UPDATE lg_runs SET run_at=now() WHERE run_id=%s", (rid2,))
        await asyncio.sleep(2.5); assert (await row(rid2))["status"] == "pending" and len(calls) == 2
        loop.cancel()
    _with_execute(go)

# ------------------------------------------------------------------ admin stats
def test_stats_endpoint_admin_only_and_workspace_scoped(fast):
    async def go():
        await mk_run(WA, status="error", failure_kind="run_error"); await mk_run(WB, status="error", failure_kind="failure_after_retry"); await mk_run(WB, status="success")
        async def as_(user):
            tok = admin.USER.set(user)
            try: return await durable.runs_stats()
            finally: admin.USER.reset(tok)
        out = await as_(U(WA, SEEDS["ua"], "admin"))
        assert set(out["counters"]) >= {"attempts", "success", "cancel", "retriable_failure", "failure_after_retry", "abandoned"}
        assert out["workspace"]["by_status"] == {"error": 1} and out["workspace"]["failure_kinds"] == {"run_error": 1}       # workspace B's runs are not in it
        assert out["limits"]["max_retries"] == 3 and out["limits"]["heartbeat_s"] == 0.05
        for bad in (U(WA, SEEDS["ua"], "editor"), U(WA, SEEDS["ua"], "viewer"), {**U(WA, SEEDS["ua"], "admin"), "demo": True}, U(WDEMO, SEEDS["ua"], "admin")):
            with pytest.raises(HTTPException) as e: await as_(bad)
            assert e.value.status_code == 403
        with pytest.raises(HTTPException) as e: await durable.runs_stats()    # no session at all
        assert e.value.status_code == 403
    run(go)
