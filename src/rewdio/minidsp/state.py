"""Shadow state for miniDSP outputs.

minidsp-rs can write per-output settings but cannot read them back, so Rewdio
tracks what it has written. The transfer function of a state lets the analysis
predict what a DSP change does to a measurement taken under another state.
"""

from __future__ import annotations

import numpy as np
from pydantic import BaseModel, Field

from .biquad import Biquad, FilterType, design


class Peq(BaseModel):
    kind: FilterType = "PK"
    freq_hz: float
    gain_db: float
    q: float

    def biquad(self, fs: float) -> Biquad:
        return design(self.kind, self.freq_hz, fs, self.gain_db, self.q)


class OutputState(BaseModel):
    gain_db: float = 0.0
    delay_ms: float = 0.0
    invert: bool = False
    mute: bool = False
    peq: dict[int, Peq] = Field(default_factory=dict, description="Rewdio-managed PEQ slots that are active")

    def transfer(self, freqs: np.ndarray, fs: float) -> np.ndarray:
        """Complex response of the managed part of this output (mute ignored)."""
        f = np.asarray(freqs, dtype=float)
        h = np.full(f.shape, 10 ** (self.gain_db / 20.0), dtype=complex)
        if self.invert:
            h = -h
        h = h * np.exp(-2j * np.pi * f * self.delay_ms / 1000.0)
        for p in self.peq.values():
            h = h * p.biquad(fs).response(f, fs)
        return h


class DspState(BaseModel):
    outputs: dict[str, OutputState] = Field(default_factory=dict, description="keyed by channel role")
    master_volume_db: float | None = None

    def get(self, role: str) -> OutputState:
        return self.outputs.setdefault(role, OutputState())


def change_transfer(old: OutputState, new: OutputState, freqs: np.ndarray, fs: float) -> np.ndarray:
    """Multiply a response measured under `old` by this to predict it under `new`."""
    return new.transfer(freqs, fs) / old.transfer(freqs, fs)
