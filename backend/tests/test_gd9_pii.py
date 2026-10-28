"""GĐ9.8 (24/10/2026): PII masking (Presidio 2.2.364 + Vietnamese recognizers) and data-subject rights.
Part 1 (in-process, no DB/server): accuracy on tests/data/pii_vi.jsonl (precision/recall >= 95% overall, per entity type printed), checksums, reversible
pseudonyms, the official LangChain PIIMiddleware with our detectors, the gateway masking the request and restoring the answer.
Part 2 (local server :8020 + Postgres, fake LLM => $0): settings RBAC/RLS, masking at trace ingest, masking before the model + restoring the answer in a real
chat run, "Xuất dữ liệu của tôi" (zip), "Xoá dữ liệu của tôi" (cascade, other users untouched, audit anonymised), admin delete on request.
Run with a RAM limit: ulimit -v 4000000."""
import asyncio, base64, io, json, os, sys, unicodedata, uuid, zipfile
from pathlib import Path
import httpx, psycopg, pytest
sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")
from test_fixes import B, ADMIN, a  # noqa: F401,E402  (fixtures)

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
PW = "phase0-gd98-local"; SUF = uuid.uuid4().hex[:6]
DATA = Path(__file__).parent / "data" / "pii_vi.jsonl"

# ================================================================== part 1: engine
def _rows(): return [json.loads(l) for l in DATA.read_text(encoding="utf-8").splitlines() if l.strip()]

def _score(rows, ents):
    import pii
    tp, fp, fn = {}, {}, {}
    for r in rows:
        t = unicodedata.normalize("NFC", r["text"])
        pred = {(f.start, f.end, f.entity) for f in pii.analyze(t, ents, 0.5)}
        gold = {(s["start"], s["end"], s["type"]) for s in r["spans"]}
        for x in pred & gold: tp[x[2]] = tp.get(x[2], 0) + 1
        for x in pred - gold: fp[x[2]] = fp.get(x[2], 0) + 1
        for x in gold - pred: fn[x[2]] = fn.get(x[2], 0) + 1
    return tp, fp, fn

def _report(title, tp, fp, fn):
    lines = [f"{title}: per entity type (exact span + type)"]
    for e in sorted(set(tp) | set(fp) | set(fn)):
        n = tp.get(e, 0); p = n / max(1, n + fp.get(e, 0)); r = n / max(1, n + fn.get(e, 0))
        lines.append(f"  {e:18} precision={p:.3f} recall={r:.3f}  (tp={n} fp={fp.get(e, 0)} fn={fn.get(e, 0)})")
    T, F, N = sum(tp.values()), sum(fp.values()), sum(fn.values())
    lines.append(f"  {'OVERALL':18} precision={T / max(1, T + F):.3f} recall={T / max(1, T + N):.3f}")
    print("\n".join(lines)); return T / max(1, T + F), T / max(1, T + N)

def test_dataset_shape():
    rows = _rows()
    assert len(rows) >= 150
    types = {s["type"] for r in rows for s in r["spans"]}
    assert {"VN_CCCD", "VN_CMND", "VN_PHONE", "VN_TAX_ID", "VN_BANK_ACCOUNT", "VN_LICENSE_PLATE", "EMAIL_ADDRESS", "IP_ADDRESS", "CREDIT_CARD", "PERSON", "ADDRESS"} <= types
    assert sum(1 for r in rows if not r["spans"]) >= 30                      # negatives: prices, dates, order numbers, street names, ...
    for r in rows:
        for s in r["spans"]: assert 0 <= s["start"] < s["end"] <= len(r["text"])

def test_accuracy_all_entities_95_percent(capsys):
    import pii
    tp, fp, fn = _score(_rows(), list(pii.ENTITIES))
    with capsys.disabled(): p, r = _report("ALL (dev + holdout)", tp, fp, fn)
    assert p >= 0.95 and r >= 0.95
    for e in sorted(set(tp) | set(fp) | set(fn)):   # every type individually
        n = tp.get(e, 0)
        assert n / max(1, n + fp.get(e, 0)) >= 0.90 and n / max(1, n + fn.get(e, 0)) >= 0.90, e

def test_accuracy_holdout_only(capsys):
    """The 'holdout' sentences were written after the recognizers were tuned on 'dev'; they must hold the same bar."""
    import pii
    rows = [r for r in _rows() if r.get("split") == "holdout"]; assert len(rows) >= 40
    tp, fp, fn = _score(rows, list(pii.ENTITIES))
    with capsys.disabled(): p, r = _report("HOLDOUT only", tp, fp, fn)
    assert p >= 0.95 and r >= 0.95

