"""Moza Agent OS — FastAPI (127.0.0.1:8020). Mã tự viết trên stack mã nguồn mở: deepagents, langchain,
langgraph, langchain.mcp (FastMCP client), Postgres + pgvector. Mọi dữ liệu nằm trong lc-postgres."""
import asyncio, json, os, re, shutil, subprocess, time, uuid
from contextlib import asynccontextmanager
from dotenv import load_dotenv
load_dotenv("/opt/langchain/.env")
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import agent_fields as AF   # GĐ13.7 response_format + middleware allow-list
from google import genai
from google.genai import types as gt
from psycopg_pool import AsyncConnectionPool
from psycopg.rows import dict_row
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool, tool as lc_tool
from langchain.agents.middleware import AgentMiddleware, TodoListMiddleware
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.runnables import RunnableConfig
from deepagents import create_deep_agent
from deepagents.middleware.filesystem import _ALL_FS_TOOL_NAMES
import ui
import gateway as gw
from sbx_cfg import ComputerCfg   # GĐ11.41
import hashlib, secrets as pysecrets

DEFAULT_MODEL, EMB_MODEL, DIMS = os.getenv("AOS_DEFAULT_MODEL", "gpt-5.4-nano") if os.getenv("OPENAI_API_KEY") else "gemini-3.6-flash", "gemini-embedding-2", 768
PRICING_SOURCE = gw.PRICING_SOURCE   # from app/pricing.json _meta (updated in place by gw.reload_pricing)
PRICING = gw.PRICING
# An toàn HA: chỉ 2 tool đọc bật mặc định; khoá/báo động/gara bị chặn phía server bất kể UI.
HA_READ_TOOLS = {"GetLiveContext", "GetDateTime"}
def is_ha_read(server, name): return server == "ha" and name.split("__")[-1] in HA_READ_TOOLS
BLOCK_RE_BASE = r"\block\b|unlock|alarm|garage|gara|khóa|khoá|ổ khoá|ổ khóa|báo động|bao dong|cửa cuốn"
_g = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
S: dict = {"agents": {}, "block_names": set(), "testjobs": {}}

_EMB_SEM = asyncio.Semaphore(4)
async def aembed(texts):
    """gemini-embedding-2 (đa phương thức) GỘP cả danh sách contents thành MỘT vector → phải nhúng từng văn bản riêng (song song ≤4).
    (Phát hiện 19/10/2026: gửi list 6 câu chỉ nhận 1 embedding; trước đây zip() âm thầm bỏ các chunk sau.)"""
    async def one(t):
        async with _EMB_SEM:
            r = await _g.aio.models.embed_content(model=EMB_MODEL, contents=t, config=gt.EmbedContentConfig(output_dimensionality=DIMS))
            return r.embeddings[0].values
    return list(await asyncio.gather(*(one(t) for t in texts)))

cost = gw.cost

DDL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS eval_runs(id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL DEFAULT now(), thread_id TEXT,
  model TEXT, user_input TEXT, answer TEXT, tool_calls JSONB, input_tokens INT, output_tokens INT, total_tokens INT,
  cost_usd NUMERIC(12,6), latency_ms INT, error TEXT);
ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS agent_id INT, ADD COLUMN IF NOT EXISTS agent_version INT,
  ADD COLUMN IF NOT EXISTS trace JSONB, ADD COLUMN IF NOT EXISTS test_run_id INT, ADD COLUMN IF NOT EXISTS test_case_id INT;
CREATE TABLE IF NOT EXISTS eval_scores(id BIGSERIAL PRIMARY KEY, run_id BIGINT REFERENCES eval_runs(id) ON DELETE CASCADE,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(), kind TEXT NOT NULL, thumb SMALLINT, score SMALLINT, comment TEXT);
CREATE INDEX IF NOT EXISTS eval_runs_ts ON eval_runs(ts DESC);
CREATE TABLE IF NOT EXISTS agents(id SERIAL PRIMARY KEY, name TEXT NOT NULL, current_version INT NOT NULL DEFAULT 1,
  created TIMESTAMPTZ NOT NULL DEFAULT now(), deleted BOOLEAN NOT NULL DEFAULT false);
CREATE TABLE IF NOT EXISTS agent_versions(id SERIAL PRIMARY KEY, agent_id INT REFERENCES agents(id) ON DELETE CASCADE,
  version INT NOT NULL, config JSONB NOT NULL, note TEXT, created TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(agent_id, version));
CREATE TABLE IF NOT EXISTS mcp_servers(id SERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL, url TEXT NOT NULL,
  transport TEXT NOT NULL DEFAULT 'streamable_http', token_env TEXT, enabled BOOLEAN NOT NULL DEFAULT true);
CREATE TABLE IF NOT EXISTS tool_settings(server TEXT NOT NULL, tool TEXT NOT NULL, enabled BOOLEAN NOT NULL,
  PRIMARY KEY(server, tool));
