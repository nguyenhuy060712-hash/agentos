"""App shell theo bố cục LangSmith agent-based (spec §2): sidebar trái, top bar (agent switcher + breadcrumb), ⌘K, dark mode.
Design tokens lấy giá trị từ Macaw design system (MIT © 2026 LangChain, Inc. — static/licenses/Macaw-MIT.txt).
Font Inter / Fira Code (OFL) và icon Lucide (ISC) phục vụ tại chỗ từ /static, không CDN."""
from pathlib import Path
SPRITE = (Path(__file__).parent / "static" / "icons.svg").read_text()

FONTS = """@font-face{font-family:'Inter';font-style:normal;font-display:swap;font-weight:100 900;src:url(static/fonts/inter-vietnamese-wght-normal.woff2) format('woff2-variations');unicode-range:U+0102-0103,U+0110-0111,U+0128-0129,U+0168-0169,U+01A0-01A1,U+01AF-01B0,U+0300-0301,U+0303-0304,U+0308-0309,U+0323,U+0329,U+1EA0-1EF9,U+20AB}
@font-face{font-family:'Inter';font-style:normal;font-display:swap;font-weight:100 900;src:url(static/fonts/inter-latin-ext-wght-normal.woff2) format('woff2-variations');unicode-range:U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+0304,U+0308,U+0329,U+1D00-1DBF,U+1E00-1E9F,U+1EF2-1EFF,U+2020,U+20A0-20AB,U+20AD-20C0,U+2113,U+2C60-2C7F,U+A720-A7FF}
@font-face{font-family:'Inter';font-style:normal;font-display:swap;font-weight:100 900;src:url(static/fonts/inter-latin-wght-normal.woff2) format('woff2-variations');unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+02BB-02BC,U+02C6,U+02DA,U+02DC,U+0304,U+0308,U+0329,U+2000-206F,U+20AC,U+2122,U+2191,U+2193,U+2212,U+2215,U+FEFF,U+FFFD}
@font-face{font-family:'Fira Code';font-style:normal;font-display:swap;font-weight:400;src:url(static/fonts/fira-code-latin-ext-400-normal.woff2) format('woff2');unicode-range:U+0100-02BA,U+02BD-02C5,U+02C7-02CC,U+02CE-02D7,U+02DD-02FF,U+1E00-1E9F,U+1EF2-1EFF}
@font-face{font-family:'Fira Code';font-style:normal;font-display:swap;font-weight:400;src:url(static/fonts/fira-code-latin-400-normal.woff2) format('woff2');unicode-range:U+0000-00FF,U+0131,U+0152-0153,U+2000-206F}
"""

TOKENS = """:root{--s1:#ffffff;--s2:#f5f8fb;--s3:#edf2f7;--s4:#e2e8f0;--el:#ffffff;--brand:#006ddd;--brand-h:#1566b8;--sel:#e5f4ff;
--t1:#0f172a;--t2:#334155;--t3:#475569;--ph:#94a3b8;--link:#0078f1;--bd:#cbd5e1;--bd2:#e5e7eb;--bd3:#edf2f7;
--ok-bg:#ecfdf3;--ok:#047647;--er-bg:#fef3f2;--er:#b42318;--wn-bg:#fffaeb;--wn:#ac5305;--dot-g:#02ae45;--dot-o:#ee7207;--dot-y:#c98e06;--dot-r:#f97066;--pu:#e3d9ff;--pu-t:#5f3dc4;
--sh-sm:0 1px 3px rgba(16,24,40,.1),0 1px 2px rgba(16,24,40,.06);--sh-md:0 4px 6px -1px rgba(16,24,40,.1),0 2px 4px -1px rgba(16,24,40,.06);
--sh-lg:0 12px 16px -4px rgba(16,24,40,.08),0 4px 6px -2px rgba(16,24,40,.03);--r-sm:4px;--r-md:6px;--r-lg:8px;--r-xl:12px;
--font:'Inter',system-ui,-apple-system,'Segoe UI',sans-serif;--mono:'Fira Code',ui-monospace,SFMono-Regular,Menlo,monospace}
html.dark{--s1:#09090f;--s2:#111521;--s3:#1b2030;--s4:#282e42;--el:#0d0f18;--brand:#0078f1;--brand-h:#008dff;--sel:#0c336a;
--t1:#f5f8fb;--t2:#e2e8f0;--t3:#cbd5e1;--ph:#555d78;--link:#5fbef8;--bd:#393f55;--bd2:#282e42;--bd3:#1b2030;
--ok-bg:#053321;--ok:#75e0a7;--er-bg:#55160c;--er:#fda29b;--wn-bg:#4e1d09;--wn:#fec84b;--pu:#190d38;--pu-t:#bdb4fe;color-scheme:dark}
"""

