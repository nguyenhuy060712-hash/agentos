"""GĐ8.1 (23/10/2026): channel framework (app/channels.py). Pure unit tests (adapter shape, chat_key, allowed_users, approval summary,
ws_secrets.get_secret host restriction) + end-to-end against the local server :8020 through the fake adapter simulator (fake LLM => $0, no network).
Synthetic data in two throwaway workspaces; the server tests run in file order and share state through the `env` fixture."""
import asyncio, base64, os, sys, uuid
import httpx, psycopg, pytest
from test_fixes import B, ADMIN, a  # noqa: F401  (fixtures)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
PW = "phase0-gd81-local"; SUF = uuid.uuid4().hex[:6]

def pg(): return psycopg.connect(PG, autocommit=True)

# ------------------------------------------------------------------ pure unit tests
def test_adapter_shape_and_helpers():
    import channels as ch
    m = ch.ADAPTERS["fake"].parse_incoming({"user_id": 7, "user_name": "An", "text": "chào"})
    assert m == ch.Incoming(chat_id="7", user_id="7", user_name="An", text="chào") and ch.chat_key(m) == "u:7"
    g = ch.ADAPTERS["fake"].parse_incoming({"chat_id": "-100", "user_id": "7", "text": "hi", "is_group": True})
    assert ch.chat_key(g) == "g:-100" and g.mentioned is False
    assert ch.ADAPTERS["fake"].parse_incoming({"user_id": "7"}) is None          # nothing for the agent
    assert ch.user_allowed({"allowed_users": []}, "x") and ch.user_allowed({"allowed_users": None}, "x")
    b = {"allowed_users": ["111", "@HoaNguyen"]}
    assert ch.user_allowed(b, "111") and ch.user_allowed(b, "999", "hoanguyen") and not ch.user_allowed(b, "222", "khac")
    s = ch.summarize([{"action_requests": [{"name": "content_publish", "args": {"channel": "facebook", "title": "T"}}]}])
    assert "content_publish(channel=facebook, title=T)" in s
    assert ch._timeout({"config": {"timeout_s": 1}}) == 5 and ch._timeout({"config": {}}) == ch.DEFAULT_TIMEOUT

def test_fake_adapter_outbox():
    import channels as ch
    ad = ch.FakeAdapter()
    asyncio.run(ad.send_text({"id": 1}, "c1", "xin chào"))
    asyncio.run(ad.send_approval({"id": 1}, "c1", 42, "duyệt?"))
    assert [x["type"] for x in ad.outbox] == ["text", "approval"] and ad.outbox[1]["approval_id"] == 42
    assert [b["approve"] for b in ad.outbox[1]["buttons"]] == [True, False]

def test_get_secret_host_restriction(monkeypatch, tmp_path):
    import crypto, ws_secrets
    monkeypatch.setattr(crypto, "_RING", None); monkeypatch.setattr(crypto, "KEY_FILE", tmp_path / "k")
    monkeypatch.setenv("AOS_SECRET_KEY", base64.b64encode(os.urandom(32)).decode())
    rows = {(5, "TG_TOKEN"): {"value_enc": crypto.encrypt("123:abc"), "allowed_hosts": ["api.telegram.org"]}}
    async def fake_q(sql, args=(), one=False): return rows.get((args[0], args[1]))
    monkeypatch.setattr(ws_secrets, "_q", fake_q)
    assert asyncio.run(ws_secrets.get_secret("TG_TOKEN", 5, "api.telegram.org")) == "123:abc"
    with pytest.raises(PermissionError): asyncio.run(ws_secrets.get_secret("TG_TOKEN", 5, "evil.example.com"))
    with pytest.raises(LookupError): asyncio.run(ws_secrets.get_secret("TG_TOKEN", 6, "api.telegram.org"))   # other workspace
    with pytest.raises(PermissionError): asyncio.run(ws_secrets.get_secret("TG_TOKEN", 5, ""))

# ------------------------------------------------------------------ against the local server
def user(a, name, ws, role="editor"):
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": role, "workspace_id": ws})
    assert r.status_code == 200, r.text
    return r.json()["id"], httpx.Client(base_url=B, auth=(name, PW), timeout=150)

@pytest.fixture(scope="module")
def env(a):
    w1 = a.post("/api/admin/workspaces", json={"name": f"ch-w1-{SUF}"}).json()["id"]
    w2 = a.post("/api/admin/workspaces", json={"name": f"ch-w2-{SUF}"}).json()["id"]
    e = {"w1": w1, "w2": w2}
    for k, role, ws in (("ed", "editor", w1), ("vw", "viewer", w1), ("ed2", "editor", w2)):
        e[k + "_id"], e[k] = user(a, f"ch_{k}_{SUF}", ws, role)
    r = e["ed"].post("/api/agents", json={"name": f"ch-agent-{SUF}", "system_prompt": "Bạn là trợ lý bán hàng.", "tools": ["local:content_publish"],
                                          "interrupt_on": ["local:content_publish"]})
    assert r.status_code == 200, r.text
    e["agent"] = r.json()["id"]
    yield e
    for k in ("ed", "vw", "ed2"): e[k].close()
    with pg() as c:
        c.execute("DELETE FROM channel_bindings WHERE workspace_id IN (%s,%s)", (w1, w2))
        c.execute("DELETE FROM approvals WHERE agent_id=%s", (e["agent"],))
        c.execute("DELETE FROM lg_runs WHERE workspace_id IN (%s,%s)", (w1, w2))
        c.execute("DELETE FROM lg_threads WHERE workspace_id IN (%s,%s)", (w1, w2))
        c.execute("DELETE FROM agents WHERE workspace_id IN (%s,%s)", (w1, w2))
        c.execute("DELETE FROM users WHERE username LIKE %s", (f"ch\\_%\\_{SUF}",))

