// GĐ4 voice hardening: Gemini Live connection resumption policy (pure, unit-tested).
// Facts (ai.google.dev/gemini-api/docs/live-session and /ephemeral-tokens, fetched 20/10/2026 22:45 ICT):
//  - a Live connection lasts ~10 min; the server sends GoAway{timeLeft} before it ends;
//  - with `sessionResumption` in setup the server sends SessionResumptionUpdate{newHandle,resumable}; a new connection with
//    setup.sessionResumption.handle continues the SAME session (handles valid 2 h after termination);
//  - the reconnect may reuse the SAME ephemeral token even with uses:1, until the token's expireTime.
export const RETRY_DELAYS_MS = [300, 1000, 2500, 5000];

/** "4.5s" / "10s" / {seconds,nanos} -> ms (protobuf Duration JSON); unknown -> fallback */
export function durationMs(d: unknown, fallback = 5000): number {
  if (typeof d === "string") { const m = /^(\d+(?:\.\d+)?)s$/.exec(d.trim()); if (m) return Math.round(parseFloat(m[1]) * 1000); }
  if (d && typeof d === "object") { const o = d as { seconds?: number | string; nanos?: number }; if (o.seconds !== undefined) return Number(o.seconds) * 1000 + Math.round((o.nanos ?? 0) / 1e6); }
  return fallback;
}

export class ResumeState {
  handle: string | null = null;
  attempts = 0;
  resumes = 0;
  private expiresAtSec: number;
  constructor(expiresAtSec: number) { this.expiresAtSec = expiresAtSec; }
  setExpiry(expiresAtSec: number) { this.expiresAtSec = expiresAtSec; }
  update(u?: { newHandle?: string; resumable?: boolean }) { if (u?.resumable !== false && u?.newHandle) this.handle = u.newHandle; }
  /** token still usable for a new connection (1 s margin) */
  tokenValid(nowMs = Date.now()) { return !this.expiresAtSec || nowMs < this.expiresAtSec * 1000 - 1000; }
  /** delay before the next reconnect attempt, or null = give up (no handle, token expired, retries exhausted) */
  next(nowMs = Date.now()): number | null {
    if (!this.handle || !this.tokenValid(nowMs) || this.attempts >= RETRY_DELAYS_MS.length) return null;
    return RETRY_DELAYS_MS[this.attempts++];
  }
  succeeded() { this.attempts = 0; this.resumes++; }
}

/** When to proactively reconnect after GoAway: right away if idle, else at the latest ~1 s before the deadline. */
export function goAwayDelayMs(timeLeftMs: number, busy: boolean) { return busy ? Math.max(0, timeLeftMs - 1000) : 0; }
