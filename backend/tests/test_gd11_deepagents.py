"""GĐ11.63 (26/10/2026): deepagents 0.7.30 -> 0.7.32 (+ langchain 1.4.7, langchain-core 1.7.0, langchain-protocol 0.0.20, langsmith 0.14.12).
Covers: version pin, CompositeBackend trailing-slash routes (/pack/ and a future /memories/), checkpoint serde round trip of real agent state through
EncryptedSerializer (plain and workspace-encrypted, wrong workspace refused), `lc_agent_name` copied into sub-agent run metadata + the "Sub-agent" filter
(`GET /api/obs/subagents`, `/api/obs/runs?lc_agent_name=`, `query_runs` tool), refused cross-workspace / unauthenticated.
Server tests use the server in tests/test_fixes.py (B), fake LLM => $0. Throwaway workspace + users + agent."""
import base64, os, sys, uuid
from importlib.metadata import version
import httpx, psycopg, pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")
os.environ.setdefault("AOS_ENV", "")
PG = os.environ.setdefault("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
from test_fixes import B, ADMIN, a, thread  # noqa: F401,E402  (fixtures)

PW = "phase0-gd1163-local"; SUF = uuid.uuid4().hex[:6]
def pg(): return psycopg.connect(PG, autocommit=True)

# ------------------------------------------------------------------ pin
def test_versions_pinned():
    assert version("deepagents") == "0.7.32"
    assert version("langchain-core") == "1.7.0" and version("langsmith") == "0.14.12"
    lock = open(os.path.join(os.path.dirname(__file__), "..", "requirements.lock"), encoding="utf-8").read().splitlines()
    assert "deepagents==0.7.32" in lock and "langchain-core==1.7.0" in lock
    assert any(l.startswith("deepagents==0.7.32") for l in open(os.path.join(os.path.dirname(__file__), "..", "requirements.in"), encoding="utf-8").read().splitlines())

# ------------------------------------------------------------------ CompositeBackend routes (trailing slash behaviour changed in 0.7.29)
def _comp():
    import context_hub as H
    from deepagents.backends import CompositeBackend, StateBackend
    pack = H.PackBackend("p@abc", {"AGENTS.md": "# Quy tắc\n", "skills/bao-gia/SKILL.md": "---\nname: bao-gia\ndescription: d\n---\nx\n", "policies/g.md": "t\n"}, "abc")
    mem = H.PackBackend("m@abc", {"notes.md": "ghi chú\n"}, "abc")   # stand-in for the future read-only /memories/... route (same backend class, other mount)
    return CompositeBackend(default=StateBackend(), routes={"/pack/": pack, "/memories/": mem})

def test_composite_routes_trailing_slash():
    comp = _comp()
    for p in ("/pack", "/pack/"):   # mount point with and without the trailing slash lists the same mounted root
        assert {e["path"] for e in comp.ls(p).entries} == {"/pack/AGENTS.md", "/pack/skills", "/pack/policies"}, p
    for p in ("/pack/skills", "/pack/skills/"): assert [e["path"] for e in comp.ls(p).entries] == ["/pack/skills/bao-gia"], p
    for p in ("/memories", "/memories/"): assert [e["path"] for e in comp.ls(p).entries] == ["/memories/notes.md"], p
    assert "Quy tắc" in comp.read("/pack/AGENTS.md").file_data["content"] and "ghi chú" in comp.read("/memories/notes.md").file_data["content"]
    assert sorted(m["path"] for m in comp.glob("*.md", "/pack").matches) == ["/pack/AGENTS.md", "/pack/policies/g.md", "/pack/skills/bao-gia/SKILL.md"]
    assert [m["path"] for m in comp.grep("ghi chú", "/memories/").matches] == ["/memories/notes.md"]

def test_composite_routes_are_read_only_and_do_not_leak():
    comp = _comp()
    for r in (comp.write("/pack/x.md", "hack"), comp.write("/memories/x.md", "hack"), comp.write("/pack", "hack"), comp.edit("/memories/notes.md", "ghi", "xoá"),
              comp.delete("/pack/AGENTS.md")):
        assert r.error and "Read-only" in r.error, r
    assert "ghi chú" in comp.read("/memories/notes.md").file_data["content"]
    # a path that merely starts with the same letters, or climbs out of the mount, must not reach a mounted backend
    for p in ("/packet/AGENTS.md", "/pack/../memories/notes.md", "/pack/../../etc/passwd"):
        try: r = comp.read(p)
        except RuntimeError: continue            # fell through to the (graph-only) StateBackend: not served from a mount
        assert not (r.file_data or {}).get("content", "").strip() or "Quy tắc" not in r.file_data["content"], p
        assert r.error or not r.file_data, p

# ------------------------------------------------------------------ checkpoint serde: real agent state through EncryptedSerializer
def _state():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from deepagents.backends.utils import create_file_data
    return {"messages": [HumanMessage(content="xin chào ✓", id="h1"),
                         AIMessage(content="", id="a1", tool_calls=[{"name": "write_todos", "args": {"todos": [{"content": "việc 1", "status": "pending"}]}, "id": "c1", "type": "tool_call"}],
                                   usage_metadata={"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}),
                         ToolMessage(content="ok", tool_call_id="c1", name="write_todos", id="t1")],
            "files": {"/notes/a.md": create_file_data("nội dung tiếng Việt\n"), "/bin/x.png": create_file_data(base64.b64encode(b"\x89PNG\x00\xff").decode(), encoding="base64")},
            "todos": [{"content": "việc 1", "status": "in_progress"}, {"content": "việc 2", "status": "completed"}],
            "skills_metadata": [{"path": "/skills/main/bao-gia/SKILL.md", "name": "bao-gia", "description": "Lập báo giá", "license": None, "compatibility": None, "metadata": {}, "allowed_tools": []}]}

def _same(x, y):
    assert type(x) is type(y) and x == y

def test_serde_roundtrip_plain():
    import aos_crypto
    ser = aos_crypto.make_serde(); st = _state()
    typ, blob = ser.dumps_typed(st)
    assert typ == "msgpack+none" or typ.endswith("+none"), typ        # no workspace in context => cipher "none" (bytes unchanged)
    back = ser.loads_typed((typ, blob))
    assert set(back) == set(st)
    for k in ("files", "todos", "skills_metadata"): assert back[k] == st[k], k
    for m1, m2 in zip(st["messages"], back["messages"]): _same(m1, m2)

def _keys(mp):
    import crypto
    mp.delenv("AOS_SECRET_KEY", raising=False); mp.delenv("AOS_SECRET_KEYS_OLD", raising=False); mp.delenv("AOS_ACTIVE_KID", raising=False)
    mp.setenv("AOS_SECRET_KEYS", "k1:" + base64.b64encode(os.urandom(32)).decode()); crypto.reset()

def test_serde_roundtrip_encrypted_and_wrong_workspace_refused(monkeypatch):
    import aos_crypto, crypto
    _keys(monkeypatch)
    cur = {"ws": 9711}
    monkeypatch.setattr(aos_crypto, "_cur_ws", lambda: cur["ws"])
    monkeypatch.setattr(aos_crypto, "flags", lambda ws: {"checkpoints": True})
    try:
        ser = aos_crypto.make_serde(); st = _state()
        typ, blob = ser.dumps_typed(st)
        assert typ.endswith("+" + aos_crypto.CP_TYPE) and "nội dung".encode() not in blob and "xin chào".encode() not in blob, typ
        back = ser.loads_typed((typ, blob))
        for k in ("files", "todos", "skills_metadata"): assert back[k] == st[k], k
        for m1, m2 in zip(st["messages"], back["messages"]): _same(m1, m2)
        cur["ws"] = 9712                                           # another workspace must not read this checkpoint
        with pytest.raises(crypto.CryptoError): ser.loads_typed((typ, blob))
        tampered = bytearray(blob); tampered[-3] ^= 1
        cur["ws"] = 9711
        with pytest.raises(Exception): ser.loads_typed((typ, bytes(tampered)))
    finally:
        pg().execute("DELETE FROM crypto_deks WHERE workspace_id BETWEEN 9711 AND 9712"); crypto.reset()

def test_serde_no_unregistered_type_warnings(caplog):
    """No custom type of ours falls outside the serde allow-list: loading real state must not log 'unregistered type' / 'blocked' messages
    (so `allowed_json_modules` / `allowed_msgpack_modules` are not needed on 0.7.32 / checkpoint 4.3)."""
    import logging, aos_crypto
    caplog.set_level(logging.DEBUG)
    ser = aos_crypto.make_serde(); ser.loads_typed(ser.dumps_typed(_state()))
    bad = [r.getMessage() for r in caplog.records if any(w in r.getMessage().lower() for w in ("unregistered", "blocked", "not allowed", "allowed_msgpack_modules"))]
    assert not bad, bad

# ------------------------------------------------------------------ lc_agent_name: sub-agent runs filterable by name
def _user(a, name, ws, role="editor"):
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": role, "workspace_id": ws}); assert r.status_code == 200, r.text
    return httpx.Client(base_url=B, auth=(name, PW), timeout=90)

@pytest.fixture(scope="module")
def env(a):
    wb = a.post("/api/admin/workspaces", json={"name": f"d63-b-{SUF}"}).json()["id"]
    other = _user(a, f"d63_oth_{SUF}", wb)
    ag = a.post("/api/agents", json={"name": f"Điều phối d63 {SUF}", "model": "gpt-5.4-nano", "tools": [],
                                     "subagents": [{"name": "researcher", "description": "Nghiên cứu theo yêu cầu"}]}); assert ag.status_code == 200, ag.text
    aid = ag.json()["id"]; gid = f"agent-{aid}"
    tid = thread(a, gid)
    r = a.post(f"/api/v1/threads/{tid}/runs/wait", json={"assistant_id": gid, "input": {"messages": [{"type": "human", "content": "delegate a short market report"}]}}); assert r.status_code == 200, r.text
    pid = next(p["id"] for p in a.get("/api/obs/projects").json() if p["name"] == "agents")
    trace = next(r["trace_id"] for r in a.get("/api/obs/runs", params={"project_id": pid, "limit": 200}).json() if r["thread_id"] == tid)
    yield {"aid": aid, "tid": tid, "pid": pid, "wb": wb, "other": other, "trace": trace}
    other.close()
    with pg() as c:
        c.execute("DELETE FROM lg_runs WHERE workspace_id=%s", (wb,)); c.execute("DELETE FROM lg_threads WHERE workspace_id=%s", (wb,))
        c.execute("DELETE FROM users WHERE username=%s", (f"d63_oth_{SUF}",))
    a.delete(f"/api/agents/{aid}")

def _trace_runs(a, env):
    root = next(r for r in a.get("/api/obs/runs", params={"project_id": env["pid"], "limit": 200}).json() if r["thread_id"] == env["tid"])
    return a.get(f"/api/obs/traces/{root['trace_id']}").json()["runs"], root

def test_sub_agent_runs_carry_lc_agent_name(a, env):
    runs, root = _trace_runs(a, env)
    sub = [r for r in runs if (r["metadata"] or {}).get("lc_agent_name") == "researcher"]
    assert sub, [(r["name"], r["metadata"]) for r in runs]
    assert any(r["run_type"] == "llm" for r in sub), "the sub-agent's own model call is tagged"
    main = [r for r in runs if r["id"] == root["id"] or (r["metadata"] or {}).get("lc_agent_name") is None]
    assert main and not any((r["metadata"] or {}).get("lc_agent_name") == f"agent_{env['aid']}" for r in runs), "main agent is not reported as a sub-agent"
    assert any(r["run_type"] == "llm" for r in main), "main agent runs stay untagged"

def test_runs_filter_by_sub_agent_name(a, env):
    p = {"project_id": env["pid"], "limit": 500}
    assert "researcher" in a.get("/api/obs/subagents", params={"project_id": env["pid"]}).json()
    got = a.get("/api/obs/runs", params={**p, "lc_agent_name": "researcher"}); assert got.status_code == 200, got.text
    _, root = _trace_runs(a, env)
    rows = [r for r in got.json() if r["trace_id"] == root["trace_id"]]
    assert rows and all((r["metadata"] or {}).get("lc_agent_name") == "researcher" for r in got.json()), "only sub-agent runs"
    assert any(r["parent_id"] is not None for r in rows), "child runs are returned even though root_only defaults to true"
    assert a.get("/api/obs/runs", params={**p, "lc_agent_name": "no-such-sub-agent-" + SUF}).json() == []
    # the filter value is a bound parameter, not SQL
    inj = a.get("/api/obs/runs", params={**p, "lc_agent_name": "x' OR '1'='1"}); assert inj.status_code == 200 and inj.json() == []

def _mcp_call(auth, tool, **args):
    import asyncio
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    hdr = {"Authorization": "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()}
    async def go():
        async with httpx.AsyncClient(headers=hdr, timeout=60) as hc, streamable_http_client(B + "/mcp/aos", http_client=hc) as (r, w):
            async with ClientSession(r, w) as sess:
                await sess.initialize(); return await sess.call_tool(tool, args)
    r = asyncio.run(go()); return r.is_error, "".join(c.text for c in r.content if getattr(c, "type", "") == "text")

def test_query_runs_tool_lc_agent_name(env):
    import json
    err, txt = _mcp_call(ADMIN, "query_runs", project=str(env["pid"]), lc_agent_name="researcher", limit=100)
    assert not err, txt
    d = json.loads(txt); mine = [i for i in d["items"] if i["trace_id"] == env["trace"]]
    assert mine, "runs inside the sub-agent are listed (children, not only roots)"
    err, txt = _mcp_call(ADMIN, "query_runs", project=str(env["pid"]), lc_agent_name="no-such-sub-agent-" + SUF, limit=100)
    assert not err and json.loads(txt)["items"] == []
    err, txt = _mcp_call(ADMIN, "query_runs", project=str(env["pid"]), limit=100)   # default unchanged: roots only
    assert not err and all(i["type"] != "llm" or i["name"] for i in json.loads(txt)["items"])

def test_query_runs_tool_refuses_other_workspace_project(env):
    err, txt = _mcp_call((f"d63_oth_{SUF}", PW), "query_runs", project=str(env["pid"]), lc_agent_name="researcher")
    assert err and "Project not found" in txt, txt

# ------------------------------------------------------------------ refused: other workspace / no login
def test_other_workspace_cannot_list_or_filter_sub_agents(env):
    c = env["other"]
    r = c.get("/api/obs/subagents", params={"project_id": env["pid"]})
    assert r.status_code == 404, r.text                                   # project of workspace A is invisible to workspace B
    r = c.get("/api/obs/runs", params={"project_id": env["pid"], "lc_agent_name": "researcher", "limit": 500})
    assert r.status_code in (403, 404) or (r.status_code == 200 and r.json() == []), r.text
    assert "researcher" not in c.get("/api/obs/subagents", params={"project_id": 0}).text

def test_unauthenticated_refused(env):
    with httpx.Client(base_url=B, timeout=30) as anon:
        assert anon.get("/api/obs/subagents", params={"project_id": env["pid"]}).status_code in (401, 403)
        assert anon.get("/api/obs/runs", params={"project_id": env["pid"], "lc_agent_name": "researcher"}).status_code in (401, 403)

def test_viewer_in_own_workspace_sees_no_foreign_traces(a, env):
    """A viewer of workspace B (the throwaway workspace) gets an empty sub-agent list for its own, empty, projects."""
    wb = env["wb"]
    v = _user(a, f"d63_vw_{SUF}", wb, "viewer")
    try:
        mine = v.get("/api/obs/projects").json()
        assert all(p["id"] != env["pid"] for p in mine)
        for p in mine: assert v.get("/api/obs/subagents", params={"project_id": p["id"]}).json() == []
    finally:
        v.close(); pg().execute("DELETE FROM users WHERE username=%s", (f"d63_vw_{SUF}",))
