"""Explanations for what the loop could not fix, for the final report.

Room-mode prediction/correlation and SBIR quarter-wave hints follow koltyj/REW-mcp; the
multi-seat spread/outlier report follows daredoole/audio-calibration-mcp (both MIT).
"""

from __future__ import annotations

import itertools

import numpy as np

from ..config import SystemConfig
from .metrics import extrema
from .response import Response, band, smooth_db


def room_modes(dims: tuple[float, float, float], c: float, f_max: float) -> list[dict]:
    L = np.asarray(dims, float)
    n_max = (2 * f_max * L / c).astype(int) + 1
    modes = []
    for n in itertools.product(*(range(k + 1) for k in n_max)):
        nz = sum(1 for v in n if v)
        if nz == 0:
            continue
        f = c / 2 * float(np.sqrt(np.sum((np.asarray(n) / L) ** 2)))
        if f <= f_max:
            modes.append({"hz": round(f, 1), "order": list(n), "type": ("axial", "tangential", "oblique")[nz - 1]})
    return sorted(modes, key=lambda m: m["hz"])


def correlate_modes(features: list[dict], modes: list[dict], tol: float) -> list[dict]:
    out = []
    for p in features:
        near = [m for m in modes if abs(m["hz"] - p["hz"]) <= tol * p["hz"]]
        if near:
            best = min(near, key=lambda m: abs(m["hz"] - p["hz"]))
            out.append({**p, "mode": best})
    return out


def sbir_hints(nulls: list[dict], cfg: SystemConfig) -> list[dict]:
    d = cfg.tuning.diagnostics
    c = cfg.tuning.physics.speed_of_sound_m_s
    lo, hi = d.sbir_band_hz
    return [{**n, "boundary_distance_m": round(c / (4 * n["hz"]), 2),
             "hint": f"a {n['hz']:.0f} Hz null with Q {n['q']} may be a boundary cancellation (SBIR): a surface about "
                     f"{c / (4 * n['hz']):.2f} m from the speaker (wall behind it, floor). If it is, EQ cannot fix it; "
                     f"moving the speaker or treating that surface can."}
            for n in nulls if lo <= n["hz"] <= hi and n["q"] >= d.sbir_min_q]


def seat_report(system: dict[str, Response], cfg: SystemConfig) -> dict:
    d = cfg.tuning.diagnostics
    seats = list(system)
    if len(seats) < 2:
        return {}
    f = system[seats[0]].freqs
    m = band(f, *d.seat_band_hz)
    curves = np.array([smooth_db(f, system[s].mag_db, d.seat_smoothing)[m] for s in seats])
    curves -= curves.mean(axis=1, keepdims=True)
    spread = curves.max(axis=0) - curves.min(axis=0)
    flagged = f[m][spread >= d.seat_spread_flag_db]
    med = np.median(curves, axis=0)
    outliers = {s: round(float(np.mean(np.abs(curves[i] - med) > d.seat_outlier_db)) * 100, 1) for i, s in enumerate(seats)}
    return {"seat_dependent_hz": [round(float(x), 1) for x in flagged[:: max(1, flagged.size // 12)]],
            "max_seat_spread_db": round(float(spread.max()), 1),
            "percent_of_band_outlying_by_seat": outliers}


def diagnose(cfg: SystemConfig, system: dict[str, Response], mains_solo: dict[str, Response]) -> dict:
    d = cfg.tuning.diagnostics
    first = system[next(iter(system))]
    feats = extrema(first.freqs, first.mag_db, *cfg.tuning.bands.bass_eval_hz)
    out: dict = {"residual_peaks": feats["peaks"], "residual_nulls": feats["nulls"]}
    if cfg.room_dims_m:
        modes = room_modes(cfg.room_dims_m, cfg.tuning.physics.speed_of_sound_m_s, d.mode_max_hz)
        out["peaks_matching_room_modes"] = correlate_modes(feats["peaks"], modes, d.mode_match_tolerance)
        out["axial_modes_hz"] = [m["hz"] for m in modes if m["type"] == "axial"]
    hints = []
    for role, r in mains_solo.items():
        for h in sbir_hints(extrema(r.freqs, r.mag_db, *d.sbir_band_hz)["nulls"], cfg):
            hints.append({"channel": role, **h})
    out["sbir"] = hints
    out["seats"] = seat_report(system, cfg)
    return out
