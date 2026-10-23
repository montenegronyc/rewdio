"""Phone remote: pairing, auth and the endpoints that let a phone drive the crawl.

Reading state is open to this computer; anything else (including every write) needs the pairing token,
because these endpoints make the speakers play. The token lives in the session folder and travels in
the pairing link / QR code. Network access is opt-in (dashboard.allow_lan).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import socket
from typing import Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .state import live_snapshot

LOCAL = {"127.0.0.1", "::1", "localhost", "testclient"}


def token_for(rt) -> str:
    p = rt.session.dir / "remote_token"
    if not p.exists():
        p.touch(mode=0o600)
        p.write_text(secrets.token_urlsafe(18))
    return p.read_text().strip()


def lan_ip() -> str:
    """This computer's address on the home network (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def pairing_url(rt) -> str:
    d = rt.session.config.dashboard
    host = lan_ip() if d.allow_lan else "127.0.0.1"
    return f"http://{host}:{d.port}/?t={token_for(rt)}"


def routes(runtime: Callable) -> list[Route]:
    def is_local(req: Request) -> bool:
        return (req.client.host if req.client else "") in LOCAL

    def authed(req: Request) -> bool:
        given = req.headers.get("x-rewdio-token") or req.query_params.get("t") or ""
        return secrets.compare_digest(given, token_for(runtime()))

    def deny() -> JSONResponse:
        return JSONResponse({"error": "pair this device first (scan the QR code on the computer)"}, status_code=401)

    async def state(req):
        if not (is_local(req) or authed(req)):
            return deny()
        return JSONResponse(json.loads(json.dumps(live_snapshot(runtime()), default=str)))

    async def events(req):
        if not (is_local(req) or authed(req)):
            return deny()

        async def stream():
            seen = -1
            while not await req.is_disconnected():
                rt = runtime()
                v = (rt.session.version, rt.busy)
                if v != seen:
                    seen = v
                    yield f"data: {json.dumps({'version': v[0], 'busy': v[1]})}\n\n"
                await asyncio.sleep(0.25)
        return StreamingResponse(stream(), media_type="text/event-stream")

    async def confirm(req):
        if not authed(req):
            return deny()
        try:
            body = await req.json() if await req.body() else {}
            xyz = body.get("xyz")
            if xyz is not None:
                xyz = [float(v) for v in xyz]
                if len(xyz) != 3:
                    raise ValueError
        except (ValueError, TypeError):
            return JSONResponse({"ok": False, "message": "xyz must be three numbers (metres)"}, status_code=400)
        rt = runtime()
        res = rt.confirm(body.get("key"), xyz)
        if res.get("ok"):
            res["running"] = rt.advance_in_background()
        return JSONResponse(json.loads(json.dumps(res, default=str)))

    async def step(req):
        if not authed(req):
            return deny()
        return JSONResponse({"running": runtime().advance_in_background()})

    async def cell(req):
        if not authed(req):
            return deny()
        try:
            body = await req.json()
            code, enabled = str(body["code"]).upper(), bool(body["enabled"])
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"ok": False, "message": "send {code, enabled}"}, status_code=400)
        rt = runtime()
        res = rt.set_cell(code, enabled)
        if res.get("ok") and rt.session.workflow.pending_action is None:
            rt.advance_in_background()  # skipped the current cell: move on to the next
        return JSONResponse(res)

    async def pair(req):
        if not is_local(req):
            return deny()
        rt = runtime()
        return JSONResponse({"url": pairing_url(rt), "lan": rt.session.config.dashboard.allow_lan})

    async def qr(req):
        if not is_local(req):
            return deny()
        import io

        import segno
        buf = io.BytesIO()
        segno.make(pairing_url(runtime()), error="m").save(buf, kind="svg", scale=6, border=2, dark="#111111")
        return Response(buf.getvalue(), media_type="image/svg+xml")

    async def setup_get(req):
        if not (is_local(req) or authed(req)):
            return deny()
        from ..engine import setup
        return JSONResponse(json.loads(json.dumps(await setup.status(runtime()), default=str)))

    async def setup_save(req):
        if not authed(req):
            return deny()
        from ..engine import setup
        res = await setup.save(runtime(), (await req.json()).get("updates", {}))
        return JSONResponse(json.loads(json.dumps(res, default=str)), status_code=200 if res.get("ok") else 400)

    async def setup_check(req):
        if not authed(req):
            return deny()
        from ..engine import setup
        return JSONResponse(await setup.check(runtime(), (await req.json()).get("checks", [])))

    return [Route("/api/setup", setup_get), Route("/api/setup", setup_save, methods=["POST"]),
            Route("/api/setup/check", setup_check, methods=["POST"]),
            Route("/api/state", state), Route("/api/events", events), Route("/api/confirm", confirm, methods=["POST"]),
            Route("/api/step", step, methods=["POST"]), Route("/api/crawl/cell", cell, methods=["POST"]),
            Route("/api/pair", pair), Route("/api/pair/qr.svg", qr)]
