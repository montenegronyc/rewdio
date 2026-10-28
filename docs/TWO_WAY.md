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

## Config and tool flow

Each driver is a channel of kind `driver` with the speaker it belongs to and the crossover Rewdio
writes. The horn's guard high-pass is marked `protect`, so no write can ever remove it:

```json
"channels": [
  {"role": "LW", "kind": "driver", "speaker": "L", "dsp_output": 0,
   "crossover": [{"kind": "LP", "topology": "LR4", "freq_hz": 1200}]},
  {"role": "LH", "kind": "driver", "speaker": "L", "dsp_output": 1,
   "crossover": [{"kind": "HP", "topology": "LR4", "freq_hz": 1200},
                 {"kind": "HP", "topology": "BW2", "freq_hz": 600, "protect": true}]}
],
"timing_mode": "loopback",
"timing_reference_channel": "LW"
```

Then, from Claude:

1. `speaker_plan(speaker="L", planes=["hor"], step_deg=10, distance_m=2)`
2. `speaker_step` repeatedly. It asks the user to rotate (relay it, then `speaker_confirm`), or
   sweeps every driver raw at the current angle. A clipped or noisy sweep repeats that angle.
3. Optional: `speaker_nearfield(driver="LW")`, `speaker_nearfield(driver="LW", label="port")`.
4. `export_frd(out_dir=..., gate_ms=5)`: FRDs plus `rewdio_session.json`.
5. `speaker_report`: spinorama curves and targets for the measured drivers through the configured
   crossover.

### SoundSible handoff (manifest `rewdio.speaker-session/2`)

The final crossover is passive, so SoundSible also needs impedance and distortion, and enough
geometry to simulate the exact setup. Agreed on the `inbox` branch (`DECISIONS.md`):

- `speaker_plan(..., variant="offset10_riser40", preset="soundsible", geometry={...}, environment={...})`.
  Geometry uses SoundSible's frame (x forward, y to the speaker's left, z up, origin at the top-front
  centre of the woofer box, metres): `pivot_xyz` (default: the origin), `mic_xyz` or
  `mic_distance_m` + `mic_height_m` + `origin_height_m`, `riser_h`, `horn_offset_m`,
  `horn_stem_lift_m`, `horn_id` (a SoundSible variant key or `stock:<name>`), `horn_stl`,
  `series_cap_uf`, `series_cap_in_circuit` (no value given means no cap; while prototyping on the
  miniDSP the protection high-pass does the cap's job). Environment: `temperature_c`, `indoor`, `site`,
  `soundsible_commit`; the start time is recorded in ET.
- Vertical plane: the speaker lies on its right side on the same pivot; positive `ver` = above the axis.
- `speaker_impedance(driver, series_cap_uf=..., cap_in_circuit=False)`: mounted ZMA on a jig. If REW
  refuses an impedance sweep over its API, take it in REW's GUI and pass `rew_measurement_id`.
  For the horn it also gives `protection_cap`: the series cap to fit while running on the miniDSP
  (it covers a DSP that boots or loses its preset into a flat path, and amp turn-on thumps, which
  the protect high-pass cannot). C = 1 / (2 pi f |Z(f)|) at 350 Hz with |Z| from the measurement,
  rounded down to an E12 value. It is not part of the passive network, which VituixCAD designs.
- `speaker_distortion(driver)`: H2/H3/THD at 85, 90 and 95 dB SPL at the mic (seat: 1.778 m). Lee never
  listens above 95 dB, so targets above 95 are refused, as are targets within 3 dB of REW's SPL abort
  limit (`sweep.max_spl_db`).
- Gate check: each FRD reports the band that stays within 0.5 dB when the gate end moves +1/+2 ms
  (`valid_band_hz`), beside the 1/T floor. The ground bounce is checked against the gate.
- `export_frd(root=...)` writes `<root>/measurements/<date>_<site>_<variant>/`: the manifest at the
  top, `frd/<driver>/`, `zma/`, `distortion/`.

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
