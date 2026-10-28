import type { Module } from "./types";

const m: Module = {
  n: 1, slug: "tong-quan-kien-truc", track: "nen-tang", roles: ["user", "admin", "it", "architect"], hours: 3,
  title: "Tổng quan và kiến trúc Agent OS",
  short: "Tổng quan, kiến trúc",
  summary: "Agent OS giải quyết bài toán gì, gồm những thành phần nào, chạy trên nền mã nguồn mở nào, và các khái niệm cốt lõi mà mọi vai trò phải nói cùng một ngôn ngữ.",
  goals: [
    "Vẽ lại được sơ đồ kiến trúc Agent OS (truy cập, API, worker, sandbox, PostgreSQL, bên ngoài) và giải thích luồng một câu hỏi đi qua hệ thống.",
    "Phân biệt đúng 12 khái niệm cốt lõi: workspace, vai trò, agent, phiên bản, thread, run, checkpoint, trace, dataset, prompt/gói, kênh, bí mật.",
    "Nêu được vai trò của LangChain, LangGraph và Deep Agents trong Agent OS.",
    "Tìm được mọi khu vực chức năng trên giao diện trong dưới 30 giây và đọc đúng nhãn mức độ sẵn sàng (Ổn định / Thử nghiệm).",
  ],
  prereq: ["Biết dùng trình duyệt và một công cụ chat AI (ChatGPT, Gemini…).", "Có tài khoản trên máy lab (xem Chuẩn bị môi trường lab)."],
  sections: [
    {
      id: "bai-toan", h: "Agent OS giải quyết bài toán gì",
      html: `<p>Doanh nghiệp muốn dùng AI không chỉ để trò chuyện mà để <strong>làm việc thật</strong>: trả lời khách trên Zalo, đọc hộp thư, soạn báo giá, tra đơn hàng, báo cáo hằng tuần. Khi đó bốn câu hỏi xuất hiện ngay: <em>dữ liệu nằm ở đâu, ai cho phép AI làm gì, tốn bao nhiêu tiền, và làm sao biết AI làm đúng</em>.</p>
<p>Agent OS trả lời bốn câu hỏi đó bằng hai sản phẩm đi cùng nhau:</p>
<ul>
<li><strong>Agent OS Chat</strong>: giao diện trò chuyện cho nhân viên, quen tay như ChatGPT, có voice song công, Canvas, dự án, nghiên cứu sâu, tạo file.</li>
<li><strong>Agent OS</strong> (bảng quản lý + máy chủ): nơi tạo agent, gắn công cụ, đặt quy tắc duyệt, đưa agent ra kênh nhắn tin, theo dõi từng bước (tracing), đo chất lượng (đánh giá), kiểm soát chi phí (LLM Gateway) và bảo mật.</li>
</ul>
<p>Mô hình triển khai là <strong>mỗi khách một máy chủ riêng</strong> (VPS): toàn bộ dữ liệu nằm trong PostgreSQL trên máy chủ đó; ra ngoài chỉ có nội dung gửi tới nhà cung cấp model để trả lời câu hỏi (có thể che dữ liệu cá nhân trước). Không có phí bản quyền nền tảng bên thứ ba; khách trả tiền VPS và tiền gọi model.</p>
<p>Về độ đầy đủ, Agent OS được chấm theo danh sách trang tính năng công khai của LangSmith (bộ công cụ thương mại của LangChain): <strong>89,5%</strong> tại cổng chất lượng GĐ9.Z ngày 25/10/2026 (191 đủ, 42 một phần, 4 chưa có).</p>`,
    },
    {
      id: "kien-truc", h: "Kiến trúc tổng thể",
      diagram: "aos-arch", diagramCaption: "Kiến trúc Agent OS trên một máy chủ riêng của khách",
      html: `<p>Mọi truy cập (Chat, bảng quản lý, kênh, phần mềm khác) đi vào qua <strong>Caddy</strong> (HTTPS) rồi chuyển vào ứng dụng chỉ nghe trên <code>127.0.0.1</code>. Ứng dụng không mở cổng trực tiếp ra Internet.</p>
<table><thead><tr><th>Thành phần</th><th>Việc chính</th></tr></thead><tbody>
<tr><td>Agent OS API (FastAPI, uvicorn)</td><td>Chạy agent (LangGraph + Deep Agents), Agent Server API tương thích LangGraph tại <code>/api/v1</code>, LLM Gateway, duyệt, kênh, tracing, đánh giá, Prompt/Context Hub, phân quyền.</td></tr>
<tr><td>Worker</td><td>Hàng đợi run nền, lịch chạy (cron), các việc định kỳ.</td></tr>
<tr><td>Sandbox (Docker + gVisor)</td><td>Chạy code do AI viết trong container dùng một lần, không mạng.</td></tr>
<tr><td>PostgreSQL + pgvector</td><td>Checkpoint của thread và run, Store (bộ nhớ dài hạn có tìm theo nghĩa), trace, dataset, cấu hình agent, người dùng, khoá, bí mật (đã mã hoá).</td></tr>
<tr><td>Agent OS Chat (React)</td><td>Giao diện chat, phục vụ tại <code>/chat/</code>, nói chuyện với API bằng giao thức LangGraph.</td></tr>
</tbody></table>
<p><strong>Luồng một câu hỏi</strong>: người dùng gửi tin trong Chat → API tạo một <em>run</em> trên <em>thread</em> → agent (graph LangGraph) gọi model qua <strong>Gateway</strong> (kiểm trần chi phí, che dữ liệu cá nhân, định tuyến model) → model đề nghị gọi công cụ → API kiểm quyền; nếu công cụ thuộc loại "Hỏi trước" thì <em>dừng</em> và tạo thẻ duyệt → chạy công cụ → lặp lại tới khi có câu trả lời → mỗi bước được lưu <em>checkpoint</em> và ghi <em>trace</em>.</p>`,
    },
    {
      id: "nen-mong", h: "Nền móng mã nguồn mở",
      html: `<p>Agent OS không tự phát minh lại "bộ não" agent. Nó dùng hệ sinh thái mã nguồn mở chính thức của LangChain (giấy phép MIT) và tuỳ biến qua các điểm mở rộng chính thức, không sửa thư viện gốc.</p>
<table><thead><tr><th>Thư viện</th><th>Vai trò trong Agent OS</th></tr></thead><tbody>
<tr><td>LangChain 1.4</td><td>Giao diện thống nhất cho nhiều nhà cung cấp model, công cụ (<code>@tool</code>), middleware chen trước/sau mỗi lần gọi model (nơi Gateway, che dữ liệu, giới hạn vòng lặp được gắn vào).</td></tr>
<tr><td>LangGraph 1.2</td><td>Bộ máy chạy: agent là một graph các bước, lưu checkpoint sau mỗi bước, dừng chờ người (interrupt) rồi chạy tiếp, stream từng token, Store cho bộ nhớ dài hạn.</td></tr>
<tr><td>Deep Agents 0.7</td><td>Khung agent dựng sẵn: lập kế hoạch (to-do), agent con, hệ thống file ảo, Skill, AGENTS.md, duyệt trước công cụ nhạy cảm. Mỗi agent của Agent OS được dựng bằng <code>create_deep_agent</code> từ phiên bản cấu hình đang dùng.</td></tr>
<tr><td>LangGraph SDK, assistant-ui</td><td>Phía giao diện: Chat nói chuyện với máy chủ theo chuẩn LangGraph; thư viện chính thức <code>langgraph-sdk</code> và giao diện mẫu <code>agent-chat-ui</code> của LangChain dùng được với Agent OS.</td></tr>
<tr><td>MCP 2.3, A2A 1.0, OpenTelemetry</td><td>Chuẩn mở để nối với phần mềm khác: công cụ (MCP), agent-với-agent (A2A), dữ liệu theo dõi (OTLP, quy ước GenAI).</td></tr>
</tbody></table>
<p>LangSmith là sản phẩm thương mại, không mã nguồn mở. Agent OS <strong>không dùng mã của LangSmith</strong>; nó tự xây các tính năng tương đương trên các thành phần mở ở trên.</p>`,
    },
    {
      id: "khai-niem", h: "12 khái niệm cốt lõi",
      html: `<p>Đây là "từ điển chung" của cả khoá. Mọi buổi sau đều dùng lại.</p>
<table><thead><tr><th>Khái niệm</th><th>Nghĩa</th><th>Gặp ở đâu</th></tr></thead><tbody>
<tr><td>Workspace</td><td>Không gian dữ liệu của một công ty hoặc một bộ phận; dữ liệu các workspace tách nhau ở tầng cơ sở dữ liệu (RLS).</td><td>Thành viên &amp; quyền</td></tr>
<tr><td>Vai trò</td><td><code>admin</code>, <code>editor</code>, <code>viewer</code>, gán theo từng workspace; cộng thêm nhóm người dùng để chia quyền.</td><td>Thành viên &amp; quyền</td></tr>
<tr><td>Agent</td><td>Trợ lý AI có chỉ dẫn, model, công cụ, kỹ năng, gói ngữ cảnh, agent con, kênh, trần chi phí.</td><td>Xây dựng</td></tr>
<tr><td>Phiên bản agent</td><td>Mỗi lần lưu cấu hình là một bản bất biến; khôi phục được.</td><td>Agent → Phiên bản</td></tr>
<tr><td>Thread</td><td>Một cuộc trò chuyện, có lịch sử và trạng thái riêng.</td><td>Chat, Tracing → Threads</td></tr>
<tr><td>Run</td><td>Một lượt agent chạy trên một thread (hoặc không cần thread).</td><td>Tracing, API <code>/runs</code></td></tr>
<tr><td>Checkpoint</td><td>Trạng thái được lưu sau mỗi bước; nền của duyệt, chạy tiếp, tua lại.</td><td>Studio</td></tr>
<tr><td>Trace / project</td><td>Bản ghi từng bước của một run; trace gom theo project, có nhãn môi trường.</td><td>Tracing</td></tr>
<tr><td>Dataset, evaluator, experiment</td><td>Bộ đề, giám khảo, lần chấm cả bộ đề.</td><td>Dataset &amp; Thí nghiệm, Đánh giá</td></tr>
<tr><td>Prompt, gói ngữ cảnh, tag</td><td>Chỉ dẫn và kiến thức có phiên bản (commit); tag như <code>prod</code> trỏ vào bản đang chạy thật.</td><td>Prompts, Context Hub</td></tr>
<tr><td>Kênh</td><td>Cửa vào của agent: Telegram, Zalo OA, Zalo cá nhân, Slack, webhook.</td><td>Agent → Kênh</td></tr>
<tr><td>Bí mật</td><td>Token, mật khẩu đã mã hoá, chỉ gửi được tới máy đích đã khai.</td><td>Thành viên &amp; quyền → Bí mật</td></tr>
</tbody></table>`,
    },
    {
      id: "ban-do-giao-dien", h: "Bản đồ giao diện",
      shot: "agent-config", shotCaption: "Bảng quản lý: thanh trái với các khu vực, trang cấu hình một agent",
      html: `<table><thead><tr><th>Nhóm trên thanh trái</th><th>Các mục</th></tr></thead><tbody>
<tr><td>Phía trên</td><td>Engine, Studio, Xây dựng, Tracing, Giám sát, Phân tích, Dataset &amp; Thí nghiệm, Đánh giá, Hàng đợi chú thích, Prompts, Playground, Deployments, Context Hub</td></tr>
<tr><td>WORKSPACE</td><td>Bảng điều khiển, Ứng dụng tuỳ chỉnh, LLM Gateway, Sandboxes, <strong>Thêm</strong> (Chat, Công cụ, Bộ nhớ, Hộp duyệt, Agent Server, Cài đặt máy chủ, Tự động hoá, Khoá Gateway, Thành viên &amp; quyền…)</td></tr>
<tr><td>Dưới cùng</td><td>Cài đặt và tên tài khoản</td></tr>
</tbody></table>
<p>Ba phím tắt nên thuộc: <strong>Ctrl + K</strong> tìm nhanh mọi thứ; <strong>Ctrl + I</strong> mở Trợ lý Agent OS (biết trang đang xem, mọi thao tác ghi đều chờ duyệt); <strong>Ctrl + F</strong> tìm trong một trace. Nút <strong>EN / VI</strong> đổi ngôn ngữ, nhớ theo từng người. Một số tên kỹ thuật (Tracing, Playground, Dataset, Deployments) được giữ nguyên tiếng Anh có chủ đích.</p>`,
    },
    {
      id: "muc-do", h: "Mức độ sẵn sàng và giới hạn cần nói thẳng",
      html: `<p>Một chuyên gia phải biết rõ tính năng nào dùng được ngay, tính năng nào còn điều kiện. Trong tài liệu khoá học và trang Tính năng, mỗi mục có nhãn:</p>
<ul>
<li><strong>Ổn định</strong>: đã xong, có kiểm thử tự động, đã chạy trên giao diện thật (AI giả hoặc model thật).</li>
<li><strong>Thử nghiệm</strong>: chạy được nhưng mới thử với dịch vụ giả lập (Telegram, Zalo OA, Slack, Google/Microsoft 365, kho S3), hoặc là kênh không chính thức (Zalo cá nhân), hoặc còn giới hạn kỹ thuật (che dữ liệu trong ảnh mờ, nghiêng).</li>
<li><strong>Cần tài khoản của khách</strong>: bot, OA, OAuth client, tên miền HTTPS, kho sao lưu.</li>
<li><strong>Nên có chuyên gia duyệt</strong>: đăng nhập 2 lớp, mã hoá dữ liệu, xác thực tuỳ biến, Ứng dụng tuỳ chỉnh, danh tính agent, Zalo cá nhân, che dữ liệu cá nhân và nghĩa vụ theo Luật Bảo vệ dữ liệu cá nhân.</li>
</ul>
<p>Số liệu kiểm thử ở cổng GĐ9.Z: máy chủ 779 bài đạt, Chat 118/118, kiểm rò rỉ sang tài khoản demo 220 đường 0 rò.</p>`,
    },
  ],
  lab: {
    title: "Lab 1: Định vị hệ thống và đọc một lượt chạy",
    time: "50 phút",
    setup: "Máy lab đang chạy ở chế độ AI giả (xem Chuẩn bị môi trường lab). Mỗi học viên có một tài khoản editor trong workspace lớp học.",
    steps: [
      "Đăng nhập bảng quản lý. Bấm <strong>EN / VI</strong> hai lần, quan sát chữ đổi theo ngôn ngữ.",
      "Bấm <strong>Ctrl + K</strong>, gõ <code>Context Hub</code> và mở trang đó. Lặp lại cho <code>LLM Gateway</code> và <code>Hộp duyệt</code>.",
      "Mở <strong>Thêm → Thành viên &amp; quyền</strong>: ghi lại vai trò của mình và tên workspace.",
      "Mở <strong>Xây dựng</strong>, chọn một agent mẫu, đọc thẻ <strong>Cấu hình</strong>: model, khối Công cụ (cột Hỏi trước), Kỹ năng, Gói ngữ cảnh, Sub-agents.",
      "Thẻ <strong>Thử</strong>: hỏi agent một câu. Sau đó bấm nút <strong>Trace</strong> ở góc trên, xem trace dạng <strong>Hành trình</strong> và <strong>Tin nhắn</strong>.",
      "Bấm <strong>Ctrl + I</strong>, hỏi Trợ lý: \"agent nào có nhiều lượt chạy nhất hôm nay?\". Quan sát trợ lý dùng công cụ đọc dữ liệu.",
      "Trên giấy: vẽ lại sơ đồ kiến trúc và đánh dấu con đường câu hỏi ở bước 5 đã đi qua (Chat/Thử → API → Gateway → model → checkpoint → trace).",
    ],
    result: "Mở được mọi khu vực bằng Ctrl+K; đọc được cấu hình agent; thấy trace của lượt chạy vừa tạo với các bước gọi model; sơ đồ vẽ tay đúng 5 khối chính và luồng đi.",
    shot: "trace-trajectory", shotAlt: "Trace dạng Hành trình của một lượt chạy",
    check: ["Học viên chỉ ra được trên trace bước gọi model và thời gian, chi phí.", "Sơ đồ có đủ: truy cập, Caddy/HTTPS, API, worker hoặc sandbox, PostgreSQL, model bên ngoài.", "Học viên nêu đúng vai trò của mình và giải thích vì sao không thấy một số mục (theo vai trò)."],
  },
  pitfalls: [
    ["Không thấy một số mục trên thanh trái (Thành viên & quyền, Cài đặt máy chủ).", "Đó là mục chỉ admin thấy. Kiểm tra vai trò ở Thành viên & quyền; xin admin cấp quyền nếu cần."],
    ["Trợ lý Ctrl+I trả lời \"không có quyền\" khi yêu cầu sửa.", "Đúng thiết kế: mọi thao tác ghi của Trợ lý chờ duyệt; tài khoản demo hoặc viewer chỉ đọc."],
    ["Giao diện lẫn tiếng Anh.", "Bấm EN/VI để chuyển. Một số tên kỹ thuật được giữ tiếng Anh có chủ đích."],
    ["Nhầm \"thread\" với \"run\" khi đọc trace.", "Thread là cuộc trò chuyện; mỗi lần gửi tin tạo một run mới trên thread đó. Xem kiểu danh sách Threads / Trace / Số run trong Tracing."],
  ],
  quiz: [
    { q: "Trong Agent OS, thành phần nào lưu trạng thái để agent có thể dừng chờ duyệt rồi chạy tiếp sau khi máy chủ khởi động lại?", options: ["Store (bộ nhớ dài hạn)", "Checkpoint trong PostgreSQL", "Bộ nhớ đệm của trình duyệt", "Trace"], a: "Checkpoint trong PostgreSQL. LangGraph lưu checkpoint sau mỗi bước; khi duyệt, run nạp lại checkpoint và chạy tiếp.", rubric: "1 điểm cho đáp án đúng. Không cho điểm Store (Store giữ bộ nhớ dài hạn, không phải trạng thái run)." },
    { q: "Thư viện nào cung cấp sẵn lập kế hoạch, agent con, Skill và AGENTS.md cho mỗi agent?", options: ["LangChain", "LangGraph", "Deep Agents", "assistant-ui"], a: "Deep Agents (create_deep_agent).", rubric: "1 điểm." },
    { q: "Nội dung nào rời khỏi máy chủ của khách khi agent trả lời một câu hỏi?", options: ["Toàn bộ cơ sở dữ liệu", "Nội dung gửi tới nhà cung cấp model để trả lời (có thể đã che dữ liệu cá nhân)", "Không gì cả, model chạy trên máy chủ", "Trace của mọi workspace"], a: "Nội dung gửi tới nhà cung cấp model (đã che nếu bật che dữ liệu cá nhân).", rubric: "1 điểm. Thảo luận thêm: nếu bật xuất OTLP hoặc sao lưu ra ngoài thì đó là luồng ra có chủ đích khác." },
    { q: "Giải thích sự khác nhau giữa workspace và vai trò, kèm một ví dụ phân quyền cho một công ty có 2 phòng ban.", a: "Workspace tách dữ liệu (ở tầng DB, RLS); vai trò quyết định được làm gì trong workspace đó. Ví dụ: workspace \"Bán hàng\" và \"Kế toán\"; trưởng phòng là admin workspace của mình, nhân viên là editor hoặc viewer; agent dùng chung chia sẻ qua nhóm người dùng.", rubric: "3 điểm: 1 điểm định nghĩa workspace (có ý tách dữ liệu), 1 điểm vai trò (admin/editor/viewer), 1 điểm ví dụ hợp lý. Trừ 1 điểm nếu cho rằng vai trò tách dữ liệu." },
    { q: "Liệt kê đúng thứ tự các khối mà một câu hỏi trong Chat đi qua cho tới khi có câu trả lời.", a: "Chat → Caddy (HTTPS) → API tạo run trên thread → graph agent → Gateway (trần, che PII, định tuyến) → model → (công cụ, có thể dừng duyệt) → lặp → trả lời; checkpoint và trace ghi ở mỗi bước.", rubric: "3 điểm: đủ khối chính (1), đúng vị trí Gateway trước model (1), nhắc checkpoint/trace hoặc điểm dừng duyệt (1)." },
    { q: "Vì sao không nên giới thiệu với khách rằng \"kênh Zalo OA đã chạy thật\" ở thời điểm hiện tại?", a: "Vì kênh mới được kiểm thử với máy chủ giả lập (nhãn Thử nghiệm) và cần OA, ứng dụng Zalo và tên miền HTTPS của khách; còn phải thử một lần với OA thật.", rubric: "2 điểm: nêu nhãn Thử nghiệm/giả lập (1), nêu điều kiện tài khoản/tên miền (1)." },
  ],
  homework: [
    "Vẽ sơ đồ kiến trúc Agent OS cho công ty của anh chị (một trang), đánh dấu dữ liệu nào nằm trong máy chủ, luồng nào đi ra ngoài.",
    "Liệt kê 3 việc trong công ty có thể giao cho agent; với mỗi việc ghi: cần công cụ gì, việc nào phải có người duyệt.",
    "Đọc mục 1–2 của Hướng dẫn sử dụng Agent OS (PDF) và ghi 3 câu hỏi mang tới buổi 2.",
  ],
  glossary: [
    ["Agent Server API", "API theo chuẩn LangGraph tại /api/v1 (assistants, threads, runs, crons, store)."],
    ["Caddy", "Máy chủ web làm cổng HTTPS phía trước ứng dụng."],
    ["Worker", "Tiến trình chạy hàng đợi run nền và lịch chạy."],
    ["gVisor", "Lớp cách ly nhân hệ điều hành cho container chạy code AI viết."],
    ["pgvector", "Phần mở rộng PostgreSQL lưu và tìm vector (embedding)."],
    ["RLS", "Row-Level Security: PostgreSQL tự lọc dòng theo workspace của người đang truy vấn."],
    ["Checkpoint", "Trạng thái run lưu sau mỗi bước."],
    ["Deep Agents", "Khung agent của LangChain: kế hoạch, agent con, Skill, file ảo, duyệt."],
    ["Parity", "Mức tương đương tính năng so với một sản phẩm chuẩn (ở đây là LangSmith)."],
  ],
  slides: [
    { t: "Bốn câu hỏi khi đưa AI vào làm việc thật", pts: ["Dữ liệu nằm ở đâu?", "Ai cho phép AI làm gì?", "Tốn bao nhiêu tiền?", "Làm sao biết AI làm đúng?"], note: "Mở bằng câu hỏi cho lớp: công ty anh chị lo câu nào nhất? Ghi lên bảng, quay lại ở cuối buổi." },
    { t: "Hai sản phẩm, một hệ thống", pts: ["Agent OS Chat: nhân viên dùng hằng ngày", "Agent OS: tạo, vận hành, đo, kiểm soát agent", "Mỗi khách một máy chủ riêng", "Không phí nền tảng bên thứ ba"], shot: "canvas-doc" },
    { t: "Kiến trúc tổng thể", diagram: "aos-arch", note: "Đi chậm từ trái qua phải. Nhấn: ứng dụng chỉ nghe 127.0.0.1, mọi thứ qua Caddy." },
    { t: "Luồng một câu hỏi", pts: ["Chat → API tạo run trên thread", "Graph agent → Gateway → model", "Công cụ: kiểm quyền, có thể dừng chờ duyệt", "Checkpoint + trace ở mỗi bước"] },
    { t: "Nền móng mã nguồn mở", pts: ["LangChain: model, công cụ, middleware", "LangGraph: graph, checkpoint, interrupt, Store", "Deep Agents: kế hoạch, agent con, Skill", "MCP · A2A · OpenTelemetry: chuẩn mở"], note: "Nhấn: không dùng mã LangSmith; tuỳ biến qua điểm mở rộng chính thức." },
    { t: "12 khái niệm cốt lõi", pts: ["Workspace · Vai trò · Agent · Phiên bản", "Thread · Run · Checkpoint · Trace", "Dataset/Evaluator/Experiment · Prompt/Gói/Tag", "Kênh · Bí mật"] },
    { t: "Bản đồ giao diện", shot: "agent-config", pts: ["Ctrl+K tìm nhanh", "Ctrl+I Trợ lý Agent OS", "Ctrl+F tìm trong trace"] },
    { t: "Mức độ sẵn sàng", pts: ["Ổn định", "Thử nghiệm: dịch vụ giả lập, kênh không chính thức", "Cần tài khoản của khách", "Nên có chuyên gia duyệt"], note: "Chuyên gia phải nói đúng giới hạn với khách. Cho ví dụ Zalo OA." },
    { t: "Số liệu chất lượng (25/10/2026)", pts: ["Tương đương LangSmith 89,5%", "779 bài kiểm thử máy chủ đạt", "Chat 118/118", "0 rò rỉ sang tài khoản demo (220 đường)"] },
    { t: "Lab 1", pts: ["Ctrl+K tới mọi khu vực", "Đọc cấu hình một agent", "Chạy thử và đọc trace", "Vẽ lại kiến trúc"] },
  ],
  plan: [
    { min: 15, act: "Khởi động", detail: "Giới thiệu khoá, cách chấm, chứng nhận. Câu hỏi mở: công ty anh chị lo câu nào trong 4 câu hỏi? Ghi bảng." },
    { min: 40, act: "Giảng: bài toán, kiến trúc, luồng một câu hỏi", detail: "Slide 1–4. Vẽ trực tiếp sơ đồ lên bảng trắng song song với slide." },
    { min: 25, act: "Giảng: nền móng mã nguồn mở và 12 khái niệm", detail: "Slide 5–6. Cho học viên ghép khái niệm với ví dụ đời thường (thẻ giấy)." },
    { min: 10, act: "Nghỉ giải lao", detail: "" },
    { min: 15, act: "Trình diễn: bản đồ giao diện", detail: "Giảng viên chiếu máy lab, đi qua từng nhóm menu, Ctrl+K, Ctrl+I." },
    { min: 50, act: "Lab 1", detail: "Học viên làm theo từng bước; giảng viên đi kiểm 3 điểm ký nhận lab." },
    { min: 15, act: "Kiểm tra nhanh", detail: "6 câu hỏi kiểm tra, chấm chéo theo cặp bằng rubric." },
    { min: 10, act: "Tổng kết và bài về nhà", detail: "Quay lại 4 câu hỏi đầu buổi: hệ thống trả lời từng câu thế nào. Giao bài tập về nhà." },
  ],
  ask: [
    "Nếu công ty anh chị chỉ được chọn một trong bốn: dữ liệu, quyền, chi phí, chất lượng, anh chị ưu tiên gì? Vì sao?",
    "Vì sao một agent cần \"checkpoint\" còn một chatbot thông thường thì không?",
    "Một khách hỏi: \"dữ liệu của tôi có đi ra nước ngoài không?\". Anh chị trả lời chính xác thế nào?",
  ],
  notes: [
    "Lớp có cả người dùng nghiệp vụ và kỹ sư IT: giữ thuật ngữ ở mức khái niệm, để chi tiết code cho buổi 9–11.",
    "Đừng hứa tính năng nhãn Thử nghiệm như đã chạy thật. Tập cho học viên thói quen đọc nhãn.",
    "Nếu máy lab chậm: chuẩn bị sẵn một trace mẫu để chiếu thay cho bước 5 của lab.",
  ],
};
export default m;
