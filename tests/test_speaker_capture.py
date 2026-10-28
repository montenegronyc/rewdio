"""Raw driver capture around a turntable, exported as VituixCAD FRDs, in the simulator.

A woofer and a horn whose acoustic centre sits 5 cm behind the woofer and 25 cm above it. After export,
horn/woofer at every angle must equal the pure path-length delay between them: that is the relative
phase VituixCAD needs, and it only survives if every file shares one time base and one removed delay.
"""

import json

import numpy as np
import pytest

from rewdio.analysis.frd import Gate, read_frd
from rewdio.config import Channel, ChannelKind
from rewdio.engine.speaker import angles_for
from rewdio.minidsp.state import XoverFilter
from rewdio.sim.world import Speaker

C = 343.0
LW, LH = (0.70, 2.0, 1.00), (0.65, 2.0, 1.25)
MODEL = [["HP", 48.0, 0.8], ["LP", 22000.0, 0.7]]  # same for both, so the ratio isolates the geometry


def _rig(rt):
    guard = XoverFilter(kind="HP", topology="BW2", freq_hz=700, protect=True)
    rt.session.config.channels = [
        Channel(role="LW", kind=ChannelKind.DRIVER, dsp_output=0, speaker="L",
                crossover=[XoverFilter(kind="LP", topology="LR4", freq_hz=1500)]),
        Channel(role="LH", kind=ChannelKind.DRIVER, dsp_output=1, speaker="L",
                crossover=[XoverFilter(kind="HP", topology="LR4", freq_hz=1500), guard]),
    ]
    rt.session.config.timing_reference_channel = "LW"
    w = rt.world
    w.speakers = [Speaker("LW", LW, "main", 0, MODEL), Speaker("LH", LH, "main", 1, MODEL)]
    w.room.reflections = []  # free field above the modal region: the test is about phase, not gating
    w.room.diffuse_scale = 0.0
    w.mic = (2.70, 2.0, 1.125)
    return guard


async def _run(rt, points):
    cap = rt.speaker
    cap.plan("L", points, distance_m=2.0)
    seen = []
    for _ in range(4 * len(points) + 4):
        rep = await cap.step()
        if rep["status"] == "user_action_required":
            seen.append(tuple(rt.world.mic))
            rt.apply_sim_effects(rep["action"], None)
            assert cap.confirm(rep["action"]["key"])["ok"]
            continue
        if rep["status"] == "done":
            return seen
        assert rep["status"] == "measured", rep
    raise AssertionError("capture did not finish")


def _delay(src, mic):
    return float(np.linalg.norm(np.subtract(src, mic))) / C


async def test_polar_capture_exports_phase_true_frds(sim_runtime, tmp_path):
    rt = sim_runtime()
    guard = _rig(rt)
    await rt.rig.assert_baseline()
    points = angles_for(["hor"], step_deg=30, max_deg=60)
    assert [(p.plane, p.angle) for p in points] == [("hor", 0), ("hor", 30), ("hor", -30), ("hor", 60), ("hor", -60)]
    mics = {}
    orig_rotate = rt._sim_rotate

    def spy(polar):
        msg = orig_rotate(polar)
        mics[polar["angle"]] = tuple(rt.world.mic)
        return msg

    rt._sim_rotate = spy
    await _run(rt, points)

    # the speaker is never left raw: the configured crossover is back after every step
    assert rt.session.dsp.get("LH").crossover == rt.session.config.channel("LH").crossover
    # raw sweeps really were raw: the horn played through its guard only
    raw = [m for m in rt.session.measurements if m.kind == "driver" and m.roles == ["LH"]]
    assert raw and all(m.snapshot.get("LH").crossover == [guard] for m in raw)

    out = tmp_path / "frd"
    res = rt.speaker.export(out, Gate(pre_ms=1.0, gate_ms=4.0))
    assert res["files"] == 10
    names = sorted(p.name for p in out.glob("*.frd"))
    assert "L_LH hor -30.frd" in names and "L_LW hor 60.frd" in names

    for angle, mic in mics.items():
        f, hw = read_frd(out / f"L_LW hor {int(angle)}.frd")
        _, hh = read_frd(out / f"L_LH hor {int(angle)}.frd")
        band = (f > 900) & (f < 5000)
        dt = _delay(LH, mic) - _delay(LW, mic)
        ratio = (hh / hw)[band]
        err = np.angle(ratio * np.exp(2j * np.pi * f[band] * dt))
        assert np.max(np.abs(err)) < np.deg2rad(5), f"angle {angle}: phase off by {np.rad2deg(np.abs(err)).max():.1f} deg"
        r_ratio = np.linalg.norm(np.subtract(LW, mic)) / np.linalg.norm(np.subtract(LH, mic))
        assert np.allclose(np.abs(ratio), r_ratio, rtol=0.05)  # protection divided out: levels match

    m = json.loads((out / "rewdio_session.json").read_text())
    assert m["schema"] == "rewdio.speaker-session/1" and m["geometry"]["driver_offsets_in_phase"]
    assert [d["role"] for d in m["drivers"]] == ["LW", "LH"]
    assert m["drivers"][1]["files"][0]["protection_removed"][0]["protect"] is True
    # the removed delay is the onset of the first arrival (a little ahead of the peak in this band-limited
    # sim); its exact value is irrelevant to VituixCAD as long as it is the same for every file
    assert m["timing"]["common_delay_removed_ms"] == pytest.approx(_delay(LW, (2.70, 2.0, 1.125)) * 1000, abs=0.5)


async def test_capture_refuses_drivers_whose_crossover_rewdio_does_not_manage(sim_runtime):
    rt = sim_runtime()
    _rig(rt)
    rt.session.config.channels[0].crossover = None
    with pytest.raises(ValueError, match="crossover"):
        rt.speaker.plan("L", angles_for(["hor"]), 2.0)


async def test_nearfield_is_exported_for_the_merger(sim_runtime, tmp_path):
    rt = sim_runtime()
    _rig(rt)
    await rt.rig.assert_baseline()
    await _run(rt, angles_for(["hor"], step_deg=90, max_deg=90, symmetric=True))
    await rt.speaker.nearfield("LW", "nf")
    res = rt.speaker.export(tmp_path)
    assert res["nearfield"] == 1 and (tmp_path / "L_LW nf.frd").exists()
