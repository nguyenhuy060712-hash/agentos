"""Workflow E2E: drives each main workflow in a headless browser, screenshots every step,
fails on page errors / failed steps. Usage: python3 workflow_e2e.py [en|vi]"""
import sys, json, re
from playwright.sync_api import sync_playwright
c = dict(l.split("=", 1) for l in open("/workspace/secrets/deepagent_web.txt").read().split()); U = c["url"].rstrip("/")
O = "/workspace/adk_ha_build/shots_wf/"; LANG = sys.argv[1] if len(sys.argv) > 1 else "en"
res = []; errs = []
def step(pg, wf, n, name, fn):
    try:
        errs.clear(); fn(); pg.wait_for_timeout(1800)
        f = f"{O}{wf}_{n:02d}_{name}_{LANG}.png"; pg.screenshot(path=f)
        ok = not errs and "is not a function" not in pg.inner_text("body")
        res.append((wf, n, name, ok, f, list(errs)))
    except Exception as e:
        f = f"{O}{wf}_{n:02d}_{name}_{LANG}_FAIL.png"
        try: pg.screenshot(path=f)
        except Exception: pass
        res.append((wf, n, name, False, f, [str(e)[:160]]))
def go(pg, p): return lambda: pg.goto(U + p, wait_until="networkidle", timeout=60000)
def click(pg, sel): return lambda: pg.locator(sel).first.click(timeout=8000)
def first_href(pg, pat):
    h = pg.eval_on_selector_all("a,[onclick]", "els=>els.map(e=>e.getAttribute('href')||e.getAttribute('onclick')||'')")
    for x in h:
        m = re.search(pat, x)
        if m: return m.group(0)
    raise Exception("no link " + pat)
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    ctx = b.new_context(viewport={"width": 1440, "height": 900}); pg = ctx.new_page()
    pg.on("pageerror", lambda e: errs.append(str(e)[:200]))
    pg.goto(U + "/login", wait_until="networkidle"); pg.fill("#u", "moza"); pg.fill("#p", c["password"]); pg.click("form button"); pg.wait_for_timeout(3000)
    pg.evaluate("l=>fetch('api/auth/lang',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({lang:l})})", LANG)
    ctx.add_cookies([{"name": "aos_lang", "value": LANG, "url": U}])
    # 1 Tracing
    step(pg, "tracing", 1, "projects", go(pg, "/projects"))
    step(pg, "tracing", 2, "project", lambda: pg.goto(U + "/" + first_href(pg, r"projects\?p=\d+"), wait_until="networkidle"))
    step(pg, "tracing", 3, "trace", click(pg, "tbody tr.r"))
    step(pg, "tracing", 4, "add_to_dataset", click(pg, "button:visible:has-text('Add to')"))
    # 2 Datasets -> experiment -> compare
    step(pg, "datasets", 1, "list", go(pg, "/datasets"))
    step(pg, "datasets", 2, "new_pane", click(pg, "button:visible:has-text('Dataset')"))
    pg.keyboard.press("Escape")
    step(pg, "datasets", 3, "dataset", lambda: pg.goto(U + "/" + first_href(pg, r"datasets\?d=\d+"), wait_until="networkidle"))
    step(pg, "datasets", 4, "examples", click(pg, "[data-tab=examples]:visible"))
    step(pg, "datasets", 5, "experiments", click(pg, "[data-tab=experiments]:visible"))
    step(pg, "datasets", 6, "compare", go(pg, "/compare"))
    # 3 Evaluators
    step(pg, "evaluators", 1, "list", go(pg, "/evaluators"))
    step(pg, "evaluators", 2, "editor", lambda: pg.goto(U + "/" + first_href(pg, r"evaluators\?id=\d+"), wait_until="networkidle"))
    step(pg, "evaluators", 3, "run_test", click(pg, "button:has-text('Run test'), button:has-text('Chạy thử')"))
    step(pg, "evaluators", 4, "bind_project", click(pg, "button:has-text('Bind'), button:has-text('Gắn')"))
    # 4 Annotation queue
    step(pg, "annotation", 1, "queues", go(pg, "/annotation"))
    step(pg, "annotation", 2, "reviewer", lambda: pg.goto(U + "/" + first_href(pg, r"annotation\?q=\d+"), wait_until="networkidle"))
    # 5 Prompts -> Playground
    step(pg, "prompts", 1, "list", go(pg, "/prompts"))
    step(pg, "prompts", 2, "prompt", lambda: pg.locator("#pt tr.r, tbody tr.r:visible").first.click(timeout=8000))
    step(pg, "prompts", 3, "playground", go(pg, "/playground"))
    step(pg, "prompts", 4, "compare_models", click(pg, "button:has-text('Compare'), button:has-text('So sánh')"))
    # 6 Build agent -> test -> deploy
    step(pg, "agent", 1, "build", go(pg, "/builder"))
    step(pg, "agent", 2, "configure", lambda: pg.goto(U + "/" + first_href(pg, r"builder\?id=\d+"), wait_until="networkidle"))
    def chat():
        pg.goto(U + "/" + first_href(pg, r"builder\?id=\d+") if False else pg.url.split("&")[0] + "&tab=test", wait_until="networkidle")
        pg.fill("#tq", "What time is it?"); pg.click("#tsend"); pg.wait_for_selector(".tmsg.a >> text=/tokens|token/", timeout=90000)
    step(pg, "agent", 3, "test_chat", chat)
    step(pg, "agent", 4, "deploy_pane", click(pg, "#adep"))
    step(pg, "agent", 5, "traces", go(pg, "/projects"))
    b.close()
bad = [r for r in res if not r[3]]
for r in res: print(("ok  " if r[3] else "FAIL"), r[0], r[1], r[2], r[4], r[5] or "")
json.dump(res, open(f"/tmp/wf_{LANG}.json", "w"), indent=1)
print(f"WORKFLOW {'PASS' if not bad else 'FAIL'} {len(res)-len(bad)}/{len(res)}")
