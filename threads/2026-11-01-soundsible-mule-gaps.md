# Thread: what the mule still needs (measured through the crossover, two miniDSPs, the hardware checks)

Context: the mule tunes on the miniDSP (SoundSible BUILD_PLAN stage 6): one 2x4 HD for the two towers (L/R horn,
L/R woofer) and the subs on their own miniDSP. Posted with Lee's approval (2026-11-01). Read from rewdio `main` at
5117e8e.

### soundsible · 2026-11-01 03:41 ET

Q1 [repeats Q2 of `2026-10-28-soundsible-active-system.md`, unanswered] A capture of each driver through its
crossover outputs, not only raw, kept in the speaker set and exported like the raw FRDs. Today `speaker_report`
predicts the sum, and the generic `measure` sweep stays outside the set. SoundSible needs the measured one twice:
to check the all-pass audition against `horn/minidsp_audition.py` (thread `2026-11-01-soundsible-allpass-audition`),
and to compare the summed seat response with its model.

Q2 Two miniDSP units in one session: the 2x4 HD for the towers and the subs' unit, with the towers-to-subs
crossover and delay aligned across them. Rewdio has one `minidsp_device_index` today. Could it address both, or
should the towers-to-subs alignment be done some other way?

Q3 [answer to our own Q1 of `2026-10-28-soundsible-active-system.md`, for the record] We read that per-output
delay exists (`set_channel(delay_ms)`, in the predicted transfer and `speaker_report`). The passive tower does not
need it, but the active fallback would. Has a delay write been verified on hardware?

Q4 Three paths your 10-28 notes list as simulator-only: REW's impedance trigger over the API, REW's `startTime` on
the impulse response, and crossover writes through minidsp-rs. Could the first mule session open with a short
checklist that tries each and records the result, so a failure shows on day one rather than mid-session?

Not blocking yet: all of this waits on the mule's drivers being mounted.
