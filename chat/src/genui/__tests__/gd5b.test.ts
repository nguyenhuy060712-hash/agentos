import { describe, expect, it } from "vitest";
import { canvasFileName, canvasRef, cronToVi, lineDiff, parseCodeRun, presetCron, viDateTime } from "../gd5b-helpers";
import { isoToVi, maskViDate, parseAnswered, viToIso } from "../gd5a-helpers";
import { modelName } from "../../lib/models";

describe("GĐ5b canvas", () => {
  it("parses the canvas tool result", () => {
    expect(canvasRef("Đã mở canvas «Kế hoạch» bản v3 cho người dùng. [canvas:0a1b2c3d4e5f:3]")).toEqual({ doc: "0a1b2c3d4e5f", v: 3 });
    expect(canvasRef("Không lưu được canvas: tài liệu quá dài.")).toBeNull();
    expect(canvasRef(undefined)).toBeNull();
  });
  it("line diff with folding", () => {
    const a = Array.from({ length: 20 }, (_, i) => `dòng ${i}`).join("\n");
    const b = a.replace("dòng 10", "dòng mười") + "\nthêm";
    const d = lineDiff(a, b, 2);
    expect(d.added).toBe(2); expect(d.removed).toBe(1);
    expect(d.rows.some((r) => r.kind === "fold")).toBe(true);
    expect(d.rows.find((r) => r.kind === "del")).toMatchObject({ text: "dòng 10", a: 11 });
    expect(lineDiff("x\ny", "x\ny").added).toBe(0);
  });
  it("download names are ascii with the right extension", () => {
    expect(canvasFileName("Kế hoạch Đà Nẵng 2026", "document", "markdown")).toBe("Ke_hoach_Da_Nang_2026.md");
    expect(canvasFileName("Phân tích", "code", "python")).toBe("Phan_tich.py");
    expect(canvasFileName("", "code", "rust")).toBe("canvas.txt");
  });
});

describe("GĐ5b code run", () => {
  it("parses ok output with charts", () => {
    const r = parseCodeRun("status=ok exit=0 (812 ms, gvisor (runsc))\n[stdout]\ntổng 42\n[1 biểu đồ đã hiện cho người dùng] [aos-images:abc123]");
    expect(r).toEqual({ status: "ok", exit: 0, ms: 812, runtime: "gvisor (runsc)", stdout: "tổng 42", stderr: "", images: ["abc123"] });
  });
  it("parses errors and refusals", () => {
    const r = parseCodeRun("status=error exit=1 (90 ms, x)\n[stdout]\na\n[stderr]\nTraceback\nValueError: x");
    expect(r?.stdout).toBe("a"); expect(r?.stderr).toBe("Traceback\nValueError: x"); expect(r?.images).toEqual([]);
    expect(parseCodeRun("Chạy code đang bị admin tắt.")).toBeNull();
    expect(parseCodeRun("status=timeout exit=-1 (30000 ms, x)")?.status).toBe("timeout");
  });
});

describe("GĐ5b schedules", () => {
  it("describes common crons in Vietnamese", () => {
    expect(cronToVi("30 8 * * 1")).toBe("Thứ 2 hằng tuần lúc 08:30");
    expect(cronToVi("0 9 * * 1-5")).toBe("Thứ 2 đến Thứ 6 lúc 09:00");
    expect(cronToVi("0 17 * * 5")).toBe("Thứ 6 hằng tuần lúc 17:00");
    expect(cronToVi("0 7 * * *")).toBe("Hằng ngày lúc 07:00");
    expect(cronToVi("15 6 1 * *")).toBe("Ngày 1 hằng tháng lúc 06:15");
    expect(cronToVi("*/30 * * * *")).toBe("Mỗi 30 phút");
    expect(cronToVi("0 0 * * 0")).toBe("Chủ nhật hằng tuần lúc 00:00");
    expect(cronToVi("0 8 1 1 *")).toBeNull();
  });
  it("builds crons from presets", () => {
    expect(presetCron("weekdays", "08:30")).toBe("30 8 * * 1-5");
    expect(presetCron("weekly", "17:05", { dow: 5 })).toBe("5 17 * * 5");
    expect(presetCron("monthly", "06:00", { dom: 15 })).toBe("0 6 15 * *");
    expect(presetCron("custom", "", { custom: " 0 8 * * * " })).toBe("0 8 * * *");
  });
  it("formats times in ICT dd/mm/yyyy", () => {
    expect(viDateTime("2026-10-21T01:30:00Z")).toBe("21/10/2026 08:30");
    expect(viDateTime(null)).toBe("");
  });
});

describe("GĐ5b small fixes", () => {
  it("ask-card dates are dd/mm/yyyy", () => {
    expect(viToIso("01/11/2026")).toBe("2026-11-01"); expect(viToIso("1/11/2026")).toBe("2026-11-01");
    expect(viToIso("31/02/2026")).toBeNull(); expect(viToIso("2026-11-01")).toBeNull();
    expect(maskViDate("01112026")).toBe("01/11/2026"); expect(maskViDate("0111")).toBe("01/11");
    expect(isoToVi("2026-11-01")).toBe("01/11/2026");
    expect(parseAnswered('Người dùng đã trả lời: {"Ngày bắt đầu": "2026-11-01"}')).toEqual([["Ngày bắt đầu", "01/11/2026"]]);
  });
  it("header model labels", () => {
    expect(modelName("gpt-5.4-nano")).toBe("GPT-5.4 nano");
    expect(modelName("gemini-3.6-flash")).toBe("Gemini 3.6 Flash");
    expect(modelName(undefined)).toBe("");
  });
});
