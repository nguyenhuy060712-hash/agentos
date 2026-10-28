// LangSmith parity numbers, copied from agentos-local/docs/GD9_PARITY.md §1 (24–25/10/2026) and TONG_KET_GD6-9.md.
// Method (19/10/2026): one row per public LangSmith docs page; score = (done + 0.5 × partial) / (done + partial + missing);
// rows that only make sense on LangSmith Cloud are excluded. Confirmed at the GĐ9.Z gate (25/10/2026).
export const parity = {
  // Newer, much stricter per-feature catalogue (25/10/2026: 2,476 LangSmith features, each scored done/partial/missing),
  // re-scored after every phase. Source: agentos-local/docs/GD13_PARITY.md (27/10/2026). Headline = SME-essential share.
  latest: {
    date: { vi: "27/10/2026", en: "27 Oct 2026" }, catalogue: 2476,
    cols: { vi: ["Chỉ số", "Trước GĐ10", "Sau GĐ11", "Sau GĐ12", "Sau GĐ13"], en: ["Metric", "Before", "After phase 11", "After phase 12", "After phase 13"] },
    rows: [
      { vi: "Nhu cầu thiết yếu của doanh nghiệp vừa và nhỏ", en: "Essential needs of small and medium businesses", v: [74.8, 81.3, 84.6, 84.6] },
      { vi: "Thiết yếu + hữu ích", en: "Essential + useful", v: [58.5, 64.1, 66.7, 68.9] },
      { vi: "Toàn bộ danh mục (gồm cả tính năng doanh nghiệp lớn)", en: "Whole catalogue (incl. large-enterprise features)", v: [45.1, 48.4, 50.0, 51.3] },
    ],
  },
  provisional: false, // 9.Z gate ticked in GD9_PLAN.md (25/10/2026); BAO_CAO_TU_DONG.md confirms 89.5%
  date: { vi: "25/10/2026", en: "25 Oct 2026" },
  baseline: { vi: "19/10/2026", en: "19 Oct 2026" },
  total: { before: 71.1, now: 89.5, done: 191, partial: 42, missing: 4, rows: 237 },
  rows: [
    { key: "P1", vi: "Quan sát (Observability)", en: "Observability", before: [44, 12, 0], now: [49, 7, 0], pctBefore: 89, pctNow: 94, area: "observe" },
    { key: "P2", vi: "Đánh giá (Evaluation)", en: "Evaluation", before: [40, 7, 18], now: [53, 8, 4], pctBefore: 67, pctNow: 88, area: "eval" },
    { key: "P3", vi: "Prompt & Context", en: "Prompt engineering", before: [10, 7, 2], now: [14, 4, 0], pctBefore: 71, pctNow: 89, area: "prompts" },
    { key: "P4", vi: "Builder / Fleet / Studio", en: "Builder / Fleet / Studio", before: [16, 7, 3], now: [20, 6, 0], pctBefore: 75, pctNow: 88, area: "agents" },
    { key: "P5", vi: "Triển khai / Agent Server", en: "Deployment / Agent Server", before: [23, 7, 10], now: [34, 6, 0], pctBefore: 66, pctNow: 93, area: "deploy" },
    { key: "P6", vi: "LLM Gateway", en: "LLM Gateway", before: [10, 5, 2], now: [12, 5, 0], pctBefore: 74, pctNow: 85, area: "cost" },
    { key: "P7", vi: "Quản trị / Xác thực / Sandbox", en: "Admin / Auth / Sandboxes", before: [3, 3, 10], now: [9, 6, 0], pctBefore: 28, pctNow: 80, area: "security" },
  ],
  // Honest gaps, from GD9_PARITY.md §3–§4.
  gaps: {
    vi: [
      "Đánh giá: còn 4 mục chưa có (tối ưu bộ phân loại, backtest trên bản agent mới, gắn giám khảo tự chạy theo dataset, đọc kết quả thí nghiệm tại máy). Backtest và giám khảo gắn dataset đã có trong mã nhưng chưa có bài kiểm chứng.",
      "Studio hiển thị dòng thời gian, chưa vẽ sơ đồ nút–cạnh của graph; chưa có trình đặt điểm dừng trước/sau trên giao diện.",
      "Prompt Hub chỉ dùng biến dạng {{biến}}; chưa có mẫu f-string/jinja và prompt có ảnh được lưu sẵn.",
      "Engine chỉ đề xuất sửa chỉ dẫn và cấu hình, không tự sửa mã; chưa có nhóm chủ đề do người dùng tự đặt.",
      "Kênh: chưa có Microsoft Teams, WhatsApp/Messenger.",
    ],
    en: [
      "Evaluation: 4 rows still missing (classifier optimisation, backtests on a new agent version, dataset-bound auto-run evaluators, reading experiment results locally). Backtests and dataset-bound evaluators exist in code but lack a proving test.",
      "Studio shows a timeline, not a node-and-edge graph canvas; no interrupt-before/after breakpoint editor in the UI yet.",
      "Prompt Hub uses {{variable}} templates only; no f-string/jinja formats or saved multimodal prompts.",
      "Engine proposes instruction and config fixes, not code changes; no user-defined insight categories.",
      "Channels: no Microsoft Teams, WhatsApp or Messenger yet.",
    ],
  },
  // Where Agent OS goes beyond LangSmith (things LangSmith does not offer as a product), from TONG_KET_GD6-9.md §2.
  extras: {
    vi: ["Chạy trọn trên máy chủ của khách, không cần gói Enterprise", "Giao diện chat cho nhân viên, có voice song công", "Kênh Telegram, Zalo, Slack với nút duyệt trong kênh", "Che dữ liệu cá nhân tiếng Việt, cả trong ảnh", "Tạo agent bằng trò chuyện, mẫu theo ngành cho doanh nghiệp Việt", "Tạo file Word/Excel/PowerPoint/PDF tiếng Việt"],
    en: ["Runs entirely on the customer's server, no Enterprise plan needed", "A chat app for staff with full-duplex voice", "Telegram, Zalo and Slack channels with in-channel approvals", "Vietnamese personal-data masking, images included", "Build agents by chatting, industry templates for Vietnamese businesses", "Creates Word/Excel/PowerPoint/PDF files in Vietnamese"],
  },
};
