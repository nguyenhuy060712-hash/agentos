"""GĐ7.7 — "Nghiên cứu sâu" / Deep research mode of the Chat.

Design (reuse, no parallel stack): the research agent is an ordinary deepagent built by deepagent_api.build_agent -> create_deep_agent (same traces, gateway
middleware, HITL, feature caps). Like the built-in assistant (assistant.py) it is ONE HIDDEN agent row per workspace (agents.hidden, name `agentos-research`),
created/updated lazily by ensure() from code. A chat run with config.configurable.mode == "deep_research" makes server._execute_inner run THAT agent's graph on the
SAME thread (checkpoint key stays a<thread agent>:<thread id>, run_agent(thread_agent=...)), tagged feature='deep_research' (gateway.FEATURE) so workspace feature caps
(scope=feature, target=deep_research) apply. The thread metadata `deep_research_agent` makes server._graph read the thread state (interrupts, todos) with this graph.

Flow: planner/orchestrator -> propose_plan(plan) [in interrupt_on: the chat shows the approve/edit/reject card] -> ONLY after approval `task(researcher)` (guard) ->
researcher subagents (web_search + fetch_url + think_tool) -> orchestrator synthesises -> report in Canvas (TOC, inline [n] citations, Sources) -> short summary.

Limits (env, read at run time unless noted): AOS_DEEP_RESEARCH_MAX_PARALLEL=3, _MAX_ROUNDS=3, _MAX_SEARCHES=15 (per run, all researchers), _TIMEOUT_S=600,
_MAX_USD=0.30 (per run). Enforced with LangChain ToolCallLimitMiddleware (task; web_search/fetch_url per researcher; fixed at agent build), ModelCallLimitMiddleware
and ResearchGuard (plan guard, parallel/round limits, global search budget, wall clock, per-run spend from the gateway usage sink; stops the run gracefully with a message).

Prompts adapted from langchain-ai/deepagents `examples/deep_research/research_agent/prompts.py` (MIT licence)."""
from __future__ import annotations
import contextvars, json, os, re, time
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool as lc_tool
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain.agents.middleware.types import hook_config
import gateway as gw

NAME = "agentos-research"
FEATURE = "deep_research"
MODE = "deep_research"
META = "deep_research_agent"          # lg_threads.metadata key: id of the hidden research agent that owns the latest run of the thread
MODEL = os.environ.get("AOS_DEEP_RESEARCH_MODEL", os.environ.get("AOS_ASSISTANT_MODEL", "gemini-3.8-flash"))

def _num(name, default, cast=float):
    try: return cast(os.environ.get(name, default))
    except ValueError: return cast(default)

def limits() -> dict:
    return {"par": _num("AOS_DEEP_RESEARCH_MAX_PARALLEL", 3, int), "rounds": _num("AOS_DEEP_RESEARCH_MAX_ROUNDS", 3, int),
            "searches": _num("AOS_DEEP_RESEARCH_MAX_SEARCHES", 15, int), "timeout": _num("AOS_DEEP_RESEARCH_TIMEOUT_S", 600, float), "soft": _num("AOS_DEEP_RESEARCH_SOFT", 0.7, float),
            "usd": _num("AOS_DEEP_RESEARCH_MAX_USD", 0.30, float)}

# ------------------------------------------------------------------ per-run state (set by server._execute_inner)
class RunState:
    def __init__(self, lang=None): self.t0 = time.monotonic(); self.spent = 0.0; self.searches = 0; self.stopped = None; self.lang = lang
RUN: contextvars.ContextVar = contextvars.ContextVar("aos_deep_research_run", default=None)

def begin(active: bool, lang: str | None = None):
    """Called at the start of every run (always: a reused worker task must not keep the previous run's counters).
    lang = the run user's UI language (users.lang, the chat's language switch); None (cron / channel / no user) -> guessed from the question."""
    RUN.set(RunState(lang if lang in ("vi", "en") else None) if active else None)

async def user_lang(uid) -> str | None:
    """GĐ8.P: UI language of the user who started the run (same source as the chat: /api/auth/me -> users.lang)."""
    if not uid: return None
    import deepagent_api as A
    r = await A.q("SELECT lang FROM users WHERE id=%s", (uid,), one=True)
    return (r or {}).get("lang")

