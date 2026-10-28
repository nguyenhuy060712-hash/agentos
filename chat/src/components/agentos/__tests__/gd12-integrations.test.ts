import { describe, expect, it } from "vitest";
import { INTEGRATIONS_URL, safeConnectUrl } from "../connect-helpers";
import i18n from "@/i18n";

describe("12.85 integrations entry", () => {
  it("links to the server Settings section, a path the chat accepts", () => {
    expect(INTEGRATIONS_URL).toBe("/settings?s=integrations");
    expect(safeConnectUrl(INTEGRATIONS_URL)).toBe(INTEGRATIONS_URL);
  });
  it("has the tab label in vi and en", () => {
    expect(i18n.getFixedT("en")("tabIntegrations")).toBe("Integrations");
    expect(i18n.getFixedT("vi")("tabIntegrations")).toBe("Tích hợp");
  });
});
