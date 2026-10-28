import { describe, expect, it, vi } from "vitest";

vi.hoisted(() => { Object.assign(globalThis, { window: { location: { origin: "http://localhost" } } }); });   // lib/agentos.ts reads window.location at import
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import "@/i18n";
import { DataView, type DataViewProps } from "../DataPanel";
import { eraseMyData, exportMyData } from "@/lib/agentos";

const noop = () => {};
const base: DataViewProps = { me: { username: "lan.pham" }, busy: false, msg: "", done: false, confirm: "", open: false, onConfirm: noop, onOpen: noop, onExport: noop, onErase: noop };
const html = (o: Partial<DataViewProps> = {}) => renderToStaticMarkup(createElement(DataView, { ...base, ...o }));
const tag = (h: string, id: string) => h.match(new RegExp(`<button[^>]*data-testid="${id}"[^>]*>`))![0];

describe("GĐ9.8 Dữ liệu của tôi tab", () => {
  it("shows export + delete sections; delete starts closed", () => {
    const h = html();
    expect(h).toContain('data-testid="data-export-btn"'); expect(h).toContain("Tải dữ liệu của tôi (zip)"); expect(h).toContain("Xoá dữ liệu của tôi…");
    expect(h).not.toContain('data-testid="data-erase-confirm"');
  });
  it("delete needs the exact username typed", () => {
    expect(tag(html({ open: true }), "data-erase-go")).toContain('disabled=""');
    expect(tag(html({ open: true, confirm: "lan" }), "data-erase-go")).toContain('disabled=""');
    expect(tag(html({ open: true, confirm: "lan.pham" }), "data-erase-go")).not.toContain('disabled=""');
    expect(html({ open: true })).toContain("lan.pham");
  });
  it("demo accounts see an explanation instead of the delete button", () => {
    const h = html({ me: { username: "demo1", demo: true } }); expect(h).toContain('data-testid="data-demo"'); expect(h).not.toContain('data-testid="data-erase-open"');
  });
  it("API: export reads the zip name, erase POSTs the confirmation", async () => {
    const f = vi.fn(async (u: string) => u.includes("export")
      ? new Response(new Blob(["PK"]), { status: 200, headers: { "content-disposition": 'attachment; filename="du-lieu-cua-lan-20261024.zip"' } })
      : new Response(JSON.stringify({ ok: true, counts: { threads: 2 }, message: "x" }), { status: 200 }));
    vi.stubGlobal("fetch", f);
    expect((await exportMyData()).name).toBe("du-lieu-cua-lan-20261024.zip");
    const r = await eraseMyData("lan.pham", "lý do"); expect(r.counts.threads).toBe(2);
    const [url, init] = f.mock.calls[1] as unknown as [string, RequestInit];
    expect(url).toBe("/api/privacy/me/erase"); expect(init.method).toBe("POST"); expect(JSON.parse(init.body as string)).toEqual({ confirm: "lan.pham", reason: "lý do" });
    vi.unstubAllGlobals();
  });
});
