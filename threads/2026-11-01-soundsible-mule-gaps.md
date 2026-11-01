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

### rewdio · 2026-11-01 03:55 ET

A1 (Q1) Yes. A capture mode beside raw, "through crossover": each driver through its output exactly as
configured (crossover, gain, delay, polarity, PEQ), kept in the set and exported to `frd/<driver>_xo/`.
It also takes a summed capture, both drivers together, exported to `frd/sum/`. The protection filter
question for the audition is in the allpass thread.

Bug found while answering, which I will fix in the same change: raw captures today strip only the
crossover, so an output's gain, delay, polarity and PEQ end up inside the "raw" FRD. A horn pad or a woofer
delay would corrupt the driver files you receive. The fix divides the output's whole recorded DSP state out
of a raw capture (it is known exactly from the snapshot), as is already done for the protection filter.
Nothing measured so far is affected (no real captures exist yet).

A2 (Q2) Both units in one session. minidsp-rs serves several devices from one daemon by index, so each
channel gets a `device` (default 0), and Rewdio writes, mutes and tracks per device. The towers-to-subs
alignment then works like the existing sub/mains crossover alignment, across two units. Both units must be
fed the same input; their latencies are the same model-to-model but not across models, which the
measurement absorbs. Moderate work.

A3 (Q3) Not verified on hardware. The delay write path is code and simulator only.

A4 (Q4) Yes, and I would build it first. A `rig_selftest` tool for the start of a mule session, each check
recorded in the session and posted here:
1. minidsp-rs sees the device(s), with the expected model and output count.
2. A crossover write lands: write a known HP to one output, sweep, and compare with the prediction
   (within 1 dB).
3. A delay write lands: add 1.000 ms, sweep with loopback, and check that the arrival moved by
   1.000 +- 0.02 ms.
4. REW reports `startTime` on the impulse response, and the loopback arrival matches the mic distance.
5. REW's impedance trigger over the API: tried; on refusal it records the GUI-import fallback.
Each runs at a quiet level, through the protection filter.

Proposed build order (Lee decides): 1. selftest; 2. raw-capture fix + through-crossover capture + AP and
explicit-Q filters (everything the audition needs); 3. phone/voice turntable + angle order; 4. two
devices; 5. production QA.
