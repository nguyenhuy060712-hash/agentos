// GĐ3.1 Generative UI: the components the model may render through the backend `present` tool (render-only, no code runs).
// Built on @assistant-ui/generative-ui 0.0.6 (MIT, npm 2026-10-19): a curated subset of its built-in vocabulary (no images,
// maps, forms or buttons -> no external requests, no actions) + Agent OS components (Chart via recharts, Comparison, SpecSheet).
import { lazy, Suspense, type ReactNode } from "react";
import { z } from "zod";
import { defaultGenerativeUILibrary, type GenerativeUILibrary } from "@assistant-ui/generative-ui/react";

const ChartView = lazy(() => import("./ChartView"));

/** built-in vocabulary entries we keep (schemas unchanged; rendering styled in genui.css via data-aui hooks) */
export const KEPT = ["Header", "Text", "Caption", "Fact", "Card", "Col", "Row", "Spacer", "Badge", "Box", "ListView", "ListViewItem",
  "Steps", "Timeline", "Table", "Markdown", "Citation", "Alert", "Divider"] as const;

const pick = Object.fromEntries(KEPT.map((k) => [k, defaultGenerativeUILibrary[k]!])) as GenerativeUILibrary;
const chartDef = defaultGenerativeUILibrary.Chart as { description: string; properties: z.ZodType };

const optionSchema = z.object({
  name: z.string().describe("Option name, e.g. a product or plan."),
  badge: z.string().optional().describe("Short tag such as 'Khuyên dùng' or 'Rẻ nhất'."),
  summary: z.string().optional().describe("One-line summary."),
  pros: z.array(z.string()).optional().describe("Advantages, short phrases."),
  cons: z.array(z.string()).optional().describe("Drawbacks, short phrases."),
});
const criterionSchema = z.object({
  label: z.string().describe("Criterion, e.g. 'Giá' or 'Tốc độ'."),
  values: z.array(z.union([z.string(), z.number()])).describe("One value per option, in the same order as `options`."),
});

export const library: GenerativeUILibrary = {
  ...pick,
  Chart: {
    properties: chartDef.properties,
    description: chartDef.description + " Use for numeric trends or comparisons (2-30 points). Label every point.",
    render: (props: Record<string, unknown>) => (
      <Suspense fallback={<div data-aui="chart" className="aos-gen-chart-skeleton" />}>
        <ChartView {...(props as object)} />
      </Suspense>
    ),
  },
  Comparison: {
    description: "Side-by-side comparison of 2-4 options: optional criteria rows (one value per option), pros/cons and a verdict.",
    properties: z.object({
      title: z.string().optional().describe("Heading."),
      options: z.array(optionSchema).describe("The options compared (2-4)."),
      criteria: z.array(criterionSchema).optional().describe("Rows of the comparison table."),
      verdict: z.string().optional().describe("One-sentence recommendation."),
    }),
    render: ({ title, options, criteria, verdict }: { title?: string; options: z.infer<typeof optionSchema>[]; criteria?: z.infer<typeof criterionSchema>[]; verdict?: string }) => (
      <section className="aos-gen-cmp" data-testid="gen-comparison">
        {title ? <h3 className="aos-gen-h">{title}</h3> : null}
        <div className="aos-gen-cmp-grid" style={{ gridTemplateColumns: `repeat(${Math.min(Math.max(options.length, 1), 4)}, minmax(0, 1fr))` }}>
          {options.slice(0, 4).map((o, i) => (
            <div key={i} className="aos-gen-cmp-col">
              <div className="aos-gen-cmp-name">{o.name}{o.badge ? <span className="aos-gen-badge">{o.badge}</span> : null}</div>
              {o.summary ? <p className="aos-gen-muted">{o.summary}</p> : null}
              {criteria?.map((c, j) => (
                <div key={j} className="aos-gen-cmp-row"><span>{c.label}</span><b>{String(c.values[i] ?? "—")}</b></div>
              ))}
              {o.pros?.length ? <ul className="aos-gen-pros">{o.pros.map((p, j) => <li key={j}>{p}</li>)}</ul> : null}
              {o.cons?.length ? <ul className="aos-gen-cons">{o.cons.map((p, j) => <li key={j}>{p}</li>)}</ul> : null}
            </div>
          ))}
        </div>
        {verdict ? <p className="aos-gen-verdict">{verdict}</p> : null}
      </section>
    ),
  },
  SpecSheet: {
    description: "A spec sheet / profile: a title and label-value rows (product specs, a customer profile, a campaign summary).",
    properties: z.object({
      title: z.string().describe("Heading."),
      subtitle: z.string().optional().describe("Secondary line."),
      specs: z.array(z.object({ label: z.string(), value: z.union([z.string(), z.number()]) })).describe("Label-value rows."),
    }),
    render: ({ title, subtitle, specs }: { title: string; subtitle?: string; specs: { label: string; value: string | number }[] }) => (
      <section className="aos-gen-spec" data-testid="gen-spec">
        <h3 className="aos-gen-h">{title}</h3>
        {subtitle ? <p className="aos-gen-muted">{subtitle}</p> : null}
        <dl>{specs.map((s, i) => <div key={i}><dt>{s.label}</dt><dd>{String(s.value)}</dd></div>)}</dl>
      </section>
    ),
  },
};

export const COMPONENT_NAMES = Object.keys(library);
export type GenNode = { _type?: string; $type?: string; children?: ReactNode };
