"""GĐ9.5 (24/10/2026): Studio time travel -- fork from any checkpoint (old branch intact), edit state then run (validated), branch tree + compare,
the same through the official langgraph-sdk 0.4.9, attachments (TXT/CSV/MD/JSON), long-term memory editor (editor/admin only, audited, RLS),
and the whole flow on a workspace with checkpoint encryption on (9.7). Local server :8020 + fake LLM => $0."""
import asyncio, base64, json, os, uuid
import httpx, psycopg, pytest
from test_fixes import B, ADMIN, a  # noqa: F401  (fixtures)

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
PW = "phase0-gd95-local"; SUF = uuid.uuid4().hex[:6]
API = B + "/api/v1"

def basic(u, p=PW): return {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}
def pg(): return psycopg.connect(PG, autocommit=True)

def user(a, name, ws, role):
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": role, "workspace_id": ws}); assert r.status_code == 200, r.text
    return r.json()["id"], httpx.Client(base_url=B, auth=(name, PW), timeout=150)

def mkagent(c, name):
    r = c.post("/api/agents", json={"name": name, "description": "Agent thử Studio fork 9.5", "system_prompt": "Bạn là trợ lý."}); assert r.status_code == 200, r.text
    return r.json()["id"]

@pytest.fixture(scope="module")
def env(a):
    ws = a.post("/api/admin/workspaces", json={"name": f"fork-w-{SUF}"}).json()["id"]
    ws2 = a.post("/api/admin/workspaces", json={"name": f"fork-w2-{SUF}"}).json()["id"]
    e = {"ws": ws, "ws2": ws2}
    for k, w, role in (("adm", ws, "admin"), ("ed", ws, "editor"), ("vw", ws, "viewer"), ("oth", ws2, "admin")):
        e[k + "_id"], e[k] = user(a, f"fk_{k}_{SUF}", w, role); e[k + "_name"] = f"fk_{k}_{SUF}"
    e["agent"] = mkagent(e["ed"], f"Fork agent {SUF}"); e["agent2"] = mkagent(e["oth"], f"Fork agent B {SUF}"); e["graph"] = f"agent-{e['agent']}"
    yield e
    for k in ("adm", "ed", "vw", "oth"): e[k].close()
    with pg() as c:
        c.execute("DELETE FROM store WHERE prefix LIKE %s OR prefix LIKE %s", (f"agent.{e['agent']}.%", f"agent.{e['agent2']}.%"))
        c.execute("DELETE FROM audit_log WHERE workspace_id IN (%s,%s) AND method='STUDIO'", (ws, ws2))
        c.execute("DELETE FROM eval_runs WHERE agent_id IN (SELECT id FROM agents WHERE workspace_id IN (%s,%s))", (ws, ws2))
        c.execute("DELETE FROM lg_runs WHERE workspace_id IN (%s,%s)", (ws, ws2)); c.execute("DELETE FROM lg_threads WHERE workspace_id IN (%s,%s)", (ws, ws2))
        c.execute("DELETE FROM chat_thread_files WHERE thread_id LIKE %s", (f"fk-{SUF}%",))
        c.execute("DELETE FROM agents WHERE workspace_id IN (%s,%s)", (ws, ws2))
        c.execute("DELETE FROM users WHERE username LIKE %s", (f"fk\\_%\\_{SUF}",))
        for t in ("checkpoint_blobs", "checkpoint_writes", "checkpoints"): c.execute(f"DELETE FROM {t} WHERE thread_id LIKE %s", (f"a{e['agent']}:fk-{SUF}%",))

# ------------------------------------------------------------------ helpers
def S(aid, tid, tail=""): return f"/api/studio/{aid}/threads/{tid}{tail}"

def fork(c, aid, tid, **b):
    return c.post(S(aid, tid, "/fork"), json=b)

def timeline(c, aid, tid):
    r = c.get(S(aid, tid, "/timeline")); assert r.status_code == 200, r.text; return r.json()

