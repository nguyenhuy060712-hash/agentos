"""GD7.R (23/10/2026): found with the real Gemini: a model call that hits the 120s client timeout raises an exception with an EMPTY message
(TimeoutError / httpx.ReadTimeout), which the gateway did not treat as retryable -> the whole deep-research run died with status=error.
In-process, fake model, $0, no network, no DB.
`_light` stubs every gateway hook that lazily imports admin/deepagent_api: that import fails outside the server, and each failed attempt
appended one more usage sink -> endless loop (7 GB RSS, WSL hang on 23/10)."""
import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")

def _light(monkeypatch, gw):
    async def nobudget(*a, **k): return []
    monkeypatch.setattr(gw, "budget_check", nobudget)
    monkeypatch.setattr(gw, "ctx_now", lambda *a, **k: {})
    monkeypatch.setattr(gw, "_cur_ws", lambda: None)
    monkeypatch.setattr(gw, "gemini_only", lambda: False)
    monkeypatch.setattr(gw, "_model", lambda name, temperature=None: object())
    monkeypatch.setattr(gw, "USAGE_SINK", [])
    monkeypatch.setattr(gw, "COOLDOWN", {})   # a timeout now cools the model down for 30 s; keep that state per test

def test_model_timeout_is_retried_then_succeeds(monkeypatch):
    import gateway as gw
    from langchain_core.messages import AIMessage
    class Req:
        def override(self, **kw): return self
    class Resp:
        result = [AIMessage(content="ok", usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})]
    calls = []
    async def handler(req):
        calls.append(1)
        if len(calls) == 1: raise TimeoutError()          # str(e) == "" like the real 120s timeout
        return Resp()
    async def nosleep(_): return None
    _light(monkeypatch, gw)
    monkeypatch.setattr(gw.asyncio, "sleep", nosleep)
    mw = gw.GatewayMiddleware(None, "gemini-3.6-flash", ["gemini-3-flash-preview"], 0.2)
    r = asyncio.run(mw.awrap_model_call(Req(), handler))
    assert isinstance(r, Resp) and len(calls) == 2

def test_non_retryable_error_still_raises(monkeypatch):
    import gateway as gw
    class Req:
        def override(self, **kw): return self
    async def handler(req): raise ValueError("bad request")
    _light(monkeypatch, gw)
    mw = gw.GatewayMiddleware(None, "gemini-3.6-flash", [], 0.2)
    try: asyncio.run(mw.awrap_model_call(Req(), handler))
    except ValueError: return
    raise AssertionError("ValueError must propagate")
