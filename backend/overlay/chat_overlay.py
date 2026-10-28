"""LOCAL-ONLY Phase-0 overlay on the real Agent OS server.py (box only, never deployed). Mirrors phase0_design.md §5, §7–§9, §12.
Phase 1 ports these as additive endpoints + migrations (voice_models, voice_settings, agents.config.voice, voice_sessions).

  GET  /api/voice/config?agent_id=agent-N   merged allow-list/default/caps for the caller (agent override > workspace > catalog)
  GET  /api/settings/voice                  catalog + workspace settings + agent overrides + audit       (any member, read)
  PUT  /api/settings/voice                  workspace settings / catalog                                 (admin only)
  PUT  /api/settings/voice/agents/{graph}   per-agent override                                           (admin | editor)
  POST /api/voice/session                   checks auth, voice.use, allow-list (403), budget/minutes (402), concurrency (429);
                                            Phase 0 returns a MOCK grant (adapter "mock") -> NO OpenAI/Google call, NO key.
  GET  /api/voice/sessions                  caller's sessions (voice_sessions mock)
  POST /api/voice/session/{id}/end          closes the session, books seconds + estimated USD
  POST /api/voice/session/{id}/token        GĐ4: fresh single-use locked token for an ACTIVE own session (resumption), max 8 per session
  POST /api/voice/delegate                  agent_os_delegate: runs the thread's agent via /api/v1 (same thread, same user)
  GET  /api/voice/delegate/{call_id}        status of a delegated call (used after an HITL approval)
  POST /api/voice/transcript                server-side transcript -> /api/v1/threads/{tid}/state (modality "voice")
  /chat/*                                   built front-end UI (Vite dist) same-origin with CSP (§5.3)
Storage: Postgres tables voice_kv / voice_sessions / voice_calls / voice_audit (GĐ3; JSON file only as rollback). Nothing here holds or reads a provider key."""
import asyncio, json, secrets, time, datetime as dt, os, threading
from pathlib import Path
import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from typing import Literal
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb
import admin as _adm
import deepagent_api as A

VAR = Path(os.environ.get("AOS_OVERLAY_VAR", "/workspace/agentos-local/var")); STORE = VAR / "voice_store.json"
DIST = Path(os.environ.get("AOS_CHAT_DIST", "/workspace/agentos-chat/dist"))
SELF = os.environ.get("AOS_SELF", "http://127.0.0.1:8020")
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "connect-src 'self' wss://generativelanguage.googleapis.com; img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self' data:; "
       "frame-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'")
_LOCK = threading.Lock()

# ---- A8 (approved 20/10/2026 17:33 UTC+7): REAL calls for provider "google" only. gpt-live-1 stays mock/blocked.
# The key lives ONLY in this process env (AOS_GEMINI_LIVE_KEY); it never leaves the server and is masked in every error.
REAL_PROVIDERS = {p.strip() for p in os.environ.get("AOS_VOICE_REAL_PROVIDERS", "").split(",") if p.strip()} - {"openai"}
A8_ABORT_USD = float(os.environ.get("AOS_A8_ABORT_USD", "0.80"))     # refuse new real sessions at/after this cumulative estimate
A8_HARD_USD = 1.00                                                   # approved hard cap
# Official paid-tier prices for gemini-3.8-live (https://ai.google.dev/gemini-api/docs/pricing, "Last updated 2026-10-19", fetched 20/10/2026)
LIVE_PRICES = {"gemini-3.8-live": {"in": {"TEXT": 0.75, "AUDIO": 3.00, "IMAGE": 1.00, "VIDEO": 1.00}, "out": {"TEXT": 4.50, "AUDIO": 12.00}}}
def _key():
    k = os.environ.get("AOS_GEMINI_LIVE_KEY", "").strip()
    return k or None
def _mask(text: str) -> str:
    k = _key(); t = str(text)
    return t.replace(k, "AQ.…") if k else t
_GENAI = None
def _genai():
    global _GENAI   # keep ONE client alive (a temporary one is closed by GC while its request is still in flight)
    if _GENAI is None:
        from google import genai
        _GENAI = genai.Client(api_key=_key(), http_options={"base_url": "https://generativelanguage.googleapis.com"})
    return _GENAI

def _usage_cost(model: str, u: dict) -> float:
    """Cost of ONE usageMetadata message (A8: sent once per turn; promptTokenCount = whole context, re-billed every turn).
    Conservative: tokens not covered by the per-modality details are priced as AUDIO (the dearer modality); thinking tokens
    (thoughtsTokenCount) are billed as output TEXT."""
    pr = LIVE_PRICES.get(model) or LIVE_PRICES["gemini-3.8-live"]
    def side(details, total, table):
        d = details or []; covered = sum(x.get("tokenCount") or 0 for x in d)
        return (sum((x.get("tokenCount") or 0) * table.get(x.get("modality", "AUDIO"), table["AUDIO"]) for x in d)
                + max((total or 0) - covered, 0) * table["AUDIO"]) / 1e6
    return (side(u.get("promptTokensDetails"), u.get("promptTokenCount"), pr["in"])
            + side(u.get("responseTokensDetails"), u.get("responseTokenCount"), pr["out"])
            + (u.get("thoughtsTokenCount") or 0) * pr["out"]["TEXT"] / 1e6)

AGENT_OS_DELEGATE = {
    "name": "agent_os_delegate", "behavior": "NON_BLOCKING",
    "description": "Giao mọi việc cần dữ liệu, công cụ hoặc hành động cho Agent OS (agent của hội thoại này). Hand any task needing data, tools or actions to Agent OS.",
    "parameters": {"type": "OBJECT", "properties": {"request": {"type": "STRING", "description": "Yêu cầu của người dùng, nguyên văn"},
                                                    "context": {"type": "STRING", "description": "Ngữ cảnh thêm (tuỳ chọn)"}}, "required": ["request"]},
}
INSTRUCTIONS_VI = ("Bạn là trợ lý giọng nói của Moza Agent OS. Luôn trả lời bằng tiếng Việt, ngắn gọn, tự nhiên. "
                   "Mọi câu hỏi về chiến dịch, số liệu, AI Lead, công việc hay hành động: gọi hàm agent_os_delegate với nguyên văn yêu cầu, "
                   "nói ngắn 'Để em hỏi Agent OS' rồi đọc lại kết quả khi có. Không tự bịa số liệu. Không duyệt việc cần phê duyệt bằng giọng nói.")