def state_msgs(c, aid, tid, cp):
    r = c.get(S(aid, tid, "/state"), params={"checkpoint_id": cp}); assert r.status_code == 200, r.text
    return [(m["id"], m["type"], m["content"]) for m in r.json()["messages"]]

def two_turns(c, aid, tid):
    assert fork(c, aid, tid, message=f"echo-user one {SUF}").status_code == 200
    r = fork(c, aid, tid, message=f"echo-user two {SUF}"); assert r.status_code == 200, r.text
    assert f"USER<<echo-user two {SUF}>>" in r.json()["done"]["reply"]
    return timeline(c, aid, tid)

def middle(tl):
    """The checkpoint right after the 1st human message was stored: 1 message, next = a real node."""
    c = next(x for x in tl["checkpoints"] if x["n_messages"] == 1 and x["next"]); return c

# ================================================================== fork: new branch, old branch intact
def test_fork_from_middle_checkpoint_new_branch_old_intact(env):
    c, aid, tid = env["ed"], env["agent"], f"fk-{SUF}-a"
    tl = two_turns(c, aid, tid)
    assert len(tl["branches"]) == 1 and tl["branches"][0]["active"] and tl["head"] == tl["branches"][0]["id"]
    assert tl["checkpoints"][0]["node"] and all(x["step"] is not None for x in tl["checkpoints"]) and any(x["diff"]["added"] for x in tl["checkpoints"])
    old_ids = [x["checkpoint_id"] for x in tl["checkpoints"]]; old_leaf = tl["branches"][0]["id"]
    old_msgs = {cid: state_msgs(c, aid, tid, cid) for cid in old_ids}; old_ans = tl["branches"][0]["answer"]
    mid = middle(tl)
    r = fork(c, aid, tid, checkpoint_id=mid["checkpoint_id"]); assert r.status_code == 200, r.text            # "Chạy lại từ đây"
    assert r.json()["done"]["type"] == "done" and not r.json()["done"].get("error") and r.json()["head"] != old_leaf
    t2 = timeline(c, aid, tid)
    assert len(t2["branches"]) == 2 and sum(b["active"] for b in t2["branches"]) == 1
    new_ids = {x["checkpoint_id"] for x in t2["checkpoints"]}
    assert set(old_ids) <= new_ids and len(new_ids) > len(old_ids)                                          # nothing deleted, only added
    for cid in old_ids: assert state_msgs(c, aid, tid, cid) == old_msgs[cid], cid                            # every old checkpoint has identical messages
    old_b = next(b for b in t2["branches"] if b["id"] == old_leaf); new_b = next(b for b in t2["branches"] if b["id"] != old_leaf)
    assert old_b["answer"] == old_ans and not old_b["active"] and new_b["active"]
    assert new_b["fork_from"] == mid["checkpoint_id"] and mid["checkpoint_id"] in new_b["path"] and mid["checkpoint_id"] in old_b["path"]
    assert next(x for x in t2["checkpoints"] if x["checkpoint_id"] == mid["checkpoint_id"])["children"] == 2   # the fork point has two children
    cmp = c.get(S(aid, tid, "/compare"), params={"a": old_leaf, "b": new_b["id"]}); assert cmp.status_code == 200, cmp.text
    j = cmp.json(); assert j["a"]["answer"] == old_ans and j["b"]["answer"] and j["common_messages"] >= 1 and j["only_a"]

def test_final_checkpoint_needs_message_or_edit_and_unknown_checkpoint_404(env):
    c, aid, tid = env["ed"], env["agent"], f"fk-{SUF}-a"
    tl = timeline(c, aid, tid); last = tl["branches"][0]
    fin = next(x for x in tl["checkpoints"] if not x["next"])
    r = fork(c, aid, tid, checkpoint_id=fin["checkpoint_id"]); assert r.status_code == 422, r.text
    n0 = len(timeline(c, aid, tid)["checkpoints"])
    assert fork(c, aid, tid, checkpoint_id="1ef00000-0000-6000-8000-000000000000", message="x").status_code == 404
    assert c.post(S(aid, tid, "/activate"), json={"checkpoint_id": "1ef00000-0000-6000-8000-000000000000"}).status_code == 404
    assert c.get(S(aid, tid, "/raw"), params={"checkpoint_id": "nope"}).status_code == 404
    assert fork(c, aid, tid).status_code == 422                                                                # nothing to run
    assert len(timeline(c, aid, tid)["checkpoints"]) == n0 and last                                           # refused runs write nothing

