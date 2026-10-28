// Renders public/og-vi.png and public/og-en.png (1200x630) with headless Chrome.
import { chromium } from "playwright-core";
import { readFileSync } from "node:fs";
const root = new URL("..", import.meta.url).pathname;
const b64 = (p) => readFileSync(root + p).toString("base64");
const font = (f) => `data:font/woff2;base64,${b64("node_modules/" + f)}`;
const shot = `data:image/png;base64,${b64("src/assets/shots/chat-canvas.png")}`;
const logo = `data:image/png;base64,${b64("src/assets/mozaai-icon.png")}`;
const T = {
  vi: { k: "Mỗi khách hàng một bộ riêng · Tự host trên VPS", h: "Đội ngũ AI riêng của doanh nghiệp", s: "Agent OS Chat + Agent OS: chat kiểu ChatGPT, voice song công, agent làm việc thật. Dữ liệu ở lại máy chủ của bạn." },
  en: { k: "One dedicated stack per customer · Self-hosted", h: "Your company's own AI team", s: "Agent OS Chat + Agent OS: ChatGPT-style chat, full-duplex voice, agents that do real work. Your data stays on your server." },
};
const b = await chromium.launch({ executablePath: "/usr/bin/google-chrome" });
const p = await b.newPage({ viewport: { width: 1200, height: 630 } });
for (const [lang, t] of Object.entries(T)) {
  await p.setContent(`<!doctype html><html><head><style>
  @font-face{font-family:BVP;font-weight:700;src:url(${font("@fontsource/be-vietnam-pro/files/be-vietnam-pro-vietnamese-700-normal.woff2")}) format("woff2");unicode-range:U+0102-0103,U+0110-0111,U+0128-0129,U+0168-0169,U+01A0-01A1,U+01AF-01B0,U+0300-0301,U+0303-0304,U+0308-0309,U+0323,U+0329,U+1EA0-1EF9,U+20AB}
  @font-face{font-family:BVP;font-weight:700;src:url(${font("@fontsource/be-vietnam-pro/files/be-vietnam-pro-latin-700-normal.woff2")}) format("woff2")}
  @font-face{font-family:BVP;font-weight:400;src:url(${font("@fontsource/be-vietnam-pro/files/be-vietnam-pro-vietnamese-600-normal.woff2")}) format("woff2");unicode-range:U+0102-0103,U+0110-0111,U+0128-0129,U+0168-0169,U+01A0-01A1,U+01AF-01B0,U+0300-0301,U+0303-0304,U+0308-0309,U+0323,U+0329,U+1EA0-1EF9,U+20AB}
  @font-face{font-family:BVP;font-weight:400;src:url(${font("@fontsource/be-vietnam-pro/files/be-vietnam-pro-latin-600-normal.woff2")}) format("woff2")}
  *{margin:0;box-sizing:border-box}body{width:1200px;height:630px;font-family:BVP;background:#0f1115;color:#fff;overflow:hidden;position:relative}
  .g{position:absolute;inset:-200px -100px auto auto;width:900px;height:700px;border-radius:50%;filter:blur(90px);opacity:.55;background:linear-gradient(110deg,#12b5ff,#3d5bff 30%,#7b3cff 65%,#ff3fa4)}
  .c{position:absolute;left:64px;top:64px;width:560px}.l{display:flex;align-items:center;gap:12px;font-size:26px;font-weight:700}
  .k{margin-top:40px;font-size:18px;color:#c9c9d6;font-weight:400}h1{margin-top:14px;font-size:58px;line-height:1.08;font-weight:700}
  .s{margin-top:22px;font-size:21px;line-height:1.45;color:#d4d4de;font-weight:400}.u{position:absolute;left:64px;bottom:48px;font-size:18px;color:#a8a8b8;font-weight:400}
  .i{position:absolute;right:-120px;top:120px;width:640px;border-radius:14px;box-shadow:0 30px 80px rgba(0,0,0,.5);border:1px solid rgba(255,255,255,.15)}
  </style></head><body><div class="g"></div><img class="i" src="${shot}"><div class="c"><div class="l"><img src="${logo}" width="40" height="40">Agent OS · MozaAI</div><div class="k">${t.k}</div><h1>${t.h}</h1><div class="s">${t.s}</div></div><div class="u">mozaai.vn</div></body></html>`);
  await p.waitForTimeout(300);
  await p.screenshot({ path: `${root}public/og-${lang}.png` });
}
await b.close();
