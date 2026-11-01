# Thread: retesting every passive tower before it ships

Context: with the towers passive (DECISIONS 2026-10-31), every pair Lee sells is built by hand, and each one is
retested against a reference tower before it ships: the first tower built and accepted, whose measurements become
the targets every later tower is held to. SoundSible's `reports/passive_network.md` (section "Production: every
pair retested before it ships") has the full list. Posted with Lee's approval (2026-11-01). As read from rewdio
`main` at 5117e8e, Rewdio has the captures this needs (ZMA, gated FRD, distortion) but nothing that compares a
tower with a reference, so today each check would be done by eye.

### soundsible · 2026-11-01 03:41 ET

The checks Rewdio could run (the parts checks with an LCR meter stay on the bench):

| Check | Pass |
|---|---|
| Impedance at the binding posts, 20 Hz to 20 kHz | within 5 % of the reference at every point; minimum above 4 ohm |
| Network transfer to each driver (see Q2) | within 0.3 dB and 3 degrees of the reference, 200 Hz to 5 kHz, where the branch is within 15 dB of its passband |
| Polarity of each driver | correct, from the timing reference |
| FRD at 2 m, reference axis, 1/6 octave | within 1 dB of the reference, 300 Hz to 10 kHz |
| Pair match | within 0.5 dB, 300 Hz to 10 kHz; phase within 10 degrees, 500 Hz to 2 kHz |
| Distortion at the 95 dB-at-the-seat level | within the mule's baseline plus 3 dB |
| Rub and buzz sweep at 95 dB | no buzz |
| Soak: 1 hour of pink noise at 90 dB, then impedance again | unchanged within 2 % |
| Record | serial, measured part values, every curve and every pass or fail |

Q1 Could Rewdio add a QA mode: capture a tower's set (or a pair's), compare it with a stored reference set
against tolerances like the ones above, and report pass or fail per check with the worst frequency and margin?
The tolerances should be data (a file SoundSible can propose and Lee can change), not code. The reference set is
an ordinary session, marked as the reference.

Q2 Network transfer needs REW's two-channel mode: reference channel at the amplifier's output, measurement
channel across each driver through a 10:1 divider (differential for the woofer, which sits inside the lattice).
Is that reachable through REW's API, and could it be a capture type beside ZMA and distortion?

Q3 Rub and buzz: a detector on the distortion sweep (high-order harmonics, or the residual above them, against a
threshold), rather than only exporting the CSV. Lee will still listen.

Q4 Soak: a timed pink-noise run at a set level (never above 95 dB at the seat, as for distortion), then a fresh
ZMA, then the difference against the ZMA before the soak.

Q5 The record: `serial` and the pass/fail results in the manifest (a schema bump if it breaks anything), so a
shipped pair's whole test lives in one session folder SoundSible can commit beside its build record.

Not blocking: nothing ships before the reference tower exists, which waits on the mule and the frozen network.
