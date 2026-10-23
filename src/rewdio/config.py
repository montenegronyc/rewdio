"""System configuration: what is plugged in where, how the solvers are tuned, and what "done" means.

No values live here. Defaults come from `defaults.json` (shipped with the package), overlaid by the
user's config file (`$REWDIO_CONFIG`, else `~/.rewdio/config.json`), overlaid by per-session changes.
These models only define and validate the shape; unknown keys are rejected so typos fail loudly.
"""

from __future__ import annotations

import copy
import json
import os
from enum import Enum
from importlib import resources
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChannelKind(str, Enum):
    MAIN = "main"
    SUB = "sub"


class TimingMode(str, Enum):
    LOOPBACK = "loopback"  # REW loopback timing reference: absolute phase
    ACOUSTIC = "acoustic"  # REW acoustic timing reference (user-configured in REW)
    SUM_FIT = "sum_fit"  # no reference: relative timing fitted from combined sweeps


class Channel(_Strict):
    role: str = Field(description="Short name, e.g. L, R, SUB1, SUB2")
    kind: ChannelKind
    dsp_output: int = Field(ge=0, description="0-based miniDSP output index")
    rew_output_channel: str | None = Field(
        default=None, description="REW output channel for this speaker; null = leave REW's selection alone")


class Baseline(_Strict):
    gain_db: float
    delay_ms: float
    invert: bool


class TargetCurve(_Strict):
    bass_boost_db: float = Field(ge=-6, le=12)
    shelf_hz: float = Field(gt=10, lt=300)
    shelf_octaves: float = Field(gt=0.1, le=4)
    house_curve_path: str | None = Field(description="REW-style house curve text file (freq dB); overrides the shelf")


class Convergence(_Strict):
    lr_level_tolerance_db: float
    min_crossover_efficiency_db: float
    max_bass_deviation_db: float = Field(description="Max std of bass deviation from target")
    max_prediction_error_db: float
    max_iterations: int = Field(ge=1)
    plateau_improvement_db: float
    lr_balance_max_std_db: float = Field(description="Max std of the smoothed L/R difference after correction")


class SweepSettings(_Strict):
    start_hz: int
    end_hz: int
    length: str
    level_dbfs: float
    min_level_dbfs: float
    max_level_dbfs: float
    target_spl_db: float
    max_spl_db: float
    min_snr_db: float
    start_delay_s: float = Field(description="REW start delay; raise it if a DAC/upsampler eats the sweep start")
    fill_silence_with_dither: bool = Field(
        description="Keep the digital stream alive between sweeps so an S/PDIF DAC stays locked")
    rew_sample_rate: int = Field(description="REW's audio sample rate, used to size the sweep timeout")


class RewClientSettings(_Strict):
    read_timeout_s: float
    command_timeout_s: float
    poll_interval_s: float
    settle_s: float = Field(description="Pause after a sweep lands; REW refuses back-to-back sweeps otherwise")
    timeout_margin_s: float = Field(description="Added to the sweep's own duration for the completion timeout")


class Physics(_Strict):
    speed_of_sound_m_s: float


class Grid(_Strict):
    f_lo_hz: float
    f_hi_hz: float


class Bands(_Strict):
    mains_level_hz: tuple[float, float]
    bass_eval_hz: tuple[float, float]
    bass_smoothing: float
    level_smoothing: float
    sub_level_lo_hz: float
    sub_level_xo_factor: float
    crossover_span_factor: float
    sub_pair_lo_hz: float
    sub_pair_xo_factor: float
    sub_pair_max_hz: float
    peq_lo_hz: float
    peq_xo_factor: float
    peq_max_hz: float
    sumfit_mains_hz: tuple[float, float]
    crawl_eval_hz: tuple[float, float]
    prediction_check_hz: tuple[float, float]
    summary_peaks_hz: tuple[float, float]


class AlignmentTuning(_Strict):
    delay_step_ms: float
    tie_tolerance_db: float
    sumfit_max_ms: float
    sumfit_step_ms: float
    sumfit_confident_residual_db: float
    sumfit_confident_margin_db: float
    arrival_f_hi_hz: float
    arrival_onset_fraction: float
    arrival_hp_hz: float
    polarity_min_gain_db: float = Field(description="Flip sub polarity only if crossover summation improves this much")


