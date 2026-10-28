"""GĐ10.2 netguard — ONE egress policy for every server-side call to a user/admin-supplied URL (SSRF). Design: GD10_THIET_KE §10.2, NEN_TANG_CHUNG F5.

 * check(url, purpose) -> (ip, host) or raise NetBlocked(reason): resolve ONCE, test EVERY returned address (IPv4-mapped / 6to4 / NAT64 / Teredo are unwrapped and
   the embedded IPv4 is tested too). Never reachable, even if allow-listed: 0.0.0.0/8, 169.254/16 (cloud metadata), link-local, multicast, reserved.
   Internal ranges (loopback, 10/8, 172.16/12, 192.168/16, 100.64/10, ULA, doc/benchmark ...) only when listed in env AOS_EGRESS_ALLOW (CIDRs or exact host names).
   Deny list WINS over the allow list: env AOS_EGRESS_DENY_HOSTS (host names [+ their subdomains], IPs, CIDRs), env AOS_SELF_IPS + this machine's own non-loopback
   interface addresses (a call to ourselves is a loop-back into our own services).
 * client(purpose) = httpx.AsyncClient whose transport re-checks EVERY request (so every redirect hop too) and whose network backend connects ONLY to the addresses
   that check returned (a 2nd DNS lookup can never swap the target = no DNS rebinding); the original host stays in Host / SNI / certificate verification.
   purpose "webhook" (secret-carrying calls: webhooks, OTLP, channel APIs): https required (http only to allow-listed addresses), a 3xx is an error, not followed.
   purpose "fetch" (public pages, files): http ok for public hosts, <= 5 redirects, each hop re-checked.
   purpose "model_provider" (GĐ11.1: LLM provider base_url + API key): behaves like webhook (https, no redirect, internal only via AOS_EGRESS_ALLOW).
   purpose "mcp" (GĐ11.23: MCP server URL, connect + every call): behaves like webhook.
   purpose "oauth" (GĐ11.25: token / revocation / userinfo / discovery endpoints and provider API calls that carry a client secret or a user's token): behaves like webhook.
   purpose "public" (git import: "public repositories only"): like webhook but AOS_EGRESS_ALLOW is ignored, so an internal address is never reachable.
   purpose "sandbox_egress" (GĐ11.45: every CONNECT / request of a virtual computer through the egress proxy): like "public" - AOS_EGRESS_ALLOW is ignored, so a box can
   NEVER reach an internal address (Odoo, Home Assistant, Postgres, the host, other boxes, the Docker ranges), even when an operator listed it for the server's own calls.
 * Every block is logged (purpose + host + reason; never the URL path/query/token). Env is read at call time (tests / ops can change it without a restart)."""
import asyncio, contextvars, ipaddress as ip_, logging, os, socket, time
import httpcore, httpx

log = logging.getLogger("aos.netguard")
PURPOSES = ("webhook", "fetch", "public", "model_provider", "mcp", "sandbox_egress", "oauth")
_N = lambda *a: tuple(ip_.ip_network(x) for x in a)
NEVER = _N("0.0.0.0/8", "169.254.0.0/16", "224.0.0.0/4", "240.0.0.0/4", "::/128", "fe80::/10", "ff00::/8")   # not even AOS_EGRESS_ALLOW can open these
INTERNAL = _N("10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15",
              "198.51.100.0/24", "203.0.113.0/24", "::/96", "100::/64", "2001::/32", "2001:10::/28", "2001:db8::/32", "2002::/16", "3fff::/20", "64:ff9b:1::/48", "fc00::/7")
_MAPPED, _NAT64 = ip_.ip_network("::ffff:0:0/96"), ip_.ip_network("64:ff9b::/96")

class NetBlocked(httpx.TransportError):
    """Egress refused. `.reason` is a short text with no URL/token in it. A TransportError so callers that already catch httpx.HTTPError fail closed."""
    def __init__(self, reason): super().__init__(reason); self.reason = reason

def _block(purpose, host, reason):
    log.warning("netguard blocked purpose=%s host=%s reason=%s", purpose, host, reason)
    return NetBlocked(reason)

