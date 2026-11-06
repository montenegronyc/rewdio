"""Audit 2: room work leaves driver channels alone, SPL aborts retry quieter, skips and exports stay honest."""

import json

import pytest
from conftest import _rig, _run

from rewdio.engine.turntable import angles_for


async def test_the_autopilot_refuses_a_speaker_only_config(sim_runtime):
    rt = sim_runtime()
    _rig(rt)
    level = rt.rig.level_dbfs
    rep = await rt.autopilot.step()
    assert rep["status"] == "error" and "speaker_plan" in rep["error"]
    assert rt.rig.level_dbfs == level and not rt.session.measurements


async def test_room_baseline_keeps_driver_settings_and_mutes(sim_runtime):
    rt = sim_runtime()
    room = list(rt.session.config.channels)
    _rig(rt)
    rt.session.config.channels = room + rt.session.config.channels
    await rt.rig.assert_baseline()
    await rt.rig.set_channel("LH", gain_db=-6.0, mute=True)
    await rt.rig.set_channel(room[0].role, gain_db=-4.0)
    await rt.rig.assert_baseline()
    assert rt.session.dsp.get("LH").gain_db == -6.0 and rt.session.dsp.get("LH").mute
    assert rt.session.dsp.get(room[0].role).gain_db == rt.session.config.baseline.gain_db
    await rt.rig.unmute_room()
    assert rt.session.dsp.get("LH").mute and not rt.session.dsp.get(room[0].role).mute


async def test_a_rew_spl_abort_retries_six_db_down(sim_runtime):
    rt = sim_runtime()
    _rig(rt)
    await rt.rig.assert_baseline()
    rt.session.config.sweep.max_spl_db = 50.0  # REW's protection trips on the first sweep
    rt.speaker.plan("L", angles_for(["hor"], 90, 90, symmetric=True), 2.0)
    rep = await rt.speaker.step()
    rt.apply_sim_effects(rep["action"], None)
    rt.speaker.confirm(rep["action"]["key"])
    level = rt.rig.level_dbfs
    rep = await rt.speaker.step()
    assert rep["status"] == "retry" and rep["quality"][0]["code"] == "rew_spl_abort"
    assert rt.rig.level_dbfs == pytest.approx(level - 6.0)
    assert not any(o.mute for o in rt.session.dsp.outputs.values())  # the mutes it found are back
    assert all(rt.session.dsp.get(d).crossover == rt.session.config.channel(d).crossover for d in ("LW", "LH"))


async def test_a_skipped_angle_holds_no_captures_and_is_in_the_manifest(sim_runtime, tmp_path):
    rt = sim_runtime()
    _rig(rt)
    await rt.rig.assert_baseline()
    rt.speaker.plan("L", angles_for(["hor"], 30, 60, symmetric=True), 2.0)
    for _ in range(3):  # 0 measured, then on to +30
        rep = await rt.speaker.step()
        if rep["status"] == "user_action_required":
            rt.apply_sim_effects(rep["action"], None)
            rt.speaker.confirm(rep["action"]["key"])
    st = rt.speaker.state()
    assert st.points[st.index].angle == 30
    rt.speaker.confirm(f"rotate:{st.index}")
    await rt.speaker.step()  # pretend this one was poor, then give up on it
    st = rt.speaker.state()
    if st.index == 2:  # it measured fine: step back to make the point
        st.index = 1
        rt.speaker._save(st)
    await rt.speaker.step(skip=True)
    assert not [c for c in rt.speaker.state().captures if c.angle == 30]
    await _run(rt)
    rt.speaker.export(tmp_path / "x")
    m = json.loads((tmp_path / "x" / "rewdio_session.json").read_text())
    assert m["skipped"] == [{"plane": "hor", "angle": 30.0}]


async def test_new_data_after_an_export_is_not_dropped_by_a_new_plan(sim_runtime, tmp_path):
    rt = sim_runtime()
    _rig(rt)
    await rt.rig.assert_baseline()
    await _run(rt, angles_for(["hor"], 90, 90, symmetric=True))
    rt.speaker.export(tmp_path / "x")
    assert rt.speaker.state().exported_to
    await rt.speaker.impedance("LW")
    assert rt.speaker.state().exported_to is None
    with pytest.raises(ValueError, match="never exported"):
        rt.speaker.plan("L", angles_for(["hor"], 90, 90, symmetric=True), 2.0)


async def test_post_soak_impedance_needs_a_completed_soak(sim_runtime):
    rt = sim_runtime()
    _rig(rt)
    await rt.rig.assert_baseline()
    rt.speaker.plan("L", angles_for(["hor"], 90, 90, symmetric=True), 2.0)
    with pytest.raises(ValueError, match="no completed soak"):
        await rt.speaker.impedance("LW", label="post_soak")
