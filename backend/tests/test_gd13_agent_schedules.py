"""GĐ13.28 an agent creates its own schedule ("nhắc tôi mỗi thứ Hai 8 giờ"): tools lich_tao / lich_danh_sach / lich_xoa / lich_sua (app/agent_schedules.py, migration 0226).
Fake LLM only ($0); ONE recorded real Gemini tool call is replayed (tests/data/gd13_13_28_call.json; recording needs AOS_GD13_28_RECORD=<600 key file>).
Trust first: external-source / mail / API-key / schedule (system) / demo / viewer callers cannot create or edit; creation waits for the approval interrupt; user A never lists or
deletes user B's schedules; another workspace sees nothing. Frozen clock: agent_schedules.now_utc (Mon 2026-10-26 10:00 UTC = 17:00 ICT)."""
import asyncio, datetime as dt, json, os, sys, uuid
from pathlib import Path
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE / ".." / "app")); sys.path.insert(0, str(HERE / ".." / "overlay"))
from test_gd13_agent_fields import harness, user, as_user, fake_models  # noqa: E402,F401  (first: imports deepagent_api before the other app modules)
import deepagent_api as A, admin, gateway, evals, run_trust  # noqa: E402
import agent_schedules as S  # noqa: E402

U = dt.timezone.utc
NOW = dt.datetime(2026, 10, 26, 10, 0, 7, tzinfo=U)
REC = HERE / "data" / "gd13_13_28_call.json"
SUF = uuid.uuid4().hex[:6]
TOOLS = ["local:lich_tao", "local:lich_danh_sach", "local:lich_xoa", "local:lich_sua"]

@pytest.fixture(autouse=True)
def frozen(monkeypatch): monkeypatch.setattr(S, "now_utc", lambda: NOW)

# ------------------------------------------------------------------ 1. pure
def test_parse_at_vietnam_time_rounds_up_and_limits():
    ict = "Asia/Ho_Chi_Minh"
    assert S.parse_at("2026-10-27T09:00", ict, NOW) == dt.datetime(2026, 10, 27, 2, 0, tzinfo=U)               # "9 giờ sáng mai" is read in Vietnam time
    assert S.parse_at("2026-10-27T09:00:20", ict, NOW) == dt.datetime(2026, 10, 27, 2, 1, tzinfo=U)            # rounded UP to the next minute
    assert S.parse_at("2026-10-27T09:00:00+00:00", ict, NOW) == dt.datetime(2026, 10, 27, 9, 0, tzinfo=U)       # an explicit zone wins
    assert S.parse_at("2026-10-26T10:01:00Z", ict, NOW) == dt.datetime(2026, 10, 26, 10, 1, tzinfo=U)           # 1 minute ahead is the minimum
    for bad in ("2026-10-26T10:00:00Z", "2026-10-26T09:00:00Z", "2020-01-01", "2027-10-27T00:00:00Z", "mai", ""):
        with pytest.raises(ValueError): S.parse_at(bad, ict, NOW)

def test_recurring_minimum_interval_15_minutes():
    S.check_interval("0 8 * * 1", "UTC", NOW); S.check_interval("*/15 * * * *", "UTC", NOW); S.check_interval("0,30 9 * * *", "UTC", NOW)
    for bad in ("* * * * *", "*/5 * * * *", "0,10 9 * * *", "0 0 31 2 *"):
        with pytest.raises(ValueError): S.check_interval(bad, "UTC", NOW)

def test_confirmation_lists_next_three_run_times_in_vietnam_time():
    txt = S.describe({"name": "lich_tao", "args": {"message": "Họp giao ban", "cron": "0 8 * * 1"}})
    assert all(d in txt for d in ("02/11/2026 08:00", "09/11/2026 08:00", "16/11/2026 08:00")) and "Họp giao ban" in txt and "Asia/Ho_Chi_Minh" in txt
    assert "một lần" in S.describe({"name": "lich_tao", "args": {"message": "x", "at": "2026-10-27T09:00"}}) and "27/10/2026 09:00" in S.describe({"name": "lich_tao", "args": {"message": "x", "at": "2026-10-27T09:00"}})
    assert "invalid" in S.describe({"name": "lich_tao", "args": {"message": "x", "cron": "bad"}})
    assert set(S.interrupt_cfg({"lich_tao", "lich_sua", "lich_xoa", "other"})) == {"lich_tao", "lich_sua"}

