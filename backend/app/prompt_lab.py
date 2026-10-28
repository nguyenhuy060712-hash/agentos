"""GĐ9.2 Prompt Canvas + GĐ9.3 Playground lab (24/10/2026), comparable to LangSmith Prompt Canvas / Playground.
Both call models only through the gateway (`gateway.lc_call`: budget_check(feature) -> fallback -> usage rows -> cost) with LangChain v1 primitives
(`bind_tools`, `with_structured_output`, `astream`). Features for the spend caps: `prompt_canvas`, `playground`.

Prompt Canvas (/api/hub/canvas/*)
- rewrite: quick actions (shorter / examples / tone / clearer / translate VI<->EN / safety / fix from a trace's feedback / free chat instruction), optionally ONLY
  for a selected part (the server splices the answer into the selection, so the rest of the prompt is untouched by construction), streamed over SSE.
  Template variables ({{x}} and {x}) are validated; when the model drops one the server retries once with the missing list, then reports `missing_variables`.
- diff: word-level hunks (difflib on word / space / punctuation tokens) so the UI shows a live diff and accepts / rejects PER HUNK; apply = merge of the accepted hunks.
- commit: a new Prompt Hub commit whose parent is the version the canvas was opened on (+ message, optional tag); refuses a template that lost variables.

Playground lab (/api/playground/lab, /api/playground/tools)
- tools: catalogue tools or inline JSON-schema tools are bound with `bind_tools`; the model's calls are shown; each call is answered by a mock result (per tool name or
  typed by the user), or - only when `tools_run=real` - by really running a READ-ONLY catalogue tool. Write tools are never executed here (they need approval in an agent run).
- structured output: `with_structured_output(schema)` or provider JSON mode; the result is validated with jsonschema (Draft 2020-12) and every error is listed.
- images: PNG / JPEG / WEBP / GIF as base64 blocks in a human message (size, count and file signature checked; no remote URLs).
- model configs: a saved preset (model, temperature, max tokens, top_p, reasoning effort) or inline settings; 1-4 configs run side by side (no fallback when comparing).
- every run is traced (project `playground`): one chain run per config + a child llm run per model turn + a child tool run per tool call."""
import asyncio, base64, binascii, difflib, json, re, time, uuid
from typing import Any, Literal
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import deepagent_api as A
import obs

router = APIRouter()
GEMINI_RX = r"^gemini-[\w.\-]+$"
MAX_IMAGES, MAX_IMAGE_BYTES = 8, 5 * 1024 * 1024
IMAGE_SIG = {"image/png": (b"\x89PNG\r\n\x1a\n",), "image/jpeg": (b"\xff\xd8\xff",), "image/gif": (b"GIF87a", b"GIF89a"), "image/webp": (b"RIFF",)}

# ================================================================ shared helpers
_VAR_RX = re.compile(r"\{\{\s*([A-Za-z_][\w.]*)\s*\}\}|(?<!\{)\{([A-Za-z_]\w*)\}(?!\})")
def vars_of(text: str) -> set:
    """Template variables as normalised tokens: {{name}} (double braces) and {name} (single braces)."""
    return {("{{" + a + "}}") if a else ("{" + b + "}") for a, b in _VAR_RX.findall(text or "")}

def _text(m) -> str:
    t = getattr(m, "text", None)
    if isinstance(t, str): return str(t)
    c = m.content
    return c if isinstance(c, str) else "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in c)

# ---------------------------------------------------------------- word-level diff
_TOK = re.compile(r"\s+|\w+|[^\w\s]", re.U)
def _tokens(a: str, b: str):
    ta, tb = _TOK.findall(a), _TOK.findall(b)
    if len(ta) * len(tb) > 25_000_000:   # huge prompts: diff by lines (keeps O(n^2) bounded)
        ta, tb = a.splitlines(True), b.splitlines(True)
    return ta, tb

def segments(a: str, b: str) -> list:
    """Word-level diff as segments: {"t":"eq","text"} | {"t":"chg","id","old","new"} (id = hunk number, in order). Hunks separated only by whitespace are merged."""
    ta, tb = _tokens(a, b)
    raw = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes():
        raw.append(["eq" if op == "equal" else "chg", "".join(ta[i1:i2]), "".join(tb[j1:j2])])
    for k in range(1, len(raw) - 1):   # whitespace between two changes belongs to one hunk
        if raw[k][0] == "eq" and raw[k][1].strip() == "" and raw[k - 1][0] == "chg" and raw[k + 1][0] == "chg": raw[k][0] = "chg"
    merged = []
    for t, o, n in raw:
        if merged and merged[-1][0] == t: merged[-1][1] += o; merged[-1][2] += n
        else: merged.append([t, o, n])
    out, hid = [], 0
    for t, o, n in merged:
        if t == "eq": out.append({"t": "eq", "text": o})
        else: out.append({"t": "chg", "id": hid, "old": o, "new": n}); hid += 1
    return out

