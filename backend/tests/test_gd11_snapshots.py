"""GĐ11.47a snapshots (capture) + setup.sh bake (app/sbx_images.py, migration 0051, routes in sbx_api.py).
FakeDriver / MemStore / MemSnapStore / temp dirs: no Docker, no Postgres. Real-box part: AOS_REAL_SANDBOX=1 (bottom of the file)."""
import asyncio, base64, os, shutil, subprocess, sys
import httpx, pytest
from fastapi import FastAPI

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")
import sbx_runtime as R  # noqa: E402
import sbx_images as S  # noqa: E402
import sbx_api as X  # noqa: E402
import capacity  # noqa: E402
import migrations as M  # noqa: E402
import egress_ctl as E  # noqa: E402

run = asyncio.run
def ctx(ws=1, uid=10, role="editor", demo=False): return {"ws": ws, "uid": uid, "role": role, "demo": demo, "scope": "full"}

class ScriptDriver(R.FakeDriver):
    """FakeDriver that also understands `bash -e <script>`: runs each line of the script from the box's files with the fake shell."""
    async def _fake_run(self, b, w, cwd, env, timeout):
        if w[:2] == ["bash", "-e"]:
            import shlex
            out, code = b"", 0
            for line in b["files"][w[2]].decode().splitlines():
                line = line.strip()
                if line.startswith("writeenv "): b["files"][line.split()[1]] = env[line.split()[2]].encode(); continue   # test-only: write an env value to a file
                o, code, _ = await super()._fake_run(b, shlex.split(line), cwd, env, timeout); out += o
                if code: break
            return out, code, False
        if w[:2] == ["rm", "-rf"]:
            for k in [k for k in b["files"] if k.startswith(w[2])]: del b["files"][k]
            return b"", 0, False
        return await super()._fake_run(b, w, cwd, env, timeout)

class Env:
    def __init__(self, tmp_path, monkeypatch, secrets=None):
        monkeypatch.setattr(capacity, "mem_available_mb", lambda: 64_000); monkeypatch.setenv("AOS_COMPUTER_MAX_RUNNING", "8")
        self.drv, self.store = ScriptDriver(), R.MemStore()
        self.store.quotas[1] = {"enabled": True, "max_running": 3, "max_total": 10, "internet": "basic"}      # the bake box needs Internet `basic`
        self.c = R.Computers(self.store, self.drv, disk_probe=lambda: 10, egress=E.EgressCtl(self.store, E.FakeNet(), E.ListSink(), directory="/nonexistent", demo_ws=set(), serve=False))
        self.secrets = secrets if secrets is not None else {}
        async def load(ws, ident): return dict(self.secrets)
        self.img = S.Images(self.c, S.MemSnapStore(), None, tmp_path, load)
        self.store.identifiers[7] = "Helper Bot"
        self.u = ctx()
    async def box(self, **kw): return await self.c.create(self.u, name="b", **kw)

@pytest.fixture
def env(tmp_path, monkeypatch): return Env(tmp_path, monkeypatch)

# ------------------------------------------------------------------ secret scan
def test_secret_needles_find_plain_and_base64_in_every_alignment(tmp_path):
    sec = "sk-live-ABCDEF123456"
    for pad in range(4):
        for enc in (base64.b64encode, base64.urlsafe_b64encode):
            blob = os.urandom(50) + b"x" * pad + enc(b"x" * pad + sec.encode() + b"tail") + os.urandom(50)   # pad moves the base64 alignment
            f = tmp_path / "f"; f.write_bytes(blob)
            assert S.scan_file(f, S.secret_needles([sec])), (pad, enc)
    f = tmp_path / "g"; f.write_bytes(b"nothing here " * 1000 + sec.encode()); assert S.scan_file(f, S.secret_needles([sec]), chunk=7)    # needle across chunk edges
    f.write_bytes(b"clean " * 100); assert not S.scan_file(f, S.secret_needles([sec])) and not S.scan_file(f, S.secret_needles(["ab"]))   # too-short values are ignored
    assert S.redact(f"key {sec} and {base64.b64encode(sec.encode()).decode()}", [sec]) == "key *** and ***"