def test_default_entities_exclude_opt_in_ones():
    import pii
    assert "PERSON" not in pii.DEFAULT_ENTITIES and "ADDRESS" not in pii.DEFAULT_ENTITIES and "VN_PHONE" in pii.DEFAULT_ENTITIES

def test_tax_checksum_real_public_company_numbers():
    import pii
    for ok in ("0100109106", "0101245486", "0303217354", "0300588569", "0101601092"):   # public business registry numbers (Viettel, Vingroup, TGDD, Vinamilk, FPT)
        assert pii.tax_checksum_ok(ok), ok
    for bad in ("0100109107", "0123456789", "1234567890"): assert not pii.tax_checksum_ok(bad)

def test_cccd_needs_valid_province_without_context():
    import pii
    assert pii.mask_text("mã 079203012345 nằm trên thẻ") == "mã [CCCD] nằm trên thẻ"        # valid province 079 (TP.HCM), century/gender digit 2
    assert pii.mask_text("số 999203012345 lạ") == "số 999203012345 lạ"                         # province 999 does not exist: not masked without context
    assert pii.mask_text("CCCD 999203012345") == "CCCD [CCCD]"                                 # ... but a typo'd number next to the word CCCD still is
    assert pii.mask_text("Đơn hàng 079203012345 đã giao") == "Đơn hàng 079203012345 đã giao"   # order-number context suppresses

def test_phone_formats_and_money_not_phone():
    import pii
    for p in ("0912345678", "0912 345 678", "091.234.5678", "+84 912 345 678", "(+84) 912345678", "+84912345678", "(028) 3822 1234", "024 3825 1234"):
        assert pii.mask_text(f"gọi {p} nhé") == "gọi [PHONE] nhé", p
    for n in ("1.500.000đ", "12/03/2024", "0123456789", "v1.2.3.4"):
        assert pii.mask_text(f"giá {n} ok") == f"giá {n} ok", n

def test_diacritic_free_context_and_nfd_input():
    import pii
    assert pii.mask_text("so dien thoai 0912345678") == "so dien thoai [PHONE]"
    assert pii.mask_text("chuyen khoan stk 0123456789012 vcb") == "chuyen khoan stk [STK] vcb"
    nfd = unicodedata.normalize("NFD", "Số điện thoại 0912345678")
    assert pii.mask_text(nfd) == unicodedata.normalize("NFC", "Số điện thoại [PHONE]")

def test_modes_mask_hash_pseudonym():
    import pii
    t = "SĐT 0912 345 678, gọi lại 0912345678, email an@x.vn"
    assert pii.mask_text(t, {"mode": "mask"}) == "SĐT [PHONE], gọi lại [PHONE], email [EMAIL]"
    h1 = pii.mask_text(t, {"mode": "hash", "salt": "s1"}); h2 = pii.mask_text(t, {"mode": "hash", "salt": "s2"})
    assert h1.count("[PHONE#") == 2 and h1.split("[PHONE#")[1][:8] == h1.split("[PHONE#")[2][:8]   # same number (any spelling) -> same hash
    assert h1 != h2                                                                                   # per-workspace salt
    pz = pii.Pseudonymizer(); m = pii.mask_text(t, {"mode": "pseudonym"}, pz)
    assert m == "SĐT <PHONE_1>, gọi lại <PHONE_1>, email <EMAIL_1>" and pz.restore("Đã gọi <PHONE_1> và gửi <EMAIL_1>.").startswith("Đã gọi 0912 345 678")
    assert pz.restore("PHONE_1 và <PHONE_9>") == "0912 345 678 và <PHONE_9>"                          # tolerant of dropped brackets; unknown tokens untouched

def test_ner_option_is_optional_and_keeps_structured_entities():
    """AOS_PII_NER / workspace `ner`: spaCy xx_ent_wiki_sm on top (+~290 MB RSS, measured; worse than the rules for VN names, see docs/PDPL_GHI_CHU.md)."""
    import pii
    if not pii.ner_available(): pytest.skip("xx_ent_wiki_sm not installed")
    fs = pii.analyze("Nguyễn Văn An gọi 0912345678 từ Hà Nội", ["VN_PHONE", "PERSON", "LOCATION"], 0.5, ner=True)
    assert any(f.entity == "VN_PHONE" for f in fs) and any(f.entity == "PERSON" for f in fs)