def apply_hunks(segs: list, accept) -> str:
    """Merge: equal text + (the new text of every accepted hunk, the old text of the others). accept = "all" | iterable of hunk ids."""
    ok = None if accept == "all" else {int(i) for i in accept}
    return "".join(s["text"] if s["t"] == "eq" else (s["new"] if ok is None or s["id"] in ok else s["old"]) for s in segs)

def _words(s): return len(re.findall(r"\w+", s, re.U))
def diff_stats(segs: list) -> dict:
    ch = [s for s in segs if s["t"] == "chg"]
    return {"hunks": len(ch), "added_words": sum(_words(s["new"]) for s in ch), "removed_words": sum(_words(s["old"]) for s in ch)}

# ---------------------------------------------------------------- PII (GĐ9.8): text parts only (never base64 images)
async def _pii_open():
    try: return await A.gw._pii_cfg()
    except Exception: return None  # noqa: BLE001

async def _mask_msgs(msgs: list, cfg):
    """Mask the text of every message (strings and text parts); returns (masked copy, Pseudonymizer | None)."""
    if not cfg: return msgs, None
    import pii
    pz = pii.Pseudonymizer() if cfg["mode"] == "pseudonym" else None
    def mk(s): return pii.mask_text(s, cfg, pz)
    out = []
    for m in msgs:
        c = m.get("content")
        if isinstance(c, str): c = await asyncio.to_thread(mk, c)
        elif isinstance(c, list): c = [({**x, "text": await asyncio.to_thread(mk, x["text"])} if isinstance(x, dict) and x.get("type") == "text" and isinstance(x.get("text"), str) else x) for x in c]
        out.append({**m, "content": c})
    return out, pz

# ================================================================ Prompt Canvas
ACTIONS = ("shorter", "examples", "tone", "clearer", "translate", "safety", "feedback", "custom")
_ACTION_TEXT = {
    "shorter": "Make the text shorter and more concise without losing any instruction, constraint or variable.",
    "examples": "Add one or two short, realistic examples that show the expected behaviour (few-shot style). Keep everything else.",
    "tone": "Change the tone as requested; keep the content and structure.",
    "clearer": "Make it clearer: unambiguous wording, explicit steps and an explicit output format. Keep the meaning.",
    "translate": "Translate the text. Keep variables, code and proper names untouched.",
    "safety": "Add concise safety constraints (refuse harmful requests, never reveal secrets or personal data, do not invent facts) that fit naturally into the text.",
    "feedback": "Rewrite so that the problem described in the FEEDBACK block is fixed. Change as little as needed.",
}
CANVAS_SYSTEM = """[canvas-rewriter]
You are the Prompt Canvas rewriter inside Agent OS. You improve prompts (system or human message templates) that people use with LLMs.
Return ONLY the rewritten text: no preface, no explanation, no markdown fences, no surrounding quotes.
Hard rules:
1. Keep every template variable exactly as written: {{name}} (double braces), {name} (single braces) and {% ... %} blocks. Never rename, remove, translate or invent variables unless the user explicitly asks.
2. Keep the original language unless the action is a translation. Keep the structure and formatting (headings, lists, line breaks) unless asked to change them.
3. Change only what the action asks for; keep meaning, constraints and facts.
4. If the scope is "selection", rewrite ONLY the text between <<<TEXT and TEXT>>>; the full prompt is context and must not be returned."""

class Sel(BaseModel):
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text: str | None = None

class CanvasReq(BaseModel):
    template: str = Field(min_length=1, max_length=50000)
    action: Literal["shorter", "examples", "tone", "clearer", "translate", "safety", "feedback", "custom"] = "custom"
    instruction: str | None = Field(default=None, max_length=2000)
    lang: Literal["vi", "en"] | None = None            # translate target; default: the other one than the text
    selection: Sel | None = None
    history: list[dict] = Field(default=[], max_length=12)   # [{role: user|assistant, content}]
    run_id: str | None = None                           # feedback action: learn from this trace (its input, output, feedback comments)
    feedback: str | None = Field(default=None, max_length=4000)
    model: str | None = Field(default=None, pattern=GEMINI_RX)
    stream: bool = True

def _is_vi(s: str) -> bool: return bool(re.search(r"[ăâđêôơưáàảãạấầẩẫậắằẳẵặéèẻẽẹếềểễệíìỉĩịóòỏõọốồổỗộớờởỡợúùủũụứừửữựýỳỷỹỵ]", s.lower()))

