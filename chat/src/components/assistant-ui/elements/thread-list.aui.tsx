"use client";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import i18n from "@/i18n";
import { useTranslation } from "react-i18next";
import {
  AuiIf,
  ThreadListItemMorePrimitive,
  ThreadListItemPrimitive,
  ThreadListPrimitive,
  useAui,
  useAuiState,
} from "@assistant-ui/react";
import {
  ArchiveIcon,
  AudioLinesIcon,
  FolderInputIcon,
  FolderMinusIcon,
  Loader2Icon,
  MoreHorizontalIcon,
  ArchiveRestoreIcon,
  PencilIcon,
  PinIcon,
  PinOffIcon,
  PlusIcon,
  SearchIcon,
  TrashIcon,
} from "lucide-react";
import {
  forwardRef,
  Fragment,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type ComponentPropsWithoutRef,
  type FC,
} from "react";
import { moveThreadToProject } from "@/lib/agentos";
import { projectOfThread, projectsStore, showInList } from "@/lib/projectsStore";
import { inboxStore, matchesFilter } from "@/lib/inboxStore";

export const ThreadList: FC = () => {
  const [search, setSearch] = useState("");
  const hasThreads = useAuiState((s) => s.threads.threadIds.length > 0);

  return (
    <ThreadListRoot>
      <ThreadListNew />
      {hasThreads && (
        <ThreadListSearch value={search} onValueChange={setSearch} />
      )}
      <ThreadListItems searchQuery={hasThreads ? search : ""} />
    </ThreadListRoot>
  );
};

export const ThreadListSearch = forwardRef<
  HTMLInputElement,
  Omit<ComponentPropsWithoutRef<typeof Input>, "value" | "onChange"> & {
    value: string;
    onValueChange: (value: string) => void;
  }
>(({ className, value, onValueChange, ...props }, ref) => {
  return (
    <div data-slot="aui_thread-list-search" className="relative px-0.5 py-1">
      <SearchIcon
        data-slot="aui_thread-list-search-icon"
        className="text-muted-foreground pointer-events-none absolute start-3 top-1/2 size-4 -translate-y-1/2"
      />
      <Input
        ref={ref}
        type="search"
        value={value}
        onChange={(event) => onValueChange(event.target.value)}
        aria-label={i18n.t("searchChats")}
        placeholder={i18n.t("searchChats")}
        className={cn("h-8 ps-8 text-sm", className)}
        {...props}
      />
    </div>
  );
});

ThreadListSearch.displayName = "ThreadListSearch";

export const ThreadListRoot: FC<
  ComponentPropsWithoutRef<typeof ThreadListPrimitive.Root>
> = ({ className, ...props }) => {
  return (
    <ThreadListPrimitive.Root
      data-slot="aui_thread-list-root"
      className={cn("flex flex-col gap-0.5", className)}
      {...props}
    />
  );
};

export const ThreadListItems: FC<
  ComponentPropsWithoutRef<"div"> & { searchQuery?: string }
> = ({ className, searchQuery = "", ...props }) => {
  return (
    <div
      data-slot="aui_thread-list-items"
      className={cn("flex flex-col gap-0.5", className)}
      {...props}
    >
      <AuiIf condition={(s) => s.threads.isLoading}>
        <ThreadListSkeleton />
      </AuiIf>
      <AuiIf condition={(s) => !s.threads.isLoading}>
        <ThreadListItemGroups searchQuery={searchQuery} />
      </AuiIf>
    </div>
  );
};

const dateGroupLabel = (
  date: Date | undefined,
  startOfToday: number,
  startOfYesterday: number,
): string => {
  if (!date || date.getTime() >= startOfToday) return i18n.t("groupToday");
  if (date.getTime() >= startOfYesterday) return i18n.t("groupYesterday");
  if (date.getTime() >= startOfYesterday - 6 * 864e5) return i18n.t("group7d");
  return i18n.t("groupOlder");
};

const startOfLocalDay = (date: Date) =>
  new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();

const subscribeToNextDay = (onDayChange: () => void) => {
  let timeout: number;
  const scheduleNextDay = () => {
    const now = new Date();
    const startOfTomorrow = new Date(
      now.getFullYear(),
      now.getMonth(),
      now.getDate() + 1,
    ).getTime();
    timeout = window.setTimeout(() => {
      onDayChange();
      scheduleNextDay();
    }, startOfTomorrow - now.getTime());
  };
  scheduleNextDay();
  return () => window.clearTimeout(timeout);
};

