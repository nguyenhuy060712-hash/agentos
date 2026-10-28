"""GĐ11.43 virtual-computer API v2 (app/sbx_api.py): FakeDriver + MemStore, two workspaces, symlink escape. No Docker, no Postgres.
The router runs in a tiny FastAPI app inside ONE event loop (httpx ASGITransport), so background deletes can be awaited with sbx_api.drain()."""
import asyncio, os, sys
import httpx, pytest
from fastapi import FastAPI

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")
import sbx_runtime as R  # noqa: E402
import sbx_api as X  # noqa: E402
import capacity  # noqa: E402

B = "/api/v2/sandboxes/boxes"
def user(ws=1, uid=10, role="editor", demo=False): return {"ws": ws, "uid": uid, "role": role, "demo": demo, "scope": "full"}

class Env:
    def __init__(self, mp):
        self.drv, self.store = R.FakeDriver(), R.MemStore()
        self.c = R.Computers(self.store, self.drv, disk_probe=lambda: 10)
        self.who, self.threads = user(), {}
        mp.setattr(X, "_c", lambda: self.c); mp.setattr(X, "_ctx", lambda: self.who)
        async def thread(tid): return self.threads.get((self.who["ws"], tid))      # stands for lg_threads under RLS
        mp.setattr(X, "_thread", thread)
        app = FastAPI(); app.include_router(X.router)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    def as_(self, **kw): self.who = user(**kw)
    async def new(self, **kw):
        r = await self.http.post(B, json=kw); assert r.status_code == 201, r.text; return r.json()

def api(fn):
    def w(monkeypatch):
        monkeypatch.setattr(capacity, "mem_available_mb", lambda: 64_000); monkeypatch.setenv("AOS_COMPUTER_MAX_RUNNING", "8")
        async def go():
            e = Env(monkeypatch)
            try: await fn(e)
            finally: await X.drain(); await e.http.aclose()
        asyncio.run(go())
    w.__name__ = fn.__name__; return w

@api
async def test_full_flow_create_upload_list_glob_run_history_delete_twice(e):
    b = await e.new(name="dev"); i = b["id"]
    assert b["status"] == "running" and "host_state" not in b and b["internet"] == "off"
    up = await e.http.post(f"{B}/{i}/files/upload", files=[("files", ("proj/src/a.txt", b"hello", "text/plain")), ("files", ("proj/src/deep/b.md", b"# b", "text/plain"))], data={"path": "work"})
    assert up.status_code == 200 and [u["path"] for u in up.json()["uploaded"]] == ["/workspace/work/proj/src/a.txt", "/workspace/work/proj/src/deep/b.md"]
    ls = (await e.http.get(f"{B}/{i}/files", params={"path": "work/proj"})).json()
    assert [(x["name"], x["type"]) for x in ls["items"]] == [("src", "dir")]
    g = (await e.http.get(f"{B}/dev/files", params={"path": "work", "glob": "*.md"})).json()      # by NAME, recursive glob
    assert [x["path"] for x in g["items"]] == ["/workspace/work/proj/src/deep/b.md"]
    assert (await e.http.get(f"{B}/{i}/files/content", params={"path": "work/proj/src/a.txt"})).content == b"hello"
    r = await e.http.post(f"{B}/{i}/execute", json={"command": "cat /workspace/work/proj/src/a.txt"})
    assert r.json() == {**r.json(), "stdout": "hello", "stderr": "", "exit_code": 0, "success": True, "truncated": False} and len(r.json()["command_id"]) == 16
    assert (await e.http.post(f"{B}/{i}/execute", json={"command": "false"})).json()["success"] is False
    h = (await e.http.get(f"{B}/{i}/commands")).json()
    assert [x["command"] for x in h["items"]] == ["false", "cat /workspace/work/proj/src/a.txt"] and h["next_cursor"] is None
    assert [x["command"] for x in (await e.http.get(f"{B}/{i}/commands", params={"exit_code": 1})).json()["items"]] == ["false"]
    assert [x["command"] for x in (await e.http.get(f"{B}/{i}/commands", params={"q": "CAT"})).json()["items"]] == ["cat /workspace/work/proj/src/a.txt"]
    assert (await e.http.get(f"{B}/{i}/commands", params={"limit": 1})).json()["next_cursor"] == "1"
    assert (await e.http.delete(f"{B}/{i}")).status_code == 204
    await X.drain()
    assert (await e.http.delete(f"{B}/{i}")).status_code == 204 and i not in e.drv.boxes
    assert (await e.http.get(f"{B}/{i}")).status_code == 404

