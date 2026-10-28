"""GĐ9.Q P5 — Agent Server parity (LangSmith Deployment pages, self-hosted, no cloud):
 * HTTP headers for runtime configuration  (`http.configurable_headers` includes/excludes -> run `config.configurable`)
 * HTTP headers in server logs            (`http.logging_headers` includes/excludes; redacted, control characters stripped)
 * server-side caching                    (`swr` / `cache_get` / `cache_set` like langgraph_sdk.cache, Postgres-backed, per workspace, TTL <= 1 day)
   + `PgCache`, a LangGraph `BaseCache` (node CachePolicy) stored in Postgres, shared by every process
 * custom checkpointer option             (postgres default | sqlite | memory | your own async context manager from aos.json / AOS_CHECKPOINTER)
 * application structure (`aos.json` + JSON schema) + integrations docs pages, and OpenAPI securitySchemes in /openapi.json
Code-loading settings (custom checkpointer, auth module) come ONLY from the file `aos.json` / environment on the server disk, never from an HTTP API
(an API that loads Python paths would be remote code execution for any admin account). Declarative settings (header lists) are editable in the UI."""
import asyncio, contextlib, importlib, importlib.util, json, logging, os, re, time, uuid
from datetime import timedelta
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator
from psycopg.types.json import Jsonb
import caller

async def _q(*a, **k):
    import deepagent_api as A   # lazy: unit tests import this module without booting the whole server
    return await A.q(*a, **k)

router = APIRouter(tags=["server-config"])
LOG = logging.getLogger("aos.access")
if not LOG.handlers:     # one line per API request on stdout (journald): method path status ms [allow-listed headers]
    _h = logging.StreamHandler(); _h.setFormatter(logging.Formatter("%(asctime)s access %(message)s")); LOG.addHandler(_h); LOG.setLevel(logging.INFO); LOG.propagate = False
APP_DIR = Path(__file__).parent
DOCS_DIR = APP_DIR / "docs"
DDL = """
CREATE TABLE IF NOT EXISTS server_config(key TEXT PRIMARY KEY, value JSONB NOT NULL, updated TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS http_log(id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL DEFAULT now(), method TEXT, path TEXT, status INT, ms INT,
  user_id INT, workspace_id INT, ip TEXT, headers JSONB NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS http_log_ts ON http_log(ts);
CREATE TABLE IF NOT EXISTS srv_cache(workspace_id INT NOT NULL, key TEXT NOT NULL, value JSONB NOT NULL, created TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires TIMESTAMPTZ NOT NULL, PRIMARY KEY(workspace_id, key));
CREATE INDEX IF NOT EXISTS srv_cache_exp ON srv_cache(expires);
CREATE TABLE IF NOT EXISTS node_cache(ns TEXT NOT NULL, key TEXT NOT NULL, enc TEXT NOT NULL, val BYTEA NOT NULL, expires TIMESTAMPTZ, PRIMARY KEY(ns, key));
"""
# Headers that can NEVER reach a run config or a log line, whatever the lists say (credentials).
HARD_DENY = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "x-aos-key", "x-gateway-key", "x-csrf-token", "x-forwarded-client-cert"}
PAT_RE = re.compile(r"^[A-Za-z0-9_*\-]{1,80}$")
CTRL_RE = re.compile(r"[\x00-\x1f\x7f  ]")
MAX_CACHE_TTL = 86400
MAX_VALUE_BYTES = 262144

# ---------------------------------------------------------------- aos.json (file config, server disk only)
class HeaderRules(BaseModel):
    includes: list[str] = Field(default_factory=list, max_length=50)
    excludes: list[str] = Field(default_factory=list, max_length=50)
    @field_validator("includes", "excludes")
    @classmethod
    def _pats(cls, v):
        for p in v:
            if not PAT_RE.fullmatch(p): raise ValueError(f"header pattern {p!r}: only letters, digits, '-', '_' and '*' are allowed (no other regex)")
        return [p.lower() for p in v]

class HttpCfg(BaseModel):
    configurable_headers: HeaderRules = Field(default_factory=HeaderRules)
    logging_headers: HeaderRules = Field(default_factory=HeaderRules)

