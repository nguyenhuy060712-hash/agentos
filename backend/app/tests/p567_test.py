"""Test P5 (Agent Server) + P6 (Gateway) + P7 (Admin) + gap (OTLP protobuf, gom cụm lỗi ngữ nghĩa, attachment cho example)."""
import base64, gzip, io, json, os, re, sys, time, uuid, threading
import requests
c = dict(l.split("=", 1) for l in open("/workspace/secrets/deepagent_web.txt").read().split())
U = c["url"].rstrip("/"); PW = c["password"]
s = requests.Session(); s.auth = ("moza", PW)
R = {}; TAG = str(int(time.time())); ST = {"tag": TAG}
def ok(n, cond, extra=""): R[n] = bool(cond); print(("PASS " if cond else "FAIL ") + n, "|", str(extra)[:260], flush=True)
def J(r):
    try: return r.json()
    except Exception: return {"_status": r.status_code, "_text": r.text[:300]}
def wait(fn, t=120, step=3):
    t0 = time.time()
    while time.time() - t0 < t:
        v = fn()
        if v: return v
        time.sleep(step)
    return None
def step(name, fn):
    print(f"\n== {name}", flush=True)
    try: fn()
    except Exception as e:
        import traceback; traceback.print_exc(); ok(f"{name} (exception)", False, repr(e)[:200])

# ======================================================= P7
def p7_sweep():
    spec = J(s.get(U + "/openapi.json"))
    paths = spec.get("paths", {})
    ok("openapi readable when logged in", len(paths) > 100, len(paths))
    anon = requests.Session(); leaks = []; n = 0
    pub = ("/d/", "/ingest/", "/share/", "/v1/", "/login", "/api/auth/login")
    for p, ops in paths.items():
        if p.startswith(pub): continue
        url = U + re.sub(r"\{[^}]+\}", "1", p)
        for m in ops:
            if m not in ("get", "post", "put", "patch", "delete"): continue
            n += 1
            r = anon.request(m.upper(), url, json={}, allow_redirects=False, timeout=30)
            if r.status_code not in (401, 303): leaks.append(f"{m.upper()} {p} -> {r.status_code}")
    ok(f"anonymous sweep: every non-public route denied ({n} route-methods)", not leaks, leaks[:10])
    r = anon.get(U + "/agents", headers={"Accept": "text/html"}, allow_redirects=False)
    ok("anonymous page -> redirect to /login", r.status_code == 303 and "/login" in r.headers.get("location", ""), r.headers.get("location"))
    ok("public /d/ still reachable (own key check)", anon.post(U + "/d/khong-co/invoke", json={"message": "x"}).status_code in (401, 404))
    ok("public /v1 requires virtual key", anon.get(U + "/v1/models").status_code == 401)

def login(user, pw):
    x = requests.Session(); r = x.post(U + "/api/auth/login", json={"username": user, "password": pw}); return x, r

def p7_users():
    x, r = login("moza", PW)
    ok("owner 'moza' logs in with current password (cookie session)", r.status_code == 200 and "aos_s" in x.cookies, r.status_code)
    ck = r.headers.get("set-cookie", "")
    ok("session cookie HttpOnly + Secure + SameSite", "HttpOnly" in ck and "Secure" in ck and "samesite=lax" in ck.lower(), ck[:120])
    me = J(x.get(U + "/api/auth/me")); ok("me = admin superadmin", me.get("role") == "admin" and me.get("username") == "moza", me)
    ok("wrong password 401", login("moza", "sai-mat-khau")[1].status_code == 401)
    codes = [login(f"khongco{TAG}", "x")[1].status_code for _ in range(21)]
    ok("login rate limit -> 429 (per IP ≤10, per account ≤20 / 15 min)", codes[-1] == 429 and 429 in codes, codes)
    pw = "Test-" + uuid.uuid4().hex[:12]; ST["pw"] = pw
    for role in ("editor", "viewer"):
        r = s.post(U + "/api/admin/users", json={"username": f"{role}{TAG}", "password": pw, "role": role}); ST[role] = J(r).get("id")
    ok("admin creates editor + viewer", ST.get("editor") and ST.get("viewer"), (ST.get("editor"), ST.get("viewer")))
    ed, _ = login(f"editor{TAG}", pw); vw, _ = login(f"viewer{TAG}", pw); ST["ed"], ST["vw"] = ed, vw
    ok("viewer can read", vw.get(U + "/api/agents").status_code == 200)
    ok("viewer cannot create agent (403)", vw.post(U + "/api/agents", json={"name": "x"}).status_code == 403)
    ok("viewer cannot run chat (403)", vw.post(U + "/chat", json={"thread_id": "x", "message": "hi"}).status_code == 403)
    r = ed.post(U + "/api/agents", json={"name": f"ed-agent {TAG}", "model": "gemini-flash-lite-latest", "tools": [], "planning": False, "filesystem": False})
    ok("editor can create agent", r.status_code == 200, J(r)); ST["ed_agent"] = J(r).get("id")
    ok("editor cannot manage users (403)", ed.get(U + "/api/admin/users").status_code == 403)
    ok("editor cannot change Gemini key (403)", ed.post(U + "/api/gateway/key", json={"api_key": "x" * 30}).status_code == 403)
    ok("editor cannot create gateway virtual key (403)", ed.post(U + "/api/gw/keys", json={"name": "x"}).status_code == 403)
    ok("cannot delete owner", s.delete(U + "/api/admin/users/1").status_code == 400)
    ok("owner cannot disable self", s.patch(U + "/api/admin/users/1", json={"disabled": True}).status_code == 400)

