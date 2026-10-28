// GĐ3 leftover (accept case 9): HITL approval cards must read clearly. Maps the Phòng AI số tool calls that need approval to a
// human label, a risk note and formatted argument rows (VND, channel names, long text clipped). Unknown tools fall back to the raw
// argument names, so nothing is ever hidden from the approver. Pure functions (unit-tested).
export type Lang = "vi" | "en";
type ArgSpec = { vi: string; en: string; kind?: "vnd" | "text" | "long" | "channel" | "date" | "int" };
type ToolSpec = { vi: string; en: string; riskVi?: string; riskEn?: string; destructive?: boolean; args: Record<string, ArgSpec> };

const CHANNELS: Record<string, string> = { facebook: "Facebook", google: "Google", tiktok: "TikTok", zalo: "Zalo", website: "Website", blog: "Blog", instagram: "Instagram", youtube: "YouTube" };

export const TOOLS: Record<string, ToolSpec> = {
  ads_create_campaign: {
    vi: "Tạo và chạy chiến dịch quảng cáo", en: "Create and launch an ad campaign", destructive: true,
    riskVi: "Hành động này chi tiền quảng cáo.", riskEn: "This spends ad budget.",
    args: { name: { vi: "Tên chiến dịch", en: "Campaign" }, channel: { vi: "Kênh", en: "Channel", kind: "channel" },
      budget_vnd: { vi: "Ngân sách", en: "Budget", kind: "vnd" }, audience: { vi: "Đối tượng", en: "Audience" }, creative: { vi: "Nội dung quảng cáo", en: "Creative", kind: "long" } },
  },
  content_publish: {
    vi: "Đăng bài công khai", en: "Publish a post", destructive: true,
    riskVi: "Bài sẽ hiện công khai trên kênh.", riskEn: "The post becomes public on the channel.",
    args: { channel: { vi: "Kênh", en: "Channel", kind: "channel" }, title: { vi: "Tiêu đề", en: "Title" }, body: { vi: "Nội dung", en: "Body", kind: "long" } },
  },
  crm_log_deal: {
    vi: "Ghi deal vào CRM", en: "Log a CRM deal",
    args: { customer: { vi: "Khách hàng", en: "Customer" }, product: { vi: "Sản phẩm", en: "Product" }, value_vnd: { vi: "Giá trị", en: "Value", kind: "vnd" },
      contact: { vi: "Liên hệ", en: "Contact" }, stage: { vi: "Giai đoạn", en: "Stage" }, note: { vi: "Ghi chú", en: "Note", kind: "long" } },
  },
  crm_update_deal: { vi: "Cập nhật deal CRM", en: "Update a CRM deal", args: { deal_id: { vi: "Deal", en: "Deal", kind: "int" }, stage: { vi: "Giai đoạn", en: "Stage" }, note: { vi: "Ghi chú", en: "Note", kind: "long" } } },
  crm_schedule_followup: { vi: "Hẹn follow-up khách", en: "Schedule a follow-up", args: { deal_id: { vi: "Deal", en: "Deal", kind: "int" }, due_iso: { vi: "Thời điểm", en: "Due", kind: "date" }, action: { vi: "Việc cần làm", en: "Action", kind: "long" } } },
};

export const vnd = (v: unknown, lang: Lang = "vi") => {
  const n = typeof v === "number" ? v : Number(String(v).replace(/[^\d.-]/g, ""));
  if (!Number.isFinite(n)) return String(v);
  return lang === "vi" ? `${new Intl.NumberFormat("vi-VN").format(n)} ₫` : `${new Intl.NumberFormat("en-US").format(n)} VND`;
};

function fmt(v: unknown, kind: ArgSpec["kind"], lang: Lang): string {
  if (v === null || v === undefined || v === "") return lang === "vi" ? "(trống)" : "(empty)";
  if (kind === "vnd") return vnd(v, lang);
  if (kind === "channel") return CHANNELS[String(v).toLowerCase()] ?? String(v);
  if (kind === "int") return `#${v}`;
  if (kind === "date") { const d = new Date(String(v)); return Number.isNaN(+d) ? String(v) : d.toLocaleString(lang === "vi" ? "vi-VN" : "en-GB", { timeZone: "Asia/Ho_Chi_Minh", dateStyle: "short", timeStyle: "short" }); }
  const s = typeof v === "string" ? v : JSON.stringify(v);
  const max = kind === "long" ? 280 : 120;
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

export type Row = { label: string; value: string };
export function describeAction(name: string, args: Record<string, unknown> = {}, lang: Lang = "vi") {
  const spec = TOOLS[name];
  const rows: Row[] = [];
  const known = spec ? Object.keys(spec.args) : [];
  for (const k of known) if (k in args) rows.push({ label: spec!.args[k][lang], value: fmt(args[k], spec!.args[k].kind, lang) });
  for (const [k, v] of Object.entries(args)) if (!known.includes(k)) rows.push({ label: k, value: fmt(v, /_vnd$/.test(k) ? "vnd" : undefined, lang) });
  return {
    title: spec ? spec[lang] : name,
    risk: spec ? (lang === "vi" ? spec.riskVi : spec.riskEn) : undefined,
    destructive: spec?.destructive ?? true,
    rows,
  };
}