class AuthFile(BaseModel):
    path: str | None = Field(default=None, description="'./auth.py:auth' — a module that defines a langgraph_sdk.Auth object (Auth.authenticate + Auth.on handlers)")
    paths: list[str] = Field(default_factory=lambda: ["/api/v1/"], description="URL prefixes where custom authentication may identify a caller (never the admin UI/API)")
    max_role: str = Field(default="editor", pattern=r"^(viewer|editor|admin)$", description="highest Agent OS role a custom-auth identity can get")
    workspace: int = Field(default=1, description="workspace that custom-auth identities are placed in")
    openapi: dict | None = Field(default=None, description="{securitySchemes, security}: documents your scheme in /openapi.json (docs only)")

class CacheCfg(BaseModel):
    max_ttl_s: int = Field(default=MAX_CACHE_TTL, ge=1, le=MAX_CACHE_TTL)

class AosFile(BaseModel):
    """`aos.json` — the Agent OS counterpart of langgraph.json (agents themselves are created in the UI/API and stored in Postgres)."""
    model_config = {"extra": "forbid"}
    schema_: str | None = Field(default=None, alias="$schema")
    http: HttpCfg = Field(default_factory=HttpCfg)
    auth: AuthFile = Field(default_factory=AuthFile)
    checkpointer: str | dict | None = Field(default=None, description="'postgres' (default) | 'sqlite:///path.db' | 'memory' | {'path': './my_cp.py:generate_checkpointer'}")
    cache: CacheCfg = Field(default_factory=CacheCfg)
    env: str | None = Field(default=None, description="documented only: environment variables come from the service's .env")

def config_path() -> Path: return Path(os.environ.get("AOS_CONFIG") or (APP_DIR / "aos.json"))

_FILE: dict = {"mtime": None, "cfg": None, "err": None}
def file_config() -> AosFile:
    p = config_path()
    try: m = p.stat().st_mtime
    except OSError: m = None
    if _FILE["cfg"] is not None and _FILE["mtime"] == m: return _FILE["cfg"]
    cfg, err = AosFile(), None
    if m is not None:
        try: cfg = AosFile.model_validate(json.loads(p.read_text(encoding="utf-8")))
        except Exception as e:  # noqa: BLE001
            err = str(e)[:500]; LOG.error("aos.json invalid, ignored: %s", err)
    _FILE.update(mtime=m, cfg=cfg, err=err); return cfg

# ---------------------------------------------------------------- effective HTTP config = aos.json defaults, overridden per section by the UI/API (DB)
_DB: dict = {"at": 0.0, "http": None}
async def _load_db(force=False):
    if not force and time.monotonic() - _DB["at"] < 5: return
    try:
        r = await _q("SELECT value FROM server_config WHERE key='http'", one=True)
        _DB.update(at=time.monotonic(), http=(r or {}).get("value"))
    except Exception: _DB["at"] = time.monotonic()  # noqa: BLE001  (table not ready / pool closing: keep the last known value)

def http_cfg() -> HttpCfg:
    f = file_config().http; d = _DB.get("http") or {}
    try: return HttpCfg.model_validate({**f.model_dump(), **{k: v for k, v in d.items() if k in ("configurable_headers", "logging_headers")}})
    except Exception: return f  # noqa: BLE001

def _rx(pats): return [re.compile("^" + ".*".join(re.escape(x) for x in p.lower().split("*")) + "$") for p in pats]

def header_allowed(name: str, rules: HeaderRules) -> bool:
    """LangGraph semantics: `*` matches any run of characters, case-insensitive, no other patterns; excludes win over includes; credentials are never allowed."""
    n = name.lower()
    if n in HARD_DENY: return False
    if any(r.match(n) for r in _rx(rules.excludes)): return False
    return any(r.match(n) for r in _rx(rules.includes))

def clean_value(v, limit=300) -> str: return CTRL_RE.sub(" ", str(v))[:limit]

def pick(headers: dict | None, rules: HeaderRules, limit=30) -> dict:
    out = {}
    for k, v in (headers or {}).items():
        if len(out) >= limit: break
        if header_allowed(k, rules): out[k.lower()] = clean_value(v)
    return out

def pick_configurable(headers) -> dict: return pick(headers, http_cfg().configurable_headers)
def pick_logging(headers) -> dict: return pick(headers, http_cfg().logging_headers)