@api
async def test_list_filters_paging_patch_start_stop_status(e):
    ids = [(await e.new(name=f"n{k}", agent_id=7 if k else None, thread_id="t1" if k == 2 else None))["id"] for k in range(3)]
    p1 = (await e.http.get(B, params={"limit": 2})).json(); assert len(p1["items"]) == 2 and p1["next_cursor"] == "2"
    assert len((await e.http.get(B, params={"limit": 2, "cursor": p1["next_cursor"]})).json()["items"]) == 1
    assert [x["id"] for x in (await e.http.get(B, params={"thread_id": "t1"})).json()["items"]] == [ids[2]]
    assert len((await e.http.get(B, params={"agent_id": 7})).json()["items"]) == 2
    assert (await e.http.get(B, params={"kind": "files"})).json()["items"] == []
    assert (await e.http.post(f"{B}/{ids[0]}/stop")).json()["status"] == "stopped"
    assert (await e.http.get(f"{B}/{ids[0]}/status")).json()["status"] == "stopped"
    assert (await e.http.post(f"{B}/{ids[0]}/stop")).status_code == 409
    assert (await e.http.post(f"{B}/{ids[0]}/execute", json={"command": "true"})).status_code == 409        # stopped by the user stays stopped
    assert (await e.http.post(f"{B}/{ids[0]}/start")).json()["status"] == "running"
    pt = (await e.http.patch(f"{B}/{ids[0]}", json={"name": "renamed", "idle_ttl_s": 900})).json(); assert pt["name"] == "renamed" and pt["idle_ttl_s"] == 900
    assert (await e.http.patch(f"{B}/{ids[0]}", json={"idle_ttl_s": 1})).status_code == 400
    assert (await e.http.post(f"{B}/batch-delete", json={"names": [ids[0], ids[1], "ffffffffffffffff"]})).json()["results"][2] == {"name": "ffffffffffffffff", "ok": True}
    await X.drain(); assert len((await e.http.get(B)).json()["items"]) == 1
    assert (await e.http.post(f"{B}/batch-delete", json={"names": []})).status_code == 400

@api
async def test_other_workspace_gets_404_and_cannot_delete(e):
    i = (await e.new())["id"]; await e.http.post(f"{B}/{i}/files/mkdir", json={"path": "d"})
    e.as_(ws=2, uid=20, role="admin")
    for m, u, kw in [("get", f"{B}/{i}", {}), ("get", f"{B}/{i}/status", {}), ("post", f"{B}/{i}/execute", {"json": {"command": "true"}}), ("get", f"{B}/{i}/commands", {}),
                     ("get", f"{B}/{i}/files", {}), ("get", f"{B}/{i}/files/content", {"params": {"path": "x"}}), ("put", f"{B}/{i}/files/content", {"params": {"path": "x"}, "content": b"a"}),
                     ("post", f"{B}/{i}/files/mkdir", {"json": {"path": "z"}}), ("post", f"{B}/{i}/files/move", {"json": {"src": "d", "dest": "e"}}), ("delete", f"{B}/{i}/files", {"params": {"path": "d"}}),
                     ("post", f"{B}/{i}/start", {}), ("post", f"{B}/{i}/stop", {}), ("patch", f"{B}/{i}", {"json": {"name": "x"}})]:
        assert (await getattr(e.http, m)(u, **kw)).status_code == 404, (m, u)
    assert (await e.http.get(B)).json()["items"] == []
    assert (await e.http.delete(f"{B}/{i}")).status_code == 204; await X.drain()
    e.as_(); assert (await e.http.get(f"{B}/{i}")).status_code == 200 and e.store.rows[i]["status"] == "running"      # still there

@api
async def test_same_workspace_other_user_404_admin_ok_viewer_cannot_write(e):
    i = (await e.new())["id"]
    e.as_(uid=11); assert (await e.http.get(f"{B}/{i}")).status_code == 404
    e.as_(uid=99, role="admin"); assert (await e.http.post(f"{B}/{i}/execute", json={"command": "true"})).status_code == 200
    e.as_(role="viewer"); assert (await e.http.get(f"{B}/{i}")).status_code == 200
    assert (await e.http.post(f"{B}/{i}/execute", json={"command": "true"})).status_code == 403
    assert (await e.http.put(f"{B}/{i}/files/content", params={"path": "a"}, content=b"x")).status_code == 403