async def _feedback_block(req: CanvasReq) -> tuple[str, str]:
    parts, first = [], ""
    if req.feedback and req.feedback.strip(): parts.append("User feedback: " + req.feedback.strip()); first = req.feedback.strip().splitlines()[0]
    if req.run_id:
        try: run = await A.q("SELECT name, inputs, outputs, error FROM tr_runs WHERE id=%s::uuid ORDER BY start_time DESC LIMIT 1", (req.run_id,), one=True)
        except Exception: run = None  # noqa: BLE001  (a malformed id)
        if not run: raise HTTPException(404, "Trace not found")
        fb = await A.q("SELECT key, score, comment FROM feedback WHERE run_id=%s::uuid ORDER BY created DESC LIMIT 6", (req.run_id,))
        parts.append("Trace input: " + json.dumps(run["inputs"], ensure_ascii=False, default=str)[:1500])
        parts.append("Trace output: " + json.dumps(run["outputs"], ensure_ascii=False, default=str)[:1500])
        if run.get("error"): parts.append("Trace error: " + str(run["error"])[:500])
        for f in fb:
            parts.append(f"Feedback {f['key']}" + (f" (score {f['score']})" if f["score"] is not None else "") + (": " + f["comment"] if f["comment"] else ""))
            if f["comment"] and not first: first = f["comment"].splitlines()[0]
    if not parts: raise HTTPException(400, "Give a trace (run_id) or feedback text to fix the prompt from")
    return "FEEDBACK:\n" + "\n".join(parts), first

def _locate(req: CanvasReq):
    """(start, end) of the part to rewrite; None = the whole prompt. Offsets are Python string indexes (code points)."""
    s = req.selection
    if not s or s.start == s.end: return None
    if s.start > s.end or s.end > len(req.template): raise HTTPException(400, "The selection is outside the prompt")
    if s.text is not None and req.template[s.start:s.end] != s.text: raise HTTPException(409, "The selection no longer matches the prompt; select it again")
    return s.start, s.end

def _clean(out: str) -> str:
    out = out.strip()
    m = re.fullmatch(r"```[\w-]*\n(.*?)\n?```", out, re.S)
    return (m.group(1) if m else out).strip()

async def _canvas_call(req: CanvasReq, target: str, scope: str, retry_missing: list | None, emit):
    instr = (req.instruction or "").replace("\n", " ").strip()
    if req.action == "custom" and not instr: raise HTTPException(400, "Write what to change")
    lang = req.lang or ("en" if _is_vi(target) else "vi")
    head = [f"ACTION: {req.action}", f"LANG: {lang}",
            "INSTRUCTION: " + (_ACTION_TEXT[req.action] + (" " + instr if instr else "") if req.action != "custom" else instr) + (f" Target language: {'English' if lang == 'en' else 'Vietnamese'}." if req.action == "translate" else ""),
            f"SCOPE: {scope}"]
    fb = ""
    if req.action == "feedback":
        fb, first = await _feedback_block(req); head.append("COMMENT: " + first.replace("\n", " "))
    if retry_missing: head += ["RETRY: yes", "MISSING: " + " ".join(retry_missing) + " (your previous answer dropped these variables; every one must appear exactly as written)"]
    hist = "\n".join(f"{'USER' if h.get('role') == 'user' else 'ASSISTANT'}: {str(h.get('content', ''))[:600]}" for h in req.history[-6:])
    body = "\n".join(head) + ("\n\n" + fb if fb else "") + ("\n\nEarlier requests in this session:\n" + hist if hist else "")
    if scope == "selection": body += f"\n\nFULL PROMPT (context only):\n<<<PROMPT\n{req.template}\nPROMPT>>>"
    body += f"\n\nTEXT TO REWRITE:\n<<<TEXT\n{target}\nTEXT>>>"
    msgs = [{"role": "system", "content": CANVAS_SYSTEM}, {"role": "human", "content": body}]
    pcfg = await _pii_open(); msgs, pz = await _mask_msgs(msgs, pcfg)
    from langchain_core.messages import HumanMessage, SystemMessage
    lc = [SystemMessage(msgs[0]["content"]), HumanMessage(msgs[1]["content"])]
    async def tok(tx):
        if emit: await emit({"type": "delta", "text": pz.restore(tx) if pz else tx})
    res = await A.gw.lc_call([req.model or A.DEFAULT_MODEL] + A.gw.DEFAULT_FALLBACKS, lc, temperature=0.4, feature="prompt_canvas", on_token=tok if emit else None)
    out = _text(res["message"]); out = pz.restore(out) if pz else out
    return _clean(out), res

async def canvas_rewrite(req: CanvasReq, emit=None) -> dict:
    loc = _locate(req); scope = "selection" if loc else "full"
    st, en = loc or (0, len(req.template)); target = req.template[st:en]
    lead, trail = target[:len(target) - len(target.lstrip())], target[len(target.rstrip()):]
    t0 = time.time(); new, res = await _canvas_call(req, target, scope, None, emit); cost = res["cost_usd"]; retried = False
    miss = sorted(vars_of(target) - vars_of(new))
    if miss:   # one automatic retry that names the dropped variables
        retried = True
        if emit: await emit({"type": "retry", "missing": miss})
        new, res = await _canvas_call(req, target, scope, miss, emit); cost += res["cost_usd"]
        miss = sorted(vars_of(target) - vars_of(new))
    if not new: raise HTTPException(502, "The model returned no text")
    new = lead + new + trail if scope == "selection" else new + (trail if trail.strip() == "" else "")
    proposed = req.template[:st] + new + req.template[en:]
    segs = segments(req.template, proposed)
    return {"proposed": proposed, "replacement": new, "selection": {"start": st, "end": en} if loc else None, "segments": segs, "stats": diff_stats(segs),
            "missing_variables": miss, "added_variables": sorted(vars_of(new) - vars_of(target)), "variables_ok": not miss, "retried": retried,
            "model": res["model"], "cost_usd": cost, "ms": int((time.time() - t0) * 1000), "action": req.action}