def run_mode(rcfg) -> str | None:
    """Allow-list for the per-run mode switch: only the exact value "deep_research" (flat or under `configurable`); anything else = normal run."""
    c = rcfg if isinstance(rcfg, dict) else {}
    m = (c.get("configurable") or {}).get("mode", c.get("mode"))
    return MODE if m == MODE else None

async def resuming(tid, run) -> str | None:
    """A HITL resume (command) on a thread whose latest run was a research run continues in research mode even if the client forgot to resend the mode."""
    if not (run or {}).get("command"): return None
    import deepagent_api as A
    r = await A.q("SELECT metadata ->> %s::text m FROM lg_threads WHERE thread_id=%s", (META, tid), one=True)
    return MODE if r and r["m"] else None

async def _sink(agent_id, model, i, o, c, status, ms, err, details=None):
    r = RUN.get()
    if r is not None and c: r.spent += float(c)
gw.USAGE_SINK.append(_sink)   # cost of every model call of this run (orchestrator + researchers), as billed by the gateway

# ------------------------------------------------------------------ prompts (adapted from deepagents examples/deep_research, MIT)
ORCHESTRATOR_PROMPT = """You are the Deep Research orchestrator of Agent OS. Answer in the language of the user's last message (Vietnamese or English).
Handle every request in these 5 steps, in order.

1. **Plan**: think (think_tool) about what must be found out, then call `propose_plan` ONCE with a short markdown plan: the goal, 3-6 research questions/aspects,
   how many researchers you will use, and the shape of the final report. The user must approve it first (they may edit or reject it).
   - If the plan is REJECTED: do not research. Say in 1-2 sentences what you need to change and ask the user (no tools).
   - NEVER call `task` before `propose_plan` has been approved (it will be refused).
2. **Track**: after approval, record the steps with write_todos (research tasks, then "write report").
3. **Research**: delegate with the `task` tool to the `researcher` subagent. ALWAYS delegate; never search the web yourself.
4. **Synthesize**: read all findings, consolidate citations (each unique URL gets ONE number across all findings, numbered 1,2,3... without gaps).
5. **Report**: write the final report with the `canvas` tool (tieu_de = report title, noi_dung = full markdown, loai = document), then reply with a 2-3 sentence summary
   of the main conclusion. Do NOT paste the report again in your reply.

# Delegation strategy
- DEFAULT: 1 researcher for most requests ("what is X", "summarize Y", "list the top N ...").
- Parallelize ONLY for explicit comparisons (1 researcher per element) or clearly separated aspects (e.g. regions). Make several `task` calls in ONE response to run them in parallel.
- Do not split "research X" into "X overview / X techniques / X applications": one researcher covers all of X.
- At most {max_parallel} parallel researchers per round and at most {max_rounds} delegation rounds. Stop earlier when you have enough sources.
- In each task description give ONE specific question/aspect, the expected output (findings + sources with URLs) and, if useful, what to ignore.

# Report format (canvas)
- Line 1: `# <Title>`. Then a table of contents: a heading `## Mục lục` (Vietnamese) or `## Table of contents` (English) followed by a bullet list of the section titles.
- Sections with `##`, subsections with `###`. Write mostly prose (be text-heavy, not only bullets). No self-reference ("I found...", "I researched...").
- Comparisons: introduction, element A, element B, detailed comparison, conclusion. Lists/rankings: just the items with details. Overviews: overview, key concepts, conclusion.
- Cite inline with [1], [2], [3]. Every claim from the research carries a citation; do not cite sources you were not given.
- End with `### Nguồn` (Vietnamese) or `### Sources` (English): one line per source `[n] Title: URL`, sequential numbers, each unique URL once.
- If sources conflict or are weak, say so plainly in the report instead of smoothing it over.

# Limits
The run has a time limit and a cost cap; if a tool says a limit was reached, stop researching and write the best report you can from what you already have."""

