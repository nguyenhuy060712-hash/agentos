"""LLM Gateway nội bộ (tự viết, mỏng): model theo agent, chuỗi fallback giữa các model Gemini, retry,
giới hạn RPM & ngân sách/ngày theo agent, ghi usage, và span tracing cho LLM/tool/sub-agent."""
import asyncio, contextvars, json, logging, sys, time, uuid
from datetime import datetime, timedelta, timezone
from collections import deque, defaultdict
from langchain.agents.middleware import AgentMiddleware
from langchain_google_genai import ChatGoogleGenerativeAI
import os

# Giá: app/pricing.json (USD / 1M token; Gemini Paid tier Standard + OpenAI Standard, nguồn & ngày kiểm trong từng model). Cập nhật qua
# POST /api/gateway/pricing/refresh + /apply (pricing.py, đề xuất từ LiteLLM, không tự áp dụng). Trường thiếu → dùng giá input/output.
import pricing as _pricing
PRICING: dict = {}
PRICING_SOURCE: dict = {}
def reload_pricing(path=None):
    d = _pricing.load(path)
    PRICING.clear(); PRICING.update(d["models"]); PRICING_SOURCE.clear(); PRICING_SOURCE.update(d["_meta"])
    OPENAI_MODELS[:] = [k for k, v in PRICING.items() if str(v.get("note", "")).startswith("OpenAI")]
OPENAI_MODELS: list = []
reload_pricing()
import gw_router as _router
def provider(m): return _router.kind_of(m, _cur_ws())   # GĐ11.2: kind of any model ref (legacy bare names unchanged; claude-* -> anthropic; workspace providers via the router cache)
def _cn(m): return _router.canonical(m, _cur_ws())   # canonical ref = COOLDOWN / usage key
def pname(m): return _router.resolve_sync(m, _cur_ws()).provider   # provider NAME of a ref (= kind for the built-in providers): gateway_usage.provider, SPENT
ProviderKeyMissing = _router.ProviderKeyMissing   # GĐ11.3: "Thiếu khoá <nhà cung cấp>", HTTP 424
async def prepare(ref):
    """GĐ11.3: await this BEFORE _model(ref): resolves the provider in the calling workspace, checks enabled + key, warms the credential cache (no-op under AOS_FAKE_LLM=1)."""
    import gw_providers
    return await gw_providers.prepare(ref)
# Hard spend cap per provider (OpenAI key is paid): calls are skipped once the cap is reached; Gemini stays as fallback.
CAPS = {"openai": float(os.getenv("OPENAI_BUDGET_USD", "1.0"))}
SPENT = defaultdict(float)   # seeded from gateway_usage at startup, then updated on every call
MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "1500"))
def under_cap(m): return not _p_blocked(provider(m), _cur_ws())
DEFAULT_FALLBACKS = ["gemini-3.6-flash", "gemini-3-flash-preview", "gemini-3.1-flash-lite"]
RETRYABLE = ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "RateLimit", "500 INTERNAL", "DEADLINE", "Timeout", "DeadlineExceeded")   # GD7.R: a 120s model timeout has an EMPTY message, so match the exception class name (TimeoutError, ReadTimeout, DeadlineExceeded)

_UNPRICED: set = set()
def cost(model, i, o, details=None):
    """USD for one call. i/o = total input/output tokens (cached, audio, image, reasoning tokens are INCLUDED in them, as in OpenAI/Gemini/LangChain usage);
    details = {cached, audio_in, image_in, reasoning, audio_out, image_out} token counts billed at their own price (fallback: plain input/output price)."""
    p = PRICING.get(_cn(model or ""))   # priced by canonical ref (`anthropic/claude-haiku-5-5`, bare Gemini / OpenAI names)
    if not p:
        if model and model not in _UNPRICED and not str(model).startswith("fake"): _UNPRICED.add(model); logging.getLogger("aos.pricing").warning("no price for model %s: cost stays None", model)
        return None
    d = details or {}
    long = (i or 0) > (p.get("long_context_tokens") or 200000)   # GĐ10.26: above the threshold the WHOLE call uses the long prices (if the model has them)
    pin = p["input_per_1m"] if not long or p.get("input_per_1m_long") is None else p["input_per_1m_long"]
    pout = p["output_per_1m"] if not long or p.get("output_per_1m_long") is None else p["output_per_1m_long"]
    def part(total, kinds, base):
        rest, usd = total or 0, 0.0
        for k, field, fb in kinds:
            n = min(max(int(d.get(k) or 0), 0), rest); rest -= n
            pr = p.get(field); usd += n / 1e6 * (base if pr is None else pr)
        return usd + rest / 1e6 * base
    return round(part(i, [("cached", "cached_input_per_1m", pin), ("cache_write", "cache_write_per_1m", pin), ("cache_write_1h", "cache_write_1h_per_1m", pin), ("audio_in", "audio_input_per_1m", 0), ("image_in", "image_input_per_1m", 0)], pin)
                 + part(o, [("reasoning", "reasoning_per_1m", 0), ("audio_out", "audio_output_per_1m", 0), ("image_out", "image_output_per_1m", 0)], pout), 6)

def details_from_usage(u):
    """details for cost() from a LangChain UsageMetadata dict (input_token_details: cache_read/cache_creation/audio; output_token_details: reasoning/audio)."""
    u = u or {}; it, ot = u.get("input_token_details") or {}, u.get("output_token_details") or {}
    d = {"cached": it.get("cache_read"), "cache_write": (it.get("cache_creation") or 0) + (it.get("ephemeral_5m_input_tokens") or 0), "cache_write_1h": it.get("ephemeral_1h_input_tokens"), "audio_in": it.get("audio"), "image_in": it.get("image"), "reasoning": ot.get("reasoning"), "audio_out": ot.get("audio"), "image_out": ot.get("image")}
    return {k: v for k, v in d.items() if v}

