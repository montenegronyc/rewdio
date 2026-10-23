// Rewdio Live: polls /api/state and redraws. No dependencies.
const $ = (id) => document.getElementById(id);
let selectedPos = null;

try { const t = localStorage.getItem("rewdio-theme"); if (t) document.documentElement.dataset.theme = t; } catch (e) {}
$("theme").onclick = () => {
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  const cur = document.documentElement.dataset.theme || (dark ? "dark" : "light");
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("rewdio-theme", next); } catch (e) {}
};
$("pos").onchange = (e) => { selectedPos = e.target.value; refresh(); };

const fmt = (v, unit = " dB") => (v === undefined || v === null ? "–" : `${v}${unit}`);
const esc = (s) => String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

function chart(el, series) {
  const W = el.clientWidth || 800, H = el.clientHeight || 280, P = { l: 40, r: 8, t: 8, b: 22 };
  const all = Object.values(series).flat();
  if (!all.length) { el.innerHTML = '<p class="muted">No solution yet.</p>'; return; }
  const fx = all.map((p) => p[0]), fy = all.map((p) => p[1]);
  const x0 = Math.log(Math.min(...fx)), x1 = Math.log(Math.max(...fx));
  const ymax = Math.ceil(Math.max(...fy) / 5) * 5 + 5, ymin = ymax - 40;
  const X = (f) => P.l + ((Math.log(f) - x0) / (x1 - x0)) * (W - P.l - P.r);
  const Y = (d) => P.t + ((ymax - Math.max(ymin, Math.min(ymax, d))) / (ymax - ymin)) * (H - P.t - P.b);
  let g = "";
  (W < 600 ? [20, 50, 100, 200, 500] : [20, 30, 50, 80, 100, 200, 300, 500]).forEach((f) => {
    if (Math.log(f) < x0 || Math.log(f) > x1) return;
    g += `<line class="grid-line" x1="${X(f)}" x2="${X(f)}" y1="${P.t}" y2="${H - P.b}"/>`;
    g += `<text class="axis" x="${X(f)}" y="${H - 6}" text-anchor="middle">${f}</text>`;
  });
  for (let d = ymin; d <= ymax; d += 10) {
    g += `<line class="grid-line" x1="${P.l}" x2="${W - P.r}" y1="${Y(d)}" y2="${Y(d)}"/>`;
    g += `<text class="axis" x="${P.l - 6}" y="${Y(d) + 4}" text-anchor="end">${d}</text>`;
  }
  const path = (pts) => pts.map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join("");
  for (const k of ["target", "predicted", "measured"]) if (series[k]) g += `<path class="${k}" d="${path(series[k])}"/>`;
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${g}</svg>`;
}

function render(s) {
  $("phase").textContent = s.phase;
  $("status").textContent = s.done ? (s.measured_ok ? "converged" : "finished at physical limit") : `pass ${s.pass}`;
  $("status").className = "pill " + (s.done && s.measured_ok ? "ok" : "muted");
  $("action").hidden = !s.pending_action;
  if (s.pending_action) $("action-text").textContent = s.pending_action.instructions;
  const m = s.metrics || {};
  $("m-dev").textContent = fmt(m.bass_deviation && m.bass_deviation.std_db);
  $("m-dev-sub").textContent = m.bass_deviation ? `std, ${m.bass_deviation.band_hz[0]}–${m.bass_deviation.band_hz[1]} Hz` : "";
  $("m-xo").textContent = fmt(m.crossover_efficiency_db);
  $("m-pred").textContent = fmt(m.prediction_error_db);
  $("m-sweeps").textContent = s.sweeps;
  $("m-pass").textContent = `pass ${s.pass}`;
  const positions = Object.keys(s.curves || {});
  const sel = $("pos");
  if (sel.options.length !== positions.length) sel.innerHTML = positions.map((p) => `<option>${esc(p)}</option>`).join("");
  if (!selectedPos || !positions.includes(selectedPos)) selectedPos = positions[0];
  sel.value = selectedPos || "";
  chart($("chart"), (s.curves || {})[selectedPos] || {});
  $("dsp").innerHTML = "<tr><th>Ch</th><th>Out</th><th>Gain</th><th>Delay</th><th>Pol</th><th>PEQ</th></tr>" +
    Object.entries(s.dsp).map(([r, o]) => `<tr><td>${esc(r)}</td><td class="num">${o.dsp_output}</td>` +
      `<td class="num">${o.gain_db.toFixed(2)}</td><td class="num">${o.delay_ms.toFixed(2)} ms</td>` +
      `<td>${o.invert ? "inv" : "norm"}</td><td class="num">${Object.keys(o.peq).length}</td></tr>`).join("");
  const checks = [];
  (s.fails || []).forEach((f) => checks.push(["warn", f]));
  (s.quality || []).forEach((q) => checks.push([q.level === "critical" ? "bad" : "warn", q.message]));
  const cc = s.crosscheck || {};
  if (cc.rew_eq_prediction && cc.rew_eq_prediction.agree !== undefined)
    checks.push([cc.rew_eq_prediction.agree ? "ok" : "bad", `REW EQ engine agrees with PEQ maths (${cc.rew_eq_prediction.max_diff_db} dB)`]);
  if (cc.alignment_tool && cc.alignment_tool.delay_ms !== undefined)
    checks.push([cc.alignment_tool.agree ? "ok" : "warn", `REW alignment tool ${cc.alignment_tool.delay_ms.toFixed(2)} ms vs Rewdio ${cc.alignment_tool.rewdio_delay_ms} ms`]);
  ((s.diagnostics || {}).sbir || []).slice(0, 3).forEach((h) => checks.push(["warn", `${h.channel}: ${h.hint}`]));
  $("checks").innerHTML = checks.length ? checks.map(([c, t]) => `<li class="${c}">${esc(t)}</li>`).join("") : "<li>Nothing flagged.</li>";
  $("notes").innerHTML = (s.notes || []).map((n) => `<li>${esc(n)}</li>`).join("") || "<li>No solution yet.</li>";
  $("log").textContent = (s.log || []).join("\n");
  return s.refresh_ms;
}

async function refresh() {
  let wait = 1500;
  try { wait = render(await (await fetch("/api/state")).json()); } catch (e) { $("status").textContent = "disconnected"; }
  return wait;
}
(async function loop() { const w = await refresh(); setTimeout(loop, w || 1500); })();
