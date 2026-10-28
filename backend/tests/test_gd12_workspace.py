"""GĐ12.99 rename workspace + GĐ12.100 invite people to a workspace (link). $0, fake LLM, no real network.
Part 1 (in-process, local Postgres, 3 workspaces + a superadmin): the rename rules and the invite create / list / revoke rules, the role ceiling, "token never stored".
Part 2 (over HTTP, OUR OWN server on :8056 behind the real middleware, fake SMTP): the anonymous accept flow and every refusal (expired / revoked / used-up / forged / malformed token,
other workspace, role escalation, viewer / editor inviting, demo workspace, demo account, inviter demoted or disabled, name taken, weak password, parallel accepts, rate limit).
The server is killed by its PID at the end (never pkill run_local.py); every row this test creates is removed again."""
import asyncio, base64, concurrent.futures as cf, hashlib, json, os, re, socketserver, subprocess, sys, threading, time, uuid
os.environ.setdefault("GOOGLE_API_KEY", "x-not-used")
import httpx, psycopg, pytest
from test_gd6_notify import real_q, PG, _S, Fake  # noqa: F401  (DB helper + the minimal fake SMTP of the 6.4 tests)

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app"))
run = asyncio.run
SUF = uuid.uuid4().hex[:6]
PORT = 8056; B = f"http://127.0.0.1:{PORT}"; ADMIN = ("moza", "phase0-local"); PW = "phase0-gd12100-local"
TEST_IP_PREFIX = "10.12."

def rows(sql, *a):
    return run(real_q(sql, a))

def U(ws, role="admin", uid=1, demo=False, auth="session"):
    return {"id": uid, "username": f"wt_u{uid}_{SUF}", "role": role, "ws": ws, "auth": auth, **({"demo": True} if demo else {})}

def as_user(user, fn, *a, **k):
    import deepagent_api, admin  # noqa: F401  (deepagent_api first: admin alone has a circular import)
    async def go():
        tok = admin.USER.set(user)
        try: return await fn(*a, **k)
        finally: admin.USER.reset(tok)
    return run(go())

def status_of(user, fn, *a, **k):
    from fastapi import HTTPException
    try: as_user(user, fn, *a, **k)
    except HTTPException as e: return e.status_code
    return 200

@pytest.fixture(scope="module", autouse=True)
def migrated():
    import migrations
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as c:
            await c.execute("SELECT pg_advisory_lock(hashtext('agentos_boot_ddl'))")
            try:
                await migrations.run_pending(c)
                await c.execute("ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS demo BOOLEAN NOT NULL DEFAULT false")
            finally: await c.execute("SELECT pg_advisory_unlock(hashtext('agentos_boot_ddl'))")
    run(go())

def _cleanup():
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute("DELETE FROM audit_log WHERE username LIKE %s OR ip LIKE %s OR path LIKE %s", (f"wt\\_%\\_{SUF}", TEST_IP_PREFIX + "%", f"%wt-%-{SUF}%"))
        c.execute("DELETE FROM users WHERE username LIKE %s", (f"wt\\_%\\_{SUF}",))
        c.execute("DELETE FROM workspaces WHERE name LIKE %s OR name LIKE %s", (f"wt-%-{SUF}", f"wt-%-{SUF}-renamed%"))

@pytest.fixture(scope="module", autouse=True)
def clean():
    _cleanup(); yield; _cleanup()

# ====================================================================== part 1: in-process
@pytest.fixture
def W(monkeypatch):
    """3 workspaces A B C, users: adm_a (admin of A), adm_ab (admin of A and B), ed_a, vw_a, adm_b, sup (superadmin, no membership), demo_a."""
    import deepagent_api
    monkeypatch.setattr(deepagent_api, "q", real_q)
    w = {}
    for k in "ABC": w[k] = rows("INSERT INTO workspaces(name) VALUES(%s) RETURNING id", f"wt-{k}-{SUF}-{uuid.uuid4().hex[:4]}")[0]["id"]
    def mk(name, ws_roles, sup=False):
        uid = rows("INSERT INTO users(username,pw_hash,is_superadmin) VALUES(%s,'x',%s) RETURNING id", f"wt_{name}_{SUF}-{uuid.uuid4().hex[:4]}".replace("-", "_"), sup)[0]["id"]
        for ws, role in ws_roles: rows("INSERT INTO memberships(user_id,workspace_id,role) VALUES(%s,%s,%s)", uid, ws, role)
        return uid
    ids = {"adm_a": mk("adma", [(w["A"], "admin")]), "adm_ab": mk("admab", [(w["A"], "admin"), (w["B"], "admin")]), "ed_a": mk("eda", [(w["A"], "editor")]),
           "vw_a": mk("vwa", [(w["A"], "viewer")]), "adm_b": mk("admb", [(w["B"], "admin")]), "sup": mk("sup", [], sup=True)}
    yield {**w, **ids}
    rows("DELETE FROM workspaces WHERE id = ANY(%s)", list(w.values()))
    rows("DELETE FROM users WHERE id = ANY(%s)", [v for k, v in ids.items()])

def ws_name(wid): return rows("SELECT name FROM workspaces WHERE id=%s", wid)[0]["name"]

