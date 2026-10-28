// GĐ5a pure helpers (no browser/API imports: unit-tested in node).
export type AskField = { name: string; label: string; kind: "text" | "number" | "choice" | "multi" | "toggle" | "date"; options: string[]; required: boolean; placeholder: string };
export type AskValue = { type: "ask_user"; tool_call_id: string; title: string; note?: string; fields: AskField[] };

export const isAsk = (v: unknown): v is AskValue => !!v && typeof v === "object" && (v as { type?: string }).type === "ask_user";

/** "Người dùng đã trả lời: {...}" -> pairs; other tool texts (declined, invalid) -> null */
export function parseAnswered(result: unknown): [string, string][] | null {
  const s = typeof result === "string" ? result : "";
  const i = s.indexOf("{"); if (!s.startsWith("Người dùng đã trả lời") || i < 0) return null;
  try {
    const o = JSON.parse(s.slice(i)) as Record<string, unknown>;
    return Object.entries(o).map(([k, v]) => [k, Array.isArray(v) ? v.join(", ") : typeof v === "boolean" ? (v ? "✓" : "✕") : isoToVi(String(v))]);
  } catch { return null; }
}

/** memory key from the `luu_ghi_nho` result "Đã lưu. [mem:<key>]" */
export const memKey = (result: unknown) => (typeof result === "string" ? /\[mem:([\w-]{4,64})\]/.exec(result)?.[1] : undefined);

/** GĐ5b: dates are typed and shown as dd/mm/yyyy (Vietnamese), sent as ISO yyyy-mm-dd */
export const isoToVi = (s: string) => { const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s); return m ? `${m[3]}/${m[2]}/${m[1]}` : s; };
/** "1/11/2026", "01-11-2026", "01.11.2026" -> "2026-11-01"; null if not a real calendar date */
export function viToIso(s: string): string | null {
  const m = /^\s*(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\s*$/.exec(s); if (!m) return null;
  const d = +m[1], mo = +m[2], y = +m[3]; const dt = new Date(Date.UTC(y, mo - 1, d));
  if (dt.getUTCFullYear() !== y || dt.getUTCMonth() !== mo - 1 || dt.getUTCDate() !== d) return null;
  return `${y}-${String(mo).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}
/** typing helper: digits only, slashes inserted after dd and mm */
export const maskViDate = (s: string) => { const d = s.replace(/\D/g, "").slice(0, 8); return [d.slice(0, 2), d.slice(2, 4), d.slice(4)].filter(Boolean).join("/"); };
