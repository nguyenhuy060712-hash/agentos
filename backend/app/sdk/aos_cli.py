#!/usr/bin/env python3
"""aos — small command-line client for Agent OS sandboxes, volumes, service URLs and custom apps (counterpart of `langsmith sandbox …` / `langsmith apps …`).
Only needs `httpx`. Authentication: an Agent OS API key (Settings > API keys) in AOS_API_KEY, server in AOS_URL (default http://127.0.0.1:8020).

  aos auth status
  aos sandbox list | create [--name N] [--ttl-hours H] [--from-snapshot NAME] | delete ID
  aos sandbox exec ID [--timeout S] (-c 'print(1)' | -f script.py)
  aos sandbox put ID LOCAL [REMOTE] | get ID REMOTE [LOCAL] | ls ID | rm ID REMOTE
  aos sandbox snapshot create ID [--name N] | list [ID] | restore SNAP ID | delete SNAP
  aos sandbox service start ID --port P [--name N] [--ttl-minutes M] -- CMD… | list ID | logs ID SVC | stop ID SVC | url ID SVC [--ttl S] | revoke ID SVC
  aos sandbox link ID PATH [--ttl S]
  aos volume list | create NAME | delete ID | import-git NAME REPO_URL [--ref R] | import-pack NAME pack@tag
  aos apps list | templates | init DIR [--template T] | push DIR [--name N] | pull ID DIR | delete ID | url ID
Add --json before the command for machine-readable output. Exit code 0 = ok, 1 = the server refused, 2 = usage error."""
import argparse, json, os, sys
from pathlib import Path
import httpx

TEXT_EXT = (".html", ".css", ".js", ".json", ".svg", ".txt", ".md")

