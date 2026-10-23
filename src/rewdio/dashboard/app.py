"""Local read-only dashboard: watch the loop run. Serves static files, generated token CSS and a JSON state."""

from __future__ import annotations

import asyncio
import contextlib
from importlib import resources
from typing import Callable

from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from ..engine.store import Session
from .state import snapshot
from .tokens import tokens_css


def _static(name: str) -> str:
    return resources.files("rewdio.dashboard").joinpath(f"static/{name}").read_text()


def build_app(session: Callable[[], Session]) -> Starlette:
    async def index(_):
        return HTMLResponse(_static("index.html"))

    async def asset(request):
        name = request.path_params["name"]
        if name == "tokens.css":
            return Response(tokens_css(), media_type="text/css")
        types = {"app.js": "text/javascript", "styles.css": "text/css"}
        if name not in types:
            return Response(status_code=404)
        return Response(_static(name), media_type=types[name])

    async def state(_):
        return JSONResponse(snapshot(session()))

    return Starlette(routes=[Route("/", index), Route("/api/state", state), Route("/static/{name}", asset)])


@contextlib.asynccontextmanager
async def serve_in_background(session: Callable[[], Session], host: str, port: int):
    """Run the dashboard next to the MCP server; never let it take the MCP server down."""
    import socket

    import uvicorn

    with socket.socket() as probe:  # uvicorn exits the process on a busy port; check first
        if probe.connect_ex((host, port)) == 0:
            yield None
            return
    server = uvicorn.Server(uvicorn.Config(build_app(session), host=host, port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    try:
        yield f"http://{host}:{port}/"
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, 5)


def main() -> None:
    """`rewdio-dashboard`: view a session without running the MCP server."""
    import os

    import uvicorn

    sess = Session(None, os.environ.get("REWDIO_SESSION", "default"))
    d = sess.config.dashboard
    uvicorn.run(build_app(lambda: Session(None, os.environ.get("REWDIO_SESSION", "default"))), host=d.host, port=d.port)
