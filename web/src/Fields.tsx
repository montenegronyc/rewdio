import { Field } from "./api";

// One input per wizard field type. Values are kept as the config expects them (numbers, lists, triples).
type Props = { field: Field; value: any; onChange: (v: any) => void };

const num = (s: string) => (s.trim() === "" ? null : Number(s));

export function FieldInput({ field, value, onChange }: Props) {
  const id = `f-${field.path}`;
  const label = <label htmlFor={id}>{field.label}{field.optional && <span className="muted"> (optional)</span>}</label>;
  const help = field.help && <small className="muted">{field.help}</small>;
  switch (field.type) {
    case "bool":
      return (
        <div className="field row">
          <input id={id} type="checkbox" checked={!!value} onChange={(e) => onChange(e.target.checked)} />
          {label}
        </div>
      );
    case "select":
      return (
        <div className="field">{label}
          <select id={id} value={value ?? ""} onChange={(e) => onChange(e.target.value)}>
            {value == null && <option value="">Choose…</option>}
            {(field.options || []).map((o) => <option key={o} value={o}>{o}</option>)}
          </select>{help}</div>
      );
    case "number":
      return (
        <div className="field">{label}
          <input id={id} inputMode="decimal" value={value ?? ""} onChange={(e) => onChange(num(e.target.value))} />{help}</div>
      );
    case "triple":
      return (
        <div className="field">{label}
          <div className="triple">
            {[0, 1, 2].map((i) => (
              <input key={i} aria-label={`${field.label} ${["x", "y", "z"][i]}`} inputMode="decimal"
                value={value?.[i] ?? ""} onChange={(e) => {
                  const v = [...(value || [null, null, null])];
                  v[i] = num(e.target.value);
                  onChange(v.every((x) => x === null) ? null : v);
                }} />
            ))}
          </div>{help}</div>
      );
    case "intlist":
    case "list":
      return (
        <div className="field">{label}
          <input id={id} value={(value || []).join(", ")} onChange={(e) => {
            const parts = e.target.value.split(",").map((x) => x.trim()).filter(Boolean);
            onChange(field.type === "intlist" ? parts.map(Number) : parts);
          }} />{help}</div>
      );
    case "channels":
      return <Channels value={value || []} onChange={onChange} />;
    default: // text, file
      return (
        <div className="field">{label}
          <input id={id} value={value ?? ""} spellCheck={false} autoCapitalize="off"
            placeholder={field.type === "file" ? "/Users/you/Downloads/cal.txt" : undefined}
            onChange={(e) => onChange(e.target.value.trim() === "" ? null : e.target.value.trim())} />{help}</div>
      );
  }
}

type Ch = { role: string; kind: string; dsp_output: number; rew_output_channel?: string | null };

function Channels({ value, onChange }: { value: Ch[]; onChange: (v: Ch[]) => void }) {
  const set = (i: number, patch: Partial<Ch>) => onChange(value.map((c, k) => (k === i ? { ...c, ...patch } : c)));
  return (
    <div className="field">
      <table className="channels">
        <thead><tr><th>Speaker</th><th>Type</th><th>Output</th><th /></tr></thead>
        <tbody>
          {value.map((c, i) => (
            <tr key={i}>
              <td><input aria-label="Speaker name" value={c.role} onChange={(e) => set(i, { role: e.target.value })} /></td>
              <td><select aria-label="Type" value={c.kind} onChange={(e) => set(i, { kind: e.target.value })}>
                <option value="main">main</option><option value="sub">sub</option></select></td>
              <td><input aria-label="Output" inputMode="numeric" value={c.dsp_output}
                onChange={(e) => set(i, { dsp_output: Number(e.target.value) })} /></td>
              <td><button type="button" className="link" aria-label={`Remove ${c.role}`}
                onClick={() => onChange(value.filter((_, k) => k !== i))}>✕</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      <button type="button" className="link" onClick={() => onChange([...value, { role: `SUB${value.filter((c) => c.kind === "sub").length + 1}`,
        kind: "sub", dsp_output: value.length }])}>+ add speaker</button>
    </div>
  );
}
