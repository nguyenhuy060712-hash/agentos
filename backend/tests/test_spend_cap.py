"""Spend cap reached -> a clear reason instead of a silently failed run (21/10/2026).
- gateway.cap_message / SpendCapReached (was `raise None` when every model of the chain was over its provider cap)
- server._spend_guard: provider cap or agent daily budget reached -> HTTP 402 with the reason before the run is queued
Local server :8020, fake LLM => $0, no network."""
import asyncio, os, sys, uuid
import httpx, psycopg, pytest
from test_fixes import B, a  # noqa: F401  (fixture)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")

def test_cap_message_and_no_bare_raise():
    import gateway as gw
    caps, spent = dict(gw.CAPS), dict(gw.SPENT)
    try:
        gw.CAPS["gemini"] = 2.0; gw.SPENT["gemini"] = 0.5
        assert gw.cap_message(["gemini-3.8-flash"]) is None
        gw.SPENT["gemini"] = 2.004
        m = gw.cap_message(["gemini-3.8-flash", "gemini-3.6-flash"])
        assert m.startswith("Đã chạm trần chi phí Gemini") and "$2.00 / trần $2.00" in m
        with pytest.raises(gw.SpendCapReached, match="trần chi phí"):   # nothing is sent: no client needed
            asyncio.run(gw.generate(None, "x", ["gemini-3.8-flash"]))
    finally:
        gw.CAPS.clear(); gw.CAPS.update(caps); gw.SPENT.clear(); gw.SPENT.update(spent)

def test_agent_daily_budget_reached_is_402_before_the_run(a):
    aid = a.post("/api/agents", json={"name": "cap-test " + uuid.uuid4().hex[:4], "model": "gemini-3.8-flash", "tools": [], "planning": False,
                                      "filesystem": False, "budget_usd_day": 0.01}).json()["id"]
    tid = a.post("/api/v1/threads", json={"metadata": {"source": "chat", "graph_id": f"agent-{aid}"}}).json()["thread_id"]
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute("INSERT INTO gateway_usage(agent_id,model,input_tokens,output_tokens,cost_usd,status) VALUES(%s,'gemini-3.8-flash',0,0,0.02,'cap_test')", (aid,))
    try:
        r = a.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": f"agent-{aid}", "input": {"messages": [{"type": "human", "content": "xin chào"}]}})
        assert r.status_code == 402 and "ngân sách ngày" in r.json()["detail"]["message"] and r.json()["detail"]["scope"] == "agent", r.text   # GĐ8.P: detail object
        with psycopg.connect(PG) as c:   # rejected before queueing: no run row, no model call
            assert c.execute("SELECT count(*) FROM lg_runs WHERE thread_id=%s", (tid,)).fetchone()[0] == 0
        r = a.post(f"/api/v1/threads/{tid}/runs/stream", json={"assistant_id": f"agent-{aid}", "input": {"messages": [{"type": "human", "content": "xin chào"}]}})
        assert r.status_code == 402   # the chat UI uses the stream endpoint: same 402 -> "Đã chạm trần chi phí" banner
    finally:
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute("DELETE FROM gateway_usage WHERE status='cap_test' AND agent_id=%s", (aid,))
            # 8.P: hard-delete the throwaway agent (soft delete left "cap-test …" rows + their threads in the local sidebar)
            c.execute("DELETE FROM lg_runs WHERE thread_id=%s", (tid,)); c.execute("DELETE FROM lg_threads WHERE thread_id=%s", (tid,))
            c.execute("DELETE FROM agents WHERE id=%s AND name LIKE 'cap-test %%'", (aid,))

def test_under_budget_still_runs(a):
    tid = a.post("/api/v1/threads", json={"metadata": {"source": "chat", "graph_id": "agent-2"}}).json()["thread_id"]
    r = a.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": "agent-2", "input": {"messages": [{"type": "human", "content": "hi"}]}})
    assert r.status_code == 200 and r.json()["messages"][-1]["type"] == "ai"
