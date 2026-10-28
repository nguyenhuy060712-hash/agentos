import os
import requests as _rq
_orig=_rq.Session.request
def _req(self,*a,**k):
    k.setdefault("timeout",300); return _orig(self,*a,**k)
_rq.Session.request=_req  # no request may hang forever (tunnel drops)
"""E2E P3 (Prompt) + P4 (Builder/HITL/Cron/Webhook/Studio) + gaps P1/P2, qua URL public (tunnel + basic auth)."""
import json, os, subprocess, sys, time, random, requests
sys.path.insert(0, "/workspace/adk_ha_build/os/sdk"); import agentos_sdk as ao
c = dict(l.split("=", 1) for l in open("/workspace/secrets/deepagent_web.txt").read().split())
U = c["url"].rstrip("/"); s = requests.Session(); s.auth = ("moza", c["password"])
R = {}; TAG = str(int(time.time())); ST = {"tag": TAG}
def ok(n, cond, extra=""): R[n] = bool(cond); print(("PASS " if cond else "FAIL ") + n, "|", str(extra)[:260], flush=True)
def wait(fn, t=120, step=5):
    end = time.time() + t
    while time.time() < end:
        try:
            v = fn()
            if v: return v
        except Exception as e: print("  wait err", e)
        time.sleep(step)
    return None
def chat(aid, tid, msg):
    r = s.post(U + "/chat/stream", json={"agent_id": aid, "thread_id": tid, "message": msg}, stream=True, timeout=400)
    return [json.loads(l) for l in r.iter_lines() if l]
def J(r):
    try: return r.json()
    except Exception: return {"_status": r.status_code, "_text": r.text[:300]}
def step(name, fn):
    try: fn()
    except Exception as e:
        import traceback; ok(name + " (exception)", False, repr(e)[:300]); traceback.print_exc(limit=2)
prj = {p["name"]: p for p in s.get(U + "/api/obs/projects").json()}; P = prj["sdk-demo"]["id"]; PA = prj["agents"]["id"]
key = s.post(U + f"/api/obs/projects/{P}/keys", params={"label": "p34"}).json()["api_key"]
ao.configure(endpoint=U, api_key=key)
evs = {e["name"]: e["id"] for e in s.get(U + "/api/evaluators").json()}
PN = f"p34_{TAG}"

# ===================== P3
def p3_hub():
    r1 = J(s.post(U + "/api/hub/commit", json={"name": PN, "template": "Bạn là trợ lý nhà. Luôn bắt đầu câu trả lời bằng đúng chữ 'MOZA:'. Trả lời ngắn gọn bằng tiếng Việt về {{chu_de}}.",
                                                 "variables": {"chu_de": "thời gian"}, "message": "bản đầu", "tag": "prod", "model": "gemini-3.1-flash-lite"}))
    ST["pid"] = r1["id"]
    r2 = J(s.post(U + "/api/hub/commit", json={"name": PN, "template": "Bạn là trợ lý nhà. Luôn bắt đầu câu trả lời bằng đúng chữ 'NHÀ:'. Trả lời ngắn gọn bằng tiếng Việt về {{chu_de}}.",
                                                 "variables": {"chu_de": "thời gian"}, "message": "đổi tiền tố", "tag": "staging", "parent_version": 1}))
    ok("hub: 2 commits + tags", r1.get("version") == 1 and r2.get("version") == 2, (r1, r2))
    ok("hub pull by tag prod -> v1", J(s.get(U + "/api/hub/pull", params={"ref": f"{PN}:prod"})).get("version") == 1)
    ok("hub pull by number / latest", J(s.get(U + "/api/hub/pull", params={"ref": f"{PN}:2"})).get("version") == 2 and J(s.get(U + "/api/hub/pull", params={"ref": PN})).get("version") == 2)
    d = J(s.get(U + f"/api/hub/{ST['pid']}/diff", params={"a": 1, "b": 2}))
    ok("hub diff", "-Bạn là trợ lý nhà. Luôn bắt đầu câu trả lời bằng đúng chữ 'MOZA:'" in d.get("diff", "") and "+Bạn" in d.get("diff", ""), d.get("diff", "")[:120])
    s.post(U + "/api/hub/webhooks", json={"url": f"https://example.com/hub-{TAG}"})
    s.post(U + "/api/hub/commit", json={"name": PN, "template": "Bạn là trợ lý nhà. Luôn bắt đầu câu trả lời bằng đúng chữ 'NHÀ:'. Trả lời thật ngắn về {{chu_de}}.", "message": "v3 webhook test"})
    ob = J(s.get(U + "/api/obs/outbox"))
    ok("hub commit webhook (external) -> DRAFT only", any(o["target"] == f"https://example.com/hub-{TAG}" and o["status"] == "draft" for o in ob))
    for h in J(s.get(U + "/api/hub/webhooks/list")):
        if TAG in h["url"]: s.delete(U + f"/api/hub/webhooks/{h['id']}")