def _err_payload(e: Exception) -> dict:
    if isinstance(e, HTTPException): return {"status": e.status_code, "message": e.detail if isinstance(e.detail, str) else json.dumps(e.detail)}
    if isinstance(e, A.gw.SpendCapReached): return {"status": 402, "message": str(e)}
    return {"status": 502, "message": str(e)[:300] or type(e).__name__}

@router.post("/api/hub/canvas/rewrite")
async def canvas_rewrite_ep(req: CanvasReq):
    if req.action == "custom" and not (req.instruction or "").strip(): raise HTTPException(400, "Write what to change")
    _locate(req)
    try: await A.gw.budget_check(A.gw.ctx_now("gemini", feature="prompt_canvas"))   # unified spend caps (402 before any stream starts)
    except A.gw.BudgetExceeded as e: raise HTTPException(402, str(e)) from None
    if not req.stream:
        try: return await canvas_rewrite(req)
        except HTTPException: raise
        except A.gw.SpendCapReached as e: raise HTTPException(402, str(e)) from None
        except Exception as e: raise HTTPException(502, str(e)[:300] or type(e).__name__) from None
    q: asyncio.Queue = asyncio.Queue()
    async def work():
        try: await q.put({"type": "done", **(await canvas_rewrite(req, q.put))})
        except Exception as e:  # noqa: BLE001
            await q.put({"type": "error", **_err_payload(e)})
        finally: await q.put(None)
    task = asyncio.create_task(work())
    async def gen():
        try:
            yield "data: " + json.dumps({"type": "start", "model": req.model or A.DEFAULT_MODEL, "scope": "selection" if _locate(req) else "full"}, ensure_ascii=False) + "\n\n"
            while (ev := await q.get()) is not None: yield "data: " + json.dumps(ev, ensure_ascii=False, default=str) + "\n\n"
        finally: task.cancel()
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

class DiffReq(BaseModel):
    a: str = Field(max_length=50000)
    b: str = Field(max_length=50000)

@router.post("/api/hub/canvas/diff")
async def canvas_diff(r: DiffReq):
    s = segments(r.a, r.b); return {"segments": s, "stats": diff_stats(s)}

class ApplyReq(DiffReq):
    accept: list[int] | Literal["all"] = "all"

@router.post("/api/hub/canvas/apply")
async def canvas_apply(r: ApplyReq):
    """Merge the accepted hunks of the diff a -> b (the others keep the text of a)."""
    text = apply_hunks(segments(r.a, r.b), r.accept)
    return {"text": text, "missing_variables": sorted(vars_of(r.a) - vars_of(text)), "added_variables": sorted(vars_of(text) - vars_of(r.a))}

class CanvasCommit(BaseModel):
    prompt_id: int
    base_version: int
    template: str = Field(min_length=1, max_length=50000)
    message: str | None = Field(default=None, max_length=500)
    tag: str | None = Field(default=None, pattern=r"^[\w\-.]{1,40}$")
    allow_removed_variables: bool = False

@router.post("/api/hub/canvas/commit")
async def canvas_commit(c: CanvasCommit):
    """Save = a NEW Prompt Hub commit whose parent is the version the canvas was opened on (model, temperature, variables, output schema, format carried over)."""
    import studio
    base = await A.q("SELECT v.*, p.name pname FROM prompt_versions v JOIN prompts p ON p.id=v.prompt_id WHERE v.prompt_id=%s AND v.version=%s", (c.prompt_id, c.base_version), one=True)
    if not base: raise HTTPException(404, "Prompt version not found")
    if c.template == base["template"]: raise HTTPException(400, "No changes to commit")
    miss = sorted(vars_of(base["template"]) - vars_of(c.template))
    if miss and not c.allow_removed_variables: raise HTTPException(422, {"error": "Template variables would be lost", "missing_variables": miss})
    r = await studio.hub_commit(studio.Commit(name=base["pname"], template=c.template, model=base["model"], temperature=base["temperature"], variables=base.get("variables") or {},
                                              output_schema=base.get("output_schema"), message=(c.message or "Canvas edit"), parent_version=c.base_version, tag=c.tag,
                                              template_format=base.get("template_format") or "mustache"))
    return {**r, "parent_version": c.base_version, "tag": c.tag, "missing_variables": miss}

