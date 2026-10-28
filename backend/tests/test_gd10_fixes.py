"""GĐ10 fixes (cards 10.4 10.26 10.18 ...): /v1/models, cache-write + long-context cost, get_thread paging.
Local server :8020 (fake LLM => $0). Each card's tests sit under their own `# --- 10.x ---` comment."""
import asyncio, base64, json, os, sys, uuid
import httpx, psycopg, pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from test_fixes import B, ADMIN, a  # noqa: F401

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import gateway as gw  # noqa: E402
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
SUF = uuid.uuid4().hex[:6]

# --- 10.4 --- /v1/models lists only callable models
def _key(a, **kw):
    r = a.post("/api/gw/keys", json={"name": f"gd10-{SUF}-{uuid.uuid4().hex[:4]}", **kw}); assert r.status_code == 200, r.text
    return r.json()

def _models(k):
    r = httpx.get(B + "/v1/models", headers={"Authorization": "Bearer " + k["key"]}, timeout=30); assert r.status_code == 200, r.text
    return [m["id"] for m in r.json()["data"]]

def test_models_list_allow_list_exact(a):
    k = _key(a, models=["gemini-3.6-flash"])
    try: assert _models(k) == ["gemini-3.6-flash"]
    finally: a.delete(f"/api/gw/keys/{k['id']}")

def test_models_list_no_allow_list_is_gemini_only(a):
    k = _key(a)
    try:
        ms = _models(k)
        assert ms and all(m.startswith("gemini-") for m in ms) and "gemini-3.6-flash" in ms
        assert not [m for m in ms if m.startswith(("gpt-", "claude-", "o3", "o4"))]
    finally: a.delete(f"/api/gw/keys/{k['id']}")

# --- 10.26 --- cache-write tokens and long context
CW = {"input_per_1m": 1.0, "output_per_1m": 10.0, "cached_input_per_1m": 0.1, "cache_write_per_1m": 1.25,
      "long_context_tokens": 200000, "input_per_1m_long": 2.0, "output_per_1m_long": 15.0}

@pytest.fixture
def cw_price(monkeypatch):
    monkeypatch.setitem(gw.PRICING, "t1026-model", dict(CW)); monkeypatch.setitem(gw.PRICING, "t1026-plain", {"input_per_1m": 1.0, "output_per_1m": 10.0})

def test_cost_cache_write_own_rate(cw_price):
    assert gw.cost("t1026-model", 100_000, 0, {"cache_write": 40_000}) == pytest.approx(0.06 * 1.0 + 0.04 * 1.25)
    assert gw.cost("t1026-model", 100_000, 0, {"cached": 10_000, "cache_write": 40_000}) == pytest.approx(0.05 * 1.0 + 0.01 * 0.1 + 0.04 * 1.25)

def test_cost_models_without_new_fields_keep_old_numbers(cw_price):
    assert gw.cost("t1026-plain", 1_000_000, 1_000_000, {"cache_write": 500_000}) == gw.cost("t1026-plain", 1_000_000, 1_000_000) == 11.0
    assert gw.cost("t1026-plain", 300_000, 1_000_000) == pytest.approx(0.3 + 10.0)   # no long fields => no long price even above 200k
    assert gw.cost("gemini-3.6-flash", 1_000_000, 1_000_000) == 4.5

def test_cost_long_context_whole_call(cw_price):
    assert gw.cost("t1026-model", 200_000, 100_000) == pytest.approx(0.2 * 1.0 + 0.1 * 10.0)        # at the threshold: still normal
    assert gw.cost("t1026-model", 300_000, 100_000) == pytest.approx(0.3 * 2.0 + 0.1 * 15.0)        # above: input AND output use long prices
    assert gw.cost("t1026-model", 300_000, 0, {"cache_write": 100_000}) == pytest.approx(0.2 * 2.0 + 0.1 * 1.25)   # cache_write has its own explicit rate

def test_details_parsers_cache_write():
    um = {"input_token_details": {"cache_read": 4, "cache_creation": 2}}
    assert gw.details_from_usage(um) == {"cached": 4, "cache_write": 2}
    assert gw.details_from_openai({"prompt_tokens_details": {"cached_tokens": 7, "cache_write_tokens": 3}}) == {"cached": 7, "cache_write": 3}
    assert "cache_write_per_1m" in __import__("pricing").LITELLM_FIELDS and "input_per_1m_long" in __import__("pricing").LITELLM_FIELDS

# --- 10.18 --- get_thread reads long conversations in pages
def _basic(u, p): return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}

async def _mcp(name, args):
    async with httpx.AsyncClient(headers=_basic(*ADMIN), timeout=60) as hc, streamable_http_client(B + "/mcp/aos", http_client=hc) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize(); return await s.call_tool(name, args)

