from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from universal_tools.cli import initialize, process_options
from universal_tools.models import atomic_json
from universal_tools.transport import http_session

TOOLS = {
    "broker_status",
    "list_services",
    "refresh_services",
    "search_tools",
    "describe_tool",
    "call_read_tool",
    "call_write_tool",
    "get_request_status",
}


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    state = tmp_path_factory.mktemp("stack")
    ports = [free_port() for _ in range(3)]
    settings = initialize(state, ports[0])
    processes, logs = [], []

    def launch(arguments):
        log = (state / f"process-{len(logs)}.log").open("wb")
        logs.append(log)
        child = subprocess.Popen(
            [sys.executable, "-m", "universal_tools", "--state", str(state), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            **process_options(),
        )
        processes.append(child)

    try:
        for index, kind in [(1, "math"), (2, "notes")]:
            launch(["demo", "--kind", kind, "--port", str(ports[index])])
            atomic_json(
                state / "services" / f"{kind}.json",
                {
                    "id": kind,
                    "display_name": kind,
                    "transport": {
                        "type": "streamable-http",
                        "url": f"http://127.0.0.1:{ports[index]}/mcp",
                    },
                    "allow_writes": kind == "notes",
                    "timeout_seconds": 3.0,
                },
            )
        deadline = time.monotonic() + 20
        for port in ports[1:]:
            while True:
                try:
                    with httpx.Client(timeout=0.5, trust_env=False) as client:
                        client.get(f"http://127.0.0.1:{port}/mcp")
                    break
                except httpx.TransportError:
                    if time.monotonic() > deadline:
                        pytest.fail("example app startup timed out")
                    time.sleep(0.05)
        launch(["serve"])
        headers = {"Authorization": f"Bearer {settings.token}"}
        while True:
            try:
                with httpx.Client(timeout=0.5, trust_env=False) as client:
                    response = client.get(settings.url + "/health", headers=headers)
                    if response.status_code == 200:
                        break
            except httpx.TransportError:
                pass
            if time.monotonic() > deadline:
                pytest.fail("broker startup timed out")
            time.sleep(0.05)
        yield state, settings, ports
    finally:
        for child in reversed(processes):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        for log in logs:
            log.close()


def data(result):
    assert not result.isError, result.model_dump_json()
    return result.structuredContent or json.loads(result.content[0].text)


async def invoke(session, tool_id, arguments, *, write=False, request_id=None):
    description = data(await session.call_tool("describe_tool", {"tool_id": tool_id}))
    payload = {
        "tool_id": tool_id,
        "catalog_revision": description["catalog_revision"],
        "arguments": arguments,
    }
    if write:
        payload["request_id"] = request_id
    return await session.call_tool("call_write_tool" if write else "call_read_tool", payload)


async def test_http_discovery_routing_and_annotations(stack):
    _, settings, _ = stack
    async with http_session(settings.url + "/mcp", settings.token) as session:
        definitions = (await session.list_tools()).tools
        assert {item.name for item in definitions} == TOOLS
        assert next(t for t in definitions if t.name == "call_read_tool").annotations.readOnlyHint
        assert not next(
            t for t in definitions if t.name == "call_write_tool"
        ).annotations.readOnlyHint
        services = data(await session.call_tool("list_services"))
        assert {item["status"] for item in services["services"]} == {"available"}
        assert data(await invoke(session, "math/add", {"a": 2, "b": 3}))["sum"] == 5
        matches = data(await session.call_tool("search_tools", {"query": "ping"}))
        assert {entry["tool_id"] for entry in matches["matches"]} == {"math/ping", "notes/ping"}


async def test_four_concurrent_clients_keep_results_separate(stack):
    _, settings, _ = stack

    async def client(number):
        async with http_session(settings.url + "/mcp", settings.token) as session:
            return data(await invoke(session, "math/echo", {"value": str(number), "delay": 0.1}))

    results = await asyncio.gather(*(client(i) for i in range(4)))
    assert [item["value"] for item in results] == [str(i) for i in range(4)]


async def test_live_registration_without_restarting_client_or_changing_tools(stack):
    state, settings, ports = stack
    async with http_session(settings.url + "/mcp", settings.token) as session:
        before = (await session.list_tools()).model_dump()
        atomic_json(
            state / "services" / "late-app.json",
            {
                "id": "late-app",
                "display_name": "Newly installed app",
                "transport": {"type": "streamable-http", "url": f"http://127.0.0.1:{ports[1]}/mcp"},
            },
        )
        try:
            missing = data(await session.call_tool("search_tools", {"service_id": "late-app"}))
            assert missing["total"] == 0
            data(await session.call_tool("refresh_services"))
            assert data(await invoke(session, "late-app/ping", {}))["app"] == "math"
            assert before == (await session.list_tools()).model_dump()
        finally:
            (state / "services" / "late-app.json").unlink()
            data(await session.call_tool("refresh_services"))


async def test_new_http_sessions_do_not_refresh_the_catalog_implicitly(stack):
    _, settings, _ = stack
    generations = []
    for _ in range(3):
        async with http_session(settings.url + "/mcp", settings.token) as session:
            generations.append(data(await session.call_tool("broker_status"))["generation"])
            await session.list_tools()
            data(await session.call_tool("search_tools", {"query": "ping"}))
    assert generations[0] == generations[1] == generations[2]


async def test_unavailable_app_does_not_remove_healthy_app(stack):
    state, settings, _ = stack
    atomic_json(
        state / "services" / "offline.json",
        {
            "id": "offline",
            "display_name": "Offline app",
            "transport": {"type": "streamable-http", "url": f"http://127.0.0.1:{free_port()}/mcp"},
            "timeout_seconds": 0.5,
        },
    )
    try:
        async with http_session(settings.url + "/mcp", settings.token) as session:
            refreshed = data(await session.call_tool("refresh_services"))
            statuses = {item["service_id"]: item["status"] for item in refreshed["services"]}
            assert statuses["offline"] == "unavailable"
            assert statuses["math"] == "available"
            assert data(await invoke(session, "math/ping", {}))["ready"]
    finally:
        (state / "services" / "offline.json").unlink()


async def test_native_media_and_app_errors_are_preserved(stack):
    _, settings, _ = stack
    async with http_session(settings.url + "/mcp", settings.token) as session:
        image = await invoke(session, "math/sample_image", {})
        assert not image.isError
        assert image.content[0].type == "image"
        assert image.content[0].mimeType == "image/png"
        error = await invoke(session, "math/sample_error", {})
        assert error.isError
        assert error.content[0].text == "intentional example failure"


async def test_write_deduplication_and_native_revision_conflicts(stack):
    _, settings, _ = stack
    async with http_session(settings.url + "/mcp", settings.token) as session:
        current = data(await invoke(session, "notes/list_items", {"notebook_id": "demo"}))
        arguments = {"notebook_id": "demo", "expected_revision": current["revision"], "text": "one"}
        result = await invoke(
            session, "notes/add_item", arguments, write=True, request_id="write-0001"
        )
        assert data(result)["revision"] == current["revision"] + 1
        duplicate = await invoke(
            session, "notes/add_item", arguments, write=True, request_id="write-0001"
        )
        assert duplicate.isError
        status = data(await session.call_tool("get_request_status", {"request_id": "write-0001"}))
        assert status["status"] == "completed"
        conflict = await invoke(
            session, "notes/add_item", arguments, write=True, request_id="write-0002"
        )
        assert conflict.isError
        final = data(await invoke(session, "notes/list_items", {"notebook_id": "demo"}))
        assert len(final["items"]) == len(current["items"]) + 1


async def test_unknown_operation_is_not_a_read(stack):
    _, settings, _ = stack
    async with http_session(settings.url + "/mcp", settings.token) as session:
        result = await invoke(session, "notes/unclassified", {})
        assert result.isError


def test_http_authentication_and_browser_origin_rejection(stack):
    _, settings, _ = stack
    with httpx.Client(timeout=2, trust_env=False) as client:
        assert client.get(settings.url + "/health").status_code == 401
        assert client.post(settings.url + "/mcp", json={}).status_code == 401
        assert (
            client.get(
                settings.url + "/health", headers={"Authorization": "Bearer incorrect"}
            ).status_code
            == 401
        )
        assert (
            client.get(
                settings.url + "/health",
                headers={
                    "Authorization": f"Bearer {settings.token}",
                    "Origin": "http://evil.example",
                },
            ).status_code
            == 401
        )


async def test_two_plugin_frontends_share_one_broker(stack):
    state, settings, _ = stack
    plugin = Path(__file__).resolve().parents[1] / "plugins/universal-tools"
    runtime_path = plugin / "runtime.json"
    original = runtime_path.read_bytes() if runtime_path.exists() else None
    atomic_json(runtime_path, {"python": sys.executable, "state": str(state)})
    headers = {"Authorization": f"Bearer {settings.token}"}
    with httpx.Client(trust_env=False) as client:
        before = client.get(settings.url + "/health", headers=headers).json()["instance_id"]

    async def frontend(value):
        params = StdioServerParameters(
            command=sys.executable, args=[str(plugin / "server/launch.py")]
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert {tool.name for tool in (await session.list_tools()).tools} == TOOLS
                return data(await invoke(session, "math/echo", {"value": value}))

    try:
        results = await asyncio.gather(frontend("chat-a"), frontend("chat-b"))
        assert [item["value"] for item in results] == ["chat-a", "chat-b"]
        with httpx.Client(trust_env=False) as client:
            after = client.get(settings.url + "/health", headers=headers).json()["instance_id"]
        assert before == after
    finally:
        if original is None:
            runtime_path.unlink()
        else:
            runtime_path.write_bytes(original)


async def test_stdio_app_compatibility(stack):
    state, settings, _ = stack
    atomic_json(
        state / "services" / "stdio-app.json",
        {
            "id": "stdio-app",
            "display_name": "Existing stdio-compatible app",
            "transport": {
                "type": "stdio",
                "command": sys.executable,
                "args": [
                    "-m",
                    "universal_tools",
                    "--state",
                    str(state),
                    "demo",
                    "--kind",
                    "math",
                    "--port",
                    "19990",
                    "--stdio",
                ],
            },
        },
    )
    try:
        async with http_session(settings.url + "/mcp", settings.token) as session:
            data(await session.call_tool("refresh_services"))
            assert data(await invoke(session, "stdio-app/add", {"a": 8, "b": 9}))["sum"] == 17
    finally:
        (state / "services" / "stdio-app.json").unlink()


def test_simultaneous_cold_start_uses_one_broker(tmp_path):
    settings = initialize(tmp_path, free_port())
    command = [sys.executable, "-m", "universal_tools", "--state", str(tmp_path)]
    children = [
        subprocess.Popen(
            command + ["start"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, **process_options()
        )
        for _ in range(2)
    ]
    try:
        results = [child.communicate(timeout=35) for child in children]
        assert all(child.returncode == 0 for child in children), results
        first, second = [json.loads(output) for output, _ in results]
        assert first["instance_id"] == second["instance_id"]
        with httpx.Client(trust_env=False) as client:
            result = client.post(
                settings.url + "/shutdown", headers={"Authorization": f"Bearer {settings.token}"}
            )
            assert result.status_code == 200
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
        subprocess.run(command + ["stop"], capture_output=True, timeout=20, **process_options())


async def test_two_native_clients_cannot_overwrite_a_stale_notebook_revision(stack):
    _, _, ports = stack

    async def writer(text, revision):
        async with http_session(f"http://127.0.0.1:{ports[2]}/mcp") as session:
            return await session.call_tool(
                "add_item", {"notebook_id": "demo", "expected_revision": revision, "text": text}
            )

    async with http_session(f"http://127.0.0.1:{ports[2]}/mcp") as session:
        initial = data(await session.call_tool("list_items", {"notebook_id": "demo"}))
    results = await asyncio.gather(
        writer("native-a", initial["revision"]), writer("native-b", initial["revision"])
    )
    assert sum(not result.isError for result in results) == 1
