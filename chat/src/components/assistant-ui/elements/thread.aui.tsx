"use client";
import { useTranslation } from "react-i18next";
import i18n from "@/i18n";

import {
  ComposerAddAttachment,
  ComposerAttachments,
  UserMessageAttachments,
} from "@/components/assistant-ui/elements/attachment.aui";
import { File, getFileHref } from "@/components/assistant-ui/elements/file";
import { ThreadFollowupSuggestions } from "@/components/assistant-ui/elements/follow-up-suggestions.aui";
import { Image } from "@/components/assistant-ui/elements/image";
import { MarkdownText } from "@/components/assistant-ui/elements/markdown-text";
import {
  Reasoning,
  ReasoningContent,
  ReasoningRoot,
  ReasoningText,
  ReasoningTrigger,
} from "@/components/assistant-ui/elements/reasoning.aui";
import { ToolFallback } from "@/components/assistant-ui/elements/tool-fallback.aui";
import { ComposerPlusMenu } from "@/components/agentos/ComposerPlus";
import { ModelPicker } from "@/components/agentos/ModelPicker";
import {
  ToolGroupContent,
  ToolGroupRoot,
  ToolGroupTrigger,
} from "@/components/assistant-ui/elements/tool-group.aui";
import { TooltipIconButton } from "@/components/assistant-ui/elements/tooltip-icon-button";
import { DictateButton, DictationBar, VoiceModeButton } from "@/components/agentos/ComposerVoice";
import { useChatUi } from "@/components/agentos/ui-context";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import {
  ActionBarMorePrimitive,
  ActionBarPrimitive,
  AuiIf,
  type AssistantState,
  BranchPickerPrimitive,
  ComposerPrimitive,
  ErrorPrimitive,
  groupPartByType,
  MessagePrimitive,
  ThreadPrimitive,
  type FileMessagePartComponent,
  type ImageMessagePartComponent,
  type TextMessagePartComponent,
  type ToolCallMessagePartComponent,
  useAui,
  useAuiState,
} from "@assistant-ui/react";
import {
  ArrowDownIcon,
  ArrowUpIcon,
  AudioLinesIcon,
  CheckIcon,
  ChevronLeftIcon,
  ChevronRightIcon,
  CopyIcon,
  DownloadIcon,
  MicIcon,
  MoreHorizontalIcon,
  PencilIcon,
  PhoneIcon,
  RefreshCwIcon,
  SquareIcon,
  Volume2Icon,
  VolumeXIcon,
  PlayIcon,
  ThumbsDownIcon,
  ThumbsUpIcon,
} from "lucide-react";
import {
  createContext,
  useContext,
  useLayoutEffect,
  useRef,
  type ComponentType,
  type FC,
  type PropsWithChildren,
  useSyncExternalStore,
} from "react";
import { stoppedMessages } from "@/runtime/useAgentOSRuntime";
import { answerText, stepsMarkdown, type CopyPart } from "@/runtime/turn-queue";
import { createDirectiveText } from "@/components/assistant-ui/elements/directive-text.aui";
import { agentFormatter } from "@/runtime/mentions";
import { MentionPopover, SlashPopover } from "@/components/agentos/ComposerTriggers";
import { enterHandler, QueueList, QueueRunner, TurnStatus, useTurnControl } from "@/components/agentos/TurnComposer";
import { BotIcon, CornerDownRightIcon, SparklesIcon } from "lucide-react";
import { RO } from "@/lib/agentos";
import { skillSaveStore } from "@/lib/skillSaveStore";

export type ThreadGroupPart = MessagePrimitive.GroupedParts.GroupPart;

const shouldShowComposerResume = (s: AssistantState) =>
  !!s.thread.canResume && s.composer.type === "thread" && s.composer.isEmpty;

const reasoningDuration = (timing: ThreadGroupPart["timing"]) =>
  timing?.completedAt === undefined
    ? undefined
    : Math.round((timing.completedAt - timing.startedAt) / 1000);

/**
 * Optional component overrides for the thread. `AssistantMessage` and
 * `Welcome` replace whole sections; the remaining slots override how the
 * assistant message renders tool calls and part groups. Tool UIs registered
 * by name (toolkit `render`, `useAssistantDataUI`) take precedence over
 * `ToolFallback`. When `TaskGroup` is set, tool calls that carry a nested
 * conversation and have no registered UI render through it instead of the
 * tool group; without it they render like any other tool call.
 */
export type ThreadComponents = {
  AssistantMessage?: ComponentType | undefined;
  Welcome?: ComponentType | undefined;
  /** Agent OS (GĐ3 case 9): rendered right after the last message, inside the scroll flow (HITL approval card) */
  AfterMessages?: ComponentType | undefined;
  ToolFallback?: ToolCallMessagePartComponent | undefined;
  ToolGroup?:
    | ComponentType<PropsWithChildren<{ group: ThreadGroupPart }>>
    | undefined;
  ReasoningGroup?:
    | ComponentType<PropsWithChildren<{ group: ThreadGroupPart }>>
    | undefined;
  TaskGroup?: ComponentType<{ group: ThreadGroupPart }> | undefined;
};

