"""GĐ12.12 automation rules (the rule block moved out of obs.py in the first commit; this file now holds the v2 features).
A rule = project + filter + sampling + actions, applied by the background worker (obs.worker -> apply_rules, every tick).
  item_type 'runs'    : root runs that ended after the rule's cursor (+ runs that got feedback after fb_cursor_ts when the filter uses `feedback.`).
  item_type 'threads' : conversations that went idle (projects.thread_idle_min, F20): see idle_threads / register_thread_idle.
Fixed action order per item: queue -> dataset -> tag -> online evaluators (composite last) -> code evaluators -> composite evaluators -> extend_retention.
A webhook action is a BATCH sent after the evaluators ran on the whole batch (so judge scores are in the payload), through destinations.emit('rule.batch').
Once per (rule, run) or (rule, thread activity): automation_applied (rows older than 60 days are pruned; the late-feedback window is 30 days, the idle window 7).
Workspace safety: every rule runs inside admin.as_workspace(<project's workspace>), so a dataset / queue / evaluator id from another workspace is invisible (RLS) even if it
was written straight into the table; the API also refuses such ids when the rule is saved."""
import asyncio, datetime as dt, hashlib, json, logging, os, re, time, uuid
from typing import Annotated, Literal
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb
import deepagent_api as A   # q() (import cycle: only attributes used at run time; import deepagent_api before this module)
import obs
import destinations

log = logging.getLogger("rules")
router = APIRouter()

destinations.register_event_type("rule.batch", {"rule_id": "id", "rule": "rule name", "project_id": "project id", "environment": "environment or null", "start_time": "first run end time",
                                                "end_time": "last run end time", "runs": "[{id, name, inputs, outputs, feedback_stats, trace_url}] (<= 100 per POST, < 1 MB)"}, "12.12")

BATCH_RUNS = 200            # runs taken per rule per tick (as before)
HOOK_RUNS, HOOK_BYTES = 100, 1_000_000   # one webhook POST: at most this many runs / bytes
FIELD_CUT = 4000            # a long input / output is cut to this many characters in a webhook
LATE_DAYS = 30              # a run older than this is not re-checked when a score arrives late
IDLE_DAYS, IDLE_LIMIT = 7, 500   # idle trigger: look at runs of the last 7 days, at most 500 threads per tick and project
APPLIED_KEEP_DAYS = 60      # > LATE_DAYS + the longest claim lag, so a claim never disappears while its run can still come back
RUN_TYPES = ("add_to_dataset", "add_to_queue", "run_evaluator", "add_tag", "webhook", "evaluate_thread", "extend_retention")
THREAD_TYPES = ("add_to_dataset", "add_to_queue", "evaluate_thread")
_KEYS = {"add_to_dataset": ("dataset_id", "split"), "add_to_queue": ("queue_id",), "run_evaluator": ("evaluator_id",), "evaluate_thread": ("evaluator_id",),
         "add_tag": ("tag",), "webhook": ("url",), "extend_retention": ()}
_JUDGE = ("run_evaluator", "evaluate_thread")   # actions that spend model money

def _now(): return dt.datetime.now(dt.timezone.utc)   # monkeypatched by tests (fake clock)
def rules_max(): return int(os.environ.get("AOS_RULES_MAX", "200"))
def _short(e): return (str(getattr(e, "detail", None) or e) or type(e).__name__)[:250]

class RuleIn(BaseModel):
    project_id: int
    name: str = Field(min_length=1, max_length=120)
    filter: str | None = None
    sampling_rate: float = Field(default=1, ge=0, le=1)
    actions: list[dict]   # {"type":"add_to_dataset","dataset_id":1} | add_to_queue | run_evaluator | add_tag | webhook | evaluate_thread | extend_retention
    backfill_hours: float = 0
    item_type: Literal["runs", "threads"] = "runs"
    environment: str | None = None   # only runs of this environment (envs.ENV_SQL); None = every environment

async def _need(sql, args, what):
    if not await A.q(sql, args, one=True): raise HTTPException(404, f"Không có {what} này")

