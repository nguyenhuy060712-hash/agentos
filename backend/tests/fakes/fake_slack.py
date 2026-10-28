"""Fake Slack (Web API + Socket Mode + file downloads/uploads) shared by tests/test_gd8_slack.py (GĐ8.4) and tests/test_gd12_slack.py (GĐ12.74/77/78). aiohttp on 127.0.0.1:<port>
in its own thread. Records every Web API call; apps.connections.open hands out ws://127.0.0.1:<port>/link/?ticket=…; push() sends envelopes to the open Socket Mode connections
of an app token and records the acks. GĐ12 additions: views.open / views.update, view_submit() (a view_submission envelope; the ack payload = the modal answer), upload V2
(files.getUploadURLExternal -> POST /upload/<id> -> files.completeUploadExternal). Several ports can run side by side (the server under test takes SLACK_API_BASE)."""
import asyncio, json, threading, time, uuid
from aiohttp import web, WSMsgType


class FakeSlack:
    def __init__(self, port=8783, bot_token="xoxb-1-fake-faketoken", app_token="xapp-1-A1-fake-faketoken", bot_user="UBOT42", sock_wait=90):
        self.port, self.bot_token, self.app_token, self.bot_user, self.sock_wait = port, bot_token, app_token, bot_user, sock_wait
        self.calls, self.files, self.tickets, self.socks, self.acks, self.downloads = [], {}, {}, [], [], []
        self.uploads, self.pending_uploads, self.completed, self.upload_fail = {}, {}, [], False

    async def api(self, req):
        m = req.match_info["method"]
        data = dict(req.query)
        if req.can_read_body:
            data.update(await req.json() if req.content_type == "application/json" else dict(await req.post()))
        tok = (req.headers.get("Authorization") or "").removeprefix("Bearer ").strip() or str(data.get("token") or "")
        rec = {"method": m, "token": tok, **data}; self.calls.append(rec)
        ok = lambda **r: web.json_response({"ok": True, **r})
        if m == "auth.test": return ok(url="https://shop.slack.com/", team="Shop", user="shopbot", team_id="T1", user_id=self.bot_user, bot_id="BBOT42")
        if m == "apps.connections.open":
            if not tok.startswith("xapp-"): return web.json_response({"ok": False, "error": "not_allowed_token_type"})
            t = uuid.uuid4().hex; self.tickets[t] = tok
            return ok(url=f"ws://127.0.0.1:{self.port}/link/?ticket={t}")
        if m in ("chat.postMessage", "chat.update"):
            ts = data.get("ts") or f"{time.time():.6f}"
            msg = {"type": "message", "user": self.bot_user, "bot_id": "BBOT42", "ts": ts, "text": data.get("text"), **({"thread_ts": data["thread_ts"]} if data.get("thread_ts") else {}),
                   **({"blocks": data["blocks"]} if data.get("blocks") else {})}
            rec["_result"] = msg; return ok(channel=data.get("channel"), ts=ts, message=msg)
        if m == "files.info":
            f = self.files.get(data.get("file"))
            if not f: return web.json_response({"ok": False, "error": "file_not_found"})
            return ok(file={"id": data["file"], "name": f[0], "size": len(f[1]), "url_private": f"http://127.0.0.1:{self.port}/files/{data['file']}"})
        if m in ("views.open", "views.update"):
            if not data.get("view"): return web.json_response({"ok": False, "error": "invalid_arguments"})
            return ok(view={"id": "V" + uuid.uuid4().hex[:8], **(data["view"] if isinstance(data["view"], dict) else {})})
        if m == "files.getUploadURLExternal":
            if self.upload_fail: return web.json_response({"ok": False, "error": "internal_error"})
            fid = "FUP" + uuid.uuid4().hex[:8].upper(); self.pending_uploads[fid] = {"filename": data.get("filename"), "length": int(data.get("length") or 0)}
            return ok(upload_url=f"http://127.0.0.1:{self.port}/upload/{fid}", file_id=fid)
        if m == "files.completeUploadExternal":
            files = json.loads(data["files"]) if isinstance(data.get("files"), str) else (data.get("files") or [])
            rec["_files"] = files; self.completed.append({"files": files, "channel_id": data.get("channel_id"), "thread_ts": data.get("thread_ts"), "initial_comment": data.get("initial_comment")})
            return ok(files=[{"id": f["id"], "title": f.get("title")} for f in files])
        return web.json_response({"ok": False, "error": "unknown_method"})

    async def file(self, req):
        self.downloads.append(req.headers.get("Authorization"))
        f = self.files.get(req.match_info["fid"])
        if not f or req.headers.get("Authorization") != f"Bearer {self.bot_token}": return web.Response(text="<html>login</html>", content_type="text/html")
        return web.Response(body=f[1])

    async def upload(self, req):
        fid = req.match_info["fid"]
        if fid not in self.pending_uploads: return web.Response(status=404)
        self.uploads[fid] = {**self.pending_uploads[fid], "data": await req.read(), "auth": req.headers.get("Authorization")}
        return web.Response(text="OK - %d" % len(self.uploads[fid]["data"]))

    async def link(self, req):
        ws = web.WebSocketResponse(autoping=True); await ws.prepare(req)
        s = {"ws": ws, "token": self.tickets.pop(req.query.get("ticket"), None), "open": True}; self.socks.append(s)
        if not s["token"]: await ws.close(); s["open"] = False; return ws
        await ws.send_str(json.dumps({"type": "hello", "num_connections": 1, "connection_info": {"app_id": "A1"}}))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT: self.acks.append(json.loads(msg.data))
        finally: s["open"] = False
        return ws

    def open_socks(self, token=None): return [s for s in list(self.socks) if s["open"] and s["token"] == (token or self.app_token)]

    def push(self, typ, payload, token=None):
        token = token or self.app_token
        eid = uuid.uuid4().hex; env = json.dumps({"envelope_id": eid, "type": typ, "payload": payload, "accepts_response_payload": typ != "events_api"})
        socks = self.open_socks(token); t0 = time.time()
        while not socks and time.time() - t0 < self.sock_wait: time.sleep(0.25); socks = self.open_socks(token)   # the server re-dials (restart / lock hand-over)
        assert socks, f"no open Socket Mode connection for {self.sock_wait}s (apps.connections.open calls so far: {len(self.sent('apps.connections.open'))})"
        for s in socks: asyncio.run_coroutine_threadsafe(s["ws"].send_str(env), self.loop).result(5)
        return eid

    def event(self, ev, token=None):
        return self.push("events_api", {"token": "v", "team_id": "T1", "api_app_id": "A1", "type": "event_callback", "event_id": "Ev" + uuid.uuid4().hex[:8],
                                         "event_time": int(time.time()), "event": ev}, token)

    def view_submit(self, view_id_meta, values, user, callback_id="aos_ask", token=None):
        """A modal submit: values = {block_id: {action_id: element state}} as Slack sends them in view.state.values. -> envelope id (the ack carries the response payload)."""
        return self.push("interactive", {"type": "view_submission", "team": {"id": "T1"}, "user": user, "api_app_id": "A1", "token": "v", "trigger_id": "tr",
                                         "view": {"id": "V" + uuid.uuid4().hex[:8], "type": "modal", "callback_id": callback_id, "private_metadata": str(view_id_meta),
                                                  "state": {"values": values}, "hash": "h"}}, token)

    def ack_of(self, eid):
        """The ack the server sent for an envelope (None until it arrives): {"envelope_id": …, "payload": {...}?}."""
        return next((a for a in list(self.acks) if a.get("envelope_id") == eid), None)

    def start(self):
        self.loop = asyncio.new_event_loop(); ready = threading.Event()
        def run():
            asyncio.set_event_loop(self.loop)
            app = web.Application(client_max_size=50 << 20)
            app.router.add_route("*", "/api/{method}", self.api); app.router.add_get("/files/{fid}", self.file); app.router.add_get("/link/", self.link)
            app.router.add_post("/upload/{fid}", self.upload)
            self.runner = web.AppRunner(app); self.loop.run_until_complete(self.runner.setup())
            self.loop.run_until_complete(web.TCPSite(self.runner, "127.0.0.1", self.port).start()); ready.set(); self.loop.run_forever()
        threading.Thread(target=run, daemon=True).start(); assert ready.wait(10)

    def stop(self):
        async def close_all():
            for s in self.socks:
                if s["open"]: await s["ws"].close()
            await self.runner.cleanup()
        asyncio.run_coroutine_threadsafe(close_all(), self.loop).result(10); self.loop.call_soon_threadsafe(self.loop.stop)

    def sent(self, method, channel=None):
        return [c for c in list(self.calls) if c["method"] == method and (channel is None or c.get("channel") == channel)]
