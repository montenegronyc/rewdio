"""BASELINE: every channel solo at every mic position, plus sum-fit sweeps when there is no
loopback, gated by measurement quality. Also builds the shared time base for the solver."""

from __future__ import annotations

import math

from ...analysis.alignment import fit_relative_delay
from ...analysis.quality import repeatability_db
from ...analysis.solver import Measured
from ...config import ChannelKind, TimingMode
from .base import PhaseBase


class BaselinePhase(PhaseBase):
    def _sumfit_band(self, role: str) -> tuple[float, float]:
        fc, k = self.cfg.crossover_hz, self.cfg.tuning.bands.crossover_span_factor
        return (fc / k, fc * k) if self.cfg.channel(role).kind == ChannelKind.SUB else self.cfg.tuning.bands.sumfit_mains_hz

    def _sumfit_probe_ms(self, role: str) -> float:
        lo, hi = self._sumfit_band(role)
        frac = self.cfg.tuning.autopilot.sumfit_probe_fraction_of_period
        return round(min(1000.0 * frac / math.sqrt(lo * hi), self.cfg.dsp_max_delay_ms / 2), 2)

    async def _gated_measure(self, roles, kind, pos, log, **extra):
        """Measure; on a critical quality finding, stop and tell the user what to fix."""
        rec, resp = await self.rig.measure(roles, kind, pos, **extra)
        q = rec.extra.get("quality") or []
        for f in q:
            self.wf.quality.append({"measurement": rec.id, "roles": roles, "position": pos, **f})
        crit = [f for f in q if f["level"] == "critical"]
        if crit:
            self.s.measurements.remove(rec)  # never solve from a bad sweep
            self.s.save()
            self._ask(f"quality:{rec.id}", f"Sweep of {'+'.join(roles)} at {pos} failed the quality gate: "
                      + "; ".join(f["message"] for f in crit) + ". Fix it and confirm; I will re-measure.",
                      findings=crit)
        for f in q:
            if f["level"] == "warning":
                log.append(f"quality warning ({'+'.join(roles)} @{pos}): {f['message']}")
        return rec, resp

    async def _phase_baseline(self, log) -> str:
        r = self.rig
        roles = [c.role for c in self.cfg.channels]
        ref = self.cfg.timing_reference_channel
        positions = list(self.cfg.mic_positions)
        if any(self.s.dsp.get(rl).peq for rl in roles):
            # Measure without Rewdio's PEQ: deep cuts starve the timing fit, and the filters are known
            # maths, so the solver adds them back exactly.
            for rl in roles:
                self.s.dsp.get(rl).peq = {}
            await r.write_outputs()
            log.append("bypassed Rewdio PEQ for baseline sweeps")
        if self.wf.pass_no % 2 == 1:
            positions = positions[::-1]  # fewer mic moves: start where the last verify ended
        for pos in positions:
            if self._baseline_done(pos, roles, ref):
                continue  # resumed after a restart: don't send the user back to a finished seat
            self._mic_to(pos, "baseline")
            have = {m.roles[0] for m in self.s.find(kind="solo", position=pos, pass_no=self.wf.pass_no)}
            for role in roles:
                if role not in have:
                    await self._gated_measure([role], "solo", pos, log)
                    log.append(f"pass {self.wf.pass_no} {pos}: {role} solo")
            await self._repeat_check(pos, ref, log)
            if self.cfg.timing_mode == TimingMode.SUM_FIT:
                await self._sumfit_sweeps(pos, roles, ref, log)
        return "SOLVE"

    def _baseline_done(self, pos, roles, ref) -> bool:
        """Every sweep this position needs in this pass is already on disk."""
        n = self.wf.pass_no
        have = {m.roles[0] for m in self.s.find(kind="solo", position=pos, pass_no=n)}
        if not set(roles) <= have:
            return False
        q = self.cfg.tuning.quality
        if q.repeat_check and n == 0 and pos == self.cfg.mic_positions[0] and not self.s.find(
                kind="repeat", position=pos, pass_no=0):
            return False
        if self.cfg.timing_mode == TimingMode.SUM_FIT:
            combined = self.s.find(kind="combined", position=pos, pass_no=n)
            for role in roles:
                if role != ref and len({m.extra.get("probe_ms") for m in combined if m.extra.get("x") == role}) < 2:
                    return False
        return True

    async def _repeat_check(self, pos, ref, log) -> None:
        q = self.cfg.tuning.quality
        if not q.repeat_check or self.wf.pass_no != 0 or pos != self.cfg.mic_positions[0]:
            return
        if self.s.find(kind="repeat", position=pos, pass_no=0):
            return
        first = self.s.find(kind="solo", position=pos, pass_no=0, roles=[ref])[0]
        _, again = await self.rig.measure([ref], "repeat", pos)
        sd = repeatability_db(self.s.response(first.id), again, self.cfg)
        log.append(f"repeatability of {ref} at {pos}: {sd:.2f} dB std")
        if sd > q.max_repeat_std_db:
            self.wf.quality.append({"level": "warning", "code": "repeatability", "position": pos,
                                    "message": f"two identical sweeps differ by {sd:.2f} dB std: noise, HVAC, a moving "
                                               f"mic stand or a time-varying DSP; results will be less certain"})
            log.append("quality warning: poor repeatability")

    async def _sumfit_sweeps(self, pos, roles, ref, log) -> None:
        r = self.rig
        for role in roles:
            if role == ref:
                continue
            have = {m.extra.get("probe_ms") for m in self.s.find(kind="combined", position=pos, pass_no=self.wf.pass_no)
                    if m.extra.get("x") == role}
            probe_ms = self._sumfit_probe_ms(role)
            if self.s.dsp.get(role).delay_ms + probe_ms > self.cfg.dsp_max_delay_ms:
                probe_ms = -probe_ms  # probe <= max/2, so one direction always fits
            for probe in (0.0, probe_ms):
                if probe in have:
                    continue
                if probe:
                    await r.nudge_delay(role, probe)
                try:
                    await self._gated_measure([ref, role], "combined", pos, log, x=role, probe_ms=probe)
                finally:
                    if probe:
                        await r.nudge_delay(role, -probe)
                log.append(f"pass {self.wf.pass_no} {pos}: {ref}+{role} combined (probe {probe} ms)")

    def timeline(self, pass_no: int | None = None) -> tuple[dict[str, dict[str, Measured]], list[str]]:
        """Solo measurements for a pass, on one time base and one drive level."""
        pass_no = self.wf.pass_no if pass_no is None else pass_no
        ref = self.cfg.timing_reference_channel
        level = self.wf.crawl.get("level_dbfs", self.rig.level_dbfs)
        warnings: list[str] = []
        data: dict[str, dict[str, Measured]] = {}
        for pos in self.cfg.mic_positions:
            solos = {m.roles[0]: m for m in self.s.find(kind="solo", position=pos, pass_no=pass_no)}
            if set(solos) != {c.role for c in self.cfg.channels}:
                raise RuntimeError(f"pass {pass_no} at {pos} is missing solo measurements")

            def load(rec):
                return self.s.response(rec.id).scaled(10 ** ((level - rec.level_dbfs) / 20))

            row = {role: Measured(load(rec), rec.snapshot.get(role), self.s.direct(rec.id), level - rec.level_dbfs)
                   for role, rec in solos.items()}
            if self.cfg.timing_mode == TimingMode.SUM_FIT:
                combos = self.s.find(kind="combined", position=pos, pass_no=pass_no)
                for role, meas in row.items():
                    if role == ref:
                        continue
                    pairs = [(load(c).mag_db, c.extra["probe_ms"]) for c in combos if c.extra.get("x") == role]
                    fit = fit_relative_delay(row[ref].resp, meas.resp, pairs, *self._sumfit_band(role),
                                             self.cfg.tuning.alignment)
                    meas.resp = meas.resp.delayed(fit.delay_ms)
                    if not fit.confident:
                        warnings.append(f"{pos} {role}: timing fit residual {fit.residual_db:.2f} dB, runner-up "
                                        f"{fit.runner_up_residual_db:.2f} dB (low confidence)")
            data[pos] = row
        return data, warnings
