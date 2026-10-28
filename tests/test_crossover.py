"""Crossover filters written by Rewdio: the maths, the minidsp-rs patch, the shadow state, and protection."""

import numpy as np
import pytest

from rewdio.config import Channel, ChannelKind
from rewdio.minidsp.biquad import cascade_response, crossover_sections
from rewdio.minidsp.client import MiniDSPClient
from rewdio.minidsp.state import XoverFilter, xover_sections

FS = 96000


def _db(h):
    return 20 * np.log10(np.abs(h))


@pytest.mark.parametrize("topo", ["LR4", "LR8"])
def test_even_order_linkwitz_riley_pairs_sum_flat(topo):
    f = np.geomspace(100, 20000, 400)
    s = (cascade_response(crossover_sections("LP", topo, 1200, FS), f, FS)
         + cascade_response(crossover_sections("HP", topo, 1200, FS), f, FS))
    assert np.max(np.abs(_db(s))) < 0.02


def test_lr2_sums_flat_with_the_tweeter_inverted():
    f = np.geomspace(100, 20000, 400)
    s = (cascade_response(crossover_sections("LP", "LR2", 1200, FS), f, FS)
         - cascade_response(crossover_sections("HP", "LR2", 1200, FS), f, FS))
    assert np.max(np.abs(_db(s))) < 0.02


@pytest.mark.parametrize("topo,at_fc", [("BW2", -3.01), ("BW4", -3.01), ("LR4", -6.02), ("LR8", -6.02)])
def test_level_at_the_crossover_frequency(topo, at_fc):
    h = cascade_response(crossover_sections("LP", topo, 1200, FS), np.array([1200.0]), FS)
    assert abs(_db(h)[0] - at_fc) < 0.05


def test_patch_fills_groups_and_bypasses_the_empty_one():
    secs = xover_sections([XoverFilter(kind="HP", topology="LR4", freq_hz=1200),
                           XoverFilter(kind="HP", topology="BW2", freq_hz=600, protect=True)], FS)
    p = MiniDSPClient.output_patch(1, crossover=secs)
    g0, g1 = p["crossover"]
    assert g0["index"] == 0 and not g0["bypass"] and [c["index"] for c in g0["coeff"]] == [0, 1, 2, 3]
    assert g0["coeff"][3] == {"index": 3, "b0": 1.0, "b1": 0.0, "b2": 0.0, "a1": -0.0, "a2": -0.0}  # unity pad
    assert g1["bypass"]
    assert "crossover" not in MiniDSPClient.output_patch(1, gain_db=0.0)  # unmanaged outputs are left alone


def test_too_many_sections_is_refused():
    with pytest.raises(ValueError):
        xover_sections([XoverFilter(kind="HP", topology="LR8", freq_hz=1000)] * 3, FS)


def _two_way(rt):
    hp = XoverFilter(kind="HP", topology="LR4", freq_hz=1200)
    guard = XoverFilter(kind="HP", topology="BW2", freq_hz=700, protect=True)
    rt.session.config.channels = [
        Channel(role="LW", kind=ChannelKind.DRIVER, dsp_output=0, speaker="L",
                crossover=[XoverFilter(kind="LP", topology="LR4", freq_hz=1200)]),
        Channel(role="LH", kind=ChannelKind.DRIVER, dsp_output=1, speaker="L", crossover=[hp, guard]),
        Channel(role="SUB1", kind=ChannelKind.SUB, dsp_output=2),
        Channel(role="SUB2", kind=ChannelKind.SUB, dsp_output=3),
    ]
    rt.session.config.timing_reference_channel = "LW"
    return hp, guard


async def test_rig_writes_and_tracks_the_crossover(sim_runtime):
    rt = sim_runtime()
    hp, guard = _two_way(rt)
    await rt.rig.assert_baseline()
    fs = rt.session.config.dsp_sample_rate
    assert rt.world.dsp[1].xover == xover_sections([hp, guard], fs)
    assert rt.session.dsp.get("LH").crossover == [hp, guard]
    sub_xo = rt.world.dsp[2].xover  # a sub with no `crossover` keeps the DSP's own filters
    assert sub_xo and rt.session.dsp.get("SUB1").crossover is None

    f = np.geomspace(200, 20000, 50)  # the shadow transfer predicts the hardware
    assert np.allclose(rt.session.dsp.get("LH").transfer(f, fs), rt.world.dsp[1].transfer(f, fs))

    await rt.rig.set_crossover("LH", raw=True)  # raw driver capture keeps only the guard
    assert rt.world.dsp[1].xover == xover_sections([guard], fs)
    await rt.rig.set_crossover("LW", raw=True)  # no protection: fully bypassed
    assert rt.world.dsp[0].xover == []
    assert rt.world.dsp[2].xover == sub_xo


async def test_protection_filter_can_never_be_dropped(sim_runtime):
    rt = sim_runtime()
    hp, _ = _two_way(rt)
    await rt.rig.assert_baseline()
    before = list(rt.world.dsp[1].xover)
    with pytest.raises(ValueError, match="protection"):
        await rt.rig.set_crossover("LH", filters=[hp])
    with pytest.raises(ValueError, match="protection"):
        from rewdio.minidsp.state import DspState
        st = DspState.model_validate(rt.session.dsp.model_dump())
        st.get("LH").crossover = []
        await rt.rig.apply(st)
    assert rt.world.dsp[1].xover == before


async def test_unknown_crossover_state_is_written_not_assumed(sim_runtime):
    rt = sim_runtime()
    _, guard = _two_way(rt)
    rt.session.dsp.get("LH").crossover = None  # e.g. a session saved before crossovers were managed
    await rt.rig.solo(["LW"])  # a mute-only write is upgraded to a full write that includes the crossover
    assert rt.session.dsp.get("LH").crossover is not None
    assert rt.world.dsp[1].xover == xover_sections(rt.session.dsp.get("LH").crossover, rt.session.config.dsp_sample_rate)
    assert rt.world.dsp[0].mute is False and rt.world.dsp[1].mute is True
