import { useEffect, useState } from "react";
import { getWaterfall, WaterfallData } from "./api";

// Cumulative spectral decay drawn as a classic waterfall: the first slice at the front, later ones stepping
// back and up. Slices are painted back to front so nearer ones hide what's behind. The only place colour is used.
const W = 600, H = 300, PAD = { l: 34, r: 8, t: 8, b: 24 };
const STOPS = 8;
const TICKS = [20, 50, 100, 200];

export function Waterfall({ seat, version }: { seat: string; version: number }) {
  const [data, setData] = useState<WaterfallData | null>(null);
  useEffect(() => { getWaterfall(seat).then(setData).catch(() => setData(null)); }, [seat, version]);
  if (!data?.db?.length) return null;

  const n = data.db.length;
  const dx = 0.3 * (W - PAD.l - PAD.r) / n, dy = 0.62 * (H - PAD.t - PAD.b) / n;
  const pw = W - PAD.l - PAD.r - dx * (n - 1), ph = H - PAD.t - PAD.b - dy * (n - 1);
  const f0 = Math.log(data.freqs[0]), f1 = Math.log(data.freqs[data.freqs.length - 1]);
  const fx = (f: number) => PAD.l + ((Math.log(f) - f0) / (f1 - f0)) * pw;
  const fy = (db: number) => H - PAD.b - ((db - data.floor_db) / -data.floor_db) * ph;
  const slice = (k: number) => {
    const ox = k * dx, oy = -k * dy;
    const top = data.db[k].map((d, i) => `${(fx(data.freqs[i]) + ox).toFixed(1)},${(fy(d) + oy).toFixed(1)}`);
    const base = `${(fx(data.freqs[data.freqs.length - 1]) + ox).toFixed(1)},${(H - PAD.b + oy).toFixed(1)} ` +
      `${(fx(data.freqs[0]) + ox).toFixed(1)},${(H - PAD.b + oy).toFixed(1)}`;
    return `M${top.join("L")}L${base.replace(" ", "L")}Z`;
  };
  const order = Array.from({ length: n }, (_, k) => n - 1 - k);
  return (
    <figure className="chart waterfall">
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Waterfall">
        {order.map((k) => (
          <path key={k} d={slice(k)} className="wf-slice"
            style={{ fill: `var(--waterfall-${Math.round((k / Math.max(1, n - 1)) * (STOPS - 1))})` }} />
        ))}
        {TICKS.filter((f) => f >= data.freqs[0] && f <= data.freqs[data.freqs.length - 1]).map((f) => (
          <text key={f} className="tick" x={fx(f)} y={H - 6} textAnchor="middle">{f}</text>
        ))}
        {[0, data.floor_db / 2, data.floor_db].map((d) => (
          <text key={d} className="tick" x={PAD.l - 6} y={fy(d) + 4} textAnchor="end">{d}</text>
        ))}
        <line className="axis" x1={PAD.l + pw} y1={H - PAD.b} x2={PAD.l + pw + dx * (n - 1)} y2={H - PAD.b - dy * (n - 1)} />
        <text className="tick" x={PAD.l + pw + dx * (n - 1)} y={H - PAD.b - dy * (n - 1) - 6} textAnchor="end">
          {data.times_ms[n - 1]} ms</text>
      </svg>
    </figure>
  );
}
