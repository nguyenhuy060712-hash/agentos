// GĐ3.3 inline citations: "[1]" in an answer becomes a superscript link to source #1 of the same message's web_search result.
import { useAuiState } from "@assistant-ui/react";
import { useMemo, type ComponentProps, type FC } from "react";
import { parseSources, safeHref } from "@/genui/sources";

type MdNode = { type: string; value?: string; children?: MdNode[]; url?: string; data?: unknown };
/** remark plugin: text "[n]" (1-20) -> link node "#cite-n" (not inside code / existing links) */
export function remarkCitations() {
  return (tree: MdNode) => {
    const walk = (n: MdNode) => {
      if (!n.children || n.type === "link" || n.type === "code" || n.type === "inlineCode") return;
      const out: MdNode[] = [];
      for (const c of n.children) {
        if (c.type === "text" && c.value && /\[\d{1,2}\]/.test(c.value)) {
          let last = 0; const re = /\[(\d{1,2})\]/g; let m: RegExpExecArray | null;
          while ((m = re.exec(c.value))) {
            if (m.index > last) out.push({ type: "text", value: c.value.slice(last, m.index) });
            out.push({ type: "link", url: `#cite-${m[1]}`, children: [{ type: "text", value: m[1]! }] });
            last = m.index + m[0].length;
          }
          if (last < c.value.length) out.push({ type: "text", value: c.value.slice(last) });
        } else { walk(c); out.push(c); }
      }
      n.children = out;
    };
    walk(tree);
  };
}

function useMessageSources() {
  const content = useAuiState((s) => s.message.content);   // stable reference; derive outside the selector
  return useMemo(() => (content as readonly { type: string; toolName?: string; result?: unknown }[])
    .filter((p) => p.type === "tool-call" && p.toolName === "web_search" && p.result !== undefined).flatMap((p) => parseSources(p.result)), [content]);
}

export const CitationLink: FC<ComponentProps<"a">> = ({ href, children, className, ...props }) => {
  const sources = useMessageSources();
  const m = href?.match(/^#cite-(\d+)$/);
  if (!m) return <a href={href} className={className} target={href?.startsWith("#") ? undefined : "_blank"} rel="noopener noreferrer nofollow" {...props}>{children}</a>;
  const n = Number(m[1]);
  const src = sources.find((x) => x.n === n);
  if (!src) return <>[{children}]</>;
  return (
    <sup><a href={safeHref(src.url)} target="_blank" rel="noopener noreferrer nofollow" className="aos-cite" title={`${src.title} — ${src.domain}`} data-testid="inline-cite">{n}</a></sup>
  );
};
