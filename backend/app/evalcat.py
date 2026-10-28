"""GD7.6 (23/10/2026): evaluator catalogue from openevals 0.2.0 (MIT).
Evaluator kind `openevals`: config {key, template: <id below>, continuous?: bool, choices?: [floats], model?, few_shot_examples?: [openevals FewShotExample]}.
Reused from openevals: `create_async_llm_as_judge` (prompt formatting, few-shot block, structured output), the prompt constants in `openevals.prompts`.
Ours: GatewayJudge = a LangChain chat model that sends the judge prompt through OUR gateway (fallbacks, usage rows, spend caps with evaluator_id + feature 'evaluator'),
the run -> prompt-variables mapping (tool outputs as RAG context, write_todos as plan, thread transcript), and the VI descriptions."""
import contextvars, json, re
from fastapi import HTTPException
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda

FEATURE = "evaluator"
FEATURE_OVERRIDE: contextvars.ContextVar = contextvars.ContextVar("aos_evalcat_feature", default=None)   # GD7.5: judge calls made by alignment count under feature 'align'

def _t(id_, const, group, name, en, vi, bad_is_true=False, needs=()):
    return {"id": id_, "const": const, "group": group, "name": name, "description": en, "description_vi": vi, "bad_is_true": bad_is_true, "needs": list(needs)}

