"""Left/right balance: the centre image.

Broadband level is matched by trimming the louder main. What remains is the frequency-dependent
difference (R - L, heavily smoothed) that makes a centred voice drift as its spectrum changes. It is
fitted with a few broad peaking filters, split half and half: +g/2 on one side, -g/2 on the other.
RBJ peaking filters with equal Q are exact inverses for +/-g, so the pair moves R - L by about g while
leaving the average of the two channels (the tonal balance) unchanged. Only broad, low-Q, small
corrections above the sub crossover: the aim is the image, not chasing seat-specific reflections.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import least_squares

from ..config import BalanceSettings
from ..minidsp.biquad import design
from ..minidsp.state import Peq
from .response import Response, band, smooth_db


@dataclass
class BalanceResult:
    broadband_db: float  # R minus L, median over the band
    filters: list[tuple[float, float, float]]  # (fc, total R-L change in dB, Q); each side gets half
    before: dict
    after: dict

    def peq(self, side: str) -> list[Peq]:
        sign = 0.5 if side == "R" else -0.5
        return [Peq(kind="PK", freq_hz=round(fc, 1), gain_db=round(sign * g, 2), q=round(q, 2)) for fc, g, q in self.filters]


def difference_db(left: list[Response], right: list[Response], smoothing: float) -> np.ndarray:
    """Seat-averaged, smoothed R - L in dB."""
    f = left[0].freqs
    return np.mean([smooth_db(f, r.mag_db, smoothing) - smooth_db(f, l.mag_db, smoothing)
                    for l, r in zip(left, right)], axis=0)


def stats(d: np.ndarray) -> dict:
    return {"median_db": round(float(np.median(d)), 2), "std_db": round(float(np.std(d - np.median(d))), 2),
            "max_abs_db": round(float(np.max(np.abs(d - np.median(d)))), 2)}


def _pair_db(params, f, fs) -> np.ndarray:
    """R - L change in dB from filters split +g/2 on R and -g/2 on L."""
    out = np.zeros(f.size)
    for fc, g, q in params:
        h = np.abs(design("PK", fc, fs, g / 2, q).response(f, fs))
        out += 20 * np.log10(h) * 2  # +g/2 on R, exact inverse on L
    return out


def solve_balance(left: list[Response], right: list[Response], fs: float, bs: BalanceSettings,
                  band_hz: tuple[float, float]) -> BalanceResult:
    f = left[0].freqs
    m = band(f, *band_hz)
    fm = f[m]
    d = difference_db(left, right, bs.smoothing)[m]
    broadband = float(np.median(d))
    resid = d - broadband
    cap = 2 * bs.max_gain_db_per_side
    lo, hi = band_hz
    params: list[tuple[float, float, float]] = []

    def err(ps):
        e = resid + _pair_db(ps, fm, fs)
        return e - np.median(e)

    def std(ps):
        return float(np.std(err(ps)))

    for _ in range(bs.max_filters):
        e = err(params)
        k = int(np.argmax(np.abs(e)))
        if abs(e[k]) < bs.stop_error_db:
            break
        x0 = [v for p in params for v in (np.log(p[0]), p[1], np.log(p[2]))]
        x0 += [np.log(fm[k]), float(np.clip(-e[k], -cap, cap)), np.log(bs.start_q)]
        n = len(x0) // 3
        lb = [v for _ in range(n) for v in (np.log(lo), -cap, np.log(bs.min_q))]
        ub = [v for _ in range(n) for v in (np.log(hi), cap, np.log(bs.max_q))]
        x0 = np.clip(x0, np.array(lb) + 1e-6, np.array(ub) - 1e-6)

        def unpack(x):
            return [(float(np.exp(x[3 * i])), float(x[3 * i + 1]), float(np.exp(x[3 * i + 2]))) for i in range(n)]

        trial = unpack(least_squares(lambda x: err(unpack(x)), x0, bounds=(lb, ub), max_nfev=bs.max_nfev).x)
        if std(trial) > std(params) - bs.min_improvement_db:
            break
        params = trial
    after = resid + _pair_db(params, fm, fs)
    return BalanceResult(broadband_db=round(broadband, 2), filters=params, before=stats(d), after=stats(after))
