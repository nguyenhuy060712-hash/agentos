"""GĐ12.5 chart query engine. In-process on the local DB (private pool under the app's RLS role, two throw-away workspaces, rows inserted straight into tr_runs so
every number can be hand-calculated). Covers: metric x agg x scope against a pure-Python calculation, buckets / empty = NULL, ratio, group_by (+ other), feedback,
statement timeout, RLS (other workspace, private chat trace), hostile input -> 422, SQL shape (values are parameters), templates."""
import asyncio, datetime as dt, itertools, os, sys, uuid
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from gd12_world import NOW, as_user, sysq, with_world

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import deepagent_api  # noqa: E402,F401  (import order: deepagent_api first)
import charts  # noqa: E402

H = dt.timedelta(hours=1); M = dt.timedelta(minutes=1)
T0 = NOW().replace(minute=0, second=0, microsecond=0) - 5 * H   # window [T0, T0+4h): 4 hour buckets, whole window in the past
END = T0 + 4 * H

# id, parent, minute offset from T0, run_type, latency ms, status, in/out tokens, cost, in/out cost, first token s, usage_details, cost_details, model, tags
R = [
    dict(k="r1", parent=None, at=10 * M, run_type="chain", lat=1000, status="success", it=100, ot=50, cost=0.010, ic=0.004, oc=0.006, ftt=0.2, ud={"cache_read": 20, "reasoning": 5}, cd={"cache_read": 0.001}, model="m1", tags=["x"]),
    dict(k="c1", parent="r1", at=10 * M + dt.timedelta(seconds=1), run_type="llm", lat=400, status="success", it=60, ot=30, cost=0.005, ic=None, oc=None, ftt=None, ud={"cache_read": 10}, cd=None, model="m1", tags=[]),
    dict(k="r2", parent=None, at=20 * M, run_type="chain", lat=3000, status="error", it=200, ot=100, cost=0.030, ic=None, oc=None, ftt=0.6, ud=None, cd=None, model="m2", tags=["x", "y"]),
    dict(k="r3", parent=None, at=70 * M, run_type="chain", lat=2000, status="success", it=10, ot=10, cost=0.020, ic=None, oc=None, ftt=None, ud=None, cd=None, model="m2", tags=[]),
]

