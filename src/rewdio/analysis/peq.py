"""Last-resort parametric EQ for the sub channels.

Optimises the *full* complex system (mains + subs*PEQ) at every mic position, so the
phase shift a filter adds near the crossover is accounted for. Cut-biased: boosts are
capped and down-weighted, and frequencies whose level varies a lot between seats
(position-dependent nulls) are left alone, because EQ cannot fix them.

With three or more positions the result is cross-validated by leaving each seat out
(idea from daredoole/audio-calibration-mcp, MIT): filters that only help the seats they
were fitted on are dropped.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.ndimage import uniform_filter1d
from scipy.optimize import least_squares

from ..config import PeqTuning
from ..minidsp.biquad import design
from ..minidsp.state import Peq
from .response import band, smooth_db


@dataclass
class PeqResult:
    filters: list[Peq]
    before_std_db: float
    after_std_db: float
    held_out_gain_db: float | None = None  # mean std improvement on left-out seats


def _response(filters: list[tuple[float, float, float]], f: np.ndarray, fs: float) -> np.ndarray:
    h = np.ones_like(f, dtype=complex)
    for fc, g, q in filters:
        h = h * design("PK", fc, fs, g, q).response(f, fs)
    return h


class _Problem:
    def __init__(self, fm, O, S, tgt, ppo, fs, pq: PeqTuning, max_boost: float):
        self.fm, self.O, self.S, self.tgt, self.fs, self.pq, self.max_boost = fm, O, S, tgt, fs, pq, max_boost
        self.width = max(1, int(round(ppo / pq.smoothing)))
        per_pos = np.array([smooth_db(fm, 10 * np.log10(np.maximum(np.abs(o + s) ** 2, 1e-24)), 6) for o, s in zip(O, S)])
        spread = per_pos.std(axis=0) if len(per_pos) > 1 else np.zeros(fm.size)
        self.weight = np.where(spread > pq.spread_limit_db, 0.1, 1.0)

    def err(self, params, rows=None):
        rows = slice(None) if rows is None else rows
        h = _response(params, self.fm, self.fs)
        p = np.mean(np.abs(self.O[rows] + self.S[rows] * h[None, :]) ** 2, axis=0)
        e = 10 * np.log10(np.maximum(uniform_filter1d(p, self.width, mode="nearest"), 1e-24)) - self.tgt
        return e - np.average(e, weights=self.weight)

    def std(self, params, rows=None) -> float:
        e = self.err(params, rows)
        return float(np.sqrt(np.average(e ** 2, weights=self.weight)))

    def fit(self, rows, n_max: int, lo: float, hi: float) -> list[tuple[float, float, float]]:
        pq = self.pq
        params: list[tuple[float, float, float]] = []
        for _ in range(n_max):
            e = self.err(params, rows) * self.weight
            e_bias = np.where(e > 0, e, pq.dip_weight * e)
            for fc, g, _ in params:  # a filter pinned at its limit cannot do more where it sits
                if g >= self.max_boost - 0.05 or g <= -pq.max_cut_db + 0.05:
                    e_bias[np.abs(np.log2(self.fm / fc)) < 1 / 3] = 0.0
            k = int(np.argmax(np.abs(e_bias)))
            if abs(e_bias[k]) < pq.stop_error_db:
                break
            g0 = float(np.clip(-e[k], -pq.max_cut_db, self.max_boost))
            x0 = [v for p in params for v in (np.log(p[0]), p[1], np.log(p[2]))] + [np.log(self.fm[k]), g0, np.log(pq.start_q)]
            n = len(x0) // 3
            lo_b = [v for _ in range(n) for v in (np.log(lo), -pq.max_cut_db, np.log(pq.min_q))]
            hi_b = [v for _ in range(n) for v in (np.log(hi), self.max_boost, np.log(pq.max_q))]
            x0 = np.clip(x0, np.array(lo_b) + 1e-6, np.array(hi_b) - 1e-6)

            def unpack(x):
                return [(float(np.exp(x[3 * i])), float(x[3 * i + 1]), float(np.exp(x[3 * i + 2]))) for i in range(n)]

            def resid(x):
                ee = self.err(unpack(x), rows)
                return np.where(ee < 0, pq.dip_weight, 1.0) * self.weight * ee

            trial = unpack(least_squares(resid, x0, bounds=(lo_b, hi_b), max_nfev=pq.max_nfev).x)
            if self.std(trial, rows) > self.std(params, rows) - pq.min_improvement_db:
                break  # this filter does not earn its keep
            params = trial
        return [p for p in params if abs(p[1]) >= pq.min_gain_db]


def design_peq(freqs: np.ndarray, others: list[np.ndarray], subs: list[np.ndarray], target_db: np.ndarray,
               lo: float, hi: float, n_max: int, fs: float, max_boost_db: float, pq: PeqTuning) -> PeqResult:
    """others[p], subs[p]: complex responses at position p (on `freqs`). PEQ applies to subs."""
    m = band(freqs, lo, hi)
    fm = freqs[m]
    O = np.array([o[m] for o in others])
    S = np.array([s[m] for s in subs])
    prob = _Problem(fm, O, S, np.asarray(target_db)[m], 1.0 / np.log2(freqs[1] / freqs[0]), fs, pq, max_boost_db)
    before = prob.std([])
    params = prob.fit(None, n_max, lo, hi)
    held = None
    n_pos = len(others)
    if params and n_pos >= 3:
        gains = []
        for k in range(n_pos):
            train = [i for i in range(n_pos) if i != k]
            p_k = prob.fit(train, n_max, lo, hi)
            gains.append(prob.std([], [k]) - prob.std(p_k, [k]))
        held = float(np.mean(gains))
        if held < pq.min_improvement_db:
            params = []  # it only fits the seats it was trained on
    return PeqResult(
        filters=[Peq(kind="PK", freq_hz=round(fc, 1), gain_db=round(g, 1), q=round(q, 2)) for fc, g, q in sorted(params)],
        before_std_db=round(before, 2), after_std_db=round(prob.std(params), 2),
        held_out_gain_db=None if held is None else round(held, 2))
