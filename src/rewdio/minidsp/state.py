"""Shadow state for miniDSP outputs.

minidsp-rs can write per-output settings but cannot read them back, so Rewdio
tracks what it has written. The transfer function of a state lets the analysis
predict what a DSP change does to a measurement taken under another state.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .biquad import Biquad, FilterType, XoverTopology, cascade_response, crossover_sections, design

XOVER_SLOTS = 8  # two crossover groups of four biquads per output (2x4HD, Flex, Flex HTx, DDRC-24...)


class Peq(BaseModel):
    kind: FilterType = "PK"
    freq_hz: float
    gain_db: float
    q: float

    def biquad(self, fs: float) -> Biquad:
        return design(self.kind, self.freq_hz, fs, self.gain_db, self.q)


class XoverFilter(BaseModel):
    """One crossover filter: a preset `topology` (LR4...), or explicit section Qs (`qs`, one biquad each).
    kind AP (all-pass) always takes `qs`; it delays without changing the level, like a passive LC all-pass."""
    model_config = ConfigDict(extra="forbid")  # it is also a config entry: typos must fail
    kind: Literal["HP", "LP", "AP"]
    topology: XoverTopology | None = "LR4"
    qs: list[float] | None = Field(default=None, description="Explicit section Qs; overrides topology")
    freq_hz: float = Field(gt=0)
    protect: bool = Field(default=False, description="Driver protection: never removed, even for raw captures")

    @model_validator(mode="after")
    def _shape(self) -> XoverFilter:
        if self.qs is not None:
            if not self.qs or any(q <= 0 for q in self.qs):
                raise ValueError("qs must be a non-empty list of positive Qs")
            self.topology = None
        elif self.kind == "AP":
            raise ValueError("an all-pass (AP) needs qs, e.g. {'kind': 'AP', 'freq_hz': 1381, 'qs': [0.8055]}")
        elif self.topology is None:
            raise ValueError("give a topology (LR4, ...) or explicit qs")
        return self

    def sections(self, fs: float) -> list[Biquad]:
        if self.qs is not None:
            return [design(self.kind, self.freq_hz, fs, q=q) for q in self.qs]
        return crossover_sections(self.kind, self.topology, self.freq_hz, fs)

    def label(self) -> str:
        shape = self.topology or "Q " + "/".join(f"{q:g}" for q in self.qs)
        return f"{self.kind} {self.freq_hz:g} Hz {shape}"


def xover_sections(filters: list[XoverFilter], fs: float) -> list[Biquad]:
    out = [bq for f in filters for bq in f.sections(fs)]
    if len(out) > XOVER_SLOTS:
        raise ValueError(f"crossover needs {len(out)} biquads; an output's crossover block holds {XOVER_SLOTS}")
    return out


class OutputState(BaseModel):
    gain_db: float = 0.0
    delay_ms: float = 0.0
    invert: bool = False
    mute: bool = False
    peq: dict[int, Peq] = Field(default_factory=dict, description="Rewdio-managed PEQ slots that are active")
    crossover: list[XoverFilter] | None = Field(
        default=None, description="Crossover block as Rewdio wrote it; None = not managed (the user's own)")

    def transfer(self, freqs: np.ndarray, fs: float) -> np.ndarray:
        """Complex response of the managed part of this output (mute ignored)."""
        f = np.asarray(freqs, dtype=float)
        h = np.full(f.shape, 10 ** (self.gain_db / 20.0), dtype=complex)
        if self.invert:
            h = -h
        h = h * np.exp(-2j * np.pi * f * self.delay_ms / 1000.0)
        for p in self.peq.values():
            h = h * p.biquad(fs).response(f, fs)
        if self.crossover:
            h = h * cascade_response(xover_sections(self.crossover, fs), f, fs)
        return h


class DspState(BaseModel):
    outputs: dict[str, OutputState] = Field(default_factory=dict, description="keyed by channel role")
    master_volume_db: float | None = None

    def get(self, role: str) -> OutputState:
        return self.outputs.setdefault(role, OutputState())


def change_transfer(old: OutputState, new: OutputState, freqs: np.ndarray, fs: float) -> np.ndarray:
    """Multiply a response measured under `old` by this to predict it under `new`."""
    return new.transfer(freqs, fs) / old.transfer(freqs, fs)
