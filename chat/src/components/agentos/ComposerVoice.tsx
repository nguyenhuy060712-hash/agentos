// Composer voice controls (ChatGPT/Gemini pattern, Oct 2026):
//  • Mic = dictation: speech -> editable text in the composer, never auto-sent; live waveform + timer, X discards, ✓ inserts.
//  • Voice mode button sits where Send is while the composer is empty; Send replaces it as soon as there is text.
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useTranslation } from "react-i18next";
import { ComposerPrimitive, useAui, useVoiceControls } from "@assistant-ui/react";
import { AudioLinesIcon, CheckIcon, MicIcon, XIcon } from "lucide-react";
import { TooltipIconButton } from "@/components/assistant-ui/elements/tooltip-icon-button";
import { dictationStore } from "@/voice/dictation";
import { useChatUi } from "./ui-context";

export function DictateButton() {
  const { t } = useTranslation(); const ui = useChatUi();
  return (
    <ComposerPrimitive.Dictate render={<TooltipIconButton tooltip={t("dictate")} side="top" type="button" variant="ghost" size="icon" data-testid="dictate-start"
      disabled={!ui?.voiceReady} className="text-muted-foreground hover:text-foreground size-9 rounded-full" aria-label={t("dictate")} />}>
      <MicIcon className="size-[18px]" />
    </ComposerPrimitive.Dictate>
  );
}

export function VoiceModeButton() {
  const { t } = useTranslation(); const ui = useChatUi(); const c = useVoiceControls();
  return (
    <TooltipIconButton tooltip={ui?.voiceReady ? t("voiceMode") : t("voiceDenied")} side="top" type="button" variant="default" size="icon"
      data-testid="voice-start" aria-label={t("voiceMode")} disabled={!ui?.voiceReady} onClick={() => c.connect()}
      className="size-9 rounded-full">
      <AudioLinesIcon className="size-[18px]" />
    </TooltipIconButton>
  );
}

const fmt = (ms: number) => { const s = Math.floor(ms / 1000); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };

/** Replaces the composer action row while dictating. */
export function DictationBar() {
  const { t } = useTranslation(); const ui = useChatUi(); const aui = useAui();
  const st = useSyncExternalStore(dictationStore.subscribe, dictationStore.get);
  const base = useRef<string>(aui.composer().getState().text);
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const i = setInterval(() => setNow(Date.now()), 250); return () => clearInterval(i); }, []);
  const cancel = () => { ui?.cancelDictation(); const b = base.current; setTimeout(() => aui.composer().setText(b), 0); };
  return (
    <div className="flex items-center gap-2" data-testid="dictation-bar" role="group" aria-label={t("dictListening")}
      onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); cancel(); } }}>
      <TooltipIconButton tooltip={t("dictCancel")} side="top" type="button" variant="ghost" size="icon" data-testid="dictate-cancel"
        aria-label={t("dictCancel")} className="size-9 rounded-full" onClick={cancel}><XIcon className="size-[18px]" /></TooltipIconButton>
      <div className="flex h-9 min-w-0 flex-1 items-center gap-[3px] overflow-hidden rounded-full bg-muted/60 px-3" aria-hidden>
        {st.levels.map((l, i) => (
          <span key={i} className="w-[3px] flex-1 rounded-full bg-foreground/70 transition-[height] duration-75 motion-reduce:transition-none"
            style={{ height: `${Math.max(3, Math.min(26, 3 + l * 30))}px` }} />
        ))}
      </div>
      <span className="text-muted-foreground w-10 text-end text-xs tabular-nums" data-testid="dictation-timer" aria-live="off">{fmt(st.active ? now - st.startedAt : 0)}</span>
      <span className="sr-only" role="status">{t("dictListening")}</span>
      <ComposerPrimitive.StopDictation render={<TooltipIconButton tooltip={t("dictConfirm")} side="top" type="button" variant="default" size="icon"
        data-testid="dictate-confirm" aria-label={t("dictConfirm")} className="size-9 rounded-full" />}><CheckIcon className="size-[18px]" /></ComposerPrimitive.StopDictation>
    </div>
  );
}
