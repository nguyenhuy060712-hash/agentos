"""GĐ13.7 Agent fields `response_format` (structured output) and `middleware` (safety / limits), validated and turned into the official LangChain v1 middleware.
`response_format`: a JSON Schema object (max 20 KB, only local `#/$defs/..` references, no reference cycle, nesting <= 24); it goes to `create_deep_agent(response_format=...)`
  and the run returns `structured_response` (checked here against the schema: LangChain does not validate raw JSON-schema dicts).
`middleware`: `[{name, params}]` with an ALLOW-LIST (ModelCallLimit, ToolCallLimit, ModelRetry, ToolRetry, Summarization, ContextEditing, PII, ModelFallback). Anything else
  (shell, file search, tool emulator, unknown) is refused (422 through the pydantic validator); params are bounded and unknown keys are refused.
  `ModelFallback` is NOT a second mechanism: it is mapped to `AgentCfg.fallback_models` (the gateway chain; a workspace chain row of 11.13 still wins). `PII` = `pii.vn_pii_middleware`
  with the workspace entity list; entities the gateway already masks for this workspace (9.8) are skipped (no double masking).
Both fields are refused for the demo workspace (server side). Pure functions + `build`; no import of deepagent_api (it imports this module)."""
import json, re

MAX_SCHEMA_BYTES = 20 * 1024
MAX_DEPTH = 24
MAX_MIDDLEWARE = 12
_LOCAL_REF = re.compile(r"^#/(\$defs|definitions)/[A-Za-z0-9_.\-]{1,80}$")
_MODEL_RE = re.compile(r"^[\w./:@\-]{1,120}$")
_REF_KEYS = ("$dynamicRef", "$recursiveRef", "$dynamicAnchor", "$recursiveAnchor")

# ------------------------------------------------------------------ response_format
def _walk(o, depth=0):
    """Yield (node, depth) for every dict in the schema, iterative (a hostile deep schema must not blow the stack)."""
    st = [(o, 1)]
    while st:
        n, d = st.pop()
        if isinstance(n, dict):
            yield n, d
            st += [(v, d + 1) for v in n.values() if isinstance(v, (dict, list))]
        elif isinstance(n, list):
            st += [(v, d + 1) for v in n if isinstance(v, (dict, list))]

def _refs_of(node):
    return [n["$ref"] for n, _ in _walk(node) if "$ref" in n]

def check_response_format(rf):
    """-> a normalised copy (title usable as a tool name) or None. Raises ValueError (pydantic -> HTTP 422)."""
    if rf is None: return None
    if not isinstance(rf, dict) or not rf: raise ValueError("response_format must be a JSON Schema object / là một JSON Schema (object)")
    try: raw = json.dumps(rf, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError, RecursionError): raise ValueError("response_format is not valid JSON / không phải JSON hợp lệ") from None
    if len(raw.encode("utf-8")) > MAX_SCHEMA_BYTES: raise ValueError(f"response_format too large (max {MAX_SCHEMA_BYTES // 1024} KB) / quá lớn")
    if max(d for _, d in _walk(rf)) > MAX_DEPTH: raise ValueError(f"response_format nested too deep (max {MAX_DEPTH}) / lồng quá sâu")
    if rf.get("type") != "object": raise ValueError('response_format needs "type": "object" at the top / cần "type": "object" ở cấp ngoài cùng')
    for n, _ in _walk(rf):
        if any(k in n for k in _REF_KEYS): raise ValueError("dynamic references are not allowed / không cho phép tham chiếu động")
        r = n.get("$ref")
        if r is not None and not (isinstance(r, str) and _LOCAL_REF.match(r)):
            raise ValueError("only local references (#/$defs/<name>) are allowed; '#' and remote URLs are refused / chỉ cho phép tham chiếu nội bộ #/$defs/<tên>")
    defs = {**(rf.get("definitions") or {}), **(rf.get("$defs") or {})}
    graph = {k: [r.rsplit("/", 1)[1] for r in _refs_of(v)] for k, v in defs.items()}
    for r in _refs_of({k: v for k, v in rf.items() if k not in ("$defs", "definitions")}):
        if r.rsplit("/", 1)[1] not in defs: raise ValueError(f"unknown reference {r} / tham chiếu không tồn tại")
    for k, outs in graph.items():
        for o in outs:
            if o not in defs: raise ValueError(f"unknown reference #/$defs/{o} / tham chiếu không tồn tại")
    state: dict = {}
    for start in graph:                     # reference cycle (a -> b -> a, or a -> a) = the schema never ends
        st = [(start, iter(graph[start]))]; state[start] = 1
        while st:
            node, it = st[-1]
            nxt = next(it, None)
            if nxt is None: state[node] = 2; st.pop(); continue
            if state.get(nxt) == 1: raise ValueError("reference cycle in response_format / vòng lặp tham chiếu trong schema")
            if state.get(nxt) is None: state[nxt] = 1; st.append((nxt, iter(graph[nxt])))
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError
    try: Draft202012Validator.check_schema(rf)
    except SchemaError as e: raise ValueError("invalid JSON Schema: " + str(e.message)[:200]) from None
    out = json.loads(raw)
    t = re.sub(r"[^A-Za-z0-9_]+", "_", str(out.get("title") or "")).strip("_")[:60]
    out["title"] = t or "structured_output"
    return out

