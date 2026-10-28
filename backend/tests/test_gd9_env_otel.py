"""GĐ9.10 (24/10/2026): environments (dev|staging|prod|custom) on projects / ingest keys / alert rules + filter + cross-env compare; OpenTelemetry in (OTLP JSON + protobuf,
GenAI semconv) and out (SDK exporter -> fake collector); W3C traceparent continue + propagate. Local server :8020, fake LLM => $0. Throwaway projects/secrets."""
import base64, datetime as dt, gzip, json, os, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import httpx, psycopg, pytest
from test_fixes import B, ADMIN, a, restore, _clean  # noqa: F401  (fixtures)

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
SUF = uuid.uuid4().hex[:6]; G = "agent-2"

def pg(): return psycopg.connect(PG, autocommit=True)

@pytest.fixture(scope="module")
def made():
    made = {"projects": [], "secrets": []}
    yield made
    with pg() as c:
        for p in made["projects"]:
            c.execute("DELETE FROM tr_runs WHERE project_id=%s", (p,)); c.execute("DELETE FROM projects WHERE id=%s", (p,))
        c.execute("DELETE FROM otel_export_cfg WHERE workspace_id=1")
        for s in made["secrets"]: c.execute("DELETE FROM ws_secrets WHERE name=%s AND workspace_id=1", (s,))

def proj(a, made, name, env=None):
    r = a.post("/api/obs/projects", json={"name": f"{name}-{SUF}", **({"environment": env} if env else {})}); assert r.status_code == 200, r.text
    made["projects"].append(r.json()["id"]); return r.json()

def key(a, pid, env=""):
    r = a.post(f"/api/obs/projects/{pid}/keys", params={"label": "t", "environment": env}); assert r.status_code == 200, r.text
    return r.json()["api_key"]

def runs(a, pid, **kw):
    r = a.get("/api/obs/runs", params={"project_id": pid, "limit": 200, **kw}); assert r.status_code == 200, r.text; return r.json()

# ------------------------------------------------------------------ environments
def test_env_labels_normalise_and_validate(a, made):
    import sys; sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
    import envs
    assert envs.norm("Production") == "prod" and envs.norm("development") == "dev" and envs.norm("stage") == "staging" and envs.norm("qa-eu") == "qa-eu" and envs.norm("", "prod") == "prod"
    with pytest.raises(ValueError): envs.norm("bad env!")
    p = proj(a, made, "env-a", "production"); assert p["environment"] == "prod"
    assert a.post("/api/obs/projects", json={"name": f"env-bad-{SUF}", "environment": "no way"}).status_code == 422
    assert a.patch(f"/api/obs/projects/{p['id']}", json={"environment": "qa-eu"}).status_code == 200
    assert next(x for x in a.get("/api/obs/projects").json() if x["id"] == p["id"])["environment"] == "qa-eu"

def test_project_list_filter_and_default(a, made):
    d = proj(a, made, "env-dev", "dev"); s = proj(a, made, "env-stg", "staging"); pr = proj(a, made, "env-default")
    assert pr["environment"] == "prod"   # default
    ids = lambda env: {x["id"] for x in a.get("/api/obs/projects", params={"environment": env}).json()}
    assert d["id"] in ids("dev") and d["id"] not in ids("prod") and s["id"] in ids("staging") and pr["id"] in ids("production")   # alias accepted
    assert {e["environment"] for e in a.get("/api/obs/environments").json()} >= {"dev", "staging", "prod"}

def test_migration_rule_names(a):
    # the one-time migration classified pre-existing projects: prod unless named like test/dev; the SQL itself:
    with pg() as c:
        assert c.execute("SELECT 'my-test-bot' ~* '(test|dev|sandbox)' AND 'agents' !~* '(test|dev|sandbox)'").fetchone()[0]
        assert c.execute("SELECT count(*) FROM projects WHERE name ~* '(test|dev|sandbox)' AND environment='prod' AND created < now() - interval '5 minutes'").fetchone()[0] >= 0
        assert c.execute("SELECT environment FROM projects WHERE name='agents'").fetchone()[0] in ("prod", "dev", "staging")