CREATE TABLE IF NOT EXISTS kb_docs(id SERIAL PRIMARY KEY, kb TEXT NOT NULL DEFAULT 'chung', title TEXT, n_chunks INT,
  created TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS kb_chunks(id BIGSERIAL PRIMARY KEY, doc_id INT REFERENCES kb_docs(id) ON DELETE CASCADE,
  idx INT, text TEXT, embedding vector(768));
ALTER TABLE kb_docs ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'kb', ADD COLUMN IF NOT EXISTS thread_id TEXT, ADD COLUMN IF NOT EXISTS user_id INT, ADD COLUMN IF NOT EXISTS file_id UUID;
CREATE TABLE IF NOT EXISTS test_sets(id SERIAL PRIMARY KEY, name TEXT NOT NULL, created TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS test_cases(id SERIAL PRIMARY KEY, set_id INT REFERENCES test_sets(id) ON DELETE CASCADE,
  question TEXT NOT NULL, expected TEXT);
ALTER TABLE eval_runs ADD COLUMN IF NOT EXISTS spans JSONB, ADD COLUMN IF NOT EXISTS models_used TEXT[], ADD COLUMN IF NOT EXISTS deployment TEXT, ADD COLUMN IF NOT EXISTS trace_root UUID;
CREATE TABLE IF NOT EXISTS gateway_usage(id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL DEFAULT now(), agent_id INT, model TEXT,
  input_tokens INT, output_tokens INT, cost_usd NUMERIC(12,6), status TEXT, latency_ms INT, error TEXT, provider TEXT);   -- 13.Z: provider (migration 0011) exists before the first seed_spend on a fresh DB
CREATE INDEX IF NOT EXISTS gateway_usage_ts ON gateway_usage(ts DESC);
CREATE TABLE IF NOT EXISTS prompts(id SERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL, created TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS prompt_versions(id SERIAL PRIMARY KEY, prompt_id INT REFERENCES prompts(id) ON DELETE CASCADE, version INT NOT NULL,
  template TEXT NOT NULL, model TEXT, temperature REAL, variables JSONB, note TEXT, created TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(prompt_id, version));
CREATE TABLE IF NOT EXISTS deployments(id SERIAL PRIMARY KEY, slug TEXT UNIQUE NOT NULL, agent_id INT REFERENCES agents(id) ON DELETE CASCADE,
  version INT NOT NULL, key_hash TEXT NOT NULL, key_prefix TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS deployment_history(id SERIAL PRIMARY KEY, deployment_id INT REFERENCES deployments(id) ON DELETE CASCADE, version INT,
  action TEXT, ts TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS test_runs(id SERIAL PRIMARY KEY, set_id INT REFERENCES test_sets(id) ON DELETE CASCADE,
  agent_id INT, agent_version INT, status TEXT, created TIMESTAMPTZ NOT NULL DEFAULT now());
"""

async def q(sql, args=(), one=False, many=True):
    ctx = S["rls"]() if S.get("rls") else ("*", "0", "admin")
    async with S["pool"].connection() as c:
        await c.execute("SELECT set_config('app.ws',%s,false), set_config('app.uid',%s,false), set_config('app.role',%s,false), set_config('role', CASE WHEN %s ~ '^[0-9]+$' THEN 'aos_rls' ELSE 'none' END, false)", ctx + (ctx[0],))
        cur = await c.execute(sql, args)
        if cur.description is None: return None
        res = await (cur.fetchone() if one else cur.fetchall())
        if res: __import__("aos_crypto").decrypt_rows([res] if one else res)   # GĐ9.7: giải mã phong bì trace tại chỗ (no-op khi không workspace nào bật)
        return res

# ---------------- Agent config ----------------
class SubAgentCfg(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9_-]{2,40}$")
    description: str = Field(max_length=500)
    system_prompt: str = Field(default="", max_length=8000)
    tools: list[str] = []
    model: str | None = None                   # model riêng cho sub-agent (None = dùng model agent chính)
    skills: list[str] = []                     # skill riêng ("tên" hoặc "tên@phiên_bản"), không kế thừa từ agent chính
    memory: list[str] = []                     # AGENTS.md riêng cho sub-agent (MemoryMiddleware chính thức trong middleware của sub-agent)
    agent_ref: int | None = None               # dùng assistant/agent đã lưu làm sub-agent (lấy prompt/tools/model/skills của bản hiện hành)

class MiddlewareCfg(BaseModel):
    """GĐ13.7: one entry of AgentCfg.middleware (name from the allow-list in agent_fields.SPECS; params bounded there)."""
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=60)
    params: dict = {}

class AgentCfg(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    system_prompt: str = Field(default="", max_length=20000)
    model: str = DEFAULT_MODEL
    temperature: float = Field(default=0.3, ge=0, le=2)
    tools: list[str] = []          # khoá dạng "server:tool" hoặc "local:tên"
    subagents: list[SubAgentCfg] = []
    planning: bool = True
    filesystem: bool = True
    long_term_memory: bool = False
    sandbox: bool = False                       # tool chạy code trong container Docker dùng-một-lần
    fallback_models: list[str] = list(gw.DEFAULT_FALLBACKS)
    rpm_limit: int | None = Field(default=10, ge=1, le=1000)
    budget_usd_day: float | None = Field(default=None, ge=0)
    prompt_ref: str | None = Field(default=None, max_length=120)   # "tên_prompt[:tag|:số]" từ Prompt Hub → system prompt
    interrupt_on: list[str] = []      # tool cần người duyệt trước khi chạy (HITL); tool ghi MCP luôn bị gắn tự động
    skills: list[str] = []            # Agent Skills từ Context Hub (kind=skill, đã publish): "tên" hoặc "tên@phiên_bản"
    delegation_guard: bool = False   # coordinator: chặn general-purpose + giao trùng (DelegationGuard, wrap_tool_call)
    memory: list[str] = []            # AGENTS.md từ Context Hub (kind=agent, đã publish) → memory= (bộ nhớ cố định)
    context_pack: str | None = Field(default=None, max_length=120)   # GĐ9.1 Context Hub pack "tên@tag" (hoặc @mã_commit / @latest): gắn chỉ-đọc tại /pack/ (AGENTS.md + skills/)
    computer: ComputerCfg | None = None   # GĐ11.41 virtual computer (Deep Agents BaseSandbox, app/sbx_backend.py): mode off|thread|agent; None / off = today's behaviour
    response_format: dict | None = None   # GĐ13.7 structured output: JSON Schema (<= 20 KB, local $ref only) -> create_deep_agent(response_format=...) -> `structured_response` in the run output
    middleware: list[MiddlewareCfg] = []  # GĐ13.7 allow-listed LangChain middleware [{name, params}] (ModelFallback -> fallback_models); not in the demo workspace
    chat_modes: dict | None = None        # 27/10 chat modes Fast / Standard / Thinking: {"default","enabled","overrides":{mode:{model,thinking}}} (app/chat_modes.py); None = defaults; not in the demo workspace
    note: str | None = None

    @field_validator("chat_modes", mode="before")
    @classmethod
    def _cm(cls, v): return __import__("chat_modes").validate(v)

    @field_validator("response_format", mode="before")
    @classmethod
    def _rf(cls, v): return AF.check_response_format(v)

    @field_validator("middleware", mode="before")
    @classmethod
    def _mw(cls, v): return AF.check_middleware(v)

    @model_validator(mode="after")
    def _fallback(self):
        rest, fb = AF.split_fallback(self.middleware)   # ModelFallback is not a second mechanism: it IS the gateway chain list of this agent
        if fb is not None: self.middleware, self.fallback_models = rest, fb
        return self

class ToolFilter(AgentMiddleware):
    """Ẩn write_todos / các tool filesystem khỏi model khi agent tắt chúng."""
    def __init__(self, hide: set[str]):
        super().__init__(); self.hide = hide
    async def awrap_model_call(self, request, handler):
        tools = [t for t in request.tools if (t.name if hasattr(t, "name") else t.get("name")) not in self.hide]
        return await handler(request.override(tools=tools))

def block_re():
    names = "|".join(re.escape(n) for n in S["block_names"] if n)
    return re.compile(BLOCK_RE_BASE + (("|" + names) if names else ""), re.I)

def guard(t, server, name=None):
    """Bọc tool MCP: chặn khoá/báo động/gara phía server, bất kể UI bật gì."""
    is_read = is_ha_read(server, t.name)
    async def _run(**kw):
        if not is_read and (block_re().search(t.name) or block_re().search(json.dumps(kw, ensure_ascii=False))):
            return "BỊ CHẶN: thao tác với khoá / báo động / gara luôn bị cấm phía server."
        r = await t.ainvoke(kw)
        return r if isinstance(r, str) else json.dumps(r, ensure_ascii=False, default=str)[:20000]
    return StructuredTool.from_function(coroutine=_run, name=name or t.name, description=t.description,
                                        args_schema=t.args_schema)

async def kb_search(query: str, k: int = 4, kb: str | None = None):
    v = (await aembed([query]))[0]
    rows = await q("SELECT d.title, d.kb, c.idx, c.text, 1-(c.embedding <=> %s::vector) AS score FROM kb_chunks c JOIN kb_docs d ON d.id=c.doc_id"
                   + " WHERE d.kind='kb'" + (" AND d.kb=%s" if kb else "") + " ORDER BY c.embedding <=> %s::vector LIMIT %s",
                   (str(v), kb, str(v), k) if kb else (str(v), str(v), k))
    return [{**r, "score": round(float(r["score"]), 4)} for r in rows]

def local_tools(agent_id):
    @lc_tool
    async def tra_cuu_kien_thuc(cau_hoi: str) -> str:
        """Tra cứu cơ sở kiến thức nội bộ (pgvector). Dùng khi cần thông tin từ tài liệu đã nạp."""
        rs = await kb_search(cau_hoi, 4)
        return "\n\n".join(f"[{r['title']} #{r['idx']} · {r['score']}] {r['text']}" for r in rs) or "Không tìm thấy."
    @lc_tool
    async def luu_ghi_nho(noi_dung: str) -> str:
        """Lưu một ghi nhớ dài hạn (Postgres Store) cho agent này."""
        k = uuid.uuid4().hex[:12]; await S["store"].aput(_mem_ns(agent_id), k, {"text": noi_dung})
        return f"Đã lưu. [mem:{k}]"   # GĐ5a: key lets the /chat 'em đã ghi nhớ' chip forget/edit exactly this memory
    @lc_tool
    async def tim_ghi_nho(truy_van: str) -> str:
        """Tìm trong ghi nhớ dài hạn của agent này."""
        ns = _mem_ns(agent_id)
        rs = await S["store"].asearch(ns, query=truy_van, limit=5)
        if len(ns) == 4: rs = sorted(list(rs) + list(await S["store"].asearch(("agent", str(agent_id), "memories"), query=truy_van, limit=3)), key=lambda i: -(i.score or 0))[:5]
        return "\n".join(f"- {i.value.get('text')}" for i in rs) or "Chưa có ghi nhớ."
    @lc_tool
    async def chay_code_python(code: str, config: RunnableConfig = None) -> str:
        """Chạy đoạn code Python trong sandbox Docker dùng-một-lần (không mạng, 256 MB RAM, 0.5 CPU, timeout 20s, rootfs chỉ đọc). Trả stdout/stderr.
        File csv/xlsx người dùng đính kèm trong cuộc trò chuyện nằm sẵn trong thư mục chạy (đọc bằng tên file)."""
        import attachments
        return await run_sandbox(code, files=await attachments.thread_data_files(attachments._thread_of(config) or ""))
    @lc_tool
    async def web_search(query: str, max_results: int = 5) -> str:
        """Search the public web (DuckDuckGo). Returns title, URL and snippet for each result."""
        return await _web_search(query, max_results)
    @lc_tool
    async def fetch_url(url: str) -> str:
        """Fetch a public web page (http/https only, private addresses blocked) and return its readable text (max 6000 chars)."""
        return await _fetch_url(url)
    @lc_tool
    async def oauth_request(provider: str, path_or_url: str, method: str = "GET", json_body: dict | None = None) -> str:
        """Call a connected third-party API (e.g. GitHub, Google) AS THE CURRENT USER, using their own OAuth connection.
        provider: provider name configured in Settings > Connections. path_or_url: API path (e.g. 'user/repos') or full URL under the provider's API base."""
        import oauth_tools
        return await oauth_tools.tool_request(provider, method, path_or_url, json_body, agent_id=agent_id)
    return {"local:chay_code_python": chay_code_python, "local:tra_cuu_kien_thuc": tra_cuu_kien_thuc, "local:luu_ghi_nho": luu_ghi_nho, "local:tim_ghi_nho": tim_ghi_nho,
            "local:web_search": web_search, "local:fetch_url": fetch_url, "local:oauth_request": oauth_request, **__import__("mocks").tools(agent_id), **__import__("genui").tools(agent_id),   # GĐ3.1 render-only generative UI
            "local:hoi_thong_tin": __import__("chat_gd5").ask_tool(),   # GĐ5a: ask-for-info form card (interrupt -> resume with answers)
            **__import__("skills_mgmt").tools(agent_id),   # GĐ12.87 skill_list / skill_draft / skill_save / skill_share
            **__import__("agent_schedules").tools(agent_id),   # GĐ13.28 lich_tao / lich_danh_sach / lich_xoa / lich_sua (agent makes its own schedule)
            **__import__("agent_identity").tools(agent_id),   # GĐ9.Q: aos_whoami / aos_connect_check (tools that read the caller)
            **__import__("chat_gd5b").tools(agent_id)}   # GĐ5b: canvas + chay_code_python with charts (gVisor sandbox, replaces the text-only one)

import html as _html, ipaddress as _ip, socket as _sock
from urllib.parse import urlparse as _up, unquote as _unq, parse_qs as _pqs
_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AgentOS/1.0"}
def _strip_html(t):
    t = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t); return re.sub(r"\s+", " ", _html.unescape(t)).strip()
async def _web_search(query, n=5):
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15, headers=_UA, follow_redirects=True) as c:
            r = await c.post("https://html.duckduckgo.com/html/", data={"q": query})
        out = []
        for m in re.finditer(r'(?s)<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</', r.text):
            href = _html.unescape(m.group(1)); q_ = _pqs(_up(href).query).get("uddg")
            out.append(f"{len(out)+1}. {_strip_html(m.group(2))}\n   {_unq(q_[0]) if q_ else href}\n   {_strip_html(m.group(3))}")
            if len(out) >= min(int(n or 5), 8): break
        return "\n".join(out) or "No results."
    except Exception as e:  # noqa: BLE001
        return f"Search failed: {str(e)[:200]}"
async def _fetch_url(url):
    import netguard
    try:   # GĐ10.2: netguard checks the first URL and every redirect hop (<= 5), and connects only to the checked IPs
        async with netguard.client("fetch", timeout=20, headers=_UA) as c:
            r = await c.get(url)
        return (_strip_html(r.text) if "html" in r.headers.get("content-type", "") else r.text)[:6000]
    except netguard.NetBlocked as e:
        return f"Blocked: {e.reason}."
    except Exception as e:  # noqa: BLE001
        return f"Fetch failed: {str(e)[:200]}"

def _mem_ns(agent_id):
    """Store riêng theo người dùng (store-auth): ghi nhớ của user A không lộ cho user B. Tác vụ nền/công khai dùng vùng chung của agent."""
    import admin, chat_projects, run_trust
    u = admin.USER.get()
    if run_trust.is_external() and not (u and u.get("auth") == "channel"):   # GĐ11.72: external run that is not a channel chatter (incoming mail): its own per-thread memory, never the owner's, never the shared one
        return ("agent", str(agent_id), "ch", "ext:" + str(u.get("chat") if u and u.get("chat") else run_trust.thread_key() or "x"), "memories")
    if (p := chat_projects.CTX.get()) and p.get("ns"): return p["ns"]   # GĐ8.9: a chat inside a project uses the project's own memory
    if u and u.get("auth") == "channel": return ("agent", str(agent_id), "ch", str(u.get("chat") or "x"), "memories")   # GĐ8.Z: one memory per external chatter, never the owner's
    return ("agent", str(agent_id), f"u{u['id']}", "memories") if u else ("agent", str(agent_id), "memories")

SANDBOX_IMAGE = "python:3.12-alpine"
SBX_RT: list = []          # ["--runtime", "runsc"] when gVisor is available (set by sandbox2.detect_runtime)
SBX_INFO: dict = {"runtime": "docker (runc)"}
async def run_sandbox(code: str, timeout: int = 20, files=()):
    name = "moza-sbx-" + uuid.uuid4().hex[:10]
    cmd = ["docker", "run", "--rm", "-i", "--name", name, *SBX_RT, "--network", "none", "--memory", "256m", "--memory-swap", "256m",
           "--cpus", "0.5", "--pids-limit", "64", "--read-only", "--tmpfs", "/tmp:rw,size=16m,noexec", "--cap-drop", "ALL",
           "--security-opt", "no-new-privileges", "--user", "65534:65534", "--workdir", "/tmp", *[x for h, n in files if ":" not in h and "," not in h for x in ("-v", f"{h}:/tmp/{n}:ro")], SANDBOX_IMAGE, "python", "-I", "-"]
    p = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(p.communicate(code.encode()[:100_000]), timeout)
        res = f"exit={p.returncode}\n" + out.decode(errors="replace")[:6000] + ("\n[stderr]\n" + err.decode(errors="replace")[:3000] if err else "")
    except asyncio.TimeoutError:
        await (await asyncio.create_subprocess_exec("docker", "kill", name, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)).wait()
        res = f"HẾT GIỜ sau {timeout}s — container đã bị huỷ."
    return res

async def usage_sink(agent_id, model, i, o, c, status, ms, err, details=None):
    try:
        model = gw._cn(model)   # 11.3: canonical ref in the usage row (aos/fast and legacy names resolve to one key)
        cx = gw.ctx_now()   # who/what is spending: unified caps (GĐ6.1) sum these columns
        d = details or {}   # GĐ6.2: token kinds billed at their own price (nullable columns)
        await q("INSERT INTO gateway_usage(agent_id,model,input_tokens,output_tokens,cost_usd,status,latency_ms,error,workspace_id,user_id,evaluator_id,feature,cached_tokens,reasoning_tokens,audio_tokens,image_tokens,vkey_id,gw_headers,provider) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (agent_id, model, i, o, c, status, ms, err, cx["workspace_id"], cx["user_id"], cx["evaluator_id"], cx["feature"],
                 d.get("cached") or None, d.get("reasoning") or None, (d.get("audio_in", 0) + d.get("audio_out", 0)) or None, (d.get("image_in", 0) + d.get("image_out", 0)) or None,
                 *__import__("gw_policies").sink_attr(), gw.pname(model)))   # GĐ9.Q: virtual key + matchable X-Gateway-* headers of the /v1 call in flight (per-customer spend); 11.3: provider
        if c: await seed_spend()   # pick up spend from the other process too
    except Exception:  # noqa: BLE001
        pass
gw.USAGE_SINK.append(usage_sink)
async def seed_spend():
    """Spend per provider is authoritative in gateway_usage (shared by the API and worker processes); re-read it, never accumulate locally only."""
    try:
        tot = {}
        for r in await q(f"SELECT {gw._PROV_SQL} p, coalesce(sum(cost_usd),0) c FROM gateway_usage GROUP BY 1"):
            p = r["p"]; tot[p] = tot.get(p, 0.0) + float(r["c"] or 0)
        for p in set(gw.SPENT) | set(tot): gw.SPENT[p] = max(tot.get(p, 0.0), 0.0)
        await gw.caps_snapshot()   # unified caps: refreshed at most every AOS_CAPS_CACHE_S
    except Exception as e: print("seed_spend", e)  # noqa: BLE001

async def spend_sync_loop():
    while True:
        await asyncio.sleep(5); await seed_spend()

async def budget_check(agent_id, budget=None):   # budget: from the agent config (server._spend_guard, before the graph is built)
    b = budget if budget is not None else S.get("budgets", {}).get(agent_id)
    if not b: return
    spent = (await q("SELECT coalesce(sum(cost_usd),0) c FROM gateway_usage WHERE agent_id=%s AND ts > date_trunc('day', now() AT TIME ZONE 'Asia/Ho_Chi_Minh') AT TIME ZONE 'Asia/Ho_Chi_Minh'", (agent_id,), one=True))["c"]
    if float(spent) >= b: raise gw.SpendCapReached(f"Agent đã dùng hết ngân sách ngày ${b} (đã tiêu ${float(spent):.4f})",   # a RuntimeError; info -> 402 detail fields (GĐ8.P)
                                                   {"scope": "agent", "target": str(agent_id), "period": "day", "limit_usd": round(float(b), 4), "used_usd": round(float(spent), 4)})

async def enabled_tool_keys(ws=None):
    return await __import__("mcp_registry").enabled_keys(ws)   # GĐ11.23: per workspace (default: the caller's)

OVERRIDABLE = {"model", "temperature", "system_prompt", "tools", "fallback_models", "planning", "subagents", "interrupt_on", "prompt_ref", "long_term_memory", "sandbox", "filesystem", "skills", "memory", "context_pack", "response_format", "middleware"}

SKILL_NAME_RE = r"^[a-z0-9]+(-[a-z0-9]+)*$"

# GĐ8.6: built-in file skills shipped with the code (app/skills/<name>/SKILL.md + helper script). Every agent that can run code
# (sandbox / local:chay_code_python) gets them as "builtin:<name>" (not in the demo workspace); the helpers run in the sandbox (chat_gd5b).
FILE_SKILLS = ("docx", "xlsx", "pptx", "pdf", "doc-to-markdown")
def builtin_skill_files(name: str, base: str = "/skills/main/") -> dict:
    if name == "agentos": return __import__("aos_tools").skill_files(base)
    if name not in FILE_SKILLS: return {}
    d = __import__("pathlib").Path(__file__).parent / "skills" / name
    return {f"{base}{name}/{f.relative_to(d).as_posix()}": f.read_text(encoding="utf-8") for f in sorted(d.rglob("*")) if f.is_file() and f.suffix in (".md", ".py") and "__pycache__" not in f.parts}   # GĐ11.64: scripts/ too

def with_file_skills(cfg, ws):
    if not (cfg.sandbox or "local:chay_code_python" in cfg.tools) or (ws and int(ws) in gw.DEMO_WS): return cfg
    return cfg.model_copy(update={"skills": list(dict.fromkeys([*cfg.skills, *(f"builtin:{n}" for n in FILE_SKILLS)]))})

async def _computer_skills(agent_id, ws_id, cfg):
    """GĐ11.64: publish the agent's skills for its virtual computer -> (agent folder, hash folder, names) | None (no computer / no skills / a skill that fails the check:
    then the old read-only /skills/main/ files still work)."""
    if not (cfg.computer and cfg.computer.on and cfg.skills) or int(ws_id) in gw.DEMO_WS: return None
    SK = __import__("sbx_skills")
    try: return await SK.prepare(agent_id, ws_id, cfg.skills)
    except SK.SkillError as e:
        print(f"[skills] agent {agent_id}: skills not mounted in the computer: {str(e)[:200]}", flush=True); return None

async def _resolve_sub_refs(cfg):
    """Sub-agent có agent_ref → lấy cấu hình bản hiện hành của assistant đó (trường để trống mới được điền)."""
    out = []
    for s in cfg.subagents:
        if s.agent_ref:
            r = await q("SELECT v.config FROM agents a JOIN agent_versions v ON v.agent_id=a.id AND v.version=a.current_version WHERE a.id=%s", (s.agent_ref,), one=True)
            if r:
                rc = r["config"]
                s = s.model_copy(update={"description": s.description or rc.get("description") or rc.get("name", ""),
                                         "system_prompt": s.system_prompt or rc.get("system_prompt", ""), "tools": s.tools or rc.get("tools", []),
                                         "model": s.model or rc.get("model"), "skills": s.skills or rc.get("skills", []),
                                         "memory": s.memory or rc.get("memory", [])})
        out.append(s)
    return cfg.model_copy(update={"subagents": out})

async def resolve_context(ws, kind, ref, agent_id=None):
    """ref = "tên" (bản publish mới nhất), "tên@N" (bản đã ghim) hoặc "tên@<tag|commit>" (Context Hub). Trả (name, version, files[, _pack]) hoặc None.
    GĐ9.P: nội dung đi qua pack (tag prod) để sửa trong Context Hub tới được agent: "tên" và "tên@N" khi N là bản publish mới nhất đọc pack@prod; N cũ hơn = bản đã ghim cứng (rollback)."""
    name, _, ver = ref.partition("@")
    ch = __import__("context_hub")
    if ver and not ver.isdigit():   # "tên@prod" / "tên@v1" / "tên@3fa9c1d2": a pack tag or commit of the legacy-mirrored pack
        g = await ch.legacy_pack_files(ws, kind, name, ver)
        if g: return {"name": name, "version": ver, "files": g[0], "_pack": (ws, kind, name, ver, g[1])}
        return await ch.skill_resolve(ws, name, ver, agent_id) if kind == "skill" else None   # GĐ12.86 skill packs (private -> owner agent only)
    r = await _resolve_context_legacy(ws, kind, name, ver)
    if r and (not ver or int(ver) == (await q("SELECT max(cv.version) v FROM context_versions cv JOIN contexts c ON c.id=cv.context_id WHERE c.kind=%s AND c.name=%s AND c.workspace_id IS NOT DISTINCT FROM %s", (kind, name, ws), one=True) or {}).get("v")):
        g = await ch.legacy_pack_files(ws, kind, name, "prod")
        if g: return {**r, "files": g[0], "_pack": (ws, kind, name, "prod", g[1])}
    if not r and kind == "skill": return await ch.skill_resolve(ws, name, "prod", agent_id)   # GĐ12.86: "name" follows the prod tag of a skill pack
    return r

async def _resolve_context_legacy(ws, kind, name, ver):
    sql = ("SELECT c.name, cv.version, cv.files FROM context_versions cv JOIN contexts c ON c.id=cv.context_id "
           "WHERE c.kind=%s AND c.name=%s AND c.workspace_id IS NOT DISTINCT FROM %s" + (" AND cv.version=%s" if ver else "") + " ORDER BY cv.version DESC LIMIT 1")
    return await q(sql, (kind, name, ws, int(ver)) if ver else (kind, name, ws), one=True)

async def _materialize(agent_id, cfg):
    """Dựng file ảo cho SkillsMiddleware/MemoryMiddleware (StateBackend): /skills/main/<skill>/..., /skills/sub/<sub>/<skill>/..., /memory/<ctx>/AGENTS.md."""
    if not (cfg.skills or cfg.memory or any(s.skills or s.memory for s in cfg.subagents)): return {}, []
    ws = (await q("SELECT workspace_id FROM agents WHERE id=%s", (agent_id,), one=True) or {}).get("workspace_id")
    files, info = {}, []
    async def put(kind, ref, base, only=None):
        r = await resolve_context(ws, kind, ref, agent_id)
        if not r: info.append({"ref": ref, "missing": True}); return
        info.append({"ref": ref, "name": r["name"], "version": r["version"], "at": base, **({"_stamp": r["_pack"], "via": "pack@" + str(r["_pack"][3])} if r.get("_pack") else {})})
        for k, v in (r["files"] or {}).items():
            if only and k != only: continue
            files[f"{base}{r['name']}/{k.lstrip('/')}"] = v
    for ref in cfg.skills:
        if ref.startswith("builtin:"):   # GĐ7.1: skills shipped with the code (aos_tools.skill_files), not in the Context Hub
            files.update(builtin_skill_files(ref[8:], "/skills/main/")); info.append({"ref": ref, "name": ref[8:], "builtin": True, "at": "/skills/main/"})
        else: await put("skill", ref, "/skills/main/")
    for s in cfg.subagents:
        for ref in s.skills: await put("skill", ref, f"/skills/sub/{s.name}/")
    for ref in dict.fromkeys(list(cfg.memory) + [m for s in cfg.subagents for m in s.memory]): await put("agent", ref, "/memory/", "AGENTS.md")
    return files, info

async def pin_refs(agent_id, cfg):
    """Khi lưu phiên bản agent: ghim "tên" → "tên@N" (bản publish mới nhất) để rollback agent = rollback skill."""
    ws = (await q("SELECT workspace_id FROM agents WHERE id=%s", (agent_id,), one=True) or {}).get("workspace_id")
    async def pin(kind, refs):
        out = []
        for ref in refs:
            if "@" in ref or ref.startswith("builtin:"): out.append(ref); continue
            r = await resolve_context(ws, kind, ref, agent_id)
            if not r: raise HTTPException(400, f"{'Skill' if kind == 'skill' else 'AGENTS.md context'} '{ref}' chưa publish / not published")
            out.append(f"{r['name']}@{r['version']}")
        return out
    subs = [s.model_copy(update={"skills": await pin("skill", s.skills), "memory": await pin("agent", s.memory)}) for s in cfg.subagents]
    pack = await __import__("context_hub").validate_ref(ws, cfg.context_pack) if cfg.context_pack else None   # GĐ9.1: pack@tag must exist (the tag itself stays movable = pinned to a tag, not a commit)
    return cfg.model_copy(update={"skills": await pin("skill", cfg.skills), "memory": await pin("agent", cfg.memory), "subagents": subs, "context_pack": pack})

async def _af_fresh(agent_id, hit):
    """GĐ13.7: a cached agent with PII middleware was built for the entities the gateway (9.8) masked at that time; the workspace setting moved -> rebuild (no double / missing masking)."""
    if not any(m.name == "PII" for m in hit[1].middleware): return True
    ws = (await q("SELECT workspace_id FROM agents WHERE id=%s", (agent_id,), one=True) or {}).get("workspace_id") or 1
    return S.get("af_sig", {}).get(id(hit[0])) == sorted((await AF.pii_state(ws))[1])

async def build_agent(agent_id, version, overrides=None):
    """overrides: cấu hình ghi đè theo run (assistant config / configurable) — chỉ các khoá trong OVERRIDABLE."""
    ov = {k: v for k, v in (overrides or {}).items() if k in OVERRIDABLE and v is not None}
    import run_trust as _rt
    trust = _rt.current()   # GĐ11.72: an external-source run (stranger, mail, public channel) gets its OWN agent instance: the trust level is part of the cache key
    key = (agent_id, version, json.dumps(ov, sort_keys=True, default=str)) if ov else (agent_id, version)
    mail = trust == _rt.EXTERNAL and _rt.is_mail_run()   # F-9: a mail-triggered run reads the owner's own mailbox / calendar without a pause (own agent instance; writes still wait)
    if trust == _rt.EXTERNAL: key += (_rt.EXTERNAL,) + (("mail",) if mail else ())
    _rp = __import__("engine_validate").current()   # GĐ13 12.35: an Engine "Test issue" replay gets its OWN agent instance (replay safety middleware, no approvals); it must run under the external-source rules
    if _rp is not None:
        if trust != _rt.EXTERNAL: raise HTTPException(500, "Engine replay must run under the external-source rules")
        key += ("engine_replay", tuple(sorted(_rp)))
    if key in S["agents"]:
        hit = S["agents"][key]; st = S.get("pack_stamp", {}).get(id(hit[0])); ls = S.get("legacy_stamp", {}).get(id(hit[0]))
        if (not st or await __import__("context_hub").is_current(st)) and (not ls or await __import__("context_hub").legacy_is_current(ls)) and await _af_fresh(agent_id, hit): return hit   # GĐ9.P: + skill/memory pins read through pack@prod   # GĐ9.1: a pinned pack tag may have moved since this build -> rebuild
        S["agents"].pop(key, None)
    row = await q("SELECT config FROM agent_versions WHERE agent_id=%s AND version=%s", (agent_id, version), one=True)
    if not row: raise HTTPException(404, "Không có agent/phiên bản")
    try: cfg = AgentCfg(**{**row["config"], **ov})
    except Exception as e: raise HTTPException(400, f"Cấu hình ghi đè không hợp lệ: {str(e)[:300]}")  # noqa: B904
    cfg = _rt.effective(cfg, trust, mail)   # GĐ11.72: external = own per-thread box, no Internet, approve_shell, no sub-agents, every non-read tool waits for approval
    if _rp is not None: cfg = cfg.model_copy(update={"rpm_limit": None})   # GĐ13 12.35: a replay never eats the per-minute budget real users of the agent share (it is bounded by max 5 parallel + the Engine caps)
    cfg = await _resolve_sub_refs(cfg)
    cfg = with_file_skills(cfg, (await q("SELECT workspace_id FROM agents WHERE id=%s", (agent_id,), one=True) or {}).get("workspace_id"))   # GĐ8.6
    skill_files, skill_info = await _materialize(agent_id, cfg)
    legacy_stamp = [i.pop("_stamp") for i in skill_info if "_stamp" in i]
    pack = await __import__("context_hub").mount(cfg.context_pack, (await q("SELECT workspace_id FROM agents WHERE id=%s", (agent_id,), one=True) or {}).get("workspace_id")) if cfg.context_pack else None   # GĐ9.1 read-only /pack/
    ws_id = (await q("SELECT workspace_id FROM agents WHERE id=%s", (agent_id,), one=True) or {}).get("workspace_id") or 1
    if (cfg.response_format or cfg.middleware) and int(ws_id) in gw.DEMO_WS: cfg = cfg.model_copy(update={"response_format": None, "middleware": []})   # GĐ13.7: never in the demo workspace (also for an agent copied into it)
    allowed = await enabled_tool_keys(ws_id); _mr = __import__("mcp_registry"); _mn = _mr.model_names(ws_id, agent_id)   # GĐ11.23 MCP registry per workspace (+ agent-private servers)
    pool = {**{k: guard(t, k.split(":")[0], _mn.get(k)) for k, t in _mr.tools_for(ws_id, agent_id).items() if allowed.get(k)}, **local_tools(agent_id), **__import__("assistant").tool_pool(), **__import__("chat_general").tool_pool(), **__import__("deep_research").tools(), **(await __import__("workspace_tools").pool(agent_id))}   # GĐ8.5 google:*/ms365:* (per workspace); GĐ7.7: propose_plan/think_tool; GĐ7.1: aos:<tool> = Agent OS tools (only used when the config lists them)
    pick = lambda keys: [pool[k] for k in keys if k in pool]
    tools = pick(cfg.tools)
    _sk = (await q("SELECT system_kind FROM agents WHERE id=%s", (agent_id,), one=True) or {}).get("system_kind")
    if _sk == "general_chat":   # GĐ11.38 "Trò chuyện chung": core tools + find_tools; every other tool of the workspace is deferred behind find_tools (demo: Agent OS read tools only)
        _gd = bool((await q("SELECT demo FROM workspaces WHERE id=%s", (ws_id,), one=True) or {}).get("demo"))
        cfg, tools, _ftm = __import__("chat_general").assemble(cfg, pool, [k for k in _mr.tools_for(ws_id, agent_id) if allowed.get(k)], _gd)
    else: tools, _ftm = __import__("tool_catalog").apply(tools, [k for k in cfg.tools if k in pool])   # GĐ11.31 > AOS_MCP_MAX_TOOLS MCP tools: only find_tools + core tools are shown to the model
    if cfg.long_term_memory:
        tools += [pool["local:luu_ghi_nho"], pool["local:tim_ghi_nho"]]
    if cfg.sandbox:
        tools.append(pool["local:chay_code_python"])
    S.setdefault("budgets", {})[agent_id] = cfg.budget_usd_day
    hide = set() if cfg.planning else {"write_todos"}
    if not cfg.filesystem and not (cfg.computer and cfg.computer.on): hide |= set(_ALL_FS_TOOL_NAMES) - ({"read_file", "ls", "glob", "grep"} if skill_files or pack else set()) - ({"read_file", "ls", "glob", "grep", "write_file", "edit_file"} if cfg.long_term_memory else set())
    sys_prompt = cfg.system_prompt
    if cfg.prompt_ref:
        import studio
        pv = await studio.resolve_prompt(cfg.prompt_ref)
        if pv: sys_prompt = await _ext().render_async(pv["template"], pv["variables"] or {}, pv.get("template_format")) + (("\n\n" + cfg.system_prompt) if cfg.system_prompt else "")
    sys_prompt = (sys_prompt or "") + __import__("genui").prompt_for([k for k in cfg.tools if k in pool]) if "local:present" in cfg.tools else sys_prompt   # GĐ3.1
    # HITL: tool người dùng chọn + mọi tool MCP không phải tool đọc HA → cần duyệt trước khi chạy
    gate = {k.split(":", 1)[-1] for k in cfg.interrupt_on}
    gate |= {k.split(":", 1)[1] for k in cfg.tools if ":" in k and not k.startswith(("local:", "aos:", "google:", "ms365:")) and not is_ha_read(k.split(":")[0], k.split(":", 1)[1])}   # aos:* = own tools; only cfg.interrupt_on gates them
    gate |= {"skill_save", "skill_share"}   # GĐ12.87: writing a Skill always waits for approval
    gate |= {"lich_tao", "lich_sua"}   # GĐ13.28: creating / editing a schedule always waits for approval
    names = {t.name for t in tools} | {t.name for s in cfg.subagents for t in pick(s.tools)}
    interrupt_on = {n: {"allowed_decisions": ["approve", "edit", "reject"]} for n in gate if n in names} or None
    interrupt_on = {**(interrupt_on or {}), **__import__("agent_schedules").interrupt_cfg(names)} or None   # GĐ13.28: same gate + "next 3 run times" text on the card
    _wt = __import__("workspace_tools"); _office = [n for n in names if n in _wt.BY_NAME]   # GĐ11.55: Google/M365 tools — ★ always ask, ☆ ask unless the admin allowed it (by arguments too); reads never
    _oc = _wt.interrupt_config(_office, {k.split(":", 1)[-1] for k in cfg.interrupt_on}, ws_id)
    if _oc: interrupt_on = {**(interrupt_on or {}), **_oc}
    _om = [_wt.OfficeMiddleware(agent_id, ws_id)] if _office else []
    mem_routes = __import__("memory_routes").routes(agent_id) if cfg.long_term_memory else {}   # GĐ11.70 /memories/user/ + /memories/agent/
    sk_src = "/memories/skills/"
    sk = await _computer_skills(agent_id, ws_id, cfg)   # GĐ11.64: the agent's skills as one tree, read-only at /memories/skills (file route + bind mount in the box)
    cw = __import__("sbx_backend").wire(agent_id, ws_id, cfg.computer, pack, extra_routes=mem_routes, skills=sk) if cfg.computer and cfg.computer.on else None   # GĐ11.41: box = file system + execute (lazy: no box until a file/shell tool is used)
    if cw and cw["interrupt"]: interrupt_on = {**(interrupt_on or {}), **cw["interrupt"]}   # approve_shell: every `execute` waits for a human decision (also in sub-agents)
    _rpm = []
    if _rp is not None:   # GĐ13 12.35: replay = the safety middleware refuses every tool that would act outside the test, and what would have waited for an approval is rejected instead (no interrupt, no Inbox row)
        _rpm = [__import__("engine_validate").EngineReplaySafetyMiddleware(_rp, rejected=set(interrupt_on or {}))]; interrupt_on = None
    await gw.prepare(cfg.model); model = gw._model(cfg.model, cfg.temperature)   # GĐ11.3: missing provider key -> ProviderKeyMissing (HTTP 424)
    _afm, _afsig = await AF.build(cfg, ws_id, model) if (cfg.middleware or cfg.response_format) else ([], None)   # GĐ13.7 allow-listed middleware (outside the gateway: a retry goes through its caps / fallback again)
    import mocks
    import attachments   # G4: local image URLs -> base64 standard blocks for the model request (state keeps the URL block)
    import deep_research as _dr
    _is_dr = cfg.name == _dr.NAME   # GĐ7.7: the hidden research agent gets its plan guard + run limits (ToolCallLimit/ModelCallLimit middleware)
    mw = lambda: [attachments.LocalImageMiddleware(), __import__("agent_clock").DateTimeMiddleware(), gw.GatewayMiddleware(agent_id, cfg.model, cfg.fallback_models, cfg.temperature, cfg.rpm_limit, budget_check), mocks.AuditHooks(agent_id), __import__("agent_identity").IdentityMiddleware(agent_id)] + _om + ([ToolFilter(hide)] if hide else []) + ([_ftm] if _ftm else []) + (_dr.main_middleware() if _is_dr else [])
    def smw(m): return [attachments.LocalImageMiddleware(), __import__("agent_clock").DateTimeMiddleware(), gw.GatewayMiddleware(agent_id, m, cfg.fallback_models, cfg.temperature, cfg.rpm_limit, budget_check), mocks.AuditHooks(agent_id), __import__("agent_identity").IdentityMiddleware(agent_id)] + _om + ([ToolFilter(hide)] if hide else []) + (_dr.sub_middleware() if _is_dr else [])
    subs = []
    for s in cfg.subagents:
        d = {"name": s.name, "description": s.description, "system_prompt": s.system_prompt or f"You are {s.name}.", "tools": pick(s.tools),
             "middleware": smw(s.model or cfg.model)}
        if s.model: await gw.prepare(s.model); d["model"] = gw._model(s.model, cfg.temperature)
        if s.skills: d["skills"] = [f"/skills/sub/{s.name}/"]
        if s.memory:
            from deepagents.middleware.memory import MemoryMiddleware
            from deepagents.backends import StateBackend
            d["middleware"] = d["middleware"] + [MemoryMiddleware(backend=StateBackend(), sources=[f"/memory/{n.split('@')[0]}/AGENTS.md" for n in s.memory])]
        if interrupt_on: d["interrupt_on"] = interrupt_on
        subs.append(d)
    from deepagents import FilesystemPermission
    perms = cw["permissions"] if cw else [FilesystemPermission(operations=["write"], paths=["/skills/**", "/memory/**"] + (["/pack/**"] if pack else []), mode="deny")] if skill_files or pack else None
    if mem_routes and not cfg.filesystem and not cw: perms = [FilesystemPermission(operations=["write"], paths=["/memories/**"], mode="allow"), FilesystemPermission(operations=["write"], paths=["/**"], mode="deny")] + (perms or [])   # memory-only file tools: writes only inside /memories/ (first matching rule wins)
    pk = {}
    if pack:   # GĐ9.1: composite FS = state files + the pack mounted read-only at /pack/ (PackBackend); AGENTS.md reloaded every run so a moved tag shows at once
        from deepagents.backends import CompositeBackend, StateBackend
        ch = __import__("context_hub")
        comp = cw["backend"] if cw else CompositeBackend(default=StateBackend(), routes={"/pack/": pack.backend, **mem_routes})   # GĐ11.41: with a computer the box is the default, /pack/ /skills/ /memory/ stay routes
        pk = {"backend": comp, "skills": (([sk_src] if sk else ["/skills/main/"]) if cfg.skills else []) + (["/pack/skills/"] if pack.has_skills else []) or None}
        _msrc = [f"/memory/{n.split('@')[0]}/AGENTS.md" for n in cfg.memory] + (["/pack/AGENTS.md"] if pack.has_agents else [])   # old memory contexts + the pack's AGENTS.md, one middleware
        pk["pack_mw"] = [ch.FreshMemoryMiddleware(backend=comp, sources=_msrc)] if _msrc else []
        sys_prompt = (sys_prompt or "") + ch.prompt_note(pack)
    if mem_routes: sys_prompt = (sys_prompt or "") + __import__("memory_routes").PROMPT_NOTE
    if trust == _rt.EXTERNAL: sys_prompt = (sys_prompt or "") + _rt.PROMPT_NOTE
    import chat_settings as _cs
    _mw_main = _rpm + [_cs.UserInstructionsMiddleware(), __import__("chat_projects").ProjectMiddleware()] + ([__import__("assistant").PageContextMiddleware()] if any(k.startswith("aos:") for k in cfg.tools) else []) + _afm + mw() + [__import__("chat_general").CallerNameMiddleware(agent_id, _sk == "general_chat")] + ([mocks.DelegationGuard(agent_id, [x["name"] for x in subs])] if cfg.delegation_guard and subs else []) + ([_rt.ExternalGuard()] if trust == _rt.EXTERNAL else [])   # GĐ11.72: wraps outside text as data + no `task`
    ag = create_deep_agent(model=model, tools=tools, system_prompt=sys_prompt or None, subagents=subs or None, interrupt_on=interrupt_on, response_format=cfg.response_format or None, context_schema=__import__("chat_modes").RunContext,
                           middleware=_mw_main + pk.get("pack_mw", []) + (cw["middleware"] if cw else []) + ([TodoListMiddleware()] if cfg.planning else []), checkpointer=S["cp"],
                           store=S["store"] if cfg.long_term_memory else None, name=f"agent_{agent_id}",
                           skills=pk["skills"] if pack else (([sk_src] if sk else ["/skills/main/"]) if cfg.skills else None),
                           memory=None if pack else ([f"/memory/{n.split('@')[0]}/AGENTS.md" for n in cfg.memory] or None), permissions=perms,
                           **({"backend": pk["backend"]} if pack else {"backend": cw["backend"]} if cw else {"backend": __import__("deepagents.backends", fromlist=["x"]).CompositeBackend(default=__import__("deepagents.backends", fromlist=["x"]).StateBackend(), routes=mem_routes)} if mem_routes else {}))
    S.setdefault("skill_files", {})[id(ag)] = (skill_files, skill_info)
    S.setdefault("af_sig", {})[id(ag)] = _afsig
    S.setdefault("pack_stamp", {}).pop(id(ag), None); S.setdefault("legacy_stamp", {}).pop(id(ag), None)
    if legacy_stamp: S["legacy_stamp"][id(ag)] = legacy_stamp
    if pack: S["pack_stamp"][id(ag)] = pack.stamp   # GĐ9.1: (workspace, "pack@tag", commit) the cache check compares on every run
    if len(S["agents"]) >= 8: S["agents"].pop(next(iter(S["agents"])))   # giới hạn RAM
    S["agents"][key] = (ag, cfg)
    return ag, cfg

async def save_version(agent_id, cfg: AgentCfg, note=None):
    AF.refuse_demo(cfg)   # GĐ13.7: response_format / middleware are not available in the demo workspace (server side)
    cfg = await pin_refs(agent_id, cfg)
    v = (await q("SELECT coalesce(max(version),0)+1 v FROM agent_versions WHERE agent_id=%s", (agent_id,), one=True))["v"]
    await q("INSERT INTO agent_versions(agent_id,version,config,note) VALUES(%s,%s,%s,%s)",
            (agent_id, v, Jsonb(cfg.model_dump(exclude={"note"})), note or cfg.note))
    await q("UPDATE agents SET current_version=%s, name=%s WHERE id=%s", (v, cfg.name, agent_id))
    return v

async def create_agent(cfg: AgentCfg, note="tạo mới"):
    import admin
    aid = (await q("INSERT INTO agents(name, workspace_id) VALUES(%s, %s) RETURNING id", (cfg.name, admin.cur_ws()), one=True))["id"]
    try: v = await save_version(aid, cfg, note)
    except Exception:
        await q("DELETE FROM agents WHERE id=%s", (aid,)); raise   # không để lại agent mồ côi khi cấu hình bị từ chối
    return aid, v

SEED = [
    AgentCfg(name="Trợ lý nhà", description="Đọc trạng thái nhà qua Home Assistant (chỉ đọc), có sub-agent Báo cáo.",
             system_prompt="Bạn là trợ lý nhà Moza. Trả lời tiếng Việt, ngắn gọn. Chỉ ĐỌC trạng thái Home Assistant, không điều khiển thiết bị. "
                           "Khi người dùng yêu cầu báo cáo / tổng hợp nhiều phòng: hãy lập kế hoạch bằng write_todos rồi giao cho sub-agent 'bao-cao'.",
             tools=["ha:homeassistant__GetLiveContext", "ha:llm__GetDateTime"], planning=True, filesystem=False,
             subagents=[SubAgentCfg(name="bao-cao", description="Lập báo cáo ngắn gọn về tình trạng nhà (nhiệt độ, độ ẩm, đèn, thiết bị) từ dữ liệu Home Assistant.",
                                    system_prompt="Bạn là sub-agent Báo cáo. Gọi GetLiveContext để lấy dữ liệu rồi viết báo cáo tiếng Việt dạng gạch đầu dòng, tối đa 8 dòng. Chỉ đọc.",
                                    tools=["ha:homeassistant__GetLiveContext", "ha:llm__GetDateTime"])]),
    AgentCfg(name="Tra cứu kiến thức", description="Trả lời dựa trên cơ sở kiến thức nội bộ (pgvector).",
             system_prompt="Bạn trả lời câu hỏi bằng tiếng Việt dựa trên cơ sở kiến thức. Luôn gọi tra_cuu_kien_thuc trước, trích nguồn [tiêu đề]. Nếu không thấy thì nói không biết.",
             tools=["local:tra_cuu_kien_thuc"], planning=False, filesystem=False),
]

async def refresh_mcp():
    await __import__("mcp_registry").refresh()   # GĐ11.23: per-workspace registry (all workspaces at boot; a caller's own workspace otherwise)
    # Lấy tên thiết bị khoá/báo động/gara từ HA để chặn chính xác hơn
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as h:
            st = (await h.get(os.environ["HA_URL"] + "/api/states", headers={"Authorization": "Bearer " + os.environ["HA_TOKEN"]})).json()
        S["block_names"] = {x["attributes"].get("friendly_name", "") for x in st if x["entity_id"].split(".")[0] in ("lock", "alarm_control_panel")
                            or x["attributes"].get("device_class") in ("garage", "garage_door", "gate", "lock")}
        S["block_names"] |= {x["entity_id"] for x in st if x["entity_id"].split(".")[0] in ("lock", "alarm_control_panel")}
    except Exception:  # noqa: BLE001
        pass
    S["agents"].clear()

@asynccontextmanager
async def lifespan(app):
    uri = os.environ["PG_URI"]   # GĐ9.7: serde= bên dưới = checkpoint mã hoá theo workspace (tuỳ chọn, aos_crypto.AosCipher)
    # GĐ5a: api + worker restart together and both run the boot DDL below (setup, CREATE POLICY, GRANT ...). Concurrent catalog
    # updates fail with "tuple concurrently updated" -> serialise the whole boot DDL with a session advisory lock.
    import psycopg as _pg
    _boot = await _pg.AsyncConnection.connect(uri, autocommit=True)
    await _boot.execute("SELECT pg_advisory_lock(hashtext('agentos_boot_ddl'))")
    async with __import__("srv_parity").open_checkpointer(uri, __import__("aos_crypto").make_serde()) as cp, \
               AsyncPostgresStore.from_conn_string(uri, index={"dims": DIMS, "embed": aembed, "fields": ["text"]}) as st, \
               AsyncConnectionPool(uri, min_size=1, max_size=6, kwargs={"autocommit": True, "row_factory": dict_row}) as pool, \
               __import__("aos_tools").mcp_lifespan(), __import__("agent_mcp").mcp_lifespan(), __import__("ext").mcp_lifespan():   # GĐ9.6 /mcp; GĐ7.0: /mcp/aos session manager; GĐ8.8 /mcp/agents/{id}
        await __import__("srv_parity").setup_saver(cp); await st.setup()   # GĐ9.Q: checkpointer is pluggable (postgres default | sqlite | memory | custom), see srv_parity
        S.update(cp=cp, store=st, pool=pool)
        await q(DDL); await q(gw.CAPS_DDL)   # CAPS_DDL (idempotent) before the first seed_spend: it reads spend_caps (GĐ6.2 fix: no "relation spend_caps does not exist" on first boot)
        await seed_spend()
        import obs, evals
        import studio
        import admin, gw_api, server, mocks
        await obs.init(); await evals.init(); await studio.init(); await admin.init(); await gw_api.init(); await server.init(); await __import__("assistant").init(); await __import__("insights").init(); await __import__("engine").init()   # GĐ7.1: agents.hidden; GD7.3: insight_reports; GD7.4: issues
        import attachments; await attachments.init()   # G4: chat_files (self-hosted image attachments), before RLS
        import ws_secrets; await ws_secrets.init()   # GĐ6.5 kho bí mật workspace (trước RLS)
        import crypto_api; await crypto_api.init()   # GĐ9.7 crypto_deks / crypto_ws (before RLS)
        import parquet_export; await parquet_export.init()   # GĐ9.9 parquet_export_cfg (before RLS)
        import otel; await otel.init()   # GĐ9.10 otel_export_cfg (before RLS)
        import privacy; await privacy.init()   # GĐ9.8 pii_settings + privacy_requests (before RLS)
        import channels; await channels.init()   # GĐ8.1 channel_bindings / channel_threads (before RLS)
        import channel_zalo; await channel_zalo.init()   # GĐ8.3 channel_events + channel_threads.last_user_at (before RLS)
        import channel_zalo_personal; await channel_zalo_personal.init()   # GĐ8.3b zp_sends (send counter, before RLS)
        await mocks.init()      # mock CRM/Ads/Publish tables: DDL as the owner role, before RLS (was lazily, silently failing under aos_rls)
        import agent_sharing; await agent_sharing.init()   # GĐ8.8 agents.visibility/owner_id + agent_shares (before RLS)
        import agent_clock; await agent_clock.init()   # GĐ9.P1 workspaces.timezone
        import context_hub; await context_hub.init()   # GĐ9.1 context_packs / blobs / commits / tags (before RLS)
        import gd9q; await gd9q.init()   # GĐ9.Q tables (server config / cache / auth / agent identity / gateway policies), before RLS
        import migrations; [print("[migrations]", l, flush=True) for l in await migrations.run_pending(_boot)]   # GĐ10.29 numbered migrations (inside the boot lock, before RLS so new tables get policies)
        await admin.rls_init(); S["rls"] = admin.rls_ctx
        await q("INSERT INTO mcp_servers(name,url,token_env) VALUES('ha',%s,'HA_TOKEN') ON CONFLICT DO NOTHING", (os.environ["HA_URL"] + "/api/mcp",))   # GĐ11.23: after migration 0020 + RLS (workspace 1)
        await refresh_mcp()
        await agent_sharing.policies()   # GĐ8.8 private agents: visibility-aware RLS on agents / lg_assistants
        # seed only after every module created its columns (agents.workspace_id comes from admin.init) -> fresh DB boots
        if not (await q("SELECT 1 FROM agents LIMIT 1")):
            for a in SEED: await create_agent(a, "seed")
        import ext; await ext.init()
        import ui_api; await ui_api.init()
        await context_hub.migrate()   # GĐ9.1 legacy contexts -> packs (idempotent; needs `contexts` from ui_api.init)
        import demo_ws; await demo_ws.init()   # workspaces.demo + gateway.DEMO_WS (demo = own workspace, Gemini only)
        import sandbox2; await sandbox2.init()
        await gd9q.init_late()   # GĐ9.Q: columns on sandbox tables created by ext / admin
        import oauth_tools; await oauth_tools.init()
        import workspace_tools; await workspace_tools.init()   # GĐ8.5 workspaces.gw_tools
        await admin.bootstrap_owner()
        await _boot.close()   # releases the boot DDL lock
        S["worker"] = asyncio.create_task(obs.worker()) if os.environ.get("AOS_ROLE") != "worker" else asyncio.create_task(asyncio.sleep(0))
        S["queue"] = asyncio.create_task(server.queue_worker())
        S["spend"] = asyncio.create_task(spend_sync_loop())
        S["tg"] = __import__("channel_telegram").start()   # GĐ8.2 Telegram pollers (in every queue process; advisory lock = one per binding)
        S["slack"] = __import__("channel_slack").start()   # GĐ8.4 Slack Socket Mode connections (same pattern; advisory lock = one per app)
        S["zp"] = __import__("channel_zalo_personal").start()   # GĐ8.3b Zalo personal sidecar supervisor (only when AOS_ZALO_PERSONAL=1, API process)
        S["mail"] = __import__("mail_triggers").start()   # GĐ11.60 mail-trigger pollers (Gmail / Outlook; advisory lock = one poller per binding)
        yield
        try: await server.shutdown()   # GĐ13.30: stop claiming, let in-flight runs finish (AOS_BG_SHUTDOWN_GRACE_SECS), release the rest to `pending`
        except Exception as _e: print("shutdown", type(_e).__name__, str(_e)[:200])  # noqa: BLE001
        S["worker"].cancel(); S["queue"].cancel(); S["tg"].cancel(); S["slack"].cancel(); S["zp"].cancel(); S["mail"].cancel()

app = FastAPI(title="Moza Agent OS", lifespan=lifespan)
@app.get("/api/gateway/caps")
async def gateway_caps():
    await seed_spend()
    import gw_api
    import gw_policy_engine
    return {**{p: {"cap_usd": c, "spent_usd": round(gw.SPENT[p], 6)} for p, c in gw.CAPS.items()}, "caps": await gw_api.caps_list(), "tz": gw_policy_engine.TZ_NAME, "tz_note": gw_policy_engine.TZ_NOTE}   # GĐ11.14: periods in ICT

def _text(c):
    if isinstance(c, str): return c
    return " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type", "text") == "text")

