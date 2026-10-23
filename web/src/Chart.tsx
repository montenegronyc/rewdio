import { Point } from "./api";

// Log-frequency response plot. Monochrome: series differ by dash pattern, never by colour.
const W = 600, H = 220, PAD = { l: 34, r: 8, t: 8, b: 22 };
const TICKS = [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000];
const khz = (f: number) => (f >= 1000 ? `${f / 1000}k` : `${f}`);

type Series = { points: Point[]; kind: "measured" | "predicted" | "target" };

function scales(series: Series[]) {
  const all = series.flatMap((s) => s.points);
  const f0 = Math.min(...all.map((p) => p[0])), f1 = Math.max(...all.map((p) => p[0]));
  const mids = all.map((p) => p[1]).sort((a, b) => a - b);
  const lo = Math.floor((mids[Math.floor(mids.length * 0.02)] - 3) / 5) * 5;
  const hi = Math.ceil((mids[Math.floor(mids.length * 0.98)] + 3) / 5) * 5;
  const x = (f: number) => PAD.l + ((Math.log(f) - Math.log(f0)) / (Math.log(f1) - Math.log(f0))) * (W - PAD.l - PAD.r);
  const y = (db: number) => PAD.t + ((hi - Math.min(hi, Math.max(lo, db))) / (hi - lo)) * (H - PAD.t - PAD.b);
  return { x, y, f0, f1, lo, hi };
}

const path = (pts: Point[], x: (f: number) => number, y: (d: number) => number) =>
  pts.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join("");

export function ResponseChart({ series }: { series: Series[] }) {
  const live = series.filter((s) => s.points?.length);
  if (!live.length) return null;
  const { x, y, f0, f1, lo, hi } = scales(live);
  const grid = [];
  for (let d = lo; d <= hi; d += 5) grid.push(d);
  return (
    <figure className="chart">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Frequency response">
        {TICKS.filter((f) => f >= f0 && f <= f1).map((f) => (
          <g key={f}>
            <line className="grid" x1={x(f)} x2={x(f)} y1={PAD.t} y2={H - PAD.b} />
            <text className="tick" x={x(f)} y={H - 6} textAnchor="middle">{khz(f)}</text>
          </g>
        ))}
        {grid.map((d) => (
          <g key={d}>
            <line className="grid" x1={PAD.l} x2={W - PAD.r} y1={y(d)} y2={y(d)} />
            {d % 10 === 0 && <text className="tick" x={PAD.l - 6} y={y(d) + 4} textAnchor="end">{d}</text>}
          </g>
        ))}
        <rect className="frame" x={PAD.l} y={PAD.t} width={W - PAD.l - PAD.r} height={H - PAD.t - PAD.b} />
        {live.map((s) => <path key={s.kind} className={`series ${s.kind}`} d={path(s.points, x, y)} />)}
      </svg>
      <figcaption>
        {live.map((s) => <span key={s.kind}><svg width="22" height="6"><line className={`series ${s.kind}`} x1="0" x2="22" y1="3" y2="3" /></svg>
          {s.kind[0].toUpperCase() + s.kind.slice(1)}</span>)}
      </figcaption>
    </figure>
  );
}

export function Spark({ points }: { points: Point[] | null }) {
  const w = 64, h = 24;
  if (!points?.length) return <svg className="spark" width={w} height={h} aria-hidden><line className="none" x1="0" x2={w} y1={h / 2} y2={h / 2} /></svg>;
  const fs = points.map((p) => Math.log(p[0])), ds = points.map((p) => p[1]);
  const [f0, f1, d0, d1] = [Math.min(...fs), Math.max(...fs), Math.min(...ds), Math.max(...ds)];
  const d = points.map((p, i) => `${i ? "L" : "M"}${(((Math.log(p[0]) - f0) / (f1 - f0)) * w).toFixed(1)},${
    (2 + ((d1 - p[1]) / Math.max(1, d1 - d0)) * (h - 4)).toFixed(1)}`).join("");
  return <svg className="spark" width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden><path d={d} /></svg>;
}
