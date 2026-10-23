"""Async client for the REW REST API (REW 5.40+, default port 4735).

Payload shapes follow REW's own OpenAPI document (served at /doc.json) and were
cross-checked against field-tested open-source REW clients.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx
import numpy as np

from .codec import decode_floats, parse_rew_json


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
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:4735",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        read_timeout: float = 15.0,
        command_timeout: float = 180.0,
        poll_interval: float = 0.5,
    ):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self.base_url, transport=transport, timeout=read_timeout)
        self.command_timeout = command_timeout
        self.poll_interval = poll_interval
        self._blocking = False
        self._lock = asyncio.Lock()  # one sweep at a time per REW instance

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
            raise RewError(
                f"REW API unreachable at {self.base_url} ({e}). Start REW with -api "
                "(macOS: open -a REW.app --args -api) or enable it in Preferences > API."
            ) from e
        text = resp.text
        body = None
        if text:
            try:
                body = parse_rew_json(text)
            except ValueError:
                body = text
        if resp.status_code >= 400:
            msg = body.get("message") if isinstance(body, dict) else text
            valid = body.get("validValues") if isinstance(body, dict) else None
            hint = ""
            if resp.status_code == 403 or (isinstance(msg, str) and "pro" in msg.lower() and path.startswith("/measure")):
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

    # -- application / audio ------------------------------------------------------

    async def version(self) -> str:
        v = await self.get("/version")
        return v.get("message", str(v)) if isinstance(v, dict) else str(v)

    async def last_error(self) -> Any:
        return await self.get("/application/last-error")

    async def audio_driver(self) -> str | None:
        d = await self.get("/audio/driver")
        return d.get("driver") if isinstance(d, dict) else d

    async def set_output_channel(self, channel: str) -> None:
        """Select REW's measurement output channel on whichever driver is active."""
        driver = (await self.audio_driver() or "").lower()
        if "asio" in driver:
            await self.post("/audio/asio/output", {"output": channel})
        else:
            await self.post("/audio/java/output-channel", {"channel": channel})

    async def output_channel(self) -> str | None:
        driver = (await self.audio_driver() or "").lower()
        if "asio" in driver:
            r = await self.get("/audio/asio/output")
            return r.get("output") if isinstance(r, dict) else r
        r = await self.get("/audio/java/output-channel")
        return r.get("channel") if isinstance(r, dict) else r

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
        return await self.input_cal()

    async def samplerate(self) -> Any:
        return await self.get("/audio/samplerate")

    # -- measurement settings -----------------------------------------------------

    async def configure_sweep(self, start_hz: int, end_hz: int, length: str) -> None:
        await self.post("/measure/sweep/configuration",
                        {"startFrequency": int(start_hz), "endFrequency": int(end_hz), "length": length})

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

    async def set_protection(self, max_spl_db: float) -> None:
        current = await self.get("/measure/protection-options") or {}
        await self.post("/measure/protection-options",
                        {**current, "clippingAbort": True, "splLimitAbort": True, "dBSPLLimit": max_spl_db})

    async def set_name(self, title: str, notes: str | None = None) -> None:
        await self.post("/measure/naming", {"title": title, "namingOption": "Use as entered"})
        if notes is not None:
            await self.post("/measure/notes", notes)

    # -- measurements -------------------------------------------------------------

    async def list_measurements(self) -> list[dict]:
        raw = await self.get("/measurements") or {}
        if isinstance(raw, list):
            return raw
        out = []
        for idx, summary in raw.items():
            out.append({**summary, "index": int(idx)})
        return sorted(out, key=lambda m: m["index"])

    async def measurement(self, ident: str) -> dict:
        return await self.get(f"/measurements/{ident}")

    async def delete_measurement(self, ident: str) -> None:
        await self.delete(f"/measurements/{ident}")

    async def sweep(self, title: str, notes: str | None = None) -> dict:
        """Run one SPL sweep and return the new measurement's summary.

        /measure/command answers 202 immediately even in blocking mode, so completion
        is observed by polling for a new measurement UUID.
        """
        async with self._lock:
            await self.ensure_blocking()
            await self.set_name(title, notes)
            before = {m["uuid"] for m in await self.list_measurements()}
            deadline = time.monotonic() + self.command_timeout
            await self._request("POST", "/measure/command", json_body={"command": "SPL"}, timeout=self.command_timeout)
            while True:
                new = [m for m in await self.list_measurements() if m["uuid"] not in before]
                if new:
                    return new[-1]
                if time.monotonic() > deadline:
                    err = None
                    try:
                        err = await self.last_error()
                    except RewError:
                        pass
                    raise RewError(f"sweep produced no measurement within {self.command_timeout:.0f}s; REW last error: {err}")
                await asyncio.sleep(self.poll_interval)

    async def frequency_response(self, ident: str, ppo: int = 96, smoothing: str | None = None) -> Spectrum:
        r = await self.get(f"/measurements/{ident}/frequency-response", ppo=ppo, smoothing=smoothing)
        mag = decode_floats(r["magnitude"])
        start = float(r["startFreq"])
        if r.get("ppo"):
            freqs = start * 2.0 ** (np.arange(mag.size) / float(r["ppo"]))
        elif r.get("freqStep"):
            freqs = start + np.arange(mag.size) * float(r["freqStep"])
        else:
            raise RewError("frequency response has neither ppo nor freqStep")
        phase = decode_floats(r["phase"]) if r.get("phase") else None
        return Spectrum(freqs=freqs, mag_db=mag, phase_deg=phase)
