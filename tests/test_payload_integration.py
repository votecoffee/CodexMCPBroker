"""Full stdio/HTTP payload preservation and local policy, using disposable apps only."""

import asyncio
import base64
import hashlib
import json
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from test_elicitation_integration import frontends
from test_integration import data, free_port
from test_integration import stack as stack

from universal_tools.cli import process_options
from universal_tools.models import (
    DEFAULT_ARGUMENT_BYTES,
    MAX_REQUEST_BYTES,
    atomic_json,
)
from universal_tools.surface import RemoteBackend


@asynccontextmanager
async def payload_app(stack, tmp_path, transport, *, limit=DEFAULT_ARGUMENT_BYTES):
    state, settings, _ = stack
    path = state / "services/payload.json"
    fixture = Path(__file__).parent / "fixtures/payload_mcp.py"
    child, log = None, None
    assert not path.exists()
    try:
        if transport == "stdio":
            connection = {
                "type": "stdio",
                "command": sys.executable,
                "args": [str(fixture)],
            }
        else:
            port = free_port()
            log = (tmp_path / "payload-app.log").open("wb")
            child = subprocess.Popen(
                [sys.executable, str(fixture), "--port", str(port)],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                **process_options(),
            )
            url = f"http://127.0.0.1:{port}/mcp"
            deadline = time.monotonic() + 15
            async with httpx.AsyncClient(timeout=0.5, trust_env=False) as client:
                while True:
                    try:
                        await client.get(url)
                        break
                    except httpx.TransportError:
                        if time.monotonic() > deadline or child.poll() is not None:
                            raise RuntimeError("synthetic payload app did not start") from None
                        await asyncio.sleep(0.05)
            connection = {"type": "streamable-http", "url": url}
        atomic_json(
            path,
            {
                "id": "payload",
                "display_name": "Synthetic payload app",
                "transport": connection,
                "session_mode": "exclusive",
                "allow_writes": True,
                "allowed_write_tools": ["stage_payload"],
                "max_argument_bytes": limit,
                "timeout_seconds": 20.0,
            },
        )
        async with RemoteBackend(settings) as admin:
            try:
                statuses = data(await admin.invoke("refresh_services", {}))["services"]
                assert (
                    next(s for s in statuses if s["service_id"] == "payload")["status"]
                    == "available"
                )
                yield
            finally:
                path.unlink()
                data(await admin.invoke("refresh_services", {}))
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        if log is not None:
            log.close()


async def invoke(frontend, name, arguments, context, *, request_id=None):
    description = data(await frontend.invoke("describe_tool", {"tool_id": "payload/" + name}))
    payload = {
        "tool_id": "payload/" + name,
        "catalog_revision": description["catalog_revision"],
        "arguments": arguments,
        "context_id": context,
    }
    if request_id is not None:
        payload["request_id"] = request_id
    return await frontend.invoke("call_write_tool" if request_id else "call_read_tool", payload)


@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_tool_write_scope_and_ownership_survive_both_hops(stack, tmp_path, transport):
    async with (
        payload_app(stack, tmp_path, transport),
        frontends(stack[0], None, None) as (owner, other),
    ):
        context = data(await owner.invoke("acquire_service", {"service_id": "payload"}))[
            "context_id"
        ]
        metadata = data(await owner.invoke("describe_service", {"service_id": "payload"}))
        assert metadata["policy"] == {
            "allow_writes": True,
            "allowed_write_tools": ["stage_payload"],
        }
        assert metadata["limits"]["max_argument_bytes"] == DEFAULT_ARGUMENT_BYTES
        assert metadata["limits"]["max_request_body_bytes"] == MAX_REQUEST_BYTES
        matches = data(await owner.invoke("search_tools", {"service_id": "payload"}))["matches"]
        assert {m["tool_id"]: m["callable"] for m in matches} == {
            "payload/roundtrip": True,
            "payload/stage_payload": True,
            "payload/state": True,
            "payload/unapproved_edit": False,
        }
        denied_id = "policy-denied-" + transport
        blocked = await invoke(owner, "unapproved_edit", {}, context, request_id=denied_id)
        assert blocked.isError and "allowed_write_tools" in blocked.content[0].text
        assert (await owner.invoke("get_request_status", {"request_id": denied_id})).isError
        arguments = {"encoded": base64.b64encode(b"fixture").decode(), "number": 2**63 - 1}
        stolen_id = "policy-stolen-" + transport
        assert (
            await invoke(other, "stage_payload", arguments, context, request_id=stolen_id)
        ).isError
        assert (await owner.invoke("get_request_status", {"request_id": stolen_id})).isError
        assert data(await invoke(owner, "state", {}, context)) == {"commits": 0, "reads": 0}
        approved_id = "policy-approved-" + transport
        result = data(
            await invoke(owner, "stage_payload", arguments, context, request_id=approved_id)
        )
        assert result["commits"] == 1 and result["number"] == 2**63 - 1
        assert (
            await invoke(owner, "stage_payload", arguments, context, request_id=approved_id)
        ).isError
        assert data(await invoke(owner, "state", {}, context))["commits"] == 1