def _items(name): return [x.strip().lower() for x in (os.environ.get(name) or "").replace(";", ",").replace(" ", ",").split(",") if x.strip()]
def _split(items):
    nets, names = [], []
    for x in items:
        try: nets.append(ip_.ip_network(x, strict=False))
        except ValueError: names.append(x.strip("."))
    return nets, names

_SELF: tuple = (0.0, set())
def _self_ips():
    """AOS_SELF_IPS + non-loopback addresses of this machine (cached 5 min)."""
    global _SELF
    if time.monotonic() < _SELF[0]: return _SELF[1]
    found = set()
    try: found |= {a[4][0].split("%")[0] for a in socket.getaddrinfo(socket.gethostname(), None)}
    except OSError: pass
    for fam, probe in ((socket.AF_INET, "192.0.2.1"), (socket.AF_INET6, "2001:db8::1")):   # UDP connect sends nothing: it only picks the outgoing interface
        try:
            with socket.socket(fam, socket.SOCK_DGRAM) as s: s.connect((probe, 9)); found.add(s.getsockname()[0].split("%")[0])
        except OSError: pass
    ok = set()
    for x in found:
        try:
            a = ip_.ip_address(x)
            if not (a.is_loopback or a.is_unspecified): ok.add(a)
        except ValueError: pass
    _SELF = (time.monotonic() + 300, ok); return ok

def _addrs(a):
    """The addresses that decide the verdict: a wrapped IPv4 counts instead of its IPv6 wrapper; tunnel prefixes also keep the wrapper (it is INTERNAL)."""
    if a.version == 4: return [a]
    if a in _MAPPED: return [a.ipv4_mapped]
    if a in _NAT64: return [ip_.IPv4Address(int(a) & 0xFFFFFFFF)]
    out = [a]
    if a.sixtofour: out.append(a.sixtofour)
    if a.teredo: out += list(a.teredo)
    if 1 < int(a) < 1 << 32: out.append(ip_.IPv4Address(int(a) & 0xFFFFFFFF))   # IPv4-compatible ::a.b.c.d
    return out

def _kind(a):
    if any(a in n for n in NEVER): return "never"
    return "internal" if any(a in n for n in INTERNAL) or not a.is_global else "public"

def _parse(url):
    try: u = httpx.URL(str(url or "").strip())
    except (httpx.InvalidURL, ValueError): raise NetBlocked("invalid URL") from None
    host = (u.host or "").lower().strip(".")
    if u.scheme not in ("http", "https") or not host: raise NetBlocked("only http/https URLs with a host are allowed")
    return u, host

def _resolve(host, port):
    try: return [ip_.ip_address(host)]       # literal IP (also the bracketed [::1] form: httpx strips the brackets)
    except ValueError: pass
    try: infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError): raise NetBlocked("host does not resolve") from None
    out = []
    for *_, sa in infos:
        try: x = ip_.ip_address(sa[0].split("%")[0])
        except ValueError: raise NetBlocked("unparseable address") from None
        if x not in out: out.append(x)
    if not out: raise NetBlocked("host does not resolve")
    return out

def _eval(url, purpose, https_only=False):
    """-> ([ips], host, internal: bool). Raises NetBlocked (not logged: the public wrappers log)."""
    if purpose not in PURPOSES: raise ValueError("unknown netguard purpose " + repr(purpose))
    u, host = _parse(url)
    ips = _resolve(host, u.port or (443 if u.scheme == "https" else 80))
    anets, anames = _split(_items("AOS_EGRESS_ALLOW")); dnets, dnames = _split(_items("AOS_EGRESS_DENY_HOSTS") + _items("AOS_SELF_IPS"))
    selfs = _self_ips()
    if any(host == n or host.endswith("." + n) for n in dnames): raise NetBlocked("host is on the deny list")
    host_ok = host in anames
    internal, all_allowed = False, True
    for ip in ips:
        for a in _addrs(ip):
            if a in selfs or any(a in n for n in dnets): raise NetBlocked("address is on the deny list or is this server itself")
            k = _kind(a)
            if k == "never": raise NetBlocked("address not reachable (metadata / link-local / multicast / reserved)")
            allowed = purpose not in ("public", "sandbox_egress") and (host_ok or any(a in n for n in anets))
            if k == "internal":
                internal = True
                if not allowed: raise NetBlocked("internal address (not in AOS_EGRESS_ALLOW)")
            all_allowed = all_allowed and allowed
    if u.scheme == "http" and (purpose != "fetch" or https_only) and not all_allowed: raise NetBlocked("https required (http only to allow-listed addresses)")
    return ips, host, internal

