"""Two miniDSP units in one session (towers on one, subs on the other)."""

import pytest
from pydantic import ValidationError

from rewdio.config import Channel, ChannelKind, TimingMode, load_config
from rewdio.engine import selftest
from rewdio.minidsp.state import XoverFilter
from rewdio.sim.world import DspOutput, Speaker


def _two_units(rt):
    cfg = rt.session.config
    cfg.channels = [
        Channel(role="LW", kind=ChannelKind.DRIVER, dsp_output=0, speaker="L",
                crossover=[XoverFilter(kind="LP", freq_hz=1500)]),
        Channel(role="LH", kind=ChannelKind.DRIVER, dsp_output=1, speaker="L",
                crossover=[XoverFilter(kind="HP", freq_hz=1500), XoverFilter(kind="HP", topology="BW2", freq_hz=700,
                                                                              protect=True)]),
        Channel(role="SUB1", kind=ChannelKind.SUB, dsp_output=0, dsp_device=1),  # same output number, other unit
    ]
    cfg.timing_reference_channel = "LW"
    cfg.timing_mode = TimingMode.LOOPBACK
    w = rt.world
    w.more_dsp = {1: {0: DspOutput(routing=(0.5, 0.5)), 1: DspOutput(routing=(0.5, 0.5))}}
    model = [["HP", 48.0, 0.8], ["LP", 22000.0, 0.7]]
    w.speakers = [Speaker("LW", (0.7, 2.0, 1.0), "main", 0, model), Speaker("LH", (0.7, 2.0, 1.25), "main", 1, model),
                  Speaker("SUB1", (0.35, 0.4, 0.25), "sub", 0, [["HP", 19.0, 0.7], ["LP", 180.0, 0.6]],
                          dsp_device=1)]


async def test_writes_and_mutes_reach_the_right_unit(sim_runtime):
    rt = sim_runtime()
    _two_units(rt)
    await rt.rig.assert_baseline()
    await rt.rig.set_channel("SUB1", gain_db=-4.0)
    assert rt.world.more_dsp[1][0].gain_db == -4.0 and rt.world.dsp[0].gain_db == 0.0
    await rt.rig.solo(["SUB1"])
    assert rt.world.dsp[0].mute and rt.world.dsp[1].mute and not rt.world.more_dsp[1][0].mute
    rec, resp = await rt.rig.measure(["SUB1"], "manual", "MLP")
    assert resp.mag_db.max() > 40  # the sub on unit 1 really plays
    await rt.rig.solo(["LW"])
    assert rt.world.more_dsp[1][0].mute and not rt.world.dsp[0].mute


def test_output_numbers_may_repeat_across_units_but_not_within_one(tmp_path):
    cfg = load_config(user_file=tmp_path / "none.json").model_dump()
    cfg["channels"] = [{"role": "L", "kind": "main", "dsp_output": 0},
                       {"role": "SUB1", "kind": "sub", "dsp_output": 0, "dsp_device": 1}]
    cfg["timing_reference_channel"] = "L"
    cfg["balance"]["enabled"] = False
    from rewdio.config import SystemConfig
    SystemConfig.model_validate(cfg)
    cfg["channels"][1]["dsp_device"] = 0
    with pytest.raises(ValidationError, match="own miniDSP output"):
        SystemConfig.model_validate(cfg)


async def test_selftest_checks_every_unit(sim_runtime):
    rt = sim_runtime()
    _two_units(rt)
    await rt.rig.assert_baseline()
    rep = await selftest.run(rt.rig, "LW")
    units = [c for c in rep["checks"] if c["check"] == "minidsp_device"]
    assert [c["device"] for c in units] == [0, 1] and all(c["result"] == "pass" for c in units)
    rt.world.more_dsp = {}
    rep = await selftest.run(rt.rig, "LW")
    assert not rep["ok"] and "device 1: missing" in rep["checks"][1]["detail"]