async def test_default_budget_rejects_bulk_calls_without_app_effects_or_write_claims(
    stack, tmp_path
):
    async with payload_app(stack, tmp_path, "stdio"), frontends(stack[0], None) as (owner,):
        context = data(await owner.invoke("acquire_service", {"service_id": "payload"}))[
            "context_id"
        ]
        result = await invoke(owner, "roundtrip", {"payload": {"text": "x" * 330_587}}, context)
        assert result.isError and "max_argument_bytes" in result.content[0].text
        result = await invoke(
            owner,
            "stage_payload",
            {"encoded": base64.b64encode(b"x" * 1024 * 1024).decode(), "number": 0},
            context,
            request_id="default-budget-001",
        )
        assert result.isError and "max_argument_bytes" in result.content[0].text
        assert (
            await owner.invoke("get_request_status", {"request_id": "default-budget-001"})
        ).isError
        assert data(await invoke(owner, "state", {}, context)) == {"commits": 0, "reads": 0}


@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_large_payloads_results_unicode_and_64_bit_integers_survive(
    stack, tmp_path, transport
):
    async with (
        payload_app(stack, tmp_path, transport, limit=2 * 1024 * 1024),
        frontends(stack[0], None) as (owner,),
    ):
        context = data(await owner.invoke("acquire_service", {"service_id": "payload"}))[
            "context_id"
        ]
        blob = bytes(range(256)) * 4096  # One MiB before base64 and JSON overhead.
        result = data(
            await invoke(
                owner,
                "stage_payload",
                {"encoded": base64.b64encode(blob).decode(), "number": 2**63 - 1},
                context,
                request_id="large-stage-" + transport,
            )
        )
        assert result == {
            "bytes": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest(),
            "number": 2**63 - 1,
            "commits": 1,
        }
        expected = {
            "text": 'é😀\\"\n' * 135_000,
            "signed": -(2**63),
            "unsigned": 2**64 - 1,
        }
        forwarded = await invoke(owner, "roundtrip", {"payload": expected}, context)
        assert data(forwarded) == expected
        assert json.loads(forwarded.content[0].text) == expected
        assert type(data(forwarded)["unsigned"]) is int


async def test_eight_mib_argument_budget_accepts_exact_limit_and_rejects_one_more_byte(
    stack, tmp_path
):
    async with (
        payload_app(stack, tmp_path, "stdio", limit=8 * 1024 * 1024),
        frontends(stack[0], None) as (owner,),
    ):
        context = data(await owner.invoke("acquire_service", {"service_id": "payload"}))[
            "context_id"
        ]
        # Twenty-three bytes for the nested argument envelope, plus the ASCII value.
        limit = 8 * 1024 * 1024
        arguments = {"payload": {"text": "x" * (limit - 23)}}
        assert len(json.dumps(arguments, separators=(",", ":")).encode()) == limit
        assert data(await invoke(owner, "roundtrip", arguments, context)) == arguments["payload"]
        arguments["payload"]["text"] += "x"
        too_large = await invoke(owner, "roundtrip", arguments, context)
        assert too_large.isError and "max_argument_bytes" in too_large.content[0].text
        assert data(await invoke(owner, "state", {}, context)) == {"commits": 0, "reads": 1}


@pytest.mark.parametrize("streamed", [False, True])
async def test_http_body_limit_rejects_declared_and_streamed_oversize_requests(stack, streamed):
    settings = stack[1]
    envelope = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "broker_status", "_meta": {"padding": ""}},
    }
    prefix_size = len(json.dumps(envelope, separators=(",", ":")).encode())
    envelope["params"]["_meta"]["padding"] = "x" * (MAX_REQUEST_BYTES + 1 - prefix_size)
    body = json.dumps(envelope, separators=(",", ":")).encode()
    assert len(body) == MAX_REQUEST_BYTES + 1

    async def chunks():
        for offset in range(0, len(body), 512 * 1024):
            yield body[offset : offset + 512 * 1024]

    async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
        response = await client.post(
            settings.url + "/mcp",
            headers={
                "Authorization": f"Bearer {settings.token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            content=chunks() if streamed else body,
        )
        assert response.status_code == 413
    async with RemoteBackend(settings) as frontend:
        assert data(await frontend.invoke("broker_status", {}))["version"]
