"""GĐ9.P group 3 (24/10/2026): (1) 9.10 leftovers: environment of an ingest key + OTLP export config in the UIs, env badge on /classic/projects, traceparent on
requests/urllib; (2) requirements.lock; (3) Context Hub: get_pack / list_pack_versions AOS tools + legacy skill pins read through pack@prod;
(4) 9.9 Parquet export -> a real S3 API (moto server) -> read back; (5) the i18n dictionary covers the new pages. Local server :8020, fake LLM => $0."""
import http.server, json, os, re, socket, subprocess, sys, threading, uuid
import httpx, psycopg, pytest
from test_fixes import B, ADMIN, a  # noqa: F401  (fixtures)
from test_gd9_context_hub import user, run, skill_md, PW
from test_gd9_parquet import env as pq_env, _seed, _db_counts, pg  # noqa: F401  (pq_env = the parquet workspaces fixture)
from test_gd7_tools import ok, call, basic

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
ROOT = os.path.join(os.path.dirname(__file__), "..")
SUF = uuid.uuid4().hex[:6]

# ------------------------------------------------------------------ 1. 9.10 leftovers
def test_classic_projects_page_has_env_badge_and_key_environment():
    h = httpx.get(B + "/classic/projects", auth=ADMIN).text
    for frag in ('id="npe"', 'id="penv"', 'id="pe"', 'id="peb"', 'id="ke"', "envB(p.environment)", "&environment='+encodeURIComponent($('ke').value)", ".env-prod{", ".env-staging{", ".env-dev{"):
        assert frag in h, frag

def test_admin_page_has_otlp_card():
    h = httpx.get(B + "/classic/admin", auth=ADMIN).text
    for frag in ('id="ot_card"', 'id="ot_ep"', 'id="ot_hd"', 'id="ot_ev"', "api/exports/otlp", "otSave()", "otRun(true)", "{{secret:"):
        assert frag in h, frag

@pytest.fixture(scope="module")
def ws3(a):
    w = a.post("/api/admin/workspaces", json={"name": f"p3-{SUF}"}).json()["id"]
    c = httpx.Client(base_url=B, auth=ADMIN, headers={"X-Workspace-Id": str(w)}, timeout=60)
    yield {"ws": w, "adm": c}
    c.close()
    with pg() as p:
        p.execute("DELETE FROM otel_export_cfg WHERE workspace_id=%s", (w,)); p.execute("DELETE FROM ws_secrets WHERE workspace_id=%s", (w,))
        p.execute("DELETE FROM tr_runs WHERE project_id IN (SELECT id FROM projects WHERE workspace_id=%s)", (w,)); p.execute("DELETE FROM projects WHERE workspace_id=%s", (w,))

def test_ingest_key_environment_and_project_badge_data(ws3):
    c = ws3["adm"]
    p = c.post("/api/obs/projects", json={"name": f"p3-proj-{SUF}", "environment": "staging"}).json(); assert p["environment"] == "staging"
    k = c.post(f"/api/obs/projects/{p['id']}/keys", params={"label": "ci", "environment": "dev"}).json()["api_key"]
    k2 = c.post(f"/api/obs/projects/{p['id']}/keys", params={"label": "plain"}).json()["api_key"]
    ks = {x["label"]: x for x in c.get(f"/api/obs/projects/{p['id']}/keys").json()}
    assert ks["ci"]["environment"] == "dev" and ks["plain"]["environment"] is None            # the UI shows the badge / "theo project"
    assert any(x["id"] == p["id"] and x["environment"] == "staging" for x in c.get("/api/obs/projects").json())
    assert c.patch(f"/api/obs/projects/{p['id']}", json={"environment": "prod"}).status_code == 200   # the select in the setup tab
    assert k and k2

