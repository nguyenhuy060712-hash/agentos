"""E2E Phòng AI số: lead plans campaign → delegates → HITL rounds → CRM/ads/analytics → evaluators. Prints metrics JSON."""
import json, time, uuid, requests, sys
c = dict(l.split("=", 1) for l in open("/workspace/secrets/deepagent_web.txt").read().split())
U = c["url"].rstrip("/"); s = requests.Session(); s.auth = ("moza", c["password"])
ids = json.load(open("/workspace/adk_ha_build/tests/team_ids.json")); evs = json.load(open("/workspace/adk_ha_build/tests/eval_ids.json"))
caps0 = s.get(U + "/api/gateway/caps").json()
tid = "phong-e2e-" + uuid.uuid4().hex[:6]
MSG = sys.argv[1] if len(sys.argv) > 1 else ("Lên chiến dịch ra mắt 'Drip bag Tết 2027' cho Cà phê Moza trong 2 tuần, ngân sách quảng cáo 8.000.000 VND. "
 "Hãy: (1) seo làm SEO brief; (2) content viết 1 bài Facebook theo brief rồi publish lên Facebook page; (3) ads đề xuất và launch 1 campaign Facebook (≤8 triệu); "
 "(4) crm-sales xử lý 2 khách tiềm năng: chị Lan (văn phòng 20 người, quan tâm gói office) và anh Minh (mua 3 hộp drip bag làm quà) — gửi lời chào bán ngắn, log deal và hẹn follow-up; "
 "(5) analytics tính CTR, CPA, pipeline value và ROAS ước tính bằng sandbox từ dữ liệu ads + CRM. Cuối cùng review và báo cáo tổng hợp.")
t0 = time.time(); rounds = []
r = s.post(U + "/chat", json={"agent_id": ids["lead"], "thread_id": tid, "message": MSG}, timeout=900).json()
last = r
while last.get("paused") and len(rounds) < 6:
    ap = last["approval_id"]
    info = [a for a in s.get(U + "/api/approvals?status=all").json() if a["id"] == ap][0]
    acts = [x["name"] for p in (info["payload"] or []) for x in (p.get("action_requests") or [])]
    rounds.append({"approval_id": ap, "actions": acts, "decision": "approve"})
    d = s.post(U + f"/api/approvals/{ap}", json={"decision": "approve"}, timeout=900).json()
    last = (d.get("events") or [{}])[-1]
out = {"thread": tid, "wall_s": round(time.time() - t0, 1), "first": {k: r.get(k) for k in ("run_id", "trace_id", "cost_usd", "tokens", "latency_ms", "error", "paused")},
       "hitl_rounds": rounds, "final": {k: last.get(k) for k in ("run_id", "trace_id", "cost_usd", "tokens", "latency_ms", "error", "paused")},
       "reply": (last.get("reply") or "")}
tr = r.get("trace_id"); scores = {}
time.sleep(3)
for n, e in evs.items():
    try: scores[n] = s.post(U + f"/api/evaluators/{e}/run_on/{tr}", timeout=200).json()
    except Exception as x: scores[n] = str(x)
out["scores"] = scores
caps1 = s.get(U + "/api/gateway/caps").json(); out["caps_before"] = caps0; out["caps_after"] = caps1
json.dump(out, open(f"/workspace/adk_ha_build/tests/e2e_{tid}.json", "w"), ensure_ascii=False, indent=1)
print(json.dumps(out, ensure_ascii=False, indent=1)[:6000])
