import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { createElement } from "react";
import { JSONGenerativeUI, renderGenerativeUI } from "@assistant-ui/generative-ui/react";
import { parseSources, safeHref } from "@/genui/sources";
import { library, COMPONENT_NAMES } from "@/genui/library";
import { adoptLocalVoiceIds } from "@/runtime/voice-dedupe";
import { remarkCitations } from "@/components/agentos/md/citations";
import serverSchema from "../present_schema.json";   // copied verbatim to Agent OS app/genui_schema.json

const WEB = "1. LangGraph documentation\n   https://docs.langchain.com/oss/python/langgraph/overview\n   LangGraph is a framework.\n2. assistant-ui\n   https://www.assistant-ui.com/docs\n   React chat.\n3. Bad\n   javascript:alert(1)\n   x";

describe("3.3 sources", () => {
  it("parses Agent OS web_search text into sources and drops non-http links", () => {
    const s = parseSources(WEB);
    expect(s.map((x) => x.n)).toEqual([1, 2]);
    expect(s[0]).toMatchObject({ title: "LangGraph documentation", domain: "docs.langchain.com", snippet: "LangGraph is a framework." });
    expect(s[1]!.domain).toBe("assistant-ui.com");
    expect(safeHref("javascript:alert(1)")).toBeUndefined();
    expect(parseSources("No results.")).toEqual([]);
  });
  it("remark plugin turns [n] into #cite-n links but not inside code", () => {
    const tree = { type: "root", children: [{ type: "paragraph", children: [{ type: "text", value: "Theo [1] và [2]." }, { type: "inlineCode", value: "[3]" }] }] };
    remarkCitations()(tree as never);
    const p = (tree.children[0] as { children: { type: string; url?: string; value?: string }[] }).children;
    expect(p.filter((x) => x.type === "link").map((x) => x.url)).toEqual(["#cite-1", "#cite-2"]);
    expect(p.at(-1)).toMatchObject({ type: "inlineCode", value: "[3]" });
  });
});

describe("3.1 generative UI", () => {
  it("UI library and server schema list the same components (one contract)", () => {
    expect([...COMPONENT_NAMES].sort()).toEqual([...(serverSchema as { components: string[] }).components].sort());
    const params = new JSONGenerativeUI({ library }).present().parameters as { properties: { _type: { enum: string[] } } };
    expect([...params.properties._type.enum].sort()).toEqual([...COMPONENT_NAMES].sort());
  });
  it("no external-request or action components are offered", () => {
    for (const bad of ["Image", "Map", "Button", "Form", "Input", "Carousel", "Icon", "Diagram"]) expect(COMPONENT_NAMES).not.toContain(bad);
  });
  it("renders Comparison, SpecSheet and Table to HTML; unknown types render nothing harmful", () => {
    const html = renderToStaticMarkup(createElement("div", null, renderGenerativeUI({ _type: "Col", children: [
      { _type: "Comparison", title: "So sánh", options: [{ name: "A", pros: ["Rẻ"] }, { name: "B", badge: "Khuyên dùng" }], criteria: [{ label: "Giá", values: ["1", "2"] }], verdict: "Chọn B" },
      { _type: "SpecSheet", title: "Spec", specs: [{ label: "RAM", value: "8 GB" }] },
      { _type: "Table", columns: [{ label: "Kênh" }], rows: [["Zalo"]] },
      { _type: "Script", src: "https://evil.example/x.js" },
    ] } as never, library)));
    expect(html).toContain("Khuyên dùng"); expect(html).toContain("8 GB"); expect(html).toContain("Zalo"); expect(html).toContain("Chọn B");
    expect(html).not.toContain("<script"); expect(html).not.toContain("evil.example");
  });
});

describe("fix (a) voice transcript dedupe", () => {
  const local = [{ id: "u1", role: "user" }, { id: "a1", role: "assistant" }, { id: "lv-u", role: "user", modality: "voice" }, { id: "lv-a", role: "assistant", modality: "voice" }];
  it("server copies of spoken turns adopt the local ids (no duplicate after refetch)", () => {
    const server = [{ id: "u1", type: "human", content: "hi" }, { id: "a1", type: "ai", content: "yo" },
      { id: "s-u", type: "human", content: "xin chào", additional_kwargs: { modality: "voice" } }, { id: "s-a", type: "ai", content: "chào anh", additional_kwargs: { modality: "voice" } }];
    const out = adoptLocalVoiceIds(server as never, local);
    expect(out.map((m) => m.id)).toEqual(["u1", "a1", "lv-u", "lv-a"]);
    expect(out[2]!.content).toBe("xin chào");   // server text wins
  });
  it("no-op when the server already has the local ids or nothing is spoken", () => {
    const server = [{ id: "u1", type: "human", content: "hi" }, { id: "lv-u", type: "human", content: "x", additional_kwargs: { modality: "voice" } }];
    expect(adoptLocalVoiceIds(server as never, [{ id: "u1", role: "user" }]).map((m) => m.id)).toEqual(["u1", "lv-u"]);
    expect(adoptLocalVoiceIds(server as never, [{ id: "u1", role: "user" }, { id: "lv-u", role: "user", modality: "voice" }])).toBe(server);
  });
  it("skips a local user turn that the server replaced by the delegated request (role-ordered pairing)", () => {
    const loc = [{ id: "v1", role: "assistant", modality: "voice" }, { id: "v2", role: "user", modality: "voice" }, { id: "v3", role: "assistant", modality: "voice" }];
    const server = [{ id: "x1", type: "ai", content: "a", additional_kwargs: { modality: "voice" } }, { id: "x3", type: "ai", content: "c", additional_kwargs: { modality: "voice" } }];
    expect(adoptLocalVoiceIds(server as never, loc).map((m) => m.id)).toEqual(["v1", "v3"]);
  });
});
