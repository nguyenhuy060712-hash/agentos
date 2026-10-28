import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MockLiveAdapter } from "../providers/mock-live";
import { GeminiLiveAdapter, GeminiMapper, mapGeminiServerMessage } from "../providers/gemini-live";
import { assertRealCallsApproved } from "../providers/guard";
import { GptLiveAdapter, mapGptLiveEvent } from "../providers/gpt-live";
import { adapterFor, registerProviderAdapter, PROVIDER_ADAPTERS } from "../registry";
import { createProviderVoiceAdapter } from "../provider-voice-adapter";
import type { RealtimeEvent, VoiceGrant } from "../types";

const grant = (o: Partial<VoiceGrant> = {}): VoiceGrant => ({
  voice_session_id: "vs_test", adapter: "mock", mock: true, provider: "google", model: "gemini-3.8-live", transport: "websocket",
  credential: { type: "ephemeral_token", value: "mock", expires_at: 0 }, max_session_s: 600, barge_in: true, language: "en", tools: [], ...o,
});
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

describe("MockLiveAdapter (full duplex)", () => {
  it("barge-in stops playback within 300 ms and goes back to listening", async () => {
    const ev: RealtimeEvent[] = [];
    const conn = await new MockLiveAdapter({ msPerWord: 40, vadDelayMs: 60, thinkMs: 20 }).connect({ grant: grant(), instructions: "", signal: new AbortController().signal, onEvent: (e) => ev.push(e) });
    await sleep(300); // greeting is being spoken
    expect(conn.isSpeaking).toBe(true);
    conn.simulateUserSpeech("tell me something long");
    await sleep(120);
    const intr = ev.find((e) => e.type === "interrupted") as { latencyMs: number } | undefined;
    expect(intr).toBeDefined(); expect(intr!.latencyMs).toBeLessThanOrEqual(300);
    expect(ev.filter((e) => e.type === "mode").map((e) => (e as { mode: string }).mode)).toContain("listening");
    conn.close();
  });
  it("hands tasks to agent_os_delegate and speaks the tool result", async () => {
    const ev: RealtimeEvent[] = [];
    const conn = await new MockLiveAdapter({ msPerWord: 5, vadDelayMs: 5, thinkMs: 5 }).connect({ grant: grant(), instructions: "", signal: new AbortController().signal, onEvent: (e) => ev.push(e) });
    await sleep(400);
    conn.simulateUserSpeech("ask AI Lead about the campaign status");
    await sleep(100);
    const call = ev.find((e) => e.type === "tool_call") as Extract<RealtimeEvent, { type: "tool_call" }>;
    expect(call.name).toBe("agent_os_delegate"); expect(call.args.request).toMatch(/campaign/);
    conn.sendToolResult(call.callId, "Campaign is on track");
    await sleep(200);
    expect(ev.some((e) => e.type === "transcript" && e.role === "assistant" && e.final && e.text.includes("on track"))).toBe(true);
    conn.close();
  });
});

describe("provider registry (A2)", () => {
  it("routes mock grants to the mock and real grants by provider, never by model name", () => {
    expect(adapterFor(grant()).id).toBe("mock");
    expect(adapterFor(grant({ adapter: "provider", mock: false, provider: "google" }))).toBeInstanceOf(GeminiLiveAdapter);
    expect(adapterFor(grant({ adapter: "provider", mock: false, provider: "openai", model: "gpt-live-1" }))).toBeInstanceOf(GptLiveAdapter);
    expect(adapterFor(grant({ adapter: "provider", mock: false, provider: "google", model: "some-future-live-model" }))).toBeInstanceOf(GeminiLiveAdapter);
  });
  it("a third provider is one registered adapter", () => {
    expect(() => adapterFor(grant({ adapter: "provider", mock: false, provider: "acme" }))).toThrow(/no voice adapter/);
    registerProviderAdapter("acme", () => new MockLiveAdapter());
    expect(adapterFor(grant({ adapter: "provider", mock: false, provider: "acme" })).id).toBe("mock");
    delete PROVIDER_ADAPTERS.acme;
  });
  it("guard: only google is approved for real calls (A8); openai stays blocked", () => {
    expect(() => assertRealCallsApproved("google")).not.toThrow();
    expect(() => assertRealCallsApproved("openai")).toThrow(/not approved/);
  });
  it("Gemini adapter never opens a provider socket on a mock grant", async () => {
    const ws = vi.fn(); vi.stubGlobal("WebSocket", ws);
    await expect(new GeminiLiveAdapter().connect({ grant: grant({ mock: true }), instructions: "", signal: new AbortController().signal, onEvent: () => {} })).rejects.toThrow(/mock/);
    expect(ws).not.toHaveBeenCalled(); vi.unstubAllGlobals();
  });
  it("GPT-Live adapter is blocked (gpt-live-1 not approved)", async () => {
    const pc = vi.fn(); vi.stubGlobal("RTCPeerConnection", pc);
    await expect(new GptLiveAdapter(async () => ({ answer: "" })).connect({ grant: grant({ mock: false, provider: "openai" }), instructions: "", signal: new AbortController().signal, onEvent: () => {} })).rejects.toThrow(/not approved/);
    expect(pc).not.toHaveBeenCalled(); vi.unstubAllGlobals();
  });
});

