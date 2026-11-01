"""Day-one hardware self-test: prove on the real rig what the simulator can only assume.

Run it at the start of the first session on new hardware, before any capture set. Each check plays
quietly through the protection filters and either passes, fails with the reason, or is skipped:

1. minidsp-rs sees the device, and it has enough outputs for the configured channels.
2. REW: version, and the timing reference matches the config (loopback for absolute phase).
3. REW reports `startTime` on the impulse response, and the first arrival is plausible for the mic distance.
4. A crossover write lands: adding a known high-pass changes the measured response the way the shadow
   state predicts (within 1 dB / 10 degrees where the driver plays).
5. A delay write lands: adding 1.000 ms moves the measured response by 1.000 +- 0.02 ms.
6. Optional (needs the impedance jig wired): REW runs an impedance sweep when asked over its API.

The report is stored in the session as selftest.json, so it travels with the work.
"""

from __future__ import annotations

import json
import time

import numpy as np

from ..analysis.frd import arrival_s
from ..config import TimingMode
from ..minidsp.state import XoverFilter, change_transfer
from ..rew.client import RewError
from .rig import Rig

DELAY_STEP_MS = 1.0
DELAY_TOL_MS = 0.02
XO_TOL_DB, XO_TOL_DEG = 1.0, 10.0
C_SOUND = 343.0


def _check(name: str, ok: bool | None, detail: str, **data) -> dict:
    return {"check": name, "result": "pass" if ok else "skipped" if ok is None else "FAIL", "detail": detail, **data}


def _band(resp, floor_db: float = 20.0) -> np.ndarray:
    """Where this driver actually plays: within `floor_db` of its loudest point."""
    db = resp.mag_db
    return db > db.max() - floor_db


def _smooth_complex(x: np.ndarray, width: float) -> np.ndarray:
    from scipy.ndimage import uniform_filter1d
    k = max(1, int(round(width)))
    return uniform_filter1d(x.real, k, mode="nearest") + 1j * uniform_filter1d(x.imag, k, mode="nearest")


def phase_delay_ms(ratio: np.ndarray, freqs: np.ndarray) -> float:
    """Pure delay that best explains a complex ratio: the slope of its unwrapped phase."""
    ph = np.unwrap(np.angle(ratio))
    slope = np.polyfit(freqs, ph, 1)[0]
    return float(-slope / (2 * np.pi) * 1000)


