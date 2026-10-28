"""GĐ12.9 hourly rollup of runs (table tr_stats_hourly, migration 0115).
Why: retention deletes raw runs after 30 days, but charts must still answer for 90 days. Every finished UTC hour is summed per (project, environment, run_type, name, model, is_root, is_private)
with two 40-bin log-scale histograms (latency, time to first token) so that percentiles stay within a few percent.
Write side: compute_hour (idempotent: the hour's rows are replaced in one transaction), catch_up (worker, every 15 ticks = 5 min: rolls every finished hour that has runs but no rows yet, max 48 per
tick, and re-rolls the last two hours for late runs; trims rows older than AOS_STATS_KEEP_DAYS = 400), ensure_hours / ensure_partition (retention calls them BEFORE it deletes, so an hour is never lost).
Read side: charts.run asks `answer(...)`: when the chart wants data older than the oldest raw run of a project that already has rollup rows, and the spec only uses what the table holds
(count / latency / ttft / tokens / cost, run_name / run_type / model / project groups, simple eq() filters), the whole chart is computed from rollup rows (older hours) + a live tail
of the last ~3 hours read from the raw runs. Otherwise the answer says `limited_before` + a reason. Private chat runs (is_private) are summed in separate rows that only a workspace admin reads.
Known limits: a hour that is not rolled yet (right after the feature starts, until the first catch-up passes) is missing from old-data answers; min / max, feedback, tags, metadata and
tree scope need raw runs; the 5 minute bucket is never served from the rollup."""
import datetime as dt, math, os
from collections import defaultdict
from fastapi import HTTPException

class A:   # lazy handle: deepagent_api imports this module while it is still being imported
    @staticmethod
    def mod():
        import deepagent_api; return deepagent_api

BINS, KB = 40, 7.0
H = dt.timedelta(hours=1)
CATCH_UP_MAX = 48
CELL_LIMIT = 300_000
DIMS = "hour, project_id, environment, run_type, name, model, is_root, is_private"
COLS = ("n", "n_err", "lat_sum", "n_lat", "ttft_sum", "n_ttft", "tok_in", "tok_out", "tok_total", "cost")
_state = {"full": None}

def _env_int(name, default):
    try: return max(1, int(os.environ.get(name, default)))
    except ValueError: return default

def floor_hour(t): return t.astimezone(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)

# ---------------------------------------------------------------- histograms
def bin_of(ms):
    """Same formula as the SQL below: floor(log10(ms + 1) * 7), clamped to 0..39."""
    return max(0, min(BINS - 1, int(math.floor(math.log10(max(ms, 0) + 1) * KB))))

def quantile(hist, p):
    """p in 0..1 -> value in ms (linear inside the bin on the log axis), None when the histogram is empty."""
    total = sum(hist)
    if not total: return None
    pos = p * (total - 1); cum = 0
    for i, c in enumerate(hist):
        if c and cum + c > pos:
            return 10 ** ((i + (pos - cum + 0.5) / c) / KB) - 1
        cum += c
    return 10 ** ((BINS - 0.5) / KB) - 1

# ---------------------------------------------------------------- compute (SQL over raw runs)
def _dims_sql():
    import envs
    return ("date_trunc('hour', start_time, 'UTC') AS hour, project_id, " + envs.ENV_SQL + " AS environment, coalesce(run_type,'') AS run_type, coalesce(name,'') AS name, "
            "coalesce(model,'') AS model, (parent_id IS NULL) AS is_root, (user_id IS NOT NULL) AS is_private")

_TTFT = "(EXTRACT(EPOCH FROM (first_token_time - start_time)) * 1000)"

