"""GĐ7.0 — ONE Agent OS tool set, three entry points: (1) the in-app assistant (7.1, `get_tools()` -> create_deep_agent(tools=...)),
(2) an MCP server at /mcp/aos (coding agents such as Claude Code), (3) the `agentos` skill (skills/agentos/SKILL.md).

Tools are plain LangChain `@tool` coroutines that call our own internal handlers (obs/evals/studio) in-process. They run AS the current user:
every query goes through `A.q`, which reads `admin.USER` (set by the HTTP middleware, or by server._execute for agent runs) and sets the
Postgres RLS context (app.ws / app.uid / app.role). Nothing here uses a system identity, so RLS (workspace + GĐ6.10 per-user traces) applies.
Write tools additionally require: not a demo user, role >= editor, key scope != read. Outputs are trimmed to a character budget
(idea from langsmith-mcp-server: paginate by chars, not by item count) so a tool result never floods the model context.
MCP: official `mcp` 2.x `MCPServer` via mcp_bridge.LCServer (GĐ9.6: serves spec 2026-07-28 and the older initialize-handshake clients)."""
import contextlib, json, os, pathlib, re, uuid
from fastapi import HTTPException
from langchain_core.tools import tool, ToolException
from psycopg.types.json import Jsonb
import deepagent_api as A   # q(), S (import vòng: chỉ dùng thuộc tính lúc chạy)

CHARS = int(os.environ.get("AOS_TOOL_CHARS", "6000"))     # default budget per tool result
WRITE_TOOLS = frozenset({"propose_prompt_edit", "create_dataset_from_runs", "draft_evaluator", "run_experiment"})   # 7.1: interrupt_on = these (user approves)
SKILL_FILE = pathlib.Path(__file__).parent / "skills" / "agentos" / "SKILL.md"

# ------------------------------------------------------------------ helpers
def _adm():
    import admin; return admin

def _user():
    u = _adm().USER.get()
    if not u: raise ToolException("Not authenticated")
    return u

def _need_write():
    u = _user()
    if u.get("demo") or u.get("scope") == "read" or _adm().ROLES.get(u.get("role"), 0) < _adm().ROLES["editor"]:
        raise ToolException("Write tools need an editor/admin session or a full-scope key (demo users and read-only keys cannot use them)")
    return u

@contextlib.contextmanager
def as_user(ident: dict):
    """Run tools as `ident` ({"id","username","role","ws"} like admin.authenticate) outside an HTTP request (tests, schedulers)."""
    a = _adm(); tok = a.USER.set(ident)
    try: yield
    finally: a.USER.reset(tok)

def _cut(v, n):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + f"...[+{len(s) - n} chars]"

def _dump(o) -> str: return json.dumps(o, ensure_ascii=False, default=str)

def _page(rows, shape, offset=0, budget=CHARS, base=0, more=False, **extra) -> str:
    """Return as many shaped rows as fit in `budget` chars (at least one) + total/next_offset so the caller can ask for the next page.
    `base`/`more`: rows were already fetched from SQL at OFFSET base and the query may have further rows."""
    items, used = [], 0
    for r in rows[offset:]:
        s = shape(r); n = len(_dump(s))
        if items and used + n > budget: break
        items.append(s); used += n
    nxt = offset + len(items)
    return _dump({**extra, "total": len(rows), "offset": base + offset, "returned": len(items), "next_offset": base + nxt if (nxt < len(rows) or more) else None, "items": items})

def _uuid(v, what="id"):
    try: return str(uuid.UUID(str(v)))
    except ValueError: raise ToolException(f"{what} must be a UUID") from None

async def _call(coro):
    try: return await coro
    except HTTPException as e: raise ToolException(f"{e.status_code}: {e.detail}") from None

async def _project_id(project) -> int:
    ws = _adm().cur_ws()
    r = await A.q("SELECT id FROM projects WHERE workspace_id=%s AND (id::text=%s OR name=%s) ORDER BY id LIMIT 1", (ws, str(project), str(project)), one=True)
    if not r: raise ToolException(f"Project not found: {project}")
    return r["id"]

