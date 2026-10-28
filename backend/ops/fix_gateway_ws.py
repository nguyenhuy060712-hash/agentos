#!/usr/bin/env python3
"""GĐ10.1 one-off repair: data that key-authenticated public routes wrote into the WRONG workspace before the fix (they landed in workspace 1).

  python ops/fix_gateway_ws.py                      dry run (default): prints what would move, changes nothing
  python ops/fix_gateway_ws.py --apply              does it, in ONE transaction
  python ops/fix_gateway_ws.py --dataset 12=5       also move external-experiment dataset 12 to workspace 5 (repeatable)

What it fixes (attribution comes from data that is really stored, never guessed):
  1. `gateway`-project traces: tag `vkey:<prefix>` -> gw_vkeys.prefix -> the key's workspace. Moved to that workspace's `gateway` project (created if missing).
     A prefix shared by keys of different workspaces is AMBIGUOUS and is reported, not moved. Runs whose inputs/outputs are trace-encryption envelopes
     (aos_crypto, sealed for the OLD workspace) are not moved either: they would become unreadable.
  2. gateway_usage rows with vkey_id whose workspace_id is NULL (e.g. /v1/decisions) or differs from the key's workspace -> the key's workspace.
  3. External-experiment datasets (created by /ingest/experiments): nothing records which ingest key made them, so they are only LISTED as candidates;
     move one explicitly with --dataset ID=WS (its experiments follow; the experiment traces `exp:<id>` move to that workspace's `experiments` project;
     a name clash in the target workspace is reported and skipped).
Not run at boot. Local only until anh decides about the VPS. Uses PG_URI like the app (default: the local dev database)."""
import asyncio, os, sys
import psycopg
from psycopg.rows import dict_row

ENV_KEY = "$aos_enc"   # aos_crypto.ENV: trace-encryption envelope marker inside tr_runs.inputs / outputs
DEFAULT_URI = "postgresql://aos:aoslocal@127.0.0.1:5432/agentos"

_TRACE_FROM = """
  FROM tr_runs r JOIN projects p ON p.id = r.project_id
  CROSS JOIN LATERAL unnest(r.tags) t
  JOIN (SELECT prefix, min(workspace_id) ws, count(DISTINCT workspace_id) n FROM gw_vkeys GROUP BY prefix) k ON k.prefix = substr(t, 6)
 WHERE p.name = 'gateway' AND t LIKE 'vkey:%%' AND k.n = 1 AND k.ws <> p.workspace_id"""
_SEALED = f"(coalesce(r.inputs, 'null'::jsonb) ? '{ENV_KEY}' OR coalesce(r.outputs, 'null'::jsonb) ? '{ENV_KEY}')"

async def plan(conn, datasets=None) -> dict:
    """Read-only: what apply() would do. datasets = {dataset_id: workspace_id} chosen by the operator."""
    cur = conn.cursor(row_factory=dict_row)
    out = {"traces": [], "ambiguous": [], "sealed": 0, "usage": [], "candidates": [], "datasets": [], "dataset_errors": []}
    await cur.execute("SELECT k.ws AS to_ws, p.workspace_id AS from_ws, substr(t, 6) AS prefix, count(*) AS n" + _TRACE_FROM + f" AND NOT {_SEALED} GROUP BY 1, 2, 3 ORDER BY 1, 2, 3")
    out["traces"] = await cur.fetchall()
    await cur.execute("""SELECT substr(t, 6) AS prefix, p.workspace_id AS from_ws, count(*) AS n FROM tr_runs r JOIN projects p ON p.id = r.project_id
        CROSS JOIN LATERAL unnest(r.tags) t WHERE p.name = 'gateway' AND t LIKE 'vkey:%%'
          AND substr(t, 6) IN (SELECT prefix FROM gw_vkeys GROUP BY prefix HAVING count(DISTINCT workspace_id) > 1) GROUP BY 1, 2""")
    out["ambiguous"] = await cur.fetchall()
    await cur.execute(f"SELECT count(*) AS n {_TRACE_FROM} AND {_SEALED}")
    out["sealed"] = (await cur.fetchone())["n"]
    await cur.execute("""SELECT u.vkey_id, k.name, u.workspace_id AS from_ws, k.workspace_id AS to_ws, count(*) AS n FROM gateway_usage u JOIN gw_vkeys k ON k.id = u.vkey_id
        WHERE u.workspace_id IS DISTINCT FROM k.workspace_id GROUP BY 1, 2, 3, 4 ORDER BY 1, 3 NULLS FIRST""")
    out["usage"] = await cur.fetchall()
    await cur.execute("""SELECT d.id, d.name, d.workspace_id, (SELECT count(*) FROM experiments e WHERE e.dataset_id = d.id) AS experiments FROM datasets d
        WHERE d.description = 'created from an external experiment' ORDER BY d.id""")
    out["candidates"] = await cur.fetchall()
    for did, ws in sorted((datasets or {}).items()):
        await cur.execute("SELECT id, name, workspace_id FROM datasets WHERE id = %s", (did,)); d = await cur.fetchone()
        await cur.execute("SELECT 1 FROM workspaces WHERE id = %s", (ws,)); w = await cur.fetchone()
        if not d: out["dataset_errors"].append(f"dataset {did}: not found"); continue
        if not w: out["dataset_errors"].append(f"dataset {did}: workspace {ws} not found"); continue
        if d["workspace_id"] == ws: out["dataset_errors"].append(f"dataset {did}: already in workspace {ws}"); continue
        await cur.execute("SELECT 1 FROM datasets WHERE workspace_id = %s AND name = %s", (ws, d["name"]))
        if await cur.fetchone(): out["dataset_errors"].append(f"dataset {did} '{d['name']}': a dataset with that name already exists in workspace {ws} (skipped)"); continue
        await cur.execute("SELECT count(*) AS n FROM experiments WHERE dataset_id = %s", (did,))
        out["datasets"].append({"id": did, "name": d["name"], "from_ws": d["workspace_id"], "to_ws": ws, "experiments": (await cur.fetchone())["n"]})
    return out