const getStartOfToday = () => startOfLocalDay(new Date());

// A server render and hydration see no day start, so a prerender never reads
// the clock; a client-only mount groups on its first render.
const getServerStartOfToday = () => undefined;

const useStartOfToday = () =>
  useSyncExternalStore<number | undefined>(
    subscribeToNextDay,
    getStartOfToday,
    getServerStartOfToday,
  );

export type ThreadListGroup = { label: string; indices: number[] };

/**
 * Filters the thread list by title and buckets the matches by last activity
 * (Today, Yesterday, Earlier). `groups` is null when no thread carries a date
 * or while the local day start is unknown during server render and hydration,
 * in which case `filteredIndices` keeps the runtime order.
 */
export const useThreadListGroups = (searchQuery = "") => {
  const threadIds = useAuiState((s) => s.threads.threadIds);
  const threadItems = useAuiState((s) => s.threads.threadItems);

  const query = searchQuery.trim().toLowerCase();
  const startOfToday = useStartOfToday();
  const lang = useTranslation().i18n.language;
  const inbox = useSyncExternalStore(inboxStore.subscribe, inboxStore.get);   // GĐ11.35: unread / awaiting chips
  const ps = useSyncExternalStore(projectsStore.subscribe, projectsStore.get);   // GĐ8.9: only the open project's chats (or chats in no project)

  return useMemo(() => {
    const itemsById = new Map(threadItems.map((item) => [item.id, item]));
    const dates = threadIds.map((id) => itemsById.get(id)?.lastMessageAt);
    const filteredIndices = threadIds
      .map((id, index) => ({ id, index }))
      .filter(({ id }) => { const it = itemsById.get(id); return showInList(projectOfThread(ps.byThread, it?.remoteId, it?.custom), ps.active); })
      .filter(({ id }) => matchesFilter(inbox.filter, inbox.byThread[itemsById.get(id)?.remoteId ?? ""]))
      .filter(
        ({ id }) =>
          !query ||
          (itemsById.get(id)?.title || i18n.t("untitled"))
            .toLowerCase()
            .includes(query),
      )
      .map(({ index }) => index);
    if (
      startOfToday === undefined ||
      (!filteredIndices.some((index) => dates[index]) && !filteredIndices.some((index) => (itemsById.get(threadIds[index]!)?.custom as { pinned?: boolean } | undefined)?.pinned))
    ) {
      return { threadIds, filteredIndices, groups: null };
    }

    const yesterday = new Date(startOfToday);
    yesterday.setDate(yesterday.getDate() - 1);
    const startOfYesterday = yesterday.getTime();
    const time = (index: number) =>
      dates[index]?.getTime() ?? Number.MAX_SAFE_INTEGER;
    const sorted = [...filteredIndices].sort((a, b) => time(b) - time(a));

    const result: ThreadListGroup[] = [];
    // GĐ5a: pinned chats first (metadata.pinned via the adapter's custom), then by date
    const isPinned = (index: number) => !!(itemsById.get(threadIds[index]!)?.custom as { pinned?: boolean } | undefined)?.pinned;
    const pinned = sorted.filter(isPinned);
    if (pinned.length) result.push({ label: i18n.t("pinned"), indices: pinned });
    for (const index of sorted) {
      if (isPinned(index)) continue;
      const label = dateGroupLabel(
        dates[index],
        startOfToday,
        startOfYesterday,
      );
      const lastGroup = result[result.length - 1];
      if (lastGroup?.label === label) {
        lastGroup.indices.push(index);
      } else {
        result.push({ label, indices: [index] });
      }
    }
    return { threadIds, filteredIndices, groups: result };
  }, [threadIds, threadItems, query, startOfToday, lang, ps, inbox]);
};