CSS = FONTS + TOKENS + """*{box-sizing:border-box}
html,body{height:100%}body{font-family:var(--font);font-size:14px;line-height:1.5;margin:0;background:var(--s1);color:var(--t1);-webkit-font-smoothing:antialiased}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
.ic{width:16px;height:16px;flex:none;stroke:currentColor;fill:none;stroke-width:1.75;stroke-linecap:round;stroke-linejoin:round;vertical-align:-3px}
/* ---- shell ---- */
#app{display:grid;grid-template-columns:220px 1fr;height:100vh}#app.col{grid-template-columns:48px 1fr}
#sb{background:var(--s2);border-right:1px solid var(--bd2);display:flex;flex-direction:column;min-height:0;overflow:hidden}
#sb .ws{display:flex;align-items:center;gap:8px;padding:10px 10px 6px;font-weight:600;font-size:13px;cursor:pointer;position:relative}
.tile{width:22px;height:22px;border-radius:var(--r-sm);background:var(--brand);color:#fff;display:inline-flex;align-items:center;justify-content:center;font-size:11px;font-weight:600;flex:none}
#sb .ws .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#sb .srch{margin:4px 8px 6px;display:flex;align-items:center;gap:8px;padding:5px 8px;border:1px solid var(--bd2);border-radius:var(--r-md);background:var(--s1);color:var(--t3);font-size:13px;cursor:pointer}
kbd{font-family:var(--font);font-size:11px;border:1px solid var(--bd);border-bottom-width:2px;border-radius:var(--r-sm);padding:0 4px;color:var(--t3);background:var(--s1)}
#sb nav{flex:1;overflow:auto;padding:2px 8px}#sb nav a{display:flex;align-items:center;gap:9px;height:30px;padding:0 8px;border-radius:var(--r-md);color:var(--t2);font-size:13px;white-space:nowrap;text-decoration:none}
#sb nav a:hover{background:var(--s3)}#sb nav a.on{background:var(--sel);color:var(--t1);font-weight:500;box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--brand) 35%,transparent)}
#sb nav a .lb{flex:1;overflow:hidden;text-overflow:ellipsis}#sb nav .cnt{color:var(--t3);font-size:12px}#sb nav hr{border:0;border-top:1px solid var(--bd2);margin:6px 4px}
.beta{font-size:10px;padding:0 6px;border-radius:99px;background:var(--s4);color:var(--t3);font-weight:500;line-height:16px}
#sb .more summary{list-style:none;display:flex;align-items:center;gap:9px;height:30px;padding:0 8px;color:var(--t3);font-size:12px;cursor:pointer;border-radius:var(--r-md)}#sb .more summary::-webkit-details-marker{display:none}
#sb .foot{border-top:1px solid var(--bd2);padding:6px 8px 8px}#sb .user{display:flex;gap:8px;align-items:center;padding:6px 8px;border-radius:var(--r-md);cursor:pointer;position:relative}#sb .user:hover{background:var(--s3)}
#sb .user .u1{font-size:13px;font-weight:600;line-height:1.2}#sb .user .u2{font-size:12px;color:var(--t3);line-height:1.2}
#app.col #sb .lb,#app.col #sb .cnt,#app.col #sb .beta,#app.col #sb .nm,#app.col #sb .srch span,#app.col #sb .srch kbd,#app.col #sb .user div,#app.col #sb .more summary span{display:none}
#mainc{display:flex;flex-direction:column;min-width:0;min-height:0}
#tb{height:44px;flex:none;display:flex;align-items:center;gap:8px;padding:0 12px;border-bottom:1px solid var(--bd2);background:var(--s1)}
#tb .crumb{color:var(--t3);font-size:13px;display:flex;gap:6px;align-items:center;white-space:nowrap;overflow:hidden}#tb .crumb b{color:var(--t1);font-weight:500}
.agsw{display:inline-flex;align-items:center;gap:6px;padding:3px 8px;border-radius:var(--r-md);cursor:pointer;font-size:13px;font-weight:500;position:relative}.agsw:hover{background:var(--s3)}
.agtile{width:20px;height:20px;border-radius:var(--r-sm);font-size:10px;font-weight:700;display:inline-flex;align-items:center;justify-content:center;color:#fff}
#content{flex:1;overflow:auto;min-height:0}
.menu{position:absolute;z-index:50;background:var(--el);border:1px solid var(--bd2);border-radius:var(--r-lg);box-shadow:var(--sh-lg);padding:4px;min-width:240px;font-weight:400}
.menu .mi{display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:var(--r-md);font-size:13px;color:var(--t1);cursor:pointer;text-decoration:none}.menu .mi:hover,.menu .mi.hl{background:var(--s3);text-decoration:none}
.menu .mh{font-size:11px;color:var(--t3);padding:6px 8px 2px;text-transform:uppercase;letter-spacing:.04em}
#ck{position:fixed;inset:0;background:rgba(15,23,42,.35);z-index:90;display:none;align-items:flex-start;justify-content:center;padding-top:12vh}#ck.on{display:flex}
#ck .box{width:560px;max-width:92vw;background:var(--el);border-radius:var(--r-xl);box-shadow:var(--sh-lg);border:1px solid var(--bd2);overflow:hidden}
#ck input{width:100%;border:0;border-bottom:1px solid var(--bd2);border-radius:0;padding:12px 14px;font-size:15px;outline:none;background:transparent}
#ck .res{max-height:50vh;overflow:auto;padding:4px}
.sbtop{display:flex;flex-direction:column;gap:6px;padding:10px 8px 8px}.newag{justify-content:center;height:30px}.sbtop .agsw{border:1px solid var(--bd2);background:var(--s1);justify-content:space-between}
.sbh{font-size:11px;font-weight:600;letter-spacing:.06em;color:var(--t3);padding:12px 8px 4px}#tb .srch{display:flex;align-items:center;gap:8px;padding:3px 8px;border:1px solid var(--bd2);border-radius:var(--r-md);color:var(--t3);font-size:13px;cursor:pointer;width:240px;height:28px}
#app.col .sbh,#app.col .sbtop .agsw #agname{display:none}#app.st #sb{background:var(--s1)}
html.embed #sb,html.embed #tb{display:none}html.embed #app{grid-template-columns:1fr}
/* ---- components ---- */
main{padding:16px 20px;display:grid;gap:16px;max-width:1600px}
.card{background:var(--s1);border:1px solid var(--bd2);border-radius:var(--r-lg);padding:14px 16px;overflow:auto}
h1{font-size:20px;font-weight:600;margin:0}h2{font-size:16px;font-weight:600;margin:0 0 10px}h3{font-size:14px;font-weight:600;margin:0 0 8px}
.h4{font-size:12px;text-transform:uppercase;letter-spacing:.06em;font-weight:600;color:var(--t2)}
button,.btn{display:inline-flex;align-items:center;gap:6px;height:32px;padding:0 12px;border:1px solid transparent;border-radius:var(--r-md);background:var(--brand);color:#fff;font:500 13px var(--font);cursor:pointer;white-space:nowrap;text-decoration:none}
button:hover,.btn:hover{background:var(--brand-h);text-decoration:none}
button.g,.btn.g,button.s,.btn.s{background:var(--s1);color:var(--t1);border-color:var(--bd)}button.g:hover,button.s:hover,.btn.g:hover{background:var(--s3)}
button.s{height:24px;padding:0 8px;font-size:12px}button.r{background:var(--s1);color:var(--er);border-color:var(--bd)}button.r:hover{background:var(--er-bg)}
button.gh{background:transparent;color:var(--t3);border:0;padding:0 6px;height:28px}button.gh:hover{background:var(--s3);color:var(--t1)}button:disabled{opacity:.5;cursor:default}
input,select,textarea{height:32px;padding:0 10px;font:13px var(--font);color:var(--t1);background:var(--s1);border:1px solid var(--bd);border-radius:var(--r-md)}
textarea{height:auto;padding:8px 10px;width:100%;font-family:var(--mono);font-size:12.5px}input:focus,select:focus,textarea:focus{outline:2px solid color-mix(in srgb,var(--brand) 30%,transparent);border-color:var(--brand)}
input::placeholder,textarea::placeholder{color:var(--ph)}input[type=checkbox],input[type=radio]{height:auto;accent-color:var(--brand)}
table{border-collapse:collapse;width:100%;font-size:13px}th{font-weight:500;color:var(--t3);font-size:12px;text-align:left;padding:7px 8px;border-bottom:1px solid var(--bd2);white-space:nowrap;background:var(--s1);position:sticky;top:0}
td{border-bottom:1px solid var(--bd3);padding:8px;text-align:left;vertical-align:top}tr.r:hover,tbody tr:hover{background:var(--s2)}tr.r{cursor:pointer}
.err{color:var(--er)}.ok{color:var(--ok)}pre,code,.mono{font-family:var(--mono);font-size:12px}
pre{white-space:pre-wrap;background:var(--s2);border:1px solid var(--bd3);padding:8px 10px;border-radius:var(--r-md);margin:4px 0;max-height:420px;overflow:auto}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
label{font-size:12px;color:var(--t2);display:block;margin-top:8px;font-weight:500}
.tag,.chip{display:inline-flex;align-items:center;gap:4px;padding:0 7px;height:20px;border-radius:var(--r-md);font-size:11.5px;background:var(--s3);color:var(--t2);margin:1px;white-space:nowrap}
.tag.ro,.chip.ok{background:var(--ok-bg);color:var(--ok)}.tag.bl,.chip.er{background:var(--er-bg);color:var(--er)}.chip.wn{background:var(--wn-bg);color:var(--wn)}.tag.off{background:var(--s2);color:var(--ph)}
.chip.pu{background:var(--pu);color:var(--pu-t)}.chip.id{font-family:var(--mono);cursor:pointer;border:1px solid var(--bd2);background:var(--s1)}
.pill{border-radius:99px}.dot{width:8px;height:8px;border-radius:99px;display:inline-block;background:var(--dot-g)}.dot.r{background:var(--dot-r)}.dot.o{background:var(--dot-o)}.dot.y{background:var(--dot-y)}
.kpi{display:flex;gap:0;flex-wrap:wrap;border:1px solid var(--bd2);border-radius:var(--r-lg);overflow:hidden}.kpi div{padding:10px 16px;min-width:130px;font-size:12px;color:var(--t3);border-right:1px solid var(--bd2)}.kpi b{display:block;font-size:20px;color:var(--t1);font-weight:600}
.seg{display:inline-flex;background:var(--s3);border-radius:var(--r-md);padding:2px;gap:2px}.seg button{height:26px;background:transparent;color:var(--t3);border:0;font-weight:500}.seg button.on{background:var(--s1);color:var(--t1);box-shadow:var(--sh-sm)}
.tabs{display:flex;gap:18px;border-bottom:1px solid var(--bd2);margin-bottom:12px}.tabs a,.tabs button{background:none;border:0;border-bottom:2px solid transparent;border-radius:0;color:var(--t3);padding:0 0 8px;height:auto;font-weight:500;text-decoration:none}
.tabs .on{color:var(--t1);border-bottom-color:var(--t1)}.ptabs{display:flex;gap:4px}.ptabs button{height:26px;background:transparent;color:var(--t3);border:0}.ptabs button.on{background:var(--s3);color:var(--t1)}
.pane{position:fixed;top:0;right:0;bottom:0;width:min(1100px,76vw);background:var(--el);border-left:1px solid var(--bd2);box-shadow:var(--sh-lg);z-index:60;display:flex;flex-direction:column;transform:translateX(102%);transition:transform .2s}.pane.on{transform:none}
.bulk{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);background:var(--el);border:1px solid var(--bd2);box-shadow:var(--sh-lg);border-radius:var(--r-lg);padding:8px 10px;display:none;gap:8px;align-items:center;z-index:55;font-size:13px}.bulk.on{display:flex}
.empty{text-align:center;padding:48px 16px;color:var(--t3)}.empty .tile{width:40px;height:40px;margin-bottom:10px;background:var(--sel);color:var(--brand)}.empty b{display:block;color:var(--t1);font-size:15px;margin-bottom:4px}
.lat{font-family:var(--mono)}.muted{color:var(--t3)}small{font-size:12px}
.phead{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.phead .sp,.sp{flex:1}
@media(max-width:800px){.grid2{grid-template-columns:1fr}#app{grid-template-columns:48px 1fr}}
"""

