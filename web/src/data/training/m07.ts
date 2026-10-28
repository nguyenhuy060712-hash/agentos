import type { Module } from "./types";

const m: Module = {
  n: 7, slug: "danh-gia", track: "van-hanh", roles: ["admin", "it", "architect"], hours: 3,
  title: "Đánh giá chất lượng agent",
  short: "Đánh giá",
  summary: "Đo chất lượng agent bằng số: thiết kế dataset, chọn và căn chỉnh giám khảo, chạy thí nghiệm, so sánh, đặt cổng đạt/không đạt, đánh giá RAG và graph, chấm trực tuyến trên lượt chạy thật.",
  goals: [
    "Thiết kế một dataset ≥ 20 ví dụ có kiểu (preset) phù hợp, lấy từ trace thật và ca khó.",
    "Chọn đúng loại giám khảo (kiểm tra cứng, giám khảo AI, hành trình, cặp, quyết định, gộp điểm) cho từng tiêu chí chất lượng.",
    "Chạy và so sánh hai thí nghiệm, đọc kết quả theo từng ví dụ, chạy lại riêng dòng lỗi.",
    "Đặt cổng đạt/không đạt và giải thích khi nào được chuyển tag prod.",
    "Đo độ khớp giám khảo AI với người chấm (kappa) và căn chỉnh nhanh.",
  ],
  prereq: ["Buổi 3 (commit/tag), buổi 6 (trace)."],
  sections: [
    {
      id: "vi-sao", h: "Vì sao phải đánh giá và hai kiểu đánh giá",
      html: `<p>Agent không cho cùng kết quả mỗi lần; khách đổi cách hỏi, tài liệu đổi, model được cập nhật. Không đo thì không biết một lần sửa làm tốt lên hay tệ đi. Hai kiểu:</p>
<ul>
<li><strong>Offline</strong>: chạy một phiên bản agent trên <em>dataset</em> cố định, chấm từng ví dụ (thí nghiệm). Dùng trước khi đưa bản mới vào chạy.</li>
<li><strong>Online</strong>: giám khảo chấm trên lượt chạy thật (quy tắc tự động hoá, có thể lấy mẫu), phát hiện tụt chất lượng sớm. Backtest là cầu nối: chạy lại các trace thật gần đây trên bản ứng viên.</li>
</ul>`,
    },
    {
      id: "dataset", h: "Dataset: bộ đề tốt",
      shot: "eval-dataset", shotCaption: "Chọn kiểu dataset có sẵn và biến đổi dữ liệu trước khi chấm",
      html: `<p>Dataset & Thí nghiệm → Dataset mới. Kiểu có sẵn (preset): <code>qa</code>, <code>chat</code>, <code>chat_model</code>, <code>tool_calling</code>, <code>rag</code>, kiểm tra theo JSON schema (dùng schema message/tool tương thích LangSmith) khi thêm, sửa, nhập CSV hoặc lấy từ trace. <strong>Biến đổi</strong> (transformations) chuẩn hoá dữ liệu trước khi chấm (ví dụ chuyển tin nhắn sang định dạng OpenAI, trích trường). Ví dụ có lịch sử sửa.</p>
<p>Nguyên tắc bộ đề: (1) <strong>câu thật</strong> lấy từ trace (đã che dữ liệu cá nhân); (2) có <strong>ca khó và ca từng sai</strong> (từ Engine); (3) có câu agent <strong>phải từ chối hoặc chuyển người</strong>; (4) đáp án mẫu viết bởi người nắm nghiệp vụ; (5) cân đối theo chủ đề (Insights); (6) bắt đầu 20–50 câu, tăng dần.</p>`,
    },
    {
      id: "giam-khao", h: "Giám khảo: chọn đúng loại cho đúng tiêu chí",
      html: `<table><thead><tr><th>Loại</th><th>Chấm gì</th><th>Ví dụ</th></tr></thead><tbody>
<tr><td>Kiểm tra cứng (Code / assertion)</td><td>Điều đo bằng luật</td><td>bằng nhau, chứa chữ, khớp regex, khoảng cách chữ (edit distance), khoảng cách nghĩa (embedding distance), hàm tự viết</td></tr>
<tr><td>Giám khảo AI (LLM-as-a-judge)</td><td>Điều cần hiểu nghĩa</td><td>17 mẫu dựng sẵn từ openevals: đúng sự thật, liên quan, độc hại, lạc đề…</td></tr>
<tr><td>Hành trình (trajectory)</td><td>Agent có gọi đúng công cụ, đúng thứ tự</td><td>so khớp chặt, không thứ tự, tập con, tập cha; chấm từng node</td></tr>
<tr><td>So cặp (pairwise)</td><td>Bản nào tốt hơn</td><td>so A/B hai thí nghiệm, hoặc so với lượt trước khi chấm online</td></tr>
<tr><td>Giám khảo quyết định (decision)</td><td>Câu hỏi có kiểu rõ</td><td>có/không, chọn một, cho điểm; mỗi câu hỏi thành một khoá phản hồi; có cổng</td></tr>
<tr><td>Gộp điểm (composite)</td><td>Một điểm tổng</td><td>trung bình, tổng, nhỏ nhất, lớn nhất hoặc công thức an toàn trên các khoá phản hồi; offline, sau thí nghiệm và online</td></tr>
</tbody></table>
<p>Quy tắc: cái gì đo được bằng luật thì <strong>đừng</strong> dùng giám khảo AI (rẻ hơn, chắc hơn). Giám khảo AI cũng tốn tiền: đặt trần riêng cho phạm vi "giám khảo".</p>`,
    },
    {
      id: "thi-nghiem", h: "Thí nghiệm, so sánh, cổng",
      shot: "eval-gate", shotCaption: "Thí nghiệm có cổng: nhãn Không qua ngưỡng",
      html: `<p>Chọn giám khảo ở thẻ Evaluators → <strong>+ Thí nghiệm</strong>: agent (hoặc prompt/cấu hình) trả lời cả bộ đề, mỗi câu được chấm. Chọn nhiều thí nghiệm → <strong>So sánh</strong> theo từng ví dụ. <strong>Chạy lại dòng lỗi</strong> chỉ làm lại các dòng lỗi/thiếu, giữ dòng đã xong. Giám khảo quyết định có <strong>cổng</strong>: cả thí nghiệm dưới ngưỡng thì báo <em>Không qua ngưỡng</em>.</p>
<p><strong>Quy trình phát hành</strong> chuẩn của khoá: commit mới (prompt/gói) → thí nghiệm trên cùng dataset → so với bản đang chạy → qua cổng và không tụt ở nhóm câu quan trọng → chuyển tag prod → theo dõi chấm online 48 giờ → giữ khả năng rollback.</p>`,
    },
    {
      id: "rag-graph", h: "Đánh giá RAG và đánh giá graph",
      shot: "eval-graph", shotCaption: "Thí nghiệm đánh giá graph với giám khảo hành trình và từng node",
      html: `<p><strong>Đánh giá RAG</strong> một bấm (Dataset &amp; Thí nghiệm → Đánh giá RAG): tạo dataset kiểu rag và 4 giám khảo: <em>đúng</em> (so đáp án), <em>liên quan</em> (trả lời đúng câu hỏi), <em>bám tài liệu</em> (groundedness: không bịa ngoài tài liệu), <em>tìm đúng tài liệu</em> (retrieval relevance), rồi chạy luôn. Đọc kết hợp: bám tài liệu thấp = bịa; tìm đúng thấp = vấn đề kho/cắt đoạn.</p>
<p><strong>Đánh giá graph</strong>: với agent nhiều bước, chấm cả hành trình từ trạng thái và chấm riêng từng node; làm được từ giao diện (preset giám khảo graph trong Thí nghiệm mới) và từ SDK (<code>evaluate</code> trên một ứng dụng LangGraph; <code>expect</code> trong pytest ghi kết quả thành phản hồi).</p>`,
    },
    {
      id: "can-chinh", h: "Căn chỉnh giám khảo theo người chấm",
      html: `<p>Giám khảo AI có thể lệch so với chuyên gia. Quy trình: người chấm tay 30–50 ví dụ trong <strong>Hàng đợi chú thích</strong> → Đánh giá → giám khảo → xem <strong>độ khớp</strong> và <strong>kappa</strong> (hiệu chỉnh cho trùng hợp ngẫu nhiên; gợi ý: &lt; 0,4 yếu, 0,4–0,6 trung bình, 0,6–0,8 tốt, &gt; 0,8 rất tốt) → <strong>Căn chỉnh nhanh</strong> thêm ví dụ mẫu từ các ca lệch vào prompt giám khảo → đo lại; tệ hơn thì quay về bản cũ.</p>
<p>Giới hạn còn lại (so với LangSmith): chưa tối ưu bộ phân loại, chưa gắn giám khảo tự chạy theo dataset được kiểm chứng, chưa đọc kết quả thí nghiệm tại máy qua SDK. Gọi thẳng endpoint "Decisions" trả phí của OpenAI không dùng; giám khảo quyết định chạy qua Gateway.</p>`,
    },
  ],
  lab: {
    title: "Lab 7: Cổng chất lượng cho agent CSKH",
    time: "60 phút",
    setup: "Agent CSKH và gói cskh @ prod của các lab trước; dataset từ Engine (Lab 6).",
    steps: [
      "Dataset mới kiểu <code>qa</code> \"cskh-de-thi\": thêm 10 câu từ trace (Tracing → chọn run → thêm vào dataset) và 10 câu tự viết, trong đó 3 câu agent phải chuyển người. Bấm <strong>Schema</strong> xem kiểm tra kiểu.",
      "Đánh giá → <strong>Evaluator</strong>: tạo (a) kiểm tra cứng \"không chứa chữ giảm giá\", (b) giám khảo AI \"đúng chính sách\" từ mẫu đúng sự thật, (c) giám khảo quyết định \"có chuyển người đúng lúc?\" (có/không).",
      "Tạo giám khảo <strong>Gộp điểm</strong>: 0,6 × đúng + 0,2 × không-giảm-giá + 0,2 × chuyển-người. Đặt cổng ≥ 0,85.",
      "+ Thí nghiệm A: agent với gói @ prod. Chờ chấm xong; nếu có dòng lỗi, bấm <strong>Chạy lại dòng lỗi</strong>.",
      "Tạo commit mới cho gói (sửa chính sách rõ hơn), chạy Thí nghiệm B trên commit đó. Chọn A và B → <strong>So sánh</strong>: tìm 2 ví dụ tốt lên và 1 ví dụ tệ đi.",
      "Hàng đợi chú thích: chấm tay 10 ví dụ của B. Xem độ khớp/kappa của giám khảo \"đúng chính sách\"; thử <strong>Căn chỉnh nhanh</strong>.",
      "Quyết định: B có được chuyển tag prod không? Viết 3 dòng lý do dựa trên số liệu.",
    ],
    result: "Dataset 20 câu; 3 giám khảo + 1 gộp điểm có cổng; 2 thí nghiệm so sánh được; có số kappa; quyết định phát hành có số liệu.",
    shot: "eval-composite", shotAlt: "Dựng giám khảo gộp điểm",
    check: ["Dataset có ≥ 3 câu phải chuyển người.", "Giám khảo gộp điểm đúng trọng số và cổng 0,85.", "So sánh A/B chỉ ra được ví dụ tốt lên và tệ đi.", "Quyết định phát hành dựa trên cổng + ví dụ tệ đi, không chỉ điểm trung bình."],
  },
  pitfalls: [
    ["Điểm cao nhưng khách vẫn 👎 nhiều.", "Bộ đề không giống câu thật, hoặc giám khảo lệch người. Thêm câu từ trace; đo kappa và căn chỉnh."],
    ["Điểm dao động giữa hai lần chạy cùng một bản.", "Nhiệt độ cao hoặc giám khảo AI không ổn định; hạ nhiệt độ, chạy nhiều lần, ưu tiên kiểm tra cứng."],
    ["Thí nghiệm tốn nhiều tiền.", "Mỗi ví dụ × mỗi giám khảo AI là một lượt gọi. Đặt trần phạm vi giám khảo; dùng kiểm tra cứng khi được."],
    ["Thêm ví dụ bị báo sai kiểu.", "Dataset có preset kiểm JSON schema; sửa dữ liệu hoặc thêm biến đổi."],
    ["So sánh hai thí nghiệm khác dataset.", "Chỉ so khi cùng dataset (cùng phiên bản ví dụ)."],
  ],
  quiz: [
    { q: "Tiêu chí \"câu trả lời không được chứa số điện thoại\" nên chấm bằng gì?", options: ["Giám khảo AI", "Kiểm tra cứng (regex)", "So cặp", "Người chấm"], a: "Kiểm tra cứng (regex): rẻ, chắc chắn.", rubric: "1 điểm." },
    { q: "Trong đánh giá RAG, điểm \"bám tài liệu\" thấp nhưng \"tìm đúng tài liệu\" cao nghĩa là gì?", options: ["Kho tài liệu sai", "Model trả lời ngoài nội dung tài liệu (bịa)", "Câu hỏi không liên quan", "Dataset sai kiểu"], a: "Model có tài liệu đúng nhưng vẫn bịa ngoài tài liệu.", rubric: "1 điểm." },
    { q: "Kappa 0,35 giữa giám khảo AI và người chấm. Bạn làm gì?", a: "Mức yếu: chưa tin giám khảo. Xem các ca lệch, làm rõ tiêu chí, Căn chỉnh nhanh bằng ví dụ mẫu, đo lại; trong lúc đó dựa vào người chấm/kiểm tra cứng cho quyết định phát hành.", rubric: "3 điểm: nhận định yếu (1), quy trình căn chỉnh (1), không dùng giám khảo đó làm cổng tạm thời (1)." },
    { q: "Viết quy trình phát hành một commit prompt mới theo chuẩn của khoá.", a: "Commit (chưa đổi tag) → thí nghiệm cùng dataset → so với bản đang chạy theo từng ví dụ → qua cổng, không tụt nhóm quan trọng → chuyển tag prod → chấm online/backtest theo dõi → rollback nếu cần.", rubric: "4 điểm: mỗi cụm đúng 1 điểm (commit không đổi tag; thí nghiệm + so sánh; cổng; chuyển tag + theo dõi/rollback)." },
    { q: "Vì sao dataset nên có câu agent phải từ chối hoặc chuyển người?", a: "Để đo hành vi an toàn và ranh giới; agent \"trả lời mọi thứ\" sẽ đạt điểm cao giả nếu chỉ có câu dễ.", rubric: "2 điểm." },
    { q: "Phân biệt đánh giá offline, online và backtest.", a: "Offline: chạy bản agent trên dataset cố định. Online: chấm trên lượt chạy thật. Backtest: chạy lại các trace thật gần đây trên bản ứng viên và chấm bằng cùng giám khảo.", rubric: "3 điểm: mỗi khái niệm 1 điểm." },
  ],
  homework: [
    "Xây dataset 30 câu cho agent của công ty bạn (che dữ liệu cá nhân), có nhãn chủ đề.",
    "Thiết kế bộ giám khảo + trọng số gộp điểm + ngưỡng cổng, giải thích từng lựa chọn.",
    "Ước tính chi phí một lần thí nghiệm: số ví dụ × số giám khảo AI × chi phí trung bình mỗi lượt.",
  ],
  glossary: [
    ["Dataset", "Bộ ví dụ (đầu vào, đáp án mẫu) để chấm."],
    ["Preset / kiểu dataset", "Mẫu JSON schema cho dataset: qa, chat, tool_calling, rag…"],
    ["Evaluator", "Giám khảo: luật, AI hoặc người."],
    ["Trajectory", "Chuỗi bước/công cụ agent đã gọi."],
    ["Composite", "Gộp nhiều điểm thành một."],
    ["Decision evaluator", "Giám khảo trả lời câu hỏi có kiểu (có/không, chọn một, điểm)."],
    ["Gate (cổng)", "Ngưỡng điểm để được phát hành."],
    ["Kappa", "Độ đồng thuận giữa hai người chấm đã trừ trùng hợp ngẫu nhiên."],
    ["Backtest", "Chạy lại trace thật gần đây trên bản ứng viên."],
    ["Groundedness", "Mức câu trả lời bám vào tài liệu được cung cấp."],
  ],
  slides: [
    { t: "Không đo thì không biết", pts: ["Agent không tất định", "Offline: dataset + thí nghiệm", "Online: chấm lượt thật", "Backtest: trace thật trên bản mới"] },
    { t: "Bộ đề tốt", pts: ["Câu thật từ trace (đã che)", "Ca khó, ca từng sai", "Câu phải từ chối / chuyển người", "Preset + JSON schema + biến đổi"], shot: "eval-dataset" },
    { t: "Chọn giám khảo", pts: ["Luật đo được → kiểm tra cứng", "Hiểu nghĩa → giám khảo AI (17 mẫu)", "Đúng công cụ → hành trình", "A/B → so cặp · Có/không → quyết định"] },
    { t: "Gộp điểm và cổng", shot: "eval-composite", pts: ["avg · sum · min · max · công thức", "Cổng: Không qua ngưỡng"] },
    { t: "Thí nghiệm và so sánh", shot: "eval-results", pts: ["So theo từng ví dụ", "Chạy lại dòng lỗi"] },
    { t: "RAG: 4 giám khảo", pts: ["Đúng", "Liên quan", "Bám tài liệu", "Tìm đúng tài liệu"], shot: "eval-rag" },
    { t: "Căn chỉnh theo người", pts: ["Hàng đợi chú thích", "Độ khớp + kappa", "Căn chỉnh nhanh · quay lại bản cũ"] },
    { t: "Quy trình phát hành", pts: ["Commit (chưa đổi tag)", "Thí nghiệm + so sánh", "Qua cổng", "Chuyển tag prod · theo dõi · rollback"], note: "Đây là quy trình sẽ được chấm trong bài thi thực hành L2/L3." },
    { t: "Lab 7", pts: ["Dataset 20 câu", "3 giám khảo + gộp điểm + cổng", "A/B · chạy lại dòng lỗi", "Kappa · quyết định phát hành"] },
  ],
  plan: [
    { min: 10, act: "Ôn buổi 6", detail: "" },
    { min: 30, act: "Giảng: vì sao đánh giá, dataset, chọn giám khảo", detail: "Slide 1–3. Hoạt động: ghép 8 tiêu chí với loại giám khảo." },
    { min: 25, act: "Giảng: gộp điểm, cổng, thí nghiệm, RAG, căn chỉnh, quy trình phát hành", detail: "Slide 4–8." },
    { min: 10, act: "Nghỉ giải lao", detail: "" },
    { min: 60, act: "Lab 7", detail: "" },
    { min: 25, act: "Kiểm tra", detail: "" },
    { min: 20, act: "Trình bày quyết định phát hành", detail: "3 nhóm trình bày bước 7 của lab, cả lớp phản biện." },
  ],
  ask: [
    "Điểm trung bình tăng 5% nhưng 2 câu về hoàn tiền tệ đi. Có phát hành không?",
    "Ai trong công ty nên viết đáp án mẫu cho bộ đề?",
  ],
  notes: [
    "Học viên hay chỉ nhìn điểm trung bình. Bắt buộc đọc ví dụ tệ đi trước khi kết luận.",
    "Giám khảo AI trên máy lab là AI giả, điểm mang tính minh hoạ; nói rõ để học viên tập trung vào quy trình.",
  ],
};
export default m;