# ---------------- Chat + trace ----------------
class Chat(BaseModel):
    thread_id: str = Field(max_length=100)
    message: str = Field(max_length=8000)
    agent_id: int | None = None
    stream_tokens: bool = True
    overrides: dict | None = None     # ghi đè cấu hình theo run: model, temperature, system_prompt, tools…

async def run_agent(agent_id, version, *args, **kw):
    """Bọc: kiểm tra agent thuộc workspace hiện tại (RLS); tác vụ nền chạy trong workspace của agent (trace/ghi đúng chỗ)."""
    import admin as _adm
    own = await q("SELECT workspace_id FROM agents WHERE id=%s", (agent_id,), one=True)
    if not own: raise HTTPException(404, "Agent không tồn tại")
    tok = _adm.WS_CTX.set(own["workspace_id"]) if not _adm.USER.get() else None
    try:
        async for ev in _run_agent(agent_id, version, *args, **kw): yield ev
    finally:
        if tok:
            try: _adm.WS_CTX.reset(tok)
            except ValueError: pass

async def _run_agent(agent_id, version, thread_id, message, test_run_id=None, test_case_id=None, deployment=None, experiment_id=None, project="agents",
                    resume=None, checkpoint_id=None, step=False, tags=None, overrides=None, tokens=False, msg_meta=None, sub_tokens=False, thread_agent=None, native=(), cmd=None):
    """Generator: phát các bước (trace) rồi sự kiện 'done'. Ghi run vào eval_runs.
    resume: quyết định HITL (Command(resume=...)); checkpoint_id: chạy tiếp/rẽ nhánh từ checkpoint (time-travel); step: dừng trước mỗi lượt gọi tool."""
    from langgraph.types import Command
    ag, cfg = await build_agent(agent_id, version, overrides)
    kid = thread_agent or agent_id   # GĐ7.7: deep research runs a hidden agent on the chat thread's own checkpoint key (a<thread agent>:<tid>)
    tcfg = {"configurable": {"thread_id": f"a{kid}:{thread_id}"}, "recursion_limit": 60}
    if checkpoint_id: tcfg["configurable"]["checkpoint_id"] = checkpoint_id
    tcfg["configurable"].update(__import__("caller").configurable())   # GĐ9.Q: allow-listed request headers + langgraph_auth_user for tools
    __import__("engine_validate").mark(tcfg)   # GĐ13 12.35: an Engine replay says so in configurable (+ X-AOS-Source: engine) and gets a short recursion limit
    if isinstance(resume, dict) and isinstance(resume.get("decisions"), list):
        # Parallel sub-agents can raise several HITL interrupts at once; LangGraph then needs
        # Command(resume={interrupt_id: value}). Split the flat decision list per interrupt (official API).
        try:
            _ints = list((await ag.aget_state(tcfg)).interrupts or [])
            if len(_ints) > 1:
                _ds, _m, _k = resume["decisions"], {}, 0
                for _it in _ints:
                    _n = len((_it.value or {}).get("action_requests") or []) or 1
                    _m[_it.id] = {"decisions": _ds[_k:_k + _n]}; _k += _n
                resume = _m
        except Exception as _e:  # noqa: BLE001
            print("multi-interrupt split failed", _e)
    inp = Command(resume=resume) if resume is not None else ({"messages": [{"role": "user", "content": message, **(msg_meta or {})}]} if message else None)
    if cmd and (cmd.get("update") is not None or cmd.get("goto") is not None):   # GĐ9.P1 Agent Server `command.update` / `command.goto` (LangGraph Command semantics)
        from langgraph.types import Send
        gt_ = cmd.get("goto"); gt_ = [gt_] if isinstance(gt_, (str, dict)) else list(gt_ or [])
        inp = Command(resume=resume, update=cmd.get("update"), goto=[Send(g["node"], g.get("input")) if isinstance(g, dict) else g for g in gt_])
    if isinstance(message, list):   # G4: standard content blocks (text + image); logs/traces keep a text summary
        _n_img = sum(1 for b in message if isinstance(b, dict) and b.get("type") == "image")
        message = (_text(message) + (f" [{_n_img} image(s)]" if _n_img else "")).strip()
    sf = S.get("skill_files", {}).get(id(ag))
    if sf and sf[0] and isinstance(inp, dict):
        from deepagents.backends.utils import create_file_data
        inp["files"] = {k: create_file_data(v) for k, v in sf[0].items()}
        inp["skills_metadata"] = None      # nạp lại danh sách skill mỗi lượt (bản đã ghim theo phiên bản agent)
    elif isinstance(inp, dict) and id(ag) in S.get("pack_stamp", {}):
        inp["skills_metadata"] = None      # GĐ9.1: context pack skills are re-read every turn (the pinned tag may have moved since this thread's last turn)
    xkw = {"interrupt_before": ["tools"]} if step else {}
    _rc = __import__("chat_modes").RUN_CTX.get()
    if _rc is not None: xkw["context"] = _rc   # chat mode of this run (LangChain runtime context); None = nothing passed, the agent behaves as before
    xkw.update(__import__("durable").stream_kw())   # GĐ13.30: queue runs write checkpoints synchronously (the AIMessage with a write tool call is saved BEFORE the tool runs)
    t0 = time.perf_counter(); seen, trace, tools_log, pend = set(), [], [], {}
    it = ot = tt = 0; answer, err = "", None; cst = 0.0; used = []; first_tok = None   # first_tok = ms to the first streamed token (GĐ12.15 time to first token)
    spans = []; gw.SPANS.set(spans); gw.T0.set(t0); gw.PARENT.set(None); t_wall = time.time()
    try:
        async for ns, mode, upd in ag.astream(inp, config=tcfg, stream_mode=(["updates", "messages"] if tokens else ["updates"]) + list(native), subgraphs=True, **xkw):
            depth = len(ns)
            if mode in ("custom", "tasks", "checkpoints", "debug"):   # GĐ9.6: LangGraph-native stream modes forwarded for the Agent Server API (server._execute)
                yield {"type": "native", "mode": mode, "data": upd, "ns": list(ns)}; continue
            if mode == "messages":       # token-level streaming (agent chính; sub-agent khi sub_tokens — G1 stream_subgraphs)
                ch, meta = upd
                if (depth == 0 or sub_tokens) and getattr(ch, "type", "") in ("AIMessageChunk", "ai") and not getattr(ch, "tool_call_chunks", None):
                    t = _text(ch.content)
                    if t and first_tok is None: first_tok = int((time.perf_counter() - t0) * 1000)
                    if t: yield {"type": "token", "text": t, "node": (meta or {}).get("langgraph_node"), "id": getattr(ch, "id", None), "ns": list(ns)}
                continue
            for node, val in (upd or {}).items():
                if node == "__interrupt__":
                    if depth: yield {"type": "sub_interrupt", "ns": list(ns), "interrupts": [{"value": getattr(i, "value", i), "id": getattr(i, "id", None)} for i in (val or [])]}
                    continue
                msgs = val.get("messages") if isinstance(val, dict) else None
                if msgs is not None and not isinstance(msgs, list): msgs = getattr(msgs, "value", None)
                for m in msgs or []:
                    mid = getattr(m, "id", None) or id(m)
                    if mid in seen: continue
                    seen.add(mid)
                    if isinstance(m, AIMessage):
                        u = m.usage_metadata or {}
                        it += u.get("input_tokens", 0); ot += u.get("output_tokens", 0); tt += u.get("total_tokens", 0)
                        mu = (m.response_metadata or {}).get("gateway_model") or cfg.model
                        cst += cost(mu, u.get("input_tokens", 0), u.get("output_tokens", 0), gw.details_from_usage(u)) or 0
                        if mu not in used: used.append(mu)
                        for tc in m.tool_calls or []:
                            kind = "todos" if tc["name"] == "write_todos" else "subagent" if tc["name"] == "task" else "tool"
                            ev = {"type": "step", "kind": kind, "depth": depth, "name": tc["name"], "args": tc.get("args"), "ns": list(ns), "node": node, "tool_call_id": tc.get("id")}
                            pend[tc.get("id")] = ev; trace.append(ev); yield ev
                        txt = _text(m.content)
                        if depth == 0 and not m.tool_calls: answer = txt
                        elif depth > 0 and txt and not m.tool_calls:
                            ev = {"type": "step", "kind": "sub_answer", "depth": depth, "text": txt[:1500], "ns": list(ns), "node": node}; trace.append(ev); yield ev
                    elif isinstance(m, ToolMessage):
                        res = _text(m.content)
                        ev = {"type": "step", "kind": "result", "depth": depth, "name": m.name, "text": res[:1500], "ns": list(ns), "node": node, "tool_call_id": m.tool_call_id}
                        trace.append(ev); yield ev
                        call = pend.get(m.tool_call_id)
                        if call and call["kind"] != "todos":
                            tools_log.append({"name": call["name"], "args": call["args"], "result": res[:2000], "depth": depth})
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {str(e)[:500]}"
    ms = int((time.perf_counter() - t0) * 1000)
    paused, pend_hitl, ckpt, sresp = False, [], None, None
    try:
        st = await ag.aget_state({"configurable": {"thread_id": f"a{kid}:{thread_id}"}})
        paused = bool(st.next); ckpt = st.config["configurable"].get("checkpoint_id")
        sresp = (st.values or {}).get("structured_response") if cfg.response_format else None   # GĐ13.7
        pend_hitl = [i.value for i in (st.interrupts or [])]
    except Exception as e:  # noqa: BLE001
        print("state error", e)
    message = message or ("(duyệt: " + json.dumps(resume, ensure_ascii=False, default=str)[:300] + ")" if resume is not None else "(tiếp tục từ checkpoint)")
    row = await q("INSERT INTO eval_runs(thread_id,model,user_input,answer,tool_calls,input_tokens,output_tokens,total_tokens,cost_usd,"
                  "latency_ms,error,agent_id,agent_version,trace,test_run_id,test_case_id,spans,models_used,deployment) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                  (thread_id, used[0] if len(used) == 1 else cfg.model, message, answer, Jsonb(tools_log), it, ot, tt, round(cst, 6), ms, err,
                   agent_id, version, Jsonb(trace), test_run_id, test_case_id, Jsonb(spans), used or None, deployment), one=True)
    trace_root = None; ptrace = None
    if resume is not None:
        try:
            ptrace = (await q("SELECT trace_id FROM approvals WHERE thread_id=%s AND trace_id IS NOT NULL ORDER BY id DESC LIMIT 1", (thread_id,), one=True) or {}).get("trace_id")
        except Exception:  # noqa: BLE001
            ptrace = None
    try:
        import obs
        aname = (await q("SELECT name FROM agents WHERE id=%s", (agent_id,), one=True) or {}).get("name", f"agent {agent_id}")
        trace_root = await obs.record_agent_trace(agent_name=aname, agent_id=agent_id, version=version, thread_id=thread_id, message=message, answer=answer,
                                                  error=err, t_start_wall=t_wall, latency_ms=ms, spans=spans, input_tokens=it, output_tokens=ot, cost=round(cst, 6),
                                                  models=used, eval_run_id=row["id"], deployment=deployment, test_run_id=test_run_id,
                                                  experiment_id=experiment_id, project=project, tags=tags, parent_trace=ptrace, first_token_ms=first_tok)
        await q("UPDATE eval_runs SET trace_root=%s WHERE id=%s", (trace_root, row["id"]))
    except Exception as e:  # noqa: BLE001
        print("trace record error", e)
    approval_id = None
    _ask = [v for v in pend_hitl if __import__("chat_gd5").is_ask(v) or __import__("agent_identity").is_auth_interrupt(v)]   # GĐ5a: an ask-for-info form is answered in chat, not an Inbox approval
    pend_hitl = [v for v in pend_hitl if v not in _ask]
    if _ask and not pend_hitl:
        yield {"type": "interrupt", "approval_id": None, "requests": json.loads(json.dumps(_ask, default=str))}
    if pend_hitl:
        approval_id = (await q("INSERT INTO approvals(agent_id,version,thread_id,payload,trace_id) VALUES(%s,%s,%s,%s,%s) RETURNING id",
                               (agent_id, version, thread_id, Jsonb(json.loads(json.dumps(pend_hitl, default=str))), trace_root), one=True))["id"]
        ev = {"type": "interrupt", "approval_id": approval_id, "requests": json.loads(json.dumps(pend_hitl, default=str))}; yield ev
    yield {"type": "done", "paused": paused, "approval_id": approval_id, "checkpoint_id": ckpt, "run_id": row["id"], "trace_id": trace_root, "reply": answer, "error": err, "tokens": tt, "cost_usd": round(cst, 6), "models_used": used,
           "latency_ms": ms, "agent_id": agent_id, "agent_version": version, **_structured(cfg, sresp)}

