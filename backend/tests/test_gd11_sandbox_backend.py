"""GĐ11.41 the virtual computer as an agent's file system + shell (app/sbx_backend.py, app/sbx_cfg.py). deepagents 0.7.32.
No Docker, no real LLM: MemStore + DiskDriver (tests/sbx_rig.py) behind the real BaseSandbox helper scripts and the real Deep Agents graph (scripted model)."""
import asyncio, base64, json, os, sys, time
import pytest

sys.path.insert(0, os.path.dirname(__file__))
from sbx_rig import ctx, Rig, WHO, agent, say, tool_msgs, DiskDriver  # noqa: E402
import sbx_runtime as R  # noqa: E402
import sbx_backend as SB  # noqa: E402
from sbx_cfg import ComputerCfg  # noqa: E402
from pydantic import ValidationError  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="runs the python3/bash helper scripts of deepagents in a temp folder (WSL / Linux)")
run = asyncio.run

@pytest.fixture
def rig(tmp_path, monkeypatch): return Rig(tmp_path, monkeypatch)

A_USER, B_USER = ctx(uid=10), ctx(uid=11)

# ------------------------------------------------------------------ file tools: ls / read / write / edit / glob / grep / delete
def test_file_tools_roundtrip_and_error_codes(rig):
    sb = rig.sandbox()
    async def go():
        with rig.run("a7:t1", A_USER):
            assert (await sb.awrite("/workspace/notes/a.txt", "hello\nworld\n")).path == "/workspace/notes/a.txt"          # parent folders are made
            r = await sb.aread("/workspace/notes/a.txt"); assert r.file_data["content"] == "hello\nworld" and r.total_lines == 2
            assert (await sb.awrite("/workspace/notes/b.md", "# T\nworld again\n")).error is None
            assert {e["path"] for e in (await sb.als("/workspace/notes")).entries} == {"/workspace/notes/a.txt", "/workspace/notes/b.md"}
            assert (await sb.aedit("/workspace/notes/a.txt", "world", "there")).occurrences == 1
            assert (await sb.aread("/workspace/notes/a.txt")).file_data["content"] == "hello\nthere"
            assert [m["path"] for m in (await sb.aglob("*.md", "/workspace/notes")).matches] == ["/workspace/notes/b.md"]
            assert [(m["path"], m["line"]) for m in (await sb.agrep("world", "/workspace")).matches] == [("/workspace/notes/b.md", 2)]
            # error codes the framework understands
            assert "file_not_found" in (await sb.aread("/workspace/nope.txt")).error
            assert "not found" in (await sb.aedit("/workspace/nope.txt", "a", "b")).error
            assert (await sb.aedit("/workspace/notes/a.txt", "absent text", "x")).error
            assert "not_a_directory" in (await sb.als("/workspace/notes/a.txt")).error or (await sb.als("/workspace/notes/a.txt")).error
            assert (await sb.als("/workspace/missing")).error
            assert (await sb.adelete("/workspace/notes/a.txt")).path == "/workspace/notes/a.txt"
            assert "not found" in (await sb.adelete("/workspace/notes/a.txt")).error
    run(go())

def test_download_upload_codes_and_protected_delete(rig):
    sb = rig.sandbox()
    async def go():
        with rig.run("a7:t1", A_USER):
            assert (await sb.aupload_files([("/workspace/x.bin", b"\xff\xfe\x00\x01")]))[0].error is None
            d = await sb.adownload_files(["/workspace/x.bin", "/workspace/none.bin"])
            assert d[0].content == b"\xff\xfe\x00\x01" and d[1].content is None and d[1].error == "file_not_found"
            assert (await sb.aupload_files([("/workspace/../etc/x", b"a")]))[0].error == "invalid_path"
            assert (await sb.aupload_files([("/proc/1/mem", b"a")]))[0].error == "invalid_path"
            for p in ("/", "/workspace", "/opt/aos-user", "/workspace/.aos", "/workspace/out", "/workspace/"):
                assert "cannot be deleted" in (await sb.adelete(p)).error, p
            assert (await sb.aread("/workspace/x.bin")).file_data["encoding"] == "base64"          # binary files come back base64
    run(go())

# ------------------------------------------------------------------ execute: timeout, output cap
def test_execute_limits(rig, monkeypatch):
    sb, seen = rig.sandbox(), []
    orig = rig.comp.exec
    async def spy(c, i, cmd, **kw): seen.append(kw.get("timeout")); return await orig(c, i, cmd, **kw)
    monkeypatch.setattr(rig.comp, "exec", spy)
    async def go():
        with rig.run("a7:t1", A_USER):
            r = await sb.aexecute("echo ola"); assert (r.output, r.exit_code, r.truncated) == ("ola\n", 0, False)
            await sb.aexecute("echo x", timeout=999_999); await sb.aexecute("echo x", timeout=0); await sb.aexecute("echo x", timeout=7)
            assert seen == [SB.EXEC_DEFAULT_S, SB.EXEC_MAX_S, SB.EXEC_DEFAULT_S, 7]                      # no limit asked -> default; a huge one is cut down; never unlimited
            r = await sb.aexecute("sleep 30", timeout=1); assert r.exit_code == 137 and "timed out after 1 s" in r.output
            r = await sb.aexecute("big 150000"); assert r.truncated and len(r.output) < 101_000 and "/workspace/.aos/out/" in r.output and "full log" in r.output
            cid = r.output.split("/workspace/.aos/out/")[1].split(".log")[0]
            assert len((await sb.adownload_files([f"/workspace/.aos/out/{cid}.log"]))[0].content) == 150_000          # nothing lost: the whole output is in the log
            assert (await sb.aexecute("exit 3")).exit_code == 3
    run(go())

