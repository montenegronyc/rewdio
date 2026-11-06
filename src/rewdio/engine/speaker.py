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

import asyncio
import time
from contextlib import asynccontextmanager

import numpy as np
from pydantic import BaseModel, Field

from ..analysis.quality import critical
from ..config import ChannelKind, TimingMode
from .production import ProductionExtras, Soak, Transfer
from .rig import Rig
from .speaker_export import MANIFEST_SCHEMA, ExportMixin  # noqa: F401 (re-exported)
from .speaker_extras import Distortion, Impedance, PassiveExtras
from .speaker_meta import Environment, Geometry
from .store import Session
from .turntable import Point, angles_for, points_from, prompt  # noqa: F401 (re-exported)

MODES = ("raw", "xo", "sum")  # driver alone / driver through its output as configured / all drivers together


class Capture(BaseModel):
    driver: str  # "sum" for a summed capture
    plane: str
    angle: float
    id: str
    mode: str = "raw"


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
    modes: list[str] = Field(default_factory=lambda: ["raw"])
    geometry: Geometry = Field(default_factory=Geometry)
    environment: Environment = Field(default_factory=Environment)
    impedance: list[Impedance] = Field(default_factory=list)
    distortion: list[Distortion] = Field(default_factory=list)
    exported_to: str | None = None
    serial: str | None = None
    reference: bool = False  # this set is the reference every later tower is held to
    parts: dict = Field(default_factory=dict)  # measured part values, e.g. {"L1_mH": 1.52}
    transfers: list[Transfer] = Field(default_factory=list)
    pending_transfer: dict | None = None
    soaks: list[Soak] = Field(default_factory=list)
    skipped: list[Point] = Field(default_factory=list)
    index: int = 0
    rotated_to: int | None = None  # index of the point the user last confirmed rotating to
    captures: list[Capture] = Field(default_factory=list)
    nearfield: list[Nearfield] = Field(default_factory=list)
    created: float = Field(default_factory=time.time)

    @property
    def done(self) -> bool:
        return self.index >= len(self.points)


