"""Production QA: a tower set held to a reference set, check by check, with tolerances as data."""

import json

import numpy as np
import pytest

from rewdio.engine import qa
from rewdio.engine.turntable import angles_for
from test_speaker_capture import _rig, _run


async def _full_set(rt, out, serial, reference=False):
    rt.speaker.plan("L", angles_for(["hor"], 30, 30), 2.0, replace=True, modes=["raw", "sum"])
    rt.speaker.update(serial=serial, reference=reference, parts={"C1_uF": 22.0})
    await _run(rt)
    for d in ("LW", "LH"):
        await rt.speaker.impedance(d)
    await rt.speaker.distortion("LH", [80.0])
    await rt.speaker.transfer("amp", "horn", "LH")
    await rt.speaker.transfer("tap", "horn", "LH")
    await rt.speaker.soak("LH", minutes=0.001, spl_db=80.0)
    await rt.speaker.impedance("LH", label="post_soak")
    rt.speaker.export(out)
    return out


@pytest.fixture
async def sets(sim_runtime, tmp_path):
    rt = sim_runtime()
    _rig(rt)
    # the simulator has no electrical tap: its "transfer" sweeps are acoustic, so take the noise out to stand in
    # for the stable voltage ratio a real tap gives
    rt.world.noise_floor_spl = -60.0
    await rt.rig.assert_baseline()
    ref = await _full_set(rt, tmp_path / "ref", "T-0001", reference=True)
    cand = await _full_set(rt, tmp_path / "cand", "T-0002")
    return rt, ref, cand


async def test_an_identical_tower_passes_every_check(sets, tmp_path):
    rt, ref, cand = sets
    m = json.loads((cand / "rewdio_session.json").read_text())
    assert m["schema"] == "rewdio.speaker-session/3" and m["serial"] == "T-0002" and m["parts"] == {"C1_uF": 22.0}
    assert m["transfer"][0]["file"] == "transfer/L_horn.frd" and m["soak"][0]["completed"]
    rep = qa.run(cand, ref, pair=ref, home=tmp_path)
    by = {(r["check"], r["subject"]) for r in rep["results"]}
    assert {("impedance", "LW"), ("impedance", "LH"), ("transfer", "horn"), ("polarity", "LW"), ("frd", "sum"),
            ("distortion", "LH @80 dB"), ("soak", "LH"), ("pair", "LW/LW")} <= by
    assert rep["pass"], [r for r in rep["results"] if r["pass"] is False]
    assert json.loads((cand / "rewdio_session.json").read_text())["qa"]["pass"] is True
    assert (cand / "qa_report.json").exists()


def _scale_zma(path, k):
    out = []
    for ln in path.read_text().splitlines():
        if ln.startswith("*"):
            out.append(ln)
        else:
            f, o, p = ln.split("\t")
            out.append(f"{f}\t{float(o) * k:.4f}\t{p}")
    path.write_text("\n".join(out) + "\n")


async def test_faults_are_caught_with_the_worst_frequency(sets, tmp_path):
    rt, ref, cand = sets
    _scale_zma(cand / "zma" / "L_LW.zma", 1.10)  # a wrong part: impedance 10 % high everywhere
    frd = cand / "frd" / "LH" / "L_LH hor 0.frd"  # a driver wired backwards
    lines = [ln if ln.startswith("*") else "\t".join([*ln.split("\t")[:2], f"{float(ln.split(chr(9))[2]) + 180:.4f}"])
             for ln in frd.read_text().splitlines()]
    frd.write_text("\n".join(lines) + "\n")
    rep = qa.run(cand, ref, home=tmp_path)
    failed = set(rep["failed"])
    assert "impedance LW" in failed and "polarity LH" in failed and not rep["pass"]
    imp = next(r for r in rep["results"] if r["check"] == "impedance" and r["subject"] == "LW")
    assert imp["margin"] == pytest.approx(-5.0, abs=0.1) and imp["worst_hz"] is not None


async def test_tolerances_are_data_lee_can_change(sets, tmp_path):
    rt, ref, cand = sets
    _scale_zma(cand / "zma" / "L_LW.zma", 1.10)
    (tmp_path / "qa_tolerances.json").write_text(json.dumps({"impedance": {"max_dev_pct": 15.0}}))
    rep = qa.run(cand, ref, home=tmp_path)
    assert "impedance LW" not in rep["failed"]
    assert rep["tolerances"]["impedance"]["min_ohm"] == 4.0  # the rest of the defaults stay


async def test_reference_must_be_marked_and_can_be_found(sets, tmp_path):
    rt, ref, cand = sets
    with pytest.raises(ValueError, match="not marked as a reference"):
        qa.run(ref, cand, home=tmp_path)
    (tmp_path / "measurements").mkdir()
    ref.rename(tmp_path / "measurements" / "ref")
    assert qa.find_reference(tmp_path) == tmp_path / "measurements" / "ref"


async def test_soak_refuses_above_the_listening_ceiling_or_without_a_level(sim_runtime):
    rt = sim_runtime()
    _rig(rt)
    await rt.rig.assert_baseline()
    rt.speaker.plan("L", angles_for(["hor"], 90, 90, symmetric=True), 2.0)
    with pytest.raises(ValueError, match="distortion"):
        rt.speaker.soak_level("LH", 85.0)
    with pytest.raises(ValueError, match="ceiling"):
        rt.speaker.soak_level("LH", 96.0)
