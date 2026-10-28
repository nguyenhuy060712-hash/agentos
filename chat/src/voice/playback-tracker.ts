/**
 * Interrupted-turn transcript truncation (fix for "the saved transcript contains words the user never heard").
 * Providers stream the model's outputTranscription faster than the audio is played, and keep the whole generated text after a
 * barge-in. The tracker records, per assistant turn:
 *   - every audio chunk scheduled on the playback queue: [startSec, durationSec] on the playback clock;
 *   - every transcription chunk with the amount of audio already RECEIVED when it arrived (its audio position).
 * On barge-in at clock time `now`, playedSec = Σ clamp(now − start, 0, duration) over the queue. The text is cut by mapping
 * playedSec onto the transcription chunks by timing (see cutChars), with a proportional character split between timing points;
 * when timing carries no information (all text before any audio), the split is proportional over the whole turn. The cut snaps back to a word boundary.
 */
export type Truncation = { text: string; truncated: true; playedMs: number; generatedMs: number; fullChars: number };

export class PlaybackTracker {
  private segs: { start: number; dur: number }[] = [];
  private chunks: { text: string; audioPos: number }[] = [];
  private received = 0;
  /** an audio chunk of `dur` seconds was scheduled to start at `start` (playback clock seconds) */
  audio(start: number, dur: number) { this.segs.push({ start, dur }); this.received += dur; }
  /** a transcription chunk arrived (delta text, not cumulative) */
  text(delta: string) { if (delta) this.chunks.push({ text: delta, audioPos: this.received }); }
  get fullText() { return this.chunks.map((c) => c.text).join(""); }
  get generatedSec() { return this.received; }
  playedSec(now: number) { return this.segs.reduce((a, s) => a + Math.max(0, Math.min(s.dur, now - s.start)), 0); }
  reset() { this.segs = []; this.chunks = []; this.received = 0; }

  /** Cut the turn's transcript to what was actually played by `now`; null when nothing was cut (everything was heard). */
  cut(now: number): Truncation | null {
    const full = this.fullText; const gen = this.received; const played = this.playedSec(now);
    if (!full.trim()) return null;
    if (gen > 0 && played >= gen - 1e-3) return null;
    const chars = cutChars(this.chunks, played, gen);
    let t = full.slice(0, chars);
    if (chars < full.length && /\S/.test(full[chars] ?? "")) { const sp = t.search(/\s\S*$/); t = sp >= 0 ? t.slice(0, sp) : ""; }   // no half words
    return { text: t.trim(), truncated: true, playedMs: Math.round(played * 1000), generatedMs: Math.round(gen * 1000), fullChars: full.length };
  }
}

/** pure mapping (unit-tested): number of characters heard after `played` s of `gen` s of generated audio.
 *  Timing points: the text up to the end of chunk j was transcribed from the audio received by its arrival (audioPos_j).
 *  Points without information (text that arrived before any audio, or out of order) are dropped; between the remaining points,
 *  and from the last point to (all text, all audio), characters are split proportionally. No timing at all => fully proportional. */
export function cutChars(chunks: { text: string; audioPos: number }[], played: number, gen: number): number {
  const total = chunks.reduce((a, c) => a + c.text.length, 0);
  if (gen <= 0 || played <= 0) return 0;
  if (played >= gen) return total;
  const pts: [number, number][] = [[0, 0]]; let cum = 0;
  for (const c of chunks) {
    cum += c.text.length; const last = pts[pts.length - 1];
    if (c.audioPos > last[1] && c.audioPos < gen && cum > last[0] && cum < total) pts.push([cum, c.audioPos]);
  }
  pts.push([total, gen]);
  for (let k = 1; k < pts.length; k++) {
    const [c0, a0] = pts[k - 1]; const [c1, a1] = pts[k];
    if (played <= a1) return Math.floor(c0 + ((played - a0) / (a1 - a0)) * (c1 - c0));
  }
  return total;
}