const baseGroupBy = groupPartByType({
  reasoning: ["group-chainOfThought", "group-reasoning"],
  "tool-call": ["group-chainOfThought", "group-tool"],
  "standalone-tool-call": [],
});

// Agent OS: Deep Agents sub-agent calls ("task") are shown as their own card,
// never folded into the collapsed "N tool calls" group, so progress + HITL stay visible.
const messageGroupBy: typeof baseGroupBy = (part, context) =>
  part.type === "tool-call" && (part as { toolName?: string }).toolName === "task"
    ? []
    : baseGroupBy(part, context);

type ThreadGroupKey =
  | "group-chainOfThought"
  | "group-reasoning"
  | "group-tool"
  | "group-task";

const TASK_GROUP_PATH: readonly ThreadGroupKey[] = [
  "group-chainOfThought",
  "group-task",
];

const taskAwareGroupBy = (
  part: Parameters<typeof messageGroupBy>[0],
  context?: Parameters<typeof messageGroupBy>[1],
): readonly ThreadGroupKey[] => {
  const path = messageGroupBy(part, context);
  return part.type === "tool-call" &&
    part.messages !== undefined &&
    path.length > 0 &&
    !context?.toolUIs?.[part.toolName]?.length
    ? TASK_GROUP_PATH
    : path;
};

export type ThreadProps = {
  components?: ThreadComponents | undefined;
  autoFocus?: boolean | undefined;
};

const EMPTY_COMPONENTS: ThreadComponents = {};

const ThreadComponentsContext =
  createContext<ThreadComponents>(EMPTY_COMPONENTS);

// Startup exposes a loading placeholder thread; treat it as a new chat so
// the composer mounts centered. Loads after startup keep the docked layout.
const isNewChatView = (s: AssistantState) =>
  s.thread.messages.length === 0 &&
  (!s.thread.isLoading || s.threads.isLoading);

// A switched thread that is still fetching its history: skeleton, not welcome.
const isHistoryLoadingView = (s: AssistantState) =>
  s.thread.messages.length === 0 &&
  s.thread.isLoading &&
  !s.thread.isDisabled &&
  !s.threads.isLoading;

const ThreadHistorySkeleton: FC = () => (
  <div
    data-slot="aui_thread-history-skeleton"
    role="status"
    className="animate-in fade-in fill-mode-both flex flex-col gap-y-6 [animation-delay:150ms] [animation-duration:200ms]"
  >
    <span className="sr-only">{i18n.t("loadingConversation")}</span>
    <Skeleton className="ml-auto h-9 w-2/5 rounded-xl motion-reduce:animate-none" />
    <div className="flex flex-col gap-y-2">
      <Skeleton className="h-4 w-11/12 motion-reduce:animate-none" />
      <Skeleton className="h-4 w-4/5 motion-reduce:animate-none" />
      <Skeleton className="h-4 w-3/5 motion-reduce:animate-none" />
    </div>
    <Skeleton className="ml-auto h-9 w-1/3 rounded-xl motion-reduce:animate-none" />
    <div className="flex flex-col gap-y-2">
      <Skeleton className="h-4 w-10/12 motion-reduce:animate-none" />
      <Skeleton className="h-4 w-2/3 motion-reduce:animate-none" />
    </div>
  </div>
);

export const Thread: FC<ThreadProps> = ({
  components = EMPTY_COMPONENTS,
  autoFocus = true,
}) => {
  const isEmpty = useAuiState(isNewChatView);

  return (
    <ThreadComponentsContext.Provider value={components}>
      <ThreadRoot isEmpty={isEmpty} autoFocus={autoFocus} />
    </ThreadComponentsContext.Provider>
  );
};

const ThreadRoot: FC<{ isEmpty: boolean; autoFocus: boolean }> = ({
  isEmpty,
  autoFocus,
}) => {
  const { Welcome = ThreadWelcome, AfterMessages } = useContext(ThreadComponentsContext);

  return (
    <ThreadPrimitive.Root
      className="aui-root aui-thread-root bg-background @container flex h-full flex-col"
      style={{
        ["--thread-max-width" as string]: "48rem",
        ["--composer-bg" as string]:
          "color-mix(in oklab, var(--color-muted) 30%, transparent)",
        ["--composer-radius" as string]: "1.5rem",
        ["--composer-padding" as string]: "8px",
      }}
    >
      <ThreadPrimitive.Viewport
        turnAnchor="top"
        data-slot="aui_thread-viewport"
        className="relative flex flex-1 flex-col overflow-x-auto overflow-y-scroll scroll-smooth"
      >
        <div
          className={cn(
            "mx-auto flex w-full max-w-(--thread-max-width) flex-1 flex-col px-4 pt-4",
            isEmpty && "justify-center",
          )}
        >
          <AuiIf condition={isNewChatView}>
            <Welcome />
          </AuiIf>
          <AuiIf condition={isHistoryLoadingView}>
            <ThreadHistorySkeleton />
          </AuiIf>

          <ThreadLoadEarlier />

          <div
            data-slot="aui_message-group"
            className="mb-14 flex flex-col gap-y-6 empty:hidden"
          >
            <ThreadPrimitive.Messages>
              {() => <ThreadMessage />}
            </ThreadPrimitive.Messages>
            {AfterMessages && <AfterMessages />}
          </div>

          <ThreadPrimitive.ViewportFooter
            className={cn(
              "aui-thread-viewport-footer bg-background flex flex-col gap-4 overflow-visible pb-4 md:pb-6",
              !isEmpty &&
                "sticky bottom-0 mt-auto rounded-t-(--composer-radius)",
            )}
          >
            <ThreadScrollToBottom />
            <ThreadFollowupSuggestions />
            <Composer autoFocus={autoFocus} />
          </ThreadPrimitive.ViewportFooter>
        </div>
      </ThreadPrimitive.Viewport>
    </ThreadPrimitive.Root>
  );
};