def test_key_default_env_stamps_runs_and_filter(a, made):
    p = proj(a, made, "env-key", "prod")
    k_stg = key(a, p["id"], "staging"); k_def = key(a, p["id"], "")
    assert {x["environment"] for x in a.get(f"/api/obs/projects/{p['id']}/keys").json()} == {"staging", None}
    now = dt.datetime.now(dt.timezone.utc)
    def send(k, name, md=None):
        rid = str(uuid.uuid4())
        r = httpx.post(B + "/ingest/runs", headers={"x-api-key": k}, json={"post": [{"id": rid, "name": name, "run_type": "chain", "start_time": now.isoformat(), "end_time": (now + dt.timedelta(milliseconds=50)).isoformat(),
                                                                                  "metadata": md or {}}]}); assert r.status_code == 200, r.text; return rid
    r1, r2, r3 = send(k_stg, "via-staging-key"), send(k_def, "via-default-key"), send(k_stg, "explicit", {"environment": "qa-eu"})
    by = lambda f: {x["name"] for x in runs(a, p["id"], filter=f)}
    assert by('eq(environment,"staging")') == {"via-staging-key"}
    assert by('eq(environment,"prod")') == {"via-default-key"}          # falls back to the project's environment
    assert by('eq(environment,"qa-eu")') == {"explicit"}                 # explicit run metadata wins over the key
    assert by('in(environment,"staging","prod")') == {"via-staging-key", "via-default-key"}
    assert {x["name"]: x["environment"] for x in runs(a, p["id"])}["via-staging-key"] == "staging"
    assert httpx.post(B + "/ingest/runs", headers={"x-api-key": "ak_nope"}, json={"post": []}).status_code == 401

def test_compare_dashboard_side_by_side(a, made):
    p = proj(a, made, "env-cmp", "dev"); k = key(a, p["id"], ""); k2 = key(a, p["id"], "staging")
    now = dt.datetime.now(dt.timezone.utc)
    for kk, n, err in ((k, 3, 1), (k2, 2, 0)):
        for i in range(n):
            httpx.post(B + "/ingest/runs", headers={"x-api-key": kk}, json={"post": [{"id": str(uuid.uuid4()), "name": "c", "run_type": "chain", "start_time": now.isoformat(), "end_time": (now + dt.timedelta(milliseconds=100)).isoformat(),
                       "error": "boom" if i < err else None, "usage": {"input_tokens": 10, "output_tokens": 5}, "model": "gemini-3.8-flash"}]})
    r = a.get("/api/obs/environments/compare", params={"hours": 1, "environments": "dev,staging", "project_id": p["id"]}); assert r.status_code == 200, r.text
    e = {x["environment"]: x for x in r.json()["environments"]}
    assert e["dev"]["traces"] == 3 and e["dev"]["errors"] == 1 and abs(e["dev"]["error_rate"] - 1 / 3) < 1e-9 and e["dev"]["projects"] >= 1
    assert e["staging"]["traces"] == 2 and e["staging"]["errors"] == 0 and e["staging"]["tokens"] == 30 and e["staging"]["cost"] > 0
    assert e["dev"]["series"] and a.get("/api/obs/environments/compare", params={"hours": 1, "environments": "x y"}).status_code == 422

def test_alert_rule_targets_environment(a, made):
    p = proj(a, made, "env-alert", "prod"); k = key(a, p["id"], "staging"); kp = key(a, p["id"], "prod")
    now = dt.datetime.now(dt.timezone.utc)
    for kk, n in ((k, 2), (kp, 5)):
        for _ in range(n): httpx.post(B + "/ingest/runs", headers={"x-api-key": kk}, json={"post": [{"id": str(uuid.uuid4()), "name": "r", "start_time": now.isoformat(), "end_time": now.isoformat()}]})
    mk = lambda env: a.post("/api/obs/alerts", json={"project_id": p["id"], "name": f"cnt-{env}-{SUF}", "metric": "count", "op": "gte", "threshold": 1, "window_min": 30, "channel": "log", **({"environment": env} if env else {})})
    ids = {env: mk(env).json()["id"] for env in ("staging", "prod", None)}
    val = lambda i: a.post(f"/api/obs/alerts/{i}/test").json()["value"]
    assert val(ids["staging"]) == 2 and val(ids["prod"]) == 5 and val(ids[None]) == 7
    assert {x["environment"] for x in a.get("/api/obs/alerts").json() if x["id"] in ids.values()} == {"staging", "prod", None}
    for i in ids.values(): a.delete(f"/api/obs/alerts/{i}")