def _structured(cfg, sresp):
    """GĐ13.7: `structured_response` of a run (JSON-safe) + `structured_error` when it does not satisfy the schema (LangChain does not validate raw JSON-schema dicts)."""
    if sresp is None: return {}
    v = json.loads(json.dumps(sresp.model_dump() if hasattr(sresp, "model_dump") else sresp, ensure_ascii=False, default=str))
    err = AF.validate_structured(cfg.response_format, v)
    return {"structured_response": v, **({"structured_error": err} if err else {})}

async def default_agent():
    return (await q("SELECT id FROM agents WHERE NOT deleted ORDER BY id LIMIT 1", one=True))["id"]

async def cur_version(aid):
    r = await q("SELECT current_version FROM agents WHERE id=%s AND NOT deleted", (aid,), one=True)
    if not r: raise HTTPException(404, "Agent không tồn tại")
    return r["current_version"]

@app.post("/chat/stream")
async def chat_stream(c: Chat):
    aid = c.agent_id or await default_agent(); v = await cur_version(aid)
    async def gen():
        async for ev in run_agent(aid, v, c.thread_id, c.message, overrides=c.overrides, tokens=c.stream_tokens):
            yield json.dumps(ev, ensure_ascii=False, default=str) + "\n"
    return StreamingResponse(gen(), media_type="application/x-ndjson", headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

@app.post("/chat")
async def chat(c: Chat):
    aid = c.agent_id or await default_agent(); v = await cur_version(aid); last = None
    async for ev in run_agent(aid, v, c.thread_id, c.message, overrides=c.overrides): last = ev
    if last["error"]: raise HTTPException(502, f"Agent lỗi (run #{last['run_id']}): {last['error'][:200]}")
    return last

# ---------------- Agents ----------------
@app.get("/api/agents")
async def list_agents():
    return await q("SELECT a.id, a.name, a.current_version, a.created, v.config->>'description' description, v.config->>'model' model,"
                   " (SELECT count(*) FROM agent_versions x WHERE x.agent_id=a.id) n_versions"
                   " FROM agents a JOIN agent_versions v ON v.agent_id=a.id AND v.version=a.current_version WHERE NOT a.deleted AND NOT a.hidden AND a.workspace_id=%s ORDER BY (a.system_kind IS NULL), a.id", (_cws(),))   # GĐ11.38: "Trò chuyện chung" first

def _cws():
    import admin; return admin.cur_ws()

async def _visible(aid):
    """The built-in assistant (agents.hidden) cannot be edited, rolled back or deleted through the agent API."""
    if await q("SELECT 1 FROM agents WHERE id=%s AND (hidden OR system_kind IS NOT NULL)", (aid,), one=True): raise HTTPException(403, "Agent hệ thống, không sửa được / built-in agent is read-only")
    await __import__("agent_sharing").require_edit(aid)   # GĐ8.8: share role 'use' cannot change the config; private agent unshared -> 404

@app.get("/api/agents/{aid}")
async def get_agent(aid: int):
    a = await q("SELECT * FROM agents WHERE id=%s AND NOT deleted AND NOT hidden AND workspace_id=%s", (aid, _cws()), one=True)   # hidden = built-in assistant (GĐ7.1): not exposed/editable
    if not a: raise HTTPException(404)
    vs = await q("SELECT version, note, created, config FROM agent_versions WHERE agent_id=%s ORDER BY version DESC", (aid,))
    return {"agent": a, "config": next(v["config"] for v in vs if v["version"] == a["current_version"]), "versions": vs}

@app.post("/api/agents")
async def new_agent(cfg: AgentCfg):
    aid, v = await create_agent(cfg); return {"id": aid, "version": v}

@app.put("/api/agents/{aid}")
async def upd_agent(aid: int, cfg: AgentCfg):
    await _visible(aid); await cur_version(aid); return {"id": aid, "version": await save_version(aid, cfg)}

@app.post("/api/agents/{aid}/clone")
async def clone_agent(aid: int):
    cfg = AgentCfg(**(await get_agent(aid))["config"]); cfg.name += " (bản sao)"
    nid, v = await create_agent(cfg, f"nhân bản từ agent #{aid}"); return {"id": nid, "version": v}

@app.post("/api/agents/{aid}/rollback/{ver}")
async def rollback(aid: int, ver: int):
    await _visible(aid)
    r = await q("SELECT config FROM agent_versions WHERE agent_id=%s AND version=%s", (aid, ver), one=True)
    if not r: raise HTTPException(404)
    return {"version": await save_version(aid, AgentCfg(**r["config"]), f"rollback về v{ver}")}

@app.delete("/api/agents/{aid}")
async def del_agent(aid: int, mode: str = "archive", confirm: str | None = None, purge: bool = False):
    """GĐ11.34: ?mode=archive (default, soft delete, restorable) | permanent (+ &confirm=<exact name>; owner / admin only) - see app/agent_purge.py. `purge=true` = permanent."""
    import agent_purge; return await agent_purge.delete_agent(aid, mode, confirm, purge)

@app.get("/api/agents/{aid}/export")
async def export_agent(aid: int):
    d = await get_agent(aid); return {"format": "moza-agent/1", "config": d["config"]}

class ImportReq(BaseModel):
    format: str = "moza-agent/1"
    config: AgentCfg

@app.post("/api/agents/import")
async def import_agent(r: ImportReq):
    aid, v = await create_agent(r.config, "nhập từ JSON"); return {"id": aid, "version": v}

_models_cache = {}
@app.get("/api/models")
async def models():
    if not _models_cache.get("t") or time.time() - _models_cache["t"] > 3600:
        try:
            ms = [m.name.split("/")[-1] async for m in await _g.aio.models.list()
                  if "generateContent" in (m.supported_actions or []) and m.name.split("/")[-1].startswith("gemini")]
        except Exception:  # noqa: BLE001
            ms = []
        _models_cache.update(t=time.time(), list=([m for m in gw.OPENAI_MODELS] if os.getenv("OPENAI_API_KEY") else []) + sorted(set(ms + [DEFAULT_MODEL]) - set(gw.OPENAI_MODELS)))
    return {"default": DEFAULT_MODEL, "models": _models_cache["list"]}

@app.get("/api/toolcatalog")
async def toolcatalog():
    allowed = await enabled_tool_keys()
    out = [{"key": k, "description": (t.description or "")[:200], "enabled": bool(allowed.get(k))} for k, t in __import__("mcp_registry").tools_for(__import__("mcp_registry").cur_ws()).items()]
    lt = local_tools(0)
    out += [{"key": k, "description": t.description, "enabled": True} for k, t in lt.items() if k in ("local:tra_cuu_kien_thuc", "local:web_search", "local:fetch_url", "local:oauth_request")]
    out += [{k: c[k] for k in ("key", "description", "enabled")} for c in await __import__("workspace_tools").catalog()]   # GĐ8.5

    return out

# ---------------- Tools / MCP: moved to app/mcp_registry.py (GĐ11.23, same paths) ----------------

# ---------------- Memory / Store / KB ----------------
@app.get("/api/store/namespaces")
async def namespaces():
    return [list(n) for n in await S["store"].alist_namespaces(limit=500)]

@app.get("/api/store/items")
async def store_items(ns: str, q_: str = "", limit: int = 50):
    res = await S["store"].asearch(tuple(ns.split("/")), query=q_ or None, limit=limit)
    return [{"key": i.key, "value": i.value, "score": None if i.score is None else round(i.score, 4), "updated": i.updated_at} for i in res]

class StorePut(BaseModel):
    ns: str
    key: str | None = None
    text: str

@app.post("/api/store/items")
async def store_put(p: StorePut):
    k = p.key or uuid.uuid4().hex[:12]; await S["store"].aput(tuple(p.ns.split("/")), k, {"text": p.text}); return {"key": k}

@app.delete("/api/store/items")
async def store_del(ns: str, key: str):
    await S["store"].adelete(tuple(ns.split("/")), key); return {"ok": True}

async def kb_add(title, text, kb="chung"):
    chunks = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=100).split_text(text)
    if not chunks: raise HTTPException(400, "Văn bản rỗng")
    embs = []
    for i in range(0, len(chunks), 50): embs += await aembed(chunks[i:i + 50])
    did = (await q("INSERT INTO kb_docs(kb,title,n_chunks) VALUES(%s,%s,%s) RETURNING id", (kb, title, len(chunks)), one=True))["id"]
    async with S["pool"].connection() as c:
        ctx = S["rls"]()
        await c.execute("SELECT set_config('app.ws',%s,false), set_config('app.uid',%s,false), set_config('app.role',%s,false), set_config('role', CASE WHEN %s ~ '^[0-9]+$' THEN 'aos_rls' ELSE 'none' END, false)", ctx + (ctx[0],))
        async with c.cursor() as cur:
            await cur.executemany("INSERT INTO kb_chunks(doc_id,idx,text,embedding) VALUES(%s,%s,%s,%s::vector)",
                                  [(did, i, ch, str(e)) for i, (ch, e) in enumerate(zip(chunks, embs))])
    return {"doc_id": did, "chunks": len(chunks)}

