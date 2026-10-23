"""REW features used for checks rather than the main loop.

- Input levels: is the mic alive before the first sweep (daredoole/audio-calibration-mcp idea).
- Alignment tool: REW's own sub/main delay, as a second opinion (brandon-fryslie, KevinMeinon).
- EQ prediction: load Rewdio's PEQ into REW and read REW's predicted response, which catches
  coefficient/sample-rate mistakes before they reach the hardware (KevinMeinon docs).
REW addresses the alignment tool by 1-based index only, never UUID; indices shift, so resolve late.
"""

from __future__ import annotations

import asyncio
import math

import numpy as np

from ..minidsp.state import Peq
from .client import RewClient, RewError
from .codec import decode_floats


async def input_levels(rew: RewClient, listen_s: float) -> dict:
    await rew.post("/input-levels/command", {"command": "Start"})
    try:
        await asyncio.sleep(listen_s)
        lv = await rew.get("/input-levels/last-levels") or {}
    finally:
        try:
            await rew.post("/input-levels/command", {"command": "Stop"})
        except RewError:
            pass
    rms = [x for x in (lv.get("rms") or []) if x is not None]
    peak = [x for x in (lv.get("peak") or []) if x is not None]
    alive = bool(peak) and max(peak) > -120 and max(peak) >= max(rms or [-200])
    return {"rms_dbfs": rms, "peak_dbfs": peak, "mic_alive": alive,
            "clipping": bool(peak) and max(peak) > -0.5}


async def index_of(rew: RewClient, uuid: str) -> int:
    for m in await rew.list_measurements():
        if m["uuid"] == uuid:
            return int(m["index"])
    raise RewError(f"measurement {uuid} not in REW")


async def alignment_tool_delay(rew: RewClient, main_uuid: str, sub_uuid: str, crossover_hz: float,
                               max_delay_ms: float) -> dict:
    """Ask REW's alignment tool for the sub delay (B) relative to the main (A)."""
    await rew.post("/alignment-tool/mode", "Phase")
    await rew.post("/alignment-tool/index-a", await index_of(rew, main_uuid))
    await rew.post("/alignment-tool/index-b", await index_of(rew, sub_uuid))
    await rew.post("/alignment-tool/max-positive-delay", max_delay_ms)
    await rew.post("/alignment-tool/max-negative-delay", max_delay_ms)
    await rew.post("/alignment-tool/command", {"command": "Align phase", "frequency": crossover_hz})
    d = await rew.get("/alignment-tool/delay-b")
    val = d.get("value") if isinstance(d, dict) else d
    if val is None or not math.isfinite(float(val)):
        raise RewError(f"REW alignment tool returned no delay ({d})")  # NaN would read as a fake 0 ms
    inv = await rew.get("/alignment-tool/invert-b")
    return {"delay_ms": float(val), "invert": bool(inv.get("value") if isinstance(inv, dict) else inv)}


async def predicted_with_peq(rew: RewClient, uuid: str, filters: list[Peq], equaliser: dict, ppo: int):
    """Load filters into REW for this measurement and return REW's predicted response."""
    await rew.post(f"/measurements/{uuid}/equaliser", equaliser)
    body = {"filters": [{"index": i + 1, "type": "PK", "enabled": True, "isAuto": False, "frequency": f.freq_hz,
                         "gaindB": f.gain_db, "q": f.q} for i, f in enumerate(filters)]}
    await rew.post(f"/measurements/{uuid}/filters", body)
    r = await rew.get(f"/measurements/{uuid}/eq/frequency-response", ppo=ppo)
    mag = decode_floats(r["magnitude"])
    freqs = float(r["startFreq"]) * 2.0 ** (np.arange(mag.size) / float(r.get("ppo") or ppo))
    return freqs, mag