DICTATION_INSTRUCTIONS = ("You are a silent transcription endpoint for a chat composer. Never answer, never speak, never call tools. "
                          "The user's words are captured by input transcription only.")
READALOUD_INSTRUCTIONS = ("Read the user's text aloud exactly as written, in its own language, then stop. Do not add, answer, summarise or comment.")

def _mint_gemini(model: str, max_s: int, voice: str | None, purpose: str = "conversation"):
    """Agent OS mints a single-use ephemeral token with the whole session config LOCKED (model, prompt, tools, transcription...).
    purpose="dictation": same live model, input transcription only (no tools, no delegation); the browser does not play output."""
    from google.genai import types
    now = dt.datetime.now(dt.timezone.utc)
    if purpose == "readaloud":
        cfg = types.LiveConnectConfig(response_modalities=["AUDIO"], system_instruction=READALOUD_INSTRUCTIONS, output_audio_transcription={},
                                      speech_config={"voice_config": {"prebuilt_voice_config": {"voice_name": voice or "Kore"}}})
    if purpose in ("dictation", "readaloud"):
        if purpose == "dictation": cfg = types.LiveConnectConfig(response_modalities=["AUDIO"], system_instruction=DICTATION_INSTRUCTIONS,
                                      input_audio_transcription={"language_codes": ["vi"]},
                                      realtime_input_config={"automatic_activity_detection": {"silence_duration_ms": 2000, "prefix_padding_ms": 100}})
        tok = _genai().auth_tokens.create(config=types.CreateAuthTokenConfig(
            uses=1, expire_time=now + dt.timedelta(seconds=min(max_s + 60, 600)), new_session_expire_time=now + dt.timedelta(minutes=1),
            live_connect_constraints=types.LiveConnectConstraints(model=model, config=cfg)))
        return tok.name, int((now + dt.timedelta(seconds=min(max_s + 60, 600))).timestamp())
    cfg = types.LiveConnectConfig(
        response_modalities=["AUDIO"], system_instruction=INSTRUCTIONS_VI, tools=[{"function_declarations": [AGENT_OS_DELEGATE]}],
        input_audio_transcription={"language_codes": ["vi"]}, output_audio_transcription={},
        session_resumption={}, context_window_compression={"sliding_window": {}},
        speech_config={"voice_config": {"prebuilt_voice_config": {"voice_name": voice or "Kore"}}},
        realtime_input_config={"automatic_activity_detection": {"silence_duration_ms": 600, "prefix_padding_ms": 100}},
    )
    tok = _genai().auth_tokens.create(config=types.CreateAuthTokenConfig(
        uses=1, expire_time=now + dt.timedelta(seconds=min(max_s + 60, 30 * 60)), new_session_expire_time=now + dt.timedelta(minutes=1),
        live_connect_constraints=types.LiveConnectConstraints(model=model, config=cfg)))
    return tok.name, int((now + dt.timedelta(seconds=min(max_s + 60, 1800))).timestamp())

# Catalog rows are DATA (Settings-editable), not code. Prices as published 20/10/2026 (design §3, §11).
CATALOG = [
    {"id": "gpt-live-1", "provider": "openai", "display_name": "OpenAI GPT-Live 1", "transport": "webrtc", "price_unit": "per_min",
     "price_per_min": 0.05, "est_usd_per_min": 0.052, "enabled": True, "voice": "default", "notes": "client delegation + sideband"},
    {"id": "gemini-3.8-live", "provider": "google", "display_name": "Google Gemini 3.8 Live", "transport": "websocket", "price_unit": "per_mtok",
     "price_in": 3.0, "price_out": 12.0, "est_usd_per_min": 0.045, "enabled": True, "voice": "Kore", "notes": "ephemeral token, agent_os_delegate function"},
]
WS_DEFAULT = {"allowed_models": ["gpt-live-1", "gemini-3.8-live"], "default_model": "gemini-3.8-live", "max_session_min": 10.0,
              "daily_min_per_user": 30.0, "daily_budget_usd": 1.0, "language": "vi", "barge_in": True, "voice_roles": ["admin", "editor"],
              "max_concurrent_per_user": 1}

def _now(): return dt.datetime.now().astimezone()
STORE_SCHEMA = 2
def _migrate_dict(s):
    if s.get("schema", 1) < 2:   # migration 1 -> 2: voice.use default = viewer denied (G9); an admin re-grants it in Settings
        s["workspace"]["voice_roles"] = [r for r in s["workspace"].get("voice_roles", []) if r != "viewer"] or ["admin", "editor"]
        s["schema"] = 2; s.setdefault("calls", {})
    return s
def _fresh(): return {"schema": STORE_SCHEMA, "catalog": json.loads(json.dumps(CATALOG)), "workspace": dict(WS_DEFAULT), "agents": {}, "sessions": {}, "calls": {}, "audit": []}
def _load_json():
    s = None
    if STORE.exists():
        try: s = json.loads(STORE.read_text())
        except Exception: pass  # noqa: BLE001
    return _fresh() if s is None else _migrate_dict(s)

# ---- GĐ3 (c): the voice store lives in Postgres (same DB as Agent OS, PG_URI). Tables voice_kv (settings, catalog, spend),
# voice_sessions, voice_calls (one row each, jsonb + indexed columns) and voice_audit (append-only). The in-process view is the
# same dict the handlers always used; _save() writes only the rows that changed. First start imports voice_store.json once
# (kept on disk, renamed *.migrated_<ts>). AOS_VOICE_STORE=json keeps the old file store (rollback switch).
STORE_BACKEND = "json" if os.environ.get("AOS_VOICE_STORE", "pg") == "json" or not os.environ.get("PG_URI") else "pg"
_ROWS = {"sessions": ("voice_sessions", "id"), "calls": ("voice_calls", "call_id")}
_DDL = """
CREATE TABLE IF NOT EXISTS voice_kv(k text PRIMARY KEY, v jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS voice_sessions(id text PRIMARY KEY, seq bigserial, user_id bigint, status text, started_at text, data jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS voice_sessions_user_status ON voice_sessions(user_id, status);
CREATE TABLE IF NOT EXISTS voice_calls(call_id text PRIMARY KEY, seq bigserial, voice_session_id text, status text, data jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS voice_audit(id bigserial PRIMARY KEY, at text, by_user text, what text, data jsonb NOT NULL);
"""
_DB = {"conn": None, "ready": False}; _DBLOCK = threading.RLock()

