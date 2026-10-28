// GĐ8.9 "Dự án" in the chat sidebar (like ChatGPT / Claude projects): list, create, rename, delete; opening a project shows only its chats
// and "New chat in project"; the settings dialog edits the project instructions and files (6.8 upload -> inline or searched by chunk).
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Dialog } from "@base-ui/react/dialog";
import { Menu } from "@base-ui/react/menu";
import { BrainIcon, FileTextIcon, FolderIcon, FolderOpenIcon, FolderPlusIcon, LoaderIcon, MoreHorizontalIcon, PencilIcon, SettingsIcon, SquarePenIcon, TrashIcon, UploadIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { addProjectFile, createProject, deleteProject, deleteProjectMemory, getProject, listProjectMemories, listProjects, patchProject, removeProjectFile, uploadFile, type Project, type ProjectMemory } from "@/lib/agentos";
import { projectNameError, projectsStore, useProjects } from "@/lib/projectsStore";
import { DOC_ACCEPT, formatSize } from "@/runtime/attachments";

const ROW = "hover:bg-sidebar-accent focus-visible:ring-ring/50 flex w-full items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-start text-sm outline-none focus-visible:ring-2";
const MI = "data-[highlighted]:bg-accent data-[highlighted]:text-accent-foreground flex cursor-default items-center gap-2.5 rounded-lg px-2.5 py-1.5 text-sm outline-none select-none";

type Edit = { id: number | null; confirmDelete?: boolean } | null;

export type ProjectListViewProps = { projects: Project[]; active: number | null; renaming: number | null;
  onOpen: (id: number) => void; onCreate: () => void; onRename: (id: number | null) => void; onCommitRename: (id: number, name: string) => void;
  onSettings: (id: number) => void; onDelete: (id: number) => void };

/** pure list (rendered in tests) */
export function ProjectListView(p: ProjectListViewProps) {
  const { t } = useTranslation();
  return (
    <section aria-labelledby="sb-projects" className="mt-3" data-testid="projects-section">
      <h2 id="sb-projects" className="text-muted-foreground px-2.5 pb-1 text-xs font-medium">{t("projects")}</h2>
      <button type="button" data-testid="project-new" onClick={p.onCreate} className={ROW}><FolderPlusIcon className="size-4" aria-hidden />{t("projectNew")}</button>
      <ul className="flex flex-col gap-0.5" data-testid="project-list">
        {p.projects.map((x) => (
          <li key={x.id} className="group relative" data-testid="project-item" data-project={x.id}>
            {p.renaming === x.id ? (
              <RenameInput name={x.name} onDone={(v) => (v == null ? p.onRename(null) : p.onCommitRename(x.id, v))} />
            ) : (
              <button type="button" onClick={() => p.onOpen(x.id)} aria-current={p.active === x.id ? "true" : undefined}
                className={cn(ROW, "pe-8", p.active === x.id && "bg-sidebar-accent font-medium")}>
                {p.active === x.id ? <FolderOpenIcon className="size-4 shrink-0" aria-hidden /> : <FolderIcon className="size-4 shrink-0" aria-hidden />}
                <span className="min-w-0 flex-1 truncate">{x.name}</span>
              </button>)}
            {p.renaming !== x.id && (
              <Menu.Root>
                <Menu.Trigger aria-label={t("more")} data-testid="project-more"
                  className="hover:bg-accent data-[popup-open]:bg-accent absolute end-1.5 top-1/2 flex size-6 -translate-y-1/2 items-center justify-center rounded-md opacity-0 group-hover:opacity-100 focus-visible:opacity-100 data-[popup-open]:opacity-100">
                  <MoreHorizontalIcon className="size-3.5" /></Menu.Trigger>
                <Menu.Portal><Menu.Positioner side="right" align="start" sideOffset={6} className="z-50">
                  <Menu.Popup className="bg-popover text-popover-foreground min-w-40 rounded-xl border p-1.5 shadow-lg outline-none">
                    <Menu.Item className={MI} data-testid="project-rename" onClick={() => p.onRename(x.id)}><PencilIcon className="size-4" />{t("rename")}</Menu.Item>
                    <Menu.Item className={MI} data-testid="project-settings" onClick={() => p.onSettings(x.id)}><SettingsIcon className="size-4" />{t("projectSettings")}</Menu.Item>
                    <Menu.Item className={cn(MI, "text-destructive")} data-testid="project-delete" onClick={() => p.onDelete(x.id)}><TrashIcon className="size-4" />{t("delete")}</Menu.Item>
                  </Menu.Popup></Menu.Positioner></Menu.Portal>
              </Menu.Root>)}
          </li>))}
      </ul>
    </section>);
}

function RenameInput({ name, onDone }: { name: string; onDone: (v: string | null) => void }) {
  const { t } = useTranslation(); const [v, setV] = useState(name); const done = useRef(false);
  const finish = (x: string | null) => { if (done.current) return; done.current = true; onDone(x); };
  return <input autoFocus data-testid="project-rename-input" aria-label={t("rename")} value={v} maxLength={100} onChange={(e) => setV(e.target.value)}
    onBlur={() => finish(projectNameError(v) ? null : v.trim())}
    onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); finish(projectNameError(v) ? null : v.trim()); } else if (e.key === "Escape") { e.preventDefault(); finish(null); } }}
    className="bg-background w-full rounded-lg border px-2.5 py-1.5 text-sm outline-none" />;
}