async def _validate_actions(actions, item_type, ws):
    """Whitelist the keys, check the types, and refuse ids that are not in the project's workspace (404, same answer as 'does not exist')."""
    allowed = RUN_TYPES if item_type == "runs" else THREAD_TYPES
    out = []
    for a in actions:
        t = a.get("type") if isinstance(a, dict) else None
        if t not in RUN_TYPES: raise HTTPException(400, "action không hỗ trợ")
        if t not in allowed: raise HTTPException(422, f"Luật theo hội thoại chưa hỗ trợ action '{t}' (dùng: {', '.join(THREAD_TYPES)})")
        c = {"type": t, **{k: a[k] for k in _KEYS[t] if a.get(k) is not None}}
        for k in ("dataset_id", "queue_id", "evaluator_id"):
            if k in _KEYS[t] and (isinstance(c.get(k), bool) or not isinstance(c.get(k), int)): raise HTTPException(422, f"{t}: cần {k} là số nguyên")
        if t == "add_tag" and not (isinstance(c.get("tag"), str) and 1 <= len(c["tag"]) <= 80): raise HTTPException(422, "add_tag: cần tag (1-80 ký tự)")
        if t == "add_to_dataset" and c.get("split") is not None and not (isinstance(c["split"], str) and len(c["split"]) <= 60): raise HTTPException(422, "add_to_dataset: split không hợp lệ")
        if t == "webhook" and "url" in c and not (isinstance(c["url"], str) and re.match(r"^https?://\S{1,2000}$", c["url"])): raise HTTPException(422, "webhook: url phải bắt đầu bằng http:// hoặc https://")
        if t == "add_to_dataset": await _need("SELECT 1 FROM datasets WHERE id=%s AND workspace_id=%s", (c["dataset_id"], ws), "dataset")
        elif t == "add_to_queue": await _need("SELECT 1 FROM annotation_queues WHERE id=%s AND workspace_id=%s", (c["queue_id"], ws), "queue")
        elif t in _JUDGE: await _need("SELECT 1 FROM evaluators WHERE id=%s AND workspace_id=%s", (c["evaluator_id"], ws), "evaluator")
        out.append(c)
    return out

async def _check_limit(ws):
    n = (await A.q("SELECT count(*) n FROM automation_rules r JOIN projects p ON p.id=r.project_id WHERE r.enabled AND p.workspace_id=%s", (ws,), one=True))["n"]
    if n >= rules_max(): raise HTTPException(409, f"Workspace đã có {n} luật đang bật (tối đa {rules_max()}). Tắt hoặc xoá bớt một luật trước.")

def _no_demo(ws=None):
    import admin
    if admin.is_demo() or (ws is not None and int(ws) in A.gw.DEMO_WS): raise HTTPException(403, "Không dùng được cho workspace demo")

