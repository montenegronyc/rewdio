"""Gated, phase-true driver responses for VituixCAD (FRD files).

Every capture shares one time base (the loopback timing reference), so the relative phase between
drivers and between angles is real information. Two rules keep it intact:

1. One window in ABSOLUTE time for every file of a set: `[t_ref - pre, t_ref + gate]`. Re-centring each
   impulse on its own peak would throw the relative timing away.
2. One common delay (`remove_s`, normally `t_ref`) is removed from every file, so the exported phase is
   not a wall of wraps. VituixCAD then needs driver X/Y/Z offsets of zero: the offsets are in the phase.

A gate of T ms is trustworthy down to about 1/T (a 5 ms gate: ~200 Hz). Below that the data is still
written (VituixCAD wants a full band) but flagged in the header; near-field captures fill that range.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy.signal import hilbert

PROTECTION_FLOOR_DB = -30.0  # dividing out a protection HP below this would only amplify noise


@dataclass
class Gate:
    pre_ms: float = 1.0  # before t_ref: room for drivers that arrive earlier than the reference
    gate_ms: float = 5.0  # after t_ref: up to just before the first reflection
    taper_ms: float = 0.5  # half-Hann at each end

    @property
    def valid_from_hz(self) -> float:
        return 1000.0 / self.gate_ms


def arrival_s(ir: np.ndarray, fs: float, t0_s: float = 0.0, onset_fraction: float = 0.2) -> float:
    """Absolute time of the first arrival: where the envelope first reaches `onset_fraction` of its peak."""
    env = np.abs(hilbert(np.asarray(ir, float)))
    return t0_s + int(np.argmax(env >= onset_fraction * env.max())) / fs


def gated_response(ir: np.ndarray, fs: float, freqs: np.ndarray, t_ref_s: float, gate: Gate,
                   t0_s: float = 0.0, remove_s: float | None = None) -> np.ndarray:
    """Complex response of `ir` (sample 0 at absolute time `t0_s`) windowed to
    `[t_ref - pre, t_ref + gate]`, with `remove_s` (default `t_ref_s`) of pure delay removed."""
    ir = np.asarray(ir, float)
    remove_s = t_ref_s if remove_s is None else remove_s
    a = int(round((t_ref_s - gate.pre_ms / 1000 - t0_s) * fs))
    b = int(round((t_ref_s + gate.gate_ms / 1000 - t0_s) * fs))
    if a < 0 or b > ir.size or b - a < 8:
        raise ValueError(f"gate [{a}, {b}) falls outside the impulse response (0..{ir.size}); "
                         "check the timing reference and t_ref")
    x = ir[a:b].copy()
    nt_in = max(1, min(int(gate.pre_ms * fs / 1000), x.size // 4))
    nt_out = max(1, min(int(gate.taper_ms * fs / 1000), x.size // 4))
    ramp = lambda k: 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, k))  # noqa: E731
    x[:nt_in] *= ramp(nt_in)
    x[-nt_out:] *= ramp(nt_out)[::-1]
    # Evaluate the DTFT directly at the requested frequencies, with sample times on the absolute
    # axis minus the common delay. Direct evaluation avoids interpolating a rotating phase.
    t = t0_s + (a + np.arange(x.size)) / fs - remove_s
    f = np.asarray(freqs, float)
    out = np.empty(f.size, complex)
    for i in range(0, f.size, 256):  # chunked: bounded memory for long gates and dense grids
        fc = f[i:i + 256]
        out[i:i + 256] = np.exp(-2j * np.pi * np.outer(fc, t)) @ x
    return out


def remove_protection(h: np.ndarray, protection: np.ndarray) -> tuple[np.ndarray, float | None]:
    """Divide out a protection filter's known response. Phase is corrected everywhere; magnitude only
    down to PROTECTION_FLOOR_DB of the filter, below which the measurement is the filter, not the driver.
    Returns the corrected response and the frequency index range note (lowest fully-corrected bin)."""
    p = np.asarray(protection, complex)
    floor = 10 ** (PROTECTION_FLOOR_DB / 20)
    mag = np.maximum(np.abs(p), floor)
    ok = np.abs(p) >= floor
    return h / (mag * np.exp(1j * np.angle(p))), (int(np.argmax(ok)) if ok.any() else None)


@dataclass
class FrdFile:
    path: Path
    freqs: np.ndarray
    h: np.ndarray
    header: list[str] = field(default_factory=list)

    def write(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = 20 * np.log10(np.maximum(np.abs(self.h), 1e-12))
        ph = np.rad2deg(np.angle(self.h))
        lines = [f"* {line}" for line in self.header]
        lines += [f"{f:.6f}\t{d:.4f}\t{p:.4f}" for f, d, p in zip(self.freqs, db, ph)]
        self.path.write_text("\n".join(lines) + "\n")
        return self.path


def read_frd(path: str | Path) -> tuple[np.ndarray, np.ndarray]:
    """(freqs, complex response) from an FRD file; comment lines start with * or a letter."""
    rows = []
    for line in Path(path).read_text().splitlines():
        s = line.strip()
        if not s or not (s[0].isdigit() or s[0] == "."):
            continue
        rows.append([float(v) for v in s.replace(",", " ").split()[:3]])
    a = np.asarray(rows)
    return a[:, 0], 10 ** (a[:, 1] / 20) * np.exp(1j * np.deg2rad(a[:, 2]))


def vituix_name(speaker: str, driver: str, plane: str, angle: float) -> str:
    """VituixCAD reads plane and angle from the file name: 'L_LW hor 30.frd', 'L_LW ver -10.frd'."""
    if plane not in ("hor", "ver"):
        raise ValueError("plane must be 'hor' or 'ver'")
    a = int(round(angle))
    a = ((a + 180) % 360) - 180 if a != 180 else 180
    return f"{speaker}_{driver} {plane} {a}.frd"


def stable_band(ir: np.ndarray, fs: float, freqs: np.ndarray, t_ref_s: float, gate: Gate, t0_s: float = 0.0,
                extend_ms: tuple[float, ...] = (1.0, 2.0), tol_db: float = 0.5,
                smoothing: float = 12.0) -> tuple[float, float] | None:
    """Gate check instead of the 1/T rule of thumb: the data is good where the impulse has died out inside
    the gate. Re-gate with the end moved later by each of `extend_ms`; where the (1/`smoothing` octave)
    magnitude moves by no more than `tol_db`, nothing that matters was still arriving at the gate end.
    Returns the widest contiguous such band (lo, hi) in Hz, or None if there is none or the impulse is too
    short to extend the gate."""
    from .response import smooth_db
    f = np.asarray(freqs, float)
    mags = []
    for extra in (0.0, *extend_ms):
        g = Gate(gate.pre_ms, gate.gate_ms + extra, gate.taper_ms)
        try:
            h = gated_response(ir, fs, f, t_ref_s, g, t0_s=t0_s)
        except ValueError:
            return None
        mags.append(smooth_db(f, 20 * np.log10(np.maximum(np.abs(h), 1e-12)), smoothing))
    dev = np.max([np.abs(m - mags[0]) for m in mags[1:]], axis=0)
    ok = dev <= tol_db
    best, start = (0, -1, -1), None
    for i, v in enumerate([*ok, False]):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if i - start > best[0]:
                best = (i - start, start, i - 1)
            start = None
    if best[0] < 2:
        return None
    return float(f[best[1]]), float(f[best[2]])
