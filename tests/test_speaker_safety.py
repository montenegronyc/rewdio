"""Nothing Rewdio does may put a speaker at risk (docs/SAFETY_AUDIT.md #4-#14).

Each test states the protection as behaviour; each failed on the code before the fix.
"""

import asyncio
import os

import pytest

from conftest import drive
from rewdio.config import load_config
from rewdio.engine import dsp_setups, selftest
from rewdio.engine.runtime import Runtime
from rewdio.minidsp.state import XoverFilter
from rewdio.rew.client import RewError

HP80 = XoverFilter(kind="HP", topology="LR4", freq_hz=80)
LP80 = XoverFilter(kind="LP", topology="LR4", freq_hz=80)


def _room_crossovers(rt):
    for c in rt.session.config.channels:
        c.crossover = [HP80] if c.kind.value == "main" else [LP80]
    rt.session.save()


def _settings_writes(rt) -> list[dict]:
    """Output patches that change more than the mute."""
    return [o for w in rt.fake_dsp.writes for o in w.get("outputs", []) if set(o) - {"index", "mute"}]


# -- #5, #6: DSP writes ----------------------------------------------------------------------------------

async def test_a_cancelled_dsp_write_marks_the_rig_out_of_sync(sim_runtime):
    rt = sim_runtime()
    await rt.rig.assert_baseline()

    async def cancelled(*a, **k):
        raise asyncio.CancelledError

    rt.rig.dsp.set_outputs = cancelled
    with pytest.raises(asyncio.CancelledError):
        await rt.rig.set_channel("SUB1", gain_db=-3.0)
    assert rt.rig.dirty  # the next write re-sends everything instead of trusting the shadow


async def test_settings_are_written_while_muted_and_unmuted_afterwards(sim_runtime):
    rt = sim_runtime()
    _room_crossovers(rt)
    rt.fake_dsp.writes.clear()
    await rt.rig.assert_baseline()  # baseline ends unmuted
    patches = _settings_writes(rt)
    assert patches and all(p.get("mute") is True for p in patches)  # no channel live while its filters change
    assert not any(d.mute for d in rt.world.dsp.values())  # and it does end unmuted
    rt.fake_dsp.writes.clear()
    await rt.rig.set_channel("L", gain_db=-2.0)  # a change to a playing channel: muted, written, unmuted
    patches = _settings_writes(rt)
    assert patches and all(p.get("mute") is True for p in patches)
    assert not rt.world.dsp[rt.session.config.channel("L").dsp_output].mute


# -- #7: errors ----------------------------------------------------------------------------------------

async def test_an_error_mutes_everything_and_stops_rew(sim_runtime):
    rt = sim_runtime()
    rt.fake_rew.fail_measures = 50
    while (rep := await rt.step())["status"] == "user_action_required":
        rt.confirm(rep["action"]["key"], None)
    assert rep["status"] == "error" and rep["outputs_muted"]
    assert all(d.mute for d in rt.world.dsp.values())  # silent until someone chooses to listen
    assert not rt.fake_rew.generator["playing"]
    rt.fake_rew.fail_measures = 0
    assert (await drive(rt))["status"] == "done"  # and it resumes where it stopped


# -- #4, #11: room crossovers ----------------------------------------------------------------------------

async def test_room_channel_crossovers_cannot_be_stripped_or_replaced(sim_runtime):
    rt = sim_runtime()
    _room_crossovers(rt)
    await rt.rig.assert_baseline()
    with pytest.raises(ValueError):
        await rt.rig.set_crossover("L", raw=True)
    with pytest.raises(ValueError):
        await rt.rig.set_crossover("L", filters=[])
    with pytest.raises(ValueError):
        await rt.rig.set_crossovers(["L", "R"], raw=True)
    o = await rt.rig.set_crossover("L")  # restoring the configured crossover is fine
    assert o.crossover == [HP80]


async def test_selftest_refuses_room_channels_without_playing(sim_runtime):
    rt = sim_runtime()
    _room_crossovers(rt)
    before = len(rt.session.measurements)
    rep = await selftest.run(rt.rig, "L")
    assert not rep["ok"] and "driver" in rep["checks"][0]["detail"]
    assert len(rt.session.measurements) == before


async def test_recalling_a_setup_keeps_the_configured_room_crossover(sim_runtime):
    rt = sim_runtime()
    _room_crossovers(rt)
    await rt.rig.assert_baseline()
    saved = dsp_setups.save(rt.session, "old")
    hp90 = XoverFilter(kind="HP", topology="LR4", freq_hz=90)
    rt.session.config.channel("L").crossover = [hp90]  # the crossover changed since that setup was saved
    assert (await dsp_setups.recall(rt.rig, saved["id"]))["ok"]
    assert rt.session.dsp.get("L").crossover == [hp90]


# -- #9, #10: gain envelope ------------------------------------------------------------------------------

async def test_manual_gain_is_capped_per_channel_kind(sim_runtime):
    rt = sim_runtime()
    cfg = rt.session.config
    assert (await rt.rig.set_channel("SUB1", gain_db=12.0)).gain_db == cfg.max_sub_gain_db
    assert (await rt.rig.set_channel("L", gain_db=12.0)).gain_db == cfg.max_main_gain_db


def test_config_refuses_a_level_and_gain_that_could_clip_the_dsp(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"sweep": {"max_level_dbfs": -2}, "max_sub_gain_db": 6}')
    with pytest.raises(ValueError, match="headroom"):
        load_config(user_file=p)
    load_config(user_file=tmp_path / "none.json")  # the defaults themselves keep the headroom


