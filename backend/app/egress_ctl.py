"""GĐ11.45 egress control: no Internet for a virtual computer unless a workspace admin allowed it, and then only through our own proxy (sandbox/egress/proxy.py,
container `aos-egress`) which asks THIS module for every connection. Design: GD11_PLAN 11.45 + "Out of scope" table (allow-list only, TLS passthrough, no MITM,
no CA, no key ever injected).
 * Box with internet=off: `--network none` (unchanged). basic|custom: the box lives alone with the proxy on its OWN internal Docker network `aos-sbx-egress-<id>`
   (a /30 carved from AOS_EGRESS_POOL, default 172.31.251.0/24 = 64 boxes; `--internal`, bridge WITHOUT an IPv4 address on the host). Measured on this Docker (29.8.1):
   one shared internal network let a box reach (a) the host's gateway address (= every host service listening on 0.0.0.0) and (b) every other box of every workspace.
   Per-box network + no host address closes both; the proxy (container `aos-egress`, also on the normal bridge `aos-egress-out`) is the only peer.
   Box identity = its fixed IP (proxy IP = .1, box = .2) + the per-box credential `box-<id>:<secret>` kept in `sandboxes.host_state.egress`; both are checked on EVERY connection.
 * Decision (`decide`): credential + source IP -> box running -> workspace not demo -> effective mode = min(box mode, workspace ceiling `workspace_sandbox_quotas.internet`)
   -> host spelling (no decimal/octal/hex IPs) -> allow-list (egress_policy) -> ONLY THEN DNS, through netguard.check_all(.., "sandbox_egress"), which refuses every internal
   address even if AOS_EGRESS_ALLOW lists it -> the PINNED IPs go back to the proxy, which connects to nothing else. A name that is not allow-listed is never even resolved
   (no DNS exfiltration through us). Every verdict is logged to sandbox_egress_log (host, port, verdict, reason; never paths / headers / tokens), 30-day prune.
 * Fail closed: controller or proxy down = no route out, the box keeps running offline.
 * Residual risk (documented): an allow-listed wildcard / regex lets a box talk to ANY name under it (including DNS lookups of attacker-chosen sub-domains of that domain);
   the SNI / Host check covers only the first request of a tunnel; a box can send data to anything its allow-list lets it reach, so `custom` is an admin decision."""
import asyncio, contextvars, functools, hmac, ipaddress, json, os, re, secrets, stat, time
import egress_policy as P
import netguard

PROXY_PORT = 3128
def proxy_name(): return os.environ.get("AOS_EGRESS_PROXY_NAME", "aos-egress")      # env: only the real-box tests run a second proxy next to the production one
def out_net(): return os.environ.get("AOS_EGRESS_OUT_NET", "aos-egress-out")
PROXY_IMAGE = os.environ.get("AOS_EGRESS_IMAGE", "aos-egress:20261026")
DOCKER_DNS = "192.0.2.1"                      # TEST-NET-1: nothing answers there and an internal network has no route to it -> no DNS out
REC_TTL = 3.0
LOG_KEEP_DAYS = 30