def test_activate_switches_current_branch(env):
    c, aid, tid = env["ed"], env["agent"], f"fk-{SUF}-a"
    tl = timeline(c, aid, tid); old = next(b for b in tl["branches"] if not b["active"])
    r = c.post(S(aid, tid, "/activate"), json={"checkpoint_id": old["id"]}); assert r.status_code == 200 and r.json()["changed"], r.text
    t2 = timeline(c, aid, tid); act = next(b for b in t2["branches"] if b["active"])
    assert act["answer"] == old["answer"] and len(t2["branches"]) == 2                                      # same answer, no extra branch

# ================================================================== update_state edit + validation
def test_edit_state_changes_forked_result_and_validation(env):
    c, aid, tid = env["ed"], env["agent"], f"fk-{SUF}-b"
    tl = two_turns(c, aid, tid); mid = middle(tl); old_leaf = tl["branches"][0]["id"]; old_ans = tl["branches"][0]["answer"]
    raw = c.get(S(aid, tid, "/raw"), params={"checkpoint_id": mid["checkpoint_id"]}).json()
    hm = raw["values"]["messages"][0]; assert hm["type"] == "human" and hm["id"]
    n0 = len(timeline(c, aid, tid)["checkpoints"])
    bad = [({"nope": 1}, "unknown state key"), ({"messages": "x"}, "non-empty list"), ({"messages": [{"type": "alien", "content": "x"}]}, "type must be"),
           ({"messages": [{"type": "remove", "id": "does-not-exist"}]}, "remove needs"), ({"messages": [{"type": "tool", "content": "x"}]}, "tool_call_id"),
           ({"messages": [{"type": "human", "content": 5}]}, "content must be"), ({"messages": [{"type": "ai", "content": "", "tool_calls": [{"args": {}}]}]}, "tool_calls")]
    for vals, frag in bad:
        v = c.post(S(aid, tid, "/validate"), json={"checkpoint_id": mid["checkpoint_id"], "values": vals}).json()
        assert not v["ok"] and any(frag in e for e in v["errors"]), (vals, v)
        r = fork(c, aid, tid, checkpoint_id=mid["checkpoint_id"], values=vals); assert r.status_code == 422 and r.json()["detail"]["errors"], (vals, r.text)
    assert c.post(S(aid, tid, "/validate"), json={"checkpoint_id": mid["checkpoint_id"], "values": {"messages": [hm]}, "as_node": "ghost"}).json()["ok"] is False
    assert len(timeline(c, aid, tid)["checkpoints"]) == n0                                                  # a rejected edit writes nothing
    ok = c.post(S(aid, tid, "/validate"), json={"checkpoint_id": mid["checkpoint_id"], "values": {"messages": [{**hm, "content": "x"}]}}).json(); assert ok["ok"], ok
    edited = f"echo-user EDITED {SUF}"
    r = fork(c, aid, tid, checkpoint_id=mid["checkpoint_id"], values={"messages": [{**hm, "content": edited}]}); assert r.status_code == 200, r.text   # "Sửa trạng thái rồi chạy"
    assert f"USER<<{edited}>>" in r.json()["done"]["reply"] and r.json()["from_checkpoint"] != mid["checkpoint_id"]
    t2 = timeline(c, aid, tid); assert len(t2["branches"]) == 2
    nb = next(b for b in t2["branches"] if b["id"] != old_leaf); assert f"USER<<{edited}>>" in nb["answer"]
    upd = next(x for x in t2["checkpoints"] if x["checkpoint_id"] == r.json()["from_checkpoint"])
    assert upd["source"] == "update" and upd["diff"]["changed"] and upd["diff"]["changed"][0]["before"] == f"echo-user one {SUF}"   # state diff vs parent
    assert next(b for b in t2["branches"] if b["id"] == old_leaf)["answer"] == old_ans                       # old branch untouched
    with pg() as cx: n = cx.execute("SELECT count(*) FROM audit_log WHERE method='STUDIO' AND workspace_id=%s AND detail LIKE %s", (env["ws"], f"studio.update_state agent={aid} thread={tid}%")).fetchone()[0]
    assert n == 1

