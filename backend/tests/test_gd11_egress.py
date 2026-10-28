"""GĐ11.45 egress proxy: no Internet by default, allow-list only (app/egress_policy.py, app/egress_ctl.py, sandbox/egress/proxy.py, netguard purpose sandbox_egress,
migration 0053). No Docker and no real network: FakeNet / FakeDriver / MemStore, DNS monkeypatched, the proxy script is run in-process against a loopback upstream.
The real box through the real proxy container is tests/test_gd11_egress_real.py (AOS_REAL_SANDBOX=1).
Negative tests first-class: another workspace's box, wrong / expired credential, stopped or deleted box, wrong source IP, demo workspace, internal addresses (even when
AOS_EGRESS_ALLOW lists them), metadata IP, names resolving to loopback / private, decimal / octal / hex IPs, over-wide wildcards, SNI / Host mismatch, controller down."""
import asyncio, base64, importlib.util, ipaddress, json, os, socket, ssl, stat, sys, uuid
import pytest

APP = os.path.join(os.path.dirname(__file__), "..", "app")
sys.path.insert(0, APP)
os.environ.setdefault("GOOGLE_API_KEY", "local-fake-no-calls")
import egress_policy as P  # noqa: E402
import egress_ctl as E  # noqa: E402
import netguard  # noqa: E402
import sbx_runtime as R  # noqa: E402
import migrations as M  # noqa: E402

run = asyncio.run
PUB = "93.184.216.34"

@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ("AOS_EGRESS_ALLOW", "AOS_EGRESS_DENY_HOSTS", "AOS_SELF_IPS", "AOS_EGRESS_DEFAULT_CEILING", "AOS_EGRESS_POOL"): monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(netguard, "_SELF", (float("inf"), set()))

def dns(monkeypatch, table):
    calls = []
    def fake(host, port, *a, **k):
        calls.append(host); v = table.get(host)
        if v is None: raise socket.gaierror(-2, "nx")
        v = v(len(calls)) if callable(v) else v
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0, 0, 0) if ":" in ip else (ip, port or 0)) for ip in v]
    monkeypatch.setattr(socket, "getaddrinfo", fake); return calls

def ctx(ws=1, uid=10, role="editor", demo=False, scope="full"): return {"ws": ws, "uid": uid, "role": role, "demo": demo, "scope": scope}

# ------------------------------------------------------------------ pattern table (LangSmith-style examples)
def one(entry, host, port=443, is_ip=False):
    return P.matches([P.parse_entry(entry)], *( (P.normalize_host(host)[0], port) ), is_ip=is_ip) is not None

@pytest.mark.parametrize("entry,host,port,want", [
    ("pypi.org", "pypi.org", 443, True), ("pypi.org", "PyPI.org.", 80, True), ("pypi.org", "www.pypi.org", 443, False), ("pypi.org", "evilpypi.org", 443, False),
    ("pypi.org", "pypi.org", 8443, False),                                  # no port in the entry = 80 / 443 only
    ("example.com:8443", "example.com", 8443, True), ("example.com:8443", "example.com", 443, False),
    ("*.githubusercontent.com", "raw.githubusercontent.com", 443, True), ("*.githubusercontent.com", "a.b.githubusercontent.com", 443, True),
    ("*.githubusercontent.com", "githubusercontent.com", 443, False),       # a wildcard does not match the bare name
    ("*.githubusercontent.com", "evilgithubusercontent.com", 443, False), ("*.githubusercontent.com", "githubusercontent.com.evil.io", 443, False),
    ("~(raw|gist)\\.githubusercontent\\.com", "raw.githubusercontent.com", 443, True), ("~(raw|gist)\\.githubusercontent\\.com", "x.raw.githubusercontent.com", 443, False),
])
def test_name_patterns(entry, host, port, want): assert one(entry, host, port) is want

def test_ip_and_cidr_entries_match_only_literal_ips():
    assert one("203.0.113.7", "203.0.113.7", 80, True) and not one("203.0.113.7", "203.0.113.8", 80, True)
    assert one("198.51.100.0/24", "198.51.100.200", 80, True) and not one("198.51.100.0/24", "198.51.101.1", 80, True)
    assert one("[2001:db8::1]:8080", "2001:db8::1", 8080, True) and not one("2001:db8::/32", "203.0.113.7", 80, True)      # family mismatch
    assert not one("pypi.org", "203.0.113.7", 443, True)                    # names / globs never match a literal IP
    assert not P.matches([P.parse_entry("*.example.com")], "203.0.113.7", 443, True)

def test_basic_list_is_what_the_card_says():
    ok = lambda h, p=443: P.allowed("basic", [], h, p)[0]   # noqa: E731
    for h in ("pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "github.com", "raw.githubusercontent.com", "codeload.github.com", "archive.ubuntu.com",
              "vn.archive.ubuntu.com", "security.ubuntu.com"): assert ok(h), h
    for h in ("example.com", "google.com", "githubusercontent.com", "api.github.com", "evil.pypi.org"): assert not ok(h), h
    assert ok("pypi.org", 80) and not ok("pypi.org", 22) and not ok("pypi.org", 8080)
    assert P.allowed("off", [], "pypi.org", 443) == (False, "off")

def test_custom_adds_to_basic_and_basic_ignores_custom():
    custom = P.parse_list(["example.com", "*.corp-cdn.net"])
    assert P.allowed("custom", custom, "example.com", 443)[0] and P.allowed("custom", custom, "x.corp-cdn.net", 443)[0] and P.allowed("custom", custom, "pypi.org", 443)[0]
    assert not P.allowed("basic", custom, "example.com", 443)[0]            # the box's own list counts only in mode custom
    assert not P.allowed("custom", custom, "corp-cdn.net", 443)[0]

@pytest.mark.parametrize("bad", ["2130706433", "0177.0.0.1", "0x7f.0.0.1", "127.1", "0x7f000001", "1.2.3.4.5", "a b.com", "user@evil.com", "evil.com/x", "ex%61mple.com",
                                 "bücher.de", "-bad.com", "a..b.com", "", "x" * 300, "fe80::1%eth0", "010.0.0.1", "017700000001", "1.2.3.0x4"])