def test_one_shot_due_at_right_vn_time_runs_once():
    ro = S.parse_at("2026-10-27T09:00", "Asia/Ho_Chi_Minh", NOW)
    at = lambda h, m: dt.datetime(2026, 10, 27, h, m, 5, tzinfo=U)
    assert not evals.is_due("@once", "Asia/Ho_Chi_Minh", ro, at(1, 59)) and evals.is_due("@once", "Asia/Ho_Chi_Minh", ro, at(2, 0))
    assert not evals.is_due("@once", "Asia/Ho_Chi_Minh", ro, at(2, 5), at(2, 0))                                   # already ran
    assert evals.is_due("0 8 * * 1", "Asia/Ho_Chi_Minh", None, dt.datetime(2026, 11, 2, 1, 0, 5, tzinfo=U))      # Monday 08:00 ICT

# ------------------------------------------------------------------ 2. tools against Postgres
def _cfg(tid="t1328-" + SUF): return {"configurable": {"thread_id": tid}}
def _rows(agent_id, extra=""): return A.q("SELECT * FROM agent_crons WHERE agent_id=%s " + extra + " ORDER BY id", (agent_id,))

async def _agent(name):
    aid, _ = await A.create_agent(A.AgentCfg(name=f"{name} {SUF}", tools=TOOLS, planning=False)); return aid

def _call(aid, name, args, tid=None): return S.tools(aid)["local:" + name].ainvoke(args, _cfg(tid) if tid else _cfg())

def test_create_list_update_delete_and_isolation_between_users():
    async def go(W):
        ua, ub = user(W["a"], "editor"), {**user(W["a"], "editor"), "id": 987138, "username": "t137b"}
        with as_user(ua):
            aid = await _agent("sch1")
            out = json.loads(await _call(aid, "lich_tao", {"message": "Nhắc tôi họp giao ban", "cron": "0 8 * * 1"}, "t1328-thread-a"))
            assert out["ok"] and out["cron"] == "0 8 * * 1" and out["timezone"] == "Asia/Ho_Chi_Minh" and out["next_runs"][0] == "2026-11-02T01:00:00+00:00"
            r = (await _rows(aid))[0]
            assert (r["source"], r["owner_user_id"], r["created_by"], r["thread_id"], r["new_thread"], r["run_once_at"], r["paused"]) == ("agent", ua["id"], ua["id"], "t1328-thread-a", False, None, False)
            one = json.loads(await _call(aid, "lich_tao", {"message": "Gọi khách", "at": "2026-10-27T09:00", "new_thread": True}))
            assert one["cron"] == "@once" and one["next_runs"] == ["2026-10-27T02:00:00+00:00"]
            r2 = (await _rows(aid, "AND id=%s" % one["id"]))[0]; assert r2["run_once_at"] == dt.datetime(2026, 10, 27, 2, 0, tzinfo=U) and r2["new_thread"] and r2["thread_id"] is None
            listing = json.loads(await _call(aid, "lich_danh_sach", {})); assert [x["id"] for x in listing] == [out["id"], one["id"]]
            for bad in ({"message": "x"}, {"message": "x", "cron": "0 8 * * 1", "at": "2026-10-27T09:00"}, {"message": "x", "cron": "* * * * *"}, {"message": "x", "cron": "zz"},
                        {"message": "x", "at": "2025-01-01T00:00"}, {"message": "", "cron": "0 8 * * 1"}):
                assert (await _call(aid, "lich_tao", bad)).startswith("Không tạo được"), bad
            assert len(await _rows(aid)) == 2
            e = json.loads(await _call(aid, "lich_sua", {"id": out["id"], "cron": "30 9 * * 2", "message": "Họp lúc 9h30"})); assert e["cron"] == "30 9 * * 2"
            p = (await _rows(aid, "AND id=%s" % out["id"]))[0]; assert (p["cron"], p["message"], p["paused"]) == ("30 9 * * 2", "Họp lúc 9h30", False)
            await _call(aid, "lich_sua", {"id": out["id"], "paused": True}); assert (await _rows(aid, "AND id=%s" % out["id"]))[0]["paused"]
            assert json.loads(await _call(aid, "lich_danh_sach", {}))[0]["paused"] is True
        with as_user(ub):                                                         # user B: the same agent, nothing of A's is visible or touchable
            assert "no schedules" in await _call(aid, "lich_danh_sach", {})
            assert "not found" in await _call(aid, "lich_xoa", {"id": out["id"]})
            assert (await _call(aid, "lich_sua", {"id": out["id"], "message": "hack"})).startswith("Không sửa được")
            assert len(await _rows(aid)) == 2 and (await _rows(aid, "AND id=%s" % out["id"]))[0]["message"] == "Họp lúc 9h30"
            mine = json.loads(await _call(aid, "lich_tao", {"message": "của B", "cron": "0 7 * * *"})); assert mine["ok"]
        with as_user(ua):
            assert [x["id"] for x in json.loads(await _call(aid, "lich_danh_sach", {}))] == [out["id"], one["id"]]
            assert "Đã xóa" in await _call(aid, "lich_xoa", {"id": one["id"]}) and len(await _rows(aid)) == 2
        with as_user(user(W["b"], "editor")):                                      # another workspace: RLS hides the agent's rows
            assert "no schedules" in await _call(aid, "lich_danh_sach", {})
            assert "not found" in await _call(aid, "lich_xoa", {"id": out["id"]})
    harness(go)