class Cli:
    def __init__(self, url=None, key=None, as_json=False, out=None, client=None):
        self.url = (url or os.environ.get("AOS_URL") or "http://127.0.0.1:8020").rstrip("/")
        self.key = key or os.environ.get("AOS_API_KEY") or ""
        self.json = as_json; self.out = out or sys.stdout
        self.c = client or httpx.Client(base_url=self.url, headers={"X-Api-Key": self.key} if self.key else {}, timeout=130)

    def p(self, *a): print(*a, file=self.out)
    def emit(self, data, text=None):
        self.p(json.dumps(data, ensure_ascii=False, indent=1, default=str) if self.json or text is None else text)

    def req(self, method, path, **kw):
        r = self.c.request(method, path, **kw)
        if r.status_code >= 400:
            try: d = r.json().get("detail") or r.json()
            except ValueError: d = r.text[:300]
            raise SystemExit(f"error {r.status_code}: {d}")
        return r

    def j(self, method, path, **kw):
        r = self.req(method, path, **kw)
        return r.json() if r.content and "json" in r.headers.get("content-type", "") else {}

    # ------------------------------------------------------------ commands
    def auth_status(self, a):
        me = self.j("GET", "/api/auth/me"); self.emit(me, f"{me.get('username')} · role {me.get('role')} · workspace {me.get('ws') or me.get('workspace_id')} · {self.url}")

    def sbx_list(self, a):
        rows = self.j("GET", "/api/sandboxes")
        self.emit(rows, "\n".join(f"{r['id']}  {r['name'] or '-':<20} {r.get('bytes', 0):>9} B  ttl {r['ttl_hours']}h" for r in rows) or "(no sandboxes)")

    def sbx_create(self, a):
        if a.from_snapshot: r = self.j("POST", "/api/sandboxes/from-snapshot", json={"snapshot": a.from_snapshot, "name": a.name})
        else: r = self.j("POST", "/api/sandboxes", json={"name": a.name, "ttl_hours": a.ttl_hours})
        self.emit(r, r["id"])

    def sbx_delete(self, a): self.j("DELETE", f"/api/sandboxes/{a.id}"); self.emit({"ok": True}, "deleted")

    def sbx_exec(self, a):
        code = a.c if a.c is not None else (Path(a.f).read_text(encoding="utf-8") if a.f else sys.stdin.read())
        r = self.j("POST", f"/api/sandboxes/{a.id}/exec", json={"code": code, "timeout": a.timeout})
        if self.json: self.emit(r)
        else:
            if r.get("stdout"): self.out.write(r["stdout"])
            if r.get("stderr"): sys.stderr.write(r["stderr"])
        if r.get("exit") not in (0, None): raise SystemExit(r["exit"] or 1)

    def sbx_put(self, a):
        p = Path(a.local); remote = a.remote or p.name
        r = self.j("POST", f"/api/sandboxes/{a.id}/files", files={"file": (p.name, p.read_bytes())}, data={"path": remote}); self.emit(r, f"uploaded {r['path']} ({r['bytes']} B)")

    def sbx_get(self, a):
        r = self.req("GET", f"/api/sandboxes/{a.id}/files/{a.remote}")
        if a.local: Path(a.local).write_bytes(r.content); self.p(f"saved {a.local} ({len(r.content)} B)")
        else: sys.stdout.buffer.write(r.content)

    def sbx_ls(self, a):
        rows = self.j("GET", f"/api/sandboxes/{a.id}/files"); self.emit(rows, "\n".join(f"{r['bytes']:>9}  {r['path']}" for r in rows) or "(empty)")

    def sbx_rm(self, a): self.j("DELETE", f"/api/sandboxes/{a.id}/files/{a.remote}"); self.emit({"ok": True}, "removed")

    def snap_create(self, a): r = self.j("POST", f"/api/sandboxes/{a.id}/snapshots", json={"name": a.name}); self.emit(r, f"{r['id']} ({r['bytes']} B)")
    def snap_list(self, a):
        rows = self.j("GET", f"/api/sandboxes/{a.id}/snapshots" if a.id else "/api/sandbox_snapshots")
        self.emit(rows, "\n".join(f"{r['id']}  {r['name']:<24} {r.get('bytes', 0):>9} B" for r in rows) or "(no snapshots)")
    def snap_restore(self, a): r = self.j("POST", f"/api/sandbox_snapshots/{a.snap}/restore/{a.id}"); self.emit(r, f"restored {r['files']} files")
    def snap_delete(self, a): self.j("DELETE", f"/api/sandbox_snapshots/{a.snap}"); self.emit({"ok": True}, "deleted")

    def svc_start(self, a):
        cmd = [x for x in a.cmd if x != "--"]
        if not cmd: raise SystemExit("usage: aos sandbox service start ID --port P -- CMD…")
        r = self.j("POST", f"/api/sandboxes/{a.id}/services", json={"command": cmd, "port": a.port, "name": a.name, "ttl_minutes": a.ttl_minutes}); self.emit(r, f"{r['id']} {r['status']} {r['url']}")
    def svc_list(self, a):
        rows = self.j("GET", f"/api/sandboxes/{a.id}/services"); self.emit(rows, "\n".join(f"{r['id']}  {r['name']:<14} :{r['port']}  {r['status']}" for r in rows) or "(no services)")
    def svc_logs(self, a): self.p(self.j("GET", f"/api/sandboxes/{a.id}/services/{a.svc}/logs")["logs"])
    def svc_stop(self, a): self.j("DELETE", f"/api/sandboxes/{a.id}/services/{a.svc}"); self.emit({"ok": True}, "stopped")
    def svc_url(self, a):
        r = self.j("POST", f"/api/sandboxes/{a.id}/service-url", json={"service": a.svc, "expires_in_seconds": a.ttl}); self.emit(r, f"{r['browser_url']}\n(expires {r['expires_at']})")
    def svc_revoke(self, a): self.j("POST", f"/api/sandboxes/{a.id}/services/{a.svc}/revoke-links"); self.emit({"ok": True}, "links revoked")
    def sbx_link(self, a): r = self.j("POST", f"/api/sandboxes/{a.id}/link", json={"path": a.path, "expires_s": a.ttl}); self.emit(r, r["url"])

    def vol_list(self, a):
        rows = self.j("GET", "/api/sandbox_volumes"); self.emit(rows, "\n".join(f"{r['id']}  {r['name']:<20} {r.get('bytes', 0):>9} B  {'locked ' + json.dumps(r['source']) if r.get('locked') else 'rw'}" for r in rows) or "(no volumes)")
    def vol_create(self, a): r = self.j("POST", "/api/sandbox_volumes", json={"name": a.name}); self.emit(r, r["id"])
    def vol_delete(self, a): self.j("DELETE", f"/api/sandbox_volumes/{a.id}"); self.emit({"ok": True}, "deleted")
    def vol_git(self, a): r = self.j("POST", "/api/sandbox_volumes/import", json={"name": a.name, "git": {"url": a.repo, **({"ref": a.ref} if a.ref else {})}}); self.emit(r, f"{r['id']} {r['files']} files (read-only)")
    def vol_pack(self, a): r = self.j("POST", "/api/sandbox_volumes/import", json={"name": a.name, "context_pack": a.pack}); self.emit(r, f"{r['id']} {r['files']} files (read-only)")

    # ---- custom apps
    def apps_list(self, a):
        rows = self.j("GET", "/api/apps"); self.emit(rows, "\n".join(f"{r['id']}  v{r['version']:<3} {r['name']}" for r in rows) or "(no apps)")
    def apps_templates(self, a):
        rows = self.j("GET", "/api/apps/templates"); self.emit(rows, "\n".join(f"{r['key']:<20} {r['title']}" for r in rows))
    def apps_init(self, a):
        t = next((x for x in self.j("GET", "/api/apps/templates") if x["key"] == a.template), None)
        if not t: raise SystemExit("unknown template (aos apps templates)")
        d = Path(a.dir); d.mkdir(parents=True, exist_ok=True)
        # the server returns file names only for the list; create the app from the template, then pull it into the directory
        r = self.j("POST", "/api/apps", json={"name": a.name or d.name, "template": a.template}); self.apps_pull(argparse.Namespace(id=r["id"], dir=str(d))); self.p(f"created {r['id']} in {d}")
    def _read_dir(self, d: Path):
        files = {}
        for p in sorted(d.rglob("*")):
            if p.is_file() and p.suffix.lower() in TEXT_EXT and ".aos" not in p.parts: files[p.relative_to(d).as_posix()] = p.read_text(encoding="utf-8")
        return files
    def apps_push(self, a):
        d = Path(a.dir); files = self._read_dir(d); meta = d / ".aos" / "app.json"
        if meta.exists():
            aid = json.loads(meta.read_text())["id"]; r = self.j("PUT", f"/api/apps/{aid}", json={"files": files}); self.emit(r, f"pushed {aid} -> v{r['version']}")
        else:
            r = self.j("POST", "/api/apps", json={"name": a.name or d.resolve().name, "files": files}); meta.parent.mkdir(exist_ok=True); meta.write_text(json.dumps({"id": r["id"]}))
            self.emit(r, f"created {r['id']} (linked in .aos/app.json) -> {self.url}{r['view_url']}")
    def apps_pull(self, a):
        app = self.j("GET", f"/api/apps/{a.id}"); d = Path(a.dir)
        for p, t in app["files"].items():
            f = d / p; f.parent.mkdir(parents=True, exist_ok=True); f.write_text(t, encoding="utf-8")
        (d / ".aos").mkdir(exist_ok=True); (d / ".aos" / "app.json").write_text(json.dumps({"id": a.id})); self.p(f"pulled {len(app['files'])} files of '{app['name']}' v{app['version']} into {d}")
    def apps_delete(self, a): self.j("DELETE", f"/api/apps/{a.id}"); self.emit({"ok": True}, "deleted")
    def apps_url(self, a): self.p(f"{self.url}/apps/view/{a.id}")

