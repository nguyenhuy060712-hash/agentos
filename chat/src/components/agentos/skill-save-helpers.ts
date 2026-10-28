// GĐ12.87: pure helpers of the "Save as Skill" dialog (name/description/body -> files, Save only without lint errors, readable errors).
import type { SkillLint, SkillLintItem } from "@/lib/agentos";

/** "agent-12" (the graph id of a chat agent) -> 12; anything else -> null. */
export const agentIdOf = (graphId?: string | null): number | null => { const m = /^agent-(\d+)$/.exec(graphId ?? ""); return m ? Number(m[1]) : null; };

/** Skill folder name: lowercase letters, digits, single hyphens, at most 64 characters ("" when nothing is left). Same rule as the server (`skills_mgmt.slug`). */
export const skillSlug = (s: string): string =>
  s.replace(/đ/g, "d").replace(/Đ/g, "D").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 64).replace(/-+$/, "");

/** The files to save: the draft's extra files stay, SKILL.md is rebuilt from the three edited fields (description quoted as JSON = valid YAML). */
export const skillFiles = (name: string, description: string, body: string, extra: Record<string, string> = {}): Record<string, string> => {
  const n = skillSlug(name), d = description.replace(/\s+/g, " ").trim().slice(0, 1024);
  const rest = Object.fromEntries(Object.entries(extra).filter(([p]) => p !== "SKILL.md"));
  return { ...rest, "SKILL.md": `---\nname: ${n}\ndescription: ${JSON.stringify(d)}\n---\n\n${body.trim()}\n` };
};

export const lintErrors = (l: SkillLint | null | undefined): number => l?.errors?.length ?? 0;
/** Save needs a name, a description, a body and no lint error. */
export const canSave = (name: string, description: string, body: string, lint: SkillLint | null | undefined): boolean =>
  !!skillSlug(name) && !!description.trim() && !!body.trim() && lintErrors(lint) === 0;
export const lintText = (i: SkillLintItem, lang: string): string => (lang.startsWith("en") ? i.msg_en : i.msg) || i.msg;

/** HTTP error -> i18n key (402 = spend cap, 403 = not allowed / demo, 409 = name taken, 422 = lint, 502 = bad draft); "" = show the server text. */
export const errorKey = (e: unknown): string => {
  switch ((e as { status?: number })?.status) {
    case 402: return "skillSaveCap"; case 403: return "skillSaveForbidden"; case 409: return "skillSaveTaken";
    case 422: return "skillSaveLintBlocked"; case 502: return "skillSaveBadDraft"; default: return "";
  }
};