const ThreadMessage: FC = () => {
  const { AssistantMessage: AssistantMessageComponent = AssistantMessage } =
    useContext(ThreadComponentsContext);
  const role = useAuiState((s) => s.message.role);
  const isEditing = useAuiState((s) => s.message.composer.isEditing);
  const isSpoken = useAuiState((s) => s.message.metadata.modality === "voice");

  if (isEditing) return <EditComposer />;
  if (isSpoken) return <SpokenMessage />;
  if (role === "user") return <UserMessage />;
  return <AssistantMessageComponent />;
};

type VoiceRunPosition = "single" | "start" | "middle" | "end";

const useVoiceRunPosition = (): VoiceRunPosition =>
  useAuiState((s) => {
    const before =
      s.thread.messages[s.message.index - 1]?.metadata.modality === "voice";
    const after =
      s.thread.messages[s.message.index + 1]?.metadata.modality === "voice";
    if (before) return after ? "middle" : "end";
    return after ? "start" : "single";
  });

const SpokenText: TextMessagePartComponent = ({ text }) => (
  <p className="aui-spoken-message-text m-0">{text}</p>
);

const SpokenMessage: FC = () => {
  const { t } = useTranslation();
  const role = useAuiState((s) => s.message.role);
  const position = useVoiceRunPosition();
  const isSpeaking = useAuiState(
    (s) =>
      s.message.role === "assistant" && s.message.status?.type === "running",
  );
  const opensExchange = position === "start" || position === "single";

  return (
    <MessagePrimitive.Root
      data-slot="aui_spoken-message-root"
      data-role={role}
      data-voice-run={position}
      className={cn(
        "aui-spoken-message bg-muted/40 mx-2 px-3 py-1.5 [contain-intrinsic-size:auto_48px] [content-visibility:auto]",
        position === "single" && "rounded-xl py-2",
        position === "start" && "rounded-t-xl pt-2",
        position === "middle" && "-mt-6",
        position === "end" && "-mt-6 rounded-b-xl pb-2",
      )}
    >
      {opensExchange && (
        <div
          data-slot="aui_spoken-exchange-header"
          className="text-muted-foreground mb-1.5 flex items-center gap-1.5 text-xs"
        >
          <PhoneIcon className="size-3" aria-hidden />
          <span>{t("voiceConversation")}</span>
        </div>
      )}
      <div
        data-slot="aui_spoken-message-content"
        className="text-foreground flex items-start gap-2 text-sm leading-relaxed"
      >
        <span className="text-muted-foreground mt-1 shrink-0" aria-hidden>
          {role === "user" ? (
            <MicIcon className="size-3.5" />
          ) : (
            <AudioLinesIcon className="size-3.5" />
          )}
        </span>
        <span className="sr-only">
          {role === "user" ? t("youSaid") : t("assistantSaid")}
        </span>
        <div className="min-w-0 flex-1 wrap-break-word">
          <MessagePrimitive.Parts components={{ Text: SpokenText }} />
          <TruncatedBadge />
          {isSpeaking && (
            <span
              data-slot="aui_spoken-message-indicator"
              role="status"
              className="text-muted-foreground ms-1 animate-pulse font-sans"
              aria-label={t("vSpeaking")}
            >
              ●
            </span>
          )}
        </div>
        <SpokenActionBar />
      </div>
    </MessagePrimitive.Root>
  );
};

const SpokenActionBar: FC = () => {
  const { t } = useTranslation();
  return (
    <ActionBarPrimitive.Root
      hideWhenRunning
      autohide="always"
      className="aui-spoken-action-bar text-muted-foreground flex shrink-0 gap-1"
    >
      <ActionBarPrimitive.Copy render={<TooltipIconButton tooltip={t("copy")} className="size-6" />}><AuiIf condition={(s) => s.message.isCopied}>
                      <CheckIcon className="animate-in zoom-in-50 fade-in duration-200 ease-out" />
                    </AuiIf><AuiIf condition={(s) => !s.message.isCopied}>
                      <CopyIcon className="animate-in zoom-in-75 fade-in duration-150" />
                    </AuiIf></ActionBarPrimitive.Copy>
    </ActionBarPrimitive.Root>
  );
};

const FOCUSABLE_SELECTOR =
  "a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1']):not([disabled])";

const nextFocusable = (start: Element) => {
  for (
    let element = start.nextElementSibling;
    element;
    element = element.nextElementSibling
  ) {
    const target = element.matches(FOCUSABLE_SELECTOR)
      ? element
      : element.querySelector(FOCUSABLE_SELECTOR);
    if (target instanceof HTMLElement) return target;
  }
  return null;
};

