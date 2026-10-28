// GĐ3.4 diagrams: Mermaid 12 (MIT), lazy chunk, securityLevel "strict" (no click handlers / HTML labels), rendered once the
// fence is complete. A syntax error shows the source instead. Layout pinned to dagre (MIT); the ELK layout is stubbed out (elkjs is EPL-2.0).
import { useEffect, useId, useState, type FC } from "react";
import type { SyntaxHighlighterProps } from "@assistant-ui/react-markdown";
import { useMessagePartText } from "@assistant-ui/react";

let mm: Promise<typeof import("mermaid").default> | null = null;
const load = () => (mm ??= import("mermaid").then((m) => { m.default.initialize({ startOnLoad: false, securityLevel: "strict", layout: "dagre", theme: document.documentElement.classList.contains("dark") ? "dark" : "default", fontFamily: "inherit" }); return m.default; }));

export const MermaidDiagram: FC<SyntaxHighlighterProps> = ({ code, components: { Pre, Code } }) => {
  const id = "m" + useId().replace(/[^a-zA-Z0-9]/g, "");
  const running = useMessagePartText().status.type === "running";
  const [svg, setSvg] = useState<string | null>(null);
  const [err, setErr] = useState(false);
  useEffect(() => {
    if (running) return;
    let live = true;
    // short debounce: the part can report "complete" a tick before the last chunk lands
    const t = setTimeout(() => load().then((m) => m.render(id, code)).then((r) => { if (live) { setSvg(r.svg); setErr(false); } }).catch((e) => { console.warn("mermaid render failed:", String(e?.message ?? e).slice(0, 200)); if (live) { setSvg(null); setErr(true); } }), 250);
    return () => { live = false; clearTimeout(t); };
  }, [code, id, running]);
  // svg comes from mermaid.render with securityLevel strict (DOMPurify-sanitised by mermaid)
  if (svg && !err) return <div className="aos-mermaid" data-testid="mermaid" dangerouslySetInnerHTML={{ __html: svg }} />;
  return <Pre><Code>{code}</Code></Pre>;
};
