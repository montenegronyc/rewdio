# rewdio-mcp

An MCP server that lets Claude calibrate a subwoofer + mains system on its own. Claude drives
[REW](https://www.roomeqwizard.com) (sweeps, data) and a miniDSP (mute/solo, gain, delay,
polarity, PEQ), reads every result, and keeps looping until level, timing, phase and response
targets are met. You only get asked to do physical things: move the mic, move a sub.

## What it does

1. **Runs sweeps itself.** Configures REW's sweep, level, timing reference, protection limits and
   mic calibration (Sonarworks SoundID Reference cal file) through REW's API, gain-stages to a
   target SPL, then measures.
2. **Switches channels on and off.** Each sweep solos the speakers it needs by muting the other
   miniDSP outputs (L only, R only, each sub only, L+sub, everything).
3. **Sets delay, level and polarity on the miniDSP.** Mains are level-matched and time-aligned;
   multiple subs are integrated with each other; the sub bundle is levelled to a house curve and
   aligned to the mains through the crossover.
4. **Reverse-calculates dual-sub placement.** By acoustic reciprocity, putting the sub at the seat
   and moving the mic through candidate sub spots gives the same responses as moving the sub.
   Rewdio then ranks every pair of spots as a coherent sum with its best delay/gain/polarity.
5. **PEQ as a last resort.** Only when alignment alone misses the target. Cut-biased, boosts capped
   at +3 dB, and seat-dependent nulls are left alone.

Every sweep passes a quality gate (SNR, clipping, dead channel, repeatability) before it can feed the
solver. Every change is predicted, applied, then verified by a real sweep. If prediction and measurement
disagree, it re-measures and re-solves. If another pass won't help, it stops and tells you what
the physical limit is (usually sub placement).

## Requirements