async def run(rig: Rig, driver: str, distance_m: float | None = None, impedance: bool = False) -> dict:
    """Run every check with `driver` as the test output. Never leaves the output changed."""
    cfg, s = rig.cfg, rig.s
    ch = cfg.channel(driver)
    checks: list[dict] = []

    # 1. the DSP
    try:
        devs = await rig.dsp.devices()
        dev = devs[cfg.minidsp_device_index] if len(devs) > cfg.minidsp_device_index else None
        st = await rig.dsp.status()
        n_out = len(st.get("output_levels") or [])
        need = max(c.dsp_output for c in cfg.channels) + 1
        name = (dev or {}).get("product_name", "?")
        checks.append(_check("minidsp_device", dev is not None and n_out >= need,
                             f"{name}: {n_out} outputs, config needs {need}", product=name, outputs=n_out))
    except Exception as e:  # noqa: BLE001 - a self-test reports every failure rather than stopping
        checks.append(_check("minidsp_device", False, f"minidsp-rs not reachable or no device: {e}"))
        return _finish(s, checks)

    # 2. REW
    try:
        rep = await rig.prepare_rew()
        absolute = cfg.timing_mode in (TimingMode.LOOPBACK, TimingMode.ACOUSTIC)
        detail = f"REW {rep['rew_version']}, timing reference {rep['timing_reference']!r}"
        if not absolute:
            detail += (f"; config timing_mode is {cfg.timing_mode.value!r}: speaker captures need absolute phase, "
                       "so set timing_mode to 'loopback' and wire the loopback; phase checks below are skipped")
        checks.append(_check("rew_timing", absolute, detail))
    except Exception as e:  # noqa: BLE001
        checks.append(_check("rew_timing", False, f"REW not ready: {e}"))
        return _finish(s, checks)

    base = rig.s.dsp.get(driver).model_copy(deep=True)
    try:
        if ch.crossover is not None:
            await rig.set_crossover(driver, raw=True)
        rec_a, resp_a = await rig.measure([driver], "selftest", "selftest", title=f"{driver} selftest A")

        # 3. startTime and the arrival
        raw = await rig.rew.get(f"/measurements/{rec_a.id}/impulse-response", windowed="false", normalised="false")
        has_t0 = isinstance(raw, dict) and raw.get("startTime") is not None
        ir, fs, t0 = await rig.rew.impulse_response_timed(rec_a.id)
        arr_ms = arrival_s(ir, fs, t0) * 1000
        detail = f"startTime {'present' if has_t0 else 'MISSING'}; first arrival {arr_ms:.2f} ms"
        ok = has_t0
        if distance_m:
            extra = arr_ms - distance_m / C_SOUND * 1000
            detail += f", {extra:+.2f} ms beyond {distance_m:g} m of air (DSP and converter latency)"
            ok = ok and -0.2 < extra < 20
        checks.append(_check("rew_start_time", ok, detail, arrival_ms=round(arr_ms, 3)))

        m = _band(resp_a)
        if m.sum() < 8:
            checks.append(_check("signal", False, f"{driver} barely plays (band too narrow): check level and wiring"))
            return _finish(s, checks)

        # 4. a crossover write lands
        if ch.crossover is not None:
            lo = float(resp_a.freqs[m][0])
            probe = XoverFilter(kind="HP", topology="BW2", freq_hz=float(np.clip(lo * 3, 100, 2000)))
            before = rig.s.dsp.get(driver).model_copy(deep=True)
            await rig.set_crossover(driver, filters=[*(before.crossover or []), probe])
            after = rig.s.dsp.get(driver).model_copy(deep=True)
            _, resp_b = await rig.measure([driver], "selftest", "selftest", title=f"{driver} selftest B")
            pred = change_transfer(before, after, resp_a.freqs, cfg.dsp_sample_rate)
            # smoothed over 1/6 octave and only where the driver is strong: room dips are noise, not the DSP
            err = _smooth_complex((resp_b.h / resp_a.h) / pred, resp_a.ppo / 6)
            mm = _band(resp_a, 10.0) & (np.abs(pred) > 10 ** (-12 / 20))
            db = float(np.max(np.abs(20 * np.log10(np.abs(err[mm])))))
            deg = float(np.max(np.abs(np.degrees(np.angle(err[mm]))))) if absolute else 0.0
            checks.append(_check("crossover_write", db <= XO_TOL_DB and deg <= XO_TOL_DEG,
                                 f"added {probe.topology} HP {probe.freq_hz:.0f} Hz: measured vs predicted worst "
                                 f"{db:.2f} dB" + (f", {deg:.1f} deg" if absolute else " (magnitude only)")
                                 + f" (limit {XO_TOL_DB:g} dB, {XO_TOL_DEG:g} deg)"))
            await rig.set_crossover(driver, raw=True)
        else:
            checks.append(_check("crossover_write", None, f"{driver} has no `crossover` in the config"))

        # 5. a delay write lands (only visible on an absolute time base)
        if not absolute:
            checks.append(_check("delay_write", None, "needs the loopback timing reference (see rew_timing)"))
        else:
            await _delay_check(rig, driver, resp_a, m, checks)
        # 6. impedance over the API
        if impedance:
            try:
                await rig.solo([driver])
                summary = await rig.rew.sweep(f"{driver} selftest impedance", notes="rewdio selftest",
                                              command="Impedance")
                await rig.rew.impedance(summary["uuid"])
                checks.append(_check("rew_impedance_api", True, "REW ran an impedance sweep over its API"))
            except RewError as e:
                checks.append(_check("rew_impedance_api", False, f"REW refused ({e}); use its GUI and import "
                                                                  "the measurement by id with speaker_impedance"))
        else:
            checks.append(_check("rew_impedance_api", None, "not run (pass impedance=true with the jig wired)"))
    except Exception as e:  # noqa: BLE001
        checks.append(_check("aborted", False, f"{type(e).__name__}: {e}"))
    finally:  # put the output back exactly as it was
        o = rig.s.dsp.get(driver)
        if o.delay_ms != base.delay_ms:
            await rig.set_channel(driver, delay_ms=base.delay_ms)
        if ch.crossover is not None and o.crossover != base.crossover:
            await rig.set_crossover(driver, filters=base.crossover)
    return _finish(s, checks)


async def _delay_check(rig: Rig, driver: str, resp_a, m: np.ndarray, checks: list[dict]) -> None:
    await rig.nudge_delay(driver, DELAY_STEP_MS)
    _, resp_c = await rig.measure([driver], "selftest", "selftest", title=f"{driver} selftest C")
    got = phase_delay_ms(resp_c.h[m] / resp_a.h[m], resp_a.freqs[m])
    checks.append(_check("delay_write", abs(got - DELAY_STEP_MS) <= DELAY_TOL_MS,
                         f"asked +{DELAY_STEP_MS:.3f} ms, measured {got:+.3f} ms (limit +-{DELAY_TOL_MS} ms)"))


def _finish(s, checks: list[dict]) -> dict:
    failed = [c["check"] for c in checks if c["result"] == "FAIL"]
    rep = {"ok": not failed, "failed": failed, "checks": checks,
           "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    (s.dir / "selftest.json").write_text(json.dumps(rep, indent=1))
    return rep