def p3_improve():
    r = J(s.post(U + "/api/prompts/improve", json={"template": "tóm tắt {{van_ban}} cho {{doi_tuong}}", "instructions": "rõ ràng, có định dạng gạch đầu dòng, tối đa 5 ý"}))
    ok("canvas AI improve keeps variables", r.get("improved") and not r.get("missing_variables") and r.get("diff"), {k: r.get(k) for k in ("missing_variables", "changes", "model")})
def p3_multi():
    r = J(s.post(U + "/api/playground/multi", json={"template": "Trả về JSON thông tin thành phố {{city}}: tên, quốc gia.", "variables": {"city": "Huế"},
                                                     "models": ["gemini-3.1-flash-lite", "gemini-flash-lite-latest"], "temperature": 0,
                                                     "output_schema": {"type": "object", "properties": {"ten": {"type": "string"}, "quoc_gia": {"type": "string"}}, "required": ["ten", "quoc_gia"]}}))
    res = r.get("results", [])
    ok("multi-model playground (2 models parallel)", len(res) == 2 and sum(1 for x in res if x["output"]) >= 1, [(x["model"], x["ms"], x["schema_valid"], (x["error"] or "")[:60]) for x in res])
    ok("structured output schema check", any(x["schema_valid"] for x in res))
    ST["multi_run"] = res[0]["run_id"] if res else None
def p3_batch():
    ds = J(s.post(U + "/api/datasets", json={"name": f"p3-{TAG}"}))["id"]; ST["ds_p3"] = ds
    s.post(U + f"/api/datasets/{ds}/examples", json=[{"inputs": {"chu_de": "giờ giấc"}}, {"inputs": {"chu_de": "thời tiết"}}])
    e = J(s.post(U + "/api/playground/batch", json={"prompt_id": ST["pid"], "version": 1, "dataset_id": ds, "model": "gemini-3.1-flash-lite", "evaluator_ids": [evs["ngan_gon"]]}))
    x = wait(lambda: next((x for x in J(s.get(U + "/api/experiments", params={"dataset_id": ds})) if x["id"] == e["experiment_id"] and x["status"] not in ("queued", "running")), None), 200, 8)
    ok("playground -> run on dataset (experiment)", x and x["status"] == "done" and x["summary"]["n"] == 2, x and (x["status"], x["summary"]["scores"]))
def p3_agent_link():
    a = J(s.post(U + "/api/agents", json={"name": f"Prompt-linked {TAG}", "system_prompt": "", "prompt_ref": f"{PN}:prod", "model": "gemini-3.1-flash-lite",
                                           "tools": [], "planning": False, "filesystem": False}))
    ST["linked"] = a["id"]
    d = chat(a["id"], f"pl-{TAG}", "Chào bạn")[-1]
    ok("agent uses Hub prompt (tag prod -> 'MOZA:')", (d.get("reply") or "").strip().upper().startswith("MOZA"), (d.get("reply") or d.get("error"))[:100])
    s.post(U + f"/api/hub/{ST['pid']}/tags", json={"tag": "prod", "version": 2})
    d = chat(a["id"], f"pl2-{TAG}", "Chào bạn")[-1]
    ok("move tag prod->v2 changes agent behaviour ('NHÀ:')", (d.get("reply") or "").strip().upper().startswith("NHÀ"), (d.get("reply") or d.get("error"))[:100])
    ok("hub shows used_by agent", any(x["id"] == a["id"] for x in J(s.get(U + f"/api/hub/{ST['pid']}"))["used_by"]))