def details_from_openai(u):
    """details for cost() from an OpenAI-style usage dict (prompt_tokens_details.cached_tokens/audio_tokens, completion_tokens_details.reasoning_tokens/audio_tokens)."""
    u = u or {}; pt, ct = u.get("prompt_tokens_details") or {}, u.get("completion_tokens_details") or {}
    d = {"cached": pt.get("cached_tokens"), "cache_write": pt.get("cache_write_tokens"), "audio_in": pt.get("audio_tokens"), "reasoning": ct.get("reasoning_tokens"), "audio_out": ct.get("audio_tokens")}
    return {k: v for k, v in d.items() if v}

def details_from_genai(um):
    """details for cost() from a google-genai usage_metadata (cached_content_token_count, thoughts_token_count, prompt_tokens_details by modality)."""
    if um is None: return {}
    d = {"cached": getattr(um, "cached_content_token_count", None), "reasoning": getattr(um, "thoughts_token_count", None)}
    for attr, suf in (("prompt_tokens_details", "in"), ("candidates_tokens_details", "out")):
        for m in getattr(um, attr, None) or []:
            mod = str(getattr(m, "modality", "") or "").upper()
            for key in ("AUDIO", "IMAGE"):
                if key in mod and getattr(m, "token_count", None): d[f"{key.lower()}_{suf}"] = d.get(f"{key.lower()}_{suf}", 0) + m.token_count
    return {k: v for k, v in d.items() if v}

# ---- span tracing (contextvars: lan sang các task con của langgraph) ----
SPANS: contextvars.ContextVar = contextvars.ContextVar("spans", default=None)
PARENT: contextvars.ContextVar = contextvars.ContextVar("parent", default=None)
T0: contextvars.ContextVar = contextvars.ContextVar("t0", default=0.0)
AGENT: contextvars.ContextVar = contextvars.ContextVar("agent", default=None)

def _lc_agent_name():
    """GĐ11.63: name of the (sub-)agent whose graph node is running (`metadata.lc_agent_name`, set by deepagents 0.7.32 on every sub-agent); None for the main agent / outside a graph."""
    try:
        from langgraph.config import get_config
        n = ((get_config() or {}).get("metadata") or {}).get("lc_agent_name")
        return n if isinstance(n, str) and n else None
    except Exception:  # noqa: BLE001  (RuntimeError outside a runnable context)
        return None

def span_open(kind, name, **kw):
    lst = SPANS.get()
    sp = {"id": uuid.uuid4().hex[:8], "parent": PARENT.get(), "kind": kind, "name": name,
          "start_ms": int((time.perf_counter() - T0.get()) * 1000), **kw}
    an = _lc_agent_name()
    if an: sp["lc_agent_name"] = an[:120]
    if lst is not None: lst.append(sp)
    return sp

def span_close(sp, **kw):
    sp["end_ms"] = int((time.perf_counter() - T0.get()) * 1000); sp["ms"] = sp["end_ms"] - sp["start_ms"]; sp.update(kw)

class RateBudget:
    def __init__(self): self.calls = defaultdict(deque)
    async def acquire(self, key, rpm):
        if not rpm: return
        q = self.calls[key]
        for _ in range(120):
            now = time.time()
            while q and now - q[0] > 60: q.popleft()
            if len(q) < rpm: q.append(now); return
            await asyncio.sleep(0.5)
        raise RuntimeError(f"Vượt giới hạn {rpm} yêu cầu/phút")

RATE = RateBudget()
COOLDOWN: dict = {}   # model -> thời điểm hết "nghỉ" sau khi bị 429 (circuit breaker)
COOLDOWN_S = 60

LAST_RESORT = "gemini-flash-lite-latest"   # luôn thêm cuối chuỗi fallback (quota riêng trên free tier)

# ---- Demo = Gemini only (21/10/2026, anh Huy): every model call made for a demo-flagged user, or inside a demo workspace
# (workspaces.demo), goes to Gemini; OpenAI is refused server-side (fail closed: GuardedChatOpenAI raises before any request).
# Latest priced Gemini text model on the #83 key (models.list, checked 21/10/2026): gemini-3.8-flash.
DEMO_MODEL = os.getenv("AOS_DEMO_MODEL", "gemini-3.8-flash")
DEMO_FALLBACKS = ["gemini-3.6-flash", "gemini-3.1-flash-lite"]
DEMO_WS: set = set()                                                         # workspace ids with workspaces.demo=true (admin.load_demo_ws)
RUN_WS: contextvars.ContextVar = contextvars.ContextVar("aos_run_ws", default=None)   # workspace of the run being executed (worker)
class DemoOpenAIBlocked(PermissionError): pass

def gemini_only():
    try:
        import admin as _adm
        u = _adm.USER.get()
        if u and u.get("demo"): return True
        ws = (u or {}).get("ws") or _adm.WS_CTX.get() or RUN_WS.get()
    except Exception:  # noqa: BLE001
        ws = RUN_WS.get()
    return bool(ws and int(ws) in DEMO_WS)

def demo_chain(chain):
    """Gemini-only chain: OpenAI models are replaced by DEMO_MODEL; Gemini models kept; demo fallbacks appended."""
    out = []
    for m in [*(DEMO_MODEL if provider(m) != "gemini" else m for m in chain if m), *DEMO_FALLBACKS]:
        if m not in out: out.append(m)
    return out

class SpendCapReached(RuntimeError):
    """Every model of the chain is over its provider spend cap: nothing was sent (was a bare `raise None` -> silent failed run).
    GĐ8.P: `info` = machine-readable cap ({scope, target, period, limit_usd, used_usd, feature}) for the HTTP 402 detail object (cap_detail)."""
    def __init__(self, msg="", info=None):
        super().__init__(msg); self.info = dict(info or {})

def cap_detail(e_or_msg, info=None) -> dict:
    """HTTP 402 detail for the chat: {"code": "spend_cap", "message": <human sentence>, scope, target, period, limit_usd, used_usd, feature}.
    Clients show their own sentence in the UI language from the fields; `message` stays for older clients and logs."""
    msg = str(e_or_msg); info = {**(getattr(e_or_msg, "info", None) or {}), **(info or {})}
    return {"code": "spend_cap", "message": msg, "feature": FEATURE.get(), **info}

