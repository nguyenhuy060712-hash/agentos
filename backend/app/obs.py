"""P1 Observability (tự viết): project, run/trace lưu trong Postgres (partition theo tháng), ingest SDK,
ngôn ngữ lọc, thread, dashboard, alert, automation rule, chia sẻ trace bằng link ký HMAC, retention, worker nền."""
import asyncio, datetime as dt, hashlib, hmac, ipaddress, json, os, random, re, socket, time, uuid
from urllib.parse import urlparse
from fastapi import APIRouter, HTTPException, Header, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator
from typing import Literal
from psycopg.types.json import Jsonb
import notify
import pii   # GĐ9.8 PII masking (lazy presidio import)
import deepagent_api as A   # q(), S, gw (import vòng: chỉ dùng thuộc tính lúc chạy)

router = APIRouter()
SECRET = os.environ.get("AGENTOS_SECRET") or hashlib.sha256((os.environ.get("PG_URI", "") + "agentos").encode()).hexdigest()

DDL = """
CREATE TABLE IF NOT EXISTS projects(id SERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT,
  retention_days INT NOT NULL DEFAULT 30, created TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS ingest_keys(id SERIAL PRIMARY KEY, project_id INT REFERENCES projects(id) ON DELETE CASCADE,
  key_hash TEXT UNIQUE NOT NULL, prefix TEXT NOT NULL, label TEXT, created TIMESTAMPTZ NOT NULL DEFAULT now(), revoked BOOLEAN NOT NULL DEFAULT false);
CREATE TABLE IF NOT EXISTS tr_runs(id UUID NOT NULL, trace_id UUID NOT NULL, parent_id UUID, project_id INT NOT NULL,
  name TEXT, run_type TEXT, start_time TIMESTAMPTZ NOT NULL, end_time TIMESTAMPTZ, latency_ms INT, status TEXT,
  error TEXT, inputs JSONB, outputs JSONB, metadata JSONB NOT NULL DEFAULT '{}', tags TEXT[] NOT NULL DEFAULT '{}',
  input_tokens INT, output_tokens INT, total_tokens INT, cost_usd NUMERIC(12,6), model TEXT, thread_id TEXT,
  dotted_order TEXT, PRIMARY KEY(id, start_time)) PARTITION BY RANGE(start_time);
CREATE TABLE IF NOT EXISTS tr_runs_default PARTITION OF tr_runs DEFAULT;
CREATE INDEX IF NOT EXISTS tr_runs_proj_time ON tr_runs(project_id, start_time DESC);
CREATE INDEX IF NOT EXISTS tr_runs_root ON tr_runs(project_id, start_time DESC) WHERE parent_id IS NULL;
CREATE INDEX IF NOT EXISTS tr_runs_trace ON tr_runs(trace_id);
CREATE INDEX IF NOT EXISTS tr_runs_id ON tr_runs(id);
CREATE INDEX IF NOT EXISTS tr_runs_thread ON tr_runs(project_id, thread_id) WHERE thread_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS tr_runs_tags ON tr_runs USING GIN(tags);
CREATE INDEX IF NOT EXISTS tr_runs_meta ON tr_runs USING GIN(metadata jsonb_path_ops);
CREATE TABLE IF NOT EXISTS feedback(id BIGSERIAL PRIMARY KEY, run_id UUID NOT NULL, trace_id UUID, key TEXT NOT NULL,
  score DOUBLE PRECISION, value TEXT, comment TEXT, correction JSONB, source TEXT NOT NULL DEFAULT 'human',
  evaluator_id INT, experiment_id INT, created TIMESTAMPTZ NOT NULL DEFAULT now(), created_by TEXT);
CREATE INDEX IF NOT EXISTS feedback_run ON feedback(run_id);
CREATE INDEX IF NOT EXISTS feedback_key ON feedback(key, created DESC);
CREATE TABLE IF NOT EXISTS alert_rules(id SERIAL PRIMARY KEY, project_id INT REFERENCES projects(id) ON DELETE CASCADE, name TEXT NOT NULL,
  metric TEXT NOT NULL, feedback_key TEXT, op TEXT NOT NULL, threshold DOUBLE PRECISION NOT NULL, window_min INT NOT NULL DEFAULT 15,
  filter TEXT, channel TEXT NOT NULL, target TEXT, enabled BOOLEAN NOT NULL DEFAULT true, last_fired TIMESTAMPTZ, last_value DOUBLE PRECISION,
  created TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS alert_events(id BIGSERIAL PRIMARY KEY, rule_id INT REFERENCES alert_rules(id) ON DELETE CASCADE, ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  value DOUBLE PRECISION, delivery TEXT, detail JSONB);
CREATE TABLE IF NOT EXISTS automation_rules(id SERIAL PRIMARY KEY, project_id INT REFERENCES projects(id) ON DELETE CASCADE, name TEXT NOT NULL,
  filter TEXT, sampling_rate DOUBLE PRECISION NOT NULL DEFAULT 1, actions JSONB NOT NULL, enabled BOOLEAN NOT NULL DEFAULT true,
  cursor_ts TIMESTAMPTZ NOT NULL DEFAULT now(), n_matched INT NOT NULL DEFAULT 0, n_applied INT NOT NULL DEFAULT 0, created TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS automation_log(id BIGSERIAL PRIMARY KEY, rule_id INT REFERENCES automation_rules(id) ON DELETE CASCADE, run_id UUID,
  action TEXT, result TEXT, ts TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS shares(id TEXT PRIMARY KEY, trace_id UUID NOT NULL, expires TIMESTAMPTZ NOT NULL, revoked BOOLEAN NOT NULL DEFAULT false,
  created TIMESTAMPTZ NOT NULL DEFAULT now(), views INT NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS outbox(id BIGSERIAL PRIMARY KEY, kind TEXT NOT NULL, target TEXT, subject TEXT, body TEXT, status TEXT NOT NULL,
  created TIMESTAMPTZ NOT NULL DEFAULT now(), detail TEXT);
ALTER TABLE outbox ADD COLUMN IF NOT EXISTS ws_id INT, ADD COLUMN IF NOT EXISTS channel JSONB, ADD COLUMN IF NOT EXISTS link TEXT,
  ADD COLUMN IF NOT EXISTS attempts INT NOT NULL DEFAULT 0, ADD COLUMN IF NOT EXISTS last_error TEXT, ADD COLUMN IF NOT EXISTS next_at TIMESTAMPTZ;
ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS channel_cfg JSONB;
"""

async def ensure_partitions():
    today = dt.date.today().replace(day=1)
    for k in range(-1, 3):
        y, m = today.year + (today.month - 1 + k) // 12, (today.month - 1 + k) % 12 + 1
        start = dt.date(y, m, 1); end = dt.date(y + (m // 12), m % 12 + 1, 1)
        name = f"tr_runs_{start:%Y%m}"
        if not await A.q("SELECT 1 FROM pg_class WHERE relname=%s", (name,)):
            # chuyển các dòng đã lỡ rơi vào default sang partition mới
            await A.q(f"CREATE TABLE {name} (LIKE tr_runs INCLUDING DEFAULTS)")
            await A.q(f"WITH m AS (DELETE FROM tr_runs_default WHERE start_time >= %s AND start_time < %s RETURNING *) INSERT INTO {name} SELECT * FROM m", (start, end))
            await A.q(f"ALTER TABLE tr_runs ATTACH PARTITION {name} FOR VALUES FROM ('{start}') TO ('{end}')")

DDL2 = """
CREATE TABLE IF NOT EXISTS redaction_rules(id SERIAL PRIMARY KEY, name TEXT NOT NULL, pattern TEXT NOT NULL, replacement TEXT NOT NULL DEFAULT '[ĐÃ ẨN]',
  enabled BOOLEAN NOT NULL DEFAULT true, created TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS saved_filters(id SERIAL PRIMARY KEY, project_id INT REFERENCES projects(id) ON DELETE CASCADE, name TEXT NOT NULL, filter TEXT NOT NULL,
  created TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(project_id, name));
CREATE TABLE IF NOT EXISTS run_attachments(id BIGSERIAL PRIMARY KEY, run_id UUID NOT NULL, trace_id UUID, name TEXT NOT NULL, mime TEXT, size INT, data BYTEA NOT NULL,
  created TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS run_att_run ON run_attachments(run_id);
"""

async def init():
    await A.q(DDL); await A.q(DDL2)
    if not await A.q("SELECT 1 FROM redaction_rules LIMIT 1"):
        for n, pt, rp in REDACT_DEFAULTS: await A.q("INSERT INTO redaction_rules(name,pattern,replacement) VALUES(%s,%s,%s)", (n, pt, rp))
    _RX["rules"] = None
    await ensure_partitions()
    await A.q("INSERT INTO projects(name, description) VALUES('agents','Trace của các agent trong Agent OS') ON CONFLICT DO NOTHING")
    await A.q("INSERT INTO projects(name, description) VALUES('sdk-demo','Trace gửi từ SDK @traceable') ON CONFLICT DO NOTHING")
    import envs; await envs.init()   # GĐ9.10 environment labels (projects / ingest keys / alert rules)

async def project_id(name):
    r = await A.q("INSERT INTO projects(name) VALUES(%s) ON CONFLICT(workspace_id,name) DO UPDATE SET name=EXCLUDED.name RETURNING id", (name,), one=True)
    return r["id"]

# ------------------------------------------------------------------ ghi run
def _ts(v):
    if v is None: return None
    if isinstance(v, (int, float)): return dt.datetime.fromtimestamp(v, dt.timezone.utc)
    if isinstance(v, dt.datetime): return v
    return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))

# ------------------------------------------------------------------ redaction phía server
REDACT_DEFAULTS = [
    ("Google API key", r"AIza[0-9A-Za-z\-_]{35}", "[ĐÃ ẨN:google_key]"),
    ("OpenAI-style key", r"\bsk-[A-Za-z0-9_\-]{20,}", "[ĐÃ ẨN:api_key]"),
    ("Bearer token", r"(?i)bearer\s+[A-Za-z0-9\-._~+/]{16,}=*", "Bearer [ĐÃ ẨN]"),
    ("Agent OS ingest key", r"\bak_[0-9a-f]{36}\b", "[ĐÃ ẨN:ingest_key]"),
    ("JWT", r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}", "[ĐÃ ẨN:jwt]"),
    ("password=…", r"(?i)(password|passwd|mật khẩu|pwd)(\s*[:=]\s*)[^\s,;\"']+", r"\1\2[ĐÃ ẨN]"),
]
_RX = {"rules": None}
async def redact_rules():
    if _RX["rules"] is None:
        rows = await A.q("SELECT pattern, replacement FROM redaction_rules WHERE enabled ORDER BY id")
        _RX["rules"] = [(re.compile(r["pattern"]), r["replacement"]) for r in rows]
    return _RX["rules"]

def _redact_val(v, rules):
    if v is None or not rules: return v
    txt = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    new = txt
    for rx, rp in rules: new = rx.sub(rp, new)
    if new == txt: return v
    return new if isinstance(v, str) else json.loads(new)

# ---- GĐ12.15 usage / cost detail columns (tr_runs.usage_details, cost_details, input_cost, output_cost)
def _cnt(v): return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else 0
def _usd(v): return round(float(v), 6) if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 else None

def usage_details_of(u):
    """Token counts by kind {cache_read, cache_creation, reasoning, audio, image, video} from a LangChain usage dict (input_token_details / output_token_details).
    audio / image / video add the input and the output side. Zero kinds are left out; nothing -> {}."""
    u = u or {}; i_, o_ = u.get("input_token_details") or {}, u.get("output_token_details") or {}
    d = {"cache_read": _cnt(i_.get("cache_read")),
         "cache_creation": _cnt(i_.get("cache_creation")) + _cnt(i_.get("ephemeral_5m_input_tokens")) + _cnt(i_.get("ephemeral_1h_input_tokens")),   # same reading as gateway.details_from_usage
         "reasoning": _cnt(o_.get("reasoning"))}
    for k in ("audio", "image", "video"): d[k] = _cnt(i_.get(k)) + _cnt(o_.get(k))
    return {k: v for k, v in d.items() if v}

