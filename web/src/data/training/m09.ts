import type { Module } from "./types";

const m: Module = {
  n: 9, slug: "tich-hop", track: "it", roles: ["it", "architect"], hours: 3,
  title: "Tích hợp: Agent Server API, SDK, MCP, A2A, OpenTelemetry, Gateway",
  short: "Tích hợp API, MCP, A2A",
  summary: "Nối Agent OS với hệ thống của doanh nghiệp bằng chuẩn mở: điều khiển agent bằng langgraph-sdk, gọi qua MCP và A2A, gửi và nhận trace OpenTelemetry, dùng Gateway như API OpenAI, nhận webhook có chữ ký, cấu hình máy chủ qua aos.json, Ứng dụng tuỳ chỉnh và CLI.",
  goals: [
    "Chọn đúng cổng tích hợp (API/SDK, MCP, A2A, OTLP, Gateway, webhook) cho 6 tình huống cho trước và nêu loại khoá cần dùng.",
    "Viết được (≤ 10 dòng) đoạn langgraph-sdk tạo thread, chạy agent, và tiếp tục sau khi duyệt.",
    "Liệt kê công cụ của máy chủ MCP /mcp/aos và lấy thẻ agent A2A bằng một lệnh HTTP.",
    "Gửi một trace OTLP vào Agent OS bằng khoá nhận trace và cấu hình xuất OTLP ra hệ thống khác.",
    "Kiểm chứng chữ ký webhook X-AOS-Signature ở phía nhận.",
  ],
  prereq: ["Buổi 1–8. Biết dùng curl hoặc một ngôn ngữ lập trình (Python khuyến nghị), hiểu HTTP, JSON."],
  sections: [
    {
      id: "ban-do", h: "Bản đồ tích hợp và xác thực",
      diagram: "integrations", diagramCaption: "Các cổng vào và các luồng Agent OS gọi ra",
      html: `<table><thead><tr><th>Khoá / phiên</th><th>Dạng</th><th>Gửi thế nào</th><th>Dùng cho</th></tr></thead><tbody>
<tr><td>Khoá cá nhân / dịch vụ</td><td><code>aos_…</code></td><td><code>X-Api-Key</code> hoặc <code>Authorization: Bearer</code></td><td>/api/v1, MCP, A2A, REST; quyền của chủ khoá. Khoá dịch vụ gắn một agent + <code>X-On-Behalf-Of</code> (danh tính agent).</td></tr>
<tr><td>Khoá nhận trace</td><td><code>ak_…</code></td><td><code>x-api-key</code> hoặc Bearer</td><td>Chỉ gọi được cổng nhận trace (/otel, /ingest); không đọc được dữ liệu.</td></tr>
<tr><td>Khoá ảo Gateway</td><td><code>vk-…</code></td><td><code>Authorization: Bearer</code></td><td>/v1/chat/completions, /v1/embeddings; ngân sách tháng riêng.</td></tr>
<tr><td>Phiên trình duyệt</td><td>cookie <code>aos_s</code></td><td>HttpOnly, Secure, SameSite=Lax, 7 ngày</td><td>Giao diện web.</td></tr>
</tbody></table>
<p>Tạo khoá: Cài đặt → API key. Nguyên tắc: mỗi ứng dụng một khoá; khoá dịch vụ thay cho khoá cá nhân trong hệ thống tự động; lưu khoá trong kho bí mật của ứng dụng gọi, không trong mã nguồn. Tài liệu OpenAPI ở <code>/openapi.json</code> có mô tả securitySchemes theo từng route.</p>`,
    },
    {
      id: "agent-server", h: "Agent Server API và langgraph-sdk",
      shot: "agent-chat-ui", shotCaption: "agent-chat-ui (giao diện mẫu của LangChain) chat với agent của Agent OS, kể cả bước duyệt",
      html: `<p>Agent OS phơi API tương thích LangGraph tại <code>/api/v1</code>: <code>/assistants</code>, <code>/threads</code>, <code>/threads/{id}/runs</code> (có stream), <code>/runs</code> không trạng thái, <code>/runs/crons</code>, <code>/store</code> (namespace theo workspace và người). Stream modes: <code>values</code>, <code>updates</code>, <code>messages</code>, <code>messages/partial</code>, <code>events</code>, <code>metadata</code>, kết thúc bằng <code>end</code>/<code>error</code>. Thư viện chính thức <code>langgraph-sdk</code> (Python, JS) và giao diện mẫu <code>agent-chat-ui</code> dùng trực tiếp được; luồng bị đứt nối lại được.</p>
<pre><code>from langgraph_sdk import get_client
c = get_client(url="https://agent.congty.vn/api/v1", api_key=KEY)
t = await c.threads.create()
r = await c.runs.wait(t["thread_id"], AGENT_ID,
      input={"messages": [{"role": "user", "content": "Tra đơn DH1023"}]})
# Nếu run dừng chờ duyệt: tiếp tục bằng
r = await c.runs.wait(t["thread_id"], AGENT_ID, command={"resume": QUYET_DINH})</code></pre>
<p>Giải thích: <code>AGENT_ID</code> là id assistant (agent); <code>KEY</code> là khoá <code>aos_</code> đọc từ biến môi trường; <code>runs.wait</code> chờ kết quả cuối, muốn nhận từng phần dùng <code>runs.stream(..., stream_mode="messages")</code>. Run chạy bằng quyền của chủ khoá; công cụ "Hỏi trước" vẫn dừng như trong giao diện.</p>`,
    },
    {
      id: "mcp-a2a", h: "MCP và A2A",
      shot: "mcp-tools", shotCaption: "Danh sách công cụ của máy chủ MCP Agent OS",
      html: `<p>Agent OS là máy chủ MCP dùng SDK chính thức <code>mcp</code> 2.3 theo đặc tả 2026-07-28, chế độ không trạng thái (vẫn nhận client cũ):</p>
<ul>
<li><code>/mcp</code>: công cụ của workspace; <code>/mcp/agents/{id}</code>: một agent như một công cụ (địa chỉ ở nút Chia sẻ của agent).</li>
<li><code>/mcp/aos</code>: 15 công cụ quản trị Agent OS (cũng là bộ công cụ của Trợ lý Ctrl+I). Đọc (11): <code>query_runs</code>, <code>get_trace</code>, <code>get_thread</code>, <code>list_issues</code>, <code>get_prompt</code>, <code>get_pack</code>, <code>list_pack_versions</code>, <code>get_costs</code>, <code>list_datasets</code>, <code>get_experiment</code>, <code>compare_experiments</code>. Ghi (4): <code>propose_prompt_edit</code>, <code>create_dataset_from_runs</code>, <code>draft_evaluator</code>, <code>run_experiment</code>.</li>
</ul>
<p>Mọi lời gọi chạy bằng quyền của chủ khoá; người chỉ có quyền Được dùng agent vẫn gọi được agent qua MCP. Khai báo trong một MCP client (ví dụ minh hoạ, cú pháp tuỳ client):</p>
<pre><code>{"mcpServers": {"agentos": {"url": "https://agent.congty.vn/mcp/aos",
  "headers": {"X-Api-Key": "\${AOS_API_KEY}"}}}}</code></pre>
<p><strong>A2A 1.0</strong> (SDK chính thức <code>a2a-sdk</code> 1.2): thẻ agent tại <code>GET /a2a/{id}/.well-known/agent-card.json</code>, gọi JSON-RPC <code>POST /a2a/{id}</code>, danh sách <code>GET /a2a</code>; tác vụ lưu trong PostgreSQL; tôn trọng chia sẻ và chặn với workspace demo.</p>`,
    },
    {
      id: "otel", h: "OpenTelemetry vào và ra, traceparent",
      shot: "otlp-trace", shotCaption: "Trace nhận qua OTLP theo quy ước GenAI",
      html: `<ul>
<li><strong>Nhận</strong>: <code>POST /otel/v1/traces</code> (protobuf hoặc JSON) với khoá <code>ak_</code>; hiểu quy ước OpenTelemetry GenAI (bản 1.44 đã ghim), OpenInference, Traceloop, LangSmith-OTel. Kiểu LangSmith: <code>POST /ingest/runs</code>. Google ADK phát OTel nên gửi được vào đây.</li>
<li><strong>Gửi ra</strong>: Thành viên &amp; quyền → Xuất trace sang OpenTelemetry (hoặc <code>GET/PUT /api/exports/otlp</code>, <code>POST /api/exports/otlp/run</code>): địa chỉ collector, header xác thực (lấy từ kho bí mật), tuỳ chọn gửi cả nội dung; Gửi ngay, Gửi lại 24 giờ qua.</li>
<li><strong>traceparent</strong> (W3C): header traceparent đến được nối tiếp vào trace của Agent OS; lời gọi ra (httpx, requests, urllib) mang traceparent đi tiếp. Một yêu cầu đi qua nhiều hệ thống hiện thành một chuỗi.</li>
</ul>
<p>Cấu hình phía ứng dụng gửi (biến môi trường chuẩn của OpenTelemetry):</p>
<pre><code>OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=https://agent.congty.vn/otel/v1/traces
OTEL_EXPORTER_OTLP_HEADERS=x-api-key=ak_...</code></pre>`,
    },
    {
      id: "gateway-webhook", h: "Gateway kiểu OpenAI và webhook có chữ ký",
      html: `<p>Ứng dụng nội bộ có thể gọi model qua Gateway để hưởng trần chi phí, che dữ liệu cá nhân, định tuyến, ghi chi phí: <code>POST /v1/chat/completions</code>, <code>/v1/embeddings</code>, <code>GET /v1/models</code> với khoá ảo <code>vk-</code>; <code>"model": "auto"</code> để Gateway tự định tuyến; header <code>X-Gateway-*</code> cho chính sách theo khách.</p>
<pre><code>curl https://agent.congty.vn/v1/chat/completions \\
  -H "Authorization: Bearer $VK" -H "Content-Type: application/json" \\
  -d '{"model":"auto","messages":[{"role":"user","content":"Tóm tắt..."}]}'</code></pre>
<p><strong>Webhook ra</strong> (cảnh báo, sự kiện prompt): kiểm chứng ở phía nhận:</p>
<pre><code>msg = ts + "." + body                      # ts = header X-AOS-Timestamp
ok = hmac.compare_digest("sha256=" + hmac_sha256(KHOA, msg), sig)
ok = ok and abs(now - int(ts)) &lt; 300       # chống phát lại</code></pre>`,
    },
    {
      id: "may-chu", h: "Cấu hình máy chủ, Ứng dụng tuỳ chỉnh, CLI",
      shot: "server-headers", shotCaption: "Cài đặt máy chủ: header cấu hình lúc chạy và header ghi vào nhật ký",
      html: `<ul>
<li><strong>aos.json</strong> (đường dẫn qua <code>AOS_CONFIG</code>, có JSON schema): <code>http</code> (header cấu hình lúc chạy theo danh sách cho phép; header ghi vào nhật ký, đã che; thông tin đăng nhập không bao giờ được nhận qua header), <code>auth</code> (luật xác thực tự viết, buổi 10), <code>checkpointer</code> (memory, sqlite, postgres hoặc module tuỳ chỉnh có kiểm tra khói), <code>cache</code> (bộ nhớ đệm phía máy chủ kiểu stale-while-revalidate, cache node LangGraph trên Postgres), <code>env</code>. Mã chỉ nạp từ tệp cấu hình/biến môi trường, không bao giờ qua HTTP.</li>
<li><strong>Ứng dụng tuỳ chỉnh</strong>: web app nhỏ lưu trong Postgres, chạy trong iframe hộp kín không mạng, gọi dữ liệu qua cầu <code>/api/app-call/{id}</code> với quyền của người đang mở; thao tác ghi phải bấm Cho phép (sửa ở cổng 9.Z). Nên có chuyên gia duyệt.</li>
<li><strong>CLI</strong> <code>aos_cli.py</code> (biến <code>AOS_URL</code>, <code>AOS_API_KEY</code>): <code>auth status</code>; <code>sandbox list|create|delete|exec|put|get|ls|rm|link|snapshot|service</code>; <code>volume</code>; <code>apps</code>.</li>
<li><strong>Xuất Parquet</strong> hằng ngày ra thư mục hoặc S3 cho BI.</li>
</ul>`,
    },
  ],
  lab: {
    title: "Lab 9: Bốn đường tích hợp trong một giờ",
    time: "65 phút",
    setup: "Máy lab có Python 3.12 và gói langgraph-sdk, opentelemetry-sdk (giảng viên cài sẵn); thư mục lab9 có script mẫu; máy nhận webhook thử của lớp.",
    steps: [
      "Cài đặt → API key → <strong>+ API Key</strong> loại Cá nhân; lưu vào biến môi trường <code>AOS_API_KEY</code> trên máy (không dán vào chat, không ghi vào tệp mã).",
      "Chạy <code>lab9_sdk.py</code>: tạo thread, chạy agent CSKH với câu \"đổi size đơn DH1023\". Khi run dừng chờ duyệt, đọc interrupt in ra, rồi tiếp tục với quyết định \"approve\". Mở Tracing xem run vừa tạo từ SDK.",
      "Gọi <code>POST /mcp/aos</code> với yêu cầu JSON-RPC <code>tools/list</code> (script <code>lab9_mcp.sh</code>); đếm số công cụ đọc/ghi. Gọi tiếp <code>get_costs</code>.",
      "Lấy thẻ A2A: <code>GET /a2a/&lt;id agent&gt;/.well-known/agent-card.json</code>; đọc tên, kỹ năng, phương thức xác thực trong thẻ.",
      "Tạo khoá nhận trace (loại Key chỉ ghi trace). Chạy <code>lab9_otel.py</code> (OpenTelemetry SDK gửi 1 span GenAI). Tìm trace trong project tương ứng. Thử dùng khoá này gọi <code>/api/v1/threads</code> và ghi lại lỗi.",
      "Gọi Gateway <code>/v1/chat/completions</code> với khoá ảo của lớp và <code>\"model\": \"auto\"</code>; tìm dòng chi phí trong LLM Gateway.",
      "Ở máy nhận webhook: chạy <code>lab9_verify.py</code> để kiểm chữ ký các webhook của Lab 6; sửa 1 ký tự body và xác nhận bị từ chối.",
    ],
    result: "Run tạo từ SDK có điểm dừng duyệt và tiếp tục được; danh sách 15 công cụ MCP (11 đọc, 4 ghi); thẻ A2A đọc được; trace OTLP hiện trong Tracing; khoá ak_ bị từ chối ở /api/v1; Gateway ghi chi phí; webhook giả mạo bị từ chối.",
    shot: "a2a", shotAlt: "Thẻ agent A2A và kết quả gọi",
    check: ["Trace của run từ SDK có metadata cho thấy gọi qua API và có bước interrupt.", "Học viên giải thích vì sao khoá ak_ không đọc được /api/v1.", "Script kiểm chữ ký dùng so sánh thời gian hằng (compare_digest) và kiểm timestamp.", "Không có khoá nào xuất hiện trong mã, ảnh chụp hoặc chat."],
  },
  pitfalls: [
    ["401 khi gọi API.", "Sai header (X-Api-Key hoặc Authorization: Bearer), khoá bị vô hiệu hoá, hoặc dùng khoá ak_ cho API đọc."],
    ["403 khi gọi agent qua MCP/A2A.", "Chủ khoá không có quyền Được dùng agent đó; agent riêng tư; hoặc đang gọi vào workspace demo."],
    ["Run qua SDK chờ mãi.", "Run đang dừng ở interrupt (công cụ Hỏi trước). Đọc trạng thái thread, tiếp tục bằng command resume hoặc duyệt trong Hộp duyệt."],
    ["Trace OTLP không hiện.", "Sai đường dẫn (/otel/v1/traces), sai khoá, project/môi trường gắn với khoá khác chỗ đang xem; JSON không đúng định dạng OTLP."],
    ["Ứng dụng tuỳ chỉnh không ghi được dữ liệu.", "Đúng thiết kế: cầu /api/app-call chặn thao tác nguy hiểm; ghi phải được người dùng Cho phép."],
    ["Muốn nạp checkpointer tuỳ chỉnh qua API.", "Không hỗ trợ có chủ đích: mã chỉ nạp từ aos.json/biến môi trường để tránh thực thi mã từ xa."],
  ],
  quiz: [
    { q: "Ứng dụng di động của công ty cần chat với agent và hiển thị từng token. Dùng cổng nào?", options: ["/v1/chat/completions", "/api/v1 runs stream (langgraph-sdk)", "/otel/v1/traces", "/ingest/runs"], a: "/api/v1 với runs.stream, stream_mode messages.", rubric: "1 điểm." },
    { q: "Khoá nào chỉ dùng để gửi trace và không đọc được dữ liệu?", options: ["aos_", "ak_", "vk-", "cookie aos_s"], a: "ak_.", rubric: "1 điểm." },
    { q: "Máy chủ MCP /mcp/aos có bao nhiêu công cụ? Kể 2 công cụ ghi.", a: "15 công cụ (11 đọc, 4 ghi). Ghi: propose_prompt_edit, create_dataset_from_runs, draft_evaluator, run_experiment.", rubric: "2 điểm: số đúng (1), 2 công cụ ghi đúng (1)." },
    { q: "Viết các bước kiểm chữ ký webhook ở phía nhận.", a: "Lấy X-AOS-Timestamp và X-AOS-Signature; tính HMAC-SHA256(khoá, ts + \".\" + body thô); so \"sha256=\"+hex với chữ ký bằng so sánh thời gian hằng; kiểm |now − ts| nhỏ (vd 5 phút); sai thì trả 401 và không xử lý.", rubric: "4 điểm: chuỗi ký đúng (1), dùng body thô (1), so sánh an toàn (1), kiểm timestamp (1)." },
    { q: "Một run gọi qua SDK dừng ở công cụ Hỏi trước. Nêu hai cách để nó chạy tiếp.", a: "Gửi run mới với command resume (quyết định duyệt) qua SDK; hoặc người có quyền duyệt trong Hộp duyệt/thẻ duyệt.", rubric: "2 điểm." },
    { q: "Vì sao checkpointer tuỳ chỉnh và luật xác thực tự viết chỉ nạp từ aos.json/biến môi trường mà không qua HTTP?", a: "Để không ai có thể đưa mã vào máy chủ qua API (thực thi mã từ xa); chỉ người có quyền vào máy chủ mới đổi được; có kiểm tra khói khi nạp.", rubric: "2 điểm." },
  ],
  homework: [
    "Chọn một hệ thống của công ty, viết thiết kế tích hợp 1 trang: cổng dùng, loại khoá, luồng dữ liệu, xử lý lỗi, nhật ký.",
    "Viết script (≤ 40 dòng) gọi agent qua langgraph-sdk theo lô 10 câu hỏi và lưu kết quả ra CSV.",
    "Nối một ứng dụng AI có sẵn (hoặc script mẫu) gửi trace OTLP vào môi trường dev.",
  ],
  glossary: [
    ["Agent Server API", "API chuẩn LangGraph ở /api/v1."],
    ["Stream mode", "Kiểu dữ liệu nhận dần: values, updates, messages, events…"],
    ["Command resume", "Lệnh tiếp tục một run đang dừng ở interrupt."],
    ["MCP server", "Máy chủ cung cấp công cụ theo chuẩn MCP."],
    ["Agent card", "Tệp JSON mô tả agent theo chuẩn A2A."],
    ["OTLP", "Giao thức gửi dữ liệu OpenTelemetry."],
    ["GenAI semconv", "Quy ước đặt tên thuộc tính trace cho ứng dụng AI tạo sinh."],
    ["traceparent", "Header W3C nối một trace qua nhiều hệ thống."],
    ["aos.json", "Tệp cấu hình máy chủ: http, auth, checkpointer, cache, env."],
    ["HMAC", "Chữ ký thông điệp bằng khoá chung."],
  ],
  slides: [
    { t: "Bản đồ tích hợp", diagram: "integrations" },
    { t: "Bốn loại khoá", pts: ["aos_: API, MCP, A2A (quyền chủ khoá)", "ak_: chỉ gửi trace", "vk-: Gateway, ngân sách riêng", "cookie aos_s: giao diện"] },
    { t: "Agent Server API", pts: ["/api/v1: assistants, threads, runs, crons, store", "Stream: values, updates, messages, events…", "langgraph-sdk · agent-chat-ui"], code: "c = get_client(url=URL, api_key=KEY)\nt = await c.threads.create()\nr = await c.runs.wait(t[\"thread_id\"], AGENT_ID, input=...)" },
    { t: "Duyệt qua SDK", code: "await c.runs.wait(tid, AGENT_ID,\n    command={\"resume\": QUYET_DINH})", pts: ["Công cụ Hỏi trước vẫn dừng", "Hoặc duyệt trong Hộp duyệt"] },
    { t: "MCP", shot: "mcp-tools", pts: ["/mcp · /mcp/aos (15 công cụ) · /mcp/agents/{id}", "mcp 2.3, không trạng thái", "Quyền của chủ khoá"] },
    { t: "A2A", pts: ["/a2a/{id}/.well-known/agent-card.json", "POST /a2a/{id} JSON-RPC", "a2a-sdk 1.2"], shot: "a2a" },
    { t: "OpenTelemetry", pts: ["Vào: /otel/v1/traces (ak_)", "Ra: collector Grafana, Datadog…", "traceparent nối chuỗi"], shot: "otlp-trace" },
    { t: "Gateway và webhook", code: "POST /v1/chat/completions  {\"model\":\"auto\"}\nX-AOS-Signature: sha256=HMAC(key, ts.\".\".body)" },
    { t: "Máy chủ và mở rộng", pts: ["aos.json: http, auth, checkpointer, cache, env", "Ứng dụng tuỳ chỉnh: iframe kín, ghi phải Cho phép", "CLI: sandbox, volume, apps", "Parquet / S3"] },
    { t: "Lab 9", pts: ["SDK + interrupt", "MCP tools/list · A2A card", "OTLP vào · Gateway", "Kiểm chữ ký webhook"] },
  ],
  plan: [
    { min: 10, act: "Khởi động", detail: "Hỏi lớp: hệ thống nào ở công ty cần nói chuyện với agent? Liệt kê lên bảng, gắn vào bản đồ tích hợp." },
    { min: 30, act: "Giảng: bản đồ, khoá, Agent Server, SDK", detail: "Slide 1–4. Live-code đoạn SDK." },
    { min: 25, act: "Giảng: MCP, A2A, OTel, Gateway, webhook, aos.json", detail: "Slide 5–9." },
    { min: 10, act: "Nghỉ giải lao", detail: "" },
    { min: 65, act: "Lab 9", detail: "Theo cặp: một người chạy lệnh, một người đọc trace; đổi vai giữa chừng." },
    { min: 25, act: "Kiểm tra", detail: "" },
    { min: 15, act: "Tổng kết, giao bài thiết kế tích hợp", detail: "" },
  ],
  ask: [
    "Khi nào nên gọi agent qua MCP thay vì qua Agent Server API?",
    "Rủi ro gì nếu nhúng khoá aos_ cá nhân vào một ứng dụng chạy trên máy nhân viên?",
    "Gửi trace có nội dung (input/output) ra Datadog có ảnh hưởng gì tới dữ liệu cá nhân?",
  ],
  notes: [
    "Kiểm tra trước giờ học: script lab9 chạy được với khoá của một tài khoản thử; máy nhận webhook đang chạy.",
    "Nhắc nghiêm: không dán khoá vào chat, ảnh, slide. Dùng biến môi trường.",
    "Học viên không quen code: cho làm theo cặp với kỹ sư, tập trung đọc kết quả và trace.",
  ],
};
export default m;
