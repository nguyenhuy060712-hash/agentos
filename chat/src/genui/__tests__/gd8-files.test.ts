import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import i18n from "@/i18n";
import { FileCard, FileCards } from "../FileCards";
import { canPreview, fileKind, fileTree, fileUrl, fmtSize, parseFiles, threadFiles } from "../files-helpers";
import { parseCodeRun } from "../gd5b-helpers";

// GĐ8.6: files the agent creates in the sandbox (/workspace/out) -> cards in the chat + file tree in the side panel.
const ID = (n: number) => `00000000-0000-4000-8000-00000000000${n}`;
const PDF = { id: ID(1), name: "bao-cao-q3.pdf", size: 9_340, mime_type: "application/pdf" };
const DOCX = { id: ID(2), name: "q3/Báo cáo Hà Nội.docx", size: 36_940, mime_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" };
const TOOL = `status=ok exit=0 (1520 ms, gvisor (runsc))\n[stdout]\n/workspace/out/bao-cao-q3.pdf\n[aos-files] ${JSON.stringify({ files: [PDF, DOCX] })}`;

describe("file marker + helpers", () => {
  it("parses the [aos-files] line of the code tool and keeps stdout clean", () => {
    expect(parseFiles(TOOL)).toEqual([PDF, DOCX]);
    const r = parseCodeRun(TOOL)!;
    expect(r.stdout).toBe("/workspace/out/bao-cao-q3.pdf"); expect(r.files).toEqual([PDF, DOCX]);
    const withErr = parseCodeRun("status=error exit=1 (9 ms, x)\n[stderr]\nboom\n[aos-files] " + JSON.stringify({ files: [PDF] }) + "\n[tệp không lưu được] x.exe: unsupported")!;
    expect(withErr.stderr).toBe("boom"); expect(withErr.files).toEqual([PDF]);
  });
  it("is tolerant: no marker, bad JSON, bad ids", () => {
    expect(parseFiles("status=ok")).toEqual([]);
    expect(parseFiles("[aos-files] {nope")).toEqual([]);
    expect(parseFiles(`[aos-files] {"files":[{"id":"../x","name":"a"}]}`)).toEqual([]);
    expect(parseFiles(42)).toEqual([]);
  });
  it("collects a thread's files once, builds the folder tree, sizes and kinds", () => {
    const msgs = [{ content: "hi" }, { content: [{ type: "text", text: "x" }, { type: "tool-call", result: TOOL }] }, { content: [{ type: "tool-call", result: TOOL }] }];
    expect(threadFiles(msgs).map((f) => f.id)).toEqual([ID(1), ID(2)]);
    const tree = fileTree([PDF, DOCX]);
    expect(tree.map((n) => n.name)).toEqual(["q3", "bao-cao-q3.pdf"]);                      // folders first
    expect(tree[0].children[0].file?.id).toBe(ID(2));
    expect(fmtSize(36_940, "vi")).toBe("36,1 KB"); expect(fmtSize(36_940, "en")).toBe("36.1 KB"); expect(fmtSize(512)).toBe("512 B");
    expect(fileKind(PDF)).toBe("pdf"); expect(fileKind(DOCX)).toBe("word"); expect(canPreview(PDF)).toBe(true); expect(canPreview(DOCX)).toBe(false);
    expect(fileUrl(PDF, true)).toBe(`/api/files/${ID(1)}?inline=1`); expect(fileUrl(DOCX, true)).toBe(`/api/files/${ID(2)}`);
  });
});

describe("FileCard component", () => {
  it("renders name, size, download link (with the real file name) and a preview button for PDF only", async () => {
    await i18n.changeLanguage("vi");
    const pdf = renderToStaticMarkup(createElement(FileCard, { f: PDF }));
    expect(pdf).toContain('data-kind="pdf"'); expect(pdf).toContain("bao-cao-q3.pdf"); expect(pdf).toContain("9,1 KB");
    expect(pdf).toContain(`href="/api/files/${ID(1)}"`); expect(pdf).toContain('download="bao-cao-q3.pdf"');
    expect(pdf).toContain("Tải về"); expect(pdf).toContain('data-testid="file-preview"'); expect(pdf).toContain("Xem trước");
    const doc = renderToStaticMarkup(createElement(FileCard, { f: DOCX }));
    expect(doc).toContain('download="Báo cáo Hà Nội.docx"'); expect(doc).not.toContain("file-preview"); expect(doc).toContain('data-kind="word"');
    await i18n.changeLanguage("en");
    expect(renderToStaticMarkup(createElement(FileCard, { f: PDF }))).toContain("Download");
    await i18n.changeLanguage("vi");
  });
  it("a list renders one card per file and nothing when empty", () => {
    expect((renderToStaticMarkup(createElement(FileCards, { files: [PDF, DOCX] })).match(/data-testid="file-card"/g) ?? []).length).toBe(2);
    expect(renderToStaticMarkup(createElement(FileCards, { files: [] }))).toBe("");
  });
});
