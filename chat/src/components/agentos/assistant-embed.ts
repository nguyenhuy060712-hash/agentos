// GĐ7.1 pure helpers for the Agent OS assistant side panel (/chat/?embed=assistant&ctx=<json>); no browser globals, unit-tested in node.

export const CTX_KEYS = ["page", "url", "project_id", "trace_id", "thread_id", "dataset_id", "experiment_id"] as const;
export type PageCtx = Partial<Record<(typeof CTX_KEYS)[number], string>>;

/** ?embed=assistant -> panel mode (anything else is the normal chat) */
export const isAssistantEmbed = (search: string) => new URLSearchParams(search).get("embed") === "assistant";

/** Page context from the parent Agent OS page: only the known keys, as short single-line strings (the server sanitises again). */
export function parseCtx(raw: unknown): PageCtx {
  let o: unknown = raw;
  if (typeof raw === "string") { try { o = JSON.parse(raw); } catch { return {}; } }
  if (!o || typeof o !== "object" || Array.isArray(o)) return {};
  const out: PageCtx = {};
  for (const k of CTX_KEYS) {
    const v = (o as Record<string, unknown>)[k];
    if (v == null || v === "") continue;
    if (typeof v !== "string" && typeof v !== "number") continue;
    const s = String(v).replace(/[\r\n\t]+/g, " ").trim().slice(0, 300);
    if (s) out[k] = s;
  }
  return out;
}

export const ctxFromSearch = (search: string): PageCtx => parseCtx(new URLSearchParams(search).get("ctx"));

/** Run input addition: `{context}` is sent with every message (empty -> nothing). */
export const ctxInput = (c: PageCtx): Record<string, unknown> => (Object.keys(c).length ? { context: c } : {});

/** postMessage from the parent page ({type:"aos-ctx", ctx}) is accepted only from the same origin. */
export function ctxFromMessage(ev: { origin: string; data: unknown }, origin: string): PageCtx | null {
  const d = ev.data as { type?: string; ctx?: unknown } | null;
  if (ev.origin !== origin || !d || d.type !== "aos-ctx") return null;
  return parseCtx(d.ctx);
}

/** One-line description for the small "viewing" chip under the header. */
export const ctxLabel = (c: PageCtx): string => {
  const ids = (["trace_id", "project_id", "thread_id", "dataset_id", "experiment_id"] as const).filter((k) => c[k]).map((k) => `${k.replace("_id", "")} ${String(c[k]).slice(0, 8)}`);
  return [c.page, ...ids].filter(Boolean).join(" · ");
};
