"""GĐ8.5 (23/10/2026): Google Workspace + Microsoft 365 tools for agents (app/workspace_tools.py) against the local fake tests/fake_workspace_api.py
(127.0.0.1:8784 = GOOGLE_API_BASE / GRAPH_API_BASE / *_OAUTH_BASE set by overlay/run_local.py GW_MOCK=1). Real per-user OAuth connect flow
(oauth_tools.py) with the fake as authorization server; the scripted fake LLM ("[tool:<name>] {json}") calls the tools => $0, no real network.
Throwaway workspaces / users (suffix SUF), cleaned up at the end. Server tests run in file order and share state through `env`."""
import json, os, re, sys, time, urllib.parse, uuid
import httpx, psycopg, pytest
from test_fixes import B, ADMIN, a  # noqa: F401  (fixtures)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app")); sys.path.insert(0, os.path.dirname(__file__))
import fake_workspace_api as F  # noqa: E402

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
PW = "phase0-gd85-local"; SUF = uuid.uuid4().hex[:6]
LOG = "/workspace/agentos-local/var/server.log"

def pg(): return psycopg.connect(PG, autocommit=True)

# ------------------------------------------------------------------ pure unit tests
def test_specs_scopes_and_write_set():
    import workspace_tools as W
    assert {"gmail_draft", "gmail_send", "gcal_create", "sheets_update", "outlook_send", "outlook_calendar_create",   # GĐ11.55: + the F16 names of the renamed ones (old names stay as aliases)
                             "gsheets_update", "ocal_create"} <= W.WRITE_NAMES   # 11.56-11.59 add write tools: the original set must stay
    assert all(re.fullmatch(r"(google|ms365):[a-z_]+", k) for k in W.SPECS)
    rd, wr = W.scopes("google", "read").split(), W.scopes("google", "write").split()
    G = "https://www.googleapis.com/auth/"   # GĐ11.55 (design 5.3): write mode = gmail.modify (replaces readonly / compose / send), never https://mail.google.com/
    assert all("readonly" in s or s in ("openid", "email", G + "calendar.freebusy") for s in rd) and not {G + "gmail.modify", G + "calendar.events", G + "spreadsheets"} & set(rd)
    assert G + "gmail.modify" in wr and G + "gmail.send" not in wr and G + "gmail.compose" not in wr and "https://mail.google.com/" not in rd + wr and G + "gmail.readonly" not in wr
    assert "Mail.Send" not in W.scopes("microsoft", "read") and "offline_access" in W.scopes("microsoft", "read") and "Mail.Send" in W.scopes("microsoft", "write")
    t = W._tool("google:gmail_send", 1)
    assert t.name == "gmail_send" and set(t.args) == {"to", "subject", "body", "cc", "bcc", "body_format", "attachments"} and "approve" in t.description   # GĐ11.56 added bcc / html / attachments
    assert set(W._tool("ms365:excel_read", 1).args) == {"item_id", "sheet", "address"}
    assert W._allowed("google:gmail_search", {"google": "read"}) and not W._allowed("google:gmail_send", {"google": "read"})
    assert W._allowed("google:gmail_send", {"google": "write"}) and not W._allowed("ms365:outlook_search", {"google": "write", "ms365": "off"})
    assert W.q("../../users/other") == "..%2F..%2Fusers%2Fother"            # ids from the model never escape the API path

# ------------------------------------------------------------------ against the local server
def user(a, name, ws, role="editor"):
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": role, "workspace_id": ws})
    assert r.status_code == 200, r.text
    return r.json()["id"], httpx.Client(base_url=B, auth=(name, PW), timeout=150)

def connect(c, provider, who):
    r = c.post(f"/api/oauth/connect/{provider}"); assert r.status_code == 200, r.text
    r = httpx.get(r.json()["authorize_url"] + "&fake_user=" + who, follow_redirects=False); assert r.status_code == 302, r.text
    loc = urllib.parse.urlsplit(r.headers["location"])
    r = c.get(loc.path + "?" + loc.query, follow_redirects=False); assert r.status_code == 303, r.text

