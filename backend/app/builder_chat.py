"""GĐ8.7 (23/10/2026): create an agent by chatting. POST /api/builder/chat {messages} -> the next question (at most 5 in total: job,
audience, data, actions on behalf of the user, tone; topics the user already covered are skipped) or a final draft; POST /api/builder/chat/create
validates the (edited) draft again and creates it through the existing builder path (studio.builder_create, /api/builder/create).
How: one structured call per turn, `with_structured_output(BuilderTurn)` through the gateway with feature='builder' (6.1 spend caps), reusing
nlsearch._ask (budget_check, model fallback + timeout retry, usage rows); on an invalid turn ONE retry that shows the model the error (nlsearch pattern).
Pattern of deepagents examples/downloading_agents: agent = instructions (AGENTS.md role) + skills + config. Per-agent cost cap = a REAL monthly
spend cap (spend_caps scope 'agent', period 'month', block; GĐ9.P1, was budget_usd_day = month/30 in 8.7), created with the agent. A channel is only a hint.
Off for demo users and demo workspaces."""
from __future__ import annotations
import json, re
from typing import Literal
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ValidationError
import deepagent_api as A   # circular import: attributes are only used at call time

router = APIRouter()
FEATURE = "builder"
MAX_Q = 5
TOPICS = ("job", "audience", "data", "actions", "tone")
CHANNELS = ("none", "telegram", "zalo", "slack")
DEFAULT_CAP = 2.0       # USD / month, small on purpose
MAX_CAP = 100.0
ACTIONS_Q = {"vi": "Agent được làm gì thay anh/chị (gửi, tạo, cập nhật…)?", "en": "What may the agent do on your behalf (send, create, update…)?"}   # GĐ9.P1: never skipped (8.R)

def _lang(b) -> str:
    if b.lang: return b.lang
    return "vi" if re.search(r"[ăâđêôơưàáạảãèéẹẻẽìíịỉĩòóọỏõùúụủũỳýỵỷỹ]", " ".join(m.content for m in b.messages if m.role == "user").lower()) else "en"

class AgentDraft(BaseModel):
    """Final agent draft (shown to the user as an editable summary card)."""
    name: str = Field(description="Short agent name, in the user's language")
    description: str = Field(default="", description="One sentence: what the agent does and for whom")
    instructions: str = Field(description="Detailed system prompt for the agent, in the user's language (Vietnamese if the user writes Vietnamese)")
    tools: list[str] = Field(default_factory=list, description="Tool keys, ONLY from the given tool list")
    model: str | None = Field(default=None, description="Model id; leave empty for the workspace default")
    channel: Literal["none", "telegram", "zalo", "slack"] = Field(default="none", description="Where end users will talk to the agent")
    cost_cap_usd_month: float = Field(default=DEFAULT_CAP, description="Monthly spend cap in USD (small, e.g. 1-5)")
    skills: list[Literal["docx", "xlsx", "pptx", "pdf"]] = Field(default_factory=list, description="Built-in file skills, only if the job creates Word/Excel/PowerPoint/PDF files")

class BuilderTurn(BaseModel):
    """One builder turn: either the next question or the final draft."""
    answered: list[Literal["job", "audience", "data", "actions", "tone"]] = Field(default_factory=list, description="Topics the user has already covered")
    question: str | None = Field(default=None, description="The single next question (only if an important topic is still missing and must_finish is false)")
    draft: AgentDraft | None = Field(default=None, description="The final agent draft (when enough is known or must_finish is true)")

class Msg(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)

class ChatIn(BaseModel):
    messages: list[Msg] = Field(min_length=1, max_length=2 * MAX_Q + 2)
    lang: str | None = Field(default=None, pattern=r"^(vi|en)$")

class CreateIn(BaseModel):
    draft: dict

SYSTEM = ("You help a non-technical business user create an AI agent on the Moza Agent OS platform, by chatting. Topics to know: "
          "job (what the agent does), audience (who talks to it), data (which documents/data it uses), actions (what it does on behalf of the user), tone. "
          "Mark every topic the user already covered in `answered`. If an important topic is missing and must_finish is false, ask ONE short, friendly question "
          f"about it (never re-ask a covered topic; at most {MAX_Q} questions in total). Otherwise return `draft`. ALWAYS find out what the agent may do on the user's behalf (send, create, update, ...) before drafting: if the user has not said, ask exactly that. Write the question and the draft in the user's language "
          "(Vietnamese if the user writes Vietnamese). Draft rules: tools ONLY from the given tool list (keys exactly as given); model empty unless the user asked "
          "for one; channel = where end users chat (none|telegram|zalo|slack); cost_cap_usd_month small (default 2); skills only if the job produces Word/Excel/"
          "PowerPoint/PDF files. The instructions must be a complete system prompt: role, audience, data to rely on, allowed actions, tone, what to refuse.")

def _guard():
    import admin
    if admin.is_demo() or int(admin.cur_ws() or 0) in A.gw.DEMO_WS:
        raise HTTPException(403, "Tạo agent bằng trò chuyện không dùng được trong workspace demo")

async def _models() -> set:
    return {A.DEFAULT_MODEL, *A.gw.DEFAULT_FALLBACKS, *(A._models_cache.get("list") or [])}

