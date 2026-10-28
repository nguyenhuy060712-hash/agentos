// GĐ8.6 pure helpers (no browser/API imports: unit-tested in node) for files the agent creates in the sandbox (/workspace/out).
// The code tool text carries one line `[aos-files] {"files":[{id,name,size,mime_type}]}` (agentos-local chat_gd5b.files_line).

export type OutFile = { id: string; name: string; size: number; mime_type: string };
const LINE = /^\[aos-files\] (\{.*\})$/m;

/** tool result text -> files (empty when none / malformed) */
export function parseFiles(result: unknown): OutFile[] {
  const m = typeof result === "string" ? LINE.exec(result) : null;
  if (!m) return [];
  try {
    const d = JSON.parse(m[1]) as { files?: unknown };
    return (Array.isArray(d.files) ? d.files : []).filter((f): f is OutFile =>
      !!f && typeof f === "object" && typeof (f as OutFile).id === "string" && /^[0-9a-f-]{36}$/.test((f as OutFile).id) && typeof (f as OutFile).name === "string")
      .map((f) => ({ id: f.id, name: f.name, size: Number(f.size) || 0, mime_type: String(f.mime_type || "") }));
  } catch { return []; }
}

/** the marker lines are for the cards, not for the plain output view */
export const stripFileLines = (s: string) => s.replace(/\n?\[aos-files\] [^\n]*/g, "");

type Msg = { content?: readonly unknown[] | string };
/** every file created in a thread (tool-call results of all messages), oldest first, each id once */
export function threadFiles(messages: readonly Msg[]): OutFile[] {
  const seen = new Set<string>(); const out: OutFile[] = [];
  for (const m of messages) {
    if (!Array.isArray(m.content)) continue;
    for (const p of m.content as { type?: string; result?: unknown }[]) {
      if (p?.type !== "tool-call") continue;
      for (const f of parseFiles(p.result)) if (!seen.has(f.id)) { seen.add(f.id); out.push(f); }
    }
  }
  return out;
}

/** 1536 -> "1,5 KB" (vi) / "1.5 KB" (en) */
export function fmtSize(n: number, lang: "vi" | "en" = "vi"): string {
  const u = ["B", "KB", "MB", "GB"]; let i = 0; let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return `${v.toLocaleString(lang === "vi" ? "vi-VN" : "en-US", { maximumFractionDigits: i ? 1 : 0 })} ${u[i]}`;
}

export type FileKind = "pdf" | "image" | "word" | "excel" | "slides" | "data" | "other";
export function fileKind(f: Pick<OutFile, "name" | "mime_type">): FileKind {
  const m = f.mime_type, ext = f.name.toLowerCase().split(".").pop() ?? "";
  if (m === "application/pdf") return "pdf";
  if (m.startsWith("image/")) return "image";
  if (m.includes("wordprocessingml") || ext === "docx") return "word";
  if (m.includes("spreadsheetml") || ext === "xlsx") return "excel";
  if (m.includes("presentationml") || ext === "pptx") return "slides";
  if (["csv", "json", "md", "txt"].includes(ext)) return "data";
  return "other";
}
export const canPreview = (f: OutFile) => ["pdf", "image"].includes(fileKind(f));
export const fileUrl = (f: OutFile, inline = false) => `/api/files/${f.id}${inline && fileKind(f) === "pdf" ? "?inline=1" : ""}`;
export const baseName = (name: string) => name.split("/").pop() || name;

export type TreeNode = { name: string; path: string; file?: OutFile; children: TreeNode[] };
/** "bao-cao/q3.docx" paths -> folder tree (folders first, then files, by name) */
export function fileTree(files: OutFile[]): TreeNode[] {
  const root: TreeNode = { name: "", path: "", children: [] };
  for (const f of files) {
    const parts = f.name.split("/").filter(Boolean); let cur = root;
    parts.forEach((part, i) => {
      const path = parts.slice(0, i + 1).join("/");
      if (i === parts.length - 1) { cur.children.push({ name: part, path, file: f, children: [] }); return; }
      let next = cur.children.find((c) => !c.file && c.name === part);
      if (!next) { next = { name: part, path, children: [] }; cur.children.push(next); }
      cur = next;
    });
  }
  const sort = (n: TreeNode[]) => { n.sort((a, b) => Number(!!a.file) - Number(!!b.file) || a.name.localeCompare(b.name, "vi")); n.forEach((c) => sort(c.children)); return n; };
  return sort(root.children);
}