def test_edit_state_on_final_checkpoint_with_new_message(env):
    c, aid, tid = env["ed"], env["agent"], f"fk-{SUF}-c"
    tl = two_turns(c, aid, tid); old_leaf = tl["branches"][0]["id"]
    fin = next(x for x in tl["checkpoints"] if not x["next"] and x["n_messages"] == 2)                       # end of turn 1 (a past, final checkpoint)
    r = fork(c, aid, tid, checkpoint_id=fin["checkpoint_id"], values={"messages": [{"type": "system", "content": "Be brief."}]}, message=f"echo-user three {SUF}")
    assert r.status_code == 200, r.text
    assert f"USER<<echo-user three {SUF}>>" in r.json()["done"]["reply"]
    t2 = timeline(c, aid, tid); assert len(t2["branches"]) == 2 and any(b["id"] == old_leaf for b in t2["branches"])
    nb = next(b for b in t2["branches"] if b["id"] != old_leaf); assert nb["active"] and nb["n_messages"] == 5   # human, ai, system, human, ai
    # continuing the CURRENT leaf (not a past checkpoint) just grows that branch
    r2 = fork(c, aid, tid, checkpoint_id=nb["id"], message=f"echo-user four {SUF}"); assert r2.status_code == 200, r2.text
    assert len(timeline(c, aid, tid)["branches"]) == 2

def test_viewer_and_foreign_workspace_cannot_fork(env):
    aid, tid = env["agent"], f"fk-{SUF}-a"
    assert env["vw"].post(S(aid, tid, "/fork"), json={"message": "x"}).status_code == 403
    assert env["vw"].post(S(aid, tid, "/validate"), json={"checkpoint_id": "x", "values": {"messages": [{"type": "human", "content": "x"}]}}).status_code == 403
    assert env["oth"].get(S(aid, tid, "/timeline")).status_code == 404
    assert env["oth"].post(S(aid, tid, "/fork"), json={"message": "x"}).status_code == 404

# ================================================================== langgraph-sdk fork (Agent Server API)
def human(t): return {"messages": [{"type": "human", "content": t}]}