class PairTuning(_Strict):
    gain_range_db: float
    smoothing: float
    spread_weight: float
    loss_weight: float
    coarse_delay_step_ms: float
    coarse_gain_step_db: float
    fine_delay_step_ms: float
    fine_gain_step_db: float
    delay_tie_penalty_per_ms: float
    gain_tie_penalty_per_db: float
    invert_penalty: float = Field(description="Score cost of inverting a sub, so polarity only flips when it pays")


class PeqTuning(_Strict):
    max_cut_db: float
    smoothing: float
    spread_limit_db: float
    dip_weight: float
    min_q: float
    max_q: float
    start_q: float
    min_gain_db: float
    min_improvement_db: float
    stop_error_db: float
    max_nfev: int
    max_filters: int = Field(description="Hard cap on sub PEQ filters, whatever slots are free")
    gain_penalty: float = Field(description="Ridge penalty per dB of filter gain: prefer the smallest filters that work")
    min_total_improvement_db: float = Field(description="PEQ earns its place if it cuts peak excess by this much...")
    min_peak_reduction_db: float = Field(description="...or if it knocks the worst peak down by this much")
    only_after_verified_pass: bool = Field(description="Never PEQ before a verified gain/delay/polarity-only pass")


class RestraintTuning(_Strict):
    gain_deadband_db: float = Field(description="Smaller gain changes than this are not written")
    delay_deadband_ms: float = Field(description="Smaller delay changes than this are not written")
    balance_trim_deadband_db: float = Field(description="L/R broadband differences below this are left alone")


class GainStaging(_Strict):
    tolerance_db: float
    max_steps: int
    max_step_up_db: float
    max_step_down_db: float
    abort_backoff_db: float
    peak_margin_db: float


class CrawlTuning(_Strict):
    wall_distance_m: float
    mic_height_m: float
    top_pairs: int


class AutopilotTuning(_Strict):
    max_sweeps_per_step: int
    sumfit_probe_fraction_of_period: float


class QualityTuning(_Strict):
    min_snr_db_critical: float
    min_snr_db_warning: float
    silent_channel_margin_db: float
    max_start_hz: float
    repeat_check: bool
    repeat_band_hz: tuple[float, float]
    max_repeat_std_db: float


class DiagnosticsTuning(_Strict):
    mode_max_hz: float
    mode_match_tolerance: float
    sbir_band_hz: tuple[float, float]
    sbir_min_q: float
    seat_spread_flag_db: float
    seat_outlier_db: float
    seat_band_hz: tuple[float, float]
    seat_smoothing: float


class PreflightTuning(_Strict):
    input_level_listen_s: float
    require_live_mic: bool
    loopback_check: bool = Field(description="In loopback mode, prove the loopback signal arrives before sweeping")
    loopback_test_level_dbfs: float
    loopback_min_rms_dbfs: float


class CrosscheckTuning(_Strict):
    enabled: bool = Field(description="Cross-check Rewdio's maths against REW's own alignment tool and EQ engine")
    alignment_disagree_ms: float
    peq_disagree_db: float
    rew_equaliser: dict[str, str] = Field(description="REW equaliser profile matching the miniDSP")


class Tuning(_Strict):
    physics: Physics
    grid: Grid
    bands: Bands
    alignment: AlignmentTuning
    pair: PairTuning
    peq: PeqTuning
    gain_staging: GainStaging
    crawl: CrawlTuning
    autopilot: AutopilotTuning
    quality: QualityTuning
    diagnostics: DiagnosticsTuning
    preflight: PreflightTuning
    crosscheck: CrosscheckTuning
    restraint: RestraintTuning


class WaterfallSettings(_Strict):
    hz: tuple[float, float] = Field(description="Frequency span of the waterfall (cumulative spectral decay)")
    ppo: int = Field(ge=1, description="Points per octave")
    smoothing: float = Field(description="Fractional-octave smoothing of each slice")
    window_ms: float = Field(gt=0, description="Length of each analysis window")
    step_ms: float = Field(gt=0, description="Time between slices")
    slices: int = Field(ge=2, le=80)
    floor_db: float = Field(lt=0, description="Bottom of the plot, relative to the loudest point")
    ir_fs: float = Field(gt=0, description="Sample rate the stored impulse is resampled to (keep > 2x hz top)")
    ir_ms: float = Field(gt=0, description="How much impulse after the peak is stored for the waterfall")