def test_helper_scripts_are_not_cut_at_100kb_but_model_output_is(rig):
    sb = rig.sandbox(); body = "".join(f"line {i:05d} " + "x" * 140 + "\n" for i in range(1500))   # ~ 230 KB of text -> the JSON answer of the read script is > 100 KB
    async def go():
        with rig.run("a7:t1", A_USER):
            await sb.awrite("/workspace/big.txt", body)
            r = await sb.aread("/workspace/big.txt", 0, 2000)
            assert r.error is None and r.total_lines == 1500 and "line 01499" in r.file_data["content"]
            assert (await sb.aexecute("big 150000")).truncated                                              # the model's own command is cut
    run(go())

# ------------------------------------------------------------------ lazy + which box
def test_no_box_until_a_file_or_command_is_used(rig):
    sb = rig.sandbox()
    async def go():
        with rig.run("a7:t1", A_USER): pass
        assert rig.rows() == []
        with rig.run("a7:t1", A_USER): await sb.aexecute("echo a")
        assert len(rig.rows()) == 1
    run(go())

def test_two_threads_two_boxes_same_thread_one_box(rig):
    sb = rig.sandbox()
    async def go():
        with rig.run("a7:t1", A_USER): await sb.awrite("/workspace/f.txt", "one")
        with rig.run("a7:t2", A_USER): await sb.awrite("/workspace/f.txt", "two")
        with rig.run("a7:t1", A_USER): assert (await sb.aread("/workspace/f.txt")).file_data["content"] == "one"
        with rig.run("a7:t2", A_USER): assert (await sb.aread("/workspace/f.txt")).file_data["content"] == "two"
        rows = rig.rows(); assert len(rows) == 2 and {r["thread_id"] for r in rows} == {"a7:t1", "a7:t2"} and {r["scope"] for r in rows} == {"thread"}
        assert {r["agent_id"] for r in rows} == {7} and {r["user_id"] for r in rows} == {10} and {r["delete_after_stop_s"] for r in rows} == {7 * 86400}
    run(go())

def test_agent_mode_is_one_box_for_all_threads_of_one_user(rig):
    sb = rig.sandbox(mode="agent")
    async def go():
        with rig.run("a7:t1", A_USER): await sb.awrite("/workspace/shared.txt", "mine")
        with rig.run("a7:t2", A_USER): assert (await sb.aread("/workspace/shared.txt")).file_data["content"] == "mine"
        (r,) = rig.rows(); assert r["scope"] == "agent" and r["thread_id"] is None and r["user_id"] == 10 and r["delete_after_stop_s"] is None   # a shared agent box is never auto-deleted
    run(go())

def test_parallel_first_calls_make_exactly_one_box(rig):
    sb = rig.sandbox()
    async def go():
        with rig.run("a7:t1", A_USER): rs = await asyncio.gather(*[sb.aexecute(f"echo {i}") for i in range(6)])
        assert [r.exit_code for r in rs] == [0] * 6 and len(rig.rows()) == 1
    run(go())

def test_idle_stopped_box_wakes_on_the_next_tool_call_files_intact(rig):
    sb = rig.sandbox()
    async def go():
        with rig.run("a7:t1", A_USER):
            await sb.awrite("/workspace/keep.txt", "kept")
            (r,) = rig.rows(); rig.store.rows[r["id"]]["last_active"] = time.time() - 700
            assert (await rig.comp.reap_tick())[0]["event"] == "idle_stop" and rig.store.rows[r["id"]]["status"] == "stopped"
            assert (await sb.aread("/workspace/keep.txt")).file_data["content"] == "kept" and rig.store.rows[r["id"]]["status"] == "running"
    run(go())

def test_stopped_by_the_person_stays_stopped_and_failed_box_is_never_replaced(rig):
    sb = rig.sandbox()
    async def go():
        with rig.run("a7:t1", A_USER):
            await sb.aexecute("echo a"); (r,) = rig.rows()
            await rig.comp.stop(A_USER, r["id"])
            out = await sb.aexecute("echo b"); assert out.exit_code == 1 and out.output.startswith("Error:") and "stopped" in out.output
            rig.store.rows[r["id"]].update(status="failed", last_error="the box is gone from the server")
            out = await sb.aexecute("echo c"); assert out.exit_code == 1 and "failed" in out.output and "gone from the server" in out.output
            assert len(rig.rows()) == 1                                                                      # no silent replacement
            assert "failed" in (await sb.aread("/workspace/x")).error
    run(go())

