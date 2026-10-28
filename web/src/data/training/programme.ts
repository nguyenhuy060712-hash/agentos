// Programme-level data: tracks, roles, certification levels, schedule. Module content lives in mNN.ts.
import type { Module, Role, Track } from "./types";
import m01 from "./m01"; import m02 from "./m02"; import m03 from "./m03"; import m04 from "./m04";
import m05 from "./m05"; import m06 from "./m06"; import m07 from "./m07"; import m08 from "./m08";
import m09 from "./m09"; import m10 from "./m10"; import m11 from "./m11"; import m12 from "./m12";

export const modules: Module[] = [m01, m02, m03, m04, m05, m06, m07, m08, m09, m10, m11, m12];
export const moduleHref = (slug: string) => `/dao-tao/${slug}/`;
export const slideHref = (slug: string) => `/dao-tao/slide/${slug}/`;

export const programme = {
  title: "Chương trình đào tạo Chuyên gia Agent OS",
  sub: "Khoá học 12 buổi × 3 giờ cho đội IT, quản trị viên và người dùng nâng cao: dùng, vận hành, tích hợp, bảo mật, triển khai và thiết kế hệ thống AI agent trên Agent OS.",
  version: "Phiên bản 1.0 · 25/10/2026 · theo Agent OS hết GĐ9",
  hours: 36,
  sessions: 12,
};

export const tracks: Record<Track, { t: string; d: string; color: string }> = {
  "nen-tang": { t: "Nền tảng", d: "Kiến trúc, khái niệm, xây agent đầu tiên", color: "emerald" },
  "van-hanh": { t: "Vận hành", d: "Dùng và quản trị hằng ngày: prompt, chat, kênh, quan sát, đánh giá, chi phí", color: "sky" },
  "it": { t: "Chuyên sâu IT", d: "Tích hợp API/MCP/A2A/OTel, bảo mật, triển khai, xử lý sự cố", color: "violet" },
  "kien-truc": { t: "Kiến trúc & Capstone", d: "Thiết kế hệ thống agent hoàn chỉnh, bảo vệ đồ án, thi cuối khoá", color: "rose" },
};

export const roles: Record<Role, { t: string; d: string; sessions: number[] }> = {
  user: { t: "Người dùng nghiệp vụ", d: "Nhân viên, trưởng nhóm dùng Agent OS Chat và agent hằng ngày.", sessions: [1, 2, 3, 4, 6] },
  admin: { t: "Quản trị viên", d: "Người tạo agent, quản lý người dùng, kênh, chất lượng và chi phí.", sessions: [1, 2, 3, 4, 5, 6, 7, 8] },
  it: { t: "Kỹ sư IT / tích hợp", d: "Người nối Agent OS với hệ thống công ty, lo bảo mật và vận hành máy chủ.", sessions: [1, 2, 3, 5, 6, 7, 8, 9, 10, 11] },
  architect: { t: "Kiến trúc sư / Chuyên gia", d: "Người thiết kế giải pháp agent cho cả doanh nghiệp; học trọn khoá.", sessions: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12] },
};

export const levels = [
  { k: "L1", t: "Agent OS Practitioner", vi: "Người dùng thành thạo", sessions: "Buổi 1, 2, 3, 4, 6", exam: "Thi trắc nghiệm 15 câu (đạt ≥ 70%) + 1 bài lab thực hành 45 phút", for: "Người dùng nghiệp vụ" },
  { k: "L2", t: "Agent OS Administrator", vi: "Quản trị viên", sessions: "Buổi 1–8", exam: "Thi trắc nghiệm 30 câu (đạt ≥ 75%) + bài lab vận hành 90 phút (dựng agent có kênh, duyệt, trần chi phí, đánh giá)", for: "Quản trị viên" },
  { k: "L3", t: "Agent OS Expert", vi: "Chuyên gia Agent OS", sessions: "Trọn 12 buổi", exam: "Thi trắc nghiệm 40 câu (đạt ≥ 80%) + bài lab IT 120 phút + đồ án capstone bảo vệ trước hội đồng (đạt ≥ 75/100)", for: "Kỹ sư IT, kiến trúc sư" },
];