def _cap_info(c):
    return {"scope": c["scope"], "target": str(c["target"]), "period": c["period"], "limit_usd": round(float(c["limit_usd"]), 4), "used_usd": round(float(c["spent"]), 4),
            **({"feature": str(c["target"])} if c["scope"] == "feature" else {})}

def cap_message(chain):
    """None while some model of the chain (as the gateway would try it) is under its provider cap, else the reason for the user."""
    ms = list(chain) + ([LAST_RESORT] if LAST_RESORT not in chain else [])
    if gemini_only(): ms = demo_chain(ms)
    if any(under_cap(m) for m in ms if m): return None
    ps = sorted({provider(m) for m in ms if m})
    return "Đã chạm trần chi phí " + ", ".join(_prov_text(p, _cur_ws()) for p in ps) + ". Liên hệ quản trị viên để nâng trần."

def cap_info(chain) -> dict:
    """Machine-readable twin of cap_message (first provider of the chain); call it only when cap_message returned a reason."""
    ms = list(chain) + ([LAST_RESORT] if LAST_RESORT not in chain else [])
    if gemini_only(): ms = demo_chain(ms)
    ps = sorted({provider(m) for m in ms if m})
    return prov_info(ps[0], _cur_ws()) if ps else {}

# ---- Unified spend caps (GĐ6.1, 22/10/2026): table spend_caps, scope provider|workspace|user|evaluator|feature, period day|week|month|total.
# Spend is summed from gateway_usage (cached CAPS_TTL s). budget_check(ctx) is the single entry point; the legacy provider cap (CAPS/SPENT,
# env OPENAI_BUDGET_USD) keeps working as an always-on "total" provider cap, and env GEMINI_BUDGET_USD_MONTH (unset = off) is a virtual Gemini month cap.
log = logging.getLogger("aos.caps")
CAPS_DDL = """
CREATE TABLE IF NOT EXISTS spend_caps(id SERIAL PRIMARY KEY, workspace_id INT,
  scope TEXT NOT NULL CHECK (scope IN ('provider','workspace','user','evaluator','feature','agent')), target TEXT NOT NULL,
  period TEXT NOT NULL CHECK (period IN ('day','week','month','total')), limit_usd NUMERIC(12,4) NOT NULL CHECK (limit_usd >= 0),
  action TEXT NOT NULL DEFAULT 'block' CHECK (action IN ('block','warn')), enabled BOOLEAN NOT NULL DEFAULT true, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
ALTER TABLE spend_caps DROP CONSTRAINT IF EXISTS spend_caps_scope_check;
ALTER TABLE spend_caps ADD CONSTRAINT spend_caps_scope_check CHECK (scope IN ('provider','workspace','user','evaluator','feature','agent'));   -- GĐ9.P1: scope 'agent' (target = agent id)
CREATE INDEX IF NOT EXISTS gateway_usage_agent ON gateway_usage(agent_id, ts) WHERE agent_id IS NOT NULL;
ALTER TABLE gateway_usage ADD COLUMN IF NOT EXISTS workspace_id INT;
ALTER TABLE gateway_usage ADD COLUMN IF NOT EXISTS user_id INT;
ALTER TABLE gateway_usage ADD COLUMN IF NOT EXISTS evaluator_id INT;
ALTER TABLE gateway_usage ADD COLUMN IF NOT EXISTS feature TEXT;
ALTER TABLE gateway_usage ADD COLUMN IF NOT EXISTS cached_tokens INT;
ALTER TABLE gateway_usage ADD COLUMN IF NOT EXISTS reasoning_tokens INT;
ALTER TABLE gateway_usage ADD COLUMN IF NOT EXISTS audio_tokens INT;
ALTER TABLE gateway_usage ADD COLUMN IF NOT EXISTS image_tokens INT;
CREATE INDEX IF NOT EXISTS gateway_usage_ws ON gateway_usage(workspace_id, ts) WHERE workspace_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS gateway_usage_user ON gateway_usage(user_id, ts) WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS gateway_usage_eval ON gateway_usage(evaluator_id, ts) WHERE evaluator_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS gateway_usage_feat ON gateway_usage(feature, ts) WHERE feature IS NOT NULL;
"""
CAPS_TTL = float(os.getenv("AOS_CAPS_CACHE_S", "10"))
CAPS_TZ = "Asia/Ho_Chi_Minh"
PERIOD_VI = {"day": "mỗi ngày", "week": "mỗi tuần", "month": "mỗi tháng", "total": "tổng cộng"}
SCOPE_VI = {"provider": "", "workspace": "workspace ", "user": "người dùng ", "evaluator": "giám khảo #", "feature": "tính năng ", "agent": "agent #"}
PROVIDER_NAME = {"openai": "OpenAI", "gemini": "Gemini", "anthropic": "Anthropic"}   # GĐ7.Z: one language in the 402 text (was "Openai", raw feature ids)
FEATURE_VI = {"assistant": "Trợ lý", "search": "Tìm trace", "deep_research": "Nghiên cứu sâu", "insights": "Insights", "engine": "Engine", "playground": "Playground",
              "builder": "Tạo agent", "gateway": "LLM Gateway", "evaluator": "Giám khảo", "channel": "Kênh nhắn tin", "prompt_canvas": "Prompt Canvas"}
def _pname(p): return PROVIDER_NAME.get(p, str(p).capitalize())
def feature_vi(f): return FEATURE_VI.get(f, f)
FEATURE: contextvars.ContextVar = contextvars.ContextVar("aos_feature", default=None)      # e.g. playground, builder, gateway, assistant
EVALUATOR: contextvars.ContextVar = contextvars.ContextVar("aos_evaluator", default=None)  # evaluator id of the judge call in progress
_SNAP: dict = {"t": 0.0, "caps": []}   # enabled caps + their current spend (refreshed at most every CAPS_TTL s)
DB_BLOCK: set = set()                  # {(workspace_id|None, provider)} of provider caps (action=block) currently over their limit
_WARNED: dict = {}

class BudgetExceeded(SpendCapReached):
    """A unified spend cap (budget_check) is reached; str(e) is the user-facing reason (server answers HTTP 402)."""

