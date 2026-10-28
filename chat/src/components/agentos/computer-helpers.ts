// GĐ11.52 pure helpers of the "Computer" panel (status chip, folder paths, sizes, line diff of write_file / edit_file). No React, no fetch: unit-tested.
export const ROOT = "/workspace";
export type BoxLite = { status: string; last_error?: string | null; disk_warn?: boolean };
export type Chip = { key: "running" | "paused" | "error" | "starting" | "warn"; tone: "ok" | "muted" | "bad" | "warn" };

/** Running / Paused (wakes when needed) / Error (New computer) / aos-boot or disk warning. */
export function statusChip(b: BoxLite): Chip {
  if (b.status === "failed") return { key: "error", tone: "bad" };
  if (b.status === "stopped") return { key: "paused", tone: "muted" };
  if (b.status === "provisioning") return { key: "starting", tone: "muted" };
  if (b.status === "running" && (b.last_error || b.disk_warn)) return { key: "warn", tone: "warn" };
  return { key: "running", tone: "ok" };
}

/** "/workspace/a/b" -> [{name:"workspace",path:"/workspace"},{name:"a",...},...] */
export function crumbs(path: string): { name: string; path: string }[] {
  const parts = path.split("/").filter(Boolean); const out: { name: string; path: string }[] = [];
  parts.forEach((p, i) => out.push({ name: p, path: "/" + parts.slice(0, i + 1).join("/") }));
  return out;
}
export const parentOf = (p: string) => (p === ROOT || !p.startsWith(ROOT + "/") ? ROOT : p.slice(0, p.lastIndexOf("/")) || ROOT);
export const inside = (p: string) => p === ROOT || p.startsWith(ROOT + "/");
/** target of a rename/move: a bare name stays in the same folder, "/workspace/x" or "x/y" relative to /workspace is taken as typed; leaving /workspace -> null */
export function moveTarget(from: string, typed: string): string | null {
  const t = typed.trim(); if (!t) return null;
  const abs = t.startsWith("/") ? t : t.includes("/") ? `${ROOT}/${t}` : `${parentOf(from)}/${t}`;
  const parts: string[] = []; for (const s of abs.split("/")) { if (!s || s === ".") continue; if (s === "..") parts.pop(); else parts.push(s); }
  const n = "/" + parts.join("/"); return inside(n) && n !== ROOT ? n : null;
}

export type Entry = { path: string; name: string; type: string; size: number; mtime?: number };
/** folders first, then A-Z; optional name filter */
export function sortEntries(items: Entry[], q = ""): Entry[] {
  const f = q.trim().toLowerCase();
  return items.filter((e) => e.name !== ".aos" && (!f || e.name.toLowerCase().includes(f)))   // .aos = the box's own bookkeeping folder
    .sort((a, b) => (a.type === "dir" ? 0 : 1) - (b.type === "dir" ? 0 : 1) || a.name.localeCompare(b.name));
}
export const fmtSize = (n: number) => (n >= 1 << 20 ? `${(n / (1 << 20)).toFixed(1)} MB` : n >= 1024 ? `${Math.round(n / 1024)} KB` : `${n} B`);
export const fmtMs = (ms: number | null | undefined) => (ms == null ? "" : ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${ms} ms`);
export const TEXT_EXT = /\.(txt|md|json|csv|tsv|py|js|ts|tsx|jsx|sh|yaml|yml|toml|ini|log|html|css|xml|sql|env|cfg)$/i;
export const isTextName = (n: string) => TEXT_EXT.test(n);

export type DiffLine = { op: "+" | "-" | " "; text: string };
const MAX_DIFF = 400;
/** line diff (LCS) between two texts, trimmed to MAX_DIFF lines; used for write_file (old = "") and edit_file (old_string -> new_string) cards */
export function lineDiff(a: string, b: string): { lines: DiffLine[]; added: number; removed: number; cut: boolean } {
  const x = a === "" ? [] : a.split("\n"), y = b === "" ? [] : b.split("\n");
  let s = 0; while (s < x.length && s < y.length && x[s] === y[s]) s++;
  let e = 0; while (e < x.length - s && e < y.length - s && x[x.length - 1 - e] === y[y.length - 1 - e]) e++;
  const m = x.slice(s, x.length - e), n = y.slice(s, y.length - e); const lines: DiffLine[] = [];
  if (m.length * n.length > 250_000) { m.forEach((t) => lines.push({ op: "-", text: t })); n.forEach((t) => lines.push({ op: "+", text: t })); }
  else {
    const L = Array.from({ length: m.length + 1 }, () => new Array<number>(n.length + 1).fill(0));
    for (let i = m.length - 1; i >= 0; i--) for (let j = n.length - 1; j >= 0; j--) L[i][j] = m[i] === n[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
    let i = 0, j = 0;
    while (i < m.length || j < n.length) {
      if (i < m.length && j < n.length && m[i] === n[j]) { lines.push({ op: " ", text: m[i] }); i++; j++; }
      else if (i < m.length && (j === n.length || L[i + 1][j] >= L[i][j + 1])) lines.push({ op: "-", text: m[i++] });
      else lines.push({ op: "+", text: n[j++] });
    }
  }
  const CTX = 2;   // a little unchanged context around the change
  lines.unshift(...x.slice(Math.max(0, s - CTX), s).map((text): DiffLine => ({ op: " ", text })));
  lines.push(...x.slice(x.length - e, x.length - e + CTX).map((text): DiffLine => ({ op: " ", text })));
  const added = lines.filter((l) => l.op === "+").length, removed = lines.filter((l) => l.op === "-").length;
  return { lines: lines.slice(0, MAX_DIFF), added, removed, cut: lines.length > MAX_DIFF };
}

/** F-4 files-panel counter: output files of the thread + the top-level items of the computer's /workspace (the `out` folder is already counted as output files) */
export function totalFiles(outCount: number, root: { name: string; type: string }[]) {
  return outCount + root.filter((e) => !(e.type === "dir" && e.name === "out")).length;
}
