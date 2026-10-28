"""GĐ12.17 full-text search in traces.
  aos_fold(text)         same function as chat_gd5 (lower + unaccent), created here too so the order of boot steps does not matter (CREATE OR REPLACE, same body).
  tr_runs.search_tsv     tsvector built by the app (app/fts.py): inputs weight A, outputs weight B, error / tags / metadata weight C.
                         NULL = not indexed yet (the worker backfill fills it; the search falls back to ILIKE for those rows only).
                         In a workspace with trace encryption the lexemes are keyed hashes of the words (blind index), never readable words.
  tr_runs_search_tsv     GIN index on the partitioned parent: every existing partition gets one, and ATTACH PARTITION (obs.ensure_partitions) builds it for new ones.
  tr_runs_fts_todo       small partial index of the not-yet-indexed rows (backfill and the ILIKE fallback use it).
down(): drops the indexes and the column (aos_fold is shared with the chat search, so it stays)."""
VERSION = "0108"
NAME = "tr_runs_search_tsv"

async def up(q):
    await q("CREATE EXTENSION IF NOT EXISTS unaccent")
    await q("""CREATE OR REPLACE FUNCTION aos_fold(text) RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
        AS $f$ SELECT lower(public.unaccent('public.unaccent'::regdictionary, $1)) $f$""")
    await q("ALTER TABLE tr_runs ADD COLUMN IF NOT EXISTS search_tsv tsvector")   # partitioned parent: every partition gets the column
    await q("CREATE INDEX IF NOT EXISTS tr_runs_search_tsv ON tr_runs USING gin(search_tsv)")
    await q("CREATE INDEX IF NOT EXISTS tr_runs_fts_todo ON tr_runs(start_time DESC) WHERE search_tsv IS NULL")

async def down(q):
    await q("DROP INDEX IF EXISTS tr_runs_fts_todo")
    await q("DROP INDEX IF EXISTS tr_runs_search_tsv")
    await q("ALTER TABLE tr_runs DROP COLUMN IF EXISTS search_tsv")

async def plan(q):
    return ["CREATE EXTENSION IF NOT EXISTS unaccent; CREATE OR REPLACE FUNCTION aos_fold(text)",
            "ALTER TABLE tr_runs ADD COLUMN search_tsv tsvector (nullable, no rewrite)",
            "CREATE INDEX tr_runs_search_tsv ON tr_runs USING gin(search_tsv) (every partition)",
            "CREATE INDEX tr_runs_fts_todo ON tr_runs(start_time DESC) WHERE search_tsv IS NULL"]
