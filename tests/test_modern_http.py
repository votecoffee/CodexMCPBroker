"""Actual HTTP envelope, authenticated affinity and sealed approval rounds."""

import asyncio
import subprocess
import sys
from contextlib import AsyncExitStack, asynccontextmanager

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp.types import ElicitResult
from test_integration import data, free_port
from test_modern import publication

from universal_tools.cli import initialize, process_options


@asynccontextmanager
async def connection(settings, identity, **options):
    @asynccontextmanager
    async def transport():
        headers = {
            "Authorization": "Bearer " + settings.token,
            "x-universal-tools-client": identity,
        }
        async with httpx2.AsyncClient(headers=headers, trust_env=False) as http:
            async with streamable_http_client(settings.url + "/mcp", http_client=http) as streams:
                yield streams

    async with Client(transport(), mode="auto", cache=None, **options) as client:
        yield client


async def test_http_modern_forms_keep_owner_and_single_dispatch_across_round_trips(tmp_path):
    settings = initialize(tmp_path / "state", free_port())
    app = publication(tmp_path)
    log = (tmp_path / "server.log").open("wb")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "universal_tools",
            "--state",
            str(tmp_path / "state"),
            "app-serve",
            "--publication",
            str(app),
        ],
        stdout=log,
        stderr=log,
        **process_options(),
    )
    try:
        async with httpx2.AsyncClient(trust_env=False) as health:
            async with asyncio.timeout(30):
                while True:
                    try:
                        response = await health.get(
                            settings.url + "/health",
                            headers={"Authorization": "Bearer " + settings.token},
                        )
                        if response.status_code == 200:
                            break
                    except httpx2.HTTPError:
                        pass
                    assert process.poll() is None
                    await asyncio.sleep(0.1)
            calls = []

            async def answer(ctx, params):
                calls.append(params.message)
                if params.mode == "url":
                    return ElicitResult(action="accept")
                return ElicitResult(action="accept", content={"approved": True})

            async with AsyncExitStack() as stack:
                owner = await stack.enter_async_context(
                    connection(settings, "a" * 32, elicitation_callback=answer)
                )
                other = await stack.enter_async_context(
                    connection(settings, "b" * 32, elicitation_callback=answer)
                )
                assert owner.protocol_version == "2026-07-28"
                context = data(await owner.call_tool("acquire_service", {"service_id": "modern"}))[
                    "context_id"
                ]
                assert (await other.call_tool("acquire_service", {"service_id": "modern"})).is_error
                tool = data(await owner.call_tool("describe_tool", {"tool_id": "modern/edit"}))
                args = {
                    "tool_id": "modern/edit",
                    "catalog_revision": tool["catalog_revision"],
                    "context_id": context,
                    "arguments": {"expected_revision": 0},
                    "request_id": "http-edit",
                }
                assert (await other.call_tool("call_write_tool", args)).is_error
                manual = {**args, "request_id": "http-manual"}
                pending = await owner.session.call_tool(
                    "call_write_tool", manual, allow_input_required=True
                )
                responses = {
                    "confirmation": ElicitResult(action="accept", content={"approved": True})
                }
                with pytest.raises(MCPError) as denied:
                    await other.session.call_tool(
                        "call_write_tool",
                        manual,
                        input_responses=responses,
                        request_state=pending.request_state,
                        allow_input_required=True,
                    )
                assert "continuation" in str(denied.value)
                completed = data(
                    await owner.session.call_tool(
                        "call_write_tool",
                        manual,
                        input_responses=responses,
                        request_state=pending.request_state,
                        allow_input_required=True,
                    )
                )
                assert completed["revision"] == 1
                with pytest.raises(MCPError):
                    await owner.session.call_tool(
                        "call_write_tool",
                        manual,
                        input_responses=responses,
                        request_state=pending.request_state,
                        allow_input_required=True,
                    )
                args["arguments"] = {"expected_revision": 1}
                result = data(await owner.call_tool("call_write_tool", args))
                assert result == {"status": "completed", "revision": 2}
                assert len(calls) == 1
                assert (await owner.call_tool("call_write_tool", args)).is_error
                link_tool = data(await owner.call_tool("describe_tool", {"tool_id": "modern/link"}))
                linked = data(
                    await owner.call_tool(
                        "call_write_tool",
                        {
                            "tool_id": "modern/link",
                            "catalog_revision": link_tool["catalog_revision"],
                            "arguments": {},
                            "context_id": context,
                            "request_id": "http-url-once-001",
                        },
                    )
                )
                assert linked == {"action": "accept", "navigation_only": True, "links": 1}
                assert len(calls) == 2
                await owner.call_tool(
                    "release_service", {"service_id": "modern", "context_id": context}
                )
            await health.post(
                settings.url + "/shutdown", headers={"Authorization": "Bearer " + settings.token}
            )
    finally:
        try:
            await asyncio.to_thread(process.wait, 20)
        except subprocess.TimeoutExpired:
            process.terminate()
            await asyncio.to_thread(process.wait, 10)
        log.close()
