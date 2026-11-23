# Rewdio

## Inbox: check it on every turn

The `inbox` branch is a written channel with the Claude session working on SoundSible (the speaker
simulator that consumes Rewdio's capture sets). At the start of every turn, and before finishing one:

```
git fetch origin inbox
git log --oneline origin/inbox -5
git show origin/inbox:threads/<file>.md   # read any thread with new entries
```

- Answer every open question addressed to Rewdio, in a new entry in that thread.
- Act on answers to Rewdio's questions (they may change defaults or the manifest schema).
- Follow the protocol in the branch's `README.md`: append only, `### rewdio · YYYY-MM-DD HH:MM ET`
  headers with the time checked (America/New_York), numbered Q/A, agreed format changes in
  `DECISIONS.md`.
- Write through a separate worktree (`git worktree add <dir> inbox`) so the working branch is not
  disturbed; push right after writing.
- Lee's word overrides either session.

## Project notes

- Open `docs/SAFETY_AUDIT.md` before touching gain staging, sweep levels, DSP write ordering, crossover tools or
  anything that decides what reaches a speaker: it lists the open speaker-safety hazards and the fix order.

- Open README.md "Acoustic timing reference" before enabling `timing_mode: "acoustic"` or touching
  `engine/acoustic.py`, `config_timing.py` or miniDSP input routing: wiring, REW settings, and why soloing there is
  done by routing rather than muting.
- Open `cal_files/2026-11-23-room-numbers-vs-last-year.md` (local, untracked) before re-measuring the room or comparing a
  new calibration with earlier ones: numbers to watch with targets, year-on-year tables, and where the REW exports are.
  `cal_files/2026-11-21-calibration-report.md` holds the settings that were tuned by ear and why.

- Tests: `uv sync --extra dev && uv run pytest -q` (the full suite takes about 3.5 minutes; the
  speaker and FRD tests run in seconds: `uv run pytest -q tests/test_speaker_capture.py tests/test_frd.py`).
- Speaker measurement work (2-way drivers, VituixCAD, SoundSible handoff) is described in
  `docs/TWO_WAY.md`. The manifest schema is `rewdio.speaker-session/N`; bump N for any breaking change.