# ---------------------------------------------------------------- ASGI middleware: request headers context + access log
class HeadersMiddleware:
    """Outermost. Puts the lower-cased request headers in `caller.REQ_HDRS` (used when a run is created) and writes one access-log line per API
    request: method path status ms + ONLY the headers allowed by `logging_headers` (never the credentials, values stripped of control characters)."""
    SKIP = ("/static/", "/health", "/favicon", "/_next/")
    def __init__(self, app): self.app = app
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http": return await self.app(scope, receive, send)
        hdr = {}
        for k, v in scope.get("headers") or []:
            kk = k.decode("latin-1").lower()
            hdr[kk] = (hdr.get(kk, "") + "," if kk in hdr else "") + v.decode("latin-1")
        caller.REQ_HDRS.set(hdr)
        await _load_db()
        st = {"status": 0}; t0 = time.perf_counter()
        async def _send(m):
            if m["type"] == "http.response.start": st["status"] = m["status"]
            await send(m)
        try: await self.app(scope, receive, _send)
        finally:
            path = scope.get("path", "")
            if not path.startswith(self.SKIP):
                try: await _log_request(scope, hdr, st["status"], int((time.perf_counter() - t0) * 1000))
                except Exception as e:  # noqa: BLE001
                    LOG.debug("access log failed: %s", e)

_N = {"n": 0}
async def _log_request(scope, hdr, status, ms):
    lh = pick_logging(hdr); path = re.sub(r"^(/invite/|/api/invites/)[^/]+", r"\g<1>*", clean_value(scope.get("path", ""), 300))   # GĐ12.100: an invite link token is a credential: never in the access log / http_log
    ip = clean_value(hdr.get("cf-connecting-ip") or hdr.get("x-forwarded-for", "").split(",")[0].strip() or (scope.get("client") or ("", 0))[0], 64)
    LOG.info("%s %s -> %s %dms%s", scope.get("method"), path, status, ms, (" headers=" + json.dumps(lh, ensure_ascii=False)) if lh else "")
    if not lh: return    # only requests that carry an allow-listed header are stored (debugging by request/correlation id); the line above always goes to the log
    _N["n"] += 1
    asyncio.get_running_loop().create_task(_store_log(scope.get("method"), path, status, ms, ip, lh, _N["n"] % 200 == 0))

async def _store_log(method, path, status, ms, ip, lh, gc):
    try:
        await _q("INSERT INTO http_log(method,path,status,ms,ip,headers) VALUES(%s,%s,%s,%s,%s,%s)", (method, path, status, ms, ip, Jsonb(lh)))
        if gc: await _q("DELETE FROM http_log WHERE ts < now() - interval '7 days'")
    except Exception as e: LOG.debug("store log failed: %s", e)  # noqa: BLE001

# ---------------------------------------------------------------- admin API
class HttpIn(BaseModel):
    configurable_headers: HeaderRules | None = None
    logging_headers: HeaderRules | None = None

async def init():
    await _q(DDL)
    import admin
    admin.ADMIN_ONLY = tuple(dict.fromkeys(admin.ADMIN_ONLY + ("/api/server-config",)))   # admin-only like /api/admin, /api/secrets
    await _load_db(True)

def _checkpointer_info() -> dict:
    s = checkpointer_spec()
    return {"kind": s["kind"], **({"path": s["path"]} if s.get("path") and s["kind"] == "sqlite" else {}), **({"custom": s["spec"]} if s["kind"] == "custom" else {}),
            "source": s["source"]}

@router.get("/api/server-config")
async def cfg_get():
    await _load_db(True)
    f = file_config()
    return {"http": http_cfg().model_dump(), "source": {"file": str(config_path()) if config_path().exists() else None, "file_error": _FILE["err"], "db_override": bool(_DB.get("http"))},
            "hard_denied": sorted(HARD_DENY), "checkpointer": _checkpointer_info(), "cache": {"max_ttl_s": f.cache.max_ttl_s, **await cache_stats()},
            "auth": {"module": f.auth.path, "paths": f.auth.paths, "max_role": f.auth.max_role}}

