import { useEffect, useState } from "react";
import { Cell, confirm, pairing, setCell, speakerReady, State, step } from "./api";
import { ActionCard, PlaceCard, SetupCard, SpeakerCard, TargetBanner } from "./Cards";
import { setVoiceEnabled, useBuzzOnChange, useLiveState, useSpeakOnChange, useWakeLock, voiceEnabled } from "./hooks";
import { RoomPlan } from "./RoomPlan";
import { SettingsView } from "./SettingsView";
import { SetupsView } from "./SetupsView";
import { StatusView } from "./StatusView";

type Tab = "room" | "status" | "setups" | "settings";
const TABS: [Tab, string][] = [["room", "Room"], ["status", "Status"], ["setups", "Setups"], ["settings", "Settings"]];

function primaryLabel(s: State): string {
  const a = s.pending_action;
  if (s.busy) return "Measuring";
  if (s.speaker) return s.speaker.waiting ? "Ready" : s.speaker.done ? "Set complete" : "Measuring";
  if (!a) return s.done ? "Finished" : "Start";
  if (a.key === "crawl:grid_setup") return "A1 is marked";
  if (a.key.startsWith("crawl:mic:")) return "Mic is here";
  if (a.key === "crawl:place_final") return "Subs are placed";
  return "Done";
}

export default function App() {
  const { state, unpaired, offline } = useLiveState();
  const [tab, setTab] = useState<Tab>("room");
  const [pair, setPair] = useState<{ url: string; lan: boolean } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const crawl = state?.crawl ?? null;
  const crawling = !!crawl && !state?.done && (!!crawl.current || crawl.setup_needed || !!crawl.place);
  useWakeLock(crawling);
  const sp = state?.speaker ?? null;
  const [voice, setVoice] = useState(voiceEnabled());
  useWakeLock(!!sp && !sp.done);
  useBuzzOnChange(state?.pending_action?.key ?? sp?.prompt?.key);
  // the first prompt, then each result ("Got it." plus the next prompt, or the reason to go again)
  useSpeakOnChange(sp?.last ? `r${sp.last.at}` : sp?.prompt?.key, sp?.last ? sp.last.speech : sp?.prompt?.speech, voice);
  useEffect(() => { pairing().then(setPair).catch(() => undefined); }, []);
  useEffect(() => { if (state && !state.crawl && tab === "room") setTab("status"); }, [state?.crawl]);

  if (unpaired) {
    return <main className="center"><h1 className="hero-title">Not paired</h1><p className="soft">Scan the code on the computer.</p></main>;
  }
  if (!state) return <main className="center"><p className="soft">{offline ? "Can't reach Rewdio" : "Connecting"}</p></main>;

  const act = async (fn: () => Promise<any>) => {
    setError(null);
    try { const r = await fn(); if (r && r.ok === false) setError(r.message); } catch { setError("Couldn't reach Rewdio"); }
  };
  const onPrimary = () => act(() => (sp ? speakerReady(sp.prompt?.key)
    : state.pending_action ? confirm(state.pending_action.key) : step()));
  const onVoice = (on: boolean) => { setVoiceEnabled(on); setVoice(on); };
  const onCell = (c: Cell) => {
    if (c.status === "measured" || !c.near_wall) return;
    const skip = c.enabled;
    if (window.confirm(skip ? `Skip ${c.code}?` : `Restore ${c.code}?`)) act(() => setCell(c.code, !skip));
  };
  const a = state.pending_action;
  const placeCodes = crawl?.place ? Object.values(crawl.place) : undefined;
  const footer = tab === "room" || tab === "status";

  return (
    <div className="app">
      <header className="top">
        <b>Rewdio</b>
        {state.simulated && <span className="soft">Simulator</span>}
        <nav>
          {TABS.map(([id, name]) => (
            <button key={id} className={tab === id ? "on" : ""} onClick={() => setTab(id)} disabled={id === "room" && !crawl}>{name}</button>
          ))}
        </nav>
      </header>
      <main>
        {tab === "settings" ? <SettingsView /> : tab === "setups" ? <SetupsView version={state.version} busy={state.busy} />
          : tab === "room" && crawl ? (
            <>
              {crawl.setup_needed && <SetupCard crawl={crawl} />}
              {!crawl.setup_needed && crawl.current && <TargetBanner crawl={crawl} />}
              {crawl.place && (a?.key === "crawl:place_final" || state.done) && <PlaceCard place={crawl.place} />}
              {a && !a.key.startsWith("crawl:") && <ActionCard action={a} />}
              <RoomPlan crawl={crawl} onCell={onCell} highlight={placeCodes} />
            </>
          ) : sp && tab === "status" ? <SpeakerCard sp={sp} voice={voice} onVoice={onVoice} />
          : <StatusView state={state} pair={pair} />}
      </main>
      {footer && <footer className="actions">
        {error && <p className="note">{error}</p>}
        {!state.busy && state.last_status === "error" && <p className="note">Stopped: {state.last_error}</p>}
        {offline && <p className="note">Reconnecting</p>}
        <button className="primary" disabled={state.busy || (sp ? !sp.waiting : state.done && !a)} onClick={onPrimary}>
          {state.busy && <span className="spinner" aria-hidden />}{primaryLabel(state)}
        </button>
      </footer>}
    </div>
  );
}
