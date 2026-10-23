"""Async client for the minidsp-rs daemon HTTP API.

Endpoints: GET /devices, GET /devices/{i}, POST /devices/{i}/config with a
partial Config ({"outputs": [{"index": n, "gain": ..., "mute": ..., ...}]}).
"""

from __future__ import annotations

from typing import Any

import httpx

from .biquad import Biquad


class MiniDSPError(RuntimeError):
    pass


def _duration(ms: float) -> dict:
    ns = int(round(ms * 1e6))
    return {"secs": ns // 1_000_000_000, "nanos": ns % 1_000_000_000}


class MiniDSPClient:
    def __init__(self, url: str = "unix:///tmp/minidsp.sock", device_index: int = 0, *,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 10.0):
        self.device_index = device_index
        if transport is None and url.startswith("unix://"):
            transport = httpx.AsyncHTTPTransport(uds=url[len("unix://"):])
            base = "http://minidsp"
        else:
            base = url.rstrip("/") if not url.startswith("unix://") else "http://minidsp"
        self.url = url
        self._http = httpx.AsyncClient(base_url=base, transport=transport, timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, body: Any = None) -> Any:
        try:
            resp = await self._http.request(method, path, json=body)
        except httpx.TransportError as e:
            raise MiniDSPError(
                f"minidsp-rs daemon unreachable at {self.url} ({e}). Run `minidspd` and enable "
                "[http_server] in its config, or point minidsp_url at its unix socket."
            ) from e
        if resp.status_code >= 400:
            raise MiniDSPError(f"minidsp {method} {path} -> {resp.status_code}: {resp.text}")
        if not resp.text:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    async def devices(self) -> list[dict]:
        return await self._call("GET", "/devices")

    async def status(self) -> dict:
        return await self._call("GET", f"/devices/{self.device_index}")

    async def apply(self, config: dict) -> None:
        await self._call("POST", f"/devices/{self.device_index}/config", config)

    async def set_outputs(self, outputs: list[dict]) -> None:
        if outputs:
            await self.apply({"outputs": outputs})

    @staticmethod
    def output_patch(index: int, *, gain_db: float | None = None, mute: bool | None = None,
                     delay_ms: float | None = None, invert: bool | None = None,
                     peq: dict[int, Biquad | None] | None = None) -> dict:
        """Build one output entry. A PEQ slot mapped to None is bypassed."""
        out: dict[str, Any] = {"index": index}
        if gain_db is not None:
            out["gain"] = round(float(gain_db), 2)
        if mute is not None:
            out["mute"] = bool(mute)
        if delay_ms is not None:
            out["delay"] = _duration(delay_ms)
        if invert is not None:
            out["invert"] = bool(invert)
        if peq:
            out["peq"] = [
                {"index": slot, "bypass": True} if bq is None
                else {"index": slot, "bypass": False, "coeff": bq.to_minidsp()}
                for slot, bq in sorted(peq.items())
            ]
        return out

    async def set_master(self, *, volume_db: float | None = None, mute: bool | None = None,
                         preset: int | None = None) -> None:
        ms: dict[str, Any] = {}
        if volume_db is not None:
            ms["volume"] = float(volume_db)
        if mute is not None:
            ms["mute"] = mute
        if preset is not None:
            ms["preset"] = preset
        if ms:
            await self.apply({"master_status": ms})
