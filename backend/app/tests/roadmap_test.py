"""Roadmap features (assistants, evaluators, backups/worker, sandboxes, tool OAuth). Run: python3 roadmap_test.py [section...]"""
import json, sys, time, uuid, requests
c = dict(l.split("=", 1) for l in open("/workspace/secrets/deepagent_web.txt").read().split())
U = c["url"].rstrip("/"); s = requests.Session(); s.auth = ("moza", c["password"])
R = {}; SECT = set(sys.argv[1:])
def ok(n, cond, extra=""): R[n] = bool(cond); print(("PASS " if cond else "FAIL ") + n, "|", str(extra)[:240], flush=True)
def want(x): return not SECT or x in SECT
def spend():
    try: return s.get(U + "/api/gateway/caps").json()
    except Exception: return None
AG = 73  # cheap cron bench agent

if want("assistants"):
    G = f"agent-{AG}"; tag = uuid.uuid4().hex[:6]
    a = s.post(U + "/api/v1/assistants", json={"graph_id": G, "name": f"rt-asst-{tag}", "description": "roadmap test",
             "config": {"temperature": 0}, "context": {"codeword": "PINEAPPLE"}}).json()
    aid = a.get("assistant_id"); ok("assistant create with context", aid and a.get("context", {}).get("codeword") == "PINEAPPLE", a)
    p = s.patch(U + f"/api/v1/assistants/{aid}", json={"description": "v2", "context": {"codeword": "MANGO"}}).json()
    ok("patch creates version 2", p.get("version") == 2 and p["context"]["codeword"] == "MANGO", p.get("version"))
    v = s.post(U + f"/api/v1/assistants/{aid}/versions", json={}).json()
    ok("versions list", [x["version"] for x in v] == [2, 1], [x["version"] for x in v])
    r = s.post(U + f"/api/v1/assistants/{aid}/latest", json={"version": 1}).json()
    ok("set active version 1 restores context", r.get("version") == 1 and r["context"]["codeword"] == "PINEAPPLE", r.get("context"))
    sr = s.post(U + "/api/v1/assistants/search", json={"query": f"rt-asst-{tag}"}).json()
    ok("search by name", len(sr) == 1 and sr[0]["assistant_id"] == aid, len(sr))
    n = s.post(U + "/api/v1/assistants/count", json={"graph_id": G}).json()
    ok("count", isinstance(n, int) and n >= 2, n)
    sc = s.get(U + f"/api/v1/assistants/{aid}/schemas").json()
    ok("schemas", "config_schema" in sc and "context_schema" in sc, list(sc))
    tid = s.post(U + "/api/v1/threads", json={"metadata": {"t": tag}}).json()["thread_id"]
    w = s.post(U + f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": aid, "input": {"messages": [{"role": "user", "content": "What is the codeword in your runtime context? Reply with only the codeword."}]}}, timeout=200)
    txt = json.dumps(w.json())
    ok("run uses assistant context", "PINEAPPLE" in txt, txt[-200:])
    th = s.get(U + f"/api/v1/assistants/{aid}/threads").json()
    ok("linked threads", any(t["thread_id"] == tid for t in th), len(th))
    ok("delete assistant", s.delete(U + f"/api/v1/assistants/{aid}").status_code == 200)

if want("oauth"):
    import subprocess, os, urllib.parse as up
    def psql(sql): return subprocess.run(["ssh","-p","30029","-i",os.path.expanduser("~/.ssh/moza_vps82"),"root@103.142.24.220",
        f"docker exec lc-postgres psql -U langgraph -d langgraph -tAc \"{sql}\""],capture_output=True,text=True).stdout.strip()
    PN = "mockrt"
    for p in s.get(U + "/api/oauth/providers").json():
        if p["name"] == PN: s.delete(U + f"/api/oauth/providers/{p['id']}")
    B = "http://127.0.0.1:8020/mock-oauth/"
    pv = s.post(U + "/api/oauth/providers", json={"name": PN, "display_name": "Mock OAuth (test)", "authorize_url": U + "/mock-oauth/authorize",
        "token_url": B + "token", "revoke_url": B + "revoke", "userinfo_url": B + "userinfo", "api_base": B + "api/", "client_id": "agentos-mock-client",
        "client_secret": "mock-secret", "scopes": "read write", "pkce": True, "token_auth": "body"}).json()
    ok("provider create (secret write-only)", pv.get("id") and pv.get("has_secret") and "client_secret" not in pv and "client_secret_enc" not in pv, list(pv))
    def flow(tamper=None):
        a = s.post(U + f"/api/oauth/connect/{PN}").json()["authorize_url"]; q = dict(up.parse_qsl(up.urlsplit(a).query))
        r = s.get(a, allow_redirects=False); loc = r.headers.get("location", "")
        if tamper: loc = tamper(loc)
        return q, s.get(loc, allow_redirects=False)
    q, cb = flow(lambda l: l.replace("state=", "state=x"))
    ok("bad state rejected", cb.status_code == 400, cb.status_code)
    q, cb = flow(lambda l: l.replace("code=", "code=x"))
    ok("bad code rejected", cb.status_code == 400 and "invalid_grant" in cb.text, cb.text[:120])
    q, cb = flow()
    ok("authorize uses PKCE S256 + state", q.get("code_challenge_method") == "S256" and len(q.get("code_challenge", "")) == 43 and len(q.get("state", "")) > 30, q.get("code_challenge_method"))
    ok("callback connects", cb.status_code == 303 and "connections" in cb.headers.get("location", ""), cb.status_code)
    C = [x for x in s.get(U + "/api/oauth/connections").json() if x["provider"] == PN]
    ok("connection listed with account", C and C[0]["account"] == "mock.user@example.test" and C[0]["has_refresh"] and C[0]["status"] == "active", C)
    cid = C[0]["id"]
    row = psql(f"SELECT left(access_token_enc,5)||'|'||(access_token_enc LIKE '%mat_%')::text||'|'||left(refresh_token_enc,5) FROM oauth_connections WHERE id={cid}")
    ok("tokens encrypted at rest (Fernet)", row.startswith("gAAAA|false|gAAAA"), row)
    st = psql("SELECT count(*) FROM oauth_states WHERE state='" + q["state"] + "'")
    ok("state is one-time", st == "0", st)
    old = psql(f"SELECT access_token_enc FROM oauth_connections WHERE id={cid}")
    rf = s.post(U + f"/api/oauth/connections/{cid}/refresh").json()
    ok("manual refresh rotates token", rf.get("ok") and psql(f"SELECT access_token_enc FROM oauth_connections WHERE id={cid}") != old, rf)
    # agent tool using caller's token (+ auto-refresh after forced expiry)
    psql(f"UPDATE oauth_connections SET expires_at=now()-interval '1 minute' WHERE id={cid}")
    base = s.get(U + f"/api/agents/{AG}").json()["config"]
    cfg = dict(base, name="rt-oauth-agent", tools=["local:oauth_request"], system_prompt="You call APIs with the oauth_request tool. Be brief.")
    ag = s.post(U + "/api/agents", json=cfg).json()["id"]
    tid = s.post(U + "/api/v1/threads", json={}).json()["thread_id"]
    w = s.post(U + f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": f"agent-{ag}", "input": {"messages": [{"role": "user",
        "content": f"Call oauth_request with provider '{PN}' and path_or_url 'me'. Reply with the project names only."}]}}, timeout=200)
    txt = json.dumps(w.json())
    ok("agent tool calls API with caller token", "alpha" in txt and "beta" in txt, txt[-200:])
    L = s.get(U + "/api/oauth/audit?limit=30").json()
    acts = [(a["action"], a["ok"]) for a in L if a["provider"] == PN]
    ok("auto-refresh on expiry audited", ("token.refresh", True) in acts and any(a["action"] == "token.refresh" and a["detail"].get("reason") == "auto" for a in L if a["provider"] == PN), acts[:6])
    ok("tool request audited with agent id", any(a["action"] == "tool.request" and a["ok"] and a["agent_id"] == ag and a["detail"].get("status") == 200 for a in L), acts[:6])
    s.delete(U + f"/api/agents/{ag}")
    act0 = s.get(U + "/mock-oauth/_debug/active").json()
    rv = s.delete(U + f"/api/oauth/connections/{cid}").json(); act1 = s.get(U + "/mock-oauth/_debug/active").json()
    ok("revoke (RFC 7009) removes tokens remotely + locally", rv.get("remote_revoked") is True and act1["access_tokens"] < act0["access_tokens"]
       and not [x for x in s.get(U + "/api/oauth/connections").json() if x["provider"] == PN], (act0, act1))
    L = s.get(U + "/api/oauth/audit?limit=40").json(); A_ = {a["action"] for a in L if a["provider"] == PN or a["action"] == "connect.callback"}
    ok("audit log covers lifecycle", {"connect.start", "connect.success", "connect.callback", "connect.token", "token.refresh", "tool.request", "connection.revoke"} <= A_, sorted(A_))
    s.delete(U + f"/api/oauth/providers/{pv['id']}")

