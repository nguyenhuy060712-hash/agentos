// Composer "+" menu (ChatGPT/Gemini style): "Upload files" and the Deep Research toggle. When research is on a removable chip
// "Deep Research x" shows next to it. Research behaviour itself (store, run config) is unchanged: see src/lib/research.ts.
import { useRef } from "react";
import { useTranslation } from "react-i18next";
import { ComposerPrimitive, useAuiState } from "@assistant-ui/react";
import { Menu } from "@base-ui/react/menu";
import { CheckIcon, PaperclipIcon, PlusIcon, TelescopeIcon, XIcon } from "lucide-react";
import { researchStore, useResearch } from "@/lib/research";

const ITEM = "data-[highlighted]:bg-accent flex w-full cursor-pointer items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-sm outline-none";

export function ComposerPlusMenu() {
  const { t } = useTranslation();
  const key = useAuiState((s) => s.threadListItem?.externalId);
  const on = useResearch(key);
  const upload = useRef<HTMLSpanElement>(null);
  return (
    <>
      <Menu.Root>
        <Menu.Trigger data-testid="composer-plus" aria-label={t("composerPlus")} title={t("composerPlus")}
          className="aui-composer-add-attachment text-muted-foreground hover:text-foreground hover:bg-muted-foreground/15 data-[popup-open]:bg-muted focus-visible:ring-ring/50 flex size-7 shrink-0 items-center justify-center rounded-full outline-none focus-visible:ring-2">
          <PlusIcon className="size-4" aria-hidden />
        </Menu.Trigger>
        <Menu.Portal><Menu.Positioner side="top" align="start" sideOffset={8} className="z-50">
          <Menu.Popup className="bg-popover text-popover-foreground w-64 max-w-[calc(100vw-1.5rem)] rounded-xl border p-1.5 shadow-lg outline-none">
            <Menu.Item data-testid="upload-files" onClick={() => upload.current?.querySelector("button")?.click()} className={ITEM}>
              <PaperclipIcon className="size-4" aria-hidden />{t("uploadFiles")}</Menu.Item>
            <Menu.CheckboxItem data-testid="research-toggle" checked={on} onCheckedChange={(v) => researchStore.set(key, v)} aria-label={t("deepResearch")} className={ITEM}>
              <TelescopeIcon className="size-4" aria-hidden /><span className="min-w-0 flex-1">{t("deepResearch")}</span>
              {on && <CheckIcon className="size-4" aria-hidden />}</Menu.CheckboxItem>
          </Menu.Popup>
        </Menu.Positioner></Menu.Portal>
      </Menu.Root>
      {/* the real attachment picker (assistant-ui's own file input), triggered by the "Upload files" item */}
      <span ref={upload} hidden><ComposerPrimitive.AddAttachment render={<button type="button" tabIndex={-1} aria-label={t("attach")} />} /></span>
      {on && <ResearchChip onRemove={() => researchStore.set(key, false)} />}
    </>
  );
}

export function ResearchChip({ onRemove }: { onRemove: () => void }) {
  const { t } = useTranslation();
  return (
    <span data-testid="research-chip" className="bg-primary/10 text-primary inline-flex h-7 min-w-0 items-center gap-1 rounded-full ps-2.5 pe-1 text-xs font-medium">
      <TelescopeIcon className="size-3.5 shrink-0" aria-hidden /><span className="truncate">{t("deepResearch")}</span>
      <button type="button" data-testid="research-chip-remove" aria-label={t("removeDeepResearch")} onClick={onRemove}
        className="hover:bg-primary/15 focus-visible:ring-ring/50 flex size-5 items-center justify-center rounded-full outline-none focus-visible:ring-2"><XIcon className="size-3" aria-hidden /></button>
    </span>
  );
}
