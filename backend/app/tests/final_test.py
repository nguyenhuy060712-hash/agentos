"""Final push tests: RLS workspace isolation, per-user privacy, backoff lockout, overrides, token streaming, MCP, A2A, Anthropic,
sandbox volumes/snapshots/links, export, presigned feedback, feedback criteria/audit, new evaluator kinds, dataset schema/transforms/bind,
retry, prompt formats/context/presets, playground tools, Store re-embed."""
import asyncio, json, os, sys, time, uuid
import requests
c = dict(l.split("=", 1) for l in open("/workspace/secrets/deepagent_web.txt").read().split())
U = c["url"].rstrip("/"); PW = c["password"]
s = requests.Session(); s.auth = ("moza", PW)
R = {}; TAG = str(int(time.time()))
def ok(n, cond, extra=""): R[n] = bool(cond); print(("PASS " if cond else "FAIL ") + n, "|", str(extra)[:240], flush=True)
def J(r):
    try: return r.json()
    except Exception: return {"_status": r.status_code, "_text": r.text[:300]}
def step(name, fn):
    print(f"\n== {name}", flush=True)
    try: fn()
    except Exception as e:
        import traceback; traceback.print_exc(); ok(f"{name} (exception)", False, repr(e)[:200])
ST = {}

def setup():
    ws = J(s.post(U + "/api/admin/workspaces", json={"name": f"iso-{TAG}"})); ST["ws2"] = ws["id"]
    for nm, role, w in (("isoA", "editor", ws["id"]), ("isoB", "editor", ws["id"]), ("isoV", "viewer", 1)):
        u = J(s.post(U + "/api/admin/users", json={"username": f"{nm}{TAG}", "password": "Pw-" + TAG + "-x1", "role": role, "workspace_id": w}))
        ST[nm] = (u["id"], f"{nm}{TAG}")
    ST.setdefault("ag1", J(s.get(U + "/api/agents"))[0]["id"])  # sections can run alone
    ok("setup workspace + users", all(k in ST for k in ("isoA", "isoB", "isoV")), ST)
def sess(nm):
    x = requests.Session(); x.auth = (ST[nm][1], "Pw-" + TAG + "-x1"); return x

def isolation():
    A_ = sess("isoA")
    # ws1 resources by owner
    ds1 = J(s.post(U + "/api/datasets", json={"name": f"iso-ds-{TAG}", "description": "ws1"}))
    ag1 = J(s.get(U + "/api/agents"))[0]["id"]
    pr = J(s.get(U + "/api/obs/projects")); pid1 = pr[0]["id"] if isinstance(pr, list) and pr else None
    ST["ds1"] = ds1["id"]; ST["ag1"] = ag1
    ok("ws2 user: dataset list has no ws1 dataset", all(d["id"] != ds1["id"] for d in J(A_.get(U + "/api/datasets"))))
    ok("ws2 user: GET ws1 dataset by id -> 404", A_.get(U + f"/api/datasets/{ds1['id']}").status_code == 404)
    ok("ws2 user: agents list empty of ws1", all(a["id"] != ag1 for a in J(A_.get(U + "/api/agents"))))
    r = A_.post(U + "/chat", json={"thread_id": "x", "message": "hi", "agent_id": ag1})
    ok("ws2 user: cannot run ws1 agent (404)", r.status_code == 404, r.status_code)
    if pid1:
        r = A_.get(U + f"/api/obs/projects/{pid1}/runs"); ok("ws2 user: ws1 project runs hidden", r.status_code == 404 or not J(r) or (isinstance(J(r), dict) and not J(r).get("runs")), r.text[:120])
    ev1 = J(s.get(U + "/api/evaluators")); ok("ws2 user: evaluators of ws1 hidden", not any(e["id"] in [x["id"] for x in ev1] for e in J(A_.get(U + "/api/evaluators"))))
    pr1 = J(s.get(U + "/api/prompts")) if s.get(U + "/api/prompts").status_code == 200 else []
    r = A_.post(U + "/api/datasets", json={"name": f"iso-ds-{TAG}", "description": "same name in ws2"})
    ok("same dataset name allowed in another workspace", r.status_code == 200, r.text[:120])
    ST["ds2"] = J(r).get("id")
    ok("owner does not see ws2 dataset", all(d["id"] != ST["ds2"] for d in J(s.get(U + "/api/datasets"))))

