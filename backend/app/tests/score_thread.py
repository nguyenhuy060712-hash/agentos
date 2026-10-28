"""Score a whole thread (all traces incl. HITL-resumed runs) with Phòng AI số evaluators; print per-agent metrics."""
import json, subprocess, sys, requests, collections
c = dict(l.split("=", 1) for l in open("/workspace/secrets/deepagent_web.txt").read().split())
U = c["url"].rstrip("/"); s = requests.Session(); s.auth = ("moza", c["password"])
evs = json.load(open("/workspace/adk_ha_build/tests/eval_ids.json")); tid = sys.argv[1]
def psq(sql): return subprocess.run(["/workspace/adk_ha_build/psq.sh", sql], capture_output=True, text=True).stdout.strip().splitlines()
runs = [l.split("|") for l in psq(f"select e.id, (select t.id from tr_runs t where t.trace_id=e.trace_root order by t.start_time limit 1), e.cost_usd, e.total_tokens, e.latency_ms from eval_runs e where e.thread_id='{tid}' order by e.id")]
runs = [r for r in runs if r[1]]
out = {"thread": tid, "runs": len(runs), "cost_usd": round(sum(float(r[2] or 0) for r in runs), 5), "tokens": sum(int(r[3] or 0) for r in runs), "latency_s": round(sum(int(r[4] or 0) for r in runs) / 1000, 1)}
sc = {}
for n, e in evs.items():
    final_root = (psq(f"select id from tr_runs where trace_id in (select trace_id from tr_runs where thread_id='{tid}') and run_type='chain' and name like 'AI Lead%' and coalesce(outputs->>'answer','')<>'' order by start_time desc limit 1") or [None])[0]
    targets = ([["", final_root]] if final_root else runs[-1:]) if n == "phong_lead_report_quality" else runs
    best = None
    for r in targets:
        try:
            res = s.post(U + f"/api/evaluators/{e}/run_on/{r[1]}", timeout=200).json()[0]
            if best is None or (res.get("score") or 0) > (best.get("score") or 0): best = res
        except Exception as x: best = best or {"score": None, "comment": str(x)[:100]}
    sc[n] = {"score": best.get("score"), "comment": (best.get("comment") or "")[:160]}
out["scores"] = sc
roots = ",".join(f"'{r[1]}'" for r in runs)
spans = [l.split("|") for l in psq(f"select t.name, count(*), round(avg(extract(epoch from (t.end_time-t.start_time))*1000)) from tr_runs t where t.trace_id in (select trace_id from tr_runs where id in ({roots})) and t.name like 'sub-agent:%' group by 1")] if runs else []
mem = psq(f"with tr as (select distinct trace_id from tr_runs where thread_id='{tid}') select count(distinct name) filter (where name in ('sub-agent: seo','sub-agent: content','sub-agent: ads','sub-agent: crm-sales','sub-agent: analytics')), count(*) filter (where name='sub-agent: general-purpose') from tr_runs where trace_id in (select trace_id from tr)")
if mem: m = mem[0].split("|"); out["delegation_thread"] = {"members_used": int(m[0]), "general_purpose": int(m[1]), "score": round(int(m[0]) / 5 * (0 if int(m[1]) else 1), 2)}
out["subagent_spans"] = {x[0]: {"calls": int(x[1]), "avg_ms": x[2]} for x in spans if len(x) == 3}
print(json.dumps(out, ensure_ascii=False, indent=1))
json.dump(out, open(f"/workspace/adk_ha_build/tests/score_{tid}.json", "w"), ensure_ascii=False, indent=1)
