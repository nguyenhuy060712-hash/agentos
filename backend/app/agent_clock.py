"""GĐ9.P1 (24/10/2026): today's date and time in every agent's system prompt (8.R: agents did not know the date).
A small middleware adds ONE line to the system message of each model call (main agent and sub-agents) at run time; nothing is saved into the
agent's prompt. Time zone = the workspace's (workspaces.timezone, default Asia/Ho_Chi_Minh; set by a workspace admin through
PUT /api/admin/workspace/timezone). Format is fixed ("Current date and time: Friday 2026-10-24 14:05 (Asia/Ho_Chi_Minh, UTC+07:00)")."""
import os, time
from datetime import datetime
from zoneinfo import ZoneInfo, available_timezones
from fastapi import APIRouter, HTTPException
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage
from pydantic import BaseModel, Field

router = APIRouter()
DEFAULT_TZ = os.getenv("AOS_DEFAULT_TZ", "Asia/Ho_Chi_Minh")
_CACHE: dict = {}   # workspace id -> (expires, tz name)
DDL = "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS timezone TEXT"

async def init():
    import deepagent_api as A
    await A.q(DDL)

def line(now: datetime) -> str:
    off = now.strftime("%z"); off = f"UTC{off[:3]}:{off[3:]}" if off else "UTC"
    return (f"Current date and time: {now.strftime('%A %Y-%m-%d %H:%M')} ({getattr(now.tzinfo, 'key', None) or 'UTC'}, {off}). "
            "Use it to resolve relative dates (today, tomorrow, next week, hôm nay, ngày mai, tuần sau).")

def stamp(sm, now: datetime) -> SystemMessage:
    """The system message plus the date line (a copy; the request keeps nothing else changed)."""
    txt = line(now); c = sm.content if sm is not None else ""
    if isinstance(c, list): return SystemMessage(content=[*c, {"type": "text", "text": "\n\n" + txt}])
    return SystemMessage(content=(c + "\n\n" if c else "") + txt)

async def workspace_tz(ws) -> str:
    if not ws: return DEFAULT_TZ
    hit = _CACHE.get(ws)
    if hit and hit[0] > time.time(): return hit[1]
    tz = DEFAULT_TZ
    try:
        import gateway as gw
        r = await gw._sys("SELECT timezone FROM workspaces WHERE id=%s", (ws,), one=True)
        if r and r["timezone"] and r["timezone"] in available_timezones(): tz = r["timezone"]
    except Exception:  # noqa: BLE001  (no pool in unit tests, DB hiccup: default zone)
        pass
    _CACHE[ws] = (time.time() + 60, tz)
    return tz

class DateTimeMiddleware(AgentMiddleware):
    """Adds the current date/time line to the system message before every model call, in the workspace time zone."""
    async def awrap_model_call(self, request, handler):
        import gateway as gw
        tz = await workspace_tz(gw._cur_ws())
        return await handler(request.override(system_message=stamp(request.system_message, datetime.now(ZoneInfo(tz)))))

class TzIn(BaseModel):
    timezone: str = Field(min_length=1, max_length=64)

@router.get("/api/admin/workspace/timezone")
async def tz_get():
    import admin
    return {"timezone": await workspace_tz(admin.cur_ws()), "default": DEFAULT_TZ}

@router.put("/api/admin/workspace/timezone")
async def tz_put(b: TzIn):
    import admin, deepagent_api as A
    if b.timezone not in available_timezones(): raise HTTPException(422, "Múi giờ không hợp lệ (vd Asia/Ho_Chi_Minh) / invalid time zone")
    ws = admin.cur_ws()
    await A.q("UPDATE workspaces SET timezone=%s WHERE id=%s", (b.timezone, ws)); _CACHE.pop(ws, None)
    return {"timezone": b.timezone}