def test_limits_per_agent_and_per_user(monkeypatch):
    async def go(W):
        with as_user(user(W["a"], "editor")):
            aid = await _agent("sch-lim")
            monkeypatch.setenv("AOS_MAX_USER_SCHEDULES", "100")
            for i in range(20): assert json.loads(await _call(aid, "lich_tao", {"message": f"m{i}", "cron": f"{i % 60} 8 * * 1"}))["ok"]
            assert "20 lịch" in await _call(aid, "lich_tao", {"message": "thứ 21", "cron": "0 9 * * 1"})          # the 21st is refused
            assert len(await _rows(aid)) == 20
            aid2 = await _agent("sch-lim2"); monkeypatch.setenv("AOS_MAX_USER_SCHEDULES", "20")
            assert "AOS_MAX_USER_SCHEDULES" in await _call(aid2, "lich_tao", {"message": "other agent, same person", "cron": "0 9 * * 1"})
            await A.q("UPDATE agent_crons SET enabled=false WHERE agent_id=%s AND message='m0'", (aid,))            # a finished / disabled one frees a slot
            assert json.loads(await _call(aid, "lich_tao", {"message": "thứ 21 lần hai", "cron": "0 9 * * 1"}))["ok"]
    harness(go)

def test_untrusted_callers_cannot_create_or_edit():
    async def go(W):
        with as_user(user(W["a"], "editor")):
            aid = await _agent("sch-trust"); ok = json.loads(await _call(aid, "lich_tao", {"message": "hợp lệ", "cron": "0 8 * * 1"}))["ok"]; assert ok
        args = {"message": "evil", "cron": "0 9 * * 1"}; edit = {"id": (await _rows(aid))[0]["id"], "message": "evil"}; delete = {"id": edit["id"]}
        def attempts():
            return [("lich_tao", args), ("lich_sua", edit), ("lich_xoa", delete)]
        async def refused(note):
            for n, a in attempts():
                out = await _call(aid, n, a); assert out.startswith("Từ chối"), (note, n, out)
            rows = await _rows(aid); assert len(rows) == 1 and rows[0]["message"] == "hợp lệ", note
        with as_user(user(W["a"], "editor")):
            with run_trust.scope(True): await refused("external")                    # stranger / public channel
            with run_trust.scope(True, mail=True): await refused("mail")             # run started by an incoming mail
        for note, u in (("api key", {**user(W["a"], "admin"), "auth": "api_key:abcd1234"}), ("channel", {**user(W["a"], "editor"), "auth": "channel"}),
                        ("schedule run", {**user(W["a"], "editor"), "auth": "run"}), ("viewer", user(W["a"], "viewer")), ("demo", user(W["a"], "editor", demo=True))):
            with as_user(u): await refused(note)
        with as_user(user(W["a"], "editor")): assert json.loads(await _call(aid, "lich_tao", {"message": "control", "cron": "0 10 * * 1"}))["ok"]     # control: a person may
        t = admin.USER.set(None)                                                      # a schedule-triggered run has no person at all (system caller)
        try:
            for n, a in attempts(): assert (await _call(aid, n, a)).startswith("Từ chối"), n
        finally: admin.USER.reset(t)
    harness(go)

# ------------------------------------------------------------------ 3. through a real agent: the approval interrupt (fake model replays the tool call)
class Replay(GenericFakeChatModel):
    def bind_tools(self, tools, **kw): return self