def privacy():
    A_, B_ = sess("isoA"), sess("isoB")
    ag = J(A_.post(U + "/api/agents", json={"name": f"ws2-agent-{TAG}", "model": "gemini-flash-lite-latest", "planning": False}))
    ST["ag2"] = ag.get("id")
    asst = J(A_.post(U + "/api/v1/assistants", json={"graph_id": f"agent-{ag['id']}" if ag.get("id") else "x", "name": "a"}))
    ST["asst"] = asst.get("assistant_id")
    t = J(A_.post(U + "/api/v1/threads", json={})); tid = t.get("thread_id")
    ok("user A creates thread", bool(tid), t)
    ok("user B (same ws, editor) cannot see A's thread", B_.get(U + f"/api/v1/threads/{tid}").status_code == 404)
    lst = J(B_.post(U + "/api/v1/threads/search", json={}))
    ok("user B thread search excludes A's thread", isinstance(lst, list) and all(x["thread_id"] != tid for x in lst), str(lst)[:100])
    ST["tid"] = tid

def lockout():
    v = ST["isoV"][1]; codes = []
    for i in range(16):
        r = requests.post(U + "/api/auth/login", json={"username": v, "password": "wrong-pass-" + str(i)}); codes.append(r.status_code)
        if r.status_code == 429: ST["ra"] = r.headers.get("retry-after"); break
    ok("backoff: 429 with Retry-After after repeated failures", 429 in codes and ST.get("ra"), (codes, ST.get("ra")))
    ok("backoff short (not 15-minute lock)", ST.get("ra") and int(ST["ra"]) <= 300, ST.get("ra"))
    time.sleep(int(ST.get("ra") or 1) + 1)
    r = requests.post(U + "/api/auth/login", json={"username": v, "password": "Pw-" + TAG + "-x1"})
    ok("correct password works after backoff window", r.status_code == 200, r.status_code)
    r = s.get(U + "/api/auth/me"); ok("owner unaffected", r.status_code == 200)

def overrides_tokens():
    ag = ST["ag1"]; toks = []; done = None
    with s.post(U + "/chat/stream", json={"thread_id": f"tok-{TAG}", "message": "Write three short sentences about the sea.", "agent_id": ag,
                                          "overrides": {"system_prompt": "Always answer in English and end with the word OVERRIDE-OK.", "tools": [], "planning": False}}, stream=True, timeout=180) as r:
        for line in r.iter_lines():
            if not line: continue
            e = json.loads(line)
            if e["type"] == "token": toks.append(e["text"])
            if e["type"] == "done": done = e
    ok("token-level streaming emits token events", len(toks) >= 2, len(toks))
    ok("per-run override applied (system_prompt)", done and "OVERRIDE-OK" in (done.get("reply") or ""), (done or {}).get("reply", "")[:120])
    r = s.post(U + "/chat", json={"thread_id": f"ov-{TAG}", "message": "hi", "agent_id": ag, "overrides": {"temperature": 9}})
    ok("invalid override rejected (400)", r.status_code == 400, r.status_code)

def server_stream():
    from langgraph_sdk import get_sync_client
    cl = get_sync_client(url=U + "/api/v1", headers={"Authorization": "Basic " + __import__("base64").b64encode(f"moza:{PW}".encode()).decode()})
    ST.setdefault("ag1", J(s.get(U + "/api/agents"))[0]["id"])  # allow running this section alone
    a = cl.assistants.create(graph_id=f"agent-{ST['ag1']}", name=f"tok-{TAG}", config={"configurable": {"system_prompt": "Reply in English and end with ASSIST-CFG.", "tools": [], "planning": False,
                                       "model": os.environ.get("FT_STREAM_MODEL", "gpt-5.4-nano"), "fallback_models": []}})  # OpenAI default: Gemini free tier 429/503 made this flaky
    t = cl.threads.create(); evs = []
    for ch in cl.runs.stream(t["thread_id"], a["assistant_id"], input={"messages": [{"role": "user", "content": "Say hello in two sentences."}]}, stream_mode=["messages-tuple", "values"]):
        evs.append(ch)
    msgs = [e for e in evs if e.event == "messages"]; vals = [e for e in evs if e.event == "values"]
    ok("Agent Server SSE 'messages' token events", len(msgs) >= 1, len(msgs))
    last = json.dumps(vals[-1].data if vals else {}, ensure_ascii=False)
    ok("assistant config applied to run", "ASSIST-CFG" in last, last[-160:])

