"""GĐ9.P group 2 (24/10/2026): (1) 9.7 leftovers - encryption of lg_runs / lg_run_events, dataset examples and chat attachments (per-workspace DEK, row-bound AAD),
migration + DEK rotation coverage, admin toggles, RLS of crypto_deks / crypto_ws; (2) 9.8 leftovers - no `<PHONE_1>` flash while streaming, masked embeddings,
masked stored experiment results; (3) Zalo OA image / file consultation messages + webhook freshness window.
Local server :8020 (fake LLM => $0) + Postgres; Zalo against a local fake OA on 127.0.0.1:8782 (run this file ALONE: test_gd8_zalo.py uses the same port).
Run with a RAM limit: ulimit -v 4000000."""
import asyncio, base64, io, json, os, sys, threading, time, uuid
import httpx, psycopg, pytest
from aiohttp import web
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
PG = os.environ.setdefault("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
os.environ.setdefault("AOS_ENV", "")
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")
from test_fixes import B, ADMIN, a, sse  # noqa: F401,E402
import crypto, aos_crypto  # noqa: E402

PW = "t9p2-password-123"; SUF = uuid.uuid4().hex[:6]
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
PHONE = "0912 345 678"

def db(sql, args=()):
    with psycopg.connect(PG, autocommit=True) as c:
        cur = c.execute(sql, args or None); return cur.fetchall() if cur.description else None

def mkws(a, tag):
    ws = a.post("/api/admin/workspaces", json={"name": f"g9p2-{tag}-{SUF}"}).json()["id"]; name = f"g9p2_{tag}_{SUF}"
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": "admin", "workspace_id": ws}); assert r.status_code == 200, r.text
    adm = httpx.Client(base_url=B, auth=(name, PW), timeout=150)
    ag = adm.post("/api/agents", json={"name": f"g9p2-agent-{tag}-{SUF}", "system_prompt": "Bạn là trợ lý."}); assert ag.status_code == 200, ag.text
    return {"ws": ws, "adm": adm, "agent": ag.json()["id"], "name": name}

def drop_ws(e):
    e["adm"].close(); ws = e["ws"]
    for sql in ("DELETE FROM crypto_ws WHERE workspace_id=%s", "DELETE FROM pii_settings WHERE workspace_id=%s", "DELETE FROM lg_runs WHERE workspace_id=%s",
                "DELETE FROM lg_threads WHERE workspace_id=%s", "DELETE FROM datasets WHERE workspace_id=%s", "DELETE FROM chat_files WHERE workspace_id=%s",
                "DELETE FROM channel_bindings WHERE workspace_id=%s", "DELETE FROM agents WHERE workspace_id=%s", "DELETE FROM ws_secrets WHERE workspace_id=%s"):
        try: db(sql, (ws,))
        except Exception: pass  # noqa: BLE001
    db("DELETE FROM crypto_deks WHERE workspace_id=%s", (ws,)); db("DELETE FROM users WHERE username=%s", (e["name"],))

def chat(e, tid, text, timeout=150):
    r = e["adm"].post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": f"agent-{e['agent']}", "input": {"messages": [{"type": "human", "content": text}]}}, timeout=timeout)
    assert r.status_code == 200, r.text; return r

def new_thread(e): return e["adm"].post("/api/v1/threads", json={"metadata": {"graph_id": f"agent-{e['agent']}"}}).json()["thread_id"]

# ============================================================================== 1. encryption: units
@pytest.fixture
def flags_all(monkeypatch):
    monkeypatch.setitem(aos_crypto._FL, "d", {9721: {g: True for g in aos_crypto.GROUPS}}); monkeypatch.setitem(aos_crypto._FL, "t", time.monotonic() + 600)
    yield
    db("DELETE FROM crypto_deks WHERE workspace_id BETWEEN 9721 AND 9739")

def test_envelope_all_tables_aad_row_bound(flags_all):
    rid = str(uuid.uuid4()); other = str(uuid.uuid4())
    out = {"reply": "bí mật", "cost_usd": 0.0123}
    env = aos_crypto.wrap_json("output", rid, out, 9721, "lg_runs")
    assert env["t"] == "lg_runs" and env["cost_usd"] == 0.0123 and "bí mật" not in json.dumps(env, ensure_ascii=False)     # cost stays readable for SQL spend caps
    assert aos_crypto.unwrap_json(env, key="output", row_id=rid) == out
    with pytest.raises(crypto.CryptoError): aos_crypto.unwrap_json(env, key="input")                                     # wrong column
    with pytest.raises(crypto.CryptoError): aos_crypto.unwrap_json(env, row_id=other)                                    # copied to another row
    with pytest.raises(crypto.CryptoError): aos_crypto.unwrap_json({**env, "t": "lg_run_events"})                         # table label forged -> AAD mismatch
    with pytest.raises(crypto.CryptoError): aos_crypto.unwrap_json({**env, "r": other})
    with pytest.raises(crypto.CryptoError): aos_crypto.unwrap_json(env, ws=9722)                                         # other workspace
    ev = aos_crypto.wrap_json("data", f"{rid}:3", {"messages": ["x"]}, 9721, "lg_run_events")
    rows = aos_crypto.decrypt_rows([{"run_id": rid, "seq": 3, "data": ev}, {"run_id": rid, "seq": 4, "data": ev}, {"id": 5, "output": env, "run_id": rid}])
    assert rows[0]["data"] == {"messages": ["x"]} and "error" in rows[1]["data"] and rows[2]["output"] == out            # row 4 holds seq 3's envelope -> refused
    rev = aos_crypto.wrap_json("inputs", "7:2", {"q": "hỏi"}, 9721, "example_revisions")
    assert aos_crypto.decrypt_rows([{"example_id": 7, "version": 2, "inputs": rev}])[0]["inputs"] == {"q": "hỏi"}
    assert "error" in aos_crypto.decrypt_rows([{"example_id": 7, "version": 3, "inputs": rev}])[0]["inputs"]
    with pytest.raises(crypto.CryptoError): aos_crypto.unwrap_json({**rev, "t": "sneaky_table"})
    t = aos_crypto.seal_text("attachments", "chat_files.md", rid, "văn bản", 9721)
    assert t.startswith(aos_crypto.TXT) and "văn bản" not in t and aos_crypto.open_text("chat_files.md", t, rid) == "văn bản"
    with pytest.raises(crypto.CryptoError): aos_crypto.open_text("chat_files.md", t, other)
    assert aos_crypto.decrypt_rows([{"id": rid, "md": t}])[0]["md"] == "văn bản" and aos_crypto.decrypt_rows([{"id": other, "md": t}])[0]["md"] == "[không giải mã được]"

def test_rls_crypto_deks_and_ws_other_workspace_cannot_read(flags_all):
    crypto.active_dek(9731); crypto.active_dek(9732)
    for w in (9731, 9732): db("INSERT INTO crypto_ws(workspace_id,checkpoints) VALUES(%s,true) ON CONFLICT DO NOTHING", (w,))
    try:
        with psycopg.connect(PG, autocommit=True) as c:
            c.execute("SET ROLE aos_rls")
            c.execute("SELECT set_config('app.ws','9732',false), set_config('app.uid','0',false), set_config('app.role','admin',false)")   # workspace B (even its admin)
            assert {r[0] for r in c.execute("SELECT DISTINCT workspace_id FROM crypto_deks")} == {9732}
            assert c.execute("SELECT count(*) FROM crypto_deks WHERE workspace_id=9731").fetchone()[0] == 0
            assert c.execute("SELECT count(*) FROM crypto_deks WHERE workspace_id=9732").fetchone()[0] >= 1
            assert {r[0] for r in c.execute("SELECT workspace_id FROM crypto_ws WHERE workspace_id IN (9731,9732)")} == {9732}
            assert c.execute("UPDATE crypto_deks SET status='destroyed', wrapped=NULL WHERE workspace_id=9731").rowcount == 0
            assert c.execute("DELETE FROM crypto_deks WHERE workspace_id=9731").rowcount == 0
            assert c.execute("UPDATE crypto_ws SET traces=true WHERE workspace_id=9731").rowcount == 0
            assert c.execute("DELETE FROM crypto_ws WHERE workspace_id=9731").rowcount == 0
            with pytest.raises(psycopg.errors.InsufficientPrivilege):          # cannot plant a DEK / flag row into workspace A
                c.execute("INSERT INTO crypto_deks(workspace_id,kid,alg,wrapped) VALUES(9731,'x',2,'\\x00')")
            c.execute("RESET ROLE")
        assert db("SELECT status FROM crypto_deks WHERE workspace_id=9731")[0][0] == "active"   # untouched
    finally:
        db("DELETE FROM crypto_ws WHERE workspace_id IN (9731,9732)")

# ============================================================================== 1b. encryption: end to end on the server
@pytest.fixture(scope="module")
def cw(a):
    e = mkws(a, "crypto"); yield e
    e["adm"].put("/api/crypto/settings", json={g: False for g in aos_crypto.GROUPS}); drop_ws(e)

def _wait_migrate(adm):
    for _ in range(80):
        j = adm.get("/api/crypto/status").json()
        if (j.get("migrate") or {}).get("state") in ("done", "partial", "error"): return j
        time.sleep(0.5)
    raise AssertionError("migrate did not finish")

def _upload(adm, name, data, mime):
    r = adm.post("/api/files", files={"file": (name, data, mime)}); assert r.status_code == 200, r.text; return r.json()

ST: dict = {}

def test_toggles_api_all_five_groups(cw):
    adm = cw["adm"]
    assert adm.put("/api/crypto/settings", json={}).status_code == 422
    j = adm.get("/api/crypto/status").json(); assert set(aos_crypto.GROUPS) <= set(j["workspace"]) and "payloads" in j
    assert any("Tệp đính kèm" in x for x in j["covered"]) and any("Lượt chạy" in x for x in j["covered"])
    # BEFORE any encryption: plain data that the migration must pick up later
    tid = new_thread(cw); M0 = f"CU-RUN-{SUF}"; chat(cw, tid, M0); ST["tid0"], ST["M0"] = tid, M0
    ds = adm.post("/api/datasets", json={"name": f"g9p2-ds-{SUF}"}).json()["id"]; ST["ds"] = ds
    P0 = f"CU-VIDU-{SUF}"; ex = adm.post(f"/api/datasets/{ds}/examples", json={"inputs": {"question": P0}, "outputs": {"answer": "đáp án cũ"}}); assert ex.status_code == 200, ex.text
    ST["P0"], ST["ex0"] = P0, ex.json()["ids"][0]
    F0 = f"CU-TEP-{SUF}"; f = _upload(adm, "cu.txt", f"{F0} nội dung tệp cũ".encode(), "text/plain"); ST["F0"], ST["f0"] = F0, f["id"]
    assert db("SELECT enc FROM chat_files WHERE id=%s", (f["id"],))[0][0] is False
    XA0 = f"CU-DINHKEM-{SUF}".encode(); r = adm.post(f"/api/examples/{ST['ex0']}/attachments", files={"file": ("cu.txt", XA0, "text/plain")}); assert r.status_code == 200, r.text
    ST["xa0"], ST["XA0"] = r.json()["id"], XA0; assert db("SELECT enc, data FROM example_attachments WHERE id=%s", (ST["xa0"],))[0][0] is False
    assert M0 in db("SELECT input::text FROM lg_runs WHERE thread_id=%s", (tid,))[0][0]
    for g in aos_crypto.GROUPS:     # each group toggles on its own, with the others untouched
        r = adm.put("/api/crypto/settings", json={g: True}); assert r.status_code == 200, r.text
        assert r.json()["workspace"][g] is True and sum(r.json()["workspace"][x] is True for x in aos_crypto.GROUPS) == aos_crypto.GROUPS.index(g) + 1
        _wait_migrate(adm); time.sleep(0.3)
    time.sleep(1)

def test_migration_encrypted_old_rows_and_api_still_plain(cw):
    adm = cw["adm"]; tid, M0 = ST["tid0"], ST["M0"]
    for _ in range(30):
        row = db("SELECT input::text, output::text, config::text FROM lg_runs WHERE thread_id=%s", (tid,))[0]
        if aos_crypto.ENV in row[0]: break
        time.sleep(0.5)
    assert all(aos_crypto.ENV in x for x in row if x and x != "null") and M0 not in " ".join(row), "run payloads must be envelopes after the migration"
    ev = db("SELECT e.data::text FROM lg_run_events e JOIN lg_runs r ON r.run_id=e.run_id WHERE r.thread_id=%s", (tid,))
    assert ev and all(aos_crypto.ENV in x[0] and M0 not in x[0] for x in ev if x[0] != "null")
    rev = db("SELECT inputs::text, outputs::text FROM example_revisions WHERE example_id=%s", (ST["ex0"],))[0]
    assert ST["P0"] not in rev[0] and aos_crypto.ENV in rev[0] and "đáp án cũ" not in rev[1]
    xa = db("SELECT enc, data FROM example_attachments WHERE id=%s", (ST["xa0"],))[0]
    assert xa[0] is True and ST["XA0"] not in bytes(xa[1]), "old example attachment must be encrypted by the migration"
    assert adm.get(f"/api/example_attachments/{ST['xa0']}").content == ST["XA0"]
    fr = db("SELECT enc, md, sha256 FROM chat_files WHERE id=%s", (ST["f0"],))[0]
    assert fr[0] is True and fr[1].startswith(aos_crypto.TXT) and ST["F0"] not in fr[1]
    raw = aos_crypto.enc_path(ST["f0"]).read_bytes(); assert ST["F0"].encode() not in raw and raw[0] == 2
    assert not (aos_crypto.files_dir() / fr[2][:2] / fr[2]).exists(), "plain copy must be removed after migration"
    # transparent reads
    runs = adm.get(f"/api/v1/threads/{tid}/runs").json(); assert M0 in json.dumps(runs, ensure_ascii=False)
    assert M0 in json.dumps(adm.get(f"/api/v1/threads/{tid}/state").json(), ensure_ascii=False)
    d = adm.get(f"/api/datasets/{ST['ds']}").json(); assert ST["P0"] in json.dumps(d["examples"], ensure_ascii=False) and "đáp án cũ" in json.dumps(d["examples"], ensure_ascii=False)
    g = adm.get(f"/api/files/{ST['f0']}"); assert g.status_code == 200 and ST["F0"].encode() in g.content
    assert ST["F0"] in adm.get(f"/api/files/{ST['f0']}/text").json()["md"]

def test_new_data_encrypted_on_write_and_usable(cw):
    adm = cw["adm"]; tid = new_thread(cw); M1 = f"MOI-RUN-{SUF}"
    evs = sse(adm, tid, {"assistant_id": f"agent-{cw['agent']}", "input": {"messages": [{"type": "human", "content": M1}]}, "stream_mode": ["messages-tuple", "values"]})
    assert M1 in json.dumps(evs, ensure_ascii=False), "streaming reads lg_run_events: must come back decrypted"
    row = db("SELECT input::text, output::text, config::text, (output->>'cost_usd') FROM lg_runs WHERE thread_id=%s", (tid,))[0]
    assert aos_crypto.ENV in row[0] and aos_crypto.ENV in row[1] and aos_crypto.ENV in row[2] and M1 not in " ".join(row[:3])
    assert db("SELECT coalesce(sum(nullif(output->>'cost_usd','')::numeric),0) FROM lg_runs WHERE thread_id=%s", (tid,))     # the spend-cap SQL still works on envelopes
    ev = db("SELECT e.data::text FROM lg_run_events e JOIN lg_runs r ON r.run_id=e.run_id WHERE r.thread_id=%s", (tid,))
    assert ev and not any(M1 in x[0] for x in ev) and sum(aos_crypto.ENV in x[0] for x in ev) >= 2
    # a dataset example (create + revise)
    P1 = f"MOI-VIDU-{SUF}"; eid = adm.post(f"/api/datasets/{ST['ds']}/examples", json={"inputs": {"question": P1}, "outputs": {"answer": "đáp án mới"}}).json()["ids"][0]
    assert adm.put(f"/api/examples/{eid}", json={"inputs": {"question": P1 + "-sửa"}, "outputs": {"answer": "đáp án sửa"}}).status_code == 200
    rv = db("SELECT inputs::text FROM example_revisions WHERE example_id=%s ORDER BY version", (eid,)); assert len(rv) == 2 and all(aos_crypto.ENV in r[0] and P1 not in r[0] for r in rv)
    assert P1 + "-sửa" in json.dumps(adm.get(f"/api/datasets/{ST['ds']}").json()["examples"], ensure_ascii=False); ST["P1"] = P1
    XA1 = f"MOI-DINHKEM-{SUF}".encode(); xr = adm.post(f"/api/examples/{eid}/attachments", files={"file": ("moi.txt", XA1, "text/plain")}); assert xr.status_code == 200, xr.text
    xa = db("SELECT enc, data FROM example_attachments WHERE id=%s", (xr.json()["id"],))[0]; assert xa[0] is True and XA1 not in bytes(xa[1])
    assert adm.get(f"/api/example_attachments/{xr.json()['id']}").content == XA1
    ST["xa1"], ST["XA1"] = xr.json()["id"], XA1
    # a document + an image attachment: bytes and extracted text encrypted, chat can still use them
    F1 = f"MOI-TEP-{SUF}"; f = _upload(adm, "moi.txt", f"{F1} nội dung tệp mới".encode(), "text/plain"); ST["f1"], ST["F1"] = f["id"], F1
    fr = db("SELECT enc, md, sha256 FROM chat_files WHERE id=%s", (f["id"],))[0]
    assert fr[0] is True and fr[1].startswith(aos_crypto.TXT) and F1.encode() not in aos_crypto.enc_path(f["id"]).read_bytes() and not (aos_crypto.files_dir() / fr[2][:2] / fr[2]).exists()
    im = _upload(adm, "a.png", PNG, "image/png"); ST["img"] = im["id"]
    assert db("SELECT enc FROM chat_files WHERE id=%s", (im["id"],))[0][0] is True and aos_crypto.enc_path(im["id"]).read_bytes() != PNG
    assert adm.get(f"/api/files/{im['id']}").content == PNG and adm.get(f"/api/files/{im['id']}").headers["content-type"] == "image/png"
    tid2 = new_thread(cw)
    r = adm.post(f"/api/v1/threads/{tid2}/runs/wait", json={"assistant_id": f"agent-{cw['agent']}", "input": {"messages": [{"type": "human", "content": [
        {"type": "text", "text": "tóm tắt"}, {"type": "file", "url": f"/api/files/{f['id']}"}, {"type": "image", "url": f"/api/files/{im['id']}"}]}]}}); assert r.status_code == 200, r.text
    ans = json.dumps(adm.get(f"/api/v1/threads/{tid2}/state").json()["values"]["messages"][-1], ensure_ascii=False)
    assert F1 in ans and f"I received 1 image(s): image/png {len(PNG)} B" in ans            # the model got the decrypted text + bytes

def test_other_workspace_cannot_read_encrypted_data(a, cw):
    o = mkws(a, "other")
    try:
        assert o["adm"].get(f"/api/files/{ST['f1']}").status_code == 404
        assert o["adm"].get(f"/api/datasets/{ST['ds']}").status_code == 404
        assert o["adm"].get(f"/api/v1/threads/{ST['tid0']}/runs").status_code in (200, 404) and ST["M0"] not in o["adm"].get(f"/api/v1/threads/{ST['tid0']}/runs").text
    finally: drop_ws(o)

def test_privacy_export_returns_decrypted_files_and_erase_removes_encrypted_copy(a, cw):
    import io, zipfile
    adm = cw["adm"]; r = adm.get("/api/privacy/me/export"); assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert any(n.startswith("tep/") and ST["F1"].encode() in z.read(n) for n in z.namelist()), "exported file must be the decrypted original"
    un = f"g9p2_erase_{SUF}"; assert a.post("/api/admin/users", json={"username": un, "password": PW, "role": "editor", "workspace_id": cw["ws"]}).status_code == 200
    try:
        with httpx.Client(base_url=B, auth=(un, PW), timeout=60, headers={"X-Workspace-Id": str(cw["ws"])}) as u:
            f = _upload(u, "xoa_toi.txt", b"se bi xoa " + SUF.encode(), "text/plain"); p = aos_crypto.enc_path(f["id"]); assert p.exists()
            r = u.post("/api/privacy/me/erase", json={"confirm": un}); assert r.status_code == 200, r.text
            assert not p.exists() and not db("SELECT 1 FROM chat_files WHERE id=%s", (f["id"],)), "erasing a person must delete the encrypted file too"
    finally: db("DELETE FROM users WHERE username LIKE %s", (f"%g9p2_erase_{SUF}%",))

def test_dek_rotation_covers_runs_datasets_attachments(cw):
    ws = cw["ws"]; adm = cw["adm"]
    old = db("SELECT id FROM crypto_deks WHERE workspace_id=%s AND purpose='data' AND status='active'", (ws,))[0][0]
    st = aos_crypto.status(); pay = st["payloads"]
    assert any(f"v2/dek{old}" in v for k, v in pay.items() if k in ("lg_runs", "lg_run_events", "example_revisions")), pay
    r = aos_crypto.rotate_dek(ws, settle=0)
    assert r["done"] and r["destroyed"] == [old] and all(r[k]["failed"] == 0 for k in ("text", "checkpoints", "traces", "runs", "datasets", "attachments")), r
    assert r["runs"]["updated"] >= 1 and r["datasets"]["updated"] >= 1 and r["attachments"]["updated"] >= 1
    pay = aos_crypto.status()["payloads"]; assert not any(f"dek{old}" in k for v in pay.values() for k in v), "no row may point at the destroyed DEK"
    assert not any(f"dek{old}" in k for k in aos_crypto.status()["checkpoints"])
    time.sleep(crypto.ACT_TTL + 1)
    assert ST["M0"] in adm.get(f"/api/v1/threads/{ST['tid0']}/runs").text                                  # everything still readable with the new DEK
    assert ST["P1"] in json.dumps(adm.get(f"/api/datasets/{ST['ds']}").json()["examples"], ensure_ascii=False)
    assert ST["F1"].encode() in adm.get(f"/api/files/{ST['f1']}").content and ST["F0"] in adm.get(f"/api/files/{ST['f0']}/text").json()["md"]
    assert adm.get(f"/api/files/{ST['img']}").content == PNG
    assert adm.get(f"/api/example_attachments/{ST['xa0']}").content == ST["XA0"] and adm.get(f"/api/example_attachments/{ST['xa1']}").content == ST["XA1"]
    assert r["datasets"]["updated"] >= 3
    new = db("SELECT id FROM crypto_deks WHERE workspace_id=%s AND purpose='data' AND status='active'", (ws,))[0][0]; assert new != old
    assert aos_crypto.rotate_dek(ws, settle=0)["destroyed"] == [new]                                          # a second rotation works too
    time.sleep(crypto.ACT_TTL + 1); assert ST["F1"].encode() in adm.get(f"/api/files/{ST['f1']}").content

def test_turning_off_keeps_old_readable_new_is_plain(cw):
    adm = cw["adm"]
    r = adm.put("/api/crypto/settings", json={g: False for g in aos_crypto.GROUPS}); assert r.status_code == 200 and not any(r.json()["workspace"][g] for g in aos_crypto.GROUPS)
    time.sleep(aos_crypto.FL_TTL + 1)
    tid = new_thread(cw); M2 = f"SAU-TAT-{SUF}"; chat(cw, tid, M2)
    assert M2 in db("SELECT input::text FROM lg_runs WHERE thread_id=%s", (tid,))[0][0]                       # new data: plain again
    assert ST["M0"] in adm.get(f"/api/v1/threads/{ST['tid0']}/runs").text                                      # old envelopes: still decrypted (flags off must not hide them)
    assert ST["F1"].encode() in adm.get(f"/api/files/{ST['f1']}").content and ST["P1"] in json.dumps(adm.get(f"/api/datasets/{ST['ds']}").json()["examples"], ensure_ascii=False)
    f = _upload(adm, "plain.txt", b"ban ro", "text/plain"); assert db("SELECT enc FROM chat_files WHERE id=%s", (f["id"],))[0][0] is False

def test_admin_card_has_real_toggles(a):
    html = a.get("/admin", headers={"Cookie": "aos_lang=vi"}).text
    for g in aos_crypto.GROUPS: assert f"'{g}'" in html
    card = html[html.index('id="cry_card"'):]; card = card[:card.index("</div></div>") + 12]
    assert "cryToggle" in html and "confirm(" in html and "Bật/tắt từng nhóm dữ liệu" in card and "Thẻ này chỉ để xem" not in card
    en = a.get("/admin").text; assert "Switch each data group on or off below" in en and "This card is read-only" not in en[en.index('id="cry_card"'):en.index('id="cry_card"') + 900]

# ============================================================================== 2. PII: streaming restore, masked embeddings, masked experiment results
def test_stream_restorer_tokens_split_across_chunks():
    import pii
    pz = pii.Pseudonymizer(); pz.token("VN_PHONE", PHONE); pz.token("VN_CCCD", "079203012345")
    def run(chunks):
        r = pii.StreamRestorer(pz); out = [r.feed(c) for c in chunks]; out.append(r.flush()); return out
    for chunks in (["Gọi <", "PHO", "NE_", "1", "> ngay, CCCD <CCCD_1>."], list("Gọi <PHONE_1> ngay, CCCD <CCCD_1>."), ["Gọi <PHONE_1> ngay, CC", "CD <CCCD_1", ">."], ["Gọi PHONE_1 ngay, CCCD <CCCD_1>."]):
        out = run(chunks); joined = "".join(out)
        assert joined == f"Gọi {PHONE} ngay, CCCD 079203012345." or (chunks[0].startswith("Gọi PHONE_1") and PHONE in joined), (chunks, joined)
        assert not any("<" in o and "PHONE" in o or "PHONE_" in o or "<CCCD" in o for o in out), f"a token flashed: {out}"
    out = run(["a < b ", "và 5 > 3, GIÁ 100"]); assert "".join(out) == "a < b và 5 > 3, GIÁ 100"                        # plain text is never swallowed
    assert run(["xin chào <PH"])[-1] == "<PH"                                                                          # unfinished token is released at the end, as is

def _stream_model():
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessageChunk
    from langchain_core.outputs import ChatGenerationChunk, ChatResult, ChatGeneration
    from langchain_core.messages import AIMessage
    class SplitStream(BaseChatModel):
        model_name: str = "split-stream"
        @property
        def _llm_type(self): return "split-stream"
        def _reply(self, messages): return "Đã nhận: " + str(messages[-1].content)
        def _generate(self, messages, stop=None, run_manager=None, **kw): return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self._reply(messages)))])
        def _stream(self, messages, stop=None, run_manager=None, **kw):
            t = self._reply(messages)
            for i in range(0, len(t), 3): yield ChatGenerationChunk(message=AIMessageChunk(content=t[i:i + 3]))
            yield ChatGenerationChunk(message=AIMessageChunk(content="", chunk_position="last"))
        async def _astream(self, messages, stop=None, run_manager=None, **kw):
            for c in self._stream(messages): yield c
    return SplitStream()

