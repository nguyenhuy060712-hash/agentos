"""GĐ12.8 Prebuilt LangSmith-style dashboard (reduced): six sections of chart specs kept in code (nothing stored in the DB), evaluated through charts.run.
GET /api/obs/dashboards/prebuilt?project_id=&environment=&agent=&hours=&group_by=   -> {range, cards:[{id, section, title, viz, result, view_runs, error}]}
  group_by = `tag` | `tag:<prefix>` | `metadata:<path>` splits every card except Tools / Run types (a card with several series keeps its first one; an error-rate ratio turns into an error count).
GET /api/obs/projects/{pid}/threads/stats?from&to  -> thread count, trace count, P50 / P99 latency, error rate, cost.
Not here (GĐ15): ECharts / drag-and-drop UI, custom dashboards CRUD (12.6 / 12.7), default-dashboard picker."""
import asyncio, copy, datetime as dt, json, re
from fastapi import APIRouter, HTTPException, Query
from pydantic import ValidationError

router = APIRouter()

class A:   # lazy handle: deepagent_api imports this module while it is still being imported
    @staticmethod
    def mod():
        import deepagent_api; return deepagent_api

LLM = 'eq(run_type,"llm")'
ERR = 'eq(status,"error")'
def ratio(num_filter, flt=""): return {"metric": "ratio", "scope": "trace" if not flt else "run", "ratio": {"num": {"metric": "count", "filter": f'and({flt},{num_filter})' if flt else num_filter}, "den": {"metric": "count", "filter": flt}}}

def _t(vi, en): return {"vi": vi, "en": en}
HINT_COST = _t("Chi phí = tổng chi phí các lần gọi LLM trong mỗi trace (mỗi lần tính một lần), nên Tổng = cộng các model. Bảng chi tiêu của LLM Gateway (/api/gw/usage) chỉ đếm các cuộc gọi đi qua Gateway nên có thể nhỏ hơn.", "Cost = sum of the LLM-call costs in each trace (each call counted once), so Total = the sum over models. The LLM Gateway spend table (/api/gw/usage) only counts calls routed through the Gateway, so it can be smaller.")

# split: "group" = group_by replaces the card's own grouping, several series keep `split_series` (default: the first); "err" = the error-rate ratio becomes an error count; "none" = never split
PREBUILT = [
    {"id": "trace_count", "section": "traces", "title": _t("Số lượt chạy (trace)", "Trace count"), "split": "group",
     "spec": {"series": [{"metric": "count", "scope": "trace", "label": "traces"}], "viz": "stacked_bar"}},
    {"id": "trace_latency", "section": "traces", "title": _t("Độ trễ P50 / P95 / P99", "Latency P50 / P95 / P99"), "split": "group",
     "spec": {"series": [{"metric": "latency", "agg": a, "scope": "trace", "label": a.upper()} for a in ("p50", "p95", "p99")], "viz": "line"}},
    {"id": "trace_error_rate", "section": "traces", "title": _t("Tỉ lệ lỗi", "Error rate"), "split": "err",
     "spec": {"series": [ratio(ERR) | {"label": "error rate"}], "viz": "line"},
     "split_series": [{"metric": "count", "scope": "trace", "filter": ERR, "label": "errors"}], "view_filter": ERR},
    {"id": "llm_calls", "section": "llm", "title": _t("Số lần gọi LLM", "LLM calls"), "split": "group",
     "spec": {"series": [{"metric": "count", "filter": LLM, "label": "calls"}], "viz": "stacked_bar"}, "view_filter": LLM},
    {"id": "llm_error_rate", "section": "llm", "title": _t("Tỉ lệ lỗi LLM", "LLM error rate"), "split": "err",
     "spec": {"series": [ratio(ERR, LLM) | {"label": "error rate"}], "viz": "line"},
     "split_series": [{"metric": "count", "filter": f"and({LLM},{ERR})", "label": "errors"}], "view_filter": f"and({LLM},{ERR})"},
    {"id": "llm_latency_by_model", "section": "llm", "title": _t("Độ trễ theo model (P50)", "Latency by model (P50)"), "split": "group",
     "spec": {"series": [{"metric": "latency", "agg": "p50", "filter": LLM}], "group_by": {"attr": "model", "top": 10}, "viz": "line"}, "view_filter": LLM},
    {"id": "cost_total", "section": "cost", "hint": HINT_COST, "title": _t("Tổng chi phí", "Total cost"), "split": "group",
     "spec": {"series": [{"metric": "cost", "agg": "sum", "scope": "trace", "label": "cost"}], "viz": "stacked_bar"}},
    {"id": "tokens", "section": "cost", "hint": HINT_COST, "title": _t("Token (vào / ra)", "Tokens (input / output)"), "split": "group",
     "spec": {"series": [{"metric": "tokens", "field": "input", "agg": "sum", "scope": "trace", "label": "input"}, {"metric": "tokens", "field": "output", "agg": "sum", "scope": "trace", "label": "output"}], "viz": "stacked_bar"},
     "split_series": [{"metric": "tokens", "field": "total", "agg": "sum", "scope": "trace", "label": "tokens"}]},
    {"id": "cost_by_model", "section": "cost", "hint": HINT_COST, "title": _t("Chi phí theo model", "Cost by model"), "split": "group",
     "spec": {"series": [{"metric": "cost", "agg": "sum", "filter": LLM}], "group_by": {"attr": "model", "top": 10, "rank_by": "cost"}, "viz": "stacked_bar"}, "view_filter": LLM},
    {"id": "tools_top5", "section": "tools", "title": _t("5 công cụ nhiều lỗi nhất", "Top 5 tools (most errors first)"), "split": "none",
     "spec": {"series": [{"metric": "count", "filter": 'eq(run_type,"tool")', "label": "calls"}], "group_by": {"attr": "run_name", "top": 5, "rank_by": "errors"}, "viz": "ranked_bar"}, "view_filter": 'eq(run_type,"tool")'},
    {"id": "run_types_top5", "section": "run_types", "title": _t("5 loại bước hay chạy nhất", "Top 5 run types"), "split": "none",
     "spec": {"series": [{"metric": "count", "label": "runs"}], "group_by": {"attr": "run_type", "top": 5}, "viz": "ranked_bar"}},
    {"id": "feedback_scores", "section": "feedback", "title": _t("Điểm phản hồi theo thời gian", "Feedback score over time"), "split": "none", "feedback": True,
     "spec": {"series": [], "viz": "line"}},
]
SECTIONS = [("traces", _t("Trace", "Traces")), ("llm", _t("Lần gọi LLM", "LLM calls")), ("cost", _t("Chi phí & token", "Cost & tokens")),
            ("tools", _t("Công cụ", "Tools")), ("run_types", _t("Loại bước", "Run types")), ("feedback", _t("Phản hồi", "Feedback"))]