@api
async def test_thread_box_readable_by_anyone_who_sees_the_thread_but_not_writable(e):
    e.threads[(1, "T1")] = {"thread_id": "T1", "agent_id": 3}
    a = (await e.http.post("/api/threads/T1/computer/activate")).json(); assert a["thread_id"] == "T1" and a["agent_id"] == 3 and a["status"] == "running"
    assert (await e.http.post("/api/threads/T1/computer/activate")).json()["id"] == a["id"]        # reused, not duplicated
    await e.http.put(f"{B}/{a['id']}/files/content", params={"path": "n.txt"}, content=b"note")
    await e.http.post(f"{B}/{a['id']}/execute", json={"command": "echo hi"})
    assert (await e.http.post(f"{B}/{a['id']}/stop")).status_code == 200; assert (await e.http.post("/api/threads/T1/computer/activate")).json()["status"] == "running"
    e.as_(uid=11, role="viewer")
    assert (await e.http.get(f"{B}/{a['id']}/files/content", params={"path": "n.txt"})).content == b"note"
    assert (await e.http.get(f"{B}/{a['id']}/commands")).json()["items"][0]["command"] == "echo hi"
    assert (await e.http.post(f"{B}/{a['id']}/execute", json={"command": "true"})).status_code == 404
    assert (await e.http.put(f"{B}/{a['id']}/files/content", params={"path": "n.txt"}, content=b"x")).status_code == 404
    e.threads.clear(); assert (await e.http.get(f"{B}/{a['id']}")).status_code == 404            # cannot see the thread (any more): no access
    assert (await e.http.post("/api/threads/NOPE/computer/activate")).status_code == 404
    e.as_(ws=2); e.threads[(2, "T1")] = {"thread_id": "T1"}; assert (await e.http.get(f"{B}/{a['id']}")).status_code == 404      # same thread id, other workspace

@api
async def test_demo_refused_and_workspace_switch_off(e):
    i = (await e.new())["id"]
    e.as_(demo=True)
    for r in [await e.http.get(B), await e.http.post(B, json={}), await e.http.get(f"{B}/{i}"), await e.http.post(f"{B}/{i}/execute", json={"command": "true"}), await e.http.delete(f"{B}/{i}")]:
        assert r.status_code == 403
    assert e.store.rows[i]["status"] == "running"
    e.as_(); e.store.quotas[1] = {"enabled": False, "max_running": 3, "max_total": 10, "internet": "off"}
    for r in [await e.http.get(B), await e.http.post(B, json={}), await e.http.post(f"{B}/{i}/execute", json={"command": "true"}), await e.http.get(f"{B}/{i}/files")]:
        assert r.status_code == 403
    assert (await e.http.delete(f"{B}/{i}")).status_code == 204        # cleanup is still allowed

@api
async def test_path_traversal_refused_everywhere(e):
    i = (await e.new())["id"]
    for bad in ["../etc/passwd", "a/../../x", "/workspace/../etc", "/proc/1/environ", "/sys/x", "a\x00b"]:
        for r in [await e.http.get(f"{B}/{i}/files/content", params={"path": bad}), await e.http.put(f"{B}/{i}/files/content", params={"path": bad}, content=b"x"),
                  await e.http.delete(f"{B}/{i}/files", params={"path": bad}), await e.http.post(f"{B}/{i}/files/mkdir", json={"path": bad}),
                  await e.http.post(f"{B}/{i}/files/move", json={"src": "ok", "dest": bad}), await e.http.get(f"{B}/{i}/files", params={"path": bad})]:
            assert r.status_code == 400, (bad, r.status_code)
    up = await e.http.post(f"{B}/{i}/files/upload", files=[("files", ("../../evil.txt", b"x", "text/plain"))]); assert up.status_code == 400
    up = await e.http.post(f"{B}/{i}/files/upload", files=[("files", ("a/../../evil.txt", b"x", "text/plain"))]); assert up.status_code == 400
    assert not any("evil" in k for k in e.drv.boxes[i]["files"])
    # an absolute path outside /workspace is not a traversal but is confined too
    assert (await e.http.put(f"{B}/{i}/files/content", params={"path": "/etc/cron.d/x"}, content=b"x")).status_code == 403
    assert (await e.http.delete(f"{B}/{i}/files", params={"path": "/workspace"})).status_code == 403          # the root itself cannot go

