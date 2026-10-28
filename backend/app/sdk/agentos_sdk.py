"""agentos_sdk — trace bất kỳ code Python nào vào Moza Agent OS. Chỉ dùng thư viện chuẩn.

    import agentos_sdk as ao
    ao.configure(endpoint="http://127.0.0.1:8020", api_key="ak_...")   # hoặc biến môi trường AGENTOS_ENDPOINT / AGENTOS_API_KEY
    @ao.traceable(run_type="chain", tags=["demo"])
    def pipeline(q): ...
    with ao.trace("buoc-tay", run_type="tool", inputs={"x": 1}) as run: run.end(outputs={"y": 2})
    ao.feedback(run_id, "dung", score=1)

Hỗ trợ: lồng nhau (contextvars, cả async), sync/async/generator, metadata/tags, thread_id, usage token + model,
lấy mẫu (AGENTOS_SAMPLING_RATE), ẩn input/output (hide_inputs/hide_outputs), gửi theo lô ở luồng nền, flush khi thoát.
"""
import atexit, contextvars, datetime as _dt, functools, inspect, json, os, queue, random, threading, time, traceback, urllib.request, uuid

_cfg = {"endpoint": os.environ.get("AGENTOS_ENDPOINT", "http://127.0.0.1:8020"), "api_key": os.environ.get("AGENTOS_API_KEY", ""),
        "sampling": float(os.environ.get("AGENTOS_SAMPLING_RATE", "1")), "enabled": os.environ.get("AGENTOS_TRACING", "true").lower() != "false",
        "hide_inputs": None, "hide_outputs": None}
_current: contextvars.ContextVar = contextvars.ContextVar("agentos_run", default=None)
_q: "queue.Queue[dict]" = queue.Queue()
_errors: list = []

def configure(endpoint=None, api_key=None, sampling_rate=None, enabled=None, hide_inputs=None, hide_outputs=None):
    if endpoint: _cfg["endpoint"] = endpoint.rstrip("/")
    if api_key: _cfg["api_key"] = api_key
    if sampling_rate is not None: _cfg["sampling"] = sampling_rate
    if enabled is not None: _cfg["enabled"] = enabled
    if hide_inputs is not None: _cfg["hide_inputs"] = hide_inputs
    if hide_outputs is not None: _cfg["hide_outputs"] = hide_outputs

def _now(): return _dt.datetime.now(_dt.timezone.utc).isoformat()

def _safe(x, limit=20000):
    try:
        s = json.dumps(x, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        s = json.dumps(repr(x))
    if len(s) > limit: return {"_truncated": s[:limit]}
    return json.loads(s)

class Run:
    def __init__(self, name, run_type="chain", inputs=None, tags=None, metadata=None, parent=None, thread_id=None):
        self.id = str(uuid.uuid4()); self.parent = parent
        self.trace_id = parent.trace_id if parent else self.id
        self.sampled = parent.sampled if parent else (random.random() < _cfg["sampling"])
        self.name, self.run_type = name, run_type
        self.tags, self.metadata = list(tags or []), dict(metadata or {})
        if thread_id: self.metadata["thread_id"] = thread_id
        if parent and "thread_id" in parent.metadata and "thread_id" not in self.metadata: self.metadata["thread_id"] = parent.metadata["thread_id"]
        self.usage, self.model, self.ended = {}, None, False
        self.dotted = (parent.dotted + "." if parent else "") + time.strftime("%Y%m%dT%H%M%S") + f"{time.time() % 1:.6f}"[2:] + "Z" + self.id
        hi = _cfg["hide_inputs"]
        self._post({"id": self.id, "trace_id": self.trace_id, "parent_run_id": parent.id if parent else None, "name": name, "run_type": run_type,
                    "start_time": _now(), "inputs": _safe(hi(inputs) if hi else inputs), "tags": self.tags, "metadata": self.metadata,
                    "dotted_order": self.dotted})
    def _post(self, d):
        if _cfg["enabled"] and self.sampled: _q.put(("post", d))
    def set_usage(self, input_tokens=0, output_tokens=0, model=None):
        self.usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}; self.model = model or self.model
    def add_metadata(self, **kw): self.metadata.update(kw)
    def end(self, outputs=None, error=None):
        if self.ended: return
        self.ended = True; ho = _cfg["hide_outputs"]
        d = {"id": self.id, "end_time": _now(), "outputs": _safe(ho(outputs) if ho else outputs) if outputs is not None else None,
             "error": error, "metadata": self.metadata, "tags": self.tags}
        if self.usage: d["usage"] = self.usage
        if self.model: d["model"] = self.model
        if _cfg["enabled"] and self.sampled: _q.put(("patch", d))

