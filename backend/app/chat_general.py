"""GĐ11.38 "Trò chuyện chung": the general chat agent every workspace gets (agents.system_kind='general_chat', NOT hidden, listed first) + the caller's NAME in the prompt.

- One row per workspace, created/refreshed lazily by ensure_general_chat() (unique index agents_one_system_kind, advisory lock: api + worker can race). A release changes the prompt/tools
  by code, no migration. Read-only through the agent API (deepagent_api._visible / agent_purge._access refuse system_kind rows); never bound to a channel (channels.create_binding refuses
  it, and the middleware refuses a channel caller: F10).
- Tools: the model sees only the CORE tools (list_examples) + find_tools. Every other tool of the workspace (MCP servers, Google/Microsoft office, Agent OS tools) is registered but hidden
  until find_tools returns it (tool_catalog.FindToolsMiddleware). The set is computed at build time (assemble); the agent cache is cleared when the registry changes. Writes keep the normal
  approval: MCP tools (not HA reads) and aos write tools through interrupt_on, office tools through workspace_tools.interrupt_config. The caller's OWN accounts are used (the agent has no
  connection of its own: connections.resolve falls back to the caller). Demo workspace: Agent OS READ tools + list_examples only (no MCP / office / write tools).
- CallerNameMiddleware: adds one line "Người dùng: <display name>" to the system message of every model call of a shared agent (this one, or any agent with an agent_shares row), also for
  headless / API runs (caller.current() is restored from the signed run metadata). Only the NAME: never an e-mail (an e-mail-looking value is cut at '@'; users have no e-mail column)."""
from __future__ import annotations
import json, re, time, uuid
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage
from langchain_core.tools import tool
from psycopg.types.json import Jsonb

KIND = "general_chat"
NAME = "Trò chuyện chung"
MODEL = __import__("os").environ.get("AOS_GENERAL_CHAT_MODEL", "gemini-3.8-flash")
CORE = ["aos:list_examples"]
_TTL = 60
_SEEN: dict = {}      # workspace id -> monotonic time of the last ensure (per process)
_SHARED: dict = {}    # agent id -> (expires, bool)
_NAMES: dict = {}     # user id -> (expires, name)

PROMPT = """Bạn là trợ lý trò chuyện chung của Agent OS. Trả lời bằng ngôn ngữ của tin nhắn cuối của người dùng, ngắn gọn: nêu kết quả trước.
- Bạn chỉ thấy vài công cụ lõi và `find_tools`. Khi cần làm việc với hệ thống ngoài (tạo issue, gửi thư, xem lịch, tra dữ liệu, trace, dataset...), gọi `find_tools` với nhu cầu bằng vài từ,
  rồi dùng công cụ tìm được. Đừng bịa tên công cụ; không có công cụ phù hợp thì nói rõ.
- Công cụ GHI (tạo, sửa, gửi, xoá) luôn chờ người dùng duyệt: nói một câu bạn sắp làm gì rồi gọi công cụ. Công cụ ĐỌC dùng thoải mái.
- Bạn dùng tài khoản của chính người đang trò chuyện; nếu thiếu kết nối, hướng dẫn họ kết nối trong thẻ hiện ra.
- `list_examples` đọc ví dụ của dataset trong workspace này. Không dán nội dung dài, tóm tắt và nêu id."""

@tool
async def list_examples(dataset: str, filter: str = "", offset: int = 0, limit: int = 20) -> str:
    """List examples (inputs/outputs) of a dataset of this workspace. `dataset` = name or id. `filter` = optional text that must appear in the inputs or outputs (case-insensitive).
    `offset`/`limit` page the result (limit max 50); the answer says total and next_offset."""
    import aos_tools as T, evals
    d = await T._dataset(dataset)
    _, rows = await evals.examples_at(d["id"])
    f = (filter or "").strip().lower()
    if f: rows = [r for r in rows if f in json.dumps([r["inputs"], r["outputs"]], ensure_ascii=False, default=str).lower()]
    offset = max(0, int(offset)); limit = max(1, min(int(limit), 50))
    page = rows[offset:offset + limit]
    return T._dump({"dataset": d["name"], "total": len(rows), "next_offset": offset + limit if offset + limit < len(rows) else None,
                    "examples": [{"id": r["id"], "inputs": T._cut(r["inputs"], 400), "outputs": T._cut(r["outputs"], 400), "splits": r["splits"]} for r in page]})
