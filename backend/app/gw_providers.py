"""GĐ11.1 LLM provider registry per workspace: OpenAI / Anthropic / Gemini / any OpenAI-compatible server (DeepSeek, Groq, Ollama, ...).
The API key is a ws_secrets row (AES-GCM envelope, allowed_hosts = the host of base_url, set automatically); it is write-only here: responses carry only the last 4 chars.
base_url goes through netguard purpose "model_provider" (https; internal addresses only when the operator listed them in AOS_EGRESS_ALLOW) on create, update, test and every call.
The two `.env` keys (GOOGLE_API_KEY / OPENAI_API_KEY) are NOT stored: system_providers() lists them read-only (no key shown). Admin-only API; the demo workspace stays Gemini-only."""
import json, os, re, time
from urllib.parse import urlparse
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import netguard, ws_secrets, gw_router

router = APIRouter()
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
RESERVED = ("aos", "custom")
COLS = "id, kind, name, base_url, secret_name, enabled, status, last_test_at, last_test_result, created_by, created_at"

# kind -> env key name (also the ws_secrets name of the first provider of that kind), default base_url, auth style, cheapest model for the paid completion test
KINDS = {
    "openai": {"env": "OPENAI_API_KEY", "base_url": "https://api.openai.com/v1", "auth": "bearer", "test_model": "gpt-5-nano"},
    "anthropic": {"env": "ANTHROPIC_API_KEY", "base_url": "https://api.anthropic.com", "auth": "x-api-key", "test_model": "claude-haiku-5-5"},
    "gemini": {"env": "GOOGLE_API_KEY", "base_url": "https://generativelanguage.googleapis.com", "auth": "x-goog-api-key", "test_model": "gemini-3.1-flash-lite"},
    "openai_compat": {"env": "OPENAI_COMPAT_API_KEY", "base_url": None, "auth": "bearer", "test_model": None},
}
# base URLs of OpenAI-compatible servers (DeepSeek checked 26/10/2026 in GD11_RESEARCH; the others are the vendors' documented defaults, not re-verified; the admin can edit them).
# DeepSeek goes through openai_compat (langchain-deepseek is not installed). Local servers are reachable only if the operator allow-lists them (AOS_EGRESS_ALLOW).
PRESETS = {
    "deepseek": "https://api.deepseek.com", "groq": "https://api.groq.com/openai/v1", "mistral": "https://api.mistral.ai/v1", "xai": "https://api.x.ai/v1",
    "fireworks": "https://api.fireworks.ai/inference/v1", "ollama": "http://localhost:11434/v1", "vllm": "http://localhost:8000/v1",
    "lmstudio": "http://localhost:1234/v1", "litellm": "http://localhost:4000/v1",
}
MSG_INTERNAL = "Địa chỉ nội bộ chỉ dùng được khi nhà vận hành đã cho phép trên máy chủ"

async def _q(*a, **k):   # lazy import: unit tests need not load the whole app
    import deepagent_api as A
    return await A.q(*a, **k)

def _admin():
    import admin; u = admin.cur()
    if not u or u.get("role") != "admin" or u.get("demo"): raise HTTPException(403, "Chỉ admin quản lý nhà cung cấp mô hình")
    return u

def _norm_url(u):
    u = (u or "").strip().rstrip("/")
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password or p.query or p.fragment: raise HTTPException(422, "base_url phải là địa chỉ http(s) hợp lệ, không chứa tài khoản/mật khẩu")
    return u

async def _guard(url):
    """netguard check of a base_url; 400 with a user-facing text (no URL echoed)."""
    try: await netguard.acheck(url, "model_provider")
    except netguard.NetBlocked as e:
        raise HTTPException(400, MSG_INTERNAL if "internal" in e.reason or "deny list" in e.reason else "Địa chỉ không dùng được: " + e.reason) from None

def secret_name_for(kind, name):
    base = KINDS[kind]["env"]
    return base if name == kind else f"{base}_{name.upper().replace('-', '_')}"[:64]

def _host(url): return (urlparse(url).hostname or "").lower()