def get_current_run(): return _current.get()

class trace:
    def __init__(self, name, run_type="chain", inputs=None, tags=None, metadata=None, thread_id=None):
        self.a = (name, run_type, inputs, tags, metadata, thread_id)
    def __enter__(self):
        n, rt, i, t, m, th = self.a
        self.run = Run(n, rt, i, t, m, _current.get(), th); self.tok = _current.set(self.run); return self.run
    def __exit__(self, et, ev, tb):
        _current.reset(self.tok)
        self.run.end(error=("".join(traceback.format_exception_only(et, ev)).strip() if et else None)); return False
    async def __aenter__(self): return self.__enter__()
    async def __aexit__(self, *a): return self.__exit__(*a)

def traceable(_fn=None, *, name=None, run_type="chain", tags=None, metadata=None):
    def deco(fn):
        nm = name or fn.__name__
        def inputs_of(args, kwargs):
            try:
                b = inspect.signature(fn).bind_partial(*args, **kwargs); return {k: v for k, v in b.arguments.items() if k != "self"}
            except Exception:  # noqa: BLE001
                return {"args": args, "kwargs": kwargs}
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def aw(*args, **kwargs):
                th = kwargs.pop("agentos_thread_id", None)
                with trace(nm, run_type, inputs_of(args, kwargs), tags, metadata, th) as run:
                    out = await fn(*args, **kwargs); run.end(outputs=out); return out
            return aw
        if inspect.isgeneratorfunction(fn):
            @functools.wraps(fn)
            def gw(*args, **kwargs):
                with trace(nm, run_type, inputs_of(args, kwargs), tags, metadata) as run:
                    items = []
                    for it in fn(*args, **kwargs):
                        items.append(it); yield it
                    run.end(outputs={"items": items[:200]})
            return gw
        @functools.wraps(fn)
        def w(*args, **kwargs):
            th = kwargs.pop("agentos_thread_id", None)
            with trace(nm, run_type, inputs_of(args, kwargs), tags, metadata, th) as run:
                out = fn(*args, **kwargs); run.end(outputs=out); return out
        return w
    return deco(_fn) if _fn else deco

def wrap_genai(client):
    """Bọc google-genai Client: mỗi generate_content thành một run 'llm' có token + model."""
    orig = client.models.generate_content
    @functools.wraps(orig)
    def gen(*a, model=None, contents=None, **kw):
        with trace("gemini.generate_content", "llm", {"model": model, "contents": contents}) as run:
            r = orig(*a, model=model, contents=contents, **kw)
            um = r.usage_metadata
            run.set_usage(um.prompt_token_count or 0, (um.candidates_token_count or 0) + (um.thoughts_token_count or 0), model)
            run.end(outputs={"text": r.text}); return r
    client.models.generate_content = gen; return client

def feedback(run_id, key, score=None, value=None, comment=None, correction=None):
    _send("/ingest/feedback", {"run_id": run_id, "key": key, "score": score, "value": value, "comment": comment, "correction": correction})

def attach(name, data, mime="application/octet-stream", run_id=None):
    """Đính kèm file (bytes hoặc đường dẫn) vào run (mặc định run hiện tại). Gọi sau khi run đã được gửi (flush())."""
    if isinstance(data, str) and os.path.exists(data):
        with open(data, "rb") as f: data = f.read()
    if isinstance(data, str): data = data.encode()
    rid = run_id or (get_current_run().id if get_current_run() else None)
    if not rid: raise ValueError("Không có run_id")
    flush()
    b = "----agentos" + uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"run_id\"\r\n\r\n{rid}\r\n"
            f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\nContent-Type: {mime}\r\n\r\n").encode() + data + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(_cfg["endpoint"] + "/ingest/attachments", data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}", "X-API-Key": _cfg["api_key"]})
    with urllib.request.urlopen(req, timeout=30) as r: return json.loads(r.read())

def log_experiment(dataset, experiment, results, metadata=None):
    """Gửi kết quả experiment chạy bên ngoài (vd pytest). results: [{inputs, outputs, reference, feedback:{k:v}, error, latency_ms, test_name}]"""
    req = urllib.request.Request(_cfg["endpoint"] + "/ingest/experiments", method="POST",
                                 data=json.dumps({"dataset": dataset, "experiment": experiment, "results": results, "metadata": metadata or {}}, default=str).encode(),
                                 headers={"Content-Type": "application/json", "X-API-Key": _cfg["api_key"]})
    with urllib.request.urlopen(req, timeout=60) as r: return json.loads(r.read())

