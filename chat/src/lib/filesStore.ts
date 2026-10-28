// GĐ8.6: is the "files in this chat" side panel open (one side panel at a time: opening it closes the canvas).
import { useSyncExternalStore } from "react";
import { canvasStore } from "./canvasStore";

let open = false;
const subs = new Set<() => void>();
const set = (v: boolean) => { if (open !== v) { open = v; subs.forEach((f) => f()); } };
export const filesStore = {
  get: () => open,
  subscribe: (f: () => void) => { subs.add(f); return () => { subs.delete(f); }; },
  open: () => { canvasStore.close(); set(true); },
  close: () => set(false),
  toggle: () => (open ? set(false) : filesStore.open()),
};
canvasStore.subscribe(() => { if (canvasStore.get().doc) set(false); });   // a canvas opening takes the side panel
export const useFilesOpen = () => useSyncExternalStore(filesStore.subscribe, filesStore.get);
