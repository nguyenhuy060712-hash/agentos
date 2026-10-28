// "What's new" block on /tinh-nang/#moi and /en/features/#whats-new: the Oct 2026 upgrade (phases GĐ10–13).
// Source (read 27/10/2026): agentos-local/docs/BAO_CAO_GD10..13.md. Features of each phase = areas[].features with `since`.
// These phases are finished and tested on the development server; they reach the public demo at its next update.
export const whatsNew = {
  date: { vi: "27/10/2026", en: "27 Oct 2026" },
  phases: [
    { n: 10, icon: "shield",
      vi: { t: "Dữ liệu từng khách tách bạch, an toàn hơn", d: "Khoá API, Gateway, trace, webhook luôn ghi đúng workspace; sàn chặn mạng chung; lịch chạy theo giờ Việt Nam; nền nâng cấp dữ liệu quay lại được." },
      en: { t: "Tighter per-customer isolation and safety", d: "API keys, Gateway, traces and webhooks always land in the right workspace; a shared network guard; Vietnam-time schedules; reversible data migrations." } },
    { n: 11, icon: "bot",
      vi: { t: "Agent mạnh hơn", d: "Nhiều model qua một cổng có dự phòng; máy tính ảo riêng cho mỗi cuộc chat; kết nối OAuth; Gmail/Outlook/Lịch; agent tự chạy khi có thư; sửa Excel/Word/PowerPoint; bộ nhớ hai lớp." },
      en: { t: "Stronger agents", d: "Many models through one gateway with fallback; a private virtual computer per chat; OAuth connections; Gmail/Outlook/Calendar; agents that start on new email; editing Excel/Word/PowerPoint; two-layer memory." } },
    { n: 12, icon: "eye",
      vi: { t: "Làm việc thật và đo chất lượng", d: "Trung tâm thông báo; trang Giám sát dựng sẵn; cảnh báo và luật tự động; trace có cột chi phí và tìm toàn văn; dataset do AI sinh; Google Docs/Drive, OneDrive/SharePoint; trang Skills với 10 Skill mẫu tiếng Việt; mời bằng link." },
      en: { t: "Real work, measured quality", d: "A notification hub; a ready-made Monitoring page; alerts and automation rules; traces with cost columns and full-text search; AI-generated datasets; Google Docs/Drive, OneDrive/SharePoint; a Skills page with 10 Vietnamese templates; invite links." } },
    { n: 13, icon: "check",
      vi: { t: "Agent tự cải thiện, chạy bền", d: "Engine tự tìm lỗi, chạy lại an toàn và đề xuất sửa; Insights \"khách hỏi gì, lỗi ở đâu\"; duyệt bộ nhớ agent; đầu ra đúng khuôn JSON; agent tự đặt lịch; lượt chạy bền không gửi trùng; lưu giữ dữ liệu có kiểm soát." },
      en: { t: "Self-improving, durable agents", d: "Engine finds bugs, replays safely and proposes fixes; \"what are people asking, where does it fail\" Insights; reviewed agent memory; exact JSON output; self-scheduling agents; durable runs with no duplicate sends; controlled data retention." } },
  ],
  // Quality evidence from the GĐ13 gate (BAO_CAO_GD13.md).
  proof: {
    vi: ["2.104 bài kiểm tra tự động máy chủ đạt", "166/166 bài kiểm ứng dụng chat đạt", "Rà rò rỉ 292 đường dữ liệu sang tài khoản demo: 0 rò", "Thử với Gemini thật cả chuỗi Engine và Insights"],
    en: ["2,104 automated server tests pass", "166/166 chat app tests pass", "Leak audit of 292 data paths into the demo account: 0 leaks", "Engine and Insights tested end-to-end with real Gemini"],
  },
};
