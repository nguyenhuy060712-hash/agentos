// Chart for the generative-ui `Chart` schema (variant bar|line|sparkline|area, data | series, stacked, showAxis, showLegend), via recharts 3 (MIT).
import { Area, AreaChart, Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

type Point = { label?: string; value: number };
type Series = { label?: string; data: Point[] };
type Props = { variant?: "bar" | "line" | "sparkline" | "area"; data?: Point[]; series?: Series[]; stacked?: boolean; showAxis?: boolean; showLegend?: boolean };
const PALETTE = ["var(--chart-1, #2563eb)", "var(--chart-2, #16a34a)", "var(--chart-3, #f59e0b)", "var(--chart-4, #db2777)", "var(--chart-5, #7c3aed)"];

export default function ChartView({ variant = "bar", data, series, stacked, showAxis = true, showLegend }: Props) {
  const ss: Series[] = series?.length ? series : [{ label: "", data: data ?? [] }];
  const n = Math.max(0, ...ss.map((s) => s.data.length));
  const rows = Array.from({ length: n }, (_, i) => {
    const r: Record<string, string | number> = { label: ss.map((s) => s.data[i]?.label).find(Boolean) ?? String(i + 1) };
    ss.forEach((s, j) => { const v = Number(s.data[i]?.value); if (Number.isFinite(v)) r[`s${j}`] = v; });
    return r;
  });
  const spark = variant === "sparkline";
  const axis = showAxis && !spark;
  const legend = (showLegend ?? ss.length > 1) && !spark;
  const common = (
    <>
      {axis ? <CartesianGrid strokeDasharray="3 3" vertical={false} /> : null}
      {axis ? <XAxis dataKey="label" tick={{ fontSize: 12 }} interval="preserveStartEnd" /> : null}
      {axis ? <YAxis tick={{ fontSize: 12 }} width={48} /> : null}
      {!spark ? <Tooltip /> : null}
      {legend ? <Legend /> : null}
    </>
  );
  const name = (j: number) => ss[j]?.label || `#${j + 1}`;
  return (
    <div data-aui="chart" data-testid="gen-chart" data-variant={variant} className="aos-gen-chart" style={{ height: spark ? 64 : 260 }}>
      <ResponsiveContainer width="100%" height="100%">
        {variant === "bar" ? (
          <BarChart data={rows}>{common}{ss.map((_, j) => <Bar key={j} dataKey={`s${j}`} name={name(j)} fill={PALETTE[j % 5]} stackId={stacked ? "a" : undefined} radius={[4, 4, 0, 0]} isAnimationActive={false} />)}</BarChart>
        ) : variant === "area" ? (
          <AreaChart data={rows}>{common}{ss.map((_, j) => <Area key={j} dataKey={`s${j}`} name={name(j)} stroke={PALETTE[j % 5]} fill={PALETTE[j % 5]} fillOpacity={0.15} stackId={stacked ? "a" : undefined} isAnimationActive={false} />)}</AreaChart>
        ) : (
          <LineChart data={rows}>{common}{ss.map((_, j) => <Line key={j} dataKey={`s${j}`} name={name(j)} stroke={PALETTE[j % 5]} dot={!spark} strokeWidth={2} isAnimationActive={false} />)}</LineChart>
        )}
      </ResponsiveContainer>
    </div>
  );
}
