"""GD7.5 (23/10/2026): judge alignment - how well does an LLM evaluator agree with human labels (annotation-queue scores, thumbs/feedback, score corrections on the same runs)?
GET  /api/evaluators/{id}/alignment      pairs (human vs judge), accuracy + Cohen's kappa (categorical / pass-fail) or Pearson r + MAE (continuous), disagreements
POST /api/evaluators/{id}/align/quick    "Căn chỉnh nhanh": pick 3-6 diverse recent disagreements as few-shot examples -> NEW evaluator version -> re-run the judge on the labelled set -> before/after
POST /api/evaluators/{id}/align/revert   back to the previous version (judge scores of the previous version are restored, no LLM call)
GET  /api/evaluators/{id}/versions
Reused: openevals `create_llm_as_judge(few_shot_examples=...)` through evalcat (kind "openevals", config.few_shot_examples); for the older kind "llm" the existing config.few_shot storage (same shape the
use_corrections feature writes). Ours: the metrics, the diverse pick (farthest-point on word overlap), versions, before/after.
Deep align (langmem prompt optimizer / DSPy): NOT implemented - langmem failed with fake models in 7.4 and DSPy is not trivially pluggable; hidden TODO in the evaluator page. Feature cap name reserved: 'align'."""
import asyncio, json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb
import deepagent_api as A
import alignkit as K
from alignkit import metrics

router = APIRouter()
HUMAN = ("human", "annotation")
MIN_LABELS = 6
ALIGNABLE = ("openevals", "llm")

# metrics, diverse pick and few-shot shapes: app/alignkit.py (pure, unit-tested)

# ------------------------------------------------------------------ data
def _val(r): return r["score"] if r["score"] is not None else r["value"]

def _scale(ev):
    c = ev["config"]
    if ev["kind"] == "openevals": return (0.0, 1.0)
    return (0.0, 1.0) if c.get("score_type") == "boolean" else (float(c.get("min", 1)), float(c.get("max", 5)))

async def human_labels(ev):
    """{run_id: {value, comment, id, source}}: latest human feedback with the evaluator's key per run; else a reviewer's correction of a judge score."""
    key = ev["config"]["key"]; out = {}
    for r in await A.q("""SELECT DISTINCT ON (run_id) id, run_id::text run_id, score, value, comment, source FROM feedback
                          WHERE key=%s AND source = ANY(%s) AND (score IS NOT NULL OR value IS NOT NULL) ORDER BY run_id, id DESC""", (key, list(HUMAN))):
        out[r["run_id"]] = {"value": _val(r), "comment": r["comment"], "id": r["id"], "source": r["source"]}
    for r in await A.q("""SELECT DISTINCT ON (run_id) id, run_id::text run_id, correction FROM feedback WHERE evaluator_id=%s AND correction IS NOT NULL ORDER BY run_id, id DESC""", (ev["id"],)):
        c = r["correction"] or {}; v = c.get("score", c.get("value"))
        if r["run_id"] not in out and v is not None: out[r["run_id"]] = {"value": float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else (1.0 if v is True else 0.0 if v is False else v), "comment": c.get("comment"), "id": r["id"], "source": "correction"}
    return out

async def judge_scores(ev):
    """{run_id: value}: latest score this evaluator gave per run (any non-human source)."""
    rows = await A.q("""SELECT DISTINCT ON (run_id) run_id::text run_id, score, value FROM feedback WHERE evaluator_id=%s AND key=%s AND source <> ALL(%s) AND (score IS NOT NULL OR value IS NOT NULL)
                        ORDER BY run_id, id DESC""", (ev["id"], ev["config"]["key"], list(HUMAN)))
    return {r["run_id"]: _val(r) for r in rows}

def _snip(x, n=160):
    s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + "…"

async def _texts(run_ids):
    import evals
    rows = await A.q("SELECT id::text id, inputs, outputs, error, latency_ms, total_tokens, metadata FROM tr_runs WHERE id::text = ANY(%s)", (list(run_ids),)) if run_ids else []
    return {r["id"]: evals._run_view(r) for r in rows}