def _cur_ws():
    try:
        import admin as _adm
        u = _adm.USER.get()
        w = (u or {}).get("ws") or _adm.WS_CTX.get() or RUN_WS.get()
    except Exception:  # noqa: BLE001
        w = RUN_WS.get()
    return int(w) if w else None

def ctx_now(provider=None, **over):
    """Who/what is calling right now (request user, workspace of the run, evaluator/feature contextvars) for budget_check and the usage sink."""
    uid = None
    try:
        import admin as _adm
        uid = (_adm.USER.get() or {}).get("id")
    except Exception:  # noqa: BLE001
        pass
    return {"provider": provider, "workspace_id": _cur_ws(), "user_id": uid, "evaluator_id": EVALUATOR.get(), "feature": FEATURE.get(), "agent_id": None, **over}

def _p_blocked(p, ws):
    return SPENT[p] >= CAPS.get(p, float("inf")) or (None, p) in DB_BLOCK or (ws, p) in DB_BLOCK

def prov_info(p, ws=None):
    """Machine-readable twin of _prov_text (the cap that blocks provider p)."""
    if SPENT[p] < CAPS.get(p, float("inf")):
        for c in _SNAP["caps"]:
            if c["scope"] == "provider" and c["target"] == p and c["action"] == "block" and c["workspace_id"] in (None, ws) and c["spent"] >= c["limit_usd"]:
                return _cap_info(c)
    return {"scope": "provider", "target": p, "period": "total", "limit_usd": round(float(CAPS.get(p, 0)), 4), "used_usd": round(float(SPENT[p]), 4)}

def _prov_text(p, ws=None):
    if SPENT[p] < CAPS.get(p, float("inf")):
        for c in _SNAP["caps"]:
            if c["scope"] == "provider" and c["target"] == p and c["action"] == "block" and c["workspace_id"] in (None, ws) and c["spent"] >= c["limit_usd"]:
                return f"{_pname(p)} (đã dùng ${c['spent']:.2f} / trần ${c['limit_usd']:.2f} {PERIOD_VI[c['period']]})"
    return f"{_pname(p)} (đã dùng ${SPENT[p]:.2f} / trần ${CAPS.get(p, 0):.2f})"

def cap_text(c):
    tgt = _pname(c['target']) if c['scope'] == 'provider' else feature_vi(c['target']) if c['scope'] == 'feature' else c['target']
    return f"{SCOPE_VI[c['scope']]}{tgt} (đã dùng ${c['spent']:.2f} / trần ${c['limit_usd']:.2f} {PERIOD_VI[c['period']]})"

def _pool():
    m = sys.modules.get("deepagent_api")
    return (m.S.get("pool") if m else None)

async def _sys(sql, args=(), one=False):
    """Query as the system (RLS off): caps are evaluated across every workspace, whoever is calling."""
    pool = _pool()
    async with pool.connection() as c:
        await c.execute("SELECT set_config('app.ws','*',false), set_config('app.uid','0',false), set_config('app.role','admin',false), set_config('role','none',false)")
        cur = await c.execute(sql, args)
        if cur.description is None: return None
        return await (cur.fetchone() if one else cur.fetchall())

_OPENAI_SQL = "(coalesce(model,'') LIKE 'gpt-%%' OR coalesce(model,'') LIKE 'o3%%' OR coalesce(model,'') LIKE 'o4%%')"
_PROV_SQL = f"coalesce(provider, CASE WHEN {_OPENAI_SQL} THEN 'openai' WHEN coalesce(model,'') LIKE 'claude-%%' THEN 'anthropic' ELSE 'gemini' END)"
_ICT = timezone(timedelta(hours=7))   # Asia/Ho_Chi_Minh (CAPS_TZ): Vietnam has no daylight saving, so a fixed +7 offset is exact and needs no tz database
def period_start(period, now=None):
    """GĐ11.14: UTC instant where the current spend period began, counted in ICT (Vietnam time): day = 00:00 ICT today, week = 00:00 ICT Monday, month = 00:00 ICT on the 1st,
    total = None. `now` (aware or naive UTC datetime) is for tests; the UI shows this boundary next to every cap."""
    if period == "total": return None
    n = now or datetime.now(timezone.utc)
    n = (n.replace(tzinfo=timezone.utc) if n.tzinfo is None else n).astimezone(_ICT)
    d = n.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week": d -= timedelta(days=d.weekday())
    elif period == "month": d = d.replace(day=1)
    elif period != "day": raise ValueError("period: " + str(period))
    return d.astimezone(timezone.utc)

def reached(spent, limit):
    """A cap is reached when spend >= limit. Compared at the 6-decimal precision of cost() so float sums (0.1 + 0.2) cannot slip under an exact limit; a $0 limit is always reached."""
    return round(float(spent), 6) >= round(float(limit), 6)

def spend_query(cap, now=None):
    """(sql, args): USD spent for this cap in its period, from gateway_usage (ICT day/week/month boundaries, see period_start)."""
    w, a = [], []
    st = period_start(cap["period"], now)
    if st is not None: w.append("ts >= %s"); a.append(st)
    sc, tg = cap["scope"], str(cap["target"])
    if sc == "provider": w.append(_PROV_SQL + " = %s"); a.append(tg)   # 11.3: gateway_usage.provider (name); old rows without it: by model name
    elif sc in ("workspace", "user", "evaluator", "agent"):
        col = {"workspace": "workspace_id", "user": "user_id", "evaluator": "evaluator_id", "agent": "agent_id"}[sc]
        if tg.isdigit(): w.append(f"{col} = %s"); a.append(int(tg))
        else: w.append("false")
    else: w.append("feature = %s"); a.append(tg)
    if cap.get("workspace_id") is not None and sc != "workspace": w.append("workspace_id = %s"); a.append(int(cap["workspace_id"]))
    return "SELECT coalesce(sum(cost_usd),0) c FROM gateway_usage" + (" WHERE " + " AND ".join(w) if w else ""), tuple(a)

async def cap_spent(cap, now=None):
    sql, a = spend_query(cap, now)
    return float((await _sys(sql, a, one=True))["c"] or 0)

