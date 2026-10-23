import numpy as np

from rewdio.analysis.alignment import arrival_ms, best_alignment, fit_relative_delay, optimize_pair
from rewdio.analysis.peq import design_peq
from rewdio.analysis.response import Response, log_grid
from rewdio.minidsp.biquad import cascade_response, design, linkwitz_riley4

FS = 96000
F = log_grid(96, 10, 20000)


def xo_pair(main_ms, sub_ms):
    lp = cascade_response(linkwitz_riley4("LP", 80, FS), F, FS)
    hp = cascade_response(linkwitz_riley4("HP", 80, FS), F, FS)
    return Response(F, hp).delayed(main_ms), Response(F, lp).delayed(sub_ms)


def test_sum_fit_needs_probe_to_break_mirror_and_then_recovers_delay(cfg):
    at = cfg.tuning.alignment
    main, sub = xo_pair(10.0, 13.5)
    c0 = (main + sub).mag_db
    c1 = (main + sub.delayed(2.5)).mag_db
    m_pa, s_pa = main.delayed(-10.0), sub.delayed(-13.5)  # REW peak alignment drops bulk delay
    one = fit_relative_delay(m_pa, s_pa, [(c0, 0.0)], 40, 160, at)
    assert not one.confident  # mirror solution is just as good
    two = fit_relative_delay(m_pa, s_pa, [(c0, 0.0), (c1, 2.5)], 40, 160, at)
    assert abs(two.delay_ms - 3.5) < 0.05 and two.confident


def test_best_alignment_finds_delay_and_polarity(cfg):
    main, sub = xo_pair(10.0, 13.5)
    al = best_alignment([main], [sub.scaled(-1)], 40, 160, (-8, 8), cfg.tuning.alignment)
    assert al.invert and abs(al.delay_ms + 3.5) < 0.3


def test_optimize_pair_undoes_a_known_offset(cfg):
    a = Response(F, cascade_response([design("LP", 120, FS, q=0.7)], F, FS))
    b = a.delayed(-2.0).scaled(-1)  # b arrives 2 ms early with flipped polarity
    sol = optimize_pair([a], [b], np.zeros(F.size), 20, 120, 8.0, cfg.tuning.pair)
    assert abs(sol.delay_ms - 2.0) < 0.3 and sol.invert


def test_arrival_from_impulse_onset(cfg):
    r = Response(F, cascade_response([design("HP", 100, FS)], F, FS)).delayed(7.0)
    assert abs(arrival_ms(r, cfg.tuning.alignment) - 7.0) < 0.1


def test_peq_cuts_a_mode_and_respects_boost_cap(cfg):
    mode = design("PK", 45, FS, gain_db=9, q=5).response(F, FS)
    dip = design("PK", 70, FS, gain_db=-8, q=4).response(F, FS)
    sub = cascade_response(linkwitz_riley4("LP", 80, FS), F, FS) * mode * dip
    res = design_peq(F, [np.zeros(F.size, complex)], [sub], np.zeros(F.size), 20, 80, 5, FS, 3.0, cfg.tuning.peq)
    assert res.after_std_db < res.before_std_db * 0.75
    assert all(f.gain_db <= 3.0 for f in res.filters)
    assert len(res.filters) <= cfg.tuning.peq.max_filters
    assert any(abs(f.freq_hz - 45) < 5 and f.gain_db <= -4.0 for f in res.filters)
    assert all(f.gain_db >= -cfg.tuning.peq.max_cut_db for f in res.filters)  # light: never deeper than the cap


def test_peq_held_out_seats_reject_overfitting(cfg):
    rng = np.random.default_rng(0)
    base = cascade_response(linkwitz_riley4("LP", 80, FS), F, FS)
    # each seat has its own random modal structure: there is no common fix
    seats = [base * np.prod([design("PK", f, FS, gain_db=g, q=6).response(F, FS)
                             for f, g in zip(rng.uniform(25, 75, 3), rng.choice([-9, 9], 3))], axis=0) for _ in range(4)]
    res = design_peq(F, [np.zeros(F.size, complex)] * 4, seats, np.zeros(F.size), 20, 80, 5, FS, 3.0, cfg.tuning.peq)
    # either nothing was worth fitting, or what was kept also helps the seats it was not fitted on
    assert res.filters == [] or res.held_out_gain_db >= cfg.tuning.peq.min_improvement_db