class KbText(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=500000)
    kb: str = "chung"

@app.post("/api/kb/text")
async def kb_text(k: KbText):
    return await kb_add(k.title, k.text, k.kb)

@app.post("/api/kb/upload")
async def kb_upload(file: UploadFile = File(...), kb: str = Form("chung")):
    if not file.filename.lower().endswith((".txt", ".md")): raise HTTPException(400, "Chỉ nhận .txt / .md")
    data = await file.read()
    if len(data) > 2_000_000: raise HTTPException(400, "File quá 2 MB")
    return await kb_add(file.filename, data.decode("utf-8", "replace"), kb)

@app.get("/api/kb/docs")
async def kb_docs():
    return await q("SELECT id,kb,title,n_chunks,created FROM kb_docs WHERE kind='kb' ORDER BY id DESC")

@app.delete("/api/kb/docs/{did}")
async def kb_del(did: int):
    await q("DELETE FROM kb_docs WHERE id=%s AND kind='kb'", (did,)); return {"ok": True}

@app.get("/api/kb/search")
async def kb_s(q_: str, k: int = 5):
    return await kb_search(q_, k)

# ---------------- Eval ----------------
class Score(BaseModel):
    run_id: int
    thumb: int | None = Field(default=None, ge=-1, le=1)
    score: int | None = Field(default=None, ge=1, le=5)
    comment: str | None = Field(default=None, max_length=2000)

