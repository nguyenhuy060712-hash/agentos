import json, time, requests, re
c = dict(l.split("=", 1) for l in open("/workspace/secrets/deepagent_web.txt").read().split())
U, A = c["url"].rstrip("/"), ("moza", c["password"]); s = requests.Session(); s.auth = A
def ok(n, cond, extra=""): print(("PASS " if cond else "FAIL ") + n, extra, flush=True)
def stream(aid, tid, msg):
    time.sleep(20)
    r = s.post(U + "/chat/stream", json={"agent_id": aid, "thread_id": tid, "message": msg}, stream=True, timeout=300)
    evs = [json.loads(l) for l in r.iter_lines() if l]; return evs
for p in ["/", "/agents", "/tools", "/memory", "/eval", "/settings", "/docs"]:
    a, b = requests.get(U + p).status_code, s.get(U + p).status_code
    ok(f"page {p}", a == 401 and b == 200, f"noauth={a} auth={b}")
# Tools/MCP
m = s.get(U + "/api/mcp").json(); tl = {t["tool"]: t for t in m["tools"]}
ok("MCP ha ok", m["servers"][0]["status"] == "ok", f"{len(tl)} tools")
ok("only read tools enabled", sorted(t for t, v in tl.items() if v["enabled"]) == ["GetDateTime", "GetLiveContext"], str(sorted(t for t,v in tl.items() if v["enabled"])))
r = s.post(U + "/api/mcp/test", json={"key": "ha:homeassistant__GetLiveContext", "args": {}}); ok("test-call GetLiveContext", r.ok and "temperature" in r.text.lower() or "°" in r.text, r.text[:120])
w = next((t for t in tl if not tl[t]["read_only"]), None)
r = s.post(U + "/api/mcp/test", json={"key": "ha:" + w, "args": {}}); ok(f"test-call write tool {w} refused", r.status_code == 403)
print("blocked-by-name:", [t for t, v in tl.items() if v["blocked"]], "devices:", m["blocked_devices"])
# KB
r = s.post(U + "/api/kb/text", json={"title": "Nội quy nhà Moza", "text": "Giờ đổ rác: tối thứ Ba và thứ Sáu lúc 19h. Mật khẩu wifi khách đổi mỗi tháng, hỏi anh Huy.\n\nMáy lọc nước thay lõi mỗi 6 tháng, lần gần nhất tháng 8/2026. Điều hoà phòng ngủ vệ sinh mỗi quý."}).json(); ok("KB add text", r.get("chunks", 0) >= 1, str(r))
r = s.post(U + "/api/kb/upload", files={"file": ("lich_bao_tri.md", "# Lịch bảo trì\n\nBình nóng lạnh: kiểm tra van an toàn vào tháng 1 hằng năm.\nCamera cổng: lau ống kính mỗi tháng.".encode())}).json(); ok("KB upload .md", r.get("chunks", 0) >= 1, str(r))
r = s.get(U + "/api/kb/search", params={"q_": "khi nào đổ rác?"}).json(); ok("KB search", "đổ rác" in r[0]["text"], f"top={r[0]['title']} {r[0]['score']}")
# Agent created through the UI's API
cfg = {"name": "Đồng hồ (test UI)", "description": "Trả lời giờ hiện tại", "system_prompt": "Trả lời tiếng Việt, một câu. Dùng GetDateTime khi được hỏi giờ. Hãy ghi nhớ tên người dùng bằng luu_ghi_nho khi họ giới thiệu.",
       "model": "gemini-3.6-flash", "temperature": 0.2, "tools": ["ha:llm__GetDateTime"], "subagents": [], "planning": False, "filesystem": False, "long_term_memory": True}
