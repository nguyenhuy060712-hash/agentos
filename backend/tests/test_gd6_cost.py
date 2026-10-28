"""GĐ6.2 detailed cost + auto-updatable pricing: gw.cost with token kinds, per-user report, metadata.cost_usd roll-up, LiteLLM refresh/apply (mocked, temp copy).
Local server :8020 (fake LLM => $0). Fake usage rows (status='t62') and the test project are removed afterwards."""
import asyncio, json, os, shutil, sys, uuid
import httpx, psycopg, pytest
from test_fixes import B, VIEWER, a  # noqa: F401

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import gateway as gw, pricing  # noqa: E402
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")

# ---------------- cost() per token kind
FAKE = {"input_per_1m": 1.0, "output_per_1m": 10.0, "cached_input_per_1m": 0.1, "reasoning_per_1m": 20.0, "audio_input_per_1m": 3.0, "audio_output_per_1m": 30.0, "image_input_per_1m": 2.0}

@pytest.fixture
def fake_price(monkeypatch):
    monkeypatch.setitem(gw.PRICING, "t62-model", dict(FAKE)); monkeypatch.setitem(gw.PRICING, "t62-plain", {"input_per_1m": 1.0, "output_per_1m": 10.0})

def test_cost_plain_keeps_old_numbers(fake_price):
    assert gw.cost("t62-model", 1_000_000, 100_000) == 2.0
    assert gw.cost("gemini-3.6-flash", 1_000_000, 1_000_000) == 4.5        # 0.75 + 3.75, unchanged by the JSON move
    assert gw.cost("gpt-5.4-nano", 2_000_000, 1_000_000) == 1.65           # 0.40 + 1.25
    assert gw.cost("nope", 1, 1) is None

def test_cost_cached_reasoning_audio_image(fake_price):
    assert gw.cost("t62-model", 1_000_000, 0, {"cached": 400_000}) == pytest.approx(0.6 * 1.0 + 0.4 * 0.1)
    assert gw.cost("t62-model", 0, 1_000_000, {"reasoning": 300_000}) == pytest.approx(0.7 * 10 + 0.3 * 20)
    assert gw.cost("t62-model", 1_000_000, 1_000_000, {"audio_in": 100_000, "audio_out": 50_000, "image_in": 200_000}) == pytest.approx(
        0.7 * 1.0 + 0.1 * 3.0 + 0.2 * 2.0 + 0.95 * 10 + 0.05 * 30)
    assert gw.cost("t62-model", 100, 100, {"cached": 10 ** 9}) == gw.cost("t62-model", 100, 100, {"cached": 100})   # clamped to the total

def test_cost_falls_back_to_plain_price(fake_price):
    d = {"cached": 500_000, "reasoning": 500_000, "audio_in": 100_000, "image_out": 1000}
    assert gw.cost("t62-plain", 1_000_000, 1_000_000, d) == gw.cost("t62-plain", 1_000_000, 1_000_000) == 11.0

def test_details_parsers():
    um = {"input_tokens": 10, "output_tokens": 5, "input_token_details": {"cache_read": 4, "cache_creation": 2, "audio": 1}, "output_token_details": {"reasoning": 3}}
    assert gw.details_from_usage(um) == {"cached": 4, "cache_write": 2, "audio_in": 1, "reasoning": 3}   # GĐ10.26: cache_creation now mapped
    assert gw.details_from_usage(None) == {}
    oa = {"prompt_tokens_details": {"cached_tokens": 7}, "completion_tokens_details": {"reasoning_tokens": 9, "audio_tokens": 2}}
    assert gw.details_from_openai(oa) == {"cached": 7, "reasoning": 9, "audio_out": 2}

# ---------------- per-user report
def _sql(q, args=()):
    with psycopg.connect(PG, autocommit=True) as c:
        cur = c.execute(q, args); return cur.fetchall() if cur.description else None

@pytest.fixture
def cleanup():
    yield
    _sql("DELETE FROM gateway_usage WHERE status='t62'")

