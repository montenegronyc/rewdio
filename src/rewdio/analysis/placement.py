"""Reverse calculation of sub positions by acoustic reciprocity.

With the sub at a seat and the mic at candidate sub location k, the measurement is
(by reciprocity) the response the seat would get from a sub at k. Rank single
positions and every pair as a coherent sum with its best delay/gain/polarity.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np

from ..config import PhaseMode
from .alignment import optimize_pair
from .metrics import deviation
from .response import Response, spatial_average_db

SPEED_OF_SOUND = 343.0


@dataclass
class Candidate:
    label: str
    by_seat: dict[str, Response]  # seat label -> response (sub at that seat, mic here)
    xyz: tuple[float, float, float] | None = None


def geometric_correction(cands: list[Candidate], seats_xyz: dict[str, tuple[float, float, float]]) -> list[Candidate]:
    """Without a timing reference REW peak-aligns every IR; restore the distance delay.

    The sub's own group delay is common to every candidate so it cancels in the comparison.
    """
    out = []
    for c in cands:
        if c.xyz is None:
            raise ValueError(f"geometric phase mode needs coordinates for candidate {c.label}")
        fixed = {}
        for seat, r in c.by_seat.items():
            d = float(np.linalg.norm(np.subtract(c.xyz, seats_xyz[seat])))
            fixed[seat] = r.delayed(1000.0 * d / SPEED_OF_SOUND)
        out.append(Candidate(c.label, fixed, c.xyz))
    return out


def rank_positions(cands: list[Candidate], target_db: np.ndarray, phase_mode: PhaseMode,
                   max_delay_ms: float, lo: float = 20.0, hi: float = 120.0, top: int = 5) -> dict:
    if len(cands) < 2:
        raise ValueError("need at least two candidate positions")
    seats = list(cands[0].by_seat.keys())
    freqs = cands[0].by_seat[seats[0]].freqs
    coherent = phase_mode != "magnitude"

    singles = []
    for c in cands:
        avg = spatial_average_db([c.by_seat[s] for s in seats])
        dev = deviation(freqs, avg, target_db, lo, hi)
        singles.append({"position": c.label, "std_db": dev["std_db"], "peak_to_peak_db": dev["peak_to_peak_db"]})
    singles.sort(key=lambda x: x["std_db"])

    pairs = []
    for a, b in itertools.combinations(cands, 2):
        sol = optimize_pair([a.by_seat[s] for s in seats], [b.by_seat[s] for s in seats], target_db, lo, hi,
                            max_delay_ms, coherent=coherent)
        pairs.append({
            "positions": [a.label, b.label], "score": round(sol.score, 2), "std_db": round(sol.std_db, 2),
            "seat_spread_db": round(sol.spread_db, 2),
            "settings": {"delay_ms_second": round(sol.delay_ms, 2), "gain_db_second": round(sol.gain_db, 2),
                         "invert_second": sol.invert},
        })
    pairs.sort(key=lambda x: x["score"])
    confidence = {"absolute": "high (loopback timing)",
                  "geometric": "medium (distance-derived timing; verify after moving subs)",
                  "magnitude": "low (no phase; ranks complementary peaks/nulls only)"}[phase_mode]
    return {"band_hz": [lo, hi], "seats": seats, "phase_mode": phase_mode, "confidence": confidence,
            "best_pairs": pairs[:top], "best_single": singles[:3], "all_singles": singles}
