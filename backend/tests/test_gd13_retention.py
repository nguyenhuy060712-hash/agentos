"""GĐ13.25 retention: default OFF, checkpoint clear / keep_latest with a real AsyncPostgresSaver, preview deletes nothing, per-workspace isolation, demo never swept,
admin-only API, env override of the event TTL. Throwaway schema (migration 0227 + minimal parent tables); no server, no LLM ($0)."""
import asyncio, importlib.util, os, sys, uuid
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")
import psycopg, pytest
from psycopg.rows import dict_row

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
import deepagent_api as A  # noqa: E402,I001  (first: circular imports)
import admin, gateway, retention as R  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from langgraph.checkpoint.base import empty_checkpoint  # noqa: E402
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # noqa: E402
from pydantic import ValidationError  # noqa: E402

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
run = asyncio.run
ADMIN1 = {"id": 1, "username": "boss", "role": "admin", "ws": 1, "auth": "session"}
ADMIN2 = {**ADMIN1, "id": 2, "username": "boss2", "ws": 2}
EDITOR = {**ADMIN1, "role": "editor"}
VIEWER = {**ADMIN1, "role": "viewer"}
DEMO_USER = {**ADMIN1, "demo": True, "role": "viewer"}
DEMO_ADMIN = {**ADMIN1, "ws": 3}          # an admin whose workspace is in gateway.DEMO_WS

def _migration():
    p = os.path.join(os.path.dirname(__file__), "..", "app", "migrations", "0227_retention_settings.py")
    sp = importlib.util.spec_from_file_location("m0227", p); m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m); return m

class Env:
    def __init__(self, sch): self.sch = sch; self.cp = None
    async def q(self, sql, args=(), one=False, many=True):
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True, options=f"-csearch_path={self.sch}", row_factory=dict_row) as c:
            cur = await c.execute(sql, args)
            if cur.description is None: return None
            return await (cur.fetchone() if one else cur.fetchall())
    async def mq(self, sql, args=()):
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True, options=f"-csearch_path={self.sch}") as c:
            cur = await c.execute(sql, args)
            return await cur.fetchall() if cur.description else None
    def go(self, body):
        """Run body(cp) in one event loop with a real Postgres saver bound to the throwaway schema."""
        async def w():
            uri = f"{PG}{'&' if '?' in PG else '?'}options=-csearch_path%3D{self.sch}"
            async with AsyncPostgresSaver.from_conn_string(uri) as cp:
                await cp.setup(); self.cp = cp; A.S["cp"] = cp
                try: return await body(cp)
                finally: A.S.pop("cp", None)
        return run(w())
    # fixtures
    async def agent(self, aid, ws): await self.q("INSERT INTO agents VALUES (%s,%s) ON CONFLICT DO NOTHING", (aid, ws))
    async def thread(self, tid, aid, ws, age_h, status="idle", cks=3):
        await self.agent(aid, ws)
        await self.q("INSERT INTO lg_threads(thread_id,agent_id,workspace_id,status,updated_at) VALUES(%s,%s,%s,%s, now() - %s * interval '1 hour')", (tid, aid, ws, status, age_h))
        cfg = {"configurable": {"thread_id": f"a{aid}:{tid}", "checkpoint_ns": ""}}
        for i in range(cks): cfg = await self.cp.aput(cfg, empty_checkpoint(), {"source": "loop", "step": i}, {})
    async def n_ck(self, aid, tid):
        return len([x async for x in self.cp.alist({"configurable": {"thread_id": f"a{aid}:{tid}", "checkpoint_ns": ""}})])
    async def settings(self, ws, **kw):
        cols = list(kw)
        await self.q(f"INSERT INTO retention_settings(workspace_id,{','.join(cols)}) VALUES(%s,{','.join(['%s'] * len(cols))}) ON CONFLICT (workspace_id) DO UPDATE SET " + ",".join(f"{c}=EXCLUDED.{c}" for c in cols), (ws, *kw.values()))
    async def event(self, rid, ws, age_h):
        await self.q("INSERT INTO lg_runs(run_id,workspace_id,status,ended_at) VALUES(%s,%s,'success', now() - %s * interval '1 hour')", (rid, ws, age_h))
        await self.q("INSERT INTO lg_run_events(run_id,seq,event) VALUES(%s,0,'x')", (rid,))
    async def has_event(self, rid): return bool(await self.q("SELECT 1 FROM lg_run_events WHERE run_id=%s", (rid,), one=True))

