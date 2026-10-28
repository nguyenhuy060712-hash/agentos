"""GĐ9.7 mã hoá dữ liệu khi lưu: phong bì KEK->DEK, AAD, xoay KEK/DEK, Fernet cũ, checkpoint + trace mã hoá (server local :8020, fake LLM, $0).
Unit test dùng workspace giả 9701..9709 (bảng crypto_deks/ws_secrets của DB local, dọn sau mỗi test)."""
import base64, json, os, sys, time, uuid
import httpx, psycopg, pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
PG = os.environ.setdefault("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
os.environ.setdefault("AOS_ENV", "")
from test_fixes import B, ADMIN, VIEWER, a  # noqa: F401
import crypto, aos_crypto, ws_secrets  # noqa: E402

PW = "t97-password-123"
K = lambda: base64.b64encode(os.urandom(32)).decode()  # noqa: E731
def db(sql, args=()):
    with psycopg.connect(PG, autocommit=True) as c:
        cur = c.execute(sql, args or None); return cur.fetchall() if cur.description else None

def keys(mp, spec, active=None):
    mp.delenv("AOS_SECRET_KEY", raising=False); mp.delenv("AOS_SECRET_KEYS_OLD", raising=False); mp.setenv("AOS_SECRET_KEYS", spec)
    if active: mp.setenv("AOS_ACTIVE_KID", active)
    else: mp.delenv("AOS_ACTIVE_KID", raising=False)
    crypto.reset()

@pytest.fixture(autouse=True)
def clean():
    yield
    crypto.reset()
    db("DELETE FROM ws_secrets WHERE workspace_id BETWEEN 9701 AND 9709"); db("DELETE FROM oauth_providers WHERE workspace_id BETWEEN 9701 AND 9709")
    db("DELETE FROM checkpoint_blobs WHERE thread_id LIKE 'gd97%'"); db("DELETE FROM crypto_deks WHERE workspace_id BETWEEN 9701 AND 9709")

def put_secret(ws, name, tok): db("INSERT INTO ws_secrets(workspace_id,name,value_enc) VALUES(%s,%s,%s) ON CONFLICT(workspace_id,name) DO UPDATE SET value_enc=EXCLUDED.value_enc", (ws, name, tok))
def get_tok(ws, name): return db("SELECT value_enc FROM ws_secrets WHERE workspace_id=%s AND name=%s", (ws, name))[0][0]

# ---------------------------------------------------------------- round-trip + định dạng
def test_roundtrip_header_and_algs(monkeypatch):
    keys(monkeypatch, f"k1:{K()}")
    t = crypto.seal("xin chào ✓", 9701, "t.c|1")
    assert t.startswith("v2:") and "xin" not in t and crypto.unseal(t, 9701, "t.c|1") == "xin chào ✓" and crypto.seal("xin chào ✓", 9701, "t.c|1") != t   # nonce ngẫu nhiên
    h = crypto.header(base64.urlsafe_b64decode(t[3:]))
    assert h["alg"] == crypto.ALG_SIV and h["dek_id"] == crypto.active_dek(9701)[0]                                                                       # mặc định GCM-SIV
    monkeypatch.setenv("AOS_CRYPTO_ALG", "gcm")
    t2 = crypto.seal("abc", 9701, "t.c|2"); assert crypto.header(base64.urlsafe_b64decode(t2[3:]))["alg"] == crypto.ALG_GCM
    assert crypto.unseal(t, 9701, "t.c|1") == "xin chào ✓" and crypto.unseal(t2, 9701, "t.c|2") == "abc"                                                 # đọc được cả hai thuật toán
    assert db("SELECT kid, alg, status FROM crypto_deks WHERE workspace_id=9701") == [("k1", 2, "active")]
    assert crypto.unseal(crypto.seal("", 9701, "t.c|3"), 9701, "t.c|3") == ""

def test_prod_refuses_without_kek(monkeypatch, tmp_path):
    for k in ("AOS_SECRET_KEY", "AOS_SECRET_KEYS", "AOS_SECRET_KEYS_OLD"): monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(crypto, "KEY_FILE", tmp_path / "none"); monkeypatch.setenv("AOS_ENV", "prod"); crypto.reset()
    with pytest.raises(crypto.CryptoError): crypto.seal("x", 9701, "t.c|1")
    monkeypatch.setenv("AOS_SECRET_KEYS", f"a:{K()}"); monkeypatch.setenv("AOS_ACTIVE_KID", "zzz"); crypto.reset()
    with pytest.raises(crypto.CryptoError): crypto.key_id()

# ---------------------------------------------------------------- AAD: không chuyển được ciphertext sang dòng/cột/workspace khác
def test_aad_swap_detection(monkeypatch):
    keys(monkeypatch, f"k1:{K()}")
    ta, tb = ws_secrets.seal_value(9701, "A", "value-A"), ws_secrets.seal_value(9701, "B", "value-B")
    assert ws_secrets.open_value(ta, 9701, "A") == "value-A" and ws_secrets.open_value(tb, 9701, "B") == "value-B"
    put_secret(9701, "A", ta); put_secret(9701, "B", ta)                          # kẻ có quyền ghi DB chép bản mã của A sang dòng B
    with pytest.raises(crypto.CryptoError): ws_secrets.open_value(get_tok(9701, "B"), 9701, "B")
    with pytest.raises(crypto.CryptoError): ws_secrets.open_value(ta, 9702, "A")                                 # sang workspace khác
    with pytest.raises(crypto.CryptoError): crypto.unseal(ta, 9701, aos_crypto.ctx("oauth_providers.client_secret_enc", "A"))   # sang cột khác
    raw = bytearray(base64.urlsafe_b64decode(ta[3:])); raw[1] = crypto.ALG_GCM                                    # hạ cấp thuật toán trong header
    with pytest.raises(crypto.CryptoError): crypto.unseal("v2:" + base64.urlsafe_b64encode(bytes(raw)).decode(), 9701, aos_crypto.ctx("ws_secrets.value_enc", "A"))
    raw = bytearray(base64.urlsafe_b64decode(ta[3:])); raw[-1] ^= 1
    with pytest.raises(crypto.CryptoError): crypto.unseal("v2:" + base64.urlsafe_b64encode(bytes(raw)).decode(), 9701, aos_crypto.ctx("ws_secrets.value_enc", "A"))
    ws2 = crypto.seal("w2", 9702, "x.y|1")                                                                       # workspace khác có DEK khác
    assert crypto.header(base64.urlsafe_b64decode(ws2[3:]))["dek_id"] != crypto.header(base64.urlsafe_b64decode(ta[3:]))["dek_id"]

# ---------------------------------------------------------------- xoay KEK: bọc lại DEK, dữ liệu không đổi, không mất
def test_kek_rotation_no_data_loss(monkeypatch):
    k1, k2 = K(), K()
    keys(monkeypatch, f"k1:{k1}")
    toks = {f"s{i}": ws_secrets.seal_value(9703, f"s{i}", f"val-{i}") for i in range(4)}
    for n, t in toks.items(): put_secret(9703, n, t)
    keys(monkeypatch, f"k2:{k2},k1:{k1}", active="k2")                              # đặt KEK mới đang dùng, giữ KEK cũ để đọc
    assert [ws_secrets.open_value(get_tok(9703, n), 9703, n) for n in toks] == [f"val-{i}" for i in range(4)]
    assert aos_crypto.status()["deks_per_kek"].get("k1", 0) >= 1
    r = aos_crypto.rotate_kek([9703]); assert r["rewrapped"] == 1 and r["failed"] == 0 and r["active_kid"] == "k2"
    assert aos_crypto.rotate_kek([9703])["rewrapped"] == 0                           # idempotent
    assert {get_tok(9703, n) for n in toks} == set(toks.values())                    # KEK đổi KHÔNG đụng bản mã dữ liệu
    keys(monkeypatch, f"k2:{k2}")                                                    # bỏ hẳn KEK cũ
    assert [ws_secrets.open_value(get_tok(9703, n), 9703, n) for n in toks] == [f"val-{i}" for i in range(4)]
    assert db("SELECT kid FROM crypto_deks WHERE workspace_id=9703") == [("k2",)]
    keys(monkeypatch, f"k3:{K()}")                                                   # mất KEK -> báo lỗi rõ, không trả rác
    with pytest.raises(crypto.CryptoError): ws_secrets.open_value(get_tok(9703, "s0"), 9703, "s0")

# ---------------------------------------------------------------- xoay DEK: theo lô, bị ngắt rồi chạy tiếp
def _seed_dek_ws(ws=9704, n=5):
    for i in range(n): put_secret(ws, f"s{i}", ws_secrets.seal_value(ws, f"s{i}", f"val-{i}"))
    for i in range(3):
        db("INSERT INTO checkpoint_blobs(thread_id,checkpoint_ns,channel,version,type,blob) VALUES(%s,'',%s,'1',%s,%s)",
           ("gd97-t", f"ch{i}", "msgpack+" + aos_crypto.CP_TYPE, crypto.seal_bytes(f"msg-{i}".encode(), ws, aos_crypto._cp_ctx(ws))))

def _check_all(ws=9704, n=5):
    act = crypto.active_dek(ws)[0]
    for i in range(n):
        t = get_tok(ws, f"s{i}"); assert ws_secrets.open_value(t, ws, f"s{i}") == f"val-{i}" and aos_crypto._tok_dek(t) == act
    for i in range(3):
        b = bytes(db("SELECT blob FROM checkpoint_blobs WHERE thread_id='gd97-t' AND channel=%s", (f"ch{i}",))[0][0])
        assert crypto.unseal_bytes(b, ws, aos_crypto._cp_ctx(ws)) == f"msg-{i}".encode() and crypto.header(b)["dek_id"] == act

def test_dek_rotation_resumes_after_interruption(monkeypatch):
    keys(monkeypatch, f"k1:{K()}"); _seed_dek_ws(); old = crypto.active_dek(9704)[0]
    r = aos_crypto.rotate_dek(9704, batch=2, max_batches=1, settle=0)                # dừng sau 1 lô
    assert r["done"] is False and r["destroyed"] == []
    new = crypto.active_dek(9704)[0]; assert new != old
    real, calls = crypto.seal, {"n": 0}
    def boom(*a, **k):                                                              # sự cố giữa chừng (mất điện / kill)
        calls["n"] += 1
        if calls["n"] == 3: raise RuntimeError("simulated crash")
        return real(*a, **k)
    monkeypatch.setattr(crypto, "seal", boom)
    with pytest.raises(RuntimeError): aos_crypto.rotate_dek(9704, batch=2, settle=0)
    monkeypatch.setattr(crypto, "seal", real)
    r = aos_crypto.rotate_dek(9704, batch=2, settle=0)                              # chạy tiếp: KHÔNG tạo DEK thứ ba
    assert r["done"] and r["destroyed"] == [old] and crypto.active_dek(9704)[0] == new
    assert db("SELECT count(*) FROM crypto_deks WHERE workspace_id=9704")[0][0] == 2
    assert db("SELECT status, wrapped IS NULL FROM crypto_deks WHERE id=%s", (old,)) == [("destroyed", True)]      # DEK cũ bị huỷ thật
    _check_all(); crypto.reset(); _check_all()                                       # sau "khởi động lại" vẫn đọc đủ (DEK mới bọc bằng KEK)
    assert aos_crypto.rotate_dek(9704, settle=0)["destroyed"]                       # lần xoay mới độc lập cũng ổn
    _check_all()

# ---------------------------------------------------------------- dữ liệu cũ: Fernet (OAuth 8.5) + v1 (GĐ6.5) vẫn đọc được rồi migrate
def test_legacy_fernet_and_v1_read_then_migrate(monkeypatch):
    from cryptography.fernet import Fernet
    fk = Fernet.generate_key().decode(); monkeypatch.setenv("AOS_OAUTH_KEY", fk)
    keys(monkeypatch, f"k1:{K()}")
    ftok = Fernet(fk).encrypt(b"client-secret-LEGACY").decode(); v1 = crypto.encrypt("v1-secret")
    pid = db("INSERT INTO oauth_providers(name,authorize_url,token_url,api_base,client_id,client_secret_enc,workspace_id) VALUES('gd97p','http://x/a','http://x/t','http://x/','cid',%s,9705) RETURNING id", (ftok,))[0][0]
    put_secret(9705, "V1", v1)
    pctx = aos_crypto.ctx("oauth_providers.client_secret_enc", pid)
    assert crypto.kind(ftok) == "fernet" and crypto.unseal(ftok, 9705, pctx) == "client-secret-LEGACY" and ws_secrets.open_value(v1, 9705, "V1") == "v1-secret"
    before = aos_crypto.status()["legacy_rows"]
    m = aos_crypto.migrate(9705); assert m["text"]["updated"] == 2 and m["text"]["failed"] == 0 and m["done"]
    nf, nv = db("SELECT client_secret_enc FROM oauth_providers WHERE id=%s", (pid,))[0][0], get_tok(9705, "V1")
    assert crypto.kind(nf) == "v2" and crypto.kind(nv) == "v2" and "LEGACY" not in nf
    assert crypto.unseal(nf, 9705, pctx) == "client-secret-LEGACY" and ws_secrets.open_value(nv, 9705, "V1") == "v1-secret"
    assert aos_crypto.status()["legacy_rows"] == before - 2
    assert aos_crypto.migrate(9705)["text"]["updated"] == 0                           # idempotent
    monkeypatch.delenv("AOS_OAUTH_KEY"); assert crypto.unseal(nf, 9705, pctx) == "client-secret-LEGACY"   # xong migrate không cần khoá Fernet nữa
    wrong = aos_crypto.ctx("oauth_providers.client_secret_enc", pid + 1)
    with pytest.raises(crypto.CryptoError): crypto.unseal(nf, 9705, wrong)

def test_status_counts_per_kid_and_version(monkeypatch):
    keys(monkeypatch, f"k1:{K()}")
    b0 = aos_crypto.status()["columns"]["ws_secrets.value_enc"]
    put_secret(9706, "A", ws_secrets.seal_value(9706, "A", "1")); put_secret(9706, "B", ws_secrets.seal_value(9706, "B", "2")); put_secret(9706, "C", crypto.encrypt("3"))
    st = aos_crypto.status(); c = st["columns"]["ws_secrets.value_enc"]; dek = crypto.active_dek(9706)[0]
    assert c.get(f"v2/dek{dek}") == 2 and c.get("v1/kid:k1", 0) - b0.get("v1/kid:k1", 0) == 1
    assert st["active_kid"] == "k1" and st["deks_per_kek"]["k1"] >= 1 and "users.totp_recovery" in st["columns"]
    assert "wrapped" not in json.dumps(st) and "\"3\"" not in json.dumps(st)                                               # không lộ bản rõ / khoá

# ---------------------------------------------------------------- API + E2E: checkpoint + trace mã hoá, chat vẫn chạy
SUF = uuid.uuid4().hex[:6]

@pytest.fixture(scope="module")
def env(a):
    ws = a.post("/api/admin/workspaces", json={"name": f"gd97-w-{SUF}"}).json()["id"]; name = f"gd97_adm_{SUF}"
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": "admin", "workspace_id": ws}); assert r.status_code == 200, r.text
    vn = f"gd97_view_{SUF}"
    assert a.post("/api/admin/users", json={"username": vn, "password": PW, "role": "viewer", "workspace_id": ws}).status_code == 200
    adm = httpx.Client(base_url=B, auth=(name, PW), timeout=150); view = httpx.Client(base_url=B, auth=(vn, PW), timeout=60)
    ag = adm.post("/api/agents", json={"name": f"gd97-agent-{SUF}", "system_prompt": "Bạn là trợ lý."}); assert ag.status_code == 200, ag.text
    yield {"ws": ws, "adm": adm, "view": view, "agent": ag.json()["id"]}
    adm.put("/api/crypto/settings", json={"checkpoints": False, "traces": False})

def chat(e, tid, text):
    r = e["adm"].post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": f"agent-{e['agent']}", "input": {"messages": [{"type": "human", "content": text}]}}); assert r.status_code == 200, r.text
    return r

def test_status_api_permissions_and_no_secrets(env):
    assert env["view"].get("/api/crypto/status").status_code == 403 and env["view"].put("/api/crypto/settings", json={"traces": True}).status_code == 403
    s = env["adm"].get("/api/crypto/status"); assert s.status_code == 200, s.text
    j = s.json(); assert j["workspace"]["workspace_id"] == env["ws"] and "active_kid" in j and "covered" in j and "unsearchable" in j
    assert "wrapped" not in s.text and "AOS_SECRET" not in s.text
    assert env["adm"].put("/api/crypto/settings", json={}).status_code == 422

def test_oauth_provider_secret_stored_v2(env):
    r = env["adm"].post("/api/oauth/providers", json={"name": "gd97prov", "authorize_url": "http://x/a", "token_url": "http://x/t", "api_base": "http://x/api", "client_id": "cid", "client_secret": "S3CRET-gd97"})
    assert r.status_code == 200, r.text and "S3CRET" not in r.text
    row = db("SELECT client_secret_enc FROM oauth_providers WHERE id=%s", (r.json()["id"],))[0][0]
    assert row.startswith("v2:") and "S3CRET" not in row
    assert crypto.unseal(row, env["ws"], aos_crypto.ctx("oauth_providers.client_secret_enc", r.json()["id"])) == "S3CRET-gd97"
    u = env["adm"].put(f"/api/oauth/providers/{r.json()['id']}", json={"name": "gd97prov", "authorize_url": "http://x/a", "token_url": "http://x/t", "api_base": "http://x/api", "client_id": "cid", "client_secret": "NEW-gd97"})
    assert u.status_code == 200 and crypto.unseal(db("SELECT client_secret_enc FROM oauth_providers WHERE id=%s", (r.json()["id"],))[0][0], env["ws"],
                                                  aos_crypto.ctx("oauth_providers.client_secret_enc", r.json()["id"])) == "NEW-gd97"

def test_secret_api_v2_and_aad(env):
    assert env["adm"].post("/api/secrets/GD97_K", json={"value": "tok-gd97-secret", "allowed_hosts": ["x.example.com"]}).status_code == 200
    tok = db("SELECT value_enc FROM ws_secrets WHERE workspace_id=%s AND name='GD97_K'", (env["ws"],))[0][0]
    assert tok.startswith("v2:") and "tok-gd97" not in tok and ws_secrets.open_value(tok, env["ws"], "GD97_K") == "tok-gd97-secret"
    with pytest.raises(crypto.CryptoError): ws_secrets.open_value(tok, env["ws"], "OTHER")

def _blob_rows(tid):
    out = []
    for tb in ("checkpoint_blobs", "checkpoint_writes"):
        out += [(tb, t, bytes(b) if b is not None else b"") for t, b in db(f"SELECT type, blob FROM {tb} WHERE thread_id LIKE %s", (f"%{tid}%",))]
    return out

def test_checkpoint_and_trace_encryption_end_to_end(env):
    adm = env["adm"]
    tid_plain = adm.post("/api/v1/threads", json={"metadata": {"graph_id": f"agent-{env['agent']}"}}).json()["thread_id"]
    M0 = f"BANGCHUNG-RO-{SUF}"; chat(env, tid_plain, M0)
    rows = _blob_rows(tid_plain); assert rows and any(M0.encode() in b for _, _, b in rows), "đối chứng: chưa bật mã hoá thì bản rõ phải thấy trong DB"
    r = adm.put("/api/crypto/settings", json={"checkpoints": True, "traces": True}); assert r.status_code == 200, r.text
    assert r.json()["workspace"]["checkpoints"] and r.json()["workspace"]["traces"]
    time.sleep(1)
    tid = adm.post("/api/v1/threads", json={"metadata": {"graph_id": f"agent-{env['agent']}"}}).json()["thread_id"]
    M1, M2 = f"BIMAT-CHAT-{SUF}-mot", f"BIMAT-CHAT-{SUF}-hai"
    chat(env, tid, M1)
    rows = _blob_rows(tid); assert rows
    assert not any(M1.encode() in b for _, _, b in rows), "tin nhắn còn là bản rõ trong checkpoint"
    assert any(t.endswith("+" + aos_crypto.CP_TYPE) for _, t, _ in rows) and not any(t in ("msgpack", "json") for _, t, _ in rows if t)
    chat(env, tid, M2)                                                              # lượt 2: phải đọc lại được lịch sử đã mã hoá
    msgs = adm.get(f"/api/v1/threads/{tid}/state").json()["values"]["messages"]
    txt = json.dumps(msgs, ensure_ascii=False); assert M1 in txt and M2 in txt
    assert not any(M2.encode() in b for _, _, b in _blob_rows(tid))
    # thread cũ (bản rõ, trước khi bật) vẫn đọc được; migrate nền đã mã hoá lại nó
    for _ in range(40):
        j = adm.get("/api/crypto/status").json()
        if (j.get("migrate") or {}).get("state") in ("done", "partial", "error"): break
        time.sleep(0.5)
    assert (j["migrate"] or {}).get("state") == "done", j["migrate"]
    assert M0 in json.dumps(adm.get(f"/api/v1/threads/{tid_plain}/state").json()["values"]["messages"], ensure_ascii=False)
    assert not any(M0.encode() in b for _, _, b in _blob_rows(tid_plain)), "migrate phải mã hoá thread cũ"
    assert any(k.startswith("aos2/dek") for k in j["checkpoints"])
    # trace: DB chỉ có phong bì; API trả bản rõ
    pid = None
    for _ in range(40):
        t = db("SELECT r.id, r.inputs::text, r.outputs::text FROM tr_runs r JOIN projects p ON p.id=r.project_id WHERE p.workspace_id=%s AND r.thread_id=%s AND r.inputs IS NOT NULL", (env["ws"], tid))
        if t: break
        time.sleep(0.5)
    assert t, "không thấy trace của thread"
    assert all(M1 not in (x[1] or "") and M2 not in (x[1] or "") for x in t) and any(aos_crypto.ENV in (x[1] or "") for x in t)
    envs = db("SELECT r.id, r.inputs FROM tr_runs r JOIN projects p ON p.id=r.project_id WHERE p.workspace_id=%s AND r.thread_id=%s AND r.inputs ? %s", (env["ws"], tid, aos_crypto.ENV))
    rid, envelope = envs[0]; plain = aos_crypto.unwrap_json(envelope, key="inputs", row_id=rid)
    assert M1 in json.dumps(plain, ensure_ascii=False) or M2 in json.dumps(plain, ensure_ascii=False)
    with pytest.raises(crypto.CryptoError): aos_crypto.unwrap_json(envelope, key="outputs")                   # phong bì nằm sai cột
    with pytest.raises(crypto.CryptoError): aos_crypto.unwrap_json(envelope, key="inputs", row_id=uuid.uuid4())   # dòng khác
    # xoay DEK của workspace này khi đang có checkpoint + trace mã hoá: vẫn đọc được, chat tiếp được
    old = db("SELECT id FROM crypto_deks WHERE workspace_id=%s AND purpose='data' AND status='active'", (env["ws"],))[0][0]
    r = aos_crypto.rotate_dek(env["ws"], settle=0); assert r["done"] and r["destroyed"] == [old] and r["checkpoints"]["failed"] == 0 and r["traces"]["failed"] == 0
    time.sleep(crypto.ACT_TTL + 1)
    M3 = f"BIMAT-CHAT-{SUF}-ba"; chat(env, tid, M3)
    txt = json.dumps(adm.get(f"/api/v1/threads/{tid}/state").json()["values"]["messages"], ensure_ascii=False); assert M1 in txt and M2 in txt and M3 in txt
    assert not any(M3.encode() in b for _, _, b in _blob_rows(tid))
    assert not any(f"dek{old}" in k for k in aos_crypto.status()["checkpoints"])                       # không còn dòng nào trỏ DEK đã huỷ
    adm.put("/api/crypto/settings", json={"checkpoints": False, "traces": False})
    tid3 = adm.post("/api/v1/threads", json={"metadata": {"graph_id": f"agent-{env['agent']}"}}).json()["thread_id"]
    M4 = f"BANGCHUNG-SAU-{SUF}"; time.sleep(11); chat(env, tid3, M4)                                    # tắt cờ -> thread mới lại là bản rõ; thread đã mã hoá vẫn đọc được
    assert any(M4.encode() in b for _, _, b in _blob_rows(tid3))
    assert M1 in json.dumps(adm.get(f"/api/v1/threads/{tid}/state").json()["values"]["messages"], ensure_ascii=False)
