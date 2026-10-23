"""Whole-system solve: levels, timing, polarity, sub integration, optional PEQ.

Input is every channel measured alone at every mic position, already on one time
base (loopback, or sum-fit corrected), each tagged with the DSP state it was
measured under. Output is a proposed DSP state plus the predicted result, so the
engine can apply it and verify against a real sweep.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..config import SystemConfig
from ..minidsp.state import DspState, OutputState, change_transfer
from .alignment import arrival_ms, best_alignment, optimize_pair
from .balance import difference_db, solve_balance, stats
from .gating import DirectSegment, gated_mag
from .metrics import deviation, summation_efficiency_db
from .peq import design_peq
from .response import Response, band_level_db, band_median_db, spatial_average_db
from .target import target_db

Data = dict[str, dict[str, "Measured"]]  # position -> role -> measurement


@dataclass
class Measured:
    resp: Response
    snapshot: OutputState
    direct: DirectSegment | None = None  # direct-sound impulse (mains only), for L/R balance
    level_offset_db: float = 0.0  # applied to `direct` to match the common drive level


def balance_gate_ms(cfg, measured: list[Measured]) -> float | None:
    """One gate for every measurement compared: the shortest any of them allows."""
    gates = [m.direct.max_gate_ms for m in measured if m.direct is not None]
    return min(gates) if (cfg.balance.use_gating and gates and len(gates) == len(measured)) else None


def balance_band(cfg, gate_ms: float | None) -> tuple[float, float]:
    """The balance band, raised to what the gate can actually resolve."""
    lo, hi = cfg.balance.band_hz
    if gate_ms:
        lo = max(lo, 1000.0 * cfg.balance.resolution_cycles / gate_ms)
    return lo, hi


def balance_view(meas: Measured, state: OutputState, fs: float, cfg, gate_ms: float | None) -> Response:
    """Magnitude used for L/R balance under `state`: gated direct sound if available, else steady state."""
    if gate_ms is None:
        return predict(meas, state, fs)
    g = gated_mag(meas.direct, meas.resp.freqs, gate_ms, cfg.balance) + meas.level_offset_db
    ch = 20 * np.log10(np.abs(change_transfer(meas.snapshot, state, meas.resp.freqs, fs)))
    return Response(meas.resp.freqs, 10 ** ((g + ch) / 20).astype(complex), has_phase=False)


@dataclass
class Proposal:
    state: DspState
    predicted: dict[str, Response]  # position -> full-system prediction
    metrics: dict
    notes: list[str] = field(default_factory=list)


def predict(meas: Measured, state: OutputState, fs: float) -> Response:
    return meas.resp.scaled(change_transfer(meas.snapshot, state, meas.resp.freqs, fs))


def _sum(rs: list[Response]) -> Response:
    out = rs[0]
    for r in rs[1:]:
        out = out + r
    return out


def group(data: Data, st: DspState, pos: str, roles: list[str], fs: float) -> Response:
    return _sum([predict(data[pos][r], st.get(r), fs) for r in roles])


def system_response(data: Data, state: DspState, pos: str, fs: float) -> Response:
    roles = [r for r in data[pos] if not state.get(r).mute]
    return group(data, state, pos, roles, fs)


class _Solve:
    def __init__(self, cfg: SystemConfig, data: Data, current: DspState):
        self.cfg, self.data = cfg, data
        self.t = cfg.tuning
        self.fs = float(cfg.dsp_sample_rate)
        self.positions = list(data.keys())
        self.mains = [c.role for c in cfg.mains]
        self.subs = [c.role for c in cfg.subs]
        self.fc = cfg.crossover_hz
        self.freqs = next(iter(data[self.positions[0]].values())).resp.freqs
        self.tgt = target_db(self.freqs, cfg.target)
        self.notes: list[str] = []
        self.st = DspState.model_validate(current.model_dump())
        for c in cfg.channels:
            o = self.st.get(c.role)
            o.mute = False
            o.peq = {}  # managed PEQ is re-derived from scratch (or left off) on every solve

    def each(self, roles):
        return [group(self.data, self.st, p, roles, self.fs) for p in self.positions]

    def mains_level(self) -> None:
        lo, hi = self.t.bands.mains_level_hz
        l0 = {r: np.mean([band_level_db(self.data[p][r].resp, None, lo, hi) for p in self.positions])
              - self.data[self.positions[0]][r].snapshot.gain_db for r in self.mains}
        ref = min(l0.values())
        for r in self.mains:
            self.st.get(r).gain_db = round(self.cfg.baseline.gain_db + ref - l0[r], 2)
        self.notes.append("mains level match: " + ", ".join(f"{r} {self.st.get(r).gain_db:+.2f} dB" for r in self.mains))

    def mains_balance(self) -> None:
        """L/R centre image: trim the louder side, then split broad corrective filters across both."""
        bs = self.cfg.balance
        left_role, right_role = bs.pair
        neutral = OutputState(gain_db=self.cfg.baseline.gain_db)
        gate = balance_gate_ms(self.cfg, [self.data[p][r] for p in self.positions for r in bs.pair])
        left = [balance_view(self.data[p][left_role], neutral, self.fs, self.cfg, gate) for p in self.positions]
        right = [balance_view(self.data[p][right_role], neutral, self.fs, self.cfg, gate) for p in self.positions]
        band_hz = balance_band(self.cfg, gate)
        res = solve_balance(left, right, self.fs, bs, band_hz)
        base = self.cfg.baseline.gain_db
        trim = res.broadband_db if abs(res.broadband_db) >= self.t.restraint.balance_trim_deadband_db else 0.0
        self.st.get(left_role).gain_db = round(base - max(-trim, 0.0), 2)
        self.st.get(right_role).gain_db = round(base - max(trim, 0.0), 2)
        for role, side in ((left_role, "L"), (right_role, "R")):
            self.st.get(role).peq = dict(zip(bs.peq_slots, res.peq(side)))
        louder = right_role if res.broadband_db > 0 else left_role
        self.notes.append(
            f"L/R balance: {louder} {abs(res.broadband_db):.2f} dB louder overall, trimmed; band difference std "
            f"{res.before['std_db']} -> {res.after['std_db']} dB (max {res.before['max_abs_db']} -> "
            f"{res.after['max_abs_db']} dB) over {band_hz[0]:.0f}-{band_hz[1]:.0f} Hz with {len(res.filters)} split filter(s): "
            + "; ".join(f"{fc:.0f} Hz {g:+.1f} dB Q{q:.2f}" for fc, g, q in res.filters)
            + (f". Direct-sound gate {gate:.1f} ms (first reflection), so balance below {band_hz[0]:.0f} Hz is not "
               "judged; more mic positions around the seat average out reflections" if gate else ""))

    def mains_timing(self) -> None:
        p0 = self.positions[0]
        arr = {r: arrival_ms(self.data[p0][r].resp, self.t.alignment) - self.data[p0][r].snapshot.delay_ms
               for r in self.mains}
        latest = max(arr.values())
        for r in self.mains:
            self.st.get(r).delay_ms = round(latest - arr[r], 3)
        self.notes.append("mains arrival (ms, zero delay): " + ", ".join(f"{r} {arr[r]:.2f}" for r in self.mains))

    def sub_to_sub(self) -> None:
        b = self.t.bands
        s0 = self.st.get(self.subs[0])
        s0.gain_db, s0.delay_ms, s0.invert = self.cfg.baseline.gain_db, self.cfg.baseline.delay_ms, False
        bundle = [self.subs[0]]
        hi = min(self.fc * b.sub_pair_xo_factor, b.sub_pair_max_hz)
        for r in self.subs[1:]:
            probe = OutputState(gain_db=self.cfg.baseline.gain_db)
            b_list = [predict(self.data[p][r], probe, self.fs) for p in self.positions]
            sol = optimize_pair(self.each(bundle), b_list, self.tgt, b.sub_pair_lo_hz, hi, self.cfg.dsp_max_delay_ms,
                                self.t.pair)
            o = self.st.get(r)
            o.gain_db, o.invert = round(self.cfg.baseline.gain_db + sol.gain_db, 2), sol.invert
            if sol.delay_ms >= 0:
                o.delay_ms = round(sol.delay_ms, 3)
            else:  # delay the existing bundle instead
                for x in bundle:
                    self.st.get(x).delay_ms = round(self.st.get(x).delay_ms - sol.delay_ms, 3)
                o.delay_ms = 0.0
            bundle.append(r)
            self.notes.append(f"{r} vs {'+'.join(bundle[:-1])}: delay {sol.delay_ms:+.2f} ms, gain {sol.gain_db:+.2f} dB, "
                              f"{'inverted' if sol.invert else 'normal'} polarity, in-band std {sol.std_db:.2f} dB")

    def bundle_level_and_align(self, mains_base: dict[str, float]) -> None:
        b, cfg, fc = self.t.bands, self.cfg, self.fc
        for r in self.mains:  # undo any headroom cut from a previous iteration before re-levelling
            self.st.get(r).gain_db = mains_base[r]
        lo_s, hi_s = b.sub_level_lo_hz, fc * b.sub_level_xo_factor
        ref = np.mean([band_median_db(m, *b.mains_level_hz, b.level_smoothing) for m in self.each(self.mains)])
        lift = float(np.mean(self.tgt[(self.freqs >= lo_s) & (self.freqs <= hi_s)]))
        have = np.mean([band_median_db(x, lo_s, hi_s, b.level_smoothing) for x in self.each(self.subs)])
        lo_g, hi_g = cfg.dsp_gain_range_db
        hi_g = min(hi_g, cfg.max_sub_gain_db)
        delta = ref + lift - have
        excess = max(max(self.st.get(r).gain_db for r in self.subs) + delta - hi_g, 0.0)
        if excess > 0:  # subs can't get there with headroom to spare: bring the mains down instead
            for r in self.mains:
                self.st.get(r).gain_db = round(self.st.get(r).gain_db - excess, 2)
            delta -= excess
            self.notes.append(f"subs short of target by {excess:.1f} dB at +{hi_g:.0f} dB max sub gain: lowered mains "
                              f"{excess:.1f} dB instead (raise master volume to compensate)")
        for r in self.subs:
            o = self.st.get(r)
            o.gain_db = round(float(np.clip(o.gain_db + delta, lo_g, hi_g)), 2)
        self.notes.append(f"sub bundle level {delta:+.2f} dB (target = mains mid level {ref - excess:.1f} dB "
                          f"+ {lift:.1f} dB house curve)")

        sub_d = [self.st.get(r).delay_ms for r in self.subs]
        main_d = [self.st.get(r).delay_ms for r in self.mains]

        def feasible(d):
            allv = [x + max(d, 0) for x in sub_d] + [x + max(-d, 0) for x in main_d]
            return max(allv) - min(allv) <= cfg.dsp_max_delay_ms + 1e-9

        k = b.crossover_span_factor
        al = best_alignment(self.each(self.mains), self.each(self.subs), fc / k, fc * k,
                            (-cfg.dsp_max_delay_ms, cfg.dsp_max_delay_ms), self.t.alignment, feasible=feasible)
        for r in self.subs:
            o = self.st.get(r)
            o.delay_ms = round(o.delay_ms + max(al.delay_ms, 0.0), 3)
            o.invert = o.invert != al.invert
        for r in self.mains:
            self.st.get(r).delay_ms = round(self.st.get(r).delay_ms + max(-al.delay_ms, 0.0), 3)
        self.notes.append(f"subs vs mains: {'subs' if al.delay_ms >= 0 else 'mains'} delayed {abs(al.delay_ms):.2f} ms, "
                          f"{'flip sub polarity' if al.invert else 'polarity unchanged'}, "
                          f"crossover summation {al.efficiency_db:.2f} dB")

    def peq(self) -> None:
        b = self.t.bands
        for r in self.subs:
            self.st.get(r).peq = {}
        others = [m.h for m in self.each(self.mains)] if self.mains else [np.zeros(self.freqs.size, complex)] * len(self.positions)
        res = design_peq(self.freqs, others, [s.h for s in self.each(self.subs)], self.tgt, b.peq_lo_hz,
                         min(self.fc * b.peq_xo_factor, b.peq_max_hz), len(self.cfg.peq_slots), self.fs,
                         self.cfg.max_peq_boost_db, self.t.peq)
        for r in self.subs:
            self.st.get(r).peq = {slot: f for slot, f in zip(self.cfg.peq_slots, res.filters)}
        held = f", held-out seats {res.held_out_gain_db:+.2f} dB" if res.held_out_gain_db is not None else ""
        self.notes.append(f"sub PEQ: {len(res.filters)} filters, EQ objective {res.before_std_db} -> {res.after_std_db} dB{held}: "
                          + "; ".join(f"{f.freq_hz:.0f} Hz {f.gain_db:+.1f} dB Q{f.q}" for f in res.filters))

    def deadband(self, current: DspState) -> None:
        """Don't touch the DSP for changes too small to matter."""
        rt = self.t.restraint
        kept = []
        for c in self.cfg.channels:
            new, cur = self.st.get(c.role), current.get(c.role)
            if new.gain_db != cur.gain_db and abs(new.gain_db - cur.gain_db) < rt.gain_deadband_db:
                new.gain_db = cur.gain_db
                kept.append(f"{c.role} gain")
            if new.delay_ms != cur.delay_ms and abs(new.delay_ms - cur.delay_ms) < rt.delay_deadband_ms:
                new.delay_ms = cur.delay_ms
                kept.append(f"{c.role} delay")
        if kept:
            self.notes.append("left unchanged (difference below deadband): " + ", ".join(kept))

    def normalize_delays(self) -> None:
        roles = [c.role for c in self.cfg.channels]
        lo = min(self.st.get(r).delay_ms for r in roles)
        for r in roles:
            o = self.st.get(r)
            o.delay_ms = round(o.delay_ms - lo, 2)
            if o.delay_ms > self.cfg.dsp_max_delay_ms:
                self.notes.append(f"WARNING: {r} needs {o.delay_ms:.2f} ms, above the "
                                  f"{self.cfg.dsp_max_delay_ms} ms DSP limit; clamped")
                o.delay_ms = self.cfg.dsp_max_delay_ms


