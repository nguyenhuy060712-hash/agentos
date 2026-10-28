import type { LiveProviderAdapter, ProviderId, VoiceGrant } from "./types";
import { MockLiveAdapter } from "./providers/mock-live";
import { GeminiLiveAdapter } from "./providers/gemini-live";
import { GptLiveAdapter } from "./providers/gpt-live";

/**
 * Provider registry keyed by catalog `provider` (A2): a new MODEL of a known provider = one catalog row in Settings, no code;
 * a new PROVIDER = one adapter registered here. Callers never branch on model names.
 */
export type AdapterFactory = (grant: VoiceGrant) => LiveProviderAdapter;
export const PROVIDER_ADAPTERS: Record<ProviderId, AdapterFactory> = {
  mock: () => new MockLiveAdapter(),
  google: () => new GeminiLiveAdapter(),
  openai: () => new GptLiveAdapter(async () => { throw new Error("SDP exchange happens in POST /api/voice/session (Phase 1)"); }),
};
export function registerProviderAdapter(id: ProviderId, f: AdapterFactory) { PROVIDER_ADAPTERS[id] = f; }
export function adapterFor(grant: VoiceGrant): LiveProviderAdapter {
  const f = grant.adapter === "mock" || grant.mock ? PROVIDER_ADAPTERS.mock : PROVIDER_ADAPTERS[grant.provider];
  if (!f) throw new Error(`no voice adapter registered for provider '${grant.provider}'`);
  return f(grant);
}
