import { afterEach, describe, expect, it } from "vitest";
import { QUEUE_MAX, getQueueMax, setQueueMax, turnQueue } from "../turn-queue";
import { RESULT_MAX, timeline } from "../subagents";

describe("F-1 queue limit from the server (chat.queue_max)", () => {
  afterEach(() => { setQueueMax(undefined); turnQueue.reset(); });
  it("uses the server number, falls back to 5 for anything invalid", () => {
    setQueueMax(2); expect(getQueueMax()).toBe(2);
    expect(turnQueue.add("t", "a")).toBe(true); expect(turnQueue.add("t", "b")).toBe(true); expect(turnQueue.add("t", "c")).toBe(false);
    for (const bad of [undefined, 0, 21, 2.5, "3", null]) { setQueueMax(bad); expect(getQueueMax()).toBe(QUEUE_MAX); }
  });
});

describe("F-1 sub-agent timeline", () => {
  it("lists text, tool calls with arguments and results; skips the human task; cuts huge results", () => {
    const tl = timeline([
      { type: "human", content: "task" },
      { type: "ai", content: "Tôi sẽ tìm", tool_calls: [{ name: "search", args: { q: "x" } }] },
      { type: "tool", name: "search", content: "y".repeat(RESULT_MAX + 50) },
      { type: "ai", content: [{ text: "Xong" }] },
    ]);
    expect(tl.map((i) => i.kind)).toEqual(["text", "call", "result", "text"]);
    expect(tl[1]).toMatchObject({ name: "search" }); expect(tl[1].text).toContain('"q": "x"');
    expect(tl[2].text.length).toBe(RESULT_MAX + 1);
    expect(timeline(undefined as never)).toEqual([]);
  });
});