def _uid(name): return _sql("SELECT id FROM users WHERE username=%s", (name,))[0][0]

def test_by_user_sums_equal_workspace_total(a, cleanup):
    uid = {"moza": _uid("moza"), "viewer1": _uid("viewer1"), None: None}
    ws = _sql("SELECT workspace_id FROM memberships WHERE user_id=%s ORDER BY workspace_id LIMIT 1", (uid["moza"],))[0][0]
    for u, cst, tok in (("moza", 0.30, 5), ("moza", 0.20, 7), ("viewer1", 0.125, 3), (None, 0.05, 1)):
        for w in (987654, ws):   # foreign workspace 987654 must NOT appear in our workspace report
            _sql("INSERT INTO gateway_usage(ts,model,input_tokens,output_tokens,cost_usd,status,workspace_id,user_id,cached_tokens) VALUES(now(),'gemini-3.6-flash',10,10,%s,'t62',%s,%s,%s)",
                 (cst, w, uid[u], tok))
    r = a.get("/api/gw/overview", params={"hours": 1}); assert r.status_code == 200, r.text
    j = r.json(); by = {u["username"]: u for u in j["by_user"]}
    assert by["moza"]["cost"] >= 0.5 and by["moza"]["cached_tok"] >= 12 and by["viewer1"]["cost"] >= 0.125
    assert any(u["user_id"] is None for u in j["by_user"])
    tot = float(_sql("SELECT coalesce(sum(cost_usd),0) FROM gateway_usage WHERE workspace_id=%s AND ts > now() - interval '1 hour'", (ws,))[0][0])
    assert j["by_user_scope"] == "workspace" and abs(sum(u["cost"] for u in j["by_user"]) - tot) < 1e-6 and abs(j["by_user_total"] - tot) < 1e-6

def test_by_user_non_admin_sees_only_self(cleanup):
    uid = _uid("viewer1")
    ws = _sql("SELECT workspace_id FROM memberships WHERE user_id=%s ORDER BY workspace_id LIMIT 1", (uid,))[0][0]
    _sql("INSERT INTO gateway_usage(ts,model,input_tokens,output_tokens,cost_usd,status,workspace_id,user_id) VALUES(now(),'gemini-3.6-flash',1,1,0.01,'t62',%s,%s)", (ws, uid))
    _sql("INSERT INTO gateway_usage(ts,model,input_tokens,output_tokens,cost_usd,status,workspace_id,user_id) VALUES(now(),'gemini-3.6-flash',1,1,0.02,'t62',%s,NULL)", (ws,))
    with httpx.Client(base_url=B, auth=VIEWER, timeout=60) as v:
        j = v.get("/api/gw/overview", params={"hours": 1}).json()
    assert j["by_user_scope"] == "self" and {u["username"] for u in j["by_user"]} == {"viewer1"}

# ---------------- metadata.cost_usd counted once
def test_metadata_cost_counted_once(a):
    p = a.post("/api/obs/projects", json={"name": "t62-" + uuid.uuid4().hex[:6]}).json()
    key = a.post(f"/api/obs/projects/{p['id']}/keys").json()["api_key"]
    tid, lid, t1, t2 = (str(uuid.uuid4()) for _ in range(4))
    now = "2026-10-22T10:00:00+00:00"
    post = [{"id": tid, "trace_id": tid, "name": "root", "run_type": "chain", "start_time": now, "end_time": now},
            {"id": lid, "trace_id": tid, "parent_run_id": tid, "name": "llm", "run_type": "llm", "start_time": now, "end_time": now, "model": "gemini-3.6-flash",
             "usage": {"input_tokens": 1000, "output_tokens": 1000}, "cost_usd": 0.01},
            {"id": t1, "trace_id": tid, "parent_run_id": tid, "name": "geocode", "run_type": "tool", "start_time": now, "end_time": now, "extra": {"metadata": {"cost_usd": 0.05}}},
            {"id": t2, "trace_id": tid, "parent_run_id": tid, "name": "sms", "run_type": "tool", "start_time": now, "end_time": now, "metadata": {"cost_usd": 0.02}}]
    try:
        with httpx.Client(base_url=B, timeout=60, headers={"x-api-key": key}) as ing:
            for body in ({"post": post}, {"patch": [{"id": t1, "trace_id": tid, "end_time": now}]}, {"patch": post[1:]}):   # re-sending must not double count
                assert ing.post("/ingest/runs", json=body).status_code == 200
        runs = {r["id"]: r for r in a.get(f"/api/obs/traces/{tid}").json()["runs"]}
        assert float(runs[t1]["cost_usd"]) == 0.05 and float(runs[t2]["cost_usd"]) == 0.02
        assert float(runs[tid]["cost_usd"]) == pytest.approx(0.08)    # 0.01 llm + 0.05 + 0.02, each run once
    finally:
        _sql("DELETE FROM tr_runs WHERE trace_id=%s", (tid,)); _sql("DELETE FROM projects WHERE id=%s", (p["id"],))

