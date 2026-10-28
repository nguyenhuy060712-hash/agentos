// GĐ5a: @mention agents in the composer. The textarea shows plain "@Tên agent" (no raw directive syntax); the same formatter
// parses it back into chips in sent messages (DirectiveText) and tells the runtime which agent was mentioned.
// API: assistant-ui 0.15.30 DirectiveFormatter {serialize, parse} (docs/guides/mentions, fetched 20/10/2026).
import { defaultDirectiveFormatter, type DirectiveFormatter, type DirectiveSegment, type TriggerItem } from "@assistant-ui/react";

export type MentionAgent = { id: string; label: string };
let agents: MentionAgent[] = [];
/** Known agents (graph_id + display name), longest label first so "Trợ lý nhà bếp" wins over "Trợ lý nhà". */
export const mentionAgents = {
  set(list: readonly MentionAgent[]) { agents = [...list].filter((a) => a.label.trim()).sort((a, b) => b.label.length - a.label.length); },
  get: () => agents,
};

const WORD = /[\p{L}\p{N}_]/u;

function parseAt(text: string): DirectiveSegment[] {
  const out: DirectiveSegment[] = []; let buf = ""; let i = 0;
  while (i < text.length) {
    if (text[i] === "@" && (i === 0 || /\s|\(|\[/.test(text[i - 1]!))) {
      const rest = text.slice(i + 1);
      const hit = agents.find((a) => rest.toLocaleLowerCase("vi").startsWith(a.label.toLocaleLowerCase("vi")) && !WORD.test(rest[a.label.length] ?? " "));
      if (hit) {
        if (buf) out.push({ kind: "text", text: buf }); buf = "";
        out.push({ kind: "mention", type: "agent", label: hit.label, id: hit.id });
        i += 1 + hit.label.length; continue;
      }
    }
    buf += text[i]; i++;
  }
  if (buf || !out.length) out.push({ kind: "text", text: buf });
  return out;
}

export const agentFormatter: DirectiveFormatter = {
  serialize: (item: TriggerItem) => (item.type === "agent" ? `@${item.label}` : defaultDirectiveFormatter.serialize(item)),
  parse(text: string) {
    // directive syntax (":agent[Label]{name=agent-1}", e.g. typed by hand or from another client) + plain "@Label"
    return defaultDirectiveFormatter.parse(text).flatMap((s) => (s.kind === "text" ? parseAt(s.text) : [s]));
  },
};

/** First agent mentioned in a message, or null. */
export function firstAgentMention(text: string): MentionAgent | null {
  const m = agentFormatter.parse(text).find((s) => s.kind === "mention" && s.type === "agent");
  return m && m.kind === "mention" ? { id: m.id, label: m.label } : null;
}
