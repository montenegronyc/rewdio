"""Timing and integration solvers.

All functions take responses on the same log grid. "Delay b by d ms" means
multiply b by exp(-j*2*pi*f*d/1000); negative d means delay a instead.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.ndimage import uniform_filter1d

from .response import Response, band


def _phasors(freqs: np.ndarray, delays_ms: np.ndarray) -> np.ndarray:
    return np.exp(-2j * np.pi * np.outer(delays_ms, freqs) / 1000.0)


def _refine_grid(center: float, step: float, n: int = 21) -> np.ndarray:
    return center + np.linspace(-step, step, n)


@dataclass
class DelayFit:
    delay_ms: float
    residual_db: float
    runner_up_residual_db: float

    @property
    def confident(self) -> bool:
        # A clear winner: best fit is tight and visibly better than the next distinct minimum.
        return self.residual_db < 1.5 and self.runner_up_residual_db - self.residual_db > 0.5


def fit_relative_delay(ref: Response, x: Response, combined: list[tuple[np.ndarray, float]], lo: float, hi: float,
                       max_ms: float = 100.0) -> DelayFit:
    """Sum-fit: find d so that |ref + x*e^{-jw(d+e_i)}| matches each measured combined sweep i.

    REW without a timing reference puts each impulse peak at t=0, so ref and x sit on
    unknown, different time bases. A combined sweep's magnitude does not depend on its own
    time base, only on the relative delay, so it pins d down. One sweep only fixes the
    relative phase up to its sign (a mirror solution exists), so pass two sweeps where the
    second has a known extra DSP delay e_i on x: the true d stays put, the mirror moves.

    combined: list of (|ref + x| in dB as measured, extra delay in ms applied to x for that sweep).
    """
    m = band(ref.freqs, lo, hi)
    f = ref.freqs[m]
    a, b = ref.h[m], x.h[m]
    w = np.minimum(np.abs(a), np.abs(b)) / np.maximum(np.maximum(np.abs(a), np.abs(b)), 1e-20)
    w = w / max(w.sum(), 1e-12)

    def residuals(delays: np.ndarray) -> np.ndarray:
        total = np.zeros(delays.size)
        for comb_db, extra in combined:
            model = np.abs(a[None, :] + b[None, :] * _phasors(f, delays + extra))
            err = 20 * np.log10(np.maximum(model, 1e-20)) - np.asarray(comb_db)[m][None, :]
            total += np.sum(w[None, :] * err ** 2, axis=1)
        return np.sqrt(total / len(combined))

    coarse = np.arange(-max_ms, max_ms + 1e-9, 0.05)
    r = residuals(coarse)
    best = int(np.argmin(r))
    fine = _refine_grid(coarse[best], 0.05, 41)
    rf = residuals(fine)
    d = float(fine[int(np.argmin(rf))])
    # runner-up: best residual at least a quarter period (at the band centre) away
    period_ms = 1000.0 / np.sqrt(lo * hi)
    far = np.abs(coarse - d) > period_ms / 4
    runner = float(r[far].min()) if far.any() else float("inf")
    return DelayFit(delay_ms=d, residual_db=float(rf.min()), runner_up_residual_db=runner)


@dataclass
class Alignment:
    delay_ms: float  # delay applied to b relative to a (negative: delay a)
    invert: bool
    efficiency_db: float


def _efficiency_matrix(a: np.ndarray, b: np.ndarray, ph: np.ndarray) -> np.ndarray:
    """Weighted summation efficiency for each row of phasors ph (n_delays x n_freqs)."""
    aa, bb = np.abs(a), np.abs(b)
    w = np.minimum(aa, bb) / np.maximum(np.maximum(aa, bb), 1e-20)
    s = np.abs(a[None, :] + b[None, :] * ph)
    eff = 20 * np.log10(np.maximum(s, 1e-20) / np.maximum(aa + bb, 1e-20)[None, :])
    return (eff @ w) / max(w.sum(), 1e-12)


def best_alignment(a_list: list[Response], b_list: list[Response], lo: float, hi: float,
                   delay_range: tuple[float, float], allow_invert: bool = True,
                   feasible=None) -> Alignment:
    """Delay/polarity for b that makes a+b sum most coherently in [lo, hi], averaged over positions.

    Among exact near-ties (within 0.003 dB), the smallest |delay| wins so we never pick a solution
    one full period away. `feasible(delay_ms) -> bool` can veto delays the DSP cannot do.
    """
    f = a_list[0].freqs
    m = band(f, lo, hi)
    delays = np.arange(delay_range[0], delay_range[1] + 1e-9, 0.02)
    if feasible is not None:
        delays = np.array([d for d in delays if feasible(d)])
        if delays.size == 0:
            raise ValueError("no feasible delay in range")
    ph = _phasors(f[m], delays)
    best: Alignment | None = None
    for inv in ([False, True] if allow_invert else [False]):
        sign = -1.0 if inv else 1.0
        score = np.mean([_efficiency_matrix(a.h[m], sign * b.h[m], ph) for a, b in zip(a_list, b_list)], axis=0)
        top = score.max()
        near = np.where(score >= top - 0.003)[0]
        i = near[np.argmin(np.abs(delays[near]))]
        cand = Alignment(float(delays[i]), inv, float(score[i]))
        if best is None or cand.efficiency_db > best.efficiency_db + 0.1 or (
                abs(cand.efficiency_db - best.efficiency_db) <= 0.1 and abs(cand.delay_ms) < abs(best.delay_ms)
                and not cand.invert):
            best = cand
    assert best is not None
    return best


@dataclass
class PairSolution:
    delay_ms: float  # delay of b relative to a
    gain_db: float  # gain of b relative to a
    invert: bool
    score: float  # lower is better: in-band std (dB) + seat-to-seat spread penalty
    std_db: float
    spread_db: float


def _smooth_rows(p: np.ndarray, width: int) -> np.ndarray:
    return uniform_filter1d(p, size=max(1, width), axis=-1, mode="nearest")


def optimize_pair(a_list: list[Response], b_list: list[Response], target_db: np.ndarray,
                  lo: float, hi: float, max_delay_ms: float, gain_range_db: float = 6.0,
                  allow_invert: bool = True, smoothing: float = 6.0, spread_weight: float = 0.5,
                  loss_weight: float = 0.3, coherent: bool = True) -> PairSolution:
    """Find delay/gain/polarity of b vs a giving the flattest a+b across positions.

    Used for dual-sub integration and for scoring candidate positions in the reciprocity crawl.
    With coherent=False, sums power (magnitude-only data): delay and polarity are then moot.
    """
    f = a_list[0].freqs
    m = band(f, lo / 1.3, hi * 1.3)  # margin so smoothing is valid at the band edges
    core = band(f[m], lo, hi)
    core_full = np.where(core)[0]
    fm = f[m]
    ppo = 1.0 / np.log2(f[1] / f[0])
    width = int(round(ppo / smoothing))
    tgt = np.asarray(target_db)[m][core]
    A = np.array([r.h[m] for r in a_list])  # positions x freqs
    B = np.array([r.h[m] for r in b_list])

    def evaluate(delays, gains, inverts):
        # combos x positions x freqs
        if coherent:
            ph = _phasors(fm, delays)  # combos x freqs
            g = 10 ** (gains / 20) * np.where(inverts, -1.0, 1.0)
            s = A[None, :, :] + (g[:, None, None] * B[None, :, :]) * ph[:, None, :]
            p = np.abs(s) ** 2
            coh = (np.abs(A)[None] + np.abs(g)[:, None, None] * np.abs(B)[None]) ** 2
            loss = -10 * np.log10(np.maximum(p[:, :, core_full], 1e-30) / np.maximum(coh[:, :, core_full], 1e-30))
            loss = loss.mean(axis=(1, 2))
        else:
            g2 = 10 ** (gains / 10)
            p = np.abs(A[None]) ** 2 + g2[:, None, None] * np.abs(B[None]) ** 2
            loss = np.zeros(p.shape[0])
        db = 10 * np.log10(np.maximum(_smooth_rows(p, width), 1e-24))[:, :, core]
        err = db - tgt[None, None, :]
        err = err - err.mean(axis=2, keepdims=True)
        std = np.sqrt(np.mean(err ** 2, axis=2)).mean(axis=1)  # mean over positions
        if A.shape[0] > 1:
            lvl = db - db.mean(axis=2, keepdims=True)
            spread = lvl.std(axis=1).mean(axis=1)
        else:
            spread = np.zeros_like(std)
        return std + spread_weight * spread + loss_weight * loss, std, spread

    def grid(dc, dstep, dn, gc, gstep, gn):
        ds = dc + dstep * np.arange(-dn, dn + 1)
        ds = ds[np.abs(ds) <= max_delay_ms + 1e-9]
        gs = np.clip(gc + gstep * np.arange(-gn, gn + 1), -gain_range_db, gain_range_db)
        invs = [False, True] if (allow_invert and coherent) else [False]
        D, G, I = np.meshgrid(ds if coherent else [0.0], np.unique(gs), invs, indexing="ij")
        return D.ravel(), G.ravel(), I.ravel()

    best = None
    # coarse then fine; evaluate in chunks to bound memory
    for dc, dstep, dn, gc, gstep, gn in [(0.0, 0.25, int(max_delay_ms / 0.25), 0.0, 1.0, int(gain_range_db))]:
        D, G, I = grid(dc, dstep, dn, gc, gstep, gn)
        best = _best_of(evaluate, D, G, I)
    for _ in range(2):
        D, G, I = grid(best[0], 0.05, 6, best[1], 0.25, 4)
        best = _best_of(evaluate, D, G, I, prefer=best)
    d, g, inv, score, std, spread, _ = best
    return PairSolution(float(d), float(g), bool(inv), float(score), float(std), float(spread))


def _best_of(evaluate, D, G, I, prefer=None, chunk: int = 512):
    out = None
    for s in range(0, D.size, chunk):
        sc, st, sp = evaluate(D[s:s + chunk], G[s:s + chunk], I[s:s + chunk])
        # prefer small delays and gains among near-ties
        tie = sc + 0.02 * np.abs(D[s:s + chunk]) + 0.005 * np.abs(G[s:s + chunk])
        k = int(np.argmin(tie))
        cand = (D[s + k], G[s + k], I[s + k], sc[k], st[k], sp[k], tie[k])
        if out is None or cand[6] < out[6]:
            out = cand
    if prefer is not None and len(prefer) == 7 and prefer[6] <= out[6]:
        return prefer
    return out


def arrival_ms(resp: Response, f_hi: float = 6000.0, threshold: float = 0.5) -> float:
    """Direct-sound arrival: first point where the impulse envelope reaches `threshold` of its peak.

    The IR is rebuilt from the log-spaced response up to f_hi (phase must unwrap cleanly there).
    """
    from scipy.signal import hilbert

    df = 2.0
    fl = np.arange(0.0, f_hi, df)
    lf = np.log(resp.freqs)
    ok = (fl >= resp.freqs[0]) & (fl <= resp.freqs[-1])
    mag = np.zeros_like(fl)
    ph = np.zeros_like(fl)
    mag[ok] = np.interp(np.log(fl[ok]), lf, np.abs(resp.h))
    ph[ok] = np.interp(np.log(fl[ok]), lf, np.unwrap(np.angle(resp.h)))
    taper = np.clip((fl - 200.0) / 300.0, 0, 1) * np.clip((f_hi - fl) / (0.2 * f_hi), 0, 1)  # high-pass: direct sound
    spec = mag * taper * np.exp(1j * ph)
    n = 2 * (fl.size - 1) + 2
    ir = np.fft.irfft(spec, n=n)
    fs = df * n
    env = np.abs(hilbert(ir))
    peak = env.max()
    i0 = int(np.argmax(env >= threshold * peak))
    t = i0 / fs
    if t > n / fs / 2:
        t -= n / fs
    return float(t * 1000.0)