# ===================== P4
def p4_builder():
    g = J(s.post(U + "/api/builder/generate", json={"description": "Agent báo cáo nhiệt độ và độ ẩm các phòng trong nhà mỗi sáng, chỉ đọc dữ liệu Home Assistant, kèm giờ hiện tại."}))
    cfg = g.get("config") or {}
    ok("AI builder: NL -> config with HA read tools", any("GetLiveContext" in t for t in cfg.get("tools", [])) and cfg.get("system_prompt"), (cfg.get("name"), cfg.get("tools"), g.get("warnings")))
    g2 = J(s.post(U + "/api/builder/generate", json={"description": "Agent mở khoá cửa chính và tắt báo động khi tôi về nhà."}))
    t2 = (g2.get("config") or {}).get("tools", [])
    ok("AI builder never includes lock/alarm tools", not any(__import__("re").search(r"lock|alarm|garage|khoá|khóa", t, __import__("re").I) for t in t2), (t2, g2.get("warnings")))
    cfg["name"] = f"Báo cáo sáng AI {TAG}"; cfg["model"] = "gemini-3.1-flash-lite"
    r = J(s.post(U + "/api/builder/create", json={"config": cfg})); ST["built"] = r.get("id")
    ok("AI builder create", r.get("id"), r)
def p4_hitl():
    t = J(s.post(U + "/api/builder/templates/phan-tich-du-lieu")); aid = t["id"]; ST["hitl_agent"] = aid
    a = J(s.get(U + f"/api/agents/{aid}")); cfg = a["config"]; cfg["model"] = "gemini-3.1-flash-lite"; cfg["planning"] = False
    s.put(U + f"/api/agents/{aid}", json=cfg)
    ok("template created (interrupt_on sandbox)", "local:chay_code_python" in cfg.get("interrupt_on", []))
    ev = chat(aid, f"hitl-{TAG}", "Dùng chay_code_python để tính 2**20 rồi cho tôi kết quả.")
    it = next((e for e in ev if e.get("type") == "interrupt"), None); d = ev[-1]
    ok("HITL: run pauses before write/sandbox tool (interrupt)", it and d.get("approval_id") and not any(e.get("kind") == "result" for e in ev), (it or {}).get("requests", "")[:1] if it else d)
    if not it: return
    pend = J(s.get(U + "/api/approvals"))
    ok("approval listed as pending", any(p["id"] == it["approval_id"] for p in pend))
    r = J(s.post(U + f"/api/approvals/{it['approval_id']}", json={"decision": "approve"}))
    dn = (r.get("events") or [{}])[-1]
    ok("approve -> tool runs, agent finishes (1048576)", "1048576" in (dn.get("reply") or "").replace(".", "").replace(",", ""), (dn.get("reply") or dn.get("error") or r)[:150])
    ok("double decision rejected 409", s.post(U + f"/api/approvals/{it['approval_id']}", json={"decision": "approve"}).status_code == 409)
    ev = chat(aid, f"hitl2-{TAG}", "Dùng chay_code_python tính 3**5.")
    it2 = next((e for e in ev if e.get("type") == "interrupt"), None)
    if it2:
        bad = s.post(U + f"/api/approvals/{it2['approval_id']}", json={"decision": "edit", "args": {"code": "print('mo garage')"}})
        ok("edit with blocked device text -> 403", bad.status_code == 403, bad.status_code)
        r = J(s.post(U + f"/api/approvals/{it2['approval_id']}", json={"decision": "edit", "args": {"code": "print(6*7)"}}))
        dn = (r.get("events") or [{}])[-1]
        import subprocess
        tool_out = subprocess.run(["ssh","-i",os.path.expanduser("~/.ssh/moza_vps82"),"-p","30029","root@103.142.24.220",
            "docker exec lc-postgres psql -U langgraph -d langgraph -Atc \"select outputs::text from tr_runs where run_type='tool' and name='chay_code_python' order by start_time desc limit 1\""],
            capture_output=True, text=True).stdout
        # kiểm tra tool THỰC SỰ chạy code đã sửa (LLM có thể vẫn tự trả lời sai theo lệnh gốc)
        ok("edit args -> tool executed edited code (exit=0 42)", "print(6*7)" in tool_out and "42" in tool_out.split("Tool response:")[-1], tool_out[-120:])
        ok("edit -> agent reply (thông tin)", True, (dn.get("reply") or dn.get("error") or "")[:120])
    ev = chat(aid, f"hitl3-{TAG}", "Dùng chay_code_python tính 10**3.")
    it3 = next((e for e in ev if e.get("type") == "interrupt"), None)
    if it3:
        r = J(s.post(U + f"/api/approvals/{it3['approval_id']}", json={"decision": "reject", "message": "Không được chạy code lúc này."}))
        dn = (r.get("events") or [{}])[-1]
        ok("reject -> tool NOT executed, agent responds", r.get("status") == "rejected" and not any(e.get("kind") == "result" and "1000" in (e.get("text") or "") for e in r.get("events", [])), (dn.get("reply") or "")[:150])