def test_gateway_streaming_never_shows_tokens_async_and_sync(monkeypatch):
    from langchain_core.messages import AIMessage, HumanMessage
    from test_gd9_pii import _gw, _Req
    gw = _gw(monkeypatch, {"enabled": True, "mode": "pseudonym", "apply_gateway": True})
    monkeypatch.setattr(gw, "_model", lambda name, temperature=None: _stream_model())
    seen = {"a": [], "s": []}
    class Resp: pass
    async def handler(r):
        seen["a"] = [c.content async for c in r.model.astream(r.messages)]
        seen["s"] = await asyncio.to_thread(lambda: [c.content for c in r.model.stream(r.messages)])   # to_thread copies the context, like langchain's own executor
        x = Resp(); x.result = [AIMessage(content="".join(seen["a"]))]; return x
    mw = gw.GatewayMiddleware(None, "gemini-3.6-flash", [], 0.2)
    resp = asyncio.run(mw.awrap_model_call(_Req([HumanMessage(content=f"Khách SĐT {PHONE} cần hỗ trợ")]), handler))
    for k in ("a", "s"):
        assert seen[k], k
        assert not any("<" in c or "PHONE" in c for c in seen[k]), f"token flashed in a streamed chunk ({k}): {seen[k]}"
        assert "".join(seen[k]) == f"Đã nhận: Khách SĐT {PHONE} cần hỗ trợ", (k, seen[k])
    assert resp.result[0].content == f"Đã nhận: Khách SĐT {PHONE} cần hỗ trợ"
    assert gw._PZ_CV.get() is None                                                                                  # context var cleaned up
    # mask mode / pseudonym off: the model class is not touched
    gw2 = _gw(monkeypatch, {"enabled": False}); m = _stream_model(); assert gw2.restoring_model(m) is not m and type(gw2.restoring_model(gw2.restoring_model(m))).__name__.startswith("Restoring")