def cell_plans(where, params):
    """-> [(sql, params)] two queries over tr_runs rows matching `where` (use the viewer's or the system context): sums per cell, then histogram bins per cell."""
    d = _dims_sql()
    s1 = (f"SELECT {d}, count(*) AS n, count(*) FILTER (WHERE status = 'error') AS n_err, coalesce(sum(latency_ms), 0)::bigint AS lat_sum, count(latency_ms) AS n_lat, "
          f"coalesce(sum({_TTFT}) FILTER (WHERE first_token_time IS NOT NULL), 0)::float8 AS ttft_sum, count(first_token_time) AS n_ttft, "
          "coalesce(sum(input_tokens), 0)::bigint AS tok_in, coalesce(sum(output_tokens), 0)::bigint AS tok_out, coalesce(sum(total_tokens), 0)::bigint AS tok_total, coalesce(sum(cost_usd), 0) AS cost "
          f"FROM tr_runs WHERE {where} GROUP BY 1, 2, 3, 4, 5, 6, 7, 8")
    s2 = (f"SELECT {DIMS}, k, bin, count(*) AS c FROM ("
          f"SELECT {d}, 'l' AS k, least(39, greatest(0, floor(log10(greatest(latency_ms, 0)::float8 + 1) * 7)))::int AS bin FROM tr_runs WHERE ({where}) AND latency_ms IS NOT NULL "
          f"UNION ALL SELECT {d}, 't', least(39, greatest(0, floor(log10(greatest({_TTFT}, 0) + 1) * 7)))::int FROM tr_runs WHERE ({where}) AND first_token_time IS NOT NULL) x "
          "GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10")
    return [(s1, list(params)), (s2, list(params) * 2)]

def assemble(rows1, rows2):
    """-> list of cell dicts (DIMS + COLS + lat_hist + ttft_hist) from the two result sets of cell_plans."""
    cells = {}
    for r in rows1:
        r = tuple(r.values()) if isinstance(r, dict) else tuple(r)
        c = dict(zip(DIMS.split(", ") + list(COLS), r)); c["lat_hist"] = [0] * BINS; c["ttft_hist"] = [0] * BINS
        c["cost"] = float(c["cost"]); cells[r[:8]] = c
    for r in rows2:
        r = tuple(r.values()) if isinstance(r, dict) else tuple(r)
        c = cells.get(r[:8])
        if c is not None: c["lat_hist" if r[8] == "l" else "ttft_hist"][int(r[9])] += int(r[10])
    return list(cells.values())

async def _sys(plans):
    """Run the plans in the system context (no workspace filter)."""
    D = A.mod(); out = []
    for sql, params in plans: out.append(await D.q(sql, params) or [])
    return out

_INS = ("INSERT INTO tr_stats_hourly(project_id, hour, environment, run_type, name, model, is_root, is_private, n, n_err, lat_sum, n_lat, ttft_sum, n_ttft, tok_in, tok_out, tok_total, cost, lat_hist, ttft_hist) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")

async def compute_hour(project_id, hour):
    """Roll one UTC hour of one project (idempotent: the rows of that hour are replaced, never added to). -> number of rows written."""
    hour = floor_hour(hour)
    rows1, rows2 = await _sys(cell_plans("project_id = %s AND start_time >= %s AND start_time < %s", (project_id, hour, hour + H)))
    cells = assemble(rows1, rows2)
    D = A.mod()
    async with D.S["pool"].connection() as c:
        await c.execute("SELECT set_config('app.ws','*',false), set_config('app.uid','0',false), set_config('app.role','admin',false), set_config('role','none',false)")
        async with c.transaction():
            await c.execute("DELETE FROM tr_stats_hourly WHERE project_id = %s AND hour = %s", (project_id, hour))
            for x in cells:
                await c.execute(_INS, (x["project_id"], x["hour"], x["environment"], x["run_type"], x["name"], x["model"], x["is_root"], x["is_private"], x["n"], x["n_err"], x["lat_sum"],
                                       x["n_lat"], x["ttft_sum"], x["n_ttft"], x["tok_in"], x["tok_out"], x["tok_total"], x["cost"], x["lat_hist"], x["ttft_hist"]))
    return len(cells)

