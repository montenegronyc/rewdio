import { Action, Crawl } from "./api";

const cm = (m: number) => `${Math.round(m * 100)} cm`;

export function TargetBanner({ crawl }: { crawl: Crawl }) {
  const cell = crawl.cells.find((c) => c.code === crawl.current);
  if (!cell) return null;
  return (
    <section className="hero" aria-live="polite">
      <div className="hero-label">Move the mic to</div>
      <div className="hero-code">{cell.code}</div>
      <div className="hero-sub">
        {cm(cell.from_front_m)} from front wall · {cm(cell.from_left_m)} from left wall · {cm(crawl.grid.mic_height_m)} up
      </div>
      <div className="hero-progress">
        <div className="bar"><span style={{ width: `${(100 * crawl.progress.done) / Math.max(1, crawl.progress.total)}%` }} /></div>
        {crawl.progress.done} of {crawl.progress.total} measured
      </div>
    </section>
  );
}

export function SetupCard({ crawl }: { crawl: Crawl }) {
  const g = crawl.grid;
  return (
    <section className="hero setup">
      <div className="hero-label">First, mark</div>
      <div className="hero-code">A1</div>
      <div className="hero-sub">{cm(g.origin_from_front_m)} out from the FRONT wall · {cm(g.origin_from_left_m)} out from the LEFT wall</div>
      <p className="hero-note">
        Letters go back into the room, numbers go right. Every step is {cm(g.spacing_m)}.
        Tap a spot on the plan to skip it if a sub can't go there.
      </p>
    </section>
  );
}

export function PlaceCard({ place, done }: { place: Record<string, string>; done?: boolean }) {
  return (
    <section className="hero place">
      <div className="hero-label">{done ? "Calibrated with subs at" : "Best spots for your subs"}</div>
      <div className="place-list">
        {Object.entries(place).map(([sub, code]) => (
          <div key={sub} className="place-item"><span>{sub}</span><b>{code}</b></div>
        ))}
      </div>
      <div className="hero-sub">{done ? "All done. The Status tab has the numbers."
        : "Move the subs there, then put the mic back at the listening seat."}</div>
    </section>
  );
}

export function ActionCard({ action }: { action: Action }) {
  return (
    <section className="hero generic">
      <div className="hero-label">Your turn</div>
      <p className="hero-note big">{action.instructions}</p>
    </section>
  );
}