def test_managed_subs_get_a_protected_infrasonic_high_pass(tmp_path):
    p = tmp_path / "c.json"
    p.write_text('{"channels": [{"role": "L", "kind": "main", "dsp_output": 0},'
                 ' {"role": "SUB1", "kind": "sub", "dsp_output": 2,'
                 ' "crossover": [{"kind": "LP", "freq_hz": 80}]}], "timing_reference_channel": "L"}')
    xo = load_config(user_file=p).channel("SUB1").crossover
    hp = [f for f in xo if f.kind == "HP"]
    assert hp and hp[0].protect and hp[0].freq_hz <= 20


# -- #8: quality gates ----------------------------------------------------------------------------------

async def test_crawl_will_not_record_a_sweep_of_a_silent_sub(sim_runtime):
    rt = sim_runtime(do_sub_crawl=True)
    cfg = rt.session.config
    cfg.channels = [c for c in cfg.channels if c.role != "SUB2"]
    rt.world.speaker("SUB2").sensitivity_db = 0.0  # unplugged: only one sub in the room
    rt.session.save()
    asked = []
    for _ in range(20):
        rep = await rt.autopilot.step()
        if rep["status"] != "user_action_required":
            break
        key = rep["action"]["key"]
        asked.append(key)
        if key.startswith("crawl:mic:"):
            rt.world.speaker("SUB1").sensitivity_db = 0.0  # the sub goes silent (cable out) at the first spot
        if key.startswith("quality:"):
            break
        rt.apply_sim_effects(rep["action"], None)
        rt.autopilot.confirm(key)
    assert any(k.startswith("quality:") for k in asked), asked
    assert not rt.session.workflow.crawl.get("measured")  # nothing recorded from the silent sweep


async def test_quality_retries_stop_instead_of_sweeping_forever(sim_runtime):
    rt = sim_runtime()
    rt.world.speaker("R").sensitivity_db = 0.0  # R is dead: every baseline sweep of it fails the gate
    asks = 0
    for _ in range(30):
        rep = await rt.step()
        if rep["status"] != "user_action_required":
            break
        if rep["action"]["key"].startswith("quality:"):
            asks += 1
        rt.apply_sim_effects(rep["action"], None)
        rt.confirm(rep["action"]["key"], None)
    assert rep["status"] == "error", rep
    assert asks <= rt.session.config.tuning.quality.max_retries + 1


async def test_sub_sweeps_are_judged_on_snr_in_their_own_band(sim_runtime):
    rt = sim_runtime()
    await rt.rig.assert_baseline()
    rec, _ = await rt.rig.measure(["SUB1"], "manual", "MLP")
    assert rec.extra["snr_inband_db"] > rt.session.config.tuning.quality.min_snr_db_warning
    assert not [q for q in rec.extra["quality"] if q["code"] == "low_snr"]


# -- #12, #13, #14 --------------------------------------------------------------------------------------

def test_a_second_process_cannot_drive_the_same_session(tmp_path):
    Runtime("s", root=str(tmp_path), simulate=True)
    lock = tmp_path / "s" / "driver.lock"
    lock.write_text(str(os.getppid()))  # another live process holds it
    with pytest.raises(RuntimeError, match="another Rewdio"):
        Runtime("s", root=str(tmp_path), simulate=True)
    lock.write_text("999999999")  # a dead process: taken over
    Runtime("s", root=str(tmp_path), simulate=True)


async def test_gain_staging_starts_at_the_configured_level_after_a_restart(sim_runtime, tmp_path):
    rt = sim_runtime()
    rt.session.workflow.crawl["level_dbfs"] = rt.session.config.sweep.max_level_dbfs  # left loud last time
    rt.session.save()
    again = Runtime("t0", root=str(tmp_path), simulate=True)
    while (rep := await again.autopilot.step(max_sweeps=1))["status"] == "user_action_required":
        again.apply_sim_effects(rep["action"], None)
        again.autopilot.confirm(rep["action"]["key"])
    first = next(m for m in again.session.measurements if m.kind == "staging")
    assert first.level_dbfs == again.session.config.sweep.level_dbfs


async def test_loopback_check_stops_the_run_if_rew_will_not_stop_the_noise(sim_runtime):
    from rewdio.config import TimingMode
    rt = sim_runtime(timing_mode=TimingMode.LOOPBACK)
    post = rt.rig.rew.post

    async def stuck(path, body=None, **k):
        if path == "/generator/command" and (body or {}).get("command") == "Stop":
            raise RewError("generator did not stop")
        return await post(path, body, **k)

    rt.rig.rew.post = stuck
    rep = await rt.autopilot.step()
    assert rep["status"] == "error" and "generator" in rep["error"].lower()
    assert not [m for m in rt.session.measurements if m.kind == "staging"]


# -- #15: outputs the config doesn't list ---------------------------------------------------------------

async def test_outputs_missing_from_the_config_are_muted_before_anything_plays(sim_runtime):
    rt = sim_runtime()
    cfg = rt.session.config
    cfg.channels = [c for c in cfg.channels if c.role != "SUB2"]  # Out4 still routed and plugged in, just unlisted
    rt.session.save()
    out4 = rt.world.dsp[3]
    assert not out4.mute
    await rt.rig.assert_baseline()
    rec, _ = await rt.rig.measure(["SUB1"], "manual", "MLP")
    assert out4.mute  # SUB2 did not play in SUB1's sweep
    await rt.rig.unmute_room()
    assert out4.mute  # and listening does not bring an unmanaged speaker back


async def test_unlisted_outputs_can_be_left_alone_when_asked(sim_runtime):
    rt = sim_runtime()
    cfg = rt.session.config
    cfg.channels = [c for c in cfg.channels if c.role != "SUB2"]
    cfg.mute_unlisted_outputs = False
    rt.session.save()
    await rt.rig.assert_baseline()
    await rt.rig.measure(["SUB1"], "manual", "MLP")
    assert not rt.world.dsp[3].mute
