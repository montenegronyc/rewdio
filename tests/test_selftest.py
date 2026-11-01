"""The day-one hardware self-test, against the simulator: it passes on a working rig and catches a DSP
that silently drops writes."""

from rewdio.config import TimingMode
from rewdio.engine import selftest
from test_speaker_capture import _rig as _speaker_rig


def _rig(rt):
    _speaker_rig(rt)
    rt.session.config.timing_mode = TimingMode.LOOPBACK  # speaker work needs absolute phase


async def test_selftest_passes_on_a_working_rig(sim_runtime):
    rt = sim_runtime()
    _rig(rt)
    await rt.rig.assert_baseline()
    before = rt.session.dsp.get("LH").model_copy(deep=True)
    rep = await selftest.run(rt.rig, "LH", distance_m=2.0)
    res = {c["check"]: c["result"] for c in rep["checks"]}
    assert rep["ok"], rep
    assert res["crossover_write"] == res["delay_write"] == res["rew_start_time"] == "pass"
    assert res["rew_impedance_api"] == "skipped"
    after = rt.session.dsp.get("LH")
    assert after.crossover == before.crossover and after.delay_ms == before.delay_ms  # left as found
    assert rt.world.dsp[1].delay_ms == before.delay_ms
    assert (rt.session.dir / "selftest.json").exists()


async def test_selftest_catches_a_dsp_that_drops_delay_and_crossover_writes(sim_runtime):
    rt = sim_runtime()
    _rig(rt)
    await rt.rig.assert_baseline()
    handle = rt.fake_dsp.handle

    def drop_delay(request):
        import json
        if request.method == "POST" and request.url.path.endswith("/config"):
            cfg = json.loads(request.content)
            for o in cfg.get("outputs", []):
                o.pop("delay", None)
                o.pop("crossover", None)
            request = request.__class__(request.method, request.url, json=cfg)
        return handle(request)
    rt.fake_dsp.handle = drop_delay
    rt.dsp._http._transport.handler = drop_delay
    rep = await selftest.run(rt.rig, "LW", impedance=True)
    res = {c["check"]: c["result"] for c in rep["checks"]}
    assert not rep["ok"] and res["delay_write"] == res["crossover_write"] == "FAIL"
    assert res["rew_impedance_api"] == "pass"  # the simulator's REW accepts an impedance sweep


async def test_selftest_without_loopback_says_why_phase_checks_are_skipped(sim_runtime):
    rt = sim_runtime()
    _speaker_rig(rt)  # default config: sum_fit, no absolute timing
    await rt.rig.assert_baseline()
    rep = await selftest.run(rt.rig, "LW")
    res = {c["check"]: c for c in rep["checks"]}
    assert res["rew_timing"]["result"] == "FAIL" and "loopback" in res["rew_timing"]["detail"]
    assert res["delay_write"]["result"] == "skipped"
    assert res["crossover_write"]["result"] == "pass" and "magnitude only" in res["crossover_write"]["detail"]
