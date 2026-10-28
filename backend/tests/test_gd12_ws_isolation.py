"""GĐ12.100b + 12.100c + 12.100d (sessions + audit): an admin of workspace A can NOT manage workspace B (members, rename, list, create users, demo builder, users list/reset/lock/delete, trace routes of B's projects). $0, fake LLM, OUR OWN server on :8062.
The platform owner (users.is_superadmin, `moza`) keeps managing every workspace. Every row this test creates is removed again; the server is killed by its PID."""
import os, subprocess, sys, time, uuid
os.environ.setdefault("GOOGLE_API_KEY", "x-not-used")
import httpx, psycopg, pytest
from test_gd6_notify import PG

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
SUF = uuid.uuid4().hex[:6]
PORT = 8062; B = f"http://127.0.0.1:{PORT}"; OWNER = ("moza", "phase0-local"); PW = "phase0-gd12100b-local"

def db(sql, *a):
    with psycopg.connect(PG, autocommit=True) as c:
        cur = c.execute(sql, a)
        return cur.fetchall() if cur.description else []

def _cleanup():
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute("DELETE FROM audit_log WHERE username LIKE %s OR path LIKE %s", (f"iso\\_%\\_{SUF}", f"%iso-%-{SUF}%"))
        c.execute("DELETE FROM sessions WHERE token_hash LIKE %s", (f"iso-%-{SUF}%",))
        c.execute("DELETE FROM tr_runs WHERE project_id IN (SELECT id FROM projects WHERE name LIKE %s)", (f"iso-proj-%-{SUF}",))
        c.execute("DELETE FROM projects WHERE name LIKE %s", (f"iso-proj-%-{SUF}",))
        c.execute("DELETE FROM users WHERE username LIKE %s", (f"iso\\_%\\_{SUF}",))
        c.execute("DELETE FROM workspaces WHERE name LIKE %s", (f"iso-%-{SUF}%",))

@pytest.fixture(scope="module", autouse=True)
def clean():
    _cleanup(); yield; _cleanup()

@pytest.fixture(scope="module")
def srv():
    envv = {**os.environ, "PORT": str(PORT), "PG_URI": PG, "AOS_EGRESS_ALLOW": "127.0.0.0/8,::1", "TELEGRAM_POLL": "0", "SLACK_SOCKET": "0", "AOS_ZALO_PERSONAL": "0"}
    os.makedirs(os.path.join(ROOT, "var"), exist_ok=True); log = open(os.path.join(ROOT, "var", f"server_{PORT}.log"), "wb")
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "overlay", "run_local.py")], cwd=ROOT, env=envv, stdout=log, stderr=subprocess.STDOUT)
    try:
        t0 = time.time()
        while time.time() - t0 < 300:
            try:
                if httpx.get(B + "/health", timeout=3).status_code < 500: break
            except httpx.HTTPError: time.sleep(1)
        else: raise AssertionError(f"own server on :{PORT} did not start")
        yield p
    finally:
        p.terminate()                                                                     # this PID only: never pkill run_local.py
        try: p.wait(timeout=20)
        except subprocess.TimeoutExpired: p.kill()
        log.close()

def uname(k): return f"iso_{k}_{SUF}"

@pytest.fixture(scope="module")
def w(srv):
    class H: pass
    h = H(); h.cl = {}
    with httpx.Client(base_url=B, auth=OWNER, timeout=60) as o:
        h.o = o
        h.A = o.post("/api/admin/workspaces", json={"name": f"iso-A-{SUF}"}).json()["id"]; h.B = o.post("/api/admin/workspaces", json={"name": f"iso-B-{SUF}"}).json()["id"]
        for k, role, ws in (("adma", "admin", h.A), ("eda", "editor", h.A), ("admb", "admin", h.B), ("victim", "viewer", h.B)):
            r = o.post("/api/admin/users", json={"username": uname(k), "password": PW, "role": role, "workspace_id": ws}); assert r.status_code == 200, r.text
            setattr(h, "id_" + k, r.json()["id"]); h.cl[k] = httpx.Client(base_url=B, auth=(uname(k), PW), timeout=60)
        h.owner_id = db("SELECT id FROM users WHERE username='moza'")[0][0]
        yield h
        for c in h.cl.values(): c.close()

def role_in(uid, ws):
    r = db("SELECT role FROM memberships WHERE user_id=%s AND workspace_id=%s", uid, ws); return r[0][0] if r else None

