import type { Module } from "./types";

const m: Module = {
  n: 6, slug: "quan-sat", track: "van-hanh", roles: ["user", "admin", "it", "architect"], hours: 3,
  title: "Quan sát: tracing, giám sát, Insights, Engine và cảnh báo",
  short: "Quan sát",
  summary: "Đọc trace như chuyên gia, tìm nhanh bằng bộ lọc và câu hỏi thường, theo dõi sức khoẻ theo môi trường, hiểu khách hỏi gì (Insights), biến lỗi thành vấn đề có nguyên nhân và đề xuất sửa (Engine), và đặt cảnh báo có chữ ký.",
  goals: [
    "Từ một lời phàn nàn của khách, tìm đúng trace trong dưới 3 phút và chỉ ra bước gây lỗi.",
    "Đọc được 4 kiểu xem trace và giải thích thời gian, token, chi phí của từng bước.",
    "Thiết lập môi trường dev/staging/prod cho project và so sánh số liệu giữa môi trường.",
    "Vận hành vòng Insights → Engine → bản sửa (commit) mà không đưa thẳng vào chạy thật.",
    "Tạo một quy tắc cảnh báo với kênh nhận phù hợp và giải thích chữ ký HMAC của webhook.",
  ],
  prereq: ["Buổi 1–2; đã có agent chạy trên máy lab (Lab 2–5)."],
  sections: [
    {
      id: "trace", h: "Trace: cấu trúc và cách đọc",
      shot: "trace-trajectory", shotCaption: "Trace dạng Hành trình: gọi model, công cụ, duyệt, thời gian, chi phí",
      html: `<p>Mỗi run sinh một <strong>trace</strong>: cây các bước (run con) như gọi model, gọi công cụ, chờ duyệt, lỗi. Mỗi bước có thời gian, token vào/ra, chi phí, metadata (model, agent, phiên bản, môi trường). Trace gom theo <strong>project</strong>.</p>
<p>Menu Tracing → chọn project → thẻ Tracing (cùng các thẻ Evaluators, Tự động hoá, Phân tích, Engine). Danh sách có 3 kiểu: <strong>Threads</strong>, <strong>Trace</strong>, <strong>Số run</strong>; ô Tìm trong input và output; Thêm bộ lọc. Trang chi tiết có 4 kiểu xem:</p>
<table><thead><tr><th>Kiểu xem</th><th>Dùng để</th></tr></thead><tbody>
<tr><td>Hành trình</td><td>Dòng thời gian từng bước: tìm bước chậm, bước lỗi, vòng lặp thừa.</td></tr>
<tr><td>Tin nhắn</td><td>Xem như đoạn chat: hiểu người dùng đã thấy gì.</td></tr>
<tr><td>Lượt</td><td>Cây run con.</td></tr>
<tr><td>Chi tiết</td><td>Đầu vào/đầu ra đầy đủ của từng bước: model đã nhận chính xác những gì.</td></tr>
</tbody></table>
<p><strong>Ctrl + F</strong> tìm và tô sáng trong trace dài. Nút <strong>Chạy trong Studio</strong> mở trace ở Studio để tua lại. Giới hạn: xem trước đầu vào/ra cố định 300 ký tự; chưa hiển thị ảnh/âm thanh trực tiếp trong trace.</p>
<p><strong>Quy trình điều tra lỗi</strong> (chuẩn của khoá): 1) xác định thời điểm + người/kênh; 2) lọc trace; 3) xem Tin nhắn để thấy người dùng thấy gì; 4) xem Hành trình tìm bước bất thường; 5) xem Chi tiết bước đó: ngữ cảnh model nhận, kết quả công cụ; 6) phân loại nguyên nhân: dữ liệu, công cụ, chỉ dẫn, model, giới hạn; 7) tạo ví dụ dataset từ trace để kiểm lại sau khi sửa.</p>`,
    },
    {
      id: "tim-kiem", h: "Tìm trace: bộ lọc, câu hỏi thường, bộ lọc đã lưu",
      shot: "trace-search", shotCaption: "Tìm và tô sáng trong một trace bằng Ctrl+F",
      html: `<p>Bộ lọc kết hợp theo thời gian, trạng thái lỗi, độ trễ, model, agent, tag, metadata, phản hồi. <strong>Hỏi bằng lời</strong>: gõ "lỗi trong 24 giờ qua", "run chậm hơn 5 giây dùng gemini hôm qua"; AI dịch thành bộ lọc, người dùng xem và sửa rồi mới Chạy; Lưu bộ lọc để dùng lại. Đo với model thật: đúng theo nghĩa 38/40 câu mẫu (95%). Công cụ <code>query_runs</code> có sắp xếp chậm nhất / tốn nhất.</p>`,
    },
    {
      id: "giam-sat", h: "Giám sát và môi trường",
      shot: "env-compare", shotCaption: "Giám sát: so sánh số liệu giữa dev, staging, prod",
      html: `<p>Menu <strong>Giám sát</strong>: số trace, tỉ lệ lỗi, độ trễ, token, chi phí theo thời gian, theo agent, theo model. Mỗi project, khoá nhận trace và cảnh báo gắn nhãn <strong>dev / staging / prod</strong>; ở Các project tracing lọc theo môi trường; Giám sát có nút Môi trường để so sánh. Nhờ vậy lỗi thử nghiệm không lẫn số liệu khách.</p>
<table><thead><tr><th>Chỉ số</th><th>Ngưỡng gợi ý cho agent CSKH</th></tr></thead><tbody>
<tr><td>Tỉ lệ lỗi</td><td>&lt; 2%</td></tr>
<tr><td>Độ trễ p95</td><td>&lt; 15 giây (có công cụ)</td></tr>
<tr><td>Chi phí trung bình mỗi thread</td><td>theo trần đã thiết kế</td></tr>
<tr><td>Tỉ lệ 👎</td><td>&lt; 5%</td></tr>
</tbody></table>`,
    },
    {
      id: "insights-engine", h: "Insights và Engine: từ dữ liệu tới cải tiến",
      diagram: "improve-loop", diagramCaption: "Vòng cải tiến hằng tuần",
      html: `<p><strong>Phân tích (Insights)</strong>: gom tin nhắn đầu của các cuộc chat thành chủ đề 2 tầng (embedding + phân cụm, AI đặt tên chủ đề), kèm tỉ lệ lỗi, điểm, chi phí từng chủ đề và so với kỳ trước. Chạy phân tích để làm mới; <strong>Báo cáo hằng tuần T2 08:00</strong> gửi qua Telegram/Email nếu đã nối.</p>
<p><strong>Engine</strong> ("vấn đề lặp lại của agent"): tự gom <em>cụm lỗi</em>, <em>điểm thấp</em>, <em>không hài lòng (👎)</em> thành vấn đề; trạng thái Mới / Đang xử lý / Đã sửa / Bỏ qua. Mỗi vấn đề: Nguyên nhân gốc, Đề xuất sửa (thường là sửa chỉ dẫn), Trace mẫu. <strong>Áp dụng</strong> tạo một commit prompt mới, <em>chưa</em> chạy thật; <strong>Tạo dataset</strong> lưu ca lỗi để kiểm lại; Backtest ở trang Đánh giá để so bản cũ với ứng viên. Engine báo qua Telegram/Email khi có vấn đề mới.</p>
<p>Giới hạn: Engine chỉ đề xuất sửa chỉ dẫn/cấu hình, không sửa mã; chưa có nhóm chủ đề do người dùng tự đặt.</p>`,
    },
    {
      id: "canh-bao", h: "Cảnh báo, tự động hoá và xuất dữ liệu",
      html: `<p>Menu <strong>Tự động hoá</strong> → Cảnh báo → Tạo: điều kiện (lỗi tăng, chi phí vượt, điểm giảm…) và kênh nhận: <code>telegram</code>, <code>email</code>, <code>slack</code>, <code>webhook</code>, <code>email_draft</code> (chỉ tạo bản nháp). Gửi qua hàng đợi outbox, lỗi thì thử lại (1, 5, 15 phút) rồi đánh dấu thất bại. Webhook ra ngoài bắt buộc có khoá ký: header <code>X-AOS-Signature: sha256=&lt;hex&gt;</code> là HMAC-SHA256 trên <code>X-AOS-Timestamp + "." + body</code>; bên nhận phải kiểm chữ ký và thời gian để chống giả mạo, phát lại.</p>
<p>Quy tắc tự động hoá còn đưa trace vào dataset, hàng đợi chú thích, hoặc chấm trực tuyến (buổi 7). Xuất dữ liệu: <strong>Parquet</strong> hằng ngày 02:00 (trace, phản hồi, kết quả đánh giá, chi phí) ra thư mục hoặc S3; <strong>OTLP</strong> ra hệ thống theo dõi khác (buổi 9).</p>`,
    },
  ],
  lab: {
    title: "Lab 6: Điều tra một lời phàn nàn và đóng vòng cải tiến",
    time: "60 phút",
    setup: "Giảng viên đã chạy kịch bản sinh ~200 trace mẫu cho agent CSKH của lớp, trong đó có lỗi và lượt 👎.",
    steps: [
      "Tình huống: \"Khách Lan phàn nàn lúc khoảng 10 giờ sáng nay agent báo sai chính sách đổi hàng\". Tracing → project lớp → <strong>Hỏi bằng lời</strong>: \"thread của khách Lan sáng nay\". Sửa bộ lọc nếu cần, Chạy, <strong>Lưu</strong> bộ lọc.",
      "Mở trace: xem <strong>Tin nhắn</strong>, rồi <strong>Hành trình</strong>; dùng <strong>Ctrl + F</strong> tìm \"đổi\". Ghi lại bước gây sai và nguyên nhân (dữ liệu, công cụ, chỉ dẫn, model).",
      "Giám sát: đọc tỉ lệ lỗi, độ trễ, chi phí 24 giờ; bấm <strong>Môi trường</strong> để so dev và prod.",
      "Phân tích: <strong>Chạy phân tích</strong>; ghi 3 chủ đề nhiều nhất và chủ đề có tỉ lệ lỗi cao nhất.",
      "Engine: <strong>Quét ngay</strong>; mở vấn đề lớn nhất, đọc Nguyên nhân gốc và Đề xuất sửa. Bấm <strong>Tạo dataset</strong>, rồi <strong>Áp dụng</strong> (tạo commit mới). Đổi trạng thái sang Đang xử lý.",
      "Tự động hoá → Cảnh báo → <strong>Tạo</strong>: tỉ lệ lỗi &gt; 5% trong 1 giờ, kênh <code>email_draft</code>. Tạo thêm một cảnh báo <code>webhook</code> trỏ tới máy nhận thử của lớp, ghi lại header chữ ký nhận được.",
      "Viết báo cáo sự cố 5 dòng: hiện tượng, trace, nguyên nhân, đề xuất, cách kiểm lại.",
    ],
    result: "Tìm được trace đúng; có bộ lọc đã lưu; 3 chủ đề Insights; một vấn đề Engine có dataset và commit đề xuất; 2 cảnh báo (bản nháp email và webhook có chữ ký); báo cáo sự cố 5 dòng.",
    shot: "env-compare", shotAlt: "So sánh môi trường trong Giám sát",
    check: ["Học viên chỉ đúng bước gây sai trong trace và phân loại nguyên nhân.", "Engine: vấn đề có dataset liên kết và commit mới (không có phiên bản agent mới, chưa chuyển tag).", "Máy nhận webhook ghi được X-AOS-Signature và X-AOS-Timestamp.", "Báo cáo sự cố đủ 5 ý."],
  },
  pitfalls: [
    ["Không tìm thấy trace của một cuộc chat.", "Sai project hoặc sai môi trường đang lọc; chat riêng tư chỉ chủ và admin thấy (lọc \"Của ai\"); nội dung trace đã mã hoá thì không tìm chữ trực tiếp được."],
    ["Trang Phân tích / Engine trống.", "Chưa đủ dữ liệu chạy; chờ có chat, hoặc chạy lại phân tích."],
    ["Bấm Áp dụng ở Engine mà agent không đổi.", "Đúng thiết kế: Áp dụng chỉ tạo commit. Phải đánh giá rồi chuyển tag (buổi 3, 7)."],
    ["Cảnh báo không tới Telegram/Email.", "Kênh nhận chưa cấu hình thật (máy lab tạo bản nháp outbox). Xem trạng thái outbox và lần thử lại."],
    ["Bên nhận webhook bị giả mạo.", "Bên nhận phải kiểm HMAC và độ lệch thời gian; từ chối khi sai."],
  ],
  quiz: [
    { q: "Kiểu xem trace nào phù hợp nhất để biết chính xác model đã nhận ngữ cảnh gì ở một bước?", options: ["Tin nhắn", "Hành trình", "Chi tiết", "Giám sát"], a: "Chi tiết.", rubric: "1 điểm." },
    { q: "Bấm \"Áp dụng\" trên một vấn đề Engine sẽ:", options: ["Sửa ngay agent đang chạy", "Tạo một commit prompt mới, chưa chạy thật", "Xoá các trace lỗi", "Gửi email cho khách"], a: "Tạo commit prompt mới, chưa đưa vào chạy.", rubric: "1 điểm." },
    { q: "Viết công thức chữ ký webhook của Agent OS và nêu 2 việc bên nhận phải làm.", a: "X-AOS-Signature = \"sha256=\" + HMAC-SHA256(khoá ký, X-AOS-Timestamp + \".\" + body). Bên nhận: tính lại và so sánh an toàn thời gian hằng; kiểm timestamp trong cửa sổ cho phép để chống phát lại.", rubric: "3 điểm: công thức (1), kiểm HMAC (1), kiểm timestamp/chống replay (1)." },
    { q: "Vì sao cần nhãn môi trường cho project?", a: "Để tách số liệu thử nghiệm khỏi số liệu khách thật; cảnh báo, giám sát, đánh giá không bị nhiễu; so sánh được giữa môi trường.", rubric: "2 điểm." },
    { q: "Trình bày 7 bước điều tra một lời phàn nàn bằng trace.", a: "Xác định thời điểm/người/kênh → lọc → xem Tin nhắn → Hành trình → Chi tiết bước bất thường → phân loại nguyên nhân → tạo ví dụ dataset để kiểm lại.", rubric: "3 điểm: ≥ 6 bước đúng thứ tự (3), 4–5 bước (2), ít hơn (1)." },
    { q: "Insights chỉ ra chủ đề \"phí ship tỉnh\" có tỉ lệ lỗi 18%. Đề xuất kế hoạch xử lý trong một tuần.", a: "Lọc trace chủ đề đó, đọc 10 trace, phân loại nguyên nhân (thiếu bảng phí trong gói? công cụ tính phí lỗi?), sửa nguồn (gói ngữ cảnh/công cụ), tạo dataset từ các ca lỗi, chạy thí nghiệm so sánh, qua cổng thì chuyển tag, theo dõi Giám sát và Insights tuần sau.", rubric: "4 điểm: điều tra trace (1), sửa đúng chỗ (1), dataset + thí nghiệm (1), theo dõi sau triển khai (1)." },
  ],
  homework: [
    "Viết quy trình điều tra sự cố (1 trang) cho đội CSKH của công ty, dùng 7 bước của buổi học.",
    "Đề xuất bộ 5 cảnh báo cho một agent sắp ra mắt: điều kiện, ngưỡng, kênh nhận, người xử lý.",
    "Đọc báo cáo Insights tuần của máy lab và viết 3 đề xuất cải tiến.",
  ],
  glossary: [
    ["Trace", "Cây các bước của một run."],
    ["Project", "Nhóm trace, có nhãn môi trường."],
    ["Độ trễ p95", "95% số lượt nhanh hơn giá trị này."],
    ["Insights", "Phân tích chủ đề khách hỏi, kèm lỗi, điểm, chi phí."],
    ["Engine", "Gom lỗi/điểm thấp/👎 thành vấn đề có nguyên nhân và đề xuất sửa."],
    ["Outbox", "Hàng đợi gửi thông báo có thử lại."],
    ["HMAC", "Mã xác thực thông điệp bằng khoá bí mật chung."],
    ["Parquet", "Định dạng tệp dữ liệu dạng cột cho phân tích."],
  ],
  slides: [
    { t: "Trace là gì", pts: ["Cây bước: model, công cụ, duyệt, lỗi", "Thời gian · token · chi phí · metadata", "Gom theo project + môi trường"], shot: "trace-trajectory" },
    { t: "4 kiểu xem", pts: ["Hành trình", "Tin nhắn", "Lượt", "Chi tiết"], note: "Cho lớp đoán: lỗi \"agent trả lời chậm\" xem kiểu nào trước?" },
    { t: "7 bước điều tra", pts: ["Thời điểm, người, kênh", "Lọc / Hỏi bằng lời", "Tin nhắn → Hành trình → Chi tiết", "Phân loại nguyên nhân", "Tạo ví dụ dataset"] },
    { t: "Tìm nhanh", shot: "trace-search", pts: ["Bộ lọc + bộ lọc đã lưu", "Hỏi bằng lời (95% câu mẫu)", "Ctrl+F trong trace"] },
    { t: "Giám sát theo môi trường", shot: "env-compare" },
    { t: "Vòng cải tiến", diagram: "improve-loop" },
    { t: "Insights và Engine", pts: ["Chủ đề 2 tầng + lỗi/điểm/chi phí", "Báo cáo tuần T2 08:00", "Vấn đề → nguyên nhân → đề xuất", "Áp dụng = commit, chưa chạy thật"] },
    { t: "Cảnh báo có chữ ký", code: "X-AOS-Timestamp: 1761360000\nX-AOS-Signature: sha256=HMAC(key, ts + \".\" + body)", pts: ["telegram · email · slack · webhook · email_draft", "Thử lại 1/5/15 phút"] },
    { t: "Lab 6", pts: ["Tìm trace của lời phàn nàn", "Insights · Engine · dataset · commit", "2 cảnh báo", "Báo cáo sự cố 5 dòng"] },
  ],
  plan: [
    { min: 10, act: "Ôn buổi 5", detail: "" },
    { min: 35, act: "Giảng: trace, 4 kiểu xem, 7 bước điều tra, tìm kiếm", detail: "Slide 1–4. Trình diễn một điều tra mẫu từ đầu tới cuối." },
    { min: 25, act: "Giảng: giám sát, vòng cải tiến, Insights, Engine, cảnh báo", detail: "Slide 5–8." },
    { min: 10, act: "Nghỉ giải lao", detail: "" },
    { min: 60, act: "Lab 6", detail: "Học viên làm theo cặp: một người điều tra, một người viết báo cáo; đổi vai ở bước 5." },
    { min: 25, act: "Kiểm tra + chữa báo cáo sự cố", detail: "" },
    { min: 15, act: "Thảo luận: chỉ số nào công ty bạn theo dõi hằng ngày?", detail: "" },
  ],
  ask: [
    "Một agent không có lỗi kỹ thuật nào nhưng khách vẫn không hài lòng. Trace giúp được gì, không giúp được gì?",
    "Bạn có cho phép Engine tự động chuyển tag prod sang bản sửa không? Vì sao?",
  ],
  notes: [
    "Kịch bản sinh trace mẫu phải chạy trước giờ học (xem tài liệu chuẩn bị lab). Kiểm tra có ít nhất 3 trace lỗi và 10 lượt 👎.",
    "Nhấn mạnh tách bạch: Engine đề xuất, con người quyết định qua đánh giá.",
  ],
};
export default m;
