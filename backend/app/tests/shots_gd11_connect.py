"""GĐ11.29 real browser flow of the chat ConnectCard + Settings > Connections / MCP servers, through the 11.30 fakes ($0, fake LLM, no real service).
Run (Windows, python playwright in a throwaway env):  uv run --no-project --with playwright python app/tests/shots_gd11_connect.py
Needs (all on 127.0.0.1): the server with the FAKE LLM on AOS_URL (own port, e.g. 8023, started with AOS_EGRESS_ALLOW=127.0.0.0/8) and the fake OAuth + MCP server
(`python tests/fakes/serve_fake_mcp_oauth.py none` -> FAKE_MCP = its mcp_url). Env: AOS_URL (default http://127.0.0.1:8023), AOS_USER / AOS_PASSWORD (local stack), FAKE_MCP.
Sets up a throw-away workspace (manual OAuth client on the fake, one agent with the fake's `whoami` tool), then per language (vi, en):
  A) a scripted run ("mcp-whoami") pauses at the pre-run gate -> screenshot -> "Bỏ qua" -> the tool pauses with auth_required -> screenshot -> Connect (popup on the fake) -> the run continues;
  B) the person is disconnected again; a new run pauses at the gate -> Connect -> the run continues without any click on the card but the first.
Then screenshots of Settings > Tool connections (3 tabs) and the Add MCP server dialog. Output: docs/anh_nghiem_thu/11.29_*.png + a text check per screen."""
import json, os, re, sys, time
from playwright.sync_api import sync_playwright

sys.stdout.reconfigure(encoding="utf-8")
U = os.environ.get("AOS_URL", "http://127.0.0.1:8023").rstrip("/")
USER, PW = os.environ.get("AOS_USER", "moza"), os.environ.get("AOS_PASSWORD", "phase0-local")
MCP = os.environ["FAKE_MCP"]; FAKE = MCP.rsplit("/mcp", 1)[0]
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "docs", "anh_nghiem_thu")
H = {"Content-Type": "application/json"}
VI_CH = "ạảãấầẩẫậắằẳẵặẹẻẽếềểễệịỉĩọỏõốồổỗộớờởỡợụủũứừửữựỳỵỷỹđâăêôơưáàéèíìóòúùýÂĂÊÔƠƯ"

