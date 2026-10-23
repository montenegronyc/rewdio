import pytest

from rewdio.minidsp.client import MiniDSPClient
from rewdio.rew.extras import input_levels
from rewdio.rew.client import RewClient, RewError
from rewdio.sim.fakes import transports
from rewdio.sim.world import default_world


def fast(cfg):
    return cfg.rew_client.model_copy(update={"settle_s": 0.0, "poll_interval_s": 0.0})


async def test_rew_client_sweep_and_frequency_response(cfg):
    w = default_world()
    rt, _, fake, _ = transports(w)
    rew = RewClient("http://rew", fast(cfg), transport=rt)
    await rew.configure_sweep(10, 20000, "256k", 0.0, 48000)
    await rew.set_level(-40)
    m = await rew.sweep("test")
    assert m["title"] == "test"
    spec = await rew.frequency_response(m["uuid"], ppo=48)
    assert spec.freqs[0] == 10 and abs(spec.freqs[48] - 20) < 1e-6
    assert spec.phase_deg is not None and spec.mag_db.size == spec.freqs.size
    await rew.aclose()


async def test_rew_errors_carry_valid_values(cfg):
    rt, _, _, _ = transports(default_world())
    rew = RewClient("http://rew", fast(cfg), transport=rt)
    with pytest.raises(RewError) as e:
        await rew.set_timing_reference("Telepathy")
    assert "Loopback" in e.value.valid_values
    await rew.aclose()


async def test_minidsp_client_writes_reach_the_device():
    w = default_world()
    _, dt, _, fake = transports(w)
    dsp = MiniDSPClient("http://sim", transport=dt)
    await dsp.set_outputs([MiniDSPClient.output_patch(3, gain_db=-2.5, delay_ms=1.25, invert=False, mute=True)])
    assert w.dsp[3].gain_db == -2.5 and abs(w.dsp[3].delay_ms - 1.25) < 1e-9 and w.dsp[3].mute
    assert (await dsp.devices())[0]["product_name"].startswith("2x4HD")
    await dsp.aclose()


async def test_protection_is_read_back_and_input_levels_detect_a_live_mic(cfg):
    rt, _, fake, _ = transports(default_world())
    rew = RewClient("http://rew", fast(cfg), transport=rt)
    got = await rew.set_protection(100.0)
    assert got["clippingAbort"] and got["dBSPLLimit"] == 100.0 and got["warnForLowLevels"] is False
    assert (await input_levels(rew, 0.0))["mic_alive"]
    await rew.aclose()
