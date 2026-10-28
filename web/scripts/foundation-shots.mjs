import { chromium } from "playwright-core";
const U = "https://suggested-ran-night-period.trycloudflare.com";
const b = await chromium.launch({ executablePath: "/usr/bin/google-chrome", args: ["--no-sandbox"] });
const out = "/workspace/agentos-show/shots/";
const errs = [];
for (const [name, path, vp, mobile] of [["desktop-vi","/",{width:1440,height:900},false],["mobile-vi","/",{width:390,height:844},true],["desktop-en","/en/",{width:1440,height:900},false],["mobile-en","/en/",{width:390,height:844},true]]) {
  const ctx = await b.newContext({ viewport: vp, deviceScaleFactor: mobile?2:1, isMobile: mobile, hasTouch: mobile });
  const p = await ctx.newPage();
  p.on("console", m => m.type()==="error" && errs.push(name+": "+m.text()));
  await p.goto(U+path, { waitUntil: "networkidle" });
  await p.addStyleTag({ content: "html{scroll-behavior:auto!important}" });
  const el = p.locator("#foundation");
  const h = await el.evaluate(e => { const r=e.getBoundingClientRect(); return {top:r.top+scrollY,h:r.height}; });
  for (let y = h.top; y < h.top + h.h; y += 500) { await p.evaluate(y=>scrollTo(0,y), y); await p.waitForTimeout(80); }
  await p.locator("#foundation details").evaluate(d => d.open = true);
  await p.waitForTimeout(300);
  await p.addStyleTag({ content: "header{visibility:hidden!important}" });
  await el.screenshot({ path: out+`vps83-foundation-${name}.png` });
  if (name==="desktop-vi") {
    const n = await p.$$eval('a[href^="http"]', as => as.filter(a=>a.target!=="_blank"&&!a.href.startsWith(location.origin)).length);
    console.log("ext without _blank:", n);
    await p.setViewportSize({width:1280,height:800}); await p.evaluate(()=>scrollTo(0,0)); await p.waitForTimeout(200);
    await p.addStyleTag({ content: "header{visibility:visible!important}" });
    const hdr = await p.$eval("header", h => { const r=[...h.querySelectorAll("a,button")].map(e=>e.getBoundingClientRect().right); return {maxRight:Math.max(...r), sw:h.scrollWidth}; });
    console.log("header@1280", JSON.stringify(hdr));
    const nav = await p.$eval("header nav", n => ({sh:n.scrollHeight, w:n.scrollWidth, cw:n.clientWidth, links:n.querySelectorAll("a").length}));
    console.log("nav@1280", JSON.stringify(nav));
    await p.screenshot({ path: out+"vps83-foundation-nav-1280.png", clip:{x:0,y:0,width:1280,height:80} });
  }
  await ctx.close();
}
console.log("console errors:", errs.length, errs.slice(0,3));
await b.close();