const ThreadLoadEarlier: FC = () => {
  const { t } = useTranslation();
  const visible = useAuiState(
    (s) => s.thread.hasEarlier || s.thread.isLoadingEarlier,
  );
  const loading = useAuiState((s) => s.thread.isLoadingEarlier);
  const slotRef = useRef<HTMLDivElement>(null);
  const focusedRef = useRef<Element | null>(null);

  // The last page removes the button; a keyboard user on it continues from the
  // next control in tab order rather than from the document body.
  useLayoutEffect(() => {
    const focused = focusedRef.current;
    const slot = slotRef.current;
    if (visible || !focused || focused.isConnected || !slot) return;
    focusedRef.current = null;
    const active = document.activeElement;
    if (active && active !== document.body) return;
    nextFocusable(slot)?.focus();
  }, [visible]);

  return (
    <div
      ref={slotRef}
      className="contents"
      onFocus={(event) => {
        focusedRef.current = event.target;
      }}
    >
      <span role="status" className="sr-only">
        {loading ? t("loadingEarlier") : ""}
      </span>
      {visible && (
        <ThreadPrimitive.LoadEarlier render={<Button variant="ghost" size="sm" data-slot="aui_thread-load-earlier" className="aui-thread-load-earlier text-muted-foreground mb-6 self-center rounded-full" />}><span
                            className={cn(loading && "shimmer motion-reduce:animate-none")}
                          >
                            {loading ? t("loadingEarlier") : t("loadEarlier")}
                          </span></ThreadPrimitive.LoadEarlier>
      )}
    </div>
  );
};

const ThreadScrollToBottom: FC = () => {
  const { t } = useTranslation();
  return (
    <ThreadPrimitive.ScrollToBottom render={<TooltipIconButton tooltip={t("scrollToBottom")} variant="outline" className="aui-thread-scroll-to-bottom dark:border-border dark:bg-background dark:hover:bg-accent absolute -top-12 z-10 self-center rounded-full p-4 disabled:invisible" />}><ArrowDownIcon /></ThreadPrimitive.ScrollToBottom>
  );
};

const ThreadWelcome: FC = () => {
  const { t } = useTranslation(); const ui = useChatUi();
  return (
    <div className="aui-thread-welcome-root mb-6 flex flex-col px-2">
      <p className="aui-thread-welcome-message-inner fade-in slide-in-from-bottom-1 animate-in fill-mode-both text-2xl font-medium tracking-tight duration-200">
        {ui?.agent.greeting ?? t("greetingFallback", { name: "Agent OS" })}
      </p>
    </div>
  );
};

const Composer: FC<{ autoFocus: boolean }> = ({ autoFocus }) => {
  const { t } = useTranslation(); const ui = useChatUi();
  const ctl = useTurnControl();   // GĐ11.36: queue while running, reject-then-send while awaiting approval
  return (
    <ComposerPrimitive.TriggerPopoverRoot>
    <ComposerPrimitive.Root className="aui-composer-root relative flex w-full flex-col">
      {/* GĐ5a: "@" agents, "/" commands (assistant-ui trigger popovers) */}
      <MentionPopover /><SlashPopover />
      <QueueRunner /><QueueList /><TurnStatus notice={ctl.notice} awaiting={ctl.awaiting} running={ctl.running} />
      <ComposerPrimitive.AttachmentDropzone render={<div data-slot="aui_composer-shell" className="shadow-[0_2px_12px_-4px_rgb(0_0_0/0.12)] dark:shadow-none bg-background dark:bg-muted/40 border-foreground/10 focus-within:border-foreground/25 data-[dragging=true]:border-ring flex w-full cursor-text flex-col gap-2 rounded-(--composer-radius) border bg-(--composer-bg) p-(--composer-padding) transition-[border-color] data-[dragging=true]:border-dashed data-[dragging=true]:bg-[color-mix(in_oklab,var(--color-accent)_50%,var(--color-background))]" />}><ComposerAttachments /><ComposerPrimitive.Input
                      placeholder={t("composerPlaceholder", { name: ui?.agent.name ?? "Agent OS" })}
                      submitMode={ctl.running || ctl.awaiting ? "none" : "enter"} onKeyDown={enterHandler(ctl)}
                      className="aui-composer-input caret-primary placeholder:text-muted-foreground/60 max-h-48 min-h-10 w-full resize-none bg-transparent px-2.5 py-1 text-base leading-6 outline-none"
                      rows={1}
                      autoFocus={autoFocus}
                      enterKeyHint="send"
                      aria-label={t("messageInput")}
                    /><ComposerAction ctl={ctl} /></ComposerPrimitive.AttachmentDropzone>
    </ComposerPrimitive.Root>
    </ComposerPrimitive.TriggerPopoverRoot>
  );
};

