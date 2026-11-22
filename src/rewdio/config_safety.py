"""Settings checks that protect the speakers, across sections of the config (docs/SAFETY_AUDIT.md)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from .minidsp.state import XoverFilter

if TYPE_CHECKING:
    from .config import SystemConfig


class LoudnessGuard(BaseModel):
    """Nothing plays loud before it has been heard quietly (2026-11-21: a sub sweep shook the house)."""
    model_config = ConfigDict(extra="forbid")
    probe_drop_db: float = Field(gt=0, description="Each room channel's first sweep (per placement) is a probe this "
                                                   "many dB below the run level")
    max_above_mains_db: float = Field(description="A channel heard louder than one main by more than this stops the "
                                                  "run until its own gain is turned down")
    crawl_extra_db: float = Field(ge=0, description="Extra allowance in the crawl: the mic sits by walls and corners, "
                                                    "which read hotter than the seat for the same sub drive")
    max_attempts: int = Field(ge=1, description="Probes of a channel that stays too loud before the run stops")
    confirm_raises: bool = Field(description="Gain staging asks before raising the level above where it started")


def enforce_safety(cfg: SystemConfig) -> None:
    """Refuse settings that let staging aim at REW's abort limit or let level + gain clip the DSP, and give every
    managed sub a protected infrasonic high-pass. Raises ValueError (the config fails to load)."""
    sw, gs = cfg.sweep, cfg.tuning.gain_staging
    if sw.target_spl_db > sw.max_spl_db - gs.peak_margin_db - 3:
        raise ValueError(f"sweep.target_spl_db ({sw.target_spl_db}) must sit at least peak_margin_db + 3 dB below "
                         f"sweep.max_spl_db ({sw.max_spl_db}), or staging aims at REW's abort limit")
    lift = max(cfg.max_main_gain_db + cfg.balance.max_gain_db_per_side,
               cfg.max_sub_gain_db + max(cfg.max_peq_boost_db, 0.0))
    if sw.max_level_dbfs + lift > sw.max_output_dbfs:
        raise ValueError(f"not enough headroom: sweep.max_level_dbfs ({sw.max_level_dbfs}) plus the largest gain "
                         f"and boost Rewdio may apply ({lift:+.1f} dB) exceeds sweep.max_output_dbfs "
                         f"({sw.max_output_dbfs}): lower max_level_dbfs or the gain limits")
    if cfg.sub_infrasonic_hp_hz is not None:
        for c in cfg.subs:
            if c.crossover is not None and not any(f.kind == "HP" for f in c.crossover):
                c.crossover = [*c.crossover, XoverFilter(kind="HP", topology="BW2", freq_hz=cfg.sub_infrasonic_hp_hz,
                                                         protect=True)]