def test_otlp_config_roundtrip_the_way_the_ui_sends_it(ws3):
    c = ws3["adm"]
    assert c.post("/api/secrets/OTLP_TOKEN", json={"value": "tok-123", "allowed_hosts": ["127.0.0.1"]}).status_code == 200
    body = {"enabled": True, "endpoint": "http://127.0.0.1:4318/v1/traces", "headers": {"Authorization": "Bearer {{secret:OTLP_TOKEN}}"}, "include_content": False,
            "environments": ["prod", "staging"], "every_s": 120}
    r = c.put("/api/exports/otlp", json=body); assert r.status_code == 200, r.text
    g = c.get("/api/exports/otlp").json()
    assert g["endpoint"] == body["endpoint"] and g["headers"] == body["headers"] and g["environments"] == ["prod", "staging"] and g["every_s"] == 120 and g["semconv_version"]
    assert c.put("/api/exports/otlp", json={**body, "headers": {"Authorization": "Bearer raw-key"}}).status_code == 422   # the UI shows this message as a toast
    c.put("/api/exports/otlp", json={**body, "enabled": False})

# ------------------------------------------------------------------ 1b. traceparent on requests / urllib
class _Echo(http.server.BaseHTTPRequestHandler):
    seen = []
    def do_GET(self):
        _Echo.seen.append({k.lower(): v for k, v in self.headers.items()}); self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): pass

def test_traceparent_is_propagated_by_requests_and_urllib():
    import otel_ctx as O, requests, urllib.request
    O.install_stdlib(); O.install_stdlib()   # idempotent
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Echo); threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/x"; _Echo.seen.clear()
    try:
        requests.get(url, timeout=5); urllib.request.urlopen(url, timeout=5).read()
        assert all("traceparent" not in h for h in _Echo.seen) and len(_Echo.seen) == 2                          # no active trace: nothing added
        ctx = O.parse("00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01", "vendor=1")
        tok = O.CTX.set(ctx); _Echo.seen.clear()
        try:
            requests.get(url, timeout=5); urllib.request.urlopen(url, timeout=5).read(); urllib.request.urlopen(urllib.request.Request(url), timeout=5).read()
            requests.get(url, headers={"traceparent": "00-" + "1" * 32 + "-" + "2" * 16 + "-01"}, timeout=5)
        finally: O.CTX.reset(tok)
        hs = _Echo.seen
        assert len(hs) == 4 and all(re.fullmatch(r"00-0af7651916cd43dd8448eb211c80319c-[0-9a-f]{16}-01", h["traceparent"]) for h in hs[:3]) and all(h.get("tracestate") == "vendor=1" for h in hs[:3])
        assert hs[3]["traceparent"].startswith("00-" + "1" * 32)                                                    # a caller's own header is never overwritten
    finally: srv.shutdown()

def test_no_unpatched_http_client_in_app_code():
    """Outgoing HTTP in app code must go through httpx (patched) / requests / urllib (patched now). aiohttp only lives inside the Slack/Telegram SDK adapters (vendor calls)."""
    bad = []
    for f in os.listdir(os.path.join(ROOT, "app")):
        if not f.endswith(".py"): continue
        s = open(os.path.join(ROOT, "app", f), encoding="utf-8").read()
        if re.search(r"^\s*(import aiohttp|from aiohttp)", s, re.M) and f not in ("channel_slack.py", "channel_telegram.py"): bad.append(f)
        if re.search(r"^\s*(import http\.client|from http\.client|import httplib2|import urllib3|from urllib3)", s, re.M): bad.append(f)
    assert bad == []

# ------------------------------------------------------------------ 2. lock file
def _names(path):
    out = {}
    for ln in open(path, encoding="utf-8"):
        ln = ln.split("#")[0].strip()
        if not ln: continue
        n = re.split(r"[\[=<>~! @;]", ln, 1)[0]; out[re.sub(r"[-_.]+", "-", n).lower()] = ln
    return out

def test_requirements_lock_pins_everything_and_covers_the_direct_deps():
    lock = _names(os.path.join(ROOT, "requirements.lock")); direct = _names(os.path.join(ROOT, "requirements.in")); dev = _names(os.path.join(ROOT, "requirements-dev.in"))
    assert len(lock) > 150
    for n, ln in lock.items(): assert "==" in ln or " @ " in ln, ln                                                  # every line is an exact pin (or a wheel URL)
    assert not (set(dev) | {"pluggy", "iniconfig", "moto", "boto3"}) & set(lock), "dev-only packages must not be in the runtime lock"
    miss = [n for n in direct if n not in lock]; assert miss == [], miss
    assert {"deepagents", "langgraph", "fastapi", "pyarrow", "cryptography", "presidio-analyzer", "opentelemetry-sdk"} <= set(lock)

