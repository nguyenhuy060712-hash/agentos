// GĐ5b pure helpers (no browser/API imports: unit-tested in node).
import { diffLines } from "diff";
import { parseFiles, stripFileLines, type OutFile } from "./files-helpers";

/** canvas tool result "... [canvas:<doc_id>:<version>]" */
export function canvasRef(result: unknown): { doc: string; v: number } | null {
  const m = typeof result === "string" ? /\[canvas:([0-9a-f]{6,32}):(\d+)\]/.exec(result) : null;
  return m ? { doc: m[1], v: Number(m[2]) } : null;
}

export type CodeRun = { status: string; exit: number; ms: number; runtime: string; stdout: string; stderr: string; images: string[]; files?: OutFile[] };
/** chay_code_python tool text -> parts ("status=ok exit=0 (812 ms, gvisor)\n[stdout]\n…\n[stderr]\n…\n[2 biểu đồ …] [aos-images:a,b]") */
export function parseCodeRun(result: unknown): CodeRun | null {
  if (typeof result !== "string") return null;
  const h = /^status=(\w+) exit=(-?\d+) \((\d+) ms, (.*)\)$/.exec(result.split("\n", 1)[0]);
  if (!h) return null;
  const img = /\[aos-images:([\w,-]+)\]/.exec(result), files = parseFiles(result);
  const body = stripFileLines(result.slice(h[0].length)).replace(/\n?\[tệp không lưu được\][^\n]*/, "").replace(/\n?\[\d+ biểu đồ[^\n]*$/, "");
  const so = /\n\[stdout\]\n([\s\S]*?)(?=\n\[stderr\]\n|$)/.exec(body), se = /\n\[stderr\]\n([\s\S]*)$/.exec(body);
  return { status: h[1], exit: Number(h[2]), ms: Number(h[3]), runtime: h[4], stdout: so?.[1] ?? "", stderr: se?.[1] ?? "", images: img ? img[1].split(",").filter(Boolean) : [], ...(files.length ? { files } : {}) };
}

export type DiffRow = { kind: "add" | "del" | "same"; text: string; a?: number; b?: number };
/** line diff of two versions (npm `diff`, BSD-3-Clause); long unchanged runs are folded to 3 lines of context */
export function lineDiff(a: string, b: string, ctx = 3): { rows: (DiffRow | { kind: "fold"; n: number })[]; added: number; removed: number } {
  const rows: DiffRow[] = []; let ia = 1, ib = 1, added = 0, removed = 0;
  const nl = (x: string) => (x.endsWith("\n") || !x ? x : x + "\n");   // a missing final newline must not mark the last line as changed
  for (const p of diffLines(nl(a), nl(b))) {
    const lines = p.value.replace(/\n$/, "").split("\n");
    for (const text of lines) {
      if (p.added) { rows.push({ kind: "add", text, b: ib++ }); added++; }
      else if (p.removed) { rows.push({ kind: "del", text, a: ia++ }); removed++; }
      else rows.push({ kind: "same", text, a: ia++, b: ib++ });
    }
  }
  const keep = rows.map((r, i) => r.kind !== "same" || rows.slice(Math.max(0, i - ctx), i + ctx + 1).some((x) => x.kind !== "same"));
  const out: (DiffRow | { kind: "fold"; n: number })[] = [];
  for (let i = 0; i < rows.length; i++) {
    if (keep[i]) { out.push(rows[i]); continue; }
    const last = out[out.length - 1];
    if (last && last.kind === "fold") last.n++; else out.push({ kind: "fold", n: 1 });
  }
  return { rows: out, added, removed };
}

const DOW = ["Chủ nhật", "Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7"];
const hm = (h: string, m: string) => `${h.padStart(2, "0")}:${m.padStart(2, "0")}`;
/** 5-field cron -> short Vietnamese text for the common shapes; null when it is something else (the raw cron is shown) */
export function cronToVi(cron: string): string | null {
  const f = cron.trim().split(/\s+/); if (f.length !== 5) return null;
  const [mi, h, dom, mon, dow] = f;
  const num = (x: string) => /^\d{1,2}$/.test(x);
  if (mi.startsWith("*/") && h === "*" && dom === "*" && mon === "*" && dow === "*") return `Mỗi ${mi.slice(2)} phút`;
  if (num(mi) && h.startsWith("*/") && dom === "*" && mon === "*" && dow === "*") return `Mỗi ${h.slice(2)} giờ (phút ${mi})`;
  if (!num(mi) || !num(h) || mon !== "*") return null;
  const t = hm(h, mi);
  if (dom === "*" && dow === "*") return `Hằng ngày lúc ${t}`;
  if (dom === "*" && dow === "1-5") return `Thứ 2 đến Thứ 6 lúc ${t}`;
  if (dom === "*" && /^[0-7](,[0-7])*$/.test(dow)) return `${dow.split(",").map((d) => DOW[Number(d) % 7]).join(", ")} hằng tuần lúc ${t}`;
  if (num(dom) && dow === "*") return `Ngày ${Number(dom)} hằng tháng lúc ${t}`;
  return null;
}

export type Preset = "daily" | "weekdays" | "weekly" | "monthly" | "custom";
/** preset + "HH:MM" (+ weekday / day of month) -> cron */
export function presetCron(p: Preset, time: string, opt: { dow?: number; dom?: number; custom?: string } = {}): string {
  const [h, m] = (/^(\d{1,2}):(\d{2})$/.exec(time) ?? ["", "8", "0"]).slice(1).map(Number);
  switch (p) {
    case "daily": return `${m} ${h} * * *`;
    case "weekdays": return `${m} ${h} * * 1-5`;
    case "weekly": return `${m} ${h} * * ${opt.dow ?? 1}`;
    case "monthly": return `${m} ${h} ${opt.dom ?? 1} * *`;
    default: return (opt.custom ?? "").trim();
  }
}

/** ISO -> "dd/mm/yyyy HH:MM" in the user's zone (Asia/Ho_Chi_Minh, same offset as Bangkok) */
export const viDateTime = (iso: string | null | undefined, tz = "Asia/Ho_Chi_Minh") => {
  if (!iso) return ""; const d = new Date(iso); if (Number.isNaN(+d)) return "";
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-GB", { timeZone: tz, day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })
    .formatToParts(d).map((x) => [x.type, x.value]));
  return `${p.day}/${p.month}/${p.year} ${p.hour}:${p.minute}`;
};

/** file name for "Tải về" */
export function canvasFileName(title: string, kind: string, language: string): string {
  const base = (title || "canvas").normalize("NFD").replace(/[\u0300-\u036f]/g, "").replace(/đ/g, "d").replace(/Đ/g, "D")
    .replace(/[^\w.-]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 60) || "canvas";
  const EXT: Record<string, string> = { python: "py", py: "py", javascript: "js", typescript: "ts", tsx: "tsx", sql: "sql", html: "html", css: "css", json: "json", bash: "sh", yaml: "yml" };
  return `${base}.${kind === "code" ? EXT[language.toLowerCase()] ?? "txt" : "md"}`;
}
