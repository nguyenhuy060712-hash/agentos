"""GĐ11.73 / 11.ZA2 helper of the local restore drill (ops/backup_drill_local.sh). Never run against the VPS.
  seed   --out F        add marker rows (provider key, OAuth provider + connection, secret connection) to the DB in PG_URI; the plain values go to F (mode 600)
  unseed --in F         remove exactly those rows again
  counts --out F        row count of every part-A/part-B table (JSON)
  check  --in F [--counts C] [--expect-fail]
                        with PG_URI = the restored scratch DB and AOS_SECRET_KEY_FILE = the restored master key: the markers must decrypt to the saved plain values,
                        every sealed cell of aos_crypto.TEXT_TARGETS is opened, and (if given) the row counts equal those taken before the dump.
                        --expect-fail: the same check with a WRONG key must fail (proves the check is not vacuous).
Run with PYTHONPATH=<agentos-local>/app and no AOS_SECRET_KEY / AOS_SECRET_KEYS in the environment. Output: one JSON line, exit 0 = pass."""
import argparse, hashlib, hmac, json, os, sys, uuid
import psycopg

TABLES = ("gw_providers", "ws_secrets", "connections", "oauth_providers", "oauth_connections", "oauth_states", "mcp_servers", "tool_settings", "gw_policies",
          "gw_fallback_chains", "gw_model_access", "gw_vkeys", "model_presets", "model_tiers", "model_feature_access", "channel_bindings", "mail_cursors", "mail_seen",
          "sandboxes", "sandbox_snapshots", "workspace_sandbox_quotas", "crypto_deks", "schema_migrations",
          # GĐ12.48a: part-A state (notify, invites, skills/templates, alerts + rules, retention, hourly stats, trace search, datasets)
          "notify_destinations", "notify_subscriptions", "ws_invites", "context_packs", "alert_rules", "alert_events", "automation_rules", "automation_applied", "trace_retention",
          "tr_stats_hourly", "tr_runs", "datasets", "dataset_examples", "example_revisions", "dataset_tags",
          # GĐ13.60
          "issues", "issue_traces", "issue_events", "engine_project_settings", "engine_runs", "insight_configs", "memory_proposals", "retention_settings", "tool_idem", "store")
G13_TABLES = ("issue_traces", "issue_events", "engine_project_settings", "engine_runs", "insight_configs", "memory_proposals", "retention_settings", "tool_idem")      # must be in every dump (ops/backup_vps83.sh + backup_drill_local.sh check the same list)
G13_MIGRATIONS = ("0200", "0201", "0202", "0210", "0211", "0220", "0225", "0226", "0227", "0230")
VOLATILE = {"oauth_states", "mail_cursors", "mail_seen", "sandboxes", "alert_events", "automation_applied", "trace_retention", "tr_stats_hourly", "tr_runs"}      # change while the server runs: a difference is reported, not a failure
G12_MIGRATIONS = ("0100", "0101", "0105", "0108", "0110", "0115", "0120", "0125", "0150", "0160", "0190")
BODY = b'{"type":"drill.ping","id":"drill"}'      # the fixed webhook body whose signature is saved at seed time
WS = 1


def conn(): return psycopg.connect(os.environ["PG_URI"], autocommit=True)


def counts():
    out = {}
    with conn() as c:
        have = {r[0] for r in c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}
        for t in TABLES:
            if t in have: out[t] = c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
    return out