def validate_structured(rf, value):
    """None when `value` satisfies the schema, else a short message (never raises)."""
    if not rf: return None
    try:
        from jsonschema import Draft202012Validator
        e = next(iter(Draft202012Validator(rf).iter_errors(value)), None)
        return None if e is None else (("/".join(str(p) for p in e.absolute_path) or "$") + ": " + str(e.message)[:200])
    except Exception as ex:  # noqa: BLE001
        return "schema check failed: " + str(ex)[:120]

# ------------------------------------------------------------------ middleware allow-list
def _i(lo, hi): return ("int", lo, hi)
def _f(lo, hi): return ("float", lo, hi)
_EXIT = ("enum", ("end", "error"))
_TEXIT = ("enum", ("continue", "error", "end"))
_ONFAIL = ("enum", ("continue", "error"))
_RETRY = {"max_retries": _i(0, 5), "on_failure": _ONFAIL, "backoff_factor": _f(0, 10), "initial_delay": _f(0, 30), "max_delay": _f(0, 120), "jitter": ("bool",)}
SPECS = {
    "ModelCallLimit": {"thread_limit": _i(1, 10000), "run_limit": _i(1, 1000), "exit_behavior": _EXIT},
    "ToolCallLimit": {"tool_name": ("str", 120), "thread_limit": _i(1, 10000), "run_limit": _i(1, 1000), "exit_behavior": _TEXIT},
    "ModelRetry": dict(_RETRY),
    "ToolRetry": {**_RETRY, "tools": ("strs", 20, 120)},
    "Summarization": {"trigger_tokens": _i(1000, 2_000_000), "trigger_messages": _i(5, 1000), "keep_messages": _i(2, 200)},
    "ContextEditing": {"trigger_tokens": _i(1000, 2_000_000), "keep": _i(0, 50), "clear_at_least": _i(0, 1_000_000), "clear_tool_inputs": ("bool",),
                       "exclude_tools": ("strs", 20, 120), "placeholder": ("str", 60)},
    "PII": {"entities": ("strs", 20, 40), "strategy": ("enum", ("redact", "mask", "hash", "block")), "apply_to_output": ("bool",), "apply_to_tool_results": ("bool",)},
    "ModelFallback": {"models": ("strs", 5, 120)},
}
ALLOWED = tuple(SPECS)

def canonical(name):
    """'ModelCallLimitMiddleware' / 'modelcalllimit' -> 'ModelCallLimit'; anything outside the allow-list -> None."""
    n = re.sub(r"(?i)middleware$", "", str(name or "").strip()).lower()
    return next((k for k in SPECS if k.lower() == n), None)

