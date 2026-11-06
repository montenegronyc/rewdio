"""Production QA: compare a tower's exported capture set with the reference tower's, check by check.

Works on exported set folders (rewdio_session.json + files), so a reference is just an earlier set marked
`reference: true`, and any two sets can be compared later without the rig. Tolerances are data:
the packaged qa_tolerances.json, overlaid by <REWDIO_HOME>/qa_tolerances.json, which SoundSible can propose
and Lee can edit. Each result: check, subject, pass (or None = skipped, with why), worst_hz, margin (how far
inside the limit, negative = how far outside), detail.

Drivers are matched by their position in the manifest (first with first), so a left tower can be held to a
right reference. The parts checks with an LCR meter stay on the bench; their values go in `parts`.
"""

from __future__ import annotations

import csv
import json
import os
from importlib import resources
from pathlib import Path

import numpy as np

from ..analysis.frd import read_frd
from ..analysis.response import band, smooth_db
from ..config import deep_merge


def tolerances(home: Path | None = None) -> dict:
    tol = json.loads(resources.files("rewdio").joinpath("qa_tolerances.json").read_text())
    user = Path(home or os.environ.get("REWDIO_HOME", Path.home() / ".rewdio")) / "qa_tolerances.json"
    return deep_merge(tol, json.loads(user.read_text())) if user.exists() else tol


class SetData:
    def __init__(self, folder: str | Path):
        self.dir = Path(folder).expanduser()
        self.m = json.loads((self.dir / "rewdio_session.json").read_text())

    @property
    def drivers(self) -> list[dict]:
        return self.m["drivers"]

    def frd(self, rel: str) -> tuple[np.ndarray, np.ndarray]:
        return read_frd(self.dir / rel)

    def zma(self, rel: str) -> tuple[np.ndarray, np.ndarray]:
        rows = [ln.split() for ln in (self.dir / rel).read_text().splitlines() if ln and not ln.startswith("*")]
        a = np.asarray(rows, float)
        return a[:, 0], a[:, 1]

    def dist(self, rel: str) -> dict[str, np.ndarray]:
        lines = [ln for ln in (self.dir / rel).read_text().splitlines() if ln and not ln.startswith("#")]
        rows = list(csv.reader(lines))
        cols = {k: np.array([float(r[i]) if i < len(r) and r[i] else np.nan for r in rows[1:]])
                for i, k in enumerate(rows[0])}
        return cols


def _res(check, subject, ok, worst_hz=None, margin=None, detail="") -> dict:
    return {"check": check, "subject": subject, "pass": ok,
            "worst_hz": None if worst_hz is None else round(float(worst_hz), 1),
            "margin": None if margin is None else round(float(margin), 3), "detail": detail}


def _on_grid(f_src, y_src, f_dst):
    return np.interp(np.log(f_dst), np.log(f_src), y_src, left=np.nan, right=np.nan)


def _on_grid_c(f_src, h_src, f_dst):
    """Complex response on another log grid; NaN outside the source range (never extrapolated)."""
    return _on_grid(f_src, h_src.real, f_dst) + 1j * _on_grid(f_src, h_src.imag, f_dst)


def _band(f, lims):
    return band(f, *lims)


def _zma(d: dict, label: str = "") -> dict | None:
    return next((z for z in d.get("zma", []) if (z.get("label") or "") == label), None)


MIN_COVER = 0.95  # a comparison must see this much of its band; less fails instead of passing on what's left


def _pairs(a: SetData, b: SetData):
    """Drivers matched by position (a left tower can be held to a right reference); the counts must agree."""
    if len(a.drivers) != len(b.drivers):
        raise ValueError(f"{a.dir.name} has {len(a.drivers)} channels and {b.dir.name} has {len(b.drivers)}: "
                         "they are not the same kind of tower")
    return list(zip(a.drivers, b.drivers))


def _missing(check, subject, what) -> dict:
    return _res(check, subject, False, detail=f"{what} is in the reference but missing from this tower")


def _covered(check, subject, vals, f) -> dict | None:
    """A failing result when the candidate covers too little of the band, else None."""
    ok = np.isfinite(vals)
    cover = float(ok.mean()) if vals.size else 0.0
    if cover >= MIN_COVER:
        return None
    lo, hi = (f[ok].min(), f[ok].max()) if ok.any() else (None, None)
    span = f"{lo:.0f}..{hi:.0f} Hz" if lo is not None else "none of it"
    return _res(check, subject, False, detail=f"the tower's data covers {cover:.0%} of the band ({span}); "
                                              f"needs {MIN_COVER:.0%}")


