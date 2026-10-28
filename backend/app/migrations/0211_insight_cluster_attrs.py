"""GĐ13 (12.43) Insights traces mode: per-group attribute statistics (share true/false, average of numbers, top categories) next to the metrics of each group.
  insight_clusters: + attrs JSONB (list of {name, type, n, ...}; NULL for topic-mode reports).
down(): drops the column."""
VERSION = "0211"
NAME = "insight_cluster_attrs"

async def up(q):
    await q("""DO $$ BEGIN IF to_regclass('public.insight_clusters') IS NOT NULL THEN
      ALTER TABLE insight_clusters ADD COLUMN IF NOT EXISTS attrs JSONB; END IF; END $$""")

async def down(q):
    await q("ALTER TABLE IF EXISTS insight_clusters DROP COLUMN IF EXISTS attrs")

async def plan(q):
    return ["ALTER TABLE insight_clusters ADD attrs JSONB (attribute statistics of a traces-mode group)"]
