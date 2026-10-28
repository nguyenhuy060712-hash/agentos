// Static search index for /dao-tao/. Public fields only: never reads plan / ask / notes / slide notes / lab.check / quiz answers.
import type { APIRoute } from "astro";
import { modules, moduleHref } from "../../data/training/programme";
import { clip, strip } from "../../lib/training";

export const GET: APIRoute = () => {
  const out: { t: string; h: string; u: string; x: string }[] = [];
  for (const m of modules) {
    const t = `Buổi ${m.n}: ${m.title}`;
    const u = moduleHref(m.slug);
    const add = (h: string, anchor: string, x: string) => out.push({ t, h, u: anchor ? `${u}#${anchor}` : u, x: clip(strip(x)) });
    add(m.title, "", m.summary);
    add("Mục tiêu", "muc-tieu", m.goals.join(" · "));
    for (const s of m.sections) add(s.h, s.id, s.html);
    add(/^lab[ :.]/i.test(m.lab.title) ? m.lab.title : `Lab: ${m.lab.title}`, "lab", [m.lab.setup ?? "", m.lab.result].join(" "));
    for (const [term, def] of m.glossary) add(`Thuật ngữ: ${term}`, "thuat-ngu", def);
    if (m.homework.length) add("Bài tập về nhà", "bai-tap", m.homework.join(" · "));
  }
  return new Response(JSON.stringify(out), { headers: { "Content-Type": "application/json; charset=utf-8" } });
};