def _one(spec, key, v):
    kind = spec[0]
    if kind == "bool":
        if not isinstance(v, bool): raise ValueError(f"{key} must be true/false")
        return v
    if kind in ("int", "float"):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or (kind == "int" and not isinstance(v, int)): raise ValueError(f"{key} must be a {'whole ' if kind == 'int' else ''}number")
        if not spec[1] <= v <= spec[2]: raise ValueError(f"{key} must be between {spec[1]} and {spec[2]}")
        return int(v) if kind == "int" else float(v)
    if kind == "enum":
        if v not in spec[1]: raise ValueError(f"{key} must be one of {', '.join(spec[1])}")
        return v
    if kind == "str":
        if not isinstance(v, str) or not v.strip() or len(v) > spec[1]: raise ValueError(f"{key} must be a text of 1-{spec[1]} characters")
        return v
    if kind == "strs":
        if not isinstance(v, list) or not v or len(v) > spec[1] or not all(isinstance(x, str) and x.strip() and len(x) <= spec[2] for x in v):
            raise ValueError(f"{key} must be a list of 1-{spec[1]} texts (max {spec[2]} characters each)")
        return list(dict.fromkeys(v))
    raise ValueError("bad spec")

def check_middleware(items):
    """-> [{name, params}] normalised. Raises ValueError (pydantic -> HTTP 422): unknown name, unknown key, out-of-range value, duplicate, > 12 entries."""
    items = list(items or [])
    if len(items) > MAX_MIDDLEWARE: raise ValueError(f"at most {MAX_MIDDLEWARE} middleware / tối đa {MAX_MIDDLEWARE} middleware")
    out, seen = [], set()
    for it in items:
        d = it if isinstance(it, dict) else {"name": getattr(it, "name", None), "params": getattr(it, "params", {})}
        extra = set(d) - {"name", "params"}
        if extra: raise ValueError("unknown middleware key(s): " + ", ".join(sorted(map(str, extra))))
        name = canonical(d.get("name"))
        if not name: raise ValueError(f"middleware '{str(d.get('name'))[:60]}' is not allowed (allowed: {', '.join(ALLOWED)}) / không được phép")
        if name in seen: raise ValueError(f"middleware {name} listed twice / trùng")
        seen.add(name)
        params = d.get("params") or {}
        if not isinstance(params, dict): raise ValueError(f"{name}.params must be an object")
        bad = set(params) - set(SPECS[name])
        if bad: raise ValueError(f"{name}: unknown param(s) {', '.join(sorted(map(str, bad)))} (allowed: {', '.join(SPECS[name])}) / tham số không hợp lệ")
        p = {k: _one(SPECS[name][k], f"{name}.{k}", v) for k, v in params.items() if v is not None}
        if name in ("ModelCallLimit", "ToolCallLimit") and not ({"thread_limit", "run_limit"} & set(p)): raise ValueError(f"{name} needs thread_limit or run_limit")
        if name == "Summarization" and not ({"trigger_tokens", "trigger_messages"} & set(p)): raise ValueError("Summarization needs trigger_tokens or trigger_messages")
        if name == "ModelFallback" and "models" not in p: raise ValueError("ModelFallback needs models")
        if name == "PII":
            import pii
            unknown = [e for e in p.get("entities", []) if e not in pii.ENTITIES]
            if unknown: raise ValueError("PII: unknown entities " + ", ".join(unknown) + " (allowed: " + ", ".join(pii.ENTITIES) + ")")
        if name == "ModelFallback" and not all(_MODEL_RE.match(m) for m in p["models"]): raise ValueError("ModelFallback: bad model name")
        out.append({"name": name, "params": p})
    return out

def split_fallback(items):
    """-> (items without ModelFallback, fallback model list | None): ModelFallback is mapped to AgentCfg.fallback_models (one mechanism, the gateway chain)."""
    rest, fb = [], None
    for m in items:
        n, p = (m["name"], m["params"]) if isinstance(m, dict) else (m.name, m.params)
        if n == "ModelFallback": fb = list(p["models"])
        else: rest.append(m)
    return rest, fb

# ------------------------------------------------------------------ demo / build
def is_demo_caller():
    import admin, gateway as gw
    try: return bool(admin.is_demo() or int(admin.cur_ws()) in gw.DEMO_WS)
    except Exception: return False  # noqa: BLE001

