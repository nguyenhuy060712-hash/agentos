"""GĐ7.1 — "Trợ lý Agent OS": the built-in assistant (LangSmith Chat equivalent). Opened with the ✦ button / Ctrl+I on every Agent OS page.

Design (reuse, no parallel stack): it is an ordinary agent built by deepagent_api.build_agent -> create_deep_agent, served by the same
LangGraph-style server runs (server.py), same HITL/approvals, same gateway middleware, same traces. What makes it "built-in":
  * one HIDDEN agent row per workspace (agents.hidden = true; named `agentos-assistant`), created/updated lazily by ensure() from code, so a
    new release changes the prompt/tool list with no migration. Hidden = absent from every agent list, not editable/deletable through the
    agent API, and never copied into a demo workspace (demo_ws copies only the explicit source ids; the demo workspace gets its own row).
  * tools = aos_tools.get_tools(handle_errors=True) registered in the tool pool as `aos:<name>`; write tools (aos_tools.WRITE_TOOLS) are in
    cfg.interrupt_on, so deepagents `interrupt_on` pauses the run until the user approves/edits/rejects (approval card in the chat).
    Demo workspaces get the READ tools only (the tools also refuse demo users; demo users cannot approve anything anyway).
  * skill = the built-in `agentos` skill (aos_tools.skill_files(), config skills=["builtin:agentos"]).
  * every run of it is tagged feature='assistant' (gateway.FEATURE) so GĐ6.1 spend caps with scope=feature, target=assistant apply (402 -> friendly
    message). NO default cap is inserted automatically. Recommended: POST /api/gateway/caps {scope:"feature", target:"assistant", period:"day",
    limit_usd:1, action:"block"} (admin; per workspace or global).
  * page context: the run input may carry `context: {page, url, project_id, trace_id, thread_id, dataset_id, experiment_id}`; server._create_run
    stores a sanitised copy in the run metadata and PageContextMiddleware appends a short "Bạn đang xem: ..." block to the LAST user turn of each
    model request (the stored history stays clean, so the chat shows only what the user typed). The context is untrusted data (ids/paths);
    tools still enforce the user's own access via RLS.
  * threads: server forces metadata.source='assistant' on a thread bound to this assistant, so the chat sidebar / thread search (source='chat')
    never list them; the panel lists them with threads/search {metadata:{source:'assistant'}, owner_id: me}. lg_threads RLS keeps them private."""
from __future__ import annotations
import asyncio, contextvars, json, os, re, uuid
from fastapi import APIRouter, HTTPException
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from psycopg.types.json import Jsonb
import admin as _adm
import deepagent_api as A

NAME = "agentos-assistant"
FEATURE = "assistant"
MODEL = os.environ.get("AOS_ASSISTANT_MODEL", "gemini-3.8-flash")
CTX_KEYS = ("page", "url", "project_id", "trace_id", "thread_id", "dataset_id", "experiment_id", "report_id")
_LOCK = asyncio.Lock()
PAGE: contextvars.ContextVar = contextvars.ContextVar("aos_assistant_page", default=None)   # set per run by server._execute_inner

PROMPT = """You are the Agent OS assistant, built into the Agent OS web app (tracing, datasets, experiments, prompts, costs).
Answer in the language of the user's last message (Vietnamese or English). Be concise: lead with the answer, then 2-5 short bullets at most.
- Use your tools to look things up. NEVER invent trace ids, run ids, prompt text, costs or scores: if a tool returns nothing or an error, say so.
- Always cite what you used: trace ids, experiment ids, prompt names/commits (quote ids exactly). Link a trace as `projects?trace=<trace_id>`.
- "Bạn đang xem: ..." at the end of a user message tells you which page/trace/project the user has open; use it for "this trace", "trace này", "dự án này".
- Read tools are safe any time. Write tools (propose_prompt_edit, create_dataset_from_runs, draft_evaluator, run_experiment) ask the user to approve first:
  say in one sentence what you are about to change and why, then call the tool. Never move a production tag.
- You can only see what the signed-in user can see. Read the `agentos` skill for the tool guide and the debugging workflow.
- Do not paste long prompts or whole traces; summarise and point to the id."""

def desired(demo: bool = False):
    import aos_tools as T
    tools = T.get_tools(write=not demo, handle_errors=True)
    return A.AgentCfg(name=NAME, description="Built-in Agent OS assistant (read traces, explain errors, propose changes with approval).",
                      system_prompt=PROMPT, model=MODEL, temperature=0.2, planning=False, filesystem=False,
                      tools=[f"aos:{t.name}" for t in tools], interrupt_on=[f"aos:{t.name}" for t in tools if t.name in T.WRITE_TOOLS],
                      skills=["builtin:agentos"], rpm_limit=20)

def tool_pool() -> dict:
    import aos_tools as T
    return {f"aos:{t.name}": t for t in T.get_tools(write=True, handle_errors=True)}

def is_builtin(assistant_row) -> bool:
    return ((assistant_row or {}).get("metadata") or {}).get("builtin") == NAME

def clean_context(c) -> dict | None:
    """Keep only the known keys, as short strings (the context comes from the browser: treat as untrusted text)."""
    if not isinstance(c, dict): return None
    out = {k: re.sub(r"[\r\n\t]+", " ", str(c[k])).strip()[:300] for k in CTX_KEYS if c.get(k) not in (None, "")}
    return out or None