@router.get("/api/hub/canvas/feedback")
async def canvas_feedback_runs():
    """Recent traces that carry a feedback comment: the candidates for the 'fix from a trace' quick action."""
    return await A.q("""SELECT f.run_id, f.key, f.score, f.comment, f.created, r.name FROM feedback f JOIN tr_runs r ON r.id=f.run_id
                        WHERE f.comment IS NOT NULL AND f.comment <> '' ORDER BY f.created DESC LIMIT 15""")

# ================================================================ Playground lab
class Cfg(BaseModel):
    label: str | None = Field(default=None, max_length=60)
    preset: str | None = Field(default=None, max_length=60)
    model: str | None = Field(default=None, pattern=GEMINI_RX)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=1, le=65536)
    top_p: float | None = Field(default=None, ge=0, le=1)
    reasoning_effort: Literal["minimal", "low", "medium", "high"] | None = None

class ToolDef(BaseModel):
    key: str | None = None                 # catalogue key, e.g. aos:list_projects
    name: str | None = Field(default=None, pattern=r"^[A-Za-z_][\w\-]{0,63}$")   # inline JSON-schema tool
    description: str | None = Field(default=None, max_length=1000)
    parameters: dict | None = None

class Structured(BaseModel):
    mode: Literal["schema", "json"] = "schema"      # schema = with_structured_output; json = provider JSON mode + validation
    schema_: dict = Field(alias="schema")
    name: str | None = Field(default=None, pattern=r"^[A-Za-z_][\w\-]{0,63}$")

class LabReq(BaseModel):
    messages: list[dict] = Field(min_length=1, max_length=60)
    variables: dict = {}
    template_format: str = Field(default="mustache", pattern=r"^(mustache|f-string|jinja2)$")
    configs: list[Cfg] = Field(default=[Cfg()], min_length=1, max_length=4)
    tools: list[ToolDef] = Field(default=[], max_length=16)
    tool_choice: str | None = None                  # auto (None) | any | none | <tool name>
    tools_run: Literal["mock", "real"] = "mock"     # real = run READ-ONLY catalogue tools for real (writes are never run here)
    mock_results: dict[str, Any] = {}               # tool name -> result the "tool" returns
    structured: Structured | None = None
    max_steps: int = Field(default=4, ge=1, le=6)
    prompt_ref: str | None = None

async def _catalog_all() -> list:
    import studio
    cat = await studio._catalog()
    try:
        import aos_tools as T
        cat += [{"key": f"aos:{t.name}", "description": (t.description or "")[:200], "read_only": not (t.metadata or {}).get("write")} for t in T.get_tools(write=True, handle_errors=True)]
    except Exception: pass  # noqa: BLE001
    return cat

@router.get("/api/playground/tools")
async def play_tools():
    """Tool catalogue for the Playground: key, the name the model sees, description, read_only (read-only tools may run for real; writes only ever get a mock result)."""
    cat = await _catalog_all(); pool = await _tool_pool()
    return [{**c, "name": getattr(pool.get(c["key"]), "name", None) or c["key"].split(":", 1)[-1]} for c in cat]

async def _tool_pool() -> dict:
    allowed = await A.enabled_tool_keys(); _mr = __import__("mcp_registry"); _mn = _mr.model_names(_mr.cur_ws()); pool = {k: A.guard(t, k.split(":")[0], _mn.get(k)) for k, t in _mr.tools_for(_mr.cur_ws()).items() if allowed.get(k)}
    try: pool.update(A.local_tools(None))
    except Exception: pass  # noqa: BLE001
    try: pool.update(__import__("assistant").tool_pool())
    except Exception: pass  # noqa: BLE001
    try: pool.update(await __import__("workspace_tools").pool(None))
    except Exception: pass  # noqa: BLE001
    return pool

def _check_image(p: dict):
    """Image part -> LangChain v1 standard block {"type":"image","base64","mime_type"}; only inline data (no remote URLs), size and file signature checked."""
    b64, mime = p.get("base64") or p.get("data"), p.get("mime_type")
    if not b64 and isinstance(p.get("image_url"), (dict, str)):
        u = p["image_url"]["url"] if isinstance(p["image_url"], dict) else p["image_url"]
        m = re.fullmatch(r"data:([\w/+.\-]+);base64,(.+)", u or "", re.S)
        if not m: raise HTTPException(400, "Images must be uploaded as data (remote image URLs are not fetched)")
        mime, b64 = m.group(1), m.group(2)
    if not b64: raise HTTPException(400, "Image part without data")
    if mime not in IMAGE_SIG: raise HTTPException(400, "Unsupported image type (use PNG, JPEG, WEBP or GIF)")
    try: raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError): raise HTTPException(400, "Image is not valid base64") from None
    if len(raw) > MAX_IMAGE_BYTES: raise HTTPException(413, f"Image larger than {MAX_IMAGE_BYTES // 1024 // 1024} MB")
    if not raw.startswith(IMAGE_SIG[mime]) or (mime == "image/webp" and raw[8:12] != b"WEBP"): raise HTTPException(400, "The file content does not match its image type")
    return {"type": "image", "base64": b64, "mime_type": mime}, len(raw)

