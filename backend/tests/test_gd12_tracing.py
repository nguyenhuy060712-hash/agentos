"""GĐ12.15 new run columns (tokens by kind, cost split, time to first token, depth, ttl_days) + the 25,000-run trace limit. In-process on the local DB
(private pool under the app's RLS role, two throw-away workspaces) + one streamed fake-LLM chat against a running local server (GD12_BASE, default :8020; skipped when
nothing answers). Fake LLM everywhere: $0. Later cards of lane T (12.17 search, 12.16 filters) append their own sections below."""
import asyncio, datetime as dt, json, os, sys, uuid
import httpx, psycopg, pytest
from fastapi import HTTPException
from gd12_world import NOW, PG, as_user, run, sysq, with_world

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import deepagent_api  # noqa: E402,F401  (import order: deepagent_api first, obs <-> deepagent_api cycle)
import obs, otel  # noqa: E402
import gateway as gw  # noqa: E402
import migrations as M  # noqa: E402

BASE = os.environ.get("GD12_BASE", "http://127.0.0.1:8020"); AUTH = ("moza", "phase0-local")
FAKE = {"input_per_1m": 1.0, "output_per_1m": 10.0, "cached_input_per_1m": 0.1, "reasoning_per_1m": 20.0, "audio_input_per_1m": 3.0, "audio_output_per_1m": 30.0, "image_input_per_1m": 2.0}
NEW_COLS = {"first_token_time", "usage_details", "cost_details", "input_cost", "output_cost", "reference_example_id", "depth", "ttl_days"}
UD = {"input_tokens": 1000, "output_tokens": 500, "input_token_details": {"cache_read": 400}, "output_token_details": {"reasoning": 100}}

@pytest.fixture
def price(monkeypatch):
    monkeypatch.setitem(gw.PRICING, "t1215-model", dict(FAKE)); monkeypatch.setitem(gw.PRICING, "t1215-plain", {"input_per_1m": 1.0, "output_per_1m": 10.0})

async def row(A, rid): return await sysq(A, "SELECT * FROM tr_runs WHERE id=%s", (rid,), one=True)

# ---------------------------------------------------------------- migration 0105
def test_migration_0105_up_down_old_rows_read_fine():
    sch = "t1215_" + uuid.uuid4().hex[:8]; mod = {v: m for v, m, _ in M._load()}["0105"]
    async def go():
        with psycopg.connect(PG, autocommit=True) as c: c.execute(f'CREATE SCHEMA "{sch}"')
        try:
            async with await psycopg.AsyncConnection.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
                await c.execute("CREATE TABLE tr_runs(id uuid NOT NULL, start_time timestamptz NOT NULL, name text, PRIMARY KEY(id, start_time)) PARTITION BY RANGE(start_time)")
                await c.execute("CREATE TABLE tr_runs_p1 PARTITION OF tr_runs FOR VALUES FROM ('2026-01-01') TO ('2027-01-01')")
                await c.execute("INSERT INTO tr_runs VALUES(gen_random_uuid(), '2026-05-05', 'old')")
                q = M._q(c)
                assert {s.split()[5] for s in await mod.plan(q)} == NEW_COLS                      # dry run lists every column
                await mod.up(q); await mod.up(q)                                                  # idempotent (IF NOT EXISTS)
                async def colset(t): return {r[0] for r in await (await c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s", (sch, t))).fetchall()}
                assert NEW_COLS <= await colset("tr_runs") and NEW_COLS <= await colset("tr_runs_p1")   # the partition got them too
                assert await (await c.execute("SELECT name, first_token_time, usage_details, cost_details, input_cost, output_cost, reference_example_id, depth, ttl_days FROM tr_runs")).fetchone() == ("old",) + (None,) * 8
                await mod.down(q)
                assert not NEW_COLS & await colset("tr_runs") and {"id", "start_time", "name"} <= await colset("tr_runs")
                assert (await (await c.execute("SELECT name FROM tr_runs")).fetchone())[0] == "old"
        finally:
            with psycopg.connect(PG, autocommit=True) as c: c.execute(f'DROP SCHEMA "{sch}" CASCADE')
    asyncio.run(go())

# ---------------------------------------------------------------- units
def test_usage_details_of_and_cost_parts(price):
    assert obs.usage_details_of(UD) == {"cache_read": 400, "reasoning": 100}
    assert obs.usage_details_of({"input_token_details": {"cache_creation": 5, "ephemeral_5m_input_tokens": 3, "ephemeral_1h_input_tokens": 2, "audio": 7, "image": 1},
                                 "output_token_details": {"audio": 4, "video": 9}}) == {"cache_creation": 10, "audio": 11, "image": 1, "video": 9}
    assert obs.usage_details_of(None) == {} and obs.usage_details_of({"input_token_details": {"cache_read": -3, "audio": "x", "image": True}}) == {}
    ic, oc, cd = obs.cost_parts("t1215-model", UD, 1000, 500)
    assert ic == pytest.approx(0.6 * 1.0 / 1000 + 0.4 * 0.1 / 1000) and oc == pytest.approx(0.4 * 10 / 1000 + 0.1 * 20 / 1000)
    assert ic + oc == pytest.approx(gw.cost("t1215-model", 1000, 500, gw.details_from_usage(UD)))   # input + output = the gateway total
    assert cd == {"cache_read": pytest.approx(400 * 0.1 / 1e6), "reasoning": pytest.approx(100 * 20 / 1e6)}
    assert obs.cost_parts("t1215-plain", UD, 1000, 500)[2] == {"cache_read": pytest.approx(400 * 1.0 / 1e6), "reasoning": pytest.approx(100 * 10 / 1e6)}   # a kind without its own price = plain price
    assert obs.cost_parts("no-such-model", UD, 10, 10) == (None, None, None) and obs.cost_parts("t1215-model", {}, 0, 0) == (None, None, None)

def test_otlp_usage_attributes_map_to_the_same_details(price):
    body = {"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": "ab" * 16, "spanId": "cd" * 8, "name": "chat", "startTimeUnixNano": "1790000000000000000", "endTimeUnixNano": "1790000001000000000",
        "attributes": [{"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}}, {"key": "gen_ai.request.model", "value": {"stringValue": "t1215-model"}},
                       {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "1000"}}, {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "500"}},
                       {"key": "gen_ai.usage.cache_read.input_tokens", "value": {"intValue": "400"}}, {"key": "gen_ai.usage.reasoning.output_tokens", "value": {"intValue": "100"}}]}]}]}]}
    r = otel.otlp_to_runs(body)[0]
    assert r["usage"]["input_tokens"] == 1000 and r["usage"]["input_token_details"] == {"cache_read": 400} and r["usage"]["output_token_details"] == {"reasoning": 100}
    assert obs.usage_details_of(r["usage"]) == {"cache_read": 400, "reasoning": 100}
    oi = otel.otlp_to_runs({"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": "ab" * 16, "spanId": "ef" * 8, "name": "x", "startTimeUnixNano": "1790000000000000000",
        "attributes": [{"key": "llm.token_count.prompt_details.cache_read", "value": {"intValue": "7"}}, {"key": "llm.token_count.completion_details.reasoning", "value": {"intValue": "3"}}]}]}]}]})[0]
    assert obs.usage_details_of(oi["usage"]) == {"cache_read": 7, "reasoning": 3}
    assert "input_token_details" not in otel.otlp_to_runs({"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": "ab" * 16, "spanId": "01" * 8, "name": "plain", "startTimeUnixNano": "1790000000000000000"}]}]}]})[0]["usage"]

