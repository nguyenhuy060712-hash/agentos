// GĐ11.29 pure helpers of the ConnectCard (vitest: __tests__/connect-card.test.ts). Server side: app/connection_runtime.py (gate + sessions), app/agent_identity.py (auth_required).
// Two kinds of pause end up in the same card:
//   auth_required                         {type, tool, tool_call_id, auth_url, auth_id, message}   a tool answered "connect first" (one service)
//   credential_authorization_required     {type, message?, credentials:[{slug, kind, display_name, reason, connect_url, auth_id}]}   the pre-run gate (one or more services)

export type AuthRequired = { type: "auth_required"; tool?: string; tool_call_id?: string; auth_url?: string; auth_id?: string; message?: string };
export type GateCredential = { slug: string; kind?: string; display_name?: string; reason?: string; connect_url?: string; auth_id?: string };
export type CredentialGate = { type: "credential_authorization_required"; message?: string; credentials: GateCredential[] };
/** one row of the card: a service the person has to connect (or a secret to type) */
export type ConnectItem = { slug: string; name: string; secret: boolean; url: string; authId: string; reason: string };
export type RowState = "idle" | "waiting" | "connected" | "failed" | "expired";

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null;
export const isAuthRequired = (v: unknown): v is AuthRequired => isObj(v) && v.type === "auth_required";
export const isCredentialGate = (v: unknown): v is CredentialGate => isObj(v) && v.type === "credential_authorization_required" && Array.isArray(v.credentials);
/** GĐ12.85 the server-side Settings section that shows the state of Google / Microsoft 365 (view-only; links on to Connections) */
export const INTEGRATIONS_URL = "/settings?s=integrations";
export const isConnectInterrupt = (v: unknown): boolean => isAuthRequired(v) || isCredentialGate(v);

/** the card only opens links the server could have made: a path on this server, https, or loopback http (never javascript: / data: / //host) */
export const safeConnectUrl = (u: unknown): string => {
  const s = typeof u === "string" ? u.trim() : "";
  return /^(https:\/\/[^\s\\]+|http:\/\/(127\.0\.0\.1|localhost)(:\d+)?\/\S*|\/(?![/\\])\S*)$/.test(s) && !/[\x00-\x1f]/.test(s) ? s : "";   // eslint-disable-line no-control-regex
};

/** "Chưa kết nối Notion / Not connected to Notion" -> "Notion" (the server's own sentence names the service); "" when it does not */
export const nameFromMessage = (m: string | undefined): string => {
  const s = (m ?? "").trim();
  const vi = /^Chưa kết nối (.+?)(?: \/ |$)/.exec(s); if (vi) return vi[1].trim();
  const en = /Not connected to (.+?)$/.exec(s); if (en) return en[1].trim();
  return "";
};

/** A secret connection has no sign-in: the server hands out auth_id "secret:<slug>" (agent_identity / connection_runtime.needs_connection). */
export const secretSlug = (authId: string | undefined): string => (authId && authId.startsWith("secret:") ? authId.slice(7) : "");

/** the rows of a pause (one for auth_required, one per missing connection for the gate) */
export function itemsOf(v: AuthRequired | CredentialGate): ConnectItem[] {
  if (isCredentialGate(v)) {
    return v.credentials.filter((c) => c && typeof c.slug === "string").map((c) => ({ slug: c.slug, name: c.display_name || c.slug, secret: c.kind === "secret",
      url: safeConnectUrl(c.connect_url), authId: c.auth_id ?? "", reason: c.reason ?? "not_connected" }));
  }
  const sec = secretSlug(v.auth_id);
  const name = nameFromMessage(v.message) || sec || v.tool || "";
  return [{ slug: sec, name, secret: !!sec, url: safeConnectUrl(v.auth_url), authId: v.auth_id ?? "", reason: "not_connected" }];
}

/** stable key of a pause: a new pause (new links) gives a fresh card */
export const pauseKey = (v: AuthRequired | CredentialGate): string => JSON.stringify(itemsOf(v).map((i) => [i.slug, i.authId, i.url]));

/** what the run is resumed with. The server does not trust "connected": it reads the store again (resuming early just pauses again). */
export const SKIP_RESUME = { skip: true, cancel: true, authorized: false } as const;
export const resumeAfter = (v: AuthRequired | CredentialGate, connected: string[]) =>
  isCredentialGate(v) ? { type: "credential_authorization_completed", connected_slugs: connected } : { authorized: true };

/** state of a row from a long-poll answer */
export const rowStateOf = (status: string | undefined): RowState => (status === "completed" ? "connected" : status === "failed" ? "failed" : status === "expired" ? "expired" : "waiting");

export const allDone = (items: ConnectItem[], states: Record<string, RowState>): boolean => items.length > 0 && items.every((_i, n) => states[String(n)] === "connected");

export const POPUP_FEATURES = "popup=yes,width=520,height=720";
