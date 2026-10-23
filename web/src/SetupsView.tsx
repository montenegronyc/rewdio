import { useEffect, useState } from "react";
import { DspSetup, getSetups, setupOp } from "./api";
import { Spark } from "./Chart";

// Named, dated DSP setups and an A/B switch. Blind mode hides which is which until you reveal it.
const SLOTS = "rewdio-ab";

function loadSlots(): [string | null, string | null] {
  try { const v = JSON.parse(localStorage.getItem(SLOTS) || "null"); if (Array.isArray(v)) return [v[0], v[1]]; } catch { /* none */ }
  return [null, null];
}
function keepSlots(v: [string | null, string | null]) {
  try { localStorage.setItem(SLOTS, JSON.stringify(v)); } catch { /* per-device convenience only */ }
}
function when(iso: string): string {
  const d = new Date(iso);
  const day = d.toLocaleDateString(undefined, { month: "short", day: "numeric",
    ...(d.getFullYear() !== new Date().getFullYear() ? { year: "numeric" } : {}) });
  return `${day}, ${d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })}`;
}

export function SetupsView({ version, busy }: { version: number; busy: boolean }) {
  const [list, setList] = useState<DspSetup[] | null>(null);
  const [slots, setSlots] = useState(loadSlots);
  const [blind, setBlind] = useState<null | { flip: boolean; revealed: boolean }>(null);
  const [name, setName] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [working, setWorking] = useState(false);

  const load = () => getSetups().then(setList).catch(() => setMsg("Couldn't load setups"));
  useEffect(() => { load(); }, [version]);
  if (!list) return <p className="soft">{msg ?? "Loading"}</p>;

  const byId = (id: string | null) => list.find((d) => d.id === id) || null;
  const run = async (fn: () => Promise<any>) => {
    setWorking(true); setMsg(null);
    try { const r = await fn(); if (r?.ok === false) setMsg(r.message); await load(); }
    catch { setMsg("Couldn't reach Rewdio"); } finally { setWorking(false); }
  };
  const assign = (k: 0 | 1, id: string) => {
    const next: [string | null, string | null] = [...slots];
    next[k] = next[k] === id ? null : id;
    if (next[1 - k] === next[k]) next[1 - k] = null;
    setSlots(next); keepSlots(next); setBlind(null);
  };
  const order = blind?.flip ? [slots[1], slots[0]] : [slots[0], slots[1]];
  const hidden = !!blind && !blind.revealed;
  const label = (k: number) => (blind ? String(k + 1) : k ? "B" : "A");

  return (
    <div className="setups">
      <section className="ab">
        {order.map((id, k) => {
          const d = byId(id);
          return (
            <button key={k} className={"ab-key" + (d?.active && !hidden ? " on" : "")} disabled={!d || busy || working}
              onClick={() => d && run(() => setupOp("recall", { id: d.id }))}>
              <b>{label(k)}</b>
              <span>{d ? (hidden ? "Hidden" : d.name) : "Unassigned"}</span>
            </button>
          );
        })}
      </section>
      <div className="row-buttons">
        <button disabled={!slots[0] || !slots[1]} className={blind ? "on" : ""}
          onClick={() => setBlind(blind ? null : { flip: Math.random() < 0.5, revealed: false })}>Blind</button>
        {blind && <button disabled={blind.revealed} onClick={() => setBlind({ ...blind, revealed: true })}>Reveal</button>}
      </div>

      <ul className="list">
        {list.map((d) => (
          <li key={d.id} className={d.active && !hidden ? "active" : ""}>
            <button className="row-main" disabled={busy || working} onClick={() => run(() => setupOp("recall", { id: d.id }))}>
              <Spark points={d.curve} />
              <span className="row-text"><b>{d.name}</b><span>{when(d.created)}</span></span>
              <span className="row-num">{d.bass_std_db != null ? `${d.bass_std_db.toFixed(1)} dB` : ""}</span>
            </button>
            <span className="row-tools">
              {[0, 1].map((k) => (
                <button key={k} className={"sq" + (slots[k] === d.id ? " on" : "")} aria-label={`Use ${d.name} as ${k ? "B" : "A"}`}
                  onClick={() => assign(k as 0 | 1, d.id)}>{k ? "B" : "A"}</button>
              ))}
              <button className="sq" aria-label={`Delete ${d.name}`}
                onClick={() => window.confirm(`Delete ${d.name}?`) && run(() => setupOp("delete", { id: d.id }))}>×</button>
            </span>
          </li>
        ))}
      </ul>

      <form className="save" onSubmit={(e) => { e.preventDefault(); if (name.trim()) run(() => setupOp("save", { name })).then(() => setName("")); }}>
        <input value={name} placeholder="Name" aria-label="Setup name" onChange={(e) => setName(e.target.value)} />
        <button type="submit" disabled={!name.trim() || working}>Save current</button>
      </form>
      {msg && <p className="note">{msg}</p>}
    </div>
  );
}