def test_uv_pip_check_is_clean():
    uv = os.path.expanduser("~/.local/bin/uv")
    if not os.path.exists(uv): pytest.skip("uv not installed")
    r = subprocess.run([uv, "pip", "check", "--python", sys.executable], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr

# ------------------------------------------------------------------ 3. Context Hub: AOS read tools + legacy skill pins resolve through pack@prod
@pytest.fixture(scope="module")
def hub(a):
    w = a.post("/api/admin/workspaces", json={"name": f"p3h-{SUF}"}).json()["id"]
    e = {"ws": w, "ed": user(a, f"p3h_ed_{SUF}", w), "name": f"p3h_ed_{SUF}"}
    yield e
    e["ed"].close()
    with pg() as p:
        p.execute("DELETE FROM lg_runs WHERE workspace_id=%s", (w,)); p.execute("DELETE FROM lg_threads WHERE workspace_id=%s", (w,))
        p.execute("DELETE FROM agents WHERE workspace_id=%s", (w,)); p.execute("DELETE FROM context_packs WHERE workspace_id=%s", (w,))
        p.execute("DELETE FROM context_versions WHERE context_id IN (SELECT id FROM contexts WHERE workspace_id=%s)", (w,)); p.execute("DELETE FROM contexts WHERE workspace_id=%s", (w,))
        p.execute("DELETE FROM users WHERE username=%s", (e["name"],))

def test_aos_tools_get_pack_and_list_pack_versions(hub):
    c = hub["ed"]; nm = f"p3-pack-{SUF}"
    r = c.post("/api/packs", json={"name": nm, "files": {"AGENTS.md": "# Gói\nNói ngắn gọn.", f"policies/giam-gia.md": "Tối đa 10%"}, "message": "v1", "tag": "prod"}); assert r.status_code == 200, r.text
    pid = r.json()["id"]
    r2 = c.post(f"/api/packs/{pid}/commits", json={"message": "v2 sửa chính sách", "changes": {"policies/giam-gia.md": "Tối đa 15%"}, "tag": "draft"}); assert r2.status_code == 200, r2.text
    H = basic(hub["name"], PW)
    d = ok(H, "get_pack", name=nm); assert d["tags"] == ["prod"] and "Nói ngắn gọn." in d["agents_md"] and set(d["files"]) == {"AGENTS.md", "policies/giam-gia.md"}   # default ref = prod
    assert ok(H, "get_pack", name=nm, ref="draft", path="policies/giam-gia.md")["content"] == "Tối đa 15%"
    assert ok(H, "get_pack", name=nm, ref="prod", path="policies/giam-gia.md")["content"] == "Tối đa 10%"
    assert call(H, "get_pack", name=nm, path="nope.md")[0] and call(H, "get_pack", name="khong-co")[0] and call(H, "get_pack", name=nm, ref="khong-tag")[0]
    v = ok(H, "list_pack_versions", name=nm); assert [x["message"] for x in v["commits"]] == ["v2 sửa chính sách", "v1"] and set(v["tags"]) == {"prod", "draft"}
    assert v["commits"][0]["tags"] == ["draft"] and v["commits"][1]["tags"] == ["prod"] and len(ok(H, "list_pack_versions", name=nm, limit=1)["commits"]) == 1
    adm = basic(*ADMIN)   # another workspace (the default one) cannot see it (RLS)
    assert call(adm, "get_pack", name=nm)[0]

def test_legacy_skill_pins_follow_pack_prod_and_older_pins_stay_frozen(hub):
    c = hub["ed"]; nm = f"ky-nang-p3-{SUF}"
    r = c.post("/api/contexts", json={"kind": "skill", "name": nm, "status": "active", "files": {"SKILL.md": skill_md(nm, f"Mô tả bản cũ alfa{SUF}")}}); assert r.status_code == 200, r.text; cid = r.json()["id"]
    aid = c.post("/api/agents", json={"name": f"ghim-{SUF}", "system_prompt": "x", "skills": [nm]}).json()["id"]
    assert c.get(f"/api/agents/{aid}").json()["config"]["skills"] == [f"{nm}@1"]                       # storage contract unchanged
    _, s = run(c, aid, "show-system"); assert f"alfa{SUF}" in s
    pk = c.get("/api/packs", params={"legacy": cid}).json()[0]; pid = pk["id"]; first = pk["head"]
    # an edit made in the Context Hub UI (commit + move prod) reaches the agent at once
    r = c.post(f"/api/packs/{pid}/commits", json={"message": "sửa trong Hub", "changes": {f"skills/{nm}/SKILL.md": skill_md(nm, f"Mô tả sửa trong Hub beta{SUF}")}, "tag": "prod"}); assert r.status_code == 200, r.text
    _, s = run(c, aid, "show-system"); assert f"beta{SUF}" in s and f"alfa{SUF}" not in s
    # rolling the tag back in the Hub rolls the agent back
    assert c.post(f"/api/packs/{pid}/tags", json={"tag": "prod", "commit": first}).status_code == 200
    _, s = run(c, aid, "show-system"); assert f"alfa{SUF}" in s and f"beta{SUF}" not in s
    # explicit pack ref in the pin: skill@draft / skill@<commit>
    r = c.post(f"/api/packs/{pid}/commits", json={"message": "bản nháp", "changes": {f"skills/{nm}/SKILL.md": skill_md(nm, f"Mô tả nháp draft{SUF}")}, "tag": "draft"}); assert r.status_code == 200, r.text
    aid2 = c.post("/api/agents", json={"name": f"ghim-tag-{SUF}", "system_prompt": "x", "skills": [f"{nm}@draft"]}).json()["id"]
    assert c.get(f"/api/agents/{aid2}").json()["config"]["skills"] == [f"{nm}@draft"]
    _, s = run(c, aid2, "show-system"); assert f"draft{SUF}" in s
    # a legacy publish (v2) moves prod; the agent pinned to v1 (now older than the latest) stays frozen on v1, a new pin sees v2
    assert c.put(f"/api/contexts/{cid}", json={"kind": "skill", "name": nm, "status": "active", "files": {"SKILL.md": skill_md(nm, f"Mô tả bản hai gamma{SUF}")}}).json()["version"] == 2
    _, s = run(c, aid, "show-system"); assert f"alfa{SUF}" in s and f"gamma{SUF}" not in s
    aid3 = c.post("/api/agents", json={"name": f"ghim-moi-{SUF}", "system_prompt": "x", "skills": [nm]}).json()["id"]
    _, s = run(c, aid3, "show-system"); assert f"gamma{SUF}" in s
    assert c.get(f"/api/agents/{aid3}").json()["config"]["skills"] == [f"{nm}@2"]

# ------------------------------------------------------------------ 4. 9.9: export -> real S3 API (moto server) -> read back
def _free_port():
    with socket.socket() as s: s.bind(("127.0.0.1", 0)); return s.getsockname()[1]

@pytest.fixture(scope="module")
def s3():
    pytest.importorskip("moto"); import boto3
    from moto.server import ThreadedMotoServer
    port = _free_port(); srv = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False); srv.start()
    ep = f"http://127.0.0.1:{port}"; bucket = f"aos-exports-{SUF}"
    cli = boto3.client("s3", endpoint_url=ep, aws_access_key_id="AKFAKE", aws_secret_access_key="SKFAKE", region_name="us-east-1")
    cli.create_bucket(Bucket=bucket)
    yield {"ep": ep, "port": port, "bucket": bucket, "cli": cli}
    srv.stop()

