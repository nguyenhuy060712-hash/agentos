"""GĐ11.45 aos-egress: the ONLY way out of a virtual computer. A small forward proxy that runs in its own container (stdlib only, no third-party code).
 * HTTPS = CONNECT tunnel, TLS passes through untouched: NO interception, NO CA, NO key or header is ever added or read.
 * Every request first goes to the decision service of the API on a Unix socket (AOS_EGRESS_CTL): {box, secret, src ip, host, port}. The service checks the box
   credential + its fixed IP, the allow-list and netguard, and answers with the PINNED IP addresses; this proxy connects ONLY to those (no DNS here: DNS-rebinding
   safe, and a box can never use us as a DNS tunnel for a name that is not allow-listed). Any failure (service down, bad answer) = refusal (fail closed).
 * After the 200 of a CONNECT we look at the first bytes of the tunnel: a TLS ClientHello must carry an SNI equal to the CONNECT host; plain HTTP must carry the same
   Host; anything else is dropped. So `CONNECT allowed.example:443` cannot be turned into a TLS session for another name on the same IP.
 * Plain `GET http://host/...` is forwarded in origin-form with `Connection: close` and the Host taken from the URL.
Runs as the same uid as the API (docker run --user) so it can open the 0700 socket directory; read-only root FS, no capabilities."""
import asyncio, base64, json, os, re, struct, sys, time

CTL = os.environ.get("AOS_EGRESS_CTL", "/run/aos-egress/ctl.sock")
PORT = int(os.environ.get("AOS_EGRESS_PORT", "3128"))
MAX_HEAD = 32 * 1024
MAX_PER_BOX = int(os.environ.get("AOS_EGRESS_MAX_CONN", "64"))
IDLE = int(os.environ.get("AOS_EGRESS_IDLE_S", "300"))
BOX_RE = re.compile(r"^box-([0-9a-f]{16})$")
_open = {}

def log(*a): print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)

async def reply(w, code, text, body="", extra=""):
    b = (body or text).encode()
    w.write(f"HTTP/1.1 {code} {text}\r\n{extra}Content-Type: text/plain\r\nContent-Length: {len(b)}\r\nConnection: close\r\n\r\n".encode() + b)
    try: await w.drain()
    except OSError: pass

async def ask(req):
    """-> dict from the decision service, or {"ok": False, "reason": "controller_unavailable"}."""
    try:
        r, w = await asyncio.wait_for(asyncio.open_unix_connection(CTL), 5)
        w.write(json.dumps(req, separators=(",", ":")).encode() + b"\n"); await w.drain()
        line = await asyncio.wait_for(r.readline(), 15); w.close()
        out = json.loads(line)
        return out if isinstance(out, dict) else {"ok": False, "reason": "controller_bad_answer"}
    except (OSError, asyncio.TimeoutError, ValueError): return {"ok": False, "reason": "controller_unavailable"}

def parse_head(raw):
    """-> (start_line, {lower-name: value}, [(name, value)...]) or None."""
    try: text = raw.decode("latin-1")
    except UnicodeError: return None
    lines = text.split("\r\n")
    if len(lines) < 1 or not lines[0]: return None
    hdr, order = {}, []
    for ln in lines[1:]:
        if not ln: continue
        if ":" not in ln: return None
        k, v = ln.split(":", 1); k, v = k.strip(), v.strip()
        order.append((k, v)); hdr.setdefault(k.lower(), v)
    return lines[0], hdr, order

def basic_creds(hdr):
    v = hdr.get("proxy-authorization", "")
    if not v.lower().startswith("basic "): return None, None
    try: user, _, pw = base64.b64decode(v[6:].strip(), validate=True).decode().partition(":")
    except (ValueError, UnicodeError): return None, None
    m = BOX_RE.match(user)
    return (m.group(1), pw) if m and pw else (None, None)

def split_authority(a, default_port):
    a = a.strip()
    if a.startswith("["):
        m = re.fullmatch(r"\[([0-9A-Fa-f:.]+)\](?::(\d{1,5}))?", a)
        if not m: return None
        host, port = m.group(1), m.group(2)
    else:
        if a.count(":") > 1 or "@" in a: return None
        host, _, port = a.partition(":")
    port = int(port) if port else default_port
    if not host or port is None or not 1 <= port <= 65535: return None
    return host, port

def parse_sni(rec):
    """SNI of a TLS ClientHello record (bytes), '' when there is none, None when it is not a (complete) ClientHello."""
    try:
        if len(rec) < 9 or rec[0] != 0x16 or rec[5] != 0x01: return None
        rl = struct.unpack(">H", rec[3:5])[0]
        if len(rec) < 5 + rl: return None
        p = 5 + 4 + 2 + 32                                           # record header, handshake header, version, random
        p += 1 + rec[p]                                              # session id
        p += 2 + struct.unpack(">H", rec[p:p + 2])[0]                # cipher suites
        p += 1 + rec[p]                                              # compression
        if p + 2 > len(rec): return ""
        end = p + 2 + struct.unpack(">H", rec[p:p + 2])[0]; p += 2
        while p + 4 <= min(end, len(rec)):
            t, l = struct.unpack(">HH", rec[p:p + 4]); p += 4
            if t == 0:                                               # server_name
                q = p + 2
                while q + 3 <= p + l:
                    nt, nl = rec[q], struct.unpack(">H", rec[q + 1:q + 3])[0]; q += 3
                    if nt == 0: return rec[q:q + nl].decode("ascii").lower().rstrip(".")
                    q += nl
                return ""
            p += l
        return ""
    except (IndexError, struct.error, UnicodeError): return None

