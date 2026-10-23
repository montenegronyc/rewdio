"""MCP prompts and resources (pattern from koltyj/REW-mcp, MIT): a guided `calibrate` prompt with
explicit pause points, and read-only resources the client can attach as context."""

from __future__ import annotations

import json
from typing import Callable

from mcp.server.mcpserver import MCPServer

CALIBRATE = """\
You are calibrating a subwoofer + mains system with the Rewdio tools. {goal}

Loop:
1. `rewdio_status`. If REW or the miniDSP shows an error, explain the fix in one or two plain sentences and stop.
2. If anything about the rig is unknown (crossover, channel map, mic cal file, room size, seats), ask once,
   then `configure`.
3. Call `autopilot_step` until status is `done`.
   - `user_action_required`: PAUSE. Tell the user exactly the physical task in `action.instructions`
     (one short paragraph, include coordinates if given). Wait for them. Then `confirm_user_action`
     (pass x/y/z if they gave any) and continue.
   - `paused`: call `autopilot_step` again.
   - Quality-gate actions (key starts with `quality:` or `mic_dead`) mean a sweep was unusable; relay the fix.
4. `get_report`. Summarise: applied settings per channel, measured bass deviation and crossover summation,
   whether it converged or stopped at a physical limit, and the diagnostics (room modes, boundary nulls,
   seat-to-seat spread) that explain what is left. Mention any cross-check disagreement with REW.
Never ask the user to click anything in REW or the miniDSP plugin; the tools do that.
Never call a predicted result a measured one.
"""


def register(mcp: MCPServer, runtime: Callable) -> None:
    @mcp.prompt(name="calibrate", description="Guided autonomous calibration of subs + mains")
    def calibrate(goal: str = "Get the flattest bass at the main seat with seamless sub/mains integration.") -> str:
        return CALIBRATE.format(goal=goal)

    @mcp.resource("rewdio://session/report", name="report", mime_type="application/json",
                  description="Latest calibration report: settings, metrics, diagnostics")
    def report() -> str:
        wf = runtime().session.workflow
        lp = {k: v for k, v in (wf.last_proposal or {}).items() if not k.startswith("_")}
        return json.dumps({"phase": wf.phase, "done": wf.done, "pass": wf.pass_no, "latest": lp,
                           "quality": wf.quality}, indent=1, default=str)

    @mcp.resource("rewdio://session/config", name="config", mime_type="application/json",
                  description="Effective configuration (defaults + user file + session overrides)")
    def config() -> str:
        return runtime().session.config.model_dump_json(indent=1)

    @mcp.resource("rewdio://session/dsp", name="dsp", mime_type="application/json",
                  description="Shadow state of every Rewdio-managed miniDSP output")
    def dsp() -> str:
        return runtime().session.dsp.model_dump_json(indent=1)
