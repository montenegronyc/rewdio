import { useEffect, useState } from "react";
import { Check, getSetup, runChecks, saveSetup, Setup } from "./api";
import { FieldInput } from "./Fields";

// The setup wizard: one step at a time, saved as you go. Same steps as Claude's `setup` prompt.
export function SetupView() {
  const [setup, setSetup] = useState<Setup | null>(null);
  const [i, setI] = useState(0);
  const [draft, setDraft] = useState<Record<string, any>>({});
  const [checks, setChecks] = useState<Record<string, Check>>({});
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => { getSetup().then(setSetup).catch(() => setMsg({ ok: false, text: "Couldn't load settings" })); }, []);
  if (!setup) return <p className="muted">{msg?.text ?? "Loading settings…"}</p>;
  const step = setup.steps[i];
  const value = (path: string, fallback: any) => (path in draft ? draft[path] : fallback);
  const dirty = Object.keys(draft).length > 0;

  const check = async () => {
    if (!step.checks?.length) return;
    try { setChecks({ ...checks, ...(await runChecks(step.checks)) }); } catch { /* shown as unchecked */ }
  };
  const save = async (next: boolean) => {
    setSaving(true); setMsg(null);
    try {
      const res = await saveSetup(draft);
      if (!res.ok) { setMsg({ ok: false, text: res.message || "Not saved" }); return; }
      setSetup(res as Setup); setDraft({});
      setMsg({ ok: true, text: "Saved" });
      await check();
      if (next && i < setup.steps.length - 1) { setI(i + 1); setMsg(null); setChecks({}); }
    } catch { setMsg({ ok: false, text: "Couldn't reach Rewdio" }); } finally { setSaving(false); }
  };

  return (
    <div className="setup">
      <ol className="steps">
        {setup.steps.map((s, k) => (
          <li key={s.id}><button className={k === i ? "on" : ""} onClick={() => { setI(k); setDraft({}); setMsg(null); setChecks({}); }}>
            {k + 1}. {s.title}</button></li>
        ))}
      </ol>
      <section className="card">
        <h2>{step.title}</h2>
        <p className="muted">{step.intro}</p>
        {step.fields.map((f) => (
          <FieldInput key={f.path} field={f} value={value(f.path, f.value)}
            onChange={(v) => setDraft({ ...draft, [f.path]: v })} />
        ))}
        {step.checks?.length ? (
          <div className="checks">
            <button type="button" className="link" onClick={check}>Test</button>
            {step.checks.map((c) => checks[c] && (
              <p key={c} className={checks[c].ok ? "ok" : checks[c].ok === false ? "bad" : "muted"}>
                {checks[c].ok ? "✓" : checks[c].ok === false ? "✗" : "–"} {checks[c].message}</p>
            ))}
          </div>
        ) : null}
        {msg && <p className={msg.ok ? "ok" : "bad"}>{msg.text}</p>}
        <div className="row-buttons">
          {i > 0 && <button type="button" className="secondary" onClick={() => { setI(i - 1); setDraft({}); setMsg(null); }}>Back</button>}
          <button type="button" className="secondary" disabled={!dirty || saving} onClick={() => save(false)}>Save</button>
          <button type="button" className="go" disabled={saving} onClick={() => (dirty ? save(true) : setI(Math.min(i + 1, setup.steps.length - 1)))}>
            {i === setup.steps.length - 1 ? (dirty ? "Save" : "Done") : dirty ? "Save & next" : "Next"}</button>
        </div>
      </section>
      {setup.missing.length > 0 && <p className="muted">Still needed: {setup.missing.join(", ")}.</p>}
      <p className="muted small">Saved to {setup.settings_file}</p>
    </div>
  );
}