async def caps_snapshot(fresh=False):
    """Enabled caps with their current spend; cached CAPS_TTL s (pass fresh=True after a write or in tests). No DB pool -> empty (legacy CAPS still apply)."""
    now = time.time()
    if not fresh and now - _SNAP["t"] < CAPS_TTL: return _SNAP["caps"]
    if not _pool(): return _SNAP["caps"]
    _SNAP["t"] = now   # also on failure: do not hammer a broken DB
    try:
        rows = [dict(r) for r in await _sys("SELECT * FROM spend_caps WHERE enabled ORDER BY id")]
        env = os.getenv("GEMINI_BUDGET_USD_MONTH")
        if env:
            rows.append({"id": None, "workspace_id": None, "scope": "provider", "target": "gemini", "period": "month", "limit_usd": float(env), "action": "block", "enabled": True})
        for r in rows:
            r["limit_usd"] = float(r["limit_usd"]); r["spent"] = await cap_spent(r)
        _SNAP["caps"] = rows; _SNAP["err"] = False
        DB_BLOCK.clear(); DB_BLOCK.update((r["workspace_id"], r["target"]) for r in rows if r["scope"] == "provider" and r["action"] == "block" and reached(r["spent"], r["limit_usd"]))
    except Exception as e:  # noqa: BLE001
        _SNAP["err"] = True   # GĐ11.14: the policy engine fails closed on this (it retries after CAPS_TTL s)
        log.warning("spend caps snapshot failed: %s", e)
    return _SNAP["caps"]

async def budget_check(ctx, fresh=False):
    """The single spend gate. GĐ11.14: a thin wrapper of `gw_policy_engine.evaluate` (model allow-list -> feature access -> spend caps). ctx: provider (name or list of names the call may
    use), workspace_id, user_id, evaluator_id, feature, agent_id, models (refs the call may use, primary first: allow-list / feature access are checked when given).
    Raises BudgetExceeded (a SpendCapReached, HTTP 402) when an enabled action=block cap that matches ctx is reached, ModelNotAllowed (HTTP 403) for a model outside the allow-list;
    action=warn only logs. A provider cap blocks only when every provider in ctx is blocked (the gateway falls back to the others). Returns the list of warnings.
    ctx["_strict"] = True reads the matching caps' spend fresh (no CAPS_TTL lag); ctx["_reserve"] = True also reserves the call's cost and leaves the Ticket in ctx["_ticket"] (see `gate`)."""
    import gw_policy_engine as pe
    return (await pe.evaluate(ctx, fresh=fresh)).warnings

async def gate(chain, **over):
    """GĐ11.14: the policy gate of the three internal model paths (agent middleware, `generate`, `lc_call`): strict (fresh spend), and it RESERVES the call's estimated cost against every
    matching cap until `await ticket.release()` (in a finally, after the usage row is written), so concurrent calls cannot all slip under a cap. -> Ticket."""
    import gw_policy_engine as pe
    await pe.prepare(chain, feature=over.get("feature") or FEATURE.get())
    ctx = ctx_now([provider(m) for m in _usable(chain)], models=list(chain), **over); ctx["_strict"] = ctx["_reserve"] = True
    await budget_check(ctx)   # always through budget_check: the one entry point (tests and callers stub it)
    return ctx.pop("_ticket", None) or pe.NO_TICKET

def _usable(chain):
    if gemini_only(): chain = demo_chain(chain)
    chain = list(chain) + ([LAST_RESORT] if LAST_RESORT not in chain else [])
    import gw_policy_engine as pe
    chain = [m for m in chain if pe.model_ok(m, _cur_ws(), FEATURE.get())]   # GĐ11.14: a model outside the workspace allow-list / the feature's list is never a fallback candidate
    now = time.time(); ok = [m for m in chain if COOLDOWN.get(_cn(m), 0) < now and under_cap(m)]
    return ok or chain[-1:]

def _mark(m, err, status=None):
    m = _cn(m)
    if status == 429 or (status is None and ("429" in err or "RESOURCE_EXHAUSTED" in err)): COOLDOWN[m] = time.time() + COOLDOWN_S
    elif status in (503, 504) or (status is None and any(k in err for k in ("503", "UNAVAILABLE", "DEADLINE", "Timeout"))): COOLDOWN[m] = time.time() + COOLDOWN_S / 2   # GD7.R: an overloaded model (504 after 120 s) -> next calls use the fallback for 30 s
USAGE_SINK = []   # app gắn hàm ghi usage vào đây (callers iterate a tuple() copy: a sink that re-imports the app could append to it mid-loop -> endless loop, GD7.R 7 GB leak)
def _spend(m, c):
    if c: SPENT[pname(m)] += c

# GĐ9.P2: pseudonym mode must not flash `<PHONE_1>` in streamed text. The tokens of a model call are restored chunk by chunk (tokens split across chunks are
# buffered) BEFORE LangChain notifies the stream callbacks, by a per-class subclass of whatever chat model the gateway uses; the pseudonymizer of the
# current call travels in a ContextVar set around the handler (copied into executor threads, so sync `_stream` works too).
import contextvars as _cv
_PZ_CV = _cv.ContextVar("aos_pii_pz", default=None)
_RESTORING = {}
def restoring_model(model):
    cls = type(model)
    if cls in _RESTORING.values() or not (hasattr(model, "model_copy") and hasattr(cls, "_astream")): return model   # stubs / non-chat models: only the post-hoc restore applies
    sub = _RESTORING.get(cls)
    if sub is None:
        class Restoring(cls):
            async def _astream(self, *a, **k):
                pz = _PZ_CV.get(); src = super()._astream(*a, **k)
                if not pz:
                    async for x in src: yield x
                    return
                import pii
                async for x in pii.arestore_chunks(src, pz): yield x
            def _stream(self, *a, **k):
                pz = _PZ_CV.get(); src = super()._stream(*a, **k)
                if not pz: yield from src; return
                import pii
                yield from pii.restore_chunks(src, pz)
        Restoring.__name__ = Restoring.__qualname__ = "Restoring" + cls.__name__; sub = _RESTORING[cls] = Restoring
    m = model.model_copy(); object.__setattr__(m, "__class__", sub)   # same fields, only the stream methods differ
    return m

