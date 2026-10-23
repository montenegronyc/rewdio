"""Live GUI: the phone-first crawl remote (React, built into static/app) and the classic status page.

Serves the built app, generated design-token CSS, and the JSON/SSE API in remote.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from importlib import resources
from typing import Callable

from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from .remote import LOCAL, token_for
from .remote import routes as remote_routes
from .state import snapshot
from .tokens import tokens_css

TYPES = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".json": "application/json",
         ".png": "image/png", ".webmanifest": "application/manifest+json", ".html": "text/html"}


def _static(path: str) -> bytes | None:
    f = resources.files("rewdio.dashboard").joinpath("static").joinpath(path)
    return f.read_bytes() if f.is_file() else None


def build_app(runtime: Callable) -> Starlette:
    async def index(request):
        html = _static("app/index.html")
        if html is None:
            return HTMLResponse(_static("index.html"))  # classic page if the app isn't built
        if (request.client.host if request.client else "") in LOCAL:
            # this computer is trusted: hand its page the token so its buttons work without pairing
            tag = f'<script>window.__rewdioToken={json.dumps(token_for(runtime()))}</script>'
            html = html.replace(b"</head>", tag.encode() + b"</head>", 1)
        return HTMLResponse(html)

    async def classic(_):
        return HTMLResponse(_static("index.html"))

    async def asset(request):
        path = request.path_params["path"]
        if path == "tokens.css":
            return Response(tokens_css(), media_type="text/css")
        if ".." in path:
            return Response(status_code=404)
        data = _static(path)
        if data is None:
            return Response(status_code=404)
        ext = "." + path.rsplit(".", 1)[-1] if "." in path else ""
        return Response(data, media_type=TYPES.get(ext, "application/octet-stream"))

    async def legacy_state(_):  # the classic page polls this
        return JSONResponse(snapshot(runtime().session))

    return Starlette(routes=[Route("/", index), Route("/classic", classic), Route("/api/classic-state", legacy_state),
                             *remote_routes(runtime), Route("/static/{path:path}", asset)])


@contextlib.asynccontextmanager
async def serve_in_background(runtime: Callable, host: str, port: int):
    """Run the GUI next to the MCP server; never let it take the MCP server down."""
    import socket

    import uvicorn

    with socket.socket() as probe:  # uvicorn exits the process on a busy port; check first
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            yield None
            return
    server = uvicorn.Server(uvicorn.Config(build_app(runtime), host=host, port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 5)


def main() -> None:
    """`rewdio-dashboard`: the GUI on its own, driving the calibration itself (no Claude needed)."""
    import os

    import uvicorn

    from ..engine.runtime import Runtime
    rt = Runtime(os.environ.get("REWDIO_SESSION", "default"))
    d = rt.session.config.dashboard
    from .remote import pairing_url
    print(f"Rewdio GUI: http://127.0.0.1:{d.port}/   phone pairing link: {pairing_url(rt)}")
    uvicorn.run(build_app(lambda: rt), host="0.0.0.0" if d.allow_lan else d.host, port=d.port, log_level="warning")
