import { Cell, Crawl } from "./api";

// Top-down plan: front wall at the top (where the speakers are), left wall on the left.
// Rows (letters) run down the screen, columns (numbers) across.
// Monochrome states: to do = keyline circle, now = solid with pulsing rings, measured = filled darker the
// better the spot (rank thirds), skipped = dashed, chosen sub spots = solid squares.
const PAD = 0.28; // metres of margin around the room drawing for wall labels

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
      <rect className="floor" x={0} y={0} width={W} height={L} />
      <text className="wall-label" x={W / 2} y={-PAD / 2.6} textAnchor="middle">Front</text>
      <text className="wall-label" x={-PAD / 2.6} y={L / 2} textAnchor="middle" transform={`rotate(-90 ${-PAD / 2.6} ${L / 2})`}>Left</text>
      {crawl.seat.xyz.length === 3 && (
        <g className="seat">
          <rect x={crawl.seat.xyz[1] - 0.3} y={crawl.seat.xyz[0] - 0.2} width={0.6} height={0.4} />
          <text x={crawl.seat.xyz[1]} y={crawl.seat.xyz[0] + 0.055} textAnchor="middle">{crawl.seat.label}</text>
        </g>
      )}
      {pair.length === 2 && (
        <line className="pair-line" x1={pos(pair[0]).x} y1={pos(pair[0]).y} x2={pos(pair[1]).x} y2={pos(pair[1]).y} />
      )}
      {crawl.cells.filter((c) => c.near_wall || c.enabled).map((c) => {
        const p = pos(c);
        const picked = (highlight || []).includes(c.code);
        const cls = picked ? "picked" : scoreClass(c, scores);
        return (
          <g key={c.code} className={`cell ${cls}`} onClick={() => onCell?.(c)} role="button" aria-label={c.code}>
            {c.status === "current" && (
              <>
                <circle className="ring" cx={p.x} cy={p.y} r={r} />
                <circle className="ring late" cx={p.x} cy={p.y} r={r} />
              </>
            )}
            {picked ? <rect className="dot" x={p.x - r} y={p.y - r} width={2 * r} height={2 * r} />
              : <circle className="dot" cx={p.x} cy={p.y} r={r} />}
            <text className="code" x={p.x} y={p.y + r * 0.35} textAnchor="middle" fontSize={r * 0.95}>{c.code}</text>
          </g>
        );
      })}
    </svg>
  );
}
