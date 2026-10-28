"""i18n crawl: open every UI route (and key modal/tab states) in EN and VI with headless Chrome.
EN: fail on any Vietnamese diacritics in visible UI text. VI: fail on English strings not in the allowed-terms list.
Both: fail on console/page errors. Text that came from API responses (user data) is excluded."""
import json, os, re, sys
from playwright.sync_api import sync_playwright
# Config: AOS_URL / AOS_USER / AOS_PASSWORD (env) or the secrets file (WSL); browser: /usr/bin/google-chrome if present, else the installed Chrome (channel "chrome", e.g. on Windows).
_f = "/workspace/secrets/deepagent_web.txt"
c = dict(l.split("=", 1) for l in open(_f).read().split()) if os.path.exists(_f) else {}
c["url"] = os.environ.get("AOS_URL") or c.get("url") or "http://127.0.0.1:8020"; c["password"] = os.environ.get("AOS_PASSWORD") or c.get("password") or "phase0-local"
USER = os.environ.get("AOS_USER", "moza"); U = c["url"].rstrip("/")
OUT = os.environ.get("I18N_OUT") or ("/tmp" if os.path.isdir("/tmp") else os.path.dirname(os.path.abspath(__file__)))
ROUTES = ["/", "/overview", "/overview?welcome=1", "/overview?agent=1", "/engine", "/studio", "/builder", "/projects", "/projects?p=1", "/monitoring?p=1",
  "/datasets", "/datasets?view=experiment", "/datasets?new_experiment=1", "/datasets?d=1", "/datasets?d=1&tab=examples", "/evaluators", "/annotation", "/hub", "/hub?id=1", "/hub?webhook=1",
  "/playground", "/deployments", "/deployments?cli=1", "/context", "/dashboards", "/apps", "/gateway", "/sandboxes", "/listeners", "/tools", "/memory", "/fleet",
  "/approvals", "/simulations", "/server", "/gwkeys", "/agents", "/rules", "/eval", "/experiments", "/traces", "/admin"] + \
  [f"/settings?s={s}" for s in "keys members oauth engine integrations secrets mcp models webhooks aisearch pricing fbtags restags shared workspaces audit usage system general".split()] + \
  ["/builder?id=2", "/builder?id=2&tab=channels", "/builder?id=2&tab=test", "/builder?id=2&tab=versions", "/builder-chat", "/insights", "/context", "/hub?id=1&canvas=1",
   "/studio?agent=2", "/monitoring?tab=environments", "/projects?p=1&tab=setup", "/classic/projects?p=1", "/classic/admin", "/settings?s=security", "/settings?s=gworkspace", "/settings?s=connections", "/settings?s=sso", "/settings?s=backups"] + \
  [f"/classic/{p}" for p in "admin agents annotation builder datasets deployments evaluators experiments gateway gwkeys hub memory playground projects rules sandboxes server settings studio".split()]
CLICKS = {"/builder?id=2&tab=channels": ["[data-chadd]", "#chadd", "button:has-text('Channel')", "button:has-text('Kênh')"], "/builder?id=2": ["button:has-text('Share')", "button:has-text('Chia sẻ')", "#shb"],
  "/hub?id=1&canvas=1": ["#cvb"], "/playground": ["#lab", "button:has-text('Lab')"], "/classic/projects?p=1": ["[data-t=setup]"], "/context": [".card .r", "tr.r"], "/overview": ["#newag", "#ubtn"], "/projects?p=1": ["#rb tr.r td:nth-child(3)"], "/datasets": ["#dn"], "/settings?s=keys": ["#ssb"], "/evaluators": ["#h button:last-child"], "/annotation": ["#h button:last-child"]}
ALLOWED = set(json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "i18n_allowed.json"), encoding="utf-8")))
VI_RX = re.compile(r"[àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ]", re.I)
COLLECT = r"""()=>{const out=[];const vis=e=>{if(!e)return false;const r=e.getClientRects();if(!r.length)return false;const s=getComputedStyle(e);return s.visibility!=='hidden'&&s.display!=='none'&&+s.opacity>0};
 const skip=e=>e.closest('script,style,pre,code,textarea,.mono,[translate=no],#ckres,svg');
 const w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);let n;while(n=w.nextNode()){const t=n.nodeValue.replace(/\s+/g,' ').trim();if(t&&n.parentElement&&!skip(n.parentElement)&&vis(n.parentElement))out.push(t)}
 document.querySelectorAll('[placeholder],[title],[aria-label]').forEach(e=>{if(skip(e)||!vis(e))return;for(const a of ['placeholder','title','aria-label']){const v=e.getAttribute(a);if(v&&v.trim())out.push(v.trim())}});
 document.querySelectorAll('select option').forEach(o=>{const s=o.closest('select');if(s&&vis(s)&&!skip(s))out.push(o.textContent.trim())});
 document.querySelectorAll('button,input[type=button],input[type=submit]').forEach(b=>{if(b.value&&b.tagName==='INPUT'&&vis(b))out.push(b.value)});return out}"""
def strings_of(o, acc):
    if isinstance(o, str): acc.add(o.strip()); [acc.add(x.strip()) for x in re.split(r"[\n:,;|()]", o) if x.strip()]
    elif isinstance(o, dict): [strings_of(v, acc) for v in o.values()]; [acc.add(str(k)) for k in o]
    elif isinstance(o, list): [strings_of(v, acc) for v in o]