describe("wire mappers", () => {
  it("Gemini: interrupted, transcripts, toolCall, cancellation", () => {
    const m = new GeminiMapper();
    const ev = [
      ...mapGeminiServerMessage({ serverContent: { interrupted: true } }),
      ...m.map({ serverContent: { inputTranscription: { text: "xin chào" } } }),
      ...m.map({ toolCall: { functionCalls: [{ id: "f1", name: "agent_os_delegate", args: { request: "báo cáo" } }] } }),
      ...m.map({ toolCallCancellation: { ids: ["f1"] } }),
    ];
    expect(ev.map((e) => e.type)).toEqual(["interrupted", "mode", "transcript", "transcript", "tool_call", "tool_call_cancelled"]);
  });
  it("Gemini: assembles chunked transcripts into one final line per turn (real A8 message shapes)", () => {
    const m = new GeminiMapper(); const fin = (evs: ReturnType<GeminiMapper["map"]>) => evs.filter((e) => e.type === "transcript" && e.final);
    expect(fin(m.map({ serverContent: { inputTranscription: { text: "Xin chào," } } }))).toEqual([]);
    m.map({ serverContent: { inputTranscription: { text: " bạn khỏe không?" } } });
    const a = fin(m.map({ serverContent: { modelTurn: { parts: [{ inlineData: { mimeType: "audio/pcm;rate=24000", data: "AAAA" } }] }, outputTranscription: { text: "Chào anh" } } }));
    expect(a).toMatchObject([{ role: "user", text: "Xin chào, bạn khỏe không?" }]);
    m.map({ serverContent: { outputTranscription: { text: ", em khỏe." } } });
    const b = fin(m.map({ serverContent: { turnComplete: true } }));
    expect(b).toMatchObject([{ role: "assistant", text: "Chào anh, em khỏe." }]);
    expect(m.map({ usageMetadata: { promptTokenCount: 10, responseTokenCount: 5 } })[0]).toMatchObject({ type: "usage", inputTokens: 10, outputTokens: 5 });
    expect([...m.seen]).toContain("serverContent.outputTranscription");
  });
  it("GPT-Live: speech start -> barge-in signal", () => {
    expect(mapGptLiveEvent({ type: "input_audio.speech_started" }).map((e) => e.type)).toEqual(["user_speech_started", "mode"]);
  });
});

describe("ProviderVoiceAdapter <-> Agent OS", () => {
  let calls: { url: string; body: unknown }[] = [];
  const respond = (status: number, body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
  beforeEach(() => { calls = []; });
  afterEach(() => vi.unstubAllGlobals());
  const hooks = () => ({ log: vi.fn(), onGrantError: vi.fn(), onApproval: vi.fn(), onSession: vi.fn(), onBargeIn: vi.fn(), onThreadChanged: vi.fn() });
  const ctx = () => ({ model: "gpt-live-1", agentId: "agent-2", ensureThread: async () => "thread-0001" });

  for (const [status, label] of [[403, "model not allowed"], [402, "budget"]] as const) {
    it(`surfaces HTTP ${status} (${label}) from POST /api/voice/session`, async () => {
      vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => { calls.push({ url, body: init?.body }); return respond(status, { detail: { code: label, message: label } }); }));
      const h = hooks();
      const session = createProviderVoiceAdapter(ctx, h).connect({});
      await sleep(50);
      expect(h.onGrantError).toHaveBeenCalledOnce();
      expect(h.onGrantError.mock.calls[0][0].status).toBe(status);
      expect(calls[0].url).toBe("/api/voice/session");
      expect(JSON.parse(String(calls[0].body))).toMatchObject({ model: "gpt-live-1", agent_id: "agent-2", thread_id: "thread-0001" });
      session.disconnect();
    });
  }
});

