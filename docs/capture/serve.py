"""Serve the GUI against the simulator for README captures: `uv run python docs/capture/serve.py <home> <port>`.

Seeds a "Flat" setup (the DSP as Rewdio found it) so the finished calibration has something to A/B against.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

home, port = Path(sys.argv[1]), int(sys.argv[2])
home.mkdir(parents=True, exist_ok=True)
(home / "config.json").write_text(json.dumps({"do_sub_crawl": True, "dashboard": {"port": port}}))
os.environ.update(REWDIO_HOME=str(home), REWDIO_CONFIG=str(home / "config.json"))

import uvicorn  # noqa: E402

from rewdio.dashboard.app import build_app  # noqa: E402
from rewdio.engine import dsp_setups  # noqa: E402
from rewdio.engine.runtime import Runtime  # noqa: E402


async def main():
    rt = Runtime("capture", simulate=True)
    await rt.rig.assert_baseline()
    await rt.rig.measure([c.role for c in rt.session.config.channels], "listening", "MLP")
    await rt.rig.unmute_all()
    dsp_setups.save(rt.session, "Flat", source="manual")
    server = uvicorn.Server(uvicorn.Config(build_app(lambda: rt), host="127.0.0.1", port=port, log_level="warning"))
    await server.serve()

asyncio.run(main())
