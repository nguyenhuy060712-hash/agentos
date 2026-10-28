// Composer picker. New servers (/api/chat/config returns `modes`, >= 2): a ChatGPT/Gemini style mode button "Tiêu chuẩn v" that opens
// a menu of the modes (label + one-line hint, model id small); sent per run as configurable.chat_mode.
// Old servers: the GĐ3.5 model <select> (only models in the agent's allow-list; sent as configurable.model, re-checked by Agent OS).
import { useTranslation } from "react-i18next";
import { Menu } from "@base-ui/react/menu";
import { CheckIcon, ChevronDownIcon, CpuIcon } from "lucide-react";
import { useChatUi } from "@/components/agentos/ui-context";
import { modeHintKey, modeLabelKey } from "@/lib/chat-modes";

export function ModePicker() {
  const { t } = useTranslation();
  const ui = useChatUi();
  const modes = ui?.chatModes ?? [];
  if (!ui || modes.length < 2) return null;
  const label = (id: string) => { const k = modeLabelKey(id); return k ? t(k) : id; };
  const cur = ui.chatMode ?? modes[0].id;
  return (
    <Menu.Root>
      <Menu.Trigger data-testid="mode-picker" aria-label={`${t("modePicker")}: ${label(cur)}`} title={t("modePickerHint")}
        className="text-muted-foreground hover:bg-muted data-[popup-open]:bg-muted focus-visible:ring-ring/50 inline-flex h-8 min-w-0 items-center gap-1 rounded-full px-2.5 text-xs font-medium outline-none focus-visible:ring-2">
        <span className="truncate">{label(cur)}</span><ChevronDownIcon className="size-3 shrink-0" aria-hidden />
      </Menu.Trigger>
      <Menu.Portal><Menu.Positioner side="top" align="start" sideOffset={8} className="z-50">
        <Menu.Popup data-testid="mode-menu" className="bg-popover text-popover-foreground w-72 max-w-[calc(100vw-1.5rem)] rounded-xl border p-1.5 shadow-lg outline-none">
          {modes.map((m) => {
            const hk = modeHintKey(m.id);
            return (
              <Menu.Item key={m.id} data-testid={`mode-${m.id}`} aria-label={label(m.id)} onClick={() => ui.onChatMode?.(m.id)}
                className="data-[highlighted]:bg-accent flex w-full cursor-pointer items-start gap-2 rounded-lg px-2.5 py-2 text-start outline-none">
                <span className="min-w-0 flex-1">
                  <span className="block text-sm font-medium">{label(m.id)}</span>
                  {hk && <span className="text-muted-foreground block text-xs">{t(hk)}</span>}
                  <span className="text-muted-foreground/70 block truncate text-[11px]">{m.model}</span>
                </span>
                {m.id === cur && <CheckIcon className="mt-0.5 size-4 shrink-0" aria-hidden />}
              </Menu.Item>
            );
          })}
        </Menu.Popup>
      </Menu.Positioner></Menu.Portal>
    </Menu.Root>
  );
}

export function ModelPicker() {
  const { t } = useTranslation();
  const ui = useChatUi();
  if (ui && (ui.chatModes?.length ?? 0) >= 2) return <ModePicker />;
  if (!ui || ui.chatModels.length < 2) return null;
  return (
    <label className="text-muted-foreground hover:bg-muted relative inline-flex h-8 min-w-0 items-center gap-1 rounded-full px-2 text-xs" title={t("modelPickerHint")}>
      <CpuIcon className="size-3.5" aria-hidden />
      <span className="sr-only">{t("modelPicker")}</span>
      <select data-testid="model-picker" aria-label={t("modelPicker")} value={ui.chatModel ?? ""} onChange={(e) => ui.onChatModel(e.target.value)}
        className="min-w-0 max-w-[8.5rem] cursor-pointer appearance-none truncate bg-transparent pe-4 outline-none sm:max-w-none">
        {ui.chatModels.map((m) => (
          <option key={m.id} value={m.id}>{m.id}{m.input_per_1m != null ? ` · $${m.input_per_1m}/$${m.output_per_1m} /1M` : ""}</option>
        ))}
      </select>
      <ChevronDownIcon className="pointer-events-none absolute end-1.5 size-3" aria-hidden />
    </label>
  );
}