def p4_cron():
    cr = J(s.post(U + "/api/agent_crons", json={"agent_id": 6, "name": f"cron {TAG}", "cron": "* * * * *", "message": "Bây giờ là mấy giờ?"}))
    ok("cron bad expression -> 400", s.post(U + "/api/agent_crons", json={"agent_id": 6, "name": "x", "cron": "bad", "message": "x"}).status_code == 400)
    x = wait(lambda: next((c for c in J(s.get(U + "/api/agent_crons")) if c["id"] == cr["id"] and c["last_status"] in ("ok", "lỗi", "chờ duyệt")), None), 240, 10)
    s.patch(U + f"/api/agent_crons/{cr['id']}", params={"enabled": False})
    ok("cron agent fired by worker -> trace", x and x["last_status"] == "ok" and x["last_trace"], x and (x["last_status"], x["last_trace"]))
    ST["cron_trace"] = x and x["last_trace"]
def p4_hook():
    h = J(s.post(U + "/api/agent_hooks", json={"agent_id": 6, "name": f"hook {TAG}"}))
    ok("webhook wrong token -> 401", requests.post(U + "/d/hook/wh_wrong", json={"message": "x"}).status_code == 401)
    r = requests.post(U + h["path"], json={"message": "Bây giờ là mấy giờ?", "wait": True}, timeout=300)
    j = J(r); ok("webhook trigger (no basic auth, token) runs agent", r.status_code == 200 and j.get("status") == "ok" and j.get("reply"), (r.status_code, str(j)[:150]))
    ST["hook_trace"] = j.get("trace_id")
    rr = requests.post(U + h["path"], json={"message": "Mấy giờ?"}, timeout=60)
    ok("webhook async accept", rr.status_code == 200 and J(rr).get("accepted"), rr.text[:100])
def p4_studio():
    g = J(s.get(U + "/api/studio/6/graph")); names = [n["id"] for n in g.get("nodes", [])]
    ok("studio graph (nodes+edges)", "model" in names and "tools" in names and g.get("edges"), names)
    tid = f"studio-{TAG}"
    r = J(s.post(U + f"/api/studio/6/threads/{tid}/run", json={"message": "Bây giờ là mấy giờ?", "step": True}))
    dn = r.get("done", {})
    ok("step mode pauses before tools", dn.get("paused") and not any(e.get("kind") == "result" for e in r.get("events", [])), {k: dn.get(k) for k in ("paused", "reply", "error")})
    r2 = J(s.post(U + f"/api/studio/6/threads/{tid}/run", json={"message": None, "step": False}))
    d2 = r2.get("done", {})
    ok("continue from pause -> tool result + answer", not d2.get("paused") and any(e.get("kind") == "result" for e in r2.get("events", [])) and d2.get("reply"), (d2.get("reply") or d2.get("error") or "")[:100])
    h = J(s.get(U + f"/api/studio/6/threads/{tid}/history"))
    ok("checkpoint history", len(h) >= 4, len(h))
    first_user = next((c for c in reversed(h) if c["n_messages"] == 1), None)
    st = J(s.get(U + f"/api/studio/6/threads/{tid}/state"))
    ai = [m for m in st["messages"] if m["type"] == "ai" and not m.get("tool_calls")]
    e = J(s.post(U + f"/api/studio/6/threads/{tid}/state", json={"message_id": ai[-1]["id"], "content": "ĐÃ SỬA BỞI STUDIO"}))
    st2 = J(s.get(U + f"/api/studio/6/threads/{tid}/state"))
    ok("edit state -> new checkpoint with edited message", e.get("checkpoint_id") and st2["messages"][-1]["content"] == "ĐÃ SỬA BỞI STUDIO", st2["messages"][-1]["content"][:60])
    if first_user:
        f = J(s.post(U + f"/api/studio/6/threads/{tid}/state", json={"checkpoint_id": h[-1]["checkpoint_id"], "append_user": "Hôm nay là ngày bao nhiêu?"}))
        r3 = J(s.post(U + f"/api/studio/6/threads/{tid}/run", json={"message": None, "checkpoint_id": f["checkpoint_id"]}))
        d3 = r3.get("done", {})
        st3 = J(s.get(U + f"/api/studio/6/threads/{tid}/state"))
        ok("time-travel: fork from first checkpoint & re-run", d3.get("reply") and "ĐÃ SỬA BỞI STUDIO" not in json.dumps(st3["messages"], ensure_ascii=False), (d3.get("reply") or d3.get("error") or "")[:100])
    ST["studio_thread"] = tid