def _clean(text, key=None, n=160):
    t = str(text or "")
    if key: t = t.replace(key, "***")
    return re.sub(r"(sk|key|AIza)[-_A-Za-z0-9]{12,}", "***", t)[:n]

def _last4(v): return v[-4:] if v and len(v) >= 12 else None

def system_providers():
    """Read-only entries for the .env keys (never stored, never shown)."""
    out = []
    for kind, env in (("gemini", "GOOGLE_API_KEY"), ("openai", "OPENAI_API_KEY")):
        if os.environ.get(env): out.append({"id": None, "kind": kind, "name": kind, "base_url": KINDS[kind]["base_url"], "enabled": True, "status": "system",
                                            "system": True, "has_key": True, "key_last4": None, "last_test_at": None, "last_test_result": None, "created_by": None, "created_at": None})
    return out

async def _keys(ws, rows):
    names = [r["secret_name"] for r in rows if r.get("secret_name")]
    if not names: return {}
    out = {}
    for s in await _q("SELECT name, value_enc FROM ws_secrets WHERE workspace_id=%s AND name = ANY(%s)", (ws, names)):
        try: out[s["name"]] = ws_secrets.open_value(s["value_enc"], ws, s["name"])
        except Exception: out[s["name"]] = None   # noqa: BLE001  (unreadable key: shown as 'no key', never an error text)
    return out

def _public(r, keys):
    k = keys.get(r.get("secret_name"))
    d = {c: r[c] for c in ("id", "kind", "name", "base_url", "enabled", "status", "last_test_at", "last_test_result", "created_by", "created_at")}
    return {**d, "system": False, "has_key": bool(r.get("secret_name")) and k is not None, "key_last4": _last4(k)}

async def _row(pid, ws):
    r = await _q(f"SELECT {COLS} FROM gw_providers WHERE id=%s AND workspace_id=%s", (pid, ws), one=True)
    if not r: raise HTTPException(404, "Không có nhà cung cấp này")
    return r

async def _put_secret(ws, name, key, host, user):
    await _q("""INSERT INTO ws_secrets(workspace_id,name,value_enc,allowed_hosts,created_by) VALUES(%s,%s,%s,%s,%s)
      ON CONFLICT(workspace_id,name) DO UPDATE SET value_enc=EXCLUDED.value_enc, allowed_hosts=EXCLUDED.allowed_hosts, updated_at=now()""",
             (ws, name, ws_secrets.seal_value(ws, name, key), [host], user))

async def credentials(ws, name):
    """-> (api_key, base_url) for provider `name` of workspace `ws` (a gw_providers name, or the system 'gemini' / 'openai' from .env).
    Missing / disabled -> LookupError. Internal base_url not allow-listed -> netguard.NetBlocked. The demo workspace gets only Gemini (PermissionError)."""
    import admin, gateway
    if int(ws) in gateway.DEMO_WS and name != "gemini": raise PermissionError("Workspace demo chỉ dùng Gemini")
    async with admin.as_workspace(ws):
        row = await _q(f"SELECT {COLS} FROM gw_providers WHERE workspace_id=%s AND name=%s AND enabled", (int(ws), name), one=True)
        if row:
            await netguard.acheck(row["base_url"], "model_provider")
            key = await ws_secrets.get_secret(row["secret_name"], int(ws), _host(row["base_url"])) if row["secret_name"] else ""
            return key, row["base_url"]
    k = KINDS.get(name)
    if k and name in ("gemini", "openai") and os.environ.get(k["env"]): return os.environ[k["env"]], k["base_url"]
    raise LookupError(f"Chưa cấu hình nhà cung cấp {name}")

# ---------------- talking to the provider
def _headers(kind, key):
    a = KINDS[kind]["auth"]
    if not key: return {}
    if a == "bearer": return {"Authorization": "Bearer " + key}
    if a == "x-api-key": return {"x-api-key": key, "anthropic-version": "2023-06-01"}
    return {"x-goog-api-key": key}

def _models_url(kind, base): return base + {"openai": "/models", "openai_compat": "/models", "anthropic": "/v1/models", "gemini": "/v1beta/models"}[kind]