def test_mask_obj_masks_strings_not_keys():
    import pii
    o = {"0912345678": ["a@b.vn", {"k": "sdt 0912345678"}, 5, None]}
    assert pii.mask_obj(o) == {"0912345678": ["[EMAIL]", {"k": "sdt [PHONE]"}, 5, None]}

def test_long_text_chunking():
    import pii
    t = ("lorem ipsum dolor " * 500 + "SĐT 0912345678\n") * 12
    assert pii.mask_text(t).count("[PHONE]") == 12

def test_langchain_piimiddleware_with_vn_detectors():
    """Official langchain PIIMiddleware (streaming-safe, state-level) driven by our detectors."""
    import pii
    from langchain_core.messages import HumanMessage
    st = {"messages": [HumanMessage(content="SĐT 0912 345 678 và CCCD 079203012345")]}
    for mw in pii.vn_pii_middleware(["VN_PHONE", "VN_CCCD"], strategy="redact"):
        up = mw.before_model(st, None)
        if up: st = {"messages": up["messages"]}
    assert st["messages"][-1].content == "SĐT [REDACTED_VN_PHONE] và CCCD [REDACTED_VN_CCCD]"
    mw = pii.vn_pii_middleware(["VN_PHONE"], strategy="hash")[0]
    assert "<vn_phone_hash:" in mw.before_model({"messages": [HumanMessage(content="gọi 0912345678")]}, None)["messages"][-1].content

# ------------------------------------------------------------------ gateway in-process (stubs as tests/test_gd7r_gateway.py::_light)
class _Req:
    def __init__(self, messages, system_message=None): self.messages, self.system_message = messages, system_message
    def override(self, **kw):
        r = _Req(self.messages, self.system_message)
        for k, v in kw.items(): setattr(r, k, v)
        return r

def _gw(monkeypatch, cfg):
    import gateway as gw, pii
    from test_gd7r_gateway import _light
    _light(monkeypatch, gw)
    monkeypatch.setattr(gw, "_cur_ws", lambda: 7)
    monkeypatch.setattr(gw, "DEMO_WS", set())
    async def wcfg(ws): return pii.normalize_cfg(cfg)
    monkeypatch.setattr(pii, "workspace_cfg", wcfg)
    return gw

def _call(gw, req, reply_fn):
    from langchain_core.messages import AIMessage
    seen = {}
    class Resp: pass
    async def handler(r):
        seen["messages"], seen["system"] = r.messages, r.system_message
        x = Resp(); x.result = [reply_fn(r)]; return x
    mw = gw.GatewayMiddleware(None, "gemini-3.6-flash", [], 0.2)
    return seen, asyncio.run(mw.awrap_model_call(req, handler))

def test_gateway_masks_request_and_restores_answer(monkeypatch):
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
    gw = _gw(monkeypatch, {"enabled": True, "mode": "pseudonym", "apply_gateway": True})
    req = _Req([HumanMessage(content="Khách có SĐT 0912 345 678, CCCD 079203012345."), AIMessage(content="", tool_calls=[{"name": "tra_cuu", "args": {"sdt": "0912345678"}, "id": "c1"}]),
                ToolMessage(content="Khách: mail an@x.vn", tool_call_id="c1")], SystemMessage(content="Người dùng tên: liên hệ 0912345678"))
    def reply(r):
        sent = " ".join(str(m.content) for m in r.messages)
        return AIMessage(content=f"Đã nhận: {sent[:60]}", tool_calls=[{"name": "gui_sms", "args": {"to": "<PHONE_1>"}, "id": "c2"}])
    seen, resp = _call(gw, req, reply)
    sent = json.dumps([str(m.content) for m in seen["messages"]] + [m.tool_calls for m in seen["messages"] if getattr(m, "tool_calls", None)] + [str(seen["system"].content)], ensure_ascii=False)
    assert "0912" not in sent and "079203012345" not in sent and "an@x.vn" not in sent       # the model never saw the real values
    assert "<PHONE_1>" in sent and "<CCCD_1>" in sent and "<EMAIL_1>" in sent
    out = resp.result[0]
    assert "0912 345 678" in out.content and "<PHONE_1>" not in out.content                   # answer restored for the user
    assert out.tool_calls[0]["args"] == {"to": "0912 345 678"}                                 # ... and tool arguments, so tools get real data

