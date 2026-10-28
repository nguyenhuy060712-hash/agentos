"""GĐ8.P part A (24/10/2026): Build "New agent" popup links /builder-chat; workspace Google/M365 settings section + i18n of the
google:*/ms365:* tools; sharing rights on agent-scoped writes (channels, crons, hooks, deployments: share 'use' -> 403); a viewer with a
'use' share can chat with that agent (only that agent, no other writes). Local server :8020, fake LLM => $0.
Throwaway workspace + users; server tests run in file order and share state through the `env` fixture."""
import json, os, re, sys, uuid
import httpx, psycopg, pytest
from test_fixes import B, ADMIN, a  # noqa: F401  (fixtures)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
PW = "phase0-gd8p-local"; SUF = uuid.uuid4().hex[:6]
DICT = json.load(open(os.path.join(os.path.dirname(__file__), "..", "app", "i18n_dict.json"), encoding="utf-8"))

def pg(): return psycopg.connect(PG, autocommit=True)

# ------------------------------------------------------------------ pure unit tests
def test_workspace_tool_strings_have_vi_and_en():
    import workspace_tools as W
    assert set(W.LABELS) == set(W.SPECS)
    for k, (_, desc, _) in W.SPECS.items():
        for s in (desc, W.LABELS[k]):
            assert s in DICT and DICT[s].get("vi") and DICT[s]["en"] == s, (k, s)
            assert re.search(r"[^\x00-\x7f]", DICT[s]["vi"]) or DICT[s]["vi"] != s, s   # really translated

# ------------------------------------------------------------------ server tests
def user(a, name, ws, role="editor"):
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": role, "workspace_id": ws})
    assert r.status_code == 200, r.text
    return r.json()["id"], httpx.Client(base_url=B, auth=(name, PW), timeout=150)

@pytest.fixture(scope="module")
def env(a):
    ws = a.post("/api/admin/workspaces", json={"name": f"p8-w-{SUF}"}).json()["id"]
    e = {"ws": ws, "adm": httpx.Client(base_url=B, auth=ADMIN, headers={"X-Workspace-Id": str(ws)}, timeout=150)}
    for k, role in (("own", "editor"), ("ue", "editor"), ("ed", "editor"), ("vw", "viewer"), ("vw2", "viewer")):
        e[k + "_id"], e[k] = user(a, f"p8_{k}_{SUF}", ws, role)
    r = e["own"].post("/api/agents", json={"name": f"Trợ lý 8P {SUF}", "description": "Agent thử 8.P", "system_prompt": "Bạn là trợ lý."})
    assert r.status_code == 200, r.text
    e["agent"] = r.json()["id"]
    r2 = e["own"].post("/api/agents", json={"name": f"Agent khác 8P {SUF}", "system_prompt": "Bạn là trợ lý."}); assert r2.status_code == 200
    e["agent2"] = r2.json()["id"]
    for k, role in (("ue", "use"), ("ed", "edit"), ("vw", "use")):
        r = e["own"].post(f"/api/agents/{e['agent']}/shares", json={"user_id": e[k + "_id"], "role": role}); assert r.status_code == 200, r.text
    yield e
    for k in ("adm", "own", "ue", "ed", "vw", "vw2"): e[k].close()
    with pg() as c:
        c.execute("DELETE FROM approvals WHERE agent_id IN (SELECT id FROM agents WHERE workspace_id=%s)", (ws,))
        c.execute("DELETE FROM eval_runs WHERE agent_id IN (SELECT id FROM agents WHERE workspace_id=%s)", (ws,))
        c.execute("DELETE FROM channel_bindings WHERE workspace_id=%s", (ws,))
        c.execute("DELETE FROM lg_runs WHERE workspace_id=%s", (ws,)); c.execute("DELETE FROM lg_threads WHERE workspace_id=%s", (ws,))
        c.execute("DELETE FROM agents WHERE workspace_id=%s", (ws,))
        c.execute("DELETE FROM users WHERE username LIKE %s", (f"p8\\_%\\_{SUF}",))

def test_new_agent_popup_links_builder_chat(env):
    h = env["own"].get("/builder", headers={"Accept": "text/html"}).text
    assert 'href="builder-chat"' in h and "Create agent by chat" in h and 'id="ntpl2"' in h
    assert DICT["Create agent by chat"]["vi"] == "Tạo bằng trò chuyện"
    assert env["own"].get("/builder-chat", headers={"Accept": "text/html"}).status_code == 200

def test_workspace_tools_settings(env):
    assert 'gworkspace' in env["adm"].get("/settings", headers={"Accept": "text/html"}).text
    r = env["adm"].get("/api/workspace-tools").json()
    assert r["can_edit"] is True and len(r["all_tools"]) >= 18 and {t["write"] for t in r["all_tools"]} == {True, False}
    assert all(t["label"] and t["description"] for t in r["all_tools"])
    e = env["own"].get("/api/workspace-tools").json(); assert e["can_edit"] is False and "all_tools" in e
    r = env["adm"].post("/api/workspace-tools/presets/google", json={"client_id": f"cid-{SUF}", "client_secret": "s3cret-8p"}); assert r.status_code == 200, r.text
    p = next(x for x in env["adm"].get("/api/oauth/providers").json() if x["name"] == "google")
    assert p["client_id"] == f"cid-{SUF}" and p["has_secret"] and "s3cret-8p" not in json.dumps(p) and "client_secret_enc" not in p   # secret never shown back
    r = env["adm"].post("/api/workspace-tools/presets/google", json={"client_id": f"cid2-{SUF}"}); assert r.status_code == 200   # empty secret keeps the stored one
    assert next(x for x in env["adm"].get("/api/oauth/providers").json() if x["name"] == "google")["has_secret"]
    with pg() as c: c.execute("DELETE FROM oauth_providers WHERE workspace_id=%s", (env["ws"],))

