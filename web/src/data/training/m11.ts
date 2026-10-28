import type { Module } from "./types";

const m: Module = {
  n: 11, slug: "trien-khai-van-hanh", track: "it", roles: ["it", "architect"], hours: 3,
  title: "Triển khai, vận hành và xử lý sự cố",
  short: "Triển khai và sự cố",
  summary: "Cài và nâng cấp Agent OS trên máy chủ riêng của khách theo quy trình có sao lưu và quay lại; sao lưu, khôi phục và diễn tập; kiểm thử và cổng chất lượng; giám sát vận hành; và bộ cẩm nang xử lý 12 sự cố thường gặp.",
  goals: [
    "Mô tả bố cục triển khai tham chiếu (dịch vụ, cổng, thư mục, biến môi trường, bộ hẹn giờ) và vai trò của từng phần.",
    "Thực hiện đúng quy trình phát hành 5 bước có sao lưu và quay lại, không đụng dịch vụ ngoài phạm vi.",
    "Diễn tập khôi phục dữ liệu từ bản sao lưu vào một cơ sở dữ liệu mới và kiểm chứng khớp dữ liệu.",
    "Chạy bộ kiểm tra chất lượng (kiểm thử theo từng tệp, kiểm rò rỉ, check-a7) và đọc kết quả.",
    "Chẩn đoán và xử lý ít nhất 8/12 sự cố trong cẩm nang trong bài thực hành có kịch bản.",
  ],
  prereq: ["Buổi 9–10. Quen Linux (systemd, journalctl), Docker, PostgreSQL cơ bản, Git."],
  sections: [
    {
      id: "bo-cuc", h: "Bố cục triển khai tham chiếu",
      diagram: "aos-arch", diagramCaption: "Kiến trúc trên một máy chủ riêng",
      html: `<table><thead><tr><th>Phần</th><th>Triển khai tham chiếu</th></tr></thead><tbody>
<tr><td>API</td><td>dịch vụ systemd <code>deepagent-api</code>: uvicorn chỉ nghe <code>127.0.0.1:8020</code>; mã ở <code>/opt/agentos</code> (là một kho git); môi trường ảo Python 3.12; <code>EnvironmentFile</code> trỏ tới tệp <code>.env</code> quyền 600.</td></tr>
<tr><td>Worker</td><td>dịch vụ <code>deepagent-worker</code> (hàng đợi run nền); bộ hẹn giờ kiểm nhịp tim mỗi phút, quá 90 giây không có nhịp thì khởi động lại.</td></tr>
<tr><td>Cơ sở dữ liệu</td><td>PostgreSQL + pgvector (container Docker); checkpoint, store, trace, cấu hình…</td></tr>
<tr><td>Cổng</td><td>Caddy: HTTPS, phục vụ <code>/chat/</code> (bản build tĩnh của Agent OS Chat), chuyển tiếp vào 127.0.0.1.</td></tr>
<tr><td>Sandbox</td><td>Docker với runtime gVisor <code>runsc</code> (biến <code>AOS_SBX_RUNTIME</code>; nếu không có runsc hệ thống lùi về runc, kém cách ly hơn: phải kiểm).</td></tr>
<tr><td>Bộ hẹn giờ</td><td>sao lưu PostgreSQL 02:30, sao lưu ngoài máy 03:30, xuất Parquet 02:00, kiểm worker mỗi phút; báo cáo Insights T2 08:00.</td></tr>
</tbody></table>
<p><strong>Biến môi trường quan trọng</strong>: <code>AOS_ENV=prod</code>, <code>AOS_SECRET_KEYS</code>, <code>PG_URI</code>, <code>AGENTOS_SECRET</code>, khoá nhà cung cấp (<code>GOOGLE_API_KEY</code>, <code>OPENAI_API_KEY</code>), trần theo nhà cung cấp (<code>OPENAI_BUDGET_USD</code>, <code>GEMINI_BUDGET_USD_MONTH</code>), <code>AOS_DEFAULT_MODEL</code>, <code>AOS_CONFIG</code> (aos.json), <code>AOS_AUTH</code>, <code>AOS_SBX_RUNTIME</code>. Không bao giờ in giá trị ra log hay chat.</p>
<p>Không có công cụ migration riêng: mỗi mô-đun tự <code>CREATE TABLE IF NOT EXISTS</code> / <code>ADD COLUMN IF NOT EXISTS</code> khi khởi động (có khoá advisory để hai tiến trình không chạy đồng thời). Vì vậy <strong>sao lưu trước mọi lần nâng cấp</strong> là bắt buộc.</p>`,
    },
    {
      id: "phat-hanh", h: "Quy trình phát hành an toàn",
      diagram: "release", diagramCaption: "Năm bước phát hành, có đường quay lại",
      html: `<ol>
<li><strong>Sao lưu</strong>: <code>pg_dump -Fc</code> + nén mã + <code>.env</code>, quyền 700, đặt tên <code>pre_&lt;tên-đợt&gt;</code>, kéo một bản ra ngoài máy.</li>
<li><strong>Đánh dấu</strong>: <code>git commit</code> + tag trên máy chủ để có điểm quay lại mã.</li>
<li><strong>Áp dụng</strong>: mã mới; thư viện theo <code>requirements.lock</code> (227 gói ghim chính xác) bằng <code>uv pip sync</code>; cập nhật <code>.env</code>; build Agent OS Chat; dựng ảnh sandbox nếu đổi; chỉnh CSP nếu cần (ví dụ xem trước PDF trong chat).</li>
<li><strong>Khởi động lại đúng phạm vi</strong>: chỉ <code>deepagent-api</code> và <code>deepagent-worker</code>. Không khởi động lại máy, không đụng dịch vụ khác cùng máy (Caddy dùng chung, ứng dụng khác).</li>
<li><strong>Kiểm chứng</strong>: <code>/health</code> (trả <code>{"ok": true}</code> khi có khoá; không khoá thì 401 là bình thường), thử nhanh đăng nhập, chat, một run có công cụ, một run có duyệt; xem log; chạy kiểm rò rỉ sang workspace demo; theo dõi 24 giờ.</li>
</ol>
<p><strong>Quay lại</strong>: <code>git checkout &lt;tag&gt;</code> (hoặc giải nén mã), <code>pg_restore --clean</code> vào cơ sở dữ liệu, khởi động lại api + worker. Tính năng mới luôn <strong>tắt mặc định</strong> với workspace demo và bật dần theo workspace.</p>`,
    },
    {
      id: "sao-luu", h: "Sao lưu, khôi phục và diễn tập",
      html: `<ul>
<li><strong>Tại máy</strong>: dump PostgreSQL hằng đêm (giữ 7 ngày / 4 tuần) + thư mục tệp người dùng; Cài đặt → Sao lưu &amp; Worker → Sao lưu ngay.</li>
<li><strong>Ngoài máy</strong> (restic, mã hoá phía mình): 03:30 hằng đêm tới Backblaze B2 hoặc Cloudflare R2 (~1 USD/tháng): DB, mã, <code>.env</code>, tệp tải lên; giữ 7 ngày / 4 tuần / 6 tháng; tự kiểm thử một phần mỗi Chủ nhật; kết quả ghi <code>status.json</code>. Cấu hình ở tệp bí mật riêng quyền 600 (<code>RESTIC_REPOSITORY</code>, <code>RESTIC_PASSWORD_FILE</code>, khoá S3). Mất mật khẩu kho = mất mọi bản sao lưu.</li>
<li><strong>Khôi phục</strong>: <code>offsite_restore.sh --list</code>; <code>--snapshot latest --target /tmp/khoiphuc</code>; vào <strong>CSDL mới để kiểm tra</strong> bằng <code>--db ten_moi</code>; ghi đè CSDL đang chạy bị chặn trừ khi có đủ <code>--into-live --yes-overwrite-live</code> và đã tắt API.</li>
<li><strong>Diễn tập</strong> mỗi quý: sao lưu → khôi phục vào CSDL mới → so số dòng các bảng chính → ghi biên bản. Đã diễn tập trên máy dev: sao lưu, xoá, khôi phục, dữ liệu khớp. Chưa bật thật vì chờ tài khoản kho của khách.</li>
<li>Bản sao lưu chứa dữ liệu đã mã hoá: phải giữ <strong>khoá gốc tương ứng</strong> cùng thời kỳ (buổi 10).</li>
</ul>`,
    },
    {
      id: "chat-luong", h: "Kiểm thử và cổng chất lượng",
      html: `<table><thead><tr><th>Kiểm tra</th><th>Cách chạy</th><th>Mục đích</th></tr></thead><tbody>
<tr><td>Kiểm thử máy chủ</td><td><code>bash tests/run_all.sh [mẫu]</code>: mỗi tệp một tiến trình, có trần RAM</td><td>779 bài đạt ở cổng 9.Z. Chạy gộp một tiến trình từng hết bộ nhớ sau ~650 bài.</td></tr>
<tr><td>Kiểm thử Chat</td><td><code>npm test</code>, <code>npm run lint</code>, <code>npm run build</code></td><td>118/118</td></tr>
<tr><td>check-a7</td><td><code>scripts/check-a7.sh</code> trong agentos-chat</td><td>Không có khoá trong bản build, không dịch vụ cloud ngoài ý muốn.</td></tr>
<tr><td>Kiểm rò rỉ demo</td><td><code>tests/tools/demo_leak_audit.py BASE CHỦ DEMO</code> (mật khẩu qua biến môi trường)</td><td>Tài khoản demo không thấy dữ liệu workspace khác (220 đường, 0 rò).</td></tr>
<tr><td>Giao diện</td><td>Playwright không giao diện</td><td>0 lỗi console, đúng ngôn ngữ.</td></tr>
</tbody></table>
<p>AI giả (fake LLM) cho phép chạy toàn bộ kiểm thử với chi phí 0; các nhãn như <code>[json]</code>, <code>[tool:x]</code>, <code>[builder]</code>, <code>[engine]</code>, <code>[insights]</code> điều khiển model giả trả lời theo kịch bản. Bài học thật: một bài kiểm thử từng làm treo WSL vì ăn 7,4 GB RAM; từ đó mọi lần chạy đều có trần RAM.</p>`,
    },
    {
      id: "su-co", h: "Cẩm nang 12 sự cố",
      html: `<table><thead><tr><th>Triệu chứng</th><th>Kiểm tra</th><th>Xử lý</th></tr></thead><tbody>
<tr><td>Trang trắng / 502</td><td><code>systemctl status deepagent-api</code>, <code>journalctl -u deepagent-api -n 100</code></td><td>Sửa lỗi khởi động (thường thiếu biến môi trường, thư viện); quay lại tag nếu vừa phát hành.</td></tr>
<tr><td>prod không khởi động, báo thiếu khoá</td><td>log khởi động</td><td>Đặt <code>AOS_SECRET_KEYS</code> (cố ý chặn).</td></tr>
<tr><td>Run xếp hàng mãi</td><td>trạng thái worker, nhịp tim</td><td>Khởi động lại worker; kiểm khoá DB treo.</td></tr>
<tr><td>Agent báo lỗi model 503 / hết giờ</td><td>trace bước model</td><td>Dự phòng model đã tự thử; kiểm trạng thái nhà cung cấp, đổi model mặc định.</td></tr>
<tr><td>Agent im lặng, chat có dải đỏ</td><td>LLM Gateway, trần</td><td>Chạm trần chặn: tăng trần hoặc chờ kỳ mới.</td></tr>
<tr><td>Kênh im lặng</td><td>thẻ Kênh, bí mật, log webhook</td><td>Kênh tắt, bí mật sai máy đích, chữ ký sai, đồng hồ lệch, quá 48 giờ (Zalo).</td></tr>
<tr><td>Google/Microsoft báo lỗi quyền</td><td>Kết nối công cụ</td><td>Token hết hạn: Làm mới; thiếu quyền: kết nối lại.</td></tr>
<tr><td>Chạy code lỗi</td><td>Docker, runsc</td><td>Thiếu runsc / ảnh sandbox chưa dựng; không chạy sandbox bằng runc ở prod.</td></tr>
<tr><td>Đĩa đầy</td><td><code>df -h</code>, thư mục sao lưu, log Docker</td><td>Dọn bản sao lưu cũ theo chính sách, xoay log; theo dõi dung lượng (từng làm dừng máy dev).</td></tr>
<tr><td>Bộ nhớ tăng vọt khi xử lý ảnh</td><td>giám sát RAM</td><td>OCR đã thu nhỏ ≤ 2048 px và chạy từng ảnh; giới hạn RAM dịch vụ.</td></tr>
<tr><td>Cảnh báo không tới</td><td>outbox: lần thử, lỗi cuối</td><td>Sửa kênh nhận; outbox thử lại 1/5/15 phút rồi đánh dấu thất bại.</td></tr>
<tr><td>Dữ liệu hỏng / xoá nhầm</td><td>bản sao lưu gần nhất</td><td>Khôi phục vào CSDL mới, so sánh, rồi mới quyết định ghi đè có xác nhận hai lớp.</td></tr>
</tbody></table>`,
    },
  ],
  lab: {
    title: "Lab 11: Phát hành, quay lại và diễn tập khôi phục trên máy lab",
    time: "70 phút",
    setup: "Mỗi nhóm có một máy lab Linux (hoặc WSL) với bản Agent OS cục bộ chạy AI giả, quyền sudo trên máy đó; KHÔNG dùng máy chủ thật của khách.",
    steps: [
      "Đọc trạng thái: <code>systemctl status</code> (hoặc tiến trình cục bộ), log 50 dòng cuối, gọi <code>/health</code> có và không có khoá (giải thích 401).",
      "Sao lưu: tạo dump <code>pg_dump -Fc</code> đặt tên <code>pre_lab11</code>, nén mã và <code>.env</code> (quyền 700). Ghi lại đường dẫn.",
      "Đánh dấu: <code>git -C &lt;thư mục mã&gt; tag lab11_truoc</code>.",
      "Áp dụng một \"bản nâng cấp\" giảng viên phát (thay đổi nhỏ cấu hình/mã), khởi động lại chỉ API và worker, kiểm chứng 4 thao tác (đăng nhập, chat, run có công cụ, run có duyệt).",
      "Kịch bản lỗi: giảng viên làm hỏng (ví dụ bỏ một biến môi trường bắt buộc). Chẩn đoán bằng log, rồi <strong>quay lại</strong>: checkout tag + khôi phục DB từ <code>pre_lab11</code>. Ghi thời gian quay lại.",
      "Diễn tập khôi phục: khôi phục <code>pre_lab11</code> vào CSDL mới <code>aos_restore_lab</code>; so số dòng 5 bảng chính (agents, lg_threads, runs/trace, datasets, users) giữa 2 CSDL.",
      "Chạy <code>bash tests/run_all.sh</code> với một mẫu nhỏ (ví dụ <code>context_hub</code>) và đọc kết quả.",
      "Rút thăm 3 sự cố trong cẩm nang; giảng viên dựng lại trên máy lab; nhóm chẩn đoán và xử lý, ghi biên bản.",
    ],
    result: "Có bản sao lưu và tag trước nâng cấp; nâng cấp kiểm chứng đủ 4 thao tác; quay lại thành công với thời gian ghi nhận; CSDL khôi phục khớp số dòng; kết quả kiểm thử đọc được; 3 biên bản sự cố.",
    check: ["Bản sao lưu có quyền 700 và nằm ngoài thư mục mã.", "Nhóm không khởi động lại máy hay dịch vụ ngoài API/worker.", "Biên bản so số dòng 5 bảng khớp.", "Mỗi biên bản sự cố có: triệu chứng, kiểm tra, nguyên nhân, xử lý, cách phòng."],
  },
  pitfalls: [
    ["Nâng cấp xong mới nhớ chưa sao lưu.", "Dừng lại, sao lưu ngay trạng thái hiện tại trước khi thử sửa tiếp; lần sau dùng danh sách kiểm 5 bước."],
    ["pg_restore báo lỗi quyền/đối tượng đã tồn tại.", "Dùng --clean vào đúng CSDL, đúng người dùng; diễn tập vào CSDL mới trước."],
    ["Khôi phục xong dữ liệu mã hoá không đọc được.", "Thiếu khoá gốc của thời kỳ sao lưu. Luôn lưu khoá cũ kèm bản sao lưu cũ."],
    ["Chạy toàn bộ kiểm thử làm treo máy.", "Dùng run_all.sh (mỗi tệp một tiến trình) và trần RAM; không chạy gộp."],
    ["Khởi động lại Caddy dùng chung làm sập dịch vụ khác.", "Chỉ khởi động lại API và worker; thay đổi Caddy phải lên kế hoạch riêng."],
  ],
  quiz: [
    { q: "Agent OS tạo bảng mới khi nâng cấp bằng cách nào?", options: ["Công cụ migration riêng", "Mỗi mô-đun tự CREATE TABLE IF NOT EXISTS khi khởi động", "Admin chạy SQL tay", "Tự tạo khi người dùng mở trang"], a: "Mỗi mô-đun tự tạo/sửa bảng khi khởi động (có khoá advisory).", rubric: "1 điểm." },
    { q: "Gọi /health không kèm khoá nhận 401. Điều đó nghĩa là:", options: ["API đang lỗi", "Bình thường: /health cần khoá", "Caddy chặn", "Cơ sở dữ liệu ngừng"], a: "Bình thường.", rubric: "1 điểm." },
    { q: "Liệt kê 5 bước phát hành và bước quay lại.", a: "Sao lưu → commit/tag → áp dụng (lock, .env, build) → khởi động lại chỉ api+worker → kiểm chứng (health, thử nhanh, kiểm rò rỉ, log). Quay lại: checkout tag + pg_restore --clean + khởi động lại.", rubric: "4 điểm: 5 bước đúng thứ tự (3), quay lại đúng (1)." },
    { q: "Vì sao diễn tập khôi phục vào một CSDL mới thay vì ghi đè?", a: "Để kiểm chứng bản sao lưu dùng được mà không làm gián đoạn hay mất dữ liệu đang chạy; so sánh được; ghi đè chỉ làm khi đã chắc và có xác nhận hai lớp.", rubric: "2 điểm." },
    { q: "Máy chủ không có runsc. Rủi ro và cách xử lý?", a: "Sandbox lùi về runc, cách ly kém hơn: code AI viết chạy gần nhân hệ điều hành hơn. Cài gVisor, đặt AOS_SBX_RUNTIME=runsc, kiểm lại; không bật Python Sandbox ở prod khi chưa có runsc.", rubric: "3 điểm: rủi ro (1), cài/cấu hình (1), không bật khi thiếu (1)." },
    { q: "Thiết kế lịch diễn tập và giám sát vận hành cho một khách (5 mục).", a: "Ví dụ: diễn tập khôi phục mỗi quý; kiểm status.json sao lưu ngoài máy mỗi ngày; cảnh báo dung lượng đĩa > 80%; cảnh báo worker không nhịp; kiểm rò rỉ sau mỗi lần phát hành; đổi khoá gốc 6–12 tháng.", rubric: "3 điểm: ≥ 5 mục hợp lý (3), 3–4 (2), ít hơn (1)." },
  ],
  homework: [
    "Viết runbook phát hành 1 trang cho máy chủ của công ty bạn theo 5 bước, có lệnh cụ thể và người chịu trách nhiệm.",
    "Lập lịch diễn tập khôi phục 12 tháng và mẫu biên bản.",
    "Bổ sung 3 sự cố vào cẩm nang từ kinh nghiệm vận hành của bạn.",
  ],
  glossary: [
    ["systemd", "Trình quản lý dịch vụ của Linux."],
    ["EnvironmentFile", "Tệp biến môi trường systemd nạp cho dịch vụ."],
    ["pg_dump -Fc / pg_restore", "Sao lưu/khôi phục PostgreSQL định dạng tuỳ biến."],
    ["restic", "Công cụ sao lưu mã nguồn mở, mã hoá phía mình."],
    ["requirements.lock", "Danh sách thư viện ghim đúng phiên bản."],
    ["Advisory lock", "Khoá mức ứng dụng trong PostgreSQL để tránh chạy trùng."],
    ["Rollback", "Quay lại phiên bản và dữ liệu trước khi phát hành."],
    ["Runbook", "Tài liệu thao tác từng bước cho vận hành."],
    ["Fake LLM", "Model giả cho kiểm thử, chi phí 0."],
  ],
  slides: [
    { t: "Bố cục triển khai", diagram: "aos-arch", pts: ["api 127.0.0.1:8020 · worker", "PostgreSQL + pgvector · Caddy", "Sandbox gVisor"] },
    { t: "Biến môi trường then chốt", pts: ["AOS_ENV=prod · AOS_SECRET_KEYS", "PG_URI · khoá nhà cung cấp", "Trần nhà cung cấp · AOS_CONFIG · AOS_AUTH", "Không bao giờ in giá trị"] },
    { t: "Phát hành 5 bước", diagram: "release", note: "Nhấn: chỉ khởi động lại api + worker; không reboot." },
    { t: "Sao lưu", pts: ["Tại máy: 02:30, 7 ngày / 4 tuần", "Ngoài máy: restic 03:30, 7/4/6", "Kiểm thử mỗi Chủ nhật · status.json", "Khoá gốc đi kèm bản sao lưu"] },
    { t: "Khôi phục và diễn tập", code: "offsite_restore.sh --list\noffsite_restore.sh --snapshot latest --db aos_restore_q4\n# ghi đè: --into-live --yes-overwrite-live", pts: ["Diễn tập mỗi quý vào CSDL mới"] },
    { t: "Cổng chất lượng", pts: ["run_all.sh: mỗi tệp một tiến trình", "Chat: test · lint · build · check-a7", "Kiểm rò rỉ demo", "Playwright: 0 lỗi console"] },
    { t: "Cẩm nang sự cố", pts: ["502 · thiếu khoá prod · run xếp hàng", "503 model · chạm trần · kênh im", "OAuth hết hạn · runsc · đĩa đầy", "RAM ảnh · cảnh báo không tới · khôi phục"] },
    { t: "Lab 11", pts: ["Sao lưu + tag", "Nâng cấp + kiểm chứng", "Hỏng → quay lại", "Diễn tập khôi phục · 3 sự cố"] },
  ],
  plan: [
    { min: 10, act: "Khởi động", detail: "Hỏi: lần cuối công ty bạn diễn tập khôi phục là khi nào?" },
    { min: 35, act: "Giảng: bố cục, biến môi trường, phát hành 5 bước", detail: "Slide 1–3." },
    { min: 25, act: "Giảng: sao lưu, khôi phục, cổng chất lượng, cẩm nang sự cố", detail: "Slide 4–7." },
    { min: 10, act: "Nghỉ giải lao", detail: "" },
    { min: 70, act: "Lab 11", detail: "Giảng viên điều phối kịch bản lỗi ở bước 5 và 8." },
    { min: 20, act: "Kiểm tra", detail: "" },
    { min: 10, act: "Tổng kết, chuẩn bị capstone", detail: "Giới thiệu đề capstone và cách chia nhóm cho buổi 12." },
  ],
  ask: [
    "Thời gian quay lại tối đa bạn chấp nhận được là bao lâu? Quy trình hiện tại đạt chưa?",
    "Ai được phép phát hành vào máy chủ của khách và ai duyệt?",
  ],
  notes: [
    "Tuyệt đối không cho học viên thao tác trên máy chủ thật của khách hay máy chủ demo. Chỉ máy lab.",
    "Chuẩn bị sẵn 12 kịch bản lỗi có thể dựng nhanh (xem tài liệu chuẩn bị lab).",
    "Lab dài: nếu thiếu thời gian, bước 8 làm 1 sự cố thay vì 3.",
  ],
};
export default m;