async def build(ev, labels=None, judge=None, with_text=True):
    labels = labels if labels is not None else await human_labels(ev); judge = judge if judge is not None else await judge_scores(ev)
    lo, hi = _scale(ev); both = [r for r in labels if r in judge]
    views = await _texts(both) if with_text else {}
    pairs = []
    for rid in both:
        h, j = labels[rid]["value"], judge[rid]
        pairs.append({"run_id": rid, "human": h, "judge": j, "fid": labels[rid]["id"], "comment": labels[rid]["comment"], "source": labels[rid]["source"],
                      "input": _snip(views.get(rid, {}).get("input", "")), "output": _snip(views.get(rid, {}).get("output", ""))})
    m = metrics(pairs, (lo, hi)); hr = K.human_range(pairs, (lo, hi)) if m["mode"] == "continuous" else None
    for p in pairs: p["agree"] = not K.disagrees(p, m["mode"], (lo, hi), hr) if m["mode"] else True
    return pairs, m, labels

async def _ev(eid):
    ev = await A.q("SELECT * FROM evaluators WHERE id=%s", (eid,), one=True)
    if not ev: raise HTTPException(404, "Evaluator not found")
    if not (ev["config"] or {}).get("key"): raise HTTPException(400, "Evaluator has no config.key")
    return ev

async def _versions(eid):
    return await A.q("SELECT id, version, note, metrics, created_by, created FROM evaluator_versions WHERE evaluator_id=%s ORDER BY version DESC", (eid,))

@router.get("/api/evaluators/{eid}/alignment")
async def alignment(eid: int):
    ev = await _ev(eid)
    pairs, m, labels = await build(ev)
    vs = await _versions(eid)
    dis = [p for p in pairs if not p["agree"]]
    dis.sort(key=lambda p: -p["fid"])
    return {"evaluator_id": eid, "key": ev["config"]["key"], "kind": ev["kind"], "alignable": ev["kind"] in ALIGNABLE, "mode": m["mode"], "metrics": m,
            "n_human": len(labels), "n_pairs": len(pairs), "n_unjudged": len(labels) - len(pairs), "min_labels": MIN_LABELS,
            "version": vs[0]["version"] if vs else 1, "versions": len(vs), "can_revert": len(vs) > 1,
            "pairs": [{k: v for k, v in p.items() if k not in ("h_unit", "j_unit")} for p in pairs[:500]],
            "disagreements": [{k: v for k, v in p.items() if k not in ("h_unit", "j_unit")} for p in dis[:100]]}

@router.get("/api/evaluators/{eid}/versions")
async def versions(eid: int):
    await _ev(eid); return await _versions(eid)

# ------------------------------------------------------------------ quick align
def _few_field(ev): return "few_shot_examples" if ev["kind"] == "openevals" else "few_shot"

class QuickIn(BaseModel):
    max_examples: int = Field(default=6, ge=3, le=6)

async def _judge_all(ev, run_ids):
    """Run the judge on each labelled run (stores feedback source 'align'); returns {run_id: value}. Raises the first HTTPException (e.g. 402 spend cap)."""
    import evals, evalcat
    sem = asyncio.Semaphore(4); tok = evalcat.FEATURE_OVERRIDE.set("align")
    async def one(rid):
        async with sem: return rid, await evals.run_evaluator_on_run(ev["id"], rid, source="align")
    try: res = await asyncio.gather(*[one(r) for r in run_ids], return_exceptions=True)
    finally: evalcat.FEATURE_OVERRIDE.reset(tok)
    for r in res:
        if isinstance(r, BaseException): raise r
    out = {}
    for rid, rows in res:
        if rows and (rows[0].get("score") is not None or rows[0].get("value") is not None): out[rid] = _val(rows[0])
    return out

async def _username():
    import admin
    return (admin.cur() or {}).get("username") or "unknown"

