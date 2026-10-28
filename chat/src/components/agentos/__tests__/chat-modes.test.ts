import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import i18n from "@/i18n";
import { ModelPicker } from "../ModelPicker";
import { ResearchChip } from "../ComposerPlus";
import { ChatUiContext, type ChatUi } from "../ui-context";
import { MODE_KEY, modesOf, readPicks, resolveMode, writePick } from "@/lib/chat-modes";
import { researchStore, RESEARCH_MODE, withConfigurable } from "@/lib/research";

const modes = [{ id: "fast", model: "gemini-3.1-flash-lite" }, { id: "standard", model: "gemini-3.6-flash" }, { id: "deep", model: "gemini-3.8-flash" }];
const mem = () => { const m = new Map<string, string>(); return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v) }; };
const ui = (o: Partial<ChatUi>): ChatUi => ({ agent: {} as never, chatModels: [], chatModel: null, onChatModel: () => {}, voiceReady: false, voiceModel: null, cancelDictation: () => {}, agents: [], newChat: () => {}, ...o });
const render = (o: Partial<ChatUi>) => renderToStaticMarkup(createElement(ChatUiContext.Provider, { value: ui(o) }, createElement(ModelPicker)));

describe("chat modes", () => {
  it("offers the picker only for >= 2 modes", () => {
    expect(modesOf({ modes })).toHaveLength(3);
    expect(modesOf({ modes: modes.slice(0, 1) })).toEqual([]);
    expect(modesOf({})).toEqual([]);
  });
  it("default = default_mode; a remembered pick wins; a pick no longer offered falls back", () => {
    expect(resolveMode({ modes, default_mode: "standard" }, {}, "a1")).toBe("standard");
    expect(resolveMode({ modes, default_mode: "standard" }, { a1: "deep" }, "a1")).toBe("deep");
    expect(resolveMode({ modes, default_mode: "standard" }, { a1: "turbo" }, "a1")).toBe("standard");
    expect(resolveMode({ modes: modes.slice(0, 2) }, { a1: "deep" }, "a1")).toBe("fast");
    expect(resolveMode({}, {}, "a1")).toBeNull();
  });
  it("remembers the choice per agent in localStorage (and survives a broken store)", () => {
    const s = mem();
    writePick("a1", "deep", s); writePick("a2", "fast", s);
    expect(readPicks(s)).toEqual({ a1: "deep", a2: "fast" });
    expect(JSON.parse(s.getItem(MODE_KEY)!)).toEqual({ a1: "deep", a2: "fast" });
    const bad = { getItem: () => { throw new Error("x"); }, setItem: () => { throw new Error("x"); } };
    expect(readPicks(bad)).toEqual({});
    expect(writePick("a1", "fast", bad)).toEqual({ a1: "fast" });
  });
  it("is sent as configurable.chat_mode, merged with the research mode", () => {
    const cfg = withConfigurable({ runConfig: { configurable: { thread_id: "t" } } }, { chat_mode: "deep", mode: RESEARCH_MODE });
    expect(cfg.runConfig.configurable).toEqual({ thread_id: "t", chat_mode: "deep", mode: "deep_research" });
  });
  it("renders the mode button with the current label (vi + en), not the raw model select", async () => {
    await i18n.changeLanguage("vi");
    let h = render({ chatModes: modes, chatMode: "standard", chatModels: [{ id: "m1", provider: "g" }, { id: "m2", provider: "g" }] });
    expect(h).toContain('data-testid="mode-picker"'); expect(h).toContain("Tiêu chuẩn"); expect(h).not.toContain("model-picker");
    await i18n.changeLanguage("en");
    h = render({ chatModes: modes, chatMode: "deep" });
    expect(h).toContain("Thinking"); expect(h).toContain("Chat mode");
  });
  it("falls back to the model picker without modes (old server), and hides with <2 models", () => {
    const h = render({ chatModels: [{ id: "m1", provider: "g" }, { id: "m2", provider: "g" }], chatModel: "m1" });
    expect(h).toContain('data-testid="model-picker"'); expect(h).not.toContain("mode-picker");
    expect(render({ chatModes: [], chatModels: [{ id: "m1", provider: "g" }] })).toBe("");
  });
});

describe("composer + menu", () => {
  it("research toggles per chat through the store; the chip is removable", () => {
    researchStore.set("t1", false); expect(researchStore.get("t1")).toBe(false);
    researchStore.set("t1", true); expect(researchStore.get("t1")).toBe(true);
    let removed = 0;
    const h = renderToStaticMarkup(createElement(ResearchChip, { onRemove: () => { removed++; } }));
    expect(h).toContain('data-testid="research-chip"'); expect(h).toContain('data-testid="research-chip-remove"'); expect(h).toContain("aria-label=");
    expect(removed).toBe(0);
    researchStore.set("t1", false); expect(researchStore.get("t1")).toBe(false);
  });
});
