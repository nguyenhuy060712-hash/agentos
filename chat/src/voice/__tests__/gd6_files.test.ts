import { describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { AgentOSDocumentAttachmentAdapter, AgentOSImageAttachmentAdapter, DOC_ACCEPT, docKindOf, docMeta, formatSize, limits, toStandardBlocks, unitLabel } from "../../runtime/attachments";
import { DocChip } from "../../components/agentos/DocChip";

const file = (name: string, size = 10, type = "") => { const f = new File(["x"], name, { type }); Object.defineProperty(f, "size", { value: size }); return f; };

describe("GD6.8 document attachments: accept / reject", () => {
  it("accepts the 8 document types by extension, images stay with the image adapter", async () => {
    for (const n of ["a.pdf", "a.DOCX", "a.xlsx", "a.csv", "a.pptx", "a.txt", "a.md", "a.json"]) expect(docKindOf(n)).not.toBeNull();
    expect(DOC_ACCEPT.split(",")).toHaveLength(8);
    const ad = new AgentOSDocumentAttachmentAdapter();
    const a = await ad.add({ file: file("Báo cáo.pdf", 1234) });
    expect(a.type).toBe("document"); expect(docMeta.get(a.id)).toMatchObject({ kind: "pdf", size: 1234 });
    await expect(new AgentOSImageAttachmentAdapter().add({ file: file("x.png", 10, "image/png") })).resolves.toMatchObject({ type: "image" });
  });
  it("rejects unknown types, macro files and files over 20 MB (images stay at 5 MB)", async () => {
    vi.stubGlobal("window", { dispatchEvent: () => true }); vi.stubGlobal("CustomEvent", class { t: string; i: unknown; constructor(t: string, i: unknown) { this.t = t; this.i = i; } });
    const ad = new AgentOSDocumentAttachmentAdapter();
    for (const n of ["a.exe", "a.docm", "a.xlsm", "a.doc", "noext"]) await expect(ad.add({ file: file(n) })).rejects.toThrow(/Unsupported/);
    await expect(ad.add({ file: file("big.pdf", limits.maxDocBytes + 1) })).rejects.toThrow(/too large/);
    await expect(new AgentOSImageAttachmentAdapter().add({ file: file("big.png", limits.maxBytes + 1, "image/png") })).rejects.toThrow(/too large/);
    expect(limits.maxPerMessage).toBe(5); expect(limits.maxDocBytes).toBe(20 * 1024 * 1024);
    vi.unstubAllGlobals();
  });
  it("turns an assistant-ui file part into a standard file block that points at /api/files", () => {
    const m = toStandardBlocks({ content: [{ type: "text", text: "hi" }, { type: "file", data: "/api/files/abc", mimeType: "application/pdf", filename: "a.pdf" }] });
    expect(m.content[1]).toEqual({ type: "file", url: "/api/files/abc", mime_type: "application/pdf", filename: "a.pdf" });
  });
});

describe("GD6.8 file chip", () => {
  it("shows icon per type, name, size and page count", () => {
    const pdf = renderToStaticMarkup(createElement(DocChip, { name: "bao-cao.pdf", kind: "pdf", size: 2 * 1048576, pages: 12, units: "page" }));
    expect(pdf).toContain("bao-cao.pdf"); expect(pdf).toContain("2.0 MB"); expect(pdf).toContain("12 trang"); expect(pdf).toContain('data-kind="pdf"');
    const x = renderToStaticMarkup(createElement(DocChip, { name: "s.xlsx", kind: "xlsx", size: 2048, pages: 3, units: "sheet" }));
    expect(x).toContain("2 KB"); expect(x).toContain("3 sheet");
    expect(renderToStaticMarkup(createElement(DocChip, { name: "p.pptx", kind: "pptx", pages: 8, units: "slide" }))).toContain("8 slide");
    expect(pdf).not.toBe(x);   // different icons
  });
  it("shows the error state with the reason", () => {
    const h = renderToStaticMarkup(createElement(DocChip, { name: "scan.pdf", kind: "pdf", error: "Không đọc được tệp: bản quét" }));
    expect(h).toContain('data-error="1"'); expect(h).toContain("bản quét");
  });
  it("helpers", () => { expect(formatSize(500)).toBe("500 B"); expect(unitLabel("page", 0)).toBe(""); });
});