def cost_parts(model, u, it, ot):
    """(input_cost, output_cost, {kind: usd}) of one call priced with the gateway table; (None, None, None) when the model has no price or no tokens.
    output = total - input, so the two always add up to gateway.cost()."""
    gw = A.gw; p = gw.PRICING.get(gw._cn(model or ""))
    if not p or not (it or ot): return None, None, None
    d = gw.details_from_usage(u); total = gw.cost(model, it or 0, ot or 0, d); inp = gw.cost(model, it or 0, 0, d)
    if total is None or inp is None: return None, None, None
    long = (it or 0) > (p.get("long_context_tokens") or 200000)
    pin = p["input_per_1m_long"] if long and p.get("input_per_1m_long") is not None else p["input_per_1m"]
    pout = p["output_per_1m_long"] if long and p.get("output_per_1m_long") is not None else p["output_per_1m"]
    pr = lambda field, base: base if p.get(field) is None else p[field]   # noqa: E731  (a kind without its own price is billed at the plain price)
    n = lambda k: d.get(k) or 0   # noqa: E731
    parts = {"cache_read": n("cached") * pr("cached_input_per_1m", pin),
             "cache_creation": n("cache_write") * pr("cache_write_per_1m", pin) + n("cache_write_1h") * pr("cache_write_1h_per_1m", pin),
             "reasoning": n("reasoning") * pr("reasoning_per_1m", pout),
             "audio": n("audio_in") * pr("audio_input_per_1m", pin) + n("audio_out") * pr("audio_output_per_1m", pout),
             "image": n("image_in") * pr("image_input_per_1m", pin) + n("image_out") * pr("image_output_per_1m", pout)}
    return inp, round(total - inp, 6), {k: round(v / 1e6, 6) for k, v in parts.items() if v}

def _kinds(d, money=False):
    """Keep only the known kinds of a usage_details / cost_details dict sent by an SDK (numbers >= 0); {} when it is not a dict."""
    if not isinstance(d, dict): return {}
    f = _usd if money else _cnt
    return {k: f(d[k]) for k in ("cache_read", "cache_creation", "reasoning", "audio", "image", "video") if f(d.get(k))}

def _opt_ts(v):
    try: return _ts(v)
    except (ValueError, TypeError, OverflowError, OSError): return None

def _opt_uuid(v):
    try: return str(uuid.UUID(str(v))) if v else None
    except ValueError: return None

async def _depth(r, parent_id):
    """tr_runs.depth: dotted_order segments - 1; else 0 for a root, else the parent's depth + 1 (NULL when the parent is unknown)."""
    do = r.get("dotted_order")
    if do: return max(len(str(do).split(".")) - 1, 0)
    if not parent_id: return 0
    p = await A.q("SELECT depth FROM tr_runs WHERE id=%s", (str(parent_id),), one=True)
    return p["depth"] + 1 if p and p["depth"] is not None else None

async def upsert_run(pid, r: dict, env=None):
    rules = await redact_rules()
    if rules:
        r = dict(r)
        for k in ("inputs", "outputs", "error"): r[k] = _redact_val(r.get(k), rules)
    pcfg = await pii.project_cfg(pid)   # GĐ9.8: Presidio + Vietnamese recognizers, per workspace; the original never reaches the table
    if pcfg:
        r = dict(r); pz = pii.Pseudonymizer() if pcfg["mode"] == "pseudonym" else None
        for k in ("inputs", "outputs", "error"): r[k] = await pii.amask_obj(r.get(k), pcfg, pz)
    st, en = _ts(r.get("start_time")) or dt.datetime.now(dt.timezone.utc), _ts(r.get("end_time"))
    md = r.get("metadata") or (r.get("extra") or {}).get("metadata") or {}
    if env and not md.get("environment") and not md.get("ls_environment"): md = {**md, "environment": env}   # GĐ9.10: default env of the ingest key / project
    u = r.get("usage") or {}
    it, ot = u.get("input_tokens") or md.get("input_tokens"), u.get("output_tokens") or md.get("output_tokens")
    model = r.get("model") or md.get("ls_model_name") or md.get("model")
    tid = r.get("thread_id") or md.get("thread_id") or md.get("session_id") or md.get("conversation_id")
    c = r.get("cost_usd")
    mdc = md.get("cost_usd")   # GĐ6.2: non-LLM step cost (API call, tool...) declared in metadata / extra.metadata
    mdc = float(mdc) if isinstance(mdc, (int, float)) and not isinstance(mdc, bool) and mdc >= 0 else None
    if c is None and mdc is not None: c = mdc
    if c is None and model and (it or ot): c = A.gw.cost(model, it or 0, ot or 0, A.gw.details_from_usage(u))   # GĐ12.15: cached / reasoning / audio tokens priced at their own rate
    status = "error" if r.get("error") else ("success" if en else "pending")
    vals = dict(id=r["id"], trace_id=r.get("trace_id") or r["id"], parent_id=r.get("parent_run_id") or r.get("parent_id"), project_id=pid,
                name=(r.get("name") or "run")[:200], run_type=r.get("run_type") or "chain", start_time=st, end_time=en,
                latency_ms=int((en - st).total_seconds() * 1000) if en else None, status=status, error=(r.get("error") or None) and str(r["error"])[:4000],
                inputs=Jsonb(r.get("inputs")) if r.get("inputs") is not None else None, outputs=Jsonb(r.get("outputs")) if r.get("outputs") is not None else None,
                metadata=Jsonb(md), tags=list(r.get("tags") or []), input_tokens=it, output_tokens=ot,
                total_tokens=(it or 0) + (ot or 0) if (it or ot) else None, cost_usd=c, model=model, thread_id=tid and str(tid)[:200],
                dotted_order=r.get("dotted_order"), user_id=r.get("user_id"))
    ic, oc, cdet = cost_parts(model, u, it, ot)   # GĐ12.15: details SDK / OTLP bodies send themselves win over our own price table
    ic, oc = (_usd(r.get("input_cost")), _usd(r.get("output_cost"))) if r.get("input_cost") is not None or r.get("output_cost") is not None else (ic, oc)
    ud, cdet = usage_details_of(u) or _kinds(r.get("usage_details")), _kinds(r.get("cost_details"), True) or cdet
    ft, ref = _opt_ts(r.get("first_token_time")), _opt_uuid(r.get("reference_example_id"))
    vals.update(first_token_time=ft if ft and ft >= st else None, usage_details=Jsonb(ud) if ud else None, cost_details=Jsonb(cdet) if cdet else None,
                input_cost=ic, output_cost=oc, reference_example_id=ref, depth=None, ttl_days=None)
    import aos_crypto
    ws_enc = await aos_crypto.trace_ws(pid)   # GĐ9.7: workspace bật mã hoá trace -> inputs/outputs lưu dạng phong bì AES-GCM-SIV (aos_crypto.wrap_json)
    import fts
    fparts = fts.tsv_text(r.get("inputs"), r.get("outputs"), vals["error"], vals["tags"], md, ws_enc)   # GĐ12.17: search words from the plaintext (redacted / masked above), BEFORE encryption; blind hashes when encrypted
    if ws_enc is not None: aos_crypto.encrypt_trace_vals(vals, ws_enc)
    ex = await A.q("SELECT start_time FROM tr_runs WHERE id=%s", (vals["id"],), one=True)
    if ex:
        present = {"end_time": "end_time", "outputs": "outputs", "error": "error", "name": "name", "model": "model",
                   "usage": None, "cost_usd": "cost_usd", "thread_id": "thread_id"}
        sets = {}
        for k, col in present.items():
            if r.get(k) is None: continue
            if k == "usage":
                sets.update(input_tokens=it, output_tokens=ot, total_tokens=vals["total_tokens"])
                m = model or (await A.q("SELECT model FROM tr_runs WHERE id=%s", (vals["id"],), one=True))["model"]
                if not model and m:   # GĐ12.15: the PATCH brings the usage but not the model: price it with the stored model
                    ic2, oc2, cd2 = cost_parts(m, u, it, ot)
                    vals.update(input_cost=ic2 if vals["input_cost"] is None else vals["input_cost"], output_cost=oc2 if vals["output_cost"] is None else vals["output_cost"],
                                cost_details=vals["cost_details"] if vals["cost_details"] is not None else (Jsonb(cd2) if cd2 else None))
                sets.update({c_: vals[c_] for c_ in ("usage_details", "cost_details", "input_cost", "output_cost") if vals[c_] is not None})
                if r.get("cost_usd") is None and m and (it or ot): sets["cost_usd"] = A.gw.cost(m, it or 0, ot or 0, A.gw.details_from_usage(u))
            else:
                sets[col] = vals[col]
        if r.get("cost_usd") is None and mdc is not None: sets["cost_usd"] = mdc
        if ft is not None and ft >= ex["start_time"]: sets["first_token_time"] = ft   # a PATCH is checked against the STORED start
        if ref is not None: sets["reference_example_id"] = ref
        if r.get("metadata"): sets["metadata"] = Jsonb({**(await A.q("SELECT metadata FROM tr_runs WHERE id=%s", (vals["id"],), one=True))["metadata"], **md})
        if r.get("tags"): sets["tags"] = vals["tags"]
        if en:
            sets["latency_ms"] = int((en - ex["start_time"]).total_seconds() * 1000); sets["status"] = "error" if r.get("error") else "success"
        elif r.get("error"):
            sets["status"] = "error"
        if sets:
            await A.q("UPDATE tr_runs SET " + ", ".join(f"{k}=%s" for k in sets) + " WHERE id=%s", (*sets.values(), vals["id"]))
        pparts = fts.tsv_text(r.get("inputs"), r.get("outputs"), r.get("error"), r.get("tags"), md if r.get("metadata") else None, ws_enc)   # GĐ12.17: a PATCH adds its new words (NULL = not indexed yet: the backfill does the whole row)
        if any(pparts): await A.q("UPDATE tr_runs SET search_tsv = CASE WHEN search_tsv IS NULL THEN NULL ELSE search_tsv || " + fts.TSV_SQL + " END WHERE id=%s AND start_time=%s", (*pparts, vals["id"], ex["start_time"]))
    else:
        vals["depth"] = await _depth(r, vals["parent_id"])
        # GĐ12.18: written on INSERT only (a later policy change never shortens it). A child run takes its trace root's ttl so a trace is never left half-deleted.
        root = await A.q("SELECT ttl_days FROM tr_runs WHERE id=%s AND ttl_days IS NOT NULL", (str(vals["trace_id"]),), one=True) if str(vals["trace_id"]) != str(vals["id"]) else None
        if root: vals["ttl_days"] = root["ttl_days"]
        else: pol = await retention_policy(pid); vals["ttl_days"] = min(pol["days"], 32767) if pol else 30
        await A.q("INSERT INTO tr_runs(" + ",".join(vals) + ",search_tsv) VALUES(" + ",".join(["%s"] * len(vals)) + "," + fts.TSV_SQL + ")", (*vals.values(), *fparts))

async def record_agent_trace(*, agent_name, agent_id, version, thread_id, message, answer, error, t_start_wall, latency_ms, spans,
                             input_tokens, output_tokens, cost, models, eval_run_id, deployment=None, test_run_id=None, experiment_id=None, project="agents", tags=None, parent_trace=None, first_token_ms=None):
    """Chuyển span của agent (gateway) thành cây run trong tr_runs. first_token_ms (GĐ12.15): ms from the start to the first streamed token (None = not streamed)."""
    pid = await project_id(project)
    root = uuid.uuid4(); tid = str(parent_trace or root); base = dt.datetime.fromtimestamp(t_start_wall, dt.timezone.utc)
    ext = None; ext_parent_run = None
    import otel_ctx
    tc = None if parent_trace else otel_ctx.current()
    if tc:   # GĐ9.10: the request carried a W3C traceparent -> continue that trace; our root run owns the span id we already propagate downstream
        rid = otel_ctx.run_id_for(tc.trace_id, tc.span_id)
        if not await A.q("SELECT 1 FROM tr_runs WHERE id=%s", (rid,), one=True):
            root = uuid.UUID(rid); tid = str(uuid.UUID(hex=tc.trace_id))
            pr = otel_ctx.run_id_for(tc.trace_id, tc.parent_span_id)
            if await A.q("SELECT 1 FROM tr_runs WHERE id=%s", (pr,), one=True): ext_parent_run = pr   # the caller is one of our own runs (same trace): a true child
            else: ext = {"trace_id": tc.trace_id, "span_id": tc.parent_span_id, "traceparent": tc.raw, "tracestate": tc.tracestate or None, "source": "traceparent"}
    md = {"agent_id": agent_id, "agent_version": version, "eval_run_id": eval_run_id, "thread_id": thread_id, "deployment": deployment,
          "models": models, "test_run_id": test_run_id, "experiment_id": experiment_id}
    md["resumed_from"] = str(parent_trace) if parent_trace else None
    if ext: md["external_parent"] = {k: v for k, v in ext.items() if v}
    import admin as _adm
    cu = _adm.cur(); uid = cu["id"] if cu and cu.get("auth") == "run" else None   # GĐ6.10: chat/agent-server runs are traced on behalf of the asking user
    md = {k: v for k, v in md.items() if v is not None}
    await upsert_run(pid, {"id": str(root), "trace_id": tid, **({"parent_run_id": tid} if parent_trace else {"parent_run_id": ext_parent_run} if ext_parent_run else {}), "name": agent_name, "run_type": "chain", "start_time": base,
                           "end_time": base + dt.timedelta(milliseconds=latency_ms), "inputs": {"message": message}, "outputs": {"answer": answer},
                           "error": error, "metadata": md, "tags": [f"agent:{agent_id}", f"v{version}"] + (["deployment"] if deployment else []) + list(tags or []),
                           "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}, "cost_usd": cost, "thread_id": thread_id,
                           "model": models[0] if models else None, "user_id": uid,
                           **({"first_token_time": base + dt.timedelta(milliseconds=max(1, min(int(first_token_ms), max(latency_ms, 1))))} if first_token_ms is not None else {})})   # LLM spans: NULL (the stream is not split per call), never 0
    ids = {}
    for sp in spans:
        ids[sp["id"]] = str(uuid.uuid4())
    rt = {"llm": "llm", "tool": "tool", "subagent": "chain", "todos": "tool"}
    for sp in spans:
        st = base + dt.timedelta(milliseconds=sp.get("start_ms", 0)); en = base + dt.timedelta(milliseconds=sp.get("end_ms", sp.get("start_ms", 0)))
        await upsert_run(pid, {"id": ids[sp["id"]], "trace_id": tid, "parent_run_id": ids.get(sp.get("parent")) or str(root),
                               "name": sp["name"], "run_type": rt.get(sp["kind"], "chain"), "start_time": st, "end_time": en,
                               "inputs": {"args": sp.get("args")} if sp.get("args") is not None else None,
                               "outputs": {"result": sp.get("result")} if sp.get("result") else None, "error": sp.get("error"),
                               "metadata": {"kind": sp["kind"], **({"attempt": sp["attempt"]} if sp.get("attempt") else {}),
                                            **({"lc_agent_name": sp["lc_agent_name"]} if sp.get("lc_agent_name") and sp["lc_agent_name"] != f"agent_{agent_id}" else {})},   # GĐ11.63: deepagents 0.7.32 names each sub-agent; the main agent is `agent_<id>`
                               "usage": {"input_tokens": sp.get("input_tokens"), "output_tokens": sp.get("output_tokens")},
                               "cost_usd": sp.get("cost_usd"), "model": sp["name"] if sp["kind"] == "llm" else None, "user_id": uid})
    return str(root)

