# Third-party notices

Rewdio re-implements in Python several ideas and REW wire-contract findings from these
MIT-licensed projects. No source was copied verbatim. Attribution is given here and at each
point of use in the code.

| Project | Licence | What Rewdio took |
|---|---|---|
| [brandon-fryslie/room-eq-wizard-mcp](https://github.com/brandon-fryslie/room-eq-wizard-mcp) © 2026 Brandon Fryslie | MIT | Median-baseline peak/null detection with Q and severity (`analysis/metrics.py`). REW wire gotchas: 202-then-poll sweeps, NaN in JSON, input-cal echo rule, smoothing vocabulary, alignment tool by index only, NaN `delay-b`, "Clear command in progress" recovery, `start-delay`. |
| [koltyj/REW-mcp](https://github.com/koltyj/REW-mcp) © 2026 Kolton Jacobs | MIT | Measurement-quality reviewer checks (`analysis/quality.py`). Room-mode correlation and SBIR quarter-wave hints (`analysis/diagnostics.py`). MCP prompt with explicit pause points, plus session resources (`prompts.py`). |
| [daredoole/audio-calibration-mcp](https://github.com/daredoole/audio-calibration-mcp) © 2026 daredoole | MIT | Quality gate thresholds (SNR, clipping, repeatability). Held-out-seat validation of EQ (`analysis/peq.py`). Multi-seat spread and outlier report. Protection and timing settings read back after writing. Settle delay after a sweep, and a timeout scaled to sweep length. Pre-sweep input-level check (`rew/extras.py`). Provenance notes on REW measurements. |
| [KevinMeinon/rew-mcp-server](https://github.com/KevinMeinon/rew-mcp-server) | MIT | REW API reference notes: EQ engine endpoints and equaliser profiles used for the PEQ cross-check, plus alignment-tool command shapes (`rew/extras.py`). |
| [mrene/minidsp-rs](https://github.com/mrene/minidsp-rs) | Apache-2.0 | Rewdio talks to its daemon's HTTP API (it is a runtime dependency you install, not vendored code). |

The MIT licence text, which applies to each of the projects above:

> Permission is hereby granted, free of charge, to any person obtaining a copy of this software and
> associated documentation files (the "Software"), to deal in the Software without restriction,
> including without limitation the rights to use, copy, modify, merge, publish, distribute,
> sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions: The above copyright notice and this
> permission notice shall be included in all copies or substantial portions of the Software.
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.

## Public Sans (bundled web font)

The phone and desktop GUI bundle Public Sans (Light, Medium, SemiBold) via `@fontsource/public-sans`.
Public Sans is by the U.S. Web Design System (USWDS) and is licensed under the SIL Open Font License 1.1
(https://openfontlicense.org). It is a derivative of Libre Franklin.
