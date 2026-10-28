"""GĐ12.10 Alerts (the alert block moved out of obs.py in the first commit of this card; same routes).
A rule is a tiny state machine: ok -> firing (alert.fired) -> still above after renotify_min (alert.renotify) -> back below: ok (alert.resolved). A NULL value (no data) changes
nothing. The value comes from the SAME engine as dashboards (charts.run over a spec built from the rule, inside the rule's workspace). Events go through destinations.emit
(Slack / e-mail / webhook ...); a rule that addressed no destination still uses its old channel (_dispatch) so rules made before 12.1 keep working. One evaluation per worker tick."""
import datetime as dt, json, logging, os, uuid
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb
import notify
import deepagent_api as A   # q() (import cycle: only attributes used at run time; import deepagent_api before this module)
import obs

log = logging.getLogger("alerts")
router = APIRouter()

class AlertIn(BaseModel):
    project_id: int
    name: str = Field(min_length=1, max_length=120)
    metric: str = Field(pattern=r"^(error_rate|latency_p95|latency_p50|count|cost|tokens|feedback_avg)$")
    feedback_key: str | None = None
    op: str = Field(pattern=r"^(gt|gte|lt|lte)$")
    threshold: float
    window_min: int = Field(default=15, ge=1, le=1440)
    filter: str | None = None
    environment: str | None = None   # GĐ9.10: the rule only looks at runs of this environment
    channel: str = Field(pattern=r"^(webhook|email_draft|log|telegram|email|slack)$")
    target: str | None = None   # telegram: chat_id | email: địa chỉ nhận | webhook: URL
    config: dict | None = None  # GĐ6.4: secret, signing_secret, host, port, user, password ("{{secret:NAME}}"), starttls, from
    description: str | None = Field(None, max_length=500)
    aggregation: str | None = Field(None, pattern=r"^(avg|sum|min|max|p50|p90|p95|p99)$")   # None = the metric's usual one
    severity: int = Field(default=1, ge=0, le=3)   # 0 info, 1 warn, 2-3 critical (destinations filter min_severity)
    renotify_min: int = Field(default=60, ge=1, le=10080)

class AlertEdit(BaseModel):
    """PUT: only the fields sent change (channel / target / config keep the rule's old delivery settings)."""
    name: str | None = Field(None, min_length=1, max_length=120)
    metric: str | None = Field(None, pattern=r"^(error_rate|latency_p95|latency_p50|count|cost|tokens|feedback_avg)$")
    feedback_key: str | None = None
    op: str | None = Field(None, pattern=r"^(gt|gte|lt|lte)$")
    threshold: float | None = None
    window_min: int | None = Field(None, ge=1, le=1440)
    filter: str | None = None
    environment: str | None = None
    description: str | None = Field(None, max_length=500)
    aggregation: str | None = Field(None, pattern=r"^(avg|sum|min|max|p50|p90|p95|p99)$")
    severity: int | None = Field(None, ge=0, le=3)
    renotify_min: int | None = Field(None, ge=1, le=10080)

class AlertEnabled(BaseModel):
    enabled: bool

class PreviewIn(BaseModel):
    rule: AlertEdit
    project_id: int
    hours: float = Field(default=168, gt=0, le=168)

_CFG_KEYS = {"secret", "signing_secret", "host", "port", "user", "password", "starttls", "from"}

@router.post("/api/obs/alerts")
async def alert_new(a: AlertIn):
    obs.compile_filter(a.filter or "")
    _spec(a.model_dump())   # 422 for a rule the chart engine cannot run (e.g. feedback_avg without feedback_key)
    cfg = {k: v for k, v in (a.config or {}).items() if k in _CFG_KEYS and v not in (None, "")}
    if cfg.get("password") is not None and not str(cfg["password"]).startswith("{{secret:"): raise HTTPException(422, "password phải là tham chiếu {{secret:TÊN}}")
    if a.channel in notify.KINDS:
        try: notify.validate_channel(a.channel, a.target, cfg)
        except ValueError as e: raise HTTPException(422, str(e))
    import envs
    return await A.q("INSERT INTO alert_rules(project_id,name,metric,feedback_key,op,threshold,window_min,filter,channel,target,channel_cfg,environment,description,aggregation,severity,renotify_min) "
                     "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                     (a.project_id, a.name, a.metric, a.feedback_key, a.op, a.threshold, a.window_min, a.filter, a.channel, a.target, Jsonb(cfg) if cfg else None, envs.norm_http(a.environment),
                      a.description, a.aggregation, a.severity, a.renotify_min), one=True)