# ------------------------------------------------------------------ capture / restore
def test_capture_restore_and_delete_rules(env):
    async def go():
        b = await env.box(); await env.c.exec(env.u, b["id"], "write /workspace/a.txt hello"); await env.c.exec(env.u, b["id"], "write /etc/pkg installed")
        await env.c.put_file(env.u, b["id"], "/opt/aos-user/venv/lib.py", b"v")
        snap = await env.img.capture(env.u, b["id"], "first")
        assert snap["status"] == "ready" and snap["archive_bytes"] > 0 and os.path.exists(snap["archive_path"]) and snap["image_tag"].startswith("aos-snap-")
        assert not [f for f in os.listdir(env.img.root()) if f.endswith(".tar")]                       # the plain tar is gone, only .tar.zst is kept
        with pytest.raises(R.BadState): await env.img.capture(env.u, b["id"], "first")                  # name unique per workspace
        await env.c.exec(env.u, b["id"], "write /workspace/a.txt CHANGED")
        nb = await env.img.create_box(env.u, "first", name="copy")                                      # by name
        assert nb["status"] == "running" and nb["image"] == snap["image_tag"]
        assert await env.drv.get(nb["id"], "/workspace/a.txt") == b"hello"                              # volume archive restored (state at capture time)
        assert await env.drv.get(nb["id"], "/etc/pkg") == b"installed"                                  # root FS (installed packages) came with the commit
        assert await env.drv.get(nb["id"], "/opt/aos-user/venv/lib.py") == b"v"
        assert await env.drv.get(b["id"], "/workspace/a.txt") == b"CHANGED"                             # the old box is unchanged
        with pytest.raises(R.BadState): await env.img.delete(env.u, snap["id"])                         # 409 while a box uses it
        await env.c.delete(env.u, nb["id"])
        assert await env.img.delete(env.u, snap["id"]) is True and await env.img.delete(env.u, snap["id"]) is True      # repeat = still fine
        assert not os.path.exists(snap["archive_path"]) and snap["image_tag"] not in env.drv.images
        with pytest.raises(R.NotFound): await env.img.get(env.u, "first")
    run(go())

def test_capture_secret_in_files_is_refused_and_cleaned_up(env):
    async def go():
        b = await env.box(); await env.c.put_file(env.u, b["id"], "/workspace/.env", b"TOKEN=" + base64.b64encode(b"hunter2-hunter2").rstrip(b"="))
        with pytest.raises(S.BakeRefused): await env.img.capture(env.u, b["id"], "leaky", secrets=["hunter2-hunter2"])
        assert await env.img.list(env.u) == [] and os.listdir(env.img.root()) == [] and env.drv.images == {}
    run(go())

def test_caps_count_size_and_permissions(env, monkeypatch):
    async def go():
        b = await env.box(); monkeypatch.setenv("AOS_SNAP_MAX_COUNT", "2")
        await env.img.capture(env.u, b["id"], "s1"); await env.img.capture(env.u, b["id"], "s2")
        with pytest.raises(R.QuotaExceeded): await env.img.capture(env.u, b["id"], "s3")
        monkeypatch.setenv("AOS_SNAP_MAX_COUNT", "9"); monkeypatch.setenv("AOS_SNAP_MAX_MB", "1")
        await env.c.put_file(env.u, b["id"], "/workspace/big", os.urandom(2 << 20))
        with pytest.raises(R.SbxError, match="larger than 1 MB"): await env.img.capture(env.u, b["id"], "toobig")
        assert "toobig" not in [r["name"] for r in await env.img.list(env.u)] and len(os.listdir(env.img.root())) == 2      # nothing half-built stays
        monkeypatch.setenv("AOS_SNAP_MAX_MB", "100"); monkeypatch.setenv("AOS_SNAP_MAX_GB", "0")
        with pytest.raises(R.QuotaExceeded): await env.img.capture(env.u, b["id"], "nospace")
        other = ctx(ws=2, uid=11)                                                                       # another workspace sees nothing
        assert await env.img.list(other) == []
        with pytest.raises(R.NotFound): await env.img.get(other, "s1")
        with pytest.raises(R.Forbidden): await env.img.delete(ctx(uid=12, role="viewer"), "s1")
        with pytest.raises(R.Forbidden): await env.img.delete(ctx(uid=12), "s1")                        # editor who is not the owner
        assert await env.img.delete(ctx(uid=12, role="admin"), "s1") is True
        with pytest.raises(R.Forbidden): await env.img.capture(ctx(demo=True), b["id"], "demo")
    run(go())

# ------------------------------------------------------------------ bake
SETUP = "write /etc/tool installed-by-setup\necho done"

