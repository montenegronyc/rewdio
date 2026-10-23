"""Rewdio MCP server: Claude drives REW + a miniDSP to calibrate subs and mains."""

from __future__ import annotations

import os
from typing import Any

from mcp.server.mcpserver import MCPServer

from .analysis.metrics import summarize
from .config import SystemConfig
from .engine.runtime import Runtime

INSTRUCTIONS = """\
Rewdio calibrates a sub + mains system by driving REW (sweeps) and a miniDSP (mute/solo, gain,
delay, polarity, PEQ) in a closed loop.

How to run a calibration:
1. Call `rewdio_status` to check REW and the miniDSP are reachable and to see the config.
2. If the rig differs from the defaults (channel map, crossover, mic positions, timing mode, mic
   cal file, room size), call `configure` once.
3. Call `autopilot_step` repeatedly. It runs every sweep and calculation itself. When it returns
   status `user_action_required`, relay `action.instructions` to the user in plain words, wait for
   them to say it is done, then call `confirm_user_action` (pass x/y/z if they give coordinates)
   and call `autopilot_step` again. Stop when status is `done`, then call `get_report`.
4. Only ask the user for physical things (moving the mic or a sub, powering gear). Never ask them
   to click in REW or the miniDSP plugin; the tools do that.

The manual tools (`measure`, `set_channel`, `solo`, `analyze_measurement`) are for spot checks and
for answering the user's questions; the autopilot does not need them.
"""

mcp = MCPServer("rewdio", instructions=INSTRUCTIONS)
_rt: Runtime | None = None


def runtime() -> Runtime:
    global _rt
    if _rt is None:
        _rt = Runtime(os.environ.get("REWDIO_SESSION", "default"))
    return _rt


