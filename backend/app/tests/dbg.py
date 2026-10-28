import json
from playwright.sync_api import sync_playwright
c = dict(l.split("=", 1) for l in open("/workspace/secrets/deepagent_web.txt").read().split()); U = c["url"].rstrip("/")
st = json.load(open("/workspace/adk_ha_build/os/tests/p12_state.json"))
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    pg = b.new_context(http_credentials={"username": "moza", "password": c["password"]}).new_page()
    pg.on("response", lambda r: r.status >= 400 and print("HTTP", r.status, r.url)); pg.on("console", lambda m: print("CONSOLE", m.type, m.text[:300])); pg.on("pageerror", lambda e: print("PAGEERR", str(e)[:300]))
    pg.goto(U + f"/projects?p={st['P']}&trace={st['trace']}", wait_until="networkidle"); pg.wait_for_timeout(8000); print(pg.evaluate("location.href"))
    print(pg.evaluate("document.getElementById('trv') && [document.getElementById('trv').style.display, document.getElementById('trv').innerHTML.length]"))
    b.close()