def _fs(s3):
    import pyarrow.fs as pafs
    return pafs.S3FileSystem(access_key="AKFAKE", secret_key="SKFAKE", endpoint_override=f"127.0.0.1:{s3['port']}", scheme="http", region="us-east-1")

def _s3_count(s3, ws, name):
    import pyarrow.dataset as ds
    d = f"{s3['bucket']}/agentos/ws={ws}/table={name}"
    import pyarrow.fs as pafs
    fs = _fs(s3)
    if fs.get_file_info(d).type != pafs.FileType.Directory: return 0
    return ds.dataset(d, filesystem=fs, format="parquet", partitioning="hive").to_table().num_rows

def test_parquet_export_to_s3_and_read_back(pq_env, s3):
    adm, ws = pq_env["adm"], pq_env["ws"]
    for n, v in (("S3_AK", "AKFAKE"), ("S3_SK", "SKFAKE")):
        r = adm.post(f"/api/secrets/{n}", json={"value": v, "allowed_hosts": ["127.0.0.1"]}); assert r.status_code == 200, r.text
    cfg = {"enabled": False, "dest": "s3", "s3_endpoint": s3["ep"], "s3_bucket": s3["bucket"], "s3_prefix": "agentos", "s3_region": "us-east-1", "access_key_secret": "S3_AK", "secret_key_secret": "S3_SK"}
    r = adm.put("/api/exports/parquet", json=cfg); assert r.status_code == 200, r.text
    r = adm.post("/api/exports/parquet/run", json={"full": False}); assert r.status_code == 200, r.text
    assert r.json()["last_status"] == "ok", r.text
    with pg() as c: db = _db_counts(c, ws)
    for t in ("traces", "feedback", "eval_results", "gateway_usage"): assert _s3_count(s3, ws, t) == db[t] > 0, (t, db)
    keys = [o["Key"] for o in s3["cli"].list_objects_v2(Bucket=s3["bucket"], Prefix=f"agentos/ws={ws}/").get("Contents", []) if not o["Key"].endswith("/")]   # pyarrow also writes empty "folder/" marker keys
    assert keys and all(k.endswith(".parquet") for k in keys) and any("table=traces/date=" in k for k in keys)
    assert s3["cli"].head_object(Bucket=s3["bucket"], Key=keys[0])["ContentLength"] > 100                                    # HEAD works on the objects
    assert not [k for k in s3["cli"].list_objects_v2(Bucket=s3["bucket"]).get("Contents", []) if f"ws={pq_env['ws2']}/" in k["Key"]]   # the other workspace never lands here
    # incremental: one new trace -> exactly one more row; nothing is duplicated
    with pg() as c: _seed(c, ws, "s3inc", 1, 0, 0, 0)
    assert adm.post("/api/exports/parquet/run", json={"full": False}).json()["last_status"] == "ok"
    assert _s3_count(s3, ws, "traces") == db["traces"] + 1
    # full re-export wipes the prefix (S3 DeleteObjects) and rewrites the same rows
    assert adm.post("/api/exports/parquet/run", json={"full": True}).json()["last_status"] == "ok"
    assert _s3_count(s3, ws, "traces") == db["traces"] + 1 and _s3_count(s3, ws, "gateway_usage") == db["gateway_usage"]
    adm.put("/api/exports/parquet", json={**cfg, "dest": "local"})
    with pg() as c: c.execute("DELETE FROM ws_secrets WHERE workspace_id=%s", (ws,))

# ------------------------------------------------------------------ 5. i18n of the 9.P3 strings (the full crawl is app/tests/i18n_crawl.py)
def test_new_ui_strings_are_translated_both_ways():
    import i18n, ui_obs, ui_admin
    import re
    strip = lambda h: re.sub(r"window\.AOS_DICT=.*?;const D=", "const D=", h, flags=re.S)   # the page embeds the whole dictionary (both languages)
    p = strip(i18n.localize(ui_obs.PAGES["/projects"], "en", cache=False)); a = strip(i18n.localize(ui_admin.PAGES["/admin"], "en", cache=False))
    for frag in ("Project environment", "Key environment", "from the project"): assert frag in p, frag
    for frag in ("Enable OTLP export", "Resend the last 24 hours", "Also send content (input/output)", "Send interval (seconds)"): assert frag in a, frag
    for vi in ("Môi trường của project", "theo dự án", "Bật xuất sang OTLP"): assert vi not in p.replace("from the project", "") and vi not in a
    assert i18n.DICT["(read-only)"]["vi"] == "(chỉ đọc)" and i18n.DICT["Set a limit"]["vi"] and i18n.DICT["Failed"]["vi"]
