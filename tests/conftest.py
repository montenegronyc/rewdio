import numpy as np
import pytest

from rewdio.config import Channel, ChannelKind, TimingMode
from rewdio.engine.runtime import Runtime
from rewdio.minidsp.state import XoverFilter
from rewdio.sim.world import Speaker


async def drive(rt: Runtime, max_rounds: int = 60) -> dict:
    """Run the autopilot to completion, performing user actions in the simulator."""
    rep = {}
    for _ in range(max_rounds):
        rep = await rt.autopilot.step()
        if rep["status"] == "user_action_required":
            rt.apply_sim_effects(rep["action"], None)
            assert rt.autopilot.confirm(rep["action"]["key"])["ok"]
            continue
        return rep
    raise AssertionError("autopilot did not finish")


@pytest.fixture
def sim_runtime(tmp_path):
    rts = []

    def make(**cfg):
        rt = Runtime(f"t{len(rts)}", root=str(tmp_path), simulate=True)
        for k, v in cfg.items():
            setattr(rt.session.config, k, v)
        rt.session.save()
        rts.append(rt)
        return rt

    yield make


@pytest.fixture
def cfg():
    from pathlib import Path

    from rewdio.config import load_config
    return load_config(user_file=Path("/nonexistent"))


# -- speaker measurement: a 2-way on a turntable in the simulator ---------------------------------

C = 343.0
LW, LH = (0.70, 2.0, 1.00), (0.65, 2.0, 1.25)
MODEL = [["HP", 48.0, 0.8], ["LP", 22000.0, 0.7]]  # same for both, so the ratio isolates the geometry


def _rig(rt):
    guard = XoverFilter(kind="HP", topology="BW2", freq_hz=700, protect=True)
    rt.session.config.channels = [
        Channel(role="LW", kind=ChannelKind.DRIVER, dsp_output=0, speaker="L",
                crossover=[XoverFilter(kind="LP", topology="LR4", freq_hz=1500)]),
        Channel(role="LH", kind=ChannelKind.DRIVER, dsp_output=1, speaker="L",
                crossover=[XoverFilter(kind="HP", topology="LR4", freq_hz=1500), guard]),
    ]
    rt.session.config.timing_reference_channel = "LW"
    rt.session.config.timing_mode = TimingMode.LOOPBACK  # speaker captures need absolute phase
    rt.session.config.mic_sensitivity_dbfs = -30.0  # the simulator's SPL is calibrated
    w = rt.world
    w.speakers = [Speaker("LW", LW, "main", 0, MODEL), Speaker("LH", LH, "main", 1, MODEL)]
    w.room.reflections = []  # free field above the modal region: the test is about phase, not gating
    w.room.diffuse_scale = 0.0
    w.mic = (2.70, 2.0, 1.125)
    return guard


async def _run(rt, points=None):
    """Drive a capture to the end; plans `points` first unless None (a set already planned)."""
    cap = rt.speaker
    if points is not None:
        cap.plan("L", points, distance_m=2.0)
    seen = []
    for _ in range(4 * len(cap.state().points) + 4):
        rep = await cap.step()
        if rep["status"] == "user_action_required":
            seen.append(tuple(rt.world.mic))
            rt.apply_sim_effects(rep["action"], None)
            assert cap.confirm(rep["action"]["key"])["ok"]
            continue
        if rep["status"] == "done":
            return seen
        assert rep["status"] == "measured", rep
    raise AssertionError("capture did not finish")


def _delay(src, mic):
    return float(np.linalg.norm(np.subtract(src, mic))) / C