def p7_keys():
    ed = ST["ed"]
    r = J(ed.post(U + "/api/auth/keys", json={"name": "ci", "role": "viewer"})); k = r.get("key", "")
    ok("editor creates API key (shown once, aos_ prefix)", k.startswith("aos_"), r.get("note"))
    ok("cannot create key above own role", ed.post(U + "/api/auth/keys", json={"role": "admin"}).status_code == 403)
    h = {"X-Api-Key": k}
    ok("API key (downgraded to viewer) can GET", requests.get(U + "/api/agents", headers=h).status_code == 200)
    ok("API key (viewer) blocked on POST", requests.post(U + "/api/agents", headers=h, json={"name": "x"}).status_code == 403)
    r2 = J(ed.post(U + "/api/auth/keys", json={"name": "srv"})); ST["ed_key"] = r2["key"]
    ok("Bearer aos_ also accepted", requests.get(U + "/api/auth/me", headers={"Authorization": "Bearer " + r2["key"]}).status_code == 200)
    ed.delete(U + f"/api/auth/keys/{r['id']}")
    ok("revoked key -> 401", requests.get(U + "/api/agents", headers=h).status_code == 401)

def p7_ws():
    w = J(s.post(U + "/api/admin/workspaces", json={"name": f"WS test {TAG}"})); ST["ws2"] = w["id"]
    s.post(U + f"/api/admin/workspaces/{w['id']}/members", json={"user_id": ST["editor"], "role": "admin"})
    ed = ST["ed"]
    ok("editor switches to workspace 2", ed.post(U + "/api/auth/workspace", json={"workspace_id": w["id"]}).status_code == 200)
    me = J(ed.get(U + "/api/auth/me")); ok("role is per-workspace (admin in ws2)", me.get("role") == "admin" and me.get("ws") == w["id"], me)
    ok("ws2 starts with no agents", J(ed.get(U + "/api/agents")) == [])
    a = J(ed.post(U + "/api/agents", json={"name": f"ws2 agent {TAG}", "model": "gemini-flash-lite-latest", "tools": [], "planning": False, "filesystem": False}))
    ok("agent created inside ws2", a.get("id"))
    ok("owner in ws1 does not see ws2 agent", all(x["id"] != a.get("id") for x in J(s.get(U + "/api/agents"))))
    ok("ws1 agent not readable from ws2 (404)", ed.get(U + "/api/agents/1").status_code == 404)
    ed.post(U + "/api/auth/workspace", json={"workspace_id": 1})
    ok("viewer cannot switch to a workspace it is not in", ST["vw"].post(U + "/api/auth/workspace", json={"workspace_id": w["id"]}).status_code == 403)

def p7_audit():
    a = J(s.get(U + f"/api/admin/audit?user=editor{TAG}&limit=100"))
    paths = {(x["method"], x["path"], x["status"]) for x in a}
    ok("audit: editor actions recorded (agent create 200, admin 403)", any(p[1] == "/api/agents" and p[2] == 200 for p in paths) and any(p[2] == 403 for p in paths), list(paths)[:6])
    ok("audit: logins recorded", any(x["path"] == "/api/auth/login" for x in J(s.get(U + "/api/admin/audit?path=/api/auth/login&limit=50"))))
    s.patch(U + f"/api/admin/users/{ST['viewer']}", json={"disabled": True})
    ok("disabled user's session revoked (401)", ST["vw"].get(U + "/api/agents").status_code == 401)

def p7_sandbox():
    sb = J(s.post(U + "/api/sandboxes", json={"name": f"sbx {TAG}", "ttl_hours": 2})); sid = sb["id"]; ST["sbx"] = sid
    r1 = J(s.post(U + f"/api/sandboxes/{sid}/exec", json={"code": "open('dem.txt','a').write('x');print(len(open('dem.txt').read()))"}))
    r2 = J(s.post(U + f"/api/sandboxes/{sid}/exec", json={"code": "open('dem.txt','a').write('x');print(len(open('dem.txt').read()))"}))
    ok("sandbox state persists across runs (1 -> 2)", r1.get("stdout", "").strip() == "1" and r2.get("stdout", "").strip() == "2", (r1.get("stdout"), r2.get("stdout"), r1.get("stderr", "")[:100]))
    up = s.post(U + f"/api/sandboxes/{sid}/files", files={"file": ("du_lieu.csv", b"a,b\n1,2\n3,4\n")})
    r3 = J(s.post(U + f"/api/sandboxes/{sid}/exec", json={"code": "import csv;rows=list(csv.DictReader(open('du_lieu.csv')));open('tong.txt','w').write(str(sum(int(r['b']) for r in rows)));print('ok')"}))
    d = s.get(U + f"/api/sandboxes/{sid}/files/tong.txt")
    ok("upload -> code reads it -> download result", up.status_code == 200 and d.status_code == 200 and d.content == b"6", (up.status_code, d.status_code, d.content[:20], r3.get("stderr", "")[:100]))
    ok("path traversal blocked", s.get(U + f"/api/sandboxes/{sid}/files/..%2F..%2Fetc%2Fpasswd").status_code in (400, 404))
    r4 = J(s.post(U + f"/api/sandboxes/{sid}/exec", json={"code": "import socket\ntry:\n socket.create_connection(('1.1.1.1',80),3);print('NET')\nexcept Exception as e: print('NONET')"}))
    ok("sandbox has no network", "NONET" in r4.get("stdout", ""), r4.get("stdout"))
    r5 = J(s.post(U + f"/api/sandboxes/{sid}/exec", json={"code": "open('/etc/x','w')"}))
    ok("sandbox root fs read-only", r5.get("exit") not in (0, None), r5.get("stderr", "")[-80:])
    fl = J(s.get(U + f"/api/sandboxes/{sid}/files")); ok("file list", {f["path"] for f in fl} >= {"dem.txt", "du_lieu.csv", "tong.txt"}, fl)

