"""Loudness guard: no room channel plays at the run level before it has been heard quietly.

The run level is set by gain staging on the MAINS. A sub in a corner, or a sub whose own amp gain is up, can be far
louder than the mains at that level (2026-11-21: the first sub sweep at the seat shook the house). So before a
sub's first sweep at a new placement, it plays one probe `probe_drop_db` below the run level. If what the mic
hears, scaled back up to the run level, is louder than one main by more than `max_above_mains_db`, the run stops and
asks the user to turn that channel's own gain down; after that it probes again. See docs/SAFETY_AUDIT.md (#9).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from ..analysis.response import band_median_db
from ..config import ChannelKind

if TYPE_CHECKING:
    from .rig import Rig

UNGUARDED_KINDS = {"probe", "staging"}  # the probe itself, and staging (mains only, from the quiet start)


class TooLoud(Exception):
    """A channel's quiet probe says it would play louder than the mains at the run level."""

    def __init__(self, role: str, over_db: float, turn_down_db: int):
        super().__init__(f"{role} would play {over_db:.0f} dB louder than one main at the run level")
        self.role, self.over_db, self.turn_down_db = role, over_db, turn_down_db


def placement(kind: str) -> str:
    """Where the speaker is for this sweep: in the crawl the sub sits at the seat; otherwise at its own place."""
    return "crawl" if kind == "crawl" else "room"


def heard_db(rig: Rig, role: str, resp) -> float:
    b, cfg = rig.cfg.tuning.bands, rig.cfg
    lo, hi = ((b.sub_level_lo_hz, cfg.crossover_hz * b.sub_level_xo_factor)
              if cfg.channel(role).kind == ChannelKind.SUB else b.mains_level_hz)
    return band_median_db(resp, lo, hi, b.level_smoothing)


async def guard(rig: Rig, roles: list[str], kind: str, position: str) -> None:
    """Probe every room channel in `roles` that hasn't been heard quietly at this placement. Raises TooLoud."""
    wf, cfg = rig.s.workflow, rig.cfg
    ref = wf.staging_reference_db
    if kind in UNGUARDED_KINDS or ref is None:
        return
    g = cfg.tuning.loudness_guard
    for role in roles:
        if cfg.channel(role).kind != ChannelKind.SUB:
            # the mains set the run level themselves (staging plays them together), so neither is louder than what
            # was staged; driver captures have their own levels and checks
            continue
        key = f"{role}:{placement(kind)}"
        if key in wf.probed:
            continue
        run = rig.level_dbfs
        quiet = max(cfg.sweep.min_level_dbfs, run - g.probe_drop_db)
        rig.level_dbfs = quiet
        try:
            rec, resp = await rig.sweep_once([role], "probe", position,
                                             title=f"{role} @{position} (quiet probe, {placement(kind)})")
        finally:
            rig.level_dbfs = run
        rig.s.measurements.remove(rec)  # a probe is a safety check, never data for the solver
        at_run = heard_db(rig, role, resp) + (run - quiet)
        over = at_run - ref
        allowed = g.max_above_mains_db + (g.crawl_extra_db if placement(kind) == "crawl" else 0.0)
        if over > allowed:
            wf.probe_attempts[role] = wf.probe_attempts.get(role, 0) + 1
            rig.s.save()
            raise TooLoud(role, over, max(3, math.ceil(over - allowed) + 2))
        wf.probed[key] = {"heard_db": round(at_run, 1), "mains_db": round(ref, 1), "run_dbfs": run}
        wf.probe_attempts.pop(role, None)
        rig.s.save()
