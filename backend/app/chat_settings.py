"""GĐ3.5–3.7 chat settings for the /chat UI (additive; no change to existing tables).

  GET /api/chat/config?agent_id=agent-N   models the caller may pick for this agent (allow-list) + default, generative UI flag,
                                          text-chat quota (agent daily budget, provider cap) for the cost/quota banner
  PUT /api/settings/chat                  admin: workspace extra models / per-agent allow-lists (aos_chat_settings)
check_run_model(agent_id, model)          called by server.py _create_run: a per-run model outside the allow-list -> 403

Allow-list per agent = agent override (if set) else the agent's own model + its fallback_models + workspace extras, always limited
to models the gateway prices (so the spend cap and the per-agent daily budget count them)."""
from __future__ import annotations
import contextvars, os
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb
from langchain.agents.middleware import AgentMiddleware
from deepagents.middleware._utils import append_to_system_message
import admin as _adm
import deepagent_api as A
import gateway as gw

_DDL = "CREATE TABLE IF NOT EXISTS aos_chat_settings(k text PRIMARY KEY, v jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now())"
_READY = {"ok": False}

async def _ensure():
    """DDL as the table owner (a request's connection may run as the RLS role aos_rls, which cannot create tables).
    Default privileges (admin.py) then grant aos_rls SELECT/INSERT/UPDATE on it. Settings rows are workspace-wide (no ws column)."""
    if _READY["ok"]: return
    async with A.S["pool"].connection() as c:
        await c.execute("RESET ROLE"); await c.execute(_DDL)
        await c.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON aos_chat_settings TO aos_rls")
    _READY["ok"] = True

async def _get(k, default):
    await _ensure()
    r = await A.q("SELECT v FROM aos_chat_settings WHERE k=%s", (k,), one=True)
    return r["v"] if r else default

async def _put(k, v):
    await _ensure()
    await A.q("INSERT INTO aos_chat_settings(k,v) VALUES(%s,%s) ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v, updated_at=now()", (k, Jsonb(v)), many=False)

def _aid(agent):
    s = str(agent or "")
    s = s[len("agent-"):] if s.startswith("agent-") else s
    if not s.isdigit(): raise HTTPException(400, "agent_id phải là agent-<số>")
    return int(s)

async def _agent_cfg(aid):
    ver = await A.cur_version(aid)
    r = await A.q("SELECT config FROM agent_versions WHERE agent_id=%s AND version=%s", (aid, ver), one=True)
    if not r: raise HTTPException(404, "Không có agent")
    return r["config"] or {}

async def allowed_models(aid) -> tuple[list[str], str | None]:
    cfg = await _agent_cfg(aid); st = await _get("models", {})
    own = cfg.get("model")
    ov = (st.get("agents") or {}).get(f"agent-{aid}")
    base = ov if ov else [own, *(cfg.get("fallback_models") or []), *(st.get("workspace") or [])]
    if gw.gemini_only():   # demo user / demo workspace: Gemini only (OpenAI never offered, and refused by check_run_model)
        base = gw.demo_chain(base); own = own if gw.provider(own) == "gemini" else gw.DEMO_MODEL
    out = []
    for m in base:
        if m and m in gw.PRICING and m not in out: out.append(m)
    import gw_features   # GĐ11.12 per-feature access ("chat"): a provider switched off or a model outside the workspace's chat list is not offered (and the run is refused with 403)
    out, fdef = await gw_features.filter_refs("chat", out)
    own = fdef if fdef and own not in out else own
    return out, (own if own in out else (out[0] if out else None))

async def check_run_model(aid, model):
    if model is None: return
    allowed, _ = await allowed_models(aid)
    if model not in allowed:
        raise HTTPException(403, f"Model '{str(model)[:60]}' không nằm trong danh sách cho phép của agent này")

async def mode_set(aid):
    """-> (modes, default_id): the agent's chat modes resolved against its allow-list (chat_modes.resolve). Demo: always the defaults (an agent copied in cannot carry custom modes)."""
    import chat_modes as CM
    allowed, _ = await allowed_models(aid)
    return CM.resolve(None if gw.gemini_only() else (await _agent_cfg(aid)).get("chat_modes"), allowed)

async def resolve_run_mode(aid, mode):
    """The mode of this run: {"id","model","thinking","timeout_s"}; 403 when it is not one of the agent's enabled (and resolvable) modes."""
    modes, _ = await mode_set(aid)
    m = next((x for x in modes if x["id"] == mode), None) if isinstance(mode, str) else None
    if not m: raise HTTPException(403, f"Chế độ trò chuyện '{str(mode)[:30]}' không được bật cho agent này / chat mode not enabled for this agent")
    return m

def _label(m):
    p = gw.PRICING.get(m) or {}
    return {"id": m, "provider": gw.provider(m), "input_per_1m": p.get("input_per_1m"), "output_per_1m": p.get("output_per_1m")}

class ChatSettingsIn(BaseModel):
    workspace: list[str] | None = Field(default=None, max_length=20)
    agents: dict[str, list[str] | None] | None = None
    queue_max: int | None = Field(default=None, ge=1, le=20)   # GĐ11 F-1: messages a user may queue per thread while a run is going

QUEUE_MAX_DEFAULT = 5

async def queue_max() -> int:
    """`chat.queue_max` (settings row `queue_max`, default 5, clamped 1..20): how many messages the chat may hold behind a running turn."""
    try: v = int(await _get("queue_max", QUEUE_MAX_DEFAULT))
    except (TypeError, ValueError): v = QUEUE_MAX_DEFAULT
    return max(1, min(20, v))