class _Store(dict):
    """The store dict + what was loaded (to write only changed rows)."""
    snap: dict

def _dump(v): return json.dumps(v, sort_keys=True, default=str)
def _cx():
    import psycopg
    c = _DB["conn"]
    if c is None or c.closed:
        c = _DB["conn"] = psycopg.connect(os.environ["PG_URI"], autocommit=True, connect_timeout=5)
    return c
def _exec(sql, args=None, fetch=False):
    import psycopg
    with _DBLOCK:
        for attempt in (0, 1):
            try:
                with _cx().cursor() as cur:
                    cur.execute(sql, args)
                    return cur.fetchall() if fetch else None
            except (psycopg.OperationalError, psycopg.InterfaceError):   # one reconnect (server restart / idle drop), then raise
                _DB["conn"] = None
                if attempt: raise

def _row_cols(kind, key, v):
    if kind == "sessions": return (key, v.get("user_id"), v.get("status"), v.get("started_at"))
    return (key, v.get("voice_session_id"), v.get("status"))
def _upsert_row(kind, key, v):
    tbl, pk = _ROWS[kind]
    if kind == "sessions":
        _exec(f"INSERT INTO {tbl}(id,user_id,status,started_at,data) VALUES(%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET "
              "user_id=EXCLUDED.user_id, status=EXCLUDED.status, started_at=EXCLUDED.started_at, data=EXCLUDED.data, updated_at=now()",
              (*_row_cols(kind, key, v), Jsonb(json.loads(_dump(v)))))
    else:
        _exec(f"INSERT INTO {tbl}(call_id,voice_session_id,status,data) VALUES(%s,%s,%s,%s) ON CONFLICT (call_id) DO UPDATE SET "
              "voice_session_id=EXCLUDED.voice_session_id, status=EXCLUDED.status, data=EXCLUDED.data, updated_at=now()",
              (*_row_cols(kind, key, v), Jsonb(json.loads(_dump(v)))))
def _put_kv(k, v):
    _exec("INSERT INTO voice_kv(k,v) VALUES(%s,%s) ON CONFLICT (k) DO UPDATE SET v=EXCLUDED.v, updated_at=now()", (k, Jsonb(json.loads(_dump(v)))))
def _add_audit(e):
    _exec("INSERT INTO voice_audit(at,by_user,what,data) VALUES(%s,%s,%s,%s)", (e.get("at"), e.get("by"), e.get("what"), Jsonb(json.loads(_dump(e)))))

def _pg_init():
    """Create tables; import voice_store.json once (idempotent: only when voice_kv has no schema row)."""
    with _DBLOCK:
        if _DB["ready"]: return
        _exec(_DDL)
        if not _exec("SELECT 1 FROM voice_kv WHERE k='schema'", fetch=True):
            src = _load_json(); had_file = STORE.exists()
            with _DBLOCK:
                for k, v in src.items():
                    if k in _ROWS:
                        for key, row in v.items(): _upsert_row(k, key, row)
                    elif k != "audit": _put_kv(k, v)
                for e in src.get("audit", []): _add_audit(e)
                _put_kv("migration", {"from": "voice_store.json" if had_file else None, "at": _now().isoformat(),
                                      "sessions": len(src.get("sessions", {})), "calls": len(src.get("calls", {})), "audit": len(src.get("audit", []))})
                _put_kv("schema", src.get("schema", STORE_SCHEMA))   # last: marks the import complete
            if had_file:
                STORE.rename(STORE.with_name(f"voice_store.json.migrated_{time.strftime('%Y%m%d_%H%M%S')}"))
        _DB["ready"] = True

def _load():
    if STORE_BACKEND == "json": return _load_json()
    _pg_init()
    s = _Store()
    for k, v in _exec("SELECT k, v FROM voice_kv WHERE k <> 'migration'", fetch=True): s[k] = v
    for kind, (tbl, pk) in _ROWS.items():
        s[kind] = {key: data for key, data in _exec(f"SELECT {pk}, data FROM {tbl} ORDER BY seq", fetch=True)}
    s["audit"] = [d for (d,) in _exec("SELECT data FROM (SELECT id, data FROM voice_audit ORDER BY id DESC LIMIT 200) a ORDER BY id", fetch=True)]
    for k, v in _fresh().items(): s.setdefault(k, v)
    s.snap = {"kv": {k: _dump(v) for k, v in s.items() if k not in _ROWS and k != "audit"},
              **{kind: {key: _dump(v) for key, v in s[kind].items()} for kind in _ROWS}, "audit": {id(e) for e in s["audit"]}}
    return s

def _save(s):
    if STORE_BACKEND == "json" or not isinstance(s, _Store):
        VAR.mkdir(parents=True, exist_ok=True); STORE.write_text(json.dumps(s, indent=1, default=str)); return
    snap = s.snap
    with _DBLOCK:
        for k, v in s.items():
            if k in _ROWS:
                for key, row in v.items():
                    d = _dump(row)
                    if snap[k].get(key) != d: _upsert_row(k, key, row); snap[k][key] = d
            elif k == "audit":
                for e in v:
                    if id(e) not in snap["audit"]: _add_audit(e); snap["audit"].add(id(e))
            else:
                d = _dump(v)
                if snap["kv"].get(k) != d: _put_kv(k, v); snap["kv"][k] = d

def store_info():
    if STORE_BACKEND == "json": return {"backend": "json", "file": STORE.name}
    _pg_init()
    m = _exec("SELECT v FROM voice_kv WHERE k='migration'", fetch=True)
    return {"backend": "postgres", "tables": ["voice_kv", "voice_sessions", "voice_calls", "voice_audit"], "migration": m[0][0] if m else None}
def _user():
    u = _adm.cur()
    if not u: raise HTTPException(401, "login required")
    return u
def _audit(s, u, what, old, new):
    s["audit"].append({"at": _now().isoformat(), "by": u.get("username"), "what": what, "old": old, "new": new}); s["audit"] = s["audit"][-200:]

