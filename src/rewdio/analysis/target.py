"""House-curve target: a smooth low shelf relative to the mains reference level."""

from __future__ import annotations

import numpy as np

from ..config import TargetCurve


def target_db(freqs: np.ndarray, t: TargetCurve) -> np.ndarray:
    x = np.log2(np.asarray(freqs) / t.shelf_hz) / (t.shelf_octaves / 4.0)
    return t.bass_boost_db / (1.0 + np.exp(x))
