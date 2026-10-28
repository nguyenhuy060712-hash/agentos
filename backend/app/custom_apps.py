"""GĐ9.Q P4 — Custom Apps (LangSmith "Custom Apps"): small UIs that members of a workspace build next to their agents and open from the sidebar.
Same security model as LangSmith: the app runs in a sandbox with NO network and no credentials of its own; the host page forwards the app's API calls with the
VIEWER'S credentials, so everyone only sees what their permissions allow, and the host blocks the endpoints for API keys, members/users, secrets, SSO, service keys.
  * storage     : `custom_apps` (workspace RLS) + `custom_app_versions` (every push keeps the previous version; rollback = restore)
  * serving     : `/apps/view/{id}` = trusted host page with an <iframe sandbox="allow-scripts"> (opaque origin: no cookies, no same-origin access) whose document is
                  `/apps/frame/{id}` (css/js bundled inline, CSP `connect-src 'none'`, `frame-ancestors 'self'`) — the only way out is `aos.call()` -> postMessage -> host
  * bridge      : host -> `POST /api/apps/{id}/call` (server re-checks the block-list, then performs the request in-process with the caller's own credentials, so the
                  normal login / role / workspace rules apply again; 120 calls per minute per user and app)
  * CLI         : `aos apps list | push DIR | pull ID DIR | delete ID` (app/sdk/aos_cli.py)."""
import json, re, secrets, time
from collections import defaultdict, deque
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, JSONResponse
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb

router = APIRouter(tags=["custom-apps"])
DDL = """
CREATE TABLE IF NOT EXISTS custom_apps(id TEXT PRIMARY KEY, workspace_id INT NOT NULL DEFAULT 1, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', files JSONB NOT NULL,
  entry TEXT NOT NULL DEFAULT 'index.html', version INT NOT NULL DEFAULT 1, created_by INT, updated_by INT, created TIMESTAMPTZ NOT NULL DEFAULT now(), updated TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE UNIQUE INDEX IF NOT EXISTS custom_apps_ws_name ON custom_apps(workspace_id, lower(name));
CREATE TABLE IF NOT EXISTS custom_app_versions(app_id TEXT NOT NULL REFERENCES custom_apps(id) ON DELETE CASCADE, version INT NOT NULL, files JSONB NOT NULL, entry TEXT NOT NULL,
  created_by INT, created TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(app_id, version));
"""
MAX_FILES, MAX_FILE, MAX_TOTAL = 40, 200_000, 1_000_000
EXT_OK = (".html", ".css", ".js", ".json", ".svg", ".txt", ".md")
PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-/]{0,119}$")
# What an app can NOT reach with the viewer's credentials (LangSmith blocks api keys, members, users, identities, roles, permissions, SCIM, service accounts).
BLOCKED = (r"^/api/auth/", r"^/api/admin", r"^/api/secrets", r"^/api/gw/keys", r"^/api/gateway/key", r"^/api/server-config", r"^/api/oauth/", r"^/api/apps(/|$)", r"^/api/app-call/",
           r"^/api/agents/\d+/(service-key|shares|identity)", r"^/api/exports", r"^/api/crypto", r"^/api/privacy", r"^/api/sandboxes/[^/]+/(link|service-url)", r"^/login", r"^/logout",
           r"^/api/workspace-tools", r"^/api/mcp/servers", r"^/api/channels", r"^/api/agents/\d+/channels", r"^/api/obs/ingest", r"^/ingest/", r"^/share/", r"^/sbx/")
_BLOCK = [re.compile(x) for x in BLOCKED]
ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}

def blocked(method: str, path: str) -> str | None:
    """Reason when an app must not call (method, path), else None. Paths are normalised first (no .. / // / encoded dots tricks)."""
    from urllib.parse import unquote, urlsplit
    if method.upper() not in ALLOWED_METHODS: return "method not allowed"
    u = urlsplit(path)
    if u.scheme or u.netloc or not path.startswith("/") or path.startswith("//"): return "only same-server paths starting with / are allowed"
    p = unquote(unquote(u.path))
    if "\\" in p or "\x00" in p or re.search(r"(^|/)\.\.?(/|$)", p) or "//" in p: return "path is not normalised"
    if not p.startswith(("/api/", "/v1/")): return "apps may call /api/* only"
    for rx in _BLOCK:
        if rx.search(p): return "this endpoint is not available to apps"
    return None