const ComposerAction: FC<{ ctl: ReturnType<typeof useTurnControl> }> = ({ ctl }) => {
  const { t } = useTranslation(); const compact = !!useChatUi()?.compact;
  // The stop control only cancels the send while no run it could stop is going.
  const isSending = useAuiState(
    (s) =>
      s.composer.submission !== undefined &&
      !(s.thread.isRunning && s.thread.capabilities.cancel),
  );
  const canSend = (s: AssistantState) => !s.composer.canCancel && !shouldShowComposerResume(s);

  return (
    <>
      <AuiIf condition={(s) => s.composer.dictation != null}>
        <DictationBar />
      </AuiIf>
      <AuiIf condition={(s) => s.composer.dictation == null}>
        <div className="aui-composer-action-wrapper relative flex items-center justify-between gap-1">
          <div className="flex min-w-0 items-center gap-1">{compact ? <ComposerAddAttachment /> : <ComposerPlusMenu />}<ModelPicker /></div>
          <div className="flex shrink-0 items-center gap-1">
            <AuiIf condition={(s) => !compact && s.thread.capabilities.dictation}>
              <DictateButton />
            </AuiIf>
            {/* Voice mode sits where Send is while the composer is empty (ChatGPT/Gemini); Send appears once there is text */}
            <AuiIf condition={(s) => !compact && canSend(s) && s.composer.isEmpty && s.composer.attachments.length === 0 && s.thread.voice === undefined}>
              <VoiceModeButton />
            </AuiIf>
            <AuiIf condition={(s) => canSend(s) && (compact || !(s.composer.isEmpty && s.composer.attachments.length === 0 && s.thread.voice === undefined))}>
              {ctl.awaiting ? (
                <TooltipIconButton tooltip={t("sendSkip")} side="top" type="button" variant="default" size="icon" data-testid="send-skip" className="aui-composer-send size-9 rounded-full" aria-label={t("sendSkip")} onClick={() => void ctl.submit()}><ArrowUpIcon className="aui-composer-send-icon size-[18px]" /></TooltipIconButton>
              ) : (
              <ComposerPrimitive.Send render={<TooltipIconButton tooltip={t("send")} side="top" type="button" variant="default" size="icon" className="aui-composer-send size-9 rounded-full" aria-label={t("send")} />}><ArrowUpIcon className="aui-composer-send-icon size-[18px]" /></ComposerPrimitive.Send>
              )}
            </AuiIf>
            <AuiIf condition={(s) => s.thread.isRunning && !s.composer.isEmpty && s.thread.voice === undefined}>
              <Button type="button" variant="secondary" size="sm" data-testid="queue-send" className="h-9 rounded-full px-3 text-xs" onClick={() => void ctl.submit()}>{t("queueSend")}</Button>
            </AuiIf>
            <AuiIf condition={shouldShowComposerResume}>
              <ComposerPrimitive.Resume render={<TooltipIconButton tooltip={t("resume")} side="top" type="button" variant="default" size="icon" className="aui-composer-resume size-9 rounded-full" aria-label={t("resume")} />}><PlayIcon className="aui-composer-resume-icon size-4" /></ComposerPrimitive.Resume>
            </AuiIf>
            <AuiIf condition={(s) => s.composer.canCancel && (s.thread.voice === undefined || s.composer.submission !== undefined)}>
              <ComposerPrimitive.Cancel render={<Button type="button" variant="default" size="icon" className="aui-composer-cancel size-9 rounded-full" aria-label={isSending ? t("cancelSend") : t("stop")} />}><SquareIcon className="aui-composer-cancel-icon size-3.5 fill-current" /></ComposerPrimitive.Cancel>
            </AuiIf>
          </div>
        </div>
      </AuiIf>
    </>
  );
};

const MessageError: FC = () => {
  const { t } = useTranslation();
  return (
    <MessagePrimitive.Error>
      <ErrorPrimitive.Root data-testid="message-error" className="aui-message-error-root border-destructive bg-destructive/10 text-destructive dark:bg-destructive/5 mt-2 flex items-start gap-2 rounded-md border p-3 text-sm dark:text-red-200">
        <ErrorPrimitive.Message className="aui-message-error-message line-clamp-2 flex-1" />
        {/* GĐ3.7: retry the turn (same input) */}
        <ActionBarPrimitive.Reload render={<Button type="button" variant="outline" size="sm" className="h-7 shrink-0 px-2 text-xs" data-testid="retry" />}>{t("retry")}</ActionBarPrimitive.Reload>
      </ErrorPrimitive.Root>
    </MessagePrimitive.Error>
  );
};

/** GĐ3.7: a run the user stopped is marked as such (not shown as a finished answer) */
const StoppedBadge: FC = () => {
  const { t } = useTranslation();
  const id = useAuiState((s) => s.message.id);
  const marked = useSyncExternalStore(stoppedMessages.subscribe, stoppedMessages.version);
  const cancelled = useAuiState((s) => s.message.status?.type === "incomplete" && s.message.status.reason === "cancelled");
  const stopped = cancelled || marked.includes(id);
  if (!stopped) return null;
  return <p data-testid="stopped" className="text-muted-foreground mt-1 text-xs italic">{t("stoppedByUser")}</p>;
};

