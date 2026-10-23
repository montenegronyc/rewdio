import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from conftest import drive
from rewdio.config import load_config

SRC = Path(__file__).resolve().parents[1] / "src" / "rewdio"


def test_user_file_overrides_defaults_and_typos_fail(tmp_path):
    f = tmp_path / "config.json"
    f.write_text(json.dumps({"crossover_hz": 90, "tuning": {"peq": {"max_cut_db": 9}}}))
    cfg = load_config(user_file=f)
    assert cfg.crossover_hz == 90 and cfg.tuning.peq.max_cut_db == 9
    assert cfg.tuning.peq.max_q == 12.0  # untouched keys keep their defaults
    f.write_text(json.dumps({"crossovr_hz": 90}))
    with pytest.raises(ValidationError):
        load_config(user_file=f)


def test_every_module_stays_under_500_lines():
    big = {str(p.relative_to(SRC)): n for p in SRC.rglob("*.py") if (n := len(p.read_text().splitlines())) >= 500}
    assert not big, big


async def test_dead_channel_is_caught_before_solving(sim_runtime):
    rt = sim_runtime()
    rt.world.speaker("SUB2").sensitivity_db = 40.0  # amp off / cable out
    for _ in range(10):
        rep = await rt.autopilot.step()
        if rep["status"] != "user_action_required" or rep["action"]["key"].startswith("quality:"):
            break
        rt.apply_sim_effects(rep["action"], None)
        rt.autopilot.confirm()
    assert rep["action"]["key"].startswith("quality:")
    assert "SUB2" in rep["action"]["instructions"]
    codes = {f["code"] for f in rep["action"]["findings"]}
    assert "silent_channel" in codes
    # user fixes it; the loop re-measures and finishes
    rt.world.speaker("SUB2").sensitivity_db = 118.0
    rt.autopilot.confirm()
    assert (await drive(rt))["status"] == "done"


async def test_report_has_diagnostics_and_rew_crosschecks_in_loopback(sim_runtime):
    from rewdio.config import TimingMode
    rt = sim_runtime(timing_mode=TimingMode.LOOPBACK)
    await drive(rt)
    lp = rt.session.workflow.last_proposal
    diag = lp["diagnostics"]
    assert "axial_modes_hz" in diag and "residual_peaks" in diag
    assert "alignment_tool" in lp["crosscheck"] and "error" not in lp["crosscheck"]["alignment_tool"]
    if lp["used_peq"]:
        assert lp["crosscheck"]["rew_eq_prediction"]["agree"]


async def test_dashboard_serves_tokens_and_state(sim_runtime):
    from starlette.testclient import TestClient

    from rewdio.dashboard.app import build_app
    from rewdio.dashboard.tokens import load_tokens, tokens_css
    rt = sim_runtime()
    await drive(rt)
    client = TestClient(build_app(lambda: rt.session))
    css = client.get("/static/tokens.css").text
    for name in load_tokens()["color"]["light"]:
        assert f"--color-{name}:" in css
    assert "prefers-color-scheme: dark" in css and css == tokens_css()
    styles = client.get("/static/styles.css").text
    import re
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b", styles), "styles.css must only use token variables"
    st = client.get("/api/state").json()
    assert st["done"] and st["curves"]["MLP"]["measured"] and set(st["dsp"]) == {"L", "R", "SUB1", "SUB2"}
    assert client.get("/").status_code == 200


async def test_loopback_check_passes_when_wired_and_stops_when_unplugged(sim_runtime):
    from rewdio.config import TimingMode
    rt = sim_runtime(timing_mode=TimingMode.LOOPBACK)
    rep = await rt.autopilot.step()
    assert any("loopback signal present" in line for line in rep["log"])

    rt2 = sim_runtime(timing_mode=TimingMode.LOOPBACK)
    rt2.fake_rew.loopback_connected = False  # splitter not plugged in
    rep = await rt2.autopilot.step()
    assert rep["status"] == "user_action_required" and rep["action"]["key"].startswith("loopback_missing")
    assert "Y-splitter" in rep["action"]["instructions"]
    assert all(o.mute for o in rt2.world.dsp.values())  # the test noise never reached the speakers
    rt2.fake_rew.loopback_connected = True
    rt2.autopilot.confirm()
    assert (await drive(rt2))["status"] == "done"


def test_example_rig_configs_are_valid():
    from rewdio.config import load_config
    for f in (Path(__file__).resolve().parents[1] / "examples" / "configs").glob("*.json"):
        load_config(user_file=f)