@pytest.fixture(scope="module")
def env(a):
    stop = F.start_in_thread(); F.reset()
    w1 = a.post("/api/admin/workspaces", json={"name": f"gw-w1-{SUF}"}).json()["id"]
    w2 = a.post("/api/admin/workspaces", json={"name": f"gw-w2-{SUF}"}).json()["id"]
    e = {"w1": w1, "w2": w2}
    for k, role, ws in (("adm", "admin", w1), ("ua", "editor", w1), ("ub", "editor", w1), ("uc", "editor", w1), ("adm2", "admin", w2)):
        e[k + "_id"], e[k] = user(a, f"gw_{k}_{SUF}", ws, role)
    yield e
    for k in ("adm", "ua", "ub", "uc", "adm2"): e[k].close()
    with pg() as c:
        c.execute("DELETE FROM oauth_connections WHERE workspace_id IN (%s,%s)", (w1, w2)); c.execute("DELETE FROM oauth_providers WHERE workspace_id IN (%s,%s)", (w1, w2))
        c.execute("DELETE FROM oauth_audit WHERE workspace_id IN (%s,%s)", (w1, w2))
        c.execute("DELETE FROM approvals WHERE agent_id IN (SELECT id FROM agents WHERE workspace_id IN (%s,%s))", (w1, w2))
        c.execute("DELETE FROM lg_runs WHERE workspace_id IN (%s,%s)", (w1, w2)); c.execute("DELETE FROM lg_threads WHERE workspace_id IN (%s,%s)", (w1, w2))
        c.execute("DELETE FROM agents WHERE workspace_id IN (%s,%s)", (w1, w2))
        c.execute("DELETE FROM users WHERE username LIKE %s", (f"gw\\_%\\_{SUF}",))
        c.execute("UPDATE workspaces SET gw_tools='{}', demo=false WHERE id IN (%s,%s)", (w1, w2))
    stop()

def ask(c, g, text):
    tid = c.post("/api/v1/threads", json={"metadata": {"source": "chat", "graph_id": g}}).json()["thread_id"]
    r = c.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": g, "input": {"messages": [{"type": "human", "content": text}]}})
    assert r.status_code == 200, r.text
    return tid, r

def resume(c, g, tid, decision):
    r = c.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": g, "command": {"resume": {"decisions": [decision]}}}); assert r.status_code == 200, r.text
    return r

def last_ai(r):
    ms = [m for m in r.json().get("messages", []) if m.get("type") == "ai" and m.get("content")]
    return ms[-1]["content"] if ms else ""

def tool(name, **args): return f"[tool:{name}] {json.dumps(args, ensure_ascii=False)}"
def status(c, tid): return c.get(f"/api/v1/threads/{tid}").json()["status"]

def test_off_by_default_and_admin_only(env):
    for k in ("adm", "adm2"):
        r = env[k].get("/api/workspace-tools").json()
        assert r["modes"] == {"google": "off", "ms365": "off"} and r["tools"] == []
    assert env["ua"].put("/api/workspace-tools", json={"google": "read"}).status_code == 403            # editors cannot enable
    assert not any(t["key"].startswith(("google:", "ms365:")) for t in env["adm"].get("/api/toolcatalog").json())

