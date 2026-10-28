import { describe, expect, it } from "vitest";
import { agentNumId, nameMatches } from "../agent-danger-helpers";

describe("F-2 danger zone helpers", () => {
  it("reads the numeric id from agent-<n> only", () => {
    expect(agentNumId("agent-12")).toBe(12);
    for (const bad of ["agent-", "agent-x", "assistant", "agent-1a", ""]) expect(agentNumId(bad)).toBeNull();
  });
  it("arms permanent delete only with the exact name", () => {
    expect(nameMatches("Trợ lý", "Trợ lý")).toBe(true);
    for (const typed of ["trợ lý", "Trợ lý ", " Trợ lý", "", "Trợ"]) expect(nameMatches(typed, "Trợ lý")).toBe(false);
    expect(nameMatches("", "")).toBe(false);
  });
});