def _model_ids(kind, js):
    if kind == "gemini": return [str(m.get("name", "")).removeprefix("models/") for m in (js.get("models") or []) if isinstance(m, dict)]
    return [str(m.get("id")) for m in (js.get("data") or []) if isinstance(m, dict) and m.get("id")]

async def _call(row, key, method, url, **kw):
    """One request under netguard (every hop re-checked, no redirects). -> httpx.Response. NetBlocked / transport errors propagate."""
    async with netguard.client("model_provider", timeout=20) as h:
        return await h.request(method, url, headers=_headers(row["kind"], key), **kw)

async def _key_of(row, ws):
    return await ws_secrets.get_secret(row["secret_name"], ws, _host(row["base_url"])) if row["secret_name"] else ""

async def list_models(row, ws):
    """-> (ok, detail, [ids]); detail never contains the key."""
    key = await _key_of(row, ws)
    try:
        await _guard(row["base_url"])
        r = await _call(row, key, "GET", _models_url(row["kind"], row["base_url"]))
    except HTTPException as e: return False, str(e.detail), []
    except Exception as e: return False, "Không kết nối được: " + _clean(getattr(e, "reason", None) or type(e).__name__, key, 100), []   # noqa: BLE001
    if r.status_code in (401, 403): return False, f"Khóa bị từ chối (HTTP {r.status_code})", []
    if r.status_code != 200: return False, f"HTTP {r.status_code}", []
    try: return True, "ok", _model_ids(row["kind"], r.json())
    except Exception: return False, "Phản hồi không đúng dạng", []   # noqa: BLE001