def test_admin_a_cannot_touch_members_of_b(w):
    a = w.cl["adma"]
    r = a.post(f"/api/admin/workspaces/{w.B}/members", json={"user_id": w.id_adma, "role": "admin"})   # make myself admin of B
    assert r.status_code == 403, r.text
    assert role_in(w.id_adma, w.B) is None
    assert a.post(f"/api/admin/workspaces/{w.B}/members", json={"user_id": w.id_eda, "role": "viewer"}).status_code == 403
    assert a.delete(f"/api/admin/workspaces/{w.B}/members/{w.id_victim}").status_code == 403
    assert role_in(w.id_victim, w.B) == "viewer"                                          # nothing changed
    assert a.get(f"/api/admin/workspaces/{w.B}").status_code == 403
    assert a.patch(f"/api/admin/workspaces/{w.B}", json={"name": f"iso-B-{SUF}-stolen"}).status_code == 403
    assert db("SELECT name FROM workspaces WHERE id=%s", w.B)[0][0] == f"iso-B-{SUF}"
    assert a.get(f"/api/admin/workspaces/{w.B}/invites").status_code == 403
    assert a.post(f"/api/admin/workspaces/{w.B}/invites", json={"role": "viewer"}).status_code == 403
    assert a.post("/api/admin/workspaces/999999/members", json={"user_id": w.id_adma, "role": "admin"}).status_code == 403   # unknown id: same answer, no probing

def test_user_new_other_workspace_refused(w):
    a = w.cl["adma"]; name = uname("evil")
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": "admin", "workspace_id": w.B})
    assert r.status_code == 403, r.text
    assert not db("SELECT 1 FROM users WHERE username=%s", name)                            # refused BEFORE the row is created
    assert a.post("/api/admin/users", json={"username": name, "password": PW, "role": "viewer", "workspace_id": 1}).status_code == 403   # default workspace too
    assert a.post("/api/admin/users", json={"username": name, "password": PW, "role": "viewer", "workspace_id": 999999}).status_code == 403
    assert not db("SELECT 1 FROM users WHERE username=%s", name)
    ok = a.post("/api/admin/users", json={"username": uname("good"), "password": PW, "role": "editor", "workspace_id": w.A}); assert ok.status_code == 200, ok.text   # own workspace still works
    assert role_in(ok.json()["id"], w.A) == "editor"

def test_workspace_list_only_own(w):
    mine = {x["id"] for x in w.cl["adma"].get("/api/admin/workspaces").json()}
    assert mine == {w.A}, mine                                                              # not B, not workspace 1
    assert {x["id"] for x in w.cl["admb"].get("/api/admin/workspaces").json()} == {w.B}
    allw = {x["id"] for x in w.o.get("/api/admin/workspaces").json()}
    assert {1, w.A, w.B} <= allw                                                            # the platform owner still lists everything

def test_editor_and_anonymous_refused(w):
    e = w.cl["eda"]
    assert e.get("/api/admin/workspaces").status_code == 403
    assert e.post(f"/api/admin/workspaces/{w.A}/members", json={"user_id": w.id_eda, "role": "admin"}).status_code == 403   # editor cannot promote itself
    assert role_in(w.id_eda, w.A) == "editor"
    assert httpx.post(f"{B}/api/admin/workspaces/{w.A}/members", json={"user_id": w.id_eda, "role": "admin"}).status_code == 401
    assert httpx.get(f"{B}/api/admin/workspaces").status_code == 401

def test_admin_cannot_demote_or_remove_the_platform_owner(w):
    a = w.cl["adma"]
    assert db("SELECT is_superadmin FROM users WHERE id=%s", w.owner_id)[0][0]
    w.o.post(f"/api/admin/workspaces/{w.A}/members", json={"user_id": w.owner_id, "role": "admin"})   # owner is admin of A
    assert a.post(f"/api/admin/workspaces/{w.A}/members", json={"user_id": w.owner_id, "role": "viewer"}).status_code == 403   # an admin of A cannot demote the owner in A
    assert role_in(w.owner_id, w.A) == "admin"
    assert a.delete(f"/api/admin/workspaces/{w.A}/members/{w.owner_id}").status_code == 403
    assert role_in(w.owner_id, w.A) == "admin"