@router.put("/api/server-config/http")
async def cfg_put_http(b: HttpIn):
    cur = dict(_DB.get("http") or {})
    for k in ("configurable_headers", "logging_headers"):
        v = getattr(b, k)
        if v is not None: cur[k] = v.model_dump()
    await _q("INSERT INTO server_config(key,value) VALUES('http',%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated=now()", (Jsonb(cur),))
    await _load_db(True)
    return await cfg_get()

@router.delete("/api/server-config/http")
async def cfg_reset_http():
    await _q("DELETE FROM server_config WHERE key='http'"); await _load_db(True); return await cfg_get()

class HeadersTest(BaseModel):
    headers: dict[str, str] = Field(max_length=100)

@router.post("/api/server-config/headers/test")
async def cfg_test(b: HeadersTest):
    """Dry run: what would reach a run's config and what would be logged for a request with these headers (nothing is stored)."""
    h = {k.lower(): v for k, v in b.headers.items()}
    return {"configurable": pick_configurable(h), "logged": pick_logging(h), "dropped": sorted(k for k in h if k in HARD_DENY)}

@router.get("/api/server-config/logs")
async def cfg_logs(limit: int = 100, header: str | None = None, value: str | None = None):
    where, args = "", []
    if header: where = "WHERE headers ->> %s = %s" if value is not None else "WHERE headers ? %s"; args = [header.lower()] + ([value] if value is not None else [])
    rows = await _q(f"SELECT id, ts, method, path, status, ms, ip, headers FROM http_log {where} ORDER BY id DESC LIMIT %s", (*args, max(1, min(limit, 500))))
    return {"logs": rows}

@router.get("/api/server-config/schema")
async def cfg_schema():
    return AosFile.model_json_schema(by_alias=True)

# ---------------------------------------------------------------- server-side cache (langgraph_sdk.cache semantics)
def _ws(): import admin; return admin.cur_ws()

def _cap_ttl(ttl) -> int:
    t = int(ttl.total_seconds() if isinstance(ttl, timedelta) else (ttl or 0))
    return MAX_CACHE_TTL if t <= 0 else min(t, MAX_CACHE_TTL, file_config().cache.max_ttl_s)

def _key(k) -> str:
    k = str(k)
    if not k or len(k) > 200 or CTRL_RE.search(k): raise HTTPException(422, "cache key must be 1-200 printable characters")
    return k

async def cache_get(key: str, ws: int | None = None):
    r = await _q("SELECT value FROM srv_cache WHERE workspace_id=%s AND key=%s AND expires > now()", (ws or _ws(), _key(key)), one=True)
    return None if not r else r["value"]["v"]

async def cache_set(key: str, value, *, ttl: timedelta | int | None = None, ws: int | None = None):
    try: raw = json.dumps({"v": value}, default=str)
    except (TypeError, ValueError): raise HTTPException(422, "cache values must be JSON-serializable") from None
    if len(raw) > MAX_VALUE_BYTES: raise HTTPException(413, f"cache value larger than {MAX_VALUE_BYTES} bytes")
    await _q("INSERT INTO srv_cache(workspace_id,key,value,expires) VALUES(%s,%s,%s,now()+%s*interval '1 second') ON CONFLICT(workspace_id,key) DO UPDATE SET value=EXCLUDED.value, created=now(), expires=EXCLUDED.expires",
              (ws or _ws(), _key(key), Jsonb({"v": json.loads(raw)["v"]}), _cap_ttl(ttl)))

async def cache_delete(key: str, ws: int | None = None):
    await _q("DELETE FROM srv_cache WHERE workspace_id=%s AND key=%s", (ws or _ws(), _key(key)))

class SWRResult:
    def __init__(self, value, status, key, loader, ws, kw): self.value, self.status, self._k, self._l, self._ws, self._kw = value, status, key, loader, ws, kw
    async def mutate(self, new=None):
        """mutate(value) writes the value; mutate() forces a revalidation by calling the loader (same as langgraph_sdk.cache.SWRResult)."""
        v = new if new is not None else await self._l()
        await cache_set(self._k, v, ttl=MAX_CACHE_TTL, ws=self._ws); self.value = v; return v

async def _cache_row(ws, k):
    return await _q("SELECT value, extract(epoch FROM now()-created) age FROM srv_cache WHERE workspace_id=%s AND key=%s AND expires > now()", (ws, k), one=True)