def solve(cfg: SystemConfig, data: Data, current: DspState, use_peq: bool = False) -> Proposal:
    s = _Solve(cfg, data, current)
    pair_ok = cfg.balance.enabled and set(cfg.balance.pair) <= set(s.mains)
    if pair_ok:
        s.mains_balance()
    elif s.mains:
        s.mains_level()
    if len(s.mains) > 1:
        s.mains_timing()
    if s.subs:
        s.sub_to_sub()
    mains_base = {r: s.st.get(r).gain_db for r in s.mains}
    best = None
    for _ in range(3 if use_peq else 1):  # PEQ shifts sub phase, so re-align after it
        if s.subs and s.mains:
            s.bundle_level_and_align(mains_base)
        if use_peq and s.subs:
            s.peq()
        # keep the best round, not the last one: alternating fits can wander
        m = evaluate(cfg, data, s.st, {p: system_response(data, s.st, p, s.fs) for p in s.positions})
        shortfall = max(0.0, cfg.convergence.min_crossover_efficiency_db - m.get("crossover_efficiency_db", 0.0))
        score = m["bass_deviation"]["std_db"] + 2.0 * shortfall
        if best is None or score < best[0]:
            best = (score, DspState.model_validate(s.st.model_dump()), list(s.notes))
    s.st, s.notes = best[1], best[2]
    s.normalize_delays()
    s.deadband(current)
    predicted = {p: system_response(data, s.st, p, s.fs) for p in s.positions}
    return Proposal(state=s.st, predicted=predicted, metrics=evaluate(cfg, data, s.st, predicted), notes=s.notes)


