import { describe, expect, it } from "vitest";
import { capDetail, failureOf } from "@/runtime/run-failure";

// 21/10/2026: the server answers 402 when a spend cap is reached (provider cap, agent daily budget, demo cap) -> "cap" banner, no retry
describe("spend cap failure", () => {
  it("402 with the server's reason is a cap failure", () => {
    const f = failureOf("xin chào", 'HTTP 402: {"detail":"Đã chạm trần chi phí Gemini (đã dùng $2.00 / trần $2.00). Liên hệ quản trị viên để nâng trần."}');
    expect(f.kind).toBe("cap");
    expect(f.error).toBe("402 · Đã chạm trần chi phí Gemini (đã dùng $2.00 / trần $2.00). Liên hệ quản trị viên để nâng trần.");
    expect(f.text).toBe("xin chào");
  });
  it("other rejections keep the retry banner", () => {
    expect(failureOf("x", 'HTTP 409: {"detail":"Thread đã gắn với graph khác"}').kind).toBeUndefined();
    expect(failureOf("x", "Failed to fetch").kind).toBeUndefined();
  });
});

// GĐ7.Z: one language in the cap banner, title not repeated in the detail line
describe("cap banner detail", () => {
  const t = (k: string, o?: Record<string, string>) => (k === "capUsed" ? `Used $${o!.used} of the $${o!.limit} limit.` : k === "capAskAdmin" ? "Ask an administrator to raise the limit." : k);
  const err = "402 · Đã chạm trần chi phí tính năng Tìm trace (đã dùng $0.60 / trần $0.30 tổng cộng). Liên hệ quản trị viên để nâng trần.";
  it("vi: drops the repeated title", () => {
    expect(capDetail(err, "vi", t)).toBe("tính năng Tìm trace (đã dùng $0.60 / trần $0.30 tổng cộng). Liên hệ quản trị viên để nâng trần.");
  });
  it("en: English only, keeps the amounts", () => {
    expect(capDetail(err, "en", t)).toBe("Used $0.60 of the $0.30 limit. Ask an administrator to raise the limit.");
  });
});
