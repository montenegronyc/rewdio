"""Live GUI: the phone-first crawl remote (React, built into static/app) and the classic status page.

Serves the built app, generated design-token CSS, and the JSON/SSE API in remote.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from importlib import resources
from pathlib import Path
from typing import Callable

from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from .remote import LOCAL, lan_ip, token_for
from .remote import routes as remote_routes
from .state import snapshot
from .tokens import tokens_css

TYPES = {".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".json": "application/json",
         ".png": "image/png", ".webmanifest": "application/manifest+json", ".html": "text/html"}


def _static(path: str) -> bytes | None:
    """A file from the packaged static folder, and nothing outside it.

    pathlib drops the base when a component is absolute ("/etc/passwd"), so resolve and check containment
    rather than looking for "..".
    """
    root = Path(str(resources.files("rewdio.dashboard").joinpath("static"))).resolve()
    if not path or path.startswith(("/", "\\")) or "\\" in path or ":" in path:
        return None
    f = (root / path).resolve()
    if not f.is_relative_to(root) or not f.is_file():
        return None
    return f.read_bytes()


class HostGuard:
    """Refuse requests whose Host isn't this machine (blocks DNS-rebinding pages) and cross-site writes."""

    def __init__(self, app, allowed: Callable[[], set[str]]):
        self.app, self.allowed = app, allowed

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            host = headers.get("host", "").rsplit(":", 1)[0].strip("[]").lower()
            origin = headers.get("origin")
            bad = host not in self.allowed()
            if not bad and scope["method"] not in ("GET", "HEAD") and origin:
                bad = origin.split("//", 1)[-1].rsplit(":", 1)[0].strip("[]").lower() != host
            if bad:
                await Response("forbidden host or origin", status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_app(runtime: Callable) -> HostGuard:
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
        data = _static(path)
        if data is None:
            return Response(status_code=404)
        ext = "." + path.rsplit(".", 1)[-1] if "." in path else ""
        return Response(data, media_type=TYPES.get(ext, "application/octet-stream"))

    async def legacy_state(_):  # the classic page polls this
        return JSONResponse(snapshot(runtime().session))

    app = Starlette(routes=[Route("/", index), Route("/classic", classic), Route("/api/classic-state", legacy_state),
                            *remote_routes(runtime), Route("/static/{path:path}", asset)])

    def allowed() -> set[str]:
        hosts = {"localhost", "127.0.0.1", "::1", "testserver"}
        if runtime().session.config.dashboard.allow_lan:
            import socket
            name = socket.gethostname().lower()
            hosts |= {lan_ip(), name, name.split(".")[0] + ".local"}
        return hosts

    return HostGuard(app, allowed)


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