const ThreadListItemGroups: FC<{ searchQuery?: string }> = ({
  searchQuery = "",
}) => {
  const { threadIds, filteredIndices, groups } =
    useThreadListGroups(searchQuery);
  const query = searchQuery.trim();

  if (!query && filteredIndices.length === 0 && projectsStore.get().active != null) {
    return <p data-testid="project-empty" className="text-muted-foreground px-2.5 py-2 text-sm">{i18n.t("projectNoChats")}</p>;
  }
  if (query && filteredIndices.length === 0) {
    return (
      <div
        data-slot="aui_thread-list-empty"
        className="text-muted-foreground px-2.5 py-4 text-sm"
      >
        {i18n.t("noResults")}
      </div>
    );
  }

  if (!groups) {
    return filteredIndices.map((index) => (
      <ThreadListPrimitive.ItemByIndex
        key={threadIds[index]}
        index={index}
        components={{ ThreadListItem }}
      />
    ));
  }

  return groups.map((group) => (
    <Fragment key={group.label}>
      <div
        data-slot="aui_thread-list-group-label"
        className="text-muted-foreground px-2.5 pt-3 pb-1 text-xs font-medium"
      >
        {group.label}
      </div>
      {group.indices.map((index) => (
        <ThreadListPrimitive.ItemByIndex
          key={threadIds[index]}
          index={index}
          components={{ ThreadListItem }}
        />
      ))}
    </Fragment>
  ));
};

export const ThreadListNew = forwardRef<
  HTMLButtonElement,
  ComponentPropsWithoutRef<typeof Button> & { labelClassName?: string }
>(({ className, labelClassName, children, ...props }, ref) => {
  return (
    <ThreadListPrimitive.New render={<Button ref={ref} variant="ghost" data-slot="aui_thread-list-new" className={cn(
                "hover:bg-muted data-active:bg-muted h-8 justify-start gap-2 rounded-md px-2.5 text-sm font-normal",
                className,
              )} {...props} />}>{children ?? (
                <>
                  <PlusIcon
                    data-slot="aui_thread-list-new-icon"
                    className="size-4 shrink-0"
                  />
                  <span
                    data-slot="aui_thread-list-new-label"
                    className={cn("whitespace-nowrap", labelClassName)}
                  >
                    {i18n.t("newChat")}
                  </span>
                </>
              )}</ThreadListPrimitive.New>
  );
});

ThreadListNew.displayName = "ThreadListNew";

const ThreadListSkeleton: FC = () => {
  return (
    <div
      role="status"
      aria-label={i18n.t("loadingConversation")}
      className="flex flex-col gap-0.5"
    >
      {Array.from({ length: 5 }, (_, i) => (
        <div
          key={i}
          aria-hidden="true"
          data-slot="aui_thread-list-skeleton-wrapper"
          className="flex h-8 items-center px-2.5"
        >
          <Skeleton
            data-slot="aui_thread-list-skeleton"
            className="h-3.5 w-full"
          />
        </div>
      ))}
    </div>
  );
};

export const ThreadListItem: FC = () => {
  const { t } = useTranslation();
  const isRunning = useAuiState((s) => s.threadListItem.isRunning);
  const isVoice = useAuiState((s) => !!(s.threadListItem.custom as { voice?: boolean } | undefined)?.voice);
  const isPinned = useAuiState((s) => !!(s.threadListItem.custom as { pinned?: boolean } | undefined)?.pinned);
  const rid = useAuiState((s) => s.threadListItem.remoteId);
  const inbox = useSyncExternalStore(inboxStore.subscribe, inboxStore.get).byThread[rid ?? ""];   // GĐ11.35
  const [isRenaming, setIsRenaming] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const restoreFocusRef = useRef(false);

  useEffect(() => {
    if (isRenaming || !restoreFocusRef.current) return;
    restoreFocusRef.current = false;
    triggerRef.current?.focus();
  }, [isRenaming]);

  return (
    <ThreadListItemPrimitive.Root
      data-slot="aui_thread-list-item"
      className="group hover:bg-muted focus-visible:bg-muted data-active:bg-muted has-focus-visible:bg-muted has-data-[state=open]:bg-muted relative flex h-8 items-center rounded-md transition-colors focus-visible:outline-none"
    >
      {isRenaming ? (
        <ThreadListItemRename
          onDone={(restoreFocus) => {
            restoreFocusRef.current = restoreFocus;
            setIsRenaming(false);
          }}
        />
      ) : (
        <ThreadListItemPrimitive.Trigger
          ref={triggerRef}
          data-slot="aui_thread-list-item-trigger"
          className="focus-visible:ring-ring/50 flex h-full min-w-0 flex-1 items-center rounded-md px-2.5 text-start text-sm outline-none group-hover:pe-9 group-has-focus-visible:pe-9 group-has-data-[state=open]:pe-9 group-data-active:pe-9 focus-visible:ring-1"
        >
          {isRunning && (
            <Loader2Icon
              aria-hidden
              data-slot="aui_thread-list-item-running"
              className="text-muted-foreground me-1.5 size-3.5 shrink-0 animate-spin"
            />
          )}
          {isPinned && !isRunning && !isVoice && (
            <PinIcon aria-label={t("pinned")} data-testid="pinned-icon" className="text-muted-foreground me-1.5 size-3.5 shrink-0" />
          )}
          {isVoice && !isRunning && (
            <AudioLinesIcon aria-label={t("voiceThread")} data-testid="voice-thread-icon" className="text-muted-foreground me-1.5 size-3.5 shrink-0" />
          )}
          <span
            data-slot="aui_thread-list-item-title"
            className="min-w-0 flex-1 truncate"
          >
            <ItemTitle />
          </span>
          {isRunning && <span className="sr-only">{t("running")}</span>}
          {inbox?.awaiting ? (
            <span data-testid="awaiting-badge" className="ms-1.5 shrink-0 rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 dark:text-amber-300">{t("awaitingBadge")}</span>
          ) : inbox?.unread && !isRunning ? (
            <span data-testid="unread-dot" role="img" aria-label={t("unreadDot")} className="ms-1.5 size-2 shrink-0 rounded-full bg-blue-500" />
          ) : null}
        </ThreadListItemPrimitive.Trigger>
      )}
      <ThreadListItemMore onRename={() => setIsRenaming(true)} />
    </ThreadListItemPrimitive.Root>
  );
};