def test_rename_works_and_me_shows_the_new_name(W):
    import admin
    r = as_user(U(W["A"], uid=W["adm_a"]), admin.ws_rename, W["A"], admin.WsRename(name="  wt-A-" + SUF + "-renamed  "))
    assert r == {"id": W["A"], "name": f"wt-A-{SUF}-renamed"} and ws_name(W["A"]) == f"wt-A-{SUF}-renamed"            # trimmed
    me = as_user(U(W["A"], uid=W["adm_a"]), admin.me)
    assert [x["name"] for x in me["workspaces"] if x["id"] == W["A"]] == [f"wt-A-{SUF}-renamed"]
    assert as_user(U(W["A"], uid=W["adm_a"]), admin.ws_rename, W["A"], admin.WsRename(name=f"wt-A-{SUF}-renamed"))["name"] == f"wt-A-{SUF}-renamed"   # same name = no-op, not 409
    one = as_user(U(W["A"], uid=W["adm_a"]), admin.ws_get, W["A"])
    assert one["id"] == W["A"] and one["name"] == f"wt-A-{SUF}-renamed" and one["members"] == 4 and "created" in one

def test_rename_duplicate_is_409(W):
    import admin
    other = ws_name(W["B"])
    assert status_of(U(W["A"], uid=W["adm_ab"]), admin.ws_rename, W["A"], admin.WsRename(name=other)) == 409
    assert status_of(U(W["A"], uid=W["adm_ab"]), admin.ws_rename, W["A"], admin.WsRename(name="  " + other + " ")) == 409    # trimmed before the check
    assert ws_name(W["A"]) != other

def test_admin_of_a_cannot_rename_b_nor_unknown_workspace(W):
    import admin
    before = ws_name(W["B"])
    assert status_of(U(W["A"], uid=W["adm_a"]), admin.ws_rename, W["B"], admin.WsRename(name="hijacked")) == 403
    assert status_of(U(W["A"], uid=W["adm_a"]), admin.ws_rename, 999999, admin.WsRename(name="x")) == 403            # unknown id answers like "not yours": no probing
    assert status_of(U(W["A"], uid=W["adm_a"]), admin.ws_get, W["B"]) == 403
    assert status_of(U(W["B"], uid=W["adm_b"]), admin.ws_rename, W["A"], admin.WsRename(name="hijacked")) == 403
    for who in ("ed_a", "vw_a"):                                                                                     # defence in depth: the membership check refuses editor / viewer even if the prefix gate were bypassed
        assert status_of(U(W["A"], role="admin", uid=W[who]), admin.ws_rename, W["A"], admin.WsRename(name="hijacked")) == 403
    assert ws_name(W["B"]) == before and "hijacked" not in ws_name(W["A"])

def test_admin_of_both_may_rename_either_but_a_key_is_bound_to_its_workspace(W):
    import admin
    key = U(W["A"], uid=W["adm_ab"], auth="api_key:aos_test")
    assert status_of(key, admin.ws_rename, W["B"], admin.WsRename(name="by-key")) == 403                              # the key belongs to A
    assert status_of(U(W["A"], uid=W["adm_ab"]), admin.ws_rename, W["B"], admin.WsRename(name=f"wt-B-{SUF}-renamed2")) == 200
    assert status_of(U(W["A"], uid=W["sup"]), admin.ws_rename, W["C"], admin.WsRename(name=f"wt-C-{SUF}-renamed3")) == 200   # a superadmin is admin everywhere

def test_default_and_demo_workspaces_cannot_be_renamed(W):
    import admin, gateway
    n1 = ws_name(1)
    assert status_of(U(1, uid=W["sup"]), admin.ws_rename, 1, admin.WsRename(name="nope")) == 403 and ws_name(1) == n1
    gateway.DEMO_WS.add(W["C"])
    try: assert status_of(U(W["C"], uid=W["sup"]), admin.ws_rename, W["C"], admin.WsRename(name="nope")) == 403
    finally: gateway.DEMO_WS.discard(W["C"])
    rows("UPDATE workspaces SET demo=true WHERE id=%s", W["C"])                                                       # the DB flag alone is enough
    assert status_of(U(W["C"], uid=W["sup"]), admin.ws_rename, W["C"], admin.WsRename(name="nope")) == 403
    assert status_of(U(W["A"], uid=W["adm_a"], demo=True), admin.ws_rename, W["A"], admin.WsRename(name="nope")) == 403   # a demo account
    assert ws_name(W["C"]) != "nope"

def test_bad_names_are_422_and_there_is_no_delete_route(W):
    import admin, deepagent_api
    from pydantic import ValidationError
    u = U(W["A"], uid=W["adm_a"])
    assert status_of(u, admin.ws_rename, W["A"], admin.WsRename(name="   ")) == 422
    assert status_of(u, admin.ws_rename, W["A"], admin.WsRename(name="a\nb")) == 422
    assert status_of(u, admin.ws_rename, W["A"], admin.WsRename(name="x" * 81)) == 422
    with pytest.raises(ValidationError): admin.WsRename(name="")
    paths = {(m, r.path) for r in admin.router.routes for m in r.methods}
    assert ("DELETE", "/api/admin/workspaces/{wid}") not in paths                                                     # delete is GĐ14
    assert ("PATCH", "/api/admin/workspaces/{wid}") in paths and ("GET", "/api/admin/workspaces/{wid}") in paths

def test_migration_0190_table_shape():
    cols = {r["column_name"] for r in rows("SELECT column_name FROM information_schema.columns WHERE table_name='ws_invites'")}
    assert {"id", "workspace_id", "token_hash", "role", "email_note", "created_by", "created_at", "expires_at", "max_uses", "used_count", "revoked_at"} <= cols
    assert "token" not in cols and not (cols & {"link", "secret", "raw_token"})
    users_cols = {r["column_name"] for r in rows("SELECT column_name FROM information_schema.columns WHERE table_name='users'")}
    assert "email" not in users_cols                                                                                  # 10.13 deferred: no email column on users
    import admin; assert "ws_invites" in admin.RLS_DIRECT and "/invite/" in admin.PUBLIC_PREFIX and "/api/invites/" in admin.PUBLIC_PREFIX

