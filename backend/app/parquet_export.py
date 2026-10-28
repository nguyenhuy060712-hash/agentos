"""GĐ9.9 Scheduled Parquet export (pyarrow 26): per workspace, 4 tables -> one zstd Parquet file per table per day:
  <root>/ws=<id>/table=<name>/date=YYYY-MM-DD/part-0.parquet      (date = UTC day of the row's own timestamp)
Tables: traces (tr_runs = runs + spans), feedback, eval_results (exp_results of dataset experiments), gateway_usage. Explicit schemas (stable types;
JSON columns are JSON text). Incremental by a per-table cursor (traces: (start_time, id); others: id) kept in parquet_export_cfg.wm; new rows of an
already-exported day are merged into that day's part-0 file. "Full" = delete the workspace's table folders + reset cursors, then export everything.
Destination: local folder (AGENTOS_PARQUET_DIR, default <AGENTOS_EXPORT_DIR>/parquet) or S3-compatible (endpoint, bucket, prefix; access/secret key are
ws secrets (6.5) whose allowed_hosts must include the endpoint host -> ws_secrets.get_secret). Schedule: daily 02:00 server-local time (worker loop, atomic claim).
Every query is explicitly filtered by workspace AND runs under that workspace's admin RLS context."""
import asyncio, datetime as dt, json, os
from pathlib import Path
from urllib.parse import urlparse
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.fs as pafs
import pyarrow.parquet as pq

router = APIRouter()
DDL = """
CREATE TABLE IF NOT EXISTS parquet_export_cfg(workspace_id INT PRIMARY KEY, enabled BOOLEAN NOT NULL DEFAULT false, dest TEXT NOT NULL DEFAULT 'local',
  s3_endpoint TEXT, s3_bucket TEXT, s3_prefix TEXT NOT NULL DEFAULT '', s3_region TEXT, access_key_secret TEXT, secret_key_secret TEXT,
  wm JSONB NOT NULL DEFAULT '{}', last_run_at TIMESTAMPTZ, last_status TEXT, last_error TEXT, last_counts JSONB, last_mode TEXT, last_sched DATE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
"""
BATCH = 5000
LOOKBACK = dt.timedelta(hours=24)   # traces: re-scan this far behind the cursor (spans arrive/finish late); rows are replaced by id, never duplicated
HOUR = 2   # daily at 02:00 local, caught up for 12 h if the worker was down
LOCKS: dict = {}

async def _q(*a, **k):
    import deepagent_api as A
    return await A.q(*a, **k)

async def init(): await _q(DDL)

def local_root() -> Path:
    return Path(os.environ.get("AGENTOS_PARQUET_DIR") or (Path(os.environ.get("AGENTOS_EXPORT_DIR", "/opt/agentos/exports")) / "parquet"))

