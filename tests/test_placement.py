import itertools

from rewdio.analysis.alignment import optimize_pair
from rewdio.analysis.placement import Candidate, rank_positions
from rewdio.analysis.response import Response, log_grid
from rewdio.analysis.target import target_db
from rewdio.sim.world import P_REF, default_world


def test_reciprocity_crawl_ranks_pairs_like_ground_truth(cfg):
    w = default_world()
    f = log_grid(96, 10, 20000)
    pt = cfg.tuning.pair
    tgt = target_db(f, cfg.target)
    for o in w.dsp.values():
        o.gain_db, o.invert, o.delay_ms = 0, False, 0
    sub = w.speaker("SUB1")
    for s in w.speakers:
        w.dsp[s.dsp_output].mute = s is not sub
    L, W, _ = w.room.dims
    spots = [(0.35, 0.35, 0.25), (0.35, W / 2, 0.25), (L - 0.35, W - 0.35, 0.25), (L - 0.35, 0.35, 0.25),
             (L / 2, 0.35, 0.25), (L / 2, W - 0.35, 0.25)]
    seat = w.seats["MLP"]

    def meas(src, mic):
        sub.pos, w.mic = src, mic
        return Response(f, w.response(f, -30, noise=False) / P_REF)

    truth = {}
    true_resp = {i: meas(sp, seat) for i, sp in enumerate(spots)}  # sub at spot, mic at seat
    for a, b in itertools.combinations(range(len(spots)), 2):
        truth[(f"C{a}", f"C{b}")] = optimize_pair([true_resp[a]], [true_resp[b]], tgt, 20, 120, 8.0, pt).score
    crawl = [Candidate(f"C{i}", {"MLP": meas(seat, sp)}, sp) for i, sp in enumerate(spots)]  # swapped
    res = rank_positions(crawl, tgt, "absolute", 8.0, (20, 120), pt, 6, 5)
    best = tuple(res["best_pairs"][0]["positions"])
    assert abs(truth[best] - min(truth.values())) < 1e-6