@pytest.fixture(scope="module")
def pw(a):
    e = mkws(a, "pii"); yield e
    e["adm"].put("/api/privacy/settings", json={"enabled": False}); drop_ws(e)

def test_server_stream_no_token_flash_real_values_final(pw):
    adm = pw["adm"]
    r = adm.put("/api/privacy/settings", json={"enabled": True, "mode": "pseudonym", "entities": [], "threshold": 0.5, "apply_traces": True, "apply_gateway": True, "ner": False}); assert r.status_code == 200, r.text
    tid = new_thread(pw)
    evs = sse(adm, tid, {"assistant_id": f"agent-{pw['agent']}", "input": {"messages": [{"type": "human", "content": f"Khách SĐT {PHONE} cần hỗ trợ"}]}, "stream_mode": ["messages-tuple"]})
    msgs = [json.dumps(d, ensure_ascii=False) for e, d in evs if e and e.startswith("messages")]
    assert msgs, [e for e, _ in evs]
    assert not any("<PHONE" in m or "PHONE_1" in m for m in msgs), "a pseudonym token flashed in the stream"
    assert PHONE in "".join(msgs) or "0912" in "".join(msgs)
    final = adm.get(f"/api/v1/threads/{tid}/state").json()["values"]["messages"][-1]["content"]; assert PHONE in final and "<PHONE" not in final
    db("DELETE FROM lg_threads WHERE thread_id=%s", (tid,))

