// GĐ5b: which canvas document the side panel shows (one at a time, like the assistant-ui Artifacts example).
import { useSyncExternalStore } from "react";

export type CanvasState = { doc: string | null; v?: number; nonce: number };
let s: CanvasState = { doc: null, nonce: 0 };
const subs = new Set<() => void>();
const emit = () => subs.forEach((f) => f());
export const canvasStore = {
  get: () => s,
  subscribe: (f: () => void) => { subs.add(f); return () => { subs.delete(f); }; },
  open: (doc: string, v?: number) => { s = { doc, v, nonce: s.nonce + 1 }; emit(); },
  close: () => { if (s.doc) { s = { doc: null, nonce: s.nonce + 1 }; emit(); } },
};
export const useCanvas = () => useSyncExternalStore(canvasStore.subscribe, canvasStore.get);