def evaluate(cfg: SystemConfig, data: Data, st: DspState, system: dict[str, Response]) -> dict:
    """Convergence metrics for a state. `system` = full-system response per position."""
    fs = float(cfg.dsp_sample_rate)
    b = cfg.tuning.bands
    positions = list(data.keys())
    freqs = system[positions[0]].freqs
    mains = [c.role for c in cfg.mains]
    subs = [c.role for c in cfg.subs]
    out: dict = {}
    if len(mains) > 1:
        lv = [np.mean([band_level_db(predict(data[p][r], st.get(r), fs), None, *b.mains_level_hz) for p in positions])
              for r in mains]
        out["mains_level_spread_db"] = round(float(max(lv) - min(lv)), 2)
    if cfg.balance.enabled and set(cfg.balance.pair) <= set(mains):
        lr, rr = cfg.balance.pair
        gate = balance_gate_ms(cfg, [data[p][r] for p in positions for r in (lr, rr)])
        blo, bhi = balance_band(cfg, gate)
        m = (freqs >= blo) & (freqs <= bhi)
        d = difference_db([balance_view(data[p][lr], st.get(lr), fs, cfg, gate) for p in positions],
                          [balance_view(data[p][rr], st.get(rr), fs, cfg, gate) for p in positions], cfg.balance.smoothing)[m]
        out["lr_balance"] = stats(d)
        out["mains_level_spread_db"] = round(abs(out["lr_balance"]["median_db"]), 2)
    if mains and subs:
        k = b.crossover_span_factor
        eff = [summation_efficiency_db(group(data, st, p, mains, fs), group(data, st, p, subs, fs),
                                       cfg.crossover_hz / k, cfg.crossover_hz * k) for p in positions]
        out["crossover_efficiency_db"] = round(float(np.mean(eff)), 2)
    avg = spatial_average_db(list(system.values()))
    out["bass_deviation"] = deviation(freqs, avg, target_db(freqs, cfg.target), *b.bass_eval_hz, b.bass_smoothing)
    return out