# ======================================================= P6
def p6_gateway():
    from openai import OpenAI
    k = J(s.post(U + "/api/gw/keys", json={"name": f"test {TAG}", "models": ["gemini-flash-lite-latest", "gemini-3.6-flash", "gemini-3.1-flash-lite"],
                                            "fallbacks": ["gemini-flash-lite-latest"], "budget_usd_month": 5, "pii_mode": "mask", "cache_ttl_s": 600}))
    key = k["key"]; ST["vk"] = key; H = {"Authorization": "Bearer " + key}
    ok("virtual key created (vk-, shown once)", key.startswith("vk-"))
    ok("/v1/models lists allowed models", [m["id"] for m in J(requests.get(U + "/v1/models", headers=H))["data"]] == ["gemini-flash-lite-latest", "gemini-3.6-flash", "gemini-3.1-flash-lite"])
    cl = OpenAI(base_url=U + "/v1", api_key=key)
    q = f"Email của tôi là an.nguyen{TAG}@vidu.vn và SĐT 0912345678. Nhắc lại chính xác email và số điện thoại tôi vừa đưa."
    r = cl.chat.completions.create(model="gemini-flash-lite-latest", messages=[{"role": "system", "content": "Trả lời ngắn."}, {"role": "user", "content": q}], temperature=0)
    txt = r.choices[0].message.content or ""
    ok("OpenAI SDK chat.completions works", r.choices[0].finish_reason == "stop" and txt, txt[:120])
    ok("PII masked before reaching model (reply has no raw email/phone)", f"an.nguyen{TAG}@vidu.vn" not in txt and "0912345678" not in txt, txt[:150])
    ok("usage tokens returned", r.usage.total_tokens > 0, r.usage)
    t0 = time.time(); raw = requests.post(U + "/v1/chat/completions", headers=H, json={"model": "gemini-flash-lite-latest", "temperature": 0, "messages": [{"role": "system", "content": "Trả lời ngắn."}, {"role": "user", "content": q}]})
    ok("identical request -> cache hit", raw.headers.get("x-agentos-cache") == "hit" and raw.json()["choices"][0]["message"]["content"] == txt, (raw.headers.get("x-agentos-cache"), round(time.time() - t0, 2)))
    chunks = list(cl.chat.completions.create(model="gemini-flash-lite-latest", messages=[{"role": "user", "content": "Đếm từ 1 đến 5, cách nhau dấu phẩy."}], stream=True))
    st = "".join((ch.choices[0].delta.content or "") for ch in chunks if ch.choices)
    ok("streaming SSE (OpenAI SDK)", len(chunks) >= 2 and "3" in st and chunks[-1].choices[0].finish_reason == "stop", (len(chunks), st[:60]))
    tools = [{"type": "function", "function": {"name": "lay_thoi_tiet", "description": "Lấy thời tiết theo thành phố", "parameters": {"type": "object", "properties": {"thanh_pho": {"type": "string"}}, "required": ["thanh_pho"]}}}]
    m = [{"role": "user", "content": "Thời tiết Hà Nội thế nào? Dùng tool."}]
    r = cl.chat.completions.create(model="gemini-flash-lite-latest", messages=m, tools=tools)
    tc = (r.choices[0].message.tool_calls or [None])[0]
    ok("tool calling -> tool_calls returned", tc and tc.function.name == "lay_thoi_tiet" and "Hà Nội" in tc.function.arguments, r.choices[0])
    if tc:
        m += [r.choices[0].message.model_dump(exclude_none=True), {"role": "tool", "tool_call_id": tc.id, "content": json.dumps({"nhiet_do": "31°C", "troi": "nắng"})}]
        r2 = cl.chat.completions.create(model="gemini-flash-lite-latest", messages=m, tools=tools)
        ok("tool result round-trip -> final answer uses it", "31" in (r2.choices[0].message.content or ""), r2.choices[0].message.content)
    r = cl.chat.completions.create(model="gemini-flash-lite-latest", messages=[{"role": "user", "content": "Trả JSON {\"so\": 7}"}], response_format={"type": "json_object"})
    ok("JSON mode", json.loads(r.choices[0].message.content).get("so") == 7, r.choices[0].message.content)
    e = cl.embeddings.create(model="text-embedding", input=["xin chào", "tạm biệt"])
    ok("embeddings endpoint", len(e.data) == 2 and len(e.data[0].embedding) == 768)
    ok("model not in allow-list -> 403", requests.post(U + "/v1/chat/completions", headers=H, json={"model": "gemini-3-flash-preview", "messages": [{"role": "user", "content": "hi"}]}).status_code == 403)
    ok("non-Gemini model -> 400", requests.post(U + "/v1/chat/completions", headers=H, json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}).status_code == 400)
    ok("bad key -> 401", requests.post(U + "/v1/chat/completions", headers={"Authorization": "Bearer vk-sai"}, json={"model": "gemini-flash-lite-latest", "messages": []}).status_code == 401)
    r = requests.post(U + "/v1/chat/completions", headers=H, json={"model": "gemini-3.6-flash", "messages": [{"role": "user", "content": "Nói 'xin chào'."}]})
    ok("fallback chain: request served (quota-exhausted primary falls back)", r.status_code == 200, (r.status_code, r.headers.get("x-agentos-model")))
    ST["fallback_model"] = r.headers.get("x-agentos-model")
    kb = J(s.post(U + "/api/gw/keys", json={"name": f"block {TAG}", "pii_mode": "block", "rpm": 1}))["key"]
    rb = requests.post(U + "/v1/chat/completions", headers={"Authorization": "Bearer " + kb}, json={"model": "gemini-flash-lite-latest", "messages": [{"role": "user", "content": "thẻ 4111 1111 1111 1111"}]})
    ok("PII block mode -> 400 guardrail (credit card, Luhn)", rb.status_code == 400 and "CARD" in rb.text, rb.text[:150])
    requests.post(U + "/v1/chat/completions", headers={"Authorization": "Bearer " + kb}, json={"model": "gemini-flash-lite-latest", "messages": [{"role": "user", "content": "hi"}]})
    ok("RPM limit -> 429", requests.post(U + "/v1/chat/completions", headers={"Authorization": "Bearer " + kb}, json={"model": "gemini-flash-lite-latest", "messages": [{"role": "user", "content": "hi"}]}).status_code == 429)
    k0 = J(s.post(U + "/api/gw/keys", json={"name": f"budget0 {TAG}", "budget_usd_month": 0}))["key"]
    ok("budget exhausted -> 402", requests.post(U + "/v1/chat/completions", headers={"Authorization": "Bearer " + k0}, json={"model": "gemini-flash-lite-latest", "messages": [{"role": "user", "content": "hi"}]}).status_code == 402)
    ks = {x["name"]: x for x in J(s.get(U + "/api/gw/keys"))}
    me = ks.get(f"test {TAG}", {})
    ok("per-key spend + cache hits tracked", float(me.get("spent_month") or 0) > 0 and me.get("cache_hits_24h", 0) >= 1, {k: me.get(k) for k in ("spent_month", "calls_24h", "cache_hits_24h")})
    pid = next(p["id"] for p in J(s.get(U + "/api/obs/projects")) if p["name"] == "gateway")
    runs = J(s.get(U + f"/api/obs/runs?project_id={pid}&limit=30"))
    rows = runs if isinstance(runs, list) else runs.get("runs", [])
    blob = json.dumps(rows, ensure_ascii=False)
    ok("gateway calls traced to project 'gateway' with PII already masked", len(rows) >= 3 and f"an.nguyen{TAG}" not in blob and "[EMAIL]" in blob, len(rows))
    ST["gw_pid"] = pid

