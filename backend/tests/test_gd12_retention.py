"""GĐ12.18 retention tiers (base 30 / extended 180 days) and the upgrade of rated traces. In-process on the local DB (private pool under the app's RLS role,
two throw-away workspaces; real projects are never touched: every retention() call is scoped with project_ids and only_partitions=()). Fake clock = the `now`
argument of obs.retention(). Negative tests: another workspace, viewer / editor / demo refused, apply_to_existing without confirm, evaluator opt-out, demo workspace."""
import asyncio, datetime as dt, os, sys, uuid
import psycopg, pytest
from psycopg.types.json import Jsonb
from fastapi import HTTPException
from gd12_world import NOW, PG, as_user, run, sysq, with_world

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import deepagent_api  # noqa: E402,F401  (import order: deepagent_api first, obs <-> deepagent_api cycle)
import obs  # noqa: E402
import migrations as M  # noqa: E402

D = lambda n: dt.timedelta(days=n)   # noqa: E731
def fb(rid, **kw): return obs.FbIn(run_id=str(rid), key="thumb", score=1.0, **kw)

async def ttl(A, rid): return (await sysq(A, "SELECT ttl_days FROM tr_runs WHERE id=%s", (rid,), one=True))["ttl_days"]
async def exists(A, rid): return bool(await sysq(A, "SELECT 1 FROM tr_runs WHERE id=%s", (rid,), one=True))
async def ret(pid, now=None, **kw): return await obs.retention(now=now, project_ids=[pid], only_partitions=(), **kw)