def test_sdk_client_fork_from_checkpoint(env):
    from langgraph_sdk import get_client
    async def go():
        cl = get_client(url=API, headers=basic(env["ed_name"]), timeout=120); g = env["graph"]
        tid = (await cl.threads.create(metadata={"graph_id": g, "src": SUF}))["thread_id"]
        await cl.runs.wait(tid, g, input=human(f"echo-user sdk one {SUF}"))
        out = await cl.runs.wait(tid, g, input=human(f"echo-user sdk two {SUF}")); assert f"USER<<echo-user sdk two {SUF}>>" in json.dumps(out)
        hist = await cl.threads.get_history(tid, limit=50)
        old_ids = [h["checkpoint"]["checkpoint_id"] for h in hist]; old_head = hist[0]["checkpoint"]["checkpoint_id"]
        old_msgs = {h["checkpoint"]["checkpoint_id"]: [m["content"] for m in h["values"].get("messages", [])] for h in hist}
        mid = next(h for h in hist if len(h["values"].get("messages", [])) == 1 and h["next"])
        page = await cl.threads.get_history(tid, limit=50, before={"configurable": {"checkpoint_id": hist[0]["checkpoint"]["checkpoint_id"]}})
        assert [p["checkpoint"]["checkpoint_id"] for p in page] == old_ids[1:]                                # `before` honoured
        hm = mid["values"]["messages"][0]; edited = f"echo-user SDKEDIT {SUF}"
        new = await cl.threads.update_state(tid, {"messages": [{**hm, "content": edited}]}, checkpoint=mid["checkpoint"], as_node="__start__")
        new_cp = new["checkpoint"]["checkpoint_id"]; assert new_cp not in old_ids
        st = await cl.threads.get_state(tid, checkpoint={"checkpoint_id": new_cp}); assert st["values"]["messages"][0]["content"] == edited and st["next"]
        res = await cl.runs.wait(tid, g, input=None, checkpoint={"checkpoint_id": new_cp})                    # run from the edited checkpoint = fork
        assert f"USER<<{edited}>>" in json.dumps(res)
        hist2 = await cl.threads.get_history(tid, limit=100); ids2 = [h["checkpoint"]["checkpoint_id"] for h in hist2]
        assert set(old_ids) <= set(ids2) and len(ids2) > len(old_ids)
        for h in hist2:
            if h["checkpoint"]["checkpoint_id"] in old_msgs: assert [m["content"] for m in h["values"].get("messages", [])] == old_msgs[h["checkpoint"]["checkpoint_id"]]
        assert hist2[0]["checkpoint"]["checkpoint_id"] != old_head
        old_state = await cl.threads.get_state(tid, checkpoint_id=old_head); assert f"USER<<echo-user sdk two {SUF}>>" in json.dumps(old_state["values"])   # old branch still readable
        # plain `checkpoint_id` (deprecated kw) + a fork run with new input on top of the old leaf
        res2 = await cl.runs.wait(tid, g, input=human(f"echo-user sdk three {SUF}"), checkpoint_id=old_head)
        assert f"USER<<echo-user sdk three {SUF}>>" in json.dumps(res2)
    asyncio.run(go())

# ================================================================== attachments
def upload(c, name, data):
    r = c.post("/api/files", files={"file": (name, data)}); return r

def test_attachments_txt_csv_md_json_reach_the_run(env):
    c, aid = env["ed"], env["agent"]
    samples = {"ghi-chu.txt": (b"Bao cao doanh thu: MARK-TXT-%s" % SUF.encode(), f"MARK-TXT-{SUF}"), "so-lieu.csv": (b"thang,doanh_thu\n10,MARK-CSV-%s\n" % SUF.encode(), f"MARK-CSV-{SUF}"),
               "mo-ta.md": (b"# Tieu de\n\nMARK-MD-%s\n" % SUF.encode(), f"MARK-MD-{SUF}"), "cau-hinh.json": (b'{"khoa": "MARK-JSON-%s"}' % SUF.encode(), f"MARK-JSON-{SUF}")}
    for name, (data, mark) in samples.items():
        up = upload(c, name, data); assert up.status_code == 200, up.text
        tid = f"fk-{SUF}-f-{name[:3]}"
        r = fork(c, aid, tid, message=f"echo-user doc {name}", files=[up.json()["id"]]); assert r.status_code == 200, r.text
        assert mark in r.json()["done"]["reply"] and f'<attachment name="{name}"' in r.json()["done"]["reply"], r.json()["done"]["reply"]   # the model request carried the text
        human_msg = next(m for m in c.get(S(aid, tid, "/raw"), params={"checkpoint_id": timeline(c, aid, tid)["head"]}).json()["values"]["messages"] if m["type"] == "human")
        assert any(b.get("type") == "file" for b in human_msg["content"]) and mark not in json.dumps(human_msg)                       # state keeps the small block only
    # fork a past checkpoint WITH a new attachment
    tid = f"fk-{SUF}-f-ghi"; tl = timeline(c, aid, tid); fin = next(x for x in tl["checkpoints"] if not x["next"])
    up = upload(c, "them.txt", b"EXTRA-%s" % SUF.encode()).json()
    r = fork(c, aid, tid, checkpoint_id=fin["checkpoint_id"], message="echo-user second doc", files=[up["id"]]); assert r.status_code == 200 and f"EXTRA-{SUF}" in r.json()["done"]["reply"]
    # plain Studio /run accepts files too
    r = c.post(S(aid, f"fk-{SUF}-f-run", "/run"), json={"message": "echo-user run", "files": [up["id"]]}); assert r.status_code == 200 and f"EXTRA-{SUF}" in r.json()["done"]["reply"]

