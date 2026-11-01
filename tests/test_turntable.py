"""Turntable order and prompts: one revolution per plane, moves derived from the angle convention."""

import numpy as np
import pytest

from rewdio.engine.speaker_meta import PRESETS
from rewdio.engine.turntable import angles_for, move, points_from, prompt
from test_speaker_capture import _rig, _run


def _moves(pts):
    out, prev = [], None
    for p in pts:
        if prev is not None:
            out.append(move(0.0 if p.plane != prev.plane else prev.angle, p.angle))
        prev = p
    return out


def test_full_revolution_is_constant_steps_in_one_direction():
    pts = angles_for(["hor"], 10, 180)
    assert [p.angle for p in pts][:3] == [0, 10, 20] and [p.angle for p in pts][18:21] == [180, -170, -160]
    assert len(pts) == 36 and set(_moves(pts)) == {10.0}  # never a bigger move, always clockwise


def test_partial_range_jumps_back_once():
    m = _moves(angles_for(["hor"], 5, 60))
    assert m.count(5.0) == 12 and m.count(-65.0) == 1 and m.count(-5.0) == 11  # 0..+60, one jump, -5..-60


def test_soundsible_preset_moves_are_small():
    m = _moves(points_from(PRESETS["soundsible"]))
    assert max(abs(x) for x in m) <= 10 and len(points_from(PRESETS["soundsible"])) == 1 + 2 * (24 + 23)


def test_move_takes_the_short_way_round():
    assert move(170, -170) == 20 and move(-170, 170) == -20 and move(0, 180) == 180


def test_prompts_say_target_and_turn():
    pts = angles_for(["hor", "ver"], 30, 60)
    first = prompt(pts, 0, "L", 2.0, 1.2)
    assert "0 degrees" in first["text"] and "1.2 m above the ground" in first["text"]
    p1 = prompt(pts, 1, "L", 2.0, None)
    assert p1["speech"] == "Horizontal, plus 30. Turn 30 degrees clockwise. Tap ready."
    jump = prompt(pts, 3, "L", 2.0, None)  # +60 -> -30
    assert jump["move_deg"] == -90 and "anticlockwise" in jump["text"]
    vert = next(i for i, p in enumerate(pts) if p.plane == "ver")
    v = prompt(pts, vert, "L", 2.0, None)
    assert "RIGHT side" in v["text"] and v["move_deg"] == 30


async def test_simulated_rotation_follows_the_convention(sim_runtime, tmp_path):
    """A source on the speaker's left (+y) gets closer to the mic at a positive horizontal angle."""
    rt = sim_runtime()
    _rig(rt)
    w = rt.world
    w.speaker("LH").pos = (0.70, 2.10, 1.0)  # horn 10 cm to the speaker's left of the woofer, same height
    await rt.rig.assert_baseline()
    await _run(rt, angles_for(["hor"], 30, 30))
    rt.speaker.export(tmp_path)
    from rewdio.analysis.frd import read_frd
    phase = {}
    for a in (30, -30):
        f, hw = read_frd(tmp_path / "frd" / "LW" / f"L_LW hor {a}.frd")
        _, hh = read_frd(tmp_path / "frd" / "LH" / f"L_LH hor {a}.frd")
        band = (f > 1000) & (f < 3000)
        ph = np.unwrap(np.angle((hh / hw)[band]))
        phase[a] = -np.polyfit(f[band], ph, 1)[0] / (2 * np.pi) * 1000  # horn lag behind woofer, ms
    assert phase[30] < 0 < phase[-30]  # +30: the horn (on the left) arrives first
    assert phase[30] == pytest.approx(-0.10 * np.sin(np.deg2rad(30)) / 0.343, rel=0.15)
