import { useEffect, useState } from "react";
import { Cell, confirm, pairing, setCell, State, step } from "./api";
import { ActionCard, PlaceCard, SetupCard, TargetBanner } from "./Cards";
import { useBuzzOnChange, useLiveState, useWakeLock } from "./hooks";
import { RoomPlan } from "./RoomPlan";
import { SetupView } from "./SetupView";
import { StatusView } from "./StatusView";

function primaryLabel(s: State): string {
  const a = s.pending_action;
  if (s.busy) return "Measuring…";
  if (!a) return s.done ? "Finished" : "Start";
  if (a.key === "crawl:grid_setup") return "A1 is marked";
  if (a.key.startsWith("crawl:mic:")) return "Mic is here";
  if (a.key === "crawl:place_final") return "Subs are placed";
  return "Done";
}

export default function App() {
  const { state, unpaired, offline } = useLiveState();
  const [tab, setTab] = useState<"crawl" | "status" | "setup">("crawl");
  const [pair, setPair] = useState<{ url: string; lan: boolean } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const crawl = state?.crawl ?? null;
  const crawling = !!crawl && !state?.done && (!!crawl.current || crawl.setup_needed || !!crawl.place);
  useWakeLock(crawling);
  useBuzzOnChange(state?.pending_action?.key);
  useEffect(() => { pairing().then(setPair).catch(() => undefined); }, []);
  useEffect(() => { if (state && !state.crawl && tab === "crawl") setTab("status"); }, [state?.crawl]);

  if (unpaired) {
    return <main className="center"><section className="hero generic"><div className="hero-label">Not paired</div>
      <p className="hero-note big">Scan the QR code on the computer running Rewdio to pair this phone.</p></section></main>;
  }
  if (!state) return <main className="center"><p className="muted">{offline ? "Can't reach Rewdio…" : "Connecting…"}</p></main>;

  const act = async (fn: () => Promise<any>) => {
    setError(null);
    try { const r = await fn(); if (r && r.ok === false) setError(r.message); } catch { setError("Couldn't reach Rewdio"); }
  };
  const onPrimary = () => act(() => (state.pending_action ? confirm(state.pending_action.key) : step()));
  const onCell = (c: Cell) => {
    if (c.status === "measured" || !c.near_wall) return;
    const skip = c.enabled;
    if (window.confirm(skip ? `Skip ${c.code}? (a sub can't go there)` : `Put ${c.code} back on the route?`)) {
      act(() => setCell(c.code, !skip));
    }
  };
  const a = state.pending_action;
  const placeCodes = crawl?.place ? Object.values(crawl.place) : undefined;

  return (
    <div className="app">
      <header className="top">
        <b>Rewdio</b>
        <nav>
          <button className={tab === "crawl" ? "on" : ""} onClick={() => setTab("crawl")} disabled={!crawl}>Room</button>
          <button className={tab === "status" ? "on" : ""} onClick={() => setTab("status")}>Status</button>
          <button className={tab === "setup" ? "on" : ""} onClick={() => setTab("setup")}>Setup</button>
        </nav>
        {state.simulated && <span className="tag">simulator</span>}
      </header>
      <main>
        {tab === "setup" ? <SetupView /> : tab === "crawl" && crawl ? (
          <>
            {crawl.setup_needed && <SetupCard crawl={crawl} />}
            {!crawl.setup_needed && crawl.current && <TargetBanner crawl={crawl} />}
            {crawl.place && a?.key === "crawl:place_final" && <PlaceCard place={crawl.place} />}
            {crawl.place && state.done && <PlaceCard place={crawl.place} done />}
            {a && !a.key.startsWith("crawl:") && <ActionCard action={a} />}
            <RoomPlan crawl={crawl} onCell={onCell} highlight={placeCodes} />
            <p className="legend">
              <i className="lg pending" />to do <i className="lg current" />now <i className="lg good" />good spot
              <i className="lg poor" />poor spot <i className="lg excluded" />skipped
            </p>
          </>
        ) : (
          <StatusView state={state} pair={pair} />
        )}
      </main>
      {tab !== "setup" && <footer className="actions">
        {error && <p className="error">{error}</p>}
        {!state.busy && state.last_status === "error" && (
          <p className="error">Stopped: {state.last_error}. Outputs were unmuted. Fix it, then tap again to resume.</p>
        )}
        {offline && <p className="error">Connection lost; retrying…</p>}
        <button className="primary" disabled={state.busy || (state.done && !a)} onClick={onPrimary}>
          {state.busy && <span className="spinner" aria-hidden />}{primaryLabel(state)}
        </button>
      </footer>}
    </div>
  );
}
