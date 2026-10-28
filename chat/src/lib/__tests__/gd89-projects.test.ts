import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {   // lib/agentos.ts reads window.location at import; the store reads localStorage
  const mem = new Map<string, string>();
  Object.assign(globalThis, { window: { location: { origin: "http://localhost" } },
    localStorage: { getItem: (k: string) => mem.get(k) ?? null, setItem: (k: string, v: string) => void mem.set(k, v), removeItem: (k: string) => void mem.delete(k) } });
});
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import "@/i18n";
import i18n from "@/i18n";
import { projectNameError, projectOfThread, projectsStore, showInList } from "../projectsStore";
import { createProject, moveThreadToProject, removeProjectFile } from "../agentos";
import { ProjectListView, type ProjectListViewProps } from "@/components/agentos/ProjectsPanel";

const P = (id: number, name: string) => ({ id, name, instructions: "" });
beforeEach(() => { projectsStore.reset(); localStorage.removeItem("aos_project"); });
afterEach(() => { vi.unstubAllGlobals(); });

describe("GĐ8.9 projects store", () => {
  it("lists a chat only in its own project, chats without a project only in the normal list", () => {
    projectsStore.seed([{ thread_id: "t1", project_id: 7 }, { thread_id: "t2", project_id: null }, { thread_id: "t3" }]);
    const { byThread } = projectsStore.get();
    expect(projectOfThread(byThread, "t1")).toBe(7); expect(projectOfThread(byThread, "t2")).toBeNull(); expect(projectOfThread(byThread, "t3")).toBeNull();
    expect(projectOfThread(byThread, "unknown", { project: 9 })).toBe(9);   // fallback: adapter custom
    expect(showInList(7, 7)).toBe(true); expect(showInList(7, null)).toBe(false); expect(showInList(null, null)).toBe(true); expect(showInList(null, 7)).toBe(false);
  });
  it("moving a chat out puts it back in the normal list; deleting a project releases its chats and closes it", () => {
    projectsStore.setProjects([P(1, "A"), P(2, "B")]); projectsStore.open(1);
    expect(localStorage.getItem("aos_project")).toBe("1");
    projectsStore.assign("t1", 1); projectsStore.assign("t2", 2);
    projectsStore.assign("t1", null); expect(showInList(projectOfThread(projectsStore.get().byThread, "t1"), null)).toBe(true);
    projectsStore.assign("t1", 1); projectsStore.remove(1);
    const s = projectsStore.get();
    expect(s.active).toBeNull(); expect(s.byThread.t1).toBeNull(); expect(s.byThread.t2).toBe(2); expect(s.projects.map((p) => p.id)).toEqual([2]);
    expect(localStorage.getItem("aos_project")).toBeNull();
  });
  it("an open project that no longer exists is closed when the list loads; upsert adds then updates", () => {
    projectsStore.open(5); projectsStore.setProjects([P(1, "A")]); expect(projectsStore.get().active).toBeNull();
    projectsStore.upsert(P(3, "C")); expect(projectsStore.get().projects[0].name).toBe("C");
    projectsStore.upsert({ ...P(3, "C2"), n_files: 2 }); expect(projectsStore.get().projects.find((p) => p.id === 3)).toMatchObject({ name: "C2", n_files: 2 });
  });
  it("name validation", () => {
    expect(projectNameError("  ")).toBe("projectNameEmpty"); expect(projectNameError("x".repeat(101))).toBe("projectNameLong"); expect(projectNameError(" Moza ")).toBe("");
  });
});

describe("GĐ8.9 projects API calls", () => {
  const ok = (body: unknown) => vi.fn(async () => new Response(JSON.stringify(body), { status: 200 }));
  it("create / move / remove file hit the right endpoints with the right bodies", async () => {
    let f = ok({ id: 1, name: "A", instructions: "" }); vi.stubGlobal("fetch", f);
    await createProject({ name: "A", instructions: "x" });
    let [url, init] = f.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/chat/projects"); expect(init.method).toBe("POST"); expect(JSON.parse(init.body as string)).toEqual({ name: "A", instructions: "x" });
    f = ok({}); vi.stubGlobal("fetch", f);
    await moveThreadToProject("t1", null);
    [url, init] = f.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/chat/projects/move"); expect(JSON.parse(init.body as string)).toEqual({ thread_id: "t1", project_id: null });
    f = ok({}); vi.stubGlobal("fetch", f);
    await removeProjectFile(4, "abc");
    [url, init] = f.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/chat/projects/4/files/abc"); expect(init.method).toBe("DELETE");
  });
  it("server error detail becomes the error message", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "Không có dự án" }), { status: 404 })));
    await expect(moveThreadToProject("t1", 99)).rejects.toThrow("Không có dự án");
  });
});

describe("GĐ8.9 sidebar list rendering", () => {
  const noop = () => {};
  const base: ProjectListViewProps = { projects: [P(1, "Quán cà phê Moza"), P(2, "Kế toán")], active: 1, renaming: null,
    onOpen: noop, onCreate: noop, onRename: noop, onCommitRename: noop, onSettings: noop, onDelete: noop };
  it("shows the section title, New project and every project, the open one marked", async () => {
    await i18n.changeLanguage("vi");
    const h = renderToStaticMarkup(createElement(ProjectListView, base));
    expect(h).toContain("Dự án"); expect(h).toContain("Dự án mới"); expect(h).toContain("Quán cà phê Moza"); expect(h).toContain("Kế toán");
    expect(h.match(/aria-current="true"/g)!.length).toBe(1);
    await i18n.changeLanguage("en");
    const e = renderToStaticMarkup(createElement(ProjectListView, base));
    expect(e).toContain("Projects"); expect(e).toContain("New project");
    await i18n.changeLanguage("vi");
  });
  it("rename mode shows the input instead of the row", () => {
    const h = renderToStaticMarkup(createElement(ProjectListView, { ...base, renaming: 2 }));
    expect(h).toContain('data-testid="project-rename-input"'); expect(h).toContain('value="Kế toán"');
  });
});
