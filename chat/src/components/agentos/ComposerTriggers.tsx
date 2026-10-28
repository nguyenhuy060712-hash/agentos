// GĐ5a composer triggers (assistant-ui 0.15.30 trigger popovers, experimental `unstable_` hooks; docs fetched 20/10/2026):
//  "@" lists the chat agents; picking one inserts "@Tên agent". On send the runtime routes an empty chat to that agent, or
//      delegates the message to it inside the current chat (POST /api/chat/delegate) and shows its answer here.
//  "/" runs a client command: /moi (new chat), /tom-tat (summary), /bieu-do (chart prompt), /voice (voice mode).
import { useMemo, type FC } from "react";
import { useTranslation } from "react-i18next";
import { unstable_useMentionAdapter, unstable_useSlashCommandAdapter, useAui, useVoiceControls, type Unstable_SlashCommand } from "@assistant-ui/react";
import { AudioLinesIcon, BarChart3Icon, BotIcon, FileTextIcon, SquarePenIcon } from "lucide-react";
import { ComposerTriggerPopover } from "@/components/assistant-ui/elements/composer-trigger-popover.aui";
import { agentFormatter } from "@/runtime/mentions";
import { useChatUi } from "./ui-context";

const ICONS = { bot: BotIcon, new: SquarePenIcon, summary: FileTextIcon, chart: BarChart3Icon, voice: AudioLinesIcon };
const POP = "w-72 rounded-xl shadow-lg max-h-72 overflow-y-auto";

export const MentionPopover: FC = () => {
  const { t } = useTranslation(); const ui = useChatUi();
  const items = useMemo(() => (ui?.agents ?? [])
    .map((a) => ({ id: a.graph_id, type: "agent", label: a.name, description: a.graph_id === ui?.agent.graph_id ? t("mentionCurrent") : a.role, icon: "bot" })), [ui?.agents, ui?.agent.graph_id, t]);
  const mention = unstable_useMentionAdapter({ items, formatter: agentFormatter, iconMap: ICONS });
  return <ComposerTriggerPopover char="@" adapter={mention.adapter} directive={mention.directive} iconMap={ICONS} fallbackIcon={BotIcon}
    className={POP} data-testid="mention-popover" backLabel={t("back")} emptyItemsLabel={t("mentionEmpty")} emptyCategoriesLabel={t("mentionEmpty")} />;
};

export const SlashPopover: FC = () => {
  const { t } = useTranslation(); const ui = useChatUi(); const aui = useAui(); const voice = useVoiceControls();
  // execute runs before the "/command" text is stripped (removeOnExecute): defer composer writes to the next tick
  const later = (f: () => void) => setTimeout(f, 0);
  const commands = useMemo<Unstable_SlashCommand[]>(() => {
    const c: Unstable_SlashCommand[] = [
      { id: "moi", label: "/moi", description: t("slashNew"), icon: "new", execute: () => later(() => ui?.newChat()) },
      { id: "tom-tat", label: "/tom-tat", description: t("slashSummary"), icon: "summary",
        execute: () => later(() => { const c = aui.composer(); if (aui.thread().getState().isRunning) return; c.setText(t("slashSummaryPrompt")); c.send(); }) },
      { id: "bieu-do", label: "/bieu-do", description: t("slashChart"), icon: "chart", execute: () => later(() => aui.composer().setText(t("slashChartPrompt"))) },
    ];
    if (ui?.voiceReady) c.push({ id: "voice", label: "/voice", description: t("slashVoice"), icon: "voice", execute: () => later(() => { void voice.connect(); }) });
    return c;
  }, [t, ui, aui, voice]);
  const slash = unstable_useSlashCommandAdapter({ commands, removeOnExecute: true, iconMap: ICONS });
  return <ComposerTriggerPopover char="/" adapter={slash.adapter} action={slash.action} iconMap={ICONS} fallbackIcon={FileTextIcon}
    className={POP} data-testid="slash-popover" backLabel={t("back")} emptyItemsLabel={t("slashEmpty")} emptyCategoriesLabel={t("slashEmpty")} />;
};
