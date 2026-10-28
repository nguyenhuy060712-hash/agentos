"""GĐ10 security cards (10.1-10.3, 10.28). In-process where possible: no server, no real network (DNS is monkeypatched; the 'public address' tests redirect the
TCP connect to a local receiver while recording which IP netguard picked). Sections: 10.2 netguard, then the later cards append their own."""
import asyncio, datetime as dt, json, os, socket, ssl, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote
import httpcore, httpx, pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")   # deepagent_api (imported by obs / sbx_parity / zalo) wants a key at import; nothing is called
run = asyncio.run
PUB = "93.184.216.34"   # a public address (never contacted: the connect is redirected to the local receiver)

# --- 10.2 ---
@pytest.fixture(autouse=True)
def clean(monkeypatch):
    import netguard
    for k in ("AOS_EGRESS_ALLOW", "AOS_EGRESS_DENY_HOSTS", "AOS_SELF_IPS"): monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(netguard, "_SELF", (float("inf"), set()))   # no auto-detected interface addresses unless a test asks
    return netguard

def dns(monkeypatch, table):
    """socket.getaddrinfo -> table[host] (list of ips, or callable(call_no) -> list); unknown host = NXDOMAIN. Returns the call log."""
    calls = []
    def fake(host, port, *a, **k):
        calls.append(host); v = table.get(host)
        if v is None: raise socket.gaierror(-2, "nx")
        v = v(len(calls)) if callable(v) else v
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0, 0, 0) if ":" in ip else (ip, port or 0)) for ip in v]
    monkeypatch.setattr(socket, "getaddrinfo", fake); return calls

class Recv:
    """Local receiver on 127.0.0.1: records requests; /r?to=<quoted URL> answers 302; /loop redirects to itself; optional TLS."""
    def __init__(self, cert=None):
        self.got = []; outer = self
        class H(BaseHTTPRequestHandler):
            def _do(s):
                n = int(s.headers.get("content-length") or 0); body = s.rfile.read(n) if n else b""
                outer.got.append({"m": s.command, "path": s.path, "host": s.headers.get("host"), "h": {k.lower(): v for k, v in s.headers.items()}, "body": body})
                if s.path.startswith("/r?to=") or s.path.startswith("/loop"):
                    s.send_response(302); s.send_header("Location", unquote(s.path[6:]) if s.path.startswith("/r?") else "/loop"); s.send_header("content-length", "0"); s.end_headers(); return
                s.send_response(200); s.send_header("content-type", "text/plain"); s.send_header("content-length", "5"); s.end_headers(); s.wfile.write(b"hello")
            do_GET = do_POST = _do
            def log_message(s, *a): pass
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H); self.port = self.srv.server_address[1]
        if cert:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(*cert); self.srv.socket = ctx.wrap_socket(self.srv.socket, server_side=True)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
    def url(self, path="/", scheme="http", host="127.0.0.1"): return f"{scheme}://{host}:{self.port}{path}"
    def redirect(self, to): return self.url("/r?to=" + quote(to, safe=""))
    def close(self): self.srv.shutdown(); self.srv.server_close()

@pytest.fixture
def recv():
    r = Recv(); yield r; r.close()

@pytest.fixture
def hijack(monkeypatch):
    """Whatever address netguard dials is recorded, and the socket really goes to 127.0.0.1 (so a 'public' IP can be exercised offline)."""
    seen = []
    orig = httpcore.AnyIOBackend.connect_tcp
    async def conn(self, host, port, **kw): seen.append(host); return await orig(self, "127.0.0.1", port, **kw)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", conn); return seen

def allow(monkeypatch, v="127.0.0.0/8,::1"): monkeypatch.setenv("AOS_EGRESS_ALLOW", v)
def blocked(url, purpose="webhook", **kw):
    import netguard
    with pytest.raises(netguard.NetBlocked) as e: netguard.check(url, purpose, **kw)
    return e.value.reason

BAD = ["http://127.0.0.1/", "https://127.0.0.1:8020/x", "https://10.0.0.5/", "https://172.16.5.4/", "https://192.168.1.1/", "https://169.254.169.254/latest/meta-data/",
       "http://169.254.169.254/", "https://100.64.0.1/", "https://0.0.0.0/", "https://224.0.0.1/", "https://255.255.255.255/", "https://[::1]/", "https://[::]/",
       "https://[::ffff:127.0.0.1]/", "https://[::ffff:169.254.169.254]/", "https://[::ffff:10.0.0.5]/", "https://[fe80::1]/", "https://[fc00::1]/", "https://[fd12:3456::1]/",
       "https://[2002:7f00:1::]/", "https://[64:ff9b::7f00:1]/", "https://[::7f00:1]/", "https://2130706433/", "https://127.1/", "https://0x7f.0.0.1/", "https://0177.0.0.1/",
       "https://[2001:0:4136:e378:8000:63bf:3fff:fdd2]/", "https://[::ffff:0:0]/", "ftp://example.com/", "file:///etc/passwd", "javascript:alert(1)", "https:///x", "", "not a url"]

@pytest.mark.parametrize("purpose", ["webhook", "fetch", "public"])
def test_literal_and_encoded_addresses_are_blocked(purpose):
    for u in BAD: assert blocked(u, purpose), u

def test_names_resolving_to_blocked_addresses_are_blocked(monkeypatch):
    dns(monkeypatch, {"a.test": ["127.0.0.1"], "b.test": ["10.0.0.5"], "c.test": ["169.254.169.254"], "d.test": ["::1"], "e.test": ["::ffff:127.0.0.1"],
                      "f.test": [PUB, "10.0.0.5"], "g.test": ["0.0.0.0"], "h.test": ["fe80::1%eth0"], "i.test": [PUB, "::ffff:192.168.0.1"]})
    for h in "abcdefghi": assert blocked(f"https://{h}.test/x"), h
    assert "does not resolve" in blocked("https://nx.test/")

def test_public_https_passes_and_http_depends_on_purpose(monkeypatch, clean):
    dns(monkeypatch, {"hook.test": [PUB], "v6.test": ["2606:2800:220:1:248:1893:25c8:1946"], "mapped.test": ["::ffff:93.184.216.34"]})
    assert clean.check("https://hook.test/h", "webhook") == (PUB, "hook.test")
    assert clean.check("https://v6.test/", "webhook")[1] == "v6.test" and clean.check("https://mapped.test/", "webhook")[1] == "mapped.test"
    assert "https required" in blocked("http://hook.test/h", "webhook") and "https required" in blocked("http://hook.test/h", "public")
    assert clean.check("http://hook.test/page", "fetch")[0] == PUB                          # public pages may be read over http
    assert "https required" in blocked("http://hook.test/page", "fetch", https_only=True)  # ... unless the caller says https only
    assert clean.check("https://hook.test/", "fetch", https_only=True)[0] == PUB

def test_allow_list_opens_internal_addresses_only_as_listed(monkeypatch, clean):
    dns(monkeypatch, {"corp.test": ["10.1.2.3"], "other.test": ["10.1.2.3"]})
    assert "AOS_EGRESS_ALLOW" in blocked("http://127.0.0.1:9/") and "AOS_EGRESS_ALLOW" in blocked("https://corp.test/")
    allow(monkeypatch, "127.0.0.0/8, ::1, corp.test")
    assert clean.check("http://127.0.0.1:9/")[0] == "127.0.0.1" and clean.check("http://[::1]:9/")[0] == "::1"
    assert clean.check("http://[::ffff:127.0.0.1]:9/")[0].startswith("::ffff:")            # unwrapped, then 127/8 matches
    assert clean.check("http://corp.test/")[0] == "10.1.2.3"                              # allow-listed host NAME -> plain http ok
    assert "AOS_EGRESS_ALLOW" in blocked("https://other.test/") and "AOS_EGRESS_ALLOW" in blocked("https://10.0.0.5/") and "AOS_EGRESS_ALLOW" in blocked("https://192.168.1.1/")
    allow(monkeypatch, "::1")
    assert "AOS_EGRESS_ALLOW" in blocked("http://[::ffff:127.0.0.1]/") and "AOS_EGRESS_ALLOW" in blocked("http://127.0.0.1/")   # only what is listed
    allow(monkeypatch, "127.0.0.0/8")
    assert "AOS_EGRESS_ALLOW" in blocked("http://[2002:7f00:1::]/") and "AOS_EGRESS_ALLOW" in blocked("http://[::7f00:1]/")      # tunnelled forms need their own entry

def test_metadata_link_local_multicast_unspecified_cannot_be_allow_listed(monkeypatch):
    allow(monkeypatch, "0.0.0.0/0, ::/0, 169.254.0.0/16, fe80::/10, 224.0.0.0/4, 0.0.0.0/8")
    dns(monkeypatch, {"meta.test": ["169.254.169.254"]})
    for u in ("http://169.254.169.254/", "https://169.254.169.254/", "http://0.0.0.0/", "http://[fe80::1]/", "http://224.0.0.1/", "http://[::ffff:169.254.169.254]/", "http://meta.test/"):
        assert "not reachable" in blocked(u), u

def test_public_purpose_ignores_the_allow_list(monkeypatch):
    allow(monkeypatch, "127.0.0.0/8, 10.0.0.0/8")
    assert blocked("https://127.0.0.1/a/b", "public") and blocked("https://10.0.0.5/a/b", "public")

def test_deny_list_wins_over_allow_list(monkeypatch, clean):
    dns(monkeypatch, {"ha.local.test": ["10.0.0.9"], "www.ha.local.test": ["10.0.0.9"], "pub.test": [PUB], "ok.test": ["10.0.0.9"]})
    allow(monkeypatch, "127.0.0.0/8, 10.0.0.0/8")
    monkeypatch.setenv("AOS_EGRESS_DENY_HOSTS", "ha.local.test, 127.0.0.5, 10.0.0.77/32")
    assert clean.check("http://127.0.0.1:9/")[0] == "127.0.0.1" and clean.check("http://ok.test/")[0] == "10.0.0.9"
    for u in ("http://127.0.0.5/", "http://10.0.0.77/", "http://ha.local.test/", "http://www.ha.local.test/", "http://[::ffff:127.0.0.5]/"): assert "deny list" in blocked(u), u
    monkeypatch.setenv("AOS_SELF_IPS", PUB)
    assert "this server itself" in blocked("https://pub.test/") and "this server itself" in blocked("https://" + PUB + "/")

def test_own_interface_addresses_are_detected(monkeypatch, clean):
    dns(monkeypatch, {socket.gethostname(): ["198.51.100.7", "127.0.1.1"], "me.test": ["198.51.100.7"]})
    monkeypatch.setattr(clean, "_SELF", (0.0, set()))
    ips = clean._self_ips()
    assert __import__("ipaddress").ip_address("198.51.100.7") in ips and not any(i.is_loopback for i in ips)   # loopback stays an allow-list matter
    allow(monkeypatch, "198.51.100.0/24")
    assert "this server itself" in blocked("https://me.test/")

# ---------------------------------------------------------------- the httpx client: pinned connect, redirects
def post(url, purpose="webhook", **kw):
    import netguard
    async def go():
        async with netguard.client(purpose, timeout=5, **kw) as h: return await h.post(url, json={"a": 1})
    return run(go())
def get(url, purpose="fetch", **kw):
    import netguard
    async def go():
        async with netguard.client(purpose, timeout=5, **kw) as h: return await h.get(url)
    return run(go())

def test_client_refuses_without_allow_list_and_sends_nothing(recv):
    import netguard
    with pytest.raises(netguard.NetBlocked): post(recv.url("/h"))
    with pytest.raises(httpx.HTTPError): get(recv.url("/h"))        # NetBlocked is an httpx error: old `except httpx.HTTPError` callers fail closed
    assert recv.got == []

def test_client_sends_to_allow_listed_internal_address(monkeypatch, recv):
    allow(monkeypatch)
    r = post(recv.url("/h")); assert r.status_code == 200 and json.loads(recv.got[0]["body"]) == {"a": 1}
    assert get(recv.url("/x")).text == "hello"

def test_dns_name_keeps_host_header_and_connects_to_checked_ip_once(monkeypatch, recv, hijack):
    calls = dns(monkeypatch, {"recv.corp.test": ["127.0.0.1"]}); allow(monkeypatch, "127.0.0.0/8")
    r = post(recv.url("/h", host="recv.corp.test")); assert r.status_code == 200
    assert recv.got[0]["host"] == f"recv.corp.test:{recv.port}" and hijack == ["127.0.0.1"] and calls == ["recv.corp.test"]   # one lookup, name kept in Host

def test_dns_rebinding_second_answer_is_never_used(monkeypatch, recv, hijack):
    import netguard
    # attacker DNS: the 1st answer passes the check, every later answer is the metadata address. We resolve once and dial the 1st answer.
    calls = dns(monkeypatch, {"evil.test": lambda n: ["127.0.0.1"] if n == 1 else ["169.254.169.254"]}); allow(monkeypatch)
    assert post(recv.url("/h", host="evil.test")).status_code == 200 and hijack == ["127.0.0.1"] and len(calls) == 1
    dns(monkeypatch, {"evil2.test": lambda n: ["169.254.169.254"] if n == 1 else ["127.0.0.1"]})      # the reverse order is refused outright
    with pytest.raises(netguard.NetBlocked): post(recv.url("/h", host="evil2.test"))
    assert hijack == ["127.0.0.1"]

def test_backend_refuses_an_unchecked_host():
    import netguard
    async def go(): await netguard._Backend().connect_tcp("127.0.0.1", 9)
    with pytest.raises(httpcore.ConnectError): run(go())     # no pin set = nobody called check first

def test_public_address_is_reached_by_its_checked_ip(monkeypatch, recv, hijack):
    import netguard
    dns(monkeypatch, {"open.test": [PUB]})
    assert post(recv.url("/h", host="open.test"), "fetch").status_code == 200 and hijack == [PUB]
    assert get(recv.url("/h", host="open.test")).status_code == 200      # fetch: plain http to a public host
    with pytest.raises(netguard.NetBlocked): post(recv.url("/h", host="open.test"), "webhook")   # webhook: http to the Internet refused
    assert len(hijack) == 2

