import type { Module } from "./types";

const m: Module = {
  n: 8, slug: "quan-tri-chi-phi", track: "van-hanh", roles: ["admin", "it", "architect"], hours: 3,
  title: "Quản trị, chi phí và LLM Gateway",
  short: "Quản trị và chi phí",
  summary: "Quản lý người dùng, vai trò, nhóm, nhật ký kiểm toán, khoá API; thiết kế trần chi phí nhiều tầng; đọc báo cáo chi phí; định tuyến model, dự phòng, chính sách theo khách; ước tính và tối ưu chi phí AI cho một hệ thống agent.",
  goals: [
    "Thiết lập cấu trúc workspace – vai trò – nhóm cho một công ty 3 phòng ban theo nguyên tắc quyền tối thiểu.",
    "Thiết kế trần chi phí 3 tầng (nhà cung cấp/workspace, tính năng/người, agent) với hành động chặn hoặc cảnh báo phù hợp.",
    "Tính được chi phí ước tính mỗi cuộc chat và mỗi tháng từ token và bảng giá, và chỉ ra 3 đòn bẩy giảm chi phí lớn nhất.",
    "Cấu hình định tuyến model tự động và chính sách theo khách ở Gateway; đọc báo cáo chi phí theo người, agent, model.",
  ],
  prereq: ["Buổi 1–2; buổi 6 (giám sát)."],
  sections: [
    {
      id: "nguoi-dung", h: "Người dùng, vai trò, nhóm, kiểm toán",
      shot: "groups", shotCaption: "Khối Nhóm người dùng trong Thành viên & quyền",
      html: `<p><strong>Thành viên &amp; quyền</strong> (chỉ admin): Người dùng (Khoá, Mở khoá, Xoá), Thêm người dùng, Workspace, Phân quyền thành viên, Nhóm người dùng, Nhật ký kiểm toán, Bí mật, Mã hoá dữ liệu khi lưu, Che dữ liệu cá nhân, Xuất Parquet, Xuất trace OTLP. <strong>Cài đặt → Thành viên</strong>: Mời; cột 2FA; đặt lại 2 lớp; bật Bắt buộc 2 lớp cho workspace.</p>
<table><thead><tr><th>Vai trò</th><th>Làm được</th></tr></thead><tbody>
<tr><td>admin</td><td>Mọi thứ trong workspace, gồm người dùng, bí mật, mã hoá, che dữ liệu, cài đặt máy chủ.</td></tr>
<tr><td>editor</td><td>Tạo/sửa agent, prompt, gói, dataset, giám khảo; sửa bộ nhớ agent; không quản trị người dùng.</td></tr>
<tr><td>viewer</td><td>Xem và dùng; không sửa cấu hình. Tài khoản demo luôn là viewer.</td></tr>
</tbody></table>
<p>Nguyên tắc: <strong>quyền tối thiểu</strong>; chia sẻ agent qua <strong>nhóm</strong> thay vì từng người; mỗi người một tài khoản (không dùng chung); xem nhật ký kiểm toán hằng tháng; khoá ngay tài khoản người nghỉ việc và thu hồi phiên.</p>
<p><strong>Khoá API</strong>: Cài đặt → API key → + API Key; nhóm Cá nhân, Dịch vụ, Key chỉ ghi trace, REST API; Vô hiệu hoá để thu hồi. Khoá cá nhân <code>aos_…</code> gọi API với đúng quyền của chủ khoá; khoá nhận trace <code>ak_…</code> chỉ gọi được cổng nhận trace. Khoá ảo của Gateway (Khoá Gateway) có ngân sách tháng riêng.</p>`,
    },
    {
      id: "chi-phi-tinh", h: "Chi phí AI được tính thế nào",
      html: `<p>Mỗi lần gọi model đi qua <strong>LLM Gateway</strong> (middleware bọc mọi lượt gọi model), được ghi: nhà cung cấp, model, token vào/ra, token suy luận, token cache, chi phí, người, agent, tính năng (chat, kênh nhắn tin, giám khảo, playground, trợ lý, nghiên cứu sâu…).</p>
<p><strong>Công thức</strong>: chi phí = token vào × giá vào + token ra × giá ra (+ suy luận, − phần cache theo giá riêng). Bảng giá model: LLM Gateway → Bảng giá model → Cập nhật bảng giá; <strong>giá mới chỉ áp dụng khi admin duyệt</strong>.</p>
<p><strong>Ví dụ ước tính</strong> (số giả định để luyện tính): một thread CSKH 6 lượt, mỗi lượt 3.000 token vào và 200 token ra; giá 0,10 USD/1 triệu token vào, 0,40 USD/1 triệu token ra → 6 × (3.000 × 0,10 + 200 × 0,40) / 1.000.000 ≈ 0,0023 USD/thread; 300 thread/ngày ≈ 0,68 USD/ngày ≈ 21 USD/tháng. Nhân lên nhanh khi: model đắt hơn hàng chục lần, ngữ cảnh dài (nhồi tài liệu), nhiều vòng công cụ, agent con, giám khảo AI chấm mọi lượt.</p>
<p><strong>Đòn bẩy giảm chi phí</strong> theo thứ tự hiệu quả: (1) model vừa sức + định tuyến; (2) gọn ngữ cảnh (gói/Skill/RAG thay vì nhồi prompt); (3) giới hạn vòng lặp và agent con; (4) lấy mẫu khi chấm online; (5) trần chi phí để chặn sự cố.</p>`,
    },
    {
      id: "tran", h: "Trần chi phí nhiều tầng",
      shot: "agent-cap", shotCaption: "Trần chi phí của một agent: ngày, tuần, tháng; chặn hoặc cảnh báo",
      html: `<table><thead><tr><th>Tầng</th><th>Đặt ở đâu</th><th>Phạm vi</th></tr></thead><tbody>
<tr><td>Công ty</td><td>LLM Gateway → TRẦN CHI PHÍ → Thêm trần</td><td>nhà cung cấp, workspace, từng người, từng giám khảo, từng tính năng; chu kỳ ngày/tuần/tháng; chặn hoặc chỉ cảnh báo; cột Đã dùng / Giới hạn</td></tr>
<tr><td>Agent</td><td>Cấu hình agent → Trần chi phí</td><td>Mỗi ngày / tuần / tháng; Chặn (HTTP 402) hoặc Chỉ cảnh báo</td></tr>
<tr><td>Khoá Gateway</td><td>Khoá Gateway</td><td>ngân sách tháng của khoá ảo dùng từ ứng dụng ngoài</td></tr>
<tr><td>Tính năng có sẵn</td><td>Thiết kế sẵn</td><td>Nghiên cứu sâu ~0,30 USD/lần; Trợ lý Agent OS có trần riêng</td></tr>
</tbody></table>
<p>Khi chạm trần kiểu chặn, Chat hiện dải báo rõ ràng (đúng ngôn ngữ người dùng). Thiết kế gợi ý: trần <em>chặn</em> theo ngày cho agent công khai; trần <em>cảnh báo</em> theo tháng cho workspace ở mức 80% ngân sách; trần riêng cho phạm vi giám khảo; cảnh báo (buổi 6) khi chi phí/giờ tăng bất thường.</p>`,
    },
    {
      id: "dinh-tuyen", h: "Định tuyến, dự phòng, chính sách theo khách",
      shot: "gw-routing", shotCaption: "LLM Gateway: định tuyến model tự động",
      html: `<ul>
<li><strong>Định tuyến tự động</strong> (<code>model: "auto"</code>): theo luật (độ dài, từ khoá, loại việc) hoặc theo một <em>giám khảo quyết định</em> phân loại độ khó; câu dễ đi model rẻ, câu khó đi model mạnh.</li>
<li><strong>Dự phòng</strong>: nhà cung cấp lỗi (ví dụ 503) hoặc hết giờ chờ thì thử lại/chuyển model dự phòng; model treo thì tạm nghỉ.</li>
<li><strong>Giới hạn yêu cầu/phút</strong> theo agent và theo khoá.</li>
<li><strong>Chính sách theo khách</strong>: header <code>X-Gateway-*</code> (và <code>X-Gateway-Metadata</code> dạng JSON) nhận diện khách hàng của bạn khi bạn bán dịch vụ trên Agent OS: trần, model được phép, luật riêng từng khách; chính sách header theo từng khoá.</li>
<li><strong>Che dữ liệu cá nhân</strong> ở Gateway trước khi gửi model (buổi 10).</li>
<li>Gateway tương thích OpenAI: <code>POST /v1/chat/completions</code>, <code>/v1/embeddings</code>, <code>GET /v1/models</code> với khoá ảo <code>vk-…</code> (buổi 9).</li>
</ul>`,
    },
  ],
  lab: {
    title: "Lab 8: Thiết kế quyền và ngân sách cho công ty ABC",
    time: "55 phút",
    setup: "Tài khoản admin của workspace lab riêng mỗi nhóm (giảng viên cấp).",
    steps: [
      "Thành viên &amp; quyền: tạo 3 nhóm \"Ban-giam-doc\", \"CSKH\", \"Ke-toan\"; thêm 2 tài khoản thử vào mỗi nhóm với vai trò phù hợp (viết lý do).",
      "Chia sẻ agent CSKH cho nhóm CSKH quyền Được dùng, cho Ban-giam-doc quyền Được sửa. Đăng nhập bằng một tài khoản CSKH để kiểm tra.",
      "LLM Gateway → TRẦN CHI PHÍ → Thêm trần: (a) workspace 30 USD/tháng, chỉ cảnh báo; (b) phạm vi giám khảo 3 USD/tháng, chặn; (c) tính năng Kênh nhắn tin 1 USD/ngày, chặn.",
      "Agent CSKH: trần 0,02 USD/ngày, Chặn. Chat nhiều lượt tới khi chạm trần; chụp dải báo; xem cột Đã dùng.",
      "Xem báo cáo chi phí <strong>Theo người dùng</strong>; xác định tính năng và model tốn nhất.",
      "Bảng giá model: bấm Cập nhật bảng giá, xem bản đề xuất (không duyệt nếu giảng viên không yêu cầu).",
      "Cấu hình một luật định tuyến: câu dưới 200 ký tự → model nhỏ; còn lại → model mặc định. Thử 2 câu, xem trace ghi model nào.",
      "Tính ước chi phí tháng cho 500 thread/ngày theo số token đo được ở bước 4, so với trần (a).",
      "Thành viên &amp; quyền → Nhật ký kiểm toán: tìm các thao tác bạn vừa làm.",
    ],
    result: "3 nhóm có vai trò hợp lý; 4 trần ở 3 tầng; thấy dải báo chạm trần; luật định tuyến thể hiện trong trace; bảng ước tính chi phí tháng; nhật ký kiểm toán có thao tác của mình.",
    shot: "cap-banner", shotAlt: "Dải báo khi chạm trần chi phí",
    check: ["Không ai trong nhóm CSKH có vai trò admin.", "Trần giám khảo và kênh ở chế độ chặn; trần workspace chỉ cảnh báo.", "Trace của 2 câu thử ghi 2 model khác nhau.", "Bảng ước tính có công thức token × giá và so với trần."],
  },
  pitfalls: [
    ["Agent báo chạm trần, không trả lời.", "Trần agent hoặc trần công ty kiểu chặn. Xem dải báo và LLM Gateway; tăng trần hoặc chờ kỳ mới."],
    ["Chi phí tăng đột biến trong đêm.", "Vòng lặp công cụ, lịch chạy dày, hoặc chấm online mọi lượt. Lọc trace theo chi phí cao nhất (query_runs sắp xếp tốn nhất), đặt trần chặn theo giờ/ngày."],
    ["Giá trong báo cáo không khớp hoá đơn nhà cung cấp.", "Bảng giá chưa cập nhật hoặc chưa được duyệt; token cache/suy luận tính giá riêng."],
    ["Người nghỉ việc vẫn gọi được API.", "Khoá API cá nhân chưa vô hiệu hoá. Khoá tài khoản, thu hồi phiên, vô hiệu hoá khoá."],
    ["Định tuyến gửi câu khó sang model rẻ.", "Luật quá đơn giản; dùng giám khảo quyết định phân loại, theo dõi điểm theo model."],
  ],
  quiz: [
    { q: "Vai trò nào đủ để tạo và sửa agent nhưng không quản lý người dùng?", options: ["admin", "editor", "viewer", "demo"], a: "editor.", rubric: "1 điểm." },
    { q: "Giá model mới lấy về từ \"Cập nhật bảng giá\" có hiệu lực khi nào?", options: ["Ngay lập tức", "Khi admin duyệt", "Đầu tháng sau", "Khi khởi động lại máy chủ"], a: "Khi admin duyệt.", rubric: "1 điểm." },
    { q: "Tính chi phí tháng: 400 thread/ngày, mỗi thread 5 lượt, mỗi lượt 2.500 token vào và 300 token ra, giá 0,10 và 0,40 USD/1 triệu token. (30 ngày)", a: "Mỗi lượt: 2.500×0,10 + 300×0,40 = 250 + 120 = 370 /1e6 = 0,00037 USD. Mỗi thread: 0,00185 USD. Mỗi ngày: 0,74 USD. Tháng: 22,2 USD.", rubric: "3 điểm: công thức đúng (1), tính đúng mỗi lượt/thread (1), kết quả tháng ≈ 22,2 USD (1)." },
    { q: "Thiết kế trần cho một agent công khai trên Zalo phục vụ 1.000 khách/ngày. Nêu tầng, giá trị tương đối, hành động.", a: "Agent: trần ngày kiểu chặn ≈ 1,5–2 lần chi phí trung bình/ngày; tính năng kênh: trần ngày chặn; workspace: trần tháng cảnh báo 80%; cảnh báo chi phí/giờ bất thường; giới hạn yêu cầu/phút.", rubric: "3 điểm: có ≥ 2 tầng (1), đúng chặn/cảnh báo (1), có giá trị tương đối hợp lý hoặc cảnh báo bất thường (1)." },
    { q: "Liệt kê 3 đòn bẩy giảm chi phí lớn nhất và giải thích ngắn.", a: "Model vừa sức + định tuyến; gọn ngữ cảnh (gói/Skill/RAG); giới hạn vòng lặp/agent con/chấm online lấy mẫu.", rubric: "3 điểm: mỗi đòn bẩy đúng 1 điểm." },
    { q: "Header X-Gateway-* dùng khi nào?", a: "Khi một ứng dụng phục vụ nhiều khách hàng gọi Gateway: gắn nhận diện khách để áp chính sách (trần, model được phép, luật) và báo cáo theo khách.", rubric: "2 điểm." },
  ],
  homework: [
    "Viết ma trận quyền (nhóm × vai trò × agent được dùng/sửa) cho công ty bạn.",
    "Lập bảng ngân sách AI tháng cho 3 agent dự kiến: ước tính token, giá, trần từng tầng.",
    "Viết quy trình nghỉ việc (offboarding) cho tài khoản Agent OS: 5 bước.",
  ],
  glossary: [
    ["LLM Gateway", "Cổng chung mọi lượt gọi model: trần, che dữ liệu, định tuyến, ghi chi phí."],
    ["Quyền tối thiểu", "Chỉ cấp quyền cần cho công việc."],
    ["Nhật ký kiểm toán", "Ghi ai làm gì, lúc nào."],
    ["Khoá ảo (vk-)", "Khoá Gateway có ngân sách riêng cho ứng dụng ngoài."],
    ["Token cache", "Phần ngữ cảnh lặp lại được nhà cung cấp tính giá rẻ hơn."],
    ["Định tuyến", "Chọn model theo loại/độ khó câu hỏi."],
    ["Dự phòng (fallback)", "Chuyển model khác khi nhà cung cấp lỗi."],
  ],
  slides: [
    { t: "Vai trò và nhóm", pts: ["admin · editor · viewer", "Chia sẻ qua nhóm", "Một người một tài khoản", "Nhật ký kiểm toán hằng tháng"], shot: "groups" },
    { t: "Khoá API", pts: ["aos_ cá nhân: quyền của chủ khoá", "ak_ nhận trace: chỉ ghi", "vk- Gateway: ngân sách riêng", "Vô hiệu hoá khi nghỉ việc"] },
    { t: "Công thức chi phí", code: "chi phí = token_vào × giá_vào\n        + token_ra × giá_ra\n        (+ suy luận, cache)", pts: ["Giá mới chỉ áp dụng khi admin duyệt"], note: "Cho lớp tính ví dụ 0,0023 USD/thread trước khi chiếu đáp án." },
    { t: "Vì sao hoá đơn tăng", pts: ["Model đắt ×10–×30", "Ngữ cảnh dài", "Vòng công cụ, agent con", "Giám khảo AI chấm mọi lượt"] },
    { t: "Trần 3 tầng", shot: "agent-cap", pts: ["Công ty: nhà cung cấp, người, giám khảo, tính năng", "Agent: ngày/tuần/tháng", "Khoá Gateway: ngân sách tháng"] },
    { t: "Khi chạm trần", shot: "cap-banner" },
    { t: "Định tuyến và chính sách", shot: "gw-routing", pts: ["model: \"auto\" theo luật / giám khảo", "Dự phòng khi 503", "X-Gateway-* theo khách"] },
    { t: "Lab 8", pts: ["Nhóm + vai trò + chia sẻ", "4 trần, chạm trần", "Báo cáo chi phí · định tuyến", "Ước tính tháng · nhật ký kiểm toán"] },
  ],
  plan: [
    { min: 10, act: "Ôn buổi 7", detail: "" },
    { min: 25, act: "Giảng: người dùng, vai trò, nhóm, khoá", detail: "Slide 1–2." },
    { min: 35, act: "Giảng: chi phí, trần, định tuyến", detail: "Slide 3–7. Bài tính nhẩm theo cặp." },
    { min: 10, act: "Nghỉ giải lao", detail: "" },
    { min: 55, act: "Lab 8", detail: "" },
    { min: 25, act: "Kiểm tra", detail: "" },
    { min: 20, act: "Thảo luận ngân sách thật", detail: "Mỗi nhóm trình bày bảng ngân sách 3 phút." },
  ],
  ask: [
    "Ai trong công ty nên được quyền tăng trần chi phí?",
    "Bạn sẽ báo cáo chi phí AI cho ban giám đốc theo đơn vị nào: theo người, theo agent hay theo việc hoàn thành?",
  ],
  notes: [
    "Bài tính chi phí là chỗ học viên nghiệp vụ hay ngại; làm mẫu trên bảng, dùng số tròn.",
    "Không duyệt bảng giá thật trên máy lab trừ khi đã thống nhất.",
  ],
};
export default m;