/** sidebar section: loads the projects once, opening one starts a new chat inside it */
export function ProjectsSection({ onNewChat }: { onNewChat: () => void }) {
  const ps = useProjects(); const [renaming, setRenaming] = useState<number | null>(null); const [edit, setEdit] = useState<Edit>(null);
  useEffect(() => { listProjects().then((r) => projectsStore.setProjects(r.items, r.read_only)).catch(() => projectsStore.setProjects([], true)); }, []);
  if (ps.readOnly) return null;   // demo account / demo workspace: feature off
  const open = (id: number) => { projectsStore.open(id); onNewChat(); };
  const rename = async (id: number, name: string) => { setRenaming(null); try { projectsStore.upsert(await patchProject(id, { name })); } catch { /* keep old name */ } };
  return (
    <>
      <ProjectListView projects={ps.projects} active={ps.active} renaming={renaming} onOpen={open} onCreate={() => setEdit({ id: null })}
        onRename={setRenaming} onCommitRename={rename} onSettings={(id) => setEdit({ id })} onDelete={(id) => setEdit({ id, confirmDelete: true })} />
      {edit && <ProjectDialog id={edit.id} confirmDelete={edit.confirmDelete} onClose={() => setEdit(null)} onCreated={(p) => { projectsStore.open(p.id); onNewChat(); }} />}
    </>);
}

/** heading of the chat list while a project is open: name, New chat in project, settings, close */
export function ProjectHeader({ onNewChat }: { onNewChat: () => void }) {
  const { t } = useTranslation(); const ps = useProjects(); const [edit, setEdit] = useState(false);
  const p = ps.projects.find((x) => x.id === ps.active);
  if (!p) return null;
  return (
    <div className="mb-1" data-testid="project-header">
      <div className="flex items-center gap-1.5 px-2.5 pb-1">
        <FolderOpenIcon className="size-3.5 shrink-0" aria-hidden />
        <h2 className="min-w-0 flex-1 truncate text-xs font-semibold" data-testid="project-open-name">{p.name}</h2>
        <button type="button" aria-label={t("projectSettings")} title={t("projectSettings")} data-testid="project-open-settings" onClick={() => setEdit(true)} className="hover:bg-sidebar-accent rounded-md p-1"><SettingsIcon className="size-3.5" /></button>
        <button type="button" aria-label={t("projectClose")} title={t("projectClose")} data-testid="project-close" onClick={() => projectsStore.open(null)} className="hover:bg-sidebar-accent rounded-md p-1"><XIcon className="size-3.5" /></button>
      </div>
      <button type="button" data-testid="project-new-chat" onClick={onNewChat} className={ROW}><SquarePenIcon className="size-4" aria-hidden />{t("projectNewChat")}</button>
      {edit && <ProjectDialog id={p.id} onClose={() => setEdit(false)} />}
    </div>);
}

