"""GĐ12.5 Chart query engine (reduced): one JSON spec -> one safe SQL over tr_runs (+ feedback) -> time buckets x series.
Rules: allow-list only (metric / field / agg / group attribute / bucket are Literals, the metadata path is regex-checked), every value is a query parameter,
every query runs under the viewer's RLS role (workspace + private chat traces) with a statement timeout, a range cap, a bucket cap (500), a series cap (top <= 20 + other),
a row cap, a 60 s cache per (workspace, user, spec, range) and a concurrency cap. Empty bucket = NULL (never 0); a zero denominator = NULL.
Endpoints: POST /api/obs/charts/query, GET /api/obs/chart_templates. `obs.dashboard()` is untouched (old UI). Rollup (12.9, app/stats_rollup.py) fills `sources.rollup_to` / `limited_before`.
Not here (GĐ15): dataset source, donut / Sankey, line-per-filter series, feedback soft-delete (12.65), dashboard CRUD (12.6)."""
import asyncio, copy, datetime as dt, hashlib, json, os, time
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import psycopg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

class A:   # lazy handle: deepagent_api imports this module while it is still being imported
    @staticmethod
    def mod():
        import deepagent_api; return deepagent_api

router = APIRouter()

MAX_BUCKETS = 500
MAX_SERIES = 8
MAX_SPEC_BYTES = 8192
MAX_CELLS = 12000            # rows of one time-series answer (500 buckets x 21 groups = 10500 fits)
CACHE_SECONDS = 60
CACHE_MAX = 256

def _env_int(name, default):
    try: return max(1, int(os.environ.get(name, default)))
    except ValueError: return default

# ---------------------------------------------------------------- the spec
Metric = Literal["count", "latency", "ttft", "tokens", "cost", "feedback"]
Agg = Literal["count", "avg", "sum", "min", "max", "p50", "p90", "p95", "p99"]
Fld = Literal["total", "input", "output", "cache_read", "reasoning"]
DEFAULT_AGG = {"count": "count", "latency": "avg", "ttft": "avg", "tokens": "sum", "cost": "sum", "feedback": "avg"}

class Part(BaseModel):
    """One measured value (also the numerator / denominator of a ratio)."""
    model_config = ConfigDict(extra="forbid")
    metric: Metric = "count"
    field: Fld = "total"                       # tokens / cost only (detail kinds come from usage_details / cost_details)
    agg: Agg | None = None                     # None = the usual one for the metric
    feedback_key: str | None = Field(None, min_length=1, max_length=80)
    feedback_value: str | None = Field(None, min_length=1, max_length=80)   # label feedback: count the rows with this value (agg count)
    filter: str = Field("", max_length=2000)   # the existing filter DSL (obs.compile_filter)

    def eff_agg(self): return self.agg or ("count" if self.feedback_value else DEFAULT_AGG[self.metric])

    @model_validator(mode="after")
    def _check_part(self): return _check_part(self)

def _check_part(self):
    if self.metric == "count" and self.eff_agg() != "count": raise ValueError("metric count only supports agg count")
    if self.metric == "feedback" and not self.feedback_key: raise ValueError("metric feedback needs feedback_key")
    if self.metric != "feedback" and (self.feedback_key or self.feedback_value): raise ValueError("feedback_key / feedback_value only with metric feedback")
    if self.feedback_value and self.eff_agg() != "count": raise ValueError("feedback_value only supports agg count")
    return self

class Ratio(BaseModel):
    model_config = ConfigDict(extra="forbid")
    num: Part
    den: Part