def test_use_share_cannot_write_agent_scoped_things(env):
    aid, ue, own, ed = env["agent"], env["ue"], env["own"], env["ed"]
    assert ue.post(f"/api/agents/{aid}/channels", json={"kind": "fake", "name": "x"}).status_code == 403
    assert ue.post("/api/agent_crons", json={"agent_id": aid, "name": "x", "cron": "0 9 * * *", "message": "hi"}).status_code == 403
    assert ue.post("/api/agent_hooks", json={"agent_id": aid, "name": "x"}).status_code == 403
    assert ue.post("/api/deployments", json={"agent_id": aid, "slug": f"p8u-{SUF}"}).status_code == 403
    # the owner creates one of each; the 'use' share cannot change or delete them, the 'edit' share can
    ch = own.post(f"/api/agents/{aid}/channels", json={"kind": "fake", "name": "Kênh thử"}); assert ch.status_code == 200, ch.text; ch = ch.json()["id"]
    cr = own.post("/api/agent_crons", json={"agent_id": aid, "name": "Lịch thử", "cron": "0 9 * * *", "message": "hi"}); assert cr.status_code == 200, cr.text; cr = cr.json()["id"]
    hk = own.post("/api/agent_hooks", json={"agent_id": aid, "name": "Hook thử"}); assert hk.status_code == 200, hk.text; hk = hk.json()["id"]
    dp = own.post("/api/deployments", json={"agent_id": aid, "slug": f"p8o-{SUF}"}); assert dp.status_code == 200, dp.text; dp = dp.json()["id"]
    assert ue.patch(f"/api/channels/{ch}", json={"status": "disabled"}).status_code == 403
    assert ue.post(f"/api/channels/{ch}/simulate", json={"user_id": "u1", "text": "hi"}).status_code == 403
    assert ue.delete(f"/api/channels/{ch}").status_code == 403
    assert ue.patch(f"/api/agent_crons/{cr}?enabled=false").status_code == 403
    assert ue.post(f"/api/agent_crons/{cr}/run_now").status_code == 403
    assert ue.delete(f"/api/agent_crons/{cr}").status_code == 403
    assert ue.delete(f"/api/agent_hooks/{hk}").status_code == 403
    assert ue.patch(f"/api/deployments/{dp}", json={"status": "paused"}).status_code == 403
    assert ue.delete(f"/api/deployments/{dp}").status_code == 403
    assert ue.get(f"/api/agents/{aid}/channels").status_code == 200                       # reading is still fine
    assert ed.patch(f"/api/channels/{ch}", json={"status": "disabled"}).status_code == 200   # 'edit' share: allowed
    assert ed.delete(f"/api/channels/{ch}").status_code == 200
    assert ed.patch(f"/api/agent_crons/{cr}?enabled=false").status_code == 200
    assert own.delete(f"/api/agent_crons/{cr}").status_code == 200 and own.delete(f"/api/agent_hooks/{hk}").status_code == 200
    assert env["adm"].patch(f"/api/deployments/{dp}", json={"status": "paused"}).status_code == 200   # admin unchanged
    assert own.delete(f"/api/deployments/{dp}").status_code == 200

def _chat(c, aid, text="xin chào"):
    g = f"agent-{aid}"
    r = c.post("/api/v1/threads", json={"metadata": {"source": "chat", "graph_id": g}})
    if r.status_code != 200: return r
    return c.post(f"/api/v1/threads/{r.json()['thread_id']}/runs/wait", json={"assistant_id": g, "input": {"messages": [{"type": "human", "content": text}]}})

def test_viewer_with_use_share_can_chat_only_that_agent(env):
    r = _chat(env["vw"], env["agent"]); assert r.status_code == 200, r.text
    assert r.json()["messages"][-1]["type"] == "ai"
    tid = env["vw"].post("/api/v1/threads/search", json={"metadata": {"source": "chat"}, "limit": 10}); assert tid.status_code == 200 and tid.json()
    r = env["vw"].post(f"/api/v1/threads/{tid.json()[0]['thread_id']}/runs/stream",
                       json={"assistant_id": f"agent-{env['agent']}", "input": {"messages": [{"type": "human", "content": "tiếp"}]}})
    assert r.status_code == 200, r.text                                                     # the chat UI uses the stream endpoint
    assert _chat(env["vw"], env["agent2"]).status_code == 403                                # not shared -> no run
    assert _chat(env["vw2"], env["agent"]).status_code == 403                                # viewer without share
    # no other writes for the viewer
    assert env["vw"].post("/api/agents", json={"name": "x", "system_prompt": "x"}).status_code == 403
    assert env["vw"].put(f"/api/agents/{env['agent']}", json={"name": "x", "system_prompt": "x"}).status_code == 403
    assert env["vw"].post(f"/api/agents/{env['agent']}/channels", json={"kind": "fake"}).status_code == 403
    assert env["vw"].post("/api/deployments", json={"agent_id": env["agent"], "slug": f"p8v-{SUF}"}).status_code == 403
    assert env["vw"].post("/api/agent_crons", json={"agent_id": env["agent"], "name": "x", "cron": "0 9 * * *", "message": "x"}).status_code == 403
    assert env["vw"].post("/api/v1/runs/wait", json={"assistant_id": f"agent-{env['agent2']}", "input": {"messages": []}}).status_code == 403   # stateless runs: not opened
    assert env["vw"].post("/api/v1/assistants", json={"graph_id": f"agent-{env['agent']}"}).status_code == 403
    assert env["own"].post(f"/api/agents/{env['agent']}/channels", json={"kind": "fake", "name": "y"}).status_code == 200   # owner unchanged
