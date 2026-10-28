"""Spinorama maths on synthetic polar sets."""

import numpy as np
import pytest

from rewdio.analysis.spin import complete, evaluate, power_weights, spinorama

F = np.geomspace(100, 20000, 200)


def _omni():
    return {(p, float(a)): np.ones(F.size) for p in ("hor", "ver") for a in range(-170, 190, 10)}


def test_omni_source_has_identical_curves_and_zero_directivity():
    s = spinorama(_omni())
    c = s["curves"]
    for k in ("on_axis", "lw", "er", "sp", "pir"):
        assert np.allclose(c[k], 0, atol=1e-9)
    assert np.allclose(c["spdi"], 0, atol=1e-9) and not s["notes"]


def test_sound_power_weights_match_the_sphere():
    keys = [(p, float(a)) for p in ("hor", "ver") for a in range(-170, 190, 10)]
    w = power_weights(complete({k: np.ones(1) for k in keys}))
    on, side = w[("hor", 0.0)], w[("hor", 90.0)]
    assert side / on == pytest.approx((np.cos(np.deg2rad(85)) - np.cos(np.deg2rad(95))) / 4
                                      / (1 - np.cos(np.deg2rad(5))), rel=1e-6)
    assert sum(w.values()) == pytest.approx(1.0)


def test_beaming_source_has_positive_directivity_and_lw_above_sp():
    # a cardioid-ish source that narrows with frequency
    pol = {}
    for p in ("hor", "ver"):
        for a in range(-170, 190, 10):
            k = np.clip(F / 2000, 0, 4)
            pol[(p, float(a))] = ((1 + np.cos(np.deg2rad(a))) / 2) ** k
    c = spinorama(pol)["curves"]
    assert np.all(c["spdi"] >= -1e-9) and c["spdi"][-1] > 6
    assert np.all(c["lw"] >= c["sp"] - 1e-9)


def test_one_sided_horizontal_set_is_mirrored_and_flagged():
    pol = {("hor", float(a)): np.ones(F.size) for a in range(0, 190, 10)}
    s = spinorama(pol)
    assert ("hor", -30.0) in complete(pol)
    assert any("horizontal plane only" in n for n in s["notes"])
    assert any("floor" in n for n in s["notes"])


def test_targets_pass_for_flat_lw_and_falling_pir():
    lw = np.zeros(F.size)
    curves = {"lw": lw, "pir": -0.8 * np.log2(F / 1000), "spdi": 0.8 * np.log2(F / 100)}
    ev = evaluate(F, curves)
    assert ev["ok"] and ev["pir"]["slope_db_per_oct"] == pytest.approx(-0.8, abs=0.01)
    curves["lw"] = 3 * np.sin(np.log2(F))  # wavy LW fails
    assert not evaluate(F, curves)["lw"]["ok"]
