"""GĐ9.10 OpenTelemetry in both directions (opentelemetry-sdk / -exporter-otlp-proto-http 1.45.1, semantic-conventions 0.66b1 = GenAI semconv, schema 1.44).
IN : POST /otel/v1/traces (OTLP/HTTP protobuf or JSON, gzip ok; key in x-api-key or Authorization: Bearer). GenAI semconv spans (gen_ai.operation.name, provider.name,
     request/response.model, usage.input_tokens/output_tokens, input/output messages as span attributes OR the inference-details event OR the older per-message
     events) + OpenInference (llm.*, input.value) + Traceloop (gen_ai.prompt.N.*) + LangSmith-OTel (langsmith.*) styles -> our runs (tokens, cost, model, thread, env).
OUT: per-workspace export of finished runs to an external OTLP collector (endpoint + headers from ws secrets), built with the OTel SDK; attribute names come from the
     installed semconv package (pinned). Message CONTENT is exported only when the workspace turns it on (GenAI content is opt-in in the spec)."""
import asyncio, datetime as dt, json, re, time, uuid, zlib
from urllib.parse import urlparse
from fastapi import APIRouter, HTTPException, Header, Request, Response
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb
import deepagent_api as A
import envs, netguard, otel_ctx
import httpx

router = APIRouter()
from opentelemetry.semconv._incubating.attributes import gen_ai_attributes as G
from opentelemetry.semconv.version import __version__ as SEMCONV_VERSION
from opentelemetry.semconv.schemas import Schemas
SCHEMA_URL = Schemas.V1_44_0.value
MAX_BODY = 32 * 1024 * 1024

