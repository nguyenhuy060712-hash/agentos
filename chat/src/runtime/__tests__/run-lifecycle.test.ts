import { describe, expect, it, vi } from "vitest";
import { awaitActiveRun, stopRun, type RunsClient } from "@/runtime/run-lifecycle";

// GĐ9.P1: closing the tab must not cancel a running run; only an explicit Stop does; reopening waits for the still-running run.
const mk = (over: Partial<RunsClient["runs"]> = {}): RunsClient => ({
  runs: { cancel: vi.fn(async () => ({})), list: vi.fn(async () => []), join: vi.fn(async () => ({})), ...over },
});

describe("stopRun (explicit Stop)", () => {
  it("cancels the known run with action interrupt", async () => {
    const c = mk();
    expect(await stopRun(c, "t1", "r1")).toBe(true);
    expect(c.runs.cancel).toHaveBeenCalledWith("t1", "r1", false, "interrupt");
  });
  it("without a run id cancels the thread's active runs only", async () => {
    const c = mk({ list: vi.fn(async () => [{ run_id: "a", status: "running" }, { run_id: "b", status: "success" }, { run_id: "c", status: "pending" }]) });
    expect(await stopRun(c, "t1", undefined)).toBe(true);
    expect(c.runs.cancel).toHaveBeenCalledTimes(2);
    expect(c.runs.cancel).toHaveBeenCalledWith("t1", "a", false, "interrupt");
    expect(c.runs.cancel).toHaveBeenCalledWith("t1", "c", false, "interrupt");
  });
  it("never throws when the run is already finished", async () => {
    const c = mk({ cancel: vi.fn(async () => { throw new Error("404"); }) });
    expect(await stopRun(c, "t1", "r1")).toBe(false);
  });
});

describe("awaitActiveRun (thread re-opened after the tab was closed)", () => {
  it("joins a running run so the next state read has the answer", async () => {
    const c = mk({ list: vi.fn(async () => [{ run_id: "r9", status: "running" }]) });
    expect(await awaitActiveRun(c, "t1")).toBe(true);
    expect(c.runs.join).toHaveBeenCalledWith("t1", "r9", expect.objectContaining({ signal: expect.anything() }));
  });
  it("does nothing for a finished or empty thread", async () => {
    const done = mk({ list: vi.fn(async () => [{ run_id: "r1", status: "success" }]) });
    expect(await awaitActiveRun(done, "t1")).toBe(false); expect(done.runs.join).not.toHaveBeenCalled();
    expect(await awaitActiveRun(mk(), "t1")).toBe(false);
  });
  it("a failing join (timeout / network) never blocks the thread from loading", async () => {
    const c = mk({ list: vi.fn(async () => [{ run_id: "r9", status: "pending" }]), join: vi.fn(async () => { throw new Error("timeout"); }) });
    expect(await awaitActiveRun(c, "t1")).toBe(false);
  });
});
