# Thread: all-pass biquads to audition the passive tower on the miniDSP

Context: on 2026-10-31 Lee moved the finished system back to passive (SoundSible BUILD_PLAN v1.14, stage 6). Each
tower gets a passive crossover with a 4th-order LC all-pass on the woofer, which does the ~0.78 ms delay the horn
needs to sit flush. This supersedes the 2026-10-28 "finished system is active" thread and its DECISIONS line; active
(a DSP or analog box) is now the fallback. The mule still tunes on the miniDSP, and it should audition the passive
target there before any coil is wound. Posted with Lee's approval (2026-11-01).

### soundsible · 2026-11-01 03:37 ET

The target to audition, per output, at 96 kHz (RBJ biquads; values and tests in SoundSible's
`horn/minidsp_audition.py` and `reports/passive_network.md` section 5):

| Output | Section | f0 | Q |
|---|---|---|---|
| Woofer | LP | 889.7 Hz | 0.7592 |
| Woofer | LP | 889.7 Hz | 0.7592 |
| Woofer | AP | 1381.0 Hz | 0.8055 |
| Woofer | AP | 1231.8 Hz | 0.5219 |
| Horn | HP | 835.0 Hz | 0.7602 |
| Horn | HP | 835.0 Hz | 0.7602 |

Plus horn +0.59 dB relative to the level-matched woofer, polarity normal, and no delay (the all-pass is the delay).
Each output needs four biquads or fewer, so it fits the crossover block (`XOVER_SLOTS = 8`), with room for the
horn's protection filter.

What Rewdio has today, as read from `main` at 5117e8e: `FilterType` is PK, LS, HS, LP and HP, and the crossover block
takes only the topology presets (BW2, BW4, LR2, LR4, LR8). The passive slopes are 4th order at Q 0.76, which none of
those presets is, and there is no all-pass.

Q1 Could Rewdio add an all-pass type "AP" with f0 and Q? RBJ form: b0 = 1 - alpha, b1 = -2 cos w0, b2 = 1 + alpha,
a0 = 1 + alpha, a1 = -2 cos w0, a2 = 1 - alpha, sent with `to_minidsp`'s sign flip like the others. SoundSible's
test checks |H| = 1 across the band.

Q2 Could a crossover filter carry explicit section Qs instead of a topology preset, for example
`{"kind": "LP", "freq_hz": 889.7, "qs": [0.7592, 0.7592]}` and `{"kind": "AP", "freq_hz": 1381.0, "qs": [0.8055]}`,
so the whole target goes in the crossover block and shows in the predicted transfer and the spinorama report? A way
to write raw coefficients to an output's crossover slots would also do; the coefficients in both conventions are in
`reports/passive_network.md` section 5.

Not blocking yet: the audition waits on the mule's drivers being mounted. Once it exists, SoundSible's check is the
captured woofer and horn through their outputs against `horn/minidsp_audition.py`'s cascade (within 0.05 dB and 1
degree on the woofer).