# Sidebar per the owner's real LangSmith account (agent-based, Oct 2026). (key, href, label, icon, extra)
NAV = [
    [("engine", "engine", "Engine", "zap", ""), ("studio", "studio", "Studio", "workflow", ""), ("builder", "builder", "Build", "hammer", ""),
     ("projects", "projects", "Tracing", "activity", "c:runs"), ("monitoring", "monitoring", "Monitoring", "chart-line", ""), ("insights", "insights", "Insights", "chart-column", ""),
     ("datasets", "datasets", "Datasets & Experiments", "database", "c:datasets"), ("evaluators", "evaluators", "Evaluations", "gauge", ""),
     ("annotation", "annotation", "Annotation Queues", "pencil-line", ""), ("hub", "hub", "Prompts", "message-square-text", "c:prompts"),
     ("playground", "playground", "Playground", "flask-conical", ""), ("deployments", "deployments", "Deployments", "rocket", "c:deployments"),
     ("context", "context", "Context Hub", "library", ""), ("skills", "skills", "Skills", "sparkles", "")],
]
WORKSPACE = [("dashboards", "dashboards", "Dashboards", "layout-dashboard"), ("apps", "apps", "Custom Apps", "box"),
             ("gateway", "gateway", "LLM Gateway", "network"), ("sandboxes", "sandboxes", "Sandboxes", "terminal")]