async def catch_up(now=None, limit=CATCH_UP_MAX):
    """Worker job. Rolls the finished hours that have runs but no rollup rows (oldest first, at most `limit`) and the last two finished hours again (late runs).
    The scan looks 6 hours back, or the whole keep window when it has not done so for 6 hours (first run after a start). -> number of hours rolled."""
    now = now or dt.datetime.now(dt.timezone.utc); cur = floor_hour(now)
    full = _state["full"] is None or now - _state["full"] > 6 * H
    since = cur - dt.timedelta(days=_env_int("AOS_STATS_KEEP_DAYS", 400)) if full else cur - 6 * H
    D = A.mod()
    todo = await D.q("""SELECT t.project_id, t.h FROM (SELECT project_id, date_trunc('hour', start_time, 'UTC') h FROM tr_runs WHERE start_time >= %s AND start_time < %s GROUP BY 1, 2) t
        WHERE NOT EXISTS (SELECT 1 FROM tr_stats_hourly s WHERE s.project_id = t.project_id AND s.hour = t.h)
           OR t.h >= %s ORDER BY t.h LIMIT %s""", (since, cur, cur - 2 * H, int(limit)))
    for r in todo: await compute_hour(r["project_id"], r["h"])
    if full:
        _state["full"] = now
        await D.q("DELETE FROM tr_stats_hourly WHERE hour < %s", (cur - dt.timedelta(days=_env_int("AOS_STATS_KEEP_DAYS", 400)),))
    return len(todo)

async def ensure_hours(pairs):
    """Retention calls this with the (project_id, start_time) of the runs it is about to delete: every hour not rolled yet is rolled first. An error stops the deletion."""
    seen = {(pid, floor_hour(t)) for pid, t in pairs}
    if not seen: return 0
    D = A.mod(); have = {(r["project_id"], r["hour"]) for r in await D.q(
        "SELECT project_id, hour FROM tr_stats_hourly WHERE (project_id, hour) IN (SELECT * FROM unnest(%s::int[], %s::timestamptz[]))", ([p for p, _ in seen], [h for _, h in seen]))}
    todo = sorted(seen - have, key=lambda x: x[1])
    for pid, h in todo: await compute_hour(pid, h)
    return len(todo)

async def ensure_partition(rel):
    """Same for a whole partition that retention is about to drop. `rel` is a tr_runs_YYYYMM name checked by the caller."""
    D = A.mod()
    rows = await D.q(f"SELECT DISTINCT project_id, date_trunc('hour', start_time, 'UTC') h FROM {rel}")
    return await ensure_hours([(r["project_id"], r["h"]) for r in rows])

# ---------------------------------------------------------------- read side: what the table can answer
class Incompat(Exception):
    def __init__(self, field): self.field = field

def parse_filter(s):
    """Filter DSL -> ({field: value}, errors_only). Only and(...) of eq(run_type|name|model|environment, "x") and eq(status,"error") is understood, anything else -> Incompat(field)."""
    if not s or not s.strip(): return {}, False
    import obs
    try: t = obs._tokens(s)
    except HTTPException: raise Incompat("filter") from None
    pos, preds = 0, {}
    def take(kind=None, val=None):
        nonlocal pos
        if pos >= len(t) or (kind and t[pos][0] != kind) or (val and t[pos][1] != val): raise Incompat("filter")
        pos += 1; return t[pos - 1][1]
    def expr():
        fn = take("id"); take("p", "(")
        if fn == "and":
            expr()
            while pos < len(t) and t[pos] == ("p", ","): take("p", ","); expr()
        elif fn == "eq":
            f = take("id"); take("p", ","); v = take("str")
            if f not in ("run_type", "name", "model", "environment", "status") or preds.get(f, v) != v: raise Incompat(f)
            if f == "status" and v != "error": raise Incompat("status")
            preds[f] = v
        else: raise Incompat(fn)
        take("p", ")")
    expr()
    if pos != len(t): raise Incompat("filter")
    return {k: v for k, v in preds.items() if k != "status"}, preds.get("status") == "error"

def part_reason(p, scope):
    """None when the rollup can answer this measured value, else the name of what it cannot do."""
    if scope == "tree": return "scope tree"
    try: _, err = parse_filter(p.filter)
    except Incompat as e: return e.field
    m, a = p.metric, p.eff_agg()
    if err and m != "count": return "status"
    if m == "count": return None
    if m in ("latency", "ttft"): return None if a in ("avg", "sum", "p50", "p90", "p95", "p99") else f"agg {a}"
    if m == "tokens": return None if p.field in ("total", "input", "output") and a == "sum" else (f"field {p.field}" if p.field not in ("total", "input", "output") else f"agg {a}")
    if m == "cost": return None if p.field == "total" and a == "sum" else (f"field {p.field}" if p.field != "total" else f"agg {a}")
    return m   # feedback

