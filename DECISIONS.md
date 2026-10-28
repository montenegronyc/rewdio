# Shared decisions

One line each, dated (ET), once both sides agree.

- 2026-10-28: Coordinate frame for geometry: x forward, y to the speaker's left, z up, origin at the top-front centre of the woofer box, metres (proposed by SoundSible, adopted by Rewdio).
- 2026-10-28: Manifest schema bumps to `rewdio.speaker-session/2` for the SoundSible handoff fields.
- 2026-10-28: Vertical plane: speaker on its right side, same pivot; positive `ver` = mic above the axis (CTA-2034). `pivot_xyz` recorded once, in the upright frame.
- 2026-10-28: Default pivot = the frame origin (top-front centre of the woofer box); the pivot stays put when the horn moves on its rails (`horn_offset_m` records that).
- 2026-10-28: Series cap: ZMA defaults to `cap_in_circuit: false`; acoustic and distortion captures default to in circuit.
- 2026-10-28: `horn_id` is free text; SoundSible reads a 16-hex variant key or `stock:<name>` (+ `horn_stl`).
- 2026-10-28: ZMA format `freq ohm phase_deg` with `*` header lines; FRD `freq dB phase_deg`, same header style.
- 2026-10-28: Seat distance 1.778 m; distortion defaults 85/95/100 dB SPL at 1 kHz pending Lee (100 needs REW's SPL abort limit raised). SUPERSEDED below.
- 2026-10-28: Example v2 set lives in `examples/v2/` on this branch and is SoundSible's reader fixture.
- 2026-10-28 (Lee): Distortion levels 85, 90, 95 dB SPL at 1 kHz at the seat; never above 95.
- 2026-10-28 (Lee): No series cap value known. `series_cap_in_circuit` is false unless a value is given.
- 2026-10-28 (Lee): the finished system is active: one central miniDSP (Flex HTx, pending probe), horns flush, woofer delay in DSP; no passive crossover.
