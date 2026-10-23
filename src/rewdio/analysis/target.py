"""Target response relative to the mains reference level.

Either a smooth low shelf (bass_boost_db below shelf_hz) or a REW-style house curve file:
lines of "freq dB", '*' or '#' comments, interpolated on log frequency and held flat beyond its ends.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np

from ..config import TargetCurve


@lru_cache(maxsize=8)
def _read_house_curve(path: str) -> tuple[np.ndarray, np.ndarray]:
    fs, db = [], []
    for line in Path(path).expanduser().read_text().splitlines():
        line = line.strip()
        if not line or line[0] in "*#;":
            continue
        parts = line.replace(",", " ").split()
        try:
            fs.append(float(parts[0]))
            db.append(float(parts[1]))
        except (ValueError, IndexError):
            continue
    if len(fs) < 2:
        raise ValueError(f"house curve {path} has fewer than two points")
    order = np.argsort(fs)
    return np.asarray(fs)[order], np.asarray(db)[order]


def target_db(freqs: np.ndarray, t: TargetCurve) -> np.ndarray:
    f = np.asarray(freqs, float)
    if t.house_curve_path:
        hf, hdb = _read_house_curve(t.house_curve_path)
        return np.interp(np.log(f), np.log(hf), hdb - np.interp(np.log(1000.0), np.log(hf), hdb))
    x = np.log2(f / t.shelf_hz) / (t.shelf_octaves / 4.0)
    return t.bass_boost_db / (1.0 + np.exp(x))