# ---------------- LiteLLM refresh / apply (mocked fetch, temp copy)
LIT = {"gemini/gemini-3.6-flash": {"input_cost_per_token": 0.75e-6, "output_cost_per_token": 4.0e-6, "cache_read_input_token_cost": 0.075e-6},   # output changed, cached new
       "gpt-5-mini": {"input_cost_per_token": 0.25e-6, "output_cost_per_token": 2.0e-6, "output_cost_per_reasoning_token": 2.0e-6},
       "some-other-model": {"input_cost_per_token": 1e-6}}

def test_refresh_diff_and_apply_temp_copy(tmp_path, monkeypatch):
    path, pend = tmp_path / "pricing.json", tmp_path / "pending.json"
    shutil.copy(pricing.PATH, path); orig = path.read_text(encoding="utf-8")
    async def fake(): return LIT
    monkeypatch.setattr(pricing, "fetch_litellm", fake)
    d = asyncio.run(pricing.refresh(path, pend))
    assert d["changes"]["gemini-3.6-flash"] == {"output_per_1m": {"old": 3.75, "new": 4.0}, "cached_input_per_1m": {"old": None, "new": 0.075}}
    assert d["changes"]["gpt-5-mini"] == {"reasoning_per_1m": {"old": None, "new": 2.0}}
    assert "some-other-model" not in d["changes"] and "gpt-4o-mini" in d["not_found"]
    assert path.read_text(encoding="utf-8") == orig and pricing.pending(pend)["changes"] == d["changes"]    # refresh never writes the price file
    r = pricing.apply(path, pend)
    assert r["applied_models"] == 2 and not pend.exists()
    new = json.loads(path.read_text(encoding="utf-8"))["models"]
    assert new["gemini-3.6-flash"]["output_per_1m"] == 4.0 and new["gemini-3.6-flash"]["source"] == "litellm" and new["gemini-3.6-flash"]["checked"] == r["checked"]
    assert new["gemini-3.8-flash"] == json.loads(orig)["models"]["gemini-3.8-flash"]    # untouched model unchanged
    assert pricing.apply(path, pend) is None
    try:
        gw.reload_pricing(path); assert gw.PRICING["gemini-3.6-flash"]["output_per_1m"] == 4.0
    finally:
        gw.reload_pricing()      # restore the real file
    assert gw.PRICING["gemini-3.6-flash"]["output_per_1m"] == 3.75

def test_pricing_endpoints_admin_only(a):
    with httpx.Client(base_url=B, auth=VIEWER, timeout=60) as v:
        assert v.post("/api/gateway/pricing/refresh").status_code == 403
        assert v.post("/api/gateway/pricing/apply").status_code == 403
        assert v.get("/api/gateway/pricing").status_code == 403
    r = a.get("/api/gateway/pricing"); assert r.status_code == 200 and "gemini-3.6-flash" in r.json()["models"]
