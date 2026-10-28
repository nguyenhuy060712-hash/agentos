import { describe, expect, it } from "vitest";
import { PlaybackTracker, cutChars } from "../playback-tracker";
import { GeminiMapper } from "../providers/gemini-live";
import { MockLiveAdapter } from "../providers/mock-live";
import { toStandardBlocks } from "../../runtime/attachments";
import { subagentStore } from "../../runtime/subagents";
import type { RealtimeEvent, VoiceGrant } from "../types";

const grant = (): VoiceGrant => ({ voice_session_id: "vs", adapter: "mock", mock: true, provider: "google", model: "gemini-3.8-live", transport: "websocket",
  credential: { type: "ephemeral_token", value: "m", expires_at: 0 }, max_session_s: 600, barge_in: true, language: "en", tools: [] });
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

describe("interrupted voice transcript = only what was played", () => {
  it("proportional split when the text carries no timing (all text before audio)", () => {
    expect(cutChars([{ text: "aaaa bbbb cccc dddd", audioPos: 0 }], 1, 4)).toBe(4);
  });
  it("maps played audio onto transcription chunks by timing", () => {
    // chunk 1 transcribes audio 0-1 s, chunk 2 1-3 s, chunk 3 3-4 s
    const ch = [{ text: "one two ", audioPos: 1 }, { text: "three four ", audioPos: 3 }, { text: "five", audioPos: 4 }];
    expect(cutChars(ch, 1, 4)).toBe(8);            // exactly the first chunk
    expect(cutChars(ch, 2, 4)).toBe(8 + 5);        // half of chunk 2
    expect(cutChars(ch, 4, 4)).toBe(23);
  });
  it("tracker sums played queue time, snaps to a word boundary and flags the cut", () => {
    const t = new PlaybackTracker();
    t.audio(10, 1); t.audio(11, 1); t.audio(12, 1); t.audio(13, 1);
    t.text("Hello there, this is a fairly long answer");
    const c = t.cut(11.5)!;
    expect(c.truncated).toBe(true); expect(c.playedMs).toBe(1500); expect(c.generatedMs).toBe(4000);
    expect("Hello there, this is a fairly long answer".startsWith(c.text)).toBe(true);
    expect(c.text.endsWith(" ")).toBe(false); expect(c.text.split(" ").length).toBeLessThan(8);
    expect(t.cut(20)).toBeNull();                  // everything heard -> no cut
  });
  it("Gemini mapper saves the cut text (no ' —' marker) with truncation metadata", () => {
    const m = new GeminiMapper();
    m.map({ serverContent: { outputTranscription: { text: "The campaign is doing well and will" } } });
    const out = m.map({ serverContent: { interrupted: true } }, { text: "The campaign is", truncated: true, playedMs: 900, generatedMs: 3000, fullChars: 35 });
    const fin = out.find((e) => e.type === "transcript") as Extract<RealtimeEvent, { type: "transcript" }>;
    expect(fin.text).toBe("The campaign is"); expect(fin.truncated?.playedMs).toBe(900); expect(fin.text).not.toContain("—");
  });
  it("mock provider (text streams ahead of audio): barge-in saves only the played words", async () => {
    const ev: RealtimeEvent[] = [];
    const conn = await new MockLiveAdapter({ msPerWord: 60, vadDelayMs: 10, thinkMs: 10, textLead: 4 }).connect({ grant: grant(), instructions: "", signal: new AbortController().signal, onEvent: (e) => ev.push(e) });
    await sleep(200 + 60 * 5);   // greeting ~13 words; ~5 played, transcription already further ahead
    conn.simulateUserSpeech("stop");
    await sleep(60);
    const shown = Math.max(...ev.filter((e) => e.type === "transcript" && !e.final && e.role === "assistant").map((e) => (e as { text: string }).text.length));
    const fin = ev.find((e) => e.type === "transcript" && e.final && e.role === "assistant") as Extract<RealtimeEvent, { type: "transcript" }>;
    expect(fin.truncated).toBeDefined(); expect(fin.text.length).toBeLessThan(shown);
    expect(fin.truncated!.playedMs).toBeLessThan(fin.truncated!.generatedMs);
    conn.close();
  });
});

describe("G4 image content blocks", () => {
  it("legacy image_url blocks become LangChain standard image blocks", () => {
    const m = toStandardBlocks({ type: "human", content: [{ type: "text", text: " " }, { type: "text", text: "what is it" }, { type: "image_url", image_url: { url: "/api/files/x" } }] });
    expect(m.content).toEqual([{ type: "text", text: "what is it" }, { type: "image", url: "/api/files/x" }]);
  });
});

describe("G1 sub-agent progress store", () => {
  it("routes '<mode>|<ns>' events, keeps sub tokens out of the main thread, matches the task by description", () => {
    expect(subagentStore.route("values|tools:abc", { messages: [{ type: "human", content: "publish the summary" }, { type: "ai", content: "", tool_calls: [{ name: "content_publish" }] }] })).toBe(false);
    expect(subagentStore.route("messages|tools:abc", [{ content: "Done" }, {}])).toBe(true);
    expect(subagentStore.route("updates|tools:abc", { __interrupt__: [{ value: {} }] })).toBe(false);
    expect(subagentStore.route("values", { messages: [] })).toBe(false);
    const e = subagentStore.find("publish the summary")!;
    expect(e.ns).toBe("tools:abc"); expect(e.status).toBe("interrupted"); expect(e.live).toBe("Done");
  });
});
