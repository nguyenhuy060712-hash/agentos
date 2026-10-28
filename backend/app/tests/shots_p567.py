import json
from playwright.sync_api import sync_playwright
c = dict(l.split("=", 1) for l in open("/workspace/secrets/deepagent_web.txt").read().split()); U = c["url"].rstrip("/")
st = json.load(open("/workspace/adk_ha_build/os/tests/p567_state.json")); O = "/workspace/adk_ha_build/os/shots/"
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    pg = b.new_context(viewport={"width": 1440, "height": 1000}).new_page()
    pg.goto(U + "/agents", wait_until="networkidle"); pg.wait_for_timeout(1500)
    pg.fill("#u", "moza"); pg.screenshot(path=O + "p7_login.png")
    pg.fill("#p", c["password"]); pg.click("button"); pg.wait_for_url("**/agents", timeout=30000); pg.wait_for_timeout(2500)
    def shot(path, name, fn=None, wait=3500):
        try:
            pg.goto(U + path, wait_until="networkidle", timeout=60000); pg.wait_for_timeout(wait)
            if fn: fn()
            pg.screenshot(path=O + name, full_page=True); print("ok", name)
        except Exception as e: print("ERR", name, str(e)[:200])
    shot("/admin", "p7_admin.png")
    def sbx():
        pg.locator("#sl .card").first.click(); pg.wait_for_timeout(1500); pg.click("text=▶ Chạy"); pg.wait_for_timeout(9000)
    shot("/sandboxes", "p7_sandbox.png", sbx)
    def gw():
        pg.fill("#n", "demo-ui"); pg.fill("#m", "gemini-flash-lite-latest"); pg.fill("#b", "1"); pg.fill("#c", "600"); pg.click("button:has-text('Tạo key')"); pg.wait_for_timeout(2000)
        pg.click("button:has-text('Gửi')"); pg.wait_for_timeout(9000)
    shot("/gwkeys", "p6_gwkeys.png", gw)
    shot("/gateway", "p6_gateway_usage.png")
    def srv():
        pg.select_option("#sa", label=f"Trợ lý server"); pg.click("button:has-text('Stream')"); pg.wait_for_timeout(15000)
    shot("/server", "p5_server.png", srv)
    shot(f"/projects?p={st['gw_pid']}", "p6_gateway_traces.png")
    def errs():
        pg.click(".tabs a[data-t=errors]"); pg.wait_for_timeout(1500); pg.select_option("#em", "semantic"); pg.click("text=Cập nhật"); pg.wait_for_timeout(5000)
    shot(f"/projects?p={st['err_pid']}", "gap_errors_semantic.png", errs)
    shot(f"/projects?p={st['otlp_pid']}", "gap_otlp_protobuf.png")
    shot("/datasets", "gap_example_attachments.png", lambda: (pg.locator("text=att-" + st["tag"]).first.click(), pg.wait_for_timeout(4000)))
    b.close()
