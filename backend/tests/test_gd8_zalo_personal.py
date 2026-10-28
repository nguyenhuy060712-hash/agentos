"""GĐ8.3b (24/10/2026): Zalo PERSONAL channel (app/channel_zalo_personal.py + sidecars/zalo-personal). Everything is fake: the local server runs the REAL
Node sidecar in ZALO_PERSONAL_MOCK=1 mode (fake zca client, no Zalo network, no real QR); tests drive it through the admin mock endpoints, so every
message travels sidecar -> /internal/zalo-personal/{id}/incoming -> agent (fake LLM, $0) -> backend limiter -> sidecar /send.
Pure unit tests run in this process; server tests run in file order and share state through the `zp` fixture (local server :8020)."""
import asyncio, glob, json, os, re, socket, subprocess, sys, threading, time, uuid
import httpx, psycopg, pytest
from aiohttp import web
from test_fixes import B, ADMIN, a  # noqa: F401  (fixtures)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
PG = os.environ.setdefault("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
LOG = os.path.join(os.path.dirname(__file__), "..", "var", "server.log")
SIDECAR = os.path.join(os.path.dirname(__file__), "..", "sidecars", "zalo-personal")
PW = "phase0-gd83b-local"; SUF = uuid.uuid4().hex[:6]; FILE_PORT = 8786
PNG = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360f8cf00000301010018dd8db00000000049454e44ae426082")

def pg(): return psycopg.connect(PG, autocommit=True)

T0 = int(time.time() * 1000) - 2000       # the sidecar keeps its mock "sent" list between server runs: only look at what THIS run sent

def wait_for(fn, timeout=90, every=0.25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = fn()
        if r: return r
        time.sleep(every)
    raise AssertionError("timed out waiting")

def log_text():
    with open(LOG, encoding="utf-8", errors="replace") as f: return f.read()

def log_has(text): return text in log_text()

def _node():
    n = os.environ.get("AOS_ZP_NODE") or (sorted(glob.glob(os.path.expanduser("~/tools/node-v24*-linux-x64/bin/node"))) or [None])[-1]
    return n or __import__("shutil").which("node")

# ------------------------------------------------------------------ pure unit tests
def test_limits_can_only_be_stricter():
    import channel_zalo_personal as Z
    assert Z.limits({}) == (2.0, 200)
    assert Z.limits({"min_interval_s": 0.1, "daily_cap": 5000}) == (2.0, 200)          # never looser than the defaults
    assert Z.limits({"min_interval_s": 10, "daily_cap": 20}) == (10.0, 20)
    assert Z.limits({"min_interval_s": "x", "daily_cap": None}) == (2.0, 200)

def test_parse_incoming_dm_group_files():
    import channel_zalo_personal as Z
    m = Z.ADAPTER.parse_incoming({"thread_id": "11", "sender_id": "11", "sender_name": "An", "text": "chào", "is_group": False, "mentioned": False})
    assert (m.chat_id, m.user_id, m.user_name, m.is_group) == ("11", "11", "An", False)
    g = Z.ADAPTER.parse_incoming({"thread_id": "77", "sender_id": "11", "sender_name": "An", "text": "hi", "is_group": True, "mentioned": True})
    assert g.chat_id == "grp:77" and g.user_name == "77" and g.is_group and g.mentioned          # allow-list can name the group
    f = Z.ADAPTER.parse_incoming({"thread_id": "11", "sender_id": "11", "text": "", "files": [{"kind": "image", "url": "https://x/a.jpg", "name": "a.jpg"}, {"kind": "video", "url": "https://x/v"}]})
    assert f.files == [{"kind": "image", "url": "https://x/a.jpg", "name": "a.jpg", "size": None}]
    assert Z.ADAPTER.parse_incoming({"thread_id": "11", "sender_id": "11", "text": "  "}) is None and Z.ADAPTER.parse_incoming({"text": "x"}) is None

def test_off_without_env(monkeypatch):
    """No AOS_ZALO_PERSONAL=1 -> kind unavailable, create refused (422), no supervisor work, no sidecar process."""
    import channel_zalo_personal as Z, channels as ch
    from fastapi import HTTPException
    monkeypatch.delenv("AOS_ZALO_PERSONAL", raising=False)
    assert not Z.enabled() and not Z.ADAPTER.available()
    b = ch.BindingIn(kind="zalo_personal", config={"accept_risk": True})
    with pytest.raises(HTTPException) as e: asyncio.run(Z.ADAPTER.prepare_create({"role": "admin", "username": "x", "ws": 1}, b))
    assert e.value.status_code == 422
    async def go():
        t = Z.start(); await t                       # completes at once, no supervisor loop
        await Z._tick()                              # nothing to do and no DB access when off
        with pytest.raises(Z.ZpError): await Z.ensure_sidecar()
    asyncio.run(go()); assert not Z._running()

def test_demo_workspace_gate(monkeypatch):
    import channels as ch, gateway as gw
    monkeypatch.setattr(gw, "DEMO_WS", set(gw.DEMO_WS) | {987654})
    assert ch._off(987654) and ch._gate({"status": "active", "workspace_id": 987654, "allowed_users": []}, "u") == "demo workspace"

@pytest.mark.skipif(not _node(), reason="no node binary (set AOS_ZP_NODE)")
def test_real_sidecar_starts_in_mock_mode_and_is_token_guarded():
    """The real index.mjs: starts, binds 127.0.0.1, refuses calls without the token, serves /status and a fake QR, exits when its parent pipe closes."""
    with socket.socket() as s: s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
    env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"), "ZP_PORT": str(port), "ZP_TOKEN": "t" * 32, "ZALO_PERSONAL_MOCK": "1",
           "ZP_WATCH_STDIN": "1", "ZP_BACKEND": "http://127.0.0.1:9"}
    p = subprocess.Popen([_node(), "index.mjs"], cwd=SIDECAR, env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base, hdr = f"http://127.0.0.1:{port}", {"x-zp-token": "t" * 32}
        d = wait_for(lambda: _try(lambda: httpx.get(base + "/status", headers=hdr, timeout=2).json()), timeout=15)
        assert d == {"ok": True, "mock": True, "sessions": {}}
        assert httpx.get(base + "/status", timeout=2).status_code == 401 and httpx.get(base + "/status", headers={"x-zp-token": "bad"}, timeout=2).status_code == 401
        q = httpx.post(base + "/login/qr", json={"binding_id": 1}, headers=hdr, timeout=10).json()
        assert q["status"] == "qr" and q["qr"].startswith("data:image/png;base64,")
        import base64; assert base64.b64decode(q["qr"].split(",", 1)[1])[:8] == b"\x89PNG\r\n\x1a\n"
        assert httpx.post(base + "/mock/scan", json={"binding_id": 1}, headers=hdr, timeout=5).json() == {"ok": True}
        assert wait_for(lambda: httpx.get(base + "/status?binding_id=1", headers=hdr, timeout=2).json()["status"] == "logged_in", timeout=10)
        assert httpx.post(base + "/send", json={"binding_id": 1, "thread_id": "5", "text": "x"}, headers=hdr, timeout=5).status_code == 200
        assert httpx.post(base + "/send", json={"binding_id": 9, "thread_id": "5", "text": "x"}, headers=hdr, timeout=5).status_code == 409   # no session
        p.stdin.close(); assert p.wait(timeout=5) == 0                                                     # parent pipe closed -> exits
    finally:
        if p.poll() is None: p.kill()

def _try(fn):
    try: return fn()
    except Exception: return None

# ------------------------------------------------------------------ fake file host (images / files sent by the "friend")
FILES: dict = {}
def _start_files():
    loop = asyncio.new_event_loop(); ready = threading.Event(); box = {}
    async def h(req):
        d = FILES.get(req.match_info["n"]); return web.Response(body=d) if d is not None else web.Response(status=404)
    def run():
        asyncio.set_event_loop(loop); app = web.Application(); app.router.add_get("/f/{n}", h)
        box["r"] = web.AppRunner(app); loop.run_until_complete(box["r"].setup()); loop.run_until_complete(web.TCPSite(box["r"], "127.0.0.1", FILE_PORT).start()); ready.set(); loop.run_forever()
    threading.Thread(target=run, daemon=True).start(); assert ready.wait(10)
    return lambda: (asyncio.run_coroutine_threadsafe(box["r"].cleanup(), loop).result(10), loop.call_soon_threadsafe(loop.stop))

# ------------------------------------------------------------------ against the local server (fake sidecar client, fake LLM)
@pytest.fixture(scope="module")
def zp(a):
    stop_files = _start_files()
    ws = a.post("/api/admin/workspaces", json={"name": f"zp-w-{SUF}"}).json()["id"]
    adm_name, ed_name = f"zp_adm_{SUF}", f"zp_ed_{SUF}"
    for n, role in ((adm_name, "admin"), (ed_name, "editor")):
        r = a.post("/api/admin/users", json={"username": n, "password": PW, "role": role, "workspace_id": ws}); assert r.status_code == 200, r.text
    adm = httpx.Client(base_url=B, auth=(adm_name, PW), timeout=150); ed = httpx.Client(base_url=B, auth=(ed_name, PW), timeout=150)
    r = adm.post("/api/agents", json={"name": f"zp-agent-{SUF}", "system_prompt": "Bạn là trợ lý bán hàng.", "tools": ["local:content_publish"],
                                       "interrupt_on": ["local:content_publish"]}); assert r.status_code == 200, r.text
    e = {"ws": ws, "adm": adm, "ed": ed, "agent": r.json()["id"], "adm_name": adm_name}
    yield e
    adm.close(); ed.close(); stop_files()
    with pg() as c:
        c.execute("DELETE FROM channel_bindings WHERE workspace_id=%s", (ws,))
        c.execute("DELETE FROM approvals WHERE agent_id=%s", (e["agent"],))
        c.execute("DELETE FROM lg_runs WHERE workspace_id=%s", (ws,))
        c.execute("DELETE FROM lg_threads WHERE workspace_id=%s", (ws,))
        c.execute("DELETE FROM agents WHERE workspace_id=%s", (ws,))
        c.execute("DELETE FROM ws_secrets WHERE workspace_id=%s", (ws,))
        c.execute("DELETE FROM users WHERE username IN (%s,%s)", (adm_name, ed_name))

def ch_url(zp, tail=""): return f"/api/channels/{zp['bid']}/zalo-personal/{tail}"
def inject(zp, **m): r = zp["adm"].post(ch_url(zp, "mock/incoming"), json={"message": m}); assert r.status_code == 200, r.text
def sent(zp, tid):
    r = zp["adm"].get(ch_url(zp, "mock/sent"))
    return [m for m in r.json()["sent"] if m["thread_id"] == str(tid) and m["at"] >= T0] if r.status_code == 200 else []   # 503 = sidecar not started yet
def cfg(zp, **c): r = zp["adm"].patch(f"/api/channels/{zp['bid']}", json={"config": {"timeout_s": 120, **c}}); assert r.status_code == 200, r.text
def dm(zp, uid, text, **kw): inject(zp, thread_id=str(uid), sender_id=str(uid), sender_name=f"Khách {uid}", text=text, **kw)

def test_capabilities_and_create_rules(zp):
    assert zp["adm"].get("/api/channels/zalo-personal/capabilities").json() == {"enabled": True, "mock": True}
    path = f"/api/agents/{zp['agent']}/channels"
    r = zp["adm"].post(path, json={"kind": "zalo_personal", "name": "no ack"}); assert r.status_code == 422 and "accept_risk" in r.text      # warning must be accepted
    r = zp["adm"].post(path, json={"kind": "zalo_personal", "config": {"accept_risk": False}}); assert r.status_code == 422
    r = zp["ed"].post(path, json={"kind": "zalo_personal", "config": {"accept_risk": True}}); assert r.status_code == 403               # admin only
    assert zp["ed"].get("/api/channels/zalo-personal/capabilities").json()["enabled"] is False
    r = zp["adm"].post(path, json={"kind": "zalo_personal", "name": "Số phụ", "config": {"accept_risk": True, "daily_cap": 5000, "min_interval_s": 0.1, "timeout_s": 120}})
    assert r.status_code == 200, r.text
    b = r.json(); zp["bid"] = b["id"]
    assert b["secret_ref"].startswith("ZALOP_") and b["config"]["daily_cap"] == 200 and b["config"]["min_interval_s"] == 2.0 and b["config"]["risk_ack_by"] == zp["adm_name"]
    assert "accept_risk" not in b["config"]
    assert zp["ed"].patch(f"/api/channels/{zp['bid']}", json={"status": "disabled"}).status_code == 403                                  # editor cannot toggle
    r = zp["adm"].patch(f"/api/channels/{zp['bid']}", json={"secret_ref": "other"}); assert r.status_code == 422
    r = zp["adm"].patch(f"/api/channels/{zp['bid']}", json={"config": {"daily_cap": 9999}}); assert r.status_code == 200 and r.json()["config"]["risk_ack_by"] == zp["adm_name"]
    cfg(zp)

def test_internal_route_needs_token_and_loopback(zp):
    url = f"{B}/internal/zalo-personal/{zp['bid']}/incoming"; ev = {"thread_id": "1", "sender_id": "1", "text": "x"}
    assert httpx.post(url, json=ev).status_code == 401
    assert httpx.post(url, json=ev, headers={"x-zp-token": "guess"}).status_code == 401
    assert httpx.post(url, json=ev, headers={"x-zp-token": "guess", "x-forwarded-for": "8.8.8.8"}).status_code == 403   # behind a proxy -> refused first
    assert httpx.post(f"{B}/internal/zalo-personal/{zp['bid']}/session", json={}).status_code == 401
    time.sleep(1); assert not sent(zp, 1)

def test_qr_only_after_confirm_then_login_stores_encrypted_session(zp):
    import crypto
    assert zp["adm"].get(ch_url(zp, "status")).json()["status"] in ("stopped", "none")   # no session yet (sidecar may or may not run)
    r = zp["adm"].post(ch_url(zp, "login"), json={}); assert r.status_code == 422                 # no QR without the confirmation
    r = zp["adm"].post(ch_url(zp, "login"), json={"confirm": False}); assert r.status_code == 422
    assert zp["ed"].post(ch_url(zp, "login"), json={"confirm": True}).status_code == 403
    r = zp["adm"].post(ch_url(zp, "login"), json={"confirm": True}); assert r.status_code == 200, r.text
    q = r.json(); assert q["status"] == "qr" and q["qr"].startswith("data:image/png;base64,")
    assert zp["adm"].get(ch_url(zp, "status")).json()["status"] == "qr"
    assert zp["adm"].post(ch_url(zp, "mock/scan")).json() == {"ok": True}
    st = wait_for(lambda: (s := zp["adm"].get(ch_url(zp, "status")).json())["status"] == "logged_in" and s)
    assert st["name"] and st["qr"] is None and st["has_session"] is True
    with pg() as c:
        enc, sname = c.execute("SELECT value_enc, name FROM ws_secrets WHERE workspace_id=%s AND name=(SELECT secret_ref FROM channel_bindings WHERE id=%s)", (zp["ws"], zp["bid"])).fetchone()
        everything = c.execute("SELECT row_to_json(b)::text FROM channel_bindings b WHERE id=%s", (zp["bid"],)).fetchone()[0]
    import ws_secrets; blob = json.loads(ws_secrets.open_value(enc, zp["ws"], sname)); assert set(blob) == {"cookie", "imei", "userAgent"}
    cookie = blob["cookie"][0]["value"]; assert cookie.startswith("MOCKCOOKIE-")
    assert cookie not in enc and "MOCKCOOKIE" not in enc and cookie not in everything                         # only encrypted at rest
    assert cookie not in log_text() and blob["imei"] not in log_text()                                        # never logged (backend + sidecar share the server log)
    assert cookie not in json.dumps(zp["adm"].get("/api/secrets").json()) and cookie not in json.dumps(st)   # and never returned by an API
    zp["cookie"] = cookie

def untrusted(t, text):
    """11.72: a stranger's text reaches the agent inside the [UNTRUSTED DATA] wrapper (the fake agent echoes it back): check the wrapper + the text, not the plain echo."""
    return t.startswith("Echo: [UNTRUSTED DATA") and t.rstrip().endswith("[END UNTRUSTED DATA]") and ("\n" + text + "\n") in t

def test_receive_and_reply(zp):
    dm(zp, 9001, "xin chào shop")
    out = wait_for(lambda: sent(zp, 9001))
    assert untrusted(out[0]["text"], "xin chào shop") and out[0]["type"] == "user"
    with pg() as c:
        md = c.execute("SELECT metadata FROM lg_runs WHERE workspace_id=%s ORDER BY created_at DESC LIMIT 1", (zp["ws"],)).fetchone()[0]
        assert md["channel"] == "zalo_personal" and md["binding_id"] == zp["bid"] and md["external_user"] == "9001"
        assert c.execute("SELECT count(*) FROM zp_sends WHERE binding_id=%s", (zp["bid"],)).fetchone()[0] >= 1

def test_duplicate_message_ignored(zp):
    dm(zp, 9002, "đơn hàng của tôi", msg_id=f"dup-{SUF}"); dm(zp, 9002, "đơn hàng của tôi", msg_id=f"dup-{SUF}")
    wait_for(lambda: sent(zp, 9002)); time.sleep(3); assert len(sent(zp, 9002)) == 1

def test_image_and_file_reach_the_agent(zp):
    FILES["p.png"] = PNG; FILES["bao_gia.txt"] = "Bảng giá: sản phẩm A giá 120.000 đồng.\n".encode()
    inject(zp, thread_id="9003", sender_id="9003", text="", file={"kind": "image", "url": f"http://127.0.0.1:{FILE_PORT}/f/p.png", "name": "p.png", "size": len(PNG)})
    out = wait_for(lambda: sent(zp, 9003)); assert f"I received 1 image(s): image/png {len(PNG)} B" in out[0]["text"], out[0]["text"]
    inject(zp, thread_id="9004", sender_id="9004", text="tóm tắt tệp", file={"kind": "file", "url": f"http://127.0.0.1:{FILE_PORT}/f/bao_gia.txt", "name": "bao_gia.txt", "size": 60})
    out = wait_for(lambda: sent(zp, 9004)); assert "120.000" in out[0]["text"], out[0]["text"]

def _approval(zp, uid, text):
    dm(zp, uid, text); out = wait_for(lambda: sent(zp, uid))
    assert "content_publish(" in out[0]["text"]
    m = re.search(r"Trả lời 'duyệt (\d+)' hoặc 'từ chối \1'", out[0]["text"]); assert m, out[0]["text"]
    return int(m[1])

def test_approval_by_text(zp):
    aid = _approval(zp, 9010, "publish bài khuyến mãi")
    dm(zp, 9010, f"duyệt {aid}"); out = wait_for(lambda: len(sent(zp, 9010)) >= 2 and sent(zp, 9010))
    assert "content_publish" in out[1]["text"] and "duyệt" not in out[1]["text"]
    with pg() as c: assert c.execute("SELECT status FROM approvals WHERE id=%s", (aid,)).fetchone()[0] == "approved"
    aid2 = _approval(zp, 9011, "publish thông báo nghỉ lễ")
    dm(zp, 9011, f"Từ chối {aid2}"); wait_for(lambda: len(sent(zp, 9011)) >= 2)
    with pg() as c: assert c.execute("SELECT status FROM approvals WHERE id=%s", (aid2,)).fetchone()[0] == "rejected"
    dm(zp, 9012, f"duyệt {aid}"); time.sleep(3); assert not sent(zp, 9012)                      # another chat cannot decide it

def test_rate_limit_two_seconds_between_sends(zp):
    dm(zp, 9020, "dài " * 1200)
    wait_for(lambda: len(sent(zp, 9020)) >= 3, timeout=120); time.sleep(1)
    out = sent(zp, 9020); assert len(out) >= 3 and all(len(o["text"]) <= 2000 for o in out)
    gaps = [(out[i + 1]["at"] - out[i]["at"]) / 1000 for i in range(len(out) - 1)]
    assert min(gaps) >= 1.9, gaps                                                               # queued parts waited their turn
    allm = [m for m in zp["adm"].get(ch_url(zp, "mock/sent")).json()["sent"] if m["at"] >= T0]
    ats = sorted(m["at"] for m in allm); assert all((y - x) >= 1900 for x, y in zip(ats, ats[1:]))   # no two sends of the binding closer than 2 s

def test_daily_cap_blocks_and_logs(zp):
    with pg() as c: n = c.execute("SELECT count(*) FROM zp_sends WHERE binding_id=%s", (zp["bid"],)).fetchone()[0]
    assert n >= 3
    cfg(zp, daily_cap=n)                                                                         # the config can lower the cap: now reached
    dm(zp, 9030, "còn hàng không")
    wait_for(lambda: log_has(f"zalo_personal: binding {zp['bid']}: daily cap reached ({n}/{n}), message to 9030 NOT sent"), timeout=60)
    time.sleep(2); assert not sent(zp, 9030)
    with pg() as c: assert c.execute("SELECT count(*) FROM zp_sends WHERE binding_id=%s", (zp["bid"],)).fetchone()[0] == n    # nothing counted
    cfg(zp, daily_cap=n + 5)
    dm(zp, 9031, "còn hàng không"); assert untrusted(wait_for(lambda: sent(zp, 9031))[0]["text"], "còn hàng không")
    cfg(zp)

def test_allow_list_users_and_groups(zp):
    r = zp["adm"].patch(f"/api/channels/{zp['bid']}", json={"allowed_users": ["9100", "555"]}); assert r.status_code == 200
    try:
        dm(zp, 9101, "tôi không có trong danh sách"); dm(zp, 9100, "tôi được phép")
        assert untrusted(wait_for(lambda: sent(zp, 9100))[0]["text"], "tôi được phép"); assert not sent(zp, 9101)
        assert log_has(f"zalo_personal: binding {zp['bid']}: ignored: user 9101 not in allowed_users") or log_has("user 9101 not in allowed_users")
        # a listed GROUP admits every member (mentioned)
        inject(zp, thread_id="555", sender_id="9102", sender_name="Thành viên", text="@Bot giá bao nhiêu", group=True, mention_self=True, mention_len=4)
        out = wait_for(lambda: sent(zp, 555)); assert untrusted(out[0]["text"], "giá bao nhiêu") and out[0]["type"] == "group"
        inject(zp, thread_id="556", sender_id="9102", text="@Bot giá", group=True, mention_self=True, mention_len=4)   # another group: not listed
        time.sleep(3); assert not sent(zp, 556)
    finally:
        zp["adm"].patch(f"/api/channels/{zp['bid']}", json={"allowed_users": []})

def test_group_without_mention_ignored(zp):
    inject(zp, thread_id="600", sender_id="9200", text="mọi người ơi", group=True)
    inject(zp, thread_id="600", sender_id="9200", text="duyệt 1", group=True)                    # decisions in groups also need the mention
    time.sleep(3); assert not sent(zp, 600) and log_has("group message without mention")
    inject(zp, thread_id="600", sender_id="9200", text="@Bot chào", group=True, mention_self=True, mention_len=4)
    assert untrusted(wait_for(lambda: sent(zp, 600))[0]["text"], "chào")

def test_disabled_binding_stops_session_and_ignores(zp):
    assert zp["adm"].patch(f"/api/channels/{zp['bid']}", json={"status": "disabled"}).status_code == 200
    wait_for(lambda: zp["adm"].get(ch_url(zp, "status")).json()["status"] in ("stopped", "none"), timeout=30)   # supervisor stopped the session
    r = zp["adm"].post(ch_url(zp, "mock/incoming"), json={"message": {"thread_id": "9300", "sender_id": "9300", "text": "x"}}); assert r.status_code == 409
    assert zp["adm"].patch(f"/api/channels/{zp['bid']}", json={"status": "active"}).status_code == 200
    # the stored (encrypted) login is handed back to the sidecar by the supervisor: no new QR needed
    wait_for(lambda: zp["adm"].get(ch_url(zp, "status")).json()["status"] == "logged_in", timeout=40)
    dm(zp, 9301, "chào lại"); assert untrusted(wait_for(lambda: sent(zp, 9301))[0]["text"], "chào lại")

def test_demo_blocked(zp, a):
    name = f"zp_demo_{SUF}"
    r = a.post("/api/admin/users", json={"username": name, "password": "demo-local-pass-123", "role": "viewer", "demo": True, "daily_cap_usd": 1.0, "demo_agents": [2]})
    assert r.status_code == 200, r.text
    try:
        with httpx.Client(base_url=B, auth=(name, "demo-local-pass-123"), timeout=60) as d:
            assert d.get("/api/channels/zalo-personal/capabilities").json()["enabled"] is False                     # tab entry hidden
            assert d.post(f"/api/agents/{zp['agent']}/channels", json={"kind": "zalo_personal", "config": {"accept_risk": True}}).status_code == 403
            assert d.post(f"/api/channels/{zp['bid']}/zalo-personal/login", json={"confirm": True}).status_code == 403
            assert d.get(f"/api/channels/{zp['bid']}/zalo-personal/status").status_code in (403, 404)
    finally:
        with pg() as c: c.execute("DELETE FROM memberships WHERE user_id=(SELECT id FROM users WHERE username=%s)", (name,)); c.execute("DELETE FROM users WHERE username=%s", (name,))

def test_logout_and_delete_remove_secret(zp):
    assert zp["adm"].post(ch_url(zp, "logout")).json() == {"ok": True}
    assert zp["adm"].get(ch_url(zp, "status")).json()["has_session"] is False
    with pg() as c: ref = c.execute("SELECT secret_ref FROM channel_bindings WHERE id=%s", (zp["bid"],)).fetchone()[0]
    assert zp["adm"].delete(f"/api/channels/{zp['bid']}").status_code == 200
    with pg() as c: assert c.execute("SELECT count(*) FROM ws_secrets WHERE workspace_id=%s AND name=%s", (zp["ws"], ref)).fetchone()[0] == 0