# ------------------------------------------------------------------ ingest (SDK)
class Ingest(BaseModel):
    post: list[dict] = []
    patch: list[dict] = []

def _h(k): return hashlib.sha256(k.encode()).hexdigest()
def _cws():
    import admin; return admin.cur_ws()

async def _key_project(key, project_name=None):
    r = await A.q("SELECT k.project_id, p.name FROM ingest_keys k JOIN projects p ON p.id=k.project_id WHERE key_hash=%s AND NOT revoked", (_h(key or ""),), one=True)
    if not r: raise HTTPException(401, "Ingest key sai")
    return r["project_id"]

async def key_ctx(key):
    """(project_id, default environment) of an ingest key: the key's own environment, else its project's (GĐ9.10)."""
    r = await A.q("SELECT k.project_id, coalesce(k.environment, p.environment) env FROM ingest_keys k JOIN projects p ON p.id=k.project_id WHERE key_hash=%s AND NOT revoked", (_h(key or ""),), one=True)
    if not r: raise HTTPException(401, "Ingest key sai")
    return r["project_id"], r["env"]

async def key_ws(key):
    """GĐ10.1: workspace that owns an ingest key (the workspace of the key's project). Invalid / revoked key -> 401."""
    r = await A.q("SELECT p.workspace_id FROM ingest_keys k JOIN projects p ON p.id=k.project_id WHERE key_hash=%s AND NOT revoked", (_h(key or ""),), one=True)
    if not r: raise HTTPException(401, "Ingest key sai")
    return r["workspace_id"]

@router.post("/ingest/runs", tags=["ingest"])
async def ingest(b: Ingest, x_api_key: str = Header(default="")):
    import admin
    ws = await key_ws(x_api_key)
    async with admin.as_workspace(ws):   # GĐ10.1: RLS of the key's workspace -> a run id owned by another workspace is invisible (no cross-workspace PATCH / rollup)
        pid, env = await key_ctx(x_api_key)
        if len(b.post) + len(b.patch) > 1000: raise HTTPException(413, "Tối đa 1000 run / lô")
        for r in b.post + b.patch:
            if "id" not in r: raise HTTPException(400, "Thiếu id")
        await check_trace_cap(b.post + b.patch)   # GĐ12.15: refuse the whole batch (nothing written) when a trace would pass 25,000 runs
        for r in b.post + b.patch:
            await upsert_run(pid, r, env=env)
        await rollup({r.get("trace_id") or r["id"] for r in b.post + b.patch})
    return {"ok": True, "n": len(b.post) + len(b.patch)}

TRACE_RUN_CAP = 25_000   # GĐ12.15 hard limit of runs in one trace (write side)

async def check_trace_cap(runs):
    """HTTP 413 when adding the NEW runs of `runs` (ids not stored yet) would bring any trace over TRACE_RUN_CAP. Runs already stored (PATCH / re-sent POST) do not count."""
    ids = [str(r["id"]) for r in runs if r.get("id")]
    if not ids: return
    try: have = {str(x["id"]) for x in await A.q("SELECT id FROM tr_runs WHERE id = ANY(%s::uuid[])", (ids,))}
    except Exception: return   # noqa: BLE001  (a malformed id is rejected later by upsert_run with its own error)
    add = {}
    for r in runs:
        if r.get("id") and str(r["id"]) not in have: add[str(r.get("trace_id") or r["id"])] = add.get(str(r.get("trace_id") or r["id"]), 0) + 1
    for t, n in add.items():
        try: cnt = (await A.q("SELECT count(*) n FROM tr_runs WHERE trace_id=%s::uuid", (t,), one=True))["n"]
        except Exception: continue   # noqa: BLE001
        if cnt + n > TRACE_RUN_CAP:
            raise HTTPException(413, f"Trace {t} would have {cnt + n} runs; the limit is {TRACE_RUN_CAP} runs per trace. Nothing was written. / Trace vượt {TRACE_RUN_CAP} run, lô này bị từ chối.")

_COSTED = "(run_type='llm' OR jsonb_typeof(metadata->'cost_usd')='number')"   # runs whose cost counts toward the trace total (LLM calls + non-LLM steps with a declared cost)
async def rollup(trace_ids):
    """Gộp token/chi phí của các run llm con lên run gốc. GĐ12.8: MỘT định nghĩa chi phí (như LangSmith) = tổng chi phí các run LLM trong cây trace; gốc luôn được tính lại
    (idempotent) trừ khi gốc chính là run LLM hoặc tự khai báo metadata.cost_usd. Trước đây gốc đã có token/model bị bỏ qua nên run nối tiếp (resume) bị mất khỏi tổng."""
    for t in trace_ids:
        await A.q("""UPDATE tr_runs g SET input_tokens=x.i, output_tokens=x.o, total_tokens=x.t, cost_usd=coalesce(x.c, g.cost_usd)
            FROM (SELECT sum(input_tokens) i, sum(output_tokens) o, sum(total_tokens) t, sum(cost_usd) c FROM tr_runs WHERE trace_id=%s AND parent_id IS NOT NULL AND """ + _COSTED + """) x
            WHERE g.trace_id=%s AND g.parent_id IS NULL AND x.t IS NOT NULL AND g.run_type IS DISTINCT FROM 'llm' AND jsonb_typeof(g.metadata->'cost_usd') IS DISTINCT FROM 'number'""", (str(t), str(t)))
        # non-LLM steps with a declared metadata.cost_usd and no LLM tokens at all: still roll their cost up (each run counted once)
        await A.q("""UPDATE tr_runs g SET cost_usd=x.c FROM (SELECT sum(cost_usd) c FROM tr_runs WHERE trace_id=%s AND parent_id IS NOT NULL AND """ + _COSTED + """) x
            WHERE g.trace_id=%s AND g.parent_id IS NULL AND x.c IS NOT NULL AND g.total_tokens IS NULL AND g.model IS NULL AND jsonb_typeof(g.metadata->'cost_usd') IS DISTINCT FROM 'number'""", (str(t), str(t)))

FB_CORRECTION_MAX, FB_METADATA_MAX = 8192, 4096   # GĐ10.14: JSON size limits (characters) of a feedback correction / metadata

class FbIn(BaseModel):
    run_id: str
    key: str = Field(min_length=1, max_length=100)
    score: float | None = None
    value: str | None = None
    comment: str | None = None
    correction: dict | None = None
    metadata: dict | None = None   # GĐ10.14: free-form JSON stored in feedback.metadata (migration 0004)

    @field_validator("metadata")
    @classmethod
    def _small(cls, v):
        if v is not None and len(json.dumps(v, ensure_ascii=False, default=str)) > FB_METADATA_MAX: raise ValueError(f"metadata is larger than {FB_METADATA_MAX} bytes")
        return v

@router.post("/ingest/feedback", tags=["ingest"])
async def ingest_feedback(f: FbIn, x_api_key: str = Header(default="")):
    import admin
    ws = await key_ws(x_api_key)
    async with admin.as_workspace(ws):   # GĐ10.1: the run must be visible to the key's workspace (another workspace's run id -> 404)
        return await add_feedback(f, "sdk")

# ------------------------------------------------------------------ ngôn ngữ lọc
FIELDS = {"name": "name", "run_type": "run_type", "status": "status", "error": "error", "model": "model", "thread_id": "thread_id",
          "latency": "latency_ms/1000.0", "latency_ms": "latency_ms", "total_tokens": "total_tokens", "input_tokens": "input_tokens",
          "output_tokens": "output_tokens", "cost": "cost_usd", "start_time": "start_time", "trace_id": "trace_id::text", "id": "id::text",
          "is_root": "(parent_id IS NULL)", "environment": "environment",
          "depth": "depth", "revision_id": "(metadata->>'revision_id')", "modality": "(metadata->>'ls_modality')", "first_token_time": "first_token_time"}   # GĐ12.16: depth / revision_id / modality / first_token_time; GĐ9.10: effective environment (run metadata > project)
OPS = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
TOK = re.compile(r'\s*(?:(?P<str>"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')|(?P<num>-?\d+(?:\.\d+)?)|(?P<id>[A-Za-z_][\w.\-]*)|(?P<p>[(),]))')

PATH_ROOTS = ("metadata", "inputs", "outputs")
NUMERIC_FIELDS = ("latency", "latency_ms", "total_tokens", "input_tokens", "output_tokens", "cost", "depth")
FB_COL_RX = re.compile(r"(.+)\.(value|comment|source|score)")

def _path(name):
    """GĐ12.16: `metadata.a.b` / `inputs.a.b` / `outputs.a.b` -> (root, [a, b]); None when `name` is not such a path. Each segment letters, digits, _ or -, at most 6."""
    root, _, rest = name.partition(".")
    if root not in PATH_ROOTS or not rest: return None
    segs = rest.split(".")
    if len(segs) > 6 or not all(re.fullmatch(r"[\w\-]+", x) for x in segs):
        raise HTTPException(400, "Đường dẫn lọc không hợp lệ: mỗi đoạn chỉ gồm chữ, số, _ và -, tối đa 6 đoạn. Lưu ý: run của workspace mã hoá không bao giờ khớp đường dẫn inputs./outputs. "
                                 "/ Invalid filter path: each segment letters, digits, _ and -, at most 6 segments. Rows of an encrypted workspace never match inputs./outputs. paths")
    return root, segs

def _tokens(s):
    pos, out = 0, []
    while pos < len(s):
        if s[pos:].strip() == "": break
        m = TOK.match(s, pos)
        if not m: raise HTTPException(400, f"Lỗi cú pháp lọc gần: {s[pos:pos+20]!r}")
        pos = m.end()
        if m.group("str"): out.append(("str", json.loads('"' + m.group("str")[1:-1].replace('"', '\\"') + '"') if m.group("str")[0] == "'" else json.loads(m.group("str"))))
        elif m.group("num"): out.append(("num", float(m.group("num"))))
        elif m.group("id"): out.append(("id", m.group("id")))
        else: out.append(("p", m.group("p")))
    return out

