// G4: image attachments uploaded to Agent OS itself (/api/files, self-hosted, no third-party upload service) and sent as
// LangChain STANDARD content blocks {type:"image", url, mime_type} (langchain-core 1.x). Limits mirror the server's
// (GET /api/files/config); the server re-validates type (magic bytes) and size anyway.
import type { AttachmentAdapter, CompleteAttachment, PendingAttachment } from "@assistant-ui/react";

export const IMAGE_TYPES = ["image/png", "image/jpeg", "image/webp", "image/gif"];
// GD6.8: documents (pdf, docx, xlsx, pptx, csv, txt, md, json) share the same upload route; the server converts them to text (markitdown).
export const DOC_EXTS = ["pdf", "docx", "xlsx", "pptx", "csv", "txt", "md", "json"] as const;
export type DocKind = (typeof DOC_EXTS)[number];
export const DOC_ACCEPT = DOC_EXTS.map((e) => "." + e).join(",");
export const limits = { maxBytes: 5 * 1024 * 1024, maxDocBytes: 20 * 1024 * 1024, maxPerMessage: 5 };
export type DocMeta = { kind: DocKind; size: number; pages?: number | null; units?: string | null; status?: "ok" | "error"; reason?: string | null };
/** Per-attachment info the chip shows (size, page/sheet/slide count, error); filled on add() and refreshed from the upload answer. */
export const docMeta = new Map<string, DocMeta>();
export const docKindOf = (name: string): DocKind | null => {
  const e = name.includes(".") ? name.split(".").pop()!.toLowerCase() : "";
  return (DOC_EXTS as readonly string[]).includes(e) ? (e as DocKind) : null;
};
export const formatSize = (n: number) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : n >= 1024 ? `${Math.round(n / 1024)} KB` : `${n} B`);
export const unitLabel = (u?: string | null, n?: number | null) => (n ? `${n} ${u === "sheet" ? "sheet" : u === "slide" ? "slide" : "trang"}` : "");
export const loadLimits = () =>
  fetch("/api/files/config").then((r) => (r.ok ? r.json() : null)).then((c) => { if (c) { limits.maxBytes = c.max_bytes; limits.maxPerMessage = c.max_per_message; if (c.max_doc_bytes) limits.maxDocBytes = c.max_doc_bytes; } }).catch(() => {});

export class AttachmentLimitError extends Error {
  constructor(msg: string) { super(msg); window.dispatchEvent(new CustomEvent("aos-attachment-error", { detail: msg })); }
}

export class AgentOSImageAttachmentAdapter implements AttachmentAdapter {
  accept = IMAGE_TYPES.join(",");
  async add({ file }: { file: File }): Promise<PendingAttachment> {
    if (!IMAGE_TYPES.includes(file.type)) throw new AttachmentLimitError(`Unsupported image type ${file.type || "?"} (PNG, JPEG, WebP, GIF)`);
    if (file.size > limits.maxBytes) throw new AttachmentLimitError(`Image too large: ${(file.size / 1048576).toFixed(1)} MB > ${(limits.maxBytes / 1048576).toFixed(0)} MB`);
    return { id: crypto.randomUUID(), type: "image", name: file.name, contentType: file.type, file, status: { type: "requires-action", reason: "composer-send" } };
  }
  async send(a: PendingAttachment): Promise<CompleteAttachment> {
    const fd = new FormData(); fd.append("file", a.file, a.name);
    const r = await fetch("/api/files", { method: "POST", body: fd });
    if (r.status === 429) throw new AttachmentLimitError(`Gửi ảnh quá nhanh, thử lại sau ${r.headers.get("Retry-After") ?? "60"} giây`);   // GĐ4 rate limit
    if (!r.ok) throw new AttachmentLimitError(`Upload refused: HTTP ${r.status} ${(await r.text()).slice(0, 160)}`);
    const f = (await r.json()) as { url: string; mime_type: string };
    return { ...a, status: { type: "complete" }, content: [{ type: "image", image: f.url }] };
  }
  async remove() { /* stored content-addressed on the server; nothing to undo client-side */ }
}

/** Document attachments: uploaded to /api/files, then sent as a STANDARD file block {type:"file", url}; the server converts + inlines/indexes it. */
export class AgentOSDocumentAttachmentAdapter implements AttachmentAdapter {
  accept = DOC_ACCEPT;
  async add({ file }: { file: File }): Promise<PendingAttachment> {
    const kind = docKindOf(file.name);
    if (!kind) throw new AttachmentLimitError(`Unsupported file type: ${file.name} (PDF, Word, Excel, PowerPoint, CSV, TXT, MD, JSON)`);
    if (file.size > limits.maxDocBytes) throw new AttachmentLimitError(`File too large: ${formatSize(file.size)} > ${formatSize(limits.maxDocBytes)}`);
    const id = crypto.randomUUID(); docMeta.set(id, { kind, size: file.size });
    return { id, type: "document", name: file.name, contentType: file.type || "application/octet-stream", file, status: { type: "requires-action", reason: "composer-send" } };
  }
  async send(a: PendingAttachment): Promise<CompleteAttachment> {
    const fd = new FormData(); fd.append("file", a.file, a.name);
    const r = await fetch("/api/files", { method: "POST", body: fd });
    const fail = (m: string) => { const d = docMeta.get(a.id); if (d) docMeta.set(a.id, { ...d, status: "error", reason: m }); return new AttachmentLimitError(m); };
    if (r.status === 429) throw fail(`Gửi tệp quá nhanh, thử lại sau ${r.headers.get("Retry-After") ?? "60"} giây`);
    if (!r.ok) throw fail(`Upload refused: HTTP ${r.status} ${(await r.text()).slice(0, 160)}`);
    const f = (await r.json()) as { url: string; mime_type: string; status?: "ok" | "error"; reason?: string; pages?: number | null; units?: string | null };
    const d = docMeta.get(a.id); if (d) docMeta.set(a.id, { ...d, pages: f.pages, units: f.units, status: f.status, reason: f.reason });
    if (f.status === "error") throw fail(`Không đọc được tệp: ${f.reason ?? "?"}`);
    return { ...a, status: { type: "complete" }, content: [{ type: "file", data: f.url, mimeType: f.mime_type, filename: a.name }] };
  }
  async remove(a: { id: string }) { docMeta.delete(a.id); }
}

type Block = Record<string, unknown> & { type?: string };
/** assistant-ui's LangChain converter emits legacy {type:"image_url"} blocks -> standard {type:"image", url} blocks. */
export function toStandardBlocks<T extends { content?: unknown }>(m: T): T {
  if (!Array.isArray(m.content)) return m;
  const content = (m.content as Block[]).map((b) => {
    if (b?.type === "file") {   // assistant-ui file part / history block that points at /api/files/<id> -> standard file block
      const url = [b.url, b.data, b.file_data].find((v) => typeof v === "string" && v.startsWith("/api/files/")) as string | undefined;
      const meta = b.metadata as { filename?: string } | undefined;
      return url ? { type: "file", url, mime_type: b.mime_type ?? b.mimeType, ...(b.filename || meta?.filename ? { filename: b.filename ?? meta?.filename } : {}) } : b;
    }
    if (b?.type !== "image_url") return b;
    const iu = b.image_url as string | { url?: string } | undefined; const url = typeof iu === "string" ? iu : iu?.url;
    return url ? { type: "image", url } : b;
  }).filter((b) => !(b?.type === "text" && typeof b.text === "string" && !b.text.trim()));
  return { ...m, content };
}