RESEARCHER_PROMPT = """You are a Deep Research researcher (subagent of Agent OS). Research ONLY the question you were given. Write in the language of the question.

<Tools>
1. `web_search(query)`: search the public web (title, URL, snippet).
2. `fetch_url(url)`: read one page when a snippet is not enough.
3. `think_tool(reflection)`: reflect after EVERY search: what did I find, what is missing, do I have enough?
</Tools>

<Instructions>
Think like a human researcher with limited time.
1. Read the question carefully. 2. Start with broad queries, then narrower ones to fill the gaps. 3. After each search call think_tool. 4. Stop when you can answer confidently.
</Instructions>

<Hard limits>
- Simple question: 2-3 searches. Complex question: at most 5 searches. Stop after 5 searches even if the answer is imperfect.
- Stop immediately when you have 3+ relevant sources, or when your last 2 searches returned similar information.
- If a tool says a limit was reached, stop searching and answer with what you have.
</Hard limits>

<Final response format>
Return findings to the orchestrator: clear headings, detailed explanations, inline [1], [2], [3] citations (number = unique URL), and end with `### Sources`
listing `[n] Title: URL` for each source you actually used. Never invent a URL or a fact that is not in the search results.
</Final response format>"""

def orchestrator_prompt() -> str:
    L = limits()
    return ORCHESTRATOR_PROMPT.replace("{max_parallel}", str(L["par"])).replace("{max_rounds}", str(L["rounds"]))

# ------------------------------------------------------------------ tools (registered in the tool pool as local:<name>)
def tools() -> dict:
    @lc_tool
    def propose_plan(plan: str) -> str:
        """Show the research plan (markdown) to the user for approval BEFORE any research. The run pauses until the user approves, edits or rejects it.
        Call it once, first. plan: goal, research questions/aspects, number of researchers, shape of the final report."""
        return "Plan approved by the user:\n" + plan[:6000]

    @lc_tool
    def think_tool(reflection: str) -> str:
        """Strategic reflection on research progress and decisions. Use after each search (researcher) and before planning / writing (orchestrator):
        what did I find, what is missing, is the evidence enough, what next?"""
        return f"Reflection recorded: {reflection[:2000]}"
    return {"local:propose_plan": propose_plan, "local:think_tool": think_tool}

def desired():
    import deepagent_api as A
    return A.AgentCfg(name=NAME, description="Built-in Deep research agent (plan approval, parallel researchers, report in Canvas).",
                      system_prompt=orchestrator_prompt(), model=MODEL, temperature=0.2, planning=True, filesystem=False,
                      tools=["local:propose_plan", "local:think_tool", "local:canvas"], interrupt_on=["local:propose_plan"], rpm_limit=30,
                      subagents=[A.SubAgentCfg(name="researcher", system_prompt=RESEARCHER_PROMPT, tools=["local:web_search", "local:fetch_url", "local:think_tool"],
                                               description="Researches ONE specific question on the public web (search + reflection) and returns findings with [n] citations and a Sources list. "
                                                           "Give it one focused question per task; run several in parallel only for comparisons.")])

# ------------------------------------------------------------------ middleware
def plan_approved(messages) -> bool:
    """True when, in the current user turn, propose_plan returned normally (HITL approve/edit run the tool; reject makes an error ToolMessage)."""
    last_h = max([i for i, m in enumerate(messages) if isinstance(m, HumanMessage)] or [-1])
    return any(isinstance(m, ToolMessage) and m.name == "propose_plan" and getattr(m, "status", "success") != "error" for m in messages[last_h + 1:])

def _deny(tc, msg): return ToolMessage(content=msg, tool_call_id=tc["id"], name=tc.get("name"), status="error")

_VI = re.compile(r"[ăâđêôơưàáảãạèéẻẽẹìíỉĩịòóỏõọùúủũụỳýỷỹỵ]")
def _is_vi(state, lang=None) -> bool:
    """Language of the stop message = the user's UI language when known (GĐ8.P), else the latest user question's (Vietnamese diacritics -> vi).
    No question in state -> vi (the product default)."""
    if lang in ("vi", "en"): return lang == "vi"
    hs = [m for m in (state or {}).get("messages", []) if isinstance(m, HumanMessage)]
    if not hs: return True
    t = hs[-1].content if isinstance(hs[-1].content, str) else json.dumps(hs[-1].content, ensure_ascii=False)
    return bool(_VI.search(t.lower()))