def parser():
    ap = argparse.ArgumentParser(prog="aos", description="Agent OS command line (sandboxes, volumes, custom apps)")
    ap.add_argument("--json", action="store_true"); ap.add_argument("--url"); ap.add_argument("--key")
    sub = ap.add_subparsers(dest="group", required=True)
    def cmd(s, name, fn, *args, **kw):
        p = s.add_parser(name, **kw); p.set_defaults(fn=fn)
        for spec in args:
            if isinstance(spec, str): spec = (spec, {})
            names, opts = spec[0], (spec[1] if len(spec) > 1 else {})
            p.add_argument(*([names] if isinstance(names, str) else names), **opts)
        return p
    a = sub.add_parser("auth").add_subparsers(dest="cmd", required=True); cmd(a, "status", "auth_status")
    sb = sub.add_parser("sandbox").add_subparsers(dest="cmd", required=True)
    cmd(sb, "list", "sbx_list")
    cmd(sb, "create", "sbx_create", ("--name", {"default": "sandbox"}), ("--ttl-hours", {"type": int, "default": 168}), ("--from-snapshot", {}))
    cmd(sb, "delete", "sbx_delete", "id")
    cmd(sb, "exec", "sbx_exec", "id", ("--timeout", {"type": int, "default": 30}), ("-c", {}), ("-f", {}))
    cmd(sb, "put", "sbx_put", "id", "local", ("remote", {"nargs": "?"})); cmd(sb, "get", "sbx_get", "id", "remote", ("local", {"nargs": "?"})); cmd(sb, "ls", "sbx_ls", "id"); cmd(sb, "rm", "sbx_rm", "id", "remote")
    cmd(sb, "link", "sbx_link", "id", "path", ("--ttl", {"type": int, "default": 3600}))
    sn = sb.add_parser("snapshot").add_subparsers(dest="scmd", required=True)
    cmd(sn, "create", "snap_create", "id", ("--name", {"default": "snapshot"})); cmd(sn, "list", "snap_list", ("id", {"nargs": "?"})); cmd(sn, "restore", "snap_restore", "snap", "id"); cmd(sn, "delete", "snap_delete", "snap")
    sv = sb.add_parser("service").add_subparsers(dest="vcmd", required=True)
    cmd(sv, "start", "svc_start", "id", ("--port", {"type": int, "default": 8000}), ("--name", {"default": "service"}), ("--ttl-minutes", {"type": int, "default": 60}), ("cmd", {"nargs": argparse.REMAINDER}))
    cmd(sv, "list", "svc_list", "id"); cmd(sv, "logs", "svc_logs", "id", "svc"); cmd(sv, "stop", "svc_stop", "id", "svc")
    cmd(sv, "url", "svc_url", "id", "svc", ("--ttl", {"type": int, "default": 600})); cmd(sv, "revoke", "svc_revoke", "id", "svc")
    vo = sub.add_parser("volume").add_subparsers(dest="cmd", required=True)
    cmd(vo, "list", "vol_list"); cmd(vo, "create", "vol_create", "name"); cmd(vo, "delete", "vol_delete", "id")
    cmd(vo, "import-git", "vol_git", "name", "repo", ("--ref", {})); cmd(vo, "import-pack", "vol_pack", "name", "pack")
    ap_ = sub.add_parser("apps").add_subparsers(dest="cmd", required=True)
    cmd(ap_, "list", "apps_list"); cmd(ap_, "templates", "apps_templates"); cmd(ap_, "init", "apps_init", "dir", ("--template", {"default": "blank"}), ("--name", {}))
    cmd(ap_, "push", "apps_push", ("dir", {"nargs": "?", "default": "."}), ("--name", {})); cmd(ap_, "pull", "apps_pull", "id", "dir"); cmd(ap_, "delete", "apps_delete", "id"); cmd(ap_, "url", "apps_url", "id")
    return ap

def main(argv=None, out=None, client=None, url=None, key=None) -> int:
    a = parser().parse_args(argv)
    cli = Cli(url=a.url or url, key=a.key or key, as_json=a.json, out=out, client=client)
    try: getattr(cli, a.fn)(a)
    except SystemExit as e:
        if isinstance(e.code, str): print(e.code, file=sys.stderr); return 1
        return int(e.code or 0)
    except httpx.HTTPError as e: print(f"error: cannot reach {cli.url}: {type(e).__name__}", file=sys.stderr); return 1
    return 0

if __name__ == "__main__": sys.exit(main())
