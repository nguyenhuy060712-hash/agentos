// Helpers shared by the /dao-tao/ pages (module pages, decks, handbook, instructor kit, search).
import type { Role, Track } from "../data/training/types";

export const pad = (n: number) => String(n).padStart(2, "0");
export const norm = (s: string) => s.normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/đ/g, "d").replace(/Đ/g, "D").toLowerCase();
/** Wrap every <table> in <div class="tbl"> so wide tables scroll sideways instead of overflowing. */
export const wrapTables = (h: string) => h.replace(/<table\b[\s\S]*?<\/table>/g, (m) => `<div class="tbl">${m}</div>`);
const ent: Record<string, string> = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&nbsp;": " " };
/** Plain text of trusted HTML (for search snippets). */
export const strip = (h: string) => h.replace(/<(pre|table)\b[\s\S]*?<\/\1>/g, " ").replace(/<[^>]+>/g, " ").replace(/&(amp|lt|gt|quot|#39|nbsp);/g, (m) => ent[m]).replace(/\s+/g, " ").trim();
export const clip = (s: string, n = 200) => (s.length <= n ? s : s.slice(0, n - 1).replace(/\s+\S*$/, "") + "…");
/** Running clock "h:mm" for lesson-plan minutes. */
export const clock = (min: number) => `${Math.floor(min / 60)}:${pad(min % 60)}`;

export const trackCls: Record<Track, string> = {
  "nen-tang": "bg-emerald-100 text-emerald-800 dark:bg-emerald-500/15 dark:text-emerald-300",
  "van-hanh": "bg-sky-100 text-sky-800 dark:bg-sky-500/15 dark:text-sky-300",
  "it": "bg-violet-100 text-violet-800 dark:bg-violet-500/15 dark:text-violet-300",
  "kien-truc": "bg-rose-100 text-rose-800 dark:bg-rose-500/15 dark:text-rose-300",
};
export const trackOrder: Track[] = ["nen-tang", "van-hanh", "it", "kien-truc"];
export const roleOrder: Role[] = ["user", "admin", "it", "architect"];
export const roleShort: Record<Role, string> = { user: "Người dùng", admin: "Quản trị", it: "IT", architect: "Kiến trúc" };