AGENT_RX = re.compile(r"^[\w\-.: ]{1,80}$")
SPLIT_RX = re.compile(r"^(tag(:[\w\-.:]{1,60})?|metadata:[A-Za-z0-9_\-.]{1,80})$")

def _and(a, b): return b if not a else a if not b else f"and({a},{b})"

def _apply_agent(series, agent):
    f = f'eq(metadata.agent_id,{json.dumps(agent)})'
    for s in series:
        for p in ([s["ratio"]["num"], s["ratio"]["den"]] if s.get("metric") == "ratio" else [s]): p["filter"] = _and(p.get("filter", ""), f)

def parse_split(group_by):
    """'tag' | 'tag:<prefix>' | 'metadata:<path>' -> (group_by dict, label prefix or None)."""
    if not group_by: return None, None
    if not SPLIT_RX.fullmatch(group_by): raise HTTPException(422, "group_by phải là tag, tag:<tiền tố> hoặc metadata:<đường dẫn> / group_by must be tag, tag:<prefix> or metadata:<path>")
    kind, _, rest = group_by.partition(":")
    return ({"attr": "tag", "top": 10} if kind == "tag" else {"attr": "metadata", "path": rest, "top": 10}), (rest or None) if kind == "tag" else None

def build_specs(project_id=None, environment=None, agent=None, group_by=None):
    """-> [(card definition, ChartSpec dict | None)] with the selectors and the split applied. A feedback card has no spec here (its series depend on the keys in the range)."""
    import charts
    if agent is not None and not AGENT_RX.fullmatch(agent): raise HTTPException(422, "agent không hợp lệ / invalid agent")
    split, _ = parse_split(group_by); out = []
    for card in PREBUILT:
        d = copy.deepcopy(card["spec"])
        if card.get("feedback"): out.append((card, None)); continue
        if split and card["split"] != "none":
            if card["split"] == "err": d["series"] = copy.deepcopy(card["split_series"])
            elif len(d["series"]) > 1: d["series"] = copy.deepcopy(card.get("split_series") or d["series"][:1])
            d["group_by"] = {**split, "rank_by": (d.get("group_by") or {}).get("rank_by", "count")}
        if agent: _apply_agent(d["series"], agent)
        d["source"] = {"projects": [int(project_id)] if project_id else [], "environments": [environment] if environment else []}
        try: charts.ChartSpec.model_validate(d)
        except ValidationError as e: raise HTTPException(500, f"card {card['id']}: {e.errors()[0]['msg']}") from None
        out.append((card, d))
    return out