def test_setup_sh_runs_once_at_creation_and_is_kept_for_the_next_start(rig):
    sb = rig.sandbox(setup_sh="touch ran.flag\n")
    async def go():
        with rig.run("a7:t1", A_USER):
            await sb.aexecute("echo a"); await sb.aexecute("echo b"); (r,) = rig.rows()
            assert rig.drv.read(r["id"], "/opt/aos-user/setup.sh") == b"touch ran.flag\n"
            assert rig.drv.read(r["id"], "/workspace/ran.flag") == b""
            assert sum("setup.sh" in c for _, c in rig.drv.execs) == 1
    run(go())

# ------------------------------------------------------------------ refusals (negative tests)
def _nothing_ran(rig): assert rig.rows() == [] and rig.drv.execs == [] and rig.drv.boxes == {}

def test_other_workspace_is_refused_and_nothing_is_created(rig):
    sb = rig.sandbox(agent_id=7, ws=1)                       # the agent lives in workspace 1
    async def go():
        with rig.run("a7:t1", ctx(uid=50, ws=2, role="admin")):            # a person who signed in to workspace 2 runs it
            r = await sb.aexecute("echo leak"); assert r.exit_code == 1 and "another workspace" in r.output
            assert "another workspace" in (await sb.awrite("/workspace/x", "y")).error
            assert "another workspace" in (await sb.adownload_files(["/workspace/x"]))[0].error
            assert "another workspace" in (await sb.aupload_files([("/workspace/x", b"y")]))[0].error
        _nothing_ran(rig)
    run(go())

def test_box_of_workspace_one_is_invisible_to_workspace_two_even_for_an_admin(rig):
    sb1, sb2 = rig.sandbox(agent_id=7, ws=1), rig.sandbox(agent_id=7, ws=2)             # same agent number in two workspaces (separate agents in reality)
    async def go():
        with rig.run("a7:t1", ctx(uid=10, ws=1)): await sb1.awrite("/workspace/secret.txt", "ws1 secret")
        with rig.run("a7:t1", ctx(uid=10, ws=2, role="admin")):
            assert "file_not_found" in (await sb2.aread("/workspace/secret.txt")).error
            assert (await sb2.aexecute("cat /workspace/secret.txt")).exit_code == 1
        assert {r["workspace_id"] for r in rig.rows()} == {1, 2}
        (b1,) = rig.rows(workspace_id=1)
        with pytest.raises(R.NotFound): await rig.comp.get_file(ctx(uid=10, ws=2, role="admin"), b1["id"], "/workspace/secret.txt")      # and the box layer itself agrees
    run(go())

@pytest.mark.parametrize("who,why", [(ctx(demo=True), "demo"), (ctx(role="viewer"), "viewers"), (ctx(scope="read"), "read-only API key"), (ctx(role="nobody"), "role")])
def test_demo_viewer_read_only_key_and_unknown_role_are_refused(rig, who, why):
    sb = rig.sandbox()
    async def go():
        with rig.run("a7:t1", who):
            r = await sb.aexecute("echo hi"); assert r.exit_code == 1 and r.output.startswith("Error:") and why in r.output
            assert (await sb.awrite("/workspace/a", "b")).error
        _nothing_ran(rig)
    run(go())

def test_unauthenticated_run_without_an_owner_is_refused(rig):
    sb = rig.sandbox()
    async def go():
        with rig.run("a7:t1", None):                           # no signed-in user (expired / missing session) and the agent has no owner
            r = await sb.aexecute("echo hi"); assert r.exit_code == 1 and "no owner" in r.output
        _nothing_ran(rig)
    run(go())

def test_outside_a_run_nothing_happens(rig):
    sb = rig.sandbox()
    async def go():
        r = await sb.aexecute("echo hi"); assert r.exit_code == 1 and "inside an agent run" in r.output
        _nothing_ran(rig)
    run(go())

def test_workspace_switch_and_box_quota_return_messages_not_crashes(rig):
    sb = rig.sandbox()
    async def go():
        rig.store.quotas[1] = {"workspace_id": 1, "enabled": False, "max_running": 3, "max_total": 10, "internet": "off"}
        with rig.run("a7:t1", A_USER):
            r = await sb.aexecute("echo a"); assert r.exit_code == 1 and R.OFF_MSG in r.output
        _nothing_ran(rig)
        rig.store.quotas[1] = {"workspace_id": 1, "enabled": True, "max_running": 3, "max_total": 1, "internet": "off"}
        with rig.run("a7:t1", A_USER): assert (await sb.aexecute("echo a")).exit_code == 0
        with rig.run("a7:t2", A_USER):
            r = await sb.aexecute("echo b"); assert r.exit_code == 1 and "limit" in r.output
        assert len(rig.rows()) == 1
    run(go())

