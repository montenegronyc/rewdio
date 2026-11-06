"""MCP tools for speaker measurement: crossover writes, the hardware self-test, raw/through-crossover polar
captures on a manual turntable, impedance and distortion, and the VituixCAD/SoundSible export."""

from __future__ import annotations

from .minidsp.state import XoverFilter


def register(mcp, runtime, exclusive, BUSY, _clean) -> None:  # noqa: N803 - the server's own names
    @mcp.tool()
    async def set_crossover(role: str, filters: list[dict] | None = None, raw: bool = False) -> dict:
        """Write a channel's crossover block. Only for channels with `crossover` in the config. Note: speaker_step,
        speaker_nearfield and the other speaker captures write the CONFIGURED crossover back when they finish, and
        speaker_report always uses the configured one; to audition a different crossover, put it in the config.

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


    @mcp.tool()
    async def rig_selftest(driver: str, distance_m: float | None = None, impedance: bool = False) -> dict:
        """Run FIRST on new hardware, before any capture: proves on the real rig what the simulator assumes.
        Quietly sweeps `driver` (through its protection filter) to check the miniDSP is seen with enough outputs,
        REW's loopback timing and impulse startTime, that a crossover write and a delay write actually land
        (measured against the prediction), and optionally (impedance=true, jig wired) REW's API impedance sweep.
        Leaves the output as it found it. Report each FAIL to the user with its detail; stored as selftest.json."""
        from .engine import selftest
        rt = runtime()
        rt.session.config.channel(driver)
        async with exclusive(rt) as ok:
            if not ok:
                return BUSY
            return _clean(await selftest.run(rt.rig, driver, distance_m, impedance))


    # -- speaker measurement (2-way drivers -> VituixCAD) ---------------------------------------------


    @mcp.tool()
    async def speaker_plan(speaker: str, planes: list[str] | None = None, step_deg: float = 10.0,
                           max_deg: float = 180.0, symmetric: bool = False, distance_m: float | None = None,
                           angles: dict | None = None, preset: str | None = None, variant: str = "default",
                           geometry: dict | None = None, environment: dict | None = None,
                           replace: bool = False, modes: list[str] | None = None) -> dict:
        """Plan a raw driver capture set of one speaker around a manual turntable, for VituixCAD/SoundSible.

        The speaker's drivers are the channels with kind 'driver' and this `speaker` name; each needs a
        `crossover` in the config (protection filters marked protect=true).
        Angles: `preset` ('soundsible': 0..60 by 5 then 70..180 by 10, hor and ver), or `angles`
        {"hor": [5, 10, ...], "ver": [...], "both_signs": true}, or planes/step_deg/max_deg/symmetric.
        variant: label of this physical setup (e.g. 'offset10_riser40'); each variant is its own set.
        geometry (frame: x forward, y to the speaker's left, z up, origin top-front centre of the woofer box,
        metres): pivot_xyz, mic_xyz (or mic_distance_m + mic_height_m + origin_height_m above the ground),
        riser_h, horn_offset_m, horn_stem_lift_m, horn_id.
        environment: temperature_c, indoor, site, soundsible_commit.
        modes (default ['raw']): 'raw' = each driver alone (crossover down to its protection filter, the output's
        gain/delay/polarity/PEQ divided out on export); 'xo' = each driver through its output exactly as configured
        (e.g. auditioning a passive target); 'sum' = all drivers together. Then call `speaker_step` repeatedly."""
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
        if rt.busy:
            return BUSY
        st = rt.speaker.plan(speaker, pts, distance_m, Geometry.from_inputs(**(geometry or {})),
                             Environment.model_validate(environment or {}), variant, replace, modes)
        return {"ok": True, "drivers": st.drivers, "angles": len(st.points), "sweeps": len(st.points) * len(st.drivers),
                "distance_m": round(st.distance_m, 4), "folder": str(rt.speaker.folder(st)),
                "not_recorded": st.geometry.missing() + st.environment.missing()}


    @mcp.tool()
    async def speaker_step(level_dbfs: float | None = None, skip: bool = False) -> dict:
        """Advance the speaker capture. Statuses:
        - user_action_required: relay action.instructions, wait for the user, then call `speaker_confirm` (from the
          paired phone Lee taps Ready instead, which confirms and sweeps by itself; the phone and the Mac voice say
          action.speech).
        - measured: every driver swept at this angle; `next` holds the following prompt.
        - retry: a critical quality finding (clipped, too quiet...). Fix the cause and step again; pass level_dbfs
          to change the sweep level (e.g. 6 dB lower after 'clipped'), or skip=true to give up on this angle.
        - done: call export_frd.
        Waits speaker_prompts.settle_s after the confirmation before sweeping."""
        rt = runtime()
        if rt.busy:
            return BUSY
        return _clean(await rt.speaker_step(level_dbfs, skip))


    @mcp.tool()
    async def speaker_confirm(key: str | None = None) -> dict:
        """The user turned the speaker as asked. Then call `speaker_step` again. (From the paired phone, Lee taps
        Ready instead, which confirms and sweeps on its own; `speaker_step` then just reports.)"""
        rt = runtime()
        if rt.busy:
            return BUSY
        return rt.speaker_confirm(key)


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
                                cap_in_circuit: bool = False, cap_corner_hz: float = 350.0, label: str = "") -> dict:
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
                                                     cap_corner_hz=cap_corner_hz, label=label))


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
    async def export_frd(out_dir: str | None = None, gate_ms: float = 5.0, pre_ms: float = 2.0,
                         ppo: int = 48, root: str | None = None) -> dict:
        """Write the captured set: <root>/measurements/<date>_<site>_<variant>/ (root defaults to REWDIO_HOME;
        out_dir overrides the whole path) with rewdio_session.json (schema rewdio.speaker-session/3) at the
        top, frd/<driver>/ ('<speaker>_<driver> hor 30.frd'), zma/ and distortion/. One common delay is removed
        from every file, so in VituixCAD set every driver's X/Y/Z offset to 0. gate_ms: up to the first
        reflection; each file also reports the band that survives moving the gate end +1/+2 ms."""
        from .analysis.frd import Gate
        rt = runtime()
        if rt.busy:
            return BUSY
        return _clean(rt.speaker.export(out_dir, Gate(pre_ms=pre_ms, gate_ms=gate_ms), ppo=ppo, root=root))


    @mcp.tool()
    async def speaker_report(gate_ms: float = 5.0, band_lo_hz: float = 300.0, band_hi_hz: float = 10000.0) -> dict:
        """Spinorama (on-axis, listening window, early reflections, sound power, PIR, directivity indices)
        of the captured drivers summed through the configured crossover, and the single-speaker targets:
        flat listening window, PIR falling ~0.5 to 1 dB/octave, smooth sound-power directivity."""
        from .analysis.frd import Gate
        rt = runtime()
        if rt.busy:
            return BUSY
        return _clean(rt.speaker.report(Gate(gate_ms=gate_ms), (band_lo_hz, band_hi_hz)))

    @mcp.tool()
    async def speaker_update(geometry: dict | None = None, environment: dict | None = None, serial: str | None = None,
                             reference: bool | None = None, parts: dict | None = None) -> dict:
        """Change the open capture set's metadata without touching its captures: geometry and environment fields
        (merged in), the tower's serial, whether this set is THE reference every later tower is held to, and
        measured part values from the LCR meter (e.g. {"L1_mH": 1.52, "C1_uF": 22.1}). Export again afterwards."""
        rt = runtime()
        if rt.busy:  # a running sweep holds its own copy of the set and would overwrite this when it saves
            return BUSY
        st = rt.speaker.update(geometry, environment, serial, reference, parts)
        return {"ok": True, "serial": st.serial, "reference": st.reference, "parts": st.parts,
                "not_recorded": st.geometry.missing() + st.environment.missing()}

    @mcp.tool()
    async def speaker_transfer(stage: str, label: str, driver: str, note: str | None = None) -> dict:
        """Passive network transfer to one driver, as two sweeps through the SAME input and divider (or differential
        probe): stage 'amp' with the input across the amplifier output, then stage 'tap' across that driver's
        terminals. Their ratio is the network; the divider and mic calibration cancel. `driver` is the channel that
        plays (the tower's amp channel); `label` names the branch (e.g. 'woofer', 'horn'). Keep the level low: the
        input reads amplifier voltage. For a terminal that is not at ground (the woofer inside the lattice, or any
        output of a bridged/BTL amplifier) use a differential probe or isolating transformer: never connect the
        interface ground to an amplifier output, it can short the amplifier."""
        rt = runtime()
        async with exclusive(rt) as ok:
            if not ok:
                return BUSY
            return _clean(await rt.speaker.transfer(stage, label, driver, note))

    @mcp.tool()
    async def speaker_soak(driver: str, minutes: float = 60.0, spl_db: float = 90.0) -> dict:
        """Burn-in check: pink noise through `driver` for `minutes` at about `spl_db` at the seat (never above 95;
        the level comes from this channel's distortion capture, 3 dB under it to be safe). Runs in the background;
        afterwards call speaker_impedance(driver, label='post_soak') and the QA run compares the two."""
        return runtime().start_soak(driver, minutes, spl_db)

    @mcp.tool()
    async def qa_run(candidate_dir: str, reference_dir: str | None = None, pair_dir: str | None = None) -> dict:
        """Production QA: compare an exported tower set (candidate_dir) with the reference tower's set (default: the
        newest set marked reference under REWDIO_HOME/measurements) and, if given, its partner tower (pair_dir).
        Checks: impedance, network transfer, polarity, on-axis FRD, pair match, distortion, a rub-and-buzz flag,
        soak. Tolerances: qa_tolerances.json (packaged, overlaid by REWDIO_HOME/qa_tolerances.json). Writes
        qa_report.json and a `qa` block into the candidate's manifest. Report every failed check with its worst
        frequency and margin; rub and buzz is a flag, Lee still listens."""
        from pathlib import Path

        from .engine import qa
        rt = runtime()
        ref = reference_dir or qa.find_reference(rt.session.dir.parent)
        if ref is None:
            return {"ok": False, "message": "no reference set found: mark one with speaker_update(reference=true) "
                                            "and export it, or pass reference_dir"}
        rep = qa.run(Path(candidate_dir), Path(ref), Path(pair_dir) if pair_dir else None, rt.session.dir.parent)
        rep.pop("tolerances", None)
        return _clean(rep)
