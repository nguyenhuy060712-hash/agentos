"""GĐ13 12.33 Detection v2 (reduced): categories, severity by rule, feedback of any key, inefficiency / truncated tool result / guard violation, idle-thread gating, masked titles,
from_trace, resume of failed analysis steps, migration 0204. Synthetic tr_runs rows in throwaway workspaces, fake LLM / patched model call, $0.
In-process tests run the detector on a private pool under the app's RLS role (as the worker does); HTTP tests need AOS_TEST_BASE (default :8020) running the new code."""
import asyncio, datetime as dt, importlib.util, os, sys, uuid
from pathlib import Path
import httpx, psycopg, pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")
B = os.environ.get("AOS_TEST_BASE", "http://127.0.0.1:8020"); ADMIN = ("moza", "phase0-local")
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
PW = "phase0-gd1333-local"; SUF = uuid.uuid4().hex[:6]
AG = 700000 + int(SUF, 16) % 90000          # a made-up agent id (no FK; _agent_names falls back to the id)
NOW = lambda: dt.datetime.now(dt.timezone.utc)   # noqa: E731
def pg(): return psycopg.connect(PG, autocommit=True)

def run(c, pid, *, name="chat", rtype="chain", status="success", error=None, parent=None, trace=None, agent=AG, user=None, lat=100, tok=100, outputs=None, md=None, ago=1, thread=None, inputs=None):
    """One synthetic tr_runs row (start `ago` minutes ago, end = start + lat ms). Returns (run id, trace id)."""
    rid = str(uuid.uuid4()); trace = trace or rid; st = NOW() - dt.timedelta(minutes=ago); m = {**({"agent_id": str(agent)} if agent else {}), **(md or {})}
    c.execute("""INSERT INTO tr_runs(id,trace_id,parent_id,project_id,name,run_type,start_time,end_time,latency_ms,status,error,inputs,outputs,metadata,total_tokens,user_id,thread_id)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
              (rid, trace, parent, pid, name, rtype, st, st + dt.timedelta(milliseconds=lat), lat, status, error, Jsonb(inputs or {"message": "hello"}), Jsonb(outputs) if outputs is not None else None,
               Jsonb(m), tok, user, thread))
    return rid, trace

def tool_trace(c, pid, tool, *, status="success", error=None, outputs=None, user=None, agent=AG):
    """A root run with ONE tool child. Returns the root id."""
    root, tid = run(c, pid, agent=agent, user=user)
    run(c, pid, name=tool, rtype="tool", status=status, error=error, parent=root, trace=tid, outputs=outputs, agent=None, user=user)
    return root

def fb(c, pid, rid, key, score, *, source="human", evaluator=None, md=None):
    t = c.execute("SELECT trace_id FROM tr_runs WHERE id=%s", (rid,)).fetchone()[0]
    c.execute("INSERT INTO feedback(run_id,trace_id,project_id,key,score,source,evaluator_id,metadata) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)", (rid, t, pid, key, score, source, evaluator, Jsonb(md) if md else None))

def mkws(c, tag):
    ws = c.execute("INSERT INTO workspaces(name) VALUES(%s) RETURNING id", (f"e33-{tag}-{SUF}",)).fetchone()[0]
    c.execute("UPDATE workspaces SET engine_enabled=true WHERE id=%s", (ws,))
    pid = c.execute("INSERT INTO projects(name, workspace_id) VALUES(%s,%s) RETURNING id", (f"e33-{tag}-{SUF}", ws)).fetchone()[0]
    return ws, pid

def inproc(fn):
    """Run `await fn(engine, as_ws)` on a private pool under the app's RLS role."""
    import deepagent_api as A, admin, engine
    async def go():
        old = (A.S.get("pool"), A.S.get("rls"))
        async with AsyncConnectionPool(PG, min_size=1, max_size=4, open=False, kwargs={"autocommit": True, "row_factory": dict_row}) as pool:
            await pool.open(); A.S["pool"] = pool; A.S["rls"] = admin.rls_ctx
            try: return await fn(engine, engine.as_ws)
            finally: A.S["pool"], A.S["rls"] = old
    return asyncio.run(go())

@pytest.fixture(scope="module")
def world():
    a = httpx.Client(base_url=B, auth=ADMIN, timeout=90); w = {"a": a}
    with pg() as c:
        w["w1"], w["p1"] = mkws(c, "a"); w["w2"], w["p2"] = mkws(c, "b")
        w["pb"] = c.execute("INSERT INTO projects(name, workspace_id) VALUES(%s,%s) RETURNING id", (f"e33-a2-{SUF}", w["w1"])).fetchone()[0]
    for k, role, ws, demo in (("ed", "editor", w["w1"], False), ("vw", "viewer", w["w1"], False), ("dm", "viewer", w["w1"], True), ("ed2", "editor", w["w2"], False)):
        r = a.post("/api/admin/users", json={"username": f"e33_{k}_{SUF}", "password": PW, "role": role, "workspace_id": ws, "demo": demo}); assert r.status_code == 200, r.text
        w[k] = httpx.Client(base_url=B, auth=(f"e33_{k}_{SUF}", PW), timeout=90)
    yield w
    for k in ("ed", "vw", "dm", "ed2", "a"): w[k].close()
    with pg() as c:
        ws = [r[0] for r in c.execute("SELECT id FROM workspaces WHERE name LIKE %s", (f"e33-%-{SUF}",)).fetchall()]
        pids = [r[0] for r in c.execute("SELECT id FROM projects WHERE workspace_id = ANY(%s)", (ws,)).fetchall()]
        c.execute("DELETE FROM feedback WHERE project_id = ANY(%s)", (pids,)); c.execute("DELETE FROM tr_runs WHERE project_id = ANY(%s)", (pids,))
        c.execute("DELETE FROM feedback_configs WHERE workspace_id = ANY(%s)", (ws,)); c.execute("DELETE FROM projects WHERE workspace_id = ANY(%s)", (ws,))
        c.execute("DELETE FROM users WHERE username LIKE %s", (f"e33\\_%\\_{SUF}",)); c.execute("DELETE FROM workspaces WHERE id = ANY(%s)", (ws,))

# ------------------------------------------------------------------ fixture traces: every category once
@pytest.fixture(scope="module")
def seeded(world):
    w = world; p = w["p1"]; ws = w["w1"]
    with pg() as c:
        c.execute("INSERT INTO feedback_configs(workspace_id,key,type,min,max) VALUES(%s,'rating5','continuous',1,5)", (ws,))
        for _ in range(60): run(c, p, user=11)                                   # healthy baseline of the agent (also its traffic: the denominator of the 5 % rule)
        for _ in range(2): run(c, p, lat=5000, user=12)                          # inefficiency: latency 50x the P95
        for u in (21, 22): tool_trace(c, p, "search_kb", status="error", error="search_kb failed: index missing", user=u)    # tool_error, 2 traces / 2 users
        for u in range(31, 36): run(c, p, status="error", error="db connection refused", user=u)                            # error, 5 distinct users -> high
        run(c, p, status="error", error="weird one-off failure", user=41)                                                   # error, one trace -> low
        for _ in range(2): run(c, p, status="error", error="Đã chạm trần chi phí Google Gemini. Liên hệ quản trị viên để nâng trần.", user=42)   # guard: cap block (not an error cluster)
        for _ in range(2): run(c, p, md={"aos.pii.masked": 3}, user=43)
        for _ in range(2): run(c, p, md={"aos.memory.refused": 1, "aos.memory.refused_kind": "secret"}, user=44)
        for _ in range(2): fb(c, p, run(c, p, user=45)[0], "instruction_following", 0, source="online", evaluator=999001)   # judge verdict of 12.70
        for _ in range(2): fb(c, p, run(c, p, user=46)[0], "helpful_judge", 0.1, source="online", evaluator=999002)          # low_quality
        for _ in range(2): fb(c, p, run(c, p, user=47)[0], "reviewer_rating", 0, source="human")                              # ANY key, score <= 0
        fb(c, p, run(c, p, user=48)[0], "rating5", 2, source="human")                                                         # normalised (2-1)/4 = 0.25 <= 0.3
        fb(c, p, run(c, p, user=49)[0], "rating5", 4, source="human"); fb(c, p, run(c, p, user=49)[0], "rawq", 0.2, source="human")   # not dissatisfied
        fb(c, p, run(c, p, user=49)[0], "other_judge", 0, source="online", evaluator=999003)                                  # a judge's 0 is low_score, not dissatisfaction
        for _ in range(2): tool_trace(c, p, "fetch_page", outputs=None)                                                      # empty result
        for _ in range(2): tool_trace(c, p, "read_file", outputs={"result": "first part of a long file …truncated"})        # marker
        for _ in range(2): tool_trace(c, p, "cat", outputs={"result": "x" * 4000})                                           # exactly a known limit
        tool_trace(c, p, "ok_tool", outputs={"result": "fine"}); tool_trace(c, p, "near", outputs={"result": "y" * 3999}); tool_trace(c, p, "broken", status="error", error="x", outputs=None)   # controls
        # workspace 2: its own data (a 5-trace cluster), must never show in workspace 1 and vice versa
        for u in range(1, 6): run(c, w["p2"], status="error", error="other tenant failure", user=u, agent=AG + 1)
    return w

def by_sig(evs): return {(e["kind"], e["signature"].split("|")[0]): e for e in evs}

def test_each_category_once_with_the_right_severity(seeded):
    w = seeded
    async def go(E, as_ws):
        with as_ws(w["w1"]): return await E.events_all(w["p1"], 0.5)
    evs = inproc(go); ev = by_sig(evs); a = f"agent:{AG}"
    exp = {("error_cluster", "search_kb failed: index missing"): ("tool_error", 2, 2), ("error_cluster", "db connection refused"): ("error", 5, 1), ("error_cluster", "weird one-off failure"): ("error", 1, 3),
           ("low_score", "helpful_judge"): ("low_quality", 2, 2), ("negative_feedback", a): ("user_dissatisfaction", 3, 2), ("inefficiency", "latency"): ("inefficiency", 2, 2),
           ("truncated_tool_result", "fetch_page"): ("truncated_tool_result", 2, 2), ("truncated_tool_result", "read_file"): ("truncated_tool_result", 2, 2), ("truncated_tool_result", "cat"): ("truncated_tool_result", 2, 2),
           ("guard_violation", "cap_block"): ("guard_violation", 2, 2), ("guard_violation", "pii_masked"): ("guard_violation", 2, 2), ("guard_violation", "memory_refused"): ("guard_violation", 2, 2),
           ("guard_violation", "instruction_following"): ("guard_violation", 2, 2),
           ("low_score", "other_judge"): ("low_quality", 1, 3), ("error_cluster", "x"): ("tool_error", 1, 3)}      # one-trace groups exist as events (low); the failing 'broken' tool control is a plain tool error
    for k, (cat, n, sev) in exp.items():
        assert k in ev, (k, sorted(ev)); assert (ev[k]["category"], ev[k]["count"], ev[k]["severity"]) == (cat, n, sev), (k, ev[k]["category"], ev[k]["count"], ev[k]["severity"])
    assert len(evs) == len(exp), sorted((e["kind"], e["signature"]) for e in evs)          # nothing else: no event from the healthy runs, ok_tool, the 3999-char result, rating5=4, rawq
    assert not any("chạm trần" in e["signature"] for e in evs if e["kind"] == "error_cluster")   # a cap block is a guard violation, not an error cluster
    assert ev[("error_cluster", "db connection refused")]["users"] == 5 and ev[("negative_feedback", a)]["users"] == 2

def test_negative_feedback_any_key_and_normalised_scale(seeded):
    w = seeded
    async def go(E, as_ws):
        with as_ws(w["w1"]): return await E.events_negative(w["p1"])
    [e] = inproc(go)
    assert e["count"] == 3 and e["category"] == "user_dissatisfaction"      # reviewer_rating 0 x2 + rating5=2 (0.25); rating5=4, rawq=0.2 (no scale) and a judge's 0 are NOT counted

def test_severity_rule_unit():
    import engine
    assert engine._sev(1, 99, 1) == 3                                         # one trace is never more than low
    assert engine._sev(5, 5, 100000) == 1 and engine._sev(4, 4, 100000) == 2   # >= 5 distinct users
    assert engine._sev(5, 0, 100) == 1 and engine._sev(4, 0, 100) == 2         # >= 5 % of the agent's traffic
    assert engine._sev(2, 0, 0) == 2

def test_workspace_isolation_of_events_and_issues(seeded):
    w = seeded
    async def go(E, as_ws):
        with as_ws(w["w2"]): x = await E.events_all(w["p1"], 0.5)                      # project of workspace 1 read as workspace 2: nothing
        with as_ws(w["w2"]): y = await E.events_all(w["p2"], 0.5)
        with as_ws(w["w1"]): z = await E.events_all(w["p2"], 0.5)                      # and the other way round
        return x, y, z
    x, y, z = inproc(go)
    assert x == [] and z == [] and [e["signature"] for e in y] == ["other tenant failure"] and y[0]["severity"] == 1

def test_baseline_minimum_for_inefficiency(world):
    with pg() as c:
        ws, p = mkws(c, "base"); ag = AG + 7
        for _ in range(29): run(c, p, agent=ag)
        run(c, p, agent=ag, lat=9000)                                            # 30 runs of the agent: 29 normal + 1 outlier -> baseline is enough
    async def go(E, as_ws):
        with as_ws(ws): return await E.events_inefficiency(p)
    [e] = inproc(go); assert e["count"] == 1 and e["signature"].startswith("latency|") and e["category"] == "inefficiency"
    with pg() as c: c.execute("DELETE FROM tr_runs WHERE id = (SELECT id FROM tr_runs WHERE project_id=%s AND latency_ms=100 LIMIT 1)", (p,))   # 29 runs: below the minimum
    assert inproc(go) == []                                                      # skipped, no guess from too little data
    with pg() as c: run(c, p, agent=ag)
    assert len(inproc(go)) == 1                                                  # back to 30: detected again

def test_inefficiency_agent_without_baseline_is_skipped_in_the_main_world(seeded):
    w = seeded
    with pg() as c:
        for _ in range(3): run(c, w["pb"], agent=AG + 3, lat=99999)             # 3 runs of another agent in project 2 of workspace 1: no baseline at all
    async def go(E, as_ws):
        with as_ws(w["w1"]): return await E.events_inefficiency(w["pb"])
    assert inproc(go) == []

def test_thread_level_feedback_waits_until_the_thread_is_idle(world):
    w = world
    with pg() as c:
        ws, p = mkws(c, "idle"); idle = c.execute("SELECT thread_idle_min FROM projects WHERE id=%s", (p,)).fetchone()[0]
        for tag, ago in (("active", 1), ("done1", idle + 60), ("done2", idle + 90)):
            rid, _ = run(c, p, thread=f"th-{tag}-{SUF}", ago=ago, agent=AG + 4)
            fb(c, p, rid, "conv_quality", 0.1, source="thread", evaluator=999004, md={"thread_id": f"th-{tag}-{SUF}"})
    async def go(E, as_ws):
        with as_ws(ws): return await E.events_low_score(p, 0.5)
    [e] = inproc(go)
    assert e["count"] == 2                                                       # the thread still active (last turn 1 min ago) gives no event; the two idle ones do
    with pg() as c: c.execute("UPDATE tr_runs SET end_time = now() - interval '1 minute' WHERE project_id=%s AND thread_id=%s", (p, f"th-done1-{SUF}"))   # activity resumed in one of them
    assert inproc(go)[0]["count"] == 1

def test_titles_and_signatures_are_masked_no_pii_in_issues(world):
    w = world
    mail, phone = "john.doe@example.com", "0912345678"
    with pg() as c:
        ws, p = mkws(c, "pii"); rid, _ = run(c, p, status="error", error=f"cannot send the invoice to {mail} (phone {phone}) now", agent=AG + 5)
    async def go(E, as_ws):
        with as_ws(ws):
            evs = await E.events_all(p, 0.5); out = []
            for e in evs: out.append(await E.upsert(ws, p, e))
            return evs, out, await E.A.q("SELECT title, signature, description FROM issues WHERE project_id=%s", (p,))
    evs, out, rows = inproc(go)
    assert mail in evs[0]["title"] or phone in evs[0]["title"]                  # the raw event does carry it ...
    assert len(rows) == 1 and out[0][0] == "new"
    for col in ("title", "signature"): assert mail not in rows[0][col] and phone not in rows[0][col], rows[0]   # ... the stored issue does not
    with pg() as c: assert not c.execute("SELECT 1 FROM issue_events e JOIN issues i ON i.id=e.issue_id WHERE i.project_id=%s AND (e.reason ILIKE %s OR e.to_val ILIKE %s)", (p, f"%{mail}%", f"%{mail}%")).fetchone()

def test_min_count_still_stops_one_off_noise_and_user_set_severity_is_kept(world):
    w = world
    with pg() as c: ws, p = mkws(c, "min")
    now = NOW()
    ev = lambda kind, n, sev: {"kind": kind, "category": "x", "signature": f"s-{kind}-{n}", "title": "t", "count": n, "first": now, "last": now, "agent_id": None, "samples": [], "severity": sev}   # noqa: E731
    async def go(E, as_ws):
        with as_ws(ws):
            r = [await E.upsert(ws, p, ev(k, 1, 2)) for k in ("low_score", "negative_feedback", "inefficiency", "truncated_tool_result", "guard_violation")]
            one = await E.upsert(ws, p, ev("error_cluster", 1, 3))                  # an error cluster of ONE trace is still reported
            [a1] = [one[1]["id"]]
            await E.A.q("INSERT INTO issue_events(issue_id, source, action, to_val) VALUES(%s,'user','severity','3')", (a1,))   # a person set the priority
            again = await E.upsert(ws, p, {**ev("error_cluster", 1, 3), "count": 4, "severity": 1})
            return r, one, again
    r, one, again = inproc(go)
    assert all(x == (None, None) for x in r) and one[0] == "new" and one[1]["severity"] == 3
    assert again[0] == "updated" and again[1]["severity"] == 3                   # detection never overrides what a user chose

def test_severity_only_goes_up_on_update(world):
    with pg() as c: ws, p = mkws(c, "up")
    now = NOW(); mk = lambda n, sev: {"kind": "error_cluster", "category": "error", "signature": "same", "title": "t", "count": n, "first": now, "last": now, "agent_id": None, "samples": [], "severity": sev}   # noqa: E731
    async def go(E, as_ws):
        with as_ws(ws):
            a = await E.upsert(ws, p, mk(1, 3)); b = await E.upsert(ws, p, mk(6, 1)); c_ = await E.upsert(ws, p, mk(6, 3))
            return a[1]["severity"], b[1]["severity"], c_[1]["severity"], b[1]["category"]
    assert inproc(go) == (3, 1, 1, "error")

# ------------------------------------------------------------------ resume of failed steps (patched model call, no LLM)
def _fake_ask(mode, calls):
    from engine import EngineAnalysis, Proposal
    async def ask(msgs, schema=None, feature=None, acc=None):
        calls.append(1)
        if mode["fail"]: raise RuntimeError("503 UNAVAILABLE simulated overload")
        acc.append(0.001)
        return EngineAnalysis(root_cause="the tool index is missing", evidence=[], proposal=Proposal(type="config", target="index", rationale="rebuild it")), None
    return ask

def test_failed_analysis_step_is_stored_and_resumed_on_the_next_scan(world, monkeypatch):
    import deepagent_api, nlsearch  # noqa: F401  (deepagent_api first: circular import)
    with pg() as c:
        ws, p = mkws(c, "resume"); run(c, p, status="error", error="resume me please", agent=AG + 6)
    mode, calls = {"fail": True}, []
    monkeypatch.setattr(nlsearch, "_ask", _fake_ask(mode, calls))
    async def go(E, as_ws):
        async def noguard(*a, **k): return None
        monkeypatch.setattr(E, "_guard", noguard)
        s1 = await E.detect_ws(ws, True, force=True)                             # scan 1: issue created, the model call fails
        with as_ws(ws): runs1 = await E.A.q("SELECT status, error FROM engine_runs WHERE project_id=%s ORDER BY id", (p,)); an1 = (await E.A.q("SELECT analysis FROM issues WHERE project_id=%s", (p,)))[0]["analysis"]
        # a paused project is not resumed
        with as_ws(ws): await E.A.q("INSERT INTO engine_project_settings(project_id, workspace_id, enabled, paused) VALUES(%s,%s,true,true) ON CONFLICT(project_id) DO UPDATE SET paused=true", (p, ws))
        n = len(calls); await E.detect_ws(ws, True, force=True); paused_calls = len(calls) - n
        with as_ws(ws): await E.A.q("UPDATE engine_project_settings SET paused=false WHERE project_id=%s", (p,))
        mode["fail"] = False
        s2 = await E.detect_ws(ws, True, force=True)                             # scan 2: nothing new, the failed step is resumed and succeeds
        with as_ws(ws):
            runs2 = await E.A.q("SELECT status, cost_usd FROM engine_runs WHERE project_id=%s ORDER BY id", (p,)); iss = (await E.A.q("SELECT analysis, cost_usd, refs FROM issues WHERE project_id=%s", (p,)))[0]
        n = len(calls); await E.detect_ws(ws, True, force=True); done_calls = len(calls) - n     # scan 3: analysed already, no more model calls
        return s1, runs1, an1, paused_calls, s2, runs2, iss, done_calls
    s1, runs1, an1, paused_calls, s2, runs2, iss, done_calls = inproc(go)
    assert len(s1["new"]) == 1 and s1["analysed"] == [] and an1 is None
    assert [r["status"] for r in runs1] == ["error"] and "503" in runs1[0]["error"]                  # the failure is stored, not lost
    assert paused_calls == 0                                                                          # paused project: no resume, no model call
    assert s2["new"] == [] and len(s2["analysed"]) == 1 and iss["analysis"]["root_cause"].startswith("the tool")
    assert [r["status"] for r in runs2] == ["error", "ok"] and float(runs2[1]["cost_usd"]) == pytest.approx(0.001) and "analysis_error" not in iss["refs"]
    assert done_calls == 0

def test_resume_gives_up_after_three_failures_and_never_crosses_workspaces(world, monkeypatch):
    import deepagent_api, nlsearch  # noqa: F401
    with pg() as c:
        ws, p = mkws(c, "giveup"); run(c, p, status="error", error="never works", agent=AG + 8)
        ws2, p2 = mkws(c, "other"); run(c, p2, status="error", error="other ws failure", agent=AG + 9)
        i2 = c.execute("INSERT INTO issues(workspace_id,project_id,kind,signature,title,count) VALUES(%s,%s,'error_cluster','other','other',1) RETURNING id", (ws2, p2)).fetchone()[0]
        c.execute("INSERT INTO engine_runs(workspace_id,project_id,issue_id,kind,status,error) VALUES(%s,%s,%s,'analyse','error','boom')", (ws2, p2, i2))   # a failed step of ANOTHER workspace
    mode, calls = {"fail": True}, []
    monkeypatch.setattr(nlsearch, "_ask", _fake_ask(mode, calls))
    async def go(E, as_ws):
        async def noguard(*a, **k): return None
        monkeypatch.setattr(E, "_guard", noguard)
        for _ in range(3): await E.detect_ws(ws, True, force=True)
        n = len(calls); await E.detect_ws(ws, True, force=True); extra = len(calls) - n             # 4th scan: RESUME_MAX reached
        with as_ws(ws): mine = await E.failed_steps(p)
        with as_ws(ws2): own = await E.failed_steps(p2)                                             # the owner sees its failed step ...
        with as_ws(ws): foreign = await E.failed_steps(p2)                                          # ... another workspace's context does not: RLS hides it
        with as_ws(ws): rows = await E.A.q("SELECT count(*) n FROM engine_runs WHERE project_id=%s", (p,))
        with as_ws(ws2): theirs = await E.A.q("SELECT count(*) n FROM engine_runs WHERE project_id=%s", (p,))   # ... and mine is hidden from them
        return extra, mine, own, foreign, rows[0]["n"], theirs[0]["n"]
    extra, mine, own, foreign, n_mine, n_theirs = inproc(go)
    assert extra == 0 and mine == [] and len(own) == 1 and foreign == [] and n_mine == 3 and n_theirs == 0
    assert len(calls) == 3                                                                          # only workspace 1 was scanned (ws2 never ran): 3 failed calls, then it stopped

# ------------------------------------------------------------------ from_trace (HTTP)
def test_from_trace_creates_one_masked_manual_issue(world):
    w = world
    with pg() as c:
        rid, tid = run(c, w["p1"], status="success", agent=AG + 10, inputs={"message": "refund for anna.k@example.com please"})
        _, tid3 = run(c, w["p1"], agent=AG + 10, inputs={"message": "hello there, I need help with my invoice"})
    r = w["ed"].post("/api/engine/issues/from_trace", json={"trace_id": tid, "note": "wrong refund, customer anna.k@example.com 0912345678 is angry"})
    assert r.status_code == 200, r.text; i = r.json()
    assert i["created"] is True and i["kind"] == "manual" and i["category"] == "user_reported" and i["severity"] == 3 and i["project_id"] == w["p1"]
    assert "anna.k@example.com" not in i["title"] and "0912345678" not in i["title"] and "anna.k@example.com" not in (i["description"] or "")   # masked
    assert [s["run_id"] for s in i["samples"]] == [rid] and i["traces"][0]["trace_id"] == tid
    h = w["ed"].get(f"/api/engine/issues/{i['id']}/history").json()["items"]
    assert len(h) == 1 and h[0]["action"] == "created" and h[0]["source"] == "user" and h[0]["actor"] == f"e33_ed_{SUF}" and "anna.k@example.com" not in (h[0]["reason"] or "")
    r2 = w["ed"].post("/api/engine/issues/from_trace", json={"trace_id": tid}).json()
    assert r2["created"] is False and r2["id"] == i["id"]                                          # the same trace twice = the same issue
    r3 = w["ed"].post("/api/engine/issues/from_trace", json={"trace_id": tid3}).json()                  # no note: the title comes from the first message
    assert r3["title"].startswith("Trace: hello there") and r3["id"] != i["id"]

def test_from_trace_refuses_other_workspace_unknown_viewer_demo_and_junk(world):
    w = world
    with pg() as c: _, tid1 = run(c, w["p1"], agent=AG + 11); _, tid2 = run(c, w["p2"], agent=AG + 11)
    n0 = lambda: len(w["ed"].get("/api/engine/issues", params={"status": "all"}).json()["items"])   # noqa: E731
    before = n0()
    assert w["ed2"].post("/api/engine/issues/from_trace", json={"trace_id": tid1}).status_code == 404      # workspace 2 editor, trace of workspace 1
    assert w["ed"].post("/api/engine/issues/from_trace", json={"trace_id": tid2}).status_code == 404       # workspace 1 editor, trace of workspace 2
    assert w["ed"].post("/api/engine/issues/from_trace", json={"trace_id": str(uuid.uuid4())}).status_code == 404
    assert w["ed"].post("/api/engine/issues/from_trace", json={"trace_id": "not-a-uuid"}).status_code == 422
    assert w["vw"].post("/api/engine/issues/from_trace", json={"trace_id": tid1}).status_code == 403        # viewer
    assert w["dm"].post("/api/engine/issues/from_trace", json={"trace_id": tid1}).status_code == 403        # demo account
    assert httpx.post(B + "/api/engine/issues/from_trace", json={"trace_id": tid1}, timeout=30).status_code in (401, 403)   # not signed in
    assert w["ed"].post("/api/engine/issues/from_trace", json={"trace_id": tid1, "note": "x" * 1001}).status_code == 422
    with pg() as c: assert c.execute("SELECT count(*) FROM issues WHERE signature IN (%s,%s)", (f"trace:{tid1}", f"trace:{tid2}")).fetchone()[0] == 0   # nothing was created anywhere
    assert n0() == before
    assert w["ed2"].get("/api/engine/issues", params={"status": "all", "category": "user_reported"}).json()["items"] == []

def test_http_scan_end_to_end_categories_sort_and_isolation(world):
    w = world
    with pg() as c:
        ws, p = mkws(c, "http")
        for _ in range(60): run(c, p, agent=AG + 12)                                                  # traffic, so 2 traces are < 5 %
        for u in range(1, 6): run(c, p, status="error", error="http cluster boom", user=u, agent=AG + 12)
        for _ in range(2): tool_trace(c, p, "fetch_page", outputs=None, agent=AG + 12)
    assert w["a"].post("/api/admin/users", json={"username": f"e33_h_{SUF}", "password": PW, "role": "editor", "workspace_id": ws}).status_code == 200
    h = httpx.Client(base_url=B, auth=(f"e33_h_{SUF}", PW), timeout=90)
    try:
        r = h.post("/api/engine/detect", params={"analyse": "false"}); assert r.status_code == 200 and len(r.json()["new"]) == 2, r.text
        items = h.get("/api/engine/issues", params={"status": "all", "sort": "severity"}).json()["items"]
        assert [(i["category"], i["severity"]) for i in items] == [("error", 1), ("truncated_tool_result", 2)]     # sorted by severity, most urgent first
        assert [i["category"] for i in h.get("/api/engine/issues", params={"category": "truncated_tool_result"}).json()["items"]] == ["truncated_tool_result"]
        assert [i["category"] for i in w["ed"].get("/api/engine/issues", params={"status": "all"}).json()["items"] if i["category"] != "user_reported"] == []   # workspace 1 sees none of them
        assert w["ed2"].get("/api/engine/issues", params={"status": "all"}).json()["items"] == []
        assert w["ed2"].get(f"/api/engine/issues/{items[0]['id']}").status_code == 404
    finally:
        h.close()
        with pg() as c: c.execute("DELETE FROM users WHERE username=%s", (f"e33_h_{SUF}",))

# ------------------------------------------------------------------ migration 0204
def test_migration_0204_up_down_roundtrip():
    spec = importlib.util.spec_from_file_location("m0204", Path(__file__).parent.parent / "app" / "migrations" / "0204_issue_kinds_v2.py"); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=False) as c:
            async def q(sql, args=()):
                cur = await c.execute(sql, args)
                return await cur.fetchall() if cur.description else None
            ws = (await q("SELECT id FROM workspaces LIMIT 1"))[0][0]
            pid = (await q("INSERT INTO projects(name, workspace_id) VALUES(%s,%s) RETURNING id", (f"e33m-{SUF}", ws)))[0][0]
            for n, k in enumerate(("error_cluster", "inefficiency", "truncated_tool_result", "guard_violation", "manual")):
                await q("INSERT INTO issues(workspace_id,project_id,kind,signature,title) VALUES(%s,%s,%s,%s,'t')", (ws, pid, k, f"s{n}"))
            await m.down(q)                                                                        # old CHECK back: the rows of the four new kinds are gone, the old one stays
            assert [r[0] for r in await q("SELECT kind FROM issues WHERE project_id=%s", (pid,))] == ["error_cluster"]
            try:
                await q("SAVEPOINT s1"); await q("INSERT INTO issues(workspace_id,project_id,kind,signature,title) VALUES(%s,%s,'manual','bad','t')", (ws, pid)); raise AssertionError("old CHECK must refuse 'manual'")
            except psycopg.errors.CheckViolation: await q("ROLLBACK TO SAVEPOINT s1")
            await m.up(q); await m.up(q)                                                           # idempotent
            await q("INSERT INTO issues(workspace_id,project_id,kind,signature,title) VALUES(%s,%s,'guard_violation','ok','t')", (ws, pid))
            await c.rollback()
    asyncio.run(go())
