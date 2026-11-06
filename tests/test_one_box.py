"""One 2x4HD for everything: the mule's two drivers on outputs 0-1, two subs on 2-3, no mains channels.
The sub crawl and the two-sub calibration run, with a sub as the timing reference; the drivers are never swept."""

import pytest
from conftest import drive

from rewdio.config import Channel, ChannelKind, SystemConfig, TimingMode
from rewdio.minidsp.state import XoverFilter


def _one_box(cfg):
    subs = [c for c in cfg.channels if c.kind == ChannelKind.SUB]
    guard = XoverFilter(kind="HP", topology="BW2", freq_hz=700, protect=True)
    return [Channel(role="LW", kind=ChannelKind.DRIVER, dsp_output=0, speaker="M",
                    crossover=[XoverFilter(kind="LP", freq_hz=1500)]),
            Channel(role="LH", kind=ChannelKind.DRIVER, dsp_output=1, speaker="M",
                    crossover=[XoverFilter(kind="HP", freq_hz=1500), guard])] + subs


async def test_sub_crawl_on_one_box_with_the_mule(sim_runtime):
    rt = sim_runtime()
    cfg = rt.session.config
    cfg.do_sub_crawl = True
    cfg.timing_mode = TimingMode.LOOPBACK
    cfg.channels = _one_box(cfg)
    cfg.timing_reference_channel = "SUB1"
    rt.session.save()
    rep = await drive(rt, 200)
    assert rep["status"] == "done", rep.get("error")
    assert rt.session.workflow.crawl["result"]["best_pairs"]
    assert not any({"LW", "LH"} & set(m.roles) for m in rt.session.measurements)


def test_the_timing_reference_must_be_a_room_channel(cfg):
    data = cfg.model_dump()
    data["channels"] = [c.model_dump() for c in _one_box(cfg)]
    for ref in ("L", "LW"):
        data["timing_reference_channel"] = ref
        with pytest.raises(ValueError, match="SUB1"):
            SystemConfig.model_validate(data)
    data["timing_reference_channel"] = "SUB1"
    SystemConfig.model_validate(data)