def compile_filter(s: str):
    """Ngôn ngữ lọc kiểu hàm: and(eq(run_type,"llm"), gt(latency,2), has(tags,"x"), search("từ"),
    eq(metadata.agent_id,"1"), gte(feedback.dung,4), eq(status,"error"), in(model,"a","b"), not(...)). Trả (sql, params).
    Grammar: expr := and(e,..) | or(e,..) | not(e) | search("text") | has(tags,"t") | OP(field,value) | like(field,"%x%") | in(field,v1,v2,..);
    OP = eq neq gt gte lt lte. field = name run_type status error model thread_id latency(s) latency_ms total_tokens input_tokens
    output_tokens cost start_time(ISO) trace_id id is_root | metadata.<key> | feedback.<key> (numeric score; OP only). Strings "..." or '...', numbers bare.
    GD7.2: nlsearch.py renders natural-language questions into this DSL (POST /api/obs/nl-filter)."""
    if not s or not s.strip(): return "TRUE", []
    t = _tokens(s); i = 0; params = []
    def peek(): return t[i] if i < len(t) else (None, None)
    def take(kind=None, val=None):
        nonlocal i
        tk = peek()
        if tk[0] is None or (kind and tk[0] != kind) or (val and tk[1] != val): raise HTTPException(400, f"Lọc: mong đợi {val or kind}, gặp {tk[1]!r}")
        i += 1; return tk
    def field(name):
        pth = _path(name)
        if pth:   # GĐ12.16 nested paths: one metadata level keeps `->>`, deeper ones and inputs./outputs. use `#>>` with a text[] parameter
            if pth[0] == "metadata" and len(pth[1]) == 1: params.append(pth[1][0]); return "(metadata->>%s)"
            params.append(pth[1]); return f"({pth[0]} #>> %s::text[])"
        if name.startswith("metadata."): raise HTTPException(400, "khoá metadata không hợp lệ")
        if name.startswith("feedback."):   # feedback.<key> (numeric score) | feedback.<key>.value|comment|source|score
            m = FB_COL_RX.fullmatch(name[9:])
            return ("FB", m.group(1), m.group(2)) if m else ("FB", name[9:], "score")
        if name == "environment":
            import envs; return envs.ENV_SQL
        if name in FIELDS: return FIELDS[name]
        raise HTTPException(400, f"Trường không hỗ trợ: {name}. Dùng: {', '.join(FIELDS)}, tags, metadata.<khoá>, feedback.<khoá>")
    def value():
        tk = take()
        if tk[0] not in ("str", "num"): raise HTTPException(400, "Lọc: giá trị phải là chuỗi hoặc số")
        return tk[1]
    def expr():
        fn = take("id")[1].lower(); take("p", "(")
        if fn in ("and", "or"):
            parts = [expr()]
            while peek() == ("p", ","): take(); parts.append(expr())
            take("p", ")"); return "(" + f" {fn.upper()} ".join(parts) + ")"
        if fn == "not":
            e = expr(); take("p", ")"); return f"(NOT {e})"
        if fn == "search":   # GĐ12.17: search("text") or search("text", "input"|"output"); full-text on search_tsv, ILIKE only for rows not indexed yet
            import aos_crypto, fts; v = value(); scope = "all"
            if peek() == ("p", ","):
                take(); scope = value()
                if scope not in fts.SCOPES[1:]: raise HTTPException(400, 'search(): phạm vi phải là "input" hoặc "output"')
            take("p", ")")
            tq = fts.query(str(v), scope, fts.encrypted_ws(aos_crypto._cur_ws()))
            if tq is None: params.extend([f"%{v}%"] * 3); return "(inputs::text ILIKE %s OR outputs::text ILIKE %s OR coalesce(error,'') ILIKE %s)"
            like = {"all": "(inputs::text ILIKE %s OR outputs::text ILIKE %s OR coalesce(error,'') ILIKE %s)", "input": "(inputs::text ILIKE %s)", "output": "(outputs::text ILIKE %s)"}[scope]
            params.append(tq); params.extend([f"%{v}%"] * like.count("%s"))
            return f"(search_tsv @@ to_tsquery('simple', %s) OR (search_tsv IS NULL AND {like}))"
        if fn == "exists":   # GĐ12.16 exists(metadata.a.b) / exists(inputs.x) / exists(feedback.k[.col]) / exists(<column>)
            fname = take("id")[1]; take("p", ")"); pth = _path(fname)
            if pth: params.append(pth[1]); return f"(({pth[0]} #> %s::text[]) IS NOT NULL)"
            if fname == "tags": return "(cardinality(tags) > 0)"
            if fname == "attachments": return "EXISTS(SELECT 1 FROM run_attachments ra WHERE ra.run_id=tr_runs.id)"
            if fname.startswith("feedback."):
                _, key, col = field(fname); params.append(key)
                return "EXISTS(SELECT 1 FROM feedback fb WHERE fb.run_id=tr_runs.id AND fb.key=%s" + (f" AND fb.{col} IS NOT NULL" if FB_COL_RX.fullmatch(fname[9:]) else "") + ")"
            return f"({field(fname)} IS NOT NULL)"
        if fn == "tool":   # GĐ12.16: the trace holds a tool run with this name (tr_runs_trace index)
            v = value(); take("p", ")"); params.append(str(v))
            return "EXISTS(SELECT 1 FROM tr_runs tc WHERE tc.trace_id=tr_runs.trace_id AND tc.run_type='tool' AND tc.name=%s)"
        if fn == "has":
            f = take("id")[1]
            if f == "attachments": take("p", ")"); return "EXISTS(SELECT 1 FROM run_attachments ra WHERE ra.run_id=tr_runs.id)"
            take("p", ","); v = value(); take("p", ")")
            if f != "tags": raise HTTPException(400, "has() chỉ dùng với tags")
            params.append(str(v)); return "(%s = ANY(tags))"
        if fn in OPS or fn == "in" or fn == "like":
            fname = take("id")[1]; n0 = len(params); f = field(fname); fp = params[n0:]; vals = []
            while peek() == ("p", ","): take(); vals.append(value())
            take("p", ")")
            if not vals: raise HTTPException(400, f"{fn}() thiếu giá trị")
            if isinstance(f, tuple):   # feedback.<key>[.value|comment|source|score]
                _, key, col = f; base = "EXISTS(SELECT 1 FROM feedback fb WHERE fb.run_id=tr_runs.id AND fb.key=%s AND "
                if col == "score":
                    if fn not in OPS: raise HTTPException(400, "feedback.score chỉ hỗ trợ eq/gt/gte/lt/lte/neq")
                    try: sc = float(vals[0])
                    except (TypeError, ValueError): raise HTTPException(400, "feedback.score cần giá trị số") from None
                    params.extend([key, sc]); return base + f"fb.score {OPS[fn]} %s)"
                if fn in OPS: params.extend([key, str(vals[0])]); return base + f"fb.{col} {OPS[fn]} %s)"
                if fn == "like": params.extend([key, str(vals[0])]); return base + f"fb.{col} ILIKE %s)"
                params.extend([key, *(str(v) for v in vals)]); return base + f"fb.{col} IN (" + ",".join(["%s"] * len(vals)) + "))"
            numeric = fname in NUMERIC_FIELDS
            cast = (lambda x: float(x)) if numeric else (lambda x: str(x))
            if _path(fname):   # a JSON text value: 5.0 -> '5'; gt/gte/lt/lte with a number compare numerically (non-numeric texts never match)
                cast = lambda x: str(int(x)) if isinstance(x, float) and x == int(x) else str(x)
                if fn in ("gt", "gte", "lt", "lte") and isinstance(vals[0], float):
                    params.extend(fp); params.append(float(vals[0]))
                    return f"(CASE WHEN {f} ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN {f}::numeric END {OPS[fn]} %s)"
            if fname in ("start_time", "first_token_time"): cast = lambda x: str(x)
            if fn == "in":
                params.extend(cast(v) for v in vals); return f"({f} IN (" + ",".join(["%s"] * len(vals)) + "))"
            if fn == "like":
                params.append(str(vals[0])); return f"({f}::text ILIKE %s)"
            params.append(cast(vals[0]))
            if fname == "is_root": return f"({f} = %s::boolean)"
            if fname in ("start_time", "first_token_time"): return f"({f} {OPS[fn]} %s::timestamptz)"
            return f"({f} {OPS[fn]} %s)"
        raise HTTPException(400, f"Hàm lọc không hỗ trợ: {fn}")
    sql = expr()
    if i != len(t): raise HTTPException(400, "Lọc: thừa ký tự ở cuối")
    return sql, params

def scoped_filter(filter_="", trace_filter="", tree_filter="", project_id=None):
    """GĐ12.16: (sql, params) of `filter_` AND [the ROOT run of the run's trace matches trace_filter] AND [at least one run of the run's tree matches tree_filter].
    The two scopes are uncorrelated `trace_id IN (SELECT ...)` subqueries over tr_runs (RLS applies inside them as well)."""
    sql, params = compile_filter(filter_); parts, ps = [f"({sql})"], list(params)
    for text, root in ((trace_filter, True), (tree_filter, False)):
        if text and text.strip():
            s, p = compile_filter(text)
            parts.append("trace_id IN (SELECT trace_id FROM tr_runs WHERE project_id=%s" + (" AND parent_id IS NULL" if root else "") + f" AND {s})"); ps += [project_id, *p]
    return " AND ".join(parts), ps

# ------------------------------------------------------------------ GĐ12.18 retention tiers (base 30 / extended 180 days) + upgrade of rated traces
RET_BATCH = 5000          # never delete more than this many runs in one statement
RET_MAX_BATCHES = 20      # ... nor more than RET_BATCH * RET_MAX_BATCHES runs in one worker tick (the next tick continues)
RET_EXTEND_SOURCES = {"human": "feedback", "sdk": "feedback", "online": "evaluator"}   # feedback sources that upgrade a trace; experiments / dataset runs / thread scores / imports do not

def _ret_admin():
    """Changing a retention policy can delete data: workspace admin only, never a demo account."""
    import admin; u = admin.cur()
    if not u or u.get("role") != "admin" or u.get("demo"): raise HTTPException(403, "Chỉ quản trị workspace đổi được chính sách lưu trữ / Only a workspace admin can change retention")
    return u

def _ret_editor():
    import admin; u = admin.cur()
    if not u or u.get("role") not in ("admin", "editor") or u.get("demo"): raise HTTPException(403, "Cần quyền editor trở lên / Editor role or higher required")
    return u

def _days(tier, custom, base, ext):
    return custom if tier == "custom" else ext if tier == "extended" else base

async def retention_policy(pid):
    """Effective retention of a project: {project_id, workspace_id, tier (base|extended|custom), inherited (follows the workspace default), days, ws_default, base_days,
    extended_days}; None when the project is not visible to the caller (another workspace)."""
    r = await A.q("""SELECT p.workspace_id ws, p.retention_tier t, p.retention_days d, w.retention_default df, w.retention_base_days b, w.retention_extended_days e
                     FROM projects p JOIN workspaces w ON w.id=p.workspace_id WHERE p.id=%s""", (pid,), one=True)
    if not r: return None
    tier = r["t"] or r["df"]
    return {"project_id": pid, "workspace_id": r["ws"], "tier": tier, "inherited": r["t"] is None, "days": _days(tier, r["d"], r["b"], r["e"]),
            "ws_default": r["df"], "base_days": r["b"], "extended_days": r["e"]}

async def extend_trace(trace_id, reason, by=None):
    """Keep a trace for the extended period (default 180 days from its start), whatever its runs' ttl_days say. Idempotent: a second call only moves keep_until later.
    Returns {trace_id, keep_until, created}, or None when the trace is not visible to the caller (another workspace / another user's private chat) or is in the demo workspace."""
    if reason not in ("feedback", "evaluator", "rule", "manual"): raise ValueError(f"bad retention reason: {reason}")
    t = await A.q("SELECT project_id, min(start_time) st FROM tr_runs WHERE trace_id=%s GROUP BY project_id", (str(trace_id),), one=True)
    if not t: return None
    pol = await retention_policy(t["project_id"])
    if not pol or pol["workspace_id"] in A.gw.DEMO_WS: return None   # new features are OFF for the demo workspace
    keep = t["st"] + dt.timedelta(days=pol["extended_days"])
    row = await A.q("""INSERT INTO trace_retention(trace_id,project_id,keep_until,reason,set_by) VALUES(%s,%s,%s,%s,%s)
        ON CONFLICT(trace_id) DO UPDATE SET keep_until=GREATEST(trace_retention.keep_until, EXCLUDED.keep_until)
        RETURNING trace_id, keep_until, (xmax = 0) AS created""", (str(trace_id), t["project_id"], keep, reason, by), one=True)
    return {"trace_id": str(row["trace_id"]), "keep_until": row["keep_until"], "created": row["created"]}

@router.post("/api/obs/traces/{trace_id}/extend")
async def trace_extend(trace_id: str):
    """Keep this trace for the extended period (editor or admin)."""
    u = _ret_editor()
    try: uuid.UUID(trace_id)
    except ValueError: raise HTTPException(422, "trace_id không hợp lệ") from None
    r = await extend_trace(trace_id, "manual", by=u.get("username"))
    if not r: raise HTTPException(404, "Không có trace này")
    return r

_KEPT = "EXISTS (SELECT 1 FROM trace_retention k WHERE k.trace_id = r.trace_id AND k.keep_until > %(now)s)"
_EXPIRED = "r.start_time < %(now)s - make_interval(days => coalesce(r.ttl_days, p.legacy_ttl_days, p.retention_days, 30)) AND NOT " + _KEPT

