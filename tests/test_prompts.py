"""Spoken prompts and the phone's Ready: speech only while idle, Ready confirms and sweeps on its own, the
Mac voice never uses REW's interface."""

import pytest

from rewdio.dashboard.state import speaker_view
from rewdio.engine.turntable import angles_for
from rewdio.engine.voice import Voice, pick_voice
from test_speaker_capture import _rig

LISTING = """Daniel              en_GB    # Hello! My name is Daniel.
Kate                en_GB    # Hello! My name is Kate.
Serena (Premium)    en_GB    # Hello! My name is Serena.
Samantha            en_US    # Hello! My name is Samantha.
"""


def test_pick_voice_prefers_premium_british_female():
    assert pick_voice(LISTING) == "Serena (Premium)"
    assert pick_voice(LISTING.replace("Serena (Premium)", "Fred            ")) == "Kate"
    assert pick_voice("Samantha  en_US  # hi") is None


class Recorder:
    def __init__(self, log):
        self.log, self.problem = log, None

    async def say(self, text):
        self.log.append(("say", text))
        return True


async def _set(sim_runtime, log):
    rt = sim_runtime()
    _rig(rt)
    await rt.rig.assert_baseline()
    rt.speaker.voice = Recorder(log)
    measure = rt.rig.measure

    async def spy(*a, **k):
        log.append(("sweep", a[0]))
        return await measure(*a, **k)
    rt.rig.measure = spy
    rt.speaker.plan("L", angles_for(["hor"], 90, 90, symmetric=True), 2.0, modes=["raw", "sum"])
    return rt


async def test_speech_only_while_idle_and_in_order(sim_runtime):
    log = []
    rt = await _set(sim_runtime, log)
    await rt.speaker_step()  # prompt for 0
    await rt.speaker_step()  # same prompt again: not repeated
    assert [x for x in log if x[0] == "say"] == [("say", "Horizontal, zero. Face the speaker at the mic. Tap ready.")]
    rt.speaker_confirm()
    await rt.speaker_step()
    kinds = [k for k, _ in log]
    first, last = kinds.index("sweep"), len(kinds) - 1 - kinds[::-1].index("sweep")
    assert "say" not in kinds[first:last]  # nothing spoken between the sweeps of one angle
    assert log[first - 1] == ("say", "Hold still.")
    assert log[-1] == ("say", "Got it. Horizontal, plus 90. Turn 90 degrees clockwise. Tap ready.")


async def test_phone_ready_confirms_and_sweeps_by_itself(sim_runtime):
    log = []
    rt = await _set(sim_runtime, log)
    await rt.speaker_step()
    card = speaker_view(rt)
    assert card["waiting"] and card["prompt"]["key"] == "rotate:0" and card["progress"] == {"done": 0, "total": 2}
    res = rt.speaker_ready("rotate:0")
    assert res["ok"] and res["running"]
    assert rt.speaker_ready("rotate:1")["ok"] is False  # still measuring
    await rt._task
    card = speaker_view(rt)
    assert card["progress"]["done"] == 1 and card["prompt"]["target"] == 90 and card["last"]["status"] == "measured"
    res = rt.speaker_ready("rotate:1")
    await rt._task
    card = speaker_view(rt)
    assert card["done"] and card["last"]["speech"] == "Got it. Set complete."


async def test_mac_voice_refuses_rews_interface_and_unknowns(sim_runtime, monkeypatch):
    rt = sim_runtime()
    cfg = rt.session.config.speaker_prompts.model_copy(update={"mac_device": "UMC404HD"})
    v = Voice(cfg, rt.rew)
    monkeypatch.setattr("rewdio.engine.voice.shutil.which", lambda _: "/usr/bin/say")
    calls = []

    async def run(*a, **k):
        calls.append(a)
        return LISTING
    v._run = run

    async def rew_get(path, **params):
        return {"outputDevice": "UMC404HD 192k", "inputDevice": "UMC404HD 192k"}
    monkeypatch.setattr(rt.rew, "get", rew_get)
    assert await v.say("Hold still.") is False and "REW" in v.problem and not calls

    async def rew_down(path, **params):
        raise RuntimeError("unreachable")
    monkeypatch.setattr(rt.rew, "get", rew_down)
    assert await v.say("Hold still.") is False and "could not read" in v.problem

    async def rew_other(path, **params):
        return {"outputDevice": "Scarlett 2i2"}
    monkeypatch.setattr(rt.rew, "get", rew_other)
    assert await v.say("Hold still.") is True
    assert calls[-1] == ("say", "-v", "Serena (Premium)", "-a", "UMC404HD", "Hold still.")
    v.cfg = cfg.model_copy(update={"mute": True})
    assert await v.say("x") is False