def test_odd_host_spellings_are_refused(bad):
    with pytest.raises(P.BadHost): P.normalize_host(bad)

def test_canonical_ip_literals_are_accepted_as_ips():
    assert P.normalize_host("203.0.113.7") == ("203.0.113.7", True) and P.normalize_host("[2001:db8::1]") == ("2001:db8::1", True)
    assert P.normalize_host("PyPI.org") == ("pypi.org", False) and P.normalize_host("127.0.0.1.") == ("127.0.0.1", True)

@pytest.mark.parametrize("bad", ["*", "*.com", "a*b.com", "~.*", "~.+", "~(a+)+$", "~" + "a" * 130, "~(", "pypi.org:99999", "pypi.org:x", "exa mple.com", "*.1.2.3.4", "", " "])
def test_dangerous_or_broken_allow_list_entries_are_rejected(bad):
    with pytest.raises(P.BadEntry): P.parse_entry(bad) if bad.strip() else P.parse_entry(bad)

def test_list_limits():
    assert P.validate_list("a.example.com, b.example.com\nc.example.com") == ["a.example.com", "b.example.com", "c.example.com"]
    with pytest.raises(P.BadEntry): P.validate_list([f"h{i}.example.com" for i in range(201)])
    assert P.validate_list(None) == [] and P.validate_list(["", "  "]) == []

# ------------------------------------------------------------------ netguard purpose sandbox_egress
def test_netguard_sandbox_egress_never_opens_internal_even_if_allow_listed(monkeypatch):
    monkeypatch.setenv("AOS_EGRESS_ALLOW", "10.0.0.0/8,127.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,169.254.0.0/16,internal.test,localhost")
    dns(monkeypatch, {"internal.test": ["10.1.2.3"], "loop.test": ["127.0.0.1"], "docker.test": ["172.17.0.1"], "sbx.test": ["172.31.251.2"], "meta.test": ["169.254.169.254"],
                      "mixed.test": [PUB, "10.0.0.5"], "v6loop.test": ["::1"], "mapped.test": ["::ffff:127.0.0.1"], "cgnat.test": ["100.64.1.1"], "pub.test": [PUB]})
    for u in ("https://10.1.2.3/", "https://127.0.0.1/", "https://172.17.0.1/", "https://172.31.251.2/", "https://192.168.1.1/", "https://169.254.169.254/", "https://[::1]/",
              "https://internal.test/", "https://loop.test/", "https://docker.test/", "https://sbx.test/", "https://meta.test/", "https://mixed.test/", "https://v6loop.test/",
              "https://mapped.test/", "https://cgnat.test/", "https://localhost/", "https://0.0.0.0/"):
        with pytest.raises(netguard.NetBlocked): netguard.check(u, "sandbox_egress")
    assert netguard.check("https://pub.test:8443/", "sandbox_egress")[0] == PUB
    assert netguard.check("https://10.1.2.3/", "webhook")[0] == "10.1.2.3"          # control: the SAME allow-list does open it for the server's own webhook calls
    assert "sandbox_egress" in netguard.PURPOSES

def test_netguard_deny_list_and_own_addresses_win(monkeypatch):
    dns(monkeypatch, {"pub.test": [PUB], "sub.bad.test": ["198.51.100.9"], "own.test": ["203.0.113.50"]})
    monkeypatch.setenv("AOS_EGRESS_DENY_HOSTS", "bad.test,203.0.113.0/24")
    for u in ("https://sub.bad.test/", "https://203.0.113.50/", "https://own.test/"):
        with pytest.raises(netguard.NetBlocked): netguard.check(u, "sandbox_egress")
    assert netguard.check("https://pub.test/", "sandbox_egress")
    monkeypatch.delenv("AOS_EGRESS_DENY_HOSTS"); monkeypatch.setattr(netguard, "_SELF", (float("inf"), {ipaddress.ip_address(PUB)}))
    with pytest.raises(netguard.NetBlocked): netguard.check("https://pub.test/", "sandbox_egress")        # a call to this very server

def test_default_config_has_no_machine_name_of_the_owner(monkeypatch):
    import re
    assert netguard._items("AOS_EGRESS_DENY_HOSTS") == []
    names = {socket.gethostname().lower(), (os.environ.get("COMPUTERNAME") or "").lower(), (os.environ.get("USERNAME") or "").lower()}
    names = {n for n in names if len(n) >= 4 and n not in ("root", "localhost")} | {"mozaai", "nguyen"}
    here = os.path.join(os.path.dirname(__file__), "..")
    for f in ("app/egress_policy.py", "app/egress_ctl.py", "sandbox/egress/proxy.py", "sandbox/egress/Dockerfile", "app/migrations/0053_sandbox_egress_log.py"):
        text = open(os.path.join(here, f), encoding="utf-8").read().lower()
        for n in names: assert not re.search(r"\b" + re.escape(n) + r"\b", text), (f, n)

# ------------------------------------------------------------------ the decision service
def ws_box(store, ws=1, mode="basic", allow=(), ip="172.31.251.2", status="running", secret="s3cret-token", box=None):
    box = box or uuid.uuid4().hex[:16]
    run(store.insert({"id": box, "name": "b", "workspace_id": ws, "owner_id": 10, "status": status, "size": "small", "image": "i", "core": "fake",
                      "host_state": {"egress": {"mode": mode, "allow_list": list(allow), "slot": 0, "net": E.net_name(box), "ip": ip, "proxy_ip": "172.31.251.1", "secret": secret}}}))
    return box

def mkctl(demo=(), ceiling="custom"):
    store, sink = R.MemStore(), E.ListSink()
    for w in (1, 2, 9): store.quotas[w] = {"enabled": True, "max_running": 3, "max_total": 10, "internet": ceiling}
    return E.EgressCtl(store, E.FakeNet(), sink, directory="/nonexistent", demo_ws=set(demo), serve=False), store, sink

