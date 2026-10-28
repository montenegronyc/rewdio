"""Impedance (ZMA) and harmonic distortion captures for the passive design, in the simulator."""

import json

import numpy as np
import pytest

from rewdio.engine.speaker import angles_for
from rewdio.rew.client import RewError
from test_speaker_capture import _rig, _run


async def _set(sim_runtime):
    rt = sim_runtime()
    _rig(rt)
    await rt.rig.assert_baseline()
    rt.speaker.plan("L", angles_for(["hor"], 90, 90, symmetric=True), 2.0)
    return rt


async def test_impedance_is_captured_and_exported_as_zma(sim_runtime, tmp_path):
    rt = await _set(sim_runtime)
    rep = await rt.speaker.impedance("LH", series_cap_uf=22.0)
    assert rep["source"] == "api" and 5 < rep["median_ohm"] < 40
    assert rep["peak_hz"] == pytest.approx(48.0, rel=0.1)  # the sim driver's resonance
    assert rt.session.dsp.get("LH").crossover == rt.session.config.channel("LH").crossover  # restored
    await _run(rt)
    rt.speaker.export(tmp_path)
    z = (tmp_path / "zma" / "L_LH.zma").read_text().splitlines()
    assert z[0].startswith("*") and "22 uF NOT in circuit" in z[1]
    f, ohm, ph = map(float, z[3].split("\t"))
    assert f > 0 and ohm > 5
    m = json.loads((tmp_path / "rewdio_session.json").read_text())
    lh = next(d for d in m["drivers"] if d["role"] == "LH")
    assert lh["zma"][0]["cap_in_circuit"] is False and lh["zma"][0]["series_cap_uf"] == 22.0


async def test_an_spl_measurement_is_never_exported_as_impedance(sim_runtime):
    rt = await _set(sim_runtime)
    rec, _ = await rt.rig.measure(["LW"], "manual", "x")
    with pytest.raises(RewError, match="not an impedance"):
        await rt.speaker.impedance("LW", rew_measurement_id=rec.id)


async def test_distortion_reaches_each_level_and_exports_csv(sim_runtime, tmp_path):
    rt = await _set(sim_runtime)
    rep = await rt.speaker.distortion("LH", [90.0, 80.0])
    assert [lv["target_spl_db"] for lv in rep["levels"]] == [80.0, 90.0]  # quietest first
    assert all(lv["reached"] for lv in rep["levels"]), rep
    assert rep["band_hz"] == [420.0, 20000.0]  # horn band: 0.6 x its 700 Hz guard, not below 400 Hz
    assert rt.rig.level_dbfs == rt.session.config.sweep.level_dbfs  # back to the normal level
    await _run(rt)
    rt.speaker.export(tmp_path)
    lo = (tmp_path / "distortion" / "L_LH_80dB.csv").read_text().splitlines()
    hi = (tmp_path / "distortion" / "L_LH_90dB.csv").read_text().splitlines()
    assert lo[3].split(",")[:3] == ["freq_hz", "fundamental_db", "thd_pct"] and "h2_pct" in lo[3]

    def thd(lines):
        rows = [r.split(",") for r in lines[4:]]
        return np.median([float(r[2]) for r in rows])
    assert thd(hi) > thd(lo)  # louder, more distortion
    assert lo[-1].endswith(",")  # a ragged REW row keeps its blank cell


async def test_distortion_above_the_listening_ceiling_or_near_the_abort_limit_is_refused(sim_runtime):
    rt = await _set(sim_runtime)
    with pytest.raises(ValueError, match="listening ceiling"):
        await rt.speaker.distortion("LH", [85.0, 100.0])
    rt.session.config.sweep.max_spl_db = 95.0  # a lower REW abort limit still wins
    with pytest.raises(ValueError, match="abort limit"):
        await rt.speaker.distortion("LH", [95.0])
