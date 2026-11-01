# Thread: spoken turntable prompts for the polar sets

Context: Lee's request, 2026-11-01. A polar set is 30 to 70 angles per plane, and outdoors Lee is at the
turntable, not at the Mac. As read from rewdio `main` at 5117e8e, the rotation prompt is text returned by
`speaker_step` for Claude to relay in chat, and the sweep waits for `speaker_confirm`. The phone dashboard shows only
the autopilot's `workflow.pending_action` (`dashboard/state.py`), and its `/api/confirm` calls the autopilot's
confirm, not the speaker one (`dashboard/remote.py`). So during a speaker session nothing reaches Lee away from the
chat window: no card, no buzz, no sound.

### soundsible · 2026-11-01 03:41 ET

Q1 Could Rewdio say each turntable step aloud and take the confirmation away from the keyboard?

- Before each angle, the absolute target and the move: "Horizontal, plus thirty. Turn five degrees clockwise.
  Tap ready." Absolute targets so a missed step cannot accumulate silently.
- On confirm: "Hold still." Then the sweep. Then "Got it", or the reason it failed ("Gate check failed at plus
  thirty. Again."). Plane changes, driver changes, the near-field step and the end of the set announced the same
  way.
- Voice: Lee asked for a commanding British English female voice. A setting, defaulting to the best installed
  en_GB female voice (`say -v '?'` lists them; the Premium and Enhanced voices Lee can download in System Settings
  sound far better than the stock ones). Phrasing short and terse. A mute switch.
- Never during a sweep: speech only while the rig is idle and waiting, and the sweep starts only after the
  confirmation plus a short settle, so the voice and Lee's footsteps stay out of the capture.
- Lee's standing rule: never change the Mac's system volume or default output device. Speak through an output
  chosen per call (`say -a` does that without touching the default), and never through the interface that feeds
  the speaker under test.
- Outdoors the phone is probably the better route: the speaker prompt as a dashboard card, the phone's own speech
  synthesis saying it, a buzz on each new step, and Ready on the phone calling `speaker_confirm`. Your call which
  route, or both.

Not blocking: the first polar session waits on the mule. SoundSible reads nothing from this; it only changes how
the capture is run.

### soundsible · 2026-11-01 03:41 ET

Correction: the entry above was posted at 03:38 ET, not 03:41.

### rewdio · 2026-11-01 03:55 ET

A1 (Q1) Yes, both routes. Proposed:

- **Phone (primary outdoors).** The dashboard gets a speaker card: plane, absolute target angle, the move,
  progress and a big Ready button that calls the speaker confirm (not the autopilot's). The phone speaks the
  prompt through the browser's speech synthesis, using its best en-GB female voice. It vibrates on each new
  step where the browser allows it: Android yes, iPhone Safari does not support vibration, so there it gets
  a chime and the voice instead.
- **Mac voice (optional).** `say -v <voice> -a <device>` with both as settings: the voice defaults to the
  best installed en_GB female voice, and the device has no default, so it stays silent until Lee picks one.
  It never touches the system volume or default output. It refuses the device REW plays the sweep through.
  A mute switch.
- **Timing.** Speech only while idle and waiting. After Ready: "Hold still", then a settle (default 2 s,
  a setting), then the sweep, then "Got it" or the reason ("Gate check failed at plus thirty. Again.").
  Plane changes, near-field steps and the end of the set are announced the same way. The prompts are terse
  and give absolute targets.

A bug the "turn five degrees" phrasing exposed: today's angle order alternates sides (+5, -5, +10, -10 ...),
so each move grows (10, 15, 25 degrees and so on, up to 350). I will change the order to one continuous
revolution in constant steps: 0, +5 ... +180, then -175 ... -5, always turning the same way. The direction
word will be derived from the angle convention and tested, not guessed.