def seed(path):
    import aos_crypto, crypto
    run = uuid.uuid4().hex[:8]; plain = {"run": run, "provider_key": "sk-drill-" + uuid.uuid4().hex, "client_secret": "cs-drill-" + uuid.uuid4().hex,
                                           "access": "at-drill-" + uuid.uuid4().hex, "refresh": "rt-drill-" + uuid.uuid4().hex, "secret_cred": "sc-drill-" + uuid.uuid4().hex}
    seal = lambda v, col, *key: crypto.seal(v, WS, aos_crypto.ctx(col, *key))      # noqa: E731
    ids = {}
    with conn() as c, c.transaction():      # all or nothing: a failed seed leaves no marker rows behind
        name = "zzdrill_" + run
        c.execute("INSERT INTO ws_secrets(workspace_id, name, value_enc, allowed_hosts, created_by) VALUES (%s,%s,%s,%s,'drill')",
                  (WS, name, seal(plain["provider_key"], "ws_secrets.value_enc", name), ["drill.invalid"]))
        ids["ws_secret"] = name
        ids["gw_provider"] = c.execute("INSERT INTO gw_providers(workspace_id, kind, name, base_url, secret_name, created_by) VALUES (%s,'openai_compat',%s,'https://drill.invalid/v1',%s,'drill') RETURNING id",
                                       (WS, "zz-drill-" + run, name)).fetchone()[0]
        pid = c.execute("INSERT INTO oauth_providers(workspace_id, name, authorize_url, token_url, api_base, client_id) VALUES (%s,%s,'https://drill.invalid/a','https://drill.invalid/t','https://drill.invalid','drill') RETURNING id",
                        (WS, "zz-drill-" + run)).fetchone()[0]
        c.execute("UPDATE oauth_providers SET client_secret_enc=%s WHERE id=%s", (seal(plain["client_secret"], "oauth_providers.client_secret_enc", pid), pid))
        ids["oauth_provider"] = pid
        cid = c.execute("INSERT INTO connections(workspace_id, slug, kind, provider_id, display_name) VALUES (%s,%s,'oauth2',%s,'drill') RETURNING id", (WS, "zz-drill-" + run, pid)).fetchone()[0]
        c.execute("INSERT INTO oauth_connections(provider_id, owner_type, owner_id, connection_id, workspace_id, account, access_token_enc, refresh_token_enc, token_type, kind) "
                  "VALUES (%s,'workspace',0,%s,%s,'drill',%s,%s,'bearer','oauth')",
                  (pid, cid, WS, seal(plain["access"], "oauth_connections.access_token_enc", pid, "workspace-0"), seal(plain["refresh"], "oauth_connections.refresh_token_enc", pid, "workspace-0")))
        sid = c.execute("INSERT INTO connections(workspace_id, slug, kind, display_name) VALUES (%s,%s,'secret','drill') RETURNING id", (WS, "zz-drill-sec-" + run)).fetchone()[0]
        oc = c.execute("INSERT INTO oauth_connections(owner_type, owner_id, connection_id, workspace_id, kind) VALUES ('workspace',0,%s,%s,'secret') RETURNING id", (sid, WS)).fetchone()[0]
        c.execute("UPDATE oauth_connections SET value_enc=%s WHERE id=%s", (seal(plain["secret_cred"], "oauth_connections.value_enc", oc), oc))
        ids.update(connection=cid, secret_connection=sid, secret_cred=oc)
        ids["g12"] = seed12(c, run, plain)
        ids["g13"] = seed13(c, run, plain)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f: json.dump({"plain": plain, "ids": ids}, f)
    return {"seeded": run}