class ResearchGuard(AgentMiddleware):
    """Per-run guards (reads RUN): wall clock + spend cap stop the run gracefully BEFORE the next model call (jump to end with a message);
    plan guard / parallel / rounds for `task`; global web-search budget for `web_search`."""
    def __init__(self, sub: bool = False):
        super().__init__(); self.sub = sub

    @hook_config(can_jump_to=["end"])
    async def abefore_model(self, state, runtime):
        r = RUN.get()
        if r is None: return None
        L = limits(); why = None; vi = _is_vi(state, r.lang)   # one language: the user's UI language (GĐ8.P), else the question's (GĐ7.Z)
        if time.monotonic() - r.t0 > L["timeout"]: why = f"đạt giới hạn thời gian {L['timeout']:.0f} giây" if vi else f"the {L['timeout']:.0f}s time limit was reached"
        elif r.spent >= L["usd"]: why = f"đạt trần chi phí ${L['usd']:.2f} cho mỗi lần nghiên cứu" if vi else f"the per-run cost cap of ${L['usd']:.2f} was reached"
        if not why: return None
        r.stopped = why
        txt = (f"Đã dừng nghiên cứu: {why}. Kết quả thu thập được đến lúc này (nếu có) nằm ở trên; anh có thể chạy lại với phạm vi hẹp hơn." if vi
               else f"Research stopped: {why}. Whatever was gathered so far is above; try again with a narrower scope.")
        return {"messages": [AIMessage(content=txt)], "jump_to": "end"}

    async def awrap_tool_call(self, request, handler):
        tc = request.tool_call; name = tc.get("name"); L = limits()
        r = RUN.get()
        if r is not None and name in ("web_search", "fetch_url", "task") and time.monotonic() - r.t0 > L["timeout"] * L["soft"]:
            # GĐ7.R: a slow model (Gemini 504 after 120 s, several times) ate the whole 600 s and the run stopped with NO report -> stop researching early
            return _deny(tc, "Time is almost up for this research run. Do not search, fetch or delegate again: write the final report now from what you already have.")
        if name == "web_search":
            r = RUN.get()
            if r is not None:
                if r.searches >= L["searches"]:
                    return _deny(tc, f"Search budget exhausted ({L['searches']} web searches per research run). Do not search again: write your findings from what you already have.")
                r.searches += 1
        elif name == "task" and not self.sub:
            msgs = list((request.state or {}).get("messages", []))
            if not plan_approved(msgs):
                return _deny(tc, "Refused: there is no approved plan yet. Call propose_plan first and wait for the user's approval; do not delegate before that.")
            last_h = max([i for i, m in enumerate(msgs) if isinstance(m, HumanMessage)] or [-1]); rnd = 0
            for m in msgs[last_h + 1:]:
                if isinstance(m, AIMessage) and any(c.get("name") == "task" for c in m.tool_calls or []):
                    rnd += 1; ids = [c.get("id") for c in m.tool_calls if c.get("name") == "task"]
                    if tc["id"] in ids:
                        if rnd > L["rounds"]: return _deny(tc, f"Refused: delegation round limit ({L['rounds']}) reached. Write the report from the findings you already have.")
                        if ids.index(tc["id"]) >= L["par"]: return _deny(tc, f"Refused: at most {L['par']} researchers in parallel. Delegate this one in the next round (if rounds remain).")
                        break
        return await handler(request)

def main_middleware() -> list:
    L = limits()
    return [ResearchGuard(False), ToolCallLimitMiddleware(tool_name="task", run_limit=max(1, L["par"] * L["rounds"])), ModelCallLimitMiddleware(run_limit=40, exit_behavior="end")]

def sub_middleware() -> list:
    return [ResearchGuard(True), ToolCallLimitMiddleware(tool_name="web_search", run_limit=5), ToolCallLimitMiddleware(tool_name="fetch_url", run_limit=5),
            ModelCallLimitMiddleware(run_limit=16, exit_behavior="end")]