@router.post("/api/obs/rules")
async def rule_new(r: RuleIn):
    _no_demo()
    proj = await A.q("SELECT id, workspace_id FROM projects WHERE id=%s", (r.project_id,), one=True)   # RLS: another workspace's project is invisible
    if not proj: raise HTTPException(404, "Không có dự án này")
    _no_demo(proj["workspace_id"])
    obs.compile_filter(r.filter or "")
    import envs
    env = envs.norm_http(r.environment)
    if r.item_type == "threads" and r.backfill_hours: raise HTTPException(422, "Luật theo hội thoại không có backfill")
    acts = await _validate_actions(r.actions, r.item_type, proj["workspace_id"])
    await _check_limit(proj["workspace_id"])
    t = _now()
    return await A.q("INSERT INTO automation_rules(project_id,name,filter,sampling_rate,actions,cursor_ts,fb_cursor_ts,item_type,environment,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                     (r.project_id, r.name, r.filter, r.sampling_rate, Jsonb(acts), t - dt.timedelta(hours=r.backfill_hours), t, r.item_type, env, t), one=True)

@router.get("/api/obs/rules")
async def rule_list():
    return await A.q("SELECT r.*, p.name project, (SELECT json_agg(l ORDER BY l.id DESC) FROM (SELECT * FROM automation_log WHERE rule_id=r.id ORDER BY id DESC LIMIT 8) l) log FROM automation_rules r JOIN projects p ON p.id=r.project_id ORDER BY r.id")

async def _mine(rid):
    r = await A.q("SELECT r.id, r.enabled, p.workspace_id ws FROM automation_rules r JOIN projects p ON p.id=r.project_id WHERE r.id=%s", (rid,), one=True)   # RLS
    if not r: raise HTTPException(404, "Không có luật này")
    return r

@router.patch("/api/obs/rules/{rid}")
async def rule_toggle(rid: int, enabled: bool):
    r = await _mine(rid); t = _now()
    if enabled and not r["enabled"]:   # switching on again never processes the gap: both cursors jump to now
        await _check_limit(r["ws"])
        await A.q("UPDATE automation_rules SET enabled=true, cursor_ts=%s, fb_cursor_ts=%s, updated_at=%s WHERE id=%s", (t, t, t, rid))
    else:
        await A.q("UPDATE automation_rules SET enabled=%s, updated_at=%s WHERE id=%s", (enabled, t, rid))
    return {"ok": True}

@router.delete("/api/obs/rules/{rid}")
async def rule_del(rid: int):
    await _mine(rid)
    await A.q("DELETE FROM automation_rules WHERE id=%s", (rid,)); return {"ok": True}

def _ts_arg(v, name):
    if not v: return None
    try:
        d = dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError: raise HTTPException(422, f"{name}: cần thời gian ISO 8601") from None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)

