// Lab environment guide. `student` is public (/dao-tao/chuan-bi-lab/); `instructor` is [GV] (instructor kit only).
// Both are trusted HTML. Facts: agentos-local/docs/TIEP_TUC.md, it_facts (25/10/2026).
export const setup = {
  student: `<h2 id="tong-quan">Môi trường lab là gì</h2>
<p>Mọi bài thực hành chạy trên <strong>máy lab Agent OS</strong> do giảng viên chuẩn bị: một bản Agent OS đầy đủ chạy với <strong>AI giả lập</strong> (không gọi model thật, chi phí 0 đồng) và các <strong>dịch vụ giả lập</strong> cho Telegram, Zalo OA, Slack, Google/Microsoft 365, kho S3. Kết quả AI trên máy lab mang tính minh hoạ: mục tiêu là học đúng thao tác, quy trình và cách đọc kết quả.</p>
<h2 id="can-co">Học viên cần có</h2>
<ul>
<li>Máy tính có trình duyệt Chrome, Edge hoặc Firefox bản mới; màn hình ≥ 13 inch (điện thoại dùng được cho phần Chat nhưng không tiện cho lab).</li>
<li>Tài khoản trên máy lab (giảng viên cấp, vai trò editor; buổi 8, 10 có thêm tài khoản admin của workspace riêng nhóm).</li>
<li>Điện thoại có ứng dụng xác thực (Google Authenticator, Microsoft Authenticator, Authy hoặc 1Password) cho bài đăng nhập 2 lớp.</li>
<li>Từ buổi 9 (học viên IT): Python 3.12, <code>pip install langgraph-sdk opentelemetry-sdk opentelemetry-exporter-otlp-proto-http</code>, curl; quen terminal. Buổi 11: một máy Linux hoặc WSL có Docker và quyền sudo (máy lab riêng của nhóm).</li>
</ul>
<h2 id="quy-tac">Quy tắc trong lớp</h2>
<ul>
<li><strong>Không dán mật khẩu, token, khoá API vào ô chat</strong>, slide, ảnh chụp. Khoá chỉ nhập ở kho bí mật hoặc biến môi trường.</li>
<li>Chỉ dùng dữ liệu tự tạo hoặc dữ liệu mẫu giảng viên phát; không đưa dữ liệu khách hàng thật lên máy lab.</li>
<li>Không thao tác trên máy chủ thật của khách hay bản demo cho khách.</li>
<li>Gặp lỗi: chụp màn hình (đã che thông tin nhạy cảm), ghi thời điểm, báo giảng viên; đây cũng là tư liệu cho buổi 6 và 11.</li>
</ul>
<h2 id="meo-ai-gia">Mẹo dùng AI giả trên máy lab</h2>
<p>AI giả nhận vài nhãn trong câu để trả lời theo kịch bản, giúp lab chạy ổn định: <code>[json]</code> trả JSON hợp lệ, <code>[json-invalid]</code> trả JSON sai, <code>[tool:ten_cong_cu]</code> buộc gọi một công cụ. Nhãn này không có tác dụng với model thật.</p>
<h2 id="kiem-tra">Tự kiểm tra trước buổi 1</h2>
<ol>
<li>Mở địa chỉ máy lab, đăng nhập được.</li>
<li>Mở Chat, gửi một câu, nhận trả lời.</li>
<li>Bấm EN/VI đổi được ngôn ngữ.</li>
<li>Bấm Ctrl + K mở được ô tìm kiếm.</li>
</ol>`,

  instructor: `<h2 id="gv-may-lab">1. Dựng máy lab (trước khoá 3 ngày)</h2>
<ul>
<li><strong>Máy</strong>: Linux hoặc WSL2, ≥ 8 GB RAM (khuyên 16 GB cho lớp 20 người), ≥ 40 GB đĩa trống (kiểm dung lượng, từng có sự cố đầy đĩa), Docker, PostgreSQL 17 cục bộ (container), gVisor <code>runsc</code> cho sandbox.</li>
<li><strong>Mã</strong>: kho <code>agentos-local</code> (nhánh dev đã qua cổng GĐ9.Z). Chạy máy chủ AI giả: <code>bash var/restart_tmp.sh</code> → <code>overlay/run_local.py</code> nghe cổng 8020, log ở <code>var/server.log</code>. <code>/health</code> trả 401 khi không có khoá là bình thường.</li>
<li><strong>Không dùng model thật trong lớp</strong>. Nếu muốn trình diễn voice hoặc nghiên cứu sâu thật: <code>var/restart_real.sh</code> có trần chi phí nhỏ, chỉ máy giảng viên, xong chuyển lại AI giả.</li>
<li><strong>Truy cập lớp</strong>: đưa máy lab ra mạng nội bộ lớp học qua HTTPS (Caddy với chứng chỉ nội bộ) hoặc đường hầm có đăng nhập; không mở công khai không TLS.</li>
<li><strong>Giới hạn RAM</strong> mọi lệnh nặng (kiểm thử, OCR): chạy kiểm thử bằng <code>tests/run_all.sh</code>.</li>
</ul>
<h2 id="gv-tai-khoan">2. Tài khoản và dữ liệu</h2>
<ul>
<li>Workspace "Lop-hoc" (mọi học viên vai trò editor) + nhóm người dùng "Lop-hoc". Mỗi nhóm học viên: 2 workspace riêng (WS-A, WS-B) với tài khoản admin cho buổi 8, 10.</li>
<li>Agent mẫu: dùng 8 mẫu theo ngành; agent "CSKH Shop ABC" mẫu của giảng viên (đáp án Lab 2–3).</li>
<li>Tệp phát cho học viên: <code>bang-gia.txt</code>, <code>doanh-thu-q3.xlsx</code> (3 chi nhánh, 90 dòng), ảnh phiếu có SĐT giả, <code>email_doc_hai.txt</code> (chứa câu prompt injection), bộ token giả cho kênh.</li>
<li><strong>Trace mẫu cho buổi 6</strong>: chạy kịch bản sinh ~200 lượt cho agent CSKH lớp (dùng langgraph-sdk theo lô, xen câu có <code>[tool:x]</code>, câu gây lỗi, và gửi 👎 qua API phản hồi). Kiểm: ≥ 3 trace lỗi, ≥ 10 lượt 👎, có một thread của "khách Lan" khoảng 10:00.</li>
<li><strong>Máy nhận webhook</strong> (buổi 6, 9): một tiến trình nhỏ ghi lại header và body của yêu cầu đến, chạy trong mạng lớp.</li>
</ul>
<h2 id="gv-script">3. Script cho buổi 9 (đặt trong thư mục lab9)</h2>
<pre><code># lab9_sdk.py — chạy: AOS_URL=... AOS_API_KEY=... AGENT_ID=... python lab9_sdk.py
import asyncio, os
from langgraph_sdk import get_client
async def main():
    c = get_client(url=os.environ["AOS_URL"] + "/api/v1", api_key=os.environ["AOS_API_KEY"])
    t = await c.threads.create()
    r = await c.runs.wait(t["thread_id"], os.environ["AGENT_ID"],
        input={"messages": [{"role": "user", "content": "Đổi size đơn DH1023"}]})
    s = await c.threads.get_state(t["thread_id"])
    print("dang cho duyet:", bool(s.get("tasks") and s["tasks"][0].get("interrupts")))
    r = await c.runs.wait(t["thread_id"], os.environ["AGENT_ID"], command={"resume": {"type": "approve"}})
    print(r)
asyncio.run(main())</code></pre>
<p>Ghi chú: định dạng giá trị <code>resume</code> phụ thuộc kiểu thẻ duyệt của công cụ; kiểm trên máy lab trước buổi học và sửa script cho khớp (xem interrupt in ra ở bước đầu).</p>
<pre><code># lab9_mcp.sh — liệt kê công cụ của /mcp/aos
curl -s "$AOS_URL/mcp/aos" -H "X-Api-Key: $AOS_API_KEY" \\
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \\
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'</code></pre>
<pre><code># lab9_otel.py — gửi 1 span GenAI bằng khoá nhận trace (ak_)
import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
p = TracerProvider(); trace.set_tracer_provider(p)
p.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
    endpoint=os.environ["AOS_URL"] + "/otel/v1/traces", headers={"x-api-key": os.environ["AOS_INGEST_KEY"]})))
with trace.get_tracer("lab9").start_as_current_span("chat gemini") as s:
    s.set_attribute("gen_ai.operation.name", "chat"); s.set_attribute("gen_ai.request.model", "gemini-lab")
    s.set_attribute("gen_ai.usage.input_tokens", 120); s.set_attribute("gen_ai.usage.output_tokens", 40)
p.shutdown()</code></pre>
<pre><code># lab9_verify.py — kiểm chữ ký webhook (body là bytes thô đã nhận)
import hmac, hashlib, time
def verify(secret: bytes, ts: str, body: bytes, sig: str, window=300) -> bool:
    mac = hmac.new(secret, ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest("sha256=" + mac, sig) and abs(time.time() - int(ts)) &lt; window</code></pre>
<h2 id="gv-su-co">4. Kịch bản lỗi cho buổi 11 (dựng trên máy lab của nhóm)</h2>
<ol>
<li>Bỏ <code>AOS_SECRET_KEYS</code> và đặt <code>AOS_ENV=prod</code> → API không khởi động.</li>
<li>Dừng worker → run xếp hàng.</li>
<li>Đặt trần agent rất thấp → dải báo chạm trần.</li>
<li>Đổi máy đích của bí mật kênh → kênh im lặng.</li>
<li>Đổi runtime sandbox sang tên sai → chạy code lỗi.</li>
<li>Làm đầy một thư mục tạm có giới hạn quota → lỗi ghi.</li>
<li>Xoá một agent (đã sao lưu) → khôi phục vào CSDL mới và lấy lại.</li>
<li>Sai giờ hệ thống 3 giờ → webhook Zalo giả lập bị từ chối (cửa sổ thời gian).</li>
</ol>
<p>Sau mỗi kịch bản: khôi phục máy về ảnh chụp sạch (snapshot VM hoặc sao lưu <code>pre_lab11</code>).</p>
<h2 id="gv-truoc-buoi">5. Danh sách kiểm trước mỗi buổi (15 phút)</h2>
<ul>
<li>Máy lab chạy, đăng nhập được bằng 1 tài khoản học viên thử; dung lượng đĩa còn ≥ 10 GB.</li>
<li>Agent mẫu và dữ liệu của buổi có sẵn; đã chạy thử lab của buổi từ đầu tới cuối.</li>
<li>Máy chiếu: mở sẵn slide (chế độ trình chiếu, phím F toàn màn hình) và tab máy lab.</li>
<li>In phiếu ký nhận lab (các điểm kiểm của buổi) và đề kiểm tra nếu thi giấy.</li>
<li>Không có khoá thật nào trên màn hình chiếu.</li>
</ul>
<h2 id="gv-cham">6. Chấm và cấp chứng nhận</h2>
<ul>
<li>Lab mỗi buổi: đạt/chưa đạt theo các điểm kiểm; chưa đạt thì làm bù trước buổi thi.</li>
<li>Kiểm tra cuối buổi: chấm theo rubric, dùng để theo dõi tiến bộ (không tính vào chứng nhận).</li>
<li>Thi cấp chứng nhận: trắc nghiệm rút ngẫu nhiên từ ngân hàng theo cấp (giữ cân bằng theo buổi), bài thực hành theo tiêu chí đạt, capstone theo rubric 100 điểm với hội đồng 2 người.</li>
<li>Lưu bài làm, điểm, biên bản bảo vệ ít nhất 2 năm; mẫu chứng nhận ở cuối tài liệu này.</li>
</ul>`,
};