def _model(name, temperature, params=None):
    """Chat model for any provider ref (GĐ11.3, langchain `init_chat_model`). Sync: non-built-in providers need `await prepare(name)` first (credentials come from CRED);
    Gemini / OpenAI without a workspace row use the .env key exactly as before. params: see gw_providers.build_model."""
    import gw_providers
    ws = _cur_ws(); res = _router.resolve_sync(name, ws)
    cred = gw_providers.CRED.get((ws, res.ref)) if ws else None
    if cred is None and (gw_providers.row_of(res, ws) is not None or res.kind not in ("gemini", "openai")): raise ProviderKeyMissing(f"Thiếu khoá {_pname(res.provider)}")   # prepare() was not awaited / no key: fail closed
    return gw_providers.build_model(res, {"temperature": temperature, **(params or {})}, cred)

async def _pii_cfg():
    """GĐ9.8: the calling workspace's PII masking config when it applies to model calls (None = off, demo workspaces never). `_cur_ws() is None` (in-process tests) = off, no DB."""
    ws = _cur_ws()
    if not ws or ws in DEMO_WS: return None
    import pii
    c = await pii.workspace_cfg(ws)
    return c if c and c["gateway"] else None

def _fb():
    import gw_fallbacks
    return gw_fallbacks

def cache_ttl(m):
    """GĐ11.10: Anthropic prompt-cache TTL of a model ref: "5m" (default) or "1h" only when its saved configuration (`custom/<name>`) sets extra_params.cache_ttl ("1h"; "30m" is rounded up to "1h")."""
    try:
        x = (_router.resolve_sync(m, _cur_ws()).params or {}).get("extra_params")
        if isinstance(x, str): x = json.loads(x)
        v = str((x or {}).get("cache_ttl") or "5m").strip().lower()
    except Exception: return "5m"   # noqa: BLE001
    return "5m" if v in ("5m", "5", "") else "1h" if v in ("30m", "1h", "60m") else "5m"

class GatewayMiddleware(AgentMiddleware):
    """Bọc mọi lời gọi model của agent (cả sub-agent): RPM, ngân sách, retry + fallback, usage, span."""
    def __init__(self, agent_id, primary, fallbacks, temperature, rpm=None, budget_check=None):
        super().__init__()
        self.agent_id, self.chain = agent_id, [primary] + [m for m in fallbacks if m != primary]
        self.temperature, self.rpm, self.budget_check = temperature, rpm, budget_check
        self.models = {}   # (ref, timeout) -> model, built on first use (a demo run never even constructs the OpenAI client)
        from langchain_anthropic.middleware import AnthropicPromptCachingMiddleware   # GĐ11.10: NOT in the agent's middleware list (its cache_control would leak into a Gemini fallback): applied per attempt, Anthropic attempts only
        self._cache_mw = {t: AnthropicPromptCachingMiddleware(ttl=t, unsupported_model_behavior="ignore") for t in ("5m", "1h")}

    async def awrap_model_call(self, request, handler):
        if self.budget_check: await self.budget_check(self.agent_id)
        rc = getattr(getattr(request, "runtime", None), "context", None)   # chat mode of this run = LangChain runtime context (chat_modes.RunContext): swap the primary model per call, no agent rebuild; sub-agents carry this middleware too
        rm = {"model": rc.model, "thinking": rc.thinking, "timeout_s": rc.timeout_s} if getattr(rc, "model", None) else None
        chain, fcfg = await _fb().resolve_chain(([rm["model"]] + [m for m in self.chain if m != rm["model"]]) if rm else self.chain); to = (rm or {}).get("timeout_s") or fcfg.get("header_timeout_s")   # 11.13: workspace chain row > the agent's list > DEFAULT_FALLBACKS; the mode's timeout wins
        tk = await gate(chain, agent_id=self.agent_id)   # 11.14 policy gate: allow-list + feature access + unified caps (workspace/user/evaluator/feature/agent/provider), reserves the call's cost; sub-agents carry the same agent id
        try: return await self._attempts(request, handler, chain, fcfg, to, (rm or {}).get("thinking"))
        finally: await tk.release()   # after the usage row is written (the sink is awaited inside)

    async def _attempts(self, request, handler, chain, fcfg, to, thinking=None):
        pcfg = await _pii_cfg(); pz = None
        if pcfg:   # GĐ9.8: the model only ever sees masked text; `pseudonym` mode swaps the real values back into the answer + tool-call arguments below
            import pii
            request, pz = await pii.mask_request(request, pcfg)
        last = None
        for m in _usable(chain):
            for attempt in range(2):
                if not under_cap(m): break
                if provider(m) != "gemini" and gemini_only(): raise DemoOpenAIBlocked("demo: OpenAI blocked (Gemini only)")
                await RATE.acquire(("agent", self.agent_id), self.rpm)
                sp = span_open("llm", m, attempt=attempt + 1)
                t = time.perf_counter()
                try:
                    th = __import__("chat_modes").think_for(m, thinking) if thinking else None; mk = (m, to, th) if th else (m, to)   # chat mode: reasoning_effort (Gemini thinking_level / OpenAI / Claude effort), nearest level the model takes
                    if mk not in self.models:
                        await prepare(m); pr = {**({"timeout": to} if to else {}), **({"reasoning_effort": th} if th else {})}
                        self.models[mk] = _model(m, self.temperature, pr) if pr else _model(m, self.temperature)   # LAST_RESORT thêm động trong _usable
                    mdl = restoring_model(self.models[mk]) if pz else self.models[mk]   # 9.P2: tokens restored per streamed chunk
                    tk_pz = _PZ_CV.set(pz) if pz else None
                    try:
                        req = request.override(model=mdl)
                        resp = await (self._cache_mw[cache_ttl(m)].awrap_model_call(req, handler) if provider(m) == "anthropic" else handler(req))   # 11.10: 5 m prompt cache by default; Gemini / GPT fallbacks get no cache_control
                    finally:
                        if tk_pz is not None: _PZ_CV.reset(tk_pz)
                    if pz: pii.restore_response(resp, pz)
                    msg = next((x for x in resp.result if type(x).__name__ == "AIMessage"), resp.result[0]) if hasattr(resp, "result") else resp
                    u = getattr(msg, "usage_metadata", None) or {}
                    i, o = u.get("input_tokens", 0), u.get("output_tokens", 0)
                    dt_ = details_from_usage(u); span_close(sp, status="ok", input_tokens=i, output_tokens=o, cost_usd=cost(m, i, o, dt_)); _spend(m, cost(m, i, o, dt_))
                    try:
                        msg.response_metadata["gateway_model"] = m
                    except Exception:  # noqa: BLE001
                        pass
                    for f in tuple(USAGE_SINK): await f(self.agent_id, m, i, o, cost(m, i, o, dt_), "ok", int((time.perf_counter() - t) * 1000), None, dt_)
                    return resp
                except ProviderKeyMissing as e:   # 11.3: the chosen (first) model has no key -> the user sees "Thiếu khoá ..."; a missing fallback is skipped
                    last = e; span_close(sp, status="error", error=str(e))
                    if m == chain[0]: raise
                    log.warning("fallback candidate %s skipped: %s", m, e)
                    break
                except Exception as e:  # noqa: BLE001
                    last = e; s = str(e)
                    span_close(sp, status="error", error=s[:200])
                    for f in tuple(USAGE_SINK): await f(self.agent_id, m, 0, 0, 0, "error", int((time.perf_counter() - t) * 1000), s[:300])
                    if not _fb().is_retryable(e, fcfg): raise   # 11.13: status codes of the chain row; 400 / 401 never
                    _mark(m, s or type(e).__name__, _fb().mark_status(e))
                    if COOLDOWN.get(_cn(m), 0) > time.time(): break
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise last or SpendCapReached(cap_message(chain) or "Không còn model nào trong hạn mức")

    async def awrap_tool_call(self, request, handler):
        tc = request.tool_call; name = tc["name"]
        kind = "subagent" if name == "task" else "todos" if name == "write_todos" else "tool"
        label = f"sub-agent: {tc['args'].get('subagent_type')}" if kind == "subagent" else name
        sp = span_open(kind, label, args=tc.get("args"))
        tok = PARENT.set(sp["id"])
        holder = {}; stok = None
        if name == "execute":
            try: import sbx_runtime as _R; stok = (_R.SPAN_ATTRS, _R.SPAN_ATTRS.set(holder))     # 11.41-F3: sandbox.* attributes of the model's own command
            except Exception: pass  # noqa: BLE001
        try:
            r = await handler(request)
            if holder: sp.update(holder)
            span_close(sp, status="ok", result=str(getattr(r, "content", r))[:2000])
            return r
        except Exception as e:  # noqa: BLE001
            span_close(sp, status="error", error=str(e)[:200]); raise
        finally:
            PARENT.reset(tok)
            if stok: stok[0].reset(stok[1])

