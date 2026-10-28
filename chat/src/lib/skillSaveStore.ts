// GĐ12.87: "Save this way of working as a Skill" dialog is open for this message (a store, because the message action bar unmounts when the pointer leaves it).
import { useSyncExternalStore } from "react";

export type SkillSaveReq = { runId: string | null } | null;
let req: SkillSaveReq = null;
const subs = new Set<() => void>();
const set = (v: SkillSaveReq) => { req = v; subs.forEach((f) => f()); };
export const skillSaveStore = {
  get: () => req,
  subscribe: (f: () => void) => { subs.add(f); return () => { subs.delete(f); }; },
  open: (runId: string | null = null) => set({ runId }),
  close: () => set(null),
};
export const useSkillSaveReq = () => useSyncExternalStore(skillSaveStore.subscribe, skillSaveStore.get);
