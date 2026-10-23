# Rewdio build plan

Goal: an MCP server that lets Claude run a full bass-management calibration on its
own. Claude drives REW (sweeps, data) and a miniDSP (mute/solo, gain, delay,
polarity, PEQ), reads the results, and loops until level, timing, phase and
response targets are met. The user is only asked to do physical things: move the
mic, move a sub.

## Hardware and software assumptions

| Piece | How Rewdio talks to it | Notes |
|---|---|---|
| REW 5.40+ | REST API, `http://127.0.0.1:4735` (`-api` flag or Preferences > API) | API-triggered **sweeps need a REW Pro licence**. Reads/analysis do not. |
| miniDSP (2x4HD, Flex, DDRC-24, SHD...) | [minidsp-rs](https://github.com/mrene/minidsp-rs) daemon HTTP API, default `/tmp/minidsp.sock` or `http://127.0.0.1:5380` | miniDSP has no official API. minidsp-rs cannot read per-output config, so Rewdio keeps a shadow state and asserts a baseline before calibrating. |
| Sonarworks SoundID Reference mic | Analog XLR into the audio interface. Cal file loaded into REW via `/audio/input-cal` | Download the per-serial cal file from Sonarworks. |
| Timing reference | REW loopback (preferred) or **sum-fit** (no extra cabling) | See "Timing" below. |

Signal chain assumed: REW -> interface (or the miniDSP's USB audio) -> miniDSP inputs ->
outputs (L, R, SUB1, SUB2...) -> amps/speakers. Rewdio solos a speaker by muting
the other miniDSP outputs, so REW can keep playing the same stimulus every sweep.

## Timing ("sync")

Phase alignment needs every measurement on one time base.

1. **Loopback** (best): one interface output wired to a second input, REW timing
   reference = loopback. Phase is absolute. Required for high-confidence sub crawl.
2. **Sum-fit** (default when no loopback): REW puts each IR peak at t=0, so absolute
   timing is lost. Rewdio measures `ref` alone, `X` alone and `ref+X` together, then
   fits the relative delay/polarity that makes `|ref + X·e^{-jωδ}|` match the measured
   sum. One extra sweep per channel; no extra hardware.
3. Closed loop either way: every DSP change is predicted, applied, then verified with a
   real sweep. If prediction and measurement disagree, Rewdio re-measures and re-solves.

## Reverse calculation for dual-sub placement (reciprocity)

Acoustic reciprocity: swapping source and receiver gives the same transfer function.
So: **put the sub at the listening position** (not the room centre, unless that is
where you sit), then move the *mic* through candidate sub locations at driver height.
Each sweep is "what the seat would hear from a sub here". Rewdio then evaluates every
pair of candidates as complex sums with optimised relative delay, gain and polarity,
and ranks them by flatness at the seat (and across seats if crawls were done for
several seats). Phase modes: `absolute` (loopback), `geometric` (peak-aligned plus
distance/c from entered coordinates), `magnitude` (power-sum fallback). After the user
moves the subs, the normal loop measures the real result.

## Workflow (autopilot state machine)

```
PREFLIGHT -> GAIN_STAGING -> [SUB_CRAWL -> SUB_PLACEMENT] -> BASELINE (per mic position)
  -> TIMING (sum-fit sweeps if no loopback) -> LEVEL_MATCH -> SUB_INTEGRATION (sub-to-sub)
  -> CROSSOVER_ALIGN (subs vs mains) -> VERIFY -> [PEQ -> VERIFY] -> DONE
          ^------------------- re-measure + re-solve if VERIFY fails -----|
```

`autopilot_step` advances as far as it can and stops only when a human must act
(returns `user_action_required` with plain instructions) or the run converges.

Convergence (configurable): L/R level within 0.5 dB; crossover summation within 1 dB of
ideal; bass deviation (20-200 Hz, 1/6 oct, spatial average) within target; predicted vs
measured within 1.5 dB RMS. PEQ is only used when alignment alone cannot hit the target.
It is cut-biased, boosts are capped at +3 dB, and deep position-dependent nulls are never
boosted.

## Code layout

```
src/rewdio/
  config.py          system config (channel map, DSP limits, targets), persisted
  rew/               REW HTTP client + base64 big-endian float codec
  minidsp/           minidsp-rs HTTP client, biquad maths (miniDSP sign convention), shadow state
  analysis/          responses, smoothing, metrics, alignment, sum-fit, placement, PEQ, target curve
  engine/            session store, measurement orchestration, autopilot workflow
  sim/               virtual room (modal sum), fake REW + fake miniDSP over httpx MockTransport
  server.py          MCP tools
tests/               unit tests + full closed-loop calibration against the simulator
```

The simulator is what makes this testable without hardware, and it lets the user dry-run
the whole loop with Claude (`REWDIO_SIMULATE=1`) before touching real gear.

## Milestones

1. Project scaffold, config, codecs, clients.
2. Analysis core (responses, smoothing, metrics, biquads).
3. Alignment, sum-fit, level match, placement, PEQ solvers.
4. Simulator (room, fake REW, fake miniDSP).
5. Engine: session store, measurement orchestration, autopilot.
6. MCP server tools.
7. Tests, including end-to-end autopilot convergence in the simulator.
8. README with setup for REW, minidsp-rs, the mic, and Claude config.
