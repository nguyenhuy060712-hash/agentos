// Course "Hiểu và xây dựng hệ thống AI Agent với Agent OS" (/hoc/). Vietnamese only for now.
// Each lesson page lives in src/pages/hoc/<slug>.astro and uses layouts/Lesson.astro.
export interface Lesson { n: number; slug: string; level: 1 | 2 | 3; mins: number; icon: string; title: string; short: string; summary: string }

export const levels = { 1: "Cơ bản", 2: "Trung cấp", 3: "Nâng cao" } as const;

export const lessons: Lesson[] = [
  { n: 1, slug: "agent-la-gi", level: 1, mins: 20, icon: "spark", title: "AI agent là gì: LLM, prompt và công cụ", short: "AI agent là gì",
    summary: "Phân biệt chatbot với agent, hiểu ba thành phần cốt lõi (bộ não LLM, bản giao việc prompt, đôi tay công cụ) và vòng lặp nghĩ, làm, xem kết quả." },
  { n: 2, slug: "bo-nho-va-rag", level: 1, mins: 25, icon: "memory", title: "Bộ nhớ, ngữ cảnh và RAG", short: "Bộ nhớ và RAG",
    summary: "Agent nhớ gì, quên gì và đọc tài liệu công ty ra sao: cửa sổ ngữ cảnh, bộ nhớ ngắn và dài hạn, dự án, gói ngữ cảnh, và tra cứu tài liệu (RAG)." },
  { n: 3, slug: "agent-nhieu-buoc", level: 2, mins: 25, icon: "steps", title: "Agent nhiều bước, con người duyệt và agent con", short: "Nhiều bước và duyệt",
    summary: "Cách agent lập kế hoạch, chia việc cho agent con, dừng lại xin phép trước việc quan trọng, và cách \"tua lại\" một lượt chạy để sửa." },
  { n: 4, slug: "quan-sat-va-danh-gia", level: 2, mins: 30, icon: "eye", title: "Quan sát và đánh giá chất lượng", short: "Quan sát và đánh giá",
    summary: "Biết agent đã làm gì (trace), đo nó tốt hay dở bằng con số (dataset, giám khảo, thí nghiệm), và vòng cải tiến mỗi tuần." },
  { n: 5, slug: "chi-phi-va-an-toan", level: 2, mins: 30, icon: "shield", title: "Chi phí, an toàn, bảo mật và dữ liệu cá nhân", short: "Chi phí và an toàn",
    summary: "Tiền AI được tính thế nào và cách giữ trong tầm tay; các rủi ro thật của agent và các lớp bảo vệ; dữ liệu cá nhân theo luật Việt Nam." },
  { n: 6, slug: "kenh-va-trien-khai", level: 3, mins: 25, icon: "rocket", title: "Kênh nhắn tin và triển khai", short: "Kênh và triển khai",
    summary: "Đưa agent ra Zalo, Telegram, Slack, nối với Gmail và phần mềm có sẵn, triển khai trên máy chủ riêng và dùng chuẩn mở để không bị khoá vào một nhà cung cấp." },
  { n: 7, slug: "thiet-ke-he-thong", level: 3, mins: 40, icon: "map", title: "Thiết kế một hệ thống agent hoàn chỉnh: case study", short: "Case study hoàn chỉnh",
    summary: "Đi trọn từ bài toán tới ra mắt: thiết kế hệ thống chăm sóc và bán hàng qua Zalo cho một shop thời trang, làm từng bước trên Agent OS." },
];

export const lessonHref = (slug: string) => `/hoc/${slug}/`;
