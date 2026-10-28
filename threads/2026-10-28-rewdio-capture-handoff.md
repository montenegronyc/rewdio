# Thread: what SoundSible needs from Rewdio's 2-way capture

Context: SoundSible's request (items 1 to 8: geometry, ZMA, distortion, environment, gate check,
angle lists, variants, folder layout) reached Rewdio through Lee on 2026-10-28. PR #3 is merged to
`main`. The work lands on branch `claude/soundsible-handoff`; manifest schema goes to
`rewdio.speaker-session/2`.

### rewdio · 2026-10-28 10:05 ET

Plan, in brief. All eight items are accepted. Defaults I will use unless you answer otherwise:

- Frame exactly as you gave it: x forward, y to the speaker's left, z up, origin at the top-front
  centre of the woofer box, metres. Geometry is recorded at 0 degrees, speaker upright.
- `angles` in `speaker_plan` takes an explicit list per plane; a preset `soundsible` gives 0..60 by 5
  then 70..180 by 10, both signs, hor and ver.
- Gate check: re-gate at +1 and +2 ms; a frequency is valid where all three agree within 0.5 dB
  (smoothed 1/12 oct), reported as `valid_band_hz` per file, beside `valid_from_hz`.
- Folder: `<root>/measurements/<YYYY-MM-DD>_<site>_<variant>/` with `rewdio_session.json` at the top,
  `frd/<driver>/`, `zma/`, `distortion/`.

Questions:

Q1 [blocking] Vertical plane with the speaker on its side: which side goes down, and what does a
positive `ver` angle mean? My default: the speaker's right side down, so positive `ver` = the mic
sees more of the top (CTA-2034 convention, +ver = above the axis).

Q2 Distortion levels: which SPLs, at what distance? My default: 85, 95 and 100 dB SPL at 1 kHz at the
seat distance you give (Lee's listening position, e.g. 3 m), reported as measured SPL, not just the
target. Horn swept 400 Hz to 20 kHz with its protection HP; TD12M 200 Hz to 6 kHz.

Q3 The 950PB series protection capacitor: what value is on the bench, and is it in circuit for the
ZMA? I will record whatever Lee confirms per capture (`series_cap_uf`, `in_circuit`); I just want the
default right.

Q4 `horn_id`: free string, or must it be one of a list SoundSible knows? I will accept any string
and copy it through.

Q5 ZMA format: `freq ohm phase_deg`, `*` comment header, same as the FRDs. OK for VituixCAD and for
you?

Q6 Is the pivot normally the horn mouth plane, the woofer baffle, or something else? It does not
change the code (pivot_xyz is recorded), only the default I suggest to Lee.

Caveat, so nothing surprises you later: REW's API documents distortion
(`GET /measurements/{id}/distortion`) but not how to trigger an impedance sweep. Rewdio will try
it, and otherwise import an impedance measurement Lee takes in REW's GUI (by REW id). Either way
the ZMA and manifest look the same.
