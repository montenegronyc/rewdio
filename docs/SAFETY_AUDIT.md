# Speaker-safety audit (2026-11-20)

Prompted by the first real calibration run (single Rythmik sub + mains, miniDSP 2x4HD, REW 5.40). The question:
what in Rewdio's code could send a signal that harms a driver? Read-only audit of `src/rewdio/`, the persisted
session (`~/.rewdio/default`) and minidsp-rs's `Output::apply` ordering; the critical findings were checked against
the code by a second reader, and #1 was reproduced in the simulator (dead mic → staging climbs −36 → −6 dBFS).

**Status 2026-11-21: #1–#15 fixed** in two commits (`e69dda2`: #1–#3; the next: #4–#14). Every fix has a test in
`tests/test_staging_safety.py` or `tests/test_speaker_safety.py` that failed on the old code first. What changed:

- **Staging (#1–#3, #13):** stops with "no signal" when a step up isn't heard (`gain_staging.rise_tolerance_db`); stages
  by SNR (`gain_staging.target_snr_db`) when `mic_sensitivity_dbfs` is unset; stops if SNR is still below
  `quality.min_snr_db_critical` at the cap; re-checks a silent input after every "fixed" (`preflight.max_mic_rechecks`);
  always starts at `sweep.level_dbfs`; the config refuses `target_spl_db` within `peak_margin_db + 3` of `max_spl_db`.
- **DSP writes (#5, #6):** settings are written with the output muted, then a mute-only write unmutes; the rig is marked
  out of sync before every write and cleared only after it lands, so cancellation or a killed process can't hide a
  half-written channel (`engine/rig.py` `_commit`).
- **Errors (#7):** stop REW (generator off, measurement cancelled) and mute every output; listening again is the
  `unmute_room` tool (or recalling a setup).
- **Room crossovers (#4, #11):** `set_crossover`, `set_crossovers(raw=True)` and `rig_selftest` refuse mains and subs;
  `apply` (and so setup recall) always takes a room channel's crossover from the config; the self-test plays at most
  at `sweep.level_dbfs`.
- **Gain envelope (#9, #10):** gains are capped per kind (`max_sub_gain_db`, new `max_main_gain_db`) for the solver and
  `set_channel` alike; the config refuses `max_level_dbfs` + largest gain/boost above `sweep.max_output_dbfs` (−3), so
  the default `max_level_dbfs` is now −9; every managed sub gets a protected BW2 high-pass at `sub_infrasonic_hp_hz`
  (15 Hz) (`config_safety.py`).
- **Quality (#8):** crawl sweeps go through the same gate as the baseline; sub-only sweeps are judged on in-band SNR
  (sub band vs `quality.sub_noise_band_hz`); a sweep that fails the gate more than `quality.max_retries` times stops the run.
- **One driver (#12):** a session folder holds a `driver.lock` with the owning pid; a second live process is refused.
- **Loopback noise (#14):** if REW's generator won't stop, the run stops (and the error path mutes everything).

**#9, closed 2026-11-21 evening, after it happened on the real rig.** I had rated the remaining half "not
essential" because other limits bounded the risk to the driver. They didn't bound the loudness: with SNR-based staging
a noisier evening staged at −21.3 dBFS instead of −30, and the sub's first sweep from a corner, about 5.5 dB over the
mains, shook the house. Fixed by `engine/loudness.py`: before a sub's first sweep at each placement, a quiet probe
(`loudness_guard.probe_drop_db` below the run level); if it would play more than `max_above_mains_db` (+3, plus
`crawl_extra_db` in the crawl) over one main, everything is muted and the user is asked to turn its own gain down,
then it is probed again (`max_attempts`, then the run stops). Gain staging also asks before raising the level above
its start (`confirm_raises`). Tests: `tests/test_loudness_guard.py`, each failing on the old code, including a replay
of that evening. Lesson: a listed hazard is not optional because other guards protect the hardware; loudness is a
hazard in itself.

Fixed 2026-11-21, found while testing the fixes above:
- **#15: outputs not in the config were never muted.** A miniDSP output that is routed but not listed as a channel
  (e.g. Out4 with one sub) played every sweep. Now every DSP write also mutes the outputs no channel uses
  (`mute_unlisted_outputs`, on by default; preflight logs which), and listening doesn't unmute them.

## What reached the speakers tonight

- The loud sweeps were mains-only (sub muted during gain staging), with Rewdio's 80 Hz LR4 high-pass written at
  preflight, peaking at −6 dBFS (later capped at −12). Loud, but inside the mains' normal operating range.
- The sub only got crawl sweeps at the staged level (−31.7 dBFS) and test tones at −40/−25 dBFS.
- So no evidence of an over-drive. The hazards below are about what *could* happen.

## Hazards, most severe first

| # | Severity | Hazard | Where |
|---|---|---|---|
| 1 | Critical | Gain staging ramps to the level cap when the mic is dead/unpowered (it reads "too quiet", adds +10 dB per step, never checks that a +10 dB step produced ~+10 dB) | `engine/phases/setup.py:69-88` |
| 2 | Critical | REW's SPL abort is meaningless with an uncalibrated mic (`splOffsetdB` 120: "SPL" = mic dBFS + 120). It tracks the preamp knob, not the speaker. `target_spl_db` likewise: turning the preamp *down* makes Rewdio drive the speakers *harder* | `engine/rig.py:218`, `rew/client.py:201-210`, `config.py:95` |
| 3 | High | Preflight "mic alive" passes on preamp hiss (`max(peak) > -120 dBFS`); confirming the mic warning stores the key so it is never re-checked | `rew/extras.py:34`, `phases/setup.py:33-36`, `phases/base.py:_ask` |
| 4 | High | Mains high-pass is `protect: false`, and `set_crossover` / `rig_selftest` accept *room* channels: `rig_selftest(driver="L")` strips L's HP (`raw=True`) and plays 10 Hz sweeps at the staged level; a failed restore leaves it stripped | `engine/rig.py:145-155`, `engine/selftest.py:100,141`, `server_speaker.py:11-43` |
| 5 | High | A DSP write interrupted by cancellation (Esc / client timeout → `CancelledError`) or a killed process is not marked out-of-sync, so a channel can be left unmuted with a partial crossover while Rewdio's shadow state says otherwise | `engine/rig.py:78-83` |
| 6 | Med-high | Outputs are unmuted in the same write that installs their crossover (minidsp-rs order: mute → gain → PEQ → invert → delay → crossover), so after a reset or USB drop a channel is live before its HP lands | `rig.py:105-131`, `dsp_setups.py:143` |
| 7 | Medium | On any error, the whole room system is unmuted even though REW may still be mid-sweep, at the last staged level | `engine/workflow.py:71-84` |
| 8 | Medium | Baseline retries after a failed quality check re-sweep at the same level with no backoff; the crawl has no quality gate at all (accepted SNR 8 dB tonight) | `phases/baseline.py:25-37`, `phases/crawl.py:75` |
| 9 | Medium | The sub plays from 10 Hz at the level staged on the *mains*; its own sensitivity is never checked and it has no infrasonic HP | `phases/setup.py:67`, `defaults.json` sweep `start_hz` |
| 10 | Medium | `set_channel` allows +12 dB on any channel, bypassing `max_sub_gain_db`; a −6 dBFS sweep + +6 dB sub gain reaches 0 dBFS at the output | `server.py:278-287`, `rig.py:139-140` |
| 11 | Medium | Recalling a saved setup writes the crossover stored in it (possibly stale or empty), unmuted | `dsp_setups.py:143` → `rig.py:125` |
| 12 | Medium | Two Rewdio processes (dashboard + MCP server, or two sessions) can drive the rig at once; the lock is per-process | `engine/runtime.py:24`, `dashboard/app.py:117-128` |
| 13 | Low-med | After a restart, staging starts at the previously staged level rather than the configured start level | `rig.py:23`, `setup.py:69` |
| 14 | Low | Loopback check: pink noise can keep playing if REW's Stop fails (loopback mode only; not in use) | `rew/extras.py:49-51` |

## Fixes, in the order to do them

1. ✅ **Staging that cannot run away (#1, #2, #3, #13):** play one probe at `min_level_dbfs` and require SNR ≥ 15 dB
   before any step up; require each step to raise the measured band by its step ±2 dB, else abort "no signal";
   cap the total rise per run without user confirmation; always start at `sweep.level_dbfs`. Stage by REW's SNR
   with a dBFS ceiling when `mic_sensitivity_dbfs` is unset; validate `target_spl_db ≤ max_spl_db − peak_margin − 3`.
2. ✅ **Mute-first writes (#5, #6, #7):** mute all → write gain/delay/PEQ/crossover → mute-only unmute. Set `unsynced`
   *before* the HTTP call, clear only on success, and catch `BaseException`. On error: mute everything, Cancel REW,
   Stop the generator; unmute only on an explicit request.
3. ✅ **Protect room crossovers (#4, #11):** restrict `set_crossover`, `raw`, and `rig_selftest` to `driver` channels; run
   the selftest at `min_level_dbfs`; on setup recall, room channels take the crossover from config.
4. ✅ **Gain envelope (#10, #9):** clamp per channel kind (sub: `max_sub_gain_db`); enforce level + gain + PEQ boost ≤ −3 dBFS;
   a quiet per-channel first sweep (the "channel alive" check) before any channel is used; a protected infrasonic HP on
   subs (e.g. BW2 15 Hz) or start sub sweeps at 15–20 Hz.
5. ✅ **Quality gates (#8):** crawl goes through the quality gate using in-band SNR; repeats after `low_snr` step down.
6. ✅ **One driver at a time (#12):** OS file lock on the session folder.

## Config to change before the next run (`~/.rewdio/config.json`)

- ✅ Done 2026-11-21: the mains HP and the sub LP are `"protect": true`. Verified in the simulator with this config:
  `set_crossover(raw=True)` keeps the 80 Hz filters on L, R and SUB1, and `set_crossover("L", filters=[])` is refused.
  This blocks #4's filter stripping and #11's stale-crossover recall; the code fixes are still needed for other users.
- `max_spl_db: 120` and `target_spl_db: 100` were set tonight to work around #2; they make the SPL guard equal to the
  clipping guard. Keep only until fix 1 lands, or calibrate the mic's sensitivity and go back to about 80 / 100.
- `max_level_dbfs: -12` is stricter than the default: keep it.
- Don't run `rig_selftest` or `set_crossover` on L, R or SUB1 until fix 3 lands.

## Checked and found safe

Biquad coefficient signs match minidsp-rs; every level is clamped to [min, max]; every sweep solos with a full mute
vector; sub PEQ boost defaults to 0 and solver sub gain is clamped; protected filters are enforced on every write;
Rewdio never writes the master volume; speaker raw/soak/distortion paths require `driver` channels (none configured),
distortion refuses an uncalibrated mic, soak is capped; QA/turntable modules play nothing; no safety check is
simulation-only; the convergence loop is bounded and does not accumulate gain.
