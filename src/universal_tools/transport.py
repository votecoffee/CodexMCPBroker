"""SDK transports; credentials never cross from the broker to another app implicitly."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Tool

from .models import BrokerError, HttpTransport, ServiceManifest


@asynccontextmanager
async def http_session(url: str, token: str | None = None, timeout: float = 20):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(
        headers=headers, timeout=httpx.Timeout(timeout), follow_redirects=False, trust_env=False
    ) as client:
        async with streamable_http_client(url, http_client=client) as (read, write, _):
            async with ClientSession(read, write, timedelta(seconds=timeout)) as session:
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
        async with http_session(transport.url, token, manifest.timeout_seconds) as session:
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
                async with ClientSession(
                    read, write, timedelta(seconds=manifest.timeout_seconds)
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