def test_gateway_mask_mode_irreversible_and_off_by_default(monkeypatch):
    from langchain_core.messages import AIMessage, HumanMessage
    gw = _gw(monkeypatch, {"enabled": True, "mode": "mask", "apply_gateway": True})
    seen, resp = _call(gw, _Req([HumanMessage(content="gọi 0912345678")]), lambda r: AIMessage(content=str(r.messages[-1].content)))
    assert seen["messages"][-1].content == "gọi [PHONE]" and resp.result[0].content == "gọi [PHONE]"
    gw = _gw(monkeypatch, {"enabled": False})
    seen, _ = _call(gw, _Req([HumanMessage(content="gọi 0912345678")]), lambda r: AIMessage(content="ok"))
    assert seen["messages"][-1].content == "gọi 0912345678"
    gw = _gw(monkeypatch, {"enabled": True, "mode": "mask", "apply_gateway": False})           # per-workspace switch: traces only
    seen, _ = _call(gw, _Req([HumanMessage(content="gọi 0912345678")]), lambda r: AIMessage(content="ok"))
    assert seen["messages"][-1].content == "gọi 0912345678"

def test_gateway_no_workspace_means_no_masking_no_db(monkeypatch):
    """The in-process path of every existing gateway test (`_cur_ws` -> None) must not touch the DB or mask."""
    import gateway as gw
    from langchain_core.messages import AIMessage, HumanMessage
    from test_gd7r_gateway import _light
    _light(monkeypatch, gw)
    seen, _ = _call(gw, _Req([HumanMessage(content="gọi 0912345678")]), lambda r: AIMessage(content="ok"))
    assert seen["messages"][-1].content == "gọi 0912345678"

# ================================================================== part 2: live server
def pg(): return psycopg.connect(PG, autocommit=True)
def q1(sql, args=()):
    with pg() as c: return c.execute(sql, args).fetchall()
def cl(name, ws=None, pw=PW): return httpx.Client(base_url=B, auth=(name, pw), timeout=150, headers={"X-Workspace-Id": str(ws)} if ws else {})

def mkuser(a, name, ws, role):
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": role, "workspace_id": ws}); assert r.status_code == 200, r.text
    return r.json()["id"]

PHONE, CCCD = "0912 345 678", "079203012345"

@pytest.fixture(scope="module")
def env(a):
    e = {}
    for k in ("w1", "w2"):
        e[k] = a.post("/api/admin/workspaces", json={"name": f"pii-{k}-{SUF}"}).json()["id"]
    e["adm"] = f"pii_adm_{SUF}"; e["u1"] = f"pii_u1_{SUF}"; e["u2"] = f"pii_u2_{SUF}"; e["vw"] = f"pii_vw_{SUF}"; e["adm2"] = f"pii_adm2_{SUF}"
    e["adm_id"] = mkuser(a, e["adm"], e["w1"], "admin"); e["u1_id"] = mkuser(a, e["u1"], e["w1"], "editor"); e["u2_id"] = mkuser(a, e["u2"], e["w1"], "editor")
    e["vw_id"] = mkuser(a, e["vw"], e["w1"], "viewer"); e["adm2_id"] = mkuser(a, e["adm2"], e["w2"], "admin")
    e["c_adm"], e["c_u1"], e["c_u2"], e["c_vw"], e["c_adm2"] = (cl(e["adm"], e["w1"]), cl(e["u1"], e["w1"]), cl(e["u2"], e["w1"]), cl(e["vw"], e["w1"]), cl(e["adm2"], e["w2"]))
    r = e["c_adm"].post("/api/agents", json={"name": f"Trợ lý PII {SUF}", "description": "thử 9.8", "system_prompt": "Bạn là trợ lý."}); assert r.status_code == 200, r.text
    e["agent"] = f"agent-{r.json()['id']}"
    yield e
    for k in ("c_adm", "c_u1", "c_u2", "c_vw", "c_adm2"): e[k].close()
    with pg() as c:
        for w in (e["w1"], e["w2"]):
            c.execute("DELETE FROM pii_settings WHERE workspace_id=%s", (w,)); c.execute("DELETE FROM privacy_requests WHERE workspace_id=%s", (w,))
            c.execute("DELETE FROM lg_runs WHERE workspace_id=%s", (w,)); c.execute("DELETE FROM lg_threads WHERE workspace_id=%s", (w,))
            c.execute("DELETE FROM approvals WHERE agent_id IN (SELECT id FROM agents WHERE workspace_id=%s)", (w,))
            c.execute("DELETE FROM eval_runs WHERE agent_id IN (SELECT id FROM agents WHERE workspace_id=%s)", (w,))
            c.execute("DELETE FROM agents WHERE workspace_id=%s", (w,)); c.execute("DELETE FROM user_api_keys WHERE workspace_id=%s", (w,))
        c.execute("DELETE FROM users WHERE username LIKE %s OR username LIKE 'da-xoa-%%' AND id IN (%s,%s)", (f"pii%{SUF}", e["u1_id"], e["u2_id"]))