async def _dataset(ref):
    r = await A.q("SELECT id, name FROM datasets WHERE workspace_id=%s AND (id::text=%s OR name=%s) ORDER BY id LIMIT 1", (_adm().cur_ws(), str(ref), str(ref)), one=True)
    if not r: raise ToolException(f"Dataset not found: {ref}")
    return r

def _run_row(r):
    return {"id": r["id"], "trace_id": r["trace_id"], "name": r["name"], "type": r["run_type"], "time": r["start_time"], "ms": r["latency_ms"], "status": r["status"],
            "error": r["error"], "in": _cut(r["inputs"], 150), "out": _cut(r["outputs"], 150), "model": r["model"], "tokens": r["total_tokens"], "cost": r["cost_usd"],
            "thread_id": r["thread_id"], "owner": r["owner"], "feedback": r["feedback"]}

# ------------------------------------------------------------------ read tools
@tool
async def query_runs(project: str, filter: str = "", limit: int = 20, offset: int = 0, sort: str = "newest", lc_agent_name: str = "") -> str:
    """List root runs (traces) of a project. `project` = name or id. `filter` uses the Agent OS filter DSL, e.g.
    eq(status,"error"), gt(latency,5), has(tags,"x"), search("text"), and(...). `sort` = newest | oldest | slowest | costliest | most_tokens
    (use slowest/costliest for "which run took longest / cost most" instead of paging). `lc_agent_name` = a sub-agent's name: list the runs made inside that
    sub-agent (child runs, not only roots). Returns a trimmed page; use next_offset for more."""
    import obs
    limit = max(1, min(limit, 100)); offset = max(0, offset)
    rows = await _call(obs.runs(await _project_id(project), filter or "", True, limit, offset, None, None, sort or "newest", lc_agent_name or ""))   # GD7.R: Q5 paged 5x to find the slowest run
    return _page(rows, _run_row, 0, base=offset, more=len(rows) == limit)

@tool
async def get_trace(trace_id: str, span_id: str = "", offset: int = 0) -> str:
    """Get one trace: spans in order (name, type, status, latency, model, tokens, error, trimmed inputs/outputs) plus feedback.
    Pass `span_id` to read one span in full (up to ~12k chars). `offset` pages through spans of a big trace."""
    import obs
    d = await _call(obs.trace(_uuid(trace_id, "trace_id")))
    runs = d["runs"]
    if span_id:
        r = next((x for x in runs if str(x["id"]) == span_id), None)
        if not r: raise ToolException("Span not found in this trace")
        return _cut({k: r.get(k) for k in ("id", "parent_id", "name", "run_type", "status", "error", "inputs", "outputs", "model", "total_tokens", "cost_usd", "latency_ms", "tags", "metadata")}, 12000)
    def shape(r):
        return {"id": r["id"], "parent": r["parent_id"], "name": r["name"], "type": r["run_type"], "status": r["status"], "ms": r["latency_ms"], "model": r["model"],
                "tokens": r["total_tokens"], "error": _cut(r["error"], 300) if r["error"] else None, "in": _cut(r["inputs"], 300), "out": _cut(r["outputs"], 300)}
    fb = [{"run_id": f["run_id"], "key": f["key"], "score": f["score"], "source": f.get("source"), "comment": _cut(f.get("comment") or "", 150)} for f in d["feedback"][:20]]
    return _page(runs, shape, max(0, offset), feedback=fb)