# ------------------------------------------------------------------ schemas (stable column names + types)
TS = pa.timestamp("us", tz="UTC")
S = pa.string()
SCHEMAS = {
    "traces": pa.schema([("id", S), ("trace_id", S), ("parent_id", S), ("project_id", pa.int32()), ("name", S), ("run_type", S), ("start_time", TS), ("end_time", TS),
        ("latency_ms", pa.int32()), ("status", S), ("error", S), ("inputs", S), ("outputs", S), ("metadata", S), ("tags", pa.list_(pa.field("element", S))),
        ("input_tokens", pa.int32()), ("output_tokens", pa.int32()), ("total_tokens", pa.int32()), ("cost_usd", pa.decimal128(12, 6)),
        ("model", S), ("thread_id", S), ("dotted_order", S)]),
    "feedback": pa.schema([("id", pa.int64()), ("run_id", S), ("trace_id", S), ("key", S), ("score", pa.float64()), ("value", S), ("comment", S),
        ("correction", S), ("source", S), ("evaluator_id", pa.int32()), ("experiment_id", pa.int32()), ("created", TS), ("created_by", S)]),
    "eval_results": pa.schema([("id", pa.int64()), ("experiment_id", pa.int32()), ("experiment_name", S), ("dataset_id", pa.int32()), ("example_id", pa.int32()),
        ("repetition", pa.int32()), ("run_id", S), ("output", S), ("error", S), ("latency_ms", pa.int32()), ("total_tokens", pa.int32()),
        ("cost_usd", pa.decimal128(12, 6)), ("created", TS)]),
    "gateway_usage": pa.schema([("id", pa.int64()), ("ts", TS), ("agent_id", pa.int32()), ("user_id", pa.int32()), ("model", S), ("feature", S),
        ("input_tokens", pa.int32()), ("output_tokens", pa.int32()), ("cached_tokens", pa.int32()), ("reasoning_tokens", pa.int32()),
        ("cost_usd", pa.decimal128(12, 6)), ("status", S), ("latency_ms", pa.int32()), ("error", S)]),
}
JSON_COLS = {"inputs", "outputs", "metadata", "correction", "output"}
TABLES = {   # name -> (SQL with %s ws + cursor params, date column, cursor columns)
    "traces": ("SELECT r.id, r.trace_id, r.parent_id, r.project_id, r.name, r.run_type, r.start_time, r.end_time, r.latency_ms, r.status, r.error, r.inputs, r.outputs, "
               "r.metadata, r.tags, r.input_tokens, r.output_tokens, r.total_tokens, r.cost_usd, r.model, r.thread_id, r.dotted_order FROM tr_runs r "
               "WHERE r.project_id IN (SELECT id FROM projects WHERE workspace_id=%s) AND (r.start_time, r.id::text) > (%s::timestamptz, %s::text) "
               "ORDER BY r.start_time, r.id::text LIMIT %s", "start_time"),
    "feedback": ("SELECT f.id, f.run_id, f.trace_id, f.key, f.score, f.value, f.comment, f.correction, f.source, f.evaluator_id, f.experiment_id, f.created, f.created_by "
                 "FROM feedback f WHERE f.id > %s AND ((f.trace_id IS NOT NULL AND EXISTS (SELECT 1 FROM tr_runs r JOIN projects p ON p.id=r.project_id "
                 "WHERE r.trace_id=f.trace_id AND p.workspace_id=%s)) OR (f.experiment_id IN (SELECT e.id FROM experiments e JOIN datasets d ON d.id=e.dataset_id "
                 "WHERE d.workspace_id=%s))) ORDER BY f.id LIMIT %s", "created"),
    "eval_results": ("SELECT x.id, x.experiment_id, e.name experiment_name, e.dataset_id, x.example_id, x.repetition, x.run_id, x.output, x.error, x.latency_ms, "
                     "x.total_tokens, x.cost_usd, x.created FROM exp_results x JOIN experiments e ON e.id=x.experiment_id JOIN datasets d ON d.id=e.dataset_id "
                     "WHERE x.id > %s AND d.workspace_id=%s ORDER BY x.id LIMIT %s", "created"),
    "gateway_usage": ("SELECT g.id, g.ts, g.agent_id, g.user_id, g.model, g.feature, g.input_tokens, g.output_tokens, g.cached_tokens, g.reasoning_tokens, g.cost_usd, "
                      "g.status, g.latency_ms, g.error FROM gateway_usage g WHERE g.id > %s AND g.workspace_id=%s ORDER BY g.id LIMIT %s", "ts"),
}

def _args(name, ws, cur):
    if name == "traces":
        ts = (cur or {}).get("ts")
        if cur and cur.get("scan"): return (ws, ts, cur["id"], BATCH)   # inside a scan: plain keyset
        return (ws, (dt.datetime.fromisoformat(ts) - LOOKBACK).isoformat() if ts else "-infinity", "", BATCH)
    last = (cur or {}).get("id") or 0
    if name == "feedback": return (last, ws, ws, BATCH)
    return (last, ws, BATCH)

def _cursor(name, row):
    return {"ts": row["start_time"].isoformat(), "id": str(row["id"])} if name == "traces" else {"id": int(row["id"])}