def _thread(**args):
    r = asyncio.run(_mcp("get_thread", args)); assert not r.is_error, r
    return json.loads("".join(c.text for c in r.content if getattr(c, "type", "") == "text"))

def test_get_thread_pages_through_50_turns(a):
    tid = f"gd10-18-{SUF}"
    pid = next(p["id"] for p in a.get("/api/obs/projects").json() if p["name"] == "agents")
    with psycopg.connect(PG, autocommit=True) as pg:
        try:
            for n in range(50):
                pg.execute("INSERT INTO tr_runs(id,trace_id,project_id,name,run_type,start_time,status,inputs,thread_id) VALUES(%s,%s,%s,'turn','chain',now() - interval '1 hour' + %s * interval '1 second','success',%s::jsonb,%s)",
                           (str(uuid.uuid4()), str(uuid.uuid4()), pid, n, json.dumps({"q": f"turn {n}"}), tid))
            seen, off, calls = [], 0, 0
            while off is not None and calls < 10:
                d = _thread(thread_id=tid, limit=20, offset=off); calls += 1
                seen += [i["in"] for i in d["items"]]; off = d["next_offset"]
            assert calls == 3 and len(seen) == 50 and len(set(seen)) == 50
            assert seen == [json.dumps({"q": f"turn {n}"}) for n in range(50)]
        finally:
            pg.execute("DELETE FROM tr_runs WHERE thread_id=%s", (tid,))

# --- 10.14 --- token feedback stores correction and metadata
# In-process (helpers of test_gd10_security: private pool on the local DB, two seeded workspaces, fake request); no server, no network.
def _fb14():
    from test_gd10_security import with_db28, Req, sysq, seen_by, as_user  # noqa: F401
    return with_db28, Req, sysq, seen_by, as_user

async def _token(A, admin, rid, max_uses=3, hours=1):
    t = "fbt_" + uuid.uuid4().hex
    await A.q("INSERT INTO feedback_tokens(token_hash,run_id,key,expires,max_uses) VALUES(%s,%s,'thumb',now()+%s*interval '1 hour',%s)", (admin._h(t), rid, hours, max_uses))
    return t

def test_token_feedback_stores_correction_and_metadata():
    with_db28, Req, sysq, seen_by, as_user = _fb14()
    import ext, obs
    from fastapi import HTTPException
    async def go(A, admin, W):
        a, b = W["a"], W["b"]; rid = uuid.uuid4()
        await obs.upsert_run(a["pid"], {"id": str(rid), "name": "fb14", "start_time": 1.0e9, "end_time": 1.0e9 + 1})
        corr, meta = {"score": 0.2, "comment": "wrong answer", "value": "bad"}, {"source": "widget", "page": "/pricing", "tags": ["a", "b"], "n": 3}
        tok = await _token(A, admin, rid)
        r = await ext.fb_public(tok, Req({"score": 1, "comment": "thanks", "correction": corr, "metadata": meta}, {"content-type": "application/json"}))
        row = (await seen_by(A, admin, a["ws"], "SELECT correction, metadata, comment, project_id, source FROM feedback WHERE id=%s", (r["feedback_id"],)))[0]
        assert row == {"correction": corr, "metadata": meta, "comment": "thanks", "project_id": a["pid"], "source": "presigned"}
        # readable through the feedback API (the trace view lists the feedback of the trace); the other workspace cannot read it
        fb = [x for x in (await as_user(admin, a["ws"], lambda: obs.trace(str(rid))))["feedback"] if x["id"] == r["feedback_id"]]
        assert len(fb) == 1 and fb[0]["correction"] == corr and fb[0]["metadata"] == meta
        assert await seen_by(A, admin, b["ws"], "SELECT 1 FROM feedback WHERE id=%s", (r["feedback_id"],)) == []
        # GET keeps score / value / comment only: nothing else can be passed, nothing else is stored
        tok2 = await _token(A, admin, rid)
        r2 = await ext.fb_public(tok2, Req(method="GET"), score=1.0, comment="get")
        row = (await seen_by(A, admin, a["ws"], "SELECT correction, metadata, comment FROM feedback WHERE id=%s", (r2["feedback_id"],)))[0]
        assert row == {"correction": None, "metadata": None, "comment": "get"}
        # POST without them still works (old clients)
        r3 = await ext.fb_public(await _token(A, admin, rid), Req({"score": 0}, {}))
        assert (await seen_by(A, admin, a["ws"], "SELECT correction, metadata FROM feedback WHERE id=%s", (r3["feedback_id"],)))[0] == {"correction": None, "metadata": None}
        # the ingest API takes metadata too
        r4 = await obs.ingest_feedback(obs.FbIn(run_id=str(rid), key="thumb", score=1.0, metadata={"k": "v"}, correction={"score": 1}), x_api_key=a["ing"])
        assert (await seen_by(A, admin, a["ws"], "SELECT correction, metadata FROM feedback WHERE id=%s", (r4["id"],)))[0] == {"correction": {"score": 1}, "metadata": {"k": "v"}}
        await sysq(A, "DELETE FROM feedback_tokens WHERE run_id=%s", (rid,))
    with_db28(go)

