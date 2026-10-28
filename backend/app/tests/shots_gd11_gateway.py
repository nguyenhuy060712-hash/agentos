"""GĐ11.22 LLM Gateway page: walks the 3-step wizard against a FAKE OpenAI-compatible provider (tests/fakes/fake_providers.py, $0), then screenshots every tab in vi and en
into docs/anh_nghiem_thu/11.22_<tab>_<lang>.png and prints a text check (get_page_text style) per tab.
Env: AOS_URL (default http://127.0.0.1:8021), AOS_USER / AOS_PASSWORD (local stack), FAKE_URL (the fake provider's /v1 base, e.g. http://127.0.0.1:46877/v1), FAKE_KEY, WS (workspace id to use; session switched with POST /api/auth/workspace).
The caller prepares: a throw-away workspace (POST /api/admin/workspaces) and the fake provider server (its port is printed by tests/fakes). Delete the workspace afterwards."""
import json, os, sys, time
from playwright.sync_api import sync_playwright

U = os.environ.get("AOS_URL", "http://127.0.0.1:8021").rstrip("/")
USER, PW = os.environ.get("AOS_USER", "moza"), os.environ.get("AOS_PASSWORD", "phase0-local")
FAKE, KEY, WS = os.environ["FAKE_URL"], os.environ.get("FAKE_KEY", "sk-fake-provider-key-1234"), os.environ["WS"]
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "docs", "anh_nghiem_thu")
TABS = ["start", "providers", "models", "fallbacks", "limits", "keys"]
EXPECT_VI = {"start": "Bước 1", "providers": "Thêm nhà cung cấp", "models": "Cấu hình model", "fallbacks": "Chuỗi model dự phòng", "limits": "Hạn mức toàn workspace", "keys": "Chạy cấu hình này"}
EXPECT_EN = {"start": "Step 1", "providers": "Add provider", "models": "Model configurations", "fallbacks": "Fallback chains", "limits": "Workspace limits", "keys": "Virtual keys"}

VI_CH = "ạảãấầẩẫậắằẳẵặẹẻẽếềểễệịỉĩọỏõốồổỗộớờởỡợụủũứừửữựỳỵỷỹđâăêôơưáàéèíìóòúùýÂĂÊÔƠƯ"

def main():
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome")
        ctx = b.new_context(viewport={"width": 1440, "height": 1500})
        r = ctx.request.post(U + "/api/auth/login", data=json.dumps({"username": USER, "password": PW}), headers={"Content-Type": "application/json"}); assert r.ok, r.status
        r = ctx.request.post(U + "/api/auth/workspace", data=json.dumps({"workspace_id": int(WS)}), headers={"Content-Type": "application/json"}); assert r.ok, r.status   # session -> the throw-away workspace
        errs = []
        pg = ctx.new_page(); pg.on("pageerror", lambda e: errs.append(str(e)[:200]))
        H = {"Content-Type": "application/json"}; old_lang = ctx.request.get(U + "/api/auth/me").json().get("lang") or "en"   # the language is stored on the user: restored at the end
        ctx.request.post(U + "/api/auth/lang", data=json.dumps({"lang": "vi"}), headers=H)
        t0 = time.time()
        pg.goto(U + "/gateway?tab=start", wait_until="networkidle"); pg.wait_for_selector("#w1go")
        pg.select_option("#w1k", "openai_compat"); pg.fill("#w1url", FAKE); pg.fill("#w1key", KEY); pg.click("#w1go")
        pg.wait_for_selector("#w2go", timeout=15000); pg.wait_for_selector("text=Kết nối tốt", timeout=15000)
        pg.fill("#w2n", "ung-dung-thu"); pg.click("#w2go"); pg.wait_for_selector("pre:has-text('vk-')", timeout=15000)
        pg.fill("#w3v", "10"); pg.click("#w3go"); pg.wait_for_function("document.querySelectorAll('.card .chip.ok svg').length >= 3", timeout=15000)
        print("wizard seconds:", round(time.time() - t0, 1))
        # richer data for the other tabs (API, same session)
        api = lambda m, u, j=None: getattr(ctx.request, m)(U + u, data=json.dumps(j) if j is not None else None, headers={"Content-Type": "application/json"})
        shots = {}
        pg.screenshot(path=os.path.join(OUT, "11.22_start_vi.png"), full_page=True); shots["start_vi"] = pg.inner_text("body")
        for r in (api("post", "/api/gw/fallbacks", {"primary_ref": "gemini-3.6-flash", "candidates": ["openai_compat/fake-model-a"]}),
                  api("post", "/api/model_configs", {"name": "Nhanh-re", "model": "fake-model-a", "provider": "openai_compat", "temperature": 0.2}),
                  api("put", "/api/gw/limits", {"day_usd": 5, "month_usd": 10, "provider_caps": {"openai_compat": {"month_usd": 3}}})):
            if not r.ok: print("seed warn", r.status, r.text()[:150])
        for lang in ("vi", "en"):
            ctx.request.post(U + "/api/auth/lang", data=json.dumps({"lang": lang}), headers=H)
            for t in TABS:
                if lang == "vi" and t == "start": continue
                pg.goto(f"{U}/gateway?tab={t}", wait_until="networkidle"); pg.wait_for_timeout(1200)
                txt = pg.inner_text("body"); want = (EXPECT_VI if lang == "vi" else EXPECT_EN)[t]
                print(f"[{lang}/{t}] {'OK' if want.lower() in txt.lower() else 'MISSING ' + want}")
                if lang == "en":
                    left = [l for l in txt.splitlines() if any(c in l for c in VI_CH)]
                    if left: print("  vietnamese left in EN:", left[:8])
                pg.screenshot(path=os.path.join(OUT, f"11.22_{t}_{lang}.png"), full_page=True)
        ctx.request.post(U + "/api/auth/lang", data=json.dumps({"lang": old_lang}), headers=H)
        print("page errors:", errs)
        b.close()

if __name__ == "__main__": main()
