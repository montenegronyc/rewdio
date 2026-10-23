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
        pf = self.cfg.tuning.preflight
        try:
            lv = await input_levels(r.rew, pf.input_level_listen_s)
        except RewError as e:
            lv = {"mic_alive": None, "error": str(e)}
        if lv.get("mic_alive") is False and pf.require_live_mic:
            self._ask(f"mic_dead:{self.wf.pass_no}", "REW hears nothing from the mic input (digital silence). Check "
                      "the XLR cable, 48 V phantom power on the interface, the preamp gain, and that REW's input "
                      "device/channel is the mic. Confirm when fixed.", levels=lv)
        if lv.get("clipping"):
            log.append("WARNING: mic input already near 0 dBFS with no signal; lower the preamp gain")
        if self.cfg.timing_mode == TimingMode.LOOPBACK and pf.loopback_check:
            await self._check_loopback(log)
        return "GAIN_STAGING"

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
        mains = [c.role for c in self.cfg.mains] or [c.role for c in self.cfg.channels]
        mid = None
        for _ in range(gs.max_steps):
            try:
                _, resp = await r.measure(mains, "staging", pos)
            except RewError as e:
                if r.level_dbfs <= sw.min_level_dbfs:
                    raise
                log.append(f"sweep at {r.level_dbfs} dBFS aborted ({e}); backing off {gs.abort_backoff_db:.0f} dB")
                await r.set_level(r.level_dbfs - gs.abort_backoff_db)
                continue
            mid = band_median_db(resp, *b.mains_level_hz, b.level_smoothing)
            inband = (resp.freqs > sw.start_hz * 2) & (resp.freqs < sw.end_hz)
            peak = float(np.max(smooth_db(resp.freqs, resp.mag_db, b.bass_smoothing)[inband]))
            err = sw.target_spl_db - mid
            log.append(f"gain staging @ {r.level_dbfs} dBFS: mains band {mid:.1f} dB SPL, peak {peak:.1f}")
            if abs(err) < gs.tolerance_db or (err > 0 and r.level_dbfs >= sw.max_level_dbfs) \
                    or (err < 0 and r.level_dbfs <= sw.min_level_dbfs):
                break
            step = float(np.clip(err, -gs.max_step_down_db, gs.max_step_up_db))
            step = min(step, sw.max_spl_db - gs.peak_margin_db - peak)
            await r.set_level(r.level_dbfs + step)
        self.wf.crawl["level_dbfs"] = r.level_dbfs
        # per-channel level in its own band will be compared to this to catch dead channels
        self.wf.staging_reference_db = None if mid is None else mid - 20 * float(np.log10(len(mains)))  # one channel
        return "SUB_CRAWL" if self.cfg.do_sub_crawl and not self.wf.crawl.get("result") else "BASELINE"
