import { describe, expect, it } from "vitest";
import { agentIdOf, canSave, errorKey, lintErrors, lintText, skillFiles, skillSlug } from "../skill-save-helpers";
import i18n from "@/i18n";

const item = { code: "E6", msg: "Có vẻ chứa khoá", msg_en: "Looks like a key" };

describe("12.87 save as Skill dialog helpers", () => {
  it("agent id from the chat graph id", () => {
    expect(agentIdOf("agent-12")).toBe(12);
    for (const v of ["agent-", "agent-x", "assistant", "xagent-1", "", null, undefined]) expect(agentIdOf(v as never)).toBeNull();
  });
  it("name becomes a valid folder name (same rule as the server)", () => {
    expect(skillSlug("Báo giá nhanh")).toBe("bao-gia-nhanh");
    expect(skillSlug("Đặt hàng — Mẫu 1")).toBe("dat-hang-mau-1");
    expect(skillSlug("***")).toBe("");
    expect(skillSlug("a".repeat(80))).toHaveLength(64);
  });
  it("SKILL.md is rebuilt from the three fields and the extra files stay", () => {
    const f = skillFiles("Báo giá", 'Dùng khi: báo giá # "nhanh"', "# B\n1. Hỏi\n", { "references/gia.md": "A=1", "SKILL.md": "old" });
    expect(f["references/gia.md"]).toBe("A=1");
    expect(f["SKILL.md"]).toBe('---\nname: bao-gia\ndescription: "Dùng khi: báo giá # \\"nhanh\\""\n---\n\n# B\n1. Hỏi\n');
  });
  it("Save is on only with name, description, body and no lint error", () => {
    const ok = { errors: [], warnings: [item] };
    expect(canSave("bao-gia", "Dùng khi...", "# B", ok)).toBe(true);
    expect(canSave("bao-gia", "Dùng khi...", "# B", { errors: [item], warnings: [] })).toBe(false);
    expect(canSave("bao-gia", "Dùng khi...", "# B", null)).toBe(true);   // not checked yet: the server still blocks errors (422)
    expect(canSave("***", "d", "b", ok)).toBe(false); expect(canSave("x", "  ", "b", ok)).toBe(false); expect(canSave("x", "d", "", ok)).toBe(false);
    expect(lintErrors({ errors: [item, item], warnings: [] })).toBe(2);
  });
  it("lint text follows the language", () => {
    expect(lintText(item, "vi")).toBe("Có vẻ chứa khoá"); expect(lintText(item, "en-US")).toBe("Looks like a key");
    expect(lintText({ code: "W1", msg: "vi", msg_en: "" }, "en")).toBe("vi");
  });
  it("HTTP errors map to readable keys, and every key exists in vi and en", () => {
    const keys = [402, 403, 409, 422, 502].map((status) => errorKey({ status }));
    expect(keys).toEqual(["skillSaveCap", "skillSaveForbidden", "skillSaveTaken", "skillSaveLintBlocked", "skillSaveBadDraft"]);
    expect(errorKey({ status: 500 })).toBe(""); expect(errorKey(null)).toBe("");
    for (const lng of ["vi", "en"]) for (const k of [...keys, "skillSaveItem", "skillSaveTitle", "skillSaveDesc", "skillSaveDrafting", "skillSaveName", "skillSaveDescription", "skillSaveBody", "skillSaveSubmit", "skillSaveDone", "skillSaveFix"])
      expect(i18n.getFixedT(lng)(k), `${lng}:${k}`).not.toBe(k);
    expect(i18n.getFixedT("vi")("skillSaveCap")).toContain("hạn mức");
  });
});
