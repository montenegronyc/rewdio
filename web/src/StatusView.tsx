import { State } from "./api";

const fmt = (v: unknown, unit = " dB") => (v === undefined || v === null ? "–" : `${v}${unit}`);

export function StatusView({ state, pair }: { state: State; pair: { url: string; lan: boolean } | null }) {
  const m = state.metrics || {};
  const a = state.pending_action;
  return (
    <div className="status">
      {a && !state.crawl && <section className="hero generic"><div className="hero-label">Your turn</div>
        <p className="hero-note big">{a.instructions}</p></section>}
      <div className="tiles">
        <div className="tile"><span>Phase</span><b>{state.done ? (state.measured_ok ? "Converged" : "At limit") : state.phase}</b></div>
        <div className="tile"><span>Bass deviation</span><b>{fmt(m.bass_deviation?.std_db)}</b></div>
        <div className="tile"><span>Crossover sum</span><b>{fmt(m.crossover_efficiency_db)}</b></div>
        <div className="tile"><span>L/R balance</span><b>{fmt(m.lr_balance?.std_db)}</b></div>
      </div>
      {state.fails && state.fails.length > 0 && <ul className="fails">{state.fails.map((f) => <li key={f}>{f}</li>)}</ul>}
      {pair && (
        <section className="pair">
          {pair.lan ? (
            <>
              <h3>Pair your phone</h3>
              <img src="/api/pair/qr.svg" alt="QR code with the pairing link" />
              <p className="muted">Scan with the phone's camera, then add the page to your home screen.</p>
            </>
          ) : (
            <p className="muted">To use a phone, set <code>dashboard.allow_lan</code> to true and restart Rewdio.</p>
          )}
        </section>
      )}
      <p className="muted"><a href="/classic">Full dashboard with response curves →</a></p>
      <pre className="log">{(state.log || []).slice(-30).join("\n")}</pre>
    </div>
  );
}