@pytest.fixture
def env(monkeypatch):
    sch = "t1325_" + uuid.uuid4().hex[:8]
    with psycopg.connect(PG, autocommit=True) as c:
        c.execute(f'CREATE SCHEMA "{sch}"')
        c.execute(f'CREATE TABLE "{sch}".agents(id int primary key, workspace_id int not null)')
        c.execute(f'''CREATE TABLE "{sch}".lg_threads(thread_id text primary key, agent_id int, workspace_id int not null default 1, status text not null default 'idle',
                      updated_at timestamptz not null default now())''')
        c.execute(f'''CREATE TABLE "{sch}".lg_runs(run_id uuid primary key default gen_random_uuid(), thread_id text, status text not null default 'pending',
                      workspace_id int not null default 1, ended_at timestamptz)''')
        c.execute(f'CREATE TABLE "{sch}".lg_run_events(run_id uuid, seq int, event text, primary key(run_id, seq))')
    e = Env(sch)
    run(_migration().up(e.mq))
    monkeypatch.setattr(R, "_q", e.q)
    monkeypatch.setattr(gateway, "DEMO_WS", {3})
    monkeypatch.delenv("AOS_RESUMABLE_STREAM_TTL_SECONDS", raising=False)
    R._last[0] = 0.0
    yield e
    with psycopg.connect(PG, autocommit=True) as c: c.execute(f'DROP SCHEMA "{sch}" CASCADE')

def as_user(user, coro):
    async def w():
        tok = admin.USER.set(user)
        try: return await coro
        finally: admin.USER.reset(tok)
    return w()

# ------------------------------------------------------------------ default OFF
def test_default_off_nothing_but_14_day_events_runs(env):
    r_old, r_new = str(uuid.uuid4()), str(uuid.uuid4())
    async def body(cp):
        await env.thread("t-old", 1, 1, age_h=24 * 90); await env.thread("t-new", 1, 1, age_h=1)
        await env.event(r_old, 1, 24 * 15); await env.event(r_new, 1, 24 * 13)
        await R.sweep_all()
        return await env.n_ck(1, "t-old"), await env.n_ck(1, "t-new"), await env.has_event(r_old), await env.has_event(r_new)
    assert env.go(body) == (3, 3, False, True)            # checkpoints untouched, only the >14-day event went
    s = run(R.get_settings(1))
    assert s["auto_delete"] is False and s["checkpoint_days"] is None and s["checkpoint_keep_latest"] is False

def test_off_settings_row_without_toggles_deletes_nothing(env):
    async def body(cp):
        await env.settings(1, run_events_days=5)          # a row exists, checkpoint toggles are off
        await env.thread("t-old", 1, 1, age_h=24 * 90)
        out = await R.sweep_checkpoints(1)
        return out, await env.n_ck(1, "t-old")
    out, n = env.go(body)
    assert out["cleared"] == out["pruned"] == 0 and n == 3