async def _q(*a, **k):
    import deepagent_api as A
    return await A.q(*a, **k)

async def init():
    await _q(DDL)
    import admin
    admin.allow_viewer_post("/api/app-call/")   # a viewer may use an app; every call it makes is judged again with the viewer's own role
    if "custom_apps" not in admin.RLS_DIRECT: admin.RLS_DIRECT.append("custom_apps")
    admin.RLS_CHILD.setdefault("custom_app_versions", "app_id IN (SELECT id FROM custom_apps)")

def check_files(files: dict, entry: str):
    if not isinstance(files, dict) or not files: raise HTTPException(422, "files must be an object {path: text}")
    if len(files) > MAX_FILES: raise HTTPException(422, f"at most {MAX_FILES} files")
    tot = 0
    for p, t in files.items():
        if not isinstance(p, str) or not PATH_RE.match(p) or ".." in p or p.endswith("/") or not p.lower().endswith(EXT_OK): raise HTTPException(422, f"invalid file path '{str(p)[:60]}' (allowed: {', '.join(EXT_OK)})")
        if not isinstance(t, str) or len(t.encode()) > MAX_FILE: raise HTTPException(413, f"{p}: text up to {MAX_FILE // 1000} KB")
        tot += len(t.encode())
    if tot > MAX_TOTAL: raise HTTPException(413, f"app larger than {MAX_TOTAL // 1000} KB")
    if entry not in files or not entry.lower().endswith(".html"): raise HTTPException(422, "entry must be an .html file of the app")

# ---------------------------------------------------------------- templates
AOS_JS = """<script>
window.aos={_n:0,_p:{},call(method,path,body){return new Promise((res,rej)=>{const id=++this._n;this._p[id]={res,rej};parent.postMessage({aos:1,id,method,path,body},'*');
 setTimeout(()=>{if(this._p[id]){delete this._p[id];rej(new Error('timeout'))}},60000)})},
 json(method,path,body){return this.call(method,path,body).then(r=>{if(r.status>=400)throw new Error((r.json&&r.json.detail)||('HTTP '+r.status));return r.json})}};
addEventListener('message',e=>{const d=e.data;if(!d||!d.aosReply)return;const p=aos._p[d.id];if(p){delete aos._p[d.id];p.res(d)}});
</script>"""
TEMPLATES = {
    "agent-chat": {"title": "Chat with an agent", "description": "Pick an agent, write a message, read the answer (uses /api/v1/threads/.../runs/wait).",
        "files": {"index.html": """<!doctype html><html><head><meta charset="utf-8"><title>Agent chat</title><link rel="stylesheet" href="style.css"></head><body>
<h1>Chat with an agent</h1><select id="ag"></select><div id="log"></div><form id="f"><input id="m" placeholder="Message" autocomplete="off"><button>Send</button></form>
<script src="app.js"></script></body></html>""",
                  "style.css": "body{font:14px system-ui;margin:16px;max-width:640px}#log{margin:12px 0;display:grid;gap:6px}.u{background:#eef;padding:8px;border-radius:8px}.a{background:#f4f4f4;padding:8px;border-radius:8px}input{width:70%;padding:6px}",
                  "app.js": """let tid=null;const log=document.getElementById('log'),ag=document.getElementById('ag');
aos.json('POST','/api/v1/assistants/search',{limit:50}).then(a=>{ag.innerHTML=a.map(x=>`<option value="${x.assistant_id}">${x.name}</option>`).join('')});
document.getElementById('f').onsubmit=async e=>{e.preventDefault();const m=document.getElementById('m');const t=m.value.trim();if(!t)return;m.value='';
 log.insertAdjacentHTML('beforeend','<div class="u"></div>');log.lastChild.textContent=t;
 if(!tid)tid=(await aos.json('POST','/api/v1/threads',{metadata:{source:'custom-app'}})).thread_id;
 const r=await aos.json('POST','/api/v1/threads/'+tid+'/runs/wait',{assistant_id:ag.value,input:{messages:[{type:'human',content:t}]}});
 const last=(r.messages||[]).filter(x=>x.type==='ai').pop();log.insertAdjacentHTML('beforeend','<div class="a"></div>');log.lastChild.textContent=last?(typeof last.content==='string'?last.content:JSON.stringify(last.content)):'(no answer)'}"""}},
    "projects-dashboard": {"title": "Tracing projects overview", "description": "Table of tracing projects with traces and errors of the last 24 h (GET /api/obs/projects).",
        "files": {"index.html": """<!doctype html><html><head><meta charset="utf-8"><title>Projects</title><style>body{font:14px system-ui;margin:16px}table{border-collapse:collapse}td,th{padding:6px 12px;border-bottom:1px solid #ddd;text-align:left}</style></head>
<body><h1>Tracing projects</h1><table id="t"><thead><tr><th>Project</th><th>Traces 24h</th><th>Errors 24h</th></tr></thead><tbody></tbody></table>
<script>aos.json('GET','/api/obs/projects').then(ps=>{document.querySelector('#t tbody').innerHTML=ps.map(p=>'<tr><td></td><td>'+p.traces_24h+'</td><td>'+p.errors_24h+'</td></tr>').join('');
 document.querySelectorAll('#t tbody tr').forEach((tr,i)=>tr.firstChild.textContent=ps[i].name)}).catch(e=>document.body.append('Error: '+e.message))</script></body></html>"""}},
    "blank": {"title": "Blank app", "description": "A single page; use aos.json(method, path, body) to call Agent OS.",
        "files": {"index.html": """<!doctype html><html><head><meta charset="utf-8"><title>My app</title></head><body><h1>Hello</h1><pre id="o"></pre>
<script>aos.json('GET','/api/auth/me').catch(()=>null).then(x=>{document.getElementById('o').textContent='aos.call is ready'})</script></body></html>"""}},
}