def mcp_test():
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client, create_mcp_http_client
    key = J(s.post(U + "/api/auth/keys", json={"name": f"mcp-{TAG}", "role": "editor", "expires_days": 1}))["key"]; ST["key"] = key
    async def go():
        async with create_mcp_http_client(headers={"Authorization": f"Bearer {key}"}) as hc:
          async with streamable_http_client(U + "/mcp", http_client=hc) as (r, w):
            async with ClientSession(r, w) as cs:
                await cs.initialize(); tl = await cs.list_tools()
                res = await cs.call_tool("ask_agent", {"agent_id": ST["ag1"], "message": "Reply with exactly: MCP-PONG"})
                return [t.name for t in tl.tools], res
    names, res = asyncio.run(go())
    ok("MCP (official client) tools/list", {"list_agents", "ask_agent"} <= set(names), names)
    ok("MCP tools/call ask_agent", "MCP-PONG" in res.content[0].text, res.content[0].text[:100])
    ok("MCP without auth -> 401", requests.post(U + "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401)

def a2a_test():
    h = {"Authorization": f"Bearer {ST['key']}"}
    card = J(requests.get(U + f"/a2a/{ST['ag1']}/.well-known/agent-card.json", headers=h))
    ok("A2A agent card", card.get("name") and card.get("capabilities", {}).get("streaming"), str(card)[:120])
    r = J(requests.post(U + f"/a2a/{ST['ag1']}", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "message/send",
          "params": {"message": {"role": "user", "messageId": "m1", "parts": [{"kind": "text", "text": "Reply with exactly: A2A-PONG"}]}}}))
    res = r.get("result", {}); ok("A2A message/send -> completed task", res.get("status", {}).get("state") == "completed" and "A2A-PONG" in json.dumps(res), str(r)[:200])
    g = J(requests.post(U + f"/a2a/{ST['ag1']}", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"id": res.get("id")}}))
    ok("A2A tasks/get", g.get("result", {}).get("id") == res.get("id"))
    n = 0
    with requests.post(U + f"/a2a/{ST['ag1']}", headers=h, stream=True, json={"jsonrpc": "2.0", "id": 3, "method": "message/stream",
                       "params": {"message": {"role": "user", "messageId": "m2", "parts": [{"kind": "text", "text": "Count from 1 to 5."}]}}}) as rr:
        for line in rr.iter_lines():
            if line.startswith(b"data:"): n += 1
    ok("A2A message/stream SSE", n >= 3, n)

def anthropic_test():
    import anthropic
    vk = J(s.post(U + "/api/gw/keys", json={"name": f"anth-{TAG}", "models": ["gemini-flash-lite-latest"]})); ST["vk"] = vk
    cl = anthropic.Anthropic(base_url=U, api_key=vk["key"])
    m = cl.messages.create(model="gemini-flash-lite-latest", max_tokens=100, messages=[{"role": "user", "content": "Reply with exactly: ANTH-OK"}])
    ok("Anthropic SDK messages.create", "ANTH-OK" in m.content[0].text, m.content[0].text[:60])
    txt = ""
    with cl.messages.stream(model="gemini-flash-lite-latest", max_tokens=100, messages=[{"role": "user", "content": "Count 1 to 5 separated by commas."}]) as st:
        for t in st.text_stream: txt += t
    ok("Anthropic SDK streaming", "3" in txt, txt[:60])
    tools = [{"name": "get_weather", "description": "Get weather for a city", "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}]
    m = cl.messages.create(model="gemini-flash-lite-latest", max_tokens=200, tools=tools, messages=[{"role": "user", "content": "What's the weather in Hanoi? Use the tool."}])
    tu = [b for b in m.content if b.type == "tool_use"]
    ok("Anthropic tool_use", m.stop_reason == "tool_use" and tu and tu[0].input.get("city"), m.stop_reason)
    if tu:
        m2 = cl.messages.create(model="gemini-flash-lite-latest", max_tokens=200, tools=tools, messages=[{"role": "user", "content": "What's the weather in Hanoi? Use the tool."},
             {"role": "assistant", "content": m.content}, {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tu[0].id, "content": "31C sunny"}]}])
        ok("Anthropic tool_result round-trip", "31" in m2.content[0].text, m2.content[0].text[:80])
    try: anthropic.Anthropic(base_url=U, api_key="bad").messages.create(model="gemini-flash-lite-latest", max_tokens=10, messages=[{"role": "user", "content": "x"}]); bad = False
    except anthropic.AuthenticationError: bad = True
    ok("Anthropic bad key -> authentication_error", bad)