async def seed_runs(A, pid, rows=R, user_id=None, tag=""):
    ids = {}
    for r in rows:
        ids[r["k"]] = str(uuid.uuid4())
    for r in rows:
        st = T0 + r["at"]; rid = ids[r["k"]]; tr = ids[r["parent"] or r["k"]]
        await sysq(A, """INSERT INTO tr_runs(id,trace_id,parent_id,project_id,name,run_type,start_time,end_time,latency_ms,status,metadata,tags,input_tokens,output_tokens,total_tokens,
            cost_usd,model,first_token_time,usage_details,cost_details,input_cost,output_cost,user_id) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                   (rid, tr, ids[r["parent"]] if r["parent"] else None, pid, r["k"] + tag, r["run_type"], st, st + dt.timedelta(milliseconds=r["lat"]), r["lat"], r["status"],
                    Jsonb({"ls_model_name": r["model"]}), r["tags"], r["it"], r["ot"], r["it"] + r["ot"], r["cost"], r["model"],
                    st + dt.timedelta(seconds=r["ftt"]) if r["ftt"] else None, Jsonb(r["ud"]) if r["ud"] else None, Jsonb(r["cd"]) if r["cd"] else None, r["ic"], r["oc"], user_id))
    return ids

def spec(**kw):
    kw.setdefault("series", [{"metric": "count"}]); kw.setdefault("bucket", "hour")
    return charts.ChartSpec.model_validate(kw)

async def q(admin, ws, sp, role="admin", **kw):
    charts._CACHE.clear()
    return await as_user(admin, ws, lambda: charts.run(sp, kw.get("start", T0), kw.get("end", END), kw.get("tz", "UTC")), role=role)

def pct(vals, p):
    v = sorted(vals); k = (len(v) - 1) * p; f = int(k)
    return v[f] if f == k else v[f] + (v[f + 1] - v[f]) * (k - f)

AGGS = {"count": len, "avg": lambda v: sum(v) / len(v), "sum": sum, "min": min, "max": max,
        "p50": lambda v: pct(v, .5), "p90": lambda v: pct(v, .9), "p95": lambda v: pct(v, .95), "p99": lambda v: pct(v, .99)}
COL = {("latency", "total"): lambda r: r["lat"], ("ttft", "total"): lambda r: r["ftt"] * 1000 if r["ftt"] else None,
       ("tokens", "total"): lambda r: r["it"] + r["ot"], ("tokens", "input"): lambda r: r["it"], ("tokens", "output"): lambda r: r["ot"],
       ("tokens", "cache_read"): lambda r: (r["ud"] or {}).get("cache_read"), ("tokens", "reasoning"): lambda r: (r["ud"] or {}).get("reasoning"),
       ("cost", "total"): lambda r: r["cost"], ("cost", "input"): lambda r: r["ic"], ("cost", "output"): lambda r: r["oc"],
       ("cost", "cache_read"): lambda r: (r["cd"] or {}).get("cache_read")}

def scope_rows(scope):
    if scope == "run": return R
    roots = [r for r in R if r["parent"] is None]
    if scope == "trace": return roots
    llm_traces = {r["parent"] or r["k"] for r in R if r["run_type"] == "llm"}
    return [r for r in roots if r["k"] in llm_traces]

# ---------------------------------------------------------------- numbers
def test_every_metric_agg_scope_matches_hand_calculation():
    async def go(A, admin, W):
        a = W["a"]; await seed_runs(A, a["pid"])
        n = 0
        for scope, ((metric, field), colf) in itertools.product(("run", "trace", "tree"), COL.items()):
            for agg, fn in AGGS.items():
                vals = [v for v in (colf(r) for r in scope_rows(scope)) if v is not None]
                want = fn(vals) if vals else None
                s = {"metric": metric, "field": field, "agg": agg, "scope": scope, **({"filter": 'eq(run_type,"llm")'} if scope == "tree" else {})}
                out = await q(admin, a["ws"], spec(series=[s], viz="kpi", source={"projects": [a["pid"]]}))
                got = out["totals"]["s0"]
                assert (got is None) == (want is None) and (want is None or got == pytest.approx(want, rel=1e-6)), (scope, metric, field, agg, got, want)
                n += 1
        for scope in ("run", "trace", "tree"):   # metric count
            out = await q(admin, a["ws"], spec(series=[{"metric": "count", "scope": scope, **({"filter": 'eq(run_type,"llm")'} if scope == "tree" else {})}], viz="kpi", source={"projects": [a["pid"]]}))
            assert out["totals"]["s0"] == len(scope_rows(scope))
        assert n == 3 * len(COL) * len(AGGS)
    with_world(go)

def test_time_buckets_empty_bucket_is_null_and_run_scope_filter():
    async def go(A, admin, W):
        a = W["a"]; await seed_runs(A, a["pid"])
        out = await q(admin, a["ws"], spec(source={"projects": [a["pid"]]}))   # run scope: 3 runs in hour 0, 1 in hour 1
        assert [b[11:16] for b in out["buckets"]] == [(T0 + i * H).strftime("%H:%M") for i in range(4)]
        assert out["series"][0]["values"] == [3, 1, None, None] and out["totals"]["s0"] == 4 and not out["truncated"]
        assert out["sources"] == {"raw_from": T0.isoformat(), "rollup_to": None}
        out = await q(admin, a["ws"], spec(series=[{"metric": "latency", "agg": "p50", "scope": "trace"}], source={"projects": [a["pid"]]}))
        assert out["series"][0]["values"] == [2000, 2000, None, None]                    # roots r1 (1000), r2 (3000) -> 2000 ; r3 -> 2000
        out = await q(admin, a["ws"], spec(series=[{"metric": "count", "filter": 'eq(status,"error")'}], source={"projects": [a["pid"]]}))
        assert out["series"][0]["values"] == [1, None, None, None]
        out = await q(admin, a["ws"], spec(bucket="auto", source={"projects": [a["pid"]]}))   # 4 h / 120 = 2 min -> 5 min buckets
        assert len(out["buckets"]) == 48 and sum(v or 0 for v in out["series"][0]["values"]) == 4
        out = await q(admin, a["ws"], spec(bucket="day", source={"projects": [a["pid"]]}), tz="Asia/Ho_Chi_Minh")
        assert sum(v or 0 for v in out["series"][0]["values"]) == 4 and out["buckets"][0].endswith("+07:00")
    with_world(go)

def test_ratio_error_rate_and_cache_hit_rate_zero_denominator_is_null():
    async def go(A, admin, W):
        a = W["a"]; await seed_runs(A, a["pid"])
        err = {"metric": "ratio", "scope": "trace", "ratio": {"num": {"metric": "count", "filter": 'eq(status,"error")'}, "den": {"metric": "count"}}}
        out = await q(admin, a["ws"], spec(series=[err], source={"projects": [a["pid"]]}))
        assert out["series"][0]["values"] == [0.5, 0.0, None, None]                      # no error in hour 1 = 0.0 ; no run in hours 2-3 = NULL (never 0)
        assert out["totals"]["s0"] == pytest.approx(1 / 3)
        hit = {"metric": "ratio", "ratio": {"num": {"metric": "tokens", "field": "cache_read", "agg": "sum", "filter": 'eq(run_type,"llm")'},
                                            "den": {"metric": "tokens", "field": "input", "agg": "sum", "filter": 'eq(run_type,"llm")'}}}
        out = await q(admin, a["ws"], spec(series=[hit], source={"projects": [a["pid"]]}))
        assert out["series"][0]["values"][0] == pytest.approx(10 / 60) and out["series"][0]["values"][1] is None
        zero = {"metric": "ratio", "ratio": {"num": {"metric": "count"}, "den": {"metric": "count", "filter": 'eq(name,"nothing")'}}}
        assert (await q(admin, a["ws"], spec(series=[zero], source={"projects": [a["pid"]]})))["totals"]["s0"] is None
    with_world(go)

def test_group_by_metadata_tag_top_n_and_other():
    async def go(A, admin, W):
        a = W["a"]; await seed_runs(A, a["pid"])
        src = {"projects": [a["pid"]]}
        out = await q(admin, a["ws"], spec(group_by={"attr": "metadata", "path": "ls_model_name"}, source=src))
        by = {s["key"]: s for s in out["series"]}
        assert set(by) == {"m1", "m2"} and by["m1"]["values"] == [2, None, None, None] and by["m2"]["values"] == [1, 1, None, None]
        assert out["totals"] == {"m1": 2, "m2": 2}
        out = await q(admin, a["ws"], spec(series=[{"metric": "cost", "agg": "sum"}], group_by={"attr": "model", "top": 1, "rank_by": "cost"}, source=src))
        assert [(s["key"], s["label"]) for s in out["series"]] == [("m2", "m2"), ("__other__", "other")]   # m2 costs 0.05, m1 (the rest) 0.015
        assert out["totals"]["m2"] == pytest.approx(0.05) and out["totals"]["__other__"] == pytest.approx(0.015)
        out = await q(admin, a["ws"], spec(group_by={"attr": "tag"}, source=src, viz="ranked_bar"))
        assert out["buckets"] == [] and out["totals"] == {"x": 2, "y": 1, "__none__": 2}
        out = await q(admin, a["ws"], spec(group_by={"attr": "project"}, source=src, viz="table"))
        assert out["totals"] == {str(a["pid"]): 4} and out["series"][0]["label"].startswith("p-a-")
        out = await q(admin, a["ws"], spec(series=[{"metric": "count", "scope": "trace"}], group_by={"attr": "run_name", "rank_by": "errors"}, source=src, viz="table"))
        assert out["totals"] == {"r1": 1, "r2": 1, "r3": 1}
    with_world(go)

def test_feedback_score_and_label_counts():
    async def go(A, admin, W):
        a = W["a"]; ids = await seed_runs(A, a["pid"])
        for rid, key, score, value in ((ids["r1"], "score", 4, None), (ids["r1"], "score", 2, None), (ids["r2"], "score", 5, None),
                                       (ids["r1"], "thumb", None, "up"), (ids["r2"], "thumb", None, "down"), (ids["r2"], "thumb", None, "up")):
            await sysq(A, "INSERT INTO feedback(run_id,trace_id,project_id,key,score,value) VALUES(%s,%s,%s,%s,%s,%s)", (rid, rid, a["pid"], key, score, value))
        src = {"projects": [a["pid"]]}
        out = await q(admin, a["ws"], spec(series=[{"metric": "feedback", "feedback_key": "score", "agg": "avg"}], source=src))
        assert out["series"][0]["values"][0] == pytest.approx(11 / 3) and out["series"][0]["values"][1] is None
        out = await q(admin, a["ws"], spec(series=[{"metric": "feedback", "feedback_key": "score", "agg": "max"}], source=src, viz="kpi"))
        assert out["totals"]["s0"] == 5
        out = await q(admin, a["ws"], spec(series=[{"metric": "feedback", "feedback_key": "thumb", "feedback_value": "up"}], source=src, viz="kpi"))
        assert out["totals"]["s0"] == 2
        out = await q(admin, a["ws"], spec(series=[{"metric": "feedback", "feedback_key": "thumb", "agg": "count"}], source=src, viz="kpi"))   # count of every label row
        assert out["totals"]["s0"] == 3
        out = await q(admin, a["ws"], spec(series=[{"metric": "count", "filter": "gte(feedback.score,5)"}], source=src, viz="kpi"))
        assert out["totals"]["s0"] == 1
    with_world(go)

def test_environments_and_projects_source():
    async def go(A, admin, W):
        a = W["a"]; await seed_runs(A, a["pid"])
        await sysq(A, "UPDATE projects SET environment='staging' WHERE id=%s", (a["pid"],))
        assert (await q(admin, a["ws"], spec(source={"environments": ["staging"]}, viz="kpi")))["totals"]["s0"] == 4
        assert (await q(admin, a["ws"], spec(source={"environments": ["production"]}, viz="kpi")))["totals"]["s0"] is None   # normalised to prod
        assert (await q(admin, a["ws"], spec(source={"projects": [a["pid"] + 999999]}, viz="kpi")))["totals"]["s0"] is None
        assert (await q(admin, a["ws"], spec(source={"environments": ["staging"]}, series=[{"metric": "count", "scope": "tree", "filter": 'eq(run_type,"llm")'}], viz="kpi")))["totals"]["s0"] == 1
    with_world(go)

# ---------------------------------------------------------------- safety
def test_slow_query_is_cancelled_at_the_timeout(monkeypatch):
    async def go(A, admin, W):
        monkeypatch.setenv("AOS_CHART_TIMEOUT_MS", "100")
        monkeypatch.setattr(charts, "compile", lambda *a, **k: [("SELECT pg_sleep(3) AS t, '__all__' AS g, 1 AS v", [])])
        monkeypatch.setattr(charts, "compile_totals", lambda *a, **k: [("SELECT '__all__' AS g, 1 AS v", [])])
        t = asyncio.get_running_loop().time()
        with pytest.raises(HTTPException) as e: await q(admin, W["a"]["ws"], spec())
        assert e.value.status_code == 504 and asyncio.get_running_loop().time() - t < 2.5
        out = await q(admin, W["a"]["ws"], spec(viz="kpi"))   # the connection is healthy afterwards
        assert out["totals"] == {"s0": 1.0}
    with_world(go)

def test_range_and_concurrency_caps(monkeypatch):
    async def go(A, admin, W):
        ws = W["a"]["ws"]
        for bad in ((T0, T0), (END, T0), (T0 - dt.timedelta(days=401), END)):
            with pytest.raises(HTTPException) as e: await q(admin, ws, spec(), start=bad[0], end=bad[1])
            assert e.value.status_code == 422
        with pytest.raises(HTTPException) as e: await q(admin, ws, spec(bucket="5m"), start=T0 - dt.timedelta(days=3), end=END)   # > 500 buckets
        assert e.value.status_code == 422
        with pytest.raises(HTTPException) as e: await q(admin, ws, spec(), tz="Mars/Olympus")
        assert e.value.status_code == 422
        monkeypatch.setenv("AOS_CHART_CONCURRENCY", "1")
        monkeypatch.setattr(charts, "compile", lambda *a, **k: [("SELECT pg_sleep(0.5) AS t, '__all__' AS g, 1 AS v", [])])
        monkeypatch.setattr(charts, "compile_totals", lambda *a, **k: [("SELECT '__all__' AS g, 1 AS v", [])])
        async def timed():
            charts._CACHE.clear(); t = asyncio.get_running_loop().time()
            await as_user(admin, ws, lambda: charts.run(spec(), T0, END))
            return asyncio.get_running_loop().time()
        end1, end2 = await asyncio.gather(timed(), timed())
        assert abs(end1 - end2) >= 0.4   # one at a time
    with_world(go)

def test_cache_is_per_user_and_role_and_expires():
    async def go(A, admin, W):
        a = W["a"]; await seed_runs(A, a["pid"])
        priv = await seed_runs(A, a["pid"], rows=[R[0]], user_id=555, tag="-priv")
        charts._CACHE.clear(); sp = spec(source={"projects": [a["pid"]]}, viz="kpi")
        adm = await as_user(admin, a["ws"], lambda: charts.run(sp, T0, END), role="admin")
        edt = await as_user(admin, a["ws"], lambda: charts.run(sp, T0, END), role="editor")   # same spec + range, other role: must NOT reuse the admin's answer
        assert adm["totals"]["s0"] == 5 and edt["totals"]["s0"] == 4 and priv
        again = await as_user(admin, a["ws"], lambda: charts.run(sp, T0, END), role="admin")
        assert again == adm and again is not adm
        for k, (exp, v) in list(charts._CACHE.items()): charts._CACHE[k] = (0, v)
        await sysq(A, "DELETE FROM tr_runs WHERE project_id=%s AND name=%s", (a["pid"], "r1-priv"))
        assert (await as_user(admin, a["ws"], lambda: charts.run(sp, T0, END), role="admin"))["totals"]["s0"] == 4   # expired -> recomputed
    with_world(go)

def test_two_workspaces_and_private_chat_trace_stay_separate():
    async def go(A, admin, W):
        a, b = W["a"], W["b"]
        await seed_runs(A, a["pid"]); await seed_runs(A, b["pid"], rows=R[:2])
        await seed_runs(A, a["pid"], rows=[R[2]], user_id=555, tag="-chat")   # a private chat trace of user 555
        assert (await q(admin, a["ws"], spec(viz="kpi")))["totals"]["s0"] == 5                              # admin of a sees everything of a, nothing of b
        assert (await q(admin, b["ws"], spec(viz="kpi")))["totals"]["s0"] == 2
        assert (await q(admin, a["ws"], spec(viz="kpi"), role="editor"))["totals"]["s0"] == 4              # the private trace is hidden from other users
        assert (await q(admin, a["ws"], spec(source={"projects": [b["pid"]]}, viz="kpi")))["totals"]["s0"] is None   # b's project id asked from a: nothing
        assert (await q(admin, a["ws"], spec(group_by={"attr": "project"}, viz="ranked_bar")))["totals"] == {str(a["pid"]): 5}
        assert (await q(admin, a["ws"], spec(series=[{"metric": "count", "scope": "tree"}], viz="kpi"), role="editor"))["totals"]["s0"] == 3
    with_world(go)

# ---------------------------------------------------------------- validation (no database)
def test_hostile_input_is_rejected_with_422():
    app = FastAPI(); app.include_router(charts.router); c = TestClient(app, raise_server_exceptions=False)
    ok = {"series": [{"metric": "count"}]}
    def post(**over): return c.post("/api/obs/charts/query", json={"spec": over.pop("spec", ok), **over}).status_code
    assert post() in (200, 500)   # valid body passes validation (no DB pool in this app: not a 422)
    bad = [{"series": [{"metric": "tokens", "field": "x'); DROP TABLE tr_runs;--"}]},
           {"series": [{"metric": "latency", "agg": "avg); DROP"}]},
           {"series": [{"metric": "count"}], "group_by": {"attr": "metadata", "path": "a'; DROP TABLE x;--"}},
           {"series": [{"metric": "count"}], "group_by": {"attr": "metadata", "path": "a b"}},
           {"series": [{"metric": "count"}], "group_by": {"attr": "metadata"}},
           {"series": [{"metric": "count"}], "group_by": {"attr": "name"}},
           {"series": [{"metric": "count"}], "group_by": {"attr": "tag", "top": 21}},
           {"series": [{"metric": "count"}, {"metric": "latency"}], "group_by": {"attr": "tag"}},
           {"series": [{"metric": "count"}, {"metric": "latency"}], "viz": "table"},
           {"series": [{"metric": "count", "agg": "p99"}]}, {"series": [{"metric": "feedback"}]}, {"series": [{"metric": "ratio"}]},
           {"series": [{"metric": "ratio", "ratio": {"num": {"metric": "count"}, "den": {"metric": "ratio"}}}]},
           {"series": [{"metric": "count"}], "bucket": "second"}, {"series": [{"metric": "count"}], "viz": "pie"}, {"series": []}, {"series": [{"metric": "count"}] * 9},
           {"series": [{"metric": "count", "extra": 1}]}, {"series": [{"metric": "count", "label": "x" * 9000}]},
           {"series": [{"metric": "count"}], "source": {"projects": ["1; DROP"]}}]
    for sp in bad: assert post(spec=sp) == 422, sp
    assert post(hours=0) == 422 and post(tz="Mars/Olympus") == 422 and post(hours=24 * 401) == 422
    assert post(**{"from": "2020-01-01T00:00:00Z", "to": "2022-01-01T00:00:00Z"}) == 422   # > 400 days
    big = {"series": [{"metric": "count", "filter": "x" * 2000}] * 8}   # 8 x 2 KB filters > 8 KB
    assert post(spec=big) == 422

def test_sql_has_only_parameters_and_snapshot_shape():
    hostile = 'eq(name,"x\' OR 1=1 --")'
    sp = spec(series=[{"metric": "feedback", "feedback_key": "k'; DROP", "filter": hostile}], source={"projects": [1], "environments": ["prod"]},
              group_by={"attr": "metadata", "path": "a.b-c", "top": 3, "rank_by": "cost"})
    (sql, params), = charts.compile(sp, T0, END, "Asia/Ho_Chi_Minh"); (tsql, tparams), = charts.compile_totals(sp, T0, END)
    for text in (sql, tsql): assert "DROP" not in text and "OR 1=1" not in text and "a.b-c" not in text
    assert sql.count("%s") == len(params) and tsql.count("%s") == len(tparams)
    assert ["a", "b-c"] in params and "k'; DROP" in params and "x' OR 1=1 --" in params and [1] in params
    assert "generate_series" in sql and "date_trunc('hour'" in sql and "metadata #>> %s::text[]" in sql and "LIMIT %s" in sql and "other" in sql
    (s5, _), = charts.compile(spec(bucket="5m"), T0, END); assert "date_bin('5 minutes'" in s5
    (stree, _), = charts.compile(spec(series=[{"metric": "count", "scope": "tree", "filter": 'eq(run_type,"llm")'}]), T0, END); assert "EXISTS" in stree and "FROM tr_runs root" in stree

def test_templates_are_valid_specs_and_run():
    tpl = asyncio.run(charts.chart_templates())
    assert {t["id"] for t in tpl} == {"error_rate", "latency_by_model", "run_volume", "tokens", "most_expensive_model", "cache_hit_rate"}
    for t in tpl:
        assert t["title"]["vi"] and t["title"]["en"]
        charts.ChartSpec.model_validate(t["spec"]); charts.compile(charts.ChartSpec.model_validate(t["spec"]), T0, END)
    async def go(A, admin, W):
        a = W["a"]; await seed_runs(A, a["pid"])
        for t in tpl:
            out = await q(admin, a["ws"], charts.ChartSpec.model_validate({**t["spec"], "bucket": "hour"}))
            assert "series" in out and "totals" in out, t["id"]
        out = await q(admin, a["ws"], charts.ChartSpec.model_validate({**[t for t in tpl if t["id"] == "most_expensive_model"][0]["spec"], "source": {"projects": [a["pid"]]}}))
        assert out["totals"] == {"m1": pytest.approx(0.005)}   # only the llm run c1 counts
    with_world(go)

# ================================================================ 12.9 hourly rollup
import random  # noqa: E402
import stats_rollup  # noqa: E402

DAY = dt.timedelta(days=1)
D0 = NOW().replace(minute=0, second=0, microsecond=0) - 40 * DAY   # old hours, 40 days back (older than the 30-day retention)

async def prep_table(A, admin):
    """What boot does: migration 0115 + its policy + grants (the test DB may not have been booted with this code yet)."""
    import importlib.util
    spec_ = importlib.util.spec_from_file_location("m0115", os.path.join(os.path.dirname(__file__), "..", "app", "migrations", "0115_tr_stats_hourly.py"))
    m = importlib.util.module_from_spec(spec_); spec_.loader.exec_module(m)
    async def qq(sql, args=()): return await sysq(A, sql, args)
    await m.up(qq)
    await admin._policy("tr_stats_hourly", "aos_ws() IS NULL OR (" + admin.RLS_CHILD["tr_stats_hourly"] + ")")
    await admin.rls_grants()

async def put(A, pid, at, lat, status="success", name="agent", model="m1", user_id=None, tok=(10, 5), cost=0.01, ttl=None, ftt=None):
    rid = str(uuid.uuid4())
    await sysq(A, """INSERT INTO tr_runs(id,trace_id,project_id,name,run_type,start_time,end_time,latency_ms,status,metadata,tags,input_tokens,output_tokens,total_tokens,cost_usd,model,user_id,ttl_days,first_token_time)
        VALUES(%s,%s,%s,%s,'llm',%s,%s,%s,%s,%s,'{}',%s,%s,%s,%s,%s,%s,%s,%s)""",
               (rid, rid, pid, name, at, at + dt.timedelta(milliseconds=lat), lat, status, Jsonb({"ls_model_name": model}), tok[0], tok[1], tok[0] + tok[1], cost, model, user_id, ttl,
                at + dt.timedelta(milliseconds=ftt) if ftt else None))
    return rid

def seeded(n=300, seed=7):
    rnd = random.Random(seed)
    return [(D0 + dt.timedelta(hours=i % 3, minutes=i % 50), max(20, int(rnd.lognormvariate(6.5, 0.9))), "error" if i % 10 == 0 else "success", "m1" if i % 3 else "m2") for i in range(n)]

async def obs_retention(A, pid):
    import obs
    return await obs.retention(project_ids=[pid], only_partitions=[])

def test_histogram_percentiles_within_ten_percent_p99_fifteen():
    rnd = random.Random(3)
    for mu, sg in ((6.5, 0.9), (4.0, 0.5), (8.5, 1.2)):
        vals = [max(1, int(rnd.lognormvariate(mu, sg))) for _ in range(500)]
        hist = [0] * 40
        for v in vals: hist[stats_rollup.bin_of(v)] += 1
        for p in (.5, .9, .95, .99):
            exact = pct(vals, p); got = stats_rollup.quantile(hist, p)
            assert abs(got - exact) / exact < (0.10 if p < .99 else 0.15), (mu, p, got, exact)   # p99 of 500 samples = 5 values in the tail
    assert stats_rollup.quantile([0] * 40, .5) is None

def test_rollup_answers_after_raw_runs_are_deleted_and_hour_roll_is_idempotent():
    async def go(A, admin, W):
        await prep_table(A, admin); a = W["a"]; pid = a["pid"]; rows = seeded()
        for at, lat, st, mdl in rows: await put(A, pid, at, lat, st, model=mdl, ttl=30)
        recent = await put(A, pid, NOW() - 2 * DAY, 500, "error", ttl=30)   # a raw run that stays after retention
        live = await put(A, pid, NOW() - 10 * M, 300, ttl=30)   # in the live tail: read from the raw run, not from the table
        for h in range(3): await stats_rollup.compute_hour(pid, D0 + h * H)
        await stats_rollup.compute_hour(pid, NOW() - 2 * DAY)
        first = await sysq(A, "SELECT sum(n) n, sum(n_err) e, sum(lat_sum) l FROM tr_stats_hourly WHERE project_id=%s", (pid,), one=True)
        for h in range(3): await stats_rollup.compute_hour(pid, D0 + h * H)   # twice: nothing doubles
        assert await sysq(A, "SELECT sum(n) n, sum(n_err) e, sum(lat_sum) l FROM tr_stats_hourly WHERE project_id=%s", (pid,), one=True) == first
        assert first["n"] == len(rows) + 1
        out = await obs_retention(A, pid)
        assert out["runs"] == len(rows)                                                   # the old raw runs are gone ...
        assert (await sysq(A, "SELECT count(*) n FROM tr_runs WHERE project_id=%s", (pid,), one=True))["n"] == 2
        start, end = NOW() - 60 * DAY, NOW() + M
        want_n = len(rows) + 2; want_err = sum(1 for r in rows if r[2] == "error") + 1
        out = await q(admin, a["ws"], spec(series=[{"metric": "count"}], viz="kpi", bucket="day", source={"projects": [pid]}), start=start, end=end)
        assert out["totals"]["s0"] == want_n                                               # ... and the 60-day chart still counts them (old hours + live tail)
        assert out["sources"]["rollup_to"] is not None and out["sources"]["raw_from"] == out["sources"]["rollup_to"]
        err = {"metric": "ratio", "ratio": {"num": {"metric": "count", "filter": 'eq(status,"error")'}, "den": {"metric": "count"}}}
        out = await q(admin, a["ws"], spec(series=[err], bucket="day", source={"projects": [pid]}), start=start, end=end)
        assert out["totals"]["s0"] == pytest.approx(want_err / want_n)
        vals = [r[1] for r in rows] + [500, 300]
        for agg in ("p50", "p95", "p99"):
            out = await q(admin, a["ws"], spec(series=[{"metric": "latency", "agg": agg}], viz="kpi", bucket="day", source={"projects": [pid]}), start=start, end=end)
            exact = pct(vals, int(agg[1:]) / 100)
            assert abs(out["totals"]["s0"] - exact) / exact < 0.10, (agg, out["totals"]["s0"], exact)
        out = await q(admin, a["ws"], spec(series=[{"metric": "latency", "agg": "avg"}], viz="kpi", bucket="day", source={"projects": [pid]}), start=start, end=end)
        assert out["totals"]["s0"] == pytest.approx(sum(vals) / len(vals))
        out = await q(admin, a["ws"], spec(series=[{"metric": "cost", "agg": "sum"}], viz="kpi", bucket="day", source={"projects": [pid]}), start=start, end=end)
        assert out["totals"]["s0"] == pytest.approx(0.01 * want_n)
        # per day buckets: the old days hold the seeded counts, empty days stay NULL
        out = await q(admin, a["ws"], spec(series=[{"metric": "count"}], bucket="day", source={"projects": [pid]}), start=start, end=end)
        v = out["series"][0]["values"]; assert sum(x or 0 for x in v) == want_n and v.count(None) > 50
        # group by model: top-N from the table
        out = await q(admin, a["ws"], spec(series=[{"metric": "count"}], group_by={"attr": "model"}, viz="ranked_bar", bucket="day", source={"projects": [pid]}), start=start, end=end)
        assert out["totals"] == {"m1": sum(1 for r in rows if r[3] == "m1") + 2, "m2": sum(1 for r in rows if r[3] == "m2")}
        assert recent and live
    with_world(go)

def test_retention_rolls_an_unrolled_hour_up_before_it_deletes():
    async def go(A, admin, W):
        await prep_table(A, admin); pid = W["a"]["pid"]
        for k in range(5): await put(A, pid, D0 + k * M, 100 + k, ttl=30)
        assert (await sysq(A, "SELECT count(*) n FROM tr_stats_hourly WHERE project_id=%s", (pid,), one=True))["n"] == 0
        assert (await obs_retention(A, pid))["runs"] == 5
        r = await sysq(A, "SELECT sum(n) n, sum(lat_sum) l FROM tr_stats_hourly WHERE project_id=%s", (pid,), one=True)
        assert r["n"] == 5 and r["l"] == sum(100 + k for k in range(5))
    with_world(go)

def test_rollup_privacy_only_admin_reads_private_numbers():
    async def go(A, admin, W):
        await prep_table(A, admin); a = W["a"]; pid = a["pid"]
        for k in range(4): await put(A, pid, D0 + k * M, 100, ttl=30)
        for k in range(3): await put(A, pid, D0 + (10 + k) * M, 100, ttl=30, user_id=987654)   # the viewer's own private chat runs
        await put(A, pid, NOW() - 2 * DAY, 100, ttl=30)
        await stats_rollup.compute_hour(pid, D0); await stats_rollup.compute_hour(pid, NOW() - 2 * DAY)
        await obs_retention(A, pid)
        kw = dict(start=NOW() - 60 * DAY, end=NOW() + M); sp = spec(series=[{"metric": "count"}], viz="kpi", bucket="day", source={"projects": [pid]})
        adm = await q(admin, a["ws"], sp, role="admin", **kw)
        assert adm["totals"]["s0"] == 8 and adm["sources"]["private_excluded"] is False
        for role in ("viewer", "editor"):
            out = await q(admin, a["ws"], sp, role=role, **kw)
            assert out["totals"]["s0"] == 5 and out["sources"]["private_excluded"] is True and "riêng tư" in out["sources"]["note"]   # not even the owner: the table has no owner
        n = await as_user(admin, a["ws"], lambda: A.q("SELECT count(*) n FROM tr_stats_hourly WHERE project_id=%s AND is_private", (pid,), one=True), role="viewer")
        assert n["n"] == 0                                                                 # the table itself hides the private rows from a non-admin session
    with_world(go)

def test_rollup_two_workspaces_stay_separate_and_limited_answers():
    async def go(A, admin, W):
        await prep_table(A, admin); a, b = W["a"], W["b"]
        for p in (a, b):
            for k in range(2): await put(A, p["pid"], D0 + k * M, 100, ttl=30)
            await put(A, p["pid"], NOW() - 2 * DAY, 100, ttl=30)
            await stats_rollup.compute_hour(p["pid"], D0); await stats_rollup.compute_hour(p["pid"], NOW() - 2 * DAY)
            await obs_retention(A, p["pid"])
        kw = dict(start=NOW() - 60 * DAY, end=NOW() + M)
        out = await q(admin, a["ws"], spec(series=[{"metric": "count"}], viz="kpi", bucket="day"), **kw)
        assert out["totals"]["s0"] == 3                                                    # workspace a only (b's rows are invisible)
        out = await q(admin, a["ws"], spec(series=[{"metric": "count"}], viz="kpi", bucket="day", source={"projects": [b["pid"]]}), **kw)
        assert out["totals"]["s0"] is None
        # what the table cannot answer: say so, answer from the raw runs
        out = await q(admin, a["ws"], spec(series=[{"metric": "count"}], group_by={"attr": "metadata", "path": "ls_model_name"}, viz="ranked_bar", bucket="day", source={"projects": [a["pid"]]}), **kw)
        s = out["sources"]; assert s["limited_field"] == "metadata" and s["limited_before"] and "cannot be filtered by metadata" in s["limited_reason"] and s["rollup_to"] is None
        out = await q(admin, a["ws"], spec(series=[{"metric": "latency", "agg": "min"}], viz="kpi", bucket="day", source={"projects": [a["pid"]]}), **kw)
        assert out["sources"]["limited_field"] == "agg min"
        out = await q(admin, a["ws"], spec(series=[{"metric": "count", "filter": 'gt(latency,2)'}], viz="kpi", bucket="day", source={"projects": [a["pid"]]}), **kw)
        assert out["sources"]["limited_field"] == "gt"
        out = await q(admin, a["ws"], spec(series=[{"metric": "count"}], viz="kpi", bucket="day", source={"projects": [a["pid"]]}), start=NOW() - 6 * H, end=NOW() + M)
        assert out["sources"]["rollup_to"] is None and "limited_before" not in out["sources"]   # recent ranges never touch the table
    with_world(go)

def test_filter_parser_and_spec_reasons():
    pf = stats_rollup.parse_filter
    assert pf("") == ({}, False) and pf('eq(run_type,"llm")') == ({"run_type": "llm"}, False)
    assert pf('and(eq(model,"m1"), eq(status,"error"))') == ({"model": "m1"}, True)
    for bad in ('eq(status,"success")', 'gt(latency,2)', 'or(eq(name,"a"),eq(name,"b"))', 'has(tags,"x")', 'eq(name,"a")garbage', 'eq(metadata.x,"1")'):
        with pytest.raises(stats_rollup.Incompat): pf(bad)
    ok = spec(series=[{"metric": "latency", "agg": "p95", "filter": 'eq(run_type,"llm")'}], group_by={"attr": "model"}, bucket="hour")
    assert stats_rollup.spec_reason(ok, "hour") is None and stats_rollup.spec_reason(ok, "5m") == "bucket 5m"
    assert stats_rollup.spec_reason(spec(series=[{"metric": "tokens", "field": "cache_read", "agg": "sum"}]), "day") == "field cache_read"
    assert stats_rollup.spec_reason(spec(series=[{"metric": "count", "scope": "tree", "filter": 'eq(run_type,"llm")'}]), "day") == "scope tree"
    assert stats_rollup.spec_reason(spec(series=[{"metric": "feedback", "feedback_key": "k"}]), "day") == "feedback"

# ================================================================ 12.8 prebuilt dashboard
import dashboards  # noqa: E402

SECTION_IDS = {"traces", "llm", "cost", "tools", "run_types", "feedback"}

async def seed_dash(A, pid):
    """R (3 roots, 1 llm child) + 3 tool runs under r1/r2 (t1 x2 one error, t2 x1) + thread ids on the roots + two feedback keys."""
    ids = await seed_runs(A, pid)
    for parent, name, at, lat, status in (("r1", "t1", 11 * M, 100, "success"), ("r2", "t1", 21 * M, 300, "error"), ("r2", "t2", 22 * M, 200, "success")):
        st = T0 + at; rid = str(uuid.uuid4())
        await sysq(A, "INSERT INTO tr_runs(id,trace_id,parent_id,project_id,name,run_type,start_time,end_time,latency_ms,status,metadata) VALUES(%s,%s,%s,%s,%s,'tool',%s,%s,%s,%s,%s)",
                   (rid, ids[parent], ids[parent], pid, name, st, st + dt.timedelta(milliseconds=lat), lat, status, Jsonb({})))
    for k, th in (("r1", "th1"), ("r2", "th1"), ("r3", "th2")):
        await sysq(A, "UPDATE tr_runs SET thread_id=%s WHERE id=%s", (th, ids[k]))
    for rid, key, score in ((ids["r1"], "quality", 1.0), (ids["r2"], "quality", 0.0), (ids["r1"], "tone", 0.5)):
        await sysq(A, "INSERT INTO feedback(run_id,trace_id,project_id,key,score) VALUES(%s,%s,%s,%s,%s)", (rid, rid, pid, key, score))
    return ids

async def dash(admin, ws, role="admin", **kw):
    charts._CACHE.clear()
    kw.setdefault("hours", 6)
    return await as_user(admin, ws, lambda: dashboards.prebuilt(**{"project_id": None, "environment": None, "agent": None, "group_by": None, "tz": "UTC", **kw}), role=role)

def card(out, cid): return [c for c in out["cards"] if c["id"] == cid][0]

def test_prebuilt_dashboard_has_six_sections_and_cards_equal_charts_run():
    async def go(A, admin, W):
        a = W["a"]; await seed_dash(A, a["pid"])
        out = await dash(admin, a["ws"], project_id=a["pid"])
        assert {c["section"] for c in out["cards"]} == SECTION_IDS and {s["id"] for s in out["sections"]} == SECTION_IDS
        assert [c["id"] for c in out["cards"]] == [c["id"] for c in dashboards.PREBUILT] and all(c["error"] is None for c in out["cards"])
        assert card(out, "trace_count")["result"]["totals"]["s0"] == 3
        assert card(out, "trace_error_rate")["result"]["totals"]["s0"] == pytest.approx(1 / 3)
        assert card(out, "trace_latency")["result"]["totals"] == {"s0": 2000, "s1": pytest.approx(2900), "s2": pytest.approx(2980)}   # P50 / P95 / P99 of roots 1000, 3000, 2000
        assert card(out, "llm_calls")["result"]["totals"]["s0"] == 1
        assert card(out, "llm_latency_by_model")["result"]["totals"] == {"m1": 400}
        assert card(out, "cost_total")["result"]["totals"]["s0"] == pytest.approx(0.06)
        assert card(out, "tokens")["result"]["totals"] == {"s0": 310, "s1": 160}
        assert card(out, "cost_by_model")["result"]["totals"] == {"m1": pytest.approx(0.005)}
        assert card(out, "tools_top5")["result"]["totals"] == {"t1": 2, "t2": 1}                       # t1 has the error: first
        assert [s["key"] for s in card(out, "tools_top5")["result"]["series"]][0] == "t1"
        assert card(out, "run_types_top5")["result"]["totals"] == {"chain": 3, "llm": 1, "tool": 3}
        fb = card(out, "feedback_scores")["result"]
        assert {s["label"]: s["values"] for s in fb["series"]}.keys() == {"quality", "tone"} and fb["totals"] == {"s0": 0.5, "s1": 0.5}
        tl = card(out, "trace_count")["result"]
        assert None in tl["series"][0]["values"] and tl["sources"]["rollup_to"] is None                 # empty buckets are gaps
        # the cards are the numbers of charts.run called directly with the same range
        rg = out["range"]; start, end = dt.datetime.fromisoformat(rg["from"]), dt.datetime.fromisoformat(rg["to"])
        for cid in ("trace_count", "trace_latency", "llm_latency_by_model", "tools_top5"):
            d = [d for c, d in dashboards.build_specs(a["pid"], None, None, None) if c["id"] == cid][0]
            direct = await q(admin, a["ws"], charts.ChartSpec.model_validate(d), start=start, end=end, tz="UTC")
            assert direct == card(out, cid)["result"], cid
        # click-through for "View runs"
        assert card(out, "llm_calls")["view_runs"] == {"p": a["pid"], "filter": 'eq(run_type,"llm")', "since": 6}
        assert card(out, "trace_count")["view_runs"] == {"p": a["pid"], "filter": "", "since": 6}
    with_world(go)

def test_prebuilt_split_by_metadata_tag_and_selectors():
    async def go(A, admin, W):
        a = W["a"]; await seed_dash(A, a["pid"])
        out = await dash(admin, a["ws"], project_id=a["pid"], group_by="metadata:ls_model_name")
        tc = card(out, "trace_count")["result"]
        assert {s["key"] for s in tc["series"]} == {"m1", "m2"} and sum(tc["totals"].values()) == 3     # one line per model
        assert {s["key"] for s in card(out, "llm_latency_by_model")["result"]["series"]} == {"m1"}
        er = card(out, "trace_error_rate")["result"]
        assert er["totals"] == {"m2": 1}                 # ratio -> error count per group when split
        assert len(card(out, "trace_latency")["result"]["series"]) >= 1 and card(out, "trace_latency")["result"]["series"][0]["label"] in ("P50", "m1", "m2", "(none)")
        assert card(out, "tools_top5")["result"]["totals"] == {"t1": 2, "t2": 1} and card(out, "run_types_top5")["result"]["totals"]["chain"] == 3   # never split
        out = await dash(admin, a["ws"], project_id=a["pid"], group_by="tag:x")
        assert {s["label"] for s in card(out, "trace_count")["result"]["series"]} == {"x"}                          # prefix keeps only the tags starting with x
        out = await dash(admin, a["ws"], project_id=a["pid"], agent="42")
        assert card(out, "trace_count")["result"]["totals"]["s0"] is None and card(out, "llm_calls")["view_runs"]["filter"] == 'and(eq(run_type,"llm"),eq(metadata.agent_id,"42"))'
        out = await dash(admin, a["ws"], project_id=a["pid"], environment="staging")
        assert card(out, "trace_count")["result"]["totals"]["s0"] is None                                          # the project is prod
        out = await dash(admin, a["ws"], hours=6)                                                                  # all projects of the workspace
        assert card(out, "trace_count")["result"]["totals"]["s0"] == 3
        for bad in ({"group_by": "metadata:a;b"}, {"group_by": "run_name"}, {"agent": 'x"y'}):
            with pytest.raises(HTTPException) as e: await dash(admin, a["ws"], project_id=a["pid"], **bad)
            assert e.value.status_code == 422
    with_world(go)

def test_prebuilt_dashboard_other_workspace_and_demo_see_nothing_of_it():
    async def go(A, admin, W):
        a, b = W["a"], W["b"]; await seed_dash(A, a["pid"])
        with pytest.raises(HTTPException) as e: await dash(admin, b["ws"], project_id=a["pid"])
        assert e.value.status_code == 404
        out = await dash(admin, b["ws"])
        assert card(out, "trace_count")["result"]["totals"]["s0"] is None and card(out, "tools_top5")["result"]["totals"] == {} and card(out, "feedback_scores")["result"] is None
        # a viewer in the same workspace may read; a private chat trace of someone else stays out
        await sysq(A, "UPDATE tr_runs SET user_id=555 WHERE project_id=%s AND name='r3'", (a["pid"],))
        out = await dash(admin, a["ws"], role="viewer", project_id=a["pid"])
        assert card(out, "trace_count")["result"]["totals"]["s0"] == 2
        out = await as_user(admin, a["ws"], lambda: dashboards.prebuilt(project_id=a["pid"], environment=None, agent=None, hours=6, group_by=None, tz="UTC"), role="viewer", demo=True)
        assert card(out, "trace_count")["result"]["totals"]["s0"] == 2
    with_world(go)

def test_threads_stats_and_health_panel_is_admin_only():
    async def go(A, admin, W):
        a, b = W["a"], W["b"]; await seed_dash(A, a["pid"])
        r = await as_user(admin, a["ws"], lambda: dashboards.threads_stats(a["pid"], T0, END))
        assert r["threads"] == 2 and r["traces"] == 3 and r["error_rate"] == pytest.approx(1 / 3) and r["cost"] == pytest.approx(0.06) and r["p50_ms"] == 2000
        r = await as_user(admin, a["ws"], lambda: dashboards.threads_stats(a["pid"], T0 + 3 * H, END))
        assert r["threads"] == 0 and r["traces"] == 0 and r["error_rate"] is None
        with pytest.raises(HTTPException) as e: await as_user(admin, b["ws"], lambda: dashboards.threads_stats(a["pid"], T0, END))
        assert e.value.status_code == 404
    with_world(go)
    import admin
    assert not admin.allowed("viewer", "GET", "/api/admin/health") and not admin.allowed("editor", "GET", "/api/admin/health") and admin.allowed("admin", "GET", "/api/admin/health")
    assert not admin.allowed("viewer", "GET", "/api/admin/health", demo=True) and admin.allowed("viewer", "GET", "/api/obs/dashboards/prebuilt", demo=True)


# ================================================================ 12.8 fix: one cost definition (total == sum of by-model)
async def seed_resumed(A, pid, at, models=("m1", "m2")):
    """The shape that used to break the total: a root that already carries tokens + model + cost of its FIRST segment, one LLM under it, and a resumed chain
    (own cumulative cost, 2x) with a second LLM under it. Plus a plain trace whose root has nothing. -> trace ids; the true cost is the sum of the LLM runs."""
    import obs
    def ins(rid, tr, parent, name, rtype, off, cost, model, tok):
        st = at + dt.timedelta(seconds=off)
        return sysq(A, """INSERT INTO tr_runs(id,trace_id,parent_id,project_id,name,run_type,start_time,end_time,latency_ms,status,metadata,tags,input_tokens,output_tokens,total_tokens,cost_usd,model)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,100,'success',%s,'{}',%s,%s,%s,%s,%s)""", (rid, tr, parent, pid, name, rtype, st, st + dt.timedelta(milliseconds=100), Jsonb({}), tok, 0, tok, cost, model))
    r, c1, ch, c2, p, p1, p2 = (str(uuid.uuid4()) for _ in range(7))
    await ins(r, r, None, "research", "chain", 0, 0.3375, models[0], 450000)
    await ins(c1, r, r, "llm-a", "llm", 1, 0.3375, models[0], 450000)
    await ins(ch, r, r, "research", "chain", 2, 0.675, models[1], 900000)
    await ins(c2, r, ch, "llm-b", "llm", 3, 0.3375, models[1], 450000)
    await ins(p, p, None, "plain", "chain", 10, None, None, None)
    await ins(p1, p, p, "llm-c", "llm", 11, 0.01, models[0], 100)
    await ins(p2, p, p, "llm-d", "llm", 12, 0.02, models[1], 200)
    await obs.rollup([r, p])
    return r, p

def test_total_cost_equals_sum_of_by_model_raw_and_rollup():
    cost_total = {"series": [{"metric": "cost", "agg": "sum", "scope": "trace"}], "viz": "kpi", "bucket": "day"}
    by_model = {"series": [{"metric": "cost", "agg": "sum", "filter": 'eq(run_type,"llm")'}], "group_by": {"attr": "model", "top": 10, "rank_by": "cost"}, "viz": "ranked_bar", "bucket": "day"}
    async def go(A, admin, W):
        await prep_table(A, admin); a = W["a"]; pid = a["pid"]
        r, p = await seed_resumed(A, pid, T0 + 30 * M)                          # recent: read from the raw runs
        root = await sysq(A, "SELECT cost_usd, total_tokens FROM tr_runs WHERE id=%s", (r,), one=True)
        assert float(root["cost_usd"]) == pytest.approx(0.675) and root["total_tokens"] == 900000 + 0   # first-segment root now includes the resumed LLM (sum of LLM runs)
        want = 0.3375 * 2 + 0.03
        # raw path: the prebuilt dashboard cards
        out = await dash(admin, a["ws"], project_id=a["pid"])
        tot = card(out, "cost_total")["result"]["totals"]["s0"]; bm = card(out, "cost_by_model")["result"]["totals"]
        assert tot == pytest.approx(want) and sum(bm.values()) == pytest.approx(tot) and card(out, "cost_total")["hint"] is not None
        assert card(out, "tokens")["result"]["totals"]["s0"] == 450000 + 450000 + 300   # input tokens of the LLM runs, once each
        # rollup path: the same runs placed 40 days back, rolled hour by hour, 60-day chart
        old = D0 + 5 * M
        await seed_resumed(A, pid, old)
        for h in (D0, D0 + H, T0): await stats_rollup.compute_hour(pid, h)   # T0 = the recent finished hour (older than the 2 h live tail)
        await sysq(A, "DELETE FROM tr_runs WHERE project_id=%s AND start_time < %s", (pid, T0 - DAY))   # old raw runs gone (as after retention): the table answers
        charts._CACHE.clear()
        rng = dict(start=NOW() - 60 * DAY, end=NOW() + M, tz="UTC")   # all from the table
        t = (await q(admin, a["ws"], spec(**cost_total, source={"projects": [pid]}), **rng))
        m = (await q(admin, a["ws"], spec(**by_model, source={"projects": [pid]}), **rng))
        assert t["sources"]["rollup_to"] is not None
        assert t["totals"]["s0"] == pytest.approx(2 * want) and sum(m["totals"].values()) == pytest.approx(t["totals"]["s0"])
    with_world(go)

def test_migration_0130_repairs_old_roots_and_clears_their_rollup_hours():
    import importlib.util
    async def go(A, admin, W):
        await prep_table(A, admin); pid = W["a"]["pid"]
        r, p = await seed_resumed(A, pid, D0 + 5 * M)
        await sysq(A, "UPDATE tr_runs SET cost_usd=0.3375, total_tokens=450000, input_tokens=450000 WHERE id=%s", (r,))   # the old broken state
        await stats_rollup.compute_hour(pid, D0)
        assert (await sysq(A, "SELECT count(*) n FROM tr_stats_hourly WHERE project_id=%s", (pid,), one=True))["n"] > 0
        sp = importlib.util.spec_from_file_location("m0130", os.path.join(os.path.dirname(__file__), "..", "app", "migrations", "0130_trace_cost_rollup_fix.py"))
        m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m)
        async def qq(sql, args=()): return await sysq(A, sql, args)
        await m.up(qq); await m.up(qq)                                         # idempotent
        assert float((await sysq(A, "SELECT cost_usd FROM tr_runs WHERE id=%s", (r,), one=True))["cost_usd"]) == pytest.approx(0.675)
        assert (await sysq(A, "SELECT count(*) n FROM tr_stats_hourly WHERE project_id=%s", (pid,), one=True))["n"] == 0
    with_world(go)