def seed12(c, run, plain):
    """GĐ12.48a: one row of each new part-A kind. The signing key lives in ws_secrets (sealed); the invite token is stored only as sha256 (as in app/invites.py)."""
    import aos_crypto, crypto, destinations
    sname = f"aos_dest_zzdrill{run}_sign"; key = destinations.new_key(); token = "drillinv" + uuid.uuid4().hex + uuid.uuid4().hex[:8]
    plain.update(sign_key=key, sign_sig=destinations.sign(key, BODY), invite_token=token, term="zzdrillword" + run)
    g = {"sign_secret": sname}
    c.execute("INSERT INTO ws_secrets(workspace_id, name, value_enc, allowed_hosts, created_by) VALUES (%s,%s,%s,%s,'drill')", (WS, sname, crypto.seal(key, WS, aos_crypto.ctx("ws_secrets.value_enc", sname)), ["drill.invalid"]))
    did = c.execute("INSERT INTO notify_destinations(workspace_id, name, kind, config, secret_refs, signing_secret_ref, created_by) VALUES (%s,%s,'webhook',%s,%s,%s,'drill') RETURNING id",
                    (WS, "zzdrill-" + run, json.dumps({"url": "https://drill.invalid/hook"}), json.dumps({"signing": sname}), sname)).fetchone()[0]
    g["destination"] = did
    g["subscription"] = c.execute("INSERT INTO notify_subscriptions(workspace_id, destination_id, event_type, created_by) VALUES (%s,%s,'alert.*','drill') RETURNING id", (WS, did)).fetchone()[0]
    admin = c.execute("SELECT m.user_id FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.workspace_id=%s AND m.role='admin' AND NOT u.disabled AND NOT u.demo ORDER BY m.user_id LIMIT 1", (WS,)).fetchone()
    g["invite"] = c.execute("INSERT INTO ws_invites(workspace_id, token_hash, role, created_by, expires_at, max_uses) VALUES (%s,%s,'viewer',%s, now() + interval '2 days', 3) RETURNING id",
                            (WS, hashlib.sha256(token.encode()).hexdigest(), admin[0] if admin else None)).fetchone()[0]
    g["packs"] = [c.execute("INSERT INTO context_packs(workspace_id, name, kind, visibility, created_by) VALUES (%s,%s,%s,'workspace','drill') RETURNING id", (WS, f"zzdrill-{k}-{run}", k)).fetchone()[0] for k in ("skill", "template")]
    pid = c.execute("SELECT id FROM projects WHERE workspace_id=%s ORDER BY id LIMIT 1", (WS,)).fetchone()[0]; g["project"] = pid
    g["alert_rule"] = c.execute("INSERT INTO alert_rules(project_id, name, metric, op, threshold, channel, severity, renotify_min, state) VALUES (%s,%s,'error_rate','>',0.5,'destination',2,60,'ok') RETURNING id", (pid, "zzdrill-" + run)).fetchone()[0]
    g["retention_trace"] = str(uuid.uuid4())
    c.execute("INSERT INTO trace_retention(trace_id, project_id, tier, keep_until, reason, set_by) VALUES (%s,%s,'extended', now() + interval '200 days','manual','drill')", (g["retention_trace"], pid))
    g["stats_name"] = "zzdrill-" + run
    c.execute("INSERT INTO tr_stats_hourly(project_id, hour, environment, run_type, name, model, is_root, is_private, n, n_err) VALUES (%s, date_trunc('hour', now()), 'drill', 'llm', %s, 'm', true, false, 7, 1)", (pid, g["stats_name"]))
    g["dataset"] = c.execute("INSERT INTO datasets(name, version) VALUES (%s,1) RETURNING id", ("zzdrill-" + run,)).fetchone()[0]
    ex = c.execute("INSERT INTO dataset_examples(dataset_id) VALUES (%s) RETURNING id", (g["dataset"],)).fetchone()[0]
    c.execute("INSERT INTO example_revisions(example_id, version, inputs, outputs) VALUES (%s,1,%s,%s)", (ex, json.dumps({"q": "drill " + run}), json.dumps({"a": "ok"})))
    g["run_id"] = str(uuid.uuid4())
    c.execute("INSERT INTO tr_runs(id, trace_id, project_id, name, run_type, start_time, inputs, search_tsv) VALUES (%s,%s,%s,%s,'llm', now(), %s, to_tsvector('simple', %s))",
              (g["run_id"], g["run_id"], pid, "zzdrill-" + run, json.dumps({"q": plain["term"]}), plain["term"]))
    return g


def unseed12(c, g):
    c.execute("DELETE FROM tr_runs WHERE id=%s", (g["run_id"],))
    c.execute("DELETE FROM datasets WHERE id=%s", (g["dataset"],))            # FK cascade: examples, revisions, tags
    c.execute("DELETE FROM tr_stats_hourly WHERE project_id=%s AND name=%s", (g["project"], g["stats_name"]))
    c.execute("DELETE FROM trace_retention WHERE trace_id=%s", (g["retention_trace"],))
    c.execute("DELETE FROM alert_rules WHERE id=%s", (g["alert_rule"],))
    c.execute("DELETE FROM context_packs WHERE id = ANY(%s)", (g["packs"],))
    c.execute("DELETE FROM ws_invites WHERE id=%s", (g["invite"],))
    c.execute("DELETE FROM notify_destinations WHERE id=%s", (g["destination"],))      # subscriptions cascade
    c.execute("DELETE FROM ws_secrets WHERE workspace_id=%s AND name=%s", (WS, g["sign_secret"]))