const ThreadListItemRename: FC<{
  onDone: (restoreFocus: boolean) => void;
}> = ({ onDone }) => {
  const aui = useAui();
  const title = useAuiState((s) => s.threadListItem.title) ?? "";
  const [value, setValue] = useState(title);
  const inputRef = useRef<HTMLInputElement>(null);
  const settledRef = useRef(false);

  useEffect(() => {
    inputRef.current?.select();
  }, []);

  const commit = (restoreFocus: boolean) => {
    if (settledRef.current) return;
    settledRef.current = true;

    const next = value.trim();
    if (!next || next === title) {
      onDone(restoreFocus);
      return;
    }

    // Deferred so a synchronous throw lands on the rejection path too.
    Promise.resolve()
      .then(() => aui.threadListItem.rename(next))
      .then(
        () => onDone(restoreFocus),
        () => {
          settledRef.current = false;
          if (restoreFocus) inputRef.current?.focus();
        },
      );
  };

  const cancel = () => {
    if (settledRef.current) return;
    settledRef.current = true;
    onDone(true);
  };

  return (
    <Input
      ref={inputRef}
      autoFocus
      data-slot="aui_thread-list-item-rename"
      aria-label={i18n.t("rename")}
      value={value}
      className="h-7 min-w-0 flex-1 ps-2.5 pe-9 text-sm"
      onChange={(event) => setValue(event.target.value)}
      onBlur={() => commit(false)}
      onKeyDown={(event) => {
        if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229)
          return;
        if (event.key === "Enter") {
          event.preventDefault();
          commit(true);
        } else if (event.key === "Escape") {
          event.preventDefault();
          cancel();
        }
      }}
    />
  );
};

const MENU_ITEM = "hover:bg-accent hover:text-accent-foreground focus:bg-accent focus:text-accent-foreground flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm outline-none select-none";

