"""GĐ9.10 Environments: every tracing project (and optionally every ingest key / alert rule) carries an environment label
dev | staging | prod | <custom>. A run's effective environment = metadata.environment (stamped at ingest from the key's default env, or from the OTel
resource attribute deployment.environment.name) > metadata.ls_environment (LangSmith SDK) > its project's environment. The filter DSL exposes it as
`eq(environment,"prod")`. Migration (once, when the column is added): existing projects = prod, except names containing test / dev / sandbox / staging."""
import re
from fastapi import APIRouter, HTTPException

class A:   # lazy handle: importing deepagent_api at module load would boot the whole app (unit tests import norm())
    @staticmethod
    async def q(*a, **k):
        import deepagent_api; return await deepagent_api.q(*a, **k)

router = APIRouter()
PRESETS = ["dev", "staging", "prod"]
ALIASES = {"production": "prod", "prd": "prod", "development": "dev", "develop": "dev", "local": "dev", "stage": "staging", "stg": "staging"}
_RX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")

def norm(v, default=None):
    """Label -> canonical label. None/'' -> default. Raises ValueError for a label that cannot be used (custom labels: a-z 0-9 _ -, max 31)."""
    s = (str(v).strip().lower() if v is not None else "")
    if not s: return default
    s = ALIASES.get(s, s)
    if not _RX.match(s): raise ValueError("Môi trường chỉ gồm a-z, 0-9, _ hoặc - (tối đa 31 ký tự)")
    return s

def norm_http(v, default=None):
    try: return norm(v, default)
    except ValueError as e: raise HTTPException(422, str(e)) from None

# SQL: effective environment of a tr_runs row (use inside a query whose FROM is tr_runs unaliased)
ENV_SQL = ("(CASE lower(coalesce(NULLIF(metadata->>'environment',''), NULLIF(metadata->>'ls_environment',''), "
           "(SELECT pe.environment FROM projects pe WHERE pe.id=tr_runs.project_id), 'prod')) "
           "WHEN 'production' THEN 'prod' WHEN 'prd' THEN 'prod' WHEN 'development' THEN 'dev' WHEN 'develop' THEN 'dev' WHEN 'local' THEN 'dev' "
           "WHEN 'stage' THEN 'staging' WHEN 'stg' THEN 'staging' ELSE lower(coalesce(NULLIF(metadata->>'environment',''), NULLIF(metadata->>'ls_environment',''), "
           "(SELECT pe.environment FROM projects pe WHERE pe.id=tr_runs.project_id), 'prod')) END)")

async def init():
    fresh = not await A.q("SELECT 1 FROM information_schema.columns WHERE table_name='projects' AND column_name='environment'")
    await A.q("ALTER TABLE projects ADD COLUMN IF NOT EXISTS environment TEXT NOT NULL DEFAULT 'prod'")
    await A.q("ALTER TABLE ingest_keys ADD COLUMN IF NOT EXISTS environment TEXT")
    await A.q("ALTER TABLE alert_rules ADD COLUMN IF NOT EXISTS environment TEXT")
    if fresh:   # one-time classification of the projects that existed before environments
        await A.q("UPDATE projects SET environment='dev' WHERE name ~* '(test|dev|sandbox)'")
        await A.q("UPDATE projects SET environment='staging' WHERE name ~* '(staging|stage|uat)' AND environment='prod'")

@router.get("/api/obs/environments")
async def environments():
    """Labels in use in this workspace (presets first) + project counts."""
    rows = await A.q("SELECT environment, count(*) projects FROM projects GROUP BY 1")
    have = {r["environment"]: r["projects"] for r in rows}
    labels = PRESETS + sorted(k for k in have if k not in PRESETS)
    return [{"environment": k, "projects": have.get(k, 0), "preset": k in PRESETS} for k in labels]

@router.get("/api/obs/environments/compare")
async def compare(hours: int = 24, bucket: str = "", environments: str = "", project_id: int | None = None):
    """Same metrics side by side for every environment (root runs of the last `hours`): traces, errors, error rate, p50/p95 latency, tokens, cost + a time series
    per environment for sparklines. `environments` = comma list to restrict; `project_id` = restrict to one project (rare)."""
    hours = max(1, min(hours, 24 * 90)); bucket = bucket or ("minute" if hours <= 1 else "hour" if hours <= 168 else "day")
    if bucket not in ("minute", "hour", "day"): raise HTTPException(400, "bucket phải là minute, hour hoặc day")
    want = [norm_http(x) for x in environments.split(",") if x.strip()]
    pw, pa = ("AND project_id=%s", (project_id,)) if project_id else ("", ())
    base = f"FROM tr_runs WHERE parent_id IS NULL AND start_time > now() - %s * interval '1 hour' {pw}"
    tot = await A.q(f"""SELECT {ENV_SQL} env, count(*) n, count(*) FILTER (WHERE status='error') errors, percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) p50,
        percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) p95, coalesce(sum(total_tokens),0) tokens, coalesce(sum(cost_usd),0)::float8 cost,
        count(DISTINCT project_id) projects {base} GROUP BY 1""", (hours, *pa))
    ser = await A.q(f"""SELECT {ENV_SQL} env, date_trunc('{bucket}', start_time AT TIME ZONE 'Asia/Ho_Chi_Minh') t, count(*) n, count(*) FILTER (WHERE status='error') errors,
        coalesce(sum(cost_usd),0)::float8 cost {base} GROUP BY 1, 2 ORDER BY 2""", (hours, *pa))
    projs = {r["environment"]: r["c"] for r in await A.q("SELECT environment, count(*) c FROM projects GROUP BY 1")}
    by = {r["env"]: r for r in tot}
    keys = want or (PRESETS + sorted(k for k in set(by) | set(projs) if k not in PRESETS))
    out = []
    for k in keys:
        r = by.get(k) or {}; n = r.get("n") or 0
        out.append({"environment": k, "projects": projs.get(k, 0), "traces": n, "errors": r.get("errors") or 0, "error_rate": (r.get("errors") or 0) / n if n else 0,
                    "p50_ms": r.get("p50"), "p95_ms": r.get("p95"), "tokens": int(r.get("tokens") or 0), "cost": r.get("cost") or 0.0,
                    "cost_per_trace": (r.get("cost") or 0.0) / n if n else 0,
                    "series": [{"t": s["t"].isoformat(), "n": s["n"], "errors": s["errors"], "cost": s["cost"]} for s in ser if s["env"] == k]})
    return {"hours": hours, "bucket": bucket, "environments": out}