# id -> openevals.prompts constant. `bad_is_true`: the prompt asks "is the problem present?" so score 1 = problem found (kept as openevals defines it).
TEMPLATES = [
    _t("correctness", "CORRECTNESS_PROMPT", "quality", "Correctness", "Is the answer accurate and complete compared with the reference answer?", "Câu trả lời có chính xác và đầy đủ so với đáp án tham chiếu không?", needs=["reference"]),
    _t("conciseness", "CONCISENESS_PROMPT", "quality", "Conciseness", "Does the answer contain only what was asked, without padding?", "Câu trả lời có gọn, chỉ nêu điều được hỏi, không dài dòng không?"),
    _t("hallucination", "HALLUCINATION_PROMPT", "quality", "Hallucination", "Is every claim supported by the context (tool results / reference)?", "Mọi khẳng định có được ngữ cảnh (kết quả tool / đáp án mẫu) chứng minh không, hay là bịa?", needs=["context"]),
    _t("answer_relevance", "ANSWER_RELEVANCE_PROMPT", "quality", "Answer relevance", "Does the answer address what the user asked?", "Câu trả lời có đúng trọng tâm câu hỏi của người dùng không?"),
    _t("rag_groundedness", "RAG_GROUNDEDNESS_PROMPT", "rag", "RAG groundedness", "Is the answer grounded in the retrieved documents?", "Câu trả lời có bám sát các tài liệu đã truy xuất không?", needs=["context"]),
    _t("rag_helpfulness", "RAG_HELPFULNESS_PROMPT", "rag", "RAG helpfulness", "Is the answer helpful and relevant for the question?", "Câu trả lời có hữu ích và liên quan đến câu hỏi không?"),
    _t("rag_retrieval_relevance", "RAG_RETRIEVAL_RELEVANCE_PROMPT", "rag", "RAG retrieval relevance", "Are the retrieved documents relevant to the question?", "Các tài liệu truy xuất được có liên quan đến câu hỏi không?", needs=["context"]),
    _t("toxicity", "TOXICITY_PROMPT", "safety", "Toxicity", "Detects insults, hostility or demeaning language. Score 1 = toxic found.", "Phát hiện lời lẽ xúc phạm, thù địch, hạ thấp người khác. Điểm 1 = có độc hại.", bad_is_true=True),
    _t("pii_leakage", "PII_LEAKAGE_PROMPT", "security", "PII leakage", "Detects personal data that could identify a person. Score 1 = PII found.", "Phát hiện dữ liệu cá nhân có thể nhận diện một người. Điểm 1 = có lộ thông tin cá nhân.", bad_is_true=True),
    _t("prompt_injection", "PROMPT_INJECTION_PROMPT", "security", "Prompt injection", "Detects attempts to override the agent's instructions in the user input. Score 1 = attempt found.", "Phát hiện nỗ lực ghi đè chỉ dẫn của agent trong tin nhắn người dùng. Điểm 1 = có tấn công.", bad_is_true=True),
    _t("trajectory_accuracy", "TRAJECTORY_ACCURACY_PROMPT", "trajectory", "Trajectory accuracy", "Are the agent's steps logical, progressive and reasonably efficient?", "Các bước agent đã làm có hợp lý, tiến triển rõ ràng và không thừa không?", needs=["trajectory"]),
    _t("tool_selection", "TOOL_SELECTION_PROMPT", "trajectory", "Tool selection", "Did the agent pick the right tools, without redundant calls?", "Agent có chọn đúng công cụ, không gọi thừa không?", needs=["trajectory"]),
    _t("plan_adherence", "PLAN_ADHERENCE_PROMPT", "trajectory", "Plan adherence", "Did the agent follow its own declared plan (write_todos)?", "Agent có làm đúng kế hoạch đã nêu (write_todos) không?", needs=["trajectory", "plan"]),
    _t("task_completion", "TASK_COMPLETION_PROMPT", "conversation", "Task completion", "Whole conversation: was every user request fully completed?", "Cả cuộc hội thoại: mọi yêu cầu của người dùng có được hoàn thành đầy đủ không?", needs=["conversation"]),
    _t("user_satisfaction", "USER_SATISFACTION_PROMPT", "conversation", "User satisfaction", "Whole conversation: does the user seem satisfied?", "Cả cuộc hội thoại: người dùng có vẻ hài lòng không?", needs=["conversation"]),
    _t("knowledge_retention", "KNOWLEDGE_RETENTION_PROMPT", "conversation", "Knowledge retention", "Whole conversation: does the agent remember and use facts given earlier?", "Cả cuộc hội thoại: agent có nhớ và dùng đúng thông tin người dùng đã cho trước đó không?", needs=["conversation"]),
    _t("agent_tone", "AGENT_TONE_PROMPT", "conversation", "Agent tone", "Whole conversation: is the agent's tone appropriate and consistent?", "Cả cuộc hội thoại: giọng điệu của agent có phù hợp và nhất quán không?", needs=["conversation"]),
    # GĐ12.68: more openevals prompts. bad_is_true: score 1 = problem found; `value` of the feedback shows "pass" (score 0) / "fail" (score 1).
    _t("code_injection", "CODE_INJECTION_PROMPT", "security", "Code injection", "Detects attempts to inject executable code (os.system, eval, SQL...) in the user input. Score 1 = injection found; value = pass/fail (pass = score 0).", "Phát hiện nỗ lực chèn mã thực thi (os.system, eval, SQL...) trong tin nhắn người dùng. Điểm 1 = có chèn mã; giá trị = pass/fail (pass = điểm 0).", bad_is_true=True),
    _t("fairness", "FAIRNESS_PROMPT", "safety", "Fairness / bias", "Detects bias or discrimination against people or groups. Score 1 = bias found; value = pass/fail (pass = score 0).", "Phát hiện thiên vị hoặc phân biệt đối xử với cá nhân hoặc nhóm người. Điểm 1 = có thiên vị; giá trị = pass/fail (pass = điểm 0).", bad_is_true=True),
    _t("code_correctness", "CODE_CORRECTNESS_PROMPT", "quality", "Code correctness", "Is the code in the answer correct and does it solve the task?", "Mã trong câu trả lời có đúng và giải được yêu cầu không?"),
    _t("language_detection", "LANGUAGE_DETECTION_PROMPT", "quality", "Language detection", "Is the answer written in the expected language?", "Câu trả lời có viết đúng ngôn ngữ mong đợi không?"),
    _t("support_intent", "SUPPORT_INTENT_PROMPT", "conversation", "Support intent", "Whole conversation: does the user get what they came to the support chat for?", "Cả cuộc hội thoại: người dùng có đạt được mục đích khi vào chat hỗ trợ không?", needs=["conversation"]),
    _t("perceived_error", "PERCEIVED_ERROR_PROMPT", "quality", "Perceived error", "Does the answer look like an error or a failure to the user? Score 1 = looks like an error; value = pass/fail (pass = score 0).", "Câu trả lời có khiến người dùng thấy như một lỗi/thất bại không? Điểm 1 = trông như lỗi; giá trị = pass/fail (pass = điểm 0).", bad_is_true=True),
    _t("laziness", "LAZINESS_PROMPT", "quality", "Laziness", "Does the agent give up early or leave work undone? Score 1 = lazy; value = pass/fail (pass = score 0).", "Agent có bỏ dở, làm qua loa không? Điểm 1 = lười; giá trị = pass/fail (pass = điểm 0).", bad_is_true=True),
]
BY_ID = {t["id"]: t for t in TEMPLATES}