def check12(c, p, g):
    """On the restored DB with the restored key: the webhook destination still signs, the invite still validates, every other kind of row is back. -> {check: 'ok' | reason}"""
    import aos_crypto, crypto, destinations
    one = lambda sql, *a: c.execute(sql, a).fetchone()[0]      # noqa: E731
    r = {}
    try:
        ref = c.execute("SELECT signing_secret_ref FROM notify_destinations WHERE id=%s AND kind='webhook'", (g["destination"],)).fetchone()[0]
        k = crypto.unseal(one("SELECT value_enc FROM ws_secrets WHERE workspace_id=%s AND name=%s", WS, ref), WS, aos_crypto.ctx("ws_secrets.value_enc", ref))
        r["destination_signs"] = "ok" if k == p["sign_key"] and hmac.compare_digest(destinations.sign(k, BODY), p["sign_sig"]) else "WRONG SIGNATURE"
    except Exception as e: r["destination_signs"] = "error: " + type(e).__name__      # noqa: BLE001
    r["subscription"] = "ok" if one("SELECT count(*) FROM notify_subscriptions WHERE id=%s AND destination_id=%s", g["subscription"], g["destination"]) == 1 else "missing"
    tok = p["invite_token"]      # the same query as invites._find, plus: the inviter must still be an admin, and the plain token must not be stored
    inv = c.execute("SELECT created_by FROM ws_invites WHERE token_hash=%s AND revoked_at IS NULL AND expires_at > now() AND used_count < max_uses", (hashlib.sha256(tok.encode()).hexdigest(),)).fetchone()
    adm = bool(inv and inv[0] and c.execute("SELECT 1 FROM memberships WHERE user_id=%s AND workspace_id=%s AND role='admin'", (inv[0], WS)).fetchone())
    r["invite_validates"] = "ok" if inv and adm and one("SELECT count(*) FROM ws_invites WHERE token_hash=%s", tok) == 0 else "INVALID"
    r["skill_and_template"] = "ok" if sorted(x[0] for x in c.execute("SELECT kind FROM context_packs WHERE id = ANY(%s)", (g["packs"],))) == ["skill", "template"] else "missing"
    r["alert_rule"] = "ok" if one("SELECT count(*) FROM alert_rules WHERE id=%s AND severity=2 AND renotify_min=60", g["alert_rule"]) == 1 else "missing"
    r["retention_tier"] = "ok" if one("SELECT count(*) FROM trace_retention WHERE trace_id=%s AND tier='extended'", g["retention_trace"]) == 1 else "missing"
    r["tr_stats_hourly"] = "ok" if one("SELECT sum(n) FROM tr_stats_hourly WHERE project_id=%s AND name=%s", g["project"], g["stats_name"]) == 7 else "missing"
    r["dataset"] = "ok" if one("SELECT count(*) FROM datasets d JOIN dataset_examples e ON e.dataset_id=d.id JOIN example_revisions r ON r.example_id=e.id WHERE d.id=%s AND r.inputs->>'q'=%s", g["dataset"], "drill " + p["run"]) == 1 else "missing"
    r["trace_search"] = "ok" if one("SELECT count(*) FROM tr_runs WHERE id=%s AND search_tsv @@ to_tsquery('simple', %s)", g["run_id"], p["term"]) == 1 else "NOT FOUND"
    have = {x[0] for x in c.execute("SELECT version FROM schema_migrations")}; miss = [m for m in G12_MIGRATIONS if m not in have]
    r["migrations_0100_0190"] = "ok" if not miss else "missing " + ",".join(miss)
    return r


def seed13(c, run, plain):
    """GĐ13.60: one issue (+ its trace link and history event) and one agent-memory file (LangGraph `store` row, same shape as memory_routes.MemoryLayer)."""
    pid = c.execute("SELECT id FROM projects WHERE workspace_id=%s ORDER BY id LIMIT 1", (WS,)).fetchone()[0]
    plain["memory_text"] = "drill memory " + run
    g = {"issue": c.execute("INSERT INTO issues(workspace_id, project_id, kind, signature, title, severity) VALUES (%s,%s,'manual',%s,%s,3) RETURNING id", (WS, pid, "zzdrill-" + run, "zzdrill " + run)).fetchone()[0]}
    c.execute("INSERT INTO issue_traces(issue_id, run_id) VALUES (%s,%s)", (g["issue"], "zzdrill-" + run))
    c.execute("INSERT INTO issue_events(issue_id, source, action, to_val) VALUES (%s,'system','drill','open')", (g["issue"],))
    g["mem_prefix"] = f"agent.zzdrill{run}.memories"
    c.execute("INSERT INTO store(prefix, key, value) VALUES (%s,'/drill.md',%s)", (g["mem_prefix"], json.dumps({"content": plain["memory_text"], "encoding": "utf-8"})))
    return g