def _on(env, mode="pseudonym", **kw):
    body = {"enabled": True, "mode": mode, "entities": [], "threshold": 0.5, "apply_traces": True, "apply_gateway": True, "ner": False, **kw}
    r = env["c_adm"].put("/api/privacy/settings", json=body); assert r.status_code == 200, r.text; return r.json()

def test_vkey_guardrail_block_mode_uses_new_engine(a):
    """/v1/chat/completions virtual keys (pii_mode block) now run on pii.py: VN kinds are named; no model call happens on a blocked request."""
    r = a.post("/api/gw/keys", json={"name": f"pii-{SUF}", "pii_mode": "block"}); assert r.status_code == 200, r.text
    k = r.json()["key"]
    try:
        with httpx.Client(base_url=B, headers={"Authorization": "Bearer " + k}, timeout=60) as c:
            r = c.post("/v1/chat/completions", json={"model": "gemini-3.6-flash", "messages": [{"role": "user", "content": "MST 0100109106 và STK 0123456789012 Vietcombank"}]})
            assert r.status_code == 400 and "MST" in r.text and "STK" in r.text, r.text
    finally:
        with pg() as c: c.execute("DELETE FROM gw_vkeys WHERE name=%s", (f"pii-{SUF}",))

def test_settings_default_off_catalogue_and_rbac(env):
    r = env["c_u1"].get("/api/privacy/settings"); assert r.status_code == 200
    j = r.json(); assert j["enabled"] is False and j["can_edit"] is False and j["mode"] == "mask" and "VN_CCCD" in j["entities"] and "PERSON" not in j["entities"]
    ids = {c["id"] for c in j["catalogue"]}; assert {"VN_PHONE", "VN_TAX_ID", "VN_LICENSE_PLATE", "PERSON", "ADDRESS"} <= ids and j["ner_available"] in (True, False)
    for who in ("c_u1", "c_vw"): assert env[who].put("/api/privacy/settings", json={"enabled": True}).status_code == 403
    assert env["c_adm"].put("/api/privacy/settings", json={"enabled": True, "entities": ["NOPE"]}).status_code == 422
    assert env["c_adm"].put("/api/privacy/settings", json={"enabled": True, "mode": "bogus"}).status_code == 422
    assert env["c_adm"].get("/api/privacy/settings").json()["can_edit"] is True

def test_settings_are_per_workspace_rls(env):
    _on(env, "hash", entities=["VN_PHONE", "PERSON"])
    j = env["c_adm"].get("/api/privacy/settings").json()
    assert j["enabled"] and j["mode"] == "hash" and j["entities"] == ["VN_PHONE", "PERSON"]
    j2 = env["c_adm2"].get("/api/privacy/settings").json(); assert j2["enabled"] is False and j2["entities"] != ["VN_PHONE", "PERSON"]   # other workspace untouched
    seen = {r[0] for r in q1("SELECT workspace_id FROM pii_settings WHERE workspace_id IN (%s,%s)", (env["w1"], env["w2"]))}; assert seen == {env["w1"]}
    assert env["c_adm2"].get("/api/privacy/requests").status_code == 200

def test_privacy_test_endpoint(env):
    _on(env, "mask")
    r = env["c_u1"].post("/api/privacy/test", json={"text": f"SĐT {PHONE}, CCCD {CCCD}, giá 1.500.000đ"}); assert r.status_code == 200, r.text
    j = r.json(); assert j["masked"] == "SĐT [PHONE], CCCD [CCCD], giá 1.500.000đ" and {f["entity"] for f in j["findings"]} == {"VN_PHONE", "VN_CCCD"}
    j = env["c_vw"].post("/api/privacy/test", json={"text": f"SĐT {PHONE}", "mode": "pseudonym"}).json()
    assert j["masked"] == "SĐT <PHONE_1>" and j["restored"] == f"SĐT {PHONE}"