async def _feedback_spec(project_id, environment, agent, start, end):
    """One series per feedback key seen in the range (top 5 by count)."""
    D = A.mod(); args = [start, end]; w = ""
    if project_id: w += " AND r.project_id = %s"; args.append(int(project_id))
    rows = await D.q(f"SELECT f.key, count(*) n FROM feedback f JOIN tr_runs r ON r.id = f.run_id WHERE r.start_time >= %s AND r.start_time < %s AND f.score IS NOT NULL{w} GROUP BY f.key ORDER BY n DESC, f.key LIMIT 5", args)
    if not rows: return None
    d = {"series": [{"metric": "feedback", "feedback_key": r["key"], "agg": "avg", "label": r["key"]} for r in rows], "viz": "line",
         "source": {"projects": [int(project_id)] if project_id else [], "environments": [environment] if environment else []}}
    if agent: _apply_agent(d["series"], agent)
    return d

def _view_runs(card, d, project_id, hours, agent):
    flt = _and(card.get("view_filter", ""), f'eq(metadata.agent_id,{json.dumps(agent)})' if agent else "")
    return {"p": project_id, "filter": flt, "since": hours} if project_id else None

async def _one(card, d, start, end, tz, label_prefix, project_id, hours, agent):
    import charts
    out = {"id": card["id"], "section": card["section"], "title": card["title"], "hint": card.get("hint"), "viz": (d or card["spec"])["viz"], "view_runs": _view_runs(card, d, project_id, hours, agent), "result": None, "error": None}
    if d is None: out["empty"] = True; return out
    try:
        res = await charts.run(charts.ChartSpec.model_validate(d), start, end, tz)
        if label_prefix:   # `tag:<prefix>`: only the tags that start with the prefix
            keep = [s for s in res["series"] if s["label"].startswith(label_prefix) or s["key"] == "__other__"]
            res["series"] = keep; res["totals"] = {k: v for k, v in res["totals"].items() if k in {s["key"] for s in keep}}
        out["result"] = res
    except HTTPException as e: out["error"] = e.detail
    return out

@router.get("/api/obs/dashboards/prebuilt")
async def prebuilt(project_id: int | None = None, environment: str | None = Query(None, max_length=31), agent: str | None = Query(None, max_length=80),
                   hours: float = Query(24, gt=0, le=24 * 400), group_by: str | None = Query(None, max_length=100), tz: str = Query("Asia/Ho_Chi_Minh", max_length=64)):
    import charts, envs
    if environment: environment = envs.norm_http(environment)
    if project_id and not await A.mod().q("SELECT 1 FROM projects WHERE id=%s", (project_id,), one=True): raise HTTPException(404, "Không có project này")
    start, end = charts.resolve_range(None, None, hours)
    split, prefix = parse_split(group_by)
    specs = build_specs(project_id, environment, agent, group_by)
    jobs = []
    for card, d in specs:
        if card.get("feedback"): d = await _feedback_spec(project_id, environment, agent, start, end)
        jobs.append(_one(card, d, start, end, tz, prefix, project_id, hours, agent))
    cards = await asyncio.gather(*jobs)
    return {"range": {"from": start.isoformat(), "to": end.isoformat(), "hours": hours, "tz": tz}, "project_id": project_id, "environment": environment, "agent": agent, "group_by": group_by,
            "sections": [{"id": i, "title": t} for i, t in SECTIONS], "cards": cards}

@router.get("/api/obs/projects/{pid}/threads/stats")
async def threads_stats(pid: int, from_: dt.datetime | None = Query(None, alias="from"), to: dt.datetime | None = None):
    """Chat threads of a project in a time range (root runs that carry a thread_id)."""
    import charts
    D = A.mod()
    if not await D.q("SELECT 1 FROM projects WHERE id=%s", (pid,), one=True): raise HTTPException(404, "Không có project này")
    end = charts._utc(to) if to else dt.datetime.now(dt.timezone.utc); start = charts._utc(from_) if from_ else end - dt.timedelta(hours=24)
    if end <= start: raise HTTPException(422, "Khoảng thời gian không hợp lệ / empty range")
    r = await D.q("""SELECT count(DISTINCT thread_id) AS threads, count(*) AS traces, percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50_ms,
        percentile_cont(0.99) WITHIN GROUP (ORDER BY latency_ms) AS p99_ms, count(*) FILTER (WHERE status = 'error') AS errors, coalesce(sum(cost_usd), 0) AS cost
        FROM tr_runs WHERE project_id = %s AND parent_id IS NULL AND thread_id IS NOT NULL AND start_time >= %s AND start_time < %s""", (pid, start, end), one=True)
    n = r["traces"]
    return {"project_id": pid, "from": start.isoformat(), "to": end.isoformat(), "threads": r["threads"], "traces": n, "p50_ms": r["p50_ms"], "p99_ms": r["p99_ms"],
            "error_rate": (r["errors"] / n) if n else None, "cost": float(r["cost"])}