def test_unknown_user_and_role_escalation(w):
    a = w.cl["adma"]
    assert a.post(f"/api/admin/workspaces/{w.A}/members", json={"user_id": 99999999, "role": "viewer"}).status_code == 403   # GĐ12.100c: unknown id = same answer as "belongs to another workspace" (no probing)
    assert w.o.post(f"/api/admin/workspaces/{w.A}/members", json={"user_id": 99999999, "role": "viewer"}).status_code == 404   # the platform owner still gets the real reason
    assert a.post(f"/api/admin/workspaces/{w.A}/members", json={"user_id": w.id_eda, "role": "superadmin"}).status_code == 422
    # an API key limited to editor cannot use the admin routes even though its user is an admin
    k = a.post("/api/auth/keys", json={"name": "iso", "role": "editor"}).json()["key"]
    kc = httpx.Client(base_url=B, headers={"X-Api-Key": k}, timeout=60)
    assert kc.post(f"/api/admin/workspaces/{w.A}/members", json={"user_id": w.id_eda, "role": "admin"}).status_code == 403
    assert kc.post("/api/admin/users", json={"username": uname("k"), "password": PW, "role": "admin"}).status_code == 403
    assert role_in(w.id_eda, w.A) == "editor"
    # a full admin key of A is bound to A: it cannot act on B even with the header trick
    k2 = a.post("/api/auth/keys", json={"name": "iso2"}).json()["key"]
    kc2 = httpx.Client(base_url=B, headers={"X-Api-Key": k2}, timeout=60)
    assert kc2.post(f"/api/admin/workspaces/{w.B}/members", json={"user_id": w.id_adma, "role": "admin"}).status_code == 403
    assert {x["id"] for x in kc2.get("/api/admin/workspaces").json()} == {w.A}
    kc.close(); kc2.close()

def test_platform_owner_still_manages_everything(w):
    o = w.o
    assert o.post(f"/api/admin/workspaces/{w.B}/members", json={"user_id": w.id_eda, "role": "viewer"}).status_code == 200
    assert role_in(w.id_eda, w.B) == "viewer"
    assert o.delete(f"/api/admin/workspaces/{w.B}/members/{w.id_eda}").status_code == 200
    assert o.patch(f"/api/admin/workspaces/{w.B}", json={"name": f"iso-B-{SUF}-ok"}).status_code == 200
    assert o.post("/api/admin/users", json={"username": uname("byowner"), "password": PW, "role": "viewer", "workspace_id": w.B}).status_code == 200

def test_demo_workspace_untouched(w):
    dws = db("INSERT INTO workspaces(name, demo) VALUES(%s, true) RETURNING id", f"iso-demo-{SUF}")[0][0]
    try:
        db("INSERT INTO memberships(user_id,workspace_id,role) VALUES(%s,%s,'admin') ON CONFLICT DO NOTHING", w.id_adma, dws)   # even a (stray) admin there cannot manage the demo workspace
        a = w.cl["adma"]
        assert a.post(f"/api/admin/workspaces/{dws}/members", json={"user_id": w.id_eda, "role": "viewer"}).status_code == 403
        assert a.delete(f"/api/admin/workspaces/{dws}/members/{w.id_adma}").status_code == 403
        assert a.post("/api/admin/users", json={"username": uname("indemo"), "password": PW, "role": "viewer", "workspace_id": dws}).status_code == 403
        assert role_in(w.id_eda, dws) is None
        # the demo builder copies agents / KB / skills from ANY workspace: owner only
        assert a.post("/api/admin/demo-workspace", json={"user_id": w.id_victim, "name": f"iso-demo2-{SUF}", "source_agents": [2]}).status_code == 403
        assert not db("SELECT 1 FROM workspaces WHERE name=%s", f"iso-demo2-{SUF}")
    finally:
        db("DELETE FROM memberships WHERE workspace_id=%s", dws); db("DELETE FROM workspaces WHERE id=%s", dws)


# ------------------------------------------------------------------ GĐ12.100c: /api/admin/users scoped to the workspaces the caller administers
def _mkuser(o, k, role, ws):
    r = o.post("/api/admin/users", json={"username": uname(k), "password": PW, "role": role, "workspace_id": ws}); assert r.status_code == 200, r.text
    return r.json()["id"]

def _pw(uid): return db("SELECT pw_hash FROM users WHERE id=%s", uid)[0][0]