def sandbox_test():
    sb = J(s.post(U + "/api/sandboxes", json={"name": f"t-{TAG}"}))["id"]; ST["sb"] = sb
    vol = J(s.post(U + "/api/sandbox_volumes", json={"name": f"v{TAG}"})); ST["vol"] = vol["id"]
    r = J(s.post(U + f"/api/sandboxes/{sb}/exec_mounted", json={"code": f"open('/mnt/v{TAG}/a.txt','w').write('vol-data'); open('w.txt','w').write('work-data'); print('ok')", "mounts": [{"volume": f"v{TAG}"}]}))
    ok("sandbox exec with rw volume mount", r.get("exit") == 0, r)
    sb2 = J(s.post(U + "/api/sandboxes", json={"name": f"t2-{TAG}"}))["id"]; ST["sb2"] = sb2
    r = J(s.post(U + f"/api/sandboxes/{sb2}/exec_mounted", json={"code": f"print(open('/mnt/v{TAG}/a.txt').read())\ntry:\n open('/mnt/v{TAG}/b','w')\nexcept Exception as e: print('RO')", "mounts": [{"volume": vol["id"], "readonly": True}]}))
    ok("volume shared + read-only enforced", "vol-data" in r.get("stdout", "") and "RO" in r.get("stdout", ""), r)
    sn = J(s.post(U + f"/api/sandboxes/{sb}/snapshots", json={"name": "snap1"})); ST["snap"] = sn["id"]
    ok("snapshot created", sn.get("bytes", 0) > 0, sn)
    s.post(U + f"/api/sandboxes/{sb}/exec", json={"code": "import os; os.remove('w.txt')"})
    r = J(s.post(U + f"/api/sandbox_snapshots/{sn['id']}/restore/{sb}"))
    files = [f["path"] for f in J(s.get(U + f"/api/sandboxes/{sb}/files"))]
    ok("snapshot restore brings file back", "w.txt" in files, files)
    cl = J(s.post(U + f"/api/sandbox_snapshots/{sn['id']}/clone", json={"name": "clone"})); ST["sb3"] = cl["id"]
    ok("create sandbox from snapshot", "w.txt" in [f["path"] for f in J(s.get(U + f"/api/sandboxes/{cl['id']}/files"))])
    ln = J(s.post(U + f"/api/sandboxes/{sb}/link", json={"path": "w.txt", "expires_s": 120}))
    r = requests.get(ln["url"]); ok("signed download link works anonymously", r.status_code == 200 and r.text == "work-data", r.status_code)
    r = requests.get(ln["url"].replace("sig=", "sig=0")); ok("tampered link -> 403", r.status_code == 403)
    ok("other workspace cannot see sandbox", sess("isoA").get(U + f"/api/sandboxes/{sb}/files").status_code == 404)

def export_feedback():
    pr = J(s.get(U + "/api/obs/projects")); pid = next(p["id"] for p in pr if p["name"] == "agents")
    e = J(s.post(U + "/api/exports", json={"project_id": pid, "fmt": "jsonl"}))
    for _ in range(40):
        x = next(j for j in J(s.get(U + "/api/exports")) if j["id"] == e["id"])
        if x["status"] in ("done", "error"): break
        time.sleep(2)
    ok("bulk export job done (monitor shows rows)", x["status"] == "done" and x["rows"] > 0, x)
    r = s.get(U + f"/api/exports/{e['id']}/download"); ok("export download gz", r.status_code == 200 and r.content[:2] == b"\x1f\x8b")
    runs = J(s.get(U + f"/api/obs/runs?project_id={pid}&limit=1"))
    rid = runs[0]["id"]; ST["rid"] = rid
    s.post(U + "/api/feedback_configs", json={"key": "helpful", "type": "continuous", "min": 0, "max": 1})
    t = J(s.post(U + f"/api/obs/runs/{rid}/feedback_token", json={"key": "helpful", "max_uses": 1}))
    ok("feedback config rejects out-of-range", requests.post(t["url"], json={"score": 5}).status_code == 400)
    r = requests.post(t["url"], json={"score": 1, "comment": "great"}); ok("presigned feedback accepted anonymously", r.status_code == 200, r.text[:100])
    ok("presigned token single-use", requests.post(t["url"], json={"score": 1}).status_code == 403)

