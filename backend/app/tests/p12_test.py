"""E2E P1 (Observability) + P2 (Evaluation) qua URL public (tunnel + basic auth)."""
import json, sys, time, random, requests
sys.path.insert(0, "/workspace/adk_ha_build/os/sdk"); import agentos_sdk as ao
c = dict(l.split("=", 1) for l in open("/workspace/secrets/deepagent_web.txt").read().split())
U = c["url"].rstrip("/"); s = requests.Session(); s.auth = ("moza", c["password"])
R = {}
def ok(n, cond, extra=""): R[n] = bool(cond); print(("PASS " if cond else "FAIL ") + n, "|", str(extra)[:240], flush=True)
def wait(fn, t=120, step=5):
    end = time.time() + t
    while time.time() < end:
        v = fn()
        if v: return v
        time.sleep(step)
    return fn()
TAG = str(int(time.time()))
prj = {p["name"]: p for p in s.get(U + "/api/obs/projects").json()}; P = prj["sdk-demo"]["id"]; PA = prj["agents"]["id"]
# ---------- P1: SDK tracing
key = s.post(U + f"/api/obs/projects/{P}/keys", params={"label": "test"}).json()["api_key"]
ok("ingest without key -> 401", requests.post(U + "/ingest/runs", json={"post": []}).status_code == 401)
ok("ingest no basic auth needed (key ok)", requests.post(U + "/ingest/runs", json={"post": []}, headers={"X-API-Key": key}).status_code == 200)
ao.configure(endpoint=U, api_key=key)
@ao.traceable(run_type="retriever", tags=["demo"])
def retrieve(q): time.sleep(0.05); return ["doc về " + q]
@ao.traceable(run_type="llm")
def fake_llm(prompt):
    r = ao.get_current_run(); time.sleep(random.uniform(0.05, 0.4)); r.set_usage(120, 40, "gemini-3.1-flash-lite"); return "Trả lời: " + prompt[:30]
@ao.traceable(name="rag_pipeline", tags=["sdk", "t" + TAG], metadata={"env": "test"})
def pipeline(q):
    if "lỗi" in q: raise ValueError("lỗi giả lập")
    return fake_llm(" ".join(retrieve(q)) + " | " + q)
qs = ["nhiệt độ phòng khách", "đèn bếp", "độ ẩm", "báo lỗi đi", "lịch tưới cây", "giá điện"]
for i, q in enumerate(qs):
    try: pipeline(q, agentos_thread_id=f"th-{TAG}-{i % 2}")
    except ValueError: pass
