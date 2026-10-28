import json, sys
from playwright.sync_api import sync_playwright
c = dict(l.split("=", 1) for l in open("/workspace/secrets/deepagent_web.txt").read().split()); U = c["url"].rstrip("/")
O = "/workspace/adk_ha_build/shots_phong/"; ids = json.load(open("/workspace/adk_ha_build/tests/team_ids.json"))
PAGES = [a.split("=", 1) for a in sys.argv[1:]]
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    pg = b.new_context(viewport={"width": 1440, "height": 1000}, http_credentials={"username": "moza", "password": c["password"]}).new_page()
    pg.goto(U + "/agents", wait_until="networkidle"); pg.wait_for_timeout(1500)
    if pg.locator("#u").count():
        pg.fill("#u", "moza"); pg.fill("#p", c["password"]); pg.click("button"); pg.wait_for_timeout(5000); print("after login", pg.url)
    for name, path in PAGES:
        try:
            pg.goto(U + path, wait_until="networkidle", timeout=60000); pg.wait_for_timeout(3500)
            pg.screenshot(path=O + name + ".png", full_page=True); print("ok", name, pg.url)
        except Exception as e: print("ERR", name, str(e)[:150])
    b.close()
