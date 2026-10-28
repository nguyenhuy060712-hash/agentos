// Final exam and certification rubric. Public: format, practical[].task, capstone.brief/deliverables/criteria
// (name + points + description) and defense. Instructor-only ([GV]): mcq answers + rubric, practical[].pass.
import type { QuizItem } from "./types";

export interface Practical { level: "L1" | "L2" | "L3"; mins: number; task: string; pass: string /* [GV] */ }
type Mcq = QuizItem & { level: "L1" | "L2" | "L3"; module: number };

const q = (level: Mcq["level"], module: number, question: string, options: string[], a: string): Mcq =>
  ({ level, module, q: question, options, a, rubric: "1 điểm; không trừ điểm câu sai." });

export const exam: {
  format: { level: string; mcq: number; pass: string; practical: string; capstone?: string }[];
  mcq: Mcq[];
  practical: Practical[];
  capstone: { brief: string; deliverables: string[]; criteria: [string, number, string][]; defense: string };
} = {
  format: [
    { level: "L1 · Agent OS Practitioner", mcq: 15, pass: "≥ 70% trắc nghiệm và đạt bài thực hành", practical: "45 phút" },
    { level: "L2 · Agent OS Administrator", mcq: 30, pass: "≥ 75% trắc nghiệm và đạt bài thực hành", practical: "90 phút" },
    { level: "L3 · Agent OS Expert", mcq: 40, pass: "≥ 80% trắc nghiệm, đạt bài thực hành và capstone ≥ 75/100", practical: "120 phút", capstone: "Bảo vệ 20 phút trước hội đồng 2 người" },
  ],
  // Bank: L1 questions are used by every level; L2 adds L2; L3 adds L3. Draw randomly per candidate, keep module balance.
  mcq: [
    q("L1", 1, "Toàn bộ dữ liệu chat, bộ nhớ, trace của khách nằm ở đâu?", ["Cloud của MozaAI", "PostgreSQL trên máy chủ riêng của khách", "Trình duyệt người dùng", "Nhà cung cấp model"], "PostgreSQL trên máy chủ riêng của khách."),
    q("L1", 1, "Thành phần nào giúp run tiếp tục sau khi người duyệt bấm Cho phép?", ["Store", "Checkpoint", "Trace", "Webhook"], "Checkpoint."),
    q("L1", 1, "Phím tắt mở Trợ lý Agent OS là:", ["Ctrl + K", "Ctrl + I", "Ctrl + F", "Ctrl + J"], "Ctrl + I."),
    q("L1", 2, "Công cụ nào nên đánh dấu Hỏi trước?", ["Tra tồn kho", "Đọc lịch", "Gửi email cho khách", "Tìm tài liệu nội bộ"], "Gửi email cho khách."),
    q("L1", 2, "Khi duyệt, chọn \"Sửa\" nghĩa là:", ["Huỷ run", "Chạy với tham số đã chỉnh", "Tạo phiên bản agent mới", "Chuyển cho admin"], "Chạy với tham số đã chỉnh."),
    q("L1", 2, "Studio rẽ nhánh từ một checkpoint thì nhánh cũ:", ["Bị xoá", "Được giữ nguyên", "Bị ghi đè", "Chuyển thành bản nháp"], "Được giữ nguyên."),
    q("L1", 3, "Chính sách dùng chung cho nhiều agent nên đặt ở:", ["System prompt từng agent", "Gói ngữ cảnh ghim theo tag", "Bộ nhớ dài hạn", "Tên agent"], "Gói ngữ cảnh ghim theo tag."),
    q("L1", 3, "Skill khác AGENTS.md ở chỗ:", ["Skill luôn được nạp", "Skill chỉ được đọc khi cần", "Skill không có phiên bản", "Skill chỉ cho admin"], "Skill chỉ được đọc khi cần."),
    q("L1", 3, "Prompt Canvas lưu kết quả thành:", ["Một agent mới", "Một commit mới của prompt", "Một trace", "Một dataset"], "Một commit mới."),
    q("L1", 4, "Muốn mọi chat về một khách tự biết bảng giá riêng, dùng:", ["Chỉ dẫn riêng", "Dự án có tệp", "Ghim chat", "Lịch chạy"], "Dự án có tệp."),
    q("L1", 4, "Excel đưa vào chat được tính chính xác nhờ:", ["Model đọc chữ", "Python trong sandbox", "Trace", "Gateway"], "Python trong sandbox."),
    q("L1", 4, "Link chia sẻ cuộc trò chuyện có hạn tối đa:", ["1 ngày", "7 ngày", "30 ngày", "Không hạn"], "30 ngày."),
    q("L1", 4, "Mất điện thoại đăng nhập 2 lớp, bước đầu là:", ["Tạo tài khoản mới", "Dùng mã dự phòng", "Tắt 2 lớp qua chat", "Đổi email"], "Dùng mã dự phòng."),
    q("L1", 6, "Kiểu xem trace để biết model đã nhận chính xác gì:", ["Tin nhắn", "Hành trình", "Chi tiết", "Giám sát"], "Chi tiết."),
    q("L1", 6, "Bấm Áp dụng ở một vấn đề Engine sẽ:", ["Sửa ngay agent đang chạy", "Tạo commit prompt mới chưa chạy thật", "Xoá trace lỗi", "Gửi email cho khách"], "Tạo commit prompt mới chưa chạy thật."),
    q("L1", 6, "Nhãn môi trường của project gồm:", ["test/live", "dev/staging/prod", "alpha/beta", "local/cloud"], "dev/staging/prod."),
    q("L2", 5, "Kênh nào không cần tên miền HTTPS công khai?", ["Zalo OA", "Slack Socket Mode", "Đăng nhập Google", "OAuth Microsoft"], "Slack Socket Mode."),
    q("L2", 5, "Người nhắn qua Zalo OA dùng được Gmail của:", ["Chủ kênh", "Admin", "Không ai (trừ khi bật danh tính agent có kiểm toán)", "Người tạo agent"], "Không ai, trừ danh tính agent có kiểm toán."),
    q("L2", 5, "Tin tư vấn Zalo OA miễn phí trong bao lâu kể từ tin cuối của khách?", ["24 giờ", "48 giờ", "7 ngày", "30 ngày"], "48 giờ."),
    q("L2", 5, "Bí mật trong Agent OS chỉ gửi được tới:", ["Mọi host", "Máy đích đã khai", "Model", "Trình duyệt"], "Máy đích đã khai."),
    q("L2", 7, "Tiêu chí \"không chứa số điện thoại\" nên chấm bằng:", ["Giám khảo AI", "Kiểm tra cứng regex", "So cặp", "Người chấm"], "Kiểm tra cứng regex."),
    q("L2", 7, "Kappa 0,3 giữa giám khảo AI và người chấm nghĩa là:", ["Rất tốt", "Tốt", "Yếu, cần căn chỉnh", "Hoàn hảo"], "Yếu, cần căn chỉnh."),
    q("L2", 7, "\"Chạy lại dòng lỗi\" sẽ:", ["Chạy lại cả thí nghiệm", "Chỉ chạy lại dòng lỗi/thiếu", "Xoá dòng lỗi", "Tạo dataset mới"], "Chỉ chạy lại dòng lỗi/thiếu."),
    q("L2", 7, "Bám tài liệu thấp, tìm đúng tài liệu cao trong RAG nghĩa là:", ["Kho tài liệu hỏng", "Model bịa ngoài tài liệu", "Dataset sai kiểu", "Câu hỏi lạc đề"], "Model bịa ngoài tài liệu."),
    q("L2", 8, "Vai trò tạo/sửa agent nhưng không quản trị người dùng:", ["admin", "editor", "viewer", "owner"], "editor."),
    q("L2", 8, "Giá model mới có hiệu lực khi:", ["Tải về", "Admin duyệt", "Khởi động lại", "Đầu tháng"], "Admin duyệt."),
    q("L2", 8, "Khi chạm trần chi phí kiểu Chặn, API trả mã:", ["401", "402", "429", "503"], "402."),
    q("L2", 8, "Đòn bẩy giảm chi phí lớn nhất thường là:", ["Đổi giao diện", "Model vừa sức + định tuyến", "Tắt trace", "Xoá bộ nhớ"], "Model vừa sức + định tuyến."),
    q("L2", 6, "Webhook của Agent OS ký bằng:", ["MD5 body", "HMAC-SHA256 trên timestamp.body", "RSA", "Không ký"], "HMAC-SHA256 trên X-AOS-Timestamp + \".\" + body."),
    q("L2", 3, "Đổi tag prod của gói sang commit mới thì agent ghim @prod dùng bản mới:", ["Ngay giữa lượt", "Ở lượt sau", "Sau khi lưu agent", "Sau khi khởi động lại"], "Ở lượt sau."),
    q("L2", 2, "Agent con kế thừa từ trưởng nhóm:", ["Model", "Cấu hình duyệt", "Tên", "Kênh"], "Cấu hình duyệt."),
    q("L2", 1, "Ứng dụng Agent OS nghe ở đâu?", ["0.0.0.0 công khai", "127.0.0.1 sau Caddy", "Cổng 80 trực tiếp", "Trên máy người dùng"], "127.0.0.1 sau Caddy."),
    q("L3", 9, "Khoá chỉ gửi được trace, không đọc dữ liệu:", ["aos_", "ak_", "vk-", "cookie"], "ak_."),
    q("L3", 9, "Đường dẫn API tương thích LangGraph:", ["/v1", "/api/v1", "/graph", "/langgraph"], "/api/v1."),
    q("L3", 9, "Số công cụ của /mcp/aos:", ["11", "13", "15", "18"], "15 (11 đọc, 4 ghi)."),
    q("L3", 9, "Thẻ agent A2A ở:", ["/a2a/{id}/.well-known/agent-card.json", "/.well-known/a2a", "/api/v1/cards", "/mcp/card"], "/a2a/{id}/.well-known/agent-card.json."),
    q("L3", 9, "Tiếp tục một run đang dừng qua SDK dùng:", ["runs.cancel", "command resume", "threads.delete", "store.put"], "command={\"resume\": ...}."),
    q("L3", 9, "Checkpointer tuỳ chỉnh được nạp từ:", ["API HTTP", "aos.json / biến môi trường", "Giao diện Chat", "MCP"], "aos.json / biến môi trường."),
    q("L3", 10, "RLS của Agent OS dùng biến phiên:", ["app.ws, app.uid, app.role", "user_id", "tenant", "search_path"], "app.ws, app.uid, app.role."),
    q("L3", 10, "Vai trò DB của truy vấn ứng dụng:", ["postgres superuser", "aos_rls NOBYPASSRLS", "public", "readonly"], "aos_rls NOBYPASSRLS."),
    q("L3", 10, "Thuật toán mã hoá khi lưu:", ["AES-128-CBC", "AES-256-GCM-SIV", "RSA-2048", "ChaCha20 không xác thực"], "AES-256-GCM-SIV."),
    q("L3", 10, "Lệnh bọc lại khoá dữ liệu bằng khoá gốc mới:", ["aos_crypto migrate", "aos_crypto rotate --kek", "aos_crypto rotate --dek", "aos_crypto status"], "rotate --kek."),
    q("L3", 10, "Khi OCR che dữ liệu trong ảnh bị lỗi thì:", ["Gửi ảnh gốc", "Không gửi ảnh", "Gửi ảnh mờ", "Hỏi người dùng"], "Không gửi ảnh (fail closed)."),
    q("L3", 10, "Lớp phòng thủ mạnh nhất trước prompt injection gửi email:", ["Dặn trong prompt", "Không cấp công cụ gửi email", "Nhiệt độ thấp", "Đổi model"], "Không cấp công cụ gửi email."),
    q("L3", 11, "Phát hành chỉ khởi động lại:", ["Toàn bộ máy", "Caddy", "API và worker", "PostgreSQL"], "API và worker."),
    q("L3", 11, "Ghi đè CSDL đang chạy khi khôi phục cần:", ["--force", "--into-live --yes-overwrite-live", "--latest", "Không cần gì"], "--into-live --yes-overwrite-live và tắt API."),
    q("L3", 11, "Chạy toàn bộ kiểm thử máy chủ an toàn RAM bằng:", ["pytest một lần", "tests/run_all.sh", "npm test", "check-a7"], "tests/run_all.sh (mỗi tệp một tiến trình)."),
    q("L3", 12, "Mẫu kiến trúc cho việc nhiều bước, nhiều chuyên môn, cần song song:", ["Một agent", "Trưởng nhóm + agent con", "Theo lịch", "A2A"], "Trưởng nhóm + agent con."),
  ],
  practical: [
    { level: "L1", mins: 45, task: "Trong Agent OS Chat: tạo một dự án có tệp; đưa một file Excel vào và lấy kết quả tính đúng; tạo một file Word tóm tắt; chạy một nghiên cứu sâu có sửa kế hoạch trước khi duyệt; bật đăng nhập 2 lớp; tìm lại trace của một câu trả lời và chỉ ra bước gọi công cụ.",
      pass: "Đạt khi hoàn thành ≥ 5/6 việc; bắt buộc: số liệu Excel đúng và chỉ đúng bước công cụ trong trace." },
    { level: "L2", mins: 90, task: "Dựng agent CSKH cho một tình huống cho sẵn: gói ngữ cảnh có tag prod và ghim; công cụ ghi Hỏi trước; trần chi phí ngày; chia sẻ cho nhóm; kênh giả lập với 2 khách; dataset ≥ 15 câu, 2 giám khảo + gộp điểm + cổng; thí nghiệm A/B khi đổi chính sách bằng chuyển tag; một cảnh báo.",
      pass: "Đạt khi: đổi chính sách chỉ bằng chuyển tag (không phiên bản agent mới); mọi công cụ ghi Hỏi trước; cổng hoạt động; 2 khách có 2 thread; trình bày quyết định phát hành dựa trên so sánh theo ví dụ. Thiếu một trong các mục bắt buộc là không đạt." },
    { level: "L3", mins: 120, task: "Trên máy lab: (1) gọi agent qua langgraph-sdk đến điểm dừng duyệt và tiếp tục; (2) liệt kê công cụ /mcp/aos; (3) gửi một trace OTLP bằng khoá ak_; (4) chứng minh khoá workspace A không đọc được dữ liệu workspace B; (5) bật che dữ liệu cá nhân chế độ tên giả và chứng minh trên trace; (6) sao lưu, phát hành thay đổi, chẩn đoán lỗi được dựng sẵn và quay lại trong thời gian quy định; (7) viết quy trình đổi khoá gốc.",
      pass: "Đạt khi ≥ 6/7 mục, bắt buộc mục 4, 6, 7; quay lại trong ≤ 20 phút; không có bí mật nào lộ trong màn hình, log nộp, ảnh." },
  ],
  capstone: {
    brief: "Nhóm 2–4 học viên thiết kế và dựng một hệ thống agent cho một bài toán doanh nghiệp thật (hoặc đề giảng viên giao), theo phương pháp 9 bước, trên máy lab Agent OS với dữ liệu tự tạo hoặc đã được phép, rồi bảo vệ trước hội đồng.",
    deliverables: [
      "Tài liệu thiết kế (≤ 12 trang): bài toán và KPI, ma trận tự chủ, mẫu kiến trúc + sơ đồ, kiến trúc ngữ cảnh, công cụ và quyền (\"quyền của ai\"), mô hình đe doạ và dữ liệu cá nhân, ngân sách, kế hoạch đánh giá, kế hoạch ra mắt có tiêu chí dừng.",
      "Hệ thống dựng trên máy lab: gói ngữ cảnh có tag, agent(s), công cụ và duyệt, trần chi phí, (nếu có) kênh giả lập.",
      "Dataset ≥ 20 ví dụ, giám khảo đã căn chỉnh với người chấm (có số kappa), giám khảo gộp điểm có cổng, ít nhất 2 thí nghiệm so sánh.",
      "≥ 3 ADR.",
      "Runbook vận hành 1 trang và phiếu go-live.",
      "Slide bảo vệ (≤ 12 trang) và bản trình diễn trực tiếp 5 phút.",
    ],
    criteria: [
      ["Bài toán, KPI và giá trị kinh doanh", 10, "KPI đo được, có mức nền và mục tiêu, phạm vi vừa sức."],
      ["Kiến trúc và mức tự chủ", 15, "Mẫu kiến trúc hợp lý, ma trận tự chủ có người duyệt, sơ đồ rõ."],
      ["Ngữ cảnh và kiến thức", 10, "Đặt kiến thức đúng chỗ, gói có tag, Skill/RAG khi cần."],
      ["Bảo mật, quyền và dữ liệu cá nhân", 15, "Mô hình đe doạ, quyền tối thiểu, Hỏi trước, che dữ liệu, nhận diện phần cần chuyên gia/luật sư."],
      ["Chi phí", 10, "Ước tính có công thức, trần nhiều tầng, đòn bẩy giảm chi phí."],
      ["Đánh giá chất lượng", 15, "Dataset từ câu thật/ca khó, giám khảo phù hợp và đã căn chỉnh, cổng, so sánh theo ví dụ."],
      ["Ra mắt và vận hành", 10, "Dev → staging → prod, tiêu chí dừng, quay lại, cảnh báo, runbook."],
      ["Hệ thống dựng được", 10, "Trình diễn chạy thật trên máy lab đúng thiết kế."],
      ["Trình bày và phản biện", 5, "Rõ ràng, đúng giờ, trả lời phản biện có căn cứ."],
    ],
    defense: "Mỗi nhóm 20 phút: 10 phút trình bày, 5 phút trình diễn trực tiếp trên máy lab, 5 phút hội đồng hỏi. Hội đồng 2 người (giảng viên + một chuyên gia) chấm độc lập theo rubric, lấy trung bình; chênh nhau > 15 điểm thì trao đổi để thống nhất. Từng thành viên phải trả lời được câu hỏi về mọi phần của đồ án.",
  },
};