# ------------------------------------------------------------------ checkpoint_days
def test_checkpoint_days_clears_old_keeps_fresh_and_protected(env):
    async def body(cp):
        await env.settings(1, checkpoint_days=1)
        await env.thread("t-old", 1, 1, age_h=72); await env.thread("t-fresh", 1, 1, age_h=2)
        await env.thread("t-busy", 1, 1, age_h=72, status="busy"); await env.thread("t-wait", 1, 1, age_h=72, status="interrupted")
        await env.thread("t-pend", 1, 1, age_h=72); await env.q("INSERT INTO lg_runs(thread_id,status,workspace_id) VALUES('t-pend','pending',1)")
        out = await R.sweep_checkpoints(1); again = await R.sweep_checkpoints(1)
        got = {t: await env.n_ck(1, t) for t in ("t-old", "t-fresh", "t-busy", "t-wait", "t-pend")}
        return out, again, got
    out, again, got = env.go(body)
    assert out["cleared"] == 1 and again["cleared"] == 0                      # second pass does not repeat itself
    assert got == {"t-old": 0, "t-fresh": 3, "t-busy": 3, "t-wait": 3, "t-pend": 3}
    assert run(env.q("SELECT 1 FROM lg_threads WHERE thread_id='t-old'", one=True))      # the thread row stays (messages, files)

def test_checkpoint_days_thread_with_new_activity_is_eligible_again(env):
    async def body(cp):
        await env.settings(1, checkpoint_days=1); await env.thread("t1", 1, 1, age_h=72)
        await R.sweep_checkpoints(1)
        cfg = {"configurable": {"thread_id": "a1:t1", "checkpoint_ns": ""}}
        await cp.aput(cfg, empty_checkpoint(), {"source": "loop", "step": 9}, {})            # new activity, then idle again
        await env.q("UPDATE lg_threads SET updated_at = now() - interval '3 days', retention_cleared_at = now() - interval '10 days' WHERE thread_id='t1'")   # time passed since the sweep
        out = await R.sweep_checkpoints(1)
        return out["cleared"], await env.n_ck(1, "t1")
    assert env.go(body) == (1, 0)

# ------------------------------------------------------------------ keep_latest
def test_keep_latest_keeps_exactly_one_and_thread_resumes(env):
    async def body(cp):
        await env.settings(1, checkpoint_keep_latest=True)
        await env.thread("t-idle", 1, 1, age_h=3, cks=4); await env.thread("t-recent", 1, 1, age_h=0.2, cks=4)   # 12 minutes idle: too fresh
        before = await cp.aget_tuple({"configurable": {"thread_id": "a1:t-idle", "checkpoint_ns": ""}})
        out = await R.sweep_checkpoints(1); again = await R.sweep_checkpoints(1)
        after = await cp.aget_tuple({"configurable": {"thread_id": "a1:t-idle", "checkpoint_ns": ""}})
        return out, again, await env.n_ck(1, "t-idle"), await env.n_ck(1, "t-recent"), before.checkpoint["id"], after.checkpoint["id"]
    out, again, n_idle, n_recent, b, a = env.go(body)
    assert out["pruned"] == 1 and again["pruned"] == 0
    assert (n_idle, n_recent) == (1, 4) and a == b          # exactly one left, and it is the latest one: the thread resumes from it

def test_both_modes_clear_wins_for_old_prune_for_middle(env):
    async def body(cp):
        await env.settings(1, checkpoint_days=2, checkpoint_keep_latest=True)
        await env.thread("t-old", 1, 1, age_h=24 * 5); await env.thread("t-mid", 1, 1, age_h=5)
        out = await R.sweep_checkpoints(1)
        return out, await env.n_ck(1, "t-old"), await env.n_ck(1, "t-mid")
    out, old, mid = env.go(body)
    assert (out["cleared"], out["pruned"], old, mid) == (1, 1, 0, 1)

# ------------------------------------------------------------------ preview
def test_preview_counts_and_deletes_nothing(env):
    r_old = str(uuid.uuid4())
    async def body(cp):
        await env.settings(1, checkpoint_days=1, checkpoint_keep_latest=True)
        await env.thread("t-old", 1, 1, age_h=72); await env.thread("t-mid", 1, 1, age_h=5); await env.event(r_old, 1, 24 * 20)
        p = await as_user(ADMIN1, R.retention_preview())
        return p, await env.n_ck(1, "t-old"), await env.n_ck(1, "t-mid"), await env.has_event(r_old)
    p, old, mid, ev = env.go(body)
    assert (p["threads_to_clear"], p["threads_to_prune"], p["run_events_to_delete"]) == (1, 1, 1)
    assert (old, mid, ev) == (3, 3, True)                                   # nothing deleted