_REVAL: set = set()
async def swr(key: str, loader, *, fresh_for: timedelta = timedelta(0), max_age: timedelta = timedelta(days=1), ws: int | None = None) -> SWRResult:
    """stale-while-revalidate: miss -> await loader; fresh (age < fresh_for) -> cached; stale (< max_age) -> cached now + background refresh (one per key);
    expired (>= max_age) -> await loader. max_age is capped at one day. Values must be JSON-serializable."""
    ws = ws or _ws(); k = _key(key); kw = {"max_age": max_age}
    r = await _cache_row(ws, k)
    ma = _cap_ttl(max_age)
    if r and float(r["age"]) < ma:
        if float(r["age"]) < fresh_for.total_seconds(): return SWRResult(r["value"]["v"], "fresh", k, loader, ws, kw)
        if (ws, k) not in _REVAL:
            _REVAL.add((ws, k))
            async def _bg():
                try: await cache_set(k, await loader(), ttl=MAX_CACHE_TTL, ws=ws)
                except Exception as e: LOG.warning("swr revalidate %s failed: %s", k, e)  # noqa: BLE001
                finally: _REVAL.discard((ws, k))
            asyncio.get_running_loop().create_task(_bg())
        return SWRResult(r["value"]["v"], "stale", k, loader, ws, kw)
    v = await loader(); await cache_set(k, v, ttl=MAX_CACHE_TTL, ws=ws)   # kept for the cap (1 day): `max_age` is judged on the entry's age, so 'expired' is distinguishable from 'miss'
    return SWRResult(v, "expired" if r else "miss", k, loader, ws, kw)

async def cache_stats():
    r = await _q("SELECT count(*) n FROM srv_cache WHERE workspace_id=%s AND expires > now()", (_ws(),), one=True)
    return {"entries": r["n"]}

class CacheIn(BaseModel):
    value: object
    ttl_seconds: int | None = Field(default=None, ge=1)

@router.get("/api/cache")
async def api_cache_list():
    return await _q("SELECT key, created, expires, length(value::text) bytes FROM srv_cache WHERE workspace_id=%s AND expires > now() ORDER BY created DESC LIMIT 200", (_ws(),))

@router.get("/api/cache/{key:path}")
async def api_cache_get(key: str):
    v = await cache_get(key)
    if v is None: raise HTTPException(404, "Not cached or expired")
    return {"key": key, "value": v}

@router.put("/api/cache/{key:path}")
async def api_cache_put(key: str, b: CacheIn):
    await cache_set(key, b.value, ttl=b.ttl_seconds); return {"key": key, "ttl_seconds": _cap_ttl(b.ttl_seconds)}

@router.delete("/api/cache/{key:path}")
async def api_cache_del(key: str):
    await cache_delete(key); return {"ok": True}

async def cache_gc():
    await _q("DELETE FROM srv_cache WHERE expires < now()"); await _q("DELETE FROM node_cache WHERE expires IS NOT NULL AND expires < now()")

# ---------------------------------------------------------------- LangGraph node cache (CachePolicy) backed by Postgres
def _pg_uri(): return os.environ.get("PG_URI", "")
def PgCache(query=None):
    """`graph.compile(cache=PgCache())` + `add_node(..., cache_policy=CachePolicy(ttl=60))`: node results survive restarts and are shared by every process
    (langgraph's InMemoryCache is per process). `query` = optional async fn(sql, args, many) -> rows; default: own psycopg connection to PG_URI."""
    from langgraph.cache.base import BaseCache
    import psycopg

    class _PgCache(BaseCache):
        async def _x(self, sql, args=(), fetch=False):
            if query: return await query(sql, args, fetch)
            async with await psycopg.AsyncConnection.connect(_pg_uri(), autocommit=True) as c:
                cur = await c.execute(sql, args)
                return await cur.fetchall() if fetch else None
        @staticmethod
        def _k(fk): return ".".join(fk[0]), fk[1]
        async def aget(self, keys):
            out = {}
            for fk in keys:
                ns, k = self._k(fk)
                rows = await self._x("SELECT enc, val FROM node_cache WHERE ns=%s AND key=%s AND (expires IS NULL OR expires > now())", (ns, k), True)
                if rows:
                    enc, val = rows[0][0], bytes(rows[0][1]); out[fk] = self.serde.loads_typed((enc, val))
            return out
        async def aset(self, pairs):
            for fk, (v, ttl) in pairs.items():
                ns, k = self._k(fk); enc, val = self.serde.dumps_typed(v)
                await self._x("INSERT INTO node_cache(ns,key,enc,val,expires) VALUES(%s,%s,%s,%s, CASE WHEN %s::int IS NULL THEN NULL ELSE now()+%s::int*interval '1 second' END) "
                              "ON CONFLICT(ns,key) DO UPDATE SET enc=EXCLUDED.enc, val=EXCLUDED.val, expires=EXCLUDED.expires", (ns, k, enc, val, ttl, ttl))
        async def aclear(self, namespaces=None):
            if namespaces is None: await self._x("DELETE FROM node_cache")
            else:
                for n in namespaces: await self._x("DELETE FROM node_cache WHERE ns=%s", (".".join(n),))
        def get(self, keys): return asyncio.run(self.aget(keys))
        def set(self, pairs): asyncio.run(self.aset(pairs))
        def clear(self, namespaces=None): asyncio.run(self.aclear(namespaces))
    return _PgCache()