def test_enable_read_presets_connect(env):
    adm = env["adm"]
    r = adm.put("/api/workspace-tools", json={"google": "read", "ms365": "read"}); assert r.status_code == 200, r.text
    for prov in ("google", "microsoft"):
        r = adm.post(f"/api/workspace-tools/presets/{prov}", json={"client_id": f"fake-{prov}-client", "client_secret": "fake-secret"}); assert r.status_code == 200, r.text
        assert "readonly" in r.json()["scopes"] or "Mail.Read" in r.json()["scopes"]
        assert "send" not in r.json()["scopes"].lower()                                                # read-only scopes by default
    assert env["ua"].post("/api/workspace-tools/presets/google", json={"client_id": "x"}).status_code == 403
    for k, who in (("ua", "alice"), ("ub", "bob")):
        for prov in ("google", "microsoft"): connect(env[k], prov, who)
    conns = env["ua"].get("/api/oauth/connections").json()
    assert {c["provider"] for c in conns} == {"google", "microsoft"} and all(c["account"] == "alice@fake.test" for c in conns)
    assert all("access_token" not in json.dumps(c) for c in conns)
    keys = {t["key"] for t in env["ua"].get("/api/toolcatalog").json()}
    assert {"google:gmail_search", "ms365:outlook_search", "google:drive_read"} <= keys and not keys & {"google:gmail_send", "ms365:outlook_send"}
    assert env["adm2"].get("/api/workspace-tools").json()["modes"] == {"google": "off", "ms365": "off"}     # other workspace untouched
    import workspace_tools as W
    r = env["adm"].post("/api/agents", json={"name": f"gw-agent-{SUF}", "system_prompt": "Trợ lý văn phòng.", "tools": list(W.SPECS) + ["local:oauth_request"],
                                             "interrupt_on": [], "rpm_limit": 600})   # interrupt_on deliberately empty: write tools must be gated anyway (rpm_limit: the default 10 calls/min would make this ~50-call file wait for the window)
    assert r.status_code == 200, r.text
    env["g"] = f"agent-{r.json()['id']}"; env["agent"] = r.json()["id"]

READS = {   # tool -> (args, text expected in alice's answer)
    "gmail_search": ({"query": "doanh thu"}, "Báo cáo doanh thu tháng 10"),
    "gmail_read": ({"message_id": "m-a2"}, "9 giờ sáng thứ Sáu"),
    "gcal_list": ({"start": "2026-10-25T00:00:00+07:00", "days": 7}, "Họp giao ban tuần"),
    "sheets_read": ({"spreadsheet_id": "sheet-alice", "range": "Sheet1!A1:B3"}, "Áo thun"),
    "drive_search": ({"query": "hợp đồng"}, "Hợp đồng mẫu ALICE"),
    "drive_read": ({"file_id": "f-a1"}, "Hợp đồng mẫu của Alice"),
    "outlook_search": ({"query": "thứ Sáu"}, "Hẹn gặp thứ Sáu"),
    "outlook_read": ({"message_id": "m-a1"}, "1,2 tỷ đồng"),
    "outlook_calendar_list": ({"start": "2026-10-25T00:00:00+07:00"}, "Họp giao ban tuần"),
    "onedrive_search": ({"query": "alice"}, "Hợp đồng mẫu ALICE.txt"),
    "onedrive_read": ({"item_id": "f-a1"}, "Hợp đồng mẫu của Alice"),
    "excel_read": ({"item_id": "f-a1", "sheet": "Sheet 1", "address": "A1:B3"}, "Quần jean"),
}

@pytest.mark.parametrize("name", list(READS))
def test_read_tools_use_only_callers_token(env, name):
    args, expect = READS[name]; n0 = len(F.S["log"])
    tid, r = ask(env["ua"], env["g"], tool(name, **args))
    ans = last_ai(r)
    assert expect in ans, ans[:500]
    assert status(env["ua"], tid) == "idle"                                                       # read tools never pause for approval
    calls = F.S["log"][n0:]
    assert calls and all(w == "alice" for _, w, _, _ in calls), calls                             # only alice's token reached the API
    assert "BOB-SECRET" not in ans

