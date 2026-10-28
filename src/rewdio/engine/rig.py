"""The physical rig: REW + miniDSP, driven through the shadow DSP state."""

from __future__ import annotations

from ..analysis.gating import direct_segment
from ..analysis.quality import check_sweep
from ..analysis.response import Response, log_grid
from ..analysis.waterfall import keep_ir
from ..config import SystemConfig, TimingMode
from ..minidsp.client import MiniDSPClient
from ..minidsp.state import DspState, OutputState, XoverFilter, xover_sections
from ..rew.client import RewClient, RewError
from .store import MeasurementRecord, Session


class Rig:
    def __init__(self, session: Session, rew: RewClient, dsp: MiniDSPClient):
        self.s = session
        self.rew = rew
        self.dsp = dsp
        # resume at the level gain staging chose; REW is re-told before every sweep (it may have restarted)
        self.level_dbfs = session.workflow.crawl.get("level_dbfs", session.config.sweep.level_dbfs)
        self.dirty = False  # a DSP write failed part-way; the next write re-sends everything
        self._rew_ready = False

    @property
    def cfg(self) -> SystemConfig:
        return self.s.config

    def grid(self):
        g = self.cfg.tuning.grid
        return log_grid(self.cfg.analysis_ppo, g.f_lo_hz, g.f_hi_hz)

    # -- DSP ------------------------------------------------------------------------
    # The hardware is written FIRST and the shadow state updated only after it succeeds. If a write fails
    # part-way (minidsp-rs applies a config sequentially, with no rollback), the rig is marked dirty and the
    # next write re-sends every channel's full state, so the shadow and the hardware can't drift apart.

    def _patch(self, role: str, o: OutputState, mute_only: bool = False, include_peq: bool = True) -> dict:
        out = self.cfg.channel(role).dsp_output
        if mute_only:
            return MiniDSPClient.output_patch(out, mute=o.mute)
        peq = xo = None
        if include_peq:  # the filter blocks: PEQ, and the crossover where Rewdio manages it
            fs = self.cfg.dsp_sample_rate
            peq = {slot: (o.peq[slot].biquad(fs) if slot in o.peq else None) for slot in self.cfg.peq_slots_for(role)}
            if self.cfg.channel(role).crossover is not None:
                xo = xover_sections(o.crossover or [], fs)
        return MiniDSPClient.output_patch(out, gain_db=o.gain_db, mute=o.mute, delay_ms=o.delay_ms, invert=o.invert,
                                          peq=peq, crossover=xo)

    def _check_protection(self, new: DspState) -> bool:
        """A protection filter (e.g. a compression driver's high-pass) may never leave the hardware. A managed
        channel whose crossover is unknown (older session state) gets its configured crossover; returns True
        then, so the caller writes it rather than only recording it."""
        filled = False
        for c in self.cfg.channels:
            if c.crossover is not None and new.get(c.role).crossover is None:
                new.get(c.role).crossover = list(c.crossover)
                filled = True
            need = [f for f in c.crossover or [] if f.protect]
            have = new.get(c.role).crossover or []
            missing = [f for f in need if f not in have]
            if missing:
                raise ValueError(f"refusing to write {c.role} without its protection filter(s) "
                                 f"{[f.model_dump() for f in missing]}")
        return filled

    async def _commit(self, new: DspState, roles: list[str] | None = None, mute_only: bool = False,
                      include_peq: bool = True) -> None:
        if self._check_protection(new) or self.dirty:  # unknown crossover filled, or a failed write: send everything
            roles, mute_only, include_peq = None, False, True
        targets = [c.role for c in self.cfg.channels if roles is None or c.role in roles]
        patches = [self._patch(r, new.get(r), mute_only, include_peq) for r in targets]
        try:
            await self.dsp.set_outputs(patches)
        except Exception:
            self.dirty = True
            raise
        self.dirty = False
        self.s.dsp = DspState.model_validate(new.model_dump())
        self.s.save()

    def _copy(self) -> DspState:
        return DspState.model_validate(self.s.dsp.model_dump())

    async def write_outputs(self, roles: list[str] | None = None, include_peq: bool = True) -> None:
        """Push the shadow state of the given channels (default all) to the miniDSP."""
        await self._commit(self._copy(), roles, include_peq=include_peq)

    async def assert_baseline(self) -> None:
        b = self.cfg.baseline
        await self._commit(DspState(outputs={c.role: OutputState(gain_db=b.gain_db, delay_ms=b.delay_ms, invert=b.invert,
                                                                 crossover=c.crossover)
                                             for c in self.cfg.channels}))

    async def apply(self, state: DspState, listen: bool = False) -> None:
        """Write `state` to every channel (clamped). listen=True unmutes everything in the same write."""
        new = self._copy()
        lo, hi = self.cfg.dsp_gain_range_db
        for c in self.cfg.channels:
            o = state.get(c.role).model_copy(deep=True)
            o.mute = False if listen else self.s.dsp.get(c.role).mute
            o.gain_db = min(max(o.gain_db, lo), hi)
            o.delay_ms = min(max(o.delay_ms, 0.0), self.cfg.dsp_max_delay_ms)
            new.outputs[c.role] = o
        await self._commit(new)

    async def set_channel(self, role: str, **fields) -> OutputState:
        new = self._copy()
        o = new.get(role)
        for k, v in fields.items():
            if v is not None:
                setattr(o, k, v)
        lo, hi = self.cfg.dsp_gain_range_db
        o.gain_db = min(max(o.gain_db, lo), hi)
        o.delay_ms = min(max(o.delay_ms, 0.0), self.cfg.dsp_max_delay_ms)
        await self._commit(new, [role], include_peq=False)
        return self.s.dsp.get(role)

    async def set_crossover(self, role: str, filters: list[XoverFilter] | None = None, raw: bool = False) -> OutputState:
        """Write a channel's crossover: `filters`, else its configured crossover. raw=True keeps only the
        protection filters, for capturing the driver itself. Protection filters can never be dropped."""
        ch = self.cfg.channel(role)
        if ch.crossover is None:
            raise ValueError(f"{role} has no `crossover` in the config, so Rewdio does not manage its crossover")
        want = list(filters if filters is not None else ch.crossover)
        if raw:
            want = [f for f in want if f.protect]
        new = self._copy()
        new.get(role).crossover = want
        await self._commit(new, [role])
        return self.s.dsp.get(role)

    async def solo(self, roles: list[str]) -> None:
        """Unmute exactly `roles`. Always sends the whole mute vector: never trust that a mute already stuck."""
        new = self._copy()
        for c in self.cfg.channels:
            new.get(c.role).mute = c.role not in roles
        await self._commit(new, mute_only=True)

    async def unmute_all(self) -> None:
        await self.solo([c.role for c in self.cfg.channels])

    async def nudge_delay(self, role: str, extra_ms: float) -> None:
        new = self._copy()
        o = new.get(role)
        o.delay_ms = round(min(max(o.delay_ms + extra_ms, 0.0), self.cfg.dsp_max_delay_ms), 3)
        await self._commit(new, [role], include_peq=False)

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
        self._rew_ready = True
        return report

    async def set_level(self, dbfs: float) -> None:
        s = self.cfg.sweep
        self.level_dbfs = round(max(s.min_level_dbfs, min(s.max_level_dbfs, dbfs)), 1)
        await self.rew.set_level(self.level_dbfs)

    async def measure(self, roles: list[str], kind: str, position: str, title: str | None = None,
                      **extra) -> tuple[MeasurementRecord, Response]:
        if not self._rew_ready:  # e.g. resumed after a restart: REW may have forgotten everything
            await self.prepare_rew()
        await self.rew.set_level(self.level_dbfs)  # the level each record is labelled with is the level played
        await self.solo(roles)
        out_ch = {self.cfg.channel(r).rew_output_channel for r in roles}
        if out_ch != {None}:  # per-speaker REW outputs are configured (validated: all channels or none)
            await self.rew.set_output_channel(out_ch.pop() if len(out_ch) == 1 else self.cfg.rew_output_all)
        title = title or f"{'+'.join(roles)} @{position} ({kind}, pass {self.s.workflow.pass_no})"
        summary = await self.rew.sweep(title, notes=f"rewdio {kind}")
        spec = await self.rew.frequency_response(summary["uuid"], ppo=self.cfg.analysis_ppo, smoothing="None")
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
        bs = self.cfg.balance
        if bs.enabled and bs.use_gating and len(roles) == 1 and roles[0] in bs.pair and kind in ("solo", "manual"):
            ir, fs = await self.rew.impulse_response(rec.id)
            self.s.add_direct(rec.id, direct_segment(ir, fs, bs, self.cfg.tuning.alignment.arrival_onset_fraction))
        if kind in ("system", "listening"):  # full-system sweeps keep a short impulse for the waterfall
            try:
                ir, fs = await self.rew.impulse_response(rec.id)
                self.s.add_ir(rec.id, keep_ir(ir, fs, self.cfg.dashboard.waterfall))
            except RewError:
                pass  # the waterfall is a nicety; never fail a sweep over it
        self.s.add_measurement(rec, resp)
        return rec, resp