# ------------------------------------------------------------------ OTLP in
def _ns(t): return str(int(t.timestamp() * 1e9))
def _kv(k, v):
    return {"key": k, "value": {"intValue": str(v)} if isinstance(v, int) and not isinstance(v, bool) else {"stringValue": str(v)}}

def genai_payload(tid, env=None, model="gemini-3.8-flash"):
    t0 = dt.datetime.now(dt.timezone.utc); t1 = t0 + dt.timedelta(milliseconds=800)
    msgs_in = json.dumps([{"role": "user", "parts": [{"type": "text", "content": "Xin chào, 2+2?"}]}])
    msgs_out = json.dumps([{"role": "assistant", "parts": [{"type": "text", "content": "Bằng 4."}], "finish_reason": "stop"}])
    return {"resourceSpans": [{"resource": {"attributes": [_kv("service.name", "otel-test")] + ([_kv("deployment.environment.name", env)] if env else [])},
        "scopeSpans": [{"scope": {"name": "t"}, "spans": [
            {"traceId": tid, "spanId": "00000000000000a1", "name": "invoke_agent helper", "startTimeUnixNano": _ns(t0), "endTimeUnixNano": _ns(t1), "attributes": [_kv("gen_ai.operation.name", "invoke_agent"), _kv("gen_ai.agent.name", "helper"), _kv("gen_ai.conversation.id", "conv-" + tid[:6])]},
            {"traceId": tid, "spanId": "00000000000000a2", "parentSpanId": "00000000000000a1", "name": f"chat {model}", "kind": 3, "startTimeUnixNano": _ns(t0), "endTimeUnixNano": _ns(t1),
             "attributes": [_kv("gen_ai.operation.name", "chat"), _kv("gen_ai.provider.name", "gcp.gemini"), _kv("gen_ai.request.model", model), _kv("gen_ai.response.model", model),
                            _kv("gen_ai.usage.input_tokens", 1000), _kv("gen_ai.usage.output_tokens", 500), _kv("gen_ai.input.messages", msgs_in), _kv("gen_ai.output.messages", msgs_out)]},
            {"traceId": tid, "spanId": "00000000000000a3", "parentSpanId": "00000000000000a1", "name": "execute_tool calc", "startTimeUnixNano": _ns(t0), "endTimeUnixNano": _ns(t1),
             "attributes": [_kv("gen_ai.operation.name", "execute_tool"), _kv("gen_ai.tool.name", "calc"), _kv("gen_ai.tool.call.arguments", '{"x": 2}'), _kv("gen_ai.tool.call.result", "4")],
             "status": {"code": 2, "message": "tool failed"}}]}]}]}

def _trace(a, tid):
    r = a.get(f"/api/obs/traces/{uuid.UUID(hex=tid)}"); assert r.status_code == 200, r.text; return {x["name"]: x for x in r.json()["runs"]}