def sim(c, bid, **body):
    r = c.post(f"/api/channels/{bid}/simulate", json=body); assert r.status_code == 200, r.text
    return r.json()

def test_crud_permissions(env):
    ed, vw = env["ed"], env["vw"]
    assert ed.post(f"/api/agents/{env['agent']}/channels", json={"kind": "carrier-pigeon"}).status_code == 422
    assert ed.post(f"/api/agents/{env['agent']}/channels", json={"kind": "fake", "secret_ref": "NO_SUCH_SECRET"}).status_code == 422
    assert vw.post(f"/api/agents/{env['agent']}/channels", json={"kind": "fake"}).status_code == 403
    r = ed.post(f"/api/agents/{env['agent']}/channels", json={"kind": "fake", "name": "Kênh thử", "config": {"timeout_s": 120}}); assert r.status_code == 200, r.text
    b = r.json(); env["bid"] = b["id"]
    assert b["status"] == "active" and b["allowed_users"] == [] and b["created_by"] == env["ed_id"] and b["workspace_id"] == env["w1"]
    L = vw.get(f"/api/agents/{env['agent']}/channels").json(); assert [x["id"] for x in L] == [b["id"]]   # viewers may see the list
    assert vw.patch(f"/api/channels/{b['id']}", json={"status": "disabled"}).status_code == 403

def untrusted(messages, meta, text):
    """11.72: text from a stranger reaches the agent inside the [UNTRUSTED DATA] wrapper (the fake agent echoes it back): the security behaviour is checked, not the plain echo."""
    t = messages[0]["text"]
    assert t.startswith("Echo: [UNTRUSTED DATA") and t.rstrip().endswith("[END UNTRUSTED DATA]") and ("\n" + text + "\n") in t
    return meta is None or [{k: v for k, v in m.items() if k != "text"} for m in messages] == meta

def test_two_external_users_get_two_threads(env):
    ed, bid = env["ed"], env["bid"]
    r1 = sim(ed, bid, text="xin chào shop", user_id="u-111", user_name="An")
    assert r1["result"]["status"] == "replied" and untrusted(r1["messages"], [{"binding_id": bid, "chat_id": "u-111", "type": "text"}], "xin chào shop")
    r2 = sim(ed, bid, text="còn hàng không", user_id="u-222")
    assert r2["result"]["status"] == "replied" and untrusted(r2["messages"], None, "còn hàng không")
    assert r1["result"]["thread_id"] != r2["result"]["thread_id"]
    r3 = sim(ed, bid, text="cảm ơn", user_id="u-111")
    assert r3["result"]["thread_id"] == r1["result"]["thread_id"]                     # same external user -> same thread
    with pg() as c:
        rows = c.execute("SELECT chat_key, thread_id FROM channel_threads WHERE binding_id=%s ORDER BY chat_key", (bid,)).fetchall()
        assert [k for k, _ in rows] == ["u:u-111", "u:u-222"]
        md = c.execute("SELECT metadata, user_id FROM lg_runs WHERE run_id=%s", (r1["result"]["run_id"],)).fetchone()
        assert md[0]["channel"] == "fake" and md[0]["binding_id"] == bid and md[0]["external_user"] == "u-111" and md[1] == env["ed_id"]   # runs as the binding creator
        own = c.execute("SELECT owner_id, workspace_id, metadata->>'source' FROM lg_threads WHERE thread_id=%s", (r1["result"]["thread_id"],)).fetchone()
        assert own == (env["ed_id"], env["w1"], "channel")
        feats = {r[0] for r in c.execute("SELECT feature FROM gateway_usage WHERE agent_id=%s AND ts > now() - interval '10 minutes'", (env["agent"],)).fetchall()}
    assert feats <= {"channel"}                                                       # costed like chat, tagged feature=channel

def test_group_needs_mention(env):
    ed, bid = env["ed"], env["bid"]
    r = sim(ed, bid, text="mọi người ơi", user_id="u-333", chat_id="grp-1", is_group=True)
    assert r["result"] == {"status": "ignored", "reason": "group message without mention"} and r["messages"] == []
    r = sim(ed, bid, text="@bot giá bao nhiêu", user_id="u-333", chat_id="grp-1", is_group=True, mentioned=True)
    assert r["result"]["status"] == "replied" and r["messages"][0]["chat_id"] == "grp-1"
    with pg() as c:
        assert c.execute("SELECT 1 FROM channel_threads WHERE binding_id=%s AND chat_key='g:grp-1'", (bid,)).fetchone()