def test_user_b_never_sees_a_data(env):
    n0 = len(F.S["log"])
    _, r = ask(env["ub"], env["g"], tool("gmail_search", query="in:inbox")); ans = last_ai(r)
    assert "BOB-SECRET" in ans and "doanh thu" not in ans.lower()
    _, r = ask(env["ub"], env["g"], tool("gmail_read", message_id="m-a1")); ans = last_ai(r)
    assert "HTTP 404" in ans and "1,2 tỷ" not in ans                                              # alice's message id is not in bob's mailbox
    _, r = ask(env["ub"], env["g"], tool("onedrive_read", item_id="f-a1")); assert "HTTP 404" in last_ai(r)
    assert all(w == "bob" for _, w, _, _ in F.S["log"][n0:]), F.S["log"][n0:]
    n1 = len(F.S["log"])                                                                          # a member who never connected gets nothing
    _, r = ask(env["uc"], env["g"], tool("gmail_search", query="in:inbox")); ans = last_ai(r)
    assert "Not connected" in ans and len(F.S["log"]) == n1

def test_token_refresh(env):
    F.S["ttl"]["google"] = 30; connect(env["ua"], "google", "alice"); F.S["ttl"]["google"] = 3600   # access token expires within the 60 s window
    with pg() as c: before = c.execute("SELECT last_refresh FROM oauth_connections WHERE user_id=%s AND provider_id=(SELECT id FROM oauth_providers WHERE workspace_id=%s AND name='google')",
                                       (env["ua_id"], env["w1"])).fetchone()[0]
    r0 = F.S["refreshes"]; n0 = len(F.S["log"])
    _, r = ask(env["ua"], env["g"], tool("gmail_search", query="doanh thu"))
    assert "Báo cáo doanh thu" in last_ai(r) and F.S["refreshes"] == r0 + 1
    assert all(w == "alice" for _, w, _, _ in F.S["log"][n0:])
    with pg() as c:
        row = c.execute("SELECT last_refresh, expires_at > now() + interval '30 minutes' FROM oauth_connections WHERE user_id=%s AND provider_id=(SELECT id FROM oauth_providers WHERE workspace_id=%s AND name='google')",
                        (env["ua_id"], env["w1"])).fetchone()
    assert row[0] > before and row[1]
    with pg() as c:
        assert c.execute("SELECT count(*) FROM oauth_audit WHERE user_id=%s AND action='token.refresh' AND ok", (env["ua_id"],)).fetchone()[0] >= 1

def test_401_mid_call_refreshes_once_and_retries(env):
    """8.P: the stored token still looks valid but the API answers 401 (revoked early) -> one forced refresh, one retry, same user."""
    for t in [t for t, v in F.S["tokens"].items() if v["prov"] == "google" and v["who"] == "alice"]: F.S["tokens"].pop(t)
    r0 = F.S["refreshes"]; n0 = len(F.S["log"])
    _, r = ask(env["ua"], env["g"], tool("gmail_search", query="doanh thu"))
    assert "Báo cáo doanh thu" in last_ai(r) and F.S["refreshes"] == r0 + 1
    calls = F.S["log"][n0:]
    assert calls[0][1] is None and all(w == "alice" for _, w, _, _ in calls[1:]), calls                # 401 first, then alice's new token
    with pg() as c:
        assert c.execute("SELECT count(*) FROM oauth_audit WHERE user_id=%s AND action='token.refresh' AND detail->>'reason'='http_401'", (env["ua_id"],)).fetchone()[0] == 1

def test_write_tools_absent_in_read_mode(env):
    _, r = ask(env["ua"], env["g"], tool("gmail_send", to=["x@khach.vn"], subject="s", body="b"))
    assert not F.S["sent"] and "gmail_send" in last_ai(r)                                          # tool not offered => nothing sent
    _, r = ask(env["ua"], env["g"], tool("oauth_request", provider="google", path_or_url="gmail/v1/users/me/messages/send", method="POST", json_body={"raw": "eA"}))
    assert "read-only" in last_ai(r) and not F.S["sent"]                                          # no approval bypass through the generic tool

