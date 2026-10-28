import { chromium } from "playwright-core";
const B=process.env.BASE || "http://127.0.0.1:4321";
const PFX=process.env.PFX || "";
const b=await chromium.launch({executablePath:"/usr/bin/google-chrome"});
const O="/workspace/agentos-show/shots/";
const ext=[];
async function shot(name, path, vp, scheme, full=true){
  const c=await b.newContext({viewport:vp, deviceScaleFactor: vp.width<500?2:1, colorScheme:scheme});
  const p=await c.newPage();
  p.on("request",r=>{ if(!r.url().startsWith(B)) ext.push(r.url()); });
  p.on("console",m=>{ if(m.type()==="error") console.log("console error",name,m.text()); });
  await p.goto(B+path,{waitUntil:"networkidle"});
  // force lazy images
  await p.evaluate(async()=>{ document.documentElement.style.scrollBehavior="auto"; const H=document.body.scrollHeight; for(let y=0;y<H;y+=600){ window.scrollTo(0,y); await new Promise(r=>setTimeout(r,60)); } window.scrollTo(0,0); });
  await p.waitForFunction(()=>[...document.images].every(i=>i.complete && i.naturalWidth>0),null,{timeout:20000}).catch(()=>console.log("imgs not all loaded",name));
  await p.waitForTimeout(800);
  await p.screenshot({path:O+PFX+name+".png", fullPage:full});
  await c.close();
}
await shot("desktop-vi-light-hero","/",{width:1440,height:900},"light",false);
await shot("desktop-vi-light-full","/",{width:1440,height:900},"light");
await shot("desktop-vi-dark-full","/",{width:1440,height:900},"dark");
await shot("desktop-en-light-full","/en/",{width:1440,height:900},"light");
await shot("mobile-vi-light-hero","/",{width:390,height:844},"light",false);
await shot("mobile-vi-light-full","/",{width:390,height:844},"light");
await shot("mobile-vi-dark-full","/",{width:390,height:844},"dark");
console.log("external requests:", ext.length, ext.slice(0,5));
await b.close();
