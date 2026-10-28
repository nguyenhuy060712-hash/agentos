"""GĐ6.5 crypto.py (AES-GCM) + kho bí mật workspace (ws_secrets). Local server :8020 (fake LLM, $0)."""
import asyncio, base64, json, os, sys
import httpx, psycopg, pytest
from test_fixes import B, VIEWER, ADMIN, a  # noqa: F401

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
VAL = "s3cr3t-VALUE-t65-9f8e7d"

@pytest.fixture
def sec(a):
    made = []
    def mk(name, value=VAL, hosts=("api.example.com",)):
        r = a.post(f"/api/secrets/{name}", json={"value": value, "allowed_hosts": list(hosts)}); assert r.status_code == 200, r.text
        made.append(name); return r.json()
    yield mk
    for n in made: a.delete(f"/api/secrets/{n}")

# ---------------- crypto
def test_crypto_roundtrip_and_format(monkeypatch, tmp_path):
    import crypto
    monkeypatch.setattr(crypto, "_RING", None); monkeypatch.setattr(crypto, "KEY_FILE", tmp_path / "k")
    monkeypatch.setenv("AOS_SECRET_KEY", base64.b64encode(os.urandom(32)).decode())
    t = crypto.encrypt("xin chào ✓"); assert t.startswith("v1:" + crypto.key_id() + ":")
    assert crypto.decrypt(t) == "xin chào ✓" and crypto.encrypt("xin chào ✓") != t        # random nonce
    assert VAL not in crypto.encrypt(VAL)

def test_crypto_tamper_and_wrong_key(monkeypatch, tmp_path):
    import crypto
    monkeypatch.setattr(crypto, "KEY_FILE", tmp_path / "k")
    k1, k2 = (base64.b64encode(os.urandom(32)).decode() for _ in range(2))
    monkeypatch.setattr(crypto, "_RING", None); monkeypatch.setenv("AOS_SECRET_KEY", k1)
    t = crypto.encrypt("abc"); v, kid, blob = t.split(":")
    raw = bytearray(base64.urlsafe_b64decode(blob)); raw[-1] ^= 1
    with pytest.raises(crypto.CryptoError): crypto.decrypt(f"v1:{kid}:" + base64.urlsafe_b64encode(bytes(raw)).decode())
    with pytest.raises(crypto.CryptoError): crypto.decrypt("v9:" + kid + ":" + blob)
    with pytest.raises(crypto.CryptoError): crypto.decrypt("garbage")
    monkeypatch.setattr(crypto, "_RING", None); monkeypatch.setenv("AOS_SECRET_KEY", k2)       # rotated, old key not kept
    with pytest.raises(crypto.CryptoError): crypto.decrypt(t)
    monkeypatch.setattr(crypto, "_RING", None); monkeypatch.setenv("AOS_SECRET_KEYS_OLD", k1)   # rotation: old key still decrypts
    assert crypto.decrypt(t) == "abc"

def test_crypto_dev_keyfile_mode_600(monkeypatch, tmp_path):
    import crypto
    monkeypatch.setattr(crypto, "_RING", None); monkeypatch.setattr(crypto, "KEY_FILE", tmp_path / "var" / "k")
    monkeypatch.delenv("AOS_SECRET_KEY", raising=False); monkeypatch.delenv("AOS_ENV", raising=False)
    t = crypto.encrypt("x"); assert (tmp_path / "var" / "k").stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(crypto, "_RING", None); assert crypto.decrypt(t) == "x"                  # key reused from file
    monkeypatch.setattr(crypto, "_RING", None); monkeypatch.setattr(crypto, "KEY_FILE", tmp_path / "none"); monkeypatch.setenv("AOS_ENV", "prod")
    with pytest.raises(crypto.CryptoError): crypto.encrypt("x")

# ---------------- API
def test_no_route_returns_value(a, sec):
    bodies = [json.dumps(sec("T65_A")), a.get("/api/secrets").text]
    r = a.put("/api/secrets/T65_A", json={"value": VAL + "2", "allowed_hosts": ["x.example.com"]}); assert r.status_code == 200; bodies.append(r.text)
    bodies.append(a.get("/api/secrets").text)
    assert not any(VAL in b for b in bodies) and not any("value_enc" in b or "v1:" in b for b in bodies)
    row = [s for s in a.get("/api/secrets").json() if s["name"] == "T65_A"][0]
    assert set(row) == {"name", "allowed_hosts", "created_by", "created_at", "updated_at"} and row["allowed_hosts"] == ["x.example.com"]
    with psycopg.connect(PG) as c:                                                              # stored encrypted, not plain
        v = c.execute("SELECT value_enc FROM ws_secrets WHERE name='T65_A'").fetchone()[0]
    assert v.startswith("v2:") and VAL not in v   # GĐ9.7: phong bì v2