def evaluators_test():
    a = J(s.post(U + "/api/evaluators", json={"name": f"assert-{TAG}", "kind": "assertion", "config": {"key": "fmt", "all_or_nothing": False,
          "assertions": [{"type": "contains", "value": "Paris"}, {"type": "max_length", "value": 50}, {"type": "no_error"}, {"type": "regex", "value": "^Z"}]}}))
    r = J(s.post(U + f"/api/evaluators/{a['id']}/test", json={"run": {"input": "q", "output": "Paris is the capital"}}))
    ok("assertion evaluator (3/4 pass = 0.75)", r and r[0]["score"] == 0.75, r)
    d = J(s.post(U + "/api/evaluators", json={"name": f"decide-{TAG}", "kind": "decision", "config": {"key": "route", "prompt": "Should this answer be escalated to a human? Answer: {{output}}",
          "choices": ["escalate", "accept"], "scores": {"escalate": 0, "accept": 1}}}))
    r = J(s.post(U + f"/api/evaluators/{d['id']}/test", json={"run": {"input": "2+2?", "output": "4"}}))
    ok("decision evaluator returns a choice", r and r[0].get("value") in ("escalate", "accept"), r)
    cpo = J(s.post(U + "/api/evaluators", json={"name": f"comp-{TAG}", "kind": "composite", "config": {"key": "overall", "evaluator_ids": [a["id"], d["id"]], "aggregate": "weighted_mean", "weights": {str(a["id"]): 3, str(d["id"]): 1}}}))
    r = J(s.post(U + f"/api/evaluators/{cpo['id']}/test", json={"run": {"input": "2+2?", "output": "Paris 4"}}))
    ok("composite evaluator aggregates children", r and r[0]["score"] is not None and "weighted_mean" in r[0]["comment"], r)
    ST["evs"] = [a["id"], d["id"], cpo["id"]]
    fb = J(s.post(U + f"/api/evaluators/{a['id']}/run_on/{ST['rid']}"))
    f = next(x for x in J(s.get(U + f"/api/evaluators/{a['id']}/audit"))["rows"])
    c = J(s.post(U + f"/api/feedback/{f['id']}/correct", json={"score": 1.0, "comment": "human says fine"}))
    au = J(s.get(U + f"/api/evaluators/{a['id']}/audit"))
    ok("evaluator score audit/correction", au["audited"] >= 1 and c.get("correction"), au.get("agreement"))
    ok("bad evaluator config rejected", s.post(U + "/api/evaluators", json={"name": f"bad-{TAG}", "kind": "assertion", "config": {"key": "k", "assertions": [{"type": "nope"}]}}).status_code == 400)

def dataset_test():
    did = ST["ds1"]
    r = s.patch(U + f"/api/datasets/{did}/config", json={"inputs_schema": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]},
                "transformations": ["strip_strings", "remove_extra_fields"], "evaluator_ids": [ST["evs"][0]]})
    ok("dataset config (schema/transforms/bound evaluators)", r.status_code == 200, r.text[:100])
    ok("schema violation rejected", s.post(U + f"/api/datasets/{did}/examples", json={"inputs": {"q": 1}}).status_code == 400)
    r = J(s.post(U + f"/api/datasets/{did}/examples", json={"inputs": {"question": "  Capital of France?  ", "junk": 1}, "outputs": {"answer": "Paris"}}))
    ex = J(s.get(U + f"/api/datasets/{did}"))["examples"][0]["inputs"]
    ok("transformations applied (strip + remove extra)", ex == {"question": "Capital of France?"}, ex)
    e = J(s.post(U + "/api/experiments", json={"name": f"bound-{TAG}", "dataset_id": did, "target": {"type": "agent", "agent_id": ST["ag1"]}}))
    x = next(z for z in J(s.get(U + f"/api/experiments?dataset_id={did}")) if z["id"] == e["id"])
    ok("experiment uses dataset-bound evaluators", x["evaluator_ids"] == [ST["evs"][0]], x["evaluator_ids"]); ST["exp"] = e["id"]
    r = J(s.post(U + f"/api/experiments/{e['id']}/retry_failed")); ok("retry_failed endpoint", "retrying" in r, r)
    time.sleep(25)
    r = s.get(U + f"/api/experiments/{e['id']}/results.jsonl"); ok("results.jsonl reader", r.status_code == 200, r.text[:80])