def test_users_list_only_own_workspaces(w):
    shared = _mkuser(w.o, "shared", "editor", w.A); assert w.o.post(f"/api/admin/workspaces/{w.B}/members", json={"user_id": shared, "role": "viewer"}).status_code == 200
    solo = _mkuser(w.o, "solo", "viewer", w.A)
    rows = {u["id"]: u for u in w.cl["adma"].get("/api/admin/users").json()}
    assert {w.id_adma, w.id_eda, shared, solo} <= set(rows)
    assert not ({w.id_victim, w.id_admb} & set(rows)), "users of workspace B are listed to the admin of A"
    for u in rows.values(): assert all(m["workspace_id"] == w.A for m in u["memberships"]), u   # the memberships of other workspaces are not shown
    assert rows[shared]["manageable"] is False and rows[solo]["manageable"] is True
    allu = {u["id"] for u in w.o.get("/api/admin/users").json()}
    assert {w.id_victim, w.id_admb, w.id_adma, shared} <= allu                                    # the platform owner still sees everyone
    listed_b = {u["id"] for u in w.cl["admb"].get("/api/admin/users").json()}
    assert {w.id_admb, w.id_victim, shared} <= listed_b and w.id_adma not in listed_b

def test_user_reset_lock_delete_cross_workspace_refused(w):
    a = w.cl["adma"]; shared = db("SELECT id FROM users WHERE username=%s", uname("shared"))[0][0]
    for target in (w.id_victim, w.id_admb, shared, w.owner_id, 99999999):                          # B-only users, a user also in B, the platform owner, an unknown id
        h = _pw(target) if target != 99999999 else None
        assert a.patch(f"/api/admin/users/{target}", json={"password": PW + "-new"}).status_code == 403, target
        assert a.patch(f"/api/admin/users/{target}", json={"disabled": True}).status_code == 403, target
        assert a.patch(f"/api/admin/users/{target}", json={"display_name": "pwned"}).status_code == 403, target
        assert a.delete(f"/api/admin/users/{target}").status_code == 403, target
        if h: assert _pw(target) == h
    ids = [w.id_victim, w.id_admb, shared, w.owner_id]
    assert db("SELECT count(*) FROM users WHERE id = ANY(%s) AND NOT disabled AND display_name IS DISTINCT FROM 'pwned'", ids)[0][0] == 4
    assert a.post(f"/api/admin/users/{shared}/2fa/reset").status_code == 403                       # 2FA of a user who also lives in B: not reset by A's admin
    assert a.post(f"/api/admin/users/{w.id_victim}/2fa/reset").status_code in (403, 404)
    # the only thing an admin of A may do to the shared user: remove them from A
    assert a.delete(f"/api/admin/workspaces/{w.A}/members/{shared}").status_code == 200
    assert role_in(shared, w.A) is None and role_in(shared, w.B) == "viewer"

def test_user_manage_own_workspace_still_works(w):
    a = w.cl["adma"]; solo = db("SELECT id FROM users WHERE username=%s", uname("solo"))[0][0]
    h = _pw(solo)
    assert a.patch(f"/api/admin/users/{solo}", json={"display_name": "ok"}).status_code == 200
    assert a.patch(f"/api/admin/users/{solo}", json={"password": PW + "-changed"}).status_code == 200 and _pw(solo) != h
    assert a.patch(f"/api/admin/users/{solo}", json={"disabled": True}).status_code == 200 and db("SELECT disabled FROM users WHERE id=%s", solo)[0][0]
    assert a.delete(f"/api/admin/users/{solo}").status_code == 200 and not db("SELECT 1 FROM users WHERE id=%s", solo)
    assert w.o.patch(f"/api/admin/users/{w.id_victim}", json={"display_name": "by-owner"}).status_code == 200      # the platform owner manages anyone
    assert a.delete(f"/api/admin/users/{w.id_adma}").status_code == 400                                              # not oneself (existing rule kept)

def test_user_routes_refuse_editor_and_anonymous(w):
    e = w.cl["eda"]
    assert e.get("/api/admin/users").status_code == 403
    assert e.patch(f"/api/admin/users/{w.id_eda}", json={"display_name": "x"}).status_code == 403
    assert e.delete(f"/api/admin/users/{w.id_victim}").status_code == 403
    assert httpx.get(f"{B}/api/admin/users").status_code == 401 and httpx.delete(f"{B}/api/admin/users/{w.id_victim}").status_code == 401
    assert db("SELECT 1 FROM users WHERE id=%s", w.id_victim)