const TruncatedBadge: FC = () => {
  const { t } = useTranslation();
  const cut = useAuiState((s) => (s.message.metadata.custom as { voice_truncated?: boolean } | undefined)?.voice_truncated === true);
  if (!cut) return null;
  return <span data-testid="truncated-badge" className="mt-1 inline-block rounded bg-muted px-1.5 text-xs text-muted-foreground">✂ {t("truncatedBadge")}</span>;
};

const AssistantMessage: FC = () => {
  const {
    ToolFallback: ToolFallbackComponent = ToolFallback,
    ToolGroup,
    ReasoningGroup,
    TaskGroup: TaskGroupComponent,
  } = useContext(ThreadComponentsContext);
  const groupBy = TaskGroupComponent ? taskAwareGroupBy : messageGroupBy;

  const ACTION_BAR_PT = "pt-1.5";
  // Keep the action bar inside the contained root's paint box, then cancel its reserved space in flow.
  const ACTION_BAR_HEIGHT = `min-h-7.5 ${ACTION_BAR_PT}`;

  return (
    <MessagePrimitive.Root
      data-slot="aui_assistant-message-root"
      data-role="assistant"
      className="fade-in slide-in-from-bottom-1 animate-in relative -mb-7.5 pb-7.5 duration-150 [contain-intrinsic-size:auto_200px] [content-visibility:auto]"
    >
      <div
        data-slot="aui_assistant-message-content"
        className="text-foreground px-2 leading-relaxed wrap-break-word"
      >
        <DelegatedFrom />
        <MessagePrimitive.GroupedParts groupBy={groupBy}>
          {({ part, children }) => {
            switch (part.type) {
              case "group-chainOfThought":
                return <div data-slot="aui_chain-of-thought">{children}</div>;
              case "group-task":
                return TaskGroupComponent ? (
                  <TaskGroupComponent group={part} />
                ) : null;
              case "group-tool":
                if (ToolGroup) {
                  return <ToolGroup group={part}>{children}</ToolGroup>;
                }
                return (
                  <ToolGroupRoot variant="ghost">
                    <ToolGroupTrigger
                      count={part.indices.length}
                      active={part.status.type === "running"}
                    />
                    <ToolGroupContent>{children}</ToolGroupContent>
                  </ToolGroupRoot>
                );
              case "group-reasoning": {
                if (ReasoningGroup) {
                  return (
                    <ReasoningGroup group={part}>{children}</ReasoningGroup>
                  );
                }
                const running = part.status.type === "running";
                return (
                  <ReasoningRoot streaming={running}>
                    <ReasoningTrigger
                      active={running}
                      duration={reasoningDuration(part.timing)}
                    />
                    <ReasoningContent aria-busy={running}>
                      <ReasoningText>{children}</ReasoningText>
                    </ReasoningContent>
                  </ReasoningRoot>
                );
              }
              case "text":
                return <MarkdownText />;
              case "reasoning":
                return <Reasoning {...part} />;
              case "tool-call":
                return part.toolUI ?? <ToolFallbackComponent {...part} />;
              case "data":
                return part.dataRendererUI;
              case "file":
                return (
                  <div data-slot="aui_assistant-message-file" className="py-1">
                    <MessageFile {...part} />
                  </div>
                );
              case "image":
                return (
                  <div data-slot="aui_assistant-message-image" className="py-1">
                    <Image {...part} />
                  </div>
                );
              case "indicator":
                return (
                  <span
                    data-slot="aui_assistant-message-indicator"
                    className="animate-pulse font-sans"
                    aria-label={i18n.t("working")}
                  >
                    {"●"}
                  </span>
                );
              default:
                return null;
            }
          }}
        </MessagePrimitive.GroupedParts>
        <MessageError />
        <StoppedBadge />
        <TruncatedBadge />
      </div>

      <div
        data-slot="aui_assistant-message-footer"
        className={cn("ms-2 flex items-center", ACTION_BAR_HEIGHT)}
      >
        <BranchPicker />
        <AssistantActionBar />
      </div>
    </MessagePrimitive.Root>
  );
};

/** GĐ11.36: copy menu on the last AI message: the answer only / every step (Markdown with tool calls, each result cut at 4,000 chars) */
const CopyTurnItems: FC = () => {
  const { t } = useTranslation(); const aui = useAui();
  const copy = (steps: boolean) => {
    const parts = aui.message().getState().content as unknown as CopyPart[];
    void navigator.clipboard.writeText(steps ? stepsMarkdown(parts) : answerText(parts));
  };
  const cls = "aui-action-bar-more-item hover:bg-accent hover:text-accent-foreground focus:bg-accent focus:text-accent-foreground flex cursor-pointer items-center gap-2 rounded-sm px-2 py-1.5 text-sm outline-none select-none";
  return (<>
    <ActionBarMorePrimitive.Item data-testid="copy-answer" className={cls} onClick={() => copy(false)}><CopyIcon className="size-4" />{t("copyAnswer")}</ActionBarMorePrimitive.Item>
    <ActionBarMorePrimitive.Item data-testid="copy-steps" className={cls} onClick={() => copy(true)}><CopyIcon className="size-4" />{t("copySteps")}</ActionBarMorePrimitive.Item>
  </>);
};

