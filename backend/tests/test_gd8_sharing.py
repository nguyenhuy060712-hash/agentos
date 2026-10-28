"""GĐ8.8 (23/10/2026): agent sharing (agent_shares + agents.visibility, RLS), per-agent MCP /mcp/agents/{id} (official `mcp` client),
8 Vietnamese industry templates, skill folder import (zip -> Context Hub skill). Local server :8020, fake LLM => $0.
Throwaway workspace + users; server tests run in file order and share state through the `env` fixture."""
import asyncio, base64, io, os, sys, uuid, zipfile
import httpx, psycopg, pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from test_fixes import B, ADMIN, a  # noqa: F401  (fixtures)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
PW = "phase0-gd88-local"; SUF = uuid.uuid4().hex[:6]
VN = ("vn-ban-hang", "vn-cskh", "vn-ke-toan", "vn-nhan-su", "vn-marketing", "vn-phap-che", "vn-giao-vien", "vn-tro-ly-giam-doc")

def pg(): return psycopg.connect(PG, autocommit=True)
def basic(u): return {"Authorization": "Basic " + base64.b64encode(f"{u}:{PW}".encode()).decode()}

# ------------------------------------------------------------------ pure unit tests
def test_slug_and_reply_text():
    import agent_mcp as M
    assert M.slug("Trợ lý bán hàng", 5) == "tro_ly_ban_hang" and M.slug("Đặt lịch ĐỘI", 5) == "dat_lich_doi" and M.slug("***", 7) == "agent_7"
    assert M.reply_text({"reply": "xin chào"}) == "xin chào"
    t = M.reply_text({"paused": True, "approval_id": 42, "reply": ""}); assert "needs approval" in t and "#42" in t