def dec(ctl, box, secret="s3cret-token", src="172.31.251.2", host="pypi.org", port=443): return run(ctl.decide(box, secret, src, host, port))

def test_allowed_host_returns_pinned_ips_and_is_logged(monkeypatch):
    dns(monkeypatch, {"pypi.org": [PUB]}); ctl, st, sink = mkctl(); b = ws_box(st)
    d = dec(ctl, b); assert d["ok"] and d["ips"] == [PUB] and d["host"] == "pypi.org" and d["ip_literal"] is False
    assert sink.rows[-1] == {"workspace_id": 1, "sandbox_id": b, "host": "pypi.org", "port": 443, "verdict": "allow", "reason": "pypi.org", "mode": "basic"}

def test_unlisted_host_is_refused_and_never_resolved(monkeypatch):
    calls = dns(monkeypatch, {"example.com": [PUB], "evil.test": [PUB]}); ctl, st, sink = mkctl(); b = ws_box(st)
    for h in ("example.com", "evil.test", "a1b2c3.secretdata.attacker.test"):
        assert dec(ctl, b, host=h) == {"ok": False, "reason": "not_listed"}
    assert calls == []                                                          # DNS exfiltration through the proxy: the name is never even looked up
    assert [r["verdict"] for r in sink.rows] == ["deny"] * 3 and sink.rows[0]["reason"] == "not_listed"

def test_credential_source_status_and_workspace_checks(monkeypatch):
    dns(monkeypatch, {"pypi.org": [PUB]}); ctl, st, sink = mkctl(); a = ws_box(st, ws=1, secret="secret-A"); b = ws_box(st, ws=2, secret="secret-B", ip="172.31.251.6")
    assert dec(ctl, a, "secret-A")["ok"]
    assert dec(ctl, a, "secret-B")["reason"] == "bad_credentials"                # the credential of ANOTHER workspace's box does not open this one
    assert dec(ctl, b, "secret-A", src="172.31.251.6")["reason"] == "bad_credentials"
    assert dec(ctl, a, "")["reason"] == "bad_credentials" and dec(ctl, a, "secret-a")["reason"] == "bad_credentials"
    assert dec(ctl, "0" * 16, "secret-A")["reason"] == "bad_credentials"        # unknown box = same answer as a wrong secret (no existence oracle)
    assert dec(ctl, a, "secret-A", src="172.31.251.6")["reason"] == "bad_source"  # right secret from the wrong address (another box stole it)
    assert dec(ctl, a, "secret-A", src="127.0.0.1")["reason"] == "bad_source"
    for bad in (None, 5, "short"): assert dec(ctl, bad, "x")["reason"] == "bad_request"
    assert dec(ctl, a, "secret-A", port=0)["reason"] == "bad_request" and dec(ctl, a, "secret-A", port=70000)["reason"] == "bad_request"
    run(st.cas(a, "running", status="stopped")); ctl.forget(a)                   # expired / stopped box: the old credential is dead
    assert dec(ctl, a, "secret-A")["reason"] == "box_not_running"
    run(st.cas(b, "running", status="deleted")); ctl.forget(b)
    assert dec(ctl, b, "secret-B", src="172.31.251.6")["reason"] == "bad_credentials"
    assert {r["workspace_id"] for r in sink.rows} <= {1, 2} and all(r["sandbox_id"] in (a, b) for r in sink.rows)

def test_record_cache_expires_so_a_revoked_box_stops_within_seconds(monkeypatch):
    dns(monkeypatch, {"pypi.org": [PUB]}); ctl, st, _ = mkctl(); a = ws_box(st)
    assert dec(ctl, a)["ok"]; run(st.cas(a, "running", status="stopped"))
    assert dec(ctl, a)["ok"]                                                    # cached for REC_TTL (3 s) ...
    monkeypatch.setattr(E, "REC_TTL", 0.0); assert dec(ctl, a)["reason"] == "box_not_running"   # ... and not longer

def test_demo_workspace_is_refused_server_side(monkeypatch):
    dns(monkeypatch, {"pypi.org": [PUB]}); ctl, st, _ = mkctl(demo=[9]); a = ws_box(st, ws=9)
    assert dec(ctl, a)["reason"] == "demo_workspace"

def test_no_quota_row_means_internet_off_by_default(monkeypatch):
    dns(monkeypatch, {"pypi.org": [PUB]}); ctl, st, _ = mkctl(); a = ws_box(st); st.quotas.clear()
    assert dec(ctl, a) == {"ok": False, "reason": "internet_off"}
    monkeypatch.setenv("AOS_EGRESS_DEFAULT_CEILING", "basic"); ctl.forget(a); assert dec(ctl, a)["ok"]
    monkeypatch.setenv("AOS_EGRESS_DEFAULT_CEILING", "nonsense"); ctl.forget(a); assert dec(ctl, a)["reason"] == "internet_off"

def test_ceiling_off_basic_custom(monkeypatch):
    monkeypatch.setattr(E, "REC_TTL", 0.0)
    dns(monkeypatch, {"pypi.org": [PUB], "example.com": [PUB]}); ctl, st, _ = mkctl(); a = ws_box(st, mode="custom", allow=["example.com"])
    for ceiling, pypi, example in (("off", False, False), ("basic", True, False), ("custom", True, True)):
        st.quotas[1] = {"enabled": True, "max_running": 3, "max_total": 10, "internet": ceiling}
        assert dec(ctl, a, host="pypi.org")["ok"] is pypi and dec(ctl, a, host="example.com")["ok"] is example, ceiling
    st.quotas[1]["internet"] = "custom"; b = ws_box(st, mode="basic")           # a box asking for less than the ceiling gets less
    assert dec(ctl, b, "s3cret-token", host="example.com")["reason"] == "not_listed"