# ---------------------------------------------------------------- pluggable checkpointer
def checkpointer_spec() -> dict:
    env = (os.environ.get("AOS_CHECKPOINTER") or "").strip(); src = "env"
    spec = env or file_config().checkpointer;
    if not env: src = "aos.json" if spec else "default"
    if not spec or spec == "postgres": return {"kind": "postgres", "source": src}
    if spec == "memory": return {"kind": "memory", "source": src}
    if isinstance(spec, str) and spec.startswith("sqlite:"):
        p = spec[len("sqlite:"):].removeprefix("//") or "checkpoints.sqlite"
        return {"kind": "sqlite", "path": p, "source": src}
    path = spec["path"] if isinstance(spec, dict) else (spec[len("custom:"):] if isinstance(spec, str) and spec.startswith("custom:") else spec)
    if isinstance(path, str) and ":" in path: return {"kind": "custom", "spec": path, "source": src}
    raise ValueError(f"unknown checkpointer {spec!r}: use postgres | sqlite:///path.db | memory | custom:<file.py|module>:<async context manager function>")

def _load_callable(spec: str):
    mod, _, fn = spec.rpartition(":")
    if mod.endswith(".py") or "/" in mod or "\\" in mod:
        p = Path(mod)
        if not p.is_absolute(): p = (config_path().parent / p)
        s = importlib.util.spec_from_file_location("aos_user_" + re.sub(r"\W", "_", p.stem), p)
        m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    else: m = importlib.import_module(mod)
    return getattr(m, fn)

async def validate_saver(saver) -> None:
    """Base-operation smoke test (put / get / list / delete_thread) — the subset of the LangGraph conformance suite the server needs. Raises on failure."""
    from langgraph.checkpoint.base import empty_checkpoint, BaseCheckpointSaver
    if not isinstance(saver, BaseCheckpointSaver): raise TypeError("the custom checkpointer must yield a langgraph BaseCheckpointSaver")
    tid = "aos-smoke-" + uuid.uuid4().hex[:8]; cp = empty_checkpoint(); cfg = {"configurable": {"thread_id": tid, "checkpoint_ns": ""}}
    nxt = await saver.aput(cfg, cp, {"source": "input", "step": 0}, {})
    t = await saver.aget_tuple(nxt)
    if not t or t.checkpoint["id"] != cp["id"]: raise RuntimeError("checkpointer: put/get round trip failed")
    if not [x async for x in saver.alist(cfg)]: raise RuntimeError("checkpointer: list returned nothing")
    await saver.adelete_thread(tid)
    if await saver.aget_tuple(cfg): raise RuntimeError("checkpointer: delete_thread did not delete")

@contextlib.asynccontextmanager
async def open_checkpointer(uri: str, serde=None):
    s = checkpointer_spec(); k = s["kind"]
    if k == "postgres":
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        async with AsyncPostgresSaver.from_conn_string(uri, serde=serde) as cp: yield cp
    elif k == "sqlite":
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        p = Path(s["path"]); p.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(str(p)) as conn:
            cp = AsyncSqliteSaver(conn, serde=serde); await cp.setup(); yield cp
    elif k == "memory":
        from langgraph.checkpoint.memory import InMemorySaver
        LOG.warning("checkpointer=memory: thread state is lost on restart (development only)")
        yield InMemorySaver(serde=serde) if serde else InMemorySaver()
    else:
        fn = _load_callable(s["spec"])
        async with fn() as cp:
            await validate_saver(cp); yield cp

