/**
 * Real provider calls are allowed per provider. A8 approved 20/10/2026 17:33 UTC+7 for google (gemini-3.8-live) ONLY,
 * with a $1.00 hard cap (server aborts at $0.80). openai (gpt-live-1) stays blocked.
 * The server must ALSO agree (grant.mock === false); this is the client-side second lock.
 */
export const REAL_PROVIDERS_APPROVED: ReadonlySet<string> = new Set(["google"]);
export function assertRealCallsApproved(provider: string) {
  if (!REAL_PROVIDERS_APPROVED.has(provider)) throw new Error(`${provider}: real provider calls are not approved (Phase 0)`);
}
