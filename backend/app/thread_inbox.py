"""GĐ11.35 thread inbox: unread / awaiting-approval lists for the chat sidebar + automatic thread title.
GET /api/inbox?agent_id&filter=unread|awaiting|all&cursor  -> the caller's own threads (newest first, 50 per page) + counts.
POST /api/threads/{tid}/read -> mark a thread read (the chat calls it when a thread is opened and when a run ends while it is open).
unread = the thread changed (message sent / run ended) after the caller's last_read_at, or it has runs and was never read; a running thread is not unread.
awaiting = lg_threads.status 'interrupted' (paused for approval). Channel threads run as the binding owner, so they land in the owner's inbox.
Title: when a run pauses for approval and the thread has no title, ONE cheap gateway call (feature 'chat_title') writes metadata.title once.
Off for demo accounts (empty inbox, 403 on read)."""
import datetime as dt
from typing import Annotated
from fastapi import APIRouter, HTTPException, Query
import deepagent_api as A
import admin

router = APIRouter(tags=["thread-inbox"])
FEATURE = "chat_title"
admin.allow_viewer_post("/api/threads/")   # marking a thread read changes nothing but the caller's own read marker
PAGE = 50

def _me():
    u = admin.USER.get() or {}
    return u.get("id")

def _title_prompt(text: str) -> str:
    return ("Write a short title (at most 6 words, no quotes, no trailing punctuation) for a chat that starts with the message below. "
            "Use the same language as the message. Reply with the title only.\n\nMessage:\n" + text[:1200])

def clean_title(s) -> str:
    s = " ".join(str(s or "").replace("\r", " ").split()).strip(" \"'`*#.")
    return s[:60]

async def auto_title(tid: str, text: str | None = None) -> str | None:
    """Set metadata.title once (never overwrites a title; never raises). Returns the new title or None."""
    try:
        if admin.is_demo(): return None
        t = await A.q("SELECT metadata FROM lg_threads WHERE thread_id=%s", (tid,), one=True)
        if not t or (t["metadata"] or {}).get("title"): return None
        if text is None:
            import server
            r = await A.q("SELECT input FROM lg_runs WHERE thread_id=%s ORDER BY created_at LIMIT 1", (tid,), one=True)
            text = server._user_text((r or {}).get("input"))
        if not text or not text.strip(): return None
        out, _m, _i, _o = await A.gw.generate(A._g, _title_prompt(text), [A.DEFAULT_MODEL] + A.gw.DEFAULT_FALLBACKS, temperature=0.3, feature=FEATURE)
        title = clean_title(out)
        if not title: return None
        from psycopg.types.json import Jsonb
        row = await A.q("UPDATE lg_threads SET metadata = metadata || %s WHERE thread_id=%s AND coalesce(metadata->>'title','') = '' RETURNING thread_id", (Jsonb({"title": title}), tid), one=True)
        return title if row else None
    except Exception:  # noqa: BLE001  (a title is nice-to-have; the run result must never depend on it)
        return None

@router.get("/api/inbox")
async def inbox(agent_id: int | None = None, filter: Annotated[str, Query(pattern="^(unread|awaiting|all)$")] = "all", cursor: str | None = None, limit: Annotated[int, Query(ge=1, le=200)] = PAGE):
    uid, ws = _me(), admin.cur_ws()
    if admin.is_demo() or uid is None: return {"items": [], "next_cursor": None, "counts": {"unread": 0, "awaiting": 0}}
    cur = None
    if cursor:
        try: cur = dt.datetime.fromisoformat(cursor)
        except ValueError: raise HTTPException(400, "cursor không hợp lệ")
    base = """FROM lg_threads t LEFT JOIN thread_reads r ON r.thread_id=t.thread_id AND r.user_id=%(uid)s AND r.workspace_id=%(ws)s
        WHERE t.workspace_id=%(ws)s AND t.owner_id=%(uid)s AND (%(aid)s::int IS NULL OR t.agent_id=%(aid)s)"""
    unread = """(t.status <> 'busy' AND (r.last_read_at IS NULL AND EXISTS (SELECT 1 FROM lg_runs x WHERE x.thread_id=t.thread_id)
        OR r.last_read_at < t.updated_at))"""
    p = {"uid": uid, "ws": ws, "aid": agent_id}
    counts = await A.q(f"SELECT count(*) FILTER (WHERE {unread}) unread, count(*) FILTER (WHERE t.status='interrupted') awaiting {base}", p, one=True)
    cond = {"unread": f" AND {unread}", "awaiting": " AND t.status='interrupted'", "all": ""}[filter]
    p.update(cur=cur, lim=limit + 1)
    rows = await A.q(f"SELECT t.thread_id, t.agent_id, t.status, t.metadata, t.updated_at, {unread} AS unread, (t.status='interrupted') AS awaiting {base}{cond} "
                     "AND (%(cur)s::timestamptz IS NULL OR t.updated_at < %(cur)s) ORDER BY t.updated_at DESC LIMIT %(lim)s", p)
    nxt = rows[limit - 1]["updated_at"].isoformat() if len(rows) > limit else None
    return {"items": rows[:limit], "next_cursor": nxt, "counts": {"unread": counts["unread"], "awaiting": counts["awaiting"]}}

@router.post("/api/threads/{tid}/read")
async def mark_read(tid: str):
    uid, ws = _me(), admin.cur_ws()
    if admin.is_demo() or uid is None: raise HTTPException(403, "Không dùng được với tài khoản demo")
    if not await A.q("SELECT 1 FROM lg_threads WHERE thread_id=%s AND workspace_id=%s", (tid, ws), one=True): raise HTTPException(404, "Không có thread")
    await A.q("INSERT INTO thread_reads(workspace_id,user_id,thread_id,last_read_at) VALUES(%s,%s,%s,now()) "
              "ON CONFLICT(workspace_id,user_id,thread_id) DO UPDATE SET last_read_at=now()", (ws, uid, tid))
    return {"ok": True}
