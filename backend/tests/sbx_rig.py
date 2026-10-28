"""Shared test rig of GĐ11.41 (tests/test_gd11_sandbox_backend.py, tests/test_gd11_shared_agent.py): NO Docker, NO real LLM.
DiskDriver = sbx_runtime.FakeDriver whose box files live in a real temp folder and which can also run the python3/bash helper scripts that deepagents'
BaseSandbox sends (ls / read / glob / grep / edit): absolute box paths (/workspace, /opt, /tmp, /large_tool_results) are mapped into that folder.
Scripted = a chat model that calls the tools listed for the human message, one per model call, then answers (the fake LLM of overlay/fake_llm.py does one call per turn only)."""
import asyncio, base64, contextlib, contextvars, json, os, re, shlex, sys, uuid
from collections.abc import MutableMapping

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")
import sbx_runtime as R  # noqa: E402
import sbx_backend as SB  # noqa: E402
from sbx_cfg import ComputerCfg  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langchain_core.runnables.config import var_child_runnable_config  # noqa: E402

WHO: contextvars.ContextVar = contextvars.ContextVar("rig_who", default=(None, None))

def ctx(uid=10, ws=1, role="editor", demo=False, scope="full"): return {"ws": ws, "uid": uid, "role": role, "demo": demo, "scope": scope}

class DiskFiles(MutableMapping):
    def __init__(self, root): self.root = root
    def _p(self, k): return os.path.join(self.root, k.lstrip("/"))
    def __contains__(self, k): return os.path.isfile(self._p(k))
    def __getitem__(self, k):
        try:
            with open(self._p(k), "rb") as f: return f.read()
        except OSError: raise KeyError(k) from None
    def __setitem__(self, k, v):
        os.makedirs(os.path.dirname(self._p(k)), exist_ok=True)
        with open(self._p(k), "wb") as f: f.write(v if isinstance(v, bytes) else str(v).encode())
    def __delitem__(self, k): os.remove(self._p(k))
    def __iter__(self):
        for d, _, fs in os.walk(self.root):
            for n in fs: yield "/" + os.path.relpath(os.path.join(d, n), self.root).replace(os.sep, "/")
    def __len__(self): return sum(1 for _ in self)

FAKE_VERBS = {"echo", "pwd", "cat", "write", "big", "sleep", "exit", "true", "false", "env"}
_B64 = re.compile(r"base64\.b64decode\('([A-Za-z0-9+/=]*)'\)")
_TOP = r"(?:workspace|opt|tmp|large_tool_results)"

class DiskDriver(R.FakeDriver):
    def __init__(self, root): super().__init__(); self.root = str(root); self.execs = []     # execs: every command that reached the box

    async def create(self, spec):
        await super().create(spec); self.boxes[spec.id]["files"] = DiskFiles(os.path.join(self.root, spec.id))
        os.makedirs(os.path.join(self.root, spec.id, "workspace", ".aos", "out"), exist_ok=True)      # what aos-boot makes in the real box

    def _map(self, cmd, root):
        def b64(m):
            try: p = base64.b64decode(m.group(1)).decode()
            except Exception: return m.group(0)  # noqa: BLE001
            if re.match(rf"^/{_TOP}(/|$)", p): p = root + p
            return "base64.b64decode('%s')" % base64.b64encode(p.encode()).decode()
        cmd = _B64.sub(b64, cmd)
        def edit(m):       # inline edit: heredoc with base64 JSON {path, old, new}
            try: d = json.loads(base64.b64decode(m.group(2)).decode())
            except Exception: return m.group(0)  # noqa: BLE001
            if isinstance(d.get("path"), str) and re.match(rf"^/{_TOP}(/|$)", d["path"]): d["path"] = root + d["path"]
            return m.group(1) + base64.b64encode(json.dumps(d).encode()).decode() + "\n"
        cmd = re.sub(r"(<<'__DEEPAGENTS_EDIT_EOF__'\n)([A-Za-z0-9+/=]+)\n", edit, cmd)
        if not cmd.startswith("python3 "): cmd = re.sub(rf"(?<![\w./-])/({_TOP})(?=/|\b|$)", lambda m: root + "/" + m.group(1), cmd)
        return cmd

    async def exec(self, id, cmd, *, cwd="/workspace", env=None, timeout=60, command_id=None, stdin=None):
        b = self._b(id); self.execs.append((id, cmd))
        if (cmd.split(None, 1) or [""])[0] in FAKE_VERBS: return await super().exec(id, cmd, cwd=cwd, env=env, timeout=timeout, command_id=command_id, stdin=stdin)
        if b["state"] != "running": raise R.BoxFailed("box is not running")
        cid, root = command_id or uuid.uuid4().hex[:16], os.path.join(self.root, id)
        c2 = self._map(cmd, root)
        if c2.startswith("python3 "): c2 = shlex.quote(sys.executable) + c2[len("python3"):]
        real_cwd = os.path.join(root, R.box_path(cwd).lstrip("/")); os.makedirs(real_cwd, exist_ok=True)
        p = await asyncio.create_subprocess_exec("bash", "-c", c2, cwd=real_cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env={**os.environ, **R.check_env(env)})
        timed = False
        try: out, _ = await asyncio.wait_for(p.communicate(), timeout)
        except asyncio.TimeoutError: p.kill(); out, timed = b"", True; await p.wait()
        out = out.replace(root.encode(), b"")
        b["files"][f"/workspace/.aos/out/{cid}.log"] = out
        return R.ExecResult(cid, 137 if timed else p.returncode, out[:R.OUT_CAP].decode(errors="replace"), len(out) > R.OUT_CAP, timed, 1)

    def read(self, id, path):
        f = self._b(id)["files"]; return f[path] if path in f else None

