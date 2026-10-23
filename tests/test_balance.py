import numpy as np

from conftest import drive
from rewdio.analysis.balance import solve_balance
from rewdio.analysis.gating import direct_segment, gated_mag
from rewdio.analysis.response import Response, log_grid
from rewdio.minidsp.biquad import design

FS = 96000
F = log_grid(96, 10, 20000)


def test_balance_recovers_broadband_and_band_imbalance(cfg):
    left = Response(F, np.ones(F.size, complex))
    bump = design("PK", 900, FS, gain_db=2.0, q=1.2).response(F, FS)
    right = Response(F, 10 ** (0.8 / 20) * bump)
    res = solve_balance([left], [right], FS, cfg.balance, (250.0, 5000.0))
    assert res.after["std_db"] < 0.15 and res.before["std_db"] > 0.4
    fc, g, q = res.filters[0]
    assert abs(fc - 900) < 120 and -2.6 < g < -1.4  # R-L moved down where R was hot
    r_peq, l_peq = res.peq("R"), res.peq("L")
    assert r_peq[0].gain_db == -l_peq[0].gain_db  # split half and half, mirror images
    assert all(abs(p.gain_db) <= cfg.balance.max_gain_db_per_side for p in r_peq + l_peq)


def test_gate_closes_before_the_first_reflection(cfg):
    fs = 48000
    ir = np.zeros(4800)
    ir[1000] = 1.0
    ir[1000 + int(0.0021 * fs)] = 0.6  # floor bounce 2.1 ms later
    seg = direct_segment(ir, fs, cfg.balance, cfg.tuning.alignment.arrival_onset_fraction)
    assert 1.5 <= seg.max_gate_ms <= 2.2
    mag = gated_mag(seg, np.array([1000.0, 3000.0, 8000.0]), seg.max_gate_ms, cfg.balance)
    assert np.ptp(mag) < 1.0  # the comb from the bounce is gone


async def test_autopilot_balances_left_and_right(sim_runtime):
    rt = sim_runtime()
    await drive(rt)
    lp = rt.session.workflow.last_proposal
    assert lp["measured"]["mains_level_spread_db"] <= rt.session.config.convergence.lr_level_tolerance_db
    assert lp["measured"]["lr_balance"]["std_db"] <= rt.session.config.convergence.lr_balance_max_std_db
    assert any(n.startswith("L/R balance") for n in lp["notes"])
    # the scenario's right main is ~1 dB hot in the gated band: it must be the one trimmed
    assert rt.world.dsp[1].gain_db < rt.world.dsp[0].gain_db