def test_attachment_limits_and_access(env):
    c, aid = env["ed"], env["agent"]
    assert upload(c, "x.exe", b"MZ\x90\x00").status_code in (400, 413, 415, 422)
    assert upload(c, "bad.json", b"{not json").status_code in (400, 415, 422)
    one = upload(c, "a.txt", f"t-{SUF}".encode()); assert one.status_code == 200, one.text; ids = [one.json()["id"]] * 6
    assert fork(c, aid, f"fk-{SUF}-lim", message="echo-user many", files=ids).status_code == 422             # > 5 files (pydantic) -- same cap as chat
    assert fork(c, aid, f"fk-{SUF}-lim", message="echo-user many", files=["00000000-0000-0000-0000-000000000000"]).status_code in (404, 422)
    mine = upload(c, "mine.txt", b"private").json()["id"]
    assert fork(env["oth"], env["agent2"], f"fk-{SUF}-oth", message="echo-user steal", files=[mine]).status_code in (404, 422)   # another workspace cannot attach my file

# ================================================================== memory editor
def seed_mem(ns_prefix, key, text):
    with pg() as c:
        c.execute("INSERT INTO store(prefix,key,value) VALUES(%s,%s,%s) ON CONFLICT(prefix,key) DO UPDATE SET value=EXCLUDED.value", (ns_prefix, key, json.dumps({"text": text})))

def test_memory_edit_delete_audited_and_rls(env):
    c, adm, aid = env["ed"], env["adm"], env["agent"]
    uid = env["ed_id"]; seed_mem(f"agent.{aid}.u{uid}.memories", "m1", f"Thích cà phê {SUF}"); seed_mem(f"agent.{aid}.memories", "s1", f"Chung {SUF}")
    seed_mem(f"agent.{aid}.u{env['adm_id']}.memories", "m9", "của admin")
    ns = c.get(f"/api/studio/{aid}/memory/namespaces"); assert ns.status_code == 200, ns.text
    toks = {x["ns"]: x for x in ns.json()["items"]}; assert toks[f"u{uid}"]["count"] == 1 and toks["shared"]["count"] == 1 and toks[f"u{env['adm_id']}"]["label"] == env["adm_name"]
    items = c.get(f"/api/studio/{aid}/memory", params={"ns": f"u{uid}"}).json()["items"]; assert [(i["key"], i["text"]) for i in items] == [("m1", f"Thích cà phê {SUF}")]
    assert c.put(f"/api/studio/{aid}/memory/m1", params={"ns": f"u{uid}"}, json={"text": f"Thích trà {SUF}"}).status_code == 200
    assert c.get(f"/api/studio/{aid}/memory", params={"ns": f"u{uid}"}).json()["items"][0]["text"] == f"Thích trà {SUF}"
    assert c.put(f"/api/studio/{aid}/memory/zzz", params={"ns": f"u{uid}"}, json={"text": "x"}).status_code == 404
    assert c.put(f"/api/studio/{aid}/memory/m1", params={"ns": "evil;drop"}, json={"text": "x"}).status_code == 422
    assert c.put(f"/api/studio/{aid}/memory/m1", params={"ns": f"u{uid}"}, json={"text": ""}).status_code == 422
    assert c.delete(f"/api/studio/{aid}/memory/m1", params={"ns": f"u{uid}"}).status_code == 200
    assert c.get(f"/api/studio/{aid}/memory", params={"ns": f"u{uid}"}).json()["items"] == []
    assert c.delete(f"/api/studio/{aid}/memory/m1", params={"ns": f"u{uid}"}).status_code == 404
    with pg() as cx: rows = [r[0] for r in cx.execute("SELECT detail FROM audit_log WHERE method='STUDIO' AND workspace_id=%s AND detail LIKE 'studio.memory.%%' ORDER BY id", (env["ws"],)).fetchall()]
    assert [r.split(" ")[0] for r in rows] == ["studio.memory.edit", "studio.memory.delete"] and all(f"agent={aid} ns=u{uid} key=m1" in r for r in rows) and f"{SUF}" not in " ".join(rows)   # who/what, never the text
    # roles + RLS
    assert env["vw"].get(f"/api/studio/{aid}/memory/namespaces").status_code == 403
    assert env["vw"].get(f"/api/studio/{aid}/memory", params={"ns": "shared"}).status_code == 403
    assert env["vw"].delete(f"/api/studio/{aid}/memory/s1", params={"ns": "shared"}).status_code == 403
    assert env["oth"].get(f"/api/studio/{aid}/memory/namespaces").status_code == 404
    assert env["oth"].get(f"/api/studio/{aid}/memory", params={"ns": "shared"}).status_code == 404
    assert env["oth"].delete(f"/api/studio/{aid}/memory/s1", params={"ns": "shared"}).status_code == 404
    assert adm.get(f"/api/studio/{aid}/memory", params={"ns": "shared"}).json()["items"][0]["text"] == f"Chung {SUF}"
    # a user id from another workspace is not reachable even on my agent
    assert c.get(f"/api/studio/{aid}/memory", params={"ns": f"u{env['oth_id']}"}).status_code == 404

