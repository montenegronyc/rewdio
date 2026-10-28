# Phase 2: measuring a 2-way speaker for VituixCAD

Goal: use the same rig (REW + loopback + miniDSP) to measure the drivers of a 2-way speaker
(woofer + horn, each on its own miniDSP output) and hand VituixCAD a polar FRD set with absolute
phase intact. The measured set then replaces the ideal drivers in SoundSible.

This is a separate workflow from the room calibration. The autopilot does not change.

## What already carries over

- REW control, loopback timing reference (absolute phase), the sweep quality gate.
- Solo by mute: one output plays, the rest are muted, REW plays the same stimulus every sweep.
- The shadow DSP state (minidsp-rs cannot read back, so Rewdio tracks every write).
- Biquad maths in the miniDSP sign convention.

## Additions, in build order

1. **Crossover writes.** Until now the crossover was "the user's own, invisible to Rewdio". A channel
   can now declare `crossover` filters (HP/LP, LR2/LR4/LR8/BW2/BW4). Rewdio writes them into the
   output's two 4-biquad crossover groups, keeps them in the shadow state and includes them in the
   predicted transfer. Channels without `crossover` are left alone, as before.
2. **Protection filters.** A filter marked `protect: true` is never removed. Raw driver captures play
   with only the protection filters active, so a compression driver never sees a full-range sweep.
   The protection filter's exact response is divided out on export.
3. **Raw capture + FRD export.** Gated complex response from the raw impulse, on the loopback time
   base. One common delay (default: mic distance / c) is removed from every file, never a per-file
   re-centre, so relative phase between drivers and angles survives. VituixCAD naming:
   `<speaker>_<driver> hor 30.frd`. In VituixCAD, set driver X/Y/Z offsets to zero: the offsets are
   already in the phase.
4. **Near-field capture.** Gated far-field data indoors is valid only above ~1/gate (a 4 ms gate is
   good from ~250 Hz). Woofer and port near-field sweeps are exported as their own FRDs for
   VituixCAD's Merger tool. Rewdio does not reinvent the merge.
5. **Polar capture.** A manual-turntable step loop: "rotate to 40 degrees, confirm", then every driver
   is swept at that angle. Same pivot, mic and loopback for every driver and angle. Motorised
   turntable later only if this gets used a lot.
6. **Spinorama targets.** From the polar set: on-axis, listening window, early reflections, sound
   power, predicted in-room response and directivity index (CTA-2034 style). Horizontal-only sets
   are allowed; curves that need the vertical plane say so.
7. **Session manifest.** One JSON file describing every FRD (driver, plane, angle, gate, valid
   band, delay removed, protection removed, DSP state during capture) so SoundSible can load the
   measured drivers in place of the ideal ones.

ZMA files are not produced: with active filters on the miniDSP, VituixCAD does not need driver
impedance. Add them only if a passive version is ever on the table.

## Hardware: is the 2x4HD enough?

**For one speaker while prototyping: yes.** Per output it has 2 crossover groups of 4 biquads
(enough for LR4 plus a protection high-pass plus spare sections) and 10 PEQ, at 96 kHz. A 2-way uses
2 of its 4 outputs.

**For the finished stereo 2-way + 2 subs (6 outputs): do not buy a Flex Eight.** minidsp-rs, which
Rewdio drives the hardware through, has no Flex Eight support (its device probe knows the 2x4HD,
Flex, Flex HTx, DDRC-24, DDRC-88, SHD, 4x10HD, 10x10HD, nanoDIGI and C-DSP 8x12 v2). Buying one would
cut Rewdio off from the DSP.

Recommendation: **Flex HTx** (8 analog in/out, RCA and balanced, 96 kHz, supported by minidsp-rs
with crossover blocks on all 8 outputs). One box, one clock, two spare outputs. The fallback is a
second 2x4HD (cheap, proven in minidsp-rs), but Rewdio then has to drive two devices and you split
the input signal to both, which is more cabling and more to go wrong.

Caveat: the Flex HTx entry in minidsp-rs was added from a user's probe, so confirm `minidsp probe`
and a crossover write work on it before relying on it.