def test_uploaded_file_text_masked_before_the_model_and_restored_in_pseudonym_mode(pw):
    adm = pw["adm"]; f = _upload(adm, "khach.txt", f"Hồ sơ khách: SĐT {PHONE}, ghi chú mua hàng.".encode(), "text/plain")
    def ask(mode):
        r = adm.put("/api/privacy/settings", json={"enabled": True, "mode": mode, "entities": [], "threshold": 0.5, "apply_traces": True, "apply_gateway": True, "ner": False}); assert r.status_code == 200, r.text
        tid = new_thread(pw)
        r = adm.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": f"agent-{pw['agent']}", "input": {"messages": [{"type": "human", "content": [
            {"type": "text", "text": "đọc giúp"}, {"type": "file", "url": f"/api/files/{f['id']}"}]}]}}); assert r.status_code == 200, r.text
        return adm.get(f"/api/v1/threads/{tid}/state").json()["values"]["messages"][-1]["content"]
    masked = ask("mask"); assert "[PHONE]" in masked and "0912" not in masked and "345 678" not in masked, masked           # the model only saw the masked file text
    real = ask("pseudonym"); assert PHONE in real and "<PHONE" not in real, real                                           # reversible mode: restored for the user
    assert PHONE in adm.get(f"/api/files/{f['id']}/text").json()["md"]                                                        # the stored document itself is untouched

