import json, time, requests
c = dict(l.split("=", 1) for l in open("/workspace/secrets/deepagent_web.txt").read().split())
U = c["url"].rstrip("/"); s = requests.Session(); s.auth = ("moza", c["password"])
R = {}
def ok(n, cond, extra=""): R[n] = bool(cond); print(("PASS " if cond else "FAIL ") + n, str(extra)[:260], flush=True)
def stream(aid, tid, msg):
    time.sleep(4); r = s.post(U + "/chat/stream", json={"agent_id": aid, "thread_id": tid, "message": msg}, stream=True, timeout=400)
    return [json.loads(l) for l in r.iter_lines() if l]
for p in ["/traces", "/playground", "/deployments", "/fleet", "/gateway"]:
    ok(f"page {p}", requests.get(U + p).status_code == 401 and s.get(U + p).status_code == 200)
ags = {a["name"]: a["id"] for a in s.get(U + "/api/agents").json()}; aid = ags["Đồng hồ (test UI)"]
ev = stream(aid, "t2-" + str(int(time.time())), "Chào, tôi tên là Huy. Bây giờ là mấy giờ?"); d = ev[-1]
ok("chat UI-created agent", not d["error"] and any("GetDateTime" in (e.get("name") or "") for e in ev), f"{d['reply']!r} models={d.get('models_used')}")
items = s.get(U + "/api/store/items", params={"ns": f"agent/{aid}/memories"}).json(); ok("long-term memory item", len(items) >= 1, [i["value"] for i in items])
ev = stream(1, "t-report-" + str(int(time.time())), "Lập báo cáo tổng hợp tình trạng nhà hiện tại giúp tôi (nhiệt độ, độ ẩm, đèn)."); d = ev[-1]
ok("trace: write_todos", any(e.get("kind") == "todos" for e in ev))
ok("trace: sub-agent bao-cao", any(e.get("kind") == "subagent" and e["args"].get("subagent_type") == "bao-cao" for e in ev))
ok("trace: tool inside sub-agent", any(e.get("kind") == "tool" and e["depth"] > 0 for e in ev), f"err={d['error']} reply={d['reply'][:200]!r} models={d.get('models_used')}")
rep = d["run_id"]; run = s.get(U + f"/api/runs/{rep}").json()["run"]; sp = run["spans"] or []
sub = [x for x in sp if x["kind"] == "subagent"]; kids = [x for x in sp if sub and x["parent"] == sub[0]["id"]]
ok("span tree: sub-agent has nested llm+tool spans", sub and any(k["kind"] == "llm" for k in kids) and any(k["kind"] == "tool" for k in kids),
   f"{len(sp)} spans; kinds={[x['kind'] for x in sp]}; sub children={[k['kind']+':'+k['name'] for k in kids]}")
ok("span llm tokens sum == run tokens(in)", sum(x.get("input_tokens", 0) for x in sp if x["kind"] == "llm" and x.get("status") == "ok") == run["input_tokens"],
   f"{sum(x.get('input_tokens',0) for x in sp if x['kind']=='llm' and x.get('status')=='ok')} vs {run['input_tokens']} cost={run['cost_usd']}")