def test_no_gvisor_means_no_computer_and_no_runc_fallback(tmp_path, monkeypatch):
    rig = Rig(tmp_path, monkeypatch)
    def no_gvisor(kind="computer", info=None): raise R.CoreUnavailable("the virtual computer needs gVisor (runsc), which is not available on this server", retry_after=30)
    monkeypatch.setattr(R, "select_core", no_gvisor)
    c = R.Computers(rig.store, None, disk_probe=lambda: 10); SB.set_computers(c); SB.set_images(rig.imgs)         # driver=None -> core chosen per call, like production
    sb = rig.sandbox()
    sb._comp = c
    async def go():
        with rig.run("a7:t1", A_USER):
            r = await sb.aexecute("echo hi"); assert r.exit_code == 1 and "gVisor" in r.output and "30 s" in r.output
        _nothing_ran(rig)
    run(go())

def test_delete_protected_paths_and_dotdot_never_reach_the_box(rig):
    sb = rig.sandbox()
    async def go():
        with rig.run("a7:t1", A_USER):
            await sb.aexecute("echo a"); n = len(rig.drv.execs)
            assert (await sb.adelete("/workspace/../")).error and (await sb.adelete("relative")).error
            assert len(rig.drv.execs) == n
    run(go())

# ------------------------------------------------------------------ people never share a box
def test_same_thread_id_other_user_gets_another_box_even_an_admin(rig):
    sb = rig.sandbox()
    async def go():
        with rig.run("a7:t1", A_USER): await sb.awrite("/workspace/private.txt", "A only")
        for who in (B_USER, ctx(uid=99, role="admin")):
            with rig.run("a7:t1", who):                              # same (guessable) thread id
                assert "file_not_found" in (await sb.aread("/workspace/private.txt")).error
                assert (await sb.aexecute("cat /workspace/private.txt")).exit_code == 1
        assert len(rig.rows()) == 3 and {r["user_id"] for r in rig.rows()} == {10, 11, 99}
    run(go())

def test_unattended_run_uses_the_owners_box_and_external_sender_never_does(rig, monkeypatch):
    sb, created = rig.sandbox(mode="agent", internet="basic"), []
    orig = rig.comp.create
    async def spy(c, **kw):
        created.append(dict(kw)); return await orig(c, **{**kw, "internet": "off", "allow_list": []})      # the spy keeps the unit test away from the egress proxy
    monkeypatch.setattr(rig.comp, "create", spy)
    async def go():
        rig.owner[7] = A_USER
        with rig.run("a7:t1", A_USER): await sb.awrite("/workspace/o.txt", "owner file")
        with rig.run("a7:cron1", None): assert (await sb.aread("/workspace/o.txt")).file_data["content"] == "owner file"          # cron / webhook = the owner's box
        assert len(rig.rows()) == 1 and created[0]["internet"] == "basic"
        with rig.run("a7:chat9", A_USER, auth="channel"):                                     # an outside chatter writing to the owner's channel
            assert "file_not_found" in (await sb.aread("/workspace/o.txt")).error
            assert (await sb.aexecute("cat /workspace/o.txt")).exit_code == 1
        with rig.run("a7:chat9", A_USER, aos_trust="external"): assert "file_not_found" in (await sb.aread("/workspace/o.txt")).error      # same rule by flag (11.72)
        ext = [r for r in rig.rows() if str(r["thread_id"]).startswith("ext:")]
        assert len(ext) == 1 and ext[0]["thread_id"] == "ext:a7:chat9" and ext[0]["scope"] == "thread" and ext[0]["user_id"] is None and ext[0]["owner_id"] == 10
        assert [c["internet"] for c in created[1:]] == ["off"] and created[1]["allow_list"] == []         # no Internet for outside senders, whatever the agent allows
        with rig.run("a7:cron1", None): assert (await sb.aread("/workspace/o.txt")).file_data["content"] == "owner file"        # the owner's own box is unchanged
        rig.owner[7] = None
        with rig.run("a7:cron2", None): assert "no owner" in (await sb.aexecute("echo x")).output
    run(go())

def test_unattended_owner_who_is_a_viewer_or_demo_is_refused(rig):
    sb = rig.sandbox()
    async def go():
        for owner in (ctx(uid=10, role="viewer"), ctx(uid=10, demo=True)):
            rig.owner[7] = owner
            with rig.run("a7:cron1", None): assert (await sb.aexecute("echo x")).exit_code == 1
        _nothing_ran(rig)
        rig.owner[7] = ctx(uid=10, ws=2)                                       # owner of another workspace
        with rig.run("a7:cron1", None): assert "another workspace" in (await sb.aexecute("echo x")).output
        _nothing_ran(rig)
    run(go())

