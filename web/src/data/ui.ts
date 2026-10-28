// Site-wide UI strings and routes (header, footer, labels shared by every page).
export type Lang = "vi" | "en";

export const routes = {
  vi: { home: "/", features: "/tinh-nang/", compare: "/tinh-nang/#so-sanh", learn: "/hoc/", training: "/dao-tao/" },
  en: { home: "/en/", features: "/en/features/", compare: "/en/features/#compare", learn: "/hoc/", training: "/dao-tao/" },
} as const;

export const ui = {
  vi: {
    nav: { home: "Tổng quan", features: "Tính năng", compare: "So với LangSmith", learn: "Học xây agent", training: "Đào tạo", demo: "Mở bản demo", menu: "Mở menu", theme: "Đổi giao diện sáng/tối", skip: "Bỏ qua tới nội dung", other: "EN", otherLabel: "English version" },
    newTab: "(mở tab mới)",
    footer: { made: "Một sản phẩm của MozaAI.", updated: "Cập nhật 27/10/2026.", top: "Lên đầu trang" },
    status: { stable: "Ổn định", beta: "Thử nghiệm", setup: "Cần tài khoản của khách", review: "Nên có chuyên gia duyệt" },
    statusHelp: {
      stable: "Đã làm xong, có kiểm thử tự động và đã chạy thử trên giao diện thật.",
      beta: "Chạy được nhưng mới thử với dịch vụ giả lập, hoặc là kênh không chính thức.",
      setup: "Cần khách cung cấp tài khoản, bot hoặc tên miền riêng mới chạy thật.",
      review: "Phần nhạy cảm về bảo mật hoặc pháp lý: nên nhờ chuyên gia xem trước khi giao cho khách.",
    },
    what: "Làm được gì", when: "Dùng khi nào", where: "Ở đâu",
    zoom: "Bấm để xem ảnh lớn",
  },
  en: {
    nav: { home: "Overview", features: "Features", compare: "vs LangSmith", learn: "Learn (VI)", training: "Training (VI)", demo: "Open the demo", menu: "Open menu", theme: "Toggle light/dark", skip: "Skip to content", other: "VI", otherLabel: "Phiên bản tiếng Việt" },
    newTab: "(opens in a new tab)",
    footer: { made: "A MozaAI product.", updated: "Updated 27 Oct 2026.", top: "Back to top" },
    status: { stable: "Stable", beta: "Beta", setup: "Needs customer account", review: "Expert review advised" },
    statusHelp: {
      stable: "Finished, covered by automated tests and tried in the real UI.",
      beta: "Works, but so far only tested against simulated services, or an unofficial channel.",
      setup: "Needs the customer's own account, bot or domain to run for real.",
      review: "Security- or legally-sensitive: have an expert review it before handing it to customers.",
    },
    what: "What it does", when: "Use it when", where: "Where",
    zoom: "Click to enlarge",
  },
} as const;
