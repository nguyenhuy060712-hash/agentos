// GĐ3.4 code highlighting: Shiki 4 (MIT) with the JavaScript regex engine (no WASM -> CSP unchanged) and a fixed set of
// bundled languages/themes, lazy-loaded the first time a code block appears.
import { useEffect, useState, type FC } from "react";
import type { SyntaxHighlighterProps } from "@assistant-ui/react-markdown";

type HL = { codeToHtml: (code: string, o: { lang: string; themes: { light: string; dark: string }; defaultColor: false }) => string; langs: Set<string> };
let hl: Promise<HL> | null = null;
const ALIAS: Record<string, string> = { py: "python", js: "javascript", ts: "typescript", sh: "bash", shell: "bash", zsh: "bash", yml: "yaml", md: "markdown", html: "html", jsonc: "json" };

function load(): Promise<HL> {
  hl ??= (async () => {
    const [{ createHighlighterCore }, { createJavaScriptRegexEngine }] = await Promise.all([import("shiki/core"), import("shiki/engine/javascript")]);
    const langs = await Promise.all([
      import("shiki/langs/python.mjs"), import("shiki/langs/javascript.mjs"), import("shiki/langs/typescript.mjs"), import("shiki/langs/tsx.mjs"),
      import("shiki/langs/json.mjs"), import("shiki/langs/bash.mjs"), import("shiki/langs/sql.mjs"), import("shiki/langs/yaml.mjs"),
      import("shiki/langs/html.mjs"), import("shiki/langs/css.mjs"), import("shiki/langs/markdown.mjs"),
    ]);
    const h = await createHighlighterCore({
      themes: [import("shiki/themes/github-light.mjs"), import("shiki/themes/github-dark.mjs")],
      langs: langs.map((m) => m.default), engine: createJavaScriptRegexEngine(),
    });
    return { codeToHtml: (c, o) => h.codeToHtml(c, o), langs: new Set(h.getLoadedLanguages()) };
  })();
  return hl;
}

export const ShikiHighlighter: FC<SyntaxHighlighterProps> = ({ code, language, components: { Pre, Code } }) => {
  const lang = ALIAS[language] ?? language;
  const [html, setHtml] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    load().then((h) => { if (live && h.langs.has(lang)) setHtml(h.codeToHtml(code, { lang, themes: { light: "github-light", dark: "github-dark" }, defaultColor: false })); })
      .catch(() => {});
    return () => { live = false; };
  }, [code, lang]);
  // Shiki output is generated from the code text by Shiki itself (escaped tokens in spans), never from model HTML.
  if (html) return <div className="aos-shiki" data-testid="shiki" data-lang={lang} dangerouslySetInnerHTML={{ __html: html }} />;
  return <Pre><Code>{code}</Code></Pre>;
};

/** GĐ5b: standalone highlighted block (canvas panel, code-run card); plain <pre> until Shiki is loaded or for unknown languages */
export function CodeView({ code, language, className = "" }: { code: string; language: string; className?: string }) {
  const lang = ALIAS[language] ?? language;
  const [html, setHtml] = useState<string | null>(null);
  useEffect(() => {
    let live = true; setHtml(null);
    load().then((h) => { if (live && h.langs.has(lang)) setHtml(h.codeToHtml(code, { lang, themes: { light: "github-light", dark: "github-dark" }, defaultColor: false })); })
      .catch(() => {});
    return () => { live = false; };
  }, [code, lang]);
  if (html) return <div className={`aos-shiki ${className}`} data-testid="shiki" data-lang={lang} dangerouslySetInnerHTML={{ __html: html }} />;
  return <pre className={`aos-code-plain ${className}`}><code>{code}</code></pre>;
}
