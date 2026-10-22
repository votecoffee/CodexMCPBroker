from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import universal_tools.surface as surface_module
from universal_tools.cli import initialize, probe, process_options, recover_context, stop
from universal_tools.models import BrokerError, atomic_json
from universal_tools.ownership import OwnershipJournal
from universal_tools.surface import RemoteBackend
from universal_tools.transport import http_session

TOOLS = {
    "describe_service",
    "describe_capability",
    "refresh_capabilities",
    "broker_status",
    "list_services",
    "refresh_services",
    "search_tools",
    "describe_tool",
    "call_read_tool",
    "call_write_tool",
    "get_request_status",
    "acquire_service",
    "release_service",
    "service_session_status",
}
BROKER_TEST_OPERATIONS = {}


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_fixture(child, ready, label, *, timeout=20):
    """Each process gets its own setup window; never retry a terminated child."""
    deadline = time.monotonic() + timeout
    while True:
        if child.poll() is not None:
            pytest.fail(f"{label} exited before readiness; inspect its isolated process log")
        if ready():
            return
        if time.monotonic() > deadline:
            pytest.fail(f"{label} startup timed out; inspect its isolated process log")
        time.sleep(0.05)


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
        return child

    try:
        apps = []
        for index, kind in [(1, "math"), (2, "notes")]:
            apps.append(launch(["demo", "--kind", kind, "--port", str(ports[index])]))
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

        def app_ready(port):
            try:
                with httpx.Client(timeout=0.5, trust_env=False) as client:
                    with client.stream("GET", f"http://127.0.0.1:{port}/mcp"):
                        pass
                return True
            except httpx.TransportError:
                return False

        for child, port in zip(apps, ports[1:], strict=True):
            wait_for_fixture(child, lambda port=port: app_ready(port), "example app")
        daemon_child = launch(["serve"])

        def wait_broker():
            wait_for_fixture(daemon_child, lambda: probe(settings) is not None, "broker")

        wait_broker()

        def recover_fixture(service_id, context_id):
            nonlocal daemon_child
            stop(state)
            daemon_child.wait(timeout=15)
            recover_context(state, service_id, context_id, acknowledged=True)
            daemon_child = launch(["serve"])
            wait_broker()

        def crash_and_restart_fixture():
            nonlocal daemon_child
            # Only this fixture's owned process, never an installed broker.
            daemon_child.terminate()
            daemon_child.wait(timeout=15)
            daemon_child = launch(["serve"])
            wait_broker()

        BROKER_TEST_OPERATIONS[state] = {
            "recover": recover_fixture,
            "crash_restart": crash_and_restart_fixture,
        }
        yield state, settings, ports
    finally:
        BROKER_TEST_OPERATIONS.pop(state, None)
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
    assert not result.is_error, result.model_dump_json()
    return result.structured_content or json.loads(result.content[0].text)


@pytest.fixture
async def remote_backend():
    async with AsyncExitStack() as resources:

        def create(settings):
            backend = RemoteBackend(settings)
            resources.push_async_callback(backend.aclose)
            return backend

        yield create


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
        assert next(t for t in definitions if t.name == "call_read_tool").annotations.read_only_hint
        assert not next(
            t for t in definitions if t.name == "call_write_tool"
        ).annotations.read_only_hint
        services = data(await session.call_tool("list_services"))
        assert {item["status"] for item in services["services"]} == {"available"}
        assert data(await invoke(session, "math/add", {"a": 2, "b": 3}))["sum"] == 5
        matches = data(await session.call_tool("search_tools", {"query": "ping"}))
        assert {entry["tool_id"] for entry in matches["matches"]} == {"math/ping", "notes/ping"}


async def test_http_initialization_metadata_reaches_frontend_without_capability_promotion(stack):
    _, settings, ports = stack
    async with http_session(f"http://127.0.0.1:{ports[1]}/mcp") as app:
        expected = app.initialization
    async with http_session(settings.url + "/mcp", settings.token) as frontend:
        info = data(await frontend.call_tool("describe_service", {"service_id": "math"}))
        assert info["initialization"] == expected
        assert info["metadata_source"] == "catalog" and "context_id" not in info
        assert info["trust"] == "untrusted_app_data"
        assert info["broker_support"]["resources"] is False
        assert info["broker_support"]["server_requests"] is False
        assert frontend.initialization["serverInfo"] != expected["serverInfo"]
        assert "describe_service" in frontend.initialization["instructions"]


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


