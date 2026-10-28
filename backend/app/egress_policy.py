"""GĐ11.45 egress policy of the virtual computer: ONE pattern matcher, pure functions (no network, no DB). Used by app/egress_ctl.py (the decision service) and
by the API that stores a box's allow-list (validate_list).
Entry forms (match on the NAME AS CALLED; a name is never matched through what it resolves to):
  host            exact name                      pypi.org            (ports 80 and 443)
  host:PORT       exact name + exactly that port  example.com:8443
  *.glob          sub-domains of a name, any depth, NOT the bare name       *.githubusercontent.com
  ~regex          full match of the (lower-cased) name                      ~(a|b)\\.example\\.com
  IP / CIDR       only when the box calls a literal IP (bare-IP HTTPS is dropped by the proxy: no SNI to check)    203.0.113.7  198.51.100.0/24
Modes: off < basic < custom. `basic` = BASIC list below; `custom` = BASIC + the box's own list. The workspace ceiling (workspace_sandbox_quotas.internet) caps the mode.
This module never decides about internal addresses: every decision also goes through netguard.check(.., "sandbox_egress") in egress_ctl."""
import ipaddress, re

MODES = ("off", "basic", "custom")
BASIC = ("pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "github.com", "*.githubusercontent.com", "codeload.github.com",
         "archive.ubuntu.com", "security.ubuntu.com", "ports.ubuntu.com", "*.archive.ubuntu.com")
DEFAULT_PORTS = (80, 443)
MAX_ENTRIES, MAX_ENTRY_LEN = 200, 200

_LABEL = re.compile(r"^[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?$")
_NESTED_QUANT = re.compile(r"\)\s*[*+?{]")          # (a+)+ style: regex from a workspace admin runs inside the decision service -> no catastrophic backtracking
_PROBES = ("aos-probe-1.invalid", "example.com", "a.b.c.d.e.f", "x", "localhost")

class BadEntry(ValueError): pass
class BadHost(ValueError): pass

def mode_rank(m): return MODES.index(m) if m in MODES else 0
def min_mode(a, b): return a if mode_rank(a) <= mode_rank(b) else b

def normalize_host(h):
    """-> (host, is_ip). Lower-case, no trailing dot, no brackets. Raises BadHost for anything that is not a plain DNS name or a canonical IP literal:
    decimal / octal / hex / short IPv4 spellings, user-info, paths, spaces, non-ASCII (IDN must arrive as punycode), over-long names."""
    if not isinstance(h, str): raise BadHost("bad host")
    h = h.strip().lower()
    if h.startswith("[") and h.endswith("]"): h = h[1:-1]
    h = h.rstrip(".")
    if not h or len(h) > 253 or not h.isascii(): raise BadHost("bad host")
    if "%" in h and ":" in h: raise BadHost("bad host")              # IPv6 zone ids
    try: return str(ipaddress.ip_address(h)), True
    except ValueError: pass
    labels = h.split(".")
    if not all(_LABEL.match(x) for x in labels): raise BadHost("bad host")
    if not re.search(r"[a-z]", labels[-1]) or re.fullmatch(r"0x[0-9a-f]*", labels[-1]): raise BadHost("numeric host spelling is not allowed")   # 2130706433, 127.1, 0177.0.0.1, 0x7f.1 ...
    return h, False

class Entry:
    __slots__ = ("raw", "kind", "val", "port")
    def __init__(self, raw, kind, val, port): self.raw, self.kind, self.val, self.port = raw, kind, val, port
    def __repr__(self): return f"Entry({self.raw!r})"
    def ports(self): return (self.port,) if self.port else DEFAULT_PORTS

def parse_entry(s):
    if not isinstance(s, str): raise BadEntry("allow-list entries must be text")
    raw = s.strip()
    if not raw or len(raw) > MAX_ENTRY_LEN or any(c in raw for c in "\n\r\x00 \t"): raise BadEntry("empty, too long or contains spaces: " + raw[:30])
    if raw.startswith("~"):
        pat = raw[1:]
        if not pat or len(pat) > 120 or _NESTED_QUANT.search(pat): raise BadEntry("regex too long or too complex (no quantified groups): " + raw[:30])
        try: rx = re.compile(pat, re.I)
        except re.error: raise BadEntry("invalid regex: " + raw[:30]) from None
        if any(rx.fullmatch(p) for p in _PROBES): raise BadEntry("regex matches almost everything (allow-list, not allow-all): " + raw[:30])
        return Entry(raw, "re", rx, None)
    port, body = None, raw.lower()
    if body.startswith("["):                                           # [v6]:port
        m = re.fullmatch(r"\[([0-9a-f:.]+)\](?::(\d{1,5}))?", body)
        if not m: raise BadEntry("bad entry: " + raw[:30])
        body, port = m.group(1), m.group(2)
    elif body.count(":") == 1:
        body, port = body.split(":");
    if port is not None:
        if not port.isdigit() or not 1 <= int(port) <= 65535: raise BadEntry("bad port: " + raw[:30])
        port = int(port)
    try: return Entry(raw, "net", ipaddress.ip_network(body, strict=False), port)
    except ValueError: pass
    if body.startswith("*."):
        h = body[2:]
        try: host, is_ip = normalize_host(h)
        except BadHost: raise BadEntry("bad pattern: " + raw[:30]) from None
        if is_ip or "." not in host: raise BadEntry("a wildcard needs a registrable domain like *.example.com: " + raw[:30])
        return Entry(raw, "glob", host, port)
    if "*" in body: raise BadEntry("only a leading '*.' wildcard is supported: " + raw[:30])
    try: host, is_ip = normalize_host(body)
    except BadHost: raise BadEntry("bad host name: " + raw[:30]) from None
    return Entry(raw, "net" if is_ip else "name", ipaddress.ip_network(host) if is_ip else host, port)

def parse_list(items):
    if items is None: return []
    if isinstance(items, str): items = re.split(r"[,\n]", items)
    items = [x for x in items if not (isinstance(x, str) and not x.strip())]
    if len(items) > MAX_ENTRIES: raise BadEntry(f"at most {MAX_ENTRIES} entries")
    return [parse_entry(x) for x in items]

def validate_list(items):
    """-> the cleaned list of strings (stored in host_state) or raise BadEntry."""
    return [e.raw for e in parse_list(items)]

_BASIC = None
def basic_entries():
    global _BASIC
    if _BASIC is None: _BASIC = [parse_entry(x) for x in BASIC]
    return _BASIC

def matches(entries, host, port, is_ip=False):
    """host must come out of normalize_host. -> the matching Entry or None."""
    try: ip = ipaddress.ip_address(host) if is_ip else None
    except ValueError: return None
    for e in entries:
        if e.port is None and port not in DEFAULT_PORTS: continue
        if e.port is not None and e.port != port: continue
        if e.kind == "net":
            if ip is not None and (ip in e.val if ip.version == e.val.version else False): return e
        elif is_ip: continue                                            # names / globs / regexes never match a literal IP
        elif e.kind == "name":
            if host == e.val: return e
        elif e.kind == "glob":
            if host.endswith("." + e.val): return e
        elif e.kind == "re":
            if e.val.fullmatch(host): return e
    return None

def allowed(mode, custom, host, port, is_ip=False):
    """-> (ok, rule). `mode` is the EFFECTIVE mode (already capped by the workspace ceiling); `custom` = parsed custom entries."""
    if mode == "off": return False, "off"
    e = matches(basic_entries(), host, port, is_ip)
    if e is None and mode == "custom": e = matches(custom, host, port, is_ip)
    return (True, e.raw) if e else (False, "not_listed")