def _body(**k): import invites; return invites.InviteIn(**k)

def test_invite_create_never_stores_the_token_and_lists_never_show_it(W, monkeypatch):
    import invites
    monkeypatch.setenv("AOS_PUBLIC_URL", "https://x.example"); monkeypatch.delenv("SMTP_HOST", raising=False)
    u = U(W["A"], uid=W["adm_a"])
    r = as_user(u, invites.invite_create, W["A"], _body(role="editor", email_note="who@example.com", max_uses=3), __import__("fastapi").Response())
    token = r["link"].rsplit("/", 1)[1]
    assert r["link"] == "https://x.example/invite/" + token and len(token) == 43 and r["emailed"] is False and r["max_uses"] == 3
    row = rows("SELECT * FROM ws_invites WHERE id=%s", r["id"])[0]
    assert row["token_hash"] == hashlib.sha256(token.encode()).hexdigest() and row["workspace_id"] == W["A"] and row["role"] == "editor" and row["email_note"] == "who@example.com"
    assert token not in json.dumps(row, default=str)
    assert rows("SELECT count(*) n FROM ws_invites WHERE token_hash=%s", token)[0]["n"] == 0                          # the clear token is nowhere
    lst = as_user(u, invites.invite_list, W["A"])
    assert [x["id"] for x in lst] == [r["id"]] and token not in json.dumps(lst, default=str) and row["token_hash"] not in json.dumps(lst, default=str)
    assert not ({"token", "token_hash", "link"} & set(lst[0]))
    assert lst[0]["used_count"] == 0 and lst[0]["max_uses"] == 3 and lst[0]["email_note"] == "who@example.com"

def test_invite_admin_api_refuses_wrong_role_workspace_and_demo(W, monkeypatch):
    import invites, gateway
    R = __import__("fastapi").Response
    mk = lambda u, wid, **k: status_of(u, invites.invite_create, wid, _body(**k), R())
    assert mk(U(W["A"], role="viewer", uid=W["vw_a"]), W["A"], role="viewer") == 403                                  # viewer inviting
    assert mk(U(W["A"], role="editor", uid=W["ed_a"]), W["A"], role="viewer") == 403                                  # editor inviting (only admins invite)
    assert mk(U(W["A"], uid=W["adm_a"], demo=True), W["A"], role="viewer") == 403                                     # demo account
    assert mk(U(W["A"], uid=W["adm_a"]), W["B"], role="viewer") == 403                                                # admin of A inviting into B
    assert mk(U(W["B"], uid=W["adm_b"]), W["A"], role="viewer") == 403                                                # admin of B inviting into A
    assert status_of(U(W["A"], role="viewer", uid=W["vw_a"]), invites.invite_list, W["A"]) == 403
    assert status_of(U(W["B"], uid=W["adm_b"]), invites.invite_list, W["A"]) == 403
    assert mk(U(W["A"], uid=W["adm_a"]), W["A"], role="viewer", email_note="not an email") == 422
    assert mk(U(W["A"], uid=W["adm_a"]), W["A"], role="viewer", email_note="a@b.com\nBcc: x@y.com") == 422           # no header injection
    assert mk(U(W["A"], uid=W["adm_a"]), W["A"], role="viewer", emails=[]) == 422
    for bad in (dict(expires_days=31), dict(expires_days=0), dict(max_uses=21), dict(max_uses=0), dict(role="owner"), dict(emails=["a@b.co"] * 21)):
        with pytest.raises(Exception): _body(**bad)
    gateway.DEMO_WS.add(W["C"])
    try:
        rows("INSERT INTO memberships(user_id,workspace_id,role) VALUES(%s,%s,'admin')", W["adm_a"], W["C"])
        assert mk(U(W["C"], uid=W["adm_a"]), W["C"], role="viewer") == 403                                            # the demo workspace never invites
    finally: gateway.DEMO_WS.discard(W["C"])
    assert rows("SELECT count(*) n FROM ws_invites WHERE workspace_id IN (%s,%s)", W["B"], W["C"])[0]["n"] == 0

def test_role_ceiling_is_the_inviters_role(W, monkeypatch):
    """Even if the minimum inviter role were lowered to editor later, an editor can never mint an admin invite; an admin can."""
    import invites
    R = __import__("fastapi").Response
    monkeypatch.setattr(invites, "INVITER_MIN_ROLE", "editor")
    ed = U(W["A"], role="editor", uid=W["ed_a"])
    assert status_of(ed, invites.invite_create, W["A"], _body(role="admin"), R()) == 403
    assert status_of(ed, invites.invite_create, W["A"], _body(role="editor"), R()) == 200
    assert status_of(ed, invites.invite_create, W["A"], _body(role="viewer"), R()) == 200
    assert status_of(U(W["A"], uid=W["adm_a"]), invites.invite_create, W["A"], _body(role="admin"), R()) == 200
    assert {r["role"] for r in rows("SELECT role FROM ws_invites WHERE workspace_id=%s", W["A"])} == {"editor", "viewer", "admin"}