def refuse_demo(cfg):
    """Saving an agent that sets response_format / middleware from the demo workspace is refused (server-side, whatever the UI shows)."""
    if (cfg.response_format or cfg.middleware or cfg.chat_modes) and is_demo_caller():
        from fastapi import HTTPException
        raise HTTPException(403, "Không có trong bản demo / not available in the demo workspace")

async def pii_state(ws):
    """-> (entities to give PII middleware by default, entities the gateway (9.8) already masks, threshold). Demo / no config: (defaults, set(), 0.5)."""
    import pii, gateway as gw
    c = await pii.workspace_cfg(int(ws)) if ws and int(ws) not in gw.DEMO_WS else None
    return (list(c["entities"]) if c else list(pii.DEFAULT_ENTITIES)), (set(c["entities"]) if c and c.get("gateway") else set()), (c["threshold"] if c else 0.5)

class StructuredReply:
    """Built lazily (needs langchain): after a run that ended with the structured-output TOOL (ToolStrategy: the model answers by calling it, so no assistant text exists),
    append one assistant message with the JSON in a code block so the chat / run reply shows the result. With ProviderStrategy (Gemini / GPT native) the answer already is the JSON text: nothing added."""
    def __new__(cls, title):
        from langchain.agents.middleware import AgentMiddleware
        from langchain_core.messages import AIMessage, ToolMessage
        def _msg(state):
            sr, ms = state.get("structured_response"), state.get("messages") or []
            if sr is None or not ms or not isinstance(ms[-1], ToolMessage) or ms[-1].name != title: return None
            v = sr.model_dump() if hasattr(sr, "model_dump") else sr
            return {"messages": [AIMessage(content="```json\n" + json.dumps(v, ensure_ascii=False, indent=2, default=str) + "\n```")]}
        class _SR(AgentMiddleware):
            def after_agent(self, state, runtime): return _msg(state)
            async def aafter_agent(self, state, runtime): return _msg(state)
        return _SR()

async def build(cfg, ws, model):
    """-> (middleware instances in the order given, signature of the workspace masking they were built for). Never called for the demo workspace."""
    from langchain.agents import middleware as M
    out, sig = ([StructuredReply(cfg.response_format["title"])] if cfg.response_format else []), None
    for m in cfg.middleware:
        n, p = m.name, dict(m.params)
        if n == "ModelCallLimit": out.append(M.ModelCallLimitMiddleware(**p))
        elif n == "ToolCallLimit": out.append(M.ToolCallLimitMiddleware(**p))
        elif n == "ModelRetry": out.append(M.ModelRetryMiddleware(**p))
        elif n == "ToolRetry": out.append(M.ToolRetryMiddleware(**p))
        elif n == "Summarization":
            trig = [("tokens", p["trigger_tokens"])] if "trigger_tokens" in p else []
            trig += [("messages", p["trigger_messages"])] if "trigger_messages" in p else []
            out.append(M.SummarizationMiddleware(model, trigger=trig[0] if len(trig) == 1 else trig, keep=("messages", p.get("keep_messages", 20))))
        elif n == "ContextEditing":
            kw = {k: v for k, v in p.items() if k != "trigger_tokens"}
            if "exclude_tools" in kw: kw["exclude_tools"] = tuple(kw["exclude_tools"])
            out.append(M.ContextEditingMiddleware(edits=[M.ClearToolUsesEdit(**({"trigger": p["trigger_tokens"]} if "trigger_tokens" in p else {}), **kw)]))
        elif n == "PII":
            import pii
            default, gw_masked, thr = await pii_state(ws)
            sig = sorted(gw_masked)
            ents = [e for e in (p.get("entities") or default) if e not in gw_masked]   # the gateway already masks these for this workspace: do not mask twice
            if ents: out += pii.vn_pii_middleware(ents, p.get("strategy", "redact"), True, bool(p.get("apply_to_output")), bool(p.get("apply_to_tool_results")), thr)
    return out, sig
