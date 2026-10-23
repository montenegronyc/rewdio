"""Autopilot: the calibration state machine.

`step()` advances as far as it can without a human. It stops and returns
`user_action_required` only for physical tasks (move the mic, move a sub), and
resumes from the persisted state after `confirm()`. Phase handlers live in
engine/phases/, one module per stage.
"""

from __future__ import annotations

from typing import Any

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
            if not was_done and self.cfg.setups.autosave_on_done:
                from . import dsp_setups
                saved = dsp_setups.save(self.s, "Calibrated", source="autopilot")
                log.append(f"saved setup {saved['id']}")
            return self._report("done", log)
        except NeedUser as e:
            return self._report("user_action_required", log, action=e.action)
        except Exception as e:  # REW abort, timeout, unreachable miniDSP...: stop safely and say why
            log.append(f"stopped in {self.wf.phase}: {type(e).__name__}: {e}")
            restored = await self._restore_listening(log)
            return self._report("error", log, error=f"{type(e).__name__}: {e}", outputs_restored=restored,
                                note="Fix the cause and call autopilot_step again; it resumes where it stopped.")

    async def _restore_listening(self, log) -> bool:
        """Never leave the system soloed after a failure: unmute everything (current settings kept)."""
        try:
            await self.rig.unmute_all()
            return True
        except Exception as e:
            log.append(f"could not unmute outputs ({e}); check the miniDSP before listening")
            return False

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