class SpeakerCapture(PassiveExtras, ProductionExtras, ExportMixin):
    FILE = "speaker.json"

    def __init__(self, rig: Rig):
        self.rig = rig
        self.voice = None  # engine.voice.Voice, set by the runtime
        self.last: dict | None = None  # what just happened, for the phone card: status, speech, point
        self._announced: int | None = None

    async def _announce(self, text: str) -> None:
        """Speak only while the rig is idle (never during a sweep): before the settle, or after the sweeps."""
        if self.voice is not None:
            await self.voice.say(text)

    def _result(self, status: str, speech: str, point=None) -> None:
        self.last = {"status": status, "speech": speech, "point": point, "at": time.time()}

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

    def _require_driver(self, driver: str) -> SpeakerState:
        st = self._require()
        if driver not in st.drivers:
            raise ValueError(f"{driver} is not a channel of {st.speaker}; its channels are {st.drivers}")
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
             replace: bool = False, modes: list[str] | None = None) -> SpeakerState:
        """Start a capture set. Each physical variant (horn offset, riser height...) is its own set."""
        tm = self.s.config.timing_mode
        if tm not in (TimingMode.LOOPBACK, TimingMode.ACOUSTIC):
            raise ValueError(f"timing_mode is {tm.value!r}: REW then puts every impulse peak at t=0, which erases the "
                             "timing between drivers and angles that the FRDs exist to carry. Wire the loopback "
                             "(interface output to a second input), set timing_mode to 'loopback', and run rig_selftest")
        old = self.state()
        held = old and (len(old.captures) + len(old.impedance) + len(old.distortion) + len(old.nearfield)
                        + len(old.transfers))
        if held and not old.exported_to and not replace:
            raise ValueError(f"the current set ({old.speaker}, variant {old.variant!r}) holds {held} captures that "
                             "were never exported: export_frd first, or pass replace=True. (To change only its "
                             "metadata, use speaker_update.)")
        geometry = geometry or Geometry()
        distance_m = distance_m if distance_m is not None else geometry.mic_distance()
        if distance_m is None:
            raise ValueError("give distance_m, or pivot_xyz and the mic position")
        modes = list(dict.fromkeys(modes or ["raw"]))
        bad = [m for m in modes if m not in MODES]
        if bad or not modes:
            raise ValueError(f"modes {bad} unknown; use any of {list(MODES)}")
        st = SpeakerState(speaker=speaker, drivers=self.drivers_of(speaker), distance_m=distance_m, points=points,
                          variant=variant, geometry=geometry, environment=environment or Environment(), modes=modes)
        self._save(st)
        return st

    def update(self, geometry: dict | None = None, environment: dict | None = None, serial: str | None = None,
               reference: bool | None = None, parts: dict | None = None) -> SpeakerState:
        """Change a set's metadata without touching its captures (fields given are merged in)."""
        st = self._require()
        if geometry:
            st.geometry = Geometry.model_validate({**st.geometry.model_dump(), **geometry})
        if environment:
            st.environment = Environment.model_validate({**st.environment.model_dump(), **environment})
        if serial is not None:
            st.serial = serial
        if reference is not None:
            st.reference = reference
        if parts:
            st.parts = {**st.parts, **parts}
        self._save(st)
        return st

    # -- capture ----------------------------------------------------------------------

    def _ask(self, st: SpeakerState) -> dict:
        p = st.points[st.index]
        pr = prompt(st.points, st.index, st.speaker, st.distance_m, st.geometry.mic_height_m)
        return {"status": "user_action_required",
                "action": {"key": f"rotate:{st.index}", "instructions": pr["text"], "speech": pr["speech"],
                           "plane": pr["plane"], "target": pr["target"], "move_deg": pr["move_deg"],
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

    @asynccontextmanager
    async def _raw_mode(self, st: SpeakerState, solo: list[str] | None = None):
        """Every driver raw (protection filters only) for the block, optionally soloed. Afterwards, whatever failed:
        the configured crossovers go back FIRST (one write), then every output is unmuted, then the sweep level is
        restored. The speaker is never left raw while waiting on the user."""
        prev = self.rig.level_dbfs
        try:
            await self.rig.set_crossovers(st.drivers, raw=True)
            if solo:
                await self.rig.solo(solo)
            yield
        finally:
            try:
                await self.rig.set_crossovers(st.drivers, raw=False)
                await self.rig.unmute_all()
            finally:
                self.rig.level_dbfs = prev

    def _critical(self, ids: list[str]) -> list[dict]:
        """The critical quality findings of these sweeps, each tagged with the channel that played."""
        return [f | {"driver": m.roles[0]} for m in self.s.measurements if m.id in ids
                for f in critical(m.extra.get("quality", []))]

    async def _sweep(self, roles: str | list[str], kind: str, position: str, **extra) -> str:
        rec, _ = await self.rig.measure([roles] if isinstance(roles, str) else roles, kind, position, **extra)
        ir, fs, t0 = await self.rig.rew.impulse_response_timed(rec.id)
        np.savez(self.s.dir / "data" / f"{rec.id}.raw.npz", data=np.asarray(ir, np.float32), fs=fs, t0=t0)
        return rec.id

    async def step(self, level_dbfs: float | None = None, skip: bool = False) -> dict:
        """level_dbfs: change the sweep level first (clamped; e.g. after a 'clipped' retry). skip: give up on the
        current angle (recorded as skipped) and move on."""
        st = self._require()
        if st.done:
            return {"status": "done", "captures": len(st.captures), "skipped": len(st.skipped), "next": "export_frd"}
        if level_dbfs is not None:
            await self.rig.set_level(level_dbfs)
        if skip:
            st.skipped.append(st.points[st.index])
            st.index += 1
            self._save(st)
            if st.done:
                return {"status": "done", "captures": len(st.captures), "skipped": len(st.skipped),
                        "next": "export_frd"}
        if st.rotated_to != st.index:
            ask = self._ask(st)
            if self._announced != st.index:  # say each prompt once, however often step is called
                self._announced = st.index
                await self._announce(ask["action"]["speech"])
            return ask
        p = st.points[st.index]
        took = []
        pos = f"{p.plane} {p.angle:g}"
        await self._announce("Hold still.")
        await asyncio.sleep(self.s.config.speaker_prompts.settle_s)  # footsteps and the voice die away first

        async def keep(roles: list[str], driver: str, mode: str) -> None:
            mid = await self._sweep(roles, "driver" if mode == "raw" else f"driver_{mode}", pos, speaker=st.speaker,
                                    driver=driver, plane=p.plane, angle=p.angle, mode=mode)
            st.captures = [c for c in st.captures if not (c.driver == driver and c.plane == p.plane
                                                          and c.angle == p.angle and c.mode == mode)]
            st.captures.append(Capture(driver=driver, plane=p.plane, angle=p.angle, id=mid, mode=mode))
            took.append(mid)
        try:
            if "raw" in st.modes:
                async with self._raw_mode(st):
                    for d in st.drivers:
                        await keep([d], d, "raw")
            if "xo" in st.modes:  # through each output exactly as configured
                for d in st.drivers:
                    await keep([d], d, "xo")
            if "sum" in st.modes:
                await keep(list(st.drivers), "sum", "sum")
        finally:
            await self.rig.unmute_all()
        bad = self._critical(took)
        if bad:  # same angle again once fixed: a clipped or noisy sweep would poison the whole set
            self._save(st)
            why = {"clipped": "Clipped", "low_snr": "Too quiet", "silent_channel": "No sound"}.get(bad[0].get("code"),
                                                                                                    "Bad sweep")
            speech = f"{why} at {p.angle:+g}. Again."
            self._result("retry", speech, p.model_dump())
            await self._announce(speech)
            return {"status": "retry", "point": p.model_dump(), "quality": bad, "speech": speech,
                    "message": "fix the problem (level, noise, cabling), then call speaker_step again"}
        st.index += 1
        self._save(st)
        rep = {"status": "measured", "point": p.model_dump(), "measurements": took,
               "progress": f"{st.index}/{len(st.points)} angles"}
        if not st.done:
            rep["next"] = self._ask(st)
            self._announced = st.index
            speech = "Got it. " + rep["next"]["action"]["speech"]
        else:
            speech = "Got it. Set complete."
        self._result("measured" if not st.done else "done", speech, p.model_dump())
        await self._announce(speech)
        return rep

    async def nearfield(self, driver: str, label: str = "nf") -> dict:
        st = self._require_driver(driver)
        if 0 < st.index < len(st.points):
            raise ValueError("a polar set is part-way: near-field means moving the mic, so finish the set first "
                             f"({st.index} of {len(st.points)} angles done)")
        async with self._raw_mode(st):
            # millimetres from the cone it is ~30 dB louder than at the far-field distance
            await self.rig.set_level(self.rig.level_dbfs - 12.0)
            mid = await self._sweep(driver, "nearfield", label, speaker=st.speaker, driver=driver)
        bad = self._critical([mid])
        if bad:
            return {"status": "retry", "quality": bad, "message": "fix it and call speaker_nearfield again"}
        st.nearfield = [n for n in st.nearfield if not (n.driver == driver and n.label == label)]
        st.nearfield.append(Nearfield(driver=driver, label=label, id=mid))
        self._save(st)
        return {"ok": True, "id": mid,
                "next": "put the mic back exactly where the far-field set had it before any more far-field sweeps"}
