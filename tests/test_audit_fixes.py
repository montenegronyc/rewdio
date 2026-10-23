"""Regression tests for the code-audit findings: hardware/shadow consistency, recovery, locking, input checks."""

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from conftest import drive
from rewdio.config import default_dict, load_config
from rewdio.dashboard.app import build_app
from rewdio.dashboard.remote import token_for
from rewdio.engine.phases.base import NeedUser
from rewdio.engine.runtime import Runtime
from rewdio.minidsp.client import MiniDSPError


def _consistent(rt) -> bool:
    for c in rt.session.config.channels:
        o, dev = rt.session.dsp.get(c.role), rt.world.dsp[c.dsp_output]
        if abs(o.gain_db - dev.gain_db) > 0.01 or abs(o.delay_ms - dev.delay_ms) > 0.01 or o.mute != dev.mute:
            return False
    return True


async def test_sweep_level_survives_a_restart(sim_runtime, tmp_path):
    rt = sim_runtime()
    await drive(rt)
    staged = rt.session.workflow.crawl["level_dbfs"]
    again = Runtime("t0", root=str(tmp_path), simulate=True)  # MCP server restarted mid-calibration
    assert again.rig.level_dbfs == staged
    await again.rig.measure(["L"], "manual", "MLP")
    assert again.fake_rew.level_dbfs == staged  # REW is told the level, not assumed to remember it


async def test_failed_dsp_write_leaves_the_shadow_matching_the_hardware(sim_runtime):
    rt = sim_runtime()
    await rt.rig.assert_baseline()
    rt.fake_dsp.fail_writes = 1
    with pytest.raises(MiniDSPError):
        await rt.rig.set_channel("SUB1", gain_db=-3.0)
    assert rt.session.dsp.get("SUB1").gain_db == 0.0 and _consistent(rt)  # nothing half-applied
    assert rt.rig.dirty
    await rt.rig.solo(["L"])  # the next write re-sends everything
    assert not rt.rig.dirty and _consistent(rt)
    assert [rt.world.dsp[c.dsp_output].mute for c in rt.session.config.channels] == [False, True, True, True]


async def test_step_error_unmutes_and_reports(sim_runtime):
    rt = sim_runtime()
    rt.fake_rew.fail_measures = 50  # persistent: gain staging backs off a single failure by design
    while (rep := await rt.step())["status"] == "user_action_required":
        rt.confirm(rep["action"]["key"], None)
    assert rep["status"] == "error" and rep["outputs_restored"]
    assert "simulated sweep failure" in rep["error"]
    assert not any(d.mute for d in rt.world.dsp.values())  # not left soloed on one speaker
    rt.fake_rew.fail_measures = 0
    assert (await drive(rt))["status"] == "done"  # and it resumes where it stopped


async def test_manual_tools_refuse_while_a_step_runs(sim_runtime):
    import rewdio.server as server
    rt = sim_runtime()
    server._rt = rt
    try:
        async with rt.lock:
            for res in (await server.measure(["L"]), await server.solo(["L"]),
                        await server.set_channel("L", gain_db=-1.0), await server.reset_calibration()):
                assert res == server.BUSY
        assert rt.session.dsp.get("L").gain_db == 0.0
    finally:
        server._rt = None


async def test_resume_does_not_send_the_mic_back_to_a_finished_seat(sim_runtime):
    rt = sim_runtime(mic_positions=["MLP", "SEAT2"])
    await drive(rt)
    ap, wf = rt.autopilot, rt.session.workflow
    n, last = len(rt.session.measurements), wf.pass_no
    for phase, pass_no in (("baseline", 0), ("verify", last)):  # passes where every sweep was taken
        wf.mic_at, wf.done, wf.pass_no = None, False, pass_no  # restart: mic position unknown,
        wf.crawl["_confirmed"] = {}  # no confirmations remembered
        try:
            await getattr(ap, f"_phase_{phase}")([])
        except NeedUser as e:
            pytest.fail(f"{phase} asked for a mic move with every sweep already on disk: {e.action['key']}")
    assert len(rt.session.measurements) == n


def test_rew_outputs_must_be_consistent():
    base = default_dict()
    chans = [{**c, "rew_output_channel": out} for c, out in zip(base["channels"], ["L", "R", "L+R", "L+R"])]
    with pytest.raises(ValidationError, match="rew_output_all"):
        load_config({"channels": chans})
    load_config({"channels": chans, "rew_output_all": "L+R"})
    half = [{**c, "rew_output_channel": "L" if i == 0 else None} for i, c in enumerate(base["channels"])]
    with pytest.raises(ValidationError):
        load_config({"channels": half})


def test_dsp_limits_are_bounded():
    with pytest.raises(ValidationError):
        load_config({"dsp_max_delay_ms": 200.0})  # the 2x4HD tops out at 80 ms
    with pytest.raises(ValidationError):
        load_config({"peq_slots": [3, 12]})


async def test_spl_limit_is_sent_as_an_integer(sim_runtime):
    rt = sim_runtime()
    await rt.step()
    assert isinstance(rt.fake_rew.protection["dBSPLLimit"], int)


def test_phone_inputs_are_validated(sim_runtime):
    rt = sim_runtime()
    with TestClient(build_app(lambda: rt)) as c:
        c.headers.update({"x-rewdio-token": token_for(rt)})
        rt.session.workflow.pending_action = {"key": "k", "instructions": "x"}
        assert c.post("/api/confirm", json={"key": "k", "xyz": [1, "a", 2]}).status_code == 400
        assert c.post("/api/confirm", json={"key": "k", "xyz": [1, 2]}).status_code == 400
        assert c.post("/api/crawl/cell", json={"code": 5}).status_code == 400