list_examples.handle_tool_error = True

def tool_pool() -> dict: return {"aos:list_examples": list_examples}

def desired(demo: bool = False):
    import aos_tools as T, deepagent_api as A
    return A.AgentCfg(name=NAME, description="Trò chuyện chung: hỏi đáp và làm việc với mọi công cụ của workspace (ghi cần duyệt).", system_prompt=PROMPT, model=MODEL, temperature=0.3,
                      planning=False, filesystem=False, tools=list(CORE), interrupt_on=[f"aos:{n}" for n in sorted(T.WRITE_TOOLS)], rpm_limit=20)

# ------------------------------------------------------------------ per-workspace agent
async def ensure_general_chat() -> dict:
    """Create/refresh the general chat agent + its (system) assistant row for the CURRENT workspace. Idempotent. Returns {"id", "assistant_id"}."""
    import admin as _adm, deepagent_api as A
    ws = _adm.cur_ws()
    wr = await A.q("SELECT demo FROM workspaces WHERE id=%s", (ws,), one=True)
    cfg = desired(bool(wr and wr["demo"]))
    want = json.loads(json.dumps(cfg.model_dump(exclude={"note"})))
    async with A.S["pool"].connection() as lc, lc.transaction():
        await lc.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"aos-general-chat-ws{ws}",))
        sel = ("SELECT a.id, a.current_version, v.config FROM agents a LEFT JOIN agent_versions v ON v.agent_id=a.id AND v.version=a.current_version "
               "WHERE a.system_kind=%s AND NOT a.deleted AND a.workspace_id=%s ORDER BY a.id LIMIT 1")
        ag = await A.q(sel, (KIND, ws), one=True)
        ins = None if ag else await A.q("INSERT INTO agents(name, workspace_id, system_kind) VALUES(%s,%s,%s) ON CONFLICT (workspace_id, system_kind) WHERE system_kind IS NOT NULL AND NOT deleted DO NOTHING RETURNING id",
                                        (NAME, ws, KIND), one=True)
        if not ag and not ins: ag = await A.q(sel, (KIND, ws), one=True)
        if ins:
            aid = ins["id"]; await A.save_version(aid, cfg, "built-in")
        else:
            aid = ag["id"]
            if json.loads(json.dumps(ag["config"] or {})) != want: await A.save_version(aid, cfg, "built-in update")
    asid = uuid.uuid5(uuid.NAMESPACE_URL, f"agentos-general-chat-ws{ws}")
    md = {"created_by": "system", "chat_visible": True, "system_kind": KIND}
    # created_at far in the past: the chat's default order (created_at ASC) lists it first
    await A.q("INSERT INTO lg_assistants(assistant_id,graph_id,agent_id,name,description,metadata,workspace_id,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,'2000-01-01') "
              "ON CONFLICT (assistant_id) DO UPDATE SET agent_id=EXCLUDED.agent_id, name=EXCLUDED.name, description=EXCLUDED.description, metadata=EXCLUDED.metadata",
              (asid, f"agent-{aid}", aid, NAME, cfg.description, Jsonb(md), ws))
    return {"id": aid, "assistant_id": str(asid)}

async def ensure_for_request():
    """Called by the assistants list (once a minute per workspace per process). Never raises: the chat list must work even if this fails."""
    import admin as _adm
    u = _adm.cur()
    if not u or u.get("auth") in ("channel", "run"): return
    ws = _adm.cur_ws()
    if time.monotonic() - _SEEN.get(ws, -1e9) < _TTL: return
    try:
        await ensure_general_chat(); _SEEN[ws] = time.monotonic()
    except Exception as e:  # noqa: BLE001
        print("general chat ensure failed:", repr(e)[:200], flush=True)

