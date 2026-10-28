"""GĐ11.35 thread inbox: when each user last opened each chat thread (unread dot = the thread changed after that). RLS: admin.RLS_DIRECT (workspace_id)."""
VERSION = "0036"
NAME = "thread_reads"

async def _has(q, t, c): return bool(await q("SELECT 1 FROM information_schema.columns WHERE table_schema='public' AND table_name=%s AND column_name=%s", (t, c)))

async def up(q):
    # migrations run BEFORE admin.rls_init(): aos_ws() may not exist yet on a fresh DB (rls_init then upgrades the default to coalesce(aos_ws(),1))
    ok = (await q("SELECT to_regproc('aos_ws') IS NOT NULL"))[0][0]
    d = "coalesce(aos_ws(), 1)" if ok else "1"
    await q(f"""CREATE TABLE IF NOT EXISTS thread_reads(workspace_id INT NOT NULL DEFAULT {d}, user_id INT NOT NULL, thread_id TEXT NOT NULL,
      last_read_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(workspace_id, user_id, thread_id))""")
    # existing chats start as "read" (otherwise the first inbox would show every old thread as unread)
    if await _has(q, "lg_threads", "owner_id"):
        await q("""INSERT INTO thread_reads(workspace_id, user_id, thread_id, last_read_at)
          SELECT workspace_id, owner_id, thread_id, now() FROM lg_threads WHERE owner_id IS NOT NULL ON CONFLICT DO NOTHING""")

async def down(q):
    await q("DROP TABLE IF EXISTS thread_reads")

async def plan(q):
    return ["CREATE TABLE thread_reads(workspace_id, user_id, thread_id, last_read_at, PRIMARY KEY(workspace_id, user_id, thread_id))",
            "mark every existing owned thread as read now", "(RLS policy is registered by admin.RLS_DIRECT at boot)"]