def test_trace_masked_at_ingest_only_in_enabled_workspace(env, a):
    _on(env, "mask"); body_txt = f"Khách gọi {PHONE}, CCCD {CCCD}, mail an.nguyen{SUF}@gmail.com"
    now = "2026-10-24T10:00:00+00:00"; made = []
    try:
        for tag, cli in (("on", env["c_adm"]), ("off", env["c_adm2"])):
            p = cli.post("/api/obs/projects", json={"name": f"pii-{tag}-{SUF}"}); assert p.status_code == 200, p.text
            pid = p.json()["id"]; key = cli.post(f"/api/obs/projects/{pid}/keys").json()["api_key"]; rid = str(uuid.uuid4()); made.append((rid, pid))
            run = {"id": rid, "trace_id": rid, "name": "pii-demo", "run_type": "chain", "start_time": now, "end_time": now, "inputs": {"messages": [{"role": "user", "content": body_txt}]},
                   "outputs": {"text": f"Đã ghi nhận {PHONE}"}, "error": None}
            with httpx.Client(base_url=B, timeout=60, headers={"x-api-key": key}) as ing: assert ing.post("/ingest/runs", json={"post": [run]}).status_code == 200
        rows = {rid: q1("SELECT inputs::text, outputs::text FROM tr_runs WHERE id=%s", (rid,))[0] for rid, _ in made}
        on, off = rows[made[0][0]], rows[made[1][0]]
        assert "[PHONE]" in on[0] and "[CCCD]" in on[0] and "[EMAIL]" in on[0] and "[PHONE]" in on[1]
        assert PHONE not in on[0] + on[1] and CCCD not in on[0] and f"an.nguyen{SUF}" not in on[0]          # never stored
        assert PHONE in off[0] and CCCD in off[0] and PHONE in off[1]                                          # workspace 2 has masking off
    finally:
        with pg() as c:
            for rid, pid in made: c.execute("DELETE FROM tr_runs WHERE id=%s", (rid,)); c.execute("DELETE FROM projects WHERE id=%s", (pid,))

def _chat(c, agent, text):
    tid = c.post("/api/v1/threads", json={"metadata": {"source": "chat", "graph_id": agent}}).json()["thread_id"]
    r = c.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": agent, "input": {"messages": [{"type": "human", "content": text}]}}); assert r.status_code == 200, r.text
    msgs = c.get(f"/api/v1/threads/{tid}/state").json()["values"]["messages"]
    return tid, msgs

def test_chat_run_gateway_reversible_pseudonym_and_masked_trace(env):
    _on(env, "pseudonym"); tid, msgs = _chat(env["c_u1"], env["agent"], f"Khách SĐT {PHONE} CCCD {CCCD} cần hỗ trợ")
    human, ai = msgs[0]["content"], msgs[-1]["content"]
    assert PHONE in human and CCCD in human                          # conversation state keeps what the user typed
    assert "Echo" in ai and PHONE in ai and CCCD in ai and "<PHONE_" not in ai       # the fake model echoed placeholders; the gateway restored the real values
    rows = q1("SELECT inputs::text || coalesce(outputs::text,'') FROM tr_runs WHERE thread_id=%s", (tid,))
    blob = " ".join(r[0] for r in rows); assert rows and PHONE not in blob and CCCD not in blob and "<PHONE_1>" in blob       # traces store the pseudonymised text only

def test_chat_run_mask_mode_model_never_sees_values(env):
    _on(env, "mask"); tid, msgs = _chat(env["c_u1"], env["agent"], f"Khách SĐT {PHONE} cần hỗ trợ")
    assert PHONE in msgs[0]["content"] and "[PHONE]" in msgs[-1]["content"] and PHONE not in msgs[-1]["content"]      # mask mode is irreversible by design

def test_chat_run_untouched_when_off(env):
    r = env["c_adm"].put("/api/privacy/settings", json={"enabled": False}); assert r.status_code == 200
    tid, msgs = _chat(env["c_u1"], env["agent"], f"Khách SĐT {PHONE}")
    assert PHONE in msgs[-1]["content"]
    assert any(PHONE in r[0] for r in q1("SELECT inputs::text FROM tr_runs WHERE thread_id=%s", (tid,)))

