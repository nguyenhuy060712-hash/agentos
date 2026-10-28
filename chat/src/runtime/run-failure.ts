// Pure helpers for RunFailureBanner (no browser globals, so they are unit-tested in node).

/** GĐ8.P: machine-readable 402 detail from the server (gateway.cap_detail); `message` is the server's own sentence (old clients, logs) */
export type CapInfo = { code: "spend_cap"; message?: string; scope?: string; target?: string; period?: string; limit_usd?: number; used_usd?: number; feature?: string | null };
/** GĐ3.7: a run the server rejected before any event (4xx, or 5xx after the SDK's own retries). react-langgraph rolls the
 *  user turn back in that case, so the text is kept here and RunFailureBanner offers "Thử lại" (re-sends it). */
export type RunFailure = { text: string; error: string; at: number; kind?: "cap"; cap?: CapInfo } | null;   // kind "cap": HTTP 402, a spend cap was reached

const parse = (m: string): { status: string; detail: unknown } | null => {
  const x = /HTTP (\d{3})[^{]*(\{.*\})?/s.exec(m);
  if (!x) return null;
  let detail: unknown = undefined; try { detail = (JSON.parse(x[2] ?? "{}") as { detail?: unknown }).detail; } catch { /* not JSON */ }
  return { status: x[1], detail };
};
const isCap = (d: unknown): d is CapInfo => !!d && typeof d === "object" && (d as { code?: unknown }).code === "spend_cap";

/** "HTTP 403: {"detail":"…"}" -> "403 · …" (the server's own detail, no stack; an object detail -> its message) */
export const errText = (m: string) => {
  const x = parse(m);
  if (!x) return m.slice(0, 200);
  const d = typeof x.detail === "string" ? x.detail : x.detail && typeof x.detail === "object" ? String((x.detail as { message?: unknown }).message ?? "") : "";
  return (d ? `${x.status} · ${d}` : `HTTP ${x.status}`).slice(0, 200);
};

/** 402 = a spend cap (provider cap, agent daily budget, demo daily cap): retrying cannot help, so the banner says so instead */
export const failureOf = (text: string, m: string): NonNullable<RunFailure> => {
  const error = errText(m); const d = parse(m)?.detail;
  return { text, error, at: Date.now(), ...(error.startsWith("402") ? { kind: "cap" as const } : {}), ...(isCap(d) ? { cap: d } : {}) };
};

type T = (k: string, o?: Record<string, string>) => string;
const PROVIDER: Record<string, string> = { gemini: "Gemini", openai: "OpenAI", anthropic: "Anthropic" };
const usd = (n: number) => n.toFixed(2);

/** GĐ8.P: the banner sentence from the 402 fields, in the UI language (null when the fields are missing -> old text parsing) */
export function capSentence(cap: CapInfo | undefined, t: T): string | null {
  if (!cap || typeof cap.limit_usd !== "number" || typeof cap.used_usd !== "number") return null;
  const tg = String(cap.target ?? "");
  const name = cap.scope === "feature" ? t(`capFeature_${tg}`, { defaultValue: tg }) : cap.scope === "provider" ? (PROVIDER[tg] ?? tg) : tg;
  const who = cap.scope ? t(`capScope_${cap.scope}`, { name, defaultValue: name || cap.scope }) : "";
  const period = cap.period ? t(`capPeriod_${cap.period}`, { defaultValue: "" }) : "";
  const used = t("capUsedP", { used: usd(cap.used_usd), limit: usd(cap.limit_usd), period }).replace(/\s+\./, ".");
  return `${who ? who + ": " : ""}${used} ${t("capAskAdmin")}`;
}

/** GĐ7.Z fallback (server without the 402 fields): the Vietnamese text starts with the same words as the title -> drop the repeat; in English show only the amounts. */
export function capDetail(error: string, lang: string | undefined, t: T, cap?: CapInfo): string {
  const s = capSentence(cap, t);
  if (s) return s;
  const d = error.replace(/^402 · /, "").replace(/^Đã chạm trần chi phí\s*/, "");
  if (!lang?.startsWith("en")) return d;
  const m = d.match(/\$([\d.]+) \/ trần \$([\d.]+)/);
  return (m ? t("capUsed", { used: m[1], limit: m[2] }) + " " : "") + t("capAskAdmin");
}
