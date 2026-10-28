// GĐ5a unit tests: @mention formatter (plain "@Tên agent" in the textarea, chips + routing target from it), ask-card result parsing, memory key.
import { describe, expect, it, beforeAll } from "vitest";
import { agentFormatter, firstAgentMention, mentionAgents } from "@/runtime/mentions";
import { isAsk, memKey, parseAnswered } from "@/genui/gd5a-helpers";

beforeAll(() => mentionAgents.set([{ id: "agent-1", label: "Trợ lý nhà" }, { id: "agent-7", label: "Trợ lý nhà bếp" }, { id: "agent-2", label: "Moza Marketing" }]));

describe("agentFormatter", () => {
  it("serializes an agent as plain @Label and other items as directives", () => {
    expect(agentFormatter.serialize({ id: "agent-2", type: "agent", label: "Moza Marketing" })).toBe("@Moza Marketing");
    expect(agentFormatter.serialize({ id: "x", type: "tool", label: "X" })).toBe(":tool[X]{name=x}");
  });
  it("parses known agents, longest label first, case-insensitive, word boundary", () => {
    const s = agentFormatter.parse("Nhờ @trợ lý nhà bếp và @Moza Marketing, còn @Moza Marketingxyz thì không");
    const m = s.filter((x) => x.kind === "mention");
    expect(m.map((x) => x.kind === "mention" && x.id)).toEqual(["agent-7", "agent-2"]);
    expect(s.map((x) => (x.kind === "text" ? x.text : `<${x.label}>`)).join("")).toBe("Nhờ <Trợ lý nhà bếp> và <Moza Marketing>, còn @Moza Marketingxyz thì không");
  });
  it("ignores e-mail-like @ and unknown names; keeps plain text intact", () => {
    expect(agentFormatter.parse("mail huy@Trợ lý nhà")).toEqual([{ kind: "text", text: "mail huy@Trợ lý nhà" }]);
    expect(agentFormatter.parse("@Ai đó")).toEqual([{ kind: "text", text: "@Ai đó" }]);
  });
  it("also understands directive syntax from other clients", () => {
    expect(firstAgentMention(":agent[Trợ lý nhà]{name=agent-1} kiểm tra đèn")).toEqual({ id: "agent-1", label: "Trợ lý nhà" });
    expect(firstAgentMention("không nhắc ai")).toBeNull();
    expect(firstAgentMention("@Trợ lý nhà bật đèn")?.id).toBe("agent-1");
  });
});

describe("ask card", () => {
  it("recognises the interrupt value", () => {
    expect(isAsk({ type: "ask_user", fields: [] })).toBe(true);
    expect(isAsk({ action_requests: [] })).toBe(false);
  });
  it("parses the answered tool result into label/value pairs", () => {
    expect(parseAnswered('Người dùng đã trả lời: {"Ngân sách (triệu đồng)": 20.0, "Kênh chính": "TikTok", "Đối tượng": ["Sinh viên", "Văn phòng"], "Gửi báo cáo": true}'))
      .toEqual([["Ngân sách (triệu đồng)", "20"], ["Kênh chính", "TikTok"], ["Đối tượng", "Sinh viên, Văn phòng"], ["Gửi báo cáo", "✓"]]);
    expect(parseAnswered("Người dùng không trả lời biểu mẫu.")).toBeNull();
    expect(parseAnswered(undefined)).toBeNull();
  });
});

describe("memory chip", () => {
  it("reads the memory key from the tool result", () => {
    expect(memKey("Đã lưu. [mem:3f9a0c1b2d4e]")).toBe("3f9a0c1b2d4e");
    expect(memKey("Đã lưu.")).toBeUndefined();
  });
});
