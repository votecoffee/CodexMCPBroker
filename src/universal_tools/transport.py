"""SDK transports; credentials never cross from the broker to another app implicitly."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from copy import deepcopy

import httpx2
from mcp import ClientSession, StdioServerParameters
from mcp.client._probe import negotiate_auto
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.client.subscriptions import ToolsListChanged, listen
from mcp.shared.exceptions import MCPError
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from mcp.types import (
    INVALID_REQUEST,
    DiscoverResult,
    ElicitationCapability,
    ElicitRequest,
    ErrorData,
    FormElicitationCapability,
    InitializeResult,
    InputRequiredResult,
    PaginatedRequestParams,
    Tool,
)

from .diagnostics import measured
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

    async def connect(self, mode="legacy", *, cache_ttl=0):
        self.catalog_changed = asyncio.Event()
        self.catalog_epoch = 0
        self.watch_ready = asyncio.Event()
        self.cache_ttl = cache_ttl
        self.tool_cache = None
        if mode == "legacy":
            await self.initialize()
        elif mode == "auto":
            await negotiate_auto(self)
        else:
            self.adopt(DiscoverResult.model_validate(await self.send_discover(mode)))
        if self.protocol_version == "2026-07-28":
            value = {
                "protocolVersion": self.protocol_version,
                "capabilities": self.server_capabilities.model_dump(
                    mode="json", by_alias=True, exclude_none=True
                ),
            }
            if self.server_info is not None:
                value["serverInfo"] = self.server_info.model_dump(
                    mode="json", by_alias=True, exclude_none=True
                )
            if self.instructions is not None:
                value["instructions"] = self.instructions
            self.initialization = initialization_metadata(value)

    async def call_tool(self, name, arguments=None, **options):
        if self.protocol_version != "2026-07-28":
            return await super().call_tool(name, arguments, **options)
        responses = state = None
        for _ in range(9):
            result = await super().call_tool(
                name,
                arguments,
                **options,
                input_responses=responses,
                request_state=state,
                allow_input_required=True,
            )
            if not isinstance(result, InputRequiredResult):
                return result
            if (
                len(result.model_dump_json().encode("utf-8")) > 64 * 1024
                or not 1 <= len(result.input_requests) <= 8
                or self.elicitation_router is None
            ):
                raise MCPError(INVALID_REQUEST, "unsupported or oversized input continuation")
            responses = {}
            for key, request in result.input_requests.items():
                if not isinstance(request, ElicitRequest):
                    raise MCPError(INVALID_REQUEST, "only form input continuations are supported")
                answer = await self.elicitation_router.callback(None, request.params)
                if isinstance(answer, ErrorData):
                    raise MCPError(answer.code, answer.message)
                responses[key] = answer
            state = result.request_state
        raise MCPError(INVALID_REQUEST, "input continuation round limit exceeded")

    async def watch(self):
        delay = 1
        while True:
            try:
                async with listen(self, tools_list_changed=True) as subscription:
                    self.watch_ready.set()
                    async for event in subscription:
                        if isinstance(event, ToolsListChanged):
                            self.tool_cache = None
                            self.catalog_epoch += 1
                            self.catalog_changed.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            # Re-listen after a lost/gracefully closed stream. Refetch, never replay calls.
            self.tool_cache = None
            self.catalog_epoch += 1
            self.catalog_changed.set()
            self.watch_ready.set()
            await asyncio.sleep(delay)
            delay = min(30, delay * 2)


@asynccontextmanager
async def http_session(
    url: str,
    token: str | None = None,
    timeout: float = 20,
    *,
    client_id: str | None = None,
    relay: bool = False,
    elicitation_callback=None,
    protocol_mode="legacy",
    cache_ttl=0,
    watch_catalog=False,
    max_sse_event_bytes=None,
):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if client_id is not None:
        headers[CLIENT_HEADER] = client_id
    async with httpx2.AsyncClient(
        headers=headers, timeout=httpx2.Timeout(timeout), follow_redirects=False, trust_env=False
    ) as client:
        async with streamable_http_client(
            url, http_client=client, max_sse_event_size=max_sse_event_bytes
        ) as (
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
                await session.connect(protocol_mode, cache_ttl=cache_ttl)
                async with watching(session, watch_catalog):
                    yield session


@asynccontextmanager
async def watching(session, enabled):
    task = (
        asyncio.create_task(session.watch())
        if enabled and session.protocol_version == "2026-07-28"
        else None
    )
    try:
        if task is not None:
            await session.watch_ready.wait()
        yield
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


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
            transport.url,
            token,
            manifest.timeout_seconds,
            relay=True,
            protocol_mode=manifest.protocol_mode,
            cache_ttl=manifest.discovery_cache_ttl_seconds,
            watch_catalog=manifest.watch_catalog,
            max_sse_event_bytes=transport.max_sse_event_bytes,
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
                    await session.connect(
                        manifest.protocol_mode, cache_ttl=manifest.discovery_cache_ttl_seconds
                    )
                    async with watching(session, manifest.watch_catalog):
                        yield session


async def list_all_tools(session: ClientSession, *, fresh=False) -> list[Tool]:
    with measured("discovery"):
        return await _list_all_tools(session, fresh=fresh)


async def _list_all_tools(session, *, fresh=False):
    epoch = getattr(session, "catalog_epoch", 0)
    cached = getattr(session, "tool_cache", None)
    if not fresh and cached is not None and cached[0] > time.monotonic():
        return deepcopy(cached[1])
    ttl = getattr(session, "cache_ttl", 0)
    tools = []
    cursor = None
    seen = set()
    for _ in range(100):
        result = await session.list_tools(params=PaginatedRequestParams(cursor=cursor))
        ttl = min(ttl, (getattr(result, "ttl_ms", None) or 0) / 1000)
        tools.extend(result.tools)
        if len(tools) > 2000:
            raise BrokerError("app tool count exceeds the v0.1 limit")
        if not result.next_cursor:
            if len({tool.name for tool in tools}) != len(tools):
                raise BrokerError("app advertised duplicate tool names")
            if ttl > 0 and getattr(session, "catalog_epoch", 0) == epoch:
                session.tool_cache = (time.monotonic() + ttl, deepcopy(tools))
            return tools
        if result.next_cursor in seen:
            raise BrokerError("app returned a repeated pagination cursor")
        cursor = result.next_cursor
        seen.add(cursor)
    raise BrokerError("app tool pagination exceeds the v0.1 limit")