# ------------------------------------------------------------------ data-subject rights
@pytest.fixture(scope="module")
def data(env):
    """u1 and u2 each own a thread (+trace), a memory, an uploaded file."""
    _on(env, "mask", apply_gateway=False, apply_traces=False); d = {}
    for k in ("u1", "u2"):
        c = env["c_" + k]; secret = f"bí mật của {k} {uuid.uuid4().hex[:6]}"
        tid, _ = _chat(c, env["agent"], f"ghi chú: {secret}")
        r = c.put("/api/v1/store/items", json={"namespace": ["ghi_chu"], "key": "k1", "value": {"text": secret}}); assert r.status_code == 204, r.text
        f = c.post("/api/files", files={"file": (f"{k}.txt", secret.encode(), "text/plain")}); assert f.status_code == 200, f.text
        d[k] = {"tid": tid, "secret": secret, "file": f.json()["id"]}
    return d

def test_export_my_data_zip(env, data):
    r = env["c_u1"].get("/api/privacy/me/export"); assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content)); names = set(z.namelist())
    assert {"ho_so.json", "cuoc_tro_chuyen.json", "bo_nho.json", "tep.json", "trace.json", "du_an.json", "su_dung_va_nhat_ky.json", "DOC_TOI_DAY.txt"} <= names
    allj = " ".join(z.read(n).decode() for n in names if n.endswith(".json"))
    assert data["u1"]["secret"] in allj and data["u1"]["tid"] in allj                                    # my thread, messages, memory, file name
    assert any(n.startswith("tep/") and data["u1"]["secret"].encode() in z.read(n) for n in names)       # the uploaded file itself
    assert data["u2"]["secret"] not in allj and data["u2"]["tid"] not in allj                            # nothing of another user
    prof = json.loads(z.read("ho_so.json")); assert prof["username"] == env["u1"] and "pw_hash" not in z.read("ho_so.json").decode() and "totp_secret" not in z.read("ho_so.json").decode()
    thr = json.loads(z.read("cuoc_tro_chuyen.json")); assert any(len(t["messages"]) >= 2 for t in thr)
    assert env["c_vw"].get("/api/privacy/me/export").status_code == 200                                  # a viewer may export too

def test_admin_export_scoped_and_rbac(env, data):
    assert env["c_u1"].get(f"/api/admin/privacy/users/{env['u2_id']}/export").status_code in (401, 403)
    r = env["c_adm"].get(f"/api/admin/privacy/users/{env['u2_id']}/export"); assert r.status_code == 200
    allj = " ".join(zipfile.ZipFile(io.BytesIO(r.content)).read(n).decode() for n in zipfile.ZipFile(io.BytesIO(r.content)).namelist() if n.endswith(".json"))
    assert data["u2"]["secret"] in allj and data["u1"]["secret"] not in allj
    assert env["c_adm2"].get(f"/api/admin/privacy/users/{env['u2_id']}/export").status_code == 404      # an admin of another workspace cannot reach him

def test_erase_requires_exact_confirmation_and_blocks_last_admin(env, data):
    with pg() as c: c.execute("DELETE FROM memberships WHERE workspace_id=%s AND user_id NOT IN (%s,%s,%s,%s)", (env["w1"], env["adm_id"], env["u1_id"], env["u2_id"], env["vw_id"]))   # the owner account is a member of every new workspace
    assert env["c_u1"].post("/api/privacy/me/erase", json={"confirm": "xoa"}).status_code == 422
    assert env["c_adm"].post("/api/privacy/me/erase", json={"confirm": env["adm"]}).status_code == 409      # only admin of the workspace
    assert env["c_adm"].post(f"/api/admin/privacy/users/{env['adm_id']}/erase", json={"confirm": env["adm"]}).status_code == 409
    assert q1("SELECT count(*) FROM lg_threads WHERE owner_id=%s", (env["u1_id"],))[0][0] >= 1                # nothing happened

