"""GĐ9.1 (24/10/2026): Context Hub = versioned context packs (commit / tag / diff / rollback), agents pin `pack@tag`, the pack is mounted READ-ONLY
at /pack/ through PackBackend (subclass of deepagents' ContextHubBackend). Local server :8020 with the fake LLM ("show-system" echoes the system prompt
the agent was built with) => $0. Throwaway workspaces + users; server tests share state through the `env` fixture, run in file order."""
import asyncio, base64, io, json, os, sys, uuid, zipfile
import httpx, psycopg, pytest
from psycopg.rows import dict_row
from test_fixes import B, ADMIN, a  # noqa: F401  (fixtures)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
PW = "phase0-gd91-local"; SUF = uuid.uuid4().hex[:6]
NAME = f"quy-trinh-{SUF}"

def pg(): return psycopg.connect(PG, autocommit=True)
def skill_md(name, desc="Lập báo giá chuẩn công ty (thử nghiệm 9.1)."): return f"---\nname: {name}\ndescription: {desc}\n---\n\n# Báo giá\nDùng bảng giá mới nhất.\n"
def zipb(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in entries.items(): z.writestr(n, d)
    return buf.getvalue()

# ------------------------------------------------------------------ pure unit tests
def test_path_and_diff_rules():
    import context_hub as H
    from fastapi import HTTPException
    for ok in ("AGENTS.md", "skills/x/SKILL.md", "policies/a.md", "prompts/sâu/đẹp.md"): H._check_path(ok)
    for bad in ("", "/etc/x", "../x", "a/../b", "a//b", "a/./b", "a\\b", ".", "x" * 201, "C:/x", "a/b/"):
        with pytest.raises(HTTPException): H._check_path(bad)
    rows, trunc = H._rows([f"l{i}" for i in range(30)], [f"l{i}" if i != 15 else "ĐỔI" for i in range(30)])
    assert not trunc and sum(1 for r in rows if r["t"] == "chg") == 1
    assert sum(1 for r in rows if r["t"] == "eq") == 6 and rows[0]["t"] == "gap" and rows[-1]["t"] == "gap"   # 3 lines of context each side, rest collapsed
    h1 = H._commit_hash(None, "t", "u", "m", __import__("datetime").datetime(2026, 10, 24)); assert h1 == H._commit_hash(None, "t", "u", "m", __import__("datetime").datetime(2026, 10, 24))
    assert h1 != H._commit_hash(h1, "t", "u", "m", __import__("datetime").datetime(2026, 10, 24)) and len(h1) == 64   # content addressed incl. parent

def test_pack_backend_is_read_only_and_reads_like_context_hub():
    """The mounted FS: stock deepagents ContextHubBackend read paths, every mutation refused; composed under /pack/ like build_agent does."""
    import context_hub as H
    from deepagents.backends import CompositeBackend, StateBackend
    from deepagents.backends.context_hub import ContextHubBackend
    files = {"AGENTS.md": "# Quy tắc\nLuôn chào khách.\n", "skills/bao-gia/SKILL.md": skill_md("bao-gia"), "policies/giam-gia.md": "Tối đa 10%\n", "prompts/mo-dau.md": "Xin chào {{ten}}\n"}
    pb = H.PackBackend("p@abc", files, "abc123")
    assert isinstance(pb, ContextHubBackend) and pb.commit == "abc123"
    comp = CompositeBackend(default=StateBackend(), routes={"/pack/": pb})
    assert "Luôn chào khách" in comp.read("/pack/AGENTS.md").file_data["content"]
    assert {e["path"] for e in comp.ls("/pack/").entries} == {"/pack/AGENTS.md", "/pack/skills", "/pack/policies", "/pack/prompts"}
    assert [e["path"] for e in comp.ls("/pack/skills/").entries] == ["/pack/skills/bao-gia"]
    assert [m["path"] for m in comp.glob("*.md", "/pack/policies").matches] == ["/pack/policies/giam-gia.md"]
    assert [m["path"] for m in comp.grep("Tối đa", "/pack/").matches] == ["/pack/policies/giam-gia.md"]
    assert comp.download_files(["/pack/AGENTS.md"])[0].content.startswith("# Quy".encode())
    # every write path is refused and nothing changes
    for r in (pb.write("/AGENTS.md", "hack"), comp.write("/pack/AGENTS.md", "hack"), pb.edit("/AGENTS.md", "Luôn", "Đừng"), comp.edit("/pack/AGENTS.md", "Luôn", "Đừng"), pb.delete("/AGENTS.md"), comp.delete("/pack/policies")):
        assert r.error and "Read-only" in r.error
    assert all(u.error and "Read-only" in u.error for u in comp.upload_files([("/pack/new.md", b"x")]))
    assert "Luôn chào khách" in comp.read("/pack/AGENTS.md").file_data["content"] and comp.read("/pack/new.md").error
    assert pb.glob("*.md").error is None and pb._commit_hash == "abc123"

def test_demo_workspace_cannot_write():
    import deepagent_api  # noqa: F401  (import order: admin needs deepagent_api first)
    import admin, context_hub as H
    from fastapi import HTTPException
    tok = admin.USER.set({"id": 9, "username": "demo", "role": "viewer", "ws": 1, "demo": True})
    try:
        with pytest.raises(HTTPException) as e: H._no_demo()
        assert e.value.status_code == 403
    finally: admin.USER.reset(tok)

# ------------------------------------------------------------------ server tests
def user(a, name, ws, role="editor"):
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": role, "workspace_id": ws}); assert r.status_code == 200, r.text
    return httpx.Client(base_url=B, auth=(name, PW), timeout=150)

@pytest.fixture(scope="module")
def env(a):
    wa = a.post("/api/admin/workspaces", json={"name": f"ch-a-{SUF}"}).json()["id"]; wb = a.post("/api/admin/workspaces", json={"name": f"ch-b-{SUF}"}).json()["id"]
    e = {"wa": wa, "wb": wb, "ed": user(a, f"ch_ed_{SUF}", wa), "vw": user(a, f"ch_vw_{SUF}", wa, "viewer"), "oth": user(a, f"ch_oth_{SUF}", wb)}
    yield e
    for k in ("ed", "vw", "oth"): e[k].close()
    with pg() as c:
        for w in (wa, wb):
            c.execute("DELETE FROM lg_runs WHERE workspace_id=%s", (w,)); c.execute("DELETE FROM lg_threads WHERE workspace_id=%s", (w,))
            c.execute("DELETE FROM eval_runs WHERE agent_id IN (SELECT id FROM agents WHERE workspace_id=%s)", (w,))
            c.execute("DELETE FROM agents WHERE workspace_id=%s", (w,))
            c.execute("DELETE FROM context_packs WHERE workspace_id=%s", (w,))
            c.execute("DELETE FROM context_versions WHERE context_id IN (SELECT id FROM contexts WHERE workspace_id=%s)", (w,))
            c.execute("DELETE FROM contexts WHERE workspace_id=%s", (w,))
        c.execute("DELETE FROM users WHERE username LIKE %s", (f"ch\\_%\\_{SUF}",))

def run(c, aid, text, thread=None):
    g = f"agent-{aid}"
    t = thread or c.post("/api/v1/threads", json={"metadata": {"source": "chat", "graph_id": g}}).json()["thread_id"]
    r = c.post(f"/api/v1/threads/{t}/runs/wait", json={"assistant_id": g, "input": {"messages": [{"type": "human", "content": text}]}})
    assert r.status_code == 200, r.text
    return t, r.json()["messages"][-1]["content"]

def test_commit_tag_diff(env):
    c = env["ed"]
    r = c.post("/api/packs", json={"name": NAME, "description": "Gói thử 9.1", "files": {"AGENTS.md": "# Quy trình\nLuôn chào khách.\n", f"skills/bao-gia/SKILL.md": skill_md("bao-gia"),
                                                                                   "policies/giam-gia.md": "Tối đa 10%\n"}})
    assert r.status_code == 200, r.text; pid = r.json()["id"]; h1 = r.json()["hash"]; env["pid"], env["h1"] = pid, h1
    assert len(h1) == 64
    assert c.post("/api/packs", json={"name": NAME}).status_code == 409                       # unique per workspace
    assert c.post("/api/packs", json={"name": "Bad Name!"}).status_code == 422
    # multi-file commit with an optimistic lock and a tag
    r = c.post(f"/api/packs/{pid}/commits", json={"message": "Giảm giá tối đa 15%", "parent": h1[:10], "tag": "prod",
                                                  "changes": {"policies/giam-gia.md": "Tối đa 15%\n", "AGENTS.md": "# Quy trình\nLuôn chào khách.\nBáo giá trong 1 giờ.\n", "prompts/mo-dau.md": "Xin chào {{ten}}\n"}})
    assert r.status_code == 200, r.text; h2 = r.json()["hash"]; env["h2"] = h2; assert r.json()["n_files"] == 4
    assert c.post(f"/api/packs/{pid}/commits", json={"message": "x", "parent": h1[:10], "changes": {"a.md": "x"}}).status_code == 409   # stale parent
    assert c.post(f"/api/packs/{pid}/commits", json={"message": "x", "changes": {"policies/giam-gia.md": "Tối đa 15%\n"}}).status_code == 400   # nothing changed
    for bad in ({"../x.md": "x"}, {"/abs.md": "x"}, {"skills/loi/SKILL.md": "no frontmatter"}, {"skills/khong-skill/readme.md": "x"}, {"skills/x.md": "x"}, {"a.md": "0" * 200_001}):
        assert c.post(f"/api/packs/{pid}/commits", json={"message": "bad", "changes": bad}).status_code == 400, bad
    d = c.get(f"/api/packs/{pid}").json()
    assert d["head"] == h2 and [x["hash"] for x in d["commits"]] == [h2, h1] and d["commits"][0]["parent"] == h1 and d["commits"][1]["parent"] is None   # immutable chain
    assert d["commits"][0]["author"] == f"ch_ed_{SUF}" and d["commits"][0]["message"] == "Giảm giá tối đa 15%" and d["commits"][0]["tags"] == ["prod"]
    assert [t["tag"] for t in d["tags"]] == ["prod"] and d["tags"][0]["hash"] == h2
    # blobs are content addressed and de-duplicated: unchanged files add no blob
    with pg() as p: n = p.execute("SELECT count(*) FROM context_blobs WHERE pack_id=%s", (pid,)).fetchone()[0]
    assert n == 6                                                                                  # 3 first + 3 new (AGENTS.md, giam-gia.md, mo-dau.md); SKILL.md shared
    # file-level diff
    df = c.get(f"/api/packs/{pid}/diff", params={"a": h1[:8], "b": "prod"}).json()
    assert {f["path"]: f["status"] for f in df["files"]} == {"AGENTS.md": "modified", "policies/giam-gia.md": "modified", "prompts/mo-dau.md": "added"}
    ag = next(f for f in df["files"] if f["path"] == "AGENTS.md"); assert ag["added"] == 1 and any(r["t"] == "add" and r["rt"] == "Báo giá trong 1 giờ." for r in ag["rows"]) and "+Báo giá trong 1 giờ." in ag["unified"]
    assert df["summary"] == {"added": 1, "removed": 0, "modified": 2}
    assert c.get(f"/api/packs/{pid}/diff", params={"b": "latest"}).json()["a"]["hash"] == h1   # default a = parent
    # tags: set, move, delete, events; names that look like hashes / latest are refused
    assert c.post(f"/api/packs/{pid}/tags", json={"tag": "staging", "commit": h1[:8]}).json()["moved"]
    assert c.post(f"/api/packs/{pid}/tags", json={"tag": "latest", "commit": "latest"}).status_code == 400 and c.post(f"/api/packs/{pid}/tags", json={"tag": "deadbeef", "commit": "latest"}).status_code == 400
    assert c.post(f"/api/packs/{pid}/tags", json={"tag": "x", "commit": "nope"}).status_code == 404
    assert c.delete(f"/api/packs/{pid}/tags/staging").status_code == 200 and c.delete(f"/api/packs/{pid}/tags/staging").status_code == 404
    ev = c.get(f"/api/packs/{pid}").json()["events"]; assert [(e["tag"], bool(e["from_hash"]), bool(e["to_hash"])) for e in ev][:3] == [("staging", True, False), ("staging", False, True), ("prod", False, True)]
    assert c.get(f"/api/packs/{pid}/files", params={"ref": "prod"}).json()["files"]["policies/giam-gia.md"] == "Tối đa 15%\n"
    assert c.get(f"/api/packs/{pid}/files", params={"ref": h1[:8]}).json()["files"]["policies/giam-gia.md"] == "Tối đa 10%\n"
    assert env["vw"].post(f"/api/packs/{pid}/commits", json={"message": "x", "changes": {"a.md": "x"}}).status_code == 403   # viewers read only
    assert env["vw"].get(f"/api/packs/{pid}").status_code == 200

def test_agent_pinned_to_tag_switches_on_retag_without_restart(env):
    c, pid, h1, h2 = env["ed"], env["pid"], env["h1"], env["h2"]
    assert c.post("/api/agents", json={"name": f"x {SUF}", "system_prompt": "x", "context_pack": "khong-co@prod"}).status_code == 400          # unknown pack
    assert c.post("/api/agents", json={"name": f"x {SUF}", "system_prompt": "x", "context_pack": f"{NAME}@khong-co"}).status_code == 400       # unknown tag
    r = c.post("/api/agents", json={"name": f"Trợ lý gói {SUF}", "system_prompt": "Bạn là trợ lý.", "context_pack": f"{NAME}@prod", "filesystem": False}); assert r.status_code == 200, r.text
    aid = env["aid"] = r.json()["id"]
    assert c.get(f"/api/agents/{aid}").json()["config"]["context_pack"] == f"{NAME}@prod"
    t, sysmsg = run(c, aid, "show-system")                                                         # prod = commit 2
    assert "Báo giá trong 1 giờ." in sysmsg and "/pack/AGENTS.md" in sysmsg and "bao-gia" in sysmsg and "/pack/skills/bao-gia/SKILL.md" in sysmsg
    assert "mounted read-only at /pack/" in sysmsg and "policies/giam-gia.md" in sysmsg and "prompts/mo-dau.md" in sysmsg
    ctx = c.get(f"/api/agents/{aid}/context").json(); assert ctx["commit"] == h2 and ctx["built_commits"] == [h2] and ctx["tags"] == ["prod"]
    # move the tag back = rollback: the SAME thread reads the old content on its next turn (cache is keyed by commit, no restart)
    assert c.post(f"/api/packs/{pid}/tags", json={"tag": "prod", "commit": h1}).status_code == 200
    _, s1 = run(c, aid, "show-system", t)
    assert "Báo giá trong 1 giờ." not in s1 and "Luôn chào khách." in s1 and "prompts/mo-dau.md" not in s1
    ctx = c.get(f"/api/agents/{aid}/context").json(); assert ctx["commit"] == h1 and ctx["built_commits"] == [h1]
    # a new commit + re-tag = new content on the next run; other tags/commits do not affect the agent
    h3 = c.post(f"/api/packs/{pid}/commits", json={"message": "Thêm quy tắc đổi trả", "changes": {"AGENTS.md": "# Quy trình\nĐổi trả trong 7 ngày.\n"}}).json()["hash"]
    _, s2 = run(c, aid, "show-system", t); assert "Đổi trả trong 7 ngày" not in s2 and "Luôn chào khách." in s2          # still prod = commit 1
    c.post(f"/api/packs/{pid}/tags", json={"tag": "prod", "commit": h3[:8]})
    _, s3 = run(c, aid, "show-system", t); assert "Đổi trả trong 7 ngày." in s3 and "Luôn chào khách." not in s3
    assert c.get(f"/api/agents/{aid}/context").json()["built_commits"] == [h3]
    env["h3"] = h3
    # pack in use cannot be deleted
    assert c.delete(f"/api/packs/{pid}").status_code == 409

def test_restore_rollback_as_new_commit(env):
    c, pid, h1, h3 = env["ed"], env["pid"], env["h1"], env["h3"]
    r = c.post(f"/api/packs/{pid}/restore", json={"commit": h1[:8], "message": "Quay lại bản đầu", "tag": "prod"}); assert r.status_code == 200, r.text
    h4 = r.json()["hash"]; assert h4 not in (h1, h3)                                               # a NEW commit: history is never rewritten
    d = c.get(f"/api/packs/{pid}").json(); assert [x["hash"] for x in d["commits"]][:2] == [h4, h3] and d["commits"][0]["parent"] == h3
    f4 = c.get(f"/api/packs/{pid}/files", params={"ref": h4}).json()["files"]; f1 = c.get(f"/api/packs/{pid}/files", params={"ref": h1}).json()["files"]
    assert f4 == f1 and c.get(f"/api/packs/{pid}/diff", params={"a": h1[:8], "b": h4[:8]}).json()["files"] == []   # identical to the old content
    assert c.post(f"/api/packs/{pid}/restore", json={"commit": h4}).status_code == 400            # already equal to head
    _, s = run(c, env["aid"], "show-system"); assert "Luôn chào khách." in s and "Đổi trả trong 7 ngày" not in s

def test_old_contexts_api_still_works_and_mirrors_into_packs(env):
    c = env["ed"]; nm = f"ky-nang-{SUF}"
    r = c.post("/api/contexts", json={"kind": "skill", "name": nm, "status": "active", "files": {"SKILL.md": skill_md(nm), "mau.md": "# Mẫu\n"}}); assert r.status_code == 200, r.text; cid = r.json()["id"]
    assert any(x["id"] == cid and x["name"] == nm and x["n_files"] == 2 for x in c.get("/api/contexts").json())
    assert c.get(f"/api/contexts/{cid}").json()["files"]["mau.md"] == "# Mẫu\n"
    assert c.put(f"/api/contexts/{cid}", json={"kind": "skill", "name": nm, "status": "active", "files": {"SKILL.md": skill_md(nm), "mau.md": "# Mẫu v2\n"}}).json()["version"] == 2
    assert [v["version"] for v in c.get(f"/api/contexts/{cid}/versions").json()] == [2, 1]
    assert any(s["name"] == nm and s["version"] == 2 for s in c.get("/api/skills/published").json())
    pk = c.get("/api/packs", params={"legacy": cid}).json(); assert len(pk) == 1 and pk[0]["name"] == nm and pk[0]["commits"] == 2 and pk[0]["skills"] == [nm]
    assert [t["tag"] for t in pk[0]["tags"]] == ["prod"] and pk[0]["tags"][0]["hash"] == pk[0]["head"]
    f = c.get(f"/api/packs/{pk[0]['id']}/files").json()["files"]; assert f[f"skills/{nm}/mau.md"] == "# Mẫu v2\n" and f"skills/{nm}/SKILL.md" in f
    # an agent can still pin the legacy way (name -> name@N) and builds fine
    r = c.post("/api/agents", json={"name": f"cũ {SUF}", "system_prompt": "x", "skills": [nm]}); assert r.status_code == 200, r.text
    assert c.get(f"/api/agents/{r.json()['id']}").json()["config"]["skills"] == [f"{nm}@2"]
    _, s = run(c, r.json()["id"], "show-system"); assert nm in s
    # a draft stays out of prod; deleting the legacy context removes its pack too (no agent pins it)
    d = c.post("/api/contexts", json={"kind": "agent", "name": f"Quy tắc cũ {SUF}", "status": "draft"}).json()["id"]
    pd = c.get("/api/packs", params={"legacy": d}).json()[0]; assert pd["tags"] == [] or pd["tags"][0]["tag"] != "prod"
    assert c.delete(f"/api/contexts/{cid}").status_code == 200 and c.get("/api/packs", params={"legacy": cid}).json() == []

def test_migration_of_existing_contexts_loses_nothing(env):
    """Boot-time migration: rows written straight into contexts/context_versions (before packs existed) -> one pack each, a commit per published version."""
    import deepagent_api as A, context_hub as H
    wa = env["wa"]; nm = f"cu-{SUF}"
    with pg() as p:
        cid = p.execute("INSERT INTO contexts(workspace_id,kind,name,description,visibility,status,files,version) VALUES(%s,'skill',%s,'mô tả','public','active',%s,2) RETURNING id",
                        (wa, nm, json.dumps({"SKILL.md": skill_md(nm), "a.md": "v2"}))).fetchone()[0]
        for v, body in ((1, "v1"), (2, "v2")): p.execute("INSERT INTO context_versions(context_id,version,files) VALUES(%s,%s,%s)", (cid, v, json.dumps({"SKILL.md": skill_md(nm), "a.md": body})))
        did = p.execute("INSERT INTO contexts(workspace_id,kind,name,description,status,files,version) VALUES(%s,'agent',%s,'bản nháp','draft',%s,1) RETURNING id", (wa, "Hướng dẫn Tiếng Việt", json.dumps({"AGENTS.md": "# nháp"}))).fetchone()[0]
        n_before = p.execute("SELECT count(*) FROM context_packs WHERE legacy_context_id IN (%s,%s)", (cid, did)).fetchone()[0]
    assert n_before == 0
    async def q(sql, args=(), one=False, many=True):
        async with await psycopg.AsyncConnection.connect(PG, row_factory=dict_row, autocommit=True) as c:
            cur = await c.execute(sql, args); rows = await cur.fetchall() if cur.description else []
            return (rows[0] if rows else None) if one else rows
    old = A.q; A.q = q
    try: asyncio.run(H.migrate()); asyncio.run(H.migrate())                                         # idempotent
    finally: A.q = old
    with pg() as p:
        packs = p.execute("SELECT id, name, workspace_id FROM context_packs WHERE legacy_context_id IN (%s,%s) ORDER BY legacy_context_id", (cid, did)).fetchall()
        assert len(packs) == 2 and packs[0][1] == nm and packs[0][2] == wa and packs[1][1] == "huong-dan-tieng-viet"        # slug of a Vietnamese name
        msgs = [r[0] for r in p.execute("SELECT message FROM context_commits WHERE pack_id=%s ORDER BY seq", (packs[0][0],))]
        assert msgs == ["Legacy publish v1", "Legacy publish v2"]                                  # one commit per published version, current files == v2 -> no extra commit
        tags = dict(p.execute("SELECT tag, hash FROM context_tags WHERE pack_id=%s", (packs[0][0],)).fetchall())
        last = p.execute("SELECT hash FROM context_commits WHERE pack_id=%s ORDER BY seq DESC LIMIT 1", (packs[0][0],)).fetchone()[0]
        assert tags == {"prod": last}
        assert dict(p.execute("SELECT tag, hash FROM context_tags WHERE pack_id=%s", (packs[1][0],)).fetchall()).keys() == {"draft"}
    f = env["ed"].get(f"/api/packs/{packs[0][0]}/files", params={"ref": "prod"}).json()["files"]; assert f[f"skills/{nm}/a.md"] == "v2"
    assert env["ed"].get(f"/api/packs/{packs[0][0]}/diff", params={"a": "latest"}).status_code == 200

def test_rls_workspace_isolation(env):
    c, o, pid = env["ed"], env["oth"], env["pid"]
    assert o.get("/api/packs").json() == [] and pid not in [p["id"] for p in o.get("/api/packs").json()]
    for method, url in (("get", f"/api/packs/{pid}"), ("get", f"/api/packs/{pid}/files"), ("get", f"/api/packs/{pid}/diff?b=latest"), ("get", f"/api/packs/{pid}/export"),
                        ("post", f"/api/packs/{pid}/tags"), ("post", f"/api/packs/{pid}/commits"), ("post", f"/api/packs/{pid}/restore"), ("delete", f"/api/packs/{pid}")):
        r = getattr(o, method)(url, **({"json": {"tag": "prod", "commit": "latest", "message": "x", "changes": {"a.md": "x"}}} if method == "post" else {}))
        assert r.status_code in (404, 422), (url, r.status_code)
    assert c.get(f"/api/packs/{pid}").status_code == 200
    assert o.post("/api/agents", json={"name": f"x {SUF}", "system_prompt": "x", "context_pack": f"{NAME}@prod"}).status_code == 400     # cannot pin another workspace's pack
    assert o.post("/api/packs", json={"name": NAME}).status_code == 200                              # same name is free in workspace B
    with pg() as p:                                                                                  # the database itself refuses, whatever the API does
        p.execute("SET ROLE aos_rls"); p.execute("SELECT set_config('app.ws', %s, false)", (str(env["wb"]),))
        for t in ("context_packs", "context_commits", "context_blobs", "context_tags", "context_tag_events"):
            assert p.execute(f"SELECT count(*) FROM {t} WHERE workspace_id=%s", (env["wa"],)).fetchone()[0] == 0, t
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            p.execute("INSERT INTO context_tags(workspace_id, pack_id, tag, hash) VALUES(%s,%s,'evil','x')", (env["wa"], pid))

def test_zip_import_export_guards(env):
    import context_hub as H, skill_import as S
    from fastapi import HTTPException
    c = env["ed"]
    files, skipped = S.read_skill_zip(zipb({"AGENTS.md": "# a", "skills/x/SKILL.md": "s", "logo.png": b"\x89PNG\x00\x01"}), require_skill=False); assert set(files) == {"AGENTS.md", "skills/x/SKILL.md"} and skipped == ["logo.png"]
    bad = {"traversal": {"AGENTS.md": "x", "../evil.txt": "x"}, "absolute": {"AGENTS.md": "x", "/etc/evil": "x"}, "bomb": {"AGENTS.md": "x", "big.txt": "0" * (6 << 20)},
           "too many": {"AGENTS.md": "x", **{f"f{i}.md": "x" for i in range(101)}}}
    for why, e in bad.items():
        with pytest.raises(HTTPException) as ex: S.read_skill_zip(zipb(e), require_skill=False)
        assert ex.value.status_code == 400, why
    imp = lambda cl, data, name, **kw: cl.post("/api/packs/import", files={"file": ("p.zip", data, "application/zip")}, data={"name": name, **kw})
    for why, e in bad.items(): assert imp(c, zipb(e), f"zip-{SUF}").status_code == 400, why
    assert imp(c, b"not a zip", f"zip-{SUF}").status_code == 400
    assert imp(c, zipb({"AGENTS.md": "x", "skills/loi/SKILL.md": "no frontmatter"}), f"zip-{SUF}").status_code == 400          # skill frontmatter checked like a commit
    assert imp(c, zipb({"AGENTS.md": "x"}), "Bad Name").status_code == 400
    assert imp(env["vw"], zipb({"AGENTS.md": "x"}), f"zip-{SUF}").status_code == 403
    z = zipb({f"goi/AGENTS.md": "# Gói zip\n", f"goi/skills/bao-gia/SKILL.md": skill_md("bao-gia"), "goi/prompts/a.md": "A", "goi/logo.png": b"\x89PNG\x00\x01"})   # one top folder is stripped
    r = imp(c, z, f"zip-{SUF}", message="Nhập zip", tag="prod"); assert r.status_code == 200, r.text
    assert r.json()["created"] and r.json()["files"] == ["AGENTS.md", "prompts/a.md", "skills/bao-gia/SKILL.md"] and r.json()["skipped"] == ["goi/logo.png"]
    pid = r.json()["id"]; assert imp(c, z, f"zip-{SUF}").status_code == 400                      # same content again -> no empty commit
    r2 = imp(c, zipb({"AGENTS.md": "# Gói zip v2\n"}), f"zip-{SUF}"); assert r2.status_code == 200 and not r2.json()["created"]     # existing name -> new commit
    assert c.get(f"/api/packs/{pid}").json()["commits"][0]["message"] == "Import zip" and len(c.get(f"/api/packs/{pid}").json()["commits"]) == 2
    ex = c.get(f"/api/packs/{pid}/export", params={"ref": "prod"}); assert ex.status_code == 200 and ex.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(ex.content)) as zf: assert sorted(zf.namelist()) == ["AGENTS.md", "prompts/a.md", "skills/bao-gia/SKILL.md"] and zf.read("AGENTS.md").decode() == "# Gói zip\n"

def test_ui_pages_and_agent_card(env):
    h = env["ed"].get("/context", headers={"Accept": "text/html"}); assert h.status_code == 200
    assert "api/packs" in h.text and "Commit History" in h.text and "git-compare" in h.text and "sbs" in h.text
    ag = env["ed"].get("/builder", headers={"Accept": "text/html"}).text; assert "Context pack" in ag and "context_pack" in ag
    from i18n import DICT
    for k in ("Import zip", "Move here", "Restore as new commit", "Open latest", "Context pack", "Tag history", "No context packs yet", "Back to files"): assert DICT[k]["vi"], k