| Piece | Notes |
|---|---|
| Python 3.11+ and [uv](https://docs.astral.sh/uv/) | |
| **REW 5.40+ with the API enabled** | `roomeqwizard -api`, or Preferences > API. **API-triggered sweeps need a [REW Pro upgrade](https://www.roomeqwizard.com/upgrades.html).** Default `http://127.0.0.1:4735`. |
| **[minidsp-rs](https://github.com/mrene/minidsp-rs) daemon** (`minidspd`) | miniDSP has no official API; minidsp-rs is the de facto one. Rewdio talks to its HTTP API on the unix socket `/tmp/minidsp.sock` (default) or `http://127.0.0.1:5380` (enable `[http_server]` in its config). Keep the miniDSP plugin closed while calibrating so only one program is talking to the unit. |
| Measurement mic | SoundID Reference (XLR, phantom power) into your interface. Download its per-serial cal file from Sonarworks and pass it as `mic_cal_path`. |
| Recommended: loopback | A tap of the playback signal, taken *before* the miniDSP, into a second interface input, with REW's timing reference set to loopback. It gives absolute phase, 6 fewer sweeps per pass and the most reliable sub crawl. Without it, Rewdio uses **sum-fit** timing (below). See "Wiring" for how. |

Assumed signal chain: REW > DAC or interface > miniDSP analog inputs > outputs L, R, SUB1, SUB2 > amps.
Change the channel map with `configure` if yours differs.

### Wiring

**Record and play on a shared timeline.** Don't play through one USB device and record on an unrelated
one with no reference. Their clocks drift and there's no common time base, which hurts timing in every
mode. Either play and record on one interface, or keep your own DAC and add a loopback tap (below).

**With your own DAC chain** (for example USB-to-S/PDIF converter > DAC > miniDSP), add a small two-input
interface (for example a Scarlett 2i2) for recording only:

```
Computer --USB--> converter --coax--> DAC --RCA--> miniDSP --> amps
                                       |
                       left RCA via Y-splitter --> interface input 2 (line, Inst off, gain min)  <- loopback
Measurement mic --XLR--> interface input 1 (48V)
miniDSP --USB--> computer (control only, for minidsp-rs)
```

- The loopback records the DAC's real output, so the converter and DAC latency drop out. The miniDSP,
  the amps and the distance stay in the measurement; that's what gets aligned.
- On macOS, an Aggregate Device in Audio MIDI Setup (playback device plus interface, with drift
  correction on the interface) removes even the small clock mismatch. Select it in REW for input and
  output.
- S/PDIF DACs can take a moment to lock, clipping the start of a sweep. `sweep.fill_silence_with_dither`
  (on by default) keeps the stream alive, and `sweep.start_delay_s: 2` adds margin.
- Set REW, the converter and the interface to the same sample rate (48 kHz).
- In loopback mode, preflight mutes every miniDSP output, plays pink noise and checks that it arrives on
  the reference input. The room stays silent, and a missing splitter or wrong REW input is caught before
  the first sweep.
- Servo or analog sub plates (for example Rythmik): set the amp's own low-pass to bypass or its maximum,
  phase to 0, and leave all its knobs alone during a run. The miniDSP does the crossover, and Rewdio
  trims level, delay and polarity.

A ready-made config for this layout is in
[`examples/configs/dac-chain-2x4hd-loopback.json`](examples/configs/dac-chain-2x4hd-loopback.json). Copy it to
`~/.rewdio/config.json` and set `mic_cal_path`.

## Install

```bash
git clone https://github.com/montenegronyc/rewdio-mcp && cd rewdio-mcp
uv sync
```

Claude Code:

```bash
claude mcp add rewdio -- uv run --directory /path/to/rewdio-mcp rewdio-mcp
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "rewdio": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/rewdio-mcp", "rewdio-mcp"]
    }
  }
}
```

### Dry run first (no hardware)

Set `REWDIO_SIMULATE=1` in the server's environment. REW and the miniDSP are replaced by a virtual
room (modal sum, two mains, two badly placed subs with unknown latency, a pre-existing crossover)
behind fake REW and minidsp-rs APIs. Claude runs the exact same loop, and your "move the mic"
confirmations move the virtual mic.

```json
"env": { "REWDIO_SIMULATE": "1" }
```

## Using it

Tell Claude something like: *"Calibrate my subs. Crossover is 80 Hz, miniDSP outputs are L, R,
SUB1, SUB2, here's my mic cal file. Do the sub placement crawl first; room is 5.8 x 4.3 x 2.6 m."*

Claude will call `configure`, then loop on `autopilot_step`. When the autopilot needs you it
returns plain instructions ("Move the mic to C3, near the floor, suggested [1.93, 3.95, 0.25] m")
and Claude relays them. After you say done, it calls `confirm_user_action` and carries on.

### Workflow

```
PREFLIGHT      REW + miniDSP checks, mic cal, timing reference, protection; DSP baseline asserted
GAIN_STAGING   ramps sweep level to the target SPL; backs off on clipping/SPL aborts
SUB_CRAWL      (optional) sub at the seat, mic through N candidate spots, ranks placements
BASELINE       every channel solo at every mic position (+ sum-fit sweeps if no loopback)
SOLVE          levels, mains alignment, sub-to-sub, subs-to-mains, optional PEQ; applied to the DSP
VERIFY         full-system sweep(s) vs prediction; converge, re-measure, or stop at a diagnosed limit
```

Typical cost with one seat, four channels, sum-fit timing and no crawl: 11 sweeps per pass (4 solo, 6
combined, 1 verify) plus a few gain-staging sweeps, 1 or 2 passes, one user action. In the simulator a full
run takes about 10 seconds of compute; on real gear it is dominated by sweep length.

### Timing: loopback vs sum-fit

Aligning subs to mains needs every measurement on one time base.

- **Loopback** (`timing_mode: "loopback"`): REW references every sweep to a wired loopback. Phase
  is absolute.
- **Sum-fit** (`timing_mode: "sum_fit"`, default): without a reference, REW puts each impulse
  peak at t=0 and absolute timing is lost. Rewdio measures `ref` alone, `X` alone and `ref+X`
  together, then fits the relative delay that reproduces the measured sum. A combined sweep only
  fixes the relative phase up to its sign, so it takes a second `ref+X` sweep with a known extra
  DSP delay on X; the true answer stays put while the mirror solution moves. Two extra sweeps per
  channel, no extra cabling.

### Reverse calculation (the sub crawl)

Put the sub **at the listening position**, driver near ear height. It doesn't go in the middle
of the room unless that's where you sit. The mic then goes to each candidate sub spot at woofer
height. If you give `room_dims_m`, Rewdio suggests spots around the perimeter. Phase handling
depends on what's available:

| Mode | When | Confidence |
|---|---|---|
| absolute | loopback timing | high |
| geometric | no loopback, but seat and spot coordinates known | medium (bulk delay from distance) |
| magnitude | neither | low (complementary peaks/nulls only) |

### Tools

| Tool | Purpose |
|---|---|
| `rewdio_status` | connectivity, config, phase, pending user action |
| `configure` | channel map, crossover, timing mode, seats, room, targets, limits |
| `autopilot_step` | run the loop until it needs you or finishes |
| `confirm_user_action` | report a physical task done (with optional x/y/z) |
| `get_report` | applied settings, predicted vs measured metrics, notes |
| `crawl_report` | re-rank placement candidates |
| `measure`, `analyze_measurement`, `list_measurements` | manual sweeps and summaries |
| `set_channel`, `solo`, `get_dsp_state` | direct miniDSP control |
| `set_mic_calibration` | load the mic cal file into REW |
| `reset_calibration` | start over (measurements kept) |
| `dashboard_url` | where the live dashboard is running |

Also an MCP prompt, `calibrate`, which walks the model through the loop with explicit pause points,
and three resources: `rewdio://session/report`, `rewdio://session/config` and `rewdio://session/dsp`.

### Live dashboard

While the server runs, open **http://127.0.0.1:8765/**, or run `rewdio-dashboard` to view a session on its
own. It shows:
- the phase, and what Rewdio is waiting on you to do
- bass deviation, crossover summation and prediction error
- measured vs predicted vs target response at each seat
- the miniDSP settings
- quality and cross-check results, the solver's notes, and the log

Styling comes entirely from design tokens in `src/rewdio/dashboard/tokens.json`, which are compiled to CSS
custom properties for light and dark themes. Edit that file to restyle the dashboard; a test fails if
`styles.css` contains a literal colour.

### Safety and checks

- **Before any sweep.** REW's SPL and clipping protection is set and read back. The timing reference and
  mic cal are read back too. The mic input is checked for digital silence.
- **After every sweep.** It checks SNR, clipping and low-frequency coverage, and looks for a silent channel
  (amp off, cable out, wrong output). One repeat sweep checks repeatability. A critical finding stops the
  run with a fix-it instruction; the sweep is never used.
- **Cross-checks against REW itself.** Rewdio's PEQ is loaded into REW's EQ engine (miniDSP profile) and
  REW's predicted response is compared with Rewdio's maths. With loopback timing, REW's alignment tool
  gives a second opinion on sub delay. Disagreements are reported, never applied silently.
- **Diagnostics in the report.** Residual peaks are matched to room modes. Narrow nulls get
  boundary-cancellation (SBIR) hints with the implied distance. With several seats, seat-to-seat spread and
  outlier seats are reported.

### Configuration

Every number lives in JSON; nothing is hard-coded in Python.

1. `src/rewdio/defaults.json` ships every default: rig, sweep, convergence targets, and a `tuning`
   section with every solver band, threshold, grid step and limit.
2. Your file at `~/.rewdio/config.json` (or `$REWDIO_CONFIG`) overrides any subset. A typo in a key is
   rejected, not ignored.
3. The `configure` tool layers per-session changes on top, which are saved with the session.

Example `~/.rewdio/config.json`:

```json
{
  "crossover_hz": 90,
  "mic_cal_path": "C:/cal/SoundID_1234567.txt",
  "room_dims_m": [6.1, 4.2, 2.7],
  "seat_xyz": {"MLP": [3.9, 2.1, 1.1]},
  "target": {"house_curve_path": "C:/cal/my_house_curve.txt"},
  "tuning": {"peq": {"max_cut_db": 9}}
}
```

The simulator's room, speakers and DSP come from a scenario JSON
(`src/rewdio/sim/scenarios/default.json`; point `sim_scenario` at your own).

### Things to know

- minidsp-rs can't read back per-output settings, so Rewdio keeps a shadow state and **asserts a
  baseline** (gain 0, delay 0, normal polarity, unmuted) on the outputs it manages when a run
  starts. Your crossovers and any PEQ slots outside `peq_slots` (default 5-9) are left alone.
- Defaults assume a 2x4HD: 96 kHz internal rate and an 8 ms delay ceiling. Set `dsp_sample_rate`
  (48000 for SHD/2x4) and `dsp_max_delay_ms` for other units; PEQ coefficients depend on the rate.
- If the subs can't reach the target with `max_sub_gain_db` of headroom, Rewdio cuts the mains
  instead and tells you to raise the master volume.
- Long sweeps or DSD/upsampling DACs can eat the start of the sweep. Raise `sweep.start_delay_s`.
- If PEQ cuts push the subs to `max_sub_gain_db`, Rewdio lowers the mains to keep headroom and tells you.
  This can cost several dB of master volume. Raise `max_sub_gain_db` if your sub amps have headroom.
- Session state lives in `~/.rewdio/<session>` (`REWDIO_HOME`, `REWDIO_SESSION`), so a run can
  resume after a restart.

## Development

```bash
uv sync --extra dev
uv run pytest -q
```

The test suite runs full calibrations against the simulator, including the crawl, two-seat runs
and loopback mode, plus a ground-truth check that the reciprocity ranking matches actually moving
the subs. See [docs/PLAN.md](docs/PLAN.md) for the design and
[docs/COMPARISON.md](docs/COMPARISON.md) for how Rewdio compares with every other REW MCP server.
[docs/research/polycam.md](docs/research/polycam.md) evaluates importing a Polycam/RoomPlan room scan.

Every module is kept under 500 lines, and a test enforces it.

## Credits

Rewdio adopts ideas and REW wire knowledge from brandon-fryslie/room-eq-wizard-mcp, koltyj/REW-mcp,
daredoole/audio-calibration-mcp and KevinMeinon/rew-mcp-server, all MIT-licensed. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## License

MIT
