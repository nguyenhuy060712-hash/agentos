import json
from playwright.sync_api import sync_playwright
c = dict(l.split("=", 1) for l in open("/workspace/secrets/deepagent_web.txt").read().split()); U = c["url"].rstrip("/")
st = json.load(open("/workspace/adk_ha_build/os/tests/p12_state.json")); O = "/workspace/adk_ha_build/os/shots/"
import requests; s = requests.Session(); s.auth = ("moza", c["password"])
share = s.post(U + "/api/obs/share", json={"trace_id": st["trace"], "hours": 1}).json()
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    ctx = b.new_context(http_credentials={"username": "moza", "password": c["password"]}, viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    def shot(path, name, wait=3500, click=None):
        pg.goto(U + path, wait_until="networkidle", timeout=60000)
        if click:
            try: pg.get_by_text(click, exact=False).first.click(); 
            except Exception as e: print("click", click, e)
        pg.wait_for_timeout(wait); pg.screenshot(path=O + name, full_page=True); print("ok", name)
    shot(f"/projects?project={st['P']}", "p1_projects_runs.png")
    shot(f"/projects?trace={st['trace']}", "p1_trace_sdk.png")
    shot(f"/projects?trace={st['agent_trace']}", "p1_trace_agent.png")
    shot(f"/projects?project={st['P']}", "p1_dashboard.png", click="Dashboard")
    shot(f"/projects?project={st['P']}", "p1_threads.png", click="Threads")
    shot("/rules", "p1_alerts_rules.png")
    shot("/datasets", "p2_datasets.png")
    shot(f"/datasets?id={st['ds']}", "p2_dataset_detail.png")
    shot("/experiments", "p2_experiments.png")
    shot(f"/experiments?compare={st['ea']},{st['eb']}", "p2_compare.png")
    shot("/evaluators", "p2_evaluators.png")
    shot("/annotation", "p2_annotation.png")
    anon = b.new_context(viewport={"width": 1280, "height": 800}).new_page()
    anon.goto(U + share["path"]); anon.wait_for_timeout(1500); anon.screenshot(path=O + "p1_share_public.png", full_page=True); print("ok share")
    b.close()
s.delete(U + f"/api/obs/share/{share['id']}")