def test_revoke_only_inside_the_own_workspace(W):
    import invites
    R = __import__("fastapi").Response
    ia = as_user(U(W["A"], uid=W["adm_a"]), invites.invite_create, W["A"], _body(role="viewer"), R())["id"]
    ib = as_user(U(W["B"], uid=W["adm_b"]), invites.invite_create, W["B"], _body(role="viewer"), R())["id"]
    assert status_of(U(W["A"], uid=W["adm_a"]), invites.invite_revoke, W["A"], ib) == 404                             # B's invite through A's path
    assert status_of(U(W["A"], uid=W["adm_a"]), invites.invite_revoke, W["B"], ib) == 403
    assert status_of(U(W["A"], role="editor", uid=W["ed_a"]), invites.invite_revoke, W["A"], ia) == 403
    assert rows("SELECT revoked_at FROM ws_invites WHERE id=%s", ib)[0]["revoked_at"] is None
    assert status_of(U(W["A"], uid=W["adm_a"]), invites.invite_revoke, W["A"], ia) == 200
    assert status_of(U(W["A"], uid=W["adm_a"]), invites.invite_revoke, W["A"], ia) == 404                             # twice
    assert as_user(U(W["A"], uid=W["adm_a"]), invites.invite_list, W["A"]) == []

# ====================================================================== part 2: over HTTP
@pytest.fixture(scope="module")
def smtp_srv():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _S); srv.daemon_threads = True
    f = srv.fake = Fake(); f.rcpt = []; f.messages = 0; f.auth = None; f.data = ""; f.port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start(); yield f; srv.shutdown(); srv.server_close()

@pytest.fixture(scope="module")
def srv(smtp_srv):
    envv = {**os.environ, "PORT": str(PORT), "PG_URI": PG, "AOS_EGRESS_ALLOW": "127.0.0.0/8,::1", "TELEGRAM_POLL": "0", "SLACK_SOCKET": "0", "AOS_ZALO_PERSONAL": "0",
            "AOS_PUBLIC_URL": B, "SMTP_HOST": "127.0.0.1", "SMTP_PORT": str(smtp_srv.port), "SMTP_STARTTLS": "0", "SMTP_FROM": "agentos@test.local"}
    os.makedirs(os.path.join(ROOT, "var"), exist_ok=True); log = open(os.path.join(ROOT, "var", f"server_{PORT}.log"), "wb")
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "overlay", "run_local.py")], cwd=ROOT, env=envv, stdout=log, stderr=subprocess.STDOUT)
    try:
        t0 = time.time()
        while time.time() - t0 < 300:
            try:
                if httpx.get(B + "/health", timeout=3).status_code < 500: break
            except httpx.HTTPError: time.sleep(1)
        else: raise AssertionError("own server on :8056 did not start")
        yield p
    finally:
        p.terminate()                                                                     # this PID only: never pkill run_local.py
        try: p.wait(timeout=20)
        except subprocess.TimeoutExpired: p.kill()
        log.close()

_N = [0]
def fresh_ip():
    _N[0] += 1; return f"{TEST_IP_PREFIX}{_N[0] // 250}.{_N[0] % 250 + 1}"

def pub(method, path, ip=None, cookie=None, **kw):
    h = {"X-Forwarded-For": ip or fresh_ip(), **kw.pop("headers", {})}
    if cookie: h["Cookie"] = f"aos_s={cookie}"
    return httpx.request(method, B + path, headers=h, timeout=60, **kw)

def cookie_of(r):
    m = re.search(r"aos_s=([^;]+)", " ".join(r.headers.get_list("set-cookie")))
    return m.group(1) if m else None

def mail_text(raw):
    import email
    return "\n".join(p.get_payload(decode=True).decode("utf-8", "replace") for p in email.message_from_string(raw).walk() if not p.is_multipart())

def token_of(link): return link.rsplit("/invite/", 1)[1]
def accept(token, ip=None, **body): return pub("POST", f"/api/invites/{token}/accept", ip=ip, json=body)
def uname(tag): return f"wt_{tag}_{SUF}"

class H:   # the HTTP world
    pass

@pytest.fixture(scope="module")
def world(srv):
    h = H(); h.cl = {}
    with httpx.Client(base_url=B, auth=ADMIN, timeout=60) as a:
        h.a = a
        h.A = a.post("/api/admin/workspaces", json={"name": f"wt-A-{SUF}"}).json()["id"]; h.Bw = a.post("/api/admin/workspaces", json={"name": f"wt-B-{SUF}"}).json()["id"]
        for k, role, ws, extra in (("adma", "admin", h.A, {}), ("eda", "editor", h.A, {}), ("vwa", "viewer", h.A, {}), ("admb", "admin", h.Bw, {}), ("demoa", "viewer", h.A, {"demo": True}),
                                   ("exist", "admin", h.Bw, {}), ("tmpadm", "admin", h.A, {}), ("tmpadm2", "admin", h.A, {})):
            r = a.post("/api/admin/users", json={"username": uname(k), "password": PW, "role": role, "workspace_id": ws, **extra}); assert r.status_code == 200, r.text
            setattr(h, "id_" + k, r.json()["id"])
            h.cl[k] = httpx.Client(base_url=B, auth=(uname(k), PW), timeout=60)
        yield h
        for c in h.cl.values(): c.close()

def list_all(h, cl=None): return (cl or h.cl["adma"]).get(f"/api/admin/workspaces/{h.A}/invites")

def mk_invite(h, role="viewer", cl="adma", wid=None, **kw):
    r = h.cl[cl].post(f"/api/admin/workspaces/{wid or h.A}/invites", json={"role": role, **kw}); assert r.status_code == 200, r.text
    return r.json()

