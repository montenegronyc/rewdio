"""Measurement-quality gate, run before anything is solved from a sweep.

Ideas from koltyj/REW-mcp's measurement reviewer and daredoole/audio-calibration-mcp's
quality gate (both MIT): low SNR, clipping, missing low-frequency coverage, silent channels
and poor repeatability are caught here instead of silently feeding the solver.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..config import ChannelKind, SystemConfig
from .response import Response, band, band_median_db, smooth_db


@dataclass
class Finding:
    level: str  # critical | warning
    code: str
    message: str

    def as_dict(self) -> dict:
        return {"level": self.level, "code": self.code, "message": self.message}


def check_sweep(cfg: SystemConfig, resp: Response, summary: dict, roles: list[str], reference_db: float | None) -> list[Finding]:
    """reference_db: mains-band level of the gain-staging sweep, to spot a dead channel."""
    q = cfg.tuning.quality
    b = cfg.tuning.bands
    out: list[Finding] = []
    snr = summary.get("signalToNoisedB")
    if snr is not None:
        if snr < q.min_snr_db_critical:
            out.append(Finding("critical", "low_snr", f"SNR {snr:.0f} dB: raise the level or quiet the room"))
        elif snr < q.min_snr_db_warning:
            out.append(Finding("warning", "low_snr", f"SNR {snr:.0f} dB is marginal"))
    if summary.get("clipped"):
        out.append(Finding("critical", "clipped", "input clipped: lower the level or the mic preamp gain"))
    start = summary.get("startFreq")
    if start is not None and start > q.max_start_hz:
        out.append(Finding("warning", "lf_coverage", f"data starts at {start:.0f} Hz; sub analysis needs {q.max_start_hz:.0f} Hz"))
    if reference_db is not None and len(roles) == 1:
        kind = cfg.channel(roles[0]).kind
        lo, hi = ((b.sub_level_lo_hz, cfg.crossover_hz * b.sub_level_xo_factor) if kind == ChannelKind.SUB
                  else b.mains_level_hz)
        level = band_median_db(resp, lo, hi, b.level_smoothing)
        if level < reference_db - q.silent_channel_margin_db:
            out.append(Finding("critical", "silent_channel",
                               f"{roles[0]} is {reference_db - level:.0f} dB below the staging level in its own band: "
                               f"check its amp, cable, routing and miniDSP output {cfg.channel(roles[0]).dsp_output}"))
    return out


def repeatability_db(a: Response, b: Response, cfg: SystemConfig) -> float:
    q = cfg.tuning.quality
    sa, sb = smooth_db(a.freqs, a.mag_db, 12), smooth_db(b.freqs, b.mag_db, 12)
    m = band(a.freqs, *q.repeat_band_hz)
    m &= sa > sa[m].max() - q.silent_channel_margin_db  # only where this channel actually plays
    d = sa[m] - sb[m]
    return float(np.std(d - np.mean(d)))


def blocking(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.level == "critical"]
