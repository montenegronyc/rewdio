// Talks to Rewdio's remote API. The pairing token arrives once in the URL (?t=...) and is kept.
export type Cell = {
  code: string; row: number; col: number; xyz: number[];
  from_front_m: number; from_left_m: number; enabled: boolean; near_wall: boolean;
  status: "measured" | "current" | "excluded" | "pending"; order: number | null; score_db?: number;
};
export type Pair = { positions: string[]; std_db: number; settings: Record<string, number | boolean> };
export type Crawl = {
  room: number[]; grid: { spacing_m: number; origin_from_front_m: number; origin_from_left_m: number; mic_height_m: number };
  cells: Cell[]; seat: { label: string; xyz: number[] }; current: string | null; setup_needed: boolean;
  progress: { done: number; total: number }; best_pairs: Pair[]; place: Record<string, string> | null;
};
export type Action = { key: string; instructions: string; cell?: string | null; place?: Record<string, string> };
export type State = {
  phase: string; pass: number; done: boolean; sweeps: number; busy: boolean; version: number;
  pending_action: Action | null; crawl: Crawl | null; simulated: boolean;
  last_status?: string | null; last_error?: string | null; measured_ok: boolean | null;
  metrics: Record<string, any> | null; fails: string[] | null; log: string[]; dsp: Record<string, any>;
};

const KEY = "rewdio-token";

export function token(): string {
  const url = new URL(window.location.href);
  const t = url.searchParams.get("t");
  if (t) {
    try { localStorage.setItem(KEY, t); } catch { /* private mode: keep it in the URL */ }
    url.searchParams.delete("t");
    window.history.replaceState(null, "", url.toString());
    (window as any).__rewdioToken = t;
    return t;
  }
  const injected = (window as any).__rewdioToken; // set by Rewdio for pages opened on its own computer
  if (injected) return injected;
  try { return localStorage.getItem(KEY) || ""; } catch { return ""; }
}

const headers = () => ({ "content-type": "application/json", "x-rewdio-token": token() });

export class Unpaired extends Error {}

export async function getState(): Promise<State> {
  const r = await fetch("/api/state", { headers: headers() });
  if (r.status === 401) throw new Unpaired();
  return r.json();
}

async function post(path: string, body: unknown = {}) {
  const r = await fetch(path, { method: "POST", headers: headers(), body: JSON.stringify(body) });
  if (r.status === 401) throw new Unpaired();
  return r.json();
}

export const confirm = (key?: string) => post("/api/confirm", key ? { key } : {});
export const step = () => post("/api/step");
export const setCell = (code: string, enabled: boolean) => post("/api/crawl/cell", { code, enabled });
export const pairing = async () => (await fetch("/api/pair")).ok ? (await fetch("/api/pair")).json() : null;
export const eventsUrl = () => `/api/events?t=${encodeURIComponent(token())}`;

export type Field = { path: string; label: string; type: string; help?: string; optional?: boolean; options?: string[]; value: any };
export type Step = { id: string; title: string; intro: string; fields: Field[]; checks?: string[] };
export type Setup = { settings_file: string; steps: Step[]; missing: string[]; busy: boolean };
export type Check = { ok: boolean | null; message: string };

export async function getSetup(): Promise<Setup> {
  const r = await fetch("/api/setup", { headers: headers() });
  if (r.status === 401) throw new Unpaired();
  return r.json();
}
export async function saveSetup(updates: Record<string, unknown>): Promise<{ ok: boolean; message?: string } & Partial<Setup>> {
  const r = await fetch("/api/setup", { method: "POST", headers: headers(), body: JSON.stringify({ updates }) });
  if (r.status === 401) throw new Unpaired();
  return r.json();
}
export const runChecks = (checks: string[]): Promise<Record<string, Check>> => post("/api/setup/check", { checks });