# ------------------------------------------------------------------ 11.47a: a new box starts from the agent's baked image / the workspace's `default` snapshot
def _bake_row(rig, agent_id=7, ws=1, tag="agent-a7--setup:abc"):
    run(rig.imgs.store.insert({"id": "b1", "name": f"agent-a{agent_id}--setup", "kind": "bake", "workspace_id": ws, "owner_id": 10, "agent_id": agent_id, "status": "ready", "image_tag": tag, "base_image": R.IMAGE}))
    rig.drv.__dict__.setdefault("images", {})[tag] = {"files": {"/opt/tools/baked.txt": b"tools"}}

def test_new_box_starts_from_the_agents_baked_setup_image_and_skips_setup_sh(rig):
    _bake_row(rig); sb = rig.sandbox(setup_sh="touch ran.flag\n")
    async def go():
        with rig.run("a7:t1", A_USER):
            assert (await sb.aread("/opt/tools/baked.txt")).file_data["content"] == "tools"
            (r,) = rig.rows(); assert r["image"] == "agent-a7--setup:abc"
            assert not any("setup.sh" in c for _, c in rig.drv.execs)             # already in the image
        with rig.run("a7:t2", A_USER): assert (await sb.aread("/opt/tools/baked.txt")).file_data["content"] == "tools"
    run(go())

def test_baked_image_of_another_workspace_or_agent_is_not_used(rig):
    _bake_row(rig, ws=2); _bake_row(rig, agent_id=8, ws=1, tag="agent-a8--setup:zzz")
    sb = rig.sandbox()
    async def go():
        with rig.run("a7:t1", A_USER):
            assert "file_not_found" in (await sb.aread("/opt/tools/baked.txt")).error
            (r,) = rig.rows(); assert r["image"] == R.IMAGE
    run(go())

def test_default_snapshot_seeds_new_boxes_but_never_an_external_senders_box(rig, tmp_path):
    sb = rig.sandbox(mode="thread")
    async def go():
        with rig.run("a7:t0", A_USER):
            await sb.awrite("/workspace/template.txt", "from the template"); (src,) = rig.rows()
        await rig.imgs.capture(A_USER, src["id"], "default")
        with rig.run("a7:t1", A_USER): assert (await sb.aread("/workspace/template.txt")).file_data["content"] == "from the template"
        with rig.run("a7:chat1", A_USER, auth="channel"): assert "file_not_found" in (await sb.aread("/workspace/template.txt")).error
        with rig.run("a7:t1", ctx(uid=11)): assert (await sb.aread("/workspace/template.txt")).file_data["content"] == "from the template"      # the workspace's template, not A's later files
    run(go())

def test_purge_hook_of_snapshots_is_registered_by_importing_the_backend():
    import sbx_images
    assert sbx_images.purge_hook in R.PURGE_HOOKS

# ------------------------------------------------------------------ attachments -> /workspace/uploads, /workspace/out -> chat files
def test_thread_attachments_are_copied_once_and_a_failure_never_blocks(rig):
    sb, loads = rig.sandbox(), []
    async def load(): loads.append(1); return b"a,b\n1,2\n"
    async def go():
        rig.uploads = [("f1", "data.csv", load)]
        with rig.run("a7:t1", A_USER):
            assert (await sb.aexecute("echo a")).exit_code == 0 and (await sb.aexecute("echo b")).exit_code == 0
            assert (await sb.aread("/workspace/uploads/data.csv")).file_data["content"].startswith("a,b") and len(loads) == 1
            async def boom(): raise OSError("disk gone")
            rig.uploads = [("f2", "x.csv", boom)]
            assert (await sb.aexecute("echo c")).exit_code == 0                      # best effort
        with rig.run("a7:t9", B_USER): assert "file_not_found" in (await sb.aread("/workspace/uploads/data.csv")).error     # another user / thread: not copied there
    run(go())

def _fake_chat_gd5b(monkeypatch, stored):
    import types
    m = types.ModuleType("chat_gd5b"); m.MAX_OUT_FILES, m.MAX_OUT_FILE_BYTES = 8, 10_000_000
    async def store(items):
        stored.extend(items); return [{"id": f"id{len(stored)}{i}", "name": it["path"], "size": len(base64.b64decode(it["b64"])), "mime_type": "text/plain"} for i, it in enumerate(items)]
    m._store_out_files = store
    m.files_line = lambda files: "[aos-files] " + json.dumps({"files": files})
    monkeypatch.setitem(sys.modules, "chat_gd5b", m)

def test_files_written_to_out_show_in_the_chat_files_panel_once(rig, monkeypatch):
    stored = []; _fake_chat_gd5b(monkeypatch, stored)
    w = SB.wire(7, 1, ComputerCfg(mode="thread"), computers_=rig.comp)
    ag = agent(w, {"make": [("execute", {"command": "write /workspace/out/report.txt first"})], "again": [("execute", {"command": "echo nothing new"})],
                   "change": [("execute", {"command": "write /workspace/out/report.txt second"})], "plain": [("write_file", {"file_path": "/workspace/out/b.txt", "content": "bb"})]})
    async def go():
        with rig.as_(A_USER):
            m = tool_msgs(await say(ag, "a7:t1", "make"))[-1].content
            assert "[aos-files]" in m and '"report.txt"' in m and [s["path"] for s in stored] == ["report.txt"]
            assert "[aos-files]" not in tool_msgs(await say(ag, "a7:t1", "again"))[-1].content and len(stored) == 1               # nothing new -> nothing published
            assert "[aos-files]" in tool_msgs(await say(ag, "a7:t1", "change"))[-1].content and len(stored) == 2                    # changed file is published again
            assert "b.txt" in tool_msgs(await say(ag, "a7:t1", "plain"))[-1].content and len(stored) == 3                           # also after write_file
    run(go())

