"""Agent OS Sandboxes SDK + CLI (stdlib only).
Auth: AGENTOS_ENDPOINT + AGENTOS_API_KEY (a personal API key from Settings > API Keys), or AGENTOS_USER + AGENTOS_PASSWORD.

SDK:
    from agentos_sandbox import Sandboxes
    sb = Sandboxes()                          # reads env
    s = sb.create("demo", ttl_hours=24)
    sb.put(s, "data.csv", open("data.csv","rb").read())
    print(sb.exec(s, "print(open('data.csv').read()[:100])")["stdout"])
    snap = sb.snapshot(s, "before-change"); sb.restore(snap, s); clone = sb.clone(snap, "copy")
    svc = sb.serve(s, ["python", "-m", "http.server", "8000"], port=8000)   # -> {"url": "https://.../svc/<id>/"} (auth + HTTPS only)
    sb.stop(s, svc["id"]); sb.delete(s)

CLI:
    python -m agentos_sandbox ls | create NAME [--ttl H] | rm ID | exec ID (CODE|-) [--mount VOL[:ro]]... [--timeout S]
                              files ID | put ID LOCAL [REMOTE] | get ID REMOTE [LOCAL] | snapshot ID [NAME] | snapshots
                              restore SNAP ID | clone SNAP [NAME] | serve ID PORT -- CMD... [--ttl MIN] | services ID | stop ID SVC
                              logs ID SVC | volumes | volume-create NAME | runtime
"""
import base64, json, os, sys, urllib.error, urllib.parse, urllib.request, uuid

class SandboxError(RuntimeError): pass