# ------------------------------------------------------------------ isolation / demo
def test_other_workspace_is_never_touched(env):
    async def body(cp):
        await env.settings(1, checkpoint_days=1)                             # only workspace 1 opted in
        await env.thread("a-old", 1, 1, age_h=72); await env.thread("b-old", 2, 2, age_h=72)
        await R.sweep_all()
        return await env.n_ck(1, "a-old"), await env.n_ck(2, "b-old")
    assert env.go(body) == (0, 3)

def test_thread_whose_agent_belongs_to_another_workspace_is_skipped(env):
    async def body(cp):
        await env.settings(1, checkpoint_days=1)
        await env.agent(50, 2)                                                # agent 50 is workspace 2's
        await env.q("INSERT INTO lg_threads(thread_id,agent_id,workspace_id,updated_at) VALUES('x',50,1, now() - interval '5 days')")
        cfg = {"configurable": {"thread_id": "a50:x", "checkpoint_ns": ""}}
        await cp.aput(cfg, empty_checkpoint(), {"source": "loop", "step": 0}, {})
        out = await R.sweep_checkpoints(1)
        return out["cleared"], await env.n_ck(50, "x")
    assert env.go(body) == (0, 1)

def test_demo_workspace_never_swept_even_with_a_row(env):
    async def body(cp):
        await env.settings(3, checkpoint_days=1, checkpoint_keep_latest=True)   # inserted behind the API's back
        await env.thread("d-old", 7, 3, age_h=72)
        direct = await R.sweep_checkpoints(3); await R.sweep_all()
        return direct, await env.n_ck(7, "d-old")
    direct, n = env.go(body)
    assert direct["cleared"] == direct["pruned"] == 0 and n == 3

def test_run_events_cutoff_is_per_workspace(env):
    a_ev, b_ev = str(uuid.uuid4()), str(uuid.uuid4())
    async def body():
        await env.settings(1, run_events_days=1)                              # ws 1: 1 day; ws 2: default 14 days
        await env.event(a_ev, 1, 48); await env.event(b_ev, 2, 48)
        n = await R.sweep_run_events()
        return n, await env.has_event(a_ev), await env.has_event(b_ev)
    assert run(body()) == (1, False, True)

def test_env_ttl_replaces_14_days_and_bad_value_falls_back(env, monkeypatch):
    e1, e2 = str(uuid.uuid4()), str(uuid.uuid4())
    async def body():
        await env.event(e1, 1, 2); await env.event(e2, 1, 0.1)
        await R.sweep_run_events()
        return await env.has_event(e1), await env.has_event(e2)
    monkeypatch.setenv("AOS_RESUMABLE_STREAM_TTL_SECONDS", "3600")
    assert run(body()) == (False, True)
    assert R.default_event_seconds() == 3600
    for bad in ("abc", "0", "-5", ""):
        monkeypatch.setenv("AOS_RESUMABLE_STREAM_TTL_SECONDS", bad); assert R.default_event_seconds() == 14 * 86400

# ------------------------------------------------------------------ admin API
def test_api_admin_only_and_never_demo(env):
    for u in (EDITOR, VIEWER, DEMO_USER, DEMO_ADMIN, None):
        for fn, args in ((R.retention_get, ()), (R.retention_put, (R.RetentionIn(checkpoint_days=1),)), (R.retention_preview, ()), (R.retention_run, (R.RunIn(confirm=True),))):
            with pytest.raises(HTTPException) as ei:
                run(as_user(u, fn(*args))) if u else run(fn(*args))
            assert ei.value.status_code == 403
    assert run(env.q("SELECT count(*) n FROM retention_settings", one=True))["n"] == 0          # nothing was written
    # the HTTP layer refuses non-admins before the handler (ADMIN_ONLY prefix /api/admin)
    for role in ("editor", "viewer"):
        for m, p in (("GET", "/api/admin/retention"), ("PUT", "/api/admin/retention"), ("POST", "/api/admin/retention/preview"), ("POST", "/api/admin/retention/run")):
            assert not admin.allowed(role, m, p)
    assert not admin.allowed("viewer", "GET", "/api/admin/retention", demo=True)
    assert admin.allowed("admin", "PUT", "/api/admin/retention")

