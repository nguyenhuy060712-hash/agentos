"""GĐ9.2 Prompt Canvas + GĐ9.3 Playground lab (24/10/2026). Local server :8020 with the fake LLM (overlay/fake_llm.py: deterministic rewrite per quick action,
tool call via "[tool:name] {json}", structured output via "[json] {obj}" / "[json-invalid]", image echo) => $0. In-process gateway tests stub the hooks like
tests/test_gd7r_gateway.py `_light()`. Throwaway workspaces + users; server tests share state through the `env` fixture, run in file order."""
import asyncio, base64, json, os, sys, time, uuid
import httpx, psycopg, pytest
from test_fixes import B, ADMIN, a  # noqa: F401  (fixtures)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "overlay"))
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
PW = "phase0-gd92-local"; SUF = uuid.uuid4().hex[:6]
NAME = f"canvas-{SUF}"
PNG = base64.b64encode(bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360f8cfc000000301010018dd8db00000000049454e44ae426082")).decode()
TEXT = "Bạn là trợ lý bán hàng. Luôn chào khách bằng tên {{ten}}. Trả lời ngắn gọn và lịch sự. Cuối cùng hỏi khách cần gì thêm."

def pg(): return psycopg.connect(PG, autocommit=True)

# ------------------------------------------------------------------ pure unit tests
def test_variables_and_word_diff():
    import deepagent_api  # noqa: F401  (import order: the app first, as in the other GD9 tests)
    import prompt_lab as P
    assert P.vars_of("Chào {{ ten }}, {ho} {{a.b}} {\"k\": 1}") == {"{{ten}}", "{ho}", "{{a.b}}"}
    segs = P.segments("một hai ba bốn", "một hai năm bốn sáu")
    assert [s["t"] for s in segs] == ["eq", "chg", "eq", "chg"] and [s["id"] for s in segs if s["t"] == "chg"] == [0, 1]
    assert P.apply_hunks(segs, "all") == "một hai năm bốn sáu" and P.apply_hunks(segs, []) == "một hai ba bốn"
    assert P.apply_hunks(segs, [0]) == "một hai năm bốn" and P.apply_hunks(segs, [1]) == "một hai ba bốn sáu"      # accept / reject PER hunk
    assert P.segments("giống hệt", "giống hệt") == [{"t": "eq", "text": "giống hệt"}]
    s2 = P.segments("a b c", "x y c")          # whitespace between two changes: ONE hunk, not two
    assert sum(1 for s in s2 if s["t"] == "chg") == 1
    st = P.diff_stats(P.segments("một hai ba", "một bốn ba năm")); assert st == {"hunks": 2, "added_words": 2, "removed_words": 1}

def _light(monkeypatch, gw):
    ctxs = []
    async def budget(ctx, fresh=False): ctxs.append(ctx); return []
    monkeypatch.setattr(gw, "budget_check", budget)
    monkeypatch.setattr(gw, "ctx_now", lambda *a, **k: {"feature": gw.FEATURE.get(), "provider": a[0] if a else None})
    monkeypatch.setattr(gw, "_cur_ws", lambda: None)
    monkeypatch.setattr(gw, "gemini_only", lambda: False)
    from fake_llm import FakeAgentModel
    monkeypatch.setattr(gw, "_model", lambda name, temperature=None: FakeAgentModel(model_name=f"fake:{name}"))
    sink = []
    async def usage(*a): sink.append(a)
    monkeypatch.setattr(gw, "USAGE_SINK", [usage]); monkeypatch.setattr(gw, "COOLDOWN", {})
    return ctxs, sink

def test_gateway_lc_call_is_tagged_with_its_feature_and_uses_langchain_primitives(monkeypatch):
    import gateway as gw
    from langchain_core.messages import HumanMessage
    ctxs, sink = _light(monkeypatch, gw)
    r = asyncio.run(gw.lc_call(["gemini-3.6-flash"], [HumanMessage("xin chào")], feature="prompt_canvas"))
    assert r["message"].content.startswith("Echo") and ctxs[-1]["feature"] == "prompt_canvas" and len(sink) == 1 and gw.FEATURE.get() is None   # tagged, then reset
    r = asyncio.run(gw.lc_call(["gemini-3.6-flash"], [HumanMessage("[json] {\"ten\": \"An\"}")], feature="playground",
                               schema={"title": "Out", "type": "object", "properties": {"ten": {"type": "string"}}, "required": ["ten"]}))   # with_structured_output
    assert r["parsed"] == {"ten": "An"} and ctxs[-1]["feature"] == "playground"
    got = []
    async def tok(t): got.append(t)
    r = asyncio.run(gw.lc_call(["gemini-3.6-flash"], [HumanMessage("một hai ba")], feature="prompt_canvas", on_token=tok))      # astream
    assert "".join(got) == "Echo: một hai ba" and len(got) > 2

def test_tune_applies_only_fields_the_model_has(monkeypatch):
    import gateway as gw
    from langchain_google_genai import ChatGoogleGenerativeAI
    llm = gw.tune(ChatGoogleGenerativeAI(model="gemini-3.6-flash", google_api_key="x"), {"max_tokens": 321, "top_p": 0.5, "reasoning_effort": "low"}, json_mode=True)
    assert llm.max_output_tokens == 321 and llm.top_p == 0.5 and llm.reasoning_effort == "low" and llm.response_mime_type == "application/json"
    from fake_llm import FakeAgentModel
    f = FakeAgentModel(); assert gw.tune(f, {"max_tokens": 9, "reasoning_effort": "high"}) is f      # the fake has none of these fields: untouched, no error
    assert "prompt_canvas" in gw.FEATURE_VI

# ------------------------------------------------------------------ server tests
def user(a, name, ws, role="editor"):
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": role, "workspace_id": ws}); assert r.status_code == 200, r.text
    return httpx.Client(base_url=B, auth=(name, PW), timeout=150)

@pytest.fixture(scope="module")
def env(a):
    wa = a.post("/api/admin/workspaces", json={"name": f"cp-a-{SUF}"}).json()["id"]; wb = a.post("/api/admin/workspaces", json={"name": f"cp-b-{SUF}"}).json()["id"]
    e = {"wa": wa, "wb": wb, "ed": user(a, f"cp_ed_{SUF}", wa), "vw": user(a, f"cp_vw_{SUF}", wa, "viewer"), "oth": user(a, f"cp_oth_{SUF}", wb)}
    yield e
    for k in ("ed", "vw", "oth"): e[k].close()
    with pg() as c:
        for w in (wa, wb):
            c.execute("DELETE FROM model_presets WHERE workspace_id=%s", (w,)); c.execute("DELETE FROM gateway_usage WHERE workspace_id=%s", (w,))
            c.execute("DELETE FROM prompts WHERE workspace_id=%s", (w,)); c.execute("DELETE FROM feedback WHERE created_by LIKE %s", (f"cp\\_%\\_{SUF}",))
        c.execute("DELETE FROM users WHERE username LIKE %s", (f"cp\\_%\\_{SUF}",))

def rewrite(c, **kw):
    r = c.post("/api/hub/canvas/rewrite", json={"stream": False, **kw}); assert r.status_code == 200, r.text; return r.json()

def sse(c, **kw):
    ev = []
    with c.stream("POST", "/api/hub/canvas/rewrite", json=kw) as r:
        assert r.status_code == 200, r.read()
        for line in r.iter_lines():
            if line.startswith("data: "): ev.append(json.loads(line[6:]))
    return ev

def test_canvas_rewrite_diff_commit_with_right_parent(env):
    c = env["ed"]
    r = c.post("/api/hub/commit", json={"name": NAME, "template": TEXT, "model": "gemini-3.6-flash", "temperature": 0.2, "variables": {"ten": "An"}, "message": "v1"}); assert r.status_code == 200, r.text
    pid, v1 = r.json()["id"], r.json()["version"]; env["pid"] = pid
    a1 = rewrite(c, template=TEXT, action="safety")                                         # whole prompt, quick action
    assert a1["proposed"].startswith(TEXT) and "Không tiết lộ" in a1["proposed"] and a1["variables_ok"] and a1["selection"] is None
    assert a1["stats"]["hunks"] == 1 and a1["segments"][0] == {"t": "eq", "text": TEXT} and a1["model"].startswith("gemini")
    # live diff + accept / reject per hunk through the API
    d = c.post("/api/hub/canvas/diff", json={"a": TEXT, "b": a1["proposed"]}).json(); assert d["segments"] == a1["segments"]
    assert c.post("/api/hub/canvas/apply", json={"a": TEXT, "b": a1["proposed"], "accept": []}).json()["text"] == TEXT
    assert c.post("/api/hub/canvas/apply", json={"a": TEXT, "b": a1["proposed"], "accept": "all"}).json()["text"] == a1["proposed"]
    # Save = a NEW commit whose parent is the version the canvas was opened on (+ message, optional tag)
    s = c.post("/api/hub/canvas/commit", json={"prompt_id": pid, "base_version": v1, "template": a1["proposed"], "message": "Thêm ràng buộc an toàn", "tag": "staging"})
    assert s.status_code == 200, s.text; v2 = s.json()["version"]; assert v2 == v1 + 1 and s.json()["parent_version"] == v1
    h = c.get(f"/api/hub/{pid}").json()
    row2 = next(v for v in h["versions"] if v["version"] == v2)
    assert row2["parent_version"] == v1 and row2["note"] == "Thêm ràng buộc an toàn" and row2["template"] == a1["proposed"] and row2["model"] == "gemini-3.6-flash" and row2["temperature"] == 0.2
    assert {t["tag"]: t["version"] for t in h["tags"]} == {"staging": v2}
    # branching: a second edit opened on v1 (not the head) gets parent v1 too
    a2 = rewrite(c, template=TEXT, action="clearer")
    s3 = c.post("/api/hub/canvas/commit", json={"prompt_id": pid, "base_version": v1, "template": a2["proposed"], "message": "Rõ ràng hơn"}); assert s3.status_code == 200, s3.text
    assert s3.json()["version"] == v2 + 1 and s3.json()["parent_version"] == v1
    assert c.post("/api/hub/canvas/commit", json={"prompt_id": pid, "base_version": v1, "template": TEXT}).status_code == 400          # nothing changed
    assert c.post("/api/hub/canvas/commit", json={"prompt_id": pid, "base_version": 99, "template": "x"}).status_code == 404
    env["v1"], env["v2"] = v1, v2

def test_quick_action_on_a_selection_only_changes_that_part(env):
    c = env["ed"]
    a, b = TEXT.index("Trả lời ngắn gọn"), TEXT.index("Cuối cùng")
    sel = TEXT[a:b]; assert sel == "Trả lời ngắn gọn và lịch sự. "
    r = rewrite(c, template=TEXT, action="examples", selection={"start": a, "end": b, "text": sel})
    assert r["proposed"][:a] == TEXT[:a] and r["proposed"][r["selection"]["start"] + len(r["replacement"]):] == TEXT[b:]    # prefix + suffix byte-identical
    assert r["selection"] == {"start": a, "end": b} and "Ví dụ" in r["replacement"] and r["replacement"].startswith("Trả lời ngắn gọn") and r["replacement"].endswith(" ")  # edge whitespace kept
    first_change = next(s for s in r["segments"] if s["t"] == "chg"); before = "".join(s["text"] if s["t"] == "eq" else s["old"] for s in r["segments"])
    assert before == TEXT and "Ví dụ" in first_change["new"]
    outside = "".join(s["text"] for s in r["segments"] if s["t"] == "eq"); assert TEXT[:a] in outside and TEXT[b:] in outside   # every change is inside the selection
    # a selection that contains a variable keeps it; whole-prompt "shorter" keeps every sentence with a variable
    v = TEXT.index("Luôn chào"); sel2 = TEXT[v:TEXT.index("Trả lời ngắn")]
    r2 = rewrite(c, template=TEXT, action="shorter", selection={"start": v, "end": v + len(sel2)}); assert "{{ten}}" in r2["replacement"] and r2["variables_ok"]
    assert rewrite(c, template=TEXT, action="shorter")["proposed"].count("{{ten}}") == 1
    # bad selections
    assert c.post("/api/hub/canvas/rewrite", json={"template": TEXT, "action": "shorter", "stream": False, "selection": {"start": 5, "end": 99999}}).status_code == 400
    assert c.post("/api/hub/canvas/rewrite", json={"template": TEXT, "action": "shorter", "stream": False, "selection": {"start": 0, "end": 5, "text": "sai"}}).status_code == 409
    assert rewrite(c, template=TEXT, action="shorter", selection={"start": 7, "end": 7})["selection"] is None      # empty selection = whole prompt

def test_variables_are_validated_retried_and_protected_on_save(env):
    c = env["ed"]
    r = rewrite(c, template=TEXT, action="custom", instruction="làm trang trọng [drop-vars]")     # the fake drops {{ten}} on the first try
    assert r["retried"] is True and r["variables_ok"] and "{{ten}}" in r["proposed"] and r["missing_variables"] == []   # the retry named it and fixed it
    ev = sse(c, template=TEXT, action="custom", instruction="làm trang trọng [drop-vars]")
    assert [e["type"] for e in ev if e["type"] in ("start", "retry", "done")] == ["start", "retry", "done"] and next(e for e in ev if e["type"] == "retry")["missing"] == ["{{ten}}"]
    # a human can still delete the variable in the editor: Save refuses unless confirmed
    gone = TEXT.replace("{{ten}}", "bạn")
    s = c.post("/api/hub/canvas/commit", json={"prompt_id": env["pid"], "base_version": env["v1"], "template": gone}); assert s.status_code == 422, s.text
    assert s.json()["detail"]["missing_variables"] == ["{{ten}}"]
    assert c.post("/api/hub/canvas/apply", json={"a": TEXT, "b": gone, "accept": "all"}).json()["missing_variables"] == ["{{ten}}"]
    s = c.post("/api/hub/canvas/commit", json={"prompt_id": env["pid"], "base_version": env["v1"], "template": gone, "allow_removed_variables": True, "message": "bỏ biến"}); assert s.status_code == 200
    assert s.json()["missing_variables"] == ["{{ten}}"]
    # single-brace variables are protected too
    r = rewrite(c, template="Chào {ho_ten}, hôm nay bạn khoẻ không?", action="custom", instruction="đơn giản hơn [drop-vars]"); assert "{ho_ten}" in r["proposed"] and r["retried"]

def test_every_quick_action_and_streaming(env):
    c = env["ed"]; out = {}
    for act, kw in (("shorter", {}), ("examples", {}), ("tone", {"instruction": "thân thiện"}), ("clearer", {}), ("translate", {}), ("safety", {}), ("feedback", {"feedback": "Trả lời quá dài dòng"}),
                    ("custom", {"instruction": "thêm lời cảm ơn"})):
        out[act] = rewrite(c, template=TEXT, action=act, **kw); assert out[act]["variables_ok"] and out[act]["proposed"] != TEXT, act
    assert out["translate"]["proposed"].startswith("[EN] ")                                          # Vietnamese text -> English
    assert rewrite(c, template="You are a helper for {{x}}.", action="translate")["proposed"].startswith("[VI] ")   # English text -> Vietnamese (VI <-> EN)
    assert rewrite(c, template=TEXT, action="translate", lang="vi")["proposed"].startswith("[VI] ")
    assert "Trả lời quá dài dòng" in out["feedback"]["proposed"] and "thêm lời cảm ơn" in out["custom"]["proposed"]
    assert c.post("/api/hub/canvas/rewrite", json={"template": TEXT, "action": "custom", "stream": False}).status_code == 400   # chat message needed
    assert c.post("/api/hub/canvas/rewrite", json={"template": TEXT, "action": "feedback", "stream": False}).status_code == 400  # no trace / feedback
    # SSE: start -> streamed deltas -> done (same text as the non-stream call)
    ev = sse(c, template=TEXT, action="examples", history=[{"role": "user", "content": "viết lại"}, {"role": "assistant", "content": "ok"}])
    assert ev[0]["type"] == "start" and ev[-1]["type"] == "done" and sum(1 for e in ev if e["type"] == "delta") > 3
    assert "".join(e["text"] for e in ev if e["type"] == "delta").strip() == ev[-1]["replacement"].strip() and ev[-1]["proposed"] == out["examples"]["proposed"]
    ev = sse(c, template=TEXT, action="feedback")   # error inside the stream is an event, not a broken response
    assert ev[-1]["type"] == "error" and ev[-1]["status"] == 400 and "trace" in ev[-1]["message"]

def test_feedback_of_a_trace_drives_the_rewrite(env):
    c = env["ed"]
    run = c.post("/api/playground/lab", json={"messages": [{"role": "human", "content": "cho tôi giá"}]}).json()["results"][0]["run_id"]
    assert c.post("/api/obs/feedback", json={"run_id": run, "key": "correctness", "score": 0, "comment": "Thiếu giá cụ thể trong câu trả lời"}).status_code == 200
    fb = c.get("/api/hub/canvas/feedback").json(); assert any(f["run_id"] == run for f in fb)
    r = rewrite(c, template=TEXT, action="feedback", run_id=run); assert "Thiếu giá cụ thể" in r["proposed"] and r["variables_ok"]
    assert c.post("/api/hub/canvas/rewrite", json={"template": TEXT, "action": "feedback", "run_id": str(uuid.uuid4()), "stream": False}).status_code == 404

def test_canvas_and_playground_calls_carry_their_feature_for_the_caps(env, a):
    with pg() as cn: n0 = {f: cn.execute("SELECT count(*) FROM gateway_usage WHERE workspace_id=%s AND feature=%s", (env["wa"], f)).fetchone()[0] for f in ("prompt_canvas", "playground")}
    rewrite(env["ed"], template=TEXT, action="shorter"); env["ed"].post("/api/playground/lab", json={"messages": [{"role": "human", "content": "xin chào"}]})
    with pg() as cn: n1 = {f: cn.execute("SELECT count(*) FROM gateway_usage WHERE workspace_id=%s AND feature=%s", (env["wa"], f)).fetchone()[0] for f in ("prompt_canvas", "playground")}
    assert n1["prompt_canvas"] > n0["prompt_canvas"] and n1["playground"] > n0["playground"]
    # a feature cap blocks the canvas (402 before any stream starts) and leaves the playground alone
    with pg() as cn: cn.execute("INSERT INTO gateway_usage(ts,model,input_tokens,output_tokens,cost_usd,status,workspace_id,feature) VALUES(now(),'gemini-3.6-flash',0,0,2.0,'t92',%s,'prompt_canvas')", (env["wa"],))
    cap = a.post("/api/gateway/caps", json={"scope": "feature", "target": "prompt_canvas", "period": "day", "limit_usd": 1.0, "action": "block", "workspace_id": env["wa"]})
    try:
        assert cap.status_code == 200, cap.text
        t0 = time.time(); code = 200
        while time.time() - t0 < 20 and code == 200:   # the caps snapshot is cached for a few seconds
            code = env["ed"].post("/api/hub/canvas/rewrite", json={"template": TEXT, "action": "shorter", "stream": False}).status_code
            if code == 200: time.sleep(1.5)
        assert code == 402
        assert env["ed"].post("/api/hub/canvas/rewrite", json={"template": TEXT, "action": "shorter"}).status_code == 402      # streaming variant too
    finally:
        if cap.status_code == 200: a.delete(f"/api/gateway/caps/{cap.json()['id']}")
        with pg() as cn: cn.execute("DELETE FROM gateway_usage WHERE status='t92'")

def test_roles_and_isolation(env):
    assert env["vw"].post("/api/hub/canvas/rewrite", json={"template": TEXT, "action": "shorter", "stream": False}).status_code == 403
    assert env["vw"].post("/api/playground/lab", json={"messages": [{"role": "human", "content": "x"}]}).status_code == 403
    assert env["oth"].post("/api/hub/canvas/commit", json={"prompt_id": env["pid"], "base_version": env["v1"], "template": "x"}).status_code == 404   # other workspace cannot commit to it
    assert env["vw"].get("/api/playground/tools").status_code == 200

# ------------------------------------------------------------------ Playground lab
def lab(c, **kw):
    r = c.post("/api/playground/lab", json=kw); assert r.status_code == 200, r.text; return r.json()

WEATHER = {"name": "get_weather", "description": "Thời tiết một thành phố", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}

def test_tool_call_round_trip_with_a_mock_result(env):
    c = env["ed"]; q = {"role": "human", "content": '[tool:get_weather] {"city": "Huế"}'}
    r = lab(c, messages=[q], tools=[WEATHER])["results"][0]                                  # the model calls the tool; nothing answers it yet
    assert [s["type"] for s in r["steps"]] == ["model", "tool"] and r["steps"][1]["source"] == "pending" and r["steps"][1]["reason"] == "needs_mock"
    pend = r["pending"]; assert len(pend) == 1 and pend[0]["name"] == "get_weather" and pend[0]["args"] == {"city": "Huế"} and r["steps"][1]["args_errors"] == []
    # the user types a mock result; the conversation continues with the model's tool-call message + a tool message
    msgs = [q, *r["appended"], {"role": "tool", "tool_call_id": pend[0]["id"], "name": "get_weather", "content": '{"temp": 24, "mô tả": "nắng nhẹ"}'}]
    r2 = lab(c, messages=msgs, tools=[WEATHER])["results"][0]
    assert r2["pending"] == [] and "get_weather" in r2["output"] and "nắng nhẹ" in r2["output"] and r2["error"] is None
    # or a mock result given up front (per tool name): one request runs model -> tool(mock) -> model
    r3 = lab(c, messages=[q], tools=[WEATHER], mock_results={"get_weather": {"temp": 30}})["results"][0]
    assert [s["type"] for s in r3["steps"]] == ["model", "tool", "model"] and r3["steps"][1]["source"] == "mock" and '"temp": 30' in r3["steps"][1]["result"] and '"temp": 30' in r3["output"]
    assert [m["role"] for m in r3["appended"]] == ["ai", "tool"]
    # arguments that break the tool's JSON schema are reported
    bad = lab(c, messages=[{"role": "human", "content": "[tool:get_weather] {\"thanh_pho\": 1}"}], tools=[WEATHER])["results"][0]["steps"][1]
    assert bad["args_errors"] and "city" in bad["args_errors"][0]["message"]
    # invalid inline tool definition
    r = c.post("/api/playground/lab", json={"messages": [q], "tools": [{"name": "x", "parameters": {"type": "wrong"}}]}); assert r.status_code == 400
    assert c.post("/api/playground/lab", json={"messages": [q], "tools": [{"description": "no name"}]}).status_code == 400

def test_catalogue_tools_real_readonly_and_writes_never_run(env):
    c = env["ed"]; cat = {t["key"]: t for t in c.get("/api/playground/tools").json()}
    assert cat["aos:get_costs"]["read_only"] is True and cat["aos:run_experiment"]["read_only"] is False and cat["aos:get_costs"]["name"] == "get_costs"
    ask = {"role": "human", "content": '[tool:get_costs] {"period": "today", "group_by": "feature"}'}
    real = lab(c, messages=[ask], tools=[{"key": "aos:get_costs"}], tools_run="real")["results"][0]
    st = real["steps"][1]; assert st["source"] == "real" and st["result"] and real["pending"] == [] and real["output"].startswith("Trợ lý đã dùng get_costs")
    mock_only = lab(c, messages=[ask], tools=[{"key": "aos:get_costs"}])["results"][0]          # default tools_run=mock: a real tool is NOT run
    assert mock_only["steps"][1]["source"] == "pending"
    w = lab(c, messages=[{"role": "human", "content": '[tool:run_experiment] {"dataset_id": 1}'}], tools=[{"key": "aos:run_experiment"}], tools_run="real")["results"][0]
    assert w["steps"][1]["source"] == "pending" and w["steps"][1]["reason"] == "needs_approval" and w["pending"][0]["reason"] == "needs_approval"   # a write tool is never executed here
    wm = lab(c, messages=[{"role": "human", "content": '[tool:run_experiment] {"dataset_id": 1}'}], tools=[{"key": "aos:run_experiment"}], tools_run="real", mock_results={"run_experiment": "giả"})["results"][0]
    assert wm["steps"][1]["source"] == "mock"                                                      # ...but it can be answered with a mock
    assert c.post("/api/playground/lab", json={"messages": [{"role": "human", "content": "x"}], "tools": [{"key": "aos:khong-co"}]}).status_code == 404

SCHEMA = {"type": "object", "properties": {"ten": {"type": "string"}, "tuoi": {"type": "integer", "minimum": 0}}, "required": ["ten", "tuoi"], "additionalProperties": False}

def test_structured_output_validates_against_the_schema(env):
    c = env["ed"]
    ok = lab(c, messages=[{"role": "human", "content": '[json] {"ten": "An", "tuoi": 30}'}], structured={"mode": "schema", "schema": SCHEMA})["results"][0]
    assert ok["structured"]["valid"] is True and ok["structured"]["parsed"] == {"ten": "An", "tuoi": 30} and ok["structured"]["errors"] == [] and ok["structured"]["mode"] == "schema"
    gen = lab(c, messages=[{"role": "human", "content": "cho tôi một người"}], structured={"mode": "schema", "schema": SCHEMA})["results"][0]      # sample generated from the schema
    assert gen["structured"]["valid"] is True and set(gen["structured"]["parsed"]) == {"ten", "tuoi"}
    bad = lab(c, messages=[{"role": "human", "content": "[json-invalid] x"}], structured={"mode": "schema", "schema": SCHEMA})["results"][0]["structured"]
    assert bad["valid"] is False and {e["path"] for e in bad["errors"]} >= {"(root)"} and any("required" in e["message"] or "additional" in e["message"] for e in bad["errors"])
    wrong = lab(c, messages=[{"role": "human", "content": '[json] {"ten": "An", "tuoi": -3}'}], structured={"mode": "schema", "schema": SCHEMA})["results"][0]["structured"]
    assert wrong["valid"] is False and wrong["errors"][0]["path"] == "/tuoi" and ("minimum" in wrong["errors"][0]["message"] or "less than" in wrong["errors"][0]["message"])
    # provider JSON mode: text -> parsed -> same validation
    j = lab(c, messages=[{"role": "human", "content": '[json] {"ten": "Bình", "tuoi": 7}'}], structured={"mode": "json", "schema": SCHEMA})["results"][0]["structured"]
    assert j["valid"] and j["mode"] == "json" and j["parsed"]["ten"] == "Bình"
    jb = lab(c, messages=[{"role": "human", "content": "[json-invalid] x"}], structured={"mode": "json", "schema": SCHEMA})["results"][0]["structured"]
    assert jb["valid"] is False and "not valid JSON" in jb["errors"][0]["message"] and jb["parsed"] is None
    assert c.post("/api/playground/lab", json={"messages": [{"role": "human", "content": "x"}], "structured": {"schema": {"type": "nope"}}}).status_code == 400   # bad schema
    assert c.post("/api/playground/lab", json={"messages": [{"role": "human", "content": "x"}], "structured": {"schema": SCHEMA}, "tools": [WEATHER]}).status_code == 400   # not both

def test_images_in_the_human_message(env):
    c = env["ed"]; part = {"type": "image", "base64": PNG, "mime_type": "image/png"}
    r = lab(c, messages=[{"role": "human", "content": [{"type": "text", "text": "mô tả ảnh {{x}}"}, part]}], variables={"x": "này"})
    assert r["images"] == 1 and "[I received 1 image(s): image/png" in r["results"][0]["output"] and "mô tả ảnh này" in r["results"][0]["output"]       # variables rendered in the text part
    r = lab(c, messages=[{"role": "human", "content": [{"type": "text", "text": "ảnh"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64," + PNG}}, part]}])   # OpenAI-style data URL too
    assert r["images"] == 2 and "2 image(s)" in r["results"][0]["output"]
    bad = lambda p: c.post("/api/playground/lab", json={"messages": [{"role": "human", "content": [p]}]}).status_code
    assert bad({"type": "image", "base64": "!!!", "mime_type": "image/png"}) == 400                                    # not base64
    assert bad({"type": "image", "base64": base64.b64encode(b"hello world, not a png").decode(), "mime_type": "image/png"}) == 400   # content != declared type
    assert bad({"type": "image", "base64": PNG, "mime_type": "image/svg+xml"}) == 400
    assert bad({"type": "image_url", "image_url": {"url": "http://127.0.0.1:9/x.png"}}) == 400                         # remote URLs are never fetched
    assert bad({"type": "image", "base64": base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * (5 * 1024 * 1024)).decode(), "mime_type": "image/png"}) == 413
    assert c.post("/api/playground/lab", json={"messages": [{"role": "human", "content": [part] * 9}]}).status_code == 400
    # the trace keeps a size marker, never the picture
    with pg() as cn: stored = cn.execute("SELECT inputs::text FROM tr_runs WHERE id=%s", (r["results"][0]["run_id"],)).fetchone()[0]
    assert PNG not in stored and "[image image/png" in stored

def test_presets_save_load_compare_and_every_run_is_traced(env):
    c = env["ed"]
    p = c.post("/api/model_presets", json={"name": "nhanh-re", "model": "gemini-3.1-flash-lite", "temperature": 0.1, "max_tokens": 512, "top_p": 0.9, "reasoning_effort": "low"}); assert p.status_code == 200, p.text
    assert p.json()["reasoning_effort"] == "low"
    c.post("/api/model_presets", json={"name": "sang-tao", "model": "gemini-3.6-flash", "temperature": 1.2, "reasoning_effort": "high"})
    assert {x["name"] for x in c.get("/api/model_presets").json()} == {"nhanh-re", "sang-tao"}
    assert c.post("/api/model_presets", json={"name": "bad", "model": "nosuchprov/some-model", "temperature": 0.1}).status_code == 400   # unknown provider is refused (11.3); bare gpt-5 now resolves to the built-in OpenAI provider -> 200
    assert c.post("/api/model_presets", json={"name": "bad", "model": "gemini-3.6-flash", "reasoning_effort": "max"}).status_code == 422
    assert env["oth"].get("/api/model_presets").json() == []                                         # per workspace
    assert env["oth"].post("/api/playground/lab", json={"messages": [{"role": "human", "content": "x"}], "configs": [{"preset": "nhanh-re"}]}).status_code == 404
    # load = pick it in a run; compare 2 configs side by side
    r = lab(c, messages=[{"role": "human", "content": "hai cấu hình song song"}], configs=[{"preset": "nhanh-re"}, {"preset": "sang-tao", "temperature": 0.5, "label": "Sáng tạo (0.5)"}])
    a_, b_ = r["results"]; assert (a_["model"], a_["preset"]) == ("gemini-3.1-flash-lite", "nhanh-re") and (b_["model"], b_["label"]) == ("gemini-3.6-flash", "Sáng tạo (0.5)")
    assert a_["config"] == {"model": "gemini-3.1-flash-lite", "temperature": 0.1, "max_tokens": 512, "top_p": 0.9, "reasoning_effort": "low"} and b_["config"]["temperature"] == 0.5 and b_["config"]["reasoning_effort"] == "high"
    assert a_["output"] == b_["output"] == "Echo: hai cấu hình song song" and a_["run_id"] != b_["run_id"]
    assert len(lab(c, messages=[{"role": "human", "content": "x"}], configs=[{"model": "gemini-3.6-flash"}] * 4)["results"]) == 4
    assert c.post("/api/playground/lab", json={"messages": [{"role": "human", "content": "x"}], "configs": [{}] * 5}).status_code == 422
    # a saved preset's settings reach the model call (reasoning effort / max tokens via gateway.tune)
    import gateway  # noqa: F401
    # every run is traced: a chain run per config + llm child, tagged with the preset
    with pg() as cn:
        for res in (a_, b_):
            root = cn.execute("SELECT name, run_type, tags, model FROM tr_runs WHERE id=%s", (res["run_id"],)).fetchone()
            assert root[1] == "chain" and "playground" in root[2] and "lab" in root[2] and root[0].startswith("playground lab")
            assert cn.execute("SELECT count(*) FROM tr_runs WHERE parent_id=%s AND run_type='llm'", (res["run_id"],)).fetchone()[0] == 1
        assert f"preset:nhanh-re" in cn.execute("SELECT tags FROM tr_runs WHERE id=%s", (a_["run_id"],)).fetchone()[0]
    # tool runs are traced as children too
    t = lab(c, messages=[{"role": "human", "content": '[tool:get_weather] {"city": "Huế"}'}], tools=[WEATHER], mock_results={"get_weather": "mưa"})["results"][0]
    with pg() as cn: kinds = sorted(x[0] for x in cn.execute("SELECT run_type FROM tr_runs WHERE parent_id=%s", (t["run_id"],)).fetchall())
    assert kinds == ["llm", "llm", "tool"]
    assert next(x for x in c.get("/api/model_presets").json() if x["name"] == "nhanh-re")["id"]
    for x in c.get("/api/model_presets").json(): assert c.delete(f"/api/model_presets/{x['id']}").status_code == 200
    assert c.get("/api/model_presets").json() == []

def test_preset_settings_are_applied_by_the_gateway_call(monkeypatch):
    """The effective config of a run = preset values overridden by inline ones, with the right fallback chain (single run: fallbacks on; compare: off)."""
    import deepagent_api  # noqa: F401
    import prompt_lab as P
    async def fake_q(sql, params=(), one=False): return {"name": "p", "model": "gemini-3.1-flash-lite", "temperature": 0.1, "max_tokens": 512, "top_p": 0.9, "fallback_models": ["gemini-3-flash-preview"], "reasoning_effort": "low"}
    monkeypatch.setattr(P.A, "q", fake_q)
    e = asyncio.run(P._effective(P.Cfg(preset="p", temperature=0.7), multi=False))
    assert e["model"] == "gemini-3.1-flash-lite" and e["temperature"] == 0.7 and e["params"] == {"max_tokens": 512, "top_p": 0.9, "reasoning_effort": "low"}
    assert e["chain"][:2] == ["gemini-3.1-flash-lite", "gemini-3-flash-preview"] and len(e["chain"]) > 2
    assert asyncio.run(P._effective(P.Cfg(preset="p"), multi=True))["chain"] == ["gemini-3.1-flash-lite"]          # fair comparison: no fallback