class DashboardSettings(_Strict):
    enabled: bool
    allow_lan: bool = Field(description="Serve on the home network so a paired phone can drive the crawl")
    host: str
    port: int
    refresh_ms: int
    log_lines: int
    chart_hz: tuple[float, float]
    chart_ppo: int
    chart_smoothing: float
    waterfall: WaterfallSettings


class BalanceSettings(_Strict):
    enabled: bool = Field(description="Correct L/R level differences per frequency band (centre image)")
    pair: tuple[str, str] = Field(description="The two mains to balance, left then right")
    band_hz: tuple[float, float] = Field(description="Band where balance is judged; above the sub crossover")
    smoothing: float = Field(description="Fractional-octave smoothing of the L/R difference (3 = 1/3 octave)")
    max_filters: int
    max_gain_db_per_side: float
    min_q: float
    max_q: float
    start_q: float
    min_improvement_db: float
    stop_error_db: float
    peq_slots: list[Annotated[int, Field(ge=0, le=9)]] = Field(description="Mains-output PEQ slots for balance filters (0-9)")
    max_nfev: int
    use_gating: bool = Field(description="Judge balance on the direct sound (gated impulse response), as the ear does")
    max_gate_ms: float = Field(description="Longest window kept after the direct arrival")
    min_gate_ms: float = Field(description="Shortest window, even if a reflection arrives sooner")
    reflection_threshold_db: float = Field(description="A later envelope peak this loud (re direct) closes the gate")
    resolution_cycles: float = Field(description="Balance band starts at cycles / gate length (resolution limit)")
    gate_pre_ms: float
    gate_taper_ms: float


class CrawlGrid(_Strict):
    enabled: bool = Field(description="Lay candidate sub spots out as a lettered grid (A1, B3...) for the crawl")
    origin_from_front_m: float = Field(description="A1 distance from the front wall (also the margin at the back wall)")
    origin_from_left_m: float = Field(description="A1 distance from the left wall (also the margin at the right wall)")
    spacing_m: float = Field(gt=0.1, description="Step between grid points")
    mic_height_m: float = Field(description="Mic height for the crawl: where the sub's woofer will be")
    mode: Literal["perimeter", "full"] = Field(description="perimeter: only cells near walls (where subs go)")
    perimeter_depth_m: float = Field(description="In perimeter mode, how far from a wall a cell may be")
    excluded: list[str] = Field(description="Cells a sub cannot go (furniture, doorways), e.g. ['C1', 'D1']")
    max_cells: int = Field(ge=3, description="Safety cap on how many cells one crawl may visit")


class SetupLibrary(_Strict):
    dir: str | None = Field(description="Folder for named, dated DSP setups; null = <REWDIO_HOME>/setups")
    autosave_on_done: bool = Field(description="Save the finished calibration as a setup automatically")
    thumb_hz: tuple[float, float] = Field(description="Frequency span of a setup's response thumbnail")
    thumb_ppo: int = Field(ge=1, description="Thumbnail points per octave")
    thumb_smoothing: float = Field(description="Thumbnail fractional-octave smoothing")