# ---- OCR: personal data inside images (local RapidOCR)
def _doc_png(lines=("PHIẾU GIAO HÀNG", "Khách: Trần Văn Bình", "SĐT: 0987 654 321", "CCCD: 001085012345", "Đơn hàng giá 1.500.000đ")):
    from PIL import Image, ImageDraw, ImageFont
    try: f = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 34)
    except OSError: f = ImageFont.load_default(34)
    im = Image.new("RGB", (1000, 420), "white"); d = ImageDraw.Draw(im)
    for i, t in enumerate(lines): d.text((30, 20 + i * 75), t, fill="black", font=f)
    b = io.BytesIO(); im.save(b, "PNG"); return b.getvalue()

def _ocr_text(data):
    import numpy as np, ocr_mask
    from PIL import Image
    r = ocr_mask._engine()(np.array(Image.open(io.BytesIO(data)).convert("RGB"))[:, :, ::-1]); return " ".join(r.txts or ())

def test_ocr_masks_phone_and_cccd_in_a_rendered_image():
    import ocr_mask
    assert ocr_mask.available()
    src = _doc_png(); before = _ocr_text(src); assert "0987" in before.replace(" ", "") and "001085012345" in before.replace(" ", "")      # OCR really reads the numbers
    cfg = {"entities": None, "threshold": 0.5, "ner": False}
    out, tags, mime = ocr_mask.mask_image(src, cfg)
    assert {"PHONE", "CCCD"} <= set(tags) and mime == "image/png" and out != src
    after = _ocr_text(out).replace(" ", ""); assert "0987654321" not in after and "001085012345" not in after, after                       # numbers gone from the pixels
    assert "1.500.000" in after, after                                                                                                    # the rest of the image is still useful (price is not personal data)
    from PIL import Image
    im = Image.open(io.BytesIO(out)).convert("RGB"); assert int((__import__("numpy").array(im).sum(axis=2) == 0).sum()) > 3000                      # black boxes exist
    clean, tags2, m2 = ocr_mask.mask_image(_doc_png(("Áo khoác gió xanh", "Giá 1.500.000đ")), cfg); assert tags2 == [] and m2 is None      # nothing personal: untouched
    assert ocr_mask.mask_image(b"not an image", cfg) is None