@tool
async def get_thread(thread_id: str, limit: int = 30, offset: int = 0) -> str:
    """Get the turns of a conversation thread (one row per root run: input, output, status, latency, cost), oldest first.
    Long threads: pass the previous result's `next_offset` as `offset` to read the next page."""
    limit, offset = max(1, min(limit, 200)), max(0, offset)
    rows = await A.q("SELECT id, trace_id, name, start_time, latency_ms, status, left(inputs::text,400) i, left(outputs::text,400) o, left(error,200) error, total_tokens, cost_usd "
                     "FROM tr_runs WHERE parent_id IS NULL AND thread_id=%s ORDER BY start_time, id LIMIT %s OFFSET %s", (thread_id, limit + 1, offset))
    if not rows and not offset: raise ToolException("Thread not found (or not visible to you)")
    more = len(rows) > limit; rows = rows[:limit]
    return _page(rows, lambda r: {"run_id": r["id"], "trace_id": r["trace_id"], "time": r["start_time"], "status": r["status"], "ms": r["latency_ms"], "tokens": r["total_tokens"],
                                  "cost": r["cost_usd"], "in": _cut(r["i"], 300), "out": _cut(r["o"], 300), "error": r["error"]}, 0, base=offset, more=more)

@tool
async def list_issues(project: str = "", status: str = "open", kind: str = "", limit: int = 30) -> str:
    """List Engine issues (GĐ7.4): recurring problems found by the detector - kind error_cluster | low_score | negative_feedback; status open (open + fixing) | open | fixing | watching |
    completed | ignored | all (old names new / in_progress / fixed still accepted). Sorted by severity (0 urgent .. 3 low), then last_seen. `project` = name or id (empty = all projects). Each item: id, title, count, last_seen, root_cause + proposal type if analysed, and sample run ids
    you may read (use get_trace)."""
    import engine
    try: sw, a = engine.status_where(status)
    except ValueError: raise ToolException("status must be open, all, fixing, watching, completed or ignored")
    if kind and kind not in engine.KINDS: raise ToolException("kind must be error_cluster, low_score or negative_feedback")
    w = [sw] if sw else []
    if kind: w.append("i.kind=%s"); a.append(kind)
    if project: w.append("i.project_id=%s"); a.append(await _project_id(project))
    rows = await A.q(f"SELECT i.*, p.name project FROM issues i JOIN projects p ON p.id=i.project_id{' WHERE ' + ' AND '.join(w) if w else ''} ORDER BY i.severity, i.last_seen DESC, i.id DESC LIMIT %s", (*a, max(1, min(limit, 100))))
    vis = await engine.visible_runs([i for r in rows for i in r["sample_run_ids"]])   # RLS of the calling user: private traces of others never show
    def shape(r):
        b = engine.brief(r)
        return {**{k: b[k] for k in ("id", "project", "kind", "title", "status", "count", "first_seen", "last_seen", "has_analysis", "root_cause", "proposal_type", "applied", "severity", "category")},
                "sample_run_ids": [i for i in r["sample_run_ids"] if i in vis]}
    return _page(rows, shape)

@tool
async def get_prompt(name: str, tag: str = "", commit: int = 0) -> str:
    """Read a prompt from the Prompt Hub. Latest commit by default; `tag` (e.g. production) or `commit` (version number) selects another.
    Returns template, model, temperature, note, plus all tags and the latest commit number."""
    import studio
    ref = f"{name}:{commit}" if commit else (f"{name}:{tag}" if tag else name)
    pv = await studio.resolve_prompt(ref)
    if not pv: raise ToolException(f"Prompt/tag/commit not found: {ref}")
    tags = await A.q("SELECT tag, version FROM prompt_tags WHERE prompt_id=%s ORDER BY tag", (pv["prompt_id"],))
    latest = (await A.q("SELECT max(version) v FROM prompt_versions WHERE prompt_id=%s", (pv["prompt_id"],), one=True))["v"]
    return _dump({"name": name, "commit": pv["version"], "latest_commit": latest, "tags": {t["tag"]: t["version"] for t in tags}, "model": pv["model"], "temperature": pv["temperature"],
                  "note": pv["note"], "created": pv["created"], "template": _cut(pv["template"], 10000)})

async def _pack_row(name):
    r = await A.q("SELECT id, name, description FROM context_packs WHERE name=%s", (name,), one=True)   # RLS: the caller's workspace only
    if not r: raise ToolException(f"Context pack not found: {name}")
    return r