@router.get("/api/obs/alerts")
async def alert_list():
    return await A.q("SELECT a.*, p.name project, (SELECT json_agg(e ORDER BY e.id DESC) FROM (SELECT * FROM alert_events WHERE rule_id=a.id ORDER BY id DESC LIMIT 5) e) events FROM alert_rules a JOIN projects p ON p.id=a.project_id ORDER BY a.id")

@router.get("/api/obs/alerts/{aid}")
async def alert_get(aid: int):
    return await _rule(aid)

async def _rule(aid):
    r = await A.q("SELECT a.*, p.name project, (SELECT json_agg(e ORDER BY e.id DESC) FROM (SELECT * FROM alert_events WHERE rule_id=a.id ORDER BY id DESC LIMIT 5) e) events FROM alert_rules a JOIN projects p ON p.id=a.project_id WHERE a.id=%s", (aid,), one=True)
    if not r: raise HTTPException(404, "Không có cảnh báo này")
    return r

@router.put("/api/obs/alerts/{aid}")
async def alert_put(aid: int, b: AlertEdit):
    old = await _rule(aid)
    ch = b.model_dump(exclude_unset=True)
    if "filter" in ch: obs.compile_filter(ch["filter"] or "")
    if "environment" in ch:
        import envs; ch["environment"] = envs.norm_http(ch["environment"])
    for k in ("name", "metric", "op", "threshold", "window_min", "severity", "renotify_min"):
        if k in ch and ch[k] is None: raise HTTPException(422, f"{k} không được để trống")
    _spec({**old, **ch})
    if ch:
        sets = ", ".join(f"{k}=%s" for k in ch)
        await A.q(f"UPDATE alert_rules SET {sets} WHERE id=%s", (*ch.values(), aid))
    return await _rule(aid)

@router.patch("/api/obs/alerts/{aid}")
async def alert_toggle(aid: int, b: AlertEnabled):
    """On/off switch. Switching off while firing quietly returns the rule to 'ok' (no event); switching on starts from 'ok' again."""
    await _rule(aid)
    await A.q("UPDATE alert_rules SET enabled=%s, state='ok', state_since=now(), dedup_key=NULL WHERE id=%s", (b.enabled, aid))
    return await _rule(aid)

@router.delete("/api/obs/alerts/{aid}")
async def alert_del(aid: int):
    await A.q("DELETE FROM alert_rules WHERE id=%s", (aid,)); return {"ok": True}

@router.post("/api/obs/alerts/preview")
async def alert_preview(b: PreviewIn):
    """What the rule WOULD have done over the last `hours`: [{t, value, fires}] per chart bucket (a bucket is the unit of evaluation here; the live check slides a window each minute)."""
    import admin, charts
    if admin.is_demo(): raise HTTPException(403, "Tài khoản demo chỉ được xem")
    given = b.rule.model_dump(exclude_none=True)
    for k in ("metric", "op", "threshold"):
        if k not in given: raise HTTPException(422, f"Thiếu {k}")
    rule = {**_PREVIEW_DEFAULTS, **given, "project_id": b.project_id}
    if rule.get("filter"): obs.compile_filter(rule["filter"])
    bucket = "5m" if rule["window_min"] < 60 and b.hours <= 40 else "hour"
    end = _minute(dt.datetime.now(dt.timezone.utc)) + dt.timedelta(minutes=1)
    out = await charts.run(_spec(rule, bucket), end - dt.timedelta(hours=b.hours), end)
    s = out["series"][0]["values"] if out["series"] else []
    pts = []
    for t, v in zip(out["buckets"], s):
        if v is None and rule["metric"] in _ZERO_IS_VALUE: v = 0.0
        pts.append({"t": t, "value": v, "fires": v is not None and _cmp(v, rule["op"], rule["threshold"])})
    return pts

