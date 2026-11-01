import { Action, Crawl, Speaker } from "./api";

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

const signed = (a: number) => (a === 0 ? "0" : `${a > 0 ? "+" : "\u2212"}${Math.abs(a)}`);

export function SpeakerCard({ sp, voice, onVoice }: { sp: Speaker; voice: boolean; onVoice: (on: boolean) => void }) {
  const p = sp.prompt;
  const pct = (100 * sp.progress.done) / Math.max(1, sp.progress.total);
  const failed = sp.last && (sp.last.status === "retry" || sp.last.status === "error");
  return (
    <section className="hero" aria-live="polite">
      {p ? (
        <>
          <p className="hero-sub">{p.plane === "hor" ? "Horizontal" : "Vertical"}</p>
          <h1 className="hero-code">{signed(p.target)}°</h1>
          {p.move_deg !== 0 && (
            <p className="hero-sub">Turn {Math.abs(p.move_deg)}° {p.move_deg > 0 ? "clockwise" : "anticlockwise"} (seen from above)</p>
          )}
          <p className="instructions">{p.instructions}</p>
        </>
      ) : <h1 className="hero-title">{sp.done ? "Set complete" : "Measuring"}</h1>}
      {failed && <p className="note">{sp.last!.speech}{sp.last!.error ? ` (${sp.last!.error})` : ""}</p>}
      <div className="progress"><div className="bar"><span style={{ width: `${pct}%` }} /></div>
        <span>{sp.progress.done} of {sp.progress.total}</span></div>
      <label className="soft"><input type="checkbox" checked={voice} onChange={(e) => onVoice(e.target.checked)} /> Voice on this phone</label>
      {sp.mac_voice.problem && <p className="soft">Mac voice off: {sp.mac_voice.problem}</p>}
    </section>
  );
}
