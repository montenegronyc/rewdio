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


def system_response(data: dict[str, dict[str, Measured]], state: DspState, pos: str, fs: float,
                    roles: list[str] | None = None) -> Response:
    roles = roles or list(data[pos].keys())
    parts = [predict(data[pos][r], state.get(r), fs) for r in roles if not state.get(r).mute]
    out = parts[0]
    for p in parts[1:]:
        out = out + p
    return out


def _copy(state: DspState) -> DspState:
    return DspState.model_validate(state.model_dump())


def solve(cfg: SystemConfig, data: dict[str, dict[str, Measured]], current: DspState,
          use_peq: bool = False) -> Proposal:
    fs = float(cfg.dsp_sample_rate)
    positions = list(data.keys())
    mains = [c.role for c in cfg.mains]
    subs = [c.role for c in cfg.subs]
    fc = cfg.crossover_hz
    freqs = next(iter(data[positions[0]].values())).resp.freqs
    tgt = target_db(freqs, cfg.target)
    notes: list[str] = []
    st = _copy(current)
    for c in cfg.channels:
        o = st.get(c.role)
        o.mute = False
        o.peq = {}  # managed PEQ is re-derived from scratch (or left off) on every solve

    # 1. Mains level match: cut louder mains to the quietest, never boost.
    if mains:
        l0 = {}
        for r in mains:
            lv = np.mean([band_level_db(data[p][r].resp, None, 500, 2000) for p in positions])
            l0[r] = lv - data[positions[0]][r].snapshot.gain_db
        ref = min(l0.values())
        for r in mains:
            st.get(r).gain_db = round(cfg.baseline.gain_db + ref - l0[r], 2)
        notes.append("mains level match: " + ", ".join(f"{r} {st.get(r).gain_db:+.2f} dB" for r in mains))

    # 2. Mains time alignment at the main seat (direct-sound arrival, 1-3 kHz group delay).
    if len(mains) > 1:
        p0 = positions[0]
        arr = {r: arrival_ms(data[p0][r].resp) - data[p0][r].snapshot.delay_ms for r in mains}
        latest = max(arr.values())
        for r in mains:
            st.get(r).delay_ms = round(latest - arr[r], 3)
        notes.append("mains arrival (ms, zero delay): " + ", ".join(f"{r} {arr[r]:.2f}" for r in mains))

    # 3. Sub-to-sub integration (greedy: each extra sub against the running bundle).
    bundle_roles: list[str] = []
    if subs:
        bundle_roles = [subs[0]]
        s0 = st.get(subs[0])
        s0.gain_db, s0.delay_ms, s0.invert = cfg.baseline.gain_db, 0.0, False
        for r in subs[1:]:
            a_list = [_sum([predict(data[p][b], st.get(b), fs) for b in bundle_roles]) for p in positions]
            probe = OutputState(gain_db=cfg.baseline.gain_db)
            b_list = [predict(data[p][r], probe, fs) for p in positions]
            sol = optimize_pair(a_list, b_list, tgt, 20.0, min(fc * 1.5, 200.0), cfg.dsp_max_delay_ms)
            o = st.get(r)
            o.gain_db = round(cfg.baseline.gain_db + sol.gain_db, 2)
            o.invert = sol.invert
            if sol.delay_ms >= 0:
                o.delay_ms = round(sol.delay_ms, 3)
            else:  # delay the existing bundle instead
                for b in bundle_roles:
                    st.get(b).delay_ms = round(st.get(b).delay_ms - sol.delay_ms, 3)
                o.delay_ms = 0.0
            bundle_roles.append(r)
            notes.append(f"{r} vs {'+'.join(bundle_roles[:-1])}: delay {sol.delay_ms:+.2f} ms, gain {sol.gain_db:+.2f} dB, "
                         f"{'inverted' if sol.invert else 'normal'} polarity, in-band std {sol.std_db:.2f} dB")

    mains_base = {r: st.get(r).gain_db for r in mains}
    for _ in range(3 if use_peq else 1):
        if subs and mains:
            _level_and_align_bundle(cfg, data, st, positions, mains, subs, tgt, fs, notes, mains_base)
        if use_peq and subs:
            _apply_peq(cfg, data, st, positions, mains, subs, tgt, fs, notes)
    _normalize_delays(cfg, st, notes)

    predicted = {p: system_response(data, st, p, fs) for p in positions}
    metrics = evaluate(cfg, data, st, predicted)
    return Proposal(state=st, predicted=predicted, metrics=metrics, notes=notes)


def _sum(rs: list[Response]) -> Response:
    out = rs[0]
    for r in rs[1:]:
        out = out + r
    return out


