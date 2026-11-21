"""Shared plumbing for phase handlers: config access and asking the user for physical tasks."""

from __future__ import annotations

from ...analysis.quality import critical
from ..rig import Rig
from ..store import Session


class NeedUser(Exception):
    def __init__(self, action: dict):
        super().__init__(action.get("instructions", "user action required"))
        self.action = action


class PhaseBase:
    rig: Rig
    s: Session

    @property
    def cfg(self):
        return self.s.config

    @property
    def wf(self):
        return self.s.workflow

    def _ask(self, key: str, instructions: str, **fields) -> None:
        """Raise NeedUser unless this action was already confirmed."""
        if key in self.wf.crawl.setdefault("_confirmed", {}):
            return
        action = {"key": key, "instructions": instructions, **fields}
        self.wf.pending_action = action
        self.s.save()
        raise NeedUser(action)

    def _confirmed(self, key: str) -> dict | None:
        return self.wf.crawl.get("_confirmed", {}).get(key)

    def _mic_to(self, position: str, why: str, xyz_hint: list[float] | None = None) -> None:
        if self.wf.mic_at == position:
            return
        hint = ""
        if position in self.cfg.seat_xyz:
            hint = f" (coordinates {list(self.cfg.seat_xyz[position])} m)"
        elif xyz_hint:
            hint = f" (suggested coordinates {xyz_hint} m)"
        self._ask(f"mic:{position}:pass{self.wf.pass_no}:{why}",
                  f"Move the measurement mic to {position}{hint}. Ear height, pointing at the ceiling "
                  f"(90° calibration), clear of reflective surfaces. Then confirm.",
                  mic_to=position, xyz_suggestion=xyz_hint)

    async def _gated_measure(self, roles, kind, pos, log, **extra):
        """Measure; on a critical quality finding, drop the sweep and tell the user what to fix. After
        tuning.quality.max_retries failed re-measures of the same sweep the run stops: a dead channel is not
        swept again and again at full level."""
        rec, resp = await self.rig.measure(roles, kind, pos, **extra)
        q = rec.extra.get("quality") or []
        for f in q:
            self.wf.quality.append({"measurement": rec.id, "roles": roles, "position": pos, "kind": kind, **f})
        crit = critical(q)
        if crit:
            self.s.measurements.remove(rec)  # never solve from a bad sweep
            self.s.save()
            failed = {e["measurement"] for e in self.wf.quality if e.get("level") == "critical"
                      and e.get("roles") == roles and e.get("position") == pos and e.get("kind") == kind}
            reasons = "; ".join(f["message"] for f in crit)
            if len(failed) > self.cfg.tuning.quality.max_retries:
                raise RuntimeError(f"the {'+'.join(roles)} sweep at {pos} failed the quality gate {len(failed)} times "
                                   f"({reasons}); stopped instead of sweeping it again. Fix the cause and run the "
                                   "autopilot again")
            self._ask(f"quality:{rec.id}", f"Sweep of {'+'.join(roles)} at {pos} failed the quality gate: "
                      + reasons + ". Fix it and confirm; I will re-measure.", findings=crit)
        for f in q:
            if f["level"] == "warning":
                log.append(f"quality warning ({'+'.join(roles)} @{pos}): {f['message']}")
        return rec, resp
