"""Gated (direct-sound) frequency response from an impulse response.

Keeps a window from just before the first arrival up to just before the first strong reflection, so
reflections drop out. Localisation of the centre image is dominated by the first-arriving sound
(precedence effect), so L/R balance is judged here.

Two steps so that channels compared with each other share ONE gate length (different lengths would
bias their levels): `direct_segment` trims each impulse to its direct-sound region and notes how long
a gate that measurement allows; `gated_mag` then applies a common gate. A gate of T ms resolves down
to about `resolution_cycles / T`, so the caller limits its band to that.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.signal import find_peaks, hilbert

from ..config import BalanceSettings


@dataclass
class DirectSegment:
    data: np.ndarray  # starts gate_pre_ms before the onset
    fs: float
    direct_index: int  # envelope peak of the direct sound within `data`
    max_gate_ms: float  # longest gate before the first strong reflection

    def to_dict(self) -> dict:
        return {"data": [float(x) for x in self.data], "fs": self.fs, "direct_index": self.direct_index,
                "max_gate_ms": self.max_gate_ms}

    @classmethod
    def from_dict(cls, d: dict) -> DirectSegment:
        return cls(np.asarray(d["data"]), float(d["fs"]), int(d["direct_index"]), float(d["max_gate_ms"]))


def direct_segment(ir: np.ndarray, fs: float, bs: BalanceSettings, onset_fraction: float) -> DirectSegment:
    ir = np.asarray(ir, float)
    env = np.abs(hilbert(ir))
    peak = env.max()
    onset = int(np.argmax(env >= onset_fraction * peak))
    direct = onset + int(np.argmax(env[onset:onset + int(0.5e-3 * fs) + 1]))
    guard = int(0.5e-3 * fs)
    search = env[direct + guard:direct + int(bs.max_gate_ms * 1e-3 * fs)]
    pk, _ = find_peaks(search, height=peak * 10 ** (bs.reflection_threshold_db / 20))
    gate_ms = bs.max_gate_ms if not pk.size else (guard + pk[0]) / fs * 1000
    a = max(onset - int(bs.gate_pre_ms * fs / 1000), 0)
    b = min(direct + int(bs.max_gate_ms * fs / 1000) + 1, ir.size)
    return DirectSegment(ir[a:b].copy(), fs, direct - a, float(np.clip(gate_ms, bs.min_gate_ms, bs.max_gate_ms)))


def gated_mag(seg: DirectSegment, freqs: np.ndarray, gate_ms: float, bs: BalanceSettings) -> np.ndarray:
    """Magnitude (dB) of the segment gated to `gate_ms` after the direct sound."""
    x = seg.data[:seg.direct_index + int(gate_ms * seg.fs / 1000)].copy()
    n_pre = seg.direct_index
    n_post = x.size - seg.direct_index
    taper = min(bs.gate_taper_ms, gate_ms / 4)
    nt_end = max(1, min(int(taper * seg.fs / 1000), n_post // 2))
    nt_start = max(1, min(int(bs.gate_pre_ms * seg.fs / 1000), n_pre))
    ramp = lambda k: 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, k))  # noqa: E731
    x[:nt_start] *= ramp(nt_start)
    x[-nt_end:] *= ramp(nt_end)[::-1]
    n = 1 << int(np.ceil(np.log2(max(x.size * 8, 4096))))
    fl = np.fft.rfftfreq(n, 1 / seg.fs)
    mag = 20 * np.log10(np.maximum(np.abs(np.fft.rfft(x, n)), 1e-20))
    return np.interp(np.clip(np.asarray(freqs, float), fl[1], fl[-1]), fl, mag)
