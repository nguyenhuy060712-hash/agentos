import { afterEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {   // lib/agentos.ts reads window.location at import; the projects store reads localStorage
  const mem = new Map<string, string>();
  Object.assign(globalThis, { window: { location: { origin: "http://localhost" } },
    localStorage: { getItem: (k: string) => mem.get(k) ?? null, setItem: (k: string, v: string) => void mem.set(k, v), removeItem: (k: string) => void mem.delete(k) } });
});
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import i18n from "@/i18n";
import { deleteProjectMemory, listProjectMemories, setLang } from "../agentos";
import { ProjectMemoryList } from "@/components/agentos/ProjectsPanel";
import { capDetail, capSentence, errText, failureOf } from "@/runtime/run-failure";

afterEach(async () => { vi.unstubAllGlobals(); await i18n.changeLanguage("vi"); });
const ok = (body: unknown) => vi.fn(async (_u: string, _o?: RequestInit) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }));

describe("GĐ8.P project memory in the project settings", () => {
  it("lists and forgets through the project memory API", async () => {
    const f = ok({ items: [{ key: "k/1", text: "Khách thích màu xanh" }] }); vi.stubGlobal("fetch", f);
    expect((await listProjectMemories(7)).items[0].text).toBe("Khách thích màu xanh");
    expect(f.mock.calls[0][0]).toBe("/api/chat/projects/7/memories");
    const d = ok({ ok: true }); vi.stubGlobal("fetch", d);
    await deleteProjectMemory(7, "k/1");
    expect(d.mock.calls[0][0]).toBe("/api/chat/projects/7/memories/k%2F1");
    expect(d.mock.calls[0][1]).toMatchObject({ method: "DELETE" });
  });
  it("renders the memories with a forget button, VI and EN", async () => {
    const html = (items: { key: string; text: string }[] | null) => renderToStaticMarkup(createElement(ProjectMemoryList, { items, onForget: () => {} }));
    let h = html([{ key: "a", text: "Giá vốn là 30%" }, { key: "b", text: "Báo cáo gửi thứ Hai" }]);
    expect(h).toContain("Bộ nhớ dự án"); expect(h).toContain("Giá vốn là 30%");
    expect(h.match(/data-testid="project-memory-delete"/g)).toHaveLength(2);
    expect(html([])).toContain("Dự án chưa có ghi nhớ nào.");
    await i18n.changeLanguage("en");
    h = html([]); expect(h).toContain("Project memory"); expect(h).toContain("Nothing remembered in this project yet.");
    expect(h).not.toMatch(/[ăâđêôơư]/i);
  });
});

describe("GĐ8.P spend-cap banner from the 402 fields", () => {
  const body = { detail: { code: "spend_cap", message: "Spend cap reached feature Deep research ($0.75 used / $0.50 limit per month).", scope: "feature", target: "deep_research",
    period: "month", limit_usd: 0.5, used_usd: 0.75, feature: "deep_research" } };
  const err = `HTTP 402: ${JSON.stringify(body)}`;
  it("keeps the object and shows its message as the error text", () => {
    const f = failureOf("xin chào", err);
    expect(f.kind).toBe("cap"); expect(f.cap?.scope).toBe("feature"); expect(f.cap?.limit_usd).toBe(0.5);
    expect(f.error).toBe("402 · " + body.detail.message); expect(errText(err)).toBe(f.error);
  });
  it("builds the sentence in the UI language, not from the server prose", async () => {
    const f = failureOf("x", err);
    expect(capDetail(f.error, "vi", i18n.t as never, f.cap)).toBe("Tính năng “Nghiên cứu sâu”: đã dùng $0.75 / trần $0.50 mỗi tháng. Liên hệ quản trị viên để nâng trần.");
    await i18n.changeLanguage("en");
    expect(capDetail(f.error, "en", i18n.t as never, f.cap)).toBe("Feature “Deep research”: used $0.75 of the $0.50 limit per month. Ask an administrator to raise the limit.");
    const prov = capSentence({ code: "spend_cap", scope: "provider", target: "gemini", period: "total", limit_usd: 2, used_usd: 2 }, i18n.t as never);
    expect(prov).toBe("Gemini: used $2.00 of the $2.00 limit in total. Ask an administrator to raise the limit.");
  });
  it("falls back to the old prose when the fields are missing", () => {
    const f = failureOf("x", 'HTTP 402: {"detail":"Đã chạm trần chi phí Gemini (đã dùng $2.00 / trần $2.00). Liên hệ quản trị viên để nâng trần."}');
    expect(f.cap).toBeUndefined();
    expect(capDetail(f.error, "vi", i18n.t as never, f.cap)).toBe("Gemini (đã dùng $2.00 / trần $2.00). Liên hệ quản trị viên để nâng trần.");
    expect(capSentence({ code: "spend_cap", message: "x" }, i18n.t as never)).toBeNull();
  });
});

describe("GĐ8.P stop message language = the UI language setting", () => {
  it("switching the chat language saves it on the server (users.lang), which the research run reads", async () => {
    const f = ok({ ok: true, lang: "en" }); vi.stubGlobal("fetch", f);
    await setLang("en");
    expect(f.mock.calls[0][0]).toBe("/api/auth/lang");
    expect(JSON.parse(String(f.mock.calls[0][1]?.body))).toEqual({ lang: "en" });
  });
});
