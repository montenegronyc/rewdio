"""FRD export maths: absolute-time gating keeps the relative phase between drivers."""

import numpy as np
import pytest

from rewdio.analysis.frd import FrdFile, Gate, arrival_s, gated_response, read_frd, remove_protection, vituix_name
from rewdio.minidsp.biquad import cascade_response, crossover_sections

FS = 48000.0


def _impulse(delay_s: float, n: int = 4800, refl_s: float | None = None) -> np.ndarray:
    """Band-limited impulse at `delay_s` (fractional), optionally with a reflection."""
    t = np.arange(n) / FS
    x = np.sinc((t - delay_s) * FS) * np.hanning(n)
    if refl_s is not None:
        x += 0.5 * np.sinc((t - refl_s) * FS)
    return x


def test_arrival_is_found_in_absolute_time():
    # the onset is where the envelope rises, a few samples ahead of the peak
    assert arrival_s(_impulse(0.0030), FS, t0_s=0.0) == pytest.approx(0.0030, abs=1e-4)
    assert arrival_s(_impulse(0.0030), FS, t0_s=-0.001) == pytest.approx(0.0020, abs=1e-4)


def test_relative_delay_between_drivers_survives_export():
    f = np.geomspace(300, 15000, 200)
    gate = Gate(pre_ms=1.0, gate_ms=4.0)
    t_ref = 0.0030
    woofer = gated_response(_impulse(0.0030), FS, f, t_ref, gate)
    horn = gated_response(_impulse(0.0032), FS, f, t_ref, gate)  # 0.2 ms (~69 mm) later
    # each file keeps its own residual delay relative to the ONE common delay removed
    assert np.allclose(np.angle(woofer), 0, atol=0.05)
    expected = np.exp(-2j * np.pi * f * 0.0002)
    assert np.allclose(horn / np.abs(horn), expected, atol=0.05)


def test_gate_excludes_a_late_reflection():
    f = np.geomspace(500, 10000, 100)
    clean = gated_response(_impulse(0.0030), FS, f, 0.0030, Gate(gate_ms=4.0))
    dirty = gated_response(_impulse(0.0030, refl_s=0.0090), FS, f, 0.0030, Gate(gate_ms=4.0))
    assert np.max(np.abs(20 * np.log10(np.abs(dirty) / np.abs(clean)))) < 0.1


def test_gate_outside_the_impulse_is_an_error():
    with pytest.raises(ValueError):
        gated_response(_impulse(0.003), FS, np.array([1000.0]), t_ref_s=0.2, gate=Gate())


def test_protection_filter_is_divided_out_above_its_floor():
    f = np.geomspace(100, 20000, 300)
    p = cascade_response(crossover_sections("HP", "BW2", 700, 96000), f, 96000)
    raw = np.ones(f.size, complex)
    fixed, first_ok = remove_protection(raw * p, p)
    ok = slice(first_ok, None)
    assert np.allclose(fixed[ok], 1.0, atol=1e-9)
    assert np.all(np.abs(fixed[:first_ok]) < 1.0)  # below the floor: not blown up


def test_frd_roundtrip_and_names(tmp_path):
    f = np.geomspace(20, 20000, 50)
    h = 0.5 * np.exp(-2j * np.pi * f * 1e-4)
    p = FrdFile(tmp_path / vituix_name("L", "LH", "hor", -30), f, h, ["test"]).write()
    assert p.name == "L_LH hor -30.frd"
    f2, h2 = read_frd(p)
    assert np.allclose(f2, f, rtol=1e-6) and np.allclose(h2, h, atol=1e-4)
    assert vituix_name("L", "LW", "ver", 190) == "L_LW ver -170.frd"
    assert vituix_name("L", "LW", "hor", 180) == "L_LW hor 180.frd"