def merged(s, agent_id):
    """agent override > workspace > catalog(enabled). Returns (allowed, default, source)."""
    enabled = [m["id"] for m in s["catalog"] if m.get("enabled")]
    ws = s["workspace"]; allowed = [m for m in ws["allowed_models"] if m in enabled]; default = ws["default_model"]; src = "workspace"
    ov = s["agents"].get(agent_id or "") or {}
    if ov.get("allowed_models"): allowed = [m for m in ov["allowed_models"] if m in allowed]; src = "agent"
    if ov.get("default_model") in allowed: default = ov["default_model"]
    if default not in allowed: default = allowed[0] if allowed else None
    return allowed, default, src

def usage_today(s, uid):
    day = _now().date().isoformat(); mins = usd = 0.0
    for v in s["sessions"].values():
        if v["started_at"][:10] != day: continue
        sec = v.get("seconds") or (time.time() - v["t0"] if v["status"] == "active" else 0)
        cost = v.get("cost_usd") if v["status"] != "active" else sec / 60 * v["est_usd_per_min"]
        usd += cost or 0
        if v["user_id"] == uid: mins += sec / 60
    return round(mins, 3), round(usd, 4)

def _expire(s):
    for v in s["sessions"].values():
        if v["status"] == "active" and time.time() - v["t0"] > v["max_session_s"] + 30:
            v.update(status="expired", seconds=v["max_session_s"], cost_usd=round(v["max_session_s"] / 60 * v["est_usd_per_min"], 4))

def _fwd(request: Request):
    h = {k: v for k, v in request.headers.items() if k.lower() in ("cookie", "authorization")}
    return httpx.AsyncClient(base_url=SELF, headers=h, timeout=120)

class WsIn(BaseModel):
    allowed_models: list[str] = Field(min_length=1)
    default_model: str
    max_session_min: float = Field(gt=0, le=30)
    daily_min_per_user: float = Field(ge=0, le=600)
    daily_budget_usd: float = Field(ge=0, le=100)
    language: str = Field(default="vi", max_length=10)
    barge_in: bool = True
    voice_roles: list[str] | None = None     # G9: roles holding voice.use (None = unchanged)
class SettingsIn(BaseModel):
    workspace: WsIn
    catalog: list[dict] | None = None
class AgentIn(BaseModel):
    allowed_models: list[str] | None = None
    default_model: str | None = None
class SessIn(BaseModel):
    model: str
    agent_id: str = Field(pattern=r"^agent-\d+$")
    thread_id: str | None = Field(default=None, min_length=8, max_length=100)   # None: server creates a voice thread (viewers cannot POST /threads)
    sdp: str | None = Field(default=None, max_length=20000)
    # "dictation": the Mic button in the composer — same selected live model, input transcription only, no thread/tools/transcript
    # "readaloud": the message action bar's Read aloud — same live model speaks a given text verbatim, no thread/tools
    purpose: Literal["conversation", "dictation", "readaloud"] = "conversation"
class DelegIn(BaseModel):
    voice_session_id: str
    call_id: str = Field(max_length=100)
    request: str = Field(min_length=1, max_length=4000)
    context: str | None = Field(default=None, max_length=4000)
class TrItem(BaseModel):
    role: str = Field(pattern=r"^(user|assistant)$")
    text: str = Field(min_length=1, max_length=8000)
    t_start: float | None = None
    t_end: float | None = None
    phase: str = Field(default="turn", pattern=r"^(turn|preamble|readback)$")   # preamble/readback belong to a delegated call
    call_id: str | None = Field(default=None, max_length=100)
    truncated: bool = False                       # barge-in: text = only what was PLAYED to the user
    played_ms: int | None = Field(default=None, ge=0, le=3_600_000)
    generated_ms: int | None = Field(default=None, ge=0, le=3_600_000)
    full_chars: int | None = Field(default=None, ge=0, le=100_000)
class TrIn(BaseModel):
    voice_session_id: str
    items: list[TrItem] = Field(max_length=200)
class CancelIn(BaseModel):
    reason: str = Field(default="barge_in", max_length=40)
class EndIn(BaseModel):
    seconds: float = Field(ge=0, le=7200)
    reason: str = Field(default="user", max_length=40)

def _own(s, sid, u):
    v = s["sessions"].get(sid)
    if not v or v["user_id"] != u.get("id"): raise HTTPException(404, "voice session not found")
    return v

async def _run_on_thread(request, v, text):
    """Delegation = a normal LangGraph run of the thread's agent, through the real /api/v1 (gateway caps, trace, HITL all apply)."""
    async with _fwd(request) as h:
        r = await h.post(f"/api/v1/threads/{v['thread_id']}/runs", json={"assistant_id": v["agent_id"], "input": {"messages": [
            {"type": "human", "content": text, "additional_kwargs": {"modality": "voice", "voice_session_id": v["id"], "delegated": True}}]},
            "metadata": {"source": "voice", "voice_session_id": v["id"]}})
        if r.status_code >= 400: raise HTTPException(r.status_code, f"Agent OS run refused: {r.text[:200]}")
        rid = r.json()["run_id"]; await h.get(f"/api/v1/threads/{v['thread_id']}/runs/{rid}/join")
        return rid, await _run_result(h, v["thread_id"], rid)

async def _run_result(h, tid, rid):
    run = (await h.get(f"/api/v1/threads/{tid}/runs/{rid}")).json()
    out = {"run_id": rid, "status": run["status"], "reply": (run.get("output") or {}).get("reply"), "approval": None}
    if run["status"] == "interrupted":
        st = (await h.get(f"/api/v1/threads/{tid}/state")).json()
        ints = [i["value"] if isinstance(i, dict) and "value" in i else i for t in st.get("tasks") or [] for i in t.get("interrupts") or []]   # G16 shape
        out["approval"] = {"approval_id": (run.get("output") or {}).get("approval_id"), "interrupts": ints}
        out["reply"] = None
    return out

async def _flush_pending(request, s, v):
    if not v.get("pending"): return
    items, v["pending"] = v["pending"], []
    await _write_transcript(request, v, items)

