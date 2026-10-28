import re, time
from playwright.sync_api import sync_playwright
c = dict(l.split("=", 1) for l in open("/workspace/secrets/deepagent_web.txt").read().split())
U = c["url"].rstrip("/") + "/"; errs = []
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
    ctx = b.new_context(http_credentials={"username": "moza", "password": c["password"]}, viewport={"width": 1400, "height": 1000}, locale="vi-VN")
    pg = ctx.new_page(); pg.on("pageerror", lambda e: errs.append(str(e))); pg.on("console", lambda m: m.type == "error" and errs.append(m.text))
    for path in ["fleet", "agents", "tools", "memory", "eval", "settings", "traces?run=28", "playground", "deployments", "gateway"]:
        pg.goto(U + path); pg.wait_for_load_state("networkidle"); time.sleep(1.5)
        pg.screenshot(path=f"/workspace/adk_ha_build/os/shots/{path.split('?')[0]}.png", full_page=False); print("page", path, "ok")
    # create an agent via the UI form
    pg.goto(U + "agents"); pg.wait_for_load_state("networkidle"); pg.click("#nb")
    name = "Lịch & giờ (tạo bằng UI)"
    pg.fill("#f_name", name); pg.fill("#f_desc", "Trả lời ngày giờ, tạo qua giao diện")
    pg.fill("#f_prompt", "Bạn trả lời tiếng Việt, một câu. Khi được hỏi ngày/giờ hãy gọi GetDateTime.")
    pg.check("input.mt[value='ha:llm__GetDateTime']"); pg.uncheck("#f_fs"); pg.uncheck("#f_plan")
    pg.click("#save"); pg.wait_for_selector("text=Lịch sử phiên bản", timeout=15000)
    aid = int(re.search(r"Sửa agent #(\d+)", pg.inner_text("#ed")).group(1)); print("created agent via UI:", aid)
    pg.goto(U + f"?agent={aid}"); pg.wait_for_load_state("networkidle"); time.sleep(1)
    pg.fill("#q", "Hôm nay là thứ mấy, ngày bao nhiêu?"); pg.click("#send")
    pg.wait_for_selector(".meta", timeout=180000); time.sleep(1)
    last = pg.locator(".m.a").last
    print("reply:", last.locator("div").first.inner_text()[:200]); print("meta:", last.locator(".meta").inner_text()[:200])
    pg.locator("details summary").last.click(); time.sleep(0.5)
    print("trace steps:", pg.locator(".st").count(), "|", pg.locator(".st").first.inner_text()[:120] if pg.locator(".st").count() else "")
    pg.screenshot(path="/workspace/adk_ha_build/os/shots/chat_ui_agent.png")
    # agent 1 report in UI: trace shows todos + sub-agent
    pg.goto(U + "?agent=1"); pg.wait_for_load_state("networkidle"); pg.click("#new")
    pg.fill("#q", "Hãy lập kế hoạch rồi làm báo cáo tổng hợp tình trạng nhà: nhiệt độ, độ ẩm, đèn."); pg.click("#send")
    pg.wait_for_selector(".meta", timeout=240000); time.sleep(1)
    pg.locator("details summary").last.click(); time.sleep(0.5)
    print("report trace: todos=", pg.locator(".st.todos").count(), "subagent=", pg.locator(".st.subagent").count(), "sub-tools=", pg.locator(".st >> text=trong sub-agent").count())
    pg.screenshot(path="/workspace/adk_ha_build/os/shots/chat_report_trace.png", full_page=True)
    b.close()
print("JS errors:", errs)
