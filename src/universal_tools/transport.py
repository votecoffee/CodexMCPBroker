"""SDK transports; credentials never cross from the broker to another app implicitly."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import httpx2
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from mcp.types import (
    ElicitationCapability,
    FormElicitationCapability,
    InitializeResult,
    PaginatedRequestParams,
    Tool,
)

from .elicitation import ElicitationRouter
from .identity import CLIENT_HEADER
from .metadata import initialization_metadata
from .models import BrokerError, HttpTransport, ServiceManifest


class RoutingDispatcher(JSONRPCDispatcher):
    """Capture the originating form route before SDK 2 schedules its callback."""

    def __init__(self, read, write, router):
        super().__init__(read, write)
        self.router = router

    async def _dispatch_request(self, *args, **kwargs):
        with self.router.capture():
            await super()._dispatch_request(*args, **kwargs)


class InitializedSession(ClientSession):
    initialization: dict

    def __init__(self, read, write, *args, relay: bool = False, **kwargs):
        self.elicitation_router = ElicitationRouter() if relay else None
        if self.elicitation_router is not None:
            kwargs["elicitation_callback"] = self.elicitation_router.callback
            kwargs["dispatcher"] = RoutingDispatcher(read, write, self.elicitation_router)
            read = write = None
        super().__init__(read, write, *args, **kwargs)

    def _build_capabilities(self, version):
        capabilities = super()._build_capabilities(version)
        if self.elicitation_router is not None:
            capabilities.elicitation = ElicitationCapability(form=FormElicitationCapability())
            capabilities.tasks = None
        return capabilities

    async def initialize(self) -> InitializeResult:
        result = await super().initialize()
        self.initialization = initialization_metadata(result)
        return result


@asynccontextmanager
async def http_session(
    url: str,
    token: str | None = None,
    timeout: float = 20,
    *,
    client_id: str | None = None,
    relay: bool = False,
    elicitation_callback=None,
):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if client_id is not None:
        headers[CLIENT_HEADER] = client_id
    async with httpx2.AsyncClient(
        headers=headers, timeout=httpx2.Timeout(timeout), follow_redirects=False, trust_env=False
    ) as client:
        async with streamable_http_client(url, http_client=client, max_sse_event_size=None) as (
            read,
            write,
        ):
            async with InitializedSession(
                read,
                write,
                timeout,
                relay=relay,
                elicitation_callback=elicitation_callback,
            ) as session:
                await session.initialize()
                yield session


@asynccontextmanager
async def service_session(manifest: ServiceManifest):
    transport = manifest.transport
    if isinstance(transport, HttpTransport):
        token = None
        if transport.bearer_token_env:
            token = os.environ.get(transport.bearer_token_env)
            if not token:
                raise BrokerError("the app's configured credential environment variable is unset")
        async with http_session(
            transport.url, token, manifest.timeout_seconds, relay=True
        ) as session:
            yield session
    else:
        # The SDK supplies a minimal platform environment. Forward only explicit additions.
        env = {}
        for name in transport.env_vars:
            if name not in os.environ:
                raise BrokerError("a configured app environment variable is unset")
            env[name] = os.environ[name]
        params = StdioServerParameters(
            command=transport.command, args=transport.args, cwd=transport.cwd, env=env
        )
        # App stderr can contain secrets; do not merge it into broker logs.
        with open(os.devnull, "w", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with InitializedSession(
                    read,
                    write,
                    manifest.timeout_seconds,
                    relay=True,
                ) as session:
                    await session.initialize()
                    yield session


async def list_all_tools(session: ClientSession) -> list[Tool]:
    tools = []
    cursor = None
    seen = set()
    for _ in range(100):
        result = await session.list_tools(params=PaginatedRequestParams(cursor=cursor))
        tools.extend(result.tools)
        if len(tools) > 2000:
            raise BrokerError("app tool count exceeds the v0.1 limit")
        if not result.next_cursor:
            if len({tool.name for tool in tools}) != len(tools):
                raise BrokerError("app advertised duplicate tool names")
            return tools
        if result.next_cursor in seen:
            raise BrokerError("app returned a repeated pagination cursor")
        cursor = result.next_cursor
        seen.add(cursor)
    raise BrokerError("app tool pagination exceeds the v0.1 limit")
