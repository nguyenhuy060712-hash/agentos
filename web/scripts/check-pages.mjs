// Opens every built page (desktop 1440 + phone 390, light + dark) against a running `astro preview`, and fails on:
// console errors, failed or external requests, horizontal overflow, broken in-page anchors / internal links, broken images.
// Usage: BASE=http://127.0.0.1:4321 CHROME=<path to chrome> node scripts/check-pages.mjs [--shots]
// --shots also writes screenshots of the main pages to shots/site/.
import { chromium } from "playwright-core";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const BASE = process.env.BASE || "http://127.0.0.1:4321";
const CHROME = process.env.CHROME || (process.platform === "win32" ? "C:/Program Files/Google/Chrome/Application/chrome.exe" : "/usr/bin/google-chrome");
const SHOTS = process.argv.includes("--shots");
const OUT = path.join(root, "shots", "site");
if (SHOTS) fs.mkdirSync(OUT, { recursive: true });

// Every HTML page in dist/.
const pages = [];
(function walk(d, rel) {
  for (const f of fs.readdirSync(d)) {
    const p = path.join(d, f);
    if (fs.statSync(p).isDirectory()) { if (!f.startsWith("_")) walk(p, rel + f + "/"); }
    else if (f === "index.html") pages.push("/" + rel);
  }
})(path.join(root, "dist"), "");
pages.sort();

const views = [
  { name: "desktop", viewport: { width: 1440, height: 900 }, scale: 1 },
  { name: "mobile", viewport: { width: 390, height: 844 }, scale: 2, mobile: true },
];
const shotList = { "/": "trang-chu", "/en/": "home-en", "/tinh-nang/": "tinh-nang", "/en/features/": "features-en", "/hoc/": "hoc", "/hoc/agent-la-gi/": "hoc-bai-1", "/hoc/thiet-ke-he-thong/": "hoc-bai-7" };

const browser = await chromium.launch({ executablePath: CHROME, args: ["--disable-gpu"] });
const problems = [];
const known = new Set(pages);
let checks = 0;
for (const v of views) {
  for (const scheme of ["light", "dark"]) {
    const ctx = await browser.newContext({ viewport: v.viewport, deviceScaleFactor: v.scale, isMobile: !!v.mobile, hasTouch: !!v.mobile, colorScheme: scheme });
    for (const pg of pages) {
      const page = await ctx.newPage();
      const tag = `${pg} [${v.name}/${scheme}]`;
      page.on("console", (m) => { if (m.type() === "error") problems.push(`${tag} console: ${m.text()}`); });
      page.on("pageerror", (e) => problems.push(`${tag} pageerror: ${e.message}`));
      page.on("requestfailed", (r) => problems.push(`${tag} request failed: ${r.url()}`));
      page.on("request", (r) => { if (!r.url().startsWith(BASE) && !r.url().startsWith("data:")) problems.push(`${tag} external request: ${r.url()}`); });
      const res = await page.goto(BASE + pg, { waitUntil: "networkidle" });
      if (!res || res.status() !== 200) problems.push(`${tag} HTTP ${res?.status()}`);
      // Load lazy images, then check them.
      await page.evaluate(async () => { document.documentElement.style.scrollBehavior = "auto"; for (let y = 0; y < document.body.scrollHeight; y += 700) { window.scrollTo(0, y); await new Promise((r) => setTimeout(r, 40)); } window.scrollTo(0, 0); });
      await page.waitForLoadState("networkidle");
      const r = await page.evaluate(() => {
        const out = { overflow: [], badImg: [], badHash: [], links: [] };
        const W = document.documentElement.clientWidth;
        if (document.documentElement.scrollWidth > W + 1) {
          for (const el of document.querySelectorAll("body *")) {
            const b = el.getBoundingClientRect();
            if (b.right > W + 1 && b.width > 0 && !el.closest(".overflow-x-auto, .tbl, .dg, pre, table, [hidden], details:not([open])")) { out.overflow.push(el.tagName + "." + (el.className?.toString?.() || "").slice(0, 60) + " right=" + Math.round(b.right)); if (out.overflow.length > 4) break; }
          }
          if (!out.overflow.length) out.overflow.push(`scrollWidth ${document.documentElement.scrollWidth} > ${W}`);
        }
        for (const img of document.images) if (img.getClientRects().length && !img.closest("dialog") && (!img.complete || img.naturalWidth === 0)) out.badImg.push(img.currentSrc || img.src);
        for (const a of document.querySelectorAll("a[href]")) {
          const h = a.getAttribute("href");
          if (h.startsWith("#") && h.length > 1 && !document.getElementById(decodeURIComponent(h.slice(1)))) out.badHash.push(h);
          else if (h.startsWith("/") && !h.startsWith("//")) out.links.push(h);
        }
        return out;
      });
      checks++;
      r.overflow.forEach((o) => problems.push(`${tag} horizontal overflow: ${o}`));
      r.badImg.forEach((o) => problems.push(`${tag} broken image: ${o}`));
      [...new Set(r.badHash)].forEach((o) => problems.push(`${tag} missing anchor: ${o}`));
      for (const l of new Set(r.links)) {
        const [p, hash] = l.split("#");
        if (/\.(png|webp|jpg|svg|xml|txt|pdf|json)$/.test(p) || p.startsWith("/_astro/")) continue;
        if (!known.has(p)) problems.push(`${tag} link to unknown page: ${l}`);
        else if (hash && v.name === "desktop" && scheme === "light") {
          const html = fs.readFileSync(path.join(root, "dist", p, "index.html"), "utf8");
          if (!html.includes(`id="${hash}"`)) problems.push(`${tag} link to missing anchor: ${l}`);
        }
      }
      if (SHOTS && shotList[pg] && scheme === "light") await page.screenshot({ path: path.join(OUT, `${shotList[pg]}-${v.name}.png`), fullPage: true });
      if (SHOTS && scheme === "dark" && v.name === "desktop" && pg === "/tinh-nang/") await page.screenshot({ path: path.join(OUT, `tinh-nang-dark-desktop.png`) });
      await page.close();
    }
    await ctx.close();
  }
}
await browser.close();
console.log(`${pages.length} pages × ${views.length} viewports × 2 themes = ${checks} checks`);
if (problems.length) { console.log(`FAIL: ${problems.length} problem(s)`); for (const p of [...new Set(problems)].slice(0, 60)) console.log(" - " + p); process.exit(1); }
console.log("PASS: 0 console errors, 0 failed/external requests, no horizontal overflow, all anchors, links and images OK");