@tool
async def get_pack(name: str, ref: str = "prod", path: str = "") -> str:
    """Read a Context Hub pack (AGENTS.md, skills, prompts, policies). `ref` = a tag (default prod) or a commit hash prefix or `latest`.
    Without `path` returns the file list, the commit and AGENTS.md; with `path` (e.g. skills/refund/SKILL.md) returns that file."""
    import context_hub as H
    pk = await _pack_row(name)
    try: c = await H.resolve_commit(pk["id"], ref)
    except HTTPException as e: raise ToolException(str(e.detail)) from None
    files = await H.files_of(pk["id"], c)
    tags = [t["tag"] for t in await A.q("SELECT tag FROM context_tags WHERE pack_id=%s AND hash=%s ORDER BY tag", (pk["id"], c["hash"]))]
    head = {"pack": pk["name"], "description": pk["description"], "ref": ref, "commit": H._short(c["hash"]), "tags": tags, "message": c["message"], "author": c["author"], "created": c["created_at"]}
    if path:
        if path not in files: raise ToolException(f"No file {path} in {name}@{ref}. Files: {', '.join(sorted(files)[:40])}")
        return _dump({**head, "path": path, "content": _cut(files[path], 10000)})
    return _dump({**head, "files": {k: len(v) for k, v in sorted(files.items())}, "agents_md": _cut(files.get("AGENTS.md", ""), 4000) or None})

@tool
async def list_pack_versions(name: str, limit: int = 20) -> str:
    """Commit history of a Context Hub pack (newest first) and which tags (prod, draft, ...) point where. Use it before get_pack to pick a ref."""
    import context_hub as H
    pk = await _pack_row(name)
    limit = max(1, min(int(limit), 100))
    commits = await A.q("SELECT hash, seq, author, message, created_at, (SELECT count(*) FROM jsonb_object_keys(tree)) n_files FROM context_commits WHERE pack_id=%s ORDER BY seq DESC LIMIT %s", (pk["id"], limit))
    tags = await A.q("SELECT tag, hash FROM context_tags WHERE pack_id=%s ORDER BY tag", (pk["id"],))
    by = {}
    for t in tags: by.setdefault(t["hash"], []).append(t["tag"])
    return _dump({"pack": pk["name"], "tags": {t["tag"]: H._short(t["hash"]) for t in tags},
                  "commits": [{"commit": H._short(c["hash"]), "seq": c["seq"], "tags": by.get(c["hash"], []), "author": c["author"], "message": _cut(c["message"], 200), "created": c["created_at"], "files": c["n_files"]} for c in commits]})

_PERIOD = re.compile(r"^(\d{1,3})([hd])$")

@tool
async def get_costs(period: str = "7d", group_by: str = "model") -> str:
    """LLM spend from the gateway for `period` (24h, 7d, 30d, today) grouped by model | user | agent | feature. Users only see their own rows in `user` grouping
    unless they are workspace admins."""
    if group_by not in ("model", "user", "agent", "feature"): raise ToolException("group_by must be model, user, agent or feature")
    if period == "today": since = "date_trunc('day', now() AT TIME ZONE 'Asia/Ho_Chi_Minh') AT TIME ZONE 'Asia/Ho_Chi_Minh'"; args = []
    else:
        m = _PERIOD.match(period or "")
        if not m: raise ToolException("period must look like 24h, 7d, 30d or today")
        since = "now() - %s * (CASE WHEN %s='h' THEN interval '1 hour' ELSE interval '1 day' END)"; args = [int(m[1]), m[2]]
    u = _user(); ws = _adm().cur_ws()
    key = {"model": "g.model", "user": "coalesce(u.username, '(none)')", "agent": "coalesce(a.name, '(none)')", "feature": "coalesce(g.feature, '(chat/agent)')"}[group_by]
    own = "" if u.get("role") == "admin" else " AND g.user_id IS NOT DISTINCT FROM %s" if group_by == "user" else ""
    extra = [u["id"]] if own else []
    rows = await A.q(f"SELECT {key} k, count(*) calls, count(*) FILTER (WHERE g.status<>'ok') errors, coalesce(sum(g.input_tokens),0) in_tok, coalesce(sum(g.output_tokens),0) out_tok, "
                     f"coalesce(sum(g.cost_usd),0) cost FROM gateway_usage g LEFT JOIN users u ON u.id=g.user_id LEFT JOIN agents a ON a.id=g.agent_id "
                     f"WHERE g.ts > {since} AND (g.workspace_id=%s OR (g.workspace_id IS NULL AND g.agent_id IN (SELECT id FROM agents))){own} GROUP BY 1 ORDER BY cost DESC",
                     (*args, ws, *extra))
    return _page(rows, lambda r: {group_by: r["k"], "calls": r["calls"], "errors": r["errors"], "in_tokens": r["in_tok"], "out_tokens": r["out_tok"], "cost_usd": round(float(r["cost"]), 6)},
                 period=period, group_by=group_by, total_cost_usd=round(sum(float(r["cost"]) for r in rows), 6))