ao.flush()
roots = wait(lambda: [r for r in s.get(U + "/api/obs/runs", params={"project_id": P, "filter": f'has(tags,"t{TAG}")'}).json()] if len(s.get(U + "/api/obs/runs", params={"project_id": P, "filter": f'has(tags,"t{TAG}")'}).json()) >= 6 else None, 30, 2)
ok("SDK traces ingested (6 roots)", roots and len(roots) == 6, len(roots or []))
tr = s.get(U + f"/api/obs/traces/{roots[0]['trace_id']}").json()["runs"]
ok("trace tree has nested llm+retriever", {r["run_type"] for r in tr} >= {"chain", "llm", "retriever"} and all(r["trace_id"] == roots[0]["trace_id"] for r in tr), [(r["name"], r["run_type"]) for r in tr])
llm = [r for r in tr if r["run_type"] == "llm"]
ok("usage/cost recorded on llm run", llm and llm[0]["total_tokens"] == 160 and float(llm[0]["cost_usd"] or 0) > 0, (llm[0]["total_tokens"], llm[0]["cost_usd"]) if llm else None)
f = lambda q: s.get(U + "/api/obs/runs", params={"project_id": P, "filter": q}).json()
ok("filter eq(status,error)", len(f(f'and(has(tags,"t{TAG}"), eq(status,"error"))')) == 1)
ok("filter search()", len(f(f'and(has(tags,"t{TAG}"), search("đèn bếp"))')) == 1)
ok("filter metadata + not", len(f(f'and(has(tags,"t{TAG}"), eq(metadata.env,"test"), not(eq(status,"error")))')) == 5)
ok("filter gt(latency) numeric", isinstance(f(f'and(has(tags,"t{TAG}"), gte(latency,0))'), list))
bad = s.get(U + "/api/obs/runs", params={"project_id": P, "filter": "eq(status;drop table x)"})
ok("filter injection rejected 400", bad.status_code == 400, bad.status_code)
th = s.get(U + f"/api/obs/projects/{P}/threads").json(); mine = [t for t in th if t["thread_id"].startswith(f"th-{TAG}")]
ok("threads view (2 threads x3 turns)", len(mine) == 2 and all(t["turns"] == 3 for t in mine), [(t["thread_id"], t["turns"]) for t in mine])
ao.feedback(roots[0]["id"], "dung", score=1, comment="sdk feedback"); ao.flush()
time.sleep(2); ok("feedback via SDK + filter feedback.dung", len(f(f'and(has(tags,"t{TAG}"), gte(feedback.dung,1))')) == 1)
db = s.get(U + f"/api/obs/projects/{P}/dashboard", params={"hours": 24}).json()
ok("dashboard p50/p95/errors/tokens/cost", db.get("series") and all(k in db["series"][-1] for k in ("p50", "p95", "errors", "n")), {k: db["series"][-1].get(k) for k in list(db["series"][-1])[:9]})
# agent trace -> obs (real Gemini)
r = s.post(U + "/chat/stream", json={"agent_id": 6, "thread_id": "p1-" + TAG, "message": "Bây giờ là mấy giờ?"}, stream=True, timeout=300)
ev = [json.loads(l) for l in r.iter_lines() if l]; d = ev[-1]
ok("agent run mirrored to obs (trace_id in done)", d.get("trace_id"), (d.get("reply") or "")[:80])
at = wait(lambda: s.get(U + f"/api/obs/traces/{d['trace_id']}").json() if s.get(U + f"/api/obs/traces/{d['trace_id']}").status_code == 200 else None, 20, 2)
ok("agent trace has llm+tool child runs", at and {x["run_type"] for x in at["runs"]} >= {"llm", "tool"}, [(x["name"], x["run_type"]) for x in (at or {"runs": []})["runs"]][:8])
# share link
sh = s.post(U + "/api/obs/share", json={"trace_id": roots[0]["trace_id"], "hours": 1}).json()
pub = requests.get(U + sh["path"]); ok("share link opens WITHOUT auth", pub.status_code == 200 and "rag_pipeline" in pub.text, pub.status_code)
ok("tampered share -> 403", requests.get(U + sh["path"][:-1] + ("a" if sh["path"][-1] != "a" else "b")).status_code == 403)
s.delete(U + f"/api/obs/share/{sh['id']}"); ok("revoked share -> 403", requests.get(U + sh["path"]).status_code == 403)
ok("private pages still need auth", requests.get(U + "/projects").status_code == 401 and s.get(U + "/projects").status_code == 200)
# alert -> draft (external webhook) 
al = s.post(U + "/api/obs/alerts", json={"project_id": P, "name": f"Lỗi SDK {TAG}", "metric": "error_rate", "op": "gt", "threshold": 0.05, "window_min": 30,
                                         "filter": f'has(tags,"t{TAG}")', "channel": "webhook", "target": "https://example.com/hook"}).json()["id"]