def test_crud_validation(a, sec):
    sec("T65_B")
    assert a.post("/api/secrets/T65_B", json={"value": "x"}).status_code == 409
    assert a.post("/api/secrets/bad name", json={"value": "x"}).status_code == 422
    assert a.post("/api/secrets/T65_C", json={"value": "x", "allowed_hosts": ["http://evil"]}).status_code == 422
    assert a.put("/api/secrets/T65_NOPE", json={"value": "x"}).status_code == 404
    assert a.delete("/api/secrets/T65_B").status_code == 200 and a.delete("/api/secrets/T65_B").status_code == 404

def test_viewer_cannot_access(a, sec):
    sec("T65_V")
    with httpx.Client(base_url=B, auth=VIEWER, timeout=30) as v:
        assert v.post("/api/secrets/T65_V2", json={"value": "x"}).status_code == 403
        assert v.put("/api/secrets/T65_V", json={"value": "x"}).status_code == 403
        assert v.delete("/api/secrets/T65_V").status_code == 403
        assert v.get("/api/secrets").status_code == 403
    assert any(s["name"] == "T65_V" for s in a.get("/api/secrets").json())

def test_cross_workspace_isolation(a, sec):
    w = a.post("/api/admin/workspaces", json={"name": "t65-ws"}).json()["id"]
    try:
        sec("T65_ISO")
        with httpx.Client(base_url=B, auth=ADMIN, headers={"X-Workspace-Id": str(w)}, timeout=30) as o:
            assert not [s for s in o.get("/api/secrets").json() if s["name"] == "T65_ISO"]
            assert o.delete("/api/secrets/T65_ISO").status_code == 404
            assert o.post("/api/secrets/T65_ISO", json={"value": "other"}).status_code == 200        # same name allowed per workspace
            assert o.delete("/api/secrets/T65_ISO").status_code == 200
        assert any(s["name"] == "T65_ISO" for s in a.get("/api/secrets").json())
    finally:
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute("DELETE FROM ws_secrets WHERE workspace_id=%s", (w,)); c.execute("DELETE FROM memberships WHERE workspace_id=%s", (w,))
            c.execute("DELETE FROM workspaces WHERE id=%s", (w,))

# ---------------- resolver (in-process, in-memory table)
@pytest.fixture
def resolver(monkeypatch, tmp_path):
    import crypto, ws_secrets
    monkeypatch.setattr(crypto, "_RING", None); monkeypatch.setattr(crypto, "KEY_FILE", tmp_path / "k")
    monkeypatch.setenv("AOS_SECRET_KEY", base64.b64encode(os.urandom(32)).decode())
    rows = {(1, "TG"): (crypto.encrypt(VAL), ["api.telegram.org", "*.hooks.example.com"]), (2, "TG"): (crypto.encrypt("ws2"), ["api.telegram.org"])}
    async def fake_q(sql, args=(), one=False, many=True):
        r = rows.get((args[0], args[1])); return {"value_enc": r[0], "allowed_hosts": r[1]} if r else None
    monkeypatch.setattr(ws_secrets, "_q", fake_q)
    return ws_secrets

def test_host_allowlist(resolver):
    rs = lambda t, u, ws=1: asyncio.run(resolver.resolve_secrets(t, u, ws))
    assert rs("Bearer {{secret:TG}}", "https://api.telegram.org/bot/x") == f"Bearer {VAL}"
    assert rs("{{ secret:TG }}", "https://API.telegram.org:443/x") == VAL
    assert rs("no refs here", "https://x.invalid") == "no refs here"                             # untouched, no lookup
    assert rs("{{secret:TG}}", "https://foo.hooks.example.com/h") == VAL                         # *.suffix
    for bad in ("https://hooks.example.com/", "https://evil.com/", "https://api.telegram.org.evil.com/", "http://api.telegram.org@evil.com/", "not a url", ""):
        with pytest.raises(PermissionError): rs("{{secret:TG}}", bad)
    with pytest.raises(LookupError): rs("{{secret:NOPE}}", "https://api.telegram.org/")
    assert rs("{{secret:TG}}", "https://api.telegram.org/", ws=2) == "ws2"                       # per workspace
    with pytest.raises(LookupError): rs("{{secret:TG}}", "https://api.telegram.org/", ws=3)
    h = asyncio.run(resolver.resolve_headers({"Authorization": "Bearer {{secret:TG}}", "X": 1}, "https://api.telegram.org/", 1))
    assert h == {"Authorization": f"Bearer {VAL}", "X": 1}