class Scripted(BaseChatModel):
    script: dict = {}
    @property
    def _llm_type(self): return "scripted"
    def bind_tools(self, tools, **kw): return self
    def _generate(self, messages, stop=None, run_manager=None, **kw):
        i = max((k for k, m in enumerate(messages) if isinstance(m, HumanMessage)), default=-1)
        text = messages[i].content if i >= 0 and isinstance(messages[i].content, str) else ""
        done = [m for m in messages[i + 1:] if isinstance(m, ToolMessage)]
        calls = self.script.get(text, [])
        if len(done) < len(calls):
            name, args = calls[len(done)]
            msg = AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "c" + uuid.uuid4().hex[:8], "type": "tool_call"}])
        else: msg = AIMessage(content="OK " + (str(done[-1].content) if done else "")[:500])
        return ChatResult(generations=[ChatGeneration(message=msg)])
    async def _agenerate(self, messages, stop=None, run_manager=None, **kw): return self._generate(messages, stop, run_manager, **kw)

class Rig:
    """One box layer (MemStore + DiskDriver) wired into sbx_backend; `as_` switches the signed-in user, `run` sets the run config like the graph does."""
    def __init__(self, tmp, monkeypatch):
        self.store, self.drv = R.MemStore(), DiskDriver(tmp)
        self.comp = R.Computers(self.store, self.drv, disk_probe=lambda: 10)
        import sbx_images as SI
        self.imgs = SI.Images(computers=self.comp, store=SI.MemSnapStore(), ops=SI.FakeOps(self.drv), root=tmp)
        SB.set_computers(self.comp); SB.set_images(self.imgs)
        monkeypatch.setattr(SB, "_caller", lambda: WHO.get())
        self.owner = {}                                           # agent id -> ctx of its owner (unattended runs)
        async def owner_ctx(agent_id, ws): return self.owner.get(agent_id)
        monkeypatch.setattr(SB, "_owner_ctx", owner_ctx)
        self.uploads = []
        async def uploads(thread): return list(self.uploads)
        monkeypatch.setattr(SB, "_thread_uploads", uploads)
        import capacity
        monkeypatch.setattr(capacity, "mem_available_mb", lambda: 64_000)
        monkeypatch.setenv("AOS_COMPUTER_MAX_RUNNING", "20"); monkeypatch.setenv("AOS_BOX_MAX_RUNNING", "20"); monkeypatch.setenv("AOS_BOX_MAX_TOTAL", "20")
        self.mp = monkeypatch

    def sandbox(self, agent_id=7, ws=1, **cfg): return SB.AgentOSSandbox(agent_id, ws, ComputerCfg(**{"mode": "thread", **cfg}), self.comp)

    @contextlib.contextmanager
    def as_(self, c, auth=None):
        tok = WHO.set((c, auth))
        try: yield
        finally: WHO.reset(tok)

    @contextlib.contextmanager
    def run(self, thread, c, auth=None, **configurable):
        t1, t2 = WHO.set((c, auth)), var_child_runnable_config.set({"configurable": {"thread_id": thread, **configurable}})
        try: yield
        finally: var_child_runnable_config.reset(t2); WHO.reset(t1)

    def rows(self, **where): return [r for r in self.store.rows.values() if r["status"] != "deleted" and all(r.get(k) == v for k, v in where.items())]

def agent(w, script, saver=None):
    """A real Deep Agents graph around the wired backend: the framework's own file tools + execute run against the box."""
    from deepagents import create_deep_agent
    from langgraph.checkpoint.memory import InMemorySaver
    return create_deep_agent(model=Scripted(script=script), tools=[], backend=w["backend"], permissions=w["permissions"], interrupt_on=w["interrupt"],
                             middleware=w["middleware"], checkpointer=saver or InMemorySaver())

async def say(ag, thread, text, files=None):
    inp = {"messages": [{"role": "user", "content": text}]}
    if files: inp["files"] = files
    return await ag.ainvoke(inp, config={"configurable": {"thread_id": thread}})

def tool_msgs(res): return [m for m in res["messages"] if isinstance(m, ToolMessage)]