a = s.post(U + "/api/agents", json=cfg).json(); aid = a["id"]; ok("create agent", a["version"] == 1, str(a))
ev = stream(aid, "t1", "Chào, tôi tên là Huy. Bây giờ là mấy giờ?"); d = ev[-1]
ok("chat new agent", d["type"] == "done" and not d["error"] and any("GetDateTime" in (e.get("name") or "") for e in ev), d["reply"][:120])
ok("planning off => no write_todos", not any(e.get("kind") == "todos" for e in ev))
cfg["temperature"] = 0.0; cfg["note"] = "temp 0"; v2 = s.put(U + f"/api/agents/{aid}", json=cfg).json(); ok("edit => v2", v2["version"] == 2)
rb = s.post(U + f"/api/agents/{aid}/rollback/1").json(); ok("rollback v1 => v3", rb["version"] == 3)
g = s.get(U + f"/api/agents/{aid}").json(); ok("history 3 versions, temp back to 0.2", len(g["versions"]) == 3 and g["config"]["temperature"] == 0.2)
cl = s.post(U + f"/api/agents/{aid}/clone").json(); ex = s.get(U + f"/api/agents/{aid}/export").json()
im = s.post(U + "/api/agents/import", json=ex).json(); ok("clone + export + import", cl["id"] and im["id"], f"clone={cl['id']} import={im['id']}")
for x in (cl["id"], im["id"]): s.delete(U + f"/api/agents/{x}")
ok("delete", all(x["id"] not in (cl["id"], im["id"]) for x in s.get(U + "/api/agents").json()))
# memory store
time.sleep(1); ns = s.get(U + "/api/store/namespaces").json(); nsn = f"agent/{aid}/memories"
items = s.get(U + "/api/store/items", params={"ns": nsn}).json() if [ "agent", str(aid), "memories"] in ns else []
ok("long-term memory saved", len(items) >= 1, str([i["value"] for i in items])[:150])
s.post(U + "/api/store/items", json={"ns": nsn, "key": "xoa-thu", "text": "mục tạm"}); s.delete(U + "/api/store/items", params={"ns": nsn, "key": "xoa-thu"})
ok("store add+delete", all(i["key"] != "xoa-thu" for i in s.get(U + "/api/store/items", params={"ns": nsn}).json()))
# sub-agent + todos
ev = stream(1, "t-report-" + str(int(time.time())), "Lập báo cáo tổng hợp tình trạng nhà hiện tại giúp tôi (nhiệt độ, độ ẩm, đèn).")
kinds = [e.get("kind") for e in ev]; d = ev[-1]
ok("Trợ lý nhà: write_todos in trace", "todos" in kinds); ok("Trợ lý nhà: sub-agent call in trace", any(e.get("kind") == "subagent" and e["args"].get("subagent_type") == "bao-cao" for e in ev))
ok("sub-agent tool call at depth>0", any(e.get("kind") == "tool" and e["depth"] > 0 for e in ev), f"steps={len(ev)-1} reply={d['reply'][:150]!r} err={d['error']}")
rep_run = d["run_id"]
# KB agent
ev = stream(2, "t-kb", "Máy lọc nước thay lõi bao lâu một lần, lần gần nhất khi nào?"); d = ev[-1]
ok("Tra cứu kiến thức agent", any(e.get("name") == "tra_cuu_kien_thuc" for e in ev) and "6" in d["reply"], d["reply"][:150])
# eval
r = s.get(U + "/api/runs", params={"agent_id": aid, "agent_version": 1}).json(); ok("eval filter agent+version", len(r) >= 1 and all(x["agent_id"] == aid for x in r), f"{len(r)} runs")
s.post(U + "/score", json={"run_id": rep_run, "thumb": 1, "score": 5, "comment": "báo cáo tốt"}); time.sleep(15); j = s.post(U + "/judge", json={"run_id": rep_run}).json(); ok("AI judge", 1 <= j["score"] <= 5, str(j)[:150])
ts = s.post(U + "/api/testsets", json={"name": "Kiến thức nhà"}).json()["id"]
s.post(U + f"/api/testsets/{ts}/cases", json={"question": "Đổ rác vào những ngày nào?", "expected": "Tối thứ Ba và thứ Sáu lúc 19h"})
s.post(U + f"/api/testsets/{ts}/cases", json={"question": "Camera cổng lau bao lâu một lần?", "expected": "Mỗi tháng"})
for ag in (2, 1): time.sleep(30); s.post(U + f"/api/testsets/{ts}/run", json={"agent_id": ag})
for _ in range(60):
    d = s.get(U + f"/api/testsets/{ts}").json()
    if all(t["status"] != "đang chạy" for t in d["test_runs"]): break
    time.sleep(5)
for t in d["test_runs"]:
    cs = [x for x in d["cells"] if x["test_run_id"] == t["id"]]
    print(f"  test run #{t['id']} {t['agent_name']} v{t['agent_version']} {t['status']}: scores={[x['ai_score'] for x in cs]}")
ok("test set side-by-side", len(d["test_runs"]) == 2 and len(d["cells"]) == 4)
st = s.get(U + "/api/stats").json(); ok("stats", st["total"]["runs"] > 4, json.dumps(st["total"]))
# settings
r = s.get(U + "/api/settings"); js = r.json()
ok("settings: key not leaked", "AQ." not in r.text and js["gemini_key"]["works"] is True)
print("RAM:", js["ram"], "disk:", js["disk"], "services:", js["services"])
print("AGENT_ID", aid)