def unseed13(c, g):
    c.execute("DELETE FROM issues WHERE id=%s", (g["issue"],))      # issue_traces + issue_events cascade
    c.execute("DELETE FROM store WHERE prefix=%s", (g["mem_prefix"],))


def check13(c, p, g):
    """On the restored scratch DB: every GĐ13 table is there, the issue with its trace + event, the memory file, migrations 0200-0230. -> {check: 'ok' | reason}"""
    one = lambda sql, *a: c.execute(sql, a).fetchone()[0]      # noqa: E731
    r = {}
    miss = [t for t in G13_TABLES if one("SELECT to_regclass(%s)", "public." + t) is None]
    r["tables_0200_0230"] = "ok" if not miss else "missing " + ",".join(miss)
    r["issue"] = "ok" if one("SELECT count(*) FROM issues WHERE id=%s AND severity=3 AND title=%s", g["issue"], "zzdrill " + p["run"]) == 1 else "missing"
    r["issue_trace_and_event"] = "ok" if one("SELECT (SELECT count(*) FROM issue_traces WHERE issue_id=%s) + (SELECT count(*) FROM issue_events WHERE issue_id=%s)", g["issue"], g["issue"]) == 2 else "missing"
    r["memory_file"] = "ok" if one("SELECT count(*) FROM store WHERE prefix=%s AND key='/drill.md' AND value->>'content'=%s", g["mem_prefix"], p["memory_text"]) == 1 else "missing"
    have = {x[0] for x in c.execute("SELECT version FROM schema_migrations")}; m = [v for v in G13_MIGRATIONS if v not in have]
    r["migrations_0200_0230"] = "ok" if not m else "missing " + ",".join(m)
    return r


def unseed(path):
    d = json.load(open(path)); i = d["ids"]
    with conn() as c:
        if i.get("g13"): unseed13(c, i["g13"])
        if i.get("g12"): unseed12(c, i["g12"])
        c.execute("DELETE FROM connections WHERE id = ANY(%s)", ([i["connection"], i["secret_connection"]],))      # FK cascade removes the credentials
        c.execute("DELETE FROM oauth_providers WHERE id=%s", (i["oauth_provider"],))
        c.execute("DELETE FROM gw_providers WHERE id=%s", (i["gw_provider"],))
        c.execute("DELETE FROM ws_secrets WHERE workspace_id=%s AND name=%s", (WS, i["ws_secret"]))
    return {"unseeded": d["plain"]["run"]}


def sweep():
    """Open every sealed cell of every TEXT_TARGET. -> (ok, failed, per-target)"""
    import aos_crypto, crypto
    ok = bad = 0; per = {}
    with conn() as c:
        have = {r[0] for r in c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}
        for t in aos_crypto.TEXT_TARGETS:
            if t["table"] not in have: continue
            keys = ", ".join(f"{k} AS k_{k}" for k in dict.fromkeys(t["key"]))
            try: rows = c.execute(f"SELECT {t['col']}, {t['ws']} AS _ws, {keys} FROM {t['table']} WHERE {t['col']} IS NOT NULL").fetchall()
            except psycopg.Error: continue
            o = b = 0
            for r in rows:
                try: crypto.unseal(r[0], int(r[1]), aos_crypto.ctx(t["name"], *r[2:])); o += 1
                except Exception: b += 1      # noqa: BLE001
            per[t["name"]] = [o, b]; ok += o; bad += b
    return ok, bad, per