def converged(cfg: SystemConfig, metrics: dict) -> tuple[bool, list[str]]:
    c = cfg.convergence
    fails = []
    if metrics.get("mains_level_spread_db", 0) > c.lr_level_tolerance_db:
        fails.append(f"mains level spread {metrics['mains_level_spread_db']} dB > {c.lr_level_tolerance_db}")
    if metrics.get("crossover_efficiency_db", 0) < c.min_crossover_efficiency_db:
        fails.append(f"crossover summation {metrics['crossover_efficiency_db']} dB < {c.min_crossover_efficiency_db}")
    if "lr_balance" in metrics and metrics["lr_balance"]["std_db"] > c.lr_balance_max_std_db:
        fails.append(f"L/R balance std {metrics['lr_balance']['std_db']} dB > {c.lr_balance_max_std_db}")
    if metrics["bass_deviation"]["std_db"] > c.max_bass_deviation_db:
        fails.append(f"bass deviation std {metrics['bass_deviation']['std_db']} dB > {c.max_bass_deviation_db}")
    return (not fails), fails


def footprint(cfg: SystemConfig, st: DspState) -> dict:
    """How much DSP the solution uses, so 'light' is visible."""
    filters = [(c.role, p) for c in cfg.channels for p in st.get(c.role).peq.values()]
    return {"filters": len(filters), "total_abs_eq_db": round(sum(abs(p.gain_db) for _, p in filters), 1),
            "max_cut_db": round(min([p.gain_db for _, p in filters] or [0.0]), 1),
            "max_boost_db": round(max([p.gain_db for _, p in filters] or [0.0]), 1),
            "inverted": [c.role for c in cfg.channels if st.get(c.role).invert],
            "gain_range_db": [round(min(st.get(c.role).gain_db for c in cfg.channels), 2),
                              round(max(st.get(c.role).gain_db for c in cfg.channels), 2)]}