# ------------------------------------------------------------------ real Deep Agents graph on the box
def test_agent_writes_and_runs_in_turn_1_and_reads_in_turn_2_two_threads_two_boxes(rig):
    w = SB.wire(7, 1, ComputerCfg(mode="thread"), computers_=rig.comp)
    script = {"turn1": [("write_file", {"file_path": "/workspace/hello.py", "content": "print('hi from the box')\n"}), ("execute", {"command": "python3 hello.py"})],
              "turn2": [("read_file", {"file_path": "/workspace/hello.py"})], "nothing": []}
    ag = agent(w, script)
    async def go():
        with rig.as_(A_USER):
            assert rig.rows() == []
            await say(ag, "a7:t1", "nothing"); assert rig.rows() == []                                              # a turn without file/shell use creates no box
            r1 = await say(ag, "a7:t1", "turn1"); t1 = tool_msgs(r1)
            assert "Updated file /workspace/hello.py" in t1[0].content and "hi from the box" in t1[1].content and "exit code 0" in t1[1].content.lower()
            assert "hi from the box" in tool_msgs(await say(ag, "a7:t1", "turn2"))[-1].content                       # turn 2 (same thread) sees it
            assert "not found" in tool_msgs(await say(ag, "a7:t2", "turn2"))[-1].content.lower() or "file_not_found" in tool_msgs(await say(ag, "a7:t2", "turn2"))[-1].content      # other thread: other box
            assert len(rig.rows()) == 2
    run(go())

def test_framework_tools_on_the_box(rig):
    w = SB.wire(7, 1, ComputerCfg(mode="thread"), computers_=rig.comp)
    ag = agent(w, {"ls": [("ls", {"path": "/workspace"})], "glob": [("glob", {"pattern": "*.txt", "path": "/workspace"})], "grep": [("grep", {"pattern": "needle", "path": "/workspace"})],
                   "edit": [("edit_file", {"file_path": "/workspace/n.txt", "old_string": "needle", "new_string": "thread"})], "w": [("write_file", {"file_path": "/workspace/n.txt", "content": "a needle here\n"})],
                   "big": [("execute", {"command": "big 150000"})], "slow": [("execute", {"command": "sleep 30", "timeout": 1})], "toolong": [("execute", {"command": "echo x", "timeout": 100000})]})
    async def go():
        with rig.as_(A_USER):
            await say(ag, "a7:t1", "w")
            assert "n.txt" in tool_msgs(await say(ag, "a7:t1", "ls"))[-1].content
            assert "/workspace/n.txt" in tool_msgs(await say(ag, "a7:t1", "glob"))[-1].content
            assert "n.txt" in tool_msgs(await say(ag, "a7:t1", "grep"))[-1].content
            assert "Successfully replaced" in tool_msgs(await say(ag, "a7:t1", "edit"))[-1].content
            m = tool_msgs(await say(ag, "a7:t1", "big"))[-1]; assert "truncated" in m.content.lower() and m.artifact["truncated"] is True and len(m.content) < 150_000
            m = tool_msgs(await say(ag, "a7:t1", "slow"))[-1]; assert "timed out" in m.content and m.artifact["exit_code"] == 137
            assert "exceeds maximum" in tool_msgs(await say(ag, "a7:t1", "toolong"))[-1].content          # the framework's own ceiling (3600 s) still applies first
    run(go())

