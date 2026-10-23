"""SOLVE and VERIFY: solve from the baseline, apply, measure the real result, decide what next."""

from __future__ import annotations

import numpy as np

from ...analysis.diagnostics import diagnose
from ...analysis.metrics import deviation, summarize
from ...analysis.response import Response, smooth_db, spatial_average_db
from ...analysis.solver import converged, footprint, peq_earns_place, solve
from ...analysis.target import target_db
from ...minidsp.state import DspState
from ...rew.client import RewError
from ...rew.extras import alignment_tool_delay, predicted_with_peq
from .base import PhaseBase


def state_summary(cfg, st: DspState) -> dict:
    out = {}
    for c in cfg.channels:
        o = st.get(c.role)
        out[c.role] = {"gain_db": o.gain_db, "delay_ms": o.delay_ms, "invert": o.invert,
                       "peq": [f"{p.freq_hz:.0f}Hz {p.gain_db:+.1f}dB Q{p.q}" for _, p in sorted(o.peq.items())]}
    return out


class SolveVerifyPhase(PhaseBase):
    def _data_pass(self) -> int:
        """The pass whose baseline sweeps the current solution was solved from."""
        return self.wf.crawl.get("data_pass", self.wf.pass_no)

    async def _phase_solve(self, log) -> str:
        data, warnings = self.timeline(self._data_pass())
        log.extend(warnings)
        prop = solve(self.cfg, data, self.s.dsp, use_peq=False)
        ok, fails = converged(self.cfg, prop.metrics)
        used_peq = False
        pq = self.cfg.tuning.peq
        # PEQ is the last rung: only after a verified gain/delay/polarity-only pass still missed the target
        unlocked = self.wf.crawl.get("peq_unlocked", False) or not pq.only_after_verified_pass
        if not ok and self.cfg.allow_peq and self.cfg.subs and unlocked:
            with_peq = solve(self.cfg, data, self.s.dsp, use_peq=True)
            earns, why = peq_earns_place(self.cfg, prop, with_peq)
            log.append(why)
            if earns:
                prop, used_peq = with_peq, True
                ok, fails = converged(self.cfg, prop.metrics)
        elif not ok and self.cfg.allow_peq and self.cfg.subs:
            log.append("PEQ held back: first verifying what gain, delay and polarity alone achieve")
        crosscheck = await self._crosscheck(prop) if self.cfg.tuning.crosscheck.enabled else {}
        await self.rig.apply(prop.state)
        log.extend(prop.notes)
        log.append(f"applied pass {self.wf.pass_no} solution{' with PEQ' if used_peq else ''}; predicted {prop.metrics}")
        self.wf.last_proposal = {"pass": self.wf.pass_no, "state": state_summary(self.cfg, prop.state),
                                 "predicted": prop.metrics, "predicted_ok": ok, "predicted_fails": fails,
                                 "used_peq": used_peq, "notes": prop.notes, "crosscheck": crosscheck,
                                 "dsp_footprint": footprint(self.cfg, prop.state),
                                 "_pred": {p: r.to_dict() for p, r in prop.predicted.items()}}
        return "VERIFY"

    async def _crosscheck(self, prop) -> dict:
        """Second opinions from REW itself. Disagreements are reported, never silently overridden."""
        cc = self.cfg.tuning.crosscheck
        out: dict = {}
        pos = self.cfg.mic_positions[0]
        solos = {m.roles[0]: m for m in self.s.find(kind="solo", position=pos, pass_no=self._data_pass())}
        mains, subs = [c.role for c in self.cfg.mains], [c.role for c in self.cfg.subs]
        if self.cfg.timing_mode.value != "sum_fit" and mains and subs:
            try:  # REW's alignment tool only means something when the measurements share a time base
                rew = await alignment_tool_delay(self.rig.rew, solos[mains[0]].id, solos[subs[0]].id,
                                                 self.cfg.crossover_hz, self.cfg.dsp_max_delay_ms)
                ours = prop.state.get(subs[0]).delay_ms - prop.state.get(mains[0]).delay_ms
                out["alignment_tool"] = {**rew, "rewdio_delay_ms": round(ours, 2),
                                         "agree": abs(rew["delay_ms"] - ours) <= cc.alignment_disagree_ms}
            except (RewError, KeyError) as e:
                out["alignment_tool"] = {"error": str(e)}
        peq = prop.state.get(subs[0]).peq if subs else {}
        if peq:
            try:
                rec = solos[subs[0]]
                f, rew_db = await predicted_with_peq(self.rig.rew, rec.id, list(peq.values()), cc.rew_equaliser,
                                                     self.cfg.analysis_ppo)
                base = self.s.response(rec.id)
                fs = self.cfg.dsp_sample_rate
                ours = base.mag_db + 20 * np.log10(np.abs(np.prod([p.biquad(fs).response(base.freqs, fs)
                                                                      for p in peq.values()], axis=0)))
                bb = self.cfg.tuning.bands
                m = (base.freqs >= bb.bass_eval_hz[0]) & (base.freqs <= self.cfg.crossover_hz * bb.crossover_span_factor)
                diff = float(np.max(np.abs(np.interp(np.log(base.freqs[m]), np.log(f), rew_db) - ours[m])))
                out["rew_eq_prediction"] = {"max_diff_db": round(diff, 2), "agree": diff <= cc.peq_disagree_db}
            except (RewError, KeyError) as e:
                out["rew_eq_prediction"] = {"error": str(e)}
        return out

    async def _phase_verify(self, log) -> str:
        roles = [c.role for c in self.cfg.channels]
        b = self.cfg.tuning.bands
        positions = list(self.cfg.mic_positions)
        if self.wf.mic_at in positions:  # start where the mic already is
            positions.sort(key=lambda p: p != self.wf.mic_at)
        measured: dict[str, Response] = {}
        for pos in positions:
            self._mic_to(pos, "verify")
            have = self.s.find(kind="system", position=pos, pass_no=self.wf.pass_no)
            measured[pos] = self.s.response(have[-1].id) if have else (await self.rig.measure(roles, "system", pos))[1]
        lp = self.wf.last_proposal
        pred = {p: Response.from_dict(d) for p, d in lp["_pred"].items()}
        lo, hi = b.prediction_check_hz
        fit_err = []
        for p in positions:
            m = (measured[p].freqs >= lo) & (measured[p].freqs <= hi)
            a = smooth_db(pred[p].freqs, pred[p].mag_db, b.bass_smoothing)
            c = smooth_db(measured[p].freqs, measured[p].mag_db, b.bass_smoothing)
            fit_err.append(float(np.sqrt(np.mean((a[m] - c[m]) ** 2))))
        freqs = measured[positions[0]].freqs
        meas_dev = deviation(freqs, spatial_average_db([measured[p] for p in positions]),
                             target_db(freqs, self.cfg.target), *b.bass_eval_hz, b.bass_smoothing)
        metrics = {**lp["predicted"], "bass_deviation": meas_dev, "prediction_error_db": round(float(np.mean(fit_err)), 2)}
        ok, fails = converged(self.cfg, metrics)
        conv = self.cfg.convergence
        if metrics["prediction_error_db"] > conv.max_prediction_error_db:
            ok = False
            fails.append(f"prediction error {metrics['prediction_error_db']} dB: model and room disagree, re-measuring")
        log.append(f"verify pass {self.wf.pass_no}: measured {metrics}")
        lp.update(measured=metrics, measured_ok=ok, measured_fails=fails,
                  measured_summary={p: summarize(measured[p], self.cfg) for p in positions})
        mains_solo = {}
        for c in self.cfg.mains:
            recs = self.s.find(kind="solo", position=positions[0], pass_no=self._data_pass(), roles=[c.role])
            if recs:
                mains_solo[c.role] = self.s.response(recs[-1].id)
        lp["diagnostics"] = diagnose(self.cfg, measured, mains_solo)
        score = meas_dev["std_db"]
        if self.wf.best is None or score < self.wf.best["score"]:
            self.wf.best = {"score": score, "pass": self.wf.pass_no, "state": self.s.dsp.model_dump(mode="json"),
                            "metrics": metrics}
        if ok:
            self.wf.done = True
            log.append("converged")
            return "DONE"
        prev = [h for h in self.wf.history if h.get("verify_std") is not None]
        self.wf.history.append({"verify_std": score, "pass": self.wf.pass_no})
        plateau = prev and metrics["prediction_error_db"] <= conv.max_prediction_error_db \
            and prev[-1]["verify_std"] - score < conv.plateau_improvement_db
        if plateau or self.wf.pass_no + 1 >= conv.max_iterations:
            await self.rig.apply(DspState.model_validate(self.wf.best["state"]))
            self.wf.done = True
            lp["limit_reached"] = bool(plateau)
            if plateau:
                log.append("plateau: the model matches the room and another pass will not improve it. Remaining error "
                           "is physical (" + "; ".join(fails) + "). Next lever: sub placement (enable do_sub_crawl) "
                           "or more mic positions; see diagnostics for modes and boundary nulls.")
            else:
                log.append(f"iteration limit reached; kept the best pass ({self.wf.best['pass']}): " + "; ".join(fails))
            return "DONE"
        self.wf.pass_no += 1
        pq = self.cfg.tuning.peq
        if (pq.only_after_verified_pass and self.cfg.allow_peq and not self.wf.crawl.get("peq_unlocked")
                and metrics["prediction_error_db"] <= conv.max_prediction_error_db):
            # the model is proven on this data: unlock PEQ and re-solve from it, no need to re-measure
            self.wf.crawl["peq_unlocked"] = True
            self.wf.crawl["data_pass"] = lp["pass"] if self.wf.crawl.get("data_pass") is None else self.wf.crawl["data_pass"]
            log.append("not converged (" + "; ".join(fails) + "); gain/delay/polarity alone are exhausted, "
                       "so PEQ is now allowed as a last resort")
            return "SOLVE"
        self.wf.crawl["peq_unlocked"] = self.wf.crawl.get("peq_unlocked", False)
        self.wf.crawl.pop("data_pass", None)
        log.append("not converged (" + "; ".join(fails) + "); re-measuring under the new settings")
        return "BASELINE"
