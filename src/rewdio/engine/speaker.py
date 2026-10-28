"""Speaker measurement: raw driver captures around a (manual) turntable, exported as VituixCAD FRDs.

Separate from the room-calibration autopilot. The flow:

    plan(speaker, points, geometry, env, variant) -> which angles, which drivers, and the setup
    step()  -> asks the user to rotate to the next angle, or sweeps every driver at the current one
    confirm(key)                              -> the user rotated
    nearfield(driver, label)                  -> optional near-field sweep for VituixCAD's Merger
    impedance(driver) / distortion(driver)    -> ZMA and harmonic distortion for the passive design
    export(root or out_dir, gate)             -> measurements/<date>_<site>_<variant>/ with the manifest

Every driver sweep is RAW: the crossover block holds only protection filters (their exact response
is divided out on export) so VituixCAD sees the driver, not the crossover. The configured crossover is
written back at the end of every step, so the speaker is never left raw while waiting on the user.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
from pydantic import BaseModel, Field

from ..analysis.frd import FrdFile, Gate, arrival_s, gated_response, remove_protection, stable_band, vituix_name
from ..analysis.spin import evaluate, spinorama
from ..config import ChannelKind
from ..minidsp.biquad import cascade_response
from ..minidsp.state import XoverFilter, xover_sections
from .rig import Rig
from .speaker_extras import Distortion, Impedance, PassiveExtras
from .speaker_meta import ANGLE_CONVENTION, FRAME, AnglePlan, Environment, Geometry, now_et, set_folder
from .store import Session

ON_AXIS = {("hor", 0.0), ("ver", 0.0)}
MANIFEST_SCHEMA = "rewdio.speaker-session/2"


class Point(BaseModel):
    plane: str
    angle: float


class Capture(BaseModel):
    driver: str
    plane: str
    angle: float
    id: str


class Nearfield(BaseModel):
    driver: str
    label: str
    id: str


class SpeakerState(BaseModel):
    speaker: str
    drivers: list[str]
    distance_m: float
    points: list[Point]
    variant: str = "default"
    geometry: Geometry = Field(default_factory=Geometry)
    environment: Environment = Field(default_factory=Environment)
    impedance: list[Impedance] = Field(default_factory=list)
    distortion: list[Distortion] = Field(default_factory=list)
    exported_to: str | None = None
    index: int = 0
    rotated_to: int | None = None  # index of the point the user last confirmed rotating to
    captures: list[Capture] = Field(default_factory=list)
    nearfield: list[Nearfield] = Field(default_factory=list)
    created: float = Field(default_factory=time.time)

    @property
    def done(self) -> bool:
        return self.index >= len(self.points)


def angles_for(planes: list[str], step_deg: float = 10.0, max_deg: float = 180.0, symmetric: bool = False) -> list[Point]:
    """0 first (once), then each plane outwards. symmetric=True measures one side only (VituixCAD mirrors)."""
    if step_deg <= 0 or max_deg <= 0:
        raise ValueError("step and max angle must be positive")
    pts = [Point(plane=planes[0], angle=0.0)]
    n = int(round(max_deg / step_deg))
    for plane in planes:
        for k in range(1, n + 1):
            a = round(k * step_deg, 3)
            pts.append(Point(plane=plane, angle=a))
            if not symmetric and a < 180:
                pts.append(Point(plane=plane, angle=-a))
    return pts


def points_from(plan: AnglePlan) -> list[Point]:
    """Explicit angle lists: 0 once, then each plane outwards (with negatives when both_signs)."""
    planes = [p for p in ("hor", "ver") if getattr(plan, p)] or ["hor"]
    pts = [Point(plane=planes[0], angle=0.0)]
    for plane in planes:
        for a in sorted({float(a) for a in getattr(plan, plane) if a != 0}):
            pts.append(Point(plane=plane, angle=a))
            if plan.both_signs and a < 180:
                pts.append(Point(plane=plane, angle=-a))
    return pts


class SpeakerCapture(PassiveExtras):
    FILE = "speaker.json"

    def __init__(self, rig: Rig):
        self.rig = rig

    @property
    def s(self) -> Session:
        return self.rig.s

    # -- state ------------------------------------------------------------------------

    def state(self) -> SpeakerState | None:
        p = self.s.dir / self.FILE
        return SpeakerState.model_validate_json(p.read_text()) if p.exists() else None

    def _save(self, st: SpeakerState) -> None:
        tmp = self.s.dir / (self.FILE + ".tmp")
        tmp.write_text(st.model_dump_json(indent=1))
        tmp.replace(self.s.dir / self.FILE)

    def _require(self) -> SpeakerState:
        st = self.state()
        if st is None:
            raise ValueError("no speaker capture planned; call speaker_plan first")
        return st

    def drivers_of(self, speaker: str) -> list[str]:
        cfg = self.s.config
        ds = [c for c in cfg.channels if c.kind == ChannelKind.DRIVER and c.speaker == speaker]
        if not ds:
            raise ValueError(f"no driver channels for speaker {speaker!r}: give channels kind 'driver' and "
                             f"speaker {speaker!r}")
        unmanaged = [c.role for c in ds if c.crossover is None]
        if unmanaged:
            raise ValueError(f"{unmanaged} have no `crossover` in the config, so Rewdio cannot make them raw. "
                             "Give each driver its crossover (use [] for none) and mark protection filters "
                             "protect=true")
        return [c.role for c in ds]

    def plan(self, speaker: str, points: list[Point], distance_m: float | None = None,
             geometry: Geometry | None = None, environment: Environment | None = None, variant: str = "default",
             replace: bool = False) -> SpeakerState:
        """Start a capture set. Each physical variant (horn offset, riser height...) is its own set."""
        old = self.state()
        if old is not None and old.captures and not old.exported_to and not replace:
            raise ValueError(f"the current set ({old.speaker}, variant {old.variant!r}) has {len(old.captures)} "
                             "captures that were never exported: export_frd first, or pass replace=True")
        geometry = geometry or Geometry()
        distance_m = distance_m if distance_m is not None else geometry.mic_distance()
        if distance_m is None:
            raise ValueError("give distance_m, or pivot_xyz and the mic position")
        st = SpeakerState(speaker=speaker, drivers=self.drivers_of(speaker), distance_m=distance_m, points=points,
                          variant=variant, geometry=geometry, environment=environment or Environment())
        self._save(st)
        return st

    # -- capture ----------------------------------------------------------------------

    def _ask(self, st: SpeakerState) -> dict:
        p = st.points[st.index]
        first = st.index == 0
        g = st.geometry
        where = (f"turntable axis at {g.pivot_xyz} and mic at {g.mic_xyz} (metres; x forward, y to the speaker's "
                 "left, z up, from the top-front centre of the woofer box)" if g.pivot_xyz and g.mic_xyz else
                 "turntable axis under the top-front centre of the woofer box, mic straight ahead")
        prev = st.points[st.index - 1] if st.index else None
        side = (" Lay the speaker on its RIGHT side for the vertical plane, keeping the same pivot."
                if p.plane == "ver" and (prev is None or prev.plane != "ver") else "")
        txt = (f"Set up {st.speaker} ({st.variant}): {where}, {st.distance_m:g} m apart. Start at 0 degrees. "
               "Do not move the mic or the pivot until every angle is done."
               if first else
               f"Rotate {st.speaker} to {p.angle:+g} degrees in the {'horizontal' if p.plane == 'hor' else 'vertical'} "
               f"plane ({ANGLE_CONVENTION[p.plane]}).{side} Keep the mic where it is.")
        return {"status": "user_action_required",
                "action": {"key": f"rotate:{st.index}", "instructions": txt,
                           "sim": {"polar": {"plane": p.plane, "angle": p.angle, "speaker": st.speaker}}},
                "progress": f"{st.index}/{len(st.points)} angles"}

    def confirm(self, key: str | None = None) -> dict:
        st = self._require()
        if st.done:
            return {"ok": False, "message": "every angle is captured"}
        want = f"rotate:{st.index}"
        if key is not None and key != want:
            return {"ok": False, "message": f"waiting on {want}, not {key}"}
        st.rotated_to = st.index
        self._save(st)
        return {"ok": True, "confirmed": want}

    async def _raw(self, roles: list[str], raw: bool) -> None:
        for r in roles:
            await self.rig.set_crossover(r, raw=raw)

    async def _sweep(self, role: str, kind: str, position: str, **extra) -> str:
        rec, _ = await self.rig.measure([role], kind, position, **extra)
        ir, fs, t0 = await self.rig.rew.impulse_response_timed(rec.id)
        self.s._write(f"data/{rec.id}.raw.json", {"fs": fs, "t0": t0, "data": [float(x) for x in ir]})
        return rec.id

    async def step(self) -> dict:
        st = self._require()
        if st.done:
            return {"status": "done", "captures": len(st.captures), "next": "export_frd"}
        if st.rotated_to != st.index:
            return self._ask(st)
        p = st.points[st.index]
        took = []
        try:
            await self._raw(st.drivers, raw=True)
            for d in st.drivers:
                mid = await self._sweep(d, "driver", f"{p.plane} {p.angle:g}", speaker=st.speaker, driver=d,
                                        plane=p.plane, angle=p.angle)
                st.captures = [c for c in st.captures if not (c.driver == d and c.plane == p.plane
                                                              and c.angle == p.angle)]
                st.captures.append(Capture(driver=d, plane=p.plane, angle=p.angle, id=mid))
                took.append(mid)
        finally:
            await self._raw(st.drivers, raw=False)  # never leave the speaker raw while waiting on the user
        bad = [f | {"driver": m.roles[0]} for m in self.s.measurements if m.id in took
               for f in m.extra.get("quality", []) if f.get("level") == "critical"]
        if bad:  # same angle again once fixed: a clipped or noisy sweep would poison the whole set
            self._save(st)
            return {"status": "retry", "point": p.model_dump(), "quality": bad,
                    "message": "fix the problem (level, noise, cabling), then call speaker_step again"}
        st.index += 1
        self._save(st)
        rep = {"status": "measured", "point": p.model_dump(), "measurements": took,
               "progress": f"{st.index}/{len(st.points)} angles"}
        if not st.done:
            rep["next"] = self._ask(st)
        return rep

    async def nearfield(self, driver: str, label: str = "nf") -> dict:
        st = self._require()
        if driver not in st.drivers:
            raise ValueError(f"{driver} is not a driver of {st.speaker}; drivers: {st.drivers}")
        try:
            await self._raw(st.drivers, raw=True)
            mid = await self._sweep(driver, "nearfield", label, speaker=st.speaker, driver=driver)
        finally:
            await self._raw(st.drivers, raw=False)
        st.nearfield = [n for n in st.nearfield if not (n.driver == driver and n.label == label)]
        st.nearfield.append(Nearfield(driver=driver, label=label, id=mid))
        self._save(st)
        return {"ok": True, "id": mid}

    # -- export -----------------------------------------------------------------------

    def _raw_ir(self, mid: str) -> tuple[np.ndarray, float, float]:
        d = json.loads((self.s.dir / "data" / f"{mid}.raw.json").read_text())
        return np.asarray(d["data"], float), float(d["fs"]), float(d["t0"])

    def _protection(self, mid: str, driver: str) -> list[XoverFilter]:
        rec = next(m for m in self.s.measurements if m.id == mid)
        return [f for f in (rec.snapshot.get(driver).crossover or []) if f.protect]

    def _gated_set(self, st: SpeakerState, gate: Gate, ppo: int, f_lo: float, f_hi: float,
                   t_ref_s: float | None) -> tuple[np.ndarray, float, list[str], list[tuple]]:
        """Every capture gated on one absolute window, protection divided out: (freqs, t_ref, notes,
        [(capture, h, protection filters, protection note, gate-checked band or None)])."""
        if not st.captures:
            raise ValueError("nothing captured yet")
        fs_dsp = self.s.config.dsp_sample_rate
        irs = {c.id: self._raw_ir(c.id) for c in st.captures}
        nyq = min(fs for _, fs, _ in irs.values()) / 2
        freqs = f_lo * 2.0 ** (np.arange(int(np.log2(min(f_hi, nyq * 0.95) / f_lo) * ppo) + 1) / ppo)

        # ONE reference time for the whole set: the earliest on-axis arrival (or earliest anywhere)
        ref_caps = [c for c in st.captures if (c.plane, c.angle) in ON_AXIS] or st.captures
        if t_ref_s is None:
            t_ref_s = min(arrival_s(*irs[c.id]) for c in ref_caps)
        expected = st.distance_m / st.environment.speed_of_sound
        notes = []
        if abs(t_ref_s - expected) > 0.002:
            notes.append(f"first arrival at {t_ref_s * 1000:.2f} ms, but {st.distance_m:g} m is "
                         f"{expected * 1000:.2f} ms: DSP latency or a wrong distance; phase is still consistent")

        rows = []
        for c in st.captures:
            ir, fs, t0 = irs[c.id]
            h = gated_response(ir, fs, freqs, t_ref_s, gate, t0_s=t0)
            band = stable_band(ir, fs, freqs, t_ref_s, gate, t0_s=t0)
            prot = self._protection(c.id, c.driver)
            prot_note = "none"
            if prot:
                h, first = remove_protection(h, cascade_response(xover_sections(prot, fs_dsp), freqs, fs_dsp))
                lo = float(freqs[first]) if first is not None else None
                prot_note = (f"divided out ({', '.join(f'{f.topology} {f.kind} {f.freq_hz:g} Hz' for f in prot)}); "
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
        freqs, t_ref_s, notes, rows = self._gated_set(st, gate, ppo, f_lo, f_hi, t_ref_s)
        files, entries = [], []
        for c, h, prot, prot_note, band in rows:
            rel = Path("frd") / c.driver / vituix_name(st.speaker, c.driver, c.plane, c.angle)
            band_txt = (f"gate check: stable within 0.5 dB when the gate end moves +1/+2 ms from "
                        f"{band[0]:.0f} to {band[1]:.0f} Hz" if band else "gate check: no stable band found")
            hdr = [f"Rewdio raw driver capture: {st.speaker} {c.driver} {c.plane} {c.angle:g} deg ({st.variant})",
                   f"REW measurement {c.id}; loopback time base; common delay removed {t_ref_s * 1000:.4f} ms",
                   f"gate {-gate.pre_ms:g}..{gate.gate_ms:g} ms around the reference; 1/T floor "
                   f"~{gate.valid_from_hz:.0f} Hz",
                   band_txt,
                   f"protection filter: {prot_note}",
                   "VituixCAD: set this driver's X/Y/Z offsets to 0 (the offsets are in the phase)",
                   "freq(Hz)\tSPL(dB)\tphase(deg)"]
            files.append(FrdFile(out / rel, freqs, h, hdr).write())
            entries.append({"driver": c.driver, "plane": c.plane, "angle": c.angle, "file": rel.as_posix(),
                            "measurement_id": c.id, "valid_band_hz": list(band) if band else None,
                            "protection_removed": [f.model_dump() for f in prot]})

        nf_entries = []
        for n in st.nearfield:
            r = self.s.response(n.id)
            rel = Path("frd") / n.driver / f"{st.speaker}_{n.driver} {n.label}.frd"
            FrdFile(out / rel, r.freqs, r.h, [f"Rewdio near-field capture: {st.speaker} {n.driver} ({n.label})",
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
        freqs, _, notes, rows = self._gated_set(st, gate, ppo, 20.0, 20000.0, None)
        fs_dsp = self.s.config.dsp_sample_rate
        xfer = {}
        for d in st.drivers:
            o = self.s.dsp.get(d).model_copy(deep=True)
            o.crossover = list(self.s.config.channel(d).crossover or [])
            xfer[d] = o.transfer(freqs, fs_dsp)
        system: dict[tuple, np.ndarray] = {}
        per: dict[str, dict[tuple, np.ndarray]] = {d: {} for d in st.drivers}
        for c, h, *_ in rows:
            k = (c.plane, c.angle)
            per[c.driver][k] = h
        for k in set().union(*[set(v) for v in per.values()]):
            if all(k in per[d] for d in st.drivers):  # only directions where every driver was measured
                system[k] = sum(per[d][k] * xfer[d] for d in st.drivers)
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
                         "files": [e for e in entries if e["driver"] == d],
                         "nearfield": [n for n in nf if n["driver"] == d],
                         "zma": [z for z in zma if z["driver"] == d],
                         "distortion": [x for x in dist if x["driver"] == d]} for d in st.drivers],
            "mic_cal": cfg.mic_cal_path,
            "dsp_sample_rate": cfg.dsp_sample_rate,
            "notes": notes,
        }