const ThreadListItemMore: FC<{ onRename: () => void }> = ({ onRename }) => {
  const aui = useAui();
  const custom = useAuiState((s) => s.threadListItem.custom);
  const pinned = !!(custom as { pinned?: boolean } | undefined)?.pinned;
  const archived = useAuiState((s) => s.threadListItem.status === "archived");
  const remoteId = useAuiState((s) => s.threadListItem.remoteId);
  const ps = useSyncExternalStore(projectsStore.subscribe, projectsStore.get);
  const inProject = projectOfThread(ps.byThread, remoteId, custom);
  const move = (pid: number | null) => { if (!remoteId) return;
    const before = inProject; projectsStore.assign(remoteId, pid);
    moveThreadToProject(remoteId, pid).catch(() => projectsStore.assign(remoteId, before)); };
  return (
    <ThreadListItemMorePrimitive.Root sharedFocusGroup>
      <ThreadListItemMorePrimitive.Trigger render={<Button variant="ghost" size="icon" data-slot="aui_thread-list-item-more" className="data-[state=open]:bg-accent absolute end-1.5 top-1/2 size-6 -translate-y-1/2 p-0 opacity-0 group-hover:opacity-100 group-has-focus-visible:opacity-100 group-data-active:opacity-100 data-[state=open]:opacity-100" />}><MoreHorizontalIcon className="size-3.5" /><span className="sr-only">{i18n.t("more")}</span></ThreadListItemMorePrimitive.Trigger>
      <ThreadListItemMorePrimitive.Content
        side="right"
        align="start"
        sideOffset={6}
        data-slot="aui_thread-list-item-more-content"
        className="bg-popover text-popover-foreground data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95 data-[state=open]:animate-in data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95 data-[state=closed]:animate-out data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2 z-50 min-w-32 overflow-hidden rounded-xl border p-1.5"
      >
        <ThreadListItemMorePrimitive.Item
          data-slot="aui_thread-list-item-more-item"
          className="hover:bg-accent hover:text-accent-foreground focus:bg-accent focus:text-accent-foreground flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm outline-none select-none"
          onSelect={onRename}
        >
          <PencilIcon className="size-4" />
          {i18n.t("rename")}
        </ThreadListItemMorePrimitive.Item>
        {!archived && (
          <ThreadListItemMorePrimitive.Item data-slot="aui_thread-list-item-more-item" data-testid="thread-pin" className={MENU_ITEM}
            onSelect={() => aui.threadListItem().updateCustom({ ...(custom ?? {}), pinned: !pinned })}>
            {pinned ? <PinOffIcon className="size-4" /> : <PinIcon className="size-4" />}{pinned ? i18n.t("unpin") : i18n.t("pin")}
          </ThreadListItemMorePrimitive.Item>)}
        {/* GĐ8.9: move this chat into a project / out of it */}
        {!archived && remoteId && !ps.readOnly && ps.projects.filter((p) => p.id !== inProject).slice(0, 8).map((p) => (
          <ThreadListItemMorePrimitive.Item key={p.id} data-slot="aui_thread-list-item-more-item" data-testid="thread-move-project" className={MENU_ITEM} onSelect={() => move(p.id)}>
            <FolderInputIcon className="size-4" /><span className="max-w-48 truncate">{i18n.t("projectMoveTo", { name: p.name })}</span>
          </ThreadListItemMorePrimitive.Item>))}
        {!archived && remoteId && inProject != null && (
          <ThreadListItemMorePrimitive.Item data-slot="aui_thread-list-item-more-item" data-testid="thread-remove-project" className={MENU_ITEM} onSelect={() => move(null)}>
            <FolderMinusIcon className="size-4" />{i18n.t("projectRemoveChat")}
          </ThreadListItemMorePrimitive.Item>)}
        {archived ? (
          <ThreadListItemPrimitive.Unarchive render={<ThreadListItemMorePrimitive.Item data-slot="aui_thread-list-item-more-item" data-testid="thread-unarchive" className={MENU_ITEM} />}><ArchiveRestoreIcon className="size-4" />{i18n.t("unarchive")}
                        </ThreadListItemPrimitive.Unarchive>
        ) : (
          <ThreadListItemPrimitive.Archive render={<ThreadListItemMorePrimitive.Item data-slot="aui_thread-list-item-more-item" data-testid="thread-archive" className={MENU_ITEM} />}><ArchiveIcon className="size-4" />{i18n.t("archive")}
                        </ThreadListItemPrimitive.Archive>
        )}
        <ThreadListItemPrimitive.Delete render={<ThreadListItemMorePrimitive.Item data-slot="aui_thread-list-item-more-item" className="text-destructive hover:bg-destructive/10 hover:text-destructive focus:bg-destructive/10 focus:text-destructive flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm outline-none select-none" />}><TrashIcon className="size-4" />{i18n.t("delete")}
                        </ThreadListItemPrimitive.Delete>
      </ThreadListItemMorePrimitive.Content>
    </ThreadListItemMorePrimitive.Root>
  );
};

/** Voice-call placeholder titles ("Voice call" / "Cuộc gọi thoại") are shown in the current UI language. */
const PLACEHOLDER_TITLES = new Set(["Voice call", "Cuộc gọi thoại"]);
function ItemTitle() {
  const { t } = useTranslation();
  const title = useAuiState((s) => s.threadListItem.title);
  if (title && PLACEHOLDER_TITLES.has(title)) return <>{t("voiceCallTitle")}</>;
  return <ThreadListItemPrimitive.Title fallback={t("untitled")} />;
}

/** GĐ5a: archived chats (status "archived" from metadata.archived), listed under a collapsible "Đã lưu trữ" section. */
export const ArchivedThreadItems: FC = () => <ThreadListPrimitive.Items archived components={{ ThreadListItem }} />;