@tool
async def list_datasets() -> str:
    """List datasets of the workspace: id, name, description, version, number of examples and experiments."""
    import evals
    rows = await _call(evals.ds_list())
    return _page(rows, lambda d: {"id": d["id"], "name": d["name"], "description": _cut(d["description"] or "", 120), "version": d["version"], "examples": d["n"], "experiments": d["n_experiments"]})

async def _exp(eid):
    return await A.q("SELECT x.*, d.name dataset FROM experiments x JOIN datasets d ON d.id=x.dataset_id WHERE x.id=%s", (eid,), one=True)

@tool
async def get_experiment(id: int) -> str:
    """Get an experiment: status, progress, target, dataset and the summary (counts, latency, tokens, cost, average score per evaluator key)."""
    import evals
    x = await _exp(id)
    if not x: raise ToolException("Experiment not found")
    s = await evals.exp_summary(id)
    return _dump({"id": x["id"], "name": x["name"], "dataset": x["dataset"], "dataset_version": x["dataset_version"], "status": x["status"], "progress": x["progress"], "target": x["target"],
                  "created": x["created"], "finished": x["finished"], "n": s["n"], "errors": s["errors"], "avg_ms": s["avg_ms"], "p95_ms": s["p95"], "tokens": s["tokens"], "cost_usd": s["cost"],
                  "scores": {r["key"]: r["avg"] for r in s["scores"]}, "summary_scores": s["summary_scores"]})

@tool
async def compare_experiments(a: int, b: int) -> str:
    """Compare two experiments on the same dataset: summary of each, score deltas (b - a) per evaluator key, how many examples b wins/loses,
    and up to 5 examples where b is worse than a."""
    import evals
    d = await _call(evals.exp_compare(f"{a},{b}"))
    if len(d["experiments"]) < 2: raise ToolException("Both experiments must exist and be visible to you")
    def per_example(eid):
        out = {}
        for r in d["results"]:
            if r["experiment_id"] != eid: continue
            sc = [f["score"] for f in (r["fb"] or []) if f["score"] is not None]
            out.setdefault(r["example_id"], []).append(sum(sc) / len(sc) if sc else None)
        return {k: (sum(x for x in v if x is not None) / max(1, len([x for x in v if x is not None])) if any(x is not None for x in v) else None) for k, v in out.items()}
    pa, pb = per_example(a), per_example(b); ex = {e["id"]: e for e in d["examples"]}
    wins = loses = ties = 0; worse = []
    for k in pa.keys() & pb.keys():
        if pa[k] is None or pb[k] is None: continue
        if pb[k] > pa[k]: wins += 1
        elif pb[k] < pa[k]: loses += 1; worse.append((pb[k] - pa[k], k))
        else: ties += 1
    sc = {e: {r["key"]: float(r["avg"]) for r in d["summary"][e]["scores"]} for e in (a, b)}
    delta = {k: round(sc[b][k] - sc[a][k], 4) for k in sc[a].keys() & sc[b].keys()}
    brief = lambda e: {k: d["summary"][e][k] for k in ("n", "errors", "avg_ms", "tokens", "cost")}
    return _cut({"a": {"id": a, **brief(a), "scores": sc[a]}, "b": {"id": b, **brief(b), "scores": sc[b]}, "score_delta_b_minus_a": delta, "examples": {"b_better": wins, "b_worse": loses, "tie": ties},
                 "worst_regressions": [{"example_id": k, "delta": round(dl, 3), "question": _cut(evals._question(ex[k]["inputs"]), 150) if k in ex else None} for dl, k in sorted(worse)[:5]]}, CHARS)