# ======================================================= P5
def p5_setup():
    a = J(s.post(U + "/api/agents", json={"name": f"Server test {TAG}", "model": "gemini-flash-lite-latest", "fallback_models": ["gemini-3.1-flash-lite"],
                                          "system_prompt": "Trả lời tiếng Việt, rất ngắn (1 câu).", "tools": [], "planning": False, "filesystem": False, "rpm_limit": 60}))
    h = J(s.post(U + "/api/agents", json={"name": f"Server HITL {TAG}", "model": "gemini-flash-lite-latest", "fallback_models": ["gemini-3.1-flash-lite"],
                                          "system_prompt": "Luôn dùng chay_code_python để tính toán rồi trả lời ngắn.", "tools": [], "sandbox": True,
                                          "interrupt_on": ["local:chay_code_python"], "planning": False, "filesystem": False, "rpm_limit": 60}))
    ST["srv_agent"], ST["hitl"] = a["id"], h["id"]
    lst = J(s.post(U + "/api/v1/assistants/search", json={"graph_id": f"agent-{a['id']}"}))
    ok("system assistant auto-created per agent", len(lst) == 1 and lst[0]["metadata"].get("created_by") == "system", lst)
    ST["sys_asst"] = lst[0]["assistant_id"]
    x = J(s.post(U + "/api/v1/assistants", json={"graph_id": f"agent-{a['id']}", "name": "Trợ lý server", "metadata": {"team": "qa"}}))
    p = J(s.patch(U + f"/api/v1/assistants/{x['assistant_id']}", json={"metadata": {"env": "test"}}))
    vs = J(s.post(U + f"/api/v1/assistants/{x['assistant_id']}/versions"))
    ok("assistant create/patch -> versions [2,1]", p.get("version") == 2 and [v["version"] for v in vs] == [2, 1] and p["metadata"] == {"team": "qa", "env": "test"}, p.get("metadata"))
    l2 = J(s.post(U + f"/api/v1/assistants/{x['assistant_id']}/latest", json={"version": 1}))
    ok("assistant set latest version (rollback to v1)", l2.get("version") == 1 and l2["metadata"] == {"team": "qa"})
    ok("assistant metadata search", any(y["assistant_id"] == x["assistant_id"] for y in J(s.post(U + "/api/v1/assistants/search", json={"metadata": {"team": "qa"}}))))
    g = J(s.get(U + f"/api/v1/assistants/{x['assistant_id']}/graph")); ok("assistant graph", "model" in [n["id"] for n in g.get("nodes", [])])
    ST["asst"] = x["assistant_id"]

