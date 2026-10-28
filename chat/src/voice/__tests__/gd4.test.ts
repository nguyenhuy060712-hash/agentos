import { describe, expect, it } from "vitest";
import { ResumeState, durationMs, goAwayDelayMs, RETRY_DELAYS_MS } from "../resume";
import { GeminiMapper } from "../providers/gemini-live";
import { describeAction, vnd } from "../../components/agentos/approval-format";

describe("GĐ4 voice resumption policy", () => {
  it("parses protobuf Duration JSON", () => {
    expect(durationMs("5s")).toBe(5000); expect(durationMs("4.5s")).toBe(4500);
    expect(durationMs({ seconds: "3", nanos: 500_000_000 })).toBe(3500); expect(durationMs(undefined, 7)).toBe(7);
  });
  it("only resumes with a handle and a valid token, bounded retries", () => {
    const now = Date.now(); const r = new ResumeState(now / 1000 + 600);
    expect(r.next(now)).toBeNull();                       // no handle yet
    r.update({ newHandle: "h1", resumable: true }); r.update({ newHandle: "h2", resumable: false });
    expect(r.handle).toBe("h1");                           // non-resumable update does not replace the handle
    const ds = RETRY_DELAYS_MS.map(() => r.next(now)); expect(ds).toEqual(RETRY_DELAYS_MS); expect(r.next(now)).toBeNull();
    r.succeeded(); expect(r.attempts).toBe(0); expect(r.resumes).toBe(1); expect(r.next(now)).toBe(RETRY_DELAYS_MS[0]);
    const exp = new ResumeState(now / 1000 + 0.5); exp.update({ newHandle: "x" }); expect(exp.next(now)).toBeNull();   // token expired
  });
  it("goAway: reconnect now when idle, just before the deadline when busy", () => {
    expect(goAwayDelayMs(5000, false)).toBe(0); expect(goAwayDelayMs(5000, true)).toBe(4000); expect(goAwayDelayMs(500, true)).toBe(0);
  });
  it("mapper no longer turns goAway into a state event (adapter owns resumption)", () => {
    expect(new GeminiMapper().map({ goAway: { timeLeft: "5s" } })).toEqual([]);
  });
});

describe("GĐ3 case 9: readable approval card", () => {
  it("formats ads_create_campaign in Vietnamese with VND and channel", () => {
    const d = describeAction("ads_create_campaign", { name: "Drip bag Tết", channel: "facebook", budget_vnd: 8000000, audience: "VP 25-40", creative: "x".repeat(400) }, "vi");
    expect(d.title).toBe("Tạo và chạy chiến dịch quảng cáo"); expect(d.destructive).toBe(true); expect(d.risk).toMatch(/chi tiền/);
    const m = Object.fromEntries(d.rows.map((r) => [r.label, r.value]));
    expect(m["Ngân sách"]).toBe(vnd(8000000)); expect(m["Ngân sách"]).toMatch(/8\.000\.000/); expect(m["Kênh"]).toBe("Facebook");
    expect(m["Nội dung quảng cáo"].length).toBeLessThanOrEqual(280);
  });
  it("unknown tools keep every argument visible", () => {
    const d = describeAction("some_tool", { a: 1, cost_vnd: 2000 }, "en");
    expect(d.title).toBe("some_tool"); expect(d.rows.map((r) => r.label)).toEqual(["a", "cost_vnd"]); expect(d.rows[1].value).toBe("2,000 VND");
  });
});

describe("GĐ4 token refresh on resume", () => {
  it("setExpiry revives a resumable state after a fresh single-use token", () => {
    const now = Date.now(); const r = new ResumeState(Math.floor(now / 1000) - 5);
    r.update({ newHandle: "h1", resumable: true });
    expect(r.next(now)).toBeNull();                       // old token expired -> would give up
    r.setExpiry(Math.floor(now / 1000) + 600);
    expect(r.next(now)).toBe(300);                        // a refreshed token lets the resume proceed
  });
});