MORE = [("overview", "overview", "Agent Overview", "layers"), ("chat", "./", "Chat", "message-circle"), ("listeners", "listeners", "Listeners", "bell"),
        ("tools", "tools", "Tools", "wrench"), ("memory", "memory", "Memory", "library"), ("fleet", "fleet", "Fleet", "bot"), ("approvals", "approvals", "Inbox", "inbox"),
        ("simulations", "simulations", "Simulations", "repeat"), ("server", "server", "Agent Server", "server"), ("srvcfg", "server-config", "Server settings", "settings"),
        ("rules", "rules", "Automations", "zap"), ("gwkeys", "gwkeys", "Gateway Keys", "key-round"), ("admin", "admin", "Members & Access", "users")]
ALIAS = {"experiments": "datasets", "agents": "overview"}

def icon(n, cls="ic"): return f'<svg class="{cls}"><use href="#i-{n}"/></svg>'

def section_label(active):
    a = ALIAS.get(active, active)
    for k, _, n, *_ in [x for g in NAV for x in g] + WORKSPACE + MORE:
        if k == a: return n
    return "Settings" if a == "settings" else ""

def _item(k, h, n, ic, tail, a):
    return f'<a href="{h}" class="{"on" if k == a else ""}" title="{n}">{icon(ic)}<span class="lb">{n}</span>{tail}</a>'

def sidebar(active):
    a = ALIAS.get(active, active); out = []
    for k, h, n, ic, ex in NAV[0]:
        tail = f'<span class="cnt" data-c="{ex[2:]}"></span>' if ex.startswith("c:") else ""
        out.append(_item(k, h, n, ic, tail, a))
    out.append('<div class="sbh"><span class="lb">WORKSPACE</span></div>')
    out += [_item(k, h, n, ic, "", a) for k, h, n, ic in WORKSPACE]
    more_open = any(k == a for k, *_ in MORE)
    out.append(f'<details class="more" {"open" if more_open else ""}><summary>{icon("ellipsis")}<span>More</span></summary>'
               + "".join(_item(k, h, n, ic, "", a) for k, h, n, ic in MORE) + "</details>")
    return "".join(out)

