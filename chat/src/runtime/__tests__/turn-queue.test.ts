import { beforeEach, describe, expect, it } from "vitest";
import { QUEUE_MAX, REJECT_MESSAGE, answerText, clockLabel, mmss, phaseFromEvent, queuedToText, rejectAll, stepsMarkdown, turnClock, turnQueue } from "../turn-queue";

const t = (k: string, o?: Record<string, unknown>) => ({ clockThinking: "Đang suy nghĩ…", clockWriting: "Đang viết câu trả lời…" } as Record<string, string>)[k] ?? `Đang gọi công cụ ${o?.name}…`;

describe("GĐ11.36 turn queue", () => {
  beforeEach(() => { turnQueue.reset(); turnClock.stop(); });

  it("rejects every pending action (never approves) with the fixed message", () => {
    const d = rejectAll(3);
    expect(d).toHaveLength(3);
    expect(d.every((x) => x.type === "reject" && x.message === REJECT_MESSAGE)).toBe(true);
  });

  it("queues up to 5 per thread, oldest first, then refuses", () => {
    for (let i = 0; i < QUEUE_MAX; i++) expect(turnQueue.add("t1", `m${i}`)).toBe(true);
    expect(turnQueue.add("t1", "m6")).toBe(false);
    expect(turnQueue.add("t2", "other")).toBe(true);      // per thread
    expect(turnQueue.get("t1").map((q) => q.text)).toEqual(["m0", "m1", "m2", "m3", "m4"]);
    expect(turnQueue.take("t1")!.text).toBe("m0");
    expect(turnQueue.get("t1")).toHaveLength(4);
    expect(turnQueue.add("t1", "   ")).toBe(false);        // blank is not queued
  });

  it("edit gives the message back, delete drops it; the canvas chip becomes a reference line", () => {
    turnQueue.add("t1", "a", "doc-7"); turnQueue.add("t1", "b");
    const [a, b] = turnQueue.get("t1");
    expect(queuedToText(a!)).toBe("a\n\n[canvas: doc-7]");
    expect(turnQueue.edit("t1", a!.id)!.text).toBe("a");
    turnQueue.remove("t1", b!.id);
    expect(turnQueue.get("t1")).toEqual([]);
  });

  it("clock labels rotate thinking -> tool -> writing from stream events", () => {
    turnClock.start(1000);
    expect(clockLabel(turnClock.get(), 13_000, t)).toBe("Đang suy nghĩ… 0:12");
    turnClock.feed("updates", { send_email: { kind: "tool", name: "send_email" } });
    expect(clockLabel(turnClock.get(), 14_000, t)).toBe("Đang gọi công cụ send_email…");
    turnClock.feed("updates", { send_email: { kind: "result", name: "send_email" } });
    expect(turnClock.get().phase).toBe("thinking");
    turnClock.feed("messages-tuple", [{ type: "AIMessageChunk", content: "Xin" }, {}]);
    expect(clockLabel(turnClock.get(), 15_000, t)).toBe("Đang viết câu trả lời…");
    turnClock.stop(); turnClock.feed("updates", { x: { kind: "tool", name: "x" } });
    expect(turnClock.get().phase).toBe("idle");          // events outside a turn are ignored
    expect(phaseFromEvent("messages", [{ content: "" }])).toBeNull();
    expect(phaseFromEvent("messages", [{ tool_call_chunks: [{ name: "web_search" }] }])).toEqual({ phase: "tool", tool: "web_search" });
    expect(mmss(75_000)).toBe("1:15");
  });
});

describe("GĐ11.36 copy", () => {
  const parts = [
    { type: "reasoning", text: "cần tra cứu" },
    { type: "tool-call", toolName: "web_search", args: { q: "giá vàng" }, result: "x".repeat(5000) },
    { type: "tool-call", toolName: "send_email", args: { to: "a@b.c" }, result: undefined },
    { type: "text", text: "Giá vàng hôm nay là 80 triệu." },
  ];
  it("answer copy = text only", () => expect(answerText(parts)).toBe("Giá vàng hôm nay là 80 triệu."));
  it("steps copy contains tool calls, args, results cut at 4000 chars, and the answer", () => {
    const md = stepsMarkdown(parts);
    expect(md).toContain("**web_search**");
    expect(md).toContain("giá vàng");
    expect(md).toContain("**send_email**");
    expect(md).toContain("Giá vàng hôm nay là 80 triệu.");
    expect(md).toContain("x".repeat(4000) + "…");
    expect(md).not.toContain("x".repeat(4001));
  });
});