ok("alert test computes error_rate", abs(s.post(U + f"/api/obs/alerts/{al}/test").json()["value"] - 1/6) < 1e-6)
# ---------- P2: datasets
ds = s.post(U + "/api/datasets", json={"name": f"smoke-{TAG}", "description": "từ trace SDK"}).json()["id"]
okr = [x["id"] for x in roots if x["status"] != "error"][:3]
s.post(U + f"/api/datasets/{ds}/from_runs", json={"run_ids": okr, "split": "train"})
v1 = s.get(U + f"/api/datasets/{ds}").json()
s.post(U + f"/api/datasets/{ds}/tags", json={"tag": "v-goc"})
s.post(U + f"/api/datasets/{ds}/import_csv", json={"csv": "question,answer\nMấy giờ rồi?,giờ\nHôm nay thứ mấy?,thứ\n", "input_cols": ["question"], "output_cols": ["answer"], "split": "test"})
v2 = s.get(U + f"/api/datasets/{ds}").json()
ok("dataset from runs + csv, version bump", len(v2["examples"]) == 5 and v2["version"] > v1["version"], (v1["version"], v2["version"], len(v2["examples"])))
old = s.get(U + f"/api/datasets/{ds}", params={"tag": "v-goc"}).json()
ok("dataset tag -> old version view (3 ex)", len(old["examples"]) == 3, len(old["examples"]))
ok("split filter test=2", len(s.get(U + f"/api/datasets/{ds}", params={"split": "test"}).json()["examples"]) == 2)
# rule: online sample -> dataset + queue + evaluator + tag (backfill)
evs = {e["name"]: e["id"] for e in s.get(U + "/api/evaluators").json()}
q = s.post(U + "/api/queues", json={"name": f"Duyệt SDK {TAG}", "instructions": "Chấm 1-5", "rubric": [{"key": "chat_luong", "type": "score", "min": 1, "max": 5}]}).json()["id"]
ds2 = s.post(U + "/api/datasets", json={"name": f"loi-{TAG}"}).json()["id"]
rule = s.post(U + "/api/obs/rules", json={"project_id": P, "name": f"rule {TAG}", "filter": f'has(tags,"t{TAG}")', "sampling_rate": 1, "backfill_hours": 1,
       "actions": [{"type": "add_to_queue", "queue_id": q}, {"type": "add_tag", "tag": "da-duyet"}, {"type": "run_evaluator", "evaluator_id": evs["co_chu_so"]},
                   {"type": "add_to_dataset", "dataset_id": ds2}, {"type": "webhook", "url": "https://example.com/rule"}]}).json()["id"]
rl = wait(lambda: next((x for x in s.get(U + "/api/obs/rules").json() if x["id"] == rule and x["n_applied"] >= 6), None), 90, 5)
ok("automation rule applied to 6 traces (worker)", rl, rl and (rl["n_matched"], rl["n_applied"]))
ok("rule add_tag", len(f(f'and(has(tags,"t{TAG}"), has(tags,"da-duyet"))')) == 6)
ok("rule run_evaluator (online, code in sandbox)", wait(lambda: len(f(f'and(has(tags,"t{TAG}"), gte(feedback.co_chu_so,0))')) >= 5, 60, 5))
ok("rule add_to_dataset", len(s.get(U + f"/api/datasets/{ds2}").json()["examples"]) == 6)
ob = s.get(U + "/api/obs/outbox").json()
ok("external webhook -> DRAFT only (no send)", any(o["target"] == "https://example.com/rule" and o["status"] == "draft" for o in ob))
fired = wait(lambda: next((a for a in s.get(U + "/api/obs/alerts").json() if a["id"] == al and a["events"]), None), 90, 5)
ok("alert fired -> outbox draft", fired and any(o["target"] == "https://example.com/hook" and o["status"] == "draft" for o in s.get(U + "/api/obs/outbox").json()), fired and fired["events"][0]["delivery"])
# annotation queue
it = s.get(U + f"/api/queues/{q}/next").json()["item"]
ok("queue has items", it and it.get("id"), it and it.get("run_id"))
s.post(U + f"/api/queue_items/{it['id']}", json={"scores": {"chat_luong": 4}, "comment": "ổn"})
ok("queue submit -> feedback chat_luong", len(f(f'and(has(tags,"t{TAG}"), eq(feedback.chat_luong,4))')) == 1)
# evaluators
ok("code evaluator test", s.post(U + f"/api/evaluators/{evs['co_chu_so']}/test", json={"run": {"outputs": {"answer": "Bây giờ là 10:30"}}}).json()[0].get("score") == 1)
nv = s.post(U + "/api/evaluators", json={"name": f"ev_len_{TAG}", "kind": "code", "config": {"key": "do_dai", "code": "def evaluate(run, example):\n    a = str((run.get('outputs') or {}).get('answer',''))\n    return {'score': 1 if len(a) < 400 else 0, 'comment': f'{len(a)} ký tự'}"}}).json()
ok("custom code evaluator create+test", s.post(U + f"/api/evaluators/{nv['id']}/test", json={"run": {"outputs": {"answer": "ngắn"}}}).json()[0].get("score") == 1)
llmev = s.post(U + f"/api/evaluators/{evs['ngan_gon']}/test", json={"run": {"inputs": {"question": "Mấy giờ?"}, "outputs": {"answer": "10 giờ 30."}}}).json()
ok("LLM-judge evaluator test (Gemini)", isinstance(llmev, list) and llmev[0].get("score") is not None, llmev)
# experiments: 2 agent targets
dse = s.post(U + "/api/datasets", json={"name": f"gio-{TAG}"}).json()["id"]
for qq in ["Bây giờ là mấy giờ?", "Hôm nay là ngày bao nhiêu?"]:
    s.post(U + f"/api/datasets/{dse}/examples", json={"inputs": {"question": qq}, "outputs": {"answer": "trả lời đúng thời gian hiện tại"}})