def pool(): return ipaddress.ip_network(os.environ.get("AOS_EGRESS_POOL", "172.31.251.0/24"))
def slot_count(): return max(0, pool().num_addresses // 4)
def slot_net(k):
    base = pool().network_address + 4 * k
    return ipaddress.ip_network(f"{base}/30"), str(base + 1), str(base + 2)      # subnet, proxy ip, box ip
def net_name(box_id): return f"aos-sbx-egress-{box_id}"
def default_ceiling():
    v = (os.environ.get("AOS_EGRESS_DEFAULT_CEILING") or "off").strip().lower()
    return v if v in P.MODES else "off"
def bracket(h): return f"[{h}]" if ":" in h else h

def box_net_args(eg):
    """`docker create` network flags of a box with Internet. Everything is re-validated here: the dict comes from host_state, but a bad value must never reach a command line."""
    box_ok = isinstance(eg, dict) and isinstance(eg.get("net"), str) and re.fullmatch(r"aos-sbx-egress-[0-9a-f]{16}", eg["net"])
    try: ip = ipaddress.ip_address(eg["ip"]) if box_ok else None
    except (ValueError, KeyError, TypeError): ip = None
    if not box_ok or ip is None or ip.version != 4 or ip not in pool(): raise EgressError("bad egress network description")
    return ["--network", eg["net"], "--ip", str(ip), "--dns", DOCKER_DNS]

def ctl_dir(first=None):
    """Directory of the control socket: mode 0700, owned by us, never a symlink. /run/aos-egress (card) when we may create it, else /run/user/<uid>/aos-egress, else /tmp/aos-egress-<uid>."""
    uid = os.getuid()
    cands = [first, os.environ.get("AOS_EGRESS_CTL_DIR"), "/run/aos-egress", f"/run/user/{uid}/aos-egress", f"/tmp/aos-egress-{uid}"]
    for d in filter(None, cands):
        try:
            if not os.path.lexists(d): os.makedirs(d, mode=0o700)
            st = os.lstat(d)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != uid: continue
            os.chmod(d, 0o700); return d
        except OSError: continue
    raise OSError("no usable directory for the egress control socket")

async def _sh(*cmd, timeout=60):
    p = await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try: out, err = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        try: p.kill()
        except ProcessLookupError: pass
        return 124, "", "timeout"
    return p.returncode, out.decode(errors="replace").strip(), err.decode(errors="replace").strip()

class EgressError(Exception): pass

# ---------------------------------------------------------------- Docker side (networks + the proxy container)
class DockerNet:
    """Everything that touches Docker for egress. Only objects named aos-egress / aos-egress-out / aos-sbx-egress-* are created or removed; other networks are never touched."""
    def __init__(self): self._lock = asyncio.Lock()

    async def ensure_proxy(self, directory):
        async with self._lock:
            if (await _sh("docker", "network", "inspect", out_net()))[0]:
                rc, _, err = await _sh("docker", "network", "create", "--label", "aos.egress=1", out_net())
                if rc and "already exists" not in err: raise EgressError("cannot create network: " + err[:200])
            if (await _sh("docker", "image", "inspect", PROXY_IMAGE))[0]:
                ctx = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sandbox", "egress")
                rc, _, err = await _sh("docker", "build", "-q", "-t", PROXY_IMAGE, ctx, timeout=300)
                if rc: raise EgressError("cannot build the proxy image: " + err[:200])
            rc, out, _ = await _sh("docker", "inspect", "-f", "{{.State.Running}}|{{index .Config.Labels \"aos.egress.ctl\"}}|{{.Config.Image}}", proxy_name())
            if rc == 0:
                running, d, img = (out.split("|") + ["", "", ""])[:3]
                if d == directory and img == PROXY_IMAGE:
                    if running == "true": return False
                    rc, _, err = await _sh("docker", "start", proxy_name())
                    if rc == 0: return False
                await _sh("docker", "rm", "-f", proxy_name())
            rc, _, err = await _sh("docker", "run", "-d", "--name", proxy_name(), "--restart", "unless-stopped", "--network", out_net(), "--user", f"{os.getuid()}:{os.getgid()}",
                                   "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "512", "--memory", "256m", "--cpus", "1",
                                   "--label", "aos.egress=1", "--label", f"aos.egress.ctl={directory}", "-v", f"{directory}:/run/aos-egress:ro", PROXY_IMAGE)
            if rc: raise EgressError("cannot start the proxy container: " + err[:300])
            return True

    async def create_net(self, box_id):
        """-> (slot, network name, proxy ip, box ip). Docker refuses an overlapping subnet, so two creators can never get the same slot."""
        name, last = net_name(box_id), ""
        for k in range(slot_count()):
            sub, pip, bip = slot_net(k)
            rc, _, err = await _sh("docker", "network", "create", "--driver", "bridge", "--internal", "--subnet", str(sub), "-o", "com.docker.network.bridge.inhibit_ipv4=true",
                                   "--label", "aos.egress=1", "--label", f"aos.box.id={box_id}", "--label", f"aos.egress.slot={k}", name)
            if rc == 0: return k, name, pip, bip
            last = err
            if "already exists" in err and name in err: raise EgressError("network of this box already exists")
        raise EgressError("no free egress network slot (" + last[-120:] + ")")

    async def attach(self, name, proxy_ip):
        rc, _, err = await _sh("docker", "network", "connect", "--ip", proxy_ip, name, proxy_name())
        if rc and "already exists" not in err and "already attached" not in err: raise EgressError("cannot attach the proxy: " + err[:200])

    async def attach_named(self, name):
        """The proxy's leg on an existing egress network (its IP is the .1 of the slot written on the network label)."""
        rc, out, _ = await _sh("docker", "network", "inspect", "-f", "{{index .Labels \"aos.egress.slot\"}}", name)
        if rc or not out.isdigit() or int(out) >= slot_count(): raise EgressError("not an egress network")
        await self.attach(name, slot_net(int(out))[1])

    async def remove(self, name):
        if not re.fullmatch(r"aos-sbx-egress-[0-9a-f]{16}", name): return
        if (await _sh("docker", "network", "inspect", name))[0]: return        # nothing to remove (box without Internet)
        await _sh("docker", "network", "disconnect", "-f", name, proxy_name())
        await _sh("docker", "network", "rm", name)

    async def reattach_all(self):
        """After the proxy container was re-created: connect it to every egress network that still exists."""
        rc, out, _ = await _sh("docker", "network", "ls", "--filter", "label=aos.egress.slot", "--format", "{{.Name}}|{{.Label \"aos.egress.slot\"}}")
        for ln in out.splitlines():
            n, _, k = ln.partition("|")
            if k.isdigit() and int(k) < slot_count():
                try: await self.attach(n, slot_net(int(k))[1])
                except EgressError: pass

class FakeNet:
    """No Docker (unit tests): remembers networks, hands out slots in order."""
    def __init__(self): self.nets, self.attached, self.proxy_starts, self.fail = {}, set(), 0, False
    async def ensure_proxy(self, directory): self.proxy_starts += 1; return False
    async def create_net(self, box_id):
        if self.fail: raise EgressError("fake network failure")
        used = {v[0] for v in self.nets.values()}
        for k in range(slot_count()):
            if k not in used:
                _, pip, bip = slot_net(k); self.nets[net_name(box_id)] = (k, pip, bip); return k, net_name(box_id), pip, bip
        raise EgressError("no free egress network slot")
    async def attach(self, name, proxy_ip): self.attached.add(name)
    async def attach_named(self, name): self.attached.add(name)
    async def remove(self, name): self.nets.pop(name, None); self.attached.discard(name)
    async def reattach_all(self): pass

# ---------------------------------------------------------------- log sinks
class ListSink:
    def __init__(self): self.rows = []
    def add(self, row): self.rows.append(row)
    async def flush(self): pass
    async def prune(self): pass

class PgSink:
    """Batched inserts (a `pip install` opens hundreds of tunnels): flushed every 2 s from the control server's loop."""
    def __init__(self, q=None): self.q, self.buf = q, []
    def _q(self):
        if self.q is None:
            import deepagent_api as A; self.q = A.q
        return self.q
    def add(self, row):
        if len(self.buf) < 5000: self.buf.append(row)
    async def flush(self):
        rows, self.buf = self.buf, []
        for r in rows:
            try: await self._q()("INSERT INTO sandbox_egress_log(workspace_id,sandbox_id,host,port,verdict,reason,mode) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                                 (r["workspace_id"], r["sandbox_id"], r["host"][:253], r["port"], r["verdict"], r["reason"][:120], r["mode"]))
            except Exception as e: print("[egress] log insert failed:", type(e).__name__, flush=True); break   # noqa: BLE001
    async def prune(self): await self._q()("DELETE FROM sandbox_egress_log WHERE ts < now() - make_interval(days => %s)", (LOG_KEEP_DAYS,))

# ---------------------------------------------------------------- the service
@functools.lru_cache(maxsize=256)
def _parsed(items): return tuple(P.parse_list(list(items)))

class EgressCtl:
    """store: anything with async get(id) -> row dict | None and async quota(ws) -> row | None (sbx_runtime.PgStore / MemStore). Lookups run as the system (no user)."""
    def __init__(self, store, net=None, sink=None, directory=None, demo_ws=None, serve=True):
        self.store, self.net, self.sink, self._dir, self._recs, self._srv, self._task = store, net or DockerNet(), sink or ListSink(), None, {}, None, None
        self._demo, self._first, self.serve = demo_ws, directory, serve           # serve=False: unit tests that never open the socket

    def directory(self):
        if self._dir is None: self._dir = ctl_dir(self._first)
        return self._dir

    # -- provisioning, called by sbx_runtime.Computers
    async def provision(self, box_id, mode, allow_list=()):
        """-> the dict stored in sandboxes.host_state['egress'] (contains the secret: never return it from an API) for mode basic|custom."""
        if mode not in ("basic", "custom"): raise EgressError("mode must be basic or custom")
        lst = P.validate_list(allow_list) if mode == "custom" else []
        await self.ensure_running()
        slot, name, pip, bip = await self.net.create_net(box_id)
        try: await self.net.attach(name, pip)
        except BaseException:
            await self.net.remove(name); raise
        return {"mode": mode, "allow_list": lst, "slot": slot, "net": name, "ip": bip, "proxy_ip": pip, "secret": secrets.token_urlsafe(24)}

    async def attach(self, eg):
        """Before a box starts: make sure the controller, the proxy and the proxy's leg on this box's network exist. Never raises: a box whose proxy is down simply stays offline."""
        try:
            await self.ensure_running()
            await self.net.attach(eg["net"], eg["proxy_ip"])
            return True
        except Exception as e:  # noqa: BLE001
            print("[egress] proxy not available, box stays offline:", type(e).__name__, str(e)[:120], flush=True); return False

    async def release(self, eg, box_id=None):
        if eg and eg.get("net"):
            try: await self.net.remove(eg["net"])
            except Exception as e: print("[egress] network removal failed:", type(e).__name__, flush=True)  # noqa: BLE001
        if box_id: self._recs.pop(box_id, None)

    async def attach_net(self, name):
        """Driver hook before `docker start` of a box that has an egress network. Never raises (see attach)."""
        try:
            await self.ensure_running()
            await self.net.attach_named(name)
        except Exception as e: print("[egress] proxy not available, box starts offline:", type(e).__name__, str(e)[:120], flush=True)   # noqa: BLE001

    @staticmethod
    def proxy_env(eg, box_id):
        u = f"http://box-{box_id}:{eg['secret']}@{eg['proxy_ip']}:{PROXY_PORT}"
        return {"HTTP_PROXY": u, "HTTPS_PROXY": u, "http_proxy": u, "https_proxy": u, "NO_PROXY": "", "no_proxy": ""}

    async def ensure_running(self):
        if self.serve: await self.start_server()
        if await self.net.ensure_proxy(self.directory()): await self.net.reattach_all()

    # -- the decision
    async def _rec(self, box):
        now = time.monotonic(); c = self._recs.get(box)
        if c and now - c[0] < REC_TTL: return c[1], c[2]
        row = await self.store.get(box)
        quota = await self.store.quota(int(row["workspace_id"])) if row else None
        self._recs[box] = (now, row, quota)
        if len(self._recs) > 2000: self._recs.clear()
        return row, quota

    def forget(self, box): self._recs.pop(box, None)

    def _deny(self, reason, ws=None, box=None, host="", port=0, mode=""):
        if ws is not None:
            self.sink.add({"workspace_id": int(ws), "sandbox_id": box, "host": str(host)[:253], "port": port if isinstance(port, int) else 0, "verdict": "deny", "reason": reason, "mode": mode})
        return {"ok": False, "reason": reason}

    def _is_demo(self, ws):
        if self._demo is not None: return int(ws) in self._demo
        try:
            import gateway as gw; return int(ws) in gw.DEMO_WS
        except Exception: return False   # noqa: BLE001

    async def decide(self, box, secret, src, host, port, plain=False):
        """-> {"ok": True, "ips": [...], "host": normalized, "ip_literal": bool} or {"ok": False, "reason": code}. Reason codes never contain URLs, paths or tokens."""
        if not (isinstance(box, str) and len(box) == 16 and isinstance(secret, str) and isinstance(host, str) and isinstance(port, int) and not isinstance(port, bool)
                and 1 <= port <= 65535 and isinstance(src, str)):
            return self._deny("bad_request")
        row, quota = await self._rec(box)
        eg = ((row or {}).get("host_state") or {}).get("egress") if row else None
        if not row or not isinstance(eg, dict) or not hmac.compare_digest(str(eg.get("secret", "")).encode(), secret.encode()):
            return self._deny("bad_credentials")
        ws = int(row["workspace_id"])
        if src != eg.get("ip"): return self._deny("bad_source", ws, box, host, port)
        if row.get("status") != "running": return self._deny("box_not_running", ws, box, host, port)
        if self._is_demo(ws): return self._deny("demo_workspace", ws, box, host, port)
        ceiling = (quota or {}).get("internet") if quota else default_ceiling()
        mode = P.min_mode(eg.get("mode", "off"), ceiling if ceiling in P.MODES else "off")
        try: h, is_ip = P.normalize_host(host)
        except P.BadHost: return self._deny("bad_host", ws, box, host, port, mode)
        if mode == "off": return self._deny("internet_off", ws, box, h, port, mode)
        if is_ip and port == 443: return self._deny("ip_https_dropped", ws, box, h, port, mode)
        try: custom = list(_parsed(tuple(eg.get("allow_list") or ())))
        except P.BadEntry: custom = []
        ok, rule = P.allowed(mode, custom, h, port, is_ip)
        if not ok: return self._deny("not_listed", ws, box, h, port, mode)
        try: ips, _ = await asyncio.to_thread(netguard.check_all, f"https://{bracket(h)}:{port}/", "sandbox_egress")   # DNS happens only now; internal addresses never pass
        except netguard.NetBlocked: return self._deny("blocked_address", ws, box, h, port, mode)
        pl = pool()
        if any(ipaddress.ip_address(i) in pl for i in ips): return self._deny("blocked_address", ws, box, h, port, mode)
        self.sink.add({"workspace_id": ws, "sandbox_id": box, "host": h, "port": port, "verdict": "allow", "reason": rule[:120], "mode": mode})
        return {"ok": True, "ips": ips, "host": h, "ip_literal": is_ip, "rule": rule}

    # -- Unix socket server
    async def _conn(self, r, w):
        try:
            line = await asyncio.wait_for(r.readline(), 5)
            if not line or len(line) > 4096: out = {"ok": False, "reason": "bad_request"}
            else:
                try: q = json.loads(line)
                except ValueError: q = None
                if not isinstance(q, dict) or q.get("op") != "decide": out = {"ok": False, "reason": "bad_request"}
                else: out = await asyncio.wait_for(self.decide(q.get("box"), q.get("secret"), q.get("src"), q.get("host"), q.get("port"), bool(q.get("plain"))), 20)
        except Exception as e:  # noqa: BLE001
            print("[egress] decision error:", type(e).__name__, flush=True); out = {"ok": False, "reason": "controller_error"}
        try: w.write(json.dumps(out).encode() + b"\n"); await w.drain()
        except OSError: pass
        finally:
            try: w.close()
            except OSError: pass

    def sock_path(self): return os.path.join(self.directory(), "ctl.sock")

    async def start_server(self):
        """Start the socket server in an EMPTY context: tasks inherit contextvars, and the server must never run with the identity (admin.USER) of whichever request started it."""
        if self._srv is None: await asyncio.create_task(self._start(), context=contextvars.Context())

    async def _start(self):
        if self._srv is not None: return
        path = self.sock_path()
        if os.path.exists(path):
            try:
                _, w = await asyncio.wait_for(asyncio.open_unix_connection(path), 2); w.close()
                return                                                     # another process of this server already listens
            except (OSError, asyncio.TimeoutError): os.unlink(path)
        self._srv = await asyncio.start_unix_server(self._conn, path=path)
        os.chmod(path, 0o600)
        self._task = asyncio.create_task(self._housekeeping())

    async def _housekeeping(self):
        last_prune = 0.0
        while True:
            try:
                await self.sink.flush()
                if time.monotonic() - last_prune > 3600: last_prune = time.monotonic(); await self.sink.prune()
            except asyncio.CancelledError: raise
            except Exception as e: print("[egress] housekeeping:", type(e).__name__, flush=True)  # noqa: BLE001
            await asyncio.sleep(2)

    async def stop_server(self):
        if self._task: self._task.cancel(); self._task = None
        if self._srv:
            self._srv.close(); self._srv = None
            try: os.unlink(self.sock_path())
            except OSError: pass
        await self.sink.flush()

_DEFAULT = None
def default():
    """The production controller (Postgres store, Docker network ops, Postgres log). Created on first use."""
    global _DEFAULT
    if _DEFAULT is None:
        import sbx_runtime as R
        _DEFAULT = EgressCtl(R.PgStore(), DockerNet(), PgSink())
    return _DEFAULT

async def start_default():
    """Called at API start (beside sandbox2.init) when gVisor is available; the proxy container itself is only created when the first box asks for internet."""
    try: await default().start_server()
    except Exception as e: print("[egress] controller not started:", type(e).__name__, str(e)[:120], flush=True)   # noqa: BLE001
