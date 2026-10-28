"""GĐ12 lane K (Skills): 12.88 lint + folder import, 12.86 private/shared skills on one model. Unit tests are pure; server tests use the fake-LLM server
started by the agent on its OWN port (env SKILLS_BASE, default http://127.0.0.1:8055). No real LLM, $0."""
import asyncio, io, os, sys, time, uuid, zipfile
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")
import skills_mgmt as SM  # noqa: E402

run = asyncio.run
GOOD_DESC = "Dùng khi cần lập báo giá cho khách. Use when the user asks for a quote (báo giá, quote, invoice)."

def md(name="bao-gia", desc=GOOD_DESC, extra="", body="# Báo giá\nXem [bảng giá](references/gia.md).\n"):
    return f"---\nname: {name}\ndescription: {desc}\n{extra}---\n\n{body}"

def good(name="bao-gia"): return {"SKILL.md": md(name), "references/gia.md": "Bảng giá: A=1, B=2\n"}
def codes(r, kind="errors"): return {x["code"] for x in r[kind]}
def lint(name, files, **kw): return run(SM.lint(name, files, **kw))

async def fake_embed(texts):                       # same description -> same vector; different -> orthogonal-ish
    import hashlib
    out = []
    for t in texts:
        h = hashlib.sha256(t.encode()).digest(); out.append([(b - 128) / 128 for b in h])
    return out

# ------------------------------------------------------------------ one good + one bad fixture per rule
def test_good_skill_has_no_errors_or_warnings():
    r = lint("bao-gia", good()); assert r == {"errors": [], "warnings": []}

def test_E0_missing_or_bad_frontmatter():
    assert "E0" in codes(lint("x", {"a.md": "hi"}))
    assert "E0" in codes(lint("x", {"SKILL.md": "# no frontmatter"}))

def test_E1_name_and_folder():
    assert "E1" in codes(lint("bao-gia", {"SKILL.md": md("khac-ten")}))
    assert "E1" in codes(lint("Bad_Name", {"SKILL.md": md("Bad_Name")}))
    assert "E1" not in codes(lint("a" * 64, {"SKILL.md": md("a" * 64)}))
    assert "E1" in codes(lint("a" * 65, {"SKILL.md": md("a" * 65)}))

def test_E2_description():
    assert "E2" in codes(lint("x", {"SKILL.md": "---\nname: x\n---\nbody"}))
    assert "E2" in codes(lint("x", {"SKILL.md": md("x", "d" * 1025)}))
    assert "E2" not in codes(lint("x", {"SKILL.md": md("x", "d" * 1024)}))

def test_E3_compatibility():
    assert "E3" in codes(lint("x", {"SKILL.md": md("x", extra="compatibility: " + "c" * 501 + "\n")}))
    assert "E3" not in codes(lint("x", {"SKILL.md": md("x", extra="compatibility: needs python\n")}))

def test_E4_metadata_and_include_tools():
    assert "E4" in codes(lint("x", {"SKILL.md": md("x", extra="metadata:\n  n: 5\n")}))                   # value is not a string
    assert "E4" in codes(lint("x", {"SKILL.md": md("x", extra="metadata:\n  include_tools: a, b\n")}))      # comma list never matches
    ok = md("x", extra="metadata:\n  include_tools: tool_a tool_b\n")
    assert "E4" not in codes(lint("x", {"SKILL.md": ok}))
    assert "E4" not in codes(lint("x", {"SKILL.md": ok}, tools={"tool_a", "tool_b", "tool_c"}))
    r = lint("x", {"SKILL.md": ok}, tools={"tool_a"}); assert "E4" in codes(r) and "tool_b" in r["errors"][0]["msg"]   # unknown to the agent

def test_E5_limits_and_binary():
    assert "E5" in codes(lint("x", {**good("x"), **{f"references/f{i}.md": "x" for i in range(501)}}))
    assert "E5" in codes(lint("x", {**good("x"), "references/big.md": "x" * ((2 << 20) + 1)}))
    assert "E5" in codes(lint("x", {**good("x"), "references/b.bin": bytes(range(256))}))                  # binary refused in this phase
    assert "E5" not in codes(lint("x", good("x")))