def test_ocr_unavailable_means_image_is_withheld_not_leaked(monkeypatch):
    import deepagent_api as A, attachments as AT, gateway as gw, ocr_mask
    data = _doc_png(); monkeypatch.setattr(AT, "_bytes", lambda r: data); AT._OCR_CACHE.clear()
    async def cfg_on(): return {"enabled": True, "mode": "mask", "entities": ["VN_PHONE"], "threshold": 0.5, "ner": False}
    monkeypatch.setattr(gw, "_pii_cfg", cfg_on)
    monkeypatch.setattr(ocr_mask, "_engine", lambda: (_ for _ in ()).throw(RuntimeError("no ocr")))
    row = {"id": uuid.uuid4(), "mime_type": "image/png", "sha256": "x"}
    blk = asyncio.run(AT._image_block(row)); assert blk["type"] == "text" and "not shown" in blk["text"] and "base64" not in blk
    async def cfg_off(): return None
    monkeypatch.setattr(gw, "_pii_cfg", cfg_off); AT._OCR_CACHE.clear()
    blk = asyncio.run(AT._image_block(row)); assert blk["type"] == "image" and base64.b64decode(blk["base64"]) == data                       # masking off: unchanged, OCR never touched

def test_server_model_gets_masked_copy_original_stays(pw):
    adm = pw["adm"]; src = _doc_png(); im = _upload(adm, "phieu.png", src, "image/png")
    def ask():
        tid = new_thread(pw)
        r = adm.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": f"agent-{pw['agent']}", "input": {"messages": [{"type": "human", "content": [
            {"type": "text", "text": "xem ảnh"}, {"type": "image", "url": f"/api/files/{im['id']}"}]}]}}, timeout=180); assert r.status_code == 200, r.text
        return json.dumps(adm.get(f"/api/v1/threads/{tid}/state").json()["values"]["messages"][-1], ensure_ascii=False)
    adm.put("/api/privacy/settings", json={"enabled": False}); AT_len = f"image/png {len(src)} B"
    assert AT_len in ask(), "masking off: the model gets the original bytes"
    r = adm.put("/api/privacy/settings", json={"enabled": True, "mode": "mask", "entities": [], "threshold": 0.5, "apply_traces": True, "apply_gateway": True, "ner": False}); assert r.status_code == 200
    ans = ask(); assert "I received 1 image(s): image/png" in ans and AT_len not in ans, ans                                             # masked copy has other bytes
    assert adm.get(f"/api/files/{im['id']}").content == src                                                                              # the stored original is untouched

def test_embeddings_never_see_pii_when_masking_on(monkeypatch):
    import deepagent_api as A, attachments as AT, gateway as gw   # deepagent_api first (attachments <-> deepagent_api import cycle)
    got = []
    async def fake_embed(texts): got.append(list(texts)); return [[0.0] * 3 for _ in texts]
    monkeypatch.setattr(A, "aembed", fake_embed)
    async def cfg_on(): return {"enabled": True, "mode": "pseudonym", "entities": ["VN_PHONE"], "threshold": 0.5, "ner": False, "salt": ""}
    monkeypatch.setattr(gw, "_pii_cfg", cfg_on)
    asyncio.run(AT.aembed_masked([f"Liên hệ {PHONE} để đặt hàng", "câu hỏi không có gì"]))
    assert got[-1][0] == "Liên hệ [PHONE] để đặt hàng" and got[-1][1] == "câu hỏi không có gì"                 # irreversible mask, whatever the workspace mode is
    async def cfg_off(): return None
    monkeypatch.setattr(gw, "_pii_cfg", cfg_off); asyncio.run(AT.aembed_masked([f"Liên hệ {PHONE}"])); assert got[-1] == [f"Liên hệ {PHONE}"]

def test_experiment_results_stored_masked(pw):
    adm = pw["adm"]; ds = adm.post("/api/datasets", json={"name": f"g9p2-pii-ds-{SUF}"}).json()["id"]
    assert adm.post(f"/api/datasets/{ds}/examples", json={"inputs": {"question": f"Khách SĐT {PHONE} cần hỗ trợ"}, "outputs": {"answer": "x"}}).status_code == 200
    x = adm.post("/api/experiments", json={"name": f"g9p2-exp-{SUF}", "dataset_id": ds, "target": {"type": "agent", "agent_id": pw["agent"], "version": 1}}); assert x.status_code == 200, x.text
    xid = x.json()["id"]
    for _ in range(120):
        rows = db("SELECT output::text, coalesce(error,'') FROM exp_results WHERE experiment_id=%s", (xid,))
        if rows: break
        time.sleep(0.5)
    assert rows, "experiment produced no result"
    assert "0912" not in rows[0][0] and "345 678" not in rows[0][0] and ("<PHONE_1>" in rows[0][0] or "[PHONE]" in rows[0][0]), rows[0]          # stored result is masked like traces

