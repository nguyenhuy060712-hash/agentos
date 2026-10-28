import { afterEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => { Object.assign(globalThis, { window: { location: { origin: "http://localhost" } } }); });   // lib/agentos.ts reads window.location at import
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import "@/i18n";
import { SecurityView, type SecurityViewProps } from "../SecurityPanel";
import { loginStart, loginVerify, twoFaDisable } from "@/lib/agentos";

const noop = () => {};
const base: SecurityViewProps = {
  status: { enabled: false, pending_setup: false, recovery_left: 0, workspace_requires: false, must_enroll: false, demo: false },
  sessions: [{ id: "a".repeat(16), device: "Chrome · Windows", ip: "1.2.3.4", created: "2026-10-22T01:00:00Z", last_seen: "2026-10-22T02:30:00Z", expires: "2026-10-29T01:00:00Z", current: true },
    { id: "b".repeat(16), device: "Safari · iOS", ip: "5.6.7.8", created: "2026-10-21T01:00:00Z", last_seen: "2026-10-21T02:30:00Z", expires: "2026-10-28T01:00:00Z", current: false }],
  setup: null, recovery: null, dialog: null, msg: "", onStart: noop, onEnable: noop, onRegen: noop, onDisable: noop, onDialog: noop, onRecoverySaved: noop, onRevoke: noop, onRevokeOthers: noop,
};
const html = (o: Partial<SecurityViewProps> = {}) => renderToStaticMarkup(createElement(SecurityView, { ...base, ...o }));

describe("GĐ6.7 Bảo mật tab rendering", () => {
  it("off: shows the enable button, devices with the current one flagged, and never a token", () => {
    const h = html();
    expect(h).toContain('data-testid="sec-enable"'); expect(h).not.toContain('data-testid="sec-disable"');
    expect(h).toContain("Chrome · Windows"); expect(h).toContain("Safari · iOS"); expect(h.match(/data-testid="sec-current"/g)).toHaveLength(1);
    expect(h).toContain("Đăng xuất các thiết bị khác");
  });
  it("on: shows state, backup codes left, disable and regenerate", () => {
    const h = html({ status: { ...base.status, enabled: true, recovery_left: 7 } });
    expect(h).toContain("Còn 7 mã dự phòng"); expect(h).toContain('data-testid="sec-disable"'); expect(h).toContain('data-testid="sec-regen"'); expect(h).not.toContain('data-testid="sec-enable"');
  });
  it("setup: QR image, manual key and code box; recovery codes shown once", () => {
    const h = html({ setup: { secret: "JBSWY3DPEHPK3PXP", qr_svg: "<svg xmlns='http://www.w3.org/2000/svg'></svg>" } });
    expect(h).toContain('data-testid="sec-qr"'); expect(h).toContain("data:image/svg+xml"); expect(h).toContain("JBSWY3DPEHPK3PXP"); expect(h).toContain('data-testid="sec-confirm"');
    const r = html({ recovery: ["aaaaa-bbbbb", "ccccc-ddddd"] });
    expect(r).toContain("aaaaa-bbbbb"); expect(r).toContain('data-testid="sec-recovery-done"');
  });
  it("must-enroll banner and demo account", () => {
    expect(html({ status: { ...base.status, must_enroll: true } })).toContain('data-testid="sec-must"');
    const d = html({ status: { ...base.status, demo: true } });
    expect(d).toContain("Tài khoản demo không bật được"); expect(d).not.toContain('data-testid="sec-enable"');
  });
  it("disable dialog asks for password and code", () => {
    const h = html({ status: { ...base.status, enabled: true }, dialog: "disable" });
    expect(h).toContain('data-testid="sec-pw"'); expect(h).toContain('data-testid="sec-code"');
  });
});

describe("GĐ6.7 two-step login client", () => {
  afterEach(() => vi.unstubAllGlobals());
  const stub = (responses: { status: number; body: unknown }[]) => {
    const calls: { url: string; body: Record<string, unknown> }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, body: JSON.parse(String(init?.body ?? "{}")) }); const x = responses.shift()!;
      return new Response(JSON.stringify(x.body), { status: x.status, statusText: "x", headers: { "Content-Type": "application/json" } });
    }));
    return calls;
  };
  it("password step returns a challenge, then the code step completes the login", async () => {
    const calls = stub([{ status: 200, body: { need_2fa: true, challenge: "CH123" } }, { status: 200, body: { ok: true, username: "u" } }]);
    const a = await loginStart("u", "pw"); expect(a.need_2fa).toBe(true); expect(a.challenge).toBe("CH123");
    const b = await loginVerify("CH123", { code: "123456", remember_device: true }); expect(b.ok).toBe(true);
    expect(calls.map((c) => c.url)).toEqual(["/api/auth/login", "/api/auth/2fa/verify"]);
    expect(calls[1].body).toEqual({ challenge: "CH123", code: "123456", remember_device: true });
  });
  it("without 2FA the first step logs in directly; wrong code surfaces the Vietnamese message; recovery code and lockout", async () => {
    stub([{ status: 200, body: { ok: true, username: "u" } }]); expect((await loginStart("u", "pw")).need_2fa).toBeUndefined();
    stub([{ status: 401, body: { detail: "Mã không đúng. Còn 4 lần thử" } }]);
    await expect(loginVerify("CH", { code: "000000" })).rejects.toThrow("Mã không đúng. Còn 4 lần thử");
    const calls = stub([{ status: 429, body: { detail: "Nhập sai mã 5 lần. Đăng nhập 2 lớp bị khoá 15 phút" } }]);
    await expect(loginVerify("CH", { recovery_code: "abcde-12345" })).rejects.toMatchObject({ status: 429, message: expect.stringContaining("15 phút") });
    expect(calls[0].body).toEqual({ challenge: "CH", recovery_code: "abcde-12345" });
  });
  it("disable sends a code or a recovery code depending on the input", async () => {
    let calls = stub([{ status: 200, body: { ok: true } }]); await twoFaDisable("pw", "123456"); expect(calls[0].body).toEqual({ password: "pw", code: "123456" });
    calls = stub([{ status: 200, body: { ok: true } }]); await twoFaDisable("pw", "abcde-12345"); expect(calls[0].body).toEqual({ password: "pw", recovery_code: "abcde-12345" });
  });
});