async def generate(client, prompt, chain, json_mode=False, temperature=None, agent_id=None, evaluator_id=None, feature=None):
    """Gọi trực tiếp (judge/playground) qua gateway: fallback + usage. Trả (text, model, in, out).
    evaluator_id / feature: attribution for the unified spend caps (budget_check) and the usage rows."""
    tk = [(EVALUATOR, EVALUATOR.set(evaluator_id)) if evaluator_id is not None else None, (FEATURE, FEATURE.set(feature)) if feature else None]
    pcfg = await _pii_cfg() if isinstance(prompt, str) else None; pz = None
    if pcfg:   # GĐ9.8: judge / playground / assistant prompts are masked too; pseudonyms are restored in the returned text
        import pii
        pz = pii.Pseudonymizer() if pcfg["mode"] == "pseudonym" else None
        prompt = await pii.amask_obj(prompt, pcfg, pz)
    try:
        text, m, i, o = await _generate(client, prompt, chain, json_mode, temperature, agent_id)
        return (pz.restore(text) if pz and isinstance(text, str) else text), m, i, o
    finally:
        for t in tk:
            if t: t[0].reset(t[1])

async def _generate(client, prompt, chain, json_mode, temperature, agent_id):
    chain, fcfg = await _fb().resolve_chain(chain)
    tk = await gate(chain, agent_id=agent_id)   # GĐ11.14 policy gate (allow-list, feature access, caps) + cost reservation
    try: return await _generate_loop(client, prompt, chain, json_mode, temperature, agent_id, fcfg)
    finally: await tk.release()

async def _generate_loop(client, prompt, chain, json_mode, temperature, agent_id, fcfg):
    from google.genai import types as gt
    to = fcfg.get("header_timeout_s")
    last = None
    for m in _usable(chain):
        for attempt in range(2):
            t = time.perf_counter()
            try:
                if not under_cap(m): break
                if provider(m) != "gemini":
                    if gemini_only(): raise DemoOpenAIBlocked("demo: OpenAI blocked (Gemini only)")
                    await prepare(m); llm = (_model(m, temperature, {"timeout": to}) if to else _model(m, temperature))
                    if json_mode and provider(m) in ("openai", "openai_compat"): llm = llm.bind(response_format={"type": "json_object"})
                    r = await llm.ainvoke(prompt); u = r.usage_metadata or {}; i, o = u.get("input_tokens", 0), u.get("output_tokens", 0)
                    dt_ = details_from_usage(u); _spend(m, cost(m, i, o, dt_))
                    for f in tuple(USAGE_SINK): await f(agent_id, m, i, o, cost(m, i, o, dt_), "ok", int((time.perf_counter() - t) * 1000), None, dt_)
                    return (r.content if isinstance(r.content, str) else "".join(x.get("text", "") for x in r.content if isinstance(x, dict))), m, i, o
                cfg = gt.GenerateContentConfig(response_mime_type="application/json" if json_mode else None, temperature=temperature)
                r = await (asyncio.wait_for(client.aio.models.generate_content(model=m, contents=prompt, config=cfg), to) if to else client.aio.models.generate_content(model=m, contents=prompt, config=cfg))
                um = r.usage_metadata; i = um.prompt_token_count or 0; o = (um.candidates_token_count or 0) + (um.thoughts_token_count or 0)
                dt_ = details_from_genai(um)
                for f in tuple(USAGE_SINK): await f(agent_id, m, i, o, cost(m, i, o, dt_), "ok", int((time.perf_counter() - t) * 1000), None, dt_)
                return r.text, m, i, o
            except ProviderKeyMissing as e:
                if m == chain[0]: raise
                log.warning("fallback candidate %s skipped: %s", m, e)
                last = e; break
            except Exception as e:  # noqa: BLE001
                last = e
                es = str(e) or type(e).__name__   # a 120 s timeout has an empty message: match/cool down on the class name (as awrap_model_call)
                for f in tuple(USAGE_SINK): await f(agent_id, m, 0, 0, 0, "error", int((time.perf_counter() - t) * 1000), es[:300])
                if not _fb().is_retryable(e, fcfg): raise
                _mark(m, es, _fb().mark_status(e))
                if COOLDOWN.get(_cn(m), 0) > time.time(): break
                await asyncio.sleep(1.5 * (attempt + 1))
    raise last or SpendCapReached(cap_message(chain) or "Không còn model nào trong hạn mức")


