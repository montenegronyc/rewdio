"""Autopilot: the calibration state machine.

`step()` advances as far as it can without a human. It stops and returns
`user_action_required` only for physical tasks (move the mic, move a sub), and
resumes from the persisted state after `confirm()`. Phase handlers live in
engine/phases/, one module per stage.
"""

from __future__ import annotations

from typing import Any

from . import acoustic
from .loudness import NotHeard, TooLoud
from .phases.base import NeedUser
from .phases.baseline import BaselinePhase
from .phases.crawl import CrawlPhase
from .phases.setup import SetupPhase
from .phases.verify import SolveVerifyPhase
from .rig import Rig

PHASES = ["PREFLIGHT", "GAIN_STAGING", "SUB_CRAWL", "BASELINE", "SOLVE", "VERIFY", "DONE"]


class Autopilot(SetupPhase, CrawlPhase, BaselinePhase, SolveVerifyPhase):
    def __init__(self, rig: Rig):
        self.rig = rig
        self.s = rig.s

    def confirm(self, key: str | None = None, xyz: list[float] | None = None, note: str | None = None) -> dict:
        pa = self.wf.pending_action
        if pa is None:
            return {"ok": False, "message": "nothing is waiting on the user"}
        if key is not None and key != pa["key"]:
            return {"ok": False, "message": f"pending action is {pa['key']!r}, not {key!r}"}
        self.wf.crawl.setdefault("_confirmed", {})[pa["key"]] = {"xyz": xyz, "note": note}
        if pa["key"] == "crawl:place_final":
            self.wf.crawl["placed"] = pa.get("place")
        if pa.get("mic_to"):
            self.wf.mic_at = pa["mic_to"]
            if xyz is not None:
                self.wf.crawl.setdefault("xyz", {})[pa["mic_to"]] = list(xyz)
        self.wf.pending_action = None
        self.s.save()
        return {"ok": True, "confirmed": pa["key"], "action": pa}

    async def step(self, max_sweeps: int | None = None) -> dict:
        """Run until a human is needed, the run converges, or the sweep budget is spent."""
        if not self.cfg.room_channels:  # gain staging would sweep nothing and keep raising the level
            return {"status": "error", "phase": self.wf.phase,
                    "error": "no room channels (mains or subs) in the config: the autopilot calibrates the room "
                             "system. For driver channels use speaker_plan / speaker_step."}
        budget = max_sweeps or self.cfg.tuning.autopilot.max_sweeps_per_step
        log: list[str] = []
        start = len(self.s.measurements)
        was_done = self.wf.done
        try:
            while not self.wf.done:
                if len(self.s.measurements) - start >= budget:
                    return self._report("paused", log, note=f"sweep budget of {budget} used; call again to continue")
                phase = self.wf.phase
                nxt = await getattr(self, f"_phase_{phase.lower()}")(log)
                self.wf.history.append({"phase": phase, "next": nxt, "sweeps": len(self.s.measurements)})
                self.wf.phase = nxt
                self.s.save()
            if await acoustic.restore_listening(self.rig):  # however the run ended, never left on a sweep routing
                log.append("miniDSP routing restored to listening")
            if not was_done and self.cfg.setups.autosave_on_done:
                from . import dsp_setups
                saved = dsp_setups.save(self.s, "Calibrated", source="autopilot")
                log.append(f"saved setup {saved['id']}")
            return self._report("done", log)
        except NeedUser as e:
            return self._report("user_action_required", log, action=e.action)
        except TooLoud as e:  # caught by a quiet probe: silence, then ask for the channel's own gain to come down
            return await self._too_loud(e, log)
        except NotHeard as e:  # the quiet probes heard nothing: never vouch for it, never play it loud
            return await self._not_heard(e, log)
        except Exception as e:  # REW abort, timeout, unreachable miniDSP...: stop safely and say why
            log.append(f"stopped in {self.wf.phase}: {type(e).__name__}: {e}")
            muted = await self._make_safe(log)
            return self._report("error", log, error=f"{type(e).__name__}: {e}", outputs_muted=muted,
                                note="Every output is muted. Fix the cause and call autopilot_step again (it resumes "
                                     "where it stopped), or call unmute_room to listen.")

    async def _too_loud(self, e: TooLoud, log) -> dict:
        n = self.wf.probe_attempts.get(e.role, 0)
        log.append(f"quiet probe: {e} (attempt {n})")
        muted = await self._make_safe(log)
        if n >= self.cfg.tuning.loudness_guard.max_attempts:
            return self._report("error", log, error=f"{e.role} is still {e.over_db:.0f} dB louder than the mains after "
                                f"{n} quiet probes; stopped before it played at full level. Turn its own amp gain "
                                "down and run the autopilot again", outputs_muted=muted)
        action = {"key": f"probe:{e.role}:{self.wf.pass_no}:{n}",
                  "instructions": f"Loudness check: a quiet test sweep says {e.role} would play about {e.over_db:.0f} dB "
                                  f"louder than the mains at the measuring level, so I stopped before it played loud. "
                                  f"Turn {e.role}'s own amp gain down by about {e.turn_down_db} dB (when unsure, more), "
                                  "then confirm: I'll check it again quietly before anything plays at full level.",
                  "role": e.role}
        self.wf.pending_action = action
        self.s.save()
        return self._report("user_action_required", log, action=action)

    async def _not_heard(self, e: NotHeard, log) -> dict:
        n = self.wf.probe_attempts.get(e.role, 0)
        log.append(f"quiet probe: {e} (attempt {n})")
        muted = await self._make_safe(log)
        if n >= self.cfg.tuning.loudness_guard.max_attempts:
            return self._report("error", log, error=f"{e.role} could not be heard by quiet probes after {n} tries; "
                                "stopped before playing it at full level. Check it is on (LED lit, not in standby), "
                                "cabled to its miniDSP output, and its own gain is up, then run the autopilot again",
                                outputs_muted=muted)
        ch = self.cfg.channel(e.role)
        action = {"key": f"probe:{e.role}:{self.wf.pass_no}:{n}",
                  "instructions": f"Loudness check: quiet test sweeps up to {e.loudest_dbfs} dBFS couldn't hear {e.role}, "
                                  "so I won't play it loud. Check it is on and awake (power LED lit; set auto-on to "
                                  f"ON if it has standby), cabled to miniDSP output {ch.dsp_output + 1}, and its own gain "
                                  "is not at minimum. Confirm when done: I'll check it quietly again first.",
                  "role": e.role}
        self.wf.pending_action = action
        self.s.save()
        return self._report("user_action_required", log, action=action)

    async def _make_safe(self, log) -> bool:
        """After a failure nothing should play: REW may still be mid-sweep or have its generator on, and the DSP
        may be half-written. Stop REW and mute every output; listening again is an explicit choice (unmute_room)."""
        try:
            await self.rig.rew.stop_playback()
        except Exception as e:  # noqa: BLE001 - keep going: muting matters more
            log.append(f"could not stop REW ({e}); stop its generator by hand before listening")
        try:
            await self.rig.solo([])
        except Exception as e:  # noqa: BLE001
            log.append(f"could not mute the outputs ({e}); turn the amps down or mute the miniDSP by hand")
            return False
        try:  # muted first, then the listening routing back (acoustic timing re-routes inputs per sweep)
            if await acoustic.restore_listening(self.rig):
                log.append("miniDSP routing restored to listening (outputs still muted)")
        except Exception as e:  # noqa: BLE001
            log.append(f"could not restore the miniDSP routing ({e}); set it back in the miniDSP plugin before "
                       "listening")
        return True

    def _report(self, status: str, log: list[str], **kw) -> dict:
        keep = self.cfg.dashboard.log_lines
        self.wf.log_tail = (self.wf.log_tail + log)[-keep:]
        self.s.save()
        out: dict[str, Any] = {"status": status, "phase": self.wf.phase, "pass": self.wf.pass_no,
                               "sweeps_total": len(self.s.measurements), "log": log, **kw}
        if self.wf.last_proposal:
            out["latest"] = self.wf.last_proposal
        return out

    async def _phase_done(self, log) -> str:
        self.wf.done = True
        return "DONE"
