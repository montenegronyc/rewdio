import json

import pytest
from starlette.testclient import TestClient

from rewdio import settings
from rewdio.dashboard.app import build_app
from rewdio.dashboard.remote import token_for
from rewdio.engine import setup


def test_settings_file_keeps_only_changes_and_rejects_bad_values(tmp_path):
    f = tmp_path / "config.json"
    cfg = settings.save(f, {"mic_cal_path": "/cal/soundid.txt", "seat_xyz.MLP": [3.6, 2.1, 1.1],
                            "crawl_grid.spacing_m": 0.6, "@minidsp_model": "SHD"})
    data = json.loads(f.read_text())
    assert data["mic_cal_path"] == "/cal/soundid.txt" and data["crawl_grid"] == {"spacing_m": 0.6}
    assert data["dsp_sample_rate"] == 48000 and cfg.dsp_sample_rate == 48000  # model preset applied
    assert "rew_url" not in data  # defaults are not copied in
    with pytest.raises(Exception):
        settings.save(f, {"crawl_grid.spacing_m": -1})
    assert json.loads(f.read_text()) == data  # a rejected save writes nothing
    settings.save(f, {"seat_xyz.MLP": None})
    assert "seat_xyz" not in json.loads(f.read_text())
    assert settings.current_model(settings.save(f, {"@minidsp_model": "2x4HD"})) == "2x4HD"


async def test_wizard_save_reloads_the_running_session(sim_runtime):
    rt = sim_runtime()
    st = await setup.status(rt)
    assert [s["id"] for s in st["steps"]][:3] == ["connections", "minidsp", "mic"]
    res = await setup.save(rt, {"crossover_hz": 90, "timing_mode": "loopback"})
    assert res["ok"] and rt.session.config.crossover_hz == 90
    assert json.loads(rt.session.settings_path.read_text())["crossover_hz"] == 90
    bad = await setup.save(rt, {"timing_mode": "telepathy"})
    assert not bad["ok"] and rt.session.config.timing_mode.value == "loopback"
    checks = await setup.check(rt, ["rew", "minidsp", "mic_cal"])
    assert checks["rew"]["ok"] and checks["minidsp"]["ok"] and checks["mic_cal"]["ok"] is None


def test_mic_cal_file_check(tmp_path):
    good = tmp_path / "cal.txt"
    good.write_text('"Sens Factor =-1.2dB"\n' + "\n".join(f"{20 * 1.1 ** i:.1f}\t{0.1 * i:.2f}" for i in range(40)))
    assert settings.file_check(str(good))["ok"]
    assert settings.file_check(str(tmp_path / "missing.txt"))["ok"] is False


def test_setup_api_needs_the_token_to_save(sim_runtime):
    rt = sim_runtime()
    phone = TestClient(build_app(lambda: rt), client=("192.168.1.50", 50000))
    assert phone.post("/api/setup", json={"updates": {"crossover_hz": 70}}).status_code == 401
    tok = {"x-rewdio-token": token_for(rt)}
    r = phone.post("/api/setup", json={"updates": {"crossover_hz": 70}}, headers=tok)
    assert r.status_code == 200 and rt.session.config.crossover_hz == 70
    r = phone.post("/api/setup", json={"updates": {"crawl_grid.mode": "zigzag"}}, headers=tok)
    assert r.status_code == 400 and "mode" in r.json()["message"]
    assert phone.get("/api/setup", headers=tok).json()["steps"]
