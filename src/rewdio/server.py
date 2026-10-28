"""Rewdio MCP server: Claude drives REW + a miniDSP to calibrate subs and mains."""

from __future__ import annotations

import contextlib
import os
from typing import Any

from mcp.server.mcpserver import MCPServer

from .analysis.metrics import summarize
from .dashboard.app import serve_in_background
from .engine import dsp_setups, setup
from .engine.runtime import Runtime
from .minidsp.state import XoverFilter
from .prompts import register as register_prompts

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

_rt: Runtime | None = None
_dashboard_url: str | None = None


@contextlib.asynccontextmanager
async def lifespan(_server):
    """Start the live dashboard next to the MCP server when enabled in config."""
    global _dashboard_url
    d = runtime().session.config.dashboard
    if not d.enabled:
        yield {}
        return
    async with serve_in_background(runtime, "0.0.0.0" if d.allow_lan else d.host, d.port) as url:
        _dashboard_url = url
        yield {}


mcp = MCPServer("rewdio", instructions=INSTRUCTIONS, lifespan=lifespan)


def runtime() -> Runtime:
    global _rt
    if _rt is None:
        _rt = Runtime(os.environ.get("REWDIO_SESSION", "default"))
    return _rt


register_prompts(mcp, runtime)


def _clean(obj: Any) -> Any:
    """Drop private keys (prefixed _) and numpy scalars so results serialise cleanly."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if hasattr(obj, "item") and callable(obj.item):
        return obj.item()
    return obj


BUSY = {"ok": False, "message": "a calibration step is running (from the phone or another call); try again when it finishes"}


@contextlib.asynccontextmanager
async def exclusive(rt):
    """Manual tools must not interleave with a running autopilot step: refuse rather than wait."""
    if rt.busy:
        yield False
        return
    async with rt.lock:
        yield True


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
async def dashboard_url() -> dict:
    """URL of the live dashboard (open it in a browser to watch the calibration)."""
    d = runtime().session.config.dashboard
    if not d.enabled:
        return {"enabled": False, "hint": "configure {'dashboard': {'enabled': true}} and restart the server"}
    from .dashboard.remote import pairing_url
    return {"enabled": True, "url": _dashboard_url or f"http://{d.host}:{d.port}/ (port was busy; run rewdio-dashboard)",
            "phone_pairing_url": pairing_url(runtime()) if d.allow_lan else None,
            "phone_hint": None if d.allow_lan else "set dashboard.allow_lan to true to drive the crawl from a phone"}


@mcp.tool()
async def setup_status() -> dict:
    """The setup wizard: every step with its current values, plus what's still missing. Walk the user
    through it one step at a time (see the `setup` prompt) and save with save_settings."""
    return _clean(await setup.status(runtime()))


@mcp.tool()
async def save_settings(updates: dict) -> dict:
    """Save settings to the user's settings file (validated; only differences from defaults are kept).

    Keys are dotted config paths, e.g. {"mic_cal_path": "/Users/me/cal.txt", "room_dims_m": [5.8, 4.3, 2.6],
    "seat_xyz.MLP": [3.6, 2.15, 1.1], "timing_mode": "loopback", "crawl_grid.spacing_m": 0.75,
    "dashboard.allow_lan": true}. "@minidsp_model": "2x4HD" applies that model's preset.
    """
    return _clean(await setup.save(runtime(), updates))


@mcp.tool()
async def test_setup(checks: list[str] | None = None) -> dict:
    """Check the setup: 'rew' and 'minidsp' connections, 'mic_cal' and 'house_curve' files (default: all)."""
    return _clean(await setup.check(runtime(), checks or ["rew", "minidsp", "mic_cal", "house_curve"]))


@mcp.tool()
async def configure(updates: dict) -> dict:
    """Same as save_settings, kept for compatibility. Nested dicts are fine, e.g. {"target": {"bass_boost_db": 3}}."""
    return _clean(await setup.save(runtime(), updates))


@mcp.tool()
async def autopilot_step(max_sweeps: int | None = None) -> dict:
    """Advance the calibration as far as possible without the user.

    Returns status: `user_action_required` (relay action.instructions, then confirm_user_action),
    `paused` (sweep budget spent; call again), or `done` (call get_report).
    """
    rep = await runtime().step(max_sweeps=max_sweeps)
    return _clean(rep)


@mcp.tool()
async def confirm_user_action(key: str | None = None, x: float | None = None, y: float | None = None,
                              z: float | None = None, note: str | None = None) -> dict:
    """Tell the autopilot the user finished the pending physical task. Pass x/y/z (metres) if known."""
    xyz = [x, y, z] if None not in (x, y, z) else None
    return _clean(runtime().confirm(key, xyz, note))


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
    async with exclusive(rt) as ok:
        if not ok:
            return BUSY
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
    async with exclusive(rt) as ok:
        if not ok:
            return BUSY
        rec, resp = await rt.rig.measure(roles, "manual", position, title=label)
    return _clean({"id": rec.id, "title": rec.title, "snr_db": rec.extra.get("snr_db"),
                   "summary": summarize(resp, rt.session.config)})


@mcp.tool()
async def analyze_measurement(measurement_id: str) -> dict:
    """Summary of a stored measurement: band levels, peaks, nulls and a 1/3-octave curve."""
    rt = runtime()
    rec = next((m for m in rt.session.measurements if m.id == measurement_id), None)
    if rec is None:
        return {"error": f"unknown measurement {measurement_id}",
                "known": [m.id for m in rt.session.measurements[-10:]]}
    return _clean({"record": rec.model_dump(mode="json"),
                   "summary": summarize(rt.session.response(rec.id), rt.session.config)})


@mcp.tool()
async def list_measurements(limit: int = 30) -> list[dict]:
    """Most recent measurements Rewdio took (id, kind, channels, position, pass)."""
    ms = runtime().session.measurements[-limit:]
    return [{"id": m.id, "title": m.title, "kind": m.kind, "roles": m.roles, "position": m.position,
             "pass": m.pass_no} for m in ms]


@mcp.tool()
async def set_channel(role: str, gain_db: float | None = None, delay_ms: float | None = None,
                      invert: bool | None = None, mute: bool | None = None) -> dict:
    """Directly set one channel's miniDSP gain, delay, polarity or mute (clamped to the DSP's limits)."""
    rt = runtime()
    rt.session.config.channel(role)
    async with exclusive(rt) as ok:
        if not ok:
            return BUSY
        o = await rt.rig.set_channel(role, gain_db=gain_db, delay_ms=delay_ms, invert=invert, mute=mute)
    return {"role": role, **o.model_dump(mode="json")}


@mcp.tool()
async def set_crossover(role: str, filters: list[dict] | None = None, raw: bool = False) -> dict:
    """Write a channel's crossover block. Only for channels with `crossover` in the config.

    filters: [{"kind": "HP"|"LP", "topology": "LR4"|"LR8"|"LR2"|"BW2"|"BW4", "freq_hz": 1200,
    "protect": false}]; omit to restore the configured crossover. raw=True keeps only protection
    filters (for measuring the driver itself). A protection filter can never be removed."""
    rt = runtime()
    rt.session.config.channel(role)
    parsed = [XoverFilter.model_validate(f) for f in filters] if filters is not None else None
    async with exclusive(rt) as ok:
        if not ok:
            return BUSY
        o = await rt.rig.set_crossover(role, parsed, raw=raw)
    return {"role": role, "crossover": [f.model_dump() for f in o.crossover or []]}


# -- speaker measurement (2-way drivers -> VituixCAD) ---------------------------------------------


@mcp.tool()
async def speaker_plan(speaker: str, planes: list[str] | None = None, step_deg: float = 10.0,
                       max_deg: float = 180.0, symmetric: bool = False, distance_m: float | None = None,
                       angles: dict | None = None, preset: str | None = None, variant: str = "default",
                       geometry: dict | None = None, environment: dict | None = None,
                       replace: bool = False) -> dict:
    """Plan a raw driver capture set of one speaker around a manual turntable, for VituixCAD/SoundSible.

    The speaker's drivers are the channels with kind 'driver' and this `speaker` name; each needs a
    `crossover` in the config (protection filters marked protect=true).
    Angles: `preset` ('soundsible': 0..60 by 5 then 70..180 by 10, hor and ver), or `angles`
    {"hor": [5, 10, ...], "ver": [...], "both_signs": true}, or planes/step_deg/max_deg/symmetric.
    variant: label of this physical setup (e.g. 'offset10_riser40'); each variant is its own set.
    geometry (frame: x forward, y to the speaker's left, z up, origin top-front centre of the woofer box,
    metres): pivot_xyz, mic_xyz (or mic_distance_m + mic_height_m + origin_height_m above the ground),
    riser_h, horn_offset_m, horn_stem_lift_m, horn_id.
    environment: temperature_c, indoor, site, soundsible_commit. Then call `speaker_step` repeatedly."""
    from .engine.speaker import angles_for, points_from
    from .engine.speaker_meta import PRESETS, AnglePlan, Environment, Geometry
    rt = runtime()
    if preset is not None:
        if preset not in PRESETS:
            raise ValueError(f"unknown preset {preset!r}; presets: {sorted(PRESETS)}")
        pts = points_from(PRESETS[preset])
    elif angles is not None:
        pts = points_from(AnglePlan.model_validate(angles))
    else:
        pts = angles_for(planes or ["hor"], step_deg, max_deg, symmetric)
    st = rt.speaker.plan(speaker, pts, distance_m, Geometry.from_inputs(**(geometry or {})),
                         Environment.model_validate(environment or {}), variant, replace)
    return {"ok": True, "drivers": st.drivers, "angles": len(st.points), "sweeps": len(st.points) * len(st.drivers),
            "distance_m": round(st.distance_m, 4), "folder": str(rt.speaker.folder(st)),
            "not_recorded": st.geometry.missing() + st.environment.missing()}


@mcp.tool()
async def speaker_step() -> dict:
    """Advance the speaker capture: either asks the user to rotate (status user_action_required: relay
    action.instructions, then call `speaker_confirm`), or sweeps every driver raw at the current angle."""
    rt = runtime()
    async with exclusive(rt) as ok:
        if not ok:
            return BUSY
        return _clean(await rt.speaker.step())


@mcp.tool()
async def speaker_confirm(key: str | None = None) -> dict:
    """The user rotated the speaker as asked. Then call `speaker_step` again."""
    rt = runtime()
    st = rt.speaker.state()
    sim = []
    if st is not None and not st.done and st.rotated_to != st.index:
        sim = rt.apply_sim_effects(rt.speaker._ask(st)["action"], None)
    res = rt.speaker.confirm(key)
    if sim:
        res["simulated_effects"] = sim
    return res


@mcp.tool()
async def speaker_nearfield(driver: str, label: str = "nf") -> dict:
    """Sweep one driver (raw) with the mic close to it: a few mm from the woofer dust cap, or in the port
    mouth (label 'port'). Exported as its own FRD for VituixCAD's Merger, which fills in below the
    far-field gate limit."""
    rt = runtime()
    async with exclusive(rt) as ok:
        if not ok:
            return BUSY
        return await rt.speaker.nearfield(driver, label)


@mcp.tool()
async def speaker_impedance(driver: str, rew_measurement_id: str | None = None, series_cap_uf: float | None = None,
                            cap_in_circuit: bool = False, cap_corner_hz: float = 350.0) -> dict:
    """Mounted impedance of one driver on an impedance jig, exported as zma/<speaker>_<driver>.zma
    (freq ohm phase). Rewdio tries REW's API impedance sweep; if REW refuses, the user takes it in REW's
    GUI and you pass that measurement's id. cap_in_circuit: the horn's series protection capacitor
    (default False: the passive design wants the driver alone). For a driver with a protection filter (the
    horn) the result also carries `protection_cap`: the series cap to fit while running on the DSP, sized
    from the MEASURED impedance at cap_corner_hz (default 350 Hz, SoundSible's BUILD_PLAN), rounded down
    to an E12 value. Relay suggested_uf to the user."""
    rt = runtime()
    async with exclusive(rt) as ok:
        if not ok:
            return BUSY
        return _clean(await rt.speaker.impedance(driver, rew_measurement_id, series_cap_uf, cap_in_circuit,
                                                 cap_corner_hz=cap_corner_hz))


@mcp.tool()
async def speaker_distortion(driver: str, spl_db: list[float] | None = None, ref_hz: float = 1000.0,
                             band_lo_hz: float | None = None, band_hi_hz: float | None = None) -> dict:
    """Harmonic distortion (H2, H3, THD...) of one driver at each target SPL (default 85, 90, 95 dB; nothing
    above 95, Lee's listening ceiling, is allowed), measured at ref_hz at the mic
    (SoundSible's seat is 1.778 m; closer is fine, the distance is recorded). Raw with its protection
    filter kept. Default band: horn 400 Hz..20 kHz, woofer 200 Hz..6 kHz. Targets within 3 dB of
    REW's SPL abort limit are refused. Exported as distortion/<speaker>_<driver>_<spl>dB.csv."""
    rt = runtime()
    band = (band_lo_hz, band_hi_hz) if band_lo_hz is not None and band_hi_hz is not None else None
    async with exclusive(rt) as ok:
        if not ok:
            return BUSY
        from .engine.speaker_extras import DEFAULT_DISTORTION_SPL_DB
        return _clean(await rt.speaker.distortion(driver, list(spl_db or DEFAULT_DISTORTION_SPL_DB), ref_hz, band))


@mcp.tool()
async def export_frd(out_dir: str | None = None, gate_ms: float = 5.0, pre_ms: float = 1.0,
                     ppo: int = 48, root: str | None = None) -> dict:
    """Write the captured set: <root>/measurements/<date>_<site>_<variant>/ (root defaults to REWDIO_HOME;
    out_dir overrides the whole path) with rewdio_session.json (schema rewdio.speaker-session/2) at the
    top, frd/<driver>/ ('<speaker>_<driver> hor 30.frd'), zma/ and distortion/. One common delay is removed
    from every file, so in VituixCAD set every driver's X/Y/Z offset to 0. gate_ms: up to the first
    reflection; each file also reports the band that survives moving the gate end +1/+2 ms."""
    from .analysis.frd import Gate
    rt = runtime()
    return _clean(rt.speaker.export(out_dir, Gate(pre_ms=pre_ms, gate_ms=gate_ms), ppo=ppo, root=root))


@mcp.tool()
async def speaker_report(gate_ms: float = 5.0, band_lo_hz: float = 300.0, band_hi_hz: float = 10000.0) -> dict:
    """Spinorama (on-axis, listening window, early reflections, sound power, PIR, directivity indices)
    of the captured drivers summed through the configured crossover, and the single-speaker targets:
    flat listening window, PIR falling ~0.5 to 1 dB/octave, smooth sound-power directivity."""
    from .analysis.frd import Gate
    rt = runtime()
    return _clean(rt.speaker.report(Gate(gate_ms=gate_ms), (band_lo_hz, band_hi_hz)))


@mcp.tool()
async def solo(roles: list[str]) -> dict:
    """Unmute only these channels; mute the rest."""
    rt = runtime()
    for r in roles:
        rt.session.config.channel(r)
    async with exclusive(rt) as ok:
        if not ok:
            return BUSY
        await rt.rig.solo(roles)
    return {"playing": roles}


@mcp.tool()
async def list_setups() -> list[dict]:
    """Saved DSP setups, newest first: id, name, date, bass flatness, and which one is on the miniDSP now."""
    return [{k: d.get(k) for k in ("id", "name", "created", "note", "source", "bass_std_db", "active")}
            for d in dsp_setups.list_all(runtime().session)]


@mcp.tool()
async def save_setup(name: str, note: str | None = None, measure_first: bool = False) -> dict:
    """Save what's on the miniDSP now as a named, dated setup (for A/B listening later).

    measure_first takes one full-system sweep at the main seat first, so the setup gets a response thumbnail.
    """
    rt = runtime()
    if measure_first:
        async with exclusive(rt) as ok:
            if not ok:
                return BUSY
            await rt.rig.measure([c.role for c in rt.session.config.channels], "listening", rt.session.config.mic_positions[0])
            await rt.rig.unmute_all()
    d = dsp_setups.save(rt.session, name, note)
    return {k: d.get(k) for k in ("ok", "id", "name", "created", "bass_std_db", "message")}


@mcp.tool()
async def recall_setup(setup: str) -> dict:
    """Put a saved setup (id or name) on the miniDSP, all outputs playing. Call twice with two setups to A/B."""
    rt = runtime()
    async with exclusive(rt) as ok:
        if not ok:
            return BUSY
        return await dsp_setups.recall(rt.rig, setup)


@mcp.tool()
async def delete_setup(setup_id: str) -> dict:
    """Delete a saved setup file by id."""
    return dsp_setups.delete(runtime().session, setup_id)


@mcp.tool()
async def get_dsp_state() -> dict:
    """The shadow state of every Rewdio-managed miniDSP output."""
    return runtime().session.dsp.model_dump(mode="json")


@mcp.tool()
async def set_mic_calibration(cal_file_path: str, sensitivity_dbfs_at_94db: float | None = None) -> dict:
    """Save the SoundID Reference (or any) mic calibration file in settings and load it into REW."""
    rt = runtime()
    res = await setup.save(rt, {"mic_cal_path": cal_file_path, "mic_sensitivity_dbfs": sensitivity_dbfs_at_94db})
    if not res.get("ok"):
        return res
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