def shell(active, title, body):
    a = ALIAS.get(active, active)
    if a == "settings":   # Settings replaces the sidebar with its own navigation (rendered by the page)
        return (f'{SPRITE}<div id="app" class="st"><aside id="sb"><div id="setnav"></div></aside><div id="mainc"><header id="tb"><span class="crumb"><b>Settings</b></span><span class="sp"></span>'
                f'<span id="langx"></span><button class="gh" id="thbtn" title="Theme">{icon("moon")}</button></header><div id="content">{body}</div></div></div>'
                f'<div id="ck"><div class="box"><input id="ckq" placeholder="Search…" autocomplete="off"><div class="res" id="ckres"></div></div></div>')
    return (f'{SPRITE}<div id="app"><aside id="sb"><div class="sbtop"><button id="newag" data-menu="1" class="newag">{icon("plus")}<span class="lb">Agent</span></button>'
            f'<span class="agsw" id="agbtn"><span class="agtile" id="agtile" style="background:var(--t3)">{icon("layers","ic")}</span><span id="agname" class="lb">All agents</span>{icon("chevrons-up-down")}</span></div>'
            f'<nav>{sidebar(active)}</nav><div class="foot"><nav style="padding:0"><a href="settings" class="{"on" if a == "settings" else ""}">{icon("settings")}<span class="lb">Settings</span></a></nav>'
            f'<div class="row" style="flex-wrap:nowrap;gap:2px"><div class="user" id="ubtn" style="flex:1;min-width:0"><span class="tile" id="utile" style="background:var(--t2)">?</span><div style="min-width:0"><div class="u1" id="uname">…</div><div class="u2" id="urole"></div></div></div>'
            f'<button class="gh" id="colbtn" title="Collapse">{icon("panel-left")}</button></div></div></aside>'
            f'<div id="mainc"><header id="tb"><span class="crumb"><b>{section_label(active) or title}</b></span><span class="sp"></span>'
            f'<div class="srch" id="ckbtn">{icon("search")}<span style="flex:1">Search</span><kbd>Ctrl</kbd><kbd>K</kbd></div>'
            f'<button class="g" id="askbtn" data-ro-ok="1" style="height:28px" title="Trợ lý Agent OS (Ctrl+I)"><span aria-hidden="true">✦</span><span class="lb">Trợ lý</span><kbd>Ctrl</kbd><kbd>I</kbd></button><a class="btn g" href="settings?s=members&invite=1" style="height:28px">{icon("users")}Invite your team</a>'
            f'<span id="langx"></span><button class="gh" id="thbtn" title="Theme">{icon("moon")}</button><span id="wsbtn" style="display:none"><span id="wstile"></span><span id="wsname"></span></span></header>'
            f'<div id="content">{body}</div></div></div>'
            f'<div id="ck"><div class="box"><input id="ckq" placeholder="Search pages, agents…" autocomplete="off"><div class="res" id="ckres"></div></div></div>')

