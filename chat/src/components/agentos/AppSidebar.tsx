// Left sidebar (ChatGPT / Gemini layout, Oct 2026): brand + New chat, AGENTS (like GPTs / Gems, grouped), search, CHATS grouped
// by date with auto titles (voice threads get an icon), user menu at the bottom (Settings, language, theme, sign out).
import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { useTranslation } from "react-i18next";
import { Menu } from "@base-ui/react/menu";
import { useAui, useAuiState } from "@assistant-ui/react";
import { ArchiveIcon, CheckIcon, ChevronRightIcon, LanguagesIcon, LoaderIcon, LogOutIcon, MonitorIcon, MoonIcon, PanelLeftCloseIcon, SettingsIcon, SquarePenIcon, SunIcon } from "lucide-react";
import { ArchivedThreadItems, ThreadListItems, ThreadListRoot, ThreadListSearch } from "@/components/assistant-ui/elements/thread-list.aui";
import { TooltipIconButton } from "@/components/assistant-ui/elements/tooltip-icon-button";
import { cn } from "@/lib/utils";
import { RO, searchThreads, type Me, type ThreadHit } from "@/lib/agentos";
import type { Theme } from "@/lib/theme";
import { AgentAvatar, type AgentProfile } from "./ui-context";
import { ProjectHeader, ProjectsSection } from "./ProjectsPanel";
import { useProjects } from "@/lib/projectsStore";
import { inboxStore, markThreadRead, refreshInbox, type InboxFilter } from "@/lib/inboxStore";

type Props = {
  me: Me; agents: AgentProfile[]; activeAgent: string; onNewChat: (graphId?: string) => void; onClose?: () => void;
  lang: "vi" | "en"; onLang: (l: "vi" | "en") => void; theme: Theme; onTheme: (t: Theme) => void; onSettings: () => void; onSignOut: () => void;
};

/** GĐ11.35: keeps the inbox fresh (every 30 s), marks the open chat read (when opened and when its run ends) */
function useInboxSync() {
  const threadId = useAuiState((s) => s.threads.threadItems.find((i) => i.id === s.threads.mainThreadId)?.remoteId);
  const running = useAuiState((s) => s.thread.isRunning);
  useEffect(() => {
    if (RO.demo) { inboxStore.disable(); return; }
    void refreshInbox(); const id = window.setInterval(() => void refreshInbox(), 30_000); return () => window.clearInterval(id);
  }, []);
  useEffect(() => { if (threadId && !running) void markThreadRead(threadId); }, [threadId, running]);
}

function InboxChips() {
  const { t } = useTranslation();
  const st = useSyncExternalStore(inboxStore.subscribe, inboxStore.get);
  if (!st.enabled) return null;
  const chips: [InboxFilter, string, number | null][] = [["all", t("inboxAll"), null], ["unread", t("inboxUnread"), st.counts.unread], ["awaiting", t("inboxAwaiting"), st.counts.awaiting]];
  return (
    <div role="group" aria-label={t("inboxFilterLabel")} data-testid="inbox-chips" className="flex flex-wrap gap-1 px-0.5 py-1">
      {chips.map(([f, label, n]) => (
        <button key={f} type="button" data-testid={`inbox-chip-${f}`} aria-pressed={st.filter === f} onClick={() => inboxStore.setFilter(f)}
          className={cn("focus-visible:ring-ring/50 rounded-full border px-2.5 py-0.5 text-xs outline-none focus-visible:ring-2",
            st.filter === f ? "bg-foreground text-background border-transparent" : "text-muted-foreground hover:bg-sidebar-accent")}>
          {label}{n ? ` ${n}` : ""}
        </button>
      ))}
    </div>
  );
}