# ---------------------------------------------------------------- stored columns
def test_cached_token_fixture_fills_usage_details_and_costs(price):
    async def go(A, admin, W):
        pid = W["a"]["pid"]; rid, _ = await run(obs, pid, run_type="llm", model="t1215-model", usage=UD)
        r = await row(A, rid)
        assert r["usage_details"] == {"cache_read": 400, "reasoning": 100} and r["input_tokens"] == 1000 and r["total_tokens"] == 1500
        assert float(r["input_cost"]) + float(r["output_cost"]) == pytest.approx(float(r["cost_usd"]), abs=2e-6)   # cost_usd now prices the cached / reasoning tokens too
        assert r["cost_details"]["cache_read"] == pytest.approx(400 * 0.1 / 1e6) and r["cost_details"]["reasoning"] == pytest.approx(100 * 20 / 1e6)
        # SDK bodies that bring their own details win; junk is dropped; nothing -> NULL (not {})
        rid2, _ = await run(obs, pid, usage_details={"cache_read": 5, "bogus": 9, "audio": -2}, cost_details={"cache_read": 0.5}, input_cost=0.25, output_cost="x")
        r2 = await row(A, rid2); assert r2["usage_details"] == {"cache_read": 5} and r2["cost_details"] == {"cache_read": 0.5} and float(r2["input_cost"]) == 0.25 and r2["output_cost"] is None
        rid3, _ = await run(obs, pid, model="t1215-plain", usage={"input_tokens": 10, "output_tokens": 5})
        r3 = await row(A, rid3); assert r3["usage_details"] is None and r3["cost_details"] is None and float(r3["input_cost"]) > 0
        rid4, _ = await run(obs, pid); r4 = await row(A, rid4); assert r4["input_cost"] is None and r4["usage_details"] is None and r4["first_token_time"] is None
        # a later PATCH with the final usage fills the details of the already stored run
        rid5, _ = await run(obs, pid, run_type="llm", model="t1215-model")
        await obs.upsert_run(pid, {"id": rid5, "end_time": NOW(), "usage": UD}); r5 = await row(A, rid5)
        assert r5["usage_details"] == {"cache_read": 400, "reasoning": 100} and float(r5["input_cost"]) > 0
    with_world(go)

def test_otlp_fixture_lands_in_the_same_columns(price):
    async def go(A, admin, W):
        pid = W["a"]["pid"]; tid = uuid.uuid4().hex
        body = {"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": tid, "spanId": "12" * 8, "name": "chat", "startTimeUnixNano": str(int(NOW().timestamp() * 1e9)),
            "endTimeUnixNano": str(int(NOW().timestamp() * 1e9) + 10 ** 9),
            "attributes": [{"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}}, {"key": "gen_ai.request.model", "value": {"stringValue": "t1215-model"}},
                           {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "1000"}}, {"key": "gen_ai.usage.output_tokens", "value": {"intValue": "500"}},
                           {"key": "gen_ai.usage.cache_read.input_tokens", "value": {"intValue": "400"}}]}]}]}]}
        for rr in otel.otlp_to_runs(body): await obs.upsert_run(pid, rr)
        r = await sysq(A, "SELECT * FROM tr_runs WHERE trace_id=%s", (str(uuid.UUID(hex=tid)),), one=True)
        assert r["usage_details"] == {"cache_read": 400} and float(r["input_cost"]) > 0 and r["depth"] == 0
    with_world(go)

def test_first_token_time_only_when_streamed_and_after_the_start():
    async def go(A, admin, W):
        pid = W["a"]["pid"]; st = NOW()
        ok, _ = await run(obs, pid, start=st, first_token_time=st + dt.timedelta(milliseconds=300)); assert (await row(A, ok))["first_token_time"] - st == dt.timedelta(milliseconds=300)
        for bad in (st - dt.timedelta(seconds=1), "not a date", 0 * 1, None):                         # before the start / garbage / absent: NULL, never 0
            rid, _ = await run(obs, pid, start=st, first_token_time=bad); assert (await row(A, rid))["first_token_time"] is None, bad
        late, _ = await run(obs, pid, start=st); await obs.upsert_run(pid, {"id": late, "first_token_time": (st + dt.timedelta(seconds=2)).isoformat()})   # arrives with the PATCH
        assert (await row(A, late))["first_token_time"] == st + dt.timedelta(seconds=2)
        # the agent path: root run gets start + ms, never equal to the start, never after the end; LLM spans stay NULL
        t0 = NOW().timestamp()
        for ms, lat, want in ((0, 800, 1), (350, 800, 350), (5000, 800, 800), (None, 800, None)):
            tid = await as_user(admin, W["a"]["ws"], lambda: obs.record_agent_trace(agent_name="a", agent_id=1, version=1, thread_id="th", message="m", answer="x", error=None, t_start_wall=t0, latency_ms=lat,
                spans=[{"id": "s1", "name": "m", "kind": "llm", "start_ms": 10, "end_ms": 700, "input_tokens": 1, "output_tokens": 1}], input_tokens=1, output_tokens=1, cost=0, models=["m"], eval_run_id=None,
                project=f"p-a-{W['tag']}", first_token_ms=ms))
            rows = await sysq(A, "SELECT parent_id, start_time, first_token_time FROM tr_runs WHERE trace_id=%s", (tid,))
            root = next(r for r in rows if r["parent_id"] is None); span = next(r for r in rows if r["parent_id"] is not None)
            assert span["first_token_time"] is None
            if want is None: assert root["first_token_time"] is None
            else: assert root["first_token_time"] - root["start_time"] == dt.timedelta(milliseconds=want)
    with_world(go)

