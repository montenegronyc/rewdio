"""Spoken turntable prompts on the computer running Rewdio (macOS `say`).

Rules, from Lee: never change the system volume or the default output device, and never speak through the
interface that feeds the speaker under test. So `say -a <device>` names its output per call and touches
nothing else, the device has no default (no device = no Mac voice), and the guard below refuses any device
REW's own audio settings mention. If REW's settings can't be read, it stays silent rather than guess.
The phone speaks the same prompts itself; this is the optional second voice.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil

PREFERRED = ("Serena", "Kate", "Stephanie", "Martha")  # en_GB female voices macOS ships or offers for download
QUALITY = ("(Premium)", "(Enhanced)", "")


def pick_voice(listing: str) -> str | None:
    """Best en_GB female voice from `say -v '?'` output: Premium, then Enhanced, then standard."""
    gb = [m.group(1).strip() for m in (re.match(r"^(.+?)\s+en[_-]GB\b", line) for line in listing.splitlines()) if m]
    for q in QUALITY:
        for name in PREFERRED:
            for v in gb:
                if v.startswith(name) and (q in v if q else True):
                    return v
    return gb[0] if gb else None


class Voice:
    def __init__(self, settings, rew):
        self.cfg = settings  # SpeakerPrompts
        self.rew = rew
        self.problem: str | None = None  # why it is silent, for the status report
        self._voice: str | None = None
        self._checked_device: str | None = None

    async def _run(self, *args: str, timeout: float = 20.0) -> str:
        p = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
        return out.decode(errors="replace")

    async def _device_ok(self, device: str) -> bool:
        if self._checked_device == device:
            return True
        try:
            driver = (await self.rew.audio_driver() or "").lower()
            prefix = "/audio/asio" if "asio" in driver else "/audio/java"
            seen = json.dumps(await self.rew.get(prefix)).lower()
        except Exception as e:  # noqa: BLE001 - fail safe: unknown means silent
            self.problem = f"could not read REW's audio settings to rule out {device!r} ({e}); Mac voice off"
            return False
        if device.lower() in seen:
            self.problem = f"{device!r} is in REW's audio settings: it may feed the speaker under test; Mac voice off"
            return False
        self._checked_device, self.problem = device, None
        return True

    async def say(self, text: str) -> bool:
        c = self.cfg
        if c.mute or not c.mac_device or not text:
            return False
        if not shutil.which("say"):
            self.problem = "no `say` on this computer (macOS only); Mac voice off"
            return False
        if not await self._device_ok(c.mac_device):
            return False
        voice = c.mac_voice or self._voice
        if voice is None:
            self._voice = voice = pick_voice(await self._run("say", "-v", "?"))
        try:
            await self._run("say", *(["-v", voice] if voice else []), "-a", c.mac_device, text)
            return True
        except (OSError, asyncio.TimeoutError) as e:
            self.problem = f"`say` failed: {e}"
            return False