def test_admin_erase_on_request_keeps_other_users(env, data):
    _on(env, "mask", apply_gateway=False, apply_traces=False)
    for who in ("c_u1", "c_vw"): assert env[who].post(f"/api/admin/privacy/users/{env['u2_id']}/erase", json={"confirm": env["u2"]}).status_code in (401, 403)
    assert env["c_adm"].post(f"/api/admin/privacy/users/{env['u2_id']}/erase", json={"confirm": "sai"}).status_code == 422
    assert env["c_adm"].post(f"/api/admin/privacy/users/{env['adm2_id']}/erase", json={"confirm": env["adm2"]}).status_code == 404   # not in this workspace
    r = env["c_adm"].post(f"/api/admin/privacy/users/{env['u2_id']}/erase", json={"confirm": env["u2"], "reason": "yêu cầu của chủ dữ liệu 24/10"}); assert r.status_code == 200, r.text
    c = r.json()["counts"]; assert c["threads"] >= 1 and c["files"] == 1 and c["memories"] >= 1 and c["account_anonymised"] is True
    assert q1("SELECT count(*) FROM lg_threads WHERE owner_id=%s", (env["u2_id"],))[0][0] == 0
    assert q1("SELECT count(*) FROM chat_files WHERE created_by=%s", (env["u2_id"],))[0][0] == 0
    assert q1("SELECT count(*) FROM store WHERE prefix ~ %s", (rf"\.user\.{env['u2_id']}(\.|$)",))[0][0] == 0
    assert q1("SELECT count(*) FROM tr_runs WHERE user_id=%s", (env["u2_id"],))[0][0] == 0
    assert q1("SELECT count(*) FROM lg_runs WHERE thread_id=%s", (data["u2"]["tid"],))[0][0] == 0
    # u1 untouched (threads, file, memory, trace)
    assert q1("SELECT count(*) FROM lg_threads WHERE owner_id=%s", (env["u1_id"],))[0][0] >= 1 and q1("SELECT count(*) FROM chat_files WHERE created_by=%s", (env["u1_id"],))[0][0] == 1
    assert q1("SELECT count(*) FROM store WHERE prefix ~ %s", (rf"\.user\.{env['u1_id']}(\.|$)",))[0][0] >= 1 and q1("SELECT count(*) FROM tr_runs WHERE user_id=%s", (env["u1_id"],))[0][0] >= 1
    u = q1("SELECT username, disabled, display_name FROM users WHERE id=%s", (env["u2_id"],))[0]; assert u[0].startswith("da-xoa-") and u[1] is True and u[2] is None
    assert httpx.get(B + "/api/auth/me", auth=(env["u2"], PW)).status_code == 401                              # cannot log in any more
    assert q1("SELECT count(*) FROM audit_log WHERE user_id=%s AND username=%s", (env["u2_id"], env["u2"]))[0][0] == 0   # the old name is gone from his audit rows ...
    assert q1("SELECT count(*) FROM audit_log WHERE user_id=%s AND ip IS NOT NULL", (env["u2_id"],))[0][0] == 0   # ... and no IP is kept
    assert q1("SELECT count(*) FROM audit_log WHERE detail ILIKE 'privacy:%%' AND detail ILIKE %s", (f"%#{env['u2_id']}%",))[0][0] >= 1   # the erase itself is logged
    lst = env["c_adm"].get("/api/privacy/requests").json(); row = next(x for x in lst if x["kind"] == "erase" and x["subject_user_id"] == env["u2_id"])
    assert row["origin"] == "admin" and row["counts"]["threads"] >= 1 and "bí mật" not in json.dumps(row, ensure_ascii=False) and row["reason"].startswith("yêu cầu")
    assert env["c_adm2"].get("/api/privacy/requests").json() == []                                             # RLS: the other workspace sees nothing

def test_erase_my_data_self_service(env, data):
    r = env["c_u1"].post("/api/privacy/me/erase", json={"confirm": env["u1"]}); assert r.status_code == 200, r.text
    assert r.json()["counts"]["threads"] >= 1
    for t in ("lg_threads", "chat_files"): assert q1(f"SELECT count(*) FROM {t} WHERE {'owner_id' if t == 'lg_threads' else 'created_by'}=%s", (env["u1_id"],))[0][0] == 0
    assert q1("SELECT count(*) FROM tr_runs WHERE user_id=%s", (env["u1_id"],))[0][0] == 0
    assert q1("SELECT count(*) FROM memberships WHERE user_id=%s", (env["u1_id"],))[0][0] == 0
    assert q1("SELECT count(*) FROM gateway_usage WHERE user_id=%s", (env["u1_id"],))[0][0] == 0
    assert httpx.get(B + "/api/auth/me", auth=(env["u1"], PW)).status_code == 401
    assert env["c_vw"].get("/api/privacy/me/export").status_code == 200                                        # the viewer's own data is still there

def test_demo_user_cannot_erase(a, env):
    d = f"pii_demo_{SUF}"
    r = a.post("/api/admin/users", json={"username": d, "password": PW, "role": "viewer", "workspace_id": env["w1"], "demo": True})
    if r.status_code != 200: pytest.skip("demo user creation not available: " + r.text[:100])
    with cl(d, env["w1"]) as c: assert c.post("/api/privacy/me/erase", json={"confirm": d}).status_code == 403