def test_res_mask_unit(monkeypatch):
    import deepagent_api as A  # must come first (circular imports)
    import evals, pii
    async def q(sql, args=(), one=False, many=True): return {"workspace_id": 7}
    monkeypatch.setattr(evals.A, "q", q)   # the module evals really uses (another test may have re-imported deepagent_api)
    async def cfg(ws): return pii.normalize_cfg({"enabled": True, "mode": "mask", "apply_traces": True})
    monkeypatch.setattr(pii, "workspace_cfg", cfg)
    o, e = asyncio.run(evals.res_mask(1, {"answer": f"gọi {PHONE}"}, f"lỗi {PHONE}")); assert o.obj == {"answer": "gọi [PHONE]"} and e == "lỗi [PHONE]"
    async def cfg2(ws): return pii.normalize_cfg({"enabled": True, "mode": "mask", "apply_traces": False})
    monkeypatch.setattr(pii, "workspace_cfg", cfg2); o, e = asyncio.run(evals.res_mask(1, {"answer": PHONE}, None)); assert o.obj == {"answer": PHONE} and e is None
    async def cfg3(ws): return None
    monkeypatch.setattr(pii, "workspace_cfg", cfg3); o, e = asyncio.run(evals.res_mask(1, None, None)); assert o is None and e is None

# ============================================================================== 3. Zalo OA: image / file messages + webhook freshness
APP_ID, OA_SECRET, APP_SECRET = f"app{SUF}", f"oa-secret-{SUF}", f"app-secret-{SUF}"
PORT = 8782

def test_fit_image_and_freshness_units():
    import channel_zalo as z
    from PIL import Image
    assert z.fit_image(PNG) == PNG and z.fit_image(b"not an image") is None
    noise = Image.frombytes("RGB", (900, 900), os.urandom(900 * 900 * 3)); buf = io.BytesIO(); noise.save(buf, "PNG"); big = buf.getvalue(); assert len(big) > z.IMG_MAX
    small = z.fit_image(big); assert small and len(small) <= z.IMG_MAX and small[:3] == b"\xff\xd8\xff"
    gif = io.BytesIO(); Image.new("RGB", (40, 40), "red").save(gif, "GIF"); conv = z.fit_image(gif.getvalue()); assert conv and conv[:3] == b"\xff\xd8\xff"   # non JPG/PNG -> JPEG
    now = 1_800_000_000.0; ms = lambda dt: str(int((now + dt) * 1000))  # noqa: E731
    assert z.stale_reason(ms(-60), now) is None and z.stale_reason(ms(-7000), now) is None
    assert "older" in z.stale_reason(ms(-7300), now) and "future" in z.stale_reason(ms(3600), now) and z.stale_reason(ms(200), now) is None
    assert z.stale_reason(None, now) and z.stale_reason("abc", now) and z.stale_reason(ms(-7300), now, limit=864000) is None
    assert z.fresh_limit({}) == z.FRESH_S and z.fresh_limit({"max_event_age_s": 10}) == 60 and z.fresh_limit({"max_event_age_s": "x"}) == z.FRESH_S

class FakeZaloMedia:
    """Fake OA API: OAuth refresh, upload/image, upload/file, message/cs (text | image template | file token). Records everything."""
    def __init__(self):
        self.cs, self.uploads, self.n, self.rt = [], [], 0, {"RT0"}; self.at = set()
    async def oauth(self, req):
        f = dict(await req.post())
        if req.headers.get("secret_key") != APP_SECRET or f.get("refresh_token") not in self.rt: return web.json_response({"error": -14014})
        self.rt.discard(f["refresh_token"]); self.n += 1; at, nrt = f"AT{self.n}", f"RT{self.n}"; self.at.add(at); self.rt.add(nrt)
        return web.json_response({"access_token": at, "refresh_token": nrt, "expires_in": "90000"})
    def _auth(self, req): return req.headers.get("access_token") in self.at
    async def upload(self, req):
        if not self._auth(req): return web.json_response({"error": -216, "message": "Access token is invalid"})
        kind = "image" if req.path.endswith("image") else "file"; rd = await req.multipart(); part = await rd.next(); data = await part.read()
        self.uploads.append({"kind": kind, "name": part.filename, "mime": part.headers.get("Content-Type"), "size": len(data), "data": data, "path": req.path})
        key = f"att-{len(self.uploads)}"
        return web.json_response({"error": 0, "message": "Success", "data": {"attachment_id": key} if kind == "image" else {"token": key}})
    async def csm(self, req):
        body = await req.json()
        if not self._auth(req): return web.json_response({"error": -216, "message": "Access token is invalid"})
        self.cs.append({"user_id": body["recipient"]["user_id"], "message": body["message"], "text": body["message"].get("text")})
        return web.json_response({"error": 0, "message": "Success", "data": {"message_id": uuid.uuid4().hex}})
    def start(self):
        self.loop = asyncio.new_event_loop(); ready = threading.Event()
        def run():
            asyncio.set_event_loop(self.loop); app = web.Application(client_max_size=20 << 20)
            app.router.add_post("/v4/oa/access_token", self.oauth); app.router.add_post("/v3.0/oa/message/cs", self.csm)
            app.router.add_post("/v2.0/oa/upload/image", self.upload); app.router.add_post("/v2.0/oa/upload/file", self.upload)
            self.runner = web.AppRunner(app); self.loop.run_until_complete(self.runner.setup())
            self.loop.run_until_complete(web.TCPSite(self.runner, "127.0.0.1", PORT).start()); ready.set(); self.loop.run_forever()
        threading.Thread(target=run, daemon=True).start(); assert ready.wait(10)
    def stop(self): asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop).result(10); self.loop.call_soon_threadsafe(self.loop.stop)
    def to(self, uid): return [m for m in list(self.cs) if m["user_id"] == str(uid)]

def wait_for(fn, timeout=90, every=0.2):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = fn()
        if r: return r
        time.sleep(every)
    raise AssertionError("timed out waiting")

def zevent(uid, text, ts_ms=None, msg_id=None):
    return {"app_id": APP_ID, "sender": {"id": str(uid)}, "user_id_by_app": f"app-{uid}", "recipient": {"id": "oa-1"}, "event_name": "user_send_text",
            "message": {"msg_id": msg_id or uuid.uuid4().hex, "text": text}, "timestamp": str(ts_ms or int(time.time() * 1000))}

def zpost(bid, ev):
    import hashlib
    body = json.dumps(ev, ensure_ascii=False).encode()
    sg = hashlib.sha256(APP_ID.encode() + body + str(ev["timestamp"]).encode() + OA_SECRET.encode()).hexdigest()
    return httpx.post(f"{B}/channels/zalo/{bid}", content=body, headers={"Content-Type": "application/json", "X-ZEvent-Signature": "mac=" + sg}, timeout=10)

