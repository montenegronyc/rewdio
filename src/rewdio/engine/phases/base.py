"""Shared plumbing for phase handlers: config access and asking the user for physical tasks."""

from __future__ import annotations

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
