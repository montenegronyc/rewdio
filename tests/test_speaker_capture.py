"""Raw driver capture around a turntable, exported as VituixCAD FRDs, in the simulator.

A woofer and a horn whose acoustic centre sits 5 cm behind the woofer and 25 cm above it. After export,
horn/woofer at every angle must equal the pure path-length delay between them: that is the relative
phase VituixCAD needs, and it only survives if every file shares one time base and one removed delay.
"""

import json

import numpy as np
import pytest
from conftest import LH, LW, _delay, _rig, _run

from rewdio.analysis.frd import Gate, read_frd
from rewdio.config import TimingMode
from rewdio.engine.speaker import angles_for


async def test_polar_capture_exports_phase_true_frds(sim_runtime, tmp_path):
    rt = sim_runtime()
    guard = _rig(rt)
    await rt.rig.assert_baseline()
    points = angles_for(["hor"], step_deg=30, max_deg=60)
    assert [(p.plane, p.angle) for p in points] == [("hor", 0), ("hor", 30), ("hor", 60), ("hor", -30), ("hor", -60)]
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
    names = sorted(p.name for p in out.glob("frd/*/*.frd"))
    assert "L_LH hor -30.frd" in names and "L_LW hor 60.frd" in names

    for angle, mic in mics.items():
        f, hw = read_frd(out / "frd" / "LW" / f"L_LW hor {int(angle)}.frd")
        _, hh = read_frd(out / "frd" / "LH" / f"L_LH hor {int(angle)}.frd")
        band = (f > 900) & (f < 5000)
        dt = _delay(LH, mic) - _delay(LW, mic)
        ratio = (hh / hw)[band]
        err = np.angle(ratio * np.exp(2j * np.pi * f[band] * dt))
        assert np.max(np.abs(err)) < np.deg2rad(5), f"angle {angle}: phase off by {np.rad2deg(np.abs(err)).max():.1f} deg"
        r_ratio = np.linalg.norm(np.subtract(LW, mic)) / np.linalg.norm(np.subtract(LH, mic))
        assert np.allclose(np.abs(ratio), r_ratio, rtol=0.05)  # protection divided out: levels match

    m = json.loads((out / "rewdio_session.json").read_text())
    assert m["schema"] == "rewdio.speaker-session/3" and m["geometry"]["driver_offsets_in_phase"]
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
    assert res["nearfield"] == 1 and (tmp_path / "frd" / "LW" / "L_LW nf.frd").exists()


async def test_report_sums_drivers_through_the_configured_crossover(sim_runtime):
    rt = sim_runtime()
    _rig(rt)
    await rt.rig.assert_baseline()
    await _run(rt, angles_for(["hor"], step_deg=10, max_deg=90, symmetric=True))
    rep = rt.speaker.report(Gate(gate_ms=4.0), band_hz=(300.0, 5000.0))
    assert set(rep["curves_db"]) == {"on_axis", "lw", "er", "sp", "pir", "erdi", "spdi"}
    assert set(rep["targets"]) >= {"lw", "pir", "spdi", "ok"}
    assert any("horizontal plane only" in n for n in rep["notes"])
    f = np.asarray(rep["freqs"])
    on = np.asarray(rep["curves_db"]["on_axis"])
    lw_, lh_ = (np.asarray(rep["drivers_on_axis_db"][d]) for d in ("LW", "LH"))
    i = int(np.argmin(np.abs(f - 3000)))  # an octave above the 1.5 kHz LR4 crossover the horn dominates
    assert abs(on[i] - lh_[i]) < 1.0 and lw_[i] < lh_[i] - 15


async def test_capture_refuses_without_an_absolute_time_base(sim_runtime):
    rt = sim_runtime()
    _rig(rt)
    rt.session.config.timing_mode = TimingMode.SUM_FIT
    with pytest.raises(ValueError, match="loopback"):
        rt.speaker.plan("L", angles_for(["hor"]), 2.0)
