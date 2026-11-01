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

// Spoken turntable prompts with the phone's own voice. Browsers only speak after a tap, so the card has a
// "Voice on" switch; the choice is kept on this phone. British English female voice when the phone has one.
const VOICE_KEY = "rewdio-voice";
const FEMALE_GB = ["Serena", "Kate", "Stephanie", "Martha", "Female"];

export function voiceEnabled(): boolean {
  try { return localStorage.getItem(VOICE_KEY) === "on"; } catch { return false; }
}

export function setVoiceEnabled(on: boolean) {
  try { localStorage.setItem(VOICE_KEY, on ? "on" : "off"); } catch { /* private mode: this page only */ }
  if (on) speak("Voice on.");
}

function bestVoice(): SpeechSynthesisVoice | undefined {
  const all = window.speechSynthesis?.getVoices() ?? [];
  const gb = all.filter((v) => v.lang.replace("_", "-").toLowerCase() === "en-gb");
  for (const q of ["Premium", "Enhanced", ""]) {
    const v = gb.find((x) => FEMALE_GB.some((n) => x.name.includes(n)) && x.name.includes(q));
    if (v) return v;
  }
  return gb[0];
}

export function speak(text: string) {
  const s = window.speechSynthesis;
  if (!s || !text) return;
  s.cancel();
  const u = new SpeechSynthesisUtterance(text);
  const v = bestVoice();
  if (v) u.voice = v;
  u.lang = "en-GB";
  u.rate = 1.0;
  s.speak(u);
}

// Say `text` whenever `id` changes (a new prompt, a new result), if the voice is on.
export function useSpeakOnChange(id: string | null | undefined, text: string | null | undefined, on: boolean) {
  const prev = useRef(id);
  useEffect(() => {
    if (on && id && id !== prev.current && text) speak(text);
    prev.current = id;
  }, [id, text, on]);
}

// A short buzz when there's somewhere new to go (Android; iOS ignores it).
export function useBuzzOnChange(value: string | null | undefined) {
  const prev = useRef(value);
  useEffect(() => {
    if (value && value !== prev.current) (navigator as any).vibrate?.([60, 40, 60]);
    prev.current = value;
  }, [value]);
}
