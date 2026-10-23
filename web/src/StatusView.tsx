import { useState } from "react";
import { State } from "./api";
import { ActionCard } from "./Cards";
import { ResponseChart } from "./Chart";
import { Waterfall } from "./Waterfall";

const fmt = (v: unknown, unit = " dB") => (v === undefined || v === null ? "–" : `${v}${unit}`);

export function StatusView({ state, pair }: { state: State; pair: { url: string; lan: boolean } | null }) {
  const m = state.metrics || {};
  const seats = Object.keys(state.curves || {});
  const [seat, setSeat] = useState<string | null>(null);
  const [view, setView] = useState<"response" | "waterfall">("response");
  const shown = seat && seats.includes(seat) ? seat : seats[0];
  const c = state.curves?.[shown];
  return (
    <div className="status">
      {state.pending_action && !state.crawl && <ActionCard action={state.pending_action} />}
      <dl className="rows">
        <div><dt>Phase</dt><dd>{state.done ? (state.measured_ok ? "Converged" : "At limit") : state.phase.toLowerCase().replace(/_/g, " ").replace(/^./, (x) => x.toUpperCase())}</dd></div>
        <div><dt>Bass deviation</dt><dd>{fmt(m.bass_deviation?.std_db)}</dd></div>
        <div><dt>Crossover sum</dt><dd>{fmt(m.crossover_efficiency_db)}</dd></div>
        <div><dt>L/R balance</dt><dd>{fmt(m.lr_balance?.std_db)}</dd></div>
        <div><dt>Sweeps</dt><dd>{state.sweeps}</dd></div>
      </dl>
      {c && (
        <>
          <div className="chart-bar">
            <div className="segmented">
              <button className={view === "response" ? "on" : ""} onClick={() => setView("response")}>Response</button>
              <button className={view === "waterfall" ? "on" : ""} onClick={() => setView("waterfall")}>Waterfall</button>
            </div>
            {seats.length > 1 && (
              <div className="segmented">{seats.map((s) => (
                <button key={s} className={s === shown ? "on" : ""} onClick={() => setSeat(s)}>{s}</button>))}</div>
            )}
          </div>
          {view === "waterfall" ? <Waterfall seat={shown} version={state.version} /> : <ResponseChart series={[
            { kind: "measured", points: c.measured || [] },
            { kind: "predicted", points: c.predicted || [] },
            { kind: "target", points: c.target || [] },
          ]} />}
        </>
      )}
      {state.fails && state.fails.length > 0 && <ul className="plain">{state.fails.map((f) => <li key={f}>{f[0].toUpperCase() + f.slice(1)}</li>)}</ul>}
      {pair?.lan && <img className="qr" src="/api/pair/qr.svg" alt="Pairing code" />}
      <details className="log">
        <summary>Log</summary>
        <pre>{(state.log || []).slice(-40).join("\n")}</pre>
      </details>
    </div>
  );
}