class Series(Part):
    metric: Literal["count", "latency", "ttft", "tokens", "cost", "feedback", "ratio"] = "count"
    scope: Literal["run", "trace", "tree"] = "run"   # run = every run matching the filter; trace = root runs; tree = root runs whose trace has a matching run
    ratio: Ratio | None = None
    label: str | None = Field(None, max_length=80)

    def eff_agg(self): return self.agg or ("count" if self.feedback_value else DEFAULT_AGG.get(self.metric, "count"))

    @model_validator(mode="after")
    def _check_part(self):
        if self.metric == "ratio":
            if not self.ratio: raise ValueError("metric ratio needs ratio{num, den}")
            return self
        if self.ratio: raise ValueError("ratio only with metric ratio")
        return _check_part(self)

class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    projects: list[int] = Field(default_factory=list, max_length=50)
    environments: list[str] = Field(default_factory=list, max_length=20)

class GroupBy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attr: Literal["run_name", "run_type", "model", "tag", "project", "metadata"]
    path: str | None = Field(None, pattern=r"^[A-Za-z0-9_\-.]{1,80}$")   # metadata key path, dots = nesting
    top: int = Field(10, ge=1, le=20)
    rank_by: Literal["count", "cost", "errors"] = "count"

    @model_validator(mode="after")
    def _check(self):
        if self.attr == "metadata" and not self.path: raise ValueError("group_by metadata needs path")
        if self.attr != "metadata" and self.path: raise ValueError("path only with attr metadata")
        return self

class ChartSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Source = Field(default_factory=Source)
    series: list[Series] = Field(min_length=1, max_length=MAX_SERIES)
    group_by: GroupBy | None = None
    viz: Literal["kpi", "line", "stacked_bar", "ranked_bar", "table"] = "line"
    bucket: Literal["auto", "5m", "hour", "day", "week"] = "auto"

    @model_validator(mode="after")
    def _check(self):
        if self.group_by and len(self.series) > 1: raise ValueError("group_by and several series are mutually exclusive")
        if self.group_by and self.series[0].metric == "ratio": raise ValueError("group_by with a ratio is not supported")
        if self.viz in ("ranked_bar", "table") and len(self.series) != 1: raise ValueError(f"{self.viz} takes exactly one series")
        if len(self.model_dump_json(exclude_none=True).encode()) > MAX_SPEC_BYTES: raise ValueError("spec larger than 8 KB")
        return self

# ---------------------------------------------------------------- time axis
UNITS = {"5m": (dt.timedelta(minutes=5), "5 minutes"), "hour": (dt.timedelta(hours=1), "1 hour"),
         "day": (dt.timedelta(days=1), "1 day"), "week": (dt.timedelta(weeks=1), "1 week")}

def pick_bucket(bucket, start, end):
    """auto = range / 120 rounded UP to 5 min, hour, day or week. An explicit bucket giving more than 500 buckets -> 422."""
    secs = (end - start).total_seconds()
    if bucket == "auto":
        raw = secs / 120
        bucket = "5m" if raw <= 300 else "hour" if raw <= 3600 else "day" if raw <= 86400 else "week"
    if secs / UNITS[bucket][0].total_seconds() + 2 > MAX_BUCKETS:
        raise HTTPException(422, f"Khoảng thời gian cần quá {MAX_BUCKETS} cột: chọn bucket lớn hơn / too many buckets")
    return bucket