def check(path, counts_path, expect_fail):
    import aos_crypto, crypto
    d = json.load(open(path)); p, i = d["plain"], d["ids"]; res = {"markers": {}}; fail = []
    def want(label, fn, plain):
        try: got = fn()
        except Exception as e: got = None; res["markers"][label] = "error: " + type(e).__name__      # noqa: BLE001
        else: res["markers"][label] = "ok" if got == plain else "WRONG VALUE"
        if res["markers"][label] != "ok": fail.append(label)
    with conn() as c:
        def cell(sql, *a): return c.execute(sql, a).fetchone()[0]
        name = i["ws_secret"]; pid = i["oauth_provider"]
        want("provider_key (ws_secrets, gw_providers.secret_name)", lambda: crypto.unseal(cell("SELECT w.value_enc FROM gw_providers g JOIN ws_secrets w ON w.name=g.secret_name AND w.workspace_id=g.workspace_id WHERE g.id=%s", i["gw_provider"]), WS, aos_crypto.ctx("ws_secrets.value_enc", name)), p["provider_key"])
        want("oauth client_secret", lambda: crypto.unseal(cell("SELECT client_secret_enc FROM oauth_providers WHERE id=%s", pid), WS, aos_crypto.ctx("oauth_providers.client_secret_enc", pid)), p["client_secret"])
        want("oauth access_token", lambda: crypto.unseal(cell("SELECT access_token_enc FROM oauth_connections WHERE provider_id=%s AND owner_type='workspace'", pid), WS, aos_crypto.ctx("oauth_connections.access_token_enc", pid, "workspace-0")), p["access"])
        want("oauth refresh_token", lambda: crypto.unseal(cell("SELECT refresh_token_enc FROM oauth_connections WHERE provider_id=%s AND owner_type='workspace'", pid), WS, aos_crypto.ctx("oauth_connections.refresh_token_enc", pid, "workspace-0")), p["refresh"])
        want("secret connection value", lambda: crypto.unseal(cell("SELECT value_enc FROM oauth_connections WHERE id=%s", i["secret_cred"]), WS, aos_crypto.ctx("oauth_connections.value_enc", i["secret_cred"])), p["secret_cred"])
    if expect_fail:
        if i.get("g12"):
            with conn() as c: sign = check12(c, p, i["g12"])["destination_signs"]
            res["markers"]["g12 webhook signing key"] = sign; fail.append("g12") if sign != "ok" else None
        res["expect_fail"] = True; ok = len(fail) == len(res["markers"]) and fail      # a wrong key must break EVERY marker
        res["pass"] = bool(ok); print(json.dumps(res)); return 0 if ok else 1
    if i.get("g12"):
        with conn() as c: res["g12"] = check12(c, p, i["g12"])
        fail += [f"g12 {k}: {v}" for k, v in res["g12"].items() if v != "ok"]
    if i.get("g13"):
        with conn() as c: res["g13"] = check13(c, p, i["g13"])
        fail += [f"g13 {k}: {v}" for k, v in res["g13"].items() if v != "ok"]
    sok, sbad, per = sweep(); res.update(sweep_ok=sok, sweep_failed=sbad, sweep=per)
    if sbad: fail.append(f"{sbad} sealed cells do not open")
    if counts_path:
        before, after = json.load(open(counts_path)), counts(); diff = {t: [before[t], after.get(t)] for t in before if before[t] != after.get(t)}
        res["counts_compared"] = len(before); res["counts_diff"] = diff
        fail += [f"row count {t}" for t in diff if t not in VOLATILE]
    res["pass"] = not fail; res["fail"] = fail; print(json.dumps(res)); return 0 if not fail else 1


if __name__ == "__main__":
    a = argparse.ArgumentParser(); a.add_argument("cmd", choices=["seed", "unseed", "counts", "check"]); a.add_argument("--out"); a.add_argument("--in", dest="inp")
    a.add_argument("--counts"); a.add_argument("--expect-fail", action="store_true"); a = a.parse_args()
    if a.cmd == "check": sys.exit(check(a.inp, a.counts, a.expect_fail))
    r = seed(a.out) if a.cmd == "seed" else unseed(a.inp) if a.cmd == "unseed" else counts()
    if a.cmd == "counts": json.dump(r, open(a.out, "w")); r = {"tables": len(r)}
    print(json.dumps(r))
