"""Opt-in bulk transport with isolated brokers and synthetic receivers only."""

import asyncio
import hashlib
import json
import subprocess
import sys
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import httpx
import pytest
from mcp.types import CallToolResult
from test_elicitation_integration import frontends
from test_integration import data, free_port

from universal_tools.cli import configure_request_limit, initialize, probe, process_options
from universal_tools.identity import CLIENT_HEADER
from universal_tools.models import (
    MAX_ARGUMENT_BYTES,
    MAX_REQUEST_BODY_BYTES,
    BrokerError,
    atomic_json,
)
from universal_tools.surface import RemoteBackend


@contextmanager
def isolated_broker(state, request_bytes):
    settings = initialize(state, free_port())
    configure_request_limit(state, request_bytes)
    # Reload rather than retaining the pre-configuration settings object.
    settings = initialize(state, settings.port)
    with (state / "broker.log").open("wb") as log:
        child = subprocess.Popen(
            [sys.executable, "-m", "universal_tools", "--state", str(state), "serve"],
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            **process_options(),
        )
        try:
            deadline = time.monotonic() + 20
            while not probe(settings):
                if time.monotonic() > deadline or child.poll() is not None:
                    raise RuntimeError("isolated bulk broker did not start")
                time.sleep(0.05)
            yield state, settings
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)


@pytest.fixture(scope="module")
def bulk_stack(tmp_path_factory):
    with isolated_broker(tmp_path_factory.mktemp("bulk-stack"), MAX_REQUEST_BODY_BYTES) as stack:
        yield stack


@pytest.fixture(scope="module")
def small_stack(tmp_path_factory):
    with isolated_broker(tmp_path_factory.mktemp("small-stack"), 4096) as stack:
        yield stack


@asynccontextmanager
async def bulk_app(stack, tmp_path, transport, *, limit=MAX_ARGUMENT_BYTES, mode="exclusive"):
    state, settings = stack
    path = state / "services/bulk.json"
    fixture = Path(__file__).parent / "fixtures/large_payload_mcp.py"
    child = log = None
    try:
        if transport == "stdio":
            connection = dict(type="stdio", command=sys.executable, args=[str(fixture)])
        else:
            port = free_port()
            log = (tmp_path / "app.log").open("wb")
            child = subprocess.Popen(
                [sys.executable, str(fixture), "--port", str(port)],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                **process_options(),
            )
            connection = dict(type="streamable-http", url=f"http://127.0.0.1:{port}/mcp")
            deadline = time.monotonic() + 20
            async with httpx.AsyncClient(timeout=0.5, trust_env=False) as client:
                while True:
                    try:
                        async with client.stream("GET", connection["url"]):
                            pass
                        break
                    except httpx.TransportError:
                        if child.poll() is not None or time.monotonic() > deadline:
                            raise RuntimeError("synthetic HTTP receiver did not start") from None
                        await asyncio.sleep(0.05)
        atomic_json(
            path,
            dict(
                id="bulk",
                display_name="Synthetic bulk receiver",
                transport=connection,
                session_mode=mode,
                allow_writes=True,
                allowed_write_tools=["stage_candidate"],
                max_argument_bytes=limit,
                timeout_seconds=60.0,
            ),
        )
        async with RemoteBackend(settings) as admin:
            try:
                rows = data(await admin.invoke("refresh_services", {}))["services"]
                assert (
                    next(row for row in rows if row["service_id"] == "bulk")["status"]
                    == "available"
                )
                yield admin
            finally:
                path.unlink(missing_ok=True)
                data(await admin.invoke("refresh_services", {}))
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
        if log is not None:
            log.close()


async def call(client, context, arguments, request_id, *, name="stage_candidate"):
    description = data(await client.invoke("describe_tool", dict(tool_id="bulk/" + name)))
    payload = dict(
        tool_id="bulk/" + name,
        catalog_revision=description["catalog_revision"],
        arguments=arguments,
    )
    if context:
        payload["context_id"] = context
    if request_id:
        payload["request_id"] = request_id
    return await client.invoke("call_write_tool" if request_id else "call_read_tool", payload)


@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_sixteen_million_utf16_units_survive_both_hops(bulk_stack, tmp_path, transport):
    async with (
        bulk_app(bulk_stack, tmp_path, transport),
        frontends(bulk_stack[0], None, None) as (owner, other),
    ):
        context = data(await owner.invoke("acquire_service", dict(service_id="bulk")))["context_id"]
        metadata = data(await owner.invoke("describe_service", dict(service_id="bulk")))
        assert metadata["limits"]["max_request_body_bytes"] == MAX_REQUEST_BODY_BYTES
        assert metadata["limits"]["max_argument_bytes"] == MAX_ARGUMENT_BYTES
        # Well-formed XML at a common engineering app's 16,777,216 UTF-16-unit
        # boundary. This is about 48 MiB of UTF-8 before the RPC envelope.
        text = "<r>" + "深" * (16 * 1024 * 1024 - 7) + "</r>"
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
        request = "bulk-valid-" + transport
        assert (
            await call(other, context, {"candidate_xml": "foreign"}, request + "-stolen")
        ).is_error
        result = data(await call(owner, context, {"candidate_xml": text}, request))
        assert result == dict(
            sha256=expected,
            utf8_bytes=len(text.encode("utf-8")),
            utf16_units=16 * 1024 * 1024,
            commits=1,
        )
        assert (
            data(await owner.invoke("get_request_status", dict(request_id=request)))["status"]
            == "completed"
        )
        assert (await call(owner, context, {"candidate_xml": text}, request)).is_error
        assert data(await call(owner, context, {}, None, name="state")) == {"commits": 1}
        data(await owner.invoke("release_service", dict(service_id="bulk", context_id=context)))