def test_add_member_only_if_not_in_a_workspace_you_do_not_administer(w):
    a = w.cl["adma"]
    assert a.post(f"/api/admin/workspaces/{w.A}/members", json={"user_id": w.id_victim, "role": "viewer"}).status_code == 403   # a B-only user: use the invite flow
    assert a.post(f"/api/admin/workspaces/{w.A}/members", json={"user_id": w.id_admb, "role": "viewer"}).status_code == 403
    assert role_in(w.id_victim, w.A) is None and role_in(w.id_admb, w.A) is None
    shared = db("SELECT id FROM users WHERE username=%s", uname("shared"))[0][0]
    assert a.post(f"/api/admin/workspaces/{w.A}/members", json={"user_id": shared, "role": "viewer"}).status_code == 403       # also in B, not (any more) here
    orphan = db("INSERT INTO users(username, pw_hash) VALUES(%s, 'x') RETURNING id", uname("orph"))[0][0]
    assert a.post(f"/api/admin/workspaces/{w.A}/members", json={"user_id": orphan, "role": "viewer"}).status_code == 200       # belongs to no other workspace: fine
    assert a.post(f"/api/admin/workspaces/{w.A}/members", json={"user_id": w.id_eda, "role": "viewer"}).status_code == 200       # role change of a member of A
    assert w.o.post(f"/api/admin/workspaces/{w.A}/members", json={"user_id": w.id_victim, "role": "viewer"}).status_code == 200  # the platform owner may
    assert role_in(w.id_victim, w.A) == "viewer"
    w.o.delete(f"/api/admin/workspaces/{w.A}/members/{w.id_victim}")
    db("UPDATE memberships SET role='editor' WHERE user_id=%s AND workspace_id=%s", w.id_eda, w.A)

# ------------------------------------------------------------------ GĐ12.100c: trace routes of another workspace's project (demo leak audit: thread uuid echoed)
def test_obs_routes_other_workspace_refused(w):
    a, b = w.cl["adma"], w.cl["admb"]
    pid = a.post("/api/obs/projects", json={"name": f"iso-proj-A-{SUF}"}).json()["id"]
    tid, tr, rid = f"th-{SUF}-{uuid.uuid4().hex[:6]}", str(uuid.uuid4()), str(uuid.uuid4())
    db("INSERT INTO tr_runs(id, trace_id, project_id, name, run_type, start_time, thread_id, status) VALUES(%s,%s,%s,'turn','chain',now(),%s,'success')", rid, tr, pid, tid)
    r = a.get(f"/api/obs/projects/{pid}/threads/{tid}/traces"); assert r.status_code == 200 and r.json()["turns"] == 1 and r.json()["traces"][0]["trace_id"] == tr
    r = b.get(f"/api/obs/projects/{pid}/threads/{tid}/traces")                                    # the leak: 200 + the thread uuid echoed
    assert r.status_code == 404 and tid not in r.text and tr not in r.text, r.text
    assert b.get(f"/api/obs/projects/999999/threads/{tid}/traces").status_code == 404
    assert httpx.get(f"{B}/api/obs/projects/{pid}/threads/{tid}/traces").status_code == 401
    q = b.post("/api/obs/runs/query", json={"project_id": pid}); assert q.status_code == 200 and q.json()["runs"] == [] and rid not in q.text and tr not in q.text
    q = b.post("/api/obs/runs/query", json={"project_id": pid, "tree_filter": 'eq(name,"turn")', "trace_filter": 'eq(name,"turn")'}); assert q.json()["runs"] == [] and rid not in q.text
    assert a.post("/api/obs/runs/query", json={"project_id": pid}).json()["runs"][0]["id"] == rid
    r = b.get(f"/api/obs/traces/{tr}/runs"); assert r.status_code == 404 and rid not in r.text
    assert a.get(f"/api/obs/traces/{tr}/runs").json()["runs"][0]["id"] == rid

# ------------------------------------------------------------------ GĐ12.100d: /api/admin/sessions + /api/admin/audit scoped to the administered workspaces
def _sess(tag, uid, ws, live=True):
    tok = f"iso-{tag}-{SUF}-{uuid.uuid4().hex[:6]}"
    db(f"INSERT INTO sessions(token_hash,user_id,workspace_id,expires,ip) VALUES(%s,%s,%s,now() + interval '{'1 day' if live else '-1 hour'}','203.0.113.9')", tok, uid, ws)
    return tok[:10]