def test_internal_and_special_destinations_are_blocked_even_when_listed(monkeypatch):
    monkeypatch.setenv("AOS_EGRESS_ALLOW", "10.0.0.0/8,127.0.0.0/8,172.16.0.0/12,169.254.0.0/16,192.168.0.0/16,loop.test")
    dns(monkeypatch, {"loop.test": ["127.0.0.1"], "priv.test": ["10.2.3.4"], "meta.test": ["169.254.169.254"], "pub.test": [PUB], "dock.test": ["172.17.0.1"], "pool.test": ["172.31.251.7"]})
    monkeypatch.setattr(E, "REC_TTL", 0.0)
    ctl, st, sink = mkctl()
    a = ws_box(st, mode="custom", allow=["loop.test", "priv.test", "meta.test", "dock.test", "pool.test", "169.254.169.254", "10.0.0.0/8", "127.0.0.1", "172.17.0.1", "192.168.1.1:80",
                                          "172.31.251.7", "pub.test"])
    for host, port in (("loop.test", 443), ("priv.test", 443), ("meta.test", 443), ("dock.test", 443), ("pool.test", 443), ("169.254.169.254", 80), ("10.1.1.1", 80),
                       ("127.0.0.1", 80), ("172.17.0.1", 80), ("192.168.1.1", 80), ("172.31.251.7", 80), ("localhost", 80), ("[::1]", 80), ("0.0.0.0", 80)):
        d = dec(ctl, a, host=host, port=port); assert d["ok"] is False and d["reason"] in ("blocked_address", "not_listed", "bad_host"), (host, d)
    assert dec(ctl, a, host="pub.test", port=443)["ok"]                         # a listed public name still works: the blocks above are not a blanket refusal

def test_decimal_octal_hex_and_short_ips_are_bad_hosts(monkeypatch):
    monkeypatch.setattr(E, "REC_TTL", 0.0); calls = dns(monkeypatch, {})
    ctl, st, _ = mkctl()
    a = ws_box(st, mode="custom", allow=["127.0.0.1"])
    for h in ("2130706433", "0177.0.0.1", "0x7f.0.0.1", "0x7f000001", "127.1", "017700000001"):
        assert dec(ctl, a, host=h, port=80)["reason"] == "bad_host", h
    assert calls == []

def test_bare_ip_https_is_dropped_listed_public_ip_http_passes_special_ranges_do_not(monkeypatch):
    monkeypatch.setattr(E, "REC_TTL", 0.0); dns(monkeypatch, {})
    ctl, st, _ = mkctl(); a = ws_box(st, mode="custom", allow=["8.8.8.8", "198.51.100.0/24"])
    assert dec(ctl, a, host="8.8.8.8", port=443)["reason"] == "ip_https_dropped"
    d = dec(ctl, a, host="8.8.8.8", port=80); assert d["ok"] and d["ips"] == ["8.8.8.8"] and d["ip_literal"] is True
    assert dec(ctl, a, host="198.51.100.4", port=80)["reason"] == "blocked_address"      # TEST-NET-2 is special-purpose: netguard refuses it although the box list has it
    assert dec(ctl, a, host="8.8.4.4", port=80)["reason"] == "not_listed"

def test_dns_rebinding_the_pinned_answer_is_the_one_that_was_checked(monkeypatch):
    calls = dns(monkeypatch, {"pypi.org": lambda n: [PUB] if n == 1 else ["127.0.0.1"]}); ctl, st, _ = mkctl()
    a = ws_box(st); d = dec(ctl, a)
    assert d["ok"] and d["ips"] == [PUB] and len(calls) == 1                     # ONE lookup; the proxy connects to this address, it never resolves again

def test_one_internal_answer_among_public_ones_blocks_the_name(monkeypatch):
    dns(monkeypatch, {"pypi.org": [PUB, "10.0.0.9"]}); ctl, st, _ = mkctl()
    assert dec(ctl, ws_box(st))["reason"] == "blocked_address"

def test_log_rows_never_carry_secrets_or_paths(monkeypatch):
    dns(monkeypatch, {"pypi.org": [PUB]}); ctl, st, sink = mkctl()
    a = ws_box(st, secret="top-secret-value"); dec(ctl, a, "top-secret-value"); dec(ctl, a, "top-secret-value", host="example.com"); dec(ctl, a, "wrong-guess")
    blob = json.dumps(sink.rows)
    assert "top-secret-value" not in blob and "wrong-guess" not in blob and "/" not in blob.replace("\\/", "")
    assert set(sink.rows[0]) == {"workspace_id", "sandbox_id", "host", "port", "verdict", "reason", "mode"}

# ------------------------------------------------------------------ Unix socket server
def test_control_socket_is_private_and_speaks_only_decide(tmp_path, monkeypatch):
    dns(monkeypatch, {"pypi.org": [PUB]}); d = str(tmp_path / "egress"); store, sink = R.MemStore(), E.ListSink()
    ctl = E.EgressCtl(store, E.FakeNet(), sink, directory=d, demo_ws=set()); store.quotas[1] = {"enabled": True, "max_running": 3, "max_total": 10, "internet": "basic"}
    a = ws_box(store)
    async def ask(raw):
        r, w = await asyncio.open_unix_connection(os.path.join(d, "ctl.sock")); w.write(raw); await w.drain()
        line = await asyncio.wait_for(r.readline(), 5); w.close(); return json.loads(line)
    async def go():
        await ctl.start_server()
        try:
            assert stat.S_IMODE(os.stat(d).st_mode) == 0o700 and stat.S_IMODE(os.stat(os.path.join(d, "ctl.sock")).st_mode) == 0o600
            q = {"op": "decide", "box": a, "secret": "s3cret-token", "src": "172.31.251.2", "host": "pypi.org", "port": 443}
            assert (await ask(json.dumps(q).encode() + b"\n"))["ips"] == [PUB]
            assert (await ask(json.dumps({**q, "secret": "nope"}).encode() + b"\n"))["reason"] == "bad_credentials"
            assert (await ask(json.dumps({**q, "op": "dump"}).encode() + b"\n"))["reason"] == "bad_request"
            assert (await ask(b"not json\n"))["reason"] == "bad_request"
            assert (await ask(b"[1,2,3]\n"))["reason"] == "bad_request"
            assert (await ask(b"x" * 10000 + b"\n"))["reason"] == "bad_request"
        finally: await ctl.stop_server()
        assert not os.path.exists(os.path.join(d, "ctl.sock"))
    if os.name == "nt": pytest.skip("unix sockets")
    run(go())