async def test_frontend_reuses_one_broker_handshake_for_sequential_calls(stack, monkeypatch):
    _, settings, _ = stack
    original = surface_module.http_session
    connections = []

    @asynccontextmanager
    async def counted(*args, **kwargs):
        connections.append(kwargs["client_id"])
        async with original(*args, **kwargs) as session:
            yield session

    monkeypatch.setattr(surface_module, "http_session", counted)
    async with RemoteBackend(settings) as frontend:
        initial = data(await frontend.invoke("broker_status", {}))
        for _ in range(8):
            assert data(await frontend.invoke("broker_status", {})) == initial
        assert connections == [frontend.client_id]
    assert not frontend.pool._workers
    assert (await frontend.invoke("broker_status", {})).is_error
    assert len(connections) == 1


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
        assert not image.is_error
        assert image.content[0].type == "image"
        assert image.content[0].mime_type == "image/png"
        error = await invoke(session, "math/sample_error", {})
        assert error.is_error
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
        assert duplicate.is_error
        status = data(await session.call_tool("get_request_status", {"request_id": "write-0001"}))
        assert status["status"] == "completed"
        conflict = await invoke(
            session, "notes/add_item", arguments, write=True, request_id="write-0002"
        )
        assert conflict.is_error
        final = data(await invoke(session, "notes/list_items", {"notebook_id": "demo"}))
        assert len(final["items"]) == len(current["items"]) + 1


async def test_unknown_operation_is_not_a_read(stack):
    _, settings, _ = stack
    async with http_session(settings.url + "/mcp", settings.token) as session:
        result = await invoke(session, "notes/unclassified", {})
        assert result.is_error


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


async def test_two_protocol_frontends_share_one_broker(stack):
    state, settings, _ = stack
    headers = {"Authorization": f"Bearer {settings.token}"}
    with httpx.Client(trust_env=False) as client:
        before = client.get(settings.url + "/health", headers=headers).json()["instance_id"]

    async def frontend(value):
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "universal_tools", "--state", str(state), "proxy"]
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                assert {tool.name for tool in (await session.list_tools()).tools} == TOOLS
                return data(await invoke(session, "math/echo", {"value": value}))

    results = await asyncio.gather(frontend("chat-a"), frontend("chat-b"))
    assert [item["value"] for item in results] == ["chat-a", "chat-b"]
    with httpx.Client(trust_env=False) as client:
        after = client.get(settings.url + "/health", headers=headers).json()["instance_id"]
    assert before == after


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
    assert sum(not result.is_error for result in results) == 1


@asynccontextmanager
async def exclusive_app(
    stack, *, timeout=20, audit_path=None, fixture="stateful_mcp.py", session_mode="exclusive"
):
    # Ordinary workflow cases need cold SDK/process startup margin. Deadline
    # cases supply their own shorter budget and deliberately stalled operation.
    state, settings, _ = stack
    path = state / "services/stateful.json"
    assert not path.exists()
    atomic_json(
        path,
        {
            "id": "stateful",
            "display_name": "Synthetic stateful app",
            "transport": {
                "type": "stdio",
                "command": sys.executable,
                "args": [str(Path(__file__).parent / "fixtures" / fixture)]
                + (["--audit", str(audit_path)] if audit_path is not None else []),
            },
            "session_mode": session_mode,
            "allow_writes": True,
            "timeout_seconds": timeout,
        },
    )
    async with RemoteBackend(settings) as admin:
        try:
            services = data(await admin.invoke("refresh_services", {}))["services"]
            assert (
                next(item for item in services if item["service_id"] == "stateful")["status"]
                == "available"
            )
            yield settings
        finally:
            path.unlink()
            refreshed = await admin.invoke("refresh_services", {})
            if refreshed.is_error:
                # A fixture-owned broker restart invalidates this old HTTP session.
                # This is an explicit metadata retry, never a replay of an app write.
                refreshed = await admin.invoke("refresh_services", {})
            data(refreshed)
            reservation = OwnershipJournal(state / "ownership.sqlite3").get("stateful")
            if reservation:
                # Reconcile disposable fixture state using the stopped-broker admin route.
                await asyncio.to_thread(
                    BROKER_TEST_OPERATIONS[state]["recover"], "stateful", reservation["context_id"]
                )