def test_api_put_get_roundtrip_is_workspace_scoped(env):
    r = run(as_user(ADMIN1, R.retention_put(R.RetentionIn(checkpoint_days=7, checkpoint_keep_latest=True, run_events_days=3))))
    assert (r["checkpoint_days"], r["checkpoint_keep_latest"], r["run_events_days"], r["updated_by"]) == (7, True, 3, "boss") and r["auto_delete"]
    r = run(as_user(ADMIN1, R.retention_put(R.RetentionIn(checkpoint_days=None))))        # an explicit null switches that field off, the others stay
    assert r["checkpoint_days"] is None and r["checkpoint_keep_latest"] is True and r["run_events_days"] == 3
    other = run(as_user(ADMIN2, R.retention_get()))                                      # workspace 2 sees defaults, not workspace 1's values
    assert other["workspace_id"] == 2 and other["run_events_days"] is None and other["checkpoint_keep_latest"] is False and not other["auto_delete"]
    run(as_user(ADMIN2, R.retention_put(R.RetentionIn(checkpoint_days=9))))
    assert run(as_user(ADMIN1, R.retention_get()))["checkpoint_days"] is None
    assert run(env.q("SELECT count(*) n FROM retention_settings", one=True))["n"] == 2
    with pytest.raises(HTTPException) as ei: run(as_user(ADMIN1, R.retention_put(R.RetentionIn())))
    assert ei.value.status_code == 422

def test_api_validation_rejects_zero_negative_and_huge():
    for bad in (0, -1, 3651, 10 ** 9):
        for f in ("checkpoint_days", "run_events_days", "thread_events_days"):
            with pytest.raises(ValidationError): R.RetentionIn(**{f: bad})

def test_manual_run_needs_confirm_and_only_touches_own_workspace(env):
    async def body(cp):
        await env.thread("a-old", 1, 1, age_h=72); await env.thread("b-old", 2, 2, age_h=72)
        await env.settings(1, checkpoint_days=1); await env.settings(2, checkpoint_days=1)
        with pytest.raises(HTTPException) as ei: await as_user(ADMIN1, R.retention_run(R.RunIn(confirm=False)))
        assert ei.value.status_code == 422 and await env.n_ck(1, "a-old") == 3         # refused: nothing deleted
        out = await as_user(ADMIN1, R.retention_run(R.RunIn(confirm=True)))
        return out, await env.n_ck(1, "a-old"), await env.n_ck(2, "b-old")
    out, a, b = env.go(body)
    assert out["cleared"] == 1 and (a, b) == (0, 3)
    assert run(env.q("SELECT last_counts FROM retention_settings WHERE workspace_id=1", one=True))["last_counts"]["cleared"] == 1

def test_sweep_limit_leaves_the_rest_for_the_next_pass(env, monkeypatch):
    async def body(cp):
        await env.settings(1, checkpoint_days=1)
        for i in range(3): await env.thread(f"t{i}", 1, 1, age_h=72, cks=1)
        first = await R.sweep_checkpoints(1, limit=2); second = await R.sweep_checkpoints(1, limit=2)
        return first, second
    first, second = env.go(body)
    assert (first["cleared"], first["more"], second["cleared"], second["more"]) == (2, True, 1, False)

def test_migration_registered_and_reversible(env):
    assert "retention_settings" in admin.RLS_DIRECT and any("/api/admin/retention".startswith(p) for p in admin.ADMIN_ONLY)
    run(_migration().up(env.mq))                                                          # idempotent
    run(_migration().down(env.mq))
    assert not run(env.q("SELECT to_regclass('retention_settings') t", one=True))["t"]