/** GĐ8.P: the project's memories (pure, rendered in tests): text + forget button */
export function ProjectMemoryList({ items, busy, onForget }: { items: ProjectMemory[] | null; busy?: boolean; onForget: (key: string) => void }) {
  const { t } = useTranslation();
  return (
    <section aria-labelledby="pm-h" className="space-y-2" data-testid="project-memory">
      <h3 id="pm-h" className="flex items-center gap-2 font-medium"><BrainIcon className="size-4" aria-hidden />{t("projectMemory")}</h3>
      <p className="text-muted-foreground text-xs">{t("projectMemoryHint")}</p>
      {items == null ? <LoaderIcon className="size-4 animate-spin" /> : !items.length ? <p className="text-muted-foreground" data-testid="project-memory-empty">{t("projectNoMemory")}</p> : (
        <ul className="divide-y rounded-lg border" data-testid="project-memories">{items.map((m) => (
          <li key={m.key} className="flex items-start gap-2 px-3 py-2" data-testid="project-memory-item">
            <span className="min-w-0 flex-1 break-words whitespace-pre-wrap">{m.text}</span>
            <Button size="sm" variant="ghost" className="text-destructive" aria-label={t("memForget")} title={t("memForget")} data-testid="project-memory-delete" disabled={busy} onClick={() => onForget(m.key)}><TrashIcon className="size-4" /></Button>
          </li>))}</ul>)}
    </section>);
}

