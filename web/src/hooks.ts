import { useEffect, useRef, useState } from "react";
import { eventsUrl, getState, State, Unpaired } from "./api";

// Live state: a server-sent event fires on every change; we then fetch the snapshot.
export function useLiveState() {
  const [state, setState] = useState<State | null>(null);
  const [unpaired, setUnpaired] = useState(false);
  const [offline, setOffline] = useState(false);
  useEffect(() => {
    let alive = true;
    const load = () => getState().then((s) => { if (alive) { setState(s); setOffline(false); setUnpaired(false); } })
      .catch((e) => { if (!alive) return; if (e instanceof Unpaired) setUnpaired(true); else setOffline(true); });
    load();
    const es = new EventSource(eventsUrl());
    es.onmessage = load;
    es.onerror = () => setOffline(true);
    const poll = window.setInterval(load, 5000); // belt and braces if the event stream drops
    return () => { alive = false; es.close(); window.clearInterval(poll); };
  }, []);
  return { state, unpaired, offline };
}

// Keep the phone screen on while you walk the grid.
export function useWakeLock(active: boolean) {
  const lock = useRef<any>(null);
  useEffect(() => {
    const nav = navigator as any;
    if (!active || !nav.wakeLock) return;
    let cancelled = false;
    const take = () => nav.wakeLock.request("screen").then((l: any) => { if (cancelled) l.release(); else lock.current = l; })
      .catch(() => undefined);
    take();
    const again = () => document.visibilityState === "visible" && take();
    document.addEventListener("visibilitychange", again);
    return () => { cancelled = true; document.removeEventListener("visibilitychange", again); lock.current?.release?.(); };
  }, [active]);
}

// A short buzz when there's somewhere new to go (Android; iOS ignores it).
export function useBuzzOnChange(value: string | null | undefined) {
  const prev = useRef(value);
  useEffect(() => {
    if (value && value !== prev.current) (navigator as any).vibrate?.([60, 40, 60]);
    prev.current = value;
  }, [value]);
}