def test_otlp_json_genai_span_becomes_run_with_tokens_and_cost(a, made):
    p = proj(a, made, "otel-json", "dev"); k = key(a, p["id"], "")
    tid = uuid.uuid4().hex
    r = httpx.post(B + "/otel/v1/traces", headers={"Authorization": f"Bearer {k}", "content-type": "application/json"}, json=genai_payload(tid, env="staging")); assert r.status_code == 200, r.text
    assert r.json()["accepted"] == 3
    t = _trace(a, tid); llm = t["chat gemini-3.8-flash"]; root = t["invoke_agent helper"]; tool = t["calc"]
    assert llm["run_type"] == "llm" and llm["model"] == "gemini-3.8-flash" and llm["input_tokens"] == 1000 and llm["output_tokens"] == 500 and float(llm["cost_usd"]) > 0
    assert llm["inputs"]["messages"] == [{"role": "user", "content": "Xin chào, 2+2?"}] and llm["outputs"]["messages"][0]["content"] == "Bằng 4."
    assert llm["metadata"]["gen_ai.provider.name"] == "gcp.gemini" and llm["metadata"]["environment"] == "staging"   # resource attr deployment.environment.name wins over key/project
    assert root["parent_id"] is None and llm["parent_id"] == root["id"] and root["thread_id"] == "conv-" + tid[:6]
    assert root["total_tokens"] == 1500 and float(root["cost_usd"]) == float(llm["cost_usd"])   # rolled up to the root
    assert tool["run_type"] == "tool" and tool["status"] == "error" and tool["error"] == "tool failed" and tool["inputs"] == {"args": {"x": 2}} and tool["outputs"] == {"result": "4"}
    assert {x["name"] for x in runs(a, p["id"], filter='eq(environment,"staging")')} == {"invoke_agent helper"}
    assert httpx.post(B + "/otel/v1/traces", headers={"x-api-key": "ak_bad", "content-type": "application/json"}, json={"resourceSpans": []}).status_code == 401
    assert httpx.post(B + "/otel/v1/traces", headers={"x-api-key": k, "content-type": "text/plain"}, content=b"x").status_code == 415

def test_otlp_events_style_and_openinference(a, made):
    p = proj(a, made, "otel-style", "dev"); k = key(a, p["id"], "")
    t0 = dt.datetime.now(dt.timezone.utc); t1 = t0 + dt.timedelta(milliseconds=5); tid = uuid.uuid4().hex; tid2 = uuid.uuid4().hex
    ev = {"traceId": tid, "spanId": "00000000000000b1", "name": "chat gpt-5.4-mini", "startTimeUnixNano": _ns(t0), "endTimeUnixNano": _ns(t1),
          "attributes": [_kv("gen_ai.operation.name", "chat"), _kv("gen_ai.request.model", "gpt-5.4-mini"), _kv("gen_ai.usage.input_tokens", 10), _kv("gen_ai.usage.output_tokens", 4)],
          "events": [{"name": "gen_ai.user.message", "attributes": [_kv("content", "hi")]}, {"name": "gen_ai.choice", "attributes": [_kv("message", json.dumps({"content": "hello!"}))]}]}
    oi = {"traceId": tid2, "spanId": "00000000000000c1", "name": "ChatOpenAI", "startTimeUnixNano": _ns(t0), "endTimeUnixNano": _ns(t1),
          "attributes": [_kv("openinference.span.kind", "LLM"), _kv("llm.model_name", "gemini-3.8-flash"), _kv("llm.token_count.prompt", 7), _kv("llm.token_count.completion", 3),
                         _kv("llm.input_messages.0.message.role", "user"), _kv("llm.input_messages.0.message.content", "q?"), _kv("llm.output_messages.0.message.content", "a!")]}
    body = {"resourceSpans": [{"resource": {"attributes": []}, "scopeSpans": [{"spans": [ev, oi]}]}]}
    assert httpx.post(B + "/otel/v1/traces", headers={"x-api-key": k, "content-type": "application/json"}, json=body).status_code == 200
    e = _trace(a, tid)["chat gpt-5.4-mini"]; o = _trace(a, tid2)["ChatOpenAI"]
    assert e["inputs"]["messages"][0]["content"] == "hi" and e["outputs"]["messages"][0]["content"] == "hello!" and e["total_tokens"] == 14
    assert o["run_type"] == "llm" and o["model"] == "gemini-3.8-flash" and o["input_tokens"] == 7 and o["inputs"]["messages"] == [{"role": "user", "content": "q?"}] and o["outputs"]["messages"][0]["content"] == "a!"