def test_write_tools_pause_for_approval(env):
    r = env["adm"].put("/api/workspace-tools", json={"google": "write", "ms365": "write"}); assert r.status_code == 200
    with pg() as c: assert "gmail.modify" in c.execute("SELECT scopes FROM oauth_providers WHERE workspace_id=%s AND name='google'", (env["w1"],)).fetchone()[0]
    connect(env["ua"], "google", "alice"); connect(env["ua"], "microsoft", "alice")                  # reconnect to grant the write scopes
    ua, g = env["ua"], env["g"]
    tid, _ = ask(ua, g, tool("gmail_send", to=["lan@khach.vn"], subject="Xác nhận lịch hẹn", body="Chào chị Lan, em xác nhận 9h thứ Sáu."))
    assert status(ua, tid) == "interrupted" and not F.S["sent"]                                     # paused, nothing sent yet
    st = ua.get(f"/api/v1/threads/{tid}/state").json()
    reqs = [x for t in st["tasks"] for i in t["interrupts"] for x in (i["value"].get("action_requests") or [])]
    assert reqs and reqs[0]["name"] == "gmail_send" and reqs[0]["args"]["to"] == ["lan@khach.vn"]
    ans = last_ai(resume(ua, g, tid, {"type": "approve"}))
    assert "Email sent" in ans and len(F.S["sent"]) == 1 and F.S["sent"][0]["who"] == "alice" and F.S["sent"][0]["prov"] == "google"
    tid, _ = ask(ua, g, tool("outlook_send", to=["lan@khach.vn"], subject="x", body="y"))
    assert status(ua, tid) == "interrupted"
    resume(ua, g, tid, {"type": "reject", "message": "không gửi"})
    assert len(F.S["sent"]) == 1 and status(ua, tid) == "idle"                                     # rejected => never sent
    tid, _ = ask(ua, g, tool("gcal_create", summary="Gặp chị Lan", start="2026-10-30T09:00:00+07:00", end="2026-10-30T10:00:00+07:00"))
    assert status(ua, tid) == "interrupted" and not F.S["created"]
    assert "Event created" in last_ai(resume(ua, g, tid, {"type": "approve"})) and F.S["created"][0]["who"] == "alice"
    # bob: same gate, and only his own token is used. GĐ11.55: his read-mode grant no longer covers a write tool (the scope is checked before the call), so he reconnects first
    connect(env["ub"], "google", "bob")
    tid, _ = ask(env["ub"], g, tool("sheets_update", spreadsheet_id="s1", range="A1:B1", values=[["a", "b"]]))
    assert status(env["ub"], tid) == "interrupted" and not F.S["updated"]
    resume(env["ub"], g, tid, {"type": "approve"})
    assert F.S["updated"] and F.S["updated"][0]["who"] == "bob"

def test_demo_workspace_off(env, a):
    with pg() as c: c.execute("UPDATE workspaces SET demo=true, gw_tools='{\"google\":\"write\"}' WHERE id=%s", (env["w2"],))
    r = env["adm2"].get("/api/workspace-tools").json()
    assert r["modes"] == {"google": "off", "ms365": "off"} and r["tools"] == []
    assert env["adm2"].put("/api/workspace-tools", json={"google": "read"}).status_code == 403
    assert env["adm2"].post("/api/workspace-tools/presets/google", json={"client_id": "x"}).status_code == 403

def test_tokens_never_logged_or_returned(env):
    toks = list(F.S["tokens"]) + list(F.S["refresh"])
    assert toks
    with open(LOG, encoding="utf-8", errors="replace") as f: log = f.read()[-3_000_000:]
    assert not any(t in log for t in toks)
    with pg() as c:
        rows = c.execute("SELECT detail::text FROM oauth_audit WHERE workspace_id=%s", (env["w1"],)).fetchall()
        enc = c.execute("SELECT access_token_enc FROM oauth_connections WHERE workspace_id=%s", (env["w1"],)).fetchall()
    assert rows and not any(t in r[0] for r in rows for t in toks)
    assert enc and not any(t in e[0] for e in enc for t in toks)                                   # stored encrypted
