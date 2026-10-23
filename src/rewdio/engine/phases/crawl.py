"""SUB_CRAWL: reverse calculation of sub placement by reciprocity."""

from __future__ import annotations

from ...analysis.placement import Candidate, geometric_correction, rank_positions
from ...analysis.target import target_db
from ...config import TimingMode
from .base import PhaseBase


class CrawlPhase(PhaseBase):
    def _crawl_spots(self) -> list[dict]:
        n = self.cfg.crawl_positions
        dims = self.cfg.room_dims_m
        ct = self.cfg.tuning.crawl
        if not dims:
            return [{"label": f"C{i + 1}", "xyz": None} for i in range(n)]
        L, W, _ = dims
        m = ct.wall_distance_m
        ring = [(m, m), (m, W / 2), (m, W - m), (L / 3, W - m), (2 * L / 3, W - m), (L - m, W - m),
                (L - m, W / 2), (L - m, m), (2 * L / 3, m), (L / 3, m), (m, W / 4), (m, 3 * W / 4),
                (L / 2, m), (L / 2, W - m), (L - m, W / 4), (L - m, 3 * W / 4)]
        return [{"label": f"C{i + 1}", "xyz": [round(ring[i % len(ring)][0], 2), round(ring[i % len(ring)][1], 2),
                                                ct.mic_height_m]} for i in range(n)]

    async def _phase_sub_crawl(self, log) -> str:
        crawl = self.wf.crawl
        subs = self.cfg.subs
        if not subs:
            raise RuntimeError("sub crawl needs at least one sub channel")
        sub = subs[0].role
        seat = self.cfg.mic_positions[0]
        seat_hint = f" ({list(self.cfg.seat_xyz[seat])} m)" if seat in self.cfg.seat_xyz else ""
        self._ask("crawl:place_sub", f"Reverse calculation set-up: put {sub} AT the listening position {seat}{seat_hint}, "
                                     f"driver as close to ear height as practical (on the seat), facing where you face. "
                                     f"Keep any other subs off. Confirm when done.",
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
                             "actual x,y,z you used.", mic_to=lab, xyz_suggestion=spot["xyz"])
            c = self._confirmed(key) or {}
            rec, _ = await self.rig.measure([sub], "crawl", lab, seat=seat, xyz=c.get("xyz") or spot["xyz"])
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
        done = self.wf.crawl.get("measured", {})
        seat = self.cfg.mic_positions[0]
        t = self.cfg.tuning
        cands = [Candidate(lab, {seat: self.s.response(v["id"])}, tuple(v["xyz"]) if v.get("xyz") else None)
                 for lab, v in done.items()]
        if self.cfg.timing_mode in (TimingMode.LOOPBACK, TimingMode.ACOUSTIC):
            mode = "absolute"
        elif all(c.xyz for c in cands) and seat in self.cfg.seat_xyz:
            mode = "geometric"
            cands = geometric_correction(cands, {seat: tuple(self.cfg.seat_xyz[seat])}, t.physics.speed_of_sound_m_s)
        else:
            mode = "magnitude"
        freqs = cands[0].by_seat[seat].freqs
        return rank_positions(cands, target_db(freqs, self.cfg.target), mode, self.cfg.dsp_max_delay_ms,
                              t.bands.crawl_eval_hz, t.pair, t.bands.bass_smoothing, t.crawl.top_pairs)
