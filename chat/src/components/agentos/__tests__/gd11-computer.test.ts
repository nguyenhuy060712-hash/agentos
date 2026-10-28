import { describe, expect, it, vi } from "vitest";

vi.hoisted(() => { Object.assign(globalThis, { window: { location: { origin: "http://localhost" } } }); });   // lib/agentos.ts reads window.location at import
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import "@/i18n";
import { CommandList, FileList, StatusChip } from "../ComputerPanel";
import { FileDiffCard } from "@/genui/FileDiffCard";
import { crumbs, fmtSize, lineDiff, moveTarget, parentOf, sortEntries, statusChip } from "../computer-helpers";
import { activateComputer, getThreadBox, listBoxFiles, runBoxCommand } from "@/lib/agentos";

const noop = () => {};
describe("GĐ11.52 helpers", () => {
  it("status chip: running / paused / error / warning", () => {
    expect(statusChip({ status: "running" }).key).toBe("running");
    expect(statusChip({ status: "stopped" }).key).toBe("paused");
    expect(statusChip({ status: "failed" }).key).toBe("error");
    expect(statusChip({ status: "running", last_error: "aos-boot: setup.sh failed" })).toEqual({ key: "warn", tone: "warn" });
    expect(statusChip({ status: "running", disk_warn: true }).key).toBe("warn");
  });
  it("paths: crumbs, parent, rename/move stays inside /workspace", () => {
    expect(crumbs("/workspace/a/b").map((c) => c.path)).toEqual(["/workspace", "/workspace/a", "/workspace/a/b"]);
    expect(parentOf("/workspace/a/b.txt")).toBe("/workspace/a"); expect(parentOf("/workspace")).toBe("/workspace");
    expect(moveTarget("/workspace/a/b.txt", "c.txt")).toBe("/workspace/a/c.txt");
    expect(moveTarget("/workspace/a/b.txt", "x/c.txt")).toBe("/workspace/x/c.txt");
    expect(moveTarget("/workspace/a/b.txt", "/workspace/z.txt")).toBe("/workspace/z.txt");
    expect(moveTarget("/workspace/a/b.txt", "/etc/passwd")).toBeNull(); expect(moveTarget("/workspace/a/b.txt", "../../etc")).toBeNull(); expect(moveTarget("/workspace/a", "  ")).toBeNull();
  });
  it("entries: folders first, A-Z, filter; sizes", () => {
    const e = [{ path: "/workspace/b.txt", name: "b.txt", type: "file", size: 5 }, { path: "/workspace/z", name: "z", type: "dir", size: 0 }, { path: "/workspace/a.md", name: "a.md", type: "file", size: 2048 }];
    expect(sortEntries(e).map((x) => x.name)).toEqual(["z", "a.md", "b.txt"]); expect(sortEntries(e, "MD").map((x) => x.name)).toEqual(["a.md"]);
    expect(fmtSize(5)).toBe("5 B"); expect(fmtSize(2048)).toBe("2 KB");
  });
  it("line diff: write_file = all added, edit_file = changed lines only", () => {
    const w = lineDiff("", "a\nb"); expect([w.added, w.removed]).toEqual([2, 0]);
    const d = lineDiff("one\ntwo\nthree", "one\nTWO\nthree"); expect([d.added, d.removed]).toEqual([1, 1]);
    expect(d.lines.map((l) => l.op + l.text)).toEqual([" one", "-two", "+TWO", " three"]);
  });
});

describe("GĐ11.52 views", () => {
  it("status chip renders the Vietnamese label", () => {
    const h = renderToStaticMarkup(createElement(StatusChip, { box: { status: "stopped" } }));
    expect(h).toContain('data-state="paused"'); expect(h).toContain("Tạm nghỉ");
  });
  it("file list: folders first, search filter, empty state", () => {
    const items = [{ path: "/workspace/a.txt", name: "a.txt", type: "file", size: 10 }, { path: "/workspace/uploads", name: "uploads", type: "dir", size: 0 }];
    const h = renderToStaticMarkup(createElement(FileList, { path: "/workspace", items, q: "", onOpen: noop, onCrumb: noop, onRename: noop }));
    expect(h.indexOf("uploads")).toBeLessThan(h.indexOf("a.txt")); expect(h).toContain('data-testid="computer-crumbs"');
    const none = renderToStaticMarkup(createElement(FileList, { path: "/workspace", items, q: "zzz", onOpen: noop, onCrumb: noop, onRename: noop }));
    expect(none).toContain('data-testid="computer-empty"');
  });
  it("commands: exit code, time and short output", () => {
    const h = renderToStaticMarkup(createElement(CommandList, { runs: [{ command: "ls", stdout: "a.txt", exit_code: 0, success: true, truncated: false, timed_out: false, duration_ms: 1500 }],
      history: [{ id: 1, command: "false", exit_code: 1, duration_ms: 20 }] }));
    expect(h).toContain("$ ls"); expect(h).toContain("a.txt"); expect(h).toContain("1.5 s"); expect(h).toContain("mã thoát 1");
  });
  it("diff card colours added and removed lines", () => {
    const h = renderToStaticMarkup(createElement(FileDiffCard, { tool: "edit_file", args: { file_path: "/workspace/x.py", old_string: "a = 1", new_string: "a = 2" } }));
    expect(h).toContain('data-op="-"'); expect(h).toContain('data-op="+"'); expect(h).toContain("/workspace/x.py");
  });
});

describe("GĐ11.52 API", () => {
  it("finds the thread's computer, activates, lists files, runs a command", async () => {
    const f = vi.fn(async (u: string) => {
      if (u.includes("boxes?thread_id")) return new Response(JSON.stringify({ items: [{ id: "f".repeat(16), status: "failed" }, { id: "a".repeat(16), status: "running" }] }));
      if (u.includes("/computer/activate")) return new Response(JSON.stringify({ id: "b".repeat(16), status: "running" }));
      if (u.includes("/files?")) return new Response(JSON.stringify({ path: "/workspace", items: [], truncated: false }));
      return new Response(JSON.stringify({ stdout: "hi", exit_code: 0, success: true, truncated: false, timed_out: false, duration_ms: 3 }));
    });
    vi.stubGlobal("fetch", f);
    expect((await getThreadBox("t1"))?.id).toBe("a".repeat(16));
    expect((await activateComputer("t 1")).status).toBe("running"); expect(f.mock.calls[1][0]).toBe("/api/threads/t%201/computer/activate");
    expect((await listBoxFiles("a", "/workspace")).path).toBe("/workspace");
    await runBoxCommand("a", "echo hi"); const init = f.mock.calls[3] as unknown as [string, RequestInit]; expect(JSON.parse(init[1].body as string)).toEqual({ command: "echo hi", timeout: 60 });
    vi.unstubAllGlobals();
  });
});

import { totalFiles } from "../computer-helpers";
describe("F-4 files counter", () => {
  it("adds the computer's top-level items to the output files, without counting the out folder twice", () => {
    expect(totalFiles(0, [])).toBe(0);
    expect(totalFiles(0, [{ name: "a.txt", type: "file" }, { name: "uploads", type: "dir" }])).toBe(2);
    expect(totalFiles(3, [{ name: "out", type: "dir" }, { name: "x.py", type: "file" }])).toBe(4);
  });
});
