// Builds the training PDFs with headless Chrome.
//   public   : dist/ (a fresh public build must exist)  ->  public/dao-tao/Giao-trinh-hoc-vien.pdf  (A4, /dao-tao/in/)
//                                                         public/dao-tao/Slide-trinh-chieu.pdf      (1280x720, /dao-tao/slide/tat-ca/)
//   instructor: builds with INSTRUCTOR=1 into dist-gv/    ->  tai-lieu-giang-vien/Tai-lieu-giang-vien-Agent-OS.pdf (A4, /dao-tao/giang-vien/), then deletes dist-gv/
// Usage: node scripts/pdf-training.mjs [--public] [--gv]      (no flag = both). CHROME=<path> overrides the Chrome location.
import { chromium } from "playwright-core";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const CHROME = process.env.CHROME || (process.platform === "win32" ? "C:/Program Files/Google/Chrome/Application/chrome.exe" : "/usr/bin/google-chrome");
const args = process.argv.slice(2);
const doPublic = args.includes("--public") || !args.includes("--gv");
const doGv = args.includes("--gv") || !args.includes("--public");

const TYPES = { ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8", ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml", ".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".woff2": "font/woff2", ".woff": "font/woff", ".ico": "image/x-icon", ".txt": "text/plain; charset=utf-8", ".xml": "application/xml", ".pdf": "application/pdf" };

/** Tiny static server for a build folder; resolves to { url, close }. */
function serve(dir) {
  const server = http.createServer((req, res) => {
    let p = decodeURIComponent(new URL(req.url, "http://x").pathname);
    let file = path.join(dir, p);
    if (!file.startsWith(dir)) { res.writeHead(403).end(); return; }
    if (fs.existsSync(file) && fs.statSync(file).isDirectory()) file = path.join(file, "index.html");
    if (!fs.existsSync(file)) { res.writeHead(404).end("not found"); return; }
    res.writeHead(200, { "Content-Type": TYPES[path.extname(file).toLowerCase()] || "application/octet-stream" });
    fs.createReadStream(file).pipe(res);
  });
  return new Promise((ok) => server.listen(0, "127.0.0.1", () => ok({ url: `http://127.0.0.1:${server.address().port}`, close: () => new Promise((r) => server.close(r)) })));
}

const pageCount = (file) => (fs.readFileSync(file).toString("latin1").match(/\/Type\s*\/Page(?![s\w])/g) || []).length;
const kb = (file) => `${Math.round(fs.statSync(file).size / 1024)} KB`;
const report = [];

/** Open a page, wait for fonts and every image (scrolling through to trigger lazy ones), return it ready to print. */
async function ready(browser, url, viewport) {
  const ctx = await browser.newContext({ viewport, colorScheme: "light", deviceScaleFactor: 1 });
  const page = await ctx.newPage();
  const failed = [];
  page.on("requestfailed", (r) => failed.push(r.url()));
  const res = await page.goto(url, { waitUntil: "networkidle" });
  if (!res || res.status() !== 200) throw new Error(`${url} -> HTTP ${res?.status()}`);
  await page.evaluate(async () => {
    document.documentElement.style.scrollBehavior = "auto";
    document.documentElement.classList.remove("dark");
    for (const img of document.images) img.loading = "eager";
    for (let y = 0; y < document.body.scrollHeight; y += 600) { window.scrollTo(0, y); await new Promise((r) => setTimeout(r, 30)); }
    window.scrollTo(0, 0);
    await document.fonts.ready;
    await Promise.all([...document.images].map((i) => (i.complete ? 0 : new Promise((r) => { i.onload = i.onerror = r; }))));
    await Promise.all([...document.images].map((i) => i.decode().catch(() => {})));
  });
  await page.waitForLoadState("networkidle");
  if (failed.length) console.warn(`  warning: ${failed.length} failed request(s), e.g. ${failed[0]}`);
  return { page, ctx };
}

const footer = `<div style="width:100%;font-size:8px;font-family:Arial,sans-serif;color:#71717a;text-align:center;padding-top:4px">Trang <span class="pageNumber"></span>/<span class="totalPages"></span></div>`;
async function printA4(browser, url, out) {
  const { page, ctx } = await ready(browser, url, { width: 1100, height: 1400 });
  fs.mkdirSync(path.dirname(out), { recursive: true });
  await page.pdf({ path: out, format: "A4", printBackground: true, displayHeaderFooter: true, headerTemplate: "<span></span>", footerTemplate: footer, margin: { top: "16mm", bottom: "18mm", left: "14mm", right: "14mm" } });
  await ctx.close();
  report.push(`${path.relative(root, out)}: ${kb(out)}, ${pageCount(out)} pages`);
}

async function printSlides(browser, url, out) {
  const { page, ctx } = await ready(browser, url, { width: 1400, height: 800 });
  // Warn about slides whose content runs into the footer (too much text for one 16:9 slide).
  const over = await page.evaluate(() => [...document.querySelectorAll(".slide")].flatMap((s) => {
    const foot = s.querySelector(".s-foot")?.getBoundingClientRect().top ?? Infinity;
    const body = [...s.querySelectorAll(".s-body *, .s-goals *, .s-sub")].reduce((m, el) => Math.max(m, el.getBoundingClientRect().bottom), 0);
    return body > foot - 2 ? [s.id] : [];
  }));
  fs.mkdirSync(path.dirname(out), { recursive: true });
  await page.pdf({ path: out, width: "1280px", height: "720px", printBackground: true, preferCSSPageSize: true });
  await ctx.close();
  report.push(`${path.relative(root, out)}: ${kb(out)}, ${pageCount(out)} pages` + (over.length ? `; ${over.length} slide(s) too full: ${over.slice(0, 8).join(", ")}` : "; no slide overflow"));
}

function build(outDir, env) {
  return new Promise((ok, fail) => {
    const p = spawn(process.execPath, [path.join(root, "node_modules/astro/bin/astro.mjs"), "build", "--outDir", outDir], { cwd: root, stdio: ["ignore", "ignore", "inherit"], env: { ...process.env, ASTRO_TELEMETRY_DISABLED: "1", NODE_OPTIONS: "--max-old-space-size=3072", ...env } });
    p.on("exit", (c) => (c === 0 ? ok() : fail(new Error(`astro build exited with ${c}`))));
  });
}

const browser = await chromium.launch({ executablePath: CHROME, args: ["--disable-gpu"] });
try {
  if (doPublic) {
    const dist = path.join(root, "dist");
    if (!fs.existsSync(path.join(dist, "dao-tao", "in", "index.html"))) throw new Error("dist/ has no /dao-tao/in/: run `npm run build` first");
    const srv = await serve(dist);
    try {
      await printA4(browser, `${srv.url}/dao-tao/in/`, path.join(root, "public", "dao-tao", "Giao-trinh-hoc-vien.pdf"));
      await printSlides(browser, `${srv.url}/dao-tao/slide/tat-ca/`, path.join(root, "public", "dao-tao", "Slide-trinh-chieu.pdf"));
    } finally { await srv.close(); }
  }
  if (doGv) {
    const gv = path.join(root, "dist-gv");
    fs.rmSync(gv, { recursive: true, force: true });
    try {
      console.log("building the instructor kit (INSTRUCTOR=1) into dist-gv/ ...");
      await build("dist-gv", { INSTRUCTOR: "1" });
      const srv = await serve(gv);
      try { await printA4(browser, `${srv.url}/dao-tao/giang-vien/`, path.join(root, "tai-lieu-giang-vien", "Tai-lieu-giang-vien-Agent-OS.pdf")); }
      finally { await srv.close(); }
    } finally { fs.rmSync(gv, { recursive: true, force: true }); }
  }
} finally { await browser.close(); }
console.log(report.join("\n"));
