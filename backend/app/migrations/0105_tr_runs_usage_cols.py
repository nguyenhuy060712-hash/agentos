"""GĐ12.15 new run columns (nullable, no table rewrite). All `IF NOT EXISTS`, so the later per-provider card (GĐ15 11.19) never recreates them.
  first_token_time      timestamptz  time of the first streamed token (NULL when not streamed - never 0)
  usage_details         jsonb        token counts by kind: cache_read, cache_creation, reasoning, audio, image, video
  cost_details          jsonb        USD by the same kinds
  input_cost/output_cost numeric(12,6)  USD of the input / output side
  reference_example_id  uuid         dataset example a run was made from (SDK / OTLP)
  depth                 smallint     number of dotted_order segments - 1 (0 = root)
  ttl_days              smallint     retention of this run, written on INSERT by GĐ12.18 (old rows: NULL)
down(): drops them again."""
VERSION = "0105"
NAME = "tr_runs_usage_cols"

COLS = (("first_token_time", "timestamptz"), ("usage_details", "jsonb"), ("cost_details", "jsonb"), ("input_cost", "numeric(12,6)"),
        ("output_cost", "numeric(12,6)"), ("reference_example_id", "uuid"), ("depth", "smallint"), ("ttl_days", "smallint"))

async def up(q):
    await q("ALTER TABLE tr_runs " + ", ".join(f"ADD COLUMN IF NOT EXISTS {c} {t}" for c, t in COLS))   # partitioned parent: every partition gets the columns

async def down(q):
    await q("ALTER TABLE tr_runs " + ", ".join(f"DROP COLUMN IF EXISTS {c}" for c, _ in COLS))

async def plan(q):
    return [f"ALTER TABLE tr_runs ADD COLUMN {c} {t} (nullable, no rewrite)" for c, t in COLS]