def test_bake_new_thread_has_package_old_unchanged_rebuild_only_on_hash_change(env):
    async def go():
        old = await env.box()
        row = await env.img.bake(env.u, 7, SETUP)
        assert row["name"] == "agent-helper-bot--setup" and row["status"] == "ready" and row["kind"] == "bake" and row["image_tag"].startswith("aos-ws1-agent-helper-bot--setup:")
        again = await env.img.bake(env.u, 7, SETUP)
        assert again["image_tag"] == row["image_tag"] and len(env.drv.images) == 1                         # same hash -> nothing rebuilt
        assert list(env.drv.boxes) == [old["id"]]                                                          # the temp box is gone
        assert env.store.rows[old["id"]]["status"] == "running" and "/etc/tool" not in env.drv.boxes[old["id"]]["files"]       # running boxes never change
        nb = await env.img.create_box(env.u, row["name"], name="t2", agent_id=7)
        assert await env.drv.get(nb["id"], "/etc/tool") == b"installed-by-setup"
        await env.c.stop(env.u, nb["id"]); await env.c.start(env.u, nb["id"]); assert await env.drv.get(nb["id"], "/etc/tool") == b"installed-by-setup"
        new = await env.img.bake(env.u, 7, SETUP + "\nwrite /etc/tool2 v2")
        assert new["image_tag"] != row["image_tag"] and new["setup_hash"] != row["setup_hash"] and row["image_tag"] in env.drv.images     # old image still used by nb: kept
        await env.c.delete(env.u, nb["id"])
    run(go())

def test_bake_failure_keeps_old_image_and_stores_log(env):
    async def go():
        ok = await env.img.bake(env.u, 7, SETUP)
        with pytest.raises(S.BakeRefused, match="exited with code 3"): await env.img.bake(env.u, 7, "echo before\nexit 3")
        cur = await env.img.get(env.u, ok["name"])
        assert cur["status"] == "ready" and cur["image_tag"] == ok["image_tag"] and cur["setup_hash"] == ok["setup_hash"] and "FAILED (old image kept)" in cur["build_log"] and "before" in cur["build_log"]
        assert list(env.drv.images) == [ok["image_tag"]]
        with pytest.raises(S.BakeRefused): await env.img.bake(env.u, 8, "exit 1", identifier="other")        # first build fails: no image, row says failed
        bad = await env.img.get(env.u, "agent-other--setup"); assert bad["status"] == "failed" and bad.get("image_tag") is None
        with pytest.raises(R.SbxError): await env.img.create_box(env.u, "agent-other--setup")
        assert list(env.drv.boxes) == []                                                                     # every temp box removed
    run(go())

def test_bake_refuses_when_setup_writes_the_key(env):
    env.secrets["API_KEY"] = "sk-very-secret-value-123"
    async def go():
        with pytest.raises(S.BakeRefused, match="setup.sh đã ghi khoá vào máy"): await env.img.bake(env.u, 7, "writeenv /root/.cfg API_KEY")
        assert env.drv.images == {} and env.drv.boxes == {}
        row = await env.img.get(env.u, "agent-helper-bot--setup"); assert row["status"] == "failed" and "sk-very-secret" not in (row["build_log"] or "")
        with pytest.raises(S.BakeRefused): await env.img.bake(env.u, 7, "echo sk-very-secret-value-123")      # key typed into the script itself
        ok = await env.img.bake(env.u, 7, "write /etc/ok fine"); assert ok["status"] == "ready"               # the secret only in the environment, never written: fine
    run(go())

# ------------------------------------------------------------------ purge hook + API + migration
def test_purge_agent_removes_rows_archives_and_images(env, monkeypatch):
    async def go():
        monkeypatch.setattr(S, "shared", lambda: env.img)
        assert S.purge_hook in R.PURGE_HOOKS
        b = await env.box(agent_id=7); await env.img.capture(env.u, b["id"], "cap")
        await env.img.bake(env.u, 7, SETUP); other = await env.img.capture(env.u, b["id"], "keep"); env.img.store.rows[other["id"]]["agent_id"] = None
        files = len(os.listdir(env.img.root()))
        res = await env.c.purge_for_agent(7)
        assert res["extra"]["purge_hook"]["snapshots"] == 2 and res["errors"] == []
        assert [r["name"] for r in await env.img.list(env.u)] == ["keep"] and len(os.listdir(env.img.root())) == files - 1 and len(env.drv.images) == 1
    run(go())