def _floor(local, bucket):
    if bucket == "5m": return local.replace(minute=local.minute // 5 * 5, second=0, microsecond=0)
    if bucket == "hour": return local.replace(minute=0, second=0, microsecond=0)
    d = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return d if bucket == "day" else d - dt.timedelta(days=d.weekday())   # week starts on Monday like date_trunc

def grid(bucket, start, end, zone):
    """Local (naive) start of every bucket, the same grid the SQL buckets fall on."""
    first = _floor(start.astimezone(zone).replace(tzinfo=None), bucket)
    last = (end - dt.timedelta(microseconds=1)).astimezone(zone).replace(tzinfo=None)
    out, t = [], first
    while t <= last: out.append(t); t += UNITS[bucket][0]
    return out

def _bucket_sql(bucket):
    if bucket == "5m": return "date_bin('5 minutes'::interval, st AT TIME ZONE %s, TIMESTAMP '2001-01-01')"
    return f"date_trunc('{bucket}', st AT TIME ZONE %s)"   # bucket is from the allow-list above

# ---------------------------------------------------------------- SQL pieces
class _B:
    """SQL text and its parameters, appended in the order they appear in the text."""
    def __init__(self): self.sql, self.p = [], []
    def add(self, sql, *p): self.sql.append(sql); self.p.extend(p); return self
    def out(self): return " ".join(self.sql), self.p

def _detail(col, key): return f"(CASE WHEN jsonb_typeof({col}->'{key}') = 'number' THEN ({col}->>'{key}')::numeric END)"   # key comes from the Fld Literal

def value_sql(p):
    m, f = p.metric, p.field
    if m == "count": return "1"
    if m == "latency": return "latency_ms"
    if m == "ttft": return "(EXTRACT(EPOCH FROM (first_token_time - start_time)) * 1000)"
    if m == "tokens": return {"total": "total_tokens", "input": "input_tokens", "output": "output_tokens"}.get(f) or _detail("usage_details", f)
    if m == "cost": return {"total": "cost_usd", "input": "input_cost", "output": "output_cost"}.get(f) or _detail("cost_details", f)
    return "NULL::double precision"   # feedback: the value comes from the joined feedback row

def agg_sql(agg, col="v"):
    if agg in ("avg", "sum", "min", "max", "count"): return f"{agg}({col})"
    return f"percentile_cont({int(agg[1:]) / 100}) WITHIN GROUP (ORDER BY {col}::double precision)"   # agg is from the Agg Literal

def _group_expr(g):
    """-> (sql, params, extra FROM text). A missing value is the group '__none__'."""
    if g is None: return "'__all__'", [], ""
    if g.attr == "metadata": return "coalesce((metadata #>> %s::text[]), '__none__')", [g.path.split(".")], ""
    if g.attr == "tag":
        return "coalesce(u.tag, '__none__')", [], " LEFT JOIN LATERAL unnest(CASE WHEN cardinality(tags) = 0 THEN ARRAY[NULL]::text[] ELSE tags END) AS u(tag) ON true"
    col = {"run_name": "name", "run_type": "run_type", "model": "model", "project": "project_id::text"}[g.attr]
    return f"coalesce({col}::text, '__none__')", [], ""

def _rows(p, scope, spec, start, end, grp):
    """Subquery with one row per run to aggregate: st, rid, v, g, c (cost), e (is error)."""
    import obs, envs
    flt, fp = obs.compile_filter(p.filter)
    tree = scope == "tree"
    gsql, gp, gfrom = _group_expr(grp)
    b = _B().add(f"SELECT start_time AS st, id AS rid, {value_sql(p)} AS v, {gsql} AS g, coalesce(cost_usd, 0) AS c, (status = 'error') AS e FROM tr_runs{' root' if tree else ''}{gfrom}", *gp)
    b.add("WHERE start_time >= %s::timestamptz AND start_time < %s::timestamptz", start, end)
    if spec.source.projects: b.add("AND project_id = ANY(%s::int[])", list(spec.source.projects))
    if spec.source.environments:
        envsql = envs.ENV_SQL.replace("tr_runs.", "root.") if tree else envs.ENV_SQL
        b.add(f"AND {envsql} = ANY(%s::text[])", [envs.norm_http(x) for x in spec.source.environments])
    if scope in ("trace", "tree"): b.add("AND parent_id IS NULL")
    if tree: b.add(f"AND EXISTS (SELECT 1 FROM tr_runs WHERE tr_runs.trace_id = root.trace_id AND tr_runs.start_time >= %s::timestamptz AND tr_runs.start_time < %s::timestamptz AND {flt})", start, end + dt.timedelta(days=1), *fp)
    else: b.add(f"AND {flt}", *fp)
    sql, params = b.out()
    if p.metric == "feedback":
        fb = _B().add(f"SELECT r0.st, r0.rid, {'1' if p.eff_agg() == 'count' else 'fb.score'} AS v, r0.g, r0.c, r0.e FROM (")
        fb.add(sql, *params).add(") r0 JOIN feedback fb ON fb.run_id = r0.rid AND fb.key = %s", p.feedback_key)
        if p.feedback_value: fb.add("AND fb.value = %s", p.feedback_value)
        sql, params = fb.out()
    return sql, params

RANK = {"count": "count(*)", "cost": "sum(c)", "errors": "count(*) FILTER (WHERE e)"}

def _frag(p, scope, spec, start, end, tz, bucket, grp, by_time):
    """Aggregate query: columns (t, g, v) when by_time else (g, v). With a group_by, g is a top-N key or '__other__'."""
    rows, rp = _rows(p, scope, spec, start, end, grp)
    b = _B().add("WITH rows AS (").add(rows, *rp).add(")")
    gsel = "g"
    if grp:
        b.add(f", top AS (SELECT g FROM rows GROUP BY g ORDER BY {RANK[grp.rank_by]} DESC NULLS LAST, g LIMIT %s)", grp.top)
        gsel = "CASE WHEN g IN (SELECT g FROM top) THEN g ELSE '__other__' END"
    agg = agg_sql(p.eff_agg())
    if by_time: b.add(f"SELECT {_bucket_sql(bucket)} AS t, {gsel} AS g, {agg} AS v FROM rows GROUP BY 1, 2", tz)
    else: b.add(f"SELECT {gsel} AS g, {agg} AS v FROM rows GROUP BY 1" + (" ORDER BY v DESC NULLS LAST LIMIT 22" if grp else ""))
    return b.out()

def _axis(bucket, start, end, zone):
    g = grid(bucket, start, end, zone)
    return "SELECT generate_series(%s::timestamp, %s::timestamp, %s::interval) AS t", [g[0], g[-1], UNITS[bucket][1]]

def _ratio_sql(s, spec, start, end, tz, bucket, by_time):
    """num / den of two sub-queries joined per bucket (or over the whole range); empty denominator -> NULL; no numerator rows -> 0 for count / sum."""
    num0 = "0" if s.ratio.num.eff_agg() in ("count", "sum") else "NULL"
    n, np_ = _frag(s.ratio.num, s.scope, spec, start, end, tz, bucket, None, by_time)
    d, dp = _frag(s.ratio.den, s.scope, spec, start, end, tz, bucket, None, by_time)
    div = f"coalesce(n.v::float8, {num0}) / NULLIF(d.v::float8, 0)"
    b = _B()
    if by_time:
        ax, axp = _axis(bucket, start, end, ZoneInfo(tz))
        b.add("WITH b AS (").add(ax, *axp).add("), n AS (").add(n, *np_).add("), d AS (").add(d, *dp)
        b.add(f") SELECT b.t, '__all__' AS g, CASE WHEN d.v IS NULL THEN NULL ELSE {div} END AS v FROM b LEFT JOIN n ON n.t = b.t LEFT JOIN d ON d.t = b.t ORDER BY b.t")
    else:
        b.add("SELECT '__all__' AS g, ").add(f"CASE WHEN d.v IS NULL THEN NULL ELSE {div} END AS v FROM (SELECT 1) o LEFT JOIN (").add(n, *np_).add(") n ON true LEFT JOIN (").add(d, *dp).add(") d ON true")
    return b.out()

def compile(spec, start, end, tz="UTC"):
    """-> [(sql, params)] one time-series query per series; columns (t, g, v), t = local bucket start."""
    zone = _zone(tz); bucket = pick_bucket(spec.bucket, start, end); out = []
    for s in spec.series:
        if s.metric == "ratio": out.append(_ratio_sql(s, spec, start, end, tz, bucket, True)); continue
        frag, fp = _frag(s, s.scope, spec, start, end, tz, bucket, spec.group_by, True)
        ax, axp = _axis(bucket, start, end, zone)
        b = _B().add("WITH b AS (").add(ax, *axp).add("), a AS (").add(frag, *fp).add(")")
        if spec.group_by: b.add(f", k AS (SELECT DISTINCT g FROM a) SELECT b.t, k.g, a.v FROM b CROSS JOIN k LEFT JOIN a ON a.t = b.t AND a.g = k.g ORDER BY k.g, b.t LIMIT {MAX_CELLS + 1}")
        else: b.add(f"SELECT b.t, a.g, a.v FROM b LEFT JOIN a ON a.t = b.t ORDER BY b.t LIMIT {MAX_CELLS + 1}")
        out.append(b.out())
    return out

def compile_totals(spec, start, end, tz="UTC"):
    """-> [(sql, params)] one query per series over the whole range; columns (g, v)."""
    _zone(tz); bucket = pick_bucket(spec.bucket, start, end); out = []
    for s in spec.series:
        out.append(_ratio_sql(s, spec, start, end, tz, bucket, False) if s.metric == "ratio" else _frag(s, s.scope, spec, start, end, tz, bucket, spec.group_by, False))
    return out

def _zone(tz):
    try: return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError, OSError): raise HTTPException(422, f"Múi giờ không hợp lệ: {tz[:40]!r}") from None

