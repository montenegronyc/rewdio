import pytest

from rewdio.minidsp.client import MiniDSPClient
from rewdio.rew.client import RewClient, RewError
from rewdio.sim.fakes import transports
from rewdio.sim.world import default_world


async def test_rew_client_sweep_and_frequency_response():
    w = default_world()
    rt, _, fake, _ = transports(w)
    rew = RewClient(transport=rt, poll_interval=0, command_timeout=2)
    await rew.configure_sweep(10, 20000, "256k")
    await rew.set_level(-40)
    m = await rew.sweep("test")
    assert m["title"] == "test"
    spec = await rew.frequency_response(m["uuid"], ppo=48)
    assert spec.freqs[0] == 10 and abs(spec.freqs[48] - 20) < 1e-6
    assert spec.phase_deg is not None and spec.mag_db.size == spec.freqs.size
    await rew.aclose()


async def test_rew_errors_carry_valid_values():
    rt, _, _, _ = transports(default_world())
    rew = RewClient(transport=rt)
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