# ------------------------------------------------------------------ IN: OTLP -> runs
def _otv(v):
    if not isinstance(v, dict): return v
    for k in ("stringValue", "boolValue", "doubleValue"):
        if k in v: return v[k]
    if "intValue" in v: return int(v["intValue"])
    if "arrayValue" in v: return [_otv(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v: return {x["key"]: _otv(x.get("value")) for x in v["kvlistValue"].get("values", [])}
    return None

def _attrs(lst): return {a["key"]: _otv(a.get("value")) for a in (lst or []) if "key" in a}

def _maybe_json(x):
    if isinstance(x, str) and x[:1] in "{[":
        try: return json.loads(x)
        except Exception: return x  # noqa: BLE001
    return x

KIND = {"llm": "llm", "chat": "llm", "text_completion": "llm", "generate_content": "llm", "completion": "llm",
        "embedding": "embedding", "embeddings": "embedding", "tool": "tool", "execute_tool": "tool",
        "retriever": "retriever", "retrieval": "retriever", "reranker": "retriever",
        "chain": "chain", "agent": "chain", "invoke_agent": "chain", "create_agent": "chain", "invoke_workflow": "chain", "workflow": "chain",
        "task": "chain", "guardrail": "chain", "evaluator": "chain", "prompt": "chain"}

def _text_of_parts(parts):
    out = []
    for p in parts or []:
        if not isinstance(p, dict): out.append(str(p)); continue
        t = p.get("type")
        if t == "text" or "content" in p and t in (None, "text"): out.append(str(p.get("content") if p.get("content") is not None else p.get("text", "")))
        elif t == "tool_call": out.append(f"[tool_call] {p.get('name')}({json.dumps(p.get('arguments'), ensure_ascii=False, default=str)})")
        elif t == "tool_call_response": out.append(str(p.get("response") if p.get("response") is not None else p.get("result", "")))
        elif t == "reasoning": continue
        elif p.get("text"): out.append(str(p["text"]))
    return "\n".join(x for x in out if x != "")

def norm_messages(v, default_role="user"):
    """GenAI messages (JSON string / list of {role, parts:[...]} or {role, content}) -> [{role, content}] (the shape the trace UI renders)."""
    v = _maybe_json(v)
    if isinstance(v, dict): v = v.get("messages") if isinstance(v.get("messages"), list) else [v]
    if isinstance(v, str): return [{"role": default_role, "content": v}] if v else []
    out = []
    for m in v if isinstance(v, list) else []:
        if isinstance(m, str): out.append({"role": default_role, "content": m}); continue
        if not isinstance(m, dict): continue
        role = m.get("role") or m.get("type") or default_role
        c = _text_of_parts(m["parts"]) if isinstance(m.get("parts"), list) else m.get("content")
        if isinstance(c, list): c = _text_of_parts(c)
        elif c is not None and not isinstance(c, str): c = json.dumps(c, ensure_ascii=False, default=str)
        msg = {"role": role, "content": c or ""}
        if m.get("finish_reason"): msg["finish_reason"] = m["finish_reason"]
        out.append(msg)
    return out

_IDX = re.compile(r"^(gen_ai\.prompt|gen_ai\.completion|llm\.input_messages|llm\.output_messages)\.(\d+)\.(?:message\.)?(role|content)$")
def _indexed(at):
    res = {"in": {}, "out": {}}
    for k, v in at.items():
        m = _IDX.match(k)
        if m: res["out" if m.group(1) in ("gen_ai.completion", "llm.output_messages") else "in"].setdefault(int(m.group(2)), {})[m.group(3)] = v
    f = lambda d, role: [{"role": x.get("role") or role, "content": str(x.get("content") or "")} for _, x in sorted(d.items())]
    return f(res["in"], "user"), f(res["out"], "assistant")

_EV_ROLE = {"gen_ai.system.message": "system", "gen_ai.user.message": "user", "gen_ai.assistant.message": "assistant", "gen_ai.tool.message": "tool"}
def _event_messages(events):
    ins, outs = [], []
    for ev in events or []:
        n, ea = ev.get("name") or "", _attrs(ev.get("attributes"))
        if n == "gen_ai.client.inference.operation.details" or G.GEN_AI_INPUT_MESSAGES in ea or G.GEN_AI_OUTPUT_MESSAGES in ea:
            if ea.get(G.GEN_AI_SYSTEM_INSTRUCTIONS):
                ins += [{"role": "system", "content": _text_of_parts(_maybe_json(ea[G.GEN_AI_SYSTEM_INSTRUCTIONS])) if isinstance(_maybe_json(ea[G.GEN_AI_SYSTEM_INSTRUCTIONS]), list) else str(ea[G.GEN_AI_SYSTEM_INSTRUCTIONS])}]
            ins += norm_messages(ea.get(G.GEN_AI_INPUT_MESSAGES)); outs += norm_messages(ea.get(G.GEN_AI_OUTPUT_MESSAGES), "assistant")
        elif n in _EV_ROLE:
            c = ea.get("content") if ea.get("content") is not None else ea.get("body")
            ins.append({"role": _EV_ROLE[n], "content": c if isinstance(c, str) else json.dumps(c, ensure_ascii=False, default=str)})
        elif n == "gen_ai.choice":
            msg = _maybe_json(ea.get("message")); c = (msg.get("content") if isinstance(msg, dict) else None) or ea.get("content") or ea.get("body") or ""
            outs.append({"role": "assistant", "content": c if isinstance(c, str) else json.dumps(c, ensure_ascii=False, default=str)})
    return ins, outs

def _first(at, *keys):
    for k in keys:
        if at.get(k) is not None: return at[k]
    return None

def _num(v):
    try: return float(v) if v is not None and not isinstance(v, bool) else None
    except (TypeError, ValueError): return None

def _int(v):
    f = _num(v); return int(f) if f is not None else None

def _usage_details(at):
    """GĐ12.15: cache / reasoning / audio token counts of a span (GenAI semconv gen_ai.usage.cache_read.input_tokens ..., OpenInference llm.token_count.*_details.*)
    as LangChain-style input_token_details / output_token_details; obs.upsert_run turns them into tr_runs.usage_details / cost_details."""
    pick = lambda d: {k: n for k, v in d.items() if (n := _int(v)) is not None and n > 0}   # noqa: E731
    ind = pick({"cache_read": _first(at, G.GEN_AI_USAGE_CACHE_READ_INPUT_TOKENS, "llm.token_count.prompt_details.cache_read"),
                "cache_creation": _first(at, G.GEN_AI_USAGE_CACHE_CREATION_INPUT_TOKENS, "llm.token_count.prompt_details.cache_write"),
                "audio": at.get("llm.token_count.prompt_details.audio")})
    outd = pick({"reasoning": _first(at, G.GEN_AI_USAGE_REASONING_OUTPUT_TOKENS, "llm.token_count.completion_details.reasoning"),
                 "audio": at.get("llm.token_count.completion_details.audio")})
    return {k: v for k, v in (("input_token_details", ind), ("output_token_details", outd)) if v}

def _pick_model(at):
    """Response model unless only the request model is known to the pricing table (response ids like 'gpt-x-2026-01-01' often are not priced)."""
    resp, req = _first(at, G.GEN_AI_RESPONSE_MODEL, "llm.model_name"), at.get(G.GEN_AI_REQUEST_MODEL)
    pr = getattr(A.gw, "PRICING", {}) or {}
    if resp and resp not in pr and req and req in pr: return req
    return resp or req

_CONSUMED = ("input.", "output.", "gen_ai.prompt", "gen_ai.completion", "langsmith.inputs", "langsmith.outputs", G.GEN_AI_INPUT_MESSAGES, G.GEN_AI_OUTPUT_MESSAGES,
             G.GEN_AI_SYSTEM_INSTRUCTIONS, "llm.input_messages", "llm.output_messages", G.GEN_AI_TOOL_CALL_ARGUMENTS, G.GEN_AI_TOOL_CALL_RESULT, "traceloop.entity.input", "traceloop.entity.output")

def otlp_to_runs(body):
    """OTLP/JSON dict -> list of run dicts (obs.upsert_run input). Pure function (no DB)."""
    out = []
    for rs in body.get("resourceSpans", []):
        res = _attrs((rs.get("resource") or {}).get("attributes"))
        env = res.get("deployment.environment.name") or res.get("deployment.environment")
        for ss in rs.get("scopeSpans", []) + rs.get("instrumentationLibrarySpans", []):
            scope = (ss.get("scope") or ss.get("instrumentationLibrary") or {}).get("name")
            for sp in ss.get("spans", []):
                tid = (sp.get("traceId") or "").lower(); sid = (sp.get("spanId") or "").lower(); par = (sp.get("parentSpanId") or "").lower()
                if len(tid) != 32 or len(sid) != 16: continue
                at = _attrs(sp.get("attributes"))
                op = str(_first(at, "langsmith.span.kind", "openinference.span.kind", "traceloop.span.kind", G.GEN_AI_OPERATION_NAME) or "").lower()
                kind = KIND.get(op) or ("llm" if _first(at, G.GEN_AI_REQUEST_MODEL, "llm.model_name") and op == "" else "chain")
                md = {k: v for k, v in at.items() if not k.startswith(_CONSUMED)}
                md.update({"otel.service": res.get("service.name"), "otel.span_id": sid})
                if sp.get("traceState"): md["otel.trace_state"] = sp["traceState"]
                if scope: md["otel.scope"] = scope
                e = _first(at, "langsmith.metadata.ls_environment", "langsmith.metadata.environment") or env
                try: e = envs.norm(e)
                except ValueError: e = None
                if e: md["environment"] = e
                ins, outs = norm_messages(at.get(G.GEN_AI_INPUT_MESSAGES)), norm_messages(at.get(G.GEN_AI_OUTPUT_MESSAGES), "assistant")
                if at.get(G.GEN_AI_SYSTEM_INSTRUCTIONS):
                    si = _maybe_json(at[G.GEN_AI_SYSTEM_INSTRUCTIONS]); ins = [{"role": "system", "content": _text_of_parts(si) if isinstance(si, list) else str(si)}] + ins
                if not ins and not outs: ins, outs = _event_messages(sp.get("events"))
                if not ins and not outs: ins, outs = _indexed(at)
                raw_in = _first(at, "input.value", "langsmith.inputs", "traceloop.entity.input", G.GEN_AI_PROMPT)
                raw_out = _first(at, "output.value", "langsmith.outputs", "traceloop.entity.output", G.GEN_AI_COMPLETION)
                if kind == "tool":
                    a = _first(at, G.GEN_AI_TOOL_CALL_ARGUMENTS, "input.value", "tool.parameters")
                    r = _first(at, G.GEN_AI_TOOL_CALL_RESULT, "output.value")
                    inputs = {"args": _maybe_json(a)} if a is not None else None; outputs = {"result": _maybe_json(r)} if r is not None else None
                    name = at.get(G.GEN_AI_TOOL_NAME) or at.get("tool.name") or sp.get("name") or "tool"
                else:
                    inputs = {"messages": ins} if ins else ({"input": _maybe_json(raw_in)} if raw_in is not None else
                             ({"query": at[G.GEN_AI_RETRIEVAL_QUERY_TEXT]} if at.get(G.GEN_AI_RETRIEVAL_QUERY_TEXT) else None))
                    outputs = {"messages": outs} if outs else ({"output": _maybe_json(raw_out)} if raw_out is not None else
                              ({"documents": _maybe_json(at[G.GEN_AI_RETRIEVAL_DOCUMENTS])} if at.get(G.GEN_AI_RETRIEVAL_DOCUMENTS) else None))
                    name = sp.get("name") or "span"
                st = sp.get("status") or {}
                err = (st.get("message") or at.get("error.type") or "error") if st.get("code") in (2, "STATUS_CODE_ERROR") else None
                cost = _num(_first(at, "gen_ai.usage.cost", "llm.cost.total", "llm.usage.cost"))
                run = {"id": str(uuid.UUID(hex=tid[:16] + sid)), "trace_id": str(uuid.UUID(hex=tid)),
                       "parent_run_id": str(uuid.UUID(hex=tid[:16] + par)) if len(par) == 16 and par != "0" * 16 else None,
                       "name": str(name)[:200], "run_type": kind,
                       "start_time": dt.datetime.fromtimestamp(int(sp.get("startTimeUnixNano", 0)) / 1e9, dt.timezone.utc),
                       "end_time": dt.datetime.fromtimestamp(int(sp["endTimeUnixNano"]) / 1e9, dt.timezone.utc) if sp.get("endTimeUnixNano") else None,
                       "inputs": inputs, "outputs": outputs, "error": err,
                       "model": _pick_model(at) if kind in ("llm", "embedding") or at.get(G.GEN_AI_REQUEST_MODEL) else None,
                       "usage": {"input_tokens": _int(_first(at, G.GEN_AI_USAGE_INPUT_TOKENS, G.GEN_AI_USAGE_PROMPT_TOKENS, "llm.token_count.prompt")),
                                 "output_tokens": _int(_first(at, G.GEN_AI_USAGE_OUTPUT_TOKENS, G.GEN_AI_USAGE_COMPLETION_TOKENS, "llm.token_count.completion")), **_usage_details(at)},
                       "thread_id": _first(at, G.GEN_AI_CONVERSATION_ID, "session.id", "langsmith.metadata.thread_id"),
                       "tags": ["otel"] + (["genai"] if any(k.startswith("gen_ai.") for k in at) else []), "metadata": md}
                if cost is not None: run["cost_usd"] = cost
                out.append(run)
    return out

def otlp_proto_to_json(raw: bytes):
    """OTLP/HTTP protobuf -> the JSON OTLP shape (ids as hex)."""
    import base64
    from google.protobuf.json_format import MessageToDict
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    m = ExportTraceServiceRequest(); m.ParseFromString(raw)
    d = MessageToDict(m)
    for rs in d.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for sp in ss.get("spans", []):
                for k in ("traceId", "spanId", "parentSpanId"):
                    if sp.get(k): sp[k] = base64.b64decode(sp[k]).hex()
    return d

def _inflate(raw: bytes, enc: str) -> bytes:
    enc = (enc or "").lower()
    if enc not in ("gzip", "deflate"): return raw
    d = zlib.decompressobj(31 if enc == "gzip" else 15); out = d.decompress(raw, MAX_BODY + 1)
    if len(out) > MAX_BODY: raise HTTPException(413, "OTLP quá lớn (tối đa 32 MB sau giải nén)")
    return out

def _key_from(req: Request, x_api_key: str):
    k = x_api_key or req.headers.get("x-api-key") or ""
    if not k:
        a = req.headers.get("authorization") or ""
        if a.lower().startswith("bearer "): k = a[7:].strip()
    return k

async def ingest(req: Request, key: str):
    import admin, obs
    ws = await obs.key_ws(key)
    async with admin.as_workspace(ws):   # GĐ10.1: all trace writes (insert, orphan re-attach, rollup) under the RLS of the key's workspace
        return await _ingest(req, key)

async def _ingest(req: Request, key: str):
    import obs
    pid, env = await obs.key_ctx(key)
    ct = (req.headers.get("content-type") or "").lower()
    raw = _inflate(await req.body(), req.headers.get("content-encoding"))
    if len(raw) > MAX_BODY: raise HTTPException(413, "OTLP quá lớn")
    if "protobuf" in ct:
        try: body = otlp_proto_to_json(raw)
        except Exception: raise HTTPException(400, "Protobuf OTLP không hợp lệ") from None  # noqa: BLE001
    elif "json" in ct:
        try: body = json.loads(raw)
        except Exception: raise HTTPException(400, "JSON OTLP không hợp lệ") from None  # noqa: BLE001
    else:
        raise HTTPException(415, "Content-Type phải là application/x-protobuf hoặc application/json")
    runs = otlp_to_runs(body)
    ids = {r["id"] for r in runs}
    for r in runs:   # a parent we have never seen (other service / another batch still to come) -> root with a visible external-parent link; re-attached below when it arrives
        p = r.get("parent_run_id")
        if p and p not in ids and not await A.q("SELECT 1 FROM tr_runs WHERE id=%s", (p,), one=True):
            psid = otel_ctx.span_of_run(p); tx = otel_ctx.trace_hex(r["trace_id"])
            r["parent_run_id"] = None
            r["metadata"] = {**r["metadata"], "otel.orphan_of": psid, "external_parent": {"trace_id": tx, "span_id": psid, "traceparent": otel_ctx.fmt(tx, psid), "source": "otlp"}}
    runs.sort(key=lambda r: (r["parent_run_id"] is not None, r["start_time"]))
    await obs.check_trace_cap(runs)   # GĐ12.15: 25,000 runs per trace, the whole request is refused (nothing written)
    for r in runs: await obs.upsert_run(pid, r, env=env)
    for r in runs:   # children that arrived before this span (batches finish child-first): attach them now
        await A.q("UPDATE tr_runs SET parent_id=%s, metadata=metadata - 'otel.orphan_of' - 'external_parent' WHERE trace_id=%s AND parent_id IS NULL AND metadata->>'otel.orphan_of'=%s AND id<>%s",
                  (r["id"], r["trace_id"], otel_ctx.span_of_run(r["id"]), r["id"]))
    await obs.rollup({r["trace_id"] for r in runs})
    if "protobuf" in ct: return Response(content=b"", media_type="application/x-protobuf")   # empty ExportTraceServiceResponse
    return {"partialSuccess": {}, "accepted": len(runs)}

@router.post("/otel/v1/traces", tags=["ingest"])
async def otlp_traces(req: Request, x_api_key: str = Header(default="")):
    return await ingest(req, _key_from(req, x_api_key))

# ------------------------------------------------------------------ OUT: runs -> external OTLP collector
DDL = """
CREATE TABLE IF NOT EXISTS otel_export_cfg(workspace_id INT PRIMARY KEY, enabled BOOLEAN NOT NULL DEFAULT false, endpoint TEXT, headers JSONB NOT NULL DEFAULT '{}',
  include_content BOOLEAN NOT NULL DEFAULT false, environments TEXT[] NOT NULL DEFAULT '{}', cursor JSONB NOT NULL DEFAULT '{}', every_s INT NOT NULL DEFAULT 60,
  last_run_at TIMESTAMPTZ, last_status TEXT, last_error TEXT, last_count INT, total INT NOT NULL DEFAULT 0, updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
"""
BATCH = 500; MAX_BATCHES = 20
LOCKS: dict = {}

async def init(): await A.q(DDL)

def endpoint_url(ep: str) -> str:
    """Validate + normalise a collector endpoint through netguard (GĐ10.2): https anywhere public; internal / plain-http collectors only when listed in AOS_EGRESS_ALLOW.
    Base URL gets /v1/traces appended."""
    ep = (ep or "").strip()
    if not ep.startswith(("http://", "https://")): raise ValueError("Endpoint phải là URL dạng https://host:4318")
    try: netguard.check(ep, "webhook")
    except netguard.NetBlocked as e: raise ValueError("Endpoint bị chặn: " + e.reason) from None
    return ep.rstrip("/") if urlparse(ep).path.rstrip("/").endswith("/v1/traces") else ep.rstrip("/") + "/v1/traces"

def provider_of(model, md=None):
    p = (md or {}).get(G.GEN_AI_PROVIDER_NAME)
    if p: return str(p)
    m = (model or "").lower()
    for pre, name in (("gemini", "gcp.gemini"), ("gpt", "openai"), ("o1", "openai"), ("o3", "openai"), ("o4", "openai"), ("claude", "anthropic"), ("mistral", "mistral_ai"), ("deepseek", "deepseek"), ("grok", "x_ai")):
        if m.startswith(pre): return name
    return "_OTHER" if m else None

def _msgs_out(v, role_default):
    """our {messages:[{role,content}]} / {answer} / plain -> GenAI semconv structured messages (JSON string)."""
    if isinstance(v, str):
        try: v = json.loads(v)
        except Exception: pass  # noqa: BLE001
    ms = v.get("messages") if isinstance(v, dict) else None
    if ms is None and isinstance(v, dict):
        t = v.get("answer") or v.get("message") or v.get("output") or v.get("question")
        ms = [{"role": role_default, "content": t}] if t else None
    if not ms: return None
    out = [{"role": ({"human": "user", "ai": "assistant"}.get(str(m.get("role")), m.get("role") or role_default)), "parts": [{"type": "text", "content": str(m.get("content") or "")}]} for m in ms if isinstance(m, dict)]
    return json.dumps(out, ensure_ascii=False) if out else None

def run_to_span(r: dict, include_content: bool, resources: dict):
    """One tr_runs row -> opentelemetry.sdk.trace.ReadableSpan with GenAI semconv attributes (names from the pinned semconv package)."""
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.util.instrumentation import InstrumentationScope
    from opentelemetry.trace import SpanContext, TraceFlags, SpanKind, Status, StatusCode
    md = r.get("metadata") or {}
    tid = int(otel_ctx.trace_hex(r["trace_id"]), 16); sid = int(otel_ctx.span_of_run(r["id"]), 16) or 1
    ctx = SpanContext(tid, sid, False, TraceFlags(TraceFlags.SAMPLED))
    parent = None
    if r.get("parent_id"): parent = SpanContext(tid, int(otel_ctx.span_of_run(r["parent_id"]), 16) or 1, False, TraceFlags(TraceFlags.SAMPLED))
    elif isinstance(md.get("external_parent"), dict) and md["external_parent"].get("span_id"):
        parent = SpanContext(int(md["external_parent"].get("trace_id") or otel_ctx.trace_hex(r["trace_id"]), 16), int(md["external_parent"]["span_id"], 16), True, TraceFlags(TraceFlags.SAMPLED))
    env = r.get("env") or "prod"
    res = resources.get(env)
    if res is None:
        res = resources[env] = Resource.create({"service.name": "agent-os", "deployment.environment.name": env, "aos.workspace.id": r.get("ws")}, schema_url=SCHEMA_URL)
    rt = r.get("run_type") or "chain"; a = {"aos.run.id": str(r["id"]), "aos.project": r.get("project"), "aos.run.type": rt}
    if rt == "llm":
        a[G.GEN_AI_OPERATION_NAME] = G.GenAiOperationNameValues.CHAT.value
        a[G.GEN_AI_REQUEST_MODEL] = r.get("model"); a[G.GEN_AI_RESPONSE_MODEL] = r.get("model"); a[G.GEN_AI_PROVIDER_NAME] = provider_of(r.get("model"), md)
        if r.get("input_tokens") is not None: a[G.GEN_AI_USAGE_INPUT_TOKENS] = int(r["input_tokens"])
        if r.get("output_tokens") is not None: a[G.GEN_AI_USAGE_OUTPUT_TOKENS] = int(r["output_tokens"])
        if include_content:
            a[G.GEN_AI_INPUT_MESSAGES] = _msgs_out(r.get("inputs"), "user"); a[G.GEN_AI_OUTPUT_MESSAGES] = _msgs_out(r.get("outputs"), "assistant")
    elif rt == "tool":
        a[G.GEN_AI_OPERATION_NAME] = G.GenAiOperationNameValues.EXECUTE_TOOL.value; a[G.GEN_AI_TOOL_NAME] = r.get("name")
        if include_content:
            i, o = r.get("inputs"), r.get("outputs")
            if i is not None: a[G.GEN_AI_TOOL_CALL_ARGUMENTS] = json.dumps((i.get("args") if isinstance(i, dict) and "args" in i else i), ensure_ascii=False, default=str)
            if o is not None: a[G.GEN_AI_TOOL_CALL_RESULT] = json.dumps((o.get("result") if isinstance(o, dict) and "result" in o else o), ensure_ascii=False, default=str)
    elif rt == "retriever": a[G.GEN_AI_OPERATION_NAME] = G.GenAiOperationNameValues.RETRIEVAL.value
    elif md.get("agent_id") is not None or any(str(t).startswith("agent:") for t in (r.get("tags") or [])):
        a[G.GEN_AI_OPERATION_NAME] = G.GenAiOperationNameValues.INVOKE_AGENT.value; a[G.GEN_AI_AGENT_NAME] = r.get("name")
        if md.get("agent_id") is not None: a[G.GEN_AI_AGENT_ID] = str(md["agent_id"])
        if md.get("agent_version") is not None: a[G.GEN_AI_AGENT_VERSION] = str(md["agent_version"])
        if include_content:
            a[G.GEN_AI_INPUT_MESSAGES] = _msgs_out(r.get("inputs"), "user"); a[G.GEN_AI_OUTPUT_MESSAGES] = _msgs_out(r.get("outputs"), "assistant")
    if r.get("thread_id"): a[G.GEN_AI_CONVERSATION_ID] = r["thread_id"]
    if r.get("cost_usd") is not None: a["aos.cost_usd"] = float(r["cost_usd"])
    if r.get("error"): a["error.type"] = "_OTHER"
    a = {k: v for k, v in a.items() if v is not None}
    ns = lambda t: int(t.timestamp() * 1e9)
    return ReadableSpan(name=(f"chat {r['model']}" if rt == "llm" and r.get("model") else f"execute_tool {r['name']}" if rt == "tool" else r.get("name") or "run"),
                        context=ctx, parent=parent, resource=res, attributes=a, kind=SpanKind.CLIENT if rt == "llm" else SpanKind.INTERNAL,
                        status=Status(StatusCode.ERROR, (r.get("error") or "")[:200]) if r.get("error") else Status(StatusCode.OK),
                        start_time=ns(r["start_time"]), end_time=ns(r["end_time"]), instrumentation_scope=InstrumentationScope("agentos.tracing", SEMCONV_VERSION, SCHEMA_URL))

SQL_ROWS = f"""SELECT r.id, r.trace_id, r.parent_id, r.name, r.run_type, r.start_time, r.end_time, r.status, r.error, r.inputs, r.outputs, r.metadata, r.tags,
  r.input_tokens, r.output_tokens, r.cost_usd, r.model, r.thread_id, p.name project, p.workspace_id ws, {envs.ENV_SQL.replace('tr_runs.', 'r.').replace('metadata->>', 'r.metadata->>')} env
  FROM tr_runs r JOIN projects p ON p.id=r.project_id WHERE p.workspace_id=%s AND r.end_time IS NOT NULL AND (r.end_time, r.id::text) > (%s::timestamptz, %s::text)
  ORDER BY r.end_time, r.id::text LIMIT %s"""

async def get_cfg(ws):
    r = await A.q("SELECT * FROM otel_export_cfg WHERE workspace_id=%s", (ws,), one=True)
    return r or {"workspace_id": ws, "enabled": False, "endpoint": None, "headers": {}, "include_content": False, "environments": [], "cursor": {}, "every_s": 60,
                 "last_run_at": None, "last_status": None, "last_error": None, "last_count": None, "total": 0}

async def _send(spans, url, headers):
    """POST one OTLP/HTTP protobuf batch through netguard (the stock exporter's `requests` session would resolve DNS on its own)."""
    from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
    body = await asyncio.to_thread(lambda: encode_spans(spans).SerializeToString())
    try:
        async with netguard.client("webhook", timeout=10) as h:
            r = await h.post(url, content=body, headers={**(headers or {}), "Content-Type": "application/x-protobuf"})
    except netguard.NetBlocked as e: raise ValueError("Endpoint bị chặn: " + e.reason) from None
    except httpx.HTTPError as e: raise RuntimeError("Collector không nhận được lô span: " + type(e).__name__) from None   # no str(e): may hold the URL
    if r.status_code >= 300: raise RuntimeError(f"Collector từ chối lô span (HTTP {r.status_code})")

async def export_ws(ws: int) -> dict:
    """Export the workspace's newly finished runs. Run as its own task (sets the RLS context). Cursor = (end_time, id) of the last exported run."""
    import admin, ws_secrets
    admin.USER.set(None); admin.WS_CTX.set(ws)
    lock = LOCKS.setdefault(ws, asyncio.Lock())
    if lock.locked(): raise HTTPException(409, "Đang xuất, đợi xong rồi thử lại")
    async with lock:
        cfg = await get_cfg(ws); cur = dict(cfg.get("cursor") or {}); sent = 0; err = None
        try:
            url = endpoint_url(cfg.get("endpoint") or "")
            hdr = await ws_secrets.resolve_headers(dict(cfg.get("headers") or {}), url, ws)
            if not cur: cur = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(), "id": ""}   # first run: only runs that finish from now on
            res: dict = {}
            for _ in range(MAX_BATCHES):
                rows = await A.q(SQL_ROWS, (ws, cur["ts"], cur["id"], BATCH))
                if not rows: break
                last = rows[-1]; keep = [r for r in rows if not cfg["environments"] or r["env"] in cfg["environments"]]
                if keep:
                    await _send([run_to_span(r, bool(cfg["include_content"]), res) for r in keep], url, hdr); sent += len(keep)
                cur = {"ts": last["end_time"].isoformat(), "id": str(last["id"])}
                await A.q("UPDATE otel_export_cfg SET cursor=%s WHERE workspace_id=%s", (Jsonb(cur), ws))
                if len(rows) < BATCH: break
        except (ValueError, LookupError, PermissionError) as e: err = str(e)[:300]
        except HTTPException: raise
        except Exception as e: err = f"{type(e).__name__}: {str(e)[:300]}"  # noqa: BLE001
        await A.q("INSERT INTO otel_export_cfg(workspace_id,cursor,last_run_at,last_status,last_error,last_count,total) VALUES(%s,%s,now(),%s,%s,%s,%s) "
                  "ON CONFLICT(workspace_id) DO UPDATE SET cursor=excluded.cursor, last_run_at=now(), last_status=excluded.last_status, last_error=excluded.last_error, "
                  "last_count=excluded.last_count, total=otel_export_cfg.total+excluded.total", (ws, Jsonb(cur), "error" if err else "ok", err, sent, sent))
        if err: raise ValueError(err)
    return {"sent": sent}

async def run_due():
    """Worker task (every ~minute): enabled workspaces whose interval has elapsed. Atomic claim on last_run_at."""
    for c in await A.q("SELECT workspace_id, last_run_at, every_s FROM otel_export_cfg WHERE enabled AND endpoint IS NOT NULL"):
        won = await A.q("UPDATE otel_export_cfg SET last_run_at=now() WHERE workspace_id=%s AND (last_run_at IS NULL OR last_run_at < now() - every_s * interval '1 second') "
                        "AND last_run_at IS NOT DISTINCT FROM %s RETURNING 1", (c["workspace_id"], c["last_run_at"]), one=True)
        if won:
            try: await asyncio.create_task(export_ws(c["workspace_id"]))
            except Exception: pass  # noqa: BLE001  (stored in last_error)

# ------------------------------------------------------------------ API (admin only)
def _admin():
    import admin; u = admin.cur()
    if not u or u.get("role") != "admin" or u.get("demo"): raise HTTPException(403, "Chỉ admin cấu hình xuất OpenTelemetry")
    return u

class CfgIn(BaseModel):
    enabled: bool = False
    endpoint: str = Field(max_length=300)
    headers: dict[str, str] = Field(default_factory=dict, max_length=20)   # values may be "{{secret:NAME}}" (host-bound ws secrets)
    include_content: bool = False
    environments: list[str] = Field(default_factory=list, max_length=20)
    every_s: int = Field(default=60, ge=10, le=3600)

def _public(c: dict) -> dict:
    keys = ("enabled", "endpoint", "headers", "include_content", "environments", "every_s", "last_run_at", "last_status", "last_error", "last_count", "total")
    return {**{k: c.get(k) for k in keys}, "semconv_version": SEMCONV_VERSION, "schema_url": SCHEMA_URL}

@router.get("/api/exports/otlp")
async def cfg_get():
    return _public(await get_cfg(_admin()["ws"]))

@router.put("/api/exports/otlp")
async def cfg_put(b: CfgIn):
    u = _admin()
    try: endpoint_url(b.endpoint)
    except ValueError as e: raise HTTPException(422, str(e)) from None
    for k, v in b.headers.items():
        if not re.fullmatch(r"[A-Za-z0-9\-_]{1,64}", k): raise HTTPException(422, f"Tên header không hợp lệ: {k}")
        if "{{" in v:
            for n in re.findall(r"\{\{secret:([A-Za-z0-9_.\-]+)\}\}", v):
                if not await A.q("SELECT 1 FROM ws_secrets WHERE workspace_id=%s AND name=%s", (u["ws"], n), one=True): raise HTTPException(422, f"Không có bí mật {n} trong workspace")
        elif k.lower() in ("authorization", "x-api-key", "api-key") or "key" in k.lower() or "token" in k.lower():
            raise HTTPException(422, f"Header {k} phải dùng tham chiếu bí mật {{{{secret:TÊN}}}} (không lưu khoá thẳng)")
    envs_ = [envs.norm_http(x) for x in b.environments]
    await A.q("INSERT INTO otel_export_cfg(workspace_id,enabled,endpoint,headers,include_content,environments,every_s) VALUES(%s,%s,%s,%s,%s,%s,%s) "
              "ON CONFLICT(workspace_id) DO UPDATE SET enabled=excluded.enabled, endpoint=excluded.endpoint, headers=excluded.headers, include_content=excluded.include_content, "
              "environments=excluded.environments, every_s=excluded.every_s, updated_at=now()", (u["ws"], b.enabled, b.endpoint.strip(), Jsonb(b.headers), b.include_content, envs_, b.every_s))
    return _public(await get_cfg(u["ws"]))

class RunIn(BaseModel):
    from_start: bool = False   # also export runs that finished before the first run (backfill, last 24 h)

@router.post("/api/exports/otlp/run")
async def run_now(b: RunIn | None = None):
    u = _admin()
    if b and b.from_start:
        await A.q("UPDATE otel_export_cfg SET cursor=%s WHERE workspace_id=%s", (Jsonb({"ts": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)).isoformat(), "id": ""}), u["ws"]))
    try: r = await asyncio.shield(asyncio.create_task(export_ws(u["ws"])))
    except ValueError as e: raise HTTPException(422, str(e)) from None
    return {"ok": True, **r, **_public(await get_cfg(u["ws"]))}