ids = [evs["co_chu_so"], nv["id"], evs["ngan_gon"]]
ea = s.post(U + "/api/experiments", json={"name": "Đồng hồ #6", "dataset_id": dse, "target": {"type": "agent", "agent_id": 6}, "evaluator_ids": ids, "concurrency": 1}).json()["id"]
eb = s.post(U + "/api/experiments", json={"name": "Lịch & giờ #11", "dataset_id": dse, "target": {"type": "agent", "agent_id": 11}, "evaluator_ids": ids, "concurrency": 1}).json()["id"]
done = wait(lambda: all(x["status"] != "pending" and x["status"] != "running" for x in s.get(U + "/api/experiments", params={"dataset_id": dse}).json()), 400, 10)
ex = {x["id"]: x for x in s.get(U + "/api/experiments", params={"dataset_id": dse}).json()}
ok("2 experiments done with scores", done and all(ex[i]["status"] == "done" and ex[i]["summary"]["scores"] for i in (ea, eb)), {i: (ex[i]["status"], ex[i]["summary"]["scores"]) for i in (ea, eb)})
cmp = s.get(U + "/api/experiments/compare", params={"ids": f"{ea},{eb}"}).json()
ok("compare side-by-side", len(cmp.get("results", [])) >= 2, str(cmp)[:200])
pw = s.post(U + "/api/pairwise", json={"exp_a": ea, "exp_b": eb}).json()["id"]
pr = wait(lambda: (lambda x: x if (x.get("run") or {}).get("status") not in ("pending", "running") else None)(s.get(U + f"/api/pairwise/{pw}").json()), 200, 8)
ok("pairwise LLM comparison", bool(pr) and pr["run"]["status"] == "done" and len(pr["results"]) > 0, str(pr)[:220])
sc = s.post(U + "/api/schedules", json={"name": f"Hằng đêm {TAG}", "cron": "0 2 * * *", "dataset_id": dse, "target": {"type": "agent", "agent_id": 6}, "evaluator_ids": [evs["co_chu_so"]]}).json()
sid = sc.get("id"); rn = s.post(U + f"/api/schedules/{sid}/run_now").json()
ok("schedule create + run_now -> experiment", rn.get("experiment_id"), rn)
w = s.get(U + "/api/obs/worker").json(); ok("worker healthy (no errors)", not w["errors"], w)
for p in ["/projects", "/rules", "/datasets", "/experiments", "/evaluators", "/annotation"]:
    ok(f"page {p}", requests.get(U + p).status_code == 401 and s.get(U + p).status_code == 200)
json.dump({"tag": TAG, "P": P, "ds": ds, "dse": dse, "ea": ea, "eb": eb, "q": q, "trace": roots[0]["trace_id"], "agent_trace": d.get("trace_id")}, open("/workspace/adk_ha_build/os/tests/p12_state.json", "w"))
print(f"\nTOTAL {sum(R.values())}/{len(R)}")
