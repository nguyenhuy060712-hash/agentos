"""GĐ13.28 an agent creates its own schedule ("nhắc tôi mỗi thứ Hai 8 giờ"): tools lich_tao / lich_danh_sach / lich_xoa / lich_sua on `agent_crons` (migration 0226).
Trust (the core): a run from outside (stranger, mail, public channel), an API-key / service-key / channel / schedule run and the demo workspace can NOT create or edit a schedule;
creating / editing waits for the person's approval (HITL interrupt, the confirmation lists the next 3 run times); every tool only sees and touches the schedules this person made
through THIS agent (`owner_user_id`), so user A never lists / deletes user B's. Times are read in the workspace time zone (agent_clock, default ICT).
A schedule made here runs like any UI schedule (studio.run_crons: same global off switch, end_time, one-shot disables itself)."""
import datetime as dt, json, os, re, time
from zoneinfo import ZoneInfo
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool as lc_tool

MIN_INTERVAL_MIN = 15
HUMAN_KINDS = ("session", "basic", "user")   # a signed-in person (cookie / basic); never api_key, agent_key, channel, run, system

def now_utc() -> dt.datetime: return dt.datetime.now(dt.timezone.utc)
def max_per_agent() -> int: return int(os.environ.get("AOS_MAX_AGENT_SCHEDULES", "20") or 20)
def max_per_user() -> int: return int(os.environ.get("AOS_MAX_USER_SCHEDULES", "20") or 20)

async def _q(*a, **k):
    import deepagent_api as A
    return await A.q(*a, **k)

def refusal(config=None) -> tuple[dict | None, str | None]:
    """-> (caller, None) when this run may manage schedules, else (None, reason)."""
    import caller, run_trust
    from agent_fields import is_demo_caller
    if run_trust.is_external(config): return None, "Lượt chạy từ nguồn bên ngoài không được tạo hoặc sửa lịch / external-source runs cannot manage schedules."
    if is_demo_caller(): return None, "Không có trong bản demo / not available in the demo workspace."
    c = caller.current()
    if c.get("kind") not in HUMAN_KINDS or c.get("user_id") is None:
        return None, "Chỉ người đang đăng nhập mới tạo hoặc sửa lịch được (không dùng khóa API, kênh, thư hay lịch) / only a signed-in person can manage schedules."
    if c.get("role") not in ("admin", "editor"): return None, "Cần quyền editor trở lên / editor role needed."
    return c, None

def tz_now(ws=None) -> str:
    """Workspace time zone from agent_clock's cache (DateTimeMiddleware fills it before every model call); default ICT."""
    import agent_clock
    hit = agent_clock._CACHE.get(ws)
    return hit[1] if hit and hit[0] > time.time() else agent_clock.DEFAULT_TZ

def parse_at(at: str, tz: str, now: dt.datetime) -> dt.datetime:
    """ISO 8601 -> aware UTC, rounded UP to the next full minute (no zone in the text = workspace zone); 1 minute .. 365 days ahead. ValueError otherwise."""
    try: d = dt.datetime.fromisoformat(str(at).strip().replace("Z", "+00:00"))
    except ValueError: raise ValueError("`at` phải là ISO 8601, ví dụ 2026-11-03T09:00") from None
    if d.tzinfo is None: d = d.replace(tzinfo=ZoneInfo(tz))
    d = d.astimezone(dt.timezone.utc)
    if d.second or d.microsecond: d = d.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    n = now.astimezone(dt.timezone.utc).replace(second=0, microsecond=0)
    if d < n + dt.timedelta(minutes=1): raise ValueError("Thời điểm đã qua hoặc quá gần (tối thiểu 1 phút nữa)")
    if d > n + dt.timedelta(days=365): raise ValueError("Chỉ đặt lịch tối đa 365 ngày tới")
    return d

def upcoming(cron, tz, run_once_at, now, n=3, end_time=None) -> list[dt.datetime]:
    import evals
    out, t = [], now
    for _ in range(n):
        r = evals.next_run_date(cron, tz, run_once_at, t, None, end_time)
        if not r: break
        out.append(r); t = r
        if run_once_at or cron == "@once": break
    return out

def check_interval(cron, tz, now):
    """Recurring: no two of the next 12 runs closer than 15 minutes; and it must fire at all."""
    runs = upcoming(cron, tz, None, now, 12)
    if not runs: raise ValueError("Lịch này không bao giờ chạy")
    if any((b - a).total_seconds() < MIN_INTERVAL_MIN * 60 for a, b in zip(runs, runs[1:])): raise ValueError(f"Lịch lặp tối thiểu {MIN_INTERVAL_MIN} phút một lần")