async def _to_lc(messages: list, variables: dict, fmt: str):
    """Message dicts -> LangChain messages (variables rendered in the text; images validated). Returns (messages, n_images)."""
    import ext
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
    out, imgs = [], 0
    for m in messages:
        role = str(m.get("role", "")).lower(); c = m.get("content", "")
        if role == "tool":
            if not m.get("tool_call_id"): raise HTTPException(400, "A tool message needs tool_call_id")
            out.append(ToolMessage(content=c if isinstance(c, str) else json.dumps(c, ensure_ascii=False), tool_call_id=str(m["tool_call_id"]), name=m.get("name"))); continue
        if isinstance(c, str): c = await ext.render_async(c, variables, fmt)
        elif isinstance(c, list):
            parts = []
            for p in c:
                if isinstance(p, str): p = {"type": "text", "text": p}
                if not isinstance(p, dict): raise HTTPException(400, "Bad content part")
                if p.get("type", "text") == "text": parts.append({"type": "text", "text": await ext.render_async(p.get("text", ""), variables, fmt)})
                elif p.get("type") in ("image", "image_url"): blk, _n = _check_image(p); parts.append(blk); imgs += 1
                else: raise HTTPException(400, f"Unsupported content part: {p.get('type')}")
            c = parts
        else: raise HTTPException(400, "Message content must be text or a list of parts")
        if role == "system": out.append(SystemMessage(content=c))
        elif role in ("human", "user"):
            out.append(HumanMessage(content=c))
        elif role in ("ai", "assistant"):
            out.append(AIMessage(content=c, tool_calls=[{"name": t["name"], "args": t.get("args") or {}, "id": str(t.get("id") or "call_" + uuid.uuid4().hex[:10]), "type": "tool_call"} for t in (m.get("tool_calls") or [])]))
        else: raise HTTPException(400, f"Unknown role: {role}")
    if imgs > MAX_IMAGES: raise HTTPException(400, f"At most {MAX_IMAGES} images per run")
    if imgs and any(isinstance(x, SystemMessage) and isinstance(x.content, list) and any(p.get("type") == "image" for p in x.content) for x in out): raise HTTPException(400, "Images belong in a human message")
    return out, imgs

def _summ(messages: list) -> list:
    """Trace-safe copy of the messages (images replaced by a size marker, never stored)."""
    def one(m):
        c = m.get("content")
        if isinstance(c, list): c = [({"type": "image", "note": f"[image {p.get('mime_type') or '?'} {len(p.get('base64') or p.get('data') or '') * 3 // 4} B]"} if isinstance(p, dict) and p.get("type") in ("image", "image_url") else p) for p in c]
        return {**m, "content": c}
    return [one(m) for m in messages]

def _validate(schema: dict, data) -> list:
    from jsonschema import Draft202012Validator
    return [{"path": "/" + "/".join(str(x) for x in e.absolute_path) if e.absolute_path else "(root)", "message": e.message[:240]} for e in sorted(Draft202012Validator(schema).iter_errors(data), key=lambda e: list(map(str, e.absolute_path)))[:20]]

def _tool_check(td: dict, args: dict) -> list:
    return _validate(td, args) if td else []

async def _step_run(pid, trace, parent, name, rtype, t0, inputs, outputs, err=None, model=None, usage=None, tags=None, md=None, rid=None):
    rid = rid or str(uuid.uuid4())
    await obs.upsert_run(pid, {"id": rid, "trace_id": trace, "parent_run_id": parent, "name": name, "run_type": rtype, "start_time": t0, "end_time": time.time(), "inputs": inputs, "outputs": outputs,
                               "error": err, "model": model, "usage": usage or {}, "tags": tags or [], "metadata": md or {}})
    return rid

async def _effective(cfg: Cfg, multi: bool):
    pr = await A.q("SELECT * FROM model_presets WHERE name=%s", (cfg.preset,), one=True) if cfg.preset else None
    if cfg.preset and not pr: raise HTTPException(404, "Model preset not found")
    pr = pr or {}
    g = lambda k: getattr(cfg, k) if getattr(cfg, k) is not None else pr.get(k)
    model = cfg.model or pr.get("model") or A.DEFAULT_MODEL
    chain = [model] if multi else list(dict.fromkeys([model] + list(pr.get("fallback_models") or []) + list(A.gw.DEFAULT_FALLBACKS)))
    return {"model": model, "temperature": g("temperature"), "params": {"max_tokens": g("max_tokens"), "top_p": g("top_p"), "reasoning_effort": g("reasoning_effort")}, "chain": chain,
            "label": cfg.label or cfg.preset or model, "preset": cfg.preset}

def _msg_dict(msg) -> dict:
    return {"role": "ai", "content": _text(msg), "tool_calls": [{"id": t.get("id"), "name": t["name"], "args": t.get("args") or {}} for t in (msg.tool_calls or [])]}

