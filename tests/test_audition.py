"""All-pass and explicit-Q crossover filters, capture through the crossover, and raw captures that are
really raw (the output's own gain, delay and polarity divided out)."""

import json

import numpy as np
import pytest
from pydantic import ValidationError

from rewdio.analysis.frd import read_frd
from rewdio.engine.speaker import angles_for
from rewdio.minidsp.biquad import cascade_response, design
from rewdio.minidsp.state import XoverFilter, xover_sections
from test_speaker_capture import LH, LW, _delay, _rig, _run

FS = 96000


def test_allpass_is_flat_and_turns_180_degrees_at_f0():
    f = np.geomspace(20, 20000, 300)
    h = design("AP", 1381.0, FS, q=0.8055).response(f, FS)
    assert np.allclose(np.abs(h), 1.0, atol=1e-9)
    h0 = design("AP", 1381.0, FS, q=0.8055).response(np.array([1381.0]), FS)[0]
    assert abs(abs(np.degrees(np.angle(h0))) - 180) < 0.01


def test_explicit_q_filters_match_soundsibles_audition_target():
    lp = XoverFilter(kind="LP", freq_hz=889.7, qs=[0.7592, 0.7592])
    ap = XoverFilter.model_validate({"kind": "AP", "freq_hz": 1381.0, "qs": [0.8055]})
    assert lp.topology is None and lp.label() == "LP 889.7 Hz Q 0.7592/0.7592"
    secs = xover_sections([lp, ap, XoverFilter(kind="AP", freq_hz=1231.8, qs=[0.5219])], FS)
    assert len(secs) == 4  # fits one output's crossover block with room to spare
    want = cascade_response([design("LP", 889.7, FS, q=0.7592)] * 2 + [design("AP", 1381.0, FS, q=0.8055),
                                                                        design("AP", 1231.8, FS, q=0.5219)],
                            np.geomspace(100, 10000, 50), FS)
    assert np.allclose(cascade_response(secs, np.geomspace(100, 10000, 50), FS), want)
    with pytest.raises(ValidationError, match="needs qs"):
        XoverFilter(kind="AP", freq_hz=1000)
    with pytest.raises(ValidationError):
        XoverFilter(kind="LP", freq_hz=1000, qs=[])
    assert XoverFilter(kind="HP", freq_hz=800).topology == "LR4"  # presets still the default


async def test_raw_captures_divide_out_the_outputs_gain_delay_and_polarity(sim_runtime, tmp_path):
    rt = sim_runtime()
    _rig(rt)
    await rt.rig.assert_baseline()
    await rt.rig.set_channel("LW", delay_ms=0.8)
    await rt.rig.set_channel("LH", gain_db=-3.0, invert=True)
    await _run(rt, angles_for(["hor"], 30, 30))
    rt.speaker.export(tmp_path)
    mic = (2.70, 2.0, 1.125)
    f, hw = read_frd(tmp_path / "frd" / "LW" / "L_LW hor 0.frd")
    _, hh = read_frd(tmp_path / "frd" / "LH" / "L_LH hor 0.frd")
    band = (f > 900) & (f < 5000)
    ratio = (hh / hw)[band] * np.exp(2j * np.pi * f[band] * (_delay(LH, mic) - _delay(LW, mic)))
    assert np.max(np.abs(np.angle(ratio))) < np.deg2rad(5)  # DSP delay and polarity gone: only geometry
    assert np.allclose(np.abs(ratio), np.linalg.norm(np.subtract(LW, mic)) / np.linalg.norm(np.subtract(LH, mic)),
                       rtol=0.05)  # and the -3 dB pad


async def test_through_crossover_and_summed_captures(sim_runtime, tmp_path):
    rt = sim_runtime()
    _rig(rt)
    await rt.rig.assert_baseline()
    rt.speaker.plan("L", angles_for(["hor"], 30, 30), 2.0, modes=["raw", "xo", "sum"])
    await _run(rt)
    res = rt.speaker.export(tmp_path)
    assert res["files"] == 3 * 3 + 3 * 2  # raw + xo per driver, plus one sum, at 3 angles
    m = json.loads((tmp_path / "rewdio_session.json").read_text())
    lw = m["drivers"][0]
    assert all(e["mode"] == "raw" for e in lw["files"]) and lw["files_xo"][0]["file"].startswith("frd/LW_xo/")
    assert m["sum_files"][0]["file"].startswith("frd/sum/") and m["capture_modes"] == ["raw", "xo", "sum"]
    # xo = raw x the configured crossover (protection divided out of both)
    f, raw = read_frd(tmp_path / "frd" / "LH" / "L_LH hor 0.frd")
    _, xo = read_frd(tmp_path / "frd" / "LH_xo" / "L_LH_xo hor 0.frd")
    xo_filters = [x for x in rt.session.config.channel("LH").crossover if not x.protect]
    pred = cascade_response(xover_sections(xo_filters, FS), f, FS)
    band = (f > 1500) & (f < 5000)
    err = (xo / raw)[band] / pred[band]
    assert np.max(np.abs(20 * np.log10(np.abs(err)))) < 0.5 and np.max(np.abs(np.angle(err))) < np.deg2rad(5)
    rep = rt.speaker.report()  # the spinorama still uses the raw captures only
    assert "curves_db" in rep