def prompt_of(tpl):
    from openevals import prompts
    return getattr(prompts, tpl["const"])

def template_entries():
    """Entries for GET /api/evaluator_templates (same shape as the built-in ones + `group`, `description_vi`, `name_vi`-free)."""
    return [{"id": "oe_" + t["id"], "name": t["name"] + " (openevals)", "kind": "openevals", "group": t["group"], "description": t["description"],
             "description_vi": t["description_vi"], "bad_is_true": t["bad_is_true"],
             "config": {"key": t["id"], "template": t["id"]}} for t in TEMPLATES]

def validate(c):
    if c.get("template") not in BY_ID: raise HTTPException(400, "config.template must be one of: " + ", ".join(BY_ID))
    if c.get("choices") is not None and not all(isinstance(x, (int, float)) for x in c["choices"]): raise HTTPException(400, "choices must be numbers")
    if c.get("scale10") is not None and not isinstance(c["scale10"], bool): raise HTTPException(400, "scale10 must be true or false")
    for x in c.get("few_shot_examples") or []:
        if not isinstance(x, dict) or "inputs" not in x or "outputs" not in x: raise HTTPException(400, "few_shot_examples items need inputs and outputs")

# ------------------------------------------------------------------ judge = our gateway as a LangChain chat model
def _msg_text(m):
    if isinstance(m, BaseMessage): role, c = m.type, m.content
    elif isinstance(m, dict): role, c = m.get("role", "user"), m.get("content", "")
    else: role, c = "user", m
    if isinstance(c, list): c = "\n".join(p.get("text", "") for p in c if isinstance(p, dict))
    return role, str(c)

def _score_type(schema):
    try: return (schema or {}).get("properties", {}).get("score", {}).get("type", "boolean")
    except AttributeError: return "boolean"

def _parse(txt, schema):
    d = json.loads(txt)
    if isinstance(d, list): d = d[0] if d else {}
    if not isinstance(d, dict) or d.get("score") is None: raise ValueError("judge returned no score")
    s = d["score"]
    if _score_type(schema) == "boolean":
        s = (s.strip().lower() in ("true", "1", "yes", "có")) if isinstance(s, str) else bool(s)
    else:
        s = max(0.0, min(1.0, float(s)))
    out = {"score": s, "reasoning": str(d.get("reasoning") or "")}
    return out

class GatewayJudge(BaseChatModel):
    """openevals `judge=`: a BaseChatModel whose only job is to answer the judge prompt through gateway.generate (JSON mode)."""
    evaluator_id: int | None = None
    judge_model: str | None = None
    feature: str = FEATURE

    @property
    def _llm_type(self): return "agentos-gateway-judge"

    def _generate(self, messages, stop=None, run_manager=None, **kw): raise NotImplementedError("GatewayJudge is async-only")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kw):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=await self._ask(messages, None)))])

    async def _ask(self, messages, schema):
        import deepagent_api as A
        prompt = "\n\n".join((f"[{r}]\n" if r not in ("human", "user") else "") + t for r, t in map(_msg_text, messages))
        if schema is not None:
            kind = "true or false" if _score_type(schema) == "boolean" else "a number from 0.0 to 1.0"
            prompt += f'\n\nReturn ONLY JSON: {{"reasoning": "<short explanation>", "score": <{kind}>}}'
        chain = [self.judge_model or A.DEFAULT_MODEL] + A.gw.DEFAULT_FALLBACKS
        try:
            await A.gw.budget_check(A.gw.ctx_now([A.gw.provider(m) for m in A.gw._usable(chain)], evaluator_id=self.evaluator_id, feature=self.feature, models=chain), fresh=True)
            txt, _, _, _ = await A.gw.generate(A._g, prompt, chain, json_mode=True, temperature=0, evaluator_id=self.evaluator_id, feature=self.feature)
        except A.gw.SpendCapReached as e: raise HTTPException(402, str(e)) from None
        return txt

    def with_structured_output(self, schema, **kw):
        async def call(msgs): return _parse(await self._ask(msgs, schema), schema)
        def sync(_): raise NotImplementedError("GatewayJudge is async-only")
        return RunnableLambda(sync, afunc=call)