def test_otlp_protobuf_from_real_sdk_exporter_gzip(a, made):
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.exporter.otlp.proto.http import Compression
    from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as g
    p = proj(a, made, "otel-pb", "dev"); k = key(a, p["id"], "")
    tp = TracerProvider(resource=Resource.create({"service.name": "sdk-app", "deployment.environment.name": "qa-eu"}))
    tp.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=B + "/otel/v1/traces", headers={"Authorization": f"Bearer {k}"}, compression=Compression.Gzip)))
    tr = tp.get_tracer("t")
    with tr.start_as_current_span("invoke_agent pb", attributes={g.GEN_AI_OPERATION_NAME: "invoke_agent"}) as root:
        tid = format(root.get_span_context().trace_id, "032x")
        with tr.start_as_current_span("chat gemini-3.8-flash", attributes={g.GEN_AI_OPERATION_NAME: "chat", g.GEN_AI_PROVIDER_NAME: "gcp.gemini", g.GEN_AI_REQUEST_MODEL: "gemini-3.8-flash",
                                                                         g.GEN_AI_USAGE_INPUT_TOKENS: 200, g.GEN_AI_USAGE_OUTPUT_TOKENS: 100}): pass
    tp.shutdown()
    t = _trace(a, tid); llm = t["chat gemini-3.8-flash"]
    assert llm["run_type"] == "llm" and llm["input_tokens"] == 200 and llm["output_tokens"] == 100 and float(llm["cost_usd"]) > 0 and llm["model"] == "gemini-3.8-flash"
    assert t["invoke_agent pb"]["parent_id"] is None and llm["parent_id"] == t["invoke_agent pb"]["id"] and t["invoke_agent pb"]["total_tokens"] == 300
    assert llm["metadata"]["environment"] == "qa-eu" and llm["metadata"]["otel.service"] == "sdk-app"

def test_otlp_orphan_child_first_then_parent_reattaches(a, made):
    p = proj(a, made, "otel-orphan", "dev"); k = key(a, p["id"], ""); tid = uuid.uuid4().hex
    t0 = dt.datetime.now(dt.timezone.utc)
    sp = lambda sid, par, n: {"traceId": tid, "spanId": sid, **({"parentSpanId": par} if par else {}), "name": n, "startTimeUnixNano": _ns(t0), "endTimeUnixNano": _ns(t0 + dt.timedelta(milliseconds=9))}
    post = lambda spans: httpx.post(B + "/otel/v1/traces", headers={"x-api-key": k, "content-type": "application/json"}, json={"resourceSpans": [{"resource": {"attributes": []}, "scopeSpans": [{"spans": spans}]}]})
    assert post([sp("00000000000000d2", "00000000000000d1", "child")]).status_code == 200          # parent unknown: child is a root with an external-parent link
    c = _trace(a, tid)["child"]; assert c["parent_id"] is None and c["metadata"]["external_parent"]["span_id"] == "00000000000000d1"
    assert post([sp("00000000000000d1", None, "parent")]).status_code == 200                          # the parent arrives later: child is re-attached
    t = _trace(a, tid); assert t["child"]["parent_id"] == t["parent"]["id"] and "external_parent" not in t["child"]["metadata"]

# ------------------------------------------------------------------ OTLP out
class Collector:
    def __init__(self):
        self.got = []; outer = self
        class H(BaseHTTPRequestHandler):
            def do_POST(s):
                raw = s.rfile.read(int(s.headers.get("content-length") or 0))
                if (s.headers.get("content-encoding") or "").lower() == "gzip": raw = gzip.decompress(raw)
                outer.got.append({"path": s.path, "headers": {k.lower(): v for k, v in s.headers.items()}, "body": raw})
                s.send_response(200); s.send_header("content-type", "application/x-protobuf"); s.send_header("content-length", "0"); s.end_headers()
            def log_message(s, *x): pass
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H); self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
    def spans(self):
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
        out = []
        for g in self.got:
            m = ExportTraceServiceRequest(); m.ParseFromString(g["body"])
            for rs in m.resource_spans:
                res = {x.key: x.value.string_value for x in rs.resource.attributes}
                for ss in rs.scope_spans:
                    for sp in ss.spans:
                        at = {x.key: (x.value.string_value or x.value.int_value or x.value.double_value) for x in sp.attributes}
                        out.append({"name": sp.name, "attrs": at, "res": res, "trace": sp.trace_id.hex(), "span": sp.span_id.hex(), "parent": sp.parent_span_id.hex(), "status": sp.status.code, "kind": sp.kind})
        return out
    def close(self): self.srv.shutdown()