def _clean(obj: Any) -> Any:
    """Drop private keys (prefixed _) and numpy scalars so results serialise cleanly."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if hasattr(obj, "item") and callable(obj.item):
        return obj.item()
    return obj


@mcp.tool()
async def rewdio_status() -> dict:
    """Connectivity check (REW, miniDSP), current config, workflow phase and any pending user action."""
    rt = runtime()
    s = rt.session
    out: dict[str, Any] = {"simulated": rt.simulate, "session_dir": str(s.dir), "phase": s.workflow.phase,
                           "pass": s.workflow.pass_no, "done": s.workflow.done,
                           "pending_user_action": s.workflow.pending_action,
                           "measurements": len(s.measurements), "config": s.config.model_dump(mode="json")}
    try:
        out["rew"] = {"version": await rt.rew.version(), "timing_reference": await rt.rew.timing_reference(),
                      "mic_cal": ((await rt.rew.input_cal()).get("calDataAllInputs") or {}).get("calFilePath")}
    except Exception as e:  # report, don't fail: the user may be mid-setup
        out["rew"] = {"error": str(e)}
    try:
        out["minidsp"] = {"devices": await rt.dsp.devices(), "status": await rt.dsp.status()}
    except Exception as e:
        out["minidsp"] = {"error": str(e)}
    return _clean(out)


@mcp.tool()
async def configure(updates: dict) -> dict:
    """Update the rig configuration (merged into the current one, validated, persisted).

    Common keys: channels [{role, kind: main|sub, dsp_output, rew_output_channel}], crossover_hz,
    timing_mode (loopback | acoustic | sum_fit), mic_positions ["MLP", ...], do_sub_crawl,
    crawl_positions, room_dims_m [L, W, H], seat_xyz {"MLP": [x, y, z]}, mic_cal_path,
    mic_sensitivity_dbfs, dsp_sample_rate, dsp_max_delay_ms, peq_slots, max_sub_gain_db,
    target {bass_boost_db, shelf_hz}, convergence {...}, sweep {...}, rew_url, minidsp_url.
    """
    rt = runtime()
    merged = rt.session.config.model_dump(mode="json")
    for k, v in updates.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    rt.session.config = SystemConfig.model_validate(merged)
    rt.session.save()
    await rt.reconnect()
    return {"ok": True, "config": rt.session.config.model_dump(mode="json")}


@mcp.tool()
async def autopilot_step(max_sweeps: int = 60) -> dict:
    """Advance the calibration as far as possible without the user.

    Returns status: `user_action_required` (relay action.instructions, then confirm_user_action),
    `paused` (sweep budget spent; call again), or `done` (call get_report).
    """
    rep = await runtime().autopilot.step(max_sweeps=max_sweeps)
    return _clean(rep)


@mcp.tool()
async def confirm_user_action(key: str | None = None, x: float | None = None, y: float | None = None,
                              z: float | None = None, note: str | None = None) -> dict:
    """Tell the autopilot the user finished the pending physical task. Pass x/y/z (metres) if known."""
    rt = runtime()
    pending = rt.session.workflow.pending_action
    xyz = [x, y, z] if None not in (x, y, z) else None
    if pending is None:
        return {"ok": False, "message": "nothing is waiting on the user"}
    sim = rt.apply_sim_effects(pending, xyz)
    res = rt.autopilot.confirm(key, xyz, note)
    if sim:
        res["simulated_effects"] = sim
    return _clean(res)


@mcp.tool()
async def get_report() -> dict:
    """Final (or latest) calibration result: applied settings, measured vs predicted metrics, notes."""
    rt = runtime()
    wf = rt.session.workflow
    st = rt.session.dsp
    settings = {c.role: {"dsp_output": c.dsp_output, **st.get(c.role).model_dump(mode="json")}
                for c in rt.session.config.channels}
    return _clean({"done": wf.done, "phase": wf.phase, "passes": wf.pass_no + 1,
                   "sweeps": len(rt.session.measurements), "applied_settings": settings,
                   "latest_pass": wf.last_proposal, "best_pass": wf.best and {k: wf.best[k] for k in ("pass", "metrics")},
                   "crawl": wf.crawl.get("result")})


@mcp.tool()
async def reset_calibration(reassert_baseline: bool = True) -> dict:
    """Start the workflow over (measurements are kept on disk). Optionally put the DSP back to baseline."""
    rt = runtime()
    rt.session.reset_workflow()
    if reassert_baseline:
        await rt.rig.assert_baseline()
    return {"ok": True, "phase": rt.session.workflow.phase}


@mcp.tool()
async def measure(roles: list[str], position: str = "MLP", label: str | None = None) -> dict:
    """Run one sweep with only `roles` playing (others muted via the miniDSP) and summarise it."""
    rt = runtime()
    for r in roles:
        rt.session.config.channel(r)
    rec, resp = await rt.rig.measure(roles, "manual", position, title=label)
    return _clean({"id": rec.id, "title": rec.title, "snr_db": rec.extra.get("snr_db"),
                   "summary": summarize(resp, rt.session.config.crossover_hz)})


@mcp.tool()
async def analyze_measurement(measurement_id: str) -> dict:
    """Summary of a stored measurement: band levels, peaks, nulls and a 1/3-octave curve."""
    rt = runtime()
    rec = next((m for m in rt.session.measurements if m.id == measurement_id), None)
    if rec is None:
        return {"error": f"unknown measurement {measurement_id}",
                "known": [m.id for m in rt.session.measurements[-10:]]}
    return _clean({"record": rec.model_dump(mode="json"),
                   "summary": summarize(rt.session.response(rec.id), rt.session.config.crossover_hz)})


@mcp.tool()
async def list_measurements(limit: int = 30) -> list[dict]:
    """Most recent measurements Rewdio took (id, kind, channels, position, pass)."""
    ms = runtime().session.measurements[-limit:]
    return [{"id": m.id, "title": m.title, "kind": m.kind, "roles": m.roles, "position": m.position,
             "pass": m.pass_no} for m in ms]


@mcp.tool()
async def set_channel(role: str, gain_db: float | None = None, delay_ms: float | None = None,
                      invert: bool | None = None, mute: bool | None = None) -> dict:
    """Directly set one channel's miniDSP gain, delay, polarity or mute (tracked in the shadow state)."""
    rt = runtime()
    cfg = rt.session.config
    cfg.channel(role)
    o = rt.session.dsp.get(role)
    lo, hi = cfg.dsp_gain_range_db
    if gain_db is not None:
        o.gain_db = max(lo, min(hi, gain_db))
    if delay_ms is not None:
        o.delay_ms = max(0.0, min(cfg.dsp_max_delay_ms, delay_ms))
    if invert is not None:
        o.invert = invert
    if mute is not None:
        o.mute = mute
    await rt.rig.write_outputs([role], include_peq=False)
    return {"role": role, **o.model_dump(mode="json")}


@mcp.tool()
async def solo(roles: list[str]) -> dict:
    """Unmute only these channels; mute the rest."""
    rt = runtime()
    for r in roles:
        rt.session.config.channel(r)
    await rt.rig.solo(roles)
    return {"playing": roles}


@mcp.tool()
async def get_dsp_state() -> dict:
    """The shadow state of every Rewdio-managed miniDSP output."""
    return runtime().session.dsp.model_dump(mode="json")


@mcp.tool()
async def set_mic_calibration(cal_file_path: str, sensitivity_dbfs_at_94db: float | None = None) -> dict:
    """Load the SoundID Reference (or any) mic calibration file into REW and remember it."""
    rt = runtime()
    rt.session.config.mic_cal_path = cal_file_path
    rt.session.config.mic_sensitivity_dbfs = sensitivity_dbfs_at_94db
    rt.session.save()
    return _clean(await rt.rew.set_input_cal(cal_file_path, sensitivity_dbfs_at_94db))


@mcp.tool()
async def crawl_report() -> dict:
    """Re-rank the reverse-calculation (reciprocity) crawl data collected so far."""
    rt = runtime()
    if not rt.session.workflow.crawl.get("measured"):
        return {"error": "no crawl measurements yet; set do_sub_crawl and run autopilot_step"}
    return _clean(rt.autopilot.analyze_crawl())


def main() -> None:
    import logging

    logging.getLogger("httpx").setLevel(logging.WARNING)
    mcp.run()


if __name__ == "__main__":
    main()
