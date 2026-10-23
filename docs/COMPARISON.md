# Rewdio vs existing REW MCP servers

Surveyed October 2026: every REW MCP server that GitHub search and MCP registries turned up.
Each was cloned and read in full.

| Server | Language | Tools | Focus |
|---|---|---|---|
| [brandon-fryslie/room-eq-wizard-mcp](https://github.com/brandon-fryslie/room-eq-wizard-mcp) | TypeScript | 90 | Broad typed wrapper over REW's API, plus peak/null, decay and room-mode analysis |
| [koltyj/REW-mcp](https://github.com/koltyj/REW-mcp) | TypeScript | 27 + 4 prompts | Studio monitors and Genelec GLM checks; advisory, with "never give EQ settings" guardrails |
| [daredoole/audio-calibration-mcp](https://github.com/daredoole/audio-calibration-mcp) | Node | about 100 | Conservative EQ for PCs, cars and laptops; exports to JamesDSP, CamillaDSP and APO; heavy evidence gating |
| [KevinMeinon/rew-mcp-server](https://github.com/KevinMeinon/rew-mcp-server) | Python | about 50 | Thin wrappers; ships with a missing module and does not start as published |
| **Rewdio** | Python | 15 + prompt + 3 resources | Closed-loop sub + mains calibration that drives REW **and a miniDSP** |

## The short version

The other four are **instruments for a human or a model to play**. Each exposes REW's controls and
reports what it sees, and the model or user decides what to change and changes it, usually by hand.

**Rewdio is the only one that closes the loop.** It measures, solves, writes the DSP, measures again,
and decides whether it is done. It asks the human only for physical tasks. Nine capabilities exist in
none of the others.

## Where Rewdio goes beyond all four

| # | Capability | Rewdio | The others |
|---|---|---|---|
| 1 | **Writes the DSP hardware** | Gain, delay, polarity, mute and PEQ biquads on a miniDSP, live, via minidsp-rs. Tracks a shadow state that tests check against the device. | None write to hardware. daredoole and KevinMeinon export a REW-format text file for you to import; brandon-fryslie and koltyj only touch REW's own EQ. |
| 2 | **Autonomous measure, solve, apply, verify loop** | `autopilot_step` runs until converged or until a diagnosed physical limit. It restores the best pass if a later one is worse. | None. koltyj's design pauses at every step on purpose; daredoole needs a confirmed plan per action; the others are tool-by-tool. |
| 3 | **Channel soloing by DSP mute** | Every sweep solos exactly the speakers it needs (L, R, each sub, pairs, all) without touching cables or REW channel settings. | None. koltyj has a fixed L, R, Sub order, but you switch speakers yourself. |
| 4 | **Timing without a loopback cable ("sum-fit")** | Recovers relative delay from the magnitude of combined sweeps. It breaks the sign ambiguity with a second sweep that adds a known DSP delay probe. Tested: the true value is recovered to within 0.05 ms, and one sweep alone is correctly flagged as ambiguous. | None. daredoole refuses without an acoustic or loopback reference. koltyj takes delay from the raw phase difference at one frequency, which can be off by whole cycles. |
| 5 | **Reverse-calculation sub placement (reciprocity crawl)** | Sub at the seat, mic at candidate spots. Every pair is ranked as a coherent sum with its best delay, gain and polarity. Candidate spots come from room size. Three phase modes: absolute, geometric, magnitude. **A test checks the ranking against actually moving the subs in the simulator and it picks the true best pair.** | None. koltyj gives generic placement advice from SBIR maths. daredoole has a research optimiser it describes as not deployable. |
| 6 | **Multi-sub plus mains integration as one solve** | Sub-to-sub delay, gain and polarity, sub level to a house curve, then subs aligned to mains across the crossover band. Delays stay within the DSP's limits. When headroom runs out it cuts the mains instead of over-driving the subs. | brandon-fryslie and KevinMeinon call REW's single-pair alignment tool. koltyj handles one sub at a single frequency. daredoole predicts but does not solve. |
| 7 | **PEQ fitted on the complex system, then re-aligned** | Sub PEQ is optimised on mains plus subs times PEQ at every seat, so phase shift near the crossover is accounted for, and the loop re-aligns after PEQ. It cuts more than it boosts, caps boosts, leaves seat-dependent nulls alone, and rejects PEQ that does not help seats it was not fitted on. | brandon-fryslie and KevinMeinon use REW's magnitude auto-EQ. daredoole proposes cut-only filters on magnitude. koltyj by design gives no EQ. |
| 8 | **Predict, then verify against a real sweep** | Each pass reports prediction error (0.02–0.06 dB in simulation). A mismatch triggers re-measuring. A plateau ends the run with an explanation. | daredoole verifies after EQ, but with the user applying it by hand. The others have nothing like this. |
| 9 | **Hardware-free twin of the whole rig** | Modal-room simulator behind fake REW and minidsp-rs APIs. It runs the real client code, so users can dry-run with Claude (`REWDIO_SIMULATE=1`) and CI runs full calibrations. | None. brandon-fryslie wraps REW's room simulator, which predicts curves but does not stand in for the rig. |

## Ideas we took from them, and why ours is more than the sum

Rewdio adopted the best idea from each (see [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)). In
the other projects these are reports for a human. In Rewdio they **act inside the loop**:

| Borrowed idea | Source | What it does in Rewdio |
|---|---|---|
| Measurement-quality review | koltyj, daredoole | Blocks the solver. A dead amp or a sweep with no SNR stops the run with a fix-it instruction, and the sweep is re-measured once you confirm (`test_dead_channel_is_caught_before_solving`). |
| Settings read-back | daredoole | Protection, timing reference and mic cal are verified before any sweep runs unattended. |
| Settle delay, scaled timeout, wedge recovery | daredoole, brandon-fryslie | Lets the loop take dozens of back-to-back sweeps without "measurement in progress" failures. |
| Median-baseline peaks/nulls with Q | brandon-fryslie | Feeds the report and diagnostics. |
| Room-mode and SBIR diagnostics; multi-seat outliers | koltyj, daredoole | Explains the physical limit when the loop plateaus, and points to the next lever: placement, treatment or seats. |
| Held-out-seat validation | daredoole | Applied automatically to PEQ, so EQ that helps one seat at others' expense is discarded. |
| MCP prompt with pause points; resources | koltyj | Pauses only for physical tasks, not for decisions. |
| REW EQ engine and alignment tool | KevinMeinon, brandon-fryslie | Used as **independent cross-checks** of Rewdio's own maths. In simulation the PEQ cross-check agrees to 0.00 dB. Disagreements are reported, never silently applied. |
| Input-level check | daredoole | A preflight gate: if the mic input is digitally silent, the run stops before the first sweep. |

## Results (simulated room, `tests/`)

| Scenario | Sweeps | User actions | Outcome |
|---|---|---|---|
| Subs bunched in one corner, sum-fit, one seat | 25 | 1 (place mic) | Plateau at 3.3 dB std, correctly diagnosed as a placement limit; prediction error 0.02 dB |
| Same room with the reciprocity crawl | 22 | 11 (sub to seat, 8 mic spots, place subs) | **Converged: 1.63 dB std** (20–200 Hz), crossover summation −0.62 dB, prediction error 0.04 dB |
| Two seats | – | mic moves only | Verified across both seats |
| Loopback timing | – | 1 | No combined sweeps needed; REW cross-checks run |
| Dead sub amp | – | 1 fix-it prompt | Caught before solving, recovered after the fix |

## Where the others are still ahead

Being exact means saying what Rewdio does not do:

- **Breadth of REW coverage.** brandon-fryslie exposes about 90 REW operations: RTA, waterfalls,
  spectrograms, RT60, distortion, impedance, stepped sweeps, groups, file import and export. Rewdio uses
  only what the loop needs.
- **Acoustic metrics above the bass region.** koltyj computes EDT, T20 and T30, C50/C80, early
  reflections and the gap before them. Rewdio is focused on the bass region.
- **Other targets and devices.** daredoole covers cars, laptops, JamesDSP, CamillaDSP and Equalizer APO,
  plus FIR design and blind A/B/ABX listening tests. Rewdio targets miniDSP only.
- **Change control.** daredoole hash-binds every plan, signs evidence tokens, and monitors DSP state
  for drift during a sweep. Rewdio checks settings by reading them back but has no mid-sweep drift
  monitor yet.
- **Real-hardware mileage.** brandon-fryslie has fixed bugs found in live use against REW. Rewdio's REW
  payloads follow REW's OpenAPI spec and those findings, but have only run against the simulator so far.

## Next candidates

- **Polycam / RoomPlan import** for room size, seats and furniture-aware crawl spots, which pre-screens
  spots before any mic moves. Worth doing as geometry input, not as acoustic simulation. See
  [research/polycam.md](research/polycam.md).
- A drift monitor during each sweep: poll the minidsp-rs state and cancel the sweep on any change.
- Ringing-mode detection from REW's waterfall, to tell PEQ-worthy resonances apart from nulls.
- Support for CamillaDSP alongside miniDSP. Rewdio already separates the DSP client from the solvers.