@api
async def test_symlink_escape_refused_for_every_operation(e):
    i = (await e.new())["id"]; box = e.drv.boxes[i]
    box["files"]["/etc/secret"] = b"HOST-SECRET"; box["files"]["/workspace/inside.txt"] = b"ok"; box["dirs"] = {"/workspace/real"}; box.setdefault("links", {})
    box["links"].update({"/workspace/evil": "/etc", "/workspace/evilfile": "/etc/secret", "/workspace/fine": "/workspace/inside.txt", "/workspace/rel": "../etc",
                         "/workspace/real/up": "../../etc", "/workspace/loop1": "/workspace/loop2", "/workspace/loop2": "/workspace/loop1"})
    g = lambda p: e.http.get(f"{B}/{i}/files/content", params={"path": p})
    assert (await g("evilfile")).status_code == 403 and (await g("evil/secret")).status_code == 403 and (await g("rel/secret")).status_code == 403 and (await g("real/up/secret")).status_code == 403
    assert b"HOST-SECRET" not in (await g("evil/secret")).content
    assert (await e.http.get(f"{B}/{i}/files", params={"path": "evil"})).status_code == 403
    assert (await e.http.get(f"{B}/{i}/files", params={"path": "real/up"})).status_code == 403
    assert (await e.http.put(f"{B}/{i}/files/content", params={"path": "evil/pwned"}, content=b"x")).status_code == 403
    assert (await e.http.put(f"{B}/{i}/files/content", params={"path": "evilfile"}, content=b"x")).status_code == 403
    assert (await e.http.post(f"{B}/{i}/files/mkdir", json={"path": "evil/new"})).status_code == 403
    assert (await e.http.post(f"{B}/{i}/files/move", json={"src": "inside.txt", "dest": "evil/x"})).status_code == 403
    assert (await e.http.post(f"{B}/{i}/files/move", json={"src": "evil/secret", "dest": "stolen"})).status_code == 403
    assert (await e.http.delete(f"{B}/{i}/files", params={"path": "evil/secret"})).status_code == 403
    assert (await e.http.post(f"{B}/{i}/files/upload", files=[("files", ("evil/up.txt", b"x", "text/plain"))])).status_code == 403
    assert (await g("loop1")).status_code in (400, 403)
    assert box["files"]["/etc/secret"] == b"HOST-SECRET" and "/etc/pwned" not in box["files"] and "/etc/up.txt" not in box["files"] and "/stolen" not in box["files"]
    assert (await g("fine")).content == b"ok"                                                         # a link that stays inside /workspace is fine
    # deleting a link removes the LINK, never its target
    assert (await e.http.delete(f"{B}/{i}/files", params={"path": "evilfile"})).status_code == 200
    assert "/workspace/evilfile" not in box["links"] and box["files"]["/etc/secret"] == b"HOST-SECRET"
    names = [x["name"] for x in (await e.http.get(f"{B}/{i}/files")).json()["items"]]; assert "evil" in names      # listed as a link, not followed
    assert {x["name"]: x["type"] for x in (await e.http.get(f"{B}/{i}/files")).json()["items"]}["evil"] == "link"

@api
async def test_file_ops_move_mkdir_delete_overwrite_and_limits(e):
    i = (await e.new())["id"]
    assert (await e.http.post(f"{B}/{i}/files/mkdir", json={"path": "a/b"})).status_code == 200
    assert (await e.http.put(f"{B}/{i}/files/content", params={"path": "a/b/f.txt"}, content=b"one")).json()["bytes"] == 3
    assert (await e.http.put(f"{B}/{i}/files/content", params={"path": "a/b/f.txt"}, content=b"twotwo")).status_code == 200       # overwrite
    assert (await e.http.post(f"{B}/{i}/files/move", json={"src": "a", "dest": "c/a2"})).status_code == 200
    assert (await e.http.get(f"{B}/{i}/files/content", params={"path": "c/a2/b/f.txt"})).content == b"twotwo"
    assert (await e.http.get(f"{B}/{i}/files/content", params={"path": "a/b/f.txt"})).status_code == 404
    assert (await e.http.post(f"{B}/{i}/files/move", json={"src": "c/a2", "dest": "c/a2"})).status_code == 409
    assert (await e.http.post(f"{B}/{i}/files/move", json={"src": "c/a2", "dest": "c/a2/inner"})).status_code == 400
    assert (await e.http.put(f"{B}/{i}/files/content", params={"path": "c"}, content=b"x")).status_code == 400      # writing onto a folder
    assert (await e.http.delete(f"{B}/{i}/files", params={"path": "c"})).status_code == 200
    assert (await e.http.delete(f"{B}/{i}/files", params={"path": "c"})).status_code == 404
    r = await e.http.get(f"{B}/{i}/files/content", params={"path": "/workspace/.aos"}); assert r.status_code in (404,)
    big = await e.http.put(f"{B}/{i}/files/content", params={"path": "big"}, content=b"x" * (R.PUT_MAX + 1)); assert big.status_code == 413
    h = (await e.http.get(f"{B}/{i}/files/content", params={"path": "a.txt"})); assert h.status_code == 404
    await e.http.put(f"{B}/{i}/files/content", params={"path": "x.html"}, content=b"<script>1</script>")
    d = await e.http.get(f"{B}/{i}/files/content", params={"path": "x.html"})
    assert d.headers["content-disposition"].startswith("attachment") and d.headers["x-content-type-options"] == "nosniff" and d.headers["content-security-policy"] == "sandbox"
    assert (await e.http.post(f"{B}/{i}/files/upload", files=[])).status_code == 400

