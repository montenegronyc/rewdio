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
        self.world = world
        self.fake_rew = self.fake_dsp = None
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
            cfg.tuning.preflight = cfg.tuning.preflight.model_copy(update={"input_level_listen_s": 0.0})
            rt, dt, self.fake_rew, self.fake_dsp = transports(self.world)
            self.rew = RewClient(cfg.rew_url, cfg.rew_client, transport=rt)
            self.dsp = MiniDSPClient("http://sim", cfg.minidsp_device_index, transport=dt)
        else:
            self.rew = RewClient(cfg.rew_url, cfg.rew_client)
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
            spk.pos = tuple(w.seats.get(sim["to_seat"]) or cfg.seat_xyz[sim["to_seat"]])  # driver at ear height
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
