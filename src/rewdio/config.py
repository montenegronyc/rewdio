"""System configuration: what is plugged in where, and what "done" means.

Everything here is persisted with the session so a calibration can resume after
a restart without re-asking the user how the rig is wired.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class ChannelKind(str, Enum):
    MAIN = "main"
    SUB = "sub"


class Channel(BaseModel):
    """One loudspeaker fed by one miniDSP output."""

    role: str = Field(description="Short name, e.g. L, R, SUB1, SUB2")
    kind: ChannelKind
    dsp_output: int = Field(ge=0, description="0-based miniDSP output index")
    rew_output_channel: str | None = Field(
        default=None,
        description="REW output channel to drive for this speaker. None = leave REW's selection alone "
        "(normally both channels, with the miniDSP doing the soloing).",
    )


class Baseline(BaseModel):
    """DSP values asserted on every Rewdio-managed output before calibrating."""

    gain_db: float = 0.0
    delay_ms: float = 0.0
    invert: bool = False


class TimingMode(str, Enum):
    LOOPBACK = "loopback"  # REW loopback timing reference: absolute phase
    ACOUSTIC = "acoustic"  # REW acoustic timing reference (user-configured in REW)
    SUM_FIT = "sum_fit"  # no reference: relative timing fitted from combined sweeps


class TargetCurve(BaseModel):
    """Target response relative to the mains reference level.

    A low-shelf "house curve": +`bass_boost_db` below `shelf_hz`, flat above.
    """

    bass_boost_db: float = Field(default=4.0, ge=-6, le=12)
    shelf_hz: float = Field(default=70.0, gt=10, lt=300)
    shelf_octaves: float = Field(default=1.5, gt=0.1, le=4, description="Transition width of the shelf")


class Convergence(BaseModel):
    lr_level_tolerance_db: float = 0.5
    min_crossover_efficiency_db: float = -1.0
    max_bass_deviation_db: float = 3.0
    max_prediction_error_db: float = 1.5
    max_iterations: int = 4


class SweepSettings(BaseModel):
    start_hz: int = 10
    end_hz: int = 20000
    length: str = "256k"
    level_dbfs: float = -36.0  # start quiet; gain staging ramps up
    min_level_dbfs: float = -50.0
    max_level_dbfs: float = -6.0
    target_spl_db: float = 80.0  # gain staging target at the seat, 500 Hz-2 kHz for mains / LF band for subs
    max_spl_db: float = 100.0  # REW protection abort
    min_snr_db: float = 40.0


class SystemConfig(BaseModel):
    rew_url: str = "http://127.0.0.1:4735"
    minidsp_url: str = Field(
        default="unix:///tmp/minidsp.sock",
        description="minidsp-rs daemon: http://host:5380 or unix:///path/to/minidsp.sock",
    )
    minidsp_device_index: int = 0
    dsp_sample_rate: int = Field(default=96000, description="miniDSP internal rate: 96000 for 2x4HD/Flex/DDRC-24, 48000 for SHD/2x4")
    dsp_max_delay_ms: float = 8.0
    dsp_gain_range_db: tuple[float, float] = (-72.0, 12.0)
    peq_slots: list[int] = Field(
        default_factory=lambda: [5, 6, 7, 8, 9],
        description="Output PEQ slots Rewdio may overwrite. Leave the rest for your own filters.",
    )
    channels: list[Channel] = Field(
        default_factory=lambda: [
            Channel(role="L", kind=ChannelKind.MAIN, dsp_output=0),
            Channel(role="R", kind=ChannelKind.MAIN, dsp_output=1),
            Channel(role="SUB1", kind=ChannelKind.SUB, dsp_output=2),
            Channel(role="SUB2", kind=ChannelKind.SUB, dsp_output=3),
        ]
    )
    baseline: Baseline = Field(default_factory=Baseline)
    crossover_hz: float = Field(default=80.0, description="Nominal sub/mains crossover already set in the DSP")
    timing_mode: TimingMode = TimingMode.SUM_FIT
    timing_reference_channel: str = Field(default="L", description="Channel every other channel is timed against in sum-fit mode")
    mic_positions: list[str] = Field(
        default_factory=lambda: ["MLP"],
        description="Seats to measure each pass. First is the main listening position.",
    )
    do_sub_crawl: bool = Field(default=False, description="Run the reciprocity crawl to choose sub positions first")
    crawl_positions: int = Field(default=8, ge=3, le=40)
    target: TargetCurve = Field(default_factory=TargetCurve)
    convergence: Convergence = Field(default_factory=Convergence)
    sweep: SweepSettings = Field(default_factory=SweepSettings)
    room_dims_m: tuple[float, float, float] | None = Field(
        default=None, description="Room length (front-to-back, x), width (y), height (z). Enables suggested crawl spots.")
    seat_xyz: dict[str, tuple[float, float, float]] = Field(
        default_factory=dict, description="Coordinates of mic positions/seats in metres (origin: front-left floor corner)")
    mic_cal_path: str | None = Field(default=None, description="SoundID Reference mic cal file to load into REW")
    mic_sensitivity_dbfs: float | None = None
    max_sub_gain_db: float = Field(default=6.0, description="Sub output gain ceiling (headroom); mains are cut instead")
    allow_peq: bool = True
    max_peq_boost_db: float = 3.0
    analysis_ppo: int = 96

    @model_validator(mode="after")
    def _check(self) -> SystemConfig:
        roles = [c.role for c in self.channels]
        if len(set(roles)) != len(roles):
            raise ValueError(f"channel roles must be unique: {roles}")
        outs = [c.dsp_output for c in self.channels]
        if len(set(outs)) != len(outs):
            raise ValueError(f"each channel needs its own miniDSP output: {outs}")
        if self.timing_reference_channel not in roles:
            raise ValueError(f"timing_reference_channel {self.timing_reference_channel!r} is not a channel")
        if not self.mic_positions:
            raise ValueError("need at least one mic position")
        return self

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