def peq_band_worst_peak(cfg: SystemConfig, predicted: dict[str, Response]) -> float:
    """Worst peak above target within the band sub PEQ can act on (spatial average)."""
    b = cfg.tuning.bands
    resp = list(predicted.values())
    f = resp[0].freqs
    hi = min(cfg.crossover_hz * b.peq_xo_factor, b.peq_max_hz)
    return deviation(f, spatial_average_db(resp), target_db(f, cfg.target), b.peq_lo_hz, hi, b.bass_smoothing)["max_db"]


def peq_earns_place(cfg: SystemConfig, without: Proposal, with_peq: Proposal) -> tuple[bool, str]:
    """PEQ is the last rung. It is used only if it clearly fixes peaks (the only thing cuts can fix)
    and never makes the overall response worse."""
    pq = cfg.tuning.peq
    a, b = without.metrics["bass_deviation"], with_peq.metrics["bass_deviation"]
    excess = a["peak_excess_db"] - b["peak_excess_db"]
    worst = peq_band_worst_peak(cfg, without.predicted) - peq_band_worst_peak(cfg, with_peq.predicted)
    ok = (excess >= pq.min_total_improvement_db or worst >= pq.min_peak_reduction_db) and b["std_db"] <= a["std_db"]
    detail = (f"worst peak in its band {worst:.1f} dB lower (bar {pq.min_peak_reduction_db}), peak excess "
              f"{excess:.2f} dB lower (bar {pq.min_total_improvement_db}), bass std {a['std_db']} -> {b['std_db']} dB")
    return ok, ("PEQ earned its place: " if ok else "PEQ considered and rejected: ") + detail