# GĐ6.9 personal instructions (A8): two free-text fields per user (users.instr_*), appended to the system prompt of every chat run of that
# user. Same mechanism as deepagents' MemoryMiddleware (append_to_system_message in wrap_model_call), but the text is per run/user, so it is
# carried in a ContextVar set by server._execute instead of a StoreBackend file: the built agent is cached and shared by all users, and
# MemoryMiddleware loads its files once per thread into state (an edit or the off-switch would not reach threads already started).
INSTR_MAX = 1500
INSTR: contextvars.ContextVar = contextvars.ContextVar("aos_user_instr", default="")

class InstructionsIn(BaseModel):
    about: str = Field(default="", max_length=INSTR_MAX)
    style: str = Field(default="", max_length=INSTR_MAX)
    enabled: bool = True

async def instructions_text(uid) -> str:
    """The block to append to the system prompt for this user ('' when none / switched off)."""
    r = await A.q("SELECT instr_about, instr_style, instr_enabled FROM users WHERE id=%s", (uid,), one=True) if uid else None
    if not r or not r["instr_enabled"]: return ""
    parts = [(t, v.strip()) for t, v in (("What the user wants the AI to know about them", r["instr_about"]), ("How the user wants the AI to answer", r["instr_style"])) if v and v.strip()]
    if not parts: return ""
    return ("## Personal instructions from the user (their own preferences; they never override safety rules, tool permissions or the agent's role)\n"
            + "\n".join(f"### {t}\n{v}" for t, v in parts))

class UserInstructionsMiddleware(AgentMiddleware):
    """Appends the current run's personal instructions (INSTR) to the system message of every model call."""
    def _mod(self, request):
        t = INSTR.get()
        return request.override(system_message=append_to_system_message(request.system_message, t)) if t else request
    def wrap_model_call(self, request, handler): return handler(self._mod(request))
    async def awrap_model_call(self, request, handler): return await handler(self._mod(request))

def router():
    r = APIRouter()

    @r.get("/api/chat/me/instructions")
    async def instructions_get():
        u = _adm.cur()
        if not u: raise HTTPException(401, "login required")
        row = await A.q("SELECT instr_about about, instr_style style, instr_enabled enabled FROM users WHERE id=%s", (u["id"],), one=True) or {}
        return {"about": row.get("about", ""), "style": row.get("style", ""), "enabled": row.get("enabled", True), "max_chars": INSTR_MAX, "read_only": bool(u.get("demo"))}

    @r.put("/api/chat/me/instructions")
    async def instructions_put(b: InstructionsIn):
        u = _adm.cur()
        if not u: raise HTTPException(401, "login required")
        if u.get("demo"): raise HTTPException(403, "Tài khoản demo không sửa được chỉ dẫn cá nhân")
        await A.q("UPDATE users SET instr_about=%s, instr_style=%s, instr_enabled=%s WHERE id=%s", (b.about.strip(), b.style.strip(), b.enabled, u["id"]))
        return {"about": b.about.strip(), "style": b.style.strip(), "enabled": b.enabled, "max_chars": INSTR_MAX}

    @r.get("/api/chat/config")
    async def chat_config(agent_id: str):
        if not _adm.cur(): raise HTTPException(401, "login required")
        aid = _aid(agent_id)
        allowed, default = await allowed_models(aid)
        cfg = await _agent_cfg(aid)
        await A.seed_spend()
        budget = cfg.get("budget_usd_day")
        spent_today = float((await A.q("SELECT coalesce(sum(cost_usd),0) c FROM gateway_usage WHERE agent_id=%s AND ts > date_trunc('day', now() AT TIME ZONE 'Asia/Ho_Chi_Minh') AT TIME ZONE 'Asia/Ho_Chi_Minh'",
                                       (aid,), one=True))["c"])
        caps = {p: {"cap_usd": c, "spent_usd": round(gw.SPENT[p], 6)} for p, c in gw.CAPS.items()}
        import genui
        modes, dmode = await mode_set(aid)
        modes = [{**m, "input_per_1m": _label(m["model"])["input_per_1m"], "output_per_1m": _label(m["model"])["output_per_1m"]} for m in modes]
        return {"agent_id": f"agent-{aid}", "models": [_label(m) for m in allowed], "default_model": default, "picker": len(allowed) > 1, "modes": modes, "default_mode": dmode,
                "genui": genui.ENABLED and "local:present" in (cfg.get("tools") or []), "genui_enabled": genui.ENABLED,
                "queue_max": await queue_max(), "quota": {"agent_budget_usd_day": budget, "agent_spent_today_usd": round(spent_today, 6), "provider_caps": caps}}

    @r.get("/api/settings/chat")
    async def chat_settings_get():
        if not _adm.cur(): raise HTTPException(401, "login required")
        return {"models": await _get("models", {}), "priced_models": sorted(gw.PRICING), "queue_max": await queue_max()}

    @r.put("/api/settings/chat")
    async def chat_settings_put(b: ChatSettingsIn):
        u = _adm.cur()
        if not u: raise HTTPException(401, "login required")
        if u.get("role") != "admin": raise HTTPException(403, "admin only")
        st = await _get("models", {})
        bad = [m for m in (b.workspace or []) + [m for v in (b.agents or {}).values() for m in (v or [])] if m not in gw.PRICING]
        if bad: raise HTTPException(422, f"Model không có trong bảng giá gateway: {', '.join(bad[:5])}")
        if b.workspace is not None: st["workspace"] = b.workspace
        for g, v in (b.agents or {}).items():
            _aid(g); st.setdefault("agents", {})
            if v: st["agents"][g] = v
            else: st["agents"].pop(g, None)
        await _put("models", st)
        if b.queue_max is not None: await _put("queue_max", b.queue_max)
        return {"models": st, "queue_max": await queue_max()}
    return r