def test_E6_secrets_fail():
    for bad in ("key sk-abcdefghijklmnopqrstuvwx1234", "AIzaSyA1234567890abcdefghijklmnopqrstuv", "ghp_" + "a" * 36, "mật khẩu: Sup3rSecret99",
                "password = hunter2hunter2", "-----BEGIN RSA PRIVATE KEY-----", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123"):
        r = lint("x", {"SKILL.md": md("x", body=f"# t\n{bad}\n")}); assert "E6" in codes(r), bad
    r = lint("x", {"SKILL.md": md("x", body="# t\nĐặt `password: <your password>` trong .env, không viết vào đây.\n")}); assert "E6" not in codes(r)
    r = lint("x", {**good("x"), "references/gia.md": "sk-abcdefghijklmnopqrstuvwxyz"}); assert any(e["file"] == "references/gia.md" for e in r["errors"] if e["code"] == "E6")

def test_E7_references():
    assert "E7" in codes(lint("x", {"SKILL.md": md("x", body="[a](references/none.md)")}))                  # missing
    assert "E7" in codes(lint("x", {"SKILL.md": md("x", body="[a](../secret.md)")}))                        # escapes
    assert "E7" in codes(lint("x", {"SKILL.md": md("x", body="[a](/etc/passwd)")}))                         # absolute
    assert "E7" in codes(lint("x", {**good("x"), "../evil.md": "x"}))                                       # bad file key
    assert "E7" not in codes(lint("x", {"SKILL.md": md("x", body="[web](https://example.com/a) [top](#x)")}))
    assert "E7" not in codes(lint("x", {"SKILL.md": md("x", body="Run `python /memories/skills/x/scripts/a.py`"), "scripts/a.py": "print(1)\n"}))
    assert "E7" in codes(lint("x", {"SKILL.md": md("x", body="Run `scripts/zzz.py`")}))

def test_W1_description_style():
    assert "W1" in codes(lint("x", {"SKILL.md": md("x", "Lập báo giá.")}), "warnings")
    assert "W1" in codes(lint("x", {"SKILL.md": md("x", "Use when the user asks for a quote.")}), "warnings")    # no Vietnamese
    assert "W1" not in codes(lint("x", {"SKILL.md": md("x")}), "warnings")

def test_W2_long_body():
    assert "W2" in codes(lint("x", {"SKILL.md": md("x", body="line\n" * 501)}), "warnings")
    assert "W2" not in codes(lint("x", {"SKILL.md": md("x", body="line\n" * 500)}), "warnings")

def test_W3_depth_and_W4_unreferenced():
    f = {**good("x"), "references/gia.md": "xem [them](more.md)", "references/more.md": "ok", "loose.txt": "x"}
    r = lint("x", f); w = codes(r, "warnings")
    assert "W3" in w and "W4" in w and not r["errors"]
    assert "W3" not in codes(lint("x", good("x")), "warnings") and "W4" not in codes(lint("x", good("x")), "warnings")

def test_W5_duplicate_description():
    others = {"b": GOOD_DESC, "c": "Một skill hoàn toàn khác về kế toán thuế"}
    r = lint("a", good("a"), others=others, embed=fake_embed); assert "W5" in codes(r, "warnings") and not r["errors"]
    assert "'b'" in next(w for w in r["warnings"] if w["code"] == "W5")["msg_en"]
    assert "W5" not in codes(lint("a", good("a"), others={"c": others["c"]}, embed=fake_embed), "warnings")
    assert "W5" not in codes(lint("b", good("b"), others=others, embed=fake_embed), "warnings")              # a skill is not its own duplicate

def test_W6_scripts_and_W7_allowed_tools():
    f = {"SKILL.md": md("x", body="Run `scripts/a.py`", extra="allowed-tools: Bash\n"), "scripts/a.py": "print(1)\n"}
    w = codes(lint("x", f), "warnings"); assert "W6" in w and "W7" in w
    w = codes(lint("x", good("x")), "warnings"); assert "W6" not in w and "W7" not in w

def test_messages_vi_and_en():
    r = lint("x", {"SKILL.md": md("x", "d" * 1025)}); e = r["errors"][0]
    assert e["code"] == "E2" and "bắt buộc" in e["msg"] and "required" in e["msg_en"]

def test_300_file_folder_lints_under_5s():
    f = {"SKILL.md": md("big", body="# t\n" + "".join(f"[r{i}](references/r{i}.md)\n" for i in range(299)))}
    f.update({f"references/r{i}.md": f"# Mục {i}\n" + "nội dung dài. " * 200 for i in range(299)})
    t = time.time(); r = lint("big", f); dt = time.time() - t
    assert len(f) == 300 and not r["errors"] and dt < 5, (dt, r["errors"][:2])

# ------------------------------------------------------------------ import parsing
def zipb(entries):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in entries.items(): z.writestr(n, d)
    return b.getvalue()

def test_split_and_zip_layouts():
    two = SM.read_zip(zipb({"a/SKILL.md": md("a"), "b/SKILL.md": md("b"), "b/x.md": "x", "_common/h.py": "x"}))
    assert set(two) == {"a", "b"} and set(two["b"]) == {"SKILL.md", "x.md"}
    one = SM.read_zip(zipb({"SKILL.md": md("solo")})); assert list(one) == ["solo"]
    wrapped = SM.read_zip(zipb({"folder/SKILL.md": md("folder"), "folder/r.md": "r"})); assert set(wrapped["folder"]) == {"SKILL.md", "r.md"}
    assert SM.split_skills({"a/SKILL.md": "x", "a/r.md": "y"}) == {"a": {"SKILL.md": "x", "r.md": "y"}}
    bin_ = SM.read_zip(zipb({"a/SKILL.md": md("a"), "a/b.bin": bytes(range(256))}))
    assert isinstance(bin_["a"]["b.bin"], bytes) and "E5" in codes(lint("a", bin_["a"]))

def test_zip_guards():
    from fastapi import HTTPException
    for bad in ({"../x/SKILL.md": md("x")}, {"/abs/SKILL.md": md("x")}):
        with pytest.raises(HTTPException): SM.read_zip(zipb(bad))
    with pytest.raises(HTTPException): SM.read_zip(b"not a zip")
    with pytest.raises(HTTPException): SM.read_zip(zipb({f"a/f{i}.md": "x" for i in range(501)}))

# ------------------------------------------------------------------ server tests (own server, fake LLM)
import httpx  # noqa: E402
BASE = os.environ.get("SKILLS_BASE", "http://127.0.0.1:8055"); ADMIN = ("moza", "phase0-local")

@pytest.fixture(scope="module")
def a():
    with httpx.Client(base_url=BASE, auth=ADMIN, timeout=60) as c:
        try: c.get("/health")
        except httpx.HTTPError: pytest.skip("own server not running on " + BASE)
        yield c

def test_api_lint_and_import(a):
    r = a.post("/api/skills/lint", json={"name": "bao-gia", "files": good()}); assert r.status_code == 200 and r.json() == {"errors": [], "warnings": []}, r.text
    bad = {"SKILL.md": md("bao-gia", extra="x: 1\n", body="key sk-abcdefghijklmnopqrstuvwx1234\n")}
    r = a.post("/api/skills/lint", json={"name": "bao-gia", "files": bad}); assert "E6" in {e["code"] for e in r.json()["errors"]}
    r = a.post("/api/skills/import/lint", json={"files": {"ok/SKILL.md": md("ok"), "ok/references/gia.md": "x", "bad/SKILL.md": "# none"}})
    j = r.json(); assert r.status_code == 200 and j["skills"]["ok"]["ok"] and not j["skills"]["bad"]["ok"] and not j["ok"], r.text
    r = a.post("/api/skills/import/lint", files={"file": ("s.zip", zipb({"ok/SKILL.md": md("ok"), "ok/references/gia.md": "x"}), "application/zip")})
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert a.post("/api/skills/import/lint", files={"file": ("s.zip", b"nope", "application/zip")}).status_code == 400
    assert a.post("/api/skills/import/lint", json={}).status_code == 400

# ------------------------------------------------------------------ 12.86 private / shared skills
import psycopg  # noqa: E402
PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
PW = "phase0-gd1286-local"; SUF = uuid.uuid4().hex[:6]
def pg(): return psycopg.connect(PG, autocommit=True)

def sk(name, desc=GOOD_DESC, extra_body=""): return {"SKILL.md": md(name, desc, body="# Báo giá\nXem [bảng giá](references/gia.md)." + extra_body + "\n"), "references/gia.md": "Bảng giá: A=1\n"}

def user(a, name, ws, role="editor"):
    r = a.post("/api/admin/users", json={"username": name, "password": PW, "role": role, "workspace_id": ws}); assert r.status_code == 200, r.text
    return httpx.Client(base_url=BASE, auth=(name, PW), timeout=150)

@pytest.fixture(scope="module")
def env(a):
    w = a.post("/api/admin/workspaces", json={"name": f"sk-{SUF}"}).json()["id"]
    e = {"w": w, "ed1": user(a, f"sk_e1_{SUF}", w), "ed2": user(a, f"sk_e2_{SUF}", w), "adm": user(a, f"sk_ad_{SUF}", w, "admin")}
    yield e
    for k in ("ed1", "ed2", "adm"): e[k].close()
    with pg() as c:
        c.execute("DELETE FROM lg_runs WHERE workspace_id=%s", (w,)); c.execute("DELETE FROM lg_threads WHERE workspace_id=%s", (w,))
        c.execute("DELETE FROM eval_runs WHERE agent_id IN (SELECT id FROM agents WHERE workspace_id=%s)", (w,))
        c.execute("DELETE FROM agents WHERE workspace_id=%s", (w,)); c.execute("DELETE FROM context_packs WHERE workspace_id=%s", (w,))
        c.execute("DELETE FROM context_versions WHERE context_id IN (SELECT id FROM contexts WHERE workspace_id=%s)", (w,)); c.execute("DELETE FROM contexts WHERE workspace_id=%s", (w,))
        c.execute("DELETE FROM users WHERE username LIKE %s", (f"sk\\_%\\_{SUF}",))

def new_agent(c, name, skills=(), expect=200):
    r = c.post("/api/agents", json={"name": f"{name} {SUF}", "system_prompt": "Bạn là trợ lý.", "skills": list(skills), "filesystem": False}); assert r.status_code == expect, r.text
    return r.json().get("id")

def say(c, aid, text="show-system"):
    g = f"agent-{aid}"; t = c.post("/api/v1/threads", json={"metadata": {"source": "chat", "graph_id": g}}).json()["thread_id"]
    r = c.post(f"/api/v1/threads/{t}/runs/wait", json={"assistant_id": g, "input": {"messages": [{"type": "human", "content": text}]}}); assert r.status_code == 200, r.text
    return r.json()["messages"][-1]["content"]

def test_private_skill_visible_only_to_its_agent(env):
    e1, e2 = env["ed1"], env["ed2"]; nm = f"rieng-{SUF}"
    a1 = new_agent(e1, "chủ"); a2 = new_agent(e1, "khác")
    assert e1.post("/api/skills", json={"name": nm, "files": sk(nm), "visibility": "private"}).status_code == 400            # private needs agent_id
    bad = e1.post("/api/skills", json={"name": nm, "files": {"SKILL.md": md(nm, body="sk-abcdefghijklmnopqrstuvwx1234")}, "agent_id": a1}); assert bad.status_code == 422 and bad.json()["detail"]["lint"]["errors"]   # lint blocks saving
    r = e1.post("/api/skills", json={"name": nm, "files": sk(nm), "agent_id": a1}); assert r.status_code == 200, r.text
    sid = r.json()["id"]; assert r.json()["visibility"] == "private"
    assert e1.post("/api/skills", json={"name": nm, "files": sk(nm), "agent_id": a1}).status_code == 409                      # name taken
    assert [s["name"] for s in e1.get("/api/skills", params={"scope": "private"}).json()] == [nm]
    assert e1.get("/api/skills", params={"scope": "private", "agent_id": a2}).json() == []
    assert e2.get("/api/skills", params={"scope": "private"}).json() == [] and e2.get(f"/api/skills/{sid}").status_code == 404
    assert nm not in [s["name"] for s in e1.get("/api/skills", params={"scope": "shared"}).json()]
    assert nm not in [p["name"] for p in e2.get("/api/packs").json()] and nm in [p["name"] for p in env["adm"].get("/api/packs").json()]   # also hidden in the Context Hub list
    new_agent(e1, "khác", [nm], expect=400)                                                                                      # another agent cannot use it
    other = new_agent(e1, "khác2"); assert e1.put(f"/api/agents/{other}", json={"name": "k", "system_prompt": "x", "skills": [nm]}).status_code == 400
    assert e1.put(f"/api/agents/{other}", json={"name": "k", "system_prompt": "x", "skills": [f"{nm}@prod"], "filesystem": False}).status_code == 200 and nm not in say(e1, other)   # explicit name@prod is saved but never loads for a non-owner
    assert e1.put(f"/api/agents/{a1}", json={"name": "chủ", "system_prompt": "Bạn là trợ lý.", "skills": [nm], "filesystem": False}).status_code == 200      # the owner can
    assert e1.get(f"/api/agents/{a1}").json()["config"]["skills"] == [f"{nm}@prod"]                                             # follows prod
    assert nm in say(e1, a1) and GOOD_DESC[:30] in say(e1, a1)
    # share: creator ok, other editor 403, then visible + usable by another agent
    assert e2.post(f"/api/skills/{sid}/share", json={"visibility": "workspace"}).status_code == 403
    assert e1.post(f"/api/skills/{sid}/share", json={"visibility": "nope"}).status_code == 400
    assert e1.post(f"/api/skills/{sid}/share", json={"visibility": "workspace"}).json()["visibility"] == "workspace"
    assert nm in [s["name"] for s in e2.get("/api/skills", params={"scope": "shared"}).json()]
    new_agent(e2, "dùng chung", [nm])

def test_shared_skill_edit_follows_prod_but_not_pinned_commit(env):
    e1, e2, adm = env["ed1"], env["ed2"], env["adm"]; nm = f"chung-{SUF}"
    r = e1.post("/api/skills", json={"name": nm, "files": sk(nm, "Dùng khi lập báo giá v1. Use when quoting (báo giá, quote)."), "visibility": "workspace"}); assert r.status_code == 200, r.text
    sid, h1 = r.json()["id"], r.json()["commit"]
    follow = new_agent(e2, "theo", [f"{nm}@prod"]); bare = new_agent(e2, "tên", [nm]); pinned = new_agent(e2, "ghim", [f"{nm}@{h1[:8]}"])
    assert e2.get(f"/api/agents/{bare}").json()["config"]["skills"] == [f"{nm}@prod"]
    assert "báo giá v1" in say(e2, follow) and "báo giá v1" in say(e2, pinned)
    # another editor cannot edit / delete / share (403); the creator and an admin can
    new = sk(nm, "Dùng khi lập báo giá v2. Use when quoting (báo giá, quote).")
    assert e2.put(f"/api/skills/{sid}", json={"files": new}).status_code == 403
    assert e2.delete(f"/api/skills/{sid}").status_code == 403 and e2.post(f"/api/skills/{sid}/share", json={"visibility": "public"}).status_code == 403
    assert e2.post(f"/api/packs/{sid}/commits", json={"message": "x", "changes": {"a.md": "x"}}).status_code == 403          # same guard on the Context Hub endpoints
    assert e2.put(f"/api/packs/{sid}", json={"description": "x"}).status_code == 403 and e2.delete(f"/api/packs/{sid}").status_code == 403
    assert e1.put(f"/api/skills/{sid}", json={"files": {"SKILL.md": "# no frontmatter"}}).status_code == 422                   # lint blocks the edit
    r = e1.put(f"/api/skills/{sid}", json={"files": new, "message": "v2"}); assert r.status_code == 200 and r.json()["commit"] != h1, r.text
    assert "báo giá v2" in say(e2, follow) and "báo giá v2" in say(e2, bare)                                                 # next run picks it up
    assert "báo giá v1" in say(e2, pinned) and "báo giá v2" not in say(e2, pinned)                                           # pinned commit does not
    new3 = sk(nm, "Dùng khi lập báo giá v3. Use when quoting (báo giá, quote)."); assert adm.put(f"/api/skills/{sid}", json={"files": new3}).status_code == 200   # admin can
    assert "báo giá v3" in say(e2, follow)
    g = e2.get(f"/api/skills/{sid}").json(); assert g["files"]["SKILL.md"] == new3["SKILL.md"] and g["can_edit"] is False and adm.get(f"/api/skills/{sid}").json()["can_edit"] is True
    pk = [p for p in e2.get("/api/packs").json() if p["id"] == sid][0]; assert pk["kind"] == "skill" and pk["visibility"] == "workspace" and "owner_agent_id" in pk   # /api/packs returns the new columns
    # search + soft delete: hidden from lists, still resolves for the agent that uses it
    assert [s["name"] for s in e2.get("/api/skills", params={"q": "chung"}).json()] == [nm]
    assert adm.delete(f"/api/skills/{sid}").status_code == 200
    assert nm not in [s["name"] for s in e2.get("/api/skills").json()] and nm not in [p["name"] for p in e2.get("/api/packs").json()]
    assert "báo giá v3" in say(e2, follow)

def test_legacy_skills_resolve_unchanged(env):
    e1 = env["ed1"]; nm = f"cu-{SUF}"
    r = e1.post("/api/contexts", json={"kind": "skill", "name": nm, "status": "active", "files": {"SKILL.md": md(nm, "Dùng khi test legacy. Use when testing."), "mau.md": "# Mẫu\n"}}); assert r.status_code == 200, r.text
    aid = new_agent(e1, "cũ", [nm]); assert e1.get(f"/api/agents/{aid}").json()["config"]["skills"] == [f"{nm}@1"]
    assert nm in say(e1, aid)
    assert e1.post("/api/skills", json={"name": nm, "files": sk(nm)}).status_code == 409                                       # name belongs to a legacy skill
    pk = [p for p in e1.get("/api/packs").json() if p["legacy_context_id"] == r.json()["id"]][0]; assert pk["kind"] == "skill"   # mirrored legacy skill = kind skill
    assert nm in [s["name"] for s in e1.get("/api/skills").json()]
    assert e1.put(f"/api/skills/{pk['id']}", json={"files": sk(nm)}).status_code == 409                                         # edit it on the legacy page

def test_demo_workspace_is_refused():
    import deepagent_api  # noqa: F401
    import admin
    from fastapi import HTTPException
    tok = admin.USER.set({"id": 9, "username": "demo", "role": "viewer", "ws": 1, "demo": True})
    try:
        with pytest.raises(HTTPException) as e: run(SM.skill_create(SM.SkillIn(name="x", files={"SKILL.md": md("x")})))
        assert e.value.status_code == 403
    finally: admin.USER.reset(tok)

# ------------------------------------------------------------------ migration 0160 (pattern of tests/test_gd10_migrations.py)
def test_migration_0160_up_down_plan():
    import migrations as M
    mod = next(m for v, m, _ in M._load() if v == "0160")
    sch = "t0160_" + uuid.uuid4().hex[:8]
    with psycopg.connect(PG, autocommit=True) as c: c.execute(f'CREATE SCHEMA "{sch}"')
    cols_sql = "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name='context_packs'"
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c:
            q = M._q(c)
            await q("CREATE TABLE contexts(id SERIAL PRIMARY KEY, kind TEXT)")
            await q("CREATE TABLE context_packs(id SERIAL PRIMARY KEY, workspace_id INT, name TEXT, legacy_context_id INT)")
            await q("INSERT INTO contexts(kind) VALUES('skill'),('agent')")
            await q("INSERT INTO context_packs(workspace_id,name,legacy_context_id) VALUES(1,'a',1),(1,'b',2),(1,'c',NULL)")
            assert len(await mod.plan(q)) == 3
            await mod.up(q); await mod.up(q)                                                          # idempotent
            assert dict(await q("SELECT name, kind FROM context_packs")) == {"a": "skill", "b": "agent", "c": "agent"}
            cols = {r[0] for r in await q(cols_sql, (sch,))}
            assert {"kind", "owner_agent_id", "visibility", "source", "lint", "created_by_id", "archived"} <= cols
            with pytest.raises(psycopg.errors.CheckViolation): await q("UPDATE context_packs SET visibility='x' WHERE name='a'")
            with pytest.raises(psycopg.errors.CheckViolation): await q("UPDATE context_packs SET kind='x' WHERE name='a'")
            await mod.down(q)
            assert {r[0] for r in await q(cols_sql, (sch,))} == {"id", "workspace_id", "name", "legacy_context_id"}
            assert (await q("SELECT to_regclass('context_packs_ws_kind')"))[0][0] is None
    try: run(go())
    finally:
        with psycopg.connect(PG, autocommit=True) as c: c.execute(f'DROP SCHEMA "{sch}" CASCADE')

# ------------------------------------------------------------------ 12.91 library of Vietnamese skill templates
TPL_NAMES = {"bao-gia", "tra-loi-khach-hang", "soan-hop-dong-tu-mau", "bao-cao-ban-hang-tu-excel", "cong-van-nd30", "bien-ban-hop",
             "email-lich-su", "ke-hoach-dang-bai-zalo-facebook", "mo-ta-cong-viec-loc-ho-so", "doi-chieu-cong-no"}
REVIEW_NOTE = "cần người am hiểu pháp lý/kế toán xem trước khi dùng chính thức"

def test_template_listing_is_the_decided_ten_outside_app_skills():
    ts = SM.templates(); assert {t["name"] for t in ts} == TPL_NAMES and len(ts) == 10
    assert not (SM.TEMPLATE_DIR.resolve() == SM.SK.APP_SKILLS.resolve()) and not any((SM.SK.APP_SKILLS / n).exists() for n in TPL_NAMES)   # never mounted into every box
    for t in ts: assert t["kind"] == "template" and t["can_edit"] is False and "SKILL.md" in t["files"] and t["description"]

def test_every_template_lints_with_zero_errors_and_has_a_try_prompt():
    for t in SM.templates():
        files = SM.template_files(t["name"]); r = lint(t["name"], files)
        assert r["errors"] == [], (t["name"], r["errors"])
        assert t["try_prompt"] and len(t["try_prompt"]) > 30, t["name"]
        assert "## Thử ngay" in files["SKILL.md"] and not any("[json]" in v for v in files.values()), t["name"]
        assert "W1" not in codes(r, "warnings") and "W4" not in codes(r, "warnings"), (t["name"], r["warnings"])

def test_legal_and_accounting_templates_carry_the_review_note():
    by = {t["name"]: t for t in SM.templates()}
    for n in ("soan-hop-dong-tu-mau", "cong-van-nd30", "doi-chieu-cong-no"):
        assert by[n]["review"] and REVIEW_NOTE in SM.template_files(n)["SKILL.md"], n
    assert all(not by[n]["review"] for n in TPL_NAMES - {"soan-hop-dong-tu-mau", "cong-van-nd30", "doi-chieu-cong-no"})

def test_template_files_rejects_bad_names():
    for bad in ("../skills/docx", "nope", "BAO-GIA", ""):
        with pytest.raises(KeyError): SM.template_files(bad)

def test_api_templates_and_from_template(env):
    e1, e2 = env["ed1"], env["ed2"]
    r = e1.get("/api/skills", params={"scope": "template"}); assert r.status_code == 200 and {t["name"] for t in r.json()} == TPL_NAMES, r.text
    assert [t["name"] for t in e1.get("/api/skills", params={"scope": "template", "q": "công nợ"}).json()] == ["doi-chieu-cong-no"]
    assert e1.get("/api/skills/template/bao-gia").json()["files"]["SKILL.md"].startswith("---") and e1.get("/api/skills/template/nope").status_code == 404
    ag = new_agent(e1, "bán hàng"); other = new_agent(e1, "khác")
    assert e1.post("/api/skills/from-template", json={"template": "nope", "agent_id": ag}).status_code == 404
    r = e1.post("/api/skills/from-template", json={"template": "bao-gia", "agent_id": ag}); assert r.status_code == 200 and r.json()["visibility"] == "private", r.text
    assert e1.post("/api/skills/from-template", json={"template": "bao-gia", "agent_id": ag}).status_code == 409
    mine = e1.get("/api/skills", params={"scope": "private"}).json(); assert [s["name"] for s in mine] == ["bao-gia"] and mine[0]["source"] == "template"
    assert e2.get("/api/skills", params={"scope": "private"}).json() == []                                                      # private to the creator
    assert e1.put(f"/api/agents/{ag}", json={"name": "bán hàng", "system_prompt": "Bạn là trợ lý.", "skills": ["bao-gia"], "filesystem": False}).status_code == 200
    assert "bao-gia" in say(e1, ag) and "bao-gia" not in say(e1, other)                                                         # the agent sees it, another does not

# ------------------------------------------------------------------ 12.87 create Skills with AI / from a conversation / from a fix
import json  # noqa: E402
from fastapi import HTTPException  # noqa: E402
DRAFT = {"name": "Báo giá nhanh", "description": "Dùng khi cần báo giá nhanh cho khách. Use when the user asks for a quick quote (báo giá, quote, price).",
         "body": "# Báo giá nhanh\n1. Hỏi sản phẩm và số lượng.\n2. Xem [bảng giá](references/gia.md).\n\nThử: báo giá 10 áo.", "files": {"references/gia.md": "Áo: 100k\n"}}
def jmark(d=DRAFT): return "[json]" + json.dumps(d, ensure_ascii=False)

def test_parse_draft_and_build_files_pure():
    d = SM.parse_draft("```json\n" + json.dumps(DRAFT, ensure_ascii=False) + "\n```\ncảm ơn")
    assert d["name"] == "bao-gia-nhanh" and d["files"]["SKILL.md"].startswith("---\nname: bao-gia-nhanh\n") and "references/gia.md" in d["files"]
    assert lint(d["name"], d["files"])["errors"] == []
    with pytest.raises(HTTPException) as e: SM.parse_draft("không phải json"); assert e.value.status_code == 502
    f = SM.build_files("X Y", 'd: có dấu hai chấm # và "ngoặc"', "---\nname: z\n---\nbody", {"SKILL.md": "x", "../a": 5, "ok.md": "t"})   # own header dropped, bad extras ignored
    fm = SM.SK.frontmatter(f["SKILL.md"]); assert fm["name"] == "x-y" and fm["description"] == 'd: có dấu hai chấm # và "ngoặc"' and f["SKILL.md"].endswith("\nbody\n") and set(f) == {"SKILL.md", "ok.md"}
    assert SM.slug("Đặt hàng — Mẫu 1") == "dat-hang-mau-1" and SM.slug("***") == ""

def test_spend_cap_gives_402(monkeypatch):
    import deepagent_api as A
    async def boom(*a, **k): raise A.gw.SpendCapReached("Đã hết hạn mức chi phí")
    monkeypatch.setattr(A.gw, "generate", boom)
    with pytest.raises(HTTPException) as e: run(SM.draft("describe", text="viết skill báo giá"))
    assert e.value.status_code == 402 and "hạn mức" in e.value.detail

def test_skill_tools_are_registered_and_external_runs_must_ask():
    import deepagent_api as A, run_trust as RT
    keys = {"local:skill_list", "local:skill_draft", "local:skill_save", "local:skill_share"}
    assert set(SM.tools(1)) == keys and not (keys & RT.SAFE_TOOLS)                                  # none is a "safe" tool
    e = RT.effective(A.AgentCfg(name="x", tools=sorted(keys)), RT.EXTERNAL); assert keys <= set(e.interrupt_on)   # a channel (external) run: all four wait for approval

def talk(c, aid, text):
    t = c.post("/api/v1/threads", json={"metadata": {"source": "chat", "graph_id": f"agent-{aid}"}}).json()["thread_id"]
    r = c.post(f"/api/v1/threads/{t}/runs/wait", json={"assistant_id": f"agent-{aid}", "input": {"messages": [{"type": "human", "content": text}]}}); assert r.status_code == 200, r.text
    runs = c.get(f"/api/v1/threads/{t}/runs").json(); return t, str(runs[0]["run_id"])

def test_draft_api_three_modes_lint_clean_and_nothing_saved(env, a):
    e1 = env["ed1"]; ag = new_agent(e1, "soạn")
    r = e1.post("/api/skills/draft", json={"mode": "describe", "text": "Viết skill báo giá nhanh. " + jmark(), "agent_id": ag}); assert r.status_code == 200, r.text
    j = r.json(); assert j["lint"]["errors"] == [] and j["draft"]["name"] == "bao-gia-nhanh" and j["saved"] is False and "references/gia.md" in j["draft"]["files"]
    t, rid = talk(e1, ag, "Làm báo giá cho khách A " + jmark())                                       # conversation: the fake model answers with the JSON, so the thread carries it
    r = e1.post("/api/skills/draft", json={"mode": "conversation", "thread_id": t, "agent_id": ag}); assert r.status_code == 200 and r.json()["lint"]["errors"] == [], r.text
    d2 = {**DRAFT, "name": "gia-chua-vat", "description": "Dùng khi báo giá cho khách. Use when quoting (giá, quote, price): không gồm VAT.", "body": "# Giá\nKhi báo giá thì ghi rõ giá chưa VAT.\n\nThử: báo giá 3 áo.", "files": {}}
    t2, rid2 = talk(e1, ag, "Báo giá 10 áo")                                                          # fix: thread + the user's correction
    for body in ({"thread_id": t2}, {"run_id": rid2}):
        r = e1.post("/api/skills/draft", json={"mode": "fix", "text": "Sai, phải ghi giá chưa VAT. " + jmark(d2), "agent_id": ag, **body}); assert r.status_code == 200, r.text
        assert r.json()["draft"]["name"] == "gia-chua-vat" and r.json()["lint"]["errors"] == []
    names = {x["name"] for sc in ("private", "shared") for x in e1.get("/api/skills", params={"scope": sc}).json()}
    assert not names & {"bao-gia-nhanh", "gia-chua-vat"}                                              # nothing was saved
    # a draft with a secret is returned WITH its error (the UI blocks Save)
    bad = {**DRAFT, "body": "key sk-abcdefghijklmnopqrstuvwx1234\n"}
    r = e1.post("/api/skills/draft", json={"mode": "describe", "text": "viết skill " + jmark(bad)}); assert r.status_code == 200 and "E6" in {x["code"] for x in r.json()["lint"]["errors"]}
    # inputs
    assert e1.post("/api/skills/draft", json={"mode": "describe", "text": "  "}).status_code == 400
    assert e1.post("/api/skills/draft", json={"mode": "fix", "thread_id": t2}).status_code == 400
    assert e1.post("/api/skills/draft", json={"mode": "magic", "text": "xxxxxx"}).status_code == 400
    assert e1.post("/api/skills/draft", json={"mode": "conversation"}).status_code == 400
    assert e1.post("/api/skills/draft", json={"mode": "conversation", "thread_id": "nope"}).status_code == 404
    r = e1.post("/api/skills/draft", json={"mode": "describe", "text": "viết skill ngắn", "agent_id": ag}); assert r.status_code == 502                  # no JSON marker -> the fake echoes -> not a draft

def test_other_workspace_thread_is_not_readable(env, a):
    ags = a.get("/api/agents").json(); ags = ags if isinstance(ags, list) else ags.get("agents", [])
    if not ags: pytest.skip("no agent in the default workspace")
    t, _ = talk(a, ags[0]["id"], "chuyện riêng của workspace khác")
    assert env["ed1"].post("/api/skills/draft", json={"mode": "conversation", "thread_id": t}).status_code == 404

def resume_ok(c, aid, t):
    r = c.post(f"/api/v1/threads/{t}/runs/wait", json={"assistant_id": f"agent-{aid}", "command": {"resume": {"decisions": [{"type": "approve"}]}}}); assert r.status_code == 200, r.text

def test_skill_save_and_share_tools_wait_for_approval(env):
    e1 = env["ed1"]; nm = f"ai-{SUF}"
    r = e1.post("/api/agents", json={"name": f"công cụ {SUF}", "system_prompt": "Bạn là trợ lý.", "tools": ["local:skill_save", "local:skill_share", "local:skill_list"], "filesystem": False}); assert r.status_code == 200, r.text
    ag = r.json()["id"]
    def pending(): return [s["name"] for s in e1.get("/api/skills", params={"scope": "private"}).json()]
    t = e1.post("/api/v1/threads", json={"metadata": {"source": "chat", "graph_id": f"agent-{ag}"}}).json()["thread_id"]
    r = e1.post(f"/api/v1/threads/{t}/runs/wait", json={"assistant_id": f"agent-{ag}", "input": {"messages": [{"type": "human", "content": f"skill-save: {nm}"}]}}); assert r.status_code == 200, r.text
    assert e1.get(f"/api/v1/threads/{t}/runs").json()[0]["status"] == "interrupted" and nm not in pending()                                              # paused: nothing written before the approval
    resume_ok(e1, ag, t)
    mine = [s for s in e1.get("/api/skills", params={"scope": "private"}).json() if s["name"] == nm]; assert [(s["name"], s["source"], s["visibility"], s["owner_agent_id"]) for s in mine] == [(nm, "ai", "private", ag)]
    assert mine[0]["lint"]["errors"] == [] and nm not in [s["name"] for s in env["ed2"].get("/api/skills", params={"scope": "private"}).json()]
    t = e1.post("/api/v1/threads", json={"metadata": {"source": "chat", "graph_id": f"agent-{ag}"}}).json()["thread_id"]
    r = e1.post(f"/api/v1/threads/{t}/runs/wait", json={"assistant_id": f"agent-{ag}", "input": {"messages": [{"type": "human", "content": f"skill-share: {nm}"}]}}); assert e1.get(f"/api/v1/threads/{t}/runs").json()[0]["status"] == "interrupted"
    assert nm not in [s["name"] for s in e1.get("/api/skills", params={"scope": "shared"}).json()]   # still private until approved
    resume_ok(e1, ag, t)
    assert nm in [s["name"] for s in e1.get("/api/skills", params={"scope": "shared"}).json()]

def test_draft_refused_in_demo_workspace():
    import deepagent_api  # noqa: F401
    import admin
    tok = admin.USER.set({"id": 9, "username": "demo", "role": "viewer", "ws": 1, "demo": True})
    try:
        with pytest.raises(HTTPException) as e: run(SM.api_draft(SM.DraftIn(mode="describe", text="viết skill báo giá")))
        assert e.value.status_code == 403
    finally: admin.USER.reset(tok)

def test_gemini_recording_replay():
    """tests/fixtures/gd12_87_gemini_real.json = ONE real Gemini call (26/10/2026, describe mode) replayed through the same parser + lint."""
    rec = json.load(open(os.path.join(os.path.dirname(__file__), "fixtures", "gd12_87_gemini_real.json"), encoding="utf-8"))
    assert rec["cost_usd"] < 0.30 and rec["feature"] == "skill_draft"
    d = SM.parse_draft(rec["response"]); r = lint(d["name"], d["files"])
    assert r["errors"] == [] and d["description"] and d["body"], r                      # a real model's draft passes the quality check
    assert "SKILL.md" in d["files"] and d["files"]["SKILL.md"].startswith("---\nname: " + d["name"])

# ------------------------------------------------------------------ 12.89 Skills page (/skills) + the API pieces it needs
PAGE_STRINGS = ["Search skills...", "All creators", "Archived", "Describe with AI", "Write manually", "Import zip or folder", "Share with the workspace", "Open in Context Hub",
                "Quality check", "No problems found", "Needs an expert review", "Use for agent", "Everyone in the workspace (shared)", "Name already used", "{n} skill(s) imported"]

def test_skills_page_is_served_with_vi_en_strings():
    import ui_skills, ui_shell, i18n
    assert "/skills" in ui_skills.PAGES and all(s in ui_skills.PAGES["/skills"] for s in ("snew", "api/skills/draft", "api/skills/import/save", "data-menu", "scope:")), "page code"
    assert ("skills", "skills", "Skills", "sparkles", "") in ui_shell.NAV[0]                                      # menu entry next to Context Hub
    for s in PAGE_STRINGS:
        e = i18n.DICT.get(s); assert e and e.get("vi") and e["vi"] != s and e.get("src") == "en", s              # English source + Vietnamese
    assert "{n}" in i18n.DICT["{n} skill(s) imported"]["vi"]

def test_page_html_has_no_unregistered_icon():
    import ui_skills, re
    icons = open(os.path.join(os.path.dirname(__file__), "..", "app", "static", "icons.svg"), encoding="utf-8").read()
    for n in set(re.findall(r"ICN\('([a-z0-9-]+)'\)", ui_skills.PAGES["/skills"])): assert f'id="i-{n}"' in icons, n

def zip_of(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for k, v in files.items(): z.writestr(k, v)
    return b.getvalue()

def test_list_archived_parsed_and_import_save(env):
    e1, e2 = env["ed1"], env["ed2"]; nm = f"pg-{SUF}"; ag = new_agent(e1, "trang")
    r = e1.post("/api/skills", json={"name": nm, "files": sk(nm), "agent_id": ag}); assert r.status_code == 200, r.text; sid = r.json()["id"]
    g = e1.get(f"/api/skills/{sid}").json(); p = g["parsed"]
    assert p["name"] == nm and p["description"] == GOOD_DESC and p["body"].startswith("# Báo giá") and "---" not in p["body"].splitlines()[0]   # the editor fields
    row = [x for x in e1.get("/api/skills", params={"scope": "private", "q": nm}).json() if x["id"] == sid][0]; assert row["archived"] is False and row["can_edit"] is True
    assert e2.delete(f"/api/skills/{sid}").status_code == 403
    assert e1.delete(f"/api/skills/{sid}").status_code == 200
    assert sid not in [x["id"] for x in e1.get("/api/skills", params={"scope": "private", "q": nm}).json()]                         # hidden by default
    arch = e1.get("/api/skills", params={"scope": "private", "q": nm, "archived": "true"}).json(); assert [x["id"] for x in arch] == [sid] and arch[0]["archived"] is True
    # import that SAVES: one good skill, one without SKILL.md frontmatter, again -> 409 for the saved one
    good_n, bad_n = f"imp-a-{SUF}", f"imp-b-{SUF}"
    body = {"files": {f"{good_n}/SKILL.md": md(good_n, body="# I\nXem [giá](references/gia.md)\n"), f"{good_n}/references/gia.md": "A=1\n", f"{bad_n}/SKILL.md": "# none"}}
    r = e1.post("/api/skills/import/save", params={"agent_id": ag}, json=body); j = r.json(); assert r.status_code == 200, r.text
    assert j["saved"] == 1 and j["skills"][good_n]["saved"] and j["skills"][good_n]["visibility"] == "private" and not j["skills"][bad_n]["saved"] and j["skills"][bad_n]["status"] == 422, j
    assert good_n in [x["name"] for x in e1.get("/api/skills", params={"scope": "private"}).json()] and bad_n not in [x["name"] for x in e1.get("/api/skills", params={"scope": "private"}).json()]
    j = e1.post("/api/skills/import/save", params={"agent_id": ag}, json=body).json(); assert j["saved"] == 0 and j["skills"][good_n]["status"] == 409
    zn = f"imp-z-{SUF}"
    r = e1.post("/api/skills/import/save", files={"file": ("s.zip", zip_of({f"{zn}/SKILL.md": md(zn), f"{zn}/references/gia.md": "x"}), "application/zip")}); j = r.json()      # no agent: shared
    assert r.status_code == 200 and j["saved"] == 1 and j["skills"][zn]["visibility"] == "workspace", r.text
    assert zn in [x["name"] for x in e2.get("/api/skills", params={"scope": "shared"}).json()]
    assert e1.post("/api/skills/import/save", json={}).status_code == 400 and e1.post("/api/skills/import/save", files={"file": ("s.zip", b"nope", "application/zip")}).status_code == 400
    # a binary file in a folder import is refused (E5), nothing is saved for that skill
    bn = f"imp-bin-{SUF}"
    j = e1.post("/api/skills/import/save", json={"skills": {bn: {"SKILL.md": md(bn), "x.bin": "\u0000\u0001"}}}).json(); assert j["saved"] == 0 and j["skills"][bn]["status"] == 422

def test_import_save_refused_in_demo_workspace():
    import deepagent_api  # noqa: F401
    import admin
    from fastapi import HTTPException
    class Req:
        headers = {"content-type": "application/json"}
        async def json(self): return {"files": {"x/SKILL.md": md("x")}}
    tok = admin.USER.set({"id": 9, "username": "demo", "role": "viewer", "ws": 1, "demo": True})
    try:
        with pytest.raises(HTTPException) as e: run(SM.api_import(Req()))
        assert e.value.status_code == 403
    finally: admin.USER.reset(tok)

# ------------------------------------------------------------------ 12.92 try-run a Skill (scripted fake model: reads the skill only when the prompt says "báo giá")
def tmp_rows(w):
    with pg() as c:
        t = c.execute("SELECT count(*) FROM lg_threads WHERE workspace_id=%s AND thread_id LIKE 'tmp-%%'", (w,)).fetchone()[0]
        r = c.execute("SELECT count(*) FROM lg_runs WHERE workspace_id=%s AND thread_id LIKE 'tmp-%%'", (w,)).fetchone()[0]
        a = c.execute("SELECT count(*) FROM agents WHERE workspace_id=%s", (w,)).fetchone()[0]
    return t, r, a

def test_try_run_used_vs_not_used_and_leaves_no_rows(env):
    e1, e2 = env["ed1"], env["ed2"]; nm = f"thu-{SUF}"; ag = new_agent(e1, "thử skill"); other = new_agent(e1, "agent khác")
    sid = e1.post("/api/skills", json={"name": nm, "files": sk(nm), "agent_id": ag}).json()["id"]
    before = tmp_rows(env["w"])
    r = e1.post(f"/api/skills/{sid}/try", json={"prompt": "Làm báo giá cho khách A"}); assert r.status_code == 200, r.text
    j = r.json(); assert j["used_skill"] is True and j["reads"] == [f"/skills/main/{nm}/SKILL.md"] and j["agent_id"] == ag and j["skill"] == nm and j["run_id"] and "read_file" in j["answer"], j
    j = e1.post(f"/api/skills/{sid}/try", json={"prompt": "Xin chào, hôm nay trời đẹp"}).json(); assert j["used_skill"] is False and j["reads"] == [] and "Echo" in j["answer"], j
    for _ in range(40):                                                                              # the temporary thread is deleted when the run completes (server._execute)
        if tmp_rows(env["w"]) == before: break
        time.sleep(0.25)
    assert tmp_rows(env["w"]) == before                                                              # no thread, no run, no agent left behind
    # a private skill only runs with its own agent; another editor cannot see it at all
    assert e1.post(f"/api/skills/{sid}/try", json={"prompt": "báo giá", "agent_id": other}).status_code == 400
    assert e2.post(f"/api/skills/{sid}/try", json={"prompt": "báo giá"}).status_code == 404
    assert e1.post(f"/api/skills/{sid}/try", json={"prompt": "  "}).status_code == 400
    # pinned commit (name@<commit>) and a shared skill without an agent_id (the first agent without a computer is used)
    head = e1.get(f"/api/skills/{sid}").json()["commit"]
    assert e1.post(f"/api/skills/{sid}/try", json={"prompt": "báo giá", "ref": head[:8]}).json()["used_skill"] is True
    assert e1.post(f"/api/skills/{sid}/share", json={"visibility": "workspace"}).status_code == 200
    j = e2.post(f"/api/skills/{sid}/try", json={"prompt": "cho tôi báo giá"}).json(); assert j["used_skill"] is True and j["agent_id"], j

def test_try_activation_needs_confirm_cost_and_counts_hits(env):
    e1 = env["ed1"]; nm = f"kich-hoat-{SUF}"; ag = new_agent(e1, "kích hoạt")
    sid = e1.post("/api/skills", json={"name": nm, "files": sk(nm), "agent_id": ag}).json()["id"]
    act = {"should": ["báo giá áo A", "báo giá áo B"], "shouldnt": ["xin chào", "mấy giờ rồi"]}
    r = e1.post(f"/api/skills/{sid}/try", json={"activation": act}); d = r.json()["detail"]
    assert r.status_code == 400 and d["error"] == "confirm_cost_required" and d["calls"] == 4, r.text      # asks first, runs nothing
    r = e1.post(f"/api/skills/{sid}/try", json={"activation": act, "confirm_cost": True}); assert r.status_code == 200, r.text
    j = r.json(); assert (j["calls"], j["hits"], j["should_total"], j["false_positives"], j["shouldnt_total"]) == (4, 2, 2, 0, 2) and all(x["ok"] for x in j["results"]), j
    bad = {"should": ["báo giá"], "shouldnt": ["báo giá (nhầm)", "xin chào"]}                              # a prompt that triggers the skill although it should not -> false positive
    j = e1.post(f"/api/skills/{sid}/try", json={"activation": bad, "confirm_cost": True}).json(); assert j["false_positives"] == 1 and j["hits"] == 1 and [x["ok"] for x in j["results"]] == [True, False, True]
    assert e1.post(f"/api/skills/{sid}/try", json={"activation": {"should": ["a"] * 6}, "confirm_cost": True}).status_code == 422     # ≤ 5 + 5 prompts
    assert e1.post(f"/api/skills/{sid}/try", json={"activation": {"should": [], "shouldnt": []}, "confirm_cost": True}).status_code == 400

def test_try_refused_in_demo_workspace():
    import deepagent_api  # noqa: F401
    import admin
    tok = admin.USER.set({"id": 9, "username": "demo", "role": "viewer", "ws": 1, "demo": True})
    try:
        with pytest.raises(HTTPException) as e: run(SM.skill_try(1, SM.TryIn(prompt="báo giá")))
        assert e.value.status_code == 403
    finally: admin.USER.reset(tok)

def test_try_page_has_the_button_and_strings():
    import ui_skills, i18n
    h = ui_skills.PAGES["/skills"]; assert all(s in h for s in ("try-go", "try-act", "/try", "confirm_cost")), "Try button + activation test"
    for s in ("Try", "Test when it is used", "Run the test", "Skill used", "Skill not used", "Used when it should: {x}", "This runs the model {n} time(s) and costs tokens. Continue?"):
        assert i18n.DICT[s].get("vi") and i18n.DICT[s]["vi"] != s, s
