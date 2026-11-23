"""REW's acoustic timing reference on a miniDSP (README.md, "Acoustic timing reference"; config in config_timing.py).

REW plays a 5-20 kHz chirp on its reference output before every sweep and times the measurement against the chirp's
arrival at the mic. Soloing by mute would silence the reference main whenever another channel is measured, so here
the soloing is done by the miniDSP routing matrix: the sweep input is routed only to the channels being measured,
the chirp input only to the reference main, and the reference main stays unmuted. Every routing change is written
with all outputs muted, and the user's listening routing comes back after a run and on any error.

After each sweep the timing is checked (REW's own description of the reference, then the impulse itself): a sweep
that was not timed against the chirp is rejected, never used.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from ..analysis.frd import arrival_s
from ..config import SystemConfig, TimingMode
from ..config_timing import check_acoustic
from ..minidsp.client import MiniDSPClient
from ..minidsp.state import DspState
from ..rew.client import RewError

if TYPE_CHECKING:
    from .rig import Rig


class TimingReferenceError(RuntimeError):
    """A sweep had no valid acoustic timing reference, or could not have had one."""


def active(cfg: SystemConfig) -> bool:
    return cfg.timing_mode == TimingMode.ACOUSTIC


def require_config(cfg: SystemConfig) -> None:
    """The config check again at run time: settings changed in a session are assigned, not re-validated."""
    try:
        check_acoustic(cfg)
    except ValueError as e:
        raise TimingReferenceError(str(e)) from e


def sweep_routing(cfg: SystemConfig, roles: list[str]) -> dict[str, list[int]]:
    """Inputs per channel for a sweep of `roles`: the sweep input to the measured channels, the chirp input to the
    reference main only. The reference main plays the sweep only when it is itself measured."""
    at, ref = cfg.acoustic_timing, cfg.timing_reference_channel
    return {c.role: sorted(([at.sweep_input] if c.role in roles else []) + ([at.reference_input] if c.role == ref
                                                                            else [])) for c in cfg.channels}


def listening_routing(cfg: SystemConfig) -> dict[str, list[int]]:
    """The user's routing for the two inputs Rewdio writes (other inputs are never touched)."""
    at = cfg.acoustic_timing
    mine = {at.sweep_input, at.reference_input}
    return {c.role: sorted(i for i in at.listening_routing.get(c.role, []) if i in mine) for c in cfg.channels}


async def _write_routing(rig: Rig, want: dict[str, list[int]]) -> None:
    """Write both inputs' routes for every channel. The caller has muted every output first. The shadow is set to
    {} (unknown) before the write and to `want` only after it lands, so a failed or interrupted write is redone."""
    cfg, at = rig.cfg, rig.cfg.acoustic_timing
    patches: dict[int, list[dict]] = {}
    for inp in (at.sweep_input, at.reference_input):
        per_dev: dict[int, dict[int, bool]] = {}
        for c in cfg.channels:
            per_dev.setdefault(cfg.device_of(c.role), {})[c.dsp_output] = inp in want[c.role]
        for dev, enabled in per_dev.items():
            patches.setdefault(dev, []).append(MiniDSPClient.routing_patch(inp, enabled))
    rig.s.dsp.routing = {}
    rig.s.save()
    for dev, p in sorted(patches.items()):
        await rig.dsp.set_routing(p, dev)
    rig.s.dsp.routing = want
    rig.s.save()


async def route(rig: Rig, roles: list[str]) -> None:
    """Acoustic-mode soloing: routes for `roles` (written muted, only when they change), then exactly `roles` and
    the reference main unmuted."""
    cfg = rig.cfg
    require_config(cfg)
    want = sweep_routing(cfg, roles)
    if rig.s.dsp.routing != want or rig.dirty:
        await rig.solo([])  # nothing plays while a route changes
        await _write_routing(rig, want)
    ref = cfg.timing_reference_channel
    await rig.solo(list(roles) + ([] if ref in roles else [ref]))


def require_reference_path(rig: Rig) -> None:
    """Refuse a sweep the chirp could not reach: the reference main muted or not routed from the chirp input."""
    cfg = rig.cfg
    ref, inp = cfg.timing_reference_channel, cfg.acoustic_timing.reference_input
    routed = (rig.s.dsp.routing or {}).get(ref, [])
    if rig.s.dsp.get(ref).mute or inp not in routed:
        why = "muted" if rig.s.dsp.get(ref).mute else f"not routed from miniDSP input {inp + 1}"
        raise TimingReferenceError(f"refusing to sweep: the timing reference main {ref} is {why}, so REW's chirp "
                                   "can't play and the sweep would have no timing reference")


def explain(e: RewError) -> Exception:
    """REW's own failure to find the chirp, said in wiring terms (other REW errors pass through)."""
    if "timing" in str(e).lower():
        return TimingReferenceError(f"REW found no acoustic timing reference ({e}). Check REW's reference output "
                                    "reaches the miniDSP's chirp input, the reference main is on, and REW's Ref level "
                                    "trim is not too low")
    return e


async def check_timing(rig: Rig, ident: str, roles: list[str], snap: DspState) -> dict:
    """Prove this sweep was timed against the chirp, else raise. Two checks: REW's description of the reference
    (when REW gives one), and where the impulse first arrives: the reference main alone must arrive at t=0 (chirp and sweep
    came from one speaker); any other channel within max_arrival_offset_ms of its expected place."""
    cfg, at = rig.cfg, rig.cfg.acoustic_timing
    ir, fs, start, desc = await rig.rew.impulse_response_described(ident)
    if desc is not None and "acoustic" not in desc.lower():
        raise TimingReferenceError(f"REW timed sweep {ident} against {desc!r}, not the acoustic reference")
    if ir.size == 0 or not np.any(np.abs(ir) > 0):
        raise TimingReferenceError(f"sweep {ident} has an empty impulse response: nothing to check its timing on")
    # the first arrival, not the peak: a sub's peak trails its arrival by its group delay and the room's modes
    arrival_ms = 1000.0 * arrival_s(ir, fs, start, cfg.tuning.alignment.arrival_onset_fraction)
    ref = cfg.timing_reference_channel
    ref_delay = rig.s.dsp.get(ref).delay_ms
    if list(roles) == [ref]:
        lo, hi = -at.reference_self_tolerance_ms, at.reference_self_tolerance_ms
    else:  # a channel's own DSP delay moves its peak; the reference's delay moves the chirp
        delays = [snap.get(r).delay_ms - ref_delay for r in roles]
        lo, hi = min(delays) - at.max_arrival_offset_ms, max(delays) + at.max_arrival_offset_ms
    if not lo <= arrival_ms <= hi:
        raise TimingReferenceError(f"sweep {ident} ({'+'.join(roles)}) arrives at {arrival_ms:.1f} ms against the chirp, "
                                   f"outside the possible {lo:.1f}..{hi:.1f} ms: REW missed the timing reference")
    return {"arrival_ms": round(arrival_ms, 3), "rew_reference": desc,
            "checked": "REW's description and the impulse" if desc is not None
            else "the impulse only (REW gave no timing-reference description)"}


async def restore_listening(rig: Rig) -> bool:
    """Put the user's listening routing back (outputs muted while it is written, mutes restored after). Nothing to
    do if Rewdio never touched the routing. Returns True if it wrote."""
    if rig.s.dsp.routing is None:
        return False
    mutes = rig.mutes()
    await rig.solo([])
    await _write_routing(rig, listening_routing(rig.cfg))
    rig.s.dsp.routing = None  # the user's own again
    rig.s.save()
    await rig.set_mutes(mutes)
    return True