def check_all(url, purpose="webhook", https_only=False):
    try: ips, host, _ = _eval(url, purpose, https_only)
    except NetBlocked as e: raise _block(purpose, _hostof(url), e.reason) from None
    return [str(i) for i in ips], host

def check(url, purpose="webhook", https_only=False):
    """-> (ip, host) of the first checked address, or raise NetBlocked. Blocking DNS: from async code use acheck()."""
    ips, host = check_all(url, purpose, https_only); return ips[0], host

async def acheck(url, purpose="webhook", https_only=False): return await asyncio.to_thread(check, url, purpose, https_only)

def is_internal(url):
    """True when the URL passes the policy AND points only to internal (allow-listed) addresses — i.e. nothing leaves our network. False when blocked/unresolvable."""
    try: return _eval(url, "fetch")[2]
    except NetBlocked: return False

def _hostof(url):
    try: return httpx.URL(str(url or "")).host[:100] or "?"
    except Exception: return "?"   # noqa: BLE001

# --------------------------------------------------------------------------------------------- httpx plumbing
_PIN: contextvars.ContextVar = contextvars.ContextVar("netguard_pin", default=None)

class _Backend(httpcore.AsyncNetworkBackend):
    """Connects only to the addresses netguard just checked for this very host (set by _Transport). TLS is started later by httpcore with server_hostname = the
    original host, so SNI and certificate verification still use the name, not the IP."""
    def __init__(self): self._b = httpcore.AnyIOBackend()
    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        pin = _PIN.get()
        if not pin or pin[0] != host.lower().strip("."): raise httpcore.ConnectError("netguard: connection to an unchecked host refused")
        err = None
        for ip in pin[1]:
            try: return await self._b.connect_tcp(ip, port, timeout=timeout, local_address=local_address, socket_options=socket_options)
            except httpcore.ConnectError as e: err = e
        raise err or httpcore.ConnectError("no address")
    async def connect_unix_socket(self, path, timeout=None, socket_options=None): raise httpcore.ConnectError("netguard: unix sockets are not allowed")
    async def sleep(self, seconds): await self._b.sleep(seconds)

class _Transport(httpx.AsyncHTTPTransport):
    def __init__(self, purpose, https_only=False):
        super().__init__(http2=False)
        self.purpose, self.https_only = purpose, https_only
        # httpx has no public hook for the network backend: swap the pool it just built for one that uses ours (httpx 0.28 / httpcore 1.0, covered by tests)
        self._pool = httpcore.AsyncConnectionPool(ssl_context=httpx.create_ssl_context(), max_connections=20, max_keepalive_connections=5, http2=False, network_backend=_Backend())
    async def handle_async_request(self, request):
        ips, host = await asyncio.to_thread(check_all, str(request.url), self.purpose, self.https_only)   # every request = every redirect hop
        tok = _PIN.set((host, ips))
        try: resp = await super().handle_async_request(request)
        finally: _PIN.reset(tok)
        if self.purpose != "fetch" and resp.status_code in (301, 302, 303, 307, 308):
            await resp.aclose(); raise _block(self.purpose, host, "redirect not followed")
        return resp

def client(purpose="webhook", https_only=False, **kw):
    """httpx.AsyncClient under the egress policy. Use as `async with netguard.client("webhook", timeout=10) as h:`. https_only: also refuse http redirect hops/URLs
    of purpose "fetch". Proxies from the environment are ignored."""
    if purpose not in PURPOSES: raise ValueError("unknown netguard purpose " + repr(purpose))
    kw.pop("transport", None); kw.pop("follow_redirects", None); kw.setdefault("timeout", 10)
    return httpx.AsyncClient(transport=_Transport(purpose, https_only), follow_redirects=purpose == "fetch", max_redirects=5, trust_env=False, **kw)