SHELL_JS = r"""
(function(){
const ICN=n=>`<svg class="ic"><use href="#i-${n}"/></svg>`;window.ICN=ICN;
const html=document.documentElement;if(new URLSearchParams(location.search).get('embed'))html.classList.add('embed');const th=localStorage.getItem('aos_theme')||'light';
function applyTheme(t){html.classList.toggle('dark',t==='dark'||(t==='system'&&matchMedia('(prefers-color-scheme: dark)').matches))}
applyTheme(th);
const app=document.getElementById('app');if(!app)return;
if(localStorage.getItem('aos_sb')==='col')app.classList.add('col');
(document.getElementById('colbtn')||{setAttribute(){}}).onclick=()=>{app.classList.toggle('col');localStorage.setItem('aos_sb',app.classList.contains('col')?'col':'')};
const thb=document.getElementById('thbtn');const thIcon=()=>thb.innerHTML=ICN(html.classList.contains('dark')?'sun':'moon');thIcon();
thb.onclick=()=>{const t=html.classList.contains('dark')?'light':'dark';localStorage.setItem('aos_theme',t);applyTheme(t);thIcon()};
function closeMenus(){document.querySelectorAll('.menu.pop').forEach(m=>m.remove())}
document.addEventListener('click',e=>{if(!e.target.closest('.menu.pop')&&!e.target.closest('[data-menu]'))closeMenus()});
function popup(anchor,inner,opt={}){closeMenus();const m=document.createElement('div');m.className='menu pop';m.innerHTML=inner;
 const r=anchor.getBoundingClientRect();m.style.position='fixed';m.style.left=(opt.right?Math.max(8,r.right-260):r.left)+'px';
 if(opt.up){m.style.bottom=(innerHeight-r.top+4)+'px'}else m.style.top=(r.bottom+4)+'px';document.body.appendChild(m);const w=m.getBoundingClientRect();if(w.right>innerWidth-8)m.style.left=Math.max(8,innerWidth-w.width-8)+'px';return m}
window.aosPopup=popup;
const COLORS=['#006ddd','#7a5af8','#0e9384','#dd2590','#e04f16','#15b79e','#6172f3','#ca8504'];
const agColor=id=>COLORS[(+id||0)%COLORS.length];const ini=s=>String(s||'?').replace(/[^\p{L}\p{N} ]/gu,'').trim().split(/\s+/).map(w=>w[0]).join('').slice(0,2).toUpperCase()||'A';
window.agTile=(a)=>`<span class="agtile" style="background:${agColor(a.id)}">${ini(a.name)}</span>`;
let AG=[],ME=null;
function curAgent(){const q=new URLSearchParams(location.search).get('agent');return q||localStorage.getItem('aos_agent')||''}
window.aosAgent=()=>{const id=curAgent();return AG.find(a=>String(a.id)===String(id))||null};
function paintAgent(){const a=window.aosAgent();const t=document.getElementById('agtile'),n=document.getElementById('agname');
 if(a&&t&&n){t.outerHTML=`<span id="agtile">${agTile(a)}</span>`;n.textContent=a.name}}
function setAgent(id){if(id)localStorage.setItem('aos_agent',id);else localStorage.removeItem('aos_agent');
 const u=new URL(location.href);if(id)u.searchParams.set('agent',id);else u.searchParams.delete('agent');location.href=u.pathname.replace(/^\//,'')+u.search||'./'}
window.aosSetAgent=setAgent;
(document.getElementById('agbtn')||{setAttribute(){}}).setAttribute('data-menu','1');
(document.getElementById('agbtn')||{setAttribute(){}}).onclick=e=>{const cur=curAgent();
 const m=popup(e.currentTarget,`<div class="row" style="padding:4px;gap:4px;flex-wrap:nowrap"><input id="agq" placeholder="Search agents" style="flex:1"><a class="btn g" href="builder" title="New agent" style="padding:0 8px">${ICN('plus')}</a></div><div id="agl"></div>`);
 const draw=q=>{m.querySelector('#agl').innerHTML=`<a class="mi" data-id="">${ICN('layers')}<span style="flex:1">All agents</span>${cur?'':ICN('check')}</a>`+
  AG.filter(a=>!q||a.name.toLowerCase().includes(q)).map(a=>`<a class="mi" data-id="${a.id}">${agTile(a)}<span style="flex:1">${esc(a.name)}</span>${String(a.id)===String(cur)?ICN('check'):''}</a>`).join('')};
 draw('');const qi=m.querySelector('#agq');qi.focus();qi.oninput=()=>draw(qi.value.toLowerCase());
 m.onclick=ev=>{const it=ev.target.closest('[data-id]');if(it)setAgent(it.dataset.id)}};
(document.getElementById('wsbtn')||{setAttribute(){}}).setAttribute('data-menu','1');
(document.getElementById('wsbtn')||{setAttribute(){}}).onclick=e=>{if(!ME)return;const m=popup(e.currentTarget,`<div class="mh">Workspaces</div>`+(ME.workspaces||[]).map(w=>`<a class="mi" data-ws="${w.id}"><span class="tile">${ini(w.name)}</span><span style="flex:1">${esc(w.name)}</span><span class="muted mono" style="font-size:11px">#${w.id}</span><span data-copy="${w.id}" title="Copy ID" style="cursor:copy;padding:0 4px;font-size:12px">&#x2398;</span>${w.id==ME.ws?ICN('check'):''}</a>`).join('')+`<hr style="border:0;border-top:1px solid var(--bd2)"><a class="mi" href="fleet">${ICN('bot')}Fleet</a>`);
 m.onclick=async ev=>{const cp=ev.target.closest('[data-copy]');if(cp){ev.stopPropagation();try{await navigator.clipboard.writeText(cp.dataset.copy);flash('Workspace ID copied')}catch(x){flash(cp.dataset.copy)}return}
 const it=ev.target.closest('[data-ws]');if(!it)return;try{await api('api/auth/workspace','POST',{workspace_id:+it.dataset.ws});try{localStorage.setItem(WSKEY(),it.dataset.ws)}catch(x){}location.reload()}catch(x){flash(x.message,1)}}};
(document.getElementById('ubtn')||{setAttribute(){}}).setAttribute('data-menu','1');
(document.getElementById('ubtn')||{setAttribute(){}}).onclick=e=>{const t=localStorage.getItem('aos_theme')||'light';
 const m=popup(e.currentTarget,`<div class="mi muted" style="cursor:default">${esc(ME?ME.username:'')}</div><hr style="border:0;border-top:1px solid var(--bd2)"><a class="mi" href="settings?s=members">${ICN('users')}Invitations</a><a class="mi" href="docs" target="_blank">${ICN('file-text')}Documentation</a><a class="mi" href="docs#/" target="_blank">${ICN('circle-help')}Get help</a>
  <div class="mh">Theme</div>`+['system','light','dark'].map(x=>`<a class="mi" data-th="${x}">${ICN(x==='dark'?'moon':x==='light'?'sun':'settings')}<span style="flex:1">${x[0].toUpperCase()+x.slice(1)}</span>${t===x?ICN('check'):''}</a>`).join('')+
  `<div class="mh">Language</div><a class="mi" data-lg="en">English${(window.LANG||'en')==='en'?' '+ICN('check'):''}</a><a class="mi" data-lg="vi">Tiếng Việt${window.LANG==='vi'?' '+ICN('check'):''}</a><hr style="border:0;border-top:1px solid var(--bd2)"><a class="mi" data-lo="1">${ICN('log-out')}Log out</a>`,{up:true});
 m.onclick=async ev=>{const a=ev.target.closest('a');if(!a)return;if(a.dataset.th){localStorage.setItem('aos_theme',a.dataset.th);applyTheme(a.dataset.th);thIcon();closeMenus()}
  else if(a.dataset.lg){window.aosSetLang&&window.aosSetLang(a.dataset.lg)}else if(a.dataset.lo){await api('api/auth/logout','POST');location.href='login'}}};
// + Agent menu and Connect pane
function sidePane(title,inner){let p=document.getElementById('xpane');if(!p){p=document.createElement('div');p.id='xpane';p.className='pane';p.style.width='min(520px,92vw)';document.body.appendChild(p)}
 p.innerHTML=`<div style="height:48px;display:flex;align-items:center;gap:8px;padding:0 14px;border-bottom:1px solid var(--bd2)"><b style="font-size:15px">${title}</b><span class="sp"></span><button class="gh" onclick="document.getElementById('xpane').classList.remove('on')">${ICN('x')}</button></div><div style="padding:14px 16px;overflow:auto;flex:1;display:flex;flex-direction:column">${inner}</div>`;requestAnimationFrame(()=>p.classList.add('on'));return p}
window.aosSidePane=sidePane;
const slug=s=>String(s||'').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g,'').replace(/đ/g,'d').replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'').replace(/^[^a-z]+/,'').slice(0,63).replace(/-+$/,'');
function connectAgent(){const p=sidePane('Connect an agent',`<p class="muted" style="margin-top:0">Send traces from an agent you already run. Traces appear under Tracing in this agent's environments.</p>
 <label>Agent name</label><input id="cn" style="width:100%" placeholder="My agent"><label>Agent ID</label><input id="cid" readonly style="width:100%;background:var(--s2)" class="mono"><small class="muted">Used in URLs and API calls. It cannot be changed later.</small>
 <div id="csnip"></div><span class="sp"></span><div class="row" style="margin-top:14px"><span class="sp"></span><button class="g" onclick="document.getElementById('xpane').classList.remove('on')">Cancel</button><button id="cnext">Next</button></div>`);
 const n=p.querySelector('#cn'),id=p.querySelector('#cid');n.oninput=()=>id.value=slug(n.value);n.focus();
 p.querySelector('#cnext').onclick=async()=>{if(!id.value)return flash('Enter an agent name',1);try{const r=await api('api/agents','POST',{name:n.value,description:'Traced externally',system_prompt:'External agent (observability only).'});
  try{await api('api/ui/agents/'+r.id+'/identifier','POST',{identifier:id.value})}catch(e){}
  p.querySelector('#csnip').innerHTML=`<div class="h4" style="margin-top:16px">Send traces</div><pre>pip install requests   # sdk/agentos_sdk.py from /opt/agentos
import agentos_sdk as ao
ao.configure(endpoint="${esc(location.origin)}", api_key="ak_...")   # create a key in Tracing → project settings

@ao.traceable(metadata={"agent_id": "${r.id}", "ls_environment": "local"})
def run(question): ...</pre><a class="btn" href="overview?agent=${r.id}">Open agent</a>`;p.querySelector('#cnext').style.display='none';AG.push({id:r.id,name:n.value})}catch(e){flash(e.message,1)}}}
window.aosConnectAgent=connectAgent;
(document.getElementById('newag')||{setAttribute(){}}).onclick=e=>{const m=popup(e.currentTarget,`<div class="mh">Build</div><a class="mi" href="builder?new=1">${ICN('hammer')}Without code</a><a class="mi" href="deployments?cli=1">${ICN('code')}With code</a><div class="mh">Connect</div><a class="mi" data-k="c">${ICN('activity')}Trace an existing agent</a>`);
 m.onclick=ev=>{if(ev.target.closest('[data-k]')){closeMenus();connectAgent()}}};
// GĐ7.1 Trợ lý Agent OS: right side panel = iframe of the chat app in assistant mode (/chat/?embed=assistant&ctx=<json>); page context = what is open now.
function aosPageCtx(){const q=new URLSearchParams(location.search),pg=location.pathname.split('/').filter(Boolean).pop()||'home',c={page:pg,url:location.pathname+location.search+location.hash};
 const id=(k,v)=>{if(v)c[k]=v};id('trace_id',q.get('trace'));id('thread_id',q.get('thread'));
 if(pg==='projects')id('project_id',q.get('p'));if(pg==='datasets'||pg==='experiments')id('dataset_id',q.get('d')||q.get('dataset'));id('experiment_id',q.get('exp')||q.get('experiment'));
 return Object.assign(c,window.AOS_CTX||{})}   // pages may set window.AOS_CTX = {trace_id, ...} for state that is not in the URL (e.g. an opened trace pane)
window.aosPageCtx=aosPageCtx;
function openAssistant(){let p=document.getElementById('xpane');const src='/chat/?embed=assistant&ctx='+encodeURIComponent(JSON.stringify(aosPageCtx()))+'&lang='+(window.LANG||'en');
 if(p&&p.dataset.k==='asst'&&p.classList.contains('on')){p.classList.remove('on');return}
 p=sidePane('✦ Trợ lý Agent OS',`<iframe id="asstf" title="Trợ lý Agent OS" src="${src}" style="border:0;flex:1;width:100%;min-height:70vh;background:transparent"></iframe>`);
 p.dataset.k='asst';p.style.width='min(420px,100vw)';p.querySelector('div:nth-child(2)').style.padding='0'}
(document.getElementById('askbtn')||{}).onclick=openAssistant;
addEventListener('message',e=>{if(e.origin===location.origin&&e.data&&e.data.type==='aos-assistant-close'){const p=document.getElementById('xpane');if(p&&p.dataset.k==='asst')p.classList.remove('on')}});
// not kept across page loads (cheap version): every open creates the iframe fresh with the current page context
document.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='i'){e.preventDefault();(document.getElementById('askbtn')||{click(){}}).click()}});
// ⌘K
const ck=document.getElementById('ck'),ckq=document.getElementById('ckq'),ckres=document.getElementById('ckres');let hl=0,items=[];
const PAGES=[...document.querySelectorAll('#sb nav a')].map(a=>({t:a.textContent.trim(),h:a.getAttribute('href'),i:(a.querySelector('use')||{getAttribute:()=>''}).getAttribute('href').slice(3)}));
function ckDraw(){const q=ckq.value.toLowerCase();items=[...PAGES.filter(p=>p.t.toLowerCase().includes(q)).map(p=>({...p,g:'Pages'})),
 ...AG.filter(a=>a.name.toLowerCase().includes(q)).slice(0,8).map(a=>({t:a.name,h:'overview?agent='+a.id,a,g:'Agents'}))];if(hl>=items.length)hl=0;
 let g='';ckres.innerHTML=items.map((it,i)=>{const hd=it.g!==g?`<div class="mh">${(g=it.g)}</div>`:'';return hd+`<a class="mi ${i===hl?'hl':''}" href="${it.h}">${it.a?agTile(it.a):ICN(it.i||'chevron-right')}<span>${esc(it.t)}</span></a>`}).join('')||'<div class="mi muted">No results</div>'}
function ckOpen(){ck.classList.add('on');ckq.value='';hl=0;ckDraw();ckq.focus()}window.aosSearch=ckOpen;
(document.getElementById('ckbtn')||{setAttribute(){}}).onclick=ckOpen;ck.onclick=e=>{if(e.target===ck)ck.classList.remove('on')};ckq.oninput=()=>{hl=0;ckDraw()};
ckq.onkeydown=e=>{if(e.key==='ArrowDown'){hl=Math.min(hl+1,items.length-1);ckDraw();e.preventDefault()}else if(e.key==='ArrowUp'){hl=Math.max(hl-1,0);ckDraw();e.preventDefault()}
 else if(e.key==='Enter'&&items[hl]){location.href=items[hl].h}else if(e.key==='Escape')ck.classList.remove('on')};
document.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='k'){e.preventDefault();ckOpen()}});
// read-only (viewer / demo account): banner, write controls disabled, api() refuses writes (the server answers 403 anyway)
const RO_RX=/^\s*(\+|New|Create|Add|Save|Delete|Remove|Edit|Deploy|Promote|Roll ?back|Run|Invite|Import|Upload|Publish|Enable|Disable|Revoke|Deactivate|Duplicate|Clone|Configure|Rotate|Replace|Approve|Reject|Test|Start|Stop|Pause|Resume|Apply|Restore|Back ?up|Refresh|Try|Call|Sync|Tạo|Thêm|Lưu|Xoá|Xóa|Sửa|Triển khai|Chạy|Mời|Nhập|Tải lên|Đăng|Bật|Tắt|Thu hồi|Nhân bản|Cấu hình|Thay|Duyệt|Từ chối|Kiểm thử|Bắt đầu|Dừng|Tạm dừng|Khôi phục|Sao lưu|Áp dụng|Gọi thử|Làm mới|Đồng bộ|Thử)\b/i;
const RO_ICN=/#i-(plus|trash|square-pen|pencil|save|upload|rocket|play|copy|rotate-ccw|key-round)\b/;
function roScan(root){root.querySelectorAll('#content button,#content a.btn,#tb a.btn,#tb #askbtn,#sb #newag,.pane button').forEach(b=>{if(b.dataset.roOk||b.closest('.seg,.tabs,nav,#ck'))return;const t=(b.textContent||'').trim(),u=[...b.querySelectorAll('use')].map(x=>x.getAttribute('href')||'').join(' ');
 if(RO_RX.test(t)||RO_ICN.test(u)||b.id==='newag'||/invite/i.test(b.getAttribute('href')||'')){b.disabled=true;b.setAttribute('aria-disabled','true');b.classList.add('ro-off');b.title=(window.LANG||'en')==='vi'?'Chỉ xem':'Read-only';if(b.tagName==='A')b.removeAttribute('href')}});root.querySelectorAll('#content td input:not([type=search]),#content td select,#content td textarea,.pane input[type=checkbox],.pane select,.pane textarea').forEach(x=>{if(!x.disabled){x.disabled=true;x.classList.add('ro-off')}})}
function aosReadOnly(m){window.AOS_RO=1;html.classList.add('ro');const vi=(window.LANG||'en')==='vi';const st=document.createElement('style');st.textContent='.ro-off{opacity:.45;pointer-events:none;cursor:not-allowed}#robar{background:var(--wa-bg,#fef3c7);color:var(--wa,#92400e);font-size:12.5px;padding:6px 14px;border-bottom:1px solid var(--bd2)}';document.head.append(st);
 const bar=document.createElement('div');bar.id='robar';bar.setAttribute('role','status');bar.dataset.testid='ro-banner';bar.textContent=m.demo?(vi?'Tài khoản demo: chỉ xem. Mọi thay đổi cấu hình, agent, người dùng, lịch chạy, key và ngân sách đều bị khoá.':'Demo account: read-only. Changes to configuration, agents, users, schedules, keys and budgets are locked.'):(vi?'Vai trò Viewer: chỉ xem.':'Viewer role: read-only.');
 const mc=document.getElementById('mainc');if(mc)mc.insertBefore(bar,mc.firstChild);roScan(document);new MutationObserver(()=>roScan(document)).observe(document.body,{childList:true,subtree:true})}
// dữ liệu
const WSKEY=()=>'aos_ws_'+(ME?ME.username:'');   // GĐ12.99: last chosen workspace per user (this browser only)
api('api/auth/me').then(m=>{ME=m;try{const last=+localStorage.getItem(WSKEY());if(last&&last!==m.ws&&(m.workspaces||[]).some(w=>w.id===last)&&!sessionStorage.getItem('aos_ws_restore')){sessionStorage.setItem('aos_ws_restore','1');api('api/auth/workspace','POST',{workspace_id:last}).then(()=>location.reload()).catch(()=>{})}}catch(x){}
 if(m.role==='viewer'||m.demo)aosReadOnly(m);(document.getElementById('wsname')||{}).textContent=(m.workspaces||[]).find(w=>w.id==m.ws)?.name||'Workspace';
 (document.getElementById('wstile')||{}).textContent=ini((m.workspaces||[]).find(w=>w.id==m.ws)?.name||'W');(document.getElementById('uname')||{}).textContent=m.username;
 (document.getElementById('urole')||{}).textContent=(m.email||m.username+'@'+location.hostname.split('.')[0]);(document.getElementById('utile')||{}).textContent=ini(m.username)}).catch(()=>{});
api('api/agents').then(a=>{AG=a;window.AOS_AGENTS=a;paintAgent();document.dispatchEvent(new CustomEvent('aos-agents',{detail:a}))}).catch(()=>{});
api('api/ui/counts').then(c=>{document.querySelectorAll('#sb [data-c]').forEach(s=>{const v=c[s.dataset.c];if(v!=null)s.textContent=v>=1000?(v/1000).toFixed(1)+'k':v})}).catch(()=>{});
})();
"""