@router.post("/api/obs/alerts/{aid}/test")
async def alert_test(aid: int, send: bool = False):
    """Value now + would it fire. send=true: also sends an `alert.test` event to the destinations subscribed to alerts (per-destination result in `deliveries`);
    a rule that reaches no destination uses its old channel (`delivery` text, as before)."""
    r = await A.q("SELECT * FROM alert_rules WHERE id=%s", (aid,), one=True)
    if not r: raise HTTPException(404, "Không có cảnh báo này")
    v = await metric_value(r); out = {"value": v, "fires": v is not None and _cmp(v, r["op"], r["threshold"])}
    if send:   # "Gửi thử"
        ws = await obs._project_ws(r["project_id"]) or 1
        res = []
        try: n = await _emit("alert.test", await _data(r, v, dt.datetime.now(dt.timezone.utc), "test", "test"), ws, r["project_id"], collect=res)
        except Exception as e:  # noqa: BLE001
            log.exception("alert test emit failed"); n = 0; res = []
        out["deliveries"] = res
        out["delivery"] = (f"{n} đích: " + ", ".join(f"{x['destination']} {'OK' if x['ok'] else x['reason']}" for x in res)) if n else \
            await _dispatch(r, f"[Moza Agent OS] Gửi thử: '{r['name']}'", "Tin thử từ trang Cảnh báo — kênh hoạt động bình thường.")
        await A.q("INSERT INTO alert_events(rule_id,value,delivery,detail,kind) VALUES(%s,%s,%s,%s,'test')", (aid, v, out["delivery"], Jsonb({"deliveries": res})))
    return out

# ---------------------------------------------------------------- metric (same engine as dashboards)
OPS = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
SEVERITY_NAME = ("info", "warn", "critical", "critical")
UNIT = {"error_rate": "%", "latency_p95": "ms", "latency_p50": "ms", "cost": "usd"}
_ZERO_IS_VALUE = ("count", "cost", "tokens")   # no runs in the window is the value 0 (not "no data"), as before
_PREVIEW_DEFAULTS = {"window_min": 15, "filter": None, "environment": None, "feedback_key": None, "aggregation": None}

def _cmp(v, op, th): return {"gt": v > th, "gte": v >= th, "lt": v < th, "lte": v <= th}[op]

def _rule_filter(r):
    """The rule's filter AND its environment (GĐ9.10)."""
    f, e = (r.get("filter") or "").strip(), r.get("environment")
    if not e: return f
    ec = f"eq(environment,{json.dumps(e)})"
    return f"and({f},{ec})" if f else ec

def _minute(t): return t.replace(second=0, microsecond=0)

def _spec(r, bucket="auto"):
    """The chart spec of a rule (metric error_rate / latency_p95 / latency_p50 / count / cost / tokens / feedback_avg). Invalid -> 422."""
    import charts
    f, m, ag = _rule_filter(r), r["metric"], r.get("aggregation")
    err = f'and({f},eq(status,"error"))' if f else 'eq(status,"error")'
    if m == "error_rate": s = {"metric": "ratio", "ratio": {"num": {"metric": "count", "filter": err}, "den": {"metric": "count", "filter": f}}}
    elif m in ("latency_p95", "latency_p50"): s = {"metric": "latency", "agg": ag or m[-3:], "filter": f}
    elif m == "count": s = {"metric": "count", "filter": f}
    elif m in ("cost", "tokens"): s = {"metric": m, "agg": ag or "sum", "filter": f}
    elif m == "feedback_avg": s = {"metric": "feedback", "agg": ag or "avg", "feedback_key": r.get("feedback_key"), "filter": f}
    else: raise HTTPException(422, "metric không hỗ trợ")
    s["scope"] = "run" if m == "feedback_avg" else "trace"   # trace = root runs only (what the old SQL counted)
    try: return charts.ChartSpec.model_validate({"source": {"projects": [r["project_id"]]}, "series": [s], "viz": "kpi" if bucket == "auto" else "line", "bucket": bucket})
    except ValueError as e: raise HTTPException(422, "Luật cảnh báo không hợp lệ: " + str(e)[:200]) from None