def test_write_to_pack_skills_memory_denied_and_state_files_still_readable(rig):
    from deepagents.backends.utils import create_file_data
    w = SB.wire(7, 1, ComputerCfg(mode="thread"), computers_=rig.comp)
    ag = agent(w, {"pack": [("write_file", {"file_path": "/pack/x.md", "content": "hack"})], "skill": [("write_file", {"file_path": "/skills/main/x/SKILL.md", "content": "hack"})],
                   "mem": [("edit_file", {"file_path": "/memory/a/AGENTS.md", "old_string": "rule", "new_string": "evil"})],
                   "readskill": [("read_file", {"file_path": "/skills/main/a/SKILL.md"})], "lsskill": [("ls", {"path": "/skills/main"})], "rootls": [("ls", {"path": "/"})],
                   "readmem": [("read_file", {"file_path": "/memory/a/AGENTS.md"})]})
    files = {"/skills/main/a/SKILL.md": create_file_data("---\nname: a\ndescription: d\n---\nuse me\n"), "/memory/a/AGENTS.md": create_file_data("rule one\n")}
    async def go():
        with rig.as_(A_USER):
            for k in ("pack", "skill", "mem"): assert "permission denied" in tool_msgs(await say(ag, "a7:t1", k, files))[-1].content.lower(), k
            assert "use me" in tool_msgs(await say(ag, "a7:t1", "readskill", files))[-1].content                  # today's virtual folders keep working next to the box
            assert "/skills/main/a" in tool_msgs(await say(ag, "a7:t1", "lsskill", files))[-1].content
            assert "rule one" in tool_msgs(await say(ag, "a7:t1", "readmem", files))[-1].content
            root = tool_msgs(await say(ag, "a7:t1", "rootls", files))[-1].content; assert "/skills/" in root and "/workspace" in root
        assert not any("hack" in str(v) for b in rig.drv.boxes.values() for v in b["files"].values())            # nothing landed in the box's file system either
    run(go())

def test_fs_rules_on_routed_paths_are_enforced(rig):
    w = SB.wire(7, 1, ComputerCfg(mode="thread", fs_rules=[{"operations": ["read"], "paths": ["/memory/secret/**"], "mode": "deny"}]), computers_=rig.comp)
    from deepagents.backends.utils import create_file_data
    ag = agent(w, {"r": [("read_file", {"file_path": "/memory/secret/k.md"})], "ok": [("read_file", {"file_path": "/memory/open/k.md"})]})
    files = {"/memory/secret/k.md": create_file_data("top\n"), "/memory/open/k.md": create_file_data("fine\n")}
    async def go():
        with rig.as_(A_USER):
            assert "permission denied" in tool_msgs(await say(ag, "a7:t1", "r", files))[-1].content.lower()
            assert "fine" in tool_msgs(await say(ag, "a7:t1", "ok", files))[-1].content
    run(go())

def test_approve_shell_pauses_before_execute_and_runs_only_after_approval(rig):
    from langgraph.types import Command
    w = SB.wire(7, 1, ComputerCfg(mode="thread", approve_shell=True), computers_=rig.comp)
    assert set(w["interrupt"]) == {"execute"} and SB.wire(7, 1, ComputerCfg(mode="thread"), computers_=rig.comp)["interrupt"] is None
    ag = agent(w, {"go": [("execute", {"command": "write /workspace/ran.txt yes"})]})
    async def go():
        with rig.as_(A_USER):
            cfg = {"configurable": {"thread_id": "a7:t1"}}
            r = await ag.ainvoke({"messages": [{"role": "user", "content": "go"}]}, config=cfg)
            assert r.get("__interrupt__") and rig.drv.execs == [] and rig.rows() == []                           # paused: no command, no box
            r = await ag.ainvoke(Command(resume={"decisions": [{"type": "reject", "message": "no"}]}), config=cfg)
            assert rig.drv.execs == []
            cfg = {"configurable": {"thread_id": "a7:t2"}}
            await ag.ainvoke({"messages": [{"role": "user", "content": "go"}]}, config=cfg)
            r = await ag.ainvoke(Command(resume={"decisions": [{"type": "approve"}]}), config=cfg)
            assert any("ran.txt" in c for _, c in rig.drv.execs) and rig.drv.read(rig.rows()[0]["id"], "/workspace/ran.txt") == b"yes"
    run(go())

# ------------------------------------------------------------------ for_config + sync entry points
def test_for_config_returns_the_callers_box_only(rig):
    sb = rig.sandbox(agent_id=7)
    async def go():
        cfg = {"configurable": {"thread_id": "a7:t1"}}
        with rig.as_(A_USER):
            b = await SB.for_config(cfg); assert b.id and b.ctx["uid"] == 10
            await b.put("/workspace/up.txt", b"attached"); assert await b.get("/workspace/up.txt") == b"attached"
            assert (await b.exec("echo via box"))["output"] == "via box\n"
            assert (await SB.for_config({"configurable": {"thread_id": "a99:t1"}})) is None                  # an agent without a computer
            assert (await SB.for_config({"configurable": {"thread_id": "plain"}})) is None
        with rig.as_(B_USER):
            b2 = await SB.for_config(cfg); assert b2.id != b.id
            with pytest.raises(R.SbxError): await b2.get("/workspace/up.txt")
            with pytest.raises(R.NotFound): await rig.comp.get_file(B_USER, b.id, "/workspace/up.txt")
        with rig.as_(ctx(uid=12, role="viewer")):
            with pytest.raises(R.Forbidden): await SB.for_config(cfg)
    run(go())

def test_sync_entry_points_work_off_the_loop_and_refuse_inside_it(rig):
    sb = rig.sandbox()
    with rig.run("a7:t1", A_USER):
        assert sb.execute("echo sync").output == "sync\n"
        assert sb.write("/workspace/s.txt", "sync file\n").error is None and sb.read("/workspace/s.txt").file_data["content"] == "sync file"
        assert sb.upload_files([("/workspace/u", b"1")])[0].error is None and sb.download_files(["/workspace/u"])[0].content == b"1"
        assert sb.delete("/workspace/u").path == "/workspace/u"
        async def inside():
            with pytest.raises(RuntimeError): sb.execute("echo no")
        run(inside())