def render_frame(app: dict) -> str:
    """One self-contained document: local <script src> / <link href> are inlined (the sandbox has no origin to load them from) + the aos bridge."""
    files = app["files"]
    def inline_js(m):
        src = m.group(1)
        return "<script>" + re.sub(r"</script", r"<\\/script", files[src], flags=re.I) + "</script>" if src in files and src.endswith(".js") else ""
    def inline_css(m):
        src = m.group(1) or m.group(2)
        return "<style>" + re.sub(r"</style", r"<\\/style", files[src], flags=re.I) + "</style>" if src in files and src.endswith(".css") else ""
    h = files[app["entry"]]
    h = re.sub(r'<script\s+[^>]*src="([^"]+)"[^>]*>\s*</script>', inline_js, h, flags=re.I)
    h = re.sub(r'<link\s+[^>]*rel="stylesheet"[^>]*href="([^"]+)"[^>]*>|<link\s+[^>]*href="([^"]+)"[^>]*rel="stylesheet"[^>]*>', inline_css, h, flags=re.I)
    h = re.sub(r"<base\b[^>]*>", "", h, flags=re.I)
    return (AOS_JS + h) if "<head" not in h.lower() else re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + AOS_JS, h, count=1, flags=re.I)

FRAME_CSP = ("sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; media-src data: blob:; "
             "connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'self'")

# ---------------------------------------------------------------- API
class AppIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    template: str | None = None
    files: dict | None = None
    entry: str = "index.html"

def _id(): return secrets.token_hex(6)

@router.get("/api/apps/templates")
async def templates(): return [{"key": k, "title": v["title"], "description": v["description"], "files": sorted(v["files"])} for k, v in TEMPLATES.items()]

@router.get("/api/apps")
async def apps_list():
    return await _q("SELECT a.id, a.name, a.description, a.version, a.entry, a.updated, a.created, u.username created_by, (SELECT sum(length(v::text)) FROM jsonb_each(a.files) j(k, v)) bytes "
                    "FROM custom_apps a LEFT JOIN users u ON u.id=a.created_by ORDER BY a.updated DESC")

