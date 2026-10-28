// F-1: which sub-agent (by its task description) is open in the right-side detail pane. One side panel at a time: opening it closes the canvas / files panel.
import { useSyncExternalStore } from "react";
import { canvasStore } from "./canvasStore";
import { filesStore } from "./filesStore";

let desc: string | null = null;
const subs = new Set<() => void>();
const set = (v: string | null) => { if (desc !== v) { desc = v; subs.forEach((f) => f()); } };
export const subagentPane = {
  get: () => desc,
  subscribe: (f: () => void) => { subs.add(f); return () => { subs.delete(f); }; },
  open: (d: string) => { canvasStore.close(); filesStore.close(); set(d); },
  close: () => set(null),
};
canvasStore.subscribe(() => { if (canvasStore.get().doc) set(null); });
filesStore.subscribe(() => { if (filesStore.get()) set(null); });
export const useSubagentPane = () => useSyncExternalStore(subagentPane.subscribe, subagentPane.get);
