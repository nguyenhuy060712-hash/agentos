"""GĐ6.3 read-only user API keys (user_api_keys.scope full|read) + ingest key stays ingest-only. Local server :8020, fake LLM."""
import httpx, pytest
from test_fixes import B, a  # noqa: F401

def mk(a, **kw):
    r = a.post("/api/auth/keys", json={"name": "t63", **kw}); assert r.status_code == 200, r.text
    return r.json()

@pytest.fixture
def keys(a):
    made = {s: mk(a, scope=s) for s in ("read", "full")}
    yield made
    for k in made.values(): a.delete(f"/api/auth/keys/{k["id"]}")

def cl(key): return httpx.Client(base_url=B, headers={"X-Api-Key": key}, timeout=30)

def test_scope_default_and_listed(a, keys):
    d = mk(a); a.delete(f"/api/auth/keys/{d["id"]}")
    assert d["scope"] == "full"
    ls = {k["id"]: k for k in a.get("/api/auth/keys").json()}
    assert ls[keys["read"]["id"]]["scope"] == "read" and ls[keys["full"]["id"]]["scope"] == "full"
    assert a.post("/api/auth/keys", json={"scope": "bogus"}).status_code == 422

def test_read_key_can_get(keys):
    with cl(keys["read"]["key"]) as c:
        assert c.get("/api/obs/projects").status_code == 200
        assert c.get("/api/auth/keys").status_code == 200

def test_read_key_blocked_on_writes(a, keys):
    pid = a.post("/api/obs/projects", json={"name": "t63-proj"}).json()["id"]
    try:
        with cl(keys["read"]["key"]) as c:
            for m, u, kw in [("POST", "/api/obs/projects", {"json": {"name": "t63-x"}}),
                             ("PATCH", f"/api/obs/projects/{pid}", {"json": {"description": "x"}}),
                             ("DELETE", f"/api/auth/keys/{keys["full"]["id"]}", {})]:
                r = c.request(m, u, **kw); assert r.status_code == 403 and r.json()["detail"] == "Key chỉ đọc", (m, u, r.text)
            assert c.post("/api/auth/keys", json={"scope": "full"}).status_code == 403   # cannot mint a stronger key
        assert a.get("/api/obs/projects").json() and not any(p["name"] == "t63-x" for p in a.get("/api/obs/projects").json())
        assert not [k for k in a.get("/api/auth/keys").json() if k["id"] == keys["full"]["id"] and k["revoked"]]
    finally:
        a.get("/api/obs/projects")
        import psycopg, os
        with psycopg.connect(os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos"), autocommit=True) as pg:
            pg.execute("DELETE FROM projects WHERE name LIKE %s", ("t63-%",))

def test_full_key_unaffected(a, keys):
    with cl(keys["full"]["key"]) as c:
        p = c.post("/api/obs/projects", json={"name": "t63-full"}); assert p.status_code == 200, p.text
        assert c.patch(f"/api/obs/projects/{p.json()["id"]}", json={"description": "ok"}).status_code == 200
        n = c.post("/api/auth/keys", json={"name": "t63-sub"}); assert n.status_code == 200
        assert c.delete(f"/api/auth/keys/{n.json()["id"]}").status_code == 200
    import psycopg, os
    with psycopg.connect(os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos"), autocommit=True) as pg:
        pg.execute("DELETE FROM projects WHERE name LIKE %s", ("t63-%",))

def test_ingest_key_only_ingest(a):
    pid = a.post("/api/obs/projects", json={"name": "t63-ing"}).json()["id"]
    ik = a.post(f"/api/obs/projects/{pid}/keys").json()["api_key"]
    with cl(ik) as c:
        assert c.get("/api/obs/projects").status_code == 401                      # not an aos_ key: no API access
        assert c.post("/api/obs/projects", json={"name": "t63-no"}).status_code == 401
        assert c.post("/ingest/runs", json={"post": [], "patch": []}).status_code == 200
    import psycopg, os
    with psycopg.connect(os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos"), autocommit=True) as pg:
        pg.execute("DELETE FROM projects WHERE name LIKE %s", ("t63-%",))

def test_read_key_allowed_read_only_posts(keys):
    with cl(keys["read"]["key"]) as c:
        for u, body in [("/api/v1/store/items/search", {"namespace_prefix": []}), ("/api/v1/store/namespaces", {}),
                        ("/api/v1/threads/search", {}), ("/api/v1/assistants/search", {})]:
            r = c.post(u, json=body); assert r.status_code != 403 or r.json().get("detail") != "Key chỉ đọc", (u, r.text)
            assert r.status_code == 200, (u, r.status_code, r.text)
        # other POSTs remain blocked
        assert c.post("/api/v1/threads", json={}).status_code == 403
        assert c.put("/api/v1/store/items", json={"namespace": ["t63"], "key": "k", "value": {}}).status_code == 403