def spec_reason(spec, bucket):
    if bucket == "5m": return "bucket 5m"
    g = spec.group_by
    if g and g.attr in ("tag", "metadata"): return g.attr
    for s in spec.series:
        for p in ([s.ratio.num, s.ratio.den] if s.metric == "ratio" else [s]):
            r = part_reason(p, s.scope)
            if r: return r
    return None

class Acc:
    __slots__ = COLS + ("lat_hist", "ttft_hist")
    def __init__(self):
        for k in COLS: setattr(self, k, 0)
        self.lat_hist = [0] * BINS; self.ttft_hist = [0] * BINS
    def add(self, c):
        for k in COLS: setattr(self, k, getattr(self, k) + c[k])
        for k in ("lat_hist", "ttft_hist"):
            h = getattr(self, k); x = c[k]
            for i in range(BINS): h[i] += x[i]

def value(p, a, err_only):
    m, ag = p.metric, p.eff_agg()
    if m == "count": return float(a.n_err if err_only else a.n)
    if m in ("latency", "ttft"):
        s, n, h = (a.lat_sum, a.n_lat, a.lat_hist) if m == "latency" else (a.ttft_sum, a.n_ttft, a.ttft_hist)
        if not n: return None
        if ag == "avg": return s / n
        if ag == "sum": return float(s)
        return quantile(h, int(ag[1:]) / 100)
    if m == "tokens": return float({"total": a.tok_total, "input": a.tok_in, "output": a.tok_out}[p.field])
    return float(a.cost)

def _match(c, preds, root_only):
    return (not root_only or c["is_root"]) and all(c[k] == v for k, v in preds.items())

def _gkey(c, g):
    if g is None: return "__all__"
    v = {"run_name": c["name"], "run_type": c["run_type"], "model": c["model"], "project": str(c["project_id"])}[g.attr]
    return v if v != "" else "__none__"

RANK = {"count": lambda a: a.n, "cost": lambda a: a.cost, "errors": lambda a: a.n_err}

def _eval(p, scope, cells, grp, bucket, zone, by_time):
    """-> {(t, g) | g: value} for one measured value; t = local start of the bucket (naive datetime), the same grid charts.grid uses."""
    import charts
    preds, err = parse_filter(p.filter); sel = [c for c in cells if _match(c, preds, scope == "trace")]
    top = None
    if grp:
        tot = defaultdict(Acc)
        for c in sel: tot[_gkey(c, grp)].add(c)
        top = set(sorted(tot, key=lambda k: (-float(RANK[grp.rank_by](tot[k])), k))[:grp.top])
    accs = defaultdict(Acc)
    for c in sel:
        g = _gkey(c, grp)
        if grp and g not in top: g = "__other__"
        key = (charts._floor(c["hour"].astimezone(zone).replace(tzinfo=None), bucket), g) if by_time else g
        accs[key].add(c)
    return {k: value(p, a, err) for k, a in accs.items()}

def _series(s, cells, spec, bucket, zone, by_time):
    """-> [(t, g, v)] or [(g, v)] for one series, in the shape charts.compile / compile_totals return."""
    if s.metric != "ratio":
        d = _eval(s, s.scope, cells, spec.group_by, bucket, zone, by_time)
        return [(*(k if by_time else (k,)), v) for k, v in d.items()]
    num0 = 0.0 if s.ratio.num.eff_agg() in ("count", "sum") else None
    n = _eval(s.ratio.num, s.scope, cells, None, bucket, zone, by_time); d = _eval(s.ratio.den, s.scope, cells, None, bucket, zone, by_time)
    out = []
    for k, dv in d.items():
        if not dv: out.append((*(k if by_time else ("__all__",)), None)); continue
        nv = n.get(k); nv = num0 if nv is None else nv
        out.append((*(k if by_time else ("__all__",)), None if nv is None else nv / dv))
    return out

