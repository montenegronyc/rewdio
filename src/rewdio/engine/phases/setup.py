"""PREFLIGHT and GAIN_STAGING."""

from __future__ import annotations

import numpy as np

from ...analysis.response import band_median_db, smooth_db
from ...rew.client import RewError
from ...config import TimingMode
from ...rew.extras import input_levels, loopback_check
from .base import PhaseBase


class SetupPhase(PhaseBase):
    async def _phase_preflight(self, log) -> str:
        r = self.rig
        rep = await r.prepare_rew()
        log.append(f"REW {rep['rew_version']}; timing reference {rep['timing_reference']}; protection read back OK")
        if not rep["mic_cal"]:
            log.append("WARNING: no mic calibration loaded in REW. Load the SoundID Reference cal file "
                       "(set mic_cal_path) for accurate SPL and HF response.")
        devs = await r.dsp.devices()
        if not devs:
            raise RuntimeError("minidsp-rs sees no device: is minidspd running and the miniDSP plugin closed?")
        log.append(f"miniDSP: {devs[0].get('product_name')}")
        await r.assert_baseline()
        log.append("DSP baseline asserted on " + ", ".join(c.role for c in self.cfg.channels))
        idle = await r.unlisted_outputs()
        if idle:
            outs = ", ".join(f"unit {d} Out{i + 1}" for d, ii in idle.items() for i in ii)
            log.append(f"muted miniDSP outputs that no channel uses ({outs}); add a channel to use one, or set "
                       "mute_unlisted_outputs to false to leave them alone")
        lv = await self._check_input_alive()
        if lv.get("clipping"):
            log.append("WARNING: mic input already near 0 dBFS with no signal; lower the preamp gain")
        if self.cfg.timing_mode == TimingMode.LOOPBACK and self.cfg.tuning.preflight.loopback_check:
            await self._check_loopback(log)
        return "GAIN_STAGING"

    async def _check_input_alive(self) -> dict:
        """Refuse to sweep into a digitally silent input. Re-checked after every "fixed": a confirmation alone
        never lets the run through. This only catches digital silence; a mic that passes preamp hiss but hears
        no speaker (e.g. no phantom power) is caught by gain staging's level-rise check."""
        pf = self.cfg.tuning.preflight
        for attempt in range(pf.max_mic_rechecks):
            try:
                lv = await input_levels(self.rig.rew, pf.input_level_listen_s)
            except RewError as e:
                return {"mic_alive": None, "error": str(e)}
            if lv.get("mic_alive") is not False or not pf.require_live_mic:
                return lv
            again = " It is still silent after your last check." if attempt else ""
            self._ask(f"mic_dead:{self.wf.pass_no}:{attempt}",
                      f"REW hears nothing from the mic input (digital silence).{again} Check the XLR cable, 48 V "
                      "phantom power on the interface, the preamp gain, and that REW's input device/channel is the "
                      "mic. Confirm when fixed.", levels=lv)
        raise RuntimeError(f"the mic input is still digitally silent after {pf.max_mic_rechecks} checks; fix the input "
                           "(REW's input device, the interface, phantom power) and run the autopilot again")

    async def _check_loopback(self, log) -> None:
        pf = self.cfg.tuning.preflight
        await self.rig.solo([])  # every output muted: the room stays silent during the test
        try:
            res = await loopback_check(self.rig.rew, pf.loopback_test_level_dbfs, max(pf.input_level_listen_s, 0.0),
                                       pf.loopback_min_rms_dbfs)
        except RewError as e:
            res = {"ok": None, "reason": str(e)}
        if res["ok"] is None:
            log.append(f"loopback check inconclusive ({res.get('reason')}); the first sweep will show if timing is wrong")
        elif res["ok"]:
            log.append(f"loopback signal present ({res['loudest_input_dbfs']} dBFS on the reference input)")
        else:
            self._ask(f"loopback_missing:{self.wf.pass_no}",
                      "The loopback signal is not reaching the interface. Check: the RCA Y-splitter is on the DAC's "
                      "LEFT output; its second leg goes into interface input 2 (line, Inst off, gain at minimum); in "
                      "REW's soundcard preferences the reference/loopback input is input 2 and the timing reference "
                      "is loopback. Confirm when fixed.", levels=res.get("levels"))

    async def _phase_gain_staging(self, log) -> str:
        r = self.rig
        sw, gs, b = self.cfg.sweep, self.cfg.tuning.gain_staging, self.cfg.tuning.bands
        self._mic_to(self.cfg.mic_positions[0], "start")
        pos = self.wf.mic_at or "unknown"
        await r.set_level(sw.level_dbfs)  # always from the quiet start, never from a level left by an earlier run
        mains = [c.role for c in self.cfg.mains] or [c.role for c in self.cfg.room_channels]
        # With no mic sensitivity, REW's "SPL" is mic dBFS + 120: it follows the preamp knob, not the speakers, so
        # an SPL target would drive the speakers harder whenever the knob is turned down. Stage by SNR instead.
        by_snr = self.cfg.mic_sensitivity_dbfs is None
        mid = snr = None
        last: tuple[float, float] | None = None  # (level played, mains-band level heard) of the previous sweep
        for _ in range(gs.max_steps):
            try:
                rec, resp = await r.measure(mains, "staging", pos)
            except RewError as e:
                if r.level_dbfs <= sw.min_level_dbfs:
                    raise
                log.append(f"sweep at {r.level_dbfs} dBFS aborted ({e}); backing off {gs.abort_backoff_db:.0f} dB")
                await r.set_level(r.level_dbfs - gs.abort_backoff_db)
                last = None  # an aborted sweep heard nothing: don't judge the next step against it
                continue
            mid = band_median_db(resp, *b.mains_level_hz, b.level_smoothing)
            inband = (resp.freqs > sw.start_hz * 2) & (resp.freqs < sw.end_hz)
            peak = float(np.max(smooth_db(resp.freqs, resp.mag_db, b.bass_smoothing)[inband]))
            snr = rec.extra.get("snr_db")
            snr_txt = "" if snr is None else f", SNR {snr:.1f} dB"
            log.append(f"gain staging @ {r.level_dbfs} dBFS: mains band {mid:.1f} dB SPL, peak {peak:.1f}{snr_txt}")
            if last is not None:
                await self._require_rise(last, (r.level_dbfs, mid), log)
            last = (r.level_dbfs, mid)
            headroom = sw.max_spl_db - gs.peak_margin_db - peak  # REW's abort limit, i.e. mic clipping uncalibrated
            if by_snr and snr is not None:
                # only ever louder until the SNR is enough; quieter only to keep the peak off the abort limit
                step = min(headroom, 0.0) if snr >= gs.target_snr_db else min(gs.target_snr_db - snr, headroom)
                if abs(step) < gs.tolerance_db:
                    break
            else:
                err = sw.target_spl_db - mid
                if abs(err) < gs.tolerance_db:
                    break
                step = min(err, headroom)
            if (step > 0 and r.level_dbfs >= sw.max_level_dbfs) or (step < 0 and r.level_dbfs <= sw.min_level_dbfs):
                break
            await r.set_level(r.level_dbfs + float(np.clip(step, -gs.max_step_down_db, gs.max_step_up_db)))
        q = self.cfg.tuning.quality
        if snr is not None and snr < q.min_snr_db_critical:
            level = r.level_dbfs
            await r.set_level(sw.level_dbfs)
            raise RuntimeError(f"gain staging reached {level} dBFS (the most it may use is {sw.max_level_dbfs}) but "
                               f"the SNR is only {snr:.0f} dB; sweeps need {q.min_snr_db_critical:.0f}. Turn the amps "
                               "or the miniDSP master up, quiet the room, or raise sweep.max_level_dbfs if the "
                               "speakers can take it, then run the autopilot again")
        self.wf.crawl["level_dbfs"] = r.level_dbfs
        # per-channel level in its own band will be compared to this to catch dead channels
        self.wf.staging_reference_db = None if mid is None else mid - 20 * float(np.log10(len(mains)))  # one channel
        return "SUB_CRAWL" if self.cfg.do_sub_crawl and not self.wf.crawl.get("result") else "BASELINE"

    async def _require_rise(self, before: tuple[float, float], after: tuple[float, float], log) -> None:
        """A step up of N dB must be heard as about N dB more. If it isn't, the mic is not hearing the speakers
        (no phantom power, wrong input, a dead amp), and every further step would only make them louder: stop,
        back at the quiet starting level."""
        played = after[0] - before[0]
        heard = after[1] - before[1]
        tol = self.cfg.tuning.gain_staging.rise_tolerance_db
        if played <= tol or heard >= played - tol:
            return
        start = self.cfg.sweep.level_dbfs
        await self.rig.set_level(start)
        log.append(f"no signal: the level went up {played:.0f} dB but the mic heard {heard:+.1f} dB")
        raise RuntimeError(f"no signal at the mic: the sweep level went up {played:.0f} dB but the mic measured "
                           f"{heard:+.1f} dB. Check 48 V phantom power, the mic cable and input, REW's input device "
                           f"and channel, and that the amps are on. Staging stopped at a safe level ({start} dBFS); "
                           "run the autopilot again when it is fixed")
