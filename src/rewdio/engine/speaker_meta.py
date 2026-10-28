"""What a speaker capture set records about itself, so a simulator can reproduce the setup.

Coordinate frame (agreed with SoundSible, see the `inbox` branch DECISIONS.md): x forward, y across
(positive to the speaker's left), z up, origin at the top of the woofer box on the front face, centre.
Metres. Positions are recorded with the speaker upright at 0 degrees.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

ET = ZoneInfo("America/New_York")
FRAME = ("x forward, y to the speaker's left, z up; origin at the top of the woofer box on the front face, "
         "centre; metres; speaker upright at 0 degrees")
ANGLE_CONVENTION = {
    "hor": "rotation about the vertical axis through the pivot; positive = the mic sees more of the speaker's left",
    "ver": "speaker laid on its right side on the same pivot; positive = the mic sees more of the speaker's top "
           "(above the axis)",
}


class Geometry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pivot_xyz: tuple[float, float, float] | None = Field(default=None, description="Turntable axis position "
                                                         "(upright frame, recorded once for both planes)")
    mic_xyz: tuple[float, float, float] | None = None
    riser_h: float | None = Field(default=None, ge=0, description="Sand-riser height under the woofer box")
    origin_height_m: float | None = Field(default=None, ge=0, description="Ground to the frame origin (top of the "
                                                                          "woofer box): riser plus feet plus box")
    mic_height_m: float | None = Field(default=None, ge=0, description="Mic above the ground")
    horn_offset_m: float | None = Field(default=None, ge=0, le=0.30, description="Horn forward of flush on its rails")
    horn_stem_lift_m: float | None = Field(default=0.04, ge=0)
    horn_id: str | None = Field(default=None, description="A SoundSible variant key (16 hex), 'stock:<name>', or a label")
    horn_stl: str | None = Field(default=None, description="Path to the horn's STL, for a 'stock:' horn")
    series_cap_uf: float | None = Field(default=None, gt=0, description="Horn driver's series protection capacitor")
    series_cap_in_circuit: bool | None = Field(
        default=None, description="For the acoustic and distortion captures; default: in circuit if a value is "
                                  "given, otherwise there is no cap")

    @model_validator(mode="after")
    def _cap(self) -> Geometry:
        if self.series_cap_in_circuit is None:
            self.series_cap_in_circuit = self.series_cap_uf is not None
        elif self.series_cap_in_circuit and self.series_cap_uf is None:
            raise ValueError("series_cap_in_circuit=true needs series_cap_uf (its value in microfarads)")
        return self
    mic_given_as: dict | None = Field(default=None, description="How the mic position was entered, if not as xyz")

    @classmethod
    def from_inputs(cls, *, pivot_xyz=None, mic_xyz=None, mic_distance_m=None, mic_height_m=None,
                    origin_height_m=None, **rest) -> Geometry:
        """mic_xyz directly, or: horizontal distance in front of the pivot + mic height above the ground +
        the frame origin's height above the ground (top of the woofer box on its riser)."""
        given = None
        if pivot_xyz is None:
            pivot_xyz = (0.0, 0.0, 0.0)  # the default the setup instructions ask for: the frame origin
        if mic_xyz is None and mic_distance_m is not None:
            if mic_height_m is None or origin_height_m is None:
                raise ValueError("mic by distance needs mic_height_m and origin_height_m (both above the ground); "
                                 "or give mic_xyz")
            mic_xyz = (pivot_xyz[0] + mic_distance_m, pivot_xyz[1], mic_height_m - origin_height_m)
            given = {"mic_distance_m": mic_distance_m, "mic_height_m": mic_height_m, "origin_height_m": origin_height_m}
        if mic_height_m is None and mic_xyz is not None and origin_height_m is not None:
            mic_height_m = origin_height_m + mic_xyz[2]
        return cls(pivot_xyz=pivot_xyz, mic_xyz=mic_xyz, mic_given_as=given, mic_height_m=mic_height_m,
                   origin_height_m=origin_height_m, **rest)

    def ground_bounce_ms(self, c: float) -> float | None:
        """How long after the direct sound the ground reflection reaches the mic, for a source at the frame
        origin. Compare with the gate: the gate must end before it."""
        if self.mic_xyz is None or self.origin_height_m is None or self.mic_height_m is None:
            return None
        d = math.hypot(self.mic_xyz[0], self.mic_xyz[1])
        hs, hm = self.origin_height_m, self.mic_height_m
        return (math.hypot(d, hs + hm) - math.hypot(d, hm - hs)) / c * 1000

    def mic_distance(self) -> float | None:
        if self.pivot_xyz is None or self.mic_xyz is None:
            return None
        return math.dist(self.pivot_xyz, self.mic_xyz)

    def missing(self) -> list[str]:
        return [k for k in ("pivot_xyz", "mic_xyz", "riser_h", "origin_height_m", "mic_height_m", "horn_offset_m",
                            "horn_id") if getattr(self, k) is None]  # no series cap is a valid answer


class Environment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    temperature_c: float | None = Field(default=None, ge=-30, le=50)
    indoor: bool | None = None
    site: str | None = None
    soundsible_commit: str | None = Field(default=None, description="SoundSible commit compared against; "
                                                                     "SoundSible fills it if empty")
    started_et: str = Field(default_factory=lambda: now_et().isoformat(timespec="seconds"))

    @property
    def speed_of_sound(self) -> float:
        t = 20.0 if self.temperature_c is None else self.temperature_c
        return 331.3 * math.sqrt(1 + t / 273.15)

    def missing(self) -> list[str]:
        return [k for k in ("temperature_c", "indoor", "site") if getattr(self, k) is None]


def now_et() -> datetime:
    return datetime.now(ET)


def slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._+-]+", "-", s.strip()).strip("-") or "x"


def set_folder(env: Environment, variant: str) -> str:
    """`<YYYY-MM-DD>_<site>_<variant>`, the date the set was started, in ET."""
    return f"{env.started_et[:10]}_{slug(env.site or 'site')}_{slug(variant)}"


class AnglePlan(BaseModel):
    """Explicit per-plane angle lists. 0 is measured once, first."""
    model_config = ConfigDict(extra="forbid")
    hor: list[float] = Field(default_factory=list)
    ver: list[float] = Field(default_factory=list)
    both_signs: bool = True

    @model_validator(mode="after")
    def _check(self) -> AnglePlan:
        for a in self.hor + self.ver:
            if not 0 <= a <= 180:
                raise ValueError(f"angle {a} outside 0..180 (both_signs adds the negatives)")
        return self


PRESETS = {
    # SoundSible BUILD_PLAN: 5 degree steps to 60, then 10 degree steps to 180, both planes
    "soundsible": AnglePlan(hor=[*range(5, 61, 5), *range(70, 181, 10)],
                            ver=[*range(5, 61, 5), *range(70, 181, 10)]),
}