async def _completion(row, key, model):
    kind, base = row["kind"], row["base_url"]
    if kind == "anthropic": url, body = base + "/v1/messages", {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": "ping"}]}
    elif kind == "gemini": url, body = f"{base}/v1beta/models/{model}:generateContent", {"contents": [{"parts": [{"text": "ping"}]}], "generationConfig": {"maxOutputTokens": 16}}
    else: url, body = base + "/chat/completions", {"model": model, "messages": [{"role": "user", "content": "ping"}], ("max_completion_tokens" if kind == "openai" else "max_tokens"): 16}
    return await _call(row, key, "POST", url, json=body)

# ---------------- API
class ProviderIn(BaseModel):
    kind: str | None = None
    preset: str | None = None
    name: str | None = Field(default=None, max_length=32)
    base_url: str | None = Field(default=None, max_length=300)
    api_key: str | None = Field(default=None, min_length=1, max_length=8000)
    enabled: bool = True

class ProviderUpd(BaseModel):
    base_url: str | None = Field(default=None, max_length=300)
    api_key: str | None = Field(default=None, min_length=1, max_length=8000)
    enabled: bool | None = None

class TestIn(BaseModel):
    confirm: bool = False
    model: str | None = Field(default=None, max_length=100)

@router.get("/api/gw/providers")
async def list_providers():
    import admin
    ws = admin.cur_ws()
    rows = await _q(f"SELECT {COLS} FROM gw_providers WHERE workspace_id=%s ORDER BY name", (ws,))
    keys = await _keys(ws, rows)
    own = {r["name"] for r in rows}
    return {"providers": [_public(r, keys) for r in rows] + [s for s in system_providers() if s["name"] not in own],
            "kinds": {k: {"base_url": v["base_url"], "test_model": v["test_model"]} for k, v in KINDS.items()}, "presets": PRESETS}

@router.post("/api/gw/providers")
async def create_provider(b: ProviderIn):
    u = _admin(); ws = u["ws"]
    kind = b.kind
    if b.preset:
        if b.preset not in PRESETS: raise HTTPException(422, "Preset không có: " + ", ".join(PRESETS))
        if kind not in (None, "openai_compat"): raise HTTPException(422, "Preset chỉ dùng với kind openai_compat")
        kind = "openai_compat"
    if kind not in KINDS: raise HTTPException(422, "kind phải là một trong: " + ", ".join(KINDS))
    name = (b.name or b.preset or kind).strip().lower()
    if not NAME_RE.match(name) or name in RESERVED or (name in KINDS and name != kind): raise HTTPException(422, "Tên: chữ thường, số, _ - (tối đa 32), không trùng tên dành riêng")
    base = _norm_url(b.base_url or (PRESETS[b.preset] if b.preset else None) or KINDS[kind]["base_url"] or "")
    if kind != "openai_compat" and not b.api_key: raise HTTPException(422, "Cần api_key")
    await _guard(base)
    if await _q("SELECT 1 FROM gw_providers WHERE workspace_id=%s AND name=%s", (ws, name), one=True): raise HTTPException(409, "Tên nhà cung cấp đã tồn tại")
    sname = secret_name_for(kind, name) if b.api_key else None
    if sname and await _q("SELECT 1 FROM ws_secrets WHERE workspace_id=%s AND name=%s", (ws, sname), one=True): raise HTTPException(409, "Đã có bí mật cùng tên khóa; xóa hoặc đổi tên nhà cung cấp")
    if sname: await _put_secret(ws, sname, b.api_key, _host(base), u["username"])
    try:
        r = await _q(f"INSERT INTO gw_providers(workspace_id,kind,name,base_url,secret_name,enabled,created_by) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING {COLS}",
                     (ws, kind, name, base, sname, b.enabled, u["username"]), one=True)
    except Exception:
        if sname: await _q("DELETE FROM ws_secrets WHERE workspace_id=%s AND name=%s", (ws, sname))
        raise HTTPException(409, "Không tạo được nhà cung cấp") from None
    gw_router.invalidate(ws)
    return _public(r, {sname: b.api_key} if sname else {})

@router.patch("/api/gw/providers/{pid}")
async def update_provider(pid: int, b: ProviderUpd):
    u = _admin(); ws = u["ws"]; r = await _row(pid, ws)
    if b.base_url is None and b.api_key is None and b.enabled is None: raise HTTPException(422, "Không có gì để đổi")
    base = _norm_url(b.base_url) if b.base_url is not None else r["base_url"]
    changed = base != r["base_url"] or b.api_key is not None
    if base != r["base_url"]:
        await _guard(base)
        if r["secret_name"] and not b.api_key: raise HTTPException(422, "Đổi địa chỉ thì phải nhập lại khóa API (khóa chỉ gửi tới đúng máy đã đăng ký)")
    sname = r["secret_name"]
    if b.api_key:
        sname = sname or secret_name_for(r["kind"], r["name"])
        if not r["secret_name"] and await _q("SELECT 1 FROM ws_secrets WHERE workspace_id=%s AND name=%s", (ws, sname), one=True): raise HTTPException(409, "Đã có bí mật cùng tên khóa")
        await _put_secret(ws, sname, b.api_key, _host(base), u["username"])
    n = await _q(f"UPDATE gw_providers SET base_url=%s, secret_name=%s, enabled=coalesce(%s,enabled), status=CASE WHEN %s THEN 'untested' ELSE status END WHERE id=%s AND workspace_id=%s RETURNING {COLS}",
                 (base, sname, b.enabled, changed, pid, ws), one=True)
    gw_router.invalidate(ws)
    return _public(n, await _keys(ws, [n]))

@router.delete("/api/gw/providers/{pid}")
async def delete_provider(pid: int):
    u = _admin(); ws = u["ws"]; r = await _row(pid, ws)
    await _q("DELETE FROM gw_providers WHERE id=%s AND workspace_id=%s", (pid, ws)); gw_router.invalidate(ws)
    if r["secret_name"]: await _q("DELETE FROM ws_secrets WHERE workspace_id=%s AND name=%s AND NOT EXISTS (SELECT 1 FROM gw_providers p WHERE p.workspace_id=%s AND p.secret_name=%s)", (ws, r["secret_name"], ws, r["secret_name"]))
    return {"ok": True}

@router.post("/api/gw/providers/{pid}/test")
async def test_provider(pid: int, b: TestIn | None = None):
    """Free check = GET /models. A completion (<= 16 tokens, costs money) only with {"confirm": true}."""
    u = _admin(); ws = u["ws"]; r = await _row(pid, ws); b = b or TestIn()
    t0 = time.perf_counter()
    ok, detail, ids = await list_models(r, ws)
    out = {"ok": ok, "detail": detail, "models": len(ids)}
    if ok and b.confirm:
        key = await _key_of(r, ws); model = b.model or KINDS[r["kind"]]["test_model"] or (ids[0] if ids else None)
        if not model: ok, out["detail"] = False, "Cần chỉ định model để thử"
        else:
            try:
                resp = await _completion(r, key, model)
                ok = resp.status_code == 200; out["detail"] = "ok" if ok else (f"Khóa bị từ chối (HTTP {resp.status_code})" if resp.status_code in (401, 403) else f"HTTP {resp.status_code}: " + _clean(resp.text, key, 120))
            except Exception as e: ok, out["detail"] = False, "Không kết nối được: " + _clean(getattr(e, "reason", None) or type(e).__name__, key, 100)   # noqa: BLE001
        out["ok"] = ok; out["completion"] = True
    out["latency_ms"] = int((time.perf_counter() - t0) * 1000)
    await _q("UPDATE gw_providers SET status=%s, last_test_at=now(), last_test_result=%s WHERE id=%s AND workspace_id=%s",
             ("ok" if out["ok"] else "failed", _clean(("ok" if out["ok"] else out["detail"]), None, 200), pid, ws))
    return out

@router.get("/api/gw/providers/{pid}/models")
async def provider_models(pid: int):
    u = _admin(); ws = u["ws"]; r = await _row(pid, ws)
    ok, detail, ids = await list_models(r, ws)
    if not ok: raise HTTPException(502, detail)
    return {"models": [{"id": i} for i in ids]}

# ---------------- GĐ11.3: building chat models (init_chat_model) for every provider
ProviderKeyMissing = gw_router.ProviderKeyMissing
CRED: dict = {}          # (ws, canonical ref) -> (loaded_at, api_key, base_url); filled by prepare(), read by the sync gateway._model()
_CRED_TTL = 30
LC_PROVIDER = {"openai": "openai", "openai_compat": "openai", "anthropic": "anthropic", "gemini": "google_genai"}   # kind -> init_chat_model provider
_NAMES = {"openai": "OpenAI", "anthropic": "Anthropic", "gemini": "Gemini"}
_WARNED: set = set()
log = __import__("logging").getLogger("aos.gwprov")

def row_of(res, ws):
    """The workspace's gw_providers row serving this resolved ref (also for legacy bare names: `gpt-*` uses the workspace's own `openai` row when it has one), from the router cache."""
    if res.row is not None: return res.row
    return ((gw_router._ROWS.get(int(ws)) or (0, {}))[1]).get(res.provider) if ws else None

def _missing(name): return ProviderKeyMissing(f"Thiếu khoá {_NAMES.get(name, name)}")

async def prepare(ref):
    """Call BEFORE gateway._model(ref) (async; _model stays sync). Resolves the ref in the calling workspace, checks that the provider is enabled and has a key
    (ProviderKeyMissing -> 424) and warms CRED. Gemini / OpenAI without a workspace row keep using the .env key (nothing to do). No-op under AOS_FAKE_LLM=1."""
    if os.environ.get("AOS_FAKE_LLM") == "1": return None
    import gateway as gw
    ws = gw._cur_ws()
    try: rows = await gw_router.rows_for(ws) if ws else None
    except Exception: rows = None   # noqa: BLE001  (no database: in-process tests; Gemini / OpenAI then use the .env key, other providers are reported as missing)
    try: res = gw_router.resolve(ref, ws, rows)
    except gw_router.ResolveError:
        if "/" in ref: raise _missing(ref.split("/", 1)[0][:40]) from None   # unknown provider prefix
        return None   # a bare/unknown name keeps the legacy Gemini path
    if res.kind != "gemini" and gw.gemini_only(): raise gw.DemoOpenAIBlocked(f"demo: {res.provider} blocked (Gemini only)")
    row = row_of(res, ws)
    if row is None and res.kind in ("gemini", "openai"): return res
    if row is not None and not row.get("enabled", True): raise ProviderKeyMissing(f"Nhà cung cấp {res.provider} đang tắt")
    if not ws: raise _missing(res.provider)
    hit = CRED.get((ws, res.ref))
    if hit and time.time() - hit[0] < _CRED_TTL: return res
    try: key, base = await credentials(ws, res.provider)
    except LookupError: raise _missing(res.provider) from None
    if not key and res.kind != "openai_compat": raise _missing(res.provider)
    CRED[(ws, res.ref)] = (time.time(), key, base)
    return res

def _guard_cls(cls):
    """Subclass that refuses to send anything while gateway.gemini_only() (demo user / demo workspace): no paid non-Gemini call, whatever path reaches the model."""
    import gateway as gw
    sub = _GUARDED.get(cls)
    if sub is None:
        class Guarded(cls):
            def _check(self):
                if gw.gemini_only(): raise gw.DemoOpenAIBlocked(f"demo: model {getattr(self, 'model_name', None) or getattr(self, 'model', '')} blocked (Gemini only)")
            def _generate(self, *a, **k): self._check(); return super()._generate(*a, **k)
            async def _agenerate(self, *a, **k): self._check(); return await super()._agenerate(*a, **k)
            def _stream(self, *a, **k): self._check(); return super()._stream(*a, **k)
            async def _astream(self, *a, **k):
                self._check()
                async for x in super()._astream(*a, **k): yield x
        Guarded.__name__ = Guarded.__qualname__ = "Guarded" + cls.__name__; sub = _GUARDED[cls] = Guarded
    return sub
_GUARDED: dict = {}

def _allowed(cls):
    names = set(cls.model_fields)
    for f in cls.model_fields.values():
        for a in (f.alias, f.validation_alias):
            if isinstance(a, str): names.add(a)
    return names

def _model_limits(kind, model, p):
    """Claude 5.x rejects what langchain-anthropic 1.7 refuses to send: Sonnet 5.5 / Fable 5 take no non-default temperature / top_p / top_k; Opus / Sonnet / Fable 5.x take no
    `thinking: enabled` with a budget (adaptive thinking + reasoning_effort instead). Such keys are dropped (the caller's WARNING path), not sent."""
    if kind != "anthropic": return p
    bad = set()
    if model.startswith(("claude-sonnet-5-5", "claude-fable-5")): bad |= {"temperature", "top_k", "top_p"}
    th = p.get("thinking")
    if model.startswith(("claude-opus-5", "claude-sonnet-5-5", "claude-fable-5")) and isinstance(th, dict) and th.get("type") == "enabled": bad.add("thinking")
    for k in bad & set(p):
        if (kind, model[:14], k) not in _WARNED: _WARNED.add((kind, model[:14], k)); log.warning("parameter %r is not supported by %s: dropped", k, model[:40])
    return {k: v for k, v in p.items() if k not in bad}

def _clean_params(cls, kind, model, params):
    """Provider-specific renames, then drop what the chat class does not know (one WARNING per key, never an error)."""
    p = {k: v for k, v in (params or {}).items() if v is not None and k != "extra_params"}
    x = (params or {}).get("extra_params")
    if isinstance(x, str):
        try: x = json.loads(x)
        except Exception: x = None   # noqa: BLE001
    if isinstance(x, dict): p.update({k: v for k, v in x.items() if v is not None})
    p.pop("cache_ttl", None)   # 11.10: read by gateway.cache_ttl, not a model field
    if kind == "gemini" and "safety" in p: p["safety_settings"] = p.pop("safety")
    if kind in ("openai", "openai_compat") and "parallel_tool_calls" in p: p.setdefault("model_kwargs", {})["parallel_tool_calls"] = p.pop("parallel_tool_calls")
    p = _model_limits(kind, model, p)
    ok = _allowed(cls); out = {}
    for k, v in p.items():
        if k in ok: out[k] = v
        elif (kind, k) not in _WARNED: _WARNED.add((kind, k)); log.warning("parameter %r is not supported by provider kind %s: dropped", k, kind)
    return out

def build_model(res, params=None, cred=None):
    """init_chat_model("<kind>:<model>", api_key, base_url, **params). cred = (loaded_at, api_key, base_url) from prepare(); None = .env key (Gemini / OpenAI, exactly as before 11.3).
    params: temperature, max_tokens, reasoning_effort, service_tier, use_responses_api, parallel_tool_calls (OpenAI); top_k, thinking (Anthropic); top_k, safety (Gemini); extra_params (JSON/dict)."""
    import gateway as gw
    from langchain.chat_models import init_chat_model
    kind, model, p = res.kind, res.model, dict(params or {})
    kw = {"max_retries": 0, "timeout": p.pop("timeout", None) or 120}   # 11.13: header_timeout_s of a fallback chain row
    if kind == "gemini":
        if cred:
            kw["api_key"] = cred[1]
            if cred[2].rstrip("/") != KINDS["gemini"]["base_url"]: kw["base_url"] = cred[2]
    elif kind == "anthropic":
        kw["max_tokens"] = int(p.pop("max_tokens", None) or 4096)
        if cred: kw["api_key"] = cred[1]; kw["base_url"] = cred[2]
        if p.get("thinking"): p.pop("temperature", None)
    else:
        kw["stream_usage"] = True
        if kind == "openai": kw["max_completion_tokens"] = int(p.pop("max_tokens", None) or gw.MAX_TOKENS)
        if cred: kw["api_key"] = cred[1] or "none"; kw["base_url"] = cred[2]
        if kind == "openai" and model.startswith("gpt-5"): p.pop("temperature", None)   # gpt-5 family: default temperature only
    spec = f"{LC_PROVIDER[kind]}:{model}"
    m = init_chat_model(spec, **kw)
    extra = _clean_params(type(m), kind, model, p)   # constructor kwargs (aliases such as api_key work), so rebuild once the class is known
    if extra: m = init_chat_model(spec, **kw, **extra)
    if kind != "gemini":
        sub = _guard_cls(type(m)); m = m.model_copy(); object.__setattr__(m, "__class__", sub)
    return m

# ---------------- GĐ11.16: price table for the models of the providers enabled in the caller's workspace
PRICE_FIELDS = ("input_per_1m", "output_per_1m", "cached_input_per_1m", "cache_write_per_1m", "cache_write_1h_per_1m", "long_context_tokens", "input_per_1m_long", "output_per_1m_long", "note", "source_url", "checked")

def _price_provider(key):
    """Provider name a PRICING key belongs to (`deepseek/x` -> deepseek, bare names by the legacy rule). Not gw_router.resolve: a provider this workspace lacks must NOT fall back to Gemini."""
    return key.split("/", 1)[0] if "/" in key else gw_router.legacy_kind(key)

@router.get("/api/gw/prices")
async def prices():
    """Rows only for models whose provider is enabled here (+ the .env system providers; demo = Gemini only). `unpriced` = models this workspace used in the last 30 days
    on an enabled provider that have no price (their cost stays None)."""
    import admin, gateway as gw
    ws = admin.cur_ws()
    rows = await _q("SELECT name, enabled FROM gw_providers WHERE workspace_id=%s", (ws,))
    on = {r["name"] for r in rows if r["enabled"]} | {s["name"] for s in system_providers()}
    if int(ws) in gw.DEMO_WS: on &= {"gemini"}
    out = [{"model": k, "provider": _price_provider(k), **{f: v[f] for f in PRICE_FIELDS if f in v}} for k, v in sorted(gw.PRICING.items()) if _price_provider(k) in on]
    used = await _q("SELECT DISTINCT model FROM gateway_usage WHERE workspace_id=%s AND ts > now() - interval '30 days' AND model IS NOT NULL", (ws,))
    unpriced = sorted({gw._cn(r["model"]) for r in used if _price_provider(gw._cn(r["model"])) in on and gw._cn(r["model"]) not in gw.PRICING and not str(r["model"]).startswith("fake")})
    return {"prices": out, "unpriced": unpriced, "meta": gw.PRICING_SOURCE}