def test_allowed_users_and_disabled(env):
    ed, bid = env["ed"], env["bid"]
    assert ed.patch(f"/api/channels/{bid}", json={"allowed_users": ["u-111", "@hoa"]}).json()["allowed_users"] == ["u-111", "@hoa"]
    r = sim(ed, bid, text="tôi là người lạ", user_id="u-999")
    assert r["result"]["status"] == "ignored" and r["messages"] == []
    assert sim(ed, bid, text="ok", user_id="u-555", user_name="Hoa")["result"]["status"] == "replied"   # allowed by name
    assert sim(ed, bid, text="ok", user_id="u-111")["result"]["status"] == "replied"
    assert ed.patch(f"/api/channels/{bid}", json={"status": "disabled"}).json()["status"] == "disabled"
    assert sim(ed, bid, text="ok", user_id="u-111")["result"] == {"status": "ignored", "reason": "binding disabled"}
    assert ed.patch(f"/api/channels/{bid}", json={"status": "active", "allowed_users": []}).status_code == 200

def test_interrupt_approve_resumes(env):
    ed, bid = env["ed"], env["bid"]
    r = sim(ed, bid, text="publish bài khuyến mãi", user_id="u-444")
    assert r["result"]["status"] == "approval", r
    m = r["messages"][0]; aid = m["approval_id"]
    assert m["type"] == "approval" and "content_publish(" in m["text"] and aid == r["result"]["approval_id"]
    assert sim(ed, bid, user_id="u-222", decision={"approval_id": aid, "approve": True})["result"]["status"] == "ignored"   # not this user's chat
    d = sim(ed, bid, user_id="u-444", decision={"approval_id": aid, "approve": True})
    assert d["result"]["status"] == "replied" and d["result"]["thread_id"] == r["result"]["thread_id"], d
    assert "content_publish" in d["messages"][0]["text"]
    with pg() as c:
        assert c.execute("SELECT status FROM approvals WHERE id=%s", (aid,)).fetchone()[0] == "approved"
        assert c.execute("SELECT metadata->>'channel' FROM lg_runs WHERE run_id=%s", (d["result"]["run_id"],)).fetchone()[0] == "fake"
    again = sim(ed, bid, user_id="u-444", decision={"approval_id": aid, "approve": True})
    assert again["result"]["status"] == "stale" and again["messages"][0]["type"] == "text"

def test_interrupt_reject_stops(env):
    ed, bid = env["ed"], env["bid"]
    r = sim(ed, bid, text="publish thông báo nghỉ lễ", user_id="u-666")
    assert r["result"]["status"] == "approval", r
    aid = r["result"]["approval_id"]
    d = sim(ed, bid, user_id="u-666", decision={"approval_id": aid, "approve": False})
    assert d["result"]["status"] == "replied" and d["messages"][0]["type"] == "text", d
    with pg() as c:
        assert c.execute("SELECT status FROM approvals WHERE id=%s", (aid,)).fetchone()[0] == "rejected"
        assert not c.execute("SELECT 1 FROM approvals WHERE thread_id=%s AND status='pending'", (r["result"]["thread_id"],)).fetchone()   # nothing left waiting

def test_workspace_isolation(env):
    ed2, bid = env["ed2"], env["bid"]
    assert ed2.get(f"/api/agents/{env['agent']}/channels").json() == []
    assert ed2.patch(f"/api/channels/{bid}", json={"name": "x"}).status_code == 404
    assert ed2.post(f"/api/channels/{bid}/simulate", json={"text": "hi"}).status_code == 404
    assert ed2.delete(f"/api/channels/{bid}").status_code == 404
    assert ed2.post(f"/api/agents/{env['agent']}/channels", json={"kind": "fake"}).status_code == 404
    with pg() as c:   # Postgres RLS itself: a workspace-B session sees no workspace-A rows
        c.execute("SELECT set_config('app.ws', %s, false), set_config('app.uid', %s, false), set_config('app.role', 'admin', false)", (str(env["w2"]), str(env["ed2_id"])))
        c.execute("SET ROLE aos_rls")
        assert c.execute("SELECT count(*) FROM channel_bindings WHERE id=%s", (bid,)).fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM channel_threads WHERE binding_id=%s", (bid,)).fetchone()[0] == 0
        c.execute("RESET ROLE")

def test_delete_binding(env):
    ed, bid = env["ed"], env["bid"]
    assert ed.delete(f"/api/channels/{bid}").json() == {"ok": True}
    assert ed.get(f"/api/agents/{env['agent']}/channels").json() == []
    with pg() as c:
        assert c.execute("SELECT count(*) FROM channel_threads WHERE binding_id=%s", (bid,)).fetchone()[0] == 0
