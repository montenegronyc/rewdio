"""The crawl grid: candidate sub spots laid out like a chessboard so they are easy to find.

Rows are letters from the front wall backwards (A, B, C...), columns are numbers from the left wall
(1, 2, 3...). A1 sits `origin_from_front_m` from the front wall and `origin_from_left_m` from the left
wall; every step is `spacing_m`. The user marks A1 once with a tape measure and paces the rest.
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
    xs, x = [], g.origin_from_front_m
    while x <= L - g.origin_from_front_m + eps:
        xs.append(round(x, 3))
        x += g.spacing_m
    ys, y = [], g.origin_from_left_m
    while y <= W - g.origin_from_left_m + eps:
        ys.append(round(y, 3))
        y += g.spacing_m
    cells = []
    for i, x in enumerate(xs):
        for j, y in enumerate(ys):
            code = f"{row_label(i)}{j + 1}"
            wall = min(x, L - x, y, W - y)
            in_mode = g.mode == "full" or wall <= g.perimeter_depth_m + eps
            cells.append({
                "code": code, "row": i, "col": j, "xyz": [x, y, g.mic_height_m],
                "from_front_m": round(x, 2), "from_left_m": round(y, 2),
                "from_back_m": round(L - x, 2), "from_right_m": round(W - y, 2),
                "enabled": in_mode and code not in excluded, "near_wall": in_mode, "excluded": code in excluded,
            })
    return cells


def route(cells: list[dict], max_cells: int) -> list[dict]:
    """Visit enabled cells in a snake: along row A left to right, row B right to left, and so on."""
    rows: dict[int, list[dict]] = {}
    for c in cells:
        if c["enabled"]:
            rows.setdefault(c["row"], []).append(c)
    out = []
    for k, i in enumerate(sorted(rows)):
        out += sorted(rows[i], key=lambda c: c["col"], reverse=bool(k % 2))
    if len(out) > max_cells:  # thin evenly across the room rather than dropping the back rows
        step = len(out) / max_cells
        out = [out[int(k * step)] for k in range(max_cells)]
    return out


def setup_instructions(g: CrawlGrid, n_cells: int) -> str:
    return (f"Set up the crawl grid. Mark A1 on the floor: {g.origin_from_front_m * 100:.0f} cm out from the FRONT "
            f"wall and {g.origin_from_left_m * 100:.0f} cm out from the LEFT wall. Rows are letters going back into "
            f"the room, columns are numbers going right; every step is {g.spacing_m * 100:.0f} cm. "
            f"{n_cells} spots to visit. Tap any cell where a sub can't go (sofa, doorway) to skip it. "
            f"Confirm when A1 is marked.")
