// Shared Agent OS chat-surface state for components rendered inside the assistant-ui Thread (composer, welcome).
import { createContext, useContext } from "react";
import type { Assistant } from "@/lib/agentos";
import i18n from "@/i18n";
import type { ChatModeInfo } from "@/lib/chat-modes";

export type AgentProfile = { graph_id: string; assistant_id: string; name: string; role: string; group: string; greeting: string; suggestions: string[]; color: string; model?: string;
  /** GĐ3 (b): assistant.metadata.chat_visible === false hides the agent from the chat sidebar (data only; nothing is deleted) */
  visible: boolean;
  /** GĐ3.6 follow-up chips after an answer (metadata.followups, else the opening suggestions); static, $0 */
  followups: string[] };
export type ChatModel = { id: string; provider: string; input_per_1m?: number | null; output_per_1m?: number | null };
export type ChatUi = {
  agent: AgentProfile;
  /** GĐ3.5 composer model picker (allow-list from /api/chat/config; the server re-checks: 403 outside the list) */
  chatModels: ChatModel[]; chatModel: string | null; onChatModel: (m: string) => void;
  /** chat modes (Nhanh / Tiêu chuẩn / Suy nghĩ sâu): when >= 2 are offered they replace the model picker */
  chatModes?: ChatModeInfo[]; chatMode?: string | null; onChatMode?: (m: string) => void;
  /** Voice mode is usable: role holds voice.use and a live model is selected (Settings). */
  voiceReady: boolean; voiceModel: string | null;
  /** composer Mic: X discards the dictated text */
  cancelDictation: () => void;
  /** GĐ5a: agents offered by "@" in the composer, and "/moi" */
  agents: AgentProfile[]; newChat: () => void;
  /** GĐ7.1 assistant side panel: no voice / dictation buttons, Send is always shown */
  compact?: boolean;
};
export const ChatUiContext = createContext<ChatUi | null>(null);
export const useChatUi = () => useContext(ChatUiContext);

const PALETTE = ["#2563eb", "#7c3aed", "#db2777", "#ea580c", "#059669", "#0891b2", "#4f46e5", "#ca8a04"];
const hash = (s: string) => [...s].reduce((h, c) => (h * 31 + c.charCodeAt(0)) >>> 0, 7);

/** Agent profile = Agent OS assistant row; role/group/greeting/suggestions live in assistant.metadata (data, editable), with fallbacks. */
export function profileOf(a: Assistant, t: (k: string, o?: Record<string, unknown>) => string): AgentProfile {
  const raw = (a.metadata ?? {}) as Record<string, unknown>;
  // localized fields: role_en / greeting_en / suggestions_en override when the UI is in English (Vietnamese is the default)
  const L = <T,>(k: string) => ((i18n.language === "en" && raw[`${k}_en`] != null ? raw[`${k}_en`] : raw[k]) as T | undefined);
  const md = { role: L<string>("role"), group: L<string>("group"), greeting: L<string>("greeting"), suggestions: L<string[]>("suggestions"), followups: L<string[]>("followups"), color: raw.color as string | undefined };
  const name = a.name.replace(/\s*\((fake|mock)\)\s*$/i, "");
  return {
    graph_id: a.graph_id, assistant_id: a.assistant_id, name, visible: raw.chat_visible !== false,
    followups: (md.followups ?? md.suggestions ?? []).filter((x) => typeof x === "string").slice(0, 3), role: md.role || a.description || t("defaultRole"), group: md.group || t("defaultGroup"),
    greeting: md.greeting || t("greetingFallback", { name }), suggestions: (md.suggestions ?? []).slice(0, 4),
    color: md.color || PALETTE[hash(a.graph_id) % PALETTE.length],
    model: (a.config as { configurable?: { model?: string } } | undefined)?.configurable?.model,
  };
}

export function AgentAvatar({ agent, size = 28 }: { agent: Pick<AgentProfile, "name" | "color">; size?: number }) {
  const ini = agent.name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase();
  return (
    <span aria-hidden className="inline-flex shrink-0 items-center justify-center rounded-full font-semibold text-white select-none"
      style={{ width: size, height: size, background: agent.color, fontSize: Math.round(size * 0.38) }}>{ini}</span>
  );
}
