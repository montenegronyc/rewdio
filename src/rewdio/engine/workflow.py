"""Autopilot: the calibration state machine.

`step()` advances as far as it can without a human. It stops and returns
`user_action_required` only for physical tasks (move the mic, move a sub), and
resumes from the persisted state after `confirm()`.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from ..analysis.alignment import fit_relative_delay
from ..analysis.metrics import deviation, summarize
from ..analysis.placement import Candidate, geometric_correction, rank_positions
from ..analysis.response import Response, band_level_db, smooth_db, spatial_average_db
from ..analysis.solver import Measured, converged, evaluate, solve
from ..analysis.target import target_db
from ..config import ChannelKind, TimingMode
from ..minidsp.state import DspState
from ..rew.client import RewError
from .rig import Rig

PHASES = ["PREFLIGHT", "GAIN_STAGING", "SUB_CRAWL", "BASELINE", "SOLVE", "VERIFY", "DONE"]


class NeedUser(Exception):
    def __init__(self, action: dict):
        super().__init__(action.get("instructions", "user action required"))
        self.action = action


class Autopilot:
    def __init__(self, rig: Rig):
        self.rig = rig
        self.s = rig.s

    @property
    def cfg(self):
        return self.s.config

    @property
    def wf(self):
        return self.s.workflow

    # -- user actions -------------------------------------------------------------------

    def _ask(self, key: str, instructions: str, **fields) -> None:
        """Raise NeedUser unless this action was already confirmed."""
        done = self.wf.crawl.setdefault("_confirmed", {})
        if key in done:
            return
        action = {"key": key, "instructions": instructions, **fields}
        self.wf.pending_action = action
        self.s.save()
        raise NeedUser(action)

    def confirm(self, key: str | None = None, xyz: list[float] | None = None, note: str | None = None) -> dict:
        pa = self.wf.pending_action
        if pa is None:
            return {"ok": False, "message": "nothing is waiting on the user"}
        if key is not None and key != pa["key"]:
            return {"ok": False, "message": f"pending action is {pa['key']!r}, not {key!r}"}
        self.wf.crawl.setdefault("_confirmed", {})[pa["key"]] = {"xyz": xyz, "note": note}
        if pa.get("mic_to"):
            self.wf.mic_at = pa["mic_to"]
            if xyz is not None:
                self.wf.crawl.setdefault("xyz", {})[pa["mic_to"]] = list(xyz)
        self.wf.pending_action = None
        self.s.save()
        return {"ok": True, "confirmed": pa["key"], "action": pa}

    def _confirmed(self, key: str) -> dict | None:
        return self.wf.crawl.get("_confirmed", {}).get(key)

    def _forget(self, prefix: str) -> None:
        c = self.wf.crawl.get("_confirmed", {})
        for k in [k for k in c if k.startswith(prefix)]:
            del c[k]

    def _mic_to(self, position: str, why: str, xyz_hint: list[float] | None = None) -> None:
        if self.wf.mic_at == position:
            return
        key = f"mic:{position}:pass{self.wf.pass_no}:{why}"
        hint = ""
        if position in self.cfg.seat_xyz:
            hint = f" (coordinates {list(self.cfg.seat_xyz[position])} m)"
        elif xyz_hint:
            hint = f" (suggested coordinates {xyz_hint} m)"
        self._ask(key, f"Move the measurement mic to {position}{hint}. Ear height, pointing at the ceiling "
                       f"(90° calibration), clear of reflective surfaces. Then confirm.",
                  mic_to=position, xyz_suggestion=xyz_hint)

    # -- the loop -------------------------------------------------------------------------

    async def step(self, max_sweeps: int = 60) -> dict:
        """Run until a human is needed, the run converges, or the sweep budget is spent."""
        log: list[str] = []
        start = len(self.s.measurements)
        try:
            while not self.wf.done:
                if len(self.s.measurements) - start >= max_sweeps:
                    return self._report("paused", log, note=f"sweep budget of {max_sweeps} used; call again to continue")
                phase = self.wf.phase
                handler = getattr(self, f"_phase_{phase.lower()}")
                nxt = await handler(log)
                self.wf.history.append({"phase": phase, "next": nxt, "sweeps": len(self.s.measurements)})
                self.wf.phase = nxt
                self.s.save()
            return self._report("done", log)
        except NeedUser as e:
            return self._report("user_action_required", log, action=e.action)

    def _report(self, status: str, log: list[str], **kw) -> dict:
        out: dict[str, Any] = {"status": status, "phase": self.wf.phase, "pass": self.wf.pass_no,
                               "sweeps_total": len(self.s.measurements), "log": log, **kw}
        if self.wf.last_proposal:
            out["latest"] = self.wf.last_proposal
        return out

    async def _phase_preflight(self, log) -> str:
        r = self.rig
        rep = await r.prepare_rew()
        log.append(f"REW {rep['rew_version']}; timing reference {rep['timing_reference']}")
        if not rep["mic_cal"]:
            log.append("WARNING: no mic calibration loaded in REW. Load the SoundID Reference cal file "
                       "(set mic_cal_path) for accurate SPL and HF response.")
        devs = await r.dsp.devices()
        log.append(f"miniDSP: {devs[0].get('product_name') if devs else 'none found'}")
        if not devs:
            raise RuntimeError("minidsp-rs sees no device")
        await r.assert_baseline()
        log.append("DSP baseline asserted on " + ", ".join(c.role for c in self.cfg.channels))
        self._mic_to(self.cfg.mic_positions[0], "start")
        return "GAIN_STAGING"

    async def _phase_gain_staging(self, log) -> str:
        r = self.rig
        sw = self.cfg.sweep
        pos = self.wf.mic_at or "unknown"
        mains = [c.role for c in self.cfg.mains] or [c.role for c in self.cfg.channels]
        for _ in range(8):
            try:
                _, resp = await r.measure(mains, "staging", pos)
            except RewError as e:
                if r.level_dbfs <= sw.min_level_dbfs:
                    raise
                log.append(f"sweep at {r.level_dbfs} dBFS aborted ({e}); backing off 10 dB")
                await r.set_level(r.level_dbfs - 10)
                continue
            mid = band_level_db(resp, None, 500, 2000)
            peak = float(np.max(smooth_db(resp.freqs, resp.mag_db, 6)[(resp.freqs > 20) & (resp.freqs < 20000)]))
            err = sw.target_spl_db - mid
            log.append(f"gain staging @ {r.level_dbfs} dBFS: mid {mid:.1f} dB SPL, peak {peak:.1f}")
            if abs(err) < 2 or (err > 0 and r.level_dbfs >= sw.max_level_dbfs) or (err < 0 and r.level_dbfs <= sw.min_level_dbfs):
                break
            step = float(np.clip(err, -20, 10))
            if peak + step > sw.max_spl_db - 3:
                step = sw.max_spl_db - 3 - peak
            await r.set_level(r.level_dbfs + step)
        self.wf.crawl["level_dbfs"] = r.level_dbfs
        return "SUB_CRAWL" if self.cfg.do_sub_crawl and not self.wf.crawl.get("result") else "BASELINE"

    # -- reciprocity crawl --------------------------------------------------------------

    def _crawl_spots(self) -> list[dict]:
        n = self.cfg.crawl_positions
        dims = self.cfg.room_dims_m
        spots = []
        if dims:
            L, W, _ = dims
            m = 0.35  # distance from walls
            ring = [(m, m), (m, W / 2), (m, W - m), (L / 3, W - m), (2 * L / 3, W - m), (L - m, W - m),
                    (L - m, W / 2), (L - m, m), (2 * L / 3, m), (L / 3, m), (m, W / 4), (m, 3 * W / 4),
                    (L / 2, m), (L / 2, W - m), (L - m, W / 4), (L - m, 3 * W / 4)]
            for i in range(n):
                x, y = ring[i % len(ring)]
                spots.append({"label": f"C{i + 1}", "xyz": [round(x, 2), round(y, 2), 0.25]})
        else:
            for i in range(n):
                spots.append({"label": f"C{i + 1}", "xyz": None})
        return spots

    async def _phase_sub_crawl(self, log) -> str:
        r = self.rig
        crawl = self.wf.crawl
        subs = self.cfg.subs
        if not subs:
            raise RuntimeError("sub crawl needs at least one sub channel")
        sub = subs[0].role
        seat = self.cfg.mic_positions[0]
        seat_hint = f" ({list(self.cfg.seat_xyz[seat])} m)" if seat in self.cfg.seat_xyz else ""
        self._ask("crawl:place_sub", f"Reverse calculation set-up: put {sub} AT the listening position {seat}{seat_hint}, "
                                     f"driver as close to ear height as practical (on the seat), facing where you face. "
                                     f"Keep the other subs off or disconnected if they are not on miniDSP outputs. Confirm when done.",
                  sim={"move_speaker": sub, "to_seat": seat})
        spots = crawl.setdefault("spots", self._crawl_spots())
        done = crawl.setdefault("measured", {})
        for spot in spots:
            lab = spot["label"]
            if lab in done:
                continue
            key = f"crawl:mic:{lab}"
            self._ask(key, f"Move the mic to candidate sub location {lab}"
                           + (f" (suggested {spot['xyz']} m, origin front-left floor corner)" if spot["xyz"] else "")
                           + ", at the height the sub's woofer will be (near the floor). Confirm, ideally with the "
                             "actual x,y,z you used.",
                      mic_to=lab, xyz_suggestion=spot["xyz"])
            c = self._confirmed(key) or {}
            rec, _ = await r.measure([sub], "crawl", lab, seat=seat, xyz=c.get("xyz") or spot["xyz"])
            done[lab] = {"id": rec.id, "xyz": c.get("xyz") or spot["xyz"]}
            log.append(f"crawl {lab}: measured")
            self.s.save()
        res = self.analyze_crawl()
        crawl["result"] = res
        best = res["best_pairs"][0]
        a, b = best["positions"]
        xyz = {k: done[k]["xyz"] for k in (a, b)}
        log.append(f"crawl best pair {a}+{b}: in-band std {best['std_db']} dB ({res['confidence']})")
        roles = [s.role for s in subs]
        moves = f"{roles[0]} to {a}{' ' + str(xyz[a]) if xyz[a] else ''}"
        if len(roles) > 1:
            moves += f" and {roles[1]} to {b}{' ' + str(xyz[b]) if xyz[b] else ''}"
        else:
            moves += f" (a second sub at {b} would score {best['std_db']} dB)"
        self._ask("crawl:place_final", f"Placement result: move {moves}. Put the mic back at {seat}. Confirm when done.",
                  mic_to=seat, recommendation=res["best_pairs"][:3],
                  sim={"move_subs": {roles[i]: xyz[p] for i, p in enumerate((a, b)[:len(roles)])}})
        self.wf.mic_at = seat
        return "BASELINE"

    def analyze_crawl(self) -> dict:
        crawl = self.wf.crawl
        done = crawl.get("measured", {})
        seat = self.cfg.mic_positions[0]
        cands = [Candidate(lab, {seat: self.s.response(v["id"])}, tuple(v["xyz"]) if v.get("xyz") else None)
                 for lab, v in done.items()]
        if self.cfg.timing_mode in (TimingMode.LOOPBACK, TimingMode.ACOUSTIC):
            mode = "absolute"
        elif all(c.xyz for c in cands) and seat in self.cfg.seat_xyz:
            mode = "geometric"
            cands = geometric_correction(cands, {seat: tuple(self.cfg.seat_xyz[seat])})
        else:
            mode = "magnitude"
        freqs = cands[0].by_seat[seat].freqs
        return rank_positions(cands, target_db(freqs, self.cfg.target), mode, self.cfg.dsp_max_delay_ms)

    # -- baseline measurements ------------------------------------------------------------

    def _sumfit_band(self, role: str) -> tuple[float, float]:
        fc = self.cfg.crossover_hz
        return (fc / 2, fc * 2) if self.cfg.channel(role).kind == ChannelKind.SUB else (150.0, 3000.0)

    def _sumfit_probe_ms(self, role: str) -> float:
        lo, hi = self._sumfit_band(role)
        return round(min(1000.0 / (4 * math.sqrt(lo * hi)), self.cfg.dsp_max_delay_ms / 2), 2)

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
            self._mic_to(pos, "baseline")
            have = {m.roles[0] for m in self.s.find(kind="solo", position=pos, pass_no=self.wf.pass_no)}
            for role in roles:
                if role not in have:
                    await r.measure([role], "solo", pos)
                    log.append(f"pass {self.wf.pass_no} {pos}: {role} solo")
            if self.cfg.timing_mode == TimingMode.SUM_FIT:
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
                            await r.measure([ref, role], "combined", pos, x=role, probe_ms=probe)
                        finally:
                            if probe:
                                await r.nudge_delay(role, -probe)
                        log.append(f"pass {self.wf.pass_no} {pos}: {ref}+{role} combined (probe {probe} ms)")
        return "SOLVE"

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

            row = {role: Measured(load(rec), rec.snapshot.get(role)) for role, rec in solos.items()}
            if self.cfg.timing_mode == TimingMode.SUM_FIT:
                combos = self.s.find(kind="combined", position=pos, pass_no=pass_no)
                for role, meas in row.items():
                    if role == ref:
                        continue
                    pairs = [(load(c).mag_db, c.extra["probe_ms"]) for c in combos if c.extra.get("x") == role]
                    lo, hi = self._sumfit_band(role)
                    fit = fit_relative_delay(row[ref].resp, meas.resp, pairs, lo, hi)
                    meas.resp = meas.resp.delayed(fit.delay_ms)
                    if not fit.confident:
                        warnings.append(f"{pos} {role}: timing fit residual {fit.residual_db:.2f} dB, runner-up "
                                        f"{fit.runner_up_residual_db:.2f} dB (low confidence)")
            data[pos] = row
        return data, warnings

    # -- solve, apply, verify ------------------------------------------------------------

    async def _phase_solve(self, log) -> str:
        data, warnings = self.timeline()
        log.extend(warnings)
        prop = solve(self.cfg, data, self.s.dsp, use_peq=False)
        ok, fails = converged(self.cfg, prop.metrics)
        used_peq = False
        if not ok and self.cfg.allow_peq and self.cfg.subs:
            with_peq = solve(self.cfg, data, self.s.dsp, use_peq=True)
            if with_peq.metrics["bass_deviation"]["std_db"] < prop.metrics["bass_deviation"]["std_db"] - 0.2:
                prop, used_peq = with_peq, True
                ok, fails = converged(self.cfg, prop.metrics)
        await self.rig.apply(prop.state)
        log.extend(prop.notes)
        log.append(f"applied pass {self.wf.pass_no} solution{' with PEQ' if used_peq else ''}; predicted {prop.metrics}")
        self.wf.last_proposal = {"pass": self.wf.pass_no, "state": _state_summary(self.cfg, prop.state),
                                 "predicted": prop.metrics, "predicted_ok": ok, "predicted_fails": fails,
                                 "used_peq": used_peq, "notes": prop.notes,
                                 "_pred": {p: r.to_dict() for p, r in prop.predicted.items()}}
        return "VERIFY"

    async def _phase_verify(self, log) -> str:
        r = self.rig
        roles = [c.role for c in self.cfg.channels]
        positions = list(self.cfg.mic_positions)
        if self.wf.mic_at in positions:  # start where the mic already is
            positions.sort(key=lambda p: p != self.wf.mic_at)
        measured: dict[str, Response] = {}
        for pos in positions:
            self._mic_to(pos, "verify")
            have = self.s.find(kind="system", position=pos, pass_no=self.wf.pass_no)
            if have:
                measured[pos] = self.s.response(have[-1].id)
            else:
                _, measured[pos] = await r.measure(roles, "system", pos)
        lp = self.wf.last_proposal
        pred = {p: Response.from_dict(d) for p, d in lp["_pred"].items()}
        fit_err = []
        for p in positions:
            a = smooth_db(pred[p].freqs, pred[p].mag_db, 6)
            b = smooth_db(measured[p].freqs, measured[p].mag_db, 6)
            m = (measured[p].freqs >= 20) & (measured[p].freqs <= 500)
            fit_err.append(float(np.sqrt(np.mean((a[m] - b[m]) ** 2))))
        freqs = measured[positions[0]].freqs
        avg = spatial_average_db([measured[p] for p in positions])
        meas_dev = deviation(freqs, avg, target_db(freqs, self.cfg.target), 20, 200, smoothing=6)
        metrics = {**lp["predicted"], "bass_deviation": meas_dev,
                   "prediction_error_db": round(float(np.mean(fit_err)), 2)}
        ok, fails = converged(self.cfg, metrics)
        if metrics["prediction_error_db"] > self.cfg.convergence.max_prediction_error_db:
            ok = False
            fails.append(f"prediction error {metrics['prediction_error_db']} dB: model and room disagree, re-measuring")
        log.append(f"verify pass {self.wf.pass_no}: measured {metrics}")
        lp["measured"] = metrics
        lp["measured_ok"] = ok
        lp["measured_fails"] = fails
        lp["measured_summary"] = {p: summarize(measured[p], self.cfg.crossover_hz) for p in positions}
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
        if prev and metrics["prediction_error_db"] <= self.cfg.convergence.max_prediction_error_db \
                and prev[-1]["verify_std"] - score < 0.15:
            best = DspState.model_validate(self.wf.best["state"])
            await self.rig.apply(best)
            self.wf.done = True
            lp["limit_reached"] = True
            log.append("plateau: the model matches the room and another pass will not improve it. Remaining error "
                       "is physical (" + "; ".join(fails) + "). Next lever: sub placement (enable do_sub_crawl) "
                       "or more mic positions for a spatial average.")
            return "DONE"
        if self.wf.pass_no + 1 >= self.cfg.convergence.max_iterations:
            best = DspState.model_validate(self.wf.best["state"])
            await self.rig.apply(best)
            self.wf.done = True
            log.append(f"iteration limit reached; kept the best pass ({self.wf.best['pass']}): " + "; ".join(fails))
            return "DONE"
        self.wf.pass_no += 1
        log.append("not converged (" + "; ".join(fails) + "); re-measuring under the new settings")
        return "BASELINE"

    async def _phase_done(self, log) -> str:
        self.wf.done = True
        return "DONE"


def _state_summary(cfg, st: DspState) -> dict:
    out = {}
    for c in cfg.channels:
        o = st.get(c.role)
        out[c.role] = {"gain_db": o.gain_db, "delay_ms": o.delay_ms, "invert": o.invert,
                       "peq": [f"{p.freq_hz:.0f}Hz {p.gain_db:+.1f}dB Q{p.q}" for _, p in sorted(o.peq.items())]}
    return out