async def retention(now=None, project_ids=None, dry_run=False, max_batches=None, only_partitions=None):
    """Worker job (hourly). Deletes a run only when start_time < now - its ttl_days (ttl_days NULL = a run from before 12.18: the project's legacy days) AND its trace is not
    listed in trace_retention with keep_until in the future; its feedback and attachments go with it. At most RET_BATCH rows per statement, RET_MAX_BATCHES statements per call.
    A monthly partition is dropped only when EVERY run in it is already expired and none belongs to a kept trace (it is skipped otherwise).
    Expired trace_retention rows (and rows whose trace has no run left) are removed afterwards.
    now / project_ids / only_partitions / dry_run exist for tests and previews; dry_run changes nothing and returns what would go. -> {runs, feedback, attachments, partitions, dry_run}"""
    now = now or dt.datetime.now(dt.timezone.utc); pids = list(project_ids) if project_ids is not None else None
    a = {"now": now, "pids": pids}; scope = "(%(pids)s::int[] IS NULL OR r.project_id = ANY(%(pids)s::int[]))"
    frm = "FROM tr_runs r LEFT JOIN projects p ON p.id=r.project_id"
    out = {"runs": 0, "feedback": 0, "attachments": 0, "partitions": [], "dry_run": bool(dry_run)}
    if dry_run:
        out["runs"] = (await A.q(f"SELECT count(*) n {frm} WHERE {scope} AND {_EXPIRED}", a, one=True))["n"]
    else:
        for _ in range(max_batches or RET_MAX_BATCHES):
            rows = await A.q(f"SELECT r.id, r.start_time, r.project_id {frm} WHERE {scope} AND {_EXPIRED} ORDER BY r.start_time LIMIT {RET_BATCH}", a)
            if not rows: break
            await __import__("stats_rollup").ensure_hours([(r["project_id"], r["start_time"]) for r in rows])   # GĐ12.9: roll the hours up BEFORE their runs are deleted (an error stops the deletion)
            ids = [r["id"] for r in rows]
            out["feedback"] += len(await A.q("DELETE FROM feedback WHERE run_id = ANY(%s) RETURNING 1", (ids,)) or [])
            out["attachments"] += len(await A.q("DELETE FROM run_attachments WHERE run_id = ANY(%s) RETURNING 1", (ids,)) or [])
            await A.q("DELETE FROM tr_runs WHERE (id, start_time) IN (SELECT * FROM unnest(%s::uuid[], %s::timestamptz[]))", (ids, [r["start_time"] for r in rows]))
            out["runs"] += len(rows)
            if len(rows) < RET_BATCH: break
        await A.q("""DELETE FROM trace_retention k WHERE (%(pids)s::int[] IS NULL OR k.project_id = ANY(%(pids)s::int[])) AND
            (k.keep_until <= %(now)s OR NOT EXISTS (SELECT 1 FROM tr_runs r WHERE r.trace_id = k.trace_id))""", a)
    for part in await A.q("SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid=i.inhrelid JOIN pg_class pp ON pp.oid=i.inhparent WHERE pp.relname='tr_runs' AND c.relname ~ '^tr_runs_[0-9]{6}$' ORDER BY 1"):
        rel = part["relname"]
        if only_partitions is not None and rel not in only_partitions: continue
        y, m = int(rel[8:12]), int(rel[12:14]); end = dt.date(y + m // 12, m % 12 + 1, 1)
        if end > (now - dt.timedelta(days=31)).date(): continue   # ended less than 31 days ago: never touched (ensure_partitions still manages the recent months; no create / drop churn)
        if pids is not None and await A.q(f"SELECT 1 FROM {rel} r WHERE NOT (r.project_id = ANY(%s::int[])) LIMIT 1", (pids,)): continue   # a scoped call never drops other projects' data
        if await A.q(f"SELECT 1 FROM {rel} r LEFT JOIN projects p ON p.id=r.project_id WHERE NOT ({_EXPIRED}) LIMIT 1", a): continue   # still holds a live run or a kept trace: skip
        if not dry_run:
            await __import__("stats_rollup").ensure_partition(rel)   # GĐ12.9
            out["feedback"] += len(await A.q(f"DELETE FROM feedback WHERE run_id IN (SELECT id FROM {rel}) RETURNING 1") or [])
            out["attachments"] += len(await A.q(f"DELETE FROM run_attachments WHERE run_id IN (SELECT id FROM {rel}) RETURNING 1") or [])
            await A.q(f"DROP TABLE {rel}")
        out["partitions"].append(rel)
    return out

async def _restamp(pid, days, max_batches=200):
    """Set ttl_days = days on the project's runs (RET_BATCH rows per statement). -> (rows updated, finished)."""
    n = 0
    for _ in range(max_batches):
        k = len(await A.q(f"WITH b AS (SELECT id, start_time FROM tr_runs WHERE project_id=%s AND ttl_days IS DISTINCT FROM %s LIMIT {RET_BATCH}) "
                          "UPDATE tr_runs t SET ttl_days=%s FROM b WHERE t.id=b.id AND t.start_time=b.start_time RETURNING 1", (pid, days, days)) or [])
        n += k
        if k < RET_BATCH: return n, True
    return n, False

async def _apply_existing(pid, days, confirm):
    """apply_to_existing: without confirm only counts (dry run: nothing is changed, not even the policy); with confirm re-stamps ttl_days of every run of the project
    (and legacy_ttl_days, the days of runs written before 12.18). Kept traces stay kept either way."""
    now = dt.datetime.now(dt.timezone.utc)
    if not confirm:
        c = await A.q("""SELECT count(*) FILTER (WHERE r.ttl_days IS DISTINCT FROM %(d)s) to_change,
            count(*) FILTER (WHERE r.start_time < %(now)s - make_interval(days => %(d)s) AND NOT """ + _KEPT + """) would_expire
            FROM tr_runs r WHERE r.project_id=%(pid)s""", {"d": days, "now": now, "pid": pid}, one=True)
        kept = (await A.q("SELECT count(*) n FROM trace_retention WHERE project_id=%s AND keep_until > %s", (pid, now), one=True))["n"]
        return {"dry_run": True, "project_id": pid, "days": days, "runs_to_change": c["to_change"], "runs_deleted_by_next_cleanup": c["would_expire"], "kept_traces_protected": kept,
                "note": "Nothing was changed. Send confirm=true to apply. / Chưa thay đổi gì; gửi confirm=true để áp dụng."}
    await A.q("UPDATE projects SET legacy_ttl_days=%s WHERE id=%s", (days, pid))
    n, done = await _restamp(pid, days)
    return {"dry_run": False, "project_id": pid, "days": days, "runs_updated": n, "finished": done}

class RetentionIn(BaseModel):
    default: Literal["base", "extended"] | None = None
    base_days: int | None = Field(default=None, ge=1, le=3650)
    extended_days: int | None = Field(default=None, ge=1, le=3650)
    apply_to_existing: bool = False
    confirm: bool = False

@router.get("/api/obs/retention")
async def retention_get():
    """Workspace retention policy + how many traces are currently kept (extended)."""
    w = await A.q("SELECT retention_default, retention_base_days, retention_extended_days FROM workspaces WHERE id=%s", (_cws(),), one=True)
    kept = (await A.q("SELECT count(*) n FROM trace_retention WHERE keep_until > now()", one=True))["n"]
    return {"default": w["retention_default"], "base_days": w["retention_base_days"], "extended_days": w["retention_extended_days"], "kept_traces": kept}

@router.put("/api/obs/retention")
async def retention_put(b: RetentionIn):
    """Admin: workspace default tier and the days of the base / extended tiers. New runs only, unless apply_to_existing=true (then a dry run first; confirm=true applies)."""
    _ret_admin(); ws = _cws()
    if ws in A.gw.DEMO_WS: raise HTTPException(403, "Không dùng được cho workspace demo")
    w = await A.q("SELECT retention_default, retention_base_days, retention_extended_days FROM workspaces WHERE id=%s", (ws,), one=True)
    new = {"retention_default": b.default or w["retention_default"], "retention_base_days": b.base_days or w["retention_base_days"], "retention_extended_days": b.extended_days or w["retention_extended_days"]}
    if new["retention_extended_days"] < new["retention_base_days"]: raise HTTPException(422, "extended_days phải >= base_days")
    ps = await A.q("SELECT id, retention_tier t, retention_days d FROM projects WHERE workspace_id=%s AND coalesce(retention_tier,'') <> 'custom' ORDER BY id", (ws,))
    plan = {p["id"]: _days(p["t"] or new["retention_default"], p["d"], new["retention_base_days"], new["retention_extended_days"]) for p in ps}
    if b.apply_to_existing and not b.confirm:
        return {"dry_run": True, "projects": [await _apply_existing(pid, days, False) for pid, days in plan.items()]}
    await A.q("UPDATE workspaces SET retention_default=%s, retention_base_days=%s, retention_extended_days=%s WHERE id=%s", (*new.values(), ws))
    out = {"ok": True, **{k.replace("retention_", ""): v for k, v in new.items()}, "applies_to": "new runs only"}
    if b.apply_to_existing:
        out["projects"] = [await _apply_existing(pid, days, True) for pid, days in plan.items()]; out["applies_to"] = "new and existing runs"
    return out

# ------------------------------------------------------------------ API project / run
class ProjectIn(BaseModel):
    name: str = Field(pattern=r"^[\w\-. ]{1,80}$")
    description: str | None = None
    retention_days: int | None = Field(default=None, ge=1, le=3650)   # GĐ12.18: given -> tier 'custom' with these days; absent -> follow the workspace default tier (admin only)
    retention_tier: Literal["base", "extended", "custom"] | None = None
    environment: str | None = None   # GĐ9.10 dev | staging | prod | custom label (default prod)

@router.get("/api/obs/projects")
async def projects(environment: str = ""):
    import envs
    ev = envs.norm_http(environment) if environment else None
    rows = await A.q("""SELECT p.*, (SELECT count(*) FROM tr_runs r WHERE r.project_id=p.id AND r.parent_id IS NULL) traces,
        (SELECT count(*) FROM tr_runs r WHERE r.project_id=p.id AND r.parent_id IS NULL AND r.start_time > now()-interval '24 hours') traces_24h,
        (SELECT count(*) FROM tr_runs r WHERE r.project_id=p.id AND r.parent_id IS NULL AND r.status='error' AND r.start_time > now()-interval '24 hours') errors_24h,
        (SELECT count(*) FROM trace_retention k WHERE k.project_id=p.id AND k.keep_until > now()) traces_extended,
        (SELECT max(start_time) FROM tr_runs r WHERE r.project_id=p.id) last_run FROM projects p WHERE p.workspace_id=%s AND (%s::text IS NULL OR p.environment=%s) ORDER BY p.id""", (_cws(), ev, ev))
    w = await A.q("SELECT retention_default df, retention_base_days b, retention_extended_days e FROM workspaces WHERE id=%s", (_cws(),), one=True)
    for r in rows:   # GĐ12.18: effective policy per project (tier, days, whether it follows the workspace default)
        t = r.get("retention_tier") or w["df"]
        r["retention"] = {"tier": t, "inherited": r.get("retention_tier") is None, "days": _days(t, r["retention_days"], w["b"], w["e"])}
    return rows

@router.post("/api/obs/projects")
async def project_new(p: ProjectIn):
    import envs
    tier = p.retention_tier or ("custom" if p.retention_days is not None else None)
    if tier is not None or p.retention_days is not None:
        _ret_admin()
        if tier == "custom" and p.retention_days is None: raise HTTPException(422, "retention_tier=custom cần retention_days")
        if tier != "custom" and p.retention_days is not None: raise HTTPException(422, "retention_days chỉ dùng với retention_tier=custom")
    return await A.q("INSERT INTO projects(name,description,retention_days,retention_tier,workspace_id,environment) VALUES(%s,%s,%s,%s,%s,%s) RETURNING *",
                     (p.name, p.description, p.retention_days or 30, tier, _cws(), envs.norm_http(p.environment, "prod")), one=True)

class ProjectUpd(BaseModel):
    description: str | None = None
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    retention_tier: Literal["base", "extended", "custom", "inherit"] | None = None   # GĐ12.18 (inherit = follow the workspace default)
    apply_to_existing: bool = False   # GĐ12.18: also re-stamp the runs already stored; answers with a dry-run count until confirm=true
    confirm: bool = False
    environment: str | None = None

@router.patch("/api/obs/projects/{pid}")
async def project_upd(pid: int, p: ProjectUpd):
    out = {"ok": True}
    if p.retention_days is not None or p.retention_tier is not None or p.apply_to_existing:   # GĐ12.18: retention can delete data -> admin only, own workspace only
        _ret_admin()
        pol = await retention_policy(pid)
        if not pol: raise HTTPException(404, "Không có dự án này")
        if pol["workspace_id"] in A.gw.DEMO_WS: raise HTTPException(403, "Không dùng được cho workspace demo")
        cur = await A.q("SELECT retention_tier t, retention_days d FROM projects WHERE id=%s", (pid,), one=True)
        tier = p.retention_tier or ("custom" if p.retention_days is not None else (cur["t"] or "inherit"))
        if tier != "custom" and p.retention_days is not None: raise HTTPException(422, "retention_days chỉ dùng với retention_tier=custom")
        custom = p.retention_days or cur["d"]
        days = _days(pol["ws_default"] if tier == "inherit" else tier, custom, pol["base_days"], pol["extended_days"])
        if p.apply_to_existing and not p.confirm: return await _apply_existing(pid, days, False)   # dry run: nothing is changed
        await A.q("UPDATE projects SET retention_tier=%s, retention_days=%s WHERE id=%s", (None if tier == "inherit" else tier, custom, pid))
        out.update(tier=None if tier == "inherit" else tier, days=days, applies_to="new runs only")
        if p.apply_to_existing: out.update(await _apply_existing(pid, days, True)); out["applies_to"] = "new and existing runs"
    if p.description is not None: await A.q("UPDATE projects SET description=%s WHERE id=%s", (p.description, pid))
    if p.environment:
        import envs; await A.q("UPDATE projects SET environment=%s WHERE id=%s", (envs.norm_http(p.environment), pid))
    return out

@router.post("/api/obs/projects/{pid}/keys")
async def key_new(pid: int, label: str = "", environment: str = ""):
    import envs
    k = "ak_" + os.urandom(18).hex()   # GĐ9.10: environment = default env stamped on runs sent with this key (empty = the project's)
    await A.q("INSERT INTO ingest_keys(project_id,key_hash,prefix,label,environment) VALUES(%s,%s,%s,%s,%s)", (pid, _h(k), k[:8], label or None, envs.norm_http(environment)))
    return {"api_key": k, "note": "Chỉ hiện một lần. Server lưu SHA-256."}

@router.get("/api/obs/projects/{pid}/keys")
async def key_list(pid: int):
    return await A.q("SELECT id,prefix,label,created,revoked,environment FROM ingest_keys WHERE project_id=%s ORDER BY id", (pid,))

@router.delete("/api/obs/keys/{kid}")
async def key_revoke(kid: int):
    await A.q("UPDATE ingest_keys SET revoked=true WHERE id=%s", (kid,)); return {"ok": True}

SORTS = {"newest": "start_time DESC", "oldest": "start_time ASC", "slowest": "latency_ms DESC NULLS LAST, start_time DESC",
         "costliest": "cost_usd DESC NULLS LAST, start_time DESC", "most_tokens": "total_tokens DESC NULLS LAST, start_time DESC"}   # GD7.2 (natural-language search can ask for a sort)
@router.get("/api/obs/subagents")
async def subagent_names(project_id: int):
    """GĐ11.63: distinct sub-agent names (metadata.lc_agent_name) seen in a project of the current workspace; feeds the 'Sub-agent' filter of the trace page."""
    if not await A.q("SELECT 1 FROM projects WHERE id=%s AND workspace_id=%s", (project_id, _cws()), one=True): raise HTTPException(404, "Không có dự án này")
    rows = await A.q("SELECT DISTINCT metadata->>'lc_agent_name' n FROM tr_runs WHERE project_id=%s AND metadata->>'lc_agent_name' IS NOT NULL AND start_time > now()-interval '30 days' ORDER BY 1 LIMIT 200", (project_id,))
    return [r["n"] for r in rows if r["n"]]

@router.get("/api/obs/runs")
async def runs(project_id: int, filter: str = "", root_only: bool = True, limit: int = 100, offset: int = 0,
               since_hours: float | None = None, owner_id: int | None = None, sort: str = "newest", lc_agent_name: str = "", query: str = ""):
    import envs, query_syntax
    sql, params = compile_filter(query_syntax.both(filter, query))   # GĐ12.16: `query` = text like `status:error -tag:test latency:[2 TO 10] báo cáo`
    order = SORTS.get(sort)
    if not order: raise HTTPException(400, f"sort phải là một trong: {', '.join(SORTS)}")
    w = ["project_id=%s"]; a = [project_id]
    if owner_id: w.append("user_id=%s"); a.append(owner_id)   # GĐ6.10 "Của ai" (RLS already hides other users' chat traces from non-admins)
    if lc_agent_name: w.append("metadata->>'lc_agent_name' = %s"); a.append(lc_agent_name[:120]); root_only = False   # GĐ11.63 "Sub-agent" filter: runs made inside that sub-agent
    if root_only: w.append("parent_id IS NULL")
    if since_hours: w.append("start_time > now() - %s * interval '1 hour'"); a.append(since_hours)
    rows = await A.q(f"""SELECT id, trace_id, parent_id, name, run_type, start_time, end_time, latency_ms, status, left(error,200) error,
        left(inputs::text, 300) inputs, left(outputs::text, 300) outputs, tags, metadata, total_tokens, cost_usd, model, thread_id, user_id,
        first_token_time, usage_details, cost_details, input_cost, output_cost, reference_example_id, depth, ttl_days,
        (SELECT username FROM users u WHERE u.id=tr_runs.user_id) owner, {envs.ENV_SQL} environment,
        (SELECT json_agg(json_build_object('key',key,'score',score,'source',source)) FROM feedback f WHERE f.run_id=tr_runs.id) feedback
        FROM tr_runs WHERE {' AND '.join(w)} AND {sql} ORDER BY {order} LIMIT %s OFFSET %s""",
                     (*a, *params, min(limit, 500), offset))
    return rows

@router.get("/api/obs/search/info")
async def search_info():
    """GĐ12.17: how the trace search works for the signed-in workspace (encrypted traces: whole words only) - the Tracing page shows `message` under the search box."""
    import aos_crypto, fts
    return fts.info(aos_crypto._cur_ws())

# ---- GĐ12.16 query v2: POST /api/obs/runs/query (keyset paging), GET /api/obs/traces/{id}/runs, GET .../threads/{tid}/traces
QUERY_COLS = ("id", "trace_id", "parent_id", "name", "run_type", "start_time", "end_time", "latency_ms", "status", "error", "inputs", "outputs", "tags", "metadata", "total_tokens",
              "input_tokens", "output_tokens", "cost_usd", "model", "thread_id", "user_id", "first_token_time", "usage_details", "cost_details", "input_cost", "output_cost",
              "reference_example_id", "depth", "ttl_days", "dotted_order", "environment", "feedback")
QUERY_DEFAULT = ("id", "trace_id", "parent_id", "name", "run_type", "start_time", "end_time", "latency_ms", "status", "total_tokens", "cost_usd", "model", "thread_id", "depth")

def _select_cols(select):
    """Column list for SELECT from an allow-list (unknown names -> 400). id and start_time are always returned (the paging cursor needs them)."""
    import envs
    want = list(dict.fromkeys([*(select or QUERY_DEFAULT), "id", "start_time"]))
    bad = [c for c in want if c not in QUERY_COLS]
    if bad: raise HTTPException(400, f"select không hỗ trợ: {', '.join(bad[:5])}. Dùng: {', '.join(QUERY_COLS)}")
    ex = {"environment": f"{envs.ENV_SQL} environment", "feedback": "(SELECT json_agg(json_build_object('key',key,'score',score,'value',value,'source',source)) FROM feedback f WHERE f.run_id=tr_runs.id) feedback"}
    return ", ".join(ex.get(c, c) for c in want)

def _cursor_enc(ts, rid):
    import base64
    return base64.urlsafe_b64encode(json.dumps([ts.isoformat(), str(rid)]).encode()).decode().rstrip("=")

def _cursor_dec(c):
    import base64
    try:
        ts, rid = json.loads(base64.urlsafe_b64decode(c + "=" * (-len(c) % 4)))
        return dt.datetime.fromisoformat(ts), str(uuid.UUID(rid))
    except Exception: raise HTTPException(400, "cursor không hợp lệ / invalid cursor") from None   # noqa: BLE001

class QueryIn(BaseModel):
    project_id: int
    filter: str = ""
    query: str = ""
    trace_filter: str = ""
    tree_filter: str = ""
    select: list[str] = Field(default_factory=list)
    cursor: str | None = None
    limit: int = Field(default=100, ge=1, le=500)
    order: Literal["newest", "oldest"] = "newest"
    root_only: bool | None = None   # None: roots only when tree_filter is given (the answer is "the traces that contain ..."), otherwise every run
    since_hours: float | None = None

@router.post("/api/obs/runs/query")
async def runs_query(b: QueryIn):
    import query_syntax
    sql, params = scoped_filter(query_syntax.both(b.filter, b.query), b.trace_filter, b.tree_filter, b.project_id)
    cols = _select_cols(b.select); desc = b.order == "newest"; d = "DESC" if desc else "ASC"
    w, a = ["project_id=%s"], [b.project_id]
    if b.root_only if b.root_only is not None else bool(b.tree_filter.strip()): w.append("parent_id IS NULL")
    if b.since_hours: w.append("start_time > now() - %s * interval '1 hour'"); a.append(b.since_hours)
    if b.cursor: ts, rid = _cursor_dec(b.cursor); w.append(f"(start_time, id) {'<' if desc else '>'} (%s::timestamptz, %s::uuid)"); a += [ts, rid]
    rows = await A.q(f"SELECT {cols} FROM tr_runs WHERE {' AND '.join(w)} AND {sql} ORDER BY start_time {d}, id {d} LIMIT %s", (*a, *params, b.limit + 1))
    more = len(rows) > b.limit; rows = rows[:b.limit]
    return {"runs": rows, "next_cursor": _cursor_enc(rows[-1]["start_time"], rows[-1]["id"]) if more else None}

@router.get("/api/obs/traces/{trace_id}/runs")
async def trace_runs(trace_id: str, filter: str = "", query: str = "", select: str = "", max_runs: int = 1000):
    import query_syntax
    try: tid = str(uuid.UUID(trace_id))
    except ValueError: raise HTTPException(404) from None
    sql, params = compile_filter(query_syntax.both(filter, query)); cols = _select_cols([c.strip() for c in select.split(",") if c.strip()])
    rows = await A.q(f"SELECT {cols} FROM tr_runs WHERE trace_id=%s AND {sql} ORDER BY start_time, dotted_order NULLS LAST LIMIT %s", (tid, *params, min(max(max_runs, 1), 1000)))
    if not rows and not await A.q("SELECT 1 FROM tr_runs WHERE trace_id=%s LIMIT 1", (tid,), one=True): raise HTTPException(404)
    return {"runs": rows}

@router.get("/api/obs/projects/{pid}/threads/{tid}/traces")
async def thread_traces(pid: int, tid: str, limit: int = 200):
    """The turns (root runs) of one conversation, oldest first, each with the number of runs in its trace.
    GĐ12.100c: a project the caller cannot see (another workspace / unknown) = 404. It used to answer 200 and echo the caller-supplied thread id for ANY project id,
    so a thread uuid of another workspace showed up in a demo response (demo leak audit)."""
    if not await A.q("SELECT 1 FROM projects WHERE id=%s", (pid,), one=True): raise HTTPException(404)   # projects has RLS: only the signed-in workspace's projects are visible
    rows = await A.q("""SELECT r.trace_id, r.id, r.name, r.start_time, r.latency_ms, r.status, r.total_tokens, r.cost_usd,
        (SELECT count(*) FROM tr_runs c WHERE c.trace_id=r.trace_id) n_runs FROM tr_runs r
        WHERE r.project_id=%s AND r.parent_id IS NULL AND r.thread_id=%s ORDER BY r.start_time, r.id LIMIT %s""", (pid, tid, min(max(limit, 1), 1000)))
    n = await A.q("SELECT count(*) n FROM tr_runs WHERE project_id=%s AND parent_id IS NULL AND thread_id=%s", (pid, tid), one=True)
    return {"thread_id": tid, "turns": n["n"], "traces": rows}

TRACE_VIEW_MAX = 1000

@router.get("/api/obs/traces/{trace_id}")
async def trace(trace_id: str):
    import envs
    rows = await A.q(f"SELECT *, {envs.ENV_SQL} environment FROM tr_runs WHERE trace_id=%s ORDER BY start_time, dotted_order NULLS LAST LIMIT {TRACE_VIEW_MAX + 1}", (trace_id,))
    if not rows: raise HTTPException(404)
    truncated = len(rows) > TRACE_VIEW_MAX   # GĐ12.15: at most 1000 runs per answer
    rows = rows[:TRACE_VIEW_MAX]
    fb = await A.q("SELECT * FROM feedback WHERE trace_id=%s OR run_id = ANY(%s) ORDER BY created", (trace_id, [r["id"] for r in rows]))
    return {"runs": rows, "feedback": fb, "truncated": truncated}

@router.get("/api/obs/projects/{pid}/threads")
async def threads(pid: int, limit: int = 100, owner_id: int | None = None, filter: str = "", since_hours: float | None = None, query: str = ""):
    import query_syntax
    sql, params = compile_filter(query_syntax.both(filter, query))   # GD7.2: a thread is listed when at least one of its root runs matches
    return await A.q("""SELECT thread_id, max(user_id) user_id, (SELECT username FROM users u WHERE u.id=max(tr_runs.user_id)) owner, count(*) turns, min(start_time) first, max(start_time) last, sum(total_tokens) tokens, sum(cost_usd) cost,
        count(*) FILTER (WHERE status='error') errors, (array_agg(left(inputs::text,300) ORDER BY start_time DESC))[1] last_input,
        (array_agg(left(inputs::text,300) ORDER BY start_time))[1] first_input, (array_agg(left(outputs::text,300) ORDER BY start_time DESC))[1] last_output,
        percentile_cont(.5) WITHIN GROUP (ORDER BY latency_ms) p50, percentile_cont(.99) WITHIN GROUP (ORDER BY latency_ms) p99
        FROM tr_runs WHERE project_id=%s AND parent_id IS NULL AND thread_id IS NOT NULL AND (%s::int IS NULL OR user_id=%s) AND (%s::float IS NULL OR start_time > now() - %s * interval '1 hour') AND """ + sql + """ GROUP BY thread_id ORDER BY max(start_time) DESC LIMIT %s""", (pid, owner_id, owner_id, since_hours, since_hours, *params, limit))

@router.get("/api/obs/projects/{pid}/threads/{tid}")
async def thread(pid: int, tid: str):
    return await A.q("SELECT id, trace_id, name, start_time, latency_ms, status, inputs, outputs, error, total_tokens, cost_usd FROM tr_runs WHERE project_id=%s AND parent_id IS NULL AND thread_id=%s ORDER BY start_time", (pid, tid))

@router.get("/api/obs/projects/{pid}/dashboard")
async def dashboard(pid: int, hours: int = 24, bucket: str = "hour", filter: str = ""):
    if bucket not in ("minute", "hour", "day"): raise HTTPException(400)
    sql, params = compile_filter(filter)
    series = await A.q(f"""SELECT date_trunc('{bucket}', start_time AT TIME ZONE 'Asia/Ho_Chi_Minh') t, count(*) n,
        count(*) FILTER (WHERE status='error') errors, percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) p50,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) p95, coalesce(sum(total_tokens),0) tokens, coalesce(sum(cost_usd),0) cost
        FROM tr_runs WHERE project_id=%s AND parent_id IS NULL AND start_time > now() - %s * interval '1 hour' AND {sql} GROUP BY 1 ORDER BY 1""",
                       (pid, hours, *params))
    llm = await A.q("""SELECT model, count(*) calls, count(*) FILTER (WHERE status='error') errors, percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) p50,
        coalesce(sum(total_tokens),0) tokens, coalesce(sum(cost_usd),0) cost FROM tr_runs WHERE project_id=%s AND run_type='llm'
        AND start_time > now() - %s * interval '1 hour' GROUP BY model ORDER BY calls DESC""", (pid, hours))
    tools = await A.q("""SELECT name, count(*) calls, count(*) FILTER (WHERE status='error') errors, percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) p50
        FROM tr_runs WHERE project_id=%s AND run_type='tool' AND start_time > now() - %s * interval '1 hour' GROUP BY name ORDER BY calls DESC LIMIT 20""", (pid, hours))
    fbk = await A.q("""SELECT f.key, count(*) n, avg(f.score) avg FROM feedback f JOIN tr_runs r ON r.id=f.run_id WHERE r.project_id=%s
        AND f.created > now() - %s * interval '1 hour' GROUP BY f.key ORDER BY n DESC""", (pid, hours))
    tot = await A.q(f"""SELECT count(*) n, count(*) FILTER (WHERE status='error') errors, percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) p50,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) p95, coalesce(sum(total_tokens),0) tokens, coalesce(sum(cost_usd),0) cost
        FROM tr_runs WHERE project_id=%s AND parent_id IS NULL AND start_time > now() - %s * interval '1 hour' AND {sql}""", (pid, hours, *params), one=True)
    return {"series": series, "llm": llm, "tools": tools, "feedback": fbk, "total": tot}

async def add_feedback(f: FbIn, source="human", evaluator_id=None, experiment_id=None, by=None, extend=True):
    r = await A.q("SELECT id, trace_id, project_id FROM tr_runs WHERE id::text=%s", (f.run_id,), one=True)
    if not r:   # G14: accept an /api/v1 (lg_runs) run id and map it to its trace server-side
        lg = await A.q("SELECT trace_id FROM lg_runs WHERE run_id::text=%s AND trace_id IS NOT NULL", (f.run_id,), one=True)
        if lg: r = await A.q("SELECT id, trace_id, project_id FROM tr_runs WHERE id=%s", (lg["trace_id"],), one=True)
    if not r: raise HTTPException(404, "Không có run")
    f = f.model_copy(update={"run_id": str(r["id"])})
    row = await A.q("INSERT INTO feedback(run_id,trace_id,project_id,key,score,value,comment,correction,metadata,source,evaluator_id,experiment_id,created_by) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (f.run_id, r["trace_id"], r["project_id"], f.key, f.score, f.value, f.comment, Jsonb(f.correction) if f.correction else None,
                     Jsonb(f.metadata) if f.metadata else None, source, evaluator_id, experiment_id, by), one=True)
    if extend and source in RET_EXTEND_SOURCES:   # GĐ12.18: a rated trace is worth keeping longer (human / SDK feedback, online evaluator scores)
        try: await extend_trace(r["trace_id"], RET_EXTEND_SOURCES[source], by=by)
        except Exception as e: print("extend_trace failed:", str(e)[:200])   # noqa: BLE001  (the feedback itself is saved)
    return {"id": row["id"]}