# ================================================================== workspace with checkpoint encryption on (9.7)
def test_fork_flow_on_encrypted_checkpoints(env):
    adm, aid = env["adm"], env["agent"]
    r = adm.put("/api/crypto/settings", json={"checkpoints": True, "traces": False}); assert r.status_code == 200, r.text
    try:
        import time; time.sleep(1)
        tid = f"fk-{SUF}-enc"; secret = f"BIMAT-FORK-{SUF}"
        tl = two_turns(adm, aid, tid); assert len(tl["checkpoints"]) >= 4
        with pg() as cx:
            blobs = [bytes(b or b"") for (b,) in cx.execute("SELECT blob FROM checkpoint_blobs WHERE thread_id=%s", (f"a{aid}:{tid}",)).fetchall()]
            blobs += [bytes(b or b"") for (b,) in cx.execute("SELECT blob FROM checkpoint_writes WHERE thread_id=%s", (f"a{aid}:{tid}",)).fetchall()]
        assert blobs and not any(f"echo-user one {SUF}".encode() in b for b in blobs), "checkpoint must be encrypted at rest"
        mid = middle(tl); old_leaf = tl["branches"][0]["id"]; old_ans = tl["branches"][0]["answer"]
        raw = adm.get(S(aid, tid, "/raw"), params={"checkpoint_id": mid["checkpoint_id"]}).json()["values"]["messages"][0]
        r = fork(adm, aid, tid, checkpoint_id=mid["checkpoint_id"], values={"messages": [{**raw, "content": f"echo-user {secret}"}]}); assert r.status_code == 200, r.text
        assert f"USER<<echo-user {secret}>>" in r.json()["done"]["reply"]
        t2 = timeline(adm, aid, tid); assert len(t2["branches"]) == 2 and next(b for b in t2["branches"] if b["id"] == old_leaf)["answer"] == old_ans
        with pg() as cx:
            rows = cx.execute("SELECT blob FROM checkpoint_blobs WHERE thread_id=%s", (f"a{aid}:{tid}",)).fetchall()
        assert not any(secret.encode() in bytes(b or b"") for (b,) in rows)                                  # the edited state is encrypted too
        assert adm.post(S(aid, tid, "/activate"), json={"checkpoint_id": old_leaf}).status_code == 200
        assert next(b for b in timeline(adm, aid, tid)["branches"] if b["active"])["answer"] == old_ans
    finally:
        adm.put("/api/crypto/settings", json={"checkpoints": False, "traces": False})
