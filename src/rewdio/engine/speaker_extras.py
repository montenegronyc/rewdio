"""Impedance (ZMA) and harmonic distortion captures: what a PASSIVE crossover design needs on top of the
polar FRDs. Mixed into SpeakerCapture; uses its state, rig and raw-mode helpers."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from pydantic import BaseModel

from ..rew.client import RewError

CAP_CORNER_HZ = 350.0  # SoundSible BUILD_PLAN: -3 dB about an octave below the lowest crossover tried (700 Hz)
E12 = (1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2)
MAX_LISTENING_SPL_DB = 95.0  # Lee never listens above 95 dB SPL at the seat: no reason to drive drivers past it
DEFAULT_DISTORTION_SPL_DB = (85.0, 90.0, 95.0)


def protection_cap(freqs: np.ndarray, ohm: np.ndarray, corner_hz: float = CAP_CORNER_HZ) -> dict:
    """Series protection capacitor for a compression driver while it runs on the DSP: C = 1 / (2 pi f |Z(f)|),
    with |Z| read from the MEASURED impedance at the corner, not the nominal rating. A first-order guard
    against a DSP that boots or loses its preset into a flat path, not part of the crossover. The suggested
    part is the nearest E12 value at or BELOW the exact one: a smaller cap sits higher, which protects more."""
    f = np.asarray(freqs, float)
    z = float(np.exp(np.interp(np.log(corner_hz), np.log(f), np.log(np.asarray(ohm, float)))))
    exact = 1e6 / (2 * np.pi * corner_hz * z)
    decade = 10 ** np.floor(np.log10(exact))
    part = max((m * decade for m in E12 if m * decade <= exact * (1 + 1e-9)), default=decade)
    return {"corner_hz": corner_hz, "ohm_at_corner": round(z, 2), "exact_uf": round(exact, 2),
            "suggested_uf": round(part, 3), "suggested_corner_hz": round(1e6 / (2 * np.pi * z * part), 0),
            "series": "E12, rounded down", "voltage_note": "film capacitor rated 100 V or more"}


class Impedance(BaseModel):
    driver: str
    id: str
    source: str  # "api" (Rewdio triggered it) or "imported" (taken in REW's GUI)
    series_cap_uf: float | None = None
    cap_in_circuit: bool | None = None
    protection_cap: dict | None = None  # suggested DSP-session guard cap, for drivers with a protection filter
    note: str | None = None


class Distortion(BaseModel):
    driver: str
    id: str
    target_spl_db: float
    measured_spl_db: float
    ref_hz: float
    distance_m: float | None
    level_dbfs: float
    band_hz: tuple[float, float]


class PassiveExtras:
    async def impedance(self, driver: str, rew_measurement_id: str | None = None,
                        series_cap_uf: float | None = None, cap_in_circuit: bool = False, ppo: int = 48,
                        cap_corner_hz: float = CAP_CORNER_HZ) -> dict:
        """One driver's mounted impedance on an impedance jig. Rewdio triggers REW's impedance sweep; if this
        REW refuses that over its API, take it in REW's GUI and pass `rew_measurement_id` to import it.
        cap_in_circuit defaults to False: the passive design wants the driver alone."""
        st = self._require()
        if driver not in st.drivers:
            raise ValueError(f"{driver} is not a driver of {st.speaker}; drivers: {st.drivers}")
        rew, source = self.rig.rew, "imported"
        if rew_measurement_id is None:
            source = "api"
            try:
                await self._raw(st.drivers, raw=True)
                await self.rig.solo([driver])
                summary = await rew.sweep(f"{st.speaker}_{driver} impedance ({st.variant})",
                                          notes="rewdio impedance", command="Impedance")
            except RewError as e:
                raise RewError(f"REW did not run an impedance sweep over its API ({e}). Take it in REW's GUI "
                               "(Measure, Impedance, with the jig) and call again with rew_measurement_id") from e
            finally:
                await self._raw(st.drivers, raw=False)
            rew_measurement_id = summary["uuid"]
        spec = await rew.impedance(rew_measurement_id, ppo=ppo)
        ohm = np.asarray(spec.mag_db, float)  # ohms for an impedance measurement
        med = float(np.median(ohm[np.isfinite(ohm)]))
        if not 0.5 <= med <= 1000:
            raise ValueError(f"median impedance {med:.3g} ohm is not a loudspeaker: check the jig and REW's "
                             "impedance calibration")
        phase = spec.phase_deg if spec.phase_deg is not None else np.zeros_like(ohm)
        self.s._write(f"data/{rew_measurement_id}.zma.json",
                      {"freqs": [float(x) for x in spec.freqs], "ohm": [float(x) for x in ohm],
                       "phase_deg": [float(x) for x in phase]})
        guarded = any(f.protect for f in self.s.config.channel(driver).crossover or [])
        cap = protection_cap(spec.freqs, ohm, cap_corner_hz) if guarded else None
        st.impedance = [z for z in st.impedance if z.driver != driver]
        st.impedance.append(Impedance(driver=driver, id=rew_measurement_id, source=source,
                                      series_cap_uf=series_cap_uf if series_cap_uf is not None
                                      else st.geometry.series_cap_uf, cap_in_circuit=cap_in_circuit,
                                      protection_cap=cap))
        self._save(st)
        i = int(np.argmax(ohm))
        rep = {"ok": True, "id": rew_measurement_id, "source": source, "median_ohm": round(med, 2),
               "peak_ohm": round(float(ohm[i]), 2), "peak_hz": round(float(spec.freqs[i]), 1)}
        if cap:
            rep["protection_cap"] = cap
        return rep

    def _driver_band(self, driver: str) -> tuple[float, float]:
        """Default distortion sweep band: a horn (it has a protection HP) 400 Hz..20 kHz, a woofer 200 Hz..6 kHz."""
        prot = [f for f in self.s.config.channel(driver).crossover or [] if f.protect]
        return (max(400.0, max(f.freq_hz for f in prot) * 0.6), 20000.0) if prot else (200.0, 6000.0)

    async def distortion(self, driver: str, spl_db: list[float], ref_hz: float = 1000.0,
                         band_hz: tuple[float, float] | None = None, tolerance_db: float = 1.0,
                         ppo: int = 12) -> dict:
        """Sweep one driver (raw, protection kept) at each target SPL (measured at `ref_hz` at the mic, which
        stays where the set records it) and keep REW's harmonic distortion. The level is set by measuring
        and correcting, up to three sweeps per target."""
        st = self._require()
        if driver not in st.drivers:
            raise ValueError(f"{driver} is not a driver of {st.speaker}; drivers: {st.drivers}")
        cfg = self.s.config
        sw = cfg.sweep
        loud = [x for x in spl_db if x > MAX_LISTENING_SPL_DB]
        if loud:
            raise ValueError(f"targets {loud} dB are above the {MAX_LISTENING_SPL_DB:g} dB SPL listening ceiling")
        over = [x for x in spl_db if x > sw.max_spl_db - 3]
        if over:
            raise ValueError(f"targets {over} dB are within 3 dB of REW's SPL abort limit ({sw.max_spl_db:g} dB, "
                             "sweep.max_spl_db). Raising that limit is Lee's call; it protects the drivers")
        band = band_hz or self._driver_band(driver)
        rew, results = self.rig.rew, []
        try:
            await self._raw(st.drivers, raw=True)
            await self.rig.prepare_rew()
            await rew.configure_sweep(int(band[0]), int(band[1]), sw.length, sw.start_delay_s, sw.rew_sample_rate,
                                      sw.fill_silence_with_dither)
            for target in sorted(spl_db):  # quietest first: the level search never starts loud
                rec = measured = None
                for _ in range(3):
                    rec, resp = await self.rig.measure([driver], "distortion", f"{target:g} dB", speaker=st.speaker,
                                                       driver=driver, target_spl_db=target)
                    m = (resp.freqs >= ref_hz * 2 ** (-1 / 6)) & (resp.freqs <= ref_hz * 2 ** (1 / 6))
                    measured = float(np.median(resp.mag_db[m]))
                    if abs(measured - target) <= tolerance_db:
                        break
                    want = self.rig.level_dbfs + (target - measured)
                    if want > sw.max_level_dbfs and self.rig.level_dbfs >= sw.max_level_dbfs:
                        break  # already at the level ceiling: record what it can do
                    await self.rig.set_level(want)
                headers, rows = await rew.distortion(rec.id, ppo=ppo)
                self.s._write(f"data/{rec.id}.dist.json", {"headers": headers, "rows": rows})
                st.distortion = [d for d in st.distortion if not (d.driver == driver and d.target_spl_db == target)]
                st.distortion.append(Distortion(driver=driver, id=rec.id, target_spl_db=target,
                                                measured_spl_db=round(measured, 2), ref_hz=ref_hz,
                                                distance_m=st.distance_m, level_dbfs=self.rig.level_dbfs,
                                                band_hz=band))
                results.append({"target_spl_db": target, "measured_spl_db": round(measured, 2),
                                "level_dbfs": self.rig.level_dbfs, "id": rec.id,
                                "reached": abs(measured - target) <= tolerance_db})
        finally:
            self.rig._rew_ready = False  # the next sweep re-applies the normal sweep range and level
            await self.rig.set_level(sw.level_dbfs)
            await self._raw(st.drivers, raw=False)
        self._save(st)
        return {"ok": True, "driver": driver, "band_hz": list(band), "levels": results}

    def _export_zma(self, st: SpeakerState, out: Path) -> list[dict]:
        entries = []
        for z in st.impedance:
            d = json.loads((self.s.dir / "data" / f"{z.id}.zma.json").read_text())
            rel = Path("zma") / f"{st.speaker}_{z.driver}.zma"
            cap = (f"series cap {z.series_cap_uf:g} uF {'IN' if z.cap_in_circuit else 'NOT in'} circuit"
                   if z.series_cap_uf is not None else f"series cap {'in' if z.cap_in_circuit else 'not in'} circuit "
                                                       "(value not recorded)")
            lines = [f"* Rewdio impedance: {st.speaker} {z.driver} ({st.variant}), mounted",
                     f"* REW measurement {z.id} ({z.source}); {cap}"]
            if z.protection_cap:
                pc = z.protection_cap
                lines.append(f"* DSP-session protection cap: {pc['suggested_uf']:g} uF ({pc['exact_uf']:g} exact) for "
                             f"{pc['corner_hz']:g} Hz at {pc['ohm_at_corner']:g} ohm measured")
            lines.append("* freq(Hz)\tohm\tphase(deg)")
            lines += [f"{f:.6f}\t{o:.4f}\t{p:.4f}" for f, o, p in zip(d["freqs"], d["ohm"], d["phase_deg"])]
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            (out / rel).write_text("\n".join(lines) + "\n")
            entries.append({"driver": z.driver, "file": rel.as_posix(), "measurement_id": z.id, "source": z.source,
                            "series_cap_uf": z.series_cap_uf, "cap_in_circuit": z.cap_in_circuit,
                            "protection_cap": z.protection_cap})
        return entries

    def _export_distortion(self, st: SpeakerState, out: Path) -> list[dict]:
        entries = []
        for x in st.distortion:
            d = json.loads((self.s.dir / "data" / f"{x.id}.dist.json").read_text())
            cols = [h.split("(")[0].strip().lower() + ("_pct" if "%" in h else "_db" if "dB" in h else "_hz"
                                                       if "Hz" in h else "") for h in d["headers"]]
            rel = Path("distortion") / f"{st.speaker}_{x.driver}_{x.target_spl_db:g}dB.csv"
            lines = [f"# Rewdio harmonic distortion: {st.speaker} {x.driver} ({st.variant})",
                     f"# target {x.target_spl_db:g} dB SPL, measured {x.measured_spl_db:g} dB SPL at {x.ref_hz:g} Hz, "
                     f"mic {x.distance_m:g} m; sweep {x.band_hz[0]:g}..{x.band_hz[1]:g} Hz at {x.level_dbfs:g} dBFS; "
                     f"protection HP kept; series cap {'in' if st.geometry.series_cap_in_circuit else 'not in'} circuit",
                     f"# REW measurement {x.id}; blank = REW gave no value (harmonic above the band)",
                     ",".join(cols)]
            for row in d["rows"]:
                vals = [("" if v is None else f"{float(v):.6g}") for v in row] + [""] * (len(cols) - len(row))
                lines.append(",".join(vals[:len(cols)]))
            (out / rel).parent.mkdir(parents=True, exist_ok=True)
            (out / rel).write_text("\n".join(lines) + "\n")
            entries.append({"driver": x.driver, "file": rel.as_posix(), "measurement_id": x.id,
                            "target_spl_db": x.target_spl_db, "measured_spl_db": x.measured_spl_db,
                            "ref_hz": x.ref_hz, "mic_distance_m": x.distance_m, "band_hz": list(x.band_hz),
                            "columns": cols, "series_cap_in_circuit": st.geometry.series_cap_in_circuit})
        return entries