ev = stream(2, "t-kb-" + str(int(time.time())), "Máy lọc nước thay lõi bao lâu một lần, lần gần nhất khi nào?"); d = ev[-1]
ok("KB agent uses tra_cuu_kien_thuc", any(e.get("name") == "tra_cuu_kien_thuc" for e in ev) and "6" in d["reply"], d["reply"][:120])
s.post(U + "/score", json={"run_id": rep, "thumb": 1, "score": 5, "comment": "báo cáo tốt"})
j = s.post(U + "/judge", json={"run_id": rep}).json(); ok("AI judge", 1 <= j.get("score", 0) <= 5, j)
# sandbox
def sbx(code): return s.post(U + "/api/sandbox/run", json={"code": code}, timeout=60).json()
r = sbx("print(sum(i*i for i in range(10**6)))"); ok("sandbox runs code", "333332833333500000" in r["output"], r)
r = sbx("import urllib.request\ntry:\n urllib.request.urlopen('http://1.1.1.1',timeout=3);print('NET OK')\nexcept Exception as e: print('NO NET',type(e).__name__)"); ok("sandbox no network", "NO NET" in r["output"], r["output"])
r = sbx("open('/etc/x','w')"); ok("sandbox read-only rootfs", "Read-only" in r["output"] or "Permission" in r["output"], r["output"][-120:])
r = sbx("a=bytearray(400*1024*1024);print('ALLOC OK')"); ok("sandbox 256MB limit", "ALLOC OK" not in r["output"], r["output"][-120:])
r = sbx("while True: pass"); ok("sandbox timeout kills", "HẾT GIỜ" in r["output"], r)
cfg = {"name": "Máy tính (sandbox)", "description": "Tính toán bằng code Python trong sandbox", "system_prompt": "Luôn dùng chay_code_python để tính toán, rồi trả lời tiếng Việt ngắn gọn.",
       "tools": [], "planning": False, "filesystem": False, "sandbox": True, "rpm_limit": 10}
sa = s.post(U + "/api/agents", json=cfg).json()["id"]
ev = stream(sa, "t-sbx", "Tính tổng các số nguyên tố nhỏ hơn 10000."); d = ev[-1]
ok("agent uses sandbox tool", any(e.get("name") == "chay_code_python" for e in ev) and "5736396" in d["reply"].replace(".", "").replace(",", ""), d["reply"][:120])
# playground + prompts
r = s.post(U + "/api/playground/run", json={"template": "Viết một câu chào {{ten}} buổi {{buoi}}, dưới 15 từ.", "variables": {"ten": "anh Huy", "buoi": "sáng"}, "temperature": 0.7}).json()
ok("playground run", r.get("output") and "anh Huy" in r["rendered"], f"{r.get('model')} {r.get('output','')[:80]!r} {r.get('cost_usd')}")
p1 = s.post(U + "/api/prompts", json={"name": "chao-buoi", "template": "Chào {{ten}}", "variables": {"ten": "Huy"}}).json()
p2 = s.post(U + "/api/prompts", json={"name": "chao-buoi", "template": "Xin chào {{ten}} buổi {{buoi}}", "variables": {"ten": "Huy", "buoi": "sáng"}, "note": "thêm buổi"}).json()
ok("prompt library versions", p1["version"] == 1 and p2["version"] == 2 and len(s.get(U + f"/api/prompts/{p1['id']}").json()) == 2)
# deployments (agent 1 -> versions): make v2 of agent 2 to test rollback
g2 = s.get(U + "/api/agents/2").json()["config"]; g2["temperature"] = 0.1; g2["note"] = "temp 0.1"; v2 = s.put(U + "/api/agents/2", json=g2).json()["version"]
slug = "tra-cuu-" + str(int(time.time()) % 100000)
dp = s.post(U + "/api/deployments", json={"agent_id": 2, "slug": slug}).json(); key = dp["api_key"]
ok("deploy creates endpoint+key", dp["version"] == v2 and key.startswith("mz_"), {k: v for k, v in dp.items() if k != "api_key"})
time.sleep(4)
r = requests.post(U + f"/d/{slug}/invoke", headers={"X-API-Key": key}, json={"message": "Đổ rác vào những ngày nào?"}, timeout=300)
ok("public invoke w/ key (no basic auth)", r.ok and r.json()["agent_version"] == v2, r.text[:200])
ok("public invoke wrong key 401", requests.post(U + f"/d/{slug}/invoke", headers={"X-API-Key": "sai"}, json={"message": "x"}).status_code == 401)
s.patch(U + f"/api/deployments/{dp['id']}", json={"version": 1})
ok("rollback deployment -> v1", requests.get(U + f"/d/{slug}/health").json()["version"] == 1)
s.patch(U + f"/api/deployments/{dp['id']}", json={"status": "paused"})
ok("paused -> 503", requests.post(U + f"/d/{slug}/invoke", headers={"X-API-Key": key}, json={"message": "x"}).status_code == 503)
s.patch(U + f"/api/deployments/{dp['id']}", json={"status": "active"}); nk = s.patch(U + f"/api/deployments/{dp['id']}", json={"rotate_key": True}).json()["api_key"]
ok("rotate key: old key 401", requests.post(U + f"/d/{slug}/invoke", headers={"X-API-Key": key}, json={"message": "x"}).status_code == 401 and nk != key)
open("/workspace/secrets/deepagent_deploy_test.txt", "w").write(f"slug={slug}\napi_key={nk}\n")
dl = [x for x in s.get(U + "/api/deployments").json() if x["slug"] == slug][0]; ok("deployment history", len(dl["history"]) >= 5, [h["action"] for h in dl["history"]])
# budget
bc = dict(cfg); bc.update(name="Ngân sách test", budget_usd_day=0.0000001, sandbox=False, system_prompt="Trả lời ngắn.")
ba = s.post(U + "/api/agents", json=bc).json()["id"]
ev = stream(ba, "t-bud", "Xin chào"); d = ev[-1]; ev = stream(ba, "t-bud", "Xin chào lần 2"); d2 = ev[-1]
ok("budget blocks after limit", d2["error"] and "ngân sách" in d2["error"], d2["error"])
s.delete(U + f"/api/agents/{ba}")
# test set
ts = s.post(U + "/api/testsets", json={"name": "Kiến thức nhà"}).json()["id"]
s.post(U + f"/api/testsets/{ts}/cases", json={"question": "Đổ rác vào những ngày nào?", "expected": "Tối thứ Ba và thứ Sáu lúc 19h"})
s.post(U + f"/api/testsets/{ts}/cases", json={"question": "Camera cổng lau bao lâu một lần?", "expected": "Mỗi tháng"})
s.post(U + f"/api/testsets/{ts}/run", json={"agent_id": 2, "agent_version": 1}); time.sleep(3); s.post(U + f"/api/testsets/{ts}/run", json={"agent_id": 2, "agent_version": v2})
for _ in range(80):
    d = s.get(U + f"/api/testsets/{ts}").json()
    if d["test_runs"] and all(t["status"] != "đang chạy" for t in d["test_runs"]): break
    time.sleep(5)
