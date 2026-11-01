"""Exporting a capture set (VituixCAD FRDs, ZMA, distortion, the SoundSible manifest) and the spinorama report.
Mixed into SpeakerCapture; see engine/speaker.py for the capture side."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from ..analysis.frd import (
    FrdFile,
    Gate,
    arrival_s,
    gated_response,
    remove_protection,
    stable_band,
    vituix_name,
)
from ..analysis.spin import evaluate, spinorama
from ..minidsp.biquad import cascade_response
from ..minidsp.state import XoverFilter, xover_sections
from .speaker_meta import ANGLE_CONVENTION, FRAME, now_et, set_folder

MANIFEST_SCHEMA = "rewdio.speaker-session/3"
ON_AXIS = {("hor", 0.0), ("ver", 0.0)}

if TYPE_CHECKING:
    from .speaker import SpeakerState


class ExportMixin:
    # -- export -----------------------------------------------------------------------

    def _raw_ir(self, mid: str) -> tuple[np.ndarray, float, float]:
        p = self.s.dir / "data" / f"{mid}.raw.npz"
        if p.exists():  # float32 binary: ~1 ms to read, a quarter of the JSON's size
            with np.load(p) as z:
                return z["data"].astype(float), float(z["fs"]), float(z["t0"])
        d = json.loads((self.s.dir / "data" / f"{mid}.raw.json").read_text())  # sets saved before
        return np.asarray(d["data"], float), float(d["fs"]), float(d["t0"])

    def _snapshot(self, mid: str, driver: str):
        rec = next(m for m in self.s.measurements if m.id == mid)
        return rec.snapshot.get(driver).model_copy(deep=True)

    def _protection(self, mid: str, driver: str) -> list[XoverFilter]:
        return [f for f in (self._snapshot(mid, driver).crossover or []) if f.protect]

    def _gated_set(self, st: SpeakerState, gate: Gate, ppo: int, f_lo: float, f_hi: float,
                   t_ref_s: float | None, check: bool = True) -> tuple[np.ndarray, float, list[str], list[tuple]]:
        """Every capture gated on one absolute window, protection divided out: (freqs, t_ref, notes,
        [(capture, h, protection filters, protection note, gate-checked band or None)])."""
        if not st.captures:
            raise ValueError("nothing captured yet")
        fs_dsp = self.s.config.dsp_sample_rate
        irs = {c.id: self._raw_ir(c.id) for c in st.captures}
        nyq = min(fs for _, fs, _ in irs.values()) / 2
        freqs = f_lo * 2.0 ** (np.arange(int(np.log2(min(f_hi, nyq * 0.95) / f_lo) * ppo) + 1) / ppo)

        # ONE reference time for the whole set: the earliest on-axis raw arrival (or earliest raw anywhere)
        raw = [c for c in st.captures if c.mode == "raw"] or st.captures
        ref_caps = [c for c in raw if (c.plane, c.angle) in ON_AXIS] or raw
        if t_ref_s is None:
            t_ref_s = min(arrival_s(*irs[c.id]) for c in ref_caps)
        expected = st.distance_m / st.environment.speed_of_sound
        notes = []
        if abs(t_ref_s - expected) > 0.002:
            notes.append(f"first arrival at {t_ref_s * 1000:.2f} ms, but {st.distance_m:g} m is "
                         f"{expected * 1000:.2f} ms: DSP latency or a wrong distance; phase is still consistent")

        early = [f"{c.driver} {c.plane} {c.angle:g}" for c in st.captures
                 if arrival_s(*irs[c.id]) < t_ref_s - (gate.pre_ms - gate.taper_ms) / 1000]
        if early:
            notes.append(f"{len(early)} captures arrive more than {gate.pre_ms - gate.taper_ms:g} ms before the "
                         f"reference and are clipped by the gate's start (e.g. {early[0]}): raise pre_ms")
        levels = {m.id: m.level_dbfs for m in self.s.measurements}
        ref_level = levels.get(ref_caps[0].id, 0.0)
        rows = []
        for c in st.captures:
            ir, fs, t0 = irs[c.id]
            # every capture as if swept at the set's first level: a level change mid-set is not a response change
            h = gated_response(ir, fs, freqs, t_ref_s, gate, t0_s=t0) * 10 ** ((ref_level - levels.get(c.id, ref_level))
                                                                                / 20)
            band = stable_band(ir, fs, freqs, t_ref_s, gate, t0_s=t0) if check else None
            # only raw captures have the protection filter divided out: xo and sum are exported exactly as played,
            # so an audition whose protect filter is one of the target's own sections compares with nothing removed
            prot = self._protection(c.id, c.driver) if c.mode == "raw" else []
            prot_note = "none" if c.mode == "raw" else "included: exported exactly as it played"
            if c.mode == "raw":  # the driver alone: take out everything the DSP output added
                o = self._snapshot(c.id, c.driver)
                o.crossover = None  # the protection filter is handled below, with its floor
                h = h / o.transfer(freqs, fs_dsp)
            if prot:
                h, first = remove_protection(h, cascade_response(xover_sections(prot, fs_dsp), freqs, fs_dsp))
                lo = float(freqs[first]) if first is not None else None
                prot_note = (f"divided out ({', '.join(f.label() for f in prot)}); "
                             f"magnitude fully corrected above {lo:.0f} Hz" if lo else "divided out")
            rows.append((c, h, prot, prot_note, band))
        return freqs, t_ref_s, notes, rows

    def folder(self, st: SpeakerState, root: str | Path | None = None) -> Path:
        """`<root>/measurements/<YYYY-MM-DD>_<site>_<variant>/`; root defaults to REWDIO_HOME."""
        base = Path(root).expanduser() if root else self.s.dir.parent
        return base / "measurements" / set_folder(st.environment, st.variant)

    def export(self, out_dir: str | Path | None = None, gate: Gate | None = None, ppo: int = 48,
               f_lo: float = 10.0, f_hi: float = 24000.0, t_ref_s: float | None = None,
               root: str | Path | None = None) -> dict:
        """Write the set: manifest at the top, frd/<driver>/, zma/, distortion/. `out_dir` is the exact set
        folder; otherwise `<root>/measurements/<date>_<site>_<variant>/`."""
        st = self._require()
        gate = gate or Gate()
        out = Path(out_dir).expanduser() if out_dir else self.folder(st, root)
        old = out / "rewdio_session.json"
        if old.exists():  # same folder name (same day, site and variant) but another set: never overwrite it
            m = json.loads(old.read_text())
            if (m.get("speaker"), m.get("variant"), (m.get("environment") or {}).get("started_et")) != \
                    (st.speaker, st.variant, st.environment.started_et):
                raise ValueError(f"{out} already holds set {m.get('speaker')} / {m.get('variant')}: give this one "
                                 "another variant (speaker_update) or out_dir")
        freqs, t_ref_s, notes, rows = self._gated_set(st, gate, ppo, f_lo, f_hi, t_ref_s)
        files, entries = [], []
        for c, h, prot, prot_note, band in rows:
            folder = c.driver if c.mode == "raw" else ("sum" if c.mode == "sum" else f"{c.driver}_xo")
            rel = Path("frd") / folder / vituix_name(st.speaker, folder, c.plane, c.angle)
            band_txt = (f"gate check: stable within 0.5 dB when the gate end moves +1/+2 ms from "
                        f"{band[0]:.0f} to {band[1]:.0f} Hz" if band else "gate check: no stable band found")
            what = {"raw": "raw driver capture (DSP output state divided out)",
                    "xo": "driver through its output as configured (crossover incl. protection, gain, delay, "
                          "polarity, PEQ)",
                    "sum": "all drivers together through their outputs"}[c.mode]
            hdr = [f"Rewdio {what}: {st.speaker} {c.driver} {c.plane} {c.angle:g} deg ({st.variant})",
                   f"REW measurement {c.id}; {self.s.config.timing_mode.value} time base; common delay removed "
                   f"{t_ref_s * 1000:.4f} ms",
                   f"gate {-gate.pre_ms:g}..{gate.gate_ms:g} ms around the reference; 1/T floor "
                   f"~{gate.valid_from_hz:.0f} Hz",
                   band_txt,
                   f"protection filter: {prot_note}",
                   "VituixCAD: set this driver's X/Y/Z offsets to 0 (the offsets are in the phase)",
                   "freq(Hz)\tSPL(dB)\tphase(deg)"]
            files.append(FrdFile(out / rel, freqs, h, hdr).write())
            entries.append({"driver": c.driver, "mode": c.mode, "plane": c.plane, "angle": c.angle, "file": rel.as_posix(),
                            "measurement_id": c.id, "valid_band_hz": list(band) if band else None,
                            "protection_removed": [f.model_dump() for f in prot]})

        nf_entries = []
        fs_dsp = self.s.config.dsp_sample_rate
        for n in st.nearfield:
            r = self.s.response(n.id)
            o = self._snapshot(n.id, n.driver)  # divide out the DSP output as for the far-field raw files
            prot = [f for f in o.crossover or [] if f.protect]
            o.crossover = None
            h = r.h / o.transfer(r.freqs, fs_dsp)
            if prot:
                h, _ = remove_protection(h, cascade_response(xover_sections(prot, fs_dsp), r.freqs, fs_dsp))
            rel = Path("frd") / n.driver / f"{st.speaker}_{n.driver} {n.label}.frd"
            FrdFile(out / rel, r.freqs, h, [f"Rewdio near-field capture: {st.speaker} {n.driver} ({n.label})",
                                              f"REW measurement {n.id}; ungated; for VituixCAD's Merger (magnitude "
                                              "below the far-field gate limit)",
                                              "freq(Hz)\tSPL(dB)\tphase(deg)"]).write()
            nf_entries.append({"driver": n.driver, "label": n.label, "file": rel.as_posix(), "measurement_id": n.id})

        zma = self._export_zma(st, out)
        dist = self._export_distortion(st, out)
        gb = st.geometry.ground_bounce_ms(st.environment.speed_of_sound)
        if gb is not None and gb < gate.gate_ms:
            notes.append(f"the ground bounce arrives {gb:.2f} ms after the direct sound, inside the {gate.gate_ms:g} ms "
                         "gate: shorten the gate, or raise the speaker or the mic")
        missing = [f"geometry.{k}" for k in st.geometry.missing()] + [f"environment.{k}" for k in st.environment.missing()]
        if missing:
            notes.append(f"not recorded: {', '.join(missing)} (pass them to speaker_plan)")
        manifest = self._manifest(st, gate, t_ref_s, entries, nf_entries, zma, dist, freqs, notes)
        manifest.update(self._export_production(st, out))
        mpath = out / "rewdio_session.json"
        mpath.write_text(json.dumps(manifest, indent=1))
        st.exported_to = str(out)
        self._save(st)
        return {"ok": True, "dir": str(out), "files": len(files), "nearfield": len(nf_entries), "zma": len(zma),
                "distortion": len(dist), "manifest": str(mpath), "t_ref_ms": round(t_ref_s * 1000, 4),
                "notes": notes}

    def report(self, gate: Gate | None = None, band_hz: tuple[float, float] = (300.0, 10000.0),
               ppo: int = 24) -> dict:
        """Spinorama of the measured drivers summed through the CONFIGURED crossover and the current
        gain/delay/polarity/PEQ, plus the single-speaker targets. Also each driver alone, for diagnosis."""
        st = self._require()
        gate = gate or Gate()
        freqs, _, notes, rows = self._gated_set(st, gate, ppo, 20.0, 20000.0, None, check=False)
        fs_dsp = self.s.config.dsp_sample_rate
        xfer = {}
        for d in st.drivers:
            o = self.s.dsp.get(d).model_copy(deep=True)
            o.crossover = list(self.s.config.channel(d).crossover or [])
            xfer[d] = o.transfer(freqs, fs_dsp)
        system: dict[tuple, np.ndarray] = {}
        per: dict[str, dict[tuple, np.ndarray]] = {d: {} for d in st.drivers}
        for c, h, *_ in rows:
            if c.mode != "raw":
                continue
            k = (c.plane, c.angle)
            per[c.driver][k] = h
        for k in set().union(*[set(v) for v in per.values()]):
            if all(k in per[d] for d in st.drivers):  # only directions where every driver was measured
                system[k] = sum(per[d][k] * xfer[d] for d in st.drivers)
        if not system:
            raise ValueError("speaker_report needs raw captures (plan with modes including 'raw')")
        spin = spinorama(system)
        curves = spin["curves"]
        valid = freqs >= max(gate.valid_from_hz, band_hz[0])
        ev = evaluate(freqs[valid], {k: v[valid] for k, v in curves.items()},
                      (max(band_hz[0], gate.valid_from_hz), band_hz[1]))
        return {"speaker": st.speaker, "targets": ev, "notes": notes + spin["notes"],
                "freqs": [round(float(f), 2) for f in freqs],
                "curves_db": {k: [round(float(x), 2) for x in v] for k, v in curves.items()},
                "drivers_on_axis_db": {d: [round(float(x), 2) for x in 20 * np.log10(np.maximum(
                    np.abs(per[d].get(("hor", 0.0), np.full(freqs.size, 1e-12)) * xfer[d]), 1e-12))]
                    for d in st.drivers}}

    def _manifest(self, st: SpeakerState, gate: Gate, t_ref_s: float, entries: list[dict], nf: list[dict],
                  zma: list[dict], dist: list[dict], freqs: np.ndarray, notes: list[str]) -> dict:
        cfg = self.s.config
        chans = {c.role: c for c in cfg.channels}
        g, env = st.geometry, st.environment
        return {
            "schema": MANIFEST_SCHEMA,
            "created_et": now_et().isoformat(timespec="seconds"),
            "speaker": st.speaker,
            "variant": st.variant,
            "geometry": {"frame": FRAME, "pivot_xyz": g.pivot_xyz, "mic_xyz": g.mic_xyz,
                         "mic_given_as": g.mic_given_as, "mic_distance_m": round(st.distance_m, 4),
                         "riser_h": g.riser_h, "origin_height_m": g.origin_height_m,
                         "mic_height_m": g.mic_height_m, "horn_offset_m": g.horn_offset_m,
                         "horn_stem_lift_m": g.horn_stem_lift_m, "horn_id": g.horn_id, "horn_stl": g.horn_stl,
                         "series_cap_uf": g.series_cap_uf, "series_cap_in_circuit": g.series_cap_in_circuit,
                         "ground_bounce_after_direct_ms": (round(gb, 3) if (gb := g.ground_bounce_ms(
                             env.speed_of_sound)) is not None else None),
                         "angle_convention": ANGLE_CONVENTION, "driver_offsets_in_phase": True},
            "environment": {"temperature_c": env.temperature_c, "speed_of_sound_m_s": round(env.speed_of_sound, 2),
                            "indoor": env.indoor, "site": env.site, "started_et": env.started_et,
                            "soundsible_commit": env.soundsible_commit},
            "timing": {"reference": cfg.timing_mode.value, "common_delay_removed_ms": round(t_ref_s * 1000, 4)},
            "gate": {"pre_ms": gate.pre_ms, "gate_ms": gate.gate_ms, "taper_ms": gate.taper_ms,
                     "valid_from_hz": round(gate.valid_from_hz, 1),
                     "gate_check": {"extend_ms": [1.0, 2.0], "tolerance_db": 0.5, "smoothing": "1/12 oct",
                                    "per_file": "drivers[].files[].valid_band_hz"}},
            "band_hz": [float(freqs[0]), float(freqs[-1])],
            "drivers": [{"role": d, "dsp_output": chans[d].dsp_output,
                         "crossover_configured": [f.model_dump() for f in chans[d].crossover or []],
                         "files": [e for e in entries if e["driver"] == d and e["mode"] == "raw"],
                         "files_xo": [e for e in entries if e["driver"] == d and e["mode"] == "xo"],
                         "nearfield": [n for n in nf if n["driver"] == d],
                         "zma": [z for z in zma if z["driver"] == d],
                         "distortion": [x for x in dist if x["driver"] == d]} for d in st.drivers],
            "sum_files": [e for e in entries if e["mode"] == "sum"],
            "capture_modes": st.modes,
            "mic_cal": cfg.mic_cal_path,
            "dsp_sample_rate": cfg.dsp_sample_rate,
            "notes": notes,
        }
