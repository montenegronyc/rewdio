"""REW's acoustic timing reference: the chirp's own path through the miniDSP, and every sweep proven timed by it."""

from pathlib import Path

import pytest
from conftest import drive

from rewdio.config import TimingMode, load_config
from rewdio.engine import acoustic
from rewdio.engine.acoustic import TimingReferenceError

ACOUSTIC = {"timing_mode": TimingMode.ACOUSTIC}


def _listening(rt) -> dict[int, list[bool]]:
    """The routing switches of every simulated output, as the scenario (the user's preset) had them."""
    return {i: [w != 0 for w in o.routing] for i, o in rt.world.dsp.items()}


def _unplug_reference(rt) -> None:
    rt.world.speaker(rt.session.config.timing_reference_channel).sensitivity_db = -200.0


def test_a_sub_as_timing_reference_is_refused_by_the_config():
    with pytest.raises(ValueError, match="never a sub"):
        load_config({"timing_mode": "acoustic", "timing_reference_channel": "SUB1"}, user_file=Path("/nonexistent"))


def test_acoustic_config_needs_the_listening_routing_of_every_channel():
    with pytest.raises(ValueError, match="listening_routing"):
        load_config({"timing_mode": "acoustic",
                     "channels": [{"role": "L", "kind": "main", "dsp_output": 0},
                                  {"role": "R", "kind": "main", "dsp_output": 1},
                                  {"role": "SUBA", "kind": "sub", "dsp_output": 2}]},
                    user_file=Path("/nonexistent"))


async def test_a_sub_as_reference_is_refused_at_run_time_too(sim_runtime):
    rt = sim_runtime(timing_reference_channel="SUB1", **ACOUSTIC)  # assigned in a session: never re-validated
    with pytest.raises(TimingReferenceError, match="never a sub"):
        await rt.rig.sweep_once(["L"], "solo", "MLP")
    assert rt.fake_rew.sweeps_run == 0


async def test_a_muted_reference_main_refuses_the_sweep(sim_runtime, monkeypatch):
    rt = sim_runtime(**ACOUSTIC)
    rig = rt.rig
    await rig.prepare_rew()
    await acoustic.route(rig, ["SUB1"])

    async def solo_by_mute(roles):  # the old way: soloing SUB1 mutes the reference main
        await rig.solo(roles)
    monkeypatch.setattr(rig, "route", solo_by_mute)
    with pytest.raises(TimingReferenceError, match="muted"):
        await rig.sweep_once(["SUB1"], "solo", "MLP")
    assert rt.fake_rew.sweeps_run == 0


async def test_every_acoustic_sweep_keeps_the_reference_live_without_playing_it_the_sweep(sim_runtime):
    rt = sim_runtime(**ACOUSTIC)
    ref = rt.session.config.timing_reference_channel  # L, on output 0
    await rt.rig.prepare_rew()
    await rt.rig.route(["SUB1"])
    out = rt.world.dsp
    assert not out[0].mute and out[0].enabled == [False, True]  # L: chirp input only
    assert not out[2].mute and out[2].enabled == [True, False]  # SUB1: sweep input only, never the chirp
    assert out[1].mute and out[3].mute
    assert rt.fake_rew.output_channel == "L" and rt.fake_rew.ref_output_channel == "R"
    await rt.rig.route([ref])
    assert out[0].enabled == [True, True]  # measured itself: the sweep and the chirp


async def test_routing_changes_only_while_every_output_is_muted(sim_runtime):
    rt = sim_runtime(**ACOUSTIC)
    muted = {i: o.mute for i, o in rt.world.dsp.items()}
    await drive(rt)
    routing_writes, live_during_routing = 0, []
    for w in rt.fake_dsp.writes:  # replay every write the miniDSP received, in order
        if w.get("inputs"):
            routing_writes += 1
            live_during_routing += [i for i, m in muted.items() if not m]
        for o in w.get("outputs", []):
            if o.get("mute") is not None:
                muted[o["index"]] = o["mute"]
    assert routing_writes >= 2  # routed for sweeps, and back to listening
    assert live_during_routing == []


