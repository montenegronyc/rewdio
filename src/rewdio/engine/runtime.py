"""Wires a session to real or simulated REW/miniDSP endpoints."""

from __future__ import annotations

import asyncio
import os

from ..minidsp.client import MiniDSPClient
from ..rew.client import RewClient
from .rig import Rig
from .speaker import SpeakerCapture
from .store import Session
from .workflow import Autopilot


class Runtime:
    def __init__(self, session_name: str = "default", root: str | None = None, simulate: bool | None = None,
                 world=None):
        self.session = Session(root, session_name)
        self.simulate = (os.environ.get("REWDIO_SIMULATE", "") not in ("", "0", "false")) if simulate is None else simulate
        self.world = world
        self.fake_rew = self.fake_dsp = None
        self.lock = asyncio.Lock()  # one driver at a time: Claude's tool calls and the phone share it
        self._task: asyncio.Task | None = None
        self.last_report: dict | None = None
        self._connect()

    def _connect(self) -> None:
        cfg = self.session.config
        if self.simulate:
            from ..sim.fakes import transports
            from ..sim.world import load_world
            self.world = self.world or load_world(cfg.sim_scenario)
            if cfg.room_dims_m is None:
                cfg.room_dims_m = tuple(self.world.room.dims)
            for k, v in self.world.seats.items():
                cfg.seat_xyz.setdefault(k, v)
            # the simulator answers instantly: no settle, polling or listening time needed
            cfg.rew_client = cfg.rew_client.model_copy(update={"settle_s": 0.0, "poll_interval_s": 0.0})
            cfg.speaker_prompts = cfg.speaker_prompts.model_copy(update={"settle_s": 0.0})
            cfg.tuning.preflight = cfg.tuning.preflight.model_copy(update={"input_level_listen_s": 0.0})
            rt, dt, self.fake_rew, self.fake_dsp = transports(self.world)
            self.rew = RewClient(cfg.rew_url, cfg.rew_client, transport=rt)
            self.dsp = MiniDSPClient("http://sim", cfg.minidsp_device_index, transport=dt)
        else:
            self.rew = RewClient(cfg.rew_url, cfg.rew_client)
            self.dsp = MiniDSPClient(cfg.minidsp_url, cfg.minidsp_device_index)
        self.rig = Rig(self.session, self.rew, self.dsp)
        self.autopilot = Autopilot(self.rig)
        self.speaker = SpeakerCapture(self.rig)
        from .voice import Voice
        self.speaker.voice = Voice(cfg.speaker_prompts, self.rew)

    @property
    def busy(self) -> bool:
        return self.lock.locked()

    async def step(self, max_sweeps: int | None = None) -> dict:
        async with self.lock:
            rep = await self.autopilot.step(max_sweeps=max_sweeps)
        self.last_report = rep
        self.session.save()
        return rep

    def confirm(self, key: str | None, xyz: list[float] | None, note: str | None = None) -> dict:
        pending = self.session.workflow.pending_action
        if pending is None:
            st = self.speaker.state()
            if st is not None and not st.done and st.rotated_to != st.index:
                return {"ok": False, "message": "the room calibration is not waiting; a speaker set is: use "
                                                "speaker_confirm for the turntable"}
            return {"ok": False, "message": "nothing is waiting on the user"}
        sim = self.apply_sim_effects(pending, xyz)
        res = self.autopilot.confirm(key, xyz, note)
        if sim:
            res["simulated_effects"] = sim
        return res

    def _sim_rotate(self, polar: dict) -> str:
        """Turning the speaker by an angle about its pivot = moving the mic the opposite way around it."""
        import numpy as np
        w = self.world
        roles = self.speaker.drivers_of(polar["speaker"])
        pivot = np.mean([w.speaker(r).pos for r in roles], axis=0)
        key = f"_polar_axis_{polar['speaker']}"
        if not hasattr(self, key):  # the mic position at 0 degrees defines the axis
            setattr(self, key, np.subtract(w.mic, pivot))
        v = getattr(self, key)
        # the mic moves round the speaker the opposite way to the turntable: toward the speaker's left (+y) for a
        # positive horizontal angle, upward (+z) for a positive vertical one
        a = np.deg2rad(polar["angle"])
        k = np.array([0.0, 0.0, 1.0]) if polar["plane"] == "hor" else np.cross(v, [0.0, 0.0, 1.0])
        k = k / np.linalg.norm(k)
        r = v * np.cos(a) + np.cross(k, v) * np.sin(a) + k * np.dot(k, v) * (1 - np.cos(a))  # Rodrigues
        w.mic = tuple(pivot + r)
        return f"rotated {polar['speaker']} to {polar['plane']} {polar['angle']:g} (mic at {tuple(np.round(w.mic, 3))})"

    def speaker_confirm(self, key: str | None = None) -> dict:
        """The person turned the speaker as asked (in the simulator: turn it)."""
        st = self.speaker.state()
        sim = []
        if st is not None and not st.done and st.rotated_to != st.index:
            sim = self.apply_sim_effects(self.speaker._ask(st)["action"], None)
        res = self.speaker.confirm(key)
        if sim:
            res["simulated_effects"] = sim
        return res

    async def speaker_step(self, level_dbfs: float | None = None, skip: bool = False) -> dict:
        async with self.lock:
            rep = await self.speaker.step(level_dbfs, skip)
        self.session.save()  # bumps the version: the phone redraws
        return rep

    def speaker_ready(self, key: str | None = None) -> dict:
        """Ready from the phone: confirm the turn, then sweep in the background (settle first)."""
        if self.busy or (self._task is not None and not self._task.done()):
            return {"ok": False, "message": "still measuring"}
        res = self.speaker_confirm(key)
        if res.get("ok"):
            self._task = asyncio.create_task(self.speaker_step())
            self._task.add_done_callback(self._speaker_done)
            res["running"] = True
        return res

    def _speaker_done(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            e = task.exception()
            self.speaker._result("error", "Sweep failed. Check the computer.", None)
            self.speaker.last["error"] = f"{type(e).__name__}: {e}"
            self.session.save()

    def start_soak(self, driver: str, minutes: float, spl_db: float) -> dict:
        """Run a soak in the background (it can take an hour); the result lands in the capture set."""
        if self.busy or (self._task is not None and not self._task.done()):
            return {"ok": False, "message": "the rig is busy"}
        level = self.speaker.soak_level(driver, spl_db)  # refuse bad requests now, not an hour later

        async def go():
            async with self.lock:
                return await self.speaker.soak(driver, minutes, spl_db)
        self._task = asyncio.create_task(go())
        self._task.add_done_callback(self._speaker_done)
        return {"ok": True, "running": True, "driver": driver, "minutes": minutes, "level_dbfs": level,
                "next": "when it finishes, wire the impedance jig and call speaker_impedance(label='post_soak')"}

    def advance_in_background(self) -> bool:
        """Kick the autopilot from the phone. False if a run is already going."""
        if self.busy or (self._task is not None and not self._task.done()):
            return False
        self._task = asyncio.create_task(self.step())
        self._task.add_done_callback(self._task_done)
        return True

    def _task_done(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:  # step() reports errors itself; this is a backstop
            e = task.exception()
            self.last_report = {"status": "error", "error": f"{type(e).__name__}: {e}"}
            self.session.save()

    def set_cell(self, code: str, enabled: bool) -> dict:
        crawl = self.session.workflow.crawl
        if code in crawl.get("measured", {}):
            return {"ok": False, "message": f"{code} is already measured"}
        ex = set(crawl.get("excluded", []))
        ex.discard(code) if enabled else ex.add(code)
        crawl["excluded"] = sorted(ex)
        pa = self.session.workflow.pending_action or {}
        if not enabled and pa.get("cell") == code:  # skipping the cell we are waiting on
            self.session.workflow.pending_action = None
        self.session.save()
        return {"ok": True, "excluded": crawl["excluded"]}

    async def reconnect(self) -> None:
        await self.aclose()
        self._connect()

    async def aclose(self) -> None:
        await self.rew.aclose()
        await self.dsp.aclose()

    def apply_sim_effects(self, action: dict, xyz: list[float] | None) -> list[str]:
        """In simulation, perform the physical task the user was asked to do."""
        if not self.simulate or self.world is None:
            return []
        w, cfg, done = self.world, self.session.config, []
        sim = action.get("sim") or {}
        if "move_speaker" in sim:
            spk = w.speaker(sim["move_speaker"])
            spk.pos = tuple(w.seats.get(sim["to_seat"]) or cfg.seat_xyz[sim["to_seat"]])  # driver at ear height
            done.append(f"moved {spk.role} to {spk.pos}")
        for role, p in (sim.get("move_subs") or {}).items():
            if p:
                w.speaker(role).pos = tuple(p)
                done.append(f"moved {role} to {tuple(p)}")
        if "polar" in sim:
            done.append(self._sim_rotate(sim["polar"]))
        target = action.get("mic_to")
        if target:
            p = xyz or cfg.seat_xyz.get(target) or w.seats.get(target) or action.get("xyz_suggestion")
            if p is None:
                raise ValueError(f"simulator needs coordinates for mic position {target}")
            w.mic = tuple(p)
            done.append(f"moved mic to {target} {tuple(p)}")
        return done
