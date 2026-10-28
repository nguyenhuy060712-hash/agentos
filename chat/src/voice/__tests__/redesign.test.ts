// Redesign regressions: composer Mic = dictation on the SAME live model (mock), read aloud on the same live model, chat titles.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DictationAdapter } from "@assistant-ui/react";
import { LiveDictationAdapter, dictationStore } from "../dictation";
import { LiveReadAloudAdapter } from "../readaloud";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
type Call = { url: string; body: Record<string, unknown> };
let calls: Call[] = [];
const grant = (purpose: string) => ({ voice_session_id: `vs_${purpose}`, adapter: "mock", mock: true, provider: "google", model: "gemini-3.8-live", transport: "websocket", purpose,
  credential: { type: "ephemeral_token", value: "m", expires_at: 0 }, max_session_s: 600, barge_in: true, language: "vi", tools: [], thread_id: null });
beforeEach(() => {
  calls = []; vi.stubGlobal("window", globalThis);
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: { body?: string }) => {
    const body = init?.body ? JSON.parse(init.body) : {}; calls.push({ url, body });
    const res = url === "/api/voice/session" ? grant(body.purpose) : { status: "ended" };
    return new Response(JSON.stringify(res), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
});
afterEach(() => vi.unstubAllGlobals());
const until = async (f: () => boolean, ms = 3000) => { const t = Date.now(); while (!f()) { if (Date.now() - t > ms) throw new Error("timeout"); await sleep(20); } };

describe("composer Mic = dictation (same live model, never sent)", () => {
  const mk = () => new LiveDictationAdapter(() => ({ model: "gemini-3.8-live", agentId: "agent-1" }), { log: () => {}, onError: (e) => { throw e; } });
  it("mints a dictation session for the selected live model, streams interim text, ✓ emits one final result", async () => {
    const s = mk().listen(); const got: DictationAdapter.Result[] = []; s.onSpeech((r) => got.push(r));
    await until(() => s.status.type === "running" && !!window.__aosDictation);
    expect(calls[0].body).toMatchObject({ model: "gemini-3.8-live", agent_id: "agent-1", thread_id: null, purpose: "dictation" });
    expect(dictationStore.get().active).toBe(true);
    window.__aosDictation!.say("xin chào em ơi"); await sleep(600);
    expect(got.some((r) => !r.isFinal && r.transcript.length > 0)).toBe(true);
    await s.stop();
    const fin = got.filter((r) => r.isFinal); expect(fin).toHaveLength(1); expect(fin[0].transcript).toBe("xin chào em ơi");
    expect(s.status).toEqual({ type: "ended", reason: "stopped" }); expect(dictationStore.get().active).toBe(false);
    const end = calls.find((c) => c.url.endsWith("/end")); expect(end?.body.reason).toBe("dictation-confirm");
  });
  it("X cancels: no final result, session ended as cancelled", async () => {
    const a = mk(); const s = a.listen(); const got: DictationAdapter.Result[] = []; s.onSpeech((r) => got.push(r));
    await until(() => !!window.__aosDictation); window.__aosDictation!.say("bỏ đi"); await sleep(300);
    a.cancelActive(); await sleep(50);
    expect(got.some((r) => r.isFinal)).toBe(false); expect(s.status).toEqual({ type: "ended", reason: "cancelled" });
    expect(calls.find((c) => c.url.endsWith("/end"))?.body.reason).toBe("dictation-cancel");
  });
});

describe("read aloud = same live model (purpose readaloud)", () => {
  it("speaks the text and finishes", async () => {
    const u = new LiveReadAloudAdapter(() => ({ model: "gemini-3.8-live", agentId: "agent-1" }), () => {}).speak("Xin chào anh.");
    const seen: string[] = []; u.subscribe(() => seen.push(u.status.type));
    await until(() => u.status.type === "ended", 8000);
    expect(calls[0].body).toMatchObject({ purpose: "readaloud", model: "gemini-3.8-live", thread_id: null });
    expect(seen).toContain("running"); expect(u.status).toEqual({ type: "ended", reason: "finished" });
  });
});

describe("chat titles", () => {
  it("titleFrom strips attachments and cuts at 60 chars", async () => {
    vi.stubGlobal("location", new URL("http://localhost:8020/chat/")); const { titleFrom } = await import("../../runtime/useAgentOSRuntime");
    expect(titleFrom('tóm tắt <attachment name="a.txt">nội dung</attachment>')).toBe("tóm tắt");
    const long = "một hai ba bốn năm sáu bảy tám chín mười ".repeat(4); const t = titleFrom(long);
    expect(t.length).toBeLessThanOrEqual(61); expect(long.startsWith(t.replace(/…$/, ""))).toBe(true);
  }, 30_000);   // the dynamic import loads the whole chat runtime: 5–12 s on a busy Windows dev machine
});
