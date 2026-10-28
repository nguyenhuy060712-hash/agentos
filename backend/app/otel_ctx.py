"""GĐ9.10 W3C Trace Context (https://www.w3.org/TR/trace-context/): `traceparent` / `tracestate`.
Incoming: a pure-ASGI middleware parses the header into a ContextVar (the run recorded by obs.record_agent_trace continues that trace: same trace id,
our span id pre-allocated here, the caller's span = external parent). Outgoing: every httpx request made while a context is active (tools, gateway upstream,
webhooks, channel sends, MCP/A2A calls) gets `traceparent` (our span id) + `tracestate` unless the caller already set one. Header names/format only —
no OTel SDK needed on the request path."""
import contextvars, os, re
from dataclasses import dataclass

_TP = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})(-.*)?$")

@dataclass(frozen=True)
class TraceCtx:
    trace_id: str            # 32 hex
    parent_span_id: str      # 16 hex: the caller's span (None when we started the trace ourselves)
    span_id: str             # 16 hex: OUR span id (becomes the root run id suffix, see run_id_for)
    flags: str = "01"
    tracestate: str = ""
    raw: str = ""

CTX: contextvars.ContextVar = contextvars.ContextVar("aos_traceparent", default=None)

def new_span_id() -> str: return os.urandom(8).hex()
def new_trace_id() -> str: return os.urandom(16).hex()

def parse(tp: str | None, ts: str | None = None):
    """Valid traceparent -> TraceCtx (fresh own span id); anything else -> None (the spec says: ignore invalid headers and start a new trace)."""
    m = _TP.match((tp or "").strip().lower())
    if not m: return None
    ver, tid, sid, fl, rest = m.groups()
    if ver == "ff" or (ver == "00" and rest) or tid == "0" * 32 or sid == "0" * 16: return None
    return TraceCtx(tid, sid, new_span_id(), fl, (ts or "").strip()[:512], (tp or "").strip())

def fmt(trace_id: str, span_id: str, flags: str = "01") -> str: return f"00-{trace_id}-{span_id}-{flags}"

def run_id_for(trace_id: str, span_id: str) -> str:
    """Run uuid for a span: first 16 hex of the trace id + the 16-hex span id (same mapping as the OTLP ingest, so the span id is recoverable from the run id)."""
    import uuid
    return str(uuid.UUID(hex=trace_id[:16] + span_id))

def span_of_run(run_id) -> str: return str(run_id).replace("-", "")[16:]
def trace_hex(trace_uuid) -> str: return str(trace_uuid).replace("-", "")

def current() -> TraceCtx | None: return CTX.get()

def outgoing_headers(ctx: TraceCtx | None = None) -> dict:
    c = ctx or CTX.get()
    if not c: return {}
    h = {"traceparent": fmt(c.trace_id, c.span_id, c.flags)}
    if c.tracestate: h["tracestate"] = c.tracestate
    return h

class TraceparentMiddleware:
    """Outermost ASGI middleware: sets CTX for the whole request (also inside tasks the handler spawns: they copy the context)."""
    def __init__(self, app): self.app = app
    async def __call__(self, scope, receive, send):
        if scope["type"] != "http": return await self.app(scope, receive, send)
        tp = ts = None
        for k, v in scope.get("headers") or []:
            if k == b"traceparent": tp = v.decode("latin-1")
            elif k == b"tracestate": ts = v.decode("latin-1")
        c = parse(tp, ts) if tp else None
        if not c: return await self.app(scope, receive, send)
        tok = CTX.set(c)
        try: await self.app(scope, receive, send)
        finally: CTX.reset(tok)

def install_httpx():
    """Idempotent: wrap httpx.Client.send / AsyncClient.send so outgoing requests carry the active trace context. Never overwrites a header the caller set."""
    import httpx
    def _inject(req):
        c = CTX.get()
        if c and "traceparent" not in req.headers:
            for k, v in outgoing_headers(c).items(): req.headers[k] = v
    if not getattr(httpx.AsyncClient.send, "_aos_tp", False):
        a0 = httpx.AsyncClient.send
        async def asend(self, request, *a, **k):
            _inject(request); return await a0(self, request, *a, **k)
        asend._aos_tp = True; httpx.AsyncClient.send = asend
    if not getattr(httpx.Client.send, "_aos_tp", False):
        s0 = httpx.Client.send
        def ssend(self, request, *a, **k):
            _inject(request); return s0(self, request, *a, **k)
        ssend._aos_tp = True; httpx.Client.send = ssend

def install_stdlib():
    """Idempotent: same propagation for `requests` (if installed) and `urllib.request` (also used by http clients built on them). No app code uses either today
    (grep 24/10: only httpx); this guards future code and third-party libs. aiohttp (Slack/Telegram SDKs) is left alone: those calls go to the vendor, not to our services."""
    def _hdrs(h):
        c = CTX.get()
        if c and not any(k.lower() == "traceparent" for k in h): return outgoing_headers(c)
        return {}
    try:
        import requests
        if not getattr(requests.Session.send, "_aos_tp", False):
            r0 = requests.Session.send
            def rsend(self, request, **k):
                for n, v in _hdrs(request.headers).items(): request.headers[n] = v
                return r0(self, request, **k)
            rsend._aos_tp = True; requests.Session.send = rsend
    except ImportError: pass
    import urllib.request as ur
    if not getattr(ur.OpenerDirector.open, "_aos_tp", False):
        u0 = ur.OpenerDirector.open
        def uopen(self, fullurl, data=None, timeout=ur.socket._GLOBAL_DEFAULT_TIMEOUT):
            if isinstance(fullurl, str): fullurl = ur.Request(fullurl)
            for n, v in _hdrs(dict(fullurl.header_items())).items(): fullurl.add_unredirected_header(n, v)
            return u0(self, fullurl, data, timeout)
        uopen._aos_tp = True; ur.OpenerDirector.open = uopen

# ---- queued runs: the request context is saved in the run's metadata and restored in the queue worker (a separate task / process)
def run_meta() -> dict:
    c = CTX.get()
    return {"_otel": {"tp": c.raw, "ts": c.tracestate, "span": c.span_id}} if c else {}

def restore(md: dict | None):
    o = (md or {}).get("_otel")
    c = parse(o.get("tp"), o.get("ts")) if isinstance(o, dict) else None
    if c and isinstance(o.get("span"), str) and re.fullmatch(r"[0-9a-f]{16}", o["span"]): c = TraceCtx(c.trace_id, c.parent_span_id, o["span"], c.flags, c.tracestate, c.raw)
    CTX.set(c)
