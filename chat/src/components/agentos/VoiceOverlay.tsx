// Full-screen Voice mode (ChatGPT Voice / Gemini Live pattern, Oct 2026): animated orb reacting to the audio level, listening /
// thinking / speaking states, live captions toggle, mute, red end control, model indicator, delegation + HITL cards stay visible.
// After the call the transcript is in the thread (persisted server-side). Lazy chunk.
import { useEffect, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Dialog } from "@base-ui/react/dialog";
import { useVoiceControls, useVoiceState, useVoiceVolume } from "@assistant-ui/react";
import { CaptionsIcon, CaptionsOffIcon, CheckCircle2Icon, LoaderIcon, MicIcon, MicOffIcon, ShieldAlertIcon, XIcon } from "lucide-react";
import { useVoiceTranscript } from "@/components/assistant-ui/elements/voice-conversation.aui";
import { cn } from "@/lib/utils";
import { AgentAvatar, type AgentProfile } from "./ui-context";

export type Delegation = { callId: string; request: string; status: "running" | "approval" | "success" | "error" | "cancelled"; reply?: string | null };
type Mode = "connecting" | "listening" | "thinking" | "speaking";

export function VoiceOverlay(p: { agent: AgentProfile; model: string | null; mode: Mode | null; link?: "ok" | "reconnecting" | "offline" | "resumed"; delegations: Delegation[]; dev: boolean; mock: boolean;
  approval: ReactNode; onInterrupt: () => void; say: (t: string) => void }) {
  const { t } = useTranslation();
  const vs = useVoiceState(); const c = useVoiceControls(); const amp = useVoiceVolume(); const transcript = useVoiceTranscript();
  const st = vs?.status?.type ?? "idle"; const live = st === "running" || st === "starting";
  const muted = !!vs?.isMuted;
  const mode: Mode = st === "starting" ? "connecting" : (p.mode ?? ((vs as { mode?: Mode } | undefined)?.mode ?? "listening"));
  const [captions, setCaptions] = useState(() => localStorage.getItem("aos_captions") !== "0");
  useEffect(() => localStorage.setItem("aos_captions", captions ? "1" : "0"), [captions]);
  const [text, setText] = useState("");
  const label = muted ? t("vMuted") : ({ connecting: t("vConnecting"), listening: t("vListening"), thinking: t("vThinking"), speaking: t("vSpeaking") } as const)[mode];
  const hint = ({ connecting: "", listening: t("vHintListening"), thinking: t("vHintThinking"), speaking: t("vHintSpeaking") } as const)[mode];
  const scale = 1 + Math.min(1, amp) * (mode === "speaking" ? 0.32 : 0.18);
  const lastTurns = transcript.slice(-2);
  const shown = p.delegations.slice(-2);
  return (
    <Dialog.Root open={live} onOpenChange={(o) => { if (!o) c.disconnect(); }}>
      <Dialog.Portal>
        <Dialog.Popup data-testid="voice-overlay" data-mode={mode} aria-label={t("voiceWith", { name: p.agent.name })}
          className="bg-background text-foreground fixed inset-0 z-50 flex flex-col outline-none data-[open]:animate-in data-[open]:fade-in-0"
          onKeyDown={(e) => { if (e.target instanceof HTMLInputElement) return;
            if (e.key === "m" || e.key === "M") { e.preventDefault(); muted ? c.unmute() : c.mute(); }
            if (e.key === "c" || e.key === "C") { e.preventDefault(); setCaptions((x) => !x); } }}>
          <Dialog.Title className="sr-only">{t("voiceWith", { name: p.agent.name })}</Dialog.Title>
          <header className="flex items-center gap-2.5 px-4 pt-4 sm:px-6">
            <AgentAvatar agent={p.agent} size={30} />
            <div className="min-w-0"><div className="truncate text-sm font-semibold">{p.agent.name}</div>
              <div className="text-muted-foreground truncate text-xs">{p.agent.role}</div></div>
            {p.model && <span data-testid="voice-model-indicator" className="bg-muted text-muted-foreground ms-auto rounded-full px-2.5 py-1 font-mono text-[11px]">{p.model}</span>}
          </header>

          <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-6 px-4">
            <button type="button" data-testid="voice-orb" data-mode={mode} onClick={p.onInterrupt} aria-label={hint || label}
              className="focus-visible:ring-ring/60 relative size-44 rounded-full outline-none focus-visible:ring-4 sm:size-56">
              <span aria-hidden className={cn("absolute inset-0 rounded-full blur-2xl transition-opacity duration-500 motion-reduce:transition-none",
                mode === "speaking" ? "opacity-70" : "opacity-40")} style={{ background: `radial-gradient(circle, ${p.agent.color}aa, transparent 70%)` }} />
              <span aria-hidden className={cn("absolute inset-3 rounded-full transition-transform duration-100 ease-out motion-reduce:transition-none",
                  mode === "thinking" && "animate-pulse", mode === "connecting" && "animate-pulse opacity-60")}
                style={{ transform: `scale(${scale})`, background: `radial-gradient(circle at 35% 30%, #ffffff 0%, ${p.agent.color} 45%, #0f172a 120%)`,
                  boxShadow: `0 0 ${20 + amp * 60}px ${p.agent.color}66` }} />
            </button>
            <div className="text-center" role="status" aria-live="polite">
              <div className="text-lg font-medium" data-testid="voice-state-label">{label}</div>
              {hint && !muted && <div className="text-muted-foreground mt-1 text-sm">{hint}</div>}
              {p.link && p.link !== "ok" && (
                <div data-testid="voice-link" data-link={p.link}
                  className={cn("mx-auto mt-3 inline-flex items-center gap-2 rounded-full px-3 py-1 text-xs",
                    p.link === "resumed" ? "bg-emerald-100 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-200" : "bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200")}>
                  {p.link !== "resumed" && <LoaderIcon aria-hidden className="size-3 animate-spin" />}
                  {p.link === "offline" ? t("vOffline") : p.link === "reconnecting" ? t("vReconnecting") : t("vResumed")}
                </div>
              )}
            </div>

            {shown.length > 0 && (
              <ul className="w-full max-w-md space-y-2" aria-label={t("agent")}>
                {shown.map((d) => (
                  <li key={d.callId} data-testid="voice-delegation" data-status={d.status}
                    className="bg-card flex items-start gap-2.5 rounded-xl border px-3 py-2 text-sm shadow-sm">
                    {d.status === "running" ? <LoaderIcon aria-hidden className="text-muted-foreground mt-0.5 size-4 shrink-0 animate-spin" />
                      : d.status === "approval" ? <ShieldAlertIcon aria-hidden className="mt-0.5 size-4 shrink-0 text-amber-600" />
                      : <CheckCircle2Icon aria-hidden className={cn("mt-0.5 size-4 shrink-0", d.status === "success" ? "text-emerald-600" : "text-muted-foreground")} />}
                    <span className="min-w-0"><span className="block font-medium">{p.agent.name}</span>
                      <span className="text-muted-foreground line-clamp-2">{d.reply || d.request}</span></span>
                  </li>
                ))}
              </ul>
            )}
            {p.approval && <div className="w-full max-w-xl" data-testid="voice-approval-slot">{p.approval}</div>}
          </div>

          {captions && lastTurns.length > 0 && (
            <div data-testid="voice-captions" aria-live="polite" className="mx-auto mb-2 w-full max-w-2xl space-y-1 px-6 text-center">
              {lastTurns.map((x) => (
                <p key={x.id} className={cn("text-balance", x.role === "user" ? "text-muted-foreground text-sm" : "text-base")}>
                  <span className="sr-only">{x.role === "user" ? t("youSaid") : t("assistantSaid")}: </span>{x.text}</p>
              ))}
            </div>
          )}

          {p.dev && p.mock && (
            <form className="mx-auto mb-2 flex w-full max-w-md gap-2 px-6" onSubmit={(e) => { e.preventDefault(); if (text.trim()) { p.say(text.trim()); setText(""); } }}>
              <input data-testid="mock-say" className="bg-background min-w-0 flex-1 rounded-full border px-3 py-1.5 text-sm" placeholder={t("mockSay")} value={text} onChange={(e) => setText(e.target.value)} />
              <button type="submit" data-testid="mock-say-send" className="bg-muted rounded-full px-3 text-sm">{t("speak")}</button>
            </form>
          )}

          <footer className="flex items-center justify-center gap-5 px-4 pt-2 pb-8 sm:pb-10">
            <CtlButton testid="voice-captions-toggle" pressed={captions} label={captions ? t("captionsOff") : t("captionsOn")} onClick={() => setCaptions((x) => !x)} keys="C">
              {captions ? <CaptionsIcon className="size-6" /> : <CaptionsOffIcon className="size-6" />}
            </CtlButton>
            <CtlButton testid="voice-mute" pressed={muted} label={muted ? t("unmute") : t("mute")} onClick={() => (muted ? c.unmute() : c.mute())} keys="M">
              {muted ? <MicOffIcon className="size-6" /> : <MicIcon className="size-6" />}
            </CtlButton>
            <Dialog.Close data-testid="voice-stop" aria-label={t("endCall")} title={t("endCall")}
              className="focus-visible:ring-ring/60 flex size-16 items-center justify-center rounded-full bg-red-600 text-white shadow-md outline-none hover:bg-red-700 focus-visible:ring-4">
              <XIcon className="size-7" />
            </Dialog.Close>
          </footer>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function CtlButton({ children, pressed, label, onClick, testid, keys }: { children: ReactNode; pressed: boolean; label: string; onClick: () => void; testid: string; keys: string }) {
  return (
    <button type="button" data-testid={testid} aria-pressed={pressed} aria-label={label} title={label} aria-keyshortcuts={keys} onClick={onClick}
      className={cn("focus-visible:ring-ring/60 flex size-16 items-center justify-center rounded-full outline-none transition-colors focus-visible:ring-4",
        pressed ? "bg-foreground text-background" : "bg-muted hover:bg-muted/70")}>
      {children}
    </button>
  );
}
