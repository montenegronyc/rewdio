"""Gain staging must never turn the speakers up when the mic cannot hear them (SAFETY_AUDIT.md #1-#3).

Each test replays a failure from the first real run: a mic without phantom power, an uncalibrated mic whose
"SPL" follows the preamp knob, and a preflight warning confirmed without a fix.
"""

import numpy as np


def _dead_mic(world):
    """No phantom power: the speakers are ~120 dB down at the mic, only the room noise is left."""
    orig = world.response

    def response(f, level_dbfs, rew_output=None, noise=True):
        h = orig(f, level_dbfs, rew_output, noise=False) * 1e-6
        if noise:
            h = h + orig(f, -300.0, rew_output, noise=True)
        return h

    world.response = response


def _preamp(world, gain_db: float):
    """Turn the interface's mic gain knob: speakers AND room noise move together at the mic."""
    orig = world.response
    world.noise_floor_spl += gain_db

    def response(f, level_dbfs, rew_output=None, noise=True):
        h = orig(f, level_dbfs, rew_output, noise=False) * 10 ** (gain_db / 20)
        if noise:
            h = h + orig(f, -300.0, rew_output, noise=True)  # noise only, at the shifted floor
        return h

    world.response = response


async def _until(rt, stop_phases=("SUB_CRAWL", "BASELINE"), rounds=12) -> dict:
    """Step the autopilot (doing the user's physical tasks) until staging is over or it stops."""
    rep = {}
    for _ in range(rounds):
        rep = await rt.autopilot.step(max_sweeps=1)
        if rep["status"] == "user_action_required":
            rt.apply_sim_effects(rep["action"], None)
            rt.autopilot.confirm(rep["action"]["key"])
            continue
        if rep["status"] != "running" or rt.session.workflow.phase in stop_phases:
            return rep
    return rep


def _staging_levels(rt) -> list[float]:
    return [m.level_dbfs for m in rt.session.measurements if m.kind == "staging"]


async def test_dead_mic_stops_staging_after_one_step_up(sim_runtime):
    rt = sim_runtime()
    _dead_mic(rt.world)
    rep = await _until(rt)
    start = rt.session.config.sweep.level_dbfs
    levels = _staging_levels(rt)
    assert rep["status"] == "error", rep.get("log")
    assert "no signal" in rep["error"].lower()
    assert max(levels) <= start + rt.session.config.tuning.gain_staging.max_step_up_db  # one step, never the cap
    assert rt.session.workflow.phase == "GAIN_STAGING"  # it did not go on to sweep the room
    assert rt.rig.level_dbfs == start  # left back at the quiet starting level


async def test_staging_with_uncalibrated_mic_ignores_the_preamp_knob(sim_runtime):
    async def staged(preamp_db: float) -> float:
        rt = sim_runtime()
        assert rt.session.config.mic_sensitivity_dbfs is None
        _preamp(rt.world, preamp_db)
        rep = await _until(rt)
        assert rep["status"] != "error", rep.get("error")
        return rt.rig.level_dbfs

    normal, knob_down = await staged(0.0), await staged(-20.0)
    # turning the mic gain down must not make Rewdio drive the speakers harder
    assert abs(knob_down - normal) <= 2.0, (normal, knob_down)


async def test_too_quiet_system_stops_instead_of_measuring_noise(sim_runtime):
    rt = sim_runtime()
    sw = rt.session.config.sweep
    sw.max_level_dbfs = sw.level_dbfs + 10  # one step of room above the start
    rt.world.master_volume_db = -38.0  # heard, and each step registers, but never loud enough to measure
    rep = await _until(rt)
    assert rep["status"] == "error", rep.get("log")
    assert "snr" in rep["error"].lower(), rep["error"]
    assert rt.session.workflow.phase == "GAIN_STAGING"
    assert rt.rig.level_dbfs == sw.level_dbfs


async def test_speakers_below_the_room_noise_stop_as_no_signal(sim_runtime):
    rt = sim_runtime()
    rt.world.master_volume_db = -70.0  # so quiet the first step up is lost in the noise: same as a dead mic
    rep = await _until(rt)
    assert rep["status"] == "error", rep.get("log")
    assert "no signal" in rep["error"].lower()
    assert max(_staging_levels(rt)) <= sw_start(rt) + rt.session.config.tuning.gain_staging.max_step_up_db


def sw_start(rt) -> float:
    return rt.session.config.sweep.level_dbfs


async def test_confirming_a_dead_mic_warning_without_a_fix_asks_again(sim_runtime):
    rt = sim_runtime()
    floor = rt.world.noise_floor_spl
    rt.world.noise_floor_spl = -400.0  # digital silence on the input
    asked = []
    for _ in range(3):
        rep = await rt.autopilot.step(max_sweeps=1)
        assert rep["status"] == "user_action_required"
        asked.append(rep["action"]["key"])
        rt.autopilot.confirm(rep["action"]["key"])  # "done", but nothing was fixed
    assert all(k.startswith("mic_dead") for k in asked) and len(set(asked)) == 3
    assert not _staging_levels(rt)  # nothing was swept at the dead mic
    rt.world.noise_floor_spl = floor  # now fixed: the next confirm re-checks and moves on
    rep = await _until(rt)
    assert rep["status"] != "error" and _staging_levels(rt)


async def test_healthy_rig_still_stages_and_finishes(sim_runtime):
    rt = sim_runtime()
    rep = await _until(rt)
    assert rep["status"] != "error", rep.get("error")
    snrs = [m.extra["snr_db"] for m in rt.session.measurements if m.kind == "staging"]
    assert snrs and snrs[-1] >= rt.session.config.tuning.quality.min_snr_db_critical
    assert np.isfinite(rt.rig.level_dbfs)
