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
from .metrics import deviation, summation_efficiency_db
from .peq import design_peq
from .response import Response, band_level_db, band_median_db, spatial_average_db
from .target import target_db

Data = dict[str, dict[str, "Measured"]]  # position -> role -> measurement


@dataclass
class Measured:
    resp: Response
    snapshot: OutputState


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
        self.notes.append(f"sub PEQ: {len(res.filters)} filters, bass std {res.before_std_db} -> {res.after_std_db} dB{held}: "
                          + "; ".join(f"{f.freq_hz:.0f} Hz {f.gain_db:+.1f} dB Q{f.q}" for f in res.filters))

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
    if s.mains:
        s.mains_level()
    if len(s.mains) > 1:
        s.mains_timing()
    if s.subs:
        s.sub_to_sub()
    mains_base = {r: s.st.get(r).gain_db for r in s.mains}
    for _ in range(3 if use_peq else 1):  # PEQ shifts sub phase, so re-align after it
        if s.subs and s.mains:
            s.bundle_level_and_align(mains_base)
        if use_peq and s.subs:
            s.peq()
    s.normalize_delays()
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
    if metrics["bass_deviation"]["std_db"] > c.max_bass_deviation_db:
        fails.append(f"bass deviation std {metrics['bass_deviation']['std_db']} dB > {c.max_bass_deviation_db}")
    return (not fails), fails
