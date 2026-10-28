"""GĐ12.8 one cost definition: the cost / tokens of a root run = the sum of the LLM runs (and declared metadata.cost_usd steps) of its trace tree (LangSmith).
Before, `obs.rollup` skipped a root that already had tokens and a model, so a resumed run (second segment under the root) was missing from the root total:
the dashboard "Total cost" was lower than "Cost by model". up(): recompute those roots once, then drop the hourly rollup rows (tr_stats_hourly) of the hours that changed
so the worker's catch_up rolls them again from the corrected runs. down(): nothing to undo (data repair)."""
VERSION = "0130"
NAME = "trace_cost_rollup_fix"

_FIX = """WITH s AS (SELECT trace_id, sum(input_tokens) i, sum(output_tokens) o, sum(total_tokens) t, sum(cost_usd) c FROM tr_runs
      WHERE parent_id IS NOT NULL AND (run_type='llm' OR jsonb_typeof(metadata->'cost_usd')='number') GROUP BY trace_id),
    u AS (UPDATE tr_runs g SET input_tokens=s.i, output_tokens=s.o, total_tokens=s.t, cost_usd=coalesce(s.c, g.cost_usd) FROM s
      WHERE g.trace_id=s.trace_id AND g.parent_id IS NULL AND s.t IS NOT NULL AND g.run_type IS DISTINCT FROM 'llm' AND jsonb_typeof(g.metadata->'cost_usd') IS DISTINCT FROM 'number'
        AND (g.cost_usd IS DISTINCT FROM coalesce(s.c, g.cost_usd) OR g.total_tokens IS DISTINCT FROM s.t OR g.input_tokens IS DISTINCT FROM s.i OR g.output_tokens IS DISTINCT FROM s.o)
      RETURNING g.project_id, date_trunc('hour', g.start_time, 'UTC') h)
    DELETE FROM tr_stats_hourly h USING (SELECT DISTINCT project_id, h FROM u) x WHERE h.project_id = x.project_id AND h.hour = x.h"""

async def up(q):
    await q(_FIX)

async def down(q):
    return None

async def plan(q):
    return ["UPDATE tr_runs root rows: tokens / cost = sum over LLM (or declared-cost) runs of the trace tree; DELETE the matching tr_stats_hourly hours (re-rolled by stats_rollup.catch_up)"]
