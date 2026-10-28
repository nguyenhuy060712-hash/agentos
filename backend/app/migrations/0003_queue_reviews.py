"""GĐ10.5 per-reviewer rows for annotation queues: who reviewed / skipped / reserved an item (queue_items.status stays the overall state)."""
VERSION = "0003"
NAME = "queue_reviews"

async def _default(q):
    # migrations run BEFORE admin.rls_init(): aos_ws() may not exist yet on a fresh DB; rls_init then upgrades the default to coalesce(aos_ws(),1)
    ok = (await q("SELECT to_regproc('aos_ws') IS NOT NULL"))[0][0]
    return "coalesce(aos_ws(), 1)" if ok else "1"

async def up(q):
    d = await _default(q)
    await q(f"""CREATE TABLE IF NOT EXISTS queue_item_reviews(id SERIAL PRIMARY KEY, item_id INT NOT NULL REFERENCES queue_items(id) ON DELETE CASCADE,
      reviewer TEXT NOT NULL, done_at TIMESTAMPTZ, skipped BOOLEAN NOT NULL DEFAULT false, reserved_until TIMESTAMPTZ,
      workspace_id INT NOT NULL DEFAULT {d}, UNIQUE(item_id, reviewer))""")
    await q("""INSERT INTO queue_item_reviews(item_id, reviewer, done_at)
      SELECT id, coalesce(reviewer, 'moza'), coalesce(completed, now()) FROM queue_items WHERE status='done' ON CONFLICT DO NOTHING""")   # keep finished history

async def down(q):
    await q("DROP TABLE IF EXISTS queue_item_reviews")

async def plan(q):
    return ["CREATE TABLE queue_item_reviews(item_id, reviewer, done_at, skipped, reserved_until, workspace_id, UNIQUE(item_id, reviewer))",
            "back-fill one review row per already-done queue item", "(RLS policy is registered by admin.RLS_DIRECT at boot)"]
