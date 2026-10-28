"""GĐ12.12 automation rules v2 (app/rules.py, migration 0125). In-process on the local Postgres under the app's RLS role with two throw-away workspaces (gd12_world),
fake clock (`now=` of apply_rules / monkeypatched rules._now), fake LLM judge (gateway.generate patched: "[judge-score:x]" in the judged text), fake webhook receiver on loopback.
Every rules call is scoped with project_ids, so real rules of the shared database are never touched. Negative tests: another workspace (projects, datasets, queues, evaluators,
rules, logs, idle conversations), viewer / demo refused, private-IP webhook refused by netguard, spend cap stops the judge, broken rule isolated, rule limit.
The HTTP test at the end runs OUR OWN server on :8061 (killed by its PID) so roles go through the real middleware."""
import asyncio, datetime as dt, hashlib, hmac, json, os, subprocess, sys, time, uuid
import httpx, psycopg, pytest
from psycopg.types.json import Jsonb
from fastapi import HTTPException
from gd12_world import NOW, PG, as_user, run, sysq, with_world
from test_gd6_notify import http  # noqa: F401  (fake webhook receiver on loopback)

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app"))
import deepagent_api  # noqa: E402,F401  (import order: deepagent_api first)
import obs, rules, migrations as M  # noqa: E402

MIN = lambda n: dt.timedelta(minutes=n)   # noqa: E731
def rin(pid, actions, **kw): return rules.RuleIn(project_id=pid, name=kw.pop("name", "t1212"), actions=actions, **kw)

@pytest.fixture(scope="module", autouse=True)
def migrated():
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as c:
            await c.execute("SELECT pg_advisory_lock(hashtext('agentos_boot_ddl'))")
            try:
                try: await M.run_pending(c)
                except M.MigrationError as e:   # another card's migration file was edited after it was applied locally (not ours): apply only 0125, the way the runner does
                    print("run_pending refused:", str(e)[:120])
                    v, m, ck = next(x for x in M._load() if x[0] == "0125")
                    if not await (await c.execute("SELECT 1 FROM schema_migrations WHERE version=%s", (v,))).fetchone():
                        async with c.transaction():
                            await m.up(M._q(c)); await c.execute("INSERT INTO schema_migrations(version,name,checksum) VALUES(%s,%s,%s)", (v, m.NAME, ck))
            finally: await c.execute("SELECT pg_advisory_unlock(hashtext('agentos_boot_ddl'))")
    asyncio.run(go())

@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("PG_URI", PG); monkeypatch.setenv("AOS_EGRESS_ALLOW", "127.0.0.0/8,::1"); monkeypatch.delenv("TELEGRAM_API_BASE", raising=False)
    monkeypatch.setenv("AOS_PUBLIC_URL", "https://aos.example")
    return monkeypatch

def fake_judge(monkeypatch, calls=None, boom=None):
    """gateway.generate -> scripted judge: the score is the number in [judge-score:x] of the prompt (default 1)."""
    import re
    async def gen(client=None, prompt="", chain=None, **k):
        if calls is not None: calls.append(prompt)
        if boom: raise boom
        m = re.search(r"\[judge-score:\s*([0-9.]+)\s*\]", prompt)
        return json.dumps({"score": float(m.group(1)) if m else 1.0, "reasoning": "fake"}), "fake", 0, 0
    monkeypatch.setattr(deepagent_api.gw, "generate", gen)

async def mk_ds(ws, name):
    return (await sysq(deepagent_api, "INSERT INTO datasets(name,workspace_id) VALUES(%s,%s) RETURNING id", (name, ws), one=True))["id"]
async def mk_q(ws, name):
    return (await sysq(deepagent_api, "INSERT INTO annotation_queues(name,rubric,workspace_id) VALUES(%s,%s,%s) RETURNING id", (name, Jsonb([{"key": "k"}]), ws), one=True))["id"]
async def mk_ev(ws, name, kind="llm", cfg=None):
    cfg = cfg or {"key": "quality", "prompt": "Chấm câu trả lời: {{output}}", "score_type": "numeric", "min": 0, "max": 1}
    return (await sysq(deepagent_api, "INSERT INTO evaluators(name,kind,config,workspace_id) VALUES(%s,%s,%s,%s) RETURNING id", (name, kind, Jsonb(cfg), ws), one=True))["id"]
async def n_ex(did): return (await sysq(deepagent_api, "SELECT count(*) n FROM dataset_examples WHERE dataset_id=%s", (did,), one=True))["n"]
async def n_q(qid): return (await sysq(deepagent_api, "SELECT count(*) n FROM queue_items WHERE queue_id=%s", (qid,), one=True))["n"]
async def rule_row(rid): return await sysq(deepagent_api, "SELECT * FROM automation_rules WHERE id=%s", (rid,), one=True)
async def logs(rid, **w):
    sql = "SELECT * FROM automation_log WHERE rule_id=%s" + "".join(f" AND {k}=%s" for k in w) + " ORDER BY id"
    return await sysq(deepagent_api, sql, (rid, *w.values()))

def world(fn, **kw):
    """with_world + removal of what these tests add on top (datasets, queues, destinations, outbox, secrets, crypto keys of the two workspaces)."""
    async def inner(A, admin, W):
        for t in ("automation_applied", "automation_log"): await admin._policy(t, "aos_ws() IS NULL OR (" + admin.RLS_CHILD[t] + ")")   # what rls_init does at boot (12.12)
        try: return await fn(A, admin, W)
        finally:
            ws = [W["a"]["ws"], W["b"]["ws"]]
            for t, col in (("datasets", "workspace_id"), ("annotation_queues", "workspace_id"), ("evaluators", "workspace_id"), ("outbox", "ws_id"), ("notify_destinations", "workspace_id"),
                           ("ws_secrets", "workspace_id"), ("crypto_deks", "workspace_id"), ("crypto_ws", "workspace_id")):
                try: await A.q(f"DELETE FROM {t} WHERE {col} = ANY(%s)", (ws,))
                except Exception as e: print("tidy:", t, str(e)[:80])   # noqa: BLE001
            rules._LISTENERS[:] = [x for x in rules._LISTENERS if x[0] == "rules"]
    return with_world(inner, **kw)