def expect_for_run(run=None, embed=None):
    """LangSmith-style `expect` whose results become feedback on `run` (default: the current traced run), e.g.
        with ao.trace("answer") as run: ... ; ao.expect_for_run(run).value(answer).to_contain("Paris")   # feedback `expectation`
        ao.expect_for_run(run).edit_distance(answer, reference).to_be_less_than(0.3)                       # feedback `edit_distance`"""
    import agentos_evals as _ev
    def sink(key, score, comment):
        r = run or get_current_run()
        if r: feedback(r.id, key, score=score, comment=comment)
    return _ev.Expect(sink, embed)

# ---------------------------------------------------------------- evaluate(): LangSmith `evaluate(target, data, evaluators)` for any Python function / LangGraph graph
def _plain(x, depth=0):
    """LangChain messages / pydantic objects / dicts -> JSON-able data (duck-typed, no langchain import)."""
    if depth > 6: return str(x)[:500]
    if isinstance(x, (str, int, float, bool)) or x is None: return x
    if isinstance(x, dict): return {str(k): _plain(v, depth + 1) for k, v in x.items()}
    if isinstance(x, (list, tuple)): return [_plain(v, depth + 1) for v in x]
    if hasattr(x, "model_dump"):
        try: return _plain(x.model_dump(), depth + 1)
        except Exception: pass  # noqa: BLE001
    if hasattr(x, "content"): return {"type": getattr(x, "type", "message"), "content": _plain(x.content, depth + 1), **({"tool_calls": _plain(x.tool_calls, depth + 1)} if getattr(x, "tool_calls", None) else {}), **({"name": x.name} if getattr(x, "name", None) else {})}
    return str(x)[:500]

def graph_trajectory(state):
    """Tool calls the graph made, in order: [{"name", "args"}] read from state["messages"] (AIMessage.tool_calls or OpenAI-style dicts); write_todos is ignored."""
    out = []
    for m in (_plain(state).get("messages") if isinstance(_plain(state), dict) else None) or []:
        for c in (m.get("tool_calls") or []) if isinstance(m, dict) else []:
            f = c.get("function") or c; n = f.get("name") or c.get("name"); a = c.get("args", f.get("arguments"))
            if n and n != "write_todos": out.append({"name": n, "args": a})
    return out

def graph_answer(state):
    st = _plain(state)
    ms = st.get("messages") if isinstance(st, dict) else None
    if ms:
        for m in reversed(ms):
            if isinstance(m, dict) and m.get("content") and m.get("type", m.get("role")) in ("ai", "assistant", "AIMessage", "message"): return m["content"] if isinstance(m["content"], str) else json.dumps(m["content"], ensure_ascii=False)
        return str(ms[-1].get("content") if isinstance(ms[-1], dict) else ms[-1])
    return st if isinstance(st, str) else (st.get("answer") or st.get("output") if isinstance(st, dict) and (st.get("answer") or st.get("output")) else json.dumps(st, ensure_ascii=False, default=str))

def _call_evaluator(fn, inputs, outputs, reference, trajectory, error=None):
    """LangSmith evaluator signatures: any subset of (inputs, outputs, reference_outputs) plus (trajectory, error) -> bool | number | {key, score, comment} | list of those."""
    names = list(inspect.signature(fn).parameters)
    avail = {"inputs": inputs, "outputs": outputs, "reference_outputs": reference, "reference": reference, "trajectory": trajectory, "error": error,
             "run": {"inputs": inputs, "outputs": outputs, "trajectory": trajectory, "error": error}, "example": {"inputs": inputs, "outputs": reference}}
    return fn(**{n: avail[n] for n in names if n in avail}) if all(n in avail for n in names) else fn(inputs, outputs, reference)

def _feedback_items(res, default_key):
    if res is None: return []
    if isinstance(res, (bool, int, float)): return [(default_key, float(res))]
    if isinstance(res, dict):
        if "results" in res: return [i for r in res["results"] for i in _feedback_items(r, default_key)]
        sc = res.get("score", res.get("value"))
        return [(res.get("key") or default_key, float(sc) if isinstance(sc, (bool, int, float)) else sc)]
    if isinstance(res, (list, tuple)): return [i for r in res for i in _feedback_items(r, default_key)]
    return [(default_key, str(res))]

