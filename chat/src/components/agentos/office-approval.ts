// GĐ11.62: approval card of the Google / Microsoft 365 office tools (gmail_*, gcal_*, outlook_*, ocal_*). The server builds the card text (workspace_tools.describe_call):
// "Label: value" lines with the REAL recipients / guests (F-7 look-up) or a "CẢNH BÁO" line when it could not look them up. Here that text becomes labelled rows
// (To / Cc / Bcc / subject / preview / attachments / attendees). Without the text, the same rows are built from the arguments. Pure functions (unit-tested).
import type { Lang, Row } from "./approval-format";

export const isOfficeTool = (name: string) => /^(gmail|gcal|outlook|ocal)_/.test(name);

const L: Record<string, { vi: string; en: string }> = {
  to: { vi: "Đến", en: "To" }, cc: { vi: "Cc", en: "Cc" }, bcc: { vi: "Bcc", en: "Bcc" }, subject: { vi: "Tiêu đề", en: "Subject" },
  text: { vi: "Nội dung (xem trước)", en: "Preview" }, attachments: { vi: "Tệp đính kèm", en: "Attachments" }, attendees: { vi: "Người tham dự (sẽ nhận thư mời)", en: "Attendees (will get an invitation)" },
  guests: { vi: "Khách hiện có", en: "Current guests" }, event: { vi: "Sự kiện", en: "Event" }, start: { vi: "Bắt đầu", en: "Start" }, end: { vi: "Kết thúc", en: "End" },
  organizer: { vi: "Người tổ chức", en: "Organizer" }, draft: { vi: "Bản nháp", en: "Draft" }, original: { vi: "Thư gốc", en: "Original message" }, answer: { vi: "Trả lời", en: "Answer" },
  where: { vi: "Địa điểm", en: "Where" }, warn: { vi: "Cảnh báo", en: "Warning" }, willsend: { vi: "Sẽ gửi tới (đã tra cứu)", en: "Will be sent to (looked up)" },
};
const RANK = ["warn", "willsend", "to", "cc", "bcc", "subject", "event", "start", "end", "organizer", "attendees", "guests", "draft", "original", "answer", "where", "text", "attachments"];
// English label printed by the server -> key above
const SERVER: [RegExp, string][] = [
  [/^will be sent to$/i, "willsend"], [/^to$/i, "to"], [/^cc$/i, "cc"], [/^bcc$/i, "bcc"], [/^subject$/i, "subject"], [/^text$/i, "text"], [/^attachments$/i, "attachments"], [/^event$/i, "event"],
  [/^start$/i, "start"], [/^end$/i, "end"], [/^organizer$/i, "organizer"], [/^draft$/i, "draft"], [/^original message$/i, "original"], [/^answer$/i, "answer"], [/^where$/i, "where"],
  [/^attendees/i, "attendees"], [/^these people will receive/i, "attendees"], [/^guests on the event/i, "guests"],
];
const TITLE: Record<string, { vi: string; en: string }> = {
  send: { vi: "Gửi thư", en: "Send an email" }, send_draft: { vi: "Gửi bản nháp", en: "Send a draft" }, reply: { vi: "Trả lời thư", en: "Reply to an email" },
  reply_all: { vi: "Trả lời tất cả", en: "Reply to all" }, forward: { vi: "Chuyển tiếp thư", en: "Forward an email" }, create: { vi: "Tạo sự kiện", en: "Create an event" },
  update: { vi: "Sửa sự kiện", en: "Update an event" }, delete: { vi: "Xoá sự kiện", en: "Delete an event" }, cancel: { vi: "Huỷ sự kiện", en: "Cancel an event" },
  respond: { vi: "Trả lời lời mời", en: "Answer an invitation" }, trash: { vi: "Chuyển thư vào thùng rác", en: "Move an email to the trash" },
};
export function officeTitle(name: string, lang: Lang): string {
  const m = /^(gmail|gcal|outlook|ocal)_(.+)$/.exec(name);
  const t = m && TITLE[m[2]];
  return t ? t[lang] : name;
}

/** Names in address headers can arrive as RFC 2047 words (=?utf-8?b?...?=): show them readable. Anything that does not decode stays as it is. */
export function decodeMime(s: string): string {
  const word = (m: string, cs: string, enc: string, body: string) => {
    try {
      const raw = enc.toLowerCase() === "b" ? atob(body) : body.replace(/_/g, " ").replace(/=([0-9a-f]{2})/gi, (_x, h: string) => String.fromCharCode(parseInt(h, 16)));
      return new TextDecoder(cs).decode(Uint8Array.from(raw, (c) => c.charCodeAt(0)));
    } catch { return m; }
  };
  return s.replace(/(=\?[\w-]+\?[bq]\?[^?\s]*\?=)\s+(?==\?[\w-]+\?[bq]\?)/gi, "$1").replace(/=\?([\w-]+)\?([bq])\?([^?\s]*)\?=/gi, word);
}
const arr = (v: unknown) => (Array.isArray(v) ? v.map(String).join(", ") : String(v));
const clip = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

type KRow = Row & { key: string };
/** Rows of one office approval. `description` is the server card text (first line = headline, skipped). Who gets it comes first, the text and attachments last. */
export function officeRows(args: Record<string, unknown> = {}, description: string | undefined, lang: Lang): Row[] {
  const rows: KRow[] = [];
  const add = (k: string, v: string) => { const t = decodeMime(v).trim(); if (t) rows.push({ key: k, label: L[k][lang], value: clip(t, k === "text" ? 400 : 300) }); };
  if (description) {
    const lines = description.split("\n").map((x) => x.trim()).filter(Boolean);
    for (const line of lines.slice(1)) {
      if (/^CẢNH BÁO/i.test(line)) { add("warn", line); continue; }
      const i = line.indexOf(": ");
      if (i < 0) { rows.push({ key: "", label: "", value: clip(line, 300) }); continue; }
      const head = line.slice(0, i), key = SERVER.find(([re]) => re.test(head))?.[1];
      if (key) add(key, line.slice(i + 2)); else rows.push({ key: "", label: head, value: clip(decodeMime(line.slice(i + 2)), 300) });
    }
  }
  if (!rows.length) {
    for (const [k, a] of [["to", "to"], ["cc", "cc"], ["bcc", "bcc"], ["subject", "subject"], ["event", "summary"], ["start", "start"], ["end", "end"], ["attendees", "attendees"]] as const)
      if (args[a]) add(k, arr(args[a]));
    const body = args.body ?? args.text ?? args.description;
    if (body) add("text", String(body).replace(/\s+/g, " "));
    if (Array.isArray(args.attachments) && args.attachments.length) add("attachments", `${args.attachments.length} (${args.attachments.map(String).join(", ")})`);
  }
  if (rows.some((r) => r.key === "willsend")) for (let i = rows.length - 1; i >= 0; i--) if (rows[i].key === "to" && /^(the sender of|everyone on)/i.test(rows[i].value)) rows.splice(i, 1);   // "To: the sender of message m-1" says less than the looked-up address
  const rank = (r: KRow) => { const i = RANK.indexOf(r.key); return i < 0 ? 15 : i; };
  return rows.map((r, i) => ({ r, i })).sort((a, b) => rank(a.r) - rank(b.r) || a.i - b.i).map(({ r }) => ({ label: r.label, value: r.value }));
}
