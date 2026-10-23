"""Named, dated DSP setups: autosave, save, recall for A/B, rename, delete."""

import re

from starlette.testclient import TestClient

from conftest import drive
from rewdio.dashboard.app import build_app
from rewdio.dashboard.remote import token_for
from rewdio.engine import dsp_setups


def _on_device(rt, role):
    return rt.world.dsp[rt.session.config.channel(role).dsp_output]


async def test_ab_between_calibrated_and_flat(sim_runtime):
    rt = sim_runtime()
    await drive(rt)
    s = rt.session
    [cal] = dsp_setups.list_all(s)  # the finished calibration saved itself
    assert cal["name"] == "Calibrated" and cal["active"] and cal["source"] == "autopilot"
    assert re.match(r"^\d{4}-\d{2}-\d{2}_\d{4}_calibrated$", cal["id"])
    assert cal["curve"] and cal["bass_std_db"] is not None  # thumbnail from the verify sweep

    await rt.rig.assert_baseline()
    flat = dsp_setups.save(s, "Flat")
    assert flat["ok"] and flat["curve"] is None  # never swept like this, so no thumbnail
    assert [d["active"] for d in dsp_setups.list_all(s) if d["name"] == "Flat"] == [True]

    sub_gain = cal["dsp"]["outputs"]["SUB1"]["gain_db"]
    assert (await dsp_setups.recall(rt.rig, "calibrated"))["ok"]  # by name, any case
    assert abs(_on_device(rt, "SUB1").gain_db - sub_gain) < 0.01
    assert not any(d.mute for d in rt.world.dsp.values())  # recalled to listen: everything plays
    active = {d["name"]: d["active"] for d in dsp_setups.list_all(s)}
    assert active == {"Calibrated": True, "Flat": False}

    assert (await dsp_setups.recall(rt.rig, flat["id"]))["ok"]
    assert _on_device(rt, "SUB1").gain_db == 0.0 and not _on_device(rt, "SUB1").peq

    r = dsp_setups.rename(s, flat["id"], "Flat, no EQ")
    assert r["ok"] and r["id"].endswith("_flat-no-eq")
    assert dsp_setups.find(s, "Flat, no EQ")["id"] == r["id"]
    assert dsp_setups.delete(s, r["id"])["ok"]
    assert [d["name"] for d in dsp_setups.list_all(s)] == ["Calibrated"]


def test_setup_ids_cannot_escape_the_folder(sim_runtime):
    rt = sim_runtime()
    for bad in ("../config", "/etc/passwd", "2026-01-01_0000_../../x"):
        assert not dsp_setups.delete(rt.session, bad)["ok"]
    assert not dsp_setups.save(rt.session, "   ")["ok"]


def test_setups_api(sim_runtime):
    rt = sim_runtime()
    with TestClient(build_app(lambda: rt)) as c:
        assert c.post("/api/setups", json={"op": "save", "name": "A"}).status_code == 401
        c.headers.update({"x-rewdio-token": token_for(rt)})
        a = c.post("/api/setups", json={"op": "save", "name": "A"}).json()
        assert a["ok"]
        assert [d["name"] for d in c.get("/api/setups").json()] == ["A"]
        assert c.post("/api/setups", json={"op": "recall", "id": a["id"]}).json()["ok"]
        assert c.post("/api/setups", json={"op": "nope"}).status_code == 400
        assert c.post("/api/setups", json={"op": "delete", "id": a["id"]}).json()["ok"]