# ---------------------------------------------------------------- run
_CACHE: dict = {}
_SEM: dict = {}

def _semaphore():
    n = _env_int("AOS_CHART_CONCURRENCY", 2); key = (id(asyncio.get_running_loop()), n)
    if _SEM.get("k") != key: _SEM.update(k=key, s=asyncio.Semaphore(n))
    return _SEM["s"]

def _num(v):
    try: return None if v is None else float(v)
    except (TypeError, ValueError): return None

def _cols(r): return tuple(r.values()) if isinstance(r, dict) else tuple(r)

async def _execute(plans, timeout_ms):
    """All queries in one transaction under the viewer's RLS role, with a statement timeout (cancelled queries -> 504)."""
    D = A.mod(); ctx = D.S["rls"]() if D.S.get("rls") else ("*", "0", "admin")
    try:
        async with D.S["pool"].connection() as c:
            await c.execute("SELECT set_config('app.ws',%s,false), set_config('app.uid',%s,false), set_config('app.role',%s,false), set_config('role', CASE WHEN %s ~ '^[0-9]+$' THEN 'aos_rls' ELSE 'none' END, false)", ctx + (ctx[0],))
            async with c.transaction():
                await c.execute("SELECT set_config('statement_timeout', %s, true)", (str(int(timeout_ms)),))
                return [[_cols(r) for r in await (await c.execute(sql, params)).fetchall()] for sql, params in plans]
    except psycopg.errors.QueryCanceled:
        raise HTTPException(504, "Biểu đồ quá chậm, hãy thu hẹp khoảng thời gian hoặc bộ lọc / chart query timed out") from None