@app.post("/score")
async def score(s: Score):
    await q("INSERT INTO eval_scores(run_id,kind,thumb,score,comment) VALUES(%s,'human',%s,%s,%s)", (s.run_id, s.thumb, s.score, s.comment))
    await _mirror_feedback(s.run_id, [("thumb", s.thumb), ("user_score", s.score)], s.comment, "human")
    return {"ok": True}

async def _mirror_feedback(eval_run_id, pairs, comment, source):
    """Đồng bộ điểm từ trang Chat/Đánh giá sang feedback của trace (P1/P2)."""
    import obs
    r = await q("SELECT trace_root FROM eval_runs WHERE id=%s", (eval_run_id,), one=True)
    if not r or not r["trace_root"]: return
    for k, v in pairs:
        if v is not None:
            await obs.add_feedback(obs.FbIn(run_id=str(r["trace_root"]), key=k, score=float(v), comment=comment), source=source)

async def gemini_json(prompt):
    try:
        txt, m, _, _ = await gw.generate(_g, prompt, [DEFAULT_MODEL] + gw.DEFAULT_FALLBACKS, json_mode=True)
        d = json.loads(txt); d["_model"] = m; return d
    except Exception as e:  # noqa: BLE001
        raise HTTPException(503, f"Gemini bận, thử lại sau: {str(e)[:120]}")

async def do_judge(run_id, expected=None):
    run = await q("SELECT * FROM eval_runs WHERE id=%s", (run_id,), one=True)
    if not run: raise HTTPException(404, "Không có run")
    prompt = ("Bạn là giám khảo đánh giá câu trả lời của một AI agent.\n"
              f"Câu hỏi: {run['user_input']}\n"
              f"Các tool đã gọi và kết quả (có thể bị cắt bớt; phần bị cắt không coi là bịa): {json.dumps(run['tool_calls'], ensure_ascii=False)[:8000]}\n"
              + (f"Đáp án mong đợi: {expected}\n" if expected else "")
              + f"Câu trả lời: {run['answer'] or '(trống)'}\n{'Lỗi: ' + run['error'] if run['error'] else ''}\n"
              "Chấm 1-5 (5 = đúng, đủ, ngắn gọn" + (", khớp đáp án mong đợi" if expected else ", khớp dữ liệu tool") + "). "
              'Trả về JSON: {"score": <1-5>, "reason": "<lý do ngắn, tiếng Việt>"}')
    d = await gemini_json(prompt)
    sc = max(1, min(5, int(d.get("score") or 1))); reason = str(d.get("reason", ""))[:1000]
    await q("INSERT INTO eval_scores(run_id,kind,score,comment) VALUES(%s,'ai',%s,%s)", (run_id, sc, reason + f" [giám khảo: {d.get('_model')}]"))
    await _mirror_feedback(run_id, [("ai_judge", sc)], reason, "llm")
    return {"score": sc, "reason": reason}

class Judge(BaseModel):
    run_id: int

@app.post("/judge")
async def judge(j: Judge):
    return await do_judge(j.run_id)

@app.get("/api/runs")
async def runs(q_: str = "", day: str = "", min_score: float | None = None, has_error: bool | None = None,
               agent_id: int | None = None, agent_version: int | None = None, limit: int = 200):
    w, a = ["1=1"], []
    if q_: w.append("(r.user_input ILIKE %s OR r.answer ILIKE %s OR r.thread_id ILIKE %s)"); a += [f"%{q_}%"] * 3
    if day: w.append("(r.ts AT TIME ZONE 'Asia/Ho_Chi_Minh')::date = %s"); a.append(day)
    if has_error is not None: w.append("r.error IS NOT NULL" if has_error else "r.error IS NULL")
    if agent_id: w.append("r.agent_id=%s"); a.append(agent_id)
    if agent_version: w.append("r.agent_version=%s"); a.append(agent_version)
    rows = await q("SELECT r.id, r.ts, r.thread_id, r.model, r.agent_id, r.agent_version, ag.name agent_name, left(r.user_input,120) user_input,"
                   " left(r.answer,160) answer, r.total_tokens, r.cost_usd, r.latency_ms, r.error, jsonb_array_length(coalesce(r.tool_calls,'[]')) n_tools,"
                   " (SELECT round(avg(score),2) FROM eval_scores s WHERE s.run_id=r.id AND s.kind='human') human_score,"
                   " (SELECT score FROM eval_scores s WHERE s.run_id=r.id AND s.kind='ai' ORDER BY ts DESC LIMIT 1) ai_score,"
                   " (SELECT sum(thumb) FROM eval_scores s WHERE s.run_id=r.id) thumbs"
                   f" FROM eval_runs r LEFT JOIN agents ag ON ag.id=r.agent_id WHERE {' AND '.join(w)} ORDER BY r.id DESC LIMIT %s",
                   (*a, min(limit, 1000)))
    if min_score is not None: rows = [x for x in rows if (x["human_score"] or x["ai_score"] or 0) >= min_score]
    return rows

@app.get("/api/runs/{rid}")
async def run_detail(rid: int):
    r = await q("SELECT * FROM eval_runs WHERE id=%s", (rid,), one=True)
    if not r: raise HTTPException(404)
    return {"run": r, "scores": await q("SELECT * FROM eval_scores WHERE run_id=%s ORDER BY ts", (rid,)), "pricing": PRICING.get(r["model"]),
            "pricing_source": PRICING_SOURCE}

@app.get("/api/stats")
async def stats(agent_id: int | None = None):
    f, a = ("WHERE agent_id=%s", (agent_id,)) if agent_id else ("", ())
    tot = await q("SELECT count(*) runs, coalesce(sum(total_tokens),0) tokens, coalesce(sum(cost_usd),0) cost,"
                  " count(*) FILTER (WHERE error IS NOT NULL) errors, round(avg(latency_ms)) avg_ms"
                  f" FROM eval_runs {f}", a, one=True)
    sc = await q("SELECT round(avg(score) FILTER (WHERE kind='human'),2) avg_human, round(avg(score) FILTER (WHERE kind='ai'),2) avg_ai,"
                 " count(*) FILTER (WHERE thumb=1) up, count(*) FILTER (WHERE thumb=-1) down FROM eval_scores s JOIN eval_runs r ON r.id=s.run_id "
                 + ("WHERE r.agent_id=%s" if agent_id else ""), a, one=True)
    days = await q("WITH d AS (SELECT (ts AT TIME ZONE 'Asia/Ho_Chi_Minh')::date AS day, count(*) runs, sum(input_tokens) in_tok,"
                   f" sum(output_tokens) out_tok, sum(total_tokens) tokens, sum(cost_usd) cost, round(avg(latency_ms)) avg_ms FROM eval_runs {f} GROUP BY 1),"
                   " sc AS (SELECT (r.ts AT TIME ZONE 'Asia/Ho_Chi_Minh')::date AS day, round(avg(s.score),2) avg_score"
                   " FROM eval_scores s JOIN eval_runs r ON r.id=s.run_id " + ("WHERE r.agent_id=%s" if agent_id else "") + " GROUP BY 1)"
                   " SELECT d.*, sc.avg_score FROM d LEFT JOIN sc USING(day) ORDER BY day DESC LIMIT 60", a + a)
    return {"total": {**tot, **sc}, "days": days, "pricing": PRICING.get(DEFAULT_MODEL), "pricing_source": PRICING_SOURCE, "model": DEFAULT_MODEL}

class TestSet(BaseModel):
    name: str = Field(min_length=1, max_length=120)