def _level_and_align_bundle(cfg, data, st, positions, mains, subs, tgt, fs, notes, mains_base):
    fc = cfg.crossover_hz
    for r in mains:  # undo any headroom cut from a previous iteration before re-levelling
        st.get(r).gain_db = mains_base[r]
    freqs = next(iter(data[positions[0]].values())).resp.freqs
    # Level: bundle (sub band) sits at the mains-sum mid level plus the house-curve lift.
    mains_sum = [_sum([predict(data[p][r], st.get(r), fs) for r in mains]) for p in positions]
    bundle = [_sum([predict(data[p][r], st.get(r), fs) for r in subs]) for p in positions]
    lo_s, hi_s = 25.0, fc * 0.8
    ref = np.mean([band_median_db(m, 500, 2000) for m in mains_sum])
    lift = float(np.mean(tgt[(freqs >= lo_s) & (freqs <= hi_s)]))
    have = np.mean([band_median_db(b, lo_s, hi_s) for b in bundle])
    lo_g, hi_g = cfg.dsp_gain_range_db
    hi_g = min(hi_g, cfg.max_sub_gain_db)
    delta = ref + lift - have
    excess = max(max(st.get(r).gain_db for r in subs) + delta - hi_g, 0.0)
    if excess > 0:
        # Subs cannot get there with headroom to spare: bring the mains down instead.
        for r in mains:
            st.get(r).gain_db = round(st.get(r).gain_db - excess, 2)
        delta -= excess
        notes.append(f"subs short of target by {excess:.1f} dB at +{hi_g:.0f} dB max sub gain: lowered mains "
                     f"{excess:.1f} dB instead (raise master volume to compensate)")
    for r in subs:
        o = st.get(r)
        o.gain_db = round(float(np.clip(o.gain_db + delta, lo_g, hi_g)), 2)
    notes.append(f"sub bundle level {delta:+.2f} dB (target = mains mid level {ref - excess:.1f} dB + {lift:.1f} dB house curve)")

    # Timing/polarity of the bundle against the mains through the crossover region.
    bundle = [_sum([predict(data[p][r], st.get(r), fs) for r in subs]) for p in positions]
    mains_sum = [_sum([predict(data[p][r], st.get(r), fs) for r in mains]) for p in positions]
    sub_delays = [st.get(r).delay_ms for r in subs]
    main_delays = [st.get(r).delay_ms for r in mains]

    def feasible(d):
        s = [x + max(d, 0) for x in sub_delays]
        m = [x + max(-d, 0) for x in main_delays]
        allv = s + m
        return max(allv) - min(allv) <= cfg.dsp_max_delay_ms + 1e-9

    span = cfg.dsp_max_delay_ms
    al = best_alignment(mains_sum, bundle, fc / 2, fc * 2, (-span, span), feasible=feasible)
    for r in subs:
        o = st.get(r)
        o.delay_ms = round(o.delay_ms + max(al.delay_ms, 0.0), 3)
        if al.invert:
            o.invert = not o.invert
    for r in mains:
        st.get(r).delay_ms = round(st.get(r).delay_ms + max(-al.delay_ms, 0.0), 3)
    notes.append(f"subs vs mains: {'subs' if al.delay_ms >= 0 else 'mains'} delayed {abs(al.delay_ms):.2f} ms, "
                 f"{'flip sub polarity' if al.invert else 'polarity unchanged'}, crossover summation {al.efficiency_db:.2f} dB")


def _apply_peq(cfg, data, st, positions, mains, subs, tgt, fs, notes):
    fc = cfg.crossover_hz
    freqs = next(iter(data[positions[0]].values())).resp.freqs
    for r in subs:
        st.get(r).peq = {}
    others = [(_sum([predict(data[p][r], st.get(r), fs) for r in mains]).h if mains else np.zeros(freqs.size, complex))
              for p in positions]
    subsum = [_sum([predict(data[p][r], st.get(r), fs) for r in subs]).h for p in positions]
    res = design_peq(freqs, others, subsum, tgt, 18.0, min(fc * 1.1, 200.0), len(cfg.peq_slots), fs,
                     max_boost_db=cfg.max_peq_boost_db)
    for r in subs:
        st.get(r).peq = {slot: f for slot, f in zip(cfg.peq_slots, res.filters)}
    notes.append(f"sub PEQ: {len(res.filters)} filters, bass std {res.before_std_db} -> {res.after_std_db} dB: "
                 + "; ".join(f"{f.freq_hz:.0f} Hz {f.gain_db:+.1f} dB Q{f.q}" for f in res.filters))


def _normalize_delays(cfg, st: DspState, notes):
    roles = [c.role for c in cfg.channels]
    lo = min(st.get(r).delay_ms for r in roles)
    for r in roles:
        o = st.get(r)
        o.delay_ms = round(o.delay_ms - lo, 2)
        if o.delay_ms > cfg.dsp_max_delay_ms:
            notes.append(f"WARNING: {r} needs {o.delay_ms:.2f} ms, above the {cfg.dsp_max_delay_ms} ms DSP limit; clamped")
            o.delay_ms = cfg.dsp_max_delay_ms


def evaluate(cfg: SystemConfig, data: dict[str, dict[str, Measured]], st: DspState,
             system: dict[str, Response]) -> dict:
    """Convergence metrics for a state. `system` = full-system response per position."""
    fs = float(cfg.dsp_sample_rate)
    positions = list(data.keys())
    freqs = system[positions[0]].freqs
    tgt = target_db(freqs, cfg.target)
    mains = [c.role for c in cfg.mains]
    subs = [c.role for c in cfg.subs]
    out: dict = {}
    if len(mains) > 1:
        lv = [np.mean([band_level_db(predict(data[p][r], st.get(r), fs), None, 500, 2000) for p in positions])
              for r in mains]
        out["mains_level_spread_db"] = round(float(max(lv) - min(lv)), 2)
    if mains and subs:
        eff = []
        for p in positions:
            b = _sum([predict(data[p][r], st.get(r), fs) for r in subs])
            m = _sum([predict(data[p][r], st.get(r), fs) for r in mains])
            eff.append(summation_efficiency_db(m, b, cfg.crossover_hz / 2, cfg.crossover_hz * 2))
        out["crossover_efficiency_db"] = round(float(np.mean(eff)), 2)
    avg = spatial_average_db(list(system.values()))
    out["bass_deviation"] = deviation(freqs, avg, tgt, 20.0, 200.0, smoothing=6)
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