async def mk_dest(admin, ws, url, events=("rule.batch", "rule.error")):
    import destinations as D
    return await as_user(admin, ws, lambda: D.create_destination(D.DestIn(name="t1212 hook", kind="webhook", config={"url": url}, events=list(events))))

def verify(raw, header, key): return hmac.compare_digest("sha256=" + hmac.new(key.encode(), raw, hashlib.sha256).hexdigest(), header or "")

# ---------------------------------------------------------------- migration
def test_migration_0125_up_down_idempotent_and_old_errors_marked():
    sch = "t1212_" + uuid.uuid4().hex[:8]
    mod = {v: m for v, m, _ in M._load()}["0125"]
    async def go():
        with psycopg.connect(PG, autocommit=True) as c: c.execute(f'CREATE SCHEMA "{sch}"')
        try:
            async with await psycopg.AsyncConnection.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
                await c.execute("CREATE TABLE projects(id SERIAL PRIMARY KEY, name TEXT)")
                await c.execute("CREATE TABLE automation_rules(id SERIAL PRIMARY KEY, project_id INT REFERENCES projects(id), name TEXT, actions JSONB NOT NULL DEFAULT '[]')")
                await c.execute("CREATE TABLE automation_log(id BIGSERIAL PRIMARY KEY, rule_id INT, run_id UUID, action TEXT, result TEXT, ts TIMESTAMPTZ DEFAULT now())")
                await c.execute("CREATE TABLE feedback(id BIGSERIAL PRIMARY KEY, project_id INT, created TIMESTAMPTZ DEFAULT now())")
                await c.execute("INSERT INTO projects(name) VALUES('p')"); await c.execute("INSERT INTO automation_rules(project_id,name) VALUES(1,'old')")
                await c.execute("INSERT INTO automation_log(rule_id,result) VALUES(1,'lỗi: x'),(1,'queued')")
                q = M._q(c)
                assert any("1 existing rules" in s for s in await mod.plan(q))
                await mod.up(q); await mod.up(q)   # idempotent
                assert (await (await c.execute("SELECT thread_idle_min, thread_idle_cursor FROM projects")).fetchone()) == (10, None)
                assert (await (await c.execute("SELECT item_type, environment, last_error FROM automation_rules")).fetchone()) == ("runs", None, None)
                assert [r[0] for r in await (await c.execute("SELECT status FROM automation_log ORDER BY id")).fetchall()] == ["error", "ok"]
                with pytest.raises(psycopg.errors.CheckViolation): await c.execute("UPDATE projects SET thread_idle_min=1")
                with pytest.raises(psycopg.errors.CheckViolation): await c.execute("UPDATE automation_rules SET item_type='x'")
                await c.execute("INSERT INTO automation_applied(rule_id,item_key) VALUES(1,'a')")
                with pytest.raises(psycopg.errors.UniqueViolation): await c.execute("INSERT INTO automation_applied(rule_id,item_key) VALUES(1,'a')")
                await c.execute("DELETE FROM automation_rules")   # applied rows go with their rule
                assert (await (await c.execute("SELECT count(*) FROM automation_applied")).fetchone())[0] == 0
                await mod.down(q)
                cols = {r[0] for r in await (await c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s", (sch,))).fetchall()}
                assert not cols & {"thread_idle_min", "item_type", "fb_cursor_ts", "batch_id", "status"} and await (await c.execute("SELECT to_regclass('automation_applied')")).fetchone() == (None,)
        finally:
            with psycopg.connect(PG, autocommit=True) as c: c.execute(f'DROP SCHEMA "{sch}" CASCADE')
    asyncio.run(go())

# ---------------------------------------------------------------- CRUD, validation, isolation, roles
def test_rule_crud_validation_limit_and_other_workspace_refused(env):
    from admin import allowed
    async def go(A, admin, W):
        a, b = W["a"], W["b"]
        da, db = await mk_ds(a["ws"], f"t12-da-{W['tag']}"), await mk_ds(b["ws"], f"t12-db-{W['tag']}")
        qb, eb = await mk_q(b["ws"], f"t12-qb-{W['tag']}"), await mk_ev(b["ws"], f"t12-eb-{W['tag']}")
        mine = lambda fn, ws=a["ws"], **k: as_user(admin, ws, fn, **k)   # noqa: E731
        ok = await mine(lambda: rules.rule_new(rin(a["pid"], [{"type": "add_to_dataset", "dataset_id": da, "junk": 1}], environment="Production")))
        row = await rule_row(ok["id"]); assert row["actions"] == [{"type": "add_to_dataset", "dataset_id": da}] and row["environment"] == "prod" and row["item_type"] == "runs"   # unknown keys dropped
        # another workspace: project, dataset, queue, evaluator -> 404 (same answer as "does not exist")
        for body in (rin(b["pid"], [{"type": "add_tag", "tag": "x"}]), rin(a["pid"], [{"type": "add_to_dataset", "dataset_id": db}]), rin(a["pid"], [{"type": "add_to_queue", "queue_id": qb}]),
                     rin(a["pid"], [{"type": "run_evaluator", "evaluator_id": eb}]), rin(a["pid"], [{"type": "add_to_dataset", "dataset_id": 987654321}])):
            with pytest.raises(HTTPException) as e: await mine(lambda body=body: rules.rule_new(body))
            assert e.value.status_code == 404
        for body, code in ((rin(a["pid"], [{"type": "nope"}]), 400), (rin(a["pid"], [{"type": "add_to_dataset"}]), 422), (rin(a["pid"], [{"type": "add_tag", "tag": ""}]), 422),
                           (rin(a["pid"], [{"type": "webhook", "url": "ftp://x"}]), 422), (rin(a["pid"], [{"type": "webhook"}], filter="bad("), 400), (rin(a["pid"], [{"type": "add_tag", "tag": "x"}], environment="bad env!"), 422),
                           (rin(a["pid"], [{"type": "webhook"}], item_type="threads"), 422), (rin(a["pid"], [{"type": "extend_retention"}], item_type="threads"), 422),
                           (rin(a["pid"], [{"type": "add_to_dataset", "dataset_id": da}], item_type="threads", backfill_hours=2), 422)):
            with pytest.raises(HTTPException) as e: await mine(lambda body=body: rules.rule_new(body))
            assert e.value.status_code == code, (body, e.value.detail)
        # workspace B sees nothing of A's rules and cannot toggle / delete / read the logs
        assert [x for x in await mine(rules.rule_list, ws=b["ws"]) if x["id"] == ok["id"]] == []
        for fn in (lambda: rules.rule_toggle(ok["id"], False), lambda: rules.rule_del(ok["id"]), lambda: rules.rule_logs(ok["id"])):
            with pytest.raises(HTTPException) as e: await mine(fn, ws=b["ws"])
            assert e.value.status_code == 404
        assert (await rule_row(ok["id"]))["enabled"] is True
        # demo user / demo workspace
        with pytest.raises(HTTPException) as e: await mine(lambda: rules.rule_new(rin(a["pid"], [{"type": "add_tag", "tag": "x"}])), demo=True, role="viewer")
        assert e.value.status_code == 403
        # roles: viewer and demo cannot write through the middleware, editor can
        assert not allowed("viewer", "POST", "/api/obs/rules") and not allowed("viewer", "PATCH", f"/api/obs/rules/{ok['id']}") and not allowed("viewer", "DELETE", f"/api/obs/rules/{ok['id']}")
        assert allowed("editor", "POST", "/api/obs/rules") and allowed("viewer", "GET", f"/api/obs/rules/{ok['id']}/logs")
        assert not allowed("viewer", "POST", "/api/obs/rules", demo=True) and not allowed("editor", "PATCH", "/api/obs/projects/1/thread_idle", demo=True)
        # limit AOS_RULES_MAX enabled rules per workspace -> 409 (also when switching one on again)
        env.setenv("AOS_RULES_MAX", "2")
        second = await mine(lambda: rules.rule_new(rin(a["pid"], [{"type": "add_tag", "tag": "y"}])))
        with pytest.raises(HTTPException) as e: await mine(lambda: rules.rule_new(rin(a["pid"], [{"type": "add_tag", "tag": "z"}])))
        assert e.value.status_code == 409
        other = await mine(lambda: rules.rule_new(rin(b["pid"], [{"type": "add_tag", "tag": "z"}])), ws=b["ws"])   # the limit is per workspace
        await mine(lambda: rules.rule_toggle(second["id"], False))
        third = await mine(lambda: rules.rule_new(rin(a["pid"], [{"type": "add_tag", "tag": "w"}])))
        with pytest.raises(HTTPException) as e: await mine(lambda: rules.rule_toggle(second["id"], True))
        assert e.value.status_code == 409 and other["id"] and third["id"]
        await mine(lambda: rules.rule_del(third["id"])); await mine(lambda: rules.rule_toggle(second["id"], True)); await mine(lambda: rules.rule_del(ok["id"]))
        assert await rule_row(ok["id"]) is None
    world(go)

def test_thread_idle_setting_min_two_and_isolation():
    async def go(A, admin, W):
        a, b = W["a"], W["b"]
        assert (await as_user(admin, a["ws"], lambda: rules.idle_get(a["pid"])))["thread_idle_min"] == 10
        await as_user(admin, a["ws"], lambda: rules.idle_set(a["pid"], rules.IdleIn(thread_idle_min=2)))
        assert (await as_user(admin, a["ws"], lambda: rules.idle_get(a["pid"])))["thread_idle_min"] == 2
        with pytest.raises(Exception): rules.IdleIn(thread_idle_min=1)
        with pytest.raises(psycopg.errors.CheckViolation): await sysq(A, "UPDATE projects SET thread_idle_min=1 WHERE id=%s", (a["pid"],))   # the column itself refuses < 2 too
        for fn in (lambda: rules.idle_get(a["pid"]), lambda: rules.idle_set(a["pid"], rules.IdleIn(thread_idle_min=30))):
            with pytest.raises(HTTPException) as e: await as_user(admin, b["ws"], fn)
            assert e.value.status_code == 404
        assert (await sysq(A, "SELECT thread_idle_min FROM projects WHERE id=%s", (a["pid"],), one=True))["thread_idle_min"] == 2
        with pytest.raises(HTTPException) as e: await as_user(admin, a["ws"], lambda: rules.idle_set(a["pid"], rules.IdleIn(thread_idle_min=5)), demo=True, role="viewer")
        assert e.value.status_code == 403
    world(go)

# ---------------------------------------------------------------- fixed order, webhook batch with the judge score
def test_fixed_action_order():
    async def go(A, admin, W):
        a = W["a"]
        plain, comp = await mk_ev(a["ws"], f"t12-p-{W['tag']}"), await mk_ev(a["ws"], f"t12-c-{W['tag']}", "composite", {"key": "all", "feedback_keys": ["quality"]})
        code = await mk_ev(a["ws"], f"t12-k-{W['tag']}", "code", {"key": "k", "code": "def evaluate(run, example):\n    return {'score': 1}"})
        acts = [{"type": "extend_retention"}, {"type": "run_evaluator", "evaluator_id": comp}, {"type": "run_evaluator", "evaluator_id": code}, {"type": "evaluate_thread", "evaluator_id": plain},
                {"type": "run_evaluator", "evaluator_id": plain}, {"type": "add_tag", "tag": "t"}, {"type": "add_to_dataset", "dataset_id": 1}, {"type": "add_to_queue", "queue_id": 1}]
        out = await rules.ordered(acts, await rules._kinds(acts))
        assert [(x["type"], x.get("evaluator_id")) for x in out] == [("add_to_queue", None), ("add_to_dataset", None), ("add_tag", None), ("run_evaluator", plain), ("evaluate_thread", plain),
                                                                     ("run_evaluator", code), ("run_evaluator", comp), ("extend_retention", None)]
    world(go)

def test_webhook_payload_has_the_judge_score_and_is_signed_masked_cut(env, http):
    fake_judge(env)
    async def go(A, admin, W):
        a = W["a"]; D = await mk_dest(admin, a["ws"], http.url + "/h"); key = D["signing_key"]
        ds, qu, ev = await mk_ds(a["ws"], f"t12-d-{W['tag']}"), await mk_q(a["ws"], f"t12-q-{W['tag']}"), await mk_ev(a["ws"], f"t12-e-{W['tag']}")
        rid_ = (await as_user(admin, a["ws"], lambda: rules.rule_new(rin(a["pid"], [{"type": "webhook"}, {"type": "run_evaluator", "evaluator_id": ev}, {"type": "add_to_queue", "queue_id": qu},
                                                                                    {"type": "add_to_dataset", "dataset_id": ds}]))))["id"]
        st = NOW() + dt.timedelta(seconds=2)
        r1, t1 = await run(obs, a["pid"], start=st, name="chat", outputs={"answer": "Liên hệ nguyen.van@example.com [judge-score:0.8]"}, inputs={"question": "x" * 6000})
        env.setattr("pii.workspace_cfg", lambda ws: _async({"mode": "mask"}))
        await rules.apply_rules(project_ids=[a["pid"]])
        assert len(http.got) == 1
        g = http.got[0]; ev_ = json.loads(g["raw"]); d = ev_["data"]
        assert verify(g["raw"], g["headers"]["x-langsmith-signature"], key) and ev_["type"] == "rule.batch"                      # signed delivery through destinations
        assert d["rule_id"] == rid_ and d["project_id"] == a["pid"] and len(d["runs"]) == 1 and d["start_time"] and d["end_time"]
        run_ = d["runs"][0]
        assert run_["id"] == r1 and run_["name"] == "chat" and run_["feedback_stats"]["quality"]["avg"] == 0.8 and run_["feedback_stats"]["quality"]["n"] == 1   # the judge score is in the payload
        assert run_["trace_url"] == f"https://aos.example/projects?p={a['pid']}&trace={t1}"
        assert "[EMAIL]" in json.dumps(run_["outputs"]) and "nguyen.van" not in g["raw"].decode()                               # PII masked when the workspace has it on
        assert isinstance(run_["inputs"], str) and len(run_["inputs"]) <= rules.FIELD_CUT + 1 and run_["inputs"].endswith("…")   # long field cut
        assert await n_ex(ds) == 1 and await n_q(qu) == 1
        order = [x["action"] for x in await logs(rid_) if x["action"] != "webhook"]; assert order == ["add_to_queue", "add_to_dataset", "run_evaluator"]
        wh = (await logs(rid_, action="webhook"))[0]; assert wh["status"] == "ok" and wh["batch_id"] == (await logs(rid_))[0]["batch_id"]
        await rules.apply_rules(project_ids=[a["pid"]]); assert len(http.got) == 1 and await n_ex(ds) == 1                      # nothing new -> nothing sent again
        rr = await rule_row(rid_); assert rr["n_applied"] == 1 and rr["last_error"] is None
        # the rated trace is kept longer (12.18): the rule's own online judge score upgraded it
        k = await sysq(A, "SELECT reason FROM trace_retention WHERE trace_id=%s", (t1,), one=True); assert k and k["reason"] == "evaluator"
    world(go)

def _async(v):
    async def f(): return v
    return f()

def test_chunks_limits_100_runs_and_1mb():
    runs = [{"id": str(i), "inputs": "x" * 10} for i in range(250)]
    parts = rules.chunks(runs); assert [len(p) for p in parts] == [100, 100, 50] and sum(parts, []) == runs
    big = [{"id": str(i), "inputs": "y" * 400_000} for i in range(5)]
    parts = rules.chunks(big); assert [len(p) for p in parts] == [2, 2, 1] and all(len(json.dumps(p)) < 1_000_000 for p in parts)
    assert rules._cut("z" * 5000).endswith("…") and rules._cut({"a": 1}) == {"a": 1}

def test_webhook_without_destination_is_logged_skipped_and_private_ip_is_refused(env, http):
    async def go(A, admin, W):
        a = W["a"]
        r_none = (await as_user(admin, a["ws"], lambda: rules.rule_new(rin(a["pid"], [{"type": "webhook"}], name="none"))))["id"]
        r_ip = (await as_user(admin, a["ws"], lambda: rules.rule_new(rin(a["pid"], [{"type": "webhook", "url": "http://169.254.169.254/latest/meta-data"}], name="ip"))))["id"]
        r_loc = (await as_user(admin, a["ws"], lambda: rules.rule_new(rin(a["pid"], [{"type": "webhook", "url": "http://10.255.255.1:9/x"}], name="priv"))))["id"]
        await run(obs, a["pid"], start=NOW() + dt.timedelta(seconds=2))
        await rules.apply_rules(project_ids=[a["pid"]])
        s = (await logs(r_none, action="webhook"))[0]; assert s["status"] == "skipped" and "rule.batch" in s["detail"]
        for rid in (r_ip, r_loc):   # netguard: link-local / private addresses are not in AOS_EGRESS_ALLOW -> refused, logged as an error, nothing sent
            w = (await logs(rid, action="webhook"))[0]; assert w["status"] == "error" and "blocked" in w["result"], w
            assert (await rule_row(rid))["last_error"]
        assert http.got == []
        ob = await sysq(A, "SELECT status, detail FROM outbox WHERE ws_id=%s AND kind='webhook' ORDER BY id", (a["ws"],)); assert ob and all(o["status"] == "failed" and "blocked" in o["detail"] for o in ob)
    world(go)

def test_send_error_emits_rule_error_to_a_destination(env, http):
    async def go(A, admin, W):
        a = W["a"]; await mk_dest(admin, a["ws"], http.url + "/h", events=("rule.error",))
        rid = (await as_user(admin, a["ws"], lambda: rules.rule_new(rin(a["pid"], [{"type": "webhook", "url": "http://169.254.169.254/x"}], name="boom"))))["id"]
        await run(obs, a["pid"], start=NOW() + dt.timedelta(seconds=2))
        await rules.apply_rules(project_ids=[a["pid"]])
        assert len(http.got) == 1 and json.loads(http.got[0]["raw"])["type"] == "rule.error" and json.loads(http.got[0]["raw"])["data"]["rule_id"] == rid
    world(go)

# ---------------------------------------------------------------- environment, workspace safety at apply time, broken rule, spend cap
def test_environment_rule_ignores_other_environments():
    async def go(A, admin, W):
        a = W["a"]; d_prod, d_all = await mk_ds(a["ws"], f"t12-pr-{W['tag']}"), await mk_ds(a["ws"], f"t12-al-{W['tag']}")
        await as_user(admin, a["ws"], lambda: rules.rule_new(rin(a["pid"], [{"type": "add_to_dataset", "dataset_id": d_prod}], environment="prod", name="prod only")))
        await as_user(admin, a["ws"], lambda: rules.rule_new(rin(a["pid"], [{"type": "add_to_dataset", "dataset_id": d_all}], name="all envs")))
        st = NOW() + dt.timedelta(seconds=2)
        await run(obs, a["pid"], start=st, metadata={"environment": "dev"}); await run(obs, a["pid"], start=st + dt.timedelta(seconds=1), metadata={"environment": "production"})
        await rules.apply_rules(project_ids=[a["pid"]])
        assert await n_ex(d_prod) == 1 and await n_ex(d_all) == 2      # prod rule: only the prod run; a rule without environment keeps today's behaviour
    world(go)

def test_rule_cannot_write_into_another_workspace_even_if_the_row_was_forged(env):
    fake_judge(env)
    async def go(A, admin, W):
        a, b = W["a"], W["b"]
        db, qb, eb = await mk_ds(b["ws"], f"t12-db-{W['tag']}"), await mk_q(b["ws"], f"t12-qb-{W['tag']}"), await mk_ev(b["ws"], f"t12-eb-{W['tag']}")
        acts = [{"type": "add_to_dataset", "dataset_id": db}, {"type": "add_to_queue", "queue_id": qb}, {"type": "run_evaluator", "evaluator_id": eb}]
        rid = (await sysq(A, "INSERT INTO automation_rules(project_id,name,actions) VALUES(%s,'forged',%s) RETURNING id", (a["pid"], Jsonb(acts)), one=True))["id"]   # bypasses the API check on purpose
        r1, _ = await run(obs, a["pid"], start=NOW() + dt.timedelta(seconds=2))
        await rules.apply_rules(project_ids=[a["pid"]])
        assert await n_ex(db) == 0 and await n_q(qb) == 0
        assert (await sysq(A, "SELECT count(*) n FROM feedback WHERE run_id=%s", (r1,), one=True))["n"] == 0
        assert {x["status"] for x in await logs(rid)} == {"error"} and len(await logs(rid)) == 3
        # and the other way round: B's rule never touches A's runs
        assert (await sysq(A, "SELECT count(*) n FROM tr_runs WHERE project_id=%s", (b["pid"],), one=True))["n"] == 0
    world(go)

def test_filter_is_compiled_inside_the_rules_workspace(env):
    seen = []
    real = obs.compile_filter
    def spy(s):
        import aos_crypto; seen.append(aos_crypto._cur_ws()); return real(s)
    env.setattr(obs, "compile_filter", spy)
    async def go(A, admin, W):
        a = W["a"]; ds = await mk_ds(a["ws"], f"t12-s-{W['tag']}")
        await sysq(A, "INSERT INTO automation_rules(project_id,name,filter,actions) VALUES(%s,'s',%s,%s)", (a["pid"], 'search("zzz")', Jsonb([{"type": "add_to_dataset", "dataset_id": ds}])))
        await run(obs, a["pid"], start=NOW() + dt.timedelta(seconds=2))
        seen.clear(); await rules.apply_rules(project_ids=[a["pid"]])
        assert seen and set(seen) == {a["ws"]}      # search() needs the workspace (blind index of encrypted workspaces)
    world(go)

def test_broken_rule_does_not_stop_the_others_and_keeps_its_error(env):
    async def go(A, admin, W):
        a = W["a"]; ds = await mk_ds(a["ws"], f"t12-b-{W['tag']}")
        bad = (await sysq(A, "INSERT INTO automation_rules(project_id,name,filter,actions) VALUES(%s,'bad','and(',%s) RETURNING id", (a["pid"], Jsonb([{"type": "add_tag", "tag": "x"}])), one=True))["id"]
        good = (await as_user(admin, a["ws"], lambda: rules.rule_new(rin(a["pid"], [{"type": "add_to_dataset", "dataset_id": ds}], name="good"))))["id"]
        await run(obs, a["pid"], start=NOW() + dt.timedelta(seconds=2))
        await rules.apply_rules(project_ids=[a["pid"]])
        assert await n_ex(ds) == 1 and (await rule_row(bad))["last_error"] and (await logs(bad))[0]["status"] == "error" and (await rule_row(good))["last_error"] is None
    world(go)

def test_spend_cap_stops_the_judge_but_not_the_other_actions(env):
    calls = []; fake_judge(env, calls, boom=deepagent_api.gw.SpendCapReached("hết hạn mức"))
    async def go(A, admin, W):
        a = W["a"]; ev, qu = await mk_ev(a["ws"], f"t12-e-{W['tag']}"), await mk_q(a["ws"], f"t12-q-{W['tag']}")
        rid = (await as_user(admin, a["ws"], lambda: rules.rule_new(rin(a["pid"], [{"type": "run_evaluator", "evaluator_id": ev}, {"type": "add_to_queue", "queue_id": qu}]))))["id"]
        st = NOW() + dt.timedelta(seconds=2)
        for i in range(3): await run(obs, a["pid"], start=st + dt.timedelta(seconds=i))
        await rules.apply_rules(project_ids=[a["pid"]])
        assert len(calls) == 1 and await n_q(qu) == 3                                       # one refused call, then no more judge calls in this batch
        assert [x["status"] for x in await logs(rid, action="run_evaluator")] == ["skipped"] * 3 and [x["status"] for x in await logs(rid, action="add_to_queue")] == ["ok"] * 3
        assert (await sysq(A, "SELECT count(*) n FROM feedback WHERE project_id=%s", (a["pid"],), one=True))["n"] == 0
    world(go)

# ---------------------------------------------------------------- late feedback, re-enable, sampling, retention
def test_late_human_score_applies_once_and_keeps_the_trace(env):
    async def go(A, admin, W):
        a = W["a"]; ds = await mk_ds(a["ws"], f"t12-l-{W['tag']}")
        rid = (await as_user(admin, a["ws"], lambda: rules.rule_new(rin(a["pid"], [{"type": "add_to_dataset", "dataset_id": ds}], filter="gte(feedback.thumb,1)"))))["id"]
        r1, t1 = await run(obs, a["pid"], start=NOW() + dt.timedelta(seconds=2)); r2, _ = await run(obs, a["pid"], start=NOW() + dt.timedelta(seconds=3))
        await rules.apply_rules(project_ids=[a["pid"]]); assert await n_ex(ds) == 0                         # no score yet -> nothing
        await as_user(admin, a["ws"], lambda: obs.add_feedback(obs.FbIn(run_id=r1, key="thumb", score=1.0), "human", by="t"))   # the score arrives late
        await as_user(admin, a["ws"], lambda: obs.add_feedback(obs.FbIn(run_id=r2, key="thumb", score=0.0), "human", by="t"))   # a score that does not match
        await rules.apply_rules(project_ids=[a["pid"]]); assert await n_ex(ds) == 1
        await as_user(admin, a["ws"], lambda: obs.add_feedback(obs.FbIn(run_id=r1, key="thumb", score=1.0, comment="again"), "human", by="t"))
        await rules.apply_rules(project_ids=[a["pid"]]); await rules.apply_rules(project_ids=[a["pid"]]); assert await n_ex(ds) == 1   # once per (rule, run)
        assert (await sysq(A, "SELECT count(*) n FROM automation_applied WHERE rule_id=%s", (rid,), one=True))["n"] == 1
        await as_user(admin, a["ws"], lambda: obs.add_feedback(obs.FbIn(run_id=r2, key="thumb", score=1.0), "human", by="t"))   # r2 is corrected to a match later
        await rules.apply_rules(project_ids=[a["pid"]]); assert await n_ex(ds) == 2
        assert (await sysq(A, "SELECT reason FROM trace_retention WHERE trace_id=%s", (t1,), one=True))["reason"] == "feedback"   # 12.18: the human score also kept the trace
    world(go)

def test_extend_retention_action_keeps_the_trace_past_30_days(env):
    async def go(A, admin, W):
        a = W["a"]
        rid = (await as_user(admin, a["ws"], lambda: rules.rule_new(rin(a["pid"], [{"type": "extend_retention"}], sampling_rate=1))))["id"]
        r1, t1 = await run(obs, a["pid"], start=NOW() + dt.timedelta(seconds=2)); r2, t2 = await run(obs, a["pid"], start=NOW() + dt.timedelta(seconds=3))
        await sysq(A, "UPDATE automation_rules SET filter=%s WHERE id=%s", (f'eq(id,"{r1}")', rid))
        await rules.apply_rules(project_ids=[a["pid"]])
        assert (await sysq(A, "SELECT reason FROM trace_retention WHERE trace_id=%s", (t1,), one=True))["reason"] == "rule" and not await sysq(A, "SELECT 1 FROM trace_retention WHERE trace_id=%s", (t2,))
        await obs.retention(now=NOW() + dt.timedelta(days=40), project_ids=[a["pid"]], only_partitions=())
        assert await sysq(A, "SELECT 1 FROM tr_runs WHERE id=%s", (r1,)) and not await sysq(A, "SELECT 1 FROM tr_runs WHERE id=%s", (r2,))   # kept vs expired
    world(go)

def test_disabled_then_enabled_rule_does_not_process_the_gap(env):
    async def go(A, admin, W):
        a = W["a"]; ds = await mk_ds(a["ws"], f"t12-g-{W['tag']}"); T0 = NOW()
        rid = (await as_user(admin, a["ws"], lambda: rules.rule_new(rin(a["pid"], [{"type": "add_to_dataset", "dataset_id": ds}]))))["id"]
        r1, _ = await run(obs, a["pid"], start=T0 + MIN(1)); await rules.apply_rules(project_ids=[a["pid"]], now=T0 + MIN(2)); assert await n_ex(ds) == 1
        await as_user(admin, a["ws"], lambda: rules.rule_toggle(rid, False))
        await run(obs, a["pid"], start=T0 + MIN(3)); await run(obs, a["pid"], start=T0 + MIN(4))      # the gap
        await rules.apply_rules(project_ids=[a["pid"]], now=T0 + MIN(5)); assert await n_ex(ds) == 1   # disabled: nothing
        env.setattr(rules, "_now", lambda: T0 + MIN(10))
        await as_user(admin, a["ws"], lambda: rules.rule_toggle(rid, True))                           # switched on at +10 min: cursors jump there
        r4, _ = await run(obs, a["pid"], start=T0 + MIN(11)); await rules.apply_rules(project_ids=[a["pid"]], now=T0 + MIN(12))
        assert await n_ex(ds) == 2
        assert [x["source_run_id"] for x in await sysq(A, "SELECT source_run_id FROM dataset_examples WHERE dataset_id=%s ORDER BY id", (ds,))] == [uuid.UUID(r1), uuid.UUID(r4)]
    world(go)

def test_sampling_is_stable_and_zero_applies_to_nothing(env):
    async def go(A, admin, W):
        a = W["a"]; d0, d5 = await mk_ds(a["ws"], f"t12-s0-{W['tag']}"), await mk_ds(a["ws"], f"t12-s5-{W['tag']}")
        await as_user(admin, a["ws"], lambda: rules.rule_new(rin(a["pid"], [{"type": "add_to_dataset", "dataset_id": d0}], sampling_rate=0)))
        await as_user(admin, a["ws"], lambda: rules.rule_new(rin(a["pid"], [{"type": "add_to_dataset", "dataset_id": d5}], sampling_rate=0.5)))
        ids = [(await run(obs, a["pid"], start=NOW() + dt.timedelta(seconds=2 + i)))[0] for i in range(40)]
        await rules.apply_rules(project_ids=[a["pid"]])
        want = sum(1 for i in ids if rules._sampled(i, 0.5)); assert await n_ex(d0) == 0 and await n_ex(d5) == want and 0 < want < 40
    world(go)

# ---------------------------------------------------------------- idle conversations
async def turns(obs_, pid, tid, start, n=3):
    out = []
    for i in range(n):
        out.append((await run(obs_, pid, start=start + MIN(i), ms=1000, thread_id=tid, inputs={"message": f"câu {i}"}, outputs={"answer": f"đáp {i}"}))[0])
    return out

def test_idle_thread_becomes_one_example_and_the_listener_is_called_once(env):
    fake_judge(env); got = []
    async def listener(events): got.extend(events)
    async def go(A, admin, W):
        a, b = W["a"], W["b"]; ds, db = await mk_ds(a["ws"], f"t12-ta-{W['tag']}"), await mk_ds(b["ws"], f"t12-tb-{W['tag']}"); ev = await mk_ev(a["ws"], f"t12-te-{W['tag']}")
        T0 = NOW()
        rid = (await as_user(admin, a["ws"], lambda: rules.rule_new(rin(a["pid"], [{"type": "add_to_dataset", "dataset_id": ds}, {"type": "evaluate_thread", "evaluator_id": ev}], item_type="threads"))))["id"]
        await as_user(admin, b["ws"], lambda: rules.rule_new(rin(b["pid"], [{"type": "add_to_dataset", "dataset_id": db}], item_type="threads")))
        rules.register_thread_idle(listener, "fake")
        pids = [a["pid"], b["pid"]]
        await rules.apply_rules(project_ids=pids, now=T0 + MIN(1))                       # first look: only sets the cursor, no catch-up
        ra = await turns(obs, a["pid"], "conv-1", T0 + MIN(2)); await turns(obs, b["pid"], "conv-1", T0 + MIN(2))   # the same thread name in another workspace
        await rules.apply_rules(project_ids=pids, now=T0 + MIN(8)); assert await n_ex(ds) == 0 and got == []                  # last turn ended at +4: only 4 min idle
        await rules.apply_rules(project_ids=pids, now=T0 + MIN(15))                      # 11 minutes idle -> fires
        assert await n_ex(ds) == 1 and await n_ex(db) == 1 and sorted((e["project_id"], e["thread_id"]) for e in got) == sorted([(a["pid"], "conv-1"), (b["pid"], "conv-1")])
        ex = await sysq(A, "SELECT source_run_id FROM dataset_examples WHERE dataset_id=%s", (ds,), one=True); assert str(ex["source_run_id"]) == ra[-1]   # the last root run of the thread
        fb = await sysq(A, "SELECT run_id, source, key FROM feedback WHERE project_id=%s", (a["pid"],)); assert [(str(x["run_id"]), x["source"], x["key"]) for x in fb] == [(ra[-1], "thread", "quality")]
        for t in (16, 40, 90): await rules.apply_rules(project_ids=pids, now=T0 + MIN(t))
        assert await n_ex(ds) == 1 and len(got) == 2                                    # exactly one example per thread, the listener called once per thread
        keys = [x["item_key"] for x in await sysq(A, "SELECT item_key FROM automation_applied WHERE rule_id=%s", (rid,))]; assert keys == ["thread:conv-1"]
        # new activity after firing = a new stretch: a second example, key thread:<id>:<last_end>
        rb = await turns(obs, a["pid"], "conv-1", T0 + MIN(100), n=1)
        await rules.apply_rules(project_ids=pids, now=T0 + MIN(105)); assert await n_ex(ds) == 1
        await rules.apply_rules(project_ids=pids, now=T0 + MIN(120)); assert await n_ex(ds) == 2 and len(got) == 3 and got[-1]["last_run_id"] == rb[0]
        await rules.apply_rules(project_ids=pids, now=T0 + MIN(200)); assert await n_ex(ds) == 2 and len(got) == 3
        keys = sorted(x["item_key"] for x in await sysq(A, "SELECT item_key FROM automation_applied WHERE rule_id=%s", (rid,))); assert len(keys) == 2 and keys[0] == "thread:conv-1" and keys[1].startswith("thread:conv-1:")
        # workspace A's rule never saw B's data and vice versa
        assert await n_ex(db) == 1 and (await sysq(A, "SELECT count(*) n FROM feedback WHERE project_id=%s", (b["pid"],), one=True))["n"] == 0
    world(go)

def test_idle_rule_ignores_old_activity_filter_and_demo(env):
    async def go(A, admin, W):
        a = W["a"]; ds = await mk_ds(a["ws"], f"t12-tf-{W['tag']}"); T0 = NOW()
        await as_user(admin, a["ws"], lambda: rules.rule_new(rin(a["pid"], [{"type": "add_to_dataset", "dataset_id": ds}], item_type="threads", filter='eq(name,"keep")')))
        await sysq(A, "UPDATE projects SET thread_idle_min=2 WHERE id=%s", (a["pid"],))
        await rules.apply_rules(project_ids=[a["pid"]], now=T0)
        await turns(obs, a["pid"], "skip-me", T0 + MIN(1), n=1)                                   # name 'r' does not match the filter
        await run(obs, a["pid"], start=T0 + MIN(1), name="keep", thread_id="keep-me")
        await run(obs, a["pid"], start=T0 - dt.timedelta(days=10), thread_id="ancient")              # older than 7 days (and before the cursor)
        await rules.apply_rules(project_ids=[a["pid"]], now=T0 + MIN(10))
        ex = await sysq(A, "SELECT r.thread_id FROM dataset_examples e JOIN tr_runs r ON r.id=e.source_run_id WHERE e.dataset_id=%s", (ds,)); assert [x["thread_id"] for x in ex] == ["keep-me"]
        # demo workspace: the trigger never fires
        env.setattr(deepagent_api.gw, "DEMO_WS", set(deepagent_api.gw.DEMO_WS) | {a["ws"]})
        await run(obs, a["pid"], start=T0 + MIN(20), name="keep", thread_id="demo-thread")
        await rules.apply_rules(project_ids=[a["pid"]], now=T0 + MIN(40)); assert await n_ex(ds) == 1
    world(go)

# ---------------------------------------------------------------- logs endpoint
def test_logs_endpoint_filters_pages_and_isolates(env):
    async def go(A, admin, W):
        a, b = W["a"], W["b"]
        rid = (await sysq(A, "INSERT INTO automation_rules(project_id,name,actions) VALUES(%s,'l','[]') RETURNING id", (a["pid"],), one=True))["id"]
        rid2 = (await sysq(A, "INSERT INTO automation_rules(project_id,name,actions) VALUES(%s,'l2','[]') RETURNING id", (b["pid"],), one=True))["id"]
        base = NOW()
        for i in range(7): await sysq(A, "INSERT INTO automation_log(rule_id,action,result,status,ts) VALUES(%s,'x',%s,%s,%s)", (rid, f"r{i}", "error" if i % 3 == 0 else "ok", base + MIN(i)))
        await sysq(A, "INSERT INTO automation_log(rule_id,action,result,status) VALUES(%s,'x','secret of B','ok')", (rid2,))
        mine = lambda fn, ws=a["ws"], **k: as_user(admin, ws, fn, **k)   # noqa: E731
        p1 = await mine(lambda: rules.rule_logs(rid, limit=3)); assert [x["result"] for x in p1["items"]] == ["r6", "r5", "r4"] and p1["next_cursor"]
        p2 = await mine(lambda: rules.rule_logs(rid, limit=3, cursor=p1["next_cursor"])); assert [x["result"] for x in p2["items"]] == ["r3", "r2", "r1"]
        p3 = await mine(lambda: rules.rule_logs(rid, limit=3, cursor=p2["next_cursor"])); assert [x["result"] for x in p3["items"]] == ["r0"] and p3["next_cursor"] is None
        assert {x["result"] for x in (await mine(lambda: rules.rule_logs(rid, status="error")))["items"]} == {"r0", "r3", "r6"}
        w = await mine(lambda: rules.rule_logs(rid, from_=(base + MIN(2)).isoformat(), to=(base + MIN(5)).isoformat())); assert [x["result"] for x in w["items"]] == ["r4", "r3", "r2"]
        for kw in ({"status": "bogus"}, {"from_": "yesterday"}):
            with pytest.raises(HTTPException) as e: await mine(lambda kw=kw: rules.rule_logs(rid, **kw))
            assert e.value.status_code == 422
        with pytest.raises(HTTPException) as e: await mine(lambda: rules.rule_logs(rid2))        # B's rule seen from A
        assert e.value.status_code == 404
        assert await mine(lambda: rules.rule_logs(rid, limit=5), demo=True, role="viewer") == {"items": [], "next_cursor": None}
        # row-level security: a direct query from A's session cannot read B's log lines
        rows = await mine(lambda: A.q("SELECT result FROM automation_log"))
        assert "secret of B" not in {x["result"] for x in rows}
    world(go)

# ---------------------------------------------------------------- the old route + the real middleware, on our own server
PORT = 8061; B = f"http://127.0.0.1:{PORT}"; ADMIN = ("moza", "phase0-local"); PW = "phase0-gd1212-local"; SUF = uuid.uuid4().hex[:6]

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
        else: raise AssertionError("own server on :8061 did not start")
        yield p
    finally:
        p.terminate()                                                                     # this PID only: never pkill run_local.py
        try: p.wait(timeout=20)
        except subprocess.TimeoutExpired: p.kill()
        log.close()

def test_http_roles_isolation_and_old_responses(srv):
    cl = {}
    with httpx.Client(base_url=B, auth=ADMIN, timeout=60) as a:
        w1 = a.post("/api/admin/workspaces", json={"name": f"rl-w1-{SUF}"}).json()["id"]; w2 = a.post("/api/admin/workspaces", json={"name": f"rl-w2-{SUF}"}).json()["id"]
        for k, role, ws in (("admin", "admin", w1), ("editor", "editor", w1), ("viewer", "viewer", w1), ("other", "admin", w2)):
            r = a.post("/api/admin/users", json={"username": f"rl_{k}_{SUF}", "password": PW, "role": role, "workspace_id": ws}); assert r.status_code == 200, r.text
            cl[k] = httpx.Client(base_url=B, auth=(f"rl_{k}_{SUF}", PW), timeout=60)
        r = a.post("/api/admin/users", json={"username": f"rl_demo_{SUF}", "password": PW, "role": "viewer", "workspace_id": w1, "demo": True})
        cl["demo"] = httpx.Client(base_url=B, auth=(f"rl_demo_{SUF}", PW), timeout=60) if r.status_code == 200 else None
    try:
        pid = cl["admin"].post("/api/obs/projects", json={"name": f"rl-p-{SUF}"}).json()["id"]
        ds = cl["admin"].post("/api/datasets", json={"name": f"rl-d-{SUF}"}).json()["id"]
        body = {"project_id": pid, "name": "http rule", "actions": [{"type": "add_to_dataset", "dataset_id": ds}]}
        assert httpx.post(B + "/api/obs/rules", json=body).status_code == 401                         # no credentials
        assert cl["viewer"].post("/api/obs/rules", json=body).status_code == 403
        if cl["demo"]: assert cl["demo"].post("/api/obs/rules", json=body).status_code == 403
        assert cl["other"].post("/api/obs/rules", json=body).status_code == 404                       # another workspace's project
        r = cl["editor"].post("/api/obs/rules", json=body); assert r.status_code == 200, r.text; rid = r.json()["id"]
        assert cl["other"].post("/api/obs/rules", json={**body, "project_id": cl["other"].post("/api/obs/projects", json={"name": f"rl-q-{SUF}"}).json()["id"]}).status_code == 404   # my dataset id in their rule
        assert cl["viewer"].patch(f"/api/obs/rules/{rid}", params={"enabled": "false"}).status_code == 403 and cl["viewer"].delete(f"/api/obs/rules/{rid}").status_code == 403
        assert cl["other"].patch(f"/api/obs/rules/{rid}", params={"enabled": "false"}).status_code == 404 and cl["other"].delete(f"/api/obs/rules/{rid}").status_code == 404
        assert cl["other"].get(f"/api/obs/rules/{rid}/logs").status_code == 404 and cl["other"].get("/api/obs/rules").json() == []
        assert [x["id"] for x in cl["viewer"].get("/api/obs/rules").json()] == [rid]                  # old list route: same shape plus the new columns
        assert cl["viewer"].get(f"/api/obs/rules/{rid}/logs").json() == {"items": [], "next_cursor": None}
        assert cl["editor"].patch(f"/api/obs/rules/{rid}", params={"enabled": "false"}).json() == {"ok": True} and cl["editor"].delete(f"/api/obs/rules/{rid}").json() == {"ok": True}
    finally:
        for c in cl.values():
            if c: c.close()
        with psycopg.connect(PG, autocommit=True) as c:
            ws = [r[0] for r in c.execute("SELECT id FROM workspaces WHERE name LIKE %s", (f"rl-w_-{SUF}",)).fetchall()]
            c.execute("DELETE FROM datasets WHERE workspace_id = ANY(%s)", (ws,)); c.execute("DELETE FROM projects WHERE workspace_id = ANY(%s)", (ws,))
            c.execute("DELETE FROM users WHERE username LIKE %s", (f"rl\\_%\\_{SUF}",))
            try: c.execute("DELETE FROM workspaces WHERE id = ANY(%s)", (ws,))
            except psycopg.Error: pass