# ===================== gaps P1
def g_otlp():
    tid = "%032x" % random.getrandbits(128); s1 = "%016x" % random.getrandbits(64); s2 = "%016x" % random.getrandbits(64); s3 = "%016x" % random.getrandbits(64)
    now = time.time_ns(); A = lambda k, v: {"key": k, "value": {"stringValue": v} if isinstance(v, str) else {"intValue": str(v)}}
    body = {"resourceSpans": [{"resource": {"attributes": [A("service.name", "otel-demo")]}, "scopeSpans": [{"spans": [
        {"traceId": tid, "spanId": s1, "name": "otel_pipeline", "startTimeUnixNano": str(now), "endTimeUnixNano": str(now + 900_000_000), "attributes": [A("input.value", "câu hỏi OTLP"), A("session.id", f"otel-{TAG}")]},
        {"traceId": tid, "spanId": s2, "parentSpanId": s1, "name": "chat gemini", "startTimeUnixNano": str(now + 100_000_000), "endTimeUnixNano": str(now + 600_000_000),
         "attributes": [A("gen_ai.operation.name", "chat"), A("gen_ai.request.model", "gemini-3.1-flash-lite"), A("gen_ai.usage.input_tokens", 200), A("gen_ai.usage.output_tokens", 50), A("gen_ai.completion", "trả lời OTLP")]},
        {"traceId": tid, "spanId": s3, "parentSpanId": s1, "name": "tool db", "startTimeUnixNano": str(now + 600_000_000), "endTimeUnixNano": str(now + 800_000_000),
         "attributes": [A("gen_ai.operation.name", "execute_tool")], "status": {"code": 2, "message": "Timeout after 30s on id 4411"}}]}]}]}
    r = requests.post(U + "/ingest/otlp/v1/traces", json=body, headers={"X-API-Key": key})
    ok("OTLP/HTTP JSON ingest", r.status_code == 200 and J(r).get("accepted") == 3, r.text[:120])
    ok("OTLP malformed protobuf -> 400 (protobuf now supported, see p567)", requests.post(U + "/ingest/otlp/v1/traces", data=b"\xff\xff\xff", headers={"X-API-Key": key, "Content-Type": "application/x-protobuf"}).status_code == 400)
    import uuid as _u
    tr = J(s.get(U + f"/api/obs/traces/{_u.UUID(hex=tid)}"))
    runs = tr.get("runs", []); llm = [x for x in runs if x["run_type"] == "llm"]
    ok("OTLP spans mapped (tree, llm tokens/cost, tool error, thread)", len(runs) == 3 and llm and llm[0]["total_tokens"] == 250 and float(llm[0]["cost_usd"] or 0) > 0
       and any(x["run_type"] == "tool" and x["status"] == "error" for x in runs) and any(x["thread_id"] == f"otel-{TAG}" for x in runs), [(x["name"], x["run_type"], x["status"], x["total_tokens"]) for x in runs])
    ST["otel_trace"] = str(_u.UUID(hex=tid))
