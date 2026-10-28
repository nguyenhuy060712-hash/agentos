"""GĐ11 fake LLM provider servers ($0): OpenAI shape (/v1/models, /v1/chat/completions), Anthropic shape (/v1/messages, /v1/models) and Gemini shape
(/v1beta/models, :generateContent) on 127.0.0.1, in a background thread (uvicorn). Knobs: cache (read, write tokens), status (every call answers it, body echoes the key like real vendors do),
latency (s), usage (input, output tokens), key (what a valid key is; anything else = 401). Every request is kept in `.calls` (path, lower-cased headers, json body).
Tests set AOS_EGRESS_ALLOW=127.0.0.1 (conftest does). 11.3 / 11.10 / 11.13 reuse it."""
import asyncio, socket, threading, time
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

class FakeProviders:
    def __init__(self, key="sk-fake-provider-key-1234", status=200, latency=0.0, usage=(5, 2), models=("fake-model-a", "fake-model-b"), cache=(0, 0)):
        self.key, self.status, self.latency, self.usage, self.models, self.calls = key, status, latency, usage, list(models), []
        self.cache = cache   # (cache read tokens, cache write tokens) reported in the Anthropic usage (11.10); 0 = field absent
        app = Starlette(routes=[Route("/{path:path}", self._handle, methods=["GET", "POST"])])
        self._sock = socket.socket(); self._sock.bind(("127.0.0.1", 0)); self.port = self._sock.getsockname()[1]
        self._srv = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="off"))
        self._th = threading.Thread(target=lambda: asyncio.run(self._srv.serve([self._sock])), daemon=True)

    def start(self):
        self._th.start()
        for _ in range(200):
            if self._srv.started: return self
            time.sleep(0.02)
        raise RuntimeError("fake provider did not start")

    def stop(self): self._srv.should_exit = True; self._th.join(5)
    def __enter__(self): return self.start()
    def __exit__(self, *a): self.stop()
    @property
    def base(self): return f"http://127.0.0.1:{self.port}"
    def url(self, kind="openai"): return self.base + "/v1" if kind in ("openai", "openai_compat") else self.base

    def _auth_ok(self, h, path):
        if path.startswith("/v1beta"): return h.get("x-goog-api-key") == self.key
        if path == "/v1/messages" or h.get("x-api-key") is not None: return h.get("x-api-key") == self.key
        return h.get("authorization") == "Bearer " + self.key

    async def _handle(self, request):
        path = "/" + request.path_params["path"]; h = {k.lower(): v for k, v in request.headers.items()}
        body = await request.json() if request.method == "POST" and await request.body() else None
        self.calls.append({"path": path, "headers": h, "body": body, "method": request.method})
        if self.latency: await asyncio.sleep(self.latency)
        sent = h.get("x-api-key") or h.get("x-goog-api-key") or (h.get("authorization") or "").removeprefix("Bearer ")
        if self.status != 200: return JSONResponse({"error": {"message": f"Incorrect API key provided: {sent}", "type": "x"}}, self.status)
        if not self._auth_ok(h, path): return JSONResponse({"error": {"message": f"Invalid key {sent}", "type": "auth"}}, 401)
        i, o = self.usage
        if path == "/v1/models": return JSONResponse({"data": [{"id": m, "object": "model"} for m in self.models]})
        if path == "/v1beta/models": return JSONResponse({"models": [{"name": "models/" + m} for m in self.models]})
        if path == "/v1/chat/completions":
            return JSONResponse({"id": "c1", "object": "chat.completion", "model": body.get("model"), "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "pong"}}],
                                 "usage": {"prompt_tokens": i, "completion_tokens": o, "total_tokens": i + o}})
        if path == "/v1/messages":
            return JSONResponse({"id": "m1", "type": "message", "role": "assistant", "model": body.get("model"), "content": [{"type": "text", "text": "pong"}], "stop_reason": "end_turn",
                                 "usage": {"input_tokens": i, "output_tokens": o, **({"cache_read_input_tokens": self.cache[0]} if self.cache[0] else {}), **({"cache_creation_input_tokens": self.cache[1]} if self.cache[1] else {})}})
        if path.startswith("/v1beta/models/") and path.endswith(":generateContent"):
            return JSONResponse({"candidates": [{"content": {"parts": [{"text": "pong"}], "role": "model"}, "finishReason": "STOP"}],
                                 "usageMetadata": {"promptTokenCount": i, "candidatesTokenCount": o, "totalTokenCount": i + o}})
        return JSONResponse({"error": {"message": "not found"}}, 404)