def used(iid): return rows("SELECT used_count FROM ws_invites WHERE id=%s", iid)[0]["used_count"]

def test_http_rename_roles(world):
    h = world; body = {"name": f"wt-A-{SUF}-renamed"}
    for k in ("eda", "vwa", "demoa"): assert h.cl[k].patch(f"/api/admin/workspaces/{h.A}", json=body).status_code == 403, k
    assert h.cl["admb"].patch(f"/api/admin/workspaces/{h.A}", json=body).status_code == 403                           # admin of B on A
    assert h.cl["adma"].patch(f"/api/admin/workspaces/{h.Bw}", json=body).status_code == 403                          # admin of A on B
    assert httpx.patch(f"{B}/api/admin/workspaces/{h.A}", json=body).status_code == 401
    assert h.cl["adma"].patch(f"/api/admin/workspaces/{h.A}", json={"name": f"wt-B-{SUF}"}).status_code == 409
    assert h.cl["adma"].patch(f"/api/admin/workspaces/{h.A}", json=body).status_code == 200
    me = h.cl["adma"].get("/api/auth/me").json(); assert [w["name"] for w in me["workspaces"]] == [f"wt-A-{SUF}-renamed"]
    assert h.cl["adma"].get(f"/api/admin/workspaces/{h.A}").json()["members"] >= 6
    assert h.a.patch("/api/admin/workspaces/1", json={"name": "renamed default"}).status_code == 403                    # workspace 1, even for the superadmin
    assert h.cl["adma"].delete(f"/api/admin/workspaces/{h.A}").status_code in (404, 405)                              # no delete route
    assert rows("SELECT name FROM workspaces WHERE id=1")[0]["name"] != "renamed default"
    assert any(r["status"] == 200 and r["method"] == "PATCH" for r in rows("SELECT method,status FROM audit_log WHERE path=%s", f"/api/admin/workspaces/{h.A}"))   # the audit middleware logged it

def test_http_only_admin_of_the_workspace_invites(world):
    h = world; p = lambda k, wid=None, **kw: h.cl[k].post(f"/api/admin/workspaces/{wid or h.A}/invites", json={"role": "viewer", **kw})
    for k in ("vwa", "eda", "demoa"): assert p(k).status_code == 403, k                                               # viewer / editor / demo account
    assert p("admb").status_code == 403 and p("admb", h.Bw).status_code == 200                                        # admin of B: not into A, yes into B
    assert p("adma", h.Bw).status_code == 403                                                                         # admin of A into B
    assert httpx.post(f"{B}/api/admin/workspaces/{h.A}/invites", json={"role": "viewer"}).status_code == 401
    assert h.cl["vwa"].get(f"/api/admin/workspaces/{h.A}/invites").status_code == 403 and h.cl["admb"].get(f"/api/admin/workspaces/{h.A}/invites").status_code == 403
    assert h.cl["eda"].delete(f"/api/admin/workspaces/{h.A}/invites/1").status_code == 403
    assert p("adma", role="owner").status_code == 422 and p("adma", expires_days=99).status_code == 422 and p("adma", max_uses=99).status_code == 422
    assert p("adma", email_note="nope").status_code == 422

def test_http_full_flow_new_account_gets_the_right_role_and_workspace(world, smtp_srv):
    h = world; before = smtp_srv.messages
    inv = mk_invite(h, "editor", email_note=f"{uname('newbie')}@example.com")
    link, token = inv["link"], token_of(inv["link"])
    assert inv["emailed"] is True and smtp_srv.messages == before + 1 and link in mail_text(smtp_srv.data)            # the e-mail carries the link
    assert link.startswith(B + "/invite/") and len(token) == 43
    pg = pub("GET", "/invite/" + token); assert pg.status_code == 200 and rows("SELECT name FROM workspaces WHERE id=%s", h.A)[0]["name"] in pg.text and "editor" in pg.text and pg.headers["cache-control"] == "no-store"
    assert pg.headers["referrer-policy"] == "no-referrer" and token not in pg.text
    ip = fresh_ip()
    assert accept(token, ip, username=uname("newbie"), password="short").status_code == 422 and used(inv["id"]) == 0    # weak password: not consumed
    assert accept(token, ip, username="bad name!", password=PW).status_code == 422 and used(inv["id"]) == 0
    assert accept(token, ip).status_code == 422 and used(inv["id"]) == 0
    r = accept(token, ip, username=uname("newbie"), display_name="Newbie", password=PW); assert r.status_code == 200, r.text
    j = r.json(); assert j["workspace_id"] == h.A and j["role"] == "editor" and j["created"] is True and cookie_of(r)
    assert rows("SELECT workspace_id, role FROM memberships WHERE user_id=(SELECT id FROM users WHERE username=%s)", uname("newbie")) == [{"workspace_id": h.A, "role": "editor"}]
    me = pub("GET", "/api/auth/me", cookie=cookie_of(r)).json(); assert me["ws"] == h.A and me["role"] == "editor" and [w["id"] for w in me["workspaces"]] == [h.A]
    assert pub("POST", "/api/agents", cookie=cookie_of(r), json={}).status_code != 401                                  # the session works
    assert accept(token, ip, username=uname("newbie2"), password=PW).status_code == 404                              # reuse of a single-use link
    assert pub("GET", "/invite/" + token).status_code == 404
    assert rows("SELECT count(*) n FROM users WHERE username=%s", uname("newbie2"))[0]["n"] == 0
    assert list_all(h).json() == [] or token not in json.dumps(list_all(h).json())
    # the token / hash never reach the audit log, the outbox, the invite table or the invite list
    for t in (token, inv["link"]):
        assert rows("SELECT count(*) n FROM audit_log WHERE path LIKE %s OR detail LIKE %s OR username LIKE %s", f"%{t}%", f"%{t}%", f"%{t}%")[0]["n"] == 0
        assert rows("SELECT count(*) n FROM outbox WHERE link LIKE %s OR subject LIKE %s OR body LIKE %s", f"%{t}%", f"%{t}%", f"%{t}%")[0]["n"] == 0
    assert rows("SELECT count(*) n FROM audit_log WHERE path='/api/invites/*/accept' AND status=200")[0]["n"] >= 1       # the accept IS audited, with a masked path
    assert token not in json.dumps(rows("SELECT * FROM ws_invites WHERE id=%s", inv["id"]), default=str)