export function AppSidebar(p: Props) {
  const { t } = useTranslation();
  useInboxSync();
  const [search, setSearch] = useState("");
  const hasThreads = useAuiState((s) => s.threads.threadIds.length + s.threads.archivedThreadIds.length > 0);
  const archivedN = useAuiState((s) => s.threads.archivedThreadIds.length);
  const searching = search.trim().length >= 2;
  const inProject = useProjects().active != null;   // GĐ8.9: an open project replaces the "Chats" heading
  const groups = useMemo(() => {
    const m = new Map<string, AgentProfile[]>(); for (const a of p.agents) m.set(a.group, [...(m.get(a.group) ?? []), a]); return [...m.entries()];
  }, [p.agents]);
  return (
    <nav aria-label={t("chats")} className="bg-sidebar text-sidebar-foreground flex h-full w-full flex-col">
      <div className="flex items-center gap-2 px-3 pt-3 pb-2">
        <span aria-hidden className="bg-foreground text-background flex size-7 items-center justify-center rounded-lg text-xs font-bold">AO</span>
        <span className="font-heading text-[15px] font-semibold tracking-tight">{t("appTitle")}</span>
        {p.onClose && (
          <TooltipIconButton tooltip={t("collapseSidebar")} variant="ghost" size="icon" className="ms-auto size-8" onClick={p.onClose} data-testid="sidebar-close">
            <PanelLeftCloseIcon className="size-[18px]" />
          </TooltipIconButton>
        )}
      </div>
      <div className="px-2">
        <button type="button" data-testid="new-chat" onClick={() => p.onNewChat()}
          className="hover:bg-sidebar-accent focus-visible:ring-ring/50 flex h-9 w-full items-center gap-2.5 rounded-lg px-2.5 text-sm font-medium outline-none focus-visible:ring-2">
          <SquarePenIcon className="size-4" aria-hidden />{t("newChat")}
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        <section aria-labelledby="sb-agents" className="mt-3">
          <h2 id="sb-agents" className="text-muted-foreground px-2.5 pb-1 text-xs font-medium">{t("agents")}</h2>
          {groups.map(([g, list]) => (
            <div key={g} role="group" aria-label={g} className="mb-1">
              {groups.length > 1 || g ? <div className="text-muted-foreground/80 px-2.5 pt-1 pb-0.5 text-[11px] tracking-wide uppercase">{g}</div> : null}
              {list.map((a) => (
                <button key={a.graph_id} type="button" data-testid={`agent-${a.graph_id}`} aria-current={a.graph_id === p.activeAgent ? "true" : undefined}
                  onClick={() => p.onNewChat(a.graph_id)} title={a.role}
                  className={cn("hover:bg-sidebar-accent focus-visible:ring-ring/50 flex w-full items-center gap-2.5 rounded-lg px-2 py-1.5 text-start outline-none focus-visible:ring-2",
                    a.graph_id === p.activeAgent && "bg-sidebar-accent")}>
                  <AgentAvatar agent={a} size={26} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium leading-tight">{a.name}</span>
                    <span className="text-muted-foreground block truncate text-xs leading-tight">{a.role}</span>
                  </span>
                </button>
              ))}
            </div>
          ))}
        </section>

        <ProjectsSection onNewChat={() => p.onNewChat()} />

        <section aria-labelledby="sb-chats" className="mt-3">
          {inProject ? <ProjectHeader onNewChat={() => p.onNewChat()} /> : null}
          <h2 id="sb-chats" className={inProject ? "sr-only" : "text-muted-foreground px-2.5 pb-1 text-xs font-medium"}>{t("chats")}</h2>
          <ThreadListRoot>
            {hasThreads && <ThreadListSearch value={search} onValueChange={setSearch} data-testid="thread-search" />}
            {hasThreads && !searching && <InboxChips />}
            {!hasThreads && <p className="text-muted-foreground px-2.5 py-2 text-sm">{t("noChats")}</p>}
            {/* GĐ5a: full-text search on the server (titles + message text, accents optional); the list itself is not filtered */}
            {searching ? <SearchResults q={search.trim()} agents={p.agents} onOpen={() => setSearch("")} /> : <ThreadListItems />}
            {!searching && archivedN > 0 && (
              <details className="group/arch mt-2" data-testid="archived-section">
                <summary className="text-muted-foreground hover:bg-sidebar-accent flex cursor-pointer list-none items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium select-none">
                  <ChevronRightIcon className="size-3.5 transition-transform group-open/arch:rotate-90" aria-hidden /><ArchiveIcon className="size-3.5" aria-hidden />{t("archivedN", { count: archivedN })}
                </summary>
                <div className="mt-0.5 flex flex-col gap-0.5 opacity-85"><ArchivedThreadItems /></div>
              </details>)}
          </ThreadListRoot>
        </section>
      </div>

      <div className="border-sidebar-border border-t p-2">
        <Menu.Root>
          <Menu.Trigger data-testid="user-menu" aria-label={t("userMenu")}
            className="hover:bg-sidebar-accent data-[popup-open]:bg-sidebar-accent focus-visible:ring-ring/50 flex w-full items-center gap-2.5 rounded-lg px-2 py-1.5 text-start outline-none focus-visible:ring-2">
            <span aria-hidden className="bg-muted text-foreground flex size-8 items-center justify-center rounded-full text-xs font-semibold">{p.me.username.slice(0, 2).toUpperCase()}</span>
            <span className="min-w-0 flex-1"><span className="block truncate text-sm font-medium">{p.me.username}</span>
              <span className="text-muted-foreground block truncate text-xs">{p.me.role}</span></span>
          </Menu.Trigger>
          <Menu.Portal>
            <Menu.Positioner side="top" align="start" sideOffset={6} className="z-50">
              <Menu.Popup className="bg-popover text-popover-foreground data-[open]:animate-in data-[open]:fade-in-0 data-[open]:zoom-in-95 w-60 rounded-xl border p-1.5 shadow-lg outline-none">
                <MItem testid="nav-settings" onClick={p.onSettings}><SettingsIcon className="size-4" />{t("settings")}</MItem>
                <Menu.Separator className="bg-border my-1 h-px" />
                <div className="text-muted-foreground px-2.5 pt-1 pb-0.5 text-xs">{t("language")}</div>
                <MItem testid="lang-toggle" onClick={() => p.onLang(p.lang === "vi" ? "en" : "vi")}>
                  <LanguagesIcon className="size-4" />{p.lang === "vi" ? "Tiếng Việt → English" : "English → Tiếng Việt"}</MItem>
                <div className="text-muted-foreground px-2.5 pt-1 pb-0.5 text-xs">{t("theme")}</div>
                {([["light", SunIcon, t("themeLight")], ["dark", MoonIcon, t("themeDark")], ["system", MonitorIcon, t("themeSystem")]] as const).map(([v, I, label]) => (
                  <MItem key={v} testid={`theme-${v}`} onClick={() => p.onTheme(v)}><I className="size-4" />{label}{p.theme === v && <CheckIcon className="ms-auto size-4" />}</MItem>
                ))}
                <Menu.Separator className="bg-border my-1 h-px" />
                <MItem testid="sign-out" onClick={p.onSignOut}><LogOutIcon className="size-4" />{t("signOut")}</MItem>
              </Menu.Popup>
            </Menu.Positioner>
          </Menu.Portal>
        </Menu.Root>
      </div>
    </nav>
  );
}

