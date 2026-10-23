"""Waterfall (cumulative spectral decay) from an impulse response.

Each slice is the spectrum of a fixed-length window that starts a little later than the one before, so
ringing room modes show up as ridges that take a long time to fall away. Rewdio keeps a short, resampled
copy of each full-system impulse (bass only needs a low sample rate) and computes the slices on demand.
"""

from __future__ import annotations

from fractions import Fraction

import numpy as np
from scipy.signal import resample_poly

from ..config import WaterfallSettings
from .response import smooth_db


def keep_ir(ir: np.ndarray, fs: float, w: WaterfallSettings) -> dict:
    """The part of an impulse the waterfall needs, resampled to w.ir_fs: a little before the peak to ir_ms after."""
    ir = np.asarray(ir, float)
    peak = int(np.argmax(np.abs(ir)))
    pre = int(0.02 * fs)
    seg = ir[max(0, peak - pre): peak + int(w.ir_ms / 1000 * fs)]
    ratio = Fraction(w.ir_fs / fs).limit_denominator(1000)
    out = resample_poly(seg, ratio.numerator, ratio.denominator)
    scale = float(np.max(np.abs(out))) or 1.0
    return {"fs": w.ir_fs, "data": [round(float(v), 5) for v in out / scale]}


def _taper(n: int, fs: float) -> np.ndarray:
    """Short raised-cosine rise (no click at the cut) and a half-Hann fall over the last 30%."""
    win = np.ones(n)
    rise = max(1, int(0.001 * fs))
    win[:rise] = 0.5 - 0.5 * np.cos(np.pi * np.arange(rise) / rise)
    fall = max(1, int(0.3 * n))
    win[n - fall:] = 0.5 + 0.5 * np.cos(np.pi * np.arange(fall) / fall)
    return win


def csd(stored: dict, w: WaterfallSettings) -> dict:
    """Slices of decay: {freqs, times_ms, db[slice][freq]} with 0 dB at the loudest point and w.floor_db below."""
    fs = float(stored["fs"])
    x = np.asarray(stored["data"], float)
    i0 = int(np.argmax(np.abs(x)))
    n = int(w.window_ms / 1000 * fs)
    step = max(1, int(w.step_ms / 1000 * fs))
    nfft = 1 << int(np.ceil(np.log2(max(n, 1) * 4)))
    bins = np.fft.rfftfreq(nfft, 1 / fs)
    lo, hi = w.hz
    freqs = lo * 2.0 ** (np.arange(int(np.log2(hi / lo) * w.ppo) + 1) / w.ppo)
    win = _taper(n, fs)
    rows, times = [], []
    for k in range(w.slices):
        start = i0 + k * step
        seg = x[start:start + n]
        if seg.size < n // 4:
            break
        seg = np.pad(seg, (0, n - seg.size)) * win
        mag = np.abs(np.fft.rfft(seg, nfft))
        db = 20 * np.log10(np.interp(freqs, bins, mag) + 1e-12)
        rows.append(smooth_db(freqs, db, w.smoothing))
        times.append(round(k * step / fs * 1000, 1))
    db = np.array(rows)
    db = np.maximum(db - db.max(), w.floor_db)
    return {"freqs": [round(float(f), 2) for f in freqs], "times_ms": times,
            "db": [[round(float(v), 1) for v in r] for r in db], "floor_db": w.floor_db}