def _label(key, names):
    return "(none)" if key == "__none__" else "other" if key == "__other__" else names.get(key, key)

async def run(spec, start, end, tz="UTC"):
    """Answer one ChartSpec for [start, end). -> {buckets, series:[{key,label,values}], totals, truncated, sources}."""
    max_days = _env_int("AOS_CHART_MAX_DAYS", 400)
    if end <= start: raise HTTPException(422, "Khoảng thời gian không hợp lệ / empty range")
    if end - start > dt.timedelta(days=max_days): raise HTTPException(422, f"Khoảng thời gian tối đa {max_days} ngày / range too long")
    zone = _zone(tz); import admin
    u = admin.cur() or {}
    key = hashlib.sha256(f"{admin.cur_ws()}|{u.get('id')}|{u.get('role')}|{bool(u.get('demo'))}|{tz}|{start.isoformat()}|{end.isoformat()}|{spec.model_dump_json()}".encode()).hexdigest()
    hit = _CACHE.get(key)
    if hit and hit[0] > time.monotonic(): return copy.deepcopy(hit[1])
    ts_viz = spec.viz in ("line", "stacked_bar")
    bucket = pick_bucket(spec.bucket, start, end)
    import stats_rollup   # GĐ12.9: data older than the oldest raw run comes from the hourly rollup when the spec allows it
    rp = await stats_rollup.answer(_execute, spec, start, end, tz, bucket, zone)
    if rp and rp["mode"] == "rollup": ts_res, tot_res = rp["ts"], rp["tot"]
    else:
        ts_plans = compile(spec, start, end, tz) if ts_viz else []
        plans = ts_plans + compile_totals(spec, start, end, tz)
        sem = _semaphore()
        try: await asyncio.wait_for(sem.acquire(), 10)
        except asyncio.TimeoutError: raise HTTPException(429, "Đang có quá nhiều biểu đồ chạy cùng lúc, thử lại sau / too many chart queries") from None
        try: res = await _execute(plans, _env_int("AOS_CHART_TIMEOUT_MS", 8000))
        finally: sem.release()
        ts_res, tot_res = res[:len(ts_plans)], res[len(ts_plans):]
    names = {}
    if spec.group_by and spec.group_by.attr == "project":
        ids = [int(r[0]) for rows in tot_res for r in rows if r[0].isdigit()]
        if ids:
            D = A.mod(); names = {str(r["id"]): r["name"] for r in await D.q("SELECT id, name FROM projects WHERE id = ANY(%s::int[])", (ids,))}
    cells = grid(bucket, start, end, zone) if ts_viz else []
    index = {t: i for i, t in enumerate(cells)}
    truncated = False; series, totals = [], {}
    for si, s in enumerate(spec.series):
        base = s.label or (f"{s.metric}.{s.eff_agg()}" if s.metric != "ratio" else "ratio")
        tot = {r[0]: _num(r[1]) for r in tot_res[si]}
        keys = sorted(tot, key=lambda k: (k == "__other__", -(tot[k] if tot[k] is not None else float("-inf")), k))
        vals = {}
        if ts_viz:
            rows = ts_res[si]
            if len(rows) > MAX_CELLS: truncated = True; rows = rows[:MAX_CELLS]
            for t, g, v in rows:
                i = index.get(t)
                if i is None: continue
                vals.setdefault(g, [None] * len(cells))[i] = _num(v)
            keys = keys + [k for k in vals if k not in tot]
        if not spec.group_by: keys = ["__all__"]
        for k in keys:
            sk = f"s{si}" if not spec.group_by else k
            series.append({"key": sk, "label": base if not spec.group_by else _label(k, names), "values": vals.get(k, [None] * len(cells) if ts_viz else [])})
            totals[sk] = tot.get(k)
    out = {"buckets": [t.replace(tzinfo=zone).isoformat() for t in cells], "series": series, "totals": totals, "truncated": truncated,
           "sources": {"raw_from": start.isoformat(), "rollup_to": None}}
    if rp:   # GĐ12.9
        c = rp["cutoff"].isoformat()
        if rp["mode"] == "rollup":
            out["sources"] = {"raw_from": c, "rollup_to": c, "private_excluded": rp["private_excluded"],
                              "note": "Số liệu trước mốc này lấy từ bảng tổng hợp theo giờ (phần trăm là gần đúng)" + ("; không gồm trace riêng tư của người dùng" if rp["private_excluded"] else "") +
                                      " / Data before this point comes from the hourly rollup (percentiles are approximate)" + ("; private user traces are excluded" if rp["private_excluded"] else "")}
        else:
            out["sources"].update(limited_before=c, limited_field=rp["field"],
                                  limited_reason=f"Dữ liệu trước {c[:10]} không lọc được theo {rp['field']} / Data before {c[:10]} cannot be filtered by {rp['field']}")
    if len(_CACHE) >= CACHE_MAX:
        now = time.monotonic()
        for k in [k for k, v in _CACHE.items() if v[0] <= now] or [next(iter(_CACHE))]: _CACHE.pop(k, None)
    _CACHE[key] = (time.monotonic() + CACHE_SECONDS, copy.deepcopy(out))
    return out