/** GĐ12.87: "Save this way of working as a Skill" (opens the review dialog; the dialog itself lives in ChatHeader, see skillSaveStore) */
const SaveSkillItem: FC = () => {
  const { t } = useTranslation();
  return <ActionBarMorePrimitive.Item data-testid="save-skill" className="aui-action-bar-more-item hover:bg-accent hover:text-accent-foreground focus:bg-accent focus:text-accent-foreground flex cursor-pointer items-center gap-2 rounded-sm px-2 py-1.5 text-sm outline-none select-none"
    onClick={() => skillSaveStore.open(null)}><SparklesIcon className="size-4" />{t("skillSaveItem")}</ActionBarMorePrimitive.Item>;
};

const AssistantActionBar: FC = () => {
  const { t } = useTranslation();
  return (
    <ActionBarPrimitive.Root
      hideWhenRunning
      autohide="not-last"
      className="aui-assistant-action-bar-root text-muted-foreground animate-in fade-in col-start-3 row-start-2 -ms-1 flex gap-1 duration-200"
    >
      <ActionBarPrimitive.Copy render={<TooltipIconButton tooltip={t("copy")} />}><AuiIf condition={(s) => s.message.isCopied}>
                      <CheckIcon className="animate-in zoom-in-50 fade-in duration-200 ease-out" />
                    </AuiIf><AuiIf condition={(s) => !s.message.isCopied}>
                      <CopyIcon className="animate-in zoom-in-75 fade-in duration-150" />
                    </AuiIf></ActionBarPrimitive.Copy>
      <AuiIf condition={(s) => s.thread.capabilities.feedback}>
        <ActionBarPrimitive.FeedbackPositive render={<TooltipIconButton tooltip={t("good")} className="data-[submitted=true]:bg-accent data-[submitted=true]:text-accent-foreground" />}><ThumbsUpIcon /></ActionBarPrimitive.FeedbackPositive>
        <ActionBarPrimitive.FeedbackNegative render={<TooltipIconButton tooltip={t("bad")} className="data-[submitted=true]:bg-accent data-[submitted=true]:text-accent-foreground" />}><ThumbsDownIcon /></ActionBarPrimitive.FeedbackNegative>
      </AuiIf>
      <ActionBarPrimitive.Reload render={<TooltipIconButton tooltip={t("regenerate")} />}><RefreshCwIcon /></ActionBarPrimitive.Reload>
      <AuiIf condition={(s) => s.thread.capabilities.speech}>
        <AuiIf condition={(s) => s.message.speech == null}>
          <ActionBarPrimitive.Speak render={<TooltipIconButton tooltip={t("readAloud")} data-testid="read-aloud" />}><Volume2Icon /></ActionBarPrimitive.Speak>
        </AuiIf>
        <AuiIf condition={(s) => s.message.speech != null}>
          <ActionBarPrimitive.StopSpeaking render={<TooltipIconButton tooltip={t("stopReading")} data-testid="read-aloud-stop" />}><VolumeXIcon /></ActionBarPrimitive.StopSpeaking>
        </AuiIf>
      </AuiIf>
      <ActionBarMorePrimitive.Root>
        <ActionBarMorePrimitive.Trigger render={<TooltipIconButton tooltip={t("more")} className="data-[state=open]:bg-accent" />}><MoreHorizontalIcon /></ActionBarMorePrimitive.Trigger>
        <ActionBarMorePrimitive.Content
          side="bottom"
          align="start"
          sideOffset={6}
          className="aui-action-bar-more-content bg-popover text-popover-foreground data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95 data-[state=open]:animate-in data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95 data-[state=closed]:animate-out data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2 z-50 min-w-[8rem] overflow-hidden rounded-xl border p-1.5"
        >
          <AuiIf condition={(s) => s.message.isLast}><CopyTurnItems /></AuiIf>
          {!RO.on && <AuiIf condition={(s) => s.message.isLast}><SaveSkillItem /></AuiIf>}
          <ActionBarPrimitive.ExportMarkdown render={<ActionBarMorePrimitive.Item className="aui-action-bar-more-item hover:bg-accent hover:text-accent-foreground focus:bg-accent focus:text-accent-foreground flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm outline-none select-none" />}><DownloadIcon className="size-4" />{t("exportMd")}
                              </ActionBarPrimitive.ExportMarkdown>
        </ActionBarMorePrimitive.Content>
      </ActionBarMorePrimitive.Root>
    </ActionBarPrimitive.Root>
  );
};

const MessageFile: FileMessagePartComponent = (part) => {
  const image = part.mimeType.toLowerCase().startsWith("image/")
    ? getFileHref(part.data, part.mimeType, part.sourceType)
    : null;
  if (!image) return <File {...part} />;
  return (
    <Image
      type="image"
      image={image}
      status={part.status}
      {...(part.filename !== undefined && { filename: part.filename })}
    />
  );
};

const UserFilePart: FileMessagePartComponent = (part) => (
  <div data-slot="aui_user-message-file" className="py-1">
    <MessageFile {...part} />
  </div>
);

const UserImagePart: ImageMessagePartComponent = (part) => (
  <div data-slot="aui_user-message-image" className="py-1">
    <Image {...part} />
  </div>
);

