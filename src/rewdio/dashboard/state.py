"""Snapshot of a session for the dashboard: small, chart-ready, no raw arrays."""

from __future__ import annotations

import numpy as np

from ..analysis.response import Response, smooth_db
from ..analysis.target import target_db
from ..engine.store import Session


def _curve(resp: Response, cfg, offset_db: float = 0.0) -> list[list[float]]:
    d = cfg.dashboard
    lo, hi = d.chart_hz
    n = int(np.floor(np.log2(hi / lo) * d.chart_ppo)) + 1
    f = lo * 2.0 ** (np.arange(n) / d.chart_ppo)
    y = np.interp(np.log(f), np.log(resp.freqs), smooth_db(resp.freqs, resp.mag_db, d.chart_smoothing)) + offset_db
    return [[round(float(a), 2), round(float(b), 2)] for a, b in zip(f, y)]


def snapshot(s: Session) -> dict:
    cfg, wf = s.config, s.workflow
    lp = wf.last_proposal or {}
    curves: dict[str, dict] = {}
    for pos, d in (lp.get("_pred") or {}).items():
        pred = Response.from_dict(d)
        entry = {"predicted": _curve(pred, cfg)}
        meas = s.find(kind="system", position=pos, pass_no=lp.get("pass", wf.pass_no))
        if meas:
            entry["measured"] = _curve(s.response(meas[-1].id), cfg)
        lo, hi = cfg.tuning.bands.mains_level_hz
        m = (pred.freqs >= lo) & (pred.freqs <= hi)
        ref = float(np.median(pred.mag_db[m]))
        t = Response(pred.freqs, 10 ** ((target_db(pred.freqs, cfg.target) + ref) / 20).astype(complex))
        entry["target"] = _curve(t, cfg)
        curves[pos] = entry
    dsp = {c.role: {"dsp_output": c.dsp_output, **s.dsp.get(c.role).model_dump(mode="json")} for c in cfg.channels}
    return {
        "phase": wf.phase, "pass": wf.pass_no, "done": wf.done, "sweeps": len(s.measurements),
        "pending_action": wf.pending_action, "metrics": lp.get("measured") or lp.get("predicted"),
        "measured_ok": lp.get("measured_ok"), "fails": lp.get("measured_fails") or lp.get("predicted_fails"),
        "notes": lp.get("notes", []), "crosscheck": lp.get("crosscheck"), "diagnostics": lp.get("diagnostics"),
        "quality": wf.quality[-20:], "dsp": dsp, "curves": curves, "log": wf.log_tail[-60:],
        "crawl": (wf.crawl.get("result") or {}).get("best_pairs"),
        "refresh_ms": cfg.dashboard.refresh_ms,
    }