@router.post("/api/obs/feedback")
async def feedback(f: FbIn):
    import admin   # G8: the signed-in user, not a hard-coded name
    return await add_feedback(f, "human", by=(admin.cur() or {}).get("username") or "unknown")

# ------------------------------------------------------------------ chia sẻ trace
def _sign(sid, exp): return hmac.new(SECRET.encode(), f"{sid}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]

class ShareIn(BaseModel):
    trace_id: str
    hours: int = Field(default=72, ge=1, le=24 * 90)

@router.post("/api/obs/share")
async def share(s: ShareIn):
    if not await A.q("SELECT 1 FROM tr_runs WHERE trace_id=%s LIMIT 1", (s.trace_id,)): raise HTTPException(404)
    sid = os.urandom(8).hex(); exp = int(time.time()) + s.hours * 3600
    await A.q("INSERT INTO shares(id,trace_id,expires) VALUES(%s,%s,to_timestamp(%s))", (sid, s.trace_id, exp))
    return {"id": sid, "path": f"/share/{sid}.{exp}.{_sign(sid, exp)}", "expires": exp}

@router.get("/api/obs/shares")
async def shares():
    return await A.q("SELECT * FROM shares ORDER BY created DESC LIMIT 100")

@router.delete("/api/obs/share/{sid}")
async def unshare(sid: str):
    await A.q("UPDATE shares SET revoked=true WHERE id=%s", (sid,)); return {"ok": True}

@router.get("/share/{token}", response_class=HTMLResponse, include_in_schema=False)
async def shared(token: str, request: Request):
    try:
        sid, exp, sig = token.split(".")
        ok = hmac.compare_digest(sig, _sign(sid, int(exp))) and int(exp) > time.time()
    except Exception:  # noqa: BLE001
        ok = False
    row = ok and await A.q("SELECT * FROM shares WHERE id=%s AND NOT revoked AND expires > now()", (sid,), one=True)
    if not row: return HTMLResponse("<h3>Invalid, expired or revoked link.</h3>", status_code=403)
    await A.q("UPDATE shares SET views=views+1 WHERE id=%s", (sid,))
    runs_ = await A.q("SELECT id,parent_id,name,run_type,start_time,latency_ms,status,error,inputs,outputs,total_tokens,cost_usd,model FROM tr_runs WHERE trace_id=%s ORDER BY start_time", (row["trace_id"],))
    import ui_obs
    import i18n
    return HTMLResponse(i18n.localize(i18n.inject(ui_obs.share_page(json.loads(json.dumps(runs_, default=str)), row["expires"])), i18n.lang_of(request), cache=False))

# ------------------------------------------------------------------ alert & automation (alert block moved to alerts.py, GĐ12.10)
async def deliver(kind, target, subject, body, headers=None, ws_id=None):
    """Webhook: sent for real when netguard lets the address through (public https, or an internal address listed in AOS_EGRESS_ALLOW); blocked -> outbox 'failed'
    with the reason (no URL). Other kinds (e-mail, ...) stay drafts for a human to send.
    GĐ10.28: every outbox row names its workspace (ws_id): the caller's, else the background task's. Callers that know the owner (rule / run / hook) pass ws_id.
    Neither known -> NULL = a system row that GET /api/obs/outbox shows to nobody."""
    import netguard
    if ws_id is None:
        import admin
        ws_id = (admin.cur() or {}).get("ws") or admin.WS_CTX.get()
    ws_id = int(ws_id) if ws_id is not None and not isinstance(ws_id, bool) else None
    _ins = "INSERT INTO outbox(kind,target,subject,body,status,detail,ws_id) VALUES(%s,%s,%s,%s,%s,%s,%s)"
    ins = lambda *a: (_ins, (*a, ws_id))   # noqa: E731
    if kind == "webhook" and target:
        try:
            if headers:   # GĐ6.5: {{secret:NAME}} trong header chỉ được thay khi máy đích nằm trong allowed_hosts
                import admin, ws_secrets
                u = admin.cur(); headers = await ws_secrets.resolve_headers(headers, target, (u or {}).get("ws") or admin.WS_CTX.get() or 1)
            async with netguard.client("webhook", timeout=10) as h:
                r = await h.post(target, json={"subject": subject, "body": body}, headers=headers or None)
            await A.q(*ins(kind, target, subject, body, "sent", f"HTTP {r.status_code}"))
            return f"webhook HTTP {r.status_code}"
        except netguard.NetBlocked as e:
            await A.q(*ins(kind, target, subject, body, "failed", "blocked: " + e.reason))
            return "webhook blocked: " + e.reason
        except Exception as e:  # noqa: BLE001
            await A.q(*ins(kind, target, subject, body, "failed", str(e)[:200]))
            return "webhook failed"
    await A.q(*ins(kind, target, subject, body, "draft", "Bản nháp: không tự gửi ra bên thứ ba; cần người duyệt gửi thủ công."))
    return "draft created (" + kind + ")"

@router.get("/api/obs/outbox")
async def outbox():
    import admin
    if admin.is_demo(): return []
    if (admin.cur() or {}).get("role") != "admin": raise HTTPException(403, "Chỉ quản trị workspace xem được hộp thư đi (địa chỉ đích có thể chứa token)")   # GĐ10.28b
    return await A.q("SELECT id,kind,target,subject,body,status,created,detail,ws_id,channel,link,attempts,last_error,next_at,destination_id,event_type,http_status,latency_ms FROM outbox WHERE ws_id=%s ORDER BY id DESC LIMIT 100", (admin.cur_ws(),))   # GĐ10.28: only this workspace's rows (ws_id NULL = system rows: hidden)

async def _dispatch(r, subj, body):   # GĐ12.10: moved to alerts.py (kept as a forwarder for old callers)
    import alerts; return await alerts._dispatch(r, subj, body)

async def _project_ws(pid):
    """GĐ10.28: workspace that owns a project (the owner of the rule / alert that sends a message)."""
    p = await A.q("SELECT workspace_id FROM projects WHERE id=%s", (pid,), one=True)
    return (p or {}).get("workspace_id")

STATE = {"last": {}, "errors": []}
async def _admin_gc():
    import admin; await admin.sandbox_ttl()
async def _ext_gc():
    import ext; await ext.gc()

async def _otel_due():
    import otel; await otel.run_due()   # GĐ9.10 OTLP export to an external collector

async def _gw_gc():
    import gw_api; await gw_api.cache_gc()

async def _computer_disk():
    import sbx_runtime; await sbx_runtime.disk_tick_global()   # GĐ11.40: virtual-computer disk watchdog (every 5 min; no docker call unless a computer runs)

async def worker():
    import evals, insights, engine, parquet_export
    n = 0
    while True:
        await asyncio.sleep(20); n += 1
        import studio
        for name, fn, every in (("agent_crons", studio.run_crons, 3), ("rules", __import__("rules").apply_rules, 1), ("alerts", __import__("alerts").check_alerts, 3), ("outbox_retry", notify.retry_due, 1), ("otel_export", _otel_due, 3), ("schedules", evals.run_schedules, 3), ("insights_weekly", insights.run_weekly, 3), ("parquet_export", parquet_export.run_due, 3), ("fts_backfill", __import__("fts").backfill, 3), ("engine_detect", engine.run_detect, 30),
                                ("retention", retention, 180), ("partitions", ensure_partitions, 180),
                                ("sandbox_ttl", _admin_gc, 90), ("gw_cache_gc", _gw_gc, 30), ("computer_disk", _computer_disk, 15), ("ext_gc", _ext_gc, 90), ("health_watch", __import__("health_watch").run_check, 3), ("agent_purge", __import__("agent_purge").tick, 30), ("stats_rollup", __import__("stats_rollup").catch_up, 15)):
            if n % every: continue
            try:
                await fn(); STATE["last"][name] = time.time()
            except Exception as e:  # noqa: BLE001
                STATE["errors"] = (STATE["errors"] + [f"{time.strftime('%H:%M:%S')} {name}: {str(e)[:200]}"])[-20:]

@router.get("/api/obs/worker")
async def worker_state():
    return {"last": {k: time.strftime("%H:%M:%S", time.localtime(v)) for k, v in STATE["last"].items()}, "errors": STATE["errors"]}


# ================================================================== bổ sung P1 (gaps)
# ---- OTLP/HTTP JSON: nhận span OpenTelemetry (exporter OTLP http/json), map sang run
def _otv(v):
    if not isinstance(v, dict): return v
    for k in ("stringValue", "boolValue", "doubleValue"):
        if k in v: return v[k]
    if "intValue" in v: return int(v["intValue"])
    if "arrayValue" in v: return [_otv(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v: return {x["key"]: _otv(x.get("value")) for x in v["kvlistValue"].get("values", [])}
    return None

def _maybe_json(x):
    if isinstance(x, str) and x[:1] in "{[":
        try: return json.loads(x)
        except Exception: return x  # noqa: BLE001
    return x

KIND = {"llm": "llm", "chat": "llm", "text_completion": "llm", "embedding": "embedding", "tool": "tool", "execute_tool": "tool", "retriever": "retriever",
        "chain": "chain", "agent": "chain", "invoke_agent": "chain"}

def otlp_to_runs(body):
    import otel; return otel.otlp_to_runs(body)   # GĐ9.10: GenAI semconv / OpenInference / LangSmith-OTel mapping lives in otel.py

def otlp_proto_to_json(raw: bytes):
    import otel; return otel.otlp_proto_to_json(raw)

@router.post("/ingest/otlp/v1/traces", tags=["ingest"])   # kept for old exporters; the standard path is /otel/v1/traces (otel.py)
async def otlp(req: Request, x_api_key: str = Header(default="")):
    import otel; return await otel.ingest(req, otel._key_from(req, x_api_key))

# ---- attachments (file đính kèm theo run), ≤ 4 MB
from fastapi import UploadFile, File, Form
from fastapi.responses import Response
MAX_ATT = 4 * 1024 * 1024

async def _save_att(run_id, name, mime, data):
    if len(data) > MAX_ATT: raise HTTPException(413, "File > 4MB")
    r = await A.q("SELECT trace_id FROM tr_runs WHERE id=%s", (run_id,), one=True)
    if not r: raise HTTPException(404, "Không có run")
    return (await A.q("INSERT INTO run_attachments(run_id,trace_id,name,mime,size,data) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
                      (run_id, r["trace_id"], name[:200], mime, len(data), data), one=True))["id"]

@router.post("/ingest/attachments", tags=["ingest"])
async def att_ingest(run_id: str = Form(...), file: UploadFile = File(...), x_api_key: str = Header(default="")):
    import admin
    ws = await key_ws(x_api_key)
    async with admin.as_workspace(ws):   # GĐ10.1: only runs of the key's workspace can receive an attachment
        return {"id": await _save_att(run_id, file.filename or "file", file.content_type, await file.read())}

@router.post("/api/obs/runs/{run_id}/attachments")
async def att_upload(run_id: str, file: UploadFile = File(...)):
    return {"id": await _save_att(run_id, file.filename or "file", file.content_type, await file.read())}

@router.get("/api/obs/traces/{trace_id}/attachments")
async def att_list(trace_id: str):
    return await A.q("SELECT id, run_id, name, mime, size, created FROM run_attachments WHERE trace_id=%s ORDER BY id", (trace_id,))

@router.get("/api/obs/attachments/{aid}")
async def att_get(aid: int):
    r = await A.q("SELECT name, mime, data FROM run_attachments WHERE id=%s", (aid,), one=True)
    if not r: raise HTTPException(404)
    safe = re.sub(r"[^\w.\-]", "_", r["name"])
    inline = (r["mime"] or "").startswith(("image/", "text/plain"))
    return Response(bytes(r["data"]), media_type=r["mime"] or "application/octet-stream",
                    headers={"Content-Disposition": f'{"inline" if inline else "attachment"}; filename="{safe}"', "X-Content-Type-Options": "nosniff"})

# ---- saved filters
class SavedIn(BaseModel):
    project_id: int
    name: str = Field(min_length=1, max_length=80)
    filter: str = Field(min_length=1, max_length=2000)

@router.post("/api/obs/saved_filters")
async def sf_new(b: SavedIn):
    compile_filter(b.filter)
    return await A.q("INSERT INTO saved_filters(project_id,name,filter) VALUES(%s,%s,%s) ON CONFLICT(project_id,name) DO UPDATE SET filter=EXCLUDED.filter RETURNING id",
                     (b.project_id, b.name, b.filter), one=True)

@router.get("/api/obs/saved_filters")
async def sf_list(project_id: int):
    return await A.q("SELECT * FROM saved_filters WHERE project_id=%s ORDER BY name", (project_id,))

@router.delete("/api/obs/saved_filters/{fid}")
async def sf_del(fid: int):
    await A.q("DELETE FROM saved_filters WHERE id=%s", (fid,)); return {"ok": True}

# ---- gom cụm lỗi (chuẩn hoá thông điệp → chữ ký)
_SIG = [(re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I), "<uuid>"), (re.compile(r"https?://\S+"), "<url>"),
        (re.compile(r"'[^']{0,200}'|\"[^\"]{0,200}\""), "<str>"), (re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{12,}\b", re.I), "<hex>"),
        (re.compile(r"\d+(\.\d+)?"), "<n>"), (re.compile(r"\s+"), " ")]
def error_signature(e):
    t = (e or "").strip().splitlines()[0] if e else ""
    t = re.sub(r"^(?:[\w.]+\.)?\w*(?:Error|Exception|Exceeded|Timeout)\w*:\s*", "", t)   # bỏ tên lớp exception để gom biến thể
    for rx, rp in _SIG: t = rx.sub(rp, t)
    return t[:180]

@router.get("/api/obs/projects/{pid}/errors")
async def error_clusters(pid: int, hours: int = 168, mode: str = "signature", threshold: float = 0.86):
    rows = await A.q("""SELECT id, trace_id, name, run_type, error, start_time, parent_id IS NULL is_root FROM tr_runs WHERE project_id=%s AND status='error'
        AND start_time > now() - %s * interval '1 hour' ORDER BY start_time DESC LIMIT 5000""", (pid, hours))
    cl = {}
    for r in rows:
        k = error_signature(r["error"])
        c = cl.setdefault(k, {"signature": k, "count": 0, "traces": set(), "names": {}, "first": r["start_time"], "last": r["start_time"],
                              "sample_error": (r["error"] or "")[:500], "sample_trace": str(r["trace_id"])})
        c["count"] += 1; c["traces"].add(str(r["trace_id"])); c["names"][r["name"]] = c["names"].get(r["name"], 0) + 1
        c["first"] = min(c["first"], r["start_time"])
    out = sorted(cl.values(), key=lambda c: -c["count"])
    if mode == "semantic" and len(out) > 1:
        out = await _semantic_merge(out, threshold)
    for c in out:
        c["n_traces"] = len(c.pop("traces")); c["names"] = sorted(c["names"].items(), key=lambda x: -x[1])[:5]
    return {"total_errors": len(rows), "mode": mode, "clusters": out}

_EMB: dict = {}
async def _semantic_merge(clusters, threshold):
    """Gom cụm ngữ nghĩa: nhúng chữ ký lỗi (Gemini embedding, cache), gộp tham lam theo cosine ≥ ngưỡng; cụm lớn nhất làm đại diện."""
    import math
    need = [c["signature"] for c in clusters if c["signature"] not in _EMB][:200]
    for i in range(0, len(need), 50):
        for sig, v in zip(need[i:i + 50], await A.aembed([x or "(trống)" for x in need[i:i + 50]])): _EMB[sig] = v
    def cos(a, b):
        return sum(x * y for x, y in zip(a, b)) / ((math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))) or 1)
    merged = []
    for c in clusters:                     # đã sắp theo số lần giảm dần
        v = _EMB.get(c["signature"])
        tgt = next((m for m in merged if v is not None and cos(v, _EMB[m["signature"]]) >= threshold), None) if v is not None else None
        if tgt is None:
            c["members"] = [{"signature": c["signature"], "count": c["count"]}]; merged.append(c); continue
        tgt["count"] += c["count"]; tgt["traces"] |= c["traces"]
        for n, k in c["names"].items(): tgt["names"][n] = tgt["names"].get(n, 0) + k
        tgt["first"] = min(tgt["first"], c["first"]); tgt["last"] = max(tgt["last"], c["last"])
        tgt["members"].append({"signature": c["signature"], "count": c["count"], "similarity": round(cos(v, _EMB[tgt["signature"]]), 3)})
    return sorted(merged, key=lambda c: -c["count"])

# ---- redaction rules CRUD
class RedIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    pattern: str = Field(min_length=1, max_length=500)
    replacement: str = "[ĐÃ ẨN]"

@router.get("/api/obs/redaction")
async def red_list(): return await A.q("SELECT * FROM redaction_rules ORDER BY id")

@router.post("/api/obs/redaction")
async def red_new(b: RedIn):
    try: re.compile(b.pattern)
    except re.error as e: raise HTTPException(400, f"Regex sai: {e}")
    r = await A.q("INSERT INTO redaction_rules(name,pattern,replacement) VALUES(%s,%s,%s) RETURNING id", (b.name, b.pattern, b.replacement), one=True)
    _RX["rules"] = None; return r

@router.patch("/api/obs/redaction/{rid}")
async def red_toggle(rid: int, enabled: bool):
    await A.q("UPDATE redaction_rules SET enabled=%s WHERE id=%s", (enabled, rid)); _RX["rules"] = None; return {"ok": True}

@router.delete("/api/obs/redaction/{rid}")
async def red_del(rid: int):
    await A.q("DELETE FROM redaction_rules WHERE id=%s", (rid,)); _RX["rules"] = None; return {"ok": True}

class RedTest(BaseModel):
    text: str
@router.post("/api/obs/redaction/test")
async def red_test(b: RedTest): return {"output": _redact_val(b.text, await redact_rules())}

# ---- chấm cả thread (multi-turn)
async def evaluate_thread(pid, tid, evaluator_id, source="thread", only_tools=None):
    import evals, evalconv
    turns = await A.q("SELECT id, trace_id, inputs, outputs FROM tr_runs WHERE project_id=%s AND parent_id IS NULL AND thread_id=%s ORDER BY start_time", (pid, tid))
    if not turns: raise HTTPException(404, "Thread rỗng")
    ev = await A.q("SELECT * FROM evaluators WHERE id=%s", (evaluator_id,), one=True)
    if not ev: raise HTTPException(404, "Không có evaluator")
    tools = await A.q("SELECT name, inputs, outputs, status FROM tr_runs WHERE project_id=%s AND parent_id IS NOT NULL AND run_type='tool' AND trace_id::text = ANY(%s) ORDER BY start_time",   # GĐ12.70: tool calls of every turn
                      (pid, [str(t["trace_id"]) for t in turns]))
    cv = evalconv.build(turns, tools, only_tools)
    tx = cv["transcript"]
    res = await evals.run_evaluator(ev, {"input": tx, "output": tx, "turns": len(turns), **cv}, None)
    last = str(turns[-1]["id"])
    for x in res:   # idempotent: one feedback row per (thread, evaluator, key, source) - an earlier verdict of this thread is replaced
        await A.q("DELETE FROM feedback WHERE project_id=%s AND source=%s AND evaluator_id=%s AND key=%s AND run_id::text = ANY(%s)", (pid, source, evaluator_id, x["key"], [str(t["id"]) for t in turns]))
        await add_feedback(FbIn(run_id=last, key=x["key"], score=x.get("score"), value=x.get("value"), comment=(x.get("comment") or "") + f" [thread {tid}, {len(turns)} lượt]",
                                metadata={"thread_id": tid, "project_id": pid}),
                           source=source, evaluator_id=evaluator_id)
    return res

class ThEval(BaseModel):
    evaluator_id: int
    only_tools: list[str] | None = None
@router.post("/api/obs/projects/{pid}/threads/{tid}/evaluate")
async def thread_eval(pid: int, tid: str, b: ThEval):
    return await evaluate_thread(pid, tid, b.evaluator_id, only_tools=b.only_tools)