@router.get("/api/obs/rules/{rid}/logs")
async def rule_logs(rid: int, from_: Annotated[str | None, Query(alias="from")] = None, to: str | None = None, status: str | None = None, cursor: int | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 100):
    """What a rule did, newest first. cursor = the `next_cursor` of the previous page. Another workspace's rule: 404."""
    await _mine(rid)
    import admin
    if admin.is_demo(): return {"items": [], "next_cursor": None}
    if status not in (None, "", "ok", "error", "skipped"): raise HTTPException(422, "status: ok | error | skipped")
    sql, args = "SELECT id, run_id, action, result, status, detail, batch_id, ts FROM automation_log WHERE rule_id=%s", [rid]
    if status: sql += " AND status=%s"; args.append(status)
    f, t = _ts_arg(from_, "from"), _ts_arg(to, "to")
    if f: sql += " AND ts >= %s"; args.append(f)
    if t: sql += " AND ts < %s"; args.append(t)
    if cursor: sql += " AND id < %s"; args.append(cursor)
    rows = await A.q(sql + " ORDER BY id DESC LIMIT %s", (*args, limit + 1))
    return {"items": rows[:limit], "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None}

class IdleIn(BaseModel):
    thread_idle_min: int = Field(ge=2, le=1440)   # minutes without a new turn before a conversation counts as finished (min 2, like LangSmith)

@router.get("/api/obs/projects/{pid}/thread_idle")
async def idle_get(pid: int):
    p = await A.q("SELECT thread_idle_min FROM projects WHERE id=%s", (pid,), one=True)
    if not p: raise HTTPException(404, "Không có dự án này")
    return p

@router.patch("/api/obs/projects/{pid}/thread_idle")
async def idle_set(pid: int, b: IdleIn):
    p = await A.q("SELECT workspace_id FROM projects WHERE id=%s", (pid,), one=True)
    if not p: raise HTTPException(404, "Không có dự án này")
    _no_demo(p["workspace_id"])
    await A.q("UPDATE projects SET thread_idle_min=%s WHERE id=%s", (b.thread_idle_min, pid)); return {"ok": True, "thread_idle_min": b.thread_idle_min}

# ---------------------------------------------------------------- the worker
def _sampled(run_id, rate):
    return rate >= 1 or int(hashlib.md5(str(run_id).encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < rate

def _filter_of(r):
    """The rule's filter AND its environment (None = every environment)."""
    f, e = (r.get("filter") or "").strip(), r.get("environment")
    if not e: return f
    ec = f"eq(environment,{json.dumps(e)})"
    return f"and({f},{ec})" if f else ec

async def _claim(rid, key, ts):
    """True the first time (rule, key) is seen: the whole 'apply once' guarantee."""
    return bool(await A.q("INSERT INTO automation_applied(rule_id,item_key,ts) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING RETURNING 1 AS x", (rid, key, ts), one=True))

async def _log(rid, run_id, action, result, status="ok", detail=None, batch=None):
    await A.q("INSERT INTO automation_log(rule_id,run_id,action,result,status,detail,batch_id) VALUES(%s,%s,%s,%s,%s,%s,%s)",
              (rid, run_id, action, str(result)[:300], status, str(detail)[:500] if detail else None, batch))

async def _guarded(r, fn, *a):
    """One broken rule (bad filter, deleted dataset...) never stops the others: the error is kept in last_error and in the log."""
    import admin
    try:
        async with admin.as_workspace(r["ws"]): await fn(r, *a)
    except Exception as e:  # noqa: BLE001
        log.warning("rule %s failed: %s", r.get("id"), _short(e))
        try:
            async with admin.as_workspace(r["ws"]):
                await A.q("UPDATE automation_rules SET last_error=%s WHERE id=%s", (_short(e), r["id"]))
                await _log(r["id"], None, "rule", "lỗi: " + _short(e), "error")
        except Exception: pass   # noqa: BLE001

async def apply_rules(now=None, project_ids=None):
    """Worker tick (obs.worker): run rules first, then the idle-thread trigger. `now` = fake clock, `project_ids` = limit to these projects (both for tests)."""
    import evals
    now = now or _now()
    rules = await A.q("SELECT r.*, p.workspace_id ws FROM automation_rules r JOIN projects p ON p.id=r.project_id WHERE r.enabled AND (%s::int[] IS NULL OR r.project_id = ANY(%s))", (project_ids, project_ids))
    for r in await evals.order_rules([x for x in rules if x["item_type"] == "runs"]):   # 9.Q: composite-only rules last
        await _guarded(r, _run_rule, now)
    pids = {r["project_id"] for r in rules if r["item_type"] == "threads"}
    if any(n != "rules" for n, _ in _LISTENERS):   # somebody else (e.g. the conversation judge) listens too: look at every project that had conversations lately
        pids |= {x["project_id"] for x in await A.q("SELECT DISTINCT project_id FROM tr_runs WHERE parent_id IS NULL AND thread_id IS NOT NULL AND start_time > %s AND (%s::int[] IS NULL OR project_id = ANY(%s))",
                                                    (now - dt.timedelta(days=IDLE_DAYS), project_ids, project_ids))}
    for pid in sorted(pids):
        try: await idle_threads(pid, now)
        except Exception as e: log.warning("idle_threads(%s) failed: %s", pid, _short(e))  # noqa: BLE001
    await _prune()

_PRUNED = {"t": 0.0}
async def _prune():
    if time.monotonic() - _PRUNED["t"] < 3600: return
    _PRUNED["t"] = time.monotonic()
    await A.q("DELETE FROM automation_applied WHERE ts < %s", (_now() - dt.timedelta(days=APPLIED_KEEP_DAYS),))

# ---- run rules
async def _late_runs(r, sql, params, now):
    """Runs that got feedback after fb_cursor_ts (a human score a day later...): -> (runs, new cursor). The cursor moves over every feedback row seen, matching or not."""
    fb = await A.q("SELECT run_id, max(created) ts FROM feedback WHERE project_id=%s AND created > %s GROUP BY run_id ORDER BY max(created) LIMIT %s", (r["project_id"], r["fb_cursor_ts"], BATCH_RUNS))
    if not fb: return [], None
    rows = await A.q(f"SELECT id, trace_id, thread_id, end_time FROM tr_runs WHERE project_id=%s AND parent_id IS NULL AND end_time IS NOT NULL AND end_time > %s AND id = ANY(%s::uuid[]) AND {sql}",
                     (r["project_id"], now - dt.timedelta(days=LATE_DAYS), [str(x["run_id"]) for x in fb], *params))
    order = {str(x["run_id"]): i for i, x in enumerate(fb)}
    return sorted(rows, key=lambda x: order[str(x["id"])]), fb[-1]["ts"]

async def _kinds(actions):
    ids = [a["evaluator_id"] for a in actions if a.get("type") in _JUDGE and isinstance(a.get("evaluator_id"), int)]
    return {x["id"]: x for x in await A.q("SELECT id, kind, (config ? 'feedback_keys') fk FROM evaluators WHERE id = ANY(%s)", (ids,))} if ids else {}

async def ordered(actions, kinds):
    """Fixed order (webhook is a batch step after all of these): queue, dataset, tag, online evaluators (composite last, evals.order_actions), code evaluators, composite evaluators, extend_retention."""
    import evals
    pick = lambda *t: [a for a in actions if a.get("type") in t]   # noqa: E731
    ev = await evals.order_actions(pick("run_evaluator"))
    k = lambda a: kinds.get(a.get("evaluator_id")) or {}   # noqa: E731
    comp = [a for a in ev if k(a).get("kind") == "composite" and k(a).get("fk")]
    code = [a for a in ev if k(a).get("kind") == "code"]
    plain = [a for a in ev if a not in comp and a not in code]
    return pick("add_to_queue") + pick("add_to_dataset") + pick("add_tag") + plain + pick("evaluate_thread") + code + comp + pick("extend_retention")

async def _exec(r, a, it, ts):
    """-> (status, result, detail). Raises on failure (the caller logs it)."""
    import evals
    t, rid = a["type"], str(it["id"])
    if t == "add_to_dataset": res = await evals.example_from_run(a["dataset_id"], rid, a.get("split"))
    elif t == "add_to_queue": res = await evals.queue_add(a["queue_id"], rid)
    elif t == "add_tag":
        await A.q("UPDATE tr_runs SET tags = array_append(tags, %s) WHERE id=%s AND NOT (%s = ANY(tags))", (a["tag"], it["id"], a["tag"])); res = "tagged"
    elif t == "run_evaluator": res = await evals.run_evaluator_on_run(a["evaluator_id"], rid, source="online")
    elif t == "evaluate_thread":
        tid = it.get("thread_id")
        if not tid: return "skipped", "không có thread", None
        res = await obs.evaluate_thread(r["project_id"], tid, a["evaluator_id"], source="thread" if r["item_type"] == "threads" else "online")
    elif t == "extend_retention":
        x = await obs.extend_trace(it["trace_id"], "rule", by=f"rule:{r['id']}")
        if x is None: return "skipped", "không áp dụng được (workspace demo hoặc không thấy trace)", None
        res = f"giữ đến {x['keep_until']:%Y-%m-%d}"
    else: raise HTTPException(400, "action không hỗ trợ")
    if isinstance(res, str) and res.startswith("bỏ qua"): return "skipped", res, None
    return "ok", res, None

async def _step(r, a, it, ctx, ts):
    t = a["type"]
    if t in _JUDGE and ctx["blocked"]:
        await _log(r["id"], it["id"], t, "bỏ qua: đã chạm hạn mức chi tiêu", "skipped", ctx["blocked"], ctx["batch"]); return
    try:
        st, res, detail = await _exec(r, a, it, ts)
    except Exception as e:  # noqa: BLE001
        if t in _JUDGE and isinstance(e, A.gw.SpendCapReached):   # the judge budget is used up: stop calling judges for the rest of this batch
            ctx["blocked"] = _short(e); st, res, detail = "skipped", "bỏ qua: đã chạm hạn mức chi tiêu", _short(e)
        else:
            st, res, detail = "error", "lỗi: " + _short(e), None
            ctx["errors"].append(_short(e))
    await _log(r["id"], it["id"], t, res, st, detail, ctx["batch"])

async def _run_rule(r, now):
    sql, params = obs.compile_filter(_filter_of(r))
    rows = await A.q(f"SELECT id, trace_id, thread_id, end_time FROM tr_runs WHERE project_id=%s AND parent_id IS NULL AND end_time IS NOT NULL AND end_time > %s AND {sql} ORDER BY end_time LIMIT {BATCH_RUNS}",
                     (r["project_id"], r["cursor_ts"], *params))
    late, fb_cur = ([], None)
    if re.search(r"\bfeedback\.", r.get("filter") or ""): late, fb_cur = await _late_runs(r, sql, params, now)
    if not rows and not late:
        if fb_cur or r.get("last_error"): await A.q("UPDATE automation_rules SET fb_cursor_ts=COALESCE(%s, fb_cursor_ts), last_error=NULL WHERE id=%s", (fb_cur, r["id"]))
        return
    acts = r["actions"] or []
    ctx = {"batch": str(uuid.uuid4()), "blocked": None, "errors": []}
    todo = [*rows, *[x for x in late if x["id"] not in {y["id"] for y in rows}]]
    steps = await ordered([a for a in acts if a.get("type") != "webhook"], await _kinds(acts))
    done = []
    for it in todo:
        if not _sampled(it["id"], r["sampling_rate"]): continue
        if not await _claim(r["id"], str(it["id"]), now): continue   # this (rule, run) was handled before (first look or an earlier late score)
        done.append(it)
        for a in steps: await _step(r, a, it, ctx, now)
    hooks = [a for a in acts if a.get("type") == "webhook"]
    if hooks and done: await _send_batch(r, hooks, done, ctx)
    await A.q("UPDATE automation_rules SET cursor_ts=COALESCE(%s, cursor_ts), fb_cursor_ts=COALESCE(%s, fb_cursor_ts), n_matched=n_matched+%s, n_applied=n_applied+%s, last_error=%s WHERE id=%s",
              (rows[-1]["end_time"] if rows else None, fb_cur, len(todo), len(done), ctx["errors"][0] if ctx["errors"] else None, r["id"]))

# ---- webhook batch
def _cut(x):
    s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False, default=str)
    return x if len(s) <= FIELD_CUT else s[:FIELD_CUT] + "…"

async def _batch_runs(r, items):
    """[{id, name, inputs, outputs, feedback_stats, trace_url}] for the batch, read AFTER the evaluators ran (scores included); long fields cut; masked when the workspace has PII masking on."""
    import pii
    ids = [str(i["id"]) for i in items]
    rows = {str(x["id"]): x for x in await A.q("SELECT id, trace_id, name, inputs, outputs FROM tr_runs WHERE project_id=%s AND id = ANY(%s::uuid[])", (r["project_id"], ids))}
    fb: dict = {}
    for x in await A.q("SELECT run_id, key, count(*) n, avg(score) avg, (array_agg(value ORDER BY id DESC))[1] value FROM feedback WHERE run_id = ANY(%s::uuid[]) GROUP BY run_id, key", (ids,)):
        fb.setdefault(str(x["run_id"]), {})[x["key"]] = {"n": x["n"], "avg": None if x["avg"] is None else float(x["avg"]), **({"value": x["value"]} if x["value"] is not None else {})}
    base = os.environ.get("AOS_PUBLIC_URL", "").rstrip("/")
    out = [{"id": i, "name": rows[i]["name"], "inputs": _cut(rows[i]["inputs"]), "outputs": _cut(rows[i]["outputs"]), "feedback_stats": fb.get(i, {}),
            "trace_url": f"{base}/projects?p={r['project_id']}&trace={rows[i]['trace_id']}"} for i in ids if i in rows]
    cfg = await pii.workspace_cfg(r["ws"])
    if cfg:
        for o in out: o["name"], o["inputs"], o["outputs"] = await asyncio.to_thread(lambda o=o: (pii.mask_obj(o["name"], cfg), pii.mask_obj(o["inputs"], cfg), pii.mask_obj(o["outputs"], cfg)))
    return out

def chunks(runs):
    """Split into POSTs of at most HOOK_RUNS runs and HOOK_BYTES bytes."""
    cur, size, out = [], 0, []
    for o in runs:
        n = len(json.dumps(o, ensure_ascii=False, default=str).encode())
        if cur and (len(cur) >= HOOK_RUNS or size + n > HOOK_BYTES): out.append(cur); cur, size = [], 0
        cur.append(o); size += n
    return out + ([cur] if cur else [])

async def _send_batch(r, hooks, items, ctx):
    runs = await _batch_runs(r, items)
    ends = [i["end_time"] for i in items if i.get("end_time")]
    for part in chunks(runs):
        data = {"rule_id": r["id"], "rule": r["name"], "project_id": r["project_id"], "environment": r.get("environment"),
                "start_time": min(ends).isoformat() if ends else None, "end_time": max(ends).isoformat() if ends else None, "runs": part}
        fail = None
        try:
            if any(not h.get("url") for h in hooks):
                n = await destinations.emit("rule.batch", data, r["ws"], project_id=r["project_id"])
                await _log(r["id"], None, "webhook", f"{n} đích nhận, {len(part)} run", "ok" if n else "skipped", None if n else "chưa có đích nào đăng ký sự kiện rule.batch", ctx["batch"])
            for h in hooks:   # old-style rule with its own url: still sent (netguard decides), now with the batch as the body
                if not h.get("url"): continue
                res = await obs.deliver("webhook", h["url"], f"Rule {r['name']}", json.dumps(data, ensure_ascii=False, default=str), ws_id=r["ws"])
                bad = res.startswith(("webhook blocked", "webhook failed")) or not res.startswith("webhook HTTP 2")
                await _log(r["id"], None, "webhook", res, "error" if bad else "ok", f"{len(part)} run", ctx["batch"])
                if bad: fail = res
        except Exception as e:  # noqa: BLE001
            fail = _short(e); await _log(r["id"], None, "webhook", "lỗi: " + fail, "error", None, ctx["batch"])
        if fail:
            ctx["errors"].append(fail)
            try: await destinations.emit("rule.error", {"title": f"Rule {r['name']}", "rule": r["name"], "rule_id": r["id"], "project_id": r["project_id"], "text": "Không gửi được webhook: " + fail[:200]}, r["ws"], project_id=r["project_id"])
            except Exception as e: log.warning("rule.error emit failed: %s", _short(e))  # noqa: BLE001

# ---------------------------------------------------------------- idle conversations (F20): the ONE trigger
_LISTENERS: list = []   # [(name, async fn(events))]; the thread rule is the first

def register_thread_idle(fn, name=None):
    """fn(events) is called with the conversations that just went idle in one project (each conversation once per stretch of activity). Same name = replaces. 12.70's judge uses this, no second trigger."""
    name = name or getattr(fn, "__qualname__", None) or str(id(fn))
    _LISTENERS[:] = [x for x in _LISTENERS if x[0] != name] + [(name, fn)]

def unregister_thread_idle(name): _LISTENERS[:] = [x for x in _LISTENERS if x[0] != name]

async def idle_threads(pid, now=None):
    """Find the conversations of project `pid` that went idle since the last look (last turn ended <= now - thread_idle_min), move the project cursor, call the listeners.
    First look at a project only sets the cursor (no catch-up). Only the last 7 days, <= 500 conversations per call. -> the events."""
    now = now or _now()
    p = await A.q("SELECT workspace_id ws, thread_idle_min idle, thread_idle_cursor cur FROM projects WHERE id=%s", (pid,), one=True)
    if not p or int(p["ws"]) in A.gw.DEMO_WS: return []
    until = now - dt.timedelta(minutes=p["idle"])
    if p["cur"] is None:
        await A.q("UPDATE projects SET thread_idle_cursor=%s WHERE id=%s AND thread_idle_cursor IS NULL", (until, pid)); return []
    since = max(p["cur"], now - dt.timedelta(days=IDLE_DAYS))
    if until <= since: return []
    act = "coalesce(end_time, start_time)"
    rows = await A.q(f"""SELECT thread_id, max({act}) last_end, count(*) turns, (array_agg(id ORDER BY start_time DESC))[1] last_run, (array_agg(trace_id ORDER BY start_time DESC))[1] last_trace
        FROM tr_runs WHERE project_id=%s AND parent_id IS NULL AND start_time > %s AND thread_id IN (
            SELECT DISTINCT thread_id FROM tr_runs WHERE project_id=%s AND parent_id IS NULL AND thread_id IS NOT NULL AND start_time > %s AND {act} > %s AND {act} <= %s)
        GROUP BY thread_id HAVING max({act}) > %s AND max({act}) <= %s ORDER BY max({act}) LIMIT {IDLE_LIMIT}""",
        (pid, now - dt.timedelta(days=IDLE_DAYS), pid, since - dt.timedelta(days=1), since, until, since, until))
    await A.q("UPDATE projects SET thread_idle_cursor=%s WHERE id=%s", (rows[-1]["last_end"] if len(rows) >= IDLE_LIMIT else until, pid))
    events = [{"project_id": pid, "workspace_id": int(p["ws"]), "thread_id": x["thread_id"], "last_end": x["last_end"], "last_run_id": str(x["last_run"]), "last_trace_id": str(x["last_trace"]),
               "turns": x["turns"], "idle_min": p["idle"], "at": now} for x in rows]
    if events:
        for name, fn in list(_LISTENERS):
            try: await fn(events)
            except Exception as e: log.warning("thread-idle listener %s failed: %s", name, _short(e))  # noqa: BLE001
    return events

async def _claim_thread(rid, tid, last_end):
    """Once per stretch of activity: key thread:<id> the first time; later activity (a turn ending after the stored time) = key thread:<id>:<last_end>, and the stored time moves on."""
    base = f"thread:{tid}"
    if await _claim(rid, base, last_end): return True
    prev = await A.q("SELECT ts FROM automation_applied WHERE rule_id=%s AND item_key=%s", (rid, base), one=True)
    if not prev or last_end <= prev["ts"] or not await _claim(rid, f"{base}:{last_end.isoformat()}", last_end): return False
    await A.q("UPDATE automation_applied SET ts=%s WHERE rule_id=%s AND item_key=%s", (last_end, rid, base)); return True

async def _rules_listener(events):
    """The first listener: every enabled 'threads' rule of the project looks at the conversations that just went idle."""
    pid = events[0]["project_id"]
    for r in await A.q("SELECT r.*, p.workspace_id ws FROM automation_rules r JOIN projects p ON p.id=r.project_id WHERE r.enabled AND r.item_type='threads' AND r.project_id=%s ORDER BY r.id", (pid,)):
        await _guarded(r, _run_thread_rule, events)

async def _run_thread_rule(r, events):
    if int(r["ws"]) in A.gw.DEMO_WS: return
    evs = [e for e in events if e["last_end"] > r["cursor_ts"]]   # only activity after the rule was created / switched on
    if evs and ((r.get("filter") or "").strip() or r.get("environment")):   # a conversation counts when one of its turns matches
        sql, params = obs.compile_filter(_filter_of(r))
        ok = {x["thread_id"] for x in await A.q(f"SELECT DISTINCT thread_id FROM tr_runs WHERE project_id=%s AND parent_id IS NULL AND thread_id = ANY(%s) AND {sql}", (r["project_id"], [e["thread_id"] for e in evs], *params))}
        evs = [e for e in evs if e["thread_id"] in ok]
    if not evs: return
    ctx = {"batch": str(uuid.uuid4()), "blocked": None, "errors": []}
    steps = await ordered(r["actions"] or [], await _kinds(r["actions"] or []))
    done = 0
    for e in evs:
        if not _sampled(f"thread:{e['thread_id']}", r["sampling_rate"]): continue
        if not await _claim_thread(r["id"], e["thread_id"], e["last_end"]): continue
        done += 1
        it = {"id": e["last_run_id"], "trace_id": e["last_trace_id"], "thread_id": e["thread_id"]}
        for a in steps: await _step(r, a, it, ctx, e["at"])
    await A.q("UPDATE automation_rules SET n_matched=n_matched+%s, n_applied=n_applied+%s, last_error=%s WHERE id=%s", (len(evs), done, ctx["errors"][0] if ctx["errors"] else None, r["id"]))

register_thread_idle(_rules_listener, "rules")