# ------------------------------------------------------------------ settings object
def test_computer_cfg_defaults_and_validation():
    c = ComputerCfg(); assert (c.mode, c.internet, c.size, c.approve_shell, c.on) == ("off", "off", "small", False, False)
    assert ComputerCfg(mode="agent").on and ComputerCfg(mode="thread", idle_ttl_seconds=300, size="medium").size == "medium"
    for bad in ({"mode": "all"}, {"internet": "full"}, {"size": "huge"}, {"idle_ttl_seconds": 30}, {"idle_ttl_seconds": 31 * 86400}, {"delete_after_stop_seconds": 90}, {"setup_sh": "x" * 20001},
                {"allow_list": ["pypi.org"]}, {"internet": "custom", "allow_list": ["has space.com"]}, {"internet": "custom", "allow_list": ["~(a+)+"]}, {"internet": "custom", "allow_list": ["*"]}):
        with pytest.raises(ValidationError): ComputerCfg(**bad)
    assert ComputerCfg(internet="custom", allow_list=["example.com", "10.1.2.3"]).allow_list == ["example.com", "10.1.2.3"]     # the private address is stored; the egress decision service refuses it at call time (11.45)
    # a rule on a path the shell can reach would be a false promise: refused (Deep Agents refuses it too)
    for p in ("/workspace/**", "/**", "/pack/../workspace", "/skills", "pack/x", "/memory/~/x"):
        with pytest.raises(ValidationError): ComputerCfg(mode="thread", fs_rules=[{"operations": ["write"], "paths": [p]}])
    assert ComputerCfg(mode="thread", fs_rules=[{"operations": ["write"], "paths": ["/pack/**", "/skills/x/**"]}]).fs_rules[0].mode == "deny"

def test_old_agent_configs_have_no_computer_and_mode_off_wires_nothing():
    import deepagent_api as A
    cfg = A.AgentCfg(name="x")
    assert cfg.computer is None or not cfg.computer.on
    assert A.AgentCfg(name="x", computer={"mode": "thread", "size": "medium"}).computer.on
    with pytest.raises(ValidationError): A.AgentCfg(name="x", computer={"mode": "thread", "fs_rules": [{"operations": ["write"], "paths": ["/etc/**"]}]})

# ------------------------------------------------------------------ F-3: history shows only the agent's own commands; span attributes reach the tool span
def test_helper_scripts_are_not_in_command_history_but_execute_is(rig):
    sb = rig.sandbox()
    async def go():
        with rig.run("a7:t1", A_USER):
            await sb.awrite("/workspace/n.txt", "hello\n"); await sb.aread("/workspace/n.txt"); await sb.aedit("/workspace/n.txt", "hello", "bye")
            await sb.als("/workspace"); await sb.aglob("*.txt", "/workspace"); await sb.agrep("bye", "/workspace"); await sb.adelete("/workspace/n.txt")
            assert rig.store.cmds == []
            await sb.aexecute("echo real")
            assert [c["command"] for c in rig.store.cmds] == ["echo real"]
    run(go())

def test_exec_record_false_skips_history_and_span_attrs(rig):
    sb = rig.sandbox()
    async def go():
        with rig.run("a7:t1", A_USER):
            await sb.aexecute("echo hi")
            t, row = await sb._resolve()
            R.SPAN_ATTRS.set(None)
            res = await rig.comp.exec(t.ctx, row["id"], "echo quiet", record=False)
            assert res["output"] == "quiet\n" and len(rig.store.cmds) == 1 and R.SPAN_ATTRS.get() is None
    run(go())

def test_tool_span_of_execute_gets_sandbox_attrs(rig):
    import gateway as G
    sb = rig.sandbox()
    class Req:
        def __init__(self, name): self.tool_call = {"name": name, "args": {}}
    mw = G.GatewayMiddleware.__new__(G.GatewayMiddleware)
    async def go():
        with rig.run("a7:t1", A_USER):
            async def handler(req):
                # the real tool runs in a copied context (like LangGraph tasks): the set must still reach the span
                await asyncio.create_task(sb.aexecute("echo span"))
                return "ok"
            spans = []; tk = G.SPANS.set(spans); tt = G.T0.set(time.perf_counter())
            try:
                await mw.awrap_tool_call(Req("execute"), handler); await mw.awrap_tool_call(Req("other"), handler)
            finally: G.SPANS.reset(tk); G.T0.reset(tt)
            assert spans[0]["name"] == "execute" and spans[0]["sandbox.kind"] == "computer" and spans[0]["sandbox.exit_code"] == 0 and spans[0]["sandbox.id"]
            assert "sandbox.id" not in spans[1]
    run(go())