def prompt_test():
    r = J(s.post(U + "/api/playground/run", json={"template": "Reply with exactly: {word}-{n}", "variables": {"word": "FSTR", "n": 7}, "template_format": "f-string", "model": "gemini-flash-lite-latest"}))
    ok("f-string template", "FSTR-7" in (r.get("output") or r.get("answer") or json.dumps(r)), str(r)[:120])
    s.put(U + "/api/context_files", json={"name": f"style{TAG}", "content": "Always end with CTX-OK."})
    s.post(U + "/api/model_presets", json={"name": f"p{TAG}", "model": "gemini-flash-lite-latest", "temperature": 0})
    r = J(s.post(U + "/api/playground/chat", json={"messages": [{"role": "system", "content": "{{ctx:style" + TAG + "}}"}, {"role": "user", "content": "{% for i in range(3) %}J{{ i }} {% endfor %}- repeat that"}],
                                                   "template_format": "jinja2", "preset": f"p{TAG}"}))
    ok("jinja2 + context file + model preset", "CTX-OK" in (r.get("message", {}).get("content") or ""), str(r)[:160])
    r = J(s.post(U + "/api/playground/chat", json={"messages": [{"role": "user", "content": "Weather in Paris? use tool"}], "model": "gemini-flash-lite-latest", "tools": [{"type": "function", "function": {"name": "weather", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]}))
    ok("playground tools -> tool_calls", (r.get("message") or {}).get("tool_calls"), str(r)[:120])
    ok("jinja2 sandbox blocks attribute escape", s.post(U + "/api/playground/chat", json={"messages": [{"role": "user", "content": "{{ ''.__class__.__mro__ }}"}], "template_format": "jinja2"}).status_code == 400)

def store_test():
    r = J(s.post(U + "/api/admin/store/reembed"))
    res = [x for x in r.get("results", []) if "cos_new_vs_fresh" in x]
    ok("Store re-embedded, vectors match per-text embedding (cos>=0.99)", res and all(x["cos_new_vs_fresh"] >= 0.99 for x in res), [(x["key"], x.get("cos_old_vs_fresh"), x["cos_new_vs_fresh"]) for x in res])
    ST["store"] = res

def cleanup():
    for k in ("isoA", "isoB", "isoV"): s.patch(U + f"/api/admin/users/{ST[k][0]}", json={"disabled": True})
    for k in ("sb", "sb2", "sb3"):
        if ST.get(k): s.delete(U + f"/api/sandboxes/{ST[k]}")
    if ST.get("vol"): s.delete(U + f"/api/sandbox_volumes/{ST['vol']}")
    if ST.get("vk"): s.patch(U + f"/api/gw/keys/{ST['vk']['id']}", json={"enabled": False})

only = sys.argv[1:]
for n, f in [("setup", setup), ("isolation", isolation), ("privacy", privacy), ("lockout", lockout), ("overrides_tokens", overrides_tokens), ("server_stream", server_stream),
             ("mcp", mcp_test), ("a2a", a2a_test), ("anthropic", anthropic_test), ("sandbox", sandbox_test), ("export_feedback", export_feedback),
             ("evaluators", evaluators_test), ("dataset", dataset_test), ("prompt", prompt_test), ("store", store_test), ("cleanup", cleanup)]:
    if not only or n in only or n in ("setup", "cleanup"): step(n, f)
print(f"\nTOTAL {sum(R.values())}/{len(R)}")
json.dump({"results": R, "store": ST.get("store")}, open("/workspace/adk_ha_build/os/tests/final_state.json", "w"), default=str)
