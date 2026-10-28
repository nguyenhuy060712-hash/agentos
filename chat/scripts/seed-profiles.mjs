// Local data only: agent profile fields (role / group / greeting / suggestions, VI default + *_en) in assistant.metadata.
// Usage: node scripts/seed-profiles.mjs   (also called by e2e.mjs). Idempotent PATCH (metadata merge).
const BASE = process.env.BASE ?? "http://localhost:8020";
const auth = "Basic " + Buffer.from("moza:phase0-local").toString("base64");
export const PROFILES = {
  "agent-1": { role: "Trợ lý tổng hợp", role_en: "General assistant", group: "Phòng AI số", group_en: "Digital AI team",
    greeting: "Chào anh, hôm nay em giúp gì được ạ?", greeting_en: "Hi, how can I help today?",
    suggestions: ["Tóm tắt cuộc họp sáng nay", "Soạn email cảm ơn khách hàng", "Lên kế hoạch tuần tới", "Giải thích tệp đính kèm"],
    suggestions_en: ["Summarise this morning's meeting", "Draft a thank-you email", "Plan next week", "Explain the attached file"] },
  "agent-2": { role: "Trưởng nhóm AI", role_en: "AI lead", group: "Phòng AI số", group_en: "Digital AI team",
    greeting: "Chào anh, chiến dịch hôm nay mình làm gì?", greeting_en: "Hi, what's on the campaign today?",
    suggestions: ["Tình hình chiến dịch thế nào?", "Lên lịch đăng bài tuần này", "Đề xuất ý tưởng nội dung"],
    suggestions_en: ["How is the campaign going?", "Schedule this week's posts", "Suggest content ideas"] },
  "agent-3": { role: "Điều phối viên", role_en: "Coordinator", group: "Phòng vận hành", group_en: "Operations",
    greeting: "Chào anh, em sẽ điều phối việc cho các agent.", greeting_en: "Hi, I'll coordinate the work across agents.",
    suggestions: ["Giao việc tổng hợp báo cáo tuần", "Kiểm tra tiến độ các nhóm", "Chuẩn bị tài liệu họp"],
    suggestions_en: ["Delegate the weekly report", "Check team progress", "Prepare meeting notes"] },
};
export async function seedProfiles() {
  const list = await (await fetch(BASE + "/api/v1/assistants/search", { method: "POST", headers: { Authorization: auth, "Content-Type": "application/json" }, body: JSON.stringify({ limit: 50 }) })).json();
  let n = 0;
  for (const a of list) { const p = PROFILES[a.graph_id]; if (!p) continue;
    const r = await fetch(`${BASE}/api/v1/assistants/${a.assistant_id}`, { method: "PATCH", headers: { Authorization: auth, "Content-Type": "application/json" }, body: JSON.stringify({ metadata: p }) });
    if (r.ok) n++; else console.log("seed", a.graph_id, r.status, await r.text()); }
  return n;
}
if (import.meta.url === `file://${process.argv[1]}`) console.log("profiles seeded:", await seedProfiles());