# ------------------------------------------------------------------ write tools (WRITE_TOOLS: the assistant must ask the user before calling these)
@tool
async def propose_prompt_edit(name: str, new_template: str, message: str) -> str:
    """Propose a prompt change: creates a NEW commit of an existing prompt (model/temperature/variables copied from the latest commit).
    It never moves any tag, so production keeps serving the old commit until a person moves the tag. `message` says why."""
    import studio
    _need_write()
    prev = await studio.resolve_prompt(name)
    if not prev: raise ToolException(f"Prompt not found: {name} (propose_prompt_edit only edits existing prompts)")
    c = studio.Commit(name=name, template=new_template, model=prev["model"], temperature=prev["temperature"], variables=prev["variables"] or {}, output_schema=prev.get("output_schema"),
                      template_format=prev.get("template_format") or "mustache", parent_version=prev["version"], message=("[proposed via Agent OS tools] " + (message or ""))[:500])
    r = await _call(studio.hub_commit(c))
    tags = await A.q("SELECT tag, version FROM prompt_tags WHERE prompt_id=%s ORDER BY tag", (r["id"],))
    return _dump({"name": name, "new_commit": r["version"], "parent_commit": prev["version"], "tags_unchanged": {t["tag"]: t["version"] for t in tags}})

@tool
async def create_dataset_from_runs(name: str, run_ids: list[str], split: str = "base") -> str:
    """Create a dataset (or add to an existing one with the same name) from run/trace ids: each run becomes an example
    (question = run input, answer = run output). Returns dataset id, version and the examples added."""
    import evals
    _need_write()
    if not run_ids: raise ToolException("run_ids is empty")
    run_ids = list(dict.fromkeys(_uuid(r, "run id") for r in run_ids))[:200]
    seen = {str(r["id"]) for r in await A.q("SELECT id FROM tr_runs WHERE id::text = ANY(%s)", (run_ids,))}
    if missing := [r for r in run_ids if r not in seen]: raise ToolException("Runs not found (or not visible to you): " + ", ".join(missing[:5]))
    ds = await A.q("SELECT id FROM datasets WHERE workspace_id=%s AND name=%s", (_adm().cur_ws(), name), one=True)
    did = ds["id"] if ds else (await _call(evals.ds_new(evals.DatasetIn(name=name, description="Created via Agent OS tools"))))["id"]
    res = await _call(evals.ds_from_runs(did, evals.FromRuns(run_ids=run_ids, split=split)))
    v = (await A.q("SELECT version FROM datasets WHERE id=%s", (did,), one=True))["version"]
    return _dump({"dataset_id": did, "name": name, "version": v, "results": res["results"]})

@tool
async def draft_evaluator(name: str, kind: str, prompt: str = "", code: str = "", key: str = "") -> str:
    """Create an evaluator marked draft=true (it scores nothing until someone attaches it to a dataset/experiment/rule). kind = llm (needs `prompt`: a rubric using
    {{input}} {{output}} {{reference}}) or code (needs `code` defining def evaluate(run, example)). `key` = score name (default = name)."""
    import evals
    _need_write()
    if kind not in ("llm", "code"): raise ToolException("kind must be llm or code")
    cfg = {"key": key or name, "draft": True}
    if kind == "llm": cfg.update(prompt=prompt, score_type="numeric", min=1, max=5)
    else: cfg["code"] = code
    r = await _call(evals.ev_new(evals.EvaluatorIn(name=name, kind=kind, config=cfg)))
    return _dump({"evaluator_id": r["id"], "name": name, "kind": kind, "draft": True})