def evaluate(target, data, evaluators=(), *, dataset="sdk-eval", experiment=None, composites=(), metadata=None, trajectory_of=None, upload=True):
    """Run `target(inputs) -> outputs` over examples, score with `evaluators`, and (upload=True) log it as an experiment in Agent OS - the same flow as LangSmith evaluate().
    data: [{"inputs": {...}, "outputs": {...} (reference)}, ...]. evaluators: functions of (inputs, outputs, reference_outputs) and/or trajectory / error, returning
    bool | number | {"key", "score", "comment"} | list. composites: [{"key": "quality", "feedback_keys": [...], "weights": {...}, "aggregate": "average|sum|min|max|all_pass|formula", "formula": "0.7*a + 0.3*b"}]
    are computed from the scores above (skipped for a row that lacks a key, as LangSmith). trajectory_of(outputs) -> [{"name", "args"}] fills `trajectory`.
    Returns {"results": [...], "experiment_id"?: int, ...}."""
    import agentos_evals as _ev
    results = []
    for i, ex in enumerate(data):
        inputs, ref = ex.get("inputs") or {}, ex.get("outputs") or ex.get("reference")
        t0 = time.time(); outputs = err = None; traj = []
        try:
            outputs = target(inputs)
            traj = (trajectory_of(outputs) if trajectory_of else None) or []
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"[:1500]
        fb = {}
        for fn in evaluators:
            key = getattr(fn, "__name__", "score")
            try:
                for k, v in _feedback_items(_call_evaluator(fn, inputs, outputs, ref, traj, err), key): fb[k] = v
            except Exception as e:  # noqa: BLE001
                fb[key + "_error"] = str(e)[:200]
        for c in composites:
            ks = c["feedback_keys"]
            if all(isinstance(fb.get(k), (bool, int, float)) for k in ks):
                fb[c["key"]] = _ev.composite({k: fb[k] for k in ks}, c.get("weights"), c.get("aggregate", "average"), c.get("pass_threshold", 1.0), c.get("formula"))
        results.append({"test_name": f"example-{i + 1}", "inputs": inputs, "outputs": outputs if isinstance(outputs, dict) else {"answer": outputs}, "reference": ref,
                        "feedback": fb, "error": err, "latency_ms": int((time.time() - t0) * 1000), **({"trajectory": traj} if traj else {})})
    out = {"results": results}
    if upload: out.update(log_experiment(dataset, experiment or f"{dataset} {time.strftime('%d/%m %H:%M')}", results, {"source": "sdk.evaluate", **(metadata or {})}))
    return out

def evaluate_graph(app, data, evaluators=(), *, to_state=None, node=None, **kw):
    """LangSmith "evaluate a graph" for a LangGraph app (anything with .invoke): each example's inputs become the graph state (`to_state`, default
    {"messages": [{"role": "user", "content": <first string input>}]}), the whole state is `outputs`, and `trajectory` (tool calls in state["messages"]) is passed to evaluators
    that ask for it. node="agent" runs only that node (app.nodes[node]) with the same input, to save time and cost. Extra kwargs go to evaluate()."""
    def default_state(inputs):
        q = next((v for v in inputs.values() if isinstance(v, str)), json.dumps(inputs, ensure_ascii=False)) if isinstance(inputs, dict) else str(inputs)
        return {"messages": [{"role": "user", "content": q}]}
    mk = to_state or default_state
    if node:
        n = app.nodes[node]; runner = getattr(n, "invoke", None) or n.bound.invoke
    else: runner = app.invoke
    def target(inputs):
        st = _plain(runner(mk(inputs)))
        return {"answer": graph_answer(st), "state": st}
    kw.setdefault("trajectory_of", lambda o: graph_trajectory(o.get("state") if isinstance(o, dict) else o))
    return evaluate(target, data, evaluators, **kw)

def _send(path, body):
    req = urllib.request.Request(_cfg["endpoint"] + path, data=json.dumps(body, default=str).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-API-Key": _cfg["api_key"]})
    with urllib.request.urlopen(req, timeout=15) as r: return r.status

def flush(timeout=10):
    t = time.time()
    while not _q.empty() and time.time() - t < timeout: _drain()

def _drain():
    post, patch = [], []
    try:
        while len(post) + len(patch) < 200:
            k, d = _q.get_nowait(); (post if k == "post" else patch).append(d)
    except queue.Empty:
        pass
    if post or patch:
        try:
            _send("/ingest/runs", {"post": post, "patch": patch})
        except Exception as e:  # noqa: BLE001
            _errors.append(str(e)[:200])

def _loop():
    while True:
        time.sleep(0.5)
        _drain()

threading.Thread(target=_loop, daemon=True, name="agentos-flush").start()
atexit.register(flush)
