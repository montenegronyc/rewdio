"""Spinorama curves (CTA-2034 style) and single-speaker targets, from a polar set.

The curves are power averages of groups of directions:
- listening window (LW): hor 0, +-10, +-20, +-30 and ver +-10
- early reflections (ER): the mean of five bounces (floor, ceiling, front, side, rear wall)
- sound power (SP): every direction, weighted by the area of the sphere it stands for
- predicted in-room response (PIR): 0.12 LW + 0.44 ER + 0.44 SP (in power)
- directivity indices: ERDI = LW - ER, SPDI = LW - SP (dB)

Missing directions are skipped and named. A horizontal-only set still gives LW (less ver +-10), ER (less
floor and ceiling) and an SP estimate that assumes the vertical plane behaves like the horizontal one.
A one-sided set (only positive angles) is mirrored, as VituixCAD does for symmetric speakers.
"""

from __future__ import annotations

import numpy as np

Key = tuple[str, float]  # (plane, angle in degrees)

LW = [("hor", 0), ("hor", 10), ("hor", -10), ("hor", 20), ("hor", -20), ("hor", 30), ("hor", -30),
      ("ver", 10), ("ver", -10)]
BOUNCES = {
    "floor": [("ver", -20), ("ver", -30), ("ver", -40)],
    "ceiling": [("ver", 40), ("ver", 50), ("ver", 60)],
    "front_wall": [("hor", 0), ("hor", 10), ("hor", -10), ("hor", 20), ("hor", -20), ("hor", 30), ("hor", -30)],
    "side_wall": [("hor", a) for a in (40, -40, 50, -50, 60, -60, 70, -70, 80, -80)],
    "rear_wall": [("hor", 180), ("hor", 90), ("hor", -90)],
}


def _norm(k: Key) -> Key:
    plane, a = k
    a = float(((round(a) + 180) % 360) - 180) if round(a) != 180 else 180.0
    if plane == "ver" and a == 0:
        plane = "hor"  # one on-axis point, whichever plane it was filed under
    if abs(a) == 180:
        return ("hor", 180.0)
    return (plane, a)


def complete(polars: dict[Key, np.ndarray]) -> dict[Key, np.ndarray]:
    """Normalise keys and mirror a one-sided plane (only >= 0 angles) to its negative side."""
    out = {_norm(k): np.asarray(v) for k, v in polars.items()}
    for plane in ("hor", "ver"):
        side = [a for (p, a) in out if p == plane and a != 0]
        if side and all(a > 0 for a in side):
            for a in side:
                if a != 180:
                    out[(plane, -a)] = out[(plane, a)]
    return out


def _power(polars: dict[Key, np.ndarray], keys: list) -> tuple[np.ndarray | None, list[Key]]:
    have = [_norm(k) for k in keys if _norm(k) in polars]
    missing = [_norm(k) for k in keys if _norm(k) not in polars]
    if not have:
        return None, missing
    return np.mean([np.abs(polars[k]) ** 2 for k in dict.fromkeys(have)], axis=0), missing


def power_weights(keys: list[Key]) -> dict[Key, float]:
    """Sphere-area weight of each direction. Every measured direction at off-axis angle t stands for its
    share of the zone between t-5 and t+5 degrees (10 degree steps), split evenly between the points
    measured at t. Normalised to sum to 1."""
    by_angle: dict[float, list[Key]] = {}
    for k in keys:
        by_angle.setdefault(abs(k[1]), []).append(k)
    w = {}
    for t, ks in by_angle.items():
        lo, hi = max(t - 5, 0), min(t + 5, 180)
        area = np.cos(np.deg2rad(lo)) - np.cos(np.deg2rad(hi))
        for k in ks:
            w[k] = area / len(ks)
    s = sum(w.values())
    return {k: v / s for k, v in w.items()}


def spinorama(polars: dict[Key, np.ndarray]) -> dict:
    """dB curves from complex (or magnitude) responses keyed by (plane, angle)."""
    p = complete(polars)
    if ("hor", 0.0) not in p:
        raise ValueError("the polar set needs the on-axis (0 degree) response")
    db = lambda x: 10 * np.log10(np.maximum(x, 1e-24))  # noqa: E731
    notes = []
    lw, miss = _power(p, LW)
    if miss:
        notes.append(f"listening window without {miss}")
    groups = {}
    for name, keys in BOUNCES.items():
        g, m = _power(p, keys)
        if g is None:
            notes.append(f"early reflections without the {name} bounce (no {keys[0][0]} data)")
        else:
            groups[name] = g
            if m:
                notes.append(f"{name} bounce without {m}")
    er = np.mean(list(groups.values()), axis=0)
    w = power_weights(list(p))
    sp = sum(w[k] * np.abs(v) ** 2 for k, v in p.items())
    if not any(k[0] == "ver" for k in p):
        notes.append("sound power from the horizontal plane only: assumes vertical directivity matches it")
    pir = 0.12 * lw + 0.44 * er + 0.44 * sp
    curves = {"on_axis": db(np.abs(p[("hor", 0.0)]) ** 2), "lw": db(lw), "er": db(er), "sp": db(sp), "pir": db(pir)}
    curves["erdi"] = curves["lw"] - curves["er"]
    curves["spdi"] = curves["lw"] - curves["sp"]
    return {"curves": curves, "notes": notes, "directions": len(p)}


def _fit(freqs: np.ndarray, y: np.ndarray) -> tuple[float, np.ndarray]:
    """Least-squares line in dB per octave; returns (slope, residual)."""
    x = np.log2(freqs)
    a, b = np.polyfit(x, y, 1)
    return float(a), y - (a * x + b)


def evaluate(freqs: np.ndarray, curves: dict, band_hz: tuple[float, float] = (300.0, 10000.0),
             lw_tolerance_db: float = 1.5, pir_slope_db_oct: tuple[float, float] = (-1.2, -0.4),
             di_smoothness_db: float = 1.0) -> dict:
    """Single-speaker targets: a flat listening window, a gently falling PIR, smooth directivity.

    lw: max deviation from its own best-fit line in the band, and that line's slope (want ~0).
    pir: slope (a typical good speaker in a room falls ~0.5 to 1 dB/octave).
    spdi: std about its fit (smooth directivity is what makes a speaker EQ-able in a room)."""
    f = np.asarray(freqs, float)
    m = (f >= band_hz[0]) & (f <= band_hz[1])
    if m.sum() < 8:
        raise ValueError(f"too few points in {band_hz} Hz")
    lw_slope, lw_res = _fit(f[m], curves["lw"][m])
    pir_slope, _ = _fit(f[m], curves["pir"][m])
    _, di_res = _fit(f[m], curves["spdi"][m])
    lw_dev = float(np.max(np.abs(lw_res)))
    out = {
        "band_hz": list(band_hz),
        "lw": {"max_deviation_db": round(lw_dev, 2), "slope_db_per_oct": round(lw_slope, 2),
               "ok": lw_dev <= lw_tolerance_db and abs(lw_slope) <= 0.5},
        "pir": {"slope_db_per_oct": round(pir_slope, 2),
                "ok": pir_slope_db_oct[0] <= pir_slope <= pir_slope_db_oct[1]},
        "spdi": {"roughness_db": round(float(np.std(di_res)), 2), "ok": float(np.std(di_res)) <= di_smoothness_db},
    }
    out["ok"] = all(v["ok"] for v in out.values() if isinstance(v, dict))
    return out
