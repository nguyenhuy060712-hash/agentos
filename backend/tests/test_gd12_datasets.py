"""GĐ12 cards 12.50 / 12.51 (lane E1): dataset rename / exists / search / clone and CSV + JSONL import / export. In-process handlers against the local Postgres, two workspaces."""
import asyncio, csv, io, json, os, sys, uuid
import pytest
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos"); os.environ.setdefault("PG_URI", PG)
TAG = uuid.uuid4().hex[:8]
WS_A, WS_B = 1, 2
BOM, NL = chr(0xFEFF), chr(10)

def with_pool(fn):
    async def go():
        import deepagent_api as A
        pool = AsyncConnectionPool(PG, min_size=1, max_size=3, open=False, kwargs={"autocommit": True, "row_factory": dict_row}); await pool.open()
        old = (A.S.get("pool"), A.S.get("rls")); A.S["pool"], A.S["rls"] = pool, None
        try: return await fn(A)
        finally:
            await A.q("DELETE FROM datasets WHERE name LIKE %s", (f"t1250-{TAG}%",))
            A.S["pool"], A.S["rls"] = old; await pool.close()
    return asyncio.run(go())

async def as_ws(ws, fn, demo=False):
    import admin
    t = admin.USER.set({"id": 987654, "username": "t1250", "role": "admin", "ws": ws, "auth": "test", "demo": demo})
    try: return await fn()
    finally: admin.USER.reset(t)

def nm(s): return f"t1250-{TAG}-{s}"

async def mk(E, name, rows=()):
    d = await E.ds_new(E.DatasetIn(name=name))
    if rows: await E.ex_add(d["id"], [E.ExampleIn(**r) for r in rows])
    return d

def test_rename_exists_conflict_and_rls():
    async def t(A):
        import evals, evals_ds as D
        a = await as_ws(WS_A, lambda: mk(evals, nm("a")))
        b = await as_ws(WS_A, lambda: mk(evals, nm("b")))
        assert (await as_ws(WS_A, lambda: D.ds_exists(nm("a"))))["exists"] is True
        assert (await as_ws(WS_B, lambda: D.ds_exists(nm("a"))))["exists"] is False
        with pytest.raises(HTTPException) as x: await as_ws(WS_A, lambda: D.ds_patch(b["id"], D.DsPatch(name=nm("a"))))
        assert x.value.status_code == 409
        r = await as_ws(WS_A, lambda: D.ds_patch(b["id"], D.DsPatch(name=nm("b2"), description="mô tả")))
        assert r["name"] == nm("b2") and r["description"] == "mô tả"
        with pytest.raises(HTTPException) as x: await as_ws(WS_B, lambda: D.ds_patch(a["id"], D.DsPatch(name=nm("hack"))))
        assert x.value.status_code == 404
        with pytest.raises(HTTPException) as x: await as_ws(WS_B, lambda: D.ds_clone(a["id"], D.CloneIn(name=nm("steal"))))
        assert x.value.status_code == 404
        with pytest.raises(HTTPException) as x: await as_ws(WS_A, lambda: D.ds_patch(a["id"], D.DsPatch(name=nm("zz"))), True)
        assert x.value.status_code == 403
    with_pool(t)

def test_search_type_limit_and_old_output():
    async def t(A):
        import evals
        for s, ty, desc in (("alpha", None, None), ("beta", "kv", "hello"), ("gamma", "kv", None)):
            await as_ws(WS_A, lambda: evals.ds_new(evals.DatasetIn(name=nm(s), description=desc, dataset_type=ty)))
        names = lambda rs: [x["name"] for x in rs]
        assert {nm("alpha"), nm("beta"), nm("gamma")} <= set(names(await as_ws(WS_A, lambda: evals.ds_list())))
        assert names(await as_ws(WS_A, lambda: evals.ds_list(q=f"{TAG}-AL"))) == [nm("alpha")]
        assert names(await as_ws(WS_A, lambda: evals.ds_list(q="hello"))) == [nm("beta")]
        assert await as_ws(WS_A, lambda: evals.ds_list(q="%")) == []   # wildcard is literal
        r = await as_ws(WS_A, lambda: evals.ds_list(q=TAG, type="kv", limit=1))
        assert len(r) == 1 and r[0]["dataset_type"] == "kv"
        assert len(await as_ws(WS_A, lambda: evals.ds_list(q=TAG, type="kv", limit=5))) == 2
        assert await as_ws(WS_B, lambda: evals.ds_list(q=TAG)) == []
    with_pool(t)