# ------------------------------------------------------------------ hidden per-workspace agent
async def ensure() -> dict:
    """Create/refresh the hidden research agent of the CURRENT workspace (same pattern as assistant.ensure). Idempotent; returns {"id", "version"}."""
    import deepagent_api as A, admin as _adm, assistant as _as
    ws = _adm.cur_ws(); cfg = desired()
    want = json.loads(json.dumps(cfg.model_dump(exclude={"note"})))
    async with _as._LOCK:
        ag = await A.q("SELECT a.id, a.current_version, v.config FROM agents a LEFT JOIN agent_versions v ON v.agent_id=a.id AND v.version=a.current_version "
                       "WHERE a.hidden AND NOT a.deleted AND a.name=%s AND a.workspace_id=%s ORDER BY a.id LIMIT 1", (NAME, ws), one=True)
        if not ag:
            aid = (await A.q("INSERT INTO agents(name, workspace_id, hidden) VALUES(%s,%s,true) RETURNING id", (NAME, ws), one=True))["id"]
            v = await A.save_version(aid, cfg, "built-in")
        else:
            aid, v = ag["id"], ag["current_version"]
            if json.loads(json.dumps(ag["config"] or {})) != want: v = await A.save_version(aid, cfg, "built-in update")
    return {"id": aid, "version": v}

async def graph_for(t):
    """Graph used to read the state of thread row `t` when its latest run was a research run (None = use the thread agent's own graph)."""
    r = ((t or {}).get("metadata") or {}).get(META)
    if not isinstance(r, int): return None
    import deepagent_api as A
    if not await A.q("SELECT 1 FROM agents WHERE id=%s AND hidden AND name=%s AND NOT deleted", (r, NAME), one=True): return None   # never trust client-edited metadata
    ag, _ = await A.build_agent(r, await A.cur_version(r)); return ag

# ------------------------------------------------------------------ PDF export of a canvas document (markdown-it-py, MIT + weasyprint, BSD)
_CSS = """@page { size: A4; margin: 18mm 16mm; @bottom-center { content: counter(page) " / " counter(pages); font-size: 9pt; color: #777; } }
body { font-family: 'DejaVu Sans', 'Noto Sans', sans-serif; font-size: 10.5pt; line-height: 1.55; color: #1b1b1b; }
h1 { font-size: 20pt; margin: 0 0 8mm; } h2 { font-size: 15pt; margin: 8mm 0 3mm; border-bottom: 1px solid #ddd; padding-bottom: 1mm; } h3 { font-size: 12pt; margin: 5mm 0 2mm; }
p { margin: 0 0 3mm; } a { color: #0b57d0; text-decoration: none; word-break: break-all; } ul, ol { margin: 0 0 3mm 5mm; padding-left: 4mm; }
code { font-family: 'DejaVu Sans Mono', monospace; font-size: 9pt; background: #f2f2f2; padding: 0 1mm; } pre { background: #f6f6f6; padding: 3mm; white-space: pre-wrap; word-break: break-all; }
pre code { background: none; padding: 0; } table { border-collapse: collapse; margin: 0 0 4mm; width: 100%; } th, td { border: 1px solid #ccc; padding: 1.5mm 2.5mm; text-align: left; vertical-align: top; }
th { background: #f0f0f0; } blockquote { margin: 0 0 3mm; padding-left: 4mm; border-left: 3px solid #ccc; color: #444; }"""

def md_to_pdf(title: str, md: str, **opts) -> bytes:
    """Markdown -> HTML (raw HTML in the document is escaped, not rendered) -> PDF. Nothing external is fetched (images/links stay text). Blocking: run in a thread."""
    import html as _h
    from markdown_it import MarkdownIt
    from weasyprint import HTML
    from weasyprint.urls import URLFetcher
    body = MarkdownIt("commonmark", {"html": False, "linkify": False, "breaks": True}).enable("table").render(md or "")
    doc = f'<!doctype html><html lang="vi"><head><meta charset="utf-8"><title>{_h.escape(title or "Report")}</title></head><body>{body}</body></html>'
    from weasyprint import CSS
    fetcher = URLFetcher(allowed_protocols=("data",))   # only data: URIs: no network, no local files (SSRF/LFI)
    return HTML(string=doc, url_fetcher=fetcher).write_pdf(stylesheets=[CSS(string=_CSS)], **opts)

def pdf_filename(title: str) -> tuple[str, str]:
    """(ascii fallback, utf-8 name) for Content-Disposition."""
    import re, unicodedata
    t = (title or "report").strip().replace("đ", "d").replace("Đ", "D")
    a = re.sub(r"[^A-Za-z0-9._-]+", "_", unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()).strip("._") or "report"
    u = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", t).strip()[:80] or "report"
    return a[:60] + ".pdf", u + ".pdf"
