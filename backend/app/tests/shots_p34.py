import json
from playwright.sync_api import sync_playwright
c = dict(l.split("=", 1) for l in open("/workspace/secrets/deepagent_web.txt").read().split()); U = c["url"].rstrip("/")
st = json.load(open("/workspace/adk_ha_build/os/tests/p34_state.json")); O = "/workspace/adk_ha_build/os/shots/"
shots = [("/hub", "p3_hub.png", None), ("/builder", "p4_builder.png", None), ("/approvals", "p4_approvals.png", None),
         (f"/studio?agent=6&thread={st['studio_thread']}", "p4_studio.png", None), ("/simulations", "p4_simulations.png", None),
         (f"/projects?p={st['pid']}", "gap_errors.png", "errors"), (f"/projects?p={st['pid']}", "gap_threads_eval.png", "threads"),
         ("/rules", "gap_redaction.png", None), (f"/projects?p=1&trace={st['redact_trace']}", "gap_attachments_trace.png", None),
         (f"/projects?p={st['pid']}&trace={st['otel_trace']}", "gap_otlp_trace.png", None), ("/experiments", "gap_summary_scores.png", None),
         ("/evaluators", "gap_evaluators.png", None), ("/playground", "p3_playground.png", None)]
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    pg = b.new_context(http_credentials={"username": "moza", "password": c["password"]}, viewport={"width": 1440, "height": 1000}).new_page()
    for path, name, tab in shots:
        try:
            pg.goto(U + path, wait_until="networkidle", timeout=60000); pg.wait_for_timeout(3500)
            if tab: pg.click(f".tabs a[data-t={tab}]"); pg.wait_for_timeout(3000)
            pg.screenshot(path=O + name, full_page=True); print("ok", name)
        except Exception as e: print("ERR", name, str(e)[:150])
    b.close()
