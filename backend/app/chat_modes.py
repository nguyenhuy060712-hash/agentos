"""Chat modes (27/10/2026, docs/CHE_DO_CHAT_PLAN.md): the person chatting picks Fast / Standard / Thinking in the composer; the agent builder sets the
model + thinking level behind each mode (AgentCfg.chat_modes). Pure module (no DB, no gateway import): defaults, validation, resolution, level clamping.

  chat_modes = {"default": "standard", "enabled": ["fast","standard","deep"], "overrides": {"<id>": {"model": "...", "thinking": "low|medium|high|null"}}}   (absent = DEFAULTS)
  resolve(chat_modes, allowed_models) -> ([{"id","model","thinking","timeout_s"}, ...] in MODES order, default_id)
  think_for(model, level) -> the level the model really accepts (nearest), or None = provider default. Sent as `reasoning_effort` through gw_providers.build_model
                             (langchain-google-genai: alias thinking_level; langchain-openai: reasoning_effort; langchain-anthropic: output_config.effort)."""
from __future__ import annotations
import contextvars
from dataclasses import dataclass

@dataclass
class RunContext:
    """LangChain runtime context (create_agent(context_schema=RunContext) / astream(context=...), docs.langchain.com/oss/python/deepagents/models): the chat mode of THIS run,
    resolved at run start. GatewayMiddleware reads it from `request.runtime.context` on every model call (main agent and sub-agents), no agent rebuild."""
    chat_mode: str | None = None
    model: str | None = None
    thinking: str | None = None
    timeout_s: int | None = None

# Transport only: server._execute resolves configurable.chat_mode and sets this (task-local); agent runner passes it as astream(context=...). Reset at every run start.
RUN_CTX: contextvars.ContextVar = contextvars.ContextVar("aos_run_ctx", default=None)

MODES = ("fast", "standard", "deep")
LEVELS = ("low", "medium", "high")
TIMEOUT_S = {"fast": 120, "standard": 180, "deep": 300}   # seconds a model may take before the gateway falls back (chat runs only); longer since anh chose gemini-3.8-flash everywhere (27/10)

# Per mode: candidate models (the first one the agent may use wins) + thinking. Measured with the real API on 27/10/2026: docs/CHE_DO_CHAT_RESEARCH.md.
DEFAULTS = {
    # anh 27/10/2026 "Dùng 3.8 hết": gemini-3.8-flash first in every mode (it measured slow that day: 156 s at LOW / 503 "high demand"); the others are fallbacks
    "fast": {"models": ["gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.6-flash"], "thinking": "low"},
    "standard": {"models": ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3-flash-preview"], "thinking": None},
    "deep": {"models": ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3-flash-preview"], "thinking": "high"},
}

# Gemini 3+ levels per model (ai.google.dev/gemini-api/docs/thinking, 27/10/2026). Our UI only ever sends low/medium/high ("minimal" is never sent: 3.8-flash refuses it).
_GEMINI_LEVELS = (("gemini-4", ("low", "medium", "high")), ("gemini-3.8", ("low", "medium", "high")), ("gemini-3.6", LEVELS), ("gemini-3.5", LEVELS),
                  ("gemini-3.1-pro", ("low", "medium", "high")), ("gemini-3.1-flash-lite", LEVELS), ("gemini-3-flash", LEVELS), ("gemini-3-pro", ("low", "high")))

def _levels(model: str):
    m = (model or "").lower()
    if m.startswith("anthropic/claude-"): return LEVELS          # effort low|medium|high on every Claude 5.x model (platform.claude.com/docs/en/build-with-claude/effort)
    if m.startswith(("gpt-5", "gpt-6", "o3", "o4")): return LEVELS   # reasoning models only; gpt-4.x / mini non-reasoning models would answer 400
    for p, lv in _GEMINI_LEVELS:
        if m.startswith(p): return lv
    return ()   # custom/, deepseek/, older Gemini, unknown: provider default (never a 400 to the user)

def think_for(model: str, level):
    """Nearest level the model accepts (ties go up); None when the model has no level control or level is None (= provider default)."""
    if level not in LEVELS: return None
    ok = _levels(model)
    if not ok: return None
    if level in ok: return level
    i = LEVELS.index(level)
    return min(ok, key=lambda x: (abs(LEVELS.index(x) - i), -LEVELS.index(x)))

def validate(v):
    """Normalise AgentCfg.chat_modes; ValueError on a bad shape. None / {} = defaults. Whether the models are allowed for the agent is checked at resolve time."""
    if v in (None, {}): return None
    if not isinstance(v, dict): raise ValueError("chat_modes phải là object / chat_modes must be an object")
    bad = set(v) - {"default", "enabled", "overrides"}
    if bad: raise ValueError(f"chat_modes: khoá lạ {sorted(bad)} / unknown keys")
    out = {}
    en = v.get("enabled")
    if en is not None:
        if not isinstance(en, list) or any(x not in MODES for x in en): raise ValueError("chat_modes.enabled chỉ nhận fast | standard | deep / only fast | standard | deep")
        en = [m for m in MODES if m in en]
        if not en: raise ValueError("chat_modes.enabled cần ít nhất 1 chế độ / needs at least one mode")
        out["enabled"] = en
    d = v.get("default")
    if d is not None:
        if d not in MODES: raise ValueError("chat_modes.default chỉ nhận fast | standard | deep")
        if en and d not in en: raise ValueError("chat_modes.default phải nằm trong enabled / default must be an enabled mode")
        out["default"] = d
    ov = v.get("overrides")
    if ov is not None:
        if not isinstance(ov, dict) or set(ov) - set(MODES): raise ValueError("chat_modes.overrides: khoá là fast | standard | deep")
        o2 = {}
        for k, x in ov.items():
            if not isinstance(x, dict) or set(x) - {"model", "thinking"}: raise ValueError("chat_modes.overrides.<mode> chỉ có model, thinking")
            y = {}
            if x.get("model") is not None:
                if not isinstance(x["model"], str) or not 0 < len(x["model"]) <= 120: raise ValueError("chat_modes.overrides.model không hợp lệ")
                y["model"] = x["model"]
            if "thinking" in x:
                if x["thinking"] not in (None, *LEVELS): raise ValueError("chat_modes.overrides.thinking chỉ nhận low | medium | high | null")
                y["thinking"] = x["thinking"]
            if y: o2[k] = y
        if o2: out["overrides"] = o2
    return out or None

def resolve(chat_modes, allowed):
    """-> (modes, default_id). A mode is listed only when enabled AND it has a usable model: the override's model if the agent may use it, else the first allowed default."""
    cm = validate(chat_modes) or {}
    enabled = cm.get("enabled") or list(MODES)
    out = []
    for mid in MODES:
        if mid not in enabled: continue
        ov = (cm.get("overrides") or {}).get(mid) or {}
        dflt = DEFAULTS[mid]
        model = ov["model"] if ov.get("model") in allowed else next((m for m in dflt["models"] if m in allowed), None)
        if not model: continue
        level = ov["thinking"] if "thinking" in ov else dflt["thinking"]
        out.append({"id": mid, "model": model, "thinking": think_for(model, level), "timeout_s": TIMEOUT_S[mid]})
    ids = [m["id"] for m in out]
    d = cm.get("default")
    default = d if d in ids else ("standard" if "standard" in ids else (ids[0] if ids else None))
    return out, default
