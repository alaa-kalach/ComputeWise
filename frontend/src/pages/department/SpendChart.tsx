import { useEffect, useRef, useState } from "react";

import { Button } from "../../components/ui";
import type { TrendPoint } from "../../lib/api/types";
import { usd } from "../../lib/money";

// Spend per month, split by who pays: stacked columns, oldest month on the left.
// Colors are validated (dataviz validator, light and dark) and live in styles.css as
// --series-billable / --series-observed / --series-unassigned.

const SERIES = [
  { key: "billable", label: "Billable", hint: "department accounts, charged back" },
  { key: "observed", label: "Observed", hint: "professors' own accounts" },
  { key: "unassigned", label: "Unassigned", hint: "funding source unknown" },
] as const;
type SeriesKey = (typeof SERIES)[number]["key"];

const H = 220;
const PAD = { top: 16, right: 12, bottom: 28, left: 48 };
const BAR = 24; // max column width
const GAP = 2; // surface gap between stacked segments
const MONTHS_SHOWN = 12;

function monthLabel(period: string, long = false): string {
  return new Date(`${period}-01T12:00:00`).toLocaleDateString("en-US", long ? { month: "long", year: "numeric" } : { month: "short" });
}

/** A round axis maximum and its ticks: 0, step, 2*step, ... */
function niceTicks(max: number): number[] {
  if (max <= 0) return [0, 10];
  const raw = max / 4;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? 10 * mag;
  const ticks = [];
  for (let v = 0; v <= max + step * 0.999; v += step) ticks.push(Math.round(v * 100) / 100);
  return ticks;
}

/** A column segment: square at the bottom, 4px rounded corners on top when it is the cap. */
function segmentPath(x: number, y: number, w: number, h: number, roundTop: boolean): string {
  const r = roundTop ? Math.min(4, h, w / 2) : 0;
  return `M${x},${y + h}V${y + r}${r ? `Q${x},${y} ${x + r},${y}` : ""}H${x + w - r}${r ? `Q${x + w},${y} ${x + w},${y + r}` : ""}V${y + h}Z`;
}

export function SpendChart({ points }: { points: TrendPoint[] }) {
  const [hover, setHover] = useState<number | null>(null);
  const [asTable, setAsTable] = useState(false);
  // Draw at the real width so text stays 11-12px on a phone instead of being scaled down.
  const box = useRef<HTMLDivElement>(null);
  const [W, setW] = useState(640);
  useEffect(() => {
    const el = box.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([e]) => setW(Math.max(280, Math.round(e.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, [asTable]);
  const data = points.slice(-MONTHS_SHOWN);

  if (data.length === 0) return <p className="panel-empty">No spend has been recorded yet.</p>;

  const ticks = niceTicks(Math.max(...data.map((p) => Number(p.total))));
  const top = ticks[ticks.length - 1];
  const plotW = W - PAD.left - PAD.right;
  const plotH = H - PAD.top - PAD.bottom;
  const band = plotW / data.length;
  const barW = Math.min(BAR, band * 0.6);
  const y = (v: number) => PAD.top + plotH - (v / top) * plotH;

  const latest = data[data.length - 1];
  const summary = `Spend per month, ${monthLabel(data[0].period, true)} to ${monthLabel(latest.period, true)}. `
    + `${monthLabel(latest.period, true)}: ${usd(latest.total)} in total, ${usd(latest.billable)} billable.`;

  return (
    <div className="chart">
      <div className="chart-head">
        <ul className="legend" aria-label="Legend">
          {SERIES.map((s) => (
            <li key={s.key}>
              <span className={`swatch swatch-${s.key}`} aria-hidden="true" />
              {s.label} <span className="muted">· {s.hint}</span>
            </li>
          ))}
        </ul>
        <Button size="sm" variant="quiet" onClick={() => setAsTable((t) => !t)} aria-pressed={asTable}>
          {asTable ? "Show chart" : "Show as table"}
        </Button>
      </div>

      {asTable ? (
        <div className="table-wrap">
          <table className="table">
            <caption className="sr-only">Spend per month by who pays</caption>
            <thead>
              <tr>
                <th scope="col">Month</th>
                {SERIES.map((s) => <th key={s.key} scope="col" className="num">{s.label}</th>)}
                <th scope="col" className="num">Total</th>
              </tr>
            </thead>
            <tbody>
              {data.map((p) => (
                <tr key={p.period}>
                  <th scope="row">{monthLabel(p.period, true)}</th>
                  {SERIES.map((s) => <td key={s.key} className="num">{usd(p[s.key])}</td>)}
                  <td className="num"><b>{usd(p.total)}</b></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="chart-plot" ref={box} onMouseLeave={() => setHover(null)}>
          <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={summary} width={W} height={H}>
            {ticks.map((t) => (
              <g key={t}>
                <line className="grid" x1={PAD.left} x2={W - PAD.right} y1={y(t)} y2={y(t)} />
                <text className="tick" x={PAD.left - 8} y={y(t)} dy="0.32em" textAnchor="end">
                  {t === 0 ? "$0" : `$${t.toLocaleString("en-US")}`}
                </text>
              </g>
            ))}
            {data.map((p, i) => {
              const cx = PAD.left + band * i + band / 2;
              const x = cx - barW / 2;
              const parts = SERIES.map((s) => ({ key: s.key as SeriesKey, v: Number(p[s.key]) })).filter((s) => s.v > 0);
              let base = 0;
              return (
                <g key={p.period} className={hover === i ? "col is-hover" : "col"}>
                  {parts.map((s, j) => {
                    const y0 = y(base);
                    base += s.v;
                    const y1 = y(base);
                    const last = j === parts.length - 1;
                    const h = Math.max(0, y0 - y1 - (j > 0 ? GAP : 0));
                    return <path key={s.key} className={`seg seg-${s.key}`} d={segmentPath(x, y1, barW, h, last)} />;
                  })}
                  <text className="tick" x={cx} y={H - 8} textAnchor="middle">{monthLabel(p.period)}</text>
                  {i === data.length - 1 && hover !== i && (
                    <text className="value" x={cx} y={y(Number(p.total)) - 6} textAnchor="middle">{usd(p.total)}</text>
                  )}
                  {/* the hover target: the whole band, wider and taller than the column */}
                  <rect className="hit" x={PAD.left + band * i} y={PAD.top} width={band} height={plotH}
                    onMouseEnter={() => setHover(i)} />
                </g>
              );
            })}
          </svg>
          {hover !== null && (
            <div className={`tooltip ${hover >= data.length / 2 ? "tooltip-left" : "tooltip-right"}`} role="status"
              style={{ left: `${((PAD.left + band * hover + band / 2) / W) * 100}%` }}>
              <div className="tooltip-title">{monthLabel(data[hover].period, true)}</div>
              {SERIES.map((s) => (
                <div key={s.key} className="tooltip-row">
                  <span className={`swatch swatch-${s.key}`} aria-hidden="true" />
                  <span className="grow">{s.label}</span>
                  <b>{usd(data[hover][s.key])}</b>
                </div>
              ))}
              <div className="tooltip-row tooltip-total"><span className="grow">Total</span><b>{usd(data[hover].total)}</b></div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
