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

Every change is predicted, applied, then verified by a real sweep. If prediction and measurement
disagree, it re-measures and re-solves. If another pass won't help, it stops and tells you what
the physical limit is (usually sub placement).

## Requirements

| Piece | Notes |
|---|---|
| Python 3.11+ and [uv](https://docs.astral.sh/uv/) | |
| **REW 5.40+ with the API enabled** | `roomeqwizard -api`, or Preferences > API. **API-triggered sweeps need a [REW Pro upgrade](https://www.roomeqwizard.com/upgrades.html).** Default `http://127.0.0.1:4735`. |
| **[minidsp-rs](https://github.com/mrene/minidsp-rs) daemon** (`minidspd`) | miniDSP has no official API; minidsp-rs is the de facto one. Rewdio talks to its HTTP API on the unix socket `/tmp/minidsp.sock` (default) or `http://127.0.0.1:5380` (enable `[http_server]` in its config). Keep the miniDSP plugin closed while calibrating so only one program is talking to the unit. |
| Measurement mic | SoundID Reference (XLR, phantom power) into your interface. Download its per-serial cal file from Sonarworks and pass it as `mic_cal_path`. |
| Optional: loopback cable | One interface output into a second input, REW timing reference set to loopback. Gives absolute phase and the most reliable sub crawl. Without it, Rewdio uses **sum-fit** timing (below). |

Assumed signal chain: REW > interface (or the miniDSP's own USB audio) > miniDSP inputs >
outputs L, R, SUB1, SUB2 > amps. Change the channel map with `configure` if yours differs.

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

### Things to know

- minidsp-rs can't read back per-output settings, so Rewdio keeps a shadow state and **asserts a
  baseline** (gain 0, delay 0, normal polarity, unmuted) on the outputs it manages when a run
  starts. Your crossovers and any PEQ slots outside `peq_slots` (default 5-9) are left alone.
- Defaults assume a 2x4HD: 96 kHz internal rate and an 8 ms delay ceiling. Set `dsp_sample_rate`
  (48000 for SHD/2x4) and `dsp_max_delay_ms` for other units; PEQ coefficients depend on the rate.
- If the subs can't reach the target with `max_sub_gain_db` of headroom, Rewdio cuts the mains
  instead and tells you to raise the master volume.
- Session state lives in `~/.rewdio/<session>` (`REWDIO_HOME`, `REWDIO_SESSION`), so a run can
  resume after a restart.

## Development

```bash
uv sync --extra dev
uv run pytest -q
```

The test suite runs full calibrations against the simulator, including the crawl, two-seat runs
and loopback mode, plus a ground-truth check that the reciprocity ranking matches actually moving
the subs. See [docs/PLAN.md](docs/PLAN.md) for the design.

## License

MIT
