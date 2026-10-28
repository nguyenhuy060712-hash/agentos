import type { Module } from "./types";

const m: Module = {
  n: 12, slug: "kien-truc-capstone", track: "kien-truc", roles: ["architect"], hours: 3,
  title: "Kiến trúc hệ thống agent và đồ án capstone",
  short: "Kiến trúc và capstone",
  summary: "Phương pháp thiết kế một hệ thống agent cho doanh nghiệp từ bài toán tới vận hành: số đo, ma trận tự chủ, chọn mẫu kiến trúc, kiến trúc ngữ cảnh, quyền, ngân sách, kế hoạch đánh giá, kế hoạch ra mắt, ghi quyết định kiến trúc (ADR); và xưởng làm đồ án capstone.",
  goals: [
    "Áp dụng phương pháp thiết kế 9 bước để tạo bản thiết kế hoàn chỉnh cho một bài toán thật.",
    "Chọn đúng mẫu kiến trúc (một agent + công cụ, bộ định tuyến, trưởng nhóm + agent con, quy trình có cổng duyệt, chạy theo lịch) và giải thích đánh đổi.",
    "Nhận diện 8 phản mẫu (anti-pattern) thường gặp và cách sửa trên Agent OS.",
    "Viết được ít nhất 3 bản ghi quyết định kiến trúc (ADR) và một kế hoạch ra mắt từng bước có tiêu chí dừng.",
    "Hoàn thành bản nháp đồ án capstone và nhận phản biện theo rubric.",
  ],
  prereq: ["Buổi 1–11."],
  sections: [
    {
      id: "phuong-phap", h: "Phương pháp thiết kế 9 bước",
      html: `<ol>
<li><strong>Bài toán và số đo</strong>: mục tiêu kinh doanh, KPI (tỉ lệ tự xử lý, thời gian phản hồi, điểm đúng chính sách, tỉ lệ 👎, chi phí/việc), mức nền hiện tại.</li>
<li><strong>Hành trình và ma trận tự chủ</strong>: liệt kê việc; mỗi việc: tự làm / làm có duyệt / chuyển người / không cho làm; ai duyệt.</li>
<li><strong>Chọn mẫu kiến trúc</strong> (bảng dưới): bắt đầu đơn giản nhất đủ dùng.</li>
<li><strong>Kiến trúc ngữ cảnh</strong>: system prompt ngắn; gói ngữ cảnh dùng chung có tag; Skill cho quy trình dài; RAG cho kho lớn; bộ nhớ theo người; dữ liệu không được đưa cho model.</li>
<li><strong>Công cụ và quyền</strong>: danh sách công cụ, đọc/ghi, cổng tích hợp (MCP/API), bí mật + máy đích, "quyền của ai" cho từng kênh.</li>
<li><strong>An toàn và dữ liệu</strong>: mô hình đe doạ, che dữ liệu cá nhân, mã hoá, nhóm và vai trò, pháp lý.</li>
<li><strong>Ngân sách</strong>: ước tính token × giá × khối lượng, trần nhiều tầng, định tuyến.</li>
<li><strong>Kế hoạch đánh giá</strong>: dataset từ câu thật, giám khảo + căn chỉnh, gộp điểm, cổng, chấm trực tuyến, backtest.</li>
<li><strong>Ra mắt và vận hành</strong>: dev → staging → prod theo đợt, tiêu chí dừng và quay lại, cảnh báo, vòng cải tiến hằng tuần, chủ sở hữu từng phần.</li>
</ol>`,
    },
    {
      id: "mau-kien-truc", h: "Mẫu kiến trúc và khi nào dùng",
      diagram: "case", diagramCaption: "Ví dụ: ba agent dùng chung gói ngữ cảnh, công cụ, gateway và vòng đánh giá",
      html: `<table><thead><tr><th>Mẫu</th><th>Mô tả</th><th>Dùng khi</th><th>Trên Agent OS</th></tr></thead><tbody>
<tr><td>Một agent + công cụ</td><td>Một agent, vài công cụ đọc, ít công cụ ghi có duyệt</td><td>Phần lớn bài toán CSKH, tra cứu, trợ lý phòng ban</td><td>Cấu hình agent, Hỏi trước</td></tr>
<tr><td>Bộ định tuyến</td><td>Phân loại yêu cầu rồi chuyển tới agent/model phù hợp</td><td>Nhiều loại câu hỏi rất khác nhau, cần tiết kiệm chi phí</td><td>@agent trong Chat; Gateway model "auto"; giám khảo quyết định phân loại</td></tr>
<tr><td>Trưởng nhóm + agent con</td><td>Lập kế hoạch, chia việc chuyên môn, tổng hợp</td><td>Việc nhiều bước, nhiều chuyên môn, cần song song</td><td>Sub-agents (Deep Agents), Nghiên cứu sâu</td></tr>
<tr><td>Quy trình có cổng duyệt</td><td>Các bước cố định, người duyệt ở điểm then chốt</td><td>Tài chính, pháp lý, đăng bài, hoàn tiền</td><td>Hỏi trước, Hộp duyệt, thẻ hỏi thông tin</td></tr>
<tr><td>Chạy theo lịch / sự kiện</td><td>Không có người hỏi; chạy định kỳ hoặc khi có sự kiện</td><td>Báo cáo, quét việc, đối soát</td><td>Lịch chạy, cron /runs/crons, webhook</td></tr>
<tr><td>Agent-với-agent</td><td>Phối hợp với agent hệ thống khác</td><td>Đối tác, hệ thống nội bộ khác đã có agent</td><td>A2A, MCP /mcp/agents/{id}</td></tr>
</tbody></table>`,
    },
    {
      id: "phan-mau", h: "Tám phản mẫu và cách sửa",
      html: `<table><thead><tr><th>Phản mẫu</th><th>Hậu quả</th><th>Sửa</th></tr></thead><tbody>
<tr><td>Agent "biết tuốt" với 30 công cụ</td><td>Chọn sai công cụ, đắt, rủi ro</td><td>Tách theo việc; công cụ tối thiểu</td></tr>
<tr><td>Chặn rủi ro chỉ bằng prompt</td><td>Bị prompt injection vượt qua</td><td>Không cấp công cụ, Hỏi trước, máy đích</td></tr>
<tr><td>Chép chính sách vào từng agent</td><td>Lệch nhau, sửa sót</td><td>Gói ngữ cảnh + tag</td></tr>
<tr><td>Sửa thẳng agent đang chạy</td><td>Không biết tốt hay tệ, không quay lại được</td><td>Commit → thí nghiệm → cổng → chuyển tag</td></tr>
<tr><td>Agent con cho mọi thứ</td><td>Chi phí, độ trễ, khó gỡ lỗi</td><td>Một agent trước; tách khi có lý do</td></tr>
<tr><td>Không có dataset</td><td>Cảm tính, tranh cãi</td><td>Dataset từ trace thật + ca khó</td></tr>
<tr><td>Không có trần chi phí</td><td>Hoá đơn bất ngờ</td><td>Trần nhiều tầng + cảnh báo</td></tr>
<tr><td>Dùng tài khoản cá nhân cho việc chung</td><td>Mượn quyền, mất kiểm soát khi người nghỉ</td><td>Danh tính agent, khoá dịch vụ, nhóm</td></tr>
</tbody></table>`,
    },
    {
      id: "adr", h: "Ghi quyết định kiến trúc (ADR)",
      html: `<p>Mỗi quyết định quan trọng ghi một ADR ngắn để người sau hiểu vì sao: <strong>Bối cảnh</strong> → <strong>Quyết định</strong> → <strong>Phương án đã loại và lý do</strong> → <strong>Hệ quả</strong> (tốt, xấu, việc phải làm) → <strong>Ngày, người quyết</strong>. Ví dụ đề tài ADR: chọn một agent hay trưởng nhóm + agent con; định tuyến model nào; bật mã hoá nội dung trace hay không (đánh đổi tìm kiếm); Zalo OA hay chỉ web chat giai đoạn 1; mức tự chủ của công cụ hoàn tiền.</p>
<pre><code>ADR-003: Không mã hoá nội dung trace ở giai đoạn 1
Bối cảnh: đội CSKH cần tìm chữ trong trace để xử lý khiếu nại.
Quyết định: tắt mã hoá nhóm trace; bật che dữ liệu cá nhân trong trace.
Loại: bật mã hoá (mất tìm kiếm), không che (rủi ro dữ liệu).
Hệ quả: xem lại sau 3 tháng; nhờ chuyên gia đánh giá.</code></pre>`,
    },
    {
      id: "capstone", h: "Đồ án capstone",
      html: `<p>Mỗi nhóm 2–4 học viên chọn một bài toán thật (hoặc đề giảng viên giao), thiết kế và dựng trên máy lab, rồi bảo vệ trước hội đồng. Đề, sản phẩm phải nộp và rubric chấm nằm ở trang <a href="/dao-tao/chung-nhan/">Chứng nhận</a>. Buổi 12 là xưởng làm việc: hoàn thành bản thiết kế 9 bước, dựng phần cốt lõi, nhận phản biện chéo. Bảo vệ đồ án và thi cuối khoá tổ chức ở buổi thi riêng (nửa ngày).</p>
<p>Gợi ý đề: CSKH + bán hàng Zalo cho shop; trợ lý kế toán đối soát hoá đơn; trợ lý nhân sự tuyển dụng (sàng lọc hồ sơ có duyệt); trợ lý báo cáo điều hành hằng tuần; trợ lý pháp chế rà hợp đồng; agent vận hành IT trả lời sự cố theo runbook.</p>`,
    },
  ],
  lab: {
    title: "Xưởng capstone: từ bài toán tới bản dựng đầu tiên",
    time: "75 phút",
    setup: "Nhóm 2–4 người, mỗi nhóm một workspace lab; mẫu thiết kế 9 bước (trong tài liệu học viên).",
    steps: [
      "Chọn đề, viết bước 1–2: KPI có mức nền và mục tiêu; ma trận tự chủ ≥ 8 việc.",
      "Bước 3: chọn mẫu kiến trúc, vẽ sơ đồ (agent, kênh, công cụ, dữ liệu, gateway, đánh giá).",
      "Bước 4–5: lập bảng \"kiến thức → đặt ở đâu\" và bảng công cụ/quyền/\"quyền của ai\".",
      "Dựng trên máy lab: gói ngữ cảnh có tag prod, agent chính ghim gói, công cụ Hỏi trước, trần chi phí, (nếu có) kênh giả lập.",
      "Bước 7–8: ước tính ngân sách tháng; tạo dataset ≥ 10 câu và 1 giám khảo gộp điểm có cổng; chạy một thí nghiệm.",
      "Viết 2 ADR.",
      "Phản biện chéo: mỗi nhóm trình bày 5 phút, nhóm khác chấm nháp theo rubric capstone và ghi 3 góp ý.",
    ],
    result: "Bản nháp thiết kế 9 bước; một hệ thống dựng được phần cốt lõi trên máy lab có một thí nghiệm qua (hoặc không qua) cổng; 2 ADR; phiếu phản biện.",
    check: ["Ma trận tự chủ có cột người duyệt.", "Không có công cụ ghi/gửi ra ngoài nào thiếu Hỏi trước (trừ khi có ADR giải thích).", "Có ước tính ngân sách và trần tương ứng.", "Thí nghiệm chạy được trên dataset của nhóm."],
  },
  pitfalls: [
    ["Đề quá rộng (\"agent cho cả công ty\").", "Thu về một phòng ban, 1–3 agent, KPI đo được trong 1 tháng."],
    ["Thiết kế đẹp nhưng không dựng được trên máy lab.", "Ưu tiên phần cốt lõi: gói + agent + quyền + thí nghiệm; phần còn lại mô tả."],
    ["Không có tiêu chí dừng khi ra mắt.", "Ghi rõ: ngưỡng lỗi, 👎, chi phí nào thì dừng/quay lại."],
    ["Bỏ qua pháp lý và dữ liệu cá nhân.", "Mục an toàn và dữ liệu là bắt buộc trong rubric."],
  ],
  quiz: [
    { q: "Bài toán: trả lời 20 loại câu hỏi đơn giản về chính sách nhân sự cho 500 nhân viên. Mẫu kiến trúc phù hợp nhất?", options: ["Trưởng nhóm + 5 agent con", "Một agent + công cụ đọc + RAG/gói ngữ cảnh", "Agent-với-agent qua A2A", "Chạy theo lịch"], a: "Một agent + công cụ đọc + kiến thức trong gói/RAG.", rubric: "1 điểm." },
    { q: "Phản mẫu nào bị khai thác bởi prompt injection?", options: ["Không có dataset", "Chặn rủi ro chỉ bằng prompt", "Không có trần chi phí", "Chép chính sách vào từng agent"], a: "Chặn rủi ro chỉ bằng prompt.", rubric: "1 điểm." },
    { q: "Nêu 5 thành phần của một ADR.", a: "Bối cảnh, quyết định, phương án đã loại và lý do, hệ quả, ngày/người quyết.", rubric: "2 điểm (≥ 4 thành phần đúng)." },
    { q: "Viết tiêu chí dừng cho đợt ra mắt prod đầu tiên của agent CSKH.", a: "Ví dụ: tỉ lệ lỗi > 3% trong 1 giờ; 👎 > 10% trong ngày; điểm chấm online < 0,8; chi phí/ngày > 150% dự kiến; bất kỳ sự cố dữ liệu cá nhân nào → dừng, quay lại tag trước, điều tra.", rubric: "3 điểm: ≥ 3 tiêu chí đo được (2), có hành động quay lại (1)." },
    { q: "Khi nào chọn trưởng nhóm + agent con thay vì một agent? Nêu một bằng chứng bạn sẽ dùng để biện minh.", a: "Khi việc nhiều bước, nhiều chuyên môn, cần song song hoặc ngữ cảnh bị ngập. Bằng chứng: thí nghiệm so sánh hai kiến trúc trên cùng dataset về điểm, độ trễ, chi phí; trace cho thấy ngữ cảnh quá dài.", rubric: "3 điểm: điều kiện (1), bằng chứng bằng thí nghiệm (1), có cân nhắc chi phí/độ trễ (1)." },
  ],
  homework: [
    "Hoàn thiện đồ án capstone theo danh sách sản phẩm phải nộp; nộp trước buổi bảo vệ 3 ngày.",
    "Ôn tập theo đề cương thi của cấp chứng nhận đăng ký.",
  ],
  glossary: [
    ["KPI", "Chỉ số đo mục tiêu."],
    ["Ma trận tự chủ", "Bảng việc × mức tự chủ × người duyệt."],
    ["Mẫu kiến trúc", "Cách tổ chức agent, công cụ, người đã được kiểm chứng."],
    ["Phản mẫu", "Cách làm phổ biến nhưng gây hại."],
    ["ADR", "Bản ghi quyết định kiến trúc."],
    ["Tiêu chí dừng", "Ngưỡng buộc dừng ra mắt và quay lại."],
    ["Capstone", "Đồ án tổng hợp cuối khoá."],
  ],
  slides: [
    { t: "Phương pháp 9 bước", pts: ["Bài toán, KPI · ma trận tự chủ · mẫu kiến trúc", "Ngữ cảnh · công cụ, quyền · an toàn, dữ liệu", "Ngân sách · đánh giá · ra mắt, vận hành"] },
    { t: "Mẫu kiến trúc", pts: ["Một agent + công cụ", "Bộ định tuyến", "Trưởng nhóm + agent con", "Quy trình có cổng duyệt · theo lịch · A2A"] },
    { t: "Ví dụ hệ thống", diagram: "case" },
    { t: "Tám phản mẫu", pts: ["Agent biết tuốt · chặn bằng prompt", "Chép chính sách · sửa thẳng prod", "Agent con tràn lan · không dataset", "Không trần · tài khoản cá nhân cho việc chung"] },
    { t: "ADR", code: "ADR-003: ...\nBối cảnh · Quyết định · Đã loại · Hệ quả · Ngày/người" },
    { t: "Kế hoạch ra mắt", pts: ["dev → staging → prod theo đợt", "Tiêu chí dừng đo được", "Quay lại tag trước", "Vòng cải tiến hằng tuần"] },
    { t: "Capstone", pts: ["Nhóm 2–4 người, bài toán thật", "Thiết kế 9 bước + dựng cốt lõi", "Thí nghiệm có cổng · 2+ ADR", "Bảo vệ trước hội đồng"] },
  ],
  plan: [
    { min: 10, act: "Ôn toàn khoá", detail: "Sơ đồ tư duy 12 buổi trên bảng." },
    { min: 45, act: "Giảng: 9 bước, mẫu kiến trúc, phản mẫu, ADR, ra mắt", detail: "Slide 1–6. Phân tích ví dụ Shop ABC." },
    { min: 10, act: "Nghỉ giải lao", detail: "" },
    { min: 75, act: "Xưởng capstone", detail: "Giảng viên đi từng nhóm, kiểm 4 điểm." },
    { min: 30, act: "Phản biện chéo", detail: "Mỗi nhóm 5 phút + nhận góp ý." },
    { min: 10, act: "Hướng dẫn thi và bảo vệ", detail: "Lịch, cách nộp, cấp chứng nhận." },
  ],
  ask: [
    "Hệ thống agent của bạn sẽ hỏng theo cách nào trước tiên? Bạn phát hiện ra bằng gì?",
    "Nếu ngân sách giảm một nửa, bạn cắt gì trong thiết kế?",
  ],
  notes: [
    "Đánh giá cao nhóm biết nói \"không làm\" với phần rủi ro và ghi ADR cho điều đó.",
    "Đề capstone dùng dữ liệu tự tạo hoặc đã được phép; không đưa dữ liệu khách thật lên máy lab.",
  ],
};
export default m;