# ---------------------------------------------------------------- GĐ9.2/9.3: LangChain v1 calls (bind_tools / with_structured_output / astream) through the gateway
def tune(llm, params=None, json_mode=False):
    """Per-call model settings (a saved model preset: max tokens, top_p, reasoning effort) applied to a LangChain chat model. Fields a provider does not have are skipped."""
    f = getattr(type(llm), "model_fields", {}); p = params or {}; up = {}
    if p.get("max_tokens"):
        k = next((x for x in ("max_output_tokens", "max_completion_tokens", "max_tokens") if x in f), None)
        if k: up[k] = int(p["max_tokens"])
    if p.get("top_p") is not None and "top_p" in f: up["top_p"] = float(p["top_p"])
    if p.get("reasoning_effort") and "reasoning_effort" in f: up["reasoning_effort"] = p["reasoning_effort"]
    if json_mode and "response_mime_type" in f: up["response_mime_type"] = "application/json"
    return llm.model_copy(update=up) if up else llm

async def lc_call(chain, messages, *, temperature=None, params=None, tools=None, tool_choice=None, schema=None, json_mode=False,
                  feature=None, agent_id=None, on_token=None):
    """One model turn with LangChain v1 primitives through the gateway: budget_check(feature) -> fallback chain -> `_model()` -> tune ->
    `with_structured_output(schema, include_raw=True)` | `bind_tools(tools)` | `astream` (on_token) -> usage sink + cost.
    Returns {"message": AIMessage, "parsed", "parsing_error", "model", "input_tokens", "output_tokens", "cost_usd"}."""
    tk_f = FEATURE.set(feature) if feature else None
    try:
        chain, fcfg = await _fb().resolve_chain(chain)
        tk = await gate(chain, agent_id=agent_id)   # GĐ11.14 policy gate (allow-list, feature access, caps) + cost reservation
        try: return await _lc_loop(chain, fcfg, messages, temperature, params, tools, tool_choice, schema, json_mode, agent_id, on_token)
        finally: await tk.release()
    finally:
        if tk_f is not None: FEATURE.reset(tk_f)

async def _lc_loop(chain, fcfg, messages, temperature, params, tools, tool_choice, schema, json_mode, agent_id, on_token):
    if True:
        to = fcfg.get("header_timeout_s")
        usable = _usable(chain)
        last = None
        for m in usable:
            for attempt in range(2):
                t = time.perf_counter(); sent = False
                try:
                    if not under_cap(m): break
                    if provider(m) != "gemini" and gemini_only(): raise DemoOpenAIBlocked("demo: OpenAI blocked (Gemini only)")
                    await prepare(m); llm = tune((_model(m, temperature, {"timeout": to}) if to else _model(m, temperature)), params, json_mode); parsed = perr = None
                    if schema is not None:
                        out = await llm.with_structured_output(schema, include_raw=True).ainvoke(messages)
                        msg, parsed, perr = out["raw"], out.get("parsed"), out.get("parsing_error")
                    else:
                        if tools: llm = llm.bind_tools(tools, **({"tool_choice": tool_choice} if tool_choice else {}))
                        if on_token:
                            msg = None
                            async for ch in llm.astream(messages):
                                msg = ch if msg is None else msg + ch
                                tx = str(getattr(ch, "text", "") or "")
                                if tx: sent = True; await on_token(tx)
                        else: msg = await llm.ainvoke(messages)
                    u = getattr(msg, "usage_metadata", None) or {}; i, o = u.get("input_tokens", 0), u.get("output_tokens", 0)
                    dt_ = details_from_usage(u); c = cost(m, i, o, dt_); _spend(m, c)
                    for f in tuple(USAGE_SINK): await f(agent_id, m, i, o, c, "ok", int((time.perf_counter() - t) * 1000), None, dt_)
                    return {"message": msg, "parsed": parsed, "parsing_error": perr, "model": m, "input_tokens": i, "output_tokens": o, "cost_usd": c}
                except ProviderKeyMissing as e:
                    if m == chain[0]: raise
                    log.warning("fallback candidate %s skipped: %s", m, e)
                    last = e; break
                except Exception as e:  # noqa: BLE001
                    last = e; es = str(e) or type(e).__name__
                    for f in tuple(USAGE_SINK): await f(agent_id, m, 0, 0, 0, "error", int((time.perf_counter() - t) * 1000), es[:300])
                    if sent or not _fb().is_retryable(e, fcfg): raise   # 11.13: after the first streamed token the answer is already partly out: never retried
                    _mark(m, es, _fb().mark_status(e))
                    if COOLDOWN.get(_cn(m), 0) > time.time(): break
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise last or SpendCapReached(cap_message(chain) or "Không còn model nào trong hạn mức")