def test_sessions_only_members_of_administered_workspaces(w):
    _sess("a", w.id_eda, w.A); _sess("v", w.id_victim, w.B); _sess("b", w.id_admb, w.B)
    _sess("o", w.owner_id, w.A)                         # the platform owner is a member of A (created it) but their session must stay hidden from A's admin
    se = _sess("e", w.id_eda, w.A, live=False)               # expired: listed to nobody
    mixed = _mkuser(w.o, "sessmix", "viewer", w.A); assert w.o.post(f"/api/admin/workspaces/{w.B}/members", json={"user_id": mixed, "role": "viewer"}).status_code == 200
    _sess("m", mixed, w.B)                              # member of A and B, session sits in B: visible (member of A) but the B workspace id is masked
    ra = w.cl["adma"].get("/api/admin/sessions"); assert ra.status_code == 200
    got = {r["id"]: r for r in ra.json()}
    users = {r["user_id"] for r in got.values()}
    assert w.id_eda in users and mixed in users
    assert not ({w.id_victim, w.id_admb, w.owner_id} & users), f"sessions of B / the owner are listed to the admin of A: {users}"
    assert all(r["workspace_id"] in (None, w.A) for r in got.values()), got                       # another workspace's id is masked
    assert not any(r["id"] == se for r in got.values()) and se not in str(got)
    rb = {r["user_id"] for r in w.cl["admb"].get("/api/admin/sessions").json()}
    assert {w.id_victim, w.id_admb, mixed} <= rb and not ({w.id_eda, w.id_adma, w.owner_id} & rb)
    ro = w.o.get("/api/admin/sessions").json(); ou = {r["user_id"] for r in ro}
    assert {w.id_victim, w.id_admb, w.id_eda, w.owner_id} <= ou and se not in {r["id"] for r in ro}      # the owner still sees all live ones, not the expired
    assert w.cl["eda"].get("/api/admin/sessions").status_code == 403 and w.cl["victim"].get("/api/admin/sessions").status_code == 403
    assert httpx.get(f"{B}/api/admin/sessions").status_code == 401

def test_audit_only_administered_workspaces(w):
    for k, ws in (("A", w.A), ("B", w.B)):
        db("INSERT INTO audit_log(user_id,username,workspace_id,auth,method,path,status,ip,ms) VALUES(NULL,%s,%s,'session','GET',%s,200,'203.0.113.9',1)", uname("aud" + k.lower()), ws, f"/iso-{k}-{SUF}/x")
    db("INSERT INTO audit_log(user_id,username,workspace_id,auth,method,path,status,ip,ms) VALUES(NULL,%s,NULL,'login','POST',%s,401,'203.0.113.9',1)", uname("audsys"), f"/iso-SYS-{SUF}/x")
    pa = w.cl["adma"].get(f"/api/admin/audit?path=/iso-&limit=1000"); assert pa.status_code == 200
    paths = {r["path"] for r in pa.json()}
    assert f"/iso-A-{SUF}/x" in paths and f"/iso-B-{SUF}/x" not in paths and f"/iso-SYS-{SUF}/x" not in paths, paths
    assert all(r["workspace_id"] == w.A for r in w.cl["adma"].get("/api/admin/audit?limit=1000").json())   # nothing else, incl. NULL-workspace system rows
    assert not any(r["username"] == uname("audb") for r in w.cl["adma"].get(f"/api/admin/audit?user={uname('audb')}").json())   # the user filter cannot reach B either
    pb = {r["path"] for r in w.cl["admb"].get("/api/admin/audit?path=/iso-&limit=1000").json()}
    assert f"/iso-B-{SUF}/x" in pb and f"/iso-A-{SUF}/x" not in pb and f"/iso-SYS-{SUF}/x" not in pb
    po = {r["path"] for r in w.o.get("/api/admin/audit?path=/iso-&limit=1000").json()}
    assert {f"/iso-A-{SUF}/x", f"/iso-B-{SUF}/x", f"/iso-SYS-{SUF}/x"} <= po                      # the owner sees everything incl. system rows
    assert w.cl["eda"].get("/api/admin/audit").status_code == 403 and w.cl["victim"].get("/api/admin/audit").status_code == 403
    assert httpx.get(f"{B}/api/admin/audit").status_code == 401
    assert w.cl["adma"].get("/api/admin/audit?limit=-5").status_code == 200