def test_clone_version_split_and_tag():
    async def t(A):
        import evals, evals_ds as D
        src = await as_ws(WS_A, lambda: mk(evals, nm("src"), [{"inputs": {"question": "q1"}, "outputs": {"answer": "a1"}, "splits": ["train"]},
                                                              {"inputs": {"question": "q2"}, "outputs": {"answer": "a2"}, "splits": ["test"]}]))   # v1
        await as_ws(WS_A, lambda: evals.ex_add(src["id"], evals.ExampleIn(inputs={"question": "q3"}, outputs={"answer": "a3"}, splits=["train"])))   # v2
        await as_ws(WS_A, lambda: evals.ex_add(src["id"], evals.ExampleIn(inputs={"question": "q4"}, splits=["test"])))   # v3
        await as_ws(WS_A, lambda: evals.ds_tag(src["id"], evals.TagIn(tag="gold", version=1)))
        c = await as_ws(WS_A, lambda: D.ds_clone(src["id"], D.CloneIn(name=nm("c3"), version=3)))
        assert c["examples"] == 4 and c["version"] == 1
        got = await as_ws(WS_A, lambda: evals.ds_get(c["id"]))
        assert {(e["inputs"]["question"], tuple(e["splits"])) for e in got["examples"]} == {("q1", ("train",)), ("q2", ("test",)), ("q3", ("train",)), ("q4", ("test",))}
        assert got["splits"] == ["test", "train"] and all(e["metadata"]["cloned_from"]["dataset_id"] == src["id"] for e in got["examples"])
        c1 = await as_ws(WS_A, lambda: D.ds_clone(src["id"], D.CloneIn(name=nm("c1"), tag="gold")))
        assert c1["examples"] == 2
        c2 = await as_ws(WS_A, lambda: D.ds_clone(src["id"], D.CloneIn(name=nm("c2"), split="train")))
        assert c2["examples"] == 2
        with pytest.raises(HTTPException) as x: await as_ws(WS_A, lambda: D.ds_clone(src["id"], D.CloneIn(name=nm("c2"))))
        assert x.value.status_code == 409
        with pytest.raises(HTTPException) as x: await as_ws(WS_A, lambda: D.ds_clone(src["id"], D.CloneIn(name=nm("c9"), version=99)))
        assert x.value.status_code == 400
    with_pool(t)

async def export(D, did, fmt, **kw):
    r = await D.ds_export(did, format=fmt, **kw); return r.body.decode("utf-8"), r.headers

