import math

import numpy as np

from rewdio.minidsp.biquad import Biquad, cascade_response, design, linkwitz_riley4
from rewdio.minidsp.client import MiniDSPClient
from rewdio.rew.codec import decode_floats, encode_floats, parse_rew_json


def test_float_codec_roundtrip_is_big_endian():
    vals = [1.5, -2.25, 1000.0]
    b64 = encode_floats(vals)
    assert np.allclose(decode_floats(b64), vals)
    # big-endian 1.5 = 0x3FC00000
    import base64
    assert base64.b64decode(encode_floats([1.5])) == bytes([0x3F, 0xC0, 0, 0])


def test_parse_rew_json_handles_bare_nan_outside_strings():
    d = parse_rew_json('{"spl": NaN, "x": -Infinity, "s": "NaN stays"}')
    assert d == {"spl": None, "x": None, "s": "NaN stays"}


def test_peaking_filter_gain_at_centre():
    fs = 96000
    bq = design("PK", 50, fs, gain_db=-6, q=4)
    assert math.isclose(20 * np.log10(abs(bq.response(np.array([50.0]), fs)[0])), -6, abs_tol=0.01)
    assert abs(20 * np.log10(abs(bq.response(np.array([5000.0]), fs)[0]))) < 0.01


def test_lr4_pair_sums_flat():
    fs, f = 96000, np.geomspace(10, 2000, 400)
    s = cascade_response(linkwitz_riley4("LP", 80, fs), f, fs) + cascade_response(linkwitz_riley4("HP", 80, fs), f, fs)
    assert np.max(np.abs(20 * np.log10(np.abs(s)))) < 0.01


def test_minidsp_sign_convention_and_patch():
    bq = design("PK", 40, 96000, gain_db=-3, q=2)
    md = bq.to_minidsp()
    assert md["a1"] == -bq.a1 and md["a1"] > 1.9  # miniDSP a1 is positive near DC
    assert Biquad.from_minidsp(md) == bq
    patch = MiniDSPClient.output_patch(2, gain_db=-1.234, delay_ms=1.5, mute=False, invert=True, peq={5: bq, 6: None})
    assert patch["delay"] == {"secs": 0, "nanos": 1_500_000}
    assert patch["gain"] == -1.23
    assert patch["peq"][1] == {"index": 6, "bypass": True}