def test_creation_waits_for_approval_and_card_shows_next_runs(fake_models, monkeypatch):
    async def go(W):
        m = Replay(messages=iter([AIMessage(content="", tool_calls=[{"name": "lich_tao", "args": {"message": "Họp giao ban", "cron": "0 8 * * 1"}, "id": "c1"}]), AIMessage(content="Đã đặt lịch.")]))
        monkeypatch.setattr(gateway, "_model", lambda *a, **k: m)
        with as_user(user(W["a"], "editor")):
            aid, v = await A.create_agent(A.AgentCfg(name=f"sch-run {SUF}", tools=TOOLS, planning=False, filesystem=False))
            first = [e async for e in A.run_agent(aid, v, "t1328-run", "nhắc tôi mỗi thứ Hai 8 giờ")]
            intr = [e for e in first if e["type"] == "interrupt"]
            assert intr and intr[0]["approval_id"], first[-1]
            card = json.dumps(intr[0]["requests"], ensure_ascii=False)
            assert "lich_tao" in card and "02/11/2026" in card and "next runs" in card, card[:400]          # the person sees the next run times before approving (real clock: next Monday)
            assert await _rows(aid) == []                                                                       # nothing created before the decision
            after = [e async for e in A.run_agent(aid, v, "t1328-run", "", resume={"decisions": [{"type": "approve"}]})]
            assert not after[-1].get("error"), after[-1]
            r = await _rows(aid); assert len(r) == 1 and r[0]["cron"] == "0 8 * * 1" and r[0]["thread_id"] == "t1328-run" and r[0]["source"] == "agent"
    harness(go)

# ------------------------------------------------------------------ 4. the runner: one-shot runs once and disables itself; AOS_SCHEDULES_DISABLED=1; thread continuation
def test_runner_one_shot_disables_itself_global_switch_and_thread(monkeypatch):
    import studio
    async def go(W):
        with as_user(user(W["a"], "editor")):
            aid = await _agent("sch-runner"); ran = []
            row = lambda msg, ro, th: A.q("INSERT INTO agent_crons(agent_id,name,cron,message,new_thread,timezone,run_once_at,source,owner_user_id,thread_id) VALUES(%s,%s,'@once',%s,%s,'Asia/Ho_Chi_Minh',%s,'agent',1,%s) RETURNING id",
                                          (aid, msg, msg, th is None, ro, th), one=True)
            due = dt.datetime.now(U) - dt.timedelta(minutes=2)
            r1 = await row("due now", due, "t1328-keep"); r2 = await row("later", dt.datetime.now(U) + dt.timedelta(days=2), None)
            orig = A.q
            async def q(sql, args=(), one=False):
                out = await orig(sql, args, one=one)
                return [x for x in out if x["agent_id"] == aid] if sql.startswith("SELECT * FROM agent_crons WHERE enabled") else out    # only this test's rows (the dev DB has others)
            async def fake_run(c): ran.append((c["id"], c.get("thread_id"), c["new_thread"]))
            monkeypatch.setattr(studio.A, "q", q); monkeypatch.setattr(studio, "_run_cron", fake_run)
            monkeypatch.setenv("AOS_SCHEDULES_DISABLED", "1"); await studio.run_crons(); assert ran == []            # global off switch
            monkeypatch.delenv("AOS_SCHEDULES_DISABLED"); await studio.run_crons(); await asyncio.sleep(0.2)
            assert ran == [(r1["id"], "t1328-keep", False)]
            assert (await orig("SELECT enabled FROM agent_crons WHERE id=%s", (r1["id"],), one=True))["enabled"] is False     # the one-shot disabled itself
            await studio.run_crons(); await asyncio.sleep(0.2); assert len(ran) == 1                                      # never runs twice
            assert (await orig("SELECT enabled FROM agent_crons WHERE id=%s", (r2["id"],), one=True))["enabled"] is True
            await orig("UPDATE agent_crons SET enabled=true, last_run=NULL, paused=true, run_once_at=%s WHERE id=%s", (due, r1["id"]))
            await studio.run_crons(); assert len(ran) == 1                                                                 # a paused one does not run
    harness(go)

def test_cron_run_continues_the_thread_it_was_made_in(monkeypatch):
    import studio
    seen = []
    async def run_agent(aid, v, tid, msg, tags=None, **k):
        seen.append(tid); yield {"type": "done", "trace_id": None}
    async def cur_version(aid): return 1
    async def q(*a, **k): return None
    monkeypatch.setattr(studio.A, "run_agent", run_agent); monkeypatch.setattr(studio.A, "cur_version", cur_version); monkeypatch.setattr(studio.A, "q", q)
    base = {"id": 7, "agent_id": 1, "message": "hi", "name": "n"}
    asyncio.run(studio._run_cron({**base, "new_thread": False, "thread_id": "chat-1"})); asyncio.run(studio._run_cron({**base, "new_thread": False, "thread_id": None}))
    asyncio.run(studio._run_cron({**base, "new_thread": True, "thread_id": "chat-1"}))
    assert seen[0] == "chat-1" and seen[1] == "cron7" and seen[2].startswith("cron7-")