def test_webhook_does_not_follow_redirects(monkeypatch, recv):
    import netguard
    allow(monkeypatch)
    with pytest.raises(netguard.NetBlocked) as e: post(recv.redirect(recv.url("/h")))
    assert "redirect" in e.value.reason and len(recv.got) == 1 and recv.got[0]["path"].startswith("/r?")       # the target was never requested

def test_fetch_rechecks_every_redirect_hop(monkeypatch, recv):
    import netguard
    allow(monkeypatch, "127.0.0.1")
    assert get(recv.redirect(recv.url("/ok"))).text == "hello" and recv.got[-1]["path"] == "/ok"       # an allowed hop is followed
    n = len(recv.got)
    for bad in ("http://169.254.169.254/latest/", "http://10.0.0.5/", "http://[::1]:9/", "http://0.0.0.0/", "http://[::ffff:127.0.0.2]/", "file:///etc/passwd"):
        with pytest.raises((netguard.NetBlocked, httpx.HTTPError)): get(recv.redirect(bad))
    assert len(recv.got) == n + 6                                      # each probe reached the receiver once; none of the targets was contacted
    with pytest.raises(httpx.TooManyRedirects): get(recv.url("/loop"))
    assert sum(1 for g in recv.got if g["path"] == "/loop") == 6      # initial + 5 follow-ups, then stop

def test_redirect_to_a_name_that_resolves_to_a_private_address(monkeypatch, recv):
    import netguard
    dns(monkeypatch, {"sneaky.test": ["10.0.0.5"]}); allow(monkeypatch)
    with pytest.raises(netguard.NetBlocked) as e: get(recv.redirect("http://sneaky.test/x"))
    assert "AOS_EGRESS_ALLOW" in e.value.reason

def test_environment_proxies_are_ignored(monkeypatch, recv):
    allow(monkeypatch); monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1"); monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1"); monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    assert post(recv.url("/h")).status_code == 200

def test_block_log_has_host_and_reason_but_no_path_or_token(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="aos.netguard"):
        blocked("https://169.254.169.254/latest/api/token?secret=SEKRET-TOKEN")
    t = caplog.text; assert "169.254.169.254" in t and "not reachable" in t and "SEKRET-TOKEN" not in t and "/latest" not in t

