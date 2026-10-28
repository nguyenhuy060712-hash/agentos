import { describe, expect, it, vi } from "vitest";

vi.hoisted(() => { Object.assign(globalThis, { window: { location: { origin: "http://localhost" } } }); });   // lib/agentos.ts reads window.location at import
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import "@/i18n";
import { PersonalView, type PersonalViewProps } from "../PersonalPanel";
import { putInstructions } from "@/lib/agentos";

const noop = () => {};
const base: PersonalViewProps = { value: { about: "Chủ quán cà phê", style: "", enabled: true, max_chars: 1500 }, msg: "", dirty: false, onChange: noop, onSave: noop };
const html = (o: Partial<PersonalViewProps> = {}) => renderToStaticMarkup(createElement(PersonalView, { ...base, ...o }));
const saveTag = (h: string) => h.match(/<button[^>]*data-testid="pers-save"[^>]*>/)![0];

describe("GĐ6.9 Cá nhân hoá tab rendering", () => {
  it("shows both boxes, the switch, the character counters and a limited length", () => {
    const h = html();
    expect(h).toContain("Bạn muốn AI biết gì về bạn?"); expect(h).toContain("Bạn muốn AI trả lời thế nào?");
    expect(h).toContain('data-testid="pers-about"'); expect(h).toContain('data-testid="pers-style"'); expect(h).toContain('data-testid="pers-enabled"');
    expect(h).toContain("15 / 1500"); expect(h).toContain("0 / 1500"); expect(h).toContain('maxLength="1500"');
    expect(h).toContain("Chủ quán cà phê");
  });
  it("save is disabled until something changed", () => {
    expect(saveTag(html())).toContain('disabled=""');
    expect(saveTag(html({ dirty: true }))).not.toContain('disabled=""');
  });
  it("demo account: everything read-only with the explanation", () => {
    const h = html({ value: { ...base.value, read_only: true }, dirty: true });
    expect(h).toContain('data-testid="pers-demo"'); expect(h.match(/disabled=""/g)!.length).toBeGreaterThanOrEqual(4);
  });
  it("PUT sends only the three fields", async () => {
    const f = vi.fn(async () => new Response(JSON.stringify({ about: "a", style: "b", enabled: false, max_chars: 1500 }), { status: 200 }));
    vi.stubGlobal("fetch", f);
    await putInstructions({ about: "a", style: "b", enabled: false });
    const [url, init] = f.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/chat/me/instructions"); expect(init.method).toBe("PUT"); expect(JSON.parse(init.body as string)).toEqual({ about: "a", style: "b", enabled: false });
    vi.unstubAllGlobals();
  });
});
