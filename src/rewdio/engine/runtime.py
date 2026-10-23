"""Wires a session to real or simulated REW/miniDSP endpoints."""

from __future__ import annotations

import os

from ..minidsp.client import MiniDSPClient
from ..rew.client import RewClient
from .rig import Rig
from .store import Session
from .workflow import Autopilot


class Runtime:
    def __init__(self, session_name: str = "default", root: str | None = None, simulate: bool | None = None,
                 world=None):
        self.session = Session(root, session_name)
        self.simulate = (os.environ.get("REWDIO_SIMULATE", "") not in ("", "0", "false")) if simulate is None else simulate
        self.world = None
        self.fake_rew = self.fake_dsp = None
        self._connect(world)

    def _connect(self, world=None) -> None:
        cfg = self.session.config
        if self.simulate:
            from ..sim.fakes import transports
            from ..sim.world import default_world
            self.world = world or self.world or default_world(cfg.dsp_sample_rate)
            if cfg.room_dims_m is None:
                cfg.room_dims_m = tuple(self.world.room.dims)
            for k, v in self.world.seats.items():
                cfg.seat_xyz.setdefault(k, v)
            rt, dt, self.fake_rew, self.fake_dsp = transports(self.world)
            self.rew = RewClient(cfg.rew_url, transport=rt, poll_interval=0.0, command_timeout=5)
            self.dsp = MiniDSPClient("http://sim", cfg.minidsp_device_index, transport=dt)
        else:
            self.rew = RewClient(cfg.rew_url)
            self.dsp = MiniDSPClient(cfg.minidsp_url, cfg.minidsp_device_index)
        self.rig = Rig(self.session, self.rew, self.dsp)
        self.autopilot = Autopilot(self.rig)

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
            seat = w.seats.get(sim["to_seat"]) or cfg.seat_xyz[sim["to_seat"]]
            spk.pos = tuple(seat)  # driver at ear height, as instructed
            done.append(f"moved {spk.role} to {spk.pos}")
        for role, p in (sim.get("move_subs") or {}).items():
            if p:
                w.speaker(role).pos = tuple(p)
                done.append(f"moved {role} to {tuple(p)}")
        target = action.get("mic_to")
        if target:
            p = xyz or cfg.seat_xyz.get(target) or w.seats.get(target) or action.get("xyz_suggestion")
            if p is None:
                raise ValueError(f"simulator needs coordinates for mic position {target}")
            w.mic = tuple(p)
            done.append(f"moved mic to {target} {tuple(p)}")
        return done