async def remote_call(backend, operation, arguments, context_id, *, request_id=None):
    tool_id = "stateful/" + operation
    description = data(await backend.invoke("describe_tool", {"tool_id": tool_id}))
    payload = {
        "tool_id": tool_id,
        "catalog_revision": description["catalog_revision"],
        "arguments": arguments,
        "context_id": context_id,
    }
    if request_id:
        payload["request_id"] = request_id
    return await backend.invoke("call_write_tool" if request_id else "call_read_tool", payload)


async def test_persistent_stdio_review_survives_http_turnover_and_refresh(stack, remote_backend):
    async with exclusive_app(stack) as settings:
        owner, other = remote_backend(settings), remote_backend(settings)
        lease = data(await owner.invoke("acquire_service", {"service_id": "stateful"}))
        context = lease["context_id"]
        initial = data(await remote_call(owner, "inspect", {}, context))
        plan = data(
            await remote_call(
                owner, "prepare", {"candidate": "reviewed"}, context, request_id="wire-prepare-001"
            )
        )
        data(await other.invoke("refresh_services", {}))
        async with http_session(
            settings.url + "/mcp", settings.token, client_id=owner.client_id
        ) as independent:
            description = data(await owner.invoke("describe_tool", {"tool_id": "stateful/inspect"}))
            assert (
                data(
                    await independent.call_tool(
                        "call_read_tool",
                        {
                            "tool_id": "stateful/inspect",
                            "catalog_revision": description["catalog_revision"],
                            "arguments": {},
                            "context_id": context,
                        },
                    )
                )
                == initial
            )
        blocked = await remote_call(
            other, "apply", {"plan_id": plan["plan_id"]}, context, request_id="wire-steal-001"
        )
        assert blocked.is_error
        assert "another frontend" in blocked.content[0].text
        applied = data(
            await remote_call(
                owner, "apply", {"plan_id": plan["plan_id"]}, context, request_id="wire-apply-001"
            )
        )
        assert applied == {"instance": initial["instance"], "value": "reviewed"}
        data(
            await owner.invoke("release_service", {"service_id": "stateful", "context_id": context})
        )
        replacement = data(await other.invoke("acquire_service", {"service_id": "stateful"}))
        after = data(await remote_call(other, "inspect", {}, replacement["context_id"]))
        assert after["instance"] != initial["instance"] and after["value"] == ""
        stale = await remote_call(other, "inspect", {}, context)
        assert stale.is_error