# ------------------------------------------------------------------ run -> openevals arguments
def _s(x, n=4000):
    return (x if isinstance(x, str) else json.dumps(x, ensure_ascii=False, default=str))[:n]

def _tools(run):
    return [s for s in run.get("spans") or [] if s.get("run_type") == "tool" and s.get("name") != "write_todos"]

def _trajectory_text(run):
    tl = _tools(run)
    if tl: return "\n".join(f"{i + 1}. {s['name']}({_s(s.get('inputs'), 300)}) -> {_s(s.get('outputs'), 500)}" for i, s in enumerate(tl))
    return "\n".join(f"{i + 1}. {s['name']}({_s(s.get('args'), 300)})" for i, s in enumerate(run.get("trajectory") or [])) or "(no tool calls)"

def _context(run, example):
    ex = example or {}
    for src in (ex.get("inputs"), ex.get("outputs"), ex.get("metadata")):
        if isinstance(src, dict) and src.get("context"): return _s(src["context"], 8000)
    tl = _tools(run)
    ret = [s for s in tl if re.search(r"search|kb|retriev|lookup|doc", s["name"], re.I)] or tl
    return "\n---\n".join(_s(s.get("outputs"), 2000) for s in ret)[:8000] or "(no context)"

def _plan(run, example):
    for s in run.get("spans") or []:
        if s.get("name") == "write_todos": return _s(s.get("inputs"), 3000)
    o = (example or {}).get("outputs") or {}
    return _s(o["plan"], 3000) if o.get("plan") else "(no plan)"

def build_args(tpl, run, example):
    """kwargs for the openevals evaluator: inputs/outputs/reference_outputs + the extra prompt variables the chosen prompt needs."""
    ref = (example or {}).get("outputs")
    a = {"inputs": run.get("input"), "outputs": run.get("output"), "reference_outputs": ref if ref else "(none)"}
    if "conversation" in tpl["needs"]:
        a["outputs"] = run.get("transcript") or f"User: {_s(run.get('input'))}\nAgent: {_s(run.get('output'))}"   # 12.70: evalconv.build already made `transcript` from `all_messages`
    if "trajectory" in tpl["needs"]:
        a["outputs"] = run.get("trajectory_text") or _trajectory_text(run)   # 12.70: whole-thread trajectory text (tool calls + results of every turn)
        if tpl["id"] == "plan_adherence": a["inputs"] = run.get("input")
    if "context" in tpl["needs"]: a["context"] = _context(run, example)
    if "plan" in tpl["needs"]: a["plan"] = _plan(run, example)
    a = {k: (v if v is not None else "") for k, v in a.items()}
    a["inputs"] = _s(a["inputs"]); a["outputs"] = _s(a["outputs"], 8000)
    return a

def build_evaluator(ev, judge_feature=FEATURE, few_shot_examples=None):
    from openevals.llm import create_async_llm_as_judge
    c = ev["config"]; tpl = BY_ID[c["template"]]
    few = few_shot_examples if few_shot_examples is not None else c.get("few_shot_examples")
    return tpl, create_async_llm_as_judge(
        prompt=prompt_of(tpl), feedback_key=c.get("key") or tpl["id"], continuous=bool(c.get("continuous")), choices=c.get("choices"),
        few_shot_examples=few or None, judge=GatewayJudge(evaluator_id=ev.get("id"), judge_model=c.get("model"), feature=judge_feature))

async def run_openevals(ev, run, example, judge_feature=None, few_shot_examples=None):
    c = ev["config"]; key = c.get("key") or c["template"]; judge_feature = judge_feature or FEATURE_OVERRIDE.get() or FEATURE
    tpl, evaluator = build_evaluator(ev, judge_feature, few_shot_examples)
    try: r = await evaluator(**build_args(tpl, run, example))
    except ValueError as e: return [{"key": key, "score": None, "comment": f"Judge error: {e}"}]
    if isinstance(r, list): r = r[0]
    sc = r.get("score"); sc = (1.0 if sc else 0.0) if isinstance(sc, bool) else (None if sc is None else float(sc))
    out = {"key": key, "score": sc, "comment": r.get("comment")}
    if tpl["bad_is_true"] and sc is not None: out["value"] = "pass" if sc == 0 else "fail"   # 12.68: security pass/fail display; `score` stays as openevals defines it
    if c.get("scale10") and c.get("continuous") and sc is not None: out["score"] = round(sc * 10, 4)
    return [out]