def test_csv_jsonl_roundtrip_injection_and_bad_rows():
    async def t(A):
        import evals, evals_ds as D
        rows = [{"inputs": {"question": "Xin chào, thế giới?", "n": "-5"}, "outputs": {"answer": '=HYPERLINK("http://x","c")'}, "metadata": {"src": "m"}, "splits": ["train"]},
                {"inputs": {"question": "q2"}, "outputs": {"answer": "a2" + NL + "xuống dòng"}, "splits": ["test", "base"]},
                {"inputs": {"question": "q3", "obj": {"k": [1, 2]}}, "splits": ["base"]}]
        src = await as_ws(WS_A, lambda: mk(evals, nm("exp"), rows))
        same = lambda got: sorted((json.dumps(e["inputs"], sort_keys=True), json.dumps(e["outputs"], sort_keys=True), sorted(e["splits"])) for e in got)
        want = same((await as_ws(WS_A, lambda: evals.ds_get(src["id"])))["examples"])
        for fmt in ("csv", "jsonl"):
            body, h = await as_ws(WS_A, lambda: export(D, src["id"], fmt))
            if fmt == "csv":
                assert body.startswith(BOM)
                cells = [c for r in csv.reader(io.StringIO(body.lstrip(BOM))) for c in r]
                assert "'=HYPERLINK(" + '"http://x","c")' in cells and "'-5" in cells   # text, not a formula
            res = await as_ws(WS_A, lambda: D.ds_import(D.ImportIn(name=nm("imp-" + fmt), format=fmt, text=body)))
            assert res["imported"] == 3 and res["skipped"] == []
            got = (await as_ws(WS_A, lambda: evals.ds_get(res["id"])))["examples"]
            assert same(got) == want, fmt
        # bad rows are skipped, the file is never aborted
        txt = NL.join(['{"inputs": {"question": "ok"}, "outputs": {"answer": "yes"}}', "not json", '{"inputs": {}}', "[1,2]", '{"question": "flat", "answer": "x"}'])
        res = await as_ws(WS_A, lambda: D.ds_import(D.ImportIn(name=nm("bad"), format="jsonl", text=txt)))
        assert res["imported"] == 2 and [s["row"] for s in res["skipped"]] == [2, 3, 4]
        # csv inference of prefixes + explicit rename
        txt = "input_question,output_answer,meta_lang,extra" + NL + "hi,yo,vi,e" + NL
        res = await as_ws(WS_A, lambda: D.ds_import(D.ImportIn(name=nm("inf"), format="csv", text=txt, rename={"input_question": "q"})))
        e = (await as_ws(WS_A, lambda: evals.ds_get(res["id"])))["examples"][0]
        assert e["inputs"] == {"q": "hi", "extra": "e"} and e["outputs"] == {"answer": "yo"} and e["metadata"] == {"lang": "vi"}
        res = await as_ws(WS_A, lambda: D.ds_import(D.ImportIn(name=nm("flatcsv"), format="csv", text="question,answer" + NL + "A,B" + NL)))
        assert (await as_ws(WS_A, lambda: evals.ds_get(res["id"])))["examples"][0]["inputs"] == {"question": "A", "answer": "B"}   # no prefix = everything is input
        with pytest.raises(HTTPException) as x: await as_ws(WS_A, lambda: D.ds_import(D.ImportIn(name=nm("flatcsv"), format="csv", text="a" + NL + "1" + NL)))
        assert x.value.status_code == 409
        with pytest.raises(HTTPException) as x: await as_ws(WS_A, lambda: D.ds_import(D.ImportIn(name=nm("demo"), format="csv", text="a" + NL + "1" + NL)), True)
        assert x.value.status_code == 403
    with_pool(t)

def test_limits_and_openai_export():
    async def t(A):
        import evals, evals_ds as D
        big = "a" + NL + NL.join(["1"] * 10000)
        with pytest.raises(HTTPException) as x: await as_ws(WS_A, lambda: D.ds_import(D.ImportIn(name=nm("big"), format="csv", text=big)))
        assert x.value.status_code == 413 and "5,000" in x.value.detail
        assert not await A.q("SELECT 1 FROM datasets WHERE name=%s", (nm("big"),))   # refused before anything is created
        with pytest.raises(HTTPException) as x: await as_ws(WS_A, lambda: D.ds_import(D.ImportIn(name=nm("big2"), format="jsonl", text="x" * (5 * 1024 * 1024 + 1))))
        assert x.value.status_code == 413
        src = await as_ws(WS_A, lambda: mk(evals, nm("ft"), [{"inputs": {"question": "Q1"}, "outputs": {"answer": "A1"}}, {"inputs": {"message": "Q2"}, "outputs": {"message": {"role": "assistant", "content": "A2"}}},
                                                           {"inputs": {"foo": "x"}, "outputs": {"answer": "A3"}}, {"inputs": {"question": "Q4"}}]))
        for fmt in ("openai_chat", "openai_ft"):
            body, h = await as_ws(WS_A, lambda: export(D, src["id"], fmt))
            ls = [json.loads(l) for l in body.splitlines()]
            assert h["X-Skipped"] == "2" and len(ls) == 2
            assert ls[0] == {"messages": [{"role": "user", "content": "Q1"}, {"role": "assistant", "content": "A1"}]} and ls[1]["messages"][1]["content"] == "A2"
        with pytest.raises(HTTPException) as x: await as_ws(WS_B, lambda: D.ds_export(src["id"], format="csv"))
        assert x.value.status_code == 404
        with pytest.raises(HTTPException) as x: await as_ws(WS_A, lambda: D.ds_export(src["id"], format="xml"))
        assert x.value.status_code == 400
    with_pool(t)

