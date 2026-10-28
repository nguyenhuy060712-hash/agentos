// GĐ3.3: Agent OS `web_search` returns numbered text ("1. title\n   url\n   snippet"); parsed here into source cards.
export type Source = { n: number; title: string; url: string; snippet: string; domain: string };
const toText = (c: unknown): string => (typeof c === "string" ? c : Array.isArray(c) ? c.map((b) => (b && typeof b === "object" && "text" in b ? String((b as { text: unknown }).text) : typeof b === "string" ? b : "")).join("\n") : c == null ? "" : String(c));

export function parseSources(result: unknown): Source[] {
  const lines = toText(result).split("\n");
  const out: Source[] = [];
  for (let i = 0; i < lines.length; i++) {
    const m = lines[i]!.match(/^(\d+)\.\s+(.*)$/);
    if (!m) continue;
    const url = (lines[i + 1] ?? "").trim();
    if (!/^https?:\/\//i.test(url)) continue;
    let domain = url;
    try { domain = new URL(url).hostname.replace(/^www\./, ""); } catch { continue; }
    out.push({ n: Number(m[1]), title: m[2]!.trim() || domain, url, snippet: (lines[i + 2] ?? "").trim(), domain });
    i += 2;
  }
  return out;
}

/** only http(s) links open; anything else is not rendered as a link */
export const safeHref = (u: string) => (/^https?:\/\//i.test(u) ? u : undefined);
