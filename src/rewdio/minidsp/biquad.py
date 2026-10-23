"""Biquad design (RBJ cookbook) and evaluation.

Coefficients are stored in the textbook convention
    H(z) = (b0 + b1 z^-1 + b2 z^-2) / (1 + a1 z^-1 + a2 z^-2)
miniDSP (and REW's miniDSP export) negate a1 and a2; use `to_minidsp` when sending.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np

FilterType = Literal["PK", "LS", "HS", "LP", "HP"]


@dataclass(frozen=True)
class Biquad:
    b0: float
    b1: float
    b2: float
    a1: float
    a2: float

    @classmethod
    def identity(cls) -> Biquad:
        return cls(1.0, 0.0, 0.0, 0.0, 0.0)

    def response(self, freqs: np.ndarray, fs: float) -> np.ndarray:
        z1 = np.exp(-2j * np.pi * np.asarray(freqs) / fs)
        z2 = z1 * z1
        return (self.b0 + self.b1 * z1 + self.b2 * z2) / (1.0 + self.a1 * z1 + self.a2 * z2)

    def to_minidsp(self) -> dict:
        return {"b0": self.b0, "b1": self.b1, "b2": self.b2, "a1": -self.a1, "a2": -self.a2}

    @classmethod
    def from_minidsp(cls, d: dict) -> Biquad:
        return cls(d["b0"], d["b1"], d["b2"], -d["a1"], -d["a2"])


def design(kind: FilterType, f0: float, fs: float, gain_db: float = 0.0, q: float = 0.7071) -> Biquad:
    if not 0 < f0 < fs / 2:
        raise ValueError(f"f0 {f0} Hz outside (0, {fs / 2})")
    if q <= 0:
        raise ValueError("Q must be positive")
    A = 10.0 ** (gain_db / 40.0)
    w0 = 2 * math.pi * f0 / fs
    cw, sw = math.cos(w0), math.sin(w0)
    alpha = sw / (2 * q)
    if kind == "PK":
        b0, b1, b2 = 1 + alpha * A, -2 * cw, 1 - alpha * A
        a0, a1, a2 = 1 + alpha / A, -2 * cw, 1 - alpha / A
    elif kind == "LS":
        sa = 2 * math.sqrt(A) * alpha
        b0 = A * ((A + 1) - (A - 1) * cw + sa)
        b1 = 2 * A * ((A - 1) - (A + 1) * cw)
        b2 = A * ((A + 1) - (A - 1) * cw - sa)
        a0 = (A + 1) + (A - 1) * cw + sa
        a1 = -2 * ((A - 1) + (A + 1) * cw)
        a2 = (A + 1) + (A - 1) * cw - sa
    elif kind == "HS":
        sa = 2 * math.sqrt(A) * alpha
        b0 = A * ((A + 1) + (A - 1) * cw + sa)
        b1 = -2 * A * ((A - 1) + (A + 1) * cw)
        b2 = A * ((A + 1) + (A - 1) * cw - sa)
        a0 = (A + 1) - (A - 1) * cw + sa
        a1 = 2 * ((A - 1) - (A + 1) * cw)
        a2 = (A + 1) - (A - 1) * cw - sa
    elif kind == "LP":
        b0 = b2 = (1 - cw) / 2
        b1 = 1 - cw
        a0, a1, a2 = 1 + alpha, -2 * cw, 1 - alpha
    elif kind == "HP":
        b0 = b2 = (1 + cw) / 2
        b1 = -(1 + cw)
        a0, a1, a2 = 1 + alpha, -2 * cw, 1 - alpha
    else:
        raise ValueError(f"unknown filter type {kind}")
    return Biquad(b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0)


def linkwitz_riley4(kind: Literal["LP", "HP"], fc: float, fs: float) -> list[Biquad]:
    """LR4 = two cascaded Butterworth (Q=0.7071) sections."""
    return [design(kind, fc, fs, q=1 / math.sqrt(2))] * 2


def cascade_response(biquads: list[Biquad], freqs: np.ndarray, fs: float) -> np.ndarray:
    h = np.ones(np.asarray(freqs).shape, dtype=complex)
    for bq in biquads:
        h = h * bq.response(freqs, fs)
    return h