async def apply(conn, pl: dict) -> dict:
    """Do what plan() listed, in one transaction (all or nothing). Returns row counts."""
    n = {"traces": 0, "usage": 0, "datasets": 0, "dataset_traces": 0}
    async with conn.transaction():
        for ws in sorted({r["to_ws"] for r in pl["traces"]}):
            await conn.execute("INSERT INTO projects(name, workspace_id) VALUES('gateway', %s) ON CONFLICT(workspace_id, name) DO NOTHING", (ws,))
        if pl["traces"]:
            cur = await conn.execute(f"""UPDATE tr_runs x SET project_id = np.id FROM (SELECT r.id AS rid, r.start_time AS st, k.ws AS to_ws {_TRACE_FROM} AND NOT {_SEALED}) m
                JOIN projects np ON np.name = 'gateway' AND np.workspace_id = m.to_ws WHERE x.id = m.rid AND x.start_time = m.st""")
            n["traces"] = cur.rowcount
        if pl["usage"]:
            cur = await conn.execute("UPDATE gateway_usage u SET workspace_id = k.workspace_id FROM gw_vkeys k WHERE u.vkey_id = k.id AND u.workspace_id IS DISTINCT FROM k.workspace_id")
            n["usage"] = cur.rowcount
        for d in pl["datasets"]:
            await conn.execute("INSERT INTO projects(name, workspace_id) VALUES('experiments', %s) ON CONFLICT(workspace_id, name) DO NOTHING", (d["to_ws"],))
            cur = await conn.execute("""UPDATE tr_runs x SET project_id = np.id FROM projects np
                WHERE np.name = 'experiments' AND np.workspace_id = %s AND x.project_id IN (SELECT id FROM projects WHERE name = 'experiments')
                  AND x.tags && (SELECT coalesce(array_agg('exp:' || e.id), '{}'::text[]) FROM experiments e WHERE e.dataset_id = %s AND e.target->>'type' = 'external')""", (d["to_ws"], d["id"]))
            n["dataset_traces"] += cur.rowcount
            await conn.execute("UPDATE datasets SET workspace_id = %s WHERE id = %s", (d["to_ws"], d["id"])); n["datasets"] += 1
    return n

def render(pl: dict, applied: dict | None = None) -> list[str]:
    L = []
    L.append(f"gateway traces to move: {sum(r['n'] for r in pl['traces'])}")
    L += [f"  {r['n']:>6} runs  key {r['prefix']}: workspace {r['from_ws']} -> {r['to_ws']}" for r in pl["traces"]]
    if pl["ambiguous"]: L += ["AMBIGUOUS key prefix (several workspaces use it) - NOT moved:"] + [f"  {r['n']} runs  {r['prefix']} (in workspace {r['from_ws']})" for r in pl["ambiguous"]]
    if pl["sealed"]: L.append(f"encrypted traces left in place (sealed for their current workspace): {pl['sealed']}")
    L.append(f"gateway_usage rows to re-attribute: {sum(r['n'] for r in pl['usage'])}")
    L += [f"  {r['n']:>6} rows  key #{r['vkey_id']} {r['name']}: workspace {r['from_ws']} -> {r['to_ws']}" for r in pl["usage"]]
    L.append(f"external-experiment datasets (candidates, not auto-moved: no ingest key is recorded): {len(pl['candidates'])}")
    L += [f"  dataset {r['id']} '{r['name']}' in workspace {r['workspace_id']} ({r['experiments']} experiments)  ->  --dataset {r['id']}=<workspace>" for r in pl["candidates"]]
    L += [f"dataset {d['id']} '{d['name']}' ({d['experiments']} experiments): workspace {d['from_ws']} -> {d['to_ws']}" for d in pl["datasets"]]
    L += ["ERROR " + e for e in pl["dataset_errors"]]
    L.append(("APPLIED: " + ", ".join(f"{k}={v}" for k, v in applied.items())) if applied is not None else "DRY RUN - nothing changed. Re-run with --apply to do it.")
    return L

def _datasets(argv):
    out = {}
    for i, a in enumerate(argv):
        if a == "--dataset" and i + 1 < len(argv):
            did, _, ws = argv[i + 1].partition("=")
            if not (did.isdigit() and ws.isdigit()): raise SystemExit("--dataset needs ID=WORKSPACE_ID (numbers)")
            out[int(did)] = int(ws)
    return out

async def main(argv):
    if any(a in ("-h", "--help") for a in argv): return print(__doc__) or 0
    ds = _datasets(argv)
    async with await psycopg.AsyncConnection.connect(os.environ.get("PG_URI", DEFAULT_URI), autocommit=True) as conn:
        pl = await plan(conn, ds)
        applied = await apply(conn, pl) if "--apply" in argv else None
    print("\n".join(render(pl, applied))); return 0

if __name__ == "__main__": sys.exit(asyncio.run(main(sys.argv[1:])))