@router.post("/api/evaluators/{eid}/align/quick")
async def align_quick(eid: int, b: QuickIn | None = None):
    b = b or QuickIn()
    ev = await _ev(eid)
    if ev["kind"] not in ALIGNABLE: raise HTTPException(400, "Only LLM judges (kind llm / openevals) can be aligned")
    labels = await human_labels(ev)
    if len(labels) < MIN_LABELS: raise HTTPException(422, f"Need at least {MIN_LABELS} runs labelled by a person (annotation queue / feedback / score correction); found {len(labels)}")
    judge = await judge_scores(ev)
    missing = [r for r in labels if r not in judge]
    if missing: judge.update(await _judge_all(ev, missing))          # label runs the judge has never scored (may raise 402 before anything is changed)
    pairs, before, _ = await build(ev, labels, judge)
    dis = [p for p in pairs if not p["agree"]]
    if not pairs: raise HTTPException(422, "The judge produced no score for the labelled runs")
    if not dis: return {"changed": False, "reason": "The judge already agrees with every human label", "before": before}
    chosen = K.pick_diverse(dis, min(b.max_examples, 6 if ev["kind"] == "openevals" else 5))
    views = await _texts([p["run_id"] for p in chosen])
    new = [K.example(ev, p, _snip(views.get(p["run_id"], {}).get("input", ""), 600), _snip(views.get(p["run_id"], {}).get("output", ""), 600)) for p in chosen]
    old_cfg = ev["config"]; fld = _few_field(ev)
    manual = [x for x in (old_cfg.get(fld) or []) if not (isinstance(x, dict) and x.get("_align"))]
    cfg = {**old_cfg, fld: (manual + new)[-8:]}
    vs = await _versions(eid); cur = vs[0]["version"] if vs else 1; user = await _username()
    if not vs: await A.q("INSERT INTO evaluator_versions(evaluator_id,version,config,note,created_by) VALUES(%s,1,%s,'before first alignment',%s)", (eid, Jsonb(old_cfg), user))
    nv = cur + 1
    await A.q("INSERT INTO evaluator_versions(evaluator_id,version,config,note,scores_before,created_by) VALUES(%s,%s,%s,%s,%s,%s)",
              (eid, nv, Jsonb(cfg), f"quick align: {len(new)} few-shot examples", Jsonb({r: judge[r] for r in labels if r in judge}), user))
    await A.q("UPDATE evaluators SET config=%s WHERE id=%s", (Jsonb(cfg), eid))
    try: after_scores = await _judge_all({**ev, "config": cfg}, list(labels))
    except BaseException:                                              # cap reached / judge error mid-way: put everything back
        await A.q("UPDATE evaluators SET config=%s WHERE id=%s", (Jsonb(old_cfg), eid)); await A.q("DELETE FROM evaluator_versions WHERE evaluator_id=%s AND version=%s", (eid, nv)); raise
    pairs2, after, _ = await build(ev, labels, {**judge, **after_scores})
    ex_ids = {p["run_id"] for p in chosen}
    held_pairs = [p for p in pairs2 if p["run_id"] not in ex_ids]
    held = metrics(held_pairs, _scale(ev)) if held_pairs else None
    res = {"changed": True, "version": nv, "examples": len(new), "example_run_ids": [p["run_id"] for p in chosen], "before": before, "after": after, "held_out_after": held,
           "held_out_before": metrics([p for p in pairs if p["run_id"] not in ex_ids], _scale(ev)) if len(pairs) > len(chosen) else None,
           "improved": (after["agreement"] or 0) > (before["agreement"] or 0)}
    await A.q("UPDATE evaluator_versions SET metrics=%s WHERE evaluator_id=%s AND version=%s", (Jsonb({k: res[k] for k in ("before", "after", "held_out_before", "held_out_after")}), eid, nv))
    return res

@router.post("/api/evaluators/{eid}/align/revert")
async def align_revert(eid: int):
    ev = await _ev(eid)
    vs = await _versions(eid)
    if len(vs) < 2: raise HTTPException(409, "No earlier version to go back to")
    cur, prev = vs[0], vs[1]
    pc = await A.q("SELECT config FROM evaluator_versions WHERE evaluator_id=%s AND version=%s", (eid, prev["version"]), one=True)
    sb = (await A.q("SELECT scores_before FROM evaluator_versions WHERE evaluator_id=%s AND version=%s", (eid, cur["version"]), one=True) or {}).get("scores_before") or {}
    nv = cur["version"] + 1
    await A.q("INSERT INTO evaluator_versions(evaluator_id,version,config,note,created_by) VALUES(%s,%s,%s,%s,%s)", (eid, nv, Jsonb(pc["config"]), f"revert to v{prev['version']}", await _username()))
    await A.q("UPDATE evaluators SET config=%s WHERE id=%s", (Jsonb(pc["config"]), eid))
    import obs
    for rid, v in sb.items():      # the previous version's judge scores become the latest again (no LLM call)
        await obs.add_feedback(obs.FbIn(run_id=rid, key=ev["config"]["key"], score=v if isinstance(v, (int, float)) and not isinstance(v, bool) else None, value=v if isinstance(v, str) else None,
                                        comment=f"restored by revert to v{prev['version']}"), source="align", evaluator_id=eid)
    ev2 = await _ev(eid); pairs, m, _ = await build(ev2)
    return {"version": nv, "restored_scores": len(sb), "metrics": m}