async def test_two_stdio_frontends_cannot_share_or_release_each_others_context(stack):
    async with exclusive_app(stack), AsyncExitStack() as contexts:
        clients = []
        state = stack[0]
        for _ in range(2):
            streams = await contexts.enter_async_context(
                stdio_client(
                    StdioServerParameters(
                        command=sys.executable,
                        args=["-m", "universal_tools", "--state", str(state), "proxy"],
                    )
                )
            )
            session = await contexts.enter_async_context(ClientSession(*streams))
            initialization = await session.initialize()
            assert "Synthetic session guidance" not in initialization.instructions
            clients.append(session)
        owner, other = clients
        public = data(await other.call_tool("describe_service", {"service_id": "stateful"}))
        lease = data(await owner.call_tool("acquire_service", {"service_id": "stateful"}))
        context = lease["context_id"]
        owned = data(await owner.call_tool("describe_service", {"service_id": "stateful"}))
        assert owned["metadata_source"] == "owned_session" and owned["context_id"] == context
        assert owned["initialization"] != public["initialization"]
        assert data(await other.call_tool("describe_service", {"service_id": "stateful"})) == public
        assert (await other.call_tool("acquire_service", {"service_id": "stateful"})).is_error
        assert (
            await other.call_tool(
                "release_service", {"service_id": "stateful", "context_id": context}
            )
        ).is_error
        info = data(await other.call_tool("service_session_status", {"service_id": "stateful"}))
        assert info == {"service_id": "stateful", "state": "owned_by_another"}
        description = data(await owner.call_tool("describe_tool", {"tool_id": "stateful/inspect"}))
        payload = {
            "tool_id": "stateful/inspect",
            "catalog_revision": description["catalog_revision"],
            "arguments": {},
            "context_id": context,
        }
        assert (await other.call_tool("call_read_tool", payload)).is_error
        before = data(await owner.call_tool("call_read_tool", payload))
        assert before["instance"] in owned["initialization"]["instructions"]
        data(await other.call_tool("refresh_services"))
        assert data(await owner.call_tool("call_read_tool", payload)) == before
        after_owned = data(await owner.call_tool("describe_service", {"service_id": "stateful"}))
        after_other = data(await other.call_tool("describe_service", {"service_id": "stateful"}))
        assert after_owned["initialization"] == owned["initialization"]
        assert after_other["initialization"] == public["initialization"]
        assert after_other["observed_generation"] == public["observed_generation"]
        assert "context_id" not in after_other
        data(
            await owner.call_tool(
                "release_service", {"service_id": "stateful", "context_id": context}
            )
        )


async def test_frontend_pool_shutdown_does_not_release_its_owned_native_session(
    stack, remote_backend
):
    async with exclusive_app(stack) as settings:
        owner, other = remote_backend(settings), remote_backend(settings)
        context = data(await owner.invoke("acquire_service", {"service_id": "stateful"}))[
            "context_id"
        ]
        before = data(await remote_call(owner, "inspect", {}, context))
        description = data(await owner.invoke("describe_tool", {"tool_id": "stateful/inspect"}))
        await owner.aclose()
        assert not owner.pool._workers
        assert (await other.invoke("acquire_service", {"service_id": "stateful"})).is_error
        assert (await remote_call(other, "inspect", {}, context)).is_error
        async with http_session(
            settings.url + "/mcp", settings.token, client_id=owner.client_id
        ) as reconnect:
            assert (
                data(
                    await reconnect.call_tool(
                        "call_read_tool",
                        {
                            "tool_id": "stateful/inspect",
                            "arguments": {},
                            "catalog_revision": description["catalog_revision"],
                            "context_id": context,
                        },
                    )
                )
                == before
            )
            data(
                await reconnect.call_tool(
                    "release_service",
                    {
                        "service_id": "stateful",
                        "context_id": context,
                    },
                )
            )


async def test_real_stdio_disconnect_marks_context_lost_without_restarting(stack, remote_backend):
    async with exclusive_app(stack) as settings:
        owner = remote_backend(settings)
        lease = data(await owner.invoke("acquire_service", {"service_id": "stateful"}))
        result = await remote_call(
            owner, "disconnect", {}, lease["context_id"], request_id="wire-crash-001"
        )
        assert result.is_error
        status = data(await owner.invoke("get_request_status", {"request_id": "wire-crash-001"}))
        assert status["status"] == "uncertain"
        info = data(await owner.invoke("service_session_status", {"service_id": "stateful"}))
        assert info["state"] == "lost"
        assert (await owner.invoke("acquire_service", {"service_id": "stateful"})).is_error
        assert (await remote_call(owner, "inspect", {}, lease["context_id"])).is_error


async def test_real_stdio_timeout_marks_write_uncertain_and_context_lost(stack, remote_backend):
    # Separate cold SDK/process startup variance from the deliberately long write.
    async with exclusive_app(stack, timeout=5.0) as settings:
        owner = remote_backend(settings)
        lease = data(await owner.invoke("acquire_service", {"service_id": "stateful"}))
        result = await remote_call(
            owner,
            "slow_edit",
            {"candidate": "maybe-applied", "delay": 30},
            lease["context_id"],
            request_id="wire-timeout-001",
        )
        assert result.is_error
        assert (
            data(await owner.invoke("get_request_status", {"request_id": "wire-timeout-001"}))[
                "status"
            ]
            == "uncertain"
        )
        assert (
            data(await owner.invoke("service_session_status", {"service_id": "stateful"}))["state"]
            == "lost"
        )


