import { Cell, Crawl } from "./api";

// Top-down plan: front wall at the top (where the speakers are), left wall on the left.
// Rows (letters) run down the screen, columns (numbers) across.
const PAD = 0.28; // metres of margin around the room drawing for wall labels

// Rank thirds, not a linear range: one bad outlier must not turn everything else green.
function scoreClass(c: Cell, scores: number[]): string {
  if (c.status !== "measured" || c.score_db === undefined || scores.length < 3) return c.status;
  const rank = scores.filter((s) => s < (c.score_db as number)).length / scores.length;
  return rank < 1 / 3 ? "good" : rank < 2 / 3 ? "ok" : "poor";
}

export function RoomPlan({ crawl, onCell, highlight }: {
  crawl: Crawl; onCell?: (c: Cell) => void; highlight?: string[];
}) {
  const [L, W] = crawl.room;
  const scores = crawl.cells.filter((c) => c.score_db !== undefined).map((c) => c.score_db as number);
  const r = Math.min(0.17, crawl.grid.spacing_m * 0.3);
  const pos = (c: { xyz: number[] }) => ({ x: c.xyz[1], y: c.xyz[0] }); // screen x = from left, y = from front
  const pair = (highlight || []).map((code) => crawl.cells.find((c) => c.code === code)).filter(Boolean) as Cell[];
  return (
    <svg className="plan" viewBox={`${-PAD} ${-PAD} ${W + 2 * PAD} ${L + 2 * PAD}`} role="img"
      aria-label={`Room plan, ${crawl.cells.length} grid spots`}>
      <rect className="floor" x={0} y={0} width={W} height={L} rx={0.06} />
      <text className="wall-label" x={W / 2} y={-PAD / 2.2} textAnchor="middle">FRONT</text>
      <text className="wall-label" x={-PAD / 2.2} y={L / 2} textAnchor="middle" transform={`rotate(-90 ${-PAD / 2.2} ${L / 2})`}>LEFT</text>
      {crawl.seat.xyz.length === 3 && (
        <g className="seat">
          <rect x={crawl.seat.xyz[1] - 0.3} y={crawl.seat.xyz[0] - 0.22} width={0.6} height={0.44} rx={0.1} />
          <text x={crawl.seat.xyz[1]} y={crawl.seat.xyz[0] + 0.06} textAnchor="middle">{crawl.seat.label}</text>
        </g>
      )}
      {pair.length === 2 && (
        <line className="pair-line" x1={pos(pair[0]).x} y1={pos(pair[0]).y} x2={pos(pair[1]).x} y2={pos(pair[1]).y} />
      )}
      {crawl.cells.filter((c) => c.near_wall || c.enabled).map((c) => {
        const p = pos(c);
        const cls = (highlight || []).includes(c.code) ? "picked" : scoreClass(c, scores);
        return (
          <g key={c.code} className={`cell ${cls}`} onClick={() => onCell?.(c)} role="button" aria-label={c.code}>
            {c.status === "current" && (
              <>
                <circle className="ring" cx={p.x} cy={p.y} r={r} />
                <circle className="ring late" cx={p.x} cy={p.y} r={r} />
              </>
            )}
            <circle className="dot" cx={p.x} cy={p.y} r={r} />
            <text className="code" x={p.x} y={p.y + r * 0.35} textAnchor="middle" fontSize={r * 0.95}>{c.code}</text>
          </g>
        );
      })}
    </svg>
  );
}