def test_ctl_dir_refuses_symlinks_and_foreign_modes(tmp_path, monkeypatch):
    if os.name == "nt": pytest.skip("posix only")
    real = tmp_path / "real"; real.mkdir(); link = tmp_path / "link"; link.symlink_to(real)
    monkeypatch.setenv("AOS_EGRESS_CTL_DIR", str(link))
    d = E.ctl_dir(); assert d != str(link) and stat.S_IMODE(os.stat(d).st_mode) == 0o700    # a symlinked directory is skipped for the next candidate
    ok = tmp_path / "ok"; monkeypatch.setenv("AOS_EGRESS_CTL_DIR", str(ok)); assert E.ctl_dir() == str(ok) and stat.S_IMODE(os.stat(ok).st_mode) == 0o700

# ------------------------------------------------------------------ Computers integration (FakeDriver + FakeNet)
def mkc(ceiling="custom"):
    store, drv, net, sink = R.MemStore(), R.FakeDriver(), E.FakeNet(), E.ListSink()
    ctl = E.EgressCtl(store, net, sink, directory="/nonexistent", demo_ws=set(), serve=False)
    store.quotas[1] = {"enabled": True, "max_running": 3, "max_total": 10, "internet": ceiling}
    return R.Computers(store, drv, disk_probe=lambda: 10, egress=ctl), drv, store, net, ctl

def test_box_without_internet_keeps_network_none():
    c, drv, st, net, _ = mkc()
    r = run(c.create(ctx())); a = drv.boxes[r["id"]]["args"]
    assert a[a.index("--network") + 1] == "none" and "--dns" not in a and net.nets == {} and "egress" not in (r["host_state"] or {})
    assert run(c.exec(ctx(), r["id"], "env HTTPS_PROXY"))["output"].strip() == ""

def test_internet_box_gets_private_internal_network_fixed_ip_and_dead_dns():
    c, drv, st, net, _ = mkc()
    a1, a2 = run(c.create(ctx(), internet="basic")), run(c.create(ctx(), internet="custom", allow_list=["example.com"]))
    for r in (a1, a2):
        args = drv.boxes[r["id"]]["args"]; eg = r["host_state"]["egress"]
        assert args[args.index("--network") + 1] == E.net_name(r["id"]) == eg["net"] and args[args.index("--ip") + 1] == eg["ip"] and args[args.index("--dns") + 1] == "192.0.2.1"
        assert ipaddress.ip_address(eg["ip"]) in E.pool() and ipaddress.ip_address(eg["proxy_ip"]) in E.pool() and len(eg["secret"]) >= 30
        assert "--privileged" not in args and "host" not in args[args.index("--network") + 1]
        assert eg["net"] in net.attached
    e1, e2 = a1["host_state"]["egress"], a2["host_state"]["egress"]
    assert e1["slot"] != e2["slot"] and e1["ip"] != e2["ip"] and e1["net"] != e2["net"] and e1["secret"] != e2["secret"]      # one network per box: no box-to-box path
    assert ipaddress.ip_network(f"{e1['ip']}/30", strict=False).network_address == ipaddress.ip_network(f"{e1['proxy_ip']}/30", strict=False).network_address
    assert e1["allow_list"] == [] and e2["allow_list"] == ["example.com"] and e2["mode"] == "custom"

def test_proxy_variables_are_added_per_command_but_not_stored_in_history():
    c, drv, st, net, _ = mkc(); r = run(c.create(ctx(), internet="basic")); eg = r["host_state"]["egress"]
    out = run(c.exec(ctx(), r["id"], "env HTTPS_PROXY"))["output"].strip()
    assert out == f"http://box-{r['id']}:{eg['secret']}@{eg['proxy_ip']}:3128"
    assert run(c.exec(ctx(), r["id"], "env http_proxy"))["output"].strip() == out and run(c.exec(ctx(), r["id"], "env NO_PROXY"))["output"].strip() == ""
    assert all(eg["secret"] not in json.dumps(x) for x in st.cmds)
    assert run(c.exec(ctx(), r["id"], "env HTTPS_PROXY", env={"HTTPS_PROXY": "http://mine"}))["output"].strip() == "http://mine"   # harmless: the network, not the variable, is the guard

def test_requesting_more_than_the_workspace_ceiling_is_refused_and_leaves_nothing_behind():
    for ceiling, asked in (("off", "basic"), ("basic", "custom"), ("off", "custom")):
        c, drv, st, net, _ = mkc(ceiling)
        with pytest.raises(R.Forbidden): run(c.create(ctx(), internet=asked, allow_list=["example.com"]))
        assert net.nets == {} and drv.boxes == {} and st.rows == {}
    c, *_ = mkc("basic"); assert run(c.create(ctx(), internet="basic"))["status"] == "running"
    c, drv, st, net, _ = mkc("custom"); del st.quotas[1]                        # no quota row -> default ceiling 'off'
    with pytest.raises(R.Forbidden): run(c.create(ctx(), internet="basic"))

def test_bad_internet_requests_are_rejected_before_anything_is_created():
    c, drv, st, net, _ = mkc()
    for kw in ({"internet": "all"}, {"internet": "custom", "allow_list": ["*"]}, {"internet": "custom", "allow_list": ["~.*"]}, {"internet": "custom", "allow_list": ["a b"]},
               {"internet": "custom", "allow_list": [f"h{i}.example.com" for i in range(300)]}, {"internet": True}, {"internet": None}):
        with pytest.raises(R.SbxError): run(c.create(ctx(), **kw))
    assert net.nets == {} and drv.boxes == {} and st.rows == {}

