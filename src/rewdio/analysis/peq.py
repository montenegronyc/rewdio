"""Last-resort parametric EQ for the sub channels.

Optimises the *full* complex system (mains + subs*PEQ) at every mic position, so the
phase shift a filter adds near the crossover is accounted for. Cut-biased: boosts are
capped and down-weighted, and frequencies whose level varies a lot between seats
(position-dependent nulls) are left alone, because EQ cannot fix them.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import least_squares

from ..minidsp.biquad import design
from ..minidsp.state import Peq
from .response import band, smooth_db


@dataclass
class PeqResult:
    filters: list[Peq]
    before_std_db: float
    after_std_db: float


def _peq_db_complex(filters: list[tuple[float, float, float]], f: np.ndarray, fs: float) -> np.ndarray:
    h = np.ones_like(f, dtype=complex)
    for fc, g, q in filters:
        h = h * design("PK", fc, fs, g, q).response(f, fs)
    return h


def design_peq(freqs: np.ndarray, others: list[np.ndarray], subs: list[np.ndarray], target_db: np.ndarray,
               lo: float, hi: float, n_max: int, fs: float, max_boost_db: float = 3.0,
               smoothing: float = 12.0, spread_limit_db: float = 4.0, max_cut_db: float = 12.0) -> PeqResult:
    """others[p], subs[p]: complex responses at position p (on `freqs`). PEQ applies to subs."""
    f = freqs
    m = band(f, lo, hi)
    fm = f[m]
    O = np.array([o[m] for o in others])
    S = np.array([s[m] for s in subs])
    tgt = np.asarray(target_db)[m]
    ppo = 1.0 / np.log2(f[1] / f[0])

    def system_db(h_peq):
        p = np.mean(np.abs(O + S * h_peq[None, :]) ** 2, axis=0)
        width = max(1, int(round(ppo / smoothing)))
        from scipy.ndimage import uniform_filter1d
        return 10 * np.log10(np.maximum(uniform_filter1d(p, width, mode="nearest"), 1e-24))

    # seat-to-seat spread: where it is large, EQ only trades one seat for another
    per_pos = np.array([smooth_db(fm, 10 * np.log10(np.maximum(np.abs(o + s) ** 2, 1e-24)), 6) for o, s in zip(O, S)])
    spread = per_pos.std(axis=0) if len(per_pos) > 1 else np.zeros(fm.size)
    weight = np.where(spread > spread_limit_db, 0.1, 1.0)

    def err_vec(h_peq):
        e = system_db(h_peq) - tgt
        e = e - np.average(e, weights=weight)
        # dips (e<0) weigh less: cut-biased
        return np.where(e < 0, 0.5, 1.0) * weight * e

    def std_of(h_peq):
        e = system_db(h_peq) - tgt
        e = e - np.average(e, weights=weight)
        return float(np.sqrt(np.average(e ** 2, weights=weight)))

    before = std_of(np.ones(fm.size))
    params: list[tuple[float, float, float]] = []
    for _ in range(n_max):
        e = system_db(_peq_db_complex(params, fm, fs)) - tgt
        e = (e - np.average(e, weights=weight)) * weight
        e_bias = np.where(e > 0, e, 0.5 * e)
        for fc, g, _ in params:  # a filter pinned at its limit cannot do more where it sits
            if g >= max_boost_db - 0.05 or g <= -max_cut_db + 0.05:
                e_bias[np.abs(np.log2(fm / fc)) < 1 / 3] = 0.0
        k = int(np.argmax(np.abs(e_bias)))
        if abs(e_bias[k]) < 1.0:
            break
        g0 = float(np.clip(-e[k], -max_cut_db, max_boost_db))
        x0 = [v for p in params for v in (np.log(p[0]), p[1], np.log(p[2]))] + [np.log(fm[k]), g0, np.log(4.0)]
        n = len(x0) // 3
        lo_b = [v for _ in range(n) for v in (np.log(lo), -max_cut_db, np.log(0.5))]
        hi_b = [v for _ in range(n) for v in (np.log(hi), max_boost_db, np.log(12.0))]
        x0 = np.clip(x0, np.array(lo_b) + 1e-6, np.array(hi_b) - 1e-6)

        def resid(x):
            ps = [(float(np.exp(x[3 * i])), float(x[3 * i + 1]), float(np.exp(x[3 * i + 2]))) for i in range(n)]
            return err_vec(_peq_db_complex(ps, fm, fs))

        sol = least_squares(resid, x0, bounds=(lo_b, hi_b), max_nfev=200)
        trial = [(float(np.exp(sol.x[3 * i])), float(sol.x[3 * i + 1]), float(np.exp(sol.x[3 * i + 2]))) for i in range(n)]
        if std_of(_peq_db_complex(trial, fm, fs)) > std_of(_peq_db_complex(params, fm, fs)) - 0.1:
            break  # this filter does not earn its keep
        params = trial
    params = [p for p in params if abs(p[1]) >= 0.5]
    after = std_of(_peq_db_complex(params, fm, fs))
    return PeqResult(
        filters=[Peq(kind="PK", freq_hz=round(fc, 1), gain_db=round(g, 1), q=round(q, 2)) for fc, g, q in sorted(params)],
        before_std_db=round(before, 2), after_std_db=round(after, 2))