_ATTACH_LOCKS: dict = {}
async def _attach_voice(request, v, c, it):
    """Store what the voice model SAID around a delegated run on that run's answer message (additional_kwargs), in place:
    preamble ("Let me check…", spoken while the run worked) and read-back (the spoken result). Fixes the misordered filler and
    the duplicated read-back AIMessage. Falls back to a normal voice line if the answer message is gone."""
    key = "voice_preamble" if it["phase"] == "preamble" else "voice_readback"
    lock = _ATTACH_LOCKS.setdefault(v["thread_id"], asyncio.Lock())
    import server as SV
    async with lock:
        msgs = (await SV._state(await SV._get_thread(v["thread_id"]))).get("values", {}).get("messages", [])
        m = next((x for x in msgs if x.get("id") == c.get("ai_message_id")), None)
        if not m:
            await _write_transcript(request, v, [it]); return
        kw = {**(m.get("additional_kwargs") or {}), key: ((m.get("additional_kwargs") or {}).get(key, "") + " " + it["text"]).strip(),
              "voice_session_id": v["id"], "call_id": it.get("call_id"), "run_id": c.get("run_id"), **_trunc(it, key + "_")}
        await SV.th_update_state(v["thread_id"], {"values": {"messages": [{"type": "ai", "id": m["id"], "content": m.get("content", ""), "additional_kwargs": kw}]}})

def _trunc(i, prefix="voice_"):
    """Interrupted assistant turn: flag + timing, and `metadata` (assistant-ui exposes additional_kwargs.metadata as message.metadata.custom)."""
    if not i.get("truncated") or i.get("role") != "assistant": return {}
    d = {f"{prefix}truncated": True, f"{prefix}played_ms": i.get("played_ms"), f"{prefix}generated_ms": i.get("generated_ms"), f"{prefix}full_chars": i.get("full_chars")}
    return {**d, "metadata": {"voice_truncated": True, "voice_played_ms": i.get("played_ms"), "voice_generated_ms": i.get("generated_ms")}}

async def _write_transcript(request, v, items):
    msgs = [{"type": "human" if i["role"] == "user" else "ai", "content": i["text"],
             "additional_kwargs": {"modality": "voice", "voice_session_id": v["id"], "provider": v["provider"], "model": v["model"],
                                   "t_start": i.get("t_start"), "t_end": i.get("t_end"), **_trunc(i), **(i.get("extra") or {})}} for i in items]
    # Written in-process through server.py's own state endpoint function (same RLS/user context as the caller), so a viewer that holds
    # voice.use can persist its own call without being granted generic POST /api/v1 rights.
    import server as SV
    try: await SV.th_update_state(v["thread_id"], {"values": {"messages": msgs}})
    except HTTPException as e:
        if e.status_code == 409: return "buffered"   # unbound thread (should not happen since G15)
        raise
    await _auto_title(v["thread_id"], items)
    return "written"

VOICE_TITLE_PLACEHOLDER = "Cuộc gọi thoại"
async def _auto_title(thread_id, items):
    """Voice thread title = the first thing the user said (≤ 60 chars), like a typed chat's first message; only while the title is auto."""
    first = next((i["text"].strip() for i in items if i.get("role") == "user" and i.get("text", "").strip()), None)
    if not first: return
    t = await A.q("SELECT metadata FROM lg_threads WHERE thread_id=%s", (thread_id,), one=True)
    md = (t or {}).get("metadata") or {}
    if not md.get("title_auto") and md.get("title") and not str(md.get("title", "")).startswith(("Voice call", VOICE_TITLE_PLACEHOLDER)): return
    title = first if len(first) <= 60 else first[:57].rsplit(" ", 1)[0] + "…"
    await A.q("UPDATE lg_threads SET metadata=metadata || %s WHERE thread_id=%s", (Jsonb({"title": title, "title_auto": False, "voice": True}), thread_id))