def test_chat_schedules_row_carries_the_created_by_agent_tag():
    import chat_gd5b
    c = {"id": 1, "agent_id": 2, "name": "n", "cron": "0 8 * * 1", "message": "m", "enabled": True, "locked_reason": None, "last_run": None, "last_status": None, "created_by": 5, "source": "agent", "paused": False}
    out = chat_gd5b._row_out(c, {"id": 5, "role": "editor"}); assert out["source"] == "agent" and out["can_edit"] and out["paused"] is False
    assert chat_gd5b._row_out({k: v for k, v in c.items() if k not in ("source", "paused")}, {"id": 5, "role": "editor"})["source"] == "ui"

# ------------------------------------------------------------------ 5. the recorded real Gemini tool call
def test_recorded_gemini_tool_call_replays_through_the_approval_gate(fake_models, monkeypatch):
    rec = json.loads(REC.read_text(encoding="utf-8"))
    assert rec["model"].startswith("gemini") and rec["cost_usd"] < 0.05 and rec["tool_call"]["name"] == "lich_tao"
    args = rec["tool_call"]["args"]; f = args["cron"].split(); assert [f[0], f[1], f[4]] == ["0", "8", "1"] and f[2:4] == ["*", "*"]       # what the real model understood: Monday 08:00
    async def go(W):
        m = Replay(messages=iter([AIMessage(content="", tool_calls=[{"name": "lich_tao", "args": args, "id": "c1"}]), AIMessage(content="ok")]))
        monkeypatch.setattr(gateway, "_model", lambda *a, **k: m)
        with as_user(user(W["a"], "editor")):
            aid, v = await A.create_agent(A.AgentCfg(name=f"sch-rec {SUF}", tools=TOOLS, planning=False, filesystem=False))
            first = [e async for e in A.run_agent(aid, v, "t1328-rec", rec["prompt"])]
            assert any(e["type"] == "interrupt" and e["approval_id"] for e in first) and await _rows(aid) == []
            after = [e async for e in A.run_agent(aid, v, "t1328-rec", "", resume={"decisions": [{"type": "approve"}]})]
            assert not after[-1].get("error"); r = await _rows(aid)
            assert len(r) == 1 and r[0]["cron"] == args["cron"] and r[0]["timezone"] == "Asia/Ho_Chi_Minh"
    harness(go)

@pytest.mark.skipif(not os.environ.get("AOS_GD13_28_RECORD"), reason="real Gemini recording (<= $0.05); AOS_GD13_28_RECORD=<path of the 600 key file>")
def test_record_real_gemini_tool_call():
    import time
    with open(os.path.expanduser(os.environ["AOS_GD13_28_RECORD"])) as f: os.environ["GOOGLE_API_KEY"] = f.read().strip()     # key stays in this process, never printed
    prompt = "nhắc tôi mỗi thứ Hai 8 giờ họp giao ban"; model = "gemini-3.8-flash"
    async def go(W):
        base = float((await A.q("SELECT coalesce(sum(cost_usd),0) c FROM gateway_usage WHERE NOT (coalesce(model,'') LIKE 'gpt-%%' OR coalesce(model,'') LIKE 'o3%%' OR coalesce(model,'') LIKE 'o4%%')", one=True))["c"])
        gateway.CAPS["gemini"] = base + 0.05; gateway.CAPS["openai"] = 0.0          # hard stop: at most $0.05 on top of everything spent before
        with as_user(user(W["a"], "editor")):
            aid, v = await A.create_agent(A.AgentCfg(name=f"sch-real {SUF}", model=model, tools=["local:lich_tao"], planning=False, filesystem=False, fallback_models=[], temperature=0,
                                                     system_prompt="Bạn là trợ lý. Khi người dùng nhờ nhắc việc định kỳ hoặc một lần, hãy dùng công cụ lich_tao."))
            t0 = time.time(); evs = [e async for e in A.run_agent(aid, v, "t1328-real" + SUF, prompt)]
            intr = [e for e in evs if e["type"] == "interrupt" and e["approval_id"]]; done = evs[-1]
            assert intr and not done.get("error"), {k: done.get(k) for k in ("error", "reply")}
            req = intr[0]["requests"]; req = req[0] if isinstance(req, list) else req
            ac = (req.get("action_requests") or [req])[0]
            REC.write_text(json.dumps({"recorded": "2026-10-26", "model": model, "prompt": prompt, "tool_call": {"name": ac.get("name"), "args": ac.get("args") or ac.get("arguments")},
                                       "cost_usd": done["cost_usd"], "tokens": done["tokens"], "latency_s": round(time.time() - t0, 2)}, ensure_ascii=False, indent=1), encoding="utf-8")
    harness(go)