def msgs_text(vals): return " ".join(m.get("content") or "" for m in (vals or {}).get("messages", []) if m.get("type") == "ai")

def p5_threads_runs():
    A = ST["asst"]
    t = J(s.post(U + "/api/v1/threads", json={"metadata": {"user": "qa", "tag": TAG}})); tid = t["thread_id"]; ST["tid"] = tid
    ok("create thread with metadata", t.get("status") == "idle")
    v = J(s.post(U + f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": A, "input": {"messages": [{"role": "user", "content": "Thủ đô của Pháp là gì?"}]}}, timeout=300))
    ok("runs/wait returns final state with AI reply", "Paris" in msgs_text(v), msgs_text(v)[:120])
    st = J(s.get(U + f"/api/v1/threads/{tid}/state")); ok("thread state (2 messages, next=[])", len(st["values"]["messages"]) == 2 and st["next"] == [], len(st["values"]["messages"]))
    h = J(s.post(U + f"/api/v1/threads/{tid}/history", json={"limit": 20})); ok("thread history (checkpoints)", len(h) >= 2, len(h))
    ok("thread search by metadata", any(x["thread_id"] == tid for x in J(s.post(U + "/api/v1/threads/search", json={"metadata": {"tag": TAG}}))))
    r = s.post(U + f"/api/v1/threads/{tid}/runs/stream", json={"assistant_id": A, "input": {"messages": [{"role": "user", "content": "Còn của Ý?"}]}, "stream_mode": ["updates", "values"]}, stream=True, timeout=300)
    evs = [l.decode()[7:] for l in r.iter_lines() if l.startswith(b"event: ")]
    ok("SSE stream: metadata ... values ... end (multi-turn memory)", evs[0] == "metadata" and "values" in evs and evs[-1] == "end", evs)
    st2 = J(s.get(U + f"/api/v1/threads/{tid}/state")); ok("multi-turn answer uses thread memory (Rome)", "Rom" in msgs_text(st2["values"]), msgs_text(st2["values"])[-80:])
    runs = J(s.get(U + f"/api/v1/threads/{tid}/runs")); ok("runs listed, all success, traced", len(runs) == 2 and all(x["status"] == "success" and x["trace_id"] for x in runs), [(x["status"], x["trace_id"]) for x in runs])
    ST["trace"] = runs[0]["trace_id"]
    rid = runs[0]["run_id"]; rs = s.get(U + f"/api/v1/threads/{tid}/runs/{rid}/stream", stream=True, timeout=30)
    ok("join stream replays a finished run (Last-Event-ID capable)", [l for l in rs.iter_lines() if l.startswith(b"event: end")])
    cp = J(s.post(U + f"/api/v1/threads/{tid}/copy")); cps = J(s.get(U + f"/api/v1/threads/{cp['thread_id']}/state"))
    ok("thread copy keeps state", len(cps["values"]["messages"]) == len(st2["values"]["messages"]), len(cps["values"]["messages"]))
    up = J(s.post(U + f"/api/v1/threads/{tid}/state", json={"values": {"messages": [{"role": "user", "content": "(ghi chú thêm)"}]}}))
    ok("update state -> new checkpoint", up.get("checkpoint", {}).get("checkpoint_id"))
    s.delete(U + f"/api/v1/threads/{cp['thread_id']}")
    ok("delete thread", s.get(U + f"/api/v1/threads/{cp['thread_id']}").status_code == 404)

def p5_bg_and_dt():
    A = ST["asst"]
    t = J(s.post(U + "/api/v1/threads", json={}))["thread_id"]
    b = {"assistant_id": A, "input": {"messages": [{"role": "user", "content": "Viết 5 câu về biển."}]}}
    r1 = J(s.post(U + f"/api/v1/threads/{t}/runs", json=b))
    ok("background run created (pending)", r1.get("status") in ("pending", "running"), r1.get("status"))
    rj = s.post(U + f"/api/v1/threads/{t}/runs", json={**b, "multitask_strategy": "reject"})
    ok("double-texting reject -> 409 while busy", rj.status_code == 409, rj.status_code)
    j = J(s.get(U + f"/api/v1/threads/{t}/runs/{r1['run_id']}/join", timeout=300)); ok("join waits for background run", msgs_text(j), msgs_text(j)[:60])
    # enqueue: 2 runs nối nhau
    e1 = J(s.post(U + f"/api/v1/threads/{t}/runs", json={**b, "input": {"messages": [{"role": "user", "content": "Số 1 + 1 = ?"}]}}))
    e2 = J(s.post(U + f"/api/v1/threads/{t}/runs", json={**b, "input": {"messages": [{"role": "user", "content": "Nhân đôi kết quả vừa rồi."}]}, "multitask_strategy": "enqueue"}))
    s.get(U + f"/api/v1/threads/{t}/runs/{e2['run_id']}/join", timeout=300)
    a1, a2 = J(s.get(U + f"/api/v1/threads/{t}/runs/{e1['run_id']}")), J(s.get(U + f"/api/v1/threads/{t}/runs/{e2['run_id']}"))
    ok("double-texting enqueue: both succeed, strictly sequential", a1["status"] == a2["status"] == "success" and a2["started_at"] >= a1["ended_at"], (a1["status"], a2["status"]))
    # interrupt
    i1 = J(s.post(U + f"/api/v1/threads/{t}/runs", json={**b, "input": {"messages": [{"role": "user", "content": "Viết một bài thơ dài 20 câu về núi."}]}}))
    wait(lambda: J(s.get(U + f"/api/v1/threads/{t}/runs/{i1['run_id']}")).get("status") == "running", 30, 0.5)
    i2 = J(s.post(U + f"/api/v1/threads/{t}/runs", json={**b, "input": {"messages": [{"role": "user", "content": "Thôi, chỉ nói 'ok'."}]}, "multitask_strategy": "interrupt"}))
    s.get(U + f"/api/v1/threads/{t}/runs/{i2['run_id']}/join", timeout=300)
    x1, x2 = J(s.get(U + f"/api/v1/threads/{t}/runs/{i1['run_id']}")), J(s.get(U + f"/api/v1/threads/{t}/runs/{i2['run_id']}"))
    ok("double-texting interrupt: first interrupted, second success", x1["status"] == "interrupted" and x2["status"] == "success", (x1["status"], x2["status"]))
    # rollback
    n_before = len(J(s.get(U + f"/api/v1/threads/{t}/state"))["values"]["messages"])
    k1 = J(s.post(U + f"/api/v1/threads/{t}/runs", json={**b, "input": {"messages": [{"role": "user", "content": "Viết một bài thơ dài 20 câu về sông."}]}}))
    wait(lambda: J(s.get(U + f"/api/v1/threads/{t}/runs/{k1['run_id']}")).get("status") == "running", 30, 0.5)
    k2 = J(s.post(U + f"/api/v1/threads/{t}/runs", json={**b, "input": {"messages": [{"role": "user", "content": "Chỉ nói 'đã rollback'."}]}, "multitask_strategy": "rollback"}))
    s.get(U + f"/api/v1/threads/{t}/runs/{k2['run_id']}/join", timeout=300)
    st = J(s.get(U + f"/api/v1/threads/{t}/state"))["values"]["messages"]
    ok("double-texting rollback: first run deleted, its input gone from state", s.get(U + f"/api/v1/threads/{t}/runs/{k1['run_id']}").status_code == 404
       and not any("về sông" in (m.get("content") or "") for m in st) and len(st) == n_before + 2, (len(st), n_before))
    # cancel
    c1 = J(s.post(U + f"/api/v1/threads/{t}/runs", json={**b, "after_seconds": 30}))
    ok("cancel pending (delayed) run", J(s.post(U + f"/api/v1/threads/{t}/runs/{c1['run_id']}/cancel")).get("ok") and J(s.get(U + f"/api/v1/threads/{t}/runs/{c1['run_id']}"))["status"] == "interrupted")
    ST["dt_thread"] = t

def p5_stateless_webhook_hitl():
    A = ST["asst"]
    before = {x["thread_id"] for x in J(s.post(U + "/api/v1/threads/search", json={"limit": 1000}))}
    v = J(s.post(U + "/api/v1/runs/wait", json={"assistant_id": A, "input": {"messages": [{"role": "user", "content": "2+3 bằng mấy?"}]}}, timeout=300))
    time.sleep(1)
    after = {x["thread_id"] for x in J(s.post(U + "/api/v1/threads/search", json={"limit": 1000}))}
    ok("stateless run returns result, temp thread cleaned up", "5" in msgs_text(v) and not [x for x in after - before if x.startswith("tmp-")], msgs_text(v)[:60])
    r = s.post(U + "/api/v1/runs/stream", json={"assistant_id": ST["sys_asst"], "input": {"messages": [{"role": "user", "content": "Chào"}]}}, stream=True, timeout=300)
    ok("stateless stream", any(l == b"event: end" for l in r.iter_lines()))
    t = J(s.post(U + "/api/v1/threads", json={}))["thread_id"]
    hook = f"https://example.com/agentos-hook-{TAG}"
    J(s.post(U + f"/api/v1/threads/{t}/runs/wait", json={"assistant_id": A, "input": {"messages": [{"role": "user", "content": "hi"}]}, "webhook": hook}, timeout=300))
    ob = wait(lambda: [o for o in J(s.get(U + "/api/obs/outbox")) if o["target"] == hook], 20, 1)
    ok("webhook to external host -> outbox DRAFT (not sent)", ob and ob[0]["status"] == "draft", ob and ob[0]["status"])
    # HITL qua server
    th = J(s.post(U + "/api/v1/threads", json={}))["thread_id"]
    ha = J(s.post(U + "/api/v1/assistants/search", json={"graph_id": f"agent-{ST['hitl']}"}))[0]["assistant_id"]
    J(s.post(U + f"/api/v1/threads/{th}/runs/wait", json={"assistant_id": ha, "input": {"messages": [{"role": "user", "content": "Tính 12*12 bằng chay_code_python."}]}}, timeout=300))
    runs = J(s.get(U + f"/api/v1/threads/{th}/runs")); tt = J(s.get(U + f"/api/v1/threads/{th}"))
    ok("HITL: run ends 'interrupted', thread status interrupted, interrupt in state", runs[0]["status"] == "interrupted" and tt["status"] == "interrupted", (runs[0]["status"], tt["status"], runs[0].get("error")))
    v = J(s.post(U + f"/api/v1/threads/{th}/runs/wait", json={"assistant_id": ha, "command": {"resume": {"decisions": [{"type": "approve"}]}}}, timeout=300))
    ok("HITL resume via command -> tool runs (144)", "144" in msgs_text(v), msgs_text(v)[-100:])

def p5_cron_ttl():
    A = ST["asst"]
    cr = J(s.post(U + "/api/v1/runs/crons", json={"assistant_id": A, "schedule": "* * * * *", "input": {"messages": [{"role": "user", "content": "Nói 'cron ok'."}]}}))
    ok("cron created", cr.get("cron_id"))
    t = J(s.post(U + "/api/v1/threads", json={"ttl": {"ttl": 1}}))["thread_id"]
    got = wait(lambda: [x for x in J(s.post(U + "/api/v1/threads/search", json={"metadata": {"cron_id": cr["cron_id"]}}))], 150, 5)
    ok("cron fired -> new thread + run", got, len(got or []))
    s.delete(U + f"/api/v1/runs/crons/{cr['cron_id']}")
    gone = wait(lambda: s.get(U + f"/api/v1/threads/{t}").status_code == 404, 150, 5)
    ok("thread TTL -> auto-deleted", gone)

def p5_sdk():
    from langgraph_sdk import get_sync_client
    if "ed_key" not in ST: ST["ed_key"] = J(s.post(U + "/api/auth/keys", json={"name": "sdk"}))["key"]
    cl = get_sync_client(url=U + "/api/v1", api_key=ST["ed_key"])
    a = cl.assistants.search(limit=50)
    t = cl.threads.create(metadata={"sdk": TAG})
    out = cl.runs.wait(t["thread_id"], ST["sys_asst"], input={"messages": [{"role": "user", "content": "Nói 'sdk ok'."}]})
    ok("open-source LangGraph SDK client works against /api/v1 (X-Api-Key)", len(a) > 0 and msgs_text(out), msgs_text(out)[:60])
    chunks = list(cl.runs.stream(t["thread_id"], ST["sys_asst"], input={"messages": [{"role": "user", "content": "Lần nữa."}]}, stream_mode=["values", "updates"]))
    ok("SDK runs.stream", [c.event for c in chunks][0] == "metadata" and chunks[-1].event in ("values", "end"), [c.event for c in chunks])

# ======================================================= gap
def g_otlp_proto():
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span
    from opentelemetry.proto.common.v1.common_pb2 import KeyValue, AnyValue
    pid = J(s.post(U + "/api/obs/projects", json={"name": f"otlp-pb-{TAG}"}))["id"]; ST["otlp_pid"] = pid
    key = J(s.post(U + f"/api/obs/projects/{pid}/keys", params={"label": "pb"}))["api_key"]
    tid = os.urandom(16); root = os.urandom(8); child = os.urandom(8); now = time.time_ns()
    kv = lambda k, v: KeyValue(key=k, value=AnyValue(string_value=v) if isinstance(v, str) else AnyValue(int_value=v))
    req = ExportTraceServiceRequest(resource_spans=[ResourceSpans(scope_spans=[ScopeSpans(spans=[
        Span(trace_id=tid, span_id=root, name="agent.pb", start_time_unix_nano=now, end_time_unix_nano=now + 900_000_000, attributes=[kv("input.value", "câu hỏi pb")]),
        Span(trace_id=tid, span_id=child, parent_span_id=root, name="llm.pb", start_time_unix_nano=now + 1000, end_time_unix_nano=now + 500_000_000,
             attributes=[kv("gen_ai.request.model", "gemini-flash-lite-latest"), kv("gen_ai.usage.input_tokens", 100), kv("gen_ai.usage.output_tokens", 20)])])])])
    raw = req.SerializeToString()
    r = requests.post(U + "/ingest/otlp/v1/traces", data=raw, headers={"Content-Type": "application/x-protobuf", "x-api-key": key})
    ok("OTLP/HTTP protobuf accepted", r.status_code == 200 and J(r).get("accepted") == 2, r.text[:150])
    tr = J(s.get(U + f"/api/obs/traces/{uuid.UUID(bytes=tid)}"))
    rows = tr if isinstance(tr, list) else tr.get("runs", [])
    ok("protobuf trace: hex ids mapped, parent/child + tokens", len(rows) == 2 and any(x.get("input_tokens") == 100 for x in rows), [(x.get("name"), x.get("input_tokens")) for x in rows])
    tid2 = os.urandom(16); req.resource_spans[0].scope_spans[0].spans[0].trace_id = tid2; req.resource_spans[0].scope_spans[0].spans[1].trace_id = tid2
    r = requests.post(U + "/ingest/otlp/v1/traces", data=gzip.compress(req.SerializeToString()), headers={"Content-Type": "application/x-protobuf", "Content-Encoding": "gzip", "x-api-key": key})
    ok("OTLP protobuf + gzip", r.status_code == 200 and J(r).get("accepted") == 2, r.text[:100])
    ok("unknown content-type -> 415", requests.post(U + "/ingest/otlp/v1/traces", data=b"x", headers={"Content-Type": "text/plain", "x-api-key": key}).status_code == 415)
    ST["otlp_key"] = key

def g_semantic():
    pid = J(s.post(U + "/api/obs/projects", json={"name": f"errs-{TAG}"}))["id"]; ST["err_pid"] = pid
    key = J(s.post(U + f"/api/obs/projects/{pid}/keys", params={"label": "e"}))["api_key"]
    errs = ["Connection refused when connecting to database server", "Could not connect to the database: connection refused by host",
            "Database connection failed (host refused the connection)", "Request to payment API timed out after waiting",
            "Payment service did not respond in time (timeout)", "Invalid API key provided for the weather service"]
    t = time.time(); batch = []
    for i, e in enumerate(errs * 2):
        rid = str(uuid.uuid4()); batch.append({"id": rid, "trace_id": rid, "name": f"job{i % 6}", "run_type": "chain", "start_time": t, "end_time": t + 1, "error": e, "inputs": {"i": i}})
    r = requests.post(U + "/ingest/runs", json={"post": batch}, headers={"x-api-key": key})
    sig = J(s.get(U + f"/api/obs/projects/{pid}/errors")); sem = J(s.get(U + f"/api/obs/projects/{pid}/errors?mode=semantic"))
    ok("signature mode: 6 clusters (wording differs)", len(sig.get("clusters", [])) == 6, len(sig.get("clusters", [])))
    sizes = sorted([c["count"] for c in sem.get("clusters", [])], reverse=True)
    ok("semantic mode merges paraphrases (db=6, timeout=4, key=2)", sizes == [6, 4, 2], (r.status_code, sizes, [[m["signature"][:30] for m in c.get("members", [])] for c in sem.get("clusters", [])]))

def g_example_att():
    d = J(s.post(U + "/api/datasets", json={"name": f"att-{TAG}"}))
    ex = J(s.post(U + f"/api/datasets/{d['id']}/examples", json={"inputs": {"question": "Mã kho trong tệp đính kèm là gì? Chỉ trả về mã."}, "outputs": {"answer": "KHO-7781"}}))
    eid = ex["ids"][0]
    up = J(s.post(U + f"/api/examples/{eid}/attachments", files={"file": ("phieu.txt", "Phiếu nhập kho\nMã kho: KHO-7781\nNgày: 19/10/2026".encode(), "text/plain")}))
    ok("upload attachment to example", up.get("id"))
    ok("list example attachments", [a["name"] for a in J(s.get(U + f"/api/examples/{eid}/attachments"))] == ["phieu.txt"])
    ok("download example attachment", b"KHO-7781" in s.get(U + f"/api/example_attachments/{up['id']}").content)
    pc = J(s.post(U + "/api/hub/commit", json={"name": f"att_prompt_{TAG}", "template": "{{question}}\n\n{{attachments}}", "model": "gemini-flash-lite-latest", "temperature": 0}))
    pr = next(p for p in J(s.get(U + "/api/hub")) if p["name"] == f"att_prompt_{TAG}")
    x = J(s.post(U + "/api/experiments", json={"name": f"att-exp-{TAG}", "dataset_id": d["id"], "target": {"type": "prompt", "prompt_id": pr["id"], "model": "gemini-flash-lite-latest"}}))
    xid = x.get("id")
    done = wait(lambda: (lambda e: e if e and e[0].get("status") not in ("running", "pending", "queued") else None)([e for e in J(s.get(U + "/api/experiments")) if e["id"] == xid]), 150, 3)
    cmp_ = J(s.get(U + f"/api/experiments/compare?ids={xid}"))
    outs = [r.get("output") or r.get("answer") or "" for r in cmp_.get("results", [])]
    ok("experiment target sees attachment content (answers KHO-7781)", any("KHO-7781" in str(o) for o in outs), (done and done[0].get("status"), [str(o)[:80] for o in outs], [r.get("error") for r in cmp_.get("results", [])]))
    ST["att_exp"] = xid

STEPS = [("P7 sweep", p7_sweep), ("P7 users/RBAC", p7_users), ("P7 API keys", p7_keys), ("P7 workspaces", p7_ws), ("P7 audit", p7_audit), ("P7 sandbox", p7_sandbox),
         ("P6 gateway", p6_gateway), ("P5 setup", p5_setup), ("P5 threads/runs", p5_threads_runs), ("P5 background + double-texting", p5_bg_and_dt),
         ("P5 stateless/webhook/HITL", p5_stateless_webhook_hitl), ("P5 SDK", p5_sdk), ("P5 cron/TTL", p5_cron_ttl),
         ("gap OTLP protobuf", g_otlp_proto), ("gap semantic clustering", g_semantic), ("gap example attachments", g_example_att)]
only = sys.argv[1:]
for n, f in STEPS:
    if not only or any(o in n for o in only): step(n, f)
json.dump({k: v for k, v in ST.items() if isinstance(v, (str, int, float))}, open(os.path.join(os.path.dirname(__file__), "p567_state.json"), "w"))
print(f"\nTOTAL {sum(R.values())}/{len(R)}")