# ---------------------------------------------------------------- 12.54 synthesize
def _fake_gen(A, monkeypatch, replies):
    """Stand-in for the fake LLM `[json] {obj}` marker: each call pops the next reply (an exception is raised)."""
    calls = []
    async def gen(client, prompt, chain, json_mode=False, temperature=None, agent_id=None, evaluator_id=None, feature=None):
        calls.append({"prompt": prompt, "feature": feature, "json_mode": json_mode})
        r = replies.pop(0)
        if isinstance(r, Exception): raise r
        return r, "fake", 1, 1
    monkeypatch.setattr(A.gw, "generate", gen); return calls

def test_synthesize_valid_rejected_save_and_demo(monkeypatch):
    async def t(A):
        import evals, evals_ds as D
        d = await as_ws(WS_A, lambda: evals.ds_new(evals.DatasetIn(name=nm("syn"), dataset_type="qa")))
        await as_ws(WS_A, lambda: evals.ex_add(d["id"], evals.ExampleIn(inputs={"question": "Giá gói Pro?"}, outputs={"answer": "499k"})))
        good = [{"inputs": {"question": f"Câu {i}?"}, "outputs": {"answer": f"Đáp {i}"}} for i in range(9)]
        bad = {"inputs": {"question": 5}, "outputs": {"answer": "x"}}   # wrong type
        calls = _fake_gen(A, monkeypatch, [json.dumps({"examples": good + [bad]})])
        r = await as_ws(WS_A, lambda: D.ds_synth(d["id"], D.SynthIn(count=10, guidance="về giá")))
        assert len(r["items"]) == 9 and len(r["rejected"]) == 1 and r["rejected"][0]["i"] == 9 and "stopped" not in r
        assert calls[0]["feature"] == "synthetic" and calls[0]["json_mode"] and "về giá" in calls[0]["prompt"] and "Giá gói Pro" in calls[0]["prompt"]
        _, before = await evals.examples_at(d["id"]); assert len(before) == 1   # nothing saved by synthesize
        s = await as_ws(WS_A, lambda: D.ds_synth_save(d["id"], D.SynthSave(items=r["items"] + [bad], split="train")))
        assert s["saved"] == 9 and len(s["rejected"]) == 1
        got = await as_ws(WS_A, lambda: evals.ds_get(d["id"]))
        syn = [e for e in got["examples"] if (e["metadata"] or {}).get("source") == "synthetic"]
        assert len(syn) == 9 and all(e["splits"] == ["train"] for e in syn)
        with pytest.raises(HTTPException) as x: await as_ws(WS_B, lambda: D.ds_synth(d["id"], D.SynthIn()))
        assert x.value.status_code == 404
        with pytest.raises(HTTPException) as x: await as_ws(WS_A, lambda: D.ds_synth(d["id"], D.SynthIn(seed_ids=[999999999])))
        assert x.value.status_code == 400
        with pytest.raises(HTTPException) as x: await as_ws(WS_A, lambda: D.ds_synth(d["id"], D.SynthIn()), True)
        assert x.value.status_code == 403
        with pytest.raises(HTTPException) as x: await as_ws(WS_A, lambda: D.ds_synth_save(d["id"], D.SynthSave(items=[])), True)
        assert x.value.status_code == 403
    with_pool(t)

def test_synthesize_budget_stop_keeps_items_so_far(monkeypatch):
    async def t(A):
        import evals, evals_ds as D
        from gateway import BudgetExceeded
        d = await as_ws(WS_A, lambda: evals.ds_new(evals.DatasetIn(name=nm("synb"), dataset_type="qa")))
        first = json.dumps([{"inputs": {"question": f"q{i}"}, "outputs": {"answer": "a"}} for i in range(10)])   # a bare list is accepted too
        _fake_gen(A, monkeypatch, [first, BudgetExceeded("hết ngân sách")])
        r = await as_ws(WS_A, lambda: D.ds_synth(d["id"], D.SynthIn(count=20)))
        assert r["stopped"] == "budget" and len(r["items"]) == 10
        _fake_gen(A, monkeypatch, [BudgetExceeded("hết")])
        r = await as_ws(WS_A, lambda: D.ds_synth(d["id"], D.SynthIn(count=5)))
        assert r["stopped"] == "budget" and r["items"] == []
        _, exs = await evals.examples_at(d["id"]); assert exs == []
    with_pool(t)
    import evals_ds as D
    with pytest.raises(Exception): D.SynthIn(count=21)   # over the 20 cap is refused by the request model

