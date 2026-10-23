import numpy as np

from conftest import drive
from rewdio.analysis.response import Response, log_grid
from rewdio.analysis.solver import Measured, footprint, peq_earns_place, solve
from rewdio.minidsp.biquad import cascade_response, design, linkwitz_riley4
from rewdio.minidsp.state import DspState, OutputState

FS = 96000
F = log_grid(96, 10, 20000)


def rig(mode_db: float) -> dict:
    hp = cascade_response(linkwitz_riley4("HP", 80, FS), F, FS)
    lp = cascade_response(linkwitz_riley4("LP", 80, FS), F, FS)
    mode = design("PK", 45, FS, gain_db=mode_db, q=6).response(F, FS)
    lvl = 10 ** (75 / 20)
    sub = Response(F, 2 * lvl * lp * mode * 10 ** (4 / 20)).delayed(10.0)
    main = Response(F, lvl * hp).delayed(10.0)
    return {"MLP": {"L": Measured(main, OutputState()), "R": Measured(main, OutputState()),
                    "SUB1": Measured(sub, OutputState()), "SUB2": Measured(sub.scaled(1e-3), OutputState())}}


def test_strong_mode_earns_a_light_cut(cfg):
    data = rig(12.0)
    plain = solve(cfg, data, DspState(), use_peq=False)
    eq = solve(cfg, data, DspState(), use_peq=True)
    ok, why = peq_earns_place(cfg, plain, eq)
    assert ok, why
    fp = footprint(cfg, eq.state)
    assert 1 <= fp["filters"] // 2 <= cfg.tuning.peq.max_filters  # same filters on both sub outputs
    assert fp["max_cut_db"] >= -cfg.tuning.peq.max_cut_db and fp["max_boost_db"] <= 0.0  # light, cut-only


def test_mild_bump_is_left_alone(cfg):
    data = rig(2.0)
    plain = solve(cfg, data, DspState(), use_peq=False)
    eq = solve(cfg, data, DspState(), use_peq=True)
    ok, why = peq_earns_place(cfg, plain, eq)
    assert not ok, why


def test_tiny_changes_are_not_written(cfg):
    data = rig(0.0)
    first = solve(cfg, data, DspState(), use_peq=False)
    nudged = DspState.model_validate(first.state.model_dump())
    for o in nudged.outputs.values():
        o.gain_db += 0.1
        o.delay_ms += 0.02
    again = solve(cfg, data, nudged, use_peq=False)
    for role, o in again.state.outputs.items():
        assert o.gain_db == nudged.get(role).gain_db and o.delay_ms == nudged.get(role).delay_ms


async def test_first_pass_never_uses_peq(sim_runtime):
    rt = sim_runtime()
    rep = await drive(rt)
    logs = rt.session.workflow.log_tail
    assert any("PEQ held back" in line for line in logs)
    first_apply = next(line for line in logs if line.startswith("applied pass 0"))
    assert "with PEQ" not in first_apply
    assert "dsp_footprint" in rt.session.workflow.last_proposal
    assert rep["status"] == "done"
