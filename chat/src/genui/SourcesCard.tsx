// GĐ3.3 web search sources: cards with title, domain and snippet; links open in a new tab (no favicons -> no external requests).
import { useTranslation } from "react-i18next";
import { GlobeIcon, LoaderIcon } from "lucide-react";
import { parseSources, safeHref, type Source } from "./sources";

export function SourceList({ sources }: { sources: Source[] }) {
  return (
    <ol className="aos-sources" data-testid="sources">
      {sources.map((s) => (
        <li key={s.n}>
          <a href={safeHref(s.url)} target="_blank" rel="noopener noreferrer nofollow" title={s.url} data-testid="source-link">
            <span className="aos-src-n" aria-hidden>{s.n}</span>
            <span className="aos-src-body">
              <span className="aos-src-title">{s.title}</span>
              <span className="aos-src-domain">{s.domain}</span>
              {s.snippet ? <span className="aos-src-snippet">{s.snippet}</span> : null}
            </span>
          </a>
        </li>
      ))}
    </ol>
  );
}

export function WebSearchCard({ args, result, status }: { args: { query?: string }; result?: unknown; status: { type: string } }) {
  const { t } = useTranslation();
  const sources = result === undefined ? [] : parseSources(result);
  const running = result === undefined && status.type === "running";
  return (
    <div className="aos-tool-card" data-testid="web-search-card">
      <div className="aos-tool-head">
        {running ? <LoaderIcon className="size-3.5 animate-spin" /> : <GlobeIcon className="size-3.5" />}
        <span>{running ? t("searchingWeb") : t("webSources", { n: sources.length })}</span>
        {args.query ? <span className="aos-tool-sub">“{args.query}”</span> : null}
      </div>
      {sources.length ? <SourceList sources={sources} /> : result !== undefined ? <p className="aos-gen-muted">{t("noSources")}</p> : null}
    </div>
  );
}

export function FetchUrlCard({ args, result }: { args: { url?: string }; result?: unknown }) {
  const { t } = useTranslation();
  const href = safeHref(args.url ?? "");
  let domain = args.url ?? "";
  try { domain = new URL(args.url ?? "").hostname.replace(/^www\./, ""); } catch { /* keep raw */ }
  return (
    <div className="aos-tool-card aos-tool-inline" data-testid="fetch-url-card">
      <GlobeIcon className="size-3.5" />
      <span>{result === undefined ? t("readingPage") : t("readPage")}</span>
      {href ? <a href={href} target="_blank" rel="noopener noreferrer nofollow" className="aos-src-domain">{domain}</a> : null}
    </div>
  );
}
