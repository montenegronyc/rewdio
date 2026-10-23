"""REW wire formats.

REW ships sample arrays as base64 over big-endian float32 bytes, and writes
Java's bare NaN / Infinity literals into otherwise-JSON bodies.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

import numpy as np

_BARE_NONFINITE = re.compile(r'"(?:[^"\\]|\\.)*"|(-?Infinity|NaN)')


def decode_floats(b64: str) -> np.ndarray:
    raw = base64.b64decode(b64)
    if len(raw) % 4:
        raise ValueError(f"REW float array: {len(raw)} bytes is not a multiple of 4")
    return np.frombuffer(raw, dtype=">f4").astype(np.float64)


def encode_floats(values: Any) -> str:
    return base64.b64encode(np.asarray(values, dtype=">f4").tobytes()).decode("ascii")


def parse_rew_json(text: str) -> Any:
    """Parse REW JSON, mapping bare NaN/Infinity tokens (outside strings) to null."""
    return json.loads(_BARE_NONFINITE.sub(lambda m: "null" if m.group(1) else m.group(0), text))
