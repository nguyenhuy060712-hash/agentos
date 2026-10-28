// GĐ5b read-only view of a shared chat (#/s/<token>). The server sends a sanitised transcript (text, render-only `present` UI,
// canvas notes; no other tool args/results, no files, redaction rules applied). Rendered with assistant-ui primitives on an
// ExternalStoreRuntime that has no composer and never runs anything. Lazy chunk.
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { AssistantRuntimeProvider, AuiConfig, MessagePrimitive, ThreadPrimitive, Tools, useAuiState, useExternalStoreRuntime, type ThreadMessageLike } from "@assistant-ui/react";
import { EyeIcon, LinkIcon, LoaderIcon } from "lucide-react";
import { MarkdownText } from "@/components/assistant-ui/elements/markdown-text";
import { makeToolkit } from "@/genui/toolkit";
import { openShare, type SharedThread } from "@/lib/agentos";
import { viDateTime } from "@/genui/gd5b-helpers";

type Msg = SharedThread["messages"][number];
const convert = (m: Msg, i: number): ThreadMessageLike => m.role === "user"
  ? { id: `s${i}`, role: "user", content: [{ type: "text", text: m.text }] }
  : { id: `s${i}`, role: "assistant", status: { type: "complete", reason: "stop" }, metadata: { custom: { delegated_from: m.delegated_from } },
      content: m.parts.map((p, j) => p.type === "present"
        ? { type: "tool-call" as const, toolCallId: `p${i}_${j}`, toolName: "present", args: p.args as never, result: "shown" }
        : { type: "text" as const, text: p.type === "note" ? `*${p.text}*` : p.text }) };

export function ShareView({ token }: { token: string }) {
  const { t } = useTranslation();
  const [d, setD] = useState<SharedThread | null>(null); const [err, setErr] = useState<{ status?: number; message: string } | null>(null);
  useEffect(() => { openShare(token).then(setD).catch((e) => setErr(e as { status?: number; message: string })); }, [token]);
  useEffect(() => { document.title = d ? `${d.title} · ${t("shareReadonlyTag")}` : "Agent OS"; }, [d, t]);
  if (err) return (
    <div className="bg-background text-foreground flex h-dvh flex-col items-center justify-center gap-3 p-6 text-center" data-testid="share-view" data-state="error">
      <LinkIcon className="text-muted-foreground size-8" />
      <p className="text-lg font-semibold">{err.status === 410 ? err.message : err.status === 404 ? t("shareInvalid") : t("shareLoadFailed")}</p>
      <a href="#/" className="text-primary text-sm underline">{t("shareBack")}</a>
    </div>);
  if (!d) return <div className="text-muted-foreground p-6 text-sm" role="status"><LoaderIcon className="me-1 inline size-4 animate-spin" />…</div>;
  return <Transcript d={d} />;
}

function Transcript({ d }: { d: SharedThread }) {
  const { t } = useTranslation();
  const runtime = useExternalStoreRuntime({ messages: d.messages, convertMessage: convert, isRunning: false, onNew: async () => {} });
  const cfg = useMemo(() => AuiConfig({ tools: Tools({ toolkit: makeToolkit({ genui: true }) }) }), []);
  return (
    <AssistantRuntimeProvider runtime={runtime} config={cfg}>
      <div className="bg-background text-foreground flex h-dvh flex-col" data-testid="share-view" data-state="ok">
        <header className="flex shrink-0 items-center gap-2 border-b px-4 py-2.5">
          <div className="min-w-0 flex-1"><h1 className="truncate text-[15px] font-semibold" data-testid="share-title">{d.title}</h1>
            <p className="text-muted-foreground truncate text-xs">{d.agent_name ? `${d.agent_name} · ` : ""}{t("shareExpires", { at: viDateTime(d.expires) })}</p></div>
          <span className="bg-muted text-muted-foreground flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-medium" data-testid="share-badge"><EyeIcon className="size-3.5" />{t("shareReadonlyTag")}</span>
        </header>
        <ThreadPrimitive.Root className="min-h-0 flex-1">
          <ThreadPrimitive.Viewport className="h-full overflow-y-auto">
            <div className="mx-auto flex w-full max-w-[768px] flex-col gap-y-6 px-4 py-6">
              <ThreadPrimitive.Messages>{() => <SharedMessage />}</ThreadPrimitive.Messages>
              {!d.messages.length && <p className="text-muted-foreground text-center text-sm">{t("shareEmpty")}</p>}
              <p className="text-muted-foreground border-t pt-3 text-center text-xs">{t("shareFooter")}</p>
            </div>
          </ThreadPrimitive.Viewport>
        </ThreadPrimitive.Root>
      </div>
    </AssistantRuntimeProvider>);
}

function SharedMessage() {
  const role = useAuiState((s) => s.message.role);
  const from = useAuiState((s) => (s.message.metadata?.custom as { delegated_from?: string } | undefined)?.delegated_from);
  const { t } = useTranslation();
  if (role === "user") return (
    <MessagePrimitive.Root className="flex justify-end" data-testid="share-user">
      <div className="bg-muted max-w-[85%] rounded-2xl px-4 py-2.5 whitespace-pre-wrap wrap-break-word"><MessagePrimitive.Parts /></div>
    </MessagePrimitive.Root>);
  return (
    <MessagePrimitive.Root className="px-2 leading-relaxed wrap-break-word" data-testid="share-assistant">
      {from && <div className="text-muted-foreground mb-1 text-xs">{t("delegatedFrom", { name: from })}</div>}
      <MessagePrimitive.Parts components={{ Text: MarkdownText }} />
    </MessagePrimitive.Root>);
}