def test_synthesize_gemini_recording_replay():
    """tests/fixtures/gd12_54_gemini_real.json = ONE real Gemini call (26/10/2026) of 10 examples, replayed through the same parser + dataset-schema validation."""
    rec = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "gd12_54_gemini_real.json"), encoding="utf-8"))
    async def t(A):
        import evals, evals_ds as D
        d = await as_ws(WS_A, lambda: evals.ds_new(evals.DatasetIn(name=nm("synr"), dataset_type="qa")))
        items = D._synth_items(rec["response"]); assert len(items) == 10 and rec["cost_usd"] < 0.30
        dd = await as_ws(WS_A, lambda: D._need(d["id"]))
        assert len([evals._prep_example(dd, evals.ExampleIn(inputs=i["inputs"], outputs=i["outputs"])) for i in items]) == 10
    with_pool(t)

# ---------------------------------------------------------------- 12.56 from threads
def test_from_threads():
    async def t(A):
        import evals, evals_ds as D
        from psycopg.types.json import Jsonb
        pa = (await A.q("INSERT INTO projects(name,workspace_id) VALUES(%s,%s) RETURNING id", (nm("pa"), WS_A), one=True))["id"]
        pb = (await A.q("INSERT INTO projects(name,workspace_id) VALUES(%s,%s) RETURNING id", (nm("pb"), WS_B), one=True))["id"]
        try:
            for i in range(5):
                u = uuid.uuid4()
                await A.q("INSERT INTO tr_runs(id,trace_id,project_id,name,start_time,inputs,outputs,thread_id) VALUES(%s,%s,%s,'t',now()+%s*interval '1 second',%s,%s,'th-1')",
                          (u, u, pa, i, Jsonb({"message": f"hỏi {i}"}), Jsonb({"answer": f"đáp {i}"})))
            u = uuid.uuid4()   # a thread whose only turn has no answer
            await A.q("INSERT INTO tr_runs(id,trace_id,project_id,name,start_time,inputs,thread_id) VALUES(%s,%s,%s,'t',now(),%s,'th-2')", (u, u, pa, Jsonb({"message": "chưa trả lời"})))
            r = await as_ws(WS_A, lambda: D.ds_from_threads(0, D.FromThreads(project_id=pa, thread_ids=["th-1", "th-2", "th-none"], new_dataset_name=nm("th"))))
            assert r["added"] == 1 and r["results"]["th-1"].startswith("đã thêm") and r["results"]["th-2"].startswith("bỏ qua") and r["results"]["th-none"].startswith("bỏ qua")
            got = await as_ws(WS_A, lambda: evals.ds_get(r["dataset_id"]))
            assert got["dataset"]["dataset_type"] == "chat" and len(got["examples"]) == 1
            e = got["examples"][0]; ms = e["inputs"]["messages"]
            assert len(ms) == 9 and [m["role"] for m in ms] == ["user", "assistant"] * 4 + ["user"] and ms[0]["content"] == "hỏi 0"
            assert e["outputs"]["message"] == {"role": "assistant", "content": "đáp 4"} and e["metadata"] == {"source_thread": "th-1", "project_id": pa, "n_turns": 5}
            r2 = await as_ws(WS_A, lambda: D.ds_from_threads(r["dataset_id"], D.FromThreads(project_id=pa, thread_ids=["th-1"])))
            assert r2["results"]["th-1"] == "đã có" and r2["added"] == 0
            with pytest.raises(HTTPException) as x: await as_ws(WS_A, lambda: D.ds_from_threads(r["dataset_id"], D.FromThreads(project_id=pb, thread_ids=["th-1"])))
            assert x.value.status_code == 404
            with pytest.raises(HTTPException) as x: await as_ws(WS_A, lambda: D.ds_from_threads(r["dataset_id"], D.FromThreads(project_id=pa, thread_ids=[str(i) for i in range(101)])))
            assert x.value.status_code == 400
            with pytest.raises(HTTPException) as x: await as_ws(WS_A, lambda: D.ds_from_threads(r["dataset_id"], D.FromThreads(project_id=pa, thread_ids=["th-1"])), True)
            assert x.value.status_code == 403
        finally:
            await A.q("DELETE FROM tr_runs WHERE project_id = ANY(%s)", ([pa, pb],)); await A.q("DELETE FROM projects WHERE id = ANY(%s)", ([pa, pb],))
    with_pool(t)