def _fmt(times, tz) -> str:
    z = ZoneInfo(tz); return "; ".join(t.astimezone(z).strftime("%a %d/%m/%Y %H:%M") for t in times) or "-"

def describe(tc, _state=None, _rt=None) -> str:
    """Approval card text: what will be created + the next 3 run times (workspace zone)."""
    import admin, evals
    a = tc.get("args") or {}; tz = tz_now(admin.cur_ws())
    try:
        if a.get("at"): runs = [parse_at(a["at"], tz, now_utc())]; kind = "một lần / once"
        elif a.get("cron"): runs = upcoming(evals.norm_schedule(a["cron"], tz)[0], tz, None, now_utc()); kind = f"cron {a['cron']}"
        else: runs, kind = [], "(không đổi giờ / time unchanged)"
    except Exception as e: runs, kind = [], f"không hợp lệ / invalid: {str(e)[:80]}"   # noqa: BLE001
    verb = "Tạo lịch / Create schedule" if tc.get("name") == "lich_tao" else "Sửa lịch / Edit schedule"
    return f"{verb} ({kind}, {tz}): {str(a.get('message') or '')[:200]}\nCác lần chạy tới / next runs: {_fmt(runs, tz)}"

def interrupt_cfg(names) -> dict:
    return {n: {"allowed_decisions": ["approve", "edit", "reject"], "description": describe} for n in ("lich_tao", "lich_sua") if n in names}

def _row(r, tz) -> dict:
    import evals
    nxt = None
    if r["enabled"] and not r.get("paused"):
        try: nxt = evals.next_run_date(r["cron"], r.get("timezone") or tz, r.get("run_once_at"), now_utc(), r.get("last_run"), r.get("end_time"))
        except ValueError: nxt = None
    return {"id": r["id"], "message": r["message"], "cron": r["cron"], "at": r["run_once_at"].isoformat() if r.get("run_once_at") else None, "timezone": r.get("timezone"),
            "enabled": bool(r["enabled"]), "paused": bool(r.get("paused")), "new_thread": r["new_thread"], "next_run": nxt.isoformat() if nxt else None}

async def _count(col, val) -> int:
    return (await _q(f"SELECT count(*) n FROM agent_crons WHERE {col}=%s AND source='agent' AND enabled", (val,), one=True))["n"]

async def create(agent_id, c, config, cron="", at="", message="", new_thread=False) -> str:
    import evals, admin, run_trust
    message = (message or "").strip()
    if not message or len(message) > 4000: raise ValueError("`message` bắt buộc, tối đa 4000 ký tự")
    if bool(cron and cron.strip()) == bool(at and at.strip()): raise ValueError("Cho đúng một trong `cron` (5 trường) hoặc `at` (ISO 8601)")
    tz = tz_now(admin.cur_ws()); now = now_utc()
    if at and at.strip(): stored, ro = "@once", parse_at(at, tz, now)
    else:
        stored, _, ro = evals.norm_schedule(" ".join(cron.split()), tz); check_interval(stored, tz, now)
    if await _count("agent_id", agent_id) >= max_per_agent(): raise ValueError(f"Agent này đã có {max_per_agent()} lịch (AOS_MAX_AGENT_SCHEDULES)")
    if await _count("owner_user_id", c["user_id"]) >= max_per_user(): raise ValueError(f"Bạn đã có {max_per_user()} lịch do agent tạo (AOS_MAX_USER_SCHEDULES)")
    tid = None if new_thread else (re.sub(r"^a\d+:", "", run_trust.thread_key(config) or "") or None)   # the graph thread is "a<id>:<chat thread>"; run_agent wants the chat thread
    r = await _q("INSERT INTO agent_crons(agent_id,name,cron,message,new_thread,timezone,run_once_at,source,owner_user_id,created_by,thread_id) VALUES(%s,%s,%s,%s,%s,%s,%s,'agent',%s,%s,%s) RETURNING id",
                 (agent_id, message[:60], stored, message, bool(new_thread or not tid), tz, ro, c["user_id"], c["user_id"], tid), one=True)
    return json.dumps({"ok": True, "id": r["id"], "cron": stored, "timezone": tz, "next_runs": [t.isoformat() for t in upcoming(stored, tz, ro, now)]}, ensure_ascii=False)

