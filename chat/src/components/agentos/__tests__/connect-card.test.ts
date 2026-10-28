import { describe, expect, it } from "vitest";
import { SKIP_RESUME, isAuthRequired, isConnectInterrupt, isCredentialGate, itemsOf, nameFromMessage, pauseKey, resumeAfter, rowStateOf, safeConnectUrl, secretSlug, type CredentialGate } from "../connect-helpers";
import i18n from "@/i18n";

const gate: CredentialGate = { type: "credential_authorization_required", credentials: [
  { slug: "mcp-notion", kind: "mcp_oauth", display_name: "Notion", reason: "not_connected", connect_url: "/api/agent-auth/sessions/aaa/start", auth_id: "aaa" },
  { slug: "mcp-linear", kind: "oauth2", display_name: "Linear", reason: "needs_reconnect", connect_url: "/api/agent-auth/sessions/bbb/start", auth_id: "bbb" }] };

describe("11.29 connect card helpers", () => {
  it("recognises both pauses and nothing else", () => {
    expect(isAuthRequired({ type: "auth_required", auth_url: "/x" })).toBe(true);
    expect(isCredentialGate(gate)).toBe(true);
    expect(isCredentialGate({ type: "credential_authorization_required" })).toBe(false);   // no list: not a gate
    for (const v of [null, undefined, "auth_required", { type: "ask_user" }, { action_requests: [] }]) expect(isConnectInterrupt(v)).toBe(false);
  });
  it("gate: one row per missing connection, in order, with its own link", () => {
    const it = itemsOf(gate);
    expect(it.map((i) => [i.slug, i.name, i.authId, i.reason])).toEqual([["mcp-notion", "Notion", "aaa", "not_connected"], ["mcp-linear", "Linear", "bbb", "needs_reconnect"]]);
    expect(it[0].url).toBe("/api/agent-auth/sessions/aaa/start");
  });
  it("auth_required: one row named from the server sentence; a secret has no sign-in", () => {
    const a = itemsOf({ type: "auth_required", tool: "notion:search", auth_url: "/api/agent-auth/sessions/zzz/start", auth_id: "zzz", message: "Chưa kết nối Notion / Not connected to Notion" });
    expect(a).toHaveLength(1); expect(a[0]).toMatchObject({ name: "Notion", secret: false, authId: "zzz" });
    const s = itemsOf({ type: "auth_required", auth_url: "/settings?s=connections&service=crm", auth_id: "secret:crm", message: "Chưa kết nối CRM / Not connected to CRM" });
    expect(s[0]).toMatchObject({ slug: "crm", secret: true, name: "CRM" });
    expect(secretSlug("secret:crm")).toBe("crm"); expect(secretSlug("abc")).toBe("");
  });
  it("names the service from either language of the server sentence", () => {
    expect(nameFromMessage("Chưa kết nối Linear / Not connected to Linear")).toBe("Linear");
    expect(nameFromMessage("Not connected to Slack")).toBe("Slack");
    expect(nameFromMessage("Kết nối 'x' hết hạn")).toBe(""); expect(nameFromMessage(undefined)).toBe("");
  });
  it("only opens links the server could have made", () => {
    for (const ok of ["/api/agent-auth/sessions/a/start", "https://idp.example/auth?x=1", "http://127.0.0.1:8023/x", "http://localhost/x"]) expect(safeConnectUrl(ok)).toBe(ok);
    for (const bad of ["javascript:alert(1)", "data:text/html,x", "//evil.example/x", "/\\evil", "http://evil.example/x", "", "ftp://x/y", "/a\nb", null, 5]) expect(safeConnectUrl(bad)).toBe("");
  });
  it("resume payloads: gate lists what connected, a tool pause says authorized, skip declines both", () => {
    expect(resumeAfter(gate, ["mcp-notion"])).toEqual({ type: "credential_authorization_completed", connected_slugs: ["mcp-notion"] });
    expect(resumeAfter({ type: "auth_required" }, [])).toEqual({ authorized: true });
    expect(SKIP_RESUME).toMatchObject({ skip: true, cancel: true, authorized: false });
  });
  it("poll status maps to a row state", () => {
    expect(rowStateOf("completed")).toBe("connected"); expect(rowStateOf("failed")).toBe("failed"); expect(rowStateOf("expired")).toBe("expired"); expect(rowStateOf("pending")).toBe("waiting");
  });
  it("a new pause (new links) has a new key", () => {
    expect(pauseKey(gate)).toBe(pauseKey(structuredClone(gate)));
    const g2 = structuredClone(gate); g2.credentials[0].auth_id = "ccc"; expect(pauseKey(g2)).not.toBe(pauseKey(gate));
  });
  it("vi and en both have every card string", () => {
    const keys = ["connectTitleOne", "connectTitleMany", "connectHint", "connectReadOnly", "connectButton", "connectWaiting", "connectDone", "connectUnnamed", "connectReconnect", "connectSkip",
      "connectResuming", "connectSecretPh", "connectSave", "connectFailed", "connectExpired", "connectPopupBlocked", "connectPasteSummary", "connectPasteSend"];
    for (const lng of ["vi", "en"]) for (const k of keys) { const v = i18n.getResource(lng, "t", k); expect(typeof v, `${lng}.${k}`).toBe("string"); expect(v).not.toBe(k); }
    expect(i18n.getResource("vi", "t", "connectTitleOne")).toContain("Kết nối"); expect(i18n.getResource("en", "t", "connectTitleOne")).toContain("Connect");
  });
});