def test_failed_create_releases_the_network():
    c, drv, st, net, _ = mkc(); drv.fail["create"] = True
    with pytest.raises(R.BoxFailed): run(c.create(ctx(), internet="basic"))
    assert net.nets == {} and net.attached == set()
    c, drv, st, net, _ = mkc(); net.fail = True
    with pytest.raises(R.Capacity): run(c.create(ctx(), internet="basic"))
    assert drv.boxes == {} and st.rows == {}                                    # no network = no box (never a box that silently has no proxy path)

def test_delete_releases_network_and_credential(monkeypatch):
    dns(monkeypatch, {"pypi.org": [PUB]}); c, drv, st, net, ctl = mkc(); r = run(c.create(ctx(), internet="basic")); eg = r["host_state"]["egress"]
    st.quotas[1]["internet"] = "basic"
    assert run(ctl.decide(r["id"], eg["secret"], eg["ip"], "pypi.org", 443))["ok"]
    run(c.delete(ctx(), r["id"]))
    assert net.nets == {} and eg["net"] not in net.attached
    assert run(ctl.decide(r["id"], eg["secret"], eg["ip"], "pypi.org", 443))["reason"] == "bad_credentials"

def test_other_workspace_and_other_user_cannot_use_the_box_or_its_secret():
    c, drv, st, net, _ = mkc(); r = run(c.create(ctx(ws=1, uid=10), internet="basic"))
    for who in (ctx(ws=2, uid=10), ctx(ws=1, uid=11), ctx(ws=1, uid=11, role="viewer")):
        with pytest.raises(R.NotFound): run(c.exec(who, r["id"], "env HTTPS_PROXY"))
    with pytest.raises(R.Forbidden): run(c.create(ctx(demo=True), internet="basic"))
    with pytest.raises(R.Forbidden): run(c.create(ctx(role="viewer"), internet="basic"))
    with pytest.raises(R.Forbidden): run(c.create(ctx(scope="read"), internet="basic"))

def test_slots_run_out_cleanly(monkeypatch):
    monkeypatch.setenv("AOS_EGRESS_POOL", "172.31.251.0/29")                    # two /30 slots
    c, drv, st, net, _ = mkc(); st.quotas[1]["max_running"] = 9
    run(c.create(ctx(), internet="basic")); run(c.create(ctx(), internet="basic"))
    with pytest.raises(R.Capacity) as e: run(c.create(ctx(), internet="basic"))
    assert e.value.retry_after

def test_box_net_args_reject_bad_descriptions():
    good = {"net": E.net_name("a" * 16), "ip": "172.31.251.2"}
    assert E.box_net_args(good)[:2] == ["--network", good["net"]]
    for bad in ({**good, "net": "host"}, {**good, "net": "bridge"}, {**good, "net": "aos-sbx-egress-../x"}, {**good, "ip": "10.0.0.5"}, {**good, "ip": "not-an-ip"},
                {**good, "ip": "::1"}, {"net": good["net"]}, None, "host"):
        with pytest.raises(E.EgressError): E.box_net_args(bad)

def test_migration_0053_files_and_registration():
    assert "0053" in [v for v, _, _ in M._load()]
    assert '"sandbox_egress_log"' in open(os.path.join(APP, "admin.py"), encoding="utf-8").read()      # row-level security by workspace (policy created by admin.rls_init)
    src = open(os.path.join(APP, "migrations", "0053_sandbox_egress_log.py"), encoding="utf-8").read()
    assert "workspace_id INT NOT NULL" in src and "CHECK (verdict IN ('allow','deny'))" in src

# ------------------------------------------------------------------ migration 0053 + PgSink on a throw-away schema
@pytest.fixture
def pg(tmp_path, monkeypatch):
    import shutil, psycopg
    PG = os.environ.get("PG_URI", "postgresql://aos:aoslocal@127.0.0.1:5432/agentos")
    shutil.copy(os.path.join(APP, "migrations", "0053_sandbox_egress_log.py"), tmp_path / "0053_sandbox_egress_log.py")
    monkeypatch.setattr(M, "DIR", tmp_path); sch = "t1145_" + uuid.uuid4().hex[:8]
    def runner(fn):
        async def go():
            async with await psycopg.AsyncConnection.connect(PG, autocommit=True, options=f"-csearch_path={sch}") as c: return await fn(c)
        return asyncio.run(go())
    with psycopg.connect(PG, autocommit=True) as c: c.execute(f'CREATE SCHEMA "{sch}"')
    yield runner
    with psycopg.connect(PG, autocommit=True) as c: c.execute(f'DROP SCHEMA "{sch}" CASCADE')

def test_migration_0053_roundtrip_constraints_sink_and_prune(pg):
    import psycopg
    from psycopg.rows import dict_row
    async def dry(c): return await M.run_pending(c, dry_run=True)
    assert len(pg(dry)) == 1
    async def up(c): return await M.run_pending(c)
    assert pg(up) == ["0053 sandbox_egress_log: applied"]
    async def work(c):
        async def q(sql, args=(), one=False):
            cur = c.cursor(row_factory=dict_row); await cur.execute(sql, args)
            if cur.description is None: return None
            return await (cur.fetchone() if one else cur.fetchall())
        sink = E.PgSink(q); sink.add({"workspace_id": 3, "sandbox_id": "a" * 16, "host": "pypi.org", "port": 443, "verdict": "allow", "reason": "pypi.org", "mode": "basic"})
        sink.add({"workspace_id": 4, "sandbox_id": None, "host": "x" * 400, "port": 80, "verdict": "deny", "reason": "not_listed", "mode": "basic"}); await sink.flush()
        rows = await q("SELECT workspace_id,host,verdict FROM sandbox_egress_log ORDER BY id"); assert [r["workspace_id"] for r in rows] == [3, 4] and len(rows[1]["host"]) == 253
        with pytest.raises(psycopg.errors.CheckViolation): await c.execute("INSERT INTO sandbox_egress_log(workspace_id,host,verdict,reason) VALUES(1,'h','maybe','r')")
        with pytest.raises(psycopg.errors.NotNullViolation): await c.execute("INSERT INTO sandbox_egress_log(host,verdict,reason) VALUES('h','allow','r')")      # workspace_id is mandatory (RLS key)
        await c.execute("UPDATE sandbox_egress_log SET ts = now() - interval '31 days' WHERE workspace_id=3"); await sink.prune()
        assert [r["workspace_id"] for r in await q("SELECT workspace_id FROM sandbox_egress_log")] == [4]                                        # 30-day prune
    pg(work)
    async def down(c): return await M.rollback(c, 1)
    assert pg(down) == ["0053 sandbox_egress_log: rolled back"]
    async def gone(c): return (await (await c.execute("SELECT to_regclass('sandbox_egress_log')")).fetchone())[0]
    assert pg(gone) is None

