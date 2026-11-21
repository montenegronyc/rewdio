"""The crawl grid: candidate sub spots laid out like a chessboard so they are easy to find.

Rows are letters from the front wall backwards (A, B, C...), columns are numbers from the left wall
(1, 2, 3...). The outer rows and columns sit `origin_from_front_m` / `origin_from_left_m` from the walls, and the
spots between are evenly spaced, about `spacing_m` apart. In perimeter mode the route is one loop around the room,
and every spot is described from its nearest real wall (`open_walls` are openings, never measured from).
Coordinates follow Rewdio's frame: x from the front wall, y from the left wall, z up.
"""

from __future__ import annotations

import string

from ..config import CrawlGrid


def row_label(i: int) -> str:
    letters = string.ascii_uppercase
    return letters[i] if i < 26 else letters[i // 26 - 1] + letters[i % 26]


def build_grid(dims: tuple[float, float, float], g: CrawlGrid, extra_excluded: set[str] | None = None) -> list[dict]:
    """Every grid cell with its code, coordinates, wall distances and whether it will be visited."""
    L, W, _ = dims
    excluded = set(g.excluded) | (extra_excluded or set())
    eps = 1e-6

    def axis(start: float, end: float) -> list[float]:
        """Evenly spaced from wall margin to wall margin, as close to spacing_m as fits: both far corners included."""
        n = max(1, round((end - start) / g.spacing_m))
        return [round(start + (end - start) * k / n, 3) for k in range(n + 1)]

    xs = axis(g.origin_from_front_m, L - g.origin_from_front_m)
    ys = axis(g.origin_from_left_m, W - g.origin_from_left_m)
    cells = []
    for i, x in enumerate(xs):
        for j, y in enumerate(ys):
            code = f"{row_label(i)}{j + 1}"
            sides = {"front": x, "back": L - x, "left": y, "right": W - y}
            wall = min(d for w, d in sides.items() if w not in g.open_walls)
            in_mode = g.mode == "full" or wall <= g.perimeter_depth_m + eps
            cells.append({
                "code": code, "row": i, "col": j, "xyz": [x, y, g.mic_height_m],
                "from_front_m": round(x, 2), "from_left_m": round(y, 2),
                "from_back_m": round(L - x, 2), "from_right_m": round(W - y, 2),
                "enabled": in_mode and code not in excluded, "near_wall": in_mode, "excluded": code in excluded,
            })
    return cells


def where(cell: dict, open_walls: list[str], short: bool = False) -> str:
    """'35 cm from the front wall and 35 cm from the right wall' (short: '35 cm from front · 35 cm from right'):
    the nearest real wall on each axis."""
    def nearest(a: str, b: str) -> str:
        options = [w for w in (a, b) if w not in open_walls] or [a]
        return min(options, key=lambda w: cell[f"from_{w}_m"])
    depth, side = nearest("front", "back"), nearest("left", "right")
    if short:
        return f"{cell[f'from_{depth}_m'] * 100:.0f} cm from {depth} · {cell[f'from_{side}_m'] * 100:.0f} cm from {side}"
    return (f"{cell[f'from_{depth}_m'] * 100:.0f} cm from the {depth} wall and "
            f"{cell[f'from_{side}_m'] * 100:.0f} cm from the {side} wall")


def _ring_pos(xyz, dims) -> float:
    """Distance along the walls, clockwise from the front-left corner: front, right, back, left."""
    L, W = dims[0], dims[1]
    x, y = xyz[0], xyz[1]
    side = min({"front": x, "right": W - y, "back": L - x, "left": y}.items(), key=lambda kv: kv[1])[0]
    return {"front": y, "right": W + x, "back": W + L + (W - y), "left": 2 * W + L + (L - x)}[side]


def route(cells: list[dict], max_cells: int, dims=None, start_xyz=None, done=frozenset()) -> list[dict]:
    """The visiting order. With `dims` (perimeter mode): one loop along the walls; a resumed crawl carries on
    from `start_xyz` (where the mic is now) to the nearer unmeasured spot and keeps going the same way. Without:
    a snake, along row A left to right, row B right to left, and so on."""
    if dims is not None:
        out = sorted((c for c in cells if c["enabled"]), key=lambda c: _ring_pos(c["xyz"], dims))
        out = _thin(out, max_cells)
        return _resume(out, dims, start_xyz, done) if start_xyz is not None else out
    rows: dict[int, list[dict]] = {}
    for c in cells:
        if c["enabled"]:
            rows.setdefault(c["row"], []).append(c)
    out = []
    for k, i in enumerate(sorted(rows)):
        out += sorted(rows[i], key=lambda c: c["col"], reverse=bool(k % 2))
    return _thin(out, max_cells)


def _resume(out: list[dict], dims, start_xyz, done) -> list[dict]:
    """Measured spots first (skipped by the crawl), then the rest in loop order from the mic's position, in
    whichever direction reaches an unmeasured spot first."""
    perimeter = 2 * (dims[0] + dims[1])
    s = _ring_pos(start_xyz, dims)
    todo = [c for c in out if c["code"] not in done]
    if not todo:
        return out
    fwd = sorted(todo, key=lambda c: (_ring_pos(c["xyz"], dims) - s) % perimeter)
    back = sorted(todo, key=lambda c: (s - _ring_pos(c["xyz"], dims)) % perimeter)
    gap = lambda c, d: ((_ring_pos(c["xyz"], dims) - s) * d) % perimeter  # noqa: E731
    order = fwd if gap(fwd[0], 1) <= gap(back[0], -1) else back
    return [c for c in out if c["code"] in done] + order


def _thin(out: list[dict], max_cells: int) -> list[dict]:
    if len(out) > max_cells:  # thin evenly across the room, but never drop a corner: prime sub spots
        rmin, rmax = min(c["row"] for c in out), max(c["row"] for c in out)
        cmin, cmax = min(c["col"] for c in out), max(c["col"] for c in out)
        corner = {(r, c) for r in (rmin, rmax) for c in (cmin, cmax)}
        keep = {i for i, c in enumerate(out) if (c["row"], c["col"]) in corner}
        rest = [i for i in range(len(out)) if i not in keep]
        n = max(0, max_cells - len(keep))
        step = len(rest) / n if n else 0
        keep |= {rest[int(k * step)] for k in range(n)}
        out = [c for i, c in enumerate(out) if i in keep]
    return out


def setup_instructions(g: CrawlGrid, n_cells: int) -> str:
    return (f"Set up the crawl grid. Mark A1 on the floor: {g.origin_from_front_m * 100:.0f} cm out from the FRONT "
            f"wall and {g.origin_from_left_m * 100:.0f} cm out from the LEFT wall. The spots run in one loop along the "
            f"walls, evenly spaced about {g.spacing_m * 100:.0f} cm apart, each "
            f"{g.origin_from_left_m * 100:.0f} cm out from its wall; every prompt says which wall to measure from. "
            f"{n_cells} spots to visit. At every spot the mic stands {g.mic_height_m * 100:.0f} cm off the floor, "
            f"where the centre of the sub's woofer will be; if yours sits higher or lower, change the grid's mic "
            f"height in Settings first. Tap any cell where a sub can't go (sofa, doorway) to skip it. "
            f"Put the mic on A1, then confirm: the first sweep starts straight away.")