WORD = re.compile(r"[A-Za-z][A-Za-z'’\-]+")
NORM = lambda s: re.sub(r"[\s*$`_#>\\]+", " ", s).strip().lower()
def is_noise(t):
    if not re.search(r"[A-Za-z]{2}", t): return True
    if re.fullmatch(r"\{\{\w+\}\}|ID: [0-9a-f]+…?", t): return True   # template vars, short ids
    if len(t) <= 3 and t.isupper(): return True   # avatar/agent initials (incl. Đ)
    if re.fullmatch(r"[\d\s.,/:$%KMBGT]+(MB|GB|KB|ms|s)?", t): return True
    if re.fullmatch(r"(thread|ws\d+:)\S*.*", t) and re.search(r"[0-9a-f]{8}-|\d{6,}|ws\d+:", t): return True   # thread ids / role lists
    if re.fullmatch(r"[\w.\-/:@#=+?&%~]+", t) and (re.search(r"[\d_/.:@]", t) or t.islower()): return True   # ids, paths, slugs, model names
    return False
def leftover_en(t):
    if is_noise(t) or VI_RX.search(t): return None
    words = [w for w in WORD.findall(t)]
    AL = {a.lower() for a in ALLOWED}
    rest = [w for w in words if w.lower() not in AL and w.lower().rstrip('s') not in AL]
    if t in ALLOWED or not rest: return None
    return t
def run(lang, report):
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"]) if os.path.exists("/usr/bin/google-chrome") else p.chromium.launch(channel="chrome"); ctx = b.new_context(viewport={"width": 1440, "height": 900}); ctx.add_init_script("try{sessionStorage.setItem('aos_lang_sync','1')}catch(e){}"); pg = ctx.new_page()
        errs, data = [], set()
        pg.on("pageerror", lambda e: errs.append("pageerror: " + str(e)[:200]))
        pg.on("console", lambda m: errs.append("console: " + m.text[:200]) if m.type == "error" and "favicon" not in m.text and "Failed to load resource" not in m.text else None)
        def on_resp(r):
            try:
                if "json" in (r.headers.get("content-type") or ""): strings_of(r.json(), data)
            except Exception: pass
        pg.on("response", on_resp)
        pg.goto(U + "/login"); pg.fill("#u", USER); pg.fill("#p", c["password"]); pg.click("form button"); pg.wait_for_timeout(2500)
        orig = pg.evaluate("()=>window.LANG||document.documentElement.lang||'en'")   # the account language overrides the cookie: remember it, restore it at the end
        SETLANG = "l=>fetch('api/auth/lang',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({lang:l})})"
        pg.evaluate(SETLANG, lang)
        ctx.add_cookies([{"name": "aos_lang", "value": lang, "url": U}])
        bad = {}
        def check(route):
            for _ in range(3):
                try: texts = pg.evaluate(COLLECT); break
                except Exception: pg.wait_for_timeout(1500); texts = []
            nd = {NORM(d) for d in data if len(d) < 4000}
            for t in texts:
                if NORM(t) in nd or (len(t) > 20 and any(NORM(t) in x for x in nd)): continue   # user data rendered as markdown
                if t in data or any(t in d for d in data if len(t) >= 4 and len(d) < 4000 and len(d) >= len(t)): continue
                t2 = t
                for d in sorted((d for d in data if 3 <= len(d) <= 300 and d in t), key=len, reverse=True): t2 = t2.replace(d, " ")
                if t2 != t and not (VI_RX.search(t2) if lang == "en" else leftover_en(t2.strip())): continue
                if lang == "en" and VI_RX.search(t) and not (len(t) <= 3 and t.isupper()): bad.setdefault(t, set()).add(route)
                if lang == "vi":
                    x = leftover_en(t)
                    if x: bad.setdefault(x, set()).add(route)
        for r in ROUTES:
            errs.clear(); data.clear()
            try: pg.goto(U + r, wait_until="networkidle", timeout=30000)
            except Exception as e: errs.append("nav: " + str(e)[:120])
            pg.wait_for_timeout(1200); check(r)
            for sel in CLICKS.get(r, []):
                try: pg.locator(sel).first.click(timeout=3000); pg.wait_for_timeout(1000); check(r + " +" + sel)
                except Exception: pass
                pg.keyboard.press("Escape")
            if errs: report["errors"][f"{lang} {r}"] = errs[:5]
        report[lang] = {k: sorted(v) for k, v in sorted(bad.items())}
        # first visit with no cookie
        if lang == "en":
            c2 = b.new_context(); p2 = c2.new_page(); p2.goto(U + "/login"); report["first_visit_lang"] = p2.evaluate("()=>[document.documentElement.lang, window.LANG, document.body.innerText.slice(0,200)]"); c2.close()
        pg.evaluate(SETLANG, orig if orig in ("en", "vi") else "en")
        b.close()
if __name__ == "__main__":
    report = {"errors": {}}
    for lang in (sys.argv[1].split(",") if len(sys.argv) > 1 else ["en", "vi"]):
        run(lang, report)
        json.dump(report, open(f"{OUT}/i18n_report.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)   # after every language: a crash in the next one keeps this result
    fv = report.get("first_visit_lang")
    n_en, n_vi, n_err = len(report.get("en", {})), len(report.get("vi", {})), len(report["errors"])
    print(f"EN Vietnamese strings: {n_en}\nVI leftover English strings: {n_vi}\nroutes with console errors: {n_err}\nfirst visit: {fv[:2] if fv else None}")
    ok = n_en == 0 and n_vi == 0 and n_err == 0 and (not fv or (fv[1] == "en" and not VI_RX.search(fv[2])))
    print("I18N", "PASS" if ok else "FAIL"); sys.exit(0 if ok else 1)
