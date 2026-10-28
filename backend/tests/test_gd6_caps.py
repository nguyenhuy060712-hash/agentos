"""GĐ6.1 unified spend caps: table spend_caps, gw.budget_check(ctx), admin API /api/gateway/caps, 402 for a run.
Local server :8020, fake LLM => $0. Fake usage rows (status='t61', ids 987xxx) are inserted directly and removed afterwards."""
import asyncio, os, re, sys
import httpx, psycopg, pytest
from test_fixes import B, VIEWER, a  # noqa: F401  (a = admin client fixture)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
U, E, W, WP, FEAT = 987001, 987002, 987003, 987004, "t61-feature"
MSG = re.compile(r"^Đã chạm trần chi phí .+\(đã dùng \$\d+\.\d\d / trần \$\d+\.\d\d .+\)\. Liên hệ quản trị viên để nâng trần\.$")
TZ = "Asia/Ho_Chi_Minh"

@pytest.fixture
def caps(a):
    made = []
    def mk(**kw):
        r = a.post("/api/gateway/caps", json=kw); assert r.status_code == 200, r.text
        made.append(r.json()["id"]); return r.json()
    yield mk
    for i in made: a.delete(f"/api/gateway/caps/{i}")
    with psycopg.connect(PG, autocommit=True) as c: c.execute("DELETE FROM gateway_usage WHERE status='t61'")

def usage(cost=0.5, ts="now()", model="gemini-3.8-flash", **cols):
    cols = {"workspace_id": None, "user_id": None, "evaluator_id": None, "feature": None, **cols}
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f"INSERT INTO gateway_usage(ts,model,input_tokens,output_tokens,cost_usd,status,workspace_id,user_id,evaluator_id,feature) VALUES({ts},%s,0,0,%s,'t61',%s,%s,%s,%s)",
                  (model, cost, cols["workspace_id"], cols["user_id"], cols["evaluator_id"], cols["feature"]))

def check(a, **ctx): return a.post("/api/gateway/caps/check", json=ctx)

SCOPES = [  # scope, target, extra cap fields, usage columns, matching ctx, non-matching ctx
    ("provider", "gemini", {"workspace_id": WP}, {"workspace_id": WP}, {"provider": "gemini", "workspace_id": WP}, {"provider": "gemini", "workspace_id": WP + 1}),
    ("workspace", str(W), {}, {"workspace_id": W}, {"workspace_id": W}, {"workspace_id": W + 1}),
    ("user", str(U), {}, {"user_id": U}, {"user_id": U}, {"user_id": U + 1}),
    ("evaluator", str(E), {}, {"evaluator_id": E}, {"evaluator_id": E}, {"evaluator_id": E + 1}),
    ("feature", FEAT, {}, {"feature": FEAT}, {"feature": FEAT}, {"feature": "other"}),
]

@pytest.mark.parametrize("scope,target,extra,cols,ctx,other", SCOPES, ids=[s[0] for s in SCOPES])
def test_scope_block_then_warn(a, caps, scope, target, extra, cols, ctx, other):
    usage(0.5, **cols)
    c = caps(scope=scope, target=target, period="total", limit_usd=0.4, action="block", **extra)
    assert c["spent"] >= 0.5 and c["reached"] is True
    r = check(a, **ctx); assert r.status_code == 402 and MSG.match(r.json()["detail"]), r.text
    assert check(a, **other).status_code == 200                               # other target / workspace: not blocked
    r = a.patch(f"/api/gateway/caps/{c['id']}", json={"action": "warn"}); assert r.status_code == 200
    r = check(a, **ctx); assert r.status_code == 200 and r.json()["warnings"], r.text   # warn: allowed, reported
    a.patch(f"/api/gateway/caps/{c['id']}", json={"action": "block", "enabled": False})
    r = check(a, **ctx); assert r.status_code == 200 and not r.json()["warnings"]       # disabled: ignored
    a.patch(f"/api/gateway/caps/{c['id']}", json={"enabled": True, "limit_usd": 5})
    assert check(a, **ctx).status_code == 200                                 # raised limit: under again

def test_period_boundaries_day_vs_month(a, caps):
    with psycopg.connect(PG) as c:
        day1 = c.execute("SELECT extract(day from now() AT TIME ZONE %s)=1", (TZ,)).fetchone()[0]
    yesterday = f"date_trunc('day', now() AT TIME ZONE '{TZ}') AT TIME ZONE '{TZ}' - interval '1 hour'"   # 23:00 ICT yesterday: never "today"
    usage(1.0, ts=yesterday, user_id=U); usage(1.0, ts="now() - interval '40 days'", user_id=U)
    d = caps(scope="user", target=str(U), period="day", limit_usd=0.5)
    m = caps(scope="user", target=str(U), period="month", limit_usd=0.5)
    t = caps(scope="user", target=str(U), period="total", limit_usd=1.5)
    assert d["spent"] == 0 and m["spent"] == (0 if day1 else 1.0) and t["spent"] == 2.0
    assert check(a, user_id=U).status_code == 402                                # total cap (2.0 >= 1.5)
    a.patch(f"/api/gateway/caps/{t['id']}", json={"enabled": False})
    assert check(a, user_id=U).status_code == (200 if day1 else 402)             # yesterday counts for the month cap, not for the day cap
    a.patch(f"/api/gateway/caps/{m['id']}", json={"enabled": False})
    assert check(a, user_id=U).status_code == 200                                # only the day cap left: yesterday + 40 days ago do not count
    usage(0.6, user_id=U)                                                        # today
    assert check(a, user_id=U).status_code == 402

