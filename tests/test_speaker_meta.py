"""Capture-set metadata for SoundSible: geometry frame, environment, angle plans, variants, folder layout."""

import json

import pytest

from rewdio.engine.speaker import points_from
from rewdio.engine.speaker_meta import PRESETS, AnglePlan, Environment, Geometry, set_folder
from test_speaker_capture import _rig, _run


def test_mic_from_distance_and_heights_lands_in_the_frame():
    g = Geometry.from_inputs(pivot_xyz=(0.1, 0.0, -0.2), mic_distance_m=2.0, mic_height_m=1.1, origin_height_m=0.9)
    assert g.mic_xyz == pytest.approx((2.1, 0.0, 0.2))
    assert g.mic_given_as["origin_height_m"] == 0.9
    assert g.mic_distance() == pytest.approx(((2.0) ** 2 + 0.4 ** 2) ** 0.5)
    with pytest.raises(ValueError):
        Geometry.from_inputs(mic_distance_m=2.0, mic_height_m=1.1)
    assert Geometry.from_inputs().pivot_xyz == (0.0, 0.0, 0.0)  # SoundSible's default pivot: the frame origin


def test_ground_bounce_time():
    g = Geometry.from_inputs(mic_xyz=(1.778, 0.0, 0.0), origin_height_m=1.0)
    assert g.mic_height_m == 1.0
    want = ((1.778 ** 2 + 2.0 ** 2) ** 0.5 - 1.778) / 343.0 * 1000
    assert g.ground_bounce_ms(343.0) == pytest.approx(want)
    with pytest.raises(ValueError):
        Geometry(horn_offset_m=0.5)  # rails stop at 0.30 m


def test_soundsible_preset_and_explicit_lists():
    pts = points_from(PRESETS["soundsible"])
    hor = sorted(p.angle for p in pts if p.plane == "hor")
    assert hor[0] == -170.0 and hor[-1] == 180.0 and 0.0 in hor and 55.0 in hor and 65.0 not in hor
    per_side = 12 + 12  # 5..60 by 5, 70..180 by 10
    assert len(pts) == 1 + 2 * (2 * per_side - 1)  # 0 once; both signs except 180
    assert [(p.plane, p.angle) for p in points_from(AnglePlan(hor=[15, 45], both_signs=False))] == \
        [("hor", 0.0), ("hor", 15.0), ("hor", 45.0)]
    with pytest.raises(ValueError):
        AnglePlan(hor=[200])


def test_folder_name_is_date_site_variant_in_et():
    env = Environment(site="Prospect Park", started_et="2026-10-28T10:05:00-04:00")
    assert set_folder(env, "offset10 riser40") == "2026-10-28_Prospect-Park_offset10-riser40"
    assert Environment(temperature_c=0).speed_of_sound == pytest.approx(331.3)


async def test_manifest_v2_carries_geometry_environment_and_layout(sim_runtime, tmp_path):
    rt = sim_runtime()
    _rig(rt)
    await rt.rig.assert_baseline()
    geo = Geometry.from_inputs(pivot_xyz=(0.0, 0.0, 0.0), mic_xyz=(2.0, 0.0, 0.0), riser_h=0.4, horn_offset_m=0.1,
                               horn_id="9756570e77f12f08")
    env = Environment(temperature_c=12.5, indoor=False, site="garden", started_et="2026-10-28T10:05:00-04:00")
    from rewdio.engine.speaker import angles_for
    rt.speaker.plan("L", angles_for(["hor"], 90, 90, symmetric=True), geometry=geo, environment=env,
                    variant="offset10_riser40")
    st = rt.speaker.state()
    assert st.distance_m == pytest.approx(2.0)
    await _run(rt)
    res = rt.speaker.export(root=tmp_path)
    out = tmp_path / "measurements" / "2026-10-28_garden_offset10_riser40"
    assert res["dir"] == str(out)
    m = json.loads((out / "rewdio_session.json").read_text())
    assert m["schema"] == "rewdio.speaker-session/2" and m["variant"] == "offset10_riser40"
    assert m["geometry"]["horn_id"] == "9756570e77f12f08" and m["geometry"]["riser_h"] == 0.4
    assert m["geometry"]["pivot_xyz"] == [0.0, 0.0, 0.0] and "y to the speaker's left" in m["geometry"]["frame"]
    assert m["environment"]["temperature_c"] == 12.5 and m["environment"]["indoor"] is False
    f0 = m["drivers"][0]["files"][0]
    assert f0["file"].startswith("frd/LW/") and (out / f0["file"]).exists()
    assert "valid_band_hz" in f0
    assert not any("not recorded" in n and "horn_id" in n for n in m["notes"])


async def test_replanning_over_unexported_captures_is_refused(sim_runtime):
    rt = sim_runtime()
    _rig(rt)
    await rt.rig.assert_baseline()
    from rewdio.engine.speaker import angles_for
    await _run(rt, angles_for(["hor"], 90, 90, symmetric=True))
    with pytest.raises(ValueError, match="never exported"):
        rt.speaker.plan("L", angles_for(["hor"]), 2.0)
    rt.speaker.plan("L", angles_for(["hor"]), 2.0, replace=True)
