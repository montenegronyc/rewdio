import { Action, Crawl } from "./api";

const cm = (m: number) => `${Math.round(m * 100)} cm`;

export function TargetBanner({ crawl }: { crawl: Crawl }) {
  const cell = crawl.cells.find((c) => c.code === crawl.current);
  if (!cell) return null;
  const pct = (100 * crawl.progress.done) / Math.max(1, crawl.progress.total);
  return (
    <section className="hero" aria-live="polite">
      <h1 className="hero-code">{cell.code}</h1>
      <p className="hero-sub">{cm(cell.from_front_m)} from front · {cm(cell.from_left_m)} from left · {cm(crawl.grid.mic_height_m)} up</p>
      <div className="progress"><div className="bar"><span style={{ width: `${pct}%` }} /></div>
        <span>{crawl.progress.done} of {crawl.progress.total}</span></div>
    </section>
  );
}

export function SetupCard({ crawl }: { crawl: Crawl }) {
  const g = crawl.grid;
  return (
    <section className="hero">
      <h1 className="hero-code">A1</h1>
      <p className="hero-sub">{cm(g.origin_from_front_m)} from front · {cm(g.origin_from_left_m)} from left · {cm(g.spacing_m)} steps</p>
    </section>
  );
}

export function PlaceCard({ place }: { place: Record<string, string> }) {
  return (
    <section className="hero">
      <dl className="rows big">
        {Object.entries(place).map(([sub, code]) => <div key={sub}><dt>{sub}</dt><dd>{code}</dd></div>)}
      </dl>
    </section>
  );
}

export function ActionCard({ action }: { action: Action }) {
  return <section className="hero"><p className="instructions">{action.instructions}</p></section>;
}