def to_table(name, rows) -> pa.Table:
    sch = SCHEMAS[name]; out = []
    for r in rows:
        d = {}
        for f in sch:
            v = r.get(f.name)
            if v is None: d[f.name] = None
            elif f.name in JSON_COLS: d[f.name] = json.dumps(v, ensure_ascii=False, default=str)
            elif pa.types.is_string(f.type): d[f.name] = str(v)
            else: d[f.name] = v
        out.append(d)
    return pa.Table.from_pylist(out, schema=sch)

# ------------------------------------------------------------------ destination
def s3_cfg(cfg: dict) -> dict:
    """Validate + normalise the S3 part of a config. Returns {scheme, host, endpoint_override, bucket, prefix, region}. Raises ValueError."""
    u = urlparse((cfg.get("s3_endpoint") or "").strip())
    if u.scheme not in ("https", "http") or not u.hostname: raise ValueError("Endpoint S3 phải là URL dạng https://host")
    if u.scheme == "http" and u.hostname not in ("127.0.0.1", "localhost", "::1"): raise ValueError("Endpoint S3 phải dùng https (http chỉ cho máy nội bộ)")
    b = (cfg.get("s3_bucket") or "").strip()
    if not b or "/" in b: raise ValueError("Cần tên bucket (không có dấu /)")
    return {"scheme": u.scheme, "host": u.hostname, "endpoint_override": f"{u.hostname}:{u.port}" if u.port else u.hostname, "bucket": b,
            "prefix": (cfg.get("s3_prefix") or "").strip().strip("/"), "region": (cfg.get("s3_region") or "").strip() or None}

def s3_root(c: dict) -> str: return c["bucket"] + ("/" + c["prefix"] if c["prefix"] else "")

async def make_fs(cfg: dict, ws: int):
    """(filesystem, root path). S3 keys come from ws secrets, only released because the secret's allowed_hosts contains the endpoint host."""
    if cfg.get("dest") != "s3":
        root = local_root(); root.mkdir(parents=True, exist_ok=True)
        return pafs.LocalFileSystem(), str(root)
    c = s3_cfg(cfg)
    import ws_secrets
    if not cfg.get("access_key_secret") or not cfg.get("secret_key_secret"): raise ValueError("Cần chọn 2 bí mật chứa access key và secret key")
    try:
        ak = await ws_secrets.get_secret(cfg["access_key_secret"], ws, c["host"]); sk = await ws_secrets.get_secret(cfg["secret_key_secret"], ws, c["host"])
    except (LookupError, PermissionError) as e: raise ValueError(str(e)) from None
    kw = {"region": c["region"]} if c["region"] else {}
    return pafs.S3FileSystem(access_key=ak, secret_key=sk, endpoint_override=c["endpoint_override"], scheme=c["scheme"], **kw), s3_root(c)

def part_path(root: str, ws: int, name: str, day) -> str:
    return f"{root}/ws={ws}/table={name}/date={day}/part-0.parquet"

def _merge_write(fs, root, ws, name, tbl: pa.Table) -> int:
    """Group the new rows by UTC day and merge each group into that day's part-0 file; a row whose id is already there is REPLACED (late updates).
    Returns how many ids were new. Sync: run in a thread."""
    col = TABLES[name][1]; days: dict = {}; fresh = 0
    for i, v in enumerate(tbl.column(col).to_pylist()):
        days.setdefault((v.astimezone(dt.timezone.utc) if v else dt.datetime.now(dt.timezone.utc)).date().isoformat(), []).append(i)
    for day, idx in days.items():
        p = part_path(root, ws, name, day); new = tbl.take(idx); fresh += new.num_rows
        if fs.get_file_info(p).type == pafs.FileType.File:
            old = pq.read_table(p, filesystem=fs, schema=SCHEMAS[name])
            fresh -= pc.sum(pc.is_in(new["id"], value_set=old["id"]).cast(pa.int64())).as_py() or 0
            new = pa.concat_tables([old.filter(pc.invert(pc.is_in(old["id"], value_set=new["id"]))), new])
        fs.create_dir(p.rsplit("/", 1)[0], recursive=True)
        pq.write_table(new, p, filesystem=fs, compression="zstd")
    return fresh

def _wipe(fs, root, ws):
    for name in TABLES:
        d = f"{root}/ws={ws}/table={name}"
        if fs.get_file_info(d).type == pafs.FileType.Directory: fs.delete_dir(d)

