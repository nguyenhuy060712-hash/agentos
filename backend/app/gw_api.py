"""P6 LLM Gateway (tự viết theo mô tả công khai): endpoint tương thích OpenAI (/v1/chat/completions, /v1/models, /v1/embeddings),
virtual key (hash), danh sách model cho phép, ngân sách USD/tháng, RPM, cache theo nội dung (TTL), guardrail PII (mask/block) cả vào và ra,
fallback giữa các model Gemini, streaming SSE, tool calling, trace vào project 'gateway'. Chỉ Gemini (không proxy nhà cung cấp khác)."""
import asyncio, base64, contextvars, functools, hashlib, json, os, re, secrets, time, uuid
from collections import defaultdict, deque
from fastapi import APIRouter, HTTPException, Request, Header
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb
from google.genai import types as gt
import deepagent_api as A
import gateway as gw
import gw_router
import obs

router = APIRouter()
DDL = """
CREATE TABLE IF NOT EXISTS gw_vkeys(id SERIAL PRIMARY KEY, name TEXT NOT NULL, prefix TEXT NOT NULL, key_hash TEXT UNIQUE NOT NULL, workspace_id INT NOT NULL DEFAULT 1,
  models TEXT[], fallbacks TEXT[], budget_usd_month DOUBLE PRECISION, rpm INT, pii_mode TEXT NOT NULL DEFAULT 'mask' CHECK (pii_mode IN ('off','mask','block')),
  cache_ttl_s INT NOT NULL DEFAULT 0, enabled BOOLEAN NOT NULL DEFAULT true, created TIMESTAMPTZ NOT NULL DEFAULT now(), last_used TIMESTAMPTZ);
CREATE TABLE IF NOT EXISTS gw_cache(h TEXT PRIMARY KEY, vkey_id INT, response JSONB NOT NULL, created TIMESTAMPTZ NOT NULL DEFAULT now(), hits INT NOT NULL DEFAULT 0);
ALTER TABLE gateway_usage ADD COLUMN IF NOT EXISTS vkey_id INT;
ALTER TABLE gateway_usage ADD COLUMN IF NOT EXISTS cached BOOLEAN NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS gw_tool_sigs(call_id TEXT PRIMARY KEY, sig BYTEA NOT NULL, created TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS gateway_usage_vkey ON gateway_usage(vkey_id, ts) WHERE vkey_id IS NOT NULL;
"""
async def init(): await A.q(DDL); await A.q(gw.CAPS_DDL)   # CAPS_DDL: spend_caps + gateway_usage attribution columns (GĐ6.1)
def _h(k): return hashlib.sha256(k.encode()).hexdigest()

# ---------------- PII guardrail (GĐ9.8: Presidio + Vietnamese recognizers in pii.py; same interface and [TAG] format as before, many more kinds:
# PHONE, CCCD, CMND, MST, STK, BIENSO, PASSPORT, NGAYSINH, EMAIL, IP, CARD; checksums / province codes / context words replace the old bare regexes)
import pii

def pii_scan(text):
    return pii.scan_text(text)

def pii_mask(text):
    return pii.mask_text(text or "", {"mode": "mask"}) if text else (text or "")

# ---------------- virtual keys (admin)
class VKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    models: list[str] | None = None
    fallbacks: list[str] | None = None
    budget_usd_month: float | None = Field(default=None, ge=0)
    rpm: int | None = Field(default=None, ge=1, le=10000)
    pii_mode: str = Field(default="mask", pattern=r"^(off|mask|block)$")
    cache_ttl_s: int = Field(default=0, ge=0, le=86400 * 30)

def _check_models(ms):
    for m in ms or []:
        if not re.fullmatch(r"gemini-[\w.\-]+", m): raise HTTPException(400, f"Chỉ hỗ trợ model Gemini: {m}")