def test_http_username_taken_does_not_burn_the_invite(world):
    h = world; inv = mk_invite(h, "viewer"); t = token_of(inv["link"]); ip = fresh_ip()
    for taken in (uname("adma"), uname("adma").upper(), "moza", "MOZA"):                                             # exact and different-case names are taken
        r = accept(t, ip, username=taken, password=PW); assert r.status_code == 409, (taken, r.text)
    assert used(inv["id"]) == 0
    assert accept(t, ip, username=uname("free1"), password=PW).status_code == 200 and used(inv["id"]) == 1

def test_http_dead_tokens_all_answer_the_same_404(world):
    h = world; ip = fresh_ip(); ip2 = fresh_ip(); dead = {}
    exp = mk_invite(h); rows("UPDATE ws_invites SET expires_at = now() - interval '1 minute' WHERE id=%s", exp["id"]); dead["expired"] = token_of(exp["link"])
    rev = mk_invite(h); assert h.cl["adma"].delete(f"/api/admin/workspaces/{h.A}/invites/{rev['id']}").status_code == 200; dead["revoked"] = token_of(rev["link"])
    live = mk_invite(h); lt = token_of(live["link"])
    dead["forged"] = lt[:-1] + ("A" if lt[-1] != "A" else "B")
    dead["random"] = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    dead["too short"] = "abc"; dead["too long"] = lt + "x"; dead["with dots"] = "." * 43
    dead["the stored hash"] = rows("SELECT token_hash FROM ws_invites WHERE id=%s", live["id"])[0]["token_hash"]        # a leaked DB row must not open the door
    dead["empty-ish"] = "%20"
    seen = set()
    for why, t in dead.items():
        r = accept(t, ip, username=uname("ghost"), password=PW); assert r.status_code == 404, (why, r.status_code); seen.add(r.text)
        g = pub("GET", "/invite/" + t, ip=ip2); assert g.status_code == 404, why
    assert len(seen) == 1                                                                                            # one answer for every kind of "no"
    assert rows("SELECT count(*) n FROM users WHERE username=%s", uname("ghost"))[0]["n"] == 0
    assert used(live["id"]) == 0

def test_http_other_workspace_isolation(world):
    h = world
    inv = mk_invite(h, "admin", cl="admb", wid=h.Bw); r = accept(token_of(inv["link"]), username=uname("inb"), password=PW); assert r.status_code == 200
    me = pub("GET", "/api/auth/me", cookie=cookie_of(r)).json(); assert [(w["id"], w["role"]) for w in me["workspaces"]] == [(h.Bw, "admin")]
    assert pub("GET", f"/api/admin/workspaces/{h.A}/invites", cookie=cookie_of(r)).status_code == 403                  # the new admin of B sees nothing of A
    assert pub("POST", f"/api/admin/workspaces/{h.A}/invites", cookie=cookie_of(r), json={"role": "viewer"}).status_code == 403
    assert pub("PATCH", f"/api/admin/workspaces/{h.A}", cookie=cookie_of(r), json={"name": "stolen"}).status_code == 403
    ia = mk_invite(h, "viewer"); assert ia["id"] not in [x["id"] for x in h.cl["admb"].get(f"/api/admin/workspaces/{h.Bw}/invites").json()]
    assert h.cl["admb"].delete(f"/api/admin/workspaces/{h.Bw}/invites/{ia['id']}").status_code == 404                  # A's invite id through B's path
    assert rows("SELECT revoked_at FROM ws_invites WHERE id=%s", ia["id"])[0]["revoked_at"] is None

def test_http_role_ceiling_and_stale_inviter(world):
    h = world
    assert h.cl["eda"].post(f"/api/admin/workspaces/{h.A}/invites", json={"role": "admin"}).status_code == 403          # editor minting an admin invite
    assert h.cl["vwa"].post(f"/api/admin/workspaces/{h.A}/invites", json={"role": "viewer"}).status_code == 403
    inv = mk_invite(h, "admin", cl="tmpadm"); t = token_of(inv["link"])                                              # an admin may invite an admin
    h.a.post(f"/api/admin/workspaces/{h.A}/members", json={"user_id": h.id_tmpadm, "role": "editor"})                 # ... who is demoted before the link is used
    assert accept(t, username=uname("esc1"), password=PW).status_code == 404 and used(inv["id"]) == 0
    assert pub("GET", "/invite/" + t).status_code == 404
    inv2 = mk_invite(h, "editor", cl="tmpadm2"); t2 = token_of(inv2["link"])
    assert h.a.patch(f"/api/admin/users/{h.id_tmpadm2}", json={"disabled": True}).status_code == 200                  # a disabled inviter kills the link too
    assert accept(t2, username=uname("esc2"), password=PW).status_code == 404
    h.a.patch(f"/api/admin/users/{h.id_tmpadm2}", json={"disabled": False})
    assert accept(t2, username=uname("esc2"), password=PW).status_code == 200                                         # enabled again: it works (the check is live, not cached)
    adm = mk_invite(h, "admin"); r = accept(token_of(adm["link"]), username=uname("newadm"), password=PW); assert r.json()["role"] == "admin"
    assert rows("SELECT is_superadmin FROM users WHERE username=%s", uname("newadm"))[0]["is_superadmin"] is False