def test_depth_and_reference_example_id():
    async def go(A, admin, W):
        pid = W["a"]["pid"]; ex = str(uuid.uuid4())
        root, tr = await run(obs, pid, reference_example_id=ex); assert (await row(A, root))["depth"] == 0 and str((await row(A, root))["reference_example_id"]) == ex
        kid, _ = await run(obs, pid, trace=tr, parent=root); gk, _ = await run(obs, pid, trace=tr, parent=kid)
        assert [(await row(A, x))["depth"] for x in (kid, gk)] == [1, 2]                           # no dotted_order: parent's depth + 1
        d, _ = await run(obs, pid, dotted_order="20260101T000000000000Z" + str(uuid.uuid4()) + ".20260101T000001000000Z" + str(uuid.uuid4()) + ".20260101T000002000000Z" + str(uuid.uuid4()))
        assert (await row(A, d))["depth"] == 2
        orphan, _ = await run(obs, pid, parent=str(uuid.uuid4()), reference_example_id="nope"); r = await row(A, orphan)
        assert r["depth"] is None and r["reference_example_id"] is None                              # unknown parent: unknown depth; junk id dropped
    with_world(go)

# ---------------------------------------------------------------- limits
def test_trace_over_the_run_limit_is_refused_whole_and_other_workspaces_are_not_counted(monkeypatch):
    async def go(A, admin, W):
        a, b = W["a"], W["b"]; monkeypatch.setattr(obs, "TRACE_RUN_CAP", 3)
        tid = str(uuid.uuid4()); mk = lambda i, t=tid: {"id": str(uuid.uuid4()) if i is None else i, "trace_id": t, "name": "n", "start_time": NOW().isoformat()}   # noqa: E731
        r1 = [mk(None) for _ in range(3)]
        out = await obs.ingest(obs.Ingest(post=r1), x_api_key=a["ing"]); assert out["n"] == 3        # exactly at the limit: fine
        before = (await sysq(A, "SELECT count(*) n FROM tr_runs WHERE trace_id=%s", (tid,), one=True))["n"]
        extra = [mk(None), mk(None)]
        with pytest.raises(HTTPException) as e: await obs.ingest(obs.Ingest(post=extra), x_api_key=a["ing"])
        assert e.value.status_code == 413 and tid in e.value.detail and "3" in e.value.detail
        assert (await sysq(A, "SELECT count(*) n FROM tr_runs WHERE trace_id=%s", (tid,), one=True))["n"] == before   # nothing of the refused batch was written
        out = await obs.ingest(obs.Ingest(post=r1, patch=[{"id": r1[0]["id"], "end_time": NOW().isoformat()}]), x_api_key=a["ing"]); assert out["n"] == 4   # re-sent / patched runs do not count
        mixed = [mk(None, str(uuid.uuid4())) for _ in range(3)] + [mk(None)]                         # 3 new traces' worth fit; the 4th run of tid does not
        with pytest.raises(HTTPException) as e: await obs.ingest(obs.Ingest(post=mixed), x_api_key=a["ing"])
        assert e.value.status_code == 413
        assert not await sysq(A, "SELECT 1 FROM tr_runs WHERE id = ANY(%s::uuid[])", ([m["id"] for m in mixed],))      # the whole batch was refused, even the part that fit
        # another workspace's trace id is not counted against us (and cannot be written into): B's key with the same trace id starts from 0
        out = await obs.ingest(obs.Ingest(post=[mk(None, str(uuid.uuid4())) for _ in range(3)]), x_api_key=b["ing"]); assert out["n"] == 3
        with pytest.raises(HTTPException) as e: await obs.ingest(obs.Ingest(post=[mk(None)] * 1001), x_api_key=a["ing"])
        assert e.value.status_code == 413                                                            # the old 1000-per-batch limit still holds
        with pytest.raises(HTTPException) as e: await obs.ingest(obs.Ingest(post=[mk(None)]), x_api_key="ak_wrong")
        assert e.value.status_code == 401
        monkeypatch.setattr(obs, "TRACE_RUN_CAP", 2)                                                 # the OTLP path shares the check
        body = {"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": "9" * 32, "spanId": f"{i:016x}", "name": "s", "startTimeUnixNano": str(int(NOW().timestamp() * 1e9))} for i in range(1, 4)]}]}]}
        runs = otel.otlp_to_runs(body)
        async def ctx(): return await obs.check_trace_cap(runs)
        with pytest.raises(HTTPException) as e: await as_user(admin, a["ws"], ctx)
        assert e.value.status_code == 413
    with_world(go)

def test_trace_view_returns_at_most_1000_runs_and_says_so(monkeypatch):
    async def go(A, admin, W):
        a = W["a"]; monkeypatch.setattr(obs, "TRACE_VIEW_MAX", 3)
        root, tr = await run(obs, a["pid"]); [await run(obs, a["pid"], trace=tr, parent=root) for _ in range(2)]
        v = await as_user(admin, a["ws"], lambda: obs.trace(tr)); assert len(v["runs"]) == 3 and v["truncated"] is False
        await run(obs, a["pid"], trace=tr, parent=root)
        v = await as_user(admin, a["ws"], lambda: obs.trace(tr)); assert len(v["runs"]) == 3 and v["truncated"] is True
        with pytest.raises(HTTPException) as e: await as_user(admin, W["b"]["ws"], lambda: obs.trace(tr))   # another workspace: not found
        assert e.value.status_code == 404
        lst = await as_user(admin, a["ws"], lambda: obs.runs(a["pid"]))
        assert lst and NEW_COLS <= set(lst[0]) and lst[0]["depth"] == 0
        assert await as_user(admin, W["b"]["ws"], lambda: obs.runs(a["pid"])) == []                  # B listing A's project: nothing
    with_world(go)

# ---------------------------------------------------------------- streamed fake-LLM chat (needs a running local server)
def _server_up():
    try: return httpx.get(BASE + "/health", timeout=3).status_code < 500
    except Exception: return False   # noqa: BLE001