def context_block(c) -> str:
    c = clean_context(c)
    if not c: return ""
    head = f"trang {c['page']}" if c.get("page") else "Agent OS"
    rest = "; ".join(f"{k}={v}" for k, v in c.items() if k != "page")
    return f"Bạn đang xem: {head}" + (f" ({rest})" if rest else "")

class PageContextMiddleware(AgentMiddleware):
    """Appends the 'Bạn đang xem: ...' block to the last user turn of every model request (history in the state is not modified)."""
    def _mod(self, request):
        blk = context_block(PAGE.get())
        if not blk: return request
        msgs = list(request.messages)
        i = next((j for j in range(len(msgs) - 1, -1, -1) if isinstance(msgs[j], HumanMessage)), None)
        if i is None: return request
        m = msgs[i]; c = m.content
        msgs[i] = m.model_copy(update={"content": (c + "\n\n" + blk) if isinstance(c, str) else [*c, {"type": "text", "text": blk}]})
        return request.override(messages=msgs)
    def wrap_model_call(self, request, handler): return handler(self._mod(request))
    async def awrap_model_call(self, request, handler): return await handler(self._mod(request))

# ------------------------------------------------------------------ per-workspace hidden agent
async def init():
    await A.q("ALTER TABLE agents ADD COLUMN IF NOT EXISTS hidden BOOLEAN NOT NULL DEFAULT false")
    # GĐ8.P: api + worker can both call ensure() at once -> one hidden assistant per workspace enforced by the DB (older duplicates soft-deleted first, lowest id kept)
    await A.q("UPDATE agents a SET deleted=true WHERE a.hidden AND NOT a.deleted AND a.name=%s AND a.id > (SELECT min(b.id) FROM agents b WHERE b.hidden AND NOT b.deleted AND b.name=%s AND b.workspace_id=a.workspace_id)", (NAME, NAME))
    await A.q(f"CREATE UNIQUE INDEX IF NOT EXISTS agents_one_assistant ON agents(workspace_id) WHERE hidden AND NOT deleted AND name='{NAME}'")

async def ensure() -> dict:
    """Create/refresh the hidden assistant agent + its system assistant row for the CURRENT workspace. Idempotent."""
    ws = _adm.cur_ws()
    wr = await A.q("SELECT demo FROM workspaces WHERE id=%s", (ws,), one=True)
    cfg = desired(bool(wr and wr["demo"]))
    want = json.loads(json.dumps(cfg.model_dump(exclude={"note"})))
    async with _LOCK, A.S["pool"].connection() as lc, lc.transaction():   # GĐ8.P: asyncio lock (this process) + advisory lock (api and worker processes)
        await lc.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"aos-assistant-ws{ws}",))
        sel = ("SELECT a.id, a.current_version, v.config FROM agents a LEFT JOIN agent_versions v ON v.agent_id=a.id AND v.version=a.current_version "
               "WHERE a.hidden AND NOT a.deleted AND a.name=%s AND a.workspace_id=%s ORDER BY a.id LIMIT 1")
        ag = await A.q(sel, (NAME, ws), one=True)
        ins = None if ag else await A.q(f"INSERT INTO agents(name, workspace_id, hidden) VALUES(%s,%s,true) ON CONFLICT (workspace_id) WHERE hidden AND NOT deleted AND name='{NAME}' DO NOTHING RETURNING id",
                                         (NAME, ws), one=True)   # the unique index is the last guard if a process without the lock raced us
        if not ag and not ins: ag = await A.q(sel, (NAME, ws), one=True)
        if ins:
            aid = ins["id"]
            await A.save_version(aid, cfg, "built-in")
        else:
            aid = ag["id"]
            if json.loads(json.dumps(ag["config"] or {})) != want: await A.save_version(aid, cfg, "built-in update")
    asid = uuid.uuid5(uuid.NAMESPACE_URL, f"agentos-assistant-ws{ws}")
    md = {"created_by": "system", "hidden": True, "builtin": NAME, "chat_visible": False}
    row = await A.q("INSERT INTO lg_assistants(assistant_id,graph_id,agent_id,name,description,metadata,workspace_id) VALUES(%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (assistant_id) DO UPDATE SET graph_id=EXCLUDED.graph_id, agent_id=EXCLUDED.agent_id, metadata=EXCLUDED.metadata RETURNING *",
                    (asid, f"agent-{aid}", aid, NAME, cfg.description, Jsonb(md), ws), one=True)
    return row

def router():
    r = APIRouter()

    @r.get("/api/assistant")
    async def info():
        """What the ✦ panel needs: the assistant's graph id (use it as assistant_id for runs), model, and whether write actions exist for this workspace."""
        u = _adm.cur()
        if not u: raise HTTPException(401, "login required")
        row = await ensure()
        cfg = (await A.q("SELECT config FROM agent_versions v JOIN agents a ON a.id=v.agent_id AND a.current_version=v.version WHERE a.id=%s", (row["agent_id"],), one=True))["config"]
        return {"assistant_id": str(row["assistant_id"]), "graph_id": row["graph_id"], "agent_id": row["agent_id"], "name": NAME, "model": cfg["model"],
                "feature": FEATURE, "can_write": bool(cfg.get("interrupt_on")) and not u.get("demo") and _adm.ROLES.get(u.get("role"), 0) >= _adm.ROLES["editor"]}

    return r