# ---------------------------------------------------------------- read side: decide + answer
async def cutoff(execute, spec, now):
    """C = the first hour of raw runs for the project(s) that already hold older rollup rows (None = no project needs the rollup). The viewer's own projects only (RLS)."""
    pids = list(spec.source.projects)
    sql = ("SELECT p.id AS pid, (SELECT min(start_time) FROM tr_runs r WHERE r.project_id = p.id) AS raw_min, (SELECT min(hour) FROM tr_stats_hourly s WHERE s.project_id = p.id) AS roll_min FROM projects p"
           + (" WHERE p.id = ANY(%s::int[])" if pids else ""))
    rows = (await execute([(sql, [pids] if pids else [])], 4000))[0]
    best = None; live = floor_hour(now) - 2 * H
    for _pid, raw_min, roll_min in rows:
        if roll_min is None: continue
        c = live if raw_min is None else floor_hour(raw_min)
        if raw_min is not None and roll_min >= c: continue   # nothing rolled before the first raw run
        best = c if best is None else max(best, c)
    return best

async def answer(execute, spec, start, end, tz, bucket, zone, now=None):
    """-> None (use the raw runs as usual) | {"mode": "rollup", "cutoff", "ts", "tot", "private_excluded"} | {"mode": "limited", "cutoff", "field"}."""
    import admin, charts
    now = now or dt.datetime.now(dt.timezone.utc)
    if start >= now - dt.timedelta(days=1): return None   # retention keeps a run at least one day
    try: c = await cutoff(execute, spec, now)
    except Exception: return None   # noqa: BLE001  (table not migrated yet: charts keep working from the raw runs)
    if c is None or start >= c: return None
    why = spec_reason(spec, bucket)
    if why: return {"mode": "limited", "cutoff": c, "field": why}
    live = floor_hour(now) - 2 * H; lo = floor_hour(start); hi_roll = min(floor_hour(end - dt.timedelta(microseconds=1)) + H, live)
    where, wp = [], []
    if spec.source.projects: where.append("project_id = ANY(%s::int[])"); wp.append(list(spec.source.projects))
    if spec.source.environments:
        import envs; where.append("environment = ANY(%s::text[])"); wp.append([envs.norm_http(x) for x in spec.source.environments])
    need_hist = any(p.eff_agg().startswith("p") and p.metric in ("latency", "ttft") for s in spec.series for p in ([s.ratio.num, s.ratio.den] if s.metric == "ratio" else [s]))
    sel = ("hour, project_id, environment, run_type, name, model, is_root, is_private, " + ", ".join(COLS) + (", lat_hist, ttft_hist" if need_hist else ", NULL::int[] AS lat_hist, NULL::int[] AS ttft_hist"))
    plans = [(f"SELECT {sel} FROM tr_stats_hourly WHERE hour >= %s AND hour < %s" + "".join(f" AND {w}" for w in where) + f" LIMIT {CELL_LIMIT + 1}", [lo, hi_roll] + wp)]
    if end > live:
        lw = "start_time >= %s AND start_time < %s"; lp = [max(start, live), end]
        if spec.source.projects: lw += " AND project_id = ANY(%s::int[])"; lp.append(list(spec.source.projects))
        if spec.source.environments:
            import envs; lw += f" AND {envs.ENV_SQL} = ANY(%s::text[])"; lp.append([envs.norm_http(x) for x in spec.source.environments])
        plans += cell_plans(lw, lp)
    res = await execute(plans, 8000)
    if len(res[0]) > CELL_LIMIT: raise HTTPException(422, "Quá nhiều dữ liệu cũ cho một biểu đồ, hãy thu hẹp khoảng thời gian hoặc bộ lọc / too much old data for one chart")
    names = DIMS.split(", ") + list(COLS) + ["lat_hist", "ttft_hist"]
    cells = []
    for r in res[0]:
        x = dict(zip(names, r)); x["cost"] = float(x["cost"]); x["lat_hist"] = list(x["lat_hist"] or [0] * BINS); x["ttft_hist"] = list(x["ttft_hist"] or [0] * BINS); cells.append(x)
    if len(res) > 1: cells += assemble(res[1], res[2])
    u = admin.cur() or {}; is_admin = (not u) or u.get("role") == "admin"
    if not is_admin: cells = [x for x in cells if not x["is_private"]]
    ts = [_series(s, cells, spec, bucket, zone, True) for s in spec.series] if spec.viz in ("line", "stacked_bar") else []
    tot = [_series(s, cells, spec, bucket, zone, False) for s in spec.series]
    return {"mode": "rollup", "cutoff": c, "ts": ts, "tot": tot, "private_excluded": not is_admin}