def test_http_signed_in_user_gets_a_membership_and_is_never_downgraded(world):
    h = world
    login = pub("POST", "/api/auth/login", json={"username": uname("exist"), "password": PW}); assert login.status_code == 200, login.text
    ck = cookie_of(login)
    inv = mk_invite(h, "viewer"); r = pub("POST", f"/api/invites/{token_of(inv['link'])}/accept", cookie=ck, json={}); assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "workspace_id": h.A, "workspace": r.json()["workspace"], "role": "viewer", "created": False}
    roles = {x["workspace_id"]: x["role"] for x in rows("SELECT workspace_id, role FROM memberships WHERE user_id=%s", h.id_exist)}
    assert roles == {h.Bw: "admin", h.A: "viewer"}                                                                   # joined A as viewer, B untouched
    assert pub("GET", "/api/auth/me", cookie=ck).json()["ws"] == h.A                                                 # the session lands in the new workspace
    inv2 = mk_invite(h, "editor"); assert pub("POST", f"/api/invites/{token_of(inv2['link'])}/accept", cookie=ck, json={}).json()["role"] == "editor"   # upgrade
    inv3 = mk_invite(h, "viewer"); assert pub("POST", f"/api/invites/{token_of(inv3['link'])}/accept", cookie=ck, json={}).json()["role"] == "editor"   # never down
    assert rows("SELECT role FROM memberships WHERE user_id=%s AND workspace_id=%s", h.id_exist, h.A)[0]["role"] == "editor"
    pg = pub("GET", "/invite/" + token_of(mk_invite(h, "viewer")["link"]), cookie=ck); assert pg.status_code == 200 and "const SIGNED=true" in pg.text and uname("exist") in pg.text
    assert pub("GET", "/invite/" + token_of(mk_invite(h, "viewer")["link"])).text.count("const SIGNED=false") == 1

def test_http_demo_account_cannot_accept_and_demo_workspace_cannot_invite(world):
    h = world; login = pub("POST", "/api/auth/login", json={"username": uname("demoa"), "password": PW}); assert login.status_code == 200, login.text
    inv = mk_invite(h, "viewer"); r = pub("POST", f"/api/invites/{token_of(inv['link'])}/accept", cookie=cookie_of(login), json={})
    assert r.status_code == 403 and used(inv["id"]) == 0
    assert rows("SELECT count(*) n FROM memberships WHERE user_id=%s", h.id_demoa)[0]["n"] == 1
    # a workspace flagged demo: no new invites, no rename, and invites made before the flag are dead
    pre = mk_invite(h, "viewer"); rows("UPDATE workspaces SET demo=true WHERE id=%s", h.A)
    try:
        assert h.cl["adma"].post(f"/api/admin/workspaces/{h.A}/invites", json={"role": "viewer"}).status_code == 403
        assert h.cl["adma"].patch(f"/api/admin/workspaces/{h.A}", json={"name": "demo rename"}).status_code == 403
        assert accept(token_of(pre["link"]), username=uname("indemo"), password=PW).status_code == 404 and pub("GET", "/invite/" + token_of(pre["link"])).status_code == 404
    finally: rows("UPDATE workspaces SET demo=false WHERE id=%s", h.A)
    assert rows("SELECT count(*) n FROM users WHERE username=%s", uname("indemo"))[0]["n"] == 0 and used(pre["id"]) == 0

def test_http_two_parallel_accepts_of_a_one_use_invite_exactly_one_wins(world):
    h = world; inv = mk_invite(h, "viewer"); t = token_of(inv["link"])
    with cf.ThreadPoolExecutor(8) as ex:
        codes = list(ex.map(lambda i: accept(t, fresh_ip(), username=uname(f"par{i}"), password=PW).status_code, range(8)))
    assert sorted(codes) == [200] + [404] * 7, codes
    assert used(inv["id"]) == 1 and rows("SELECT count(*) n FROM users WHERE username LIKE %s", f"wt\\_par%\\_{SUF}")[0]["n"] == 1
    inv3 = mk_invite(h, "viewer", max_uses=3); t3 = token_of(inv3["link"])
    with cf.ThreadPoolExecutor(8) as ex:
        codes = list(ex.map(lambda i: accept(t3, fresh_ip(), username=uname(f"mu{i}"), password=PW).status_code, range(6)))
    assert sorted(codes) == [200] * 3 + [404] * 3, codes
    assert used(inv3["id"]) == 3

def test_http_same_username_in_parallel_creates_one_account_and_burns_one_use(world):
    h = world; inv = mk_invite(h, "viewer", max_uses=5); t = token_of(inv["link"])
    with cf.ThreadPoolExecutor(5) as ex:
        codes = list(ex.map(lambda i: accept(t, fresh_ip(), username=uname("same"), password=PW).status_code, range(5)))
    assert sorted(codes) == [200, 409, 409, 409, 409], codes
    assert used(inv["id"]) == 1                                                                                      # the losers were rolled back: their use was not consumed