# ---------------------------------------------------------------- TLS: the name stays in SNI + certificate check, the IP is what we dial
@pytest.fixture
def tls(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    k = ec.generate_private_key(ec.SECP256R1()); n = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "hook.example.test")])
    now = dt.datetime.now(dt.timezone.utc)
    c = (x509.CertificateBuilder().subject_name(n).issuer_name(n).public_key(k.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
         .not_valid_after(now + dt.timedelta(days=2)).add_extension(x509.SubjectAlternativeName([x509.DNSName("hook.example.test")]), False)
         .add_extension(x509.BasicConstraints(ca=True, path_length=None), True).sign(k, hashes.SHA256()))
    cf, kf = tmp_path / "c.pem", tmp_path / "k.pem"
    cf.write_bytes(c.public_bytes(serialization.Encoding.PEM)); kf.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    monkeypatch.setenv("SSL_CERT_FILE", str(cf))
    r = Recv((str(cf), str(kf))); yield r; r.close()

def test_https_to_public_ip_verifies_the_certificate_against_the_name(monkeypatch, tls, hijack):
    dns(monkeypatch, {"hook.example.test": [PUB], "other.example.test": [PUB]})
    assert post(tls.url("/h", "https", "hook.example.test")).status_code == 200 and hijack == [PUB] and tls.got[0]["host"] == f"hook.example.test:{tls.port}"
    with pytest.raises(httpx.ConnectError): post(tls.url("/h", "https", "other.example.test"))        # right IP, wrong certificate name -> refused (never verified by IP)

# ---------------------------------------------------------------- call sites
class FakeQ:
    def __init__(self): self.rows = []
    async def __call__(self, sql, args=(), **kw): self.rows.append(args)

@pytest.fixture
def obs_(monkeypatch):
    import deepagent_api, obs   # noqa: F401  (deepagent_api first: obs <-> deepagent_api import cycle)
    q = FakeQ(); monkeypatch.setattr(obs.A, "q", q); return obs, q

def test_deliver_sends_real_webhooks_and_records_blocks(monkeypatch, obs_, recv):
    obs, q = obs_
    dns(monkeypatch, {"hooks.example.test": [PUB], "internal.test": ["10.9.9.9"]})
    # an Internet webhook needs https (plain http to a public host is refused, nothing is dialled)
    assert "https required" in run(obs.deliver("webhook", recv.url("/h", host="hooks.example.test"), "s", "b")) and q.rows[-1][4] == "failed" and q.rows[-1][5].startswith("blocked:")
    # not allow-listed internal / metadata / redirect: failed with the reason, nothing delivered, the URL is not in the detail
    for u in (recv.url("/h"), "http://169.254.169.254/latest/", "https://10.0.0.5/x?token=ABC", recv.redirect(recv.url("/h")), "http://[::1]:1/", "https://internal.test/"):
        r = run(obs.deliver("webhook", u, "s", "b")); st, det = q.rows[-1][4], q.rows[-1][5]
        assert r.startswith("webhook blocked") and st == "failed" and det.startswith("blocked:") and "ABC" not in det and "10.0.0.5" not in det, (u, r)
    assert recv.got == []
    allow(monkeypatch)                                                  # allow-listed internal receiver: really sent
    r = run(obs.deliver("webhook", recv.url("/h"), "subj", "body")); assert r == "webhook HTTP 200" and q.rows[-1][4] == "sent"
    assert json.loads(recv.got[-1]["body"]) == {"subject": "subj", "body": "body"}
    n = len(recv.got); run(obs.deliver("webhook", recv.redirect(recv.url("/h")), "s", "b")); assert q.rows[-1][4] == "failed" and len(recv.got) == n + 1   # redirect: not followed
    assert run(obs.deliver("email", "a@b.test", "s", "b")).startswith("draft") and q.rows[-1][4] == "draft"                                           # other kinds: still drafts

def test_deliver_public_https_goes_out(monkeypatch, obs_, tls, hijack):
    obs, q = obs_
    dns(monkeypatch, {"hook.example.test": [PUB]})
    assert run(obs.deliver("webhook", tls.url("/h", "https", "hook.example.test"), "s", "b")) == "webhook HTTP 200" and q.rows[-1][4] == "sent" and hijack == [PUB]

def test_fetch_url_tool(monkeypatch, recv):
    import deepagent_api as A
    f = lambda u: run(A._fetch_url(u))
    dns(monkeypatch, {"intra.test": ["10.0.0.5"]})
    for u in ("http://127.0.0.1:9/", "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://intra.test/", "file:///etc/passwd", "http://0.0.0.0/"):
        assert f(u).startswith("Blocked:"), u
    assert f(recv.url("/x")).startswith("Blocked:") and recv.got == []         # no allow-list: even the local receiver is out of reach
    allow(monkeypatch, "127.0.0.1")
    assert f(recv.url("/x")) == "hello" and f(recv.redirect(recv.url("/y"))) == "hello"      # allowed target, and a redirect to an allowed target
    assert f(recv.redirect("http://169.254.169.254/")).startswith("Blocked:")                # redirect to the metadata address

def test_fetch_url_reads_public_pages_over_http(monkeypatch, recv, hijack):
    import deepagent_api as A
    dns(monkeypatch, {"pub.test": [PUB]})
    assert run(A._fetch_url(recv.url("/x", host="pub.test"))) == "hello" and hijack == [PUB]

def test_notify_post_and_webhook_rules(monkeypatch, recv):
    import deepagent_api, notify   # noqa: F401
    dns(monkeypatch, {"pub.test": [PUB]})
    r = run(notify._post(recv.url("/h"), json={"a": 1})); assert not r["ok"] and r["permanent"] and "bị chặn" in r["detail"] and recv.got == []
    r = run(notify._post("http://169.254.169.254/latest/", json={})); assert not r["ok"] and r["permanent"]
    ch = {"kind": "webhook", "target": "https://pub.test/h"}            # Internet webhook without a signing secret: refused before any traffic
    with pytest.raises(notify.Permanent): run(notify._webhook(1, ch, "t", "x", None))
    allow(monkeypatch)
    assert run(notify._post(recv.url("/h"), json={"a": 1}))["ok"] and len(recv.got) == 1
    assert run(notify._webhook(1, {"kind": "webhook", "target": recv.url("/h")}, "t", "x", None))["ok"]   # internal, unsigned: allowed

def test_is_internal(monkeypatch, clean):
    dns(monkeypatch, {"pub.test": [PUB]}); allow(monkeypatch)
    assert clean.is_internal("http://127.0.0.1:9/") and not clean.is_internal("https://pub.test/") and not clean.is_internal("http://169.254.169.254/") and not clean.is_internal("nonsense")
    monkeypatch.delenv("AOS_EGRESS_ALLOW"); assert not clean.is_internal("http://127.0.0.1:9/")

def test_otlp_endpoint_validation_and_export(monkeypatch, recv):
    import deepagent_api, otel   # noqa: F401
    dns(monkeypatch, {"col.test": [PUB], "intra.test": ["10.0.0.5"]})
    for ep in ("http://169.254.169.254/x", "http://example.com", "http://col.test:4318", "https://intra.test", "https://10.0.0.5:4318", "http://[::1]:1", "ftp://x.test", "", "col.test:4318"):
        with pytest.raises(ValueError): otel.endpoint_url(ep)
    assert otel.endpoint_url("https://col.test/") == "https://col.test/v1/traces" and otel.endpoint_url("https://col.test:4318/v1/traces") == "https://col.test:4318/v1/traces"
    with pytest.raises(ValueError, match="bị chặn"): otel.endpoint_url(recv.url())            # a loopback collector needs the allow-list
    with pytest.raises(ValueError, match="bị chặn"): run(otel._send([], recv.url("/v1/traces"), {"Authorization": "Bearer t"}))
    assert recv.got == []
    allow(monkeypatch)
    assert otel.endpoint_url(recv.url()).endswith("/v1/traces")
    run(otel._send([], recv.url("/v1/traces"), {"Authorization": "Bearer t"}))
    g = recv.got[0]; assert g["path"] == "/v1/traces" and g["h"]["authorization"] == "Bearer t" and g["h"]["content-type"] == "application/x-protobuf"
    with pytest.raises(ValueError, match="bị chặn"): run(otel._send([], recv.redirect(recv.url("/h")), {}))      # collector redirect: not followed

def test_git_import_host_is_public_only_and_clone_is_pinned(monkeypatch):
    import deepagent_api, sbx_parity as S   # noqa: F401
    from fastapi import HTTPException
    allow(monkeypatch, "127.0.0.0/8, 10.0.0.0/8, ::1")                 # even an allow-listed internal address is not a "public repository"
    dns(monkeypatch, {"git.intra.test": ["10.0.0.5"], "git.test": [PUB], "mixed.test": [PUB, "127.0.0.1"], "git6.test": ["2606:2800:220:1::1946"]})
    for u in ("https://127.0.0.1/a/b", "https://[::1]/a/b", "https://10.0.0.5/a/b", "https://169.254.169.254/a/b", "https://git.intra.test/a/b", "https://mixed.test/a/b", "https://nx.test/a/b"):
        with pytest.raises(HTTPException) as e: S.validate_git_url(u)
        assert e.value.status_code == 422, u
    assert S.validate_git_url("https://git.test/o/r") == "https://git.test/o/r" and S._pin("https://git.test/o/r") == f"git.test:443:{PUB}"
    assert S._pin("https://git6.test/o/r") == "git6.test:443:[2606:2800:220:1::1946]"

def test_zalo_file_download(monkeypatch, recv, tls, hijack):
    import deepagent_api, channel_zalo as Z   # noqa: F401
    from fastapi import HTTPException
    dl = lambda u, cap=1000: run(Z._download(u, "f.txt", cap))
    dns(monkeypatch, {"cdn.example.test": [PUB], "hook.example.test": [PUB]})
    allow(monkeypatch)                                                 # the local mock lives on loopback
    assert dl(recv.url("/f")) == b"hello" and dl(recv.redirect(recv.url("/f"))) == b"hello"
    with pytest.raises(HTTPException) as e: dl(recv.url("/f"), cap=2)
    assert e.value.status_code == 413
    assert dl(tls.url("/f", "https", "hook.example.test")) == b"hello" and hijack[-1] == PUB       # a public https CDN
    n = len(recv.got)
    for u in (recv.redirect("http://169.254.169.254/x"), "http://169.254.169.254/x", "https://10.0.0.5/x", "file:///etc/passwd", "http://cdn.example.test/x"):   # last: plain http to a public host
        with pytest.raises(HTTPException) as e: dl(u)
        assert e.value.status_code == 422, u
    monkeypatch.delenv("AOS_EGRESS_ALLOW")
    with pytest.raises(HTTPException) as e: dl(recv.url("/f"))
    assert e.value.status_code == 422 and len(recv.got) == n + 1       # without the allow-list the loopback file URL is refused (only the redirect probe above arrived)


# --- 10.1 ---
# In-process against the LOCAL database (no server restart): handlers are called directly with a fake request, the app's pool is replaced by a private one, and
# the LLM is a fake object (no network, $0). Two workspaces (a, b) are seeded per test; every row is removed at the end.
import ast, importlib.util, inspect, io, textwrap, time, types, uuid
import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
import deepagent_api   # noqa: F401,E402  (first app import: the modules below import each other circularly)

PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
SEC = os.path.dirname(os.path.abspath(__file__))

class Req:
    """Just enough of a Starlette request for the handlers under test."""
    def __init__(self, body=None, headers=None, method="POST"):
        self._b = body; self.method = method; self.headers = {k.lower(): v for k, v in (headers or {}).items()}
    async def json(self): return self._b
    async def body(self): return json.dumps(self._b).encode()

class FakeGenai:
    """Stands in for google-genai: answers 'pong' (or a decision JSON when asked for JSON); counts calls."""
    def __init__(self):
        self.calls = 0; self.aio = types.SimpleNamespace(models=types.SimpleNamespace(generate_content=self._gen))
    async def _gen(self, model, contents, config):
        self.calls += 1
        txt = '{"answers": {"ok": 0.9}}' if getattr(config, "response_mime_type", None) == "application/json" else "pong"
        part = types.SimpleNamespace(text=txt, thought=False, function_call=None, thought_signature=None)
        um = types.SimpleNamespace(prompt_token_count=5, candidates_token_count=2, thoughts_token_count=0, cached_content_token_count=0)
        return types.SimpleNamespace(text=txt, usage_metadata=um, candidates=[types.SimpleNamespace(content=types.SimpleNamespace(parts=[part]), finish_reason="STOP")])

def with_db(fn):
    """Run `await fn(A, admin, W)` on a private pool (the app's RLS role) with a seeded 2-workspace world, then clean up and restore the app's globals."""
    import deepagent_api as A, admin, gateway as gw, gw_policies
    async def go():
        old = (A.S.get("pool"), A.S.get("rls"), A._g)
        async with AsyncConnectionPool(PG, min_size=1, max_size=4, open=False, kwargs={"autocommit": True, "row_factory": dict_row}) as pool:
            await pool.open(); A.S["pool"] = pool; A.S["rls"] = admin.rls_ctx
            await admin._policy("hook_calls", "aos_ws() IS NULL OR (" + admin.RLS_CHILD["hook_calls"] + ")")   # what rls_init does at boot
            A._g = FakeGenai(); gw._SNAP["t"] = 0; gw_policies._CACHE["at"] = 0
            W = await seed(A, admin)
            try: return await fn(A, admin, W)
            finally:
                try: await cleanup(A, W)
                finally: A.S["pool"], A.S["rls"], A._g = old; gw._SNAP["t"] = 0; gw_policies._CACHE["at"] = 0
    return run(go())

async def seed(A, admin):
    import gw_api as G
    tag = uuid.uuid4().hex[:8]; W = {"tag": tag, "sides": ("a", "b")}
    for s in "ab":
        ws = (await A.q("INSERT INTO workspaces(name) VALUES(%s) RETURNING id", (f"s101-{s}-{tag}",), one=True))["id"]
        tok = "vk-" + uuid.uuid4().hex; ing = "ak_" + uuid.uuid4().hex[:36]; hook = "hk_" + uuid.uuid4().hex; dkey = "mz_" + uuid.uuid4().hex[:24]
        kid = (await A.q("INSERT INTO gw_vkeys(name,prefix,key_hash,workspace_id) VALUES(%s,%s,%s,%s) RETURNING id", (f"k-{s}-{tag}", tok[:10], G._h(tok), ws), one=True))["id"]
        pid = (await A.q("INSERT INTO projects(name,workspace_id) VALUES(%s,%s) RETURNING id", (f"p-{s}-{tag}", ws), one=True))["id"]
        await A.q("INSERT INTO ingest_keys(project_id,key_hash,prefix,label) VALUES(%s,%s,%s,'t')", (pid, admin._h(ing), ing[:8]))
        aid = (await A.q("INSERT INTO agents(name,workspace_id) VALUES(%s,%s) RETURNING id", (f"ag-{s}-{tag}", ws), one=True))["id"]
        hid = (await A.q("INSERT INTO agent_hooks(agent_id,name,token_hash,prefix) VALUES(%s,'h',%s,%s) RETURNING id", (aid, admin._h(hook), hook[:9]), one=True))["id"]
        await A.q("INSERT INTO deployments(slug,agent_id,version,key_hash,key_prefix) VALUES(%s,%s,1,%s,%s)", (f"dep-{s}-{tag}", aid, A._hash(dkey), dkey[:7]))
        W[s] = {"ws": ws, "vk": tok, "kid": kid, "ing": ing, "pid": pid, "agent": aid, "hook": hook, "hid": hid, "slug": f"dep-{s}-{tag}", "dkey": dkey, "prefix": tok[:10]}
    return W

async def cleanup(A, W):
    ws = [W[s]["ws"] for s in "ab"]; kids = [W[s]["kid"] for s in "ab"]
    steps = [
        ("DELETE FROM feedback WHERE run_id IN (SELECT r.id FROM tr_runs r JOIN projects p ON p.id=r.project_id WHERE p.workspace_id = ANY(%s))", (ws,)),
        ("DELETE FROM feedback WHERE experiment_id IN (SELECT e.id FROM experiments e JOIN datasets d ON d.id=e.dataset_id WHERE d.workspace_id = ANY(%s))", (ws,)),
        ("DELETE FROM feedback_tokens WHERE run_id IN (SELECT r.id FROM tr_runs r JOIN projects p ON p.id=r.project_id WHERE p.workspace_id = ANY(%s))", (ws,)),
        ("DELETE FROM run_attachments WHERE run_id IN (SELECT r.id FROM tr_runs r JOIN projects p ON p.id=r.project_id WHERE p.workspace_id = ANY(%s))", (ws,)),
        ("DELETE FROM exp_results WHERE experiment_id IN (SELECT e.id FROM experiments e JOIN datasets d ON d.id=e.dataset_id WHERE d.workspace_id = ANY(%s))", (ws,)),
        ("DELETE FROM experiments WHERE dataset_id IN (SELECT id FROM datasets WHERE workspace_id = ANY(%s))", (ws,)),
        ("DELETE FROM tr_runs WHERE project_id IN (SELECT id FROM projects WHERE workspace_id = ANY(%s))", (ws,)),
        ("DELETE FROM datasets WHERE workspace_id = ANY(%s)", (ws,)),
        ("DELETE FROM gateway_usage WHERE vkey_id = ANY(%s) OR workspace_id = ANY(%s)", (kids, ws)),
        ("DELETE FROM gw_cache WHERE vkey_id = ANY(%s)", (kids,)),
        ("DELETE FROM gw_policies WHERE workspace_id = ANY(%s)", (ws,)),
        ("DELETE FROM spend_caps WHERE workspace_id = ANY(%s)", (ws,)),
        ("DELETE FROM gw_vkeys WHERE id = ANY(%s)", (kids,)),
        ("DELETE FROM ingest_keys WHERE project_id IN (SELECT id FROM projects WHERE workspace_id = ANY(%s))", (ws,)),
        ("DELETE FROM projects WHERE workspace_id = ANY(%s)", (ws,)),
        ("DELETE FROM agents WHERE workspace_id = ANY(%s)", (ws,)),
        ("DELETE FROM workspaces WHERE id = ANY(%s)", (ws,)),
    ]
    for sql, args in steps:
        try: await A.q(sql, args)
        except Exception as e: print("cleanup:", sql[:50], str(e)[:100])   # noqa: BLE001  (best effort: never hide the test result)

async def as_user(admin, ws, fn):
    t = admin.USER.set({"id": 987654, "username": "t101", "role": "admin", "ws": ws, "auth": "test"})
    try: return await fn()
    finally: admin.USER.reset(t)

async def sysq(A, sql, args=()): return await A.q(sql, args)       # no USER / WS_CTX = the system context
async def seen_by(A, admin, ws, sql, args=()): return await as_user(admin, ws, lambda: A.q(sql, args))

CHAT = {"model": "gemini-flash-lite-latest", "messages": [{"role": "user", "content": "ping"}]}
ANTH = {"model": "gemini-flash-lite-latest", "max_tokens": 16, "messages": [{"role": "user", "content": "ping"}]}
def jbody(resp): return json.loads(resp.body)

def test_as_workspace_semantics():
    import deepagent_api, admin   # noqa: F401  (deepagent_api first: circular imports)
    async def go():
        assert admin.WS_CTX.get() is None
        async with admin.as_workspace(7):
            assert admin.rls_ctx()[0] == "7"
            async with admin.as_workspace(9): assert admin.WS_CTX.get() == 9
            assert admin.WS_CTX.get() == 7
        assert admin.WS_CTX.get() is None and admin.rls_ctx()[0] == "*"
        with pytest.raises(RuntimeError):
            async with admin.as_workspace(5): raise RuntimeError("boom")
        assert admin.WS_CTX.get() is None                                    # restored even when the body fails
        for bad in (None, True):                                             # a missing id must never fall back to the system context
            with pytest.raises(ValueError):
                async with admin.as_workspace(bad): pass
        t = admin.USER.set({"id": 1, "username": "u", "role": "admin", "ws": 3})
        try:
            async with admin.as_workspace(8): assert admin.WS_CTX.get() is None and admin.rls_ctx()[0] == "3"   # a signed-in user keeps its own workspace
        finally: admin.USER.reset(t)
    run(go())

def test_gateway_calls_land_in_the_key_workspace():
    """/v1/chat/completions and /v1/messages (both write through G._record): usage row + `gateway` trace in the KEY's workspace, invisible to the other one."""
    import gw_api as G, ext
    async def go(A, admin, W):
        for s in "ab":
            r1 = await G.chat_completions(Req(CHAT), authorization="Bearer " + W[s]["vk"]); assert r1.status_code == 200
            r2 = await ext.anth_messages(Req(ANTH), x_api_key=W[s]["vk"], authorization=""); assert r2.status_code == 200, r2.body
        for s, o in (("a", "b"), ("b", "a")):
            mine = await seen_by(A, admin, W[s]["ws"], "SELECT r.tags FROM tr_runs r JOIN projects p ON p.id=r.project_id WHERE p.name='gateway' AND %s = ANY(r.tags)", (f"vkey:{W[s]['prefix']}",))
            assert len(mine) == 2                                            # one per route
            other = await seen_by(A, admin, W[s]["ws"], "SELECT 1 FROM tr_runs WHERE %s = ANY(tags)", (f"vkey:{W[o]['prefix']}",))
            assert other == []                                               # the other workspace's traces are not visible here
            wrong = await sysq(A, "SELECT p.workspace_id FROM tr_runs r JOIN projects p ON p.id=r.project_id WHERE %s = ANY(r.tags) AND p.workspace_id <> %s", (f"vkey:{W[s]['prefix']}", W[s]["ws"]))
            assert wrong == []                                               # and nothing leaked into workspace 1 (the old behaviour)
            use = await sysq(A, "SELECT workspace_id FROM gateway_usage WHERE vkey_id=%s", (W[s]["kid"],))
            assert len(use) == 2 and {u["workspace_id"] for u in use} == {W[s]["ws"]}
    with_db(go)

def test_stream_record_lands_in_the_key_workspace():
    """The streaming generators call G._record from the response task: still the key's workspace."""
    import gw_api as G, ext
    async def go(A, admin, W):
        s = "b"
        async def agen_models(model, contents, config):
            async def it():
                yield (await A._g._gen(model, contents, config))
            return it()
        A._g.aio.models.generate_content_stream = agen_models
        r = await G.chat_completions(Req({**CHAT, "stream": True}), authorization="Bearer " + W[s]["vk"])
        async for _ in r.body_iterator: pass
        r = await ext.anth_messages(Req({**ANTH, "stream": True}), x_api_key=W[s]["vk"], authorization="")
        async for _ in r.body_iterator: pass
        rows = await sysq(A, "SELECT p.workspace_id, r.tags FROM tr_runs r JOIN projects p ON p.id=r.project_id WHERE %s = ANY(r.tags)", (f"vkey:{W[s]['prefix']}",))
        assert len(rows) == 2 and {x["workspace_id"] for x in rows} == {W[s]["ws"]} and all("stream" in x["tags"] for x in rows)
    with_db(go)

def test_gateway_cache_is_per_workspace():
    """The response cache key includes the workspace: the same prompt from the other workspace is a miss, not a hit on foreign data."""
    import gw_api as G
    async def go(A, admin, W):
        await sysq(A, "UPDATE gw_vkeys SET cache_ttl_s=60 WHERE id = ANY(%s)", ([W["a"]["kid"], W["b"]["kid"]],))
        body = {**CHAT, "temperature": 0}
        h = lambda r: r.headers["x-agentos-cache"]
        assert h(await G.chat_completions(Req(body), authorization="Bearer " + W["a"]["vk"])) == "miss"
        assert h(await G.chat_completions(Req(body), authorization="Bearer " + W["a"]["vk"])) == "hit"
        assert h(await G.chat_completions(Req(body), authorization="Bearer " + W["b"]["vk"])) == "miss"      # b must not read a's cached answer
        assert h(await G.chat_completions(Req(body), authorization="Bearer " + W["b"]["vk"])) == "hit"
    with_db(go)

def test_decisions_usage_is_attributed_to_the_key_workspace():
    """/v1/decisions: the usage row written by gateway.generate() carries the key's workspace (it was NULL: no user, no WS_CTX)."""
    import gw_policies as P
    async def go(A, admin, W):
        body = {"input": "hello", "questions": [{"type": "predicate", "name": "ok", "instructions": "is it a greeting?"}]}
        for s in "ab":
            r = await P.decisions(Req(body), authorization="Bearer " + W[s]["vk"])
            assert isinstance(r, dict) and r["answers"][0]["probability"] == 0.9, r
            rows = await sysq(A, "SELECT workspace_id FROM gateway_usage WHERE vkey_id=%s", (W[s]["kid"],))
            assert rows and {x["workspace_id"] for x in rows} == {W[s]["ws"]}
    with_db(go)

def span(tid, sid, name): return {"traceId": tid, "spanId": sid, "name": name, "startTimeUnixNano": str(int(time.time() * 1e9)), "endTimeUnixNano": str(int(time.time() * 1e9) + 5_000_000), "attributes": []}

def test_ingest_runs_otlp_attachments_feedback_stay_in_the_key_workspace():
    import obs, otel
    from starlette.datastructures import Headers, UploadFile
    async def go(A, admin, W):
        a, b = W["a"], W["b"]
        rid = str(uuid.uuid4()); now = time.time()
        r = await obs.ingest(obs.Ingest(post=[{"id": rid, "name": "orig", "run_type": "chain", "start_time": now - 1, "end_time": now, "inputs": {"q": 1}, "outputs": {"a": "mine"}}]), x_api_key=a["ing"])
        assert r["ok"]
        assert [x["name"] for x in await seen_by(A, admin, a["ws"], "SELECT name FROM tr_runs WHERE id::text=%s", (rid,))] == ["orig"]
        assert await seen_by(A, admin, b["ws"], "SELECT 1 FROM tr_runs WHERE id::text=%s", (rid,)) == []
        # NEGATIVE: workspace b's key patches workspace a's run id -> a's run is untouched (before 10.1 the system context overwrote it)
        await obs.ingest(obs.Ingest(patch=[{"id": rid, "name": "HIJACKED", "outputs": {"a": "evil"}, "end_time": now}]), x_api_key=b["ing"])
        mine = await seen_by(A, admin, a["ws"], "SELECT name, outputs FROM tr_runs WHERE id::text=%s", (rid,))
        assert [(x["name"], x["outputs"]) for x in mine] == [("orig", {"a": "mine"})]
        # (b's patch above was stored as b's OWN new run: an unknown id is an insert.) Fresh run that only a owns, for the checks below:
        rid = str(uuid.uuid4())
        await obs.ingest(obs.Ingest(post=[{"id": rid, "name": "only-a", "run_type": "chain", "start_time": now - 1, "end_time": now}]), x_api_key=a["ing"])
        # NEGATIVE: feedback with b's key on a's run -> 404, nothing stored; with a's own key it works and b cannot see it
        with pytest.raises(HTTPException) as e: await obs.ingest_feedback(obs.FbIn(run_id=rid, key="thumb", score=1.0), x_api_key=b["ing"])
        assert e.value.status_code == 404 and await sysq(A, "SELECT 1 FROM feedback WHERE run_id::text=%s", (rid,)) == []
        await obs.ingest_feedback(obs.FbIn(run_id=rid, key="thumb", score=1.0), x_api_key=a["ing"])
        assert len(await seen_by(A, admin, a["ws"], "SELECT 1 FROM feedback WHERE run_id::text=%s", (rid,))) == 1
        assert await seen_by(A, admin, b["ws"], "SELECT 1 FROM feedback WHERE run_id::text=%s", (rid,)) == []
        # NEGATIVE: attachment of a foreign run -> 404; own run ok
        up = lambda: UploadFile(file=io.BytesIO(b"hello"), filename="a.txt", headers=Headers({"content-type": "text/plain"}))
        with pytest.raises(HTTPException) as e: await obs.att_ingest(run_id=rid, file=up(), x_api_key=b["ing"])
        assert e.value.status_code == 404
        assert (await obs.att_ingest(run_id=rid, file=up(), x_api_key=a["ing"]))["id"]
        # NEGATIVE: bad / revoked key -> 401 on every ingest route
        for call in (obs.ingest(obs.Ingest(), x_api_key="ak_nope"), obs.ingest_feedback(obs.FbIn(run_id=rid, key="k"), x_api_key=""), obs.att_ingest(run_id=rid, file=up(), x_api_key="x")):
            with pytest.raises(HTTPException) as e: await call
            assert e.value.status_code == 401
        # OTLP: lands in the key's workspace; the same trace/span ids sent with the other workspace's key cannot rename it
        tid, sid = uuid.uuid4().hex, uuid.uuid4().hex[:16]
        otlp = lambda name: Req({"resourceSpans": [{"scopeSpans": [{"spans": [span(tid, sid, name)]}]}]}, {"content-type": "application/json"})
        await otel.otlp_traces(otlp("otlp-a"), x_api_key=b["ing"])
        wrong = await sysq(A, "SELECT p.workspace_id FROM tr_runs r JOIN projects p ON p.id=r.project_id WHERE r.name='otlp-a'")
        assert [x["workspace_id"] for x in wrong] == [b["ws"]]
        await sysq(A, "DELETE FROM tr_runs WHERE name='otlp-a'")
        await otel.otlp_traces(otlp("otlp-a"), x_api_key=a["ing"])
        await otel.otlp_traces(otlp("OTLP-HIJACK"), x_api_key=b["ing"])
        rows = await sysq(A, "SELECT p.workspace_id, r.name FROM tr_runs r JOIN projects p ON p.id=r.project_id WHERE r.name IN ('otlp-a','OTLP-HIJACK') ORDER BY 1")
        assert (a["ws"], "otlp-a") in {(x["workspace_id"], x["name"]) for x in rows}            # a's span is still named otlp-a
        assert all(x["workspace_id"] == b["ws"] for x in rows if x["name"] == "OTLP-HIJACK")     # b's attempt, if stored at all, is b's own
        await sysq(A, "UPDATE ingest_keys SET revoked=true WHERE key_hash=%s", (admin._h(a["ing"]),))   # a revoked key is refused everywhere (ingest + OTLP)
        with pytest.raises(HTTPException) as e: await obs.ingest(obs.Ingest(), x_api_key=a["ing"])
        assert e.value.status_code == 401
        with pytest.raises(HTTPException) as e: await otel.otlp_traces(otlp("late"), x_api_key=a["ing"])
        assert e.value.status_code == 401
    with_db(go)

def test_ingest_experiments_land_in_the_key_workspace():
    import evals
    async def go(A, admin, W):
        a, b = W["a"], W["b"]; ds = f"ds-{W['tag']}"
        mk = lambda out: evals.ExtExp(dataset=ds, experiment="e1", results=[evals.ExtResult(inputs={"q": "hi"}, outputs={"answer": out}, feedback={"ok": True})])
        ra = await evals.ext_experiment(mk("A"), x_api_key=a["ing"])
        rb = await evals.ext_experiment(mk("B"), x_api_key=b["ing"])
        assert ra["dataset_id"] != rb["dataset_id"]                                             # same name, two workspaces = two datasets (not a merge in workspace 1)
        d = await sysq(A, "SELECT id, workspace_id FROM datasets WHERE name=%s ORDER BY id", (ds,))
        assert {(x["id"], x["workspace_id"]) for x in d} == {(ra["dataset_id"], a["ws"]), (rb["dataset_id"], b["ws"])}
        assert [x["id"] for x in await seen_by(A, admin, a["ws"], "SELECT id FROM datasets WHERE name=%s", (ds,))] == [ra["dataset_id"]]
        assert [x["id"] for x in await seen_by(A, admin, b["ws"], "SELECT id FROM experiments WHERE id=%s OR id=%s", (ra["experiment_id"], rb["experiment_id"]))] == [rb["experiment_id"]]
        tr = await sysq(A, "SELECT p.workspace_id FROM tr_runs r JOIN projects p ON p.id=r.project_id WHERE %s = ANY(r.tags)", (f"exp:{ra['experiment_id']}",))
        assert tr and {x["workspace_id"] for x in tr} == {a["ws"]}                               # experiment traces too
        n = await sysq(A, "SELECT count(*) n FROM dataset_examples WHERE dataset_id=%s", (ra["dataset_id"],))
        assert n[0]["n"] == 1                                                                   # b's upload did not append examples to a's dataset
        with pytest.raises(HTTPException) as e: await evals.ext_experiment(mk("x"), x_api_key="ak_bad")
        assert e.value.status_code == 401
    with_db(go)

def test_hook_and_deployment_run_in_the_agents_workspace(monkeypatch):
    import studio
    async def go(A, admin, W):
        seen = []
        async def fake_run(agent_id, version, thread_id, message, **kw):
            seen.append((agent_id, admin.WS_CTX.get())); yield {"reply": "ok", "error": None, "run_id": "r1", "tokens": 1, "cost_usd": 0, "trace_id": None}
        async def fake_ver(aid): return 1
        monkeypatch.setattr(A, "run_agent", fake_run); monkeypatch.setattr(A, "cur_version", fake_ver)
        for s in "ab":
            r = await studio.hook_call(W[s]["hook"], studio.HookCall(message="hi", wait=True)); assert r["status"] == "ok"
            i = await A.public_invoke(W[s]["slug"], A.Invoke(message="hi"), x_api_key=W[s]["dkey"]); assert i["reply"] == "ok"
        assert [w for _, w in seen] == [W["a"]["ws"], W["a"]["ws"], W["b"]["ws"], W["b"]["ws"]]    # run context = the hook's / deployment's workspace
        assert admin.WS_CTX.get() is None                                                        # and the context was restored afterwards
        for s, o in (("a", "b"), ("b", "a")):                                                     # the call log is visible only in its own workspace (new RLS policy)
            assert len(await seen_by(A, admin, W[s]["ws"], "SELECT 1 FROM hook_calls WHERE hook_id=%s", (W[s]["hid"],))) == 1
            assert await seen_by(A, admin, W[s]["ws"], "SELECT 1 FROM hook_calls WHERE hook_id=%s", (W[o]["hid"],)) == []
        # NEGATIVE: the other workspace's key / unknown token / disabled hook / paused deployment are refused and write nothing
        with pytest.raises(HTTPException) as e: await A.public_invoke(W["a"]["slug"], A.Invoke(message="hi"), x_api_key=W["b"]["dkey"])
        assert e.value.status_code == 401
        with pytest.raises(HTTPException) as e: await A.public_invoke(W["a"]["slug"], A.Invoke(message="hi"), x_api_key="")
        assert e.value.status_code == 401
        with pytest.raises(HTTPException) as e: await studio.hook_call("hk_unknown", studio.HookCall(message="hi", wait=True))
        assert e.value.status_code == 401
        await sysq(A, "UPDATE agent_hooks SET enabled=false WHERE id=%s", (W["a"]["hid"],))
        with pytest.raises(HTTPException) as e: await studio.hook_call(W["a"]["hook"], studio.HookCall(message="hi", wait=True))
        assert e.value.status_code == 401
        await sysq(A, "UPDATE deployments SET status='paused' WHERE slug=%s", (W["a"]["slug"],))
        n = len(seen)
        with pytest.raises(HTTPException) as e: await A.public_invoke(W["a"]["slug"], A.Invoke(message="hi"), x_api_key=W["a"]["dkey"])
        assert e.value.status_code == 503 and len(seen) == n
    with_db(go)

def test_share_feedback_token_lands_in_the_runs_workspace():
    import ext, obs
    async def go(A, admin, W):
        a, b = W["a"], W["b"]; rid = uuid.uuid4(); now = time.time()
        await obs.upsert_run(a["pid"], {"id": str(rid), "name": "fbrun", "start_time": now - 1, "end_time": now})
        t1 = "fbt_" + uuid.uuid4().hex
        await sysq(A, "INSERT INTO feedback_tokens(token_hash,run_id,key,expires,max_uses) VALUES(%s,%s,'thumb',now()+interval '1 hour',1)", (admin._h(t1), rid))
        r = await ext.fb_public(t1, Req(method="GET"), score=1.0)
        assert r["ok"]
        assert len(await seen_by(A, admin, a["ws"], "SELECT 1 FROM feedback WHERE run_id=%s", (rid,))) == 1
        assert await seen_by(A, admin, b["ws"], "SELECT 1 FROM feedback WHERE run_id=%s", (rid,)) == []
        with pytest.raises(HTTPException) as e: await ext.fb_public(t1, Req(method="GET"), score=1.0)      # single use
        assert e.value.status_code == 403
        t2 = "fbt_" + uuid.uuid4().hex                                                                 # expired
        await sysq(A, "INSERT INTO feedback_tokens(token_hash,run_id,key,expires,max_uses) VALUES(%s,%s,'thumb',now()-interval '1 hour',1)", (admin._h(t2), rid))
        with pytest.raises(HTTPException) as e: await ext.fb_public(t2, Req(method="GET"), score=1.0)
        assert e.value.status_code == 403
        with pytest.raises(HTTPException) as e: await ext.fb_public("fbt_unknown", Req(method="GET"), score=1.0)
        assert e.value.status_code == 403
        t3 = "fbt_" + uuid.uuid4().hex                                                                 # token whose run is gone: refused, use not consumed
        ghost = uuid.uuid4()
        await sysq(A, "INSERT INTO feedback_tokens(token_hash,run_id,key,expires,max_uses) VALUES(%s,%s,'thumb',now()+interval '1 hour',1)", (admin._h(t3), ghost))
        with pytest.raises(HTTPException) as e: await ext.fb_public(t3, Req(method="GET"), score=1.0)
        assert e.value.status_code == 404
        assert (await sysq(A, "SELECT uses FROM feedback_tokens WHERE token_hash=%s", (admin._h(t3),)))[0]["uses"] == 0
        await sysq(A, "DELETE FROM feedback_tokens WHERE token_hash=ANY(%s)", ([admin._h(x) for x in (t1, t2, t3)],))
    with_db(go)

# -- route scan: every public (no user) route that can write must put its work in a workspace, or be listed here with the reason --
ALLOW_NO_WS = {
    "/api/auth/login": "password sign-in: writes the user's own session / audit rows with the workspace chosen from the user's memberships; not a key-authenticated data write",
    "/api/auth/2fa/verify": "second step of the same sign-in (challenge token): session + audit rows for the signed-in user",
    "/channels/zalo/{binding_id}": "signature-authenticated; every insert passes the binding's workspace_id explicitly (channels._system) and the agent run uses run_agent (agent's workspace)",
    "/internal/zalo-personal/{binding_id}/incoming": "loopback + shared token only; rows carry the binding's workspace_id explicitly",
    "/internal/zalo-personal/{binding_id}/session": "loopback + shared token only; ws_secrets row carries the binding's workspace_id explicitly",
    "/d/{slug}/health": "read-only: returns {status, version} of a globally unique public slug",
    "/sbx/{sid}/{svc}/{path:path}": "token-authenticated reverse proxy to a sandbox service: its only write is an UPDATE by primary key of the service the signed token names (no row is created, no workspace default)",
    "/login/oidc/callback": "SSO sign-in: provisions the user in the workspace configured by the admin (oidc_config.default_workspace); not data written by a key",
}
SCAN_ALSO_GET = {"/login/oidc/callback"}       # GET routes that do write (the card scans non-GET + /d/*); /share/feedback/{tok} is GET+POST so it is scanned anyway

def _flat(routes):
    for r in routes:
        if hasattr(r, "effective_route_contexts"): yield from r.effective_route_contexts()
        else: yield r

def public_routes():
    """{path: (methods, endpoint)} of the public routes that may write: non-GET methods, every /d/* route and SCAN_ALSO_GET (not /static/, /login*, /favicon.ico)."""
    import deepagent_api as A, admin
    out = {}; public = admin.PUBLIC_PREFIX + ("/sbx/",)      # "/sbx/" is added to PUBLIC_PREFIX at boot (sbx_parity.init_late); its router is already mounted at import
    for r in _flat(A.app.routes):
        orig = getattr(r, "original_route", None)
        path = getattr(r, "path", None); ep = getattr(r, "endpoint", None) or getattr(orig, "endpoint", None); ms = getattr(r, "methods", None) or getattr(orig, "methods", None)
        if not path or not ep or not ms or not path.startswith(public): continue
        if path.startswith(("/static/", "/login", "/favicon.ico")) and path not in SCAN_ALSO_GET: continue
        if (ms - {"GET", "HEAD", "OPTIONS"}) or path.startswith("/d/") or path in SCAN_ALSO_GET: out[path] = (ms, ep)
    return out

def mentions_as_workspace(fn, depth=4, seen=None):
    """True if fn, or an app function it calls (resolved through the module globals and the imports inside it), uses as_workspace."""
    seen = set() if seen is None else seen
    if fn in seen or depth < 0 or not inspect.isfunction(fn): return False
    seen.add(fn)
    try: src = textwrap.dedent(inspect.getsource(fn))
    except (OSError, TypeError): return False
    if "as_workspace" in src: return True
    tree = ast.parse(src); mods = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names: mods[(a.asname or a.name).split(".")[0]] = a.name
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call): continue
        f, target = n.func, None
        if isinstance(f, ast.Name): target = fn.__globals__.get(f.id)
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            base = fn.__globals__.get(f.value.id) or sys.modules.get(mods.get(f.value.id, ""))
            target = getattr(base, f.attr, None) if base is not None else None
        sf = os.path.abspath(inspect.getsourcefile(target) or "") if inspect.isfunction(target) else ""
        if sf and (sf.startswith(os.path.abspath(os.path.join(SEC, "..", "app"))) or sf == os.path.abspath(__file__)) and mentions_as_workspace(target, depth - 1, seen): return True
    return False