def main():
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome")
        ctx = b.new_context(viewport={"width": 1280, "height": 900})
        api = lambda m, u, j=None, **k: getattr(ctx.request, m)(U + u, data=json.dumps(j) if j is not None else None, headers=H, **k)
        ok = lambda r: (r.ok or print("FAIL", r.url, r.status, r.text()[:300]) or sys.exit(1)) and r
        ok(api("post", "/api/auth/login", {"username": USER, "password": PW}))
        old_lang = api("get", "/api/auth/me").json().get("lang") or "en"
        ws = ok(api("post", "/api/admin/workspaces", {"name": "Ket noi 11.29 " + str(int(time.time()))[-5:]})).json()["id"]
        ok(api("post", "/api/auth/workspace", {"workspace_id": ws}))
        # --- fake service: a manual OAuth client (the fake publishes no metadata), one agent that may call `whoami`
        r = ok(api("post", "/api/mcp/servers/oauth", {"name": "fk", "url": MCP, "client_id": "manual-client-id", "client_secret": "manual-client-secret",
                                                       "authorize_url": FAKE + "/authorize", "token_url": FAKE + "/token", "scopes": "read write"})).json()
        def connect_api():   # what the popup does, without a popup (only used to fill the saved tool list once)
            a = ok(api("post", "/api/oauth/connect/mcp-fk", {})).json()["authorize_url"]
            loc = ctx.request.get(a, max_redirects=0).headers["location"]; ctx.request.get(loc, max_redirects=0)
        connect_api()
        sid = [s for s in api("get", "/api/mcp").json()["servers"] if s["name"] == "fk"][0]["id"]
        ok(api("post", f"/api/mcp/servers/{sid}/refresh-tools")); ok(api("post", "/api/mcp/toggle", {"server": "fk", "tool": "whoami", "enabled": True}))
        agent = ok(api("post", "/api/agents", {"name": "Trợ lý kết nối", "system_prompt": "Bạn là trợ lý.", "tools": ["fk:whoami"]})).json()["id"]
        a = next(x for x in api("post", "/api/v1/assistants/search", {"limit": 50}).json() if x["agent_id"] == agent)
        ok(api("patch", f"/api/v1/assistants/{a['assistant_id']}", {"metadata": {"chat_visible": True}}))
        disconnect = lambda: ok(api("delete", "/api/connections/mcp-fk"))
        disconnect()
        errs = []
        def chat_page():
            pg = ctx.new_page(); pg.on("pageerror", lambda e: errs.append(str(e)[:200]))
            pg.goto(U + "/chat/", wait_until="networkidle"); pg.wait_for_selector(f"[data-testid='agent-agent-{agent}']"); pg.click(f"[data-testid='agent-agent-{agent}']")
            pg.wait_for_selector(".aui-composer-input"); return pg
        def ask(pg):
            pg.fill(".aui-composer-input", "mcp-whoami"); pg.keyboard.press("Enter")
        def shot(pg, name): pg.screenshot(path=os.path.join(OUT, name))
        def click_connect(pg):
            with ctx.expect_page() as pop:
                pg.click("[data-testid='connect-open']")
            return pop.value   # the card closes the popup itself once the sign-in is done
        results = {}
        def _dump(e):
            for n, pg in enumerate(ctx.pages):
                try: pg.screenshot(path=os.path.join(os.environ.get("DEBUG_DIR", OUT), f"_fail_{n}.png")); print("PAGE", n, pg.url, pg.inner_text("body")[-700:].replace(chr(10), " | "))
                except Exception: pass
        try:
            for lang in ("vi", "en"):
                ok(api("post", "/api/auth/lang", {"lang": lang}))
                T = {"vi": {"gate": "Kết nối MCP fk để tiếp tục", "skip": "Bỏ qua", "done": "Đã kết nối"}, "en": {"gate": "Connect MCP fk to continue", "skip": "Skip", "done": "Connected"}}[lang]
                # ---- A) gate -> skip -> tool pause (auth_required) -> connect -> the run continues
                pg = chat_page(); ask(pg)
                pg.wait_for_selector("[data-testid='connect-card'][data-kind='gate']", timeout=30000)
                txt = pg.inner_text("[data-testid='connect-card']"); assert T["gate"] in txt and T["skip"] in txt, txt
                shot(pg, f"11.29_gate_card_{lang}.png")
                pg.click("[data-testid='connect-skip']")
                pg.wait_for_selector("[data-testid='approval-card'] [data-slot='approval-card-allow-once']", timeout=30000)   # every MCP tool waits for an approval first
                pg.click("[data-testid='approval-card'] [data-slot='approval-card-allow-once']")
                pg.wait_for_selector("[data-testid='connect-card'][data-kind='auth']", timeout=30000)
                txt = pg.inner_text("[data-testid='connect-card']"); assert "MCP fk" in txt, txt
                shot(pg, f"11.29_tool_card_{lang}.png")
                pu = click_connect(pg)
                pg.wait_for_selector("[data-testid='connect-resuming'], [data-testid='connect-done']", timeout=40000)
                pg.get_by_text("user-1").first.wait_for(timeout=40000)
                results[f"A_{lang}"] = "skip -> approval -> tool pause -> connect -> run continued, the tool answered user-1"
                pg.close()
                # ---- B) disconnected again -> a new run pauses at the gate -> Connect -> continues by itself
                disconnect()
                pg = chat_page(); ask(pg)
                pg.wait_for_selector("[data-testid='connect-card'][data-kind='gate']", timeout=30000)
                pu = click_connect(pg)
                pg.wait_for_selector("[data-testid='approval-card'] [data-slot='approval-card-allow-once']", timeout=40000)   # the run continued: the MCP tool asks for approval
                pg.click("[data-testid='approval-card'] [data-slot='approval-card-allow-once']")
                pg.get_by_text("user-1").first.wait_for(timeout=40000)
                results[f"B_{lang}"] = "gate -> connect -> run continued"
                pg.close(); disconnect()
                # ---- settings: Connections (3 tabs) and the Add MCP server dialog
                sp = ctx.new_page(); sp.on("pageerror", lambda e: errs.append(str(e)[:200]))
                for tab, q in (("me", ""), ("agent", f"&ct=agent&ag={agent}"), ("workspace", "&ct=workspace")):
                    sp.goto(f"{U}/settings?s=connections{q}", wait_until="networkidle"); sp.wait_for_selector("#conncard")
                    txt = sp.inner_text("#conncard")
                    if tab == "agent": assert ("Mọi người dùng agent này sẽ hành động bằng tài khoản này" if lang == "vi" else "Everyone who uses this agent will act with this account") in txt, txt
                    if lang == "vi": assert re.search("[" + VI_CH + "]", txt), txt
                    if tab == "me": assert "mcp-fk" in txt and ("Chưa kết nối" if lang == "vi" else "Not connected") in txt, txt
                    shot(sp, f"11.29_connections_{tab}_{lang}.png")
                sp.goto(f"{U}/settings?s=connections", wait_until="networkidle"); sp.wait_for_selector("#conncard [data-cn]")
                sp.click("#conncard [data-cn]"); sp.wait_for_url(re.compile(r".*/settings\?s=connections.*"), timeout=20000); sp.wait_for_selector("#conncard .chip.ok")
                shot(sp, f"11.29_connections_active_{lang}.png"); disconnect()
                sp.goto(f"{U}/settings?s=mcp", wait_until="networkidle"); sp.wait_for_selector("[data-mr]")
                assert ("Đăng ký lại" if lang == "vi" else "Re-register") in sp.inner_text("main, body")
                sp.click("#nm"); sp.fill("#mu", MCP); sp.click("#mp"); sp.wait_for_selector("#mform", state="visible", timeout=20000)
                sp.wait_for_selector("#mman", state="visible"); shot(sp, f"11.29_mcp_dialog_{lang}.png")
                sp.close()
        except BaseException as e:   # show what every page looked like, then fail
            _dump(e); raise
        ok(api("post", "/api/auth/lang", {"lang": old_lang}))
        print(json.dumps(results, ensure_ascii=False, indent=1)); print("page errors:", errs[:5] or "none")
        b.close()
        sys.exit(1 if errs else 0)

main()