@router.post("/api/gw/keys")
async def vk_new(b: VKeyIn):
    import admin
    _check_models(b.models); _check_models(b.fallbacks)
    raw = "vk-" + secrets.token_hex(24)
    r = await A.q("INSERT INTO gw_vkeys(name,prefix,key_hash,workspace_id,models,fallbacks,budget_usd_month,rpm,pii_mode,cache_ttl_s) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                  (b.name, raw[:10], _h(raw), admin.cur_ws(), b.models, b.fallbacks, b.budget_usd_month, b.rpm, b.pii_mode, b.cache_ttl_s), one=True)
    return {"id": r["id"], "key": raw, "base_url": "/v1", "note": "Chỉ hiện một lần. Dùng như OpenAI API key (Authorization: Bearer)."}

@router.get("/api/gw/keys")
async def vk_list():
    return await A.q("""SELECT k.id,k.name,k.prefix,k.models,k.fallbacks,k.budget_usd_month,k.rpm,k.pii_mode,k.cache_ttl_s,k.enabled,k.created,k.last_used,
      (SELECT coalesce(sum(cost_usd),0) FROM gateway_usage g WHERE g.vkey_id=k.id AND g.ts > date_trunc('month', now())) spent_month,
      (SELECT count(*) FROM gateway_usage g WHERE g.vkey_id=k.id AND g.ts > now()-interval '24 hours') calls_24h,
      (SELECT count(*) FROM gateway_usage g WHERE g.vkey_id=k.id AND g.cached AND g.ts > now()-interval '24 hours') cache_hits_24h FROM gw_vkeys k ORDER BY id""")

@router.get("/api/gw/keys/{kid}/usage")
async def vk_usage(kid: int, days: int = 14):
    days = max(1, min(days, 90))
    k = await A.q("SELECT id,name,prefix,budget_usd_month,rpm FROM gw_vkeys WHERE id=%s", (kid,), one=True)   # RLS (GĐ10.28): only this workspace's keys are visible
    if not k: raise HTTPException(404, "Key not found")
    daily = await A.q("""SELECT to_char(d,'YYYY-MM-DD') AS day, coalesce(count(g.id),0) calls, count(g.id) FILTER (WHERE g.status<>'ok') errors,
        count(g.id) FILTER (WHERE g.cached) cached, coalesce(sum(g.input_tokens),0) in_tok, coalesce(sum(g.output_tokens),0) out_tok, coalesce(sum(g.cost_usd),0)::float8 cost
        FROM generate_series(date_trunc('day', now() AT TIME ZONE 'Asia/Bangkok') - make_interval(days => %s - 1), date_trunc('day', now() AT TIME ZONE 'Asia/Bangkok'), interval '1 day') d
        LEFT JOIN gateway_usage g ON g.vkey_id=%s AND date_trunc('day', g.ts AT TIME ZONE 'Asia/Bangkok')=d GROUP BY d ORDER BY d""", (days, kid))
    models = await A.q("""SELECT model, count(*) calls, count(*) FILTER (WHERE status<>'ok') errors, coalesce(sum(cost_usd),0)::float8 cost,
        percentile_cont(.5) WITHIN GROUP (ORDER BY latency_ms) p50 FROM gateway_usage WHERE vkey_id=%s AND ts > now()-make_interval(days => %s) GROUP BY 1 ORDER BY 2 DESC""", (kid, days))
    return {"key": k, "days": days, "daily": daily, "by_model": models}

@router.patch("/api/gw/keys/{kid}")
async def vk_upd(kid: int, b: dict):
    allowed = {"enabled", "budget_usd_month", "rpm", "pii_mode", "cache_ttl_s", "models", "fallbacks", "name"}
    if not await A.q("SELECT 1 FROM gw_vkeys WHERE id=%s", (kid,), one=True): raise HTTPException(404, "Key not found")   # GĐ10.28: not this workspace's key = not found
    for k, v in b.items():
        if k not in allowed: raise HTTPException(400, f"không sửa được {k}")
        if k in ("models", "fallbacks"): _check_models(v)
        if k == "pii_mode" and v not in ("off", "mask", "block"): raise HTTPException(400)
        await A.q(f"UPDATE gw_vkeys SET {k}=%s WHERE id=%s", (v, kid))
    return {"ok": True}

@router.delete("/api/gw/keys/{kid}")
async def vk_del(kid: int):
    if not await A.q("DELETE FROM gw_vkeys WHERE id=%s RETURNING id", (kid,), one=True): raise HTTPException(404, "Key not found")   # GĐ10.28: RLS hides other workspaces' keys
    return {"ok": True}

# ---------------- auth + giới hạn
WIN: dict = defaultdict(deque)
async def _vkey(authorization: str):
    tok = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    if not tok.startswith("vk-"): raise HTTPException(401, {"error": {"message": "Thiếu/ sai virtual key (Authorization: Bearer vk-...)", "type": "invalid_request_error"}})
    k = await A.q("SELECT * FROM gw_vkeys WHERE key_hash=%s AND enabled", (_h(tok),), one=True)
    if not k: raise HTTPException(401, {"error": {"message": "Virtual key không hợp lệ hoặc đã tắt", "type": "invalid_request_error"}})
    if k["rpm"]:
        w = WIN[k["id"]]; now = time.time()
        while w and now - w[0] > 60: w.popleft()
        if len(w) >= k["rpm"]: raise HTTPException(429, {"error": {"message": f"Vượt {k['rpm']} request/phút", "type": "rate_limit_error"}})
        w.append(now)
    if k["budget_usd_month"] is not None:
        sp = (await A.q("SELECT coalesce(sum(cost_usd),0) c FROM gateway_usage WHERE vkey_id=%s AND ts > date_trunc('month', now())", (k["id"],), one=True))["c"]
        if float(sp) >= k["budget_usd_month"]:
            raise HTTPException(402, {"error": {"message": f"Hết ngân sách tháng ${k['budget_usd_month']} (đã tiêu ${float(sp):.4f})", "type": "budget_exceeded"}})
    await A.q("UPDATE gw_vkeys SET last_used=now() WHERE id=%s", (k["id"],))
    return k

TICKET: contextvars.ContextVar = contextvars.ContextVar("gw_v1_ticket", default=None)   # GĐ11.14: the cost reservation of the /v1 call in progress
async def budget(k, reserve=False):
    """Unified spend caps (GĐ6.1) for a /v1 call made with virtual key k: workspace of the key, feature 'gateway', provider gemini -> HTTP 402.
    GĐ11.14: strict (the matching caps' spend is read fresh); reserve=True also reserves the call's estimated cost until release_ticket() (see ticketed)."""
    ctx = gw.ctx_now("gemini", workspace_id=k["workspace_id"], user_id=None, feature="gateway"); ctx["_strict"] = True; ctx["_reserve"] = reserve
    try: await gw.budget_check(ctx)
    except gw.BudgetExceeded as e: raise HTTPException(402, {"error": {"message": str(e), "type": "budget_exceeded"}}) from None
    if reserve and ctx.get("_ticket") is not None: TICKET.set(ctx["_ticket"])

async def release_ticket():
    t = TICKET.get()
    if t is not None: TICKET.set(None); await t.release()

async def run_ticketed(coro):
    """Await a /v1 handler body; the reservation is freed when it returns (a streaming response frees it when the stream ends: with_ticket) or raises."""
    try: r = await coro
    except BaseException: await release_ticket(); raise   # noqa: BLE001
    if not isinstance(r, StreamingResponse): await release_ticket()
    return r

def ticketed(fn):
    @functools.wraps(fn)
    async def w(*a, **kw): return await run_ticketed(fn(*a, **kw))
    return w

async def with_ticket(gen):
    try:
        async for x in gen: yield x
    finally: await release_ticket()

async def model_gate(k, model, chain):
    """GĐ11.14 workspace model allow-list for a /v1 call: the model asked for outside the list -> 403 (OpenAI error body); fallbacks outside it are dropped. -> the chain to use."""
    import gw_policy_engine as pe
    try: ok = await pe.check_models(k["workspace_id"], [model, *[m for m in chain if m != model]], "gateway")
    except pe.ModelNotAllowed as e: raise HTTPException(403, {"error": {"message": str(e.detail), "type": "permission_error"}}) from None
    return [m for m in chain if m in ok]

# ---------------- Spend caps admin API (GĐ6.1): GET (+ legacy provider summary) in deepagent_api /api/gateway/caps; write routes here. Admin-only (admin.ADMIN_ONLY).
class CapIn(BaseModel):
    scope: str = Field(pattern=r"^(provider|workspace|user|evaluator|feature|agent)$")
    target: str = Field(min_length=1, max_length=80)
    period: str = Field(pattern=r"^(day|week|month|total)$")
    limit_usd: float = Field(ge=0)
    action: str = Field(default="block", pattern=r"^(block|warn)$")
    enabled: bool = True
    workspace_id: int | None = None      # superadmin only (None = global); a workspace admin's caps are always pinned to its own workspace

class CapPatch(BaseModel):
    target: str | None = Field(default=None, min_length=1, max_length=80)
    period: str | None = Field(default=None, pattern=r"^(day|week|month|total)$")
    limit_usd: float | None = Field(default=None, ge=0)
    action: str | None = Field(default=None, pattern=r"^(block|warn)$")
    enabled: bool | None = None

async def _is_super():
    import admin
    u = admin.cur() or {}
    r = await A.q("SELECT is_superadmin FROM users WHERE id=%s", (u.get("id"),), one=True) if u.get("id") else None
    return bool(r and r["is_superadmin"])

async def _capdb():
    """(query fn, is_super): superadmin sees/edits every cap (global ones too) as the system; a workspace admin only its own workspace (RLS)."""
    sup = await _is_super()
    return (gw._sys if sup else A.q), sup

def _ict(r):
    """GĐ11.14: every cap row says how much is used now and when its period began; periods are counted in ICT (Vietnam time)."""
    ps = gw.period_start(r["period"]); r["current_spend_usd"] = r["spent"]; r["period_start"] = ps.isoformat() if ps else None; r["tz"] = gw.CAPS_TZ
    return r

def _norm_cap(r):
    r = dict(r); r["limit_usd"] = float(r["limit_usd"]); r["created_at"] = r["created_at"].isoformat() if r.get("created_at") else None
    return r

async def caps_list():
    db, sup = await _capdb()
    out = []
    for r in await db("SELECT * FROM spend_caps ORDER BY id"):
        if not sup and r["workspace_id"] is None: continue   # GĐ10.28: a workspace session can now READ the whole-machine caps; their spend figure is machine-wide, so the list keeps showing the workspace's own only
        r = _norm_cap(r); r["spent"] = round(await gw.cap_spent(r), 6); r["source"] = "db"; r["reached"] = gw.reached(r["spent"], r["limit_usd"]); _ict(r); out.append(r)
    env = os.getenv("GEMINI_BUDGET_USD_MONTH")
    if env and sup:
        r = {"id": None, "workspace_id": None, "scope": "provider", "target": "gemini", "period": "month", "limit_usd": float(env), "action": "block", "enabled": True, "created_at": None, "source": "env"}
        r["spent"] = round(await gw.cap_spent(r), 6); r["reached"] = gw.reached(r["spent"], r["limit_usd"]); _ict(r); out.append(r)
    return out

def _check_target(scope, target):
    if scope in ("workspace", "user", "evaluator", "agent") and not target.isdigit(): raise HTTPException(422, f"target của scope {scope} phải là số (id)")

@router.post("/api/gateway/caps")
async def caps_create(b: CapIn):
    db, sup = await _capdb(); _check_target(b.scope, b.target)
    import admin
    ws = b.workspace_id if sup else admin.cur_ws()
    r = await db("INSERT INTO spend_caps(workspace_id,scope,target,period,limit_usd,action,enabled) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                 (ws, b.scope, b.target, b.period, b.limit_usd, b.action, b.enabled), one=True)
    await gw.caps_snapshot(fresh=True)
    r = _norm_cap(r); r["spent"] = round(await gw.cap_spent(r), 6); r["source"] = "db"; r["reached"] = gw.reached(r["spent"], r["limit_usd"]); _ict(r)
    return r

@router.patch("/api/gateway/caps/{cid}")
async def caps_update(cid: int, b: CapPatch):
    db, sup = await _capdb()
    cur = await db("SELECT * FROM spend_caps WHERE id=%s", (cid,), one=True)
    if not cur or (not sup and cur["workspace_id"] is None): raise HTTPException(404, "Không có trần này")   # GĐ10.28: a whole-machine cap is read-only for a workspace admin
    d = {k: v for k, v in b.model_dump().items() if v is not None}
    if "target" in d: _check_target(cur["scope"], d["target"])
    if d:
        await db("UPDATE spend_caps SET " + ",".join(f"{k}=%s" for k in d) + " WHERE id=%s", (*d.values(), cid))
    await gw.caps_snapshot(fresh=True)
    r = _norm_cap(await db("SELECT * FROM spend_caps WHERE id=%s", (cid,), one=True)); r["spent"] = round(await gw.cap_spent(r), 6); r["source"] = "db"; r["reached"] = gw.reached(r["spent"], r["limit_usd"]); _ict(r)
    return r

@router.delete("/api/gateway/caps/{cid}")
async def caps_delete(cid: int):
    db, sup = await _capdb()
    if not await db("DELETE FROM spend_caps WHERE id=%s" + ("" if sup else " AND workspace_id IS NOT NULL") + " RETURNING id", (cid,), one=True): raise HTTPException(404, "Không có trần này")
    await gw.caps_snapshot(fresh=True)
    return {"deleted": cid}

# ---- GĐ9.P1: per-agent spend caps (scope 'agent', target = agent id; day / week / month; block or warn) for the agent page card.
# Read = anyone who can use the agent; write = anyone who can edit it (agent_sharing). Enforced for every model call of the agent (gateway.budget_check).
class AgentCapIn(BaseModel):
    period: str = Field(pattern=r"^(day|week|month)$")
    limit_usd: float | None = Field(default=None, ge=0)      # None = remove the cap of this period
    action: str = Field(default="block", pattern=r"^(block|warn)$")
    enabled: bool = True

async def _agent_caps(aid):
    out = []
    for r in await A.q("SELECT * FROM spend_caps WHERE scope='agent' AND target=%s ORDER BY id", (str(aid),)):
        r = _norm_cap(r); r["spent"] = round(await gw.cap_spent(r), 6); r["reached"] = gw.reached(r["spent"], r["limit_usd"]); _ict(r); out.append(r)
    return out

@router.get("/api/agents/{aid}/caps")
async def agent_caps_get(aid: int):
    import agent_sharing
    x = await agent_sharing.require_use(aid)
    cfg = await A.q("SELECT v.config->>'budget_usd_day' b FROM agents a JOIN agent_versions v ON v.agent_id=a.id AND v.version=a.current_version WHERE a.id=%s", (aid,), one=True)
    return {"agent_id": aid, "caps": await _agent_caps(aid), "can_edit": x["edit"], "legacy_day_budget_usd": float(cfg["b"]) if cfg and cfg["b"] else None}   # legacy = deprecated config field, still enforced

@router.put("/api/agents/{aid}/caps")
async def agent_caps_put(aid: int, b: AgentCapIn):
    import admin, agent_sharing
    await agent_sharing.require_edit(aid)
    ag = await A.q("SELECT workspace_id FROM agents WHERE id=%s", (aid,), one=True)
    ws = (ag or {}).get("workspace_id") or admin.cur_ws()
    await A.q("DELETE FROM spend_caps WHERE scope='agent' AND target=%s AND period=%s", (str(aid), b.period))
    if b.limit_usd is not None:
        await A.q("INSERT INTO spend_caps(workspace_id,scope,target,period,limit_usd,action,enabled) VALUES(%s,'agent',%s,%s,%s,%s,%s)", (ws, str(aid), b.period, b.limit_usd, b.action, b.enabled))
    await gw.caps_snapshot(fresh=True)
    return {"agent_id": aid, "caps": await _agent_caps(aid)}

class CapCheck(BaseModel):
    provider: str | list[str] | None = None
    agent_id: int | None = None
    workspace_id: int | None = None
    user_id: int | None = None
    evaluator_id: int | None = None
    feature: str | None = None

@router.post("/api/gateway/caps/check")
async def caps_check(b: CapCheck):
    """Dry run of budget_check(ctx) with fresh sums: 200 {ok, warnings} or 402 with the reason (same as a blocked run)."""
    try: return {"ok": True, "warnings": await gw.budget_check(b.model_dump(), fresh=True)}
    except gw.BudgetExceeded as e: raise HTTPException(402, str(e)) from None

# ---------------- chuyển đổi OpenAI ↔ Gemini
def _parts(content):
    if content is None: return []
    if isinstance(content, str): return [gt.Part(text=content)]
    out = []
    for c in content:
        if c.get("type") == "text": out.append(gt.Part(text=c.get("text", "")))
        elif c.get("type") == "image_url":
            u = (c.get("image_url") or {}).get("url", "")
            m = re.match(r"data:([\w/+.\-]+);base64,(.+)", u, re.S)
            if not m: raise HTTPException(400, "image_url chỉ nhận data: URL base64 (không tải URL ngoài)")
            out.append(gt.Part(inline_data=gt.Blob(mime_type=m.group(1), data=base64.b64decode(m.group(2)))))
    return out

def _text_of(content):
    if isinstance(content, str): return content
    return " ".join(c.get("text", "") for c in content or [] if c.get("type") == "text")

def to_gemini(body, sigs=None):
    sigs = sigs or {}
    sys_txt, contents, names = [], [], {}
    for m in body.get("messages") or []:
        role = m.get("role")
        if role in ("system", "developer"): sys_txt.append(_text_of(m.get("content"))); continue
        if role == "tool":
            nm = names.get(m.get("tool_call_id"), m.get("name") or "tool")
            try: resp = json.loads(m.get("content") or "{}")
            except Exception: resp = {"result": m.get("content")}  # noqa: BLE001
            if not isinstance(resp, dict): resp = {"result": resp}
            contents.append(gt.Content(role="user", parts=[gt.Part(function_response=gt.FunctionResponse(name=nm, response=resp))])); continue
        parts = _parts(m.get("content"))
        for tc in m.get("tool_calls") or []:
            f = tc.get("function") or {}; names[tc.get("id")] = f.get("name")
            parts.append(gt.Part(function_call=gt.FunctionCall(name=f.get("name"), args=json.loads(f.get("arguments") or "{}")),
                                 thought_signature=sigs.get(tc.get("id"))))   # Gemini 3 bắt buộc gửi lại thought_signature của lời gọi tool
        contents.append(gt.Content(role="model" if role == "assistant" else "user", parts=parts or [gt.Part(text="")]))
    cfg = {"temperature": body.get("temperature"), "top_p": body.get("top_p"),
           "max_output_tokens": body.get("max_completion_tokens") or body.get("max_tokens"),
           "stop_sequences": ([body["stop"]] if isinstance(body.get("stop"), str) else body.get("stop")), "seed": body.get("seed")}
    if sys_txt: cfg["system_instruction"] = "\n".join(sys_txt)
    rf = body.get("response_format") or {}
    if rf.get("type") in ("json_object", "json_schema"):
        cfg["response_mime_type"] = "application/json"
        if rf.get("type") == "json_schema" and (rf.get("json_schema") or {}).get("schema"): cfg["response_json_schema"] = rf["json_schema"]["schema"]
    if body.get("tools"):
        decl = [gt.FunctionDeclaration(name=t["function"]["name"], description=t["function"].get("description"),
                                       parameters_json_schema=t["function"].get("parameters") or {"type": "object", "properties": {}})
                for t in body["tools"] if t.get("type") == "function"]
        cfg["tools"] = [gt.Tool(function_declarations=decl)]
        tcs = body.get("tool_choice")
        if tcs == "none": cfg["tool_config"] = gt.ToolConfig(function_calling_config=gt.FunctionCallingConfig(mode="NONE"))
        elif tcs == "required": cfg["tool_config"] = gt.ToolConfig(function_calling_config=gt.FunctionCallingConfig(mode="ANY"))
        elif isinstance(tcs, dict): cfg["tool_config"] = gt.ToolConfig(function_calling_config=gt.FunctionCallingConfig(mode="ANY", allowed_function_names=[tcs["function"]["name"]]))
    return contents, gt.GenerateContentConfig(**{k: v for k, v in cfg.items() if v is not None})

def _msg_from(r, sig_out=None):
    txt, calls = [], []
    cand = (r.candidates or [None])[0]
    for p in (cand.content.parts if cand and cand.content and cand.content.parts else []):
        if getattr(p, "thought", False): continue
        if p.text: txt.append(p.text)
        if p.function_call:
            cid_ = "call_" + uuid.uuid4().hex[:20]
            if getattr(p, "thought_signature", None) and sig_out is not None: sig_out[cid_] = p.thought_signature
            calls.append({"id": cid_, "type": "function",
                          "function": {"name": p.function_call.name, "arguments": json.dumps(dict(p.function_call.args or {}), ensure_ascii=False)}})
    fr = str(getattr(cand, "finish_reason", "") or "")
    finish = "tool_calls" if calls else ("length" if "MAX_TOKENS" in fr else "content_filter" if "SAFETY" in fr else "stop")
    msg = {"role": "assistant", "content": "".join(txt) or (None if calls else "")}
    if calls: msg["tool_calls"] = calls
    return msg, finish

async def _save_sigs(so):
    for k_, v in so.items():
        await A.q("INSERT INTO gw_tool_sigs(call_id,sig) VALUES(%s,%s) ON CONFLICT DO NOTHING", (k_, v))

def _usage(r):
    um = r.usage_metadata; i = (um.prompt_token_count or 0) if um else 0; o = ((um.candidates_token_count or 0) + (um.thoughts_token_count or 0)) if um else 0
    return i, o

def _details(r):
    """Cached / reasoning / audio / image token counts of a genai response (GĐ6.2) for cost() and the usage row."""
    return gw.details_from_genai(r.usage_metadata)

def _usage_extra(i, o, det):
    """OpenAI-style usage object; the *_details blocks only when there is something to report."""
    u = {"prompt_tokens": i, "completion_tokens": o, "total_tokens": i + o}
    if det.get("cached") or det.get("audio_in"): u["prompt_tokens_details"] = {k: v for k, v in (("cached_tokens", det.get("cached")), ("audio_tokens", det.get("audio_in"))) if v}
    if det.get("reasoning") or det.get("audio_out"): u["completion_tokens_details"] = {k: v for k, v in (("reasoning_tokens", det.get("reasoning")), ("audio_tokens", det.get("audio_out"))) if v}
    return u

def _chain(k, model):
    allowed = k["models"]
    if allowed and model not in allowed: raise HTTPException(403, {"error": {"message": f"Key không được dùng model {model}. Cho phép: {allowed}", "type": "permission_error"}})
    ch = [model] + [m for m in (k["fallbacks"] or gw.DEFAULT_FALLBACKS) if m != model]
    return gw._usable(ch)

async def _record(k, model, i, o, ms, status, err, body_in, out_msg, cached=False, stream=False, details=None):
    """Usage row + `gateway` trace of one call. GĐ10.1: written inside the workspace of the virtual key (public /v1 paths carry no user -> would be workspace 1)."""
    import admin
    async with admin.as_workspace(k["workspace_id"]):
        await _record_ws(k, model, i, o, ms, status, err, body_in, out_msg, cached, stream, details)

async def _record_ws(k, model, i, o, ms, status, err, body_in, out_msg, cached, stream, details):
    d = details or {}
    c = 0 if cached else (gw.cost(model, i, o, d) or 0)
    await A.q("INSERT INTO gateway_usage(agent_id,model,input_tokens,output_tokens,cost_usd,status,latency_ms,error,vkey_id,cached,workspace_id,feature,cached_tokens,reasoning_tokens,audio_tokens,image_tokens,gw_headers) VALUES(NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'gateway',%s,%s,%s,%s,%s)",
              (model, i, o, c, status, ms, err, k["id"], cached, k["workspace_id"], d.get("cached") or None, d.get("reasoning") or None,
               (d.get("audio_in", 0) + d.get("audio_out", 0)) or None, (d.get("image_in", 0) + d.get("image_out", 0)) or None, __import__("gw_policies").sink_attr()[1]))
    try:
        pid = await obs.project_id("gateway"); now = time.time()
        await obs.upsert_run(pid, {"id": str(uuid.uuid4()), "name": "chat.completions", "run_type": "llm", "start_time": now - ms / 1000, "end_time": now,
                                   "inputs": {"messages": body_in.get("messages")}, "outputs": {"message": out_msg} if out_msg else None, "error": err, "model": model,
                                   "usage": {"input_tokens": i, "output_tokens": o}, "cost_usd": c,
                                   "tags": [f"vkey:{k['prefix']}"] + (["cache_hit"] if cached else []) + (["stream"] if stream else []),
                                   "metadata": {"vkey": k["name"], "cached": cached}})
    except Exception as e:  # noqa: BLE001
        print("gw trace error", e)

def _guard_in(k, body):
    if k["pii_mode"] == "off": return body
    hits = []
    for m in body.get("messages") or []:
        c = m.get("content")
        if isinstance(c, str): hits += pii_scan(c)
        elif isinstance(c, list): hits += [h for x in c if x.get("type") == "text" for h in pii_scan(x.get("text", ""))]
    if not hits: return body
    if k["pii_mode"] == "block":
        raise HTTPException(400, {"error": {"message": "Guardrail PII chặn request: phát hiện " + ", ".join(sorted({h[0] for h in hits})), "type": "guardrail_violation"}})
    b2 = json.loads(json.dumps(body))
    for m in b2.get("messages") or []:
        if isinstance(m.get("content"), str): m["content"] = pii_mask(m["content"])
        elif isinstance(m.get("content"), list):
            for x in m["content"]:
                if x.get("type") == "text": x["text"] = pii_mask(x.get("text", ""))
    return b2

@router.get("/v1/models")
async def models(authorization: str = Header(default="")):
    k = await _vkey(authorization)
    ms = [m for m in (k["models"] or list(gw.PRICING.keys())) if re.fullmatch(r"gemini-[\w.\-]+", m) and m in gw._usable([m])]   # GĐ10.4: only models /v1/chat/completions can really call
    import gw_policy_engine as pe
    ms = await pe.filter_models(k["workspace_id"], ms)   # GĐ11.14: only models the workspace allow-list lets through
    return {"object": "list", "data": [{"id": m, "object": "model", "created": 0, "owned_by": "google"} for m in ms]}

@router.post("/v1/chat/completions")
@ticketed
async def chat_completions(request: Request, authorization: str = Header(default="")):
    k = await _vkey(authorization); await budget(k, reserve=True); await __import__("gw_policies").enforce(k, request.headers)   # GĐ9.Q: per-customer X-Gateway-* policies
    try: body = await request.json()
    except Exception: raise HTTPException(400, "JSON sai")  # noqa: B904,BLE001
    model = body.get("model") or A.DEFAULT_MODEL
    route_info = None
    if model in ("auto", "aos/auto"): model, route_info = await __import__("gw_policies").route(k, body)   # GĐ9.Q: routing policy picks the model (rules or a decision model)
    if "/" in model: model = gw_router.v1_gemini(model, k["workspace_id"])   # GĐ11.2: gemini/<m>, aos/fast accepted; another provider -> 400 "Chỉ hỗ trợ Gemini ở /v1"
    if not re.fullmatch(r"gemini-[\w.\-]+", model): raise HTTPException(400, {"error": {"message": "Chỉ hỗ trợ model Gemini", "type": "invalid_request_error"}})
    if (body.get("n") or 1) != 1: raise HTTPException(400, "Chỉ hỗ trợ n=1")
    body = _guard_in(k, body)
    chain = await model_gate(k, model, _chain(k, model))   # GĐ11.14 workspace model allow-list (403)
    stream = bool(body.get("stream"))
    cid = "chatcmpl-" + uuid.uuid4().hex[:24]; created = int(time.time())
    ckey = None
    if k["cache_ttl_s"] and not stream and (body.get("temperature") in (None, 0, 0.0) or body.get("cache") is True):
        ckey = _h(json.dumps({"ws": k["workspace_id"], **{k2: body.get(k2) for k2 in ("model", "messages", "tools", "response_format", "temperature", "max_tokens", "stop", "top_p")}}, sort_keys=True, ensure_ascii=False))   # GĐ10.1: the workspace is part of the cache key (no cross-workspace cache hit)
        hit = await A.q("SELECT response FROM gw_cache WHERE h=%s AND created > now() - %s * interval '1 second'", (ckey, k["cache_ttl_s"]), one=True)
        if hit:
            await A.q("UPDATE gw_cache SET hits=hits+1 WHERE h=%s", (ckey,))
            resp = {**hit["response"], "id": cid, "created": created, "system_fingerprint": "agentos-cache"}
            await _record(k, resp.get("model"), 0, 0, 1, "ok", None, body, resp["choices"][0]["message"], cached=True)
            return JSONResponse(resp, headers={"x-agentos-cache": "hit"})
    ids = [tc.get("id") for m in body.get("messages") or [] for tc in (m.get("tool_calls") or []) if tc.get("id")]
    sigs = {r["call_id"]: bytes(r["sig"]) for r in await A.q("SELECT call_id, sig FROM gw_tool_sigs WHERE call_id = ANY(%s)", (ids,))} if ids else {}
    contents, cfg = to_gemini(body, sigs)
    if stream: return StreamingResponse(with_ticket(_stream(k, body, chain, contents, cfg, cid, created)), media_type="text/event-stream")
    last = None
    for m in chain:
        t0 = time.perf_counter()
        try:
            r = await A._g.aio.models.generate_content(model=m, contents=contents, config=cfg)
        except Exception as e:  # noqa: BLE001
            last = e; ms = int((time.perf_counter() - t0) * 1000)
            await _record(k, m, 0, 0, ms, "error", str(e)[:300], body, None)
            if any(x in str(e) for x in gw.RETRYABLE): gw._mark(m, str(e)); continue
            raise HTTPException(400, {"error": {"message": str(e)[:500], "type": "upstream_error"}})  # noqa: B904
        ms = int((time.perf_counter() - t0) * 1000); i, o = _usage(r); det = _details(r)
        so = {}; msg, finish = _msg_from(r, so); await _save_sigs(so)
        if k["pii_mode"] != "off" and msg.get("content"):
            if k["pii_mode"] == "mask": msg["content"] = pii_mask(msg["content"])
            elif pii_scan(msg["content"]): msg["content"] = "[Câu trả lời bị guardrail PII chặn]"; finish = "content_filter"
        resp = {"id": cid, "object": "chat.completion", "created": created, "model": m,
                "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                "usage": _usage_extra(i, o, det)}
        await _record(k, m, i, o, ms, "ok", None, body, msg, details=det)
        if ckey and not msg.get("tool_calls"): await A.q("INSERT INTO gw_cache(h,vkey_id,response) VALUES(%s,%s,%s) ON CONFLICT(h) DO UPDATE SET response=EXCLUDED.response, created=now()", (ckey, k["id"], Jsonb(resp)))
        return JSONResponse(resp, headers={"x-agentos-cache": "miss" if ckey else "off", "x-agentos-model": m,
                                           **({"x-agentos-route": json.dumps({k2: route_info.get(k2) for k2 in ("mode", "tier", "reason", "objective")}, ensure_ascii=False)} if route_info else {})})
    raise HTTPException(503, {"error": {"message": f"Tất cả model trong chuỗi fallback đều lỗi: {str(last)[:300]}", "type": "upstream_unavailable"}})

async def _stream(k, body, chain, contents, cfg, cid, created):
    def chunk(delta, finish=None, model=None, usage=None):
        d = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if usage: d["usage"] = usage
        return "data: " + json.dumps(d, ensure_ascii=False) + "\n\n"
    last = None
    for m in chain:
        t0 = time.perf_counter(); full = []; i = o = 0; det = {}; started = False; calls = []
        try:
            async for r in await A._g.aio.models.generate_content_stream(model=m, contents=contents, config=cfg):
                if not started: yield chunk({"role": "assistant", "content": ""}, model=m); started = True
                if r.usage_metadata: i, o = _usage(r); det = _details(r)
                so = {}; msg, _ = _msg_from(r, so); await _save_sigs(so)
                if msg.get("content"):
                    t = pii_mask(msg["content"]) if k["pii_mode"] == "mask" else msg["content"]
                    full.append(t); yield chunk({"content": t}, model=m)
                for n, tc in enumerate(msg.get("tool_calls") or []):
                    calls.append(tc); yield chunk({"tool_calls": [{"index": len(calls) - 1, **tc}]}, model=m)
            ms = int((time.perf_counter() - t0) * 1000)
            yield chunk({}, "tool_calls" if calls else "stop", m, _usage_extra(i, o, det))
            yield "data: [DONE]\n\n"
            await _record(k, m, i, o, ms, "ok", None, body, {"role": "assistant", "content": "".join(full), "tool_calls": calls or None}, stream=True, details=det)
            return
        except Exception as e:  # noqa: BLE001
            last = e; await _record(k, m, 0, 0, int((time.perf_counter() - t0) * 1000), "error", str(e)[:300], body, None, stream=True)
            if started or not any(x in str(e) for x in gw.RETRYABLE): break
            gw._mark(m, str(e))
    yield "data: " + json.dumps({"error": {"message": str(last)[:300], "type": "upstream_error"}}) + "\n\n"
    yield "data: [DONE]\n\n"

@router.post("/v1/embeddings")
async def embeddings(request: Request, authorization: str = Header(default="")):
    k = await _vkey(authorization); await budget(k); await __import__("gw_policies").enforce(k, request.headers); body = await request.json()
    inp = body.get("input"); inp = [inp] if isinstance(inp, str) else inp
    if not inp or len(inp) > 100: raise HTTPException(400, "input 1..100 phần tử")
    if k["pii_mode"] == "mask": inp = [pii_mask(x) for x in inp]
    t0 = time.perf_counter()
    dims = body.get("dimensions") or A.DIMS
    vecs = await A.aembed(inp) if dims == A.DIMS else [(await A._g.aio.models.embed_content(model=A.EMB_MODEL, contents=x, config=gt.EmbedContentConfig(output_dimensionality=dims))).embeddings[0].values for x in inp]
    toks = sum(len(x) // 4 for x in inp)
    import admin
    async with admin.as_workspace(k["workspace_id"]):   # GĐ10.1: same rule as _record (the row already names the workspace; the context keeps any default-workspace column honest)
        await A.q("INSERT INTO gateway_usage(model,input_tokens,output_tokens,cost_usd,status,latency_ms,vkey_id,workspace_id,feature) VALUES(%s,%s,0,%s,'ok',%s,%s,%s,'gateway')",
                  (A.EMB_MODEL, toks, round(toks / 1e6 * 0.20, 6), int((time.perf_counter() - t0) * 1000), k["id"], k["workspace_id"]))
    return {"object": "list", "model": A.EMB_MODEL, "data": [{"object": "embedding", "index": n, "embedding": v} for n, v in enumerate(vecs)],
            "usage": {"prompt_tokens": toks, "total_tokens": toks}}

@router.get("/api/gw/overview")
async def overview(hours: int = 24):
    return {"by_key": await A.q("""SELECT k.name, k.prefix, count(g.*) calls, count(*) FILTER (WHERE g.status<>'ok') errors, count(*) FILTER (WHERE g.cached) cache_hits,
               coalesce(sum(g.cost_usd),0) cost, round(avg(g.latency_ms) FILTER (WHERE NOT g.cached)) avg_ms FROM gw_vkeys k LEFT JOIN gateway_usage g ON g.vkey_id=k.id
               AND g.ts > now() - %s * interval '1 hour' GROUP BY k.id ORDER BY k.id""", (hours,)),
            "cache_rows": (await A.q("SELECT count(*) n, coalesce(sum(hits),0) hits FROM gw_cache", one=True)),
            **(await usage_by_user(hours))}

async def usage_by_user(hours: int = 24):
    """GĐ6.2 (B27): spend per user in the current workspace (gateway_usage.workspace_id; user_id NULL = API keys / system). Admin sees every user, others only themselves."""
    import admin
    u = admin.cur() or {}; ws = admin.cur_ws(); is_admin = u.get("role") == "admin"
    where, args = "g.workspace_id=%s AND g.ts > now() - %s * interval '1 hour'", [ws, hours]
    if not is_admin: where += " AND g.user_id=%s"; args.append(u.get("id"))
    rows = await gw._sys(f"""SELECT g.user_id, coalesce(us.username, CASE WHEN g.user_id IS NULL THEN '(API keys / hệ thống)' ELSE '#'||g.user_id END) username, count(*) calls,
        count(*) FILTER (WHERE g.status<>'ok') errors, coalesce(sum(g.input_tokens),0) in_tok, coalesce(sum(g.output_tokens),0) out_tok, coalesce(sum(g.cached_tokens),0) cached_tok,
        coalesce(sum(g.reasoning_tokens),0) reasoning_tok, coalesce(sum(g.audio_tokens),0) audio_tok, coalesce(sum(g.image_tokens),0) image_tok, coalesce(sum(g.cost_usd),0) cost
        FROM gateway_usage g LEFT JOIN users us ON us.id=g.user_id WHERE {where} GROUP BY g.user_id, us.username ORDER BY cost DESC""", tuple(args))
    return {"by_user": [{**r, "cost": float(r["cost"])} for r in rows], "by_user_total": round(sum(float(r["cost"]) for r in rows), 6), "by_user_scope": "workspace" if is_admin else "self"}

USAGE_BY = {"model": "coalesce(g.model,'?')", "provider": "coalesce(g.provider,'')", "feature": "coalesce(g.feature,'?')", "user": "coalesce(us.username, CASE WHEN g.user_id IS NULL THEN '(API keys / hệ thống)' ELSE '#'||g.user_id END)"}

@router.get("/api/gw/usage")
async def usage(days: int = 30, breakdown: str = "model"):
    """GĐ11.22: spend of the current workspace over the last `days` days grouped by model | provider | feature | user. Top 6 rows + 'Khác'; every row has its share of the total.
    Admin sees the whole workspace, others only their own calls; the demo workspace gets 403 (page hidden)."""
    import admin
    u = admin.cur() or {}; ws = admin.cur_ws()
    if u.get("demo"): raise HTTPException(403, "Không có trong tài khoản demo")
    if breakdown not in USAGE_BY: raise HTTPException(422, "breakdown phải là model, provider, feature hoặc user")
    if not 1 <= days <= 365: raise HTTPException(422, "days từ 1 đến 365")
    where, args = "g.workspace_id=%s AND g.ts > now() - %s * interval '1 day'", [ws, days]
    if u.get("role") != "admin": where += " AND g.user_id=%s"; args.append(u.get("id"))
    rows = await gw._sys(f"""SELECT {USAGE_BY[breakdown]} k, coalesce(g.model,'?') m, count(*) calls, count(*) FILTER (WHERE g.status<>'ok') errors, coalesce(sum(g.input_tokens),0) in_tok,
        coalesce(sum(g.output_tokens),0) out_tok, coalesce(sum(g.cost_usd),0) cost FROM gateway_usage g LEFT JOIN users us ON us.id=g.user_id WHERE {where} GROUP BY 1, 2""", tuple(args))
    agg = {}
    for r in rows:
        k = r["k"]
        if breakdown == "provider" and not k:   # rows written before 11.3 have no provider: classify by model name
            try: import gw_providers; k = gw_providers._price_provider(gw._cn(r["m"]))
            except Exception: k = "?"   # noqa: BLE001
        a = agg.setdefault(k, {"key": k, "calls": 0, "errors": 0, "in_tok": 0, "out_tok": 0, "cost": 0.0})
        a["calls"] += r["calls"]; a["errors"] += r["errors"]; a["in_tok"] += int(r["in_tok"]); a["out_tok"] += int(r["out_tok"]); a["cost"] += float(r["cost"])
    ordered = sorted(agg.values(), key=lambda a: (-a["cost"], -a["calls"], str(a["key"])))
    top, rest = ordered[:6], ordered[6:]
    if rest: top.append({"key": "Khác", "other": True, "calls": sum(a["calls"] for a in rest), "errors": sum(a["errors"] for a in rest), "in_tok": sum(a["in_tok"] for a in rest),
                         "out_tok": sum(a["out_tok"] for a in rest), "cost": sum(a["cost"] for a in rest)})
    total = {"calls": sum(a["calls"] for a in ordered), "errors": sum(a["errors"] for a in ordered), "in_tok": sum(a["in_tok"] for a in ordered), "out_tok": sum(a["out_tok"] for a in ordered),
             "cost": round(sum(a["cost"] for a in ordered), 6)}
    for a in top: a["cost"] = round(a["cost"], 6); a["share"] = round(a["cost"] / total["cost"], 4) if total["cost"] else 0.0
    return {"days": days, "breakdown": breakdown, "rows": top, "total": total, "scope": "workspace" if u.get("role") == "admin" else "self"}

# ---------------- Pricing (GĐ6.2): list / refresh from LiteLLM (proposal only) / apply. Admin-only (admin.ADMIN_ONLY).
@router.get("/api/gateway/pricing")
async def pricing_get():
    import pricing
    return {"meta": gw.PRICING_SOURCE, "models": gw.PRICING, "pending": pricing.pending()}

@router.post("/api/gateway/pricing/refresh")
async def pricing_refresh():
    import pricing
    try: return await pricing.refresh()
    except Exception as e: raise HTTPException(502, f"Không lấy được bảng giá LiteLLM: {str(e)[:200]}")  # noqa: B904

@router.post("/api/gateway/pricing/apply")
async def pricing_apply():
    import pricing
    r = pricing.apply()
    if r is None: raise HTTPException(409, "Chưa có đề xuất giá nào — bấm Cập nhật bảng giá trước")
    gw.reload_pricing()
    return r

async def cache_gc():
    await A.q("DELETE FROM gw_tool_sigs WHERE created < now() - interval '2 days'")
    await A.q("DELETE FROM gw_cache c USING gw_vkeys k WHERE c.vkey_id=k.id AND c.created < now() - GREATEST(k.cache_ttl_s,60) * interval '1 second'")