def test_route_scan_every_public_writer_uses_as_workspace():
    routes = public_routes()
    must = {"/ingest/runs", "/ingest/feedback", "/ingest/attachments", "/ingest/experiments", "/ingest/otlp/v1/traces", "/otel/v1/traces", "/v1/chat/completions", "/v1/embeddings",
            "/v1/messages", "/v1/decisions", "/d/hook/{token}", "/d/{slug}/invoke", "/d/{slug}/health", "/share/feedback/{tok}", "/api/auth/login"}
    assert must <= set(routes), f"the scan no longer finds: {sorted(must - set(routes))} (did the framework change how routes are stored?)"
    bad = sorted(p for p, (_, ep) in routes.items() if p not in ALLOW_NO_WS and not mentions_as_workspace(ep))
    assert not bad, f"public routes that write without admin.as_workspace (use it, or list them in ALLOW_NO_WS with a reason): {bad}"
    stale = sorted(p for p in ALLOW_NO_WS if p not in routes)
    assert not stale, f"ALLOW_NO_WS lists routes that do not exist (or are not scanned): {stale}"
    assert all(len(r) > 20 for r in ALLOW_NO_WS.values())
    for p in ("/ingest/runs", "/v1/messages", "/d/{slug}/invoke"): assert p not in ALLOW_NO_WS      # the fixed routes must keep using it, not hide in the allow-list

