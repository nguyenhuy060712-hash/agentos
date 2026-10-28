// Chat modes (Nhanh / Tiêu chuẩn / Suy nghĩ sâu): the contract is docs/CHE_DO_CHAT_PLAN.md in agentos-local.
// /api/chat/config returns `modes` + `default_mode`; the pick is remembered per agent and sent per run as configurable.chat_mode.
export type ChatModeInfo = { id: string; model: string; thinking?: string | null; timeout_s?: number; input_per_1m?: number | null; output_per_1m?: number | null };
export type ModeCfg = { modes?: ChatModeInfo[]; default_mode?: string | null };

export const MODE_KEY = "aos_chat_mode";
const KNOWN = ["fast", "standard", "deep"] as const;
/** i18n key of a mode label / hint (unknown ids fall back to the raw id in the UI) */
export const modeLabelKey = (id: string) => (KNOWN as readonly string[]).includes(id) ? `mode_${id}` : null;
export const modeHintKey = (id: string) => (KNOWN as readonly string[]).includes(id) ? `mode_${id}_hint` : null;

/** the mode picker shows only when the server offers at least 2 modes (old servers: model picker) */
export const modesOf = (cfg: ModeCfg | null | undefined): ChatModeInfo[] => (cfg?.modes && cfg.modes.length >= 2 ? cfg.modes : []);

type Store = Pick<Storage, "getItem" | "setItem">;
const store = (): Store | null => { try { return typeof localStorage === "undefined" ? null : localStorage; } catch { return null; } };

export function readPicks(s: Store | null = store()): Record<string, string> {
  try { const v = JSON.parse(s?.getItem(MODE_KEY) ?? "{}"); return v && typeof v === "object" ? v : {}; } catch { return {}; }
}
export function writePick(agent: string, mode: string, s: Store | null = store()): Record<string, string> {
  const n = { ...readPicks(s), [agent]: mode };
  try { s?.setItem(MODE_KEY, JSON.stringify(n)); } catch { /* private mode: the pick just isn't remembered */ }
  return n;
}

/** current mode: the remembered pick when it is still offered, else default_mode, else the first offered */
export function resolveMode(cfg: ModeCfg | null | undefined, picks: Record<string, string>, agent: string): string | null {
  const modes = modesOf(cfg);
  if (!modes.length) return null;
  const has = (id?: string | null) => !!id && modes.some((m) => m.id === id);
  return has(picks[agent]) ? picks[agent] : has(cfg?.default_mode) ? (cfg!.default_mode as string) : modes[0].id;
}