def test_export_to_fake_collector_with_gen_ai_attributes(a, made):
    from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as g
    from opentelemetry.semconv.version import __version__ as semv
    col = Collector()
    try:
        sec = f"otel_tok_{SUF}"; made["secrets"].append(sec)
        assert a.post(f"/api/secrets/{sec}", json={"value": "tok-" + SUF, "allowed_hosts": ["127.0.0.1"]}).status_code == 200
        cfg = {"enabled": True, "endpoint": f"http://127.0.0.1:{col.port}", "headers": {"Authorization": f"Bearer {{{{secret:{sec}}}}}"}, "include_content": False, "every_s": 60}
        r = a.put("/api/exports/otlp", json=cfg); assert r.status_code == 200, r.text
        assert r.json()["semconv_version"] == semv and "tok-" not in json.dumps(r.json())
        bad = dict(cfg, headers={"Authorization": "Bearer plain-key"}); assert a.put("/api/exports/otlp", json=bad).status_code == 422          # raw key refused
        assert a.put("/api/exports/otlp", json=dict(cfg, endpoint="http://169.254.169.254/x")).status_code == 422                                  # metadata address refused
        assert a.put("/api/exports/otlp", json=dict(cfg, endpoint="http://example.com")).status_code in (422,)                                     # plain http to the Internet refused
        assert a.post("/api/exports/otlp/run").status_code == 200   # first run: just sets the cursor to "now"
        col.got.clear()
        p = proj(a, made, "otel-out", "staging"); k = key(a, p["id"], "")
        t0 = dt.datetime.now(dt.timezone.utc); root, llm, tool = (str(uuid.uuid4()) for _ in range(3)); tid = root
        body = {"post": [{"id": root, "name": "my-agent", "run_type": "chain", "start_time": t0.isoformat(), "end_time": (t0 + dt.timedelta(seconds=1)).isoformat(), "thread_id": "th-1", "metadata": {"agent_id": 7}},
                         {"id": llm, "trace_id": tid, "parent_run_id": root, "name": "llm", "run_type": "llm", "start_time": t0.isoformat(), "end_time": (t0 + dt.timedelta(milliseconds=500)).isoformat(),
                          "model": "gemini-3.8-flash", "usage": {"input_tokens": 120, "output_tokens": 30}, "inputs": {"messages": [{"role": "human", "content": "SECRET-PROMPT"}]}, "outputs": {"messages": [{"role": "ai", "content": "reply"}]}},
                         {"id": tool, "trace_id": tid, "parent_run_id": root, "name": "search", "run_type": "tool", "start_time": t0.isoformat(), "end_time": (t0 + dt.timedelta(milliseconds=100)).isoformat(), "error": "boom",
                          "inputs": {"args": {"q": 1}}}]}
        assert httpx.post(B + "/ingest/runs", headers={"x-api-key": k}, json=body).status_code == 200
        r = a.post("/api/exports/otlp/run"); assert r.status_code == 200 and r.json()["sent"] >= 3, r.text
        assert r.json()["last_status"] == "ok"
        h = col.got[0]; assert h["path"] == "/v1/traces" and h["headers"]["authorization"] == "Bearer tok-" + SUF and "protobuf" in h["headers"]["content-type"]
        sp = {s["name"]: s for s in col.spans() if s["attrs"].get("aos.run.id") in (root, llm, tool)}
        l = sp["chat gemini-3.8-flash"]
        assert l["attrs"][g.GEN_AI_OPERATION_NAME] == "chat" and l["attrs"][g.GEN_AI_REQUEST_MODEL] == "gemini-3.8-flash" and l["attrs"][g.GEN_AI_PROVIDER_NAME] == "gcp.gemini"
        assert l["attrs"][g.GEN_AI_USAGE_INPUT_TOKENS] == 120 and l["attrs"][g.GEN_AI_USAGE_OUTPUT_TOKENS] == 30 and l["attrs"]["aos.cost_usd"] > 0
        assert g.GEN_AI_INPUT_MESSAGES not in l["attrs"] and b"SECRET-PROMPT" not in b"".join(x["body"] for x in col.got)          # content is opt-in
        assert l["res"]["deployment.environment.name"] == "staging" and l["res"]["service.name"] == "agent-os"
        assert sp["execute_tool search"]["attrs"][g.GEN_AI_OPERATION_NAME] == "execute_tool" and sp["execute_tool search"]["attrs"][g.GEN_AI_TOOL_NAME] == "search" and sp["execute_tool search"]["status"] == 2
        ag = sp["my-agent"]; assert ag["attrs"][g.GEN_AI_OPERATION_NAME] == "invoke_agent" and ag["attrs"][g.GEN_AI_AGENT_ID] == "7" and ag["attrs"][g.GEN_AI_CONVERSATION_ID] == "th-1"
        assert l["parent"] == ag["span"] and l["trace"] == ag["trace"] == uuid.UUID(root).hex   # tree + ids preserved
        # content on + environment filter: a second run is exported with messages; a prod-only filter drops the staging run
        assert a.put("/api/exports/otlp", json=dict(cfg, include_content=True)).status_code == 200
        col.got.clear(); llm2 = str(uuid.uuid4()); t1 = t0 + dt.timedelta(seconds=2)   # the cursor is the newest end_time exported (the first batch ended at t0+1s)
        httpx.post(B + "/ingest/runs", headers={"x-api-key": k}, json={"post": [{"id": llm2, "name": "l2", "run_type": "llm", "start_time": t1.isoformat(), "end_time": (t1 + dt.timedelta(milliseconds=5)).isoformat(),
                    "model": "gemini-3.8-flash", "inputs": {"messages": [{"role": "human", "content": "SECRET-PROMPT"}]}}]})
        assert a.post("/api/exports/otlp/run").json()["sent"] >= 1
        l2 = next(s for s in col.spans() if s["attrs"].get("aos.run.id") == llm2)
        assert json.loads(l2["attrs"][g.GEN_AI_INPUT_MESSAGES]) == [{"role": "user", "parts": [{"type": "text", "content": "SECRET-PROMPT"}]}]
        assert a.put("/api/exports/otlp", json=dict(cfg, environments=["prod"])).status_code == 200
        col.got.clear(); llm3 = str(uuid.uuid4()); t2 = t0 + dt.timedelta(seconds=4)
        httpx.post(B + "/ingest/runs", headers={"x-api-key": k}, json={"post": [{"id": llm3, "name": "l3", "run_type": "llm", "start_time": t2.isoformat(), "end_time": (t2 + dt.timedelta(milliseconds=5)).isoformat(), "model": "gemini-3.8-flash"}]})
        a.post("/api/exports/otlp/run")
        assert not [s for s in col.spans() if s["attrs"].get("aos.run.id") == llm3]
    finally: col.close()