class SystemConfig(_Strict):
    rew_url: str
    minidsp_url: str = Field(description="minidsp-rs daemon: http://host:5380 or unix:///path/to/minidsp.sock")
    minidsp_device_index: int
    dsp_sample_rate: int = Field(description="miniDSP internal rate: 96000 for 2x4HD/Flex/DDRC-24, 48000 for SHD/2x4")
    dsp_max_delay_ms: float = Field(gt=0, le=80, description="minidsp-rs accepts at most 80 ms")
    dsp_gain_range_db: tuple[float, float]
    peq_slots: list[Annotated[int, Field(ge=0, le=9)]] = Field(description="Output PEQ slots Rewdio may overwrite (0-9)")
    channels: list[Channel]
    rew_output_all: str | None = Field(description="REW output that drives every speaker at once; needed only if "
                                                    "channels name their own rew_output_channel")
    baseline: Baseline
    crossover_hz: float = Field(description="Nominal sub/mains crossover already set in the DSP")
    timing_mode: TimingMode
    timing_reference_channel: str
    mic_positions: list[str] = Field(description="Seats to measure each pass; first is the main listening position")
    do_sub_crawl: bool
    crawl_positions: int = Field(ge=3, le=40)
    crawl_grid: CrawlGrid
    room_dims_m: tuple[float, float, float] | None = Field(description="Length (x, front to back), width (y), height (z)")
    seat_xyz: dict[str, tuple[float, float, float]] = Field(description="Seat coordinates, origin front-left floor corner")
    mic_cal_path: str | None
    mic_sensitivity_dbfs: float | None
    max_sub_gain_db: float = Field(description="Sub output gain ceiling (headroom); mains are cut instead")
    allow_peq: bool
    max_peq_boost_db: float
    analysis_ppo: int
    sim_scenario: str | None = Field(description="Simulator scenario JSON; null = packaged default")
    target: TargetCurve
    convergence: Convergence
    sweep: SweepSettings
    rew_client: RewClientSettings
    balance: BalanceSettings
    dashboard: DashboardSettings
    setups: SetupLibrary
    tuning: Tuning

    @model_validator(mode="after")
    def _check(self) -> SystemConfig:
        roles = [c.role for c in self.channels]
        if len(set(roles)) != len(roles):
            raise ValueError(f"channel roles must be unique: {roles}")
        outs = [c.dsp_output for c in self.channels]
        if len(set(outs)) != len(outs):
            raise ValueError(f"each channel needs its own miniDSP output: {outs}")
        per = [c.rew_output_channel for c in self.channels]
        if any(per) and not all(per):
            raise ValueError("set rew_output_channel on every channel or on none (mixing leaves REW on the wrong output)")
        if any(per) and len(set(per)) > 1 and not self.rew_output_all:
            raise ValueError("channels use different REW outputs, so set rew_output_all (the REW output that plays "
                             "them all, used for combined and full-system sweeps)")
        if self.timing_reference_channel not in roles:
            raise ValueError(f"timing_reference_channel {self.timing_reference_channel!r} is not a channel")
        if not self.mic_positions:
            raise ValueError("need at least one mic position")
        if self.balance.enabled and len(self.mains) >= 2 and not set(self.balance.pair) <= set(roles):
            raise ValueError(f"balance.pair {self.balance.pair} must name two channels")
        return self

    def peq_slots_for(self, role: str) -> list[int]:
        """Subs use peq_slots (bass EQ); mains use balance.peq_slots (L/R balance)."""
        return self.peq_slots if self.channel(role).kind == ChannelKind.SUB else self.balance.peq_slots

    def channel(self, role: str) -> Channel:
        for c in self.channels:
            if c.role == role:
                return c
        raise KeyError(f"no channel {role!r}; channels are {[c.role for c in self.channels]}")

    @property
    def mains(self) -> list[Channel]:
        return [c for c in self.channels if c.kind == ChannelKind.MAIN]

    @property
    def subs(self) -> list[Channel]:
        return [c for c in self.channels if c.kind == ChannelKind.SUB]


PhaseMode = Literal["absolute", "geometric", "magnitude"]


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "seat_xyz":
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def default_dict() -> dict[str, Any]:
    return json.loads(resources.files("rewdio").joinpath("defaults.json").read_text())


def user_config_path() -> Path:
    env = os.environ.get("REWDIO_CONFIG")
    if env:
        return Path(env)
    return Path(os.environ.get("REWDIO_HOME", Path.home() / ".rewdio")) / "config.json"


def load_config(overrides: dict | None = None, user_file: Path | None = None) -> SystemConfig:
    """defaults.json < user config file < overrides."""
    data = default_dict()
    path = user_file or user_config_path()
    if path.exists():
        data = deep_merge(data, json.loads(path.read_text()))
    if overrides:
        data = deep_merge(data, overrides)
    return SystemConfig.model_validate(data)