async def _scan_plain(): return 1
async def _scan_sneaky(): return await _scan_plain()
async def _scan_good():
    import admin
    async with admin.as_workspace(1): return 1
async def _scan_via_helper(): return await _scan_good()

def test_route_scan_detects_a_route_without_it():
    import deepagent_api   # noqa: F401
    assert not mentions_as_workspace(_scan_sneaky) and mentions_as_workspace(_scan_good) and mentions_as_workspace(_scan_via_helper)

# -- ops/fix_gateway_ws.py: dry-run by default, --apply moves misfiled rows --
def load_fix_tool():
    spec = importlib.util.spec_from_file_location("fix_gateway_ws", os.path.join(SEC, "..", "ops", "fix_gateway_ws.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def test_fix_gateway_ws_dry_run_and_apply_on_seeded_rows(capsys, monkeypatch):
    """Everything happens inside ONE transaction that is rolled back at the end: the real local data is never changed, not even by --apply."""
    F = load_fix_tool(); tag = uuid.uuid4().hex[:6]
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, row_factory=dict_row) as c:       # autocommit off: nothing below is committed
            try:
                async def one(sql, args=()): return (await (await c.execute(sql, args)).fetchone())
                w1 = (await one("INSERT INTO workspaces(name) VALUES(%s) RETURNING id", (f"fx-wrong-{tag}",)))["id"]       # where the old bug filed everything
                w2 = (await one("INSERT INTO workspaces(name) VALUES(%s) RETURNING id", (f"fx-right-{tag}",)))["id"]
                w3 = (await one("INSERT INTO workspaces(name) VALUES(%s) RETURNING id", (f"fx-amb-{tag}",)))["id"]
                p1 = (await one("INSERT INTO projects(name,workspace_id) VALUES('gateway',%s) RETURNING id", (w1,)))["id"]
                k = (await one("INSERT INTO gw_vkeys(name,prefix,key_hash,workspace_id) VALUES('fx',%s,%s,%s) RETURNING id", (f"vk-fx{tag}", "h" + tag + "1", w2)))["id"]
                for n, ws in (("a", w2), ("b", w3)):                                   # an AMBIGUOUS prefix: two workspaces share it
                    await c.execute("INSERT INTO gw_vkeys(name,prefix,key_hash,workspace_id) VALUES('amb',%s,%s,%s)", (f"vk-am{tag}", "h" + tag + n, ws))
                def run_(tags, inputs=None):
                    return c.execute("INSERT INTO tr_runs(id,trace_id,project_id,name,start_time,tags,inputs) VALUES(%s,%s,%s,'chat.completions',now(),%s,%s)",
                                     (u := uuid.uuid4(), u, p1, tags, psycopg.types.json.Jsonb(inputs) if inputs is not None else None))
                await run_([f"vkey:vk-fx{tag}"]); await run_([f"vkey:vk-fx{tag}"])
                await run_([f"vkey:vk-am{tag}"])                                        # ambiguous -> must stay
                await run_([f"vkey:vk-fx{tag}"], {"$aos_enc": "v2:sealed", "c": "inputs", "r": "x"})   # sealed for the old workspace -> must stay
                await run_(["vkey:vk-nobody"])                                          # key deleted long ago -> must stay
                await c.execute("INSERT INTO gateway_usage(model,vkey_id,workspace_id,cost_usd,status) VALUES('m',%s,NULL,0,'ok')", (k,))          # /v1/decisions row (NULL)
                await c.execute("INSERT INTO gateway_usage(model,vkey_id,workspace_id,cost_usd,status) VALUES('m',%s,%s,0,'ok')", (k, w1))          # filed in the wrong workspace
                await c.execute("INSERT INTO gateway_usage(model,vkey_id,workspace_id,cost_usd,status) VALUES('m',%s,%s,0,'ok')", (k, w2))          # already right
                d = (await one("INSERT INTO datasets(name,description,workspace_id) VALUES(%s,'created from an external experiment',%s) RETURNING id", (f"fxds-{tag}", w1)))["id"]
                dclash = (await one("INSERT INTO datasets(name,description,workspace_id) VALUES(%s,'created from an external experiment',%s) RETURNING id", (f"fxds2-{tag}", w1)))["id"]
                await c.execute("INSERT INTO datasets(name,workspace_id) VALUES(%s,%s)", (f"fxds2-{tag}", w2))                                  # name taken in the target workspace
                x = (await one("INSERT INTO experiments(name,dataset_id,target,status) VALUES('e',%s,%s,'done') RETURNING id", (d, psycopg.types.json.Jsonb({"type": "external"}))))["id"]
                pe = (await one("INSERT INTO projects(name,workspace_id) VALUES('experiments',%s) ON CONFLICT(workspace_id,name) DO UPDATE SET name=EXCLUDED.name RETURNING id", (w1,)))["id"]
                u = uuid.uuid4(); await c.execute("INSERT INTO tr_runs(id,trace_id,project_id,name,start_time,tags) VALUES(%s,%s,%s,'ext',now(),%s)", (u, u, pe, ["external", f"exp:{x}"]))

                mine = lambda pl: [r for r in pl["traces"] if r["prefix"] in (f"vk-fx{tag}",)]
                pl = await F.plan(c, {d: w2, dclash: w2})
                assert [(r["from_ws"], r["to_ws"], r["n"]) for r in mine(pl)] == [(w1, w2, 2)]                       # dry-run plan: 2 moves, not the sealed / ambiguous / orphan ones
                assert any(r["prefix"] == f"vk-am{tag}" and r["n"] == 1 for r in pl["ambiguous"]) and pl["sealed"] >= 1
                assert [(r["from_ws"], r["to_ws"], r["n"]) for r in pl["usage"] if r["vkey_id"] == k] == [(None, w2, 1), (w1, w2, 1)]
                assert d in [r["id"] for r in pl["candidates"]] and [x_["id"] for x_ in pl["datasets"]] == [d] and any("already exists" in e for e in pl["dataset_errors"])
                lines = "\n".join(F.render(pl)); assert "DRY RUN" in lines and "APPLIED" not in lines
                where = lambda: one("SELECT count(*) FILTER (WHERE p.workspace_id=%s) wrong, count(*) FILTER (WHERE p.workspace_id=%s) right FROM tr_runs r JOIN projects p ON p.id=r.project_id WHERE %s = ANY(r.tags)", (w1, w2, f"vkey:vk-fx{tag}"))
                before = await where(); assert (before["wrong"], before["right"]) == (3, 0)
                await F.plan(c, {})                                                                                 # planning twice changes nothing
                assert dict(await where()) == dict(before)
                done = await F.apply(c, pl)
                assert done["traces"] >= 2 and done["usage"] >= 2 and done["datasets"] == 1 and done["dataset_traces"] == 1   # >=: apply() works on the whole database, real misfiled rows included (all rolled back below)
                after = await where(); assert (after["wrong"], after["right"]) == (1, 2)                           # the sealed one stays
                assert (await one("SELECT count(*) n FROM gateway_usage WHERE vkey_id=%s AND workspace_id=%s", (k, w2)))["n"] == 3
                assert (await one("SELECT workspace_id w FROM datasets WHERE id=%s", (d,)))["w"] == w2 and (await one("SELECT workspace_id w FROM datasets WHERE id=%s", (dclash,)))["w"] == w1
                assert (await one("SELECT p.workspace_id w FROM tr_runs r JOIN projects p ON p.id=r.project_id WHERE r.id=%s", (u,)))["w"] == w2
                assert (await one("SELECT count(*) n FROM tr_runs r JOIN projects p ON p.id=r.project_id WHERE %s = ANY(r.tags) AND p.workspace_id=%s", (f"vkey:vk-am{tag}", w1)))["n"] == 1   # ambiguous stayed
                again = await F.plan(c, {}); assert mine(again) == [] and not [r for r in again["usage"] if r["vkey_id"] == k]            # idempotent: nothing left to do
            finally: await c.rollback()
    run(go())
    # the CLI: no flag = dry run (apply() must not even be called; the real local data is only read), and it says so
    async def boom(*a, **k): raise AssertionError("apply() called without --apply")
    monkeypatch.setattr(F, "apply", boom); monkeypatch.setenv("PG_URI", PG)
    assert run(F.main([])) == 0
    out = capsys.readouterr().out; assert "DRY RUN - nothing changed" in out and "APPLIED" not in out
    with pytest.raises(SystemExit): run(F.main(["--dataset", "x=y"]))                       # bad flag value is refused before touching the database

# --- 10.3 ---
BLOCK = "INSERT INTO gw_policies(workspace_id,name,kind,scope,match_header,match_value,limit_usd_month) VALUES(%s,%s,'spend','workspace','customer','bad',0)"   # "block": a $0 cap for one header value

def test_header_policy_blocks_every_gateway_route_alike():
    """A policy matching an X-Gateway-* header blocks /v1/chat/completions, /v1/messages, /v1/embeddings and /v1/decisions (same enforce(), same order), before any LLM call."""
    import gw_api as G, gw_policies as P, ext
    async def go(A, admin, W):
        a, b = W["a"], W["b"]
        await sysq(A, BLOCK, (a["ws"], "block-bad")); P._CACHE["at"] = 0
        bad = {"x-gateway-customer": "bad"}; auth = lambda s: "Bearer " + W[s]["vk"]
        dec = {"input": "hi", "questions": [{"type": "predicate", "name": "ok", "instructions": "?"}]}
        # blocked: status 402 on all four routes; the messages route answers in Anthropic's error shape
        with pytest.raises(HTTPException) as e: await G.chat_completions(Req(CHAT, bad), authorization=auth("a"))
        assert e.value.status_code == 402 and e.value.detail["error"]["type"] == "budget_exceeded"
        r = await ext.anth_messages(Req(ANTH, bad), x_api_key=a["vk"], authorization="")
        assert r.status_code == 402 and jbody(r)["type"] == "error" and "block-bad" in jbody(r)["error"]["message"] and jbody(r)["error"]["type"] == "api_error"
        r = await ext.anth_messages(Req(ANTH, bad), x_api_key="", authorization=auth("a")); assert r.status_code == 402            # Bearer form too
        with pytest.raises(HTTPException) as e: await G.embeddings(Req({"input": "x"}, bad), authorization=auth("a"))
        assert e.value.status_code == 402
        with pytest.raises(HTTPException) as e: await P.decisions(Req(dec, bad), authorization=auth("a"))
        assert e.value.status_code == 402
        assert A._g.calls == 0                                                                                                   # nothing reached the model
        # not blocked: no header / another value / the OTHER workspace's key with the very same header (policies are per workspace)
        for hdr, s in (({}, "a"), ({"x-gateway-customer": "good"}, "a"), (bad, "b")):
            assert (await G.chat_completions(Req(CHAT, hdr), authorization=auth(s))).status_code == 200
            assert (await ext.anth_messages(Req(ANTH, hdr), x_api_key=W[s]["vk"], authorization="")).status_code == 200
        # a client cannot dodge the policy by renaming / hiding the header through the key's own header policy... unless the operator allows it: allow-list drops it
        await sysq(A, "UPDATE gw_vkeys SET header_policy=%s WHERE id=%s", (psycopg.types.json.Jsonb({"allow": ["tier"]}), a["kid"]))
        assert (await ext.anth_messages(Req(ANTH, bad), x_api_key=a["vk"], authorization="")).status_code == 200                 # header not allowed -> never reaches the policy (operator's choice)
        await sysq(A, "UPDATE gw_vkeys SET header_policy=%s WHERE id=%s", (psycopg.types.json.Jsonb({"inject": {"customer": "bad"}}), a["kid"]))
        assert (await ext.anth_messages(Req(ANTH, {"x-gateway-customer": "good"}), x_api_key=a["vk"], authorization="")).status_code == 402   # injected value wins over the client's
    with_db(go)

def test_rate_policy_applies_to_messages_with_anthropic_error_shape():
    import gw_api as G, gw_policies as P, ext
    async def go(A, admin, W):
        a = W["a"]
        await sysq(A, "INSERT INTO gw_policies(workspace_id,name,kind,scope,rpm) VALUES(%s,'one-per-minute','rate','key',1)", (a["ws"],)); P._CACHE["at"] = 0
        assert (await ext.anth_messages(Req(ANTH), x_api_key=a["vk"], authorization="")).status_code == 200
        r = await ext.anth_messages(Req(ANTH), x_api_key=a["vk"], authorization="")
        assert r.status_code == 429 and jbody(r)["error"]["type"] == "rate_limit_error" and "one-per-minute" in jbody(r)["error"]["message"]
        with pytest.raises(HTTPException) as e: await G.chat_completions(Req(CHAT), authorization="Bearer " + a["vk"])        # same bucket as the chat route
        assert e.value.status_code == 429
        assert (await ext.anth_messages(Req(ANTH), x_api_key=W["b"]["vk"], authorization="")).status_code == 200              # other workspace: its own policies
    with_db(go)

def test_key_budget_and_workspace_spend_cap_apply_to_messages():
    import gw_api as G, gateway as gw, ext
    async def go(A, admin, W):
        a, b = W["a"], W["b"]
        await sysq(A, "UPDATE gw_vkeys SET budget_usd_month=0.0000001 WHERE id=%s", (a["kid"],))
        await sysq(A, "INSERT INTO gateway_usage(model,vkey_id,workspace_id,cost_usd,status) VALUES('m',%s,%s,1,'ok')", (a["kid"], a["ws"]))
        with pytest.raises(HTTPException) as e: await G.chat_completions(Req(CHAT), authorization="Bearer " + a["vk"])
        assert e.value.status_code == 402
        r = await ext.anth_messages(Req(ANTH), x_api_key=a["vk"], authorization=""); assert r.status_code == 402 and "Hết ngân sách" in jbody(r)["error"]["message"]
        assert A._g.calls == 0
        # workspace spend cap (unified caps): b's workspace only
        await sysq(A, "INSERT INTO spend_caps(workspace_id,scope,target,period,limit_usd,action) VALUES(%s,'workspace',%s,'total',0,'block')", (b["ws"], str(b["ws"]))); gw._SNAP["t"] = 0
        r = await ext.anth_messages(Req(ANTH), x_api_key=b["vk"], authorization=""); assert r.status_code == 402 and jbody(r)["error"]["type"] == "api_error"
        with pytest.raises(HTTPException) as e: await G.chat_completions(Req(CHAT), authorization="Bearer " + b["vk"])
        assert e.value.status_code == 402 and e.value.detail["error"]["type"] == "budget_exceeded"
        await sysq(A, "UPDATE gw_vkeys SET budget_usd_month=NULL WHERE id=%s", (a["kid"],))
        assert (await ext.anth_messages(Req(ANTH), x_api_key=a["vk"], authorization="")).status_code == 200                      # a is not affected by b's cap
    with_db(go)

def test_messages_refuses_bad_or_disabled_keys_in_anthropic_shape():
    import ext
    async def go(A, admin, W):
        for kw in ({"x_api_key": "vk-nope", "authorization": ""}, {"x_api_key": "", "authorization": ""}, {"x_api_key": "ak_" + "0" * 36, "authorization": ""}):
            r = await ext.anth_messages(Req(ANTH), **kw); assert r.status_code == 401 and jbody(r)["error"]["type"] == "authentication_error", kw
        await sysq(A, "UPDATE gw_vkeys SET enabled=false WHERE id=%s", (W["a"]["kid"],))
        r = await ext.anth_messages(Req(ANTH), x_api_key=W["a"]["vk"], authorization=""); assert r.status_code == 401
        await sysq(A, "UPDATE gw_vkeys SET models=%s WHERE id=%s", (["gemini-3.6-flash"], W["b"]["kid"]))                           # model allow-list still enforced
        r = await ext.anth_messages(Req(ANTH), x_api_key=W["b"]["vk"], authorization=""); assert r.status_code == 403 and jbody(r)["error"]["type"] == "invalid_request_error"
        assert A._g.calls == 0
    with_db(go)

# --- 10.28 ---
# RLS for workspace_id NULL rows ("whole machine" rows), the feedback policy (rows without a trace follow feedback.project_id), the gateway key/cache tables and
# the outbox. Direct psycopg sessions switch role exactly like deepagent_api.q does (set_config app.ws + role aos_rls). Every seeded row is removed at the end.
_APPLIED28 = []

def apply28(force=False):
    """What the next boot does for these tables on the local DB: numbered migrations, then the RLS policies from admin (same helpers + same conditions as rls_init)."""
    if _APPLIED28 and not force: return
    import deepagent_api as A, admin, migrations
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as c: await migrations.run_pending(c)
        async with AsyncConnectionPool(PG, min_size=1, max_size=2, open=False, kwargs={"autocommit": True, "row_factory": dict_row}) as pool:
            await pool.open(); old = (A.S.get("pool"), A.S.get("rls")); A.S["pool"] = pool; A.S["rls"] = None
            try:
                await admin._policy("feedback", "aos_ws() IS NULL OR (" + admin.RLS_CHILD["feedback"] + ")")
                await admin._policy("gw_vkeys", "aos_ws() IS NULL OR workspace_id = aos_ws()")
                await admin._policy("gw_cache", "aos_ws() IS NULL OR (" + admin.RLS_CHILD["gw_cache"] + ")")
                for t in admin.GLOBAL_ROW_TABLES: await admin._policy_global(t)
                for t in ("experiments", "eval_runs"): await admin._policy(t, "aos_ws() IS NULL OR (" + admin.RLS_CHILD[t] + ")")   # 10.28b
            finally: A.S["pool"], A.S["rls"] = old
    run(go()); _APPLIED28.append(1)

def with_db28(fn):
    apply28(); return with_db(fn)

def _pg(): return psycopg.connect(PG, autocommit=True, row_factory=dict_row)

def _as(c, ws):
    """Session of workspace `ws` (an int), or the system session ('*')."""
    c.execute("SELECT set_config('app.ws',%s,false), set_config('app.uid','0',false), set_config('app.role','admin',false), set_config('role',%s,false)", (str(ws), "aos_rls" if ws != "*" else "none"))

@pytest.fixture
def w28():
    """Two workspaces, each with a project, a traced run, a feedback row with a trace and a feedback row without one; plus one orphan row (no trace, no project)."""
    apply28(); tag = uuid.uuid4().hex[:8]; W = {"tag": tag, "fb": [], "ws": [], "pid": [], "runs": []}
    with _pg() as c:
        try:
            for s in "ab":
                ws = c.execute("INSERT INTO workspaces(name) VALUES(%s) RETURNING id", (f"s1028-{s}-{tag}",)).fetchone()["id"]; W["ws"].append(ws)
                pid = c.execute("INSERT INTO projects(name,workspace_id) VALUES(%s,%s) RETURNING id", (f"p1028-{s}-{tag}", ws)).fetchone()["id"]; W["pid"].append(pid)
                tr = uuid.uuid4(); W["runs"].append(tr)
                c.execute("INSERT INTO tr_runs(id,trace_id,project_id,name,run_type,start_time) VALUES(%s,%s,%s,'r1028','chain',now())", (tr, tr, pid))
                traced = c.execute("INSERT INTO feedback(run_id,trace_id,project_id,key,score) VALUES(%s,%s,%s,'t1028',1) RETURNING id", (tr, tr, pid)).fetchone()["id"]
                loose = c.execute("INSERT INTO feedback(run_id,project_id,key,score) VALUES(%s,%s,'t1028',1) RETURNING id", (uuid.uuid4(), pid)).fetchone()["id"]
                W[s] = {"ws": ws, "pid": pid, "trace": tr, "traced": traced, "loose": loose}; W["fb"] += [traced, loose]
            W["orphan"] = c.execute("INSERT INTO feedback(run_id,key,score) VALUES(%s,'t1028',1) RETURNING id", (uuid.uuid4(),)).fetchone()["id"]; W["fb"].append(W["orphan"])
            yield W
        finally:
            c.execute("SELECT set_config('role','none',false), set_config('app.ws','*',false)")
            c.execute("DELETE FROM feedback WHERE id = ANY(%s) OR key LIKE 't1028%%'", (W["fb"],)); c.execute("DELETE FROM tr_runs WHERE id = ANY(%s)", (W["runs"],))
            c.execute("DELETE FROM projects WHERE id = ANY(%s)", (W["pid"],)); c.execute("DELETE FROM workspaces WHERE id = ANY(%s)", (W["ws"],))

def _ids(c, sql, args=()): return {r["id"] for r in c.execute(sql, args).fetchall()}

def test_feedback_rls_workspace_isolation_with_and_without_trace(w28):
    a, b = w28["a"], w28["b"]; mine = [w28["fb"][0], w28["fb"][1]]; theirs = [w28["fb"][2], w28["fb"][3]]
    with _pg() as c:
        _as(c, a["ws"])
        assert _ids(c, "SELECT id FROM feedback WHERE key='t1028'") == set(mine)         # own traced + own trace-less row; NOT b's, NOT the orphan
        # UPDATE / DELETE of the other workspace's rows (traced and trace-less) and of the orphan touch nothing
        assert c.execute("UPDATE feedback SET score=99 WHERE id = ANY(%s)", (theirs + [w28["orphan"]],)).rowcount == 0
        assert c.execute("DELETE FROM feedback WHERE id = ANY(%s)", (theirs + [w28["orphan"]],)).rowcount == 0
        # INSERT into the other workspace: with b's trace, with b's project (no trace), with no project at all (the old hole), with a project that does not exist
        for cols, vals in ((("run_id", "trace_id"), (uuid.uuid4(), b["trace"])), (("run_id", "project_id"), (uuid.uuid4(), b["pid"])),
                           (("run_id",), (uuid.uuid4(),)), (("run_id", "project_id"), (uuid.uuid4(), 999999999))):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(f"INSERT INTO feedback({','.join(cols)},key) VALUES({','.join(['%s'] * len(cols))},'t1028x')", (*vals,))
        # moving an own row into the other workspace is refused too (WITH CHECK)
        with pytest.raises(psycopg.errors.InsufficientPrivilege): c.execute("UPDATE feedback SET project_id=%s WHERE id=%s", (b["pid"], a["loose"]))
        # own rows still work
        ok = c.execute("INSERT INTO feedback(run_id,project_id,key) VALUES(%s,%s,'t1028') RETURNING id", (uuid.uuid4(), a["pid"])).fetchone()["id"]; w28["fb"].append(ok)
        assert c.execute("UPDATE feedback SET score=5 WHERE id=%s", (ok,)).rowcount == 1
        _as(c, b["ws"])
        assert _ids(c, "SELECT id FROM feedback WHERE key='t1028'") == set(theirs)       # symmetric
        _as(c, "*")
        assert _ids(c, "SELECT id FROM feedback WHERE key='t1028'") >= set(mine + theirs + [w28["orphan"]])   # the system session sees everything, incl. the orphan
        assert c.execute("SELECT score FROM feedback WHERE id=%s", (a["loose"],)).fetchone()["score"] == 1       # nothing above changed a row

def test_feedback_rls_experiment_rows_follow_the_experiment(w28):
    a, b = w28["a"], w28["b"]
    with _pg() as c:
        ds = c.execute("INSERT INTO datasets(name,workspace_id) VALUES(%s,%s) RETURNING id", (f"d1028-{w28['tag']}", a["ws"])).fetchone()["id"]
        ex = c.execute("INSERT INTO experiments(name,dataset_id,target) VALUES('e1028',%s,'{}') RETURNING id", (ds,)).fetchone()["id"]
        fb = c.execute("INSERT INTO feedback(run_id,key,score,experiment_id) VALUES(%s,'t1028e',1,%s) RETURNING id", (uuid.uuid4(), ex)).fetchone()["id"]
        try:
            _as(c, a["ws"]); assert _ids(c, "SELECT id FROM feedback WHERE key='t1028e'") == {fb}
            _as(c, b["ws"]); assert _ids(c, "SELECT id FROM feedback WHERE key='t1028e'") == set()
            assert c.execute("DELETE FROM feedback WHERE id=%s", (fb,)).rowcount == 0
            with pytest.raises(psycopg.errors.InsufficientPrivilege): c.execute("INSERT INTO feedback(run_id,key,experiment_id) VALUES(%s,'t1028e',%s)", (uuid.uuid4(), ex))
        finally:
            _as(c, "*"); c.execute("DELETE FROM feedback WHERE id=%s", (fb,)); c.execute("DELETE FROM experiments WHERE id=%s", (ex,)); c.execute("DELETE FROM datasets WHERE id=%s", (ds,))

def test_feedback_added_through_the_app_records_the_project(w28):
    import deepagent_api as A, admin, obs
    async def go():
        async with AsyncConnectionPool(PG, min_size=1, max_size=2, open=False, kwargs={"autocommit": True, "row_factory": dict_row}) as pool:
            await pool.open(); old = (A.S.get("pool"), A.S.get("rls")); A.S["pool"] = pool; A.S["rls"] = admin.rls_ctx
            try:
                a, b = w28["a"], w28["b"]
                r = await as_user(admin, a["ws"], lambda: obs.add_feedback(obs.FbIn(run_id=str(a["trace"]), key="t1028", score=1.0)))
                w28["fb"].append(r["id"])
                assert (await sysq(A, "SELECT project_id, trace_id FROM feedback WHERE id=%s", (r["id"],)))[0] == {"project_id": a["pid"], "trace_id": a["trace"]}
                with pytest.raises(HTTPException) as e: await as_user(admin, b["ws"], lambda: obs.add_feedback(obs.FbIn(run_id=str(a["trace"]), key="t1028", score=1.0)))
                assert e.value.status_code == 404      # b cannot even see a's run
            finally: A.S["pool"], A.S["rls"] = old
    run(go())

def test_migration_0002_backfills_and_down_up_round_trip(w28):
    import migrations
    m0002 = next(m for v, m, _ in migrations._load() if v == "0002")
    col = "SELECT 1 FROM information_schema.columns WHERE table_name='feedback' AND column_name='project_id'"
    async def go():
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as conn:
            q = migrations._q(conn)
            await q("UPDATE feedback SET project_id=NULL WHERE id=%s", (w28["a"]["traced"],))           # an old-style row: has a run, no project yet
            await m0002.up(q)                                                                        # idempotent, and it back-fills
            assert (await q("SELECT project_id FROM feedback WHERE id=%s", (w28["a"]["traced"],)))[0][0] == w28["a"]["pid"]
            assert (await q("SELECT project_id FROM feedback WHERE id=%s", (w28["orphan"],)))[0][0] is None   # no run -> stays NULL (hidden from workspaces)
            async with conn.transaction(force_rollback=True):                                        # down() then up() in a transaction that is rolled back
                await m0002.down(q)
                assert not (await q(col))
                await m0002.up(q)
                assert (await q(col))
            assert (await q(col))
            assert [r[0] for r in await q("SELECT version FROM schema_migrations WHERE version='0002'")] == ["0002"]
    run(go())

def test_global_row_tables_inventory_and_policy_shape():
    import admin
    apply28(); assert admin.GLOBAL_ROW_TABLES == ["spend_caps"]
    assert "gw_vkeys" in admin.RLS_DIRECT and "gw_cache" in admin.RLS_CHILD and "project_id IN (SELECT id FROM projects)" in admin.RLS_CHILD["feedback"]
    with _pg() as c:
        pol = {r["policyname"]: r for r in c.execute("SELECT policyname, cmd, qual, with_check FROM pg_policies WHERE tablename='spend_caps'").fetchall()}
        assert set(pol) == {"aos_ws_sel", "aos_ws_ins", "aos_ws_upd", "aos_ws_del"}                  # the old single ALL policy (which let a workspace write NULL rows) is gone
        assert "workspace_id IS NULL" in pol["aos_ws_sel"]["qual"] and "workspace_id IS NULL" not in (pol["aos_ws_ins"]["with_check"] or "")
        assert "workspace_id IS NULL" not in pol["aos_ws_upd"]["qual"] and "workspace_id IS NULL" not in pol["aos_ws_del"]["qual"]
        rs = c.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname IN ('spend_caps','feedback','gw_vkeys','gw_cache')").fetchall()
        assert len(rs) == 4 and all(r["relrowsecurity"] and r["relforcerowsecurity"] for r in rs)

def test_policies_are_idempotent_for_an_existing_database():
    apply28(force=True); apply28(force=True)       # a second and third boot over the same DB: no error, still exactly the four policies
    with _pg() as c:
        assert sorted(r["policyname"] for r in c.execute("SELECT policyname FROM pg_policies WHERE tablename='spend_caps'").fetchall()) == ["aos_ws_del", "aos_ws_ins", "aos_ws_sel", "aos_ws_upd"]
        assert [r["policyname"] for r in c.execute("SELECT policyname FROM pg_policies WHERE tablename='feedback'").fetchall()] == ["aos_ws"]

def test_global_rows_workspace_reads_only_system_writes(w28):
    a, b = w28["a"], w28["b"]; tg = w28["tag"]
    ins = "INSERT INTO spend_caps(workspace_id,scope,target,period,limit_usd,action,enabled) VALUES(%s,'feature',%s,'month',1,'warn',false) RETURNING id"   # enabled=false: never enforced
    with _pg() as c:
        ids = []
        try:
            _as(c, "*")
            g = c.execute(ins, (None, f"g1028-{tg}")).fetchone()["id"]; ids.append(g)                       # the system session writes a NULL row
            ob = c.execute(ins, (b["ws"], f"b1028-{tg}")).fetchone()["id"]; ids.append(ob)
            _as(c, a["ws"])
            assert _ids(c, "SELECT id FROM spend_caps WHERE target LIKE %s", (f"%1028-{tg}",)) == {g}      # reads the global row, NOT b's row
            with pytest.raises(psycopg.errors.InsufficientPrivilege): c.execute(ins, (None, f"x1028-{tg}"))  # cannot create a global row
            assert c.execute("UPDATE spend_caps SET limit_usd=999 WHERE id=%s", (g,)).rowcount == 0           # cannot change it
            assert c.execute("DELETE FROM spend_caps WHERE id=%s", (g,)).rowcount == 0                        # cannot delete it
            with pytest.raises(psycopg.errors.InsufficientPrivilege): c.execute(ins, (b["ws"], f"y1028-{tg}"))  # cannot write into another workspace
            mine = c.execute(ins, (a["ws"], f"m1028-{tg}")).fetchone()["id"]; ids.append(mine)              # own rows: full access
            assert c.execute("UPDATE spend_caps SET limit_usd=2 WHERE id=%s", (mine,)).rowcount == 1
            with pytest.raises(psycopg.errors.InsufficientPrivilege): c.execute("UPDATE spend_caps SET workspace_id=NULL WHERE id=%s", (mine,))   # cannot turn an own row global
            with pytest.raises(psycopg.errors.InsufficientPrivilege): c.execute("UPDATE spend_caps SET workspace_id=%s WHERE id=%s", (b["ws"], mine))
            assert c.execute("DELETE FROM spend_caps WHERE id=%s", (mine,)).rowcount == 1
            _as(c, "*")
            assert c.execute("SELECT limit_usd FROM spend_caps WHERE id=%s", (g,)).fetchone()["limit_usd"] == 1   # untouched
            assert c.execute("UPDATE spend_caps SET limit_usd=3 WHERE id=%s", (g,)).rowcount == 1               # the system session may write it
        finally:
            _as(c, "*"); c.execute("DELETE FROM spend_caps WHERE id = ANY(%s)", (ids,))

def test_gateway_keys_and_cache_are_private_to_their_workspace():
    import gw_api as G
    async def go(A, admin, W):
        a, b = W["a"], W["b"]; tg = W["tag"]
        assert [k["id"] for k in await as_user(admin, a["ws"], G.vk_list)] == [a["kid"]]                    # list: only own (before 10.28: every workspace's keys)
        assert [k["id"] for k in await as_user(admin, b["ws"], G.vk_list)] == [b["kid"]]
        assert (await as_user(admin, a["ws"], lambda: G.vk_usage(a["kid"])))["key"]["id"] == a["kid"]
        for call in (lambda: G.vk_usage(b["kid"]), lambda: G.vk_upd(b["kid"], {"name": "HIJACK", "enabled": False}), lambda: G.vk_del(b["kid"])):
            with pytest.raises(HTTPException) as e: await as_user(admin, a["ws"], call)
            assert e.value.status_code == 404
        row = (await sysq(A, "SELECT name, enabled FROM gw_vkeys WHERE id=%s", (b["kid"],)))[0]
        assert row["name"] == f"k-b-{tg}" and row["enabled"]                                                 # b's key is untouched
        await as_user(admin, a["ws"], lambda: G.vk_upd(a["kid"], {"rpm": 7}))
        assert (await sysq(A, "SELECT rpm FROM gw_vkeys WHERE id=%s", (a["kid"],)))[0]["rpm"] == 7
        assert [k["prefix"] for k in (await as_user(admin, a["ws"], G.overview))["by_key"]] == [a["prefix"]]
        # the answer cache follows the key's workspace
        ha, hb = f"h1028a-{tg}", f"h1028b-{tg}"
        await sysq(A, "INSERT INTO gw_cache(h,vkey_id,response) VALUES(%s,%s,'{}'),(%s,%s,'{}')", (ha, a["kid"], hb, b["kid"]))
        assert [r["h"] for r in await seen_by(A, admin, a["ws"], "SELECT h FROM gw_cache WHERE h IN (%s,%s)", (ha, hb))] == [ha]
        assert await as_user(admin, a["ws"], lambda: A.q("UPDATE gw_cache SET hits=99 WHERE h=%s RETURNING h", (hb,))) == []
        assert await as_user(admin, a["ws"], lambda: A.q("DELETE FROM gw_cache WHERE h=%s RETURNING h", (hb,))) == []
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await as_user(admin, a["ws"], lambda: A.q("INSERT INTO gw_cache(h,vkey_id,response) VALUES(%s,%s,'{}')", (f"x{hb}", b["kid"])))
        assert (await sysq(A, "SELECT hits FROM gw_cache WHERE h=%s", (hb,)))[0]["hits"] == 0
        await sysq(A, "DELETE FROM gw_cache WHERE h IN (%s,%s)", (ha, hb))
    with_db28(go)

def test_global_spend_caps_stay_read_only_for_a_workspace_admin():
    import gw_api as G
    async def go(A, admin, W):
        a = W["a"]; tg = W["tag"]
        gid = (await sysq(A, "INSERT INTO spend_caps(workspace_id,scope,target,period,limit_usd,action,enabled) VALUES(NULL,'feature',%s,'month',1,'warn',false) RETURNING id", (f"gc1028-{tg}",)))[0]["id"]
        oid = (await sysq(A, "INSERT INTO spend_caps(workspace_id,scope,target,period,limit_usd,action,enabled) VALUES(%s,'feature',%s,'month',1,'warn',false) RETURNING id", (a["ws"], f"oc1028-{tg}")))[0]["id"]
        try:
            listed = {r["id"] for r in await as_user(admin, a["ws"], G.caps_list)}
            assert oid in listed and gid not in listed                                                        # the workspace admin's list shows its own caps, not the machine-wide ones
            for call in (lambda: G.caps_update(gid, G.CapPatch(limit_usd=999)), lambda: G.caps_delete(gid)):
                with pytest.raises(HTTPException) as e: await as_user(admin, a["ws"], call)
                assert e.value.status_code == 404
            assert float((await sysq(A, "SELECT limit_usd FROM spend_caps WHERE id=%s", (gid,)))[0]["limit_usd"]) == 1.0
            assert (await as_user(admin, a["ws"], lambda: G.caps_update(oid, G.CapPatch(limit_usd=2))))["limit_usd"] == 2.0   # own cap still editable
        finally: await sysq(A, "DELETE FROM spend_caps WHERE id = ANY(%s)", ([gid, oid],))
    with_db28(go)

def test_outbox_rows_carry_their_workspace_and_listing_is_filtered():
    import obs
    async def go(A, admin, W):
        a, b = W["a"], W["b"]; s = lambda x: f"S1028-{W['tag']}-{x}"
        await obs.deliver("email", "a@example.com", s("a"), "body", ws_id=a["ws"])
        await obs.deliver("email", "b@example.com", s("b"), "body", ws_id=b["ws"])
        await obs.deliver("email", "sys@example.com", s("sys"), "body")                                       # no caller, no owner -> a system row
        await as_user(admin, a["ws"], lambda: obs.deliver("email", "a2@example.com", s("a2"), "body"))          # a signed-in caller -> its workspace
        assert (await obs.deliver("webhook", "http://127.0.0.1:9/hook", s("hook"), "{}", ws_id=a["ws"])).startswith("webhook blocked")   # private address: refused, still recorded for a
        r = {"channel": "email_draft", "target": "x@example.com", "project_id": a["pid"], "channel_cfg": None}
        await obs._dispatch(r, s("alert"), "body")                                                            # background alert: the project's workspace
        rows = {x["subject"]: x["ws_id"] for x in await sysq(A, "SELECT subject, ws_id FROM outbox WHERE subject LIKE %s", (f"S1028-{W['tag']}-%",))}
        assert rows == {s("a"): a["ws"], s("b"): b["ws"], s("sys"): None, s("a2"): a["ws"], s("hook"): a["ws"], s("alert"): a["ws"]}
        mine = lambda lst: {x["subject"] for x in lst if x["subject"].startswith(f"S1028-{W['tag']}-")}
        assert mine(await as_user(admin, a["ws"], obs.outbox)) == {s("a"), s("a2"), s("hook"), s("alert")}
        assert mine(await as_user(admin, b["ws"], obs.outbox)) == {s("b")}                                    # b sees neither a's rows nor the system row
        assert "sys@example.com" not in {x["target"] for x in await as_user(admin, a["ws"], obs.outbox)}
        demo = admin.USER.set({"id": 5, "username": "d", "role": "viewer", "ws": a["ws"], "auth": "test", "demo": True})
        try: assert await obs.outbox() == []                                                                # demo accounts still get nothing
        finally: admin.USER.reset(demo)
        await sysq(A, "DELETE FROM outbox WHERE subject LIKE %s", (f"S1028-{W['tag']}-%",))
    with_db28(go)

def test_rls_init_boots_twice_over_an_existing_database(monkeypatch):
    """The real rls_init() (as run at every boot), narrowed to the tables 10.28 touches so it does not re-lock the whole schema while other suites use the DB."""
    import deepagent_api as A, admin
    apply28()
    monkeypatch.setattr(admin, "RLS_DIRECT", ["gw_vkeys"]); monkeypatch.setattr(admin, "RLS_UNIQUE", [])
    monkeypatch.setattr(admin, "RLS_CHILD", {k: v for k, v in admin.RLS_CHILD.items() if k in ("feedback", "gw_cache")})
    async def go():
        async with AsyncConnectionPool(PG, min_size=1, max_size=2, open=False, kwargs={"autocommit": True, "row_factory": dict_row}) as pool:
            await pool.open(); old = (A.S.get("pool"), A.S.get("rls")); A.S["pool"] = pool; A.S["rls"] = None
            try: await admin.rls_init(); await admin.rls_init()
            finally: A.S["pool"], A.S["rls"] = old
    run(go())
    with _pg() as c:
        by = {}
        for r in c.execute("SELECT tablename, policyname FROM pg_policies WHERE tablename IN ('spend_caps','feedback','gw_vkeys','gw_cache')").fetchall(): by.setdefault(r["tablename"], []).append(r["policyname"])
        assert sorted(by["spend_caps"]) == ["aos_ws_del", "aos_ws_ins", "aos_ws_sel", "aos_ws_upd"] and by["feedback"] == ["aos_ws"] and by["gw_vkeys"] == ["aos_ws"] and by["gw_cache"] == ["aos_ws"]

# --- 10.28b ---
# experiments / eval_runs rows with dataset_id / agent_id NULL used to be visible to every workspace (now: workspace_id column, migration 0006, trigger + policy),
# GET /api/gateway is filtered by workspace (gateway_usage has no RLS on purpose), GET /api/obs/outbox is workspace-admin only.
async def as_role(admin, ws, role, fn, **extra):
    t = admin.USER.set({"id": 987655, "username": "t102b", "role": role, "ws": ws, "auth": "test", **extra})
    try: return await fn()
    finally: admin.USER.reset(t)

def test_experiments_without_dataset_belong_to_one_workspace():
    async def go(A, admin, W):
        a, b = W["a"], W["b"]; nm = lambda x: f"x1028b-{W['tag']}-{x}"
        ins = lambda ws, name, ds=None, tgt=None, **kw: as_user(admin, ws, lambda: A.q(
            "INSERT INTO experiments(name,dataset_id,target" + (",workspace_id" if "wid" in kw else "") + ") VALUES(%s,%s,%s" + (",%s" if "wid" in kw else "") + ") RETURNING id, workspace_id",
            (name, ds, psycopg.types.json.Jsonb(tgt or {}), *([kw["wid"]] if "wid" in kw else [])), one=True))
        sys_ins = lambda name, ds=None, tgt=None: A.q("INSERT INTO experiments(name,dataset_id,target) VALUES(%s,%s,%s) RETURNING id, workspace_id", (name, ds, psycopg.types.json.Jsonb(tgt or {})), one=True)
        try:
            ds_a = (await A.q("INSERT INTO datasets(name,workspace_id) VALUES(%s,%s) RETURNING id", (nm("ds"), a["ws"]), one=True))["id"]
            x_null = await ins(a["ws"], nm("a-null"))                                      # no dataset, written by a workspace-A session -> workspace A
            x_ds = await ins(a["ws"], nm("a-ds"), ds_a)
            x_agent = await sys_ins(nm("b-agent"), None, {"type": "agent", "agent_id": b["agent"]})   # no dataset but the target agent is b's -> workspace b
            x_none = await sys_ins(nm("none"))                                             # names nothing, written by the system -> default workspace 1
            assert (x_null["workspace_id"], x_ds["workspace_id"], x_agent["workspace_id"], x_none["workspace_id"]) == (a["ws"], a["ws"], b["ws"], 1)
            mine = lambda ws: as_user(admin, ws, lambda: A.q("SELECT id FROM experiments WHERE name LIKE %s", (f"x1028b-{W['tag']}-%",)))
            assert {r["id"] for r in await mine(a["ws"])} == {x_null["id"], x_ds["id"]}    # the NULL-dataset row is a's alone; not b's, not the default-workspace one
            assert {r["id"] for r in await mine(b["ws"])} == {x_agent["id"]}
            # b cannot touch a's NULL-dataset experiment, nor see its results
            await A.q("INSERT INTO exp_results(experiment_id,repetition) VALUES(%s,1)", (x_null["id"],))
            assert await as_user(admin, b["ws"], lambda: A.q("SELECT id FROM exp_results WHERE experiment_id=%s", (x_null["id"],))) == []
            assert await as_user(admin, b["ws"], lambda: A.q("UPDATE experiments SET name='hacked' WHERE id=%s RETURNING id", (x_null["id"],))) == []
            assert await as_user(admin, b["ws"], lambda: A.q("DELETE FROM experiments WHERE id=%s RETURNING id", (x_null["id"],))) == []
            assert len(await as_user(admin, a["ws"], lambda: A.q("SELECT id FROM exp_results WHERE experiment_id=%s", (x_null["id"],)))) == 1
            # refused writes: b naming a's dataset, b forcing workspace a, a forging workspace b, a moving its row into b
            with pytest.raises(psycopg.errors.InsufficientPrivilege): await ins(b["ws"], nm("b-steal"), ds_a)
            with pytest.raises(psycopg.errors.InsufficientPrivilege): await ins(b["ws"], nm("b-forge"), wid=a["ws"])
            with pytest.raises(psycopg.errors.InsufficientPrivilege): await ins(a["ws"], nm("a-forge"), wid=b["ws"])
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await as_user(admin, a["ws"], lambda: A.q("UPDATE experiments SET workspace_id=%s WHERE id=%s", (b["ws"], x_null["id"])))
            assert (await as_user(admin, a["ws"], lambda: A.q("UPDATE experiments SET progress='1' WHERE id=%s RETURNING id", (x_null["id"],)))) != []   # own row still editable
        finally:
            await A.q("DELETE FROM experiments WHERE name LIKE %s", (f"x1028b-{W['tag']}-%",)); await A.q("DELETE FROM datasets WHERE name LIKE %s", (f"x1028b-{W['tag']}-%",))
    with_db28(go)

def test_eval_runs_without_agent_belong_to_one_workspace():
    async def go(A, admin, W):
        a, b = W["a"], W["b"]; mdl = f"e1028b-{W['tag']}"
        ins = lambda ws, agent=None, **kw: as_user(admin, ws, lambda: A.q(
            "INSERT INTO eval_runs(model,agent_id" + (",workspace_id" if "wid" in kw else "") + ") VALUES(%s,%s" + (",%s" if "wid" in kw else "") + ") RETURNING id, workspace_id",
            (mdl, agent, *([kw["wid"]] if "wid" in kw else [])), one=True))
        try:
            r_null = await ins(a["ws"])                                                    # no agent, a workspace-A session -> A
            r_ag = await ins(a["ws"], a["agent"])
            r_sysb = await A.q("INSERT INTO eval_runs(model,agent_id) VALUES(%s,%s) RETURNING id, workspace_id", (mdl, b["agent"]), one=True)   # system writer: the agent's workspace
            r_none = await A.q("INSERT INTO eval_runs(model) VALUES(%s) RETURNING id, workspace_id", (mdl,), one=True)                          # nothing known -> workspace 1
            assert (r_null["workspace_id"], r_ag["workspace_id"], r_sysb["workspace_id"], r_none["workspace_id"]) == (a["ws"], a["ws"], b["ws"], 1)
            mine = lambda ws: as_user(admin, ws, lambda: A.q("SELECT id FROM eval_runs WHERE model=%s", (mdl,)))
            assert {r["id"] for r in await mine(a["ws"])} == {r_null["id"], r_ag["id"]}
            assert {r["id"] for r in await mine(b["ws"])} == {r_sysb["id"]}
            await A.q("INSERT INTO eval_scores(run_id,kind,score) VALUES(%s,'t1028b',1)", (r_null["id"],))
            assert await as_user(admin, b["ws"], lambda: A.q("SELECT id FROM eval_scores WHERE run_id=%s", (r_null["id"],))) == []      # scores follow the run
            assert await as_user(admin, b["ws"], lambda: A.q("UPDATE eval_runs SET error='x' WHERE id=%s RETURNING id", (r_null["id"],))) == []
            assert await as_user(admin, b["ws"], lambda: A.q("DELETE FROM eval_runs WHERE id=%s RETURNING id", (r_null["id"],))) == []
            with pytest.raises(psycopg.errors.InsufficientPrivilege): await ins(b["ws"], a["agent"])        # b naming a's agent
            with pytest.raises(psycopg.errors.InsufficientPrivilege): await ins(b["ws"], wid=a["ws"])
            with pytest.raises(psycopg.errors.InsufficientPrivilege): await ins(a["ws"], wid=b["ws"])
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await as_user(admin, a["ws"], lambda: A.q("UPDATE eval_runs SET workspace_id=%s WHERE id=%s", (b["ws"], r_null["id"])))
        finally:
            await A.q("DELETE FROM eval_scores WHERE kind='t1028b'"); await A.q("DELETE FROM eval_runs WHERE model=%s", (mdl,))
    with_db28(go)

def test_migration_0006_backfills_old_rows_and_down_up_round_trips():
    import migrations
    m = next(m for v, m, _ in migrations._load() if v == "0006")
    col = "SELECT 1 FROM information_schema.columns WHERE table_name='experiments' AND column_name='workspace_id'"
    async def go(A, admin, W):
        a, b = W["a"], W["b"]; tag = W["tag"]
        async with await psycopg.AsyncConnection.connect(PG, autocommit=True) as conn:
            q = migrations._q(conn)
            async with conn.transaction(force_rollback=True):                              # everything below is rolled back
                ds = (await q("INSERT INTO datasets(name,workspace_id) VALUES(%s,%s) RETURNING id", (f"x1028b-{tag}", a["ws"])))[0][0]
                for t in ("experiments", "eval_runs"):                                     # an old-style database: no trigger, column nullable
                    await q(f"ALTER TABLE {t} DISABLE TRIGGER aos_fill_ws"); await q(f"ALTER TABLE {t} ALTER COLUMN workspace_id DROP NOT NULL")
                ex = lambda name, d, tgt: q("INSERT INTO experiments(name,dataset_id,target,workspace_id) VALUES(%s,%s,%s,NULL) RETURNING id", (name, d, psycopg.types.json.Jsonb(tgt)))
                e1, e2, e3 = [r[0][0] for r in (await ex("o1", ds, {}), await ex("o2", None, {"type": "agent", "agent_id": b["agent"]}), await ex("o3", None, {}))]
                ev = lambda ag: q("INSERT INTO eval_runs(model,agent_id,workspace_id) VALUES('o',%s,NULL) RETURNING id", (ag,))
                r1, r2 = (await ev(a["agent"]))[0][0], (await ev(None))[0][0]
                await m.up(q); await m.up(q)                                               # back-fills, and is idempotent
                ws_of = lambda t, i: q(f"SELECT workspace_id FROM {t} WHERE id=%s", (i,))
                assert [(await ws_of("experiments", e))[0][0] for e in (e1, e2, e3)] == [a["ws"], b["ws"], 1]
                assert [(await ws_of("eval_runs", r))[0][0] for r in (r1, r2)] == [a["ws"], 1]
                assert (await q("SELECT is_nullable FROM information_schema.columns WHERE table_name='eval_runs' AND column_name='workspace_id'"))[0][0] == "NO"
                async with conn.transaction(force_rollback=True):                          # down() then up() (nested: rolled back to here)
                    await m.down(q); assert not (await q(col)) and not (await q("SELECT 1 FROM pg_trigger WHERE tgname='aos_fill_ws'"))
                    await m.up(q); assert (await q(col))
            assert (await q(col)) and [r[0] for r in await q("SELECT version FROM schema_migrations WHERE version='0006'")] == ["0006"]
    with_db28(go)

def test_experiments_eval_runs_policies_require_the_workspace():
    import admin
    apply28()
    for t in ("experiments", "eval_runs"):
        assert "workspace_id = aos_ws()" in admin.RLS_CHILD[t]
        with _pg() as c:
            pol = c.execute("SELECT policyname, qual, with_check FROM pg_policies WHERE tablename=%s", (t,)).fetchall()
            assert [p["policyname"] for p in pol] == ["aos_ws"] and "workspace_id" in pol[0]["qual"] and "workspace_id" in pol[0]["with_check"]

def test_gateway_endpoint_returns_only_the_callers_workspace():
    async def go(A, admin, W):
        a, b = W["a"], W["b"]; t = W["tag"]; m = lambda x: f"m1028b-{t}-{x}"
        for model, ws, ag in ((m("a"), a["ws"], a["agent"]), (m("b"), b["ws"], b["agent"]), (m("a-noagent"), a["ws"], None),
                              (m("legacy-a"), None, a["agent"]), (m("legacy-b"), None, b["agent"]), (m("orphan"), None, None)):
            await A.q("INSERT INTO gateway_usage(agent_id,model,input_tokens,output_tokens,cost_usd,status,latency_ms,workspace_id) VALUES(%s,%s,1,1,0.01,'ok',5,%s)", (ag, model, ws))
        mine = lambda g: ({x["model"] for x in g["by_model"] if x["model"].startswith(f"m1028b-{t}-")}, {x["model"] for x in g["recent"] if x["model"].startswith(f"m1028b-{t}-")},
                          {x["agent_id"] for x in g["by_agent"] if x["agent_id"] in (a["agent"], b["agent"])})
        ga = await as_role(admin, a["ws"], "admin", A.gateway_info)
        assert mine(ga) == ({m("a"), m("a-noagent"), m("legacy-a")},) * 2 + ({a["agent"]},)        # b's rows, b's legacy row and the orphan never show
        gb = await as_role(admin, b["ws"], "viewer", A.gateway_info)                              # any role: its own workspace only
        assert mine(gb) == ({m("b"), m("legacy-b")},) * 2 + ({b["agent"]},)
        gd = await as_role(admin, a["ws"], "viewer", A.gateway_info, demo=True)                   # the demo account is narrowed to the agents it can see
        assert mine(gd)[0] == {m("a"), m("legacy-a")}
        # the provider-wide spend (caps) must still see every workspace: no RLS on gateway_usage
        tot = await A.q("SELECT count(*) n FROM gateway_usage WHERE model LIKE %s", (f"m1028b-{t}-%",), one=True)
        assert tot["n"] == 6
        await A.q("DELETE FROM gateway_usage WHERE model LIKE %s", (f"m1028b-{t}-%",))
    with_db28(go)

def test_outbox_listing_is_workspace_admin_only():
    import obs
    async def go(A, admin, W):
        a, b = W["a"], W["b"]; s = f"S1028b-{W['tag']}"
        await obs.deliver("webhook", "http://127.0.0.1:9/hook?token=SECRET", s, "{}", ws_id=a["ws"])   # private address: refused, recorded for a (target carries a token)
        try:
            rows = await as_role(admin, a["ws"], "admin", obs.outbox)
            assert any(x["subject"] == s for x in rows)
            assert not any(x["subject"] == s for x in await as_role(admin, b["ws"], "admin", obs.outbox))   # another workspace's admin: not a's rows
            for role in ("editor", "viewer", "member", ""):                                                  # every other role is refused
                with pytest.raises(HTTPException) as e: await as_role(admin, a["ws"], role, obs.outbox)
                assert e.value.status_code == 403
            with pytest.raises(HTTPException) as e: await obs.outbox()                                      # no signed-in user at all
            assert e.value.status_code == 403
            assert await as_role(admin, a["ws"], "viewer", obs.outbox, demo=True) == []                      # demo: still the empty list, never the data
        finally: await A.q("DELETE FROM outbox WHERE subject=%s", (s,))
    with_db28(go)