async def test_ownerless_http_client_cannot_acquire_an_exclusive_service(stack):
    async with exclusive_app(stack) as settings:
        async with http_session(settings.url + "/mcp", settings.token) as session:
            result = await session.call_tool("acquire_service", {"service_id": "stateful"})
            assert result.is_error
            assert "identified" in result.content[0].text


async def test_exclusive_http_transport_preserves_owner_across_fresh_broker_requests(
    stack, remote_backend
):
    state, settings, _ = stack
    path = state / "services/math.json"
    original = path.read_bytes()
    config = json.loads(original)
    atomic_json(path, {**config, "session_mode": "exclusive"})
    owner, other = remote_backend(settings), remote_backend(settings)
    try:
        data(await owner.invoke("refresh_services", {}))

        context = data(await owner.invoke("acquire_service", {"service_id": "math"}))["context_id"]
        description = data(await owner.invoke("describe_tool", {"tool_id": "math/echo"}))
        payload = {
            "tool_id": "math/echo",
            "catalog_revision": description["catalog_revision"],
            "arguments": {"value": "owned-http"},
            "context_id": context,
        }
        assert data(await owner.invoke("call_read_tool", payload))["value"] == "owned-http"
        concurrent = await asyncio.gather(
            *(
                owner.invoke(
                    "call_read_tool", {**payload, "arguments": {"value": str(index), "delay": 0.05}}
                )
                for index in range(6)
            )
        )
        assert [data(result)["value"] for result in concurrent] == [
            str(index) for index in range(6)
        ]
        assert len(owner.pool._workers) == 4
        data(await other.invoke("refresh_services", {}))
        assert (await other.invoke("call_read_tool", payload)).is_error
        assert data(await owner.invoke("call_read_tool", payload))["value"] == "owned-http"
        data(await owner.invoke("release_service", {"service_id": "math", "context_id": context}))
        replacement = data(await other.invoke("acquire_service", {"service_id": "math"}))
        assert replacement["context_id"] != context
        data(
            await other.invoke(
                "release_service",
                {
                    "service_id": "math",
                    "context_id": replacement["context_id"],
                },
            )
        )
    finally:
        path.write_bytes(original)
        data(await owner.invoke("refresh_services", {}))


async def test_crashed_broker_requires_stopped_admin_recovery_before_new_ownership(
    stack, remote_backend
):
    async with exclusive_app(stack) as settings:
        state = stack[0]
        owner, other = remote_backend(settings), remote_backend(settings)
        context = data(await owner.invoke("acquire_service", {"service_id": "stateful"}))[
            "context_id"
        ]
        original = data(await remote_call(owner, "inspect", {}, context))
        await asyncio.to_thread(BROKER_TEST_OPERATIONS[state]["crash_restart"])
        assert (await owner.invoke("list_services", {})).is_error  # Old MCP session is gone.
        rows = data(await owner.invoke("list_services", {}))["services"]
        assert (
            next(row for row in rows if row["service_id"] == "stateful")["status"]
            == "recovery_required"
        )
        assert (
            data(await owner.invoke("service_session_status", {"service_id": "stateful"}))[
                "context_id"
            ]
            == context
        )
        assert (await other.invoke("acquire_service", {"service_id": "stateful"})).is_error
        assert (
            await owner.invoke("release_service", {"service_id": "stateful", "context_id": context})
        ).is_error
        with pytest.raises(BrokerError, match="already owns"):
            recover_context(state, "stateful", context, acknowledged=True)
        await asyncio.to_thread(BROKER_TEST_OPERATIONS[state]["recover"], "stateful", context)
        assert (await other.invoke("broker_status", {})).is_error  # Second restart, no replay.
        replacement = data(await other.invoke("acquire_service", {"service_id": "stateful"}))[
            "context_id"
        ]
        current = data(await remote_call(other, "inspect", {}, replacement))
        assert replacement != context and current["instance"] != original["instance"]