# ------------------------------------------------------------------ export
async def get_cfg(ws: int) -> dict:
    r = await _q("SELECT * FROM parquet_export_cfg WHERE workspace_id=%s", (ws,), one=True)
    return r or {"workspace_id": ws, "enabled": False, "dest": "local", "s3_endpoint": None, "s3_bucket": None, "s3_prefix": "", "s3_region": None,
                 "access_key_secret": None, "secret_key_secret": None, "wm": {}, "last_run_at": None, "last_status": None, "last_error": None, "last_counts": None, "last_mode": None}

async def export_ws(ws: int, full: bool = False, mode: str = "manual") -> dict:
    """Export one workspace. Run it as its own task (it sets the RLS context contextvars). Returns {table: {"added", "total"}}."""
    import admin
    admin.USER.set(None); admin.WS_CTX.set(ws)   # RLS as the workspace admin
    lock = LOCKS.setdefault(ws, asyncio.Lock())
    if lock.locked(): raise HTTPException(409, "Đang xuất, đợi xong rồi thử lại")
    async with lock:
        cfg = await get_cfg(ws); wm = {} if full else dict(cfg.get("wm") or {}); counts = {}
        try:
            fs, root = await make_fs(cfg, ws)
            if full: await asyncio.to_thread(_wipe, fs, root, ws); wm = {}
            for name, (sql, _) in TABLES.items():
                st = wm.get(name) or {}; cur = st.get("cursor"); added = 0
                while True:
                    rows = await _q(sql, _args(name, ws, cur))
                    if not rows: break
                    added += await asyncio.to_thread(_merge_write, fs, root, ws, name, to_table(name, rows))
                    cur = _cursor(name, rows[-1])
                    if name == "traces":   # stored cursor = newest start_time ever exported; "scan" marks the in-progress keyset position
                        top = max(cur["ts"], (st.get("cursor") or {}).get("ts") or "")
                        wm[name] = {"cursor": {"ts": top, "id": cur["id"]}, "total": int(st.get("total") or 0) + added}
                        scan = {"ts": cur["ts"], "id": cur["id"], "scan": True}
                    else:
                        wm[name] = {"cursor": cur, "total": int(st.get("total") or 0) + added}; scan = cur
                    await _save(ws, wm); cur = scan
                    if len(rows) < BATCH: break
                counts[name] = {"added": added, "total": int((wm.get(name) or st).get("total") or 0)}
            await _save(ws, wm, status="ok", error=None, counts=counts, mode="full" if full else mode)
        except Exception as e:  # noqa: BLE001
            await _save(ws, wm, status="error", error=f"{type(e).__name__}: {str(e)[:300]}", counts=counts or None, mode="full" if full else mode)
            raise
    return counts

async def _save(ws, wm, status=None, error=None, counts=None, mode=None):
    if status is None:
        await _q("INSERT INTO parquet_export_cfg(workspace_id, wm) VALUES(%s,%s) ON CONFLICT(workspace_id) DO UPDATE SET wm=excluded.wm", (ws, Jsonb(wm)))
    else:
        await _q("INSERT INTO parquet_export_cfg(workspace_id, wm, last_run_at, last_status, last_error, last_counts, last_mode) VALUES(%s,%s,now(),%s,%s,%s,%s) "
                 "ON CONFLICT(workspace_id) DO UPDATE SET wm=excluded.wm, last_run_at=now(), last_status=excluded.last_status, last_error=excluded.last_error, "
                 "last_counts=excluded.last_counts, last_mode=excluded.last_mode", (ws, Jsonb(wm), status, error, Jsonb(counts) if counts is not None else None, mode))

def due(now: dt.datetime, last_sched) -> bool:
    """Daily 02:00 server-local, caught up until 14:00; once per local day."""
    n = now.astimezone(); d = n.date()
    if n.hour < HOUR or n.hour >= HOUR + 12: return False
    return last_sched is None or last_sched < d

