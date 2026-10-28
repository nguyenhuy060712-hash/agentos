import type { Module } from "./types";

const m: Module = {
  n: 5, slug: "kenh-ket-noi", track: "van-hanh", roles: ["admin", "it", "architect"], hours: 3,
  title: "Kênh nhắn tin và kết nối công cụ",
  short: "Kênh và kết nối",
  summary: "Đưa agent ra Telegram, Zalo OA, Zalo cá nhân (thử nghiệm), Slack; nối Gmail, Lịch, Drive, Sheets, Outlook, OneDrive bằng OAuth của từng người; cắm công cụ qua MCP; quản lý bí mật theo máy đích; và hiểu rõ \"agent đang hành động bằng quyền của ai\".",
  goals: [
    "Mô tả đúng 6 tính chất chung của khung kênh và giải thích vì sao người nhắn qua kênh không dùng được tài khoản, bộ nhớ của chủ agent.",
    "Liệt kê đủ điều kiện để chạy thật từng kênh (bot, OA, app, tên miền, quyền) và các giới hạn riêng (Zalo 48 giờ, Slack Socket Mode, Zalo cá nhân).",
    "Cấu hình Google Workspace / Microsoft 365 ở 3 mức cho từng dịch vụ và giải thích luồng OAuth theo từng người.",
    "Tạo bí mật với máy đích được phép và gắn vào một kênh; thử kênh bằng khung Thử kênh có duyệt trong kênh.",
  ],
  prereq: ["Buổi 2 (duyệt, công cụ), buổi 1 (bí mật, workspace)."],
  sections: [
    {
      id: "khung-kenh", h: "Khung kênh dùng chung",
      shot: "channel-tab", shotCaption: "Thẻ Kênh của agent và khung Thử kênh",
      html: `<p>Mọi kênh dùng một khung chung (theo mẫu langgraph-messaging-integrations), nên có cùng 6 tính chất:</p>
<ol>
<li>Mỗi khách hoặc nhóm chat có <strong>thread và bộ nhớ riêng</strong>.</li>
<li>Trong nhóm, agent <strong>chỉ trả lời khi được gọi tên</strong>.</li>
<li><strong>Duyệt ngay trong kênh</strong>: nút Duyệt/Từ chối (Telegram, Slack), trả lời <code>duyệt &lt;số&gt;</code> (Zalo).</li>
<li>Người nhắn qua kênh <strong>không</strong> dùng được Gmail/Outlook, bộ nhớ, chỉ dẫn riêng của chủ agent: lỗ hổng này từng tồn tại và đã được chặn ở cổng GĐ8.Z.</li>
<li>Chi phí kênh ghi riêng tính năng "Kênh nhắn tin" để đặt trần và báo cáo.</li>
<li>Câu trả lời chậm vẫn được gửi muộn; khung <strong>Thử kênh</strong> cho nhắn như một khách mà không gửi gì ra ngoài.</li>
</ol>
<p>Thêm kênh: Agent → thẻ Kênh → Thêm kênh → chọn loại → nhập <strong>tên bí mật</strong>. Bảng kênh: Loại, Tên, Trạng thái, Người được dùng, Cuộc chat, nút Tắt.</p>`,
    },
    {
      id: "tung-kenh", h: "Từng kênh: điều kiện và giới hạn",
      diagram: "deploy", diagramCaption: "Các kênh đi vào máy chủ riêng qua cổng HTTPS",
      html: `<table><thead><tr><th>Kênh</th><th>Cách nối</th><th>Cần chuẩn bị</th><th>Điểm cần nhớ</th><th>Mức độ</th></tr></thead><tbody>
<tr><td>Telegram</td><td>Bot API (aiogram), mỗi bot một bộ nhận tin</td><td>Bot từ @BotFather; bí mật máy đích <code>api.telegram.org</code></td><td>Chữ, ảnh, file; không cần tên miền</td><td>Thử nghiệm, cần bot</td></tr>
<tr><td>Zalo OA</td><td>Webhook có chữ ký</td><td>OA + app trên developers.zalo.me, mã uỷ quyền lần đầu, tên miền HTTPS; máy đích <code>openapi.zalo.me</code>, <code>oauth.zaloapp.com</code></td><td>Kiểm chữ ký, chống tin lặp, cửa sổ thời gian 2 giờ cho webhook; tin tư vấn trong 48 giờ kể từ tin cuối của khách là miễn phí, gửi tối đa 7 ngày; trả lời được ảnh/file (chưa thử với OA thật)</td><td>Thử nghiệm, cần OA + tên miền</td></tr>
<tr><td>Zalo cá nhân</td><td>Thư viện không chính thức zca-js trong tiến trình Node phụ chỉ nghe nội bộ</td><td>Số Zalo phụ quét QR</td><td>Mặc định tắt, chỉ admin bật sau khi tick xác nhận rủi ro; ≥ 2 giây/tin, ≤ 200 tin/ngày, danh sách cho phép; phiên đăng nhập lưu mã hoá. Trái điều khoản Zalo, có thể bị khoá số. Không đưa vào bản giao khách.</td><td>Thử nghiệm, cần chuyên gia duyệt</td></tr>
<tr><td>Slack</td><td>Socket Mode (không cần tên miền)</td><td>Slack app: <code>app_mentions:read</code>, <code>chat:write</code>, <code>im:history</code>, <code>files:read</code>; token <code>xoxb-</code> và <code>xapp-</code>; máy đích <code>slack.com</code>, <code>files.slack.com</code></td><td>Trả lời trong luồng, tin riêng, nhận file; nhớ <code>/invite @bot</code>; người được phép nhập bằng mã thành viên U…</td><td>Thử nghiệm, cần app</td></tr>
</tbody></table>
<p>Chưa có: Microsoft Teams, email làm kênh nhận tin, WhatsApp/Messenger.</p>`,
    },
    {
      id: "google-m365", h: "Google Workspace và Microsoft 365",
      shot: "google-m365", shotCaption: "Cài đặt Google Workspace & Microsoft 365 của workspace: 3 mức cho từng dịch vụ",
      html: `<p>Agent đọc/ghi Gmail, Lịch, Drive, Sheets, Outlook, Lịch, OneDrive, Excel bằng <strong>tài khoản của chính người đang chat</strong>. 18 công cụ chạy ngay trong máy chủ (không thêm tiến trình phụ).</p>
<ol>
<li><strong>Admin</strong>: Cài đặt → Google Workspace &amp; Microsoft 365: nhập Client ID/secret (OAuth client loại Web của Google Cloud; app đăng ký ở Microsoft Entra ID với quyền Graph như Mail.Read, Calendars.Read, Files.Read), bấm Lưu client; đặt mỗi dịch vụ ở <em>Chưa bật</em>, <em>Chỉ đọc</em> hoặc <em>Đọc và ghi</em>. Workspace demo luôn tắt.</li>
<li><strong>Người dùng</strong>: Cài đặt → Kết nối công cụ (OAuth) → kết nối Google hoặc Microsoft, đăng nhập, cho phép. Ngắt kết nối / Làm mới khi token hết hạn. Token lưu mã hoá; chỉ người đó dùng được.</li>
<li><strong>Duyệt khi ghi</strong>: gửi mail, tạo lịch, sửa bảng tính luôn có thẻ duyệt với đủ nội dung; không tắt được.</li>
</ol>
<p>Redirect URI có dạng <code>https://&lt;tên miền&gt;/oauth/callback</code>, nên cần tên miền HTTPS. Mở cho hơn 100 người ngoài công ty thì Google yêu cầu thẩm định ứng dụng. Khi cần agent gửi từ hộp thư chung (cskh@), dùng <strong>Danh tính agent</strong> (chủ agent hoặc admin bật, có nhật ký kiểm toán; nên có chuyên gia duyệt).</p>`,
    },
    {
      id: "mcp-cong-cu", h: "Cắm công cụ qua MCP",
      shot: "mcp-tools", shotCaption: "Danh sách công cụ lấy từ một máy chủ MCP",
      html: `<p>MCP (Model Context Protocol) là chuẩn để phần mềm "cho mượn" công cụ. Agent OS là <strong>MCP client</strong> (dùng client MCP chính thức trong LangChain 1.x thay cho thư viện adapters cũ đã ngừng): Thêm → Công cụ → khai máy chủ MCP (ví dụ Odoo, CRM); công cụ ghi qua MCP mặc định cần duyệt; xác thực OAuth 2.0 theo từng người (PKCE), token mã hoá. Agent OS đồng thời là <strong>MCP server</strong> (buổi 9).</p>`,
    },
    {
      id: "bi-mat", h: "Bí mật và \"quyền của ai\"",
      html: `<p>Kho bí mật (Thành viên &amp; quyền → Bí mật): token, mật khẩu được mã hoá, không hiển thị lại; mỗi bí mật chỉ gửi được tới <strong>máy được phép</strong> đã khai, nên dù agent bị lừa (prompt injection) cũng không gửi bí mật đi nơi khác. Khoá nhà cung cấp AI ở Cài đặt → Secret nhà cung cấp.</p>
<p>Câu hỏi thiết kế quan trọng nhất của buổi: <strong>"agent đang hành động bằng quyền của ai?"</strong></p>
<table><thead><tr><th>Ngữ cảnh</th><th>Hành động bằng quyền của</th></tr></thead><tbody>
<tr><td>Nhân viên chat trong Agent OS Chat</td><td>Chính nhân viên đó (OAuth riêng, bộ nhớ riêng)</td></tr>
<tr><td>Khách nhắn qua Zalo/Telegram/Slack</td><td>Không phải chủ kênh; khách có thread và bộ nhớ riêng, không có OAuth của ai</td></tr>
<tr><td>Agent bật Danh tính agent</td><td>Tài khoản đã gắn cho agent, mọi lần dùng ghi nhật ký</td></tr>
<tr><td>Phần mềm gọi qua API/MCP</td><td>Chủ của khoá API dùng để gọi</td></tr>
</tbody></table>`,
    },
  ],
  lab: {
    title: "Lab 5: Đưa agent CSKH ra kênh và nối công cụ",
    time: "55 phút",
    setup: "Máy lab chế độ AI giả và máy chủ kênh giả lập; agent CSKH của Lab 2–3.",
    steps: [
      "Thành viên &amp; quyền → <strong>Bí mật</strong>: tạo bí mật <code>TELEGRAM_BOT_LAB</code> với giá trị giảng viên phát (token giả), máy đích <code>api.telegram.org</code>. Thử sửa máy đích thành một tên miền khác và đọc thông báo.",
      "Agent CSKH → thẻ <strong>Kênh</strong> → <strong>Thêm kênh</strong> Telegram, tên bí mật <code>TELEGRAM_BOT_LAB</code>.",
      "Khung <strong>Thử kênh</strong>: khách \"Lan\" nhắn \"áo khoác xanh size M còn không?\". Khách \"Huy\" nhắn một câu khác. Mở Tracing → Threads: xác nhận có 2 thread riêng.",
      "Khách \"Lan\" yêu cầu đổi hàng để kích hoạt công cụ Hỏi trước; bấm duyệt ngay trong khung kênh.",
      "Khách \"Lan\" nhắn \"đọc giúp email mới nhất của chủ shop\". Quan sát agent từ chối/không có quyền; giải thích vì sao.",
      "(Admin) Cài đặt → <strong>Google Workspace &amp; Microsoft 365</strong>: đặt Gmail = Chỉ đọc, Lịch = Đọc và ghi (không cần client thật, chỉ xem cấu hình). Đặt lại về Chưa bật.",
      "Thêm → <strong>Công cụ</strong>: xem danh sách máy chủ MCP của lớp; mở một công cụ ghi và xác nhận cờ duyệt mặc định.",
      "Viết \"phiếu go-live kênh Zalo OA\" cho shop: danh sách điều kiện cần khách cung cấp (dựa trên bảng ở bài giảng).",
    ],
    result: "Bí mật bị từ chối khi máy đích sai; kênh Telegram (giả lập) hoạt động; 2 khách có 2 thread; duyệt trong kênh chạy tiếp; agent không đọc được Gmail của chủ qua kênh; phiếu go-live Zalo OA đủ mục.",
    shot: "telegram", shotAlt: "Khách chat với agent trên Telegram (giả lập)",
    check: ["Tracing → Threads có 2 thread riêng của 2 khách.", "Có bản ghi duyệt từ kênh trong Hộp duyệt → Lịch sử.", "Học viên giải thích đúng vì sao bước 5 bị từ chối (quyền của người nhắn, không phải chủ kênh).", "Phiếu go-live có: OA, app Zalo, mã uỷ quyền, tên miền HTTPS, bí mật + máy đích, luật 48 giờ, thử với OA thật."],
  },
  pitfalls: [
    ["Khách nhắn Zalo/Telegram mà agent im lặng.", "Theo thứ tự: kênh có đang Đang dùng; agent có chạm trần; bí mật đúng và đúng máy đích; trong nhóm phải gọi tên; Zalo quá 48 giờ."],
    ["Zalo OA báo lỗi chữ ký hoặc tin bị bỏ qua.", "Sai secret của app, đồng hồ máy chủ lệch (cửa sổ thời gian 2 giờ), hoặc tin lặp đã được chống trùng. Kiểm tra log và NTP."],
    ["Slack bot không trả lời trong kênh.", "Chưa /invite bot vào kênh, thiếu scope, hoặc người nhắn không nằm trong danh sách được dùng."],
    ["OAuth Google báo redirect_uri_mismatch.", "Redirect URI trong Google Cloud phải đúng https://<tên miền>/oauth/callback."],
    ["Muốn agent tiếp khách gửi mail xác nhận.", "Không dùng OAuth của nhân viên; dùng Danh tính agent cho hộp thư chung, luôn duyệt, có nhật ký."],
  ],
  quiz: [
    { q: "Trong một nhóm Telegram 50 người, agent trả lời khi nào?", options: ["Mọi tin nhắn", "Chỉ khi được gọi tên", "Chỉ tin của admin nhóm", "Chỉ tin có ảnh"], a: "Chỉ khi được gọi tên.", rubric: "1 điểm." },
    { q: "Kênh nào không cần tên miền HTTPS công khai?", options: ["Zalo OA", "Slack (Socket Mode)", "Đăng nhập Google", "OAuth Microsoft 365"], a: "Slack (Socket Mode). Telegram cũng không cần.", rubric: "1 điểm." },
    { q: "Máy đích được phép của bí mật bảo vệ trước rủi ro nào?", a: "Agent bị lừa (prompt injection) hoặc lỗi cấu hình khiến bí mật bị gửi tới máy chủ lạ; bí mật chỉ đi được tới host đã khai.", rubric: "2 điểm." },
    { q: "Giải thích luật 48 giờ của Zalo OA và ảnh hưởng tới thiết kế agent.", a: "Tin tư vấn trả lời trong 48 giờ kể từ tin cuối của khách là miễn phí (gửi được tới 7 ngày). Agent nên trả lời nhanh; việc hẹn gọi lại quá 48 giờ cần kênh khác hoặc tin trả phí; quy trình chuyển người phải xong trong cửa sổ này.", rubric: "3 điểm: nêu đúng 48 giờ/miễn phí (1), 7 ngày (1), hệ quả thiết kế (1)." },
    { q: "Vì sao Zalo cá nhân không được đưa vào bản giao khách?", a: "Dùng thư viện không chính thức giả lập Zalo Web, trái điều khoản, số có thể bị khoá, đụng dữ liệu cá nhân; chỉ dùng thử nghiệm nội bộ với số phụ, cần chuyên gia duyệt.", rubric: "2 điểm." },
    { q: "Thiết kế quyền cho tình huống: agent lễ tân trên Zalo cần (a) tra đơn, (b) gửi email xác nhận từ cskh@shop.vn. Nêu cách cấu hình an toàn.", a: "(a) công cụ tra đơn chỉ đọc qua MCP/API, tự chạy; (b) bật Danh tính agent gắn hộp thư cskh@ (do admin), công cụ gửi mail luôn Hỏi trước, nhân viên duyệt trong Hộp duyệt; không dùng OAuth cá nhân; có nhật ký; trần chi phí kênh.", rubric: "4 điểm: (a) đúng (1), Danh tính agent cho hộp thư chung (1), duyệt bắt buộc (1), nhật ký/trần/không dùng OAuth cá nhân (1)." },
  ],
  homework: [
    "Lập phiếu go-live cho một kênh thật của công ty (Telegram hoặc Zalo OA): điều kiện, người chịu trách nhiệm, rủi ro, cách thử.",
    "Vẽ bảng \"quyền của ai\" cho 3 agent bạn định triển khai.",
    "Liệt kê 3 hệ thống nội bộ có thể nối qua MCP và công cụ ghi nào phải duyệt.",
  ],
  glossary: [
    ["Webhook", "Địa chỉ máy chủ để Zalo gọi vào khi có tin mới."],
    ["Socket Mode", "Slack giữ kết nối tới máy chủ, không cần tên miền công khai."],
    ["OAuth", "Cho ứng dụng dùng tài khoản Google/Microsoft của người dùng mà không lấy mật khẩu."],
    ["Redirect URI", "Địa chỉ nhà cung cấp OAuth trả người dùng về sau khi cho phép."],
    ["Máy đích được phép", "Host duy nhất mà một bí mật được gửi tới."],
    ["MCP client / server", "Bên dùng công cụ / bên cung cấp công cụ theo chuẩn MCP."],
    ["Danh tính agent", "Agent hành động bằng một tài khoản đã gắn, có kiểm toán."],
  ],
  slides: [
    { t: "Khung kênh: 6 tính chất", pts: ["Thread + bộ nhớ riêng mỗi khách", "Nhóm: chỉ trả lời khi gọi tên", "Duyệt ngay trong kênh", "Không dùng quyền của chủ agent", "Chi phí riêng · Thử kênh"], shot: "channel-tab" },
    { t: "Kênh vào máy chủ", diagram: "deploy" },
    { t: "Telegram và Slack", pts: ["Telegram: bot, không cần tên miền", "Slack: Socket Mode, 4 scope, /invite"], shot: "slack" },
    { t: "Zalo OA", shot: "zalo-oa", pts: ["Webhook có chữ ký, chống lặp", "48 giờ miễn phí, tối đa 7 ngày", "Cần OA + tên miền HTTPS", "Thử nghiệm: chưa thử OA thật"] },
    { t: "Zalo cá nhân", shot: "zalo-personal", pts: ["Không chính thức, có thể bị khoá số", "Mặc định tắt, chỉ admin", "Không giao khách"], note: "Nói rõ rủi ro pháp lý và điều khoản." },
    { t: "Google & Microsoft 365", shot: "google-m365", pts: ["3 mức: Chưa bật / Chỉ đọc / Đọc và ghi", "OAuth từng người", "Ghi luôn phải duyệt"] },
    { t: "Quyền của ai?", pts: ["Chat: chính người dùng", "Kênh: khách, không phải chủ", "Danh tính agent: tài khoản gắn, có nhật ký", "API/MCP: chủ khoá"], note: "Kể câu chuyện lỗ hổng 8.Z đã chặn." },
    { t: "Lab 5", pts: ["Bí mật + máy đích", "Kênh Telegram giả lập", "2 khách = 2 thread · duyệt trong kênh", "Phiếu go-live Zalo OA"] },
  ],
  plan: [
    { min: 10, act: "Ôn buổi 3–4", detail: "" },
    { min: 30, act: "Giảng: khung kênh, từng kênh", detail: "Slide 1–5." },
    { min: 25, act: "Giảng: Google/M365, MCP, bí mật, quyền của ai", detail: "Slide 6–7. Thảo luận câu chuyện 8.Z." },
    { min: 10, act: "Nghỉ giải lao", detail: "" },
    { min: 55, act: "Lab 5", detail: "Ký nhận 4 điểm kiểm." },
    { min: 25, act: "Kiểm tra + chữa phiếu go-live", detail: "6 câu; chữa 2 phiếu go-live trước lớp." },
    { min: 15, act: "Hỏi đáp tình huống thật của học viên", detail: "" },
    { min: 10, act: "Tổng kết", detail: "" },
  ],
  ask: [
    "Một khách hỏi agent trên Zalo \"cho tôi xem lịch họp của giám đốc\". Hệ thống nên phản ứng thế nào và vì sao?",
    "Nếu công ty chỉ có thể chọn một kênh đầu tiên, bạn chọn kênh nào và tiêu chí gì?",
  ],
  notes: [
    "Không dùng token thật trong lớp. Mọi token phát cho học viên là giả, máy chủ kênh là giả lập.",
    "Nhấn mạnh nhãn Thử nghiệm của các kênh: học viên không hứa với khách là \"đã chạy thật\".",
  ],
};
export default m;
