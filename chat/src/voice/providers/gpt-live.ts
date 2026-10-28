import type { LiveConnectOptions, LiveConnection, LiveProviderAdapter, RealtimeEvent } from "../types";
import { assertRealCallsApproved } from "./guard";

/**
 * GptLiveAdapter (gpt-live-1). Browser <-> OpenAI over WebRTC: audio on media tracks, JSON events on a data channel.
 * Agent OS creates the session server-side (client.live.create, delegation {type:"client"}) and exchanges the browser SDP offer
 * for the answer, so no key or token reaches the browser. Agent OS also attaches a SIDEBAND to receive delegation + transcript
 * events itself (the browser cannot forge tool results). Design §5.1, §7.
 * NOT executed in Phase 0. Event names from developers.openai.com/api/docs/guides/live* — verify at build time.
 */
type GptEvt = { type: string; delegation?: { id: string }; transcript?: string; role?: "user" | "assistant"; item_id?: string; final?: boolean };

export function mapGptLiveEvent(e: GptEvt): RealtimeEvent[] {
  switch (e.type) {
    case "session.started": return [{ type: "state", state: "connected" }];
    case "input_audio.speech_started": return [{ type: "user_speech_started", at: performance.now() }, { type: "mode", mode: "listening" }];
    case "output_audio.started": return [{ type: "mode", mode: "speaking" }];
    case "output_audio.interrupted": return [{ type: "interrupted", latencyMs: 0 }, { type: "mode", mode: "listening" }];
    case "output_audio.done": return [{ type: "mode", mode: "listening" }];
    case "transcript.delta": case "transcript.done":
      return e.transcript ? [{ type: "transcript", role: e.role ?? "assistant", text: e.transcript, final: e.type === "transcript.done", itemId: e.item_id ?? "" }] : [];
    case "session.delegation.created": return [{ type: "mode", mode: "thinking" }]; // handled by Agent OS via sideband, not by the browser
    default: return [];
  }
}

export class GptLiveAdapter implements LiveProviderAdapter {
  readonly id = "openai";
  private mintWithSdp: (sdp: string) => Promise<{ answer: string }>;
  constructor(mintWithSdp: (sdp: string) => Promise<{ answer: string }>) { this.mintWithSdp = mintWithSdp; }
  async connect(o: LiveConnectOptions): Promise<LiveConnection> {
    assertRealCallsApproved("openai");
    const pc = new RTCPeerConnection();
    const mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    mic.getTracks().forEach((t) => pc.addTrack(t, mic));
    const out = new Audio(); out.autoplay = true; pc.ontrack = (ev) => { out.srcObject = ev.streams[0]; };
    const dc = pc.createDataChannel("events");
    dc.onmessage = (ev) => { for (const e of mapGptLiveEvent(JSON.parse(ev.data) as GptEvt)) o.onEvent(e); };
    await pc.setLocalDescription(await pc.createOffer());
    const { answer } = await this.mintWithSdp(pc.localDescription!.sdp);
    await pc.setRemoteDescription({ type: "answer", sdp: answer });
    const send = (x: unknown) => dc.readyState === "open" && dc.send(JSON.stringify(x));
    return {
      sendText: (text) => send({ type: "conversation.item.create", item: { role: "user", content: [{ type: "input_text", text }] } }),
      sendToolResult: () => { /* gpt-live-1: results are appended by Agent OS over the sideband (session.commentary.append) */ },
      injectContext: () => { /* idem: server-side commentary.append */ },
      interrupt: () => { out.pause(); send({ type: "response.cancel" }); },
      setMuted: (m) => mic.getAudioTracks().forEach((t) => (t.enabled = !m)),
      close: () => { mic.getTracks().forEach((t) => t.stop()); pc.close(); o.onEvent({ type: "state", state: "closed" }); },
    };
  }
}