async def test_a_sweep_whose_chirp_is_missed_is_rejected(sim_runtime):
    rt = sim_runtime(**ACOUSTIC)
    await rt.rig.prepare_rew()
    _unplug_reference(rt)  # REW hears no chirp and times the sweep from noise
    with pytest.raises(TimingReferenceError, match="missed the timing reference"):
        await rt.rig.sweep_once(["SUB1"], "solo", "MLP")
    assert rt.fake_rew.sweeps_run == 1 and not rt.session.measurements


async def test_a_sweep_rew_refuses_for_want_of_a_chirp_is_rejected_in_wiring_terms(sim_runtime):
    rt = sim_runtime(**ACOUSTIC)
    rt.fake_rew.missing_reference = "error"
    await rt.rig.prepare_rew()
    _unplug_reference(rt)
    with pytest.raises(TimingReferenceError, match="no acoustic timing reference"):
        await rt.rig.sweep_once(["SUB1"], "solo", "MLP")
    assert not rt.session.measurements


async def test_a_sweep_rew_timed_against_another_reference_is_rejected(sim_runtime):
    rt = sim_runtime(**ACOUSTIC)
    await rt.rig.prepare_rew()
    rt.fake_rew.timing = "None"  # someone changed REW's timing reference mid-run
    with pytest.raises(TimingReferenceError, match="not the acoustic reference"):
        await rt.rig.sweep_once(["SUB1"], "solo", "MLP")
    assert not rt.session.measurements


async def test_without_rews_description_the_check_says_it_judged_the_impulse_alone(sim_runtime):
    rt = sim_runtime(**ACOUSTIC)
    rt.fake_rew.ir_describes_timing = False
    await rt.rig.prepare_rew()
    rec, _ = await rt.rig.sweep_once(["R"], "solo", "MLP")
    assert "impulse only" in rec.extra["timing_check"]["checked"]


async def test_routing_is_restored_after_a_run(sim_runtime):
    rt = sim_runtime(**ACOUSTIC)
    before = _listening(rt)
    rep = await drive(rt)
    assert rep["status"] == "done"
    assert {i: o.enabled for i, o in rt.world.dsp.items()} == before
    assert rt.session.dsp.routing is None


async def test_routing_is_restored_after_an_error_with_everything_muted(sim_runtime):
    rt = sim_runtime(**ACOUSTIC)
    before = _listening(rt)
    _unplug_reference(rt)  # the very first sweep has no chirp: the run stops
    rep = await drive(rt)
    assert rep["status"] == "error" and "TimingReferenceError" in rep["error"]
    assert rep["outputs_muted"] and all(o.mute for o in rt.world.dsp.values())
    assert {i: o.enabled for i, o in rt.world.dsp.items()} == before
    assert rt.session.dsp.routing is None


async def test_acoustic_run_reaches_done_with_the_delays_a_loopback_finds(sim_runtime):
    loop = sim_runtime(timing_mode=TimingMode.LOOPBACK)
    assert (await drive(loop))["status"] == "done"
    rt = sim_runtime(**ACOUSTIC)
    rep = await drive(rt)
    assert rep["status"] == "done"
    assert not rt.session.find(kind="combined")  # absolute timing at each seat: no sum-fit sweeps
    assert rt.session.workflow.last_proposal["measured"]["prediction_error_db"] < 0.5
    roles = [c.role for c in rt.session.config.channels]

    def rel(r):  # delays relative to the earliest channel: what the time base can decide
        d = {x: r.session.dsp.get(x).delay_ms for x in roles}
        return {x: v - min(d.values()) for x, v in d.items()}
    want, got = rel(loop), rel(rt)
    assert all(abs(got[x] - want[x]) <= 0.15 for x in roles), (got, want)
    assert all(m.extra.get("timing_check") for m in rt.session.measurements)


async def test_the_crawl_in_acoustic_mode_does_not_treat_timing_as_absolute(sim_runtime):
    rt = sim_runtime(do_sub_crawl=True, **ACOUSTIC)
    rep = await drive(rt)
    assert rep["status"] == "done"
    assert rt.session.workflow.crawl["result"]["phase_mode"] != "absolute"