def check_impedance(c: SetData, r: SetData, t: dict) -> list[dict]:
    out = []
    for dc, dr in _pairs(c, r):
        zc, zr = _zma(dc), _zma(dr)
        if zr and not zc:
            out.append(_missing("impedance", dc["role"], "an impedance capture"))
            continue
        if not (zc and zr):
            continue
        fr, ohm_r = r.zma(zr["file"])
        fc, ohm_c = c.zma(zc["file"])
        m = _band(fr, t["band_hz"])
        cand = _on_grid(fc, ohm_c, fr[m])
        if bad := _covered("impedance", dc["role"], cand, fr[m]):
            out.append(bad)
            continue
        dev = np.abs(cand / ohm_r[m] - 1) * 100
        i = int(np.nanargmax(dev))
        lim = t["max_dev_pct"]
        out.append(_res("impedance", dc["role"], bool(dev[i] <= lim), fr[m][i], lim - dev[i],
                        f"worst {dev[i]:.1f} % from the reference (limit {lim:g} %)"))
        lo = float(np.nanmin(cand))
        out.append(_res("impedance_min", dc["role"], lo >= t["min_ohm"], fr[m][int(np.nanargmin(cand))],
                        lo - t["min_ohm"], f"minimum {lo:.2f} ohm (limit {t['min_ohm']:g})"))
    return out


def check_transfer(c: SetData, r: SetData, t: dict) -> list[dict]:
    out = []
    ref = {x["label"]: x for x in r.m.get("transfer", [])}
    cand = {x["label"]: x for x in c.m.get("transfer", [])}
    for label in sorted(set(ref) | set(cand)):
        if label not in cand:
            out.append(_missing("transfer", label, "this network transfer"))
            continue
        if label not in ref:
            out.append(_res("transfer", label, None, detail="no reference transfer with this label"))
            continue
        fr, hr = r.frd(ref[label]["file"])
        fc, hc = c.frd(cand[label]["file"])
        m = _band(fr, t["band_hz"])
        db_r = 20 * np.log10(np.abs(hr))
        m &= db_r > db_r[m].max() - t["passband_window_db"]
        hcm = _on_grid_c(fc, hc, fr[m])
        if bad := _covered("transfer", label, hcm, fr[m]):
            out.append(bad)
            continue
        err = hcm / hr[m]
        db = np.abs(20 * np.log10(np.abs(err)))
        deg = np.abs(np.degrees(np.angle(err)))
        i, j = int(np.nanargmax(db)), int(np.nanargmax(deg))
        ok = db[i] <= t["max_db"] and deg[j] <= t["max_deg"]
        margin = min(t["max_db"] - db[i], (t["max_deg"] - deg[j]) * t["max_db"] / t["max_deg"])
        out.append(_res("transfer", label, bool(ok), fr[m][i if db[i] / t["max_db"] >= deg[j] / t["max_deg"]
                                                       else j], margin,
                        f"worst {db[i]:.2f} dB, {deg[j]:.1f} deg (limits {t['max_db']:g} dB, {t['max_deg']:g} deg)"))
    return out


def _hor0(entries: list[dict]) -> dict | None:
    return next((e for e in entries if e["plane"] == "hor" and e["angle"] == 0), None)


def _on_axis(d: dict, mode: str = "raw") -> dict | None:
    return _hor0(d.get("files", []) if mode == "raw" else d.get("files_xo", []))


def check_polarity(c: SetData, r: SetData, t: dict) -> list[dict]:
    out = []
    for dc, dr in _pairs(c, r):
        ec, er = _on_axis(dc), _on_axis(dr)
        if er and not ec:
            out.append(_missing("polarity", dc["role"], "an on-axis capture"))
            continue
        if not (ec and er):
            continue
        fr, hr = r.frd(er["file"])
        fc, hc = c.frd(ec["file"])
        m = _band(fr, t["band_hz"])
        hcm = _on_grid_c(fc, hc, fr[m])
        if bad := _covered("polarity", dc["role"], hcm, fr[m]):
            out.append(bad)
            continue
        corr = float(np.real(np.nansum(hcm * np.conj(hr[m]))) / np.nansum(np.abs(hcm) * np.abs(hr[m])))
        out.append(_res("polarity", dc["role"], corr > 0, None, corr,
                        "same polarity as the reference" if corr > 0 else "INVERTED against the reference"))
    return out


