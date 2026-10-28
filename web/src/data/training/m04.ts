import type { Module } from "./types";

const m: Module = {
  n: 4, slug: "chat-nguoi-dung", track: "van-hanh", roles: ["user", "admin", "architect"], hours: 3,
  title: "Agent OS Chat cho người dùng thành thạo",
  short: "Agent OS Chat",
  summary: "Dùng Agent OS Chat như một chuyên gia: giao việc cho đúng agent, đọc file, làm việc theo dự án, Canvas, nghiên cứu sâu, tạo file Office, chỉ dẫn riêng, bộ nhớ, lịch chạy, chia sẻ, quyền dữ liệu của mình, và hướng dẫn lại cho đồng nghiệp.",
  goals: [
    "Hoàn thành một nhiệm vụ nghiệp vụ trọn vẹn trong Chat: đưa file vào, nhận phân tích, xuất ra file Excel/Word, không rời Chat.",
    "Tổ chức công việc bằng Dự án (chỉ dẫn, tệp, bộ nhớ riêng) và giải thích khác biệt với chỉ dẫn riêng và bộ nhớ cá nhân.",
    "Chạy một nghiên cứu sâu có duyệt kế hoạch và tải báo cáo PDF.",
    "Thiết lập chỉ dẫn riêng, bật đăng nhập 2 lớp, xuất dữ liệu của mình.",
    "Hướng dẫn được một đồng nghiệp mới trong 15 phút theo bộ \"10 thao tác cốt lõi\".",
  ],
  prereq: ["Buổi 1–2."],
  sections: [
    {
      id: "giao-dien", h: "Giao diện và 10 thao tác cốt lõi",
      shot: "chat-welcome", shotCaption: "Thanh trái: khu Agent theo phòng ban, khu Cuộc trò chuyện, Dự án",
      html: `<ol>
<li>Chọn agent trong mục <strong>Agent</strong> (theo phòng ban) → gõ vào ô <em>Nhắn cho …</em> → Enter.</li>
<li><strong>Cuộc trò chuyện mới</strong>; tìm lại bằng ô <strong>Tìm cuộc trò chuyện</strong> (gõ không dấu vẫn ra); ghim chat quan trọng.</li>
<li>Gõ <code>@</code> để gọi thêm agent khác vào cuộc; gõ <code>/</code> để dùng lệnh nhanh.</li>
<li>Bấm <strong>+</strong> để đính kèm file (hoặc kéo thả).</li>
<li>Bật <strong>Nghiên cứu sâu</strong> cho đề tài cần tìm hiểu kỹ.</li>
<li>Duyệt thẻ <strong>Cho phép / Sửa / Từ chối</strong> và điền thẻ hỏi thông tin.</li>
<li>Đọc, sửa, so sánh, tải tài liệu trong <strong>Canvas</strong>.</li>
<li>Làm việc trong <strong>Dự án</strong>.</li>
<li>Đặt <strong>chỉ dẫn riêng</strong>, xem <strong>bộ nhớ</strong> trong Cài đặt.</li>
<li>Đánh giá 👍/👎 câu trả lời: dữ liệu này đi vào Engine để cải tiến agent (buổi 6).</li>
</ol>
<p>Đóng tab không làm dừng agent; mở lại sẽ thấy kết quả. Cuộc trò chuyện của ai người nấy thấy, kể cả admin cũng không đọc chat riêng của người khác qua giao diện Chat.</p>`,
    },
    {
      id: "file-va-tinh-toan", h: "Đưa file vào và tính trên dữ liệu thật",
      html: `<p>Đọc được PDF, Word, Excel, CSV, PowerPoint, txt, md, json. Giới hạn: tài liệu tối đa 20 MB, 5 tệp mỗi tin; ảnh tối đa 5 MB. File ngắn được đưa thẳng cho model; file dài được cắt đoạn và chỉ các đoạn liên quan được đưa vào (RAG). Excel/CSV được nạp vào <strong>sandbox</strong> để agent tính bằng Python trên số liệu thật thay vì "đoán" từ chữ. Hệ thống chặn file giả mạo đuôi, file nén độc hại và file Office có macro.</p>
<p><strong>Mẹo của chuyên gia</strong>: yêu cầu agent nêu rõ đã đọc phần nào ("trích dẫn số trang"), và với số liệu thì yêu cầu "tính bằng Python, cho xem bảng trung gian".</p>`,
    },
    {
      id: "du-an-canvas", h: "Dự án và Canvas",
      shot: "project", shotCaption: "Chat trong dự án tự dùng chỉ dẫn và tệp của dự án",
      html: `<p><strong>Dự án</strong> (Dự án → Dự án mới) gom các chat cùng một việc: chỉ dẫn riêng của dự án, tệp chung, bộ nhớ riêng. Trò chuyện mới trong dự án tự dùng tất cả; Cài đặt dự án để sửa; Đóng dự án để quay lại chat thường. Người khác không thấy dự án của bạn.</p>
<p><strong>Canvas</strong> là khung bên cạnh chat cho tài liệu dài. Nút Sửa, So sánh (khác biệt giữa các bản), Sao chép, Tải về, Tải PDF (chữ Việt đúng dấu). Mỗi lần sửa là một phiên bản; chọn lại bản cũ ở ô phiên bản. Agent cần được bật công cụ Canvas.</p>`,
    },
    {
      id: "nghien-cuu-sau", h: "Nghiên cứu sâu và tạo file",
      shot: "deep-research", shotCaption: "Kế hoạch nghiên cứu chờ duyệt trước khi các agent con tìm web",
      html: `<p><strong>Nghiên cứu sâu</strong>: bật nút, gõ đề tài. Agent đưa thẻ <em>Kế hoạch nghiên cứu</em> để Duyệt / Sửa kế hoạch / Từ chối; chưa duyệt thì chưa tìm gì. Sau đó agent con tìm web song song, tiến độ hiện trên màn hình; báo cáo có mục lục, trích dẫn [1] [2] mở trong Canvas, Tải PDF. Giới hạn để kiểm soát chi phí: tối đa 15 lượt tìm, 3 agent con song song, 3 vòng, 10 phút, khoảng 0,30 USD mỗi lần (một lần chạy thật đo được khoảng 0,06 USD). Nếu chưa kịp duyệt, đề nghị nằm trong Hộp duyệt.</p>
<p><strong>Tạo file</strong>: nói "làm bảng doanh thu tháng 10 bằng Excel", "soạn báo giá dạng Word". File hiện thành thẻ trong chat, xem trước được (PDF), tải về; mở bằng Office như file thường. Tạo trong sandbox bằng các skill Word/Excel/PowerPoint/PDF có sẵn. Kiểm thử với model thật: 2 tệp Word và Excel mở được, tiếng Việt đúng, số khớp.</p>`,
    },
    {
      id: "ca-nhan-hoa", h: "Cá nhân hoá, bộ nhớ, lịch chạy, chia sẻ",
      html: `<ul>
<li><strong>Chỉ dẫn riêng</strong>: tên tài khoản → Cài đặt → Cá nhân hoá → tick Dùng chỉ dẫn của tôi; hai ô "Bạn muốn AI biết gì về bạn?" và "Bạn muốn AI trả lời thế nào?" (mỗi ô ≤ 1500 ký tự).</li>
<li><strong>Bộ nhớ</strong>: chip "Em đã ghi nhớ" có nút Quên; Cài đặt → Bộ nhớ để xem, xoá từng mục. Chỉ bạn thấy bộ nhớ của mình.</li>
<li><strong>Lịch chạy</strong>: Cài đặt → Lịch chạy: mẫu hằng ngày, T2–T6, hằng tuần, hằng tháng hoặc cron; hiện giờ chạy kế tiếp; tối đa 48 lần/ngày mỗi lịch.</li>
<li><strong>Chia sẻ</strong>: link chỉ xem, hạn 1/7/30 ngày, thu hồi được; người xem phải đăng nhập cùng workspace; nội dung chia sẻ đã lọc (không tham số công cụ, không tệp).</li>
<li><strong>Voice</strong>: nói chuyện song công, ngắt lời được, giao việc cho agent ngay trong cuộc gọi; model live do admin chọn. (Máy lab dùng AI giả nên không có voice.)</li>
</ul>`,
    },
    {
      id: "bao-mat-ca-nhan", h: "Bảo mật tài khoản và dữ liệu của tôi",
      shot: "my-data", shotCaption: "Thẻ Dữ liệu của tôi: tải zip hoặc xoá dữ liệu của mình",
      html: `<ul>
<li><strong>Đăng nhập 2 lớp</strong>: Cài đặt → Bảo mật → Bật xác thực 2 lớp; quét QR bằng ứng dụng xác thực; nhập mã 6 số; cất 10 mã dự phòng. Có "nhớ thiết bị" 30 ngày; sai 5 lần khoá 15 phút.</li>
<li><strong>Thiết bị đang đăng nhập</strong>: thu hồi máy lạ, Đăng xuất các thiết bị khác.</li>
<li><strong>Dữ liệu của tôi</strong>: Tải dữ liệu của tôi (zip: chat, bộ nhớ, tệp, trace, dự án, canvas, lịch sử đăng nhập; không có mật khẩu hay khoá). Xoá dữ liệu của tôi: gõ lại tên đăng nhập, không hoàn tác, tài khoản bị khoá.</li>
<li><strong>Quy tắc vàng</strong>: không dán mật khẩu, token, khoá API vào chat.</li>
</ul>`,
    },
  ],
  lab: {
    title: "Lab 4: Một ngày làm việc của chuyên viên kinh doanh",
    time: "60 phút",
    setup: "Máy lab chế độ AI giả; tệp mẫu doanh-thu-q3.xlsx và bang-gia.txt (giảng viên phát).",
    steps: [
      "Chat → Dự án → <strong>Dự án mới</strong> \"Khách hàng Minh Phát\". Chỉ dẫn: \"Xưng em, gọi anh. Luôn kèm đơn vị tiền.\" Tải <code>bang-gia.txt</code> vào Tệp.",
      "Trò chuyện mới trong dự án: hỏi giá 2 sản phẩm. Kiểm tra câu trả lời dùng đúng tệp.",
      "Đính kèm <code>doanh-thu-q3.xlsx</code>: \"Tính tổng doanh thu theo chi nhánh, vẽ biểu đồ cột\". Quan sát agent chạy Python trong sandbox.",
      "\"Xuất bảng này ra Excel và soạn tóm tắt dạng Word\". Mở thẻ file, xem trước, tải về.",
      "Yêu cầu một kế hoạch chăm sóc khách hàng 1 trang; mở trong Canvas, sửa một đoạn, So sánh hai phiên bản, Tải PDF.",
      "Bật <strong>Nghiên cứu sâu</strong>: \"Xu hướng thương mại điện tử Việt Nam 2026\". Bấm <strong>Sửa kế hoạch</strong>, bỏ một câu hỏi, rồi Duyệt. Chờ báo cáo, tải PDF.",
      "Cài đặt → Cá nhân hoá: đặt chỉ dẫn riêng. Cài đặt → Lịch chạy: tạo lịch \"T2 08:00 tóm tắt việc tuần\".",
      "Cài đặt → Bảo mật: bật 2 lớp (dùng ứng dụng xác thực trên điện thoại lab), cất mã dự phòng. Dữ liệu của tôi: Tải zip, mở xem có những gì.",
      "Chia sẻ cuộc trò chuyện ở bước 4 bằng link 1 ngày; đồng nghiệp mở link; sau đó thu hồi.",
    ],
    result: "Có dự án với tệp; biểu đồ và file Excel/Word tạo từ dữ liệu thật; tài liệu Canvas có ≥ 2 phiên bản và PDF; một báo cáo nghiên cứu sâu có trích dẫn; lịch chạy; 2 lớp bật; file zip dữ liệu; link chia sẻ đã thu hồi.",
    shot: "file-card", shotAlt: "Thẻ file Word/PDF trong chat",
    check: ["Học viên mở được file Excel tải về và số khớp với dữ liệu.", "Báo cáo nghiên cứu có trích dẫn và kế hoạch đã sửa trước khi duyệt.", "Tài khoản hiện 2FA đã bật trong Thành viên (cột 2FA).", "Link chia sẻ báo hết hiệu lực sau khi thu hồi."],
  },
  pitfalls: [
    ["Agent trả lời sai số liệu từ Excel.", "Agent đang đọc chữ thay vì tính. Yêu cầu \"tính bằng Python\"; kiểm tra agent có bật Python Sandbox."],
    ["Tải file bị từ chối.", "Sai loại, có macro, file nén nguy hiểm hoặc vượt 20 MB/5 tệp. Lưu lại dạng .xlsx/.docx không macro hoặc PDF."],
    ["Agent không biết tệp trong dự án.", "Chat được tạo ngoài dự án. Mở dự án rồi bấm Trò chuyện mới trong dự án."],
    ["Nghiên cứu sâu dừng giữa chừng.", "Chạm giới hạn thời gian/lượt tìm hoặc trần chi phí; xem dải báo và trace. Thu hẹp đề tài."],
    ["Agent nhớ sai một điều.", "Cài đặt → Bộ nhớ → xoá mục đó, hoặc bấm Quên trên chip."],
    ["Mất điện thoại 2 lớp.", "Dùng mã dự phòng; hết mã thì nhờ admin đặt lại 2 lớp."],
  ],
  quiz: [
    { q: "Bạn muốn mọi chat về khách hàng Minh Phát tự biết bảng giá riêng của khách này. Dùng gì?", options: ["Chỉ dẫn riêng trong Cài đặt", "Dự án có tệp bảng giá", "Bộ nhớ dài hạn", "Ghim cuộc trò chuyện"], a: "Dự án có tệp bảng giá.", rubric: "1 điểm." },
    { q: "Giới hạn nào KHÔNG thuộc tính năng Nghiên cứu sâu?", options: ["Tối đa 15 lượt tìm", "Tối đa 10 phút", "Tối đa 3 agent con song song", "Tối đa 5 báo cáo mỗi ngày"], a: "Tối đa 5 báo cáo mỗi ngày (không có giới hạn này).", rubric: "1 điểm." },
    { q: "Vì sao nên yêu cầu agent \"tính bằng Python\" với dữ liệu Excel?", a: "Để agent chạy code trên số liệu thật trong sandbox, cho kết quả chính xác và kiểm chứng được, thay vì model ước đoán từ văn bản.", rubric: "2 điểm: nêu sandbox/tính thật (1), nêu độ chính xác/kiểm chứng (1)." },
    { q: "Người xem link chia sẻ cuộc trò chuyện thấy và không thấy những gì?", a: "Thấy nội dung hội thoại đã lọc; không thấy tham số công cụ, tệp, ID; phải đăng nhập cùng workspace; link hết hạn theo 1/7/30 ngày và thu hồi được.", rubric: "2 điểm: ít nhất 2 ý đúng." },
    { q: "Một đồng nghiệp xoá dữ liệu của mình rồi hỏi \"bản sao lưu còn không?\". Trả lời đúng là gì?", a: "Bản sao lưu cũ vẫn giữ tới khi hết hạn lưu trữ (7 ngày/4 tuần/6 tháng tuỳ loại); thời hạn này cần được quy định theo tư vấn pháp lý.", rubric: "2 điểm." },
    { q: "Soạn \"10 thao tác cốt lõi\" thành một bài hướng dẫn 15 phút cho nhân viên mới: nêu thứ tự và lý do.", a: "Gợi ý: chọn agent và gửi → chat mới/tìm → đính kèm file → duyệt/hỏi lại → Canvas → dự án → nghiên cứu sâu → @ và / → chỉ dẫn riêng/bộ nhớ → 👍👎. Lý do: từ dùng hằng ngày tới nâng cao, an toàn (duyệt) sớm.", rubric: "3 điểm: thứ tự hợp lý từ cơ bản tới nâng cao (1), có thao tác duyệt sớm (1), có lý do sư phạm (1)." },
  ],
  homework: [
    "Hướng dẫn một đồng nghiệp dùng Agent OS Chat trong 15 phút; ghi lại 3 chỗ họ vướng.",
    "Tạo một dự án thật cho công việc của bạn (chỉ với dữ liệu được phép), dùng trong một tuần, ghi chép kết quả.",
    "Đặt một lịch chạy hữu ích và đánh giá sau 3 lần chạy.",
  ],
  glossary: [
    ["Dự án", "Nhóm chat cùng một việc, có chỉ dẫn, tệp, bộ nhớ riêng."],
    ["Canvas", "Khung tài liệu cạnh chat, có phiên bản và tải PDF."],
    ["Nghiên cứu sâu", "Agent lập kế hoạch, agent con tìm web song song, báo cáo có nguồn."],
    ["Chỉ dẫn riêng", "Lời dặn cố định của từng người cho mọi chat."],
    ["Thẻ hỏi thông tin", "Biểu mẫu nhỏ agent đưa ra khi thiếu dữ liệu."],
    ["Link chia sẻ", "Link chỉ xem có hạn dùng, thu hồi được."],
    ["Mã dự phòng", "10 mã dùng một lần khi mất điện thoại 2 lớp."],
  ],
  slides: [
    { t: "10 thao tác cốt lõi", pts: ["Chọn agent · chat mới · tìm · ghim", "@agent · /lệnh · đính kèm", "Duyệt · Canvas · Dự án", "Chỉ dẫn riêng · 👍👎"], shot: "chat-welcome" },
    { t: "File và tính toán", pts: ["PDF, Word, Excel, CSV, PPT…", "≤ 20 MB, ≤ 5 tệp/tin", "Excel/CSV vào sandbox: tính thật", "Chặn macro, file giả mạo"], shot: "chat-code" },
    { t: "Dự án", shot: "project", pts: ["Chỉ dẫn · tệp · bộ nhớ riêng", "Riêng tư theo người"] },
    { t: "Canvas", shot: "canvas-doc", pts: ["Phiên bản · So sánh · Tải PDF"] },
    { t: "Nghiên cứu sâu", shot: "deep-research", pts: ["Duyệt kế hoạch trước", "Agent con song song", "≤ 15 lượt tìm · 10 phút · ~0,30 USD"] },
    { t: "Tạo file Office", shot: "file-card", pts: ["Word, Excel, PowerPoint, PDF", "Tiếng Việt đúng, số khớp"] },
    { t: "Cá nhân hoá và lịch", pts: ["Chỉ dẫn riêng ≤ 1500 ký tự/ô", "Bộ nhớ: xem, quên", "Lịch chạy ≤ 48 lần/ngày", "Link chia sẻ 1/7/30 ngày"] },
    { t: "Bảo mật của tôi", shot: "my-data", pts: ["2 lớp + 10 mã dự phòng", "Thiết bị đang đăng nhập", "Tải / xoá dữ liệu của tôi", "Không dán khoá vào chat"] },
    { t: "Lab 4", pts: ["Dự án + tệp", "Excel → biểu đồ → file", "Canvas → PDF", "Nghiên cứu sâu · 2 lớp · zip · chia sẻ"] },
  ],
  plan: [
    { min: 10, act: "Khởi động", detail: "Hỏi: việc gì bạn làm lặp lại mỗi tuần? Ghi 5 việc lên bảng để dùng làm ví dụ suốt buổi." },
    { min: 30, act: "Giảng + trình diễn: 10 thao tác, file, dự án, Canvas", detail: "Slide 1–4, trình diễn trực tiếp." },
    { min: 20, act: "Giảng + trình diễn: nghiên cứu sâu, tạo file", detail: "Slide 5–6." },
    { min: 10, act: "Nghỉ giải lao", detail: "" },
    { min: 15, act: "Giảng: cá nhân hoá, lịch, chia sẻ, bảo mật của tôi", detail: "Slide 7–8." },
    { min: 60, act: "Lab 4", detail: "Ký nhận 4 điểm kiểm. Học viên nhanh: làm thêm voice nếu máy có model thật." },
    { min: 25, act: "Kiểm tra + thực hành dạy lại", detail: "6 câu; sau đó 2 học viên dạy lại 10 thao tác trong 5 phút." },
    { min: 10, act: "Tổng kết", detail: "" },
  ],
  ask: [
    "Việc nào trong 5 việc lặp lại trên bảng có thể giao cho lịch chạy?",
    "Khi nào nên dùng Dự án, khi nào chỉ dẫn riêng là đủ?",
    "Bạn sẽ giải thích cho nhân viên lớn tuổi về đăng nhập 2 lớp thế nào?",
  ],
  notes: [
    "Buổi này dành nhiều cho người dùng nghiệp vụ; kỹ sư IT có thể làm thêm bài: đọc trace của bước tạo file để thấy skill và sandbox.",
    "Chuẩn bị điện thoại có ứng dụng xác thực cho học viên không có (hoặc dùng ứng dụng xác thực trên máy tính lab).",
  ],
};
export default m;