function MItem({ children, onClick, testid }: { children: React.ReactNode; onClick: () => void; testid: string }) {
  return (
    <Menu.Item data-testid={testid} onClick={onClick}
      className="data-[highlighted]:bg-accent data-[highlighted]:text-accent-foreground flex cursor-default items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-sm outline-none select-none">
      {children}
    </Menu.Item>
  );
}

/** GĐ5a: server-side thread search (POST /api/chat/threads/search, Postgres FTS + unaccent): title, agent and a snippet per hit. */
function SearchResults({ q, agents, onOpen }: { q: string; agents: AgentProfile[]; onOpen: () => void }) {
  const { t } = useTranslation(); const aui = useAui();
  const [res, setRes] = useState<{ q: string; items: ThreadHit[]; error?: string } | null>(null);
  useEffect(() => {
    const ac = new AbortController();
    const tm = setTimeout(() => { searchThreads(q, 20, ac.signal).then((r) => setRes({ q, items: r.items })).catch((e) => { if ((e as Error).name !== "AbortError") setRes({ q, items: [], error: String((e as Error).message) }); }); }, 220);
    return () => { clearTimeout(tm); ac.abort(); };
  }, [q]);
  const byId = useMemo(() => new Map(agents.map((a) => [a.graph_id, a])), [agents]);
  if (!res || res.q !== q) return <div className="text-muted-foreground flex items-center gap-2 px-2.5 py-3 text-sm" role="status"><LoaderIcon className="size-3.5 animate-spin" />{t("searching")}</div>;
  if (res.error) return <p role="alert" className="text-destructive px-2.5 py-3 text-sm">{res.error}</p>;
  if (!res.items.length) return <p className="text-muted-foreground px-2.5 py-3 text-sm" data-testid="search-empty">{t("noResults")}</p>;
  return (
    <ul className="flex flex-col gap-0.5" data-testid="search-results" aria-label={t("searchResults")}>
      {res.items.map((h) => { const a = h.agent_id ? byId.get(h.agent_id) : undefined;
        return (
          <li key={h.thread_id}>
            <button type="button" data-testid="search-hit" data-thread={h.thread_id}
              onClick={() => { void Promise.resolve(aui.threads().switchToThread(h.thread_id, { unarchive: false })).catch(() => { location.hash = `#/c/${h.thread_id}`; }); onOpen(); }}
              className="hover:bg-sidebar-accent focus-visible:ring-ring/50 flex w-full flex-col gap-0.5 rounded-lg px-2.5 py-1.5 text-start outline-none focus-visible:ring-2">
              <span className="flex min-w-0 items-center gap-1.5 text-sm">
                {a && <AgentAvatar agent={a} size={16} />}
                <span className="min-w-0 flex-1 truncate font-medium">{h.title || t("untitled")}</span>
                {h.archived && <ArchiveIcon className="text-muted-foreground size-3.5 shrink-0" aria-label={t("archive")} />}
              </span>
              {h.snippet && <span className="text-muted-foreground line-clamp-2 text-xs leading-snug">{h.snippet}</span>}
            </button>
          </li>); })}
    </ul>
  );
}