/** GĐ5a: "@Tên agent" in a sent message renders as a chip (same formatter as the composer). */
const MentionText: TextMessagePartComponent = createDirectiveText(agentFormatter, { iconMap: { agent: BotIcon } });

/** GĐ5a: answer written by another agent through an @mention (POST /api/chat/delegate) */
const DelegatedFrom: FC = () => {
  const { t } = useTranslation();
  const md = useAuiState((s) => s.message.metadata?.custom as { delegated_from?: string; delegate_name?: string; delegate_status?: string } | undefined);
  if (!md?.delegated_from) return null;
  return (
    <div data-testid="delegated-from" className="text-muted-foreground mb-1.5 flex items-center gap-1.5 text-xs">
      <CornerDownRightIcon className="size-3.5" aria-hidden />
      <span>{t("delegatedFrom", { name: (md.delegate_name ?? md.delegated_from).replace(/\s*\((fake|mock)\)\s*$/i, "") })}</span>
      {md.delegate_status === "interrupted" && <span className="rounded-full border px-1.5 py-px">{t("delegateWaiting")}</span>}
    </div>
  );
};

const UserMessage: FC = () => {
  return (
    <MessagePrimitive.Root
      data-slot="aui_user-message-root"
      className="fade-in slide-in-from-bottom-1 animate-in grid auto-rows-auto grid-cols-[minmax(72px,1fr)_auto] content-start gap-y-2 px-2 duration-150 [contain-intrinsic-size:auto_200px] [content-visibility:auto] [&:where(>*)]:col-start-2"
      data-role="user"
    >
      <UserMessageAttachments />

      <div className="aui-user-message-content-wrapper relative col-start-2 min-w-0">
        <div className="aui-user-message-content peer bg-muted text-foreground rounded-(--composer-radius) px-4 py-2 wrap-break-word empty:hidden">
          <MessagePrimitive.Parts
            components={{ File: UserFilePart, Image: UserImagePart, Text: MentionText }}
          />
        </div>
        <div className="aui-user-action-bar-wrapper absolute start-0 top-1/2 -translate-x-full -translate-y-1/2 pe-2 peer-empty:hidden rtl:translate-x-full">
          <UserActionBar />
        </div>
      </div>

      <BranchPicker
        data-slot="aui_user-branch-picker"
        className="col-span-full col-start-1 -me-1 justify-end"
      />
    </MessagePrimitive.Root>
  );
};

const UserActionBar: FC = () => {
  const { t } = useTranslation();
  return (
    <ActionBarPrimitive.Root
      hideWhenRunning
      autohide="not-last"
      className="aui-user-action-bar-root flex flex-col items-end"
    >
      <ActionBarPrimitive.Edit render={<TooltipIconButton tooltip={t("editMsg")} className="aui-user-action-edit" />}><PencilIcon /></ActionBarPrimitive.Edit>
    </ActionBarPrimitive.Root>
  );
};

const EditComposer: FC = () => {
  const { t } = useTranslation();
  return (
    <MessagePrimitive.Root
      data-slot="aui_edit-composer-wrapper"
      className="flex flex-col px-2 [contain-intrinsic-size:auto_200px] [content-visibility:auto]"
    >
      <ComposerPrimitive.Root className="aui-edit-composer-root border-foreground/10 focus-within:border-foreground/25 ms-auto flex w-full max-w-[85%] cursor-text flex-col rounded-(--composer-radius) border bg-(--composer-bg) transition-[border-color]">
        <ComposerPrimitive.Input
          className="aui-edit-composer-input text-foreground min-h-14 w-full resize-none bg-transparent px-4 pt-3 pb-1 text-base outline-none"
          autoFocus
        />
        <div className="aui-edit-composer-footer mx-2.5 mb-2.5 flex items-center gap-1.5 self-end">
          <ComposerPrimitive.Cancel render={<Button variant="ghost" size="sm" className="h-8 px-3" />}>{t("cancel")}
                              </ComposerPrimitive.Cancel>
          <ComposerPrimitive.Send render={<Button size="sm" className="h-8 px-3" />}>{t("update")}
                              </ComposerPrimitive.Send>
        </div>
      </ComposerPrimitive.Root>
    </MessagePrimitive.Root>
  );
};

const BranchPicker: FC<BranchPickerPrimitive.Root.Props> = ({
  className,
  ...rest
}) => {
  const { t } = useTranslation();
  return (
    <BranchPickerPrimitive.Root
      hideWhenSingleBranch
      className={cn(
        "aui-branch-picker-root text-muted-foreground -ms-2 me-2 inline-flex items-center text-xs",
        className,
      )}
      {...rest}
    >
      <BranchPickerPrimitive.Previous render={<TooltipIconButton tooltip={t("prev")} />}><ChevronLeftIcon /></BranchPickerPrimitive.Previous>
      <span className="aui-branch-picker-state font-medium">
        <BranchPickerPrimitive.Number /> / <BranchPickerPrimitive.Count />
      </span>
      <BranchPickerPrimitive.Next render={<TooltipIconButton tooltip={t("next")} />}><ChevronRightIcon /></BranchPickerPrimitive.Next>
    </BranchPickerPrimitive.Root>
  );
};