async def metric_value(r, now=None):
    """Value of the rule over the last window_min minutes (None = no data). Runs inside the rule's workspace (RLS + blind search index), like a dashboard would."""
    import admin, charts
    end = _minute(now or dt.datetime.now(dt.timezone.utc)) + dt.timedelta(minutes=1)
    ws = await obs._project_ws(r["project_id"]) or 1
    async with admin.as_workspace(ws):
        out = await charts.run(_spec(r), end - dt.timedelta(minutes=r["window_min"]), end)
    v = out["totals"].get("s0")
    if v is None and r["metric"] in _ZERO_IS_VALUE: v = 0.0
    return None if v is None else float(v)

# ---------------------------------------------------------------- delivery
async def _dispatch(r, subj, body):
    if r["channel"] == "log": return "chỉ ghi log"
    if r["channel"] in notify.KINDS:
        p = await A.q("SELECT workspace_id FROM projects WHERE id=%s", (r["project_id"],), one=True)
        return await notify.send((p or {}).get("workspace_id") or 1, notify.channel_of(r["channel"], r["target"], r["channel_cfg"]), subj, body)
    return await obs.deliver("email" if r["channel"] == "email_draft" else r["channel"], r["target"], subj, body, ws_id=await obs._project_ws(r["project_id"]))

async def _emit(event_type, data, ws, project_id, collect=None):
    import destinations
    return await destinations.emit(event_type, data, ws, project_id=project_id, collect=collect)

def _payload(r, v, now, state, kind):
    """LangSmith's alert webhook fields + what our Slack / e-mail renderers use (rule, metric, value, threshold, op, unit, agg, window_min, link, title, text)."""
    base = os.environ.get("AOS_PUBLIC_URL", "").rstrip("/")
    sev = SEVERITY_NAME[int(r.get("severity") or 0)]
    pct = r["metric"] == "error_rate"
    show = lambda x: None if x is None else (round(x * 100, 4) if pct else x)   # noqa: E731  (error_rate is a ratio; people read percent)
    try: agg = "ratio" if pct else _spec(r).series[0].eff_agg()
    except HTTPException: agg = r.get("aggregation")
    link = f"{base}/rules?alert={r['id']}" if base else None
    word = {"fired": "đang vượt ngưỡng", "renotify": "vẫn vượt ngưỡng", "resolved": "đã hết", "test": "tin thử"}[kind]
    unit = UNIT.get(r["metric"], "")
    value_txt = f"{r['metric']} = {show(v):.4g}{unit if unit in ('%', 'ms') else ''}" if v is not None else f"{r['metric']} (chưa có dữ liệu)"
    return {"project_name": r.get("project") or "", "workspace_name": r.get("workspace") or "", "alert_rule_id": r["id"], "alert_rule_name": r["name"],
            "alert_rule_description": r.get("description") or "", "alert_rule_type": "threshold", "alert_rule_attribute": r["metric"], "alert_rule_url": link or "",
            "runs_url": f"{base}/projects?p={r['project_id']}" if base else "", "triggered_metric_value": v, "triggered_threshold": r["threshold"],
            "timestamp": now.isoformat(timespec="seconds"), "alert_state": state, "severity": sev, "severity_level": int(r.get("severity") or 0), "dedup_key": r.get("dedup_key"),
            "rule": r["name"], "project_id": r["project_id"], "workspace": r.get("workspace") or "", "metric": r["metric"], "value": show(v), "threshold": show(r["threshold"]),
            "op": OPS[r["op"]], "unit": unit, "agg": agg, "window_min": r["window_min"], "link": link,
            "title": f"[Moza Agent OS] Cảnh báo '{r['name']}': {word}",
            "text": f"{value_txt} ({OPS[r['op']]} {show(r['threshold'])}), cửa sổ {r['window_min']} phút." + (f" Môi trường: {r['environment']}." if r.get("environment") else "")}