async def update(agent_id, c, sid, cron="", at="", message="", paused=None) -> str:
    import evals, admin
    r = await _q("SELECT * FROM agent_crons WHERE id=%s AND agent_id=%s AND source='agent' AND owner_user_id=%s AND locked_reason IS NULL", (sid, agent_id, c["user_id"]), one=True)
    if not r: raise ValueError("Không có lịch này của bạn")
    tz = r.get("timezone") or tz_now(admin.cur_ws()); now = now_utc(); stored, ro = r["cron"], r.get("run_once_at")
    if bool(cron and cron.strip()) and bool(at and at.strip()): raise ValueError("Chỉ một trong `cron` hoặc `at`")
    if at and at.strip(): stored, ro = "@once", parse_at(at, tz, now)
    elif cron and cron.strip(): stored, _, ro = evals.norm_schedule(" ".join(cron.split()), tz); check_interval(stored, tz, now)
    msg = (message or "").strip() or r["message"]
    if len(msg) > 4000: raise ValueError("`message` tối đa 4000 ký tự")
    await _q("UPDATE agent_crons SET cron=%s, run_once_at=%s, message=%s, name=%s, paused=%s, enabled=%s WHERE id=%s",
             (stored, ro, msg, msg[:60], r["paused"] if paused is None else bool(paused), True if (at or cron) else r["enabled"], sid))
    return json.dumps({"ok": True, "id": sid, "cron": stored, "next_runs": [t.isoformat() for t in upcoming(stored, tz, ro, now)]}, ensure_ascii=False)

def tools(agent_id=None) -> dict:
    @lc_tool
    async def lich_tao(message: str, cron: str = "", at: str = "", new_thread: bool = False, config: RunnableConfig = None) -> str:
        """Create a schedule for THIS agent ("nhắc tôi mỗi thứ Hai 8 giờ"); the person must approve. Give EITHER `cron` (5 fields: minute hour day month weekday, e.g. '0 8 * * 1' = every Monday 08:00,
        at least 15 minutes apart) OR `at` (ISO 8601, one time only, 1 minute to 365 days ahead; no zone = workspace time zone, default Vietnam). `message` = what the agent should do when it fires.
        new_thread=false continues this conversation."""
        c, why = refusal(config)
        if why: return "Từ chối / refused: " + why
        try: return await create(agent_id, c, config, cron, at, message, new_thread)
        except ValueError as e: return f"Không tạo được / failed: {e}"
    @lc_tool
    async def lich_danh_sach(config: RunnableConfig = None) -> str:
        """List the schedules the current person made through this agent (never anyone else's)."""
        import caller
        c = caller.current()
        if c.get("user_id") is None or c.get("kind") not in HUMAN_KINDS: return "Từ chối / refused: not a signed-in person."
        rows = await _q("SELECT * FROM agent_crons WHERE agent_id=%s AND source='agent' AND owner_user_id=%s ORDER BY id", (agent_id, c["user_id"]))
        return json.dumps([_row(r, tz_now(None)) for r in rows], ensure_ascii=False) if rows else "Chưa có lịch nào / no schedules."
    @lc_tool
    async def lich_xoa(id: int, config: RunnableConfig = None) -> str:
        """Delete one of your own schedules (by id from lich_danh_sach)."""
        c, why = refusal(config)
        if why: return "Từ chối / refused: " + why
        r = await _q("DELETE FROM agent_crons WHERE id=%s AND agent_id=%s AND source='agent' AND owner_user_id=%s AND locked_reason IS NULL RETURNING id", (id, agent_id, c["user_id"]), one=True)
        return f"Đã xóa lịch #{id}" if r else "Không có lịch này của bạn / not found."
    @lc_tool
    async def lich_sua(id: int, cron: str = "", at: str = "", message: str = "", paused: bool | None = None, config: RunnableConfig = None) -> str:
        """Edit one of your own schedules: new `cron` or `at`, new `message`, or paused=true/false. The person must approve."""
        c, why = refusal(config)
        if why: return "Từ chối / refused: " + why
        try: return await update(agent_id, c, id, cron, at, message, paused)
        except ValueError as e: return f"Không sửa được / failed: {e}"
    return {"local:lich_tao": lich_tao, "local:lich_danh_sach": lich_danh_sach, "local:lich_xoa": lich_xoa, "local:lich_sua": lich_sua}
