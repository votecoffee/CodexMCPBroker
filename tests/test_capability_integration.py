"""Search inner operations through actual stdio frontends and MCP HTTP app/broker hops."""

import asyncio
import json
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from test_elicitation_integration import frontends
from test_integration import BROKER_TEST_OPERATIONS, data, free_port
from test_integration import stack as stack

from universal_tools.cli import process_options
from universal_tools.models import atomic_json
from universal_tools.ownership import OwnershipJournal
from universal_tools.surface import RemoteBackend


@asynccontextmanager
async def capability_app(stack, tmp_path, transport="stdio", timeout=3.0):
    state, settings, _ = stack
    path = state / "services/cap-index.json"
    fixture = Path(__file__).parent / "fixtures/capability_mcp.py"
    child, log = None, None
    assert not path.exists()
    try:
        connection = {"type": "stdio", "command": sys.executable, "args": [str(fixture)]}
        if transport == "http":
            port = free_port()
            log = (tmp_path / "catalog-app.log").open("wb")
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
                            raise RuntimeError("synthetic catalog app did not start") from None
                        await asyncio.sleep(0.05)
            connection = {"type": "streamable-http", "url": url}
        atomic_json(
            path,
            {
                "id": "cap-index",
                "display_name": "Synthetic capability app",
                "transport": connection,
                "session_mode": "exclusive",
                "allow_writes": True,
                "allowed_write_tools": ["reload_fixture", "run_feature"],
                "capability_catalog": {"schema_version": 1, "tool": "catalog", "arguments": {}},
                "timeout_seconds": timeout,
            },
        )
        async with RemoteBackend(settings) as admin:
            try:
                statuses = data(await admin.invoke("refresh_services", {}))["services"]
                status = next(s for s in statuses if s["service_id"] == "cap-index")
                assert (
                    status["status"] == "available" and status["capability_catalog"]["count"] == 1
                )
                yield
            finally:
                path.unlink()
                data(await admin.invoke("refresh_services", {}))
                reservation = OwnershipJournal(state / "ownership.sqlite3").get("cap-index")
                if reservation:
                    await asyncio.to_thread(
                        BROKER_TEST_OPERATIONS[state]["recover"],
                        "cap-index",
                        reservation["context_id"],
                    )
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


async def acquire(owner):
    return data(await owner.invoke("acquire_service", {"service_id": "cap-index"}))["context_id"]


async def call(owner, name, arguments, context, request_id=None):
    tool_id = "cap-index/" + name
    definition = data(await owner.invoke("describe_tool", {"tool_id": tool_id}))
    payload = {
        "tool_id": tool_id,
        "catalog_revision": definition["catalog_revision"],
        "arguments": arguments,
        "context_id": context,
    }
    if request_id:
        payload["request_id"] = request_id
    return await owner.invoke("call_write_tool" if request_id else "call_read_tool", payload)


async def matches(owner, query=""):
    return data(
        await owner.invoke(
            "search_tools", {"query": query, "service_id": "cap-index", "kind": "capability"}
        )
    )["matches"]


async def describe(owner, match):
    return await owner.invoke(
        "describe_capability",
        {
            "service_id": "cap-index",
            "capability_id": match["capability_id"],
            "capability_revision": match["capability_revision"],
        },
    )


async def refresh(owner, context):
    return await owner.invoke(
        "refresh_capabilities", {"service_id": "cap-index", "context_id": context}
    )


@pytest.mark.parametrize("transport", ["stdio", "http"])
async def test_discovery_reference_preserves_native_revision_target_and_write_rules(
    stack, tmp_path, transport
):
    async with (
        capability_app(stack, tmp_path, transport),
        frontends(stack[0], None, None) as (owner, other),
    ):
        public = (await matches(other, "counterbore"))[0]
        context = await acquire(owner)
        assert not await matches(owner)
        assert data(await refresh(owner, context))["status"] == "available"
        match = (await matches(owner, "counterbore"))[0]
        assert match["discovery_only"] and not match["callable"]
        reference = data(await describe(owner, match))["discovery"]
        native = data(await call(owner, "describe_feature", reference["arguments"], context))
        assert native["catalog_revision"] == match["app_catalog_revision"]
        result = data(
            await call(
                owner,
                "run_feature",
                {
                    "document": "disposable-document",
                    "capability": match["capability_id"],
                    "arguments": {"diameter": 12.5},
                    "catalog_revision": native["catalog_revision"],
                },
                context,
                "catalog-run-" + transport,
            )
        )
        assert result["commits"] == 1 and result["arguments"] == {"diameter": 12.5}
        assert (await refresh(other, context)).isError
        assert (await describe(other, match)).isError
        assert (await matches(other))[0] == public
        service = data(await other.invoke("describe_service", {"service_id": "cap-index"}))
        assert match["app_catalog_revision"] not in json.dumps(service)