def _stringify(r) -> str:
    s = r if isinstance(r, str) else json.dumps(r, ensure_ascii=False, default=str)
    return s[:6000]

async def _run_cfg(req: LabReq, eff: dict, lc_msgs: list, raw_msgs: list, lc_tools: list, tool_defs: dict, pool: dict, cat: dict, pz, n_images: int, pid: int) -> dict:
    from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
    t_all = time.time(); trace = str(uuid.uuid4()); steps, appended, pending = [], [], []; tin = tout = 0; cost = 0.0
    msgs = list(lc_msgs); st = req.structured; schema = None; out = None; err = None; model_used = eff["model"]
    if st:
        schema = dict(st.schema_); schema.setdefault("title", st.name or "Output")
        if st.mode == "json": msgs = [SystemMessage("Reply ONLY with a JSON value that satisfies this JSON schema (no prose, no fences):\n" + json.dumps(st.schema_, ensure_ascii=False))] + msgs
    tags = ["playground", "lab"] + (["tools"] if lc_tools else []) + (["structured"] if st else []) + (["images"] if n_images else []) + ([f"preset:{eff['preset']}"] if eff["preset"] else []) \
        + ([f"prompt:{req.prompt_ref}"] if req.prompt_ref else [])
    tool_choice = None if req.tool_choice in (None, "", "auto") else req.tool_choice
    try:
        for step in range(req.max_steps):
            t0 = time.time()
            res = await A.gw.lc_call(eff["chain"], msgs, temperature=eff["temperature"], params=eff["params"], tools=lc_tools or None, tool_choice=tool_choice if lc_tools else None,
                                     schema=schema if (st and st.mode == "schema") else None, json_mode=bool(st and st.mode == "json"), feature="playground")
            msg = res["message"]; model_used = res["model"]; tin += res["input_tokens"]; tout += res["output_tokens"]; cost += res["cost_usd"]
            md = _msg_dict(msg)
            if pz:
                md["content"] = pz.restore(md["content"]); md["tool_calls"] = [{**t, "args": json.loads(pz.restore(json.dumps(t["args"], ensure_ascii=False)))} for t in md["tool_calls"]]
            lid = await _step_run(pid, trace, trace, f"llm · {model_used}", "llm", t0, {"messages": _summ(raw_msgs)[-6:]}, {"message": md}, model=model_used,
                                  usage={"input_tokens": res["input_tokens"], "output_tokens": res["output_tokens"]}, tags=tags)
            steps.append({"type": "model", "message": md, "model": model_used, "input_tokens": res["input_tokens"], "output_tokens": res["output_tokens"], "cost_usd": res["cost_usd"], "ms": int((time.time() - t0) * 1000), "run_id": lid})
            if st:
                out = _structured_result(st, schema, res, md["content"], pz); break
            calls = md["tool_calls"]
            if not calls: out = md["content"]; break
            msgs.append(msg); appended.append(md)
            for tc in calls:
                name, args, tid = tc["name"], tc["args"], tc["id"] or "call_" + uuid.uuid4().hex[:8]
                td = tool_defs.get(name); t1 = time.time(); source = result = None; ent = cat.get(name) or {}
                check = _tool_check((td or {}).get("schema"), args)
                if name not in tool_defs: source, result = "error", f"Unknown tool: {name}"
                elif name in req.mock_results or ent.get("key") in req.mock_results: source, result = "mock", _stringify(req.mock_results[name] if name in req.mock_results else req.mock_results[ent["key"]])
                elif req.tools_run == "real" and ent.get("read_only") and ent.get("obj") is not None:
                    try: source, result = "real", _stringify(await asyncio.wait_for(ent["obj"].ainvoke(args), 25))
                    except Exception as e:  # noqa: BLE001
                        source, result = "error", f"Tool error: {str(e)[:300]}"
                if result is None:   # nothing to answer with: wait for the user (writes can only ever be mocked)
                    pending.append({"id": tid, "name": name, "args": args, "reason": "needs_approval" if (ent and not ent.get("read_only")) else "needs_mock"})
                    steps.append({"type": "tool", "id": tid, "name": name, "args": args, "source": "pending", "reason": pending[-1]["reason"], "args_errors": check}); continue
                await _step_run(pid, trace, trace, f"tool · {name}", "tool", t1, {"args": args}, {"result": result}, err=result if source == "error" else None, tags=tags + [f"source:{source}"])
                steps.append({"type": "tool", "id": tid, "name": name, "args": args, "result": result, "source": source, "args_errors": check, "ms": int((time.time() - t1) * 1000)})
                msgs.append(ToolMessage(content=result, tool_call_id=tid, name=name)); appended.append({"role": "tool", "tool_call_id": tid, "name": name, "content": result})
            if pending: break
        else:
            err = f"Stopped after {req.max_steps} steps"
    except HTTPException: raise
    except Exception as e:  # noqa: BLE001
        err = str(e)[:300] or type(e).__name__
        if isinstance(e, A.gw.SpendCapReached): err = str(e)
    final_text = out if isinstance(out, str) else (steps[-1]["message"]["content"] if steps and steps[-1]["type"] == "model" else "")
    rid = await _step_run(pid, trace, None, f"playground lab · {eff['label']}", "chain", t_all, {"messages": _summ(raw_msgs), "variables": req.variables, "config": {"model": eff["model"], "preset": eff["preset"], "temperature": eff["temperature"], **eff["params"]},
                                                                            **({"tools": [t.key or t.name for t in req.tools]} if req.tools else {}), **({"structured": {"mode": st.mode, "schema": st.schema_}} if st else {})},
                          {"answer": final_text, "pending": pending, **({"structured": out} if st else {})}, err=err, model=model_used, usage={"input_tokens": tin, "output_tokens": tout}, tags=tags, md={"trace_root": True}, rid=trace)
    ms = int((time.time() - t_all) * 1000)
    return {"label": eff["label"], "model": model_used, "preset": eff["preset"], "config": {"model": eff["model"], "temperature": eff["temperature"], **eff["params"]}, "output": final_text if not st else (out or {}).get("raw", ""), "structured": out if st else None,
            "steps": steps, "pending": pending, "appended": appended, "error": err, "ms": ms, "input_tokens": tin, "output_tokens": tout, "cost_usd": cost, "run_id": rid, "trace_id": trace}