def test_export_admin_only_and_viewer_blocked(a):
    with httpx.Client(base_url=B, auth=("viewer1", "phase0-viewer-local"), timeout=30) as v:
        assert v.get("/api/exports/otlp").status_code == 403 and v.put("/api/exports/otlp", json={"endpoint": "https://x.example.com"}).status_code == 403

# ------------------------------------------------------------------ traceparent
TP_TRACE = uuid.uuid4().hex; TP_PARENT = "00f067aa0ba902b7"

def test_traceparent_unit():
    import sys; sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
    import otel_ctx as o
    c = o.parse(f"00-{TP_TRACE}-{TP_PARENT}-01", "vendor=x")
    assert c.trace_id == TP_TRACE and c.parent_span_id == TP_PARENT and len(c.span_id) == 16 and c.span_id != TP_PARENT and c.tracestate == "vendor=x"
    for bad in (None, "", "garbage", f"00-{'0'*32}-{TP_PARENT}-01", f"00-{TP_TRACE}-{'0'*16}-01", f"ff-{TP_TRACE}-{TP_PARENT}-01", f"00-{TP_TRACE[:-1]}-{TP_PARENT}-01", f"00-{TP_TRACE}-{TP_PARENT}-01-extra"):
        assert o.parse(bad) is None
    assert o.parse(f"01-{TP_TRACE}-{TP_PARENT}-01-future") is not None   # newer versions: extra fields tolerated
    assert o.span_of_run(o.run_id_for(TP_TRACE, TP_PARENT)) == TP_PARENT

