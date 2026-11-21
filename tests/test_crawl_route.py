"""The crawl walks the room in one loop, evenly spaced, measured from the nearest real wall (first real-rig feedback)."""

import math

from rewdio.engine.grid import build_grid, route, where

ROOM = (5.334, 3.2, 3.048)  # 17.5 x 10.5 x 10 ft; the back opens into a kitchen


def _grid(cfg, **kw):
    g = cfg.crawl_grid.model_copy(update=kw)
    return g, build_grid(ROOM, g)


def test_spots_are_evenly_spaced_from_wall_to_wall(cfg):
    g, cells = _grid(cfg)
    ys = sorted({c["xyz"][1] for c in cells})
    gaps = {round(b - a, 3) for a, b in zip(ys, ys[1:])}
    assert ys[0] == g.origin_from_left_m and abs(ys[-1] - (ROOM[1] - g.origin_from_left_m)) < 1e-3
    assert max(gaps) - min(gaps) < 0.002  # no squeezed last column
    xs = sorted({c["xyz"][0] for c in cells})
    xgaps = [b - a for a, b in zip(xs, xs[1:])]
    assert max(xgaps) - min(xgaps) < 0.002


def test_the_route_is_one_loop_around_the_room(cfg):
    g, cells = _grid(cfg, excluded=[c for c in ("G1", "G2", "G3", "G4")])
    path = route(cells, 99, dims=ROOM)
    codes = [c["code"] for c in path]
    assert codes[:4] == ["A1", "A2", "A3", "A4"]  # along the front
    hops = [math.dist(a["xyz"], b["xyz"]) for a, b in zip(path, path[1:])]
    long = [h for h in hops if h > 1.5 * g.spacing_m]
    assert len(long) <= 1  # one crossing (the open back), never back and forth


def test_a_resumed_crawl_carries_on_from_where_the_mic_is(cfg):
    g, cells = _grid(cfg, excluded=["G1", "G2", "G3", "G4"])
    done = {"A1", "A2", "A3", "A4", "B1", "B4", "C1"}
    c1 = next(c for c in cells if c["code"] == "C1")
    path = route(cells, 99, dims=ROOM, start_xyz=c1["xyz"], done=done)
    todo = [c["code"] for c in path if c["code"] not in done]
    assert todo[0] == "D1" and todo[-1] == "C4"  # down the left side, across, back up the right
    assert set(todo) == {"D1", "E1", "F1", "F4", "E4", "D4", "C4"}


def test_distances_are_from_the_nearest_real_wall(cfg):
    g, cells = _grid(cfg)
    right = next(c for c in cells if c["code"] == "C4")
    left = next(c for c in cells if c["code"] == "C1")
    assert "right wall" in where(right, []) and "left wall" in where(left, [])
    deep = max(cells, key=lambda c: c["xyz"][0])
    assert "back wall" in where(deep, [])
    assert "back" not in where(deep, ["back"]) and "front wall" in where(deep, ["back"])  # no wall to measure from


def test_open_walls_do_not_make_spots_near_a_wall(cfg):
    g, cells = _grid(cfg, open_walls=["back"])
    middle_back = [c for c in cells if c["row"] == max(x["row"] for x in cells) and 0 < c["col"] < 3]
    assert middle_back and not any(c["near_wall"] for c in middle_back)


async def test_the_phone_shows_only_the_spots_on_the_route(sim_runtime):
    from rewdio.dashboard.state import crawl_view
    rt = sim_runtime(do_sub_crawl=True)
    for _ in range(6):
        rep = await rt.autopilot.step()
        if rep["status"] == "user_action_required" and rep["action"]["key"].startswith("crawl:mic:"):
            break
        rt.apply_sim_effects(rep["action"], None)
        rt.autopilot.confirm(rep["action"]["key"])
    view = crawl_view(rt)
    route_codes = {sp["label"] for sp in rt.session.workflow.crawl["spots"]}
    assert view["cells"] and {c["code"] for c in view["cells"]} <= route_codes
    assert not any(c["status"] == "excluded" for c in view["cells"])
    assert "wall" in rep["action"]["instructions"]


async def _crawl_until(rt, measured: int):
    """Step the crawl (doing the user's tasks) until `measured` spots are in, stopping at the next mic prompt."""
    for _ in range(80):
        rep = await rt.autopilot.step()
        assert rep["status"] == "user_action_required", rep
        if rep["action"]["key"].startswith("crawl:mic:") and len(rt.session.workflow.crawl.get("measured", {})) >= measured:
            return rep
        rt.apply_sim_effects(rep["action"], None)
        rt.autopilot.confirm(rep["action"]["key"])
    raise AssertionError("crawl did not get there")


async def test_a_measured_spot_can_be_measured_again(sim_runtime):
    rt = sim_runtime(do_sub_crawl=True)
    await _crawl_until(rt, 3)
    measured = rt.session.workflow.crawl["measured"]
    old = measured["A2"]["id"]
    res = rt.remeasure_cell("A2")
    assert res["ok"] and "A2" not in measured
    rep = await rt.autopilot.step()
    assert rep["action"]["key"] == "crawl:mic:A2"  # asks for the mic first: never re-sweeps where the mic was
    rt.apply_sim_effects(rep["action"], None)
    rt.autopilot.confirm(rep["action"]["key"])
    await rt.autopilot.step()
    assert measured["A2"]["id"] != old and len(measured) == 3


async def test_remeasuring_refuses_spots_that_are_not_measured(sim_runtime):
    rt = sim_runtime(do_sub_crawl=True)
    await _crawl_until(rt, 1)
    assert not rt.remeasure_cell("F4")["ok"]
    assert not rt.remeasure_cell("Z9")["ok"]