def test_token_feedback_rejects_oversize_and_bad_shapes_without_spending_the_token():
    with_db28, Req, sysq, seen_by, as_user = _fb14()
    import ext, obs
    from fastapi import HTTPException
    from pydantic import ValidationError
    async def go(A, admin, W):
        a = W["a"]; rid = uuid.uuid4()
        await obs.upsert_run(a["pid"], {"id": str(rid), "name": "fb14b", "start_time": 1.0e9, "end_time": 1.0e9 + 1})
        tok = await _token(A, admin, rid, max_uses=10)
        post = lambda body: ext.fb_public(tok, Req(body, {"content-type": "application/json"}))
        big = lambda total: {"x": "a" * (total - 9)}          # a dict whose JSON text is exactly `total` characters
        assert len(json.dumps(big(8192))) == 8192
        for body, code in (({"score": 1, "correction": big(8193)}, 413),                    # 1 over the 8 KB limit
                           ({"score": 1, "metadata": big(4097)}, 413),                      # 1 over the 4 KB limit
                           ({"score": 1, "correction": big(9000)}, 413), ({"score": 1, "metadata": big(9000)}, 413),
                           ({"score": 1, "correction": big(50000)}, 413),                   # whole body too large
                           ({"score": 1, "comment": "c" * 40000}, 413),                     # whole body too large
                           ({"score": 1, "correction": "text"}, 400), ({"score": 1, "metadata": [1, 2]}, 400), ({"score": 1, "correction": 5}, 400)):
            with pytest.raises(HTTPException) as e: await post(body)
            assert e.value.status_code == code, (str(body)[:60], e.value.status_code)
        assert (await sysq(A, "SELECT uses FROM feedback_tokens WHERE token_hash=%s", (admin._h(tok),)))[0]["uses"] == 0     # a rejected request spends nothing
        assert await sysq(A, "SELECT 1 FROM feedback WHERE run_id=%s", (rid,)) == []                                          # and stores nothing
        r = await post({"score": 1, "correction": big(8192), "metadata": big(4096)})                                       # exactly at the limits: accepted
        got = (await sysq(A, "SELECT correction, metadata FROM feedback WHERE id=%s", (r["feedback_id"],)))[0]
        assert got == {"correction": big(8192), "metadata": big(4096)}
        obs.FbIn(run_id=str(rid), key="thumb", metadata=big(4096))
        with pytest.raises(ValidationError): obs.FbIn(run_id=str(rid), key="thumb", metadata=big(4097))                    # the ingest model refuses an oversize metadata too
        # NEGATIVE: expired / used-up / unknown token with a correction -> 403, nothing stored
        old = await _token(A, admin, rid, hours=-1)
        one = await _token(A, admin, rid, max_uses=1)
        await ext.fb_public(one, Req({"score": 1}, {}))
        n0 = (await sysq(A, "SELECT count(*) c FROM feedback WHERE run_id=%s", (rid,)))[0]["c"]
        for t in (old, one, "fbt_unknown"):
            with pytest.raises(HTTPException) as e: await ext.fb_public(t, Req({"score": 1, "correction": {"score": 0}, "metadata": {"a": 1}}, {}))
            assert e.value.status_code == 403
        assert (await sysq(A, "SELECT count(*) c FROM feedback WHERE run_id=%s", (rid,)))[0]["c"] == n0
        await sysq(A, "DELETE FROM feedback_tokens WHERE run_id=%s", (rid,))
    with_db28(go)

def test_migration_0004_adds_and_drops_the_metadata_column():
    import migrations
    m0004 = next(m for v, m, _ in migrations._load() if v == "0004")
    col = "SELECT 1 FROM information_schema.columns WHERE table_name='feedback' AND column_name='metadata'"
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as conn:
            await migrations.run_pending(conn); q = migrations._q(conn)
            assert (await q(col)) and "0004" in {r[0] for r in await q("SELECT version FROM schema_migrations")}
            async with conn.transaction(force_rollback=True):
                await m0004.down(q); assert not (await q(col))
                await m0004.up(q); assert (await q(col))
            assert (await q(col)) and (await migrations.run_pending(conn, dry_run=True)) == []
    asyncio.run(go())
