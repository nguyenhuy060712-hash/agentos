import type { Module } from "./types";

const m: Module = {
  n: 2, slug: "xay-agent", track: "nen-tang", roles: ["user", "admin", "it", "architect"], hours: 3,
  title: "Xây agent: cấu hình, công cụ, duyệt và agent con",
  short: "Xây agent",
  summary: "Tạo agent bằng trò chuyện hoặc từ mẫu, hiểu từng khối cấu hình, đặt công cụ \"Hỏi trước\", chia việc cho agent con, quản lý phiên bản, chia sẻ, và dùng Studio để tua lại một lượt chạy.",
  goals: [
    "Tạo được một agent bằng trò chuyện trong dưới 10 phút và giải thích từng trường AI đã điền.",
    "Cấu hình đúng 6 khối của agent (Hướng dẫn, Model, Trần chi phí, Khả năng, Công cụ, Kỹ năng/Gói/Sub-agents) cho một yêu cầu nghiệp vụ cho trước.",
    "Thiết kế mức tự chủ cho từng công cụ (tự làm / Hỏi trước / không cấp) theo hậu quả của hành động.",
    "Khôi phục được một phiên bản cũ của agent và tạo được một nhánh mới trong Studio từ một checkpoint.",
    "Chia sẻ agent cho một nhóm người dùng với đúng quyền Được dùng hoặc Được sửa.",
  ],
  prereq: ["Buổi 1: kiến trúc và 12 khái niệm cốt lõi."],
  sections: [
    {
      id: "tao-agent", h: "Ba cách tạo agent",
      shot: "builder-chat", shotCaption: "Tạo bằng trò chuyện: AI hỏi tối đa 5 câu rồi điền sẵn cấu hình để duyệt",
      html: `<ol>
<li><strong>Tạo bằng trò chuyện</strong> (khuyên dùng): Xây dựng → + Agent mới → Tạo bằng trò chuyện. Nhập tên, mô tả "agent này cần làm gì", chọn model, bấm Tạo cấu hình. AI hỏi tối đa 5 câu, luôn có câu <em>"agent được làm gì thay anh"</em> (chỉ đọc, hay được gửi mail, đăng bài…), rồi tự điền chỉ dẫn, công cụ, model, kênh, trần chi phí. Người dùng xem bản tóm tắt, sửa, rồi bấm Tạo hoặc Chạy thử. Hệ thống tự đưa ngày giờ hiện tại vào chỉ dẫn.</li>
<li><strong>Từ mẫu</strong>: Xây dựng → Mẫu. Có 8 mẫu cho doanh nghiệp Việt (chăm sóc khách hàng, bán hàng, kế toán, nhân sự, marketing, pháp chế nội bộ, giáo viên, trợ lý giám đốc) và vài mẫu khác.</li>
<li><strong>Thiết lập thủ công</strong>: cho người đã quen cấu hình.</li>
</ol>
<p>Cách 1 là cách nhanh nhất để có một bản nháp tốt; chuyên gia vẫn phải đọc lại từng trường trước khi đưa vào dùng.</p>`,
    },
    {
      id: "cau-hinh", h: "Sáu khối cấu hình của một agent",
      html: `<table><thead><tr><th>Khối</th><th>Trường chính</th><th>Lời khuyên của chuyên gia</th></tr></thead><tbody>
<tr><td>Hướng dẫn</td><td>Tên, Mô tả, System prompt, Prompt từ mục Prompts, Ghi chú thay đổi</td><td>System prompt ngắn và đo được (vai trò, phạm vi, giới hạn, giọng, định dạng, khi nào chuyển người). Chỉ dẫn dùng chung nhiều agent thì đặt trong Prompts hoặc Context Hub, không chép tay.</td></tr>
<tr><td>Model</td><td>Model, Nhiệt độ, Yêu cầu mỗi phút</td><td>Việc cần chính xác để nhiệt độ thấp. Đặt giới hạn yêu cầu/phút cho agent công khai.</td></tr>
<tr><td>Trần chi phí</td><td>Mỗi ngày / tuần / tháng, Chặn (HTTP 402) hoặc Chỉ cảnh báo</td><td>Agent tiếp khách công khai luôn có trần Chặn theo ngày.</td></tr>
<tr><td>Khả năng</td><td>Lập kế hoạch (to-do), File nháp, Bộ nhớ dài hạn, Python Sandbox</td><td>Chỉ bật cái cần. Python Sandbox chạy code trong gVisor không mạng.</td></tr>
<tr><td>Công cụ</td><td>Danh sách công cụ, cột <strong>Hỏi trước</strong></td><td>Cấp ít nhất có thể. Mọi công cụ ghi hoặc gửi ra ngoài: tick Hỏi trước.</td></tr>
<tr><td>Kỹ năng, Bộ nhớ cố định, Gói ngữ cảnh, Sub-agents</td><td>Skill đã publish, AGENTS.md, <code>gói @ tag</code>, + Thêm sub-agent</td><td>Kiến thức để ở gói ngữ cảnh ghim theo tag (buổi 3); agent con khi có phần việc chuyên môn riêng.</td></tr>
</tbody></table>
<p>Các thẻ của trang agent: <strong>Cấu hình, Thử, Phiên bản, Deployments, Kênh, Danh tính</strong>. Nút góc trên: Chia sẻ, Trace, Studio, Triển khai, Lưu. Mỗi lần Lưu tạo một <strong>phiên bản</strong> bất biến; thẻ Phiên bản có nút Khôi phục.</p>`,
    },
    {
      id: "muc-tu-chu", h: "Mức tự chủ: công cụ, duyệt và hỏi lại",
      html: `<p>Công cụ là "đôi tay" của agent. Model chỉ <em>đề nghị</em> gọi công cụ; máy chủ Agent OS mới thực sự chạy, kiểm quyền và ghi lại. Công cụ có cột Hỏi trước thì run <strong>dừng</strong> (LangGraph interrupt), trạng thái lưu vào checkpoint, và thẻ duyệt xuất hiện trong chat, trong kênh (nút Duyệt/Từ chối; Zalo trả lời <code>duyệt &lt;số&gt;</code>) và trong <strong>Hộp duyệt</strong>. Người duyệt chọn <strong>Cho phép</strong>, <strong>Sửa</strong> (chỉnh tham số rồi chạy) hoặc <strong>Từ chối</strong> (kèm lý do để agent tìm cách khác).</p>
<table><thead><tr><th>Loại hành động</th><th>Ví dụ</th><th>Mức tự chủ</th></tr></thead><tbody>
<tr><td>Đọc trong nhà</td><td>tra đơn, đọc lịch, tìm tài liệu</td><td>Tự làm</td></tr>
<tr><td>Ghi hoàn tác được</td><td>tạo nháp, ghi chú CRM</td><td>Tự làm, có nhật ký</td></tr>
<tr><td>Gửi ra ngoài</td><td>gửi mail, đăng bài, nhắn khách</td><td>Hỏi trước</td></tr>
<tr><td>Tiền, dữ liệu quan trọng</td><td>hoàn tiền, xoá, chạy quảng cáo</td><td>Hỏi trước, người có thẩm quyền</td></tr>
<tr><td>Không bao giờ</td><td>đổi mật khẩu, mở khoá cửa</td><td>Không cấp công cụ</td></tr>
</tbody></table>
<p>Khi thiếu thông tin, agent có thể <strong>hỏi lại</strong> bằng một biểu mẫu nhỏ (số, lựa chọn, ngày, bật/tắt, chữ). Gmail/Outlook gửi thư và Google/Microsoft ghi dữ liệu <em>luôn</em> phải duyệt, không tắt được.</p>`,
    },
    {
      id: "agent-con", h: "Agent con và đội agent",
      diagram: "team", diagramCaption: "Trưởng nhóm lập kế hoạch, chia việc cho agent con; việc chi tiền dừng chờ duyệt",
      html: `<p>Agent con (sub-agent) nhận một phần việc từ agent trưởng nhóm, làm trên ngữ cảnh riêng và chỉ gửi kết quả tóm tắt về. Lợi ích: chuyên môn riêng, ngữ cảnh của trưởng nhóm gọn, chạy song song (Nghiên cứu sâu dùng 3 agent con song song). Chi phí: nhiều lượt gọi model hơn, trace phức tạp hơn. Agent con <strong>kế thừa cấu hình duyệt</strong> của trưởng nhóm.</p>
<p>Quy tắc thiết kế: bắt đầu bằng một agent; tách agent con khi (1) một phần việc cần chỉ dẫn hoặc công cụ khác hẳn, (2) cần song song, hoặc (3) phần việc sinh nhiều chữ làm ngập ngữ cảnh.</p>`,
    },
    {
      id: "phien-ban-studio", h: "Phiên bản, Studio và tua lại",
      diagram: "fork", diagramCaption: "Rẽ nhánh từ một checkpoint: nhánh cũ được giữ nguyên để so sánh",
      html: `<p><strong>Phiên bản agent</strong> giúp quay lại cấu hình cũ. <strong>Studio</strong> giúp quay lại <em>một thời điểm trong một lượt chạy</em>: Dòng thời gian checkpoint liệt kê các mốc; chọn một mốc để chạy lại từ đó, hoặc <strong>Sửa trạng thái rồi chạy</strong>. Studio dùng cơ chế rẽ nhánh gốc của LangGraph (không chép thread), nên nhánh cũ luôn còn; khối Các nhánh có cây nhánh và So sánh hai nhánh đã chọn. Tick "Từng bước" để dừng trước mỗi lượt gọi công cụ; đính kèm tệp TXT, CSV, MD, JSON; khối Bộ nhớ dài hạn để xem, sửa, xoá bộ nhớ (chỉ admin và editor, có nhật ký).</p>
<p>Giới hạn hiện tại: Studio hiển thị dòng thời gian, chưa vẽ sơ đồ nút–cạnh của graph và chưa có trình đặt điểm dừng trước/sau trên giao diện.</p>`,
    },
    {
      id: "chia-se", h: "Chia sẻ, danh tính và trần chi phí của agent",
      shot: "share-group", shotCaption: "Chia sẻ agent cho một nhóm người dùng với quyền Được dùng hoặc Được sửa",
      html: `<ul>
<li><strong>Chia sẻ</strong>: đặt agent riêng tư hoặc cho cả workspace; thêm người hoặc nhóm với quyền <em>Được dùng</em> (chat, chạy, gọi qua MCP) hoặc <em>Được sửa</em>. Quyền này áp cho cả kênh, lịch chạy, triển khai. Mỗi agent có địa chỉ MCP riêng.</li>
<li><strong>Danh tính agent</strong> (thẻ Danh tính): cho agent làm việc bằng tài khoản Google/Microsoft của chủ agent; chỉ chủ hoặc admin bật; có nhật ký. Nhạy cảm, nên có chuyên gia duyệt.</li>
<li><strong>Trần chi phí của agent</strong>: ngày/tuần/tháng; cột Đã dùng; chạm trần thì chat hiện dải báo rõ ràng.</li>
</ul>`,
    },
  ],
  lab: {
    title: "Lab 2: Dựng agent chăm sóc khách có duyệt, agent con, phiên bản và nhánh Studio",
    time: "55 phút",
    setup: "Máy lab chế độ AI giả. Admin lớp đã tạo nhóm người dùng \"Lop-hoc\" chứa mọi học viên.",
    steps: [
      "Xây dựng → + Agent mới → <strong>Tạo bằng trò chuyện</strong>. Tên: \"CSKH Shop ABC – <tên bạn>\". Mô tả: \"Tư vấn size, đổi trả, tra đơn; tạo phiếu đổi khi khách yêu cầu\". Bấm Tạo cấu hình và trả lời các câu hỏi (với câu \"được làm gì thay anh\": tạo phiếu đổi nhưng phải hỏi trước).",
      "Đọc bản tóm tắt, sửa System prompt cho đủ 6 phần (vai trò, nhiệm vụ, bối cảnh, giới hạn, giọng, ví dụ). Bấm <strong>Tạo</strong>.",
      "Thẻ Cấu hình: khối <strong>Công cụ</strong>, đảm bảo mọi công cụ ghi có tick <strong>Hỏi trước</strong>. Khối <strong>Trần chi phí</strong>: Mỗi ngày 0,5 USD, Chặn. Bật <strong>Lập kế hoạch</strong>. Lưu.",
      "<strong>+ Thêm sub-agent</strong> tên \"Người soạn phản hồi\", chỉ dẫn \"viết câu trả lời lịch sự dưới 80 chữ\". Lưu (phiên bản 2).",
      "Thẻ <strong>Thử</strong>: \"Mình muốn đổi size đơn DH1023\". Quan sát kế hoạch, agent con và thẻ duyệt. Bấm <strong>Sửa</strong>, đổi ghi chú phiếu, rồi Cho phép. Lặp lại một lần với <strong>Từ chối</strong> kèm lý do.",
      "Mở <strong>Thêm → Hộp duyệt</strong>, xem Lịch sử.",
      "Bấm <strong>Studio</strong>: chọn thread vừa chạy, chọn một mốc trước bước tạo phiếu, <strong>Sửa trạng thái rồi chạy</strong>. Ở khối Các nhánh, chọn hai nhánh và So sánh.",
      "Thẻ <strong>Phiên bản</strong>: Khôi phục phiên bản 1, rồi khôi phục lại bản mới nhất.",
      "<strong>Chia sẻ</strong>: thêm nhóm \"Lop-hoc\" quyền Được dùng. Một bạn cùng lớp mở Chat và thấy agent của bạn.",
    ],
    result: "Agent có System prompt 6 phần, công cụ ghi đều Hỏi trước, trần 0,5 USD/ngày, 1 agent con; có ít nhất 1 lần Cho phép sau Sửa và 1 lần Từ chối; Studio có 2 nhánh; khôi phục phiên bản thành công; bạn cùng lớp dùng được agent.",
    shot: "studio-timeline", shotAlt: "Dòng thời gian checkpoint trong Studio",
    check: ["Mở cấu hình: cột Hỏi trước đúng cho công cụ ghi; trần ngày Chặn.", "Hộp duyệt → Lịch sử có cả một bản ghi Cho phép (đã sửa) và một Từ chối.", "Studio → Các nhánh có ≥ 2 nhánh; học viên giải thích nhánh mới bắt đầu từ mốc nào.", "Bạn cùng lớp chat được với agent (quyền Được dùng, không sửa được)."],
  },
  pitfalls: [
    ["Agent tự tạo phiếu mà không xin duyệt.", "Công cụ chưa tick Hỏi trước, hoặc đã sửa nhưng chưa Lưu (vẫn chạy phiên bản cũ). Kiểm tra thẻ Phiên bản."],
    ["Bấm Cho phép nhưng run không chạy tiếp.", "Thường do thẻ duyệt thuộc một lượt cũ đã bị thay. Mở Hộp duyệt, duyệt đúng dòng; xem trace để biết run đang chờ interrupt nào."],
    ["Agent con không được gọi.", "Mô tả của agent con quá chung; trưởng nhóm không biết khi nào giao. Viết mô tả nêu rõ \"dùng khi…\"."],
    ["Chi phí tăng mạnh sau khi thêm agent con.", "Mỗi agent con là thêm lượt gọi model. Dùng model nhỏ cho agent con, giới hạn số vòng, đặt trần."],
    ["Người được chia sẻ không thấy agent.", "Chưa thêm đúng nhóm hoặc agent đang để riêng tư; người chỉ có quyền Được dùng thì không thấy nút Lưu."],
  ],
  quiz: [
    { q: "Công cụ nào sau đây NÊN tick \"Hỏi trước\"?", options: ["tra_don (đọc đơn hàng)", "tim_tai_lieu (tìm trong kho tài liệu)", "gui_mail_khach (gửi email cho khách)", "doc_lich (đọc lịch của người dùng)"], a: "gui_mail_khach: gửi ra ngoài, có hậu quả với khách.", rubric: "1 điểm." },
    { q: "Khi người duyệt chọn \"Sửa\" trên thẻ duyệt, điều gì xảy ra?", options: ["Run bị huỷ", "Công cụ chạy với tham số đã chỉnh", "Agent được tạo phiên bản mới", "Thẻ chuyển sang Hộp duyệt của admin"], a: "Công cụ chạy với tham số đã chỉnh, run tiếp tục từ checkpoint.", rubric: "1 điểm." },
    { q: "Phiên bản agent khác nhánh trong Studio thế nào?", a: "Phiên bản là bản lưu của cấu hình agent (chỉ dẫn, công cụ…), khôi phục được. Nhánh Studio là một đường chạy mới của một thread, rẽ ra từ một checkpoint; nhánh cũ giữ nguyên.", rubric: "2 điểm: mỗi khái niệm đúng 1 điểm. Trừ 1 nếu cho rằng rẽ nhánh làm mất nhánh cũ." },
    { q: "Nêu 3 tình huống nên tách agent con và 1 cái giá phải trả.", a: "Phần việc cần chỉ dẫn/công cụ khác; cần chạy song song; phần việc sinh nhiều chữ làm ngập ngữ cảnh. Cái giá: nhiều lượt gọi model hơn (chi phí, độ trễ), trace phức tạp hơn.", rubric: "2 điểm: 3 tình huống (1, cần ≥ 2 đúng), cái giá (1)." },
    { q: "Viết lại System prompt sau cho đo được: \"Bạn là trợ lý tốt, hãy trả lời khách thật chuyên nghiệp và đầy đủ.\"", a: "Ví dụ: \"Em là CSKH của shop ABC. Xưng em, gọi anh/chị. Chỉ trả lời về size, giá, đổi trả, đơn hàng. Tối đa 4 câu, luôn nêu giá và thời gian giao. Không hứa giảm giá. Không chắc thì nói em kiểm tra lại và chuyển nhân viên.\"", rubric: "3 điểm: có vai trò + phạm vi (1), có ràng buộc đo được (độ dài, phải có…) (1), có cách xử lý khi không biết/chuyển người (1)." },
    { q: "Agent công khai trên Zalo cần những cài đặt an toàn tối thiểu nào trong 6 khối cấu hình?", a: "Trần chi phí theo ngày kiểu Chặn; giới hạn yêu cầu/phút; công cụ ít nhất có thể, công cụ ghi Hỏi trước; nhiệt độ thấp; chỉ dẫn có giới hạn và cách chuyển người; không bật Python Sandbox nếu không cần.", rubric: "3 điểm: mỗi ý đúng 0,5 điểm, tối đa 3." },
  ],
  homework: [
    "Chọn một quy trình thật ở công ty, viết bảng mức tự chủ (hành động, ví dụ, tự làm/Hỏi trước/không cấp, ai duyệt).",
    "Viết System prompt 6 phần cho agent đó (tối đa 15 dòng).",
    "Quan sát 5 trace của agent lab: đếm số lượt gọi model mỗi run và ghi chi phí trung bình.",
  ],
  glossary: [
    ["Hỏi trước", "Cờ trên công cụ bắt run dừng chờ người duyệt trước khi chạy."],
    ["Interrupt", "Cơ chế LangGraph dừng graph và lưu checkpoint để chờ dữ liệu từ người."],
    ["Hộp duyệt (Inbox)", "Nơi gom mọi yêu cầu duyệt, có lịch sử."],
    ["Sub-agent", "Agent con nhận một phần việc từ trưởng nhóm."],
    ["Phiên bản agent", "Bản lưu bất biến của cấu hình agent."],
    ["Rẽ nhánh (fork)", "Chạy lại một thread từ một checkpoint cũ thành nhánh mới."],
    ["Danh tính agent", "Agent hành động bằng tài khoản của chủ agent, có kiểm toán."],
    ["HTTP 402", "Mã lỗi khi chạm trần chi phí kiểu Chặn."],
  ],
  slides: [
    { t: "Ba cách tạo agent", pts: ["Tạo bằng trò chuyện (khuyên dùng)", "Từ 8 mẫu theo ngành", "Thiết lập thủ công"], shot: "builder-chat" },
    { t: "Sáu khối cấu hình", pts: ["Hướng dẫn · Model · Trần chi phí", "Khả năng · Công cụ (Hỏi trước)", "Kỹ năng · Gói ngữ cảnh · Sub-agents"], shot: "agent-config" },
    { t: "System prompt đo được", pts: ["Vai trò, nhiệm vụ, bối cảnh", "Giới hạn, giọng điệu, định dạng", "Ví dụ, khi nào chuyển người"], code: "Em là CSKH shop ABC. Xưng em, gọi anh/chị.\nTối đa 4 câu, luôn nêu giá.\nKhông chắc: \"em kiểm tra lại\" và chuyển nhân viên.", note: "Cho cả lớp sửa một prompt xấu trong 5 phút trước khi xem đáp án." },
    { t: "Mức tự chủ theo hậu quả", pts: ["Đọc: tự làm", "Ghi hoàn tác được: tự làm + nhật ký", "Gửi ra ngoài, tiền: Hỏi trước", "Nguy hiểm: không cấp công cụ"] },
    { t: "Thẻ duyệt", pts: ["Cho phép", "Sửa (chỉnh tham số rồi chạy)", "Từ chối (kèm lý do)", "Trong chat, kênh, Hộp duyệt"], shot: "chat-approval" },
    { t: "Agent con", diagram: "team" },
    { t: "Phiên bản và Studio", diagram: "fork", pts: ["Phiên bản: quay lại cấu hình", "Studio: quay lại một bước trong lượt chạy"] },
    { t: "Chia sẻ và an toàn của agent", pts: ["Người / nhóm · Được dùng / Được sửa", "Địa chỉ MCP riêng", "Danh tính agent: cần chuyên gia duyệt", "Trần ngày / tuần / tháng"], shot: "share-group" },
    { t: "Lab 2", pts: ["Tạo bằng trò chuyện", "Hỏi trước + trần + agent con", "Duyệt: Sửa và Từ chối", "Studio rẽ nhánh · khôi phục phiên bản · chia sẻ"] },
  ],
  plan: [
    { min: 10, act: "Ôn buổi 1", detail: "3 câu hỏi nhanh về kiến trúc; kiểm tra bài về nhà (sơ đồ kiến trúc công ty)." },
    { min: 35, act: "Giảng: tạo agent, 6 khối cấu hình, System prompt", detail: "Slide 1–3. Hoạt động: cả lớp sửa một prompt xấu, so với rubric." },
    { min: 25, act: "Giảng: mức tự chủ, duyệt, agent con", detail: "Slide 4–6. Hoạt động nhóm: phân loại 10 hành động vào bảng mức tự chủ." },
    { min: 10, act: "Nghỉ giải lao", detail: "" },
    { min: 15, act: "Giảng + trình diễn: phiên bản, Studio, chia sẻ", detail: "Slide 7–8. Trình diễn rẽ nhánh trên máy lab." },
    { min: 55, act: "Lab 2", detail: "Giảng viên ký nhận 4 điểm kiểm." },
    { min: 20, act: "Kiểm tra", detail: "6 câu hỏi; câu 5–6 chấm theo rubric." },
    { min: 10, act: "Tổng kết, bài về nhà", detail: "" },
  ],
  ask: [
    "Công ty anh chị có hành động nào AI tuyệt đối không được làm? Ta chặn nó ở đâu: chỉ dẫn, công cụ hay máy chủ?",
    "Nếu người duyệt bấm Cho phép 100% yêu cầu trong tháng, điều đó nói gì về thiết kế?",
    "Khi nào một agent con làm hệ thống tệ đi?",
  ],
  notes: [
    "Nhấn mạnh: chặn bằng chỉ dẫn là yếu nhất; không cấp công cụ là mạnh nhất; Hỏi trước nằm giữa.",
    "Bước Studio hay làm học viên lúng túng: chiếu mẫu một lần trước khi cho làm.",
    "Với lớp đông, ghép cặp ở bước chia sẻ để tiết kiệm thời gian.",
  ],
};
export default m;