@router.post("/api/apps")
async def apps_new(b: AppIn):
    import admin
    files = b.files if b.files is not None else (TEMPLATES.get(b.template or "blank") or {}).get("files")
    if files is None: raise HTTPException(422, "unknown template")
    check_files(files, b.entry)
    if await _q("SELECT 1 FROM custom_apps WHERE lower(name)=lower(%s)", (b.name,), one=True): raise HTTPException(409, "An app with this name already exists in the workspace")
    aid = _id(); uid = (admin.cur() or {}).get("id")
    await _q("INSERT INTO custom_apps(id,workspace_id,name,description,files,entry,created_by,updated_by) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)", (aid, admin.cur_ws(), b.name, b.description, Jsonb(files), b.entry, uid, uid))
    await _q("INSERT INTO custom_app_versions(app_id,version,files,entry,created_by) VALUES(%s,1,%s,%s,%s)", (aid, Jsonb(files), b.entry, uid))
    return {"id": aid, "version": 1, "view_url": f"/apps/view/{aid}"}

async def _get(aid):
    r = await _q("SELECT * FROM custom_apps WHERE id=%s", (aid,), one=True) if re.fullmatch(r"[0-9a-f]{12}", aid) else None
    if not r: raise HTTPException(404, "App not found")
    return r

@router.get("/api/apps/{aid}")
async def apps_get(aid: str):
    return await _get(aid)