@pytest.fixture(scope="module")
def zl(a):
    fake = FakeZaloMedia(); fake.start(); e = mkws(a, "zalo"); adm = e["adm"]; sec = f"ZALO_{SUF}"
    val = json.dumps({"app_id": APP_ID, "oa_secret_key": OA_SECRET, "app_secret_key": APP_SECRET, "access_token": "", "refresh_token": "RT0", "access_expires_at": 0})
    assert adm.post(f"/api/secrets/{sec}", json={"value": val, "allowed_hosts": ["openapi.zalo.me", "oauth.zaloapp.com"]}).status_code == 200
    r = adm.post(f"/api/agents/{e['agent']}/channels", json={"kind": "zalo", "name": "OA thử 9P2", "secret_ref": sec, "config": {"timeout_s": 120}}); assert r.status_code == 200, r.text
    e.update(fake=fake, bid=r.json()["id"]); yield e
    drop_ws(e); fake.stop()

def untrusted(t, text):
    """11.72: a stranger's text reaches the agent inside the [UNTRUSTED DATA] wrapper (the fake agent echoes it): check wrapper + text."""
    return t.startswith("Echo: [UNTRUSTED DATA") and t.rstrip().endswith("[END UNTRUSTED DATA]") and ("\n" + text + "\n") in t

def test_zalo_image_reply_uploaded_then_sent(zl):
    f, adm = zl["fake"], zl["adm"]; fid = _upload(adm, "anh_san_pham.png", PNG, "image/png")["id"]; ST["zimg"] = fid
    assert zpost(zl["bid"], zevent(9001, f"Gửi ảnh sản phẩm ![ảnh sản phẩm](/api/files/{fid}) nhé")).status_code == 200
    out = wait_for(lambda: len(f.to(9001)) >= 2 and f.to(9001))
    assert untrusted(out[0]["text"], "Gửi ảnh sản phẩm nhé")                                                          # file reference stripped from the text part
    up = [u for u in f.uploads if u["kind"] == "image"][-1]; assert up["data"] == PNG and up["path"] == "/v2.0/oa/upload/image" and up["name"].endswith(".png")
    att = out[1]["message"]["attachment"]; assert att["type"] == "template" and att["payload"]["template_type"] == "media"
    assert att["payload"]["elements"] == [{"media_type": "image", "attachment_id": f"att-{f.uploads.index(up) + 1}"}]

def test_zalo_big_image_is_shrunk_to_1mb(zl):
    from PIL import Image
    f, adm = zl["fake"], zl["adm"]; noise = Image.frombytes("RGB", (900, 900), os.urandom(900 * 900 * 3)); buf = io.BytesIO(); noise.save(buf, "PNG")
    fid = _upload(adm, "anh_lon.png", buf.getvalue(), "image/png")["id"]; n0 = len(f.uploads)
    zpost(zl["bid"], zevent(9002, f"ảnh lớn ![x](/api/files/{fid})"))
    wait_for(lambda: len(f.uploads) > n0)
    up = f.uploads[-1]; assert up["size"] <= 1_000_000 and up["data"][:3] == b"\xff\xd8\xff" and up["name"].endswith(".jpg") and up["mime"] == "image/jpeg"
    assert wait_for(lambda: [m for m in f.to(9002) if (m["message"].get("attachment") or {}).get("type") == "template"])

def test_zalo_pdf_file_reply_and_unsupported_type_note(zl):
    f, adm = zl["fake"], zl["adm"]
    pdf = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"
    fid = _upload(adm, "bao_gia.pdf", pdf, "application/pdf")["id"]; n0 = len(f.uploads)
    zpost(zl["bid"], zevent(9003, f"Báo giá đây [bao_gia.pdf](/api/files/{fid})"))
    wait_for(lambda: len(f.uploads) > n0 and any((m["message"].get("attachment") or {}).get("type") == "file" for m in f.to(9003)))
    up = f.uploads[-1]; assert up["kind"] == "file" and up["path"] == "/v2.0/oa/upload/file" and up["name"] == "bao_gia.pdf" and up["data"] == pdf
    fm = [m for m in f.to(9003) if (m["message"].get("attachment") or {}).get("type") == "file"][0]; assert fm["message"]["attachment"]["payload"] == {"token": f"att-{len(f.uploads)}"}
    assert any(m["text"] and untrusted(m["text"], "Báo giá đây bao_gia.pdf") for m in f.to(9003))             # link replaced by its name
    txt = _upload(adm, "ghi_chu.txt", b"abc", "text/plain")["id"]; n1 = len(f.uploads)
    zpost(zl["bid"], zevent(9004, f"ghi chú [ghi_chu.txt](/api/files/{txt})"))
    note = wait_for(lambda: [m for m in f.to(9004) if (m["text"] or "").startswith("(Không gửi được ghi_chu.txt")])
    assert "PDF/DOC/DOCX" in note[0]["text"] and len(f.uploads) == n1                                               # nothing uploaded for an unsupported type

def test_zalo_foreign_file_reference_stays_text(zl):
    f = zl["fake"]; ghost = str(uuid.uuid4()); n0 = len(f.uploads)
    zpost(zl["bid"], zevent(9005, f"xem ![x](/api/files/{ghost})"))
    out = wait_for(lambda: f.to(9005)); assert f"/api/files/{ghost}" in out[0]["text"] and len(f.uploads) == n0       # unreadable / missing file: text untouched, no upload

def test_zalo_webhook_freshness_window_and_dedupe(zl):
    f = zl["fake"]; hour = 3600 * 1000; now = int(time.time() * 1000)
    r = zpost(zl["bid"], zevent(9010, "tin cũ 3 giờ", ts_ms=now - 3 * hour)); assert r.status_code == 200 and r.json().get("stale") is True
    r = zpost(zl["bid"], zevent(9010, "tin từ tương lai", ts_ms=now + hour)); assert r.status_code == 200 and r.json().get("stale") is True
    time.sleep(1.5); assert not f.to(9010)                                                                              # neither was processed
    mid = uuid.uuid4().hex
    assert zpost(zl["bid"], zevent(9011, "tin 90 phút", ts_ms=now - 90 * 60 * 1000, msg_id=mid)).json().get("stale") is None    # inside the 2 h window: processed
    wait_for(lambda: f.to(9011))
    r = zpost(zl["bid"], zevent(9011, "tin 90 phút", ts_ms=now - 90 * 60 * 1000, msg_id=mid)); assert r.status_code == 200 and r.json().get("duplicate") is True   # retry deduped
    time.sleep(1.5); assert len(f.to(9011)) == 1
    # per-binding override (30 d max) for operators who really want old events
    assert zl["adm"].patch(f"/api/channels/{zl['bid']}", json={"config": {"timeout_s": 120, "max_event_age_s": 5 * 3600}}).status_code == 200
    assert zpost(zl["bid"], zevent(9012, "tin 3 giờ nhưng được phép", ts_ms=now - 3 * hour)).json().get("stale") is None
    assert untrusted(wait_for(lambda: f.to(9012))[0]["text"], "tin 3 giờ nhưng được phép")