if want("p2"):
    tag = uuid.uuid4().hex[:6]
    # #18 evaluator editor label, #21 traces deep link, #25 classic density
    ev = s.get(U + "/evaluators").text; ok("#18 evaluator editor says 'Model'", "<label>Model</label>" in ev and "<label>Models</label>" not in ev)
    b = s.get(U + "/builder").text; tr = s.get(U + "/projects").text
    ok("#21 Traces button deep-links to filtered traces", "projects?agent=${id}" in b and "key:'agent_id'" in tr)
    cl = s.get(U + "/studio").text; nw = s.get(U + "/overview").text
    ok("#25 classic pages share one density standard", 'class="cls"' in cl and "body.cls main .card" in cl and 'class="cls"' not in nw)
    # #22 per-key usage
    k = s.post(U + "/api/gw/keys", json={"name": f"rt-key-{tag}"}).json()
    u = s.get(U + f"/api/gw/keys/{k['id']}/usage?days=14").json()
    ok("#22 per-key usage endpoint (14 daily buckets)", len(u.get("daily", [])) == 14 and "by_model" in u, len(u.get("daily", [])))
    gp = s.get(U + "/gateway").text; ok("#22 key detail slide-over with chart", "keyPane" in gp and "Daily calls" in gp)
    s.delete(U + f"/api/gw/keys/{k['id']}")
    # #23 Studio time-travel + fork (cheap agent, 2 short runs)
    tid = f"rt-fork-{tag}"
    s.post(U + f"/api/studio/{AG}/threads/{tid}/run", json={"message": "Remember the word APPLE. Reply OK."}, timeout=200)
    s.post(U + f"/api/studio/{AG}/threads/{tid}/run", json={"message": "Now the word is BANANA. Reply OK."}, timeout=200)
    h = s.get(U + f"/api/studio/{AG}/threads/{tid}/history").json()
    cp = next((c for c in h if c["n_messages"] == 2 and not c["next"]), None)
    ok("#23 checkpoint history (time-travel)", len(h) >= 4 and cp, [(c["step"], c["n_messages"]) for c in h][:6])
    st = s.get(U + f"/api/studio/{AG}/threads/{tid}/state", params={"checkpoint_id": cp["checkpoint_id"]}).json()
    ok("#23 state at past checkpoint", len(st["messages"]) == 2 and "BANANA" not in json.dumps(st), len(st["messages"]))
    f = s.post(U + f"/api/studio/{AG}/threads/{tid}/run", json={"message": "What word did I ask you to remember? One word.", "checkpoint_id": cp["checkpoint_id"]}, timeout=200).json()
    rep = json.dumps(f.get("done"))
    ok("#23 fork from past checkpoint ignores later turns", "APPLE" in rep.upper() and "BANANA" not in rep.upper(), rep[-160:])
    D = s.get(U + "/deployments").text; ok("#24 deployment detail has Assistants tab", "'assistants','Assistants'" in D)