def test_evaluator_weekly_cap(a, caps):
    usage(2.0, ts="now() - interval '10 days'", evaluator_id=E)
    c1 = caps(scope="evaluator", target=str(E), period="week", limit_usd=1.0)
    assert c1["spent"] == 0 and check(a, evaluator_id=E).status_code == 200      # 10 days ago is outside the week
    usage(1.2, evaluator_id=E)
    r = check(a, evaluator_id=E); assert r.status_code == 402 and "mỗi tuần" in r.json()["detail"], r.text

def test_legacy_provider_cap_still_blocks():
    import gateway as gw
    caps, spent = dict(gw.CAPS), dict(gw.SPENT)
    try:
        gw.CAPS["openai"] = 1.0; gw.SPENT["openai"] = 1.2; gw.SPENT["gemini"] = 0.0
        with pytest.raises(gw.BudgetExceeded) as ei: asyncio.run(gw.budget_check({"provider": "openai"}))
        assert str(ei.value).startswith("Đã chạm trần chi phí OpenAI (đã dùng $1.20 / trần $1.00)") and isinstance(ei.value, gw.SpendCapReached)
        assert asyncio.run(gw.budget_check({"provider": ["openai", "gemini"]})) == []   # Gemini fallback still allowed
        assert not gw.under_cap("gpt-5.4-nano") and gw.under_cap("gemini-3.8-flash")
    finally:
        gw.CAPS.clear(); gw.CAPS.update(caps); gw.SPENT.clear(); gw.SPENT.update(spent)

def test_workspace_cap_gives_402_before_a_chat_run(a, caps):
    usage(0.02, workspace_id=1)
    c = caps(scope="workspace", target="1", period="total", limit_usd=0.01)
    assert c["reached"]
    tid = a.post("/api/v1/threads", json={"metadata": {"source": "chat", "graph_id": "agent-2"}}).json()["thread_id"]
    body = {"assistant_id": "agent-2", "input": {"messages": [{"type": "human", "content": "xin chào"}]}}
    for path in ("wait", "stream"):
        r = a.post(f"/api/v1/threads/{tid}/runs/{path}", json=body)
        assert r.status_code == 402 and r.json()["detail"]["code"] == "spend_cap" and r.json()["detail"]["message"].startswith("Đã chạm trần chi phí workspace 1"), r.text   # 8.P: structured 402
    a.delete(f"/api/gateway/caps/{c['id']}")
    r = a.post(f"/api/v1/threads/{tid}/runs/wait", json=body); assert r.status_code == 200, r.text   # cap removed: runs again

def test_caps_crud_and_validation(a, caps):
    c = caps(scope="feature", target="t61-crud", period="month", limit_usd=3, action="warn")
    assert c["enabled"] and c["limit_usd"] == 3.0 and c["spent"] == 0 and c["source"] == "db"
    assert any(x["id"] == c["id"] for x in a.get("/api/gateway/caps").json()["caps"])
    assert "openai" in a.get("/api/gateway/caps").json()                         # legacy provider summary kept
    r = a.patch(f"/api/gateway/caps/{c['id']}", json={"limit_usd": 4}); assert r.json()["limit_usd"] == 4.0
    assert a.post("/api/gateway/caps", json={"scope": "bogus", "target": "x", "period": "day", "limit_usd": 1}).status_code == 422
    assert a.post("/api/gateway/caps", json={"scope": "user", "target": "abc", "period": "day", "limit_usd": 1}).status_code == 422
    assert a.patch("/api/gateway/caps/99999999", json={"limit_usd": 1}).status_code == 404
    assert a.delete("/api/gateway/caps/99999999").status_code == 404

def test_non_admin_cannot_write_caps(a, caps):
    c = caps(scope="feature", target="t61-viewer", period="day", limit_usd=1)
    with httpx.Client(base_url=B, auth=VIEWER, timeout=30) as v:
        assert v.post("/api/gateway/caps", json={"scope": "feature", "target": "x", "period": "day", "limit_usd": 1}).status_code == 403
        assert v.patch(f"/api/gateway/caps/{c['id']}", json={"limit_usd": 9}).status_code == 403
        assert v.delete(f"/api/gateway/caps/{c['id']}").status_code == 403
        assert v.get("/api/gateway/caps").status_code == 403
        assert v.post("/api/gateway/caps/check", json={}).status_code == 403
    assert a.get("/api/gateway/caps").json()["caps"]                              # still there, unchanged