# ------------------------------------------------------------------ the proxy script, in-process against a loopback upstream
def load_proxy():
    spec = importlib.util.spec_from_file_location("aos_egress_proxy", os.path.join(os.path.dirname(__file__), "..", "sandbox", "egress", "proxy.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def client_hello(name):
    inc, out = ssl.MemoryBIO(), ssl.MemoryBIO(); o = ssl.create_default_context().wrap_bio(inc, out, server_hostname=name)
    try: o.do_handshake()
    except ssl.SSLWantReadError: pass
    return out.read()

def basic(box, secret): return "Proxy-Authorization: Basic " + base64.b64encode(f"box-{box}:{secret}".encode()).decode() + "\r\n"

class Rig:
    """controller (real EgressCtl on a unix socket) + proxy.handle on 127.0.0.1 + an upstream that answers 'pong' to anything. netguard is replaced by 'pin to loopback' so the
    proxy mechanics (auth, SNI / Host check, plain HTTP rewriting) can be exercised offline; netguard itself is tested above."""
    def __init__(self, tmp, monkeypatch, allow=("pypi.org",)):
        self.proxy = load_proxy(); self.tmp = tmp; self.seen = []
        self.store, self.sink = R.MemStore(), E.ListSink()
        self.ctl = E.EgressCtl(self.store, E.FakeNet(), self.sink, directory=str(tmp / "ctl"), demo_ws=set())
        self.store.quotas[1] = {"enabled": True, "max_running": 3, "max_total": 10, "internet": "custom"}
        self.box = ws_box(self.store, mode="custom", ip="127.0.0.1", allow=allow)
        monkeypatch.setattr(E.netguard, "check_all", lambda url, purpose="webhook", https_only=False: (["127.0.0.1"], "x"))
    async def __aenter__(self):
        await self.ctl.start_server(); self.proxy.CTL = self.ctl.sock_path()
        async def up(r, w):
            data = await r.read(65536)
            if data: self.seen.append(data); w.write(b"pong"); await w.drain()
            w.close()
        self.up = await asyncio.start_server(up, "127.0.0.1", 0); self.up_port = self.up.sockets[0].getsockname()[1]
        self.srv = await asyncio.start_server(self.proxy.handle, "127.0.0.1", 0, limit=self.proxy.MAX_HEAD); self.port = self.srv.sockets[0].getsockname()[1]
        return self
    async def __aexit__(self, *a):
        self.srv.close(); self.up.close(); await self.ctl.stop_server()
    async def talk(self, first, then=None, read=65536):
        r, w = await asyncio.open_connection("127.0.0.1", self.port); w.write(first); await w.drain()
        head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 10); body = b""
        if then is not None:
            w.write(then); await w.drain()
        try: body = await asyncio.wait_for(r.read(read), 5)
        except asyncio.TimeoutError: pass
        w.close(); return head, body

def connect_req(rig, host, port=None, creds=True, secret="s3cret-token"):
    port = port or rig.up_port
    return (f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n" + (basic(rig.box, secret) if creds else "") + "\r\n").encode()

def rig_test(fn):
    def wrapper(tmp_path, monkeypatch):
        if os.name == "nt": pytest.skip("unix sockets")
        rig = Rig(tmp_path, monkeypatch)
        async def go():
            async with rig:
                row = await rig.store.get(rig.box); eg = row["host_state"]["egress"]; eg["allow_list"] = [f"pypi.org:{rig.up_port}", f"plain.example:{rig.up_port}"]
                rig.ctl.forget(rig.box)
                await fn(rig)
        run(go())
    wrapper.__name__ = fn.__name__; return wrapper

@rig_test
async def test_proxy_tunnels_tls_with_matching_sni(rig):
    head, body = await rig.talk(connect_req(rig, "pypi.org"), client_hello("pypi.org"))
    assert head.startswith(b"HTTP/1.1 200") and body == b"pong" and len(rig.seen) == 1 and rig.proxy.parse_sni(rig.seen[0]) == "pypi.org"
    assert [r["verdict"] for r in rig.sink.rows] == ["allow"]

@rig_test
async def test_proxy_drops_a_tunnel_whose_sni_names_another_host(rig):
    head, body = await rig.talk(connect_req(rig, "pypi.org"), client_hello("evil.example"))
    assert head.startswith(b"HTTP/1.1 200") and body == b"" and rig.seen == []                 # nothing reached the upstream
    head, body = await rig.talk(connect_req(rig, "pypi.org"), b"\x16\x03\x01\x00\x05\x01\x00\x00\x01\x00")      # TLS-looking junk without SNI
    assert body == b"" and rig.seen == []

@rig_test
async def test_proxy_drops_plain_http_with_wrong_host_and_unknown_protocols(rig):
    ok = f"GET / HTTP/1.1\r\nHost: pypi.org:{rig.up_port}\r\n\r\n".encode(); bad = f"GET / HTTP/1.1\r\nHost: evil.example\r\n\r\n".encode()
    head, body = await rig.talk(connect_req(rig, "pypi.org"), ok); assert body == b"pong" and rig.seen[-1] == ok
    n = len(rig.seen)
    head, body = await rig.talk(connect_req(rig, "pypi.org"), bad); assert body == b"" and len(rig.seen) == n
    head, body = await rig.talk(connect_req(rig, "pypi.org"), b"SSH-2.0-OpenSSH_9.6\r\n\r\n"); assert body == b"" and len(rig.seen) == n      # not TLS, no Host
    head, body = await rig.talk(connect_req(rig, "pypi.org"), b"\x00\x01\x02\x03"); assert body == b"" and len(rig.seen) == n

@rig_test
async def test_proxy_refuses_without_or_with_wrong_credentials(rig):
    head, _ = await rig.talk(connect_req(rig, "pypi.org", creds=False)); assert head.startswith(b"HTTP/1.1 407") and b"Proxy-Authenticate: Basic" in head
    head, _ = await rig.talk(connect_req(rig, "pypi.org", secret="wrong")); assert head.startswith(b"HTTP/1.1 403")
    bogus = f"CONNECT pypi.org:{rig.up_port} HTTP/1.1\r\nProxy-Authorization: Basic " + base64.b64encode(b"box-../../etc:x").decode() + "\r\n\r\n"
    head, _ = await rig.talk(bogus.encode()); assert head.startswith(b"HTTP/1.1 407")
    head, _ = await rig.talk(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n"); assert head.startswith(b"HTTP/1.1 407")
    assert rig.seen == []

@rig_test
async def test_proxy_refuses_unlisted_hosts_ports_and_bad_targets(rig):
    for req in (connect_req(rig, "example.com"), connect_req(rig, "pypi.org", port=22), connect_req(rig, "169.254.169.254", port=80), connect_req(rig, "127.0.0.1"),
                connect_req(rig, "2130706433"), connect_req(rig, "0x7f.1")):
        head, _ = await rig.talk(req); assert head.startswith(b"HTTP/1.1 403"), req
    for raw in (b"CONNECT nonsense HTTP/1.1\r\n" + basic(rig.box, "s3cret-token").encode() + b"\r\n", b"CONNECT a@b:80 HTTP/1.1\r\n" + basic(rig.box, "s3cret-token").encode() + b"\r\n",
                b"FTP ftp://x/ HTTP/1.1\r\n" + basic(rig.box, "s3cret-token").encode() + b"\r\n", b"GET https://pypi.org/ HTTP/1.1\r\n" + basic(rig.box, "s3cret-token").encode() + b"\r\n"):
        head, _ = await rig.talk(raw); assert head.startswith(b"HTTP/1.1 4"), raw
    assert rig.seen == []

@rig_test
async def test_proxy_forwards_plain_http_in_origin_form_without_proxy_credentials(rig):
    req = (f"GET http://plain.example:{rig.up_port}/simple/x?y=1 HTTP/1.1\r\nHost: evil.example\r\nUser-Agent: pip\r\n" + basic(rig.box, "s3cret-token") +
           "Proxy-Connection: keep-alive\r\nAccept: */*\r\n\r\n").encode()
    r, w = await asyncio.open_connection("127.0.0.1", rig.port); w.write(req); await w.drain()
    body = await asyncio.wait_for(r.read(100), 5); w.close()
    assert body == b"pong"
    sent = rig.seen[0].decode()
    assert sent.startswith("GET /simple/x?y=1 HTTP/1.1\r\n") and f"Host: plain.example:{rig.up_port}" in sent and "evil.example" not in sent     # Host comes from the URL, not from the client
    assert "Proxy-" not in sent and "Basic" not in sent and "Connection: close" in sent and "User-Agent: pip" in sent
    r, w = await asyncio.open_connection("127.0.0.1", rig.port); w.write(req.replace(b"plain.example", b"other.example")); await w.drain()
    assert (await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 5)).startswith(b"HTTP/1.1 403"); w.close()

def test_proxy_fails_closed_when_the_controller_is_down(tmp_path):
    if os.name == "nt": pytest.skip("unix sockets")
    proxy = load_proxy(); proxy.CTL = str(tmp_path / "missing.sock")
    async def go():
        srv = await asyncio.start_server(proxy.handle, "127.0.0.1", 0); port = srv.sockets[0].getsockname()[1]
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(("CONNECT pypi.org:443 HTTP/1.1\r\n" + basic("a" * 16, "x") + "\r\n").encode()); await w.drain()
        head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 10); w.close(); srv.close(); return head
    assert run(go()).startswith(b"HTTP/1.1 503")
@rig_test
async def test_proxy_per_box_connection_cap(rig):
    rig.proxy.MAX_PER_BOX = 1
    r1, w1 = await asyncio.open_connection("127.0.0.1", rig.port); w1.write(connect_req(rig, "pypi.org")); await w1.drain()
    assert (await asyncio.wait_for(r1.readuntil(b"\r\n\r\n"), 5)).startswith(b"HTTP/1.1 200")        # first tunnel stays open
    head, _ = await rig.talk(connect_req(rig, "pypi.org")); assert head.startswith(b"HTTP/1.1 429")
    w1.close(); await asyncio.sleep(0.3)
    head, body = await rig.talk(connect_req(rig, "pypi.org"), client_hello("pypi.org")); assert head.startswith(b"HTTP/1.1 200") and body == b"pong"     # slot freed

def test_proxy_helpers():
    proxy = load_proxy()
    assert proxy.parse_sni(client_hello("Pypi.ORG")) == "pypi.org" and proxy.parse_sni(b"GET / HTTP/1.1") is None and proxy.parse_sni(client_hello("a.b")[:20]) is None
    assert proxy.split_authority("[2001:db8::1]:443", None) == ("2001:db8::1", 443) and proxy.split_authority("a@b:1", None) is None and proxy.split_authority("a:70000", None) is None
    assert proxy.split_authority("a.b", None) is None and proxy.split_authority("a.b", 80) == ("a.b", 80)