# ---------------------------------------------------------------- migration
def test_migration_0110_up_down_and_existing_projects_become_custom():
    sch = "t1218_" + uuid.uuid4().hex[:8]
    mod = {v: m for v, m, _ in M._load()}["0110"]
    async def go():
        with psycopg.connect(PG, autocommit=True) as c: c.execute(f'CREATE SCHEMA "{sch}"')
        try:
            async with await psycopg.AsyncConnection.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
                await c.execute("CREATE TABLE workspaces(id SERIAL PRIMARY KEY, name TEXT)")
                await c.execute("CREATE TABLE projects(id SERIAL PRIMARY KEY, name TEXT, retention_days INT NOT NULL DEFAULT 30)")
                await c.execute("INSERT INTO projects(name, retention_days) VALUES('old7', 7), ('old90', 90)")
                q = M._q(c)
                assert any("2 existing projects" in s for s in await mod.plan(q))
                await mod.up(q)
                rows = await (await c.execute("SELECT name, retention_tier, retention_days, legacy_ttl_days FROM projects ORDER BY name")).fetchall()
                assert rows == [("old7", "custom", 7, 7), ("old90", "custom", 90, 90)]   # nothing changes for old projects
                await c.execute("INSERT INTO workspaces(name) VALUES('w')")
                assert await (await c.execute("SELECT retention_default, retention_base_days, retention_extended_days FROM workspaces")).fetchone() == ("base", 30, 180)
                await c.execute("INSERT INTO projects(name) VALUES('new')")   # a project made after the migration follows the workspace default
                assert (await (await c.execute("SELECT retention_tier, legacy_ttl_days FROM projects WHERE name='new'")).fetchone()) == (None, None)
                with pytest.raises(psycopg.errors.CheckViolation): await c.execute("UPDATE workspaces SET retention_default='forever'")
                await mod.up(q)   # idempotent (IF NOT EXISTS); the second UPDATE must not touch the new project
                assert (await (await c.execute("SELECT retention_tier FROM projects WHERE name='new'")).fetchone())[0] is None
                await mod.down(q)
                assert await (await c.execute("SELECT to_regclass('trace_retention')")).fetchone() == (None,)
                cols = {r[0] for r in await (await c.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s", (sch,))).fetchall()}
                assert not cols & {"retention_tier", "legacy_ttl_days", "retention_default", "retention_base_days", "retention_extended_days"} and "retention_days" in cols
        finally:
            with psycopg.connect(PG, autocommit=True) as c: c.execute(f'DROP SCHEMA "{sch}" CASCADE')
    asyncio.run(go())

# ---------------------------------------------------------------- the main promise
def test_rated_trace_survives_30_days_unrated_is_deleted_and_expires_at_180():
    import obs
    async def go(A, admin, W):
        a = W["a"]; start = NOW() - D(40)
        rated, tr1 = await run(obs, a["pid"], start=start); child, _ = await run(obs, a["pid"], trace=tr1, parent=rated, start=start + D(0) + dt.timedelta(seconds=1))
        plain, _ = await run(obs, a["pid"], start=start)
        young, _ = await run(obs, a["pid"], start=NOW() - D(2))
        assert {await ttl(A, x) for x in (rated, child, plain, young)} == {30}            # workspace default 'base'
        await as_user(admin, a["ws"], lambda: obs.add_feedback(fb(rated), "human", by="t12"))   # human feedback on the root -> the WHOLE trace is kept
        k = await sysq(A, "SELECT tier, reason, keep_until FROM trace_retention WHERE trace_id=%s", (tr1,), one=True)
        assert k["tier"] == "extended" and k["reason"] == "feedback" and abs((k["keep_until"] - (start + D(180))).total_seconds()) < 2
        await sysq(A, "INSERT INTO feedback(run_id,trace_id,project_id,key,score) VALUES(%s,%s,%s,'old',1)", (plain, plain, a["pid"]))   # feedback of an unrated trace goes with the run
        r = await ret(a["pid"])
        assert r["runs"] == 1 and r["feedback"] == 1 and not await exists(A, plain)               # unrated + expired: deleted (with its feedback)
        assert await exists(A, rated) and await exists(A, child) and await exists(A, young)       # rated trace (both runs) and the young one stay
        assert (await ret(a["pid"], NOW() + D(100)))["runs"] == 1 and not await exists(A, young) and await exists(A, rated)   # fake clock +100 d: young one expires, rated stays
        r = await ret(a["pid"], NOW() + D(120))                                                   # +120 d: still kept (start 40 d ago + 180 = day 140)
        assert r["runs"] == 0 and await exists(A, rated)
        r = await ret(a["pid"], NOW() + D(200))                                                   # +200 d: past the extended period
        assert r["runs"] == 2 and not await exists(A, rated) and not await exists(A, child)
        assert not await sysq(A, "SELECT 1 FROM trace_retention WHERE trace_id=%s", (tr1,))        # its keep row is cleaned up too
    with_world(go)

def test_changing_the_policy_never_deletes_or_shortens_old_traces():
    async def go(A, admin, W):
        a = W["a"]; start = NOW() - D(10)
        old, _ = await run(obs, a["pid"], start=start)
        legacy, _ = await run(obs, a["pid"], start=start); await sysq(A, "UPDATE tr_runs SET ttl_days=NULL WHERE id=%s", (legacy,))   # a run written before 12.18
        await sysq(A, "UPDATE projects SET legacy_ttl_days=30 WHERE id=%s", (a["pid"],))
        out = await as_user(admin, a["ws"], lambda: obs.retention_put(obs.RetentionIn(base_days=5)))   # workspace base 30 -> 5 days, NEW runs only
        assert out["applies_to"] == "new runs only" and out["base_days"] == 5
        new, _ = await run(obs, a["pid"], start=start)
        assert await ttl(A, old) == 30 and await ttl(A, new) == 5 and await ttl(A, legacy) is None
        r = await ret(a["pid"])
        assert await exists(A, old) and await exists(A, legacy) and not await exists(A, new) and r["runs"] == 1   # only the NEW run (ttl 5) expired
        # project tier base -> extended -> back: old rows keep the ttl they were written with
        await as_user(admin, a["ws"], lambda: obs.project_upd(a["pid"], obs.ProjectUpd(retention_tier="extended")))
        assert await ttl(A, old) == 30
        p = await as_user(admin, a["ws"], lambda: obs.projects())
        me = next(x for x in p if x["id"] == a["pid"]); assert me["retention"] == {"tier": "extended", "inherited": False, "days": 180} and me["traces_extended"] == 0
    with_world(go)

def test_apply_to_existing_needs_confirm_dry_run_changes_nothing():
    async def go(A, admin, W):
        a = W["a"]; start = NOW() - D(40)
        kept, tk = await run(obs, a["pid"], start=start); await as_user(admin, a["ws"], lambda: obs.add_feedback(fb(kept), "human", by="t12"))
        plain, _ = await run(obs, a["pid"], start=start); fresh, _ = await run(obs, a["pid"], start=NOW() - D(1))
        before = await sysq(A, "SELECT retention_tier, retention_days FROM projects WHERE id=%s", (a["pid"],), one=True)
        dry = await as_user(admin, a["ws"], lambda: obs.project_upd(a["pid"], obs.ProjectUpd(retention_tier="custom", retention_days=7, apply_to_existing=True)))
        assert dry["dry_run"] is True and dry["days"] == 7 and dry["runs_to_change"] == 3 and dry["runs_deleted_by_next_cleanup"] == 1 and dry["kept_traces_protected"] == 1
        assert await sysq(A, "SELECT retention_tier, retention_days FROM projects WHERE id=%s", (a["pid"],), one=True) == before     # policy NOT saved
        assert {await ttl(A, x) for x in (kept, plain, fresh)} == {30} and (await ret(a["pid"]))["runs"] == 1                           # (the 40-day-old plain one is expired by the OLD 30-day rule anyway)
        await run(obs, a["pid"], start=NOW() - D(20), rid=(r20 := str(uuid.uuid4())))              # 20 days old, ttl 30: survives today ...
        assert await exists(A, r20)
        ok = await as_user(admin, a["ws"], lambda: obs.project_upd(a["pid"], obs.ProjectUpd(retention_tier="custom", retention_days=7, apply_to_existing=True, confirm=True)))
        assert ok["dry_run"] is False and ok["finished"] and ok["applies_to"] == "new and existing runs" and await ttl(A, r20) == 7 and await ttl(A, kept) == 7
        r = await ret(a["pid"])
        assert not await exists(A, r20) and await exists(A, kept) and await exists(A, fresh)           # ... but not after the confirmed change; the rated trace still stays
        # the same switch on the workspace policy: dry run first
        dry = await as_user(admin, a["ws"], lambda: obs.retention_put(obs.RetentionIn(default="extended", apply_to_existing=True)))
        assert dry["dry_run"] is True and dry["projects"] == []                                    # the only project is 'custom': not affected
        w = await sysq(A, "SELECT retention_default FROM workspaces WHERE id=%s", (a["ws"],), one=True); assert w["retention_default"] == "base"
    with_world(go)

# ---------------------------------------------------------------- partitions
def test_a_partition_with_a_kept_trace_is_not_dropped_until_it_is_empty():
    rel = "tr_runs_200101"
    async def go(A, admin, W):
        a = W["a"]
        assert not await sysq(A, "SELECT 1 FROM pg_class WHERE relname=%s", (rel,))              # never touch a real partition
        await sysq(A, f"CREATE TABLE {rel} (LIKE tr_runs INCLUDING DEFAULTS)")
        await sysq(A, f"ALTER TABLE tr_runs ATTACH PARTITION {rel} FOR VALUES FROM ('2001-01-01') TO ('2001-02-01')")
        try:
            start = dt.datetime(2001, 1, 15, tzinfo=dt.timezone.utc)
            kept, tk = await run(obs, a["pid"], start=start); kid, _ = await run(obs, a["pid"], trace=tk, parent=kept, start=start + dt.timedelta(seconds=2))
            plain, _ = await run(obs, a["pid"], start=start); plain2, _ = await run(obs, a["pid"], start=start)
            assert (await sysq(A, f"SELECT count(*) n FROM {rel}", one=True))["n"] == 4         # all four are really in the partition
            await as_user(admin, a["ws"], lambda: obs.add_feedback(fb(kept), "human", by="t12"))
            fake = dt.datetime(2001, 4, 1, tzinfo=dt.timezone.utc)
            r = await obs.retention(now=fake, project_ids=[a["pid"]], only_partitions=[rel])
            assert r["runs"] == 2 and r["partitions"] == [] and await sysq(A, "SELECT 1 FROM pg_class WHERE relname=%s", (rel,))   # the 2 unrated runs are gone; the partition stays
            assert await exists(A, kept) and await exists(A, kid)                               # NO run of the kept trace was dropped with the partition
            d = await obs.retention(now=fake, project_ids=[a["pid"]], only_partitions=[rel], dry_run=True)
            assert d["dry_run"] and d["partitions"] == [] and d["runs"] == 0
            late = dt.datetime(2002, 1, 1, tzinfo=dt.timezone.utc)                              # keep_until (start + 180 d = July 2001) has passed
            await sysq(A, "INSERT INTO feedback(run_id,trace_id,project_id,key,score) VALUES(%s,%s,%s,'x',1)", (kid, tk, a["pid"]))
            d = await obs.retention(now=late, project_ids=[a["pid"]], only_partitions=[rel], dry_run=True)
            assert d["dry_run"] and d["runs"] == 2 and d["partitions"] == [rel]                  # preview only ...
            assert await exists(A, kept) and await sysq(A, "SELECT 1 FROM pg_class WHERE relname=%s", (rel,))   # ... nothing deleted
            r = await obs.retention(now=late, project_ids=[a["pid"]], only_partitions=[rel])    # real run: the 2 runs go, the partition is empty and fully expired -> dropped
            assert r["runs"] == 2 and r["feedback"] == 2 and r["partitions"] == [rel] and not await sysq(A, "SELECT 1 FROM pg_class WHERE relname=%s", (rel,))
            assert not await sysq(A, "SELECT 1 FROM feedback WHERE run_id = ANY(%s)", ([kept, kid],))   # feedback left no orphan
        finally:
            if await sysq(A, "SELECT 1 FROM pg_class WHERE relname=%s", (rel,)): await sysq(A, f"DROP TABLE {rel}")
    with_world(go)

def test_a_scoped_call_never_drops_a_partition_holding_other_projects_data():
    rel = "tr_runs_200102"
    async def go(A, admin, W):
        a, b = W["a"], W["b"]
        await sysq(A, f"CREATE TABLE {rel} (LIKE tr_runs INCLUDING DEFAULTS)")
        await sysq(A, f"ALTER TABLE tr_runs ATTACH PARTITION {rel} FOR VALUES FROM ('2001-02-01') TO ('2001-03-01')")
        try:
            s = dt.datetime(2001, 2, 10, tzinfo=dt.timezone.utc)
            ra, _ = await run(obs, a["pid"], start=s); rb, _ = await run(obs, b["pid"], start=s)
            await sysq(A, "UPDATE projects SET retention_tier='custom', retention_days=3650 WHERE id=%s", (b["pid"],)); await sysq(A, "UPDATE tr_runs SET ttl_days=3650 WHERE id=%s", (rb,))
            late = dt.datetime(2002, 6, 1, tzinfo=dt.timezone.utc)
            r = await obs.retention(now=late, project_ids=[a["pid"]], only_partitions=[rel])
            assert r["runs"] == 1 and r["partitions"] == [] and not await exists(A, ra) and await exists(A, rb)   # workspace B's long-lived run is untouched
            r = await obs.retention(now=late, project_ids=[a["pid"], b["pid"]], only_partitions=[rel])   # both projects: B's run (3650 d) is still live -> the partition stays
            assert r["runs"] == 0 and r["partitions"] == [] and await exists(A, rb)
        finally:
            await sysq(A, f"DROP TABLE IF EXISTS {rel}")
    with_world(go)

# ---------------------------------------------------------------- batches, dry run, attachments
def test_batches_are_bounded_and_dry_run_deletes_nothing(monkeypatch):
    async def go(A, admin, W):
        a = W["a"]; ids = [(await run(obs, a["pid"], start=NOW() - D(60)))[0] for _ in range(7)]
        for i in ids[:2]: await sysq(A, "INSERT INTO run_attachments(run_id,name,data) VALUES(%s,'f',%s)", (i, b"x"))
        monkeypatch.setattr(obs, "RET_BATCH", 2); monkeypatch.setattr(obs, "RET_MAX_BATCHES", 2)
        d = await ret(a["pid"], dry_run=True)
        assert d["dry_run"] and d["runs"] == 7 and (await sysq(A, "SELECT count(*) n FROM tr_runs WHERE project_id=%s", (a["pid"],), one=True))["n"] == 7
        assert (await ret(a["pid"]))["runs"] == 4                                              # 2 batches x 2 rows, never more per tick
        assert (await ret(a["pid"]))["runs"] == 3                                              # 2 + 1
        assert (await sysq(A, "SELECT count(*) n FROM tr_runs WHERE project_id=%s", (a["pid"],), one=True))["n"] == 0
        assert not await sysq(A, "SELECT 1 FROM run_attachments WHERE run_id = ANY(%s)", (ids,))   # attachments go with their run
    with_world(go)

def test_a_scoped_retention_run_leaves_other_workspaces_alone():
    async def go(A, admin, W):
        a, b = W["a"], W["b"]
        ra, _ = await run(obs, a["pid"], start=NOW() - D(60)); rb, _ = await run(obs, b["pid"], start=NOW() - D(60))
        await ret(a["pid"])                                                                      # scoped to A
        assert not await exists(A, ra) and await exists(A, rb)                                   # B's expired run is not A's business
        assert (await ret(b["pid"]))["runs"] == 1 and not await exists(A, rb)
    with_world(go)

# ---------------------------------------------------------------- extend: idempotent, who may, other workspaces
def test_extend_is_idempotent_keeps_first_reason_and_validates():
    async def go(A, admin, W):
        a = W["a"]; start = NOW() - D(5); rid, tr = await run(obs, a["pid"], start=start)
        r1 = await as_user(admin, a["ws"], lambda: obs.extend_trace(tr, "manual", by="u1")); assert r1["created"] is True
        r2 = await as_user(admin, a["ws"], lambda: obs.extend_trace(tr, "rule", by="u2")); assert r2["created"] is False and r2["keep_until"] == r1["keep_until"]
        row = await sysq(A, "SELECT reason, set_by FROM trace_retention WHERE trace_id=%s", (tr,)); assert len(row) == 1 and row[0]["reason"] == "manual" and row[0]["set_by"] == "u1"
        with pytest.raises(ValueError): await obs.extend_trace(tr, "because", by="x")
        assert await as_user(admin, a["ws"], lambda: obs.extend_trace(str(uuid.uuid4()), "manual")) is None   # unknown trace
        await sysq(A, "UPDATE workspaces SET retention_extended_days=400 WHERE id=%s", (a["ws"],))
        r3 = await as_user(admin, a["ws"], lambda: obs.extend_trace(tr, "manual")); assert abs((r3["keep_until"] - (start + D(400))).total_seconds()) < 2   # keep_until only moves later
    with_world(go)

def test_feedback_sources_that_extend_and_the_ones_that_do_not():
    async def go(A, admin, W):
        a = W["a"]; got = {}
        for src in ("human", "sdk", "online", "experiment", "thread", "external", "composite"):
            rid, tr = await run(obs, a["pid"]); await as_user(admin, a["ws"], lambda: obs.add_feedback(fb(rid), src, by="t12"))
            row = await sysq(A, "SELECT reason FROM trace_retention WHERE trace_id=%s", (tr,), one=True); got[src] = row and row["reason"]
        assert got == {"human": "feedback", "sdk": "feedback", "online": "evaluator", "experiment": None, "thread": None, "external": None, "composite": None}
        rid, tr = await run(obs, a["pid"]); await as_user(admin, a["ws"], lambda: obs.add_feedback(fb(rid), "human", by="t12", extend=False))
        assert not await sysq(A, "SELECT 1 FROM trace_retention WHERE trace_id=%s", (tr,))
    with_world(go)

def test_evaluator_option_extend_retention_false_is_honoured(monkeypatch):
    import evals
    async def go(A, admin, W):
        a = W["a"]
        async def fake_eval(ev, view, example): return [{"key": "quality", "score": 0.9}]
        monkeypatch.setattr(evals, "run_evaluator", fake_eval)
        ids = {}
        for flag in (True, False):
            e = await sysq(A, "INSERT INTO evaluators(name,kind,config,workspace_id) VALUES(%s,'llm',%s,%s) RETURNING id", (f"t12-ev{flag}-{W['tag']}", Jsonb({} if flag else {"extend_retention": False}), a["ws"]), one=True)
            rid, tr = await run(obs, a["pid"])
            await as_user(admin, a["ws"], lambda: evals.run_evaluator_on_run(e["id"], rid, source="online"))
            ids[flag] = bool(await sysq(A, "SELECT 1 FROM trace_retention WHERE trace_id=%s", (tr,)))
            assert (await sysq(A, "SELECT count(*) n FROM feedback WHERE run_id=%s", (rid,), one=True))["n"] == 1   # the score itself is stored either way
        assert ids == {True: True, False: False}
    with_world(go)

def test_other_workspace_cannot_extend_read_or_change_retention():
    async def go(A, admin, W):
        a, b = W["a"], W["b"]; start = NOW() - D(3); rid, tr = await run(obs, a["pid"], start=start)
        await as_user(admin, a["ws"], lambda: obs.extend_trace(tr, "manual", by="t12"))
        # B (admin of workspace B) ...
        assert await as_user(admin, b["ws"], lambda: obs.extend_trace(tr, "manual")) is None                                   # cannot see A's trace
        with pytest.raises(HTTPException) as e: await as_user(admin, b["ws"], lambda: obs.trace_extend(tr))
        assert e.value.status_code == 404
        assert await as_user(admin, b["ws"], lambda: A.q("SELECT 1 FROM trace_retention WHERE trace_id=%s", (tr,))) == []      # cannot read A's keep rows
        assert (await as_user(admin, b["ws"], lambda: obs.retention_get()))["kept_traces"] == 0
        assert (await as_user(admin, a["ws"], lambda: obs.retention_get()))["kept_traces"] == 1
        with pytest.raises(HTTPException) as e: await as_user(admin, b["ws"], lambda: obs.project_upd(a["pid"], obs.ProjectUpd(retention_days=1)))
        assert e.value.status_code == 404                                                                                      # cannot change A's project
        assert (await sysq(A, "SELECT retention_days FROM projects WHERE id=%s", (a["pid"],), one=True))["retention_days"] == 30
        with pytest.raises(psycopg.Error): await as_user(admin, b["ws"], lambda: A.q("INSERT INTO trace_retention(trace_id,project_id,keep_until,reason) VALUES(%s,%s,now()+interval '1 day','manual')", (str(uuid.uuid4()), a["pid"])))   # RLS WITH CHECK
        await as_user(admin, b["ws"], lambda: obs.retention_put(obs.RetentionIn(base_days=3)))                                  # B's own policy ...
        assert (await sysq(A, "SELECT retention_base_days FROM workspaces WHERE id=%s", (a["ws"],), one=True))["retention_base_days"] == 30   # ... never A's
        # a retention run for B's project does not touch A's expired run
        old, _ = await run(obs, a["pid"], start=NOW() - D(90)); await ret(b["pid"]); assert await exists(A, old)
    with_world(go)

def test_roles_demo_and_demo_workspace_are_refused():
    import gateway
    async def go(A, admin, W):
        a = W["a"]; rid, tr = await run(obs, a["pid"])
        put = lambda: obs.retention_put(obs.RetentionIn(base_days=10))   # noqa: E731
        patch = lambda: obs.project_upd(a["pid"], obs.ProjectUpd(retention_days=5))   # noqa: E731
        for role in ("viewer", "editor"):                                                          # not admin: no policy change
            for fn in (put, patch, lambda: obs.project_new(obs.ProjectIn(name="x" + W["tag"], retention_days=9))):
                with pytest.raises(HTTPException) as e: await as_user(admin, a["ws"], fn, role=role)
                assert e.value.status_code == 403
        with pytest.raises(HTTPException) as e: await as_user(admin, a["ws"], lambda: obs.trace_extend(tr), role="viewer")
        assert e.value.status_code == 403
        assert (await as_user(admin, a["ws"], lambda: obs.trace_extend(tr), role="editor"))["created"] is True                  # an editor may keep a trace
        with pytest.raises(HTTPException) as e: await as_user(admin, a["ws"], put, demo=True)                                  # demo account (even with the admin role)
        assert e.value.status_code == 403
        with pytest.raises(HTTPException) as e: await as_user(admin, a["ws"], lambda: obs.trace_extend(tr), demo=True)
        assert e.value.status_code == 403
        with pytest.raises(HTTPException) as e: await obs.trace_extend(tr)
        assert e.value.status_code == 403                  # no signed-in user at all
        assert (await sysq(A, "SELECT retention_base_days FROM workspaces WHERE id=%s", (a["ws"],), one=True))["retention_base_days"] == 30
        assert not await sysq(A, "SELECT 1 FROM projects WHERE name=%s", ("x" + W["tag"],))
        # the demo WORKSPACE: feedback does not keep traces, policy is read-only
        gateway.DEMO_WS.add(a["ws"])
        try:
            r2, t2 = await run(obs, a["pid"]); await as_user(admin, a["ws"], lambda: obs.add_feedback(fb(r2), "human", by="t12"))
            assert not await sysq(A, "SELECT 1 FROM trace_retention WHERE trace_id=%s", (t2,)) and await obs.extend_trace(t2, "manual") is None
            with pytest.raises(HTTPException) as e: await as_user(admin, a["ws"], put)
            assert e.value.status_code == 403
            with pytest.raises(HTTPException) as e: await as_user(admin, a["ws"], patch)
            assert e.value.status_code == 403
        finally: gateway.DEMO_WS.discard(a["ws"])
    with_world(go)

def test_validation_extended_not_shorter_than_base_and_tier_rules():
    async def go(A, admin, W):
        a = W["a"]
        with pytest.raises(HTTPException) as e: await as_user(admin, a["ws"], lambda: obs.retention_put(obs.RetentionIn(base_days=100, extended_days=50)))
        assert e.value.status_code == 422
        with pytest.raises(HTTPException) as e: await as_user(admin, a["ws"], lambda: obs.project_upd(a["pid"], obs.ProjectUpd(retention_tier="base", retention_days=3)))
        assert e.value.status_code == 422                                                       # days only with 'custom'
        for bad in ({"retention_tier": "forever"}, {"retention_days": 0}, {"retention_days": 99999}):
            with pytest.raises(ValueError): obs.ProjectUpd(**bad)
        out = await as_user(admin, a["ws"], lambda: obs.project_upd(a["pid"], obs.ProjectUpd(retention_days=45, description="d")))   # old UI call still works: -> custom 45 d
        assert out["tier"] == "custom" and out["days"] == 45
        run1, _ = await run(obs, a["pid"]); assert await ttl(A, run1) == 45
        out = await as_user(admin, a["ws"], lambda: obs.project_upd(a["pid"], obs.ProjectUpd(retention_tier="inherit")))
        assert out["tier"] is None and out["days"] == 30
        np = await as_user(admin, a["ws"], lambda: obs.project_new(obs.ProjectIn(name="n" + W["tag"])))                        # no retention fields: allowed for everyone, follows the default
        assert np["retention_tier"] is None
    with_world(go)

def test_a_child_run_takes_its_trace_roots_ttl():
    async def go(A, admin, W):
        a = W["a"]; root, tr = await run(obs, a["pid"])
        await as_user(admin, a["ws"], lambda: obs.retention_put(obs.RetentionIn(base_days=7)))          # policy changes while the trace is still running
        kid, _ = await run(obs, a["pid"], trace=tr, parent=root); other, _ = await run(obs, a["pid"])
        assert (await ttl(A, root), await ttl(A, kid), await ttl(A, other)) == (30, 30, 7)              # same trace = same ttl; a new trace gets the new policy
    with_world(go)

def test_a_new_run_in_each_tier_gets_that_tiers_days_and_the_limit_is_smallint_safe():
    async def go(A, admin, W):
        a = W["a"]
        await sysq(A, "UPDATE workspaces SET retention_extended_days=200 WHERE id=%s", (a["ws"],))
        for tier, days in (("base", 30), ("extended", 200), (None, 30)):
            await sysq(A, "UPDATE projects SET retention_tier=%s WHERE id=%s", (tier, a["pid"]))
            rid, _ = await run(obs, a["pid"]); assert await ttl(A, rid) == days, tier
        await sysq(A, "UPDATE projects SET retention_tier='custom', retention_days=3650 WHERE id=%s", (a["pid"],))
        rid, _ = await run(obs, a["pid"]); assert await ttl(A, rid) == 3650
        await sysq(A, "UPDATE workspaces SET retention_default='extended' WHERE id=%s", (a["ws"],)); await sysq(A, "UPDATE projects SET retention_tier=NULL WHERE id=%s", (a["pid"],))
        rid, _ = await run(obs, a["pid"]); assert await ttl(A, rid) == 200                       # inherits the workspace default
        # updating an existing run (PATCH) never rewrites its ttl
        await sysq(A, "UPDATE workspaces SET retention_default='base' WHERE id=%s", (a["ws"],))
        await obs.upsert_run(a["pid"], {"id": rid, "end_time": NOW(), "outputs": {"a": 1}}); assert await ttl(A, rid) == 200
    with_world(go)

# ---------------------------------------------------------------- over HTTP (needs a running local server: GD12_BASE, default :8020; skipped when nothing answers)
import httpx  # noqa: E402
BASE = os.environ.get("GD12_BASE", "http://127.0.0.1:8020")
def _up():
    try: return httpx.get(BASE + "/health", timeout=3).status_code < 500
    except Exception: return False   # noqa: BLE001

@pytest.mark.skipif(not _up(), reason=f"no local server at {BASE} (set GD12_BASE)")
def test_http_viewer_and_anonymous_are_refused_admin_dry_run_changes_nothing():
    unknown = str(uuid.uuid4())
    with httpx.Client(base_url=BASE, timeout=60) as anon:
        for m, u, b in (("GET", "/api/obs/retention", None), ("PUT", "/api/obs/retention", {"base_days": 1}), ("POST", f"/api/obs/traces/{unknown}/extend", None), ("PATCH", "/api/obs/projects/1", {"retention_days": 1})):
            assert anon.request(m, u, json=b).status_code == 401, (m, u)                       # not signed in
    with httpx.Client(base_url=BASE, auth=("viewer1", "phase0-viewer-local"), timeout=60) as v:
        for m, u, b in (("PUT", "/api/obs/retention", {"base_days": 1}), ("POST", f"/api/obs/traces/{unknown}/extend", None), ("PATCH", "/api/obs/projects/1", {"retention_days": 1})):
            assert v.request(m, u, json=b).status_code == 403, (m, u)                          # a viewer cannot change or keep anything
    with httpx.Client(base_url=BASE, auth=("moza", "phase0-local"), timeout=60) as a:
        before = a.get("/api/obs/retention").json(); assert {"default", "base_days", "extended_days", "kept_traces"} <= set(before)
        d = a.put("/api/obs/retention", json={"base_days": 1, "apply_to_existing": True}); assert d.status_code == 200 and d.json()["dry_run"] is True      # dry run: counts only
        assert a.get("/api/obs/retention").json() == before                                    # nothing changed
        assert a.post(f"/api/obs/traces/{unknown}/extend").status_code == 404                  # unknown trace
        assert a.post("/api/obs/traces/not-a-uuid/extend").status_code == 422
        assert a.patch("/api/obs/projects/99999999", json={"retention_days": 5}).status_code == 404
        assert a.put("/api/obs/retention", json={"base_days": 100, "extended_days": 50}).status_code == 422
        assert a.put("/api/obs/retention", json={"default": "forever"}).status_code == 422
