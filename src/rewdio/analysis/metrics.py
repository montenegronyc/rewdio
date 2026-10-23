"""Compact, model-readable summaries of responses."""

from __future__ import annotations

import numpy as np
from scipy.signal import find_peaks

from .response import Response, band, band_level_db, smooth_db


def deviation(freqs: np.ndarray, mag_db: np.ndarray, target_db: np.ndarray, lo: float, hi: float,
              smoothing: float = 6) -> dict:
    """Deviation from target after aligning the mean level in the band."""
    m = band(freqs, lo, hi)
    s = smooth_db(freqs, mag_db, smoothing)
    err = s[m] - target_db[m]
    err = err - np.mean(err)
    return {"band_hz": [lo, hi], "std_db": round(float(np.std(err)), 2),
            "max_db": round(float(np.max(err)), 2), "min_db": round(float(np.min(err)), 2),
            "peak_to_peak_db": round(float(np.ptp(err)), 2)}


def peaks_and_nulls(freqs: np.ndarray, mag_db: np.ndarray, lo: float = 15, hi: float = 300,
                    smoothing: float = 12, prominence: float = 4.0, limit: int = 6) -> dict:
    m = band(freqs, lo, hi)
    f = freqs[m]
    s = smooth_db(freqs, mag_db, smoothing)[m]

    def pick(sig):
        idx, props = find_peaks(sig, prominence=prominence)
        order = np.argsort(props["prominences"])[::-1][:limit]
        return [{"hz": round(float(f[idx[i]]), 1), "db": round(float(props["prominences"][i]), 1)}
                for i in sorted(order, key=lambda k: idx[k])]

    return {"peaks": pick(s), "nulls": pick(-s)}


def summarize(resp: Response, crossover_hz: float, ppo_out: int = 3) -> dict:
    """A small dict: band levels plus a 1/3-octave decimated curve, sized for an LLM context."""
    f = resp.freqs
    mag = resp.mag_db
    sm = smooth_db(f, mag, 3)
    pts = np.unique(np.clip(np.round(np.log2(f / f[0]) * ppo_out), 0, None).astype(int))
    curve_f = f[0] * 2.0 ** (pts / ppo_out)
    curve = np.interp(np.log(curve_f), np.log(f), sm)
    keep = (curve_f >= 15) & (curve_f <= 20000)
    return {
        "level_db": {
            "sub_band": round(band_level_db(resp, None, 25, crossover_hz * 0.8), 1),
            "crossover_band": round(band_level_db(resp, None, crossover_hz / 2, crossover_hz * 2), 1),
            "mid_band_500_2k": round(band_level_db(resp, None, 500, 2000), 1),
        },
        **peaks_and_nulls(f, mag),
        "curve_third_octave": [[round(float(a), 1), round(float(b), 1)] for a, b in zip(curve_f[keep], curve[keep])],
    }


def summation_efficiency_db(a: Response, b: Response, lo: float, hi: float) -> float:
    """How close |a+b| comes to |a|+|b| (0 dB = perfectly coherent), weighted to the overlap."""
    m = band(a.freqs, lo, hi)
    aa, bb = np.abs(a.h[m]), np.abs(b.h[m])
    s = np.abs(a.h[m] + b.h[m])
    w = np.minimum(aa, bb) / np.maximum(np.maximum(aa, bb), 1e-20)
    eff = 20 * np.log10(np.maximum(s, 1e-20) / np.maximum(aa + bb, 1e-20))
    return float(np.sum(w * eff) / max(np.sum(w), 1e-12))
