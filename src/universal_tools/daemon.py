"""Authenticated loopback broker. Only this process owns the shared app locks."""

from __future__ import annotations

import hmac
import os
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from starlette.applications import Starlette
from starlette.responses import JSONResponse

from . import __version__
from .broker import Broker
from .identity import CLIENT_HEADER, CLIENT_PATTERN, client_identity
from .models import BrokerError, Settings, canonical_digest, load_settings
from .surface import LocalBackend, create_surface


class InstanceLock:
    """An OS-owned lock is released automatically if the daemon exits or crashes."""

    def __init__(self, path: Path):
        self.path = path
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise BrokerError("a broker already owns this state directory") from None
        return self

    def __exit__(self, *args):
        self.stream.close()


class ProtectedApp:
    def __init__(self, app, settings: Settings, server: uvicorn.Server, instance_id: str):
        self.app, self.settings, self.server, self.instance_id = app, settings, server, instance_id

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        expected = f"Bearer {self.settings.token}".encode("ascii")
        if b"origin" in headers or not hmac.compare_digest(
            headers.get(b"authorization", b""), expected
        ):
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
        elif scope["path"] == "/health" and scope["method"] == "GET":
            response = JSONResponse(
                {"name": "universal-tools", "version": __version__, "instance_id": self.instance_id}
            )
        elif scope["path"] == "/shutdown" and scope["method"] == "POST":
            self.server.should_exit = True
            response = JSONResponse({"stopping": True})
        else:
            value = headers.get(CLIENT_HEADER.encode(), b"").decode("ascii", errors="replace")
            if value and not re.fullmatch(CLIENT_PATTERN, value):
                return await JSONResponse({"error": "invalid frontend identity"}, status_code=400)(
                    scope, receive, send
                )
            token = client_identity.set(value or None)
            try:
                # The SDK binds stateful MCP sessions to this affinity principal.
                # Bearer authentication above remains the local-user boundary.
                scope = {
                    **scope,
                    "user": AuthenticatedUser(
                        AccessToken(
                            token="validated-local-broker",
                            client_id=canonical_digest({"frontend": value or None}),
                            scopes=[],
                        )
                    ),
                }
                return await self.app(scope, receive, send)
            finally:
                client_identity.reset(token)
        return await response(scope, receive, send)


def serve(state: Path) -> None:
    with InstanceLock(state / "broker.lock"):
        # Read only after taking the same lock used by stopped-broker settings
        # updates, so a concurrent start cannot retain an obsolete limit.
        settings = load_settings(state)
        broker = Broker(state, max_request_body_bytes=settings.max_request_body_bytes)
        mcp = create_surface(LocalBackend(broker), port=settings.port)
        base_app = mcp.streamable_http_app()

        @asynccontextmanager
        async def lifespan(app):
            # Discovery belongs to the ASGI process, independently of MCP sessions.
            await broker.start()
            try:
                await broker.refresh()
                async with mcp.session_manager.run():
                    yield
            finally:
                await broker.aclose()

        app = Starlette(routes=base_app.routes, lifespan=lifespan)
        config = uvicorn.Config(
            app,
            host=settings.host,
            port=settings.port,
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=15,
        )
        server = uvicorn.Server(config)
        server.config.app = ProtectedApp(app, settings, server, secrets.token_hex(16))
        server.run()