def _structured_result(st: Structured, schema: dict, res: dict, text: str, pz) -> dict:
    """with_structured_output result (or provider JSON mode text) -> {mode, parsed, raw, valid, errors}."""
    data, errors, raw = None, [], text
    if st.mode == "schema":
        data = res.get("parsed"); pe = res.get("parsing_error")
        if hasattr(data, "model_dump"): data = data.model_dump()
        if data is None: errors = [{"path": "(root)", "message": f"The model did not return structured data{': ' + str(pe)[:200] if pe else ''}"}]
        else: raw = json.dumps(data, ensure_ascii=False)
    else:
        try: data = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()))
        except Exception as e:  # noqa: BLE001
            errors = [{"path": "(root)", "message": f"Output is not valid JSON: {str(e)[:160]}"}]
    if data is not None:
        if pz: data = json.loads(pz.restore(json.dumps(data, ensure_ascii=False))); raw = pz.restore(raw)
        errors = _validate(st.schema_, data)
    return {"mode": st.mode, "parsed": data, "raw": raw, "valid": data is not None and not errors, "errors": errors}

@router.post("/api/playground/lab")
async def play_lab(req: LabReq):
    if req.structured:
        from jsonschema import Draft202012Validator
        from jsonschema.exceptions import SchemaError
        try: Draft202012Validator.check_schema(req.structured.schema_)
        except SchemaError as e: raise HTTPException(400, f"Invalid JSON schema: {e.message[:200]}") from None
        if req.tools: raise HTTPException(400, "Use either tools or structured output in one run (not both)")
    pool = cat = None
    lc_tools, tool_defs, catalog = [], {}, {}
    if req.tools:
        cat = {c["key"]: c for c in await _catalog_all()}; pool = await _tool_pool()
        for t in req.tools:
            if t.key:
                if t.key not in cat: raise HTTPException(404, f"Tool not in the catalogue: {t.key}")
                obj = pool.get(t.key)
                if obj is None: raise HTTPException(400, f"Tool is not available in the Playground: {t.key}")
                lc_tools.append(obj); tool_defs[obj.name] = {"schema": None}; catalog[obj.name] = {**cat[t.key], "obj": obj}
            elif t.name:
                params = t.parameters or {"type": "object", "properties": {}}
                from jsonschema import Draft202012Validator
                from jsonschema.exceptions import SchemaError
                try: Draft202012Validator.check_schema(params)
                except SchemaError as e: raise HTTPException(400, f"Invalid parameters schema for {t.name}: {e.message[:200]}") from None
                lc_tools.append({"type": "function", "function": {"name": t.name, "description": t.description or t.name, "parameters": params}}); tool_defs[t.name] = {"schema": params}
            else: raise HTTPException(400, "A tool needs a catalogue key or a name")
    multi = len(req.configs) > 1
    effs = [await _effective(c, multi) for c in req.configs]
    raw_msgs = req.messages
    pcfg = await _pii_open(); masked, pz = await _mask_msgs(raw_msgs, pcfg)
    lc_msgs, n_img = await _to_lc(masked, req.variables, req.template_format)
    try: await A.gw.budget_check(A.gw.ctx_now("gemini", feature="playground"))
    except A.gw.BudgetExceeded as e: raise HTTPException(402, str(e)) from None
    pid = await obs.project_id("playground")
    results = await asyncio.gather(*(_run_cfg(req, e, lc_msgs, raw_msgs, lc_tools, tool_defs, pool, catalog, pz, n_img, pid) for e in effs))
    return {"results": results, "images": n_img}