@api
async def test_execute_validation_env_and_idle_wake(e):
    i = (await e.new())["id"]
    r = await e.http.post(f"{B}/{i}/execute", json={"command": "env FOO", "env": {"FOO": "bar"}}); assert r.json()["stdout"] == "bar\n"
    assert (await e.http.post(f"{B}/{i}/execute", json={"command": "echo $x", "env": {"1bad": "x"}})).status_code == 400
    assert (await e.http.post(f"{B}/{i}/execute", json={"command": ""})).status_code == 422
    assert (await e.http.post(f"{B}/{i}/execute", json={"command": "sleep 5", "timeout": 1})).json()["success"] is False
    assert (await e.http.post(f"{B}/{i}/execute", json={"command": "pwd", "cwd": "../x"})).status_code == 400
    attrs = R.SPAN_ATTRS.get(); assert attrs is None or attrs["sandbox.kind"] == "computer"
    await e.c._stop_box(e.store.rows[i], idle=True)                                           # the idle reaper stopped it: the next call wakes it, files intact
    await e.http.put(f"{B}/{i}/files/content", params={"path": "k.txt"}, content=b"kept")
    assert e.store.rows[i]["status"] == "running"
    assert (await e.http.post(f"{B}/{i}/execute", json={"command": "cat /workspace/k.txt"})).json()["stdout"] == "kept"

def test_secret_host_state_never_in_view():
    v = X._view({"id": "a" * 16, "status": "running", "workspace_id": 1, "host_state": {"egress": {"mode": "basic", "secret": "S3CRET", "ip": "10.0.0.2"}}})
    assert "S3CRET" not in str(v) and v["internet"] == "basic"

def test_real_in_box_script_confines_symlinks(tmp_path):
    """The REAL bash script of the Docker driver, run here with /workspace replaced by a temp folder (needs GNU bash / coreutils / findutils: WSL or Linux)."""
    import shutil, subprocess
    if not (shutil.which("bash") and shutil.which("realpath") and shutil.which("find")) or os.name == "nt": pytest.skip("needs a Linux userland")
    ws, out = tmp_path / "ws", tmp_path / "outside"; ws.mkdir(); out.mkdir(); (out / "secret").write_text("HOST-SECRET"); (ws / "ok.txt").write_text("ok")
    (ws / "evil").symlink_to(out); (ws / "evilfile").symlink_to(out / "secret"); (ws / "fine").symlink_to(ws / "ok.txt")
    script = X._FS.replace("/workspace", str(ws))
    def run(op, path, dest="", sh="1", data=None): return subprocess.run(["bash", "-c", script, "_", op, str(ws / path) if path else str(ws), str(ws / dest) if dest else "", sh], input=data, capture_output=True)
    assert run("read", "ok.txt").stdout.endswith(b"ok")
    assert run("read", "fine").stdout.endswith(b"ok")
    for op, p, kw in [("read", "evilfile", {}), ("read", "evil/secret", {}), ("ls", "evil", {}), ("write", "evil/pwn", {"data": b"x"}), ("write", "evilfile", {"data": b"x"}), ("mkdir", "evil/d", {}),
                      ("mv", "ok.txt", {"dest": "evil/x"}), ("mv", "evil/secret", {"dest": "stolen"}), ("rm", "evil/secret", {})]:
        assert run(op, p, **kw).returncode == 43, (op, p)
    assert (out / "secret").read_text() == "HOST-SECRET" and not (out / "pwn").exists() and not (out / "x").exists() and not (ws / "stolen").exists()
    assert run("rm", "evilfile").returncode == 0 and not (ws / "evilfile").is_symlink() and (out / "secret").exists()      # the link goes, never its target
    assert run("rm", "").returncode == 43 and ws.exists()                                                                     # the root cannot be deleted
    assert run("write", "a/b/c.txt", data=b"deep").returncode == 0 and (ws / "a/b/c.txt").read_bytes() == b"deep"
    assert run("mv", "a", dest="z/a2").returncode == 0 and (ws / "z/a2/b/c.txt").exists() and run("mv", "z", dest="ok.txt").returncode == 45 and run("rm", "nope").returncode == 44
    ls = run("ls", "").stdout.split(b"\0"); assert any(str(ws / "ok.txt").encode() in e for e in ls)
