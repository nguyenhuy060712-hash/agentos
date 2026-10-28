"""GĐ10 cards 10.8 / 10.9 / 10.10 (evals): the handlers are called in-process against the local Postgres (system session), no server restart needed."""
import asyncio, os, sys, uuid
import psycopg, pytest
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")   # read at import; no call is made here
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos"); os.environ.setdefault("PG_URI", PG)

def run(coro): return asyncio.run(coro)

def with_pool(fn):
    async def go():
        import deepagent_api as A
        pool = AsyncConnectionPool(PG, min_size=1, max_size=3, open=False, kwargs={"autocommit": True, "row_factory": dict_row}); await pool.open()
        old = (A.S.get("pool"), A.S.get("rls")); A.S["pool"], A.S["rls"] = pool, None
        try: return await fn(A)
        finally:
            A.S["pool"], A.S["rls"] = old; await pool.close()
    return run(go())

TAG = uuid.uuid4().hex[:8]

def test_ev_del_in_use_409_force_cleans_everything():
    async def t(A):
        import evals
        mk = lambda n, kind, cfg: A.q("INSERT INTO evaluators(name,kind,config) VALUES(%s,%s,%s) RETURNING id", (f"t108-{n}-{TAG}", kind, Jsonb(cfg)), one=True)
        e = (await mk("e", "code", {"key": "k"}))["id"]; keep = (await mk("keep", "code", {"key": "k2"}))["id"]
        comp = (await mk("comp", "composite", {"evaluator_ids": [e, keep]}))["id"]
        ds = (await A.q("INSERT INTO datasets(name,workspace_id,evaluator_ids) VALUES(%s,1,%s) RETURNING id", (f"t108-ds-{TAG}", [e, keep]), one=True))["id"]
        ex = (await A.q("INSERT INTO experiments(name,dataset_id,target,evaluator_ids) VALUES('t108',%s,%s,%s) RETURNING id", (ds, Jsonb({}), [e]), one=True))["id"]
        sc = (await A.q("INSERT INTO eval_schedules(name,cron,dataset_id,target,evaluator_ids) VALUES('t108','* * * * *',%s,%s,%s) RETURNING id", (ds, Jsonb({}), [e, keep]), one=True))["id"]
        pj = (await A.q("INSERT INTO projects(name,workspace_id) VALUES(%s,1) RETURNING id", (f"t108-p-{TAG}",), one=True))["id"]
        rule = (await A.q("INSERT INTO automation_rules(project_id,name,actions) VALUES(%s,'t108',%s) RETURNING id", (pj, Jsonb([{"type": "run_evaluator", "evaluator_id": e}, {"type": "add_tag", "tag": "x"}])), one=True))["id"]
        fb = (await A.q("INSERT INTO feedback(run_id,key,score,source,evaluator_id) VALUES(%s,'t108',1,'t',%s) RETURNING id", (str(uuid.uuid4()), e), one=True))["id"]
        try:
            kinds = {u["kind"] for u in await evals.ev_usages(e)}
            assert kinds == {"experiment", "schedule", "dataset", "composite", "rule"}
            with pytest.raises(HTTPException) as x: await evals.ev_del(e)
            assert x.value.status_code == 409 and len(x.value.detail["usages"]) == 5
            assert await A.q("SELECT 1 FROM evaluators WHERE id=%s", (e,), one=True)
            await evals.ev_del(e, force=1)
            assert not await A.q("SELECT 1 FROM evaluators WHERE id=%s", (e,), one=True)
            for tbl, i in (("experiments", ex), ("eval_schedules", sc), ("datasets", ds)):
                assert e not in (await A.q(f"SELECT evaluator_ids FROM {tbl} WHERE id=%s", (i,), one=True))["evaluator_ids"]
            assert (await A.q("SELECT evaluator_ids FROM eval_schedules WHERE id=%s", (sc,), one=True))["evaluator_ids"] == [keep]
            assert (await A.q("SELECT config FROM evaluators WHERE id=%s", (comp,), one=True))["config"]["evaluator_ids"] == [keep]
            assert (await A.q("SELECT actions FROM automation_rules WHERE id=%s", (rule,), one=True))["actions"] == [{"type": "add_tag", "tag": "x"}]
            assert await A.q("SELECT 1 FROM feedback WHERE id=%s", (fb,), one=True)   # history kept
        finally:
            await A.q("DELETE FROM feedback WHERE id=%s", (fb,)); await A.q("DELETE FROM projects WHERE id=%s", (pj,))
            await A.q("DELETE FROM eval_schedules WHERE id=%s", (sc,)); await A.q("DELETE FROM datasets WHERE id=%s", (ds,))
            await A.q("DELETE FROM evaluators WHERE id = ANY(%s)", ([comp, keep, e],))
    with_pool(t)