async def test_reloaded_inner_operations_appear_without_changing_outer_tools_or_frontend(
    stack, tmp_path
):
    async with capability_app(stack, tmp_path), frontends(stack[0], None) as (owner,):
        context = await acquire(owner)
        data(await refresh(owner, context))
        previous = (await matches(owner))[0]
        outer = data(await owner.invoke("describe_tool", {"tool_id": "cap-index/run_feature"}))[
            "catalog_revision"
        ]
        surface = (await owner.session.list_tools()).model_dump()
        data(await call(owner, "reload_fixture", {}, context, "catalog-reload-001"))
        data(await owner.invoke("refresh_services", {}))
        assert not await matches(owner, "bolt")
        data(await refresh(owner, context))
        added = (await matches(owner, "bolt"))[0]
        assert added["capability_id"] == "fasteners.bolt"
        assert (await describe(owner, previous)).isError
        assert (
            outer
            == data(await owner.invoke("describe_tool", {"tool_id": "cap-index/run_feature"}))[
                "catalog_revision"
            ]
        )
        assert surface == (await owner.session.list_tools()).model_dump()
        stale = await call(
            owner,
            "run_feature",
            {
                "document": "disposable-document",
                "capability": "holes.drill",
                "arguments": {},
                "catalog_revision": previous["app_catalog_revision"],
            },
            context,
            "catalog-stale-001",
        )
        assert stale.isError
        assert data(await call(owner, "state", {}, context))["commits"] == 0


async def test_global_refresh_preserves_public_snapshot_without_probing_owned_catalog(
    stack, tmp_path
):
    async with capability_app(stack, tmp_path), frontends(stack[0], None, None) as (owner, other):
        public = (await matches(other))[0]
        context = await acquire(owner)
        data(await refresh(owner, context))
        owned = (await matches(owner))[0]
        before = data(await call(owner, "state", {}, context))
        result = data(await other.invoke("refresh_services", {}))
        assert owned["app_catalog_revision"] not in json.dumps(result)
        assert (await matches(other))[0] == public and (await matches(owner))[0] == owned
        assert data(await call(owner, "state", {}, context)) == before


async def test_bad_provider_snapshot_hides_owned_hints_and_retains_healthy_context(stack, tmp_path):
    async with capability_app(stack, tmp_path), frontends(stack[0], None, None) as (owner, other):
        context = await acquire(owner)
        data(await refresh(owner, context))
        before = data(await call(owner, "state", {}, context))["instance"]
        data(
            await call(
                owner, "reload_fixture", {"make_invalid": True}, context, "catalog-invalid-001"
            )
        )
        assert data(await refresh(owner, context))["status"] == "unavailable"
        assert not await matches(owner) and await matches(other)
        assert data(await call(owner, "state", {}, context))["instance"] == before
        data(await call(owner, "reload_fixture", {}, context, "catalog-repair-001"))
        assert data(await refresh(owner, context))["status"] == "available"


async def test_installing_an_opt_in_provider_only_requires_refresh_in_existing_frontend(
    stack, tmp_path
):
    async with frontends(stack[0], None) as (owner,):
        surface = (await owner.session.list_tools()).model_dump()
        assert not await matches(owner)
        async with capability_app(stack, tmp_path):
            data(await owner.invoke("refresh_services", {}))
            assert (await matches(owner, "counterbore"))[0]["kind"] == "capability"
            assert surface == (await owner.session.list_tools()).model_dump()


async def test_provider_timeout_cannot_leave_searchable_owned_hints(stack, tmp_path):
    async with capability_app(stack, tmp_path, timeout=2.0), frontends(stack[0], None) as (owner,):
        context = await acquire(owner)
        data(await refresh(owner, context))
        data(await call(owner, "reload_fixture", {"wait": 10.0}, context, "catalog-stall-001"))
        assert (await refresh(owner, context)).isError
        assert not await matches(owner)
        status = data(await owner.invoke("service_session_status", {"service_id": "cap-index"}))
        assert status["recovery_required"]


async def test_metadata_probe_never_binds_an_approval_route(stack, tmp_path):
    prompts = []

    async def unexpected(_, params):
        prompts.append(params)
        raise AssertionError("metadata discovery must not prompt the host")

    async with capability_app(stack, tmp_path), frontends(stack[0], unexpected) as (owner,):
        context = await acquire(owner)
        data(
            await call(
                owner, "reload_fixture", {"require_confirmation": True}, context, "catalog-form-001"
            )
        )
        assert data(await refresh(owner, context))["status"] == "unavailable"
        assert not prompts
        assert data(await call(owner, "state", {}, context))["commits"] == 0