async def setup_saver(cp):
    f = getattr(cp, "setup", None)
    if f:
        r = f()
        if asyncio.iscoroutine(r): await r

# ---------------------------------------------------------------- OpenAPI securitySchemes
SCHEMES = {
    "ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-Api-Key", "description": "Agent OS API key (`aos_…`, Settings > API keys), or an agent service key (`aosagent_…`)."},
    "BearerAuth": {"type": "http", "scheme": "bearer", "description": "`Authorization: Bearer aos_…` (API key) or a JWT from your identity provider (Settings > SSO, when API tokens are enabled)."},
    "BasicAuth": {"type": "http", "scheme": "basic", "description": "Username and password (not available for accounts with 2-factor authentication)."},
    "SessionCookie": {"type": "apiKey", "in": "cookie", "name": "aos_s", "description": "Browser session cookie set by /login."},
    "GatewayKey": {"type": "http", "scheme": "bearer", "bearerFormat": "vk-…", "description": "LLM Gateway virtual key (`Authorization: Bearer vk-…`) for /v1/*."},
    "IngestKey": {"type": "apiKey", "in": "header", "name": "X-Api-Key", "description": "Tracing project ingest key for /ingest/* and /otel/*."},
}
DEFAULT_SECURITY = [{"ApiKeyAuth": []}, {"BearerAuth": []}, {"BasicAuth": []}, {"SessionCookie": []}]

def install_openapi(app):
    import admin
    orig = app.openapi
    def openapi():
        if app.openapi_schema: return app.openapi_schema
        sch = orig()
        comp = sch.setdefault("components", {}).setdefault("securitySchemes", {}); comp.update(SCHEMES)
        extra = file_config().auth.openapi or {}
        if extra.get("securitySchemes"): comp.update(extra["securitySchemes"])
        sec = extra.get("security") or DEFAULT_SECURITY
        sch["security"] = sec
        pub = tuple(p for p in admin.PUBLIC_PREFIX if p not in ("/v1/", "/ingest/", "/otel/v1/"))
        for path, item in sch.get("paths", {}).items():
            for m, op in item.items():
                if not isinstance(op, dict): continue
                if path.startswith("/v1/"): op["security"] = [{"GatewayKey": []}]
                elif path.startswith(("/ingest/", "/otel/")): op["security"] = [{"IngestKey": []}, {"BearerAuth": []}]
                elif path.startswith(pub): op["security"] = []
        sch.setdefault("info", {})["description"] = ((sch["info"].get("description") or "") + "\n\nAuthentication: send `X-Api-Key: aos_…` (or Bearer / Basic / session cookie). See /api/server-docs/auth.").strip()
        app.openapi_schema = sch; return sch
    app.openapi = openapi

# ---------------------------------------------------------------- docs pages (markdown files in app/docs, SERVER_<SLUG>.md)
def _slugs() -> dict:
    return {f.stem.removeprefix("SERVER_").lower().replace("_", "-"): f for f in sorted(DOCS_DIR.glob("SERVER_*.md"))}

@router.get("/api/server-docs")
async def docs_list():
    out = []
    for s, f in _slugs().items():
        t = f.read_text(encoding="utf-8"); out.append({"slug": s, "title": (re.search(r"^#\s+(.+)$", t, re.M) or [None, s])[1], "bytes": len(t.encode())})
    return {"pages": out}

@router.get("/api/server-docs/{slug}")
async def docs_get(slug: str, format: str = "md"):
    f = _slugs().get(slug.lower())
    if not f: raise HTTPException(404, "No such page")
    t = f.read_text(encoding="utf-8")
    if format == "html":
        from markdown_it import MarkdownIt
        return HTMLResponse(MarkdownIt("commonmark", {"html": False}).enable("table").render(t))   # html=False: raw HTML in the markdown is escaped
    return PlainTextResponse(t, media_type="text/markdown; charset=utf-8")