@pytest.mark.skipif(not _server_up(), reason=f"no local server at {BASE} (set GD12_BASE)")
def test_streamed_fake_llm_chat_root_run_has_first_token_after_start():
    with httpx.Client(base_url=BASE, auth=AUTH, timeout=120) as c:
        ag = c.get("/api/agents").json()[0]["id"]
        done, tokens = None, 0
        with c.stream("POST", "/chat/stream", json={"agent_id": ag, "thread_id": "t1215-" + uuid.uuid4().hex[:8], "message": "Xin chào, nói một câu ngắn.", "stream_tokens": True}) as r:
            for line in r.iter_lines():
                if not line.strip(): continue
                ev = json.loads(line); tokens += ev["type"] == "token"
                if ev["type"] == "done": done = ev
        assert done and not done["error"] and tokens > 0 and done["trace_id"]
        t = c.get(f"/api/obs/traces/{done['trace_id']}").json()
        root = next(x for x in t["runs"] if x["parent_id"] is None)
        assert t["truncated"] is False and root["first_token_time"] and root["first_token_time"] > root["start_time"] and root["first_token_time"] <= root["end_time"]
        assert root["depth"] == 0
        assert all(x["first_token_time"] is None for x in t["runs"] if x["parent_id"] is not None)
        # not streamed: no first token (NULL, never 0)
        r2 = c.post("/chat/stream", json={"agent_id": ag, "thread_id": "t1215-" + uuid.uuid4().hex[:8], "message": "ngắn thôi", "stream_tokens": False})
        d2 = [json.loads(x) for x in r2.text.splitlines() if x.strip()][-1]
        root2 = next(x for x in c.get(f"/api/obs/traces/{d2['trace_id']}").json()["runs"] if x["parent_id"] is None)
        assert root2["first_token_time"] is None

# ================================================================ 12.17 full-text search (fts.py, migration 0108, search() in compile_filter)
# Timing note (26/10, local Postgres, GIN on a tsvector column, 100,000 rows x 41 words): a one-word search takes about 1 ms (target < 1 s; 1M rows grows ~linearly in matches only).
import hashlib, hmac as _hmac
import fts  # noqa: E402

def _fake_mac(monkeypatch):
    """crypto.mac with a fake per-workspace key (no DEK table needed): same shape 'm1:<hex>'."""
    monkeypatch.setattr(fts.crypto, "mac", lambda data, ws=0: "m1:" + _hmac.new(f"k{ws}".encode(), data.encode(), hashlib.sha256).hexdigest())

def test_fts_fold_tokens_and_query_shapes():
    assert fts.fold("Báo cáo ĐƠN hàng") == "bao cao don hang"
    assert fts.tokens("Báo cáo, báo-cáo; Đơn_hàng 12.5 x") == ["bao", "cao", "don", "hang", "12", "5", "x"]   # distinct, punctuation and _ split
    assert fts.tokens("a" * 65 + " ok") == ["ok"]                                                              # blobs are not words
    assert fts.query("Báo cáo") == "'bao' & 'cao'" and fts.query("Báo cáo", "input") == "'bao':A & 'cao':A" and fts.query("báo cáo", "output") == "'bao':B & 'cao':B"
    assert fts.query("báo cáo của the") == "'bao' & 'cao'"                  # stop words (vi + en) dropped
    assert fts.query("the và") == "'the' & 'va'"                            # ... but not when nothing else is left
    assert fts.query("***") is None and fts.query("") is None               # no word: the caller falls back to a substring match
    q = fts.query("x'); DROP TABLE tr_runs; --")                             # only letters / digits can reach the tsquery text
    assert set(q) <= set("'& :ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz") and "DROP" not in q and "drop" in q
    with pytest.raises(ValueError): fts.query("x", "everything")
    a, b, c = fts.tsv_text({"messages": [{"content": "Xin chào Báo cáo"}], "n": 7, "ok": True}, {"a": "doanh thu"}, "Lỗi mạng", ["vip"], {"agent": "kế toán"})
    assert a.split() == ["xin", "chao", "bao", "cao", "7"] and b == "doanh thu" and set(c.split()) == {"loi", "mang", "vip", "ke", "toan"}
    big = fts.tsv_text({"t": " ".join(f"w{i}" for i in range(20000))}, None)[0]                  # only the first 32 KB of a field is indexed
    assert 0 < len(big) <= 32 * 1024 and "w19999" not in big.split()

def test_fts_blind_index_hides_words_and_is_per_workspace(monkeypatch):
    _fake_mac(monkeypatch)
    a, b, c = fts.tsv_text({"q": "Báo cáo doanh thu"}, {"a": "xong"}, None, [], {}, ws=7)
    assert all(not any(w in t for w in ("bao", "cao", "doanh", "thu", "xong")) for t in (a, b)) and len(a.split()) == 4 and all(w.startswith("h") and len(w) == 65 for w in a.split())
    assert a.split()[0] == fts.blind("bao", 7) and fts.blind("bao", 7) != fts.blind("bao", 8) and fts.blind("bao", 7) == fts.blind("bao", 7)
    q = fts.query("bao cao", "input", 7); assert q.startswith("('bao':A & 'cao':A) | (") and fts.blind("bao", 7) in q and fts.blind("cao", 7) in q
    assert fts.query("bao", "all", None) == "'bao'"
    assert fts.info(None)["encrypted"] is False