def test_http_bulk_invites_and_list(world, smtp_srv):
    h = world; before = smtp_srv.messages
    mails = [f"{uname('b1')}@example.com", f"{uname('b2')}@example.com", f"{uname('b1')}@example.com"]
    r = h.cl["adma"].post(f"/api/admin/workspaces/{h.A}/invites", json={"role": "viewer", "emails": mails, "max_uses": 9}); assert r.status_code == 200, r.text
    inv = r.json()["invites"]; assert len(inv) == 2 and all(i["max_uses"] == 1 and i["emailed"] for i in inv) and smtp_srv.messages == before + 2   # de-duplicated, single-use each
    assert len({i["link"] for i in inv}) == 2
    assert h.cl["adma"].post(f"/api/admin/workspaces/{h.A}/invites", json={"role": "viewer", "emails": [f"a{i}@example.com" for i in range(21)]}).status_code == 422
    assert h.cl["adma"].post(f"/api/admin/workspaces/{h.A}/invites", json={"role": "viewer", "emails": ["bad"]}).status_code == 422
    lst = list_all(h).json(); txt = json.dumps(lst)
    for i in inv: assert token_of(i["link"]) not in txt and hashlib.sha256(token_of(i["link"]).encode()).hexdigest() not in txt
    assert {"id", "role", "email_note", "created_at", "expires_at", "used_count", "max_uses"} <= set(lst[0]) and not ({"token", "token_hash", "link"} & set(lst[0]))
    note = [x for x in lst if x["email_note"] == mails[0]][0]; assert note["role"] == "viewer"
    assert rows("SELECT count(*) n FROM users WHERE username = ANY(%s)", mails)[0]["n"] == 0                          # the address is never turned into a user

def test_http_rate_limit_on_bad_tokens_per_ip(world):
    h = world; bad_ip, ok_ip = fresh_ip(), fresh_ip()
    inv = mk_invite(h, "viewer"); good = token_of(inv["link"])
    for i in range(20):
        assert accept(base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("="), bad_ip, username="x", password=PW).status_code == 404, i
    n = rows("SELECT count(*) n FROM audit_log WHERE ip=%s AND detail=%s", bad_ip, "mời: token sai")[0]["n"]; assert n == 20
    r = accept(good, bad_ip, username=uname("rl1"), password=PW); assert r.status_code == 429 and r.headers.get("retry-after")   # even the RIGHT token is refused from that IP
    assert pub("GET", "/invite/" + good, ip=bad_ip).status_code == 429
    assert accept(base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("="), bad_ip, username="x", password=PW).status_code == 429
    assert rows("SELECT count(*) n FROM audit_log WHERE ip=%s AND detail=%s", bad_ip, "mời: token sai")[0]["n"] == 20   # a limited request writes nothing: the table cannot be filled from one IP
    assert used(inv["id"]) == 0 and rows("SELECT count(*) n FROM users WHERE username=%s", uname("rl1"))[0]["n"] == 0
    assert accept(good, ok_ip, username=uname("rl2"), password=PW).status_code == 200                                 # another IP is not affected

def test_access_log_never_contains_an_invite_token(capsys, caplog):
    import srv_parity
    import logging; caplog.set_level(logging.INFO)
    tok = "A" * 43
    for path in (f"/invite/{tok}", f"/api/invites/{tok}/accept"):
        run(srv_parity._log_request({"method": "POST", "path": path, "client": ("10.12.9.9", 0)}, {}, 404, 3))
    cap = capsys.readouterr(); out = cap.err + cap.out + caplog.text
    assert tok not in out and "/invite/* ->" in out and "/api/invites/*/accept ->" in out, out

def test_http_extra_body_fields_cannot_escalate_and_pages_are_bilingual(world):
    h = world; inv = mk_invite(h, "viewer"); t = token_of(inv["link"])
    r = accept(t, username=uname("sneak"), password=PW, role="admin", workspace_id=1, is_superadmin=True, demo=False, workspaces=[1]); assert r.status_code == 200, r.text
    assert r.json()["role"] == "viewer" and r.json()["workspace_id"] == h.A
    assert rows("SELECT workspace_id, role FROM memberships WHERE user_id=(SELECT id FROM users WHERE username=%s)", uname("sneak")) == [{"workspace_id": h.A, "role": "viewer"}]
    assert rows("SELECT is_superadmin FROM users WHERE username=%s", uname("sneak"))[0]["is_superadmin"] is False
    t2 = token_of(mk_invite(h, "viewer")["link"])
    en = pub("GET", "/invite/" + t2); vi = pub("GET", "/invite/" + t2, headers={"Cookie": "aos_lang=vi"})
    body = lambda r: r.text.split("<body>", 1)[1].split("<script>", 1)[0]                                            # the visible markup (the dictionary inside the script legitimately holds both languages)
    assert "You are invited to join the workspace" in body(en) and "Bạn được mời tham gia workspace" not in body(en) and "Bạn được mời tham gia workspace" in body(vi)
    bad = accept("Z" * 43); assert bad.status_code == 404 and bad.json()["detail"] == "This invitation does not exist or has expired"   # API errors are English
    js = lambda r: r.text.split("<body>", 1)[1].split("<script>", 1)[1]
    assert "'The two passwords do not match'" in js(en) and "'Hai mật khẩu không khớp'" in js(vi)
