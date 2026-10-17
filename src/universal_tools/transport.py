"""SDK transports; credentials never cross from the broker to another app implicitly."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import (
    ElicitationCapability,
    ElicitRequest,
    FormElicitationCapability,
    InitializeRequest,
    InitializeResult,
    Tool,
)

from .elicitation import ElicitationRouter, rejected
from .identity import CLIENT_HEADER
from .metadata import initialization_metadata
from .models import BrokerError, HttpTransport, ServiceManifest


class InitializedSession(ClientSession):
    initialization: dict

    def __init__(self, *args, relay: bool = False, **kwargs):
        self.elicitation_router = ElicitationRouter() if relay else None
        if self.elicitation_router is not None:
            kwargs["elicitation_callback"] = self.elicitation_router.callback
        super().__init__(*args, **kwargs)

    async def _received_request(self, responder):
        # SDK 1.30 awaits callbacks in its receive loop. A pending form must
        # not block tool results/cancellation. Keep responders in the SDK's
        # own task group so its normal cancellation and teardown still apply.
        if isinstance(responder.request.root, ElicitRequest):
            if responder.request.root.params.task is not None:
                with responder:
                    await responder.respond(rejected("task-augmented elicitation is not supported"))
                return
            self._task_group.start_soon(super()._received_request, responder)
        else:
            await super()._received_request(responder)

    async def send_request(self, request, result_type, *args, **kwargs):
        # SDK 1.30 advertises both modes for any callback. This bridge implements
        # forms only; narrow the public initialize request before it goes on wire.
        if self.elicitation_router is not None and isinstance(request.root, InitializeRequest):
            request = request.model_copy(deep=True)
            request.root.params.capabilities.elicitation = ElicitationCapability(
                form=FormElicitationCapability()
            )
            request.root.params.capabilities.tasks = None
        return await super().send_request(request, result_type, *args, **kwargs)

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
    async with httpx.AsyncClient(
        headers=headers, timeout=httpx.Timeout(timeout), follow_redirects=False, trust_env=False
    ) as client:
        async with streamable_http_client(url, http_client=client) as (read, write, _):
            async with InitializedSession(
                read,
                write,
                timedelta(seconds=timeout),
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
                    timedelta(seconds=manifest.timeout_seconds),
                    relay=True,
                ) as session:
                    await session.initialize()
                    yield session


async def list_all_tools(session: ClientSession) -> list[Tool]:
    tools = []
    cursor = None
    seen = set()
    for _ in range(100):
        result = await session.list_tools(cursor=cursor)
        tools.extend(result.tools)
        if len(tools) > 2000:
            raise BrokerError("app tool count exceeds the v0.1 limit")
        if not result.nextCursor:
            if len({tool.name for tool in tools}) != len(tools):
                raise BrokerError("app advertised duplicate tool names")
            return tools
        if result.nextCursor in seen:
            raise BrokerError("app returned a repeated pagination cursor")
        cursor = result.nextCursor
        seen.add(cursor)
    raise BrokerError("app tool pagination exceeds the v0.1 limit")
