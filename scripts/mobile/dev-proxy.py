#!/usr/bin/env python3
"""Dev-only stand-in for `tailscale serve`: 127.0.0.1:8766 -> 127.0.0.1:8765.

Forwards HTTP (streamed) and WebSocket traffic to the gated mobile server, the way
`tailscale serve` does:

- injects ``Tailscale-User-Login: <--user>`` and drops any ``tailscale-*`` header the
  client sent, so the identity can't be forged through the proxy;
- forwards ``Host`` and ``Origin`` exactly as the client sent them.

Run the mobile server with ``FEEDBACK_MOBILE_ORIGIN=http://127.0.0.1:8766``, then:

    ../feedBack/.venv/bin/python scripts/mobile/dev-proxy.py [--user you@example.com]

It refuses to listen on anything but loopback. Never point it at a server without the gate.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import sys

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import PlainTextResponse, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailer", "transfer-encoding", "upgrade",
}
WS_HANDSHAKE = {
    "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
    "sec-websocket-accept", "sec-websocket-protocol",
}


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _forward_headers(headers, user: str, drop=()) -> list[tuple[str, str]]:
    out = []
    for k, v in headers.items():
        lk = k.lower()
        if lk.startswith("tailscale-") or lk in HOP_BY_HOP or lk in drop:
            continue
        out.append((k, v))
    out.append(("Tailscale-User-Login", user))
    return out


def build_app(user: str, upstream_port: int) -> Starlette:
    upstream = f"http://127.0.0.1:{upstream_port}"
    client = httpx.AsyncClient(base_url=upstream, timeout=None, trust_env=False)

    async def http_proxy(request: Request):
        raw_path = request.scope.get("raw_path") or request.url.path.encode()
        target = raw_path.decode("latin-1")
        if request.url.query:
            target += "?" + request.url.query
        req = client.build_request(
            request.method, target,
            headers=_forward_headers(request.headers, user, drop={"content-length"}),
            content=request.stream(),
        )
        try:
            resp = await client.send(req, stream=True)
        except httpx.HTTPError:
            return PlainTextResponse("Bad Gateway", status_code=502)
        # uvicorn adds its own date/server headers to the proxied response.
        skip = HOP_BY_HOP | {"date", "server"}
        headers = [(k, v) for k, v in resp.headers.multi_items() if k.lower() not in skip]
        out = StreamingResponse(
            resp.aiter_raw(), status_code=resp.status_code,
            background=BackgroundTask(resp.aclose),
        )
        out.raw_headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers]
        return out

    async def ws_proxy(websocket: WebSocket):
        host = websocket.headers.get("host", "")
        raw_path = (websocket.scope.get("raw_path") or b"/").decode("latin-1")
        qs = websocket.scope.get("query_string", b"").decode("latin-1")
        uri = f"ws://{host}{raw_path}" + (f"?{qs}" if qs else "")
        drop = WS_HANDSHAKE | {"host", "origin"}
        extra = _forward_headers(websocket.headers, user, drop=drop)
        subprotocols = [
            p.strip() for p in websocket.headers.get("sec-websocket-protocol", "").split(",") if p.strip()
        ] or None
        try:
            upstream_ws = await ws_connect(
                uri,
                host="127.0.0.1", port=upstream_port,   # TCP target; Host header stays the client's
                origin=websocket.headers.get("origin"),
                additional_headers=extra,
                subprotocols=subprotocols,
                proxy=None,
                max_size=None,
                open_timeout=10,
            )
        except (InvalidHandshake, OSError, ValueError):
            await websocket.close(code=1008)   # before accept -> client sees 403
            return

        await websocket.accept(subprotocol=upstream_ws.subprotocol)

        async def client_to_upstream():
            try:
                while True:
                    msg = await websocket.receive()
                    if msg["type"] == "websocket.disconnect":
                        break
                    if msg.get("text") is not None:
                        await upstream_ws.send(msg["text"])
                    elif msg.get("bytes") is not None:
                        await upstream_ws.send(msg["bytes"])
            except (WebSocketDisconnect, ConnectionClosed):
                pass
            finally:
                await upstream_ws.close()

        async def upstream_to_client():
            code = 1000
            try:
                async for data in upstream_ws:
                    if isinstance(data, bytes):
                        await websocket.send_bytes(data)
                    else:
                        await websocket.send_text(data)
                code = upstream_ws.close_code or 1000
            except ConnectionClosed as exc:
                code = exc.rcvd.code if exc.rcvd else 1011
            try:
                await websocket.close(code=code)
            except RuntimeError:
                pass

        tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await upstream_ws.close()

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            await client.aclose()

    methods = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
    return Starlette(routes=[
        WebSocketRoute("/{path:path}", ws_proxy),
        Route("/{path:path}", http_proxy, methods=methods),
    ], lifespan=lifespan)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--user", default="dev@example.com", help="Tailscale-User-Login to inject")
    ap.add_argument("--listen", default="127.0.0.1", help="listen address (loopback only)")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--upstream-port", type=int, default=8765)
    args = ap.parse_args(argv)
    if not _is_loopback(args.listen):
        print(f"dev-proxy: refusing to listen on non-loopback address {args.listen!r}", file=sys.stderr)
        return 2
    if not args.user.strip():
        print("dev-proxy: --user must not be empty", file=sys.stderr)
        return 2
    uvicorn.run(
        build_app(args.user.strip(), args.upstream_port),
        host=args.listen, port=args.port, ws_max_size=64 * 1024, log_level="info",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
