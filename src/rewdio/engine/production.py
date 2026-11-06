"""Production captures for retesting each hand-built passive tower: network transfer and the soak.

Network transfer: the response of the crossover network to one driver, as a voltage ratio. Two sweeps on the
same loopback time base and the SAME input and divider: first with the measurement input across the amplifier
output ("amp"), then across the driver's terminals ("tap"). Their ratio is the network; the divider, the input's
own response and REW's mic calibration all cancel in it. A woofer inside a lattice is not ground-referenced: use a
differential probe or an isolating transformer for both clips, never two divided inputs subtracted, and never tie
the interface ground to an amplifier output (with a bridged/BTL amplifier that shorts it).

Soak: pink noise at a stated SPL at the seat (never above 95 dB) for a set time, then a fresh impedance capture
(label 'post_soak') to compare with the one before. The level comes from a distortion capture of the same
channel, which measured what dBFS gives what SPL there; pink noise plays 3 dB below that sweep level, on the safe
side, so the stated SPL is a ceiling, not a promise.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import numpy as np
from pydantic import BaseModel

from ..analysis.frd import FrdFile
from .speaker_extras import MAX_LISTENING_SPL_DB

PINK_BELOW_SWEEP_DB = 3.0


class Transfer(BaseModel):
    label: str
    driver: str  # the channel that played (the tower's amp channel)
    amp_id: str
    tap_id: str
    note: str | None = None


class Soak(BaseModel):
    driver: str
    minutes: float
    spl_db: float
    level_dbfs: float
    started: float
    ended: float | None = None
    completed: bool = False


class ProductionExtras:
    """Mixed into SpeakerCapture; uses its state, rig and save helpers."""

    async def transfer(self, stage: str, label: str, driver: str, note: str | None = None) -> dict:
        st = self._require_driver(driver)
        if stage not in ("amp", "tap"):
            raise ValueError("stage is 'amp' (input across the amplifier output) or 'tap' (across the driver)")
        rec, _ = await self.rig.measure([driver], f"transfer_{stage}", label, speaker=st.speaker, label=label)
        if stage == "amp":
            st.pending_transfer = {"label": label, "driver": driver, "amp_id": rec.id}
            self._save(st)
            return {"ok": True, "next": f"move the SAME divider/probe across {label}'s terminals, then call "
                                        f"speaker_transfer(stage='tap', label='{label}', driver='{driver}')"}
        pend = st.pending_transfer
        if not pend or pend["label"] != label or pend["driver"] != driver:
            raise ValueError(f"take the 'amp' sweep for {label} first")
        amp, tap = self.s.response(pend["amp_id"]), self.s.response(rec.id)
        h = tap.h / amp.h
        self.s._write(f"data/{rec.id}.xfer.json", {"freqs": [float(f) for f in amp.freqs],
                                                   "re": [float(x) for x in h.real], "im": [float(x) for x in h.imag]})
        st.transfers = [t for t in st.transfers if t.label != label]
        st.transfers.append(Transfer(label=label, driver=driver, amp_id=pend["amp_id"], tap_id=rec.id, note=note))
        st.pending_transfer = None
        self._save(st)
        m = (amp.freqs >= 200) & (amp.freqs <= 5000)
        db = 20 * np.log10(np.maximum(np.abs(h[m]), 1e-12))
        return {"ok": True, "label": label, "passband_db": round(float(np.max(db)), 2),
                "note": "the ratio of two sweeps through the same input: divider and mic calibration cancel"}

    def soak_level(self, driver: str, spl_db: float) -> float:
        st = self._require()
        if spl_db > MAX_LISTENING_SPL_DB:
            raise ValueError(f"{spl_db:g} dB is above the {MAX_LISTENING_SPL_DB:g} dB SPL listening ceiling")
        known = sorted((d for d in st.distortion if d.driver == driver), key=lambda d: d.measured_spl_db)
        if not known:
            raise ValueError(f"run speaker_distortion on {driver} first: it measures what level gives what SPL")
        ref = min(known, key=lambda d: abs(d.measured_spl_db - spl_db))
        level = ref.level_dbfs + (spl_db - ref.measured_spl_db) - PINK_BELOW_SWEEP_DB
        return round(min(level, self.s.config.sweep.max_level_dbfs), 1)

    async def soak(self, driver: str, minutes: float, spl_db: float) -> dict:
        """Play pink noise through `driver` (protection kept) for `minutes`. Long: run it in the background."""
        st = self._require()
        if not 0 < minutes <= 240:
            raise ValueError("minutes must be between 0 and 240")
        level = self.soak_level(driver, spl_db)
        rec = Soak(driver=driver, minutes=minutes, spl_db=spl_db, level_dbfs=level, started=time.time())
        st.soaks.append(rec)
        self._save(st)
        rew = self.rig.rew
        try:
            async with self._raw_mode(st):  # protection filters only, as for every capture
                try:
                    await self.rig.route([driver])  # REW output channel and solo, as for a sweep
                    await rew.post("/generator/signal", {"signal": "pinknoise"})
                    await rew.post("/generator/level", {"value": level, "unit": "dBFS"})
                    await rew.post("/generator/command", {"command": "Play"})
                    await asyncio.sleep(minutes * 60)
                    rec.completed = True
                finally:  # the noise stops before the crossover goes back
                    await rew.post("/generator/command", {"command": "Stop"})
        finally:
            rec.ended = time.time()
            self._save(st)
        return {"ok": True, "driver": driver, "minutes": minutes, "level_dbfs": level,
                "next": f"wire the impedance jig and call speaker_impedance(driver='{driver}', label='post_soak')"}

    def _export_production(self, st, out: Path) -> dict:
        xfers = []
        for t in st.transfers:
            d = json.loads((self.s.dir / "data" / f"{t.tap_id}.xfer.json").read_text())
            h = np.asarray(d["re"]) + 1j * np.asarray(d["im"])
            rel = Path("transfer") / f"{st.speaker}_{t.label}.frd"
            FrdFile(out / rel, np.asarray(d["freqs"]), h,
                    [f"Rewdio network transfer: {st.speaker} {t.label} (tap / amplifier output)",
                     f"REW measurements {t.amp_id} (amp) and {t.tap_id} (tap); {t.note or ''}",
                     "freq(Hz)\tdB\tphase(deg)"]).write()
            xfers.append({"label": t.label, "driver": t.driver, "file": rel.as_posix(), "note": t.note})
        return {"serial": st.serial, "reference": st.reference, "parts": st.parts, "transfer": xfers,
                "soak": [s.model_dump() for s in st.soaks]}