def _zip(entries: dict, method=zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", method) as z:
        for n, d in entries.items(): z.writestr(n, d)
    return buf.getvalue()

def _skill_md(name): return f"---\nname: {name}\ndescription: Lập báo giá chuẩn công ty (thử nghiệm 8.8).\n---\n\n# Báo giá\nDùng scripts/tinh.py để tính tổng.\n"

def test_skill_zip_rules():
    from fastapi import HTTPException
    import skill_import as S
    files, skipped = S.read_skill_zip(_zip({"bg/SKILL.md": _skill_md("bg"), "bg/scripts/tinh.py": "print(1)\n", "bg/logo.png": b"\x89PNG\x00\x01", "__MACOSX/x": "y"}))
    assert set(files) == {"SKILL.md", "scripts/tinh.py"} and skipped == ["logo.png"]
    bad = {"traversal": {"SKILL.md": _skill_md("x"), "../evil.txt": "x"}, "absolute": {"SKILL.md": _skill_md("x"), "/etc/evil": "x"},
           "no SKILL.md": {"README.md": "x"}, "bomb": {"SKILL.md": _skill_md("x"), "big.txt": "0" * (6 * 1024 * 1024)},
           "too many": {"SKILL.md": _skill_md("x"), **{f"f{i}.md": "x" for i in range(101)}}}
    for why, e in bad.items():
        with pytest.raises(HTTPException) as ex: S.read_skill_zip(_zip(e))
        assert ex.value.status_code == 400, why
    with pytest.raises(HTTPException): S.read_skill_zip(b"not a zip")

# ------------------------------------------------------------------ server tests
def user(a, name, ws, role="editor"):
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": role, "workspace_id": ws})
    assert r.status_code == 200, r.text
    return r.json()["id"], httpx.Client(base_url=B, auth=(name, PW), timeout=150)

@pytest.fixture(scope="module")
def env(a):
    ws = a.post("/api/admin/workspaces", json={"name": f"sh-w-{SUF}"}).json()["id"]
    e = {"ws": ws, "adm": httpx.Client(base_url=B, auth=ADMIN, headers={"X-Workspace-Id": str(ws)}, timeout=150)}
    for k, role in (("own", "editor"), ("ed", "editor"), ("oth", "editor"), ("vw", "viewer")):
        e[k + "_id"], e[k] = user(a, f"sh_{k}_{SUF}", ws, role)
        e[k + "_name"] = f"sh_{k}_{SUF}"
    r = e["own"].post("/api/agents", json={"name": f"Trợ lý riêng {SUF}", "description": "Agent thử chia sẻ 8.8", "system_prompt": "Bạn là trợ lý.",
                                            "tools": ["local:content_publish"], "interrupt_on": ["local:content_publish"]})
    assert r.status_code == 200, r.text
    e["agent"] = r.json()["id"]
    yield e
    for k in ("adm", "own", "ed", "oth", "vw"): e[k].close()
    with pg() as c:
        c.execute("DELETE FROM approvals WHERE agent_id IN (SELECT id FROM agents WHERE workspace_id=%s)", (ws,))
        c.execute("DELETE FROM eval_runs WHERE agent_id IN (SELECT id FROM agents WHERE workspace_id=%s)", (ws,))
        c.execute("DELETE FROM lg_runs WHERE workspace_id=%s", (ws,)); c.execute("DELETE FROM lg_threads WHERE workspace_id=%s", (ws,))
        c.execute("DELETE FROM agents WHERE workspace_id=%s", (ws,))
        c.execute("DELETE FROM context_versions WHERE context_id IN (SELECT id FROM contexts WHERE workspace_id=%s)", (ws,))
        c.execute("DELETE FROM contexts WHERE workspace_id=%s", (ws,))
        c.execute("DELETE FROM user_api_keys WHERE workspace_id=%s", (ws,))
        c.execute("DELETE FROM users WHERE username LIKE %s", (f"sh\\_%\\_{SUF}",))

def chat(c, aid, text):
    g = f"agent-{aid}"
    r = c.post("/api/v1/threads", json={"metadata": {"source": "chat", "graph_id": g}})
    if r.status_code != 200: return r
    return c.post(f"/api/v1/threads/{r.json()['thread_id']}/runs/wait", json={"assistant_id": g, "input": {"messages": [{"type": "human", "content": text}]}})

def test_workspace_agent_keeps_todays_rights(env):
    aid = env["agent"]
    d = env["own"].get(f"/api/agents/{aid}/shares").json()
    assert d["visibility"] == "workspace" and d["owner"]["id"] == env["own_id"] and d["me"]["owner"]
    assert env["oth"].get(f"/api/agents/{aid}").status_code == 200                       # every member sees a workspace agent
    cfg = env["oth"].get(f"/api/agents/{aid}").json()["config"]
    assert env["oth"].put(f"/api/agents/{aid}", json=cfg).status_code == 200            # editors still edit (unchanged behaviour)
    assert env["oth"].post(f"/api/agents/{aid}/shares", json={"visibility": "private"}).status_code == 403   # only owner/admin change visibility

def test_private_agent_hidden_from_unshared_user(env):
    aid = env["agent"]
    assert env["own"].post(f"/api/agents/{aid}/shares", json={"visibility": "private"}).json()["visibility"] == "private"
    for k in ("oth", "ed", "vw"):
        assert env[k].get(f"/api/agents/{aid}").status_code == 404, k
        assert aid not in [x["id"] for x in env[k].get("/api/agents").json()], k
        assert env[k].get(f"/api/agents/{aid}/shares").status_code == 404, k
    assert env["oth"].put(f"/api/agents/{aid}", json={"name": "x"}).status_code == 404
    assert chat(env["oth"], aid, "echo-user xin chào").status_code == 404                # chat path hides it too (lg_assistants RLS)
    assert env["adm"].get(f"/api/agents/{aid}").status_code == 200                      # workspace admin sees it
    assert env["own"].get(f"/api/agents/{aid}").status_code == 200

def test_share_use_can_chat_but_not_edit(env):
    aid = env["agent"]
    d = env["own"].post(f"/api/agents/{aid}/shares", json={"username": env["ed_name"], "role": "use"}).json()
    assert [(s["username"], s["role"]) for s in d["shares"]] == [(env["ed_name"], "use")]
    assert env["own"].post(f"/api/agents/{aid}/shares", json={"username": "no-such-user-" + SUF}).status_code == 404
    ed = env["ed"]
    g = ed.get(f"/api/agents/{aid}"); assert g.status_code == 200
    r = chat(ed, aid, f"echo-user chào {SUF}"); assert r.status_code == 200, r.text
    assert f"chào {SUF}" in str(r.json())
    assert ed.put(f"/api/agents/{aid}", json=g.json()["config"]).status_code == 403
    assert ed.post(f"/api/agents/{aid}/rollback/1").status_code == 403
    assert ed.delete(f"/api/agents/{aid}").status_code == 403
    assert ed.post(f"/api/agents/{aid}/shares", json={"username": env["oth_name"]}).status_code == 403   # 'use' cannot re-share
    assert env["oth"].get(f"/api/agents/{aid}").status_code == 404                      # still hidden from others

def test_share_edit_can_change_config(env):
    aid = env["agent"]
    env["own"].post(f"/api/agents/{aid}/shares", json={"username": env["ed_name"], "role": "edit"})
    cfg = env["ed"].get(f"/api/agents/{aid}").json()["config"]; cfg["description"] = "sửa bởi người được chia sẻ"
    assert env["ed"].put(f"/api/agents/{aid}", json=cfg).status_code == 200
    env["own"].post(f"/api/agents/{aid}/shares", json={"username": env["ed_name"], "role": "use"})   # back to 'use' for the MCP tests

# ------------------------------------------------------------------ per-agent MCP
async def _mcp(url, headers, op, *args):
    async with httpx.AsyncClient(headers=headers, timeout=150) as hc, streamable_http_client(url, http_client=hc) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            return await getattr(s, op)(*args)

def mcp(env, who, op, *args, aid=None, headers=None):
    return asyncio.run(_mcp(f"{B}/mcp/agents/{aid or env['agent']}", headers or basic(env[who + "_name"]), op, *args))

def test_mcp_list_and_call_as_shared_user(env):
    tools = mcp(env, "ed", "list_tools").tools
    assert [t.name for t in tools] == [f"ask_tro_ly_rieng_{SUF}"] and "message" in tools[0].input_schema["properties"]
    r = mcp(env, "ed", "call_tool", tools[0].name, {"message": f"echo-user mcp {SUF}"})
    assert not r.is_error and f"USER<<echo-user mcp {SUF}>>" == r.content[0].text
    res = mcp(env, "ed", "list_resources").resources
    assert [str(x.uri) for x in res] == [f"agent://{env['agent']}/description"]
    txt = mcp(env, "ed", "read_resource", res[0].uri).contents[0].text
    assert f"Trợ lý riêng {SUF}" in txt

def test_mcp_with_api_key_and_interrupt(env):
    k = env["ed"].post("/api/auth/keys", json={"name": "gd88", "scope": "full"}).json()
    h = {"X-Api-Key": k["key"]}
    t = mcp(env, None, "list_tools", headers=h).tools[0].name
    r = mcp(env, None, "call_tool", t, {"message": "please publish the post"}, headers=h)
    assert not r.is_error and "needs approval" in r.content[0].text

def test_mcp_refuses_unshared_and_viewer_without_share(env):
    url = f"{B}/mcp/agents/{env['agent']}"
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    hd = {"Accept": "application/json, text/event-stream"}
    assert env["oth"].post(url, json=body, headers=hd).status_code == 404               # private + unshared -> 404
    assert env["vw"].post(url, json=body, headers=hd).status_code == 404
    with pytest.raises(Exception): mcp(env, "oth", "list_tools")
    assert httpx.post(url, json=body, headers=hd).status_code == 401                    # no credentials
    # a viewer with a 'use' share may call it over MCP; a viewer without share on a workspace agent may not (needs editor or a share)
    env["own"].post(f"/api/agents/{env['agent']}/shares", json={"username": env["vw_name"], "role": "use"})
    assert mcp(env, "vw", "list_tools").tools
    r2 = env["own"].post("/api/agents", json={"name": f"Agent chung {SUF}", "system_prompt": "x"}).json()["id"]
    assert env["vw"].post(f"{B}/mcp/agents/{r2}", json=body, headers=hd).status_code == 403
    assert mcp(env, "oth", "list_tools", aid=r2).tools                                  # editor on a workspace agent: allowed

def test_unshare_removes_access(env):
    aid = env["agent"]
    d = env["own"].get(f"/api/agents/{aid}/shares").json()
    sid = next(s["id"] for s in d["shares"] if s["username"] == env["ed_name"])
    assert env["own"].delete(f"/api/agents/{aid}/shares/{sid}").status_code == 200
    assert env["ed"].get(f"/api/agents/{aid}").status_code == 404

# ------------------------------------------------------------------ templates
def test_eight_vietnamese_templates_create_valid_agents(env):
    ts = {t["key"]: t for t in env["own"].get("/api/builder/templates").json()}
    assert all(k in ts and ts[k]["industry"] for k in VN)
    titles = {ts[k]["title"] for k in VN}
    assert titles == {"Bán hàng", "Chăm sóc khách hàng", "Kế toán", "Nhân sự", "Marketing", "Pháp chế nội bộ", "Giáo viên", "Trợ lý giám đốc"}
    writes = {"local:crm_log_deal", "local:crm_update_deal", "local:crm_schedule_followup", "local:ads_create_campaign", "local:content_publish"}
    for k in VN:
        r = env["own"].post(f"/api/builder/templates/{k}"); assert r.status_code == 200, (k, r.text)
        c = env["own"].get(f"/api/agents/{r.json()['id']}").json()["config"]
        assert "tiếng Việt" in c["system_prompt"] and len(c["system_prompt"]) > 300, k
        assert set(c["tools"]) & writes <= set(c["interrupt_on"]), k                     # every write tool waits for approval
        if c["sandbox"]: assert "local:chay_code_python" in c["interrupt_on"] and all(s.startswith("builtin:") for s in c["skills"]), k

# ------------------------------------------------------------------ skill import
def test_skill_import_and_materialize(env):
    name = f"bao-gia-{SUF}"
    z = _zip({f"{name}/SKILL.md": _skill_md(name), f"{name}/scripts/tinh.py": "print(sum([1, 2]))\n", f"{name}/mau.md": "# Mẫu báo giá\n"})
    up = lambda c, data: c.post("/api/skills/import", files={"file": ("skill.zip", data, "application/zip")})
    r = up(env["own"], z); assert r.status_code == 200, r.text
    assert r.json()["files"] == ["SKILL.md", "mau.md", "scripts/tinh.py"] and r.json()["version"] == 1
    assert up(env["vw"], z).status_code == 403                                            # viewers cannot import
    assert up(env["own"], _zip({"SKILL.md": _skill_md(name), "../x.md": "x"})).status_code == 400
    assert up(env["own"], _zip({"SKILL.md": _skill_md(name), "big.txt": "0" * (6 << 20)})).status_code == 400
    assert up(env["own"], _zip({"SKILL.md": _skill_md("ten-khac")}, )).status_code == 200   # another skill, fine
    pub = env["oth"].get("/api/skills/published").json()
    assert any(s["name"] == name and s["kind"] == "skill" for s in pub)                  # shared in the workspace
    r2 = up(env["own"], z); assert r2.json()["version"] == 2 and r2.json()["updated"]     # re-import = new version
    ag = env["own"].post("/api/agents", json={"name": f"dùng skill {SUF}", "system_prompt": "x", "skills": [name]}); assert ag.status_code == 200, ag.text
    aid = ag.json()["id"]
    cfg = env["own"].get(f"/api/agents/{aid}").json()["config"]; assert cfg["skills"] == [f"{name}@2"]
    # materialized for the agent: the same function the runtime uses (deepagent_api._materialize), DB read directly
    os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")
    import deepagent_api as A
    from psycopg.rows import dict_row
    async def q(sql, args=(), one=False, many=True):
        async with await psycopg.AsyncConnection.connect(PG, row_factory=dict_row) as c:
            cur = await c.execute(sql, args); rows = await cur.fetchall() if cur.description else []
            return (rows[0] if rows else None) if one else rows
    old = A.q; A.q = q
    try: files, info = asyncio.run(A._materialize(aid, A.AgentCfg(**cfg)))
    finally: A.q = old
    assert f"/skills/main/{name}/SKILL.md" in files and files[f"/skills/main/{name}/scripts/tinh.py"].startswith("print(")
    assert info[0]["version"] == 2
