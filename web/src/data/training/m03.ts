import type { Module } from "./types";

const m: Module = {
  n: 3, slug: "prompt-context", track: "van-hanh", roles: ["user", "admin", "it", "architect"], hours: 3,
  title: "Prompt, Context Hub, Skill và Playground",
  short: "Prompt và Context Hub",
  summary: "Thiết kế ngữ cảnh cho agent: quản lý prompt và gói ngữ cảnh có phiên bản, tag, so khác, quay lại; viết lại prompt bằng Prompt Canvas; thử nhanh bằng Playground với công cụ, đầu ra có cấu trúc, ảnh và so sánh model.",
  goals: [
    "Giải thích được cửa sổ ngữ cảnh và quyết định đặt mỗi loại kiến thức vào đúng chỗ (system prompt, Prompts, gói ngữ cảnh, Skill, bộ nhớ, dự án, RAG).",
    "Tạo một gói ngữ cảnh, commit 2 bản, gắn tag prod, ghim vào agent và chuyển tag để mọi agent dùng bản mới mà không sửa từng agent.",
    "Dùng Prompt Canvas viết lại một prompt, nhận/bỏ từng thay đổi và lưu thành commit mới.",
    "Trong Playground: kiểm tra đầu ra theo JSON schema, thử gọi công cụ giả lập, so sánh 2–4 cấu hình model trên cùng một câu.",
  ],
  prereq: ["Buổi 2: cấu hình agent, System prompt 6 phần."],
  sections: [
    {
      id: "thiet-ke-ngu-canh", h: "Thiết kế ngữ cảnh (context engineering)",
      diagram: "context-desk", diagramCaption: "Mỗi lượt, hệ thống chọn những gì bày lên \"mặt bàn\" của model",
      html: `<p>Model chỉ thấy những gì có trong <strong>cửa sổ ngữ cảnh</strong> của lượt gọi đó. Nhồi nhiều thì đắt, chậm và model dễ bỏ sót; thiếu thì model đoán. Thiết kế ngữ cảnh là chọn đúng thứ cần bày cho từng lượt.</p>
<table><thead><tr><th>Loại kiến thức</th><th>Đặt ở đâu</th><th>Vì sao</th></tr></thead><tbody>
<tr><td>Vai trò, giới hạn, giọng của một agent</td><td>System prompt</td><td>Ngắn, luôn có mặt.</td></tr>
<tr><td>Chỉ dẫn dùng chung nhiều agent</td><td>Prompts (theo tag) hoặc gói ngữ cảnh</td><td>Một nguồn, có phiên bản.</td></tr>
<tr><td>Quy trình, chính sách, kiến thức nghiệp vụ</td><td>Gói ngữ cảnh: AGENTS.md + tệp</td><td>Gắn vào hệ thống file ảo của agent, chỉ đọc.</td></tr>
<tr><td>Cách làm một việc dài, ít khi cần</td><td>Skill trong gói</td><td>Agent chỉ mở khi cần (progressive disclosure).</td></tr>
<tr><td>Sở thích từng người</td><td>Chỉ dẫn riêng, bộ nhớ dài hạn</td><td>Tách theo người.</td></tr>
<tr><td>Tài liệu của một vụ việc</td><td>Tệp trong Dự án (Chat)</td><td>Áp cho mọi chat trong dự án.</td></tr>
<tr><td>Kho tài liệu lớn</td><td>Tải vào chat / kho tài liệu (RAG, pgvector)</td><td>Chỉ lấy vài đoạn liên quan.</td></tr>
</tbody></table>`,
    },
    {
      id: "prompt-hub", h: "Prompt Hub: commit, tag, webhook",
      shot: "prompt-commit", shotCaption: "Prompt có lịch sử commit và tag",
      html: `<p>Menu <strong>Prompts</strong> lưu mẫu prompt. Mỗi lần sửa là một <strong>commit</strong>; bản tốt gắn <strong>tag</strong> (ví dụ <code>production</code>). Agent lấy prompt theo tag ở ô "Prompt từ mục Prompts", nên chuyển tag là đổi hành vi agent, quay lại cũng chỉ là chuyển tag về commit cũ. Lọc: Tất cả / Của tôi / Đã gắn tag / Chưa gắn tag. Nút <strong>Webhook</strong> báo ra ngoài khi prompt đổi (thư ký thay đổi, CI). Biến trong prompt dùng dạng <code>{{ten_bien}}</code>; chưa hỗ trợ mẫu f-string/jinja.</p>`,
    },
    {
      id: "prompt-canvas", h: "Prompt Canvas: viết lại bằng chat",
      shot: "prompt-canvas", shotCaption: "Prompt Canvas: nút nhanh, khác biệt từng chữ, nhận hoặc bỏ từng chỗ",
      html: `<p>Mở một prompt → <strong>Viết lại bằng AI</strong>. Có nút nhanh: Ngắn gọn hơn, Thêm ví dụ, Đổi giọng điệu, Rõ ràng hơn, Dịch VI↔EN, Thêm ràng buộc an toàn, Sửa theo phản hồi. Bôi đen một đoạn để chỉ sửa đoạn đó. Kết quả hiện <strong>khác biệt từng chữ</strong>; nhận hoặc bỏ từng chỗ, hoặc Chấp nhận tất cả / Từ chối tất cả; <strong>Áp dụng phần đã chấp nhận</strong> rồi <strong>Lưu commit</strong>. Chuyên gia luôn chạy thí nghiệm (buổi 7) trước khi chuyển tag sang commit mới.</p>`,
    },
    {
      id: "context-hub", h: "Context Hub: gói ngữ cảnh có phiên bản",
      shot: "pack-history", shotCaption: "Lịch sử commit và tag của một gói ngữ cảnh",
      html: `<p>Một <strong>gói</strong> gồm AGENTS.md (luôn được đưa cho agent), Skill, mẫu prompt và chính sách. Tạo bằng <strong>+ Gói</strong> hoặc <strong>Nhập từ zip</strong> (cả thư mục). Mở gói để sửa, <strong>Commit…</strong> để lưu, <strong>So khác</strong> giữa hai bản, <strong>Chuyển về đây</strong> để quay lại, <strong>Bỏ thay đổi</strong> để huỷ phần đang sửa. Gắn tag như <code>prod</code>, <code>staging</code>.</p>
<p>Trong cấu hình agent, khối <strong>Gói ngữ cảnh</strong>: chọn <code>gói @ tag</code> (gọi là <em>ghim</em>). Khi tag <code>prod</code> chuyển sang commit mới, mọi agent ghim <code>@prod</code> dùng bản mới <strong>ở lượt sau</strong>. Gói được gắn vào hệ thống file ảo của agent ở chế độ <strong>chỉ đọc</strong> (theo mẫu ContextHubBackend của Deep Agents); agent không tự sửa được gói. Trợ lý Agent OS và MCP <code>/mcp/aos</code> đọc được gói qua công cụ <code>get_pack</code>, <code>list_pack_versions</code>.</p>
<p><strong>Skill</strong>: thư mục có <code>SKILL.md</code> mô tả khi nào dùng và các bước; agent chỉ đọc nội dung khi cần. Có sẵn skill tạo Word, Excel, PowerPoint, PDF. Skill đã publish hiện trong khối Kỹ năng để tick; khi lưu, hệ thống ghim phiên bản hiện tại.</p>`,
    },
    {
      id: "playground", h: "Playground: thử nhanh, không đụng agent thật",
      shot: "pg-compare", shotCaption: "So sánh hai cấu hình model cạnh nhau trên cùng một câu",
      html: `<ul>
<li><strong>Công cụ</strong>: khai báo công cụ, xem model gọi thế nào; kết quả công cụ là giả lập.</li>
<li><strong>Schema đầu ra</strong>: bắt model trả đúng mẫu JSON và kiểm tra hợp lệ (dùng <code>with_structured_output</code> của LangChain).</li>
<li><strong>Ảnh</strong>: gửi ảnh kèm câu hỏi.</li>
<li><strong>Cấu hình model</strong>: lưu 1–4 cấu hình (model, nhiệt độ, mức suy luận) rồi So sánh cạnh nhau: chất lượng, thời gian, token, chi phí.</li>
<li><strong>Nạp dataset</strong> rồi <strong>Chạy tất cả</strong> để thử trên cả bộ câu.</li>
</ul>
<p>Trên máy lab dùng AI giả, có thể "điều khiển" model giả bằng nhãn trong câu: <code>[json]</code> trả JSON hợp lệ, <code>[json-invalid]</code> trả JSON sai để thấy lỗi kiểm tra, <code>[tool:ten_cong_cu]</code> buộc gọi một công cụ. Nhãn này chỉ có tác dụng với AI giả trên máy lab.</p>`,
    },
  ],
  lab: {
    title: "Lab 3: Một nguồn chính sách cho nhiều agent",
    time: "55 phút",
    setup: "Agent CSKH của Lab 2. Máy lab chế độ AI giả.",
    steps: [
      "Context Hub → <strong>+ Gói</strong> tên <code>cskh-&lt;tên&gt;</code>. Trong AGENTS.md viết: thông tin shop, giọng điệu, chính sách đổi trả 7 ngày. <strong>Commit…</strong> (ghi chú \"bản đầu\"), gắn tag <code>prod</code>.",
      "Cấu hình agent CSKH → khối <strong>Gói ngữ cảnh</strong>: chọn <code>cskh-&lt;tên&gt; @ prod</code>. Lưu. Thẻ Thử: hỏi \"đổi hàng mấy ngày?\". Mở trace, tìm chỗ nội dung gói được đưa vào.",
      "Sửa AGENTS.md: đổi thành 10 ngày. Commit. Bấm <strong>So khác</strong> giữa hai bản. Chuyển tag <code>prod</code> sang commit mới. Hỏi lại agent (không sửa agent).",
      "Chuyển tag <code>prod</code> về commit cũ (rollback) và kiểm tra agent trả lời 7 ngày.",
      "Prompts → tạo prompt \"cskh-chao-hoi\" với một lời chào dài dòng. Mở <strong>Viết lại bằng AI</strong>, bấm <strong>Ngắn gọn hơn</strong> rồi <strong>Thêm ràng buộc an toàn</strong>. Nhận một chỗ, bỏ một chỗ. Áp dụng, Lưu commit.",
      "Playground: khai báo schema JSON <code>{\"y_dinh\": string, \"ma_don\": string}</code>. Gửi \"[json] khách muốn đổi đơn DH1023\" rồi \"[json-invalid] …\" và đọc kết quả kiểm tra.",
      "Playground: lưu 2 cấu hình model (nhiệt độ 0,2 và 0,9), bấm So sánh trên cùng một câu, ghi lại khác biệt thời gian và token.",
    ],
    result: "Agent đổi câu trả lời 7 → 10 → 7 ngày chỉ bằng chuyển tag; prompt có commit mới tạo từ Canvas; Playground hiện một lần kiểm schema đạt và một lần không đạt; có bảng so sánh 2 cấu hình.",
    shot: "pack-diff", shotAlt: "So khác giữa hai bản của gói ngữ cảnh",
    check: ["Lịch sử gói có ≥ 2 commit và lịch sử tag thể hiện prod chuyển đi rồi chuyển về.", "Agent không có phiên bản mới nào trong lúc đổi chính sách (đổi qua tag).", "Prompt có commit ghi nguồn Canvas.", "Học viên giải thích vì sao [json-invalid] bị đánh dấu sai."],
  },
  pitfalls: [
    ["Đổi tag rồi mà agent vẫn trả lời theo bản cũ.", "Thay đổi áp dụng ở lượt sau; thread đang chạy giữa chừng vẫn dùng bản đã nạp. Bắt đầu lượt mới hoặc thread mới. Kiểm tra agent đang ghim đúng gói và đúng tag."],
    ["Agent không dùng skill.", "Phần mô tả trong SKILL.md không nói rõ khi nào dùng; hoặc skill chưa publish / chưa tick trong khối Kỹ năng."],
    ["System prompt ngày càng dài.", "Chuyển phần quy trình sang gói/Skill; giữ system prompt cho vai trò và giới hạn."],
    ["Biến trong prompt không được thay.", "Chỉ hỗ trợ dạng {{ten_bien}}; kiểm tra đúng tên biến."],
    ["So sánh model trên một câu rồi kết luận.", "Một câu không đủ: nạp dataset và Chạy tất cả, hoặc làm thí nghiệm ở buổi 7."],
  ],
  quiz: [
    { q: "Chính sách đổi trả dùng chung cho 15 agent nên đặt ở đâu?", options: ["System prompt của từng agent", "Gói ngữ cảnh trong Context Hub, ghim theo tag", "Bộ nhớ dài hạn của từng người", "Tệp đính kèm trong một cuộc chat"], a: "Gói ngữ cảnh, ghim @prod: một nguồn, có phiên bản, đổi một lần áp cho mọi agent.", rubric: "1 điểm." },
    { q: "Khi chuyển tag prod sang commit mới, agent đang ghim @prod sẽ dùng bản mới khi nào?", options: ["Ngay giữa lượt đang chạy", "Ở lượt sau", "Sau khi admin khởi động lại máy chủ", "Sau khi lưu lại agent"], a: "Ở lượt sau.", rubric: "1 điểm." },
    { q: "Agent có thể tự sửa gói ngữ cảnh mà nó đang ghim không? Vì sao thiết kế như vậy?", a: "Không. Gói được gắn chỉ đọc vào file ảo của agent. Để tránh agent (hoặc nội dung độc hại agent đọc phải) thay đổi chính sách chung; mọi thay đổi phải qua commit của người có quyền và có lịch sử.", rubric: "2 điểm: đúng \"không/chỉ đọc\" (1), lý do an toàn/kiểm soát thay đổi (1)." },
    { q: "Skill khác AGENTS.md ở điểm nào về cách nạp vào ngữ cảnh?", a: "AGENTS.md luôn được đưa vào ngữ cảnh; Skill chỉ có phần mô tả ngắn, nội dung đầy đủ chỉ được đọc khi agent thấy cần.", rubric: "2 điểm." },
    { q: "Mô tả quy trình an toàn để đưa một bản prompt viết lại bằng Canvas vào chạy thật.", a: "Canvas → nhận/bỏ thay đổi → Lưu commit (chưa đổi tag) → chạy thí nghiệm trên dataset so với commit cũ → qua cổng → chuyển tag production → theo dõi; sai thì chuyển tag về.", rubric: "3 điểm: commit không đổi tag ngay (1), có bước đánh giá so sánh (1), chuyển tag và rollback (1)." },
    { q: "Trong Playground, bạn cần agent luôn trả về JSON có trường \"ma_don\". Bạn kiểm tra điều đó bằng tính năng nào, và làm sao chứng minh trên nhiều câu?", a: "Schema đầu ra (structured output) với kiểm tra hợp lệ; nạp dataset và Chạy tất cả, hoặc dùng giám khảo kiểm tra cứng/JSON schema trong thí nghiệm.", rubric: "2 điểm: schema đầu ra (1), chứng minh trên nhiều câu (1)." },
  ],
  homework: [
    "Lập bảng \"kiến thức → đặt ở đâu\" cho agent ở bài về nhà buổi 2 (ít nhất 6 dòng).",
    "Viết một Skill (SKILL.md, tối đa 1 trang) cho một quy trình thật: khi nào dùng, các bước, ví dụ.",
    "Trong Playground, so sánh 3 cấu hình trên 5 câu của bạn; ghi lại cấu hình nào rẻ nhất mà vẫn đạt.",
  ],
  glossary: [
    ["Cửa sổ ngữ cảnh", "Lượng chữ tối đa model đọc trong một lượt."],
    ["Thiết kế ngữ cảnh", "Chọn đúng thông tin đưa cho model ở mỗi lượt."],
    ["Commit", "Một lần lưu có ghi chú trong lịch sử, quay lại được."],
    ["Tag", "Nhãn trỏ vào một commit, ví dụ prod."],
    ["Ghim (pin)", "Agent dùng gói theo dạng gói @ tag."],
    ["AGENTS.md", "Tài liệu hướng dẫn luôn được đưa cho agent."],
    ["Skill / SKILL.md", "Sổ tay một việc, agent đọc khi cần."],
    ["Structured output", "Bắt model trả đúng một mẫu JSON."],
    ["Prompt Canvas", "Viết lại prompt bằng chat, xem khác biệt từng chữ."],
  ],
  slides: [
    { t: "Mặt bàn của model", diagram: "context-desk", note: "Ẩn dụ mặt bàn: hỏi lớp \"bày gì lên bàn cho câu hỏi đổi hàng?\"" },
    { t: "Kiến thức đặt ở đâu", pts: ["System prompt: vai trò, giới hạn", "Gói ngữ cảnh: quy trình, chính sách", "Skill: việc dài, ít dùng", "Bộ nhớ, dự án, RAG: theo người, vụ việc, kho lớn"] },
    { t: "Prompt Hub", pts: ["Commit cho mỗi lần sửa", "Tag production cho bản chạy thật", "Agent lấy prompt theo tag", "Webhook khi prompt đổi"], shot: "prompt-commit" },
    { t: "Prompt Canvas", shot: "prompt-canvas", pts: ["Nút nhanh", "Sửa đoạn được chọn", "Nhận/bỏ từng chỗ → commit"] },
    { t: "Context Hub", shot: "pack-history", pts: ["AGENTS.md + Skill + chính sách", "Commit · So khác · Chuyển về đây", "Tag prod / staging"] },
    { t: "Ghim theo tag", pts: ["Agent ghim gói @ prod", "Chuyển tag = mọi agent đổi ở lượt sau", "Gói chỉ đọc với agent", "Rollback = chuyển tag về"], shot: "pack-diff" },
    { t: "Playground", pts: ["Công cụ giả lập", "Schema JSON có kiểm tra", "Ảnh", "So sánh 1–4 cấu hình · chạy cả dataset"], shot: "pg-schema" },
    { t: "Mẹo máy lab", code: "[json] khách muốn đổi đơn DH1023\n[json-invalid] …\n[tool:tra_don] DH1023", note: "Chỉ có tác dụng với AI giả. Nhắc học viên không dùng trên hệ thống thật." },
    { t: "Lab 3", pts: ["Gói cskh @ prod", "7 → 10 → 7 ngày chỉ bằng tag", "Canvas → commit", "Playground: schema + so sánh"] },
  ],
  plan: [
    { min: 10, act: "Ôn buổi 2", detail: "Xem nhanh 2 bảng mức tự chủ của học viên." },
    { min: 30, act: "Giảng: thiết kế ngữ cảnh", detail: "Slide 1–2. Bài tập 5 phút: xếp 8 mẩu kiến thức vào đúng chỗ." },
    { min: 30, act: "Giảng + trình diễn: Prompt Hub, Canvas, Context Hub", detail: "Slide 3–6. Trình diễn chuyển tag trực tiếp." },
    { min: 10, act: "Nghỉ giải lao", detail: "" },
    { min: 15, act: "Giảng: Playground và mẹo máy lab", detail: "Slide 7–8." },
    { min: 55, act: "Lab 3", detail: "Ký nhận 4 điểm kiểm." },
    { min: 20, act: "Kiểm tra", detail: "6 câu." },
    { min: 10, act: "Tổng kết, bài về nhà", detail: "" },
  ],
  ask: [
    "Nếu một nhân viên sửa thẳng system prompt của agent đang chạy thật, rủi ro gì? Quy trình nào chặn được?",
    "Có loại kiến thức nào KHÔNG nên đưa cho agent dù tiện?",
    "Khi nào nên tách một gói thành hai gói?",
  ],
  notes: [
    "Điểm \"aha\" của buổi: đổi hành vi 15 agent mà không mở agent nào. Dành thời gian cho khoảnh khắc này.",
    "Nhắc: so sánh model trên vài câu chỉ để cảm nhận; quyết định phải dựa trên thí nghiệm (buổi 7).",
  ],
};
export default m;