def test_ev_del_unused_plain():
    async def t(A):
        import evals
        e = (await A.q("INSERT INTO evaluators(name,kind,config) VALUES(%s,'code',%s) RETURNING id", (f"t108u-{TAG}", Jsonb({"key": "k"})), one=True))["id"]
        assert await evals.ev_usages(e) == []
        assert (await evals.ev_del(e))["ok"] and not await A.q("SELECT 1 FROM evaluators WHERE id=%s", (e,), one=True)
    with_pool(t)

def test_ex_upd_partial_keeps_metadata_and_splits():
    async def t(A):
        import evals
        d = (await A.q("INSERT INTO datasets(name,workspace_id) VALUES(%s,1) RETURNING id", (f"t109-{TAG}", ), one=True))["id"]
        try:
            eid = (await evals.ex_add(d, evals.ExampleIn(inputs={"q": "a"}, outputs={"a": "1"}, metadata={"src": "m"}, splits=["test"])))["ids"][0]
            async def cur(): return next(x for x in (await evals.examples_at(d))[1] if x["id"] == eid)
            await evals.ex_upd(eid, evals.ExampleUpd(outputs={"a": "2"}))
            c = await cur()
            assert c["outputs"] == {"a": "2"} and c["metadata"] == {"src": "m"} and c["splits"] == ["test"] and c["inputs"] == {"q": "a"}
            await evals.ex_upd(eid, evals.ExampleUpd(metadata={}))
            c = await cur(); assert c["metadata"] == {} and c["outputs"] == {"a": "2"}
            await evals.ex_upd(eid, evals.ExampleUpd(outputs=None))
            assert (await cur())["outputs"] is None
        finally:
            await A.q("DELETE FROM datasets WHERE id=%s", (d,))
    with_pool(t)

def test_experiment_example_carries_text_attachment_into_judge_prompt():
    async def t(A):
        import evals
        d = (await A.q("INSERT INTO datasets(name,workspace_id) VALUES(%s,1) RETURNING id", (f"t1010-{TAG}",), one=True))["id"]
        try:
            eid = (await evals.ex_add(d, evals.ExampleIn(inputs={"q": "a"}, outputs={"a": "1"})))["ids"][0]
            await A.q("INSERT INTO example_attachments(example_id,name,mime,bytes,data) VALUES(%s,'note.txt','text/plain',11,%s)", (eid, b"SECRET-TEXT!"))
            await A.q("INSERT INTO example_attachments(example_id,name,mime,bytes,data) VALUES(%s,'pic.png','image/png',3,%s)", (eid, b"\x89PN"))
            x = next(r for r in (await evals.examples_at(d))[1] if r["id"] == eid)
            exd = await evals.exp_example(x)
            p = evals._fill("Ref: {{reference}}\nFiles: {{attachments}}", {"input": "q", "output": "o"}, exd)
            assert "SECRET-TEXT!" in p and "pic.png" in p and "\x89" not in p
        finally:
            await A.q("DELETE FROM datasets WHERE id=%s", (d,))
    with_pool(t)
