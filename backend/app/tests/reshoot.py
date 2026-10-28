import json
from playwright.sync_api import sync_playwright
c = dict(l.split("=", 1) for l in open("/workspace/secrets/deepagent_web.txt").read().split()); U = c["url"].rstrip("/")
st = json.load(open("/workspace/adk_ha_build/os/tests/p12_state.json")); O = "/workspace/adk_ha_build/os/shots/"
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    pg = b.new_context(http_credentials={"username": "moza", "password": c["password"]}, viewport={"width": 1440, "height": 900}).new_page()
    for path, name in [(f"/projects?p={st['P']}&trace={st['trace']}", "p1_trace_sdk.png"), (f"/projects?p=1&trace={st['agent_trace']}", "p1_trace_agent.png"),
                       (f"/projects?p={st['P']}", "p1_projects_runs.png"), (f"/datasets?d={st['ds']}", "p2_dataset_detail.png")]:
        pg.goto(U + path, wait_until="networkidle"); pg.wait_for_timeout(4000)
        if "trace=" in path: pg.locator("#trv").scroll_into_view_if_needed(); pg.wait_for_timeout(500); pg.locator("#trv").screenshot(path=O + name)
        else: pg.screenshot(path=O + name, full_page=False); print("ok", name)
    b.close()