def g_redact_attach():
    @ao.traceable(name="redact_demo", tags=["t" + TAG])
    def f(msg): return "ok, mật khẩu đã nhận"
    fake = "AIza" + "B" * 35
    f(f"key của tôi là {fake} và password=abc123xyz")
    ao.flush(); time.sleep(2)
    rr = J(s.get(U + "/api/obs/runs", params={"project_id": P, "filter": f'and(has(tags,"t{TAG}"), eq(name,"redact_demo"))'}))
    full = J(s.get(U + f"/api/obs/traces/{rr[0]['trace_id']}"))["runs"][0] if rr else {}
    txt = json.dumps(full.get("inputs"), ensure_ascii=False)
    ok("server-side redaction (Google key + password)", fake not in txt and "abc123xyz" not in txt and "ĐÃ ẨN" in txt, txt[:200])
    ok("redaction test endpoint", "ĐÃ ẨN" in J(s.post(U + "/api/obs/redaction/test", json={"text": "Bearer abcdefghijklmnopqrstuvwxyz123"}))["output"])
    att = ao.attach("ghi_chu.txt", "nội dung đính kèm 123".encode(), "text/plain", run_id=rr[0]["id"])
    lst = J(s.get(U + f"/api/obs/traces/{rr[0]['trace_id']}/attachments"))
    dl = s.get(U + f"/api/obs/attachments/{att['id']}")
    ok("attachments upload (SDK) / list / download", lst and dl.content.decode() == "nội dung đính kèm 123", (len(lst), dl.status_code))
    ok("attachment without key -> 401", requests.post(U + "/ingest/attachments", data={"run_id": rr[0]["id"]}, files={"file": ("x.txt", b"x")}).status_code == 401)
    ST["redact_trace"] = rr[0]["trace_id"]
def g_filters_errors():
    sf = J(s.post(U + "/api/obs/saved_filters", json={"project_id": P, "name": f"lỗi {TAG}", "filter": 'eq(status,"error")'}))
    ok("saved filter create/list", any(x["id"] == sf["id"] for x in J(s.get(U + "/api/obs/saved_filters", params={"project_id": P}))))
    ok("saved filter invalid -> 400", s.post(U + "/api/obs/saved_filters", json={"project_id": P, "name": "x", "filter": "eq(status;x)"}).status_code == 400)
    @ao.traceable(name="flaky_db", tags=["t" + TAG])
    def flaky(i): raise TimeoutError(f"Timeout after {30 + i}s on id {1000 + i * 7}")
    for i in range(4):
        try: flaky(i)
        except TimeoutError: pass
    ao.flush(); time.sleep(2)
    cl = J(s.get(U + f"/api/obs/projects/{P}/errors", params={"hours": 1}))["clusters"]
    tm = next((x for x in cl if x["signature"].startswith("Timeout after <n>s on id <n>")), None)
    ok("error clustering groups variants (>=5 incl. OTLP)", tm and tm["count"] >= 5, [(x["signature"][:50], x["count"]) for x in cl[:4]])

# ===================== gaps P2
def g_summary_traj():
    ds = J(s.post(U + "/api/datasets", json={"name": f"traj-{TAG}"}))["id"]; ST["ds_traj"] = ds
    s.post(U + f"/api/datasets/{ds}/examples", json=[{"inputs": {"question": "Bây giờ là mấy giờ?"}, "outputs": {"answer": "giờ hiện tại", "trajectory": ["llm__GetDateTime"]}},
                                                      {"inputs": {"question": "Hôm nay là thứ mấy?"}, "outputs": {"answer": "thứ trong tuần", "trajectory": ["llm__GetDateTime"]}}])
    e = J(s.post(U + "/api/experiments", json={"name": f"traj {TAG}", "dataset_id": ds, "target": {"type": "agent", "agent_id": 6},
                                                "evaluator_ids": [evs["co_chu_so"], evs["quy_dao_tool"], evs["quy_dao_hop_ly"], evs["ty_le_dat"]], "concurrency": 1}))["id"]
    ST["exp_traj"] = e
    x = wait(lambda: next((x for x in J(s.get(U + "/api/experiments", params={"dataset_id": ds})) if x["id"] == e and x["status"] not in ("queued", "running")), None), 300, 10)
    sc = {y["key"]: y["avg"] for y in (x or {}).get("summary", {}).get("scores", [])}
    ok("trajectory evaluator (code) = 1.0", sc.get("quy_dao") == 1.0, sc)
    ok("trajectory LLM judge scored", sc.get("quy_dao_hop_ly") is not None, sc)
    ss = (x or {}).get("summary", {}).get("summary_scores") or {}
    ok("summary evaluator (experiment-level ty_le_dat/ty_le_loi)", "ty_le_dat" in ss and "ty_le_loi" in ss, ss)
    r = J(s.post(U + f"/api/experiments/{e}/evaluate", json={"evaluator_ids": [evs["khop_tu_khoa"]]}))
    ok("add evaluator to existing experiment", "khop_tu_khoa" in r, r)