def _mag_compare(check, subject, fa, ha, fb, hb, band, max_db, smoothing) -> dict:
    m = _band(fb, band)
    sa = smooth_db(fa, 20 * np.log10(np.abs(ha)), smoothing)
    sb = smooth_db(fb, 20 * np.log10(np.abs(hb)), smoothing)
    cand = _on_grid(fa, sa, fb[m])
    if bad := _covered(check, subject, cand, fb[m]):
        return bad
    diff = np.abs(cand - sb[m])
    i = int(np.nanargmax(diff))
    return _res(check, subject, bool(diff[i] <= max_db), fb[m][i], max_db - diff[i],
                f"worst {diff[i]:.2f} dB (1/{smoothing:g} oct, limit {max_db:g} dB)")


def check_frd(c: SetData, r: SetData, t: dict) -> list[dict]:
    out = []
    pairs = [(dc["role"], _on_axis(dc), _on_axis(dr)) for dc, dr in _pairs(c, r)]
    pairs.append(("sum", _hor0(c.m.get("sum_files", [])), _hor0(r.m.get("sum_files", []))))
    for subject, ec, er in pairs:
        if er and not ec:
            out.append(_missing("frd", subject, "an on-axis capture"))
        elif ec and er:
            out.append(_mag_compare("frd", subject, *c.frd(ec["file"]), *r.frd(er["file"]), t["band_hz"],
                                    t["max_db"], t["smoothing_oct"]))
    return out


def check_pair(a: SetData, b: SetData, t: dict) -> list[dict]:
    out = []
    for da, db_ in _pairs(a, b):
        ea, eb = _on_axis(da), _on_axis(db_)
        subject = f"{da['role']}/{db_['role']}"
        if not (ea and eb):
            out.append(_res("pair", subject, False, detail="an on-axis capture is missing from one of the pair"))
            continue
        fa, ha = a.frd(ea["file"])
        fb, hb = b.frd(eb["file"])
        out.append(_mag_compare("pair", subject, fa, ha, fb, hb, t["band_hz"], t["max_db"], t["smoothing_oct"]))
        m = _band(fb, t["phase_band_hz"])
        hm = _on_grid_c(fa, ha, fb[m])
        if bad := _covered("pair_phase", subject, hm, fb[m]):
            out.append(bad)
            continue
        deg = np.abs(np.degrees(np.angle(hm / hb[m])))
        i = int(np.nanargmax(deg))
        out.append(_res("pair_phase", subject, bool(deg[i] <= t["max_deg"]), fb[m][i],
                        t["max_deg"] - deg[i], f"worst {deg[i]:.1f} deg (limit {t['max_deg']:g} deg)"))
    return out


def check_distortion(c: SetData, r: SetData, t: dict, rb: dict) -> list[dict]:
    out = []
    for dc, dr in _pairs(c, r):
        ref = {x["target_spl_db"]: x for x in dr.get("distortion", [])}
        cand = {x["target_spl_db"]: x for x in dc.get("distortion", [])}
        for spl in sorted(set(ref) | set(cand)):
            subject = f"{dc['role']} @{spl:g} dB"
            if spl not in cand:
                out.append(_missing("distortion", subject, "this distortion level"))
                continue
            if spl not in ref:
                out.append(_res("distortion", subject, None, detail="no reference at this level"))
                continue
            dcand, dref = c.dist(cand[spl]["file"]), r.dist(ref[spl]["file"])
            fr = dref["freq_hz"]
            thd_c = _on_grid(dcand["freq_hz"], dcand["thd_pct"], fr)
            if bad := _covered("distortion", subject, thd_c, fr):
                out.append(bad)
                continue
            over = 20 * np.log10(np.maximum(thd_c, 1e-9) / np.maximum(dref["thd_pct"], 1e-9))
            i = int(np.nanargmax(over))
            lim = t["max_over_ref_db"]
            out.append(_res("distortion", subject, bool(over[i] <= lim), fr[i],
                            lim - over[i], f"THD worst {over[i]:+.1f} dB against the reference (limit +{lim:g} dB)"))
            out.append(_rub_buzz(dc["role"], spl, dcand, dref, rb))
    return out


