"""The manual turntable: which angles in which order, and what to tell the person turning it.

Order: one continuous revolution per plane in constant steps, never alternating sides. Alternating
(+5, -5, +10, -10 ...) makes every move bigger than the last, up to 350 degrees, and over 70 steps that is
where the mistakes come from. Here every move is one step, except at most one jump per plane.

Direction: a positive angle means the mic sees more of the speaker's left (horizontal plane) or of its top
(vertical plane, speaker lying on its right side). Seen from ABOVE, both are a CLOCKWISE turn of the
turntable: the speaker's front swings toward its own right. Prompts give the absolute target as well as the
move, so a missed step shows instead of accumulating.
"""

from __future__ import annotations

from pydantic import BaseModel

from .speaker_meta import AnglePlan


class Point(BaseModel):
    plane: str
    angle: float


def _plane_order(angles: list[float], both_sides: bool) -> list[float]:
    frac = [a for a in angles if float(a) != round(float(a))]
    if frac:  # VituixCAD file names carry whole degrees: 2.5 and 3 would overwrite each other
        raise ValueError(f"angles must be whole degrees; got {frac}")
    pos = sorted({round(float(a), 3) for a in angles if 0 < a <= 180})
    if not both_sides:
        return pos
    neg = [-a for a in pos if a < 180]
    if pos and pos[-1] >= 180:  # carry on round the same way: 180 -> -170 is one more clockwise step
        neg = sorted(neg)
    else:  # one jump back through 0, then outwards on the other side
        neg = sorted(neg, reverse=True)
    return pos + neg


def order(plan: dict[str, list[float]], both_sides: bool) -> list[Point]:
    """0 once (first plane), then each plane as one revolution."""
    planes = [p for p in ("hor", "ver") if plan.get(p)] or ["hor"]
    pts = [Point(plane=planes[0], angle=0.0)]
    for plane in planes:
        pts += [Point(plane=plane, angle=a) for a in _plane_order(plan.get(plane, []), both_sides)]
    return pts


def angles_for(planes: list[str], step_deg: float = 10.0, max_deg: float = 180.0, symmetric: bool = False) -> list[Point]:
    """Even steps to max_deg in each plane. symmetric=True measures the positive side only (VituixCAD mirrors)."""
    if step_deg <= 0 or max_deg <= 0:
        raise ValueError("step and max angle must be positive")
    n = int(round(max_deg / step_deg))
    steps = [round(k * step_deg, 3) for k in range(1, n + 1)]
    return order({p: steps for p in planes}, both_sides=not symmetric)


def points_from(plan: AnglePlan) -> list[Point]:
    return order({"hor": plan.hor, "ver": plan.ver}, both_sides=plan.both_signs)


def move(prev: float, target: float) -> float:
    """Signed turn from `prev` to `target` in degrees, the short way round: positive = clockwise from above."""
    d = ((target - prev + 180) % 360) - 180
    return 180.0 if d == -180 else round(d, 3)


def _say_angle(a: float) -> str:
    if a == 0:
        return "zero"
    return f"{'plus' if a > 0 else 'minus'} {abs(a):g}"


def prompt(points: list[Point], index: int, speaker: str, distance_m: float, mic_height_m: float | None) -> dict:
    """What to tell the person at the turntable before point `index`: a full instruction and a terse
    spoken line, with the absolute target and the move."""
    p = points[index]
    prev = points[index - 1] if index else None
    plane_word = "Horizontal" if p.plane == "hor" else "Vertical"
    if index == 0:
        mic = f" at {mic_height_m:g} m above the ground" if mic_height_m is not None else ", level with that point"
        text = (f"Set up {speaker}: the turntable axis under the top-front centre of the woofer box, the speaker "
                f"facing the mic squarely (that is 0 degrees), the mic {distance_m:g} m away straight ahead{mic}, "
                "pointing at the speaker. Leave a full turn of slack in the speaker and amplifier cables: a plane is "
                "one whole revolution. Do not move the mic or the turntable until the plane is done.")
        return {"plane": p.plane, "target": 0.0, "move_deg": 0.0, "text": text,
                "speech": f"{plane_word}, zero. Face the speaker at the mic. Tap ready."}
    new_plane = prev.plane != p.plane
    start = 0.0 if new_plane else prev.angle
    m = move(start, p.angle)
    way = "clockwise" if m > 0 else "anticlockwise"
    turn = f"turn {abs(m):g} degrees {way} as seen from above" if m else "no turn"
    if new_plane:
        text = (f"Vertical plane: first unwind the cables (the last plane was a full turn). Lay {speaker} on its "
                "RIGHT side on the turntable, with the same point (top-front "
                "centre of the woofer box) over the turntable axis, facing the mic squarely (vertical 0). Raise or "
                "lower the mic so it is level with that point again, same distance, and support the horn. Then "
                f"{turn}, to vertical {p.angle:+g}.")
        speech = f"Vertical plane. Speaker on its right side, facing the mic. Then {_say_angle(p.angle)}. Tap ready."
    else:
        text = f"{plane_word} {p.angle:+g}: {turn}. Keep the mic where it is."
        speech = f"{plane_word}, {_say_angle(p.angle)}. Turn {abs(m):g} degrees {way}. Tap ready."
    return {"plane": p.plane, "target": p.angle, "move_deg": m, "text": text, "speech": speech}