async def _data(r, v, now, state, kind):
    p = await A.q("SELECT p.name project, w.name workspace FROM projects p LEFT JOIN workspaces w ON w.id=p.workspace_id WHERE p.id=%s", (r["project_id"],), one=True) or {}
    return _payload({**r, **p}, v, now, state, kind)

async def _deliver(r, kind, v, now, state):
    """emit alert.<kind> to the workspace's destinations; none addressed -> the rule's old channel (not for 'resolved'). -> (delivery text, payload)."""
    ws = await obs._project_ws(r["project_id"]) or 1
    data = await _data(r, v, now, state, kind)
    try: n = await _emit("alert." + kind, data, ws, r["project_id"])
    except Exception as e:  # noqa: BLE001
        log.exception("alert emit failed"); return "lỗi: " + type(e).__name__, data
    if n: return f"{n} đích", data
    if kind == "resolved": return "không có đích nào đăng ký", data
    return await _dispatch(r, data["title"], data["text"]), data

# ---------------------------------------------------------------- state machine
async def evaluate(r, now=None, value=None, use_value=False):
    """One evaluation of one rule at `now`. -> the event kind sent ('fired' | 'renotify' | 'resolved') or None. use_value=True: take `value` instead of querying (tests)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    try: v = value if use_value else await metric_value(r, now)
    except Exception as e:  # noqa: BLE001
        log.exception("alert %s evaluation failed", r["id"])
        await A.q("UPDATE alert_rules SET last_error=%s WHERE id=%s", (f"{type(e).__name__}: {str(e)[:160]}", r["id"])); return None
    await A.q("UPDATE alert_rules SET last_value=%s, last_error=NULL WHERE id=%s", (v, r["id"]))
    if v is None: return None   # no data: nothing changes
    breach = _cmp(v, r["op"], r["threshold"])
    if r["state"] == "ok" and breach:
        row = await A.q("UPDATE alert_rules SET state='firing', state_since=%s, last_notified_at=%s, last_fired=%s, dedup_key=%s WHERE id=%s AND state='ok' AND enabled RETURNING *",
                        (now, now, now, uuid.uuid4().hex, r["id"]), one=True)
        kind = "fired"
    elif r["state"] == "firing" and breach:
        if r["last_notified_at"] and (now - r["last_notified_at"]).total_seconds() < r["renotify_min"] * 60: return None
        row = await A.q("UPDATE alert_rules SET last_notified_at=%s WHERE id=%s AND state='firing' AND last_notified_at IS NOT DISTINCT FROM %s RETURNING *", (now, r["id"], r["last_notified_at"]), one=True)
        kind = "renotify"
    elif r["state"] == "firing":
        row = await A.q("UPDATE alert_rules SET state='ok', state_since=%s WHERE id=%s AND state='firing' RETURNING *", (now, r["id"]), one=True)
        kind = "resolved"
    else: return None
    if not row: return None   # another process moved it first
    d, data = await _deliver({**r, "dedup_key": row["dedup_key"] if kind != "resolved" else r["dedup_key"]}, kind, v, now, "ok" if kind == "resolved" else "firing")
    if kind == "resolved": await A.q("UPDATE alert_rules SET dedup_key=NULL WHERE id=%s", (r["id"],))
    await A.q("INSERT INTO alert_events(rule_id,value,delivery,detail,kind) VALUES(%s,%s,%s,%s,%s)", (r["id"], v, d, Jsonb({"subject": data["title"], "severity": data["severity"]}), kind))
    return kind

async def check_alerts():
    for r in await A.q("SELECT a.* FROM alert_rules a JOIN projects p ON p.id=a.project_id LEFT JOIN workspaces w ON w.id=p.workspace_id WHERE a.enabled AND NOT coalesce(w.demo, false) ORDER BY a.id"):
        try: await evaluate(r)
        except Exception: log.exception("alert %s failed", r["id"])  # noqa: BLE001