async def first_bytes(r):
    """The first chunk the client sends into a tunnel: a whole TLS record (<= 16 KB) or an HTTP head."""
    b = await asyncio.wait_for(r.readexactly(1), 15)
    if b[0] == 0x16:
        b += await asyncio.wait_for(r.readexactly(4), 15)
        n = struct.unpack(">H", b[3:5])[0]
        if n > 16384 + 2048: raise ValueError("record too big")
        return b + await asyncio.wait_for(r.readexactly(n), 15)
    if 0x41 <= b[0] <= 0x5A:
        return b + await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 15)
    raise ValueError("neither TLS nor HTTP")

def host_of_header(v):
    s = split_authority(v or "", 80)
    return s[0].lower().rstrip(".") if s else ""

async def connect_pinned(ips, port):
    err = None
    for ip in ips:
        try: return await asyncio.wait_for(asyncio.open_connection(ip, port), 10)
        except (OSError, asyncio.TimeoutError) as e: err = e
    raise err or OSError("no address")

async def pump(r, w):
    try:
        while True:
            d = await asyncio.wait_for(r.read(65536), IDLE)
            if not d: break
            w.write(d); await w.drain()
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError): pass
    finally:
        try:
            if w.can_write_eof(): w.write_eof()
        except OSError: pass

async def tunnel(cr, cw, ur, uw):
    t = [asyncio.create_task(pump(cr, uw)), asyncio.create_task(pump(ur, cw))]
    try: await asyncio.gather(*t)                       # an EOF is passed on as a half-close, so both directions end together (each read has the IDLE limit)
    finally:
        for x in t: x.cancel()
    for w in (cw, uw):
        try: w.close()
        except OSError: pass

async def handle(cr, cw):
    box, counted = None, False
    try:
        try: raw = await asyncio.wait_for(cr.readuntil(b"\r\n\r\n"), 15)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError): return
        ph = parse_head(raw[:-4])
        if not ph: return await reply(cw, 400, "Bad Request")
        start, hdr, order = ph
        parts = start.split(" ")
        if len(parts) != 3 or not parts[2].startswith("HTTP/1."): return await reply(cw, 400, "Bad Request")
        method, target = parts[0].upper(), parts[1]
        box, secret = basic_creds(hdr)
        if not box: return await reply(cw, 407, "Proxy Authentication Required", "aos-egress: credentials required", 'Proxy-Authenticate: Basic realm="aos-egress"\r\n')
        peer = (cw.get_extra_info("peername") or ("?",))[0]
        if _open.get(box, 0) >= MAX_PER_BOX: return await reply(cw, 429, "Too Many Requests", "aos-egress: too many open connections")
        _open[box] = _open.get(box, 0) + 1; counted = True
        if method == "CONNECT":
            hp = split_authority(target, None)
            if not hp: return await reply(cw, 400, "Bad Request")
            host, port, plain = hp[0], hp[1], False
        else:
            m = re.fullmatch(r"http://([^/?#]+)(/[^#]*)?", target, re.I)
            hp = split_authority(m.group(1), 80) if m else None
            if not hp: return await reply(cw, 400, "Bad Request", "aos-egress: only CONNECT and http:// absolute URLs")
            host, port, plain, path = hp[0], hp[1], True, (m.group(2) or "/")
        d = await ask({"op": "decide", "box": box, "secret": secret, "src": peer, "host": host, "port": port, "plain": plain})
        if not d.get("ok"):
            code = 503 if str(d.get("reason", "")).startswith("controller_") else 403
            return await reply(cw, code, "Forbidden" if code == 403 else "Service Unavailable", f"aos-egress: blocked ({str(d.get('reason', 'denied'))[:60]})")
        try: ur, uw = await connect_pinned([str(i) for i in d.get("ips", [])][:8], port)
        except (OSError, asyncio.TimeoutError): return await reply(cw, 502, "Bad Gateway", "aos-egress: could not connect")
        want = str(d.get("host") or host).lower().rstrip(".")
        try:
            if plain:
                skip = {"proxy-authorization", "proxy-connection", "connection", "keep-alive", "host", "upgrade", "te", "trailer"}
                out = [f"{method} {path} {parts[2]}", "Host: " + (f"[{hp[0]}]" if ":" in hp[0] else hp[0]) + (f":{hp[1]}" if hp[1] != 80 else "")] + [f"{k}: {v}" for k, v in order if k.lower() not in skip] + ["Connection: close", "", ""]
                uw.write("\r\n".join(out).encode("latin-1"))
                return await tunnel(cr, cw, ur, uw)
            cw.write(b"HTTP/1.1 200 Connection Established\r\n\r\n"); await cw.drain()
            fb = await first_bytes(cr)
            if fb[0] == 0x16:
                sni = parse_sni(fb)
                if d.get("ip_literal") or not sni or sni != want: raise ValueError("sni mismatch")
            else:
                h = parse_head(fb[:-4]);
                if not h or host_of_header(h[1].get("host")) != want: raise ValueError("host mismatch")
            uw.write(fb)
        except (ValueError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as e:
            log("dropped tunnel:", type(e).__name__, str(e)[:40]); uw.close(); return
        await tunnel(cr, cw, ur, uw)
    except Exception as e:  # noqa: BLE001
        log("handler error", type(e).__name__)
    finally:
        if counted: _open[box] = max(0, _open.get(box, 1) - 1)
        try: cw.close()
        except OSError: pass

async def main():
    srv = await asyncio.start_server(handle, "0.0.0.0", PORT, limit=MAX_HEAD, backlog=256)
    log("aos-egress listening on", PORT, "ctl", CTL)
    async with srv: await srv.serve_forever()

if __name__ == "__main__": asyncio.run(main())