async def finalize(d: AgentDraft, cat: list[dict], models: set) -> tuple[A.AgentCfg, dict, list[str]]:
    """Draft -> validated AgentCfg + the cleaned draft + warnings. Unknown / disabled / blocked tools are dropped with a note."""
    keys = {c["key"] for c in cat}; warn = []
    tools = list(dict.fromkeys(t for t in d.tools if t in keys))
    dropped = [t for t in dict.fromkeys(d.tools) if t not in keys]
    if dropped: warn.append("Đã bỏ tool không có/không bật: " + ", ".join(dropped))
    blocked = [t for t in tools if A.block_re().search(t)]
    if blocked: tools = [t for t in tools if t not in blocked]; warn.append("Đã bỏ tool bị chặn (khoá/báo động/gara): " + ", ".join(blocked))
    model = d.model if d.model in models else A.DEFAULT_MODEL
    if d.model and d.model != model: warn.append(f"Model {d.model} không có, dùng model mặc định {model}")
    cap = round(min(max(float(d.cost_cap_usd_month or DEFAULT_CAP), 0.1), MAX_CAP), 2)
    skills = list(dict.fromkeys(d.skills))
    cfg = A.AgentCfg(name=d.name.strip()[:80] or "Agent mới", description=d.description.strip()[:500], system_prompt=d.instructions.strip()[:20000],
                     model=model, tools=tools, planning=False, filesystem=bool(skills), sandbox=bool(skills),   # file skills run their helpers in the sandbox
                     skills=[f"builtin:{s}" for s in skills])
    if not cfg.system_prompt: raise ValueError("instructions is empty")
    writes = [t for t in tools if not t.startswith("local:") and not A.is_ha_read(t.split(":")[0], t.split(":", 1)[1])]
    if writes: warn.append("Tool GHI sẽ dừng chờ duyệt trước mỗi lần chạy: " + ", ".join(writes))
    clean = {"name": cfg.name, "description": cfg.description, "instructions": cfg.system_prompt, "tools": tools, "model": model,
             "channel": d.channel, "cost_cap_usd_month": cap, "skills": skills}
    return cfg, clean, warn

def _hint(channel: str) -> str | None:
    if channel == "none": return None
    return f"Kênh gợi ý: {channel}. Cài đặt kênh trong tab Kênh của agent sau khi tạo."

@router.post("/api/builder/chat")
async def builder_chat(b: ChatIn):
    _guard()
    import nlsearch, studio
    if b.messages[0].role != "user": raise HTTPException(422, "Tin nhắn đầu tiên phải là mô tả của người dùng")
    asked = sum(m.role == "assistant" for m in b.messages)
    cat = await studio._catalog(); models = await _models()
    payload = {"conversation": [m.model_dump() for m in b.messages], "questions_asked": asked, "max_questions": MAX_Q, "must_finish": asked >= MAX_Q,
               "lang": b.lang, "tools": [{"key": c["key"], "description": c["description"], "read_only": c["read_only"]} for c in cat],
               "default_model": A.DEFAULT_MODEL, "channels": list(CHANNELS)}
    msgs = [SYSTEM, "[builder] " + json.dumps(payload, ensure_ascii=False)]
    err = None
    for _ in range(2):   # one retry that shows the model its error (nlsearch pattern)
        try: turn, perr = await nlsearch._ask(msgs, schema=BuilderTurn, feature=FEATURE)
        except A.gw.SpendCapReached as e: raise HTTPException(402, str(e)) from None
        except HTTPException: raise
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, "Builder model unavailable: " + str(e)[:200]) from None
        try:
            if turn is None: raise ValueError(perr or "no structured output")
            if turn.question and not turn.draft and asked < MAX_Q:
                return {"type": "question", "question": turn.question.strip()[:1000], "asked": asked + 1, "max_questions": MAX_Q, "answered": turn.answered}
            if not turn.draft: raise ValueError(f"{asked} questions were already asked: return `draft` now, no more questions")
            if "actions" not in turn.answered and asked < MAX_Q:   # server-side guard: a draft is never made before "what may the agent do on your behalf" is covered
                return {"type": "question", "question": ACTIONS_Q[_lang(b)], "asked": asked + 1, "max_questions": MAX_Q, "answered": turn.answered}
            cfg, draft, warn = await finalize(turn.draft, cat, models)
            return {"type": "draft", "draft": draft, "config": cfg.model_dump(exclude={"note"}), "warnings": warn, "channel_hint": _hint(draft["channel"]),
                    "asked": asked, "answered": turn.answered}
        except (ValueError, ValidationError) as e: err = str(e)[:500]
        msgs = [msgs[0], msgs[1], f"BUILDER_ERROR: {err}\nFix it and answer again with a corrected BuilderTurn."]
    raise HTTPException(422, "Không tạo được bản nháp hợp lệ: " + (err or ""))

@router.post("/api/builder/chat/create")
async def builder_chat_create(b: CreateIn):
    """Edited summary card -> same validation as the draft -> existing create path (/api/builder/create). The channel stays a hint."""
    _guard()
    import studio
    try: d = AgentDraft(**b.draft)
    except ValidationError as e: raise HTTPException(422, "Bản nháp không hợp lệ: " + str(e)[:300]) from None
    try: cfg, draft, warn = await finalize(d, await studio._catalog(), await _models())
    except (ValueError, ValidationError) as e: raise HTTPException(422, "Bản nháp không hợp lệ: " + str(e)[:300]) from None
    r = await studio.builder_create(studio.CreateReq(config=cfg.model_dump(exclude={"note"})))
    import admin
    await A.q("INSERT INTO spend_caps(workspace_id,scope,target,period,limit_usd,action) VALUES(%s,'agent',%s,'month',%s,'block')", (admin.cur_ws(), str(r["id"]), draft["cost_cap_usd_month"]))
    await A.gw.caps_snapshot(fresh=True)
    return {**r, "draft": draft, "warnings": warn, "channel_hint": _hint(draft["channel"]), "test_url": f"builder?id={r['id']}&tab=test"}
