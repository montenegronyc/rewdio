"""REW's acoustic timing reference: how the chirp reaches the reference main (README.md, "Acoustic timing reference").

Before every sweep REW plays a short 5-20 kHz chirp on its timing reference output; each measurement is timed against
the chirp's arrival at the mic. Rewdio sends the sweep and the chirp down separate paths: REW's sweep output feeds one
miniDSP input routed to the channels being measured, REW's reference output feeds a second input routed only to the
reference main. A main that is not being measured then plays the chirp and never the sweep.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from .config import SystemConfig


class AcousticTiming(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rew_sweep_output: str = Field(description="REW output channel that carries every sweep in acoustic mode, e.g. 'L'")
    rew_reference_output: str = Field(description="REW timing reference output channel (the chirp), e.g. 'R'")
    sweep_input: int = Field(ge=0, description="0-based miniDSP input the sweep arrives on")
    reference_input: int = Field(ge=0, description="0-based miniDSP input the chirp arrives on")
    listening_routing: dict[str, list[int]] = Field(
        description="The miniDSP inputs that feed each channel when listening (role -> 0-based inputs); restored "
                    "after every run and on any error. Only these two inputs' routes are ever written")
    max_arrival_offset_ms: float = Field(gt=0, description="A sweep's impulse peak further than this from the chirp's "
                                                           "arrival is impossible in a room: the reference was missed")
    reference_self_tolerance_ms: float = Field(gt=0, description="The reference main measured on its own peaks this "
                                                                 "close to t=0 (the chirp came from the same speaker)")


def check_acoustic(cfg: SystemConfig) -> None:
    """Refuse an acoustic-timing config that cannot work (raises ValueError). Called only in acoustic mode."""
    from .config import ChannelKind

    at = cfg.acoustic_timing
    ref = cfg.channel(cfg.timing_reference_channel)
    if ref.kind != ChannelKind.MAIN:
        raise ValueError(f"timing_reference_channel {ref.role!r} is a {ref.kind.value}: REW's acoustic timing chirp is "
                         "a 5-20 kHz sweep, so the reference must be a main (a speaker with a tweeter), never a sub")
    if at.sweep_input == at.reference_input:
        raise ValueError("acoustic_timing.sweep_input and reference_input must differ: the chirp needs its own path")
    if at.rew_sweep_output.strip().lower() == at.rew_reference_output.strip().lower():
        raise ValueError("acoustic_timing.rew_sweep_output and rew_reference_output must be different REW outputs")
    if any(c.rew_output_channel for c in cfg.channels):
        raise ValueError("acoustic timing sends every sweep out acoustic_timing.rew_sweep_output; remove the channels' "
                         "rew_output_channel settings")
    roles = {c.role for c in cfg.channels}  # extra entries are ignored: config files merge into the defaults' map
    missing = sorted(roles - set(at.listening_routing))
    if missing:
        raise ValueError(f"acoustic_timing.listening_routing must say which inputs feed every channel; missing "
                         f"{missing}. Copy it from the miniDSP plugin's routing matrix")
