"""SUB_CRAWL: reverse calculation of sub placement by reciprocity."""

from __future__ import annotations

from ...analysis.placement import Candidate, geometric_correction, rank_positions
from ...analysis.target import target_db
from ...config import TimingMode
from ..grid import build_grid, route, setup_instructions, where
from .base import PhaseBase


class CrawlPhase(PhaseBase):
    def grid_cells(self) -> list[dict]:
        if not self.cfg.room_dims_m:
            raise RuntimeError("the crawl grid needs room_dims_m (length, width, height); set it with configure")
        return build_grid(self.cfg.room_dims_m, self.cfg.crawl_grid, set(self.wf.crawl.get("excluded", [])))

    def _crawl_spots(self) -> list[dict]:
        g = self.cfg.crawl_grid
        if g.enabled:
            done = self.wf.crawl.get("measured", {})
            at = (done.get(self.wf.mic_at) or {}).get("xyz")  # resume from where the mic is now
            path = route(self.grid_cells(), g.max_cells, dims=self.cfg.room_dims_m if g.mode == "perimeter" else None,
                         start_xyz=at, done=set(done))
            return [{"label": c["code"], "xyz": c["xyz"], "cell": c} for c in path]
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
        # Reciprocity swaps two points: the woofer stands in for the listener's ears, so height matters
        ear = (f"about {self.cfg.seat_xyz[seat][2] * 100:.0f} cm off the floor, your ear height"
               if seat in self.cfg.seat_xyz else "at your ear height")
        self._ask("crawl:place_sub", f"Reverse calculation set-up: put {sub} AT the listening position {seat}{seat_hint}, "
                                     f"raised on the seat or a stand so the centre of its woofer is {ear}, facing where "
                                     f"you face. Keep any other subs off. Confirm when done.",
                  sim={"move_speaker": sub, "to_seat": seat})
        g = self.cfg.crawl_grid
        if g.enabled:
            spots = self._crawl_spots()  # recomputed each time: cells excluded mid-crawl drop out
            self._ask("crawl:grid_setup", setup_instructions(g, len(spots)), grid=True)
        else:
            spots = crawl.setdefault("spots", self._crawl_spots())
        crawl["spots"] = spots
        done = crawl.setdefault("measured", {})
        redo = crawl.setdefault("redo", [])  # spots the user asked to measure again go first
        for spot in sorted(spots, key=lambda sp: sp["label"] not in redo):
            lab = spot["label"]
            if lab in done:
                continue
            key = f"crawl:mic:{lab}"
            if g.enabled:
                c = spot["cell"]
                text = (f"Move the mic to {lab}: {where(c, g.open_walls)}, {g.mic_height_m * 100:.0f} cm off the "
                        f"floor, pointing at the ceiling. Then tap 'Mic is here'.")
            else:
                text = (f"Move the mic to candidate sub location {lab}"
                        + (f" (suggested {spot['xyz']} m, origin front-left floor corner)" if spot["xyz"] else "")
                        + ", at the height the sub's woofer will be (near the floor). Confirm, ideally with the "
                          "actual x,y,z you used.")
            self._ask(key, text, mic_to=lab, xyz_suggestion=spot["xyz"], cell=lab if g.enabled else None)
            c = self._confirmed(key) or {}
            rec, _ = await self._gated_measure([sub], "crawl", lab, log, seat=seat, xyz=c.get("xyz") or spot["xyz"])
            done[lab] = {"id": rec.id, "xyz": c.get("xyz") or spot["xyz"]}
            if lab in redo:
                redo.remove(lab)
            log.append(f"crawl {lab}: measured")
            self.s.save()
        res = self.analyze_crawl()
        crawl["result"] = res
        best = res["best_pairs"][0]
        roles = [s.role for s in subs]
        if len(roles) > 1:
            a, b = best["positions"]
            log.append(f"crawl best pair {a}+{b}: in-band std {best['std_db']} dB ({res['confidence']})")
        else:
            # One sub: the best place for it alone, which is often not either half of the best pair
            single = res["best_single"][0]
            a, b = single["position"], None
            log.append(f"crawl best single spot {a}: in-band std {single['std_db']} dB ({res['confidence']})")
        xyz = {k: done[k]["xyz"] for k in (a, b) if k}
        self._ask("crawl:place_final", self.placement_text(roles, (a, b), res),
                  mic_to=seat, recommendation=res["best_pairs"][:3], place={roles[i]: p for i, p in enumerate((a, b)[:len(roles)])},
                  sim={"move_subs": {roles[i]: xyz[p] for i, p in enumerate((a, b)[:len(roles)])}})
        self.wf.mic_at = seat
        return "BASELINE"

    def placement_text(self, roles: list[str], spots: tuple, res: dict) -> str:
        """The winner (or the user's pick) in plain words, the ranking with what each alternative costs, and what to
        do now."""
        seat = self.cfg.mic_positions[0]
        ear = self.cfg.seat_xyz.get(seat)
        mic = f", {ear[2] * 100:.0f} cm up" if ear else ""
        if len(roles) > 1:
            head = f"The best pair is {spots[0]} + {spots[1]}."
            ranks = [f"{i + 1}. {'+'.join(p['positions'])}: {p['std_db']} dB" for i, p in enumerate(res["best_pairs"][:5])]
        else:
            singles = sorted(res["all_singles"], key=lambda x: x["std_db"])
            top = singles[0]
            pick = next((x for x in singles if x["position"] == spots[0]), top)
            rank = singles.index(pick) + 1
            head = (f"The winning spot is {top['position']}." if pick is top else
                    f"You chose {pick['position']} (rank {rank}): {pick['std_db'] - top['std_db']:+.1f} dB vs the "
                    f"winner {top['position']}.")
            ranks = [f"{i + 1}. {x['position']}: {x['std_db']} dB"
                     + (f" ({x['std_db'] - top['std_db']:+.1f})" if i else " (best)")
                     + f", swing {x['peak_to_peak_db']:.0f} dB" for i, x in enumerate(singles[:5])]
        moves = " and ".join(self._move_text(r, p) for r, p in zip(roles, spots) if p)
        return (f"{head} Ranking (lower is flatter bass at your seat; swing = biggest peak to deepest dip): "
                f"{'; '.join(ranks)}. To use another spot, tap it on the room plan. "
                f"Now move {moves}. Then put the mic back at your seat ({seat}){mic}, pointing at the ceiling. "
                f"Confirm when the sub{'s are' if len(roles) > 1 else ' is'} moved and the mic is at your seat.")

    def _move_text(self, role: str, spot: str) -> str:
        """'SUB1 from your seat to A1 (the best spot): 35 cm from the front wall and 35 cm from the left wall, on
        the floor ...' in plain words, using the grid's nearest-wall distances."""
        g = self.cfg.crawl_grid
        cell = next((c for c in self.grid_cells() if c["code"] == spot), None) if g.enabled else None
        here = where(cell, g.open_walls) if cell else "the spot you measured"
        return (f"{role} from your seat to {spot}: {here}. Stand it on the floor with the centre of its woofer "
                f"where the mic was ({g.mic_height_m * 100:.0f} cm up), facing into the room")

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