class TestCase(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    expected: str = Field(default="", max_length=4000)

@app.get("/api/testsets")
async def testsets():
    return await q("SELECT s.*, (SELECT count(*) FROM test_cases c WHERE c.set_id=s.id) n FROM test_sets s ORDER BY id DESC")

@app.post("/api/testsets")
async def new_testset(t: TestSet):
    return await q("INSERT INTO test_sets(name) VALUES(%s) RETURNING id", (t.name,), one=True)

@app.delete("/api/testsets/{sid}")
async def del_testset(sid: int):
    await q("DELETE FROM test_sets WHERE id=%s", (sid,)); return {"ok": True}

@app.post("/api/testsets/{sid}/cases")
async def add_case(sid: int, c: TestCase):
    return await q("INSERT INTO test_cases(set_id,question,expected) VALUES(%s,%s,%s) RETURNING id", (sid, c.question, c.expected), one=True)

@app.delete("/api/testcases/{cid}")
async def del_case(cid: int):
    await q("DELETE FROM test_cases WHERE id=%s", (cid,)); return {"ok": True}

class TestRunReq(BaseModel):
    agent_id: int
    agent_version: int | None = None

async def _exec_testrun(trid, sid, aid, ver):
    try:
        for c in await q("SELECT * FROM test_cases WHERE set_id=%s ORDER BY id", (sid,)):
            last = None
            async for ev in run_agent(aid, ver, f"test-{trid}-{c['id']}", c["question"], trid, c["id"]): last = ev
            try: await do_judge(last["run_id"], c["expected"] or None)
            except Exception: pass  # noqa: BLE001
        await q("UPDATE test_runs SET status='xong' WHERE id=%s", (trid,))
    except Exception as e:  # noqa: BLE001
        await q("UPDATE test_runs SET status=%s WHERE id=%s", (f"lỗi: {str(e)[:200]}", trid))

@app.post("/api/testsets/{sid}/run")
async def run_testset(sid: int, r: TestRunReq):
    ver = r.agent_version or await cur_version(r.agent_id)
    trid = (await q("INSERT INTO test_runs(set_id,agent_id,agent_version,status) VALUES(%s,%s,%s,'đang chạy') RETURNING id",
                    (sid, r.agent_id, ver), one=True))["id"]
    S["testjobs"][trid] = asyncio.create_task(_exec_testrun(trid, sid, r.agent_id, ver))
    return {"test_run_id": trid, "agent_version": ver}

@app.get("/api/testsets/{sid}")
async def testset_detail(sid: int):
    cases = await q("SELECT * FROM test_cases WHERE set_id=%s ORDER BY id", (sid,))
    trs = await q("SELECT t.*, a.name agent_name FROM test_runs t LEFT JOIN agents a ON a.id=t.agent_id WHERE set_id=%s ORDER BY id", (sid,))
    cells = await q("SELECT r.id run_id, r.test_run_id, r.test_case_id, left(r.answer,400) answer, r.error, r.total_tokens, r.cost_usd,"
                    " (SELECT score FROM eval_scores s WHERE s.run_id=r.id AND kind='ai' ORDER BY ts DESC LIMIT 1) ai_score,"
                    " (SELECT comment FROM eval_scores s WHERE s.run_id=r.id AND kind='ai' ORDER BY ts DESC LIMIT 1) ai_reason"
                    " FROM eval_runs r JOIN test_runs t ON t.id=r.test_run_id WHERE t.set_id=%s", (sid,))
    return {"cases": cases, "test_runs": trs, "cells": cells}

# ---------------- Settings ----------------
@app.get("/api/settings")
async def settings():
    k = os.environ.get("GOOGLE_API_KEY", "")
    key_ok = None
    try:
        await _g.aio.models.get(model=DEFAULT_MODEL); key_ok = True
    except Exception as e:  # noqa: BLE001
        key_ok = f"lỗi: {type(e).__name__}"
    mem = {l.split(":")[0]: int(l.split()[1]) // 1024 for l in open("/proc/meminfo") if l.split(":")[0] in ("MemTotal", "MemAvailable")}
    rss = next(int(l.split()[1]) // 1024 for l in open("/proc/self/status") if l.startswith("VmRSS"))
    try: cg = int(open("/sys/fs/cgroup/system.slice/deepagent-api.service/memory.current").read()) // 2**20
    except Exception: cg = None  # noqa: BLE001
    try: cgmax = open("/sys/fs/cgroup/system.slice/deepagent-api.service/memory.max").read().strip()
    except Exception: cgmax = None  # noqa: BLE001
    du = shutil.disk_usage("/")
    svcs = ["deepagent-api", "caddy", "cloudflared-deepagent", "cloudflared-adk", "adk-web", "adk-api", "docker"]
    st = {s: subprocess.run(["systemctl", "is-active", s], capture_output=True, text=True).stdout.strip() for s in svcs}
    try:
        dk = subprocess.run(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"], capture_output=True, text=True, timeout=5).stdout.strip().splitlines()
    except Exception: dk = []  # noqa: BLE001
    return {"gemini_key": {"configured": bool(k), "masked": "•••••••• (đã cấu hình, không hiển thị)" if k else "chưa cấu hình", "works": key_ok},
            "pricing": PRICING, "pricing_source": PRICING_SOURCE, "default_model": DEFAULT_MODEL,
            "ram": {"total_mb": mem["MemTotal"], "available_mb": mem["MemAvailable"], "api_rss_mb": rss, "api_cgroup_mb": cg,
                    "api_limit": (f"{int(cgmax) // 2**20} MB" if cgmax and cgmax.isdigit() else cgmax)},
            "disk": {"total_gb": round(du.total / 1e9, 1), "used_gb": round(du.used / 1e9, 1), "pct": round(du.used / du.total * 100)},
            "services": st, "containers": dk, "cached_agents": [f"{k[0]}:v{k[1]}" + (":external" if k[-1] == "external" else "") for k in S["agents"]]}

@app.get("/health")
async def health():
    return {"ok": True}

# ---------------- Playground + prompt library ----------------
class PlayReq(BaseModel):
    template: str = Field(min_length=1, max_length=50000)
    variables: dict = {}
    model: str = DEFAULT_MODEL
    temperature: float = Field(default=0.3, ge=0, le=2)
    fallback: bool = True
    template_format: str = Field(default="mustache", pattern=r"^(mustache|f-string|jinja2)$")

def _ext():
    import ext; return ext

def render_tpl(t, v):
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: str(v.get(m.group(1), m.group(0))), t)

@app.post("/api/playground/run")
async def play(p: PlayReq):
    prompt = await _ext().render_async(p.template, p.variables, p.template_format); t0 = time.perf_counter()
    try:
        txt, m, i, o = await gw.generate(_g, prompt, [p.model] + (gw.DEFAULT_FALLBACKS if p.fallback else []), temperature=p.temperature, feature="playground")
    except gw.SpendCapReached as e:
        raise HTTPException(402, str(e)) from None
    except gw.ProviderKeyMissing as e:
        raise HTTPException(424, str(e)) from None
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, str(e)[:300])
    return {"output": txt, "model": m, "input_tokens": i, "output_tokens": o, "cost_usd": cost(m, i, o),
            "ms": int((time.perf_counter() - t0) * 1000), "rendered": prompt}

class PromptSave(BaseModel):
    name: str = Field(pattern=r"^[\w\-. ]{1,80}$")
    template: str = Field(min_length=1, max_length=50000)
    model: str = DEFAULT_MODEL
    temperature: float = 0.3
    variables: dict = {}
    note: str | None = None

@app.post("/api/prompts")
async def prompt_save(p: PromptSave):
    pid = (await q("INSERT INTO prompts(name) VALUES(%s) ON CONFLICT(workspace_id,name) DO UPDATE SET name=EXCLUDED.name RETURNING id", (p.name,), one=True))["id"]
    v = (await q("SELECT coalesce(max(version),0)+1 v FROM prompt_versions WHERE prompt_id=%s", (pid,), one=True))["v"]
    await q("INSERT INTO prompt_versions(prompt_id,version,template,model,temperature,variables,note) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (pid, v, p.template, p.model, p.temperature, Jsonb(p.variables), p.note))
    return {"id": pid, "version": v}

@app.get("/api/prompts")
async def prompt_list():
    return await q("SELECT p.id, p.name, max(v.version) latest, max(v.created) updated FROM prompts p JOIN prompt_versions v ON v.prompt_id=p.id GROUP BY p.id ORDER BY updated DESC")

@app.get("/api/prompts/{pid}")
async def prompt_get(pid: int):
    return await q("SELECT * FROM prompt_versions WHERE prompt_id=%s ORDER BY version DESC", (pid,))

@app.delete("/api/prompts/{pid}")
async def prompt_del(pid: int):
    await q("DELETE FROM prompts WHERE id=%s", (pid,)); return {"ok": True}

# ---------------- Deployments ----------------
class DeployReq(BaseModel):
    agent_id: int
    version: int | None = None
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,40}$")

def _hash(k): return hashlib.sha256(k.encode()).hexdigest()

@app.post("/api/deployments")
async def deploy(d: DeployReq):
    await __import__("agent_sharing").guard_edit(d.agent_id)   # 8.P: share 'use' -> 403
    ver = d.version or await cur_version(d.agent_id)
    key = "mz_" + pysecrets.token_urlsafe(24)
    try:
        did = (await q("INSERT INTO deployments(slug,agent_id,version,key_hash,key_prefix) VALUES(%s,%s,%s,%s,%s) RETURNING id",
                       (d.slug, d.agent_id, ver, _hash(key), key[:7]), one=True))["id"]
    except Exception:  # noqa: BLE001
        raise HTTPException(409, "Đường dẫn (slug) đã tồn tại")
    await q("INSERT INTO deployment_history(deployment_id,version,action) VALUES(%s,%s,'deploy')", (did, ver))
    return {"id": did, "slug": d.slug, "version": ver, "endpoint": f"/d/{d.slug}/invoke", "api_key": key,
            "note": "API key chỉ hiển thị MỘT lần; server chỉ lưu SHA-256."}

@app.get("/api/deployments")
async def deploy_list():
    return await q("SELECT d.id, d.slug, d.agent_id, a.name agent_name, d.version, a.current_version, d.key_prefix, d.status, d.created, d.updated,"
                   " (SELECT count(*) FROM eval_runs r WHERE r.deployment=d.slug) runs,"
                   " (SELECT count(*) FROM eval_runs r WHERE r.deployment=d.slug AND r.error IS NOT NULL) errors,"
                   " (SELECT json_agg(h ORDER BY h.id DESC) FROM deployment_history h WHERE h.deployment_id=d.id) history"
                   " FROM deployments d JOIN agents a ON a.id=d.agent_id ORDER BY d.id")

class DeployUpd(BaseModel):
    version: int | None = None
    status: str | None = Field(default=None, pattern=r"^(active|paused)$")
    rotate_key: bool = False

@app.patch("/api/deployments/{did}")
async def deploy_upd(did: int, u: DeployUpd):
    d = await q("SELECT * FROM deployments WHERE id=%s", (did,), one=True)
    if not d: raise HTTPException(404)
    await __import__("agent_sharing").guard_edit(d["agent_id"])
    out = {}
    if u.version:
        if not await q("SELECT 1 FROM agent_versions WHERE agent_id=%s AND version=%s", (d["agent_id"], u.version), one=True):
            raise HTTPException(404, "Không có phiên bản này")
        await q("UPDATE deployments SET version=%s, updated=now() WHERE id=%s", (u.version, did))
        await q("INSERT INTO deployment_history(deployment_id,version,action) VALUES(%s,%s,%s)",
                (did, u.version, "rollback" if u.version < d["version"] else "promote"))
    if u.status:
        await q("UPDATE deployments SET status=%s, updated=now() WHERE id=%s", (u.status, did))
        await q("INSERT INTO deployment_history(deployment_id,version,action) VALUES(%s,%s,%s)", (did, d["version"], u.status))
    if u.rotate_key:
        key = "mz_" + pysecrets.token_urlsafe(24)
        await q("UPDATE deployments SET key_hash=%s, key_prefix=%s, updated=now() WHERE id=%s", (_hash(key), key[:7], did))
        await q("INSERT INTO deployment_history(deployment_id,version,action) VALUES(%s,%s,'rotate_key')", (did, d["version"]))
        out["api_key"] = key
    return {"ok": True, **out}

@app.delete("/api/deployments/{did}")
async def deploy_del(did: int):
    d = await q("SELECT agent_id FROM deployments WHERE id=%s", (did,), one=True)
    if d: await __import__("agent_sharing").guard_edit(d["agent_id"])
    await q("DELETE FROM deployments WHERE id=%s", (did,)); return {"ok": True}

class Invoke(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    thread_id: str | None = Field(default=None, max_length=100)

from fastapi import Header
@app.post("/d/{slug}/invoke", tags=["deployments"])
async def public_invoke(slug: str, body: Invoke, x_api_key: str = Header(default="")):
    """Endpoint công khai cho agent đã deploy. Xác thực bằng header X-API-Key (không cần basic auth)."""
    d = await q("SELECT d.*, a.workspace_id FROM deployments d JOIN agents a ON a.id=d.agent_id WHERE d.slug=%s", (slug,), one=True)   # GĐ10.1: slug is globally unique; the agent's workspace comes with the row
    if not d or not pysecrets.compare_digest(_hash(x_api_key), d["key_hash"]): raise HTTPException(401, "API key sai")
    if d["status"] != "active": raise HTTPException(503, "Deployment đang tạm dừng")
    last = None
    import admin as _adm
    async with _adm.as_workspace(d["workspace_id"]):   # GĐ10.1: the whole run (traces, usage, threads) is written in the deployment's workspace
        async for ev in run_agent(d["agent_id"], d["version"], body.thread_id or ("dep-" + uuid.uuid4().hex[:10]), body.message, deployment=slug):
            last = ev
    if last["error"]: raise HTTPException(502, last["error"][:200])
    return {"reply": last["reply"], "run_id": last["run_id"], "agent_version": d["version"], "tokens": last["tokens"], "cost_usd": last["cost_usd"]}

@app.get("/d/{slug}/health", tags=["deployments"])
async def public_health(slug: str):
    d = await q("SELECT status, version FROM deployments WHERE slug=%s", (slug,), one=True)
    if not d: raise HTTPException(404)
    return d

# ---------------- Fleet ----------------
@app.get("/api/fleet")
async def fleet():
    return await q("SELECT a.id, a.name, a.current_version, v.config->>'model' model, v.config->>'description' description,"
                   " (v.config->>'sandbox')::bool sandbox, jsonb_array_length(v.config->'subagents') n_sub, jsonb_array_length(v.config->'tools') n_tools,"
                   " (SELECT count(*) FROM eval_runs r WHERE r.agent_id=a.id) runs,"
                   " (SELECT count(*) FROM eval_runs r WHERE r.agent_id=a.id AND r.ts > now()-interval '24 hours') runs_24h,"
                   " (SELECT count(*) FROM eval_runs r WHERE r.agent_id=a.id AND r.error IS NOT NULL) errors,"
                   " (SELECT coalesce(sum(cost_usd),0) FROM eval_runs r WHERE r.agent_id=a.id) cost,"
                   " (SELECT coalesce(sum(total_tokens),0) FROM eval_runs r WHERE r.agent_id=a.id) tokens,"
                   " (SELECT round(avg(s.score),2) FROM eval_scores s JOIN eval_runs r ON r.id=s.run_id WHERE r.agent_id=a.id) avg_score,"
                   " (SELECT max(ts) FROM eval_runs r WHERE r.agent_id=a.id) last_run,"
                   " (SELECT json_agg(json_build_object('slug',d.slug,'version',d.version,'status',d.status)) FROM deployments d WHERE d.agent_id=a.id) deployments"
                   " FROM agents a JOIN agent_versions v ON v.agent_id=a.id AND v.version=a.current_version WHERE NOT a.deleted AND NOT a.hidden ORDER BY a.id")

# ---------------- Gateway ----------------
@app.get("/api/gateway")
async def gateway_info():
    # GĐ10.28b: gateway_usage has no RLS (the global provider caps must sum every workspace), so EVERY caller is filtered to its own workspace here.
    # A legacy row with workspace_id NULL (before 10.1) counts only when its agent is visible to the caller (RLS on agents). The demo account is narrowed further to its visible agents.
    ws = __import__("admin").cur_ws()
    mine = " AND (workspace_id=%s OR (workspace_id IS NULL AND agent_id IN (SELECT id FROM agents)))"
    own = mine + (" AND agent_id IN (SELECT id FROM agents)" if __import__("admin").is_demo() else "")
    by_model = await q("SELECT model, count(*) calls, count(*) FILTER (WHERE status<>'ok') errors, sum(input_tokens) in_tok, sum(output_tokens) out_tok,"
                       " sum(cost_usd) cost, round(avg(latency_ms)) avg_ms FROM gateway_usage WHERE ts > now()-interval '7 days'" + own + " GROUP BY model ORDER BY calls DESC", (ws,))
    by_agent = await q("SELECT g.agent_id, a.name, count(*) calls, count(*) FILTER (WHERE g.status<>'ok') errors, sum(g.cost_usd) cost,"
                       " sum(g.cost_usd) FILTER (WHERE g.ts > date_trunc('day', now() AT TIME ZONE 'Asia/Ho_Chi_Minh') AT TIME ZONE 'Asia/Ho_Chi_Minh') cost_today,"
                       " (v.config->>'budget_usd_day') budget, (v.config->>'rpm_limit') rpm, v.config->'fallback_models' fallbacks, v.config->>'model' model"
                       " FROM gateway_usage g LEFT JOIN agents a ON a.id=g.agent_id LEFT JOIN agent_versions v ON v.agent_id=a.id AND v.version=a.current_version"
                       " WHERE g.ts > now()-interval '7 days'" + own.replace("agent_id", "g.agent_id").replace("workspace_id", "g.workspace_id") + " GROUP BY g.agent_id, a.name, v.config ORDER BY calls DESC", (ws,))
    recent = await q("SELECT ts, agent_id, model, status, input_tokens, output_tokens, cost_usd, latency_ms, left(error,160) error FROM gateway_usage WHERE true" + own + " ORDER BY id DESC LIMIT 40", (ws,))
    k = os.environ.get("GOOGLE_API_KEY", "")
    return {"by_model": by_model, "by_agent": by_agent, "recent": recent, "default_chain": [DEFAULT_MODEL] + gw.DEFAULT_FALLBACKS,
            "key": {"provider": "Gemini API", "configured": bool(k), "fingerprint": ("sha256:" + hashlib.sha256(k.encode()).hexdigest()[:10]) if k else None},
            "pricing": PRICING, "pricing_source": PRICING_SOURCE}

class KeyReq(BaseModel):
    api_key: str = Field(min_length=20, max_length=200)

@app.post("/api/gateway/key")
async def set_key(k: KeyReq):
    """Thay Gemini key: kiểm tra trước, rồi ghi /opt/langchain/.env (600). Không bao giờ trả key về."""
    global _g
    test = genai.Client(api_key=k.api_key)
    try: await test.aio.models.get(model=DEFAULT_MODEL)
    except Exception as e: raise HTTPException(400, f"Key không dùng được: {type(e).__name__}")  # noqa: B904
    p = "/opt/langchain/.env"; lines = open(p).read().splitlines()
    lines = [l for l in lines if not l.startswith("GOOGLE_API_KEY=")] + ["GOOGLE_API_KEY=" + k.api_key]
    open(p + ".tmp", "w").write("\n".join(lines) + "\n"); os.chmod(p + ".tmp", 0o600); os.replace(p + ".tmp", p)
    os.environ["GOOGLE_API_KEY"] = k.api_key; _g = test; S["agents"].clear()
    return {"ok": True, "fingerprint": "sha256:" + hashlib.sha256(k.api_key.encode()).hexdigest()[:10]}

class SbxReq(BaseModel):
    code: str = Field(min_length=1, max_length=100000)

@app.post("/api/sandbox/run")
async def sandbox_run(r: SbxReq):
    t0 = time.perf_counter(); out = await run_sandbox(r.code)
    return {"ms": int((time.perf_counter() - t0) * 1000), "output": out}

import obs as _obs, evals as _evals, studio as _studio, ui_obs, ui_studio, admin as _admin, gw_api as _gw_api, server as _server, ui_admin
app.include_router(_obs.router); app.include_router(__import__("evals_ds").router); app.include_router(_evals.router); app.include_router(_studio.router)
import studio_tt as _studio_tt; app.include_router(_studio_tt.router)   # GĐ9.5 Studio time travel (fork / edit state / branches / memory editor)
import nlsearch as _nls; app.include_router(_nls.router); _nls.register()   # GD7.2 natural-language trace search
import builder_chat as _bchat; app.include_router(_bchat.router)   # GĐ8.7 create an agent by chatting (/api/builder/chat)
import parquet_export as _pqx; app.include_router(_pqx.router)   # GĐ9.9 Parquet export
import otel as _otel, envs as _envs, otel_ctx as _octx; app.include_router(_otel.router); app.include_router(_envs.router)   # GĐ9.10 OTLP in/out + environments
app.add_middleware(_octx.TraceparentMiddleware); _octx.install_httpx(); _octx.install_stdlib()   # GĐ9.10 W3C traceparent: continue incoming, propagate on outgoing httpx
import privacy as _prv; _prv.setup(); app.include_router(_prv.router)   # GĐ9.8 PII masking settings + data-subject rights
import insights as _ins; app.include_router(_ins.router)   # GD7.3 Insights (usage topics + weekly report)
import align as _align; app.include_router(_align.router)   # GD7.5 judge alignment
import engine as _eng; app.include_router(_eng.router)   # GD7.4 Engine-lite (issues -> root cause -> fix -> dataset -> re-test)
import ext as _ext_mod
import ui_api as _ui_api
from fastapi.staticfiles import StaticFiles as _SF
app.include_router(_ui_api.router); app.mount("/static", _SF(directory=str(__import__("pathlib").Path(__file__).parent / "static")), name="static")
app.include_router(_ext_mod.router); app.include_router(_admin.router); app.include_router(_gw_api.router); app.include_router(_server.router)
import ops_api as _ops_api; app.include_router(_ops_api.router)
import sandbox2 as _sbx2; app.include_router(_sbx2.router)
import sbx_api as _sbxapi; app.include_router(_sbxapi.router)   # GĐ11.43 /api/v2/sandboxes/boxes (virtual computers: execute, commands, files)
import oauth_tools as _oauth; app.include_router(_oauth.router)
import connections as _conns; app.include_router(_conns.router)   # GĐ11.24 /api/connections (owner user | agent | workspace)
import workspace_tools as _wst; app.include_router(_wst.router)   # GĐ8.5 /api/workspace-tools (Google / M365 per workspace)
import integrations as _integ; app.include_router(_integ.router)   # GĐ12.85 /api/integrations (view-only status per Google / Microsoft family)
import ws_secrets as _wss; app.include_router(_wss.router)   # GĐ6.5
import durable as _dur; app.include_router(_dur.router)   # GĐ13.30 /api/admin/runs/stats (admin only)
import retention as _ret; app.include_router(_ret.router)   # GĐ13.25 /api/admin/retention (admin only)
import tool_catalog as _tcat; app.include_router(_tcat.router)   # GĐ11.31 GET /api/tools
import mcp_registry as _mcpr; app.include_router(_mcpr.router)   # GĐ11.23 /api/mcp* (per workspace)
import mcp_oauth as _mcpo; app.include_router(_mcpo.router)   # GĐ11.26 POST /api/mcp/servers/probe + /oauth, GET /oauth/client-metadata.json
import gw_providers as _gwp; app.include_router(_gwp.router)   # GĐ11.1 /api/gw/providers
import gw_fallbacks as _gwfb; app.include_router(_gwfb.router)   # GĐ11.13 /api/gw/fallbacks
import gw_policy_engine as _gwpe; app.include_router(_gwpe.router)   # GĐ11.14 /api/gw/model-access, /api/gw/limits
import thread_inbox as _tinbox; app.include_router(_tinbox.router)   # GĐ11.35 /api/inbox, /api/threads/{tid}/read
import crypto_api as _crya; app.include_router(_crya.router)   # GĐ9.7
import channels as _channels; app.include_router(_channels.router)   # GĐ8.1 /api/agents/{id}/channels, /api/channels/{bid}
import channel_zalo as _ch_zalo; app.include_router(_ch_zalo.router)   # GĐ8.3 public Zalo OA webhook /channels/zalo/{bid} (signature-checked)
import channel_zalo_personal as _ch_zp; app.include_router(_ch_zp.router)   # GĐ8.3b admin API + loopback-only sidecar callbacks (404 unless AOS_ZALO_PERSONAL=1)
import mail_triggers as _mailtr; app.include_router(_mailtr.router)   # GĐ11.60 /api/channels/{bid}/status|poll-now|pause|resume (Gmail / Outlook new-mail triggers)
import twofa as _twofa; app.include_router(_twofa.router)   # GĐ6.7 TOTP 2FA + trang phiên
import attachments as _att; app.include_router(_att.router)   # G4: /api/files
import demo_ws as _demo_ws; app.include_router(_demo_ws.router)   # demo workspace provisioning (admin)
import chat_gd5 as _gd5; app.include_router(_gd5.router())   # GĐ5a: memories, thread FTS, @mention delegation
import chat_projects as _cproj; app.include_router(_cproj.router)   # GĐ8.9 chat projects (/api/chat/projects)
import chat_gd5b as _gd5b; app.include_router(_gd5b.router())   # GĐ5b: canvas, code run, schedules, share links, /favicon.ico
import assistant as _assistant; app.include_router(_assistant.router())   # GĐ7.1: /api/assistant
import aos_tools as _aos_tools; _aos_tools.mount(app); _ext_mod.mount_mcp(app); __import__('a2a_official').mount(app)   # GĐ9.6 /mcp (official mcp 2.x); GĐ7.0: one tool set -> MCP /mcp/aos (also get_tools() for the 7.1 assistant)
import agent_sharing as _ashare, agent_mcp as _amcp, skill_import as _skimp; app.include_router(_ashare.router); app.include_router(_skimp.router); _amcp.mount(app)   # GĐ8.8 /api/agents/{id}/shares, /api/skills/import, MCP /mcp/agents/{id}
import agent_clock as _aclk; app.include_router(_aclk.router)   # GĐ9.P1 workspace time zone API
import context_hub as _chub; app.include_router(_chub.router)   # GĐ9.1 /api/packs*, /api/agents/{id}/context
import skills_mgmt as _skm; app.include_router(_skm.router)   # GĐ12.88 /api/skills (lint, import check) + 12.86 private/shared skills
import prompt_lab as _plab; app.include_router(_plab.router)   # GĐ9.2 Prompt Canvas (/api/hub/canvas/*) + GĐ9.3 Playground lab (/api/playground/lab, /tools)
import destinations as _dests; app.include_router(_dests.router)   # GĐ12.1 /api/notify/*, /api/me/destinations
import health_watch as _hw; app.include_router(_hw.router)   # GĐ11 11.N3 /api/admin/health
import charts as _charts; app.include_router(_charts.router)   # GĐ12.5 POST /api/obs/charts/query, GET /api/obs/chart_templates
import dashboards as _dashboards; app.include_router(_dashboards.router)   # GĐ12.8 GET /api/obs/dashboards/prebuilt, /api/obs/projects/{pid}/threads/stats
import rules as _rules; app.include_router(_rules.router)   # GĐ12.12 /api/obs/rules (automation rules; moved out of obs.py)
import alerts as _alerts; app.include_router(_alerts.router)   # GĐ12.10 /api/obs/alerts* (alert rules; moved out of obs.py)
import invites as _invites; app.include_router(_invites.router)   # GĐ12.100 /api/admin/workspaces/{wid}/invites + public /invite/{token}, /api/invites/{token}/accept
import memory_routes  # noqa: F401,E402  GĐ11.70 registers its agent_purge hook
import memory_admin as _madm; app.include_router(_madm.router)   # GĐ13.6 /api/agents/{id}/memory/* (proposals, view / edit)
import agent_purge as _apurge; app.include_router(_apurge.router)   # GĐ11.34 /api/agents/{id}/restore, /api/archived-agents, /api/admin/agent-exports
app.middleware("http")(_admin.middleware)
app.add_middleware(_server.DefaultJsonContentType)   # GĐ9.6: bodies without Content-Type (agent-chat-ui passthrough) are JSON
from fastapi.exceptions import HTTPException as _HE
from fastapi.responses import JSONResponse as _JR
from fastapi.exception_handlers import http_exception_handler as _default_he
@app.exception_handler(gw.ProviderKeyMissing)
async def _provider_key_missing(request, exc):
    """GĐ11.3: a model's provider has no key / is disabled -> 424 with the user text ("Thiếu khoá <nhà cung cấp>")."""
    return _JR({"detail": str(exc)}, status_code=424)
@app.exception_handler(_HE)
async def _openai_errors(request, exc):
    """/v1/*: trả lỗi đúng dạng OpenAI {"error": {...}} thay vì {"detail": ...}. Mọi thông báo lỗi API trả tiếng Anh (qua từ điển i18n)."""
    import i18n
    exc.detail = i18n.en_detail(exc.detail)
    if request.url.path.startswith("/v1/"):
        d = exc.detail
        body = d if isinstance(d, dict) and "error" in d else {"error": {"message": str(d), "type": "invalid_request_error"}}
        return _JR(body, status_code=exc.status_code, headers=getattr(exc, "headers", None))
    return await _default_he(request, exc)
import ui_ls as _ui_ls; ui.PAGES.update(_ui_ls.PAGES); ui.PAGES.update(ui_obs.PAGES); ui.PAGES.update(ui_studio.PAGES); ui.PAGES.update(ui_admin.PAGES)
import ui_tracing as _ui_tr, ui_ls2 as _ui_ls2, ui_ls3 as _ui_ls3, ui_ls4 as _ui_ls4, ui_insights as _ui_ins, ui_engine as _ui_eng
# Audit #25: one density/padding standard for the older classic pages (inline paddings overridden).
_CLS_CSS = ("<style>body.cls main{gap:14px}body.cls main .card{padding:14px 16px!important;border-radius:var(--r-lg)!important}"
            "body.cls main td,body.cls main th{padding:7px 8px!important}body.cls main td{font-size:13px}body.cls main th{font-size:12px}"
            "body.cls main h2{font-size:15px;margin:0 0 10px}body.cls main h3{font-size:14px;margin:0 0 8px}body.cls main .row{gap:8px;flex-wrap:wrap}"
            "body.cls main small{font-size:12px}body.cls main pre{font-size:12px}body.cls .grid2{gap:14px}</style>")
for _k in list(ui.PAGES):
    if _k != '/overview': ui.PAGES[_k] = ui.PAGES[_k].replace('</head>', _CLS_CSS + '</head>', 1).replace('<body>', '<body class="cls">', 1)
_CLASSIC = {'/classic' + k: v.replace('<head>', '<head><base href="/">', 1) for k, v in ui.PAGES.items() if k in ('/projects', '/rules', '/datasets', '/experiments', '/hub', '/playground', '/deployments', '/settings', '/admin', '/server', '/evaluators', '/studio', '/agents', '/annotation', '/sandboxes', '/gwkeys', '/builder', '/gateway', '/memory')}
ui.PAGES.update(_ui_tr.PAGES); ui.PAGES.update(_ui_ls2.PAGES); ui.PAGES.update(_ui_ls3.PAGES); ui.PAGES.update(_ui_ls4.PAGES); ui.PAGES.update(_ui_ins.PAGES); ui.PAGES.update(_ui_eng.PAGES); ui.PAGES.update(_CLASSIC)
import ui_builder_chat as _ui_bchat; ui.PAGES.update(_ui_bchat.PAGES)   # GĐ8.7 /builder-chat
import ui_skills as _ui_skills; ui.PAGES.update(_ui_skills.PAGES)   # GĐ12.89 /skills (private / shared Skills, templates, create / import)
import gd9q as _gd9q; _gd9q.mount(app)   # GĐ9.Q: server config / custom auth / agent identity / sandbox parity / gateway policies / custom apps (routers, middleware, hooks, pages)
def _page_route(html):
    import i18n
    async def _r(request: Request): return HTMLResponse(i18n.page_for(html, request))   # chỉ đọc cookie ngôn ngữ; query string không ghi đè nội dung
    return _r
for _path, _page in ui.PAGES.items():
    app.add_api_route(_path, _page_route(_page), methods=["GET"], include_in_schema=False)