class Sandboxes:
    def __init__(self, endpoint=None, api_key=None, user=None, password=None, timeout=180):
        self.base = (endpoint or os.environ.get("AGENTOS_ENDPOINT") or "").rstrip("/")
        if not self.base: raise SandboxError("Set AGENTOS_ENDPOINT")
        self.h = {}
        key = api_key or os.environ.get("AGENTOS_API_KEY")
        u, p = user or os.environ.get("AGENTOS_USER"), password or os.environ.get("AGENTOS_PASSWORD")
        if key: self.h["X-Api-Key"] = key
        elif u and p: self.h["Authorization"] = "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()
        else: raise SandboxError("Set AGENTOS_API_KEY (or AGENTOS_USER/AGENTOS_PASSWORD)")
        self.timeout = timeout

    def _req(self, method, path, body=None, raw=False, ctype=None):
        data = None; h = dict(self.h)
        if body is not None and ctype is None: data = json.dumps(body).encode(); h["Content-Type"] = "application/json"
        elif body is not None: data = body; h["Content-Type"] = ctype
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=self.timeout) as resp:
                b = resp.read(); return b if raw else (json.loads(b) if b else None)
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")
            try: msg = json.loads(msg).get("detail", msg)
            except Exception: pass  # noqa: BLE001
            raise SandboxError(f"{e.code}: {msg}") from None

    @staticmethod
    def _id(s): return s["id"] if isinstance(s, dict) else str(s)
    # sandboxes
    def list(self): return self._req("GET", "/api/sandboxes")
    def create(self, name="sandbox", ttl_hours=168): return self._req("POST", "/api/sandboxes", {"name": name, "ttl_hours": ttl_hours})
    def delete(self, s): return self._req("DELETE", f"/api/sandboxes/{self._id(s)}")
    def exec(self, s, code, timeout=30, mounts=None):
        if mounts:
            m = [{"volume": x.split(":")[0], "readonly": x.endswith(":ro")} if isinstance(x, str) else x for x in mounts]
            return self._req("POST", f"/api/sandboxes/{self._id(s)}/exec_mounted", {"code": code, "timeout": timeout, "mounts": m})
        return self._req("POST", f"/api/sandboxes/{self._id(s)}/exec", {"code": code, "timeout": timeout})
    # files
    def files(self, s): return self._req("GET", f"/api/sandboxes/{self._id(s)}/files")
    def put(self, s, remote, data: bytes):
        bnd = uuid.uuid4().hex; fn = os.path.basename(remote)
        body = (f"--{bnd}\r\nContent-Disposition: form-data; name=\"path\"\r\n\r\n{remote}\r\n--{bnd}\r\n"
                f"Content-Disposition: form-data; name=\"file\"; filename=\"{fn}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{bnd}--\r\n".encode()
        return self._req("POST", f"/api/sandboxes/{self._id(s)}/files", body, ctype=f"multipart/form-data; boundary={bnd}")
    def get(self, s, remote): return self._req("GET", f"/api/sandboxes/{self._id(s)}/files/{urllib.parse.quote(remote)}", raw=True)
    def rm_file(self, s, remote): return self._req("DELETE", f"/api/sandboxes/{self._id(s)}/files/{urllib.parse.quote(remote)}")
    # snapshots
    def snapshot(self, s, name="snapshot"): return self._req("POST", f"/api/sandboxes/{self._id(s)}/snapshots", {"name": name})
    def snapshots(self): return self._req("GET", "/api/sandbox_snapshots")
    def restore(self, snap, s): return self._req("POST", f"/api/sandbox_snapshots/{self._id(snap)}/restore/{self._id(s)}")
    def clone(self, snap, name="clone"): return self._req("POST", f"/api/sandbox_snapshots/{self._id(snap)}/clone", {"name": name})
    def delete_snapshot(self, snap): return self._req("DELETE", f"/api/sandbox_snapshots/{self._id(snap)}")
    # volumes (mounts)
    def volumes(self): return self._req("GET", "/api/sandbox_volumes")
    def create_volume(self, name): return self._req("POST", "/api/sandbox_volumes", {"name": name})
    def delete_volume(self, v): return self._req("DELETE", f"/api/sandbox_volumes/{self._id(v)}")
    # services (exposed behind auth + HTTPS)
    def serve(self, s, command, port=8000, ttl_minutes=60, name="service", files_readonly=False):
        return self._req("POST", f"/api/sandboxes/{self._id(s)}/services", {"command": command, "port": port, "ttl_minutes": ttl_minutes, "name": name, "files_readonly": files_readonly})
    def services(self, s): return self._req("GET", f"/api/sandboxes/{self._id(s)}/services")
    def stop(self, s, svc): return self._req("DELETE", f"/api/sandboxes/{self._id(s)}/services/{self._id(svc)}")
    def logs(self, s, svc, tail=200): return self._req("GET", f"/api/sandboxes/{self._id(s)}/services/{self._id(svc)}/logs?tail={tail}")
    def fetch(self, url_or_path, method="GET", body=None):
        """Call a running service through the authenticated proxy (returns bytes)."""
        p = url_or_path[len(self.base):] if url_or_path.startswith(self.base) else url_or_path
        return self._req(method, p, body, raw=True)
    def runtime(self): return self._req("GET", "/api/sandbox_runtime")

def _table(rows, cols):
    if not rows: print("(none)"); return
    w = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("  ".join(c.ljust(w[c]) for c in cols))
    for r in rows: print("  ".join(str(r.get(c, "")).ljust(w[c]) for c in cols))

def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    if not a or a[0] in ("-h", "--help", "help"): print(__doc__.split("CLI:")[1].strip()); return 0
    def opt(flag, default=None, many=False):
        vals = []
        while flag in a:
            i = a.index(flag); vals.append(a[i + 1]); del a[i:i + 2]
        return vals if many else (vals[-1] if vals else default)
    sb = Sandboxes(); cmd = a.pop(0)
    try:
        if cmd == "ls": _table(sb.list(), ["id", "name", "owner", "bytes", "ttl_hours", "created"])
        elif cmd == "create": ttl = int(opt("--ttl", 168)); print(sb.create(a[0] if a else "sandbox", ttl)["id"])
        elif cmd == "rm": sb.delete(a[0]); print("deleted")
        elif cmd == "exec":
            mounts = opt("--mount", many=True); to = int(opt("--timeout", 30)); code = sys.stdin.read() if a[1] == "-" else a[1]
            r = sb.exec(a[0], code, to, mounts); sys.stdout.write(r.get("stdout", "")); sys.stderr.write(r.get("stderr", "")); return 0 if r.get("exit") == 0 else 1
        elif cmd == "files": _table(sb.files(a[0]), ["path", "bytes", "mtime"])
        elif cmd == "put": print(sb.put(a[0], a[2] if len(a) > 2 else os.path.basename(a[1]), open(a[1], "rb").read()))
        elif cmd == "get":
            data = sb.get(a[0], a[1]); out = a[2] if len(a) > 2 else os.path.basename(a[1])
            (sys.stdout.buffer.write(data) if out == "-" else open(out, "wb").write(data))
        elif cmd == "snapshot": print(sb.snapshot(a[0], a[1] if len(a) > 1 else "snapshot")["id"])
        elif cmd == "snapshots": _table(sb.snapshots(), ["id", "sandbox_id", "name", "bytes", "created"])
        elif cmd == "restore": print(sb.restore(a[0], a[1]))
        elif cmd == "clone": print(sb.clone(a[0], a[1] if len(a) > 1 else "clone")["id"])
        elif cmd == "serve":
            ttl = int(opt("--ttl", 60)); i = a.index("--"); r = sb.serve(a[0], a[i + 1:], int(a[1]), ttl); print(json.dumps(r, indent=1))
        elif cmd == "services": _table(sb.services(a[0]), ["id", "name", "port", "status", "runtime", "expires"])
        elif cmd == "stop": sb.stop(a[0], a[1]); print("stopped")
        elif cmd == "logs": print(sb.logs(a[0], a[1])["logs"])
        elif cmd == "volumes": _table(sb.volumes(), ["id", "name", "bytes", "created"])
        elif cmd == "volume-create": print(sb.create_volume(a[0]))
        elif cmd == "runtime": print(json.dumps(sb.runtime(), indent=1))
        else: print("unknown command; see --help"); return 2
    except SandboxError as e:
        print("error:", e, file=sys.stderr); return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
