"""Async client for the REW REST API (REW 5.40+, default port 4735).

Payload shapes follow REW's OpenAPI document (/doc.json) and the field notes of existing REW
MCP servers (brandon-fryslie/room-eq-wizard-mcp, daredoole/audio-calibration-mcp,
KevinMeinon/rew-mcp-server, koltyj/REW-mcp; all MIT). Gotchas handled here:
- /measure/command answers 202 at once even in blocking mode: completion = a new UUID appearing.
- REW refuses a sweep that follows another too closely ("measurement in progress"): settle after.
- Settings writes are read back and checked; REW sometimes answers OK and ignores a field.
- A wedged command is cleared with "Clear command in progress" before giving up.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
import numpy as np

from ..config import RewClientSettings
from .codec import decode_floats, parse_rew_json

SWEEP_SAMPLES = {"128k": 131072, "256k": 262144, "512k": 524288, "1M": 1048576, "2M": 2097152, "4M": 4194304}
SMOOTHING = ("1/1", "1/2", "1/3", "1/6", "1/12", "1/24", "1/48", "Var", "Psy", "ERB", "None")


class RewError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, valid_values: list[str] | None = None):
        super().__init__(message)
        self.status = status
        self.valid_values = valid_values or []


@dataclass
class Spectrum:
    freqs: np.ndarray
    mag_db: np.ndarray
    phase_deg: np.ndarray | None


class RewClient:
    def __init__(self, base_url: str, settings: RewClientSettings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.s = settings
        self._http = httpx.AsyncClient(base_url=self.base_url, transport=transport, timeout=settings.read_timeout_s)
        self._blocking = False
        self._lock = asyncio.Lock()  # one sweep at a time per REW instance
        self.sweep_seconds = 6.0  # updated by configure_sweep

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- plumbing -----------------------------------------------------------------

    async def _request(self, method: str, path: str, *, json_body: Any = None, params: dict | None = None,
                       timeout: float | None = None) -> Any:
        kwargs: dict[str, Any] = {"params": {k: v for k, v in (params or {}).items() if v is not None}}
        if json_body is not None:
            kwargs["json"] = json_body
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            resp = await self._http.request(method, path, **kwargs)
        except httpx.TransportError as e:
            raise RewError(f"REW API unreachable at {self.base_url} ({e}). Start REW with -api "
                           "(macOS: open -a REW.app --args -api) or enable it in Preferences > API.") from e
        text = resp.text
        body: Any = None
        if text:
            try:
                body = parse_rew_json(text)
            except ValueError:
                body = text
        if resp.status_code >= 400:
            msg = body.get("message") if isinstance(body, dict) else text
            valid = body.get("validValues") if isinstance(body, dict) else None
            hint = ""
            if path.startswith("/measure") and (resp.status_code in (401, 403) or "pro" in str(msg).lower()):
                hint = " (API-triggered measurement needs a REW Pro upgrade)"
            extra = f" valid values: {valid}" if valid else ""
            raise RewError(f"REW {method} {path} -> {resp.status_code}: {msg}{hint}{extra}", resp.status_code, valid)
        return body

    async def get(self, path: str, **params: Any) -> Any:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, body: Any = None) -> Any:
        return await self._request("POST", path, json_body=body)

    async def put(self, path: str, body: Any = None) -> Any:
        return await self._request("PUT", path, json_body=body)

    async def delete(self, path: str) -> Any:
        return await self._request("DELETE", path)

    async def ensure_blocking(self) -> None:
        if not self._blocking:
            await self.post("/application/blocking", True)
            self._blocking = True

    async def diagnostics(self) -> dict:
        """REW's own account of what went wrong: last error/warning plus recent lists."""
        out = {}
        for key, path in (("last_error", "/application/last-error"), ("last_warning", "/application/last-warning"),
                          ("errors", "/application/errors"), ("warnings", "/application/warnings")):
            try:
                out[key] = await self.get(path)
            except RewError as e:
                out[key] = f"unavailable ({e.status})"
        return out

    async def clear_command(self) -> None:
        """Recover from a wedged command ('There is already a measurement in progress')."""
        for path, body in (("/application/command", {"command": "Clear command in progress"}),
                           ("/measure/command", {"command": "Cancel"})):
            try:
                await self.post(path, body)
            except RewError:
                pass

    # -- application / audio ------------------------------------------------------

    async def version(self) -> str:
        v = await self.get("/version")
        return v.get("message", str(v)) if isinstance(v, dict) else str(v)

    async def audio_driver(self) -> str | None:
        d = await self.get("/audio/driver")
        return d.get("driver") if isinstance(d, dict) else d

    async def _driver_prefix(self) -> str:
        return "/audio/asio" if "asio" in (await self.audio_driver() or "").lower() else "/audio/java"

    async def set_output_channel(self, channel: str) -> None:
        prefix = await self._driver_prefix()
        if prefix.endswith("asio"):
            await self.post(f"{prefix}/output", {"output": channel})
        else:
            await self.post(f"{prefix}/output-channel", {"channel": channel})

    async def input_cal(self) -> dict:
        return await self.get("/audio/input-cal") or {}

    async def set_input_cal(self, cal_file_path: str, sensitivity_dbfs_at_94: float | None = None) -> dict:
        cfg = await self.input_cal()
        cal = dict(cfg.get("calDataAllInputs") or {})
        # REW derives sensitivity for USB mics and rejects echoing it back; only send what we change.
        cal.pop("dBFSAt94dBSPL", None)
        cal.pop("fullScaleSineVrms", None)
        cal["calFilePath"] = cal_file_path
        if sensitivity_dbfs_at_94 is not None:
            cal["dBFSAt94dBSPL"] = sensitivity_dbfs_at_94
        await self.put("/audio/input-cal", {**cfg, "calDataAllInputs": cal})
        got = await self.input_cal()
        if (got.get("calDataAllInputs") or {}).get("calFilePath") != cal_file_path:
            raise RewError(f"REW did not keep the mic cal file {cal_file_path!r}; check the path exists on REW's machine")
        return got

    # -- measurement settings (all read back) --------------------------------------

    async def configure_sweep(self, start_hz: int, end_hz: int, length: str, start_delay_s: float, sample_rate: int,
                              fill_silence_with_dither: bool) -> None:
        await self.post("/measure/sweep/configuration",
                        {"startFrequency": int(start_hz), "endFrequency": int(end_hz), "length": length})
        await self.post("/measure/start-delay", float(start_delay_s))
        await self.post("/measure/fill-silence-with-dither", bool(fill_silence_with_dither))
        got = await self.get("/measure/fill-silence-with-dither")
        got = got.get("value", got) if isinstance(got, dict) else got
        if bool(got) != bool(fill_silence_with_dither):
            raise RewError(f"REW did not keep fill-silence-with-dither={fill_silence_with_dither} (reads {got})")
        self.sweep_seconds = SWEEP_SAMPLES.get(length, SWEEP_SAMPLES["1M"]) / sample_rate + start_delay_s

    async def set_level(self, dbfs: float) -> None:
        await self.post("/measure/level", {"value": float(dbfs), "unit": "dBFS"})

    async def timing_reference(self) -> str:
        r = await self.get("/measure/timing/reference")
        return r if isinstance(r, str) else str(r)

    async def timing_reference_choices(self) -> list[str]:
        r = await self.get("/measure/timing/reference/choices")
        return list(r) if isinstance(r, list) else []

    async def set_timing_reference(self, name: str) -> None:
        await self.post("/measure/timing/reference", name)
        got = await self.timing_reference()
        if got != name:
            raise RewError(f"REW timing reference is {got!r} after setting {name!r}")

    async def set_protection(self, max_spl_db: float) -> dict:
        current = await self.get("/measure/protection-options") or {}
        want = {"clippingAbort": True, "splLimitAbort": True, "dBSPLLimit": int(round(max_spl_db)),  # int32 in REW
                "warnForLowLevels": False}  # a modal warning would stall an unattended run
        await self.post("/measure/protection-options", {**current, **want})
        got = await self.get("/measure/protection-options") or {}
        bad = [k for k in ("clippingAbort", "splLimitAbort", "dBSPLLimit") if got.get(k) != want[k]]
        if bad:
            raise RewError(f"REW did not keep protection settings {bad}: {got}")
        return got

    async def set_name(self, title: str, notes: str | None = None) -> None:
        await self.post("/measure/naming", {"title": title, "namingOption": "Use as entered"})
        if notes is not None:
            await self.post("/measure/notes", notes)

    # -- measurements -------------------------------------------------------------

    async def list_measurements(self) -> list[dict]:
        raw = await self.get("/measurements") or {}
        if isinstance(raw, list):
            return raw
        return sorted(({**v, "index": int(k)} for k, v in raw.items()), key=lambda m: m["index"])

    async def measurement(self, ident: str) -> dict:
        return await self.get(f"/measurements/{ident}")

    async def annotate(self, ident: str, title: str, provenance: dict) -> None:
        """Write provenance JSON into the measurement's notes so a saved .mdat explains itself."""
        try:
            await self.put(f"/measurements/{ident}", {"title": title, "notes": json.dumps(provenance)})
        except RewError:
            pass  # cosmetic; never fail a calibration over it

    async def sweep(self, title: str, notes: str | None = None, command: str = "SPL") -> dict:
        """Run one sweep (`command` "SPL", or "Impedance" with an impedance jig) and return the new
        measurement's summary."""
        async with self._lock:
            await self.ensure_blocking()
            await self.set_name(title, notes)
            before = {m["uuid"] for m in await self.list_measurements()}
            budget = self.sweep_seconds + self.s.timeout_margin_s
            deadline = time.monotonic() + budget
            try:
                await self._request("POST", "/measure/command", json_body={"command": command}, timeout=budget)
            except RewError as e:
                if "in progress" not in str(e).lower():
                    raise
                await self.clear_command()
                await asyncio.sleep(self.s.settle_s)
                await self._request("POST", "/measure/command", json_body={"command": command}, timeout=budget)
            while True:
                new = [m for m in await self.list_measurements() if m["uuid"] not in before]
                if new:
                    await asyncio.sleep(self.s.settle_s)
                    return new[-1]
                if time.monotonic() > deadline:
                    raise RewError(f"sweep produced no measurement within {budget:.0f}s; REW says {await self.diagnostics()}")
                await asyncio.sleep(self.s.poll_interval_s)

    async def frequency_response(self, ident: str, ppo: int, smoothing: str | None = None) -> Spectrum:
        if smoothing is not None and smoothing not in SMOOTHING:
            raise ValueError(f"smoothing must be one of {SMOOTHING} (REW rejects long names)")
        r = await self.get(f"/measurements/{ident}/frequency-response", ppo=ppo, smoothing=smoothing)
        return self._spectrum(r)

    @staticmethod
    def _spectrum(r: dict) -> Spectrum:
        mag = decode_floats(r["magnitude"])
        start = float(r.get("startFreq", r.get("startFrequency")))
        step_ppo = r.get("ppo") or r.get("pointsPerOctave")
        if step_ppo:
            freqs = start * 2.0 ** (np.arange(mag.size) / float(step_ppo))
        elif r.get("freqStep"):
            freqs = start + np.arange(mag.size) * float(r["freqStep"])
        else:
            raise RewError("frequency response has neither ppo nor freqStep")
        phase = decode_floats(r["phase"]) if r.get("phase") else None
        return Spectrum(freqs=freqs, mag_db=mag, phase_deg=phase)

    async def impedance(self, ident: str, ppo: int = 48) -> Spectrum:
        """An impedance measurement's curve: `mag_db` holds OHMS here (not dB), `phase_deg` degrees.
        Refuses a measurement that is not in ohms, so an SPL sweep can never be exported as a ZMA."""
        r = await self.get(f"/measurements/{ident}/frequency-response", ppo=ppo)
        unit = str(r.get("unit", ""))
        if "ohm" not in unit.lower() and "\u03a9" not in unit:
            raise RewError(f"measurement {ident} is not an impedance measurement (unit {unit!r})")
        return self._spectrum(r)

    async def distortion(self, ident: str, ppo: int = 12, unit: str = "percent") -> tuple[list[str], list[list]]:
        """Harmonic distortion of a sweep: REW's column headers (e.g. 'Freq (Hz)', 'Fundamental (dB)',
        'THD (%)', 'Noise (%)', 'H2 (%)'...) and its rows, which may be ragged (missing high harmonics)."""
        r = await self.get(f"/measurements/{ident}/distortion", unit=unit, ppo=ppo)
        if not isinstance(r, dict) or not r.get("columnHeaders"):
            raise RewError(f"REW returned no distortion data for {ident}: {r}")
        return list(r["columnHeaders"]), list(r.get("data") or [])

    async def impulse_response(self, ident: str) -> tuple[np.ndarray, float]:
        """Raw (unwindowed, unnormalised) impulse response and its sample rate."""
        ir, fs, _ = await self.impulse_response_timed(ident)
        return ir, fs

    async def impulse_response_timed(self, ident: str) -> tuple[np.ndarray, float, float]:
        """Raw impulse response, sample rate, and the time of its first sample in seconds relative to the
        timing reference (REW's `startTime`). With a loopback reference that time base is absolute."""
        r = await self.get(f"/measurements/{ident}/impulse-response", windowed="false", normalised="false")
        if not isinstance(r, dict) or "data" not in r:
            raise RewError(f"REW returned no impulse response for {ident}: {r}")
        fs = float(r.get("sampleRate") or 1.0 / float(r["sampleInterval"]))
        return decode_floats(r["data"]), fs, float(r.get("startTime") or 0.0)