# X-1 (11.54): the Docker tag of a baked image carries the workspace; identifiers are not unique across workspaces
def test_x1_same_identifier_in_two_workspaces_gets_two_docker_tags_and_purge_stays_in_its_workspace(env, monkeypatch):
    env.store.quotas[2] = {"enabled": True, "max_running": 3, "max_total": 10, "internet": "basic"}; env.store.identifiers[8] = "Helper Bot"
    u2 = ctx(ws=2, uid=20)
    async def go():
        r1 = await env.img.bake(env.u, 7, SETUP); r2 = await env.img.bake(u2, 8, SETUP)               # same identifier, same setup.sh, other workspace
        assert r1["image_tag"] == f"aos-ws1-agent-helper-bot--setup:{r1['setup_hash']}" and r2["image_tag"] == f"aos-ws2-agent-helper-bot--setup:{r2['setup_hash']}"
        assert r1["image_tag"] != r2["image_tag"] and set(env.drv.images) == {r1["image_tag"], r2["image_tag"]}
        assert await env.img.purge_agent(8, 3) == {"snapshots": 0, "errors": []} and len(env.drv.images) == 2           # a purge aimed at a third workspace touches nothing
        assert (await env.img.purge_agent(8, 2))["snapshots"] == 1                                                      # workspace 2 purges its own agent ...
        assert set(env.drv.images) == {r1["image_tag"]} and (await env.img.get(env.u, r1["name"]))["status"] == "ready"  # ... workspace 1 keeps image and row
        with pytest.raises(R.NotFound): await env.img.get(u2, r1["name"])
        nb = await env.img.create_box(env.u, r1["name"], name="still-works", agent_id=7); assert nb["status"] == "running"
        assert (await env.img.purge_agent(7))["snapshots"] == 0 and r1["image_tag"] in env.drv.images                 # a box still uses it: row and image stay (repeatable)
        await env.c.delete(env.u, nb["id"])
        assert (await env.img.purge_agent(7))["snapshots"] == 1 and "aos-ws1-agent-helper-bot--setup" not in str(list(env.drv.images))   # workspace_id=None: only the agent's own workspace
    run(go())

def test_old_shared_setup_tag_is_rebuilt_under_the_workspace_tag_and_never_removed_while_another_row_uses_it(env):
    env.store.quotas[2] = {"enabled": True, "max_running": 3, "max_total": 10, "internet": "basic"}; env.store.identifiers[8] = "Helper Bot"
    u2 = ctx(ws=2, uid=20)
    async def go():
        r1 = await env.img.bake(env.u, 7, SETUP); r2 = await env.img.bake(u2, 8, SETUP); legacy = f"agent-helper-bot--setup:{r1['setup_hash']}"
        env.drv.images[legacy] = env.drv.images.pop(r1["image_tag"]); env.drv.images.pop(r2["image_tag"])             # the state before the fix: ONE shared tag, both rows point at it
        for r in (r1, r2): await env.img.store.update(r["id"], image_tag=legacy)
        n1 = await env.img.bake(env.u, 7, SETUP)                                                                    # same hash: still rebuilt (never reuse / retag content that may come from another workspace)
        assert n1["image_tag"] == r1["image_tag"] != legacy and legacy in env.drv.images and (await env.img.get(u2, r2["name"]))["image_tag"] == legacy   # workspace 2 still has its image
        n2 = await env.img.bake(u2, 8, SETUP)
        assert n2["image_tag"] == r2["image_tag"] and legacy not in env.drv.images and set(env.drv.images) == {n1["image_tag"], n2["image_tag"]}    # the last row to leave the old tag removes it
    run(go())

def test_x1_workspace_tag_cannot_be_deleted_through_another_workspace(env):
    env.store.quotas[2] = {"enabled": True, "max_running": 3, "max_total": 10, "internet": "basic"}
    u2 = ctx(ws=2, uid=20)
    async def go():
        r1 = await env.img.bake(env.u, 7, SETUP)
        with pytest.raises(R.NotFound): await env.img.get(u2, r1["name"])
        assert await env.img.delete(u2, r1["name"]) is True and r1["image_tag"] in env.drv.images                   # a "delete" by name from workspace 2 finds nothing of workspace 1
        with pytest.raises(R.Forbidden): await env.img.delete(ctx(uid=12, role="viewer"), r1["name"])             # unauthorised role refused
        assert r1["image_tag"] in env.drv.images
    run(go())