@tool
async def run_experiment(dataset: str, target_agent: str, name: str = "") -> str:
    """Start an experiment: run every example of `dataset` (name or id) through `target_agent` (agent id or name, current version). Runs in the background;
    poll get_experiment(id). Spends model budget (feature caps apply)."""
    import evals
    _need_write()
    ds = await _dataset(dataset)
    ag = await A.q("SELECT id, name, current_version FROM agents WHERE NOT deleted AND NOT hidden AND (id::text=%s OR name=%s) ORDER BY id LIMIT 1", (str(target_agent), str(target_agent)), one=True)
    if not ag: raise ToolException(f"Agent not found: {target_agent}")
    eid = await _call(evals.start_experiment(evals.ExpIn(name=(name or f"{ds['name']} on {ag['name']}")[:120], dataset_id=ds["id"],
                                                         target={"type": "agent", "agent_id": ag["id"], "version": ag["current_version"]})))
    return _dump({"experiment_id": eid, "dataset": ds["name"], "agent": ag["name"], "agent_version": ag["current_version"], "status": "started"})

_TOOLS = [query_runs, get_trace, get_thread, list_issues, get_prompt, get_pack, list_pack_versions, get_costs, list_datasets, get_experiment, compare_experiments,
          propose_prompt_edit, create_dataset_from_runs, draft_evaluator, run_experiment]
for _t in _TOOLS: _t.metadata = {"write": _t.name in WRITE_TOOLS}
assert {t.name for t in _TOOLS if t.metadata["write"]} == WRITE_TOOLS

def get_tools(write: bool = True, handle_errors: bool = False) -> list:
    """The tool set. 7.1: `create_deep_agent(tools=get_tools(handle_errors=True), interrupt_on={n: True for n in WRITE_TOOLS})`.
    handle_errors=True turns ToolException into a message for the model; False (MCP) lets it surface as isError."""
    out = [t for t in _TOOLS if write or t.name not in WRITE_TOOLS]
    if handle_errors:
        out = [t.model_copy(update={"handle_tool_error": True}) for t in out]
    return out

def skill_files(base: str = "/skills/main/") -> dict:
    """Built-in `agentos` skill as virtual files for deepagents `skills=[base]` (merge into the StateBackend files, like deepagent_api._materialize)."""
    return {f"{base}agentos/SKILL.md": SKILL_FILE.read_text(encoding="utf-8")}

# ------------------------------------------------------------------ MCP server at /mcp/aos
_MCP = None

def mcp_server():
    """FastMCP (official mcp SDK) exposing the same tools. Stateless + JSON responses: every request is authenticated by the normal middleware
    (session cookie / aos_ key / Basic) and runs under that request's identity; there is no long-lived session that could keep an old identity."""
    global _MCP
    if _MCP is None:
        import mcp_bridge as B
        _MCP = B.build(B.LCServer("moza-agent-os", get_tools(write=True), instructions="Agent OS tools: read traces/prompts/costs/datasets/experiments; write tools propose changes (new commits, drafts) and never move production tags."))   # auth is done by admin.middleware
    return _MCP

@contextlib.asynccontextmanager
async def mcp_lifespan():
    async with mcp_server().session_manager.run(): yield

def mount(app):
    """Add /mcp/aos (and /mcp/aos/) as raw ASGI routes (a Mount would 307-redirect the bare path)."""
    from starlette.routing import Route
    srv = mcp_server()
    class Asgi:   # a class instance (not a function) is used by Starlette as a raw ASGI app, for every method
        async def __call__(self, scope, receive, send): await srv.session_manager.handle_request(scope, receive, send)
    for p in ("/mcp/aos", "/mcp/aos/"): app.router.routes.insert(0, Route(p, endpoint=Asgi()))
