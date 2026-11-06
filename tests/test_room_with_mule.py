"""The room calibration (sub crawl included) with a speaker under measurement in the same config: it works on
mains and subs only, keeps the mule's drivers muted, and never solves or resets them."""

from conftest import drive
from rewdio.config import Channel, ChannelKind, TimingMode
from rewdio.minidsp.state import XoverFilter
from rewdio.sim.world import DspOutput


async def test_crawl_and_calibration_ignore_driver_channels(sim_runtime):
    rt = sim_runtime()
    cfg = rt.session.config
    cfg.do_sub_crawl = True
    cfg.timing_mode = TimingMode.LOOPBACK
    lw = Channel(role="LW", kind=ChannelKind.DRIVER, dsp_output=0, dsp_device=1, speaker="M",
                 crossover=[XoverFilter(kind="LP", freq_hz=1200)])
    cfg.channels = cfg.channels + [lw]
    rt.world.more_dsp = {1: {0: DspOutput()}}
    rt.session.save()
    rep = await drive(rt, 200)
    assert rep["status"] == "done"
    assert rt.session.workflow.crawl["result"]["best_pairs"]
    assert not any("LW" in m.roles for m in rt.session.measurements)  # never swept by the room calibration
    assert rt.world.more_dsp[1][0].mute  # still muted after the final listening state
