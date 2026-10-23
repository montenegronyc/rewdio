"""A virtual listening room with speakers, a miniDSP and a mic, loaded from a scenario JSON.

Low frequencies come from a rectangular-room modal sum (symmetric in source and receiver, so
the reciprocity crawl behaves as it does in a real room). Higher frequencies blend into direct
sound plus delayed, decaying diffuse reflections. The DSP has pre-existing crossovers Rewdio does
not know about, and each speaker has its own unknown latency, so the solvers have real work to do.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import numpy as np

from ..minidsp.biquad import Biquad, cascade_response, design, linkwitz_riley4

C = 343.0
P_REF = 20e-6


@dataclass
class Room:
    dims: tuple[float, float, float]
    rt60: float
    f_max_mode: float
    transition_hz: tuple[float, float] = (180.0, 400.0)
    diffuse_scale: float = 0.5
    reflections: list[tuple] = field(default_factory=lambda: [(2.8, 0.5, "specular"), (9.0, 0.4, "diffuse")])

    def __post_init__(self):
        L = np.array(self.dims)
        nmax = (2 * self.f_max_mode * L / C).astype(int) + 1
        modes = [n for n in itertools.product(*(range(k + 1) for k in nmax))
                 if 0 < C / 2 * np.sqrt(np.sum((np.array(n) / L) ** 2)) <= self.f_max_mode]
        self.n = np.array(modes, float)
        self.fn = C / 2 * np.sqrt(np.sum((self.n / L) ** 2, axis=1))
        self.lam = np.prod(np.where(self.n == 0, 1.0, 0.5), axis=1)
        self.delta = 6.91 / self.rt60
        rng = np.random.default_rng(1)
        pts = rng.uniform(0.3, 1.0, (24, 3)) * (L - 0.6) + 0.3
        f = np.linspace(150, 250, 60)
        raw = np.mean([np.mean(np.abs(self._modal(pts[i], pts[i + 1], f)) ** 2) for i in range(0, 24, 2)])
        self.kappa = np.sqrt(((1 / 3.0) ** 2 + (1 / self.critical_distance) ** 2) / raw)

    @property
    def critical_distance(self) -> float:
        return float(0.057 * np.sqrt(np.prod(self.dims) / self.rt60))

    def _psi(self, r) -> np.ndarray:
        return np.prod(np.cos(np.pi * self.n * np.asarray(r) / np.array(self.dims)), axis=1)

    def _modal(self, src, mic, f) -> np.ndarray:
        w = 2 * np.pi * np.asarray(f)[:, None]
        wn = 2 * np.pi * self.fn[None, :]
        num = (self._psi(src) * self._psi(mic) / self.lam)[None, :]
        return (1j * w[:, 0] / (2 * np.pi * 100)) * np.sum(num / (wn ** 2 - w ** 2 + 2j * self.delta * w), axis=1) * 1e4

    def transfer(self, src, mic, f) -> np.ndarray:
        """Pressure at mic per unit source strength (1.0 = free field at 1 m)."""
        f = np.asarray(f, float)
        r = max(float(np.linalg.norm(np.subtract(src, mic))), 0.05)
        delay = np.exp(-2j * np.pi * f * r / C)
        lf = self.kappa * self._modal(src, mic, f) * delay
        key = hashlib.sha1(repr(sorted([tuple(np.round(src, 2)), tuple(np.round(mic, 2))])).encode()).digest()
        rng = np.random.default_rng(int.from_bytes(key[:8], "little"))  # symmetric in src/mic
        knots = np.geomspace(100, 25000, 160)
        reverb = np.zeros(f.shape, complex)
        early = np.zeros(f.shape, complex)
        for refl in self.reflections:
            t_ms, amp = refl[0], refl[1]
            if len(refl) > 2 and refl[2] == "specular":
                # a delayed, quieter copy of the direct sound; timing depends on the geometry of this pair
                t = t_ms + rng.uniform(-0.7, 0.7)
                hf_loss = 1.0 / np.sqrt(1.0 + (f / 8000.0) ** 2)
                early += amp * hf_loss * np.exp(-2j * np.pi * f * (r / C + t / 1000)) / r
            else:  # diffuse tail: decaying, decorrelated
                rip = rng.normal(size=knots.size) + 1j * rng.normal(size=knots.size)
                rp = np.interp(np.log(f), np.log(knots), rip.real) + 1j * np.interp(np.log(f), np.log(knots), rip.imag)
                reverb += amp * rp * np.exp(-2j * np.pi * f * (r / C + t_ms / 1000))
        hf = delay / r + early + self.diffuse_scale * reverb / (np.sqrt(2) * self.critical_distance)
        a, b = self.transition_hz
        x = np.clip((np.log2(f) - np.log2(a)) / (np.log2(b) - np.log2(a)), 0, 1)
        wgt = 0.5 - 0.5 * np.cos(np.pi * x)
        return (1 - wgt) * lf + wgt * hf


@dataclass
class Speaker:
    role: str
    pos: tuple[float, float, float]
    kind: str  # "main" | "sub"
    dsp_output: int
    model: list[list]  # [[type, f0, q(, gain_db)], ...] driver/box response
    latency_ms: float = 0.0
    sensitivity_db: float = 112.0  # SPL at 1 m for a 0 dBFS drive through unity DSP

    def response(self, f: np.ndarray, fs: float) -> np.ndarray:
        h = cascade_response([design(m[0], min(m[1], fs * 0.45), fs, gain_db=m[3] if len(m) > 3 else 0.0, q=m[2])
                              for m in self.model], f, fs)
        return 10 ** (self.sensitivity_db / 20) * P_REF * h * np.exp(-2j * np.pi * f * self.latency_ms / 1000)


@dataclass
class DspOutput:
    gain_db: float = 0.0
    mute: bool = False
    delay_ms: float = 0.0
    invert: bool = False
    peq: dict[int, Biquad] = field(default_factory=dict)
    xover: list[Biquad] = field(default_factory=list)  # the user's own crossover, invisible to Rewdio
    routing: tuple[float, float] = (1.0, 0.0)  # weights of DSP inputs 0 (L) and 1 (R)

    def transfer(self, f: np.ndarray, fs: float) -> np.ndarray:
        h = np.full(f.shape, 10 ** (self.gain_db / 20), complex) * (-1 if self.invert else 1)
        h = h * np.exp(-2j * np.pi * f * self.delay_ms / 1000)
        return h * cascade_response(list(self.peq.values()) + self.xover, f, fs)


@dataclass
class World:
    room: Room
    speakers: list[Speaker]
    dsp: dict[int, DspOutput]
    mic: tuple[float, float, float]
    seats: dict[str, tuple[float, float, float]]
    fs: int = 96000
    master_volume_db: float = 0.0
    noise_floor_spl: float = 30.0
    clip_spl: float = 118.0
    seed: int = 0

    def drive(self, rew_output: str | None) -> tuple[float, float]:
        c = (rew_output or "").strip().lower()
        if c in ("l", "left", "1"):
            return (1.0, 0.0)
        if c in ("r", "right", "2"):
            return (0.0, 1.0)
        return (1.0, 1.0)

    def response(self, f: np.ndarray, level_dbfs: float, rew_output: str | None = None, noise: bool = True) -> np.ndarray:
        f = np.asarray(f, float)
        drv = self.drive(rew_output)
        total = np.zeros(f.shape, complex)
        for spk in self.speakers:
            out = self.dsp[spk.dsp_output]
            mix = out.routing[0] * drv[0] + out.routing[1] * drv[1]
            if out.mute or mix == 0:
                continue
            total += mix * out.transfer(f, self.fs) * spk.response(f, self.fs) * self.room.transfer(spk.pos, self.mic, f)
        total *= 10 ** ((level_dbfs + self.master_volume_db) / 20)
        if noise:
            self.seed += 1
            rng = np.random.default_rng(self.seed)
            n = rng.normal(size=f.size) + 1j * rng.normal(size=f.size)
            total = total + n * P_REF * 10 ** (self.noise_floor_spl / 20) / np.sqrt(2)
        return total

    def speaker(self, role: str) -> Speaker:
        for s in self.speakers:
            if s.role == role:
                return s
        raise KeyError(role)


def load_world(path: str | Path | None = None) -> World:
    """Build a World from a scenario JSON (default: the packaged scenarios/default.json)."""
    if path is None:
        raw = json.loads(resources.files("rewdio.sim").joinpath("scenarios/default.json").read_text())
    else:
        raw = json.loads(Path(path).expanduser().read_text())
    fs = raw["fs"]
    r = raw["room"]
    room = Room(tuple(r["dims"]), r["rt60"], r["f_max_mode"], tuple(r["transition_hz"]), r["diffuse_scale"],
                [tuple(x) for x in r["reflections"]])
    models = raw["speaker_models"]
    speakers = [Speaker(s["role"], tuple(s["pos"]), s["kind"], s["dsp_output"], s.get("model", models[s["kind"]]),
                        s.get("latency_ms", 0.0), s.get("sensitivity_db", 112.0)) for s in raw["speakers"]]
    dsp = {}
    for d in raw["dsp"]:
        xo = linkwitz_riley4(d["xover"][0], d["xover"][1], fs) if d.get("xover") else []
        dsp[d["index"]] = DspOutput(gain_db=d.get("gain_db", 0.0), delay_ms=d.get("delay_ms", 0.0),
                                    invert=d.get("invert", False), routing=tuple(d["routing"]), xover=xo)
    seats = {k: tuple(v) for k, v in raw["seats"].items()}
    mic = seats[raw["mic"]] if isinstance(raw["mic"], str) else tuple(raw["mic"])
    return World(room, speakers, dsp, mic, seats, fs, noise_floor_spl=raw["noise_floor_spl"], clip_spl=raw["clip_spl"])


def default_world(fs: int | None = None) -> World:
    return load_world()