if want("evaluators"):
    tag = uuid.uuid4().hex[:5]
    T = s.get(U + "/api/evaluator_templates").json(); ok("templates listed", len(T) >= 10, [t["id"] for t in T])
    mk = lambda tid, ov=None: s.post(U + "/api/evaluators/from_template", json={"template_id": tid, "name": f"rt_{tid}_{tag}", "overrides": ov or {}}).json()
    test = lambda eid, run, ex=None: s.post(U + f"/api/evaluators/{eid}/test", json={"run": run, "example": ex}).json()
    ref = {"outputs": {"trajectory": ["web_search", "fetch_url"]}}
    cases = {"traj_strict": ([["web_search", "fetch_url"], 1], [["fetch_url", "web_search"], 0]),
             "traj_unordered": ([["fetch_url", "web_search"], 1], [["web_search"], 0]),
             "traj_subset": ([["web_search"], 1], [["web_search", "read_file"], 0]),
             "traj_superset": ([["web_search", "fetch_url", "read_file"], 1], [["fetch_url"], 0])}
    made = []
    for tid, cs in cases.items():
        e = mk(tid); made.append(e["id"])
        res = [test(e["id"], {"output": "x", "trajectory": [{"name": n} for n in tr]}, ref)[0]["score"] for tr, _ in cs]
        ok(f"trajectory {tid}", res == [float(x) for _, x in cs], res)
    a1 = mk("guardrails"); a2 = mk("contains_reference"); made += [a1["id"], a2["id"]]
    r = test(a1["id"], {"output": "Paris", "latency_ms": 30000}, {"outputs": {"answer": "Paris"}})[0]
    ok("assertion partial score (2/3 rules)", abs(r["score"] - 0.6667) < 0.01, r)
    cp = mk("composite_quality", {"evaluator_ids": [a1["id"], a2["id"]], "weights": {str(a1["id"]): 1, str(a2["id"]): 3}}); made.append(cp["id"])
    r = test(cp["id"], {"output": "Paris", "latency_ms": 30000}, {"outputs": {"answer": "Paris"}})[0]
    ok("composite weighted mean", abs(r["score"] - (0.6667 * 1 + 1 * 3) / 4) < 0.01, r)
    d = mk("escalate_decision"); made.append(d["id"])
    r = test(d["id"], {"input": "I want a refund NOW, this is the third time, get me a manager", "output": "I cannot help with that."})[0]
    ok("decision evaluator returns a label + score", r.get("value") in ("escalate", "resolved") and r.get("score") in (0, 1, 0.0, 1.0), r)
    bad = s.post(U + "/api/evaluators", json={"name": f"rt_bad_{tag}", "kind": "trajectory", "config": {"key": "k", "mode": "fuzzy"}})
    ok("trajectory validation rejects bad mode", bad.status_code == 400, bad.status_code)
    bt = s.post(U + "/api/backtests", json={"source_agent_id": 74, "hours": 720, "limit": 2, "candidate": {"agent_id": 74}, "evaluator_ids": [a1["id"]]})
    btj = bt.json(); ok("backtest started", bt.status_code == 200 and btj.get("traces", 0) >= 1, btj)
    if bt.status_code == 200:
        def done(x):
            r = s.get(U + "/api/experiments", params={"dataset_id": btj["dataset_id"]}).json()
            return r if len(r) == 2 and all(e["status"] == "done" for e in r) else None
        t0 = time.time(); r = None
        while time.time() - t0 < 240 and not r: time.sleep(6); r = done(0)
        ok("backtest: production + candidate experiments done with scores", r and all(any(sc["key"] == "guardrails" for sc in e["summary"]["scores"]) for e in r),
           [(e["name"], e["status"], e["summary"]["scores"]) for e in (r or [])])
        cmp_ = s.get(U + "/api/experiments/compare", params={"ids": f"{btj['baseline_experiment_id']},{btj['candidate_experiment_id']}"}).json()
        ok("backtest compare view", len(cmp_.get("experiments", [])) == 2 and len(cmp_.get("results", [])) >= 2, len(cmp_.get("results", [])))
    for i in made: s.delete(U + f"/api/evaluators/{i}")

