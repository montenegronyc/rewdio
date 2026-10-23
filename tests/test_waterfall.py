"""Waterfall (cumulative spectral decay) from the full-system sweep's stored impulse."""

import numpy as np
from starlette.testclient import TestClient

from conftest import drive
from rewdio.dashboard.app import build_app


async def test_waterfall_decays_and_is_served(sim_runtime):
    rt = sim_runtime()
    await drive(rt)
    w = TestClient(build_app(lambda: rt)).get("/api/waterfall?seat=MLP").json()
    db = np.array(w["db"])
    cfg = rt.session.config.dashboard.waterfall
    assert w["measurement"] and db.shape == (cfg.slices, len(w["freqs"]))
    assert db.max() == 0.0 and db.min() >= cfg.floor_db
    level = db.mean(axis=1)
    assert level[0] > level[len(level) // 2] > cfg.floor_db - 1  # sound dies away over time
    assert w["times_ms"][1] == cfg.step_ms


def test_waterfall_without_a_sweep_is_empty(sim_runtime):
    rt = sim_runtime()
    assert TestClient(build_app(lambda: rt)).get("/api/waterfall").json()["measurement"] is None
