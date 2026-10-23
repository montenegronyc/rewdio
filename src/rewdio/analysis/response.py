"""Complex frequency responses on a shared log-spaced grid."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.ndimage import uniform_filter1d

F_LO, F_HI = 10.0, 20000.0


def log_grid(ppo: int = 96, f_lo: float = F_LO, f_hi: float = F_HI) -> np.ndarray:
    n = int(np.floor(np.log2(f_hi / f_lo) * ppo)) + 1
    return f_lo * 2.0 ** (np.arange(n) / ppo)


@dataclass
class Response:
    freqs: np.ndarray
    h: np.ndarray  # complex pressure, |h| in Pa-relative units so 20log10|h| = dB SPL
    has_phase: bool = True

    @property
    def mag_db(self) -> np.ndarray:
        return 20 * np.log10(np.maximum(np.abs(self.h), 1e-12))

    @property
    def ppo(self) -> float:
        return 1.0 / np.log2(self.freqs[1] / self.freqs[0])

    @classmethod
    def from_db_phase(cls, freqs, mag_db, phase_deg=None, grid: np.ndarray | None = None) -> Response:
        freqs = np.asarray(freqs, float)
        mag_db = np.asarray(mag_db, float)
        ok = np.isfinite(mag_db) & (freqs > 0)
        freqs, mag_db = freqs[ok], mag_db[ok]
        has_phase = phase_deg is not None
        ph = np.unwrap(np.deg2rad(np.asarray(phase_deg, float)[ok])) if has_phase else np.zeros_like(mag_db)
        if grid is not None:
            lf = np.log(freqs)
            lg = np.log(grid)
            mag_db = np.interp(lg, lf, mag_db, left=mag_db[0] - 60, right=mag_db[-1] - 60)
            ph = np.interp(lg, lf, ph)
            freqs = grid
        return cls(freqs=freqs, h=10 ** (mag_db / 20) * np.exp(1j * ph), has_phase=has_phase)

    def scaled(self, factor: complex | np.ndarray) -> Response:
        return Response(self.freqs, self.h * factor, self.has_phase)

    def delayed(self, ms: float) -> Response:
        return self.scaled(np.exp(-2j * np.pi * self.freqs * ms / 1000.0))

    def __add__(self, other: Response) -> Response:
        _check_grid(self, other)
        return Response(self.freqs, self.h + other.h, self.has_phase and other.has_phase)

    def to_dict(self) -> dict:
        return {"freqs": self.freqs.tolist(), "re": self.h.real.tolist(), "im": self.h.imag.tolist(),
                "has_phase": self.has_phase}

    @classmethod
    def from_dict(cls, d: dict) -> Response:
        return cls(np.asarray(d["freqs"]), np.asarray(d["re"]) + 1j * np.asarray(d["im"]), d.get("has_phase", True))


def _check_grid(a: Response, b: Response) -> None:
    if a.freqs.shape != b.freqs.shape or not np.allclose(a.freqs, b.freqs):
        raise ValueError("responses are on different frequency grids")


def power_sum(responses: list[Response]) -> Response:
    """Incoherent sum (phase-free), for magnitude-only estimates."""
    p = sum(np.abs(r.h) ** 2 for r in responses)
    return Response(responses[0].freqs, np.sqrt(p).astype(complex), has_phase=False)


def spatial_average_db(responses: list[Response]) -> np.ndarray:
    """Power average across mic positions, in dB."""
    p = np.mean([np.abs(r.h) ** 2 for r in responses], axis=0)
    return 10 * np.log10(np.maximum(p, 1e-24))


def smooth_db(freqs: np.ndarray, mag_db: np.ndarray, fraction: float) -> np.ndarray:
    """Fractional-octave power smoothing (fraction=6 means 1/6 octave). Needs a log grid."""
    if fraction <= 0:
        return np.asarray(mag_db, float)
    ppo = 1.0 / np.log2(freqs[1] / freqs[0])
    width = max(1, int(round(ppo / fraction)))
    p = 10 ** (np.asarray(mag_db) / 10)
    return 10 * np.log10(np.maximum(uniform_filter1d(p, size=width, mode="nearest"), 1e-24))


def band(freqs: np.ndarray, lo: float, hi: float) -> np.ndarray:
    return (freqs >= lo) & (freqs <= hi)


def band_level_db(resp: Response | np.ndarray, freqs: np.ndarray | None, lo: float, hi: float) -> float:
    """Power-average level in a band (dB)."""
    if isinstance(resp, Response):
        freqs, mag = resp.freqs, np.abs(resp.h) ** 2
    else:
        mag = 10 ** (np.asarray(resp) / 10)
    m = band(freqs, lo, hi)
    if not m.any():
        raise ValueError(f"no data between {lo} and {hi} Hz")
    return float(10 * np.log10(np.mean(mag[m])))


def band_median_db(resp: Response, lo: float, hi: float, smoothing: float = 3) -> float:
    """Robust band level: median of the fractional-octave-smoothed curve (peaks and nulls do not drag it)."""
    m = band(resp.freqs, lo, hi)
    if not m.any():
        raise ValueError(f"no data between {lo} and {hi} Hz")
    return float(np.median(smooth_db(resp.freqs, resp.mag_db, smoothing)[m]))