class AppPut(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    files: dict | None = None
    entry: str | None = None

@router.put("/api/apps/{aid}")
async def apps_put(aid: str, b: AppPut):
    import admin
    a = await _get(aid); files = b.files if b.files is not None else a["files"]; entry = b.entry or a["entry"]; check_files(files, entry)
    if b.name and b.name.lower() != a["name"].lower() and await _q("SELECT 1 FROM custom_apps WHERE lower(name)=lower(%s) AND id<>%s", (b.name, aid), one=True): raise HTTPException(409, "An app with this name already exists")
    uid = (admin.cur() or {}).get("id"); v = a["version"] + (1 if (b.files is not None or b.entry) else 0)
    await _q("UPDATE custom_apps SET name=%s, description=%s, files=%s, entry=%s, version=%s, updated=now(), updated_by=%s WHERE id=%s", (b.name or a["name"], a["description"] if b.description is None else b.description, Jsonb(files), entry, v, uid, aid))
    if v != a["version"]: await _q("INSERT INTO custom_app_versions(app_id,version,files,entry,created_by) VALUES(%s,%s,%s,%s,%s)", (aid, v, Jsonb(files), entry, uid))
    return {"id": aid, "version": v}

@router.delete("/api/apps/{aid}")
async def apps_del(aid: str):
    await _get(aid); await _q("DELETE FROM custom_apps WHERE id=%s", (aid,)); return {"ok": True}

@router.get("/api/apps/{aid}/versions")
async def apps_versions(aid: str):
    await _get(aid)
    return await _q("SELECT v.version, v.created, u.username created_by, (SELECT count(*) FROM jsonb_object_keys(v.files)) files FROM custom_app_versions v LEFT JOIN users u ON u.id=v.created_by "
                    "WHERE v.app_id=%s ORDER BY v.version DESC", (aid,))

class Restore(BaseModel):
    version: int

@router.post("/api/apps/{aid}/restore")
async def apps_restore(aid: str, b: Restore):
    v = await _q("SELECT * FROM custom_app_versions WHERE app_id=%s AND version=%s", (aid, b.version), one=True)
    if not v: raise HTTPException(404, "Version not found")
    return await apps_put(aid, AppPut(files=v["files"], entry=v["entry"]))

# ---------------------------------------------------------------- serving (host page + sandboxed frame)
HOST = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>html,body{{height:100%;margin:0;font:14px system-ui,sans-serif}}header{{display:flex;gap:10px;align-items:center;padding:8px 14px;border-bottom:1px solid #e3e3e3}}
header a{{color:#444;text-decoration:none}}header b{{font-size:15px}}.muted{{color:#777;font-size:12px}}iframe{{border:0;width:100%;height:calc(100% - 45px)}}</style></head>
<body><header><a href="/apps">&larr; Custom Apps</a><b>{title}</b><span class="muted">v{version} · runs in a sandbox with no network · API calls use your own permissions</span></header>
<iframe id="f" sandbox="allow-scripts" src="/apps/frame/{aid}" referrerpolicy="no-referrer"></iframe>
<script>
const AID={aid_json},f=document.getElementById('f');
addEventListener('message',async e=>{{
  if(e.source!==f.contentWindow)return;const d=e.data;if(!d||d.aos!==1||typeof d.id!=='number')return;
  const reply=(o)=>f.contentWindow.postMessage({{aosReply:1,id:d.id,...o}},'*');
  const send=c=>fetch('/api/app-call/'+AID,{{method:'POST',credentials:'same-origin',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{method:String(d.method||'GET'),path:String(d.path||''),body:d.body===undefined?null:d.body,confirmed:c}})}});
  try{{let r=await send(false);let j=await r.json().catch(()=>null);
    if(r.status===428&&j&&j.detail&&j.detail.code==='confirm_write'){{   /* 9.Z: writes need the viewer's OK */
      if(!confirm('Ứng dụng này muốn thực hiện thao tác ghi bằng quyền của bạn / This app wants to make a change with your permissions:

'+j.detail.method+' '+j.detail.path+'

Cho phép? / Allow?')){{reply({{status:403,json:{{detail:'Denied by the viewer'}}}});return}}
      r=await send(true);j=await r.json().catch(()=>null)}}
    reply(r.ok?{{status:j.status,json:j.json,text:j.text}}:{{status:r.status,json:j}})}}catch(x){{reply({{status:0,json:{{detail:String(x)}}}})}}
}});
</script></body></html>"""

@router.get("/apps/view/{aid}")
async def apps_view(aid: str):
    a = await _get(aid)
    from html import escape
    return HTMLResponse(HOST.format(title=escape(a["name"]), version=a["version"], aid=aid, aid_json=json.dumps(aid)), headers={"Content-Security-Policy": "frame-src 'self'; frame-ancestors 'self'", "Cache-Control": "no-store"})

@router.get("/apps/frame/{aid}")
async def apps_frame(aid: str):
    a = await _get(aid)
    return HTMLResponse(render_frame(a), headers={"Content-Security-Policy": FRAME_CSP, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})

# ---------------------------------------------------------------- the bridge: perform the app's call with the viewer's own credentials
class CallIn(BaseModel):
    method: str = Field(max_length=10)
    path: str = Field(max_length=2000)
    body: object | None = None
    confirmed: bool = False   # 9.Z: the viewer clicked "Allow" for this write call

_RATE: dict = defaultdict(deque)
FWD = ("cookie", "authorization", "x-api-key", "x-workspace-id", "accept-language", "traceparent")

@router.post("/api/app-call/{aid}")
async def apps_call(aid: str, b: CallIn, request: Request):
    import admin, httpx
    a = await _get(aid)
    why = blocked(b.method, b.path)
    if why: raise HTTPException(403, f"Blocked for custom apps: {why}")
    u = admin.cur() or {}
    # 9.Z review: an app runs with the VIEWER's permissions, so an editor could publish an app that performs admin writes when an admin opens it.
    # Every write (non-GET) needs the viewer's explicit confirmation unless the viewer is the person who last changed the app's code.
    if b.method.upper() != "GET" and not b.confirmed and u.get("id") != (a.get("updated_by") or a.get("created_by")):
        raise HTTPException(428, {"code": "confirm_write", "method": b.method.upper(), "path": b.path[:300]})
    w = _RATE[(u.get("id"), aid)]; now = time.time()
    while w and now - w[0] > 60: w.popleft()
    if len(w) >= 120: raise HTTPException(429, "This app made too many calls (120 per minute)")
    w.append(now)
    hdr = {k: v for k, v in request.headers.items() if k.lower() in FWD}; hdr["x-aos-custom-app"] = aid
    kw = {}
    if b.body is not None and b.method.upper() != "GET": kw["content"] = json.dumps(b.body).encode(); hdr["content-type"] = "application/json"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=request.app), base_url="http://aos.internal", timeout=120, follow_redirects=False) as h:
        r = await h.request(b.method.upper(), b.path, headers=hdr, **kw)
    ct = r.headers.get("content-type", "")
    out = {"status": r.status_code}
    if "json" in ct:
        try: out["json"] = r.json()
        except ValueError: out["text"] = r.text[:200000]
    else: out["text"] = r.text[:200000]
    return out
