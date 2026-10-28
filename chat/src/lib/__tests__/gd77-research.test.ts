import { afterEach, describe, expect, it, vi } from "vitest";
import { canvasPdfUrl, getCanvasPdf, planDecision, planOf, researchStore, withConfigurable, RESEARCH_MODE } from "../research";

describe("GĐ7.7 deep research", () => {
  afterEach(() => { researchStore.set(null, false); researchStore.set("t1", false); vi.unstubAllGlobals(); });
  it("toggle is per chat, off by default, and sets configurable.mode next to the model", () => {
    expect(researchStore.get("t1")).toBe(false);
    researchStore.set("t1", true);
    expect(researchStore.get("t1")).toBe(true); expect(researchStore.get("t2")).toBe(false);
    const cfg = withConfigurable({ runConfig: { configurable: { model: "m" } } }, { mode: RESEARCH_MODE });
    expect(cfg.runConfig.configurable).toEqual({ model: "m", mode: "deep_research" });
    expect(withConfigurable({ a: 1 }, {})).toEqual({ a: 1 });
  });
  it("a new chat's choice moves to its thread on the first run", () => {
    researchStore.set(null, true);
    researchStore.adopt("t3");
    expect(researchStore.get("t3")).toBe(true); expect(researchStore.get(null)).toBe(false);
  });
  it("propose_plan: plan text and approve/edit decisions", () => {
    expect(planOf([{ name: "propose_plan", args: { plan: "# Kế hoạch" } }])).toBe("# Kế hoạch");
    expect(planOf([{ name: "ads_create_campaign", args: {} }])).toBeNull();
    expect(planDecision("approve", "propose_plan", "x")).toEqual({ type: "approve" });
    expect(planDecision("edit", "propose_plan", "mới")).toEqual({ type: "edit", edited_action: { name: "propose_plan", args: { plan: "mới" } } });
  });
  it("PDF button calls the right URL", async () => {
    expect(canvasPdfUrl("abc123")).toBe("/api/chat/canvas/abc123/pdf");
    expect(canvasPdfUrl("abc123", 3)).toBe("/api/chat/canvas/abc123/pdf?v=3");
    const f = vi.fn(async () => new Response(new Blob(["%PDF"]), { status: 200 })); vi.stubGlobal("fetch", f);
    const b = await getCanvasPdf("abc123", 2);
    expect(f).toHaveBeenCalledWith("/api/chat/canvas/abc123/pdf?v=2"); expect(b.size).toBe(4);
  });
});