async def run_due():
    """Worker task (~every minute): enabled workspaces whose 02:00 slot has come. Atomic claim on last_sched => one process runs it."""
    now = dt.datetime.now(dt.timezone.utc)
    for c in await _q("SELECT workspace_id, last_sched FROM parquet_export_cfg WHERE enabled"):
        if not due(now, c["last_sched"]): continue
        won = await _q("UPDATE parquet_export_cfg SET last_sched=%s WHERE workspace_id=%s AND last_sched IS NOT DISTINCT FROM %s RETURNING 1",
                       (now.astimezone().date(), c["workspace_id"], c["last_sched"]), one=True)
        if won:
            try: await asyncio.create_task(export_ws(c["workspace_id"], mode="scheduled"))
            except Exception: pass  # noqa: BLE001  (error is stored in last_error)

# ------------------------------------------------------------------ API (admin only, demo off; also blocked by admin.ADMIN_ONLY)
def _admin():
    import admin; u = admin.cur()
    if not u or u.get("role") != "admin" or u.get("demo"): raise HTTPException(403, "Chỉ admin cấu hình xuất Parquet")
    return u

class CfgIn(BaseModel):
    enabled: bool = False
    dest: str = Field(default="local", pattern=r"^(local|s3)$")
    s3_endpoint: str | None = Field(default=None, max_length=300)
    s3_bucket: str | None = Field(default=None, max_length=100)
    s3_prefix: str = Field(default="", max_length=200)
    s3_region: str | None = Field(default=None, max_length=50)
    access_key_secret: str | None = Field(default=None, max_length=64)
    secret_key_secret: str | None = Field(default=None, max_length=64)

PUBLIC = ("enabled", "dest", "s3_endpoint", "s3_bucket", "s3_prefix", "s3_region", "access_key_secret", "secret_key_secret", "last_run_at", "last_status", "last_error", "last_counts", "last_mode")

def _public(c: dict) -> dict:
    out = {k: c.get(k) for k in PUBLIC}
    out["local_dir"] = str(local_root() / f"ws={c['workspace_id']}") if c.get("dest") != "s3" else None
    out["totals"] = {k: v.get("total", 0) for k, v in (c.get("wm") or {}).items()}
    return out

@router.get("/api/exports/parquet")
async def cfg_get():
    u = _admin(); return _public(await get_cfg(u["ws"]))

@router.put("/api/exports/parquet")
async def cfg_put(b: CfgIn):
    u = _admin()
    if b.dest == "s3":
        try: s3_cfg(b.model_dump())
        except ValueError as e: raise HTTPException(422, str(e)) from None
        for n in (b.access_key_secret, b.secret_key_secret):
            if not n or not await _q("SELECT 1 FROM ws_secrets WHERE workspace_id=%s AND name=%s", (u["ws"], n), one=True): raise HTTPException(422, f"Không có bí mật {n or '(trống)'} trong workspace")
    await _q("INSERT INTO parquet_export_cfg(workspace_id,enabled,dest,s3_endpoint,s3_bucket,s3_prefix,s3_region,access_key_secret,secret_key_secret) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) "
             "ON CONFLICT(workspace_id) DO UPDATE SET enabled=excluded.enabled, dest=excluded.dest, s3_endpoint=excluded.s3_endpoint, s3_bucket=excluded.s3_bucket, "
             "s3_prefix=excluded.s3_prefix, s3_region=excluded.s3_region, access_key_secret=excluded.access_key_secret, secret_key_secret=excluded.secret_key_secret, updated_at=now()",
             (u["ws"], b.enabled, b.dest, b.s3_endpoint, b.s3_bucket, b.s3_prefix.strip("/"), b.s3_region, b.access_key_secret, b.secret_key_secret))
    return _public(await get_cfg(u["ws"]))

class RunIn(BaseModel):
    full: bool = False

@router.post("/api/exports/parquet/run")
async def run_now(b: RunIn | None = None):
    u = _admin(); full = bool(b and b.full)
    try: counts = await asyncio.shield(asyncio.create_task(export_ws(u["ws"], full=full)))
    except ValueError as e: raise HTTPException(422, str(e)) from None
    return {"ok": True, "full": full, "counts": counts, **_public(await get_cfg(u["ws"]))}