def g_sim():
    r = J(s.post(U + "/api/simulations", json={"agent_id": 6, "persona": "Người dùng lớn tuổi, hỏi ngắn", "goal": "Biết giờ hiện tại rồi hỏi hôm nay thứ mấy", "max_turns": 2,
                                                "evaluator_id": evs["hoi_thoai_dat_muc_tieu"]}))
    x = wait(lambda: (lambda y: y if y["status"] != "running" else None)(J(s.get(U + f"/api/simulations/{r['id']}"))), 400, 10)
    turns = [t for t in (x or {}).get("transcript", []) if t["role"] == "user"]
    ok("multi-turn simulation (LLM user x agent)", x and x["status"] == "done" and len(turns) >= 1 and all(t.get("text") for t in x["transcript"]), x and (x["status"], len(turns)))
    ok("multi-turn evaluator scored whole thread", x and x.get("result") and x["result"][0].get("score") is not None, x and x.get("result"))
    th = J(s.get(U + f"/api/obs/projects/{PA}/threads"))
    ok("simulation thread visible in Threads", any(t["thread_id"] == r["thread_id"] for t in th))
    ST["sim"] = r["id"]; ST["sim_thread"] = r["thread_id"]
def g_pytest():
    env = {**os.environ, "AGENTOS_ENDPOINT": U, "AGENTOS_API_KEY": key, "AGENTOS_DATASET": f"pytest-{TAG}", "AGENTOS_EXPERIMENT": f"pytest {TAG}",
           "PYTHONPATH": "/workspace/adk_ha_build/os/sdk"}
    p = subprocess.run(["/workspace/.pwvenv/bin/python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "/workspace/adk_ha_build/os/tests/pytest_demo"], env=env, capture_output=True, text=True, timeout=180)
    out = p.stdout[-600:]; print(out)
    ds = next((d for d in J(s.get(U + "/api/datasets")) if d["name"] == f"pytest-{TAG}"), None)
    xs = J(s.get(U + "/api/experiments", params={"dataset_id": ds["id"]})) if ds else []
    sc = {y["key"]: y for y in xs[0]["summary"]["scores"]} if xs else {}
    ok("pytest plugin -> experiment (3 tests, 1 fail)", xs and xs[0]["summary"]["n"] == 3 and sc.get("pass", {}).get("n") == 3 and abs(float(sc["pass"]["avg"]) - 0.667) < 0.01 and "[agentos] experiment" in p.stdout,
       (ds and ds["id"], sc))
    ok("pytest dataset reused (no dup examples on re-run)", True)
    subprocess.run(["/workspace/.pwvenv/bin/python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "/workspace/adk_ha_build/os/tests/pytest_demo"], env=env, capture_output=True, text=True, timeout=180)
    d2 = J(s.get(U + f"/api/datasets/{ds['id']}")) if ds else {}
    R["pytest dataset reused (no dup examples on re-run)"] = len(d2.get("examples", [])) == 3
    print("PASS" if len(d2.get("examples", [])) == 3 else "FAIL", "pytest re-run dedupe | examples =", len(d2.get("examples", [])))

for name, fn in [("p3_hub", p3_hub), ("p3_improve", p3_improve), ("p3_multi", p3_multi), ("p3_batch", p3_batch), ("p3_agent_link", p3_agent_link),
                 ("p4_builder", p4_builder), ("p4_hitl", p4_hitl), ("p4_cron", p4_cron), ("p4_hook", p4_hook), ("p4_studio", p4_studio),
                 ("g_otlp", g_otlp), ("g_redact_attach", g_redact_attach), ("g_filters_errors", g_filters_errors), ("g_summary_traj", g_summary_traj),
                 ("g_sim", g_sim), ("g_pytest", g_pytest)]:
    print(f"--- {name}", flush=True); step(name, fn)
for p in ["/hub", "/builder", "/approvals", "/studio", "/simulations", "/projects", "/rules", "/evaluators", "/experiments"]:
    ok(f"page {p}", requests.get(U + p).status_code == 401 and s.get(U + p).status_code == 200)
w = J(s.get(U + "/api/obs/worker")); ok("worker healthy", not w.get("errors"), w)
json.dump(ST, open("/workspace/adk_ha_build/os/tests/p34_state.json", "w"))
print(f"\nTOTAL {sum(R.values())}/{len(R)}")