def test_migration_0108_up_down_and_partition_attached_later_is_indexed():
    sch = "t1217_" + uuid.uuid4().hex[:8]; mod = {v: m for v, m, _ in M._load()}["0108"]
    async def go():
        with psycopg.connect(PG, autocommit=True) as c: c.execute(f'CREATE SCHEMA "{sch}"')
        try:
            async with await psycopg.AsyncConnection.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:   # ONLY the throw-away schema: down() must never reach the real tr_runs indexes
                await c.execute("CREATE TABLE tr_runs(id uuid NOT NULL, start_time timestamptz NOT NULL, inputs jsonb, PRIMARY KEY(id, start_time)) PARTITION BY RANGE(start_time)")
                await c.execute("CREATE TABLE tr_runs_p1 PARTITION OF tr_runs FOR VALUES FROM ('2026-01-01') TO ('2026-02-01')")
                await c.execute("INSERT INTO tr_runs VALUES(gen_random_uuid(), '2026-01-05', '{}')")
                q = M._q(c); assert len(await mod.plan(q)) == 4
                await mod.up(q); await mod.up(q)                                                  # idempotent
                async def idx(t): return [r[0] for r in await (await c.execute("SELECT indexdef FROM pg_indexes WHERE schemaname=%s AND tablename=%s", (sch, t))).fetchall()]
                assert any("USING gin (search_tsv)" in d for d in await idx("tr_runs_p1"))        # the existing partition got the GIN index
                assert (await (await c.execute("SELECT search_tsv FROM tr_runs")).fetchone())[0] is None   # old rows: NULL = not indexed yet
                # what obs.ensure_partitions does for a new month: CREATE ... LIKE + ATTACH
                await c.execute("CREATE TABLE tr_runs_p2 (LIKE tr_runs INCLUDING DEFAULTS)")
                await c.execute("ALTER TABLE tr_runs ATTACH PARTITION tr_runs_p2 FOR VALUES FROM ('2026-02-01') TO ('2026-03-01')")
                assert any("USING gin (search_tsv)" in d for d in await idx("tr_runs_p2"))        # indexed by the ATTACH
                await c.execute("INSERT INTO tr_runs(id,start_time,search_tsv) VALUES(gen_random_uuid(), '2026-02-02', to_tsvector('simple','bao cao'))")
                assert (await (await c.execute("SELECT count(*) FROM tr_runs WHERE search_tsv @@ to_tsquery('simple','bao')")).fetchone())[0] == 1
                assert (await (await c.execute("SELECT aos_fold('Đơn hàng Báo cáo')")).fetchone())[0] == "don hang bao cao"
                await mod.down(q)
                cols = {r[0] for r in await (await c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name='tr_runs'", (sch,))).fetchall()}
                assert "search_tsv" not in cols and "id" in cols
        finally:
            with psycopg.connect(PG, autocommit=True) as c: c.execute(f'DROP SCHEMA "{sch}" CASCADE')
    asyncio.run(go())

def test_every_real_partition_has_the_search_index():
    with psycopg.connect(PG, autocommit=True) as c:
        parts = [r[0] for r in c.execute("SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid WHERE i.inhparent='tr_runs'::regclass")]
        assert parts, "no partitions"
        for p in parts:
            assert c.execute("SELECT 1 FROM pg_indexes WHERE tablename=%s AND indexdef LIKE '%%USING gin (search_tsv)%%'", (p,)).fetchone(), p

async def _found(admin, W, side, flt):
    return {str(r["id"]) for r in await as_user(admin, W[side]["ws"], lambda: obs.runs(W[side]["pid"], filter=flt, limit=500))}

def test_search_folds_accents_scopes_and_old_rows_still_work():
    async def go(A, admin, W):
        pid = W["a"]["pid"]; f = lambda s, *sc: "search(" + ",".join(json.dumps(x) for x in (s, *sc)) + ")"   # noqa: E731
        r1, _ = await run(obs, pid, inputs={"q": "Gửi tôi Báo cáo doanh thu"}, outputs={"a": "đã xong"})
        r2, _ = await run(obs, pid, inputs={"q": "don hang moi"}, outputs={"a": "báo cáo ở file"}, error="Lỗi kết nối kho")
        r3, _ = await run(obs, pid, inputs={"q": "hello world"}, outputs={"a": "zzz"})
        assert await _found(admin, W, "a", f("bao cao")) == {r1, r2}                              # 'bao cao' finds 'Báo cáo' (inputs of 1, outputs of 2)
        assert await _found(admin, W, "a", f("đơn hàng")) == {r2} and await _found(admin, W, "a", f("DON HANG")) == {r2}   # 'đơn hàng' <-> 'don hang'
        assert await _found(admin, W, "a", f("bao cao", "input")) == {r1} and await _found(admin, W, "a", f("bao cao", "output")) == {r2}   # scopes differ
        assert await _found(admin, W, "a", f("ket noi kho")) == {r2}                              # the error text is searched too
        assert await _found(admin, W, "a", f("bao hello")) == set()                               # AND of the words
        assert await _found(admin, W, "a", f("của bao cao")) == {r1, r2}                          # a stop word does not have to occur
        assert await _found(admin, W, "a", "not(" + f("bao cao") + ")") == {r3}
        with pytest.raises(HTTPException) as e: obs.compile_filter('search("x","both")')
        assert e.value.status_code == 400
        # a run not indexed yet (NULL, written before 0108 / waiting for the backfill) is still found by the ILIKE fallback
        old, _ = await run(obs, pid, inputs={"q": "legacy PhraseXyz"}); await sysq(A, "UPDATE tr_runs SET search_tsv=NULL WHERE id=%s", (old,))
        assert await _found(admin, W, "a", f("phrasexyz")) == {old} and await _found(admin, W, "a", f("legacy phrase")) == {old}   # substring fallback
        assert await fts.backfill(50) >= 1 and (await sysq(A, "SELECT search_tsv FROM tr_runs WHERE id=%s", (old,), one=True))["search_tsv"] is not None
        assert await _found(admin, W, "a", f("legacy phrasexyz")) == {old}                        # now through the index
        # PATCH: the final output arrives later and becomes searchable
        p1, _ = await run(obs, pid, inputs={"q": "cau hoi"}, outputs=None, end_time=None)
        await obs.upsert_run(pid, {"id": p1, "end_time": NOW(), "outputs": {"a": "ketqua cuoicung"}})
        assert await _found(admin, W, "a", f("cuoicung", "output")) == {p1} and await _found(admin, W, "a", f("cau hoi", "input")) == {p1}
        # RLS: workspace B sees nothing of A, with or without the search
        assert await _found(admin, W, "b", f("bao cao")) == set() and await _found(admin, W, "b", "") == set()
        # attack strings stay data
        for atk in ("x'); DROP TABLE tr_runs; --", "a:* | b", "%' OR 1=1 --", "\\"):
            assert await _found(admin, W, "a", f(atk)) == set()
        assert (await sysq(A, "SELECT count(*) n FROM tr_runs WHERE project_id=%s", (pid,), one=True))["n"] >= 5
    with_world(go)

def test_encrypted_workspace_blind_index_whole_words_only_and_no_readable_word_in_the_db(monkeypatch):
    monkeypatch.setenv("PG_URI", PG)
    import aos_crypto, crypto
    async def go(A, admin, W):
        a, b = W["a"], W["b"]; pid = a["pid"]
        await sysq(A, "INSERT INTO crypto_ws(workspace_id, traces) VALUES(%s, true) ON CONFLICT(workspace_id) DO UPDATE SET traces=true", (a["ws"],))
        aos_crypto.invalidate_flags(); aos_crypto._PW.clear()
        try:
            f = lambda s: f"search({json.dumps(s)})"   # noqa: E731
            r1, _ = await run(obs, pid, inputs={"q": "Báo cáo doanh thu quý ba"}, outputs={"a": "hoàn thành"})
            row_ = await sysq(A, "SELECT inputs::text raw, search_tsv::text tsv FROM tr_runs WHERE id=%s", (r1,), one=True)
            assert "doanh" not in row_["raw"] and row_["tsv"]                                  # payload is an envelope, the index exists
            assert not any(w in row_["tsv"] for w in ("bao", "cao", "doanh", "thu", "hoan", "thanh"))   # ... and holds no readable word
            assert await _found(admin, W, "a", f("Báo cáo")) == {r1} and await _found(admin, W, "a", f("DOANH")) == {r1}   # whole words, accents folded
            assert await _found(admin, W, "a", f("doan")) == set()                                    # part of a word: unavailable
            assert await _found(admin, W, "b", f("báo cáo")) == set()                                 # another workspace: nothing
            inf = await as_user(admin, a["ws"], lambda: obs.search_info()); assert inf["encrypted"] and inf["mode"] == "whole_word" and "mã hoá" in inf["message"] and "encrypted" in inf["message"]
            assert (await as_user(admin, b["ws"], lambda: obs.search_info()))["encrypted"] is False
            # backfill (worker) re-indexes an encrypted row by decrypting it with the server key: stored lexemes stay blind
            await sysq(A, "UPDATE tr_runs SET search_tsv=NULL WHERE id=%s", (r1,))
            assert await fts.backfill(50) >= 1
            row_ = await sysq(A, "SELECT search_tsv::text tsv FROM tr_runs WHERE id=%s", (r1,), one=True); assert row_["tsv"] and "doanh" not in row_["tsv"]
            assert await _found(admin, W, "a", f("quý ba")) == {r1}
            # a row written before encryption was switched on (readable lexemes) stays findable
            await sysq(A, "UPDATE crypto_ws SET traces=false WHERE workspace_id=%s", (a["ws"],)); aos_crypto.invalidate_flags(); aos_crypto._PW.clear()
            plain, _ = await run(obs, pid, inputs={"q": "ghi chu cu"})
            await sysq(A, "UPDATE crypto_ws SET traces=true WHERE workspace_id=%s", (a["ws"],)); aos_crypto.invalidate_flags(); aos_crypto._PW.clear()
            assert await _found(admin, W, "a", f("ghi chu")) == {plain}
        finally:
            await sysq(A, "DELETE FROM crypto_ws WHERE workspace_id=%s", (a["ws"],)); await sysq(A, "DELETE FROM crypto_deks WHERE workspace_id=%s", (a["ws"],))
            aos_crypto.invalidate_flags(); aos_crypto._PW.clear(); crypto._ACT.clear()
    with_world(go)

# ================================================================ 12.16 trace filters v2 (query_syntax.py, compile_filter paths / exists / tool / scopes, POST /api/obs/runs/query)
import query_syntax as qs  # noqa: E402

PARSE_TABLE = [
    ("status:error", 'eq(status,"error")'),
    ("-tag:test", 'not(has(tags,"test"))'),
    ("tag:vip", 'has(tags,"vip")'),
    ("name:~agent", 'like(name,"%agent%")'),
    ("model:gemini*", 'like(model,"gemini%")'),
    ("metadata.customer:*", "exists(metadata.customer)"),
    ("latency:[2 TO 10]", "and(gte(latency,2), lte(latency,10))"),
    ("latency:[* TO 10]", "lte(latency,10)"),
    ("total_tokens:[100 TO *]", "gte(total_tokens,100)"),
    ("total_tokens:100", "eq(total_tokens,100)"),
    ('name:"my agent"', 'eq(name,"my agent")'),
    ("tool:web_search", 'tool("web_search")'),
    ("-tool:web_search", 'not(tool("web_search"))'),
    ("attachments:*", "has(attachments)"),
    ("metadata.a.b:5", 'eq(metadata.a.b,"5")'),
    ("inputs.user.name:~an", 'like(inputs.user.name,"%an%")'),
    ("feedback.score:1", "eq(feedback.score,1)"),
    ("feedback.k.comment:~late", 'like(feedback.k.comment,"%late%")'),
    ("feedback.k.source:human", 'eq(feedback.k.source,"human")'),
    ("depth:[1 TO 3]", "and(gte(depth,1), lte(depth,3))"),
    ("báo cáo", 'search("báo cáo")'),
    ('"báo cáo" doanh thu', 'search("báo cáo doanh thu")'),
    ("status:error báo cáo -tag:test", 'and(eq(status,"error"), not(has(tags,"test")), search("báo cáo"))'),
    ("name:~100%_x", 'like(name,"%100\\\\%\\\\_x%")'),      # LIKE wildcards typed by the user are data
    ("", ""), ("   ", ""),
]

def test_query_syntax_table_every_example_compiles_to_the_expected_dsl():
    for text, want in PARSE_TABLE:
        got = qs.parse_query(text); assert got == want, (text, got)
        obs.compile_filter(got)                                                     # ... and the DSL itself is valid
    assert qs.looks_like_query("status:error") and qs.looks_like_query("hello -tag:x") and not qs.looks_like_query("báo cáo 12:30") and not qs.looks_like_query("hello world")
    assert qs.both("", "") == "" and qs.both('eq(status,"error")', "") == 'eq(status,"error")' and qs.both("", "name:x") == 'eq(name,"x")'
    assert qs.both('eq(status,"error")', "name:x") == 'and(eq(status,"error"), eq(name,"x"))'

def test_query_syntax_errors_carry_position_and_a_did_you_mean_hint():
    with pytest.raises(qs.QueryError) as e: qs.parse_query("báo status:ok stauts:error")
    assert e.value.status_code == 400 and e.value.position == 15 and e.value.suggestion == "status" and "vị trí 15" in e.value.detail and "'status'" in e.value.detail
    with pytest.raises(qs.QueryError) as e: qs.parse_query("run_typ:llm")
    assert e.value.suggestion == "run_type"
    with pytest.raises(qs.QueryError) as e: qs.parse_query("zzzzqq:1")
    assert e.value.suggestion is None
    for bad in ("status:", "latency:[2 TO]", "name:[1 TO 2]", "latency:abc", "latency:inf", "tool:*", "tag:~x", "attachments:no", "feedback.k.comment:", "name:\"unclosed", "metadata.a:[1 TO 2]"):
        with pytest.raises(HTTPException) as e: obs.compile_filter(qs.parse_query(bad))
        assert e.value.status_code == 400, bad

def test_compile_filter_new_forms_are_parameterised_and_attacks_are_refused():
    for flt in ('eq(metadata.a.b,"100%")', 'eq(inputs.x.y,"\'; DROP TABLE tr_runs; --")', 'like(outputs.t,"%\\\\%")', 'exists(metadata.a-b.c_d)', 'tool("x\'); DROP TABLE tr_runs; --")',
                'in(feedback.k.value,"a%","b\'")', 'gt(metadata.n.x,5)', 'search("100%\'; DROP")'):
        sql, params = obs.compile_filter(flt)
        assert "DROP" not in sql and "100%" not in sql and "'; " not in sql, flt                 # user text only ever travels as a parameter
        assert sql.count("%s") == len(params), (flt, sql, params)
    sql, params = obs.compile_filter('eq(metadata.a.b,"x")'); assert sql == "((metadata #>> %s::text[]) = %s)" and params == [["a", "b"], "x"]
    sql, params = obs.compile_filter('gt(inputs.a.b,5)'); assert sql.count("%s") == 3 and params == [["a", "b"], ["a", "b"], 5.0]       # path used twice -> parameter twice
    sql, _ = obs.compile_filter('eq(metadata.k,"v")'); assert sql == "((metadata->>%s) = %s)"                                        # one level: the old shape
    for atk in ("eq(metadata.a';DROP,1)", 'eq(metadata.a.b.c.d.e.f.g,1)', 'eq(metadata..a,1)', 'eq(inputs.a b,1)', 'exists(metadata.)', 'exists(metadata.a.)', 'exists(nope)', 'eq(feedback.k.value)',
                'has(attachments,"x")', 'tool()', 'tool(1,2)', 'eq(feedback.k.score,"abc")', 'like(feedback.k,"a")'):
        with pytest.raises(HTTPException) as e: obs.compile_filter(atk)
        assert e.value.status_code == 400, atk
    with pytest.raises(HTTPException) as e: obs.compile_filter("eq(metadata.a.b.c.d.e.f.g,1)")
    assert "mã hoá" in e.value.detail and "encrypted" in e.value.detail                       # the error says encrypted rows never match inputs./outputs. paths
    obs.compile_filter('eq(metadata.a.b.c.d.e.f,1)')                                          # exactly 6 segments: fine
    for old in ('and(eq(run_type,"llm"), gt(latency,2), has(tags,"x"), search("từ"))', 'eq(metadata.agent_id,"1")', 'gte(feedback.dung,4)', 'in(model,"a","b")', 'not(eq(status,"error"))', 'like(name,"%x%")'):
        obs.compile_filter(old)                                                                # every old form still compiles

async def _q(admin, W, side, **kw):
    b = obs.QueryIn(project_id=W[side]["pid"], **kw)
    return await as_user(admin, W[side]["ws"], lambda: obs.runs_query(b))

def test_filters_on_real_rows_paths_exists_tool_attachments_feedback_depth():
    async def go(A, admin, W):
        pid, ws = W["a"]["pid"], W["a"]["ws"]
        names = lambda out: {r["name"] for r in out["runs"]}   # noqa: E731
        r1, t1 = await run(obs, pid, name="agent-1", metadata={"customer": "acme", "ctx": {"plan": {"tier": "gold"}, "n": 7}}, inputs={"user": {"name": "An", "age": 30}, "text": "hi"}, outputs={"result": {"ok": True}}, tags=["vip"])
        k1, _ = await run(obs, pid, name="web_search", run_type="tool", trace=t1, parent=r1)
        r2, t2 = await run(obs, pid, name="agent-2", metadata={"ctx": {"plan": {"tier": "free"}, "n": 12}, "revision_id": "rev-9", "ls_modality": "audio"}, inputs={"user": {"name": "Bình"}}, error="boom")
        k2, _ = await run(obs, pid, name="calculator", run_type="tool", trace=t2, parent=r2)
        r3, t3 = await run(obs, pid, name="agent-3", inputs={"messages": [{"role": "user"}]})
        await sysq(A, "INSERT INTO run_attachments(run_id,trace_id,name,mime,size,data) VALUES(%s,%s,'a.txt','text/plain',1,'x')", (r2, t2))
        await as_user(admin, ws, lambda: obs.add_feedback(obs.FbIn(run_id=r1, key="quality", score=4.0, value="good", comment="very late reply")))
        await as_user(admin, ws, lambda: obs.add_feedback(obs.FbIn(run_id=r3, key="quality", score=1.0, value="bad", comment="ok")))
        Q = lambda **kw: _q(admin, W, "a", root_only=True, **kw)   # noqa: E731
        assert names(await Q(filter='eq(metadata.ctx.plan.tier,"gold")')) == {"agent-1"}                          # nested metadata
        assert names(await Q(query="metadata.ctx.plan.tier:free")) == {"agent-2"}
        assert names(await Q(filter="gt(metadata.ctx.n,10)")) == {"agent-2"} and names(await Q(filter="lte(metadata.ctx.n,7)")) == {"agent-1"}   # numeric compare on JSON text
        assert names(await Q(filter='eq(inputs.user.name,"An")')) == {"agent-1"} and names(await Q(query="inputs.user.age:30")) == {"agent-1"}
        assert names(await Q(filter='eq(outputs.result.ok,"true")')) == {"agent-1"}
        assert names(await Q(filter="exists(inputs.user.age)")) == {"agent-1"} and names(await Q(filter="not(exists(inputs.user))")) == {"agent-3"}
        assert names(await Q(query="metadata.customer:*")) == {"agent-1"}
        assert names(await Q(filter='eq(revision_id,"rev-9")')) == {"agent-2"} and names(await Q(filter='eq(modality,"audio")')) == {"agent-2"}
        assert names(await Q(query="tool:web_search")) == {"agent-1"} and names(await Q(query="-tool:web_search")) == {"agent-2", "agent-3"}
        assert names(await Q(query="attachments:*")) == {"agent-2"} and names(await Q(filter="not(has(attachments))")) == {"agent-1", "agent-3"}
        assert names(await Q(query="feedback.quality.value:good")) == {"agent-1"} and names(await Q(query="feedback.quality.comment:~late")) == {"agent-1"}
        assert names(await Q(query="feedback.quality:1")) == {"agent-3"}            # plain feedback.<key> = score
        assert names(await Q(filter='eq(feedback.quality.source,"human")')) == {"agent-1", "agent-3"} and names(await Q(filter="exists(feedback.quality.comment)")) == {"agent-1", "agent-3"}
        assert names(await Q(filter="exists(feedback.nokey)")) == set()
        out = await _q(admin, W, "a", filter="eq(depth,1)"); assert names(out) == {"web_search", "calculator"}      # depth column
        assert names(await Q(query="status:error")) == {"agent-2"} and names(await Q(query="-status:error tag:vip")) == {"agent-1"}
        # scopes: trace_filter looks at the ROOT of the trace, tree_filter at ANY run of the tree
        out = await _q(admin, W, "a", filter='eq(run_type,"tool")', trace_filter='eq(name,"agent-1")'); assert names(out) == {"web_search"}
        out = await _q(admin, W, "a", tree_filter='eq(name,"web_search")'); assert names(out) == {"agent-1"} and all(r["parent_id"] is None for r in out["runs"])   # roots only
        out = await _q(admin, W, "a", tree_filter='tool("calculator")', root_only=False); assert names(out) == {"agent-2", "calculator"}   # explicit root_only=False: the whole matching trace
        out = await _q(admin, W, "a", filter='eq(run_type,"tool")', trace_filter='eq(status,"error")'); assert names(out) == {"calculator"}
        assert names(await _q(admin, W, "a", tree_filter='eq(name,"nothing")')) == set()
        # query + filter + select
        out = await _q(admin, W, "a", query="status:error", filter='eq(is_root,"true")', select=["name", "metadata"]); assert [r["name"] for r in out["runs"]] == ["agent-2"] and set(out["runs"][0]) == {"name", "metadata", "id", "start_time"}
        with pytest.raises(HTTPException) as e: await _q(admin, W, "a", select=["name", "password_hash; DROP TABLE x"])
        assert e.value.status_code == 400
        with pytest.raises(HTTPException) as e: await _q(admin, W, "a", cursor="not-a-cursor!!")
        assert e.value.status_code == 400
        with pytest.raises(HTTPException) as e: await _q(admin, W, "a", query="stauts:error")
        assert e.value.status_code == 400 and "status" in e.value.detail
        with pytest.raises(Exception): obs.QueryIn(project_id=1, limit=501)                                         # limit <= 500
        # old GET /api/obs/runs: same answer for the old filter, and the new optional `query`
        old = await as_user(admin, ws, lambda: obs.runs(pid, filter='eq(status,"error")')); new = await as_user(admin, ws, lambda: obs.runs(pid, query="status:error"))
        assert [r["name"] for r in old] == ["agent-2"] == [r["name"] for r in new]
        both_ = await as_user(admin, ws, lambda: obs.runs(pid, filter='eq(status,"error")', query="tag:vip")); assert both_ == []
        # trace + thread endpoints
        out = await as_user(admin, ws, lambda: obs.trace_runs(t1, filter='eq(run_type,"tool")', select="name,run_type")); assert [r["name"] for r in out["runs"]] == ["web_search"]
        out = await as_user(admin, ws, lambda: obs.trace_runs(t1, query="tool:web_search", max_runs=1)); assert [r["name"] for r in out["runs"]] == ["agent-1"]
        for bad in (str(uuid.uuid4()), "nope"):
            with pytest.raises(HTTPException) as e: await as_user(admin, ws, lambda: obs.trace_runs(bad))
            assert e.value.status_code == 404
        th = "th-" + uuid.uuid4().hex[:6]
        a1, ta1 = await run(obs, pid, thread_id=th, name="turn1"); await run(obs, pid, trace=ta1, parent=a1, name="child")
        await run(obs, pid, thread_id=th, name="turn2", start=NOW() + dt.timedelta(seconds=1))
        out = await as_user(admin, ws, lambda: obs.thread_traces(pid, th)); assert out["turns"] == 2 and [t["name"] for t in out["traces"]] == ["turn1", "turn2"] and [t["n_runs"] for t in out["traces"]] == [2, 1]
        with pytest.raises(HTTPException) as e: await as_user(admin, W["b"]["ws"], lambda: obs.thread_traces(pid, th))   # GĐ12.100c: another workspace's project = 404 (no echo of the thread id)
        assert e.value.status_code == 404
    with_world(go)

def test_cursor_paging_has_no_duplicates_or_gaps_over_1000_rows_even_with_equal_start_times():
    async def go(A, admin, W):
        pid, ws = W["a"]["pid"], W["a"]["ws"]; base = NOW() - dt.timedelta(hours=1)
        await sysq(A, """INSERT INTO tr_runs(id,trace_id,project_id,name,run_type,start_time,status)
                         SELECT u, u, %s, 'bulk', 'chain', %s::timestamptz + (g / 4) * interval '1 second', 'success' FROM (SELECT gen_random_uuid() u, g FROM generate_series(1, 1000) g) s""", (pid, base))   # groups of 4 equal times
        want = {r["id"] for r in await sysq(A, "SELECT id FROM tr_runs WHERE project_id=%s", (pid,))}; assert len(want) == 1000
        for order in ("newest", "oldest"):
            seen, cur, pages = [], None, 0
            while True:
                out = await _q(admin, W, "a", limit=70, cursor=cur, order=order, query="name:bulk"); pages += 1
                seen += [r["id"] for r in out["runs"]]; cur = out["next_cursor"]
                if not cur: break
            assert len(seen) == 1000 and set(seen) == want and len(set(seen)) == 1000 and pages == 15, (order, len(seen), pages)   # no duplicate, no gap
        out = await _q(admin, W, "a", limit=500); assert len(out["runs"]) == 500 and out["next_cursor"]
        out = await _q(admin, W, "a", limit=500, cursor=out["next_cursor"]); assert len(out["runs"]) == 500 and out["next_cursor"] is None   # exactly the end: no phantom next page
    with_world(go)

def test_rls_private_chat_trace_other_workspace_and_roles():
    async def go(A, admin, W):
        pid, ws = W["a"]["pid"], W["a"]["ws"]; th = "pv-" + uuid.uuid4().hex[:6]
        pub, tpub = await run(obs, pid, name="public-run", thread_id=th, tags=["x"])
        prv, tprv = await run(obs, pid, name="private-chat", thread_id=th, user_id=123456, tags=["x"], start=NOW() + dt.timedelta(seconds=1))   # a chat trace of user 123456
        async def names(side, role, **kw):
            b = obs.QueryIn(project_id=W[side]["pid"], **kw)
            return {r["name"] for r in (await as_user(admin, W[side]["ws"], lambda: obs.runs_query(b), role=role))["runs"]}
        assert await names("a", "admin", query="tag:x") == {"public-run", "private-chat"}                         # workspace admin sees every chat trace
        assert await names("a", "viewer", query="tag:x") == {"public-run"} and await names("a", "editor", query="tag:x") == {"public-run"}   # ... another member does not
        assert await names("a", "viewer", tree_filter='eq(name,"private-chat")') == set()                         # not through the tree / trace scopes either
        assert await names("a", "viewer", trace_filter='eq(name,"private-chat")') == set()
        assert await names("b", "admin", query="tag:x") == set() and await names("b", "admin") == set()           # workspace B: nothing of A, even asking for A's project id
        tr = await as_user(admin, ws, lambda: obs.thread_traces(pid, th), role="viewer"); assert [t["name"] for t in tr["traces"]] == ["public-run"] and tr["turns"] == 1
        with pytest.raises(HTTPException) as e: await as_user(admin, ws, lambda: obs.trace_runs(tprv), role="viewer")
        assert e.value.status_code == 404
        assert [r["name"] for r in (await as_user(admin, ws, lambda: obs.trace_runs(tprv), role="admin"))["runs"]] == ["private-chat"]
        with pytest.raises(HTTPException) as e: await as_user(admin, W["b"]["ws"], lambda: obs.trace_runs(tpub))
        assert e.value.status_code == 404
    with_world(go)