def router():
    r = APIRouter()
    # G9: viewers may POST to the voice session/transcript endpoints ONLY if the workspace grants them voice.use (checked per endpoint).
    _adm.allow_viewer_post("/api/voice/session", "/api/voice/transcript")

    @r.get("/api/voice/config")
    async def voice_config(agent_id: str | None = None):
        u = _user(); s = _load(); _expire(s)
        allowed, default, src = merged(s, agent_id); mins, usd = usage_today(s, u.get("id")); ws = s["workspace"]
        cat = {m["id"]: m for m in s["catalog"]}
        return {"phase0_mock": not REAL_PROVIDERS or _key() is None, "real_providers": sorted(REAL_PROVIDERS) if _key() else [], "agent_id": agent_id, "allowed": allowed, "default_model": default, "source": src,
                "models": [{k: cat[m].get(k) for k in ("id", "provider", "display_name", "transport", "est_usd_per_min", "voice")} for m in allowed],
                "barge_in": ws["barge_in"], "language": ws["language"], "can_use": u.get("role") in ws["voice_roles"],
                "caps": {"max_session_min": ws["max_session_min"], "daily_min_per_user": ws["daily_min_per_user"],
                         "daily_budget_usd": ws["daily_budget_usd"], "used_min_today": mins, "spent_usd_today": usd}}

    @r.get("/api/settings/voice")
    async def settings_get():
        u = _user(); s = _load()
        return {"catalog": s["catalog"], "workspace": s["workspace"], "agents": s["agents"], "audit": s["audit"][-20:], "store": store_info(),
                "can_edit": u.get("role") == "admin", "can_override_agent": u.get("role") in ("admin", "editor")}

    @r.put("/api/settings/voice")
    async def settings_put(b: SettingsIn):
        u = _user()
        if u.get("role") != "admin": raise HTTPException(403, "admin only")
        with _LOCK:
            s = _load()
            if b.catalog is not None:
                for m in b.catalog:
                    if not {"id", "provider", "transport"} <= set(m): raise HTTPException(422, "catalog rows need id, provider, transport")
                _audit(s, u, "catalog", [m["id"] for m in s["catalog"]], [m["id"] for m in b.catalog]); s["catalog"] = b.catalog
            ids = {m["id"] for m in s["catalog"]}; w = b.workspace.model_dump()
            if w["voice_roles"] is None: w.pop("voice_roles")
            elif not set(w["voice_roles"]) <= set(_adm.ROLES) or "admin" not in w["voice_roles"]:
                raise HTTPException(422, f"voice_roles must be a subset of {sorted(_adm.ROLES)} and keep 'admin'")
            bad = [m for m in w["allowed_models"] + [w["default_model"]] if m not in ids]
            if bad: raise HTTPException(422, f"unknown voice model(s): {bad}")
            if w["default_model"] not in w["allowed_models"]: raise HTTPException(422, "default_model must be in allowed_models")
            old = {k: s["workspace"].get(k) for k in w}; s["workspace"].update(w); _audit(s, u, "workspace", old, w); _save(s)
        return await settings_get()

    @r.put("/api/settings/voice/agents/{graph_id}")
    async def agent_put(graph_id: str, b: AgentIn):
        u = _user()
        if u.get("role") not in ("admin", "editor"): raise HTTPException(403, "admin or editor only")
        with _LOCK:
            s = _load(); ws_allowed = s["workspace"]["allowed_models"]
            if b.allowed_models and any(m not in ws_allowed for m in b.allowed_models): raise HTTPException(422, "agent allow-list must be a subset of the workspace list")
            if b.default_model and b.default_model not in (b.allowed_models or ws_allowed): raise HTTPException(422, "agent default must be in its allow-list")
            old = s["agents"].get(graph_id); new = {k: v for k, v in b.model_dump().items() if v}
            if new: s["agents"][graph_id] = new
            else: s["agents"].pop(graph_id, None)
            _audit(s, u, f"agent:{graph_id}", old, new); _save(s)
        return await settings_get()

    @r.post("/api/voice/session")
    async def voice_session(b: SessIn, request: Request):
        u = _user()
        if u.get("role") not in _load()["workspace"]["voice_roles"]: raise HTTPException(403, "voice.use not granted")
        # G15: a voice-first thread is bound to its agent BEFORE any transcript write (server.bind_thread = same rule as a first run)
        import server as SV
        dictation = b.purpose in ("dictation", "readaloud")
        if dictation:
            b.thread_id = None   # dictation / read-aloud never touch a thread: the text lands in the composer and the user decides to send it
            await SV._get_assistant(b.agent_id)
        else:
            if b.thread_id is None:
                b.thread_id = (await SV.th_create(SV.ThreadIn(metadata={"source": "chat", "graph_id": b.agent_id, "title": VOICE_TITLE_PLACEHOLDER,
                                                                       "title_auto": True, "voice": True, "created_by_voice": True})))["thread_id"]
            t = await A.q("SELECT * FROM lg_threads WHERE thread_id=%s AND workspace_id=%s", (b.thread_id, SV._ws()), one=True)
            if not t: raise HTTPException(404, "thread not found")
            ag = await SV._get_assistant(b.agent_id)
            if t["agent_id"] and t["agent_id"] != ag["agent_id"]: raise HTTPException(409, "thread belongs to another agent")
            await SV.bind_thread(t, b.agent_id)
            if not (t.get("metadata") or {}).get("voice"):   # mark the thread as a voice thread (sidebar icon); title stays unless it is auto
                await A.q("UPDATE lg_threads SET metadata=metadata || %s WHERE thread_id=%s", (Jsonb({"voice": True}), b.thread_id))
        with _LOCK:
            s = _load(); _expire(s); ws = s["workspace"]
            if u.get("role") not in ws["voice_roles"]: raise HTTPException(403, "voice.use not granted")
            allowed, _, _ = merged(s, b.agent_id)
            if b.model not in allowed: raise HTTPException(403, f"model '{b.model}' is not allowed for {b.agent_id}")
            mins, usd = usage_today(s, u.get("id"))
            if usd >= ws["daily_budget_usd"]:
                raise HTTPException(402, {"code": "budget", "message": f"Daily voice budget reached (${usd:.2f} / ${ws['daily_budget_usd']:.2f})"})
            if mins >= ws["daily_min_per_user"]:
                raise HTTPException(402, {"code": "minutes", "message": f"Daily voice minutes reached ({mins:.1f} / {ws['daily_min_per_user']:.0f} min)"})
            if sum(1 for v in s["sessions"].values() if v["user_id"] == u.get("id") and v["status"] == "active") >= ws["max_concurrent_per_user"]:
                raise HTTPException(429, "another voice session is still active")
            m = next(x for x in s["catalog"] if x["id"] == b.model)
            real = m["provider"] in REAL_PROVIDERS and _key() is not None
            if real and s.get("real_spend_usd", 0.0) >= A8_ABORT_USD:
                raise HTTPException(402, {"code": "a8_cap", "message": f"A8 real-call budget reached (${s.get('real_spend_usd', 0):.4f} ≥ ${A8_ABORT_USD:.2f})"})
            left_min = min(ws["max_session_min"], ws["daily_min_per_user"] - mins, (ws["daily_budget_usd"] - usd) / max(m["est_usd_per_min"], 1e-6))
            if real: left_min = min(left_min, 1.5)   # A8: each real session ≤ 90 s
            sid = "vs_" + secrets.token_hex(8); exp = int(time.time()) + int(left_min * 60) + 30
            s["sessions"][sid] = {"id": sid, "user_id": u.get("id"), "username": u.get("username"), "thread_id": b.thread_id, "agent_id": b.agent_id,
                                  "provider": m["provider"], "model": m["id"], "started_at": _now().isoformat(), "t0": time.time(), "status": "active",
                                  "max_session_s": int(left_min * 60), "est_usd_per_min": m["est_usd_per_min"], "seconds": None, "cost_usd": None, "pending": [],
                                  "real": real, "usage_usd": 0.0, "usage_msgs": 0, "usage_last": None, "purpose": b.purpose}
            _save(s)
        # Phase 1 (real): openai -> client.live.create(session={model, delegation:{type:"client"}, ...}, transport={type:"webrtc", sdp}) -> sdp answer;
        #                 google -> client.auth_tokens.create(uses=1, expire_time, new_session_expire_time, live_connect_constraints=...) -> token.name
        if real:
            try: token, exp = await asyncio.to_thread(_mint_gemini, m["id"], s["sessions"][sid]["max_session_s"], m.get("voice"), b.purpose)
            except Exception as e:  # noqa: BLE001
                with _LOCK:
                    st = _load(); st["sessions"][sid].update(status="error", seconds=0, cost_usd=0); _save(st)
                raise HTTPException(502, f"ephemeral token mint failed: {_mask(e)[:300]}")
            cred = {"type": "ephemeral_token", "value": token}
        else:
            cred = ({"type": "sdp_answer", "value": "v=0 mock-sdp-answer"} if m["provider"] == "openai" else
                    {"type": "ephemeral_token", "value": "mock-auth_tokens/" + secrets.token_hex(6)})
        return {"voice_session_id": sid, "thread_id": b.thread_id, "adapter": "provider" if real else "mock", "mock": not real,
                "ws_url": "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContentConstrained" if real else None, "provider": m["provider"], "model": m["id"], "transport": m["transport"],
                "credential": {**cred, "expires_at": exp}, "max_session_s": s["sessions"][sid]["max_session_s"], "barge_in": ws["barge_in"],
                "language": ws["language"], "voice": m.get("voice"), "purpose": b.purpose,
                "tools": [] if dictation else [{"name": "agent_os_delegate", "behavior": "NON_BLOCKING", "scheduling": "WHEN_IDLE",
                           "description": "Hand the user's task to the Agent OS agent of this thread (tools, history, HITL, budget stay in Agent OS).",
                           "parameters": {"request": "string", "context": "string?"}}]}

    @r.get("/api/voice/sessions")
    async def voice_sessions(status: str | None = None):
        u = _user(); s = _load(); _expire(s)
        return [{k: v.get(k) for k in ("id", "thread_id", "agent_id", "provider", "model", "started_at", "status", "seconds", "cost_usd", "end_reason")}
                for v in s["sessions"].values() if v["user_id"] == u.get("id") and (status is None or v["status"] == status)][-50:]

    @r.post("/api/voice/session/{sid}/end")
    async def voice_end(sid: str, b: EndIn, request: Request):
        u = _user()
        with _LOCK:
            s = _load(); v = _own(s, sid, u)
            if v["status"] == "active":
                sec = min(b.seconds, v["max_session_s"])
                cost = v["usage_usd"] if v.get("real") else sec / 60 * v["est_usd_per_min"]
                v.update(status="ended", end_reason=b.reason, seconds=round(sec, 1), cost_usd=round(cost, 6), ended_at=_now().isoformat())
            _save(s)
        return {k: v[k] for k in ("id", "status", "seconds", "cost_usd")}

    TOKEN_REISSUE_MAX = 8
    @r.post("/api/voice/session/{sid}/token")
    async def voice_token(sid: str):
        """GĐ4 session resumption: Gemini ephemeral tokens open ONE connection (uses=1; a resumed socket with the same token gets
        1011 "Token has been used too many times"). For the caller's own ACTIVE session only, mint a fresh token with the SAME locked
        config, bounded by the session's remaining time, at most TOKEN_REISSUE_MAX times. Budget and A8 caps still apply."""
        u = _user()
        with _LOCK:
            s = _load(); _expire(s); v = _own(s, sid, u)
            if v["status"] != "active": raise HTTPException(409, "voice session is not active")
            if v.get("reissues", 0) >= TOKEN_REISSUE_MAX: raise HTTPException(429, "token reissue limit reached for this session")
            left = int(v["t0"] + v["max_session_s"] - time.time())
            if left < 5: raise HTTPException(409, "voice session time is used up")
            if v.get("real") and s.get("real_spend_usd", 0.0) >= A8_ABORT_USD:
                raise HTTPException(402, {"code": "a8_cap", "message": "A8 real-call budget reached"})
            v["reissues"] = v.get("reissues", 0) + 1; n = v["reissues"]; real = v.get("real"); model = v["model"]; purpose = v.get("purpose") or "conversation"
            _save(s)
        if not real:
            return {"value": "mock-auth_tokens/" + secrets.token_hex(6), "expires_at": int(time.time()) + left + 30, "reissues": n}
        m = next((x for x in _load()["catalog"] if x["id"] == model), {})
        try: token, exp = await asyncio.to_thread(_mint_gemini, model, left, m.get("voice"), purpose)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"ephemeral token mint failed: {_mask(e)[:300]}")
        return {"value": token, "expires_at": exp, "reissues": n}

    class UsageIn(BaseModel):
        usage: dict
    @r.post("/api/voice/session/{sid}/usage")
    async def voice_usage(sid: str, b: UsageIn):
        """Browser forwards every Gemini usageMetadata. Server prices it (official table) and enforces the A8 cap."""
        u = _user()
        with _LOCK:
            s = _load(); v = _own(s, sid, u)
            c = _usage_cost(v["model"], b.usage) if v.get("real") else 0.0
            v["usage_usd"] = v.get("usage_usd", 0.0) + c; v["usage_msgs"] = v.get("usage_msgs", 0) + 1; v["usage_last"] = b.usage
            s["real_spend_usd"] = s.get("real_spend_usd", 0.0) + c
            s.setdefault("usage_log", []).append({"at": _now().isoformat(), "sid": sid, "cost": c, "usage": b.usage}); s["usage_log"] = s["usage_log"][-500:]
            total = s["real_spend_usd"]; _save(s)
        return {"session_usd": round(v["usage_usd"], 6), "real_spend_usd": round(total, 6), "stop": total >= A8_ABORT_USD, "abort_at": A8_ABORT_USD}

    @r.get("/api/voice/spend")
    async def voice_spend():
        _user(); s = _load()
        real = [v for v in s["sessions"].values() if v.get("real")]
        return {"real_spend_usd": round(s.get("real_spend_usd", 0.0), 6), "abort_at": A8_ABORT_USD, "hard_cap": A8_HARD_USD,
                "real_providers": sorted(REAL_PROVIDERS), "key_loaded": _key() is not None,
                "sessions": [{k: v.get(k) for k in ("id", "model", "started_at", "status", "seconds", "usage_usd", "usage_msgs", "end_reason")} for v in real]}

    @r.post("/api/voice/delegate")
    async def voice_delegate(b: DelegIn, request: Request):
        u = _user(); s = _load(); v = _own(s, b.voice_session_id, u)
        if v["status"] != "active": raise HTTPException(409, "voice session is not active")
        with _LOCK:
            s = _load(); s["calls"][b.call_id] = {"voice_session_id": v["id"], "thread_id": v["thread_id"], "run_id": None, "status": "starting",
                                                 "request": b.request, "voice": [], "ai_message_id": None}; _save(s)
        async with _fwd(request) as h:
            r = await h.post(f"/api/v1/threads/{v['thread_id']}/runs", json={"assistant_id": v["agent_id"], "input": {"messages": [
                {"type": "human", "id": f"voice-{b.call_id}", "content": b.request,
                 "additional_kwargs": {"modality": "voice", "voice_session_id": v["id"], "delegated": True, "call_id": b.call_id}}]},
                "metadata": {"source": "voice", "voice_session_id": v["id"], "call_id": b.call_id}})
            if r.status_code >= 400:
                with _LOCK:
                    s = _load(); s["calls"][b.call_id]["status"] = "error"; _save(s)
                raise HTTPException(r.status_code, f"Agent OS run refused: {r.text[:200]}")
            rid = r.json()["run_id"]
            with _LOCK:
                s = _load(); c = s["calls"][b.call_id]; c.update(run_id=rid, status="running"); early_cancel = c.get("cancel_requested"); _save(s)
            if early_cancel: await h.post(f"/api/v1/threads/{v['thread_id']}/runs/{rid}/cancel", params={"action": "interrupt"})
            await h.get(f"/api/v1/threads/{v['thread_id']}/runs/{rid}/join")
            out = await _run_result(h, v["thread_id"], rid)
            msgs = (await h.get(f"/api/v1/threads/{v['thread_id']}/state")).json().get("values", {}).get("messages", [])
        ids = [m.get("id") for m in msgs]; i = ids.index(f"voice-{b.call_id}") if f"voice-{b.call_id}" in ids else -1
        answer = next((m for m in reversed(msgs[i + 1:]) if m.get("type") == "ai" and not m.get("tool_calls")), None) if i >= 0 else None
        with _LOCK:
            s = _load(); c = s["calls"][b.call_id]; cancelled = bool(c.get("cancelled"))
            c.update(status="cancelled" if cancelled else out["status"], ai_message_id=(answer or {}).get("id"))
            buffered, c["voice"] = c.get("voice") or [], []
            vv = s["sessions"][v["id"]]; pending = vv.get("pending") or []; vv["pending"] = []; _save(s)
        if pending: await _write_transcript(request, vv, pending)
        for it in buffered: await _attach_voice(request, vv, c, it)
        if cancelled: out = {**out, "status": "cancelled", "reply": None}
        return {"call_id": b.call_id, **out}

    @r.post("/api/voice/delegate/{call_id}/cancel")
    async def voice_delegate_cancel(call_id: str, b: CancelIn, request: Request):
        """Barge-in while a delegated run is in flight: proper run cancel (server.py /runs/{rid}/cancel) + a transcript note."""
        u = _user()
        with _LOCK:
            s = _load(); c = s["calls"].get(call_id)
            if not c: raise HTTPException(404, "unknown call")
            v = _own(s, c["voice_session_id"], u)
            if c["status"] not in ("starting", "running") or c.get("cancelled"): return {"call_id": call_id, "cancelled": False, "status": c["status"]}
            c["cancelled"] = True; c["cancel_requested"] = True; c["cancel_reason"] = b.reason; rid = c["run_id"]; _save(s)
        async with _fwd(request) as h:
            if rid:
                await h.post(f"/api/v1/threads/{c['thread_id']}/runs/{rid}/cancel", params={"action": "interrupt"})
                for _ in range(30):   # write the note only once the graph has stopped writing (no concurrent state update)
                    if (await h.get(f"/api/v1/threads/{c['thread_id']}/runs/{rid}")).json().get("status") in ("interrupted", "success", "error"): break
                    await asyncio.sleep(0.1)
        note = ("⏹ Đã huỷ yêu cầu giao cho Agent OS (người dùng ngắt lời)." if s["workspace"].get("language") == "vi"
                else "⏹ Delegated request cancelled — the user interrupted.")
        await _write_transcript(request, v, [{"role": "assistant", "text": note, "extra": {"voice_event": "delegate_cancelled", "call_id": call_id,
                                                                                           "run_id": rid, "reason": b.reason, "request": c.get("request")}}])
        return {"call_id": call_id, "cancelled": True, "run_id": rid}

    @r.get("/api/voice/delegate/{call_id}")
    async def voice_delegate_status(call_id: str, request: Request):
        u = _user(); s = _load(); c = s["calls"].get(call_id)
        if not c: raise HTTPException(404, "unknown call")
        _own(s, c["voice_session_id"], u)
        async with _fwd(request) as h:
            runs = (await h.get(f"/api/v1/threads/{c['thread_id']}/runs", params={"limit": 5})).json()
            ids = [x["run_id"] for x in runs]; i = ids.index(c["run_id"]) if c["run_id"] in ids else len(ids)
            later = runs[:i]   # runs created after the delegated one (e.g. the HITL resume run)
            last = later[-1] if later else None
            for x in reversed(later):
                if x["status"] in ("success", "error", "interrupted"): last = x
            if not last: return {"call_id": call_id, **(await _run_result(h, c["thread_id"], c["run_id"]))}
            return {"call_id": call_id, "resumed_by": last["run_id"], **(await _run_result(h, c["thread_id"], last["run_id"]))}

    @r.post("/api/voice/transcript")
    async def voice_transcript(b: TrIn, request: Request):
        u = _user(); s = _load(); v = _own(s, b.voice_session_id, u)
        if v.get("purpose") in ("dictation", "readaloud"): raise HTTPException(409, "dictation sessions have no transcript (the text goes to the composer)")
        items = [i.model_dump() for i in b.items]
        plain = []
        for it in items:
            if it["phase"] == "turn" or not it["call_id"]: plain.append(it); continue
            with _LOCK:
                st = _load(); c = st["calls"].get(it["call_id"])
                if not c or c.get("cancelled"): plain.append(it); continue
                if not c.get("ai_message_id"):
                    if c["status"] in ("starting", "running"): c.setdefault("voice", []).append(it); _save(st); continue
                    plain.append(it); continue
            await _attach_voice(request, v, c, it)
        items = plain
        if not items: return {"status": "attached", "n": len(b.items)}
        res = await _write_transcript(request, v, items)
        if res == "buffered":
            with _LOCK:
                s = _load(); s["sessions"][v["id"]].setdefault("pending", []).extend(items); _save(s)
        return {"status": res, "n": len(items)}

    @r.get("/chat", include_in_schema=False)
    async def chat_root(): return RedirectResponse("/chat/")
    @r.get("/chat/{path:path}", include_in_schema=False)
    async def chat_static(path: str, request: Request):
        f = (DIST / path).resolve()
        if not str(f).startswith(str(DIST.resolve())) or not f.is_file(): f = DIST / "index.html"
        if not f.exists(): raise HTTPException(404, "front-end UI not built")
        resp = FileResponse(f)
        resp.headers.update({"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
                             "Permissions-Policy": "microphone=(self), camera=(), geolocation=()",
                             "Cache-Control": "no-cache" if f.name == "index.html" else "public, max-age=31536000, immutable"})
        return resp
    import chat_settings   # GĐ3.5–3.7: /api/chat/config (model allow-list, generative-UI flag, quota), /api/settings/chat
    r.include_router(chat_settings.router())
    return r