describe("delegated calls: transcript phases + barge-in cancel (fixes)", () => {
  const respond = (body: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
  afterEach(() => { vi.unstubAllGlobals(); registerProviderAdapter("mock", () => new MockLiveAdapter()); });
  const setup = (delegateMs: number) => {
    registerProviderAdapter("mock", () => new MockLiveAdapter({ msPerWord: 15, vadDelayMs: 10, thinkMs: 10 }));
    const calls: { url: string; body: any }[] = []; let release: (() => void) | null = null;
    vi.stubGlobal("fetch", vi.fn((url: string, init?: RequestInit) => {
      const body = init?.body ? JSON.parse(String(init.body)) : null; calls.push({ url, body });
      if (url === "/api/voice/session") return respond({ ...grant({ mock: true, language: "en" }), voice_session_id: "vs_1", max_session_s: 60 });
      if (url === "/api/voice/delegate") return new Promise<Response>((ok) => {
        const done = () => ok(new Response(JSON.stringify({ call_id: body.call_id, run_id: "run-1234567", status: calls.some((c) => c.url.endsWith("/cancel")) ? "cancelled" : "success", reply: "Campaign is on track.", approval: null }), { headers: { "Content-Type": "application/json" } }));
        release = done; setTimeout(done, delegateMs);
      });
      if (url.endsWith("/cancel")) { release?.(); return respond({ call_id: "x", cancelled: true, run_id: "run-1234567" }); }
      if (url === "/api/voice/transcript") return respond({ status: "written", n: body.items.length });
      return respond({ status: "ended", seconds: 1, cost_usd: 0, id: "vs_1" });
    }));
    let conn: any = null;
    const h = { log: vi.fn(), onGrantError: vi.fn(), onApproval: vi.fn(), onSession: vi.fn(), onBargeIn: vi.fn(), onThreadChanged: vi.fn(), expose: (c: unknown) => { if (c) conn = c; } };
    const session = createProviderVoiceAdapter(() => ({ model: "gemini-3.8-live", agentId: "agent-2", ensureThread: async () => "thread-0001" }), h).connect({});
    return { calls, session, h, conn: () => conn };
  };
  it("preamble and read-back are tagged with the call id (attached to the run answer server-side, not new turns)", async () => {
    const t = setup(80);
    await sleep(600); t.conn().simulateUserSpeech("ask AI Lead about the campaign");
    await sleep(2500); t.session.disconnect(); await sleep(100);
    const items = t.calls.filter((c) => c.url === "/api/voice/transcript").flatMap((c) => c.body.items);
    const call = t.calls.find((c) => c.url === "/api/voice/delegate")!.body.call_id;
    expect(items.find((i: any) => i.phase === "preamble")).toMatchObject({ role: "assistant", call_id: call, text: expect.stringMatching(/Let me check/) });
    expect(items.find((i: any) => i.phase === "readback")).toMatchObject({ role: "assistant", call_id: call, text: "Campaign is on track." });
    expect(items.some((i: any) => i.role === "user" && /campaign/.test(i.text))).toBe(false); // the request is the run's own input message
  });
  it("barge-in while the delegated run is in flight cancels it (POST …/cancel) and sends a silent tool result", async () => {
    const t = setup(5000);
    await sleep(600); t.conn().simulateUserSpeech("ask AI Lead about the campaign");
    await sleep(300); t.conn().simulateUserSpeech("never mind");
    await sleep(400); t.session.disconnect(); await sleep(100);
    const c = t.calls.find((x) => x.url.endsWith("/cancel"));
    expect(c?.url).toMatch(/^\/api\/voice\/delegate\/call_u1\/cancel$/); expect(c?.body).toEqual({ reason: "barge_in" });
    expect(t.h.log.mock.calls.flat().join("\n")).toMatch(/cancelled by barge-in -> silent tool result/);
  });
});
