"""Compact, model-readable summaries of responses.

Peak/null detection follows brandon-fryslie/room-eq-wizard-mcp (MIT): deviation from a running
median baseline, so a mode's shoulders don't read as extra peaks, with a -3 dB-width Q estimate.
"""

from __future__ import annotations

import numpy as np
from scipy.ndimage import median_filter

from ..config import Bands, SystemConfig
from .response import Response, band, band_level_db, smooth_db


def deviation(freqs: np.ndarray, mag_db: np.ndarray, target_db: np.ndarray, lo: float, hi: float,
              smoothing: float) -> dict:
    """Deviation from target after aligning the mean level in the band."""
    m = band(freqs, lo, hi)
    s = smooth_db(freqs, mag_db, smoothing)
    err = s[m] - target_db[m]
    err = err - np.mean(err)
    return {"band_hz": [lo, hi], "std_db": round(float(np.std(err)), 2),
            "max_db": round(float(np.max(err)), 2), "min_db": round(float(np.min(err)), 2),
            "peak_to_peak_db": round(float(np.ptp(err)), 2)}


def extrema(freqs: np.ndarray, mag_db: np.ndarray, lo: float, hi: float, *, smoothing: float = 12,
            baseline_octaves: float = 1.0, min_dev_db: float = 3.0, merge_octaves: float = 1 / 3,
            limit: int = 8) -> dict:
    """Peaks and nulls against a running-median baseline, each with depth, Q and severity."""
    s = smooth_db(freqs, mag_db, smoothing)
    ppo = 1.0 / np.log2(freqs[1] / freqs[0])
    base = median_filter(s, size=max(3, int(round(ppo * baseline_octaves)) | 1), mode="nearest")
    dev = s - base
    m = np.where(band(freqs, lo, hi))[0]
    out: dict[str, list] = {"peaks": [], "nulls": []}
    for sign, key in ((1, "peaks"), (-1, "nulls")):
        d = sign * dev
        cand = [i for i in m[1:-1] if d[i] >= min_dev_db and d[i] >= d[i - 1] and d[i] >= d[i + 1]]
        cand.sort(key=lambda i: -d[i])
        kept: list[int] = []
        for i in cand:
            if all(abs(np.log2(freqs[i] / freqs[j])) > merge_octaves for j in kept):
                kept.append(i)
        for i in sorted(kept[:limit]):
            half = d[i] - 3.0
            lo_i = i
            while lo_i > 0 and d[lo_i] > half:
                lo_i -= 1
            hi_i = i
            while hi_i < d.size - 1 and d[hi_i] > half:
                hi_i += 1
            bw = max(freqs[hi_i] - freqs[lo_i], 1e-6)
            depth = float(d[i])
            out[key].append({"hz": round(float(freqs[i]), 1), "db": round(depth, 1),
                             "q": round(float(freqs[i] / bw), 2),
                             "severity": "severe" if depth >= 10 else "moderate" if depth >= 6 else "minor"})
    return out


def summarize(resp: Response, cfg: SystemConfig, ppo_out: int = 3) -> dict:
    """A small dict: band levels, peaks/nulls and a 1/3-octave curve, sized for an LLM context."""
    b: Bands = cfg.tuning.bands
    fc = cfg.crossover_hz
    f, mag = resp.freqs, resp.mag_db
    sm = smooth_db(f, mag, 3)
    pts = np.unique(np.clip(np.round(np.log2(f / f[0]) * ppo_out), 0, None).astype(int))
    curve_f = f[0] * 2.0 ** (pts / ppo_out)
    curve = np.interp(np.log(curve_f), np.log(f), sm)
    keep = (curve_f >= cfg.sweep.start_hz) & (curve_f <= cfg.sweep.end_hz)
    lo_m, hi_m = b.mains_level_hz
    return {
        "level_db": {
            "sub_band": round(band_level_db(resp, None, b.sub_level_lo_hz, fc * b.sub_level_xo_factor), 1),
            "crossover_band": round(band_level_db(resp, None, fc / b.crossover_span_factor,
                                                  fc * b.crossover_span_factor), 1),
            "mains_band": round(band_level_db(resp, None, lo_m, hi_m), 1),
        },
        **extrema(f, mag, *b.summary_peaks_hz),
        "curve_third_octave": [[round(float(a), 1), round(float(v), 1)] for a, v in zip(curve_f[keep], curve[keep])],
    }


def summation_efficiency_db(a: Response, b: Response, lo: float, hi: float) -> float:
    """How close |a+b| comes to |a|+|b| (0 dB = perfectly coherent), weighted to the overlap."""
    m = band(a.freqs, lo, hi)
    aa, bb = np.abs(a.h[m]), np.abs(b.h[m])
    s = np.abs(a.h[m] + b.h[m])
    w = np.minimum(aa, bb) / np.maximum(np.maximum(aa, bb), 1e-20)
    eff = 20 * np.log10(np.maximum(s, 1e-20) / np.maximum(aa + bb, 1e-20))
    return float(np.sum(w * eff) / max(np.sum(w), 1e-12))