if want("ops"):
    import subprocess
    SSH = ["ssh", "-p", "30029", "-i", "/home/box/.ssh/moza_vps82", "root@103.142.24.220"]
    o = s.get(U + "/api/admin/ops").json()
    ok("ops status: backups healthy", o["backups"].get("healthy"), {k: o["backups"].get(k) for k in ("last_backup", "last_restore_test")})
    ok("ops status: 2 workers healthy", sum(1 for w in o["workers"] if w["healthy"]) >= 2, [(w["name"], w["age_s"]) for w in o["workers"]])
    aid = s.post(U + "/api/v1/assistants/search", json={"graph_id": f"agent-{AG}"}).json()[0]["assistant_id"]
    before = {w["name"]: w["claimed_total"] for w in o["workers"]}
    runs = []
    for i in range(6):
        tid = s.post(U + "/api/v1/threads", json={}).json()["thread_id"]
        r = s.post(U + f"/api/v1/threads/{tid}/runs", json={"assistant_id": aid, "input": {"messages": [{"role": "user", "content": f"Reply with exactly: ok{i}"}]}}).json()
        runs.append((tid, r["run_id"]))
    t0 = time.time(); st = []
    while time.time() - t0 < 180:
        st = [s.get(U + f"/api/v1/threads/{t}/runs/{r}").json()["status"] for t, r in runs]
        if all(x in ("success", "error", "interrupted") for x in st): break
        time.sleep(4)
    ok("6 background runs all succeed", st.count("success") == 6, st)
    after = {w["name"]: w["claimed_total"] for w in s.get(U + "/api/admin/ops").json()["workers"]}
    got = {k: after.get(k, 0) - before.get(k, 0) for k in after}
    ok("dedicated worker took the runs (API process = fallback)", sum(got.values()) >= 6 and got.get("worker1", 0) >= 4, got)
    # cross-process cancel: long task, cancel through the API, whichever worker runs it must stop
    tid = s.post(U + "/api/v1/threads", json={}).json()["thread_id"]
    r = s.post(U + f"/api/v1/threads/{tid}/runs", json={"assistant_id": aid, "input": {"messages": [{"role": "user", "content": "Count slowly from 1 to 300, one number per line."}]}}).json()
    for _ in range(20):
        if s.get(U + f"/api/v1/threads/{tid}/runs/{r['run_id']}").json()["status"] == "running": break
        time.sleep(1)
    s.post(U + f"/api/v1/threads/{tid}/runs/{r['run_id']}/cancel")
    time.sleep(8); x = s.get(U + f"/api/v1/threads/{tid}/runs/{r['run_id']}").json()
    ok("cancel works across processes", x["status"] == "interrupted", (x["status"], x.get("worker")))
    # crash recovery: SIGKILL the worker, systemd must bring it back and it must heartbeat again
    subprocess.run(SSH + ["systemctl kill -s KILL deepagent-worker"], capture_output=True)
    time.sleep(35)
    w = {w["name"]: w for w in s.get(U + "/api/admin/ops").json()["workers"]}
    ok("worker restarted after SIGKILL and heartbeats", w.get("worker1", {}).get("healthy") and w["worker1"]["age_s"] < 20, w.get("worker1"))
    subprocess.run(SSH + ["systemctl stop deepagent-worker"], capture_output=True); time.sleep(25)
    tid = s.post(U + "/api/v1/threads", json={}).json()["thread_id"]
    w = s.post(U + f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": aid, "input": {"messages": [{"role": "user", "content": "Reply with exactly: fallback"}]}}, timeout=120).json()
    ok("API process runs the queue when the worker is down", "fallback" in json.dumps(w).lower(), json.dumps(w)[-120:])
    subprocess.run(SSH + ["systemctl start deepagent-worker"], capture_output=True); time.sleep(20)
    sp = s.get(U + "/api/gateway/caps").json()["openai"]["spent_usd"]
    import subprocess as sp_
    db = sp_.run(SSH + ["docker exec lc-postgres psql -U langgraph -d langgraph -Atc \"select round(sum(cost_usd)::numeric,6) from gateway_usage where model like 'gpt-%' or model like 'o%'\""], capture_output=True, text=True).stdout.strip()
    ok("spend cap sees both processes (API caps == DB total)", abs(sp - float(db or 0)) < 1e-4, (sp, db))
    b = s.post(U + "/api/admin/backups/run", timeout=300).json()
    ok("backup now + restore drill from Settings API", b.get("ok"), b.get("restore_test"))

if want("sandboxes"):
    import os as _os, subprocess
    sys.path.insert(0, "/workspace/adk_ha_build/os/sdk"); import agentos_sandbox as AS
    env = {**_os.environ, "AGENTOS_ENDPOINT": U, "AGENTOS_USER": "moza", "AGENTOS_PASSWORD": c["password"]}
    sb = AS.Sandboxes(endpoint=U, user="moza", password=c["password"])
    rt = sb.runtime(); ok("isolation runtime is gVisor", "gvisor" in rt.get("runtime", ""), rt)
    x = sb.create("rt-sdk", ttl_hours=2); sid = x["id"]
    sb.put(x, "data.txt", b"version-1")
    r = sb.exec(x, "import platform;print(open('data.txt').read(), platform.release())")
    ok("SDK put + exec inside gVisor", "version-1" in r["stdout"] and "gvisor" in r["stdout"], r)
    r = sb.exec(x, "import socket\ntry:\n socket.create_connection(('1.1.1.1',80),2);print('NET')\nexcept Exception as e: print('blocked')")
    ok("code runs have no network", "blocked" in r["stdout"], r["stdout"])
    snap = sb.snapshot(x, "v1"); sb.put(x, "data.txt", b"version-2"); sb.put(x, "extra.txt", b"x")
    sb.restore(snap, x); files = {f["path"] for f in sb.files(x)}
    ok("snapshot restore brings back exact state", sb.get(x, "data.txt") == b"version-1" and "extra.txt" not in files, files)
    cl = sb.clone(snap, "rt-clone"); ok("clone from snapshot", sb.get(cl, "data.txt") == b"version-1", cl)
    vn = "rtvol" + uuid.uuid4().hex[:5]; v = sb.create_volume(vn)
    r1 = sb.exec(x, f"open('/mnt/{vn}/shared.txt','w').write('from-x');print('w')", mounts=[vn])
    r2 = sb.exec(cl, f"print(open('/mnt/{vn}/shared.txt').read())\ntry:\n open('/mnt/{vn}/y','w').write('1');print('WROTE')\nexcept Exception as e: print('ro-ok')", mounts=[vn + ":ro"])
    ok("volume mount shared across sandboxes (rw + ro)", "from-x" in r2["stdout"] and "ro-ok" in r2["stdout"], (r1.get("stderr"), r2["stdout"]))
    sb.put(x, "index.html", b"<h1>hello from sandbox service</h1>")
    svc = sb.serve(x, ["python", "-m", "http.server", "8000"], port=8000, ttl_minutes=10)
    ok("service starts behind HTTPS URL", svc["status"] == "running" and svc["url"].startswith("https://"), svc)
    body = sb.fetch(svc["url"] + "index.html")
    ok("service reachable through authenticated proxy", b"hello from sandbox service" in body, body[:80])
    anon = requests.get(svc["url"] + "index.html", allow_redirects=False)
    ok("service NOT reachable without auth", anon.status_code in (401, 403, 302, 303), anon.status_code)
    SSH = ["ssh", "-p", "30029", "-i", "/home/box/.ssh/moza_vps82", "root@103.142.24.220"]
    plain = subprocess.run(SSH + [f"curl -s -o /dev/null -w '%{{http_code}}' -u moza:\"$(grep -o 'WEB_PW=[^ ]*' /dev/null)\" http://127.0.0.1:8020/api/sandboxes/{sid}/svc/{svc['id']}/"], capture_output=True, text=True).stdout
    plain2 = subprocess.run(SSH + [f"curl -s -o /dev/null -w '%{{http_code}}' -H 'X-Forwarded-Proto: http' http://127.0.0.1:8020/api/sandboxes/{sid}/svc/{svc['id']}/"], capture_output=True, text=True).stdout
    ok("plain HTTP (no TLS) refused or unauthenticated", plain in ("401", "403") and plain2 in ("401", "403"), (plain, plain2))
    kk = s.post(U + "/api/auth/keys", json={"name": "rt-sbx-tls", "expires_days": 1}).json()
    pr = subprocess.run(SSH + [f"read K; curl -s -o /dev/null -w '%{{http_code}}' -H \"X-Api-Key: $K\" http://127.0.0.1:8020/api/sandboxes/{sid}/svc/{svc['id']}/index.html"],
                        input=kk["key"] + "\n", capture_output=True, text=True).stdout
    ok("authenticated but plain HTTP -> 403 (HTTPS only)", pr == "403", pr)
    sbk = AS.Sandboxes(endpoint=U, api_key=kk["key"])
    ok("SDK with personal API key over HTTPS", b"hello from sandbox service" in sbk.fetch(svc["url"] + "index.html"))
    s.delete(U + f"/api/auth/keys/{kk['id']}")
    lg = sb.logs(x, svc["id"]); ok("service logs", "GET /index.html" in lg["logs"], lg["logs"][-120:])
    out = subprocess.run([sys.executable, "-m", "agentos_sandbox", "services", sid], cwd="/workspace/adk_ha_build/os/sdk", env=env, capture_output=True, text=True)
    ok("CLI lists the service", svc["id"] in out.stdout, out.stdout[-200:] + out.stderr[-200:])
    out = subprocess.run([sys.executable, "-m", "agentos_sandbox", "exec", sid, "print(6*7)"], cwd="/workspace/adk_ha_build/os/sdk", env=env, capture_output=True, text=True)
    ok("CLI exec", out.stdout.strip() == "42", out.stdout + out.stderr)
    sb.stop(x, svc["id"]); time.sleep(1)
    ok("service stopped", sb.services(x)[0]["status"] == "stopped", sb.services(x)[0]["status"])
    for t in (x, cl): sb.delete(t)
    sb.delete_snapshot(snap); sb.delete_volume(v)

c_ = spend(); print("SPEND", json.dumps(c_)[:200] if c_ else None)
print(f"TOTAL {sum(R.values())}/{len(R)}")