def test_running_broker_refuses_limit_changes_without_mutating_settings(bulk_stack):
    path = bulk_stack[0] / "settings.json"
    before = path.read_bytes()
    with pytest.raises(BrokerError):
        configure_request_limit(bulk_stack[0], 1024)
    assert path.read_bytes() == before


async def test_argument_budget_is_independent_of_the_larger_http_limit(bulk_stack, tmp_path):
    limit = 16 * 1024 * 1024
    async with (
        bulk_app(bulk_stack, tmp_path, "stdio", limit=limit),
        frontends(bulk_stack[0], None) as (owner,),
    ):
        context = data(await owner.invoke("acquire_service", dict(service_id="bulk")))["context_id"]
        overhead = len(json.dumps({"candidate_xml": ""}, separators=(",", ":")).encode())
        arguments = {"candidate_xml": "x" * (limit - overhead)}
        assert data(await call(owner, context, arguments, "bulk-exact"))["commits"] == 1
        arguments["candidate_xml"] += "x"
        rejected = await call(owner, context, arguments, "bulk-too-large")
        assert rejected.is_error and "max_argument_bytes" in rejected.content[0].text
        assert (
            await owner.invoke("get_request_status", dict(request_id="bulk-too-large"))
        ).is_error
        assert data(await call(owner, context, {}, None, name="state")) == {"commits": 1}
        data(await owner.invoke("release_service", dict(service_id="bulk", context_id=context)))


def packet(response):
    assert response.status_code == 200, response.text[:200]
    if response.headers["content-type"].startswith("application/json"):
        return response.json()
    return json.loads(
        next(line[6:] for line in response.text.splitlines() if line.startswith("data: "))
    )


@pytest.mark.parametrize("streamed", [False, True])
async def test_configured_body_limit_rejects_declared_and_chunked_oversize(small_stack, streamed):
    settings = small_stack[1]
    body = b" " * (settings.max_request_body_bytes + 1)

    async def chunks():
        for offset in range(0, len(body), 127):
            yield body[offset : offset + 127]

    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        response = await client.post(
            settings.url + "/mcp",
            headers={
                "Authorization": "Bearer " + settings.token,
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            content=chunks() if streamed else body,
        )
        assert response.status_code == 413
    async with RemoteBackend(settings) as observer:
        assert data(await observer.invoke("broker_status", {}))["version"]


async def test_configured_body_limit_accepts_the_exact_wire_size(small_stack):
    settings = small_stack[1]
    envelope = dict(
        jsonrpc="2.0",
        id=1,
        method="initialize",
        params=dict(
            protocolVersion="2025-11-25",
            capabilities={},
            clientInfo=dict(name="synthetic-boundary-check", version="1"),
        ),
    )
    raw = json.dumps(envelope).encode()
    raw += b" " * (settings.max_request_body_bytes - len(raw))
    assert len(raw) == settings.max_request_body_bytes
    headers = {
        "Authorization": "Bearer " + settings.token,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        response = await client.post(settings.url + "/mcp", headers=headers, content=raw)
        assert packet(response)["result"]["protocolVersion"] == "2025-11-25"
        headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
        await client.delete(settings.url + "/mcp", headers=headers)


async def test_ascii_escaped_http_body_can_exceed_the_canonical_argument_size(bulk_stack, tmp_path):
    async with bulk_app(bulk_stack, tmp_path, "stdio", mode="transient") as admin:
        description = data(
            await admin.invoke("describe_tool", dict(tool_id="bulk/stage_candidate"))
        )
        text = "深" * (4 * 1024 * 1024)
        headers = {
            "Authorization": "Bearer " + bulk_stack[1].token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            CLIENT_HEADER: "a" * 48,
        }
        async with httpx.AsyncClient(timeout=90, trust_env=False) as client:
            response = await client.post(
                bulk_stack[1].url + "/mcp",
                headers=headers,
                json=dict(
                    jsonrpc="2.0",
                    id=1,
                    method="initialize",
                    params=dict(
                        protocolVersion="2025-11-25",
                        capabilities={},
                        clientInfo=dict(name="synthetic-body-check", version="1"),
                    ),
                ),
            )
            packet(response)
            headers["Mcp-Session-Id"] = response.headers["mcp-session-id"]
            headers["MCP-Protocol-Version"] = "2025-11-25"
            response = await client.post(
                bulk_stack[1].url + "/mcp",
                headers=headers,
                json=dict(jsonrpc="2.0", method="notifications/initialized"),
            )
            assert response.status_code == 202
            envelope = dict(
                jsonrpc="2.0",
                id=2,
                method="tools/call",
                params=dict(
                    name="call_write_tool",
                    arguments=dict(
                        tool_id="bulk/stage_candidate",
                        catalog_revision=description["catalog_revision"],
                        arguments=dict(candidate_xml=text),
                        request_id="bulk-ascii-escaped",
                    ),
                ),
            )
            raw = json.dumps(envelope, ensure_ascii=True, separators=(",", ":")).encode()
            assert len(raw) > 24 * 1024 * 1024
            response = await client.post(bulk_stack[1].url + "/mcp", headers=headers, content=raw)
            result = data(CallToolResult.model_validate(packet(response)["result"]))
            assert result["sha256"] == hashlib.sha256(text.encode()).hexdigest()
            assert result["utf8_bytes"] == 12 * 1024 * 1024 and result["commits"] == 1
            await client.delete(bulk_stack[1].url + "/mcp", headers=headers)