def test_api_routes(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    monkeypatch.setattr(X, "_c", lambda: e.c); monkeypatch.setattr(X, "_ctx", lambda: e.u); monkeypatch.setattr(X, "_i", lambda: e.img)
    app = FastAPI(); app.include_router(X.router)
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as h:
            b = (await h.post("/api/v2/sandboxes/boxes", json={"name": "m"})).json()
            r = await h.post(f"/api/v2/sandboxes/boxes/{b['id']}/snapshot", json={"name": "s"}); assert r.status_code == 201, r.text
            v = r.json(); assert v["status"] == "ready" and "archive_path" not in v and "image_tag" not in v
            assert (await h.get("/api/v2/sandboxes/snapshots")).json()["items"][0]["id"] == v["id"] and (await h.get("/api/v2/sandboxes/snapshots/s")).json()["id"] == v["id"]
            r = await h.post("/api/v2/sandboxes/snapshots/s/boxes", json={"name": "c2"}); assert r.status_code == 201 and r.json()["name"] == "c2"
            assert (await h.delete("/api/v2/sandboxes/snapshots/s")).status_code == 409
            await e.c.delete(e.u, r.json()["id"])
            assert (await h.delete("/api/v2/sandboxes/snapshots/s")).status_code == 204 and (await h.delete("/api/v2/sandboxes/snapshots/s")).status_code == 204
            assert (await h.get("/api/v2/sandboxes/snapshots/s")).status_code == 404
            r = await h.post("/api/v2/sandboxes/snapshots/bake", json={"agent_id": 7, "setup_sh": "exit 2"}); assert r.status_code == 422
            r = await h.post("/api/v2/sandboxes/snapshots/bake", json={"agent_id": 7, "setup_sh": SETUP}); assert r.status_code == 201 and r.json()["kind"] == "bake"
            e.u = ctx(demo=True); assert (await h.get("/api/v2/sandboxes/snapshots")).status_code == 403
    run(go())

def test_migration_0051_registered_and_plan():
    assert "0051" in [v for v, _, _ in M._load()]
    import importlib; m = importlib.import_module("migrations.0051_sandbox_snapshots_v2")
    assert run(m.plan(None)) and m.VERSION == "0051"

# ------------------------------------------------------------------ real box (AOS_REAL_SANDBOX=1): bake through a real gVisor box
real = pytest.mark.skipif(os.environ.get("AOS_REAL_SANDBOX") != "1" or shutil.which("docker") is None, reason="needs AOS_REAL_SANDBOX=1 and Docker")

@real
def test_real_capture_and_bake(tmp_path):
    if subprocess.run(["docker", "image", "inspect", R.IMAGE], capture_output=True).returncode: pytest.skip(f"image {R.IMAGE} not built")
    u = ctx()
    async def go():
        c = R.Computers(R.MemStore(), R.DockerGvisorDriver(), disk_probe=lambda: 10)
        async def load(ws, ident): return {"BAKE_KEY": "zz-real-bake-secret-9876"}
        img = S.Images(c, S.MemSnapStore(), None, tmp_path, load); made = []
        try:
            row = await img.bake(u, 91, 'mkdir -p /opt/baked && echo ok > /opt/baked/marker && test -n "$BAKE_KEY"', identifier="real47a", internet="off")
            b = await img.create_box(u, row["name"], name="real-after"); made.append(b["id"])
            assert (await c.exec(u, b["id"], "cat /opt/baked/marker"))["output"].strip() == "ok"
            await c.stop(u, b["id"]); await c.start(u, b["id"])
            assert (await c.exec(u, b["id"], "cat /opt/baked/marker"))["output"].strip() == "ok"
            await c.exec(u, b["id"], "echo mine > /workspace/f.txt"); await img.capture(u, b["id"], "real-cap")
            b2 = await img.create_box(u, "real-cap", name="real-copy"); made.append(b2["id"])
            assert (await c.exec(u, b2["id"], "cat /workspace/f.txt /opt/baked/marker"))["output"].split() == ["mine", "ok"]
            with pytest.raises(S.BakeRefused): await img.bake(u, 91, "echo $BAKE_KEY > /opt/leak", identifier="real47a", internet="off")
        finally:
            for i in made:
                try: await c.delete(u, i)
                except Exception: pass  # noqa: BLE001
            await img.purge_agent(91)
            for r in await img.store.list(1): await img.delete(u, r["id"])
    run(go())