def _rub_buzz(role, spl, dc: dict, dr: dict, t: dict) -> dict:
    """A flag, not a verdict: rub and buzz lives above H9, which REW does not report. Lee still listens."""
    cols = [f"h{n}_pct" for n in t["orders"]]
    have = [k for k in cols if k in dc and k in dr]
    subject = f"{role} @{spl:g} dB"
    if not have:
        return _res("rub_buzz", subject, None,
                    detail=f"REW gave no {cols[0][:-4].upper()}..{cols[-1][:-4].upper()} columns; listen instead")
    c_cols = np.array([dc[k] for k in have])
    r_cols = np.array([_on_grid(dr["freq_hz"], dr[k], dc["freq_hz"]) for k in have])
    both = np.isfinite(c_cols).any(axis=0) & np.isfinite(r_cols).any(axis=0)  # only where both sides have data
    if not both.any():
        return _res("rub_buzz", subject, None, detail="no frequencies where both sides report high harmonics")
    hc = np.sqrt(np.nansum(c_cols[:, both] ** 2, axis=0))
    hr = np.sqrt(np.nansum(r_cols[:, both] ** 2, axis=0))
    limit = np.maximum(hr * 10 ** (t["margin_db"] / 20), t["floor_pct"])
    ratio = hc / limit
    i = int(np.argmax(ratio))
    return _res("rub_buzz", subject, bool(ratio[i] <= 1), dc["freq_hz"][both][i], 1 - ratio[i],
                f"high-order sum {hc[i]:.3f} % at worst (limit {limit[i]:.3f} %); a flag, listen too")


def check_soak(c: SetData, r: SetData, t: dict) -> list[dict]:
    out = []
    soaked = {s["driver"] for s in c.m.get("soak", []) if s.get("completed")}
    for d, dr in _pairs(c, r):
        pre, post = _zma(d), _zma(d, "post_soak")
        if _zma(dr, "post_soak") and not (pre and post):
            out.append(_missing("soak", d["role"], "a soak (impedance before and after)"))
            continue
        if not (pre and post):
            continue
        if d["role"] not in soaked:
            out.append(_res("soak", d["role"], False, detail="a post_soak impedance but no completed soak: the noise "
                                                             "stopped early or never played"))
            continue
        f0, z0 = c.zma(pre["file"])
        f1, z1 = c.zma(post["file"])
        m = _band(f0, t["band_hz"])
        after = _on_grid(f1, z1, f0[m])
        if bad := _covered("soak", d["role"], after, f0[m]):
            out.append(bad)
            continue
        dev = np.abs(after / z0[m] - 1) * 100
        i = int(np.nanargmax(dev))
        out.append(_res("soak", d["role"], bool(dev[i] <= t["max_change_pct"]), f0[m][i], t["max_change_pct"] - dev[i],
                        f"impedance moved {dev[i]:.2f} % after the soak (limit {t['max_change_pct']:g} %)"))
    return out


def find_reference(root: Path) -> Path | None:
    """The most recent exported set marked reference under <root>/measurements/."""
    best = None
    for p in sorted((root / "measurements").glob("*/rewdio_session.json")):
        m = json.loads(p.read_text())
        if m.get("reference"):
            best = p.parent if best is None or m.get("created_et", "") >= json.loads(
                (best / "rewdio_session.json").read_text()).get("created_et", "") else best
    return best


def run(candidate: str | Path, reference: str | Path, pair: str | Path | None = None,
        home: Path | None = None) -> dict:
    tol = tolerances(home)
    c, r = SetData(candidate), SetData(reference)
    if not r.m.get("reference"):
        raise ValueError(f"{r.dir} is not marked as a reference set (speaker_update(reference=true), then export)")
    results = (check_impedance(c, r, tol["impedance"]) + check_transfer(c, r, tol["transfer"])
               + check_polarity(c, r, tol["polarity"]) + check_frd(c, r, tol["frd"])
               + check_distortion(c, r, tol["distortion"], tol["rub_buzz"]) + check_soak(c, r, tol["soak"]))
    if pair:
        results += check_pair(c, SetData(pair), tol["pair"])
    failed = [f"{x['check']} {x['subject']}" for x in results if x["pass"] is False]
    report = {"serial": c.m.get("serial"), "reference": str(r.dir), "reference_serial": r.m.get("serial"),
              "pair": str(pair) if pair else None, "pass": not failed and any(x["pass"] for x in results),
              "failed": failed, "results": results, "tolerances": tol}
    (c.dir / "qa_report.json").write_text(json.dumps(report, indent=1))
    c.m["qa"] = {k: report[k] for k in ("pass", "failed", "reference", "reference_serial", "pair", "results")}
    (c.dir / "rewdio_session.json").write_text(json.dumps(c.m, indent=1))
    return report