/** create (id null) or edit a project: name, instructions, files, memories; delete with a confirm step */
export function ProjectDialog({ id, confirmDelete, onClose, onCreated }: { id: number | null; confirmDelete?: boolean; onClose: () => void; onCreated?: (p: Project) => void }) {
  const { t } = useTranslation();
  const [p, setP] = useState<Project | null>(id == null ? { id: 0, name: "", instructions: "", files: [] } : null);
  const [name, setName] = useState(""); const [instr, setInstr] = useState("");
  const [busy, setBusy] = useState(false); const [err, setErr] = useState(""); const [msg, setMsg] = useState(""); const [del, setDel] = useState(!!confirmDelete);
  const fileRef = useRef<HTMLInputElement>(null); const [mems, setMems] = useState<ProjectMemory[] | null>(null);
  useEffect(() => { if (id != null) getProject(id).then((x) => { setP(x); setName(x.name); setInstr(x.instructions); }).catch((e) => setErr(String((e as Error).message))); }, [id]);
  useEffect(() => { if (id != null) listProjectMemories(id).then((r) => setMems(r.items)).catch(() => setMems([])); }, [id]);
  const run = async (f: () => Promise<void>) => { setBusy(true); setErr(""); setMsg(""); try { await f(); } catch (e) { setErr(String((e as Error).message)); } finally { setBusy(false); } };
  const nameErr = projectNameError(name);
  const save = () => run(async () => {
    if (p && p.id) { const x = await patchProject(p.id, { name: name.trim(), instructions: instr }); setP(x); projectsStore.upsert(x); setMsg(t("saved")); }
    else { const x = await createProject({ name: name.trim(), instructions: instr }); setP(x); projectsStore.upsert(x); onCreated?.(x); setMsg(t("projectCreated")); }
  });
  const upload = (files: FileList | null) => run(async () => {
    for (const f of Array.from(files ?? [])) {
      const u = await uploadFile(f);
      if (u.status && u.status !== "ok") throw new Error(`${u.name}: ${u.reason ?? ""}`);
      const x = await addProjectFile(p!.id, u.id); setP(x); projectsStore.upsert({ id: x.id, name: x.name, instructions: x.instructions, n_files: x.files?.length });
    }
    if (fileRef.current) fileRef.current.value = "";
  });
  const forget = (key: string) => run(async () => { await deleteProjectMemory(p!.id, key); setMems((m) => (m ?? []).filter((x) => x.key !== key)); setMsg(t("memForgotten")); });
  const remove = (fid: string) => run(async () => { const x = await removeProjectFile(p!.id, fid); setP(x); });
  const destroy = () => run(async () => { await deleteProject(p!.id); projectsStore.remove(p!.id); onClose(); });
  const dirty = !!p && (name.trim() !== p.name || instr !== p.instructions);
  return (
    <Dialog.Root open onOpenChange={(o) => { if (!o) onClose(); }}>
      <Dialog.Portal>
        <Dialog.Backdrop className="fixed inset-0 z-40 bg-black/30 backdrop-blur-[2px]" />
        <Dialog.Popup data-testid="project-dialog" className="bg-background text-foreground fixed inset-x-3 top-[6dvh] z-50 mx-auto max-h-[88dvh] max-w-xl overflow-auto rounded-2xl border p-5 shadow-2xl outline-none">
          <div className="flex items-center gap-2">
            <FolderIcon className="size-4" /><Dialog.Title className="text-base font-semibold">{p && p.id ? t("projectSettings") : t("projectNew")}</Dialog.Title>
            <Dialog.Close aria-label={t("close")} className="hover:bg-muted ms-auto rounded-lg p-1.5"><XIcon className="size-4" /></Dialog.Close>
          </div>
          <Dialog.Description className="text-muted-foreground mt-1 text-sm">{t("projectDesc")}</Dialog.Description>
          {!p ? <LoaderIcon className="mt-4 size-4 animate-spin" /> : (
            <div className="mt-4 space-y-4 text-sm">
              <label className="block space-y-1.5"><span className="font-medium">{t("projectName")}</span>
                <input data-testid="project-name" autoFocus={!p.id} value={name} maxLength={100} onChange={(e) => setName(e.target.value)} placeholder={t("projectNamePh")}
                  className="bg-background w-full rounded-lg border px-2.5 py-1.5 text-sm" /></label>
              <label className="block space-y-1.5"><span className="font-medium">{t("projectInstructions")}</span>
                <textarea data-testid="project-instructions" value={instr} maxLength={8000} onChange={(e) => setInstr(e.target.value)} placeholder={t("projectInstructionsPh")}
                  className="bg-background min-h-28 w-full rounded-lg border px-2.5 py-1.5 text-sm" />
                <span className="text-muted-foreground block text-end text-xs">{t("persCount", { n: instr.length, max: 8000 })}</span></label>
              <div className="flex items-center gap-3">
                <Button size="sm" data-testid="project-save" onClick={save} disabled={busy || !!nameErr || (!!p.id && !dirty)}>{busy && <LoaderIcon className="size-4 animate-spin" />}{p.id ? t("save") : t("projectCreate")}</Button>
                <span className="text-muted-foreground text-sm" role="status" data-testid="project-msg">{msg}</span>
              </div>
              {!!p.id && (
                <section aria-labelledby="pf-h" className="space-y-2">
                  <div className="flex items-center gap-2"><h3 id="pf-h" className="font-medium">{t("projectFiles")}</h3>
                    <Button size="sm" variant="outline" className="ms-auto" data-testid="project-upload" disabled={busy} onClick={() => fileRef.current?.click()}><UploadIcon className="size-4" />{t("projectAddFiles")}</Button>
                    <input ref={fileRef} type="file" multiple accept={DOC_ACCEPT} hidden data-testid="project-file-input" onChange={(e) => upload(e.target.files)} /></div>
                  <p className="text-muted-foreground text-xs">{t("projectFilesHint")}</p>
                  {!p.files?.length ? <p className="text-muted-foreground" data-testid="project-files-empty">{t("projectNoFiles")}</p> : (
                    <ul className="divide-y rounded-lg border" data-testid="project-files">{p.files.map((f) => (
                      <li key={f.id} className="flex items-center gap-2 px-3 py-2" data-testid="project-file">
                        <FileTextIcon className="text-muted-foreground size-4 shrink-0" />
                        <span className="min-w-0 flex-1 truncate">{f.name}</span>
                        <span className="text-muted-foreground text-xs">{formatSize(f.size)}</span>
                        <span className="bg-muted rounded-full px-2 py-0.5 text-xs">{f.mode === "inline" ? t("projectModeInline") : t("projectModeSearch")}</span>
                        <Button size="sm" variant="ghost" aria-label={t("remove")} data-testid="project-file-remove" onClick={() => remove(f.id)}><XIcon className="size-4" /></Button>
                      </li>))}</ul>)}
                </section>)}
              {!!p.id && <ProjectMemoryList items={mems} busy={busy} onForget={forget} />}
              {!!p.id && (
                <div className="border-t pt-3">
                  {!del ? <Button size="sm" variant="ghost" className="text-destructive" data-testid="project-delete-start" onClick={() => setDel(true)}><TrashIcon className="size-4" />{t("projectDelete")}</Button> : (
                    <div className="flex flex-wrap items-center gap-2" data-testid="project-delete-confirm">
                      <span>{t("projectDeleteConfirm")}</span>
                      <Button size="sm" variant="destructive" data-testid="project-delete-yes" disabled={busy} onClick={destroy}>{t("delete")}</Button>
                      <Button size="sm" variant="ghost" onClick={() => setDel(false)}>{t("cancel")}</Button>
                    </div>)}
                </div>)}
              {err && <p role="alert" className="text-destructive text-xs">{err}</p>}
            </div>)}
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>);
}