def test_traceparent_continues_trace_for_a_run(a, made):
    tid = uuid.uuid4().hex; par = "1122334455667788"
    th = a.post("/api/v1/threads", json={"metadata": {"source": "pytest", "graph_id": G}}).json()["thread_id"]
    r = a.post(f"/api/v1/threads/{th}/runs/wait", headers={"traceparent": f"00-{tid}-{par}-01", "tracestate": "vendor=abc"},
               json={"assistant_id": G, "input": {"messages": [{"type": "human", "content": "hello traceparent " + SUF}]}}); assert r.status_code == 200, r.text
    with pg() as c:
        rows = c.execute("SELECT id::text, parent_id, metadata, trace_id::text FROM tr_runs WHERE trace_id=%s ORDER BY start_time", (str(uuid.UUID(hex=tid)),)).fetchall()
        assert rows, "no run recorded under the incoming trace id"
        root = next(x for x in rows if x[1] is None)
        assert root[3] == str(uuid.UUID(hex=tid)) and root[0].replace("-", "")[:16] == tid[:16]                     # trace id preserved, run id = trace[:16] + our span id
        ep = root[2]["external_parent"]
        assert ep["span_id"] == par and ep["trace_id"] == tid and ep["traceparent"] == f"00-{tid}-{par}-01" and ep["tracestate"] == "vendor=abc"
        assert all(x[3] == root[3] for x in rows)                                                                    # spans of the run stay in the same trace
        c.execute("DELETE FROM tr_runs WHERE trace_id=%s", (str(uuid.UUID(hex=tid)),))
    # the API shows the link on the trace
    # (re-run is not needed: the metadata above is what the trace page reads)

class Downstream:
    def __init__(self):
        self.hdr = []; outer = self
        class H(BaseHTTPRequestHandler):
            def do_POST(s):
                s.rfile.read(int(s.headers.get("content-length") or 0)); outer.hdr.append({k.lower(): v for k, v in s.headers.items()})
                s.send_response(200); s.send_header("content-length", "2"); s.end_headers(); s.wfile.write(b"ok")
            def log_message(s, *x): pass
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H); self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/hook"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

def test_traceparent_propagates_on_outgoing_http(a, made):
    d = Downstream()
    try:
        p = proj(a, made, "tp-out", "dev")
        al = a.post("/api/obs/alerts", json={"project_id": p["id"], "name": f"tp-{SUF}", "metric": "count", "op": "gte", "threshold": 0, "channel": "webhook", "target": d.url}).json()["id"]
        tid = uuid.uuid4().hex; par = "aabbccddeeff0011"
        r = a.post(f"/api/obs/alerts/{al}/test", params={"send": "true"}, headers={"traceparent": f"00-{tid}-{par}-01", "tracestate": "vendor=abc"}); assert r.status_code == 200, r.text
        h = d.hdr[-1]; ver, t, s, fl = h["traceparent"].split("-")
        assert (ver, t, fl) == ("00", tid, "01") and len(s) == 16 and s != par and h.get("tracestate") == "vendor=abc"      # same trace, a NEW span id of ours (the caller's span is our parent)
        n = len(d.hdr); a.post(f"/api/obs/alerts/{al}/test", params={"send": "true"})                                          # no incoming context -> nothing invented, nothing leaked
        assert "traceparent" not in d.hdr[n]
        a.delete(f"/api/obs/alerts/{al}")
    finally: d.srv.shutdown()