# ---------------------------------------------------------------- API
class QueryIn(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    spec: ChartSpec
    from_: dt.datetime | None = Field(None, alias="from")
    to: dt.datetime | None = None
    hours: float | None = Field(None, gt=0, le=24 * 400)
    tz: str = Field("UTC", max_length=64)

def _utc(d): return d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d.astimezone(dt.timezone.utc)

def resolve_range(from_, to, hours):
    if from_ is not None:
        start = _utc(from_); end = _utc(to) if to is not None else dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
    else:   # relative ranges end on the minute so repeated calls share the 60 s cache
        end = _utc(to) if to is not None else dt.datetime.now(dt.timezone.utc).replace(second=0, microsecond=0)
        start = end - dt.timedelta(hours=hours or 24)
    return start, end

@router.post("/api/obs/charts/query")
async def query(b: QueryIn):
    start, end = resolve_range(b.from_, b.to, b.hours)
    return await run(b.spec, start, end, b.tz)

LLM = 'eq(run_type,"llm")'
TEMPLATES = [
    {"id": "error_rate", "title": {"vi": "Tỉ lệ lỗi", "en": "Error rate"},
     "spec": {"series": [{"metric": "ratio", "scope": "trace", "ratio": {"num": {"metric": "count", "filter": 'eq(status,"error")'}, "den": {"metric": "count"}}, "label": "error rate"}], "viz": "line"}},
    {"id": "latency_by_model", "title": {"vi": "Độ trễ theo model (p50)", "en": "Latency by model (p50)"},
     "spec": {"series": [{"metric": "latency", "agg": "p50", "filter": LLM}], "group_by": {"attr": "model", "top": 10}, "viz": "line"}},
    {"id": "run_volume", "title": {"vi": "Số lượt chạy", "en": "Run volume"},
     "spec": {"series": [{"metric": "count", "scope": "trace", "label": "traces"}], "viz": "stacked_bar"}},
    {"id": "tokens", "title": {"vi": "Token (vào / ra)", "en": "Tokens (input / output)"},
     "spec": {"series": [{"metric": "tokens", "field": "input", "agg": "sum", "label": "input"}, {"metric": "tokens", "field": "output", "agg": "sum", "label": "output"}], "viz": "stacked_bar"}},
    {"id": "most_expensive_model", "title": {"vi": "Model tốn nhất", "en": "Most expensive model"},
     "spec": {"series": [{"metric": "cost", "agg": "sum", "filter": LLM}], "group_by": {"attr": "model", "top": 10, "rank_by": "cost"}, "viz": "ranked_bar"}},
    {"id": "cache_hit_rate", "title": {"vi": "Tỉ lệ trúng cache", "en": "Cache hit rate"},
     "spec": {"series": [{"metric": "ratio", "ratio": {"num": {"metric": "tokens", "field": "cache_read", "agg": "sum", "filter": LLM}, "den": {"metric": "tokens", "field": "input", "agg": "sum", "filter": LLM}}, "label": "cache hit rate"}], "viz": "line"}},
]

@router.get("/api/obs/chart_templates")
async def chart_templates():
    return copy.deepcopy(TEMPLATES)

try:   # a viewer may read charts through POST (the endpoint only reads; RLS keeps private traces private)
    import admin as _admin; _admin.allow_viewer_post("/api/obs/charts/query")
except ImportError:
    pass