for t in d["test_runs"]: print("   test run", t["id"], t["agent_name"], "v", t["agent_version"], t["status"], [x["ai_score"] for x in d["cells"] if x["test_run_id"] == t["id"]])
ok("test set side-by-side 2x2 judged", len(d["cells"]) == 4 and all(x["ai_score"] for x in d["cells"]))
rv = s.get(U + "/api/runs", params={"agent_id": 2, "agent_version": v2}).json(); ok("eval filter agent+version", rv and all(x["agent_version"] == v2 for x in rv), len(rv))
# fleet, gateway, settings
f = s.get(U + "/api/fleet").json(); ok("fleet", any(a["deployments"] for a in f), [(a["name"], a["runs"], a["errors"], float(a["cost"])) for a in f])
g = s.get(U + "/api/gateway").json(); ok("gateway usage", g["by_model"] and g["key"]["fingerprint"].startswith("sha256:"), [(m["model"], m["calls"], m["errors"]) for m in g["by_model"]])
r = s.get(U + "/api/settings"); st = r.json(); ok("settings key never shown", "AQ." not in r.text and st["gemini_key"]["works"] is True)
print("RAM", st["ram"], "disk", st["disk"], "services", st["services"])
print("SUMMARY", sum(R.values()), "/", len(R), "fails:", [k for k, v in R.items() if not v])
