"""The physical rig: REW + miniDSP, driven through the shadow DSP state."""

from __future__ import annotations

from ..analysis.quality import check_sweep
from ..analysis.response import Response, log_grid
from ..config import SystemConfig, TimingMode
from ..minidsp.client import MiniDSPClient
from ..minidsp.state import DspState, OutputState
from ..rew.client import RewClient, RewError
from .store import MeasurementRecord, Session


class Rig:
    def __init__(self, session: Session, rew: RewClient, dsp: MiniDSPClient):
        self.s = session
        self.rew = rew
        self.dsp = dsp
        self.level_dbfs = session.config.sweep.level_dbfs

    @property
    def cfg(self) -> SystemConfig:
        return self.s.config

    def grid(self):
        g = self.cfg.tuning.grid
        return log_grid(self.cfg.analysis_ppo, g.f_lo_hz, g.f_hi_hz)

    # -- DSP ------------------------------------------------------------------------

    async def write_outputs(self, roles: list[str] | None = None, include_peq: bool = True) -> None:
        """Push the shadow state of the given channels (default all) to the miniDSP."""
        patches = []
        for c in self.cfg.channels:
            if roles is not None and c.role not in roles:
                continue
            o = self.s.dsp.get(c.role)
            peq = None
            if include_peq:
                fs = self.cfg.dsp_sample_rate
                peq = {slot: (o.peq[slot].biquad(fs) if slot in o.peq else None) for slot in self.cfg.peq_slots}
            patches.append(MiniDSPClient.output_patch(
                c.dsp_output, gain_db=o.gain_db, mute=o.mute, delay_ms=o.delay_ms, invert=o.invert, peq=peq))
        await self.dsp.set_outputs(patches)
        self.s.save()

    async def assert_baseline(self) -> None:
        b = self.cfg.baseline
        self.s.dsp = DspState(outputs={c.role: OutputState(gain_db=b.gain_db, delay_ms=b.delay_ms, invert=b.invert)
                                       for c in self.cfg.channels})
        await self.write_outputs()

    async def apply(self, state: DspState) -> None:
        for c in self.cfg.channels:
            new = state.get(c.role).model_copy(deep=True)
            new.mute = self.s.dsp.get(c.role).mute
            self.s.dsp.outputs[c.role] = new
        await self.write_outputs()

    async def solo(self, roles: list[str]) -> None:
        changed = []
        for c in self.cfg.channels:
            o = self.s.dsp.get(c.role)
            want_mute = c.role not in roles
            if o.mute != want_mute:
                o.mute = want_mute
                changed.append(c.role)
        if changed:
            await self.dsp.set_outputs([MiniDSPClient.output_patch(self.cfg.channel(r).dsp_output, mute=self.s.dsp.get(r).mute)
                                        for r in changed])
            self.s.save()

    async def nudge_delay(self, role: str, extra_ms: float) -> None:
        o = self.s.dsp.get(role)
        o.delay_ms = round(o.delay_ms + extra_ms, 3)
        await self.dsp.set_outputs([MiniDSPClient.output_patch(self.cfg.channel(role).dsp_output, delay_ms=o.delay_ms)])
        self.s.save()

    # -- REW --------------------------------------------------------------------------

    async def prepare_rew(self) -> dict:
        cfg = self.cfg
        report: dict = {"rew_version": await self.rew.version()}
        await self.rew.ensure_blocking()
        if cfg.mic_cal_path:
            await self.rew.set_input_cal(cfg.mic_cal_path, cfg.mic_sensitivity_dbfs)
        cal = await self.rew.input_cal()
        report["mic_cal"] = (cal.get("calDataAllInputs") or {}).get("calFilePath") or None
        sw = cfg.sweep
        await self.rew.configure_sweep(sw.start_hz, sw.end_hz, sw.length, sw.start_delay_s, sw.rew_sample_rate,
                                       sw.fill_silence_with_dither)
        await self.rew.set_level(self.level_dbfs)
        await self.rew.set_protection(cfg.sweep.max_spl_db)
        choices = await self.rew.timing_reference_choices()
        want = {TimingMode.LOOPBACK: "loop", TimingMode.ACOUSTIC: "acoustic", TimingMode.SUM_FIT: "none"}[cfg.timing_mode]
        pick = next((c for c in choices if want in c.lower()), None)
        if pick is None:
            raise RewError(f"REW offers no timing reference matching {want!r}; choices: {choices}")
        await self.rew.set_timing_reference(pick)
        report["timing_reference"] = pick
        return report

    async def set_level(self, dbfs: float) -> None:
        s = self.cfg.sweep
        self.level_dbfs = round(max(s.min_level_dbfs, min(s.max_level_dbfs, dbfs)), 1)
        await self.rew.set_level(self.level_dbfs)

    async def measure(self, roles: list[str], kind: str, position: str, title: str | None = None,
                      **extra) -> tuple[MeasurementRecord, Response]:
        await self.solo(roles)
        out_ch = {self.cfg.channel(r).rew_output_channel for r in roles} - {None}
        if len(out_ch) == 1:
            await self.rew.set_output_channel(out_ch.pop())
        title = title or f"{'+'.join(roles)} @{position} ({kind}, pass {self.s.workflow.pass_no})"
        summary = await self.rew.sweep(title, notes=f"rewdio {kind}")
        spec = await self.rew.frequency_response(summary["uuid"], ppo=self.cfg.analysis_ppo)
        resp = Response.from_db_phase(spec.freqs, spec.mag_db, spec.phase_deg, grid=self.grid())
        snap = DspState(outputs={r: self.s.dsp.get(r).model_copy(deep=True) for r in roles})
        findings = check_sweep(self.cfg, resp, summary, list(roles), self.s.workflow.staging_reference_db)
        rec = MeasurementRecord(id=summary["uuid"], title=title, kind=kind, roles=list(roles), position=position,
                                pass_no=self.s.workflow.pass_no, snapshot=snap, level_dbfs=self.level_dbfs,
                                timing=self.cfg.timing_mode.value,
                                extra={**extra, "snr_db": summary.get("signalToNoisedB"),
                                       "quality": [f.as_dict() for f in findings]})
        # provenance in REW itself, so a saved .mdat explains how it was taken
        await self.rew.annotate(rec.id, title, {"rewdio": {"kind": kind, "roles": list(roles), "position": position,
                                                           "pass": rec.pass_no, "level_dbfs": self.level_dbfs,
                                                           "dsp": snap.model_dump(mode="json")["outputs"]}})
        self.s.add_measurement(rec, resp)
        return rec, resp
