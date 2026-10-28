"""GĐ12.9 hourly rollup of runs: the numbers charts still need after the raw runs are deleted by retention.
  tr_stats_hourly: one row per (project, UTC hour, environment, run_type, name, model, is_root, is_private). Text dimensions are '' when the run has none.
    n / n_err = runs / runs with status error; lat_sum + n_lat = sum and count of latency_ms; ttft_sum + n_ttft = same for time-to-first-token (ms);
    tok_in / tok_out / tok_total / cost = sums; lat_hist / ttft_hist = 40-bin log-scale histograms (bin = floor(log10(ms + 1) * 7), last bin = overflow) for percentiles.
    is_private = the run belongs to one chat user (tr_runs.user_id IS NOT NULL): only a workspace admin reads those rows (policy in admin.RLS_CHILD).
down(): drops the table."""
VERSION = "0115"
NAME = "tr_stats_hourly"

async def up(q):
    await q("""CREATE TABLE IF NOT EXISTS tr_stats_hourly(
        project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE, hour TIMESTAMPTZ NOT NULL,
        environment TEXT NOT NULL DEFAULT '', run_type TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
        is_root BOOLEAN NOT NULL, is_private BOOLEAN NOT NULL DEFAULT false,
        n INT NOT NULL DEFAULT 0, n_err INT NOT NULL DEFAULT 0,
        lat_sum BIGINT NOT NULL DEFAULT 0, n_lat INT NOT NULL DEFAULT 0, ttft_sum DOUBLE PRECISION NOT NULL DEFAULT 0, n_ttft INT NOT NULL DEFAULT 0,
        lat_hist INT[] NOT NULL DEFAULT array_fill(0, ARRAY[40]), ttft_hist INT[] NOT NULL DEFAULT array_fill(0, ARRAY[40]),
        tok_in BIGINT NOT NULL DEFAULT 0, tok_out BIGINT NOT NULL DEFAULT 0, tok_total BIGINT NOT NULL DEFAULT 0, cost NUMERIC(14,6) NOT NULL DEFAULT 0,
        PRIMARY KEY(project_id, hour, environment, run_type, name, model, is_root, is_private))""")
    await q("CREATE INDEX IF NOT EXISTS tr_stats_hourly_hour ON tr_stats_hourly(hour)")

async def down(q):
    await q("DROP TABLE IF EXISTS tr_stats_hourly")

async def plan(q):
    return ["CREATE TABLE tr_stats_hourly (project_id -> projects ON DELETE CASCADE, hour, environment, run_type, name, model, is_root, is_private, counts, sums, 2 x int[40] histograms) (RLS via admin.RLS_CHILD)",
            "CREATE INDEX tr_stats_hourly_hour ON tr_stats_hourly(hour)"]