# ------------------------------------------------------------------ tools (called by deepagent_api.build_agent)
def assemble(cfg, pool: dict, mcp_keys: list, demo: bool):
    """-> (cfg with the full key list, tools, FindToolsMiddleware). Core tools stay visible; everything else is deferred behind find_tools."""
    import aos_tools as T, tool_catalog as C
    core = [k for k in cfg.tools if k in pool]
    if demo:
        deferred = [k for k in pool if k.startswith("aos:") and k.split(":", 1)[1] not in T.WRITE_TOOLS and k not in core]
    else:
        mk = set(mcp_keys)
        deferred = [k for k in pool if k not in core and (k.startswith(("aos:", "google:", "ms365:")) or k in mk)]
    seen, keys = {t.name for t in (pool[k] for k in core)}, []
    for k in deferred:   # one entry per tool name (old office aliases point at the same tool)
        n = pool[k].name
        if n in seen: continue
        seen.add(n); keys.append(k)
    dtools = [pool[k] for k in keys]
    ft = C.make_find_tools(dtools)
    return cfg.model_copy(update={"tools": core + keys}), [pool[k] for k in core] + dtools + [ft], C.FindToolsMiddleware({t.name for t in dtools})

# ------------------------------------------------------------------ caller name
def clean_name(v) -> str:
    s = re.sub(r"[\x00-\x1f\x7f\s]+", " ", str(v or "")).strip()
    if "@" in s: s = s.split("@", 1)[0].strip()   # never an e-mail address
    return s[:80]

async def caller_name(c: dict) -> str:
    """Display name of the person a run acts for (users.display_name, else username); '' when unknown."""
    au = c.get("auth_user") or {}
    uid = c.get("user_id")
    if isinstance(uid, int):
        hit = _NAMES.get(uid)
        if hit and hit[0] > time.monotonic(): return hit[1]
        import gateway as gw
        r = await gw._sys("SELECT display_name, username FROM users WHERE id=%s", (uid,), one=True)
        nm = clean_name((r or {}).get("display_name")) or clean_name((r or {}).get("username"))
        _NAMES[uid] = (time.monotonic() + _TTL, nm)
        if nm: return nm
    return clean_name(au.get("display_name")) or clean_name(au.get("identity")) or clean_name(c.get("username"))

async def is_shared(agent_id: int) -> bool:
    hit = _SHARED.get(agent_id)
    if hit and hit[0] > time.monotonic(): return hit[1]
    import gateway as gw
    r = await gw._sys("SELECT 1 FROM agent_shares WHERE agent_id=%s LIMIT 1", (agent_id,), one=True)
    _SHARED[agent_id] = (time.monotonic() + _TTL, bool(r)); return bool(r)

def with_line(sm, text: str) -> SystemMessage:
    c = sm.content if sm is not None else ""
    if isinstance(c, list): return SystemMessage(content=[*c, {"type": "text", "text": "\n\n" + text}])
    return SystemMessage(content=(c + "\n\n" if c else "") + text)

class CallerNameMiddleware(AgentMiddleware):
    """'Người dùng: <name>' in the system message of a shared agent; refuses a public-channel caller on the general chat agent."""
    def __init__(self, agent_id: int, general: bool = False):
        super().__init__(); self.agent_id = agent_id; self.general = general
    async def awrap_model_call(self, request, handler):
        import caller
        c = caller.current()
        if self.general and c.get("kind") == "channel": raise PermissionError("Trò chuyện chung không dùng được qua kênh công khai / not available on public channels")
        try:
            if self.general or await is_shared(self.agent_id):
                nm = await caller_name(c)
                if nm: request = request.override(system_message=with_line(request.system_message, f"Người dùng: {nm}"))
        except Exception as e:  # noqa: BLE001  (a lookup hiccup must not stop the run)
            print("caller name failed:", repr(e)[:160], flush=True)
        return await handler(request)
